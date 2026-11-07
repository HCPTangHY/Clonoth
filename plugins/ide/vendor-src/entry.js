// ide 插件 CodeMirror vendor 打包入口。
// 构建：cd plugins/ide/vendor-src && npm install && bash build.sh
// 产物 plugins/ide/web/vendor/codemirror.js（IIFE，全局名 CM）。
// 面板页（plugins/ide/web/index.html）只依赖此全局对象。
import { EditorState, RangeSet } from '@codemirror/state';
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  drawSelection, Decoration, ViewPlugin, crosshairCursor,
} from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
// [2026-09-13] 矩形选择：Alt+拖动框选列块。包停在 0.19.2（未随 6.x 重发，
// API 兼容，peer 声明旧需 --legacy-peer-deps 安装）。
// crosshairCursor 来自 commands：Alt 按下时光标变十字，提示框选模式。
import { rectangularSelection } from '@codemirror/rectangular-selection';
import {
  HighlightStyle, defaultHighlightStyle, StreamLanguage, indentUnit,
  LanguageDescription, syntaxHighlighting, bracketMatching, foldGutter,
} from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { javascript } from '@codemirror/lang-javascript';
import { json } from '@codemirror/lang-json';
import { python } from '@codemirror/lang-python';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { xml } from '@codemirror/lang-xml';
import { yaml } from '@codemirror/lang-yaml';
import { rust } from '@codemirror/lang-rust';
import { cpp } from '@codemirror/lang-cpp';
import { java } from '@codemirror/lang-java';
import { go } from '@codemirror/lang-go';
import { sql } from '@codemirror/lang-sql';
import { php } from '@codemirror/lang-php';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { shell } from '@codemirror/legacy-modes/mode/shell';
import { toml } from '@codemirror/legacy-modes/mode/toml';
import { dockerFile } from '@codemirror/legacy-modes/mode/dockerfile';
import { lua } from '@codemirror/legacy-modes/mode/lua';
import { ruby } from '@codemirror/legacy-modes/mode/ruby';

export {
  EditorState, RangeSet, Decoration, ViewPlugin,
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  drawSelection, bracketMatching, foldGutter, highlightSelectionMatches,
  defaultKeymap, history, historyKeymap, indentWithTab, searchKeymap,
  rectangularSelection, crosshairCursor,
  HighlightStyle, defaultHighlightStyle, StreamLanguage, indentUnit,
  LanguageDescription, syntaxHighlighting, tags,
  javascript, json, python, html, css, xml, yaml, rust, cpp, java, go, sql, php,
  markdown, markdownLanguage,
  shell, toml, dockerFile, lua, ruby,
};
