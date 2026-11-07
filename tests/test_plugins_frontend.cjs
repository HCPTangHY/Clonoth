// Offline plugin checks: no browser, network, package install or generated bundle.
// Run: node --experimental-vm-modules tests/test_plugins_frontend.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const web = path.join(root, 'plugins/ide/web');
const read = (relative) => fs.readFileSync(path.join(web, relative), 'utf8');
let parsed = 0;

function syntax(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) syntax(file);
    else if (file.endsWith('.js')) {
      new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { identifier: file });
      parsed++;
    }
  }
}

function element() {
  return {
    attrs: {}, events: {}, children: [], scrollTop: 0,
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(name, callback) { this.events[name] = callback; },
    appendChild(child) { this.children.push(child); },
    querySelectorAll() { return []; },
    remove() {},
  };
}

try {
  syntax(path.join(root, 'plugins'));
  for (const match of read('index.html').matchAll(/(?:src|href)="([^"]+)"/g)) {
    assert.ok(fs.existsSync(path.join(web, match[1])), `missing HTML asset: ${match[1]}`);
  }

  // Exercise the shipped bundle, including the legacy extension's shared-state alias.
  const bundle = vm.createContext({});
  vm.runInContext(read('vendor/codemirror.js'), bundle, { timeout: 5000 });
  for (const name of ['EditorState', 'EditorView', 'rectangularSelection', 'crosshairCursor']) {
    assert.equal(typeof bundle.CM[name], 'function', name);
  }
  const state = bundle.CM.EditorState.create({
    doc: 'test', extensions: [bundle.CM.rectangularSelection(), bundle.CM.crosshairCursor()],
  });
  assert.equal(state.doc.toString(), 'test');

  // Scroll restoration waits for CodeMirror measurement, then corrects two frames.
  const frames = [], applied = [];
  const editor = vm.createContext({
    document: { addEventListener() {}, createElement: element },
    requestAnimationFrame: (fn) => frames.push(fn),
    applyScrollFraction: (el, fraction) => applied.push([el, fraction]),
  });
  vm.runInContext(read('js/editor.js'), editor);
  let measurement;
  const view = { scrollDOM: {}, requestMeasure: (request) => { measurement = request; } };
  editor.restoreEditorScroll(view, 0.6);
  assert.equal(applied.length, 0);
  measurement.write();
  frames.shift()();
  frames.shift()();
  assert.equal(applied.length, 3);
  assert.ok(applied.every(([el, fraction]) => el === view.scrollDOM && fraction === 0.6));

  // Selecting Markdown text must not enter editing; an ordinary click still can.
  let selected = 'selected text', edits = 0;
  editor.window = { getSelection: () => ({ toString: () => selected }) };
  editor.MarkedLib = { marked: { parse: (text) => text } };
  editor.viewEl = element();
  editor.setTabMode = () => { edits++; };
  editor.renderMarkdown({ text: 'markdown', scroll: 0 });
  const click = editor.viewEl.children[0].events.click;
  click({ target: { closest: () => null } });
  assert.equal(edits, 0);
  selected = '';
  click({ target: { closest: () => null } });
  assert.equal(edits, 1);
  click({ target: { closest: () => ({}) } });
  assert.equal(edits, 1);

  // Tree scrolling is separate from the Git/search full-page views.
  const treeFrames = [];
  const tree = vm.createContext({
    document: { createElement: element }, TREE_TAB: 'tree', viewPath: 'tree', activePath: 'tree',
    viewEl: element(), gitViewActive: false, searchViewActive: false,
    findTab: () => null, renderStatusbar() {}, requestAnimationFrame: (fn) => treeFrames.push(fn),
  });
  vm.runInContext(read('js/view.js'), tree);
  tree.viewEl.scrollTop = 320;
  vm.runInContext('treeBooted = true; stashScroll(); viewPath = "file";', tree);
  tree.viewEl.scrollTop = 0;
  tree.renderView();
  assert.equal(tree.viewEl.scrollTop, 320);
  treeFrames.shift()();
  tree.gitViewActive = true;
  tree.viewEl.scrollTop = 999;
  tree.stashScroll();
  tree.gitViewActive = false;
  tree.viewPath = 'file';
  tree.renderView();
  assert.equal(tree.viewEl.scrollTop, 320);

  // Tabs expose focus/ARIA attributes and activate with Enter or Space.
  const rendered = [];
  const tabs = vm.createContext({
    document: { createElement: element }, TREE_TAB: 'tree',
    tabbarEl: { querySelectorAll: () => [], querySelector: () => ({}),
      insertBefore: (el) => rendered.push(el) },
    baseName: (name) => name, renderView() {},
  });
  vm.runInContext(read('js/tabs.js'), tabs);
  vm.runInContext('tabs.push({ path: "note.txt", pinned: true }); renderTabs();', tabs);
  const fileTab = rendered[1];
  assert.equal(fileTab.tabIndex, 0);
  assert.equal(fileTab.attrs.role, 'button');
  assert.equal(fileTab.attrs['aria-label'], 'note.txt');
  assert.equal(fileTab.attrs['aria-pressed'], 'false');
  let prevented = 0;
  fileTab.events.keydown({ target: fileTab, key: 'Enter', preventDefault() { prevented++; } });
  assert.equal(vm.runInContext('activePath', tabs), 'note.txt');
  rendered[0].events.keydown({ target: rendered[0], key: ' ', preventDefault() { prevented++; } });
  assert.equal(vm.runInContext('activePath', tabs), 'tree');
  assert.equal(prevented, 2);

  fs.writeSync(1, JSON.stringify({ javascriptSyntax: parsed, assetReferences: 'passed',
    bundleExtensionIdentity: 'passed', editorScroll: 'passed', markdownSelection: 'passed',
    treeScroll: 'passed', keyboardTabs: 'passed' }) + '\n');
  // ponytail: these checks are synchronous; explicit exit avoids the host's
  // Node 22/libuv teardown failure on its older kernel. Browser layout is not tested.
  process.exit(0);
} catch (error) {
  fs.writeSync(2, String(error.stack || error) + '\n');
  process.exit(1);
}
