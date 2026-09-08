# MCP Error Handling, Diagnostics & Resilience

External subprocess communications are inherently vulnerable to runtime disruptions (e.g., process crashes, environment misconfigurations, and invalid tool arguments). The platform implements robust diagnostic and fault-tolerance patterns in [app/mcp/client.py](file:///d:/MultiAgentOrchestrator/app/mcp/client.py).

---

## 1. Two Classes of Tool Failures

MCP distinguishes between communication protocol failures and semantic tool execution failures:

| Category | Transport Representation | System Handling | LLM Context Injection |
| :--- | :--- | :--- | :--- |
| **Protocol Error** | JSON-RPC error or broken pipe | Logged as warning; triggers [`MCPToolError`](file:///d:/MultiAgentOrchestrator/app/mcp/client.py#L18-L32) | Marked as `status: "error"`; returns error message string. |
| **Tool Execution Error**| Valid JSON-RPC response with `isError: true` | Recorded with `status: "error"`; keeps process alive | **Raw server error text is injected verbatim** into LLM context. |

### Preserving Verbatim Error Text for LLM Self-Correction
When an agent passes an invalid path (e.g. `write_file(path="../outside.py")`) and the server responds with:
```json
{
  "isError": true,
  "content": [{"type": "text", "text": "Path traversal rejected: path must be inside workspace"}]
}
```
The platform **never obfuscates or replaces this message with a generic error**. The LLM reads the exact failure string in its observation message, analyzes the mistake, adjusts the parameters, and successfully re-executes the tool within the same turn.

---

## 2. Stderr Diagnostics via `_StderrTee`

When an MCP subprocess crashes during startup (e.g., due to a missing Python package or syntax error in a custom server), the `anyio` async framework raises a generic exception:
```text
ExceptionGroup: unhandled errors in a TaskGroup
```
This generic message contains zero diagnostic value. The actual root cause (`ModuleNotFoundError: No module named 'xyz'`) was written by the child process directly to `stderr`.

### The `_StderrTee` Solution ([app/mcp/client.py](file:///d:/MultiAgentOrchestrator/app/mcp/client.py#L52-L111)):
1. Creates an OS pipe (`os.pipe()`) and attaches the write descriptor to the subprocess's `stderr`.
2. Spawns a background daemon thread that pumps the read descriptor to the main application's console while maintaining a ring buffer of:
   - **Head lines** (first 4 lines: typically the immediate failure statement, e.g. `Cannot find module ...`).
   - **Tail lines** (last 8 lines: the stack trace root).
3. Stores this diagnostic string in `MCPClientConnection.connect_error`.
4. Renders the exact error trace inside a hover tooltip on the UI's server status chip:

```mermaid
flowchart LR
    Child[MCP Subprocess] -->|stderr pipe| Pipe[os.pipe Descriptor]
    Pipe --> Pump[Daemon Thread _StderrTee]
    Pump --> Console[Console sys.stderr]
    Pump --> Buffer[Head/Tail Ring Buffer]
    Buffer --> Tooltip[UI Roster Hover Tooltip]
```

---

## 3. Automatic Reconnection & Safety

When a tool invocation fails because the underlying stdio process died:
- [`MCPClientConnection.execute_tool()`](file:///d:/MultiAgentOrchestrator/app/mcp/client.py) detects the broken pipe and attempts **exactly one automatic reconnection**.
- If reconnection succeeds, the call proceeds.
- If a tool invocation fails logically (`isError: true` with a live server), **no retry is attempted**. Automatically retrying side-effecting operations (such as file appending or git commits) could cause duplicate operations or state corruption.
- Uvicorn's file watcher explicitly excludes `workspace/`, `*.db*`, and `.git/` so that file operations performed by sandbox or filesystem tools do not trigger false hot-reloads and application restarts.

---

## 4. The Tool-Failure Boundary (v0.5.0)

Before v0.5.0 a tool error could take the speaking agent down with it, and occasionally the
backend process too. The defence was a single layer with two holes:

- **`except Exception` does not catch `BaseExceptionGroup`.** That is precisely what the
  `anyio`-based stdio paths raise, so it passed straight through `MCPManager`,
  `engine._speak()`, and `DebateRunner` alike. The debate task died leaving no trace and
  the screen sat on "토론 중..." forever.
- **`except (Exception, BaseException)` swallowed `CancelledError`.** Cancellation then did
  nothing, `runner.shutdown()` waited on a task that would never stop, the lifespan hung,
  and Uvicorn was force-killed — which looks exactly like a crashed server.

The rule is now stated in one place and enforced at every layer:

> **A tool failure is an observation for the agent to read and correct — never a reason to
> end a turn, a debate, or the process. Cancellation is the sole exception: it is an
> instruction, not a failure, and must always propagate.**

| Layer | Guarantee |
| :--- | :--- |
| [`MCPManager.execute_tool()`](file:///d:/MultiAgentOrchestrator/app/mcp/manager.py) | **The boundary.** Returns `(text, "error")` for anything that goes wrong. Re-raises `CancelledError` only. |
| [`LLMCaller._execute_tool_safely()`](file:///d:/MultiAgentOrchestrator/app/agents/llm.py) | Enforces the same rule again in case the manager is replaced (test doubles) or lookup itself throws. |
| [`LLMCaller._parse_tool_call()`](file:///d:/MultiAgentOrchestrator/app/agents/llm.py) | Absorbs malformed `tool_calls` — object or dict shape, broken argument JSON, missing `tool_call_id` (an empty id makes the *next* request 400). |
| [`LLMCaller._notify_tool_call()`](file:///d:/MultiAgentOrchestrator/app/agents/llm.py) | A dead browser callback cannot discard the observation of a tool that actually ran. |
| [`engine._speak()`](file:///d:/MultiAgentOrchestrator/app/orchestration/engine.py) | Catches `BaseException`, records a `msg_type="error"` turn, and lets the other agents continue. A failed DB commit rolls back so the session is not left broken. |
| [`DebateRunner`](file:///d:/MultiAgentOrchestrator/app/orchestration/runner.py) | Reports `BaseExceptionGroup` as `failed` instead of dying silently. `cancel()` uses `asyncio.wait` with a 20 s cap so shutdown is never blocked by a task that ignores cancellation. |
| [`app/main.py`](file:///d:/MultiAgentOrchestrator/app/main.py) | Installs an event-loop exception handler and `threading.excepthook`; MCP init failure no longer prevents the app from starting. |

### Time and size limits

Two limits stop a single server from taking the whole debate hostage. Both are read once at
import from the environment (see
[Environment Variables §3.5](file:///d:/MultiAgentOrchestrator/wiki/configuration/environment-variables.md)):

| Variable | Default | Behaviour on breach |
| :--- | :--- | :--- |
| `MCP_TOOL_TIMEOUT` | `180` s | The call is abandoned, the session is closed and reopened (a late reply must not mix into the next call's answer), and the agent is told the tool did not answer. |
| `MCP_TOOL_MAX_CHARS` | `200000` | Head and tail are kept, the middle elided. Servers really do return tens of megabytes; passing that on breaks token counting, the DB write, and the browser in turn. |

Without a timeout, an unresponsive server — a runaway script, a dropped remote, a CLI
waiting on stdin — holds the turn open forever, and cancellation cannot reach a coroutine
that is simply stuck.

### Process teardown by handle, not pid

Teardown terminates the child through the **process object** captured from the stdio
context, and only when `returncode is None`. The pid is a fallback used solely when the
object could not be captured.

The distinction is not cosmetic. If the child has already exited, `os.kill(pid, ...)` may
hit **a different process that the OS assigned that pid in the meantime** — Windows recycles
pids aggressively, and a plausible occupant is a Uvicorn reloader worker. Cleaning up one
MCP server could take the server down with it.

---

## 5. Real-Time Connection Monitoring

The system exposes connection states via [`MCPManager.connection_status()`](file:///d:/MultiAgentOrchestrator/app/mcp/manager.py#L210-L230) and the `GET /api/mcp` endpoint:

| Status Chip | Visual Indicator | Meaning & Health State |
| :--- | :--- | :--- |
| 🟢 `filesystem 도구 14` | Green Chip | Connected and healthy; shows registered tool count. |
| 🟠 `연결 끊김` | Orange Chip | Subprocess terminated; will auto-reconnect on next call. |
| 🔴 `연결 실패` | Red Chip | Process startup failed; hover displays stderr diagnostic trace. |
| ⚪ `비활성` | Grey Chip | Server explicitly disabled (`"enabled": false` in `conf.json`). |

A **"Refresh"** button in the UI header invokes `mcp_manager.reconnect_disconnected()`, re-attempting initialization solely for failed servers without interrupting healthy ones.
