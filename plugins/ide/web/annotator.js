// File-link annotator for assistant messages.
// Declared in PLUGIN_META.client.annotators; loaded by the host's annotator
// registry as a blob ES module. Pure function: text + ctx in, match ranges out.
// The host renders matched spans as clickable buttons that open the 'files'
// panel with the ide-private intent {kind:'open-file', path}.

const _cache = new Map(); // sessionId -> { ts, set, ok }
const _inflight = new Map(); // sessionId -> Promise (dedup concurrent fetches)
const _failures = new Map(); // sessionId -> consecutive failure count
const _TTL = 60_000;
const _ERROR_TTL_BASE = 10_000; // 10s base, doubles per consecutive failure
const _ERROR_TTL_MAX = 300_000; // cap at 5 minutes

function _errorTTL(sid) {
  const count = _failures.get(sid) || 0;
  return Math.min(_ERROR_TTL_BASE * Math.pow(2, count), _ERROR_TTL_MAX);
}

async function _fileSet(sid) {
  const cached = _cache.get(sid);
  if (cached) {
    const ttl = cached.ok ? _TTL : _errorTTL(sid);
    if (Date.now() - cached.ts < ttl) return cached.set;
  }
  const token = (() => { try { return localStorage.getItem('clonoth_admin_token') || ''; } catch { return ''; } })();
  const q = sid ? `session_id=${encodeURIComponent(sid)}&` : '';
  let resp;
  try {
    resp = await fetch(`/v1/workspace/tree?${q}depth=8`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  } catch (err) {
    // Network error: cache empty set with error TTL + backoff
    _failures.set(sid, (_failures.get(sid) || 0) + 1);
    _cache.set(sid, { ts: Date.now(), set: new Set(), ok: false });
    return new Set();
  }
  if (!resp.ok) {
    // HTTP error: same treatment
    _failures.set(sid, (_failures.get(sid) || 0) + 1);
    _cache.set(sid, { ts: Date.now(), set: new Set(), ok: false });
    return new Set();
  }
  // Success: reset failure counter, cache with normal TTL
  _failures.delete(sid);
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
  _cache.set(sid, { ts: Date.now(), set, ok: true });
  return set;
}

// Deduplicates concurrent _fileSet calls for the same session.
function _fileSetDedup(sid) {
  const existing = _inflight.get(sid);
  if (existing) return existing;
  const p = _fileSet(sid).catch((err) => {
    console.warn('[annotator] _fileSet failed', err);
    // Ensure failure is cached even if _fileSet itself throws unexpectedly
    _failures.set(sid, (_failures.get(sid) || 0) + 1);
    _cache.set(sid, { ts: Date.now(), set: new Set(), ok: false });
    return new Set();
  }).finally(() => _inflight.delete(sid));
  _inflight.set(sid, p);
  return p;
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
  if (!cached) {
    // Absolute cold start: trigger fetch, return empty
    void _fileSetDedup(sid);
    return out;
  }
  const ttl = cached.ok ? _TTL : _errorTTL(sid);
  if (Date.now() - cached.ts >= ttl) {
    // Cache expired (success TTL or error backoff TTL): refresh
    void _fileSetDedup(sid);
    // If last fetch was an error, don't try to match against empty set
    if (!cached.ok) return out;
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
