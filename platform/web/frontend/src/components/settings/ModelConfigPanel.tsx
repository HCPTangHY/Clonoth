// [2026-05-16] Model config — right slide panel (IdoFront style).
// [2026-06-01] The full-height model panel now edits session provider overrides.
// Why: Header model text should not open a global modal, and the requested panel is
// session-level. How: delegate the actual compact editor to SessionConfigPanel with
// model focus. Purpose: keep one implementation for model override loading, saving,
// and clearing.
import { Icon } from '../common';
import { SessionConfigPanel } from './SessionConfigPanel';

interface ModelConfigPanelProps {
  sessionId: string;
  onClose: () => void;
}

export const ModelConfigPanel = ({ sessionId, onClose }: ModelConfigPanelProps) => (
  <div className="flex h-full flex-col overflow-hidden">
    <div className="flex items-center justify-between border-b border-[var(--duties-border)] px-3 py-2">
      <h3 className="font-mono text-[0.65rem] font-semibold uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">会话模型</h3>
      <button className="text-xs text-[var(--duties-tertiary)] hover:text-[var(--duties-text)]" onClick={onClose} type="button">
        {/* [2026-06-01] Why: the session model panel close button used a multiplication glyph.
            How: render close through the shared Icon. Purpose: settings panel controls
            use Material Symbols consistently. */}
        <Icon name="close" size={14} />
      </button>
    </div>
    <SessionConfigPanel focus="model" sessionId={sessionId} />
  </div>
);
