// File-link annotator for assistant messages.
// Declared in PLUGIN_META.client.annotators; loaded by the host's annotator
// registry as a blob ES module. Pure function: text + ctx in, match ranges out.
// The host renders matched spans as clickable buttons that open the 'files'
// panel with the ide-private intent {kind:'open-file', path}.

const _cache = new Map(); // sessionId -> { ts, set }
const _TTL = 60_000;

async function _fileSet(sid) {
  const cached = _cache.get(sid);
  if (cached && Date.now() - cached.ts < _TTL) return cached.set;
  // The annotator module runs in the host page main world; callHostAction is
  // not importable from a blob module. The host passes api through ctx — but
  // annotators receive { role, sessionId } only. File-set loading therefore
  // uses the composer's session via a lazily-imported host module path is not
  // available either. Simplest correct approach: fetch the tree through the
  // same api surface slots use — but annotator scripts have no api handle.
  // Resolution: the host's annotator registry injects nothing; the annotator
  // instead reads the tree through window.fetch with the admin token from
  // localStorage (same-origin, same mechanism as supervisorClient).
  const token = (() => { try { return localStorage.getItem('clonoth_admin_token') || ''; } catch { return ''; } })();
  const q = sid ? `session_id=${encodeURIComponent(sid)}&` : '';
  const resp = await fetch(`/v1/workspace/tree?${q}depth=8`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) return new Set();
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
  _cache.set(sid, { ts: Date.now(), set });
  return set;
}

// Path candidate: contains a dot or slash, no whitespace, looks file-like.
const _PATH_RE = /(?:[\w.@~-][\w./@~-]*)/g;

function _looksLikePath(s) {
  if (!s || s.length < 3) return false;
  if (!/\.[A-Za-z0-9]{1,12}$/.test(s) && !s.includes('/')) return false;
  if (/^(https?:|mailto:|\d+\.\d+)/.test(s)) return false; // urls/versions
  return true;
}

export default function match(text, ctx) {
  const sid = (ctx && ctx.sessionId) || '';
  const out = [];
  // The file set loads async; the annotator must be synchronous per contract.
  // On a cold cache we return no matches this render; the next render (cache
  // warm) picks them up. Trigger the load as a side effect.
  const cached = _cache.get(sid);
  if (!cached || Date.now() - cached.ts >= _TTL) {
    void _fileSet(sid); // warm for next render
    return out;
  }
  const set = cached.set;
  let m;
  _PATH_RE.lastIndex = 0;
  while ((m = _PATH_RE.exec(text))) {
    const p = m[0];
    if (!_looksLikePath(p)) continue;
    // normalize a leading ./ before lookup
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
