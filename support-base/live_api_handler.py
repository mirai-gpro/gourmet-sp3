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
import pathlib
import struct
import httpx
from scipy.signal import resample_poly
import numpy as np
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# A2E (Audio2Expression) サービス設定
A2E_SERVICE_URL = os.getenv("A2E_SERVICE_URL", "https://audio2exp-service-417509577941.us-central1.run.app")
# プロトコルが省略された場合に自動補完
if A2E_SERVICE_URL and not A2E_SERVICE_URL.startswith("http"):
    A2E_SERVICE_URL = f"https://{A2E_SERVICE_URL}"
A2E_MIN_BUFFER_BYTES = 4800      # 最低バッファサイズ（24kHz 16bit mono × 0.1秒 = 4800bytes）
A2E_FIRST_FLUSH_BYTES = 4800     # 初回フラッシュ閾値（0.1秒分 = 4800bytes）遅延最小化
A2E_AUTO_FLUSH_BYTES = 240000    # 2回目以降フラッシュ閾値（5秒分 = 240000bytes）品質優先
A2E_EXPRESSION_FPS = 30

# stt_stream.py から転記（変更禁止）
LIVE_API_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
MAX_AI_CHARS_BEFORE_RECONNECT = 800
LONG_SPEECH_THRESHOLD = 500

# ============================================================
# プロンプト定義
# GCS/ローカルのプロンプトファイルから読み込む
# - support_system_ja.txt : グルメ(チャット)モード
# - concierge_ja.txt      : コンシェルジュモード
# ============================================================

_PROMPT_DIR = pathlib.Path(__file__).resolve().parent / "prompts"


def _load_prompt_file(filename: str) -> str:
    """ローカルのプロンプトファイルを読み込む"""
    filepath = _PROMPT_DIR / filename
    try:
        return filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error(f"[Prompt] ファイルが見つかりません: {filepath}")
        return ""


def build_system_instruction(mode: str, user_profile: dict = None) -> str:
    """モードに応じたシステムインストラクションを組み立てる

    プロンプト本体は prompts/ ディレクトリのテキストファイルから読み込む。
    コンシェルジュモードの {user_context} のみ動的に差し替える。
    LiveAPI用にsearch_shopsツール使用指示を追記する。

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
        template = _load_prompt_file("concierge_ja.txt")
        user_context = _build_concierge_user_context(user_profile)
        base_prompt = template.replace("{user_context}", user_context)
    else:
        base_prompt = _load_prompt_file("support_system_ja.txt")

    # LiveAPI専用: 「喋る前にまずfunction call」を強制
    # 音声生成とfunction call準備が同時に走ると1008で切断されるため
    base_prompt += SEARCH_SHOPS_INSTRUCTION
    return base_prompt


# LiveAPI専用のsearch_shopsツール使用指示
# （テキストチャットのプロンプトには含めない）
# Gemini LiveAPIでは音声応答モードだとモデルが「喋って満足」して
# function callを発火せずにturn_completeしてしまう問題がある。
# 対策: 「喋るより先にツールを呼べ」と明示的に指示する。
SEARCH_SHOPS_INSTRUCTION = """

---

## 【最重要】ショップ検索ツール（search_shops）の使い方

お店を検索する際は、必ず search_shops ツールをfunction callingで呼び出すこと。

### 行動の優先順位（厳守）
1. search_shops の実行が最優先。返事は二の次。
2. 店探しを依頼されたら、挨拶は最小限（「わかりました」程度）にし、直ちに search_shops を実行すること。
3. 検索結果が出る前に「今調べています」などと長々と喋ってターンを終了してはいけない。
4. 何も喋らずに search_shops を呼び出しても良い（無言実行OK）。
5. 音声で返答を生成しきる前に、必ず search_shops を呼び出すこと。

### 絶対に守るルール
- 「お調べしますね」等と喋るだけでターンを終了することは禁止
- search_shops を呼ばずにターンを終了することは禁止
- テキストや音声だけで応答して検索を省略することは絶対に禁止

### 呼び出し方法
- search_shops(user_request="恵比寿 イタリアン") のように、要望をキーワードで要約して渡す
- ユーザーの音声が曖昧な場合は正しく補完する（例: 「エピス」→「恵比寿」）
- ユーザーが条件を1つでも言ったら、即座に search_shops を呼び出す
- 情報が不足していても呼び出してよい。エリアだけ、ジャンルだけでもOK。
"""


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

        # ターン内でtool_callを受信したかの追跡フラグ
        self._tool_call_received_in_turn = False

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

        # ★ A2Eバッファリング機構（仕様書08 セクション3.1）
        self._a2e_audio_buffer = bytearray()       # PCMチャンク蓄積用
        self._a2e_transcript_buffer = ""            # 句読点検出用
        self._a2e_chunk_index = 0                   # expression同期用チャンク識別子
        self._a2e_http_client = httpx.AsyncClient(timeout=10.0)

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
                # Native Audioモデルでは音声データの間にtool_callが
                # 遅延して届くことがある。確実にキャッチする。
                if response.tool_call:
                    logger.info("[LiveAPI] !!! Function Call 発火 !!!")
                    self._tool_call_received_in_turn = True
                    await self._handle_tool_call(response.tool_call, session)
                    continue

                if response.server_content:
                    sc = response.server_content

                    # 2. ターン完了
                    if hasattr(sc, 'turn_complete') and sc.turn_complete:
                        # ★ A2E: 残存バッファを強制フラッシュ（最終チャンク）
                        await self._flush_a2e_buffer(force=True, is_final=True)
                        self._a2e_chunk_index = 0  # 次ターン用にリセット
                        self._process_turn_complete()
                        self.socketio.emit('turn_complete', {},
                                           room=self.client_sid)

                        # 初期あいさつフェーズ終了（仕様書02 セクション4.5.5）
                        if self._is_initial_greeting_phase:
                            self._is_initial_greeting_phase = False
                            self.socketio.emit('greeting_done', {},
                                               room=self.client_sid)
                            logger.info("[LiveAPI] greeting_done送信")
                        else:
                            # ターン完了したがtool_callが来なかった場合をログ記録
                            # （Native Audioモデルではtool_callが遅延する場合がある）
                            logger.info(f"[LiveAPI] ターン完了（tool_call無し）: AI='{self.ai_transcript_buffer[:50]}'")
                            self._tool_call_received_in_turn = False

                    # 3. 割り込み検知
                    if hasattr(sc, 'interrupted') and sc.interrupted:
                        # ★ A2E: 残存バッファを強制フラッシュ（最終チャンク）
                        await self._flush_a2e_buffer(force=True, is_final=True)
                        self._a2e_chunk_index = 0  # 次ターン用にリセット
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
                            # ★ A2E: 句読点検出でバッファフラッシュ（仕様書08 セクション3.2）
                            self._on_output_transcription(text)

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
                                    # ★ A2E: PCMバッファ蓄積（仕様書08 セクション3.2）
                                    self._buffer_for_a2e(part.inline_data.data)

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

                # function responseを返す
                await session.send_tool_response(
                    function_responses=[types.FunctionResponse(
                        name=fc.name,
                        id=fc.id,
                        response={"result": "検索結果をユーザーに表示しました"}
                    )]
                )
            else:
                logger.warning(f"[LiveAPI] 未知のfunction call: {fc.name}")

    async def _handle_shop_search(self, user_request: str):
        """
        ショップ検索を実行し、結果をブラウザに送信する（v5 §5.5）

        【設計】
        - shop_search_callback を呼び出してショップデータを取得
        - コールバックは SupportAssistant.process_user_message() を内部的に呼び出す
        - 取得したデータはshop_search_resultイベントでブラウザに送信
        - エラー時・空結果時もshop_search_resultを送信してフロントの待機を解除
        """
        if not self._shop_search_callback:
            logger.error("[ShopSearch] shop_search_callback が未設定")
            self.socketio.emit('shop_search_result', {
                'shops': [], 'response': '',
            }, room=self.client_sid)
            return

        try:
            # ★ 同期ブロッキング呼び出しをイベントループ外で実行
            loop = asyncio.get_event_loop()
            shop_data = await loop.run_in_executor(
                None,
                self._shop_search_callback,
                user_request, self.language, self.mode
            )

            if not shop_data or not shop_data.get('shops'):
                logger.info("[ShopSearch] ショップ見つからず")
                self.socketio.emit('shop_search_result', {
                    'shops': [], 'response': shop_data.get('response', '') if shop_data else '',
                }, room=self.client_sid)
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
            self.socketio.emit('shop_search_result', {
                'shops': [], 'response': '',
            }, room=self.client_sid)

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
                    # ★ A2E: 残存バッファを強制フラッシュ（最終チャンク）
                    await self._flush_a2e_buffer(force=True, is_final=True)
                    self._a2e_chunk_index = 0  # 次ターン用にリセット
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

                # 出力トランスクリプション（ショップ説明 - A2E句読点検出）
                if (hasattr(sc, 'output_transcription')
                        and sc.output_transcription):
                    text_tr = sc.output_transcription.text
                    if text_tr:
                        self._on_output_transcription(text_tr)

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
                                # ★ A2E: ショップ説明でも蓄積（仕様書08 セクション3.5）
                                self._buffer_for_a2e(part.inline_data.data)

    # ============================================================
    # A2E バッファリングメソッド（仕様書08 セクション3.3）
    # ============================================================

    def _buffer_for_a2e(self, pcm_data: bytes):
        """PCMをバッファに追加。閾値超過で自動フラッシュ（句読点待ち不要）"""
        self._a2e_audio_buffer.extend(pcm_data)
        # ★ 初回は即フラッシュ（遅延最小化）、2回目以降は品質優先で大きく溜める
        threshold = A2E_FIRST_FLUSH_BYTES if self._a2e_chunk_index == 0 else A2E_AUTO_FLUSH_BYTES
        if len(self._a2e_audio_buffer) >= threshold:
            asyncio.ensure_future(self._flush_a2e_buffer(force=True))

    def _on_output_transcription(self, text: str):
        """句読点検出でフラッシュ判定（仕様書08 セクション3.3）"""
        self._a2e_transcript_buffer += text
        # 句読点（。？！）を検出したらフラッシュ
        flush_triggers = ['。', '？', '！', '?', '!']
        if any(t in text for t in flush_triggers):
            asyncio.ensure_future(self._flush_a2e_buffer(force=False))
            self._a2e_transcript_buffer = ""

    async def _flush_a2e_buffer(self, force: bool = False, is_final: bool = False):
        """最低バイト数チェック後、非同期でA2E送信（仕様書08 セクション3.3）"""
        if len(self._a2e_audio_buffer) == 0:
            return

        # force=Falseの場合、最低バッファサイズをチェック
        if not force and len(self._a2e_audio_buffer) < A2E_MIN_BUFFER_BYTES:
            return

        # バッファを取得してクリア
        pcm_data = bytes(self._a2e_audio_buffer)
        self._a2e_audio_buffer = bytearray()
        chunk_index = self._a2e_chunk_index
        self._a2e_chunk_index += 1

        # 非同期でA2Eに送信
        try:
            await self._send_to_a2e(pcm_data, chunk_index, is_final=is_final)
        except Exception as e:
            logger.error(f"[A2E] フラッシュエラー: {e}")

    async def _send_to_a2e(self, pcm_data: bytes, chunk_index: int, is_final: bool = False):
        """リサンプリング（24→16kHz）後、A2Eサービスに送信（仕様書08 セクション3.4）

        a2e_engine.py _decode_audio の "pcm" フォーマット:
          samples = np.frombuffer(audio_bytes, dtype=np.int16)
        → raw int16 PCMバイトをbase64で送信
        """
        try:
            # PCM 24kHz 16bit mono → numpy int16
            int16_array = np.frombuffer(pcm_data, dtype=np.int16)

            # 24kHz → 16kHz リサンプリング（SciPy resample_poly）
            resampled = resample_poly(int16_array.astype(np.float32), up=2, down=3)

            # int16に戻す
            int16_resampled = np.clip(resampled, -32768, 32767).astype(np.int16)

            # ★ A2Eへ送る音声データの品質ログ
            duration_sec = len(int16_resampled) / 16000
            rms = np.sqrt(np.mean(int16_resampled.astype(np.float32) ** 2))
            peak = np.max(np.abs(int16_resampled))
            logger.info(
                f"[A2E Input] chunk {chunk_index}: "
                f"samples={len(int16_resampled)}, duration={duration_sec:.3f}s, "
                f"RMS={rms:.1f}, peak={peak}, "
                f"is_start={chunk_index == 0}, is_final={is_final}"
            )

            # raw int16 PCMをbase64エンコードしてJSON送信
            audio_b64 = base64.b64encode(int16_resampled.tobytes()).decode('utf-8')

            response = await self._a2e_http_client.post(
                f"{A2E_SERVICE_URL}/api/audio2expression",
                json={
                    "audio_base64": audio_b64,
                    "session_id": self.session_id,
                    "audio_format": "pcm",
                    "is_start": chunk_index == 0,
                    "is_final": is_final,
                },
                timeout=10.0
            )

            if response.status_code == 200:
                result = response.json()
                frames = result.get('frames', [])
                names = result.get('names', [])
                frame_rate = result.get('frame_rate', A2E_EXPRESSION_FPS)

                # ★ A2Eレスポンスの詳細ログ
                logger.info(
                    f"[A2E Response] chunk {chunk_index}: "
                    f"keys={list(result.keys())}, "
                    f"names_count={len(names)}, frames_count={len(frames)}, "
                    f"frame_rate={frame_rate}"
                )

                if frames:
                    # A2Eレスポンス frames: [{weights: [float]}] →
                    # フロントエンド expressions: [[float]] に変換
                    expressions = [f['weights'] if isinstance(f, dict) else f for f in frames]

                    # ★ 先頭フレームの52パラメータを詳細出力
                    first_expr = expressions[0] if expressions else []
                    if first_expr:
                        non_zero = [(names[i] if i < len(names) else f'[{i}]', v)
                                    for i, v in enumerate(first_expr) if abs(v) > 0.001]
                        non_zero_str = ', '.join(f'{n}={v:.4f}' for n, v in non_zero[:15])
                        expr_arr = np.array(first_expr)
                        logger.info(
                            f"[A2E Params] chunk {chunk_index} firstFrame: "
                            f"dims={len(first_expr)}, nonZero={len(non_zero)}/52, "
                            f"min={expr_arr.min():.4f}, max={expr_arr.max():.4f}, "
                            f"top: {{{non_zero_str}}}"
                        )

                    self.socketio.emit('live_expression', {
                        'expressions': expressions,
                        'expression_names': names,
                        'frame_rate': frame_rate,
                        'chunk_index': chunk_index,
                    }, room=self.client_sid)
                    logger.info(f"[A2E] chunk {chunk_index}: {len(frames)} frames送信")
                else:
                    logger.warning(f"[A2E] chunk {chunk_index}: frames空! response={result}")
            else:
                logger.warning(f"[A2E] サービスエラー: {response.status_code}, body={response.text[:200]}")

        except Exception as e:
            logger.error(f"[A2E] 送信エラー: {e}")

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
