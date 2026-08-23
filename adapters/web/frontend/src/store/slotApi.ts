// [AutoC 2026-08-22] Host action surface for plugin slot scripts.
// Why: slot contributions need to trigger host behaviors that only the web app
// can perform — retry depends on the message-id credential and JSONL fallback
// logic living in chatStore, and blob-imported plugin modules cannot reach
// bundled stores. How: a stable singleton exposing two channels — api.call
// dispatches to the host action registry (hostActions.ts, actions registered
// once by name), api.request reaches any supervisor endpoint with auth.
// Purpose: plugins act on the host through one generic surface; adding a core
// capability registers an action once, no per-plugin whitelist edits.
import { pluginApiRequest } from '../api/supervisorClient';
import { callHostAction } from './hostActions';

export interface PluginSlotApi {
  /**
   * Invoke a registered host action by name, e.g. api.call('retryMessage', id).
   * Unknown names log a warning and resolve null, so optional chaining across
   * host versions stays safe.
   */
  call: (name: string, ...args: unknown[]) => Promise<unknown>;
  /**
   * Authenticated request to any supervisor /v1 endpoint.
   * Example: api.request('/sessions'). Non-ok responses reject.
   */
  request: (path: string, init?: RequestInit) => Promise<unknown>;
}

let cached: PluginSlotApi | null = null;

export function getPluginSlotApi(): PluginSlotApi {
  if (cached) return cached;
  cached = {
    call: (name, ...args) => callHostAction(name, ...args),
    request: (path, init) => pluginApiRequest(path, init),
  };
  return cached;
}
