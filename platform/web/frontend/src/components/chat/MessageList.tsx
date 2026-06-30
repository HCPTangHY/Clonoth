// [2026-05-31] MessageList renders normalized WsMessage objects.
// [2026-06-04] Scroll fixes: track user scroll position, only auto-scroll when
// user is near bottom. Suppress scroll during initial history hydration.
// [2026-06-07] Virtual pagination: initially render only the last page of
// render items. Scrolling to the top loads earlier pages incrementally.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { ToolExecution, WsMessage } from '../../types/message';
import { useChatStore } from '../../store';
import { MessageCard } from './MessageCard';
import { WorkingGroup } from './WorkingGroup';

interface MessageListProps {
  messages: WsMessage[];
  toolsById: Record<string, ToolExecution>;
}

// How far from the bottom (in px) the user can be and still count as "at bottom"
const SCROLL_THRESHOLD = 120;
// How close to the top (in px) the user must scroll to trigger loading more history from backend
const LOAD_MORE_THRESHOLD = 200;

type RenderItem =
  | { type: 'message'; message: WsMessage }
  | { type: 'working-group'; cards: WsMessage[]; defaultExpanded: boolean };

const ACTIVE_WORKING_STATUSES = new Set<WsMessage['status']>(['pending', 'streaming', 'running_tools', 'awaiting_approval']);

function isActiveWorkingStatus(status: WsMessage['status']): boolean {
  return ACTIVE_WORKING_STATUSES.has(status);
}

function isStandaloneAssistantCompletion(message: WsMessage): boolean {
  return message.role === 'assistant'
    && (message.completionType === 'finish' || message.completionType === 'reply' || message.completionType === 'ask');
}

function buildRenderItems(messages: WsMessage[]): RenderItem[] {
  const renderItems: RenderItem[] = [];
  let currentWorkGroup: WsMessage[] = [];

  const flushWorkGroup = (includesFinish: boolean) => {
    if (currentWorkGroup.length === 0) return;
    const cards = currentWorkGroup;
    const lastCard = cards[cards.length - 1];
    const active = !includesFinish && isActiveWorkingStatus(lastCard.status);
    renderItems.push({ type: 'working-group', cards, defaultExpanded: active });
    currentWorkGroup = [];
  };

  for (const message of messages) {
    if (message.role === 'assistant' && !isStandaloneAssistantCompletion(message)) {
      currentWorkGroup.push(message);
      continue;
    }

    if (message.role === 'assistant' && message.completionType === 'finish') {
      currentWorkGroup.push(message);
      flushWorkGroup(true);
      continue;
    }

    if (message.role === 'assistant' && (message.completionType === 'reply' || message.completionType === 'ask')) {
      currentWorkGroup.push(message);
      flushWorkGroup(false);
      continue;
    }

    flushWorkGroup(false);
    renderItems.push({ type: 'message', message });
  }

  flushWorkGroup(false);

  // Only the LAST working group defaults to expanded.
  let lastGroupSeen = false;
  for (let i = renderItems.length - 1; i >= 0; i--) {
    if (renderItems[i].type === 'working-group') {
      if (!lastGroupSeen) {
        lastGroupSeen = true;
        (renderItems[i] as Extract<RenderItem, { type: 'working-group' }>).defaultExpanded = true;
      } else {
        (renderItems[i] as Extract<RenderItem, { type: 'working-group' }>).defaultExpanded = false;
      }
    }
  }

  return renderItems;
}

function getAdjacentMessageRole(renderItems: RenderItem[], index: number, offset: -1 | 1): WsMessage['role'] | undefined {
  const adjacentItem = renderItems[index + offset];
  return adjacentItem?.type === 'message' ? adjacentItem.message.role : undefined;
}

function getWorkingGroupKey(item: Extract<RenderItem, { type: 'working-group' }>): string {
  const firstCard = item.cards[0];
  const activeMarker = item.defaultExpanded ? 'active' : 'done';
  return `working-group:${firstCard?.id ?? 'empty'}:${activeMarker}`;
}

function getScrollSignature(messages: WsMessage[], toolsById: Record<string, ToolExecution>): string {
  // Only compute signature from the last few messages to avoid O(n) on every render
  const tail = messages.slice(-10);
  const messagePart = tail.map((message) => `${message.id}:${message.updatedAt}:${message.status}:${message.blocks.length}`).join('|');
  const toolKeys = Object.keys(toolsById);
  const toolTail = toolKeys.slice(-10);
  const toolPart = toolTail.map((key) => {
    const tool = toolsById[key];
    return `${tool.stableId}:${tool.updatedAt}:${tool.status}`;
  }).join('|');
  return `${messages.length}:${messagePart}::${toolKeys.length}:${toolPart}`;
}

export const MessageList = ({ messages, toolsById }: MessageListProps) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const paddingRef = useRef<HTMLDivElement>(null);
  const [isUserNearBottom, setIsUserNearBottom] = useState(false);
  const [hasInitialized, setHasInitialized] = useState(false);
  const prevMessageCountRef = useRef(0);
  const lastScrolledUserIdRef = useRef<string | null>(null);

  // [AutoC 2026-06-15] Dynamic padding: when a new user message appears,
  // insert a large padding at the bottom so the user message sits at the
  // viewport top. As assistant content streams in, recalculate padding to
  // shrink by the same amount. scrollToBottom(instant) runs every frame,
  // keeping the user message visually pinned. Once padding = 0, normal
  // auto-follow resumes.
  const anchorUserIdRef = useRef<string | null>(null);
  const anchorPaddingRef = useRef(0);

  // [AutoC 2026-06-15] Why: switching sessions reuses the same component
  // instance (no key remount). hasInitialized stays true from the previous
  // session, so the "first render → scroll to bottom" path is skipped.
  // How: track the first message id; when it changes the message set is
  // from a different conversation. Reset init state so the next effect
  // treats it as a fresh load. Purpose: entering a session always scrolls
  // to the absolute bottom.
  const firstMessageId = messages[0]?.id ?? null;
  const prevFirstMessageIdRef = useRef<string | null>(firstMessageId);
  if (firstMessageId !== prevFirstMessageIdRef.current) {
    prevFirstMessageIdRef.current = firstMessageId;
    if (hasInitialized) {
      setHasInitialized(false);
      prevMessageCountRef.current = 0;
      lastScrolledUserIdRef.current = null;
      anchorUserIdRef.current = null;
      anchorPaddingRef.current = 0;
      if (paddingRef.current) paddingRef.current.style.height = '0px';
    }
  }

  // [2026-06-10] Render ALL items directly. WorkingGroup already folds completed
  // assistant work into collapsed headers, so DOM count stays manageable.
  // The previous virtual-pagination slice caused a window-drift bug where the
  // bottom of the rendered list no longer corresponded to the latest messages.
  const renderItems = useMemo(() => buildRenderItems(messages), [messages]);

  const scrollSignature = useMemo(() => getScrollSignature(messages, toolsById), [messages, toolsById]);

  // Track scroll position + load earlier history from backend on scroll-to-top
  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsUserNearBottom(distanceFromBottom <= SCROLL_THRESHOLD);
    if (!hasInitialized) setHasInitialized(true);

    // Fetch earlier pages from backend when scrolling near the top
    if (el.scrollTop < LOAD_MORE_THRESHOLD) {
      const { historyFullyLoaded, isLoadingMoreHistory, loadMoreHistory } = useChatStore.getState();
      if (!historyFullyLoaded && !isLoadingMoreHistory) {
        const prevScrollHeight = el.scrollHeight;
        void loadMoreHistory().then(() => {
          requestAnimationFrame(() => {
            const newScrollHeight = el.scrollHeight;
            if (newScrollHeight > prevScrollHeight) {
              el.scrollTop += newScrollHeight - prevScrollHeight;
            }
          });
        });
      }
    }
  }, [hasInitialized]);

  // Find the last user message for scroll-to-user behavior
  const lastUserMessageId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'user') return messages[i].id;
    }
    return null;
  }, [messages]);

  // Auto-scroll logic
  useEffect(() => {
    const currentCount = messages.length;
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = currentCount;

    // First render with messages = history hydration → scroll to bottom
    if (!hasInitialized && prevCount === 0 && currentCount > 0) {
      setHasInitialized(true);
      setIsUserNearBottom(true);
      lastScrolledUserIdRef.current = lastUserMessageId;
      bottomRef.current?.scrollIntoView({ behavior: 'instant' });
      return;
    }

    // [AutoC 2026-06-15] New user message → activate dynamic padding anchor.
    // Set padding so the user message sits at the viewport top. The padding
    // fills the space between the user message bottom and the container bottom,
    // not the full viewport height. This prevents over-scrolling past the
    // user message. Subsequent streaming updates shrink the padding as content
    // fills in below.
    if (lastUserMessageId && lastUserMessageId !== lastScrolledUserIdRef.current) {
      lastScrolledUserIdRef.current = lastUserMessageId;
      // Only activate for genuinely new messages (not hydration)
      if (prevCount > 0) {
        const container = containerRef.current;
        if (container) {
          anchorUserIdRef.current = lastUserMessageId;
          setIsUserNearBottom(true);
          // Use double-rAF so React commits the new user message DOM first,
          // then we measure and set padding based on actual element position.
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              const userEl = container.querySelector<HTMLElement>(
                `[data-message-id="${lastUserMessageId}"]`,
              );
              if (userEl && contentRef.current) {
                const viewportH = container.clientHeight;
                const userBottom = userEl.offsetTop + userEl.offsetHeight;
                const contentH = contentRef.current.scrollHeight;
                // Padding = viewport minus the content that appears below the
                // user message's top edge (including the message itself).
                const contentBelowUserTop = contentH - userEl.offsetTop;
                const padding = Math.max(0, viewportH - contentBelowUserTop);
                anchorPaddingRef.current = padding;
                if (paddingRef.current) paddingRef.current.style.height = `${padding}px`;
                // Scroll the user message to the top smoothly.
                userEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
              } else {
                // Fallback: just scroll to bottom
                bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
              }
            });
          });
        }
        return;
      }
    }

    // [AutoC 2026-06-15] Dynamic padding recalculation: measure the height
    // of content from the anchored user message to the end of the content
    // area. Reduce padding so that: padding + contentBelowUser = viewportH.
    // Once content fills the viewport, padding = 0 and anchor is cleared.
    if (anchorUserIdRef.current) {
      const container = containerRef.current;
      const contentEl = contentRef.current;
      const userEl = container?.querySelector<HTMLElement>(
        `[data-message-id="${anchorUserIdRef.current}"]`,
      );
      if (container && contentEl && userEl) {
        const viewportH = container.clientHeight;
        const userTop = userEl.offsetTop;
        // Content height from user message top to end of content (excluding padding)
        const contentH = contentEl.scrollHeight;
        const contentBelowUser = contentH - userTop;
        const newPadding = Math.max(0, viewportH - contentBelowUser);

        if (newPadding !== anchorPaddingRef.current) {
          anchorPaddingRef.current = newPadding;
          if (paddingRef.current) paddingRef.current.style.height = `${newPadding}px`;
        }

        if (newPadding === 0) {
          // Content has filled the viewport. Clear anchor, resume normal auto-follow.
          anchorUserIdRef.current = null;
          setIsUserNearBottom(true);
        }
      }
      // Always scroll to bottom during anchor phase
      bottomRef.current?.scrollIntoView({ behavior: 'instant' });
      return;
    }

    // New message added and user is near bottom → scroll
    if (isUserNearBottom && currentCount > prevCount) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
      return;
    }

    // Streaming updates → only scroll if near bottom
    if (isUserNearBottom) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollSignature]);

  const scrollToBottom = useCallback(() => {
    // Clear any active anchor so we don't fight the padding logic
    anchorUserIdRef.current = null;
    anchorPaddingRef.current = 0;
    if (paddingRef.current) paddingRef.current.style.height = '0px';
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    setIsUserNearBottom(true);
  }, []);

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-[var(--duties-tertiary)]">
        请选择或创建一个对话。
      </div>
    );
  }

  return (
    <div ref={containerRef} className="relative h-full overflow-y-auto" onScroll={handleScroll}>
      <div className="mx-auto max-w-3xl flex flex-col" style={{ minHeight: '100%' }}>
        <div ref={contentRef}>
          {!useChatStore.getState().historyFullyLoaded && (
            <div
              className="flex items-center justify-center py-3 text-xs text-[var(--duties-tertiary)] cursor-pointer select-none"
              onClick={() => void useChatStore.getState().loadMoreHistory()}
            >
              {useChatStore.getState().isLoadingMoreHistory
                ? '加载中…'
                : '↑ 加载更早的消息'}
            </div>
          )}
          {renderItems.map((item, index) => {
            if (item.type === 'working-group') {
              return (
                <WorkingGroup
                  key={getWorkingGroupKey(item)}
                  cards={item.cards}
                  toolsById={toolsById}
                  defaultExpanded={item.defaultExpanded}
                />
              );
            }

            const prevRole = getAdjacentMessageRole(renderItems, index, -1);
            const nextRole = getAdjacentMessageRole(renderItems, index, 1);

            return (
              <MessageCard
                key={item.message.id}
                message={item.message}
                toolsById={toolsById}
                prevRole={prevRole}
                nextRole={nextRole}
              />
            );
          })}
        </div>
        {/* Dynamic padding: shrinks as assistant content grows below the
         * anchored user message. When no anchor is active, height = 0. */}
        <div ref={paddingRef} style={{ height: 0 }} />
        <div className="flex-grow" />
        <div ref={bottomRef} />
      </div>
      {/* Floating scroll-to-bottom button */}
      {hasInitialized && !isUserNearBottom && (
        <button
          type="button"
          className="sticky bottom-4 left-1/2 z-10 -translate-x-1/2 inline-flex items-center gap-1.5 rounded-full bg-[var(--duties-panel)] px-3 py-1.5 font-mono text-[0.65rem] text-[var(--duties-secondary)] shadow-md border border-[var(--duties-border)] transition-colors hover:text-[var(--duties-text)] hover:border-[var(--duties-text)]"
          onClick={scrollToBottom}
        >
          ↓ 回到最新
        </button>
      )}
    </div>
  );
};

export type { MessageListProps };
