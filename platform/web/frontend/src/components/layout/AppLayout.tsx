// [2026-06-01] Three-column slot layout inspired by IdoFront.
// Left, center, and right columns are pure slots; viewRegistry decides what each
// slot contains. Why: settings mode should replace the left and center content
// without adding App-level conditionals or layout-specific overrides. How: make the
// composer optional and split the right column into upper and lower slots only.
// Purpose: AppLayout remains unaware of chat, settings, or any concrete panel type.
import { type PropsWithChildren, type ReactNode, useRef, useState } from 'react';

import { useSettingsStore } from '../../store/settingsStore';
import { Icon } from '../common';

interface AppLayoutProps extends PropsWithChildren {
  sidebar: ReactNode;
  header: ReactNode;
  composer?: ReactNode;
  logPanel?: ReactNode;
  rightPanel?: ReactNode;
}

export const AppLayout = ({ sidebar, header, composer, logPanel, rightPanel, children }: AppLayoutProps) => {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { rightPanelOpen, setRightPanelOpen } = useSettingsStore();
  const hasRightPanel = Boolean(logPanel || rightPanel);
  const touchStart = useRef<{ x: number; y: number } | null>(null);

  const handleTouchStart = (event: React.TouchEvent<HTMLDivElement>) => {
    // [2026-06-02] Store only the first touch point for mobile panel gestures. Why:
    // side panels should be accessible by swiping without interfering with normal
    // content rendering. How: capture the starting x/y coordinates and defer direction
    // checks until touch end. Purpose: AppLayout owns consistent sidebar gestures.
    const touch = event.touches[0];
    touchStart.current = { x: touch.clientX, y: touch.clientY };
  };

  const handleTouchEnd = (event: React.TouchEvent<HTMLDivElement>) => {
    // [2026-06-02] Convert horizontal swipes into panel open/close actions. Why: mobile
    // users need a larger interaction target than the header toggles. How: require a
    // 50px horizontal movement, ignore mostly vertical gestures, and only open hidden
    // panels from a 48px screen edge zone while allowing reverse swipes to close the
    // currently open opposite panel. Purpose: right and left panels can be opened or
    // dismissed with predictable swipes without hijacking normal horizontal content.
    if (!touchStart.current) return;
    const start = touchStart.current;
    const touch = event.changedTouches[0];
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;
    touchStart.current = null;

    if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy)) return;

    const edgeSwipeZone = 48;
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const startedNearLeftEdge = start.x <= edgeSwipeZone;
    const startedNearRightEdge = start.x >= viewportWidth - edgeSwipeZone;

    if (dx > 0) {
      if (rightPanelOpen) setRightPanelOpen(false);
      else if (startedNearLeftEdge) setSidebarOpen(true);
      return;
    }

    if (sidebarOpen) setSidebarOpen(false);
    else if (hasRightPanel && startedNearRightEdge) setRightPanelOpen(true);
  };

  return (
    <div
      className="flex h-[100dvh] min-h-0 bg-[var(--duties-bg)] text-[var(--duties-text)]"
      data-testid="app-layout-root"
      onTouchEnd={handleTouchEnd}
      onTouchStart={handleTouchStart}
    >
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/30 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      {rightPanelOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/30 md:hidden"
          onClick={() => setRightPanelOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 w-[15rem] flex-shrink-0 border-r border-[var(--duties-border)] bg-[var(--duties-panel)] transition-transform md:relative md:z-auto md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {sidebar}
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <div className="flex-shrink-0 border-b border-[var(--duties-border)] bg-[var(--duties-bg)]">
          <div className="flex items-center">
            <button
              className="flex-shrink-0 px-3 py-3 text-lg text-[var(--duties-secondary)] md:hidden"
              onClick={() => setSidebarOpen(!sidebarOpen)}
              type="button"
            >
              {/* [2026-06-01] Why: replace the hamburger Unicode glyph with Material Symbols.
                  How: render the shared Icon with the menu symbol. Purpose: navigation
                  controls use the same icon font as the rest of the frontend. */}
              <Icon name="menu" size={22} />
            </button>
            <div className="min-w-0 flex-1">{header}</div>
            {hasRightPanel && (
              <button
                className="flex-shrink-0 px-3 py-2 font-mono text-[0.6rem] text-[var(--duties-secondary)] transition-colors hover:text-[var(--duties-text)]"
                onClick={() => setRightPanelOpen(!rightPanelOpen)}
                type="button"
                title={rightPanelOpen ? '收起面板' : '展开面板'}
              >
                {/* [2026-06-01] Why: replace triangle toggle glyphs with Material Symbols.
                    How: choose the chevron symbol from rightPanelOpen. Purpose: the
                    right panel toggle follows the shared icon system. */}
                <Icon name={rightPanelOpen ? 'chevron_right' : 'chevron_left'} size={18} />
              </button>
            )}
          </div>
        </div>
        <section className="relative min-h-0 flex-1 overflow-hidden">{children}</section>
        {composer && (
          <div className="flex-shrink-0 border-t border-[var(--duties-border)] bg-[var(--duties-bg)]">{composer}</div>
        )}
      </main>

      {hasRightPanel && (
        <aside
          aria-label="右侧面板"
          className={`flex-shrink-0 flex-col overflow-hidden border-l border-[var(--duties-border)] bg-[var(--duties-panel)] ${
            rightPanelOpen
              ? 'fixed inset-y-0 right-0 z-40 flex w-[85vw] translate-x-0 transition-transform duration-200 md:relative md:z-auto md:w-72 md:translate-x-0 md:transition-[width]'
              : 'fixed inset-y-0 right-0 z-40 w-[85vw] translate-x-full transition-transform duration-200 md:relative md:z-auto md:w-0 md:translate-x-0 md:transition-[width] md:duration-200'
          }`}
        >
          {logPanel ? (
            <>
              {/* [2026-06-02] Preserve the split layout only when a log panel exists.
                  Why: chat mode still needs status plus EventLogPanel. How: keep the
                  historical 60/40 wrappers inside this branch. Purpose: settings mode
                  can omit logPanel without inheriting a stale 60 percent height. */}
              <div className="flex h-[60%] min-h-0 flex-shrink-0 flex-col overflow-hidden border-b border-[var(--duties-border)]">
                {rightPanel}
              </div>
              <div
                aria-label="事件日志面板"
                className="flex h-[40%] min-h-0 flex-shrink-0 flex-col overflow-hidden"
              >
                {logPanel}
              </div>
            </>
          ) : (
            <div className="flex h-full min-h-0 flex-1 flex-col overflow-hidden">
              {rightPanel}
            </div>
          )}
        </aside>
      )}
    </div>
  );
};
