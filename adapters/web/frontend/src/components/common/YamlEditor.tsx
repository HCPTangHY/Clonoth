// [2026-06-02] Shared raw text editor for YAML, Markdown, and Python settings files.
// Why: many new settings tabs edit backend-owned raw config text. How: expose a thin
// controlled textarea with monospace styling, optional read-only mode, and a small
// indentation sanity check. Purpose: callers can own save/cancel behavior while the
// editor stays visually and behaviorally consistent.
import type { ChangeEvent } from 'react';

interface YamlEditorProps {
  value: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  height?: number | string;
  placeholder?: string;
  'aria-label'?: string;
}

function normalizeEditorHeight(height: number | string): string {
  // [2026-06-02] Accept both numeric and CSS-string heights.
  // Why: the original editor contract allows callers to pass a simple number while
  // existing tests and pages pass CSS values. How: convert numbers to pixel strings
  // and leave strings unchanged. Purpose: the shared editor remains flexible without
  // forcing page-level conversion code.
  return typeof height === 'number' ? `${height}px` : height;
}

export function hasSuspiciousYamlTabs(value: string): boolean {
  // [2026-06-02] Provide a lightweight YAML-specific check without adding js-yaml.
  // Why: the project does not currently depend on a YAML parser in the frontend. How:
  // flag leading tab indentation, which YAML rejects and users can fix immediately.
  // Purpose: save buttons can warn about a common syntax problem while final schema
  // validation remains on the backend.
  return value.split('\n').some((line) => /^\t+/.test(line));
}

export const YamlEditor = ({ value, onChange, readOnly = false, height = '300px', placeholder, 'aria-label': ariaLabel = 'YAML 编辑器' }: YamlEditorProps) => (
  <textarea
    aria-label={ariaLabel}
    className="w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] disabled:opacity-70"
    onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onChange(event.target.value)}
    placeholder={placeholder}
    readOnly={readOnly}
    spellCheck={false}
    // [2026-06-02] Use the caller-provided CSS height directly.
    // Why: the first infrastructure batch specifies height as a CSS height string
    // with a 300px default. How: bind the value to textarea height instead of
    // minHeight. Purpose: shared settings editors render predictably in fixed and
    // resizable panels.
    style={{ height: normalizeEditorHeight(height) }}
    value={value}
  />
);
