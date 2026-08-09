// [2026-06-01] Legacy wrapper for the old SettingsPanel import path.
// Why: the implementation was split into registered settings pages, but external or
// older imports may still reference SettingsPanel during migration. How: render the
// new SettingsPageHost without owning visibility or modal state. Purpose: preserve a
// safe compatibility path while App.tsx uses viewRegistry.
import { SettingsPageHost } from './SettingsPageHost';

export const SettingsPanel = () => <SettingsPageHost />;
