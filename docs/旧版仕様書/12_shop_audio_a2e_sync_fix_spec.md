# ショップ読み上げ A2E同期修正仕様書 — 修正案C（A2E先行＋sleep）

**作成日**: 2026-03-19
**目的**: ショップ説明読み上げ時のリップシンク同期崩壊を修正する
**方針**: 公式デモと同等の「A2Eデータが先に到着 → 音声再生開始」パターン
**状態**: `docs/13_a2e_lipsync_comprehensive_guide.md` に統合済み（旧版）

---

（内容は `docs/13_a2e_lipsync_comprehensive_guide.md` §5-§8 に統合。詳細はそちらを参照。）

---

## 修正案Cの要約

1. **A2E先行パターン**: 音声送信前にExpressionデータを先にフロントエンドに届ける
2. **`_send_a2e_ahead()`**: 新規メソッド。全PCMを一括でA2Eに送信し、Expressionを先に受信
3. **`resetForNewSegment()`**: フロントエンド新規メソッド。`isAiSpeaking=true`を維持しつつバッファリセット
4. **`live_expression_reset`**: セグメント境界信号。`resetForNewSegment()`をトリガー
5. **sleep**: Expression到着の時間マージン（50ms cached / 30ms streaming）

## 修正ファイル一覧

| ファイル | 修正内容 |
|---------|---------|
| `support-base/live_api_handler.py` | `_send_a2e_ahead()` 新規追加 |
| `support-base/live_api_handler.py` | `_emit_cached_audio()` — A2E先行+sleep方式に変更 |
| `support-base/live_api_handler.py` | `_emit_collected_shop()` — A2E先行+sleep方式に変更 |
| `support-base/live_api_handler.py` | `_stream_single_shop()` — reset後にsleep追加 |
| `src/scripts/chat/live-audio-manager.ts` | `resetForNewSegment()` 新規追加 |
| `src/scripts/chat/core-controller.ts` | `live_expression_reset` ハンドラ修正 |
