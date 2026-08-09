// ── Event Reducer Payload and ID Helpers ───────────────────────────────────
// [AutoC 2026-06-16] Payload parsing, ID generation, JSON helpers, and small utilities.

import type { SupervisorEvent } from '../types/chat';
import type { Attachment } from '../types/message';
import type { EventPayload } from './eventReducerShared';

export function getPayload(event: SupervisorEvent): EventPayload {
  return getRecord(event.payload) || {};
}

export function getRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  return value as Record<string, unknown>;
}

export function getString(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : String(value);
}

export function getNumber(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

export function getBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

export function getSourceInboundSeq(payload: EventPayload): number | undefined {
  const seq = getNumber(payload.source_inbound_seq);
  return seq !== undefined && seq > 0 ? seq : undefined;
}

export function hasSourceInboundSeq(payload: EventPayload): boolean {
  return getSourceInboundSeq(payload) !== undefined;
}

export function getAttachments(value: unknown): Attachment[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value
    .filter((item): item is Record<string, unknown> => Boolean(getRecord(item)))
    .map((item) => {
      const record = item as Record<string, unknown>;
      return {
        name: getString(record.name),
        size: getNumber(record.size),
        url: getString(record.url) || undefined,
        type: getString(record.type) || undefined,
        path: getString(record.path) || undefined,
        mime_type: getString(record.mime_type) || undefined,
      };
    });
}

export function normalizeConversationKey(value: string): string {
  if (!value) {
    return '';
  }
  return value.startsWith('web:') ? value.slice(4) : value;
}

export function getEventId(event: SupervisorEvent): string {
  return event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
}

export function getUserMessageId(conversationId: string, inboundSeq: number): string {
  return `message:${conversationId}:user:inbound:${inboundSeq}`;
}

export function getAssistantMessageId(conversationId: string, turnKey: string): string {
  return `message:${conversationId}:assistant:${turnKey}`;
}

export function appendUnique<T>(items: readonly T[], ...incoming: T[]): T[] {
  const next = [...items];
  for (const item of incoming) {
    if (!next.includes(item)) {
      next.push(item);
    }
  }
  return next;
}

export function stringifyJson(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

export function parseJsonRecord(value: string | undefined): Record<string, unknown> | undefined {
  if (!value) {
    return undefined;
  }

  try {
    return getRecord(JSON.parse(value));
  } catch {
    return undefined;
  }
}
