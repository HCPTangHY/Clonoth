/* 面板外壳：状态栏、标签栏保存按钮、缩进偏好切换。 */

function renderStatusbar() {
  const tab = findTab(activePath);
  if (!tab || tab.path === TREE_TAB) {
    statusbarEl.innerHTML = '<div class="seg grow">' + (gitViewActive ? '源代码管理' : '工作区文件') + '</div>';
    barSaveBtn.classList.remove('show');
    return;
  }
  let hint = '';
  let hintClass = '';
  if (saveHint) {
    hint = saveHint;
    hintClass = saveHint.startsWith('保存失败') ? 'hint-error' : '';
    saveHint = '';   // 一次性
  } else if (tab.kind === 'diff') {
    hint = tab.note || '修改前后对比（逆推重建，仅供参考）';
    hintClass = 'hint-diff';
  } else if (tab.kind === 'gitdiff') {
    hint = tab.note || '工作区变更对比（git diff）';
    hintClass = 'hint-diff';
  } else if (tab.kind === 'text') {
    hint = tab.dirty ? '未保存 · Ctrl+S' : '已保存';
    hintClass = tab.dirty ? 'hint-dirty' : '';
  }
  const isMd = tab.kind === 'text' && isMarkdown(tab.path);
  // [AutoC 2026-08-24] Typora 式焦点切换：渲染视图点击进入编辑，编辑器
  // 失焦切回渲染，状态栏不再放切换分栏。渲染态提示放在 hint 之后。
  const mdHint = (isMd && tab.mode === 'render')
    ? '<div class="seg">点击进入编辑</div>'
    : '';
  const indentSeg = (tab.kind === 'text' && (!isMd || tab.mode === 'edit'))
    ? '<div class="seg clickable" id="sb-indent" title="Tab 缩进宽度，点击在 2/4/8 间切换">缩进: ' + indentWidth + '</div>'
    : '';
  statusbarEl.innerHTML = '<div class="seg grow">' + esc(tab.path) + '</div>'
    + (hint ? '<div class="seg ' + hintClass + '">' + esc(hint) + '</div>' : '')
    + mdHint + indentSeg;
  const ibtn = document.getElementById('sb-indent');
  if (ibtn) ibtn.addEventListener('click', cycleIndent);
  updateSaveButton(tab);
}

// 标签栏右端常驻保存按钮：仅文本标签页时显示，脏状态点亮
const barSaveBtn = document.getElementById('bar-save');
barSaveBtn.addEventListener('click', () => {
  const tab = findTab(activePath);
  if (tab && tab.kind === 'text') saveTab(tab);
});

const barSaveDot = barSaveBtn.querySelector('.dirty-dot');

function updateSaveButton(tab) {
  // 渲染模式的 MD 标签页不可编辑，无保存需求，隐藏按钮。
  if (!tab || tab.kind !== 'text' || (isMarkdown(tab.path) && tab.mode === 'render')) {
    barSaveBtn.classList.remove('show');
    return;
  }
  barSaveBtn.classList.add('show');
  barSaveBtn.disabled = !tab.dirty;
  barSaveDot.style.display = tab.dirty ? '' : 'none';
}

function cycleIndent() {
  const idx = INDENT_STEPS.indexOf(indentWidth);
  indentWidth = INDENT_STEPS[(idx + 1) % INDENT_STEPS.length];
  try { localStorage.setItem(LS_INDENT, String(indentWidth)); } catch (e) {}
  // 重建当前激活的文本编辑器（保留文档内容与滚动位置）；其他标签页在
  // 下次激活时经 renderEditor 读取新偏好。
  const tab = findTab(activePath);
  if (tab && tab.kind === 'text' && !(isMarkdown(tab.path) && tab.mode === 'render')) {
    tab.text = tab._view ? tab._view.state.doc.toString() : tab.text;
    if (tab._view) {
      tab.scroll = scrollFraction(tab._view.scrollDOM);
      tab.cursor = tab._view.state.selection.main.head;
    }
    tab._view = null;
    renderFileTab(tab);
  }
  renderStatusbar();
}
