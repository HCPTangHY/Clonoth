// File-link annotator for assistant messages.
// Declared in PLUGIN_META.client.annotators; loaded by the host's annotator
// registry as a blob ES module. Pure function: text + ctx in, match ranges out.
// The host renders matched spans as clickable buttons that open the 'files'
// panel with the ide-private intent {kind:'open-file', path}.

// ── Data layer: file tree cache, completely decoupled from render ─────────
const _cache = new Map(); // sessionId -> { ts, set }
const _inflight = new Map(); // sessionId -> Promise (dedup)
const _knownSids = new Set(); // sessions we've seen and scheduled refresh for
const _TTL = 60_000;
let _refreshTimer = null;

async function _fetchTree(sid) {
  const token = (() => { try { return localStorage.getItem('clonoth_admin_token') || ''; } catch { return ''; } })();
  const q = sid ? `session_id=${encodeURIComponent(sid)}&` : '';
  const resp = await fetch(`/v1/workspace/tree?${q}depth=8`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) return null;
  const data = await resp.json();
  const set = new Set();
  const walk = (n) => {
    if (!n || !Array.isArray(n.children)) return;
    for (const c of n.children) {
      if (c.type === 'file') set.add(String(c.path || ''));
      else if (c.type === 'directory') walk(c);
    }
  };
  walk(data && data.tree);
  return set;
}

// Single in-flight per sid. On success caches the set; on failure does nothing
// (stale cache or empty is fine, next scheduled tick will retry).
function _loadTree(sid) {
  if (_inflight.has(sid)) return _inflight.get(sid);
  const p = _fetchTree(sid)
    .then((set) => { if (set) _cache.set(sid, { ts: Date.now(), set }); })
    .catch(() => {}) // swallow — stale cache is acceptable
    .finally(() => _inflight.delete(sid));
  _inflight.set(sid, p);
  return p;
}

// Background refresh: runs on a fixed interval, independent of renders.
function _ensureRefreshLoop() {
  if (_refreshTimer) return;
  _refreshTimer = setInterval(() => {
    for (const sid of _knownSids) {
      const cached = _cache.get(sid);
      if (!cached || Date.now() - cached.ts >= _TTL) {
        void _loadTree(sid);
      }
    }
  }, _TTL);
}

// ── Render layer: pure synchronous, zero side effects ─────────────────────
const _PATH_RE = /(?:[\w.@~-][\w./@~-]*)/g;

function _looksLikePath(s) {
  if (!s || s.length < 3) return false;
  if (!/\.[A-Za-z0-9]{1,12}$/.test(s) && !s.includes('/')) return false;
  if (/^(https?:|mailto:|\d+\.\d+)/.test(s)) return false;
  return true;
}

export default function match(text, ctx) {
  const sid = (ctx && ctx.sessionId) || '';
  const out = [];

  // First time seeing this session: schedule one load + start background loop.
  // This is the ONLY place that triggers a fetch, and it fires at most once
  // per session id. All subsequent refreshes come from the interval timer.
  if (sid && !_knownSids.has(sid)) {
    _knownSids.add(sid);
    void _loadTree(sid);
    _ensureRefreshLoop();
    return out; // cache is guaranteed cold, skip matching
  }

  const cached = _cache.get(sid);
  if (!cached) return out; // still loading, return empty — no fetch triggered

  const set = cached.set;
  let m;
  _PATH_RE.lastIndex = 0;
  while ((m = _PATH_RE.exec(text))) {
    const p = m[0];
    if (!_looksLikePath(p)) continue;
    let norm = p;
    while (norm.startsWith('./')) norm = norm.slice(2);
    if (!set.has(norm)) continue;
    out.push({
      start: m.index,
      end: m.index + p.length,
      open: { panel: 'files', intent: { kind: 'open-file', path: norm } },
    });
  }
  return out;
}
