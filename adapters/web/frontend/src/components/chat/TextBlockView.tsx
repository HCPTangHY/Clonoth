// [2026-05-31] TextBlockView renders normalized text blocks for MessageCard v2.
// Why: the new reducer stores final, intermediate, history, and streaming text in
// RenderBlock objects instead of the old split message/preview fields. How: render
// Markdown through ReactMarkdown and add small visual markers only from the block's
// delivery metadata. Purpose: keep all text output in the unified MessageCard path.
import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import type { TextBlock } from '../../types/message';
import { INLINE_BLOCK_BODY_TEXT_CLASS, INLINE_BLOCK_INDENT_CLASS, INLINE_TEXT_BORDER_BASE_CLASS, type MessageRenderContext } from './renderingConstants';
import { annotateText, getAnnotatorVersion, type AnnotatorMatch } from '../../store/annotators';
import { callHostAction } from '../../store/hostActions';

interface TextBlockViewProps {
  block: TextBlock;
  messageContext?: MessageRenderContext;
}

function getTextBorderClassName(messageContext?: MessageRenderContext): string {
  // Why: reply and finish borders are message-level completion markers, not text
  // delivery markers. How: read the compact MessageRenderContext and apply the
  // shared inline border base only to assistant reply or successful finish output.
  // Purpose: text receives the colored bar while sibling thinking and tool blocks
  // keep their normal inline alignment.
  if (messageContext?.role !== 'assistant') return '';
  if (messageContext.completionType === 'reply') return `${INLINE_TEXT_BORDER_BASE_CLASS} border-blue-400`;
  if (messageContext.completionType === 'finish' && messageContext.status !== 'failed') return `${INLINE_TEXT_BORDER_BASE_CLASS} border-green-400`;
  return '';
}

function isAssistantFreeProse(block: TextBlock, messageContext?: MessageRenderContext): boolean {
  // Why: free prose is assistant text emitted between tool calls, not a formal
  // reply/ask/finish completion. How: detect assistant stream/final text only when
  // no completionType is present. Purpose: it can be dimmed without adding a border
  // rail that would be confused with ThinkingBlock's existing left marker.
  return messageContext?.role === 'assistant'
    && !messageContext.completionType
    && (block.delivery === 'stream' || block.delivery === 'final' || block.delivery === 'history');
}

// Why: stream text can briefly contain tool protocol markers before the parser
// consumes them. How: strip complete and trailing marker sections before rendering.
// Purpose: users see only the assistant text that belongs in the message body.
const TOOL_CALL_PATTERN = /<<<TOOL_CALL>>>[\s\S]*?<<<END_TOOL_CALL>>>/g;
const TRAILING_TOOL_CALL_PATTERN = /<<<(?:TOOL_CALL|END_TOOL_CALL)>>>[\s\S]*$/;

function cleanProtocolMarkers(text: string): string {
  let cleaned = text.replace(TOOL_CALL_PATTERN, '').replace(TRAILING_TOOL_CALL_PATTERN, '');
  cleaned = cleaned.replace(/<<<(?:TOOL_CALL|END_TOOL_CALL)>>>/g, '');
  return cleaned.trim();
}

function isDiffFence(className: string | undefined): boolean {
  return /\blanguage-(diff|patch)\b/.test(className || '');
}

function diffLineClassName(line: string): string {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'diff-line diff-line-add';
  if (line.startsWith('-') && !line.startsWith('---')) return 'diff-line diff-line-delete';
  if (line.startsWith('@@')) return 'diff-line diff-line-hunk';
  return 'diff-line diff-line-context';
}

// Base markdown renderer map. The code arm delegates non-diff inline code to the
// annotation-aware renderer via a per-render closure (see buildMarkdownComponents).
const markdownComponents: Components = {
  code({ node: _node, className, children, ...props }) {
    return renderCodeSpan(className, children, props);
  },
};

/** Shared code renderer: diff fences get per-line classes, others plain. */
function renderCodeSpan(
  className: string | undefined,
  children: unknown,
  props: Record<string, unknown>,
) {
  const text = String(children ?? '');
  if (!isDiffFence(className)) {
    return <code className={className} {...props}>{text}</code>;
  }
  const normalized = text.replace(/\n$/, '');
  const lines = normalized.split('\n');
  return (
    <code className={`${className || ''} diff-code-block`} {...props}>
      {lines.map((line, index) => (
        <span className={diffLineClassName(line)} key={`${index}:${line}`}>
          {line || ' '}
        </span>
      ))}
    </code>
  );
}

/**
 * [AutoC 2026-08-25] Build a per-render ReactMarkdown components map whose code
 * renderer queries the annotator registry with the current message context.
 * Why: plugins contribute message annotations (file links etc.) without touching
 * host DOM; the host renders matched spans as clickable. How: the shared
 * module-level components map cannot see messageContext, so TextBlockView calls
 * this once per render when annotators are loaded; streaming blocks and an empty
 * annotator set return the shared map unchanged. Matched spans become buttons
 * that call the generic openPanel host action with the annotator-declared
 * panel and opaque intent — the host never parses the intent.
 */
function buildMarkdownComponents(
  messageContext: MessageRenderContext | undefined,
  streaming: boolean,
): Components {
  if (streaming || getAnnotatorVersion() === 0) return markdownComponents;
  const role = messageContext?.role || '';
  const sessionId = messageContext?.sessionId || '';

  const annotated: Components = { ...markdownComponents };
  annotated.code = ({ node: _node, className, children, ...props }) => {
    if (isDiffFence(className)) {
      return renderCodeSpan(className, children, props);
    }
    const text = String(children ?? '');
    const matches = annotateText(text, { role, sessionId });
    if (matches.length === 0) {
      return <code className={className} {...props}>{text}</code>;
    }
    const parts: Array<{ t: string; m?: AnnotatorMatch; key: string }> = [];
    let cursor = 0;
    matches.forEach((m, i) => {
      if (m.start > cursor) parts.push({ t: text.slice(cursor, m.start), key: `p${i}` });
      parts.push({ t: text.slice(m.start, m.end), m, key: `m${i}` });
      cursor = m.end;
    });
    if (cursor < text.length) parts.push({ t: text.slice(cursor), key: 'tail' });
    return (
      <code className={className} {...props}>
        {parts.map((p) =>
          p.m ? (
            <button
              className="msg-annotation"
              key={p.key}
              onClick={() => void callHostAction('openPanel', p.m!.open.panel, p.m!.open.intent)}
              title={p.t}
              type="button"
            >
              {p.t}
            </button>
          ) : (
            <span key={p.key}>{p.t}</span>
          ),
        )}
      </code>
    );
  };
  return annotated;
}

export const TextBlockView = ({ block, messageContext }: TextBlockViewProps) => {
  // [2026-06-06] Streaming timeout guard: same pattern as ThinkingBlock.
  // If streaming stays true but text stops updating for 10s, hide the cursor.
  const textLen = block.text.length;
  const lastTextLenRef = useRef(textLen);
  const lastUpdateRef = useRef(Date.now());
  const [streamingTimedOut, setStreamingTimedOut] = useState(false);

  useEffect(() => {
    if (!block.streaming) {
      setStreamingTimedOut(false);
      return;
    }
    if (textLen !== lastTextLenRef.current) {
      lastTextLenRef.current = textLen;
      lastUpdateRef.current = Date.now();
      setStreamingTimedOut(false);
    }
    const timer = setInterval(() => {
      if (Date.now() - lastUpdateRef.current > 10_000) {
        setStreamingTimedOut(true);
        clearInterval(timer);
      }
    }, 2_000);
    return () => clearInterval(timer);
  }, [block.streaming, textLen]);

  const isActivelyStreaming = block.delivery === 'stream' && block.streaming && !streamingTimedOut;
  const showCursor = isActivelyStreaming;
  const displayText = cleanProtocolMarkers(block.text);
  const isFreeProse = isAssistantFreeProse(block, messageContext);

  // [AutoC 2026-08-25] Per-render components with annotation context. Memoized on
  // the streaming flag and annotator version so identical re-renders reuse the map.
  const components = useMemo(
    () => buildMarkdownComponents(messageContext, !!isActivelyStreaming),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isActivelyStreaming, messageContext?.role, messageContext?.sessionId, getAnnotatorVersion()],
  );

  if (!displayText && !showCursor) return null;

  // Free prose: smaller font, dimmed color, indented to align with tool/thinking blocks
  const containerClass = isFreeProse
    ? `markdown-body ${INLINE_BLOCK_BODY_TEXT_CLASS} leading-5 text-[var(--duties-tertiary)] ${INLINE_BLOCK_INDENT_CLASS} pl-3`
    : `markdown-body text-sm leading-6 text-[var(--duties-text)] ${getTextBorderClassName(messageContext)}`;

  return (
    <div className={containerClass}>
      {displayText && <ReactMarkdown components={components} remarkPlugins={[remarkGfm]}>{displayText}</ReactMarkdown>}
      {showCursor && (
        // [2026-06-01] Why: the streaming cursor used a block Unicode glyph.
        // How: draw the cursor as a small CSS rectangle instead. Purpose: live text
        // rendering does not rely on decorative Unicode symbols.
        <span aria-label="流式输出光标" className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-blue-500 align-middle" />
      )}
    </div>
  );
};
