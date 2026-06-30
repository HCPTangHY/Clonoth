"""Built-in supervisor hook handler for automatic memory extraction."""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config


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
        entry_node_id = get_str(runtime_cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip()
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

        # Why: a task that explicitly saved memory has already handled the turn.
        # How: preserve the old save_memory tool-name mutual exclusion. Purpose:
        # avoid duplicate memory entries from automatic extraction.
        tool_names = (getattr(task, "result", None) or {}).get("_tool_names") or []
        if "save_memory" in tool_names:
            log.debug("memory_extract TRACE: BLOCKED by save_memory in tool_names")
            return

        # [Fork/Merge 2026-05-17] Why: completed entry tasks run on temporary
        # branch sessions, while on_entry_task_complete passes the merged parent
        # session in ctx["session_id"]. How: prefer the hook route session and
        # fall back to the task runtime session for legacy/non-branch tasks.
        # Purpose: pending task ids and transcript extraction read durable parent
        # history rather than the temporary branch name after merge.
        session_id = str(ctx.get("session_id") or getattr(task, "session_id", "") or "").strip()
        if not session_id:
            log.debug("memory_extract: skip, empty session_id")
            return
        log.debug("memory_extract TRACE: session_id=%s (ctx=%s task=%s)", session_id, ctx.get('session_id'), getattr(task, 'session_id', '?'))
        # [2026-05-23] Why: entry-task completions remain monotonic within this
        # handler and are still useful for diagnostics. How: keep the historical
        # total task counter unchanged. Purpose: preserve observability while the
        # trigger threshold below uses the unextracted pending-task-id list.
        task_count = self._memory_extract_task_counts.get(session_id, 0) + 1
        self._memory_extract_task_counts[session_id] = task_count
        # [AutoC 2026-05-24] Why: message indexes are polluted by compact summary
        # records and no longer identify new dialogue. How: append the exact entry
        # task id that passed every gate. Purpose: later build the transcript by
        # ConversationStore.source_task_id membership.
        pending_task_ids = self._memory_extract_pending_task_ids.setdefault(session_id, [])
        pending_task_ids.append(task_id_str)
        pending_task_count = len(pending_task_ids)

        min_increment = get_int(runtime_cfg, "memory.auto_extract.min_increment", 3, min_value=1, max_value=100)
        last_extracted_task_count = self._memory_extract_last_extracted_task_counts.get(session_id, 0)
        log.debug(
            "memory_extract TRACE: task_count=%d last_extracted_task_count=%d pending_task_count=%d min_incr=%d",
            task_count,
            last_extracted_task_count,
            pending_task_count,
            min_increment,
        )
        if pending_task_count < min_increment:
            log.debug(
                "memory_extract: pending_task_count=%d < min_increment=%d (task_count=%d, last_extracted_task_count=%d)",
                pending_task_count,
                min_increment,
                task_count,
                last_extracted_task_count,
            )
            # [2026-05-23 / AutoC 2026-05-24] Why: low-volume sessions may not
            # reach the task threshold quickly. How: keep the fallback idle path,
            # but prepare its transcript from pending source_task_id values instead
            # of a message cursor. Purpose: preserve low-volume extraction without
            # reintroducing msg-index slicing.
            fallback_delay = get_int(
                runtime_cfg,
                "memory.auto_extract.idle_fallback_delay_sec",
                120,
                min_value=5,
                max_value=3600,
            )
            log.debug(
                "memory_extract TRACE: scheduling FALLBACK timer (%ds) for session %s task_count=%d pending_task_count=%d",
                fallback_delay,
                session_id,
                task_count,
                pending_task_count,
            )
            pending_extract = self._prepare_memory_extract_pending_locked(
                ctx=ctx,
                task=task,
                workspace_root=workspace_root,
                runtime_cfg=runtime_cfg,
                session_id=session_id,
                task_count=task_count,
            )
            if pending_extract is not None:
                self._schedule_memory_extract_idle_locked(
                    ctx=ctx,
                    session_id=session_id,
                    pending_extract=pending_extract,
                    delay_sec=fallback_delay,
                    timer_label="fallback idle",
                )
            return
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

        # [2026-04-26] P4b pre-injection stays removed: large memory lists polluted
        # the main session history and could exceed context budgets. The extractor
        # node prompt already contains duplicate-prevention instructions.
        idle_delay = get_int(runtime_cfg, "memory.auto_extract.idle_delay_sec", 30, min_value=5, max_value=120)
        log.debug(
            "memory_extract TRACE: scheduling NORMAL timer (%ds) for session %s task_count=%d pending_task_count=%d",
            idle_delay,
            session_id,
            task_count,
            pending_task_count,
        )
        self._schedule_memory_extract_idle_locked(
            ctx=ctx,
            session_id=session_id,
            pending_extract=pending_extract,
            delay_sec=idle_delay,
            timer_label="idle",
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
        transcript = _format_transcript_for_extract(task_msgs)
        if not transcript.strip():
            return None

        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        return {
            "session_id": session_id,
            "session_generation": int(getattr(task, "session_generation", 0) or 0),
            "transcript": transcript,
            "task_count": task_count,
            "pending_task_ids": pending_task_ids,
            "extractor_node": extractor_node,
            "kind": getattr(task, "kind", "node"),
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

    def _fire_memory_extract_idle(self, ctx: dict[str, Any], session_id: str) -> None:
        """Fire a pending automatic memory extraction after the idle window."""
        sid = str(session_id or "").strip()
        if not sid:
            return

        lock = ctx.get("acquire_lock")
        if lock is None:
            return
        # Why: threading.Timer runs outside the supervisor request/event thread.
        # How: take the injected lock before touching pending state or creating
        # tasks. Purpose: serialize timer callbacks with inbound handling and task
        # routing while keeping handler-owned dictionaries consistent.
        with lock:
            pending_extract = self._memory_extract_pending.pop(sid, None)
            self._memory_extract_timers.pop(sid, None)
            if not isinstance(pending_extract, dict):
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

            # [AutoC 2026-05-24] Why: cancelled idle windows must not mark task
            # ids as extracted. How: commit the total task count and clear the
            # pending source_task_id list only inside the timer callback after
            # pending data is popped. Purpose: cancelled idle windows still
            # contribute to future extraction without losing transcript scope.
            self._memory_extract_task_counts[sid] = task_count
            self._memory_extract_last_extracted_task_counts[sid] = task_count
            self._memory_extract_pending_task_ids[sid] = []

            create_task = ctx.get("create_task")
            if not callable(create_task):
                return
            # [AutoC 2026-05-31] Why: the extractor prompt no longer embeds a
            # static book taxonomy, so the task input must carry the current
            # memory books. How: resolve workspace_root from hook context first
            # and fall back to pending payloads, then prefix the transcript with
            # the sorted data/memory/*.yaml stems. Purpose: prefer existing books
            # while still allowing the extractor to create a new semantic book.
            workspace_value = ctx.get("workspace_root") or pending_extract.get("workspace_root")
            workspace_root = Path(workspace_value) if workspace_value is not None else None
            mem_dir = workspace_root / "data" / "memory" if workspace_root is not None else None
            book_names = sorted(
                p.stem for p in mem_dir.glob("*.yaml")
            ) if mem_dir is not None and mem_dir.exists() else []
            book_list_header = ""
            if book_names:
                book_list_header = f"当前已有的 memory book 列表：{', '.join(book_names)}\n保存时优先使用已有 book，也可以创建新 book。\n\n"

            # [2026-04-26] child_session_id isolation prevents the system task's
            # instruction from being written into the main session JSONL history.
            child_sid = f"child_{uuid.uuid4().hex[:12]}"
            create_task(
                session_id=sid,
                session_generation=session_generation,
                kind=pending_extract.get("kind") or "node",
                node_id=extractor_node,
                input_data={
                    "instruction": book_list_header + transcript,
                    "child_session_id": child_sid,
                    "_system_task": True,
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )


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
    for msg in messages:
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
        is_tool_result = role == "tool" or message_type == "tool_result"
        limit = 500 if is_tool_result else 2000
        if len(content) > limit:
            content = content[:limit] + "...<truncated>"
        if is_tool_result:
            label = f"Tool ({name})" if name else "Tool"
        elif role == "user":
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
