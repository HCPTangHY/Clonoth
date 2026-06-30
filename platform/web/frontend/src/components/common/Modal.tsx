// [AutoC 2026-06-04] Shared modal shell.
// Why: SessionConfigModal, ActiveTasksModal, and NodePickerModal all duplicated
// the same backdrop/container/close-button pattern. How: extract a reusable
// Modal shell with consistent styling. Purpose: DRY and uniform modal UX.
import { type ReactNode, useEffect } from 'react';

import { Icon } from './Icon';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  ariaLabel?: string;
  maxWidth?: string;
  children: ReactNode;
}

export const Modal = ({
  open,
  onClose,
  title,
  subtitle,
  ariaLabel,
  maxWidth = 'max-w-lg',
  children,
}: ModalProps) => {
  useEffect(() => {
    if (!open) return undefined;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={onClose}>
      <div
        aria-label={ariaLabel || title}
        aria-modal="true"
        className={`flex max-h-[86dvh] w-full ${maxWidth} flex-col border border-[var(--duties-border)] bg-[var(--duties-panel)] shadow-xl`}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="flex items-center justify-between border-b border-[var(--duties-border)] px-3 py-2">
          <div>
            {subtitle && (
              <p className="font-mono text-[0.55rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">{subtitle}</p>
            )}
            <h2 className="font-mono text-sm font-semibold tracking-[-0.03em]">{title}</h2>
          </div>
          <button
            aria-label={`关闭${title}`}
            className="rounded-sm p-1 text-[var(--duties-tertiary)] transition-colors hover:bg-[var(--duties-muted)] hover:text-[var(--duties-text)]"
            onClick={onClose}
            type="button"
          >
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {children}
        </div>
      </div>
    </div>
  );
};
