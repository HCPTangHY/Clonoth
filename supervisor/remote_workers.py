from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from .eventlog import SYSTEM_SESSION_ID, EventLog

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
_REMOTE_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_REMOTE_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_PATH_PART_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

WorkerSendJson = Callable[[Any, dict[str, Any]], Awaitable[None]]
ToolsChangedCallback = Callable[[], int]


class RemoteWorkerError(RuntimeError):
    """Base class for remote worker broker errors."""


class RemoteWorkerRejected(RemoteWorkerError):
    """Raised when a worker hello is not accepted."""


class RemoteToolNotFound(RemoteWorkerError):
    """Raised when a requested remote tool is unknown."""


class RemoteWorkerBusy(RemoteWorkerError):
    """Raised when no worker becomes available within the queue timeout."""


class RemoteCallNotFound(RemoteWorkerError):
    """Raised when a remote call id does not exist."""


class RemoteWorkerUnavailable(RemoteWorkerError):
    """Raised when routing cannot safely select a worker."""


@dataclass(frozen=True)
class RemoteWorkerConfig:
    config_id: str
    enabled: bool
    token_sha256: str
    namespaces: set[str]
    allowed_tools: dict[str, set[str]]
    max_concurrency: int
    allow_dynamic_schema: bool
    tool_definitions: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


@dataclass
class RemoteToolRegistration:
    registered_name: str
    namespace: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    timeout_sec: float
    stateful: bool
    schema_fingerprint: str


@dataclass
class RemoteToolPool:
    registered_name: str
    namespace: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    timeout_sec: float
    stateful: bool
    schema_fingerprint: str
    workers: set[str] = field(default_factory=set)


@dataclass
class RemoteWorker:
    connection_id: str
    worker_id: str
    worker_name: str
    namespace: str
    websocket: Any
    max_concurrency: int
    heartbeat_interval_sec: float
    capabilities: dict[str, Any]
    config_id: str
    connected_at: datetime
    last_seen_at: datetime
    registered_tools: list[str] = field(default_factory=list)
    running_calls: set[str] = field(default_factory=set)
    reported_running_calls: int = 0
    load: float | None = None


@dataclass
class RemoteCall:
    call_id: str
    registered_name: str
    namespace: str
    tool_name: str
    arguments: dict[str, Any]
    context: dict[str, Any]
    timeout_sec: float
    worker_id: str
    status: str
    created_at: datetime
    deadline_monotonic: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] | None = None
    cancel_requested: bool = False


class RemoteWorkerManager:
    """Runtime broker for remote tool workers connected to the supervisor.

    [AutoC 2026-08-01] This manager is supervisor-side only.
    Why: remote workers connect through WebSocket while engines call ordinary HTTP
    broker endpoints. How: keep worker registration, tool pools, routing, call state,
    stale heartbeat cleanup, and attachment persistence in one runtime object. Purpose:
    ToolRegistry can later treat remote tools like local, script, or MCP tools without
    knowing which worker is behind the call.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        eventlog: EventLog,
        send_json: WorkerSendJson,
        on_tools_changed: ToolsChangedCallback | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.eventlog = eventlog
        self._send_json = send_json
        self._on_tools_changed = on_tools_changed
        self.config_path = workspace_root / "data" / "remote_workers.yaml"
        self.attachments_root = workspace_root / "data" / "attachments" / "remote"

        self.worker_stale_sec = 45.0
        self.queue_timeout_sec = 10.0
        self.max_timeout_sec = 300.0
        self.max_result_bytes = 1_048_576
        self.max_attachment_bytes = 10_485_760
        self._configs_by_id: dict[str, RemoteWorkerConfig] = {}

        self._condition = asyncio.Condition()
        self._reaper_task: asyncio.Task | None = None

        self.workers_by_id: dict[str, RemoteWorker] = {}
        self.connections: dict[str, str] = {}
        self.tools_by_registered_name: dict[str, RemoteToolPool] = {}
        self.calls: dict[str, RemoteCall] = {}
        self.session_affinity: dict[tuple[str, str], str] = {}

        self._load_config()

    def _default_config(self) -> dict[str, Any]:
        return {
            "version": 1,
            "worker_stale_sec": 45,
            "queue_timeout_sec": 10,
            "max_timeout_sec": 300,
            "max_result_bytes": 1_048_576,
            "max_attachment_bytes": 10_485_760,
            "workers": {},
        }

    def _load_config(self) -> None:
        # [AutoC 2026-08-01] Keep the worker credential store separate from admin auth.
        # Why: remote workers are long-lived external clients and must not reuse the
        # web Admin Token. How: load data/remote_workers.yaml, creating an empty
        # default file when it is absent. Purpose: deployments start locked down until
        # explicit token hashes and tool allow-lists are configured.
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(yaml.safe_dump(self._default_config(), sort_keys=False), encoding="utf-8")

        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("failed to load remote worker config: %s", self.config_path)
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        self.worker_stale_sec = self._float_config(raw.get("worker_stale_sec"), 45.0, minimum=1.0)
        self.queue_timeout_sec = self._float_config(raw.get("queue_timeout_sec"), 10.0, minimum=0.0)
        self.max_timeout_sec = self._float_config(raw.get("max_timeout_sec"), 300.0, minimum=1.0)
        self.max_result_bytes = int(self._float_config(raw.get("max_result_bytes"), 1_048_576, minimum=1024))
        self.max_attachment_bytes = int(self._float_config(raw.get("max_attachment_bytes"), 10_485_760, minimum=1024))

        configs: dict[str, RemoteWorkerConfig] = {}
        workers_raw = raw.get("workers") if isinstance(raw.get("workers"), dict) else {}
        for config_id, cfg_raw in workers_raw.items():
            if not isinstance(cfg_raw, dict):
                continue
            cfg = self._parse_worker_config(str(config_id), cfg_raw)
            if cfg.enabled and cfg.token_sha256:
                configs[cfg.config_id] = cfg
        self._configs_by_id = configs

    @staticmethod
    def _float_config(value: Any, default: float, *, minimum: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = default
        if parsed < minimum:
            return minimum
        return parsed

    def _parse_worker_config(self, config_id: str, raw: dict[str, Any]) -> RemoteWorkerConfig:
        namespaces_raw = raw.get("namespaces")
        namespaces = {str(item).strip() for item in namespaces_raw or [] if str(item).strip()}

        allowed_tools: dict[str, set[str]] = {}
        allowed_raw = raw.get("allowed_tools") if isinstance(raw.get("allowed_tools"), dict) else {}
        for namespace, tools in allowed_raw.items():
            if isinstance(tools, list):
                allowed_tools[str(namespace)] = {str(item).strip() for item in tools if str(item).strip()}

        tool_definitions: dict[tuple[str, str], dict[str, Any]] = {}
        tools_raw = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
        for namespace, namespace_tools in tools_raw.items():
            if not isinstance(namespace_tools, dict):
                continue
            for tool_name, tool_def in namespace_tools.items():
                if isinstance(tool_def, dict):
                    tool_definitions[(str(namespace), str(tool_name))] = dict(tool_def)

        try:
            max_concurrency = int(raw.get("max_concurrency") or 1)
        except Exception:
            max_concurrency = 1
        max_concurrency = max(1, max_concurrency)

        return RemoteWorkerConfig(
            config_id=config_id,
            enabled=bool(raw.get("enabled", True)),
            token_sha256=str(raw.get("token_sha256") or "").strip().lower(),
            namespaces=namespaces,
            allowed_tools=allowed_tools,
            max_concurrency=max_concurrency,
            allow_dynamic_schema=bool(raw.get("allow_dynamic_schema", False)),
            tool_definitions=tool_definitions,
        )

    async def start(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._stale_reaper_loop())

    async def stop(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        async with self._condition:
            for connection_id in list(self.connections):
                self._unregister_connection_locked(connection_id, reason="supervisor_shutdown")
            self._condition.notify_all()

    async def _stale_reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, min(self.worker_stale_sec / 3.0, 15.0)))
            await self.reap_stale_workers()

    async def reap_stale_workers(self) -> None:
        now = self._now()
        stale_connections: list[str] = []
        timed_out_calls: list[str] = []
        tools_changed = False
        async with self._condition:
            for worker in self.workers_by_id.values():
                if (now - worker.last_seen_at).total_seconds() > self.worker_stale_sec:
                    stale_connections.append(worker.connection_id)
            # GC completed calls older than 5 minutes
            gc_cutoff = time.monotonic() - 300.0
            gc_ids = [
                cid for cid, c in self.calls.items()
                if c.status not in {"running", "cancel_requested"} and c.deadline_monotonic < gc_cutoff
            ]
            for cid in gc_ids:
                self.calls.pop(cid, None)
            for call in self.calls.values():
                if call.status in {"running", "cancel_requested"} and time.monotonic() >= call.deadline_monotonic:
                    timed_out_calls.append(call.call_id)
            for call_id in timed_out_calls:
                call = self.calls.get(call_id)
                if call is not None:
                    self._finish_call_locked(call, ok=False, status="timeout", error="remote tool timed out")
            for connection_id in stale_connections:
                # [AutoC 2026-08-01] Treat heartbeat expiry as a real disconnect.
                # Why: worker tools disappear from the live pool when the stale reaper
                # removes a worker, just like a WebSocket close. How: remember whether
                # unregister changed tool state and bump reload after leaving the lock.
                # Purpose: engine hot-reload can remove stale remote tools promptly.
                tools_changed = self._unregister_connection_locked(connection_id, reason="heartbeat_timeout") or tools_changed
            if stale_connections or timed_out_calls:
                self._condition.notify_all()
        if tools_changed:
            self._bump_tools_reload()

    async def register_worker(self, *, websocket: Any, token: str, hello: dict[str, Any]) -> dict[str, Any]:
        self._load_config()
        try:
            config = self._authenticate_token(token)
            registrations = self._validate_hello(config, hello)
        except RemoteWorkerRejected as exc:
            return {"type": "hello_ack", "accepted": False, "error": str(exc)}

        worker_id = str(hello.get("worker_id") or "").strip()
        worker_name = str(hello.get("worker_name") or worker_id).strip() or worker_id
        namespace = str(hello.get("namespace") or "").strip()
        capabilities = hello.get("capabilities") if isinstance(hello.get("capabilities"), dict) else {}
        try:
            requested_concurrency = int(hello.get("max_concurrency") or config.max_concurrency)
        except Exception:
            requested_concurrency = config.max_concurrency
        max_concurrency = max(1, min(requested_concurrency, config.max_concurrency))
        heartbeat_interval_sec = self._float_config(hello.get("heartbeat_interval_sec"), 15.0, minimum=1.0)
        connection_id = uuid.uuid4().hex
        now = self._now()

        async with self._condition:
            if worker_id in self.workers_by_id:
                old = self.workers_by_id[worker_id]
                self._unregister_connection_locked(old.connection_id, reason="worker_replaced")

            worker = RemoteWorker(
                connection_id=connection_id,
                worker_id=worker_id,
                worker_name=worker_name,
                namespace=namespace,
                websocket=websocket,
                max_concurrency=max_concurrency,
                heartbeat_interval_sec=heartbeat_interval_sec,
                capabilities=dict(capabilities),
                config_id=config.config_id,
                connected_at=now,
                last_seen_at=now,
                registered_tools=[item.registered_name for item in registrations],
            )
            self.workers_by_id[worker_id] = worker
            self.connections[connection_id] = worker_id

            for item in registrations:
                pool = self.tools_by_registered_name.get(item.registered_name)
                if pool is None:
                    pool = RemoteToolPool(
                        registered_name=item.registered_name,
                        namespace=item.namespace,
                        tool_name=item.tool_name,
                        description=item.description,
                        input_schema=item.input_schema,
                        timeout_sec=item.timeout_sec,
                        stateful=item.stateful,
                        schema_fingerprint=item.schema_fingerprint,
                    )
                    self.tools_by_registered_name[item.registered_name] = pool
                pool.workers.add(worker_id)
                self._emit_event(
                    "remote_tool_registered",
                    {
                        "worker_id": worker_id,
                        "connection_id": connection_id,
                        "registered_name": item.registered_name,
                        "namespace": item.namespace,
                        "tool_name": item.tool_name,
                    },
                )
            self._emit_event(
                "remote_worker_connected",
                {
                    "worker_id": worker_id,
                    "worker_name": worker_name,
                    "connection_id": connection_id,
                    "namespace": namespace,
                    "tool_count": len(registrations),
                },
            )
            self._condition.notify_all()

        self._bump_tools_reload()
        return {
            "type": "hello_ack",
            "accepted": True,
            "protocol_version": PROTOCOL_VERSION,
            "worker_id": worker_id,
            "connection_id": connection_id,
            "registered_tools": [
                {"registered_name": item.registered_name, "namespace": item.namespace, "tool_name": item.tool_name}
                for item in registrations
            ],
            "heartbeat_interval_sec": heartbeat_interval_sec,
        }

    def _authenticate_token(self, token: str) -> RemoteWorkerConfig:
        if not token:
            raise RemoteWorkerRejected("missing worker token")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest().lower()
        for config in self._configs_by_id.values():
            if hmac.compare_digest(token_hash, config.token_sha256):
                return config
        raise RemoteWorkerRejected("invalid worker token")

    def _validate_hello(self, config: RemoteWorkerConfig, hello: dict[str, Any]) -> list[RemoteToolRegistration]:
        if hello.get("type") != "hello":
            raise RemoteWorkerRejected("first worker message must be hello")
        if int(hello.get("protocol_version") or 0) != PROTOCOL_VERSION:
            raise RemoteWorkerRejected("unsupported protocol_version")

        worker_id = str(hello.get("worker_id") or "").strip()
        namespace = str(hello.get("namespace") or "").strip()
        if not _REMOTE_WORKER_ID_RE.match(worker_id):
            raise RemoteWorkerRejected("invalid worker_id")
        if not _REMOTE_TOOL_NAME_RE.match(namespace):
            raise RemoteWorkerRejected("invalid namespace")
        if config.namespaces and namespace not in config.namespaces:
            raise RemoteWorkerRejected(f"unauthorized namespace: {namespace}")

        tools = hello.get("tools")
        if not isinstance(tools, list) or not tools:
            raise RemoteWorkerRejected("worker must declare at least one tool")

        registrations: list[RemoteToolRegistration] = []
        seen: set[str] = set()
        for item in tools:
            if not isinstance(item, dict):
                raise RemoteWorkerRejected("tool definitions must be objects")
            tool_name = str(item.get("name") or "").strip()
            if not _REMOTE_TOOL_NAME_RE.match(tool_name):
                raise RemoteWorkerRejected(f"invalid tool name: {tool_name}")
            if tool_name in seen:
                raise RemoteWorkerRejected(f"duplicate tool: {namespace}.{tool_name}")
            seen.add(tool_name)

            allowed_for_namespace = config.allowed_tools.get(namespace)
            if allowed_for_namespace is not None and tool_name not in allowed_for_namespace:
                raise RemoteWorkerRejected(f"unauthorized tool: {namespace}.{tool_name}")
            if allowed_for_namespace is None and config.allowed_tools:
                raise RemoteWorkerRejected(f"unauthorized namespace tools: {namespace}")
            if not config.allowed_tools:
                raise RemoteWorkerRejected("worker token has no allowed_tools configured")

            definition = config.tool_definitions.get((namespace, tool_name), {})
            if definition:
                description = str(definition.get("description") or item.get("description") or f"Remote tool {namespace}.{tool_name}")
                input_schema = definition.get("input_schema") if isinstance(definition.get("input_schema"), dict) else {"type": "object", "properties": {}, "required": []}
                timeout_sec = self._float_config(definition.get("timeout_sec", item.get("timeout_sec")), 30.0, minimum=0.1)
                stateful = bool(definition.get("stateful", item.get("stateful", False)))
            elif config.allow_dynamic_schema:
                # [AutoC 2026-08-01] Allow worker-provided schemas only in explicit
                # development mode. Why: the design says production should not trust
                # arbitrary worker schema or description data. How: require
                # allow_dynamic_schema=true when no supervisor-owned definition exists.
                # Purpose: a token plus allow-list is not enough to mutate tool schemas
                # unless the operator deliberately enables that mode.
                description = str(item.get("description") or f"Remote tool {namespace}.{tool_name}")
                input_schema = item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {"type": "object", "properties": {}, "required": []}
                timeout_sec = self._float_config(item.get("timeout_sec"), 30.0, minimum=0.1)
                stateful = bool(item.get("stateful", False))
            else:
                raise RemoteWorkerRejected(f"tool schema not configured: {namespace}.{tool_name}")
            timeout_sec = min(timeout_sec, self.max_timeout_sec)
            registered_name = f"remote_{namespace}_{tool_name}"
            schema_fingerprint = self._schema_fingerprint(input_schema)

            pool = self.tools_by_registered_name.get(registered_name)
            if pool is not None:
                if pool.schema_fingerprint != schema_fingerprint or pool.stateful != stateful:
                    raise RemoteWorkerRejected(f"conflicting schema for tool: {registered_name}")

            registrations.append(
                RemoteToolRegistration(
                    registered_name=registered_name,
                    namespace=namespace,
                    tool_name=tool_name,
                    description=description,
                    input_schema=dict(input_schema),
                    timeout_sec=timeout_sec,
                    stateful=stateful,
                    schema_fingerprint=schema_fingerprint,
                )
            )
        return registrations

    @staticmethod
    def _schema_fingerprint(schema: dict[str, Any]) -> str:
        text = json.dumps(schema, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def unregister_worker(self, connection_id: str, *, reason: str = "disconnected") -> None:
        changed = False
        async with self._condition:
            changed = self._unregister_connection_locked(connection_id, reason=reason)
            self._condition.notify_all()
        if changed:
            self._bump_tools_reload()

    def _unregister_connection_locked(self, connection_id: str, *, reason: str) -> bool:
        worker_id = self.connections.pop(connection_id, None)
        if not worker_id:
            return False
        worker = self.workers_by_id.pop(worker_id, None)
        if worker is None:
            return False

        for call in list(self.calls.values()):
            if call.worker_id == worker_id and call.status in {"running", "cancel_requested"}:
                self._finish_call_locked(call, ok=False, status="worker_disconnected", error=f"worker_disconnected: {reason}")

        for registered_name in worker.registered_tools:
            pool = self.tools_by_registered_name.get(registered_name)
            if pool is None:
                continue
            pool.workers.discard(worker_id)
            if not pool.workers:
                self.tools_by_registered_name.pop(registered_name, None)

        for key, affinity_worker_id in list(self.session_affinity.items()):
            if affinity_worker_id == worker_id:
                self.session_affinity.pop(key, None)

        self._emit_event(
            "remote_worker_disconnected",
            {
                "worker_id": worker_id,
                "connection_id": connection_id,
                "reason": reason,
            },
        )
        logger.info("remote worker disconnected: worker_id=%s connection_id=%s reason=%s", worker_id, connection_id, reason)
        return True

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for pool in sorted(self.tools_by_registered_name.values(), key=lambda item: item.registered_name):
            worker_count = len([worker_id for worker_id in pool.workers if worker_id in self.workers_by_id])
            if worker_count <= 0:
                continue
            tools.append(
                {
                    "name": pool.registered_name,
                    "description": f"[Remote:{pool.namespace}] {pool.description}",
                    "input_schema": pool.input_schema,
                    "timeout_sec": pool.timeout_sec,
                    "remote": {
                        "namespace": pool.namespace,
                        "tool_name": pool.tool_name,
                        "worker_count": worker_count,
                        "stateful": pool.stateful,
                    },
                }
            )
        return tools

    async def list_tools_async(self) -> list[dict[str, Any]]:
        async with self._condition:
            return self.list_tools()

    async def list_workers(self) -> list[dict[str, Any]]:
        now = self._now()
        async with self._condition:
            return [
                {
                    "connection_id": worker.connection_id,
                    "worker_id": worker.worker_id,
                    "worker_name": worker.worker_name,
                    "namespace": worker.namespace,
                    "connected_at": worker.connected_at,
                    "last_seen_at": worker.last_seen_at,
                    "max_concurrency": worker.max_concurrency,
                    "running_calls": len(worker.running_calls),
                    "load": worker.load,
                    "tools": list(worker.registered_tools),
                    "capabilities": dict(worker.capabilities),
                    "stale": (now - worker.last_seen_at).total_seconds() > self.worker_stale_sec,
                }
                for worker in sorted(self.workers_by_id.values(), key=lambda item: item.worker_id)
            ]

    async def create_call(
        self,
        *,
        registered_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        timeout_sec: float | None,
    ) -> RemoteCall:
        timeout = min(self._float_config(timeout_sec, 30.0, minimum=0.1), self.max_timeout_sec)
        route_deadline = time.monotonic() + self.queue_timeout_sec
        call: RemoteCall | None = None
        worker: RemoteWorker | None = None
        pool: RemoteToolPool | None = None

        async with self._condition:
            while True:
                pool = self.tools_by_registered_name.get(registered_name)
                if pool is None:
                    raise RemoteToolNotFound(f"remote tool not found: {registered_name}")
                worker = self._select_worker_locked(pool, context)
                if worker is not None:
                    call_id = "call_" + uuid.uuid4().hex
                    call = RemoteCall(
                        call_id=call_id,
                        registered_name=registered_name,
                        namespace=pool.namespace,
                        tool_name=pool.tool_name,
                        arguments=dict(arguments),
                        context=dict(context),
                        timeout_sec=timeout,
                        worker_id=worker.worker_id,
                        status="running",
                        created_at=self._now(),
                        deadline_monotonic=time.monotonic() + timeout,
                    )
                    self.calls[call_id] = call
                    worker.running_calls.add(call_id)
                    route_session_id = self._route_session_id(context)
                    if pool.stateful and route_session_id:
                        self.session_affinity[(route_session_id, registered_name)] = worker.worker_id
                    self._condition.notify_all()
                    break

                remaining = route_deadline - time.monotonic()
                if remaining <= 0:
                    raise RemoteWorkerBusy(f"no available worker for {registered_name}")
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise RemoteWorkerBusy(f"no available worker for {registered_name}") from None

        if call is None or worker is None or pool is None:
            raise RemoteWorkerUnavailable("remote call routing failed")

        message = {
            "type": "tool_call",
            "call_id": call.call_id,
            "registered_name": registered_name,
            "namespace": pool.namespace,
            "tool_name": pool.tool_name,
            "arguments": arguments,
            "deadline_ms": int(timeout * 1000),
            "context": context,
        }
        try:
            await self._send_json(worker.websocket, message)
        except Exception as exc:
            await self._fail_call(call.call_id, status="failed", error=f"worker send failed: {exc}")
            await self.unregister_worker(worker.connection_id, reason="send_failed")
            raise RemoteWorkerUnavailable(f"worker send failed: {exc}") from exc

        self._emit_event(
            "remote_tool_call_started",
            {
                "call_id": call.call_id,
                "registered_name": registered_name,
                "worker_id": worker.worker_id,
                "context": context,
            },
        )
        return call

    def _select_worker_locked(self, pool: RemoteToolPool, context: dict[str, Any]) -> RemoteWorker | None:
        route_session_id = self._route_session_id(context)
        affinity_key = (route_session_id, pool.registered_name) if route_session_id else None
        if pool.stateful and affinity_key is not None:
            affinity_worker_id = self.session_affinity.get(affinity_key)
            if affinity_worker_id:
                worker = self.workers_by_id.get(affinity_worker_id)
                if worker is None or affinity_worker_id not in pool.workers:
                    self.session_affinity.pop(affinity_key, None)
                    raise RemoteWorkerUnavailable(f"stateful affinity worker disconnected for {pool.registered_name}")
                if len(worker.running_calls) < worker.max_concurrency:
                    return worker
                return None

        candidates = [
            worker
            for worker_id in pool.workers
            if (worker := self.workers_by_id.get(worker_id)) is not None and len(worker.running_calls) < worker.max_concurrency
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: (len(item.running_calls), item.reported_running_calls, item.worker_id))

    @staticmethod
    def _route_session_id(context: dict[str, Any]) -> str:
        for key in ("session_id", "conversation_key"):
            value = str(context.get(key) or "").strip()
            if value:
                return value
        return ""

    async def wait_call_result(self, call_id: str, wait_sec: float) -> dict[str, Any] | None:
        await self.reap_stale_workers()
        async with self._condition:
            call = self.calls.get(call_id)
            if call is None:
                raise RemoteCallNotFound(f"remote call not found: {call_id}")
            if call.result is not None:
                return dict(call.result)
            done = call.done

        wait = max(0.0, min(float(wait_sec or 0.0), 30.0))
        if wait > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=wait)

        await self.reap_stale_workers()
        async with self._condition:
            call = self.calls.get(call_id)
            if call is None:
                raise RemoteCallNotFound(f"remote call not found: {call_id}")
            if call.result is not None:
                return dict(call.result)
            return None

    async def call_status(self, call_id: str) -> dict[str, Any]:
        async with self._condition:
            call = self.calls.get(call_id)
            if call is None:
                raise RemoteCallNotFound(f"remote call not found: {call_id}")
            return {
                "ok": True,
                "call_id": call.call_id,
                "worker_id": call.worker_id,
                "status": call.status,
            }

    async def cancel_call(self, call_id: str, reason: str) -> bool:
        async with self._condition:
            call = self.calls.get(call_id)
            if call is None:
                raise RemoteCallNotFound(f"remote call not found: {call_id}")
            if call.status not in {"running", "cancel_requested"}:
                return False
            call.status = "cancel_requested"
            call.cancel_requested = True
            worker = self.workers_by_id.get(call.worker_id)
            if worker is None:
                self._finish_call_locked(call, ok=False, status="worker_disconnected", error="worker_disconnected: cancel failed")
                self._condition.notify_all()
                return False
            message = {"type": "cancel_tool_call", "call_id": call_id, "reason": reason or "cancelled"}

        try:
            await self._send_json(worker.websocket, message)
        except Exception as exc:
            await self._fail_call(call_id, status="failed", error=f"worker cancel send failed: {exc}")
            await self.unregister_worker(worker.connection_id, reason="cancel_send_failed")
            raise RemoteWorkerUnavailable(f"worker cancel send failed: {exc}") from exc
        return True

    async def handle_worker_message(self, connection_id: str, message: dict[str, Any]) -> None:
        msg_type = str(message.get("type") or "")
        if msg_type == "heartbeat":
            await self._handle_heartbeat(connection_id, message)
        elif msg_type == "tool_result":
            await self._handle_tool_result(connection_id, message)
        elif msg_type == "tool_cancelled":
            await self._handle_tool_cancelled(connection_id, message)
        else:
            logger.warning("unknown remote worker message: connection_id=%s type=%s", connection_id, msg_type)

    async def _handle_heartbeat(self, connection_id: str, message: dict[str, Any]) -> None:
        websocket: Any | None = None
        async with self._condition:
            worker = self._worker_for_connection_locked(connection_id)
            if worker is None:
                return
            worker.last_seen_at = self._now()
            try:
                worker.reported_running_calls = max(0, int(message.get("running_calls") or 0))
            except Exception:
                worker.reported_running_calls = len(worker.running_calls)
            try:
                worker.load = float(message.get("load")) if message.get("load") is not None else None
            except Exception:
                worker.load = None
            websocket = worker.websocket
        if websocket is not None:
            await self._send_json(websocket, {"type": "heartbeat_ack", "server_time": self._now().isoformat()})

    async def _handle_tool_result(self, connection_id: str, message: dict[str, Any]) -> None:
        call_id = str(message.get("call_id") or "").strip()
        if not call_id:
            return
        async with self._condition:
            worker = self._worker_for_connection_locked(connection_id)
            call = self.calls.get(call_id)
            if worker is None or call is None or call.worker_id != worker.worker_id:
                return
            if call.status not in {"running", "cancel_requested"}:
                return
            worker_id = worker.worker_id

        try:
            result = self._normalise_tool_result(worker_id=worker_id, call_id=call_id, message=message)
        except Exception as exc:
            logger.exception("failed to process remote tool result: call_id=%s", call_id)
            result = self._result_payload(call_id=call_id, worker_id=worker_id, ok=False, status="failed", error=str(exc))

        async with self._condition:
            call = self.calls.get(call_id)
            if call is None or call.status not in {"running", "cancel_requested"}:
                return
            self._finish_call_locked(
                call,
                ok=bool(result.get("ok")),
                status=str(result.get("status") or ("completed" if result.get("ok") else "failed")),
                error=result.get("error"),
                result=result,
            )
            self._condition.notify_all()

    async def _handle_tool_cancelled(self, connection_id: str, message: dict[str, Any]) -> None:
        call_id = str(message.get("call_id") or "").strip()
        if not call_id:
            return
        async with self._condition:
            worker = self._worker_for_connection_locked(connection_id)
            call = self.calls.get(call_id)
            if worker is None or call is None or call.worker_id != worker.worker_id:
                return
            result = self._result_payload(
                call_id=call.call_id,
                worker_id=call.worker_id,
                ok=False,
                status="cancelled",
                error="remote tool cancelled",
                data={"result": "ERROR: remote tool cancelled"},
                cancelled=True,
            )
            self._finish_call_locked(call, ok=False, status="cancelled", error="remote tool cancelled", result=result)
            self._condition.notify_all()

    def _worker_for_connection_locked(self, connection_id: str) -> RemoteWorker | None:
        worker_id = self.connections.get(connection_id)
        if not worker_id:
            return None
        return self.workers_by_id.get(worker_id)

    def _normalise_tool_result(self, *, worker_id: str, call_id: str, message: dict[str, Any]) -> dict[str, Any]:
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        ok = bool(message.get("ok", False))
        cancelled = bool(message.get("cancelled", False))
        status = "cancelled" if cancelled else ("completed" if ok else "failed")
        error = None if ok and not cancelled else str(message.get("error") or "remote tool failed")
        self._ensure_result_size(ok=ok, data=data, error=message.get("error"))
        attachments = self._save_attachments(worker_id=worker_id, call_id=call_id, attachments=message.get("attachments"))
        payload = self._result_payload(
            call_id=call_id,
            worker_id=worker_id,
            ok=ok and not cancelled,
            status=status,
            error=error,
            data=data,
            attachments=attachments,
            cancelled=cancelled,
        )
        if "elapsed_ms" in message:
            payload["elapsed_ms"] = message.get("elapsed_ms")
        return payload

    def _ensure_result_size(self, *, ok: bool, data: dict[str, Any], error: Any) -> None:
        # [AutoC 2026-08-01] Enforce a size cap for the model-visible result body.
        # Why: remote screenshots or files should be returned as attachments, not as
        # huge data.result strings. How: measure the JSON body that would be returned
        # by the broker and reject oversized inline results. Purpose: a worker cannot
        # push unbounded data into Supervisor memory or later model context.
        payload = {"ok": ok, "data": data, "error": error}
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        if size > self.max_result_bytes:
            raise ValueError(f"remote result too large: {size} bytes > {self.max_result_bytes} bytes")

    def _save_attachments(self, *, worker_id: str, call_id: str, attachments: Any) -> list[dict[str, Any]]:
        if not isinstance(attachments, list):
            return []
        out: list[dict[str, Any]] = []
        target_dir = self.attachments_root / self._safe_path_part(worker_id) / self._safe_path_part(call_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        for idx, item in enumerate(attachments):
            if not isinstance(item, dict):
                continue
            raw_base64 = str(item.get("base64") or "")
            if not raw_base64:
                continue
            try:
                content = base64.b64decode(raw_base64, validate=True)
            except Exception as exc:
                raise ValueError(f"invalid attachment base64 at index {idx}") from exc
            if len(content) > self.max_attachment_bytes:
                raise ValueError(f"attachment too large at index {idx}")
            filename = self._safe_filename(str(item.get("name") or f"attachment_{idx}"))
            path = target_dir / filename
            if path.exists():
                stem = path.stem or "attachment"
                suffix = path.suffix
                path = target_dir / f"{stem}_{idx}{suffix}"
            path.write_bytes(content)
            mime_type = str(item.get("mime_type") or item.get("type") or "application/octet-stream")
            rel_path = path.relative_to(self.workspace_root).as_posix()
            out.append(
                {
                    "path": rel_path,
                    "name": filename,
                    "type": "image" if mime_type.startswith("image/") else "file",
                    "mime_type": mime_type,
                    "size": len(content),
                }
            )
        return out

    @staticmethod
    def _safe_path_part(value: str) -> str:
        safe = _SAFE_PATH_PART_RE.sub("_", value.strip()).strip("._")
        return safe or "unknown"

    @staticmethod
    def _safe_filename(value: str) -> str:
        name = Path(value).name.strip() or "attachment"
        safe = _SAFE_FILENAME_RE.sub("_", name).strip("._")
        return safe or "attachment"

    async def _fail_call(self, call_id: str, *, status: str, error: str) -> None:
        async with self._condition:
            call = self.calls.get(call_id)
            if call is not None and call.status in {"running", "cancel_requested"}:
                self._finish_call_locked(call, ok=False, status=status, error=error)
                self._condition.notify_all()

    def _finish_call_locked(
        self,
        call: RemoteCall,
        *,
        ok: bool,
        status: str,
        error: Any = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        call.status = status
        if result is None:
            result = self._result_payload(call_id=call.call_id, worker_id=call.worker_id, ok=ok, status=status, error=error)
        call.result = result
        worker = self.workers_by_id.get(call.worker_id)
        if worker is not None:
            worker.running_calls.discard(call.call_id)
        call.done.set()
        event_type = "remote_tool_call_completed" if ok else "remote_tool_call_failed"
        self._emit_event(
            event_type,
            {
                "call_id": call.call_id,
                "registered_name": call.registered_name,
                "worker_id": call.worker_id,
                "status": status,
                "error": error,
            },
        )

    @staticmethod
    def _result_payload(
        *,
        call_id: str,
        worker_id: str,
        ok: bool,
        status: str,
        error: Any = None,
        data: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "call_id": call_id,
            "worker_id": worker_id,
            "status": status,
            "data": data or {"result": "" if ok else f"ERROR: {error or status}"},
            "error": None if ok else str(error or status),
            "attachments": attachments or [],
            "cancelled": cancelled,
        }

    def _emit_event(self, type_: str, payload: dict[str, Any]) -> None:
        try:
            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="remote_worker",
                type_=type_,
                payload=payload,
                transient=True,
            )
        except Exception:
            logger.exception("failed to append remote worker event: %s", type_)

    def _bump_tools_reload(self) -> None:
        if self._on_tools_changed is None:
            return
        try:
            self._on_tools_changed()
        except Exception:
            logger.exception("failed to bump tools reload after remote worker change")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
