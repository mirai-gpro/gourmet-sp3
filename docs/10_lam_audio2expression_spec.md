# LAM Audio2Expression 技術仕様書

**作成日**: 2026-03-18
**参照元**:
- 論文: He et al., "LAM: Large Avatar Model for One-shot Animatable Gaussian Head", arXiv:2502.17796v2, 2025年4月
- GitHub: https://github.com/aigc3d/LAM_Audio2Expression
- 論文PDF: `docs/2502.17796v2.pdf`

---

## 1. LAM（Large Avatar Model）アーキテクチャ

### 1.1 概要

LAMは、**1枚の画像からアニメーション可能な3D Gaussianヘッドアバターを生成**するモデル。アリババ Tongyi Lab が開発。

従来手法との最大の違い:
- **追加のニューラルネットワーク不要**でアニメーション・レンダリング可能
- 2Dポストプロセッシングを排除 → WebGLから直接レンダリング可能
- 既存のレンダリングパイプラインにそのまま統合できる

### 1.2 パイプライン（論文 Figure 2）

```
入力画像
  ↓
DINOv2（事前学習済みViT）で特徴量抽出
  ↓ F_I（画像特徴量: 浅い層 + 深い層を融合）
  ↓
FLAME頂点を初期位置として使用（M=81,424点、2回メッシュ細分割）
  ↓ F_P（学習可能なクエリ特徴量: 位置符号化経由）
  ↓
Stacked Cross-Attention Modules
  F_P_i = C_i(F_P_{i-1}, F_I)
  ↓
Decoding Headers（MLP群）
  → c_k（色: R^3）, o_k（不透明度: R）, s_k（スケール: R）, R_k（回転: SO(3)）, O_k（オフセット: R^3）
  ↓
正準空間(Canonical Space)でGaussianアバター再構成
  ↓
FLAME LBS + Corrective Blendshapes でアニメーション
  F_G(θ,φ) = S(T_G(θ,φ), J, θ, W)
  ↓
Gaussian Splatting でレンダリング（WebGL/HTML）
```

### 1.3 核心的設計判断

| 設計 | 内容 | 意義 |
|------|------|------|
| FLAME頂点をGaussianの初期位置に使用 | 事前形状情報を活用 | 再構成の複雑さを軽減 |
| 正準空間で再構成 | 全表情・ポーズを同一座標系で再構成 | 表情変化時に再構成不要 |
| LBS + Corrective Blendshapes | FLAMEモデルと同じアニメーション方式 | **追加NNなしでアニメーション可能** |
| 2Dポストプロセス排除 | 純粋な3Dソリューション | WebGL等の既存パイプラインに直接統合 |

### 1.4 アニメーション数式（論文 §3.3）

FLAMEモデルのアニメーション:
```
F(β, θ, φ) = S(T_P(β, θ, φ), J(β), θ, W)
T_P = T̄ + B_S(β; S) + B_P(θ; P) + B_E(φ; E)
```

LAMの拡張（オフセット追加）:
```
T_G(θ, φ) = Ḡ + B_P(θ; P) + B_E(φ; E)
Ḡ = T̄ + B_S(β; S) + O
F_G(θ, φ) = S(T_G(θ, φ), J, θ, W)
```

- `O = N(T̄ + B_S(β; S))` : ネットワークが予測するXYZオフセット（髪・アクセサリー等FLAME外の形状）
- `Ḡ` は1回だけ計算してキャッシュ可能 → 推論時は高速

### 1.5 レンダリング性能（論文 Table 3）

| プラットフォーム | FPS |
|----------------|-----|
| A100 GPU (PyTorch + 3DGS) | **280.96** |
| MacBook M1 Pro (WebGL) | **120** |
| iPhone 16 (WebGL) | **35** |
| Xiaomi 14 (WebGL) | **26** |

### 1.6 訓練詳細（論文 §4.1）

- Transformer: L=10層、16ヘッド、1024次元
- データセット: VFHQ（15,204動画、3Mフレーム）
- 画像サイズ: 512×512
- Gaussian点数: M=81,424（メッシュ2回細分割）
- オプティマイザ: ADAM + コサインウォームアップ（200エポック）
- 損失: L = λ₁L₁ + λ₂L_lpips + λ₃L_mask + λ₄L_o（λ₁=λ₂=λ₃=1, λ₄=0.1）

---

## 2. Audio2Expression（A2E）モジュール

### 2.1 概要

音声から52個のARKit blendshape係数をリアルタイム生成するモジュール。

```
音声入力 (16kHz PCM)
  ↓
Wav2Vec2 エンコーダ（事前学習済み音声特徴抽出）
  ↓ ~50fps の特徴量
F.interpolate(mode='linear') で30fpsにダウンサンプル
  ↓
デコーダ（スタイル特徴量とのクロスアテンション）
  ↓
52個のARKit blendshape係数 × 30fps
```

### 2.2 基本パラメータ（GitHubリポジトリのコードから確認）

| パラメータ | 値 | ソース |
|-----------|-----|--------|
| 音声サンプルレート | 16,000 Hz | `audio_sr = 16000` |
| 出力FPS | 30.0 | `fps = 30.0` |
| ストリーミングウィンドウ | 最大64フレーム | `max_frame_length = 64` |
| 1チャンク入力 | 16,000サンプル（1秒） | `gap = 16000` |
| 設定ファイル | `lam_audio2exp_config_streaming.py` | — |

### 2.3 ストリーミング推論（`inference_streaming_audio.py`）

```python
# ストリーミングループ
gap = 16000  # 1秒分
input_num = audio.shape[0] // 16000 + 1
context = None

for i in range(input_num):
    output, context = infer.infer_streaming_audio(
        audio[i*gap:(i+1)*gap],
        sample_rate,
        context
    )
    all_exp.append(output['expression'])

# 結果結合
expressions = np.concatenate(all_exp, axis=0)
```

### 2.4 ストリーミングエンジン（`engines/infer.py`）

#### フレーム数計算
```python
frame_length = math.ceil(audio.shape[0] / ssr * 30)
```
- 1秒（16,000サンプル）→ `ceil(16000 / 16000 * 30)` = **30フレーム**

#### スライディングウィンドウ機構

```python
max_frame_length = 64  # ウィンドウサイズ

# 初回: ブランク音声でパディング
blank_audio_length = audio_sr * max_frame_length // 30 - in_audio.shape[0]

# 2回目以降: 前チャンクの音声を結合
clip_pre_audio = context['previous_audio'][-clip_pre_audio_length:]
# → 前チャンクの音声 + 新チャンクの音声 を結合してモデルに入力
```

#### 出力スライシング
```python
start_frame = int(max_frame_length - in_audio.shape[0] / self.cfg.audio_sr * 30)
out_exp = output_dict['pred_exp'].squeeze().cpu().numpy()[start_frame:, :]
```
- モデルは常に64フレーム分を一括推論
- `start_frame` でパディング分をスキップし、新規音声に対応するフレームのみ返却
- 例: 1秒入力 → `start_frame = 64 - 30 = 34` → `out_exp[34:]` = **30フレーム返却**

#### contextオブジェクト
```python
context = {
    'previous_audio': ...,       # 前チャンクの音声波形
    'previous_expression': ...,  # 前チャンクの出力blendshape
    'previous_volume': ...,      # 前チャンクの音量
    'is_initial_input': False    # 初回フラグ
}
```
- チャンク間の時系列連続性を確保するための状態管理
- 前チャンクの音声をオーバーラップさせることで、チャンク境界のアーティファクトを防止

---

## 3. 後処理パイプライン（`models/utils.py`）

A2Eモデルの生出力に対して、以下の順序で後処理を適用:

### 3.1 `smooth_mouth_movements()`
- **無音区間**で口のblendshapeを10%に抑制
- 音声がない時に口がパクパクしないようにする

### 3.2 `apply_frame_blending()`
- チャンク境界での**線形補間**
- 初回: 3フレーム、後続: 5フレームの補間ウィンドウ
- チャンク切り替わり時の不連続を滑らかにする

### 3.3 `apply_savitzky_golay_smoothing()`
- 時間軸方向の**多項式平滑化フィルタ**
- パラメータ: window=5, polyorder=2
- 高周波ノイズの除去

### 3.4 `symmetrize_blendshapes()`
- 左右ペア**20組**の平均化
- モード: average / max / min / left_dominant / right_dominant
- 左右非対称なアーティファクトの抑制

### 3.5 `apply_random_eye_blinks_context()`
- **まばたきは音声から予測不可能** → 手続き的に生成
- A2Eモデルは eyeBlink (#8, #9) を**ゼロで出力**
- 事前定義パターン**4種**からランダム選択
- 1回のまばたき = **7フレーム（233ms）**
- まばたき間隔 = **40〜100フレーム**（1.3〜3.3秒）

### 3.6 `apply_random_brow_movement()`（暗黙的に適用）
- **眉の動きも手続き的に生成**
- 音声のRMSボリュームに基づいて高音量区間で眉を動かす
- blendshape #0-4 に対して事前キャプチャパターン**3種**を適用

---

## 4. 52 ARKit ブレンドシェイプ — 完全リスト

### 4.1 公式順序（GitHub `models/utils.py` の `ARKitBlendShape` 配列）

| # | パラメータ名 | 対象部位 | 制御内容 |
|---|-----------|---------|---------|
| 0 | browDownLeft | 左眉 | 眉を下げる（しかめる） |
| 1 | browDownRight | 右眉 | 眉を下げる（しかめる） |
| 2 | browInnerUp | 眉内側 | 眉の内側を上げる（心配顔） |
| 3 | browOuterUpLeft | 左眉外側 | 左眉の外側を上げる |
| 4 | browOuterUpRight | 右眉外側 | 右眉の外側を上げる |
| 5 | cheekPuff | 頬 | 頬を膨らませる |
| 6 | cheekSquintLeft | 左頬 | 左頬を上に引き上げる（笑い） |
| 7 | cheekSquintRight | 右頬 | 右頬を上に引き上げる（笑い） |
| 8 | eyeBlinkLeft | 左目 | 左まぶたを閉じる |
| 9 | eyeBlinkRight | 右目 | 右まぶたを閉じる |
| 10 | eyeLookDownLeft | 左目 | 左目を下に向ける |
| 11 | eyeLookDownRight | 右目 | 右目を下に向ける |
| 12 | eyeLookInLeft | 左目 | 左目を内側（鼻方向）に向ける |
| 13 | eyeLookInRight | 右目 | 右目を内側（鼻方向）に向ける |
| 14 | eyeLookOutLeft | 左目 | 左目を外側に向ける |
| 15 | eyeLookOutRight | 右目 | 右目を外側に向ける |
| 16 | eyeLookUpLeft | 左目 | 左目を上に向ける |
| 17 | eyeLookUpRight | 右目 | 右目を上に向ける |
| 18 | eyeSquintLeft | 左目 | 左目を細める |
| 19 | eyeSquintRight | 右目 | 右目を細める |
| 20 | eyeWideLeft | 左目 | 左目を見開く |
| 21 | eyeWideRight | 右目 | 右目を見開く |
| 22 | jawForward | 顎 | 顎を前に出す |
| 23 | jawLeft | 顎 | 顎を左に動かす |
| 24 | jawOpen | 顎 | 口を開ける（**リップシンクの主要パラメータ**） |
| 25 | jawRight | 顎 | 顎を右に動かす |
| 26 | mouthClose | 口 | 唇を閉じる（jawOpenとの組合せ） |
| 27 | mouthDimpleLeft | 左口角 | 左口角にえくぼ |
| 28 | mouthDimpleRight | 右口角 | 右口角にえくぼ |
| 29 | mouthFrownLeft | 左口角 | 左口角を下げる（不満顔） |
| 30 | mouthFrownRight | 右口角 | 右口角を下げる（不満顔） |
| 31 | mouthFunnel | 口 | 口をすぼめる（「お」の形） |
| 32 | mouthLeft | 口 | 口全体を左に動かす |
| 33 | mouthLowerDownLeft | 下唇左 | 下唇の左側を下げる |
| 34 | mouthLowerDownRight | 下唇右 | 下唇の右側を下げる |
| 35 | mouthPressLeft | 左唇 | 左唇を押し付ける |
| 36 | mouthPressRight | 右唇 | 右唇を押し付ける |
| 37 | mouthPucker | 口 | 唇をとがらせる（キス、「う」の形） |
| 38 | mouthRight | 口 | 口全体を右に動かす |
| 39 | mouthRollLower | 下唇 | 下唇を内側に巻き込む |
| 40 | mouthRollUpper | 上唇 | 上唇を内側に巻き込む |
| 41 | mouthShrugLower | 下唇 | 下唇を持ち上げる（しゃくれ） |
| 42 | mouthShrugUpper | 上唇 | 上唇を持ち上げる |
| 43 | mouthSmileLeft | 左口角 | 左口角を上げる（笑顔） |
| 44 | mouthSmileRight | 右口角 | 右口角を上げる（笑顔） |
| 45 | mouthStretchLeft | 左口角 | 左口角を横に引っ張る |
| 46 | mouthStretchRight | 右口角 | 右口角を横に引っ張る |
| 47 | mouthUpperUpLeft | 上唇左 | 上唇の左側を上げる |
| 48 | mouthUpperUpRight | 上唇右 | 上唇の右側を上げる |
| 49 | noseSneerLeft | 左鼻 | 左鼻翼を上げる（鼻にしわ） |
| 50 | noseSneerRight | 右鼻 | 右鼻翼を上げる（鼻にしわ） |
| 51 | tongueOut | 舌 | 舌を出す（※FLAMEは舌をモデル化していない） |

### 4.2 機能グループ別分類

| グループ | 個数 | インデックス |
|---------|------|------------|
| 眉 (brow) | 5 | #0-4 |
| 頬 (cheek) | 3 | #5-7 |
| 目 (eye) | 14 | #8-21 |
| 顎 (jaw) | 4 | #22-25 |
| 口 (mouth) | 23 | #26-48 |
| 鼻 (nose) | 2 | #49-50 |
| 舌 (tongue) | 1 | #51 |

### 4.3 リップシンク主要パラメータ（13個）

音声同期に最も重要なblendshape:

| パラメータ | # | 役割 |
|-----------|---|------|
| jawOpen | 24 | 口の開閉（最重要） |
| mouthClose | 26 | 唇の閉じ（jawOpenとペア） |
| mouthFunnel | 31 | 「お」の口形 |
| mouthPucker | 37 | 「う」の口形 |
| mouthLowerDownLeft | 33 | 下唇の動き（左） |
| mouthLowerDownRight | 34 | 下唇の動き（右） |
| mouthUpperUpLeft | 47 | 上唇の動き（左） |
| mouthUpperUpRight | 48 | 上唇の動き（右） |
| mouthSmileLeft | 43 | 口角の上げ（左） |
| mouthSmileRight | 44 | 口角の上げ（右） |
| mouthStretchLeft | 45 | 口角の横引き（左） |
| mouthStretchRight | 46 | 口角の横引き（右） |

### 4.4 A2Eが生成しないパラメータ

以下はA2Eモデルがゼロで出力し、後処理で手続き的に生成するもの:

| パラメータ | 生成方法 |
|-----------|---------|
| eyeBlinkLeft (#8), eyeBlinkRight (#9) | `apply_random_eye_blinks_context()` — 4種のパターンからランダム生成 |
| browDownLeft (#0) 〜 browOuterUpRight (#4) | `apply_random_brow_movement()` — 音声RMSボリュームに基づく |

### 4.5 ブレンドシェイプ順序の注意

**A2Eサービスの公式順序**（GitHub `models/utils.py`）:
```
browDownLeft(0) → browDownRight(1) → browInnerUp(2) → ... → jawOpen(24) → ... → tongueOut(51)
```

A2Eサービスのレスポンスには `names` 配列が含まれるため、**名前ベースでマッチング**すればインデックス不一致の問題は発生しない。インデックスベースで直接参照している箇所がある場合は注意が必要。

---

## 5. 論文の制限事項（§5 Conclusions and Limitations）

| 制限 | 詳細 |
|------|------|
| FLAMEの表現限界 | FLAMEがモデル化できない表情は再現不可（例: 舌の動き `tongueOut`） |
| 動的しわ | 2Dポストプロセッシングを排除したため、表情依存の動的しわは完全にはモデル化できない |
| FLAME推定精度 | 入力画像からのFLAMEパラメータ推定の精度が最終結果に影響する |
| 表情中立化 | 単一画像からの表情中立化には限界がある |

---

## 6. 本プロジェクトでの利用アーキテクチャ

```
Gemini LiveAPI（音声対話）
  ↓ PCM音声ストリーム
バックエンド (live_api_handler.py)
  ↓ 音声チャンクをA2Eサービスに送信
A2Eサービス (audio2exp-service)
  ↓ 52個のblendshape係数 × 30fps を返却
  ↓ （フレームは送らない — blendshape係数だけ）
バックエンド
  ↓ live_expression イベントで Socket.IO 経由送信
フロントエンド (lam-websocket-manager.ts)
  ↓ blendshape係数をバッファに格納
LAM WebGLレンダラー
  ↓ 係数を適用してGaussianアバターをアニメーション
ブラウザ画面に表示
```

**核心**: フレーム画像は一切送らない。52個のfloat値（blendshape係数）だけを送る。これがA2E + LAMの低遅延リップシンクの設計思想。

---

## 参照

- He, Y. et al. (2025). "LAM: Large Avatar Model for One-shot Animatable Gaussian Head." arXiv:2502.17796v2.
- GitHub: https://github.com/aigc3d/LAM_Audio2Expression
- プロジェクトページ: https://aigc3d.github.io/projects/LAM/
