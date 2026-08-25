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
// Purpose: plugins get a full UI surface with zero frontend build involvement,
// visually indistinguishable from host chrome.
import { useEffect, useMemo, useRef } from 'react';

import { getStoredAdminToken } from '../../api/supervisorClient';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { applyHostTheme } from './themeBridge';

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
  // remount the frame whenever entry or session changes; the effect below
  // re-publishes the boot object before the new page's scripts run.
  const frameKey = useMemo(() => `${entry}::${sessionId}`, [entry, sessionId]);
  const frameRef = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    const w = window as unknown as { __CLONOTH_BOOT__?: unknown };
    w.__CLONOTH_BOOT__ = {
      token: getStoredAdminToken(),
      sessionId,
    };
    return () => {
      delete w.__CLONOTH_BOOT__;
    };
  }, [sessionId]);

  // inject the host theme after every (re)load of the panel document
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const inject = () => applyHostTheme(frame.contentDocument);
    frame.addEventListener('load', inject);
    // a remounted frame may already be complete before the listener attaches
    if (frame.contentDocument?.readyState === 'complete') inject();
    return () => frame.removeEventListener('load', inject);
  }, [frameKey]);

  // [AutoC 2026-08-25] Forward the open intent to the panel page after load.
  // The intent is opaque to the host; the panel page listens for
  // 'clonoth:panel-intent' and reacts voluntarily. Sending on load avoids the
  // race where the overlay opens and the frame is not yet listening.
  useEffect(() => {
    if (!overlayId) return;
    const frame = frameRef.current;
    if (!frame) return;
    const send = () => {
      const intent = useViewStore.getState().panelOverlay.rightIntent;
      frame.contentWindow?.postMessage({ type: 'clonoth:panel-intent', intent }, window.location.origin);
    };
    frame.addEventListener('load', send);
    if (frame.contentDocument?.readyState === 'complete') send();
    return () => frame.removeEventListener('load', send);
  }, [frameKey, overlayId]);

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
      <iframe
        key={frameKey}
        ref={frameRef}
        src={entry}
        title={title}
        className="min-h-0 w-full flex-1 border-0 bg-[var(--duties-bg)]"
      />
    </div>
  );
};
