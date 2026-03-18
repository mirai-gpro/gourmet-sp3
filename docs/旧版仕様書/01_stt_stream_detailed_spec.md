# stt_stream.py 詳細仕様書（移植元コード精査）

> **作成日**: 2026-03-10
> **対象ファイル**: `docs/stt_stream.py`（1063行）
> **目的**: LiveAPI移植時に「何を・どう実装するか」の唯一の正解として参照する仕様書
> **注意**: Gemini LiveAPI は 2025年12月プレビュー版。Claudeの知識ベースにほぼ情報がないため、本仕様書のコード引用を絶対的な正解として扱うこと。推論で補完してはならない。

---

## 0. 本仕様書の使い方（最重要）

### Claudeへの指示

1. **本仕様書に書かれていないことは実装しない**
2. **本仕様書のコードと異なる実装をしない**
3. **「多分こうだろう」で書き換えない**。不明点があれば必ずユーザーに確認する
4. **修正が必要な場合、修正前に本仕様書の該当箇所を再読する**（「確認しました」と言って確認しないのは厳禁）
5. **本仕様書から乖離した改変を入れた場合は、即座にrevertする**

---

## 1. 全体アーキテクチャ

### 1.1 システム概要

```
デスクトップアプリケーション（Python, PyAudio）
├── Gemini Live API（WebSocket接続、音声in/out）
│   └── モデル: gemini-2.5-flash-native-audio-preview-12-2025
├── REST API（テキストベース、検索・資料参照用）
│   └── モデル: gemini-2.5-flash
├── Google Cloud TTS（REST APIの応答を音声化）
└── ローカルオーディオ（PyAudio: マイク入力、スピーカー出力）
```

### 1.2 ハイブリッド方式の意図（stt_stream.py:1-7）

```python
"""
Gemini Live API ベースの会議アシスタント（Function Callingハイブリッド方式）
- 短い応答 → Live API（低遅延）
- 長い応答（要約・質問・検索・資料参照）→ REST API + TTS
- Function Callingで明示的に切り替え
"""
```

- **短い応答**（相槌、短い質問）: Live APIが音声で直接応答（低遅延）
- **長い応答**（検索、資料参照、要約）: REST APIでテキスト生成 → Google Cloud TTSで音声合成
- **切り替え方法**: Function Calling（ただし現在は無効化、後述）

---

## 2. 定数・設定値（stt_stream.py:25-50）

### 2.1 Live API設定

| 定数名 | 値 | 用途 |
|---|---|---|
| `LIVE_API_MODEL` | `"gemini-2.5-flash-native-audio-preview-12-2025"` | Live API用モデル名 |
| `REST_API_MODEL` | `"gemini-2.5-flash"` | REST API用モデル名 |
| `SEND_SAMPLE_RATE` | `16000` | マイク→Live APIの送信サンプルレート（16kHz） |
| `RECEIVE_SAMPLE_RATE` | `24000` | Live API→スピーカーの受信サンプルレート（24kHz） |
| `TTS_SAMPLE_RATE` | `24000` | Google Cloud TTSのサンプルレート（24kHz） |
| `CHUNK_SIZE` | `1024` | 音声チャンクサイズ |
| `CHANNELS` | `1` | モノラル |
| `FORMAT` | `pyaudio.paInt16` | 16bit PCM |

### 2.2 セッション再接続閾値（stt_stream.py:372-373）

| 定数名 | 値 | 用途 |
|---|---|---|
| `MAX_AI_CHARS_BEFORE_RECONNECT` | `800` | AI発話の累積文字数上限。超過で再接続 |
| `LONG_SPEECH_THRESHOLD` | `500` | 1回の発話がこの文字数を超えたら即再接続 |

---

## 3. Function Calling（stt_stream.py:56-59）

### 3.1 現在の状態：無効化

```python
def get_interview_tools():
    """Live API用のツール定義 - 一旦無効化"""
    # ポリシーエラーが発生するため、ツールを無効化
    return []
```

**重要**: Live APIでのFunction Callingは**ポリシーエラー**により無効化されている。これはプレビュー版の制約と思われる。移植時も同様の問題が発生する可能性が高い。

---

## 4. システムインストラクション（stt_stream.py:65-156）

### 4.1 3つのモード

| モード | 変数名 | 発話制限 | 用途 |
|---|---|---|---|
| standard | `STANDARD_SYSTEM_INSTRUCTION` | 2〜3文、10秒以内 | 会議サポート |
| silent | `SILENT_SYSTEM_INSTRUCTION` | 呼ばれた時のみ応答 | 書記役 |
| interview | `INTERVIEW_SYSTEM_INSTRUCTION` | パターンに従う | インタビュー |

### 4.2 LiveAPI共通の重要な制約

**発話の長さ制限**（stt_stream.py:73-74）:
```
1回の発話は2〜3文以内、10秒以内に収めてください。
```

これはLive APIの音声出力に厳しい制限があるため。長い応答はREST API + TTSに委譲する設計。

### 4.3 REST API用システムインストラクション（stt_stream.py:139-156）

```python
REST_API_SYSTEM_INSTRUCTION = """あなたはプロのインタビュアーです。
必ず日本語で回答してください。

【回答スタイル】
- 音声で読み上げられることを意識して、聞きやすい表現を使う
- マークダウン記法（**太字**、# 見出し、- リストなど）は使わない
- 質問は必ず「〜でしょうか？」「〜ですか？」で丁寧に終える
"""
```

**TTSで読み上げるため、マークダウン禁止**。これはgourmet-sp3でも同様に重要。

---

## 5. GeminiLiveApp クラス（stt_stream.py:368-975）

### 5.1 クラス構成

```python
class GeminiLiveApp:
    # 再接続閾値
    MAX_AI_CHARS_BEFORE_RECONNECT = 800
    LONG_SPEECH_THRESHOLD = 500

    def __init__(self, mode, input_device_index, output_device_index):
        # 状態管理
        self.user_transcript_buffer = ""    # ユーザー発話バッファ
        self.ai_transcript_buffer = ""      # AI発話バッファ
        self.conversation_history = []      # 会話履歴（直近20ターン）
        self.ai_char_count = 0              # AI発話文字数の累積
        self.needs_reconnect = False        # 再接続フラグ
        self.session_count = 0              # セッション番号

        # 外部サービス
        self.client = genai.Client(api_key=...)     # Gemini APIクライアント
        self.rest_handler = RestAPIHandler(mode)     # REST API処理
        self.tts_player = TTSPlayer(output_device_index)  # TTS再生
```

### 5.2 Live API接続設定（stt_stream.py:410-469）

**最重要**: この設定がLive APIの動作を決定する。移植時に1つでも変えると動作が変わる。

```python
config = {
    "response_modalities": ["AUDIO"],          # ★ 音声応答モード
    "system_instruction": instruction,
    "input_audio_transcription": {},            # ★ 入力音声の文字起こしを有効化
    "output_audio_transcription": {},           # ★ 出力音声の文字起こしを有効化
    "speech_config": {
        "language_code": "ja-JP",              # ★ 日本語
    },
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False,                 # ★ サーバーサイドVAD有効
            "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
            "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
            "prefix_padding_ms": 100,
            "silence_duration_ms": 500,        # ★ 500ms無音でターン終了
        }
    },
    "context_window_compression": {
        "sliding_window": {
            "target_tokens": 32000,            # ★ コンテキストウィンドウ圧縮
        }
    },
}
```

#### 各設定の意味（コードから読み取れる事実のみ）

| 設定 | 値 | 意味 |
|---|---|---|
| `response_modalities` | `["AUDIO"]` | AIは音声で応答する |
| `input_audio_transcription` | `{}` | ユーザーの音声入力を文字起こしする（空辞書で有効化） |
| `output_audio_transcription` | `{}` | AIの音声出力を文字起こしする（空辞書で有効化） |
| `speech_config.language_code` | `"ja-JP"` | 音声の言語 |
| `automatic_activity_detection.disabled` | `False` | サーバーサイドVADが有効 |
| `start_of_speech_sensitivity` | `HIGH` | 発話開始検知の感度が高い |
| `end_of_speech_sensitivity` | `HIGH` | 発話終了検知の感度が高い |
| `prefix_padding_ms` | `100` | 発話開始前のパディング100ms |
| `silence_duration_ms` | `500` | 500msの無音でターン終了と判定 |
| `context_window_compression` | `target_tokens: 32000` | スライディングウィンドウで32000トークンに圧縮 |

---

## 6. Live API接続フロー（stt_stream.py:714-796）

### 6.1 メインループ（run()）

```python
async def run(self):
    while True:
        self.session_count += 1
        self.ai_char_count = 0          # 文字数カウントリセット
        self.needs_reconnect = False

        # 再接続時は会話履歴を引き継ぐ
        context = None
        if self.session_count > 1:
            context = self._get_context_summary()

        config = self._build_config(with_context=context)

        async with self.client.aio.live.connect(
            model=LIVE_API_MODEL,
            config=config
        ) as session:
            # 初回 or 再接続の処理
            if self.session_count > 1:
                # 再接続時はテキストで挨拶を送って応答を促す
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
```

### 6.2 接続メソッド

```python
self.client.aio.live.connect(model=LIVE_API_MODEL, config=config)
```

- `genai.Client` の `aio.live.connect()` を使用
- **async with** でコンテキストマネージャとして使用
- `session` オブジェクトが返される

### 6.3 再接続時のテキスト送信

```python
await session.send_client_content(
    turns=types.Content(
        role="user",
        parts=[types.Part(text="続きをお願いします")]
    ),
    turn_complete=True
)
```

- `send_client_content()` でテキストメッセージを送信
- `turns` に `types.Content` オブジェクトを渡す
- `turn_complete=True` でターン完了を通知

---

## 7. 音声送受信（stt_stream.py:832-938）

### 7.1 セッションループ（_session_loop()）

4つの非同期タスクを `asyncio.TaskGroup` で並行実行:

```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(listen_audio())   # マイク→キュー
    tg.create_task(send_audio())     # キュー→Live API
    tg.create_task(receive())        # Live API→キュー
    tg.create_task(play_audio())     # キュー→スピーカー
```

### 7.2 音声送信（send_audio）

```python
async def send_audio():
    while not self.needs_reconnect:
        msg = await asyncio.wait_for(
            self.audio_queue_mic.get(),
            timeout=0.1
        )
        await session.send_realtime_input(audio=msg)
```

**送信メソッド**: `session.send_realtime_input(audio=msg)`
**音声フォーマット**: `{"data": data, "mime_type": "audio/pcm"}`（listen_audioで作成）

```python
# listen_audio内
self.audio_queue_mic.put_nowait({"data": data, "mime_type": "audio/pcm"})
```

### 7.3 音声受信（receive_audio, stt_stream.py:579-683）

```python
async def receive_audio(self, session):
    while not self.needs_reconnect:
        turn = session.receive()
        async for response in turn:
            # 1. tool_call イベント
            if hasattr(response, 'tool_call') and response.tool_call:
                await self._handle_tool_call(response.tool_call, session)
                continue

            if response.server_content:
                sc = response.server_content

                # 2. ターン完了
                if hasattr(sc, 'turn_complete') and sc.turn_complete:
                    # ユーザー/AI トランスクリプトバッファを処理
                    # 再接続判定（後述）

                # 3. 割り込み検知
                if hasattr(sc, 'interrupted') and sc.interrupted:
                    # 音声キューをクリア

                # 4. 入力トランスクリプション
                if hasattr(sc, 'input_transcription') and sc.input_transcription:
                    self.user_transcript_buffer += sc.input_transcription.text

                # 5. 出力トランスクリプション
                if hasattr(sc, 'output_transcription') and sc.output_transcription:
                    self.ai_transcript_buffer += sc.output_transcription.text

                # 6. 音声データ
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if hasattr(part, 'inline_data') and part.inline_data:
                            self.audio_queue_output.put_nowait(part.inline_data.data)
```

#### レスポンスオブジェクトの構造（コードから判明した事実）

```
response
├── .tool_call                           # Function Call応答
│   └── .function_calls[]
│       ├── .name                        # 関数名
│       ├── .args                        # 引数（dict）
│       └── .id                          # 関数呼び出しID
├── .server_content
│   ├── .turn_complete (bool)            # ターン完了フラグ
│   ├── .generation_complete (bool)      # 生成完了フラグ
│   ├── .interrupted (bool)              # 割り込みフラグ
│   ├── .input_transcription
│   │   └── .text (str)                  # ユーザー音声のテキスト
│   ├── .output_transcription
│   │   └── .text (str)                  # AI音声のテキスト
│   └── .model_turn
│       └── .parts[]
│           └── .inline_data
│               └── .data (bytes)        # 音声データ（PCM 24kHz）
```

---

## 8. 再接続メカニズム（stt_stream.py:600-643, 940-966）

### 8.1 再接続トリガー条件（3つ）

#### 条件1: 発言途切れ

```python
if is_incomplete:
    self.needs_reconnect = True
```

`_is_speech_incomplete()` で判定:
- 文末が「、」「の」「を」「が」「は」等の助詞で終わっている → 途切れ

#### 条件2: 長い発話

```python
elif char_count >= self.LONG_SPEECH_THRESHOLD:  # 500文字
    self.needs_reconnect = True
```

#### 条件3: 累積上限

```python
elif self.ai_char_count >= self.MAX_AI_CHARS_BEFORE_RECONNECT:  # 800文字
    self.needs_reconnect = True
```

### 8.2 再接続時の会話引き継ぎ（stt_stream.py:940-966）

```python
def _get_context_summary(self):
    recent = self.conversation_history[-10:]  # 直近10ターン
    summary_parts = []
    for h in recent:
        text = h['text'][:150]  # 150文字まで
        summary_parts.append(f"{h['role']}: {text}")

    # 最後のAI発言が質問なら強調
    # ...
    return summary
```

### 8.3 再接続時のconfig再構築（stt_stream.py:414-438）

```python
instruction += f"""
【これまでの会話の要約】
{with_context}

【重要：必ず守ること】
1. 直前の話者の発言「{last_user_message}」に対して短い相槌を入れる
2. 既に聞いた質問は絶対に繰り返さない
3. 次に聞くべき質問：「{next_q[:100]}」
"""
```

---

## 9. エラーハンドリング（stt_stream.py:786-796, 898-911）

### 9.1 接続エラー時の再接続

```python
# 再接続可能なエラー
if any(keyword in error_msg for keyword in
    ["1011", "internal error", "disconnected", "closed", "websocket"]):
    await asyncio.sleep(3)
    self.needs_reconnect = True
    continue
```

### 9.2 受信エラー時

```python
# 受信側で検出するエラー
if any(keyword in error_msg for keyword in
    ["1011", "1008", "internal error", "closed", "deadline", "policy"]):
    if "deadline" in error_msg:
        # サーバータイムアウト
    elif "1008" in error_msg or "policy" in error_msg:
        # ポリシーエラー
    self.needs_reconnect = True
```

| エラーコード | 意味 | 対処 |
|---|---|---|
| 1011 | WebSocket内部エラー | 再接続 |
| 1008 | ポリシーエラー | 再接続 |
| deadline | タイムアウト | 再接続 |
| policy | ポリシー違反 | 再接続 |

---

## 10. REST APIハンドラ（stt_stream.py:307-362）

### 10.1 初期化

```python
class RestAPIHandler:
    def __init__(self, mode):
        self.client = genai.Client(api_key=...)
        self.chat = None
        self.pdf_file = None
        self._init_chat()

    def _init_chat(self):
        # PDFアップロード
        self.pdf_file = self.client.files.upload(file=REFERENCE_PDF_FILE_PATH)

        # チャットセッション作成
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        self.chat = self.client.chats.create(
            model=REST_API_MODEL,
            config=config,
        )
```

### 10.2 クエリ実行

```python
def query(self, prompt):
    response = self.chat.send_message(prompt)
    return response.text.strip()
```

---

## 11. TTSプレイヤー（stt_stream.py:213-301）

### 11.1 音声合成設定

```python
self.voice = texttospeech.VoiceSelectionParams(
    language_code="ja-JP",
    name="ja-JP-Wavenet-D",  # 男性、自然な声
)
self.audio_config = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.LINEAR16,
    sample_rate_hertz=TTS_SAMPLE_RATE,  # 24000
    speaking_rate=1.05,  # 少し速め
    pitch=0.0,
)
```

### 11.2 再生フロー

1. テキストからマークダウン記法を除去
2. 文単位で分割（最大200文字/文）
3. 全文を**事前バッファリング**（先に全文合成）
4. 合成完了後に**連続再生**

---

## 12. 効果音（stt_stream.py:162-183）

| 定数名 | 用途 | 仕様 |
|---|---|---|
| `SEARCHING_BEEP` | 検索中のビープ音 | 600Hz, 0.2秒 |
| `THINKING_SOUND` | 考え中の音 | 500Hz 0.15秒 → 無音0.1秒 → 600Hz 0.15秒 |

---

## 13. Gemini LiveAPI ブラウザ実装のベストプラクティス（Gemini_LiveAPI_ans.txt）

### 13.1 AudioContext / MediaStream ライフサイクル

> **結論: 毎回破棄・再作成せず、セッション全体で「1つ」を維持し、フラグ制御に切り替えるべき**

- AudioContext: シングルトン（1つだけ）
- AudioWorkletNode: `addModule` は1回だけ
- MediaStream: セッション終了まで保持

### 13.2 半二重制御

> **NodeやStreamを破棄するのではなく、送信処理のON/OFF（フラグ制御）で半二重を実現**

```typescript
// AI応答開始 → マイク送信ブロック
this.isAiSpeaking = true;

// AI応答終了 → マイク送信再開
this.isAiSpeaking = false;
```

### 13.3 VADの役割分担

> **会話のターン制御は「Gemini側のVAD」に任せ、クライアント側VADは「帯域節約（無音カット）」のみ**

- クライアント側: 無音検知 → 送信スキップ（帯域節約）
- サーバー側（Gemini）: 文脈考慮のターン終了検知 → AI応答開始

### 13.4 iOS Safari対策

> **AIの応答音声も HTMLAudioElement ではなく、Web Audio API (AudioContext) を使って再生する**

- 入出力を同じ `AudioContext` で統一
- iOS Audio Session が `PlayAndRecord` に固定される問題を回避

### 13.5 推奨コードパターン

```typescript
class AudioStreamManager {
    // セッション開始時に1度だけ呼ばれる初期化
    async initializeAndStart(ws: WebSocket) { ... }

    // フラグ切り替え
    onAiResponseStarted() { this.isAiSpeaking = true; }
    onAiResponseEnded()   { this.isAiSpeaking = false; }

    // iOS対策: Web Audio APIで再生
    playAiAudio(pcmData24kHz: Float32Array) { ... }

    // 通話終了時だけ全て破棄
    terminateSession() { ... }
}
```

---

## 14. gourmet-sp3 現行システムとの対応表

| stt_stream.py の要素 | gourmet-sp3 現行の対応物 | 移植時の関係 |
|---|---|---|
| `GeminiLiveApp` | `SupportAssistant` (REST API) | LiveAPI版SupportAssistantに置き換え |
| `RestAPIHandler` | `SupportAssistant.process_user_message()` | 長文応答のフォールバック用に残す可能性 |
| `TTSPlayer` | `/api/tts/synthesize` エンドポイント | LiveAPIは音声直接出力なのでTTS不要 |
| PyAudio マイク入力 | AudioWorklet (ブラウザ) | ブラウザ→WebSocket→サーバー→LiveAPI |
| PyAudio スピーカー出力 | HTMLAudioElement / Web Audio API | LiveAPI→サーバー→WebSocket→ブラウザ |
| `session.send_realtime_input()` | 新規実装必要 | サーバー側でLiveAPIに音声転送 |
| `session.receive()` | 新規実装必要 | LiveAPIからの応答をブラウザに転送 |
| `automatic_activity_detection` | 現行: クライアント側VAD | サーバー側VADに委譲 |
| 再接続メカニズム | なし | 新規実装必要 |

---

## 15. 注意事項・既知の問題

### 15.1 Function Callingの無効化

stt_stream.py:56-59で明示的に無効化。`ポリシーエラーが発生するため`。
プレビュー版の制約の可能性が高い。

### 15.2 LiveAPIの音声出力制限

音声モードでは**長い応答ができない**。これがハイブリッド方式が必要な理由。
gourmet-sp3のショップ提案（JSON形式の長文応答）をLiveAPIで直接返すことは困難。

### 15.3 再接続の必要性

LiveAPIセッションは累積発話量に応じて品質劣化する（発言途切れなど）。
`MAX_AI_CHARS_BEFORE_RECONNECT = 800` という比較的低い閾値が設定されている。

### 15.4 `context_window_compression`

```python
"context_window_compression": {
    "sliding_window": {
        "target_tokens": 32000,
    }
}
```

長時間セッションでコンテキストが溢れないための圧縮設定。

---

*以上が stt_stream.py の詳細仕様。移植時はこの文書を唯一の正解として参照すること。*
