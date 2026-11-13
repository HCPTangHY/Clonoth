import type { Attachment } from '../types';

export interface ComposerSubmission {
  text: string;
  attachments: ReadonlyArray<{ name: string; path: string }>;
  sessionId: string;
  conversationKey: string;
  requestId: string;
  signal: AbortSignal;
  state: Record<string, unknown>;
}

export interface ComposerMode {
  id: string;
  label: string;
  placeholder: string;
  command?: string;
  onSubmit: (submission: ComposerSubmission) => Promise<unknown>;
}

/** Instance-scoped: plugins never mutate the React-owned textarea or capture Enter. */
export interface ComposerApi {
  registerMode: (mode: ComposerMode) => () => void;
  setMode: (id: string, text?: string, metadata?: unknown) => boolean;
  getDraft: () => { text: string; mode: string; busy: boolean; attachments: Array<{ name: string }>; sessionId: string };
  setDraft: (text: string) => boolean;
  getState: (id: string) => Record<string, unknown>;
  mode: string;
  busy: boolean;
}

export function resolveComposerSubmission(text: string, selected: string, modes: Iterable<ComposerMode>, knownCommands: ReadonlySet<string>) {
  const trimmed = text.trim();
  const token = trimmed.split(/\s/, 1)[0];
  const entries = Array.from(modes);
  const commandMode = entries.find(mode => mode.command === token);
  const mode = commandMode || entries.find(mode => mode.id === selected);
  if (knownCommands.has(token) && !commandMode) throw new Error('此输入方式当前不可用，请启用对应插件。正文已保留。');
  if (selected && !mode) throw new Error('输入插件已卸载，请重新选择输入方式。正文已保留。');
  const body = commandMode ? trimmed.slice(token.length).trim() : trimmed;
  if (mode && !body) throw new Error('请输入目标正文。');
  return { mode, text: body };
}

export const composerDrafts = new Map<string, { text: string; attachments: Attachment[]; mode: string }>();
const modeStates = new Map<string, Record<string, unknown>>();
export function getComposerModeState(scope: string, id: string): Record<string, unknown> {
  const key = JSON.stringify([scope, id]);
  let state = modeStates.get(key);
  if (!state) { state = {}; modeStates.set(key, state); }
  return state;
}
let uploadedPaths = new WeakMap<File, Map<string, { name: string; path: string }>>();
export function clearComposerDrafts(): void {
  for (const draft of composerDrafts.values()) for (const attachment of draft.attachments) {
    if (attachment.url?.startsWith('blob:')) URL.revokeObjectURL(attachment.url);
  }
  composerDrafts.clear();
  for (const state of modeStates.values()) for (const key of Object.keys(state)) delete state[key];
  modeStates.clear();
  uploadedPaths = new WeakMap();
}

export async function uploadComposerAttachments(
  attachments: readonly Attachment[], conversationKey: string,
  upload: (file: File, key: string) => Promise<{ name: string; path: string }>,
  isCurrent: () => boolean,
): Promise<Array<{ name: string; path: string }>> {
  const results = [];
  for (const attachment of attachments) {
    if (!isCurrent()) throw new Error('会话已切换，目标未提交。');
    let result;
    if (attachment.file) {
      const paths = uploadedPaths.get(attachment.file) || new Map();
      uploadedPaths.set(attachment.file, paths);
      result = paths.get(conversationKey);
      if (!result) {
        result = await upload(attachment.file, conversationKey);
        if (typeof result.path !== 'string' || !result.path || /^(blob:|https?:|file:)/i.test(result.path)) throw new Error('附件没有可访问的服务端路径。');
        paths.set(conversationKey, result);
      }
    } else {
      result = { name: attachment.name, path: attachment.path || '' };
    }
    if (typeof result.path !== 'string' || !result.path || /^(blob:|https?:|file:)/i.test(result.path)) throw new Error('附件没有可访问的服务端路径。');
    results.push({ name: result.name, path: result.path });
  }
  if (!isCurrent()) throw new Error('会话已切换，目标未提交。');
  return results;
}
