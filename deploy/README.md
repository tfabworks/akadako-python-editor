# 本番配信（デプロイ）ガイド

このアプリは **100% 静的ファイル**（HTML/JS/WASM/Python テキスト）です。サーバー側で
Python は動きません（生徒のコードはブラウザの Pyodide 内で実行されます）。配信側に必要なのは
「静的ファイルを正しいヘッダで配ること」だけです。

## 絶対条件

1. **HTTPS**（Web MIDI は secure context 必須）
2. すべてのレスポンスに次の2ヘッダ（SharedArrayBuffer = crossOriginIsolated の有効化に必須）
   ```
   Cross-Origin-Opener-Policy: same-origin
   Cross-Origin-Embedder-Policy: require-corp
   ```
3. **全アセットを同一オリジンに**（`require-corp` 下では他オリジンの読み込み不可）。
   `./fetch-vendor.sh` を実行して `web/vendor/` に Pyodide / CodeMirror を取り込んでから配信する。
4. `.wasm` を `application/wasm` で返す
5. iPad/Scrub 対応なら、Scrub の許可ドメイン（`*.akadako.com` / `*.tfabworks.com` /
   `*.399.jp` / `*.699.jp`）配下に置く

> GitHub Pages は**単体では使えません**（カスタムヘッダ COOP/COEP を出せないため）。
> ただし前段に CloudFront を置いてヘッダを付与すれば利用可（後述）。

公開する内容はリポジトリのルート一式（`web/`, `akadako/`, `index` への入口は `/web/`）。

## 配信先ごとの設定

### Netlify / Cloudflare Pages
ルートの **`_headers`**（本リポジトリに同梱済み）がそのまま効きます。publish ディレクトリ=リポジトリルート。

### Amazon S3 + CloudFront
- S3 に一式をアップロード（静的ウェブサイトホスティング）
- CloudFront に **Response headers policy** を作成し、ビヘイビアに割り当て：
  - `Cross-Origin-Opener-Policy: same-origin`
  - `Cross-Origin-Embedder-Policy: require-corp`
- `.wasm` の Content-Type が `application/wasm` になるよう S3 のメタデータを設定
- `web/vendor/*` は長期キャッシュ（`Cache-Control: public, max-age=31536000, immutable`）

AWS CLI 例（Response Headers Policy）は `deploy/cloudfront-response-headers-policy.json` を参照。

### GitHub Pages をオリジンにした CloudFront（無料ホスト + ヘッダ付与）
GitHub Pages 単体は COOP/COEP を出せず使えませんが、**前段に CloudFront を置いて
CloudFront 側でヘッダを付与**すれば使えます。ブラウザから見ると全て CloudFront の
同一オリジンになるので `require-corp` を満たします。

```
ブラウザ ──► CloudFront（COOP/COEP 付与・キャッシュ）──► GitHub Pages（静的ファイル）
```

手順：
1. リポジトリを GitHub Pages で公開（`./fetch-vendor.sh` 実行後の一式。`web/vendor/` 込み）
2. CloudFront のオリジンを **カスタムオリジン**で設定
   - Origin domain: `<ユーザー名>.github.io`
   - Protocol: **HTTPS only**
   - Origin path: プロジェクトページなら **`/<リポジトリ名>`**（例 `/akadako-py-web`）
     → CloudFront の `/web/...` `/akadako/...` が GitHub Pages 側の正しいパスに対応する
     （アプリは絶対パス `/web/` 等を使うため必須）
3. **Response Headers Policy**（`deploy/cloudfront-response-headers-policy.json`）を
   ビヘイビアに割り当て（COOP `same-origin` + COEP `require-corp`）
4. 利用者には **CloudFront のURL（or 独自ドメイン）** を案内する

注意：
- `<ユーザー名>.github.io/...` を**直接開くとヘッダが無く動かない**。必ず CloudFront 経由のURLを使う。
- `.wasm` は GitHub Pages が `application/wasm` で返すので基本OK（CloudFront は素通し）。
  万一ダメなら CloudFront Function で Content-Type を上書きする。
- GitHub Pages の制限（ファイル100MB / サイト約1GB / 帯域ゆるい上限）。Pyodide 約13MB は問題なし。
  CloudFront キャッシュによりオリジンへのアクセスは激減する。

### Nginx（自前サーバー）
`deploy/nginx.conf.example` を参照。

## キャッシュ / オフライン（推奨）
Pyodide は約 13MB。初回だけ読めば以降はブラウザ/エッジキャッシュで高速です。
GIGA の共有 Wi-Fi 対策として Service Worker（PWA）でのプリキャッシュも検討してください
（残作業 B-3）。
