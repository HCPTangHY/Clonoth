// [2026-05-16] Node picker — centered modal (IdoFront style).
import { useSettingsStore } from '../../store/settingsStore';
import type { NodeDef } from '../../types';
import { Button, Icon } from '../common';

interface NodePickerModalProps {
  open: boolean;
  onClose: () => void;
  onSwitch: (nodeId: string) => void;
}

function getSwitchableNodes(allNodes: NodeDef[]): NodeDef[] {
  const aiNodes = allNodes.filter(n =>
    n.type === 'ai' && !n.id.startsWith('system.') && !n.id.startsWith('bootstrap.cmd'),
  );
  const delegated = new Set<string>();
  for (const n of allNodes) {
    for (const t of (n.delegate_targets || [])) delegated.add(t);
  }
  const roots = aiNodes.filter(n => !delegated.has(n.id));
  return roots.length > 0 ? roots : aiNodes;
}

export const NodePickerModal = ({ open, onClose, onSwitch }: NodePickerModalProps) => {
  const { availableNodes, activeNodeId } = useSettingsStore();
  const switchable = getSwitchableNodes(availableNodes as any[]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/30" />
      <div
        className="relative w-full max-w-sm border border-[var(--duties-border)] bg-[var(--duties-panel)] shadow-lg"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[var(--duties-border)] px-4 py-3">
          <h3 className="font-mono text-sm font-semibold">切换节点</h3>
          <button className="text-lg text-[var(--duties-tertiary)] hover:text-[var(--duties-text)]" onClick={onClose} type="button">
            {/* [2026-06-01] Why: replace the modal close glyph with Material Symbols.
                How: render close through the shared Icon component. Purpose: modal
                controls no longer depend on literal Unicode symbols. */}
            <Icon name="close" size={18} />
          </button>
        </div>
        {/* Node list */}
        <div className="max-h-[60vh] overflow-y-auto p-2">
          {switchable.map(n => (
            <button
              key={n.id}
              className={`mb-1 flex w-full items-center justify-between px-3 py-2.5 text-left transition-colors hover:bg-[var(--duties-accent)] ${
                n.id === activeNodeId ? 'bg-[var(--duties-muted)]' : ''
              }`}
              onClick={() => onSwitch(n.id)}
              type="button"
            >
              <div className="min-w-0 flex-1">
                <div className="text-xs font-semibold">{n.name || n.id}</div>
                {n.description && (
                  <div className="mt-0.5 truncate text-[0.65rem] text-[var(--duties-tertiary)]">{n.description}</div>
                )}
              </div>
              <div className="ml-3 flex-shrink-0 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
                {(n as any).model || '默认'}
              </div>
            </button>
          ))}
        </div>
        {/* Reset to default */}
        <div className="border-t border-[var(--duties-border)] p-2">
          <Button className="w-full text-[0.65rem]" onClick={() => onSwitch('')} variant="ghost">
            {/* [2026-06-01] Why: replace the reset arrow glyph with Material Symbols.
                How: render keyboard_return before the existing label. Purpose: node
                reset action follows the shared icon system. */}
            <Icon name="keyboard_return" size={14} />
            <span>重置为默认节点</span>
          </Button>
        </div>
      </div>
    </div>
  );
};
