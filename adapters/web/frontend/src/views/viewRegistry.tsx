// [2026-06-01] Application view registry for chat and settings modes.
// Why: App.tsx should choose a view by key instead of branching over every slot.
// How: each AppViewDefinition supplies left sidebar, header, main, optional composer,
// and right-column slots as render functions. Purpose: adding a future app view or
// settings page does not create a root-level if-else chain.
import type { ReactNode } from 'react';

import { ChatInput, WelcomePage } from '../components/chat';
import { ChildNodePanel, MessageList } from '../components/chat';
import { SystemDashboard } from '../components/dashboard/SystemDashboard';
import { Header, Sidebar } from '../components/layout';
import { EventLogPanel } from '../components/log';
import { SettingsHeader } from '../components/settings/SettingsHeader';
import { SettingsPageHost } from '../components/settings/SettingsPageHost';
import { SettingsRightPanel } from '../components/settings/SettingsRightPanel';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { WorkspaceFileTree } from '../components/workspace/WorkspaceFileTree';
import type { ConversationMeta } from '../store/chatStore';
import { useViewStore, type ViewMode, type PanelOverlayState } from '../store/viewStore';
import type { Attachment } from '../types';
import type { ToolExecution, WsMessage } from '../types/message';

export interface AppViewContext {
  sessionId: string;
  title: string;
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  messages: WsMessage[];
  toolsById: Record<string, ToolExecution>;
  isGenerating: boolean;
  viewingChildSessionId?: string | null;
  viewingChildNodeId?: string;
  onExitChildSession?: () => void;
  onCreateConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => void;
  onSendMessage: (text: string, attachments?: Attachment[]) => Promise<void> | void;
  onTitleChange?: (newTitle: string) => void;
}

export interface AppViewDefinition {
  id: string;
  sidebar: (ctx: AppViewContext) => ReactNode;
  header: (ctx: AppViewContext) => ReactNode;
  main: (ctx: AppViewContext) => ReactNode;
  composer?: (ctx: AppViewContext) => ReactNode;
  rightTop?: (ctx: AppViewContext) => ReactNode;
  rightBottom?: (ctx: AppViewContext) => ReactNode;
  rightOverlay?: (ctx: AppViewContext) => ReactNode;
}

const safeSessionId = (sessionId: string) => sessionId || 'no-session';

// [AutoC 2026-08-22] Plugin panels resolve through the runtime manifest instead of
// a compiled switch arm. Why: panels come from backend PLUGIN_META at runtime and
// must not require a frontend rebuild. How: the plugin:{owner}:{id} namespace reads
// the resolved panel from pluginsStore. Purpose: plugin UI mounts through the same
// overlay channel as built-in overlays, with identical close semantics.
// [AutoC 2026-08-27] The built-in files overlay is itself a host contribution in
// the unified panel registry — no compiled switch arm and no override table left.
// A plugin declaring replaces:'files' registers at the same overlayId with higher
// priority and wins; unloading it restores the host entry on the next manifest
// refresh.
import { PluginPanel } from '../components/plugins/PluginPanel';
import { registerHostPanel, usePluginsStore } from '../store/pluginsStore';

// host seeding: built-in overlays as registry contributions (priority 0)
registerHostPanel({
  key: 'host:files',
  overlayId: 'files',
  title: '工作区文件',
  priority: 0,
  standalone: false,
  Component: WorkspaceFileTree,
});

/** Resolve a panel overlay id through the unified panel registry. */
function resolveOverlay(id: string, ctx: AppViewContext): ReactNode {
  const close = () => useViewStore.getState().clearPanelOverlays();
  const winner = usePluginsStore.getState().panelWinner(id);
  if (!winner) return null;
  if (winner.kind === 'react' && winner.Component) {
    const Panel = winner.Component;
    return <Panel sessionId={safeSessionId(ctx.sessionId)} onClose={close} />;
  }
  if (!winner.entry) return null;
  return (
    <PluginPanel
      entry={winner.entry}
      overlayId={id}
      sessionId={safeSessionId(ctx.sessionId)}
      title={winner.title}
      onClose={close}
    />
  );
}

export const viewRegistry: Record<ViewMode, AppViewDefinition> = {
  chat: {
    id: 'chat',
    sidebar: (ctx) => (
      <Sidebar
        activeConversationId={ctx.activeConversationId}
        conversations={ctx.conversations}
        onCreateConversation={ctx.onCreateConversation}
        onDeleteConversation={ctx.onDeleteConversation}
        onSelectConversation={ctx.onSelectConversation}
      />
    ),
    header: (ctx) => {
      // [2026-06-11] Hide header on the welcome page, but show it when viewing
      // a child session (e.g. from ActiveTasksModal) even without a parent conversation.
      if (!ctx.activeConversationId && !ctx.viewingChildSessionId) return null;
      return (
        <Header
          isGenerating={ctx.isGenerating}
          onExitChildSession={ctx.onExitChildSession}
          onTitleChange={ctx.onTitleChange}
          sessionId={safeSessionId(ctx.sessionId)}
          title={ctx.title}
          viewingChildNodeId={ctx.viewingChildNodeId || ctx.viewingChildSessionId || undefined}
        />
      );
    },
    main: (ctx) => {
      // [2026-06-11] When viewing a child session from the dashboard while on the
      // welcome page, render the message stream instead of the welcome screen.
      if (ctx.viewingChildSessionId) {
        return <MessageList messages={ctx.messages} toolsById={ctx.toolsById} />;
      }
      if (!ctx.activeConversationId) {
        const chatInput = (
          <ChatInput
            disabled={ctx.isGenerating}
            onSend={ctx.onSendMessage}
          />
        );
        return <WelcomePage composer={chatInput} />;
      }
      return (
        <>
          <MessageList messages={ctx.messages} toolsById={ctx.toolsById} />
          <ChildNodePanel conversationId={ctx.activeConversationId} />
        </>
      );
    },
    composer: (ctx) => {
      // [AutoC 2026-06-18] Keep the composer visible for temporary session views.
      // Why: the System session browser can enter an existing session without adding
      // it to the sidebar, and operators still need to speak there. How: hide only the
      // welcome-page footer composer; temporary sessions use the normal bottom input.
      // Purpose: temporary session entry supports both inspection and direct replies.
      if (!ctx.activeConversationId && !ctx.viewingChildSessionId) return null;
      return (
        <ChatInput
          disabled={ctx.isGenerating}
          onSend={ctx.onSendMessage}
        />
      );
    },
    rightTop: () => <SystemDashboard />,
    rightBottom: () => <EventLogPanel />,
    rightOverlay: (ctx) => {
      const overlay = useViewStore.getState().panelOverlay.right;
      return overlay ? resolveOverlay(overlay, ctx) : null;
    },
  },
  settings: {
    id: 'settings',
    sidebar: () => <SettingsSidebar />,
    header: () => <SettingsHeader />,
    main: () => <SettingsPageHost />,
    // [AutoC 2026-08-28] 插件 iframe 设置页不占右栏。Why: 插件面板（MCP、插件
    // 管理器、技能）是自包含 iframe，列表与编辑器都在页内；此前右栏对它们渲染
    // 通用「设置帮助」占位，把 iframe 挤在中间栏，形成无意义的三栏分剖。How:
    // 面向 plugin: 前缀的页签返回 undefined，AppLayout 的 hasRightPanel 为假，
    // 右栏整体卸载、主区占满；内置页签仍保留右栏（上下文编辑器或帮助）。
    // Purpose: 自包含面板拿到全部主区宽度，内置页签的双栏结构不变。
    rightTop: () => {
      const active = useViewStore.getState().activeSettingsTab;
      if (typeof active === 'string' && active.startsWith('plugin:')) {
        // [AutoC 2026-08-28] 插件页编辑进右栏。Why: 列表页（iframe）点击条目
        // 时 postMessage 请求宿主打开编辑器，宿主在右栏渲染同一面板页的
        // mode=editor 形态；未打开编辑器时右栏整体卸载、列表占满主区。
        // How: 读 pluginsStore.pluginEditorTarget，匹配当前页签才渲染。
        // Purpose: 与内置设置页「主区列表 + 右栏编辑」的布局约定一致。
        const target = usePluginsStore.getState().pluginEditorTarget;
        if (target && target.panelKey === active) {
          const panel = usePluginsStore.getState().settingsPanels.find((p) => p.key === active);
          if (panel) {
            const sep = panel.entry.includes('?') ? '&' : '?';
            const qs = new URLSearchParams({ mode: 'editor', ...target.params }).toString();
            return (
              <PluginPanel
                chrome={false}
                entry={`${panel.entry}${sep}${qs}`}
                sessionId=""
                title={panel.title}
                onClose={() => usePluginsStore.getState().setPluginEditorTarget(null)}
              />
            );
          }
        }
        return undefined;
      }
      return <SettingsRightPanel />;
    },
    // [2026-06-02] Settings no longer reserves a lower EventLog slot. Why: contextual
    // settings editors need the full right rail, especially on narrow screens. How:
    // leave rightBottom undefined for settings while chat keeps EventLogPanel. Purpose:
    // AppLayout can promote SettingsRightPanel to full height without view-specific CSS.
  },
};
