# Gemini LiveAPI 移植設計書（gourmet-sp3）

> **作成日**: 2026-03-10
> **前提文書**: `docs/01_stt_stream_detailed_spec.md`（必ず先に読むこと）
> **目的**: 現行REST APIベースのgourmet-sp3に、Gemini LiveAPIによるリアルタイム音声対話を追加する設計

---

## 0. Claudeへの厳守事項（最重要）

### 過去3回の失敗パターン

```
1) 仕様書作成
2) 仕様通りに実装（ここまではOK）
3) 不備発見 → 修正開始
4) 修正中に仕様書を読まずに推測で変更を入れる ← ★ここが問題
5) 推測が間違っていて不具合増加
6) 「仕様書を確認しろ」→「確認しました」と嘘をつく ← ★ここも問題
7) 更に推測で修正 → ドツボ
```

### 防止ルール

1. **修正する前に、必ず `01_stt_stream_detailed_spec.md` の該当セクションを `Read` ツールで読む**
2. **「確認しました」と報告する場合、確認したファイルパスと行番号を明記する**
3. **仕様書に記載がない機能を追加しない**
4. **推測で API の引数やメソッド名を変えない**
5. **困ったらユーザーに聞く。推測で進めない**

---

## 1. 移植のスコープ

### 1.1 やること

| # | 内容 | 優先度 |
|---|---|---|
| 1 | バックエンド: LiveAPI WebSocketプロキシの新設 | 必須 |
| 2 | フロントエンド: AudioStreamManager の改修 | 必須 |
| 3 | LiveAPI → REST API フォールバック機構 | 必須 |
| 4 | セッション再接続メカニズム | 必須 |
| 5 | トランスクリプション（文字起こし）表示 | 推奨 |
| 6 | Function Calling（ショップ提案トリガー） | 将来 |

### 1.2 やらないこと

- 現行REST APIエンドポイント（`/api/chat`, `/api/tts/synthesize`, `/api/stt/transcribe`）の廃止
  → LiveAPIが不安定な間は、REST APIをフォールバックとして維持
- PyAudio関連の移植（ブラウザにはPyAudioがない）
- 効果音生成（ブラウザ側で別途実装するなら可）

---

## 2. 全体アーキテクチャ

### 2.1 現行アーキテクチャ（REST API方式）

```
ブラウザ
├── マイク → AudioWorklet → PCM 16kHz → Socket.IO → サーバー
│                                                      ├── Google STT（音声→テキスト）
│                                                      ├── Gemini REST API（テキスト→テキスト）
│                                                      └── Google TTS（テキスト→音声）
│                                                             ↓
└── スピーカー ← HTMLAudioElement ← MP3 base64 ← HTTP Response
```

**問題点**: 音声→テキスト→AI→テキスト→音声 の変換が多く、遅延が大きい

### 2.2 新アーキテクチャ（LiveAPI方式）

```
ブラウザ
├── マイク → AudioWorklet → PCM 16kHz → WebSocket → サーバー
│                                                      ├── Gemini LiveAPI（音声→音声）
│                                                      │   ├── 音声応答（PCM 24kHz）
│                                                      │   ├── input_transcription（ユーザー文字起こし）
│                                                      │   └── output_transcription（AI文字起こし）
│                                                      │
│                                                      └── [フォールバック] REST API + TTS
│                                                             ↓
└── スピーカー ← Web Audio API ← PCM 24kHz ← WebSocket
    チャット欄 ← transcription テキスト ← WebSocket
```

**利点**: 音声→音声の直接変換で低遅延

### 2.3 データフロー詳細

```
[ブラウザ → サーバー]
1. WebSocket: {type: 'audio', data: base64_pcm_16khz}
   → サーバーが base64デコード
   → session.send_realtime_input(audio={"data": pcm_bytes, "mime_type": "audio/pcm"})

[サーバー → ブラウザ]
2. LiveAPIレスポンスを分類して転送:
   a. 音声データ:
      response.server_content.model_turn.parts[].inline_data.data
      → WebSocket: {type: 'audio', data: base64_pcm_24khz}

   b. ユーザー文字起こし:
      response.server_content.input_transcription.text
      → WebSocket: {type: 'user_transcript', text: '...'}

   c. AI文字起こし:
      response.server_content.output_transcription.text
      → WebSocket: {type: 'ai_transcript', text: '...'}

   d. ターン完了:
      response.server_content.turn_complete == True
      → WebSocket: {type: 'turn_complete'}

   e. 割り込み:
      response.server_content.interrupted == True
      → WebSocket: {type: 'interrupted'}
```

---

## 3. バックエンド設計

### 3.1 新規ファイル: `live_api_handler.py`

stt_stream.py の `GeminiLiveApp` をWeb向けに変換したクラス。

```python
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
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# stt_stream.py から転記（変更禁止）
LIVE_API_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
MAX_AI_CHARS_BEFORE_RECONNECT = 800
LONG_SPEECH_THRESHOLD = 500
```

### 3.2 LiveAPISession クラス設計

```python
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
    # 【設計意図】
    # LiveAPI は response_modalities: ["AUDIO"] で動作するため、
    # AI側から先に話し始めることができない（ユーザー入力が必要）。
    # そのため、セッション開始時にサーバー側からダミーの
    # ユーザーメッセージを send_client_content() で送信し、
    # AIの初期あいさつ音声応答をトリガーする。
    #
    # 【根拠】stt_stream.py:766-776 の再接続時パターンと同じ手法:
    #   await session.send_client_content(
    #       turns=types.Content(role="user", parts=[types.Part(text="...")]),
    #       turn_complete=True
    #   )
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
        self.socketio = socketio         # Socket.IOインスタンス
        self.client_sid = client_sid     # ブラウザ側のSocket.IO ID

        # 初期あいさつフェーズ（ダミーメッセージのinput_transcriptionを非表示）
        self._is_initial_greeting_phase = True

        # Gemini APIクライアント
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        # 状態管理（stt_stream.py:387-394 から転記）
        self.user_transcript_buffer = ""
        self.ai_transcript_buffer = ""
        self.conversation_history = []
        self.ai_char_count = 0
        self.needs_reconnect = False
        self.session_count = 0

        # 非同期キュー
        self.audio_queue_to_gemini = None   # ブラウザ→Gemini
        self.is_running = False

    def _build_config(self, with_context=None):
        """
        Live API接続設定を構築

        【厳守】仕様書 セクション5.2 のconfig辞書をそのまま使う。
        値を変えない。キーを追加・削除しない。
        """
        instruction = self.system_prompt

        if with_context:
            # 仕様書 セクション8.3 の再接続時コンテキスト注入
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
```

### 3.3 音声送受信の実装方針

```python
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
                    # 仕様書 セクション6.2: 接続メソッド
                    async with self.client.aio.live.connect(
                        model=LIVE_API_MODEL,
                        config=config
                    ) as session:

                        if self.session_count == 1:
                            # ★ 初回接続: ダミーメッセージで初期あいさつを発火
                            # LiveAPIは response_modalities: ["AUDIO"] のため
                            # AI側から先に話し始められない。
                            # ユーザーからのダミー問いかけを送信して
                            # AIの初期あいさつ音声応答をトリガーする。
                            # （stt_stream.py:766-776 の再接続パターンと同じ手法）
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
                            # 再接続時: 仕様書 セクション6.3
                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[types.Part(text="続きをお願いします")]
                                ),
                                turn_complete=True
                            )

                        await self._session_loop(session)

                        if not self.needs_reconnect:
                            break

                except Exception as e:
                    # 仕様書 セクション9.1: 接続エラー処理
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
        （listen_audioとplay_audioはブラウザ側なので不要）
        """
        async def send_audio():
            """ブラウザからの音声をLiveAPIに転送"""
            while not self.needs_reconnect:
                try:
                    audio_data = await asyncio.wait_for(
                        self.audio_queue_to_gemini.get(),
                        timeout=0.1
                    )
                    # 仕様書 セクション7.2: send_realtime_input
                    await session.send_realtime_input(
                        audio={"data": audio_data, "mime_type": "audio/pcm"}
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self.needs_reconnect:
                        return
                    logger.error(f"[LiveAPI] 送信エラー: {e}")
                    self.needs_reconnect = True
                    return

        async def receive():
            """LiveAPIからの応答を受信してブラウザに転送"""
            try:
                await self._receive_and_forward(session)
            except Exception as e:
                if self.needs_reconnect:
                    return
                error_msg = str(e).lower()
                # 仕様書 セクション9.2
                if any(kw in error_msg for kw in
                    ["1011", "1008", "internal error",
                     "closed", "deadline", "policy"]):
                    self.needs_reconnect = True
                else:
                    raise

        # キューをクリア
        while not self.audio_queue_to_gemini.empty():
            try:
                self.audio_queue_to_gemini.get_nowait()
            except:
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
```

### 3.4 レスポンス受信・転送

```python
    async def _receive_and_forward(self, session):
        """
        LiveAPIレスポンスを受信してSocket.IOでブラウザに転送
        仕様書 セクション7.3 の receive_audio() を移植
        """
        while not self.needs_reconnect:
            turn = session.receive()
            async for response in turn:
                if self.needs_reconnect:
                    return

                # 1. tool_call（現在は無効化だが将来用）
                if hasattr(response, 'tool_call') and response.tool_call:
                    # 仕様書 セクション3: 現在は無効化
                    continue

                if response.server_content:
                    sc = response.server_content

                    # 2. ターン完了
                    if hasattr(sc, 'turn_complete') and sc.turn_complete:
                        self._process_turn_complete()
                        self.socketio.emit('turn_complete', {},
                                          room=self.client_sid)

                    # 3. 割り込み検知
                    if hasattr(sc, 'interrupted') and sc.interrupted:
                        self.ai_transcript_buffer = ""
                        self.socketio.emit('interrupted', {},
                                          room=self.client_sid)
                        continue

                    # 4. 入力トランスクリプション
                    if (hasattr(sc, 'input_transcription')
                            and sc.input_transcription):
                        text = sc.input_transcription.text
                        if text:
                            self.user_transcript_buffer += text
                            self.socketio.emit('user_transcript',
                                {'text': text}, room=self.client_sid)

                    # 5. 出力トランスクリプション
                    if (hasattr(sc, 'output_transcription')
                            and sc.output_transcription):
                        text = sc.output_transcription.text
                        if text:
                            self.ai_transcript_buffer += text
                            self.socketio.emit('ai_transcript',
                                {'text': text}, room=self.client_sid)

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
```

### 3.5 app_customer_support.py への統合

既存の Socket.IO イベントハンドラに LiveAPI 用のイベントを追加:

```python
from live_api_handler import LiveAPISession

# アクティブなLiveAPIセッションを管理
active_live_sessions = {}  # {client_sid: LiveAPISession}

@socketio.on('live_start')
def handle_live_start(data):
    """LiveAPIセッション開始"""
    client_sid = request.sid
    session_id = data.get('session_id')
    mode = data.get('mode', 'chat')
    language = data.get('language', 'ja')

    # 既存のLiveAPIセッションがあれば停止
    if client_sid in active_live_sessions:
        old_session = active_live_sessions[client_sid]
        old_session.stop()
        del active_live_sessions[client_sid]

    # プロンプト構築（03_prompt_modification_spec.md セクション7.1参照）
    # テストフェーズ: build_system_instruction() でハードコードから構築
    # 将来: GCSから取得する形に差し替え可能
    system_prompt = build_system_instruction(mode)

    # LiveAPIセッション作成
    live_session = LiveAPISession(
        session_id=session_id,
        mode=mode,
        language=language,
        system_prompt=system_prompt,
        socketio=socketio,
        client_sid=client_sid
    )
    active_live_sessions[client_sid] = live_session

    # 別スレッドでasyncioイベントループを実行（セクション10.3参照）
    def start_live_session_thread(session):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(session.run())
        except Exception as e:
            logger.error(f"[LiveAPI] スレッドエラー: {e}")
        finally:
            loop.close()

    thread = threading.Thread(
        target=start_live_session_thread,
        args=(live_session,),
        daemon=True
    )
    thread.start()

    emit('live_ready', {'status': 'connected'})


@socketio.on('live_audio_in')
def handle_live_audio_in(data):
    """ブラウザ → LiveAPI 音声データ"""
    client_sid = request.sid
    live_session = active_live_sessions.get(client_sid)

    if not live_session or not live_session.is_running:
        return

    audio_b64 = data.get('data', '')
    if not audio_b64:
        return

    try:
        pcm_bytes = base64.b64decode(audio_b64)
        live_session.enqueue_audio(pcm_bytes)
    except Exception as e:
        logger.error(f"[LiveAPI] 音声デコードエラー: {e}")


@socketio.on('live_stop')
def handle_live_stop():
    """LiveAPIセッション終了"""
    client_sid = request.sid
    if client_sid in active_live_sessions:
        live_session = active_live_sessions[client_sid]
        live_session.stop()
        del active_live_sessions[client_sid]
    emit('live_stopped', {'status': 'disconnected'})
```

**既存のイベントは変更しない**（`start_stream`, `audio_chunk`, `stop_stream` はSTT用として残す）

### 3.6 Dockerfile への追記

`live_api_handler.py` をコンテナに含める:

```dockerfile
COPY app_customer_support.py .
COPY support_core.py .
COPY api_integrations.py .
COPY long_term_memory.py .
COPY live_api_handler.py .    # ★ 追加必須
```

**注意**: このCOPYが漏れるとFlask起動時に `ImportError` → CORSヘッダーなしエラーになる。

---

## 4. フロントエンド設計

### 4.1 AudioStreamManager の改修方針

Gemini_LiveAPI_ans.txt のベストプラクティスに従い:

1. **AudioContext はシングルトン**（セッション全体で1つ）
2. **MediaStream は再利用**（毎回 getUserMedia しない）
3. **AudioWorkletNode は `addModule` 1回だけ**
4. **半二重制御はフラグで**（`isAiSpeaking`）
5. **VAD はGemini側に委譲**（クライアント側は帯域節約のみ）

### 4.2 新規ファイル: `live-audio-manager.ts`

```typescript
/**
 * Gemini LiveAPI 用 AudioStreamManager
 *
 * 【設計原則】(Gemini_LiveAPI_ans.txt より)
 * - AudioContext/MediaStream/AudioWorkletNode はセッション中使い回し
 * - 半二重制御はフラグ（isAiSpeaking）で行う
 * - VADはGemini側に委譲
 * - AI音声再生もWeb Audio APIで行う（iOS対策）
 */

class LiveAudioManager {
    private audioContext: AudioContext | null = null;
    private mediaStream: MediaStream | null = null;
    private audioWorkletNode: AudioWorkletNode | null = null;
    private socket: any; // Socket.IO

    public isAiSpeaking: boolean = false;

    // ========================================
    // セッション開始時に1度だけ呼ぶ
    // ========================================
    async initialize(socket: any): Promise<void> {
        if (this.audioContext) return; // 既に初期化済み

        this.socket = socket;

        // 1. AudioContext (1つだけ)
        this.audioContext = new AudioContext({ sampleRate: 48000 });

        // 2. getUserMedia (1回だけ)
        this.mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
                channelCount: 1,
            }
        });

        // 3. AudioWorklet登録 (1回だけ)
        // 既存の audio-manager.ts のプロセッサを流用
        // 48kHz → 16kHz ダウンサンプリング + Int16変換
        await this.audioContext.audioWorklet.addModule(processorBlobUrl);

        // 4. Node作成・接続
        const source = this.audioContext.createMediaStreamSource(
            this.mediaStream
        );
        this.audioWorkletNode = new AudioWorkletNode(
            this.audioContext, 'audio-processor'
        );
        source.connect(this.audioWorkletNode);

        // 5. フラグによる送信制御
        this.audioWorkletNode.port.onmessage = (e) => {
            if (this.isAiSpeaking) return; // 半二重: AI応答中は送信しない

            const audioChunk = e.data.audioChunk; // Int16Array
            const base64 = arrayBufferToBase64(audioChunk.buffer);
            this.socket.emit('live_audio_in', { data: base64 });
        };
    }

    // ========================================
    // AI応答音声の再生（Web Audio API, iOS対策）
    // ========================================
    playPcmAudio(pcmBase64: string): void {
        if (!this.audioContext) return;

        const pcmBytes = base64ToArrayBuffer(pcmBase64);
        // PCM 24kHz 16bit mono → Float32
        const int16 = new Int16Array(pcmBytes);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 32768.0;
        }

        const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
        buffer.copyToChannel(float32, 0);

        const source = this.audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(this.audioContext.destination);
        source.start();
    }

    // ========================================
    // フラグ切り替え
    // ========================================
    onAiResponseStarted(): void { this.isAiSpeaking = true; }
    onAiResponseEnded(): void   { this.isAiSpeaking = false; }

    // ========================================
    // 完全終了時のみ全破棄
    // ========================================
    terminate(): void {
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(t => t.stop());
        }
        if (this.audioContext) {
            this.audioContext.close();
        }
        this.audioContext = null;
        this.mediaStream = null;
        this.audioWorkletNode = null;
    }
}
```

### 4.3 Socket.IO イベントハンドリング

```typescript
// ブラウザ側の受信イベント
socket.on('live_audio', (data) => {
    // AI音声再生
    liveAudioManager.onAiResponseStarted();
    liveAudioManager.playPcmAudio(data.data);
});

socket.on('user_transcript', (data) => {
    // ユーザー文字起こし → チャット欄に表示
    updateUserTranscript(data.text);
});

socket.on('ai_transcript', (data) => {
    // AI文字起こし → チャット欄に表示
    updateAiTranscript(data.text);
});

socket.on('turn_complete', () => {
    // ターン完了 → マイク送信再開
    liveAudioManager.onAiResponseEnded();
    finalizeTranscripts(); // バッファを確定してメッセージに変換
});

socket.on('interrupted', () => {
    // 割り込み → 音声再生停止
    stopAudioPlayback();
    liveAudioManager.onAiResponseEnded();
});
```

### 4.4 フロントエンド起動フロー（コントローラー改修）

**最重要**: アプリは起動時からLiveAPIありきで動作する。
ユーザーがマイクボタンを押す前に、LiveAPI接続と初期挨拶が完了している。

#### 4.4.1 起動シーケンス全体

```
ページロード
  ↓
CoreController.init()
  ├── initSocket()          ← Socket.IO接続 + LiveAPIリスナー登録
  └── initializeSession()   ← REST APIでsession_id取得 → startLiveMode()
                                ↓
                            LiveAudioManager.initialize(socket)
                            socket.emit('live_start', {session_id, mode, language})
                                ↓
                            サーバー: LiveAPISession作成 → Gemini接続
                            サーバー: ダミーメッセージ送信 → AI初期挨拶(音声)
                                ↓
                            ブラウザ: live_audio → 音声再生
                            ブラウザ: ai_transcript → チャット欄表示
                                ↓
                            ★ ユーザーに「こんにちは！」が聞こえる
                            ★ 以降、マイクから話しかけるだけで会話継続
```

#### 4.4.2 CoreController の改修

**`initSocket()` — LiveAPIリスナーの登録**

既存の `transcript`, `error` リスナーに加え、LiveAPI用のリスナーを登録:

```typescript
protected initSocket() {
    this.socket = io(this.apiBase || window.location.origin, {
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionAttempts: 5,
        timeout: 10000
    });

    // 既存リスナー（STTフォールバック用に残す）
    this.socket.on('transcript', ...);
    this.socket.on('error', ...);

    // ★ LiveAPIリスナー（ここに全て登録する）
    this.socket.on('live_ready', () => {
        this.liveAudioManager.startStreaming();
    });
    this.socket.on('live_audio', (data) => {
        if (!this.isLiveMode) return;
        this.liveAudioManager.onAiResponseStarted();
        this.liveAudioManager.playPcmAudio(data.data);
    });
    this.socket.on('user_transcript', (data) => { ... });
    this.socket.on('ai_transcript', (data) => { ... });
    this.socket.on('turn_complete', () => { ... });
    this.socket.on('interrupted', () => { ... });
    this.socket.on('live_reconnecting', () => { ... });
    this.socket.on('live_reconnected', () => { ... });
    this.socket.on('live_fallback', (data) => {
        this.switchToRestApiMode();
    });
    this.socket.on('live_stopped', () => { ... });
}
```

**`initializeSession()` — セッション開始とLiveAPI自動接続**

REST APIでsession_idを取得した後、**自動的に** `startLiveMode()` を呼ぶ:

```typescript
protected async initializeSession() {
    // 1. REST APIでsession_id取得（既存）
    const res = await fetch(`${this.apiBase}/api/session/start`, ...);
    const data = await res.json();
    this.sessionId = data.session_id;

    // 2. UIを有効化（既存）
    this.els.userInput.disabled = false;
    // ...

    // 3. ★ LiveAPIで初期挨拶を開始（新規）
    //    REST API挨拶 + GCP TTS の処理は全て削除
    //    speakTextGCP(), preGeneratedAcks も不要
    await this.startLiveMode();
}
```

**削除するもの**:
- `this.t('initialGreeting')` のテキスト表示
- `speakTextGCP(this.t('initialGreeting'))` の呼び出し
- `preGeneratedAcks` のTTS事前生成

**`toggleRecording()` — マイクボタンの動作変更**

LiveAPIモード中はマイクボタンは「LiveAPI停止」に変わる:

```typescript
protected async toggleRecording() {
    this.enableAudioPlayback();
    this.els.userInput.value = '';

    // LiveAPIモード中 → 停止
    if (this.isLiveMode) {
        this.switchToRestApiMode();
        this.isRecording = false;
        this.els.micBtn.classList.remove('recording');
        this.resetInputState();
        return;
    }

    // 既存のSTT録音中 → 停止
    if (this.isRecording) {
        this.stopStreamingSTT();
        return;
    }

    // ... 割り込み処理（既存） ...

    // ★ LiveAPIモードで起動
    if (this.socket && this.socket.connected) {
        this.isRecording = true;
        this.els.micBtn.classList.add('recording');
        try {
            await this.startLiveMode();
        } catch (error) {
            this.isRecording = false;
            this.els.micBtn.classList.remove('recording');
            this.showError(this.t('micAccessError'));
        }
    } else {
        await this.startLegacyRecording();
    }
}
```

#### 4.4.3 ConciergeController の改修

**`initSocket()` — 親クラスを呼んでからtranscriptだけ上書き**

```typescript
protected initSocket() {
    // ★ 親クラスのinitSocket()を呼んでLiveAPIリスナーも登録
    super.initSocket();

    // コンシェルジュ版のtranscriptハンドラを上書き
    this.socket.off('transcript');
    this.socket.on('transcript', (data) => {
        const { text, is_final } = data;
        if (this.isAISpeaking) return;
        if (is_final) {
            this.handleStreamingSTTComplete(text);
            this.currentAISpeech = "";
        } else {
            this.els.userInput.value = text;
        }
    });
}
```

**重要**: 以前の実装では `super.initSocket()` を呼ばずに完全オーバーライドしていた。
これだとLiveAPIリスナーが一切登録されない。必ず `super.initSocket()` を呼ぶこと。

**`initializeSession()` — コンシェルジュ版もLiveAPI挨拶に統一**

```typescript
protected async initializeSession() {
    // 1. REST APIでsession_id取得（既存、user_id付き）
    const userId = this.getUserId();
    const res = await fetch(`${this.apiBase}/api/session/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            user_info: { user_id: userId },
            language: this.currentLanguage,
            mode: 'concierge'
        })
    });
    const data = await res.json();
    this.sessionId = data.session_id;

    // 2. UIを有効化
    // ...

    // 3. ★ LiveAPIで初期挨拶を開始
    //    REST APIの greetingText + speakTextGCP() は全て削除
    //    preGeneratedAcks のTTS事前生成も削除
    await this.startLiveMode();
}
```

**削除するもの**:
- `data.initial_message || this.t('initialGreetingConcierge')` のテキスト表示
- `speakTextGCP(greetingText)` の呼び出し
- `preGeneratedAcks` のTTS事前生成（ackTextsのfetch群）

---

## 4.5 初期あいさつのLiveAPI統一

### 4.5.1 現行方式の問題点

現行では `SupportAssistant.get_initial_message()` がテキスト文字列を返し、
フロントエンドが REST API 経由で TTS 再生 + チャット欄表示する方式。

LiveAPI化した場合、通常会話は LiveAPI 経由の音声 + transcription なのに、
初期あいさつだけ REST + TTS という**処理経路の不一致**が生まれる。

### 4.5.2 新方式: ダミーメッセージで初期あいさつをトリガー

LiveAPI は `response_modalities: ["AUDIO"]` で動作するため、
**AI側から先に話し始めることができない**（ユーザー入力が必要）。

この制約を回避するため、セッション開始時にサーバー側からダミーの
ユーザーメッセージを `send_client_content()` で送信し、
AIの初期あいさつ音声応答をトリガーする。

**根拠**: stt_stream.py:766-776 で再接続時に同じパターンを使用:
```python
await session.send_client_content(
    turns=types.Content(role="user", parts=[types.Part(text="続きをお願いします")]),
    turn_complete=True
)
```

### 4.5.3 初期あいさつフロー

```
ブラウザ                   サーバー                         Gemini LiveAPI
  │                          │                                │
  ├─ emit('live_start') ──→ │                                │
  │                          ├─ LiveAPI接続 ─────────────────→│
  │                          │                                │
  │  （ユーザーは何もしていない）                               │
  │                          │                                │
  │                          ├─ send_client_content() ──────→│
  │                          │  "こんにちは。お店探しを        │
  │                          │   手伝ってください。"            │
  │                          │  (ダミーメッセージ)              │
  │                          │                                │
  │                          │←─ AI音声応答 ─────────────────│
  │                          │  「こんにちは！どんなお店を     │
  │                          │   お探しですか？」              │
  │                          │                                │
  │ ←─ emit('live_audio') ─ │  (音声PCM)                     │
  │ ←─ emit('ai_transcript')│  (文字起こし)                   │
  │                          │                                │
  │  ★ ユーザーに聞こえる:                                    │
  │  「こんにちは！どんなお店をお探しですか？」                  │
  │  ★ チャット欄に表示:                                      │
  │  AI: こんにちは！どんなお店をお探しですか？                  │
  │                                                           │
  │  （以降、ユーザーがマイクで話し始める）                     │
```

### 4.5.4 ダミーメッセージの設計

ダミーメッセージはチャット欄に**表示しない**。
ユーザーには「AIが先に話しかけてくれた」ように見える。

```python
# バックエンド側（LiveAPISession クラス変数）
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
```

**chatモード**: 用途を含めたメッセージ → AIがお店探しの文脈で応答
**conciergeモード**: シンプルな挨拶 → AIがシステムプロンプトに従い名前を聞く等

### 4.5.5 `_is_initial_greeting_phase` フラグの制御

ダミーメッセージの `input_transcription`（「こんにちは。」等）がチャット欄に
表示されないよう、サーバー側でフラグ制御する。

**フラグのライフサイクル**:

```python
# __init__で True に設定
self._is_initial_greeting_phase = True

# run() 内:
if self.session_count == 1:
    # 初回接続: ダミーメッセージ送信前に True にする
    self._is_initial_greeting_phase = True
    # ... send_client_content() ...
else:
    # 再接続時: ダミーメッセージではないので False
    self._is_initial_greeting_phase = False

# _receive_and_forward() 内:
# ターン完了時にフラグを解除
if hasattr(sc, 'turn_complete') and sc.turn_complete:
    if self._is_initial_greeting_phase:
        self._is_initial_greeting_phase = False  # ★ 初期挨拶完了

# input_transcription の転送判定
if hasattr(sc, 'input_transcription') and sc.input_transcription:
    text = sc.input_transcription.text
    if text and not self._is_initial_greeting_phase:
        # ★ 初期あいさつフェーズのinput_transcriptionは転送しない
        self.user_transcript_buffer += text
        self.socketio.emit('user_transcript',
            {'text': text}, room=self.client_sid)
```

### 4.5.6 ブラウザ側の変更

ブラウザ側は特別な制御不要。サーバーが `user_transcript` を送信しないため、
ダミーメッセージはチャット欄に表示されない。AI発話のみ通常通り表示される。

```typescript
// 通常のai_transcriptハンドラがそのまま使える
socket.on('ai_transcript', (data) => {
    // AI発話をチャット欄に表示
    updateAiTranscript(data.text);
});
```

### 4.5.7 現行 get_initial_message() との関係

| | 現行 (REST API) | 新設計 (LiveAPI) |
|---|---|---|
| 初期あいさつ生成 | `SupportAssistant.get_initial_message()` | LiveAIが音声で応答 |
| テキスト表示 | REST レスポンスをチャット欄に表示 | `output_transcription` をチャット欄に表示 |
| 音声再生 | Google TTS (`/api/tts/synthesize`) | LiveAPIの音声出力を直接再生 |
| コンシェルジュ初回 | 「何とお呼びすれば？」(ハードコード) | システムプロンプトの指示でAIが判断 |
| リピーター対応 | profile読み込み→名前呼びかけ(ハードコード) | システムプロンプトにprofile注入→AIが判断 |

**`get_initial_message()` は廃止ではなくフォールバック用に残す**
（LiveAPI接続失敗時にREST APIモードで使用）

---

## 5. ショップ提案の処理フロー

### 5.1 課題

LiveAPIは `response_modalities: ["AUDIO"]` で動作するため、**テキストJSONを直接返せない**。
ショップ提案はJSON形式が必要（`shops` 配列、`action` フィールド等）。

### 5.2 設計方針: ハイブリッドアプローチ

stt_stream.py のハイブリッド方式（仕様書 セクション1.2）を踏襲:

```
[通常会話]
ユーザー音声 → LiveAPI → AI音声応答（低遅延）
                         ↓
                   output_transcription → チャット欄表示

[ショップ提案が必要な場合]
ユーザー音声 → LiveAPI → 「お調べしますね」等の短い音声応答
                         ↓
                   output_transcriptionからショップ提案意図を検知
                         ↓
              サーバー側で REST API に切り替え
                         ↓
              Gemini REST API → JSON応答（shops配列）
                         ↓
              WebSocket → ブラウザ → ショップカード表示
              Google TTS → WebSocket → ブラウザ → 音声再生
```

### 5.3 ショップ提案検知ロジック

LiveAPIの `output_transcription` テキストを監視して、ショップ提案の意図を検知:

```python
# サーバー側
SHOP_TRIGGER_KEYWORDS = [
    'お探ししますね', 'お調べしますね', '探してみますね',
    'ご紹介しますね', 'おすすめ', '提案',
]

def should_trigger_shop_search(ai_text: str) -> bool:
    """AI発話からショップ検索トリガーを検知"""
    return any(kw in ai_text for kw in SHOP_TRIGGER_KEYWORDS)
```

**注意**: この検知ロジックは暫定的。将来 Function Calling が有効になれば、明示的なトリガーに置き換え可能。

### 5.4 ショップ提案時の処理フロー（詳細）

```
1. LiveAPI output_transcription でショップ提案意図を検知
2. LiveAPI の音声応答は「お探ししますね」等の短い応答のみ
3. ターン完了後、サーバー側で REST API を呼び出し:
   - ユーザーの要望（user_transcript_buffer から取得）
   - 現行の SupportAssistant.process_user_message() を流用
4. REST API のJSON応答をブラウザに転送:
   - WebSocket: {type: 'shop_result', shops: [...], message: '...'}
5. ブラウザ側:
   - shops → ShopCardList コンポーネントで表示
   - message → TTS で読み上げ（既存の speakTextGCP() を流用）
```

---

## 6. セッション管理

### 6.1 LiveAPIセッションのライフサイクル

```
ブラウザ                    サーバー                      Gemini LiveAPI
  │                          │                              │
  ├─ emit('live_start') ──→ │                              │
  │                          ├─ LiveAPISession 作成 ──────→│
  │                          │  client.aio.live.connect()   │
  │                          │←─ 接続完了 ─────────────────│
  │ ←─ emit('live_ready')── │                              │
  │                          │                              │
  ├─ emit('live_audio_in')→ │                              │
  │                          ├─ send_realtime_input() ────→│
  │                          │                              │
  │                          │←─ response ────────────────│
  │ ←─ emit('live_audio') ─ │  (audio + transcription)     │
  │ ←─ emit('*_transcript')─│                              │
  │                          │                              │
  ├─ emit('live_stop') ──→  │                              │
  │                          ├─ セッション切断 ────────────→│
  │                          │  is_running = False           │
```

### 6.2 既存セッションとの関係

```python
# 既存の SupportSession（RAM）に LiveAPI セッション情報を追加
data = {
    'session_id': ...,
    'mode': ...,
    'language': ...,
    # 既存フィールド...

    # LiveAPI用追加フィールド
    'live_api_session': LiveAPISession,  # LiveAPIセッションオブジェクト
    'live_api_active': True/False,       # LiveAPIが有効かどうか
}
```

---

## 7. 再接続メカニズム

### 7.1 サーバー側の再接続（stt_stream.py準拠）

仕様書 セクション8 をそのまま実装:

1. **再接続トリガー**: 発言途切れ / 長い発話(500文字) / 累積上限(800文字)
2. **会話履歴引き継ぎ**: 直近10ターン、各150文字まで
3. **システムインストラクション再構築**: コンテキスト要約を注入
4. **再接続通知**: `send_client_content(text="続きをお願いします")`

### 7.2 ブラウザ側への通知

```python
# サーバーが再接続する際にブラウザに通知
self.socketio.emit('live_reconnecting', {}, room=self.client_sid)
# 再接続完了後
self.socketio.emit('live_reconnected', {}, room=self.client_sid)
```

ブラウザ側は:
- 再接続中: マイク送信を一時停止（音声が消失しないように）
- 再接続完了: マイク送信を再開

---

## 8. フォールバック戦略

### 8.1 LiveAPI → REST API フォールバック

LiveAPIが利用不可能な場合（接続エラー、モデル非対応等）:

```python
try:
    # LiveAPIで接続
    async with self.client.aio.live.connect(...) as session:
        ...
except Exception as e:
    logger.error(f"[LiveAPI] 接続失敗、REST APIにフォールバック: {e}")
    # 既存のREST APIフローに切り替え
    self.socketio.emit('live_fallback', {
        'reason': str(e)
    }, room=self.client_sid)
```

ブラウザ側:
```typescript
socket.on('live_fallback', (data) => {
    // LiveAPIモードを無効化
    // 既存のSTT→REST API→TTSフローに切り替え
    switchToRestApiMode();
});
```

### 8.2 モード切り替えUI

ユーザーが手動でLiveAPIモードとREST APIモードを切り替えられるトグルを用意:

```
[LiveAPI モード（リアルタイム音声）] ←→ [テキストモード（従来方式）]
```

---

## 9. 実装フェーズ計画

### Phase 1: 基盤構築（最小動作確認）

1. `live_api_handler.py` の作成（LiveAPISession クラス）
2. Socket.IO イベントハンドラの追加（`live_start`, `live_audio_in`, `live_stop`）
3. LiveAPI接続→音声送信→音声受信→ブラウザ再生の最小ループ確認
4. **テスト**: ブラウザで話しかけて、AIの音声応答が聞こえることを確認

### Phase 2: トランスクリプション

1. `input_transcription` → チャット欄にユーザー発話表示
2. `output_transcription` → チャット欄にAI発話表示
3. ターン完了時のメッセージ確定処理

### Phase 3: ショップ提案ハイブリッド

1. AI発話からショップ提案意図の検知
2. REST API へのフォールバック
3. ショップカード表示 + TTS読み上げ

### Phase 4: 安定化

1. 再接続メカニズムの実装
2. エラーハンドリングの強化
3. iOS Safari 対応（Web Audio APIでの再生統一）
4. フォールバック戦略の実装

### Phase 5: 最適化

1. 音声バッファリングの最適化
2. 帯域節約（クライアント側無音カット）
3. UIの改善（音声波形表示等）

---

## 10. 既知のリスク・未解決課題

### 10.1 LiveAPIプレビュー版の制約

- **Function Calling無効**: ポリシーエラーで使えない（stt_stream.py:57-58）
- **音声出力の長さ制限**: 長文は途切れる（仕様書 セクション15.2）
- **セッション品質劣化**: 累積発話量で品質が下がる（仕様書 セクション15.3）
- **モデル名変更の可能性**: `gemini-2.5-flash-native-audio-preview-12-2025` は暫定名

### 10.2 WebSocketの二重化

現行: Socket.IO（STT用）
新規: Socket.IO（LiveAPI用）

同一のSocket.IO接続を共有するが、イベント名で分離する設計。

### 10.3 async/syncの混在

Flask + Socket.IO は同期ベース。LiveAPIは asyncio ベース。
`asyncio.run()` または `threading` + `asyncio.new_event_loop()` での統合が必要。

```python
# 案: 別スレッドでasyncioイベントループを実行
import threading

def start_live_session_thread(session: LiveAPISession):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(session.run())
    finally:
        loop.close()

thread = threading.Thread(target=start_live_session_thread,
                          args=(live_session,), daemon=True)
thread.start()
```

### 10.4 音声再生の連続性

LiveAPIから断片的に送られてくるPCMチャンクを、途切れなく連続再生する必要がある。
`AudioBufferSourceNode` を逐次作成する方式では、チャンク間にギャップが生じる可能性。

**対策案**: ScriptProcessorNode または AudioWorklet で再生キューを実装

```typescript
// 再生キューの概念
class AudioPlaybackQueue {
    private queue: Float32Array[] = [];
    private isPlaying = false;

    enqueue(pcmData: Float32Array): void {
        this.queue.push(pcmData);
        if (!this.isPlaying) this.startPlayback();
    }

    // AudioWorklet内でキューから順次読み出して再生
}
```

---

## 11. テスト計画

### 11.1 Phase 1 テスト項目

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | LiveAPI接続 | `live_ready` イベントがブラウザに届く |
| 2 | 音声送信 | サーバーログに「音声受信」が表示される |
| 3 | 音声受信 | ブラウザのスピーカーからAI音声が聞こえる |
| 4 | ターン完了 | `turn_complete` イベントが届く |
| 5 | 接続エラー | REST APIにフォールバックする |
| 6 | セッション終了 | リソースが正しく解放される |

### 11.2 Phase 2 テスト項目

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | ユーザー文字起こし | チャット欄にユーザー発話が表示される |
| 2 | AI文字起こし | チャット欄にAI発話が表示される |
| 3 | 割り込み | AI音声再生が停止し、新しい入力を受け付ける |

### 11.3 Phase 3 テスト項目

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | ショップ提案トリガー | AI発話からショップ検索意図を検知する |
| 2 | REST APIフォールバック | JSON形式のショップデータが返る |
| 3 | ショップカード表示 | ブラウザにショップカードが表示される |
| 4 | TTS読み上げ | ショップ紹介が音声で読み上げられる |

---

*以上が LiveAPI 移植設計書。実装時は本設計書と `01_stt_stream_detailed_spec.md` を常に参照すること。*
