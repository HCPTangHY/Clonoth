// [2026-06-05] WorkingGroup folds adjacent assistant work cards at render time.
// Why: thinking, tool, and free-prose cards are useful trace output but should not
// crowd completed conversations. How: keep the original WsMessage cards unchanged,
// show one compact disclosure row styled identically to a MessageCard header, and
// render MessageCard children only when expanded. The finish card's thinking/tools
// are inside the fold, while its final text is rendered outside as the visible result.
// Purpose: the chat store and reducer remain untouched while the UI gains a scannable
// work layer that seamlessly merges with the finish result.
import { useEffect, useMemo, useState } from 'react';

import type { TextBlock, ToolExecution, WsMessage } from '../../types/message';
import { Icon } from '../common';
import { MessageCard, MessageCopyButton, MessageMetaInfo } from './MessageCard';
import { AttachmentList } from './AttachmentList';
import { RenderBlockView } from './RenderBlockView';
import { BLOCK_STACK_CLASS } from './renderingConstants';

interface WorkingGroupProps {
  cards: WsMessage[];
  toolsById: Record<string, ToolExecution>;
  defaultExpanded?: boolean;
}

function parseTimestamp(value: string): number | null {
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
}

function formatElapsed(milliseconds: number): string {
  const seconds = Math.max(0, milliseconds / 1000);
  return `${seconds.toFixed(1)}s`;
}

function getElapsedLabel(cards: WsMessage[], active: boolean, now: number): string {
  const firstCard = cards[0];
  const lastCard = cards[cards.length - 1];
  if (!firstCard || !lastCard) return '0.0s';

  const start = parseTimestamp(firstCard.createdAt);
  if (start === null) return '0.0s';

  // Multiple cards: card-level timestamps already span the full duration.
  if (cards.length > 1) {
    const staticEnd = parseTimestamp(lastCard.updatedAt);
    const end = active ? now : (staticEnd ?? start);
    return formatElapsed(end - start);
  }

  // Single card: createdAt === updatedAt is common, so scan thinking blocks
  // for more precise startedAt/endedAt timing.
  let earliest = start;
  let latest: number | null = parseTimestamp(lastCard.updatedAt);
  for (const block of firstCard.blocks) {
    if (block.kind === 'thinking') {
      const tb = block as { startedAt?: string; endedAt?: string };
      const s = tb.startedAt ? parseTimestamp(tb.startedAt) : null;
      const e = tb.endedAt ? parseTimestamp(tb.endedAt) : null;
      if (s !== null && s < earliest) earliest = s;
      if (e !== null && (latest === null || e > latest)) latest = e;
    }
  }
  const end = active ? now : (latest ?? earliest);
  return formatElapsed(end - earliest);
}

function useWorkingGroupElapsed(cards: WsMessage[], active: boolean): string {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  return getElapsedLabel(cards, active, now);
}

export const WorkingGroup = ({ cards, toolsById, defaultExpanded = false }: WorkingGroupProps) => {
  const [expanded, setExpanded] = useState(defaultExpanded);

  // Detect if the last card is a completion card (finish/reply/ask) — its text
  // blocks render outside the fold, while its thinking/tool blocks stay inside.
  const lastCard = cards[cards.length - 1];
  const hasCompletion = lastCard?.completionType === 'finish'
    || lastCard?.completionType === 'reply'
    || lastCard?.completionType === 'ask';

  // Active = last card is still streaming/generating, NOT just "is this the last group".
  // Once all cards have a terminal completion type, the timer should stop.
  const isActive = !hasCompletion && lastCard?.status !== 'completed' && lastCard?.status !== 'failed';
  const elapsed = useWorkingGroupElapsed(cards, isActive);

  // Split completion card: thinking/tool blocks go in fold, text blocks go outside
  const completionTextBlocks = useMemo(() => {
    if (!hasCompletion || !lastCard) return [];
    return lastCard.blocks.filter((b) => b.kind === 'text');
  }, [hasCompletion, lastCard]);

  const completionContext = useMemo(() => {
    if (!hasCompletion || !lastCard) return undefined;
    return {
      role: lastCard.role,
      status: lastCard.status,
      completionType: lastCard.completionType,
      sessionId: lastCard.sessionId || undefined,
    };
  }, [hasCompletion, lastCard]);

  // Work cards = all cards except the completion card (which is split)
  const workCards = hasCompletion ? cards.slice(0, -1) : cards;

  if (cards.length === 0) return null;

  return (
    <>
      {/* Working header — same structure as MessageCard article */}
      <article className={`group/card ${expanded ? '' : (hasCompletion ? '' : 'border-b border-[var(--duties-border)]')} bg-[var(--duties-panel)] px-3 pt-3 pb-2 sm:px-4`}>
        <div className="mx-auto max-w-3xl">
          <header className="mb-1.5 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[0.6rem] font-semibold uppercase tracking-[0.18em] text-blue-600">
              助手
            </span>
            <time className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]" dateTime={cards[0]?.createdAt}>
              {cards[0]?.createdAt ? new Date(cards[0].createdAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}
            </time>
            <span className={`font-mono text-[0.55rem] ${isActive ? 'text-blue-500' : 'text-[var(--duties-tertiary)]'}`}>
              {isActive ? '输出中' : '已完成'}
            </span>
            {isActive && <span aria-label="工作进行中" className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />}
            {hasCompletion && lastCard && completionTextBlocks.length > 0 && (
              <div className="msg-footer-row ml-auto">
                <MessageCopyButton
                  text={completionTextBlocks.map((b) => (b as TextBlock).text).join('\n')}
                />
                <MessageMetaInfo source={lastCard.source} />
              </div>
            )}
          </header>
          <button
            type="button"
            className="inline-flex items-center gap-2 font-mono text-[0.6rem] text-[var(--duties-secondary)] transition-colors hover:text-[var(--duties-text)]"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            <span className={`transition-transform ${expanded ? 'rotate-90' : ''}`}>
              <Icon name="chevron_right" size={13} />
            </span>
            <Icon name="engineering" size={13} />
            <span className="font-semibold uppercase tracking-[0.18em]">Working</span>
            <span className="text-[var(--duties-tertiary)]">{elapsed}</span>
          </button>
        </div>
      </article>

      {/* Expanded: render all work cards + finish's thinking/tool blocks */}
      {expanded && (
        <>
          {workCards.map((message, index) => {
            const prevRole = index === 0 ? 'assistant' : workCards[index - 1]?.role;
            const nextRole = workCards[index + 1]?.role ?? (hasCompletion ? 'assistant' : undefined);

            return (
              <MessageCard
                key={message.id}
                message={message}
                toolsById={toolsById}
                prevRole={prevRole}
                nextRole={nextRole}
              />
            );
          })}
          {/* Finish card's thinking + tool blocks inside the fold */}
          {hasCompletion && lastCard && lastCard.blocks.some((b) => b.kind !== 'text') && (
            <article className="bg-[var(--duties-panel)] px-3 pt-0 pb-2 sm:px-4">
              <div className="mx-auto max-w-3xl">
                <div className={BLOCK_STACK_CLASS}>
                  {lastCard.blocks
                    .filter((b) => b.kind !== 'text')
                    .map((block) => (
                      <RenderBlockView
                        key={block.id}
                        block={block}
                        toolsById={toolsById}
                        messageContext={{
                          role: lastCard.role,
                          status: lastCard.status,
                          completionType: lastCard.completionType,
                          sessionId: lastCard.sessionId || undefined,
                        }}
                      />
                    ))}
                </div>
              </div>
            </article>
          )}
        </>
      )}

      {/* Finish text blocks: always visible, no gap with fold above */}
      {hasCompletion && lastCard && (completionTextBlocks.length > 0 || (lastCard.attachments && lastCard.attachments.length > 0)) && completionContext && (
        <article className="group/card overflow-hidden border-b border-[var(--duties-border)] bg-[var(--duties-panel)] px-3 pt-0 pb-3 sm:px-4">
          <div className="mx-auto max-w-3xl min-w-0">
            <div className={BLOCK_STACK_CLASS}>
              {completionTextBlocks.map((block) => (
                <RenderBlockView
                  key={block.id}
                  block={block}
                  toolsById={toolsById}
                  messageContext={completionContext}
                />
              ))}
            </div>
            {lastCard.attachments && lastCard.attachments.length > 0 && (
              <AttachmentList attachments={lastCard.attachments} sessionId={lastCard.sessionId} />
            )}
          </div>
        </article>
      )}
    </>
  );
};

export type { WorkingGroupProps };
