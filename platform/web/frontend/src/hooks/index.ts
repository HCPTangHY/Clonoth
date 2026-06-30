// This hook barrel is added to provide one import path for the current and future web hooks.
// It has no runtime side effects; it only re-exports the chat and session adapters.
// The purpose is to keep component imports simple as more Supervisor hooks are added.
// [2026-06-03] The chat hook now has no V2 suffix. Why: the reducer-backed
// adapter became the canonical useChat export. How: keep a single named export.
// Purpose: callers can import useChat without duplicate barrel symbols.
export { useChat } from './useChat';
export { useSession } from './useSession';
