# 08: リップシンク・アバター実装仕様書（フェーズ2）

## 1. 概要

### 1.1 目的
コンシェルジュモードの2Dアバター画像（CSSアニメーション）を、Gaussian Splatting 3Dアバター + ARKit 52ブレンドシェイプによるリアルタイムリップシンクに置き換える。

### 1.2 実証テスト済みコンポーネント
| コンポーネント | 実証元 | 状態 |
|------------|--------|------|
| Audio2Expression サービス | `C:\Users\hamad\audio2exp-service` | デプロイ済み・動作確認済み |
| support-base A2E統合 | `app_customer_support.py` L129-161, L543-564 | 実装済み |
| LAMAvatar コンポーネント | `LAM_gpro` リポジトリ | 実証テスト済み |
| audio-sync-player | `LAM_gpro` リポジトリ | 実証テスト済み |
| lam-websocket-manager | `LAM_gpro` リポジトリ | 実証テスト済み |
| Gaussian Splatモデル | `concierge.zip` (4.09MB) | 作成済み |

### 1.3 対象ファイル

#### 移植元（LAM_gpro リポジトリ）
```
LAM_gpro/gourmet-sp/
├── src/components/LAMAvatar.astro          → 新規追加
├── src/scripts/lam/audio-sync-player.ts    → 新規追加
├── src/scripts/lam/lam-websocket-manager.ts → 新規追加
└── public/avatar/concierge.zip             → 新規追加
```

#### 修正対象（gourmet-sp3）
```
src/components/Concierge.astro              → アバターステージをLAMAvatar埋め込みに変更
src/scripts/chat/concierge-controller.ts    → A2Eリップシンク統合
```

#### バックエンド
```
audio2exp-service/                          → 新規ディレクトリとして移植
.github/workflows/deploy-audio2exp.yml      → 新規：自動デプロイ設定
support-base/                               → 変更なし（対応済み）
```

---

## 2. アーキテクチャ

### 2.1 全体データフロー

```
┌─────────────────────────────────────────────────────────┐
│ フロントエンド（Astro + TypeScript）                       │
│                                                         │
│  ┌──────────────┐    ┌───────────────────────┐          │
│  │ concierge-   │    │ LAMAvatar.astro        │          │
│  │ controller   │    │ (Gaussian Splat        │          │
│  │ .ts          │    │  Renderer)             │          │
│  │              │    │                        │          │
│  │ TTS応答受信  │───→│ queueExpressionFrames()│          │
│  │ expression   │    │ getExpressionData()    │          │
│  │ data抽出     │    │ @60fps描画ループ        │          │
│  │              │    │                        │          │
│  │ ttsPlayer    │───→│ setExternalTtsPlayer() │          │
│  │ (HTML Audio) │    │ currentTime同期        │          │
│  └──────┬───────┘    └───────────────────────┘          │
│         │                                               │
└─────────┼───────────────────────────────────────────────┘
          │ POST /api/tts/synthesize
          ▼
┌─────────────────────────────────────────────────────────┐
│ support-base（Cloud Run）                                │
│                                                         │
│  TTS合成 (Google Cloud TTS)                              │
│       │                                                 │
│       │ MP3 audio_base64                                │
│       ▼                                                 │
│  get_expression_frames()                                │
│       │ POST /api/audio2expression                      │
│       ▼                                                 │
│  レスポンス: { audio, expression: {names, frames, frame_rate} }│
└─────────┼───────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────┐
│ audio2exp-service（Cloud Run / 別サービス）               │
│                                                         │
│  入力: MP3 audio (base64)                                │
│  処理: Audio → ARKit 52 Blendshape Expression            │
│  出力: { names: string[52], frames: [{weights: float[52]}], │
│         frame_rate: 30 }                                │
└─────────────────────────────────────────────────────────┘
```

### 2.2 同期メカニズム（External TTS Playerモード）

```
時間軸 →
────────────────────────────────────────────────

ttsPlayer:  [────────── MP3再生中 ──────────]
            ↑play                         ↑ended
            │                              │
currentTime: 0.0s ─── 0.5s ─── 1.0s ─── 1.5s

Expression:  frame[0] frame[15] frame[30] frame[45]
             │        │         │         │
             ▼        ▼         ▼         ▼
Avatar:      口閉じ → 開き → 閉じ → 開き → 閉じ

フレーム計算:
  frameIndex = Math.floor((ttsPlayer.currentTime * 1000 / 1000) * frameRate)
  → frameRate=30の場合、1秒間に30フレームの表情データを適用

フェードイン/アウト:
  - 最初の6フレーム（~200ms @ 30fps）: alpha 0→1
  - 最後の6フレーム（~200ms @ 30fps）: alpha 1→0
  - Gaussian Splatの歪みアーティファクト防止
```

### 2.3 REST TTS vs LiveAPI音声の使い分け

| 音声ソース | A2Eリップシンク | 理由 |
|-----------|----------------|------|
| REST TTS（`/api/tts/synthesize`） | **適用する** | MP3完成後にA2E処理、expressionデータ同梱で遅延ゼロ |
| LiveAPI音声出力（ストリーミング） | **適用しない（現時点）** | 短いチャンク（300ms）単位のため、A2Eに十分な音声長がない |
| ショップ紹介セリフ | **適用する** | REST TTS使用のため上記と同じ |

---

## 3. バックエンド：audio2exp-service の移植とデプロイ

### 3.1 移植方針

**結論：再デプロイ推奨（自動デプロイ化）**

理由：
1. 既存デプロイは手動のため、コード変更時に再デプロイの手間が発生
2. support-baseと同様のCI/CDパイプラインで運用を統一
3. リポジトリ内にコードがあることでバージョン管理・レビューが可能

### 3.2 ディレクトリ構成

```
gourmet-sp3/
├── audio2exp-service/              ← C:\Users\hamad\audio2exp-service から移植
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py                      ← メインFlaskアプリ
│   ├── audio2expression.py         ← A2Eコア処理
│   ├── models/                     ← 推論モデルファイル
│   └── ...
├── support-base/                   ← 変更なし
└── .github/workflows/
    ├── deploy-cloud-run.yml        ← 既存（support-base用）
    └── deploy-audio2exp.yml        ← 新規（audio2exp-service用）
```

### 3.3 GitHub Actions：deploy-audio2exp.yml

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
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Authenticate to Google Cloud
        uses: google-github-actions/auth@v2
        with:
          credentials_json: '${{ secrets.GCP_SA_KEY }}'

      - name: Set up Cloud SDK
        uses: google-github-actions/setup-gcloud@v2

      - name: Configure Docker for Artifact Registry
        run: gcloud auth configure-docker ${{ env.REGION }}-docker.pkg.dev --quiet

      - name: Create Artifact Registry repository (if not exists)
        run: |
          gcloud artifacts repositories describe ${{ env.SERVICE_NAME }} \
            --location=${{ env.REGION }} \
            --project=${{ env.PROJECT_ID }} 2>/dev/null || \
          gcloud artifacts repositories create ${{ env.SERVICE_NAME }} \
            --repository-format=docker \
            --location=${{ env.REGION }} \
            --project=${{ env.PROJECT_ID }}

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

      - name: Show deployed URL
        run: |
          URL=$(gcloud run services describe ${{ env.SERVICE_NAME }} \
            --region=${{ env.REGION }} \
            --project=${{ env.PROJECT_ID }} \
            --format='value(status.url)')
          echo "::notice::Audio2Exp deployed to: $URL"

      - name: Health check
        run: |
          sleep 10
          curl -sf "$URL/health" || echo "Health check pending"
```

**注意点：**
- `memory=2Gi`, `cpu=2` — A2E推論はGPU不使用でもCPU/メモリを多く消費
- `min-instances=1` — コールドスタート排除（推論モデルのロードに時間がかかるため）
- `max-instances=3` — 同時接続数に応じてスケール

### 3.4 support-base デプロイ設定の更新

`deploy-cloud-run.yml` に `AUDIO2EXP_SERVICE_URL` を追加：

```yaml
# 既存の --set-env-vars に追加
--set-env-vars="AUDIO2EXP_SERVICE_URL=${{ secrets.AUDIO2EXP_SERVICE_URL }}"
```

### 3.5 support-base 側の既存実装（変更不要）

以下は実装済みで変更不要：

| 機能 | ファイル | 行 |
|------|---------|-----|
| A2Eサービス設定 | `app_customer_support.py` | L62-68 |
| `get_expression_frames()` | `app_customer_support.py` | L129-161 |
| TTS応答にexpression同梱 | `app_customer_support.py` | L543-564 |
| ヘルスチェックでA2E状態報告 | `app_customer_support.py` | L735 |

---

## 4. フロントエンド：ファイル追加

### 4.1 追加ファイル一覧

LAM_gpro リポジトリ (`claude/update-lam-modelscope-UQKxj` ブランチ) から移植：

| 移植元 | 移植先 | 説明 |
|--------|--------|------|
| `gourmet-sp/src/components/LAMAvatar.astro` | `src/components/LAMAvatar.astro` | 3Dアバターコンポーネント |
| `gourmet-sp/src/scripts/lam/audio-sync-player.ts` | `src/scripts/lam/audio-sync-player.ts` | WebSocket用音声同期プレイヤー |
| `gourmet-sp/src/scripts/lam/lam-websocket-manager.ts` | `src/scripts/lam/lam-websocket-manager.ts` | LAM WebSocket管理 |
| `gourmet-sp/public/avatar/concierge.zip` | `public/avatar/concierge.zip` | Gaussian Splatモデル (4.09MB) |

### 4.2 LAMAvatar.astro の主要機能

```typescript
// グローバルインターフェース
interface ExpressionData {
  names: string[];      // ARKit 52 blendshape名
  weights: number[];    // 各blendshapeの重み (0.0-1.0)
}

interface LAMAvatarController {
  queueExpressionFrames(frames: ExpressionData[], frameRate: number): void;
  clearFrameBuffer(): void;
  setExternalTtsPlayer(player: HTMLAudioElement): void;
}

// window.lamAvatarController として公開
```

**描画ループ（@60fps）：**
```
getExpressionData() が renderer から毎フレーム呼ばれる
  ↓
ttsPlayer.currentTime から現在のフレームインデックスを計算
  frameIndex = Math.floor((currentTimeMs / 1000) * frameRate)
  ↓
frameBuffer[frameIndex] から ExpressionData を取得
  ↓
フェードイン/アウト処理（最初・最後の6フレーム）
  ↓
renderer が blendshape を Gaussian Splat に適用
```

**依存パッケージ：**
```
gaussian-splat-renderer-for-lam   ← npm パッケージ（GaussianSplats3D フォーク）
```

### 4.3 audio-sync-player.ts

WebSocket経由のバンドルモード用。現段階では**直接は使用しない**が、将来LiveAPI音声ストリーミング対応時に必要になる可能性があるため移植しておく。

- Web Audio API ベースの再生キュー
- Int16 → Float32 変換
- 再生オフセット追跡（`getCurrentPlaybackOffset()`）

### 4.4 lam-websocket-manager.ts

OpenAvatarChat バックエンドとの通信用。現段階では**直接は使用しない**が、JBIN形式パーサーとARKit定数定義を含むため移植しておく。

- JBIN バイナリプロトコルのパーサー
- ARKit 52 チャンネル名定数 (`ARKIT_CHANNEL_NAMES`)
- WebSocket接続管理（自動再接続、keepalive）

---

## 5. フロントエンド：既存ファイル修正

### 5.1 Concierge.astro の変更

**変更内容：** アバターステージを2D画像からLAMAvatarコンポーネントに置換

```diff
 <!-- 変更前 -->
 <div class="avatar-stage" id="avatarStage">
   <div class="avatar-container">
-    <img id="avatarImage" src="/images/avatar-anime.png" alt="AI Avatar" class="avatar-img" />
+    <LAMAvatar />
   </div>
 </div>
```

- `LAMAvatar.astro` をインポート
- 2D画像 (`avatar-anime.png`) はフォールバック用にLAMAvatar内部で保持
- `.avatar-container` のCSSを3Dレンダリング用に調整（canvasサイズ対応）

### 5.2 concierge-controller.ts の変更

#### 5.2.1 TTS Player リンク（init時）

```typescript
// init() 内に追加
private linkLamAvatar() {
  const lamController = (window as any).lamAvatarController;
  if (lamController) {
    lamController.setExternalTtsPlayer(this.ttsPlayer);
  } else {
    // LAMAvatar初期化待ち（2秒後リトライ）
    setTimeout(() => this.linkLamAvatar(), 2000);
  }
}
```

#### 5.2.2 speakTextGCP() のオーバーライド修正

```typescript
// 変更前: CSSクラスの付け外しのみ
protected async speakTextGCP(text, stopPrevious, autoRestartMic, skipAudio) {
  // avatarContainer.classList.add('speaking');  ← 削除
  await super.speakTextGCP(text, stopPrevious, autoRestartMic, skipAudio);
  // avatarContainer.classList.remove('speaking');  ← 削除
}

// 変更後: TTS応答からexpressionデータを抽出してLAMAvatarに適用
protected async speakTextGCP(text, stopPrevious, autoRestartMic, skipAudio) {
  if (skipAudio || !this.isTTSEnabled || !text) return Promise.resolve();
  if (stopPrevious) this.ttsPlayer.pause();

  const langConfig = this.LANGUAGE_CODE_MAP[this.currentLanguage];
  const cleanText = this.stripMarkdown(text);

  const response = await fetch(`${this.apiBase}/api/tts/synthesize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text: cleanText,
      language_code: langConfig.tts,
      voice_name: langConfig.voice,
      session_id: this.sessionId  // ★ A2E用にsession_idを送信
    })
  });
  const data = await response.json();

  if (data.success) {
    // ★ expression データがあればLAMAvatarに適用
    if (data.expression) {
      this.applyExpressionFromTts(data.expression);
    }

    this.ttsPlayer.src = `data:audio/mp3;base64,${data.audio}`;
    await new Promise<void>((resolve) => {
      this.ttsPlayer.onended = () => resolve();
      this.ttsPlayer.play();
    });
  }
}
```

#### 5.2.3 Expression適用メソッド（新規追加）

```typescript
private applyExpressionFromTts(expression: {
  names: string[];
  frames: Array<{ weights: number[] }>;
  frame_rate: number;
}) {
  const lamController = (window as any).lamAvatarController;
  if (!lamController) return;

  // フレームバッファをクリア
  lamController.clearFrameBuffer();

  // frames を ExpressionData[] に変換
  const expressionFrames = expression.frames.map(frame => ({
    names: expression.names,
    weights: frame.weights
  }));

  // キューに追加（再生開始前に完了）
  lamController.queueExpressionFrames(expressionFrames, expression.frame_rate);
}
```

#### 5.2.4 speakResponseInChunks() の修正

並行TTS処理でもexpressionデータを適用するよう修正：

```typescript
// TTS fetchレスポンスから expression を取り出して適用
const result = await response.json();
if (result.success && result.expression) {
  this.applyExpressionFromTts(result.expression);
}
```

**注意：** センテンス分割再生時、各セグメントごとに `clearFrameBuffer()` → `queueExpressionFrames()` を行う。

#### 5.2.5 ショップ紹介TTS部分の修正

`sendMessage()` 内のショップ紹介TTS呼び出し箇所（L541-659）でも、同様にexpressionデータを適用。

#### 5.2.6 stopAvatarAnimation() の修正

```typescript
// 変更前: CSSクラス操作
private stopAvatarAnimation() {
  this.els.avatarContainer?.classList.remove('speaking');
}

// 変更後: フレームバッファクリア
private stopAvatarAnimation() {
  const lamController = (window as any).lamAvatarController;
  if (lamController) {
    lamController.clearFrameBuffer();
  }
}
```

### 5.3 session_id の送信追加

現在の `speakTextGCP()`（`core-controller.ts` の親クラス）は `session_id` を TTS APIに送信していない。A2Eサービスが `session_id` を必要とするため、TTS呼び出し時に `session_id` を含める必要がある。

```typescript
// core-controller.ts の speakTextGCP 内
body: JSON.stringify({
  text: cleanText,
  language_code: langConfig.tts,
  voice_name: langConfig.voice,
  session_id: this.sessionId  // ★ 追加
})
```

---

## 6. npmパッケージ追加

```bash
npm install gaussian-splat-renderer-for-lam
```

`package.json` に追加される依存：
```json
{
  "dependencies": {
    "gaussian-splat-renderer-for-lam": "^x.x.x"
  }
}
```

---

## 7. 実装順序

### Step 1: audio2exp-service の移植
1. `C:\Users\hamad\audio2exp-service` の内容を `gourmet-sp3/audio2exp-service/` にコピー
2. `.github/workflows/deploy-audio2exp.yml` を作成
3. `deploy-cloud-run.yml` に `AUDIO2EXP_SERVICE_URL` 環境変数を追加
4. GitHub Secrets に `AUDIO2EXP_SERVICE_URL` を設定

### Step 2: フロントエンド ファイル追加
1. LAM_gpro から4ファイルを移植
   - `src/components/LAMAvatar.astro`
   - `src/scripts/lam/audio-sync-player.ts`
   - `src/scripts/lam/lam-websocket-manager.ts`
   - `public/avatar/concierge.zip`
2. `npm install gaussian-splat-renderer-for-lam`

### Step 3: Concierge.astro 修正
1. LAMAvatarコンポーネントの埋め込み
2. CSS調整（canvasサイズ、レスポンシブ対応）

### Step 4: concierge-controller.ts 修正
1. `linkLamAvatar()` の追加（init時）
2. `speakTextGCP()` の書き換え（expression適用）
3. `applyExpressionFromTts()` の追加
4. `speakResponseInChunks()` の修正
5. ショップ紹介TTS部分の修正
6. `stopAvatarAnimation()` の修正
7. `session_id` の送信追加

### Step 5: テスト・検証
1. ローカルでGaussian Splatモデルのロード確認
2. TTS + expression同期再生の確認
3. フェードイン/アウトの動作確認
4. WebGL非対応時のフォールバック確認
5. モバイル端末でのパフォーマンス確認

---

## 8. 検証チェックリスト

### 8.1 バックエンド
- [ ] audio2exp-service が Cloud Run にデプロイされている
- [ ] `/health` エンドポイントが200を返す
- [ ] support-base の `/health` で audio2exp が "connected" と報告される
- [ ] `/api/tts/synthesize` のレスポンスに `expression` フィールドが含まれる
- [ ] `expression.names` が52要素の配列（ARKit blendshape名）
- [ ] `expression.frames` が適切なフレーム数を含む
- [ ] `expression.frame_rate` が30（または設定値）

### 8.2 フロントエンド
- [ ] Gaussian Splatモデル（`concierge.zip`）が正常にロードされる
- [ ] 3Dアバターが表示される（WebGL対応ブラウザ）
- [ ] WebGL非対応時に2Dフォールバック画像が表示される
- [ ] TTS再生時にリップシンクが動作する
- [ ] 音声とリップシンクが同期している（目視確認）
- [ ] フェードイン/アウトが滑らかに動作する
- [ ] 音声停止時にアバターが静止状態に戻る
- [ ] ショップ紹介セリフでもリップシンクが動作する
- [ ] 並行TTS処理（speakResponseInChunks）でもリップシンクが動作する
- [ ] モバイルでGaussian Splatが表示される（パフォーマンス許容範囲）

### 8.3 デプロイ
- [ ] `audio2exp-service/` への変更で自動デプロイが発動する
- [ ] `support-base/` のデプロイに `AUDIO2EXP_SERVICE_URL` が含まれる
- [ ] min-instances=1 によりコールドスタートが回避されている

---

## 9. 既知の制限事項・今後の課題

### 9.1 LiveAPI音声ストリーミング対応（未対応）
- 現在LiveAPIの音声出力は短いPCMチャンク（300ms @ 24kHz）
- A2Eに十分な長さの音声を送るには、チャンクのバッファリングが必要
- `audio-sync-player.ts` がこのユースケースの基盤として将来利用可能
- **フェーズ3の課題として保留**

### 9.2 Gaussian Splatのパフォーマンス
- WebGLが必要（非対応ブラウザではフォールバック画像）
- モバイル端末ではGPU負荷が高い可能性
- モデルサイズ 4.09MB のダウンロードがある（初回ロード）

### 9.3 TTS応答のレイテンシ
- A2E処理がTTSレスポンスに加算（通常0.5-2秒）
- support-base の `min-instances=1`、audio2exp の `min-instances=1` でコールドスタート回避済み
- 並行TTS処理で体感レイテンシを最小化

---

## 10. ファイル変更サマリ

| ファイル | アクション | 変更規模 |
|---------|----------|---------|
| `audio2exp-service/*` | 新規移植 | ディレクトリごとコピー |
| `.github/workflows/deploy-audio2exp.yml` | 新規作成 | ~60行 |
| `.github/workflows/deploy-cloud-run.yml` | 修正 | 1行追加 |
| `src/components/LAMAvatar.astro` | 新規移植 | ~500行 |
| `src/scripts/lam/audio-sync-player.ts` | 新規移植 | ~220行 |
| `src/scripts/lam/lam-websocket-manager.ts` | 新規移植 | ~400行 |
| `public/avatar/concierge.zip` | 新規追加 | 4.09MB |
| `src/components/Concierge.astro` | 修正 | ~10行変更 |
| `src/scripts/chat/concierge-controller.ts` | 修正 | ~100行変更 |
| `src/scripts/chat/core-controller.ts` | 修正 | ~5行（session_id追加） |
| `package.json` | 修正 | 依存追加 |
