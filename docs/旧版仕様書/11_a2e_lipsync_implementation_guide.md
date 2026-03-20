# A2E リップシンク実装ガイド — 音声・Expression同期の必須ルール

**作成日**: 2026-03-18
**目的**: A2Eリップシンクのコード修正・新規実装時に、同期崩壊を防ぐための実装規約
**状態**: `docs/13_a2e_lipsync_comprehensive_guide.md` に統合済み（旧版）

---

## 1. A2Eの基本特性 — 公式リポジトリからの事実

> 参照: https://github.com/aigc3d/LAM_Audio2Expression

### 1.1 入出力の対応関係（`engines/infer.py`）

```python
# 公式コード: inference_streaming_audio.py
gap = 16000  # 1秒分（16kHz）
for i in range(input_num):
    output, context = infer.infer_streaming_audio(
        audio[i*gap:(i+1)*gap], sample_rate, context
    )
    all_exp.append(output['expression'])
```

| 入力 | 出力 | 根拠 |
|------|------|------|
| 16,000サンプル（1秒 @ 16kHz） | 30フレーム（1秒 @ 30fps） | `frame_length = math.ceil(audio.shape[0] / ssr * 30)` |
| N秒の音声 | N × 30 フレーム | 線形対応。例外なし |

**A2Eの出力フレーム数は、入力音声の長さに対して厳密に決定論的。**

### 1.2 推論レイテンシ

A2Eの推論は**事実上ゼロレイテンシ**（公式デモで確認済み）。
「A2Eの処理時間が遅延の原因」と推測してはならない。
同期がズレている場合、原因は**常にアプリケーション側のコードロジック**にある。

### 1.3 ストリーミングcontext（`engines/infer.py`）

```python
context = {
    'previous_audio': ...,       # 前チャンクの音声波形（オーバーラップ用）
    'previous_expression': ...,  # 前チャンクの出力blendshape
    'previous_volume': ...,      # 前チャンクの音量（無音判定用）
    'is_initial_input': False    # 初回フラグ
}
```

- `is_start=True` で context がリセットされ、新しい音声セグメントとして処理される
- `is_start=False` で前チャンクとの連続性が保持される（スライディングウィンドウ）
- **意味的に独立した音声セグメント**（別のショップ説明、キャッシュ音声 等）は `is_start=True` で切る

### 1.4 後処理パイプライン（`models/utils.py`）

A2Eモデルの生出力に対して、公式コードは以下の後処理を適用する:

1. `smooth_mouth_movements()` — 無音区間の口パクパク抑制
2. `apply_frame_blending()` — チャンク境界の線形補間（初回3フレーム、後続5フレーム）
3. `apply_savitzky_golay_smoothing()` — 時間軸の多項式平滑化（window=5, polyorder=2）
4. `symmetrize_blendshapes()` — 左右20ペアの対称化
5. `apply_random_eye_blinks_context()` — 手続き的まばたき生成（A2EはeyeBlinkをゼロ出力）
6. `apply_random_brow_movement()` — 音声RMSに基づく眉の動き生成

**これらは全てA2Eサービス内部で完結する。アプリケーション側で再実装してはならない。**

---

## 2. 本プロジェクトのリップシンク同期メカニズム

### 2.1 全体フロー

```
バックエンド                              フロントエンド
──────────                              ──────────────
                                         onAiResponseStarted()
                                           isAiSpeaking=false → true に遷移時のみ:
                                             firstChunkStartTime = 0
                                             expressionFrameBuffer = []

socketio.emit('live_audio')          →   playPcmAudio()
                                           firstChunkStartTime = audioContext.currentTime
                                           （0の場合のみ設定 = 最初の1回のみ）

_buffer_for_a2e() → A2Eサービス
socketio.emit('live_expression')     →   onExpressionReceived()
                                           expressionFrameBuffer に追加

                                         レンダリングループ（毎フレーム）:
                                           getCurrentExpressionFrame()
                                             offsetMs = (currentTime - firstChunkStartTime) * 1000
                                             frameIndex = floor(offsetMs / 1000 * 30)
                                             return expressionFrameBuffer[frameIndex]

socketio.emit('turn_complete')       →   onAiResponseEnded()
                                           isAiSpeaking = false
                                           → 次の live_audio で全リセット
```

### 2.2 同期が成立する条件（3つ全て必須）

| 条件 | 説明 |
|------|------|
| **条件1: 時間ベースの一致** | `firstChunkStartTime` が音声再生開始時刻と一致していること |
| **条件2: フレーム数の一致** | `expressionFrameBuffer` のフレーム数が、再生済み音声の長さ（秒）× 30 と一致していること |
| **条件3: ギャップの不在** | `firstChunkStartTime` から現在時刻までの間に、音声もフレームも生成されない「空白期間」が存在しないこと |

**条件3が最も重要。** `audioContext.currentTime` は音声が鳴っていなくても進み続けるリアルタイムクロックであるため、空白期間があると `frameIndex` だけが進み、バッファとの対応が崩壊する。

---

## 3. 正常パス vs 異常パス — 実際に起きた不具合の解析

### 3.1 正常パス: 通常会話（`_receive_and_forward`）

**なぜ同期するか:**
1. LiveAPIが音声を逐次ストリーミング → 音声とExpressionが自然にインターリーブ
2. `turn_complete` でフロントエンドがリセット → 次ターンは白紙から開始
3. 1ターン内に空白期間が発生しない（LiveAPIが連続的に音声を送る）
4. **3条件が全て満たされている**

### 3.2 異常パス: ショップカード読み上げ（2026-03-18時点の不具合）

**なぜ同期が崩壊したか:**
1. **条件1違反**: `firstChunkStartTime` がLLMの「お探ししますね」発話時に設定されたまま、以降リセットされない
2. **条件3違反**: REST API検索中の空白期間で `audioContext.currentTime` だけが進む
3. `frameIndex` が実際のバッファ位置を大幅に超過 → 全セグメントで恒常的にズレる

---

## 4. 実装の必須ルール

### ルール1: 音声セグメントの境界でフロントエンドをリセットせよ
### ルール2: 音声送信とExpression送信は同一セグメント内で完結させよ
### ルール3: `audioContext.currentTime` の性質を理解せよ
### ルール4: `_emit_cached_audio` / `_emit_collected_shop` は `_receive_and_forward` と同じ同期保証を提供せよ
### ルール5: A2Eの `is_start` / `is_final` を正しく使え

---

## 5. 禁止事項

### 5.1 A2Eの推論遅延を仮定するな
### 5.2 A2Eの後処理を再実装するな
### 5.3 フロントエンドの同期メカニズムを迂回するな
### 5.4 正常パスのコードを「見た目だけ」コピーするな

---

## 参照

- **A2E公式リポジトリ**: https://github.com/aigc3d/LAM_Audio2Expression
- **論文**: He, Y. et al. (2025). "LAM: Large Avatar Model for One-shot Animatable Gaussian Head." arXiv:2502.17796v2.
- **A2E技術仕様書**: `docs/10_lam_audio2expression_spec.md`
- **V6統合仕様書**: `docs/09_liveapi_migration_design_v6.md`
