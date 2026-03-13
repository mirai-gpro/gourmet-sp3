# ショップカード＆チャットテキスト表示用 JSON形式対応 修正仕様書

> **作成日**: 2026-03-13
> **前提文書**: `docs/05_liveapi_migration_design_v5.md`
> **対象コード**: `support-base/live_api_handler.py`, `support-base/app_customer_support.py`, `support-base/support_core.py`, `src/scripts/chat/core-controller.ts`

---

## 1. 背景と課題

### 1.1 現状（v5設計）

v5設計では「REST APIからLiveAPIへの全面移行」を方針とし、以下のフローでショップ提案を行う:

```
LiveAPI(会話) → function calling(search_shops) → shop_search_callback() → shop_search_result送信 → LiveAPI(説明読み上げ)
                                                   │
                                                   └── 内部でREST版SupportAssistantを呼び出し
                                                       → Gemini REST APIでJSON取得
                                                       → enrich_shops_with_photos()で補完
```

### 1.2 テストで判明した問題

LiveAPIのプロンプト（`LIVEAPI_SHOP_CARD_RULES`）では、shop提案時にJSON形式での出力を指示しているが、以下の問題がある:

1. **LiveAPIのJSON出力が不安定**: LiveAPIは音声応答（`response_modalities: ["AUDIO"]`）に最適化されており、構造化JSONの精度が安定しない
2. **二重生成の矛盾**: function calling（`search_shops`）経由でREST版がショップデータを生成するのに、LiveAPIのプロンプトにもJSON出力ルールが残っている
3. **役割の重複**: ショップカードデータの生成元が不明確（LiveAPIのJSON出力 vs REST版callback）

### 1.3 テストで成功したパターン

以下のハイブリッド構成で「正確なショップカード表示」と「LiveAPIによるお店説明の読み上げ」の両立を確認:

| 機能 | 担当 | 方式 |
|------|------|------|
| **会話（ヒアリング）** | LiveAPI | 音声入出力（既存のまま） |
| **ショップ検索の発火** | LiveAPI | function calling: `search_shops`（既存のまま） |
| **ショップカードJSON生成** | **REST版（安定版）** | `SupportAssistant.process_user_message()` |
| **チャットテキスト表示** | **REST版（安定版）** | `response` フィールドのテキスト |
| **ショップデータ補完** | **REST版（安定版）** | `enrich_shops_with_photos()`（Google Places, TripAdvisor等） |
| **お店説明の読み上げ** | LiveAPI | `_describe_shops_via_live()`（既存のまま） |

---

## 2. 修正方針

### 2.1 基本原則

**「表示はREST、音声はLiveAPI」の役割分離を明確化する。**

```
【修正前（v5設計 — 役割が曖昧）】
LiveAPI: 会話 + JSON出力指示(SHOP_CARD_RULES) + function calling + 説明読み上げ
REST版: shop_search_callback内で使用（裏方）

【修正後（本仕様）】
LiveAPI: 会話 + function calling(search_shopsトリガーのみ) + 説明読み上げ(TTS)
REST版: ショップカードJSON生成 + チャットテキスト生成 + データ補完（表示の責任者）
```

### 2.2 変更しないもの（既存実装をそのまま維持）

| # | 項目 | ファイル | 理由 |
|---|------|---------|------|
| 1 | LiveAPI接続・音声送受信・再接続 | `live_api_handler.py` 全般 | 動作確認済み |
| 2 | function calling定義 (`SEARCH_SHOPS_DECLARATION`) | `live_api_handler.py` L291-304 | 動作確認済み |
| 3 | `_handle_tool_call()` | `live_api_handler.py` L674-697 | 動作確認済み |
| 4 | `_handle_shop_search()` | `live_api_handler.py` L699-734 | REST版callbackを呼び出す既存フロー |
| 5 | `shop_search_callback()` | `app_customer_support.py` L783-806 | REST版SupportAssistant経由のデータ取得 |
| 6 | `_describe_shops_via_live()` | `live_api_handler.py` L775-845 | TTS読み上げ（既存のまま） |
| 7 | `_receive_shop_description()` | `live_api_handler.py` L847-890 | TTS音声受信（既存のまま） |
| 8 | `shop_search_result` Socket.IOイベント | フロント・バックエンド両方 | カード表示トリガー（既存のまま） |
| 9 | `ShopCardList.astro` のUI表示ロジック | `src/components/ShopCardList.astro` | カード描画（既存のまま） |
| 10 | `enrich_shops_with_photos()` | `api_integrations.py` L512-701 | データ補完（既存のまま） |

### 2.3 変更するもの

| # | 項目 | 変更内容 |
|---|------|---------|
| 1 | `LIVEAPI_SHOP_CARD_RULES` プロンプト | JSON出力ルールを削除し、function calling専用ルールに書き換え |
| 2 | `LIVEAPI_CHAT_SYSTEM` / `LIVEAPI_CONCIERGE_SYSTEM` | `{shop_card_rules}` の展開内容が変わる（上記に連動） |
| 3 | `shop_search_result` イベントの `response` フィールド活用 | チャットテキスト表示をREST版のresponseで確実に行う |
| 4 | ウェイティングアニメーション（LiveAPI版） | function calling検知時に `shop_search_start` イベント送信 → 既存の待機オーバーレイを表示 |
| 5 | `shop_search_result` ハンドラ | 待機オーバーレイの非表示処理を追加 |

---

## 3. 詳細設計

### 3.1 プロンプト修正: `LIVEAPI_SHOP_CARD_RULES` → `LIVEAPI_SHOP_SEARCH_RULES`

**修正前**（現在のコード `live_api_handler.py` L42-107）:

LiveAPIに対してJSON形式での出力を指示するルール。message構造、予算表記の使い分け（漢数字/アラビア数字）、ショップカード用フィールド一式の出力を要求。

**修正後**:

LiveAPIには「function callingでsearch_shopsを呼ぶ」ことだけを指示し、ショップカードJSON生成の責任はREST版に委ねる。

```python
LIVEAPI_SHOP_SEARCH_RULES = """
## ★★★ ショップ検索ルール（絶対厳守） ★★★

お店を探すと判断したら、必ず search_shops ツールを呼び出すこと。
「お探ししますね」「お調べしますね」と音声で言うだけでは検索は実行されない。
search_shops ツールを実際に呼び出すこと（ツール呼び出し＝function calling）。

### search_shopsの呼び出し方
- search_shops(user_request="六本木 接待 イタリアン 1万円 4名") のように呼び出す
- user_request にはユーザーの要望を自然言語で要約して渡す

### 重要な注意
- ショップカードの表示はシステムが自動で行う。あなたはJSON出力をする必要はない
- search_shopsを呼び出した後は、検索結果の紹介が自動で始まるので、静かに待つこと
- お店の紹介は別のセッションで行われるので、あなたが紹介する必要はない
- 深掘り質問への回答（既提案店についての質問）は自然な音声で回答してよい
"""
```

**変更のポイント**:

| 項目 | 修正前 | 修正後 |
|------|--------|--------|
| JSON出力指示 | あり（JSON構造、messageフィールド、予算表記ルール等を詳細記述） | **なし**（LiveAPIはJSON出力不要） |
| function calling指示 | なし（別セクション§5で定義） | **統合**（search_shopsの呼び出しルールをここに集約） |
| ショップカードフィールド定義 | LiveAPIプロンプト内に記述 | **削除**（REST版のSupportAssistantが管理） |
| 検索後の動作指示 | なし | **追加**（「静かに待つ」ルールを明示） |

### 3.2 データフローの明確化

```
[会話フェーズ] ─────────────────────────────────────────────
  ブラウザ → マイク音声 → Socket.IO → LiveAPI(Gemini)
  ブラウザ ← 音声応答   ← Socket.IO ← LiveAPI(Gemini)
  ブラウザ ← ai_transcript(チャット表示) ← Socket.IO

[検索トリガー] ─────────────────────────────────────────────
  LiveAPI(Gemini) が search_shops を function calling
    │
    ├── Socket.IO: shop_search_start → ブラウザで待機アニメーション表示
    │
    ▼
[ショップデータ取得 — REST版が担当]（数秒〜10秒+）──────
  shop_search_callback()
    ├── SupportAssistant.process_user_message()
    │   └── Gemini REST API (gemini-2.5-flash) でJSON応答取得
    │       → shops配列（name, area, category, description,
    │         rating, priceRange, highlights, tips 等）
    │       → response テキスト（チャット表示用メッセージ）
    │
    ├── enrich_shops_with_photos()
    │   ├── Google Places API → 写真URL, 評価, レビュー数, 住所, maps_url
    │   ├── HotPepper API → hotpepper_url（日本国内・日本語の場合）
    │   ├── TripAdvisor API → tripadvisor_url, rating, reviews（海外の場合）
    │   └── 食べログ/ぐるなび → tabelog_url, gnavi_url（日本国内の場合）
    │
    └── 結果を返却: { shops: [...], response: "..." }

[ブラウザへの送信] ──────────────────────────────────────────
  Socket.IO: shop_search_result
    ├── 待機アニメーション非表示（hideWaitOverlay）
    ├── shops: [...] → ShopCardList.astro でカード描画
    └── response: "..." → addMessage('assistant', ...) でチャットテキスト表示

[お店説明の読み上げ — LiveAPIが担当] ──────────────────────
  _describe_shops_via_live(shops)
    ├── ショップ1: LiveAPI再接続 → 音声で紹介 → live_audio + ai_transcript
    ├── ショップ2: LiveAPI再接続 → 音声で紹介 → live_audio + ai_transcript
    ├── ...
    └── ショップN: LiveAPI再接続 → 音声で紹介 → live_audio + ai_transcript
  → 通常会話に復帰
```

### 3.3 ショップカードJSONの生成元（REST版 — 変更なし）

ショップカードに表示するJSONデータは、REST版の `SupportAssistant.process_user_message()` が生成する。
このフローは `shop_search_callback()` 内で既に実装済みであり、変更不要。

**REST版が生成するショップカードJSON構造**（`support_core.py` 内のプロンプトで定義）:

```json
{
  "message": "ユーザーへのメッセージ全文（A:導入 + B:店舗リスト + C:会話の導き）",
  "shops": [
    {
      "name": "正式な店舗名",
      "area": "最寄り駅またはエリア名",
      "category": "料理ジャンル",
      "description": "料理内容・体験価値・雰囲気を含む要約（2〜3文）",
      "rating": 4.5,
      "reviewCount": 150,
      "priceRange": "ランチ1,500円〜、ディナー6,000円〜8,000円",
      "location": "最寄り駅・エリア",
      "highlights": ["看板メニューや特徴", "雰囲気や設備の特徴", "利用シーンの特徴"],
      "tips": "来店時のおすすめポイント"
    }
  ]
}
```

**`enrich_shops_with_photos()` で追加されるフィールド**:

```json
{
  "image": "Google Places 写真URL",
  "rating": 4.5,
  "reviewCount": 280,
  "location": "〒150-0042 東京都渋谷区...",
  "maps_url": "https://www.google.com/maps/place/?q=place_id:...",
  "phone": "+81-3-1234-5678",
  "hotpepper_url": "https://www.hotpepper.jp/...",
  "tabelog_url": "https://tabelog.com/tokyo/...",
  "gnavi_url": "https://www.google.com/search?q=...",
  "tripadvisor_url": "https://www.tripadvisor.com/...",
  "tripadvisor_rating": 4.0,
  "tripadvisor_reviews": 150,
  "latitude": 35.6762,
  "longitude": 139.6503
}
```

### 3.4 チャットテキスト表示（REST版のresponseフィールド）

REST版の `SupportAssistant.process_user_message()` が返す `response` フィールドのテキストを、そのままチャットエリアに表示する。

**現在の実装（変更不要）**:

```
# バックエンド（live_api_handler.py L723-726）
self.socketio.emit('shop_search_result', {
    'shops': shops,
    'response': response_text,  ← REST版が生成したテキスト
}, room=self.client_sid)

# フロントエンド（core-controller.ts L377-379）
if (data.response) {
    this.addMessage('assistant', data.response);  ← チャットに表示
}
```

このテキストには以下が含まれる:
- A. 導入部: 「おすすめの5軒はこちらです。」
- B. 店舗リスト: 各店を「**店舗名**（駅名）- 説明」形式で記載
- C. 会話の導き: 状況に応じた自然な問いかけ

### 3.5 お店説明の読み上げ（LiveAPI — 変更なし）

`_describe_shops_via_live()` によるTTS読み上げフローは一切変更しない。

- 1軒ごとにLiveAPIセッションを再接続
- ショップ情報をシステムプロンプトに注入
- 自然な話し言葉で紹介（3〜5文）
- `live_audio` イベントで音声データ送信
- `ai_transcript` イベントで読み上げテキスト送信

### 3.6 ウェイティングアニメーション（REST版からの移植）

#### 3.6.1 背景

REST版では、`/api/chat` へのリクエスト送信後、レスポンス到着までの待ち時間にウェイティングアニメーションを表示している。

| モード | 表示タイミング | 非表示タイミング |
|--------|-------------|---------------|
| Chat（REST版） | `/api/chat` 送信の **4秒後** | レスポンス受信時 |
| Concierge（REST版） | `/api/chat` 送信の **6.5秒後** | レスポンス受信時 |

LiveAPI版では、`search_shops` function calling検知後にREST callbackでショップデータ取得 + `enrich_shops_with_photos()` による外部API補完処理が走る。この処理には数秒〜10秒以上かかる可能性があるが、現状この間のフィードバックがない。

#### 3.6.2 既存のUI部品（変更なし・そのまま流用）

`GourmetChat.astro` / `Concierge.astro` に実装済みのオーバーレイをそのまま使用する:

```html
<!-- GourmetChat.astro L18-25 / Concierge.astro L21-28 -->
<div class="wait-overlay hidden" id="waitOverlay">
  <div class="wait-content">
    <video id="waitVideo" class="wait-video" muted playsinline loop>
      <source src="/wait.mp4" type="video/mp4">
    </video>
    <p class="wait-text">Thinking...</p>
  </div>
</div>
```

- 白半透明オーバーレイ（`rgba(255,255,255,0.95)`）がチャットエリアを覆う
- ループ動画（`wait.mp4`）+ テキスト表示
- `hidden` クラスの付け外しで fade in/out（`opacity 0.5s`）
- テキストは `updateUILanguage()` で多言語対応済み（`i18n.ts` の `waitMessage`）

```
ja: 'AIがお店を検索しています...'
en: 'AI is searching for restaurants...'
zh: 'AI正在搜索餐厅...'
ko: 'AI가 레스토랑을 검색하고 있습니다...'
```

既存の `showWaitOverlay()` / `hideWaitOverlay()` メソッド（`core-controller.ts` L995-1004）もそのまま使用する:

```typescript
// core-controller.ts L995-1004（変更なし）
protected showWaitOverlay() {
    this.els.waitOverlay.classList.remove('hidden');
    this.els.waitVideo.currentTime = 0;
    this.els.waitVideo.play().catch((e: any) => console.log('Video err', e));
}

protected hideWaitOverlay() {
    if (this.waitOverlayTimer) { clearTimeout(this.waitOverlayTimer); this.waitOverlayTimer = null; }
    this.els.waitOverlay.classList.add('hidden');
    setTimeout(() => this.els.waitVideo.pause(), 500);
}
```

#### 3.6.3 バックエンド修正: `shop_search_start` イベント送信

`_handle_tool_call()` 内で `search_shops` の function calling を検知した時点で、ショップ検索開始をブラウザに通知する。

```python
# live_api_handler.py _handle_tool_call() 内に追加
async def _handle_tool_call(self, tool_call, session):
    for fc in tool_call.function_calls:
        if fc.name == "search_shops":
            user_request = fc.args.get("user_request", "")
            logger.info(f"[LiveAPI] search_shops呼び出し: '{user_request}'")

            # ★ 追加: 検索開始をブラウザに通知 → 待機アニメーション表示
            self.socketio.emit('shop_search_start', {},
                               room=self.client_sid)

            # ショップ検索を実行（既存のまま）
            await self._handle_shop_search(user_request)

            # function responseを返す（既存のまま）
            tool_response = types.LiveClientToolResponse(...)
            await session.send_tool_response(tool_response)
```

#### 3.6.4 フロントエンド修正: イベントハンドラ追加

`setupSocketListeners()` 内に2つの変更を追加する。

**追加1: `shop_search_start` ハンドラ — 待機アニメーション表示**

```typescript
// core-controller.ts setupSocketListeners() 内に追加
this.socket.on('shop_search_start', () => {
    console.log('[LiveAPI] shop_search_start: 待機アニメーション表示');
    this.showWaitOverlay();
});
```

**追加2: `shop_search_result` ハンドラ — 待機アニメーション非表示**

既存の `shop_search_result` ハンドラの先頭に `hideWaitOverlay()` を追加する。

```typescript
// core-controller.ts 既存の shop_search_result ハンドラを修正
this.socket.on('shop_search_result', (data: any) => {
    console.log('[LiveAPI] shop_search_result:', data?.shops?.length || 0, '件');

    // ★ 追加: 待機アニメーション非表示
    this.hideWaitOverlay();

    const shops = data?.shops || [];
    if (shops.length > 0) {
        // ... 既存のカード表示処理（変更なし）
    }
});
```

#### 3.6.5 REST版との違い

| 項目 | REST版 | LiveAPI版（本仕様） |
|------|--------|------------------|
| 表示トリガー | タイマー（4秒/6.5秒後） | `shop_search_start` イベント受信で**即座に表示** |
| 非表示トリガー | `/api/chat` レスポンス受信 | `shop_search_result` イベント受信 |
| 遅延表示 | あり（短時間で返った場合は非表示） | **なし**（function calling検知 = 検索確定なので即表示） |

**即座に表示する理由**:
- REST版のタイマーは「LLM応答が高速な場合にアニメを出さない」ための工夫
- LiveAPI版では `search_shops` function calling検知 = 必ずショップ検索が実行される（キャンセルなし）
- REST callback + `enrich_shops_with_photos()` は確実に数秒かかるため、遅延なしで即表示が適切

#### 3.6.6 タイムライン

```
t=0   search_shops function calling 検知
      → shop_search_start 送信 → 待機アニメーション表示
      │
      │  [バックエンド処理中]
      │  ├── SupportAssistant.process_user_message() ... 2-5秒
      │  └── enrich_shops_with_photos() ............. 3-8秒
      │      ├── Google Places API × 5店舗
      │      ├── HotPepper API / TripAdvisor API
      │      └── 重複チェック・フィルタリング
      │
t=5~13秒  shop_search_result 送信
           → 待機アニメーション非表示
           → ショップカード表示 + チャットテキスト表示
      │
t=5~13秒+  _describe_shops_via_live() 開始
            → LiveAPIで1軒ずつ音声紹介
```

---

## 4. 役割分担の整理

### 4.1 LiveAPI の責務（音声・会話）

| 責務 | 説明 |
|------|------|
| 音声入力の処理 | マイク → AudioWorklet → PCM 16kHz → LiveAPI |
| 会話応答（音声） | ヒアリング、質問、雑談等の音声応答 |
| ショップ検索の判断 | 会話から条件が揃ったか判断 → function calling |
| お店説明の読み上げ | `_describe_shops_via_live()` による音声紹介 |
| トランスクリプション | `ai_transcript`, `user_transcript` の送信 |

**LiveAPIがやらないこと**:
- ショップカード用JSONの生成（REST版の責務）
- チャットテキスト表示用メッセージの生成（REST版の責務）
- 外部API（Google Places, HotPepper等）の呼び出し（REST版の責務）

### 4.2 REST版（SupportAssistant）の責務（データ・表示）

| 責務 | 説明 |
|------|------|
| ショップカードJSON生成 | Gemini REST API (gemini-2.5-flash) でshops配列を取得 |
| チャットテキスト生成 | responseフィールドに表示用メッセージを含める |
| ショップデータ補完 | `enrich_shops_with_photos()` で写真・評価・URL等を追加 |
| セッション管理 | `SupportSession` による会話履歴・状態管理 |

**REST版がやらないこと**:
- 音声生成（TTS）
- 音声入力の認識（STT）
- リアルタイム会話

### 4.3 対比表

```
┌─────────────────────────┬──────────────┬──────────────┐
│         機能             │   LiveAPI     │   REST版     │
├─────────────────────────┼──────────────┼──────────────┤
│ 会話（音声入出力）       │     ●        │              │
│ ショップ検索トリガー      │     ●        │              │
│ ショップカードJSON生成    │              │      ●       │
│ チャットテキスト生成      │              │      ●       │
│ ショップデータ補完        │              │      ●       │
│ お店説明の読み上げ(TTS)  │     ●        │              │
│ トランスクリプション表示  │     ●        │              │
└─────────────────────────┴──────────────┴──────────────┘
```

---

## 5. 修正対象ファイルと変更内容

### 5.1 `support-base/live_api_handler.py`

| 行番号 | 変更内容 |
|--------|---------|
| L42-107 | `LIVEAPI_SHOP_CARD_RULES` → `LIVEAPI_SHOP_SEARCH_RULES` に書き換え（§3.1参照） |
| L126 | `{shop_card_rules}` → `{shop_search_rules}` に変数名変更 |
| L220 | `{shop_card_rules}` → `{shop_search_rules}` に変数名変更 |
| L244-245 | `shop_card_rules=LIVEAPI_SHOP_CARD_RULES` → `shop_search_rules=LIVEAPI_SHOP_SEARCH_RULES` |
| L248-249 | 同上 |
| L680付近 | `_handle_tool_call()` 内に `shop_search_start` イベント送信を追加（§3.6.3参照） |

**変更しないもの**: `_handle_shop_search()`, `_describe_shops_via_live()`, `_receive_shop_description()`, 再接続関連コード全般

### 5.2 `support-base/app_customer_support.py`

**変更なし。** `shop_search_callback()` は既にREST版SupportAssistantを使ってデータ取得しており、そのまま維持。

### 5.3 `support-base/support_core.py`

**変更なし。** `SupportAssistant.process_user_message()` および内部のプロンプト（JSON出力ルール）はREST版の安定版として維持。

### 5.4 `src/scripts/chat/core-controller.ts`

| 行番号 | 変更内容 |
|--------|---------|
| `setupSocketListeners()` 内 | `shop_search_start` ハンドラを追加: `this.showWaitOverlay()` 呼び出し（§3.6.4 追加1） |
| L365付近 | `shop_search_result` ハンドラの先頭に `this.hideWaitOverlay()` を追加（§3.6.4 追加2） |

**変更しないもの**: `showWaitOverlay()` / `hideWaitOverlay()` メソッド本体、カード表示ロジック、チャットテキスト表示ロジック

### 5.5 `src/components/ShopCardList.astro`

**変更なし。** `displayShops` カスタムイベント経由でショップデータを受け取り、カードを描画するロジックは維持。

---

## 6. 実装上の注意事項

### 6.1 LiveAPIのfunction calling後の挙動

`search_shops` のfunction callingが発火した後、LiveAPIは `send_tool_response()` でレスポンスを受け取る。
その後のLiveAPIの応答（もしあれば）は、すでにショップカードとチャットテキストがREST版から表示された後になる。

→ プロンプトで「search_shopsを呼び出した後は静かに待つ」と指示することで、
LiveAPIが追加の音声応答を生成してユーザーを混乱させることを防ぐ。

### 6.2 `_describe_shops_via_live()` のタイミング

`_handle_shop_search()` 内で以下の順序が守られる（既存実装のまま）:

1. `shop_search_result` イベント送信 → ブラウザでカード＋チャットテキスト表示
2. `_describe_shops_via_live()` 呼び出し → LiveAPIで1軒ずつ音声紹介

この順序により、ユーザーはまずカードを視覚的に確認し、その後に音声での詳細説明を聞くことができる。

### 6.3 REST版のJSONプロンプトとの関係

REST版（`support_core.py`）のプロンプトには、ショップカード用のJSON出力ルールが定義されている。
これはREST版のGemini API呼び出し時にのみ使用されるため、LiveAPI側のプロンプト変更とは独立している。

```
LiveAPI プロンプト: search_shopsの呼び出しルールのみ（JSONは出力しない）
REST版 プロンプト: ショップカードJSON出力ルール（shops配列 + messageフィールド）
→ 干渉しない。それぞれが自分の責務だけ担当する。
```

---

## 7. テスト観点

### 7.1 ショップカード表示

- [ ] function calling発火後、REST版から取得したshops配列でカードが正しく表示されるか
- [ ] Google Places写真、評価、レビュー数が表示されるか
- [ ] 外部リンク（HotPepper, 食べログ, ぐるなび, TripAdvisor, Google Maps）が正しく表示されるか
- [ ] highlights, tips, priceRange 等のフィールドが正しく表示されるか

### 7.2 チャットテキスト表示

- [ ] REST版のresponseフィールドがチャットエリアに表示されるか
- [ ] 店舗名の太字表記（**店舗名**）が反映されるか
- [ ] 導入部 + 店舗リスト + 会話の導き の構成で表示されるか

### 7.3 TTS読み上げ

- [ ] `_describe_shops_via_live()` で各店の音声紹介が再生されるか
- [ ] 1軒ごとの再接続が正常に動作するか
- [ ] 最後の店の紹介後「以上、N軒のお店をご紹介しました」で締められるか
- [ ] 読み上げ中の `ai_transcript` がチャットに表示されるか

### 7.4 ウェイティングアニメーション

- [ ] `search_shops` function calling検知時に待機アニメーションが即座に表示されるか
- [ ] 待機動画（`wait.mp4`）が再生されるか
- [ ] 待機テキスト（「AIがお店を検索しています...」）が表示されるか
- [ ] `shop_search_result` 受信時に待機アニメーションが非表示になるか
- [ ] 言語切替時に待機テキストが正しい言語で表示されるか（ja/en/zh/ko）
- [ ] 待機アニメーション表示中にショップカードが到着した場合、スムーズに切り替わるか
- [ ] 検索エラー時（shopsが空の場合等）でも待機アニメーションが非表示になるか

### 7.5 LiveAPIプロンプト変更の影響

- [ ] `search_shops` function callingが正常に発火するか
- [ ] LiveAPIがJSON出力を試みないか（音声のみの応答になるか）
- [ ] function calling後にLiveAPIが余計な音声応答を生成しないか

### 7.6 会話フロー全体

- [ ] グルメモード: 条件指定 → search_shops発火 → **待機アニメ表示** → カード表示 → 読み上げ → 通常会話復帰
- [ ] コンシェルジュモード: ヒアリング → 条件確定 → search_shops発火 → **待機アニメ表示** → カード表示 → 読み上げ → 通常会話復帰
- [ ] 再検索: 「もっと安いところ」等 → 再度search_shops発火 → **待機アニメ表示** → 新しいカード表示

---

## 8. まとめ

### 本仕様の本質

**「表示はREST、音声はLiveAPI」という役割分離の明確化。**

- コード変更の実態は `LIVEAPI_SHOP_CARD_RULES` プロンプトの書き換え + ウェイティングアニメーション対応
- バックエンドのデータフロー（`shop_search_callback` → REST版 → `enrich_shops_with_photos`）は変更なし
- フロントエンドのカード表示・チャットテキスト表示ロジックは変更なし
- LiveAPIのTTS読み上げフローは変更なし
- ウェイティングアニメーションは既存UI部品（`waitOverlay`、`showWaitOverlay()`/`hideWaitOverlay()`）を流用

### 変更の規模

```
変更ファイル: 2ファイル
  1. live_api_handler.py:
     - プロンプト書き換え（L42-107 + L126,220,244-249の変数名参照）
     - _handle_tool_call()に shop_search_start イベント送信を1行追加
  2. core-controller.ts:
     - shop_search_start ハンドラ追加（3行）
     - shop_search_result ハンドラに hideWaitOverlay() を1行追加
```

### なぜこの方式が有効か

1. **REST版は安定版**: `gourmet-support`（移植元）で実績があり、JSON出力精度が高い
2. **LiveAPIは音声に特化**: `response_modalities: ["AUDIO"]` で音声に最適化されている
3. **既存実装の活用**: `shop_search_callback()` 内で既にREST版を使っているため、新規実装は不要
4. **プロンプト変更のみ**: LiveAPIにJSON出力を求めないことで、役割の矛盾を解消

---

*以上がショップカード＆チャットテキスト表示用JSON形式対応の修正仕様書。*
*実装変更はプロンプト書き換えのみ。データフローは既存のまま。*
