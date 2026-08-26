#!/bin/bash
# 构建 ide 插件的 CodeMirror vendor 包。
# 前置：本目录下已 npm install（依赖见 entry.js 的 import）。
# esbuild 二进制复用 zoaholic 前端的安装，避免本插件引入构建链依赖。
cd "$(dirname "$0")"
exec /www/wwwroot/zoaholic_original/frontend/node_modules/esbuild/bin/esbuild entry.js \
  --bundle \
  --outfile=../web/vendor/codemirror.js \
  --format=iife \
  --global-name=CM \
  --minify
