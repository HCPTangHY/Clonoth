from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import classify_path, load_yaml_dict, parse_extra_roots

from .types import SafetyLevel


# [AutoC 2026-06-30] Git push writes to a remote repository. It must keep a
# human approval step even when execute_command is otherwise configured as auto.
_GIT_REMOTE_PUSH_PATTERN = r"(?:^|[;&|()\n])\s*(?:(?:sudo|command|time)\s+)*(?:env\s+\S+\s+)*git(?:\s+(?:-[A-Za-z](?:\s+\S+)?|--[A-Za-z0-9][A-Za-z0-9-]*(?:=\S+)?))*\s+push(?:\s|$)"


@dataclass(frozen=True)
class PolicyDecision:
    safety_level: SafetyLevel
    reason: str
    trust_level: str = ""  # 'workspace' | 'trusted' | 'external' | '' (non-path ops)


def _default_policy_dict() -> dict[str, Any]:
    """Built-in default policy.

    注意：在不引入 OS 沙盒的前提下，这套策略不是强安全边界；
    它是控制面约束和人类确认的执行规范。

    命令审核采用双层机制：
    1. cmd_reviewer 节点做 AI 前置审核；
    2. Supervisor 继续做人类审批与硬规则兜底。

    AI 审核不能替代人类审批。
    """

    return {
        "version": 1,
        "extra_roots": [],
        "read_file": {
            "default": "auto",
            "rules": [
                {"pattern": ".env", "decision": "deny", "reason": "do not allow reading dotenv secrets"},
                {"pattern": "**/.env", "decision": "deny", "reason": "do not allow reading dotenv secrets"},
            ],
        },
        "write_file": {
            "default": "auto",
            "rules": [
                {"pattern": "tools/**", "decision": "approval_required", "reason": "creating/updating tools requires approval"},
                {"pattern": "config/runtime.yaml", "decision": "auto", "reason": "runtime tuning config"},
                {"pattern": "config/nodes/**", "decision": "approval_required", "reason": "node definition changes affect execution, prompts, and model selection"},
                {"pattern": "data/config.yaml", "decision": "approval_required", "reason": "config changes require approval"},
                {"pattern": "data/policy.yaml", "decision": "deny", "reason": "policy is high-risk (human-only)"},
                {"pattern": "data/events.jsonl", "decision": "deny", "reason": "event log is append-only; never modify"},
                {"pattern": "data/schedules.yaml", "decision": "approval_required", "reason": "schedule changes require approval"},
                {"pattern": "engine/**", "decision": "approval_required", "reason": "engine source changes require approval"},
                {"pattern": "supervisor/**", "decision": "approval_required", "reason": "supervisor source changes require approval"},
                {"pattern": "toolbox/**", "decision": "approval_required", "reason": "toolbox source changes require approval"},
                {"pattern": "providers/**", "decision": "approval_required", "reason": "provider source changes require approval"},
                {"pattern": "shell/**", "decision": "approval_required", "reason": "shell source changes require approval"},
                {"pattern": "clonoth_runtime.py", "decision": "approval_required", "reason": "runtime lib changes require approval"},
                {"pattern": "main.py", "decision": "approval_required", "reason": "entrypoint changes require approval"},
                {"pattern": ".env", "decision": "deny", "reason": "do not allow writing dotenv secrets"},
                {"pattern": "**/.env", "decision": "deny", "reason": "do not allow writing dotenv secrets"},
            ],
        },
        "execute_command": {
            "default": "approval_required",
            "approval_patterns": [
                _GIT_REMOTE_PUSH_PATTERN,
            ],
            "deny_patterns": [
                r"\brm\s+-rf\s+/",
                r"\brm\s+-rf\s+~",
                r"\brm\s+-rf\s+\*",
                r"\bformat\b",
                r"\bmkfs\b",
                r"\bfdisk\b",
                r"\bdd\s+if=/dev/zero\b",
                r"\bshutdown\b",
                r"\breboot\b",
            ],
        },
        "restart": {
            "default": "approval_required",
        },
        # [AutoC 2026-08-10] 工具级审批：所有真实工具调用先过 policy。
        # 伪工具（finish / intermediate_reply 等）不经过 registry，天然跳过。
        # 内部已有路径/命令级审批的工具配 auto，避免双重拦截；
        # 其余工具（生图、记忆、MCP 等）默认 approval_required，由前端/Discord
        # 按 trust_level（空 = 无路径）+ 用户偏好自动批准或弹人工审批。
        "tools": {
            "default": "approval_required",
            "rules": [
                {"name": "read_file", "decision": "auto", "reason": "路径级审批在工具内部处理"},
                {"name": "write_file", "decision": "auto", "reason": "路径级审批在工具内部处理"},
                {"name": "apply_diff", "decision": "auto", "reason": "路径级审批在工具内部处理"},
                {"name": "execute_command", "decision": "auto", "reason": "命令级审批在工具内部处理"},
                {"name": "request_restart", "decision": "auto", "reason": "restart 审批在工具内部处理（强制人工）"},
                {"name": "grep", "decision": "auto", "reason": "只读，replace 模式内部审批"},
                {"name": "list_dir", "decision": "auto", "reason": "只读目录"},
                {"name": "search_in_files", "decision": "auto", "reason": "只读搜索"},
                {"name": "create_schedule", "decision": "auto", "reason": "内部 write_file 审批 data/schedules.yaml"},
                {"name": "delete_schedule", "decision": "auto", "reason": "内部 write_file 审批 data/schedules.yaml"},
                {"name": "delete_skill", "decision": "auto", "reason": "内部 write_file 审批 skills/**"},
            ],
        },
    }


def _to_safety_level(s: str) -> SafetyLevel:
    v = (s or "").strip()
    try:
        return SafetyLevel(v)
    except Exception:
        return SafetyLevel.deny


class PolicyEngine:
    """策略引擎。

    - read_file / write_file: 用 glob 规则匹配。
    - execute_command: 先做硬拒绝，再做人类审批。

    命令的语义审核交给 cmd_reviewer 节点。
    是否真正放行，仍由 Supervisor 的审批流程决定。
    """

    def __init__(self, *, workspace_root: Path, policy_path: Path | None = None):
        self._root = workspace_root
        self._policy_path = policy_path or (workspace_root / "data" / "policy.yaml")

        self._cached_mtime: float | None = None

        self._cfg: dict[str, Any] = _default_policy_dict()

        self._extra_roots: list[Path] = []

    @property
    def extra_roots(self) -> list[Path]:
        """Public read access for attachment trust resolution."""
        self._reload_if_needed()
        return list(self._extra_roots)

        self._read_default: SafetyLevel = SafetyLevel.auto
        self._write_default: SafetyLevel = SafetyLevel.auto
        self._restart_default: SafetyLevel = SafetyLevel.approval_required
        self._command_default: SafetyLevel = SafetyLevel.approval_required
        self._tool_default: SafetyLevel = SafetyLevel.approval_required

        self._read_rules: list[tuple[str, SafetyLevel, str]] = []
        self._write_rules: list[tuple[str, SafetyLevel, str]] = []
        self._tool_rules: list[tuple[str, SafetyLevel, str]] = []
        self._approval_command_patterns: list[re.Pattern[str]] = [re.compile(_GIT_REMOTE_PUSH_PATTERN, re.IGNORECASE)]
        self._deny_command_patterns: list[re.Pattern[str]] = []

        self._ensure_policy_file_exists()
        self._reload_if_needed(force=True)

    def _ensure_policy_file_exists(self) -> None:
        if self._policy_path.exists():
            return
        try:
            self._policy_path.parent.mkdir(parents=True, exist_ok=True)
            text = yaml.safe_dump(_default_policy_dict(), sort_keys=False, allow_unicode=True)
            self._policy_path.write_text(text, encoding="utf-8")
        except Exception:
            pass

    def _reload_if_needed(self, *, force: bool = False) -> None:
        try:
            st = self._policy_path.stat()
            mtime = float(st.st_mtime)
        except Exception:
            return

        if not force and self._cached_mtime is not None and mtime == self._cached_mtime:
            return

        data = load_yaml_dict(self._policy_path)
        if not isinstance(data, dict):
            data = _default_policy_dict()

        self._cfg = data
        self._cached_mtime = mtime
        self._compile()

    def _compile_rules(self, section: dict[str, Any]) -> tuple[SafetyLevel, list[tuple[str, SafetyLevel, str]]]:
        default_s = str(section.get("default", "deny"))
        default = _to_safety_level(default_s)

        rules: list[tuple[str, SafetyLevel, str]] = []
        raw_rules = section.get("rules")
        if isinstance(raw_rules, list):
            for r in raw_rules:
                if not isinstance(r, dict):
                    continue
                pat = str(r.get("pattern", "")).strip()
                dec = _to_safety_level(str(r.get("decision", "deny")))
                reason = str(r.get("reason", ""))
                if not pat:
                    continue
                rules.append((pat, dec, reason))
        return default, rules

    def _compile(self) -> None:
        self._extra_roots = parse_extra_roots(self._root, self._cfg.get("extra_roots"))

        read_sec = self._cfg.get("read_file")
        if isinstance(read_sec, dict):
            self._read_default, self._read_rules = self._compile_rules(read_sec)
        else:
            self._read_default, self._read_rules = SafetyLevel.auto, []

        write_sec = self._cfg.get("write_file")
        if isinstance(write_sec, dict):
            self._write_default, self._write_rules = self._compile_rules(write_sec)
        else:
            self._write_default, self._write_rules = SafetyLevel.auto, []

        cmd_sec = self._cfg.get("execute_command")
        if isinstance(cmd_sec, dict):
            self._command_default = _to_safety_level(str(cmd_sec.get("default", "approval_required")))
            approval_pats = cmd_sec.get("approval_patterns")
            deny_pats = cmd_sec.get("deny_patterns")
        else:
            self._command_default = SafetyLevel.approval_required
            approval_pats = None
            deny_pats = None

        approval_sources = [_GIT_REMOTE_PUSH_PATTERN]
        approval_sources.extend(p for p in (approval_pats if isinstance(approval_pats, list) else []) if isinstance(p, str) and p.strip())
        self._approval_command_patterns = [re.compile(p, re.IGNORECASE) for p in dict.fromkeys(approval_sources)]

        self._deny_command_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (deny_pats if isinstance(deny_pats, list) else [])
            if isinstance(p, str) and p.strip()
        ]

        restart_sec = self._cfg.get("restart")
        if isinstance(restart_sec, dict):
            self._restart_default = _to_safety_level(str(restart_sec.get("default", "approval_required")))
        else:
            self._restart_default = SafetyLevel.approval_required

        tool_sec = self._cfg.get("tools")
        if isinstance(tool_sec, dict):
            self._tool_default = _to_safety_level(str(tool_sec.get("default", "approval_required")))
            self._tool_rules = []
            for r in (tool_sec.get("rules") if isinstance(tool_sec.get("rules"), list) else []):
                if not isinstance(r, dict):
                    continue
                pat = str(r.get("name", "")).strip()
                dec = _to_safety_level(str(r.get("decision", "deny")))
                if not pat:
                    continue
                self._tool_rules.append((pat, dec, str(r.get("reason", ""))))
        else:
            self._tool_default = SafetyLevel.approval_required
            self._tool_rules = []

    def _resolve_relpath(self, path_str: str, *, workspace: Path | None = None) -> tuple[Path | None, str, str]:
        return classify_path(self._root, self._extra_roots, path_str, workspace=workspace)

    @staticmethod
    def _match_rules(rel: str, rules: list[tuple[str, SafetyLevel, str]], default: SafetyLevel) -> PolicyDecision:
        for pat, dec, reason in rules:
            if fnmatch.fnmatchcase(rel, pat):
                return PolicyDecision(dec, reason or f"matched rule: {pat}")
        return PolicyDecision(default, "default policy")

    def evaluate_read_file(self, *, path: str, workspace: Path | None = None) -> PolicyDecision:
        self._reload_if_needed()
        resolved, rel, trust_level = self._resolve_relpath(path, workspace=workspace)
        if resolved is None:
            return PolicyDecision(SafetyLevel.deny, rel, trust_level="external")
        default = self._read_default
        if trust_level == "external":
            default = SafetyLevel.approval_required
        decision = self._match_rules(rel, self._read_rules, default)
        decision.trust_level = trust_level
        return decision

    def evaluate_write_file(self, *, path: str, workspace: Path | None = None) -> PolicyDecision:
        self._reload_if_needed()
        resolved, rel, trust_level = self._resolve_relpath(path, workspace=workspace)
        if resolved is None:
            return PolicyDecision(SafetyLevel.deny, rel, trust_level="external")
        default = self._write_default
        if trust_level == "external":
            default = SafetyLevel.approval_required
        decision = self._match_rules(rel, self._write_rules, default)
        decision.trust_level = trust_level
        return decision

    def evaluate_execute_command(self, *, command: str) -> PolicyDecision:
        self._reload_if_needed()
        cmd = command.strip()
        if not cmd:
            return PolicyDecision(SafetyLevel.deny, "empty command")

        for pat in self._deny_command_patterns:
            if pat.search(cmd):
                return PolicyDecision(SafetyLevel.deny, f"command denied by pattern: {pat.pattern}")

        for pat in self._approval_command_patterns:
            if pat.search(cmd):
                return PolicyDecision(SafetyLevel.approval_required, "git push to remote requires approval")

        if self._command_default == SafetyLevel.auto:
            return PolicyDecision(SafetyLevel.auto, "command auto-allowed by default")
        if self._command_default == SafetyLevel.deny:
            return PolicyDecision(SafetyLevel.deny, "command denied by default")
        return PolicyDecision(SafetyLevel.approval_required, "command requires approval")

    def evaluate_restart(self, *, target: str) -> PolicyDecision:
        self._reload_if_needed()
        if target not in {"engine", "all"}:
            return PolicyDecision(SafetyLevel.deny, f"unknown restart target: {target}")
        if self._restart_default == SafetyLevel.auto:
            return PolicyDecision(SafetyLevel.auto, f"restart {target} auto-allowed")
        if self._restart_default == SafetyLevel.deny:
            return PolicyDecision(SafetyLevel.deny, f"restart {target} denied")
        return PolicyDecision(SafetyLevel.approval_required, f"restart {target} requires approval")

    def evaluate_tool(self, *, name: str, arguments: dict[str, Any] | None = None) -> PolicyDecision:
        """工具级审批：按工具名匹配 tools 配置段规则。"""
        self._reload_if_needed()
        name = str(name or "").strip()
        if not name:
            return PolicyDecision(SafetyLevel.deny, "empty tool name")
        for pat, dec, reason in self._tool_rules:
            if fnmatch.fnmatchcase(name, pat):
                return PolicyDecision(dec, reason or f"matched tool rule: {pat}")
        return PolicyDecision(self._tool_default, "default tool policy")

    def evaluate(self, *, op: str, parameters: dict[str, Any], workspace: Path | None = None) -> PolicyDecision:
        if op == "read_file":
            return self.evaluate_read_file(path=str(parameters.get("path", "")), workspace=workspace)
        if op == "write_file":
            return self.evaluate_write_file(path=str(parameters.get("path", "")), workspace=workspace)
        if op == "execute_command":
            return self.evaluate_execute_command(command=str(parameters.get("command", "")))
        if op == "restart":
            return self.evaluate_restart(target=str(parameters.get("target", "")))
        if op == "tool_call":
            return self.evaluate_tool(
                name=str(parameters.get("tool_name", "")),
                arguments=parameters.get("arguments") if isinstance(parameters.get("arguments"), dict) else None,
            )
        return PolicyDecision(SafetyLevel.deny, f"unknown op: {op}")
