/* 编辑与差异渲染：语言映射、共享 CodeMirror 视图层、diff/编辑器/Markdown 渲染、保存。 */

function isMarkdown(path) {
  return /\.(md|markdown)$/i.test(String(path));
}

// [AutoC 2026-08-24] 语法高亮语言映射。CodeMirror 语言按独立包分发，
// 这里只接入常用语言，按扩展名匹配；未命中的文件走无高亮纯文本。
// legacy-modes 覆盖 shell/toml/dockerfile/lua/ruby。
// 浅色高对比语法配色。tags 来自 @lezer/highlight，颜色值为最终 CSS；
// 注释与正文引用宿主变量，其余用 Atom One Light 的成熟浅色色系。
let _hlStyle = null;
function hlStyle() {
  if (_hlStyle === null) {
    const t = CM.tags;
    // [AutoC 2026-08-24] 细线字体下区分度不足：关键字/类型/函数加粗
    // 补偿，字符串保持常规（绿色已足够显眼），数字加粗。
    _hlStyle = CM.HighlightStyle.define([
      { tag: [t.keyword, t.controlKeyword, t.moduleKeyword], color: '#a626a4', fontWeight: 'bold' },
      { tag: [t.string, t.special(t.string), t.regexp], color: '#50a14f' },
      { tag: t.comment, color: 'var(--duties-tertiary, #9a9a9a)', fontStyle: 'italic' },
      { tag: [t.number, t.bool, t.null], color: '#b76b01', fontWeight: 'bold' },
      { tag: [t.atom, t.labelName], color: '#0184bc' },
      { tag: [t.function(t.variableName), t.function(t.propertyName)], color: '#4078f2', fontWeight: 'bold' },
      { tag: [t.typeName, t.className, t.namespace], color: '#c18401', fontWeight: 'bold' },
      { tag: [t.propertyName, t.attributeName], color: '#e45649' },
      { tag: t.tagName, color: '#e45649', fontWeight: 'bold' },
      { tag: t.attributeValue, color: '#50a14f' },
      { tag: [t.operator, t.punctuation, t.separator], color: '#383a42' },
      { tag: [t.variableName, t.name], color: 'var(--duties-text, #1a1a1a)' },
      { tag: [t.heading, t.strong], color: 'var(--duties-text, #1a1a1a)', fontWeight: 'bold' },
      { tag: t.emphasis, fontStyle: 'italic' },
      { tag: t.link, color: 'var(--duties-accent, #feaf2c)' },
      { tag: [t.invalid, t.deleted], color: '#c0392b' },
      { tag: t.inserted, color: '#50a14f' },
      { tag: t.changed, color: '#c18401' },
    ]);
  }
  return _hlStyle;
}

let _langDescs = null;
function languageFor(path) {
  if (_langDescs === null) {
    const { LanguageDescription, StreamLanguage,
            javascript, python, json, html, css, xml, yaml, rust, cpp,
            java, go, sql, php,
            shell, toml, dockerFile, lua, ruby } = CM;
    _langDescs = [
      LanguageDescription.of({ name: 'javascript', extensions: ['js','mjs','cjs'], support: javascript() }),
      LanguageDescription.of({ name: 'typescript', extensions: ['ts','mts','cts'], support: javascript({ typescript: true }) }),
      LanguageDescription.of({ name: 'jsx', extensions: ['jsx'], support: javascript({ jsx: true }) }),
      LanguageDescription.of({ name: 'tsx', extensions: ['tsx'], support: javascript({ jsx: true, typescript: true }) }),
      LanguageDescription.of({ name: 'json', extensions: ['json','jsonl','map'], support: json() }),
      LanguageDescription.of({ name: 'python', extensions: ['py','pyi'], support: python() }),
      LanguageDescription.of({ name: 'html', extensions: ['html','htm'], support: html() }),
      LanguageDescription.of({ name: 'css', extensions: ['css'], support: css() }),
      LanguageDescription.of({ name: 'xml', extensions: ['xml','svg','xsd'], support: xml() }),
      LanguageDescription.of({ name: 'yaml', extensions: ['yaml','yml'], support: yaml() }),
      LanguageDescription.of({ name: 'rust', extensions: ['rs'], support: rust() }),
      LanguageDescription.of({ name: 'cpp', extensions: ['c','h','cpp','hpp','cc','cxx','hh'], support: cpp() }),
      LanguageDescription.of({ name: 'java', extensions: ['java'], support: java() }),
      LanguageDescription.of({ name: 'go', extensions: ['go'], support: go() }),
      LanguageDescription.of({ name: 'sql', extensions: ['sql'], support: sql() }),
      LanguageDescription.of({ name: 'php', extensions: ['php'], support: php() }),
      LanguageDescription.of({ name: 'shell', extensions: ['sh','bash','zsh'], support: StreamLanguage.define(shell) }),
      LanguageDescription.of({ name: 'toml', extensions: ['toml'], support: StreamLanguage.define(toml) }),
      LanguageDescription.of({ name: 'dockerfile', extensions: ['dockerfile'], support: StreamLanguage.define(dockerFile) }),
      LanguageDescription.of({ name: 'lua', extensions: ['lua'], support: StreamLanguage.define(lua) }),
      LanguageDescription.of({ name: 'ruby', extensions: ['rb'], support: StreamLanguage.define(ruby) }),
    ];
  }
  return CM.LanguageDescription.matchFilename(_langDescs, String(path));
}

// ── diff view ─────────────────────────────────────────────────────────────

function renderDiff(tab) {
  viewEl.innerHTML = '';
  if (tab._view) { tab._view.destroy(); tab._view = null; }
  const before = tab.text;
  const after = tab.afterText;
  if (!before && !after) { showStatus('加载中…'); return; }

  const aLines = before.split('\n');
  const bLines = after.split('\n');
  let pre = 0;
  while (pre < aLines.length && pre < bLines.length && aLines[pre] === bLines[pre]) pre++;
  let suf = 0;
  while (suf < aLines.length - pre && suf < bLines.length - pre
         && aLines[aLines.length - 1 - suf] === bLines[bLines.length - 1 - suf]) suf++;

  // 统一 diff 文档：上下文行 + 删除行 + 新增行 + 上下文行，逐行记录类别，
  // 行内容不带 +/- 前缀（前缀由 CSS ::before 呈现），语法高亮作用于原文。
  const kinds = [];
  const lines = [];
  for (let i = 0; i < pre; i++) { lines.push(aLines[i]); kinds.push('ctx'); }
  for (let i = pre; i < aLines.length - suf; i++) { lines.push(aLines[i]); kinds.push('del'); }
  for (let i = pre; i < bLines.length - suf; i++) { lines.push(bLines[i]); kinds.push('add'); }
  for (let i = aLines.length - suf; i < aLines.length; i++) { lines.push(aLines[i]); kinds.push('ctx'); }

  const host = document.createElement('div');
  host.className = 'cm-host';
  viewEl.appendChild(host);
  const { EditorState, EditorView, Decoration, ViewPlugin } = CM;
  const lineClass = {
    add: Decoration.line({ class: 'cm-diff-add' }),
    del: Decoration.line({ class: 'cm-diff-del' }),
    ctx: Decoration.line({ class: 'cm-diff-ctx' }),
  };
  // 只读文档构建后不变，装饰在视图创建时一次性计算即可。
  const decoPlugin = ViewPlugin.fromClass(class {
    constructor(view) {
      const ranges = [];
      const n = Math.min(kinds.length, view.state.doc.lines);
      for (let i = 0; i < n; i++) {
        const mark = lineClass[kinds[i]];
        if (mark) ranges.push(mark.range(view.state.doc.line(i + 1).from));
      }
      this.decorations = Decoration.set(ranges, true);
    }
  }, { decorations: (v) => v.decorations });

  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: lines.join('\n'),
      extensions: [
        // 与普通文件标签同一渲染层：行号、语法高亮、折叠、缩进、主题
        ...codeCommonExtensions(tab.path),
        decoPlugin,
        EditorState.readOnly.of(true),
        EditorView.editable.of(false),
      ],
    }),
  });
  tab._view = view;
  applyScrollFraction(view.scrollDOM, tab.scroll || 0);
  view.requestMeasure();
}

// ── editor ───────────────────────────────────────────────────────────────

// ── 共享代码视图层 ────────────────────────────────────────────────────────
// 普通文件标签与 diff 标签（apply_diff 逆推 / git 变更 / 提交差异）共用同一
// CodeMirror 构造路径：行号、语法高亮、括号匹配、折叠、缩进、基础主题都在
// 这里。可编辑差异（history/keymap/脏跟踪/失焦切换）由 renderEditor 追加，
// diff 视图追加只读与行级红绿装饰。

function diffBasePath(p) {
  // diff 标签路径形如 'a/b.py (diff)' 或 'a/b.py @1a2b3c4 (diff)'，
  // 语法匹配前剥掉后缀取真实文件路径。
  return String(p).replace(/(?:\s*@[0-9a-f]{7,40})?\s*\(diff\)\s*$/, '');
}

function codeCommonExtensions(path) {
  const { EditorState, EditorView, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
          drawSelection, bracketMatching, foldGutter, syntaxHighlighting,
          highlightSelectionMatches, indentUnit, markdown, markdownLanguage } = CM;
  const base = diffBasePath(path);
  const langExts = isMarkdown(base)
    ? [markdown({ base: markdownLanguage })]
    : (() => { const d = languageFor(base); return d && d.support ? [d.support] : []; })();
  return [
    lineNumbers(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    drawSelection(),
    bracketMatching(),
    foldGutter(),
    // [AutoC 2026-08-24] 高对比浅色配色（Atom One Light 色系）。
    syntaxHighlighting(hlStyle()),
    highlightSelectionMatches(),
    ...langExts,
    // tabSize 决定 Tab 字符显示宽度，indentUnit 决定 Tab 命令插入的空白。
    EditorState.tabSize.of(indentWidth),
    indentUnit.of(' '.repeat(indentWidth)),
    EditorView.theme({
      '&': { height: '100%' },
      '.cm-scroller': { overflow: 'auto' },
    }),
  ];
}

function markEdited(tab, text) {
  tab.text = text;
  const wasDirty = tab.dirty;
  const wasPinned = tab.pinned;
  tab.dirty = tab.text !== tab.savedText;
  if (!tab.pinned) tab.pinned = true;   // 编辑预览标签 → 固定
  if (tab.dirty !== wasDirty || tab.pinned !== wasPinned) {
    renderTabs();
    renderStatusbar();
  }
  if (activePath === tab.path) updateSaveButton(tab);
}

function renderEditor(tab) {
  viewEl.innerHTML = '';
  if (tab._view) { tab._view.destroy(); tab._view = null; }
  const host = document.createElement('div');
  host.className = 'cm-host';
  viewEl.appendChild(host);

  const { EditorState, EditorView, keymap, defaultKeymap, history,
          historyKeymap, indentWithTab, searchKeymap } = CM;

  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: tab.text,
      extensions: [
        // 共享代码视图层：行号、高亮、括号、折叠、缩进、主题
        ...codeCommonExtensions(tab.path),
        history(),
        keymap.of([
          ...defaultKeymap,
          ...historyKeymap,
          ...searchKeymap,
          indentWithTab,
          { key: 'Mod-s', run: () => { saveTab(tab); return true; } },
        ]),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) markEdited(tab, u.state.doc.toString());
        }),
        // MD 编辑器失焦时切回渲染。relatedTarget 在面板内控件（状态栏
        // 分栏、保存按钮、标签栏）上时不切，避免点控件时意外切换。
        ...(isMarkdown(tab.path) ? [EditorView.domEventHandlers({
          blur: (e) => {
            const to = e.relatedTarget;
            if (to && (to.closest('.statusbar') || to.closest('.tabbar'))) return;
            // 延迟检查：焦点可能还在面板内其他可聚焦元素之间移动
            setTimeout(() => {
              const ae = document.activeElement;
              if (ae && (ae.closest('.cm-editor') || ae.closest('.statusbar') || ae.closest('.tabbar'))) return;
              if (activePath === tab.path && tab.mode === 'edit') {
                setTabMode(tab, 'render');
              }
            }, 0);
          },
        })] : []),
      ],
    }),
  });
  tab._view = view;
  // 游标位置按文档长度钳制（文档可能被外部改短），滚动按比例恢复。
  const head = Math.min(tab.cursor || 0, view.state.doc.length);
  view.dispatch({ selection: { anchor: head } });
  applyScrollFraction(view.scrollDOM, tab.scroll || 0);
  view.requestMeasure();
}

// [AutoC 2026-08-24] Markdown 渲染：marked 解析 + 本地 sanitize。
// 文件可能由 AI 写入，渲染前剥离 script/iframe 等元素、on* 事件属性与
// javascript: 链接，防止面板页内脚本执行（面板可访问 boot token）。
function sanitizeDom(root) {
  root.querySelectorAll('script,iframe,object,embed,form,link,meta,style').forEach((el) => el.remove());
  root.querySelectorAll('*').forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on')) el.removeAttribute(attr.name);
      else if ((name === 'href' || name === 'src' || name === 'xlink:href')
               && /^\s*javascript:/i.test(attr.value)) el.removeAttribute(attr.name);
    }
  });
}

function renderMarkdown(tab) {
  viewEl.innerHTML = '';
  if (tab._view) { tab._view.destroy(); tab._view = null; }
  const body = document.createElement('div');
  body.className = 'md-body';
  try {
    body.innerHTML = MarkedLib.marked.parse(tab.text || '');
  } catch (err) {
    body.textContent = tab.text || '';
  }
  sanitizeDom(body);
  // 点击渲染视图任意位置进入编辑模式（Typora 语义）
  body.addEventListener('click', (e) => {
    // 链接点击正常跳转，不触发编辑
    if (e.target.closest('a')) return;
    setTabMode(tab, 'edit');
    // 进入编辑后立即获得焦点，让光标落在点击位置附近
    requestAnimationFrame(() => {
      if (tab._view) tab._view.focus();
    });
  });
  viewEl.appendChild(body);
  applyScrollFraction(viewEl, tab.scroll || 0);
}

function setTabMode(tab, mode) {
  if (tab.mode === mode) return;
  // 编辑 → 渲染：文本已由 docChanged 同步到 tab.text，无需额外保存。
  tab.mode = mode;
  stashScroll();
  renderFileTab(tab);
  renderStatusbar();
}

async function saveTab(tab) {
  if (tab.kind !== 'text' || !tab.dirty) return;
  try {
    await api('/plugins/ide/file?session_id=' + encodeURIComponent(SESSION_ID)
      + '&path=' + encodeURIComponent(tab.path), {
      method: 'PUT',
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      body: tab.text,
    });
    tab.savedText = tab.text;
    tab.dirty = false;
    saveHint = '已保存';
  } catch (err) {
    saveHint = '保存失败：' + err.message;
  }
  renderTabs();
  renderStatusbar();
  updateSaveButton(tab);
}

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    const tab = findTab(activePath);
    if (tab && tab.kind === 'text') {
      e.preventDefault();
      saveTab(tab);
    }
  }
});
