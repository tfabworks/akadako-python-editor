# AkaDako Python in the browser — PoC (Pyodide + Web Worker + Web MIDI)

ブラウザだけで Python を書き・実行し、**実機の AkaDako** を制御できるかを実証する最小プロトタイプです。
[tfabworks/akadako-python](https://github.com/tfabworks/akadako-python) を **無改造のまま** 使い、
ブラウザ向けの差分だけを薄いブリッジ層に閉じ込めています。

## 使い方

```bash
./fetch-vendor.sh         # 初回のみ: Pyodide / CodeMirror を web/vendor/ に取得
python3 serve.py          # http://localhost:8770/web/
```

すべてのアセットは同一オリジンから配信されます（`require-corp` 対応）。本番配信は
[`deploy/README.md`](deploy/README.md) を参照。

Chrome か Edge で開く（Web MIDI と SharedArrayBuffer が必要）→ AkaDako を USB 接続 →
**Connect board** → コードを編集して **Run ▶**（Ctrl/⌘+Enter）。**Stop ◼** で実行中断（KeyboardInterrupt）。

### エディタ / デバッグ機能

- **CodeMirror エディタ**: Python シンタックスハイライト・行番号・括弧対応・自動インデント。
- **AkaDako API 補完**: `board.` / `AkaDako.` と入力すると、センサー読み取りや出力メソッドの候補が
  シグネチャ + 日本語説明つきで出る（`Ctrl+Space` でも起動）。一覧は `web/akadako-api.js`。
- **ライブ・センサーモニタ**（右パネル）: 実行中、`fetch_* / analog_* / digital_* / motion_sensor`
  の読み取り値が自動で表示され、スパークラインで推移も見える。表示名は日本語ラベル化
  （`web/akadako-api.js` の `AKADAKO_LABELS`、メソッド名はツールチップで確認可）。
  `monitor("名前", 値)` で任意の値もウォッチに追加できる（値はそのまま返るので式に挟める）。
- **ボード別スターター自動生成**: 「ボードに接続」すると `pybridge.probe_sensors()` が各センサーを
  1回ずつ試し、応答したものだけを使った読みやすいプログラムをエディタに自動生成する
  （エディタ未編集のときのみ。編集済みなら上書きしない）。

## 仕組み

```
┌─ Main thread ─────────────┐        ┌─ Web Worker ───────────────────────┐
│ エディタ / コンソール UI    │        │ Pyodide                            │
│ Web MIDI (port を所有)     │        │  └ akadako (無改造)                 │
│                           │        │      └ pybridge が3点だけ差し替え   │
│  device→  ring SAB  ──────┼──────► │  midiPump(): Atomics.wait で待機     │
│  ◄──── midi-out (postMsg) ┼────────┤  midiSend(): 送信を main へ          │
│  Stop → irq SAB (SIGINT)  ┼──────► │  setInterruptBuffer                  │
└───────────────────────────┘        └─────────────────────────────────────┘
```

ブラウザは単一スレッド＋イベントループのため、元ライブラリの
「別スレッドで届く MIDI を `threading.Event.wait()` でブロッキング待ちする」設計がそのままでは
デッドロックします。`web/pybridge.py` がこれを**3点だけ**差し替えて解消します（ライブラリ本体は無改造）:

1. **`WebMidiTransport`** — `MidiTransport` を置換。送信バイトは main へ post、受信は SharedArrayBuffer
   リングから取得。Firmata 変換（`0xF4→0xA0` など）は本家と同一。
2. **pumping `time.sleep`** — sleep 中も受信リングを drain して Firmata に流す。
   これで「読む→sleep→繰り返す」型でもストリーミングの analog/digital キャッシュが更新される。
3. **pumping `Event`** — `Event.wait()` が同じ drain ループを回し、I2C 読み取りや
   version/UID/水温など request/reply 系の待ちを解決する。

drain は JS の `Atomics.wait` で効率的にブロックしつつ、最大 ~100ms ごとに Python へ戻るので
Stop ボタン（interrupt buffer）が効きます。

## 検証状況

- ✅ `import akadako`（`python-rtmidi` 不要 — 遅延 import を回避）
- ✅ ヘッドレス検証（`harness.py` 相当）で、`connect()` 完走 / slink版バージョン応答の同期解決 /
  ストリーミング値のキャッシュ反映 / `disconnect()` を確認
- ⬜ **実機 + 実ブラウザでの End-to-End は未検証**（Web MIDI のポート列挙、SAB+Atomics の
  クロスコンテキスト動作、Pyodide ロードは標準機構だが要実機確認）

## 既知の制約 / 次のステップ

- **対応ブラウザ**: Web MIDI のある Chrome/Edge（PC/Chromebook）。iPad は Scrub 経由（WKWebView+WebMIDIKit）。
- **SysEx 権限必須**: `requestMIDIAccess({sysex:true})`（バージョン/UID/NeoPixel 等で使用）。
- **クロスオリジン分離必須**: COOP `same-origin` + COEP `require-corp`（`serve.py` / `_headers` /
  `deploy/` が付与）。これが無いと SharedArrayBuffer が使えない。
- **アセットは全て自前ホスト**: `./fetch-vendor.sh` で Pyodide / CodeMirror を `web/vendor/` に取得。
  `require-corp` 下では他オリジンを読めないため、CDN ではなく同一オリジン配信が必須。CM は 5 系
  （単一ファイル・無ビルド）。CM6 へ上げる場合はバンドル工程が前提になる。
- **デバッグ強化（実装済み）**: AkaDako API 補完 + ライブ・センサーモニタ（自動ウォッチ + `monitor()`）。
  未実装: ステップ実行/ブレークポイント（`sys.settrace` ベースで別途必要）、実行時エラーの該当行マーカー。
- **リング溢れ**: 受信リング 64KB。極端に高頻度な受信が続くと取りこぼし得る（PoC では許容）。
- **本家ライブラリへの修正1か所**: `akadako/firmata.py` の `I2C_READ_ONCE` を `0x18`(STOP_READING) →
  `0x08`(READ once) に修正。元の値だと全 I2C 読み取りがタイムアウトし、BME280 等が
  `chip id 0x00` で `NotSupportedError` になる（実機 STEAM BOX で確認）。要・upstream 報告。
- **`akadako` の同梱**: `akadako/` をそのまま配信し Worker が Pyodide FS へ展開。アップストリーム更新時は
  ディレクトリを差し替えるだけ。

## ファイル構成

| パス | 役割 |
|---|---|
| `serve.py` | COOP/COEP 付き開発サーバー |
| `web/index.html` | エディタ + コンソール UI |
| `web/main.js` | メインスレッド: Web MIDI、SAB、Worker 管理 |
| `web/worker.js` | Worker: Pyodide 起動、リング drain、interrupt |
| `web/pybridge.py` | ブリッジ（3点差し替え + ライブモニタ計測） |
| `web/akadako-api.js` | 補完 + リファレンス用の AkaDako API カタログ |
| `web/vendor/` | 自前ホストの Pyodide / CodeMirror（`fetch-vendor.sh` で取得） |
| `akadako/` | 同梱した本家ライブラリ（I2C読み取りモードの修正1か所のみ・後述） |
| `fetch-vendor.sh` | Pyodide / CodeMirror を `web/vendor/` に取得するスクリプト |
| `_headers` | Netlify / Cloudflare Pages 用 COOP/COEP ヘッダ |
| `deploy/` | 本番配信ガイド（Nginx / CloudFront 設定例つき） |
