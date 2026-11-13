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
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { type ApprovalLevel, useChatStore } from '../../store';
import { useSessionApproval } from '../../store/useSessionApproval';
import { isConversationGenerating } from '../../store/eventRouting';
import type { Attachment } from '../../types';
import { Icon } from '../common';
import { AttachmentList } from './AttachmentList';
import { ContextPopover } from './ContextPopover';
import { PluginSlotHost } from '../plugins/PluginSlotHost';
import { composerDrafts, getComposerModeState, type ComposerApi, type ComposerMode, resolveComposerSubmission, uploadComposerAttachments } from '../../store/composerModes';
import { ensureComposerSession, bindComposerSession } from '../../store/chatStore';
import { createConversationId } from '../../store/chatTypes';
import { createClientRequestId, uploadAttachment } from '../../api/supervisorClient';

import { captureStoreGeneration, isCurrentStoreGeneration } from '../../store/historyLoader';
import { registerHostAction } from '../../store/hostActions';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';

const APPROVAL_LEVEL_ORDER: ApprovalLevel[] = ['manual', 'smart', 'yolo'];

const APPROVAL_LEVEL_DISPLAY: Record<ApprovalLevel | 'custom', { icon: string; label: string }> = {
  manual: { icon: 'shield', label: '审批' },
  smart: { icon: 'bolt', label: '自动' },
  yolo: { icon: 'lock_open', label: '放行' },
  custom: { icon: 'tune', label: '自定义' },
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
  // [AutoC 2026-08-27] 低档从三级灰改为次级正文色：三级灰（#6e6e73）与相邻
  // 工具栏按钮（次级 #4a4a4a）相比明显发虚，视觉上像半透明。
  if (utilization >= 0.75) return 'text-red-500';
  if (utilization >= 0.5) return 'text-yellow-500';
  return 'text-[var(--duties-secondary)]';
};

const revokeLocalAttachmentUrls = (items: readonly Attachment[]) => {
  for (const attachment of items) {
    if (attachment.url?.startsWith('blob:')) URL.revokeObjectURL(attachment.url);
  }
};

export const ChatInput = ({ disabled = false, onSend }: ChatInputProps) => {
  const initialScope = (() => {
    const state = useChatStore.getState();
    return state.viewingChildSessionId ? `child:${state.viewingChildSessionId}` : state.activeConversationId || 'welcome';
  })();
  const [draft, setDraft] = useState(() => composerDrafts.get(initialScope)?.text || '');
  const [attachments, setAttachments] = useState<Attachment[]>(() => composerDrafts.get(initialScope)?.attachments || []);
  const [isDraggingFiles, setIsDraggingFiles] = useState(false);
  const [inputMode, setInputMode] = useState(() => composerDrafts.get(initialScope)?.mode || '');
  const [submitBusy, setSubmitBusy] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [preparedSessionId, setPreparedSessionId] = useState('');
  const preparedSessionRef = useRef('');
  const [modeRevision, setModeRevision] = useState(0);
  const modes = useRef(new Map<string, ComposerMode>());
  const knownCommands = useRef(new Set(['/goal']));
  const submission = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const alive = useRef(true);
  const [isContextPopoverOpen, setIsContextPopoverOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachmentsRef = useRef<Attachment[]>([]);
  const approval = useSessionApproval();
  const approvalLevel = approval.level;
  const approvalDisplay = APPROVAL_LEVEL_DISPLAY[approvalLevel];
  const contextUsage = useChatStore((state) => {
    const activeConversation = state.activeConversationId
      ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
      : undefined;
    const sessionId = state.viewingChildSessionId || activeConversation?.sessionId || '';
    return sessionId ? state.contextUsageBySession[sessionId] || null : state.contextUsage;
  });
  // [2026-09-05] Why: this selector previously ignored viewingChildSessionId, so
  // while viewing a running child task (entered from the active-tasks panel) the
  // composer stayed in idle mode and submission went through the plain inbound
  // path instead of preempting the running task. How: reuse the shared helper
  // that already understands the visible child session. Purpose: generating with
  // a draft = preempt, generating without draft = stop, in child views too.
  const isGenerating = useChatStore((state) =>
    isConversationGenerating(
      state.conversations,
      state.activeConversationId,
      state.generatingBySession,
      state.activeConversationId ? state.isGenerating : false,
      state.viewingChildSessionId,
    ),
  );
  const composerDisabled = (disabled && !isGenerating) || submitBusy;
  const cancelCurrentTask = useChatStore((state) => state.cancelCurrentTask);
  const preemptCurrentTask = useChatStore((state) => state.preemptCurrentTask);
  const contextSessionId = useChatStore((state) => {
    const activeConversation = state.activeConversationId
      ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
      : undefined;
    return state.viewingChildSessionId || activeConversation?.sessionId || '';
  });
  const conversationKey = useChatStore((state) => state.viewingChildSessionId
    ? state.viewingChildConversationKey || state.viewingChildSessionId
    : state.activeConversationId ? `web:${state.activeConversationId}` : '',
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
  // This is a temporary navigation view, not proof that the backend session is a child.
  const isTemporaryView = useChatStore(state => Boolean(state.viewingChildSessionId));
  const scope = isTemporaryView ? `child:${contextSessionId}` : activeConversationId || 'welcome';
  const rightPanelOpen = useSettingsStore(state => state.rightPanelOpen);
  const panelId = useViewStore(state => state.panelOverlay.right);
  const scopeRef = useRef(scope);
  const draftRef = useRef({ text: draft, attachments, mode: inputMode });
  draftRef.current = { text: draft, attachments, mode: inputMode };
  const getModeState = useCallback((id: string) => getComposerModeState(scope, id), [scope]);
  const setMode = useCallback((id: string, text?: string, metadata?: unknown) => {
    if (!alive.current || (busyRef.current && (id !== '' || text !== undefined))) return false;
    if (id && !modes.current.has(id)) throw new Error('输入插件尚未就绪。');
    if (text !== undefined && draftRef.current.text.trim() && draftRef.current.text !== text) {
      if (!window.confirm('输入框已有未提交内容，是否替换为目标正文？')) return false;
    }
    draftRef.current.mode = id;
    setInputMode(id);
    setSubmitError('');
    if (text !== undefined) {
      draftRef.current.text = text;
      setDraft(text);
      getComposerModeState(scopeRef.current, id).metadata = metadata;
    }
    if (!(metadata && typeof metadata === 'object' && 'focus' in metadata && metadata.focus === false)) {
      requestAnimationFrame(() => { if (alive.current) textareaRef.current?.focus(); });
    }
    return true;
  }, []);
  const setComposerDraft = useCallback((text: string) => {
    if (!alive.current || busyRef.current || typeof text !== 'string') return false;
    draftRef.current.text = text;
    setDraft(text);
    return true;
  }, []);
  const getComposerDraft = useCallback(() => ({
    text: draftRef.current.text, mode: draftRef.current.mode, busy: busyRef.current,
    attachments: draftRef.current.attachments.map(({ name }) => ({ name })),
    sessionId: contextSessionId || preparedSessionRef.current,
  }), [contextSessionId]);
  const registerMode = useCallback((mode: ComposerMode) => {
    if (!mode.id || typeof mode.onSubmit !== 'function') throw new Error('无效的输入方式。');
    const previous = modes.current.get(mode.id);
    if (previous) throw new Error('输入方式已注册。');
    modes.current.set(mode.id, mode);
    if (mode.command) knownCommands.current.add(mode.command);
    setModeRevision(value => value + 1);
    return () => {
      if (modes.current.get(mode.id) !== mode) return;
      modes.current.delete(mode.id);
      submission.current?.abort();
      if (alive.current) {
        setModeRevision(value => value + 1);
        if (draftRef.current.mode === mode.id) setSubmitError('输入插件已卸载。草稿已保留，请重新选择输入方式。');
      }
    };
  }, []);
  const composer = useMemo<ComposerApi>(() => ({ registerMode, setMode, getState: getModeState,
    getDraft: getComposerDraft, setDraft: setComposerDraft, mode: inputMode, busy: submitBusy,
  }), [registerMode, setMode, getModeState, getComposerDraft, setComposerDraft, inputMode, submitBusy]);
  const skipDraftSave = useRef(false);
  useLayoutEffect(() => {
    if (scopeRef.current === scope) return;
    skipDraftSave.current = true;
    submission.current?.abort();
    scopeRef.current = scope;
    preparedSessionRef.current = '';
    setPreparedSessionId('');
    const saved = composerDrafts.get(scope);
    setDraft(saved?.text || '');
    setAttachments(saved?.attachments || []);
    setInputMode(saved?.mode || '');
    setSubmitError('');
    setSubmitBusy(false);
    busyRef.current = false;
  }, [scope]);
  useEffect(() => {
    if (skipDraftSave.current) { skipDraftSave.current = false; return; }
    composerDrafts.set(scope, { text: draft, attachments, mode: inputMode });
  }, [scope, draft, attachments, inputMode]);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; submission.current?.abort(); };
  }, []);
  const activeMode = modes.current.get(inputMode);
  const commandMode = Array.from(modes.current.values()).find(mode => mode.command === draft.trim().split(/\s/, 1)[0]);
  const modeIntent = Boolean(inputMode || commandMode || /^\/goal(?:\s|$)/.test(draft.trim()));
  void modeRevision;
  const inputSlotData = useMemo(
    () => ({
      conversationId: activeConversationId || '',
      sessionId: contextSessionId || preparedSessionId,
      isGenerating,
      composerDisabled,
      rerollTargetId,
      isChildSession: isTemporaryView, // Legacy navigation hint for existing slot consumers.
      isTemporaryView,
      composerScope: scope,
      inputMode,
      rightPanelOpen,
      panelId,
    }),
    [activeConversationId, contextSessionId, preparedSessionId, isGenerating, composerDisabled, rerollTargetId, isTemporaryView, scope, inputMode, rightPanelOpen, panelId],
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
  const showStop = isGenerating && !hasDraft && !modeIntent;
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

  // Keep attachment previews with their session draft on composer remount.

  // The panel and form share one transaction, including uploads and session creation.
  const submitDraft = async (modeOnly = false): Promise<boolean> => {
    const { text: draft, mode: inputMode, attachments } = draftRef.current;
    if (modeOnly && (!inputMode || !modes.current.has(inputMode))) throw new Error('请先选择已注册的输入方式。');
    if (!modeOnly && showStop) {
      await cancelCurrentTask();
      return true;
    }
    if (composerDisabled || busyRef.current) {
      if (modeOnly) throw new Error('输入框正在提交或当前不可用。');
      return false;
    }
    if (!modeOnly && !draft.trim() && !attachments.length && !modeIntent) return false;
    const resolved = resolveComposerSubmission(draft, inputMode, modes.current.values(), knownCommands.current);
    if (resolved.mode) {
      if (resolved.text.length > 16000) throw new Error('目标正文不能超过 16000 字符。');
      // Reject obvious overflow before uploading files. The plugin checks final paths too.
      if (resolved.text.length + attachments.reduce((n, item) => n + item.name.length + (item.path?.length || 80) + 16, 0) > 16000) {
        throw new Error('目标正文与附件路径总长度超过限制，请减少内容或附件。');
      }
      const currentScope = scopeRef.current;
      const controller = new AbortController();
      submission.current = controller;
      busyRef.current = true;
      setSubmitBusy(true);
      setSubmitError('');
      const generation = captureStoreGeneration();
      const current = () => {
        const state = useChatStore.getState();
        const sameView = isTemporaryView
          ? state.viewingChildSessionId === contextSessionId
          : !state.viewingChildSessionId && state.activeConversationId === activeConversationId;
        return alive.current && !controller.signal.aborted && scopeRef.current === currentScope
          && sameView && isCurrentStoreGeneration(generation);
      };
      const welcomeState = getComposerModeState('welcome', '');
      const cid = isTemporaryView ? '' : activeConversationId || String(welcomeState.conversationId ||= createConversationId());
      const targetKey = conversationKey || `web:${cid}`;
      try {
        if (!current()) return false;
        const sid = contextSessionId || preparedSessionRef.current || await ensureComposerSession(cid, current);
        if (!current()) return false;
        if (!isTemporaryView) {
          preparedSessionRef.current = sid;
          setPreparedSessionId(sid);
        }
        const uploaded = await uploadComposerAttachments([...attachments], targetKey, uploadAttachment, current);
        if (!current()) return false;
        // Temporary views already own a session: never bind it to the selected sidebar row.
        if (!isTemporaryView) bindComposerSession(cid, sid, resolved.text, false);
        await resolved.mode.onSubmit({ text: resolved.text, attachments: uploaded, sessionId: sid,
          conversationKey: targetKey, requestId: createClientRequestId(), signal: controller.signal,
          state: getComposerModeState(currentScope, resolved.mode.id) });
        if (!current()) return false;
        revokeLocalAttachmentUrls(attachments);
        composerDrafts.delete(currentScope);
        draftRef.current = { text: '', attachments: [], mode: '' };
        setDraft(''); setAttachments([]); setInputMode('');
        if (!isTemporaryView && !activeConversationId) {
          delete welcomeState.conversationId;
          bindComposerSession(cid, sid, resolved.text, true);
        }
        return true;
      } catch (error) {
        if (current()) setSubmitError(error instanceof Error ? error.message : String(error));
        throw error;
      } finally {
        if (submission.current === controller) {
          submission.current = null;
          if (alive.current) { busyRef.current = false; setSubmitBusy(false); }
        }
      }
    }

    const text = draft.trim();
    const currentAttachments = [...attachments];
    draftRef.current = { ...draftRef.current, text: '', attachments: [] };
    setDraft('');
    setAttachments([]);
    revokeLocalAttachmentUrls(currentAttachments);
    if (isGenerating) {
      await preemptCurrentTask(text, currentAttachments.length > 0 ? currentAttachments : undefined);
    } else {
      await onSend(text, currentAttachments.length > 0 ? currentAttachments : undefined);
    }
    return true;
  };
  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const submittedScope = scopeRef.current;
    const generation = captureStoreGeneration();
    try { await submitDraft(); }
    catch (error) {
      if (alive.current && scopeRef.current === submittedScope && isCurrentStoreGeneration(generation)) {
        setSubmitError(error instanceof Error ? error.message : String(error));
      }
    }
  };

  useEffect(() => {
    const matchesSession = (metadata: unknown) => {
      const source = metadata && typeof metadata === 'object' ? metadata as Record<string, unknown> : null;
      const current = useChatStore.getState();
      const sid = contextSessionId || preparedSessionRef.current;
      const currentSid = current.viewingChildSessionId
        || current.conversations.find(item => item.id === current.activeConversationId)?.sessionId || preparedSessionRef.current;
      const sameView = isTemporaryView
        ? current.viewingChildSessionId === contextSessionId
        : !current.viewingChildSessionId && current.activeConversationId === activeConversationId;
      return alive.current && sameView && scopeRef.current === scope
        && currentSid === sid && typeof source?.sessionId === 'string'
        && (source.sessionId === 'no-session' ? '' : source.sessionId) === sid;
    };
    const dispose = [
      registerHostAction('setComposerMode', (id, text, metadata) => {
        if (!matchesSession(metadata) || typeof id !== 'string' || (text !== undefined && typeof text !== 'string')) return false;
        return setMode(id, text, metadata);
      }),
      registerHostAction('getComposerDraft', metadata => {
        if (!matchesSession(metadata)) throw new Error('输入框会话已失效。');
        return getComposerDraft();
      }),
      registerHostAction('setComposerDraft', (text, metadata) => {
        if (!matchesSession(metadata) || typeof text !== 'string') return false;
        return setComposerDraft(text);
      }),
      registerHostAction('submitComposer', async metadata => {
        if (!matchesSession(metadata)) throw new Error('输入框会话已失效。');
        return submitDraft(true);
      }),
    ];
    return () => { for (const unregister of dispose.reverse()) unregister(); };
  });

  const handleDraftChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setComposerDraft(event.target.value);
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
    if (!showStop && !canSend && !modeIntent) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  const cycleApprovalLevel = () => {
    // Only an explicit choice replaces custom or policy-default session rules.
    const currentIndex = approvalLevel === 'custom' ? -1 : APPROVAL_LEVEL_ORDER.indexOf(approvalLevel);
    const nextIndex = currentIndex >= 0 ? (currentIndex + 1) % APPROVAL_LEVEL_ORDER.length : 0;
    approval.setLevel(APPROVAL_LEVEL_ORDER[nextIndex]);
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
    <div className="relative mx-auto max-w-3xl px-2 pb-2 pt-1 sm:px-3 sm:pb-3">
      {/* [AutoC 2026-08-22] Floating slot above the input bar. Why: quick-action
          chips need to hover over the message area without consuming layout
          height. How: absolutely positioned layer pinned to this container's top
          edge, right-aligned flex wrap; PluginSlotHost renders nothing when the
          slot has no contribution. */}
      <div className="pointer-events-none absolute inset-x-2 bottom-full z-10 flex items-end justify-between gap-2 sm:inset-x-3">
        <PluginSlotHost slot="input_status" data={inputSlotData} composer={composer}
          className="pointer-events-auto mb-1 min-w-0 flex-1" />
        {/* [2026-09-08] Why: this strip used to span the composer's full width
            with pointer-events-auto, so it swallowed clicks meant for the chat
            flow above it. How: shrink the row to its content width and pin it
            right; the completer dropdown still expands when open. Purpose: only
            real chip/dropdown pixels intercept pointer events. */}
        <PluginSlotHost
          className="pointer-events-auto mb-1 ml-auto flex w-fit max-w-full flex-wrap items-center justify-end gap-1"
          data={inputSlotData}
          slot="input_above"
        />
      </div>
      {/* [2026-06-05] Why: previews should not consume the bordered composer body.
          How: keep the existing preview list above the form container. Purpose:
          attachments remain visible while the textarea and toolbar share one border. */}
      <AttachmentList attachments={attachments} onRemove={submitBusy ? () => {} : removeAttachment} variant="composer" />
      {/* Content-width controls; match the attachment row's 8px inset plus the form's 1px border. */}
      <PluginSlotHost slot="input_controls" data={inputSlotData} composer={composer}
        className="mx-[calc(0.5rem+1px)] mb-1 flex w-fit min-w-0 flex-wrap items-center gap-1" />
      <form
        className={`relative border border-[var(--duties-border)] transition-colors focus-within:border-[var(--duties-text)] ${isDraggingFiles ? 'border-blue-500 bg-blue-50/40' : ''}`}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
        onSubmit={handleSubmit}
      >
        {submitError && <p role="alert" className="m-0 border-b border-[var(--duties-border)] bg-[var(--duties-panel)] px-3 py-1 text-xs leading-relaxed text-[var(--duties-secondary)]">{submitError}</p>}
        {inputMode && !activeMode && <button type="button" onClick={() => setMode('')}
          className="mx-3 mt-1 border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2 py-1 font-mono text-xs text-[var(--duties-text)] transition-colors hover:border-[var(--duties-text)] focus-visible:outline-2 focus-visible:outline-offset-1">改为普通消息（保留正文）</button>}
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
          placeholder={activeMode?.placeholder || commandMode?.placeholder || (isGenerating ? '输入追加指令… Enter 发送 / 留空按 Enter 停止' : '输入消息… Enter 发送 / Shift+Enter 换行')}
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
              className="inline-flex h-8 items-center gap-1 rounded-lg border border-[var(--duties-border)] px-2 font-mono text-xs text-[var(--duties-secondary)] transition-all duration-150 hover:bg-zinc-500/10 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 disabled:opacity-50"
              disabled={approval.busy}
              onClick={cycleApprovalLevel}
              title={`${approval.sessionId ? '当前会话' : '新会话默认'}审批规则：${approvalDisplay.label}`}
              type="button"
            >
              {/* [2026-06-05] Why: the requested compact label includes both an
                  icon and a short Chinese name. How: keep a literal space between
                  the two rendered values instead of relying only on flex gap. Purpose:
                  the visible text and tests match the documented manual/smart/yolo labels. */}
              <Icon name={approvalDisplay.icon} size={16} />
              <span>{approval.busy ? '读取中' : approvalDisplay.label}</span>
            </button>
            {approval.error && <span role="alert" className="max-w-48 text-xs leading-relaxed text-[var(--duties-secondary)]">{approval.error}</span>}
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
                    const strokeColor = contextUsage.utilization >= 0.75 ? '#ef4444' : contextUsage.utilization >= 0.5 ? '#eab308' : 'var(--duties-secondary)';
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
            {isGenerating && modeIntent && <button type="button" aria-label="停止当前任务"
              className="inline-flex h-8 items-center border border-[var(--duties-border)] px-2 text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] focus-visible:outline-2 focus-visible:outline-offset-1"
              onClick={() => void cancelCurrentTask()}><Icon name="stop" size={20} /></button>}
            <button
              aria-label={modeIntent ? '提交目标并开始' : showStop ? '停止当前任务' : '发送消息'}
              className={`inline-flex h-8 items-center justify-center px-2 text-xs leading-relaxed transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 disabled:cursor-not-allowed disabled:opacity-30 ${modeIntent ? 'border border-[var(--duties-text)] bg-[var(--duties-text)] font-mono text-[var(--duties-bg)] hover:bg-[var(--duties-accent)] hover:text-[var(--duties-text)]' : 'text-[var(--duties-secondary)] hover:bg-[var(--duties-panel)]'}`}
              disabled={!showStop && (composerDisabled || !canSend)}
              title={modeIntent ? '提交目标并立即开始' : showStop ? '停止' : isGenerating ? '追加指令' : '发送'}
              type="submit"
            >
              {modeIntent ? (submitBusy ? '提交中…' : activeMode?.label || commandMode?.label || '开始目标') : <Icon name={showStop ? 'stop' : isGenerating ? 'quick_phrases' : 'send'} size={20} />}
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
