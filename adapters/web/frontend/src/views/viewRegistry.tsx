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
import { usePluginsStore } from '../store/pluginsStore';
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
import { PluginPanel } from '../components/plugins/PluginPanel';

/** Resolve a panel overlay id to a React element. Add new overlays here. */
function resolveOverlay(id: string, ctx: AppViewContext): ReactNode {
  const close = () => useViewStore.getState().clearPanelOverlays();
  if (id.startsWith('plugin:')) {
    const panel = usePluginsStore.getState().panelByKey(id);
    if (!panel) return null;
    return (
      <PluginPanel
        entry={panel.entry}
        overlayId={id}
        sessionId={safeSessionId(ctx.sessionId)}
        title={panel.title}
        onClose={close}
      />
    );
  }
  // [AutoC 2026-08-24] Plugin replacement of built-in overlays: a plugin panel
  // declaring replaces:'files' takes over the whole overlay. The built-in
  // implementation below stays as the fallback when no plugin replaces it.
  const replacement = usePluginsStore.getState().overlayOverrides[id];
  if (replacement) {
    return (
      <PluginPanel
        entry={replacement.entry}
        overlayId={id}
        sessionId={safeSessionId(ctx.sessionId)}
        title={replacement.title}
        onClose={close}
      />
    );
  }
  switch (id) {
    case 'files':
      return <WorkspaceFileTree sessionId={safeSessionId(ctx.sessionId)} onClose={close} />;
    default:
      return null;
  }
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
    rightTop: () => <SettingsRightPanel />,
    // [2026-06-02] Settings no longer reserves a lower EventLog slot. Why: contextual
    // settings editors need the full right rail, especially on narrow screens. How:
    // leave rightBottom undefined for settings while chat keeps EventLogPanel. Purpose:
    // AppLayout can promote SettingsRightPanel to full height without view-specific CSS.
  },
};
