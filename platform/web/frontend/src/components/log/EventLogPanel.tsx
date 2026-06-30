// [2026-05-31] Right-rail event-log panel for reducer-backed chat sessions.
// Why: Step 2A adds a dedicated place for low-level Supervisor events so the chat
// stream can stay focused on messages. How: read eventLog through selectEventLog,
// render recent rows in a full-height monospace scroller, and keep the viewport at
// the newest row. Purpose: expose realtime reducer input without coupling the main
// message renderer to audit/debug events.
import { useLayoutEffect, useMemo, useRef } from 'react';
import { useShallow } from 'zustand/react/shallow';

import { selectEventLog } from '../../store/eventSelectors';
import { useChatStore } from '../../store/chatStore';
import type { EventLogEntry } from '../../types/message';

interface EventLogPanelProps {
  limit?: number;
  defaultCollapsed?: boolean;
}

function formatLogTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return new Intl.DateTimeFormat('en', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

function summarizePayload(payload: Record<string, unknown>): string {
  const candidates = ['message', 'summary', 'text', 'content', 'tool_name', 'operation', 'target_node_id'];
  for (const key of candidates) {
    const value = payload[key];
    if (typeof value === 'string' && value.trim()) return value.replace(/\s+/g, ' ').trim();
  }

  try {
    const compact = JSON.stringify(payload);
    return compact.length > 120 ? `${compact.slice(0, 120)}…` : compact;
  } catch {
    return '';
  }
}

function formatLogSummary(entry: EventLogEntry): string {
  return entry.summary || summarizePayload(entry.payload) || `seq ${entry.seq}`;
}

export const EventLogPanel = ({ limit = 200, defaultCollapsed: _defaultCollapsed = false }: EventLogPanelProps) => {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const { activeSessionId, eventLog } = useChatStore(
    useShallow((state) => {
      const activeConversation = state.activeConversationId
        ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
        : undefined;
      return {
        activeSessionId: state.viewingChildSessionId || activeConversation?.sessionId || '',
        eventLog: state.eventLog,
      };
    }),
  );
  const logs = useMemo(
    () => (activeSessionId ? selectEventLog({ eventLog } as Parameters<typeof selectEventLog>[0], activeSessionId, limit) : []),
    [activeSessionId, eventLog, limit],
  );

  const latestLogId = logs[logs.length - 1]?.id || '';

  useLayoutEffect(() => {
    const node = scrollContainerRef.current;
    if (!node) return;

    // Why: the right event log is a debug tail and should always follow the newest
    // event. Smooth scroll can lag behind during dense streams, and scrollIntoView on
    // a zero-height sentinel is not reliable inside the split right rail. How: write
    // scrollTop directly after layout, then repeat once in rAF for late row wrapping.
    // Purpose: the log stays pinned to the bottom while events are arriving.
    const scrollToBottom = () => {
      node.scrollTop = node.scrollHeight;
    };
    scrollToBottom();
    const frame = window.requestAnimationFrame(scrollToBottom);
    return () => window.cancelAnimationFrame(frame);
  }, [activeSessionId, latestLogId, logs.length]);

  return (
    <section
      className="flex h-full min-h-0 flex-col bg-[var(--duties-panel)]"
      // Why: AppLayout already labels the containing right rail as the event log panel.
      // How: give this inner section a distinct accessible name. Purpose: screen-reader
      // and test queries see one panel landmark instead of two identical labels.
      aria-label="事件日志内容"
    >
      <div className="flex flex-shrink-0 items-center justify-between border-b border-[var(--duties-border)] px-3 py-1.5">
        <div className="min-w-0 font-mono text-[0.65rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">
          事件日志{activeSessionId ? ` · ${activeSessionId.slice(0, 8)}` : ''}
        </div>
      </div>
      <div ref={scrollContainerRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono text-[0.68rem] leading-5 text-[var(--duties-secondary)]">
        {logs.length === 0 ? (
          <div className="text-[var(--duties-tertiary)]">当前会话暂无事件。</div>
        ) : (
          logs.map((entry) => (
            <div className="whitespace-pre-wrap break-words" key={entry.id}>
              <span className="text-[var(--duties-tertiary)]">[{formatLogTime(entry.ts)}]</span>{' '}
              <span className="text-[var(--duties-text)]">[{entry.type}]</span>{' '}
              <span>{formatLogSummary(entry)}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
};
