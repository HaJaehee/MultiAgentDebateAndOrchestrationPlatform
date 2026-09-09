# Bundled MCP Servers & Tool Capabilities

The platform comes pre-configured with a suite of official and open-source MCP servers. These servers provide file manipulation, knowledge retention across context truncations, git version tracking, and live Python code execution.

---

## 1. Bundled Server Roster

| Server Key | Runtime | Source Package / Repo | Tool Count | Default Status | Primary Responsibility |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `filesystem` | Node.js | `@modelcontextprotocol/server-filesystem` | 14 | **Enabled** | File read/write/search within `./workspace`. |
| `memory` | Node.js | fork of `@modelcontextprotocol/server-memory` ([`mcp_servers/memory_scoped/`](file:///d:/MultiAgentOrchestrator/mcp_servers/memory_scoped/index.mjs)) | 9 | **Enabled** | Knowledge graph persistence, one graph per conversation. |
| `git` | Python | `mcp-server-git` (pip package) | 12 | **Enabled** | Repository version control & diff tracking. |
| `sandbox` | Python | [AirgappedPySandbox](https://github.com/HaJaehee/AirgappedPySandbox) v0.4.1 | 5 | **Enabled** | Stateful IPython kernel code execution, one namespace per conversation and speaker. |
| `sequential_thinking` | Node.js | `@modelcontextprotocol/server-sequential-thinking`| 1 | *Disabled* | Step-by-step reasoning (used only when `mode = "mcp"`). |
| `fetch` | Python | `mcp-server-fetch` (pip package) | 1 | *Disabled* | Web URL to Markdown extractor. **Keep disabled in air-gaps.** |

---

## 2. Server Profiles & Capabilities

### 2.1. Filesystem Server (`filesystem`)
- **Execution**: Direct Node.js entrypoint (`dist/index.js`).
- **Scope**: Strictly restricted to `./workspace`. Any attempt to access files outside this path is rejected at the protocol layer.
- **MCP Roots Capability**: The host registers a `list_roots_callback` returning `file:///.../workspace`, declaring client Roots support to eliminate "Client does not support MCP Roots" fallback warnings.
- **Key Tools**:
  - `read_text_file`: Inspects code or configuration files.
  - `write_file`: Creates new files in the workspace.
  - `edit_file`: Performs targeted edits on existing files.
  - `directory_tree`: Inspects project directory structures recursively.
  - `search_files`: Fast regex/glob search across the workspace.

### 2.2. Memory Knowledge Graph (`memory`)
- **Execution**: Node.js entrypoint, run from `${MCP_NODE_HOME}/memory-scoped.mjs`. Persists one graph file per conversation under `./workspace/.memory-graphs/<graph_id>.jsonl`.
- **Purpose**: When debates extend over multiple rounds, earlier context may be truncated due to token budget limits. The memory graph stores verified architectural decisions and agreed constraints as graph nodes and relations.
- **Why a fork**: The official server holds one graph per process (`MEMORY_FILE_PATH`). The server process is shared by every conversation, so the next conversation read the previous one's memory. The fork moves the isolation boundary off process lifetime and onto an explicit scope the request carries — see [Per-conversation tool scope](#per-conversation-tool-scope-_meta) below. Everything else is upstream code, kept verbatim so upstream releases stay easy to re-apply.
- **Key Tools**:
  - `create_entities`: Creates entities (e.g. `Database`, `AuthService`).
  - `create_relations`: Links entities (e.g. `AuthService --[authenticates]--> UserSession`).
  - `add_observations`: Appends factual observations to existing entities.
  - `search_nodes`: Searches for stored architectural decisions.

### 2.3. Git Versioning Server (`git`)
- **Execution**: Python module (`-m mcp_server_git --repository ./workspace`).
- **Prerequisite**: Requires `./workspace` to be an initialized git repository.
- **Corporate Intranet & Git Discovery**: [`find_git_executable()`](file:///d:/MultiAgentOrchestrator/app/mcp/manager.py) locates git across custom enterprise paths (e.g. `C:\Program Files\Git\cmd\git.exe` or `%LOCALAPPDATA%\Programs\Git\cmd\git.exe`).
- **Safe Directory Protection**: Executes `git config --global --add safe.directory "*"` automatically to prevent `fatal: detected dubious ownership in repository` errors in containerized or shared drive environments.
- **Workspace Auto-Init**: [`ensure_workspace()`](file:///d:/MultiAgentOrchestrator/app/mcp/manager.py) checks for `./workspace/.git` upon startup. If missing, it initializes the repository with `git init`, creates a `.gitkeep`, and creates the initial commit automatically.
- **Key Tools**:
  - `git_status`: Checks working tree changes.
  - `git_diff`: Inspects modifications introduced during a debate round.
  - `git_commit`: Commits verified functional increments.
  - `git_log`: Inspects change history across debate rounds.

### 2.4. Python Code Execution Sandbox (`sandbox`)
- **Repository**: [HaJaehee/AirgappedPySandbox](https://github.com/HaJaehee/AirgappedPySandbox)
- **Execution**: Embedded stateful IPython execution daemon.
- **Bundled version**: v0.4.1. Vendored into `mcp_sandbox/`; the app does not fork it.
- **Namespace binding**: Every tool takes a `namespace` argument, but the host overrides it with the scope it sends in `_meta` — one namespace per conversation *and speaker* (see [Per-conversation tool scope](#per-conversation-tool-scope-_meta)). The argument only decides the namespace for clients that send no metadata.
- **Offline Kernel Requirements**: Core data science libraries (`requirements-kernel.txt`: `ipykernel`, `pandas`, `numpy`, `matplotlib`, `seaborn`, `scipy`, `sympy`) are pre-packaged into offline bundle wheels.
- **Why It Matters**: Prevents hallucination by allowing the Coder to verify implementations and empowering the Critic to test hypotheses and security boundaries with real code executions.
- **Key Tools**:
  - `execute_python_code`: Runs code snippets in an isolated namespace; maintains variables and imports across calls.
  - `run_python_file`: Executes a target script inside the workspace.
  - `reset_kernel_state`: Clears kernel state and memory when starting a clean session.

---

## 3. Agent Tool Allocation Matrix

Tools are distributed across agents according to their specialized responsibilities:

| Agent | Assigned MCP Servers | Purpose |
| :--- | :--- | :--- |
| **Orchestrator** | `filesystem`, `memory` | Read existing files, coordinate decisions, record facts. |
| **Architect** | `filesystem`, `memory`, `fetch` | Review project structure, query facts, inspect external specs. |
| **Coder** | `filesystem`, `sandbox`, `git` | Write code files, verify code execution, commit functional diffs. |
| **Critic** | `filesystem`, `sandbox`, `git`, `memory` | Inspect code, run stress/edge-case tests in sandbox, verify git diffs, record security findings. |

> **Security Note**: The `critic` is intentionally given access to the `sandbox` so that code review criticisms can be backed by empirical execution logs rather than theoretical assertions.


---

## Workspace Resolution

Four of the bundled servers are rooted at one shared directory:

| Server | How it receives the root |
| :--- | :--- |
| `filesystem` | last `args` entry (allowed directory) |
| `git` | `--repository` argument |
| `memory` | `MEMORY_GRAPH_DIR` env, a directory inside it |
| `sandbox` | `SANDBOX_WORKSPACE` env |

`WORKSPACE_DIR` is normalised to an **absolute path** at import time in
[`app/config.py`](file:///d:/MultiAgentOrchestrator/app/config.py), anchored at the project
root rather than the current working directory.

This is not cosmetic. Passing the relative `./workspace` leaves resolution to each child
process, and these are different runtimes: `filesystem` is node, `sandbox` is python. The
sandbox in particular resolves what it is given with `Path(value).resolve()` against *its own*
cwd, so `./workspace` pointed at the sandbox's install directory while `filesystem` pointed at
the project's. Two servers, one setting, two folders.

### One server group per workspace

The root is fixed at spawn time for every one of these servers, so there is no way to point a
running server at another folder. Rather than restart the shared servers whenever a session
wants a different workspace, the platform keeps **one group per workspace**:
[`MCPRuntimePool`](file:///d:/MultiAgentOrchestrator/app/mcp/pool.py) resolves the **raw**
`mcp_servers` table against that folder — re-running the same substitution is exact, whereas
string-replacing paths in already-substituted argv would not be — and starts a group for it.

Conversations in different workspaces therefore debate concurrently; conversations sharing a
workspace share the group, and stay apart inside it through the per-conversation scope in each
request's `_meta`. See
[Runtime Isolation](file:///d:/MultiAgentOrchestrator/wiki/mcp/runtime-isolation.md) for the
reference counting, idle TTL, and the runtime budget.

### The sandbox kernel's cwd (closed in v0.4.1)

Up to v0.4.0 the sandbox started its IPython kernel with `cwd=PROJECT_ROOT` — its *own*
directory — regardless of `SANDBOX_WORKSPACE`. Tool-mediated paths were fine, but a bare
`open("./workspace/x")` inside `execute_python_code` resolved against the sandbox's install
directory, so files written that way vanished from everyone else's view.

v0.4.1 starts the kernel inside the workspace and normalises a `./workspace/` prefix away, so
plain relative names (`chart.png`) now land where every other tool looks. Verified against the
bundled copy: a bare `open("bare.txt", "w")` inside executed code lands in `SANDBOX_WORKSPACE`
and nothing appears in the sandbox's own directory.
---

## Per-conversation tool scope (`_meta`)

Two servers hold state that must not leak between conversations: `memory` (the knowledge
graph) and `sandbox` (the IPython namespace). Both processes are shared by every conversation,
so neither can infer the boundary on its own.

The host supplies it. Every tool call carries the conversation id in the MCP request's `_meta`:

```
OrchestratorEngine._speak(state.session_id)
  -> LLMCaller.call_agent(session_id=...)
    -> MCPManager.execute_tool(..., scope=session_id)
      -> MCPClientConnection.execute_tool(..., scope=...)
        -> session.call_tool(name, arguments, meta={"conversationId": ..., "sessionId": ..., "graphId": ...})
```

Three key names are sent because servers look for different ones (the sandbox scans
`conversationId`/`sessionId`/`threadId`; the memory fork also accepts `graphId`).

### What is shared, and how far

The boundary differs per server, and the host is what decides it — a server cannot infer what
its state is pinned to, and a model asked to declare it will eventually forget.

| State | Boundary | Composed scope | Why |
| :--- | :--- | :--- | :--- |
| Workspace directory | shared by all | — (the path *is* the boundary) | The handoff channel between agents. Visible in `filesystem`, in git diffs, in the artifact viewer. |
| Knowledge graph (`memory`) | conversation | `<session_id>` | Agreed facts belong to every participant in that debate. |
| Kernel namespace (`sandbox`) | conversation x speaker | `<session_id>-<agent_key>` | Kernel variables are recorded nowhere. |

`MCPManager.compose_scope()` holds this policy; `AGENT_SCOPED_SERVERS` lists the servers that
get the speaker appended.

The kernel split is the non-obvious one. `_build_context_for_agent` passes only each message's
**prose** to the next speaker — tool call logs stay in the UI accordion and never enter another
agent's context. A shared kernel therefore hands the critic variables it has no record of, and
its failure mode is a stale `df` from three rounds ago read as current and reviewed with
confidence. It also cuts against why the critic has a sandbox at all: inspecting objects the
coder built interactively examines the coder's *result*, not the coder's *code*. Re-running the
file does. So agents hand off through the workspace, where the failure is a loud `NameError`
that reading the file fixes, and one agent's own state still persists across its rounds.

Cost: kernels are now needed per `concurrent debates x agents holding sandbox access`, which is
why `SANDBOX_MAX_NAMESPACES` defaults to 16. Past the cap the pool evicts the least recently
used kernel and its variables are gone.

The sandbox already preferred request metadata over its `namespace` argument, but nothing was
sending any, so that path had never run. It is live now.

**Why not a tool argument.** The model would have to remember the id and repeat it on every
call. Forgetting it is silent, and the failure it produces is exactly the one being fixed —
one conversation writing into another's state. The host already knows which conversation a
call belongs to, so it is the host's job to say so.

The memory fork resolves the graph in this order:

| Priority | Source | Notes |
| :--- | :--- | :--- |
| 1 | `_meta` (`graphId`, `conversationId`, `threadId`, `sessionId`) | What this app sends. |
| 2 | `graph_id` tool argument | For MCP clients that send no metadata. Cannot override `_meta` — a model naming another conversation's graph does not get it. |
| 3 | `unscoped-<pid>` | Fallback, with a warning on stderr. |

The fallback is deliberately **not** a shared graph. Falling back to one would reproduce the
original bug — conversations quietly reading each other — whenever scope injection breaks. A
`unscoped-<pid>.jsonl` file appearing in `.memory-graphs/` is the visible symptom instead.

If the installed `mcp` SDK predates `call_tool(meta=...)`, the host logs a warning and calls
without scope; tools keep working, isolation does not.

### Deploying the fork

The source of truth is `mcp_servers/memory_scoped/index.mjs`. The running copy must sit next
to `mcp_node/node_modules` to resolve `@modelcontextprotocol/sdk` and `zod`, so
`sync_vendored_servers()` copies it to wherever `conf.json` points (`${MCP_NODE_HOME}/memory-scoped.mjs`)
on every startup, absolutising a relative path first. `setup_mcp.py` does the same after `npm install`.
Nothing needs to be re-run after pulling an updated fork.

### Sharing a graph across conversations

Scope is a host decision, not a model one: pass a project id instead of the session id and
several conversations accumulate into one graph. The app does not expose this today — the
memory server's purpose here is continuity *within* one debate whose context gets truncated,
so session scope is the right default.
