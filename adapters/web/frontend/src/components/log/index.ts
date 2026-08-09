// [2026-05-31] Log component barrel for the Step 2A bottom panel.
// Why: layout consumers should import log components from the folder boundary.
// How: re-export EventLogPanel only. Purpose: keep future log widgets isolated from
// chat and layout component modules.
export { EventLogPanel } from './EventLogPanel';
