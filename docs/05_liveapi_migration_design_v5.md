# Gemini LiveAPI 移植設計書 v5（gourmet-sp3）

> **作成日**: 2026-03-12
> **前版**: v2（02_liveapi_migration_design_v2.md）, v3.3（02_liveapi_migration_design_v3.md）
> **前提文書**: `docs/01_stt_stream_detailed_spec.md`, `docs/03_prompt_modification_spec.md`
> **成功事例**: `docs/stt_stream.py`（インタビューモードの再接続方式）
> **移植元安定版**: `github.com/mirai-gpro/gourmet-support`（REST版）, `github.com/mirai-gpro/gourmet-sp`
> **ベースコード**: `c04a302`（v2実装直前の状態）

---

## v5の位置づけ

v2とv3.3から**設計方針と一致するロジックのみ**を引き継ぎ、**Claudeが改ざん・妄想で混入させたロジック**を全て排除した統合仕様書。

### v2・v3.3からの引き継ぎ判定

| 項目 | v2 | v3.3 | v5 |
|---|---|---|---|
| LiveAPI基盤（接続・音声送受信・再接続） | ○ | ○ | **引き継ぐ** |
| 累積文字数制限の再接続回避 | ○ | ○ | **引き継ぐ** |
| 再接続時の会話履歴再送（send_client_content） | — | ○ 新規 | **引き継ぐ** |
| 短期記憶ルールのプロンプトハードコード | — | ○ 新規 | **引き継ぐ** |
| ショップ検索判断基準のプロンプトハードコード | — | ○ 新規 | **引き継ぐ** |
| `should_trigger_shop_search()` キーワードマッチング | ○ 残存 | ○ 廃止 | **廃止（v3.3に従う）** |
| REST APIフォールバック（`switchToRestApiMode`） | ○ | ○ 無効化 | **廃止** |
| `/api/chat` でショップデータ取得 | ○ | ○ 残存 | **廃止（全面LiveAPI化）** |
| ショップ説明のLiveAPI読み上げ（1軒ごと再接続） | ○ 新規 | ○ 維持 | **引き継ぐ** |
| ショップ検索の発火方式 | キーワードマッチング | 「要設計」 | **v5で定義（§5）** |

---

## 0. Claudeへの厳守事項

### 防止ルール

1. **修正する前に、必ず `01_stt_stream_detailed_spec.md` の該当セクションを `Read` ツールで読む**
2. **「確認しました」と報告する場合、確認したファイルパスと行番号を明記する**
3. **仕様書に記載がない機能を追加しない**
4. **推測で API の引数やメソッド名を変えない**
5. **困ったらユーザーに聞く。推測で進めない**
6. **間違えたら戻る。修正を重ねない。`git checkout`で最後の正常状態に戻してからやり直す**
7. **診断ログを入れて→ログを見て→推測で修正、のサイクルは禁止**

### 絶対禁止事項

8. **`should_trigger_shop_search()` および `SHOP_TRIGGER_KEYWORDS` によるキーワードマッチングは絶対に使用禁止。**
   - これはClaude（AI）が過去のセッションで妄想で作り出した誤ったロジックである
   - AIの発話テキストをコード側で解析してショップ検索トリガーを検知するアプローチ自体が根本的に間違い
   - REST版にはこのような仕組みは一切存在しない
   - **ショップ検索の発火はLLM（Gemini）の判断に委ねる。コード側でトリガーを検知しない。**
   - このルールに違反するコードを発見した場合は、即座に削除すること

9. **Gemini LiveAPI（プレビュー版・2025年12月）の動作を推測しない。**
   - このAPIはClaudeの知識ベースに存在しない
   - LiveAPIのVAD、interrupted、turn_complete、output_transcriptionの動作タイミングについて推測で議論しない
   - 不明な点はユーザーに確認する

10. **REST APIへのフォールバックを設計・実装しない。**
    - 本設計はREST APIからLiveAPIへの全面移行である
    - `switchToRestApiMode()`、`live_fallback`イベント、`/api/chat`経由のショップ検索は全て廃止
    - 「部分的にRESTを残す」設計は禁止

11. **リバートする前に、何が正しい状態かを確認する。**
    - 過去のセッションでClaude が100回以上デタラメなリバートを行い、コードが壊れた実績がある
    - リバートは「正しい状態」を特定してから行う。闇雲にリバートしない

### 過去の失敗の教訓（300時間のロス）

```
失敗パターン:
1. Claude が仕様書作成時に自分の知識で改ざん（キーワードマッチング混入等）
2. Claude が実装時に仕様書通りに作らず、自分の知識で勝手に修正
3. テスト失敗 → Claude が推測で修正 → 泥沼化
4. リバート → 何が正しいか分からず → さらに壊れる
5. 次のセッション → 前のClaude の間違いを無批判に引き継ぎ

v5では:
- 仕様書に書いてないことはしない
- LiveAPIの動作を推測しない
- 分からなければユーザーに聞く
```

---

## 1. 移植のスコープ

### 1.1 設計方針

**REST APIからLiveAPIへの全面移行。フォールバックでもRESTは残さない。**

- グルメモード、コンシェルジュモード共にLiveAPI化
- ショップ検索の発火もLiveAPI内で完結（REST API経由の検索は廃止）
- 累積文字数制限は定期的な再接続（リセット）で回避

### 1.2 やること

| # | 内容 | 出典 |
|---|---|---|
| 1 | LiveAPI WebSocketプロキシ（バックエンド） | v2 §3 |
| 2 | LiveAudioManager（フロントエンド） | v2 §4 |
| 3 | セッション再接続メカニズム（累積文字数制限回避） | v2 §7 |
| 4 | トランスクリプション表示 | v2 §4 |
| 5 | 再接続時の会話履歴再送（send_client_content turns） | v3.3 §3.2.1 |
| 6 | 短期記憶ルールのプロンプトハードコード | v3.3 §3.3.1 |
| 7 | ショップ検索判断基準のプロンプトハードコード | v3.3 §3.3.2 |
| 8 | ショップ検索の発火方式（LLM判断 → function calling） | **v5新規 §5** |
| 9 | ショップ説明のLiveAPI読み上げ（1軒ごと再接続） | v2 §5.4 |

### 1.3 やらないこと

- REST APIフォールバック（`switchToRestApiMode`、`live_fallback`）
- `/api/chat` 経由のショップ検索
- キーワードマッチングによるトリガー検知
- `speakTextGCP()` によるTTS（LiveAPI音声に統一）
- PyAudio関連の移植

### 1.4 廃止するコード（c04a302から削除対象）

| 削除対象 | 理由 |
|---|---|
| `SHOP_TRIGGER_KEYWORDS` 定数 | Claudeが妄想で作ったキーワードマッチング |
| `should_trigger_shop_search()` 関数 | 同上 |
| `_build_search_request()` メソッド | キーワードマッチング廃止に伴い不要 |
| `shop_search_trigger` Socket.IOイベント | ブラウザ側トリガーは廃止 |
| `switchToRestApiMode()` メソッド | RESTフォールバック廃止 |
| `live_fallback` イベントの emit/ハンドラ | RESTフォールバック廃止 |
| `handleShopSearchFromLiveAPI()` メソッド | REST経由のショップ検索廃止 |

---

## 2. 全体アーキテクチャ

```
ブラウザ
├── マイク → AudioWorklet → PCM 16kHz → Socket.IO → サーバー
│                                                      │
│                                                      ├── Gemini LiveAPI（音声→音声）
│                                                      │   ├── 通常会話（低遅延音声応答）
│                                                      │   ├── function calling: search_shops
│                                                      │   │   → サーバー内部でショップデータ取得
│                                                      │   │   → shop_search_result でブラウザにカード送信
│                                                      │   ├── ショップ説明（再接続+コンテキスト注入）
│                                                      │   ├── input_transcription
│                                                      │   └── output_transcription
│                                                      │
│                                                      └── [内部処理] ショップデータ取得
│                                                          └── SupportAssistant.process_user_message()
│                                                             （Gemini REST APIでJSON取得 — 外部公開しない）
│
└── スピーカー ← AudioWorklet再生キュー ← PCM 24kHz ← Socket.IO
    チャット欄 ← transcription テキスト ← Socket.IO
```

**v2との違い:**
```
v2: LiveAPI(会話) → キーワードマッチング → REST API(/api/chat) → LiveAPI(説明読み上げ)
                     ↑ Claudeが作った間違い    ↑ 外部エンドポイント経由

v5: LiveAPI(会話) → function calling(search_shops) → 内部処理(データ取得) → LiveAPI(説明読み上げ)
                     ↑ LLMが自ら判断                 ↑ サーバー内部で完結
```

---

## 3. バックエンド設計

### 3.1 設計方針（REST版準拠）

**REST版で機能していた原理をLiveAPIで再現する。**

```
REST版の原理:
1. プロンプトに短期記憶ルール + ショップ検索の判断基準を記述
2. 毎回の API 呼び出しで全会話履歴を送信
→ Geminiが会話履歴から「何が確定済みか」を自然に把握
→ 条件が揃ったらGeminiが自ら判断し、shops配列をJSONで返す
→ コード側のキーワード抽出・トリガー検知は一切なし

LiveAPI v5での再現:
1. プロンプトに短期記憶ルール + ショップ検索判断基準を強化ハードコード
2. 再接続時に send_client_content(turns) で会話履歴を再送
3. LLMが条件が揃ったと判断 → function calling (search_shops) を呼び出す
→ REST版の「shops配列をJSONで返す」に相当する構造化された信号
→ キーワードマッチングは一切不要
```

### 3.2 再接続時のコンテキスト復元（v3.3から引き継ぎ）

#### 3.2.1 _send_history_on_reconnect()

```python
async def _send_history_on_reconnect(self, session):
    """
    再接続時に会話履歴をsend_client_content()で再送する。

    【設計根拠（REST版準拠）】
    REST版では毎回全会話履歴をGemini APIに送信していた。
    LiveAPIのsend_client_content()のturnsパラメータで同等のことを実現。

    - turnsは types.Content のリストとして送信
    - role は "user" または "model"（"ai"ではない）
    - 直近10ターン、各150文字までに制限（トークン消費抑制）
    """
    if not self.conversation_history:
        return

    recent = self.conversation_history[-10:]
    history_turns = []

    for h in recent:
        role = "user" if h['role'] == 'user' else "model"
        text = h['text'][:150]
        history_turns.append(
            types.Content(
                role=role,
                parts=[types.Part(text=text)]
            )
        )

    if history_turns:
        await session.send_client_content(
            turns=history_turns,
            turn_complete=False  # まだターンは終わっていない
        )
        logger.info(f"[LiveAPI] 会話履歴 {len(history_turns)} ターン再送")
```

#### 3.2.2 _get_context_summary()（簡素化）

```python
def _get_context_summary(self) -> str:
    """
    再接続時のコンテキスト要約。
    主力は send_client_content(turns) による履歴再送。
    ここでは最後のAIの質問のみ補足情報として返す。
    """
    if not self.conversation_history:
        return ""

    last_ai = None
    for h in reversed(self.conversation_history):
        if h['role'] == 'ai':
            last_ai = h['text']
            break

    if last_ai and ('?' in last_ai or '？' in last_ai
                    or 'ですか' in last_ai or 'ますか' in last_ai):
        return f"【直前のAIの質問（回答を待っています）】\n{last_ai[:200]}"

    return ""
```

#### 3.2.3 _process_turn_complete()（キーワードマッチング完全削除）

```python
def _process_turn_complete(self):
    """
    ターン完了時の処理
    - 会話履歴の蓄積
    - 発言途切れ・累積文字数による再接続判定

    【v5重要】
    ショップ検索トリガーの検知は行わない。
    ショップ検索はfunction calling（search_shops）で発火する（§5参照）。
    コード側でAIの発話テキストを解析してトリガーを検知する処理は一切ない。
    """
    user_text = ""
    if self.user_transcript_buffer.strip():
        user_text = self.user_transcript_buffer.strip()
        logger.info(f"[LiveAPI] ユーザー: {user_text}")
        self._add_to_history("user", user_text)
        self.user_transcript_buffer = ""

    if self.ai_transcript_buffer.strip():
        ai_text = self.ai_transcript_buffer.strip()
        logger.info(f"[LiveAPI] AI: {ai_text}")
        self._add_to_history("ai", ai_text)

        # 発言途切れチェック・文字数カウント・再接続判定
        is_incomplete = self._is_speech_incomplete(ai_text)
        char_count = len(ai_text)
        self.ai_char_count += char_count
        remaining = MAX_AI_CHARS_BEFORE_RECONNECT - self.ai_char_count
        logger.info(f"[LiveAPI] 累積: {self.ai_char_count}文字 / 残り: {remaining}文字")

        self.ai_transcript_buffer = ""

        if is_incomplete:
            logger.info("[LiveAPI] 発言途切れのため再接続")
            self.needs_reconnect = True
        elif char_count >= LONG_SPEECH_THRESHOLD:
            logger.info(f"[LiveAPI] 長い発話({char_count}文字)のため再接続")
            self.needs_reconnect = True
        elif self.ai_char_count >= MAX_AI_CHARS_BEFORE_RECONNECT:
            logger.info("[LiveAPI] 累積制限到達のため再接続")
            self.needs_reconnect = True
```

### 3.3 プロンプト設計

#### 3.3.1 短期記憶ルール（v3.3から引き継ぎ）

`LIVEAPI_CONCIERGE_SYSTEM` 内にハードコード:

```
## 【短期記憶・セッション行動ルール（最重要・厳守）】

### 1. 短期記憶の前提
あなたは会話履歴の内容を記憶している前提で行動すること。
会話中に一度確定・明示された情報は、ユーザーが条件を変えない限り有効。
「覚えていない前提」での聞き直しは絶対に禁止。

### 2. 記憶対象（会話履歴から把握すべき情報）
- 利用目的・シーン（接待、デート、忘年会、女子会、家族利用 等）
- エリア・地域
- 予算感
- 参加人数
- 料理ジャンル
- 店の雰囲気・優先条件（個室、静か、カジュアル、高級 等）

### 3. 重複質問の禁止ルール（絶対厳守）
✅ 再質問してよいケース：ユーザーが明示的に条件変更を指示した場合のみ
❌ 再質問してはいけないケース：すでに取得済みの条件を理由なく聞き直す行為

### 4. 業態別ヒアリング制御
簡易飲食業態（ラーメン、カフェ、ファーストフード等）は即提案優先

### 5. 再接続時の行動ルール（LiveAPI固有・最重要）
再接続後も会話履歴が再送されるので確認し、同じ質問を繰り返さない

### 6. このルールの優先順位
1位：本セクション > 2位：質問ルール > 3位：応答スタイル
```

#### 3.3.2 ショップ検索判断基準（v3.3から引き継ぎ + v5改訂）

```
## 【ショップ検索の判断基準（REST版準拠・厳守）】

### 判断の原則
あなたは会話のキャッチボールを通じて、ユーザーの要望が十分に揃ったかを自ら判断する。
条件が揃ったと判断したら、search_shopsツールを呼び出してショップ検索を実行する。

### 検索に必要な最低条件
以下のうち、少なくとも2〜3項目が確定していれば検索可能と判断する:
- エリア・地域
- 料理ジャンルまたは利用シーン
- 予算感（任意・なくても可）
- 人数（任意・なくても可）

### 検索を実行するタイミング
- ユーザーが十分な条件を伝えた時点で、search_shopsツールを呼び出す
- 全項目が揃うまで待つ必要はない
- ユーザーが「もういいから探して」等と言った場合は即座に検索する
- 簡易業態（ラーメン、カフェ等）はエリアだけでも検索可能

### search_shopsツールの使い方
- 条件が揃ったら search_shops(user_request="六本木 接待 イタリアン 1万円 4名") のように呼び出す
- user_request にはユーザーの要望を自然言語で要約して渡す
```

---

## 4. フロントエンド設計

### 4.1 廃止するもの

- `handleShopSearchFromLiveAPI()` メソッド全体
- `shop_search_trigger` Socket.IOリスナー
- `switchToRestApiMode()` メソッド
- `live_fallback` ハンドラ内の `switchToRestApiMode()` 呼び出し
- ショップ説明における `speakTextGCP()` 呼び出し

### 4.2 維持するもの

- `shop_search_result` イベントハンドラ（v2 §5.5.1）— カード表示
- `live_audio` ハンドラ — 音声再生（ショップ説明含む）
- `ai_transcript` ハンドラ — チャット表示
- `terminateLiveSession()` — マイクボタンによるLiveAPI停止

### 4.3 変更するもの

- `toggleRecording()` 内の `switchToRestApiMode()` → `terminateLiveSession()` に置換

---

## 5. ショップ検索の発火方式（v5新規：function calling）

### 5.1 設計方針

**REST版と同じ原理: LLMが自ら判断し、構造化された信号でコードに通知する。**

```
REST版:
LLM判断 → JSON応答にshops配列を含める → コードが自動検知

LiveAPI v5:
LLM判断 → function calling (search_shops) を呼び出す → コードが検索を実行
```

function callingはLLMの判断を構造化されたデータとしてコードに伝える手段であり、
REST版の「JSON応答にshops配列を含める」と同じ原理。
キーワードマッチングとは根本的に異なる。

### 5.2 function declaration

```python
SEARCH_SHOPS_DECLARATION = {
    "name": "search_shops",
    "description": "ユーザーの条件に基づいてレストランを検索する。条件が十分に揃ったと判断した時に呼び出す。",
    "parameters": {
        "type": "object",
        "properties": {
            "user_request": {
                "type": "string",
                "description": "ユーザーの要望の要約（例: '六本木 接待 イタリアン 1万円 4名'）"
            }
        },
        "required": ["user_request"]
    }
}
```

### 5.3 _build_config() への追加

```python
config = {
    "response_modalities": ["AUDIO"],
    "system_instruction": instruction,
    "tools": [{"function_declarations": [SEARCH_SHOPS_DECLARATION]}],
    # ... 既存の設定
}
```

**【注意】** LiveAPIプレビュー版でfunction callingが動作するかはテストで検証が必要。
動作しない場合の代替方式はテスト結果に基づいてユーザーと相談する。
**Claudeが推測で代替方式を設計することは禁止。**

### 5.4 tool_call ハンドラ

`_receive_and_forward()` 内の既存の tool_call チェック箇所を実装:

```python
# 1. tool_call（search_shops）
if hasattr(response, 'tool_call') and response.tool_call:
    await self._handle_tool_call(response.tool_call, session)
    continue
```

```python
async def _handle_tool_call(self, tool_call, session):
    """
    LLMからのfunction calling応答を処理する。
    search_shops の場合、ショップ検索を実行する。
    """
    for fc in tool_call.function_calls:
        if fc.name == "search_shops":
            user_request = fc.args.get("user_request", "")
            logger.info(f"[LiveAPI] search_shops呼び出し: '{user_request}'")

            # ショップ検索を実行（サーバー内部処理）
            await self._handle_shop_search(user_request)

            # function responseを返す（LiveAPI仕様に従う）
            # ※ 正確な構文はテストで検証
            await session.send_tool_response(
                function_responses=[{
                    "id": fc.id,
                    "response": {"result": "検索結果をユーザーに表示しました"}
                }]
            )
```

### 5.5 ショップ検索の実行

```python
async def _handle_shop_search(self, user_request: str):
    """
    ショップ検索を実行し、結果をブラウザに送信する。

    【設計】
    - SupportAssistant.process_user_message() を内部的に呼び出してショップデータを取得
    - これはGemini REST API（generateContent）でJSON応答を得る内部処理
    - 外部エンドポイント（/api/chat）は経由しない
    - 取得したデータはshop_search_resultイベントでブラウザに送信
    """
    shop_data = await self._fetch_shop_data(user_request)

    if not shop_data or not shop_data.get('shops'):
        logger.info("[ShopSearch] ショップ見つからず")
        return

    shops = shop_data['shops']
    response_text = shop_data.get('response', '')

    # ショップカードデータをブラウザに送信
    self.socketio.emit('shop_search_result', {
        'shops': shops,
        'response': response_text,
    }, room=self.client_sid)

    # ショップ説明をLiveAPIで1軒ずつ読み上げ
    await self._describe_shops_via_live(shops)
```

---

## 6. ショップ説明のLiveAPI読み上げ（v2 §5.4から引き継ぎ）

### 6.1 1軒ごとのLiveAPI再接続

```python
async def _describe_shops_via_live(self, shops: list):
    """
    ショップ説明をLiveAPIで読み上げ（1軒ごとに再接続）

    【設計根拠】
    stt_stream.py の再接続メカニズム（セクション8）と同じ手法:
    - 累積文字数制限(800文字)を回避するため、適切なタイミングで再接続
    - 再接続時に system_prompt にコンテキストを注入
    - send_client_content() でトリガーメッセージを送信
    """
    total = len(shops)

    for i, shop in enumerate(shops):
        shop_number = i + 1
        is_last = (shop_number == total)

        shop_context = self._format_shop_for_prompt(shop, shop_number, total)

        shop_instruction = self.system_prompt + f"""

【現在のタスク：ショップ紹介】
あなたは今、ユーザーに検索結果のお店を紹介しています。

{shop_context}

【読み上げルール】
1. このお店の特徴を自然な話し言葉で紹介する（3〜5文程度）
2. 店名、ジャンル、エリア、特徴、価格帯を含める
3. マークダウン記法は使わない（音声出力のため）
4. 「{shop_number}軒目は」から始める
5. 紹介が終わったら、次のお店の紹介に自然につなげる。「以上です」とは言わない。
"""
        if is_last:
            shop_instruction += f"5の代わりに: 最後のお店です。紹介後「以上、{total}軒のお店をご紹介しました。気になるお店はありましたか？」で締めてください。\n"

        try:
            config = self._build_config()
            config["system_instruction"] = shop_instruction
            # ショップ説明時はfunction callingのtoolsを外す
            config.pop("tools", None)

            async with self.client.aio.live.connect(
                model=LIVE_API_MODEL,
                config=config
            ) as session:
                trigger_text = f"{shop_number}軒目のお店を紹介してください。"
                if shop_number == 1:
                    trigger_text = f"検索結果を紹介してください。まず1軒目のお店からお願いします。"

                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=trigger_text)]
                    ),
                    turn_complete=True
                )

                await self._receive_shop_description(session, shop_number)

        except Exception as e:
            logger.error(f"[ShopDesc] ショップ{shop_number}説明エラー: {e}")
            continue

    # 全ショップ説明完了 → 通常会話に復帰
    summary = f"{total}軒のお店を紹介しました。気になるお店はありましたか？"
    self._add_to_history("ai", summary)
    self._resume_message = "ありがとうございます。気になるお店について教えてください。"
    self.needs_reconnect = True  # 通常会話に復帰するために再接続
```

### 6.2 ショップ説明の音声受信

v2 §5.4.3 から引き継ぎ。`_receive_shop_description()` はショップ説明専用の応答受信。
`type: 'shop_description'` を付けてemitし、フロントで区別可能にする。

### 6.3 通常会話への復帰

ショップ説明完了後、`needs_reconnect = True` で通常の再接続フローに戻る。
`_resume_message` に復帰メッセージをセットしておき、再接続時にトリガーとして送信する。

---

## 7. 再接続メカニズム

### 7.1 通常会話の再接続

| # | 項目 | 内容 |
|---|---|---|
| 1 | 再接続トリガー | 発言途切れ / 長い発話(500文字) / 累積上限(800文字) |
| 2 | システムプロンプト | 短期記憶ルール + ショップ検索判断基準 + 直前質問の補足 |
| 3 | 会話履歴の再送 | send_client_content(turns) で直近10ターン再送 |
| 4 | トリガーメッセージ | `_resume_message` or `"続きをお願いします"` |

### 7.2 ショップ説明の再接続

| # | 項目 | 内容 |
|---|---|---|
| 1 | 再接続トリガー | 1軒の説明完了（turn_complete） |
| 2 | システムプロンプト | ショップ紹介専用プロンプト（function calling toolsなし） |
| 3 | 引き継ぎ情報 | 次のショップの詳細データ |
| 4 | トリガーメッセージ | 「N軒目のお店を紹介してください。」 |

---

## 8. セッション管理

### 8.1 ライフサイクル

```
[通常会話]
LiveAPIセッション#1 (初回接続・挨拶・function calling有効)
  ↓ 累積制限 or 発話途切れ
LiveAPIセッション#2 (再接続・履歴再送・function calling有効)
  ↓ ...
LiveAPIセッション#N (LLMがsearch_shopsをfunction callingで呼び出し)
  ↓
[ショップ検索]
サーバー内部でSupportAssistant経由のショップデータ取得
  ↓ shop_search_result イベントでブラウザにカード送信
  ↓
[ショップ説明]（function calling無効）
LiveAPIセッション#N+1 (ショップ1説明)
  ↓ 再接続
LiveAPIセッション#N+2 (ショップ2説明)
  ↓ ...
  ↓
[通常会話復帰]（function calling有効）
LiveAPIセッション#N+M (「気になるお店はありましたか？」)
```

### 8.2 conversation_history

- セッション終了まで蓄積（最大20ターン保持）
- 再接続時に直近10ターンを再送
- ショップ検索後もリセットしない（条件変更による再検索に対応）

---

## 9. 実装フェーズ計画

### Phase 1: コードをc04a302にリバート
- `git checkout c04a302 -- src/ support-base/` でv2実装前のコードに戻す

### Phase 2: 廃止コードの削除（§1.4）
- キーワードマッチング関連コードを全削除
- RESTフォールバック関連コードを全削除

### Phase 3: v3.3の正しい部分を適用
- 短期記憶ルールのプロンプトハードコード（§3.3.1）
- 再接続時の会話履歴再送 `_send_history_on_reconnect()`（§3.2.1）
- `_get_context_summary()` の簡素化（§3.2.2）
- `_process_turn_complete()` からキーワードマッチング除去（§3.2.3）
- run() の再接続フローに履歴再送を追加

### Phase 4: function calling実装（§5）
- `SEARCH_SHOPS_DECLARATION` の定義
- `_build_config()` にtools追加
- `_handle_tool_call()` の実装
- `_handle_shop_search()` の実装
- **テスト: LiveAPIプレビュー版でfunction callingが動作するか検証**
- **動作しない場合: ユーザーと代替方式を相談（Claudeが推測で決めない）**

### Phase 5: ショップ説明のLiveAPI読み上げ（§6）
- `_describe_shops_via_live()` の実装
- `_receive_shop_description()` の実装
- `_format_shop_for_prompt()` の実装
- 通常会話への復帰フロー

### Phase 6: フロントエンド修正（§4）
- `shop_search_result` ハンドラの確認/修正
- 廃止コードの削除
- `toggleRecording()` の修正

### Phase 7: テスト
- グルメモード: 1ターン検索が動作するか
- コンシェルジュモード: 会話のキャッチボール → 検索提案 → 検索発火
- 短期記憶: 再接続後に同じ質問を繰り返さないか
- ショップ説明: 1軒ずつLiveAPI読み上げが動作するか
- 累積文字数: 再接続で制限を回避できているか

---

## 10. 既知のリスク

| リスク | 影響 | 対策 |
|---|---|---|
| LiveAPIプレビュー版でfunction callingが動作しない | ショップ検索が発火しない | ユーザーと代替方式を相談。**Claudeが推測で決めない** |
| 累積文字数制限の再接続タイミング | 会話が途切れる | stt_stream.pyの実績値（800文字）を踏襲 |
| send_client_content()のturns再送がトークンを消費 | コンテキストウィンドウの圧迫 | 直近10ターン・各150文字に制限 |
| ショップ説明1軒の文字数が800文字超 | 説明が途切れる | テストで確認し、必要なら分割 |
| ショップ間の再接続レイテンシ | 無音の隙間 | テスト結果を見てから対策検討 |

---

*以上が LiveAPI 移植設計書 v5。*
*v2の正しい基盤 + v3.3の正しい改善 を統合し、Claudeが改ざんした部分を全て排除。*
*function callingの動作可否はテストで検証。不明点はユーザーに確認する。*
