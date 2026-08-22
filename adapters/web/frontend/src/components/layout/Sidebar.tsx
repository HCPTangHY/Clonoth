// [2026-05-16] Upgraded: connection status from settingsStore, settings toggle, delete conversation.
// [2026-05-31] Step 3 accepts V2 ConversationMeta rows instead of legacy
// Conversation objects. Why: chatStore keeps message bodies normalized outside the
// sidebar list. How: render title, session preview, and updated time without reading a
// messages array. Purpose: let App switch stores without changing sidebar behavior.
import { useMemo } from 'react';

import { useChatStore, type ChildNodeState, type ConversationMeta } from '../../store/chatStore';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { Button, getChildNodeStatusLabel, Icon, StatusDot } from '../common';
import { PluginSlotHost } from '../plugins/PluginSlotHost';

interface SidebarProps {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  onCreateConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => void;
  childNodesByConversation?: Record<string, ChildNodeState[]>;
}

const formatTime = (isoDate: string) =>
  new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(isoDate));

function groupChildNodesByConversation(
  childNodes: Readonly<Record<string, ChildNodeState>>,
  conversations: ConversationMeta[],
): Record<string, ChildNodeState[]> {
  // [2026-06-03] Sidebar receives a flat normalized childNodes map from chatStore.
  // Why: hooks cannot be called inside conversations.map for each row. How: group the
  // map once per render and sort each child list by startedAt. Purpose: each parent
  // conversation can render a stable tree without violating React hook rules.
  const knownConversationIds = new Set(conversations.map((conversation) => conversation.id));
  const grouped: Record<string, ChildNodeState[]> = {};

  Object.values(childNodes).forEach((child) => {
    if (!knownConversationIds.has(child.parentConversationId)) return;
    // [AutoC 2026-06-04] Filter out system nodes.
    if (child.nodeId.startsWith('system.')) return;
    grouped[child.parentConversationId] = [...(grouped[child.parentConversationId] || []), child];
  });

  Object.values(grouped).forEach((children) => {
    children.sort((a, b) => (a.startedAt || '').localeCompare(b.startedAt || ''));
  });

  return grouped;
}

export const Sidebar = ({
  conversations, activeConversationId,
  onCreateConversation, onSelectConversation, onDeleteConversation,
  childNodesByConversation: providedChildNodesByConversation,
}: SidebarProps) => {
  const isConnected = useSettingsStore(state => state.isConnected);
  const openSettings = useViewStore(state => state.openSettings);
  const childNodeMap = useChatStore(state => state.childNodes);
  const viewChildSession = useChatStore(state => state.viewChildSession);
  const groupedChildNodes = useMemo(
    () => providedChildNodesByConversation || groupChildNodesByConversation(childNodeMap, conversations),
    [childNodeMap, conversations, providedChildNodesByConversation],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b border-[var(--duties-border)] p-3">
        <div className="flex items-center gap-2.5">
          <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-9 w-9 rounded-lg" />
          <div>
            <h1 className="font-mono text-base font-semibold tracking-[-0.04em]">Clonoth</h1>
            <p className="text-[0.6rem] text-[var(--duties-tertiary)]">调度器网页界面</p>
          </div>
        </div>
        <Button className="mt-3 w-full" onClick={onCreateConversation} variant="primary">
          新对话
        </Button>
      </div>

      {/* Conversation list */}
      <nav aria-label="对话" className="min-h-0 flex-1 overflow-y-auto">
        {conversations.length === 0 && (
          <p className="p-3 text-center text-xs text-[var(--duties-tertiary)]">暂无对话</p>
        )}
        {conversations.map((conv) => {
          const isActive = conv.id === activeConversationId;
          const childNodes = groupedChildNodes[conv.id] || [];
          return (
            <div
              className={`group relative border-b border-[var(--duties-border)] transition-colors hover:bg-[var(--duties-accent)] ${
                isActive ? 'bg-[var(--duties-muted)]' : 'bg-transparent'
              }`}
              key={conv.id}
            >
              <button
                className="block w-full p-3 text-left focus:outline-none"
                onClick={() => onSelectConversation(conv.id)}
                type="button"
              >
                <span className="block font-mono text-xs font-semibold">{conv.title}</span>
                <span className="mt-1.5 flex items-center gap-1 truncate text-[0.7rem] text-[var(--duties-tertiary)]">
                  {conv.workspaceName
                    ? <><Icon name="folder" size={10} className="shrink-0" /><span className="truncate">{conv.workspaceName}</span></>
                    : conv.sessionId ? `会话 ${conv.sessionId.slice(0, 8)}` : '暂无会话'}
                </span>
                <span className="mt-2 block font-mono text-[0.6rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">
                  {formatTime(conv.updatedAt)}
                </span>
              </button>
              {/* Reserved slot: plugins may append badges under each conversation row. */}
              <PluginSlotHost
                className="px-3 pb-1"
                data={{ conversationId: conv.id, sessionId: conv.sessionId, title: conv.title }}
                slot="session_item_suffix"
              />
              {/* Delete button — visible on hover (desktop) or always visible (mobile).
                  [2026-06-01] Why: remove the literal close glyph used for deletion.
                  How: render the Material Symbols delete icon through Icon. Purpose:
                  destructive conversation controls are visually clear and consistent. */}
              <button
                className="absolute right-2 top-2 text-xs text-[var(--duties-tertiary)] hover:text-red-500 md:hidden md:group-hover:block"
                onClick={(e) => { e.stopPropagation(); onDeleteConversation(conv.id); }}
                title="删除对话"
                type="button"
              >
                <Icon name="delete" size={15} />
              </button>
              {childNodes.length > 0 && childNodes.map((child) => (
                <div
                  className="ml-3 border-l border-[var(--duties-border)] pl-6"
                  key={child.sessionId}
                >
                  <button
                    aria-label={`子节点 ${child.nodeId}`}
                    className="block w-full p-2 text-left text-[0.65rem] text-[var(--duties-secondary)] transition-colors hover:bg-[var(--duties-muted)]"
                    onClick={(event) => {
                      event.stopPropagation();
                      // [2026-06-03] Why: the sidebar child row should open the same
                      // child stream as the floating panel. How: call the store-level
                      // navigation action with the child session id. Purpose: users can
                      // inspect delegated work directly from the conversation tree.
                      viewChildSession(child.sessionId);
                    }}
                    type="button"
                  >
                    {/* [2026-06-03] Render child sessions as navigable tree rows.
                        Why: Phase 3 adds child-session chat streams. How: keep the
                        shared status dot, node id, and start time while wiring the row
                        to chatStore.viewChildSession. Purpose: users can see and open
                        delegated work without creating a separate sidebar conversation. */}
                    <span className="flex items-center gap-1.5">
                      <StatusDot
                        label={`子节点 ${child.nodeId} 状态：${getChildNodeStatusLabel(child.status)}`}
                        status={child.status}
                      />
                      <span className="font-mono font-medium">{child.nodeId}</span>
                    </span>
                    {child.startedAt && (
                      <span className="mt-0.5 block text-[var(--duties-tertiary)]">
                        {formatTime(child.startedAt)}
                      </span>
                    )}
                  </button>
                </div>
              ))}
            </div>
          );
        })}
      </nav>

      {/* Bottom — connection status + settings */}
      <div className="border-t border-[var(--duties-border)] p-3">
        <div className="flex items-center gap-2 text-[0.7rem] text-[var(--duties-tertiary)]">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isConnected ? '已连接' : '已断开'}
        </div>
        <button
          className="mt-2 flex w-full items-center gap-2 px-2 py-1.5 text-xs text-[var(--duties-secondary)] transition-colors hover:bg-[var(--duties-muted)]"
          onClick={() => {
            // [2026-06-01] Settings is now a full registered view, not a legacy
            // right-panel toggle. Why: the left sidebar must be replaced by
            // settings categories while the right panel remains independent. How:
            // route through viewStore.openSettings(). Purpose: AppLayout receives
            // settings slots from viewRegistry without Sidebar knowing the details.
            openSettings();
          }}
          type="button"
        >
          {/* [2026-06-01] Why: replace the settings gear emoji with Material Symbols.
              How: render the shared Icon using the settings symbol. Purpose: sidebar
              actions stay on the same icon system as layout and message controls. */}
          <Icon name="settings" size={16} />
          <span>设置</span>
        </button>
      </div>
    </div>
  );
};
