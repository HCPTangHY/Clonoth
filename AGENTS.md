# AGENTS.md

## Project Overview

Clonoth is a modular, multi-platform AI Agent framework designed for long-running agent services. It uses a Supervisor + Engine + Bot Adapter multi-process architecture.

- **Supervisor** (`supervisor/`): Process management, session/task routing, approval policies, event logging, scheduled tasks, admin API.
- **Engine** (`engine/`): AI node execution — context building, model inference, tool calls, hooks, memory, context compaction, node switching.
- **Providers** (`providers/`): Pluggable LLM provider implementations (OpenAI, Anthropic, Gemini, etc.).
- **SDK** (`clonoth_sdk/`): Client SDK for external callers — request/callback/approval/event routing.
- **Toolbox** (`toolbox/`): Tool runtime, registry, MCP client support, skill injection.
- **Bot Adapters** (`adapters/`): Platform connectors (Discord, QQ/OneBot, Web).
- **Remote Worker SDK** (`remote_worker_sdk/`): SDK for offloading tool execution to remote machines.

## Setup

```bash
# Python 3.11+ required
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and edit config
cp config.example.yaml data/config.yaml
cp policy.example.yaml data/policy.yaml

# Start
python main.py
```

Production deployments use PM2. The main process is `clonoth_runtime.py` (spawns Supervisor which spawns Engine).

## Testing

```bash
# Run all tests
pytest

# Run a specific test
pytest tests/test_hooks.py -v

# Syntax-check a file before committing
python3.11 -c "import py_compile; py_compile.compile('path/to/file.py', doraise=True)"
```

Always run `py_compile` on modified files before committing.

## Code Style

- Python 3.11+, type hints encouraged but not enforced everywhere.
- 4-space indentation, no tabs.
- Use `logging.getLogger(__name__)` for all logging.
- Prefer `async/await` for I/O-bound operations.
- Private methods prefixed with `_`. Lock-holding methods suffixed with `_locked` (e.g. `_route_completed_task_locked`).
- YAML for all configuration files. Use `yaml.safe_load` / `yaml.safe_dump`.
- No `print()` in production code — use logger.

## Architecture Conventions

### Task Lifecycle

Inbound message → Supervisor creates Task → Engine executes Node → Tool calls / LLM inference loop → Task completes → Supervisor routes result.

### Session Model

- **Session**: Long-lived conversation container, identified by `conversation_key`.
- **Entry Branch**: Isolated branch within a session for concurrent task isolation. Merges back on completion.
- **Child Session**: Independent session for dispatched sub-agents (`agent:{node_id}:{parent_conv_key}`).

### Node Types

- Regular nodes: One-shot execution per task.
- Persistent nodes (`persistent: true` in YAML): Long-lived agents with accumulating context, own memory namespace, and compression support.
- System nodes (`engine/system_nodes/`): Built-in nodes for compaction, summarization, memory extraction.

### Dispatch Modes

- `fresh`: New session each time, cleaned up after completion.
- `accumulate`: Reuses session by fixed conversation_key, history persists.
- `fork`: New session seeded with parent's conversation history.

## Key Files

| Path | Purpose |
|---|---|
| `main.py` | Entry point |
| `clonoth_runtime.py` | Runtime bootstrap (Supervisor → Engine) |
| `supervisor/task_router.py` | Core task routing, completion handling, turn summary triggering |
| `supervisor/task_store.py` | Task persistence, session management, inbound dispatch |
| `supervisor/api.py` | HTTP API endpoints (admin, inbound, tasks) |
| `engine/runner.py` | Node execution loop, TaskRecord writing |
| `engine/inference/ai_step.py` | Single LLM inference step |
| `engine/inference/pseudo_handlers.py` | Built-in pseudo-tool handlers (dispatch, finish, intermediate_reply, etc.) |
| `engine/builtin/compact.py` | Context compaction (L1/L2/L3) |
| `engine/turn_summary.py` | Turn-level summarization |
| `engine/conversation_store.py` | Conversation JSONL read/write |
| `engine/hooks/registry.py` | Hook registration and dispatch |
| `providers/llm_call.py` | Unified LLM call with retry/fallback |
| `config/nodes/*.yaml` | Node definitions (model, tools, personality) |
| `data/config.yaml` | Runtime configuration |
| `data/policy.yaml` | Approval and security policies |
| `data/workspaces.yaml` | Workspace name → path registry |
| `data/sessions.json` | Session persistence (workspace, provider override) |

## Commit Format

```
type(scope): 中文描述
```

Types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `chore`.
Scopes: `engine`, `supervisor`, `sdk`, `provider`, `toolbox`, `config`, `bot`.

Examples:
- `fix(supervisor): persistent 子节点支持轮摘要和压缩`
- `feat(engine): 增加 TaskRecord 双写逻辑`

## Code Sync Flow (Production)

All changes MUST be made in the source repo (`clonoth_original/`) first:

1. Edit files in source repo (`clonoth_original/`)
2. `py_compile` check
3. `git add -A && git commit -m 'type(scope): ...'`
4. Copy to production instances
5. Restart affected services

**Never edit production directories directly.**

Sync scope (only these directories):
- `engine/`
- `supervisor/`
- `providers/`
- `toolbox/`
- `clonoth_sdk/`
- `adapters/`

## Adding New Components

### New Provider

Create `providers/my_provider.py`, implement the provider interface, register via `ProviderRegistry`.

### New Tool

Create `tools/<name>.py` with JSON stdin/stdout protocol. Or use `create_or_update_tool` API.

### New Node

Create `config/nodes/<name>.yaml` with required fields: `id`, `name`, `description`, `system_prompt`. Optional: `model`, `provider`, `tool_access`, `persistent`, `memory_book`.

### New Hook Plugin

Create `plugins/<name>.py`, expose `PLUGIN_META` (auto-discovery) or a `register(ctx)` function receiving the EngineContext. See docs/plugin-system.md.

### New Skill

Create `skills/<name>/SKILL.md` with YAML frontmatter (strategy, keywords, enabled, etc.).

## Security Notes

- API keys and secrets are in `.env` and `data/config.yaml` — never commit these.
- Tool execution is guarded by `policy.yaml` approval rules.
- Shell commands require approval unless whitelisted.
- Node `tool_access` controls which tools each node can invoke.
