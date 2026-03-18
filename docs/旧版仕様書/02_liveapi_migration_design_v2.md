# Gemini LiveAPI 移植設計書 v2（gourmet-sp3）

> **作成日**: 2026-03-11
> **前版**: `docs/02_liveapi_migration_design.md`（v1: 2026-03-10）
> **前提文書**: `docs/01_stt_stream_detailed_spec.md`
> **成功事例**: `docs/stt_stream.py`（インタビューモードの再接続方式）
> **v2変更理由**: ショップ説明のREST API + GCP TTS方式を全面廃止し、LiveAPI統一に変更

---

## v1 → v2 変更概要

| 項目 | v1（旧） | v2（新） |
|---|---|---|
| ショップ説明の音声 | REST API検索 → GCP TTS読み上げ | **LiveAPIで直接読み上げ** |
| 声の一貫性 | 会話中にLiveAPI音声→GCP TTS音声に切り替わり違和感あり | **全編同じLiveAPI音声で統一** |
| 累積文字数制限の回避 | REST切替で回避 | **ショップ1軒ごとに再接続で回避**（stt_stream.py方式） |
| ブラウザAutoplay問題 | `isUserInteracted`フラグ依存でTTSが動かない | **LiveAPI音声は常に再生可能（AudioWorklet経由）で問題なし** |
| `speakTextGCP()` | ショップ説明に使用 | **廃止**（LiveAPI統一のため不要。LiveAPI全体が落ちた場合は`live_fallback`でRESTモードに切り替わり、そちらのTTS処理が動く） |

---

## 0. Claudeへの厳守事項（v1から継続）

### 防止ルール

1. **修正する前に、必ず `01_stt_stream_detailed_spec.md` の該当セクションを `Read` ツールで読む**
2. **「確認しました」と報告する場合、確認したファイルパスと行番号を明記する**
3. **仕様書に記載がない機能を追加しない**
4. **推測で API の引数やメソッド名を変えない**
5. **困ったらユーザーに聞く。推測で進めない**

---

## 1. 移植のスコープ（v2改訂）

### 1.1 やること

| # | 内容 | 優先度 | v2変更 |
|---|---|---|---|
| 1 | バックエンド: LiveAPI WebSocketプロキシの新設 | 必須 | 変更なし |
| 2 | フロントエンド: LiveAudioManager の実装 | 必須 | 変更なし |
| 3 | LiveAPI → REST API フォールバック機構 | 必須 | 変更なし |
| 4 | セッション再接続メカニズム | 必須 | 変更なし |
| 5 | トランスクリプション（文字起こし）表示 | 必須 | 変更なし |
| 6 | **ショップ説明のLiveAPI読み上げ（1軒ごとに再接続）** | **必須** | **v2新規** |

### 1.2 やらないこと（v2改訂）

- ~~REST APIエンドポイントの維持~~ → **ショップ説明用のREST TTS呼び出しは廃止**
- `/api/chat` エンドポイント自体は**ショップデータ取得用に残す**（音声生成はしない）
- `/api/tts/synthesize` はフォールバック時のみ使用（通常フローでは使わない）
- PyAudio関連の移植（ブラウザにはPyAudioがない）

---

## 2. 全体アーキテクチャ（v2改訂）

### 2.1 v2アーキテクチャ（LiveAPI完全統一）

```
ブラウザ
├── マイク → AudioWorklet → PCM 16kHz → Socket.IO → サーバー
│                                                      │
│                                                      ├── Gemini LiveAPI（音声→音声）
│                                                      │   ├── 通常会話（低遅延音声応答）
│                                                      │   ├── ショップ説明（再接続+コンテキスト注入）
│                                                      │   ├── input_transcription
│                                                      │   └── output_transcription
│                                                      │
│                                                      └── [データ取得のみ] REST API
│                                                          └── /api/chat → shops JSON取得
│                                                             （音声生成はしない）
│
└── スピーカー ← AudioWorklet再生キュー ← PCM 24kHz ← Socket.IO
    チャット欄 ← transcription テキスト ← Socket.IO
```

### 2.2 v1との比較

```
v1: LiveAPI(会話) → shop_search_trigger → REST API(/api/chat) → GCP TTS → HTMLAudioElement
                                           ↑ここで声が変わる    ↑isUserInteracted問題

v2: LiveAPI(会話) → shop_search_trigger → REST API(データのみ) → LiveAPI再接続(説明読み上げ)
                                           ↑JSONデータだけ取得    ↑同じLiveAPI音声で統一
```

**v2で解決される問題**:
1. ブラウザAutoplay制限（`isUserInteracted`問題）→ LiveAPI音声はAudioWorklet経由で再生されるため影響なし
2. 声質の違和感 → 全編LiveAPI音声で統一
3. GCP TTSのレイテンシ → LiveAPIのストリーミング音声で即時再生

---

## 3. バックエンド設計（v1セクション3から変更なし）

v1のセクション3（LiveAPISession クラス、app_customer_support.py統合）はそのまま維持。

変更点は**セクション5（ショップ提案フロー）**に集約。

---

## 4. フロントエンド設計（v1セクション4から一部変更）

v1のセクション4.1〜4.5（LiveAudioManager、Socket.IOイベント、起動フロー、初期あいさつ）はそのまま維持。

### 4.6 ショップ説明関連の変更（v2新規）

#### 4.6.1 廃止するもの

- `handleShopSearchFromLiveAPI()` メソッド全体
- `sendMessage()` 内のショップ説明TTS読み上げロジック（`ttsIntro` + `shopLines` 分割再生）
- ショップ説明における `speakTextGCP()` 呼び出し全般
- ショップ説明における `isUserInteracted` チェック
- `shop_search_trigger` Socket.IOリスナー

#### 4.6.2 維持するもの（REST APIモード用）

- `speakTextGCP()` メソッド自体 → `live_fallback` 発動時のREST APIモードで使用
- `isUserInteracted` / `enableAudioPlayback()` → REST APIモード時のTTSで使用
- `ttsPlayer`（HTMLAudioElement）→ REST APIモード時に使用
- **注意**: これらは通常フロー（LiveAPI統一）では一切使用しない。LiveAPI全体が接続不能になった場合の`live_fallback`でRESTモードに切り替わった時のみ稼働する

#### 4.6.3 新規追加

- `handleShopDescriptionFromLiveAPI()` — LiveAPIからのショップ説明音声を受信・表示するハンドラ
  - 詳細はセクション5.5で定義

---

## 5. ショップ提案の処理フロー（v2全面改訂）

### 5.1 設計方針

**stt_stream.py の成功パターンを踏襲**:
- stt_stream.py は累積文字数制限（800文字）を**適切なタイミングでの再接続**で回避している
- 同じ手法で、ショップ説明（長文）も1軒ごとに再接続することで制限を回避する
- REST APIは**ショップデータ（JSON）の取得のみ**に使用し、音声生成には使わない

### 5.2 処理フロー全体図

```
ブラウザ                     サーバー                           Gemini
  │                            │                                │
  │ [通常会話中 - LiveAPIセッション#N]                           │
  │ ◄──────────────────────── AI音声応答 ◄──────────────────── │
  │                            │                                │
  │                            │  AI: 「お探ししますね」         │
  │                            │  → output_transcription検知    │
  │                            │  → should_trigger_shop_search()│
  │                            │                                │
  │                            │── ① LiveAPIセッション停止 ──→ │ (切断)
  │                            │                                │
  │                            │── ② REST API で店舗検索 ────→ │ (Gemini REST)
  │                            │◄── shops JSON ────────────── │
  │                            │                                │
  │ ◄── shop_search_result ── │  ③ ショップカードデータ送信     │
  │  (shops JSON)              │                                │
  │  → ショップカード表示      │                                │
  │                            │                                │
  │                            │── ④ LiveAPI再接続 ───────────→│ (新セッション)
  │                            │   system_prompt に              │
  │                            │   ショップ1の詳細を注入         │
  │                            │                                │
  │                            │── send_client_content() ─────→│
  │                            │   「1軒目のお店を紹介して」     │
  │                            │                                │
  │ ◄── live_audio ────────── │◄── AI音声(ショップ1説明) ◄───│
  │ ◄── ai_transcript ─────── │◄── 文字起こし ◄──────────── │
  │  → 音声再生 + チャット表示  │                                │
  │                            │                                │
  │                            │── turn_complete ──────────────│
  │                            │                                │
  │                            │── ⑤ LiveAPI再接続 ───────────→│ (新セッション)
  │                            │   system_prompt に              │
  │                            │   ショップ2の詳細を注入         │
  │                            │                                │
  │                            │── send_client_content() ─────→│
  │                            │   「2軒目のお店を紹介して」     │
  │                            │                                │
  │ ◄── live_audio ────────── │◄── AI音声(ショップ2説明) ◄───│
  │                            │                                │
  │         ... (ショップN軒目まで繰り返し) ...                  │
  │                            │                                │
  │                            │── ⑥ 全ショップ説明完了         │
  │                            │── LiveAPI再接続 ─────────────→│ (通常会話に復帰)
  │                            │   「説明が終わりました。        │
  │                            │    気になるお店はありましたか？」│
  │                            │                                │
  │ ◄── live_audio ────────── │◄── AI音声 ◄──────────────── │
  │  → 通常会話に戻る          │                                │
```

### 5.3 ショップ検索トリガー（v1から変更なし）

```python
SHOP_TRIGGER_KEYWORDS = [
    'お探ししますね', 'お調べしますね', '探してみますね',
    'ご紹介しますね',
    # 丁寧形（コンシェルジュモード対応）
    'お探しいたしますね', 'お調べいたしますね', '探してまいりますね',
    'ご紹介いたしますね',
    # 部分一致用
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
```

### 5.4 サーバー側ショップ説明フロー（v2新規）

#### 5.4.1 ショップ検索 → データ取得

```python
async def _handle_shop_search(self):
    """
    ショップ検索トリガー検知後の処理

    【v2変更点】
    - REST APIはショップデータ(JSON)の取得のみに使用
    - 音声説明はLiveAPI再接続で行う
    """
    user_request = self.user_transcript_buffer.strip()
    logger.info(f"[ShopSearch] 検索開始: '{user_request}'")

    # ① 現在のLiveAPIセッションを停止
    self.needs_reconnect = False  # 通常再接続を抑制
    self.is_running = False       # session_loopを終了させる

    # ② REST APIでショップデータを取得（音声生成なし、データのみ）
    #    既存の /api/chat エンドポイントを内部的に呼び出す
    shop_data = await self._fetch_shop_data(user_request)

    if not shop_data or not shop_data.get('shops'):
        # ショップが見つからない場合 → 通常会話に復帰
        await self._restart_live_session_with_message(
            "検索しましたが、条件に合うお店が見つかりませんでした。"
            "条件を変えてもう一度お探ししましょうか？"
        )
        return

    shops = shop_data['shops']
    response_text = shop_data.get('response', '')

    # ③ ショップカードデータをブラウザに送信（表示用）
    self.socketio.emit('shop_search_result', {
        'shops': shops,
        'response': response_text,  # チャット欄表示用テキスト
    }, room=self.client_sid)

    # ④ ショップ説明をLiveAPIで1軒ずつ読み上げ
    await self._describe_shops_via_live(shops, response_text)
```

#### 5.4.2 ショップ1軒ごとのLiveAPI再接続

```python
async def _describe_shops_via_live(self, shops: list, intro_text: str):
    """
    ショップ説明をLiveAPIで読み上げ（1軒ごとに再接続）

    【設計根拠】
    stt_stream.py の再接続メカニズム（セクション8）と同じ手法:
    - 累積文字数制限(800文字)を回避するため、適切なタイミングで再接続
    - 再接続時に system_prompt にコンテキストを注入
    - send_client_content() でトリガーメッセージを送信

    【再接続単位】
    - 基本: 1軒ごとに再接続（1軒の説明が300〜500文字程度を想定）
    - 予備案: 1軒の説明が長すぎる場合、2分割も検討
    """
    total = len(shops)

    for i, shop in enumerate(shops):
        shop_number = i + 1
        is_last = (shop_number == total)

        # ショップ情報をテキスト化（system_promptに注入する用）
        shop_context = self._format_shop_for_prompt(shop, shop_number, total)

        # system_prompt にショップ情報を注入して再接続
        shop_instruction = self.system_prompt + f"""

【現在のタスク：ショップ紹介】
あなたは今、ユーザーに検索結果のお店を紹介しています。

{shop_context}

【読み上げルール】
1. このお店の特徴を自然な話し言葉で紹介する（3〜5文程度）
2. 店名、ジャンル、エリア、特徴、価格帯を含める
3. マークダウン記法は使わない（音声出力のため）
4. 「{shop_number}軒目は」から始める
5. 紹介が終わったら「以上です」で締める
6. {"最後のお店です。紹介後「以上、" + str(total) + "軒のお店をご紹介しました。気になるお店はありましたか？」で締めてください。" if is_last else "次のお店の紹介に続きます。"}
"""

        try:
            config = self._build_config()
            # system_instructionをショップ説明用に差し替え
            config["system_instruction"] = shop_instruction

            async with self.client.aio.live.connect(
                model=LIVE_API_MODEL,
                config=config
            ) as session:
                # トリガーメッセージを送信
                trigger_text = f"{shop_number}軒目のお店を紹介してください。"
                if shop_number == 1 and intro_text:
                    trigger_text = f"検索結果を紹介してください。まず{shop_number}軒目のお店からお願いします。"

                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=trigger_text)]
                    ),
                    turn_complete=True
                )

                # 説明の音声応答を受信・転送
                await self._receive_shop_description(session, shop_number)

                logger.info(f"[ShopDesc] ショップ{shop_number}/{total} 説明完了")

        except Exception as e:
            logger.error(f"[ShopDesc] ショップ{shop_number}説明エラー: {e}")
            # エラー時はスキップして次のショップへ
            continue

    # ⑥ 全ショップ説明完了 → 通常会話に復帰
    await self._restart_normal_conversation(
        f"{total}軒のお店を紹介しました。気になるお店はありましたか？"
    )
```

#### 5.4.3 ショップ説明の音声受信

```python
async def _receive_shop_description(self, session, shop_number: int):
    """
    ショップ説明のLiveAPI応答を受信してブラウザに転送

    通常の _receive_and_forward() とほぼ同じだが:
    - input_transcription は転送しない（トリガーメッセージは表示不要）
    - turn_complete で終了（1ターンで完結）
    """
    turn = session.receive()
    async for response in turn:
        if response.server_content:
            sc = response.server_content

            # ターン完了 → この軒の説明終了
            if hasattr(sc, 'turn_complete') and sc.turn_complete:
                if self.ai_transcript_buffer.strip():
                    ai_text = self.ai_transcript_buffer.strip()
                    logger.info(f"[ShopDesc] #{shop_number}: {ai_text[:80]}...")
                    self.ai_transcript_buffer = ""

                self.socketio.emit('turn_complete', {
                    'type': 'shop_description',
                    'shop_number': shop_number,
                }, room=self.client_sid)
                return  # この軒の説明完了

            # 割り込み
            if hasattr(sc, 'interrupted') and sc.interrupted:
                self.ai_transcript_buffer = ""
                self.socketio.emit('interrupted', {}, room=self.client_sid)
                return

            # output_transcription（AI文字起こし → チャット欄に表示）
            if hasattr(sc, 'output_transcription') and sc.output_transcription:
                text = sc.output_transcription.text
                if text:
                    self.ai_transcript_buffer += text
                    self.socketio.emit('ai_transcript', {
                        'text': text,
                        'type': 'shop_description',
                        'shop_number': shop_number,
                    }, room=self.client_sid)

            # 音声データ → ブラウザで再生
            if sc.model_turn:
                for part in sc.model_turn.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        if isinstance(part.inline_data.data, bytes):
                            audio_b64 = base64.b64encode(
                                part.inline_data.data
                            ).decode('utf-8')
                            self.socketio.emit('live_audio', {
                                'data': audio_b64,
                            }, room=self.client_sid)

            # input_transcription は転送しない（トリガーメッセージ非表示）
```

#### 5.4.4 ショップ情報のテキスト化

```python
def _format_shop_for_prompt(self, shop: dict, number: int, total: int) -> str:
    """
    ショップデータをシステムプロンプト注入用テキストに変換

    /api/chat が返す shops 配列の各要素から情報を抽出
    """
    name = shop.get('name', '不明')
    genre = shop.get('genre', '')
    area = shop.get('area', '')
    budget = shop.get('budget', '')
    rating = shop.get('rating', '')
    description = shop.get('description', '')
    features = shop.get('features', [])
    access = shop.get('access', '')

    text = f"【{number}軒目 / 全{total}軒】\n"
    text += f"店名: {name}\n"
    if genre: text += f"ジャンル: {genre}\n"
    if area: text += f"エリア: {area}\n"
    if budget: text += f"予算: {budget}\n"
    if rating: text += f"評価: {rating}\n"
    if access: text += f"アクセス: {access}\n"
    if description: text += f"説明: {description}\n"
    if features: text += f"特徴: {', '.join(features)}\n"

    return text
```

#### 5.4.5 通常会話への復帰

```python
async def _restart_normal_conversation(self, summary_message: str):
    """
    ショップ説明完了後、通常のLiveAPI会話セッションに復帰

    会話履歴にショップ紹介済みの情報を含めて再接続
    """
    # 会話履歴にショップ紹介の記録を追加
    self._add_to_history("AI", summary_message)

    # 通常の再接続フローで復帰
    self.session_count += 1
    self.ai_char_count = 0
    context = self._get_context_summary()
    config = self._build_config(with_context=context)

    try:
        async with self.client.aio.live.connect(
            model=LIVE_API_MODEL,
            config=config
        ) as session:
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text="ありがとうございます。気になるお店について教えてください。")]
                ),
                turn_complete=True
            )

            # 通常の会話ループに復帰
            self.is_running = True
            self.needs_reconnect = False
            await self._session_loop(session)

    except Exception as e:
        logger.error(f"[ShopDesc] 通常会話復帰エラー: {e}")
        self.socketio.emit('live_fallback', {
            'reason': str(e)
        }, room=self.client_sid)

async def _restart_live_session_with_message(self, message: str):
    """ショップが見つからなかった場合等、メッセージ付きで再接続"""
    self.session_count += 1
    self.ai_char_count = 0
    context = self._get_context_summary()
    config = self._build_config(with_context=context)

    try:
        async with self.client.aio.live.connect(
            model=LIVE_API_MODEL,
            config=config
        ) as session:
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text=message)]
                ),
                turn_complete=True
            )
            self.is_running = True
            self.needs_reconnect = False
            await self._session_loop(session)
    except Exception as e:
        logger.error(f"[LiveAPI] 再接続エラー: {e}")
        self.socketio.emit('live_fallback', {
            'reason': str(e)
        }, room=self.client_sid)
```

### 5.5 フロントエンド側のショップ説明受信（v2新規）

#### 5.5.1 新規Socket.IOイベント: `shop_search_result`

```typescript
// core-controller.ts の initSocket() に追加

// ★ ショップ検索結果（カードデータ）受信
this.socket.on('shop_search_result', (data: any) => {
    console.log('[LiveAPI] shop_search_result:', data.shops?.length, '件');

    // ショップカード表示（既存ロジックを流用）
    if (data.shops && data.shops.length > 0) {
        this.currentShops = data.shops;
        this.els.reservationBtn.classList.add('visible');
        document.dispatchEvent(new CustomEvent('displayShops', {
            detail: { shops: data.shops, language: this.currentLanguage }
        }));
        const section = document.getElementById('shopListSection');
        if (section) section.classList.add('has-shops');

        if (window.innerWidth < 1024) {
            setTimeout(() => {
                const shopSection = document.getElementById('shopListSection');
                if (shopSection) shopSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 300);
        }
    }

    // テキスト応答をチャット欄に表示（あれば）
    if (data.response) {
        this.addMessage('assistant', data.response);
    }

    // ★ TTS読み上げは不要 — LiveAPIの音声がそのまま流れてくる
});
```

#### 5.5.2 ショップ説明音声の受信

ショップ説明の音声は通常の `live_audio` イベントとして届く。
既存の `live_audio` ハンドラがそのまま処理するため、**追加実装は不要**。

```typescript
// 既存のハンドラがそのまま使える
this.socket.on('live_audio', (data: any) => {
    if (!this.isLiveMode) return;
    this.liveAudioManager.onAiResponseStarted();
    this.liveAudioManager.playPcmAudio(data.data);
});
```

#### 5.5.3 handleShopSearchFromLiveAPI の廃止

v1の `handleShopSearchFromLiveAPI()` は以下を行っていた:
1. `terminateLiveSession()` — LiveAPI停止
2. REST API `/api/chat` 呼び出し — ショップデータ+テキスト取得
3. `speakTextGCP()` — GCP TTSで読み上げ

v2ではこれら全てがサーバー側で処理されるため、**このメソッドは廃止**。

代わりに `shop_search_result` イベントハンドラ（5.5.1）でショップカード表示のみ行い、
音声読み上げはLiveAPIからの `live_audio` で自動的に処理される。

### 5.6 ショップ検索トリガーの発火タイミング（v2変更）

#### v1の問題

v1では `shop_search_trigger` イベントをブラウザに送信し、ブラウザ側の
`handleShopSearchFromLiveAPI()` がREST API呼び出し+TTS読み上げを行っていた。

#### v2の変更

ショップ検索トリガーを検知したら、**サーバー側で全て処理**する:

```python
# _receive_and_forward() 内、turn_complete 処理時:
if hasattr(sc, 'turn_complete') and sc.turn_complete:
    self._process_turn_complete()

    # ★ ショップ検索トリガーチェック
    ai_text = self.ai_transcript_buffer.strip()
    if should_trigger_shop_search(ai_text):
        # サーバー側でショップ検索→LiveAPI説明の全フローを実行
        await self._handle_shop_search()
        return  # 現在のセッションループを終了

    self.socketio.emit('turn_complete', {}, room=self.client_sid)
```

**ブラウザ側の `shop_search_trigger` リスナーは廃止**。

### 5.7 累積文字数制限の管理

#### 5.7.1 基本方針: 1軒ごとに再接続

```
ショップ1説明 (推定300〜500文字) → 再接続 → リセット
ショップ2説明 (推定300〜500文字) → 再接続 → リセット
ショップ3説明 (推定300〜500文字) → 再接続 → リセット
```

MAX_AI_CHARS_BEFORE_RECONNECT = 800 に対して、1軒の説明は300〜500文字程度。
1軒ごとに再接続すれば、累積制限に到達することはない。

#### 5.7.2 予備案: 1軒の説明を2分割

万が一、1軒の説明が長すぎる場合（700文字超など）の対策:

```python
async def _describe_shops_via_live(self, shops, intro_text):
    for i, shop in enumerate(shops):
        shop_number = i + 1
        shop_context = self._format_shop_for_prompt(shop, shop_number, len(shops))

        # 説明テキストの推定文字数を事前チェック
        estimated_chars = len(shop_context)

        if estimated_chars > 600:
            # 長いショップ情報 → 2回に分割
            await self._describe_shop_split(shop, shop_number, len(shops))
        else:
            # 通常 → 1回で説明
            await self._describe_shop_single(shop, shop_number, len(shops))
```

**ただし、これは予備案であり、初期実装では1軒1再接続の基本方式のみ実装する。**
テストで文字数制限に引っかかる場合にのみ、分割方式を追加する。

---

## 6. セッション管理（v1セクション6に追加）

### 6.1 LiveAPIセッションのライフサイクル（v2改訂）

```
[通常会話]
LiveAPIセッション#1 (初回接続・挨拶)
  ↓ 累積制限 or 発話途切れ
LiveAPIセッション#2 (再接続・会話継続)
  ↓ ...
LiveAPIセッション#N (会話中にショップ検索トリガー)
  ↓
[ショップ検索]
REST API でショップデータ取得 (JSONのみ)
  ↓
[ショップ説明]
LiveAPIセッション#N+1 (ショップ1説明)
  ↓ 再接続
LiveAPIセッション#N+2 (ショップ2説明)
  ↓ 再接続
LiveAPIセッション#N+3 (ショップ3説明)
  ↓ 再接続
[通常会話復帰]
LiveAPIセッション#N+4 (「気になるお店はありましたか？」)
```

### 6.2 セッション状態遷移

```python
class SessionState(Enum):
    CONVERSATION = "conversation"           # 通常会話
    SHOP_SEARCHING = "shop_searching"       # ショップ検索中
    SHOP_DESCRIBING = "shop_describing"     # ショップ説明中
    RETURNING_TO_CHAT = "returning_to_chat" # 通常会話復帰中
```

---

## 7. 再接続メカニズム（v1セクション7に追加）

### 7.1 通常会話の再接続（v1と同じ）

1. **再接続トリガー**: 発言途切れ / 長い発話(500文字) / 累積上限(800文字)
2. **会話履歴引き継ぎ**: 直近10ターン、各150文字まで
3. **システムインストラクション再構築**: コンテキスト要約を注入
4. **再接続通知**: `send_client_content(text="続きをお願いします")`

### 7.2 ショップ説明の再接続（v2新規）

1. **再接続トリガー**: 1軒の説明完了（turn_complete）
2. **引き継ぎ情報**: 次のショップの詳細データ
3. **システムインストラクション**: ショップ紹介専用プロンプトに差し替え
4. **トリガーメッセージ**: `「N軒目のお店を紹介してください。」`

---

## 8. フォールバック戦略（v2改訂）

### 8.1 LiveAPI接続失敗時

v1と同じ。`live_fallback` イベントをブラウザに送信し、REST APIモードに切り替え。

### 8.2 ショップ説明中のLiveAPI再接続失敗時

```python
# ショップ説明のLiveAPI再接続が失敗した場合
except Exception as e:
    logger.error(f"[ShopDesc] ショップ{shop_number}説明エラー: {e}")
    # このショップをスキップして次へ
    # 全ショップ失敗した場合はフォールバック通知
    continue
```

最悪のケース（LiveAPIが完全に使えなくなった場合）は、
`live_fallback` でREST APIモード全体に切り替わる。
その場合は既存の `sendMessage()` → `speakTextGCP()` フローが稼働する。
ショップ説明「だけ」GCP TTSにフォールバックする中途半端なパスは設けない。

---

## 9. 実装フェーズ計画（v2改訂）

### Phase 1: 基盤構築（v1と同じ）
- LiveAPI接続 → 音声送受信 → ブラウザ再生の最小ループ確認

### Phase 2: トランスクリプション（v1と同じ）
- input/output_transcription → チャット欄表示

### Phase 3: ショップ説明のLiveAPI統一（v2改訂）

| # | 内容 | 詳細 |
|---|---|---|
| 1 | サーバー: `_handle_shop_search()` 実装 | ショップ検索トリガー後のフロー制御 |
| 2 | サーバー: `_describe_shops_via_live()` 実装 | 1軒ごとの再接続＋説明読み上げ |
| 3 | サーバー: `_receive_shop_description()` 実装 | ショップ説明専用の応答受信 |
| 4 | サーバー: `_format_shop_for_prompt()` 実装 | ショップデータ→プロンプトテキスト変換 |
| 5 | サーバー: `_restart_normal_conversation()` 実装 | 説明完了後の通常会話復帰 |
| 6 | フロント: `shop_search_result` イベントハンドラ追加 | ショップカード表示（音声は不要） |
| 7 | フロント: `handleShopSearchFromLiveAPI()` 廃止 | REST TTS呼び出しを削除 |
| 8 | フロント: `shop_search_trigger` リスナー廃止 | サーバー側で処理するため不要 |

### Phase 4: 安定化（v1と同じ + ショップ説明のテスト）
- 累積文字数制限のテスト（1軒ごとの再接続で回避できているか）
- ショップ説明が途切れないかのテスト
- 通常会話への復帰がスムーズかのテスト
- 必要に応じて2分割方式（予備案）を実装

### Phase 5: 最適化（v1と同じ）

---

## 10. 既知のリスク・未解決課題（v2追加）

### 10.1 v1からの継続リスク（変更なし）
- LiveAPIプレビュー版の制約
- WebSocketの二重化
- async/syncの混在
- 音声再生の連続性

### 10.2 v2追加リスク

| リスク | 影響 | 対策 |
|---|---|---|
| ショップ説明1軒あたりの文字数が800文字超 | 説明が途切れる | 2分割方式（予備案）を実装 |
| 1軒ごとの再接続のレイテンシ | ショップ間に無音の隙間 | 「次のお店を紹介しますね」等のつなぎを入れる / ブラウザ側でローディング表示 |
| ショップ説明中のユーザー割り込み | 説明フローが中断 | `interrupted` 検知で残りのショップ説明をスキップし、通常会話に復帰 |
| REST APIでのショップデータ取得失敗 | 説明できない | エラーメッセージをLiveAPIで読み上げ |

### 10.3 再接続レイテンシの対策案

ショップ間の再接続に1〜2秒のレイテンシが生じる可能性がある。
ユーザー体験を維持するための対策:

1. **音声つなぎ**: 各ショップ説明の末尾を「次のお店を紹介しますね」で締めるようプロンプトで指示
2. **ブラウザ側表示**: 再接続中にショップカードのハイライトを次の軒に移動
3. **並行処理**: 1軒目の説明再生中に2軒目のLiveAPI再接続を開始（要検証）

---

## 11. テスト計画（v2改訂）

### 11.1〜11.2 v1と同じ

### 11.3 Phase 3 テスト項目（v2改訂）

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | ショップ検索トリガー検知 | AI発話から検索意図を検知し、サーバー側で処理開始 |
| 2 | REST APIデータ取得 | ショップJSON取得成功、`shop_search_result`でブラウザに送信 |
| 3 | ショップカード表示 | ブラウザにショップカードが表示される |
| 4 | ショップ1説明(LiveAPI) | LiveAPI再接続→音声で1軒目を説明→ブラウザで再生 |
| 5 | ショップ2説明(再接続) | 1軒目完了後に再接続→2軒目を説明→途切れなく再生 |
| 6 | ショップ3説明(再接続) | 同上 |
| 7 | 通常会話復帰 | 全ショップ説明後、「気になるお店は？」で通常会話に戻る |
| 8 | 声の一貫性 | 会話→ショップ説明→会話で声質が変わらない |
| 9 | 累積文字数 | 1軒ごとの再接続で800文字制限に引っかからない |
| 10 | ユーザー割り込み | ショップ説明中に話しかけたら説明を中断して応答 |
| 11 | 検索結果0件 | 「見つかりませんでした」をLiveAPI音声で伝え、通常会話に戻る |

---

*以上が LiveAPI 移植設計書 v2。v1との差分はセクション5（ショップ提案フロー）が中心。
実装時は本設計書と `01_stt_stream_detailed_spec.md` を常に参照すること。*
