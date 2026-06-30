// ── Event Reducer Tool Execution Helpers ───────────────────────────────────
// [AutoC 2026-06-16] Tool stable IDs, tool table updates, tool blocks, and patches.

import type { SupervisorEvent } from '../types/chat';
import type { ChatState, RenderBlock, ToolBlock, ToolExecution, ToolStatus, WsMessage } from '../types/message';
import { CONTROL_TOOL_NAMES, TERMINAL_TOOL_STATUSES, type ToolPatch } from './eventReducerShared';
import { appendUnique, getEventId, getPayload, parseJsonRecord, stringifyJson } from './eventReducerPayload';
import { setMessageStatus } from './eventReducerBlocks';
import { upsertMessage } from './eventReducerState';
import { getOrCreateAssistantMessageForToolPatch } from './eventReducerAssistant';

export function applyToolPatchToAssistant(
  state: ChatState,
  event: SupervisorEvent,
  patch: ToolPatch,
  attachBlock: boolean,
): ChatState {
  // [2026-06-03] Why: a reply/ask card closes one visible assistant round, but the
  // next streamed LLM round can begin with provider tool_call_delta events and no
  // text/thinking token, so stream_delta-only boundary detection misses it. How:
  // route new provider tool deltas through the same post-reply boundary helper while
  // keeping existing tool executions on their original card. Purpose: live streaming
  // uses the same card split points as structured history without moving same-round
  // tool end updates away from the reply card.
  let { state: nextState, message } = getOrCreateAssistantMessageForToolPatch(state, event, patch);

  // [2026-06-12] Debug: log tool patch routing to diagnose card/block placement
  if (typeof console !== 'undefined' && (event.type === 'tool_call_start' || (event.type === 'tool_call_delta' && getPayload(event).event === 'tool_call_start'))) {
    const blockSummary = message.blocks.filter(b => b.kind === 'tool').map(b => `[${b.toolIds.length} tools]`).join(',');
    console.debug('[toolPatch] %s name=%s id=%s msgId=%s blocks=%s roundComplete=%s status=%s',
      event.type, patch.name, patch.id?.slice(0, 16), message.id.slice(0, 24), blockSummary || 'none', message.roundComplete, message.status);
  }

  const toolResult = upsertToolExecution(nextState, message, event, patch);
  nextState = toolResult.state;

  if (attachBlock && !toolResult.tool.hidden) {
    const attached = attachToolToMessage(nextState, message, toolResult.tool, event);
    nextState = attached.state;
    message = attached.message;
  } else {
    message = nextState.messagesById[message.id] || message;
  }

  // [2026-06-10] Why: when a control tool (finish/reply/ask) completes, the
  // message must transition to 'completed' so the card stops spinning. The
  // outbound_message event handles visible text replacement and status, but
  // the tool_call_end for a hidden control tool was leaving the message stuck
  // at 'running_tools'. How: if the tool is a terminal hidden control tool,
  // finalize the message. Purpose: finish cards stop showing a pending spinner.
  if (toolResult.tool.hidden && toolResult.tool.control
    && toolResult.tool.status && TERMINAL_TOOL_STATUSES.has(toolResult.tool.status)) {
    message = {
      ...setMessageStatus(message, 'completed', event),
      roundComplete: true,
    };
  } else {
    message = setMessageStatus(message, 'running_tools', event);
  }
  nextState = upsertMessage(nextState, message);
  return nextState;
}

export function upsertToolExecution(
  state: ChatState,
  message: WsMessage,
  event: SupervisorEvent,
  patch: ToolPatch,
): { state: ChatState; tool: ToolExecution } {
  const identity = resolveToolStableId(state, message, event, patch);
  const current = state.toolExecutionsById[identity.stableId];
  const currentText = current?.argumentsText || '';
  const argumentsTextFromPatch = patch.argumentsText !== undefined
    ? patch.argumentsText
    : patch.argumentsTextDelta !== undefined
      ? `${currentText}${patch.argumentsTextDelta}`
      : patch.arguments !== undefined
        ? stringifyJson(patch.arguments)
        : current?.argumentsText;
  const parsedArguments = patch.arguments !== undefined
    ? patch.arguments
    : parseJsonRecord(argumentsTextFromPatch) || current?.arguments;
  const name = patch.name || current?.name || 'tool';
  const rejected = patch.rejected !== undefined ? patch.rejected : current?.rejected;
  const rejectionCode = patch.rejectionCode !== undefined ? patch.rejectionCode : current?.rejectionCode;
  const resultVisibility = patch.resultVisibility !== undefined ? patch.resultVisibility : current?.resultVisibility;
  // [2026-06-02] Why: a rejected tool result is a failed execution even when a
  // backend payload still carries status=success. How: resolve rejected before status
  // and coerce the visible lifecycle state to error. Purpose: live replay matches the
  // error semantics used by historical rejected tool results.
  const status: ToolStatus = rejected ? 'error' : patch.status || current?.status || 'queued';
  const control = current?.control || CONTROL_TOOL_NAMES.has(name);
  // [2026-06-07] Why: protocol-level rejection presentation is decided by backend
  // structure, not by parsing localized result text. How: honor
  // resultVisibility='hidden' while preserving successful control-tool hiding.
  const hidden = resultVisibility === 'hidden' || (!rejected && ((current?.hidden || false) || (control && status === 'success')));
  const tool: ToolExecution = {
    stableId: identity.stableId,
    messageId: message.id,
    blockId: current?.blockId,
    id: patch.id || current?.id,
    itemId: patch.itemId || current?.itemId,
    index: patch.index !== undefined ? patch.index : current?.index,
    name,
    status,
    arguments: parsedArguments,
    argumentsText: argumentsTextFromPatch,
    summary: patch.summary !== undefined ? patch.summary : current?.summary,
    result: patch.result !== undefined ? patch.result : current?.result,
    rawInline: patch.rawInline !== undefined ? patch.rawInline : current?.rawInline,
    format: patch.format !== undefined ? patch.format : current?.format,
    elapsedMs: patch.elapsedMs !== undefined ? patch.elapsedMs : current?.elapsedMs,
    attachments: patch.attachments !== undefined ? patch.attachments : current?.attachments,
    control,
    rejected,
    rejectionCode,
    resultVisibility,
    hidden,
    error: patch.error || current?.error,
    approvalId: patch.approvalId || current?.approvalId,
    approvalStatus: patch.approvalStatus || current?.approvalStatus,
    approvalDetails: patch.approvalDetails || current?.approvalDetails,
    taskId: patch.taskId || current?.taskId || message.source.taskId,
    nodeId: patch.nodeId || current?.nodeId || message.source.nodeId,
    nodeName: patch.nodeName || current?.nodeName || message.source.nodeName,
    createdAt: current?.createdAt || event.ts,
    updatedAt: event.ts,
    eventIds: appendUnique(current?.eventIds || [], getEventId(event)),
  };

  return {
    state: {
      ...state,
      toolExecutionsById: {
        ...state.toolExecutionsById,
        [identity.stableId]: tool,
      },
      toolExecutionOrder: current ? state.toolExecutionOrder : [...state.toolExecutionOrder, identity.stableId],
      toolStableIdByExternalId: identity.externalKey
        ? { ...state.toolStableIdByExternalId, [identity.externalKey]: identity.stableId }
        : state.toolStableIdByExternalId,
      toolStableIdByIndex: identity.indexKey
        ? { ...state.toolStableIdByIndex, [identity.indexKey]: identity.stableId }
        : state.toolStableIdByIndex,
    },
    tool,
  };
}

export function attachToolToMessage(
  state: ChatState,
  message: WsMessage,
  tool: ToolExecution,
  event: SupervisorEvent,
): { state: ChatState; message: WsMessage; tool: ToolExecution } {
  const existingBlockIndex = message.blocks.findIndex(
    (block) => block.kind === 'tool' && block.toolIds.includes(tool.stableId),
  );
  const lastBlock = message.blocks[message.blocks.length - 1];
  const reusableLastToolBlock = existingBlockIndex < 0 && lastBlock?.kind === 'tool' ? lastBlock : undefined;

  // [2026-06-12] Debug: log block attachment decisions
  if (typeof console !== 'undefined') {
    const action = existingBlockIndex >= 0 ? 'UPDATE_EXISTING' : reusableLastToolBlock ? 'REUSE_LAST' : 'CREATE_NEW';
    const blockToolCount = reusableLastToolBlock && reusableLastToolBlock.kind === 'tool' ? reusableLastToolBlock.toolIds.length : 0;
    console.debug('[attachTool] %s stableId=%s name=%s existingIdx=%d reusableTools=%d totalBlocks=%d',
      action, tool.stableId.slice(-30), tool.name, existingBlockIndex, blockToolCount, message.blocks.length);
  }

  let blockId = tool.blockId;
  let nextBlocks: RenderBlock[];

  if (existingBlockIndex >= 0) {
    nextBlocks = message.blocks.map((block, index) => {
      if (index !== existingBlockIndex || block.kind !== 'tool') {
        return block;
      }
      blockId = block.id;
      return {
        ...block,
        updatedAt: event.ts,
        eventIds: appendUnique(block.eventIds, getEventId(event)),
      };
    });
  } else if (reusableLastToolBlock) {
    blockId = reusableLastToolBlock.id;
    nextBlocks = message.blocks.map((block) => {
      if (block.id !== reusableLastToolBlock.id || block.kind !== 'tool') {
        return block;
      }
      return {
        ...block,
        toolIds: appendUnique(block.toolIds, tool.stableId),
        updatedAt: event.ts,
        eventIds: appendUnique(block.eventIds, getEventId(event)),
      };
    });
  } else {
    blockId = `${message.id}|block:tool:${getEventId(event)}`;
    const block: ToolBlock = {
      id: blockId,
      kind: 'tool',
      toolIds: [tool.stableId],
      createdAt: event.ts,
      updatedAt: event.ts,
      eventIds: [getEventId(event)],
    };
    nextBlocks = [...message.blocks, block];
  }

  const nextTool = blockId && tool.blockId !== blockId ? { ...tool, blockId } : tool;
  const nextMessage = {
    ...message,
    blocks: nextBlocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
  const stateWithMessage = upsertMessage(state, nextMessage);
  const nextState = nextTool === tool
    ? stateWithMessage
    : {
        ...stateWithMessage,
        toolExecutionsById: {
          ...stateWithMessage.toolExecutionsById,
          [nextTool.stableId]: nextTool,
        },
      };

  return { state: nextState, message: nextMessage, tool: nextTool };
}

export function resolveToolStableId(
  state: ChatState,
  message: WsMessage,
  event: SupervisorEvent,
  patch: ToolPatch,
): { stableId: string; externalKey?: string; indexKey?: string } {
  const externalId = patch.id || '';
  const externalKey = externalId ? `${message.id}|external:${externalId}` : undefined;
  const indexKey = patch.index !== undefined ? `${message.id}|index:${patch.index}` : undefined;

  // 1. Exact external ID match — authoritative, always trust
  if (externalKey && state.toolStableIdByExternalId[externalKey]) {
    return { stableId: state.toolStableIdByExternalId[externalKey], externalKey, indexKey };
  }

  // 2. Index-based match — only trust if the external IDs are compatible.
  //    Provider tool_call_delta resets index to 0 each LLM round. If the new
  //    tool has a known externalId that differs from the index-mapped tool's id,
  //    it's a different tool from a new round — do NOT reuse the old stableId.
  if (indexKey && state.toolStableIdByIndex[indexKey]) {
    const candidateStableId = state.toolStableIdByIndex[indexKey];
    const candidateTool = state.toolExecutionsById[candidateStableId];
    const idsCompatible = !externalId || !candidateTool?.id || candidateTool.id === externalId;
    if (idsCompatible) {
      return { stableId: candidateStableId, externalKey, indexKey };
    }
    // IDs differ — fall through to create a new stableId
  }

  const stableId = externalId
    ? `${message.id}|tool:id:${externalId}`
    : patch.index !== undefined
      ? `${message.id}|tool:index:${patch.index}`
      : `${message.id}|tool:event:${getEventId(event)}`;

  return { stableId, externalKey, indexKey };
}
