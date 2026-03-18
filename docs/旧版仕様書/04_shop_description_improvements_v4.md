# ショップ説明TTS改善仕様書 v4（gourmet-sp3）

> **作成日**: 2026-03-12
> **前提文書**: `docs/02_liveapi_migration_design_v3.md`（v3.3）, `docs/02_liveapi_migration_design_v2.md`（v2 §5.4）
> **対象**: ショップ説明のLiveAPI読み上げ部分（`_describe_shops_via_live()` / `_receive_shop_description()`）

---

## 0. Claudeへの厳守事項

`docs/02_liveapi_migration_design_v3.md` §0 の全ルールに加え、以下を厳守:

1. **Gemini LiveAPIの動作（VAD、interrupted、turn_complete、output_transcription等）を推測しない**
2. **推測で API の引数やメソッド名を変えない。不明な点はユーザーに確認する**
3. **本仕様書に記載のない変更を加えない**

---

## 1. 変更概要

| # | 改善項目 | 現状の問題 | 対策 |
|---|---|---|---|
| 1 | TTS読み上げテキストのチャット非表示 | ショップ説明がチャット欄にも表示され二重表示になる | フロントの`ai_transcript`ハンドラで`shop_description`をスキップ |
| 2 | 「以上です。」の除去 | 各店ごとに「以上です」と言い、冗長 | プロンプト修正。最後の店のみ締めの言葉 |
| 3 | 店間の読み上げギャップ縮小 | 各店ごとにWebSocket再接続で1-3秒のギャップ | 次の店のセッションを先行接続（パイプライン化） |
| 4 | カード表示→1軒目読み上げのギャップ縮小 | カード表示後にLiveAPI接続開始で数秒待ち | 1軒目の接続を先行開始 |

---

## 2. 改善1: TTS読み上げテキストのチャット非表示

### 2.1 現状の問題

- バックエンド `_receive_shop_description()` (L920-928) が `ai_transcript` イベントに `type: 'shop_description'` を付けてemitしている
- フロントエンド `core-controller.ts` の `ai_transcript` ハンドラ (L292-310) は `type` を区別せず全てチャット欄に表示している
- 結果、ショップカード + テキスト説明 + TTS読み上げテキストが重複表示される

### 2.2 対策

**フロントエンド（core-controller.ts）のみの変更。バックエンドは変更なし。**

`ai_transcript` ハンドラで `data.type === 'shop_description'` の場合にチャット表示をスキップする。

```typescript
this.socket.on('ai_transcript', (data: any) => {
  if (!this.isLiveMode) return;
  const text = data.text;
  if (text) {
    // ★ v4: ショップ説明のTTS読み上げはチャット欄に表示しない
    // 音声再生のみ。チャット欄にはショップカードが既に表示されている
    if (data.type === 'shop_description') {
      return;  // チャット表示をスキップ（音声はlive_audioで再生される）
    }

    this.aiTranscriptBuffer += text;
    // 以降、通常のチャット表示処理（既存コードそのまま）
    // ...
  }
});
```

### 2.3 影響範囲

- `core-controller.ts`: `ai_transcript` ハンドラに条件分岐を1つ追加
- バックエンド: 変更なし（既に `type: 'shop_description'` を送信済み）
- 音声再生: 影響なし（`live_audio` イベントは別経路）

---

## 3. 改善2: 「以上です。」の除去

### 3.1 現状の問題

- プロンプト (L842): `5. 紹介が終わったら「以上です」で締める`
- 各店ごとに「以上です」と言うため冗長
- 最後の店は別途 L845 で締めの文言を指示しているが、それに加えて「以上です」も言ってしまう

### 3.2 対策

**バックエンド（live_api_handler.py）の `_describe_shops_via_live()` 内プロンプト修正のみ。**

```python
# 現在（L837-845）
"""
【読み上げルール】
1. このお店の特徴を自然な話し言葉で紹介する（3〜5文程度）
2. 店名、ジャンル、エリア、特徴、価格帯を含める
3. マークダウン記法は使わない（音声出力のため）
4. 「{shop_number}軒目は」から始める
5. 紹介が終わったら「以上です」で締める
"""

# 変更後
"""
【読み上げルール】
1. このお店の特徴を自然な話し言葉で紹介する（3〜5文程度）
2. 店名、ジャンル、エリア、特徴、価格帯を含める
3. マークダウン記法は使わない（音声出力のため）
4. 「{shop_number}軒目は」から始める
5. 紹介が終わったら、次のお店の紹介に自然につなげるような口調で締める。「以上です」とは言わない。
"""
```

**最後の店の場合（is_last == True）:**

```python
# 現在（L844-845）
if is_last:
    shop_instruction += f"6. 最後のお店です。紹介後「以上、{total}軒のお店をご紹介しました。気になるお店はありましたか？」で締めてください。\n"

# 変更後
if is_last:
    shop_instruction += f"5の代わりに: 最後のお店です。紹介後「以上、{total}軒のお店をご紹介しました。気になるお店はありましたか？」で締めてください。\n"
```

### 3.3 影響範囲

- `live_api_handler.py`: `_describe_shops_via_live()` 内のプロンプト文字列のみ
- フロントエンド: 変更なし
- 最後の店以外: 「以上です」→ 自然なつなぎ
- 最後の店: 従来通り「以上、N軒のお店をご紹介しました」で締める

---

## 4. 改善3: 店間の読み上げギャップ縮小（パイプライン化）

### 4.1 現状の問題

```
[現在のフロー]
Shop 1: connect() → send → receive → turn_complete → close
         ↓ (1-3秒のギャップ: 接続確立待ち)
Shop 2: connect() → send → receive → turn_complete → close
         ↓ (1-3秒のギャップ)
Shop 3: connect() → send → receive → turn_complete → close
```

各店ごとに `client.aio.live.connect()` で新規WebSocket接続を確立するため、接続確立に1-3秒かかる。

### 4.2 対策: 先行接続（パイプライン化）

現在の店の音声受信中に、次の店のLiveAPI接続をバックグラウンドで先行確立する。

```
[改善後のフロー]
Shop 1: connect() → send → receive ──── turn_complete → close
                              ↑ この間に
Shop 2:              connect() (先行)    → send → receive ──── turn_complete → close
                                                        ↑ この間に
Shop 3:                                   connect() (先行)    → send → receive → close
```

### 4.3 実装方針

`_describe_shops_via_live()` を以下のように改修:

```python
async def _describe_shops_via_live(self, shops: list):
    """
    ショップ説明をLiveAPIで読み上げ（パイプライン化）
    """
    total = len(shops)

    # 次のショップの先行接続タスク
    next_session_task = None
    next_config = None

    for i, shop in enumerate(shops):
        shop_number = i + 1
        is_last = (shop_number == total)

        # プロンプト・トリガーテキストの準備
        shop_instruction = self._build_shop_instruction(shop, shop_number, total, is_last)
        trigger_text = self._build_shop_trigger_text(shop_number)

        config = self._build_config()
        config["system_instruction"] = shop_instruction

        try:
            # 先行接続済みのセッションがあればそれを使う
            # なければ新規接続
            if next_session_task and not next_session_task.done():
                await next_session_task  # 先行接続の完了を待つ

            async with self.client.aio.live.connect(
                model=LIVE_API_MODEL,
                config=config
            ) as session:

                # ★ 次のショップの先行接続を開始（最後の店以外）
                if not is_last:
                    next_shop = shops[i + 1]
                    next_number = shop_number + 1
                    next_is_last = (next_number == total)
                    next_instruction = self._build_shop_instruction(
                        next_shop, next_number, total, next_is_last
                    )
                    next_config = self._build_config()
                    next_config["system_instruction"] = next_instruction
                    # 先行接続をバックグラウンドで開始
                    # ※ connect() の実装によっては async context manager の
                    #   事前確立が必要。テストで検証。
                    next_session_task = asyncio.create_task(
                        self._preconnect_session(next_config)
                    )

                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=trigger_text)]
                    ),
                    turn_complete=True
                )

                await self._receive_shop_description(session, shop_number)

            logger.info(f"[ShopDesc] ショップ{shop_number}/{total} 説明完了")

        except Exception as e:
            logger.error(f"[ShopDesc] ショップ{shop_number}説明エラー: {e}")
            continue

    # 全ショップ説明完了 → 通常会話に復帰
    summary = f"{total}軒のお店を紹介しました。気になるお店はありましたか？"
    self._add_to_history("ai", summary)
    await self._restart_live_with_message(
        "ありがとうございます。気になるお店について教えてください。"
    )
```

### 4.4 注意事項

- `client.aio.live.connect()` が async context manager であるため、先行接続の実装方法はテストで検証が必要
- LiveAPIプレビュー版の接続制限（同時セッション数等）は不明。テストで確認する
- 先行接続が失敗した場合は、従来通り逐次接続にフォールバックする

### 4.5 ヘルパーメソッドの抽出

プロンプト構築を独立メソッドに抽出し、再利用可能にする:

```python
def _build_shop_instruction(self, shop: dict, shop_number: int, total: int, is_last: bool) -> str:
    """ショップ紹介用のシステムプロンプトを構築"""
    shop_context = self._format_shop_for_prompt(shop, shop_number, total)
    instruction = self.system_prompt + f"""

【現在のタスク：ショップ紹介】
あなたは今、ユーザーに検索結果のお店を紹介しています。

{shop_context}

【読み上げルール】
1. このお店の特徴を自然な話し言葉で紹介する（3〜5文程度）
2. 店名、ジャンル、エリア、特徴、価格帯を含める
3. マークダウン記法は使わない（音声出力のため）
4. 「{shop_number}軒目は」から始める
5. 紹介が終わったら、次のお店の紹介に自然につなげるような口調で締める。「以上です」とは言わない。
"""
    if is_last:
        instruction += f"5の代わりに: 最後のお店です。紹介後「以上、{total}軒のお店をご紹介しました。気になるお店はありましたか？」で締めてください。\n"

    return instruction

def _build_shop_trigger_text(self, shop_number: int) -> str:
    """ショップ紹介用のトリガーテキストを構築"""
    if shop_number == 1:
        return f"検索結果を紹介してください。まず{shop_number}軒目のお店からお願いします。"
    return f"{shop_number}軒目のお店を紹介してください。"
```

---

## 5. 改善4: カード表示→1軒目読み上げのギャップ縮小

### 5.1 現状の問題

```
[現在のフロー]
_handle_shop_search():
  1. _fetch_shop_data()       — REST API呼び出し（完了済み）
  2. shop_search_result emit  — ブラウザにカード表示
  3. _describe_shops_via_live()
     → connect()              — ★ ここで初めてLiveAPI接続を開始
     → send_client_content()
     → 応答待ち
     → 音声到着               — ユーザーはここまで待つ
```

カード表示後にLiveAPI接続を開始するため、接続確立＋最初の音声チャンク到着まで数秒かかる。

### 5.2 対策: 1軒目の接続を先行開始

`_handle_shop_search()` で `shop_search_result` emit の前に1軒目のLiveAPI接続をバックグラウンドで開始する。

```python
async def _handle_shop_search(self, user_request: str):
    """
    ショップ検索トリガー検知後の処理
    """
    logger.info(f"[ShopSearch] 検索開始: '{user_request}'")

    # ① REST APIでショップデータを取得
    shop_data = await self._fetch_shop_data(user_request)

    if not shop_data or not shop_data.get('shops'):
        logger.info("[ShopSearch] ショップ見つからず、通常会話に復帰")
        await self._restart_live_with_message(
            "検索しましたが、条件に合うお店が見つかりませんでした。"
            "条件を変えてもう一度お探ししましょうか？"
        )
        return

    shops = shop_data['shops']
    response_text = shop_data.get('response', '')

    # ★ v4: 1軒目のLiveAPI接続を先行開始（カード送信と並行）
    first_shop = shops[0]
    first_instruction = self._build_shop_instruction(first_shop, 1, len(shops), len(shops) == 1)
    first_config = self._build_config()
    first_config["system_instruction"] = first_instruction
    preconnect_task = asyncio.create_task(
        self._preconnect_session(first_config)
    )

    # ② ショップカードデータをブラウザに送信（表示用）
    # ★ 先行接続はバックグラウンドで進行中
    self.socketio.emit('shop_search_result', {
        'shops': shops,
        'response': response_text,
    }, room=self.client_sid)
    logger.info(f"[ShopSearch] {len(shops)}件をブラウザに送信")

    # ③ ショップ説明をLiveAPIで1軒ずつ読み上げ
    # ★ 1軒目は先行接続済みのセッションを使用
    await self._describe_shops_via_live(shops, preconnect_task=preconnect_task)
```

### 5.3 注意事項

- 改善3（§4）と改善4（§5）は同じ先行接続の仕組みを共有する
- `_preconnect_session()` の実装は `client.aio.live.connect()` の仕様に依存するため、テストで検証が必要
- 先行接続が失敗した場合は従来通り逐次接続にフォールバックする

---

## 6. 実装の優先順位

| 順位 | 改善 | 難易度 | リスク |
|---|---|---|---|
| 1 | 改善1: TTS非表示 | 低（フロント1行追加） | なし |
| 2 | 改善2: 「以上です」除去 | 低（プロンプト文字列変更） | なし |
| 3 | 改善4: 1軒目先行接続 | 中（async処理） | LiveAPI同時接続の制約を要確認 |
| 4 | 改善3: 店間パイプライン | 中〜高（async処理） | 同上 + context manager制約を要確認 |

**改善1と改善2は即座に実装可能。改善3と改善4はテストで先行接続の実現可能性を確認してから実装。**

---

## 7. テスト計画

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | ショップ検索実行後、チャット欄の表示確認 | ショップカードのみ表示。TTS読み上げテキストはチャット欄に表示されない |
| 2 | 各店の読み上げ終了時の発言確認 | 途中の店は「以上です」と言わない。自然なつなぎで次の店へ |
| 3 | 最後の店の読み上げ終了時 | 「以上、N軒のお店をご紹介しました。気になるお店はありましたか？」で締める |
| 4 | 店間のギャップ計測 | 先行接続により、従来の1-3秒から1秒未満に短縮（目標） |
| 5 | カード表示→1軒目音声到着の計測 | 先行接続により、従来より1-3秒短縮（目標） |
| 6 | LiveAPI同時接続テスト | 先行接続が2セッション同時に確立可能か確認 |
| 7 | 先行接続失敗時のフォールバック | 先行接続失敗時に従来の逐次接続で正常に動作する |

---

*以上がショップ説明TTS改善仕様書 v4。*
*改善1・2は即実装可能な軽微な変更。改善3・4はLiveAPIの同時接続制約のテスト後に実装。*
