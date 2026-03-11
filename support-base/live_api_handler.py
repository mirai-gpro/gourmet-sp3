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
import re
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

LIVEAPI_CHAT_SYSTEM = """あなたはグルメAIアシスタントです。
ユーザーのお店探しを手伝います。

## 役割
- ユーザーの希望を聞いて、お店の条件を整理する。
- 条件が揃ったら店舗検索を実行する。

## 応答スタイル
- フレンドリーで親しみやすい口調。
- 「どのあたりで探しますか？」のように、1つずつ質問する。
- ユーザーが条件を言ったら、短く確認して次の質問へ進む。

## 会話フロー（1ターン検索を最優先）
- ユーザーが条件を1つでも言ったら、即座に「お探ししますね」と言って終わる。
- 追加質問は一切しない。予算・人数・シーンなどを聞き返さない。
- 「お探ししますね」の後に質問を続けることは禁止。

{common_rules}
"""

LIVEAPI_CONCIERGE_SYSTEM = """あなたはグルメコンシェルジュです。
高級レストランのコンシェルジュのように、丁寧にユーザーの好みを引き出してください。

## 役割
- 会話のキャッチボールを通じて、ユーザーの本当の希望を引き出す。
- 一方的に質問を並べるのではなく、ユーザーの回答に寄り添い、深掘りする。
- 条件が十分に揃ったら、「お探ししますね」と言って店舗検索を促す。

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

## 短期記憶ルール（厳守）
- 再接続時、システムインストラクションに【ユーザーの確定済み条件】が注入される。
- 確定済み条件は既にユーザーが回答済み。再度質問してはならない。
- 確定済み条件を踏まえて、未確認の条件のみを質問する。
- 条件が十分に揃ったら「お探ししますね」と言って検索を促す。

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
            user_context=user_context
        )
    else:
        return LIVEAPI_CHAT_SYSTEM.format(common_rules=LIVEAPI_COMMON_RULES)


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
# ショップ提案検知
# ============================================================

SHOP_TRIGGER_KEYWORDS = [
    # 基本形
    'お探ししますね', 'お調べしますね', '探してみますね',
    'ご紹介しますね',
    # 丁寧形（コンシェルジュモード対応）
    'お探しいたしますね', 'お調べいたしますね', '探してまいりますね',
    'ご紹介いたしますね',
    # 部分一致用（「お探ししますね」「お探しいたします」等を幅広くカバー）
    'お探しします', 'お調べします', 'お探しいたします', 'お調べいたします',
    '探してみます', 'ご紹介します', 'ご紹介いたします',
    # その他バリエーション
    'お店を探し', 'お店をお探し', '検索しますね', '検索いたしますね',
]


def should_trigger_shop_search(ai_text: str) -> bool:
    """AI発話からショップ検索トリガーを検知"""
    if any(kw in ai_text for kw in SHOP_TRIGGER_KEYWORDS):
        logger.info(f"[ShopTrigger] キーワード検知: '{ai_text[:50]}'")
        return True
    return False


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
                 system_prompt: str, socketio, client_sid: str):
        self.session_id = session_id
        self.mode = mode
        self.language = language
        self.system_prompt = system_prompt  # 外部から受け取る（将来GCS移行対応）
        self.socketio = socketio
        self.client_sid = client_sid

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

        # v3: 短期記憶（コンシェルジュモード用）
        self.short_term_memory = {
            'area': None,
            'purpose': None,
            'cuisine': None,
            'atmosphere': None,
            'party_size': None,
            'budget': None,
            'date': None,
        }
        self.hearing_step = 1  # 1〜5（concierge_ja.txtのステップに対応）

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
                            # 再接続時（v3: 履歴再送 → トリガーの2段階）
                            self._is_initial_greeting_phase = False
                            self.socketio.emit('live_reconnecting', {},
                                               room=self.client_sid)

                            # 1. 会話履歴turnsを再送（turn_complete=False）
                            await self._send_history_on_reconnect(session)

                            # 2. トリガーメッセージ（turn_complete=True）
                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[types.Part(text="続きをお願いします")]
                                ),
                                turn_complete=True
                            )
                            logger.info("[LiveAPI] 再接続: 履歴再送 + トリガー送信")
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
                        # 致命的エラー: ブラウザに通知してREST APIフォールバック
                        logger.error(f"[LiveAPI] 致命的エラー: {e}")
                        self.socketio.emit('live_fallback', {
                            'reason': str(e)
                        }, room=self.client_sid)
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
                        audio={"data": audio_data, "mime_type": "audio/pcm"}
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

                # 1. tool_call（現在は無効化だが将来用）
                if hasattr(response, 'tool_call') and response.tool_call:
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

            # v3: 短期記憶を更新（コンシェルジュモードのみ）
            if self.mode == 'concierge':
                self._update_short_term_memory(user_text, ai_text)

            # ショップ検索トリガー検知（仕様書02 セクション5.2-5.4）
            if should_trigger_shop_search(ai_text):
                # ユーザーの要望を会話履歴全体から構築
                user_request = self._build_search_request(user_text)
                logger.info(f"[LiveAPI] ショップ検索トリガー検知: '{ai_text}' → ユーザー要望: '{user_request}'")
                self.socketio.emit('shop_search_trigger', {
                    'user_request': user_request,
                    'session_id': self.session_id,
                    'language': self.language,
                    'mode': self.mode
                }, room=self.client_sid)
            else:
                logger.debug(f"[LiveAPI] トリガー未検知: '{ai_text[:50]}'")

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

    def _build_search_request(self, current_user_text: str) -> str:
        """会話履歴からショップ検索用のリクエストテキストを構築"""
        # v3: コンシェルジュモードで短期記憶があれば構造化された検索リクエストを作成
        if self.mode == 'concierge' and any(self.short_term_memory.values()):
            labels = {
                'area': 'エリア', 'purpose': '利用目的', 'cuisine': 'ジャンル',
                'atmosphere': '雰囲気', 'party_size': '人数', 'budget': '予算',
                'date': '日時',
            }
            conditions = []
            for key, label in labels.items():
                value = self.short_term_memory[key]
                if value:
                    conditions.append(f"{label}: {value}")
            request = "以下の条件でお店を探してください。\n" + "\n".join(conditions)
            logger.info(f"[LiveAPI] 構造化検索リクエスト: {request}")
            return request

        # フォールバック: 会話履歴からユーザー発言を収集
        if current_user_text:
            context_parts = [current_user_text]
            for h in reversed(self.conversation_history[:-1]):
                if h['role'] == 'user' and h['text'] != current_user_text:
                    context_parts.insert(0, h['text'])
                    if len(context_parts) >= 3:
                        break
            return '。'.join(context_parts)

        user_texts = []
        for h in reversed(self.conversation_history):
            if h['role'] == 'user':
                user_texts.insert(0, h['text'])
                if len(user_texts) >= 3:
                    break
        return '。'.join(user_texts) if user_texts else "おすすめのお店を探してください"

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

    # ============================================================
    # v3: 短期記憶（コンシェルジュモード用）
    # ============================================================

    def _update_short_term_memory(self, user_text: str, ai_text: str):
        """
        ターン完了時にユーザー発言から条件をキーワードベースで抽出。
        LLM呼び出しは行わない（レイテンシ回避）。
        拾えない条件は会話履歴の再送で補完される。
        """
        if not user_text:
            return

        # ---- エリア検出 ----
        area_keywords = [
            '六本木', '渋谷', '新宿', '銀座', '恵比寿', '池袋',
            '表参道', '赤坂', '麻布', '品川', '東京駅', '丸の内',
            '目黒', '中目黒', '代官山', '自由が丘', '吉祥寺',
            '横浜', '新橋', '浜松町', '五反田', '大崎', '田町',
            '上野', '秋葉原', '神田', '日本橋', '八重洲',
            '大阪', '梅田', '難波', '心斎橋', '京都', '神戸',
            '名古屋', '栄', '福岡', '天神', '博多', '札幌',
        ]
        for area in area_keywords:
            if area in user_text:
                self.short_term_memory['area'] = area
                break

        # ---- 利用目的検出 ----
        purpose_map = {
            '接待': '接待', 'デート': 'デート', '女子会': '女子会',
            '忘年会': '忘年会', '新年会': '新年会', '歓迎会': '歓迎会',
            '送別会': '送別会', '家族': '家族利用', '記念日': '記念日',
            '誕生日': '誕生日', '会食': '会食', '飲み会': '飲み会',
            '合コン': '合コン', '同窓会': '同窓会', 'ランチ': 'ランチ',
            '食事会': '食事会', '打ち上げ': '打ち上げ',
        }
        for kw, purpose in purpose_map.items():
            if kw in user_text:
                self.short_term_memory['purpose'] = purpose
                break

        # ---- 料理ジャンル検出 ----
        cuisine_map = {
            '和食': '和食', '洋食': '洋食', 'イタリアン': 'イタリアン',
            'フレンチ': 'フレンチ', '中華': '中華', '焼肉': '焼肉',
            '寿司': '寿司', '鮨': '寿司', '焼き鳥': '焼き鳥',
            'ラーメン': 'ラーメン', 'カフェ': 'カフェ',
            '居酒屋': '居酒屋', '韓国料理': '韓国料理',
            'タイ料理': 'タイ料理', 'インド料理': 'インド料理',
            'スペイン料理': 'スペイン料理', '鉄板焼': '鉄板焼',
            'しゃぶしゃぶ': 'しゃぶしゃぶ', 'すき焼き': 'すき焼き',
            '天ぷら': '天ぷら', 'うなぎ': 'うなぎ', 'そば': 'そば',
        }
        for kw, cuisine in cuisine_map.items():
            if kw in user_text:
                self.short_term_memory['cuisine'] = cuisine
                break

        # ---- 人数検出 ----
        party_match = re.search(r'(\d+)\s*[人名]', user_text)
        if party_match:
            self.short_term_memory['party_size'] = f"{party_match.group(1)}名"
        else:
            num_words = {
                '二人': '2名', 'ふたり': '2名', '2人': '2名',
                '三人': '3名', '四人': '4名', '五人': '5名',
            }
            for kw, size in num_words.items():
                if kw in user_text:
                    self.short_term_memory['party_size'] = size
                    break

        # ---- 雰囲気検出 ----
        atmo_map = {
            '落ち着い': '落ち着いた雰囲気', '静か': '静かな雰囲気',
            'カジュアル': 'カジュアル', '高級': '高級感',
            '個室': '個室希望', 'おしゃれ': 'おしゃれ',
            '賑やか': '賑やかな雰囲気', 'アットホーム': 'アットホーム',
            '隠れ家': '隠れ家的', 'モダン': 'モダン',
        }
        for kw, atmo in atmo_map.items():
            if kw in user_text:
                self.short_term_memory['atmosphere'] = atmo
                break

        # ---- 予算検出 ----
        budget_match = re.search(r'(\d[\d,]*)\s*円', user_text)
        if budget_match:
            self.short_term_memory['budget'] = budget_match.group(0)
        else:
            man_match = re.search(r'(\d+)\s*万', user_text)
            if man_match:
                self.short_term_memory['budget'] = f"{man_match.group(1)}万円程度"

        # ---- 日時検出 ----
        date_patterns = [
            '今日', '明日', '明後日', '今週', '来週', '再来週',
            '今月', '来月', '月曜', '火曜', '水曜', '木曜',
            '金曜', '土曜', '日曜', '週末', '祝日',
        ]
        for pattern in date_patterns:
            if pattern in user_text:
                self.short_term_memory['date'] = user_text
                break
        if not self.short_term_memory['date']:
            date_match = re.search(r'\d+[月/]\d+', user_text)
            if date_match:
                self.short_term_memory['date'] = date_match.group(0)

        # ---- ステップ更新 ----
        self._update_hearing_step()

        logger.info(f"[ShortTermMemory] step={self.hearing_step}, "
                    f"conditions={self._get_confirmed_conditions()}")

    def _update_hearing_step(self):
        """確定条件に基づいてヒアリングステップを更新"""
        m = self.short_term_memory

        if not m['area'] and not m['purpose']:
            self.hearing_step = 1
            return
        if not m['cuisine'] and not m['party_size']:
            self.hearing_step = 2
            return
        if not m['budget']:
            self.hearing_step = 3
            return
        if not m['date']:
            self.hearing_step = 4
            return
        self.hearing_step = 5

    def _get_confirmed_conditions(self) -> dict:
        """確定済み条件のみを返す"""
        return {k: v for k, v in self.short_term_memory.items() if v}

    def _get_context_summary(self) -> str:
        """
        再接続時のコンテキスト注入（v3全面改訂）

        コンシェルジュモード: 構造化された条件状態 + ステップ指示
        その他モード: 従来のチャットログ形式
        """
        parts = []

        # ===== コンシェルジュモード: 構造化された短期記憶 =====
        if self.mode == 'concierge' and any(self.short_term_memory.values()):
            condition_labels = {
                'area': 'エリア',
                'purpose': '利用目的',
                'cuisine': '料理ジャンル',
                'atmosphere': '雰囲気',
                'party_size': '人数',
                'budget': '予算',
                'date': '日時',
            }

            confirmed = []
            unconfirmed = []

            for key, label in condition_labels.items():
                value = self.short_term_memory[key]
                if value:
                    confirmed.append(f"  - {label}: {value}")
                else:
                    unconfirmed.append(f"  - {label}: 未確認")

            parts.append("【ユーザーの確定済み条件（短期記憶）】")
            parts.append("以下は会話で既に確認済み。再度質問してはならない。")
            parts.extend(confirmed)

            if unconfirmed:
                parts.append("")
                parts.append("【未確認の条件】")
                parts.extend(unconfirmed)

            step_instructions = {
                1: "まずエリアと利用目的を質問してください。",
                2: "料理ジャンル・雰囲気・人数を質問してください。",
                3: "予算を質問してください。",
                4: "日時を質問してください。なお日時はユーザーが言わなければ省略可。",
                5: "条件は十分です。「お探ししますね」と言って検索を開始してください。",
            }
            parts.append(f"\n【次のステップ】{step_instructions.get(self.hearing_step, '')}")

        # ===== 直前のAI質問の強調（全モード共通） =====
        if self.conversation_history:
            last_ai = None
            for h in reversed(self.conversation_history):
                if h['role'] == 'ai':
                    last_ai = h['text']
                    break

            if last_ai and ('?' in last_ai or '？' in last_ai
                            or 'ですか' in last_ai or 'ますか' in last_ai):
                parts.append(f"\n【直前のAIの質問（回答を待っています）】\n{last_ai[:200]}")

        return "\n".join(parts)

    async def _send_history_on_reconnect(self, session):
        """
        再接続時に会話履歴をsend_client_content()で再送する。
        REST版では毎回全会話履歴をAPI引数として送信していた。
        LiveAPIのsend_client_content() turnsで同等のことを行う。
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
