// [2026-05-31] MessageCard is the unified v2 renderer for normalized chat messages.
// Why: Step 2B replaces the old MessageBubble plus StreamPreview split with one replayable
// message surface. How: derive header, role styling, streaming indicator, ordered block
// rendering, and attachments from WsMessage only. Purpose: make active and historical
// messages follow the same UI contract before the app is rewired to v2.
import { useState, useRef, useEffect } from 'react';
import type { MessageRole, MessageStatus, TextBlock, ToolExecution, WsMessage } from '../../types/message';
import { useChatStore } from '../../store/chatStore';
import { AttachmentList } from './AttachmentList';
import { RenderBlockView } from './RenderBlockView';
import { BLOCK_STACK_CLASS, type MessageRenderContext } from './renderingConstants';

interface MessageCardProps {
  message: WsMessage;
  toolsById: Record<string, ToolExecution>;
  prevRole?: WsMessage['role'];
  nextRole?: WsMessage['role'];
  isLastUserMessage?: boolean;
}

const ROLE_LABELS: Record<Exclude<MessageRole, 'dispatch_callback'>, string> = {
  user: '你',
  assistant: '助手',
  system: '系统',
};

const ROLE_STYLES: Record<MessageRole, { row: string; label: string }> = {
  user: {
    row: 'bg-[var(--duties-bg)]',
    label: 'text-[var(--duties-text)]',
  },
  assistant: {
    row: 'bg-[var(--duties-panel)]',
    label: 'text-blue-600',
  },
  system: {
    row: 'bg-orange-50/60',
    label: 'text-orange-600',
  },
  // Why: dispatch callbacks need a visual lane separate from user, assistant, and
  // system cards. How: apply a subtle purple row and label. Purpose: the callback
  // remains fully expanded while being easy to scan.
  dispatch_callback: {
    row: 'bg-purple-50/40',
    label: 'text-purple-600',
  },
};

const STATUS_LABELS: Record<MessageStatus, string> = {
  pending: '等待中',
  streaming: '输出中',
  running_tools: '工具运行中',
  awaiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function isActiveStatus(status: MessageStatus): boolean {
  return status === 'streaming' || status === 'running_tools';
}

function getDispatchCallbackTitle(message: WsMessage): string {
  // Why: dispatch_result payload text is now raw child output and no longer includes
  // a localized title. How: derive the Chinese title from MessageSource caller and
  // child fields, with the old fixed label only when all structured fields are absent.
  // Purpose: old history remains readable while new cards never parse result text for
  // presentation.
  const source = message.source || {};
  const nodeId = (source.childNodeId || source.nodeId || '').trim();
  const callerNodeId = (source.callerNodeId || '').trim();
  const hasStructuredSource = Boolean(nodeId || callerNodeId || source.summary || source.childSessionId || source.childTaskId);
  if (!hasStructuredSource) return '子节点回调';
  const displayNodeId = nodeId || '未知';
  return callerNodeId ? `${callerNodeId} 委派的 ${displayNodeId} 已完成` : `子节点 ${displayNodeId} 已完成`;
}

function getRoleLabel(message: WsMessage): string {
  if (message.role === 'dispatch_callback') return getDispatchCallbackTitle(message);
  return ROLE_LABELS[message.role];
}

function getStatusClassName(status: MessageStatus): string {
  if (status === 'failed') return 'text-red-600';
  if (status === 'cancelled') return 'text-gray-500';
  if (status === 'awaiting_approval') return 'text-orange-600';
  if (isActiveStatus(status)) return 'text-blue-600';
  return 'text-[var(--duties-tertiary)]';
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date);
}

// ponytail: user message retry/edit lives in the header row, shown on hover only.
// upgrade path: extract into a separate file if more per-role actions are added.
function useUserRetryEdit(message: WsMessage) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const originalText = message.blocks
    .filter((b) => b.kind === 'text')
    .map((b) => (b as TextBlock).text)
    .join('\n');

  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
      textareaRef.current.setSelectionRange(draft.length, draft.length);
    }
  }, [editing]);

  const doRetry = (newText?: string) => {
    if (window.confirm('将取消当前任务并截断此消息之后的所有内容，确认？')) {
      useChatStore.getState().retryMessage(message.id, newText);
      setEditing(false);
    }
  };

  const startEdit = () => { setDraft(originalText); setEditing(true); };
  const cancelEdit = () => setEditing(false);
  const submitEdit = () => doRetry(draft.trim() || undefined);

  return { editing, draft, setDraft, textareaRef, doRetry, startEdit, cancelEdit, submitEdit };
}

export const MessageCard = ({ message, toolsById, prevRole, nextRole, isLastUserMessage }: MessageCardProps) => {
  const roleStyle = ROLE_STYLES[message.role];
  const active = isActiveStatus(message.status);
  const attachments = message.attachments ?? [];
  // Why: child block renderers only need the message fields that affect presentation.
  // How: pass a small context object instead of threading completionType, role, and
  // status as separate props. Purpose: the message-to-block contract stays explicit
  // without making RenderBlockView mirror the full WsMessage shape.
  const messageContext: MessageRenderContext = {
    completionType: message.completionType,
    role: message.role,
    status: message.status,
  };
  // Why: only dispatch callback cards can navigate to child sessions. How: read the
  // backend-provided source.childSessionId and leave normal messages without an
  // action. Purpose: navigation stays structured and does not parse the callback text.
  const childSessionId = message.role === 'dispatch_callback' ? message.source.childSessionId?.trim() || '' : '';
  // Why: child result summaries moved out of backend text into structured source
  // metadata. How: render source.summary directly below the dynamic dispatch title.
  // Purpose: users see the summary without duplicating it in payload.text.
  const dispatchSummary = message.role === 'dispatch_callback' ? message.source.summary?.trim() || '' : '';

  // Why: consecutive assistant cards look fragmented with repeated headers and
  // borders. How: hide the top border when the previous message is also assistant,
  // hide the bottom border when the next message is also assistant, and suppress the
  // header row entirely for continuation cards. Purpose: adjacent assistant cards
  // merge visually while keeping separate data structures underneath.
  const isAssistant = message.role === 'assistant';
  const continuedFromPrev = isAssistant && prevRole === 'assistant';
  const continuesIntoNext = isAssistant && nextRole === 'assistant';
  const borderClass = continuesIntoNext ? '' : 'border-b border-[var(--duties-border)]';
  // When cards merge, inner spacing should match the block stack gap (space-y-2 = 8px).
  // continuedFromPrev: no top padding (visually attached to previous card)
  // continuesIntoNext: reduced bottom padding so gap matches internal block spacing
  const paddingClass = continuedFromPrev
    ? (continuesIntoNext ? 'px-3 pt-0 pb-2 sm:px-4' : 'px-3 pt-0 pb-3 sm:px-4')
    : (continuesIntoNext ? 'px-3 pt-3 pb-2 sm:px-4' : 'px-3 py-3 sm:px-4');

  const showRetry = message.role === 'user' && message.status === 'completed' && !!message.source.inboundSeq;
  const retryEdit = useUserRetryEdit(message);

  return (
    <article className={`group/card ${borderClass} ${paddingClass} ${roleStyle.row}`} data-message-id={message.id}>
      <div className="mx-auto max-w-3xl">
        {!continuedFromPrev && (
          <header className="mb-1.5 flex flex-wrap items-center gap-2">
            <span className={`font-mono text-[0.6rem] font-semibold uppercase tracking-[0.18em] ${roleStyle.label}`}>
              {getRoleLabel(message)}
            </span>
            <time className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]" dateTime={message.createdAt}>
              {formatTime(message.createdAt)}
            </time>
            <span className={`font-mono text-[0.55rem] ${getStatusClassName(message.status)}`}>{STATUS_LABELS[message.status]}</span>
            {message.source.nodeName && (
              <span className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]">{message.source.nodeName}</span>
            )}
            {active && <span aria-label="消息正在活动" className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />}
            {showRetry && !retryEdit.editing && (
              <span className="ml-auto flex gap-1.5 opacity-0 transition-opacity group-hover/card:opacity-100">
                <button
                  className="font-mono text-[0.55rem] text-[var(--duties-tertiary)] hover:text-blue-600 transition-colors"
                  title="原样重试"
                  onClick={() => retryEdit.doRetry()}
                >
                  ↻
                </button>
                <button
                  className="font-mono text-[0.55rem] text-[var(--duties-tertiary)] hover:text-blue-600 transition-colors"
                  title="编辑后重试"
                  onClick={retryEdit.startEdit}
                >
                  ✎
                </button>
              </span>
            )}
          </header>
        )}

        {dispatchSummary && (
          <div className="mb-2 text-xs text-purple-700">
            {dispatchSummary}
          </div>
        )}

        <div className={BLOCK_STACK_CLASS}>
          {message.blocks
            .filter((block) => {
              // [2026-06-03] Why: free prose (stream/final text) duplicates reply text
              // and clutters the card. How: when a card contains any intermediate text
              // block (from reply/ask), hide non-intermediate text blocks. Cards without
              // any intermediate block (pure free prose) render normally.
              // Purpose: reply/finish/ask text is the authoritative user-facing output;
              // free prose is internal LLM reasoning noise.
              if (block.kind !== 'text') return true;
              const hasIntermediateText = message.blocks.some(
                (b) => b.kind === 'text' && (b as TextBlock).delivery === 'intermediate',
              );
              if (!hasIntermediateText) return true;
              return (block as TextBlock).delivery === 'intermediate';
            })
            .map((block) => (
              <RenderBlockView key={block.id} block={block} toolsById={toolsById} messageContext={messageContext} />
            ))}
        </div>

        {childSessionId && (
          <button
            className="mt-1 text-xs font-mono text-purple-600 underline hover:text-purple-800"
            onClick={() => useChatStore.getState().viewChildSession(childSessionId)}
          >
            {/* Why: dispatch_result payloads now carry the child session id. How:
                call the store's child-session view action directly with that id.
                Purpose: users can inspect the child node transcript without relying
                on the sidebar or text parsing. */}
            查看子节点详情 →
          </button>
        )}

        <AttachmentList attachments={attachments} />

        {showRetry && retryEdit.editing && (
          <div className="mt-1.5 flex flex-col gap-1.5">
            <textarea
              ref={retryEdit.textareaRef}
              className="w-full rounded border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1.5 text-xs text-[var(--duties-text)] focus:outline-none focus:ring-1 focus:ring-blue-500 resize-y min-h-[3rem]"
              value={retryEdit.draft}
              onChange={(e) => retryEdit.setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') retryEdit.cancelEdit();
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) retryEdit.submitEdit();
              }}
            />
            <div className="flex gap-2">
              <button className="text-[0.6rem] font-mono text-blue-600 hover:text-blue-800 transition-colors" onClick={retryEdit.submitEdit}>✓ 提交</button>
              <button className="text-[0.6rem] font-mono text-[var(--duties-tertiary)] hover:text-red-500 transition-colors" onClick={retryEdit.cancelEdit}>✕ 取消</button>
            </div>
          </div>
        )}
      </div>
    </article>
  );
};

export type { MessageCardProps };
