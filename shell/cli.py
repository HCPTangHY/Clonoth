from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import get_float, load_runtime_config, strip_tool_trace_blocks


def wait_supervisor(
    client: httpx.Client,
    base_url: str,
    *,
    health_timeout_sec: float = 2.0,
    poll_interval_sec: float = 0.5,
) -> None:
    print(f"[shell-cli] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = client.get(f"{base_url}/v1/health", timeout=health_timeout_sec)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(poll_interval_sec)


def _print_outbound(text: str) -> None:
    cleaned = strip_tool_trace_blocks(text)
    if cleaned:
        print(f"assistant> {cleaned}", flush=True)
    else:
        print("assistant> （已生成回复，但包含内部调试信息已被隐藏）", flush=True)


# ---------------------------------------------------------------------------
#  流式渲染
# ---------------------------------------------------------------------------

class _StreamRenderer:
    """管理终端流式输出的状态。"""

    def __init__(self) -> None:
        self._in_thinking = False
        self._in_text = False
        self._thinking_chars = 0

    def on_delta(self, kind: str, content: str) -> None:
        if kind == "thinking":
            if not self._in_thinking:
                self._in_thinking = True
                self._thinking_chars = 0
                sys.stdout.write("\033[2m💭 ")
            self._thinking_chars += len(content)
            remaining = 200 - (self._thinking_chars - len(content))
            if remaining > 0:
                show = content[:remaining]
                sys.stdout.write(show)
                if remaining < len(content):
                    sys.stdout.write("...")
            sys.stdout.flush()
        elif kind == "text":
            if self._in_thinking:
                sys.stdout.write("\033[0m\n")
                self._in_thinking = False
            if not self._in_text:
                self._in_text = True
                sys.stdout.write("assistant> ")
            sys.stdout.write(content)
            sys.stdout.flush()

    def on_stream_end(self) -> None:
        if self._in_thinking:
            sys.stdout.write("\033[0m\n")
            self._in_thinking = False
        if self._in_text:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._in_text = False

    @property
    def streamed_text(self) -> bool:
        return self._in_text


# ---------------------------------------------------------------------------
#  后台推送轮询器（处理定时任务等推送消息）
# ---------------------------------------------------------------------------

class _PushPoller:
    """后台线程。空闲时轮询事件，打印推送消息。"""

    def __init__(self, client: httpx.Client, base_url: str, poll_sec: float, global_seq: int = 0) -> None:
        self._client = client
        self._base_url = base_url
        self._poll_sec = poll_sec
        self._lock = threading.Lock()
        self._global_seq: int = global_seq
        self._paused: bool = False

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="push-poller")
        t.start()

    def update_seq(self, seq: int) -> None:
        with self._lock:
            self._global_seq = max(self._global_seq, seq)

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    @property
    def global_seq(self) -> int:
        with self._lock:
            return self._global_seq

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self._poll_sec)

    def _tick(self) -> None:
        with self._lock:
            if self._paused:
                return
            seq = self._global_seq

        try:
            r = self._client.get(
                f"{self._base_url}/v1/events",
                params={"after_seq": seq, "types": "outbound_message,approval_requested"},
            )
            if r.status_code != 200:
                return
            events = r.json()
        except Exception:
            return

        if not events:
            return

        new_seq = seq
        for e in events:
            new_seq = max(new_seq, int(e.get("seq", 0)))
            et = e.get("type")
            payload = e.get("payload") or {}

            if et == "outbound_message":
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    cleaned = strip_tool_trace_blocks(text)
                    if cleaned:
                        sys.stdout.write(f"\n[push] assistant> {cleaned}\nyou> ")
                        sys.stdout.flush()

            elif et == "approval_requested":
                aid = payload.get("approval_id", "")
                op = payload.get("operation", "")
                sys.stdout.write(
                    f"\n[push] 有待审批的操作: {op} (id={aid})\n"
                    f"[push] 输入 /approve {aid} 或 /deny {aid} 处理\nyou> "
                )
                sys.stdout.flush()

        with self._lock:
            self._global_seq = new_seq


# ---------------------------------------------------------------------------
#  主函数
# ---------------------------------------------------------------------------


def _fetch_latest_global_seq(client: httpx.Client, base_url: str) -> int:
    """返回当前全局事件流的最新 seq，用于避免 CLI 启动时重放旧推送。"""

    try:
        r = client.get(f"{base_url}/v1/events", params={"after_seq": 0})
        if r.status_code != 200:
            return 0
        events = r.json()
        if not isinstance(events, list):
            return 0
        seq = 0
        for e in events:
            if isinstance(e, dict):
                seq = max(seq, int(e.get("seq", 0) or 0))
        return seq
    except Exception:
        return 0

def main() -> None:
    parser = argparse.ArgumentParser(description="Clonoth Shell CLI (channel adapter)")
    parser.add_argument(
        "--supervisor",
        default=os.getenv("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765"),
        help="Supervisor base URL",
    )
    parser.add_argument("--conversation-key", default=os.getenv("CLONOTH_CONVERSATION_KEY") or None)
    args = parser.parse_args()

    base_url = args.supervisor.rstrip("/")

    workspace_root = Path(__file__).resolve().parents[1]
    runtime_cfg = load_runtime_config(workspace_root)

    conversation_key = str(args.conversation_key or "").strip()
    if not conversation_key:
        conversation_key = f"cli:{uuid.uuid4()}"
        print(f"[shell-cli] new conversation_key: {conversation_key}", flush=True)

    client_timeout_sec = get_float(runtime_cfg, "shell.http.client_timeout_sec", 10.0, min_value=1.0, max_value=120.0)
    health_timeout_sec = get_float(runtime_cfg, "shell.supervisor.health_timeout_sec", 2.0, min_value=0.5, max_value=30.0)
    wait_poll_interval_sec = get_float(
        runtime_cfg,
        "shell.supervisor.wait_poll_interval_sec",
        0.5,
        min_value=0.1,
        max_value=5.0,
    )
    events_poll_interval_sec = get_float(runtime_cfg, "shell.events_poll_interval_sec", 0.5, min_value=0.1, max_value=10.0)

    with httpx.Client(timeout=client_timeout_sec, trust_env=False) as client:
        wait_supervisor(
            client,
            base_url,
            health_timeout_sec=health_timeout_sec,
            poll_interval_sec=wait_poll_interval_sec,
        )
        print(f"[shell-cli] connected to supervisor: {base_url}", flush=True)

        session_id: str | None = None
        after_seq = 0

        push_seq = _fetch_latest_global_seq(client, base_url)
        push = _PushPoller(client, base_url, events_poll_interval_sec * 4, push_seq)
        push.start()

        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[shell-cli] exit", flush=True)
                return

            if not text:
                continue
            if text in {"/exit", "/quit", "exit", "quit"}:
                print("[shell-cli] bye", flush=True)
                return
            if text == "/clear":
                conversation_key = f"cli:{uuid.uuid4()}"
                session_id = None
                after_seq = 0
                print(f"[shell-cli] 上下文已清空，开始新对话。 conversation_key={conversation_key}", flush=True)
                continue
            if text == "/help":
                print("[shell-cli] 可用指令: /clear /cancel /exit /approve <id> /deny <id>", flush=True)
                print("  /clear   清空上下文，开始新对话", flush=True)
                print("  /cancel  取消当前正在执行的任务", flush=True)
                print("  /exit    退出", flush=True)
                print("  Ctrl+C   等待回复时按下可取消当前任务", flush=True)
                continue
            if text == "/cancel":
                if session_id:
                    try:
                        client.post(f"{base_url}/v1/sessions/{session_id}/cancel")
                        print("[shell-cli] 已发送取消请求", flush=True)
                    except Exception as e:
                        print(f"[shell-cli] 取消失败: {e}", flush=True)
                else:
                    print("[shell-cli] 没有活跃的会话", flush=True)
                continue

            # 处理推送审批
            if text.startswith("/approve ") or text.startswith("/deny "):
                parts = text.split(None, 1)
                decision = "allow" if parts[0] == "/approve" else "deny"
                aid = parts[1].strip() if len(parts) > 1 else ""
                if aid:
                    try:
                        dr = client.post(
                            f"{base_url}/v1/approvals/{aid}",
                            json={"decision": decision, "comment": "approved via shell cli"},
                        )
                        dr.raise_for_status()
                        print(f"[approval] decided: {decision}", flush=True)
                    except Exception as e:
                        print(f"[approval] failed: {e}", flush=True)
                else:
                    print("[approval] 用法: /approve <id> 或 /deny <id>", flush=True)
                continue

            # 暂停后台轮询，由主线程接管
            push.pause()

            msg_id = str(uuid.uuid4())
            r = client.post(
                f"{base_url}/v1/inbound",
                json={
                    "channel": "cli",
                    "conversation_key": conversation_key,
                    "message_id": msg_id,
                    "text": text,
                },
            )
            r.raise_for_status()
            session_id = r.json()["session_id"]

            # 首次交互时定位 inbound seq
            if after_seq == 0:
                try:
                    er0 = client.get(
                        f"{base_url}/v1/sessions/{session_id}/events",
                        params={"after_seq": 0},
                    )
                    er0.raise_for_status()
                    evts0 = er0.json()
                    if isinstance(evts0, list):
                        for e in evts0:
                            if not isinstance(e, dict):
                                continue
                            if e.get("type") != "inbound_message":
                                continue
                            p0 = e.get("payload") or {}
                            if isinstance(p0, dict) and p0.get("message_id") == msg_id:
                                try:
                                    after_seq = int(e.get("seq", 0))
                                except Exception:
                                    pass
                except Exception:
                    pass

            # 等待回复
            print("assistant> （已发送，等待系统回复...）", flush=True)
            stream_renderer = _StreamRenderer()
            streaming_active = False
            cancelled = False

            try:
                while True:
                    er = client.get(
                        f"{base_url}/v1/sessions/{session_id}/events",
                        params={"after_seq": after_seq},
                    )
                    er.raise_for_status()
                    events = er.json()

                    got_reply = False

                    for e in events:
                        after_seq = max(after_seq, int(e.get("seq", 0)))
                        et = e.get("type")
                        payload = e.get("payload") or {}

                        if et == "node_started":
                            label = payload.get("node_name") or payload.get("node_id", "")
                            print(f"[node] ▶ {label} 开始执行", flush=True)

                        elif et == "node_completed":
                            label = payload.get("node_name") or payload.get("node_id", "")
                            oc = payload.get("outcome", "")
                            sm = payload.get("summary", "")
                            print(f"[node] ✓ {label} → {oc}: {sm[:120]}", flush=True)

                        elif et == "handoff_progress":
                            msg = payload.get("message")
                            if msg:
                                print(f"[progress] {msg}", flush=True)

                        elif et == "stream_delta":
                            streaming_active = True
                            kind = payload.get("type", "text")
                            content = payload.get("content", "")
                            if content:
                                stream_renderer.on_delta(kind, content)

                        elif et == "stream_end":
                            stream_renderer.on_stream_end()
                            streaming_active = False

                        elif et == "outbound_message":
                            text_out = payload.get("text")
                            if isinstance(text_out, str) and text_out.strip():
                                if not stream_renderer.streamed_text:
                                    _print_outbound(text_out)
                                got_reply = True

                        elif et == "cancel_acknowledged":
                            print("\n[cancelled] 任务已取消", flush=True)
                            cancelled = True
                            got_reply = True

                        elif et == "approval_requested":
                            approval_id = payload.get("approval_id")
                            operation = payload.get("operation")
                            details = payload.get("details")
                            fingerprint = payload.get("fingerprint")
                            print("\n[approval requested]", flush=True)
                            print(f"  id: {approval_id}", flush=True)
                            print(f"  operation: {operation}", flush=True)
                            print(f"  fingerprint: {fingerprint}", flush=True)
                            print(f"  details: {details}", flush=True)

                            ans = input("allow? (y/N)> ").strip().lower()
                            decision = "allow" if ans in {"y", "yes"} else "deny"
                            dr = client.post(
                                f"{base_url}/v1/approvals/{approval_id}",
                                json={"decision": decision, "comment": "approved via shell cli"},
                            )
                            dr.raise_for_status()
                            print(f"[approval] decided: {decision}\n", flush=True)

                    if got_reply:
                        stream_renderer.on_stream_end()
                        break

                    poll = 0.1 if streaming_active else events_poll_interval_sec
                    time.sleep(poll)

            except KeyboardInterrupt:
                # Ctrl+C：发送取消请求
                print("\n[shell-cli] 正在取消...", flush=True)
                try:
                    client.post(f"{base_url}/v1/sessions/{session_id}/cancel")
                except Exception:
                    pass
                cancelled = True

            # 回复结束，恢复后台轮询
            push.update_seq(after_seq)
            push.resume()


if __name__ == "__main__":
    main()
