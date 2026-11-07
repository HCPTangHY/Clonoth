"""Built-in supervisor hook handler for automatic memory extraction."""
from __future__ import annotations

from engine.conversation_store import is_dynamic_context

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_entry_node_id, get_int, get_str, load_runtime_config


log = logging.getLogger(__name__)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "MemoryExtractHandler",
    "hook_points": [
        ("on_entry_task_complete", "on_task_complete"),
        ("on_inbound_message", "on_inbound"),
    ],
    "priority": 100,
    # Why: memory_extract writes new entries that knowledge_inject must cache-invalidate.
    # How: declare the dependency so loader ensures knowledge_inject loads first.
    # Purpose: fail clearly if knowledge_inject is missing rather than silent runtime errors.
    "requires": ["knowledge_inject"],
    # [AutoC 2026-08-30] system.memory_extractor 节点从 engine/system_nodes/ 迁移为插件声明。
    "nodes": [{"file": "nodes/system.memory_extractor.yaml"}],
}


class MemoryExtractHandler:
    """Handle idle automatic memory extraction through injected callbacks.

    Why: this handler now lives under engine.builtin and therefore cannot import
    supervisor internals. How: every supervisor operation is read from the hook
    context as a callback or plain value. Purpose: keep memory extraction behavior
    while removing the old supervisor -> handler -> supervisor dependency loop.
    """

    name = "memory_extract"

    def __init__(self) -> None:
        # [2026-05-23] Why: compacted conversations can shrink message counts,
        # while entry-task completions remain monotonic within this handler. How:
        # count every qualifying entry-task finish per session. Purpose: make the
        # auto-extract increment gate use task granularity instead of message offsets.
        self._memory_extract_task_counts: dict[str, int] = {}
        # [2026-05-23] Why: idle timers can be cancelled before extraction, but
        # those passed entry tasks should still count toward a later threshold.
        # How: keep a separate committed cursor that only moves when the timer
        # fires. Purpose: compute task increments correctly across cancelled idle windows.
        self._memory_extract_last_extracted_task_counts: dict[str, int] = {}
        # [AutoC 2026-05-24] Why: compact summaries can occupy many durable
        # message indexes, so a message cursor can select the wrong transcript
        # slice. How: keep the exact unextracted entry task ids per session and
        # later match ConversationStore.source_task_id. Purpose: extract only the
        # messages that belong to completed entry tasks that have not committed.
        self._memory_extract_pending_task_ids: dict[str, list[str]] = {}
        # Why: automatic extraction waits for user idleness. How: keep one daemon
        # Timer per session and replace it on later qualifying finishes. Purpose:
        # avoid extracting memory while the user is still sending follow-ups.
        self._memory_extract_timers: dict[str, threading.Timer] = {}
        # Why: pending task ids must commit only if the idle timer fires. How:
        # keep the prepared transcript until the timer callback commits it.
        # Purpose: cancelled idle windows can be retried by the next entry-node
        # finish without losing the associated source_task_id values.
        self._memory_extract_pending: dict[str, dict[str, Any]] = {}

    def on_task_complete(self, ctx: dict[str, Any]) -> None:
        """Schedule memory extraction after a qualifying entry task finishes."""
        task = ctx.get("task")
        if task is None:
            return
        self._maybe_trigger_memory_extract_locked(ctx=ctx, task=task)

    def on_inbound(self, ctx: dict[str, Any]) -> None:
        """Cancel pending idle extraction when a new inbound message arrives."""
        session_id = str(ctx.get("session_id") or "").strip()
        if not session_id:
            return
        lock = ctx.get("acquire_lock")
        if lock is None:
            self._cancel_memory_extract_idle_locked(session_id)
            return
        # Why: inbound cancellation touches the same handler dictionaries as the
        # timer callback. How: use the injected re-entrant supervisor lock when it
        # is available. Purpose: keep cancellation serialized without importing
        # SupervisorState or reaching into private attributes.
        with lock:
            self._cancel_memory_extract_idle_locked(session_id)

    def _maybe_trigger_memory_extract_locked(self, *, ctx: dict[str, Any], task: Any) -> None:
        """Entry-node finish gate for automatic memory extraction."""
        # Why: only successful entry-node finishes represent complete conversation
        # turns. How: keep the old action/kind/system-task gates using only task
        # values. Purpose: prevent child tasks or internal system tasks from
        # recursively triggering extraction.
        act = str((getattr(task, "result", None) or {}).get("action") or "").strip()
        if act != "finish" or not _task_kind_is_node(task):
            log.debug("memory_extract gate: blocked by action=%s kind=%s", act, getattr(task, "kind", "?"))
            return
        task_input = getattr(task, "input", {}) or {}
        if task_input.get("_system_task"):
            log.debug("memory_extract gate: blocked by _system_task")
            return
        inbound_mtype = str(task_input.get("inbound_message_type") or "").strip()
        if inbound_mtype in ("dispatch_result", "async_tool_result"):
            log.debug("memory_extract gate: blocked by inbound_message_type=%s", inbound_mtype)
            return
        # [2026-06-04] Why: fresh child tasks are short-lived and disposable (session
        # cleaned within 24h), so extracting memory from them wastes CPU. However,
        # accumulate/persistent child nodes (like Smith) maintain long-lived sessions
        # with valuable context worth extracting. How: check dispatch_context_mode
        # from task input — only skip fresh children. Purpose: enable memory
        # extraction for persistent sub-agents while still filtering throwaway tasks.
        caller_tid = getattr(task, "caller_task_id", None)
        if caller_tid:
            ctx_mode = str(task_input.get("dispatch_context_mode") or "").strip()
            if ctx_mode == "fresh" or not ctx_mode:
                log.debug("memory_extract gate: blocked by caller_task_id=%s (fresh/unknown child)", str(caller_tid)[:8])
                return
            log.debug("memory_extract gate: allowing %s child (context_mode=%s)", str(caller_tid)[:8], ctx_mode)
        # [2026-05-23 fix] Entry tasks run on temporary branch sessions (task.session_id
        # starts with 'branch_'), but they represent real user conversation turns. The
        # on_entry_task_complete hook passes the merged parent session in ctx["session_id"].
        # Only filter by task_id prefix (which is never 'branch_' for entry tasks);
        # do NOT filter by task.session_id — that would block all entry task extractions.
        task_id_str = str(getattr(task, "task_id", "") or "")
        if task_id_str.startswith("branch_"):
            log.debug("memory_extract gate: blocked by branch task_id=%s", task_id_str[:16])
            return
        log.debug("memory_extract: entered gate check for task %s node=%s", task_id_str[:8], getattr(task, 'node_id', '?'))

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            log.debug("memory_extract gate: no workspace_root")
            return
        workspace_root = Path(workspace_root)
        runtime_cfg = load_runtime_config(workspace_root)
        entry_node_id = get_entry_node_id(runtime_cfg)
        if getattr(task, "node_id", None) != entry_node_id:
            log.debug("memory_extract gate: node_id=%s != entry_node_id=%s", getattr(task, "node_id", None), entry_node_id)
            return
        if not get_bool(runtime_cfg, "memory.auto_extract.enabled", False):
            log.debug("memory_extract gate: auto_extract disabled")
            return
        log.debug(
            "memory_extract: passed gates for task %s runtime_session=%s route_session=%s",
            str(getattr(task, "task_id", "?") or "?")[:8],
            getattr(task, "session_id", "?"),
            ctx.get("session_id"),
        )

        session_id = str(ctx.get("session_id") or getattr(task, "session_id", "") or "").strip()
        if not session_id:
            log.debug("memory_extract: skip, empty session_id")
            return
        log.debug("memory_extract TRACE: session_id=%s (ctx=%s task=%s)", session_id, ctx.get('session_id'), getattr(task, 'session_id', '?'))

        # Explicit memory writes/deletes suppress extraction; read-only calls do not.
        if _memory_mutation_happened(getattr(task, "result", None)):
            log.debug("memory_extract TRACE: BLOCKED by manage_memory upsert/delete in _tool_actions")
            # If earlier unextracted turns are still pending in this session, re-arm the tail timer
            # because on_inbound_message cancelled the previous timer when this turn started.
            existing_pending = self._memory_extract_pending_task_ids.get(session_id) or []
            if existing_pending:
                fallback_delay = get_int(runtime_cfg, "memory.auto_extract.idle_fallback_delay_sec", 1800, min_value=5, max_value=7200)
                pending_extract = self._prepare_memory_extract_pending_locked(
                    ctx=ctx,
                    task=task,
                    workspace_root=workspace_root,
                    runtime_cfg=runtime_cfg,
                    session_id=session_id,
                    task_count=self._memory_extract_task_counts.get(session_id, 0),
                )
                if pending_extract is not None:
                    self._schedule_memory_extract_idle_locked(
                        ctx=ctx,
                        session_id=session_id,
                        pending_extract=pending_extract,
                        delay_sec=fallback_delay,
                        timer_label="tail idle (re-armed)",
                    )
            return

        task_count = self._memory_extract_task_counts.get(session_id, 0) + 1
        self._memory_extract_task_counts[session_id] = task_count
        pending_task_ids = self._memory_extract_pending_task_ids.setdefault(session_id, [])
        pending_task_ids.append(task_id_str)
        pending_task_count = len(pending_task_ids)

        min_increment = get_int(runtime_cfg, "memory.auto_extract.min_increment", 6, min_value=1, max_value=100)
        last_extracted_task_count = self._memory_extract_last_extracted_task_counts.get(session_id, 0)
        log.debug(
            "memory_extract TRACE: task_count=%d last_extracted_task_count=%d pending_task_count=%d min_incr=%d",
            task_count,
            last_extracted_task_count,
            pending_task_count,
            min_increment,
        )
        pending_extract = self._prepare_memory_extract_pending_locked(
            ctx=ctx,
            task=task,
            workspace_root=workspace_root,
            runtime_cfg=runtime_cfg,
            session_id=session_id,
            task_count=task_count,
        )
        if pending_extract is None:
            return

        if pending_task_count >= min_increment:
            # Batch full (>= min_increment): cancel any tail timer and dispatch immediately in background!
            self._cancel_memory_extract_idle_locked(session_id)
            log.debug(
                "memory_extract TRACE: immediate background dispatch for session %s (pending_task_count=%d >= min_increment=%d)",
                session_id,
                pending_task_count,
                min_increment,
            )
            self._dispatch_memory_extract_locked(ctx=ctx, session_id=session_id, pending_extract=pending_extract)
            return

        # Tail tasks (1 <= pending_task_count < min_increment): schedule tail idle timer (default 1800s / 30min)
        min_fallback_tasks = get_int(
            runtime_cfg,
            "memory.auto_extract.min_fallback_tasks",
            1,
            min_value=1,
            max_value=100,
        )
        if pending_task_count < min_fallback_tasks:
            return
        fallback_delay = get_int(
            runtime_cfg,
            "memory.auto_extract.idle_fallback_delay_sec",
            1800,
            min_value=5,
            max_value=7200,
        )
        log.debug(
            "memory_extract TRACE: scheduling TAIL IDLE timer (%ds) for session %s task_count=%d pending_task_count=%d",
            fallback_delay,
            session_id,
            task_count,
            pending_task_count,
        )
        self._schedule_memory_extract_idle_locked(
            ctx=ctx,
            session_id=session_id,
            pending_extract=pending_extract,
            delay_sec=fallback_delay,
            timer_label="tail idle",
        )

    def _prepare_memory_extract_pending_locked(
        self,
        *,
        ctx: dict[str, Any],
        task: Any,
        workspace_root: Path,
        runtime_cfg: dict[str, Any],
        session_id: str,
        task_count: int,
    ) -> dict[str, Any] | None:
        """Build the pending extraction payload shared by normal and fallback timers."""
        # [AutoC 2026-05-24] Why: msg-index ranges can include compact summaries
        # and miss the real new turn. How: copy the current uncommitted task-id
        # list and read ConversationStore directly. Purpose: prepare a transcript
        # from messages whose source_task_id exactly belongs to pending entry tasks.
        pending_task_ids = list(self._memory_extract_pending_task_ids.get(session_id, []))
        if not pending_task_ids:
            return None
        try:
            from engine.conversation_store import ConversationStore

            store = ConversationStore(workspace_root / "data" / "conversations")
            all_msgs = store.load(session_id)
        except Exception as exc:
            log.debug("memory_extract: failed to load ConversationStore for session %s: %s", session_id, exc)
            return None

        # [AutoC 2026-05-24] Why: the pending list is ordered for payload
        # observability, but membership checks should be exact and efficient. How:
        # convert it to a set only for filtering while preserving store order in
        # the resulting messages. Purpose: the transcript stays chronologically
        # readable and includes every message from each pending task.
        pending_task_id_set = set(pending_task_ids)
        task_msgs = [m for m in all_msgs if m.source_task_id in pending_task_id_set]
        min_messages = get_int(runtime_cfg, "memory.auto_extract.min_messages", 4, min_value=1, max_value=200)
        dialogue_msgs = [
            m for m in task_msgs
            if not is_dynamic_context(m) and getattr(m, "role", "") in ("user", "assistant")
            and getattr(m, "message_type", "") not in ("tool_result", "retry_hint", "output_rejected")
            and not (getattr(m, "meta", None) or {}).get("rejected_draft")
            and not (getattr(m, "meta", None) or {}).get("is_rejection")
            and (
                bool(getattr(m, "content", "").strip())
                if isinstance(getattr(m, "content", None), str)
                else bool(getattr(m, "content", None))
                if isinstance(getattr(m, "content", None), list)
                else False
            )
        ]
        if len(dialogue_msgs) < min_messages:
            log.debug(
                "memory_extract: dialogue_msgs=%d < min_messages=%d for session %s",
                len(dialogue_msgs), min_messages, session_id,
            )
            return None
        transcript = _format_transcript_for_extract(task_msgs)
        if not transcript.strip():
            return None

        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        # [AutoC 2026-08-28] Capture the source node_id so the dream_router
        # hook can redirect save_memory writes to the correct namespace.
        _entry_nid = get_entry_node_id(runtime_cfg)
        source_node_id = str(getattr(task, "node_id", "") or "").strip() or _entry_nid
        source_ws = ""
        # Storage scope follows the durable session, independent of transport or
        # workspace naming. A malformed registry must not leak scoped writes
        # into the node-level book through a silent fallback.
        import json
        try:
            sessions = json.loads((workspace_root / "data" / "sessions.json").read_text(encoding="utf-8"))
            session = sessions.get(session_id, {})
            workspace = session.get("workspace") or {}
            source_ws = str(workspace.get("name") or "").strip()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            log.warning("memory_extract: cannot resolve session memory scope; skipping extraction", exc_info=True)
            return None
        return {
            "session_id": session_id,
            "session_generation": int(getattr(task, "session_generation", 0) or 0),
            "transcript": transcript,
            "task_count": task_count,
            "pending_task_ids": pending_task_ids,
            "extractor_node": extractor_node,
            "kind": getattr(task, "kind", "node"),
            "_source_node_id": source_node_id,
            "_source_ws": source_ws,
        }

    def _schedule_memory_extract_idle_locked(
        self,
        *,
        ctx: dict[str, Any],
        session_id: str,
        pending_extract: dict[str, Any],
        delay_sec: int,
        timer_label: str,
    ) -> None:
        """Replace the per-session idle timer with a prepared extraction payload."""
        # Why: fallback and normal idle extraction must share one cancellation slot.
        # How: cancel any existing Timer for the session before storing the new
        # pending payload and Timer. Purpose: a later finish or inbound cannot leave
        # stale normal or fallback callbacks active for the same session.
        old_timer = self._memory_extract_timers.pop(session_id, None)
        if old_timer is not None:
            old_timer.cancel()
            log.debug("memory_extract TRACE: cancelled old timer for session %s", session_id)
        self._memory_extract_pending[session_id] = pending_extract
        timer = threading.Timer(delay_sec, self._fire_memory_extract_idle, args=[dict(ctx), session_id])
        timer.daemon = True
        self._memory_extract_timers[session_id] = timer
        timer.start()
        log.debug("memory_extract TRACE: TIMER STARTED [%s] session=%s delay=%ds", timer_label, session_id, delay_sec)

    def _cancel_memory_extract_idle_locked(self, session_id: str) -> None:
        """Cancel a pending idle memory extraction for one session."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        timer = self._memory_extract_timers.pop(sid, None)
        if timer is not None:
            timer.cancel()
        pending = self._memory_extract_pending.pop(sid, None)
        if timer is not None or pending is not None:
            log.debug("memory_extract TRACE: INBOUND CANCEL timer for session %s (had_timer=%s had_pending=%s)", sid, timer is not None, pending is not None)

    def _dispatch_memory_extract_locked(
        self,
        *,
        ctx: dict[str, Any],
        session_id: str,
        pending_extract: dict[str, Any],
    ) -> None:
        """Commit pending task cursor and create the background system.memory_extractor task."""
        sid = str(session_id or "").strip()
        if not sid or not isinstance(pending_extract, dict):
            return
        transcript = str(pending_extract.get("transcript") or "")
        if not transcript.strip():
            return
        task_count = _safe_int(pending_extract.get("task_count"), self._memory_extract_task_counts.get(sid, 0))
        session_generation = _safe_int(pending_extract.get("session_generation"), 0)
        if session_generation <= 0:
            current_generation = ctx.get("current_session_generation")
            session_generation = int(current_generation(sid) or 1) if callable(current_generation) else 1
        extractor_node = str(pending_extract.get("extractor_node") or "system.memory_extractor").strip()
        extractor_node = extractor_node or "system.memory_extractor"

        self._memory_extract_task_counts[sid] = task_count
        self._memory_extract_last_extracted_task_counts[sid] = task_count
        self._memory_extract_pending_task_ids[sid] = []

        create_task = ctx.get("create_task")
        if not callable(create_task):
            return
        _source_nid = str(pending_extract.get("_source_node_id") or "").strip()
        workspace_value = ctx.get("workspace_root") or pending_extract.get("workspace_root")
        workspace_root = Path(workspace_value) if workspace_value is not None else None
        mem_root = workspace_root / "data" / "memory" if workspace_root is not None else None
        book_stems: set[str] = set()
        if mem_root is not None and mem_root.exists():
            ns_dir = (mem_root / _source_nid) if _source_nid else mem_root
            for scan_dir in (ns_dir, mem_root):
                if scan_dir.is_dir():
                    book_stems.update(p.stem for p in scan_dir.glob("*.yaml"))
        book_names = sorted(book_stems)
        book_list_header = ""
        if book_names:
            book_list_header = f"当前已有的 memory book 列表：{', '.join(book_names)}\n保存时优先使用已有 book，也可以创建新 book。\n\n"

        child_sid = f"child_{uuid.uuid4().hex[:12]}"
        _extra_input: dict[str, Any] = {}
        if _source_nid:
            _extra_input["_memory_route_ns"] = _source_nid
        source_ws = str(pending_extract.get("_source_ws") or "").strip()
        if source_ws:
            _extra_input["_memory_route_ws"] = source_ws
        create_task(
            session_id=sid,
            session_generation=session_generation,
            kind=pending_extract.get("kind") or "node",
            node_id=extractor_node,
            input_data={
                "instruction": book_list_header + transcript,
                "child_session_id": child_sid,
                "_system_task": True,
                **_extra_input,
            },
            continuation={},
            source_inbound_seq=None,
            caller_task_id=None,
        )

    def _fire_memory_extract_idle(self, ctx: dict[str, Any], session_id: str) -> None:
        """Fire a pending tail automatic memory extraction after the idle window."""
        sid = str(session_id or "").strip()
        if not sid:
            return

        lock = ctx.get("acquire_lock")
        if lock is None:
            return
        with lock:
            pending_extract = self._memory_extract_pending.pop(sid, None)
            self._memory_extract_timers.pop(sid, None)
            if not isinstance(pending_extract, dict):
                return
            self._dispatch_memory_extract_locked(
                ctx=ctx,
                session_id=sid,
                pending_extract=pending_extract,
            )


def _memory_mutation_happened(task_result: Any) -> bool:
    """Check the recorded actions for explicit manage_memory writes/deletes."""
    if not isinstance(task_result, dict):
        return False
    actions = task_result.get("_tool_actions")
    if not isinstance(actions, list):
        return False
    for item in actions:
        if not isinstance(item, dict) or item.get("name") != "manage_memory":
            continue
        if str(item.get("action") or "").strip().lower() in ("upsert", "delete"):
            return True
    return False


def _task_kind_is_node(task: Any) -> bool:
    """Return whether a task is a node task without depending on task enums."""
    # Why: importing the supervisor task enum would recreate the cycle this move is
    # meant to remove. How: compare the enum value when present, otherwise compare
    # the plain string. Purpose: keep the gate compatible with real tasks and
    # lightweight tests.
    kind = getattr(task, "kind", "")
    value = getattr(kind, "value", kind)
    return str(value) == "node"


def _format_transcript_for_extract(messages: list[Any], *, max_chars: int = 12000) -> str:
    """Format task-scoped messages into readable transcript text."""
    # [AutoC 2026-05-24] Why: memory extraction now receives ConversationStore
    # Message objects selected by source_task_id, and the old [role]\ncontent
    # blocks are harder for the extractor to read. How: format each message as a
    # natural role label such as "User: ..." or "Assistant: ...". Purpose: give
    # the memory extractor only the precise pending-task dialogue in a stable form.
    parts: list[str] = []
    total = 0
    for msg in reversed(messages):
        if is_dynamic_context(msg):
            continue
        if isinstance(msg, dict):
            role = str(msg.get("role") or "")
            content: Any = msg.get("content", "")
            message_type = str(msg.get("message_type") or msg.get("type") or "")
            name = str(msg.get("name") or "")
        else:
            role = str(getattr(msg, "role", "") or "")
            content = getattr(msg, "content", "")
            message_type = str(getattr(msg, "message_type", "") or "")
            name = str(getattr(msg, "name", "") or "")
        if role == "system":
            continue
        if isinstance(content, list):
            # Why: older session message dictionaries can contain multimodal
            # content lists. How: preserve only text parts before formatting.
            # Purpose: avoid leaking raw attachment dictionaries to the extractor.
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
            content = "\n".join(texts)
        if not isinstance(content, str):
            content = str(content)
        if not content.strip():
            continue
        _m_meta_ext = (msg.get("meta") or msg.get("_meta") or {}) if isinstance(msg, dict) else (getattr(msg, "meta", None) or {})
        if message_type in ("retry_hint", "output_rejected") or (isinstance(_m_meta_ext, dict) and (_m_meta_ext.get("rejected_draft") or _m_meta_ext.get("is_rejection"))):
            continue
        is_tool_result = role == "tool" or message_type == "tool_result"
        if is_tool_result:
            # Skip raw tool outputs (read_file, execute_command, etc.) so memory
            # extractor focuses on user preferences/instructions and assistant replies.
            continue
        limit = 2000
        if len(content) > limit:
            content = content[:limit] + "...<truncated>"
        if role == "user":
            label = "User"
        elif role == "assistant":
            label = "Assistant"
        else:
            label = role.capitalize() if role else "Message"
        line = f"{label}: {content}"
        total += len(line)
        if total > max_chars:
            break
        parts.append(line)
    parts.reverse()
    return "\n\n".join(parts)


def _safe_int(value: Any, default: int) -> int:
    """Convert a value to int with a small fallback."""
    # Why: hook contexts can be supplied by tests or future registries. How: guard
    # integer conversion at the boundary. Purpose: keep timer callbacks best-effort
    # and avoid losing the supervisor thread to malformed context data.
    try:
        return int(value)
    except Exception:
        return default
