// Workspace file tree browser.
// Replaces the right panel when activated from the Header workspace pill.
// Lazy-loads directory children on expand via the /v1/workspace/tree API.
import { useCallback, useEffect, useState } from 'react';

import { getWorkspaceTree, type FileTreeNode } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Icon } from '../common';

// ---- Tree node component ----

interface TreeNodeProps {
  node: FileTreeNode;
  sessionId: string;
  token: string;
  depth: number;
}

function TreeNode({ node, sessionId, token, depth }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<FileTreeNode[] | null>(node.children || null);
  const [loading, setLoading] = useState(false);

  const isDir = node.type === 'directory';

  const handleToggle = useCallback(async () => {
    if (!isDir) return;

    if (expanded) {
      setExpanded(false);
      return;
    }

    if (node.truncated || (!children && isDir)) {
      setLoading(true);
      try {
        const resp = await getWorkspaceTree(sessionId, token, {
          subPath: node.path === '.' ? '' : node.path,
          depth: 2,
        });
        setChildren(resp.tree.children || []);
      } catch {
        setChildren([]);
      } finally {
        setLoading(false);
      }
    }

    setExpanded(true);
  }, [isDir, expanded, node, children, sessionId, token]);

  const indent = depth * 16;
  const badge = node.truncated && !expanded
    ? ` (${node.childDirs || 0}d ${node.childFiles || 0}f)`
    : '';

  const iconName = isDir
    ? (expanded ? 'folder_open' : 'folder')
    : 'description';

  const displayChildren = expanded ? (children || node.children || []) : [];

  return (
    <>
      <button
        type="button"
        className={`flex w-full items-center gap-1 px-2 py-0.5 text-left font-mono text-xs transition-colors hover:bg-[var(--duties-hover)] text-[var(--duties-secondary)]`}
        style={{ paddingLeft: `${indent + 8}px` }}
        onClick={handleToggle}
        title={node.path}
      >
        {isDir && (
          <Icon
            name={expanded ? 'expand_more' : 'chevron_right'}
            size={14}
            className={`flex-shrink-0 ${loading ? 'animate-spin' : ''}`}
          />
        )}
        {!isDir && <span className="w-3.5 flex-shrink-0" />}
        <Icon name={iconName} size={14} className="flex-shrink-0" />
        <span className="truncate">
          {node.name}
          {badge && <span className="text-[var(--duties-tertiary)]">{badge}</span>}
        </span>
        {!isDir && node.size != null && (
          <span className="ml-auto flex-shrink-0 text-[0.6rem] text-[var(--duties-tertiary)]">
            {node.size > 1024 * 1024
              ? `${(node.size / 1024 / 1024).toFixed(1)}M`
              : node.size > 1024
                ? `${(node.size / 1024).toFixed(1)}K`
                : `${node.size}B`}
          </span>
        )}
      </button>
      {displayChildren.map((child) => (
        <TreeNode
          key={child.path}
          node={child}
          sessionId={sessionId}
          token={token}
          depth={depth + 1}
        />
      ))}
    </>
  );
}

// ---- Main component ----

interface WorkspaceFileTreeProps {
  sessionId: string;
  onClose: () => void;
}

export function WorkspaceFileTree({ sessionId, onClose }: WorkspaceFileTreeProps) {
  const adminToken = useSettingsStore((s) => s.adminToken) || '';
  const [tree, setTree] = useState<FileTreeNode | null>(null);
  const [workspacePath, setWorkspacePath] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!sessionId || !adminToken) return;
    setLoading(true);
    setError('');
    getWorkspaceTree(sessionId, adminToken, { depth: 2 })
      .then((resp) => {
        setTree(resp.tree);
        setWorkspacePath(resp.workspace_path);
      })
      .catch((err) => setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => setLoading(false));
  }, [sessionId, adminToken]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="flex flex-shrink-0 items-center gap-2 border-b border-[var(--duties-border)] px-3 py-2">
        <Icon name="folder" size={16} className="text-[var(--duties-secondary)]" />
        <span className="flex-1 truncate text-xs font-medium text-[var(--duties-text)]">
          工作区文件
        </span>
        <button
          type="button"
          className="flex-shrink-0 rounded p-0.5 text-[var(--duties-tertiary)] transition-colors hover:bg-[var(--duties-hover)] hover:text-[var(--duties-text)]"
          onClick={onClose}
          title="关闭文件树"
        >
          <Icon name="close" size={16} />
        </button>
      </div>

      {/* Workspace path subtitle */}
      {workspacePath && (
        <div className="flex-shrink-0 border-b border-[var(--duties-border)] px-3 py-1">
          <span className="font-mono text-[0.6rem] text-[var(--duties-tertiary)]" title={workspacePath}>
            {workspacePath}
          </span>
        </div>
      )}

      {/* Content */}
      {loading && (
        <div className="flex flex-1 items-center justify-center text-xs text-[var(--duties-tertiary)]">
          加载中…
        </div>
      )}
      {error && (
        <div className="flex flex-1 items-center justify-center px-4 text-center text-xs text-red-400">
          {error}
        </div>
      )}
      {!loading && !error && tree && (
        <div className="flex-1 overflow-auto">
          {(tree.children || []).map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              sessionId={sessionId}
              token={adminToken}
              depth={0}
            />
          ))}
          {(!tree.children || tree.children.length === 0) && (
            <div className="p-4 text-center text-xs text-[var(--duties-tertiary)]">
              空目录
            </div>
          )}
        </div>
      )}
      {!loading && !error && !tree && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-4 text-center text-[var(--duties-tertiary)]">
          <Icon name="folder_off" size={32} />
          <span className="text-xs">未设置工作区</span>
        </div>
      )}
    </div>
  );
}
