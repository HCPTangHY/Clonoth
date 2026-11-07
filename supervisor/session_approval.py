"""Platform-neutral rules for requests already requiring policy approval."""
from __future__ import annotations

import fnmatch
from typing import Any


ASK_RULES = {"version": 1, "default_action": "ask", "rules": []}


def validate_approval_rules(value: Any) -> dict[str, Any]:
    """Validate the complete document; never discard an invalid restrictive rule."""
    if not isinstance(value, dict) or set(value) != {"version", "default_action", "rules"}:
        raise ValueError("approval_rules requires version, default_action and rules")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported approval_rules version")
    if value["default_action"] not in ("ask", "allow") or not isinstance(value["rules"], list):
        raise ValueError("invalid approval_rules default_action or rules")
    clean = []
    for rule in value["rules"]:
        if not isinstance(rule, dict) or set(rule) - {"tool", "operation", "trust_levels", "action"}:
            raise ValueError("invalid approval rule fields")
        if rule.get("action") not in ("ask", "allow"):
            raise ValueError("approval rule action must be ask or allow")
        for key in ("tool", "operation"):
            if key in rule and (not isinstance(rule[key], str) or not rule[key].strip()):
                raise ValueError(f"approval rule {key} must be a nonempty string")
        if "trust_levels" in rule:
            levels = rule["trust_levels"]
            if not isinstance(levels, list) or not levels or any(
                level not in ("", "workspace", "trusted", "external") for level in levels
            ):
                raise ValueError("invalid approval rule trust_levels")
        clean.append({key: list(val) if isinstance(val, list) else val for key, val in rule.items()})
    return {"version": 1, "default_action": value["default_action"], "rules": clean}


def stored_approval_rules(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        return validate_approval_rules(value)
    except ValueError:
        # Invalid stored grants cannot inherit a more permissive parent's rules.
        return {**ASK_RULES, "rules": []}


def approval_action(rules: dict[str, Any], *, tool: str, operation: str, trust_level: str) -> str:
    for rule in rules["rules"]:
        if "tool" in rule and not fnmatch.fnmatchcase(tool, rule["tool"]):
            continue
        if "operation" in rule and operation != rule["operation"]:
            continue
        if "trust_levels" in rule and trust_level not in rule["trust_levels"]:
            continue
        return rule["action"]
    return rules["default_action"]
