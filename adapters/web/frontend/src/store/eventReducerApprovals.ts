// ── Event Reducer Notice and Approval Helpers ──────────────────────────────
// [AutoC 2026-06-16] Notices, legacy approval blocks, and inline tool approval state.

import type { SupervisorEvent } from '../types/chat';
import type { ApprovalBlock, ChatState, NoticeBlock, RenderBlock, ToolExecution, ToolStatus } from '../types/message';
import { TERMINAL_TOOL_STATUSES, type EventPayload } from './eventReducerShared';
import { appendUnique, getEventId, getRecord, getString } from './eventReducerPayload';
import { setMessageStatus } from './eventReducerBlocks';
import { getTurnKey, upsertMessage } from './eventReducerState';
import { getOrCreateAssistantMessage } from './eventReducerAssistant';

export function appendNoticeToAssistant(
  state: ChatState,
  event: SupervisorEvent,
  notice: Pick<NoticeBlock, 'level' | 'text' | 'title' | 'eventType'>,
): ChatState {
  const turnKey = getTurnKey(state, event);
  let { state: nextState, message } = getOrCreateAssistantMessage(state, event, turnKey);
  const block: NoticeBlock = {
    id: `${message.id}|notice:${getEventId(event)}`,
    kind: 'notice',
    level: notice.level,
    text: notice.text,
    title: notice.title,
    eventType: notice.eventType,
    createdAt: event.ts,
    updatedAt: event.ts,
    eventIds: [getEventId(event)],
  };

  message = setMessageStatus({ ...message, blocks: [...message.blocks, block] }, 'running_tools', event);
  nextState = upsertMessage(nextState, message);
  return nextState;
}

export function appendLegacyApprovalBlock(
  state: ChatState,
  event: SupervisorEvent,
  payload: EventPayload,
  approvalId: string,
): ChatState {
  // [AutoC 2026-05-31] Why: older backend events and cached EventLog rows may not
  // include tool_call_id. How: keep the prior ApprovalBlock path as an explicit
  // fallback. Purpose: mixed-version and historical approvals remain actionable.
  const turnKey = getTurnKey(state, event);
  let { state: nextState, message } = getOrCreateAssistantMessage(state, event, turnKey);
  const blockId = `${message.id}|approval:${approvalId}`;
  const approvalBlock: ApprovalBlock = {
    id: blockId,
    kind: 'approval',
    approvalId,
    operation: getString(payload.operation),
    details: getRecord(payload.details) || {},
    status: normalizeApprovalStatus(payload.status, payload.decision),
    decision: getString(payload.decision) || undefined,
    comment: getString(payload.comment) || undefined,
    createdAt: event.ts,
    updatedAt: event.ts,
    eventIds: [getEventId(event)],
  };

  message = setMessageStatus({
    ...message,
    blocks: appendOrReplaceApprovalBlock(message.blocks, approvalBlock),
  }, 'awaiting_approval', event);

  nextState = upsertMessage(nextState, message);
  return {
    ...nextState,
    approvalBlockById: {
      ...nextState.approvalBlockById,
      [approvalId]: { messageId: message.id, blockId },
    },
  };
}

export function updateLegacyApprovalBlock(
  state: ChatState,
  event: SupervisorEvent,
  payload: EventPayload,
  approvalId: string,
  location: { messageId: string; blockId: string },
): ChatState {
  // [AutoC 2026-05-31] Why: standalone approval blocks still exist for legacy
  // payloads. How: preserve the old block update behavior behind a named fallback.
  // Purpose: the new tool-card path does not break old approvals.
  const message = state.messagesById[location.messageId];
  if (!message) {
    return state;
  }

  const decision = getString(payload.decision);
  const nextBlocks = message.blocks.map((block) => {
    if (block.kind !== 'approval' || block.id !== location.blockId) {
      return block;
    }

    return {
      ...block,
      status: normalizeApprovalStatus(payload.status, decision),
      decision: decision || block.decision,
      comment: getString(payload.comment) || block.comment,
      updatedAt: event.ts,
      eventIds: appendUnique(block.eventIds, getEventId(event)),
    } satisfies ApprovalBlock;
  });

  const nextMessage = setMessageStatus({ ...message, blocks: nextBlocks }, 'running_tools', event);
  return upsertMessage(state, nextMessage);
}

export function appendOrReplaceApprovalBlock(blocks: readonly RenderBlock[], incoming: ApprovalBlock): RenderBlock[] {
  const existingIndex = blocks.findIndex((block) => block.kind === 'approval' && block.approvalId === incoming.approvalId);

  if (existingIndex < 0) {
    return [...blocks, incoming];
  }

  return blocks.map((block, index) => {
    if (index !== existingIndex || block.kind !== 'approval') {
      return block;
    }
    return {
      ...incoming,
      createdAt: block.createdAt,
      eventIds: appendUnique(block.eventIds, ...incoming.eventIds),
    };
  });
}

export function normalizeToolStatus(value: unknown): ToolStatus {
  const raw = getString(value);
  if (raw === 'async_started' || raw === 'success' || raw === 'error' || raw === 'cancelled') {
    return raw;
  }
  if (raw === 'running' || raw === 'queued' || raw === 'args_streaming' || raw === 'awaiting_approval') {
    return raw;
  }
  return 'success';
}

export function normalizeApprovalStatus(status: unknown, decision: unknown): ApprovalBlock['status'] {
  const rawStatus = getString(status);
  const rawDecision = getString(decision);

  if (rawStatus === 'allowed' || rawDecision === 'allow') {
    return 'allowed';
  }
  if (rawStatus === 'denied' || rawDecision === 'deny') {
    return 'denied';
  }
  return 'pending';
}

export function findToolByCallId(state: ChatState, toolCallId: string): ToolExecution | undefined {
  // [AutoC 2026-05-31] Why: approval events carry the provider tool_call_id, not
  // the reducer's stable id. How: scan the normalized tool table for the external
  // id field. Purpose: approvals can attach to tools regardless of block location.
  return Object.values(state.toolExecutionsById).find((tool) => tool.id === toolCallId);
}

export function buildApprovalDetails(payload: EventPayload): Record<string, unknown> {
  // [AutoC 2026-05-31] Why: ToolExecution needs enough approval data to render
  // operation, path, and reason without reconstructing the old ApprovalBlock. How:
  // retain both operation and raw details under one object. Purpose: ToolCallCard
  // can show the same meaningful fields as the previous standalone card.
  const details = getRecord(payload.details) || {};
  const toolName = getString(details.tool_name) || getString(details.tool) || getString(details.name);
  const policyOperation = getString(payload.operation);
  return {
    operation: toolName || policyOperation,
    policyOperation,
    details,
  };
}

export function getToolStatusAfterApproval(
  currentStatus: ToolStatus,
  approvalStatus: ApprovalBlock['status'],
): ToolStatus {
  // [AutoC 2026-05-31] Why: approval_decided should not overwrite a result that
  // has already arrived during replay or reconnect. How: preserve terminal statuses
  // and only move pending approval back into running/error. Purpose: event order
  // remains robust while the card reflects the decision immediately.
  if (TERMINAL_TOOL_STATUSES.has(currentStatus)) return currentStatus;
  if (approvalStatus === 'allowed') return 'running';
  if (approvalStatus === 'denied') return 'error';
  return 'awaiting_approval';
}
