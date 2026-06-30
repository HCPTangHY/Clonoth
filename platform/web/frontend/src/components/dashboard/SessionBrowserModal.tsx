// [AutoC 2026-06-18] Session browser modal for the System settings tab.
// Why: the System page showed only a session count, while operators need to inspect
// every session, enter one temporarily, and handle related pending approvals. How:
// poll /v1/sessions, render a searchable list, and route entry through chatStore's
// temporary child-session view. Purpose: session inspection does not add rows to the
// normal conversation sidebar.
import { useEffect, useMemo, useState } from 'react';

import {
  deleteSession,
  listSessions,
  type AdminApproval,
  type SessionListItem,
} from '../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../store/settingsSelectionStore';
import { useChatStore } from '../../store/chatStore';
import { useViewStore } from '../../store/viewStore';
import { Button, Modal } from '../common';

interface SessionBrowserModalProps {
  open: boolean;
  onClose: () => void;
  pendingApprovals?: AdminApproval[];
}

type ChannelFilter = string;

function shortId(value: string, length = 8): string {
  return value ? value.slice(0, length) : '';
}

function formatRelative(value: string): string {
  const parsed = new Date(value).getTime();
  if (!Number.isFinite(parsed)) return '未知时间';
  const seconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function sessionTitle(session: SessionListItem): string {
  const key = session.conversation_key || '';
  if (key.startsWith('web:')) return key.slice(4) || 'web';
  return key || session.session_id;
}

function matchSession(session: SessionListItem, query: string): boolean {
  if (!query) return true;
  const haystack = [
    session.session_id,
    session.conversation_key,
    session.channel,
    session.parent_session_id || '',
  ].join(' ').toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function approvalsForSession(session: SessionListItem, approvals: AdminApproval[]): AdminApproval[] {
  return approvals.filter((approval) => approval.session_id === session.session_id);
}

function channelLabel(channel: string): string {
  return channel.trim() || 'unknown';
}

export const SessionBrowserModal = ({ open, onClose, pendingApprovals = [] }: SessionBrowserModalProps) => {
  const setSettingsTab = useViewStore(state => state.setSettingsTab);
  const setSelectedApproval = useSettingsSelectionStore(state => state.setSelectedApproval);
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [channel, setChannel] = useState<ChannelFilter>('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [deletingBySession, setDeletingBySession] = useState<Record<string, boolean>>({});
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    const load = async () => {
      try {
        setLoading(true);
        const nextSessions = await listSessions('', 200);
        if (!cancelled) {
          setSessions(nextSessions);
          setError('');
        }
      } catch (loadError) {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : '会话列表加载失败。');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(load, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [open, refreshTick]);

  const channelOptions = useMemo(
    () => {
      const counts = new Map<string, number>();
      for (const session of sessions) {
        const key = session.channel?.trim() || 'unknown';
        counts.set(key, (counts.get(key) || 0) + 1);
      }
      const sorted = [...counts.entries()]
        .sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));
      return [
        { channel: '', count: sessions.length },
        ...sorted.map(([itemChannel, count]) => ({ channel: itemChannel, count })),
      ];
    },
    [sessions],
  );

  useEffect(() => {
    if (!channel) return;
    if (channelOptions.length > 1 && !channelOptions.some(item => item.channel === channel)) setChannel('');
  }, [channel, channelOptions]);

  const filteredSessions = useMemo(
    () => sessions
      .filter((session) => !channel || (session.channel?.trim() || 'unknown') === channel)
      .filter((session) => matchSession(session, query.trim())),
    [channel, query, sessions],
  );

  const enterSession = (session: SessionListItem) => {
    useViewStore.getState().closeSettings();
    useChatStore.getState().viewChildSession(session.session_id, undefined, session.conversation_key);
    onClose();
  };

  const jumpToApprovals = (approval: AdminApproval | undefined) => {
    if (approval) setSelectedApproval(approval);
    setSettingsTab('approvals');
    onClose();
  };

  const handleDelete = async (session: SessionListItem) => {
    if (!window.confirm(`确认删除会话 ${session.session_id} 吗？`)) return;
    setDeletingBySession(current => ({ ...current, [session.session_id]: true }));
    try {
      const result = await deleteSession(session.session_id);
      if (!result.ok) setError('删除会话失败。');
      setRefreshTick(value => value + 1);
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : '删除会话失败。');
    } finally {
      setDeletingBySession(current => {
        const next = { ...current };
        delete next[session.session_id];
        return next;
      });
    }
  };

  return (
    <Modal
      ariaLabel="会话浏览"
      maxWidth="max-w-5xl"
      onClose={onClose}
      open={open}
      subtitle="会话管理"
      title="会话浏览"
    >
      <div className="space-y-3 p-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap gap-2">
            {channelOptions.map(option => {
              const value = option.channel;
              const active = channel === value;
              return (
                <button
                  className={`border px-2 py-1 font-mono text-[0.65rem] transition-colors ${active ? 'border-[var(--duties-text)] bg-[var(--duties-muted)] text-[var(--duties-text)]' : 'border-[var(--duties-border)] text-[var(--duties-secondary)] hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]'}`}
                  key={value || 'all'}
                  onClick={() => setChannel(value)}
                  type="button"
                >
                  {value ? channelLabel(value) : '全部'}
                  <span className="ml-1 text-[var(--duties-tertiary)]">{option.count}</span>
                </button>
              );
            })}
          </div>
          <Button disabled={loading} onClick={() => setRefreshTick(value => value + 1)}>
            {loading ? '刷新中...' : '刷新'}
          </Button>
        </div>

        <input
          className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-xs text-[var(--duties-text)] outline-none placeholder:text-[var(--duties-tertiary)] focus:border-[var(--duties-text)]"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索 session id、conversation key、channel、parent session"
          value={query}
        />

        <div className="flex flex-wrap items-center gap-3 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">
          <span>显示 {filteredSessions.length} / {sessions.length}</span>
          <span>待审批 {pendingApprovals.length}</span>
        </div>

        {error && (
          <div className="border border-orange-200 bg-orange-50 px-2.5 py-2 text-[0.65rem] text-orange-700">
            {error}
          </div>
        )}

        {loading && sessions.length === 0 ? (
          <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-4 text-center text-sm text-[var(--duties-secondary)]">
            正在加载会话列表…
          </div>
        ) : filteredSessions.length === 0 ? (
          <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-4 text-center text-sm text-[var(--duties-secondary)]">
            没有匹配的会话
          </div>
        ) : (
          <ul className="space-y-2">
            {filteredSessions.map((session) => {
              const relatedApprovals = approvalsForSession(session, pendingApprovals);
              const deleting = Boolean(deletingBySession[session.session_id]);
              return (
                <li className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3" key={session.session_id}>
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="border border-[var(--duties-border)] px-1.5 py-0.5 font-mono text-[0.6rem] text-[var(--duties-secondary)]">
                          {session.channel || 'unknown'}
                        </span>
                        {session.parent_session_id && (
                          <span className="border border-blue-500/30 bg-blue-500/10 px-1.5 py-0.5 font-mono text-[0.6rem] text-blue-700">
                            child
                          </span>
                        )}
                        {relatedApprovals.length > 0 && (
                          <button
                            className="border border-orange-500/40 bg-orange-500/10 px-1.5 py-0.5 font-mono text-[0.6rem] text-orange-700 transition-colors hover:bg-orange-500/20"
                            onClick={() => jumpToApprovals(relatedApprovals[0])}
                            type="button"
                          >
                            待审批 {relatedApprovals.length}
                          </button>
                        )}
                      </div>
                      <p className="mt-2 truncate font-mono text-xs font-semibold text-[var(--duties-text)]" title={session.conversation_key}>
                        {sessionTitle(session)}
                      </p>
                      <div className="mt-2 grid gap-1 text-[0.65rem] leading-5 text-[var(--duties-secondary)] sm:grid-cols-2">
                        <p>session：<span className="font-mono">{session.session_id}</span></p>
                        <p>短号：<span className="font-mono">{shortId(session.session_id)}</span></p>
                        <p className="sm:col-span-2">conversation：<span className="font-mono">{session.conversation_key || '未提供'}</span></p>
                        <p>更新时间：<span className="font-mono">{formatRelative(session.updated_at)}</span></p>
                        <p>创建时间：<span className="font-mono">{formatRelative(session.created_at)}</span></p>
                        {session.parent_session_id && (
                          <p className="sm:col-span-2">父会话：<span className="font-mono">{session.parent_session_id}</span></p>
                        )}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2 lg:justify-end">
                      <Button onClick={() => enterSession(session)} title="临时打开该会话，不加入侧边栏" variant="primary">打开会话</Button>
                      {relatedApprovals.length > 0 && (
                        <Button onClick={() => jumpToApprovals(relatedApprovals[0])}>去审批</Button>
                      )}
                      <Button disabled={deleting} onClick={() => { void handleDelete(session); }} variant="danger">
                        {deleting ? '删除中...' : '删除'}
                      </Button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Modal>
  );
};
