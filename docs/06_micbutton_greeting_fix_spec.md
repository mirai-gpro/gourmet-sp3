# マイクボタン・初期挨拶バグ修正仕様書（gourmet-sp3）

> **作成日**: 2026-03-13
> **前提文書**: `docs/05_liveapi_migration_design_v5.md`
> **ベースコミット**: `4d6cac8`（全修正リバート後、`6bba0d5` 相当の状態）
> **助言元**: ChatGPT / Gemini（Gemini LiveAPI仕様に関する知識提供）

---

## 1. 本仕様書の位置づけ

### 1.1 背景

`6bba0d5` の状態で以下2つのバグが報告された。

| # | バグ | 症状 |
|---|------|------|
| 1 | マイクボタン1回目不発 | 初期挨拶後、マイクボタンを押しても録音が始まらない |
| 2 | 挨拶リピート | マイクボタン押下後、初期挨拶が再度再生される |

これに対し複数回の修正を試みたが、修正のたびに症状が悪化しリバートを繰り返した（コミット `a883257` → `502e428` → `13e80bd` → `d98a6e5` → `59b1da1` → `facf3ef` → `0a6dca2` → `4d6cac8`）。

### 1.2 失敗の根本原因

1. **Claudeが Gemini LiveAPI の仕様知識を持っていない**（2025年12月末リリースのプレビュー版）
2. 知識不足にもかかわらず、Claudeの推論に基づいた修正を行った
3. 特に `initialize()` の分割（`initPlayback` / `initMicrophone`）が致命的だった
   - `getUserMedia()` の許可ダイアログ = ユーザージェスチャー = AudioContextロック解除、という関係を見落とした
   - `getUserMedia()` を `initializeSession()` フローから外した結果、AudioContextが suspended のまま → 挨拶音声が再生不能に

### 1.3 本仕様書の方針

- **Claudeの推論を排除し、仕様に基づく変更のみ行う**
- 各変更の根拠を `[仕様]` または `[ロジック]` で明示する
  - `[仕様]` = ChatGPT / Gemini からの LiveAPI 仕様情報に基づく変更
  - `[ロジック]` = コード構造上の明白な修正（API仕様の推論不要）
- 段階的にデプロイし、1段階ごとに検証する

---

## 2. バグの根本原因分析

### 2.1 現在のマイクボタン動作フロー（バグ状態）

```
ページロード
  → initializeSession() → startLiveMode()
  → initialize(socket): getUserMedia + AudioContext + AudioWorklet
  → emit('live_start') → サーバー: LiveAPISession作成(session_count=1)
  → live_ready → startStreaming() → isStreaming=true
  → send_client_content（挨拶テキスト） → 挨拶音声再生
  → isLiveMode=true, isRecording=false（※ initializeSession経由のため）

マイクボタン1回目クリック:
  → toggleRecording()
  → isLiveMode === true → terminateLiveSession() → return  ← ★ ここで録音開始せず終了
  → emit('live_stop') → サーバー: セッション破壊
  → isLiveMode=false

マイクボタン2回目クリック:
  → toggleRecording()
  → isLiveMode === false → startLiveMode()
  → emit('live_start') → サーバー: 新LiveAPISession(session_count=1)
  → send_client_content → ★ 挨拶が再発火
```

### 2.2 根本原因（1文で要約）

**`toggleRecording()` が LiveAPI セッションの破壊/再作成をトグルしているため、マイク1回目は「破壊のみ」で終了し、2回目に新セッションが作られ挨拶が再発火する。**

### 2.3 あるべき動作

```
マイクボタンクリック:
  → isLiveMode=true の場合、セッションは維持したまま isStreaming のトグルのみ
  → live_start / live_stop は送信しない
  → 挨拶は初回ページロード時の1回のみ
```

---

## 3. 変更仕様

### 3.1 変更一覧

| # | ファイル | 行 | 根拠 | 概要 |
|---|---------|-----|------|------|
| 1 | `core-controller.ts` | L423-434 | `[ロジック]` | toggleRecording: セッション破壊 → ストリーミングトグル |
| 2 | `core-controller.ts` | L272-275 | `[仕様]` | live_ready: startStreaming即実行 → greeting_done待ち |
| 3 | `core-controller.ts` | 新規 | `[仕様]` | greeting_doneハンドラ追加 |
| 4 | `live_api_handler.py` | L551-552 | `[仕様]` | 挨拶ターン完了時にgreeting_done emit |
| 5 | `live_api_handler.py` | L476 | `[仕様]` | mime_type修正: `audio/pcm` → `audio/pcm;rate=16000` |
| 6 | `app_customer_support.py` | L744 | `[ロジック]` | greeted_client_sids ガード追加 |
| 7 | `core-controller.ts` | L155 | `[ロジック]` | resetAppContent: terminateLiveSession重複呼び出し削除 |

### 3.2 変更しないもの（厳守）

| 項目 | 理由 |
|------|------|
| `initializeSession()` 内の `startLiveMode()` 呼び出し (L416) | AudioContext解除の仕組みを維持するため |
| `LiveAudioManager.initialize()` の構造 (L62-148) | getUserMedia含む一体初期化を維持するため |
| `initialize()` の分割（initPlayback / initMicrophone） | **絶対禁止**: 前回の失敗の核心 |

---

## 4. 変更詳細

### 4.1 変更1: `toggleRecording()` のLiveAPI分岐修正 `[ロジック]`

**ファイル**: `src/scripts/chat/core-controller.ts`
**行**: L423-434

**現在のコード**:
```typescript
// ★ LiveAPIモード中 → 停止（v5仕様書: RESTフォールバックなし）
if (this.isLiveMode) {
  this.terminateLiveSession();
  this.isRecording = false;
  this.els.micBtn.classList.remove('recording');
  this.resetInputState();
  return;
}
```

**変更後のコード**:
```typescript
// ★ LiveAPIモード中 → ストリーミングのトグル（セッションは維持）
if (this.isLiveMode) {
  if (this.isRecording) {
    // マイクOFF: ストリーミング停止
    this.liveAudioManager.stopStreaming();
    this.isRecording = false;
    this.els.micBtn.classList.remove('recording');
  } else {
    // マイクON: ストリーミング再開
    this.liveAudioManager.startStreaming();
    this.isRecording = true;
    this.els.micBtn.classList.add('recording');
  }
  return;
}
```

**変更理由**:
- セッション（`live_start` / `live_stop`）を触らず、`isStreaming` のトグルのみで制御
- セッションを破壊しないため、再作成が起きず、挨拶は再発火しない
- 1回目クリックで即座にマイクOFF（ストリーミング停止）が機能する

### 4.2 変更2: `live_ready` ハンドラの変更 `[仕様]`

**ファイル**: `src/scripts/chat/core-controller.ts`
**行**: L272-275

**現在のコード**:
```typescript
this.socket.on('live_ready', () => {
  console.log('[LiveAPI] live_ready受信');
  this.liveAudioManager.startStreaming();
});
```

**変更後のコード**:
```typescript
this.socket.on('live_ready', () => {
  console.log('[LiveAPI] live_ready受信 → greeting_done待機');
  // ★ startStreaming()は呼ばない。greeting_done を待つ。
  //   理由: send_client_content（挨拶）と send_realtime_input（マイク音声）の
  //         混在はLiveAPI SDK非推奨（ChatGPT/Gemini助言）
});
```

**変更理由**:
- Gemini LiveAPI では `send_client_content`（テキスト送信）と `send_realtime_input`（リアルタイム音声送信）の混在が非推奨
- 初期挨拶は `send_client_content` で発火される
- 挨拶ターンが完了する前にマイク音声を送ると、予期しない動作の原因になる

### 4.3 変更3: `greeting_done` ハンドラ追加 `[仕様]`

**ファイル**: `src/scripts/chat/core-controller.ts`
**挿入位置**: `live_ready` ハンドラの直後（L275の後）

**追加コード**:
```typescript
this.socket.on('greeting_done', () => {
  console.log('[LiveAPI] greeting_done受信 → マイクOFF状態で待機（ユーザータップ待ち）');
  // ★ startStreaming()は呼ばない。ユーザーがマイクボタンを押すまでOFF状態。
  //   理由: iOSではユーザージェスチャーなしのマイク有効化がセキュリティ制約に抵触する。
  //   マイクON/OFFはtoggleRecording()で制御する。
});
```

**変更理由**:
- 挨拶ターン完了を受信し、LiveAPIセッションが挨拶フェーズを完了したことをフロント側で認知する
- **ストリーミングは開始しない**: iOSではユーザージェスチャーなしのマイク有効化がセキュリティ制約に抵触するため
- ユーザーがマイクボタンをタップして初めて `toggleRecording()` → `startStreaming()` が実行される
- `isRecording = false` のまま待機し、マイクボタンは非録音状態で表示される

### 4.4 変更4: 挨拶ターン完了時に `greeting_done` emit `[仕様]`

**ファイル**: `support-base/live_api_handler.py`
**行**: L550-552

**現在のコード**:
```python
# 初期あいさつフェーズ終了（仕様書02 セクション4.5.5）
if self._is_initial_greeting_phase:
    self._is_initial_greeting_phase = False
```

**変更後のコード**:
```python
# 初期あいさつフェーズ終了（仕様書02 セクション4.5.5）
if self._is_initial_greeting_phase:
    self._is_initial_greeting_phase = False
    self.socketio.emit('greeting_done', {},
                       room=self.client_sid)
    logger.info("[LiveAPI] greeting_done送信")
```

**変更理由**:
- 挨拶の `turn_complete` を検知し、フロントエンドにストリーミング開始のタイミングを通知する
- `_is_initial_greeting_phase` フラグは既に存在するため、追加ロジックは最小限

### 4.5 変更5: `send_realtime_input` の mime_type 修正 `[仕様]`

**ファイル**: `support-base/live_api_handler.py`
**行**: L475-476

**現在のコード**:
```python
await session.send_realtime_input(
    audio={"data": audio_data, "mime_type": "audio/pcm"}
)
```

**変更後のコード**:
```python
await session.send_realtime_input(
    audio={"data": audio_data, "mime_type": "audio/pcm;rate=16000"}
)
```

**変更理由**:
- Gemini LiveAPI仕様: PCM音声のサンプルレートを明示する必要がある
- フロントエンドの AudioWorklet は 48kHz → 16kHz にダウンサンプリングしている（`live-audio-manager.ts` L84）
- `rate=16000` が正しい値

### 4.6 変更6: `greeted_client_sids` ガード追加 `[ロジック]`

**ファイル**: `support-base/app_customer_support.py`
**行**: L744（`active_live_sessions` 定義の直後）

**追加コード（定義）**:
```python
active_live_sessions = {}  # {client_sid: LiveAPISession}
greeted_client_sids = set()  # 挨拶済みのclient_sid
```

**`handle_live_start` 内の変更（L803付近）**:
```python
# LiveAPIセッション作成
live_session = LiveAPISession(
    session_id=session_id,
    mode=mode,
    language=language,
    system_prompt=system_prompt,
    socketio=socketio,
    client_sid=client_sid,
    shop_search_callback=shop_search_callback
)

# ★ 挨拶ガード: 同一client_sidで既に挨拶済みなら session_count を1に設定
#    → run() 内で session_count > 1 の分岐に入り、挨拶をスキップ
if client_sid in greeted_client_sids:
    live_session.session_count = 1  # 初回扱いにしない
else:
    greeted_client_sids.add(client_sid)

active_live_sessions[client_sid] = live_session
```

**`handle_live_stop` / `disconnect` でのクリーンアップ**:
- `greeted_client_sids` からは**削除しない**（同一接続内では挨拶は1回のみ）
- `disconnect` 時のみ削除する（新しい接続 = 新しい client_sid なので自然にクリーンアップされる）

**変更理由**:
- 変更1（toggleRecording修正）によりセッション再作成は通常起きないが、万が一の防御として
- ソフトリロード時: `resetAppContent()` → `stopAllActivities()` → `terminateLiveSession()` → `live_stop` → `initializeSession()` → `live_start` の流れで再作成が起きうる
- `session_count = 1` をセットすることで、`run()` 内の `if self.session_count == 1:` 分岐をスキップし、挨拶を防止

### 4.7 変更7: `resetAppContent()` の `terminateLiveSession()` 重複削除 `[ロジック]`

**ファイル**: `src/scripts/chat/core-controller.ts`
**行**: L155

**現在のコード**:
```typescript
// ★ LiveAPIセッションをリセット
this.terminateLiveSession();
```

**変更後**: 該当行を削除

**変更理由**:
- L120 の `stopAllActivities()` 内部（L1129）で既に `terminateLiveSession()` が呼ばれている
- 2回呼ばれると `live_stop` が2回 emit される可能性がある（1回目で `isLiveMode=false` になるため実害は小さいが、不要なコード）

---

## 5. 修正後のフロー全体図

### 5.1 ページロード → 初期挨拶

```
Browser                          Server                         Gemini LiveAPI
  |                                |                                |
  |-- initializeSession() -------->|                                |
  |   REST: /api/session/start     |                                |
  |<-- session_id -----------------|                                |
  |                                |                                |
  |-- startLiveMode() ------------>|                                |
  |   initialize(socket):         |                                |
  |     getUserMedia() → 許可ダイアログ                              |
  |     (= ユーザージェスチャー → AudioContext解除)                    |
  |   emit('live_start')           |                                |
  |                                |-- LiveAPISession作成 --------->|
  |                                |   send_client_content(挨拶)    |
  |<-- live_ready ------------------|                                |
  |   (何もしない = 待機)           |                                |
  |                                |<--- 挨拶音声 + transcript ------|
  |<-- live_audio -----------------|                                |
  |   playPcmAudio() → 音声再生    |                                |
  |<-- ai_transcript --------------|                                |
  |   チャット欄にテキスト表示       |                                |
  |                                |<--- turn_complete -------------|
  |<-- turn_complete --------------|                                |
  |<-- greeting_done --------------|  ★新規                         |
  |   (何もしない = マイクOFF待機)   |                                |
  |   isRecording = false          |                                |
  |   マイクボタン: 通常表示（ユーザータップ待ち）                       |
```

### 5.2 マイクボタン操作

```
■ マイクOFF（isRecording=true → false）
Browser                          Server                         Gemini LiveAPI
  |                                |                                |
  |-- toggleRecording() ---------->|                                |
  |   stopStreaming()              |                                |
  |   isStreaming = false          |                                |
  |   isRecording = false          |                                |
  |   マイクボタン: 通常表示         |                                |
  |                                |                                |
  |   ※ live_stop は送信しない      |                                |
  |   ※ セッションは維持            |                                |

■ マイクON（isRecording=false → true）
Browser                          Server                         Gemini LiveAPI
  |                                |                                |
  |-- toggleRecording() ---------->|                                |
  |   startStreaming()             |                                |
  |   isStreaming = true           |                                |
  |   isRecording = true           |                                |
  |   マイクボタン: 録音表示         |                                |
  |                                |                                |
  |   ※ live_start は送信しない     |                                |
  |   ※ セッションは維持            |                                |
```

### 5.3 ソフトリロード

```
Browser                          Server                         Gemini LiveAPI
  |                                |                                |
  |-- resetAppContent() ---------->|                                |
  |   stopAllActivities()         |                                |
  |     terminateLiveSession()    |                                |
  |       emit('live_stop') ----->|-- session.stop() ------------->|
  |       isLiveMode = false      |   del active_live_sessions     |
  |                                |<-- live_stopped ---------------|
  |                                |                                |
  |   (300ms wait)                 |                                |
  |                                |                                |
  |-- initializeSession() -------->|                                |
  |   startLiveMode()             |                                |
  |     initialize(socket):       |                                |
  |       audioContext存在 → skip  |                                |
  |     emit('live_start') ------>|-- LiveAPISession作成 --------->|
  |                                |   ★ client_sid in greeted     |
  |                                |     → session_count = 1       |
  |                                |     → run() で再接続扱い       |
  |                                |     → 挨拶スキップ             |
  |<-- live_ready ------------------|                                |
  |<-- greeting_done --------------|  (即座にemitされる※)           |
  |   (何もしない = マイクOFF待機)   |                                |
```

※ ソフトリロード時の `greeting_done` の扱いについて:
- `session_count > 1` の場合、`_is_initial_greeting_phase = False` で開始される（L416）
- `_is_initial_greeting_phase` が最初から `False` の場合、挨拶ターンの `turn_complete` では `greeting_done` は emit されない
- **対応**: `handle_live_start` で `greeted_client_sids` に含まれる場合、`live_ready` の直後に `greeting_done` も emit する

**`handle_live_start` の追加修正**:
```python
emit('live_ready', {'status': 'connected'})

# ★ 挨拶済みの場合、greeting_doneも即座にemit
if client_sid in greeted_client_sids:
    emit('greeting_done', {})
```

---

## 6. デプロイ・検証手順

### 6.1 段階的デプロイ

| ステップ | 変更 | 検証項目 |
|---------|------|----------|
| Step 1 | 変更1（toggleRecording） + 変更7（重複削除） | マイクボタンのON/OFFトグルが機能するか |
| Step 2 | + 変更2（live_ready待機） + 変更3（greeting_done） + 変更4（greeting_done emit） | 挨拶完了後にマイクが自動有効化されるか |
| Step 3 | + 変更5（mime_type） | 音声認識精度に変化がないか |
| Step 4 | + 変更6（greeted_sidsガード） | ソフトリロード時に挨拶が繰り返されないか |

### 6.2 検証チェックリスト

#### 基本動作

- [ ] ハードリロード → 挨拶音声が再生される
- [ ] 挨拶テキストがチャット欄に表示される
- [ ] 挨拶完了後、マイクボタンが録音状態（recording）になる
- [ ] マイクに話しかけると音声認識される

#### マイクボタン

- [ ] マイクボタン1回目クリック → 即座にOFF（録音停止）になる
- [ ] マイクボタン2回目クリック → 即座にON（録音開始）になる
- [ ] ON/OFF を連続で切り替えても問題ない
- [ ] OFF→ON後に挨拶が繰り返されない

#### ソフトリロード

- [ ] リセットボタン押下 → 画面がクリアされる
- [ ] 新しい挨拶が再生される（新セッションなので正常）
- [ ] 挨拶が2回以上再生されない
- [ ] 音声が重複しない

#### コンシェルジュモード

- [ ] コンシェルジュモードでも上記全てが同様に動作する
- [ ] ユーザープロファイルに基づいた挨拶が行われる

#### エッジケース

- [ ] 挨拶再生中にマイクボタンを押した場合の動作
- [ ] ネットワーク不安定時の動作
- [ ] ブラウザバックグラウンド → フォアグラウンド復帰時

---

## 7. Claudeへの厳守事項（本修正固有）

### 7.1 絶対禁止

1. **`initialize()` を分割しない** — `initPlayback()` / `initMicrophone()` への分割は絶対禁止。前回の失敗の直接原因
2. **`initializeSession()` から `startLiveMode()` を削除しない** — AudioContext解除の仕組みが壊れる
3. **`getUserMedia()` を `initialize()` から移動しない** — 許可ダイアログがユーザージェスチャーとして機能している
4. **推論でLiveAPI仕様を補完しない** — 不明点はユーザーに確認する

### 7.2 修正ルール

1. **1ステップごとにデプロイ・検証** — 複数変更を一括デプロイしない
2. **検証で問題が出たら即リバート** — 追加修正で対処しない
3. **変更根拠を明示** — `[仕様]` か `[ロジック]` か、各変更の根拠をコミットメッセージに記載
4. **本仕様書に記載のない変更は行わない** — 追加変更が必要な場合はユーザーに確認

---

## 8. リスク評価

| 変更 | 根拠 | リスク | 備考 |
|------|------|--------|------|
| 1. toggleRecording修正 | `[ロジック]` | **低** | コード構造上明白 |
| 2. live_ready待機 | `[仕様]` | **低** | SDK仕様に準拠 |
| 3. greeting_doneハンドラ | `[仕様]` | **低** | 新規イベント追加のみ |
| 4. greeting_done emit | `[仕様]` | **低** | 既存フラグ活用 |
| 5. mime_type修正 | `[仕様]` | **低** | 値の修正のみ |
| 6. greeted_sidsガード | `[ロジック]` | **低** | セーフティネット |
| 7. 重複呼び出し削除 | `[ロジック]` | **低** | 不要コード削除 |

### 8.1 残存リスク

- **`audioStreamEnd` 未実装**: LiveAPI仕様ではマイクOFF時に `audioStreamEnd` を送信することが推奨されているが、本修正では見送り。現状の半二重制御（`isAiSpeaking` フラグ）で動作していることを確認の上、必要であれば次のイテレーションで追加
- **ソフトリロード時の `initialize()` スキップ**: `audioContext` が存在する場合に `return` する（L63）ため、socket参照は更新されない。Socket.IO の再接続は同一オブジェクトを再利用するため通常は問題ないが、監視対象とする
