// [2026-06-05] Shared rendering constants for the Chat v2 message body.
// Why: ThinkingBlock, TextBlockView, and ToolCallCard used the same arbitrary
// spacing and small font values independently. How: keep the exact Tailwind class
// strings in one module and import them where the visual contract is shared.
// Purpose: future message-rendering changes can adjust the shared inline rhythm
// without reintroducing one-off magic values in separate components.
import type { WsMessage } from '../../types/message';

export interface MessageRenderContext {
  role: WsMessage['role'];
  status: WsMessage['status'];
  completionType?: WsMessage['completionType'];
}

export const BLOCK_STACK_CLASS = 'space-y-2';
export const INLINE_BLOCK_INDENT_CLASS = 'ml-[5.5px]';
export const INLINE_BLOCK_BODY_TEXT_CLASS = 'text-[0.72rem]';
export const INLINE_BLOCK_HEADER_TEXT_CLASS = 'text-[0.65rem]';
export const INLINE_TEXT_BORDER_BASE_CLASS = `${INLINE_BLOCK_INDENT_CLASS} border-l-2 pl-3`;
