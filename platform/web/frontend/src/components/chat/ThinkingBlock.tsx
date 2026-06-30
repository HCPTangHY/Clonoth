// [2026-05-31] ThinkingBlock renders reasoning blocks in the unified message card.
// Why: reasoning used to live in a separate stream preview and could disappear from
// the final message layout. How: keep reasoning collapsible by default, but open the
// live block while it is streaming so users can see progress. Purpose: make thought
// rendering consistent for historical and active messages.
import { useEffect, useRef, useState } from 'react';

import { useClientPrefsStore } from '../../store/clientPrefsStore';
import type { ThinkingBlock as ThinkingRenderBlock } from '../../types/message';
import { Icon } from '../common';
import { INLINE_BLOCK_BODY_TEXT_CLASS, INLINE_BLOCK_HEADER_TEXT_CLASS, INLINE_BLOCK_INDENT_CLASS } from './renderingConstants';

interface ThinkingBlockProps {
  block: ThinkingRenderBlock;
}

function getPreview(text: string): string {
  const normalized = text.trim();
  if (normalized.length <= 180) return normalized;
  return `${normalized.slice(0, 180)}…`;
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s.toFixed(0)}s`;
}

function useElapsedTime(startedAt?: string, endedAt?: string, streaming?: boolean): string | null {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!streaming || !startedAt) return;
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, [streaming, startedAt]);

  if (!startedAt) return null;
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return null;

  if (endedAt) {
    const end = new Date(endedAt).getTime();
    if (!Number.isNaN(end)) return formatElapsed((end - start) / 1000);
  }

  if (streaming) {
    return formatElapsed((now - start) / 1000);
  }

  return null;
}

function getLastLines(text: string, maxLines = 6): string {
  // Why: streaming text often ends with a trailing newline, causing split('\n') to
  // produce an empty last element. How: filter out empty trailing lines before
  // slicing. Purpose: the collapsed streaming preview always ends on the most recent
  // visible line.
  const lines = text.split('\n');
  while (lines.length > 0 && lines[lines.length - 1].trim() === '') {
    lines.pop();
  }
  if (lines.length === 0) return '';
  if (lines.length <= maxLines) return lines.join('\n');
  return '…\n' + lines.slice(-maxLines).join('\n');
}

export const ThinkingBlock = ({ block }: ThinkingBlockProps) => {
  const defaultCollapsed = useClientPrefsStore(state => state.thinkingDefaultCollapsed);
  const [expanded, setExpanded] = useState(() => !defaultCollapsed && !block.streaming);
  const textLength = block.text.length;
  const hasText = textLength > 0;

  // [2026-06-06] Streaming timeout guard: if block.streaming stays true but no new
  // text arrives for 10 seconds, force streaming to false locally. Why: stream_end
  // or finalizeStreamingBlocks may fail to reach this block (e.g. turnKey mismatch,
  // missing llm_request_id, or backend error). Purpose: the spinner and timer stop
  // instead of running forever.
  const lastTextLenRef = useRef(textLength);
  const lastUpdateRef = useRef(Date.now());
  const [streamingOverride, setStreamingOverride] = useState(false);

  useEffect(() => {
    if (!block.streaming) {
      setStreamingOverride(false);
      return;
    }
    if (textLength !== lastTextLenRef.current) {
      lastTextLenRef.current = textLength;
      lastUpdateRef.current = Date.now();
      setStreamingOverride(false);
    }
    const timer = setInterval(() => {
      if (Date.now() - lastUpdateRef.current > 10_000) {
        setStreamingOverride(true);
        clearInterval(timer);
      }
    }, 2_000);
    return () => clearInterval(timer);
  }, [block.streaming, textLength]);

  const isStreaming = Boolean(block.streaming) && !streamingOverride;
  const elapsed = useElapsedTime(block.startedAt, block.endedAt, isStreaming);

  return (
    <div className={`${INLINE_BLOCK_INDENT_CLASS} border-l-2 border-[var(--duties-border)] pl-3 font-mono ${INLINE_BLOCK_BODY_TEXT_CLASS} text-[var(--duties-secondary)]`}>
      <button
        type="button"
        className={`flex w-full items-center gap-2 text-left ${INLINE_BLOCK_HEADER_TEXT_CLASS} text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)]`}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className={`transition-transform ${expanded ? 'rotate-90' : ''}`}>
          {/* [2026-06-01] Why: thinking disclosure used a triangle glyph.
              How: render chevron_right through Material Symbols and rotate it when
              expanded. Purpose: collapsible controls avoid decorative Unicode. */}
          <Icon name="chevron_right" size={13} />
        </span>
        {block.streaming ? (
          <>
            <span className="inline-block animate-spin text-blue-500">
              {/* [2026-06-01] Why: streaming thinking used a gapped circle arrow glyph.
                  How: render progress_activity through the shared Icon. Purpose:
                  active reasoning status uses the same spinner symbol as tool cards. */}
              <Icon name="progress_activity" size={13} />
            </span>
            <span>思考中{elapsed ? ` ${elapsed}` : '...'}</span>
          </>
        ) : (
          <span>思考{elapsed ? ` (${elapsed})` : ` (${textLength} 字符)`}</span>
        )}
      </button>

      {expanded ? (
        <pre className="mt-1.5 max-h-64 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed">
          {hasText ? block.text : '思考中...'}
        </pre>
      ) : isStreaming && hasText ? (
        <div className="mt-1.5 flex max-h-24 flex-col-reverse overflow-hidden">
          <pre className="whitespace-pre-wrap break-words text-[0.68rem] text-[var(--duties-tertiary)] leading-relaxed">
            {getLastLines(block.text)}
          </pre>
        </div>
      ) : hasText ? (
        <div className="mt-1.5 truncate text-[0.68rem] text-[var(--duties-tertiary)]">{getPreview(block.text)}</div>
      ) : null}
    </div>
  );
};
