import type { ApprovalLevel } from './clientPrefsStore';

export interface ApprovalRule {
  tool?: string;
  operation?: string;
  trust_levels?: Array<'' | 'workspace' | 'trusted' | 'external'>;
  action: 'ask' | 'allow';
}

export interface SessionApprovalRules {
  version: 1;
  default_action: 'ask' | 'allow';
  rules: ApprovalRule[];
}

export interface SessionApprovalState {
  rules: SessionApprovalRules | null;
  effective_rules: SessionApprovalRules | null;
  source_session_id: string | null;
}

// Preset names belong to Web; Supervisor receives only the sparse rule difference.
export function compileApprovalPreset(
  level: ApprovalLevel,
  tools: Record<string, boolean> = {},
): SessionApprovalRules {
  if (level !== 'smart') {
    return { version: 1, default_action: level === 'yolo' ? 'allow' : 'ask', rules: [] };
  }
  return {
    version: 1,
    default_action: 'allow',
    rules: [
      { trust_levels: ['trusted', 'external'], action: 'ask' },
      { operation: 'restart', action: 'ask' },
      { tool: 'request_restart', action: 'ask' },
      ...Object.keys(tools).sort()
        .filter((tool) => tools[tool] === false && tool !== 'request_restart' && tool !== 'restart')
        .map((tool): ApprovalRule => ({ tool, action: 'ask' })),
    ],
  };
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonical(child)}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'undefined';
}

export function identifyApprovalPreset(rules: SessionApprovalRules | null): {
  level: ApprovalLevel | 'custom';
  tools: Record<string, boolean>;
} {
  if (!rules || rules.version !== 1 || !Array.isArray(rules.rules)
    || rules.rules.some((rule) => !rule || typeof rule !== 'object')) return { level: 'custom', tools: {} };
  for (const level of ['manual', 'yolo'] as const) {
    if (canonical(rules) === canonical(compileApprovalPreset(level))) return { level, tools: {} };
  }
  const tools: Record<string, boolean> = {};
  for (const rule of rules.rules) {
    if (rule.tool && rule.tool !== 'request_restart') {
      if (/[?*\[\]]/.test(rule.tool) || rule.action !== 'ask') return { level: 'custom', tools: {} };
      tools[rule.tool] = false;
    }
  }
  // Smart rules all ask, so ordering and duplicate conditions do not change semantics.
  // Unknown fields or mixed allow/ask conditions still produce a custom selection.
  const normalize = (value: SessionApprovalRules) => canonical({
    ...value,
    rules: [...new Set(value.rules.map((rule) => canonical({
      ...rule,
      ...(rule.trust_levels ? { trust_levels: [...new Set(rule.trust_levels)].sort() } : {}),
    })))].sort(),
  });
  if (normalize(rules) === normalize(compileApprovalPreset('smart', tools))) {
    return { level: 'smart', tools };
  }
  return { level: 'custom', tools: {} };
}
