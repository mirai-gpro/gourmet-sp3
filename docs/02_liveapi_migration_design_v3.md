# Gemini LiveAPI 移植設計書 v3（gourmet-sp3）

> **作成日**: 2026-03-11
> **前版**: `docs/02_liveapi_migration_design_v2.md`（v2: 2026-03-11）
> **前提文書**: `docs/01_stt_stream_detailed_spec.md`, `docs/03_prompt_modification_spec.md`
> **成功事例**: `docs/stt_stream.py`（インタビューモードの再接続方式）
> **移植元安定版**: `github.com/mirai-gpro/gourmet-support`（REST版）
> **v3変更理由**: コンシェルジュモードの短期記憶がLiveAPI再接続時に失われる問題の対策

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

## 0. Claudeへの厳守事項（v1から継続）

### 防止ルール

1. **修正する前に、必ず `01_stt_stream_detailed_spec.md` の該当セクションを `Read` ツールで読む**
2. **「確認しました」と報告する場合、確認したファイルパスと行番号を明記する**
3. **仕様書に記載がない機能を追加しない**
4. **推測で API の引数やメソッド名を変えない**
5. **困ったらユーザーに聞く。推測で進めない**
6. **間違えたら戻る。修正を重ねない。`git checkout`で最後の正常状態に戻してからやり直す** ← v3追加
7. **診断ログを入れて→ログを見て→推測で修正、のサイクルは禁止** ← v3追加

---

## 1. 移植のスコープ（v3改訂）

### 1.1 やること

| # | 内容 | 優先度 | v3変更 |
|---|---|---|---|
| 1 | バックエンド: LiveAPI WebSocketプロキシの新設 | 必須 | 変更なし |
| 2 | フロントエンド: LiveAudioManager の実装 | 必須 | 変更なし |
| 3 | LiveAPI → REST API フォールバック機構 | 必須 | 変更なし |
| 4 | セッション再接続メカニズム | 必須 | 変更なし |
| 5 | トランスクリプション（文字起こし）表示 | 必須 | 変更なし |
| 6 | ショップ説明のLiveAPI読み上げ（1軒ごとに再接続） | 必須 | 変更なし |
| 7 | **プロンプトの短期記憶ルール強化（REST版準拠）** | **必須** | **v3改訂** |
| 8 | **再接続時の会話履歴再送（send_client_content turns）** | **必須** | **v3新規** |

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

### 3.1 設計方針（v3改訂：キーワード抽出廃止）

**REST版で機能していた短期記憶の原理をそのまま踏襲する。**

```
REST版の原理:
1. プロンプト（concierge_ja.txt）に短期記憶ルールを詳細に記述
2. 毎回の API 呼び出しで全会話履歴を送信
→ Geminiが会話履歴から「何が確定済みか」を自然に把握
→ コード側のキーワード抽出・ステップ追跡は一切不要

LiveAPI v3での再現:
1. プロンプト（LIVEAPI_CONCIERGE_SYSTEM）にREST版の短期記憶ルールを強化ハードコード
2. 再接続時に send_client_content(turns) で会話履歴を再送
→ REST版と同じ情報量をGeminiに渡す
→ キーワード抽出（short_term_memory）、hearing_step は廃止
```

**廃止した理由:**
- キーワード抽出はリスト外のエリア名・曖昧な表現を拾えず網羅性が低い
- REST版にはそもそも存在しない仕組みであり、REST版で問題なく機能していた
- 会話履歴の再送 + プロンプトの短期記憶ルールで同等の精度が出る
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

#### 3.2.3 run() の再接続フロー（v3改訂）

```python
# 再接続時の処理（run()内）
else:
    self._is_initial_greeting_phase = False
    self.socketio.emit('live_reconnecting', {}, room=self.client_sid)

    # 1. 会話履歴turnsを再送（turn_complete=False）
    #    → REST版の「毎回全履歴送信」と同等
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
    logger.info(f"[LiveAPI] 再接続: 履歴再送 + トリガー送信")
    self.socketio.emit('live_reconnected', {}, room=self.client_sid)
```

#### 3.2.4 _process_turn_complete()（v3改訂：短期記憶更新を削除）

```python
def _process_turn_complete(self):
    """
    ターン完了時の処理（v3改訂: キーワード抽出を廃止）
    短期記憶はプロンプト + 履歴再送で対応するため、
    コード側の条件抽出は行わない。
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

        # ショップ検索トリガー検知（v2と同じ）
        if should_trigger_shop_search(ai_text):
            # ... トリガー処理 ...
        else:
            logger.debug(f"[LiveAPI] トリガー未検知: '{ai_text[:50]}'")

        # 発言途切れチェック・文字数カウント・再接続判定（v2と同じ）
        # ... 省略（変更なし）...
```

### 3.2 再接続時のコンテキスト復元（v3全面改訂）

REST版では毎回全会話履歴をGemini APIに送信していた。
v3ではそれと同等の情報量を、以下の2つの手段で復元する:

1. **`_get_context_summary()`** → システムプロンプトに構造化された条件状態を注入
2. **`send_client_content()`** → 会話履歴turnsを再送

#### 3.2.1 _get_context_summary()（v3全面改訂）

```python
def _get_context_summary(self) -> str:
    """
    再接続時のコンテキスト注入。

    【v3変更点】
    v2: チャットログの断片（"user: xxx\nai: xxx"）
    v3: 構造化された条件状態 + ヒアリングステップ + 禁止事項

    【設計根拠】
    REST版では concierge_ja.txt の【短期記憶・セッション行動ルール】に
    「一度確定した情報は有効」「聞き直し禁止」と明記されていた。
    LiveAPIでは再接続でGeminiが全てを忘れるため、
    この「何が確定済みか」を明示的に伝える必要がある。
    """
    parts = []

    # ===== 1. 短期記憶（コンシェルジュモードのみ） =====
    if self.mode == 'concierge' and any(self.short_term_memory.values()):
        condition_labels = {
            'area': 'エリア',
            'purpose': '利用目的',
            'cuisine': '料理ジャンル',
            'atmosphere': '雰囲気',
            'party_size': '人数',
            'budget': '予算',
            'date': '日時',
        }

        confirmed = []
        unconfirmed = []

        for key, label in condition_labels.items():
            value = self.short_term_memory[key]
            if value:
                confirmed.append(f"  - {label}: {value}")
            else:
                unconfirmed.append(f"  - {label}: 未確認")

        parts.append("【ユーザーの確定済み条件（短期記憶）】")
        parts.append("以下は会話で既に確認済み。再度質問してはならない。")
        parts.extend(confirmed)

        if unconfirmed:
            parts.append("")
            parts.append("【未確認の条件】")
            parts.extend(unconfirmed)

        # ステップ指示
        step_instructions = {
            1: "まずエリアと利用目的を質問してください。",
            2: "料理ジャンル・雰囲気・人数を質問してください。",
            3: "予算を質問してください。",
            4: "日時を質問してください。なお日時はユーザーが言わなければ省略可。",
            5: "条件は十分です。「お探ししますね」と言って検索を開始してください。",
        }
        parts.append(f"\n【次のステップ】{step_instructions.get(self.hearing_step, '')}")

    # ===== 2. 直前の会話（最小限の参考情報） =====
    # send_client_content() で会話履歴turnsを別途再送するため、
    # ここでは最後のAI発言が質問だった場合のみ強調する
    if self.conversation_history:
        last_ai = None
        for h in reversed(self.conversation_history):
            if h['role'] == 'ai':
                last_ai = h['text']
                break

        if last_ai and ('?' in last_ai or '？' in last_ai
                        or 'ですか' in last_ai or 'ますか' in last_ai):
            parts.append(f"\n【直前のAIの質問（回答を待っています）】\n{last_ai[:200]}")

    return "\n".join(parts)
```

#### 3.2.2 再接続時の会話履歴再送（v3新規）

```python
async def _send_history_on_reconnect(self, session):
    """
    再接続時に会話履歴をsend_client_content()で再送する。

    【設計根拠】
    REST版では毎回全会話履歴をAPI引数として送信していた。
    LiveAPIのsend_client_content()のturnsパラメータで同等のことができる。
    これにより、Geminiは再接続後も直前の会話文脈を把握できる。

    【注意】
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
            turn_complete=False  # ★ まだターンは終わっていない
        )
        logger.info(f"[LiveAPI] 会話履歴 {len(history_turns)} ターン再送")
```

#### 3.2.3 run() の再接続フロー修正（v3改訂）

```python
async def run(self):
    """メインループ（v3改訂: 再接続時の履歴再送を追加）"""
    self.audio_queue_to_gemini = asyncio.Queue(maxsize=5)
    self.is_running = True

    try:
        while self.is_running:
            self.session_count += 1
            self.ai_char_count = 0
            self.needs_reconnect = False

            context = None
            if self.session_count > 1:
                context = self._get_context_summary()  # ★ v3: 構造化コンテキスト

            config = self._build_config(with_context=context)

            try:
                async with self.client.aio.live.connect(
                    model=LIVE_API_MODEL,
                    config=config
                ) as session:

                    if self.session_count == 1:
                        # 初回接続: ダミーメッセージで初期あいさつを発火（v2と同じ）
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
                        logger.info(f"[LiveAPI] 初期あいさつトリガー送信: '{dummy_text}'")
                    else:
                        # ★ v3改訂: 再接続時に会話履歴を再送
                        self._is_initial_greeting_phase = False
                        self.socketio.emit('live_reconnecting', {},
                                           room=self.client_sid)

                        # 1. 会話履歴turnsを再送（turn_complete=False）
                        await self._send_history_on_reconnect(session)

                        # 2. トリガーメッセージ（turn_complete=True）
                        await session.send_client_content(
                            turns=types.Content(
                                role="user",
                                parts=[types.Part(text="続きをお願いします")]
                            ),
                            turn_complete=True
                        )
                        logger.info("[LiveAPI] 再接続: 履歴再送 + トリガー送信")
                        self.socketio.emit('live_reconnected', {},
                                           room=self.client_sid)

                    await self._session_loop(session)

                    if not self.needs_reconnect:
                        break

            except Exception as e:
                # エラーハンドリング（v2と同じ）
                error_msg = str(e).lower()
                if any(kw in error_msg for kw in
                       ["1011", "internal error", "disconnected",
                        "closed", "websocket"]):
                    logger.warning(f"[LiveAPI] 接続エラー、3秒後に再接続: {e}")
                    await asyncio.sleep(3)
                    self.needs_reconnect = True
                    continue
                else:
                    logger.error(f"[LiveAPI] 致命的エラー: {e}")
                    self.socketio.emit('live_fallback', {
                        'reason': str(e)
                    }, room=self.client_sid)
                    break

    except asyncio.CancelledError:
        pass
    finally:
        self.is_running = False
        logger.info(f"[LiveAPI] セッション終了: {self.session_id}")
```

### 3.3 プロンプトの短期記憶ルール（v3改訂：REST版準拠で強化ハードコード）

REST版 `concierge_ja.txt` の【短期記憶・セッション行動ルール（最重要）】を
LiveAPI向けに凝縮して `LIVEAPI_CONCIERGE_SYSTEM` にハードコード。

#### 3.3.1 ハードコードした短期記憶ルール

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

**設計根拠:** REST版では `concierge_ja.txt` 内の同等ルールにより、コード側のキーワード抽出なしで短期記憶が成立していた。LiveAPI版でも同じ原理を適用する。

#### 3.3.2 含めないもの（意図的な除外）

`concierge_ja.txt` から以下は **LiveAPIプロンプトに含めない**:

| 項目 | 除外理由 |
|---|---|
| JSON出力形式（`{"message": ..., "shops": ...}`） | LiveAPIは音声出力。JSONは不要 |
| shops配列の構造定義 | 同上。ショップデータはREST APIで別途取得 |
| 長期記憶サマリー生成ルール | LiveAPIモードでは別途実装。プロンプトに含めると肥大化 |
| actionフィールド（名前変更等） | LiveAPIモードでの名前変更は音声で処理。JSON actionは不要 |
| 予算表記ルール（漢数字等） | LiveAPIは音声出力なのでGeminiが自然に処理 |

---

## 4. フロントエンド設計（v2から変更なし）

v2のセクション4をそのまま維持。
短期記憶はサーバー側で完結するため、フロントエンド変更は不要。

---

## 5. ショップ提案の処理フロー（v2から変更なし）

v2のセクション5をそのまま維持。

---

## 6. セッション管理（v3改訂）

### 6.1 LiveAPIセッションのライフサイクル（v3改訂）

```
[通常会話]
LiveAPIセッション#1 (初回接続・挨拶)
  ↓ 累積制限 or 発話途切れ
  ↓ ★ short_term_memory は保持される（サーバー側）
LiveAPIセッション#2 (再接続・構造化コンテキスト注入 + 履歴再送)
  ↓ ...
  ↓ ★ short_term_memory は累積更新される
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
  ↓ ★ short_term_memory はショップ検索後もリセットしない（追加検索対応）
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

## 8. フォールバック戦略（v2から変更なし）

v2のセクション8をそのまま維持。

---

## 9. 実装フェーズ計画（v3改訂）

### Phase 1: 基盤構築（v1と同じ）
- LiveAPI接続 → 音声送受信 → ブラウザ再生の最小ループ確認

### Phase 2: トランスクリプション（v1と同じ）
- input/output_transcription → チャット欄表示

### Phase 2.5: 短期記憶（v3改訂：REST版準拠）

| # | 内容 | 詳細 |
|---|---|---|
| 1 | `LIVEAPI_CONCIERGE_SYSTEM` に短期記憶ルール強化ハードコード | REST版concierge_ja.txtの短期記憶ルールをLiveAPI向けに凝縮 |
| 2 | `_send_history_on_reconnect()` 実装 | send_client_content()で会話履歴再送 |
| 3 | `_get_context_summary()` 簡素化 | 直前の質問のみ補足。構造化条件注入は廃止 |
| 4 | `run()` の再接続フロー修正 | 履歴再送 → トリガー の2段階送信 |

**廃止した項目:**
- ~~`short_term_memory` dict~~ → 不要（Geminiが会話履歴から判断）
- ~~`_update_short_term_memory()`~~ → 不要（キーワード抽出廃止）
- ~~`_update_hearing_step()`~~ → 不要（ステップ追跡廃止）

### Phase 3: ショップ説明のLiveAPI統一（v2と同じ）

v2のPhase 3をそのまま維持。

### Phase 4: 安定化（v3改訂）
- v2のPhase 4テスト項目に加え、以下を追加:
  - 短期記憶が再接続後も維持されるか
  - 同じ質問を繰り返さないか
  - 検索トリガーに正しく到達するか

### Phase 5: 最適化（v1と同じ）

---

## 10. 既知のリスク・未解決課題（v3改訂）

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

### 10.4 REST版準拠の設計根拠

```
REST版の短期記憶が機能していた原理:
  1. プロンプト: 短期記憶ルール（聞き直し禁止、業態別制御等）
  2. 全会話履歴の送信: Geminiが文脈から条件を把握
  → コード側のキーワード抽出・ステップ追跡は一切なし
  → REST版で問題なく機能していた実績あり

LiveAPI v3で同じ原理を再現:
  1. プロンプト: REST版の短期記憶ルールを強化ハードコード
  2. send_client_content(turns): 会話履歴再送（REST版の全履歴送信に相当）
  → キーワード抽出は廃止（網羅性の問題、コード複雑性の増大を回避）
  → REST版で実証済みの方式なので信頼性が高い
```

---

## 11. テスト計画（v3改訂）

### 11.1〜11.2 v1と同じ

### 11.3 Phase 2.5 テスト項目（v3改訂：REST版準拠）

| # | テスト内容 | 期待結果 |
|---|---|---|
| 1 | コンシェルジュモードで「接待で六本木」と発言 | conversation_history に記録される |
| 2 | 再接続が発生した後 | AIが「エリアは？」「目的は？」と聞き直さない |
| 3 | 再接続後にAIが次の質問をする | 既に回答済みの条件をスキップし、未確認の条件を質問する |
| 4 | 条件を段階的に伝える（3〜4ターン） | conversation_history に全ターンが蓄積される |
| 5 | 全条件確定後 | AIが「お探ししますね」と発言し、検索トリガーが発火する |
| 6 | 検索後に「別のエリアで」と言う | AIが他の条件を維持したまま新エリアで対応する |
| 7 | 再接続時のsend_client_content再送 | 直近10ターンが正しく再送される（ログで確認） |
| 8 | 任意のエリア名（リストにないもの含む） | Geminiが会話履歴から自然に理解する |

### 11.4 Phase 3 テスト項目（v2と同じ）

v2のセクション11.3をそのまま維持。

---

## 12. REST版（gourmet-support）との対応表（v3新規・参考資料）

LiveAPI移行で「何がどう変わったか」の対応表。
実装時に迷った場合の参照用。

| 機能 | REST版（gourmet-support） | LiveAPI版（gourmet-sp3 v3） |
|---|---|---|
| システムプロンプト | concierge_ja.txt（537行） | LIVEAPI_CONCIERGE_SYSTEM（短期記憶ルール含む） |
| 会話履歴の送信 | 毎回全履歴をGemini REST APIに送信 | send_client_content(turns)で再接続時に再送 |
| 短期記憶 | Geminiが全履歴から自然に把握 | 同じ方式: 会話履歴再送 + プロンプトルール（REST版準拠） |
| ヒアリングステップ追跡 | Geminiが自然に追跡 | 同じ方式: Geminiが自然に追跡（コード側追跡は廃止） |
| 条件の重複質問防止 | concierge_ja.txt内のルールで制御 | 同じ方式: プロンプト内の短期記憶ルールで制御 |
| セッション管理 | RAMベースのSupportSession | LiveAPISession + conversation_history |
| 出力形式 | JSON（message + shops配列） | 音声（LiveAPI audio） |
| ショップ検索 | /api/chat が全て処理 | REST API(データ取得のみ) + LiveAPI(音声説明) |

---

*以上が LiveAPI 移植設計書 v3（改訂版）。*
*v3改訂の主な変更: キーワード抽出（short_term_memory / hearing_step）を廃止し、REST版と同じ「プロンプト + 会話履歴送信」方式に統一。*
*v2との差分はセクション3（履歴再注入方式）、セクション7（再接続メカニズム）が中心。*
*実装時は本設計書、`01_stt_stream_detailed_spec.md`、`03_prompt_modification_spec.md` を常に参照すること。*
