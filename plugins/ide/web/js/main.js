/* 入口：宿主 intent 监听、panel-ready 握手、boot 渲染。 */

// ── panel-intent listener ──────────────────────────────────────────────────
// The host forwards the opaque open intent after the iframe loads. ide owns
// 'open-file' and 'open-diff'; other intents are ignored.
window.addEventListener('message', (ev) => {
  const data = ev && ev.data;
  if (!data || data.type !== 'clonoth:panel-intent') return;
  const intent = data.intent;
  if (!intent) return;
  if (intent.kind === 'open-file' && intent.path) {
    openFileTab(String(intent.path), false);
  } else if (intent.kind === 'open-diff' && intent.path && Array.isArray(intent.diffs)) {
    openDiffTab(String(intent.path), intent.diffs);
  }
});
// 通知宿主：监听器已就绪，可以重发 intent。解决首次点击只初始化不跳转。
window.parent.postMessage({ type: 'clonoth:panel-ready' }, window.location.origin);

// ── boot ─────────────────────────────────────────────────────────────────

renderTabs();
renderView();
