// [AutoC 2026-08-22] Plugin panel host: iframe container for plugin-declared panels.
// Why: plugin panels are self-contained HTML pages served by the plugin itself;
// the host only provides the frame, a title bar, and a same-origin boot object.
// How: set window.__CLONOTH_BOOT__ (token + sessionId) before mount so the panel
// page reads it via window.parent — the token never appears in a URL; remount
// the iframe per session so stale boot data cannot leak across sessions.
// Purpose: plugins get a full UI surface with zero frontend build involvement.
import { useEffect, useMemo } from 'react';

import { getStoredAdminToken } from '../../api/supervisorClient';
import { Icon } from '../common';

interface PluginPanelProps {
  entry: string;
  title: string;
  sessionId: string;
  onClose: () => void;
  /** [plugin-admin 2026-08-23] When false, render the bare iframe without the
   * title bar and close button — the settings-view variant where the sidebar
   * already names the tab and closing is navigation, not an overlay. */
  chrome?: boolean;
}

export const PluginPanel = ({ entry, title, sessionId, onClose, chrome = true }: PluginPanelProps) => {
  // remount the frame whenever entry or session changes; the effect below
  // re-publishes the boot object before the new page's scripts run.
  const frameKey = useMemo(() => `${entry}::${sessionId}`, [entry, sessionId]);

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
        src={entry}
        title={title}
        className="min-h-0 w-full flex-1 border-0 bg-white"
      />
    </div>
  );
};
