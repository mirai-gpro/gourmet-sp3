# ショップ読み上げ A2E同期修正仕様書 — 修正案C（A2E先行＋sleep）

**作成日**: 2026-03-19
**目的**: ショップ説明読み上げ時のリップシンク同期崩壊を修正する
**方針**: 公式デモと同等の「A2Eデータが先に到着 → 音声再生開始」パターン

---

## 1. 問題の根本原因（doc/11より再掲）

### 1.1 正常パス（通常会話 `_receive_and_forward`）
```
live_audio → playPcmAudio() → firstChunkStartTime設定
↕ 同時進行
_buffer_for_a2e → A2E → live_expression → バッファ追加
```
- 音声とExpressionがインターリーブで到着 → 同期成立

### 1.2 異常パス（ショップ読み上げ）
```
LLM「お探ししますね」→ firstChunkStartTime設定、isAiSpeaking=true
  ↓
REST検索（数秒の空白）→ currentTimeだけ進む
  ↓
キャッシュ音声「お待たせしました」→ リセットなし
  ↓
1軒目ストリーミング → リセットなし
  ↓
2軒目〜3軒目 collected → リセットなし
```
**結果**: `frameIndex` が実際のバッファ位置を大幅超過 → 全セグメントで恒常的にズレ

---

## 2. 修正案Cの設計思想

### 2.1 公式デモとの対応

| 公式デモ（Gradio） | 修正案C |
|---|---|
| 推論完了 → `bsData.json`が全フレーム揃う | A2E応答の`await`完了 → expressionバッファに全フレーム到着 |
| `window.loadAudio()` → 音声ロード | `live_audio` 送信前の状態 |
| `window.start()` → 再生+レンダリング同時開始 | `live_audio` 送信 → `playPcmAudio()` → `firstChunkStartTime` 設定 |

### 2.2 核心: 「A2E先行」の意味

**音声をフロントに送信する前に、対応するExpressionデータをフロントに到着させる。**

```
修正前（現行）:
  live_audio送信 ──→ _buffer_for_a2e ──→ A2E応答 ──→ live_expression送信
  （音声が先）         （A2Eが後追い）

修正後（案C）:
  A2Eに送信 ──→ A2E応答await ──→ live_expression送信 ──→ sleep ──→ live_audio送信
  （A2Eが先）                      （expressionが先着）     （確実な到着待ち）
```

### 2.3 sleepの役割

`live_expression` と `live_audio` は同じSocket.IOチャネルだが、
フロントエンドの処理（バッファ追加 vs AudioContext再生スケジュール）は非同期。
**sleepはExpressionデータがフロントエンドのバッファに確実に格納される時間マージン。**

---

## 3. 修正対象と変更内容

### 3.1 バックエンド: `live_api_handler.py`

#### 3.1.1 `_emit_cached_audio()` の修正

**現行コード** (L1092-1114):
```python
async def _emit_cached_audio(self, pcm_data):
    self.socketio.emit('live_expression_reset', ...)
    for chunk in pcm_data:             # ← 音声を即送信
        self.socketio.emit('live_audio', ...)
        self._buffer_for_a2e(chunk)    # ← A2Eは後追い
    await self._flush_a2e_buffer(...)  # ← Expression送信はさらに後
```

**修正後**:
```python
async def _emit_cached_audio(self, pcm_data):
    if not pcm_data:
        return

    # 1. expressionリセット → フロントのバッファクリア
    self.socketio.emit('live_expression_reset', room=self.client_sid)

    # 2. A2E先行: 全音声を一括でA2Eに送信し、Expressionを先にフロントへ届ける
    await self._send_a2e_ahead(pcm_data)

    # 3. sleep: Expressionがフロントのバッファに格納される時間マージン
    await asyncio.sleep(0.05)  # 50ms

    # 4. 音声送信: Expressionが既にバッファにある状態で再生開始
    CHUNK_SIZE = 4800
    for i in range(0, len(pcm_data), CHUNK_SIZE):
        chunk = pcm_data[i:i + CHUNK_SIZE]
        audio_b64 = base64.b64encode(chunk).decode('utf-8')
        self.socketio.emit('live_audio', {'data': audio_b64},
                           room=self.client_sid)
```

#### 3.1.2 `_emit_collected_shop()` の修正

**現行コード** (L1011-1029):
```python
async def _emit_collected_shop(self, audio_chunks, transcript, shop_number):
    self.socketio.emit('live_expression_reset', ...)
    for chunk in audio_chunks:
        self.socketio.emit('live_audio', ...)    # ← 音声を即送信
        self._buffer_for_a2e(chunk)              # ← A2Eは後追い
    await self._flush_a2e_buffer(...)
```

**修正後**:
```python
async def _emit_collected_shop(self, audio_chunks, transcript, shop_number):
    if transcript:
        logger.info(f"[ShopDesc] ショップ{shop_number}: {transcript}")
        self._add_to_history("ai", transcript)

    # 1. expressionリセット → フロントのバッファクリア
    self.socketio.emit('live_expression_reset', room=self.client_sid)

    # 2. A2E先行: 全音声チャンクを結合してA2Eに一括送信
    all_pcm = b''.join(audio_chunks)
    await self._send_a2e_ahead(all_pcm)

    # 3. sleep: Expressionがフロントのバッファに格納される時間マージン
    await asyncio.sleep(0.05)  # 50ms

    # 4. 音声送信: Expressionが既にバッファにある状態で再生開始
    for chunk in audio_chunks:
        audio_b64 = base64.b64encode(chunk).decode('utf-8')
        self.socketio.emit('live_audio', {'data': audio_b64},
                           room=self.client_sid)
```

#### 3.1.3 新規メソッド `_send_a2e_ahead()` の追加

```python
async def _send_a2e_ahead(self, pcm_data: bytes):
    """A2E先行送信: 音声をフロントに送る前にExpressionを先に届ける

    公式デモの「推論完了 → 全データ揃ってから再生」パターンの再現。
    _buffer_for_a2e() + _flush_a2e_buffer() の一括実行版。
    """
    # chunk_indexリセット（新セグメント）
    self._a2e_chunk_index = 0
    self._a2e_audio_buffer = bytearray()

    # 一括でA2Eに送信（is_start=True, is_final=True）
    await self._send_to_a2e(pcm_data, chunk_index=0, is_final=True)

    # chunk_indexを1に進める（次のフラッシュがis_start=Falseになるように）
    self._a2e_chunk_index = 1
```

#### 3.1.4 `_stream_single_shop()` の修正（1軒目）

1軒目はLiveAPIからストリーミングで音声が到着するため、
`_emit_cached_audio` / `_emit_collected_shop` のような一括先行は不可能。

**1軒目は現行の同期メカニズムを維持する。** ただし以下の変更を加える:

```python
async def _stream_single_shop(self, shop, shop_number, total):
    ...
    async with self.client.aio.live.connect(...) as session:
        # A2E: 新音声セグメント開始前にexpressionリセット
        self.socketio.emit('live_expression_reset', room=self.client_sid)
        # ★ 追加: リセット後にsleep（フロントのバッファクリアを確実に待つ）
        await asyncio.sleep(0.03)  # 30ms

        ...
        await self._receive_shop_description(session, shop_number)
```

1軒目のリップシンクは `_receive_and_forward` と同じインターリーブ方式。
`live_expression_reset` 直後の `live_audio` でフロントの `onAiResponseStarted()` → リセットが走るので、
sleepは安全マージン程度でよい。

### 3.2 フロントエンド: 変更なし

フロントエンドの同期メカニズム（`firstChunkStartTime` + `expressionFrameBuffer` + `getCurrentExpressionFrame()`）はそのまま使う。

`live_expression_reset` → `onAiResponseEnded()` → `isAiSpeaking = false` → 次の `live_audio` で `onAiResponseStarted()` → リセット、の既存フローが正しく動く。

---

## 4. 修正後のタイムライン（ショップ検索全体）

```
LLM「お探ししますね」→ tool_call: search_shops
  ↓
shop_search_start送信
  ↓
(0.5秒後) キャッシュ「お店をお探ししますね」
  ├── live_expression_reset          ← バッファクリア
  ├── _send_a2e_ahead(pcm)          ← Expression先行到着
  ├── await sleep(50ms)              ← 到着マージン
  └── live_audio × N chunks         ← 再生開始、firstChunkStartTime設定
  ↓
(6.5秒後) キャッシュ「只今、確認中です」※検索が早ければキャンセル
  ├── live_expression_reset
  ├── _send_a2e_ahead(pcm)
  ├── await sleep(50ms)
  └── live_audio × N chunks
  ↓
検索完了 → shop_search_result送信
  ↓
キャッシュ「お待たせしました」
  ├── live_expression_reset
  ├── _send_a2e_ahead(pcm)
  ├── await sleep(50ms)
  └── live_audio × N chunks
  ↓
1軒目 _stream_single_shop()
  ├── live_expression_reset
  ├── await sleep(30ms)
  └── LiveAPIストリーム受信
       ├── live_audio + _buffer_for_a2e ← インターリーブ方式（正常パス同等）
       ├── _on_output_transcription → 句読点フラッシュ
       └── turn_complete → _flush_a2e_buffer(is_final=True)
  ↓
2軒目 _emit_collected_shop()
  ├── live_expression_reset
  ├── _send_a2e_ahead(全音声結合)
  ├── await sleep(50ms)
  └── live_audio × N chunks
  ↓
3軒目 _emit_collected_shop()  ← 同上
  ↓
live_expression_reset（全ショップ完了）
needs_reconnect = True → 通常会話に復帰
```

---

## 5. 同期が成立する理由の検証

### 5.1 キャッシュ音声（条件1〜3の確認）

| 条件 | 状態 | 根拠 |
|------|------|------|
| 条件1: firstChunkStartTime | `live_expression_reset` でリセット → 次の `live_audio` で再設定 | `onAiResponseEnded()` → `onAiResponseStarted()` |
| 条件2: フレーム数一致 | A2Eに全PCMを一括送信 → N秒 × 30フレーム | `_send_a2e_ahead()` |
| 条件3: ギャップ不在 | Expression到着 → sleep(50ms) → 即音声再生開始 | `_emit_cached_audio()` 内で連続実行 |

### 5.2 2軒目以降 collected（条件1〜3の確認）

| 条件 | 状態 | 根拠 |
|------|------|------|
| 条件1 | `live_expression_reset` → 次の `live_audio` でリセット | 同上 |
| 条件2 | 全チャンク結合 → A2Eに一括送信 → フレーム数一致 | `b''.join(audio_chunks)` |
| 条件3 | Expression到着 → sleep(50ms) → 音声再生開始 | セグメント内連続 |

### 5.3 1軒目ストリーミング（条件1〜3の確認）

| 条件 | 状態 | 根拠 |
|------|------|------|
| 条件1 | `live_expression_reset` → 次の `live_audio` でリセット | `_stream_single_shop()` 冒頭 |
| 条件2 | LiveAPIからの逐次到着 → `_buffer_for_a2e` でインターリーブ | `_receive_shop_description()` 内 |
| 条件3 | LiveAPIが連続的に音声送信 → 空白期間なし | 正常パスと同等 |

---

## 6. セグメント間のリセットフロー（詳細）

```
[セグメントA 再生中]
  ↓
live_expression_reset  ← バックエンド送信
  ↓ フロントエンド:
  onAiResponseEnded()
    isAiSpeaking = false
  ↓
_send_a2e_ahead(pcm)  ← A2E送信 + live_expression送信
  ↓ フロントエンド:
  onExpressionReceived()
    expressionFrameBuffer にフレーム追加
    ★ ただし firstChunkStartTime = 0 のため
      getCurrentExpressionFrame() は offsetMs=0 → frameIndex=0
      → 正しく先頭フレームを返す（まだ再生前）
  ↓
await asyncio.sleep(0.05)  ← 50ms待機
  ↓
live_audio 送信
  ↓ フロントエンド:
  onAiResponseStarted()
    isAiSpeaking was false → リセット実行
    firstChunkStartTime = 0    ← クリア済み（live_expression_resetで既にクリア）
    expressionFrameBuffer = [] ← ★ ここで問題！
  ↓
  playPcmAudio()
    firstChunkStartTime = audioContext.currentTime  ← 再設定
```

### 6.1 問題点: `onAiResponseStarted()` がExpressionバッファをクリアする

`live_expression_reset` → `onAiResponseEnded()` で `isAiSpeaking = false` にした後、
`live_audio` → `onAiResponseStarted()` で `expressionFrameBuffer = []` が実行される。

**A2E先行で追加したフレームが消えてしまう。**

### 6.2 解決策: `live_expression_reset` ハンドラの修正

`live_expression_reset` 受信時に `onAiResponseEnded()` を呼ぶのではなく、
**専用のリセット処理**を行う。

**フロントエンド修正（core-controller.ts）**:

```typescript
// 現行:
this.socket.on('live_expression_reset', () => {
    this.liveAudioManager.onAiResponseEnded();  // isAiSpeaking=false
});

// 修正後:
this.socket.on('live_expression_reset', () => {
    this.liveAudioManager.resetForNewSegment();  // 専用リセット
});
```

**フロントエンド修正（live-audio-manager.ts）** — 新規メソッド追加:

```typescript
/**
 * 新音声セグメント開始前のリセット（live_expression_reset用）
 * onAiResponseStarted() のリセットを無効化し、
 * A2E先行で追加されたExpressionフレームを保持する。
 */
resetForNewSegment(): void {
    // 再生キューをクリア（前セグメントの残音声を停止）
    this.nextPlayTime = 0;
    for (const source of this.scheduledSources) {
        try { source.stop(); } catch (_) {}
    }
    this.scheduledSources = [];

    // Expressionバッファクリア + タイムスタンプリセット
    this.expressionFrameBuffer = [];
    this.firstChunkStartTime = 0;

    // ★ isAiSpeaking = true を維持
    // → 次の live_audio で onAiResponseStarted() のリセットが走らない
    // → A2E先行で追加されたフレームが保持される
    this.isAiSpeaking = true;
}
```

### 6.3 修正後のリセットフロー（正しいフロー）

```
[セグメントA 再生中]
  ↓
live_expression_reset
  ↓ フロントエンド:
  resetForNewSegment()
    scheduledSources → 全stop（前セグメント残音声停止）
    expressionFrameBuffer = []
    firstChunkStartTime = 0
    isAiSpeaking = true  ← 維持！
  ↓
_send_a2e_ahead(pcm)  → live_expression
  ↓ フロントエンド:
  onExpressionReceived()
    expressionFrameBuffer にフレーム追加  ← 保持される！
  ↓
await asyncio.sleep(0.05)
  ↓
live_audio
  ↓ フロントエンド:
  onAiResponseStarted()
    isAiSpeaking は already true → リセット不実行！
    expressionFrameBuffer 保持！
  ↓
  playPcmAudio()
    firstChunkStartTime = audioContext.currentTime  ← 設定
    → 以降 getCurrentExpressionFrame() が正常に同期
```

---

## 7. 修正ファイル一覧

| ファイル | 修正内容 |
|---------|---------|
| `support-base/live_api_handler.py` | `_send_a2e_ahead()` 新規追加 |
| `support-base/live_api_handler.py` | `_emit_cached_audio()` — A2E先行+sleep方式に変更 |
| `support-base/live_api_handler.py` | `_emit_collected_shop()` — A2E先行+sleep方式に変更 |
| `support-base/live_api_handler.py` | `_stream_single_shop()` — reset後にsleep追加 |
| `src/scripts/chat/live-audio-manager.ts` | `resetForNewSegment()` 新規追加 |
| `src/scripts/chat/core-controller.ts` | `live_expression_reset` ハンドラ修正 |

**変更しないファイル**:
- `audio-sync-player.ts` — 変更不要
- `lam-websocket-manager.ts` — 変更不要
- `_receive_and_forward()` — 正常パスは触らない
- `_receive_shop_description()` — 1軒目のインターリーブ方式は維持

---

## 8. 定数・パラメータ

| パラメータ | 値 | 根拠 |
|-----------|-----|------|
| A2E先行後のsleep | 50ms | Socket.IO emit → フロントエンドのイベントハンドラ実行の往復遅延 |
| `_stream_single_shop` reset後のsleep | 30ms | `live_expression_reset` がフロントで処理される時間マージン |

**注意**: これらのsleep値は「Expressionの到着を保証する最小マージン」であり、
A2Eの推論遅延とは無関係（A2Eの推論は事実上ゼロレイテンシ: doc/11 §1.2）。

---

## 9. 正常パス（通常会話）への影響

**なし。** 修正対象は以下の3メソッドのみ:
- `_emit_cached_audio()` — キャッシュ音声再生時のみ使用
- `_emit_collected_shop()` — 2軒目以降のショップ説明時のみ使用
- `_stream_single_shop()` — 1軒目のショップ説明時のみ使用

`_receive_and_forward()` は一切変更しない。

フロントエンドの `resetForNewSegment()` は `live_expression_reset` イベント専用。
通常会話の `turn_complete` → `onAiResponseEnded()` のフローには影響しない。
