#!/usr/bin/env bash
# Download all third-party assets into web/vendor/ so the app can be served
# fully same-origin (required for COEP: require-corp / SharedArrayBuffer, and
# so school proxies that block CDNs don't break it). Run once before deploy:
#
#   ./fetch-vendor.sh
#
set -euo pipefail
cd "$(dirname "$0")"

PYODIDE_VER="v0.26.2"
CM_VER="5.65.16"
PY_BASE="https://cdn.jsdelivr.net/pyodide/${PYODIDE_VER}/full"
CM_BASE="https://cdnjs.cloudflare.com/ajax/libs/codemirror/${CM_VER}"

mkdir -p web/vendor/pyodide
mkdir -p web/vendor/codemirror/mode/python
mkdir -p web/vendor/codemirror/addon/hint
mkdir -p web/vendor/codemirror/addon/edit
mkdir -p web/vendor/codemirror/theme

echo "== Pyodide (${PYODIDE_VER}) =="
for f in pyodide.js pyodide.asm.js pyodide.asm.wasm python_stdlib.zip pyodide-lock.json; do
  echo "  $f"
  curl -fsSL "${PY_BASE}/${f}" -o "web/vendor/pyodide/${f}"
done

echo "== CodeMirror (${CM_VER}) =="
declare -a CM_FILES=(
  "codemirror.min.css"
  "codemirror.min.js"
  "mode/python/python.min.js"
  "addon/hint/show-hint.min.css"
  "addon/hint/show-hint.min.js"
  "addon/edit/matchbrackets.min.js"
  "addon/edit/closebrackets.min.js"
  "theme/material-darker.min.css"
)
for f in "${CM_FILES[@]}"; do
  echo "  $f"
  curl -fsSL "${CM_BASE}/${f}" -o "web/vendor/codemirror/${f}"
done

echo "== lz-string (1.5.0) + qrcode-generator (共有URL機能) =="
mkdir -p web/vendor/lz-string web/vendor/qrcode
curl -fsSL "https://raw.githubusercontent.com/pieroxy/lz-string/1.5.0/libs/lz-string.min.js" \
  -o web/vendor/lz-string/lz-string.min.js
curl -fsSL "https://raw.githubusercontent.com/kazuhikoarase/qrcode-generator/master/js/dist/qrcode.js" \
  -o web/vendor/qrcode/qrcode.js

echo "done. vendored into web/vendor/"
du -sh web/vendor/pyodide web/vendor/codemirror web/vendor/lz-string web/vendor/qrcode 2>/dev/null || true
