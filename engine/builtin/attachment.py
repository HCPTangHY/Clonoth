from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "AttachmentCollector",
    "hook_points": [
        ("after_tool_call", "handle"),
    ],
    "priority": 0,
}


class AttachmentCollector:
    """Collect attachments produced by real tool calls."""

    name = "attachment_collector"
    priority = 0

    @staticmethod
    def _normalize_attachment(item: Any, *, workspace_root: Path | None, session_id: str) -> dict[str, Any] | None:
        """Normalize tool attachment values to web-servable attachment dicts.

        Tools may still return plain workspace paths such as data/temp/a.png.
        Those paths are valid for engine-side LLM input, but the web file endpoint
        only serves data/attachments/. Copy them there once and persist the copied
        path so refreshed history can render the same image card.
        """
        raw: dict[str, Any]
        if isinstance(item, dict):
            raw = dict(item)
            path = str(raw.get("path") or raw.get("url") or "").replace("file://", "").lstrip("/").strip()
        elif isinstance(item, str):
            path = item.replace("file://", "").lstrip("/").strip()
            raw = {"path": path}
        else:
            return None

        if not path:
            return raw if isinstance(item, dict) else None

        normalized = path.replace("\\", "/")
        if normalized.startswith("data/attachments/"):
            raw["path"] = normalized
        elif workspace_root is not None:
            source = Path(normalized)
            if not source.is_absolute():
                source = workspace_root / source
            try:
                source.resolve().relative_to(workspace_root.resolve())
            except ValueError:
                return None
            if source.is_file():
                try:
                    from engine.attachments import save_attachment
                    copied = save_attachment(
                        workspace_root,
                        session_id or "tool",
                        source.read_bytes(),
                        filename=source.name,
                        mime_type=mimetypes.guess_type(str(source))[0] or "",
                    )
                    raw.update(copied)
                except Exception:
                    raw["path"] = normalized
            else:
                raw["path"] = normalized
        else:
            raw["path"] = normalized

        path_for_name = str(raw.get("path") or normalized)
        raw.setdefault("name", Path(path_for_name).name or "附件")
        mime = str(raw.get("mime_type") or mimetypes.guess_type(path_for_name)[0] or "")
        if mime:
            raw["mime_type"] = mime
        raw.setdefault("type", "image" if mime.startswith("image/") else "file")
        return raw

    async def handle(self, ctx: Any) -> Any | None:
        """Preserve legacy attachment collection after a tool result.

        Why: real tools can return attachments that final pseudo tools later
        select or expose. How: read tool_result from ctx.extra, extend the local
        per-batch attachment list and the loop-level collected attachment lists.
        Purpose: move the after-tool side effect out of ai_step.py without
        changing final attachment behavior.
        """
        tool_result = ctx.extra.get("tool_result")
        if not isinstance(tool_result, dict):
            return None

        # [AutoC 2026-05-31] Why: generated media tools now place structured
        # fields under data, including data.attachments, while older tools still
        # expose top-level attachments. How: prefer data.attachments and fall back
        # to the legacy top-level list. Purpose: keep final attachment delivery
        # working during the ok/data/error response migration.
        data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
        nested_attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
        legacy_attachments = tool_result.get("attachments") if isinstance(tool_result.get("attachments"), list) else []
        raw_attachments = list(nested_attachments or legacy_attachments)
        if not raw_attachments:
            return None

        ls = ctx.extra.get("loop_state")
        workspace_root = getattr(getattr(ls, "rctx", None), "workspace_root", None) if ls is not None else None
        session_id = str(getattr(getattr(ls, "rctx", None), "session_id", "") or "") if ls is not None else ""
        attachments = [
            normalized for item in raw_attachments
            if (normalized := self._normalize_attachment(item, workspace_root=workspace_root, session_id=session_id)) is not None
        ]
        if not attachments:
            return None

        data["attachments"] = attachments
        tool_result["attachments"] = attachments

        local_attachments = ctx.extra.get("tool_attachments")
        if isinstance(local_attachments, list):
            local_attachments.extend(attachments)

        if ls is not None:
            ls.collected_attachments.extend(attachments)
            ls.tool_produced_attachments.extend(attachments)

        return hook_result(modified=True)
