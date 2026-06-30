"""Built-in supervisor hook handler for scheduled memory dream tasks."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_bool, get_str, load_runtime_config
from engine.builtin.memory_extract import _format_transcript_for_extract


log = logging.getLogger(__name__)


# [AutoC 2026-05-31] Why: Dream now runs as a bounded preprocessing
# pipeline under the scheduler lock. How: keep all limits near the handler so
# session scanning, transcript formatting, and pending polling remain predictable.
# Purpose: avoid reintroducing full memory scans or large conversation tails.
_MAX_DREAM_SESSIONS = 5
_DREAM_TRANSCRIPT_MAX_CHARS = 12000
_DREAM_PENDING_TIMEOUT_MINUTES = 15
_TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "missing"}
# [AutoC 2026-06-01] Why: the final Dream node no longer has shell
# access to update scheduler state itself. How: centralize the lock note
# beside the polling constants. Purpose: keep the generated .dream-lock
# format stable and avoid scattering literal strings across the handler.
_DREAM_LOCK_NOTE = "Dream cycle completed via automated pipeline"


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "DreamHandler",
    "hook_points": [
        ("on_schedule_tick", "on_tick"),
    ],
    "priority": 100,
    # Why: dream reorganizes memory entries that knowledge_inject caches.
    # How: declare the dependency so loader ensures knowledge_inject loads first.
    # Purpose: fail clearly if knowledge_inject is missing.
    "requires": ["knowledge_inject"],
}


# Why: DreamHandler cannot depend on the scheduler module for cron matching.
# How: keep a local copy of the small 5-field matcher. Purpose: move dream logic
# into engine.builtin without creating an engine/supervisor import cycle.
def _match_field(field: str, value: int, max_val: int) -> bool:
    """Return whether one cron field matches the provided integer value."""
    field = field.strip()
    if field == "*":
        return True

    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False

    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                if int(lo) <= value <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue

    return False


def _cron_match(expr: str, dt: datetime) -> bool:
    """Return whether a 5-field cron expression matches a datetime."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, dt.minute, 59)
        and _match_field(hour, dt.hour, 23)
        and _match_field(day, dt.day, 31)
        and _match_field(month, dt.month, 12)
        and _match_field(weekday, dt.weekday(), 6)
    )


class DreamHandler:
    """Handle scheduled dream creation through injected supervisor callbacks.

    Why: dream was a supervisor-side handler that imported SupervisorState. How:
    read workspace, session counts, and task creation from ctx callbacks instead.
    Purpose: keep the schedule gate while allowing all built-ins to live under
    engine.builtin without supervisor imports.
    """

    name = "dream"

    def __init__(self) -> None:
        # Why: duplicate suppression belongs to the dream feature. How: keep the
        # last fired minute on the handler. Purpose: remove dream-specific fields
        # from SchedulerThread while preserving behavior.
        self._last_dream_fired: str = ""
        # [AutoC 2026-06-01] Why: Dream now waits for memory_extractor
        # tasks and then for the final organizer task. How: store the
        # extractor ids, topology payload, and final dream_task_id in handler
        # memory only. Purpose: update .dream-lock after success without
        # reintroducing execute_command access to the Dream node.
        self._dream_pending: dict[str, Any] | None = None

    def on_tick(self, ctx: dict[str, Any]) -> None:
        """Advance the scheduled Dream preprocessing state machine."""
        if str(ctx.get("schedule_type") or "").strip() != "dream":
            return

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            return
        workspace_root = Path(workspace_root)
        now_value = ctx.get("now")
        now = now_value if isinstance(now_value, datetime) else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_key = str(ctx.get("now_key") or now.strftime("%Y-%m-%d %H:%M"))

        # [AutoC 2026-06-01] Why: pending completion can happen after the cron
        # minute has passed, and pending now covers both extractor tasks and the
        # final Dream task. How: poll the pipeline before cron checks. Purpose:
        # finish a Dream run and update .dream-lock as soon as the task is ready.
        if self._dream_pending is not None:
            self._poll_and_create_dream(ctx=ctx, workspace_root=workspace_root, now=now)
            return

        runtime_cfg = load_runtime_config(workspace_root)
        if not get_bool(runtime_cfg, "memory.dream.enabled", False):
            return

        cron_expr = get_str(runtime_cfg, "memory.dream.cron", "0 3 * * *").strip()
        if not cron_expr:
            return

        if self._last_dream_fired == now_key:
            return

        if not _cron_match(cron_expr, now):
            return

        self._last_dream_fired = now_key
        self._start_dream_run(ctx=ctx, workspace_root=workspace_root, now=now, now_key=now_key)

    def _start_dream_run(
        self,
        *,
        ctx: dict[str, Any],
        workspace_root: Path,
        now: datetime,
        now_key: str,
    ) -> None:
        """Create extractor tasks and compute keyword topology for one Dream run."""
        create_task = ctx.get("create_task")
        if not callable(create_task):
            return

        session_ids = self._recent_active_session_ids(workspace_root)
        topology_json = self._build_keyword_topology_json(workspace_root)
        # [AutoC 2026-05-31] Why: Dream extractor prompts need the same dynamic
        # book guidance as automatic extraction. How: compute the book list once
        # per Dream run from data/memory/*.yaml and pass it to each instruction
        # builder. Purpose: avoid stale hard-coded book categories.
        book_list = self._build_book_list(workspace_root)
        extractor_task_ids: list[str] = []
        extractor_node = "system.memory_extractor"
        generation_cb = ctx.get("current_session_generation")

        for sid in session_ids:
            transcript = self._session_transcript(workspace_root=workspace_root, ctx=ctx, session_id=sid)
            if not transcript.strip():
                continue
            try:
                session_generation = int(generation_cb(sid) or 1) if callable(generation_cb) else 1
            except Exception:
                session_generation = 1
            child_sid = f"child_{uuid.uuid4().hex[:12]}"
            instruction = self._build_extractor_instruction(transcript, book_list=book_list)
            try:
                # [AutoC 2026-05-31] Why: Dream preprocessing must reuse the
                # existing memory_extractor node without changing its system
                # prompt. How: pass stricter one-shot JSON instructions through
                # the task input and isolate the run in a child session. Purpose:
                # extract signals without directly writing memory at this stage.
                task = create_task(
                    session_id=sid,
                    session_generation=max(1, session_generation),
                    kind="node",
                    node_id=extractor_node,
                    input_data={
                        "instruction": instruction,
                        "child_session_id": child_sid,
                        "_system_task": True,
                    },
                    continuation={},
                    source_inbound_seq=None,
                    caller_task_id=None,
                )
            except Exception as exc:
                log.warning("[scheduler] dream extractor task creation failed for session=%s: %s", sid, exc)
                continue
            task_id = str(getattr(task, "task_id", "") or "").strip()
            if task_id:
                extractor_task_ids.append(task_id)

        # [AutoC 2026-05-31] Why: restart persistence is explicitly unnecessary
        # for this workflow. How: keep only the pending ids and topology JSON in
        # process memory. Purpose: orphaned extractor results can be ignored and
        # the next cron run can rebuild the same inputs.
        self._dream_pending = {
            "now_key": now_key,
            "extractor_task_ids": extractor_task_ids,
            "topology_json": topology_json,
            "session_ids": session_ids,
        }
        log.info(
            "[scheduler] dream preprocessing started sessions=%d extractors=%d time=%s",
            len(session_ids),
            len(extractor_task_ids),
            now.strftime("%Y-%m-%d %H:%M UTC"),
        )

    def _poll_and_create_dream(
        self,
        *,
        ctx: dict[str, Any],
        workspace_root: Path,
        now: datetime,
    ) -> None:
        """Poll Dream pipeline snapshots and advance the run."""
        pending = self._dream_pending
        if pending is None:
            return

        task_snapshots = ctx.get("task_snapshots")
        if not callable(task_snapshots):
            log.warning("[scheduler] dream pipeline cannot poll tasks: missing task_snapshots callback")
            self._dream_pending = None
            return

        dream_task_id = str(pending.get("dream_task_id") or "").strip()
        if dream_task_id:
            # [AutoC 2026-06-01] Why: after the final Dream task is created,
            # extractor polling is already complete and only the organizer result
            # matters. How: branch on dream_task_id and poll that single task.
            # Purpose: write .dream-lock only after the actual Dream cycle has
            # completed successfully.
            self._poll_final_dream_task(
                task_snapshots=task_snapshots,
                workspace_root=workspace_root,
                now=now,
                pending=pending,
                dream_task_id=dream_task_id,
            )
            return

        if self._pending_expired(pending, now=now):
            log.warning("[scheduler] dream preprocessing expired from %s; dropping pending run", pending.get("now_key"))
            self._dream_pending = None
            return

        task_ids = [str(tid) for tid in pending.get("extractor_task_ids", []) if str(tid).strip()]
        snapshots = task_snapshots(task_ids)
        if not isinstance(snapshots, dict):
            return
        if any(str(snapshots.get(tid, {}).get("status") or "") not in _TERMINAL_TASK_STATUSES for tid in task_ids):
            return

        signals: list[dict[str, Any]] = []
        for tid in task_ids:
            snapshot = snapshots.get(tid, {})
            if str(snapshot.get("status") or "") != "completed":
                continue
            text = str(snapshot.get("result_text") or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                log.warning("[scheduler] dream extractor result is not JSON array task=%s error=%s", tid[:8], exc)
                continue
            if not isinstance(parsed, list):
                log.warning("[scheduler] dream extractor result ignored task=%s type=%s", tid[:8], type(parsed).__name__)
                continue
            for item in parsed:
                if isinstance(item, dict):
                    signals.append(item)

        runtime_cfg = load_runtime_config(workspace_root)
        # [AutoC 2026-05-31] Load hit_cache and skill list for Prune/Promote phases
        hit_cache_json = self._load_hit_cache_json(workspace_root)
        skill_list = self._load_skill_list(workspace_root)
        # [AutoC 2026-05-31] Why: the final organizer also needs to know the
        # current book landscape. How: rebuild the list at final task creation
        # time so files created during preprocessing are visible. Purpose: guide
        # save/delete decisions with live book names.
        book_list = self._build_book_list(workspace_root)
        instruction = self._build_dream_instruction(
            run_id=str(uuid.uuid4()),
            now=now,
            signals=signals,
            topology_json=str(pending.get("topology_json") or "{}"),
            hit_cache_json=hit_cache_json,
            skill_list=skill_list,
            book_list=book_list,
        )
        dream_task_id = self._create_final_dream_task(
            ctx=ctx,
            runtime_cfg=runtime_cfg,
            now=now,
            instruction=instruction,
        )
        if dream_task_id:
            # [AutoC 2026-06-01] Why: clearing pending here loses the only link
            # between this scheduler run and the final Dream task. How: retain the
            # pending payload and add the final task id plus creation time. Purpose:
            # a later tick can detect successful completion and write .dream-lock.
            pending["dream_task_id"] = dream_task_id
            pending["dream_created_at"] = now.astimezone().isoformat(timespec="seconds")
            log.info(
                "[scheduler] dream task created after preprocessing extractors=%d signals=%d task=%s",
                len(task_ids),
                len(signals),
                dream_task_id[:8],
            )

    def _create_final_dream_task(
        self,
        *,
        ctx: dict[str, Any],
        runtime_cfg: dict[str, Any],
        now: datetime,
        instruction: str,
    ) -> str:
        """Create the final system.dream task and return its task id."""
        create_task = ctx.get("create_task")
        if not callable(create_task):
            return ""
        node_id = get_str(runtime_cfg, "memory.dream.node_id", "system.dream").strip()
        conv_key = get_str(runtime_cfg, "memory.dream.conversation_key", "system:dream").strip()
        channel = conv_key.split(":", 1)[0] if ":" in conv_key else "system"
        msg_id = f"dream:{uuid.uuid4()}"
        try:
            # [AutoC 2026-06-01] Why: the scheduler must later know which final
            # Dream task finished. How: keep using the generic create_task hook but
            # capture the returned Task object's task_id. Purpose: make lock-file
            # updates depend on actual task completion rather than task creation.
            task = create_task(
                channel=channel,
                conversation_key=conv_key,
                kind="node",
                node_id=node_id,
                input_data={
                    "instruction": instruction,
                    "context_ref": "",
                    "resume_data": {},
                    "use_context": False,
                    "_system_task": True,
                    "task_context": {
                        "conversation_key": conv_key,
                        "channel": channel,
                        "message_id": msg_id,
                        "entry_node_id": node_id,
                        "is_system_task": True,
                        "use_context": False,
                    },
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
            task_id = str(getattr(task, "task_id", "") or "").strip()
            if not task_id:
                log.warning("[scheduler] dream inject returned task without task_id")
                return ""
            return task_id
        except Exception as exc:
            log.warning("[scheduler] dream inject failed: %s", exc)
            return ""

    def _poll_final_dream_task(
        self,
        *,
        task_snapshots: Any,
        workspace_root: Path,
        now: datetime,
        pending: dict[str, Any],
        dream_task_id: str,
    ) -> None:
        """Poll the final Dream task and write the lock after success."""
        snapshots = task_snapshots([dream_task_id])
        if not isinstance(snapshots, dict):
            return
        snapshot = snapshots.get(dream_task_id, {})
        status = str(snapshot.get("status") or "").strip()
        if status not in _TERMINAL_TASK_STATUSES:
            return

        if status == "completed":
            if not self._write_dream_lock(workspace_root=workspace_root, completed_at=now):
                return
            log.info(
                "[scheduler] dream task completed; lock updated task=%s created_at=%s",
                dream_task_id[:8],
                pending.get("dream_created_at"),
            )
        else:
            # [AutoC 2026-06-01] Why: failed, cancelled, or missing final Dream
            # tasks must not mark the cycle as successful. How: clear the pending
            # run without writing .dream-lock. Purpose: allow a future cron fire to
            # retry while preserving the lock as the last known successful run.
            log.warning("[scheduler] dream task ended without lock update task=%s status=%s", dream_task_id[:8], status)
        self._dream_pending = None

    def _write_dream_lock(self, *, workspace_root: Path, completed_at: datetime) -> bool:
        """Write data/memory/.dream-lock for a successful Dream cycle."""
        try:
            # [AutoC 2026-06-01] Why: the final Dream prompt cannot use
            # execute_command, but existing scheduling state expects this lock file.
            # How: write a small JSON document from the supervisor hook after the
            # final task reaches completed. Purpose: keep Dream completion durable
            # without giving the Dream node shell access.
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            mem_dir = workspace_root / "data" / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_run": completed_at.astimezone().isoformat(timespec="seconds"),
                "note": _DREAM_LOCK_NOTE,
            }
            (mem_dir / ".dream-lock").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            log.warning("[scheduler] dream lock update failed: %s", exc)
            return False

    def _recent_active_session_ids(self, workspace_root: Path) -> list[str]:
        """Return up to five recent user-facing sessions by updated activity."""
        conversations_dir = workspace_root / "data" / "conversations"
        if not conversations_dir.exists():
            return []

        allowed_sessions = self._active_session_ids_from_registry(workspace_root)
        registry_updates = self._active_session_updates_from_registry(workspace_root)
        ranked: list[tuple[float, str]] = []
        for path in conversations_dir.glob("*.jsonl"):
            sid = path.stem.strip()
            if not sid or sid.startswith("child_") or sid.startswith("branch_"):
                continue
            if allowed_sessions is not None and sid not in allowed_sessions:
                continue
            try:
                file_mtime = path.stat().st_mtime
            except OSError:
                continue
            # [AutoC 2026-05-31] Why: the Dream design asks for active sessions
            # ordered by updated_at, but older sessions.json rows may only have a
            # created_at field. How: prefer explicit updated_at/last_active_at
            # values when present, and fall back to the conversation JSONL mtime.
            # Purpose: honor newer registry metadata without losing compatibility
            # with existing deployments.
            rank_time = registry_updates.get(sid, file_mtime) if registry_updates is not None else file_mtime
            ranked.append((rank_time, sid))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [sid for _mtime, sid in ranked[:_MAX_DREAM_SESSIONS]]

    def _active_session_ids_from_registry(self, workspace_root: Path) -> set[str] | None:
        """Read sessions.json and return active non-child session ids when available."""
        path = workspace_root / "data" / "sessions.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        active: set[str] = set()
        for sid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("reset") or entry.get("is_child"):
                continue
            conv_key = str(entry.get("conversation_key") or "")
            channel = str(entry.get("channel") or "")
            # [AutoC 2026-05-31] Why: Dream should preprocess live user-facing
            # conversations, not its own internal system sessions. How: skip
            # system/internal registry rows before ranking JSONL mtimes. Purpose:
            # prevent recursive Dream self-analysis.
            if conv_key.startswith("system:") or channel == "internal":
                continue
            active.add(str(entry.get("session_id") or sid))
        return active

    def _active_session_updates_from_registry(self, workspace_root: Path) -> dict[str, float] | None:
        """Read explicit activity timestamps from sessions.json when available."""
        path = workspace_root / "data" / "sessions.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        updates: dict[str, float] = {}
        for sid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("reset") or entry.get("is_child"):
                continue
            raw_updated = str(entry.get("updated_at") or entry.get("last_active_at") or "").strip()
            if not raw_updated:
                continue
            try:
                updated_at = datetime.fromisoformat(raw_updated)
            except Exception:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            updates[str(entry.get("session_id") or sid)] = updated_at.timestamp()
        return updates

    def _session_transcript(self, *, workspace_root: Path, ctx: dict[str, Any], session_id: str) -> str:
        """Format one session transcript with the memory extractor formatter."""
        try:
            messages = self._load_recent_conversation_messages(workspace_root=workspace_root, session_id=session_id)
            transcript = _format_transcript_for_extract(messages, max_chars=_DREAM_TRANSCRIPT_MAX_CHARS)
            if transcript.strip():
                return transcript
        except Exception as exc:
            log.debug("[scheduler] dream conversation tail read failed session=%s error=%s", session_id, exc)

        session_messages = ctx.get("session_messages")
        if not callable(session_messages):
            return ""
        try:
            messages = session_messages(session_id, 200)
        except Exception:
            return ""
        return _format_transcript_for_extract(messages, max_chars=_DREAM_TRANSCRIPT_MAX_CHARS)

    def _load_recent_conversation_messages(self, *, workspace_root: Path, session_id: str) -> list[dict[str, Any]]:
        """Read a bounded recent JSONL window for one conversation session."""
        path = workspace_root / "data" / "conversations" / f"{session_id}.jsonl"
        if not path.exists():
            return []
        # [AutoC 2026-05-31] Why: Dream preprocessing runs while the scheduler
        # holds the supervisor lock, so loading an entire large ConversationStore
        # file would increase lock hold time. How: read only a bounded tail chunk,
        # parse valid JSONL rows, then keep the most recent rows that fit the
        # transcript budget. Purpose: provide the extractor useful recent context
        # without reviving the old full-tail scan behavior.
        max_bytes = max(_DREAM_TRANSCRIPT_MAX_CHARS * 8, 65536)
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read().decode("utf-8", errors="ignore")
        if start > 0 and "\n" in raw:
            raw = raw.split("\n", 1)[1]

        parsed: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                parsed.append(item)

        recent_reversed: list[dict[str, Any]] = []
        total = 0
        for item in reversed(parsed):
            role = str(item.get("role") or "")
            if role == "system":
                continue
            content = item.get("content", "")
            if isinstance(content, list):
                content_len = sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
            else:
                content_len = len(str(content or ""))
            total += content_len + len(role) + 16
            if recent_reversed and total > _DREAM_TRANSCRIPT_MAX_CHARS:
                break
            recent_reversed.append(item)
        return list(reversed(recent_reversed))

    def _build_extractor_instruction(self, transcript: str, book_list: str = "") -> str:
        """Build the one-shot instruction that turns memory_extractor into a signal emitter."""
        # [AutoC 2026-05-31] Why: preprocessing emits candidate memory records
        # with book names but must not rely on static categories. How: include the
        # dynamic book list before the transcript. Purpose: make later Dream
        # organization prefer existing books without blocking new ones.
        return f"""本次调用来自 Dream 预处理流水线。
以下是当前 memory book 列表：
<book_list>
{book_list}
</book_list>
保存时优先使用已有 book，也可以创建新 book。

禁止调用 save_memory、delete_memory、list_memories。
只分析下方 transcript。
最终必须调用 finish，finish 的 text 必须是纯 JSON 数组：
[{{"id": "...", "book": "...", "content": "...", "keywords": ["..."]}}]
无可保存信息时返回 []。

--- 以下是对话记录 ---
{transcript}"""

    def _build_book_list(self, workspace_root: Path) -> str:
        """Return current memory book names from data/memory YAML files."""
        # [AutoC 2026-05-31] Why: memory books are deployment data and can change
        # without code changes. How: scan data/memory/*.yaml and return sorted
        # stems, with an explicit empty-state marker. Purpose: centralize dynamic
        # book-list formatting for Dream instructions.
        mem_dir = workspace_root / "data" / "memory"
        if not mem_dir.exists():
            return "(no books found)"
        books = sorted(p.stem for p in mem_dir.glob("*.yaml"))
        return ", ".join(books) if books else "(no books found)"

    def _build_keyword_topology_json(self, workspace_root: Path) -> str:
        """Build Jaccard keyword clusters from existing memory books."""
        entries = self._load_memory_topology_entries(workspace_root)
        clusterable = [entry for entry in entries if entry["keyword_set"]]
        parent = list(range(len(clusterable)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        keyword_index: dict[str, list[int]] = {}
        for idx, entry in enumerate(clusterable):
            for keyword in entry["keyword_set"]:
                keyword_index.setdefault(keyword, []).append(idx)

        seen_pairs: set[tuple[int, int]] = set()
        for bucket in keyword_index.values():
            for pos, left in enumerate(bucket):
                left_keywords = clusterable[left]["keyword_set"]
                for right in bucket[pos + 1:]:
                    pair = (left, right) if left < right else (right, left)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    right_keywords = clusterable[right]["keyword_set"]
                    intersection = len(left_keywords & right_keywords)
                    union_size = len(left_keywords | right_keywords)
                    if union_size and intersection / union_size > 0.5:
                        union(left, right)

        grouped: dict[int, list[dict[str, Any]]] = {}
        for idx, entry in enumerate(clusterable):
            grouped.setdefault(find(idx), []).append(entry)

        clusters: list[dict[str, Any]] = []
        for members in grouped.values():
            if len(members) < 2:
                continue
            member_keys = sorted(f"{m['book']}:{m['id']}" for m in members)
            cluster_id = f"cluster_{uuid.uuid5(uuid.NAMESPACE_URL, '|'.join(member_keys)).hex[:12]}"
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "entries": [
                        {
                            "book": m["book"],
                            "id": m["id"],
                            "content_preview": m["content_preview"],
                            "keywords": m["keywords"],
                            "constant": m["constant"],
                            "source": m["source"],
                        }
                        for m in sorted(members, key=lambda item: (item["book"], item["id"]))
                    ],
                    "suggested_action": "merge_candidates",
                }
            )
        clusters.sort(key=lambda cluster: len(cluster.get("entries", [])), reverse=True)
        payload = {
            "clusters": clusters,
            "total_entries": len(entries),
            "total_clusters": len(clusters),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _load_memory_topology_entries(self, workspace_root: Path) -> list[dict[str, Any]]:
        """Read minimal memory fields required for topology clustering."""
        mem_dir = workspace_root / "data" / "memory"
        if not mem_dir.exists() or not mem_dir.is_dir():
            return []

        result: list[dict[str, Any]] = []
        for yaml_path in sorted(mem_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("[scheduler] dream topology skipped %s: %s", yaml_path.name, exc)
                continue
            if not isinstance(data, dict):
                continue
            book = str(data.get("book") or yaml_path.stem).strip() or yaml_path.stem
            raw_entries = data.get("entries")
            if not isinstance(raw_entries, list):
                continue
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue
                eid = str(entry.get("id") or "").strip()
                if not eid:
                    continue
                raw_keywords = entry.get("keywords")
                if isinstance(raw_keywords, list):
                    keywords = [str(keyword).strip() for keyword in raw_keywords if str(keyword).strip()]
                elif isinstance(raw_keywords, str) and raw_keywords.strip():
                    keywords = [raw_keywords.strip()]
                else:
                    keywords = []
                content = str(entry.get("content") or "").strip()
                # [AutoC 2026-05-31] Why: the final Dream node must not scan full
                # memory files, but it still needs enough metadata to avoid
                # deleting protected entries. How: expose a small preview plus the
                # source/constant protection fields, not the full Task or raw YAML.
                # Purpose: make topology cleanup precise and safe.
                result.append(
                    {
                        "book": book,
                        "id": eid,
                        "content_preview": content[:50],
                        "keywords": keywords,
                        "keyword_set": set(keywords),
                        "constant": bool(entry.get("constant", False)),
                        "source": str(entry.get("source") or ""),
                    }
                )
        return result

    def _pending_expired(self, pending: dict[str, Any], *, now: datetime) -> bool:
        """Return whether a pending Dream run has exceeded its polling window."""
        raw_key = str(pending.get("now_key") or "").strip()
        try:
            started = datetime.strptime(raw_key, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo or timezone.utc)
        except Exception:
            return False
        return now - started > timedelta(minutes=_DREAM_PENDING_TIMEOUT_MINUTES)

    def _load_hit_cache_json(self, workspace_root: Path) -> str:
        """Load hit_cache.json and return as compact JSON string.

        [AutoC 2026-05-31] Why: Dream Prune phase needs hit timestamps to
        determine which auto-source entries are expired (30d no hit).
        How: read the JSON file, truncate to entries with hits in last 60d
        to keep instruction bounded. Purpose: enable lifecycle management.
        """
        hit_path = workspace_root / "data" / "memory" / ".hit_cache.json"
        if not hit_path.exists():
            return "{}"
        try:
            data = json.loads(hit_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return "{}"
            # Only include entries with hits in last 60 days to bound size
            from datetime import timedelta as _td
            cutoff = datetime.now(timezone.utc) - _td(days=60)
            filtered: dict[str, str] = {}
            for eid, ts_str in data.items():
                try:
                    ts = datetime.fromisoformat(str(ts_str))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        filtered[eid] = str(ts_str)
                except Exception:
                    pass
            return json.dumps(filtered, ensure_ascii=False)
        except Exception as exc:
            log.warning("[scheduler] dream: failed to load hit_cache: %s", exc)
            return "{}"

    def _load_skill_list(self, workspace_root: Path) -> str:
        """Load existing skill names and descriptions.

        [AutoC 2026-05-31] Why: Dream Promote phase needs to know existing
        skills to avoid creating duplicates. How: scan skills/*/SKILL.md
        frontmatter for name and description. Purpose: enable L1→L2 promotion.
        """
        skills_dir = workspace_root / "skills"
        if not skills_dir.exists():
            return "(no skills directory)"
        lines: list[str] = []
        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            name = skill_dir.name
            # Extract description from frontmatter
            desc = ""
            try:
                content = skill_md.read_text(encoding="utf-8")[:500]
                for line in content.split("\n"):
                    if line.strip().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
            except Exception:
                pass
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines) if lines else "(no skills found)"

    def _build_dream_instruction(
        self,
        *,
        run_id: str,
        now: datetime,
        signals: list[dict[str, Any]],
        topology_json: str,
        hit_cache_json: str = "{}",
        skill_list: str = "",
        book_list: str = "",
    ) -> str:
        """Assemble the final Dream task instruction from preprocessed data."""
        signals_json = json.dumps(signals, ensure_ascii=False, indent=2)
        # [AutoC 2026-05-31] Why: Dream should preserve reusable lessons,
        # related-memory activation, and valuable-but-dormant memories during
        # scheduled cleanup. How: include pattern extraction, association
        # discovery, and reactivation constraints in every generated Dream task.
        # Purpose: prevent memory organization from collapsing recurring
        # incidents into one concrete record or pruning useful rules too early.
        return f"""[auto_dream]
run_id: {run_id}
time: {now.strftime('%Y-%m-%d %H:%M UTC')}

以下是从最近活跃 session 中提取的新信号：
<extracted_signals>
{signals_json}
</extracted_signals>

以下是现有记忆的关键词拓扑聚类分析：
<keyword_topology>
{topology_json}
</keyword_topology>

以下是记忆关键词命中时间记录（entry_id → 最后命中时间 ISO）：
<hit_cache>
{hit_cache_json}
</hit_cache>

以下是现有 skill 列表：
<skill_list>
{skill_list}
</skill_list>

以下是当前 memory book 列表：
<book_list>
{book_list}
</book_list>

约束：
1. 不要调用 list_memories 全量扫描。
2. 不要读取对话文件（tail/cat/read_file）。
3. 只基于上面的 signals、topology、hit_cache 和 skill_list 做操作。
4. 对 signals 中的新信息：判断是否值得保存，调用 save_memory。
5. 对 topology 中的重复簇：判断是否需要合并/删除/更新，调用 save_memory/delete_memory。
6. 过期清理（Prune）：对 topology 中 source=auto 的条目，若 hit_cache 中超过 30 天未命中且 created_at 超过 30 天，审查后可用 delete_memory 清理。
7. 提升（Promote）：对 source=auto、created_at 超过 7 天、hit_cache 中最近 7 天内仍被命中、content 长度 > 200 字的条目，检查 skill_list 是否已有同主题 skill，如有则合并，如无则用 create_or_update_skill 提升为 skill，之后 delete_memory 删除原条目。单次最多提升 3 条。
8. 单轮最多操作 20 条（含 save/delete/create_or_update_skill）。
9. constant=true 的记忆和 source 不是 auto 的手工记忆，保持保护不删。
10. 完成后用 finish 报告操作摘要。
11. Book 整理：检查 <book_list> 中条目数 ≤ 5 的碎片 book，将其条目用 save_memory 迁移到语义最近的大 book，然后 delete_memory 删除原条目。
12. 结构抽象（Pattern Extraction）：合并重复簇（约束5）时，不要只保留最完整的一条——如果簇内多条记忆描述的是同一类事件的不同实例（如 5 条都是「在生产环境直接改代码出事」的记录），应从中归纳出一条更抽象的规则（如「所有代码修改必须先在 original 做」），而非简单保留其中一条。
13. 关联链强化（Association Discovery）：处理 topology 簇时，交叉比对 <hit_cache>。如果簇内多条记忆在 48h 内被同一类 session 命中（共现），说明它们在实际使用中经常一起被需要。用 save_memory 给它们互相补充 keywords，使它们更容易一起被激活。
14. 重激活（Reactivation）：对 priority > 0 且 <hit_cache> 中超过 20 天未命中（快过期但还没到 30 天删除线）的条目，审查其 keywords 是否过窄导致命中率低。如果内容仍有价值，用 save_memory 拆分或优化 keywords 使其更容易被触发，而非坐等过期删除。"""
