// [AutoC 2026-08-22] Plugin panel host: iframe container for plugin-declared panels.
// Why: plugin panels are self-contained HTML pages served by the plugin itself;
// the host only provides the frame, a title bar, and a same-origin boot object.
// How: set window.__CLONOTH_BOOT__ (token + sessionId) before mount so the panel
// page reads it via window.parent — the token never appears in a URL; remount
// the iframe per session so stale boot data cannot leak across sessions.
// [plugin-admin 2026-08-23] Theme injection: after each frame load, copy the
// host's live CSS variables onto the panel document (themeBridge). Panels
// reference var(--duties-*) by name and hold no theme values themselves, so
// host theme changes and plugin styles overrides propagate automatically.
// [2026-09-12] Panel session handshake: the panel iframe stays on the plugin's
// own static path (same origin, /v1/plugins/{owner}/web/...). Before it mounts,
// the host asks the backend for a short-lived panel cookie scoped to that path
// and renews it while the panel is open; logout revokes it. The boot object
// keeps publishing the admin token for plugin API calls — the cookie is an
// additional transport grant, not a replacement for that token.
// Purpose: plugins get a full UI surface with zero frontend build involvement,
// visually indistinguishable from host chrome.
import { useEffect, useMemo, useRef, useState } from 'react';

import { pluginApiRequest } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { callHostAction } from '../../store/hostActions';
import { Icon } from '../common';
import { resolvePanelEntry } from './panelEntry';
import { applyHostTheme } from './themeBridge';

// module-level refcount for the shared window.__CLONOTH_BOOT__ (see effect below)
let bootRefCount = 0;

interface PanelContext {
  sessionId: string;
  readonly viewportWidth: number;
  request: (path: string, init?: RequestInit) => Promise<unknown>;
  call: (name: string, ...args: unknown[]) => Promise<unknown>;
  close: () => void;
}

type PanelWindow = Window & { __CLONOTH_PANEL__?: PanelContext };

interface PanelAccess {
  key: string;
  ready: boolean;
  error?: string;
}

interface PluginPanelProps {
  entry: string;
  title: string;
  sessionId: string;
  onClose: () => void;
  /** [AutoC 2026-08-25] Overlay id (e.g. 'files' or 'plugin:owner:id') used to
   * look up the open intent in viewStore and forward it to the panel page. */
  overlayId?: string;
  /** [plugin-admin 2026-08-23] When false, render the bare iframe without the
   * title bar and close button — the settings-view variant where the sidebar
   * already names the tab and closing is navigation, not an overlay. */
  chrome?: boolean;
}

export const PluginPanel = ({ entry, title, sessionId, onClose, overlayId, chrome = true }: PluginPanelProps) => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [attempt, setAttempt] = useState(0);
  // remount the frame whenever entry, session, or the credential changes; the
  // effect below re-publishes the boot object before the new page's scripts run.
  const frameKey = useMemo(
    () => `${entry}::${sessionId}::${adminToken}::${attempt}`,
    [entry, sessionId, adminToken, attempt],
  );
  const [access, setAccess] = useState<PanelAccess | null>(null);
  const accessForFrame = access?.key === frameKey ? access : null;
  const panelReady = Boolean(adminToken) && Boolean(accessForFrame?.ready);
  const accessError = accessForFrame?.error || '';
  const frameRef = useRef<HTMLIFrameElement>(null);
  const currentFrameKey = useRef(frameKey);
  currentFrameKey.current = frameKey;
  const bridgeDisposer = useRef<(() => void) | null>(null);
  useEffect(() => () => { bridgeDisposer.current?.(); bridgeDisposer.current = null; }, [frameKey]);

  // A frame-local bridge avoids shared session/token state. Panel modules listen
  // for this event or read the context immediately if their import finishes later.
  const attachPanelContext = (frame: HTMLIFrameElement) => {
    bridgeDisposer.current?.();
    const target = frame.contentWindow as PanelWindow | null;
    if (!target) return;
    let active = true;
    const pending = new Set<AbortController>();
    const assertCurrent = () => {
      if (!active || !frame.isConnected || frameRef.current !== frame
          || currentFrameKey.current !== frameKey || !adminToken
          || useSettingsStore.getState().adminToken !== adminToken) {
        throw new Error('面板会话已失效，请重新打开。');
      }
    };
    const context: PanelContext = {
      sessionId,
      get viewportWidth() { assertCurrent(); return window.innerWidth; },
      async request(path, init) {
        assertCurrent();
        const controller = new AbortController();
        const abort = () => controller.abort();
        if (init?.signal?.aborted) abort();
        init?.signal?.addEventListener('abort', abort, { once: true });
        pending.add(controller);
        try {
          const headers = Object.fromEntries(new Headers(init?.headers));
          delete headers.authorization;
          headers.Authorization = `Bearer ${adminToken}`;
          const result = await pluginApiRequest(path, { ...init, headers, signal: controller.signal });
          assertCurrent();
          return result;
        } finally {
          pending.delete(controller);
          init?.signal?.removeEventListener('abort', abort);
        }
      },
      async call(name, ...args) {
        assertCurrent();
        return callHostAction(name, ...args);
      },
      close() { assertCurrent(); bridgeDisposer.current?.(); onClose(); },
    };
    target.__CLONOTH_PANEL__ = context;
    bridgeDisposer.current = () => {
      active = false;
      for (const controller of pending) controller.abort();
      if (target.__CLONOTH_PANEL__ === context) delete target.__CLONOTH_PANEL__;
    };
    applyHostTheme(frame.contentDocument);
    target.dispatchEvent(new CustomEvent('clonoth:panel-context'));
  };

  // [2026-09-12] Panel session lifecycle: request a short-lived cookie before the
  // frame mounts and renew it at half the advertised lifetime. Logout clears the
  // token, which aborts this effect, so renewal stops and the cookie is revoked
  // by the settings page.
  useEffect(() => {
    if (!adminToken) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pending = false;

    const renew = async () => {
      if (pending || controller.signal.aborted) return;
      pending = true;
      clearTimeout(timer);
      try {
        const resolved = resolvePanelEntry(entry, window.location.origin);
        if (resolved.error) throw new Error(resolved.error);
        const result = await pluginApiRequest('/admin/auth/panel-session', {
          method: 'POST',
          signal: controller.signal,
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${adminToken}` },
          body: JSON.stringify({ entry: resolved.path }),
        }) as { expires_in?: number } | null;
        if (controller.signal.aborted) return;
        if (!result || typeof result.expires_in !== 'number' || result.expires_in <= 0) {
          throw new Error('面板认证响应无效');
        }
        setAccess({ key: frameKey, ready: true });
        timer = setTimeout(() => void renew(), result.expires_in * 500);
      } catch (error) {
        if (!controller.signal.aborted) {
          // A renewal failure on an already loaded panel must not blank it; only a
          // panel that never became ready reports the error and offers a retry.
          setAccess((previous) => (
            previous && previous.key === frameKey && previous.ready
              ? previous
              : {
                key: frameKey,
                ready: false,
                error: error instanceof Error ? error.message : '面板认证失败',
              }
          ));
        }
      } finally {
        pending = false;
      }
    };

    const onFocus = () => {
      if (!document.hidden) void renew();
    };
    void renew();
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [frameKey, entry, adminToken]);

  useEffect(() => {
    const w = window as unknown as { __CLONOTH_BOOT__?: unknown };
    // [AutoC 2026-08-28] 引用计数。Why: 设置页编辑器进右栏后，主区列表
    // iframe 与右栏编辑器 iframe 同时存活，两个 PluginPanel 共享同一全局
    // boot 对象；面板页在每次 api() 调用时实时读取它。编辑器卸载时若直接
    // delete，列表页的下一次请求会失去 token 变成 401。How: 挂载 +1、卸载
    // -1，归零才真正清除。Purpose: 并发面板共存期间 boot 始终可用。
    bootRefCount += 1;
    w.__CLONOTH_BOOT__ = {
      // [2026-09-12] Unchanged contract: panels keep using the admin token for
      // their own API calls. The panel cookie only authorizes loading the static
      // page itself.
      token: adminToken || '',
      sessionId,
    };
    return () => {
      bootRefCount = Math.max(0, bootRefCount - 1);
      if (bootRefCount === 0) delete w.__CLONOTH_BOOT__;
    };
  }, [sessionId, adminToken]);

  // inject the host theme after every (re)load of the panel document
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const inject = () => applyHostTheme(frame.contentDocument);
    frame.addEventListener('load', inject);
    // a remounted frame may already be complete before the listener attaches
    if (frame.contentDocument?.readyState === 'complete') inject();
    return () => frame.removeEventListener('load', inject);
  }, [frameKey, panelReady]);

  // [AutoC 2026-08-25] Forward the open intent to the panel page.
  // The intent is opaque to the host; the panel page listens for
  // 'clonoth:panel-intent' and reacts voluntarily. Send on load (cold frame)
  // and on every intent change while the panel is mounted (warm frame) — the
  // overlay stays open across clicks, so the load-only path misses repeat
  // intents.
  const rightIntent = useViewStore((s) => s.panelOverlay.rightIntent);
  const lastSentIntent = useRef<unknown>(undefined);
  useEffect(() => {
    if (!overlayId || !panelReady) return;
    const frame = frameRef.current;
    if (!frame) return;
    const send = () => {
      const intent = useViewStore.getState().panelOverlay.rightIntent;
      if (intent === lastSentIntent.current) return;
      lastSentIntent.current = intent;
      frame.contentWindow?.postMessage({ type: 'clonoth:panel-intent', intent }, window.location.origin);
    };
    frame.addEventListener('load', send);
    if (frame.contentDocument?.readyState === 'complete') send();
    // [AutoC 2026-08-25] Panel-ready handshake: the panel page posts
    // 'clonoth:panel-ready' after its message listener is registered, which
    // may happen after the iframe load event. Resend the intent then so
    // first-click intents are not swallowed by an unready panel.
    const onMessage = (ev: MessageEvent) => {
      if (ev.source !== frame.contentWindow) return;
      if ((ev.data as { type?: string })?.type !== 'clonoth:panel-ready') return;
      lastSentIntent.current = undefined;
      send();
    };
    window.addEventListener('message', onMessage);
    return () => {
      frame.removeEventListener('load', send);
      window.removeEventListener('message', onMessage);
    };
  }, [frameKey, panelReady, overlayId, rightIntent]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {chrome && (
        <div className="flex items-center justify-between border-b border-[var(--duties-border)] px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <Icon name="extension" size={14} />
            <span className="truncate font-mono text-xs font-semibold">{title}</span>
          </div>
          <button
            className="text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)]"
            onClick={onClose}
            title="关闭面板"
            type="button"
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      )}
      {!panelReady && (
        <div className="p-3 text-sm text-[var(--duties-secondary)]" role="status">
          {!adminToken ? '请先登录' : accessError ? (
            <>
              <p>{accessError}</p>
              <button type="button" className="mt-2 underline" onClick={() => setAttempt((n) => n + 1)}>
                重试
              </button>
            </>
          ) : '正在验证面板访问权限…'}
        </div>
      )}
      {panelReady && (
        <iframe
          key={frameKey}
          ref={frameRef}
          src={entry}
          onLoad={(event) => attachPanelContext(event.currentTarget)}
          title={title}
          className="min-h-0 w-full flex-1 border-0 bg-[var(--duties-bg)]"
        />
      )}
    </div>
  );
};
