// [2026-06-05] Rewritten composer layout.
// Why: the chat footer now needs a single-line-first auto-growing textarea, Enter
// submission, and a local approval-level selector in the same bordered control.
// How: keep the existing attachment upload state, add a textarea ref for measured
// resizing, and read approvalLevel from the browser-local client preferences store.
// Purpose: the input matches the compact two-layer layout without changing the send API.
import {
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { type ApprovalLevel, useChatStore, useClientPrefsStore } from '../../store';
import type { Attachment } from '../../types';
import { Icon } from '../common';
import { AttachmentList } from './AttachmentList';
import { ContextPopover } from './ContextPopover';
import { PluginSlotHost } from '../plugins/PluginSlotHost';

const APPROVAL_LEVEL_ORDER: ApprovalLevel[] = ['manual', 'smart', 'yolo'];

const APPROVAL_LEVEL_DISPLAY: Record<ApprovalLevel, { icon: string; label: string }> = {
  manual: { icon: 'shield', label: '审批' },
  smart: { icon: 'bolt', label: '自动' },
  yolo: { icon: 'lock_open', label: '放行' },
};

const FALLBACK_LINE_HEIGHT_PX = 20;
const MAX_VISIBLE_TEXTAREA_LINES = 6;

const readCssPixelValue = (value: string): number => {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

const resizeTextareaToContent = (textarea: HTMLTextAreaElement) => {
  // [2026-06-05] Why: CSS alone cannot cap a textarea at exactly six measured
  // lines while keeping the first row compact. How: reset height, read scrollHeight
  // and computed line metrics, then clamp to six lines and enable vertical scrolling
  // only after that limit. Purpose: long drafts stop expanding the page footer.
  textarea.style.height = 'auto';
  const styles = window.getComputedStyle(textarea);
  const parsedLineHeight = Number.parseFloat(styles.lineHeight);
  const lineHeight = Number.isFinite(parsedLineHeight) ? parsedLineHeight : FALLBACK_LINE_HEIGHT_PX;
  const verticalPadding = readCssPixelValue(styles.paddingTop) + readCssPixelValue(styles.paddingBottom);
  const minHeight = lineHeight + verticalPadding;
  const maxHeight = lineHeight * MAX_VISIBLE_TEXTAREA_LINES + verticalPadding;
  const nextHeight = Math.min(Math.max(textarea.scrollHeight, minHeight), maxHeight);

  textarea.style.height = `${nextHeight}px`;
  textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
};

interface ChatInputProps {
  disabled?: boolean;
  onSend: (text: string, attachments?: Attachment[]) => Promise<void> | void;
}

const formatTokenCount = (tokens: number): string => {
  const safeTokens = Math.max(0, Math.round(tokens));
  if (safeTokens >= 1_000_000) return `${(safeTokens / 1_000_000).toFixed(1)}M`;
  if (safeTokens >= 1_000) return `${Math.round(safeTokens / 1_000)}K`;
  return String(safeTokens);
};

const contextUsageColorClass = (utilization: number): string => {
  // [2026-06-05] Why: the indicator should become more prominent as compaction gets
  // closer. How: apply the requested gray, yellow, and red text classes by threshold.
  // Purpose: token pressure is visible without adding borders or disrupting toolbar layout.
  if (utilization >= 0.75) return 'text-red-500';
  if (utilization >= 0.5) return 'text-yellow-500';
  return 'text-[var(--duties-tertiary)]';
};

const revokeLocalAttachmentUrls = (items: readonly Attachment[]) => {
  for (const attachment of items) {
    if (attachment.url?.startsWith('blob:')) URL.revokeObjectURL(attachment.url);
  }
};

export const ChatInput = ({ disabled = false, onSend }: ChatInputProps) => {
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [isDraggingFiles, setIsDraggingFiles] = useState(false);
  const [isContextPopoverOpen, setIsContextPopoverOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachmentsRef = useRef<Attachment[]>([]);
  const approvalLevel = useClientPrefsStore((state) => state.approvalLevel) || 'smart';
  const setApprovalLevel = useClientPrefsStore((state) => state.setApprovalLevel);
  const approvalDisplay = APPROVAL_LEVEL_DISPLAY[approvalLevel] || APPROVAL_LEVEL_DISPLAY.smart;
  const contextUsage = useChatStore((state) => {
    const activeConversation = state.activeConversationId
      ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
      : undefined;
    const sessionId = state.viewingChildSessionId || activeConversation?.sessionId || '';
    return sessionId ? state.contextUsageBySession[sessionId] || null : state.contextUsage;
  });
  // [AutoC 2026-06-15] Why: on the welcome page (activeConversationId=null),
  // a background session may still be generating. The global isGenerating flag
  // stays true from that session, making the input show stop/preempt instead of
  // send. How: derive generating state from the active conversation's session.
  // If no conversation is active (welcome page), always treat as idle.
  const isGenerating = useChatStore((state) => {
    if (!state.activeConversationId) return false;
    const conv = state.conversations.find((c) => c.id === state.activeConversationId);
    if (!conv?.sessionId) return state.isGenerating;
    return Boolean(state.generatingBySession[conv.sessionId]);
  });
  const composerDisabled = disabled && !isGenerating;
  const cancelCurrentTask = useChatStore((state) => state.cancelCurrentTask);
  const preemptCurrentTask = useChatStore((state) => state.preemptCurrentTask);
  const contextSessionId = useChatStore((state) => {
    const activeConversation = state.activeConversationId
      ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
      : undefined;
    return state.viewingChildSessionId || activeConversation?.sessionId || '';
  });
  const conversationKey = useChatStore((state) =>
    state.activeConversationId ? `web:${state.activeConversationId}` : '',
  );

  // [AutoC 2026-08-22] Plugin input slots need the reroll target and generating
  // state as data. Why: a quick-reroll contribution cannot read the store by
  // itself; the host resolves the last user message id here. How: reverse-scan
  // the active conversation's order array; empty when a child session is being
  // viewed (target conversation ambiguous) or nothing to rewind. Purpose: slot
  // scripts get a declarative enable/disable signal instead of store access.
  const activeConversationId = useChatStore((state) => state.activeConversationId);
  const rerollTargetId = useChatStore((state) => {
    if (state.viewingChildSessionId) return '';
    const cid = state.activeConversationId;
    if (!cid) return '';
    const order = state.messageOrderByConversation[cid] || [];
    for (let i = order.length - 1; i >= 0; i--) {
      const message = state.messagesById[order[i]];
      if (message?.role === 'user') return order[i];
    }
    return '';
  });
  const inputSlotData = useMemo(
    () => ({
      conversationId: activeConversationId || '',
      sessionId: contextSessionId,
      isGenerating,
      composerDisabled,
      rerollTargetId,
    }),
    [activeConversationId, contextSessionId, isGenerating, composerDisabled, rerollTargetId],
  );

  const hasDraft = draft.trim().length > 0 || attachments.length > 0;
  const canSend = hasDraft;
  const addFiles = (files: FileList | File[]) => {
    if (composerDisabled) return;
    const incoming = Array.from(files).filter((file) => file.size > 0);
    if (incoming.length === 0) return;
    const newAttachments: Attachment[] = incoming.map((file) => ({
      name: file.name || '粘贴的文件',
      size: file.size,
      type: file.type.startsWith('image/') ? 'image' : 'file',
      mime_type: file.type || undefined,
      url: URL.createObjectURL(file),
      file,
    }));
    setAttachments((prev) => [...prev, ...newAttachments]);
  };
  // [2026-06-06] Why: the submit button should adapt to streaming state. How: when
  // generating and draft is empty, show stop; when generating with draft, show send
  // (preempt semantics); when idle, normal send. Purpose: cancel and preempt are
  // reachable from the same button position without a separate header control.
  const showStop = isGenerating && !hasDraft;
  const contextUsagePercent = contextUsage ? Math.round(contextUsage.utilization * 100) : 0;
  const contextUsageTitle = contextUsage
    ? `上下文：${contextUsage.effectiveTokens} / ${contextUsage.compactThreshold} tokens (${contextUsagePercent}%)`
    : '';

  useLayoutEffect(() => {
    // [2026-06-05] Why: clearing or editing the draft changes the textarea's natural
    // height. How: remeasure after React commits each draft value. Purpose: submitted
    // messages collapse the composer back to a single-row control immediately.
    if (textareaRef.current) resizeTextareaToContent(textareaRef.current);
  }, [draft]);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => () => {
    revokeLocalAttachmentUrls(attachmentsRef.current);
  }, []);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (showStop) {
      await cancelCurrentTask();
      return;
    }
    if (composerDisabled || !canSend) return;

    const text = draft.trim();
    const currentAttachments = [...attachments];
    setDraft('');
    setAttachments([]);
    revokeLocalAttachmentUrls(currentAttachments);
    if (isGenerating) {
      await preemptCurrentTask(text, currentAttachments.length > 0 ? currentAttachments : undefined);
    } else {
      await onSend(text, currentAttachments.length > 0 ? currentAttachments : undefined);
    }
  };

  const handleDraftChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setDraft(event.target.value);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // [2026-06-07] Why: 'ontouchstart' in window returns true on touch-screen
    // laptops (Surface, etc.), breaking Enter-to-send on desktop. How: use the
    // CSS media query 'pointer: coarse' which checks if the PRIMARY input is a
    // finger (phone/tablet) vs a mouse (desktop, even with touch screen).
    // Shift+Enter and IME composition always insert text. Purpose: desktop keeps
    // Enter shortcut; only true mobile devices use the send button.
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
    const isMobile = window.matchMedia?.('(pointer: coarse)').matches ?? false;
    if (isMobile) return;
    if (!showStop && !canSend) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  const cycleApprovalLevel = () => {
    // [2026-06-05] Why: chat users need the same safety-level switch that exists in
    // client settings. How: advance through the explicit manual → smart → yolo order
    // and persist through clientPrefsStore. Purpose: the footer button remains compact
    // while still changing the real approval behavior.
    const currentIndex = APPROVAL_LEVEL_ORDER.indexOf(approvalLevel);
    const nextIndex = currentIndex >= 0 ? (currentIndex + 1) % APPROVAL_LEVEL_ORDER.length : 0;
    setApprovalLevel(APPROVAL_LEVEL_ORDER[nextIndex]);
  };

  const handleFileSelect = () => {
    const files = fileInputRef.current?.files;
    if (!files) return;
    addFiles(files);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = event.clipboardData?.files;
    if (!files || files.length === 0) return;
    event.preventDefault();
    addFiles(files);
  };

  const handleDragOver = (event: DragEvent<HTMLFormElement>) => {
    if (composerDisabled || !event.dataTransfer?.types.includes('Files')) return;
    event.preventDefault();
    setIsDraggingFiles(true);
  };

  const handleDragLeave = (event: DragEvent<HTMLFormElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setIsDraggingFiles(false);
    }
  };

  const handleDrop = (event: DragEvent<HTMLFormElement>) => {
    if (!event.dataTransfer?.files || event.dataTransfer.files.length === 0) return;
    event.preventDefault();
    setIsDraggingFiles(false);
    addFiles(event.dataTransfer.files);
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => {
      const removed = prev[index];
      if (removed?.url) URL.revokeObjectURL(removed.url);
      return prev.filter((_, i) => i !== index);
    });
  };

  return (
    <div className="relative mx-auto max-w-3xl px-2 py-2 sm:px-3 sm:py-3">
      {/* [AutoC 2026-08-22] Floating slot above the input bar. Why: quick-action
          chips need to hover over the message area without consuming layout
          height. How: absolutely positioned layer pinned to this container's top
          edge, right-aligned flex wrap; PluginSlotHost renders nothing when the
          slot has no contribution. */}
      <div className="pointer-events-none absolute inset-x-2 bottom-full z-10 sm:inset-x-3">
        <PluginSlotHost
          className="pointer-events-auto mb-1 flex flex-wrap items-center justify-end gap-1"
          data={inputSlotData}
          slot="input_above"
        />
      </div>
      {/* [2026-06-05] Why: previews should not consume the bordered composer body.
          How: keep the existing preview list above the form container. Purpose:
          attachments remain visible while the textarea and toolbar share one border. */}
      <AttachmentList attachments={attachments} onRemove={removeAttachment} variant="composer" />
      <form
        className={`relative border border-[var(--duties-border)] transition-colors focus-within:border-[var(--duties-text)] ${isDraggingFiles ? 'border-blue-500 bg-blue-50/40' : ''}`}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
        onSubmit={handleSubmit}
      >
        {isDraggingFiles && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-blue-50/80 text-sm font-medium text-blue-700">
            松开鼠标添加附件
          </div>
        )}
        {/* [2026-06-05] Why: the old grid placed the textarea between buttons and made
            the default composer feel like a multiline box. How: move the textarea to
            the first row, remove its own border, and let JS control its height from
            rows=1 up to six lines. Purpose: text entry gets the full available width. */}
        <textarea
          className="block w-full resize-none bg-transparent px-3 py-2.5 font-sans text-sm leading-5 text-[var(--duties-text)] outline-none placeholder:text-[var(--duties-tertiary)] disabled:cursor-not-allowed disabled:opacity-50"
          data-composer-textarea
          disabled={composerDisabled}
          onChange={handleDraftChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={isGenerating ? '输入追加指令… Enter 发送 / 留空按 Enter 停止' : '输入消息… Enter 发送 / Shift+Enter 换行'}
          ref={textareaRef}
          rows={1}
          style={{ overflowY: 'hidden' }}
          value={draft}
        />
        {/* [2026-06-05] Why: attachment controls, approval level, and submit action
            need a dedicated second row. How: split the toolbar with justify-between,
            keep utility controls on the left, and keep Send on the right. Purpose:
            the composer reads as one bordered unit with predictable alignment. */}
        <div className="flex items-center justify-between gap-2 px-2 py-1.5">
          <div className="flex min-w-0 items-center gap-1.5">
            {/* Attach files button.
                [2026-06-01] Why: the file picker button used a paperclip emoji.
                How: render attach_file with the shared Icon primitive. Purpose: composer
                controls use Material Symbols instead of platform emoji. */}
            <button
              className="inline-flex h-8 items-center justify-center border border-[var(--duties-border)] px-2 text-sm text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]"
              disabled={composerDisabled}
              onClick={() => fileInputRef.current?.click()}
              title="添加文件"
              type="button"
            >
              <Icon name="attach_file" size={18} />
            </button>
            <button
              aria-label={`审批级别：${approvalDisplay.label}。点击切换`}
              className="inline-flex h-8 items-center gap-1 border border-[var(--duties-border)] px-2 font-mono text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]"
              onClick={cycleApprovalLevel}
              title={`审批级别：${approvalDisplay.label}`}
              type="button"
            >
              {/* [2026-06-05] Why: the requested compact label includes both an
                  icon and a short Chinese name. How: keep a literal space between
                  the two rendered values instead of relying only on flex gap. Purpose:
                  the visible text and tests match the documented manual/smart/yolo labels. */}
              <Icon name={approvalDisplay.icon} size={16} />
              <span>{approvalDisplay.label}</span>
            </button>
            {/* Reserved slot: plugins may append toolbar controls on the left. */}
            <PluginSlotHost
              className="flex min-w-0 items-center gap-1.5"
              data={inputSlotData}
              slot="input_toolbar_left"
            />
          </div>
          <div className="flex items-center gap-2">
            {/* Reserved slot: plugins may append toolbar controls on the right
                (quick actions like reroll, before the context indicator). */}
            <PluginSlotHost
              className="flex items-center gap-2"
              data={inputSlotData}
              slot="input_toolbar_right"
            />
            {contextUsage && (
              <div
                className="relative"
                // [2026-06-06] Why: the popover owns document-level outside-click
                // handling, but the trigger sits outside the popover panel. How: stop
                // pointer starts on this wrapper from bubbling to the document listener.
                // Purpose: clicking the indicator toggles cleanly instead of closing
                // and reopening during the same interaction.
                onMouseDown={(event) => event.stopPropagation()}
                onTouchStart={(event) => event.stopPropagation()}
              >
                <button
                  aria-label={`${contextUsageTitle}。点击打开上下文压缩设置`}
                  className="inline-flex items-center gap-1.5 whitespace-nowrap border-0 bg-transparent p-0 font-mono text-xs text-[var(--duties-secondary)]"
                  disabled={!contextSessionId}
                  onClick={() => setIsContextPopoverOpen((open) => !open)}
                  title={contextUsageTitle}
                  type="button"
                >
                  {(() => {
                    const size = 22;
                    const strokeWidth = 2.5;
                    const r = (size - strokeWidth) / 2;
                    const cx = size / 2;
                    const cy = size / 2;
                    const totalDeg = 275;
                    const gapDeg = 360 - totalDeg;
                    const startAngle = 90 + gapDeg / 2; // opening centered at bottom
                    const circumference = 2 * Math.PI * r;
                    const arcLen = (totalDeg / 360) * circumference;
                    const fillLen = arcLen * Math.min(1, Math.max(0, contextUsage.utilization));
                    const strokeColor = contextUsage.utilization >= 0.75 ? '#ef4444' : contextUsage.utilization >= 0.5 ? '#eab308' : 'var(--duties-tertiary)';
                    const toRad = (deg: number) => (deg * Math.PI) / 180;
                    const sx = cx + r * Math.cos(toRad(startAngle));
                    const sy = cy + r * Math.sin(toRad(startAngle));
                    const cacheRate = contextUsage.cacheHitRate;
                    // [AutoC 2026-08-24] 缓存率分档变色：90% 以上绿色（良好），
                    // 70%–90% 次级正文色（正常），70% 以下三级灰（偏低）。
                    const cacheRateColor = cacheRate === null || cacheRate === undefined
                      ? 'text-[var(--duties-tertiary)]'
                      : cacheRate >= 0.9
                        ? 'text-green-600'
                        : cacheRate >= 0.7
                          ? 'text-[var(--duties-secondary)]'
                          : 'text-[var(--duties-tertiary)]';
                    return (
                      <span className="relative inline-flex flex-shrink-0" style={{ width: size, height: size }}>
                      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
                        <circle
                          cx={cx} cy={cy} r={r}
                          fill="none"
                          stroke="var(--duties-border)"
                          strokeWidth={strokeWidth}
                          strokeDasharray={`${arcLen} ${circumference - arcLen}`}
                          strokeDashoffset={-(startAngle / 360) * circumference}
                          strokeLinecap="round"
                          transform={`rotate(0 ${cx} ${cy})`}
                          style={{ transformOrigin: `${cx}px ${cy}px` }}
                        />
                        {fillLen > 0 && (
                          <circle
                            cx={cx} cy={cy} r={r}
                            fill="none"
                            stroke={strokeColor}
                            strokeWidth={strokeWidth}
                            strokeDasharray={`${fillLen} ${circumference - fillLen}`}
                            strokeDashoffset={-(startAngle / 360) * circumference}
                            strokeLinecap="round"
                            style={{ transformOrigin: `${cx}px ${cy}px`, transition: 'stroke-dasharray 0.3s ease' }}
                          />
                        )}
                      </svg>
                      {/* [AutoC 2026-08-24] 缓存命中率显示在环中心：百分比量纲
                          与环的进度语义一致，比放在环右侧更紧凑。 */}
                      {cacheRate !== null && cacheRate !== undefined && (
                        <span
                          className={`pointer-events-none absolute inset-0 flex items-center justify-center font-mono text-[0.45rem] leading-none ${cacheRateColor}`}
                          title={`缓存命中率（指数移动平均）：${(cacheRate * 100).toFixed(1)}%`}
                        >
                          {(cacheRate * 100).toFixed(0)}%
                        </span>
                      )}
                      </span>
                    );
                  })()}
                  <span className={contextUsageColorClass(contextUsage.utilization)}>{formatTokenCount(contextUsage.effectiveTokens)}</span>
                </button>
                {isContextPopoverOpen && (
                  <ContextPopover
                    conversationKey={conversationKey}
                    onClose={() => setIsContextPopoverOpen(false)}
                    sessionId={contextSessionId}
                    usage={contextUsage}
                  />
                )}
              </div>
            )}
            <button
              className="inline-flex h-8 w-8 items-center justify-center text-[var(--duties-secondary)] transition-colors hover:text-[var(--duties-text)] disabled:cursor-not-allowed disabled:opacity-30"
              disabled={!showStop && (composerDisabled || !canSend)}
              title={showStop ? '停止' : isGenerating ? '追加指令' : '发送'}
              type="submit"
            >
              <Icon name={showStop ? 'stop' : isGenerating ? 'quick_phrases' : 'send'} size={20} />
            </button>
          </div>
        </div>
        <input
          accept="*/*"
          className="hidden"
          disabled={composerDisabled}
          multiple
          onChange={handleFileSelect}
          ref={fileInputRef}
          type="file"
        />
      </form>
      {/* Reserved slot: plugins may append widgets under the composer. */}
      <PluginSlotHost slot="input_suffix" className="mt-1.5" />
    </div>
  );
};
