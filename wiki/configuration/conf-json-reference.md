# conf.json Configuration Reference

The [conf.json](file:///d:/MultiAgentDebateOrchestration/conf.json) file is the single source of truth for all runtime behaviors in the MADO: Multi-Agent Debate & Orchestration Platform. It dynamically configures the web application, default LLM provider options, MCP background servers, and all specialist agent personas.

The configuration file is loaded, validated, and normalized by [app/config.py](file:///d:/MultiAgentDebateOrchestration/app/config.py) using the standard-library `json` module and Pydantic v2.

---

## 1. High-Level Schema Structure

```json
{
  "app": { },
  "llm": {
    "sequential_thinking": { }
  },
  "mcp_servers": {
    "<name>": { }
  },
  "agents": {
    "<key>": {
      "sequential_thinking": { }
    }
  },
  "tool_security": { },
  "skills": { }
}
```

| Object | Purpose |
| :--- | :--- |
| `app` | Application network and storage settings |
| `llm` | Global LLM settings inherited by all agents |
| `llm.sequential_thinking` | Global default for step-by-step reasoning |
| `mcp_servers.<name>` | External/local MCP server processes (stdio) |
| `agents.<key>` | Specialist agent definitions (override `llm`) |
| `agents.<key>.sequential_thinking` | Optional per-agent reasoning overrides |
| `tool_security` | Allow / ask / deny for MCP tool calls |
| `skills` | Where skill folders live and which are turned off |

### 1.1. Comments

JSON has no comment syntax, so the loader treats **any key beginning with `//` as
documentation** and strips it before validation ([`strip_comment_keys`](file:///d:/MultiAgentDebateOrchestration/app/config.py)).
The value is a string, or an array of strings for a multi-line note. Because the
writers read and rewrite the raw file, these notes survive every edit made from
the roster panel.

```json
"// filesystem": [
  "공용 작업 공간 파일 I/O (공식 서버, 도구 14종).",
  "지정한 디렉터리 밖 경로는 서버가 자체적으로 차단합니다."
],
"filesystem": { "command": "${NODE_BIN:-node}", "args": ["..."], "enabled": true }
```

### 1.2. Multi-line text

Any long text field — `system_prompt`, `prompt_template` — accepts either a plain
string or an **array of strings**, which the loader joins with newlines. The
writers emit the array form whenever the text has more than one line, so prompts
stay readable in the file instead of collapsing into `
` escapes.

```json
"system_prompt": [
  "당신은 수석 소프트웨어 아키텍트입니다.",
  "확장성과 유지보수성을 고려하여 구조를 제안하세요."
]
```

### 1.3. Formatting

Writes go through [`write_conf_file()`](file:///d:/MultiAgentDebateOrchestration/app/config.py), which emits
`json.dumps(..., ensure_ascii=False, indent=2)` and swaps the file in with `os.replace()`. The shipped
[conf.example.json](file:///d:/MultiAgentDebateOrchestration/conf.example.json) adds blank lines between
sections for readability; the first edit made from the roster panel normalizes them away. Only
whitespace is affected — every `//` note, value, and key order is carried through.

---

## 2. Section Specifications

### 2.1. `app` Object
Configures the FastAPI and NiceGUI host environment.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `host` | `str` | `"127.0.0.1"` | IP address to bind Uvicorn server to. |
| `port` | `int` | `8000` | HTTP port for the web interface and API. |
| `db_url` | `str` | `"sqlite+aiosqlite:///./multiagent.db"` | SQLAlchemy async database connection URI. |
| `debug` | `bool` | `true` | Enables FastAPI auto-reload and verbose database logging. |

### 2.2. `llm` Global Object
Defines system-wide defaults. Any agent that does not explicitly set an attribute inherits the value defined here.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `model` | `str` | `"openai/gpt-4o"` | Default model identifier in LiteLLM format (`<provider>/<model_name>`). |
| `api_base` | `str` | `null` | Base URL for LLM API requests. Accepts aliases `api_url` or `base_url`. |
| `api_key` | `str` | `null` | Global API key (can reference environment variables via `${OPENAI_API_KEY}`). |
| `api_version` | `str` | `null` | API version string (required for Azure OpenAI deployments). |
| `provider` | `str` | `null` | Force LiteLLM provider (e.g. `"openai"`, `"azure"`, `"ollama"`, `"vertex_ai"`). |
| `temperature` | `float` | `0.4` | Default sampling temperature (range: `0.0` to `2.0`). |
| `top_p` | `float` | `null` | Nucleus sampling probability cutoff. |
| `max_tokens` | `int` | `4096` | Maximum generation token budget per response. |
| `max_context_window`| `int` | `128000` | The model's context window. The transcript is trimmed to fit before every call — **set this to the endpoint's real limit**, or the endpoint answers with 400 instead. |
| `timeout` | `float` | `600.0` (example) | Seconds to wait **between response chunks** (LiteLLM passes it to aiohttp as `sock_read`), not a limit on the whole response. A tool call that writes a long file can stream nothing until its arguments are complete, so set it above `max_tokens ÷ generation tokens per second` (16,000 ÷ 30 ≈ 530 s → 600). Too short, and the speech is cut mid-generation with `MidStreamFallbackError … Timeout on reading data from socket`. Unset, LiteLLM's own default applies. |
| `num_retries` | `int` | `2` | Number of automatic retries on network/rate-limit failure. |
| `drop_params` | `bool` | `true` | Silently drops unsupported parameters for local model compatibility. |
| `max_tool_iterations`| `int` | `30` | Maximum number of consecutive tool-call loops per agent turn (1-100). Exhausting it raises `LLMUnavailableError` rather than returning a placeholder answer. |
| `extra_headers` | `dict` | `{}` | Custom HTTP headers sent with every LLM request (e.g. gateway auth). |
| `extra_body` | `dict` | `{}` | Custom JSON body fields sent with requests. |

#### Inheritance Rules ([app/config.py](file:///d:/MultiAgentDebateOrchestration/app/config.py#L148-L175))
- If an agent leaves an inheritable field empty or whitespace-only (e.g., `${LLM_API_BASE}` with no value in the environment), the field resolves to `None` and inherits from `llm`.
- Setting any alias in an alias group (e.g., `api_base`, `api_url`, `base_url`) overrides the entire group.

### 2.3. Sequential Thinking Configuration (`llm.sequential_thinking`)
Controls step-by-step cognitive reasoning before generating final responses.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `enabled` | `bool` | `false` | Enables sequential thinking for agents inheriting this section. |
| `mode` | `str` | `"prompt"` | Strategy mode: `"prompt"`, `"native"`, or `"mcp"`. |
| `max_steps` | `int` | `5` | Maximum reasoning steps (`1` to `50`). |
| `show_steps` | `bool` | `true` | If `false`, hides reasoning steps in UI and displays only final conclusions. |
| `reasoning_effort` | `str` | `null` | Native mode effort: `"minimal"`, `"low"`, `"medium"`, `"high"`. |
| `thinking_budget_tokens`| `int`| `null` | Native mode extended thinking token budget (Anthropic models). |
| `mcp_server` | `str` | `"sequential_thinking"` | MCP server identifier providing the `sequentialthinking` tool. |
| `prompt_template` | `str` | `...` | Custom reasoning protocol template supporting `{max_steps}` replacement. |

### 2.4. `mcp_servers.<name>` Object
Declares external MCP server processes launched and monitored by the backend.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `command` | `str` | Required | Executable command (`node`, `python`, etc.). Supports `${NODE_BIN:-node}`. |
| `args` | `list[str]`| `[]` | Command-line arguments passed to the server process. |
| `env` | `dict[str, str]`| `{}` | Process environment variables injected into the child process. |
| `enabled` | `bool` | `true` | When `false`, the server is skipped during startup. |

> **Important**: Never use `npx` in air-gapped or offline production environments, as `npx` attempts to reach the npm registry if packages are not in the current working directory. Execute entrypoint scripts directly with `node` (e.g. `node ./mcp_node/node_modules/.../dist/index.js`).

### 2.5. `agents.<key>` Object
Configures specialist agents in the agent pool.

> These objects can be added, edited, and removed from the roster panel without restarting the app.
> Every writer reads the raw file, edits the parsed dictionary, and rewrites it in one atomic
> `os.replace`, so `//` notes and `${VAR}` placeholders survive untouched. See
> [roster-editing.md](../agents/roster-editing.md).

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `name` | `str` | Required | Display name of the agent. |
| `role` | `str` | Required | Role title (e.g. `"System Architect"`, `"Senior Python Engineer"`). |
| `enabled` | `bool` | `true` | When `false`, the agent is omitted from the pool. |
| `model` | `str` | Inherited | Model override. |
| `api_base` | `str` | Inherited | Custom endpoint override. |
| `api_key` | `str` | Inherited | Custom API key override. |
| `temperature` | `float` | Inherited | Custom sampling temperature override. |
| `max_tokens` | `int` | Inherited | Token budget override. |
| `allowed_mcp_servers` | `list[str]` | `[]` | List of MCP server keys this agent is authorized to call. |
| `allowed_skills` | `list[str]` | `[]` | Skill names (folders under `skills.dir`) this agent may load. Frozen into a started conversation like `allowed_mcp_servers`; the skills themselves stay live. See [Skills](../agents/skills.md). |
| `debate_priority` | `int` | `100` | Speaking order within a round; lower speaks first. Ties keep `conf.json` order, so leaving every agent at the default speaks in file order. Rewritten as `10, 20, 30…` when cards are dragged in the roster. |
| `debate_stance` | `str` | `"neutral"` | `"proponent"` / `"critic"` / `"neutral"`. Read only by the adversarial strategy, which alternates the two sides. If no agent declares a side, that strategy degrades to a single priority-ordered pass. |
| `system_prompt` | `str \| list[str]` | `""` | Base persona instruction and behavioral guidelines. An array is joined with newlines. |
| `card_color` | `str` | `null` | Card colour: `"#rrggbb"` or a Quasar palette name (`"teal-8"`). Drives the avatar, the role badge and the card border. Omit it and the colour is derived from the agent key. Also accepted spelled `color`. |
| `icon` | `str` | `null` | A Material icon name (`"query_stats"`), or a path to an image relative to the project root (`"data/agent_icons/coder-1a2b3c4d5e.png"`). Omit it and the icon is derived from the agent key. Also accepted spelled `avatar` or `icon_path`. |
| `sequential_thinking` | `dict` | Inherited | Per-agent sequential thinking overrides (keys merge with `llm`). |

Neither appearance key is inherited from `llm` — a value shared by every agent would defeat the point
of having one. Both are chosen from the UI and written back here; clearing one removes the key (and
its legacy spelling) so the agent returns to key-derived styling.

### 2.4.1. Icon images

Uploading an image from the **에이전트 추가** dialog or the persona editor copies it into
`data/agent_icons/` under the project root — created on demand — and stores only the relative path,
so the folder can be moved or transferred to an air-gapped machine without breaking the config. The
filename is `<key>-<content-sha1[:10]>.<ext>`, which removes both path traversal and name collisions,
and makes re-uploading the same image a no-op. Accepted: png · jpg · gif · webp · svg · bmp · ico, up
to 2 MB.

If the file cannot be found — deleted, mistyped, or config moved without its images — the agent falls
back to its key-derived icon rather than showing a broken avatar. See
[Agent Pool §1.1](../agents/agent-pool-and-roles.md).

```json
"data_scientist": {
  "name": "Data Scientist",
  "role": "Data Analysis & ML Pipeline",
  "card_color": "#0097a7",
  "icon": "query_stats",
  "allowed_mcp_servers": ["filesystem"],
  "system_prompt": "Owns data-pipeline design and ML architecture review."
}
```

### 2.6. `tool_security` Object

Allow / ask / deny for every MCP tool call. The full design — actions, trust, the code scan, hard
protections and the approval card — is in [Tool Security](../mcp/tool-security.md).

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `mode` | `str` | `"default"` | Default mode for conversations: `read_only`, `default`, `review`, `auto`. Each conversation can pick its own in the roster panel. |
| `approval_timeout` | `float` | `180` | Seconds an approval card waits; no answer denies (10–3600). |
| `deny` | `list[str]` | built-in list | Rules that always deny. Writing this key **replaces** the built-in list (secret files, capture services). The card's "항상 거부" appends here and copies the built-in list in first when the key is missing. |
| `ask` | `list[str]` | `[]` | Rules that always ask, even when an allow rule matches. |
| `allow` | `list[str]` | `[]` | Rules that run without asking. The card's "항상 허용" appends here (server PC only). |
| `trusted_servers` | `list[str] \| null` | `null` | Servers whose tool names and annotations are believed. `null` = local servers and remote servers on this PC. |
| `agents` | `dict[str, object]` | `{}` | Per-agent tightening: `mode`, `deny`, `ask` (no `allow`). The stricter mode wins. |

Rule syntax: `read(glob)`, `write(glob)`, `delete(glob)`, `exec`, `exec(regex:…)`, `net(host)`,
`mcp(server/tool)`, `mcp(server)`. Every rule is validated at load; a bad line fails the load and the
error lists all of them.

```json
"tool_security": {
  "mode": "default",
  "deny": ["read(**/.env)", "read(**/.ssh/**)", "net(webhook.site)"],
  "allow": ["net(docs.python.org)"],
  "agents": { "critic": { "mode": "read_only" } }
}
```

> Per-agent security is **not** under `agents.<key>`: agent settings freeze into each conversation's
> snapshot, and security must stay live. MCP servers no longer inherit MADO's secret environment
> variables; declare a secret a server needs in its `env` block.

### 2.7. `skills` Object

Skill folders are the skills (`<dir>/<name>/SKILL.md`); this object only says where they are and which
are off. Both apply to started conversations from their next speech. Full design: [Skills](../agents/skills.md).

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `dir` | `str` | `"skills"` | Skills folder. Relative paths resolve against the project root. The source update package replaces the default `skills/` wholesale — point elsewhere to keep your own. |
| `disabled` | `list[str]` | `[]` | Skills turned off. Anything not listed is on. Written by the roster's skill switches. |

`mcp_servers.skills` is rejected: `skills__` is the prefix of the built-in skill tools.

---

## 3. Live Mode vs. Unconfigured Agents

Each agent computes an `is_live` property dynamically ([app/config.py](file:///d:/MultiAgentDebateOrchestration/app/config.py#L269-L277)):

```python
@property
def is_live(self) -> bool:
    if self.api_base:
        return True
    if self.api_key and self.api_key.strip():
        return True
    # Local runtimes that need neither an API key nor an explicit URL
    return self.model.split("/", 1)[0] in {"ollama", "ollama_chat", "lm_studio"}
```

- **Live Execution**: If `api_base`, `api_key`, or a local provider prefix (`ollama/`, `lm_studio/`) is present, real network requests are dispatched via LiteLLM.
- **Keyless Local Endpoints**: When connecting to local servers without an API key (e.g. `vLLM` or `LM Studio` at `http://localhost:1234/v1`), LiteLLM requires a non-empty key parameter. The caller automatically injects a placeholder (`sk-no-key-required`) so local calls succeed.
- **Unconfigured Agents**: If an agent has neither an endpoint nor a key, its turn raises `LLMUnavailableError` and the debate records an explicit "연결 끊김" message in its place. Nothing is invented to fill the gap — see [llm-integration.md](../agents/llm-integration.md).

---

## 4. Configuration Precedence & Override Hierarchy

The platform applies settings through a strictly defined 3-tier precedence hierarchy:

```text
Environment Variables (.env) ──> conf.json ──> Command-Line Arguments (CLI)
      (Lowest precedence)         (Base)              (Highest precedence)
```

1. **`.env` Environment Variables**:
   Variables defined in `.env` (or inherited from the parent shell) provide base configuration defaults and sensitive credentials.
2. **`conf.json` File Configuration**:
   References environment variables via syntax such as `"host": "${APP_HOST:-${HOST:-127.0.0.1}}"` and `"port": "${APP_PORT:-${PORT:-8000}}"`. If the environment variable exists, it is substituted; otherwise, the default fallback is used.
3. **Command-Line Parameters (`app.main`)**:
   CLI arguments (`--host`, `--port`, `--config`, `--reload`, `--no-reload`) supersede any values found in both `.env` and `conf.json`. For example:
   ```bash
   python -m app.main --host 0.0.0.0 --port 9000 --config custom_conf.json
   ```
   binds to `0.0.0.0:9000` regardless of values defined in `.env` or `custom_conf.json`.

