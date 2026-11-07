// [2026-09-12] Panel entry resolution, kept out of the React component so the
// same-origin rule is unit-testable without a DOM.
// Why: the panel cookie is scoped to the plugin's own /v1/plugins/{owner}/web/
// path, so a panel may only load same-origin plugin statics. How: resolve the
// entry against the current origin and reject anything that is not a plain
// plugin static path (no credentials, no fragment, no foreign origin).
// Purpose: host and backend panel_auth agree on exactly one allowed shape.
export const PANEL_ENTRY_PATH_PATTERN = /^\/v1\/plugins\/[A-Za-z0-9_-]{1,64}\/web\//;

export const PANEL_ENTRY_REJECTED = '面板地址必须是本站插件静态资源';

export type PanelEntryResolution = { path: string; error?: undefined } | { path?: undefined; error: string };

export function resolvePanelEntry(entry: string, origin: string): PanelEntryResolution {
  let url: URL;
  try {
    url = new URL(entry, origin);
  } catch {
    return { error: '面板地址无效' };
  }
  if (url.origin !== origin || url.username || url.password || url.hash
      || !PANEL_ENTRY_PATH_PATTERN.test(url.pathname)) {
    return { error: PANEL_ENTRY_REJECTED };
  }
  return { path: url.pathname + url.search };
}
