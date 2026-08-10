// [2026-05-16] Full app: login gate and chat layout.
// [2026-05-31] Step 3 switches MainApp to the reducer-backed V2 chat path.
// Why: the new message model owns streaming, tools, and event history in one store.
// How: use ChatInput, MessageList, and EventLogPanel through viewRegistry. Purpose:
// leave old files available as fallback while the active application uses V2 data.
// [2026-06-01] MainApp now selects shell content through viewRegistry.
// Why: settings mode replaces the left and center columns and should not be encoded
// as App-level modal/right-panel conditionals. How: build one AppViewContext, choose
// viewRegistry[viewMode], and pass the resolved slots to AppLayout. Purpose: App.tsx
// remains a small composition root instead of a business-routing file.
import { useEffect } from 'react';

import { checkHealth, resetConversation } from './api/supervisorClient';
import { LoginPage } from './components/auth/LoginPage';
import { AppLayout } from './components/layout';
import { SetupWizard } from './components/setup/SetupWizard';
import { useChat } from './hooks/useChat';
import { useChatStore } from './store/chatStore';
import { useSettingsStore } from './store/settingsStore';
import { useViewStore } from './store/viewStore';
import type { Attachment } from './types';
import { viewRegistry, type AppViewContext } from './views/viewRegistry';

const MainApp = () => {
  const {
    conversations, activeConversationId, activeConversation, messages, isGenerating,
    selectConversation, createConversation, deleteConversation, renameConversation, sendMessage, cancelCurrentTask,
  } = useChat();
  // [2026-05-31] MessageList needs the normalized tool table beside ordered
  // messages. Why: tool blocks store stable tool ids, not full tool objects. How:
  // subscribe to toolExecutionsById directly from chatStore. Purpose: preserve the
  // reducer-owned data model without reintroducing legacy streamPreview state.
  const toolsById = useChatStore((state) => state.toolExecutionsById);
  const viewingChildSessionId = useChatStore((state) => state.viewingChildSessionId);
  const childNodes = useChatStore((state) => state.childNodes);
  const exitChildSession = useChatStore((state) => state.exitChildSession);
  const { activeNodeId, entryNodeId, pendingProviderOverride, setPendingProviderOverride } = useSettingsStore();
  const viewMode = useViewStore(state => state.viewMode);
  const viewingChildNode = viewingChildSessionId ? childNodes[viewingChildSessionId] : undefined;
  const activeSessionId = viewingChildSessionId || activeConversation?.sessionId || '';
  // [2026-06-03] Why: child-session navigation renders a different chat stream while
  // the parent conversation remains selected. How: replace the header title with a
  // child label when viewingChildSessionId is set. Purpose: users can see they are
  // inspecting a child node and can return to the parent.
  const activeTitle = viewingChildSessionId
    ? (viewingChildNode?.nodeId ? `子节点: ${viewingChildNode.nodeId}` : `临时会话: ${viewingChildSessionId.slice(0, 8)}`)
    : activeConversation?.title || '未选择对话';

  // [2026-05-31] Startup loading now belongs to chatStore. Why: the canonical store
  // hydrates ConversationMeta and structured history through reducer-shaped data.
  // How: call loadStartup once from the store singleton. Purpose: avoid mounting the
  // legacy startup loader and its old message accumulator.
  useEffect(() => {
    useChatStore.getState().loadStartup();
  }, []);

  // [2026-06-01] Health check runs at App level so every registered view can show
  // the same connection status. Why: settings mode no longer mounts the legacy
  // SettingsPanel. How: update settingsStore.isConnected on an interval. Purpose:
  // Sidebar and SettingsSidebar share a current Supervisor health indicator.
  const { setConnected } = useSettingsStore();
  useEffect(() => {
    const check = async () => {
      try {
        await checkHealth();
        setConnected(true);
      } catch {
        setConnected(false);
      }
    };
    check();
    const iv = setInterval(check, 10000);
    return () => clearInterval(iv);
  }, [setConnected]);

  const handleSend = async (text: string, attachments?: Attachment[]) => {
    const nodeId = activeNodeId || entryNodeId || undefined;
    // [2026-06-16] Pass the welcome-page model selection so the backend applies it
    // during session creation, before the first task runs.
    const override = pendingProviderOverride && Object.keys(pendingProviderOverride).length > 0
      ? pendingProviderOverride
      : null;
    await sendMessage(text, attachments, nodeId, override);
    if (override) setPendingProviderOverride(null);
  };

  const handleReset = async () => {
    if (!activeConversationId) return;
    const convKey = `web:${activeConversationId}`;
    try { await resetConversation(convKey); } catch { /* ignore reset failures in the shell */ }
  };

  const view = viewRegistry[viewMode];
  const viewContext: AppViewContext = {
    sessionId: activeSessionId,
    title: activeTitle,
    conversations,
    activeConversationId,
    messages,
    toolsById,
    isGenerating,
    viewingChildSessionId,
    viewingChildNodeId: viewingChildNode?.nodeId,
    onExitChildSession: exitChildSession,
    onCreateConversation: createConversation,
    onSelectConversation: selectConversation,
    onDeleteConversation: deleteConversation,
    onSendMessage: handleSend,
    onTitleChange: activeConversationId
      ? (newTitle: string) => renameConversation(activeConversationId, newTitle)
      : undefined,
  };

  return (
    <AppLayout
      composer={view.composer?.(viewContext)}
      header={view.header(viewContext)}
      logPanel={view.rightBottom?.(viewContext)}
      rightOverlay={view.rightOverlay?.(viewContext)}
      rightPanel={view.rightTop?.(viewContext)}
      sidebar={view.sidebar(viewContext)}
    >
      {view.main(viewContext)}
    </AppLayout>
  );
};

const App = () => {
  const { isAuthenticated, needsSetup } = useSettingsStore();
  if (!isAuthenticated) return <LoginPage />;
  if (needsSetup) return <SetupWizard />;
  return <MainApp />;
};

export default App;
