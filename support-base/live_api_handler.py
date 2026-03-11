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

# モデル名
LIVE_API_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

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

    - Live API接続設定: types.LiveConnectConfig による型安全な構成
    - 音声送受信: 全二重（VADに委譲）
    - コンテキスト管理: sliding_window による自動管理（手動再接続不要）
    - エラーハンドリング: 接続エラー時のみリトライ
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

        # 状態管理
        self.user_transcript_buffer = ""
        self.ai_transcript_buffer = ""
        self.conversation_history = []

        # 非同期キュー
        self.audio_queue_to_gemini = None
        self.is_running = False

    def _build_config(self):
        """
        Live API接続設定を構築（types.LiveConnectConfig 使用）

        Q1: 型付きオブジェクトでタイポ防止・IDE補完・将来的な変更への耐性確保
        Q7: sliding_window により手動再接続は不要
        Q9: VADに委譲し全二重対応（半二重制御不要）
        """
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part(text=self.system_prompt)]
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Aoede"
                    )
                ),
                language_code=self._get_speech_language_code(),
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity="START_SENSITIVITY_HIGH",
                    end_of_speech_sensitivity="END_SENSITIVITY_HIGH",
                    prefix_padding_ms=100,
                    silence_duration_ms=500,
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(
                    target_tokens=32000,
                )
            ),
        )
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

    async def run(self):
        """
        メインループ

        Q7: sliding_window が自動でコンテキスト管理するため、
        手動の800文字再接続は不要。単一セッションを維持する。
        エラー時のみ再接続を試みる。
        """
        self.audio_queue_to_gemini = asyncio.Queue(maxsize=5)
        self.is_running = True
        max_retries = 3
        retry_count = 0

        try:
            while self.is_running and retry_count <= max_retries:
                config = self._build_config()

                try:
                    async with self.client.aio.live.connect(
                        model=LIVE_API_MODEL,
                        config=config
                    ) as session:
                        retry_count = 0  # 接続成功でリセット

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

                        await self._session_loop(session)
                        break  # 正常終了

                except Exception as e:
                    error_msg = str(e).lower()
                    if any(kw in error_msg for kw in
                           ["1011", "internal error", "disconnected",
                            "closed", "websocket"]):
                        retry_count += 1
                        wait_sec = min(3 * retry_count, 10)
                        logger.warning(f"[LiveAPI] 接続エラー({retry_count}/{max_retries})、{wait_sec}秒後に再接続: {e}")
                        await asyncio.sleep(wait_sec)
                        continue
                    else:
                        logger.error(f"[LiveAPI] 致命的エラー: {e}")
                        self.socketio.emit('live_fallback', {
                            'reason': str(e)
                        }, room=self.client_sid)
                        break

            if retry_count > max_retries:
                logger.error("[LiveAPI] 最大再接続回数超過")
                self.socketio.emit('live_fallback', {
                    'reason': 'Maximum reconnection attempts exceeded'
                }, room=self.client_sid)

        except asyncio.CancelledError:
            pass
        finally:
            self.is_running = False
            logger.info(f"[LiveAPI] セッション終了: {self.session_id}")

    async def _session_loop(self, session):
        """
        セッション内ループ: 送信・受信タスクを並行実行
        Q9: 全二重 - AIが話している間もユーザー音声を送信し続ける
        """
        async def send_audio():
            """ブラウザからの音声をLiveAPIに転送（常時送信）"""
            while self.is_running:
                try:
                    audio_data = await asyncio.wait_for(
                        self.audio_queue_to_gemini.get(),
                        timeout=0.1
                    )
                    await session.send_realtime_input(
                        audio=types.Blob(data=audio_data, mime_type="audio/pcm")
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if not self.is_running:
                        return
                    logger.error(f"[LiveAPI] 送信エラー: {e}")
                    raise

        async def receive():
            """LiveAPIからの応答を受信してブラウザに転送"""
            try:
                await self._receive_and_forward(session)
            except Exception as e:
                if not self.is_running:
                    return
                logger.error(f"[LiveAPI] 受信エラー: {e}")
                raise

        # キューをクリア
        while not self.audio_queue_to_gemini.empty():
            try:
                self.audio_queue_to_gemini.get_nowait()
            except asyncio.QueueEmpty:
                break

        async with asyncio.TaskGroup() as tg:
            tg.create_task(send_audio())
            tg.create_task(receive())

    async def _receive_and_forward(self, session):
        """
        LiveAPIレスポンスを受信してSocket.IOでブラウザに転送

        Q6: turn_complete と interrupted は排他的
        - turn_complete: AIが最後まで発話した場合
        - interrupted: ユーザー割り込みでAIが生成停止した場合
        """
        while self.is_running:
            turn = session.receive()
            async for response in turn:
                if not self.is_running:
                    return

                # 1. tool_call（現在は無効化だが将来用）
                if hasattr(response, 'tool_call') and response.tool_call:
                    continue

                if response.server_content:
                    sc = response.server_content

                    # 2. ターン完了（Q6: interruptedが発生した場合はturn_completeにならない）
                    if hasattr(sc, 'turn_complete') and sc.turn_complete:
                        self._process_turn_complete()
                        self.socketio.emit('turn_complete', {},
                                           room=self.client_sid)

                        # 初期あいさつフェーズ終了
                        if self._is_initial_greeting_phase:
                            self._is_initial_greeting_phase = False

                    # 3. 割り込み検知（Q6: turn_completeとは排他的）
                    if hasattr(sc, 'interrupted') and sc.interrupted:
                        logger.info("[LiveAPI] ユーザー割り込み検知")
                        # 割り込まれたAI発話はクリア
                        self.ai_transcript_buffer = ""
                        self.socketio.emit('interrupted', {},
                                           room=self.client_sid)
                        continue

                    # 4. 入力トランスクリプション
                    #    Q4: VAD区切り or ターン完了時に一括で届く
                    #    初期あいさつフェーズのinput_transcriptionは転送しない
                    if (hasattr(sc, 'input_transcription')
                            and sc.input_transcription):
                        text = sc.input_transcription.text
                        if text and not self._is_initial_greeting_phase:
                            self.user_transcript_buffer += text
                            self.socketio.emit('user_transcript',
                                               {'text': text},
                                               room=self.client_sid)

                    # 5. 出力トランスクリプション
                    #    Q5: 音声とほぼ同時にストリーミングで小刻みに届く
                    if (hasattr(sc, 'output_transcription')
                            and sc.output_transcription):
                        text = sc.output_transcription.text
                        if text:
                            self.ai_transcript_buffer += text
                            self.socketio.emit('ai_transcript',
                                               {'text': text},
                                               room=self.client_sid)

                    # 6. 音声データ（Q3: 24kHz/16bit/LE/Mono）
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

        Q7: sliding_window が自動でコンテキスト管理するため、
        手動の文字数カウント・再接続判定は不要。
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

            # ショップ検索トリガー検知
            if should_trigger_shop_search(ai_text):
                user_request = self._build_search_request(user_text)
                logger.info(f"[LiveAPI] ショップ検索トリガー検知: '{ai_text}' → ユーザー要望: '{user_request}'")
                self.socketio.emit('shop_search_trigger', {
                    'user_request': user_request,
                    'session_id': self.session_id,
                    'language': self.language,
                    'mode': self.mode
                }, room=self.client_sid)

            self.ai_transcript_buffer = ""

    def _build_search_request(self, current_user_text: str) -> str:
        """会話履歴からショップ検索用のリクエストテキストを構築"""
        # 現在のターンのユーザー発言があればそれを優先
        if current_user_text:
            # 直近の会話コンテキストも付加（条件が分散している場合）
            context_parts = [current_user_text]
            for h in reversed(self.conversation_history[:-1]):  # 最後（今追加した分）は除く
                if h['role'] == 'user' and h['text'] != current_user_text:
                    context_parts.insert(0, h['text'])
                    if len(context_parts) >= 3:
                        break
            return '。'.join(context_parts)

        # 現在のターンにユーザー発言がない場合、履歴から収集
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

    def _add_to_history(self, role: str, text: str):
        """会話履歴に追加"""
        self.conversation_history.append({"role": role, "text": text})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

