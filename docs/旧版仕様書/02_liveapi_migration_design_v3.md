# Gemini LiveAPI 移植設計書 v3（gourmet-sp3）

> **作成日**: 2026-03-11
> **改訂日**: 2026-03-12（v3.3: ショップ検索トリガーをLLM判断方式に全面改訂。キーワードマッチング廃止）
> **前版**: `docs/02_liveapi_migration_design_v2.md`（v2: 2026-03-11）
> **前提文書**: `docs/01_stt_stream_detailed_spec.md`, `docs/03_prompt_modification_spec.md`
> **成功事例**: `docs/stt_stream.py`（インタビューモードの再接続方式）
> **移植元安定版**: `github.com/mirai-gpro/gourmet-support`（REST版）
> **v3変更理由**: コンシェルジュモードの短期記憶がLiveAPI再接続時に失われる問題の対策

---

## v3.2 → v3.3 変更概要

| 項目 | v3.2（旧） | v3.3（新） |
|---|---|---|
| ショップ検索トリガー | `should_trigger_shop_search()` キーワードマッチング | **廃止。LLMの判断に委ねる（REST版と同じ原理）** |
| `_process_turn_complete()` | キーワードマッチングで `_shop_search_pending` をセット | **キーワードマッチング削除。会話履歴蓄積と再接続判定のみ** |
| ショップ検索の発火 | コード側でAIの発話テキストからキーワード検知 | **LLMが会話の中で条件が揃ったと判断 → REST APIでショップ検索を実行（プロンプト指示に従う）** |
| プロンプト | 短期記憶ルールのみ | **ショップ検索の判断基準を `03_prompt_modification_spec.md` に従いハードコード** |

### v3.3変更の背景

**v3.2の致命的欠陥:**
```
v3.2の§3.2.4は、AIの発話テキスト（ai_transcript_buffer）に対する
キーワードマッチング（should_trigger_shop_search()）でショップ検索を発火させていた。

これは根本的に間違ったアプローチ:
- REST版ではキーワードマッチングは一切使っていない
- REST版ではLLMが自然に判断し、shops配列をJSONで返すことで検索が発火する
- キーワードマッチングはClaude（AI）が過去のセッションで妄想で作った誤ったロジック
- テキスト入力では発火するのにSTT音声入力で発火しない原因がこの誤ったトリガー方式にあった
```

**v3.3の修正方針:**
```
REST版と同じ原理を適用する:
- LLMが会話のキャッチボールの中で、ユーザーの要望が十分に揃ったかを判断する
- 判断はプロンプト（03_prompt_modification_spec.md）の指示に従う
- コード側でAIの発話を解析してトリガーを検知する処理は一切行わない
- should_trigger_shop_search()、SHOP_TRIGGER_KEYWORDS は完全に削除する
```

---

## v2 → v3 変更概要

| 項目 | v2（旧） | v3（新） |
|---|---|---|
| 再接続時のコンテキスト | チャットログの断片を要約テキストとして注入 | **会話履歴の`send_client_content()`再送（REST版準拠）** |
| 短期記憶の管理 | なし（Geminiのセッション内記憶に依存） | **キーワード抽出は廃止。プロンプト強化 + 履歴再送で対応** |
| `_get_context_summary()` | `"user: xxx\nai: xxx"` 形式のテキスト断片 | **最小限（直前の質問のみ補足）。主力は履歴再送** |
| 再接続時の`send_client_content()` | `"続きをお願いします"` のみ | **会話履歴turnsの再送 + トリガーメッセージ** |
| `LIVEAPI_CONCIERGE_SYSTEM` | 短期記憶ルールなし | **REST版concierge_ja.txtの短期記憶ルールを凝縮してハードコード** |

### v3変更の背景

**REST版（gourmet-support）で短期記憶が機能していた理由:**
```
REST: 毎回 system_prompt（concierge_ja.txt全文）+ 全会話履歴 → Gemini REST API
→ Geminiは全ターンを見て「何が確定済みか」を自然に把握できた
→ 短期記憶は会話履歴の全量送信により「無料で」成立していた
→ コード側のキーワード抽出・ステップ追跡は一切なし
```

**LiveAPI版で壊れた理由:**
```
LiveAPI: セッション内はGeminiが記憶保持 → だが再接続で全消失
→ 再接続時に「user: 接待で... ai: 承知しました...」の断片しか渡していない
→ Geminiは「何が確定済みで何が未確認か」を把握できない
→ ステップ1に戻って同じ質問を繰り返す → 検索に辿りつけない
```

**v3の解決方針（REST版準拠）:**
```
LiveAPI v3: REST版と同じ方式を再現
→ 再接続時に send_client_content(turns) で会話履歴を再送（REST版の全履歴送信に相当）
→ プロンプトにREST版の短期記憶ルールを強化ハードコード（聞き直し禁止を明示）
→ キーワード抽出・hearing_step等のコード側追跡は不要（Geminiが自然に判断）
→ REST版と同等の精度を、同じ原理で実現する
```

---

## 0. Claudeへの厳守事項（v3.3改訂）

### 防止ルール

1. **修正する前に、必ず `01_stt_stream_detailed_spec.md` の該当セクションを `Read` ツールで読む**
2. **「確認しました」と報告する場合、確認したファイルパスと行番号を明記する**
3. **仕様書に記載がない機能を追加しない**
4. **推測で API の引数やメソッド名を変えない**
5. **困ったらユーザーに聞く。推測で進めない**
6. **間違えたら戻る。修正を重ねない。`git checkout`で最後の正常状態に戻してからやり直す** ← v3追加
7. **診断ログを入れて→ログを見て→推測で修正、のサイクルは禁止** ← v3追加

### v3.3追加：絶対禁止事項

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

---

## 1. 移植のスコープ（v3改訂）

### 1.1 やること

| # | 内容 | 優先度 | v3変更 |
|---|---|---|---|
| 1 | バックエンド: LiveAPI WebSocketプロキシの新設 | 必須 | 変更なし |
| 2 | フロントエンド: LiveAudioManager の実装 | 必須 | 変更なし |
| 3 | ~~LiveAPI → REST API フォールバック機構~~ | ~~必須~~ | **v3.2: テストフェーズでは無効化。switchToRestApiMode()を削除** |
| 4 | セッション再接続メカニズム | 必須 | 変更なし |
| 5 | トランスクリプション（文字起こし）表示 | 必須 | 変更なし |
| 6 | ショップ説明のLiveAPI読み上げ（1軒ごとに再接続） | 必須 | 変更なし |
| 7 | **プロンプトの短期記憶ルール強化（REST版準拠）** | **必須** | **v3改訂** |
| 8 | **再接続時の会話履歴再送（send_client_content turns）** | **必須** | **v3新規** |
| 9 | **ショップ検索トリガーをLLM判断方式に変更（`03_prompt_modification_spec.md`準拠）** | **必須** | **v3.3新規** |

### 1.2 やらないこと（v2から変更なし）

- ショップ説明用のREST TTS呼び出しは廃止
- `/api/chat` エンドポイント自体はショップデータ取得用に残す（音声生成はしない）
- `/api/tts/synthesize` はフォールバック時のみ使用
- PyAudio関連の移植

---

## 2. 全体アーキテクチャ（v2から変更なし）

v2のセクション2をそのまま維持。

---

## 3. バックエンド設計（v3改訂：REST版準拠の履歴再注入方式）

### 3.1 設計方針（v3.3改訂：キーワードマッチング完全廃止）

**REST版で機能していた原理をそのまま踏襲する。ショップ検索の判断もLLMに委ねる。**

```
REST版の原理:
1. プロンプト（concierge_ja.txt）に短期記憶ルール + ショップ検索の判断基準を記述
2. 毎回の API 呼び出しで全会話履歴を送信
→ Geminiが会話履歴から「何が確定済みか」を自然に把握
→ 条件が揃ったらGeminiが自ら「お探ししますね」と判断し、shops配列を返す
→ コード側のキーワード抽出・ステップ追跡・トリガー検知は一切なし

LiveAPI v3.3での再現:
1. プロンプト（LIVEAPI_CONCIERGE_SYSTEM）にREST版の短期記憶ルール
   + ショップ検索の判断基準（03_prompt_modification_spec.md準拠）を強化ハードコード
2. 再接続時に send_client_content(turns) で会話履歴を再送
→ REST版と同じ情報量をGeminiに渡す
→ キーワード抽出（short_term_memory）、hearing_step は廃止
→ should_trigger_shop_search()、SHOP_TRIGGER_KEYWORDS は完全削除
→ ショップ検索の発火判断はLLM（Gemini）に委ねる
```

**廃止した理由:**
- キーワードマッチングはREST版に存在しない。Claude（AI）が妄想で作った誤ったロジック
- キーワードリスト外の表現を拾えず網羅性が低い
- STT音声入力でトリガーが発火しない根本原因がこの方式にあった
- 会話履歴の再送 + プロンプトのルールで、LLMが自然に判断できる（REST版で実証済み）
- コードの複雑性が大幅に低減される

### 3.2 再接続時のコンテキスト復元（v3改訂）

#### 3.2.1 _send_history_on_reconnect()（v3新規）

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

#### 3.2.2 _get_context_summary()（v3改訂：最小限に簡素化）

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

#### 3.2.3 run() メインループ（v3.2改訂：フォールバック削除）

```python
async def run(self):
    """メインループ"""
    self.audio_queue_to_gemini = asyncio.Queue(maxsize=5)
    self.is_running = True

    try:
        while self.is_running:
            self.session_count += 1
            self.ai_char_count = 0
            self.needs_reconnect = False

            context = None
            if self.session_count > 1:
                context = self._get_context_summary()

            config = self._build_config(with_context=context)

            try:
                async with self.client.aio.live.connect(
                    model=LIVE_API_MODEL,
                    config=config
                ) as session:

                    if self.session_count == 1:
                        # 初回接続: ダミーメッセージで初期あいさつを発火
                        self._is_initial_greeting_phase = True
                        trigger_msgs = self.INITIAL_GREETING_TRIGGERS
                        mode_msgs = trigger_msgs.get(self.mode, trigger_msgs['chat'])
                        dummy_text = mode_msgs.get(self.language, mode_msgs['ja'])

                        await session.send_client_content(
                            turns=types.Content(
                                role="user",
                                parts=[types.Part(text=dummy_text)]
                            ),
                            turn_complete=True
                        )
                    else:
                        # ★ v3: 再接続時に会話履歴を再送
                        self._is_initial_greeting_phase = False
                        self.socketio.emit('live_reconnecting', {}, room=self.client_sid)

                        # 1. 会話履歴turnsを再送（turn_complete=False）
                        await self._send_history_on_reconnect(session)

                        # 2. トリガーメッセージ（turn_complete=True）
                        resume_text = self._resume_message or "続きをお願いします"
                        self._resume_message = None
                        await session.send_client_content(
                            turns=types.Content(
                                role="user",
                                parts=[types.Part(text=resume_text)]
                            ),
                            turn_complete=True
                        )
                        self.socketio.emit('live_reconnected', {}, room=self.client_sid)

                    await self._session_loop(session)

                    if not self.needs_reconnect:
                        break

            except Exception as e:
                error_msg = str(e).lower()
                if any(kw in error_msg for kw in
                       ["1011", "internal error", "disconnected",
                        "closed", "websocket"]):
                    logger.warning(f"[LiveAPI] 接続エラー、3秒後に再接続: {e}")
                    await asyncio.sleep(3)
                    self.needs_reconnect = True
                    continue
                else:
                    # ★ v3.2: フォールバック（switchToRestApiMode）は発動しない
                    # テストフェーズではエラーをログに出すだけ
                    logger.error(f"[LiveAPI] 致命的エラー: {e}")
                    break

    except asyncio.CancelledError:
        pass
    finally:
        self.is_running = False
        logger.info(f"[LiveAPI] セッション終了: {self.session_id}")
```

**v3.3での重要な変更点:**
1. `_session_loop()` 後の `_shop_search_pending` チェックを**削除** — キーワードマッチングによるトリガー検知自体を廃止したため不要
2. ショップ検索の発火はLLMの判断に委ねる（§5参照）

#### 3.2.4 _process_turn_complete()（v3.3改訂：キーワードマッチング完全削除）

```python
def _process_turn_complete(self):
    """
    ターン完了時の処理
    - 会話履歴の蓄積
    - 発言途切れ・累積文字数による再接続判定

    【v3.3重要】
    ショップ検索トリガーの検知は行わない。
    should_trigger_shop_search() は廃止済み。
    ショップ検索の発火はLLM（Gemini）の判断に委ねる（REST版と同じ原理）。
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

**v3.3で削除したもの:**
- `should_trigger_shop_search(ai_text)` の呼び出し — 完全削除
- `_shop_search_pending` のセット — 完全削除
- `_build_search_request()` の呼び出し — 完全削除

### 3.3 プロンプト設計（v3.3改訂：ショップ検索判断基準を追加ハードコード）

REST版 `concierge_ja.txt` の【短期記憶・セッション行動ルール（最重要）】に加え、
`03_prompt_modification_spec.md` に従うショップ検索の判断基準をハードコードする。

#### 3.3.1 ハードコードする短期記憶ルール + ショップ検索判断基準

`live_api_handler.py` の `LIVEAPI_CONCIERGE_SYSTEM` 内に以下を直接記述:

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

#### 3.3.2 ハードコードするショップ検索判断基準（v3.3新規）

`03_prompt_modification_spec.md` §5（コンシェルジュモード）の会話フロー例に従い、
LLMが自らショップ検索を行うタイミングを判断できるよう、以下をハードコード:

```
## 【ショップ検索の判断基準（REST版準拠・厳守）】

### 判断の原則
あなたは会話のキャッチボールを通じて、ユーザーの要望が十分に揃ったかを自ら判断する。
条件が揃ったと判断したら、「お探ししますね」等と発言し、ショップ検索を実行する。
コード側でトリガーを検知する仕組みは存在しない。あなたの判断が全てである。

### 検索に必要な最低条件
以下のうち、少なくとも2〜3項目が確定していれば検索可能と判断する:
- エリア・地域
- 料理ジャンルまたは利用シーン
- 予算感（任意・なくても可）
- 人数（任意・なくても可）

### 検索を提案するタイミング
- ユーザーが十分な条件を伝えた時点で、自然に「お探ししますね」と提案する
- 全項目が揃うまで待つ必要はない。ユーザーが「もういいから探して」等と言った場合は即座に検索する
- 簡易業態（ラーメン、カフェ等）はエリアだけでも検索可能

### 検索提案の例
「銀座の和食、個室があるお店をお探ししますね。」
「渋谷で5000円のイタリアン、探しますね。」
```

**設計根拠:** REST版では `concierge_ja.txt` のプロンプト指示により、LLMが自然にショップ検索タイミングを判断していた。コード側のキーワード検知は不要だった。LiveAPI版でも同じ原理を適用する。

#### 3.3.3 含めないもの（意図的な除外）

`concierge_ja.txt` から以下は **LiveAPIプロンプトに含めない**:

| 項目 | 除外理由 |
|---|---|
| JSON出力形式（`{"message": ..., "shops": ...}`） | LiveAPIは音声出力。JSONは不要 |
| shops配列の構造定義 | 同上。ショップデータはREST APIで別途取得 |
| 長期記憶サマリー生成ルール | LiveAPIモードでは別途実装。プロンプトに含めると肥大化 |
| actionフィールド（名前変更等） | LiveAPIモードでの名前変更は音声で処理。JSON actionは不要 |
| 予算表記ルール（漢数字等） | LiveAPIは音声出力なのでGeminiが自然に処理 |

### 3.4 廃止するコード（v3.3：完全削除対象）

以下は `live_api_handler.py` から**完全に削除**する:

| 削除対象 | 理由 |
|---|---|
| `SHOP_TRIGGER_KEYWORDS` 定数 | キーワードマッチング廃止 |
| `should_trigger_shop_search()` 関数 | キーワードマッチング廃止 |
| `_shop_search_pending` 属性 | キーワードマッチング廃止に伴い不要 |
| `_build_search_request()` メソッド | キーワードマッチング廃止に伴い不要 |
| `run()` 内の `_shop_search_pending` チェック | 同上 |
| `_process_turn_complete()` 内の `should_trigger_shop_search()` 呼び出し | 同上 |

---

## 4. フロントエンド設計（v2から変更なし）

v2のセクション4をそのまま維持。
短期記憶はサーバー側で完結するため、フロントエンド変更は不要。

---

## 5. ショップ提案の処理フロー（v3.3全面改訂：LLM判断方式）

### 5.1 設計方針（v3.3改訂）

**REST版と同じ原理: ショップ検索の発火はLLMの判断に委ねる。**

```
REST版:
LLM（Gemini）がプロンプトのルールに従い、条件が揃ったと判断
→ LLMがshops配列をJSON応答に含めて返す
→ コード側はJSON応答を受け取るだけ（トリガー検知ロジックなし）

LiveAPI v3.3:
LLM（Gemini）がプロンプトのルールに従い、条件が揃ったと判断
→ LLMが「お探ししますね」と音声で発言する
→ ★ この発言をサーバー側で検知し、REST APIでショップデータを取得する仕組みが必要
→ ★ ただし、検知方式はキーワードマッチングではない（§5.2で定義）
```

### 5.2 ショップ検索の発火方式（v3.3新規：要設計）

**v3.3時点の課題:**

REST版ではLLMのJSON応答にshops配列が含まれることで自動的に検索が発火した。
LiveAPIではLLMは音声で応答するため、JSON応答は返せない。

LLMが「条件が揃った、検索する」と判断した後、実際にREST APIでショップデータを取得する
ブリッジ機構が必要。この方式はテストフェーズで検証し、確定する。

**候補（テストで検証）:**
- LLMの会話履歴から、サーバー側が「検索提案に至った」ことを判断する
- LiveAPIセッション内でのfunction calling（利用可否はテストで確認）
- その他、テスト結果に基づく方式

**禁止事項（再掲）:**
- `should_trigger_shop_search()` によるキーワードマッチングは絶対に使用禁止
- AIの発話テキストをコード側でパターンマッチする方式は全て禁止

### 5.3 ショップ検索後のフロー（v2を維持）

ショップ検索が発火した後のフロー（REST APIデータ取得 → カード送信 → LiveAPI説明読み上げ）は
v2のセクション5.4以降をそのまま維持。

---

## 6. セッション管理（v3.3改訂）

### 6.1 LiveAPIセッションのライフサイクル（v3.3改訂）

```
[通常会話]
LiveAPIセッション#1 (初回接続・挨拶)
  ↓ 累積制限 or 発話途切れ
  ↓ ★ conversation_history はサーバー側で保持される
LiveAPIセッション#2 (再接続・send_client_content(turns)で履歴再送)
  ↓ ...
  ↓ ★ conversation_history は累積蓄積される
LiveAPIセッション#N (AIが条件を揃え、「お探ししますね」と判断・発言)
  ↓ ★ LLMの判断により検索が発火（コード側キーワード検知ではない）
  ↓ ★ 発火方式は §5.2 で定義（テストフェーズで確定）
  ↓
[ショップ検索]
REST API でショップデータ取得 (JSONのみ)
  ↓ shop_search_result イベントでブラウザにカード送信
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
  ↓ ★ conversation_history はショップ検索後もリセットしない（条件変更による再検索に対応）
```

### 6.2 セッション状態遷移（v2と同じ）

```python
class SessionState(Enum):
    CONVERSATION = "conversation"
    SHOP_SEARCHING = "shop_searching"
    SHOP_DESCRIBING = "shop_describing"
    RETURNING_TO_CHAT = "returning_to_chat"
```

### 6.3 短期記憶の実現方式（v3改訂：REST版準拠）

```
短期記憶の実現:

コード側の構造化管理（short_term_memory / hearing_step）は廃止。
REST版と同じ方式で短期記憶を実現する:

1. プロンプト: 短期記憶ルールを強くハードコード
   → Geminiに「会話履歴から条件を把握し、聞き直しを禁止」と指示
2. 会話履歴: conversation_history にサーバー側で蓄積
   → 再接続時に send_client_content(turns) で再送
3. Geminiが自然に判断
   → 会話履歴 + プロンプトルールにより、REST版と同等の精度

ライフサイクル:
- conversation_history はセッション終了まで蓄積（最大20ターン保持）
- 再接続時に直近10ターンを再送
- ショップ検索後もリセットしない（条件変更による再検索に対応）
```

---

## 7. 再接続メカニズム（v3改訂）

### 7.1 通常会話の再接続（v3改訂：REST版準拠）

| # | 項目 | v2 | v3 |
|---|---|---|---|
| 1 | 再接続トリガー | 発言途切れ / 長い発話 / 累積上限 | **変更なし** |
| 2 | システムプロンプト注入 | チャットログ断片 | **短期記憶ルール強化プロンプト + 直前質問の補足のみ** |
| 3 | 会話履歴の再送 | なし | **send_client_content()でturns再送（REST版の全履歴送信に相当）** |
| 4 | トリガーメッセージ | `"続きをお願いします"` | **変更なし**（履歴再送の後に送信） |

**再接続時のデータフロー（v3・REST版準拠）:**

```
                   system_instruction（毎回同じ）
                   ┌──────────────────────────────────────┐
                   │ LIVEAPI_CONCIERGE_SYSTEM              │
                   │ + 【短期記憶・セッション行動ルール】    │
                   │   → 聞き直し禁止                      │
                   │   → 会話履歴から条件を把握せよ          │
                   │   → 再接続時も同じ質問を繰り返すな      │
                   │ + 【ショップ検索の判断基準】            │  ← v3.3追加
                   │   → 条件が揃ったら自ら検索を提案        │  ← v3.3追加
                   │ + user_context（初期挨拶指示）          │
                   │ + LIVEAPI_COMMON_RULES                │
                   │                                        │
                   │ （補足：直前AIの質問があれば追記）      │
                   └──────────────────────────────────────┘

                   send_client_content (turns) ← REST版の全履歴送信に相当
                   ┌──────────────────────────────────────┐
                   │ user: "接待で使いたい"                  │
                   │ model: "どのエリアをお考えですか？"      │
                   │ user: "六本木で"                        │
                   │ model: "六本木ですね。ジャンルは？"      │
                   └──────────────────────────────────────┘

                   send_client_content (trigger)
                   ┌──────────────────────────────────────┐
                   │ user: "続きをお願いします"              │ turn_complete=True
                   └──────────────────────────────────────┘

→ Geminiは会話履歴から「六本木で接待、ジャンルを聞いている途中」と把握
→ プロンプトの短期記憶ルールにより「聞き直し禁止」が強く効く
→ REST版と同じ原理でコンテキストが復元される
```

### 7.2 ショップ説明の再接続（v2と同じ）

v2のセクション7.2をそのまま維持。

---

## 8. フォールバック戦略（v3.2改訂：テストフェーズでは無効化）

**テストフェーズでは `switchToRestApiMode()` によるフォールバックを完全に無効化する。**

| 項目 | v2 | v3.2（テストフェーズ） |
|---|---|---|
| バックエンド: `live_fallback` イベント | 致命的エラー時に emit | **emit しない** |
| フロントエンド: `switchToRestApiMode()`（フォールバック） | `live_fallback` 受信で発動 | **削除** |
| フロントエンド: `toggleRecording()`（マイクボタン） | LiveAPI中にマイクボタン押下→`switchToRestApiMode()` | **`terminateLiveSession()` を直接呼び出しに変更** |
| エラー時の挙動 | REST APIモードに切り替え | **エラーログを出して終了（ユーザーが手動リロード）** |

**削除対象（core-controller.ts）:**
- `live_fallback` イベントハンドラ内の `switchToRestApiMode()` 呼び出し
- `switchToRestApiMode()` メソッド自体（テストフェーズでは不要）

**変更対象（core-controller.ts）:**
- `toggleRecording()` 内の `switchToRestApiMode()` → `terminateLiveSession()` に置換
  - マイクボタンによるLiveAPI停止はフォールバックではなくユーザー操作による意図的な停止であるため、削除ではなく `terminateLiveSession()` の直接呼び出しに変更する

**理由:**
テストフェーズではLiveAPIの挙動を正確に検証する必要がある。
フォールバックが発動すると、LiveAPI側の問題が隠蔽され、デバッグが困難になる。
ただし、マイクボタンによるLiveAPI停止はユーザーの意図的な操作であり、フォールバックとは異なるため維持する。

---

## 9. 実装フェーズ計画（v3.3改訂）

### Phase 1: 基盤構築（v1と同じ）
- LiveAPI接続 → 音声送受信 → ブラウザ再生の最小ループ確認

### Phase 2: トランスクリプション（v1と同じ）
- input/output_transcription → チャット欄表示

### Phase 2.5: 短期記憶（v3改訂：REST版準拠）

| # | 内容 | 詳細 |
|---|---|---|
| 1 | `LIVEAPI_CONCIERGE_SYSTEM` に短期記憶ルール強化ハードコード | REST版concierge_ja.txtの短期記憶ルールをLiveAPI向けに凝縮 |
| 2 | `LIVEAPI_CONCIERGE_SYSTEM` にショップ検索判断基準をハードコード | `03_prompt_modification_spec.md` 準拠。LLMが自ら検索タイミングを判断 |
| 3 | `_send_history_on_reconnect()` 実装 | send_client_content()で会話履歴再送 |
| 4 | `_get_context_summary()` 簡素化 | 直前の質問のみ補足。構造化条件注入は廃止 |
| 5 | `run()` の再接続フロー修正 | 履歴再送 → トリガー の2段階送信 |
| 6 | キーワードマッチング関連コードの完全削除 | §3.4の削除対象を全て除去 |

**廃止した項目:**
- ~~`short_term_memory` dict~~ → 不要（Geminiが会話履歴から判断）
- ~~`_update_short_term_memory()`~~ → 不要（キーワード抽出廃止）
- ~~`_update_hearing_step()`~~ → 不要（ステップ追跡廃止）
- ~~`should_trigger_shop_search()`~~ → 不要（キーワードマッチング廃止）← v3.3追加
- ~~`SHOP_TRIGGER_KEYWORDS`~~ → 不要（キーワードマッチング廃止）← v3.3追加
- ~~`_shop_search_pending`~~ → 不要（キーワードマッチング廃止）← v3.3追加
- ~~`_build_search_request()`~~ → 不要（キーワードマッチング廃止）← v3.3追加

### Phase 3: ショップ説明のLiveAPI統一（v2と同じ）

v2のPhase 3をそのまま維持。

### Phase 4: 安定化（v3.3改訂）
- v2のPhase 4テスト項目に加え、以下を追加:
  - 短期記憶が再接続後も維持されるか
  - 同じ質問を繰り返さないか
  - LLMが自ら検索提案に到達するか（キーワードマッチングに依存しないこと）
  - ショップ検索発火方式（§5.2）の検証

### Phase 5: 最適化（v1と同じ）

---

## 10. 既知のリスク・未解決課題（v3.3改訂）

### 10.1 v1からの継続リスク（変更なし）
- LiveAPIプレビュー版の制約
- WebSocketの二重化
- async/syncの混在
- 音声再生の連続性

### 10.2 v2追加リスク（変更なし）

v2のセクション10.2をそのまま維持。

### 10.3 v3追加リスク

| リスク | 影響 | 対策 |
|---|---|---|
| send_client_content()のturns再送がトークンを消費 | コンテキストウィンドウの圧迫 | 直近10ターン・各150文字に制限。context_window_compression(32000)も有効 |
| 再接続回数が多い場合の累積コスト | 同じ履歴を何度も再送 | 再接続のたびに最新10ターンのみ送信。古いターンは自然に落ちる |
| プロンプトの短期記憶ルールの遵守率 | Geminiがルールを無視して聞き直す可能性 | ルールの優先順位を明記（本セクション > 他ルール）。テストで遵守率を検証 |

### 10.4 v3.3追加リスク

| リスク | 影響 | 対策 |
|---|---|---|
| LLMが検索提案タイミングを適切に判断できない | 条件不十分で検索 or いつまでも検索しない | プロンプトの判断基準を調整。REST版で実証済みの原理なので信頼性は高い |
| ショップ検索の発火方式（§5.2）が未確定 | 実装着手できない | テストフェーズで検証し確定。候補は§5.2に記載 |

### 10.5 REST版準拠の設計根拠

```
REST版の短期記憶 + ショップ検索が機能していた原理:
  1. プロンプト: 短期記憶ルール + ショップ検索判断基準
  2. 全会話履歴の送信: Geminiが文脈から条件を把握
  3. Geminiが自ら検索タイミングを判断
  → コード側のキーワード抽出・ステップ追跡・トリガー検知は一切なし
  → REST版で問題なく機能していた実績あり

LiveAPI v3.3で同じ原理を再現:
  1. プロンプト: REST版の短期記憶ルール + ショップ検索判断基準を強化ハードコード
  2. send_client_content(turns): 会話履歴再送（REST版の全履歴送信に相当）
  3. Geminiが自ら検索タイミングを判断（REST版と同じ）
  → キーワード抽出は廃止（網羅性の問題、コード複雑性の増大を回避）
  → should_trigger_shop_search() は完全削除（Claude妄想ロジック）
  → REST版で実証済みの方式なので信頼性が高い
```

---

## 11. テスト計画（v3.3改訂）

### 11.1〜11.2 v1と同じ

### 11.3 Phase 2.5 テスト項目（v3.3改訂）

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | コンシェルジュモードで「接待で六本木」と発言 | conversation_history に記録される |
| 2 | 再接続が発生した後 | AIが「エリアは？」「目的は？」と聞き直さない |
| 3 | 再接続後にAIが次の質問をする | 既に回答済みの条件をスキップし、未確認の条件を質問する |
| 4 | 条件を段階的に伝える（3〜4ターン） | conversation_history に全ターンが蓄積される |
| 5 | 全条件確定後 | AIが自ら「お探ししますね」と発言する（LLM判断） |
| 6 | AIの検索提案後 | ショップ検索が発火し、shop_search_result イベントがブラウザに送信される |
| 7 | ショップ検索実行後 | カードが表示され、LiveAPIで説明読み上げが開始される |
| 8 | 検索後に「別のエリアで」と言う | AIが他の条件を維持したまま新エリアで対応する |
| 9 | 再接続時のsend_client_content再送 | 直近10ターンが正しく再送される（ログで確認） |
| 10 | 任意のエリア名（リストにないもの含む） | Geminiが会話履歴から自然に理解する |
| 11 | フォールバックが発動しないこと | `live_fallback` イベントが発火しない。マイクボタン以外で `terminateLiveSession()` が呼ばれない |
| 12 | キーワードマッチング関連コードが存在しないこと | `should_trigger_shop_search`、`SHOP_TRIGGER_KEYWORDS` がコードに存在しない |

### 11.4 Phase 3 テスト項目（v2と同じ）

v2のセクション11.3をそのまま維持。

---

## 12. REST版（gourmet-support）との対応表（v3.3改訂）

LiveAPI移行で「何がどう変わったか」の対応表。
実装時に迷った場合の参照用。

| 機能 | REST版（gourmet-support） | LiveAPI版（gourmet-sp3 v3.3） |
|---|---|---|
| システムプロンプト | concierge_ja.txt（537行） | LIVEAPI_CONCIERGE_SYSTEM（短期記憶ルール + ショップ検索判断基準含む） |
| 会話履歴の送信 | 毎回全履歴をGemini REST APIに送信 | send_client_content(turns)で再接続時に再送 |
| 短期記憶 | Geminiが全履歴から自然に把握 | 同じ方式: 会話履歴再送 + プロンプトルール（REST版準拠） |
| ヒアリングステップ追跡 | Geminiが自然に追跡 | 同じ方式: Geminiが自然に追跡（コード側追跡は廃止） |
| 条件の重複質問防止 | concierge_ja.txt内のルールで制御 | 同じ方式: プロンプト内の短期記憶ルールで制御 |
| ショップ検索の発火 | LLMが自ら判断しshops配列を返す | **同じ原理: LLMが自ら判断（コード側キーワード検知は廃止）** |
| セッション管理 | RAMベースのSupportSession | LiveAPISession + conversation_history |
| 出力形式 | JSON（message + shops配列） | 音声（LiveAPI audio） |
| ショップ検索 | /api/chat が全て処理 | REST API(データ取得のみ) + LiveAPI(音声説明) |

---

*以上が LiveAPI 移植設計書 v3.3。*
*v3.2→v3.3の変更: should_trigger_shop_search()キーワードマッチングを完全廃止。ショップ検索トリガーをLLM判断方式に全面改訂。§0にキーワードマッチング禁止の厳守事項追加。§3.3にショップ検索判断基準のハードコード指示追加。§5をLLM判断方式で再定義。§3.4に削除対象コード一覧を明記。*
*v3の主な方針: キーワード抽出（short_term_memory / hearing_step）を廃止し、REST版と同じ「プロンプト + 会話履歴送信」方式に統一。ショップ検索トリガーもREST版と同じLLM判断方式に統一。*
*実装時は本設計書、`01_stt_stream_detailed_spec.md`、`03_prompt_modification_spec.md` を常に参照すること。*
