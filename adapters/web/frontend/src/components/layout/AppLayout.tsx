// [2026-06-01] Three-column slot layout inspired by IdoFront.
// Left, center, and right columns are pure slots; viewRegistry decides what each
// slot contains. Why: settings mode should replace the left and center content
// without adding App-level conditionals or layout-specific overrides. How: make the
// composer optional and split the right column into upper and lower slots only.
// Purpose: AppLayout remains unaware of chat, settings, or any concrete panel type.
// [AutoC 2026-08-24] Resizable side columns. Why: long session lists and the IDE
// file panel both outgrow fixed widths, and different workflows want different
// splits. How: a 6px drag handle on each side boundary (desktop only), pointer
// events with clamped live width updates, final width persisted via settingsStore.
// Purpose: column sizing is user-controlled layout state, not a fixed constant.
import { type PropsWithChildren, type ReactNode, useRef, useState } from 'react';

import { useSettingsStore } from '../../store/settingsStore';
import { Icon } from '../common';

// [AutoC 2026-08-24] Clamp bounds shared with settingsStore.readStoredWidth —
// stored values outside the range are discarded there, live drags stop here.
const LEFT_MIN = 180;
const LEFT_MAX = 420;
const RIGHT_MIN = 220;
const RIGHT_MAX = 640;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

interface AppLayoutProps extends PropsWithChildren {
  sidebar: ReactNode;
  header: ReactNode;
  composer?: ReactNode;
  logPanel?: ReactNode;
  rightPanel?: ReactNode;
  rightOverlay?: ReactNode;
}

export const AppLayout = ({ sidebar, header, composer, logPanel, rightPanel, rightOverlay, children }: AppLayoutProps) => {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { rightPanelOpen, setRightPanelOpen } = useSettingsStore();
  const sidebarWidth = useSettingsStore((s) => s.sidebarWidth);
  const rightPanelWidth = useSettingsStore((s) => s.rightPanelWidth);
  const hasRightPanel = Boolean(logPanel || rightPanel);
  const touchStart = useRef<{ x: number; y: number } | null>(null);

  // [AutoC 2026-08-24] Drag-to-resize. Pointer capture keeps tracking outside
  // the handle; user-select is suppressed for the drag duration so text under
  // the pointer is not highlighted.
  const startDrag = (side: 'left' | 'right') => (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = side === 'left' ? sidebarWidth : rightPanelWidth;
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';

    const onMove = (e: PointerEvent) => {
      const delta = side === 'left' ? e.clientX - startX : startX - e.clientX;
      const next = clamp(
        startWidth + delta,
        side === 'left' ? LEFT_MIN : RIGHT_MIN,
        side === 'left' ? LEFT_MAX : RIGHT_MAX,
      );
      const store = useSettingsStore.getState();
      if (side === 'left') store.setSidebarWidth(next);
      else store.setRightPanelWidth(next);
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

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
        className={`fixed inset-y-0 left-0 z-40 flex-shrink-0 border-r border-[var(--duties-border)] bg-[var(--duties-panel)] transition-transform md:relative md:z-auto md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
        style={{ width: '15rem' }}
        ref={(el) => {
          // desktop width follows the draggable value; mobile keeps the fixed
          // off-canvas width from the class above
          if (el && window.matchMedia('(min-width: 768px)').matches) {
            el.style.width = `${sidebarWidth}px`;
          }
        }}
      >
        {sidebar}
      </aside>

      {/* left drag handle (desktop only) */}
      <div
        aria-hidden="true"
        className="hidden w-1.5 flex-shrink-0 cursor-col-resize bg-transparent transition-colors hover:bg-[var(--duties-accent)] md:block"
        onPointerDown={startDrag('left')}
      />

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

      {/* right drag handle (desktop only, only when the panel is open) */}
      {hasRightPanel && rightPanelOpen && (
        <div
          aria-hidden="true"
          className="hidden w-1.5 flex-shrink-0 cursor-col-resize bg-transparent transition-colors hover:bg-[var(--duties-accent)] md:block"
          onPointerDown={startDrag('right')}
        />
      )}
      {hasRightPanel && (
        <aside
          aria-label="右侧面板"
          className={`flex-shrink-0 flex-col overflow-hidden border-l border-[var(--duties-border)] bg-[var(--duties-panel)] ${
            rightPanelOpen
              ? 'fixed inset-y-0 right-0 z-40 flex translate-x-0 transition-transform duration-200 md:relative md:z-auto md:translate-x-0'
              : 'fixed inset-y-0 right-0 z-40 w-[85vw] translate-x-full transition-transform duration-200 md:relative md:z-auto md:w-0 md:translate-x-0 md:transition-[width] md:duration-200'
          }`}
          style={rightPanelOpen ? { width: `${rightPanelWidth}px` } : undefined}
          ref={(el) => {
            if (!el) return;
            if (window.matchMedia('(min-width: 768px)').matches) {
              el.style.width = rightPanelOpen ? `${rightPanelWidth}px` : '0px';
            } else if (rightPanelOpen) {
              el.style.width = '85vw';
            }
          }}
        >
          {rightOverlay && (
            <div className="absolute inset-0 z-10 flex flex-col overflow-hidden bg-[var(--duties-panel)]">
              {rightOverlay}
            </div>
          )}
          {logPanel ? (
            <>
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
