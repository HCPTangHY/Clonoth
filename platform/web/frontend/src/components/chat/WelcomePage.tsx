// [2026-06-10] Welcome page — shown when no conversation is selected.
// Reuses ChatInput via the `composer` prop. Logo + title from Sidebar.
// Node/model selector reuses the same settingsStore + SessionConfigModal as Header.
import { type ReactNode, useState } from 'react';

import { useSettingsStore } from '../../store/settingsStore';
import { Icon } from '../common';
import { SessionConfigModal } from '../settings/SessionConfigModal';

interface WelcomePageProps {
  composer: ReactNode;
}

export const WelcomePage = ({ composer }: WelcomePageProps) => {
  const { activeNodeId, entryNodeId, activeEffectiveModel, availableNodes, pendingProviderOverride } = useSettingsStore();
  const [configFocus, setConfigFocus] = useState<'node' | 'model' | null>(null);

  const displayNodeId = activeNodeId || entryNodeId || '';
  const activeNode = availableNodes.find((n) => n.id === displayNodeId);
  const pendingModel = pendingProviderOverride && typeof pendingProviderOverride === 'object' && 'model' in pendingProviderOverride
    ? String(pendingProviderOverride.model || '')
    : '';
  const displayModel = pendingModel || activeEffectiveModel || '(默认)';

  return (
    <>
      <div className="flex h-full flex-col items-center justify-center px-4">
        {/* Logo — same image as Sidebar header */}
        <img
          src={`${import.meta.env.BASE_URL}logo-sm.jpg`}
          alt="Clonoth"
          className="mb-4 h-14 w-14 rounded-xl"
        />
        <h1 className="mb-1 font-mono text-lg font-semibold tracking-[-0.04em] text-[var(--duties-text)]">
          Clonoth
        </h1>
        <p className="mb-6 font-mono text-xs text-[var(--duties-tertiary)]">
          开始新的对话
        </p>

        {/* Node / Model selector — same style as Header badges */}
        <div className="mb-6 flex items-center gap-1.5 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">
          <span
            className="cursor-pointer transition-colors hover:text-[var(--duties-text)]"
            onClick={() => setConfigFocus('node')}
            title="切换节点"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="hub" size={13} />
              <span>{activeNode?.name || displayNodeId || '选择节点'}</span>
            </span>
          </span>
          <span className="text-[var(--duties-border)]">/</span>
          <span
            className="cursor-pointer transition-colors hover:text-[var(--duties-text)]"
            onClick={() => setConfigFocus('model')}
            title="模型配置"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="model_training" size={13} />
              <span>{displayModel}</span>
            </span>
          </span>
        </div>

        {/* The real ChatInput */}
        <div className="w-full max-w-3xl">
          {composer}
        </div>
      </div>

      {/* Reuse the same SessionConfigModal as Header */}
      {configFocus && (
        <SessionConfigModal
          focus={configFocus}
          onClose={() => setConfigFocus(null)}
          sessionId="no-session"
        />
      )}
    </>
  );
};
