# Clonoth SDK

Pure protocol layer that encapsulates all communication between a Bot adapter and the Clonoth Supervisor. The SDK handles HTTP API calls, event polling, protocol state management, and approval logic — so adapters only need to implement platform-specific operations (sending messages, editing UI, etc.).

The SDK has **zero platform dependencies** (no discord.py, no telegram, etc.). Platform concerns live in the adapter.

## Architecture

Four layers, bottom to top:

| Layer | Class | Role |
|-------|-------|------|
| **HTTP Client** | `ClonothClient` | 11 typed async methods wrapping every Supervisor HTTP endpoint |
| **State** | `SessionState` | Centralized runtime state — replaces scattered global dicts (triggers, session maps, watermarks, task states) |
| **Event Router** | `EventRouter` | Polls `/v1/events`, dispatches 18 event types through protocol handlers, then notifies the adapter |
| **Callbacks** | `AdapterCallbacks` | 15-method `Protocol` interface — the adapter implements these to perform platform operations |

Supporting modules: `BotConfig` (configuration injection), `types` (dataclasses for API responses), `approval` (dedup + path classification + auto-approve).

## Two-Layer Hook Architecture

```
  Supervisor event stream
        │
        ▼
  ┌─ Layer 1: on_raw_event hook ──────────────────────┐
  │  Adapter-registered interceptor.                   │
  │  Runs BEFORE SDK default processing.               │
  │  Return 'handled' → SDK skips this event.          │
  │  Return None → SDK continues to Layer 2.           │
  │  Use case: stream_delta animation, custom events.  │
  └────────────────────────────────────────────────────┘
        │
        ▼
  ┌─ SDK protocol processing ─────────────────────────┐
  │  Trigger matching, state updates, dedup,           │
  │  watermark advancement, approval classification.   │
  └────────────────────────────────────────────────────┘
        │
        ▼
  ┌─ Layer 2: AdapterCallbacks ───────────────────────┐
  │  SDK calls the appropriate callback method.        │
  │  Adapter performs platform I/O (send, edit, etc.). │
  └────────────────────────────────────────────────────┘
```

## Quick Start

```python
import asyncio
import sys
sys.path.insert(0, "/www/wwwroot/Clonoth")

from clonoth_sdk import (
    BotConfig, ClonothClient, SessionState,
    EventRouter, AdapterCallbacks,
)

# 1. Configuration
config = BotConfig(
    base_url="http://127.0.0.1:8765",
    entry_node_id="my_main_node",
    conversation_key_prefix="discord",
)

# 2. Core objects
client = ClonothClient(config.base_url)
state = SessionState()

# 3. Implement AdapterCallbacks (all 15 async methods)
class MyAdapter:
    async def send_reply(self, trigger, text, attachments, *, main_state=None):
        print(f"Reply: {text}")
    # ... implement remaining 14 callbacks ...

callbacks = MyAdapter()

# 4. Create router and start event loop
router = EventRouter(client, state, callbacks, config)

# Optional: register Layer 1 hook for stream_delta handling
async def my_hook(event):
    if event.type == "stream_delta":
        # custom animation logic
        return "handled"  # skip SDK default
    return None  # let SDK handle

router.set_raw_event_hook(my_hook)

# 5. Run (blocks until cancelled or router.stop())
asyncio.run(router.run())
```

## File Inventory

| File | Description |
|------|-------------|
| `__init__.py` | Public API surface — re-exports all user-facing symbols |
| `client.py` | `ClonothClient` — async HTTP client for 11 Supervisor API endpoints |
| `state.py` | `SessionState` + dataclasses (`TriggerInfo`, `MainTaskState`, `ChildTaskState`) — centralized runtime state |
| `callbacks.py` | `AdapterCallbacks` — 15-method `typing.Protocol` the adapter implements |
| `event_router.py` | `EventRouter` — poll loop, 18 event handlers, Layer 1/2 dispatch, `strip_protocol_markers()` |
| `config.py` | `BotConfig` — configuration dataclass injected into router and client |
| `types.py` | `Event`, `InboundResult`, `RunningTask`, `HealthInfo`, `OpenAIConfig` — API response types |
| `approval.py` | `ApprovalTracker` (dedup), `classify_path` / `is_external_operation` (path classification), `auto_approve` (retry logic) |

## SDK Boundary

**Inside SDK:**
- `ClonothClient` — all Supervisor HTTP API communication
- Data types — `InboundResult`, `Event`, `RunningTask`, etc.
- Approval policy — dedup, path classification, auto-approve
- `SessionState` — trigger lifecycle, session mapping, watermarks, task states
- `EventRouter` — event polling, protocol dispatch, 18 event handlers
- `AdapterCallbacks` — callback protocol definition
- `BotConfig` — configuration injection
- Protocol marker cleanup — `[CLONOTH_TOOL_TRACE]` stripping

**Outside SDK (adapter responsibility):**
- `[SPLIT]` message segmentation, `[REACT:xxx]` reaction extraction, `[BOT_RESTART]` signal handling
- `TextProcessor` / protocol marker display formatting
- Platform libraries (discord.py, telegram, etc.)
- Dot animation, typing throttle, streaming preview — display-layer logic
- Channel history queue management
- UI components (approval buttons, cancel buttons, embeds)

## Restart Scope

Modifying any file in `clonoth_sdk/` only requires restarting the **Bot process**. The Clonoth Supervisor backend is unaffected — no engine restart, no session loss.
