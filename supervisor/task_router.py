"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import copy
import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config

from ._helpers import _now
from .types import Task, TaskKind, TaskStatus


log = logging.getLogger(__name__)


@dataclass
class PostCompletionWork:
    """Frozen payload for non-critical work after a task completion is routed.

    Why: outbound_message must become visible as soon as the critical route has
    merged branch history and appended the user reply. How: copy the completed
    task and its task-scoped message snapshot before the supervisor lock is
    released, then let a background worker run hooks and turn-summary creation.
    Purpose: preserve memory extraction and turn summaries without keeping bot
    event polling blocked behind their I/O and preprocessing.
    """

    task: Task
    route_session_id: str
    session_generation: int
    task_messages: list[Any] = field(default_factory=list)


def _message_to_turn_summary_dict(message: Any) -> dict[str, Any]:
    """Convert a task message into the history shape used for summary input.

    Why: post-completion work now receives a frozen task-message snapshot from
    the completion critical section instead of reloading ConversationStore later.
    How: accept both Message objects and serialized dictionaries, then copy role,
    content, metadata, tool calls, and native tool-result pairing fields into the
    same shape runner uses. Purpose: one sanitizer protects LLM replay and
    system.turn_summarizer input while the summary path stays I/O-free.
    """
    if isinstance(message, dict):
        d: dict[str, Any] = {
            "role": str(message.get("role") or "unknown"),
            "content": message.get("content") or "",
        }
        meta: dict[str, Any] = {}
        raw_meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else message.get("meta")
        if isinstance(raw_meta, dict):
            meta.update(raw_meta)
        source_task_id = str(message.get("source_task_id") or "").strip()
        if source_task_id:
            meta.setdefault("source_task_id", source_task_id)
        if meta:
            d["_meta"] = meta
        message_type = str(message.get("message_type") or "").strip()
        if message_type:
            d["message_type"] = message_type
        if isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
            d["tool_calls"] = copy.deepcopy(message.get("tool_calls"))
        if message.get("tool_call_id"):
            d["tool_call_id"] = str(message.get("tool_call_id") or "")
        if message.get("name"):
            d["name"] = str(message.get("name") or "")
        return d

    d = {"role": getattr(message, "role", "unknown"), "content": getattr(message, "content", "") or ""}
    meta = dict(message.meta) if isinstance(getattr(message, "meta", None), dict) else {}
    source_task_id = str(getattr(message, "source_task_id", "") or "").strip()
    if source_task_id:
        meta.setdefault("source_task_id", source_task_id)
    if meta:
        d["_meta"] = meta
    message_type = str(getattr(message, "message_type", "") or "").strip()
    if message_type:
        d["message_type"] = message_type
    if getattr(message, "tool_calls", None):
        d["tool_calls"] = list(message.tool_calls)
    if getattr(message, "tool_call_id", ""):
        d["tool_call_id"] = str(message.tool_call_id)
    if getattr(message, "name", ""):
        d["name"] = str(message.name)
    return d


def _format_task_messages_for_turn_summary(messages: list[Any]) -> str:
    """Format task messages for system.turn_summarizer without control-tool plumbing."""
    from engine.inference.tool_format import sanitize_control_tool_history

    # [2026-05-07] 轮摘要输入必须跳过 finish 控制流工具历史。
    # 原因：system.turn_summarizer 从 ConversationStore 重新读取任务消息，旧数据中可能已有 finish tool_call/tool_result。
    # 做法：把 Message 转成与 LLM 历史一致的 dict 后复用控制流清洗，再拼接摘要输入。
    # 目的：摘要子任务只看到已交付文本和普通工具结果，不继续传播 finish 协议占位内容。
    cleaned = sanitize_control_tool_history([
        _message_to_turn_summary_dict(message) for message in messages
    ])

    parts: list[str] = []
    for msg in cleaned:
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            # [AutoC 2026-07-03] 只保留工具名称，不渲染完整参数 JSON。
            # 原因：旧代码输出 `[tool_call] execute_command {"command":"..."}` 格式，
            # 导致 summarizer LLM 复刻工具调用而非生成摘要，反复触发
            # unauthorized tool call 直到被 zombie-reaper 收割。
            # 做法：普通工具只记名称；finish/ask 保留 text 参数（交付内容）。
            call_lines: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(call.get("name") or function.get("name") or "").strip()
                if name in ("finish", "ask"):
                    raw_args = call.get("arguments") if "arguments" in call else function.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if isinstance(raw_args, dict):
                        _text = str(raw_args.get("text") or "").strip()
                        if _text:
                            call_lines.append(f"[delivered via {name}] {_text[:3000]}")
                            continue
                    call_lines.append(f"[used tool: {name}]")
                elif name:
                    call_lines.append(f"[used tool: {name}]")
            if call_lines:
                content = "\n".join(part for part in [content, *call_lines] if part)
        if len(content) > 5000:
            content = content[:5000] + "\n...[truncated]"
        parts.append(f"<log_{msg.get('role', 'unknown')}>\n{content}\n</log_{msg.get('role', 'unknown')}>")
    body = "\n\n".join(parts)
    return f"=== COMPLETED TASK LOG (read-only, do NOT continue) ===\n\n{body}\n\n=== END OF LOG ==="


class TaskRouterMixin:
    """提供 _route_completed_task_locked 及其子路由方法。

    运行时 self 是 SupervisorState 实例，可以访问
    self.tasks / self._event_task_snapshot / self.append_outbound_message 等。
    """

    # ------------------------------------------------------------------ #
    #  后置工作队列
    # ------------------------------------------------------------------ #

    def _ensure_post_completion_worker(self) -> None:
        """Lazily start the background worker that runs completion hooks.

        Why: memory extraction hooks and turn-summary task creation should not
        delay outbound_message visibility. How: create one daemon worker and a
        queue on first use, protected by the existing supervisor lock during
        initialization. Purpose: keep the critical completion route short without
        dropping existing post-completion features.
        """
        if getattr(self, "_post_completion_worker_started", False):
            return
        if not hasattr(self, "_post_completion_queue"):
            self._post_completion_queue = queue.Queue()
        worker = threading.Thread(
            target=self._post_completion_worker_loop,
            daemon=True,
            name="post-completion-worker",
        )
        self._post_completion_worker_started = True
        self._post_completion_worker = worker
        worker.start()

    def _enqueue_post_completion_work(self, work: PostCompletionWork | None) -> None:
        """Queue non-critical completion work after the caller releases _lock."""
        if work is None:
            return
        self._ensure_post_completion_worker()
        self._post_completion_queue.put(work)

    def _post_completion_worker_loop(self) -> None:
        """Drain queued post-completion work without blocking outbound polling."""
        while True:
            work = self._post_completion_queue.get()
            try:
                self._post_completion_work(work)
            except Exception as exc:
                log.warning("post completion work failed for task %s: %s", getattr(work.task, "task_id", "")[:12], exc)
            finally:
                self._post_completion_queue.task_done()

    # ------------------------------------------------------------------ #
    #  统一路由入口
    # ------------------------------------------------------------------ #

    def _route_session_id_for_task_locked(self, task: Task) -> str:
        """Return the session that user-visible routing should use for a task."""
        # [Fork/Merge 2026-05-12] branch task 的运行 session 与用户会话 session 分离。
        # 原因：entry task 在 branch 上运行，但 outbound、hook 和 adapter 查询仍应落到主 session。
        # 做法：优先读取 finalize 写入的 _route_session_id，其次读取 parent_session_id。
        # 目的：没有 branch_session_id 的旧 task 保持原 session_id 行为。
        return str(
            task.input.get("_route_session_id")
            or task.input.get("parent_session_id")
            or task.session_id
            or ""
        )

    def _merge_branch_transcript_locked(self, parent_session_id: str, branch_session_id: str) -> int:
        """Append branch transcript records into parent transcript, rewriting session_id.

        Returns number of records merged.
        """
        from pathlib import Path
        import json
        transcript_dir = Path(self.workspace_root) / "data" / "transcripts"
        branch_path = transcript_dir / f"{branch_session_id}.jsonl"
        if not branch_path.exists():
            return 0
        parent_path = transcript_dir / f"{parent_session_id}.jsonl"
        count = 0
        try:
            with open(branch_path, "r", encoding="utf-8") as bf:
                lines = bf.readlines()
            with open(parent_path, "a", encoding="utf-8") as pf:
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        # Rewrite session_id so snip_history and summary write-back
                        # can find the record under the parent session.
                        rec["session_id"] = parent_session_id
                        pf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        count += 1
                    except json.JSONDecodeError:
                        continue
            if count:
                log.info("Merged %d transcript records from %s → %s", count, branch_session_id, parent_session_id[:12])
                # [2026-05-25] 用后即焚：合并成功后删除 branch transcript 源文件
                try:
                    branch_path.unlink()
                except Exception:
                    pass
        except Exception as e:
            log.warning("Failed to merge branch transcript %s: %s", branch_session_id, e)
        return count

    def _finalize_branch_task_locked(self, task: Task, *, merge: bool) -> str:
        """Merge and clean an entry branch task once; return the routing session id."""
        parent_session_id = str(task.input.get("parent_session_id") or "").strip()
        branch_session_id = str(task.input.get("branch_session_id") or "").strip()
        if not parent_session_id or not branch_session_id:
            return task.session_id
        task.input["_route_session_id"] = parent_session_id
        if task.input.get("_branch_finalized"):
            return parent_session_id

        merged_count = 0
        if merge:
            try:
                base_count = int(task.input.get("base_count") or 0)
            except Exception:
                base_count = 0
            merged_count = self._merge_branch_locked(parent_session_id, branch_session_id, base_count)
            # [2026-05-19] Merge branch transcript records into parent transcript.
            # Why: engine writes TaskRecord to branch_xxx.jsonl (keyed by task.session_id),
            # but turn_summary and snip_history look up records in the parent transcript.
            # How: append all branch transcript lines to parent transcript file.
            # Purpose: ensure turn summary write-back and snip_history can find the records.
            self._merge_branch_transcript_locked(parent_session_id, branch_session_id)
        # [Fork/Merge 2026-05-12] finalize 标记写入 task.input。
        # 原因：完成、取消、僵尸回收等路径都可能尝试收束同一个入口分支。
        # 做法：用 _branch_finalized 做进程内幂等保护，并记录 merged_count 供调试。
        # 目的：避免重复 merge 或重复删除 branch session。
        task.input["_branch_finalized"] = True
        task.input["_branch_merged_count"] = merged_count
        if merge:
            self._cleanup_branch_locked(branch_session_id)
        return parent_session_id

    def _route_completed_task_locked(self, task: Task) -> None:
        """统一路由入口。根据 result.action 分发。"""
        # [AutoC 2026-05-30] Why: child session 清理曾散落在多个 return
        # 路径，新增路由分支时容易遗漏。How: 外层只负责 try/finally，原路由
        # 逻辑下沉到 inner。Purpose: 所有完成路径共享一个清理出口。
        try:
            self._route_completed_task_inner_locked(task)
        finally:
            self._cleanup_task_child_session_if_needed(task)

    def _route_completed_task_inner_locked(self, task: Task) -> None:
        """执行 task 完成后的实际路由逻辑。"""
        # [AutoC 2026-05-30] Why: _route_completed_task_locked 需要统一 finally
        # 清理，但直接包裹原大方法会造成整体缩进变更。How: 保持原路由主体
        # 缩进不变并移动到 inner。Purpose: 降低重构噪音，便于审查行为差异。
        # ---- 批量 task：优先走统一批量收集 ----
        if task.batch_id:
            self._try_complete_batch_locked(task)
            # 后置记忆提取不对 batch 子 task 触发
            return

        # ---- 压缩 dispatch 结果：compactor 子 task 完成 ----
        if self._is_compact_dispatch_result(task):
            self._apply_compact_result_locked(task)
            return

        # ---- 轮摘要 dispatch 结果：summarizer 子 task 完成 ----
        if self._is_turn_summary_result(task):
            self._apply_turn_summary_result_locked(task)
            return

        action = task.result or {}
        act = str(action.get("action") or "").strip()
        route_session_id = task.session_id

        # system node 失败时发通知（memory_extractor / dream 等）
        _sys_node_id = str(task.node_id or "")
        if _sys_node_id.startswith("system.") and act == "fail":
            _sys_fail_reason = str((action.get("result") or {}).get("error") or action.get("error") or "").strip()
            _sys_labels = {
                "system.memory_extractor": "记忆提取",
                "system.dream": "记忆整理",
                "system.turn_summarizer": "轮摘要",
            }
            _sys_label = _sys_labels.get(_sys_node_id, _sys_node_id)
            log.warning(
                "%s node failed: task=%s error=%s",
                _sys_label, str(task.task_id or "")[:12], _sys_fail_reason[:200] if _sys_fail_reason else "unknown",
            )
            _sys_notice_sid = str(task.session_id or "").strip()
            if _sys_notice_sid:
                self.eventlog.append(
                    session_id=_sys_notice_sid,
                    component="supervisor",
                    type_="system_notice",
                    payload={
                        "level": "warning",
                        "title": f"{_sys_label}失败",
                        "text": f"{_sys_label}节点执行失败，不影响正常对话。",
                        "subsystem": _sys_node_id,
                        "error": _sys_fail_reason[:500] if _sys_fail_reason else None,
                    },
                )

        if act == "preempted":
            self._route_preempted_locked(task, action)
            return  # preempted 不触发记忆提取，也不 merge branch
        elif act == "dispatch":
            self._route_dispatch_locked(task, action)
        elif act in ("finish", "ask", "reject"):
            # [AutoC 2026-06-08] Why: output chain 会在 finish 后同步创建下游 task，入口分支不能提前收束。
            # How: finish/ask 先交给 _route_finish_locked 判断 output、ask 和 __source__ 语义。
            # Purpose: 只有最终穿透到 __source__ 或旧式离开链路时才 merge/cleanup branch。
            # [AutoC 2026-06-10] Why: reject is also a terminal child action that
            # must be routed before branch finalization. How: include it in this
            # dispatch set and let _route_finish_locked select reject routing.
            # Purpose: output chain QA can resume the entry task instead of falling
            # through without routing.
            self._route_finish_locked(task, action)
            if self._task_terminal(task):
                route_session_id = self._route_session_id_for_task_locked(task)
            else:
                return
        elif act == "fail":
            # [Fork/Merge 2026-05-12] fail 也会结束入口分支。
            # 原因：错误回复应进入主 session，同时保留分支中已产生的上下文。
            # 做法：先 merge/cleanup，再走原 fail 输出。目的：失败路径与成功路径一致。
            route_session_id = self._finalize_branch_task_locked(task, merge=True)
            self._route_fail_locked(task, action)
        elif act == "cancelled":
            # [Fork/Merge 2026-05-12] engine 主动返回 cancelled 时不输出，但仍收束分支。
            # 原因：cancel 是终态，不应遗留 branch session。做法：只 finalize，不调用输出路由。
            # 目的：符合 finish/cancel 时 merge 回主 session 的架构约束。
            route_session_id = self._finalize_branch_task_locked(task, merge=True)
        # cancelled → 不做用户输出

        # Why: automatic memory extraction is now an engine.builtin supervisor
        # hook handler and must not receive SupervisorState directly. How: build a
        # callback-only context that includes the completed task. Purpose: keep
        # TaskRouterMixin focused on routing while eliminating handler cycles.
        self.hook_registry.fire(
            "on_entry_task_complete",
            self._build_supervisor_hook_ctx(task=task, session_id=route_session_id),
        )

        # 后置触发：检查是否需要创建轮摘要任务
        self._maybe_trigger_turn_summary_locked(task)

    # ------------------------------------------------------------------ #
    #  dispatch
    # ------------------------------------------------------------------ #

    def _route_dispatch_locked(self, task: Task, action: dict[str, Any]) -> None:
        target = str(action.get("target_node") or "").strip()
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        dispatch_batch = action.get("dispatch_batch")

        # 标记父 task 正在等待压缩 dispatch
        compact_target_session_id = ""
        if dispatch_input.get("_compact_dispatch"):
            task.input["_compact_dispatch_pending"] = True
            task.input["_compact_keep_recent"] = int(
                dispatch_input.get("_compact_keep_recent", 6)
            )
            # [AutoC 2026-05-13] Why: compactor results are applied after the
            # caller task is suspended, and caller.session_id may be an entry
            # branch. How: store the explicit parent-first target from engine,
            # falling back to caller parent/session for older actions. Purpose:
            # _apply_compact_result_locked always rewrites the durable session.
            compact_target_session_id = str(
                dispatch_input.get("target_session_id")
                or task.input.get("parent_session_id")
                or task.session_id
                or ""
            ).strip()
            if compact_target_session_id:
                task.input["_compact_target_session_id"] = compact_target_session_id

        created_child_ids: list[str] = []
        # [Fork/Merge 2026-05-17] Why: descendants of an entry branch should keep
        # branch-local storage but still route user-visible events through the
        # parent session. How: compute the parent route once and propagate it in
        # child task input while leaving task.session_id unchanged. Purpose: child
        # nodes/tools do not call supervisor APIs with a soon-to-be-deleted branch.
        route_parent_session_id = self._route_session_id_for_task_locked(task)
        route_parent_for_child = route_parent_session_id if route_parent_session_id != task.session_id else ""

        # ---- 批量委派（node 或 tool 均可） ----
        if isinstance(dispatch_batch, list) and dispatch_batch:
            batch_id = str(uuid.uuid4())
            for idx, item in enumerate(dispatch_batch):
                item_kind = str(item.get("kind") or "node").strip()
                item_target = str(item.get("target") or "").strip()
                if not item_target:
                    continue

                if item_kind == "tool":
                    _tool_input = {
                        "arguments": item.get("arguments", {}),
                        "call_id": str(item.get("call_id") or ""),
                    }
                    if route_parent_for_child:
                        # [Fork/Merge 2026-05-17] Why: batch tool tasks inherit
                        # the caller branch as runtime session. How: carry the
                        # parent route in input for engine.runner ToolContext.
                        # Purpose: guarded tools and progress route to parent.
                        _tool_input["parent_session_id"] = route_parent_for_child
                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.tool,
                        tool_name=item_target,
                        input_data=_tool_input,
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                else:
                    _batch_ctx_mode = str(item.get("context_mode") or "accumulate").strip()
                    _batch_ctx_key = str(item.get("context_key") or "").strip() or None
                    _batch_input: dict[str, Any] = {
                        "instruction": str(item.get("instruction") or "").strip(),
                    }
                    if route_parent_for_child:
                        # [Fork/Merge 2026-05-17] Why: batch child nodes can run
                        # under a branch session while emitting progress or tool
                        # approvals. How: pass the durable parent as route metadata.
                        # Purpose: RunContext.emit_event targets the user session.
                        _batch_input["parent_session_id"] = route_parent_for_child
                    if _batch_ctx_key:
                        _batch_input["_context_key"] = _batch_ctx_key

                    # Child Session 隔离（Phase B）：批量委派也走 child session
                    _b_child_sid, _b_is_new = self.get_or_create_child_session(
                        task.session_id, item_target, _batch_ctx_key or "", _batch_ctx_mode,
                    )
                    _batch_input["child_session_id"] = _b_child_sid
                    _batch_input["context_mode"] = _batch_ctx_mode
                    _batch_input["use_context"] = False
                    if _batch_ctx_mode == "fork":
                        _batch_input["fork_from_session_id"] = task.session_id
                    # 审计报告 Step 1（2026-04-16）：删除此处对 _find_last_context_ref_locked
                    # 的 accumulate fallback 注入。engine/runner.py 在 child_session_id
                    # 非空时会无条件清空 context_ref（参见 runner.py:514），此注入永远
                    # 不会被消费。保留只是兼容期死代码，现移除。

                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.node,
                        node_id=item_target,
                        input_data=_batch_input,
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                created_child_ids.append(child.task_id)

        # ---- 单节点委派 ----
        if not created_child_ids and target:
            _ctx_mode = str(dispatch_input.get("context_mode") or "accumulate").strip()
            _ctx_key = str(dispatch_input.get("context_key") or "").strip() or None
            _single_input: dict[str, Any] = {
                "instruction": str(dispatch_input.get("instruction") or "").strip(),
            }
            if route_parent_for_child:
                # [Fork/Merge 2026-05-17] Why: a synchronous child task should be
                # branch-local for merge semantics but parent-routed for events and
                # session tools. How: pass parent_session_id without changing the
                # child task's runtime session. Purpose: avoid hidden branch events.
                _single_input["parent_session_id"] = route_parent_for_child
            if _ctx_key:
                _single_input["_context_key"] = _ctx_key

            # Child Session 隔离（Phase B）：用 child session 替代 context_ref
            # [AutoC 2026-05-13] Why: system.compactor is a child node, but its
            # output must target the parent session when the caller is a branch.
            # How: for compact dispatch only, key the child session and child task
            # under compact_target_session_id. Purpose: compactor bookkeeping and
            # result application stay attached to the durable parent session.
            _dispatch_session_id = compact_target_session_id or task.session_id
            _child_sid, _is_new = self.get_or_create_child_session(
                _dispatch_session_id, target, _ctx_key or "", _ctx_mode,
            )
            _single_input["child_session_id"] = _child_sid
            _single_input["context_mode"] = _ctx_mode
            _single_input["use_context"] = False  # child session 模式不走 _fetch_history
            if _ctx_mode == "fork":
                # fork 模式需要告诉 engine 从哪个 session 复制历史
                _single_input["fork_from_session_id"] = _dispatch_session_id

            # 审计报告 Step 1（2026-04-16）：删除此处单节点 dispatch 的 accumulate/fresh
            # 兼容期 fallback。engine/runner.py:514 在 child_session_id 非空时会无条件
            # 清空 context_ref，accumulate/fresh 两个分支写的值永远不会被消费。

            child = self._create_task_locked(
                session_id=_dispatch_session_id,
                session_generation=task.session_generation,
                kind=TaskKind.node,
                node_id=target,
                input_data=_single_input,
                continuation={},
                source_inbound_seq=task.source_inbound_seq,
                caller_task_id=task.task_id,
            )
            created_child_ids.append(child.task_id)

        # ---- 挂起当前 task ----
        if created_child_ids:
            task.status = TaskStatus.suspended
            task.waiting_for_task_id = created_child_ids[0] if len(created_child_ids) == 1 else None
            task.updated_at = _now()
            self._event_task_snapshot("task_suspended", task)

    # ------------------------------------------------------------------ #
    #  preempted
    # ------------------------------------------------------------------ #

    def _route_preempted_locked(self, task: Task, action: dict[str, Any]) -> None:
        """处理 preempted 结果：保存 context_ref，发射事件，恢复 caller（如有）。"""
        context_ref = str(action.get("context_ref") or "")
        task.preempted_context_ref = context_ref

        # [Fork/Merge 2026-05-17] Why: preempt notifications are user-visible,
        # while task.session_id can be a temporary branch. How: route the event to
        # the parent session when task metadata provides one. Purpose: adapters
        # polling the durable session can observe the preempted task.
        route_session_id = self._route_session_id_for_task_locked(task)
        self.eventlog.append(
            session_id=route_session_id,
            component="supervisor",
            type_="task_preempted",
            payload={
                "task_id": task.task_id,
                "node_id": task.node_id,
                "context_ref": context_ref,
                "session_id": route_session_id,
                "runtime_session_id": task.session_id,
            },
            transient=True,
        )

        # 子节点被 preempt：恢复 suspended 的 caller，注入 child_preempted
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if caller and caller.status == TaskStatus.suspended:
                caller.status = TaskStatus.pending
                caller.waiting_for_task_id = None
                caller.worker_id = None
                caller.lease_expires_at = None
                caller.updated_at = _now()
                caller.input["resume_data"] = {
                    "type": "child_preempted",
                    "child_node_id": str(task.node_id or ""),
                    "context_ref": context_ref,
                }
                caller.input["context_ref"] = str(
                    caller.result.get("context_ref")
                    or caller.input.get("context_ref")
                    or ""
                )
                self._event_task_snapshot("task_resumed", caller)

        # 不输出给用户，不注入 inbound

    # ------------------------------------------------------------------ #
    #  finish / ask / fail
    # ------------------------------------------------------------------ #

    def _route_finish_locked(self, task: Task, action: dict[str, Any]) -> None:
        result = action.get("result") if isinstance(action.get("result"), dict) else {}
        action_type = str(action.get("action") or "finish").strip() or "finish"
        if action_type == "ask":
            return self._route_ask_to_caller_locked(task, result)
        if action_type == "reject":
            return self._route_reject_to_source_locked(task, result)

        output_target = self._node_output_target_locked(str(task.node_id or ""))
        if output_target:
            if output_target == "__source__":
                return self._route_output_chain_to_source_locked(task, result)
            return self._start_output_chain_task_locked(task, output_target, result)

        return self._route_finish_to_caller_or_user_locked(task, result)

    def _node_output_target_locked(self, node_id: str | None) -> str:
        """Return the node YAML output target used by the Supervisor route."""
        # [AutoC 2026-06-08] Why: output chain 声明位于节点 YAML，但节点运行时不应感知路由拓扑。
        # How: Supervisor 在 finish 后按需读取 Node.output，读取失败或空字段都返回空字符串。
        # Purpose: 无 output 字段的节点完全沿用旧 finish 路由。
        nid = str(node_id or "").strip()
        if not nid:
            return ""
        try:
            from engine.node import load_node
            node = load_node(self.workspace_root, nid)
        except Exception as exc:
            log.warning("output chain: load node %s failed: %s", nid, exc)
            return ""
        return str(getattr(node, "output", "") or "").strip() if node else ""

    def _output_chain_resume_data(self, task: Task, result: dict[str, Any], *, action_type: str = "finish") -> dict[str, Any]:
        """Build the resume payload shared by ordinary finish and output chain finish."""
        # [AutoC 2026-06-08] Why: output chain 的下游和旧 caller 恢复都需要同一份结构化结果。
        # How: 保持原 child_result payload，并继续携带 action_type 兼容既有调用方。
        # Purpose: 新链路不改变普通 finish 的下游可见数据格式。
        return {
            "type": "child_result",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
            "action_type": action_type,
        }

    def _compute_or_inherit_output_chain_meta_locked(self, task: Task) -> dict[str, Any]:
        """Compute entry/source/path metadata for the next output chain task."""
        # [AutoC 2026-06-08] Why: __source__ 必须指向链入口的原始 caller，而不是直接上游。
        # How: 链内任务继承已有元数据；链入口第一次创建下游时记录自身 task_id、caller、route session 和 dispatch origin。
        # Purpose: 最终节点 output=__source__ 时能准确穿透回入口任务的原始调用方。
        current_node = str(task.node_id or "").strip()
        inherited_path = [str(x).strip() for x in (task.output_chain_path or []) if str(x).strip()]
        if inherited_path:
            path = list(inherited_path)
        else:
            path = [current_node] if current_node else []
        entry_task_id = str(task.output_chain_entry_task_id or "").strip() or task.task_id
        source_task_id = str(task.output_chain_source_task_id or "").strip() or str(task.caller_task_id or "").strip() or None
        source_session_id = str(task.output_chain_source_session_id or "").strip() or self._route_session_id_for_task_locked(task)
        dispatch_origin = dict(task.output_chain_source_dispatch_origin or {})
        if not dispatch_origin and isinstance(task.input.get("_dispatch_origin"), dict):
            dispatch_origin = dict(task.input.get("_dispatch_origin") or {})
        depth = int(task.output_chain_depth or max(len(path) - 1, 0))
        return {
            "entry_task_id": entry_task_id,
            "source_task_id": source_task_id,
            "source_session_id": source_session_id,
            "dispatch_origin": dispatch_origin,
            "path": path,
            "depth": depth,
        }

    def _validate_output_chain_target_locked(self, task: Task, target_node_id: str, meta: dict[str, Any]) -> tuple[bool, str]:
        """Validate output chain next target for loops and configured max depth."""
        # [AutoC 2026-06-08] Why: YAML output 配置错误可能形成递归链或过深链路。
        # How: 用 output_chain_path 做循环检测，并从 runtime 读取 max_depth，默认 8 且上限 64。
        # Purpose: 错误配置失败得可见，而不是无限创建任务。
        target = str(target_node_id or "").strip()
        if not target:
            return False, "output chain target is empty"
        path = [str(x).strip() for x in (meta.get("path") or []) if str(x).strip()]
        if target in path:
            return False, f"output chain loop detected: {' -> '.join([*path, target])}"
        runtime_cfg = load_runtime_config(self.workspace_root)
        max_depth = get_int(runtime_cfg, "engine.output_chain.max_depth", 8, min_value=1, max_value=64)
        depth = int(meta.get("depth") or 0)
        if depth + 1 > max_depth:
            return False, f"output chain max depth exceeded: {depth + 1}/{max_depth}"
        return True, ""

    @staticmethod
    def _extract_output_chain_text(result: dict[str, Any]) -> str:
        """Extract raw finish text for the downstream output chain node."""
        # [AutoC 2026-06-09] Why: downstream nodes should receive the raw
        # finish text without any wrapper. AI nodes get context from their
        # system prompt; script nodes get raw text for deterministic checks.
        return str(result.get("text") or "").strip()

    def _result_attachments(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract finish attachments from result dict.

        Supported shapes:
          1. result["data"]["attachments"]  — data-wrapped result
          2. result["attachments"]           — flat inner result
        """
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        atts = data.get("attachments") if isinstance(data.get("attachments"), list) else result.get("attachments")
        return list(atts) if isinstance(atts, list) else []

    def _start_output_chain_task_locked(self, task: Task, target_node_id: str, result: dict[str, Any]) -> None:
        """Create the next synchronous output chain task and suspend the current task."""
        # [AutoC 2026-06-08] Why: finish + output 表示同步后处理，当前 task 不能先回到 caller 或用户。
        # How: 创建 fresh child session 的下游 task，并把当前 task 改回 suspended 等待下游完成。
        # Purpose: output chain 中间节点保持调用栈形态，ask/fail/cancel 仍可沿 caller chain 返回。
        meta = self._compute_or_inherit_output_chain_meta_locked(task)
        ok, error = self._validate_output_chain_target_locked(task, target_node_id, meta)
        if not ok:
            # [AutoC 2026-06-08] Why: output 配置校验失败发生在创建下游前，当前 finish 已经不能继续链路。
            # How: 先按旧终态收束入口分支，再复用 fail 路由输出错误或恢复 caller。
            # Purpose: 循环和深度错误不会遗留未清理的 entry branch。
            self._finalize_branch_task_locked(task, merge=True)
            return self._route_fail_locked(task, {"error": error})

        target = str(target_node_id or "").strip()
        route_parent_session_id = self._route_session_id_for_task_locked(task)
        child_sid, _ = self.get_or_create_child_session(route_parent_session_id, target, f"output_chain:{task.task_id}", "fresh")
        next_path = [*list(meta.get("path") or []), target]
        next_depth = int(meta.get("depth") or 0) + 1
        child_input: dict[str, Any] = {
            "instruction": self._extract_output_chain_text(result),
            "attachments": self._result_attachments(result),
            # [AutoC 2026-06-09] Why: downstream nodes may need the full
            # result dict (summary, data, attachments) beyond just text.
            "upstream_result": dict(result) if isinstance(result, dict) else {},
            "parent_session_id": route_parent_session_id if route_parent_session_id != task.session_id else "",
            "child_session_id": child_sid,
            "context_mode": "fresh",
            "dispatch_context_mode": "fresh",
            "use_context": False,
            # [AutoC 2026-06-08] Why: 调试和事件消费者需要知道该 task 来自 output chain，而非普通 dispatch。
            # How: 在 input 中写入轻量标记和上游 task/node，Task 字段保存可路由元数据。
            # Purpose: 不影响 engine 协议，同时保留可观测性。
            "_output_chain": True,
            "_output_chain_from_task_id": task.task_id,
            "_output_chain_from_node_id": str(task.node_id or task.tool_name or ""),
        }
        child = self._create_task_locked(
            session_id=task.session_id,
            session_generation=task.session_generation,
            kind=TaskKind.node,
            node_id=target,
            input_data=child_input,
            continuation={},
            source_inbound_seq=task.source_inbound_seq,
            caller_task_id=task.task_id,
            output_chain_entry_task_id=str(meta.get("entry_task_id") or "") or None,
            output_chain_source_task_id=meta.get("source_task_id"),
            output_chain_source_session_id=str(meta.get("source_session_id") or "") or None,
            output_chain_source_dispatch_origin=dict(meta.get("dispatch_origin") or {}),
            output_chain_path=next_path,
            output_chain_depth=next_depth,
        )
        task.status = TaskStatus.suspended
        task.waiting_for_task_id = child.task_id
        task.worker_id = None
        task.lease_expires_at = None
        task.updated_at = _now()
        task.result = dict(task.result or {})
        task.result["_output_chain_waiting_for"] = child.task_id
        self._event_task_snapshot("task_suspended", task)

    def _route_output_chain_to_source_locked(self, task: Task, result: dict[str, Any]) -> None:
        """Route the final output chain result through the original source caller."""
        # [AutoC 2026-06-08] Why: __source__ 表示回到链入口的原始 caller，不是最终节点的直接上游。
        # How: 找到入口 task，完成中间 suspended 等待者，再用入口 task 执行旧式 caller/user 输出。
        # Purpose: coder -> review -> qa 的最终结果只对原始 caller 或用户可见。
        entry_id = str(task.output_chain_entry_task_id or "").strip()
        entry = self.tasks.get(entry_id) if entry_id else None
        if entry is None:
            return self._route_finish_to_caller_or_user_locked(task, result)
        self._complete_output_chain_waiters_locked(final_task=task, entry_task=entry)
        resume_data = self._output_chain_resume_data(task, result, action_type="finish")
        source_task_id = str(task.output_chain_source_task_id or entry.caller_task_id or "").strip()
        if source_task_id:
            entry.caller_task_id = source_task_id
        source_session_id = str(task.output_chain_source_session_id or "").strip()
        if source_session_id:
            entry.input["_route_session_id"] = source_session_id
        dispatch_origin = dict(task.output_chain_source_dispatch_origin or {})
        if dispatch_origin:
            entry.input["_dispatch_origin"] = dispatch_origin
        self._finalize_branch_task_locked(entry, merge=True)
        # [AutoC 2026-06-09] Why: _finalize_branch_task_locked 内部也会写 _route_session_id（覆盖为 parent_session_id），
        # 在 dispatch_origin 场景下可能与 output chain 的 source_session_id 不一致。
        # How: finalize 之后重新写入 source_session_id 保证穿透目标正确。
        # Purpose: 最终 resume_caller_or_output 使用正确的 route session。
        if source_session_id:
            entry.input["_route_session_id"] = source_session_id
        self._resume_caller_or_output_locked(entry, resume_data, result, action_type="finish")
        # [AutoC 2026-06-08] Why: output chain 入口 task 在最初 finish 时被重新挂起，无法再走自己的 finally 清理。
        # How: 等最终结果已经恢复 caller 或注入 dispatch_origin 后，再清理入口的 fresh/fork child session。
        # Purpose: 回调事件仍能携带有效 child_session_id，同时不会泄漏一次性子会话。
        self._cleanup_task_child_session_if_needed(entry)

    def _complete_output_chain_waiters_locked(self, *, final_task: Task, entry_task: Task) -> None:
        """Mark suspended output chain waiters as completed after final source routing."""
        # [AutoC 2026-06-08] Why: output chain 中间 task 只是同步等待者，最终穿透后不应继续保持 suspended。
        # How: 从最终 task 的 caller 链回溯到入口 task，逐个清空 waiting_for_task_id 并标记 completed。
        # Purpose: 活跃任务列表不会残留已经完成的链路中间节点。
        now = _now()
        tid = str(final_task.caller_task_id or "").strip()
        visited: set[str] = set()
        while tid and tid not in visited:
            visited.add(tid)
            waiter = self.tasks.get(tid)
            if waiter is None:
                break
            if waiter.status == TaskStatus.suspended:
                waiter.status = TaskStatus.completed
                waiter.waiting_for_task_id = None
                waiter.worker_id = None
                waiter.lease_expires_at = None
                waiter.updated_at = now
                waiter.result = dict(waiter.result or {})
                waiter.result["_output_chain_completed_by"] = final_task.task_id
                self._event_task_snapshot("task_completed", waiter)
                if waiter.task_id != entry_task.task_id:
                    # [AutoC 2026-06-08] Why: 中间 output chain 节点不会再次经过 complete_task 的 finally。
                    # How: 在标记非入口等待者完成后主动调用统一 child session 清理逻辑，入口延后到最终恢复后清理。
                    # Purpose: fresh output chain 子会话不会泄漏，也不提前删除入口回调需要的 child_session_id。
                    self._cleanup_task_child_session_if_needed(waiter)
            if waiter.task_id == entry_task.task_id:
                break
            tid = str(waiter.caller_task_id or "").strip()

    def _route_ask_to_caller_locked(self, task: Task, result: dict[str, Any]) -> None:
        """Route ask directly to the immediate caller without following output declarations."""
        # [AutoC 2026-06-08] Why: ask 表示当前节点需要直接上游补充信息，不是可继续加工的 finish 产物。
        # How: 使用 child_ask resume payload 恢复 caller；无 caller 时按旧式用户输出收束分支。
        # Purpose: qa ask -> review、review ask -> coder、coder ask -> user 的逐级打回语义成立。
        # [AutoC 2026-06-09] Why: 旧代码对有 caller 的 ask 跳过了 branch finalization，
        # 如果 ask task 同时有 branch_session_id，会导致孤立 branch session。
        # How: 无论是否有 caller，都先 finalize branch（幂等，无 branch 时 no-op）。
        # Purpose: 避免 output chain 中 ask 路径遗留未收束的 branch session。
        self._finalize_branch_task_locked(task, merge=True)
        self._resume_caller_or_output_locked(task, {
            "type": "child_ask",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
            "action_type": "ask",
        }, result, action_type="ask")

    def _route_reject_to_source_locked(self, task: Task, result: dict[str, Any]) -> None:
        """Route reject from an output chain node back to the chain entry."""
        # [AutoC 2026-06-10] Why: reject 表示 output chain 下游拒绝了上游的输出。
        # How: 穿透回链入口 task，清理中间 waiter，用 output_rejected resume 恢复入口。
        # Purpose: 入口节点收到明确的「输出被拒绝」语义，而非模糊的 child_ask。
        entry_id = str(task.output_chain_entry_task_id or "").strip()
        entry = self.tasks.get(entry_id) if entry_id else None
        if entry is None:
            # 不在 output chain 中（不应发生），降级为 ask 路由
            return self._route_ask_to_caller_locked(task, result)
        reject_reason = str(result.get("text") or "").strip()
        # Try to extract finish tool_call_id from entry task's runtime state.
        # [AutoC 2026-06-10] Why: ai_step must format the reject as the matching
        # finish tool's result, especially in native mode. How: read the persisted
        # runtime tool_call_log from the suspended entry task and keep the latest
        # finish call_id. Purpose: output_rejected can preserve provider tool_call
        # pairing instead of creating an orphan tool result.
        _entry_result = entry.result if isinstance(entry.result, dict) else {}
        _rt_state = _entry_result.get("_runtime_state") if isinstance(_entry_result.get("_runtime_state"), dict) else {}
        _tool_log = _rt_state.get("tool_call_log") if isinstance(_rt_state.get("tool_call_log"), list) else []
        _finish_call_id = ""
        for _log_entry in reversed(_tool_log):
            if isinstance(_log_entry, dict) and _log_entry.get("name") == "finish":
                _finish_call_id = str(_log_entry.get("call_id") or "")
                break

        resume_data = {
            "type": "output_rejected",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
            "action_type": "reject",
            "reject_reason": reject_reason,
            "_finish_tool_call_id": _finish_call_id,
        }
        # 恢复入口 task 本身（不是入口的 caller），让入口节点重新执行。
        if entry.status == TaskStatus.suspended:
            # [AutoC 2026-06-10] Why: _complete_output_chain_waiters_locked marks
            # every suspended waiter up to and including entry as completed. How:
            # switch entry to pending before calling it, so only intermediate
            # output-chain waiters are completed. Purpose: reject can resume the
            # original entry task instead of completing it by accident.
            entry.status = TaskStatus.pending
            entry.waiting_for_task_id = None
            entry.worker_id = None
            entry.lease_expires_at = None
            entry.updated_at = _now()
            entry.input["resume_data"] = resume_data
            entry.input["_last_child_action_type"] = "reject"
            entry.input["context_ref"] = str(entry.result.get("context_ref") or entry.input.get("context_ref") or "")
            self._complete_output_chain_waiters_locked(final_task=task, entry_task=entry)
            self._event_task_snapshot("task_resumed", entry)
        else:
            # entry 不在 suspended 状态，降级为普通 fail
            self._finalize_branch_task_locked(task, merge=True)
            self._route_fail_locked(task, {"error": f"output chain reject but entry not suspended: {reject_reason}"})

    def _route_finish_to_caller_or_user_locked(self, task: Task, result: dict[str, Any]) -> None:
        """Preserve the old finish behavior for nodes without output declarations."""
        # [AutoC 2026-06-08] Why: output chain 必须向后兼容，没有 output 的节点不能改变行为。
        # How: 在旧式离开链路时才 finalize branch，再调用原有 caller/outbound 分发函数。
        # Purpose: 普通 finish、异步 dispatch 回调和系统任务继续使用原路由。
        self._finalize_branch_task_locked(task, merge=True)
        self._resume_caller_or_output_locked(
            task,
            self._output_chain_resume_data(task, result, action_type="finish"),
            result,
            action_type="finish",
        )

    def _route_fail_locked(self, task: Task, action: dict[str, Any]) -> None:
        error = str(action.get("error") or "未知错误").strip()
        # [2026-08-26] 失败落盘：错误同步写入 conversation JSONL（message_type=error）。
        # 此前错误只进 event log（outbound_message），刷新后历史加载时失败轮凭空
        # 消失，schedule 会话更是完全无人可见。写入与 outbound 同一路由 session，
        # 前端历史渲染为错误卡片。
        self._persist_fail_message_locked(task, action, error)
        self._resume_caller_or_output_locked(task, {
            "type": "child_failed",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "error": error,
        }, {"text": f"[错误] {error}"}, action_type="fail")

    def _persist_fail_message_locked(self, task: Task, action: dict[str, Any], error: str) -> None:
        """Append one system/error message to the route session's JSONL.

        崩溃安全：落盘失败只记日志，绝不阻断后续 fail 路由（outbound 输出）。
        """
        try:
            route_sid = self._route_session_id_for_task_locked(task)
            if not route_sid:
                return
            from datetime import datetime, timezone
            from pathlib import Path

            from engine.conversation_store import ConversationStore, Message

            store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            _meta: dict[str, Any] = {"task_id": task.task_id}
            _node_id = str(task.node_id or task.tool_name or "").strip()
            if _node_id:
                _meta["node_id"] = _node_id
            msg = Message(
                id=str(uuid.uuid4()),
                role="system",
                content=error[:4000],
                message_type="error",
                created_at=datetime.now(timezone.utc).isoformat(),
                meta=_meta,
                source_node_id=_node_id,
                source_task_id=task.task_id,
            )
            store.append(route_sid, msg)
        except Exception:
            log.exception("persist fail message failed: task=%s", str(task.task_id or "")[:12])

    # ------------------------------------------------------------------ #
    #  统一批量收集
    # ------------------------------------------------------------------ #

    def _try_complete_batch_locked(self, task: Task) -> None:
        """统一批量收集。不区分 tool/node，等同 batch_id 全部终态后打包恢复 caller。"""
        batch_id = task.batch_id
        if not batch_id:
            return

        # 找所有同 batch 的兄弟
        siblings: list[Task] = []
        for t in self.tasks.values():
            if t.batch_id == batch_id and t.session_id == task.session_id:
                siblings.append(t)
        if not siblings:
            return

        # 有任何一个还没结束，等着
        if any(not self._task_terminal(t) for t in siblings):
            return

        # 防止重复恢复（多个兄弟同时完成时可能多次进入这里）
        resume_key = f"batch:{batch_id}"
        for t in self.tasks.values():
            if t.session_id == task.session_id and str(t.input.get("_resume_key") or "") == resume_key:
                return

        # 按 batch_index 排序
        siblings.sort(key=lambda x: x.batch_index)

        # 收集结果
        entries: list[dict[str, Any]] = []
        for t in siblings:
            tr = t.result.get("result") if isinstance(t.result.get("result"), dict) else {}
            act = str(t.result.get("action") or "").strip()

            entry: dict[str, Any] = {
                "kind": t.kind.value,
                "status": act,
                "summary": str(t.result.get("summary") or tr.get("summary") or ""),
            }

            if t.kind == TaskKind.node:
                entry["node_id"] = str(t.node_id or "")
                entry["instruction"] = str(t.input.get("instruction") or "")
                entry["text"] = str(tr.get("text") or "")
            elif t.kind == TaskKind.tool:
                entry["name"] = str(t.tool_name or tr.get("tool_name") or "")
                entry["args"] = dict(tr.get("arguments") or t.input.get("arguments") or {})
                entry["format"] = str(tr.get("raw_format") or tr.get("format") or "json")
                entry["raw_inline"] = str(tr.get("raw_inline") or tr.get("text") or "")
                entry["truncated"] = bool(tr.get("truncated", False))
                entry["ref"] = str(tr.get("ref") or "")

            if act == "fail":
                entry["error"] = str(t.result.get("error") or "")

            atts = tr.get("attachments")
            if isinstance(atts, list):
                entry["attachments"] = atts

            entries.append(entry)

        # 构建摘要
        total = len(entries)
        fail_count = sum(1 for e in entries if e["status"] == "fail")
        summary_parts = [f"批量完成 ({total} 个)"]
        if fail_count:
            summary_parts.append(f"{fail_count} 个失败")
        summary_text = ", ".join(summary_parts)

        resume_data: dict[str, Any] = {
            "type": "batch_results",
            "entries": entries,
        }
        self._resume_caller_or_output_locked(
            task, resume_data, {"summary": summary_text},
        )

    # ------------------------------------------------------------------ #
    #  公共：恢复 caller 或输出给用户
    # ------------------------------------------------------------------ #

    def _resume_caller_or_output_locked(
        self,
        task: Task,
        resume_data: dict[str, Any],
        fallback_result: dict[str, Any],
        *,
        action_type: str = "",
    ) -> None:
        """尝试恢复 caller 节点。如果没有 caller，把 fallback_result 输出给用户。"""
        # 优先尝试直接唤醒 suspended 的 caller
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if caller and caller.status == TaskStatus.suspended:
                caller.status = TaskStatus.pending
                caller.waiting_for_task_id = None
                caller.worker_id = None
                caller.lease_expires_at = None
                caller.updated_at = _now()
                # 注入 resume_data 到 caller 的 input 中
                caller.input["resume_data"] = resume_data
                if action_type:
                    # [AutoC 2026-05-31] Why: ask and finish currently resume the
                    # caller through the same code path. How: copy action_type into
                    # resume_data for callers and also into input for cheap
                    # supervisor inspection. Purpose: preserve ask identity for
                    # upcoming topology routing without changing the payload shape.
                    caller.input["_last_child_action_type"] = str(action_type).strip()
                caller.input["context_ref"] = str(caller.result.get("context_ref") or caller.input.get("context_ref") or "")
                # 标记防重复恢复
                if task.batch_id:
                    caller.input["_resume_key"] = f"batch:{task.batch_id}"
                self._event_task_snapshot("task_resumed", caller)
                return

        # 没有 suspended caller
        # [2026-05-28] 新路径：通过 inbound dispatch_origin 回调。
        # 为什么：异步 dispatch 统一走 inbound 后，子任务完成时通过 _dispatch_origin
        #   中记录的 parent_session_id 将结果注入回调用方 session。
        # 怎么改：在旧 _async_dispatch 判断之前新增 _dispatch_origin 检查。
        # 目的：支持新路径的同时保持旧路径向后兼容。
        if task.input.get("_dispatch_origin"):
            self._inject_dispatch_result_via_origin_locked(task, fallback_result)
            # [2026-05-28] fresh/fork session 清理：conversation_key 带 uuid 的
            # dispatch session 完成后删除，避免资源积累。
            _d_ctx_mode = str(task.input.get("_dispatch_origin", {}).get("context_mode") or "").strip()
            if not _d_ctx_mode:
                _d_ctx_mode = str(task.input.get("dispatch_context_mode") or "").strip()
            if _d_ctx_mode in ("fresh", "fork"):
                self._cleanup_dispatch_session_locked(task)
            return

        # 旧路径（向后兼容）：异步 dispatch 子任务完成 → 注入 inbound 通知入口节点
        if task.input.get("_async_dispatch"):
            self._inject_async_dispatch_result_locked(task, fallback_result)
            return

        # 系统内部任务（如记忆提取）静默完成，不输出给用户
        if task.input.get("_system_task"):
            return
        if not isinstance(fallback_result, dict):
            fallback_result = {}
        text = str(fallback_result.get("text") or fallback_result.get("summary") or "").strip()
        atts = fallback_result.get("attachments") if isinstance(fallback_result.get("attachments"), list) else None
        # [Fix] finish(text="") 空文本时也产出 outbound_message 事件。
        # 原先 `if text or atts:` 导致空文本 finish 不发事件，
        # Bot 侧 trigger 和 status_msg（含 stream_delta 累积的原始标记预览）永远无法清理。
        # 去掉守卫后，Bot 侧 send_reply 会跳过 Discord 发送但正常执行清理收尾。
        self.append_outbound_message(
            session_id=self._route_session_id_for_task_locked(task), text=text,
            attachments=atts, source_inbound_seq=task.source_inbound_seq,
            node_id=task.node_id,
            action_type=action_type,
            llm_request_id=str(task.result.get("llm_request_id") or "").strip(),
            task_id=task.task_id,
        )


    # ------------------------------------------------------------------ #
    #  异步 dispatch 结果注入
    # ------------------------------------------------------------------ #

    def _inject_async_dispatch_result_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """异步 dispatch 子任务完成后，将结果作为新 inbound 注入 session。

        [Fork/Merge 2026-05-12] 不再自动 preempt running/suspended 入口 task。
        原因：preempt 只能由 adapter 显式 API 触发；内部结果注入应 fork 新分支并发处理。
        做法：始终走 inbound 创建路径。目的：避免内部事件破坏正在运行的入口分支。

        此方法在 self._lock 内调用，不可调用会再次获取 _lock 的公开方法。
        """
        # [AutoC 2026-06-04] Why: dispatch result payloads must be language-neutral
        # and should not store presentation text. How: keep the child finish text and
        # summary as raw fields, then emit caller/child ids as structured metadata.
        # Purpose: the LLM and each client can render their own localized wrapper
        # without parsing or persisting backend-generated Chinese prose.
        result_text = str(fallback_result.get("text") or "").strip()
        result_summary = str(fallback_result.get("summary") or "").strip()
        caller_node = str(task.input.get("_caller_node_id") or "").strip()
        # [AutoC 2026-06-03] Why: the web callback card needs a stable target for
        # child-session navigation. How: read the child session created for the
        # completed dispatch task from task.input. Purpose: clients do not need to
        # infer the child transcript from localized notification text.
        child_session_id = str(task.input.get("child_session_id") or "").strip()

        result_atts = (
            fallback_result.get("attachments")
            if isinstance(fallback_result.get("attachments"), list)
            else None
        )

        # [Fork/Merge 2026-05-17] Why: async dispatch tasks may originate from a
        # branch, but their completion should inject a new inbound into the parent
        # conversation. How: use the same route helper as outbound/hook paths.
        # Purpose: async results do not target a deleted branch session.
        route_session_id = self._route_session_id_for_task_locked(task)
        session_info = self.sessions.get(route_session_id)
        if not session_info:
            return

        # 检查 session generation 是否过期
        current_gen = self._current_session_generation_locked(route_session_id)
        if task.session_generation and current_gen and task.session_generation != current_gen:
            return

        # 检查 session 是否已被 cancel
        if route_session_id in self._cancelled_sessions:
            return

        # [Fork/Merge 2026-05-12] 异步 dispatch 结果不再自动 preempt 入口 task。
        # 原因：新架构规定 preempt 只能由 adapter 显式调用 preempt API 触发，普通 inbound
        # 或内部注入都应通过 fork 分支并发处理。做法：移除 running/suspended 入口任务的
        # 自动 preempt 分支，直接创建 inbound。目的：同一 session 下多个入口分支可并行运行。

        # [AutoC 2026-07-05] 无 branch 模式下优先 preempt running task，避免并发写同一 ConversationStore
        _dispatch_preempt_text = result_text
        if result_summary:
            _dispatch_preempt_text = f"{result_summary}\n{result_text}" if result_text else result_summary
        branchless_task = self._is_branchless_running_task_locked(route_session_id)
        if branchless_task is not None:
            branchless_task.preempt_requested = True
            branchless_task.preempt_message = _dispatch_preempt_text
            branchless_task.preempt_attachments = list(result_atts) if result_atts else []
            self.eventlog.append(
                session_id=route_session_id,
                component="supervisor",
                type_="preempt_requested",
                payload={
                    "task_id": branchless_task.task_id,
                    "session_id": route_session_id,
                    "has_message": bool(_dispatch_preempt_text),
                    "source": "async_dispatch_result",
                    "child_task_id": task.task_id,
                    "child_node_id": task.node_id,
                },
                transient=True,
            )
            return

        # ---- branch 模式或无 running task：创建 inbound → 新 branch task ----
        conv_key = session_info.conversation_key
        channel = session_info.channel
        msg_id = f"async_dispatch:{task.task_id}"

        payload: dict[str, Any] = {
            "channel": channel,
            "conversation_key": conv_key,
            "message_id": msg_id,
            "text": result_text,
            "summary": result_summary,
            "message_type": "dispatch_result",
            "caller_node_id": caller_node,
            "child_node_id": task.node_id,
            "child_task_id": task.task_id,
            "child_session_id": child_session_id,
        }
        if result_atts:
            payload["attachments"] = result_atts

        evt = self.eventlog.append(
            session_id=route_session_id,
            component="supervisor",
            type_="inbound_message",
            payload=payload,
        )
        seq = int(evt.get("seq", 0))
        self._apply_inbound_message(seq=seq, session_id=route_session_id, payload=payload)
        self._advance_inbound_cursor()
        self._create_entry_task_for_inbound_locked(
            inbound_seq=seq, session_id=route_session_id, payload=payload,
        )

    # ------------------------------------------------------------------ #
    #  [2026-05-28] 新路径：通过 dispatch_origin 回调结果
    # ------------------------------------------------------------------ #

    def _inject_dispatch_result_via_origin_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """dispatch_origin 路径的回调注入。

        [2026-05-28] 异步 dispatch 统一走 inbound 后，子任务完成时通过
        task.input["_dispatch_origin"] 中记录的 parent_session_id 将结果
        作为新 inbound 注入回调用方 session。

        与 _inject_async_dispatch_result_locked 逻辑基本一致，区别在于：
        - 从 task.input["_dispatch_origin"] 读取回调目标
        - 目标是 dispatch_origin.parent_session_id（不是 task 自身的 route session）

        此方法在 self._lock 内调用。
        """
        origin = task.input.get("_dispatch_origin") or {}
        target_session_id = str(origin.get("parent_session_id") or "").strip()
        caller_node = str(origin.get("caller_node_id") or "").strip()
        # [AutoC 2026-06-03] Why: dispatch_origin callbacks use the same visible
        # callback card as legacy async dispatch. How: keep the child session id from
        # the completed child task input. Purpose: both callback paths support the
        # same structured web jump button.
        child_session_id = str(task.input.get("child_session_id") or "").strip()

        if not target_session_id:
            log.warning(
                "dispatch_origin callback skipped: no parent_session_id in task %s",
                task.task_id[:12],
            )
            return

        session_info = self.sessions.get(target_session_id)
        if not session_info:
            log.warning(
                "dispatch_origin callback skipped: session %s not found for task %s",
                target_session_id[:12], task.task_id[:12],
            )
            return

        # 检查 session generation 是否过期
        current_gen = self._current_session_generation_locked(target_session_id)
        if task.session_generation and current_gen and task.session_generation != current_gen:
            return
        # 检查 session 是否已被 cancel
        if target_session_id in self._cancelled_sessions:
            return

        # [AutoC 2026-06-04] Why: dispatch_origin callbacks share the new
        # language-neutral dispatch_result contract. How: keep raw child result text
        # and summary separate from caller/child ids. Purpose: this path cannot persist
        # backend-localized notification text while the legacy path has been cleaned.
        result_text = str(fallback_result.get("text") or "").strip()
        result_summary = str(fallback_result.get("summary") or "").strip()

        result_atts = (
            fallback_result.get("attachments")
            if isinstance(fallback_result.get("attachments"), list)
            else None
        )

        # [AutoC 2026-07-05] 无 branch 模式下优先 preempt running task
        _origin_preempt_text = result_text
        if result_summary:
            _origin_preempt_text = f"{result_summary}\n{result_text}" if result_text else result_summary
        branchless_task = self._is_branchless_running_task_locked(target_session_id)
        if branchless_task is not None:
            branchless_task.preempt_requested = True
            branchless_task.preempt_message = _origin_preempt_text
            branchless_task.preempt_attachments = list(result_atts) if result_atts else []
            self.eventlog.append(
                session_id=target_session_id,
                component="supervisor",
                type_="preempt_requested",
                payload={
                    "task_id": branchless_task.task_id,
                    "session_id": target_session_id,
                    "has_message": bool(_origin_preempt_text),
                    "source": "dispatch_origin_result",
                    "child_task_id": task.task_id,
                    "child_node_id": task.node_id,
                },
                transient=True,
            )
            return

        # ---- branch 模式或无 running task：构造 inbound payload 并注入目标 session ----
        conv_key = session_info.conversation_key
        channel = session_info.channel
        msg_id = f"dispatch_origin:{task.task_id}"

        payload: dict[str, Any] = {
            "channel": channel,
            "conversation_key": conv_key,
            "message_id": msg_id,
            "text": result_text,
            "summary": result_summary,
            "message_type": "dispatch_result",
            "caller_node_id": caller_node,
            "child_node_id": task.node_id,
            "child_task_id": task.task_id,
            "child_session_id": child_session_id,
        }
        if result_atts:
            payload["attachments"] = result_atts

        evt = self.eventlog.append(
            session_id=target_session_id,
            component="supervisor",
            type_="inbound_message",
            payload=payload,
        )
        seq = int(evt.get("seq", 0))
        self._apply_inbound_message(seq=seq, session_id=target_session_id, payload=payload)
        self._advance_inbound_cursor()
        self._create_entry_task_for_inbound_locked(
            inbound_seq=seq, session_id=target_session_id, payload=payload,
        )

    def _cleanup_dispatch_session_locked(self, task: Task) -> None:
        """清理 fresh/fork 模式 dispatch 创建的临时 session。

        [2026-05-28] fresh/fork dispatch 的 conversation_key 带 uuid，
        完成后不会被复用。清理对应的 session 和 JSONL，释放资源。
        """
        # task 运行在 entry branch 上，需要找到其 parent session
        _parent_sid = str(task.input.get("parent_session_id") or "").strip()
        _branch_sid = str(task.input.get("branch_session_id") or "").strip()
        # dispatch session 是 branch 的 parent
        _dispatch_sid = _parent_sid or ""
        if not _dispatch_sid:
            return
        _info = self.sessions.get(_dispatch_sid)
        if not _info:
            return
        # 只清理 agent: 前缀的 dispatch session
        if not _info.conversation_key.startswith("agent:"):
            return
        try:
            _conv_dir = self.workspace_root / "data" / "conversations"
            _jsonl = _conv_dir / f"{_dispatch_sid}.jsonl"
            if _jsonl.exists():
                _jsonl.unlink()
            # 清理 branch JSONL（如果还在）
            if _branch_sid:
                _branch_jsonl = _conv_dir / f"{_branch_sid}.jsonl"
                if _branch_jsonl.exists():
                    _branch_jsonl.unlink()
            # 从内存中移除
            self.conversation_map.pop(_info.conversation_key, None)
            self.sessions.pop(_dispatch_sid, None)
            self.session_generations.pop(_dispatch_sid, None)
            self._cancelled_sessions.discard(_dispatch_sid)
            self._purge_context_usage_for_session(_dispatch_sid)
            # [AutoC 2026-05-30] Why: fresh/fork dispatch session 完成后是临时
            # 会话，不应在 sessions.json 中留下 reset=true 记录。How: 改为物理删除
            # registry 条目。Purpose: 阻止 agent:* 派生 session 持续堆积。
            self._session_store.remove_session(_dispatch_sid)
        except Exception as e:
            log.warning("cleanup dispatch session %s failed: %s", _dispatch_sid[:12], e)

    def _is_branchless_running_task_locked(self, session_id: str) -> Task | None:
        """查找直接运行在主 session 上的 running entry task（无 branch 模式）。

        [AutoC 2026-07-05] Why: 无 branch 模式下 task 直接跑在主 session 上，
        异步回调如果再创建 inbound 会导致两个 task 并发写同一个 ConversationStore。
        How: 查 running entry task 且 parent_session_id 为空（无 branch 标志）。
        Purpose: 回调路径据此选择 preempt 而非 inbound。
        """
        task = self._find_running_entry_task_locked(session_id)
        if task is None:
            return None
        # 有 branch 元数据 → 说明是 branch 模式，不需要 preempt
        if str(task.input.get("parent_session_id") or "").strip():
            return None
        return task

    def inject_async_result(self, session_id: str, text: str, attachments: list | None = None, source_task_id: str = "") -> dict[str, Any]:
        """注入异步工具结果到 session。

        [AutoC 2026-07-05] 优先按 source_task_id 精确定位发起异步工具的 task：
        - task_id 匹配且 running → preempt 该 task（无论 branch 还是主 session）
        - 无 branch 模式下按 session 查找 running entry task → preempt
        - 否则 → 创建 inbound（走 branch fork）
        """
        with self._lock:
            session_info = self.sessions.get(session_id)
            if not session_info:
                return {"ok": False, "error": "session not found"}

            result_atts = list(attachments or [])

            # ---- 策略 1：按 task_id 精确查找（覆盖 branch 模式） ----
            target_task: Task | None = None
            if source_task_id:
                t = self.tasks.get(source_task_id)
                if t is not None and t.status == TaskStatus.running:
                    target_task = t

            # ---- 策略 2：无 branch 模式下按 session 查找 ----
            if target_task is None:
                target_task = self._is_branchless_running_task_locked(session_id)

            if target_task is not None:
                target_task.preempt_requested = True
                target_task.preempt_message = text
                target_task.preempt_attachments = [
                    {"path": p} for p in result_atts
                ] if result_atts else []
                self.eventlog.append(
                    session_id=session_id,
                    component="supervisor",
                    type_="preempt_requested",
                    payload={
                        "task_id": target_task.task_id,
                        "session_id": session_id,
                        "has_message": bool(text),
                        "source": "async_tool_result",
                    },
                    transient=True,
                )
                return {"ok": True, "strategy": "preempt", "task_id": target_task.task_id}

            # ---- 策略 3：创建 inbound（branch 模式且原 task 已结束） ----
            conv_key = session_info.conversation_key
            channel = session_info.channel
            msg_id = f"async_tool_result:{session_id}"

            payload: dict[str, Any] = {
                "channel": channel,
                "conversation_key": conv_key,
                "message_id": msg_id,
                "text": text,
            }
            if result_atts:
                payload["attachments"] = [{"path": p} for p in result_atts]

            evt = self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="inbound_message",
                payload=payload,
            )
            seq = int(evt.get("seq", 0))
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()
            self._create_entry_task_for_inbound_locked(
                inbound_seq=seq, session_id=session_id, payload=payload,
            )
            return {"ok": True, "strategy": "inbound"}

    def _find_entry_task_by_status_locked(self, session_id: str, statuses: set) -> Task | None:
        """在 session 中查找指定状态的入口 task。

        入口 task 定义：无 caller、非异步 dispatch 子任务、非系统任务。
        若存在多个符合条件的 task（理论上不应发生），返回最早创建的。
        """
        candidate: Task | None = None
        for t in self.tasks.values():
            if t.session_id != session_id:
                continue
            if t.status not in statuses:
                continue
            # 排除：有 caller 的子任务
            if t.caller_task_id:
                continue
            # 排除：异步 dispatch 子任务自身（旧路径）
            if t.input.get("_async_dispatch"):
                continue
            # [2026-05-28] 排除：通过 inbound dispatch_origin 创建的子任务。
            # 为什么：这类任务是异步委派的执行体，不应被视为用户入口 task。
            if t.input.get("_dispatch_origin"):
                continue
            # 排除：系统内部任务
            if t.input.get("_system_task"):
                continue
            if candidate is None or t.created_at < candidate.created_at:
                candidate = t
        return candidate

    def _find_running_entry_task_locked(self, session_id: str) -> Task | None:
        """在 session 中查找 running 状态的入口 task。"""
        return self._find_entry_task_by_status_locked(session_id, {TaskStatus.running})

    def _find_suspended_entry_task_locked(self, session_id: str) -> Task | None:
        """在 session 中查找 suspended 状态的入口 task。"""
        return self._find_entry_task_by_status_locked(session_id, {TaskStatus.suspended})

    # ------------------------------------------------------------------ #
    #  child session 清理
    # ------------------------------------------------------------------ #

    def _cleanup_task_child_session_if_needed(self, task: Task) -> None:
        """统一清理 task 关联的 child session（非 accumulate 模式）。

        [AutoC 2026-05-30] Why: child session 清理逻辑原先散落在 10+ 个
        return 路径中，每新增路径都要记得补清理，容易遗漏导致 sessions.json
        堆积。How: 在 _route_completed_task_locked 的 finally 中统一调用。
        Purpose: 单一出口，防止泄漏。
        """
        child_sid = str(task.input.get("child_session_id") or "").strip()
        if not child_sid:
            return

        # [AutoC 2026-05-30] Why: action=dispatch 会把 task 改成 suspended，
        # 该 caller 后续还要继续使用自己的 child session。How: 只处理终态
        # task。Purpose: 避免 finally 在挂起路径误删仍活跃的 child session。
        if not self._task_terminal(task):
            return

        # [AutoC 2026-05-30] Why: task.input 中的 context_mode 可能为空或滞后，
        # sessions.json registry 才是 child session 的持久来源。How: 优先读取
        # registry，缺失时才回退 task.input。Purpose: 避免误删 accumulate 会话。
        entry = self._session_store._registry.get(child_sid)
        registry_mode = str(entry.get("context_mode") or "").strip() if isinstance(entry, dict) else ""
        input_mode = str(task.input.get("context_mode") or "").strip()
        ctx_mode = registry_mode or input_mode

        if ctx_mode == "accumulate":
            return

        self._expire_child_session(child_sid)

    # ------------------------------------------------------------------ #
    #  压缩 dispatch 结果处理
    # ------------------------------------------------------------------ #

    def _is_compact_dispatch_result(self, task: Task) -> bool:
        """判断是否是 compactor 子 task 的完成事件。"""
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if not caller:
                return False
            return bool(caller.input.get("_compact_dispatch_pending"))

        # [2026-06-06] Why: manual API compact now creates a standalone
        # system.compactor task without a suspended caller. How: recognize the
        # explicit apply target placed on task.input by the API endpoint. Purpose:
        # standalone compact results are routed to ConversationStore instead of
        # falling through normal finish handling.
        apply_to_session_id = str(task.input.get("_compact_apply_to_session_id") or "").strip()
        return bool(apply_to_session_id and str(task.node_id or "") == "system.compactor")

    def _apply_standalone_compact_result_locked(self, task: Task, target_session_id: str) -> None:
        """Apply an API-created compactor task directly to ConversationStore."""
        from engine.compact import (
            _format_compact_summary,
            record_compact_failure,
            record_compact_success,
        )

        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "node_id": str(task.node_id or ""),
            "success": False,
        }
        try:
            keep_recent = int(task.input.get("_compact_keep_recent", 6))
        except (TypeError, ValueError):
            keep_recent = 6
        try:
            threshold_tokens = int(task.input.get("_compact_threshold_tokens", 0) or 256000)
        except (TypeError, ValueError):
            threshold_tokens = 256000

        try:
            act = str((task.result or {}).get("action") or "").strip()
            if act == "finish":
                result = (task.result or {}).get("result") or {}
                raw_summary = str(result.get("text") or "").strip()
                summary = _format_compact_summary(raw_summary)

                if summary:
                    # [2026-06-06] Why: standalone compact has no parent task to
                    # resume, so task_router must apply the accepted summary here.
                    # How: use the existing ConversationStore compaction helper with
                    # the target session carried by task.input. Purpose: API compact
                    # writes back through the same durable store path as dispatch compact.
                    cr = self._apply_compact_via_conv_store_locked(
                        target_session_id,
                        summary,
                        keep_recent=keep_recent,
                        threshold_tokens=threshold_tokens,
                    )
                    payload.update(cr)
                    payload["success"] = cr["after"] < cr["before"]
                    record_compact_success(target_session_id)
                    self.eventlog.append(
                        session_id=target_session_id,
                        component="supervisor",
                        type_="compact_done",
                        payload=payload,
                    )
                    return

                record_compact_failure(target_session_id)
                emergency_summary = "[上下文压缩摘要生成失败，旧消息已被裁剪以恢复服务]"
                try:
                    # [2026-06-06] Why: invalid standalone summaries would otherwise
                    # leave the API compact endpoint in the same retry loop as the
                    # automatic path. How: reuse the existing emergency truncation
                    # strategy with a fixed fallback summary. Purpose: old segments
                    # can still be removed when the LLM output is unusable.
                    cr = self._apply_compact_via_conv_store_locked(
                        target_session_id,
                        emergency_summary,
                        keep_recent=keep_recent,
                        threshold_tokens=threshold_tokens,
                    )
                    payload.update(cr)
                    payload["success"] = cr["after"] < cr["before"]
                    if payload["success"]:
                        record_compact_success(target_session_id)
                        self.eventlog.append(
                            session_id=target_session_id,
                            component="supervisor",
                            type_="compact_done",
                            payload=payload,
                        )
                        return
                except Exception as exc:
                    payload["error"] = str(exc)
                    log.warning("standalone compact emergency truncation failed: %s", exc)
            else:
                payload["error"] = f"unexpected compactor action: {act or 'empty'}"
        except Exception as exc:
            record_compact_failure(target_session_id)
            payload["error"] = str(exc)
            log.warning("standalone compact result apply failed: %s", exc)

        self.eventlog.append(
            session_id=target_session_id,
            component="supervisor",
            type_="compact_failed",
            payload=payload,
        )

    def _apply_compact_result_locked(self, task: Task) -> None:
        """compactor 完成后，对父 task 的上下文执行压缩并恢复父 task。

        Step 2（2026-04-16）：支持两条路径。
        - ConversationStore 路径：caller 是主节点（flag 开启）或子节点（有 child_session_id），
          直接操作 data/conversations/{target}.jsonl，用 summary 消息替换中间部分。
        - 旧 snapshot 路径：flag 关闭时的主节点，沿用 caller.input.context_ref 读写。
        """
        from engine.compact import _format_compact_summary, apply_compact_summary
        from engine.context_store import load_context_snapshot, write_context_snapshot

        caller_id = task.caller_task_id
        caller = self.tasks.get(caller_id) if caller_id else None
        standalone_target_session_id = str(task.input.get("_compact_apply_to_session_id") or "").strip()
        if not caller and standalone_target_session_id:
            self._apply_standalone_compact_result_locked(task, standalone_target_session_id)
            return
        if not caller or caller.status != TaskStatus.suspended:
            return

        act = str((task.result or {}).get("action") or "").strip()
        parent_ctx_ref = str(
            caller.result.get("context_ref")
            or caller.input.get("context_ref")
            or ""
        ).strip()

        # ---- 选择压缩路径 ----
        # 1. caller 提供 _compact_target_session_id / parent_session_id → ConversationStore（父 session）
        # 2. caller 无 parent 但 main_session_enabled → ConversationStore（当前 session）
        # 3. 否则 → 旧 snapshot 路径
        from clonoth_runtime import load_runtime_config
        _rc = load_runtime_config(self.workspace_root)
        _main_conv_enabled = bool(
            _rc.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)
        )
        # [AutoC 2026-05-13] Why: child_session_id and branch session ids are
        # temporary copies, so compacting them does not shrink the parent history.
        # How: prefer the explicit target saved during dispatch, then the caller's
        # parent_session_id, and only then the caller session. Purpose: supervisor
        # result application matches engine-side parent-first target selection.
        target_sid_for_conv = str(
            caller.input.get("_compact_target_session_id")
            or caller.input.get("target_session_id")
            or caller.input.get("parent_session_id")
            or ""
        ).strip()
        if not target_sid_for_conv and _main_conv_enabled:
            # Step 2 修复：main_session_enabled=true 时无条件走 ConvStore 路径。
            # 原条件 `and not parent_ctx_ref` 会被 _persist_ctx 写入的 context_ref 拦住，
            # 导致 compact 走旧 snapshot 路径，而 runner.py 已改为从 JSONL 读取——读写不一致。
            target_sid_for_conv = caller.session_id

        if act == "finish":
            result = (task.result or {}).get("result") or {}
            raw_summary = str(result.get("text") or "").strip()
            summary = _format_compact_summary(raw_summary)

            # --- Circuit breaker + emergency truncation ---
            # If summary was rejected (empty), record failure for circuit breaker
            # AND perform emergency segment truncation to break the loop.
            if not summary and target_sid_for_conv:
                from engine.compact import record_compact_failure
                _cb_sid = target_sid_for_conv
                record_compact_failure(_cb_sid)
                # Emergency truncation: even without a valid summary, force-remove
                # old segments to break the compact loop.
                _keep = int(caller.input.get("_compact_keep_recent", 6))
                _threshold = int(caller.input.get("_compact_threshold_tokens", 0) or 256000)
                _emergency_summary = (
                    "[上下文压缩摘要生成失败，旧消息已被裁剪以恢复服务]"
                )
                try:
                    cr = self._apply_compact_via_conv_store_locked(
                        target_sid_for_conv, _emergency_summary,
                        keep_recent=_keep,
                        threshold_tokens=_threshold,
                    )
                    if cr["after"] < cr["before"]:
                        log.warning(
                            "compact: emergency truncation applied (summary rejected), "
                            "before=%d after=%d", cr["before"], cr["after"],
                        )
                        self._resume_compact_parent_locked(
                            caller, parent_ctx_ref,
                            compact_result=cr, success=True,
                        )
                        return
                except Exception as _et:
                    log.warning("compact: emergency truncation failed: %s", _et)
                # If emergency truncation didn't help, fall through to failure path

            if summary and target_sid_for_conv:
                # ---- ConversationStore 路径 ----
                cr = self._apply_compact_via_conv_store_locked(
                    target_sid_for_conv, summary,
                    keep_recent=int(caller.input.get("_compact_keep_recent", 6)),
                    threshold_tokens=int(caller.input.get("_compact_threshold_tokens", 0) or 256000),
                )
                if cr["after"] < cr["before"]:
                    self._resume_compact_parent_locked(
                        caller, parent_ctx_ref,
                        compact_result=cr, success=True,
                    )
                    return
                # before == after：消息太少未压缩，静默恢复
                self._resume_compact_parent_locked(
                    caller, parent_ctx_ref,
                    compact_result=cr, success=False,
                )
                return

            if summary and parent_ctx_ref:
                # ---- 旧 snapshot 路径（flag 关闭时的主节点）----
                snapshot = load_context_snapshot(self.workspace_root, parent_ctx_ref)
                if snapshot and isinstance(snapshot.get("messages"), list):
                    old_messages = snapshot["messages"]
                    keep_recent = int(caller.input.get("_compact_keep_recent", 6))
                    compressed = apply_compact_summary(
                        old_messages, summary, keep_recent=keep_recent,
                        threshold_tokens=int(caller.input.get("_compact_threshold_tokens", 0) or 256000),
                    )
                    if len(compressed) < len(old_messages):
                        snapshot["messages"] = compressed
                        write_context_snapshot(
                            self.workspace_root, parent_ctx_ref, snapshot,
                        )
                        self._resume_compact_parent_locked(
                            caller, parent_ctx_ref,
                            before=len(old_messages),
                            after=len(compressed),
                            success=True,
                        )
                        return

        # 失败路径：记录错误、发通知、恢复父 task
        _fail_sid = target_sid_for_conv or caller.session_id
        _fail_act = act or "unknown"
        log.warning(
            "compact failed for session %s: compactor returned action=%s",
            _fail_sid, _fail_act,
        )
        from engine.compact import record_compact_failure as _rcf
        _rcf(_fail_sid)
        self.eventlog.append(
            session_id=_fail_sid,
            component="supervisor",
            type_="system_notice",
            payload={
                "level": "warning",
                "title": "上下文压缩失败",
                "text": f"压缩节点返回 {_fail_act}，将在下次对话时重试。",
                "subsystem": "compact",
            },
        )
        self._resume_compact_parent_locked(
            caller, parent_ctx_ref, success=False,
        )

    def _apply_compact_via_conv_store_locked(
        self, target_session_id: str, summary: str, *, keep_recent: int,
        threshold_tokens: int = 256000,
    ) -> dict[str, int]:
        """Step 2（2026-04-16）：在 ConversationStore 层面执行压缩。

        读取 target session 的 JSONL，用 summary 消息替换旧 task segment，保留
        最近 keep_recent 个完整 task segment。与 engine.compact.apply_compact_summary
        的策略一致，但直接操作 Message 对象而非 dict。

        注意 ConversationStore 里没有 system 消息（system prompt 每轮由 ai_step
        的 assemble_initial_messages 重建），所以无需处理 prefix/inner system。

        Returns: dict with keys: before, after, total_segments, kept_segments, compressed_segments
        """
        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import ConversationStore, Message, MessageType

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        msgs = store.load(target_session_id)
        before = len(msgs)

        # [AutoC 2026-05-13] Why: ConversationStore stores Message objects, while
        # engine.compact.apply_compact_summary works on dict history. How: read
        # task ids through Message attributes and meta, then split only when the
        # consecutive source_task_id changes. Purpose: keep recent task segments
        # complete instead of slicing by raw message count.
        # [2026-05-17] Split messages into task segments, but treat old
        # compact_summary segments as "prefix" rather than a real task segment.
        # Old summaries should be replaced alongside other old segments, not
        # counted toward keep_recent (which would cause an infinite loop where
        # the old summary is deleted and recreated each cycle with no net change).
        prefix_msgs: list[Any] = []  # old compact_summary messages
        segments: list[list[Any]] = []
        cur_seg: list[Any] = []
        cur_tid: str = ""
        for msg in msgs:
            meta = msg.meta if isinstance(getattr(msg, "meta", None), dict) else {}
            tid = str(meta.get("source_task_id", "") or getattr(msg, "source_task_id", "") or "")
            if tid == "compact_summary":
                # [2026-05-17] Why: an old compact_summary is compressed prefix
                # material, not a task boundary. How: collect it for replacement
                # and keep the current real segment open. Purpose: the old summary
                # is fed into the next summary and never consumes keep_recent.
                prefix_msgs.append(msg)
                continue
            if tid != cur_tid and cur_seg:
                segments.append(cur_seg)
                cur_seg = []
            cur_tid = tid
            cur_seg.append(msg)
        if cur_seg:
            segments.append(cur_seg)

        # keep_recent applies to real task segments only (prefix excluded)
        keep_recent = max(keep_recent, 1)
        if len(segments) <= keep_recent:
            return {"before": before, "after": before, "total_segments": len(segments), "kept_segments": len(segments), "compressed_segments": 0}

        kept_segments = segments[-keep_recent:]
        to_remove_segments = segments[:-keep_recent]

        to_keep: list[Any] = []
        for seg in kept_segments:
            to_keep.extend(seg)

        to_remove: list[Any] = list(prefix_msgs)  # old summaries are always replaced
        for seg in to_remove_segments:
            to_remove.extend(seg)

        # P6.5 Metadata Preservation: 收集被压缩掉的消息所属的 source_task_id，
        # 存入 summary 消息的 meta 中。L2 snip_history 据此判断哪些 task 已被
        # LLM 压缩过，避免因 ID 丢失而反复 fall through 到 LLM compact。
        # [2026-04-26] 累积继承：旧 compact_summary 被再次压缩时，
        # 继承其 meta.compressed_task_ids，防止历史 ID 丢失。
        # [AutoC 2026-05-13] Why: metadata must describe removed task segments,
        # not an arbitrary message prefix. How: iterate over to_remove from the
        # segment split above and access Message fields by attribute. Purpose:
        # keep compressed_task_ids accurate after task-granular retention.
        _ctid_set: set[str] = set()
        for _rm in to_remove:
            _rm_meta = _rm.meta if isinstance(getattr(_rm, "meta", None), dict) else {}
            # [2026-05-17] Why: compact_summary is not a real task id and should
            # not be recorded as a compressed task. How: inherit its historical
            # compressed_task_ids but skip the compact_summary source id itself.
            # Purpose: L2 snip metadata remains about real tasks only.
            _rm_tid = str(_rm_meta.get("source_task_id", "") or getattr(_rm, "source_task_id", "") or "")
            if _rm_tid and _rm_tid != "compact_summary":
                _ctid_set.add(_rm_tid)
            _old_ctids = _rm_meta.get("compressed_task_ids")
            if isinstance(_old_ctids, list):
                for _ctid in _old_ctids:
                    if _ctid:
                        _ctid_set.add(str(_ctid))
        compressed_task_ids = list(_ctid_set)

        summary_msg = Message(
            id=str(uuid4()),
            role="user",
            content="[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n" + summary,
            message_type=MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_task_id="compact_summary",
            meta={"compressed_task_ids": compressed_task_ids},
        )
        new_messages = [summary_msg] + to_keep

        # --- Progressive keep_recent reduction ---
        # If the compressed result still exceeds the token threshold,
        # drop older kept segments one at a time until we fit or reach 0.
        # Estimate tokens as total chars / 3 (mixed CJK/English).
        _threshold = threshold_tokens
        try:
            # Build a rough char-based check: sum content lengths
            _total_chars = sum(
                len(getattr(m, 'content', '') or '')
                for m in new_messages
            )
            while keep_recent > 0 and _total_chars // 3 > _threshold:
                keep_recent -= 1
                if keep_recent > 0:
                    kept_segments = segments[-keep_recent:]
                else:
                    kept_segments = []
                to_keep = []
                for seg in kept_segments:
                    to_keep.extend(seg)
                new_messages = [summary_msg] + to_keep
                _total_chars = sum(
                    len(getattr(m, 'content', '') or '')
                    for m in new_messages
                )
                log.info(
                    "conv_store compact: still over threshold, reduced keep_recent to %d",
                    keep_recent,
                )
        except Exception:
            pass  # safety net: never break compact flow

        after = len(new_messages)
        result = {
            "before": before, "after": after,
            "total_segments": len(segments),
            "kept_segments": len(kept_segments),
            "compressed_segments": len(segments) - len(kept_segments),
        }
        if after >= before:
            # [2026-05-17] Why: callers treat after==before as an unsuccessful
            # compact, so persisting a same-length replacement can mutate durable
            # history while the engine still believes compact failed. How: return
            # counts without replace_all unless the new message list is shorter.
            # Purpose: avoid no-op rewrites and prevent compact retry loops.
            return result

        store.replace_all(target_session_id, new_messages)
        # [AutoC 2026-05-13] Why: active branches forked before parent compact
        # keep their own old prefix and would otherwise resume or merge from an
        # uncompressed copy. How: after the parent rewrite succeeds, mirror a
        # simplified prefix compaction into active branch sessions. Purpose:
        # branch histories stay bounded while preserving branch-local tails.
        self._sync_compact_to_branches(target_session_id, summary_msg, to_keep)
        return result

    def _clone_compact_summary_message(self, summary_msg: Any) -> Any:
        """Clone a compact summary message for a branch session."""
        # [AutoC 2026-05-13] Why: parent and branch JSONL files should not share
        # the same Message id even when they carry the same summary content. How:
        # create a new Message with copied content and metadata. Purpose: future
        # message-id based matching can distinguish parent and branch records.
        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import Message, MessageType

        return Message(
            id=str(uuid4()),
            role=getattr(summary_msg, "role", "user") or "user",
            content=str(getattr(summary_msg, "content", "") or ""),
            message_type=getattr(summary_msg, "message_type", "") or MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=dict(getattr(summary_msg, "meta", {}) or {}),
            source_node_id=getattr(summary_msg, "source_node_id", "") or "",
            source_task_id=getattr(summary_msg, "source_task_id", "") or "compact_summary",
        )

    def _sync_compact_to_branches(
        self, target_session_id: str, summary_msg: Any, to_keep: list[Any],
    ) -> None:
        """Synchronize a successful parent compact into active branch sessions."""
        # [AutoC 2026-05-13] Why: already-forked branch sessions keep the old
        # uncompressed parent prefix after the parent JSONL is compacted. How:
        # find active entry branches for the parent and compact only their forked
        # prefix, while separately compacting ordinary active child sessions under
        # the same parent. Purpose: keep branch stores small without dropping
        # branch-local messages that still need to merge back.
        from engine.conversation_store import ConversationStore

        target = str(target_session_id or "").strip()
        if not target:
            return
        keep_recent = len(to_keep)
        store = ConversationStore(self.workspace_root / "data" / "conversations")

        entry_branches: dict[str, dict[str, Any]] = {}
        child_sessions: set[str] = set()
        for task in list(self.tasks.values()):
            if self._task_terminal(task):
                continue
            parent_sid = str(task.input.get("parent_session_id") or "").strip()
            branch_sid = str(task.input.get("branch_session_id") or "").strip()
            if parent_sid == target and branch_sid:
                info = entry_branches.setdefault(branch_sid, {"tasks": [], "base_count": 0})
                info["tasks"].append(task)
                try:
                    base_count = int(task.input.get("base_count") or 0)
                except Exception:
                    base_count = 0
                if base_count > int(info.get("base_count") or 0):
                    info["base_count"] = base_count

            child_sid = str(task.input.get("child_session_id") or "").strip()
            if task.session_id == target and child_sid:
                child_sessions.add(child_sid)

        for branch_sid, info in entry_branches.items():
            self._sync_entry_branch_compact_locked(
                store,
                branch_sid,
                summary_msg,
                keep_recent=keep_recent,
                base_count=int(info.get("base_count") or 0),
                tasks=list(info.get("tasks") or []),
            )

        for child_sid in child_sessions:
            self._sync_child_session_compact_locked(
                store,
                child_sid,
                summary_msg,
                keep_recent=keep_recent,
            )

    def _sync_entry_branch_compact_locked(
        self,
        store: Any,
        branch_session_id: str,
        summary_msg: Any,
        *,
        keep_recent: int,
        base_count: int,
        tasks: list[Task],
    ) -> None:
        """Compact the forked parent prefix of one active entry branch."""
        # [AutoC 2026-05-13] Why: entry branch merge uses base_count to append only
        # branch-local tail messages back to the parent. How: replace only the
        # forked prefix with summary + recent prefix messages, then update base_count
        # on active tasks. Purpose: branch sync cannot lose or duplicate the branch
        # tail during final merge.
        branch = str(branch_session_id or "").strip()
        if not branch or base_count <= 0:
            return
        try:
            branch_msgs = list(store.load(branch))
            if len(branch_msgs) < base_count:
                log.warning(
                    "compact branch sync skipped for %s: base_count=%d exceeds len=%d",
                    branch,
                    base_count,
                    len(branch_msgs),
                )
                return
            if base_count <= keep_recent + 1:
                return
            branch_prefix = branch_msgs[:base_count]
            branch_tail = branch_msgs[base_count:]
            prefix_keep = branch_prefix[-keep_recent:] if keep_recent > 0 else []
            branch_summary = self._clone_compact_summary_message(summary_msg)
            new_base = [branch_summary] + prefix_keep
            store.replace_all(branch, new_base + branch_tail)
            for task in tasks:
                task.input["base_count"] = len(new_base)
                task.input["base_last_id"] = getattr(prefix_keep[-1], "id", "") if prefix_keep else branch_summary.id
                task.updated_at = _now()
            log.info(
                "compact branch sync: %s base %d → %d, preserved tail=%d",
                branch,
                base_count,
                len(new_base),
                len(branch_tail),
            )
        except Exception as exc:
            log.warning("compact branch sync failed for %s: %s", branch, exc)

    def _sync_child_session_compact_locked(
        self,
        store: Any,
        child_session_id: str,
        summary_msg: Any,
        *,
        keep_recent: int,
    ) -> None:
        """Apply simplified compact sync to one active ordinary child session."""
        # [AutoC 2026-05-13] Why: ordinary child sessions are not merged by
        # base_count, so they can use the same simple summary + recent messages
        # shape as the parent. How: replace the whole child JSONL when doing so
        # shortens it. Purpose: long-lived active child sessions do not keep an
        # outdated uncompressed parent fork.
        child = str(child_session_id or "").strip()
        if not child:
            return
        try:
            child_msgs = list(store.load(child))
            before = len(child_msgs)
            if before <= keep_recent + 1:
                return
            child_keep = child_msgs[-keep_recent:] if keep_recent > 0 else []
            store.replace_all(child, [self._clone_compact_summary_message(summary_msg)] + child_keep)
            log.info(
                "compact child sync: %s %d → %d",
                child,
                before,
                1 + len(child_keep),
            )
        except Exception as exc:
            log.warning("compact child sync failed for %s: %s", child, exc)

    def _resume_compact_parent_locked(
        self, caller: Task, context_ref: str, *,
        compact_result: dict[str, int] | None = None,
        before: int = 0, after: int = 0, success: bool = True,
    ) -> None:
        """恢复等待压缩的父 task。"""
        caller.status = TaskStatus.pending
        caller.waiting_for_task_id = None
        caller.worker_id = None
        caller.lease_expires_at = None
        caller.updated_at = _now()
        if compact_result:
            _rd = {"type": "compact_done", "success": success, **compact_result}
        else:
            _rd = {"type": "compact_done", "success": success, "before": before, "after": after}
        caller.input["resume_data"] = _rd
        caller.input["context_ref"] = context_ref
        caller.input.pop("_compact_dispatch_pending", None)
        self._event_task_snapshot("task_resumed", caller)


    # ------------------------------------------------------------------ #
    #  Turn Summary — 非阻塞轮摘要节点化
    #  原先在 engine/runner.py 中做阻塞式 LLM 调用生成轮摘要，
    #  改为 supervisor 在 task 完成后按需触发独立的 system.turn_summarizer 节点。
    #  Created: 2026-04-25
    # ------------------------------------------------------------------ #

    def _is_turn_summary_result(self, task: Task) -> bool:
        """判断是否是 turn_summarizer 子 task 的完成事件。"""
        return bool(task.input.get("_turn_summary_dispatch"))

    def _apply_turn_summary_result_locked(self, task: Task) -> None:
        """summarizer 完成后，将摘要回写到 TaskRecord。

        write_task_record 是 append 模式（追加到 JSONL），所以回写 summary 时
        会追加一条新记录。同一 task_id 出现多次时，后者覆盖前者——
        snip_history 遍历全部 records 取最后一条有 summary 的。
        """
        act = str((task.result or {}).get("action") or "").strip()
        _ts_route_sid = str(task.input.get("_target_session_id") or task.session_id or "").strip()
        if act != "finish":
            log.warning(
                "turn_summary: summarizer returned action=%s for task %s",
                act or "empty", str(task.task_id or "")[:12],
            )
            if _ts_route_sid:
                self.eventlog.append(
                    session_id=_ts_route_sid,
                    component="supervisor",
                    type_="system_notice",
                    payload={
                        "level": "warning",
                        "title": "轮摘要生成失败",
                        "text": f"摘要节点返回 {act or 'empty'}，不影响正常对话。",
                        "subsystem": "turn_summary",
                    },
                )
            return

        result = (task.result or {}).get("result") or {}
        summary = str(result.get("text") or "").strip()
        if not summary or len(summary) < 50:
            log.warning(
                "turn_summary: output too short (%d chars) for task %s",
                len(summary), str(task.input.get("_target_task_id") or "")[:12],
            )
            return

        target_task_id = str(task.input.get("_target_task_id") or "").strip()
        target_session_id = str(task.input.get("_target_session_id") or "").strip()
        if not target_task_id or not target_session_id:
            log.warning("turn_summary: missing target_task_id or target_session_id")
            return

        # 回写 TaskRecord：追加一条 updated record
        try:
            from engine.task_record import TaskRecord, write_task_record, load_task_records
            from pathlib import Path
            records = load_task_records(Path(self.workspace_root), target_session_id)
            for r in records:
                if r.task_id == target_task_id:
                    r.summary = summary
                    write_task_record(Path(self.workspace_root), r)
                    log.info("Turn summary written for task %s: %d chars", target_task_id[:12], len(summary))
                    break
        except Exception as e:
            log.warning("Failed to write turn summary for task %s: %s", target_task_id[:12], e)

    def _maybe_trigger_turn_summary_locked(self, task: Task) -> None:
        """Task 完成后，检查是否需要创建轮摘要后置任务。

        门控条件：
        - 仅对 finish/fail 的 node 类型 task 触发
        - 不对系统内部任务触发（避免递归）
        - tool_call_count >= 3 或 total_tokens >= 4000
        - runtime.yaml 中 engine.turn_summary.enabled 为 true
        """
        act = str((task.result or {}).get("action") or "").strip()
        if act not in ("finish", "fail") or task.kind != TaskKind.node:
            return
        # 不对系统内部任务触发
        if task.input.get("_system_task"):
            return
        # [AutoC 2026-05-30] Why: persistent 子节点（accumulate 模式）需要轮摘要来支持 snip compact。
        # How: 仅跳过 fresh/fork 模式的一次性子节点，保留 accumulate 模式的。
        # Purpose: scout/smith 等持久节点能正常触发轮摘要和压缩。
        if task.caller_task_id:
            _child_ctx_mode = str(task.input.get("context_mode") or "").strip()
            if _child_ctx_mode in ("fresh", "fork") or not _child_ctx_mode:
                return

        runtime_cfg = load_runtime_config(self.workspace_root)
        if not get_bool(runtime_cfg, "engine.turn_summary.enabled", True):
            return

        # 门控：仅针对长工具链，短任务不触发
        min_calls = get_int(runtime_cfg, "engine.turn_summary.min_tool_calls", 3)
        _tool_call_count = (task.result or {}).get("_tool_call_count", 0)
        if _tool_call_count < min_calls:
            return

        # 格式化 task 消息，作为 summarizer 的输入
        _instruction = ""
        # [Fork/Merge 2026-05-12] 入口分支完成后，摘要任务必须回到主 session。
        # 原因：branch JSONL 已经在 finalize 中 merge 并 cleanup，继续读取 task.session_id
        # 会访问已删除的分支。做法：无 child_session_id 时读取 route_session_id。
        # 目的：轮摘要仍能基于合并后的主 ConversationStore 生成。
        _route_sid = self._route_session_id_for_task_locked(task)
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            _store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            _child_sid = task.input.get("child_session_id") or ""
            _load_sid = _child_sid if _child_sid else _route_sid
            _all_msgs = _store.load(_load_sid)
            _task_msgs = [m for m in _all_msgs if m.source_task_id == task.task_id]
            if not _task_msgs:
                return
            _instruction = _format_task_messages_for_turn_summary(_task_msgs)
        except Exception as e:
            log.warning("Failed to format task messages for turn summary: %s", e)
            return

        if not _instruction.strip():
            return

        # Strip tool call blocks to prevent summarizer from mimicking tool calls.
        # The <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>> markers cause flash models to
        # hallucinate tool execution instead of summarizing.
        import re
        _instruction = re.sub(
            r'<<<TOOL_CALL>>>.*?<<<END_TOOL_CALL>>>',
            '[tool call omitted]',
            _instruction,
            flags=re.DOTALL,
        )

        # 截断（保持在 ~30K chars，与原 turn_summary.py 一致）
        if len(_instruction) > 30000:
            _instruction = _instruction[:30000] + "\n...[truncated]"

        summarizer_node = get_str(runtime_cfg, "engine.turn_summary.node_id", "system.turn_summarizer").strip()

        # [2026-05-07] Give turn_summarizer its own child session so its messages
        # don't pollute the parent conversation JSONL.
        # Why: previously used task.session_id directly, causing all summarizer
        # tool_call/tool_result messages to be written into the parent's JSONL,
        # creating orphan tool pairs that Anthropic/Gemini reject with 400.
        _sum_child_sid, _ = self.get_or_create_child_session(
            _route_sid, summarizer_node, "turn_summary", "fresh",
        )
        _summary_generation = self._current_session_generation_locked(_route_sid) or task.session_generation

        self._create_task_locked(
            session_id=_route_sid,
            session_generation=_summary_generation,
            kind=TaskKind.node,
            node_id=summarizer_node,
            input_data={
                "instruction": _instruction,
                "_system_task": True,
                "_turn_summary_dispatch": True,
                "_target_task_id": task.task_id,
                # [AutoC 2026-05-30] Why: persistent 子节点的 TaskRecord 存在 child_session_id 下，
                # 轮摘要回写时需要从正确的 transcript JSONL 找到记录。
                # How: 优先使用 child_session_id。 Purpose: snip compact 能找到摘要。
                "_target_session_id": str(task.input.get("child_session_id") or "").strip() or _route_sid,
                "child_session_id": _sum_child_sid,
            },
            continuation={},
            source_inbound_seq=None,
            caller_task_id=None,
        )
