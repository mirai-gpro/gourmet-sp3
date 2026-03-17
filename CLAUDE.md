# CLAUDE.md — gourmet-sp3 開発ガイド

## プロジェクト概要

グルメAIコンシェルジュアプリ。Gemini LiveAPIによるリアルタイム音声対話 + A2Eリップシンクアバター。
- **フロントエンド**: Astro + TypeScript
- **バックエンド**: Flask + Socket.IO (Python) → Cloud Run
- **AI**: Gemini LiveAPI (音声対話), Gemini REST API (テキストチャット・ショップ検索)
- **アバター**: LAM/A2E (Audio2Expression) — 52個のblendshape係数による低遅延リップシンク

---

## 絶対守るべきルール

### 1. 推論するな。確認しろ。

- **知識ベースにない技術（Gemini LiveAPI, LAM/A2E等）について推測で語るな**
- コード1行読めばわかることを推論で済ませるな
- 「可能性としては…」「おそらく…」は禁止。事実だけ述べろ
- わからないことは「わかりません」と言え

### 2. 仕様書を先に読め

- `docs/` 配下の仕様書を**必ず先に読んでから**作業を開始する
- 設計意図が明記された判断を「問題」と指摘するな
- ユーザーの実証テスト結果はパラメータ値や設計判断の根拠。安易に否定するな

### 3. 勝手に修正するな

- **コード修正は必ずユーザーの許可を得てから実行する**
- 特に以下は絶対に確認なしで変更するな:
  - モデル名・モデルバージョン
  - 本番設定（環境変数、デプロイ設定）
  - CI/CDワークフロー
  - GCS上のプロンプトファイル
  - セキュリティ・IAM関連

### 4. フォールバック禁止

- 問題の根本原因を特定・修正せよ
- フォールバック機構で問題を覆い隠すな
- キーワード検出による代替ロジックは絶対にやるな

### 5. 指示・回答は最後まで読め

- ユーザーの指示を部分的に読んで着手するな
- Gemini等LLMの回答も全て読んでから実装せよ
- 5つの対策が提示されたら5つ全て理解してから動け

### 6. 根本原因を追え

- 副次的症状に飛びつくな（例: 発火しない問題を放置して1008エラーに対処するな）
- 修正前に正常だったなら、修正が原因。推論で別の原因を探すな
- ユーザーの「修正前は正常だった」という事実報告は最優先の手がかり

---

## プロジェクト構造

```
gourmet-sp3/
├── support-base/                  ← バックエンド (Cloud Run)
│   ├── app_customer_support.py    ← Flask + Socket.IO メインアプリ
│   ├── live_api_handler.py        ← Gemini LiveAPI セッション管理
│   ├── support_core.py            ← ビジネスロジック
│   ├── api_integrations.py        ← 外部API連携 (Places, HotPepper等)
│   ├── long_term_memory.py        ← Supabase長期記憶
│   └── prompts/                   ← プロンプトファイル (GCSにも配備)
│       ├── concierge_ja.txt
│       └── support_system_*.txt
│
├── src/scripts/chat/              ← フロントエンド (Astro + TS)
│   ├── core-controller.ts         ← 共通ベース
│   ├── chat-controller.ts         ← グルメモード
│   ├── concierge-controller.ts    ← コンシェルジュモード + アバター
│   ├── audio-manager.ts           ← マイク入力
│   ├── live-audio-manager.ts      ← LiveAPI音声送受信
│   ├── audio-sync-player.ts       ← 音声再生バッファ
│   └── lam-websocket-manager.ts   ← A2E WebSocket管理
│
├── docs/                          ← 仕様書 (必ず先に読む)
│   ├── 02_liveapi_migration_design_v3.md  ← v3.3仕様書 (メイン)
│   ├── 03_prompt_modification_spec.md     ← プロンプト修正仕様
│   ├── 07_shop_card_json_spec.md          ← ショップカードJSON仕様
│   ├── 08_lipsync_avatar_spec (1).md      ← A2Eリップシンク仕様
│   └── ...
│
└── DESIGN_SPEC_PHASE1.md          ← Phase1全体設計書
```

---

## アーキテクチャの要点

### プロンプトの配信経路（混同するな）

| 経路 | 対象 | ファイル |
|------|------|---------|
| GCS | テキストチャット (REST API) | `prompts/concierge_ja.txt` 等 → GCSから読み込み |
| Pythonハードコード | LiveAPI | `live_api_handler.py` 内の `LIVEAPI_CONCIERGE_SYSTEM` 等 |

- **GCSのプロンプトとPythonハードコードは別系統**。片方だけ修正して矛盾を残すな
- GCSファイルの修正にはIAMロール・セキュリティの制約がある。勝手にデプロイワークフローを変えるな

### ショップ検索の発火方式

- **LLMの判断に委ねる**（v3.3で確定）
- コード側でAI発話テキストからキーワード検知する処理は**完全に廃止済み**
- `should_trigger_shop_search()`, `SHOP_TRIGGER_KEYWORDS` は削除済み

### A2E (Audio2Expression) リップシンク

- **フレームは送らない**。52個のblendshape係数だけを送る — これがA2Eの核心
- バッファ閾値（初回0.1秒、後続5秒）は実証テスト済みの値。変更するな
- 詳細は `docs/08_lipsync_avatar_spec (1).md` を参照

### LiveAPIセッション管理

- セッションタイムアウト時は再接続 → 会話履歴再送（`_send_history_on_reconnect()`）
- `tool_config mode="ANY"` はLive APIでは**利用不可**（公式ドキュメント確認済み）
- `send_tool_response()` はキーワード引数 `function_responses=[...]` で渡す（SDK仕様）

---

## 作業時の心得

1. **確認 → 事実 → 行動** の順序を守る
2. **公式ドキュメントを最初に確認**する（LLMの回答ではなく）
3. **LLMにLLMの動作保証を求めても意味がない**
4. 1つの問題に対して的外れな修正を連鎖させるな。立ち止まって根本原因を考えろ
5. 「ちょっと確認すれば済むこと」を推論で済ませるな — それが泥沼の入口

---

## 参照すべき仕様書（優先順位）

1. `docs/02_liveapi_migration_design_v3.md` — v3.3 LiveAPI移植設計書（メイン）
2. `docs/03_prompt_modification_spec.md` — プロンプト修正仕様
3. `docs/07_shop_card_json_spec.md` — ショップカードJSON仕様
4. `docs/08_lipsync_avatar_spec (1).md` — A2Eリップシンク仕様
5. `docs/04_shop_description_improvements_v4.md` — ショップ説明改善
6. `DESIGN_SPEC_PHASE1.md` — Phase1全体設計書

---

## 変更してはいけないファイル（明示的指示がない限り）

- `api_integrations.py` — 外部API連携
- `long_term_memory.py` — Supabase長期記憶
- PWA設定、`i18n.ts`
- `.github/workflows/` — CI/CDワークフロー
