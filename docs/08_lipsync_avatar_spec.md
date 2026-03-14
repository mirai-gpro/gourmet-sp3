# 08: リップシンク・アバター実装仕様書（フェーズ2）

## 1. 概要

### 1.1 目的
コンシェルジュモードの2Dアバター画像（CSSアニメーション）を、Gaussian Splatting 3Dアバター + ARKit 52ブレンドシェイプによるリアルタイムリップシンクに置き換える。

### 1.2 現行アーキテクチャの前提（重要）

**REST TTS は完全廃止済み。** 全音声はGemini LiveAPI経由のPCMストリーミングで処理される。

| 項目 | 現行実装 |
|------|---------|
| 音声ソース | Gemini LiveAPI（PCM 24kHz 16bit mono） |
| 音声転送 | Socket.IO `live_audio` イベント（base64 PCM） |
| 音声再生 | `LiveAudioManager` — Web Audio API (`AudioBufferSourceNode` キュー) |
| HTML `<audio>` | **使用しない**（`ttsPlayer` は廃止済み） |
| A2E統合 | `app_customer_support.py` L129-161 に既存（REST TTS用のみ） |

**したがって、フェーズ2のメイン課題は：**
1. バックエンド：LiveAPI PCMチャンクをA2Eサービスに送信し、expressionフレームをSocket.IOで転送
2. フロントエンド：Web Audio API再生キューとexpressionフレームの同期（`AudioContext.currentTime` ベース）

### 1.3 実証テスト済みコンポーネント
| コンポーネント | 実証元 | 状態 |
|------------|--------|------|
| Audio2Expression サービス | `C:\Users\hamad\audio2exp-service` | デプロイ済み・動作確認済み |
| support-base A2E統合（REST TTS用） | `app_customer_support.py` L129-161, L543-564 | 実装済み |
| LAMAvatar コンポーネント | `LAM_gpro` リポジトリ | 実証テスト済み |
| audio-sync-player | `LAM_gpro` リポジトリ | 実証テスト済み（★本フェーズで活用） |
| lam-websocket-manager | `LAM_gpro` リポジトリ | 実証テスト済み |
| Gaussian Splatモデル | `concierge.zip` (4.09MB) | 作成済み |

### 1.4 対象ファイル

#### 移植元（LAM_gpro リポジトリ）
```
LAM_gpro/gourmet-sp/
├── src/components/LAMAvatar.astro          → 新規追加（改修必要）
├── src/scripts/lam/audio-sync-player.ts    → 新規追加（★中心的に活用）
├── src/scripts/lam/lam-websocket-manager.ts → 新規追加（ARKit定数利用）
└── public/avatar/concierge.zip             → 新規追加
```

#### 修正対象（gourmet-sp3）
```
support-base/live_api_handler.py            → A2Eバッファリング＋expression転送（★中心課題）
src/scripts/chat/live-audio-manager.ts      → expression同期メカニズム追加（★中心課題）
src/scripts/chat/core-controller.ts         → live_expression イベント受信・適用
src/scripts/chat/concierge-controller.ts    → LAMAvatar連携
src/components/Concierge.astro              → アバターステージをLAMAvatar埋め込みに変更
```

#### バックエンド（デプロイ関連）
```
audio2exp-service/                          → 新規ディレクトリとして移植
.github/workflows/deploy-audio2exp.yml      → 新規：自動デプロイ設定
```

---

## 2. アーキテクチャ

### 2.1 全体データフロー

```
┌──────────────────────────────────────────────────────────────┐
│ フロントエンド（Astro + TypeScript）                            │
│                                                              │
│  ┌──────────────────┐    ┌───────────────────────┐           │
│  │ core-controller  │    │ LAMAvatar.astro        │           │
│  │ .ts              │    │ (Gaussian Splat        │           │
│  │                  │    │  Renderer)             │           │
│  │ Socket.IO受信:   │    │                        │           │
│  │ 'live_audio'     │    │ getExpressionData()    │           │
│  │ 'live_expression'│───→│ @60fps描画ループ        │           │
│  │                  │    │                        │           │
│  └──────┬───────────┘    └───────────────────────┘           │
│         │                                                    │
│  ┌──────▼───────────┐                                        │
│  │ LiveAudioManager │ ← expression同期タイムスタンプ管理        │
│  │ (Web Audio API)  │                                        │
│  │ PCM 24kHz再生    │                                        │
│  │ AudioContext     │                                        │
│  │ .currentTime同期 │                                        │
│  └──────────────────┘                                        │
└──────────┬───────────────────────────────────────────────────┘
           │ Socket.IO (双方向)
           │
┌──────────▼───────────────────────────────────────────────────┐
│ support-base（Cloud Run）                                     │
│                                                              │
│  ┌─────────────────────────────────────┐                     │
│  │ live_api_handler.py                 │                     │
│  │                                     │                     │
│  │ Gemini LiveAPI ←→ _receive_and_forward()                  │
│  │                    │                │                     │
│  │                    │ PCMチャンク受信  │                     │
│  │                    ▼                │                     │
│  │              A2Eバッファ            │                     │
│  │              (PCMチャンク蓄積)       │                     │
│  │                    │                │                     │
│  │          ┌─────────▼──────────┐     │                     │
│  │          │ 十分な長さに達したら │     │                     │
│  │          │ A2Eサービスに送信   │     │                     │
│  │          └─────────┬──────────┘     │                     │
│  │                    │                │                     │
│  │          ┌─────────▼──────────┐     │                     │
│  │          │ emit('live_audio') │     │                     │
│  │          │ emit('live_expression') │  │                     │
│  │          └────────────────────┘     │                     │
│  └─────────────────────────────────────┘                     │
│                    │                                         │
└────────────────────┼─────────────────────────────────────────┘
                     │ POST /api/audio2expression
                     ▼
┌──────────────────────────────────────────────────────────────┐
│ audio2exp-service（Cloud Run / 別サービス）                     │
│                                                              │
│  入力: PCM audio (base64) or MP3                              │
│  処理: Audio → ARKit 52 Blendshape Expression                 │
│  出力: { names: string[52],                                   │
│         frames: [{weights: float[52]}],                      │
│         frame_rate: 30 }                                     │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 同期メカニズム（LiveAPI PCMストリーミング）

**課題：** LiveAPIの音声はPCMチャンクとして非同期に到着する。HTML `<audio>` の `currentTime` は使えない。

**解決策：** `AudioContext.currentTime` + 再生オフセット追跡

```
時間軸 →
─────────────────────────────────────────────────────────

PCMチャンク到着:  [chunk1][chunk2][chunk3]...[chunkN]
                  │       │       │
                  ▼       ▼       ▼
Web Audio再生:    ┣━━━━━━━┫━━━━━━━┫━━━━━━━┫─────────────
                  ↑ startTime      ↑ nextPlayTime
                  │
                  scheduleTime = AudioContext.currentTime

Expression到着:   [expr_batch_1]        [expr_batch_2]
(A2Eバッチ応答)    │                     │
                  ▼                     ▼
                  frameBuffer に追加     frameBuffer に追加

同期計算:
  playbackOffset = AudioContext.currentTime - firstChunkStartTime
  frameIndex = Math.floor(playbackOffset * frameRate)
  → frameBuffer[frameIndex] の ExpressionData を適用
```

**バッファリング戦略（バックエンド）：**

> **⚠️ 実験で判明した重要な制約：**
> 前段の実験テストにおいて、LiveAPIのPCMチャンクを細かく分割してA2Eに送信した結果、
> 表情データの生成クオリティが著しく低下し、リップシンクの品質が問題になった。
> **最低でも文節単位（1〜3秒程度）の音声長がA2Eの表情データ抽出には必要。**
> 短すぎる音声断片では、口の開閉パターンが不自然になり、ほぼ意味のない表情データが生成される。

```
バッファリング方針（文節単位）:

  ❌ NG: 固定長バッファ（0.5秒ごと等）
     → チャンクが細かすぎてA2E品質が劣化する（実験で実証済み）

  ✅ OK: ターン単位バッファ（turn_complete まで蓄積してから一括送信）
     → 文単位の音声をA2Eに渡すため、表情データの品質が安定する
     → レイテンシは増えるが、品質を優先する

  ✅ OK（改善案）: output_transcription ベースの文節検出
     → AI transcriptに句読点（。、？、！）が出現したタイミングでバッファをフラッシュ
     → 文節単位（1〜3秒）の自然な区切りでA2Eに送信
     → ターン単位より低レイテンシで、かつ品質を維持

  推奨: output_transcription ベースの文節検出 → フォールバックとしてターン単位
```

### 2.3 LAM_gpro の audio-sync-player.ts の活用

LAM_gpro の `AudioSyncPlayer` はまさにこのユースケースのために設計されている：

| AudioSyncPlayer の機能 | 本フェーズでの活用 |
|----------------------|------------------|
| `firstStartAbsoluteTime` 追跡 | 再生開始からの経過時間でexpression同期 |
| `getCurrentPlaybackOffset()` | expressionフレームインデックスの計算に使用 |
| `getSampleIndexForOffset(offsetMs)` | チャンク内のサブオフセット計算 |
| Int16→Float32変換 | `LiveAudioManager` と同じ処理（統合可能） |

**方針：** `LiveAudioManager` に `AudioSyncPlayer` の同期機能を統合する。

---

## 3. バックエンド：live_api_handler.py の修正（★中心課題）

### 3.1 A2Eバッファリング機構の追加

> **⚠️ 設計上の最重要制約：文節単位バッファリング**
>
> 細かいチャンク単位（0.5秒等）でA2Eに送信すると表情データの品質が著しく劣化する（実験実証済み）。
> バッファリングは **文節単位（1〜3秒）以上** を確保すること。
>
> **推奨方式：** `output_transcription` の句読点検出をトリガーにバッファをフラッシュ。
> 句読点（。？！）が検出されるまでPCMチャンクを蓄積し、文節が完結した時点でA2Eに一括送信する。
> これにより、A2Eに渡す音声が自然な発話単位となり、表情データの品質が維持される。

`LiveAPISession` クラスに以下を追加：

```python
class LiveAPISession:
    def __init__(self, ...):
        # ... 既存の初期化 ...

        # ★ A2Eリップシンク用バッファ（文節単位）
        self._a2e_audio_buffer = bytearray()
        self._a2e_transcript_buffer = ""  # 対応するトランスクリプト（句読点検出用）
        self._a2e_chunk_index = 0  # expression同期用のチャンクインデックス
        # ⚠️ 固定長閾値は使わない（品質劣化の原因）
        # 代わりに output_transcription の句読点でフラッシュ判定する
```

### 3.2 _receive_and_forward() の修正

```python
async def _receive_and_forward(self, session):
    """音声データ受信時にA2Eバッファリングを追加"""
    while not self.needs_reconnect and self.is_running:
        turn = session.receive()
        async for response in turn:
            # ... 既存のtool_call, turn_complete等の処理 ...

            if response.server_content:
                sc = response.server_content

                # ★ output_transcription → A2Eフラッシュ判定
                if hasattr(sc, 'output_transcription') and sc.output_transcription:
                    text = sc.output_transcription.text
                    if text:
                        # ... 既存のtranscript転送処理 ...
                        # ★ 新規: 句読点検出でA2Eバッファフラッシュ
                        self._on_output_transcription(text)

                # 音声データ
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if hasattr(part, 'inline_data') and part.inline_data:
                            if isinstance(part.inline_data.data, bytes):
                                audio_bytes = part.inline_data.data
                                audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

                                # ★ 既存: 音声をブラウザに転送
                                self.socketio.emit('live_audio',
                                                   {'data': audio_b64},
                                                   room=self.client_sid)

                                # ★ 新規: A2Eバッファに追加（送信はしない）
                                await self._buffer_for_a2e(audio_bytes)
                                # ↑ バッファに蓄積するだけ。
                                #   フラッシュは _on_output_transcription() の
                                #   句読点検出か、turn_complete で行う。
```

### 3.3 A2Eバッファリング＆送信メソッド（新規）

```python
async def _buffer_for_a2e(self, pcm_bytes: bytes):
    """
    PCMチャンクをバッファに追加（送信タイミングは句読点検出に委譲）

    【設計根拠 — 文節単位バッファリング】
    ⚠️ 実験で判明した制約:
      短いチャンク（0.5秒等）でA2Eに送ると表情データ品質が著しく劣化する。
      最低でも文節単位（1〜3秒）の音声長が必要。

    ✅ 本メソッドはPCMをバッファに追加するだけ。
      フラッシュは _check_a2e_flush()（句読点検出時）または
      turn_complete時に行う。
    """
    if not AUDIO2EXP_SERVICE_URL:
        return

    self._a2e_audio_buffer.extend(pcm_bytes)

def _on_output_transcription(self, text: str):
    """
    output_transcription 受信時に句読点を検出してA2Eフラッシュ判定

    【呼び出し元】_receive_and_forward() 内の output_transcription 処理箇所
    """
    self._a2e_transcript_buffer += text

    # 句読点検出 → 文節完結と判断してフラッシュ
    sentence_endings = ['。', '？', '?', '！', '!', '、']  # 読点も区切りに含む
    if any(self._a2e_transcript_buffer.rstrip().endswith(e) for e in sentence_endings):
        self._flush_a2e_buffer()

def _flush_a2e_buffer(self):
    """
    A2Eバッファをフラッシュ（文節単位で送信）

    ⚠️ 最低音声長チェック:
      バッファが短すぎる場合（0.5秒未満）はフラッシュせず次の文節と結合する。
      これにより、極端に短い句（「はい。」等）でも品質を確保する。
    """
    MIN_BUFFER_BYTES = 24000  # 0.5秒分 (24kHz * 16bit = 最低限)
    if len(self._a2e_audio_buffer) < MIN_BUFFER_BYTES:
        return  # 短すぎる → 次の文節と結合

    buffer_copy = bytes(self._a2e_audio_buffer)
    self._a2e_audio_buffer.clear()
    self._a2e_transcript_buffer = ""
    chunk_index = self._a2e_chunk_index
    self._a2e_chunk_index += 1

    # 非同期でA2Eに送信（音声再生をブロックしない）
    asyncio.create_task(
        self._send_to_a2e(buffer_copy, chunk_index)
    )

async def _send_to_a2e(self, pcm_bytes: bytes, chunk_index: int):
    """
    A2EサービスにバッファされたPCMを送信し、
    expressionフレームをブラウザに転送
    """
    try:
        audio_b64 = base64.b64encode(pcm_bytes).decode('utf-8')

        # A2Eサービスに送信（PCM 24kHz 16bit monoをそのまま）
        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{AUDIO2EXP_SERVICE_URL}/api/audio2expression",
                json={
                    "audio_base64": audio_b64,
                    "session_id": self.session_id,
                    "audio_format": "pcm_24000_16bit_mono",
                    "is_start": chunk_index == 0,
                    "is_final": False,
                },
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    frames = result.get('frames', [])

                    if frames:
                        # ★ expressionフレームをブラウザに転送
                        self.socketio.emit('live_expression', {
                            'chunk_index': chunk_index,
                            'names': result.get('names', []),
                            'frames': frames,
                            'frame_rate': result.get('frame_rate', 30),
                        }, room=self.client_sid)

                        logger.debug(
                            f"[A2E] chunk {chunk_index}: {len(frames)}フレーム送信"
                        )
    except Exception as e:
        logger.warning(f"[A2E] ストリーミング送信エラー: {e}")
```

### 3.4 ターン完了時のバッファフラッシュ

```python
def _process_turn_complete(self):
    """既存のターン完了処理に追加"""
    # ... 既存の処理 ...

    # ★ A2Eバッファの残りを強制フラッシュ（文節途中でも送信）
    #   ターン完了 = 発話終了なので、残っているバッファは最後の文節。
    #   MIN_BUFFER_BYTES チェックをバイパスして必ず送信する。
    if self._a2e_audio_buffer:
        buffer_copy = bytes(self._a2e_audio_buffer)
        self._a2e_audio_buffer.clear()
        self._a2e_transcript_buffer = ""
        chunk_index = self._a2e_chunk_index
        self._a2e_chunk_index += 1
        asyncio.create_task(
            self._send_to_a2e(buffer_copy, chunk_index)
        )

    # バッファインデックスをリセット（次のターン用）
    self._a2e_chunk_index = 0
```

### 3.5 ショップ説明時の対応

`_receive_shop_description()` でも同じ `_buffer_for_a2e()` を呼び出す：

```python
async def _receive_shop_description(self, session, shop_number: int):
    """既存の音声転送箇所にA2Eバッファリングを追加"""
    # ... 既存のturn受信ループ ...

    # 音声データ（既存部分に追加）
    if sc.model_turn:
        for part in sc.model_turn.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                if isinstance(part.inline_data.data, bytes):
                    audio_bytes = part.inline_data.data
                    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
                    self.socketio.emit('live_audio',
                                       {'data': audio_b64},
                                       room=self.client_sid)
                    # ★ 追加
                    await self._buffer_for_a2e(audio_bytes)
```

### 3.6 A2Eサービスの入力形式拡張

現在の `get_expression_frames()` はMP3入力を想定している。LiveAPIのPCM入力に対応するため、audio2exp-serviceで `audio_format: "pcm_24000_16bit_mono"` を受け付けるよう拡張する。

```python
# audio2exp-service/app.py に追加
# 既存のMP3入力に加えて、PCM入力も受け付ける
if audio_format == "pcm_24000_16bit_mono":
    # PCMを直接処理（pydub変換不要）
    audio_data = base64.b64decode(audio_base64)
    # 24kHz 16bit mono PCM → numpy array
    samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
    # ... A2E推論処理 ...
```

---

## 4. フロントエンド：LiveAudioManager の拡張（★中心課題）

### 4.1 expression同期機能の追加

`live-audio-manager.ts` に `AudioSyncPlayer` の同期メカニズムを統合：

```typescript
export class LiveAudioManager {
    // ... 既存のフィールド ...

    // ★ Expression同期用（AudioSyncPlayerから移植）
    private firstChunkStartTime: number = 0;  // 最初のチャンク再生開始時の AudioContext.currentTime
    private isFirstChunk: boolean = true;
    private expressionFrameBuffer: ExpressionData[] = [];
    private expressionFrameRate: number = 30;
    private expressionNames: string[] = [];

    // ★ 再生オフセット追跡
    getCurrentPlaybackOffset(): number {
        if (!this.audioContext || this.firstChunkStartTime === 0) return 0;
        return (this.audioContext.currentTime - this.firstChunkStartTime) * 1000; // ms
    }

    // ★ 現在のexpressionフレームを取得（LAMAvatarの描画ループから呼ばれる）
    getCurrentExpressionFrame(): ExpressionData | null {
        if (this.expressionFrameBuffer.length === 0) return null;
        if (!this.isAiSpeaking) return null;

        const offsetMs = this.getCurrentPlaybackOffset();
        const frameIndex = Math.floor((offsetMs / 1000) * this.expressionFrameRate);

        if (frameIndex < 0 || frameIndex >= this.expressionFrameBuffer.length) {
            return null;
        }

        return this.expressionFrameBuffer[frameIndex];
    }
```

### 4.2 playPcmAudio() の拡張

```typescript
playPcmAudio(pcmBase64: string): void {
    if (!this.audioContext) return;

    const pcmBytes = base64ToArrayBuffer(pcmBase64);
    const int16 = new Int16Array(pcmBytes);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / 32768.0;
    }

    const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
    buffer.copyToChannel(float32, 0);

    this.playbackQueue.push(buffer);

    // ★ 最初のチャンクの再生開始時刻を記録
    if (this.isFirstChunk) {
        this.isFirstChunk = false;
        // _processPlaybackQueue で startTime を記録
    }

    this._processPlaybackQueue();
}

private _processPlaybackQueue(): void {
    if (this.isPlaying || this.playbackQueue.length === 0 || !this.audioContext) return;

    this.isPlaying = true;
    const buffer = this.playbackQueue.shift()!;

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioContext.destination);

    const now = this.audioContext.currentTime;
    const startTime = Math.max(now, this.nextPlayTime);
    source.start(startTime);

    // ★ 最初のチャンクの再生開始時刻を記録
    if (this.firstChunkStartTime === 0) {
        this.firstChunkStartTime = startTime;
    }

    this.nextPlayTime = startTime + buffer.duration;

    source.onended = () => {
        this.isPlaying = false;
        this._processPlaybackQueue();
    };
}
```

### 4.3 expressionフレーム受信メソッド（新規）

```typescript
// Socket.IOの 'live_expression' イベントから呼ばれる
onExpressionReceived(data: {
    chunk_index: number;
    names: string[];
    frames: Array<{ weights: number[] }>;
    frame_rate: number;
}): void {
    this.expressionNames = data.names;
    this.expressionFrameRate = data.frame_rate;

    // フレームをバッファに追加（chunk_indexに基づく位置に配置）
    const newFrames = data.frames.map(frame => ({
        names: data.names,
        weights: frame.weights
    }));

    this.expressionFrameBuffer.push(...newFrames);
}
```

### 4.4 リセット処理の拡張

```typescript
onAiResponseStarted(): void {
    this.isAiSpeaking = true;
    // ★ expression状態をリセット
    this.expressionFrameBuffer = [];
    this.firstChunkStartTime = 0;
    this.isFirstChunk = true;
}

onAiResponseEnded(): void {
    this.isAiSpeaking = false;
}

clearPlaybackQueue(): void {
    this.playbackQueue = [];
    this.isPlaying = false;
    this.nextPlayTime = 0;
    // ★ expression状態もクリア
    this.expressionFrameBuffer = [];
    this.firstChunkStartTime = 0;
    this.isFirstChunk = true;
}
```

---

## 5. フロントエンド：core-controller.ts の修正

### 5.1 live_expression イベントの受信

`initSocket()` 内に追加：

```typescript
// ★ A2E expressionフレーム受信
this.socket.on('live_expression', (data: any) => {
    if (!this.isLiveMode) return;
    this.liveAudioManager.onExpressionReceived(data);
});
```

### 5.2 LAMAvatar連携

`LAMAvatar.astro` の `getExpressionData()` は既存のexternalTtsPlayerモード（`ttsPlayer.currentTime` 同期）を前提としている。LiveAPIモードでは `LiveAudioManager.getCurrentExpressionFrame()` から直接取得する必要がある。

**2つの方法：**

#### 方法A: LAMAvatarのgetExpressionData()をLiveAudioManager対応に改修

```typescript
// LAMAvatar.astro 内
function getExpressionData(): ExpressionData | null {
    // LiveAPI モード: LiveAudioManager から直接取得
    const liveManager = (window as any).liveAudioManagerRef;
    if (liveManager) {
        return liveManager.getCurrentExpressionFrame();
    }

    // 旧モード: externalTtsPlayer.currentTime 同期（フォールバック）
    // ... 既存のロジック ...
}
```

#### 方法B: LiveAudioManagerがLAMAvatarのqueueExpressionFrames()を呼ぶ（既存APIを活用）

```typescript
// core-controller.ts
this.socket.on('live_expression', (data: any) => {
    if (!this.isLiveMode) return;
    this.liveAudioManager.onExpressionReceived(data);

    // LAMAvatarにもフレームをキュー
    const lamController = (window as any).lamAvatarController;
    if (lamController) {
        const frames = data.frames.map((f: any) => ({
            names: data.names,
            weights: f.weights
        }));
        lamController.queueExpressionFrames(frames, data.frame_rate);
    }
});
```

**推奨：方法A** — LAMAvatarの同期ロジックを `LiveAudioManager` に統一することで、
`AudioContext.currentTime` ベースの正確な同期が得られる。方法Bは既存の `ttsPlayer.currentTime` 同期ロジックを使い回すが、LiveAPIモードではttsPlayerが存在しないため動作しない。

---

## 6. フロントエンド：LAMAvatar.astro の改修

### 6.1 External TTS Player モードから LiveAudioManager モードへ

LAM_gproの元コードは `setExternalTtsPlayer(player: HTMLAudioElement)` で HTML `<audio>` 要素をバインドし、`player.currentTime` で同期する。

gourmet-sp3では `LiveAudioManager` の `getCurrentPlaybackOffset()` で同期するよう改修：

```typescript
// LAMAvatar.astro 内のインターフェース変更

interface LAMAvatarController {
    // 旧: setExternalTtsPlayer(player: HTMLAudioElement): void;
    // 新:
    setLiveAudioManager(manager: LiveAudioManager): void;
    queueExpressionFrames(frames: ExpressionData[], frameRate: number): void;
    clearFrameBuffer(): void;
}
```

### 6.2 getExpressionData() の改修

```typescript
// 変更前（LAM_gpro）:
function getExpressionData(): ExpressionData | null {
    if (!ttsActive || !externalTtsPlayer) return null;
    const currentTimeMs = externalTtsPlayer.currentTime * 1000;
    const frameIndex = Math.floor((currentTimeMs / 1000) * frameRate);
    return frameBuffer[frameIndex];
}

// 変更後（gourmet-sp3）:
function getExpressionData(): ExpressionData | null {
    if (!liveAudioManager) return null;
    return liveAudioManager.getCurrentExpressionFrame();
    // フェードイン/アウトは getCurrentExpressionFrame() 内で処理
}
```

### 6.3 Concierge.astro の変更

```diff
+import LAMAvatar from './LAMAvatar.astro';

 <div class="avatar-stage" id="avatarStage">
   <div class="avatar-container">
-    <img id="avatarImage" src="/images/avatar-anime.png" alt="AI Avatar" class="avatar-img" />
+    <LAMAvatar />
   </div>
 </div>
```

---

## 7. フロントエンド：concierge-controller.ts の修正

### 7.1 LAMAvatar初期化連携

```typescript
protected async init() {
    await super.init();
    // ... 既存のコンシェルジュ固有初期化 ...

    // ★ LAMAvatarにLiveAudioManagerを接続
    this.linkLamAvatar();
}

private linkLamAvatar() {
    const lamController = (window as any).lamAvatarController;
    if (lamController) {
        lamController.setLiveAudioManager(this.liveAudioManager);
    } else {
        setTimeout(() => this.linkLamAvatar(), 2000);
    }
}
```

### 7.2 speakTextGCP() / CSSアニメーションの削除

現在の `speakTextGCP()` オーバーライドは CSSクラス `speaking` の付け外しのみ。これを削除し、リップシンクは `LiveAudioManager` + `LAMAvatar` の自動同期に委譲する。

```typescript
// 削除: speakTextGCP() オーバーライド
// 削除: stopAvatarAnimation()
// これらはLiveAPIモードでは呼ばれない
```

### 7.3 stopAllActivities() の修正

```typescript
protected stopAllActivities() {
    super.stopAllActivities();
    // ★ CSSアニメーション停止 → LAMAvatarフレームバッファクリア
    const lamController = (window as any).lamAvatarController;
    if (lamController) {
        lamController.clearFrameBuffer();
    }
}
```

---

## 8. audio2exp-service の移植とデプロイ

### 8.1 ディレクトリ構成

```
gourmet-sp3/
├── audio2exp-service/              ← C:\Users\hamad\audio2exp-service から移植
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py                      ← メインFlaskアプリ
│   ├── audio2expression.py         ← A2Eコア処理
│   ├── models/                     ← 推論モデルファイル
│   └── ...
└── .github/workflows/
    ├── deploy-cloud-run.yml        ← 既存（support-base用）+ AUDIO2EXP_SERVICE_URL追加
    └── deploy-audio2exp.yml        ← 新規（audio2exp-service用）
```

### 8.2 GitHub Actions：deploy-audio2exp.yml

```yaml
name: Deploy Audio2Exp to Cloud Run

on:
  push:
    branches: [main, 'claude/**']
    paths:
      - 'audio2exp-service/**'
      - '.github/workflows/deploy-audio2exp.yml'
  workflow_dispatch:

env:
  PROJECT_ID: ai-meet-486502
  SERVICE_NAME: audio2exp-service
  REGION: us-central1
  GAR_LOCATION: us-central1-docker.pkg.dev

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write

    steps:
      - uses: actions/checkout@v4

      - uses: google-github-actions/auth@v2
        with:
          credentials_json: '${{ secrets.GCP_SA_KEY }}'

      - uses: google-github-actions/setup-gcloud@v2

      - run: gcloud auth configure-docker ${{ env.REGION }}-docker.pkg.dev --quiet

      - name: Build and push Docker image
        run: |
          IMAGE="${{ env.GAR_LOCATION }}/${{ env.PROJECT_ID }}/${{ env.SERVICE_NAME }}/${{ env.SERVICE_NAME }}:${{ github.sha }}"
          docker build -t $IMAGE ./audio2exp-service
          docker push $IMAGE
          echo "IMAGE=$IMAGE" >> $GITHUB_ENV

      - name: Deploy to Cloud Run
        run: |
          gcloud run deploy ${{ env.SERVICE_NAME }} \
            --image=${{ env.IMAGE }} \
            --region=${{ env.REGION }} \
            --project=${{ env.PROJECT_ID }} \
            --platform=managed \
            --allow-unauthenticated \
            --port=8080 \
            --memory=2Gi \
            --cpu=2 \
            --min-instances=1 \
            --max-instances=3 \
            --timeout=60
```

### 8.3 PCM入力対応の拡張

`audio2exp-service` に `audio_format: "pcm_24000_16bit_mono"` を受け付けるよう拡張が必要。
現在はMP3入力のみ対応のため、PCMの直接処理パスを追加する。

---

## 9. npmパッケージ追加

```bash
npm install gaussian-splat-renderer-for-lam
```

---

## 10. 実装順序

### Step 1: audio2exp-service の移植・デプロイ
1. `C:\Users\hamad\audio2exp-service` を `gourmet-sp3/audio2exp-service/` にコピー
2. PCM入力対応を追加（`audio_format: "pcm_24000_16bit_mono"`）
3. `.github/workflows/deploy-audio2exp.yml` を作成
4. GitHub Secrets に `AUDIO2EXP_SERVICE_URL` を設定

### Step 2: バックエンド — live_api_handler.py の修正（★最重要）
1. `LiveAPISession` に A2E文節単位バッファリング機構を追加
2. `_receive_and_forward()` に `_buffer_for_a2e()` 呼び出しを追加
3. `_on_output_transcription()` による句読点検出フラッシュを実装
4. `_receive_shop_description()` にも同様の追加
5. ターン完了時のバッファ強制フラッシュを追加
6. `live_expression` Socket.IOイベントの送信

### Step 3: フロントエンド — LiveAudioManager の拡張（★最重要）
1. expression同期フィールドの追加
2. `getCurrentPlaybackOffset()` メソッドの追加
3. `getCurrentExpressionFrame()` メソッドの追加
4. `onExpressionReceived()` メソッドの追加
5. `playPcmAudio()` に `firstChunkStartTime` 記録を追加
6. リセット処理の拡張

### Step 4: フロントエンド — LAMAvatar移植・改修
1. LAM_gpro から4ファイルを移植
2. `LAMAvatar.astro` を `LiveAudioManager` 連携に改修
3. `npm install gaussian-splat-renderer-for-lam`

### Step 5: フロントエンド — コントローラー修正
1. `core-controller.ts` に `live_expression` イベント受信を追加
2. `concierge-controller.ts` に `linkLamAvatar()` を追加
3. CSSアニメーション関連コードを削除
4. `Concierge.astro` にLAMAvatarを埋め込み

### Step 6: テスト・検証
1. ローカルでGaussian Splatモデルのロード確認
2. LiveAPI音声 + expression同期の確認
3. ショップ説明時のリップシンク確認
4. 割り込み（interrupted）時のリセット確認
5. 再接続時の状態リセット確認
6. モバイル端末でのパフォーマンス確認

---

## 11. 検証チェックリスト

### 11.1 バックエンド
- [ ] audio2exp-service がPCM入力（24kHz 16bit mono）を受け付ける
- [ ] `live_api_handler.py` でPCMチャンクが文節単位でバッファリングされる
- [ ] `output_transcription` の句読点（。？！）検出でA2Eバッファがフラッシュされる
- [ ] 短すぎるバッファ（0.5秒未満）は次の文節と結合される
- [ ] `live_expression` Socket.IOイベントが正しく送信される
- [ ] ターン完了時にバッファが強制フラッシュされる
- [ ] A2E送信が音声再生をブロックしない（非同期処理）
- [ ] A2Eに送信される音声が文節単位（1〜3秒）であることをログで確認

### 11.2 フロントエンド
- [ ] Gaussian Splatモデル（`concierge.zip`）が正常にロードされる
- [ ] 3Dアバターが表示される（WebGL対応ブラウザ）
- [ ] WebGL非対応時に2Dフォールバック画像が表示される
- [ ] `live_expression` イベント受信でexpressionフレームがバッファされる
- [ ] `AudioContext.currentTime` ベースの同期が正しく動作する
- [ ] LiveAPI音声再生中にリップシンクが動作する
- [ ] 音声とリップシンクが同期している（目視確認）
- [ ] 割り込み（interrupted）時にアバターが静止状態に戻る
- [ ] ターン完了時に正しくリセットされる
- [ ] ショップ説明時のリップシンクが動作する

### 11.3 デプロイ
- [ ] audio2exp-service の自動デプロイが動作する
- [ ] min-instances=1 によりコールドスタートが回避されている
- [ ] support-base の `AUDIO2EXP_SERVICE_URL` が設定されている

---

## 12. 既知の制限事項

### 12.1 A2Eレイテンシと品質のトレードオフ

> **⚠️ 実験で判明した最重要制約：A2E入力音声の最低長**
> 短いチャンク（0.5秒等）でA2Eに送信すると表情データの品質が著しく劣化する。
> 最低でも文節単位（1〜3秒）の音声長が必要（実験実証済み）。

- 文節単位バッファリング（1〜3秒）+ A2E推論（0.5-1秒）= 最大2〜4秒の遅延
- expressionフレームは音声より遅れて到着するため、初回文節はリップシンクなし
- **品質 > レイテンシ** を優先する設計判断
  - 短くすれば遅延は減るが、A2E品質が劣化してリップシンクが不自然になる
  - 不自然なリップシンクは「無い方がマシ」（実験で確認済み）
- **対策：**
  - 初回文節のリップシンク遅延は許容する（アバターは静止状態で自然に見える）
  - 2文節目以降は前の文節のA2E処理が完了しているため、ほぼリアルタイムに近づく
  - `output_transcription` の句読点検出でバッファリング単位を動的に最適化

### 12.2 Gaussian Splatのパフォーマンス
- WebGLが必要（非対応ブラウザではフォールバック画像）
- モバイル端末ではGPU負荷が高い可能性
- モデルサイズ 4.09MB のダウンロード（初回ロード）

### 12.3 A2Eサービスの同時接続
- `max-instances=3` で3セッション同時対応
- 超過時はA2Eのみ失敗（音声再生は影響なし、リップシンクだけ無効化）

---

## 13. ファイル変更サマリ

| ファイル | アクション | 変更規模 | 重要度 |
|---------|----------|---------|--------|
| `support-base/live_api_handler.py` | **修正** | ~80行追加 | ★★★ |
| `src/scripts/chat/live-audio-manager.ts` | **修正** | ~60行追加 | ★★★ |
| `src/components/LAMAvatar.astro` | **新規移植+改修** | ~500行（移植）+ 改修 | ★★ |
| `src/scripts/chat/core-controller.ts` | **修正** | ~10行追加 | ★★ |
| `src/scripts/chat/concierge-controller.ts` | **修正** | ~30行変更 | ★★ |
| `src/components/Concierge.astro` | **修正** | ~10行変更 | ★ |
| `audio2exp-service/*` | **新規移植** | ディレクトリごとコピー | ★ |
| `.github/workflows/deploy-audio2exp.yml` | **新規作成** | ~50行 | ★ |
| `src/scripts/lam/audio-sync-player.ts` | **新規移植** | ~220行（参考用） | - |
| `src/scripts/lam/lam-websocket-manager.ts` | **新規移植** | ~400行（ARKit定数利用） | - |
| `public/avatar/concierge.zip` | **新規追加** | 4.09MB | ★ |
| `package.json` | **修正** | 依存追加 | ★ |
