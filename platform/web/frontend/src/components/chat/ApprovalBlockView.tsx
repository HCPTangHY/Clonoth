// ApprovalBlockView renders standalone approval blocks that are not attached to a
// ToolExecution (e.g. legacy events without tool_call_id).
import { useState } from 'react';

import { decideApproval } from '../../api/supervisorClient';
import type { ApprovalBlock } from '../../types/message';
import { Icon } from '../common';

interface ApprovalBlockViewProps {
  block: ApprovalBlock;
}

export const ApprovalBlockView = ({ block }: ApprovalBlockViewProps) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const isPending = block.status === 'pending';
  const details = (block.details || {}) as Record<string, unknown>;
  const path = typeof details.path === 'string' ? details.path : '';
  const reason = typeof details.reason === 'string' ? details.reason : '';

  const handleDecision = async (decision: 'allow' | 'deny') => {
    setLoading(true);
    setError('');
    try {
      await decideApproval(block.approvalId, decision, `${decision} via approval block`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border-l-2 border-orange-400 pl-3 font-mono text-[0.72rem] text-[var(--duties-secondary)]">
      <div className="flex items-center gap-1.5 text-[0.65rem]">
        <span className="text-orange-500">
          <Icon name="verified_user" size={13} />
        </span>
        <span className="font-semibold">{block.operation || '审批请求'}</span>
        {block.status !== 'pending' && (
          <span className={`ml-1.5 ${block.status === 'allowed' ? 'text-green-600' : 'text-red-500'}`}>
            {block.status === 'allowed' ? '已批准' : '已拒绝'}
          </span>
        )}
      </div>

      {(path || reason) && (
        <div className="mt-1 space-y-0.5 text-[0.66rem]">
          {path && <div><span className="text-[var(--duties-tertiary)]">路径：</span><code>{path}</code></div>}
          {reason && <div><span className="text-[var(--duties-tertiary)]">原因：</span>{reason}</div>}
        </div>
      )}

      {isPending && (
        <div className="mt-1.5 flex items-center gap-2">
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm bg-green-50 px-1.5 py-0.5 text-[0.65rem] font-medium text-green-700 hover:bg-green-100 disabled:opacity-60"
            onClick={() => handleDecision('allow')}
            disabled={loading}
          >
            <Icon name="check_circle" size={13} />
            <span>允许</span>
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm bg-red-50 px-1.5 py-0.5 text-[0.65rem] font-medium text-red-600 hover:bg-red-100 disabled:opacity-60"
            onClick={() => handleDecision('deny')}
            disabled={loading}
          >
            <Icon name="cancel" size={13} />
            <span>拒绝</span>
          </button>
          {loading && <span className="text-[var(--duties-tertiary)]">提交中…</span>}
        </div>
      )}

      {(block.comment || block.decision) && (
        <div className="mt-1 text-[0.66rem] text-[var(--duties-tertiary)]">
          {block.decision && <span className="mr-2">决定：{block.decision}</span>}
          {block.comment && <span>备注：{block.comment}</span>}
        </div>
      )}

      {error && <div className="mt-1 text-[0.65rem] font-semibold text-red-600">{error}</div>}
    </div>
  );
};
