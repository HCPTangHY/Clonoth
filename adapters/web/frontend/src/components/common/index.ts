// This common component barrel is added to keep shared UI controls easy to import.
// [2026-06-01] It now also exports the Material Symbols icon primitive. Why: old
// emoji glyphs are being replaced across the UI. How: expose Icon beside Button.
// Purpose: callers use a stable common import path for shared visual primitives.
export { Button } from './Button';
export { Icon } from './Icon';

// [2026-06-03] Export the shared child-node status indicator.
// Why: Sidebar and ChildNodePanel both render child lifecycle state. How: expose the
// dot and label helper through the existing common barrel. Purpose: child-node UI
// keeps one color and label mapping across the application.
export { StatusDot, getChildNodeStatusLabel } from './StatusDot';

// [2026-06-02] Export the raw settings editor from the common barrel.
// Why: System, Agents, Tools, Skills, MCP, Automation, and Advanced pages all need
// the same textarea editor. How: re-export the component and its lightweight YAML
// helper. Purpose: page imports stay short and consistent.
export { YamlEditor, hasSuspiciousYamlTabs } from './YamlEditor';
export { Modal } from './Modal';
