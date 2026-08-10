// [2026-05-17] Header: node display from backend (source of truth).
// [2026-05-31] Step 3 renames the activity prop from isTyping to isGenerating.
// Why: V2 no longer has typingConversationId or streamPreview state. How: the header
// consumes the store-level generation flag directly. Purpose: keep cancel/reset UI
// aligned with the reducer-backed chat flow.
import { useEffect, useState } from 'react';

import { getActiveNode, getAppConfig, getNodes, getSessionWorkspace } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { Button, Icon } from '../common';
import { SessionConfigModal } from '../settings/SessionConfigModal';

interface HeaderProps {
  title: string;
  sessionId: string;
  isGenerating: boolean;
  onTitleChange?: (newTitle: string) => void;
  viewingChildNodeId?: string;
  onExitChildSession?: () => void;
}

export const Header = ({ title, sessionId, isGenerating, onTitleChange, viewingChildNodeId, onExitChildSession }: HeaderProps) => {
  const {
    adminToken, availableNodes, activeNodeId, entryNodeId, activeEffectiveModel,
    setActiveNode, setGlobalConfig, setAvailableNodes,
  } = useSettingsStore();
  const displayNodeId = activeNodeId || entryNodeId || '';
  const activeNode = availableNodes.find(n => n.id === displayNodeId);
  const [configModalFocus, setConfigModalFocus] = useState<'node' | 'model' | 'workspace' | 'title' | null>(null);
  const [draftTitle, setDraftTitle] = useState(title);
  const [workspaceName, setWorkspaceName] = useState('');

  // Sync draft when title prop changes from outside
  useEffect(() => { setDraftTitle(title); }, [title]);

  // 后端 /active_node 返回已解析的有效模型；Header 不再自行拼 fallback。
  const displayModel = activeEffectiveModel || '(默认)';

  const openSessionConfigModal = (focus: 'node' | 'model' | 'workspace' | 'title') => {
    if (focus === 'title') {
      setDraftTitle(title);
    }
    setConfigModalFocus(focus);
  };

  const handleTitleSave = () => {
    const trimmed = draftTitle.trim();
    setConfigModalFocus(null);
    if (trimmed && trimmed !== title && onTitleChange) {
      onTitleChange(trimmed);
    } else {
      setDraftTitle(title);
    }
  };

  // Fetch active node from backend when sessionId changes — backend is source of truth
  useEffect(() => {
    if (!sessionId || sessionId === 'no-session') return;
    getActiveNode(sessionId)
      .then(r => {
        setActiveNode(r.node_id, r.is_override, r.default_node_id, {
          provider: r.effective_provider || '',
          model: r.effective_model || '',
          baseUrl: r.effective_base_url || '',
        });
      })
      .catch(() => {});
  }, [sessionId, setActiveNode]);

  // Fetch global config once
  useEffect(() => {
    getAppConfig()
      .then(r => setGlobalConfig(r.openai?.model || '', r.openai?.base_url || '', r.provider_models || {}))
      .catch(() => {});
  }, [setGlobalConfig]);

  // Load nodes if needed
  useEffect(() => {
    if (availableNodes.length > 0 || !adminToken) return;
    getNodes(adminToken)
      .then(n => setAvailableNodes(n.filter((nd: any) => nd.type === 'ai' && !nd.id.startsWith('system.'))))
      .catch(() => {});
  }, [adminToken, availableNodes.length, setAvailableNodes]);

  // Fetch session workspace name for the header badge
  useEffect(() => {
    if (!sessionId || sessionId === 'no-session' || !adminToken) {
      setWorkspaceName('');
      return;
    }
    getSessionWorkspace(sessionId, adminToken)
      .then(ws => setWorkspaceName(ws?.name || ''))
      .catch(() => setWorkspaceName(''));
  }, [sessionId, adminToken]);

  return (
    <>
      <header className="px-3 py-2 sm:px-4 sm:py-3">
      <div className="mx-auto flex max-w-3xl items-center justify-between gap-2">
        {/* Left: title + badges */}
        <div className="min-w-0 flex-1">
          {/* [2026-06-03] Why: the child-view title is derived from childNodes and
              should not edit the parent conversation title. How: disable the title
              click affordance while viewingChildNodeId is present. Purpose: the
              title editor remains scoped to parent conversations only. */}
          <div className="flex items-center gap-2">
            <h2
              className={`min-w-0 truncate font-mono text-sm font-semibold tracking-[-0.03em]${onTitleChange && !viewingChildNodeId ? ' cursor-pointer transition-colors hover:text-[var(--duties-text)]' : ''}`}
              onClick={onTitleChange && !viewingChildNodeId ? () => openSessionConfigModal('title') : undefined}
              title={onTitleChange && !viewingChildNodeId ? '点击编辑标题' : undefined}
            >
              {title}
            </h2>
            <span
              className="inline-flex shrink-0 cursor-pointer items-center gap-0.5 rounded border border-[var(--duties-border)] px-1.5 py-0.5 font-mono text-[0.55rem] text-[var(--duties-tertiary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]"
              onClick={() => {
                if (workspaceName) {
                  const { panelOverlay, setPanelOverlay } = useViewStore.getState();
                  const { setRightPanelOpen } = useSettingsStore.getState();
                  if (panelOverlay.right === 'files') {
                    setPanelOverlay('right', null);
                  } else {
                    setPanelOverlay('right', 'files');
                    setRightPanelOpen(true);
                  }
                } else {
                  openSessionConfigModal('workspace');
                }
              }}
              title={workspaceName ? '浏览工作区文件' : '设置工作区'}
            >
              <Icon name="folder" size={11} />
              {workspaceName || '设置工作区'}
            </span>
          </div>
          <div className="mt-1 flex items-center gap-1.5 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
            <span
              className="inline-flex cursor-pointer items-center gap-1 transition-colors hover:text-[var(--duties-text)]"
              onClick={() => openSessionConfigModal('node')}
              title="切换节点"
            >
              <Icon name="hub" size={13} />
              <span>{activeNode?.name || displayNodeId || '选择节点'}</span>
            </span>
            <span className="text-[var(--duties-border)]">/</span>
            <span
              className="inline-flex cursor-pointer items-center gap-1 transition-colors hover:text-[var(--duties-text)]"
              onClick={() => openSessionConfigModal('model')}
              title="模型配置"
            >
              <Icon name="model_training" size={13} />
              <span>{displayModel}</span>
            </span>
          </div>
        </div>

        {/* Right: action buttons */}
        <div className="flex items-center gap-2">
          {viewingChildNodeId && onExitChildSession && (
            <Button className="h-7 px-2 text-[0.6rem]" onClick={onExitChildSession} variant="ghost">
              {/* [2026-06-03] Why: child-session view temporarily replaces the parent
                  message list. How: show an explicit return action in the header.
                  Purpose: users can leave the child stream without selecting the parent
                  conversation again from the sidebar. */}
              <Icon name="arrow_back" size={14} /> 返回父会话
            </Button>
          )}

        </div>
      </div>
    </header>
      {/* Title edit modal */}
      {configModalFocus === 'title' && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setConfigModalFocus(null)}>
          <div className="w-full max-w-sm border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4 shadow-lg" onClick={(e) => e.stopPropagation()}>
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">编辑对话标题</p>
            <input
              autoFocus
              className="mb-3 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1.5 font-mono text-sm outline-none focus:border-[var(--duties-accent)]"
              onChange={(e) => setDraftTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleTitleSave();
                if (e.key === 'Escape') setConfigModalFocus(null);
              }}
              value={draftTitle}
            />
            <div className="flex justify-end gap-2">
              <Button className="h-7 px-3 text-[0.6rem]" onClick={() => setConfigModalFocus(null)} variant="ghost">取消</Button>
              <Button className="h-7 px-3 text-[0.6rem]" onClick={handleTitleSave}>保存</Button>
            </div>
          </div>
        </div>
      )}
      {/* Node/Model/Workspace config modal */}
      {(configModalFocus === 'node' || configModalFocus === 'model' || configModalFocus === 'workspace') && (
        <SessionConfigModal
          focus={configModalFocus}
          onClose={() => setConfigModalFocus(null)}
          sessionId={sessionId}
        />
      )}
    </>
  );
};
