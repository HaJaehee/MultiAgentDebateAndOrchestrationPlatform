# Host Tools and Plugins (ADR-042)

Host tools run inside the tool loop instead of in an MCP server process. To the model they look like any
other tool: `prefix__name`. They exist because some tools cannot live in another process: the list changes per
agent and speech (`skills`), they ask a person (`ask`), they call another agent (`peers`), or they change the
current request's `tools` (`tools`).

Code: [app/agents/host_tools.py](file:///d:/MultiAgentDebateOrchestration/app/agents/host_tools.py) (registry),
[app/plugins.py](file:///d:/MultiAgentDebateOrchestration/app/plugins.py) (loader). Tests: `tests/test_host_tools.py`,
`tests/test_plugins.py`. Decision: [ADR-042](../../lectures/05-adr/ADR-042-plugins.md).

## 1. The Registry

One `HostToolProvider` answers everything the pipeline asks about its tools. Call sites only talk to the registry.

| Step | Registry function | Provider method | Used by |
| :--- | :--- | :--- | :--- |
| Which tools go out | `host_tools_for(agent)` | `enabled_for`, `tools_for` | `LLMCaller`, `ConversationMemory.tools_for` |
| What the prompt says | `host_tool_guidance(tools)` | `guidance` | `build_system_prompt` |
| Who handles a call | `find_host_tool(name)` | `owns` | `_run_one_tool`, `_execute_tool_safely` |
| How it is judged | `host_tool_meta(name)` | `gated`, `risk_of` | `ToolGate` |
| Running it | `run_host_tool(provider, call)` | `run` | `_execute_tool_safely` |
| Names that are not servers | `host_tool_switches()` | `assignable` | `resolve_tool_servers`, roster |

Registration order is the order definitions and guidance go out in, so the built-ins keep the prompt cache:
`skills`, `peers`, `ask`, `tools`, then plugins.

| | Built-in | Plugin |
| :--- | :--- | :--- |
| Tool gate | Skipped (`gated = False`) | **Judged** |
| Switched on by | Its own rule: `allowed_skills`, the session switch, `allowed_mcp_servers` for `peers` | `allowed_mcp_servers` |
| Bare name (`lookup`) | Accepted, models drop prefixes | Refused, it must not steal an MCP tool of that name |

## 2. Writing a Host Tool

Put a file in `host_tools/` and restart. Nothing else changes. Working examples to copy are in
[docs/examples/plugins/](../../docs/examples/plugins/README.md), and the
[Korean manual page](../../docs/user_manual/03-core/11-plugins.md) walks through them.

```python
# host_tools/glossary.py
from app.agents.host_tools import HostToolProvider


class GlossaryTools(HostToolProvider):
    prefix = "glossary"                     # tool prefix, reserved as an MCP server name
    tails = ("lookup",)                     # glossary__lookup
    label = "사내 용어집 도구"                 # shown when the name is refused for an MCP server
    summary = "사내 용어집에서 뜻을 찾습니다"     # shown in the tool assignment dialog
    risk = "read"                           # read, state, write, outside, delete, exec, net, unknown (default)

    def tools_for(self, agent):
        return [self.definition("lookup", "사내 용어집에서 용어의 뜻을 찾습니다.", {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        })]

    def guidance(self, tools):              # optional; return None unless `tools` has your tool
        names = {(t.get("function") or {}).get("name") for t in tools or ()}
        return "[용어집] 사내 용어는 먼저 찾으세요." if "glossary__lookup" in names else None

    async def run(self, call):              # call.agent, .name, .arguments, .session_id, .workspace, .tools, .llm_caller
        return f"{call.arguments.get('term')}: ...", "success"
```

Then tick `glossary` in an agent's tool dialog, or add it to `allowed_mcp_servers`. Not in `mcp_servers`: there is no
process to start.

Rules:

- `tools_for` runs on every speech, so keep it light. Tool names outside `prefix` and `tails` are dropped, because
  that call would reach an MCP server or another provider.
- One object serves every conversation. Keep per-conversation state out of its attributes.
- `run` returns `(text, status)` with `success` or `error`. An exception becomes an `error` result the model reads
  ([ADR-014](../../lectures/05-adr/ADR-014-tool-failure-is-observation.md)). Output is clipped like any tool's.
- `run` runs on the event loop. Use `await asyncio.to_thread(...)` for slow work.

## 3. Tool Security

A plugin is judged like an MCP tool from an untrusted server: at tool level, by the risk it declares. Rules use
`mcp(glossary/lookup)` or `mcp(glossary/*)`. A `deny` rule also takes it off the list before the request.

| Declared `risk` | `read_only` | `default` | `review` | No person to ask |
| :--- | :--- | :--- | :--- | :--- |
| `read`, `state` | Runs | Runs | Runs | Runs |
| `write` | Denied | Runs | Asks | Runs in `default`, denied in `review` |
| `unknown` (nothing declared) | Denied | Asks | Asks | Denied |

**What this does not cover.** The gate decides whether a call may run. It cannot see inside the plugin, so argument-level
rules (`read(**/.env)`) and hard protections do not apply. A plugin that takes a path must enforce its own limits.

`gated = False` skips the gate. Only use it for tools that are safe without a verdict, and the tool dialog says so.

## 4. Loading Plugins

Both kinds load the same way, once, at startup (`load_plugins` in the app's `lifespan` and in `app.bench`).

| Kind | Folder (`plugins.*`) | Entry point group | Base class |
| :--- | :--- | :--- | :--- |
| Debate strategy | `strategies_dir`, default `strategies/` | `mado.strategies` | `BaseDebateStrategy` |
| Host tool | `host_tools_dir`, default `host_tools/` | `mado.host_tools` | `HostToolProvider` |

- **Folders**: one `.py` file or one package folder is one plugin. Names starting with `_` or `.` are skipped, which is
  where shared helpers go. Every concrete subclass defined there is registered.
- **Entry points**: may name a class, an instance or a module.
  ```toml
  [project.entry-points."mado.host_tools"]
  glossary = "my_package.glossary:GlossaryTools"
  ```
- `plugins.disabled` lists file, folder or entry point names that are not even imported.
- Changes need a restart. Skills are text and apply live; plugins are code.
- The source update package does not touch these folders, so plugins survive updates. They are not copied into a new
  offline package.

### 4.1. When a plugin fails

The app still starts. The plugin is skipped and the reason is kept:

- the log, as a warning;
- `GET /api/plugins`, with `kind`, `source`, `name` and `problem`;
- an amber icon next to the roster's strategy selector, with the reasons in its tooltip.

Refused on purpose: another plugin's name, a built-in strategy or prefix, a legacy strategy name, and a prefix that is also
in `mcp_servers`. A stored conversation holds its strategy name, so taking one over would change what those conversations do.

### 4.2. Security

**Plugins run with the app's privileges.** Dropping a file in the folder carries the same weight as installing a package.

- The folders are hard-protected (`manager.protected_files`): file tools cannot read or write them, even when one points
  inside a workspace.
- A folder inside the default workspace is refused. Code execution tools can write there through paths the gate cannot
  see, and that file would run with the app's privileges at the next start.
- A workspace picked per conversation cannot be checked at startup, so a folder outside the install folder should not be
  one you also use as a workspace.

## 5. Related

- [Debate Strategies §3](../orchestration/debate-strategies.md#3-adding-a-custom-debate-strategy) — strategy plugins
- [conf.json Reference §2.10](../configuration/conf-json-reference.md#210-plugins-object)
- [Tool Security](../mcp/tool-security.md)
- [Skills](skills.md), [Ask the User](ask-user.md) — built-in host tools
