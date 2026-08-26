/* 基础层：boot 凭证、常量、DOM 引用、api 封装、通用工具。 */

/* [AutoC 2026-08-24] IDE 面板：单主页 + 标签栏切换 + 文本编辑保存。
 *
 * 「文件」标签固定在第一、不可关闭，内容是工作区文件树。单击树中文件
 * 打开预览标签（斜体，至多一个，再单击别的文件时复用替换）；双击固定；
 * 编辑预览标签内容会自动将其固定（VSCode 语义）。文本文件直接可编辑，
 * 脏状态在标签上显示圆点，Ctrl+S 或状态栏按钮保存，写回走本插件注册的
 * PUT /v1/plugins/ide/file。 */

const boot = (window.parent && window.parent.__CLONOTH_BOOT__) || {};
const TOKEN = boot.token || '';
const SESSION_ID = boot.sessionId || '';

const IMAGE_EXTS = new Set(['png','jpg','jpeg','gif','webp','svg','ico','bmp','avif']);

// [AutoC 2026-08-24] Tab 缩进宽度可调：状态栏按钮在 2/4/8 间循环，
// 偏好持久化到 localStorage，作用于所有文本标签页（重建编辑器生效）。
const LS_INDENT = 'clonoth_ide_indent';
const INDENT_STEPS = [2, 4, 8];
function readIndent() {
  try {
    const n = Number(localStorage.getItem(LS_INDENT));
    if (INDENT_STEPS.includes(n)) return n;
  } catch (e) {}
  return 2;
}
let indentWidth = readIndent();
const MAX_TEXT_BYTES = 512 * 1024;
const TREE_TAB = '::tree';

const tabbarEl = document.getElementById('tabbar');
const viewEl = document.getElementById('view');
const statusbarEl = document.getElementById('statusbar');

async function api(path, init) {
  init = init || {};
  init.headers = Object.assign(
    {}, TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {}, init.headers || {});
  const resp = await fetch('/v1' + path, init);
  if (!resp.ok) {
    let detail = '';
    try { detail = (await resp.json()).detail || ''; } catch (e) {}
    throw new Error(resp.status + (detail ? ' ' + detail : ''));
  }
  return resp;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtSize(n) {
  if (n == null) return '';
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + 'M';
  if (n > 1024) return (n / 1024).toFixed(1) + 'K';
  return n + 'B';
}

function extOf(path) {
  const name = String(path).split('/').pop() || '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

function baseName(path) {
  // diff 标签的 path 带 ' (diff)' 后缀，显示时剥离
  return String(path).replace(/ \(diff\)$/, '').split('/').pop() || String(path);
}
