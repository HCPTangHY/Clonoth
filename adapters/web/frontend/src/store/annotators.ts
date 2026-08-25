// [AutoC 2026-08-25] Declarative message annotator registry.
// Why: assistant message text is owned by ReactMarkdown's reconciliation; plugins
// cannot safely rewrite its DOM (stream re-renders erase foreign nodes). The
// rendering-interception need is met by splitting the concern: plugins decide
// WHICH spans are clickable and WHERE a click goes (data), the host renders and
// handles clicks (render). How: each annotator is a pure match(text, ctx)
// function loaded as a blob ES module, registered from the plugin manifest and
// dropped whenever the plugin list refreshes — disappearance is automatic since
// the registry is rebuilt from the manifest. Purpose: plugins annotate message
// text without touching host DOM or host business semantics.
import { listPlugins, type PluginAnnotatorDecl } from '../api/supervisorClient';

export interface AnnotatorMatch {
  start: number;
  end: number;
  /** Opaque click target: open one overlay panel with an opaque intent object. */
  open: { panel: string; intent: Record<string, unknown> };
}

export interface AnnotatorCtx {
  role: string;
  sessionId: string;
}

export type AnnotatorMatchFn = (text: string, ctx: AnnotatorCtx) => AnnotatorMatch[];

export interface LoadedAnnotator {
  id: string;
  owner: string;
  priority: number;
  match: AnnotatorMatchFn;
}

interface AnnotatorHandle {
  blobUrl: string;
  match: AnnotatorMatchFn;
}

const active = new Map<string, AnnotatorHandle>();
let loading: Promise<void> | null = null;
let version = 0;
let epoch = 0;

function annotatorKey(owner: string, decl: PluginAnnotatorDecl): string {
  return `${owner}:${decl.id}`;
}

/**
 * Rebuild the annotator set from the current manifest. New annotators load as
 * blob modules; annotators absent from the manifest are revoked. Concurrent or
 * overlapping refreshes are collapsed onto one in-flight rebuild.
 */
export function refreshAnnotators(): Promise<void> {
  if (loading) return loading;
  const myEpoch = ++epoch;
  loading = (async () => {
    let plugins;
    try {
      plugins = await listPlugins();
    } catch {
      return; // backend unreachable; keep existing set
    }
    if (myEpoch !== epoch) return;

    const wanted = new Map<string, { owner: string; priority: number; script: string }>();
    for (const plugin of plugins) {
      const client = plugin.client;
      if (!client || typeof client !== 'object') continue;
      for (const decl of client.annotators || []) {
        if (!decl?.id || !decl.script) continue;
        wanted.set(annotatorKey(plugin.name, decl), {
          owner: plugin.name,
          priority: Number.isFinite(decl.priority) ? Number(decl.priority) : 50,
          script: decl.script,
        });
      }
    }

    // revoke annotators that disappeared (plugin unloaded)
    for (const [key, handle] of active) {
      if (wanted.has(key)) continue;
      URL.revokeObjectURL(handle.blobUrl);
      active.delete(key);
      version++;
    }

    // load annotators not yet active (new or script changed)
    for (const [key, entry] of wanted) {
      const existing = active.get(key);
      if (existing) continue; // unchanged scripts stay; reload after unload/reload
      const blobUrl = URL.createObjectURL(
        new Blob([entry.script], { type: 'text/javascript' }),
      );
      try {
        const mod = await import(/* @vite-ignore */ blobUrl);
        const def: unknown = (mod as { default?: unknown }).default;
        if (typeof def !== 'function') {
          URL.revokeObjectURL(blobUrl);
          console.warn(`[annotator] ${key} default export is not a function`);
          continue;
        }
        active.set(key, { blobUrl, match: def as AnnotatorMatchFn });
        version++;
      } catch (err) {
        URL.revokeObjectURL(blobUrl);
        console.error(`[annotator] ${key} failed to load`, err);
      }
    }

    // sync priority/owner meta for consumers (getLoadedAnnotators)
    annotatorMeta.clear();
    for (const [key, entry] of wanted) {
      if (active.has(key)) annotatorMeta.set(key, { owner: entry.owner, priority: entry.priority });
    }
  })().finally(() => {
    loading = null;
  });
  return loading;
}

/**
 * Annotate one text fragment. Annotators run in priority order; each receives
 * the spans already claimed by higher-priority annotators and must not overlap
 * them (taken spans are passed as reference, not filtered automatically — the
 * annotator declares clean matches against the visible text). Errors in one
 * annotator drop only that annotator's contribution.
 */
export function annotateText(text: string, ctx: AnnotatorCtx): AnnotatorMatch[] {
  const out: AnnotatorMatch[] = [];
  const taken: Array<[number, number]> = [];
  const list = getLoadedAnnotators();
  for (const annotator of list) {
    let matches: AnnotatorMatch[];
    try {
      matches = annotator.match(text, ctx) || [];
    } catch (err) {
      console.error(`[annotator] ${annotator.id} match failed`, err);
      continue;
    }
    for (const m of matches) {
      if (!m || !Number.isFinite(m.start) || !Number.isFinite(m.end) || m.end <= m.start) continue;
      if (!m.open || typeof m.open.panel !== 'string' || !m.open.panel) continue;
      if (typeof m.open.intent !== 'object' || m.open.intent == null) continue;
      const overlaps = taken.some(([s, e]) => m.start < e && m.end > s);
      if (overlaps) continue;
      taken.push([m.start, m.end]);
      out.push(m);
    }
  }
  out.sort((a, b) => a.start - b.start);
  return out;
}

export function getLoadedAnnotators(): LoadedAnnotator[] {
  const result: LoadedAnnotator[] = [];
  // rebuild identity from the manifest-independent side each call; cheap and
  // avoids a second priority copy. IDs come from active keys plus the manifest.
  // Priority/owner live in the manifest-facing path above; here we re-read via
  // a small parallel map kept in refreshAnnotators.
  for (const [key, handle] of active) {
    const meta = annotatorMeta.get(key);
    if (!meta) continue;
    result.push({ id: key, owner: meta.owner, priority: meta.priority, match: handle.match });
  }
  result.sort((a, b) => b.priority - a.priority);
  return result;
}

// priority/owner per active key; refreshed alongside `active`
const annotatorMeta = new Map<string, { owner: string; priority: number }>();

/** Monotonic version for annotation consumers (memoization). */
export function getAnnotatorVersion(): number {
  return version;
}
