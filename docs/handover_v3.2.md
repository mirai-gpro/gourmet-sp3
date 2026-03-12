# V3.2 実装 引継ぎ文

## タスク

`docs/02_liveapi_migration_design_v3.md`（v3.2仕様書）に従って、コードを実装する。

## 現在の状態

- **ブランチ**: `claude/gourmet-ai-v3-testing-EG5GN`
- **コードの状態**: `6e32fed`（v3修正以前）と同一。コードにv3系の変更は一切入っていない
- **仕様書の状態**: v3.2a に更新済み（`docs/02_liveapi_migration_design_v3.md`）
- **HEAD**: `25a6726`（コードを6e32fedに復元したコミット）

## やること

v3.2仕様書を読み、`6e32fed` のコードに対して仕様書の内容をゼロから実装する。

### 対象ファイルと変更内容

**バックエンド: `support-base/live_api_handler.py`**

1. **プロンプトに短期記憶ルールを追加**（仕様書セクション3.3.1）
   - `LIVEAPI_CONCIERGE_SYSTEM` にREST版の短期記憶ルールを追加

2. **`_send_history_on_reconnect()` メソッドを新規作成**（仕様書セクション3.2.1）
   - 再接続時に `send_client_content(turns)` で会話履歴（直近10ターン、各150文字）を再送

3. **`_get_context_summary()` を簡素化**（仕様書セクション3.2.2）
   - 現在: 直近10ターンをテキスト結合して返す
   - 変更後: 直前のAIの質問のみ補足として返す（主力は履歴再送に移行）

4. **`run()` の再接続フローを修正**（仕様書セクション3.2.3）
   - 再接続時に `_send_history_on_reconnect(session)` を呼び出してから、トリガーメッセージを送信する2段階方式に変更

5. **`run()` に `_shop_search_pending` チェックを追加**（仕様書セクション3.2.3）
   - `_session_loop()` 終了後に `_shop_search_pending` を確認し、セットされていれば `_handle_shop_search()` を呼ぶ

6. **`live_fallback` イベントの emit を削除**（仕様書セクション8）
   - エラー時に `live_fallback` を emit する箇所を削除し、ログ出力のみにする

**フロントエンド: `src/scripts/chat/core-controller.ts`**

7. **`live_fallback` ハンドラから `switchToRestApiMode()` 呼び出しを削除**（仕様書セクション8）
   - ログ出力のみに変更

8. **`toggleRecording()` 内の `switchToRestApiMode()` を `terminateLiveSession()` に置換**（仕様書セクション8）
   - マイクボタンによるLiveAPI停止はフォールバックではなくユーザー操作

9. **`switchToRestApiMode()` メソッド自体を削除**（仕様書セクション8）

## やってはいけないこと

- 仕様書に記載がない変更をしない
- 推測でAPIの引数やメソッド名を変えない
- 過去のv3コミット（148a31e, 5e4e3e5等）のコードをコピーしない。仕様書を読んでゼロから実装する
- 困ったら推測で進めずユーザーに聞く

## 参照すべき文書

1. `docs/02_liveapi_migration_design_v3.md` — v3.2仕様書（メイン）
2. `docs/01_stt_stream_detailed_spec.md` — STTストリーミング詳細仕様
3. `docs/03_prompt_modification_spec.md` — プロンプト修正仕様

## 過去の経緯（参考）

v3仕様書に基づく実装を複数回試みたが、全て失敗してリバートされた。
仕様書をv3 → v3修版 → v3.1 → v3.2と修正し、現在のv3.2が最新。
コードは都度リバートされ、現在は `6e32fed`（v3修正以前）の状態に戻っている。
