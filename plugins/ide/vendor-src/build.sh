#!/bin/bash
# 构建 ide 插件的 CodeMirror vendor 包。
# 前置：本目录下已 npm install（依赖见 entry.js 的 import）。
# esbuild 经 npx 从本目录 node_modules 解析，不依赖外部路径。
#
# [2026-09-13] alias 强制 @codemirror/state 与 @codemirror/view 统一解析到
# 顶层 6.x 副本。原因：@codemirror/rectangular-selection@0.19.2（该包未随
# 6.x 重新发版，是唯一可用版本）把 0.19 时代的 state/view 声明为常规依赖，
# npm 会在其目录下嵌套安装旧副本；默认打包会让 bundle 出现两份模块实例，
# 旧副本产生的扩展对象带有旧实例的内部符号，6.x 的 EditorState 无法识别，
# 浏览器端报 "Unrecognized extension value in extension set"，编辑器无法
# 打开。0.19 到 6.0 的公开 API 兼容（官方该次大版本主要是重新编号），
# 强制同源解析即可。@codemirror/text 只被引用 findColumn/countColumn 两个
# 纯函数，不涉及共享符号，保持默认解析。
cd "$(dirname "$0")"
exec npx esbuild entry.js \
  --bundle \
  --outfile=../web/vendor/codemirror.js \
  --format=iife \
  --global-name=CM \
  --minify \
  --alias:@codemirror/state="$PWD/node_modules/@codemirror/state/dist/index.js" \
  --alias:@codemirror/view="$PWD/node_modules/@codemirror/view/dist/index.js"
