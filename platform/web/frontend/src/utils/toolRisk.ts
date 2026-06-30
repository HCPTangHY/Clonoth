// [2026-06-02] Tool risk inference for settings and auto-approval UI.
// Why: maintaining hard-coded risk levels in multiple components becomes stale as
// Supervisor registers new built-in, MCP, and external tools. How: classify risk from
// stable verb prefixes while keeping unknown tools conservative. Purpose: the client
// can show a consistent risk badge everywhere without editing curated tool metadata.
export type RiskLevel = 'low' | 'medium' | 'high';

export function inferToolRisk(toolName: string): RiskLevel {
  if (/^(read_|list_|search_|get_|mcp_)/.test(toolName)) return 'low';
  if (/^(execute_|restart_|remote_|request_restart)/.test(toolName)) return 'high';
  if (/^(write_|apply_|delete_|create_)/.test(toolName)) return 'medium';
  return 'medium';
}

export function riskLabel(risk: RiskLevel): string {
  // [2026-06-02] Centralize Chinese labels beside the inferred enum. Why: badges in
  // multiple settings pages should not drift in wording. How: map each level in one
  // helper. Purpose: future wording changes touch only this file.
  if (risk === 'low') return '低风险';
  if (risk === 'medium') return '中风险';
  return '高风险';
}

export function riskClassName(risk: RiskLevel): string {
  // [2026-06-02] Centralize visual risk colors beside the inferred enum. Why: Client
  // settings and Tool settings both need the same low/medium/high badge style. How:
  // return Tailwind utility classes keyed by risk. Purpose: risk display remains
  // consistent without extracting a larger design-system component.
  if (risk === 'low') return 'border-green-200 bg-green-50 text-green-700';
  if (risk === 'medium') return 'border-orange-200 bg-orange-50 text-orange-700';
  return 'border-red-200 bg-red-50 text-red-700';
}
