# -*- coding: utf-8 -*-
"""
Gemini LiveAPI ハンドラ（WebSocket プロキシ）
stt_stream.py の GeminiLiveApp をWebアプリ向けに改変

【重要】
- 本ファイルの実装は 01_stt_stream_detailed_spec.md のセクション5-9に準拠する
- LiveAPI の設定値・メソッド名は仕様書のコード引用を正解とする
- 推測でメソッド名や引数を変えてはならない
"""

import asyncio
import base64
import os
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# stt_stream.py から転記（変更禁止）
LIVE_API_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
MAX_AI_CHARS_BEFORE_RECONNECT = 800
LONG_SPEECH_THRESHOLD = 500

# ============================================================
# プロンプト定義（03_prompt_modification_spec.md 準拠）
# テストフェーズ: ハードコード → 最終形: GCS移行
# ============================================================

LIVEAPI_COMMON_RULES = """
## 応答ルール（厳守）

1. 【文字数制限】1回の発話は50文字以内。超過厳禁。
2. 【簡潔さ】要点だけ伝える。修飾語・前置き・繰り返し不要。
3. 【1トピック1ターン】1回の発話で扱う話題は1つだけ。
4. 【ユーザーの番を奪わない】発話したら黙ってユーザーの返答を待つ。
5. 【マークダウン禁止】音声出力のため、記号・箇条書き・URL不可。
6. 【日本語】自然な話し言葉で応答する。書き言葉にならない。
"""

LIVEAPI_SHOP_CARD_RULES = """
## ★★★ ショップカード表示用JSON出力ルール（絶対厳守） ★★★

search_shopsツールの実行結果として店舗情報をユーザーに伝える場合は、
**必ず以下のJSON形式で出力**すること。
このJSONはシステムがパースしてショップカードUIに表示する。
JSON形式で出力しないと、ショップカードが表示されずユーザー体験が大きく損なわれる。
※ このJSON出力には50文字制限は適用されない（50文字制限はTTS音声応答のみ）。

### 出力条件
- 店舗を提案・紹介する場合 → **必ずJSON形式で出力**
- 深掘り質問への回答（既提案店についての質問） → 自然な文章でOK（JSON不要）

### JSON形式（厳守）
- 応答は必ず { で始まり } で終わること
- JSON以外のテキストを前後に付けない
- マークダウンのコードブロックで囲まない

### JSON構造
{
  "message": "ユーザーへのメッセージ全文（A:導入 + B:店舗リスト + C:会話の導き）",
  "shops": [
    {
      "name": "正式な店舗名",
      "area": "最寄り駅またはエリア名",
      "category": "料理ジャンル",
      "description": "料理内容・体験価値・雰囲気を含む要約（2〜3文）",
      "rating": 4.5,
      "reviewCount": 150,
      "priceRange": "ランチ1,500円〜、ディナー6,000円〜8,000円",
      "location": "最寄り駅・エリア",
      "image": "店舗画像URL",
      "highlights": ["看板メニューや特徴", "雰囲気や設備の特徴", "利用シーンの特徴"],
      "tips": "来店時のおすすめポイント",
      "specialty": "看板メニューや得意料理",
      "atmosphere": "雰囲気",
      "features": "特色",
      "hotpepper_url": "ホットペッパーURL",
      "maps_url": "Google Maps URL",
      "tabelog_url": "食べログURL",
      "gnavi_url": "ぐるなびURL",
      "tripadvisor_url": "TripAdvisor URL",
      "tripadvisor_rating": 4.0,
      "tripadvisor_reviews": 200,
      "latitude": 35.6762,
      "longitude": 139.6503
    }
  ]
}

### messageフィールドの構成（A+B+C）
A. 導入部: 「おすすめの5軒はこちらです。」
B. 店舗リスト: 各店を「**店舗名**（駅名）- 説明（2〜3文）」形式で記載
C. 会話の導き: 定型文ではなく、状況に応じた自然な問いかけ

### 予算表記の使い分け（厳守）
- messageフィールド内 → 漢数字（五千円、一万円）：音声読み上げ用
- priceRangeフィールド → 数字+カンマ形式（5,000円、10,000円）：カード表示用

### 重要な注意
- 新規検索では5軒を提案する
- 実在する店舗のみ提案する（架空・閉店済み禁止）
- 店舗名はmessage内で太字（**店舗名**）にする
- 日時ワード（明日、ランチ、◯時等）が含まれる場合はAI電話予約機能を案内し「予約依頼画面」を含める
- 誤読防止: 老舗→しにせ、明太子→めんたいこ 等、TTS誤読されやすい単語は置き換え
"""

LIVEAPI_CHAT_SYSTEM = """あなたはグルメAIアシスタントです。
ユーザーのお店探しを手伝います。

## 役割
- ユーザーの希望を聞いて、お店の条件を整理する。
- 条件が揃ったら search_shops ツールを呼び出して店舗検索を実行する。

## 応答スタイル
- フレンドリーで親しみやすい口調。
- 「どのあたりで探しますか？」のように、1つずつ質問する。
- ユーザーが条件を言ったら、短く確認して次の質問へ進む。

## 会話フロー（1ターン検索を最優先）
- ユーザーが条件を1つでも言ったら、即座に search_shops ツールを呼び出す。
- 追加質問は一切しない。予算・人数・シーンなどを聞き返さない。
- 検索実行後に質問を続けることは禁止。

{shop_card_rules}

{common_rules}
"""

LIVEAPI_CONCIERGE_SYSTEM = """あなたはグルメコンシェルジュです。
高級レストランのコンシェルジュのように、丁寧にユーザーの好みを引き出してください。

## 役割
- 会話のキャッチボールを通じて、ユーザーの本当の希望を引き出す。
- 一方的に質問を並べるのではなく、ユーザーの回答に寄り添い、深掘りする。
- 条件が十分に揃ったら、search_shopsツールを呼び出して店舗検索を実行する。

{user_context}

## 質問ルール（厳守）
- 1ターンの質問は最大3つまで。それ以上は絶対に聞かない。
- 理想は1ターン1質問。必要な場合のみ2-3に増やす。
- ユーザーが答えやすい順番で聞く（まず大枠、次に詳細）。

## 禁止事項
以下のように一度に大量の質問を並べることは禁止：
「料理のジャンルは？エリアは？人数は？目的は？予算は？雰囲気は？」
→ ユーザーは一度にこれだけの質問には答えられない。
→ これはコンシェルジュではなくアンケートである。

## 正しい会話の進め方
2ターン目以降: ユーザーの回答に応じて自然に深掘りする。
例: 「接待ですね。和食と洋食、どちらがお好みですか？」
例: 「お二人でしたら、カウンターのお店も素敵ですよ。いかがですか？」

## 応答スタイル
- 丁寧語（です・ます調）を基本とする。
- 押しつけず、提案する姿勢。「いかがですか？」「よろしければ」を活用。
- ユーザーの発言を受け止めてから次に進む。「素敵ですね」「なるほど」等。

## 【短期記憶・セッション行動ルール（最重要・厳守）】

### 1. 短期記憶の前提
あなたは会話履歴の内容を記憶している前提で行動すること。
会話中に一度確定・明示された情報は、ユーザーが条件を変えない限り有効。
「覚えていない前提」での聞き直しは絶対に禁止。

### 2. 記憶対象（会話履歴から把握すべき情報）
- 利用目的・シーン（接待、デート、忘年会、女子会、家族利用 等）
- エリア・地域
- 予算感
- 参加人数
- 料理ジャンル
- 店の雰囲気・優先条件（個室、静か、カジュアル、高級 等）

### 3. 重複質問の禁止ルール（絶対厳守）
再質問してよいケース：ユーザーが明示的に条件変更を指示した場合のみ
再質問してはいけないケース：すでに取得済みの条件を理由なく聞き直す行為

### 4. 業態別ヒアリング制御
簡易飲食業態（ラーメン、カフェ、ファーストフード等）は即提案優先

### 5. 再接続時の行動ルール（LiveAPI固有・最重要）
再接続後も会話履歴が再送されるので確認し、同じ質問を繰り返さない

### 6. このルールの優先順位
1位：本セクション > 2位：質問ルール > 3位：応答スタイル

## 【ショップ検索の実行ルール（最重要・絶対厳守）】

### ★★★ 最重要ルール ★★★
お店を探すと判断したら、必ず search_shops ツールを呼び出せ。
「お探ししますね」「お調べしますね」と音声で言うだけでは検索は実行されない。
音声で発話するのではなく、search_shops ツールを実際に呼び出すこと。
これはツール呼び出し（function calling）であり、音声応答ではない。

### 判断の原則
あなたは会話のキャッチボールを通じて、ユーザーの要望が十分に揃ったかを自ら判断する。
条件が揃ったと判断したら、search_shopsツールを呼び出してショップ検索を実行する。

### 検索に必要な最低条件
以下のうち、少なくとも2〜3項目が確定していれば検索可能と判断する:
- エリア・地域
- 料理ジャンルまたは利用シーン
- 予算感（任意・なくても可）
- 人数（任意・なくても可）

### 検索を実行するタイミング
- ユーザーが十分な条件を伝えた時点で、search_shopsツールを呼び出す
- 全項目が揃うまで待つ必要はない
- ユーザーが「もういいから探して」等と言った場合は即座に検索する
- 簡易業態（ラーメン、カフェ等）はエリアだけでも検索可能

### search_shopsツールの呼び出し方法
- search_shops(user_request="六本木 接待 イタリアン 1万円 4名") のようにツールを呼び出す
- user_request にはユーザーの要望を自然言語で要約して渡す
- 「探しますね」等の音声発話は不要。ツール呼び出しだけを行うこと

{shop_card_rules}

{common_rules}
"""


def build_system_instruction(mode: str, user_profile: dict = None) -> str:
    """モードに応じたシステムインストラクションを組み立てる
    （03_prompt_modification_spec.md セクション7.1）

    Args:
        mode: 'chat' or 'concierge'
        user_profile: コンシェルジュモード用。
            {
                'is_first_visit': bool,
                'preferred_name': str or None,
                'name_honorific': str or None,
            }
    """
    if mode == 'concierge':
        # ユーザープロファイルに応じた初期あいさつ指示を構築
        user_context = _build_concierge_user_context(user_profile)
        return LIVEAPI_CONCIERGE_SYSTEM.format(
            common_rules=LIVEAPI_COMMON_RULES,
            user_context=user_context,
            shop_card_rules=LIVEAPI_SHOP_CARD_RULES
        )
    else:
        return LIVEAPI_CHAT_SYSTEM.format(
            common_rules=LIVEAPI_COMMON_RULES,
            shop_card_rules=LIVEAPI_SHOP_CARD_RULES
        )


def _build_concierge_user_context(user_profile: dict = None) -> str:
    """コンシェルジュモードのユーザーコンテキストを構築"""
    if not user_profile:
        # プロファイル不明 → 新規ユーザー扱い
        return _get_first_visit_context()

    is_first_visit = user_profile.get('is_first_visit', True)
    preferred_name = user_profile.get('preferred_name', '')
    name_honorific = user_profile.get('name_honorific', '')

    if is_first_visit or not preferred_name:
        return _get_first_visit_context()
    else:
        return _get_returning_user_context(preferred_name, name_honorific)


def _get_first_visit_context() -> str:
    """新規ユーザー用コンテキスト: 名前を聞く"""
    return """## 初期あいさつ（新規ユーザー）
このユーザーは初めての来訪です。
最初の発話で「初めまして、AIコンシェルジュです。よろしければ、何とお呼びすればいいか教えてください。」と名前を聞いてください。
ユーザーが名前を教えてくれたら、その名前で呼びかけてから、お店探しの会話を始めてください。"""


def _get_returning_user_context(preferred_name: str, name_honorific: str) -> str:
    """リピーター用コンテキスト: 名前で呼びかける"""
    full_name = f"{preferred_name}{name_honorific}"
    return f"""## 初期あいさつ（リピーター）
このユーザーの名前は「{full_name}」です。
最初の発話で「お帰りなさいませ、{full_name}。今日はどのようなお食事をお考えですか？」と名前を呼んで挨拶してください。
以降の会話でも「{full_name}」と呼びかけてください。"""


# ============================================================
# Function Calling 定義（v5 §5.2）
# ============================================================

SEARCH_SHOPS_DECLARATION = types.FunctionDeclaration(
    name="search_shops",
    description="ユーザーの条件に基づいてレストランを検索する。条件が十分に揃ったと判断した時に呼び出す。",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "user_request": types.Schema(
                type="STRING",
                description="ユーザーの要望の要約（例: '六本木 接待 イタリアン 1万円 4名'）"
            )
        },
        required=["user_request"]
    )
)


# ============================================================
# LiveAPISession クラス
# ============================================================

class LiveAPISession:
    """
    1つのブラウザクライアントに対応するLiveAPIセッション

    stt_stream.py の GeminiLiveApp を参考に、以下を移植:
    - Live API接続設定 (仕様書 セクション5.2)
    - 音声送受信 (仕様書 セクション7)
    - 再接続メカニズム (仕様書 セクション8)
    - エラーハンドリング (仕様書 セクション9)
    """

    # ========================================
    # 初期あいさつ用ダミーメッセージ（モード別・言語別）
    # ========================================
    INITIAL_GREETING_TRIGGERS = {
        'chat': {
            'ja': 'こんにちは。お店探しを手伝ってください。',
            'en': 'Hello. I need help finding a restaurant.',
            'zh': '你好，请帮我找餐厅。',
            'ko': '안녕하세요. 레스토랑을 찾아주세요.',
        },
        'concierge': {
            'ja': 'こんにちは。',
            'en': 'Hello.',
            'zh': '你好。',
            'ko': '안녕하세요.',
        },
    }

    def __init__(self, session_id: str, mode: str, language: str,
                 system_prompt: str, socketio, client_sid: str,
                 shop_search_callback=None):
        self.session_id = session_id
        self.mode = mode
        self.language = language
        self.system_prompt = system_prompt  # 外部から受け取る（将来GCS移行対応）
        self.socketio = socketio
        self.client_sid = client_sid
        self._shop_search_callback = shop_search_callback  # v5 §5.5: ショップ検索用コールバック

        # 初期あいさつフェーズ（ダミーメッセージのinput_transcriptionを非表示）
        # （仕様書02 セクション4.5.5）
        self._is_initial_greeting_phase = True

        # Gemini APIクライアント
        api_key = os.getenv("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key)

        # 状態管理（stt_stream.py:387-394 から転記）
        self.user_transcript_buffer = ""
        self.ai_transcript_buffer = ""
        self.conversation_history = []
        self.ai_char_count = 0
        self.needs_reconnect = False
        self.session_count = 0

        # 再接続時のトリガーメッセージ（§7.1）
        self._resume_message = None

        # 非同期キュー
        self.audio_queue_to_gemini = None
        self.is_running = False

    def _build_config(self, with_context=None):
        """
        Live API接続設定を構築

        【厳守】仕様書 セクション5.2 のconfig辞書をそのまま使う。
        """
        instruction = self.system_prompt

        if with_context:
            last_user_message = ""
            for h in reversed(self.conversation_history):
                if h['role'] == 'user':
                    last_user_message = h['text'][:100]
                    break

            instruction += f"""

【これまでの会話の要約】
{with_context}

【重要：必ず守ること】
1. 直前のユーザーの発言「{last_user_message}」に対して短い相槌を入れる
2. 既に話した内容は繰り返さない
"""

        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": instruction,
            "tools": [types.Tool(function_declarations=[SEARCH_SHOPS_DECLARATION])],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "speech_config": {
                "language_code": self._get_speech_language_code(),
            },
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": False,
                    "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
                    "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                    "prefix_padding_ms": 100,
                    "silence_duration_ms": 500,
                }
            },
            "context_window_compression": {
                "sliding_window": {
                    "target_tokens": 32000,
                }
            },
        }
        return config

    def _get_speech_language_code(self):
        """言語コードをLiveAPI形式に変換"""
        lang_map = {
            'ja': 'ja-JP',
            'en': 'en-US',
            'zh': 'zh-CN',
            'ko': 'ko-KR',
        }
        return lang_map.get(self.language, 'ja-JP')

    def enqueue_audio(self, pcm_bytes: bytes):
        """ブラウザから受信したPCMデータをキューに追加"""
        if self.audio_queue_to_gemini and self.is_running:
            try:
                self.audio_queue_to_gemini.put_nowait(pcm_bytes)
            except asyncio.QueueFull:
                pass  # キューが満杯の場合はドロップ

    def stop(self):
        """セッションを停止"""
        self.is_running = False
        self.needs_reconnect = False

    async def run(self):
        """
        メインループ（再接続対応）
        仕様書 セクション6.1 の run() を移植
        """
        self.audio_queue_to_gemini = asyncio.Queue(maxsize=5)
        self.is_running = True

        try:
            while self.is_running:
                self.session_count += 1
                self.ai_char_count = 0
                self.needs_reconnect = False

                context = None
                if self.session_count > 1:
                    context = self._get_context_summary()

                config = self._build_config(with_context=context)

                try:
                    async with self.client.aio.live.connect(
                        model=LIVE_API_MODEL,
                        config=config
                    ) as session:

                        if self.session_count == 1:
                            # 初回接続: ダミーメッセージで初期あいさつを発火
                            self._is_initial_greeting_phase = True
                            trigger_msgs = self.INITIAL_GREETING_TRIGGERS
                            mode_msgs = trigger_msgs.get(self.mode, trigger_msgs['chat'])
                            dummy_text = mode_msgs.get(self.language, mode_msgs['ja'])

                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[types.Part(text=dummy_text)]
                                ),
                                turn_complete=True
                            )
                            logger.info(f"[LiveAPI] 初期あいさつトリガー送信: '{dummy_text}'")
                        else:
                            # 再接続時（v5 §7.1: 履歴再送 → トリガーの2段階送信）
                            self._is_initial_greeting_phase = False
                            self.socketio.emit('live_reconnecting', {},
                                               room=self.client_sid)

                            # 1. 会話履歴turnsを再送（turn_complete=False）
                            await self._send_history_on_reconnect(session)

                            # 2. トリガーメッセージ（turn_complete=True）
                            resume_text = self._resume_message or "続きをお願いします"
                            self._resume_message = None
                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[types.Part(text=resume_text)]
                                ),
                                turn_complete=True
                            )
                            logger.info("[LiveAPI] 再接続: 履歴再送+トリガー送信完了")
                            self.socketio.emit('live_reconnected', {},
                                               room=self.client_sid)

                        await self._session_loop(session)

                        if not self.needs_reconnect:
                            break

                except Exception as e:
                    error_msg = str(e).lower()
                    if any(kw in error_msg for kw in
                           ["1011", "internal error", "disconnected",
                            "closed", "websocket"]):
                        logger.warning(f"[LiveAPI] 接続エラー、3秒後に再接続: {e}")
                        await asyncio.sleep(3)
                        self.needs_reconnect = True
                        continue
                    else:
                        # 致命的エラー: ログ出力して終了（RESTフォールバックなし）
                        logger.error(f"[LiveAPI] 致命的エラー: {e}")
                        break

        except asyncio.CancelledError:
            pass
        finally:
            self.is_running = False
            logger.info(f"[LiveAPI] セッション終了: {self.session_id}")

    async def _session_loop(self, session):
        """
        セッション内ループ
        仕様書 セクション7.1: 2つのタスクを並行実行
        """
        async def send_audio():
            """ブラウザからの音声をLiveAPIに転送"""
            while not self.needs_reconnect and self.is_running:
                try:
                    audio_data = await asyncio.wait_for(
                        self.audio_queue_to_gemini.get(),
                        timeout=0.1
                    )
                    await session.send_realtime_input(
                        audio={"data": audio_data, "mime_type": "audio/pcm;rate=16000"}
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self.needs_reconnect or not self.is_running:
                        return
                    logger.error(f"[LiveAPI] 送信エラー: {e}")
                    self.needs_reconnect = True
                    return

        async def receive():
            """LiveAPIからの応答を受信してブラウザに転送"""
            try:
                await self._receive_and_forward(session)
            except Exception as e:
                if self.needs_reconnect or not self.is_running:
                    return
                error_msg = str(e).lower()
                if any(kw in error_msg for kw in
                       ["1011", "1008", "internal error",
                        "closed", "deadline", "policy"]):
                    logger.warning(f"[LiveAPI] 受信エラー（再接続）: {e}")
                    self.needs_reconnect = True
                else:
                    logger.error(f"[LiveAPI] 受信致命エラー: {e}")
                    raise

        # キューをクリア
        while not self.audio_queue_to_gemini.empty():
            try:
                self.audio_queue_to_gemini.get_nowait()
            except asyncio.QueueEmpty:
                break

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(send_audio())
                tg.create_task(receive())
        except* Exception as eg:
            if not self.needs_reconnect:
                for e in eg.exceptions:
                    error_msg = str(e).lower()
                    if any(kw in error_msg for kw in
                           ["1011", "internal error", "closed", "websocket"]):
                        self.needs_reconnect = True
                    else:
                        logger.error(f"[LiveAPI] タスクエラー: {e}")

    async def _receive_and_forward(self, session):
        """
        LiveAPIレスポンスを受信してSocket.IOでブラウザに転送
        仕様書 セクション7.3 の receive_audio() を移植
        """
        while not self.needs_reconnect and self.is_running:
            turn = session.receive()
            async for response in turn:
                if self.needs_reconnect or not self.is_running:
                    return

                # 1. tool_call: search_shops（v5 §5.4）
                if hasattr(response, 'tool_call') and response.tool_call:
                    await self._handle_tool_call(response.tool_call, session)
                    continue

                if response.server_content:
                    sc = response.server_content

                    # 2. ターン完了
                    if hasattr(sc, 'turn_complete') and sc.turn_complete:
                        self._process_turn_complete()
                        self.socketio.emit('turn_complete', {},
                                           room=self.client_sid)

                        # 初期あいさつフェーズ終了（仕様書02 セクション4.5.5）
                        if self._is_initial_greeting_phase:
                            self._is_initial_greeting_phase = False
                            self.socketio.emit('greeting_done', {},
                                               room=self.client_sid)
                            logger.info("[LiveAPI] greeting_done送信")

                    # 3. 割り込み検知
                    if hasattr(sc, 'interrupted') and sc.interrupted:
                        self.ai_transcript_buffer = ""
                        self.socketio.emit('interrupted', {},
                                           room=self.client_sid)
                        continue

                    # 4. 入力トランスクリプション
                    #    初期あいさつフェーズのinput_transcriptionは転送しない
                    #    （仕様書02 セクション4.5.5）
                    if (hasattr(sc, 'input_transcription')
                            and sc.input_transcription):
                        text = sc.input_transcription.text
                        if text and not self._is_initial_greeting_phase:
                            self.user_transcript_buffer += text
                            self.socketio.emit('user_transcript',
                                               {'text': text},
                                               room=self.client_sid)

                    # 5. 出力トランスクリプション
                    if (hasattr(sc, 'output_transcription')
                            and sc.output_transcription):
                        text = sc.output_transcription.text
                        if text:
                            self.ai_transcript_buffer += text
                            self.socketio.emit('ai_transcript',
                                               {'text': text},
                                               room=self.client_sid)

                    # 6. 音声データ
                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if (hasattr(part, 'inline_data')
                                    and part.inline_data):
                                if isinstance(part.inline_data.data, bytes):
                                    audio_b64 = base64.b64encode(
                                        part.inline_data.data
                                    ).decode('utf-8')
                                    self.socketio.emit('live_audio',
                                                       {'data': audio_b64},
                                                       room=self.client_sid)

    async def _handle_tool_call(self, tool_call, session):
        """
        LLMからのfunction calling応答を処理する（v5 §5.4）
        search_shops の場合、ショップ検索を実行する。
        """
        for fc in tool_call.function_calls:
            if fc.name == "search_shops":
                user_request = fc.args.get("user_request", "")
                logger.info(f"[LiveAPI] search_shops呼び出し: '{user_request}'")

                # 検索開始をブラウザに通知 → 待機アニメーション表示（§3.6.3）
                self.socketio.emit('shop_search_start', {},
                                   room=self.client_sid)

                # ショップ検索を実行
                await self._handle_shop_search(user_request)

                # function responseを返す（LiveAPI confirmed syntax）
                tool_response = types.LiveClientToolResponse(
                    function_responses=[types.FunctionResponse(
                        name=fc.name,
                        id=fc.id,
                        response={"result": "検索結果をユーザーに表示しました"}
                    )]
                )
                await session.send_tool_response(tool_response)
            else:
                logger.warning(f"[LiveAPI] 未知のfunction call: {fc.name}")

    async def _handle_shop_search(self, user_request: str):
        """
        ショップ検索を実行し、結果をブラウザに送信する（v5 §5.5）

        【設計】
        - shop_search_callback を呼び出してショップデータを取得
        - コールバックは SupportAssistant.process_user_message() を内部的に呼び出す
        - 取得したデータはshop_search_resultイベントでブラウザに送信
        """
        if not self._shop_search_callback:
            logger.error("[ShopSearch] shop_search_callback が未設定")
            return

        try:
            shop_data = self._shop_search_callback(user_request, self.language, self.mode)

            if not shop_data or not shop_data.get('shops'):
                logger.info("[ShopSearch] ショップ見つからず")
                return

            shops = shop_data['shops']
            response_text = shop_data.get('response', '')

            # ショップカードデータをブラウザに送信
            self.socketio.emit('shop_search_result', {
                'shops': shops,
                'response': response_text,
            }, room=self.client_sid)

            logger.info(f"[ShopSearch] {len(shops)}件のショップをブラウザに送信")

            # ショップ説明をLiveAPIで1軒ずつ読み上げ（v5 §6）
            await self._describe_shops_via_live(shops)

        except Exception as e:
            logger.error(f"[ShopSearch] エラー: {e}", exc_info=True)

    def _process_turn_complete(self):
        """
        ターン完了時の処理
        stt_stream.py:600-644 から移植
        """
        user_text = ""
        if self.user_transcript_buffer.strip():
            user_text = self.user_transcript_buffer.strip()
            logger.info(f"[LiveAPI] ユーザー: {user_text}")
            self._add_to_history("user", user_text)
            self.user_transcript_buffer = ""

        if self.ai_transcript_buffer.strip():
            ai_text = self.ai_transcript_buffer.strip()
            logger.info(f"[LiveAPI] AI: {ai_text}")
            self._add_to_history("ai", ai_text)

            # 発言が途中で切れているかチェック
            is_incomplete = self._is_speech_incomplete(ai_text)

            # 文字数をカウント
            char_count = len(ai_text)
            self.ai_char_count += char_count
            remaining = MAX_AI_CHARS_BEFORE_RECONNECT - self.ai_char_count
            logger.info(f"[LiveAPI] 累積: {self.ai_char_count}文字 / 残り: {remaining}文字")

            self.ai_transcript_buffer = ""

            # 再接続判定
            if is_incomplete:
                logger.info("[LiveAPI] 発言途切れのため再接続")
                self.needs_reconnect = True
            elif char_count >= LONG_SPEECH_THRESHOLD:
                logger.info(f"[LiveAPI] 長い発話({char_count}文字)のため再接続")
                self.needs_reconnect = True
            elif self.ai_char_count >= MAX_AI_CHARS_BEFORE_RECONNECT:
                logger.info("[LiveAPI] 累積制限到達のため再接続")
                self.needs_reconnect = True

    async def _describe_shops_via_live(self, shops: list):
        """
        ショップ説明をLiveAPIで読み上げ（1軒ごとに再接続）（v5 §6.1）

        【設計根拠】
        stt_stream.py の再接続メカニズムと同じ手法:
        - 累積文字数制限(800文字)を回避するため、1軒ごとに再接続
        - 再接続時に system_prompt にコンテキストを注入
        - send_client_content() でトリガーメッセージを送信
        """
        total = len(shops)

        for i, shop in enumerate(shops):
            if not self.is_running:
                break

            shop_number = i + 1
            is_last = (shop_number == total)

            shop_context = self._format_shop_for_prompt(shop, shop_number, total)

            shop_instruction = self.system_prompt + f"""

【現在のタスク：ショップ紹介】
あなたは今、ユーザーに検索結果のお店を紹介しています。

{shop_context}

【読み上げルール】
1. このお店の特徴を自然な話し言葉で紹介する（3〜5文程度）
2. 店名、ジャンル、エリア、特徴、価格帯を含める
3. マークダウン記法は使わない（音声出力のため）
4. 「{shop_number}軒目は」から始める
5. 紹介が終わったら、次のお店の紹介に自然につなげる。「以上です」とは言わない。
"""
            if is_last:
                shop_instruction += f"5の代わりに: 最後のお店です。紹介後「以上、{total}軒のお店をご紹介しました。気になるお店はありましたか？」で締めてください。\n"

            try:
                config = self._build_config()
                config["system_instruction"] = shop_instruction
                # ショップ説明時はfunction callingのtoolsを外す
                config.pop("tools", None)

                async with self.client.aio.live.connect(
                    model=LIVE_API_MODEL,
                    config=config
                ) as session:
                    trigger_text = f"{shop_number}軒目のお店を紹介してください。"
                    if shop_number == 1:
                        trigger_text = f"検索結果を紹介してください。まず1軒目のお店からお願いします。"

                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[types.Part(text=trigger_text)]
                        ),
                        turn_complete=True
                    )

                    await self._receive_shop_description(session, shop_number)

            except Exception as e:
                logger.error(f"[ShopDesc] ショップ{shop_number}説明エラー: {e}")
                continue

        # 全ショップ説明完了 → 通常会話に復帰
        summary = f"{total}軒のお店を紹介しました。気になるお店はありましたか？"
        self._add_to_history("ai", summary)
        self._resume_message = "ありがとうございます。気になるお店について教えてください。"
        self.needs_reconnect = True  # 通常会話に復帰するために再接続

    async def _receive_shop_description(self, session, shop_number: int):
        """
        ショップ説明専用の応答受信（v5 §6.2）
        turn_completeまで受信して終了。
        """
        turn = session.receive()
        async for response in turn:
            if not self.is_running:
                return

            if response.server_content:
                sc = response.server_content

                # ターン完了
                if hasattr(sc, 'turn_complete') and sc.turn_complete:
                    if self.ai_transcript_buffer.strip():
                        ai_text = self.ai_transcript_buffer.strip()
                        logger.info(f"[ShopDesc] ショップ{shop_number}: {ai_text}")
                        self._add_to_history("ai", ai_text)
                        self.ai_transcript_buffer = ""
                    return

                # 出力トランスクリプション
                if (hasattr(sc, 'output_transcription')
                        and sc.output_transcription):
                    text = sc.output_transcription.text
                    if text:
                        self.ai_transcript_buffer += text
                        self.socketio.emit('ai_transcript',
                                           {'text': text, 'type': 'shop_description'},
                                           room=self.client_sid)

                # 音声データ
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if (hasattr(part, 'inline_data')
                                and part.inline_data):
                            if isinstance(part.inline_data.data, bytes):
                                audio_b64 = base64.b64encode(
                                    part.inline_data.data
                                ).decode('utf-8')
                                self.socketio.emit('live_audio',
                                                   {'data': audio_b64},
                                                   room=self.client_sid)

    def _format_shop_for_prompt(self, shop: dict, number: int, total: int) -> str:
        """ショップ情報をプロンプト用にフォーマット"""
        name = shop.get('name', '不明')
        genre = shop.get('genre', '')
        area = shop.get('area', '')
        budget = shop.get('budget', '')
        description = shop.get('description', '')
        features = shop.get('features', '')

        lines = [f"【{number}/{total}軒目】{name}"]
        if genre:
            lines.append(f"ジャンル: {genre}")
        if area:
            lines.append(f"エリア: {area}")
        if budget:
            lines.append(f"予算: {budget}")
        if description:
            lines.append(f"説明: {description}")
        if features:
            lines.append(f"特徴: {features}")

        return "\n".join(lines)

    def _get_last_user_text(self) -> str:
        """会話履歴から最後のユーザー発言を取得"""
        for h in reversed(self.conversation_history):
            if h['role'] == 'user':
                return h['text']
        return ""

    def _is_speech_incomplete(self, text: str) -> bool:
        """
        発言が途中で切れているかチェック
        stt_stream.py:501-529 から移植
        """
        if not text:
            return False

        text = text.strip()

        normal_endings = ['。', '？', '?', '！', '!', 'ます', 'です',
                          'ね', 'よ', 'した', 'ください']
        for ending in normal_endings:
            if text.endswith(ending):
                return False

        incomplete_patterns = ['、', 'の', 'を', 'が', 'は', 'に', 'で', 'と', 'も', 'や']
        for pattern in incomplete_patterns:
            if text.endswith(pattern):
                return True

        return False

    def _add_to_history(self, role: str, text: str):
        """会話履歴に追加"""
        self.conversation_history.append({"role": role, "text": text})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

    def _get_context_summary(self) -> str:
        """
        再接続時のコンテキスト要約（v5 §3.2.2: 簡素化）
        主力は send_client_content(turns) による履歴再送。
        ここでは最後のAIの質問のみ補足情報として返す。
        """
        if not self.conversation_history:
            return ""

        last_ai = None
        for h in reversed(self.conversation_history):
            if h['role'] == 'ai':
                last_ai = h['text']
                break

        if last_ai and ('?' in last_ai or '？' in last_ai
                        or 'ですか' in last_ai or 'ますか' in last_ai):
            return f"【直前のAIの質問（回答を待っています）】\n{last_ai[:200]}"

        return ""

    async def _send_history_on_reconnect(self, session):
        """
        再接続時に会話履歴をsend_client_content()で再送する（v5 §3.2.1）

        【設計根拠（REST版準拠）】
        REST版では毎回全会話履歴をGemini APIに送信していた。
        LiveAPIのsend_client_content()のturnsパラメータで同等のことを実現。

        - turnsは types.Content のリストとして送信
        - role は "user" または "model"（"ai"ではない）
        - 直近10ターン、各150文字までに制限（トークン消費抑制）
        """
        if not self.conversation_history:
            return

        recent = self.conversation_history[-10:]
        history_turns = []

        for h in recent:
            role = "user" if h['role'] == 'user' else "model"
            text = h['text'][:150]
            history_turns.append(
                types.Content(
                    role=role,
                    parts=[types.Part(text=text)]
                )
            )

        if history_turns:
            await session.send_client_content(
                turns=history_turns,
                turn_complete=False  # まだターンは終わっていない
            )
            logger.info(f"[LiveAPI] 会話履歴 {len(history_turns)} ターン再送")
