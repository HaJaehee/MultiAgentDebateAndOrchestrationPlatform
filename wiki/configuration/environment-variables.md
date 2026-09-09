# Environment Variables & Dynamic Resolution

The MADO: Multi-Agent Debate & Orchestration Platform features a dynamic environment variable substitution engine in [app/config.py](file:///d:/MultiAgentOrchestrator/app/config.py#L41-L86). It allows a single [conf.json](file:///d:/MultiAgentOrchestrator/conf.json) to be shared seamlessly between local developer workstations, staging servers, and air-gapped production bundles without modification.

---

## 1. Variable Substitution Syntax

The configuration loader parses all strings recursively, supporting standard shell-style parameter expansion:

| Syntax | Description | Example |
| :--- | :--- | :--- |
| `${VAR}` | Replaced with the value of environment variable `VAR`. If unset, resolves to `""`. | `api_key = "${OPENAI_API_KEY}"` |
| `${VAR:-default}` | If `VAR` is unset or empty, replaced with literal `default`. | `port = "${APP_PORT:-8000}"` |
| `${A:-${B:-fallback}}` | **Nested Evaluation**: Evaluates variable `A`. If unset, falls back to evaluating variable `B`. If `B` is also unset, uses `fallback`. | `model = "${CODER_MODEL:-${LLM_MODEL:-openai/gpt-4o}}"` |

### Blank-to-None Conversion & Inheritance Protection
After substitution, an unresolved variable like `${CODER_API_BASE}` is left as an empty string `""`. If treated as a string, this empty value would overwrite the global ``llm.api_base`` setting with blank credentials.

To prevent this, [app/config.py](file:///d:/MultiAgentOrchestrator/app/config.py#L255-L261) uses a Pydantic `before` validator (`_blank_to_none`):
```python
@field_validator("api_key", "api_base", "api_version", "provider", mode="before")
@classmethod
def _blank_to_none(cls, v: Any) -> Any:
    if isinstance(v, str) and not v.strip():
        return None
    return v
```
Any variable that resolves to empty or whitespace is converted to `None`, allowing the agent to inherit the global `llm` value cleanly.

---

## 2. Automatic Python Interpreter Binding

Python-based MCP servers (e.g. `mcp-server-git` and `AirgappedPySandbox`) must run with the **exact same Python interpreter** as the application itself:

```python
# app/config.py lines 37-39
if not os.environ.get("PYTHON_BIN"):
    os.environ["PYTHON_BIN"] = sys.executable
```

### Why this is critical:
If `conf.json` defaulted to PATH `python`, launching MCP servers inside a virtual environment (`.venv`) or a portable air-gapped bundle would invoke the system's global Python interpreter instead. The server would immediately crash with `ModuleNotFoundError: No module named 'mcp_server_git'`.

By defaulting `PYTHON_BIN` to `sys.executable`, the child MCP process inherits the virtualenv, installed packages, and standard libraries of the host application automatically.

---

## 3. Environment Variables Reference Table

### 3.1. Application & Network

| Environment Variable | Default in conf.json | Purpose |
| :--- | :--- | :--- |
| `APP_HOST` (or `HOST`) | `127.0.0.1` | Network interface to bind Uvicorn server to (`${APP_HOST:-${HOST:-127.0.0.1}}`). |
| `APP_PORT` (or `PORT`) | `8000` | Port for the web interface and REST API (`${APP_PORT:-${PORT:-8000}}`). |

### 3.2. Global LLM Gateway & Provider Defaults

| Environment Variable | Default in conf.json | Purpose |
| :--- | :--- | :--- |
| `LLM_MODEL` | `openai/gpt-4o` | Default model identifier for all agents. |
| `LLM_API_BASE` | `""` (Inherits provider default) | URL of OpenAI-compatible proxy, vLLM, Ollama, or LM Studio. |
| `LLM_API_KEY` | `""` (or `${OPENAI_API_KEY}`) | Global API authentication token. |
| `LLM_API_VERSION` | `""` | API version string for Azure OpenAI. |
| `LLM_PROVIDER` | `""` | Explicit LiteLLM provider override. |

### 3.3. Per-Agent Model Overrides

| Environment Variable | Fallback Chain | Purpose |
| :--- | :--- | :--- |
| `ORCHESTRATOR_MODEL` | `${LLM_MODEL:-openai/gpt-4o}` | Model for Master Orchestrator. |
| `ORCHESTRATOR_API_BASE` | `${LLM_API_BASE}` | Custom endpoint for Master Orchestrator. |
| `ARCHITECT_MODEL` | `${LLM_MODEL:-anthropic/claude-3-5-sonnet-20241022}` | Model for System Architect. |
| `ARCHITECT_API_BASE` | `${LLM_API_BASE}` | Custom endpoint for System Architect. |
| `CODER_MODEL` | `${LLM_MODEL:-openai/gpt-4o}` | Model for Senior Python Engineer. |
| `CODER_API_BASE` | `${LLM_API_BASE}` | Custom endpoint for Senior Python Engineer. |
| `CRITIC_MODEL` | `${LLM_MODEL:-google/gemini-1.5-pro}` | Model for Security & Quality Critic. |
| `CRITIC_API_BASE` | `${LLM_API_BASE}` | Custom endpoint for Security & Quality Critic. |
| `OPENAI_API_KEY` | - | Standard OpenAI API Key. |
| `ANTHROPIC_API_KEY` | - | Standard Anthropic API Key. |
| `GEMINI_API_KEY` | - | Standard Google Gemini API Key. |

### 3.4. MCP Server Paths & Execution

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `NODE_BIN` | `node` | Node.js binary path. In portable bundles: `node_runtime\node.exe`. |
| `PYTHON_BIN` | `sys.executable` | Python binary path. In portable bundles: `python_runtime\python.exe`. |
| `MCP_NODE_HOME` | `./mcp_node` | Path where Node MCP server npm modules are located. |
| `MCP_SANDBOX_HOME` | `./mcp_sandbox` | Path to the AirgappedPySandbox repository checkout. |
| `WORKSPACE_DIR` | `./workspace` | **Default** workspace for agent filesystem I/O and git commits. A session can point at any other folder from the roster panel; each distinct folder gets its own group of MCP server processes. |
| `SANDBOX_KERNEL_PYTHON`| `PYTHON_BIN` | Python interpreter used for the IPython code sandbox execution kernel. |
| `SANDBOX_EXEC_TIMEOUT` | `60` | Execution timeout in seconds for code evaluation. |
| `SANDBOX_MAX_NAMESPACES`| `16` | Total IPython kernel budget. Namespaces are scoped per conversation **and per speaker**, so budget `concurrent debates x agents holding sandbox access` (two by default: coder and critic). Because a sandbox server now runs per workspace, the pool divides this number by `MCP_MAX_RUNTIMES` before handing it to each one — otherwise the kernels would multiply. Above the cap the least recently used kernel is shut down and its variables are gone. |

### 3.5. MCP Runtime Pool

Read at import time by [app/mcp/pool.py](file:///d:/MultiAgentOrchestrator/app/mcp/pool.py). An MCP server binds the folder it may touch at spawn time, so the platform keeps **one server group per workspace**; see [Runtime Isolation](file:///d:/MultiAgentOrchestrator/wiki/mcp/runtime-isolation.md).

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `MCP_MAX_RUNTIMES` | `4` | How many distinct workspaces may have live server groups at once. This is a memory budget, **not** a limit on concurrent debates — any number of sessions sharing a folder share one group. When the cap is reached the pool reaps idle groups first; if every group is in use, `acquire` raises `RuntimeCapacityError` naming what holds the slots rather than pointing a debate at the wrong folder. |
| `MCP_RUNTIME_IDLE_TTL` | `300` | Seconds an unreferenced group stays alive before being stopped. Consecutive turns in the same folder then start instantly instead of paying the spawn cost again (the sandbox spends seconds launching an IPython kernel). |

### 3.6. Tool-Call Safety Limits

Read once at import time by [app/mcp/client.py](file:///d:/MultiAgentOrchestrator/app/mcp/client.py). Both exist so that a single misbehaving server cannot take the whole backend with it; leave them alone unless a specific server legitimately needs more room.

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `MCP_TOOL_TIMEOUT` | `180` | Seconds a single `call_tool` may take. On expiry the call is abandoned, the session is closed and reopened, and the agent is told the tool did not answer. Without a limit an unresponsive server (a runaway script, a dropped remote, a CLI waiting on stdin) holds the debate — and the shutdown path — forever. |
| `MCP_TOOL_MAX_CHARS` | `200000` | Maximum characters kept from one tool result. Longer output keeps its head and tail with the middle elided. Servers do return tens of megabytes (a large file read, a recursive listing), and passing that on breaks token counting, the DB write and the browser in turn. |

---

### 3.7. Windows Console & Process Encoding
| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `PYTHONIOENCODING` | `utf-8` | Prevents Windows CP949 encoding crashes on console/stdio pipes. |
| `PYTHONUTF8` | `1` | Enables PEP 540 UTF-8 mode for all spawned Python subprocesses. |

---

## 4. Setting Up Local `.env` & Override Order

Create a `.env` file in the project root to configure local endpoints without modifying `conf.json`:

```bash
# Example .env for local network or gateway setup
APP_HOST=0.0.0.0
APP_PORT=8080
LLM_API_BASE=http://localhost:1234/v1
LLM_MODEL=openai/qwen2.5-coder-32b
LLM_API_KEY=sk-dummy-key
```

### Precedence Rule
Settings are resolved according to the following order:
```text
.env (Environment) ──> conf.json (File Configuration) ──> CLI Command Parameters
```
If `.env` specifies `APP_HOST` and `APP_PORT`, `conf.json` interpolates those values. However, passing explicit CLI flags (e.g. `python -m app.main --host 192.168.1.10 --port 9000`) takes the highest precedence and overrides both `.env` and `conf.json`.

