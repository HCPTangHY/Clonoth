// [2026-06-03] The chat store now has no V2 suffix. Why: the reducer-backed
// implementation became the canonical store. How: expose only the renamed store
// hook and its public types. Purpose: barrel imports cannot resolve duplicate
// chat store symbols after the mechanical rename.
export { useChatStore } from './chatStore';
export type { ChatStoreState, ChildNodeState, ChildNodeStatus, ConversationMeta, ConnectionStatus } from './chatStore';
export { useClientPrefsStore, shouldAutoApproveTool } from './clientPrefsStore';
// [2026-06-05] Why: approvalLevel is now part of the public client preferences
// contract. How: re-export the ApprovalLevel type beside the existing preference
// types. Purpose: barrel imports can type settings code without reaching into the
// store implementation file.
export type { ApprovalLevel, ClientPrefs, TitleGenerationMode } from './clientPrefsStore';
export { useSettingsStore } from './settingsStore';
export type { SettingsState } from './settingsStore';
// [2026-06-01] Export viewStore beside configuration stores.
// Why: shell routing is intentionally separate from settings data. How: expose the
// view hook and types from the store barrel. Purpose: tests and future views can
// consume the registry-driven view state without importing configuration state.
export { useViewStore } from './viewStore';
export type { ViewMode, ViewState } from './viewStore';
