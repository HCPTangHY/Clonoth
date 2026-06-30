// [2026-06-02] Global approvals settings page.
// Why: pending approvals can originate from any session, so operators need a global
// list outside individual chat cards. How: read pending_approvals from admin state,
// expose allow and deny actions, and mirror the selected approval into the right
// panel. Purpose: approvals can be reviewed and decided from Settings.
import { useCallback, useEffect, useState } from 'react';

import { decideApproval, getAdminState, type AdminApproval } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button, Icon } from '../../common';
import { inferToolRisk, riskClassName, riskLabel } from '../../../utils/toolRisk';
import { AuthRequired, Card, PageHeader, PageShell, StatusText } from './settingsPagePrimitives';

function detailString(approval: AdminApproval, key: string): string {
  // [2026-06-02] Read common approval detail fields from arbitrary backend payloads.
  // Why: approval records come from different tools and do not share one fixed shape.
  // How: return a string value only when the requested detail is present. Purpose:
  // the P0 approval list can show operation, path, and reason without unsafe casts.
  const value = approval.details?.[key];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}

function approvalToolName(approval: AdminApproval): string {
  // [2026-06-02] Prefer the actual tool name for risk inference and row headings.
  // Why: operation text may be localized or descriptive, while inferToolRisk expects
  // stable tool identifiers such as execute_command. How: inspect common detail keys
  // before falling back to operation or tool_call_id. Purpose: the risk badge matches
  // the tool being approved.
  return detailString(approval, 'tool_name') || detailString(approval, 'tool') || approval.operation || approval.tool_call_id || approval.approval_id;
}

function approvalPath(approval: AdminApproval): string {
  return detailString(approval, 'path') || detailString(approval, 'command') || detailString(approval, 'url') || detailString(approval, 'name');
}

function approvalReason(approval: AdminApproval): string {
  return detailString(approval, 'reason') || detailString(approval, 'description') || detailString(approval, 'summary') || approval.comment || '未提供';
}

export const ApprovalsSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { selectedApproval, setSelectedApproval } = useSettingsSelectionStore();
  const [approvals, setApprovals] = useState<AdminApproval[]>([]);
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (showSpinner = true) => {
    if (!adminToken || !isAuthenticated) return;
    if (showSpinner) setLoading(true);
    setMessage('');
    try {
      const state = await getAdminState(adminToken);
      const pending = state.pending_approvals || [];
      setApprovals(pending);
      if (selectedApproval && !pending.some((item) => item.approval_id === selectedApproval.approval_id)) setSelectedApproval(null);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载审批失败');
    } finally {
      if (showSpinner) setLoading(false);
    }
  }, [adminToken, isAuthenticated, selectedApproval, setSelectedApproval]);

  useEffect(() => {
    void load();
    if (!adminToken || !isAuthenticated) return undefined;
    // [2026-06-02] Refresh pending approvals every 10 seconds.
    // Why: approvals may be created or decided by other sessions while this tab is
    // open. How: poll the admin state quietly between manual refreshes. Purpose:
    // operators can make decisions from a current queue.
    const timer = window.setInterval(() => { void load(false); }, 10000);
    return () => window.clearInterval(timer);
  }, [adminToken, isAuthenticated, load]);

  const decide = async (approval: AdminApproval, decision: 'allow' | 'deny') => {
    try {
      await decideApproval(approval.approval_id, decision, `settings ${decision}`);
      setMessage(decision === 'allow' ? '已允许审批' : '已拒绝审批');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '审批操作失败');
    }
  };

  return (
    <PageShell>
      <PageHeader description="查看所有会话的待审批项，并对工具或操作请求执行允许或拒绝。" title="审批" />
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="全局待审批列表" description="列表来自 /v1/admin/state 的 pending_approvals 字段。">
          <div className="mb-3 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={() => load()}>{loading ? '刷新中...' : '刷新列表'}</Button>
          </div>
          {approvals.length === 0 ? (
            <div className="flex items-center gap-2 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-4 text-sm text-[var(--duties-secondary)]">
              {/* [2026-06-02] Add a muted icon to the empty approvals state. Why: the
                  requested empty state includes both text and a gray visual marker.
                  How: use the shared Material Symbol icon with tertiary color.
                  Purpose: the empty queue is clear without looking like an alert. */}
              <Icon className="text-[var(--duties-tertiary)]" name="inbox" size={18} />
              <span>暂无待审批项</span>
            </div>
          ) : (
            <div className="space-y-2">
              {approvals.map((approval) => (
                <article
                  className={`border p-3 ${selectedApproval?.approval_id === approval.approval_id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`}
                  key={approval.approval_id}
                >
                  <button className="block w-full text-left" onClick={() => setSelectedApproval(approval)} type="button">
                    <div className="flex flex-wrap items-center gap-2">
                      {(() => {
                        const toolName = approvalToolName(approval);
                        const risk = inferToolRisk(toolName);
                        return (
                          <>
                            <span className="font-mono text-xs font-semibold">{toolName}</span>
                            <span className={`border px-1.5 py-0.5 font-mono text-[0.55rem] ${riskClassName(risk)}`}>{riskLabel(risk)}</span>
                          </>
                        );
                      })()}
                      <span className="font-mono text-[0.65rem] text-[var(--duties-tertiary)]">{approval.tool_call_id || approval.approval_id}</span>
                    </div>
                    <div className="mt-2 grid gap-1 text-xs leading-5 text-[var(--duties-secondary)] sm:grid-cols-2">
                      <p>节点：<span className="font-mono">{approval.node_id || '未提供'}</span></p>
                      <p>任务：<span className="font-mono">{approval.task_id || '未提供'}</span></p>
                      <p>操作：<span className="font-mono">{approval.operation || '未提供'}</span></p>
                      <p>路径：<span className="font-mono">{approvalPath(approval) || '未提供'}</span></p>
                      <p className="sm:col-span-2">原因：<span>{approvalReason(approval)}</span></p>
                    </div>
                  </button>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button onClick={() => decide(approval, 'allow')} variant="primary">允许</Button>
                    <Button onClick={() => decide(approval, 'deny')} variant="danger">拒绝</Button>
                  </div>
                </article>
              ))}
            </div>
          )}
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
