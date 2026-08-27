#!/bin/bash
# 构建 ide 插件的 CodeMirror vendor 包。
# 前置：本目录下已 npm install（依赖见 entry.js 的 import）。
# esbuild 经 npx 从本目录 node_modules 解析，不依赖外部路径。
cd "$(dirname "$0")"
exec npx esbuild entry.js \
  --bundle \
  --outfile=../web/vendor/codemirror.js \
  --format=iife \
  --global-name=CM \
  --minify
