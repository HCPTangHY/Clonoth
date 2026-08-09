// [2026-06-02] Small shared primitives for the expanded Settings pages.
// Why: eight new tabs share the same header, auth notice, card, status, and text
// control patterns. How: keep the repeated JSX in one local settings helper file.
// Purpose: new pages stay readable while preserving the existing Duties visual style.
import type { ReactNode } from 'react';

export const PageShell = ({ children }: { children: ReactNode }) => (
  <section className="h-full min-h-0 overflow-y-auto p-4 sm:p-6">
    <div className="mx-auto max-w-4xl space-y-5">{children}</div>
  </section>
);

export const PageHeader = ({ eyebrow = '设置', title, description }: { eyebrow?: string; title: string; description: string }) => (
  <header>
    <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">{eyebrow}</p>
    <h1 className="mt-2 font-mono text-xl font-semibold tracking-[-0.04em]">{title}</h1>
    <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--duties-secondary)]">{description}</p>
  </header>
);

export const Card = ({ title, description, children }: { title?: string; description?: string; children: ReactNode }) => (
  <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
    {(title || description) && (
      <div className="mb-3">
        {title && <h2 className="font-mono text-sm font-semibold">{title}</h2>}
        {description && <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">{description}</p>}
      </div>
    )}
    {children}
  </section>
);

export const AuthRequired = () => (
  <Card>
    {/* [2026-06-02] Use the exact cross-tab authentication guidance requested for
        P0 Settings pages. Why: System, Approvals, and Advanced depend on protected
        Admin API endpoints and should direct users to the one login location. How:
        render the same Chinese sentence from the shared helper. Purpose: every
        protected settings page has consistent empty-auth copy. */}
    <p className="text-sm leading-6 text-[var(--duties-secondary)]">请先在通用页面登录 Admin Token</p>
  </Card>
);

export const StatusText = ({ message }: { message: string }) => (
  message ? <p className="mt-2 text-xs leading-5 text-[var(--duties-tertiary)]">{message}</p> : null
);

export const FieldLabel = ({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) => (
  <label className="mb-1 block text-xs font-semibold text-[var(--duties-secondary)]" htmlFor={htmlFor}>{children}</label>
);

export const TextInput = (props: React.InputHTMLAttributes<HTMLInputElement>) => (
  <input
    {...props}
    className={`w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] ${props.className || ''}`}
  />
);

export const SelectInput = (props: React.SelectHTMLAttributes<HTMLSelectElement>) => (
  <select
    {...props}
    className={`w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] ${props.className || ''}`}
  />
);

export function formatUptime(seconds: number | undefined): string {
  // [2026-06-02] Shared uptime formatter for settings system status. Why: health can
  // report zero seconds after startup. How: treat only undefined and negative values
  // as unknown, then show compact Chinese units. Purpose: runtime display stays clear.
  if (seconds === undefined || seconds < 0) return '未知';
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}天 ${hours}小时`;
  if (hours > 0) return `${hours}小时 ${minutes}分钟`;
  if (minutes > 0) return `${minutes}分钟`;
  return `${total}秒`;
}

export function countActiveTasks(tasks: Record<string, number> | undefined): number {
  // [2026-06-02] Count non-terminal task buckets for settings status cards. Why:
  // pending and suspended tasks are still operational work. How: add running,
  // pending, and suspended buckets. Purpose: the count matches admin dashboard usage.
  return (tasks?.running || 0) + (tasks?.pending || 0) + (tasks?.suspended || 0);
}

export function hasLikelyYamlSyntaxIssue(value: string): string {
  // [2026-06-02] Lightweight frontend YAML check. Why: js-yaml is not installed and
  // adding a dependency is unnecessary for this task. How: catch leading tab
  // indentation and unclosed quotes before saving, then let the backend perform final
  // validation. Purpose: users get immediate feedback for common raw-config mistakes.
  if (value.split('\n').some((line) => /^\t+/.test(line))) return 'YAML 缩进不能使用制表符。';
  const singleQuotes = (value.match(/'/g) || []).length;
  const doubleQuotes = (value.match(/"/g) || []).length;
  if (singleQuotes % 2 === 1 || doubleQuotes % 2 === 1) return '文本中可能存在未闭合的引号。';
  return '';
}
