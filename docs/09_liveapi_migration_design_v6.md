# グルメAIコンシェルジュ — LiveAPI移植設計書 v6

**作成日**: 2026-03-17
**ベース**: 01_stt_stream_detailed_spec.md (V1) + 05_liveapi_migration_design_v5.md (V5)
**統合対象**: spec 03（プロンプト）, 04（ショップ説明改善）, 06（マイクボタン修正）, 07（ショップカードJSON）, 08（A2Eリップシンク）

---

> **⚠️ 用語に関する注意**
>
> 本書内で「chatモード」と記載されている箇所は、正しくは **「グルメモード」** を指す。
> 本アプリに存在するモードは以下の2つのみ：
>
> | 本書での表記 | 正式名称 |
> |------------|---------|
> | chatモード | **グルメモード** |
> | conciergeモード | **コンシェルジュモード** |
>
> また、両モードとも **基本はLiveAPI仕様** である。RESTは一部限定的な用途にのみ使用する。

---

## 本書の位置づけ

V1（stt_stream.pyの詳細仕様）をベースに、V5（LiveAPI移植設計）の修正を加え、
V5以降の実証テストで追加・修正した仕様を統合した**現行仕様の正式記録**。

### 目的
1. 現在のアーキテクチャを正確に記録する（前半）
2. 今後の方針・課題を明確にする（後半）

### V1→V5→V6の変遷

| バージョン | 時期 | 主な変更 |
|-----------|------|---------|
| V1 (01_stt) | 初期 | stt_stream.pyの詳細仕様。FC無効、ハイブリッド（Live+REST+TTS） |
| V5 (05_design) | 2026-03-12 | REST全廃方針。FC有効化。キーワード検知の完全禁止 |
| **V6（本書）** | 2026-03-17 | REST一部残存を正式決定。A2E統合。テスト教訓の反映 |

---

## 第1部：現状アーキテクチャ（正確な記録）

---

## 1. プロジェクト概要

グルメAIコンシェルジュアプリ。2つの主目的を持つ実証テストフェーズ：

1. **REST APIからLiveAPIへの移行** — UX向上が最終目的、移行はその手段
2. **リップシンク・アバターの導入** — A2E (Audio2Expression) による52 blendshape駆動

### 1.1 REST API残存の判断

V5では「REST API全廃」を目指したが、実証テストで以下の判断に至った：

**REST APIを残す箇所と理由**：

| 機能 | 判断 | 理由 |
|------|------|------|
| テキストチャット (`/api/chat`) | **REST残存** | テキスト入力はLiveAPI不要。REST APIのJSON応答が安定 |
| ショップカードJSON生成 | **REST残存** | LiveAPIにJSON構造出力を求めると不安定。REST `SupportAssistant`の実績と安定性 |
| ショップ検索トリガー | **LiveAPI** | LLM判断→function calling（`search_shops`）。V5方針を維持 |
| 音声対話 | **LiveAPI** | 低遅延voice-to-voice。移行の核心 |
| ショップ説明読み上げ | **LiveAPI** | 再接続方式で1軒ずつ紹介 |
| TTS（テキスト読み上げ） | **Google Cloud TTS** | 事前生成キャッシュ音声（検索UX用） |
| A2E処理 | **サーバー側** | LiveAPI音声→A2Eサービス→blendshape係数をフロントに配信 |

**原則: 「表示はREST、音声はLiveAPI」**（spec 07で確定）

- LiveAPIは音声対話と検索トリガーのみ担当
- ショップカードUIに必要なJSON構造データはREST APIが生成
- これはUXを損なわないための判断であり、移行の後退ではない

---

## 2. システムアーキテクチャ

### 2.1 全体構成

```
┌─────────────────────────────────────────────────────────┐
│  Browser (Astro + TypeScript)                           │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐ │
│  │ CoreController│  │LiveAudioMgr   │  │LAMWebSocket  │ │
│  │ / Concierge   │  │(Audio I/O)    │  │Mgr (Avatar)  │ │
│  └──────┬───────┘  └──────┬────────┘  └──────┬───────┘ │
│         │ Socket.IO        │ Socket.IO         │ Pull    │
└─────────┼─────────────────┼──────────────────┼─────────┘
          │                  │                  │
     ┌────┴──────────────────┴──────────────────┘
     │
┌────┴──────────────────────────────────────────────────┐
│  Cloud Run (Flask + Socket.IO)                         │
│  ┌────────────────┐  ┌────────────────────────────┐   │
│  │app_customer_   │  │ live_api_handler.py         │   │
│  │support.py      │  │ ┌────────────────────────┐ │   │
│  │                │  │ │ LiveAPISession          │ │   │
│  │ /api/chat ─────┤  │ │  - Gemini LiveAPI接続   │ │   │
│  │ /api/session/* │  │ │  - 音声送受信           │ │   │
│  │ /api/tts/*     │  │ │  - Function Calling     │ │   │
│  │ /api/stt/*     │  │ │  - 再接続管理           │ │   │
│  │                │  │ │  - A2Eバッファリング    │ │   │
│  └────────┬───────┘  │ └────────────────────────┘ │   │
│           │          └─────────┬──────────────────┘   │
│           │                    │                       │
│  ┌────────┴───────┐  ┌────────┴───────┐               │
│  │support_core.py │  │A2E Service     │               │
│  │(REST Gemini)   │  │(Cloud Run)     │               │
│  └────────────────┘  └────────────────┘               │
└───────────────────────────────────────────────────────┘
```

### 2.2 データフロー（LiveAPI音声対話）

```
User Mic → Browser AudioWorklet (48kHz→16kHz downsample)
  → Socket.IO live_audio_in (base64 Int16)
    → Server → Gemini LiveAPI (PCM 16kHz)
      → LiveAPI Response (PCM 24kHz + transcription)
        → A2E Buffer → A2E Service → 52 blendshape coefficients
        → Socket.IO live_audio (PCM 24kHz, base64)
        → Socket.IO live_expression (blendshape frames)
          → Browser: Web Audio playback + LAMAvatar rendering
```

### 2.3 データフロー（ショップ検索 — function calling経由）

```
LiveAPI会話中、LLMが条件十分と判断
  → search_shops function call 発火
    → Server: shop_search_start イベント発行
    → Server: キャッシュ音声「お店をお探ししますね」再生（0.5秒後）
    → Server: shop_search_callback 実行
      → REST SupportAssistant.process_user_message()
        → Gemini REST API → JSON応答（shops配列）
      → enrich_shops_with_photos() → Google Places API補完
    → Server: キャッシュ音声「お待たせしました」再生
    → Socket.IO shop_search_result (shops + response)
      → Browser: ショップカードUI表示
    → Server: _describe_shops_via_live() → 各店舗をLiveAPI再接続で読み上げ
      → 各店舗ごとに新セッション → 音声 + A2E expression → Browser
    → send_tool_response() → LiveAPI通常会話に復帰
```

### 2.4 データフロー（テキストチャット — REST API）

```
User テキスト入力 → POST /api/chat
  → SupportAssistant.process_user_message()
    → Gemini REST API → JSON or 平文
      → shops配列あり → enrich_shops_with_photos() → ショップカード表示
      → shops配列なし → テキスト応答をチャットに表示
```

---

## 3. バックエンド詳細

### 3.1 エンドポイント一覧

| エンドポイント | メソッド | 用途 | APIタイプ |
|---------------|---------|------|----------|
| `/api/session/start` | POST | セッション作成 | REST |
| `/api/chat` | POST | テキストメッセージ処理 | REST (Gemini) |
| `/api/finalize` | POST | セッション完了・長期記憶保存 | REST |
| `/api/cancel` | POST | セッションキャンセル | REST |
| `/api/session/<id>` | GET | セッション取得 | REST |
| `/api/tts/synthesize` | POST | TTS音声合成 + A2E expression | REST (Cloud TTS) |
| `/api/stt/transcribe` | POST | 音声文字起こし | REST (Cloud Speech) |
| `/api/stt/stream` | POST | ストリーミング文字起こし | REST (Cloud Speech) |
| `/health` | GET | ヘルスチェック | REST |
| Socket.IO `live_start` | — | LiveAPIセッション開始 | WebSocket |
| Socket.IO `live_audio_in` | — | マイク音声チャンク送信 | WebSocket |
| Socket.IO `live_stop` | — | LiveAPIセッション終了 | WebSocket |

### 3.2 モード

| モード | 名前 | 特徴 |
|--------|------|------|
| `chat` | グルメモード | フレンドリー。条件1つで即検索。追加質問しない |
| `concierge` | コンシェルジュモード | 丁寧語。会話のキャッチボールで好みを引き出す。長期記憶あり |

### 3.3 Gemini モデル

| 用途 | モデル | 変更禁止 |
|------|-------|---------|
| LiveAPI（音声対話） | `gemini-2.5-flash-native-audio-preview-12-2025` | ⚠️ |
| REST API（テキスト・JSON生成） | `gemini-2.5-flash`（support_core.py） | ⚠️ |

### 3.4 LiveAPISession クラス

#### 3.4.1 接続設定（`_build_config()`）

```python
config = {
    "response_modalities": ["AUDIO"],
    "system_instruction": instruction,
    "tools": [{"function_declarations": [SEARCH_SHOPS_DECLARATION]}],
    "input_audio_transcription": {},
    "output_audio_transcription": {},
    "speech_config": {"language_code": "ja-JP"},
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False,
            "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
            "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
            "prefix_padding_ms": 100,
            "silence_duration_ms": 500,
        }
    },
    "context_window_compression": {
        "sliding_window": {"target_tokens": 32000}
    }
}
```

**注意**: `tool_config mode="ANY"` はLiveAPIでは利用不可（公式確認済み）

#### 3.4.2 Function Calling定義

```python
SEARCH_SHOPS_DECLARATION = types.FunctionDeclaration(
    name="search_shops",
    description="ユーザーの条件に基づいてレストランを検索する。条件が十分に揃ったと判断した時に呼び出す。",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "user_request": types.Schema(
                type="STRING",
                description="ユーザーの要望の要約（例: '六本木 接待 イタリアン 1万円 4名'）"
            )
        },
        required=["user_request"]
    )
)
```

#### 3.4.3 再接続メカニズム

**トリガー条件**（stt_stream.pyから転記、変更禁止）:

| 条件 | 閾値 | 判定 |
|------|------|------|
| 不完全発話 | — | テキスト末尾が助詞（、の、を、が、は等）で終了 |
| 長文発話 | 500文字 | 1回の応答が500文字以上 |
| 累積文字数 | 800文字 | セッション内のAI出力合計が800文字以上 |

**再接続フロー**:
1. 現セッション切断
2. 新セッション作成（同一システムプロンプト + コンテキスト要約追加）
3. `_send_history_on_reconnect(session)`: 直近10ターン（各150文字max）を `send_client_content(turns)` で再送
4. トリガーメッセージ送信（`_resume_message` または「続きをお願いします」）

**履歴再送のデータ形式**:
```python
types.Content(
    role="user" | "model",  # ★ "ai"ではなく"model"
    parts=[types.Part(text="...")]
)
```

#### 3.4.4 初期あいさつ制御

- 初回接続: ダミーメッセージ（`INITIAL_GREETING_TRIGGERS[mode][lang]`）でAIの初期あいさつを誘発
- `_is_initial_greeting_phase = True`: ダミーメッセージの `input_transcription` を非表示にする
- `greeting_done` イベント: あいさつ完了後にフロントエンドに通知
- `greeted_client_sids` セット: リロード時の重複あいさつ防止

#### 3.4.5 ショップ検索フロー（function calling経由）

1. LLMが`search_shops`を呼び出す（`response.tool_call`で検出）
2. `_handle_tool_call()` → `_handle_shop_search()`
3. 並列タスク起動:
   - 0.5秒後: キャッシュ音声「お店をお探ししますね」+ A2E
   - 6.5秒後: キャッシュ音声「只今、お店の情報を確認中です」+ A2E
4. `shop_search_callback` 実行（REST SupportAssistant経由でJSON取得）
5. タイマータスクをキャンセル
6. キャッシュ音声「お待たせしました」+ A2E再生
7. `shop_search_result` イベント発行
8. `_describe_shops_via_live()` で各店舗をLiveAPI再接続方式で読み上げ
9. `send_tool_response(function_responses=[...])` でLiveAPIに応答返却

**shop_search_callback の実装**（`app_customer_support.py`内）:
```python
session.update_mode(search_mode)  # concierge or chat
search_message = (
    f"以下の条件でお店を検索して、必ずJSON形式（shopsに5軒）で回答してください。"
    f"会話や質問は不要です。検索結果のみ返してください。\n"
    f"条件: {user_request}"
)
session.add_message('user', search_message, 'chat')
assistant = SupportAssistant(session, SYSTEM_PROMPTS)
result = assistant.process_user_message(search_message, 'conversation')
# → Places APIで写真・評価・URL等を補完
area = extract_area_from_text(user_request, lang)
enriched = enrich_shops_with_photos(result['shops'], area, lang)
```

#### 3.4.6 ショップ説明読み上げ（LiveAPI再接続方式）

```
1軒目: 新LiveAPIセッション → 店舗情報をプロンプト注入 → 「1軒目のお店を紹介してください」
  → 音声応答 + A2E → turn_complete
2軒目: 新LiveAPIセッション → 同上
  → 音声応答 + A2E → turn_complete
...
最後: 「以上、N軒のお店をご紹介しました」
→ 通常会話セッションに復帰（function calling再有効化）
```

**ルール**:
- ショップ説明セッションでは `tools` を**含めない**（FC無効化）
- 3〜5文で自然な話し言葉で紹介
- 50文字制限はTTS音声のみ。JSON出力には適用されない

#### 3.4.7 send_tool_response の呼び出し方

```python
tool_response = types.LiveClientToolResponse(
    function_responses=[types.FunctionResponse(
        name=fc.name,
        id=fc.id,
        response={"result": "検索結果をユーザーに表示しました"}
    )]
)
await session.send_tool_response(tool_response)
```

**注意**: キーワード引数 `function_responses=[...]` で渡す（SDK仕様）

### 3.5 プロンプト管理（GCS一元管理）

**正式なプロンプトはGCS上のファイルに一元管理**する。

| 対象 | GCSファイル |
|------|-------------|
| テキストチャット・conciergeモード | `prompts/concierge_ja.txt` |
| テキストチャット・chatモード | `prompts/support_system_ja.txt` |
| LiveAPI・chatモード | `prompts/concierge_ja.txt` or `support_system_ja.txt`（将来統合予定） |
| LiveAPI・conciergeモード | 同上 |

#### 暫定パッチ（Pythonベタ書き）

実証テスト中に仮説検証のためプロンプトを素早く変更する必要がある場合、**暫定措置として**Pythonコード内にベタ書きパッチを入れることを認める。

**現在の暫定パッチ**:

| 場所 | 内容 | ステータス |
|------|------|-----------|
| `live_api_handler.py` の `LIVEAPI_*` 定数 | LiveAPI用システムプロンプト（共通ルール、ショップ検索ルール、モード別プロンプト） | **暫定** — 安定確認後GCSへ統合 |
| ~~`support_core.py` の `json_enforcement`~~ | ~~REST API JSON形式強制ルール~~ | **削除済み** — GCSプロンプトに統合完了 |

**ルール**:
- 暫定パッチはあくまで一時的なもの。ある程度安定を確認したらGCS版に統合すること
- GCSプロンプトが正式版。暫定パッチはGCSプロンプトを**補完**するものであり、**矛盾**してはならない
- 暫定パッチを入れる際は、コメントで「暫定パッチ」であることを明記すること

#### LiveAPI暫定プロンプト構成（現在の状態）

| 定数名 | 内容 |
|--------|------|
| `LIVEAPI_COMMON_RULES` | 50文字制限、簡潔さ、1トピック1ターン、マークダウン禁止 |
| `LIVEAPI_SHOP_CARD_RULES` | search_shopsツール呼び出しルール。自力JSON生成禁止 |
| `LIVEAPI_CHAT_SYSTEM` | chatモード用。1ターン検索最優先 |
| `LIVEAPI_CONCIERGE_SYSTEM` | conciergeモード用。短期記憶ルール、ヒアリング制御、検索実行ルール |

#### REST API JSON出力ルール（GCSプロンプトに統合済み）

~~`support_core.py` の `json_enforcement` は削除済み。~~
JSON出力ルールは `concierge_ja.txt` / `support_system_ja.txt` のGCSプロンプトに一元化:
- ショップ提案時 → JSON形式（`message` + `shops`配列、1JSONオブジェクト厳守）
- 通常の会話・深掘り → 自然な文章でOK
- アクション時 → JSON形式（`action`フィールド追加）

### 3.6 ショップ検索の発火条件（モード別）

#### 共通ルール

- JSON出力はショップカード表示時のみ（`message` + `shops`配列）
- ショップカード表示は、お店検索の結果を返すときのみ
- 検索トリガーにコード側のキーワード検知は一切使わない（完全廃止済み）

#### conciergeモード（コンシェルジュ）

| 項目 | 内容 |
|------|------|
| 検索判断 | **LLMに一任** |
| 会話フロー | ヒアリング→条件収集→LLMが十分と判断→`search_shops` FC発火 |
| LiveAPI | LLM判断→function calling（`search_shops`） |
| REST テキストチャット | LLM判断→JSON応答（`shops`配列） |

#### chatモード（グルメ）

| 項目 | 内容 |
|------|------|
| 1ターン目 | ユーザーが条件を1つでも言ったら**即座に検索**。追加質問しない |
| 2ターン目以降 | LLMが判断: **深掘り**（既にカード表示済みの情報への質問）→ 自然な文章で回答 / **再検索**（新条件）→ 新たに検索実行 |
| LiveAPI | 1ターン目即FC→以降はLLM判断 |
| REST テキストチャット | 1ターン目即JSON応答→以降はLLM判断 |

#### 深掘り vs 再検索の判定（chatモード2ターン目以降）

| 判定 | 条件 | 応答形式 |
|------|------|---------|
| **深掘り** | 提案済み店舗への質問（個室、予算、ワイン等） | 自然な文章（JSON不要） |
| **再検索** | 異なるエリア・ジャンル指定、「他で〜」「別の〜」 | JSON（新しい`shops`配列） |

### 3.7 ショップカードJSON構造

```json
{
  "message": "ユーザーへのメッセージ全文",
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
      "image": "店舗画像URL",
      "highlights": ["特徴1", "特徴2", "特徴3"],
      "tips": "来店時のおすすめポイント",
      "specialty": "看板メニュー",
      "atmosphere": "雰囲気",
      "features": "特色",
      "hotpepper_url": "URL",
      "maps_url": "Google Maps URL",
      "tabelog_url": "食べログURL",
      "latitude": 35.6762,
      "longitude": 139.6503
    }
  ]
}
```

**enrichment（Places API補完）**: `enrich_shops_with_photos()` が以下を追加
- `photo_url`: Google Places Photo API
- `rating`, `reviewCount`: Google評価
- `maps_url`: Google Maps直リンク
- `tripadvisor_*`: TripAdvisor情報（利用可能な場合）

### 3.8 事前生成キャッシュ音声

モジュール起動時にGoogle Cloud TTSで3つの音声を事前生成（24kHz LINEAR16 PCM）:

| キー | テキスト | タイミング |
|------|---------|-----------|
| `searching` | 「お店をお探ししますね」 | FC発火0.5秒後 |
| `please_wait` | 「只今、お店の情報を確認中です。もう少々お待ち下さい」 | FC発火6.5秒後 |
| `announce` | 「お待たせしました、お店をご紹介しますね！」 | 検索完了時 |

ボイス: `ja-JP-Chirp3-HD-Leda`

---

## 4. A2E（Audio2Expression）リップシンク

### 4.1 概要

LAM/A2E（アリババ研究所、2025年10月論文）による52個のARKit blendshape係数駆動のリップシンク。
**フレームは送らない。係数だけ送る** — これがA2Eの核心。

### 4.2 サービス構成

| 項目 | 値 |
|------|-----|
| サービスURL | `A2E_SERVICE_URL` 環境変数（Cloud Run） |
| 入力 | 16kHz float32 LPCM（24kHz PCMからリサンプリング） |
| 出力 | `{names: string[52], frames: [{weights: float[52]}], frame_rate: 30}` |
| FPS | 30固定 |

### 4.3 3つのA2Eパス

#### パス1: ストリーミングバッファリング（LiveAPI音声対話中）

```
LiveAPI PCM応答 → _buffer_for_a2e() → バッファ蓄積
  → 閾値到達 or 句読点検出 → _send_to_a2e()
    → 24kHz→16kHz リサンプリング → A2Eサービス
      → live_expression イベント（blendshape frames）
```

**バッファ閾値**（実証テスト済み、変更禁止）:

| パラメータ | 値 | 根拠 |
|-----------|-----|------|
| `A2E_FIRST_FLUSH_BYTES` | 4,800 bytes (0.1秒) | 初回レイテンシ最小化 |
| `A2E_AUTO_FLUSH_BYTES` | 240,000 bytes (5秒) | 品質優先（短すぎると表情が不安定） |
| `A2E_MIN_BUFFER_BYTES` | 4,800 bytes | 最低バッファサイズ |

**句読点フラッシュ**: `output_transcription` で「。」「？」「！」を検出したらフラッシュ

#### パス2: 同期一括（キャッシュ音声・ショップ説明）

```
全PCMデータ → A2Eサービス（一括処理）
  → expression frames受信
    → live_expression_reset イベント（バッファクリア）
    → live_expression イベント（全frames）
    → live_audio イベント（PCMチャンク）
```

`_emit_audio_with_a2e_sync()`: expression先行送信 → audio後追い送信で同期

#### パス3: TTS + A2E同期（`/api/tts/synthesize`）

```
テキスト → Cloud TTS → MP3
  → A2Eサービス → expression frames
    → JSONレスポンス: {audio: base64, expressions: {...}}
```

### 4.4 フロントエンド同期メカニズム

- `firstChunkStartTime`: AudioContext.currentTimeベースのアンカー
- `getCurrentPlaybackOffset()`: アンカーからの経過時間を計算
- `getCurrentExpressionFrame()`: `frameRate × offsetMs` でフレーム番号を算出
- LAMAvatar renderer: 各レンダリングフレームで `getExpressionData()` コールバックで52個のblendshape値をpull

---

## 5. フロントエンド詳細

### 5.1 ファイル構成と責務

| ファイル | 行数 | 責務 |
|---------|------|------|
| `core-controller.ts` | ~1255 | 共通ベース。Socket.IOイベント、LiveAPI状態管理、ショップカード表示 |
| `concierge-controller.ts` | ~685 | コンシェルジュモード固有。アバター連携、並行TTS、ショップ説明フロー |
| `chat-controller.ts` | — | グルメモード固有ロジック |
| `live-audio-manager.ts` | ~374 | マイク入力(48→16kHz)、PCM再生(24kHz)、A2E expression同期 |
| `lam-websocket-manager.ts` | ~181 | Gaussian Splatレンダラ連携、blendshapeマッピング |
| `audio-manager.ts` | — | 旧マイク入力（REST用） |
| `audio-sync-player.ts` | — | 音声再生バッファ |

### 5.2 Socket.IOイベント

**クライアント → サーバー**:

| イベント | データ | 用途 |
|---------|--------|------|
| `live_start` | `{session_id, mode, language}` | LiveAPIセッション開始 |
| `live_audio_in` | `{audio: base64}` | マイク音声チャンク |
| `live_stop` | — | LiveAPIセッション終了 |

**サーバー → クライアント**:

| イベント | データ | 用途 |
|---------|--------|------|
| `live_ready` | — | 接続完了 |
| `greeting_done` | — | 初期あいさつ完了（マイクボタン有効化） |
| `user_transcript` | `{text, is_final}` | ユーザー発話テキスト |
| `ai_transcript` | `{text, type}` | AI応答テキスト。`type='shop_description'`のときチャット非表示 |
| `turn_complete` | — | AI応答完了 |
| `interrupted` | — | ユーザー割り込み |
| `live_audio` | `{audio: base64, chunk_index}` | AI音声PCMチャンク (24kHz) |
| `live_expression` | `{names, frames, frame_rate, chunk_index}` | A2E blendshapeデータ |
| `live_expression_reset` | — | expression状態リセット（新音声セグメント開始前） |
| `shop_search_start` | — | ショップ検索開始（待機UI表示） |
| `shop_search_result` | `{shops, response}` | 検索結果（カードUI表示） |
| `live_reconnecting` | — | 再接続中 |
| `live_reconnected` | — | 再接続完了 |
| `live_stopped` | — | セッション終了 |

### 5.3 マイクボタンのライフサイクル

1. **ページ読み込み**: `initialize()` → `getUserMedia()` でAudioContextアンロック → LiveAPIセッション開始
2. **あいさつ完了**: `greeting_done` イベント受信 → マイクボタン有効化（`isRecording = false`）
3. **マイクON**: `toggleRecording()` → `isStreaming = true`（セッション維持、音声キャプチャ開始）
4. **マイクOFF**: `toggleRecording()` → `isStreaming = false`（セッション維持、音声キャプチャ停止）
5. **セッション終了**: `terminateLiveSession()` → セッション破棄

**重要**: マイクボタンは**セッションを破棄しない**。`isStreaming`のトグルのみ。
（spec 06で確定。セッション破棄→再作成するとあいさつが重複する問題の修正）

### 5.4 LAMAvatar連携

```
concierge-controller.ts
  ├─ linkLamAvatar() → window.__lamAvatarController.initialize(liveAudioManager)
  └─ lam-websocket-manager.ts
       ├─ GaussianSplatRenderer.getInstance({
       │    getChatState: () => currentState,
       │    getExpressionData: () => blendshapeMap  ← LiveAudioManager から取得
       │  })
       └─ updateExpression(): ARKit 52 names → Map<string, number>
```

**カメラ設定**: z=0.4, y=1.73, target.y=1.62

---

## 6. セッションライフサイクル

```
[ページ読み込み]
  → /api/session/start (REST)
  → Socket.IO live_start
    → LiveAPISession #1 (初期あいさつ、FC有効)

[通常会話]
LiveAPI Session #1
  ↓ 累積800文字 or 不完全発話
LiveAPI Session #2 (再接続、履歴再送、FC有効)
  ↓ ... (必要に応じて再接続繰り返し)

[ショップ検索] LLMがsearch_shops FC呼び出し
  → shop_search_callback (REST SupportAssistant)
  → shop_search_result → ブラウザにカード表示

[ショップ説明読み上げ] (FC無効)
LiveAPI Session #N+1 (1軒目)
LiveAPI Session #N+2 (2軒目)
...
LiveAPI Session #N+M (最後の店 + 締め)

[通常会話復帰] (FC再有効化)
LiveAPI Session #N+M+1 (「気になるお店はありましたか？」)

[テキスト入力] (LiveAPIとは独立)
  → POST /api/chat → REST SupportAssistant
```

---

## 第2部：今後の方針・課題

---

## 7. 実証テストで判明した技術的制約

### 7.1 LiveAPI Function Callingの発火安定性

- LiveAPIのfunction callingは**発火する場合としない場合がある**
- プロンプト設計で発火率を上げることは可能だが、100%保証はできない
- `tool_config mode="ANY"` は使用不可（LiveAPI制約）
- **対策**: プロンプトに明示的な呼び出しルールを記載（`LIVEAPI_SHOP_CARD_RULES`）

### 7.2 A2Eバッファ閾値

| パラメータ | テスト結果 |
|-----------|-----------|
| 初回0.1秒 (4,800 bytes) | レイテンシと品質のバランス最適。これ以上小さいと表情データが不安定 |
| 後続5秒 (240,000 bytes) | 十分な音声長で安定した表情生成。短い（0.5秒等）と表情が不自然 |

**これらの値は実証テスト済み。変更しないこと。**

### 7.3 LiveAPIの累積出力制限

- 800文字を超えると応答品質が低下する（stt_stream.pyでも確認済み）
- 再接続で文字数カウントをリセットする方式で対応
- 32,000トークンのスライディングウィンドウで文脈圧縮

### 7.4 REST APIのJSON応答安定性

- conciergeモードのREST APIでは、会話が長くなるとJSON形式ではなく平文で応答することがある
- Python側のJSON強制ルール（support_core.py）とGCSプロンプト（concierge_ja.txt）の間に矛盾がある
  - GCS: 「必ずJSON形式のみで応答」
  - Python: 「通常の会話→平文、ショップ提案時のみ→JSON」
- 会話履歴が全て平文の場合、Geminiがパターンを学習して平文を継続する傾向

### 7.5 ショップ説明のTTS非表示

- `ai_transcript` イベントの `type='shop_description'` でフロントエンドが判別
- ショップカードが既にUIに表示されているため、TTS説明テキストはチャットに表示しない（spec 04）

---

## 8. REST API残存の判断根拠

### 8.1 UX観点での判断

**LiveAPIの強み**: 低遅延voice-to-voice、自然な会話、リアルタイム性
**LiveAPIの弱み**: 構造化データ（JSON）出力が不安定、長文応答の品質低下

**REST APIの強み**: JSON構造の安定出力、豊富な会話履歴でのコンテキスト維持
**REST APIの弱み**: 音声対話には不向き（リクエスト→レスポンスの同期モデル）

### 8.2 各機能の判断理由

| 機能 | 判断 | UX上の理由 |
|------|------|-----------|
| 音声対話 | LiveAPI | 低遅延が必須。REST+TTSでは体験が根本的に劣る |
| テキストチャット | REST | テキスト入出力に音声パイプラインは不要。REST APIの方が安定 |
| 検索トリガー | LiveAPI FC | 音声会話中にシームレスに検索を発動するにはFCが最適 |
| ショップカードJSON | REST | 安定したJSON構造が必須。LiveAPIのJSON出力は不安定 |
| ショップ説明読み上げ | LiveAPI | 自然な音声で紹介するのが目的。voice-to-voiceの強みを活かす |
| A2E処理 | サーバー側 | ブラウザからA2Eサービスへの直接通信はCORS・レイテンシの問題 |

### 8.3 原則

> UXを損なうLiveAPI移行は本末転倒。
> 移行はUX向上の手段であり、目的ではない。
> 各機能について「LiveAPIの方がUXが良いか？」で判断する。

---

## 9. Claude向け作業ルール

### 9.1 知識ベースの限界

| 技術 | リリース時期 | Claudeの知識 |
|------|------------|-------------|
| LAM/A2E (Audio2Expression) | 2025年10月 | **なし** |
| Gemini LiveAPI | 2025年12月末 | **なし** |
| `gemini-2.5-flash-native-audio-preview` | 2025年12月末 | **なし** |
| LiveAPIのfunction calling仕様 | 2025年12月末 | **なし** |

**推論で進めると必ず間違う。唯一の正解は「コードを読む」「仕様書を読む」「ユーザーに聞く」。**

### 9.2 絶対守るべきルール

1. **推論するな。確認しろ。** — 「可能性としては…」「おそらく…」は禁止
2. **仕様書を先に読め** — `docs/`配下を必ず先に読んでから作業開始
3. **勝手に修正するな** — コード修正は必ずユーザーの許可を得てから
4. **フォールバック禁止** — 根本原因を特定・修正せよ。問題を覆い隠すな
5. **指示・回答は最後まで読め** — 部分的に読んで着手するな
6. **根本原因を追え** — 副次的症状に飛びつくな

### 9.3 修正前チェックリスト

1. 仕様書を読んだか？
2. 現在のコードを読んだか？（推論禁止）
3. 自分の知識ベースにある技術か？（なければユーザーに確認）
4. ユーザーの許可を得たか？
5. 修正は1つだけか？（複数同時禁止）

### 9.4 禁止パターン

| パターン | 具体例 | なぜ危険か |
|---------|--------|-----------|
| 「一般的にはこう」修正 | A2E閾値を「常識的」な値に変更 | 実証テスト済みの値を破壊 |
| 知らない技術を知ったかぶり | 「構造的レイテンシ2-4秒」と断言 | 修正前は遅延ゼロだった |
| フォールバック自動挿入 | キーワード検出で検索発動 | LLM判断方式への退行 |
| 確認なし本番設定変更 | モデル名変更してコミット | 破壊的変更 |
| 的外れ修正の連鎖 | 9回の的外れ修正 | 2回で直らなかったら止まれ |
| プロンプト経路の混同 | Pythonだけ修正しGCS未修正 | 本番で古いプロンプトが使われ続ける |

### 9.5 変更禁止ファイル（明示的指示がない限り）

- `api_integrations.py` — 外部API連携
- `long_term_memory.py` — Supabase長期記憶
- PWA設定、`i18n.ts`
- `.github/workflows/` — CI/CDワークフロー

### 9.6 変更に特別な注意が必要な項目

以下は**必ずユーザーに確認してから**変更すること：
- モデル名・モデルバージョン
- 本番設定（環境変数、デプロイ設定）
- GCS上のプロンプトファイル
- セキュリティ・IAM関連
- A2Eバッファ閾値
- 再接続閾値（800文字、500文字）

---

## 10. 今後の課題

### 10.1 ショップカードJSON応答の安定化

**現状**: conciergeモードのREST APIで、Geminiがショップ提案時にJSON形式ではなく平文で応答することがある
**原因**: GCSプロンプトとPython JSON強制ルールの矛盾、会話履歴の平文パターン学習
**対策案**: 要検討

### 10.2 LiveAPI FC発火率の向上

**現状**: function callingが発火しないケースが散見される
**制約**: `tool_config mode="ANY"` が使えない
**対策案**: プロンプト改善の継続

### 10.3 ショップ説明の並列化・事前接続

**現状**: 各店舗を順次接続→読み上げ→切断
**改善案**: N+1店舗を事前接続して切り替えレイテンシを削減（spec 04）
**ステータス**: 未実装（LiveAPIの並行セッション制限の確認が必要）

### 10.4 A2Eの長期安定性

**現状**: 動作確認済みだが長時間セッションでの安定性は未検証
**リスク**: expression frameの蓄積によるメモリ増加、同期ずれの累積

### 10.5 長期記憶のREST API統合

**現状**: 長期記憶（Supabase）はconcierge LiveAPIのみで利用
**課題**: REST APIテキストチャットでも長期記憶を活用すべきか

---

## 付録

### A. ファイル構成

```
gourmet-sp3/
├── support-base/                  ← バックエンド (Cloud Run)
│   ├── app_customer_support.py    ← Flask + Socket.IO メインアプリ
│   ├── live_api_handler.py        ← Gemini LiveAPI セッション管理
│   ├── support_core.py            ← ビジネスロジック（REST Gemini API）
│   ├── api_integrations.py        ← 外部API連携 (Places, HotPepper等)
│   ├── long_term_memory.py        ← Supabase長期記憶
│   └── prompts/                   ← GCSプロンプトファイル（REST API用）
│       ├── concierge_ja.txt
│       └── support_system_ja.txt
│
├── src/scripts/chat/              ← フロントエンド (Astro + TS)
│   ├── core-controller.ts         ← 共通ベース
│   ├── chat-controller.ts         ← グルメモード
│   ├── concierge-controller.ts    ← コンシェルジュモード + アバター
│   ├── live-audio-manager.ts      ← マイク入力・PCM再生・A2E同期
│   ├── lam-websocket-manager.ts   ← Gaussian Splat + blendshape
│   ├── audio-manager.ts           ← 旧マイク入力（REST用）
│   └── audio-sync-player.ts       ← 音声再生バッファ
│
├── docs/                          ← 仕様書
│   ├── 01_stt_stream_detailed_spec.md    ← V1: stt_stream.py詳細仕様
│   ├── 02_liveapi_migration_design.md    ← 初版設計書
│   ├── 02_liveapi_migration_design_v2.md ← V2設計書
│   ├── 02_liveapi_migration_design_v3.md ← V3.3設計書
│   ├── 03_prompt_modification_spec.md    ← プロンプト修正仕様
│   ├── 04_shop_description_improvements_v4.md ← ショップ説明改善
│   ├── 05_liveapi_migration_design_v5.md ← V5設計書
│   ├── 06_micbutton_greeting_fix_spec.md ← マイクボタン修正仕様
│   ├── 07_shop_card_json_spec.md         ← ショップカードJSON仕様
│   ├── 08_lipsync_avatar_spec (1).md     ← A2Eリップシンク仕様
│   └── 09_liveapi_migration_design_v6.md ← ★本書（V6統合仕様）
│
└── CLAUDE.md                      ← Claude作業ガイド
```

### B. 仕様書バージョン履歴

| バージョン | ファイル | 主な内容 |
|-----------|---------|---------|
| V1 | 01_stt_stream_detailed_spec.md | stt_stream.py詳細仕様。FC無効、ハイブリッド構成 |
| V2 | 02_liveapi_migration_design_v2.md | LiveAPI移植初版。キーワード検知方式（後に廃止） |
| V3.3 | 02_liveapi_migration_design_v3.md | 短期記憶ルール、履歴再送、再接続改善 |
| V4 | 04_shop_description_improvements_v4.md | ショップ説明UX改善、TTS非表示 |
| V5 | 05_liveapi_migration_design_v5.md | REST全廃方針、FC有効化、キーワード検知完全禁止 |
| **V6** | **09_liveapi_migration_design_v6.md** | **REST一部残存確定、A2E統合、テスト教訓統合** |

### C. 参照すべき外部ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| `docs/2502.17796v2.pdf` | LAM/A2E論文（アリババ研究所） |
| `docs/Gemini_LiveAPI_ans.txt` | Gemini LiveAPI Q&A |
| `DESIGN_SPEC_PHASE1.md` | Phase1全体設計書 |
