// [2026-06-06] Manual context compaction popover.
// Why: the compact context indicator should expose details and a manual compression
// action without leaving the chat input. How: render a small anchored popover that
// reads the current token snapshot, lets users choose keep_recent, and calls the
// typed supervisor API wrapper. Purpose: manual compaction stays a normal inbound
// request while the frontend gives clear progress and completion feedback.
import { type ChangeEvent, useEffect, useRef, useState } from 'react';

import { compactSession, resetConversation } from '../../api/supervisorClient';
import type { ContextUsageState } from '../../store/chatStore';

interface ContextPopoverProps {
  sessionId: string;
  conversationKey?: string;
  usage: ContextUsageState;
  onClose: () => void;
}

type ToastKind = 'success' | 'error';

interface ToastState {
  kind: ToastKind;
  text: string;
}

const KEEP_RECENT_MIN = 1;
const KEEP_RECENT_MAX = 20;
const DEFAULT_KEEP_RECENT = 6;

const tokenFormatter = new Intl.NumberFormat('en-US');

const clampKeepRecent = (value: number): number => {
  // [2026-06-06] Why: keep_recent is a bounded UI control and should not send
  // accidental large or invalid values. How: round numeric input and clamp it to
  // the requested 1..20 range. Purpose: buttons, typing, and paste share one guard.
  if (!Number.isFinite(value)) return DEFAULT_KEEP_RECENT;
  return Math.min(KEEP_RECENT_MAX, Math.max(KEEP_RECENT_MIN, Math.round(value)));
};

const formatFullTokens = (tokens: number): string => tokenFormatter.format(Math.max(0, Math.round(tokens)));

const progressColorClass = (utilization: number): string => {
  // [2026-06-06] Why: the popover progress bar should match the compact toolbar
  // warning thresholds. How: reuse gray, yellow, and red visual states by ratio.
  // Purpose: manual controls and the inline indicator communicate pressure alike.
  if (utilization >= 0.75) return 'bg-red-500';
  if (utilization >= 0.5) return 'bg-yellow-500';
  return 'bg-[var(--duties-tertiary)]';
};

export const ContextPopover = ({ sessionId, conversationKey, usage, onClose }: ContextPopoverProps) => {
  const popoverRef = useRef<HTMLDivElement>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [keepRecent, setKeepRecent] = useState(DEFAULT_KEEP_RECENT);
  const [isCompacting, setIsCompacting] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);

  const utilizationPercent = Math.min(100, Math.max(0, Math.round(usage.utilization * 100)));
  const usageText = `${formatFullTokens(usage.effectiveTokens)} / ${formatFullTokens(usage.compactThreshold)} tokens (${utilizationPercent}%)`;
  // [AutoC 2026-08-24] 缓存命中率：有数据时追加在用量文本后
  const cacheHitRateText = usage.cacheHitRate !== null && usage.cacheHitRate !== undefined
    ? `缓存率 ${(usage.cacheHitRate * 100).toFixed(1)}%`
    : null;

  useEffect(() => {
    const handleOutsidePress = (event: MouseEvent | TouchEvent) => {
      // [2026-06-06] Why: the popover should behave like a real floating panel.
      // How: close only when a document press starts outside this panel; the chat
      // input wrapper stops trigger events before they reach this listener. Purpose:
      // users can dismiss the panel without changing composer focus behavior.
      const target = event.target;
      if (target instanceof Node && popoverRef.current?.contains(target)) return;
      onClose();
    };

    document.addEventListener('mousedown', handleOutsidePress);
    document.addEventListener('touchstart', handleOutsidePress);
    return () => {
      document.removeEventListener('mousedown', handleOutsidePress);
      document.removeEventListener('touchstart', handleOutsidePress);
    };
  }, [onClose]);

  useEffect(() => () => {
    // [2026-06-06] Why: toast timers can outlive the popover if the user clicks
    // outside immediately. How: clear the pending timeout during unmount. Purpose:
    // avoid setting state after the popover has been removed.
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
  }, []);

  const showToast = (kind: ToastKind, text: string) => {
    // [2026-06-06] Why: this frontend has no global toast provider. How: keep a
    // small self-contained fixed status message beside the popover. Purpose: users
    // still receive success and failure feedback from the compaction action.
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast({ kind, text });
    toastTimerRef.current = setTimeout(() => setToast(null), 3000);
  };

  const updateKeepRecentFromInput = (event: ChangeEvent<HTMLInputElement>) => {
    setKeepRecent(clampKeepRecent(Number(event.target.value)));
  };

  const adjustKeepRecent = (delta: number) => {
    setKeepRecent((current) => clampKeepRecent(current + delta));
  };

  const handleCompact = async () => {
    // [2026-06-06] Why: manual compaction is asynchronous and only enqueues work on
    // the supervisor. How: disable duplicate clicks during the request and surface
    // the enqueue result through a toast. Purpose: the UI remains deterministic even
    // when the engine performs compression later.
    if (!sessionId || isCompacting) return;
    setIsCompacting(true);
    try {
      await compactSession(sessionId, keepRecent);
      showToast('success', '已提交上下文压缩请求。');
    } catch (error) {
      const detail = error instanceof Error ? error.message : '未知错误';
      showToast('error', `上下文压缩请求失败：${detail}`);
    } finally {
      setIsCompacting(false);
    }
  };

  return (
    <>
      <div
        aria-label="上下文压缩设置"
        className="absolute bottom-full right-0 z-40 mb-2 w-[220px] rounded border border-[var(--duties-border)] bg-[var(--duties-panel)] p-2 text-[var(--duties-text)] shadow-lg"
        ref={popoverRef}
        role="dialog"
      >
        <p className="font-mono text-xs text-[var(--duties-text)]">{usageText}</p>
        {cacheHitRateText && (
          <p className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]">{cacheHitRateText}</p>
        )}
        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[var(--duties-border)]" aria-hidden="true">
          <div
            className={`h-full rounded-full transition-all ${progressColorClass(usage.utilization)}`}
            style={{ width: `${utilizationPercent}%` }}
          />
        </div>

        <div className="mt-2 flex items-center gap-1.5">
          <span className="text-[0.65rem] text-[var(--duties-secondary)]">保留</span>
          <button
            aria-label="减少"
            className="inline-flex h-6 w-6 items-center justify-center border border-[var(--duties-border)] font-mono text-xs text-[var(--duties-secondary)] hover:text-[var(--duties-text)] disabled:opacity-40"
            disabled={keepRecent <= KEEP_RECENT_MIN || isCompacting}
            onClick={() => adjustKeepRecent(-1)}
            type="button"
          >−</button>
          <input
            aria-label="保留轮数"
            className="h-6 w-10 border border-[var(--duties-border)] bg-[var(--duties-bg)] text-center font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] disabled:opacity-50"
            disabled={isCompacting}
            id="context-keep-recent-input"
            max={KEEP_RECENT_MAX}
            min={KEEP_RECENT_MIN}
            onChange={updateKeepRecentFromInput}
            type="number"
            value={keepRecent}
          />
          <button
            aria-label="增加"
            className="inline-flex h-6 w-6 items-center justify-center border border-[var(--duties-border)] font-mono text-xs text-[var(--duties-secondary)] hover:text-[var(--duties-text)] disabled:opacity-40"
            disabled={keepRecent >= KEEP_RECENT_MAX || isCompacting}
            onClick={() => adjustKeepRecent(1)}
            type="button"
          >+</button>
          <span className="text-[0.65rem] text-[var(--duties-secondary)]">轮</span>
          <button
            className="ml-auto inline-flex h-6 items-center justify-center border border-[var(--duties-text)] bg-[var(--duties-text)] px-2 font-mono text-[0.65rem] text-[var(--duties-bg)] disabled:opacity-50"
            disabled={!sessionId || isCompacting}
            onClick={handleCompact}
            type="button"
          >{isCompacting ? '压缩中' : '压缩'}</button>
        </div>
        {conversationKey && (
          <button
            className="mt-2 flex w-full items-center justify-center gap-1 border border-[var(--duties-border)] py-1 font-mono text-[0.65rem] text-[var(--duties-secondary)] transition-colors hover:border-red-400 hover:text-red-500"
            onClick={async () => {
              try {
                await resetConversation(conversationKey);
                showToast('success', '对话已重置，下条消息将创建新会话。');
              } catch {
                showToast('error', '重置失败。');
              }
            }}
            title="重置对话（清空上下文，下条消息开新会话）"
            type="button"
          >🗑 重置对话</button>
        )}
      </div>

      {toast && (
        <div
          className={`fixed bottom-4 right-4 z-50 rounded-md border px-3 py-2 text-xs shadow-lg ${toast.kind === 'success' ? 'border-[var(--duties-border)] bg-[var(--duties-panel)] text-[var(--duties-text)]' : 'border-red-300 bg-red-50 text-red-700'}`}
          role="status"
        >
          {toast.text}
        </div>
      )}
    </>
  );
};
