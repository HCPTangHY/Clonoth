from __future__ import annotations

import argparse
import os
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

    # CLI 默认每次启动都使用一个新的 conversation_key -> 新 session。
    # 这样不会把历史积压事件/审批带到新一轮交互里。
    # 如果用户希望复用旧会话，可显式传 --conversation-key 或设置环境变量 CLONOTH_CONVERSATION_KEY。
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

    # trust_env=False: 避免环境代理变量影响本地 127.0.0.1 通信
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
                print("[shell-cli] 可用指令: /clear (清空上下文) /exit (退出)", flush=True)
                continue

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

            # 如果这是 CLI 本次启动后的第一次交互：
            # 我们不希望把历史积压事件全部打印出来（尤其是旧的 approval_requested 会导致 y/n 卡住）。
            # 但也不能把 after_seq 直接跳到当前最大 seq —— 否则可能错过“本条消息”产生的快速回复。
            #
            # 解决方案：定位本次 inbound_message 的 seq（通过 message_id 匹配），然后从该 seq 之后开始拉取。
            if after_seq == 0:
                try:
                    er0 = client.get(
                        f"{base_url}/v1/sessions/{session_id}/events",
                        params={"after_seq": 0},
                    )
                    er0.raise_for_status()
                    evts0 = er0.json()

                    inbound_seq = None
                    if isinstance(evts0, list):
                        for e in evts0:
                            if not isinstance(e, dict):
                                continue
                            if e.get("type") != "inbound_message":
                                continue
                            payload0 = e.get("payload") or {}
                            if not isinstance(payload0, dict):
                                continue
                            if payload0.get("message_id") != msg_id:
                                continue
                            try:
                                inbound_seq = int(e.get("seq", 0))
                            except Exception:
                                inbound_seq = None

                    if inbound_seq is not None and inbound_seq > 0:
                        after_seq = inbound_seq
                except Exception:
                    pass

            # Wait for at least one assistant outbound message, while also showing progress & approvals.
            print("assistant> （已发送，等待系统回复...）", flush=True)
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
                        nid = payload.get("node_id", "")
                        nname = payload.get("node_name", "")
                        label = nname or nid
                        print(f"[node] ▶ {label} 开始执行", flush=True)

                    if et == "node_completed":
                        nid = payload.get("node_id", "")
                        nname = payload.get("node_name", "")
                        oc = payload.get("outcome", "")
                        sm = payload.get("summary", "")
                        label = nname or nid
                        print(f"[node] ✓ {label} → {oc}: {sm[:120]}", flush=True)

                    if et == "handoff_progress":
                        msg = payload.get("message")
                        if msg:
                            print(f"[progress] {msg}", flush=True)

                    if et == "outbound_message":
                        text_out = payload.get("text")
                        if isinstance(text_out, str) and text_out.strip():
                            _print_outbound(text_out)
                            got_reply = True

                    if et == "approval_requested":
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
                    break

                time.sleep(events_poll_interval_sec)


if __name__ == "__main__":
    main()
