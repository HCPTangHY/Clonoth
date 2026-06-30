// ── History Hydration & Tool Result Parsing ────────────────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts. Handles:
//   - Structured history → WsMessage conversion (hydrateStructuredHistory)
//   - Tool result index building from /history responses
//   - History content deduplication for preserve-mode hydration
//   - Block factories (text, thinking, tool)
//   - Message CRUD on ChatState (append, remove)

import type { StructuredMessage, StructuredThinkingBlock } from '../api/supervisorClient';
import type {
  Attachment,
  ChatState,
  MessageStatus,
  NoticeBlock,
  RenderBlock,
  TextBlock,
  ThinkingBlock,
  ToolBlock,
  ToolExecution,
  ToolStatus,
  WsMessage,
} from '../types/message';
import {
  CONTROL_TOOL_NAMES,
  INTERNAL_USER_MESSAGE_TYPES,
  collapseForPreview,
  isRecord,
  stringifyContent,
  truncateForPreview,
  isDispatchResultHistoryMessage,
} from './chatTypes';
import type { HistoryThinkingSegment, HistoryToolCall, HistoryToolResult } from './chatTypes';

// ── Tool Argument Summary ──────────────────────────────────────────────────

export function summarizeArguments(toolName: string, args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return '';
  if (CONTROL_TOOL_NAMES.has(toolName) && typeof args.text === 'string') return '';

  return Object.entries(args)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${truncateForPreview(collapseForPreview(stringifyContent(value)), 80)}`)
    .join(', ');
}

// ── History Tool Result Index ──────────────────────────────────────────────

export function buildHistoryToolResultIndex(messages: readonly StructuredMessage[]): Map<string, HistoryToolResult> {
  const resultIndex = new Map<string, HistoryToolResult>();

  for (const message of messages) {
    if (message.role !== 'tool' || !message.tool_call_id) continue;

    const toolName = message.tool_name || message.name || '';
    const rawContent = stringifyContent(message.content);
    const trimmed = rawContent.trim();
    const rejected = Boolean(message.tool_rejected);
    const status: HistoryToolResult['status'] = rejected || /^ERROR(?:\b|:)/i.test(trimmed) ? 'error' : 'success';
    const isAutoResult = status === 'success' && CONTROL_TOOL_NAMES.has(toolName) && trimmed.toLowerCase() === 'ok';

    const structured = (message as unknown as Record<string, unknown>).tool_result_structured;
    let resolvedResult: unknown;
    if (isAutoResult) {
      resolvedResult = undefined;
    } else if (status === 'error') {
      resolvedResult = rawContent;
    } else if (typeof structured === 'object' && structured !== null) {
      resolvedResult = structured;
    } else {
      try {
        const parsed = JSON.parse(rawContent);
        if (typeof parsed === 'object' && parsed !== null) {
          resolvedResult = parsed;
        } else {
          resolvedResult = truncateForPreview(collapseForPreview(rawContent), 120) || undefined;
        }
      } catch {
        resolvedResult = truncateForPreview(collapseForPreview(rawContent), 120) || undefined;
      }
    }

    const rawInline = message.tool_result_raw_inline || message.raw_inline || rawContent;
    const elapsedMs = typeof message.tool_result_elapsed_ms === 'number'
      ? message.tool_result_elapsed_ms
      : typeof message.elapsed_ms === 'number'
        ? message.elapsed_ms
        : undefined;
    const attachments = mergeHistoryAttachments(
      normalizeHistoryAttachments(message),
      normalizeHistoryAttachmentList(message.tool_attachments),
      extractHistoryAttachmentsFromValue(structured),
      extractHistoryAttachmentsFromValue(rawContent),
    );

    resultIndex.set(message.tool_call_id, {
      status,
      rawInline,
      format: message.tool_result_format || message.format || undefined,
      elapsedMs,
      summary: message.tool_result_summary || message.tool_summary || undefined,
      attachments: attachments.length > 0 ? attachments : undefined,
      rejected: rejected || undefined,
      rejectionCode: message.tool_rejection_code || undefined,
      resultVisibility: message.tool_result_visibility || undefined,
      isAutoResult: isAutoResult || undefined,
      result: resolvedResult,
    });
  }

  return resultIndex;
}

// ── History Tool Call Normalization ─────────────────────────────────────────

export function normalizeHistoryToolCalls(value: StructuredMessage['tool_calls']): HistoryToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.map((toolCall, index) => ({
    id: toolCall.id || `history-tool-${index}`,
    name: toolCall.name || 'unknown',
    arguments: isRecord(toolCall.arguments) ? toolCall.arguments : undefined,
  }));
}

// ── History Thinking Segments ──────────────────────────────────────────────

function getHistoryString(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : String(value);
}

function safeDecodeHistoryPath(value: string): string {
  try { return decodeURIComponent(value); } catch { return value; }
}

function getHistoryAttachmentName(path: string, fallback = '附件'): string {
  const clean = path.replace(/\\/g, '/').split('?')[0].split('#')[0];
  return safeDecodeHistoryPath(clean.split('/').pop() || fallback);
}

function normalizeHistoryAttachmentPath(value: unknown): Attachment | null {
  const path = getHistoryString(value).replace(/^file:\/\//, '').replace(/^\/+/, '').trim();
  if (!path) return null;
  const imageLike = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(path);
  return {
    name: getHistoryAttachmentName(path, imageLike ? '图片' : '附件'),
    type: imageLike ? 'image' : 'file',
    path,
    mime_type: imageLike ? 'image/*' : undefined,
  };
}

function normalizeHistoryAttachmentItem(value: unknown): Attachment | null {
  if (typeof value === 'string') return normalizeHistoryAttachmentPath(value);
  if (!isRecord(value)) return null;
  const path = getHistoryString(value.path).replace(/^file:\/\//, '').replace(/^\/+/, '');
  const url = getHistoryString(value.url) || undefined;
  const name = getHistoryString(value.name) || getHistoryAttachmentName(path || url || '', '附件');
  const mimeType = getHistoryString(value.mime_type) || undefined;
  const type = getHistoryString(value.type) || (mimeType?.startsWith('image/') ? 'image' : undefined);
  return {
    name,
    size: typeof value.size === 'number' && Number.isFinite(value.size) ? value.size : undefined,
    url,
    type,
    path: path || undefined,
    mime_type: mimeType,
  };
}

function normalizeHistoryAttachmentList(value: unknown): Attachment[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => normalizeHistoryAttachmentItem(item))
    .filter((item): item is Attachment => Boolean(item));
}

function parseHistoryJsonRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function extractHistoryAttachmentsFromValue(value: unknown, depth = 0): Attachment[] {
  if (depth > 5) return [];
  if (typeof value === 'string') {
    const parsed = parseHistoryJsonRecord(value);
    return parsed ? extractHistoryAttachmentsFromValue(parsed, depth + 1) : [];
  }
  if (Array.isArray(value)) {
    return mergeHistoryAttachments(
      normalizeHistoryAttachmentList(value),
      ...value.map((item) => extractHistoryAttachmentsFromValue(item, depth + 1)),
    );
  }
  if (!isRecord(value)) return [];

  const direct = mergeHistoryAttachments(
    normalizeHistoryAttachmentList(value.attachments),
    normalizeHistoryAttachmentList(value.tool_attachments),
    normalizeHistoryAttachmentList(value.tool_result_attachments),
    normalizeHistoryAttachmentList(value.attachment_paths),
    normalizeHistoryAttachmentList(value.attachmentPaths),
  );

  const nested = mergeHistoryAttachments(
    ...['data', 'result', 'output', 'payload'].map((key) => extractHistoryAttachmentsFromValue(value[key], depth + 1)),
  );

  return mergeHistoryAttachments(direct, nested);
}

function normalizeHistoryAttachments(message: StructuredMessage): Attachment[] {
  const attachments: Attachment[] = [];
  const seen = new Set<string>();
  const add = (attachment: Attachment | null) => {
    if (!attachment) return;
    const key = attachment.path || attachment.url || attachment.name;
    if (key && seen.has(key)) return;
    if (key) seen.add(key);
    attachments.push(attachment);
  };

  if (Array.isArray(message.attachments)) {
    for (const item of message.attachments) add(normalizeHistoryAttachmentItem(item));
  }

  if (Array.isArray(message.content)) {
    for (const part of message.content) {
      if (!isRecord(part)) continue;
      const type = getHistoryString(part.type);
      if (type === 'image_url' && isRecord(part.image_url)) {
        const rawUrl = getHistoryString(part.image_url.url);
        const path = rawUrl.replace(/^file:\/\//, '').replace(/^\/+/, '');
        add({
          name: getHistoryAttachmentName(path || rawUrl, '图片'),
          type: 'image',
          path: path.startsWith('data/attachments/') ? path : undefined,
          url: path.startsWith('data/attachments/') ? undefined : rawUrl,
          mime_type: 'image/*',
        });
      }
      if (type === 'text') {
        const text = getHistoryString(part.text);
        const pattern = /\[Attached file:\s*([^|\]]+?)\s*\|\s*type:\s*([^|\]]+?)\s*\|\s*path:\s*([^\]]+?)\]/g;
        for (const match of text.matchAll(pattern)) {
          const path = match[3].trim().replace(/^file:\/\//, '').replace(/^\/+/, '');
          add({
            name: match[1].trim() || getHistoryAttachmentName(path),
            type: 'file',
            path: path || undefined,
            mime_type: match[2].trim() || undefined,
          });
        }
      }
    }
  }

  if (Array.isArray(message.tool_calls)) {
    for (const toolCall of message.tool_calls) {
      const args = isRecord(toolCall.arguments) ? toolCall.arguments : undefined;
      const paths = args?.attachment_paths;
      if (Array.isArray(paths)) {
        for (const item of paths) add(isRecord(item) ? normalizeHistoryAttachmentItem(item) : normalizeHistoryAttachmentPath(item));
      }
      const directAttachments = args?.attachments;
      if (Array.isArray(directAttachments)) {
        for (const item of directAttachments) add(isRecord(item) ? normalizeHistoryAttachmentItem(item) : normalizeHistoryAttachmentPath(item));
      }
    }
  }

  return attachments;
}

function mergeHistoryAttachments(...groups: readonly Attachment[][]): Attachment[] {
  const result: Attachment[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const attachment of group) {
      const key = attachment.path || attachment.url || attachment.name;
      if (key && seen.has(key)) continue;
      if (key) seen.add(key);
      result.push(attachment);
    }
  }
  return result;
}

function getHistoryTextContent(message: StructuredMessage): string {
  if (!Array.isArray(message.content)) return stringifyContent(message.content);
  return message.content
    .map((part) => (isRecord(part) && getHistoryString(part.type) === 'text' ? getHistoryString(part.text) : ''))
    .filter((text) => text.trim() && !/^\[Attached file:/i.test(text.trim()))
    .join('\n');
}

export function normalizeHistoryThinkingSegments(message: StructuredMessage): HistoryThinkingSegment[] {
  const normalizeBlock = (block: StructuredThinkingBlock): HistoryThinkingSegment | null => {
    const text = typeof block.text === 'string' ? block.text : '';
    if (!text.trim()) return null;
    return {
      text,
      startedAt: typeof block.started_at === 'string' ? block.started_at : undefined,
      endedAt: typeof block.ended_at === 'string' ? block.ended_at : undefined,
    };
  };

  const structuredBlocks = Array.isArray(message.thinking_blocks)
    ? message.thinking_blocks.map(normalizeBlock).filter((block): block is HistoryThinkingSegment => Boolean(block))
    : [];
  if (structuredBlocks.length > 0) return structuredBlocks;

  if (isRecord(message.thinking)) {
    const normalized = normalizeBlock(message.thinking as StructuredThinkingBlock);
    return normalized ? [normalized] : [];
  }

  const text = typeof message.thinking === 'string' ? message.thinking : message.thinking_text || '';
  if (!text.trim()) return [];

  return [{
    text,
    startedAt: message.reasoning_started_at,
    endedAt: message.reasoning_ended_at,
  }];
}

// ── Control Tool Text Extraction ───────────────────────────────────────────

export function extractControlToolText(toolCalls: readonly HistoryToolCall[]): { text: string; toolName?: string } {
  for (const toolCall of toolCalls) {
    if (!CONTROL_TOOL_NAMES.has(toolCall.name)) continue;
    const text = typeof toolCall.arguments?.text === 'string' ? toolCall.arguments.text : '';
    if (text) return { text, toolName: toolCall.name };
  }
  return { text: '' };
}

function historyToolStatus(result: HistoryToolResult | undefined): ToolStatus {
  if (!result) return 'success';
  return result.status;
}

// ── History Tool Execution Factory ─────────────────────────────────────────

export function createHistoryToolExecutions(
  toolCalls: readonly HistoryToolCall[],
  resultIndex: Map<string, HistoryToolResult>,
  messageId: string,
  createdAt: string,
  sessionId: string,
  nodeId?: string,
): ToolExecution[] {
  return toolCalls.map((toolCall, index) => {
    const result = toolCall.id ? resultIndex.get(toolCall.id) : undefined;
    const stableId = `${messageId}|history-tool:${toolCall.id || index}`;
    return {
      stableId,
      messageId,
      id: toolCall.id,
      index,
      name: toolCall.name,
      status: historyToolStatus(result),
      arguments: toolCall.arguments,
      argumentsText: toolCall.arguments ? JSON.stringify(toolCall.arguments) : undefined,
      summary: result?.summary || summarizeArguments(toolCall.name, toolCall.arguments),
      result: result?.result,
      rawInline: result?.rawInline,
      format: result?.format,
      elapsedMs: result?.elapsedMs,
      attachments: result?.attachments,
      rejected: result?.rejected,
      rejectionCode: result?.rejectionCode,
      resultVisibility: result?.resultVisibility,
      hidden: result?.resultVisibility === 'hidden' || (CONTROL_TOOL_NAMES.has(toolCall.name) && result?.isAutoResult && !result?.rejected),
      nodeId,
      createdAt,
      updatedAt: createdAt,
      eventIds: [`history:${sessionId}:${messageId}:tool:${index}`],
    } satisfies ToolExecution;
  });
}

// ── Block Factories ────────────────────────────────────────────────────────

export function createTextBlock(
  id: string,
  createdAt: string,
  text: string,
  delivery: TextBlock['delivery'] = 'history',
): TextBlock {
  return {
    id,
    kind: 'text',
    text,
    delivery,
    streaming: false,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

export function createThinkingBlock(
  id: string,
  createdAt: string,
  text: string,
  startedAt?: string,
  endedAt?: string,
): ThinkingBlock {
  return {
    id,
    kind: 'thinking',
    text,
    streaming: false,
    startedAt,
    endedAt,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

export function createToolBlock(id: string, createdAt: string, toolIds: readonly string[]): ToolBlock {
  return {
    id,
    kind: 'tool',
    toolIds: [...toolIds],
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

// ── Message CRUD ───────────────────────────────────────────────────────────

export function appendHistoryMessage(
  state: ChatState, message: WsMessage, tools: readonly ToolExecution[],
  prepend = false,
): ChatState {
  const order = state.messageOrderByConversation[message.conversationId] || [];
  const nextToolExecutionsById = { ...state.toolExecutionsById };
  const nextToolExecutionOrder = [...state.toolExecutionOrder];

  for (const tool of tools) {
    nextToolExecutionsById[tool.stableId] = tool;
    if (!nextToolExecutionOrder.includes(tool.stableId)) nextToolExecutionOrder.push(tool.stableId);
  }

  let nextOrder: string[];
  if (order.includes(message.id)) {
    nextOrder = [...order];
  } else {
    nextOrder = prepend ? [message.id, ...order] : [...order, message.id];
  }

  return {
    ...state,
    messagesById: { ...state.messagesById, [message.id]: message },
    messageOrderByConversation: {
      ...state.messageOrderByConversation,
      [message.conversationId]: nextOrder,
    },
    toolExecutionsById: nextToolExecutionsById,
    toolExecutionOrder: nextToolExecutionOrder,
  };
}

export function removeConversationMessages(state: ChatState, conversationId: string): ChatState {
  const removedMessageIds = new Set(state.messageOrderByConversation[conversationId] || []);
  if (removedMessageIds.size === 0) return state;

  const messagesById = { ...state.messagesById };
  for (const messageId of removedMessageIds) delete messagesById[messageId];

  const messageOrderByConversation = { ...state.messageOrderByConversation };
  delete messageOrderByConversation[conversationId];

  const toolExecutionsById = { ...state.toolExecutionsById };
  const toolExecutionOrder = state.toolExecutionOrder.filter((toolId) => {
    const tool = state.toolExecutionsById[toolId];
    if (tool && removedMessageIds.has(tool.messageId)) {
      delete toolExecutionsById[toolId];
      return false;
    }
    return true;
  });

  const approvalBlockById = { ...state.approvalBlockById };
  for (const [approvalId, location] of Object.entries(state.approvalBlockById)) {
    if (removedMessageIds.has(location.messageId)) delete approvalBlockById[approvalId];
  }

  return { ...state, messagesById, messageOrderByConversation, toolExecutionsById, toolExecutionOrder, approvalBlockById };
}

// ── Content Deduplication Helpers ──────────────────────────────────────────

function getHistoryEventId(sessionId: string, sourceMessageId: string | undefined, messageId: string): string {
  return `history:${sessionId}:${sourceMessageId || messageId}`;
}

function getMessageTextForHistoryDedupe(message: WsMessage): string {
  return message.blocks
    .filter((block): block is TextBlock => block.kind === 'text')
    .map((block) => block.text)
    .join('\n');
}

function createHistoryContentSignature(role: WsMessage['role'], text: string): string | undefined {
  const compact = collapseForPreview(text);
  return compact ? `${role}:${compact}` : undefined;
}

// ── Main Hydration Function ────────────────────────────────────────────────

export function hydrateStructuredHistory(
  state: ChatState,
  sessionId: string,
  conversationId: string,
  history: readonly StructuredMessage[],
  preserveExistingMessages = false,
): ChatState {
  const resultIndex = buildHistoryToolResultIndex(history);
  let nextState = preserveExistingMessages ? state : removeConversationMessages(state, conversationId);
  const existingSourceIds = new Set<string>();
  const existingMessageIds = new Set<string>();
  const existingContentSignatures = new Set<string>();

  if (preserveExistingMessages) {
    for (const id of nextState.messageOrderByConversation[conversationId] || []) {
      const existingMessage = nextState.messagesById[id];
      if (!existingMessage) continue;
      existingMessageIds.add(existingMessage.id);
      for (const eventId of existingMessage.eventIds || []) {
        if (eventId.startsWith(`history:${sessionId}:`)) existingSourceIds.add(eventId);
      }
      const signature = createHistoryContentSignature(existingMessage.role, getMessageTextForHistoryDedupe(existingMessage));
      if (signature) existingContentSignatures.add(signature);
    }
  }

  const shouldSkipPreservedHistoryMessage = (
    sourceEventId: string,
    messageId: string,
    role: WsMessage['role'],
    text: string,
  ): boolean => {
    if (!preserveExistingMessages) return false;
    if (existingSourceIds.has(sourceEventId) || existingMessageIds.has(messageId)) return true;
    const signature = createHistoryContentSignature(role, text);
    return Boolean(signature && existingContentSignatures.has(signature));
  };

  const rememberHydratedHistoryMessage = (sourceEventId: string, messageId: string) => {
    if (!preserveExistingMessages) return;
    existingSourceIds.add(sourceEventId);
    existingMessageIds.add(messageId);
  };

  let accumulatedThinking: HistoryThinkingSegment[] = [];
  let accumulatedTools: ToolExecution[] = [];
  let accumulatedCreatedAt = '';

  const resetAccumulatedAssistant = () => {
    accumulatedThinking = [];
    accumulatedTools = [];
    accumulatedCreatedAt = '';
  };

  const pushAssistantMessage = (
    sourceMessage: StructuredMessage,
    text: string,
    currentTools: readonly ToolExecution[],
    completionType?: WsMessage['completionType'],
  ) => {
    const createdAt = sourceMessage.created_at || accumulatedCreatedAt || new Date().toISOString();
    const messageId = `message:${conversationId}:history:${sourceMessage.id || nextState.messageOrderByConversation[conversationId]?.length || 0}`;
    const historyEventId = getHistoryEventId(sessionId, sourceMessage.id, messageId);
    if (shouldSkipPreservedHistoryMessage(historyEventId, messageId, 'assistant', text)) {
      resetAccumulatedAssistant();
      return;
    }
    const thinkingSegments = [
      ...accumulatedThinking,
      ...normalizeHistoryThinkingSegments(sourceMessage),
    ].filter((item) => item.text.trim());
    const tools = [...accumulatedTools, ...currentTools].map((tool) => ({
      ...tool,
      messageId,
    }));
    const attachments = mergeHistoryAttachments(
      ...tools.map((tool) => tool.attachments || []),
      normalizeHistoryAttachments(sourceMessage),
    );
    const blocks: RenderBlock[] = [];

    for (const [index, thinking] of thinkingSegments.entries()) {
      blocks.push(createThinkingBlock(
        `${messageId}|block:thinking:history:${index}`,
        createdAt,
        thinking.text,
        thinking.startedAt,
        thinking.endedAt,
      ));
    }
    const textDelivery: TextBlock['delivery'] = completionType === 'reply'
      ? 'intermediate'
      : completionType === 'finish'
        ? 'final'
        : 'history';
    if (text) {
      blocks.push(createTextBlock(`${messageId}|block:text:history`, createdAt, text, textDelivery));
    }
    if (tools.length > 0) {
      const blockId = `${messageId}|block:tool:history`;
      blocks.push(createToolBlock(blockId, createdAt, tools.map((tool) => tool.stableId)));
      tools.forEach((tool, index) => { tools[index] = { ...tool, blockId }; });
    }

    const status: MessageStatus = 'completed';
    const message: WsMessage = {
      id: messageId,
      conversationId,
      sessionId,
      role: 'assistant',
      status,
      createdAt,
      updatedAt: createdAt,
      source: {
        nodeId: sourceMessage.source_node_id || undefined,
        taskId: sourceMessage.source_task_id || undefined,
        llmRequestId: sourceMessage.llm_request_id || undefined,
        provider: sourceMessage.provider || undefined,
        providerMetadata: sourceMessage.provider_metadata,
        usage: sourceMessage.usage,
      },
      blocks,
      attachments,
      eventIds: [historyEventId],
      hydratedFromHistory: true,
      ...(completionType && { completionType }),
    };

    nextState = appendHistoryMessage(nextState, message, tools.map((tool) => ({
      ...tool,
      eventIds: [`history:${sessionId}:${messageId}:tool:${tool.index ?? 0}`],
    })));
    rememberHydratedHistoryMessage(historyEventId, messageId);
    resetAccumulatedAssistant();
  };

  const flushDanglingAssistant = () => {
    if (accumulatedThinking.length === 0 && accumulatedTools.length === 0) return;
    pushAssistantMessage({
      id: `dangling-${nextState.messageOrderByConversation[conversationId]?.length || 0}`,
      role: 'assistant',
      content: '',
      created_at: accumulatedCreatedAt || new Date().toISOString(),
    }, '', []);
  };

  for (const message of history) {
    if (message.role === 'user') {
      flushDanglingAssistant();

      const createdAt = message.created_at || new Date().toISOString();
      const messageId = `message:${conversationId}:history:${message.id || `user-${nextState.messageOrderByConversation[conversationId]?.length || 0}`}`;
      const historyEventId = getHistoryEventId(sessionId, message.id, messageId);
      const text = getHistoryTextContent(message);
      const attachments = normalizeHistoryAttachments(message);

      if (message.message_type === 'tool_result_attachment') {
        // [AutoC 2026-06-17] Why: tool-generated attachment rows are LLM-history
        // support messages, not human-authored chat. How: attach their persisted
        // attachment metadata to the previous assistant card. Purpose: refresh shows
        // tool output files on the tool card without creating a fake user message.
        const order = nextState.messageOrderByConversation[conversationId] || [];
        let targetMsg: WsMessage | undefined;
        for (let k = order.length - 1; k >= 0; k--) {
          const candidate = nextState.messagesById[order[k]];
          if (candidate?.role === 'assistant') { targetMsg = candidate; break; }
        }
        if (targetMsg && attachments.length > 0) {
          nextState = {
            ...nextState,
            messagesById: {
              ...nextState.messagesById,
              [targetMsg.id]: {
                ...targetMsg,
                attachments: mergeHistoryAttachments(targetMsg.attachments || [], attachments),
                eventIds: [...targetMsg.eventIds, historyEventId],
              },
            },
          };
        }
        rememberHydratedHistoryMessage(historyEventId, messageId);
        continue;
      }

      if (INTERNAL_USER_MESSAGE_TYPES.has(message.message_type || '') && message.message_type !== 'summary') continue;

      if (message.message_type === 'summary') {
        if (shouldSkipPreservedHistoryMessage(historyEventId, messageId, 'system', text)) continue;
        const isCompactSummary = message.source_task_id === 'compact_summary';
        const summaryBody = isCompactSummary
          ? text.replace(/^\[以下是之前对话的结构化摘要.*?\]\s*/s, '').trim()
          : text.replace(/^\[Task summary.*?\]\s*/s, '').trim();
        const noticeBlock: NoticeBlock = {
          id: `${messageId}|block:notice:${isCompactSummary ? 'compact' : 'turn-summary'}`,
          kind: 'notice',
          level: 'info',
          title: isCompactSummary ? '上下文压缩摘要' : '轮摘要',
          text: summaryBody || text,
          eventType: isCompactSummary ? 'context_compacted' : 'turn_summary',
          createdAt,
          updatedAt: createdAt,
          eventIds: [historyEventId],
        };
        const wsMessage: WsMessage = {
          id: messageId,
          conversationId,
          sessionId,
          role: 'system',
          status: 'completed',
          createdAt,
          updatedAt: createdAt,
          source: {},
          blocks: [noticeBlock],
          eventIds: [historyEventId],
          hydratedFromHistory: true,
        };
        nextState = appendHistoryMessage(nextState, wsMessage, []);
        rememberHydratedHistoryMessage(historyEventId, messageId);
        continue;
      }

      const hydratedRole: WsMessage['role'] = isDispatchResultHistoryMessage(message, text) ? 'dispatch_callback' : 'user';
      if (shouldSkipPreservedHistoryMessage(historyEventId, messageId, hydratedRole, text)) continue;
      if (message.is_preempt) {
        const order = nextState.messageOrderByConversation[conversationId] || [];
        let targetMsg: WsMessage | undefined;
        for (let k = order.length - 1; k >= 0; k--) {
          const candidate = nextState.messagesById[order[k]];
          if (candidate?.role === 'assistant') { targetMsg = candidate; break; }
        }
        if (targetMsg) {
          const noticeBlock: NoticeBlock = {
            id: `${messageId}|block:notice:preempt`,
            kind: 'notice',
            level: 'preempt' as const,
            text,
            createdAt,
            updatedAt: createdAt,
            eventIds: [historyEventId],
          };
          nextState = {
            ...nextState,
            messagesById: {
              ...nextState.messagesById,
              [targetMsg.id]: { ...targetMsg, blocks: [...targetMsg.blocks, noticeBlock] },
            },
          };
          rememberHydratedHistoryMessage(historyEventId, messageId);
          continue;
        }
      }
      const userBlocks: RenderBlock[] = [createTextBlock(`${messageId}|block:text:history`, createdAt, text)];
      const wsMessage: WsMessage = {
        id: messageId,
        conversationId,
        sessionId,
        role: hydratedRole,
        status: 'completed',
        createdAt,
        updatedAt: createdAt,
        source: {
          nodeId: message.child_node_id || message.dispatch_node_id || message.source_node_id || undefined,
          childNodeId: message.child_node_id || message.dispatch_node_id || undefined,
          taskId: message.child_task_id || message.dispatch_task_id || message.source_task_id || undefined,
          childTaskId: message.child_task_id || message.dispatch_task_id || undefined,
          callerNodeId: message.caller_node_id || undefined,
          summary: message.summary || undefined,
          childSessionId: message.child_session_id || undefined,
          llmRequestId: message.llm_request_id || undefined,
          provider: message.provider || undefined,
          providerMetadata: message.provider_metadata,
          usage: message.usage,
        },
        blocks: userBlocks,
        attachments,
        eventIds: [historyEventId],
        hydratedFromHistory: true,
      };
      nextState = appendHistoryMessage(nextState, wsMessage, []);
      rememberHydratedHistoryMessage(historyEventId, messageId);
      continue;
    }

    if (message.role !== 'assistant') continue;

    const pendingMessageId = `message:${conversationId}|history-pending:${message.id || 'assistant'}`;
    const toolCalls = normalizeHistoryToolCalls(message.tool_calls);
    const currentTools = createHistoryToolExecutions(toolCalls, resultIndex, pendingMessageId, message.created_at || new Date().toISOString(), sessionId, message.source_node_id);
    const controlText = extractControlToolText(toolCalls);
    const contentText = stringifyContent(message.content);
    const displayText = contentText.trim() ? contentText : controlText.text;
    const hasControlTool = toolCalls.some((toolCall) => CONTROL_TOOL_NAMES.has(toolCall.name));

    if (hasControlTool && controlText.text) {
      flushDanglingAssistant();
      const ctName = controlText.toolName;
      const ctType: WsMessage['completionType'] =
        ctName === 'finish' ? 'finish' : ctName === 'ask' ? 'ask' : ctName === 'reply' ? 'reply' : undefined;
      pushAssistantMessage(message, controlText.text, currentTools, ctType);
      continue;
    }

    pushAssistantMessage(message, displayText, currentTools);
  }

  flushDanglingAssistant();
  return {
    ...nextState,
    conversationIdsBySession: {
      ...nextState.conversationIdsBySession,
      [sessionId]: conversationId,
    },
  };
}
