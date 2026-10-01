# Tool Security — Allow, Ask, Deny

Implementation:
[app/mcp/policy.py](file:///d:/MultiAgentDebateOrchestration/app/mcp/policy.py) (pure verdicts) ·
[app/mcp/exec_scan.py](file:///d:/MultiAgentDebateOrchestration/app/mcp/exec_scan.py) (sandbox code scan) ·
[app/orchestration/tool_gate.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/tool_gate.py) (asking, remembering) ·
[app/orchestration/control.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/control.py) (`ToolApprovalRequest`) ·
[app/mcp/manager.py](file:///d:/MultiAgentDebateOrchestration/app/mcp/manager.py) (last-line hard protection)

Until v0.10.0 every MCP tool an agent was allowed to see ran as soon as the model called it. The only
guards were the servers' own (the filesystem server refuses paths outside its folder) and the binary
document refusal. This page describes the layer that now sits between the model and the tools.

---

## 1. Why a Multi-Agent Debate Needs Its Own Design

The designs this borrows from — Claude Code, Zed and Google Antigravity — all assume one agent and a
person watching. MADO differs in four ways that shaped every decision:

| MADO trait | Consequence |
| :--- | :--- |
| Every tool is an MCP tool | All three products judge MCP tools **per tool only**. MADO knows the argument shape of its bundled servers, so it judges them **per path, host and code**. |
| Debates run unattended, several speakers in parallel | Prompts must be rare, several cards can be open at once, and **no answer means deny**. The existing `TurnControl` mailbox already carried this for tool budgets and context windows. |
| `sandbox` runs arbitrary Python | A rule on filesystem tools alone is bypassed by `open('.env')` — the same bypass Antigravity suffered when a blocked `.env` was read with `cat`. |
| Remote viewers log in with a token | Approvals record who answered, and only the server PC can write rules into `conf.json`. |

Three holes were found while designing this and are closed by the hard protections (§6):

1. `workspace/.memory-graphs/<other conversation>.jsonl` was readable with `filesystem__read_file`,
   sidestepping the per-conversation isolation the memory server enforces through `_meta`.
2. A session workspace set to the install folder (or above it) put `conf.json`, `.env` and
   `multiagent.db` in tool reach — an agent could rewrite its own permission rules.
3. MCP servers inherited the whole process environment, including `MADO_ACCESS_TOKEN` and the LLM API
   keys that `.env` loads; the sandbox kernel inherits the server's environment, so one line of agent
   code (`os.environ`) read them.

---

## 2. What Was Taken From Each Product

| Topic | Claude Code | Zed | Antigravity | MADO |
| :--- | :--- | :--- | :--- | :--- |
| Order | deny → ask → allow, first match wins | built-in rules → deny → confirm → allow → tool default → global default | Deny > Ask > Allow | **hard → deny → ask → allow → mode default** |
| Presets | default, acceptEdits, plan, auto, dontAsk, bypass | `default` confirm/allow/deny | Default (sandboxed), Request Review, Turbo | `read_only`, `default`, `review`, `auto` |
| Rule target | `Tool(specifier)` | regex per tool | `action(target)` | `action(target)` |
| Prompt answers | once / don't ask again (scope varies by kind) / deny with comment | allow once / always for tool / always for pattern | widen the target, then approve for the turn | allow or deny × once / this conversation / always (`conf.json`), with a reason on deny |
| Unremovable | protected paths (`.git`, `.claude`) | `rm -rf` of root/home, settings dirs | strict mode | install folder, `.memory-graphs`, `.git` writes, secret env |

Two incidents shaped the defaults: Antigravity's `.env` read through `cat` (rules must target
*actions*, not tool names) and its browser allow-list shipping with `webhook.site` (network defaults to
*ask*, and well-known capture services are denied outright).

---

## 3. The Pipeline

```text
model calls a tool
  └─ ToolGate.check ───────────────────────────── app/orchestration/tool_gate.py
       1. hard protection  (MCPManager.hard_refusal)        → deny, no card
       2. profile_call     tool + arguments → actions
       3. evaluate         deny → session denials → ask → allow/grants → mode default
       4. ask?             TurnControl.ask_tool_approval → card → answer / timeout
  └─ MCPManager.execute_tool
       hard protection again (any caller, gate or not)  → deny
       server call
```

The gate is created per turn (`OrchestratorEngine._tool_gates`) and passed to **every** `call_agent` of
the turn — speeches and the auxiliary calls (speaker selection, dispatch, graph gates, summaries, ledger,
Mermaid repair), because those also carry the agent's tools. A denied call is not executed; the refusal
text becomes the tool result, so the model reads it and changes course. Tools that can never run under
the current policy (a write tool in `read_only`, a tool matched by a bare `mcp(...)` deny) are removed from
the tool list before the request, as Claude Code does with bare-name denies.

If the gate itself raises, the call is **denied** (`gate-error`). If there is nobody to ask (no
`TurnControl` — batch runs and tests), an *ask* becomes a deny (`unattended`), like Claude Code's
`dontAsk`.

---

## 4. From Calls to Actions

Skill tools (`skills__load_skill`, `skills__read_skill_file`) never reach this pipeline: they are host tools
that read the operator's skill files inside the install folder, and who may use them is decided by
`allowed_skills` and the skill's on/off switch. Their records carry no verdict. See
[Skills §2](../agents/skills.md#2-host-tools-not-an-mcp-server). **Running** a skill's script is different:
the host copies it into the workspace (`.mado/skills/<name>/`) and the agent runs it with
`run_python_file`, which is judged like any other code ([Skills §5](../agents/skills.md#5-scripts)).

`profile_call` turns a call into actions: `read(path)`, `write(path)`, `delete(path)`, `exec(code)`,
`net(host)`, plus the tool itself (`mcp(server/tool)`).

| Tool (by name tail) | Actions |
| :--- | :--- |
| filesystem `read_*`, `list_*`, `search_files`, `get_file_info`, `directory_tree` | `read(path)` |
| filesystem `write_file`, `edit_file`, `create_directory` | `write(path)` |
| filesystem `move_file` | `write(source)` + `write(destination)` (the server refuses to overwrite) |
| git status/diff/log/show/branch | `read(repo_path)` |
| git add/commit/reset/create_branch/checkout/init | `write(repo_path)` (+ `write(files)` for `git_add`) |
| memory, `sequentialthinking`, `reset_kernel_state` | conversation state — allowed in every mode |
| sandbox `write_workspace_file` / `append_workspace_file` | `write(filename)` |
| sandbox `execute_python_code` / `run_python_file` | `exec(code)` + whatever the code scan finds |
| sandbox `install_python_packages` | flagged execution (it brings in outside code) |
| fetch `fetch` | `net(host)` |
| PairSlide `slide_*` / `sheet_write_table` | read or write (paths are PairSlide-relative, not resolved) |
| any tool on a remote server not on this PC | + `net(host of the server)` |

Relative paths resolve against the session workspace; a leading `./workspace/` is stripped, the way the
sandbox accepts it. Resolution is textual (`normpath`), because the target may not exist yet.

**Trust.** The name table and MCP `annotations` are only believed for trusted servers:
`tool_security.trusted_servers`, or — when unset — local (stdio) servers and remote servers on this PC.
A remote server elsewhere could name a tool `read_file` to pass as a read; its tools are judged per tool
(`mcp(server/tool)`) and as `net(host)`. Annotations follow the MCP defaults: an omitted
`destructiveHint` or `openWorldHint` counts as `true`, so vaguely annotated tools ask.

**Code scan** (`exec_scan.scan_python`). The AST is read, never run. Literal paths passed to `open`,
`Path(...).read_text/write_text/unlink`, `shutil.*`, `os.remove`, pandas/matplotlib savers and readers
become path actions; any other path-looking string literal becomes a `read` so rules and hard protection
see it (a path assigned to a variable and opened later is caught). URL literals and network modules
become `net`. **Uncertainty is flagged, not guessed:** a parse failure (shell escapes, IPython magics),
`eval`/`exec`/`__import__`, `subprocess`/`os.system`, `ctypes` and friends make the call *flagged
execution*, which asks in `default`. `run_python_file` reads the file from the workspace and scans it;
if it cannot, the call is flagged. **This is a net for mistakes and ordinary injected code, not a
security boundary** — Python can hide anything. The boundary is OS isolation (§10).

---

## 5. Rules and Modes

```json
"tool_security": {
  "mode": "default",
  "approval_timeout": 180,
  "deny":  ["read(**/.env)", "read(**/.ssh/**)", "net(webhook.site)"],
  "ask":   ["mcp(git/git_checkout)"],
  "allow": ["net(docs.python.org)"],
  "trusted_servers": null,
  "agents": { "critic": { "mode": "read_only" } }
}
```

| Rule | Matches |
| :--- | :--- |
| `read(glob)` · `write(glob)` · `delete(glob)` | Workspace-relative glob; `**` crosses folders, `*`/`?` stay within one. Absolute globs match absolute paths. A glob starting with `**` (and `regex:`) also sees paths outside the workspace (`~/.env`). Case-insensitive. Folders are written `src/**`. |
| `exec` · `exec(regex:…)` | Sandbox code execution, optionally by code content |
| `net(host)` | The host and its subdomains (`net(python.org)` covers `docs.python.org`); `*` globs allowed |
| `mcp(server/tool)` · `mcp(server)` | The tool itself — every call of it |

**Restrictions spread up, allowances spread down** (Antigravity's implicit rules): denying
`read(.env)` also denies writing and deleting it; allowing `write(src/**)` also allows reading it.

Order: deny beats ask beats allow. An allow never carves an exception out of a deny, and an ask rule
still asks even when an allow rule matches — which is why a card raised by an ask rule offers no
"remember" buttons (they would have no effect).

`deny` in `conf.json` **replaces** the built-in list (`DEFAULT_TOOL_DENY_RULES`: secret files and
capture services) so entries can be removed; keep the defaults when adding.

| Risk | `read_only` | `default` | `review` | `auto` |
| :--- | :---: | :---: | :---: | :---: |
| read, conversation state | allow | allow | allow | allow |
| write inside the workspace | deny | allow | ask | allow |
| outside the workspace | deny | ask | ask | allow |
| delete | deny | ask | ask | allow |
| code execution (scan clean) | deny | allow | ask | allow |
| flagged execution, package install | deny | ask | ask | allow |
| network, remote servers | deny | ask | ask | allow |
| unknown tools | deny | ask | ask | allow |

`default` does not ask for workspace writes: stopping the debate at every write makes collaboration
impossible, and the workspace is a git repository the app creates, so changes can be inspected and
reverted. Antigravity's Default preset makes the same call.

The session mode is chosen in the roster panel (`도구 보안`, stored in `sessions.tool_mode`; empty means
the `conf.json` default). Changing it during a debate applies from the next tool call
(`OrchestratorEngine.set_tool_mode`). Per-agent overrides live in `tool_security.agents.<key>` — **not**
in `agents.<key>` — because agent configs freeze into the conversation snapshot, and security must stay
live: a rule tightened later applies to old conversations immediately. Overrides only tighten: the
stricter of session and agent mode wins, rules are added, and `allow` is refused by validation.

---

## 6. Hard Protections

Checked by `policy.hard_block` in the gate (so no card is raised) and again in
`MCPManager.execute_tool` (so no caller bypasses them). No mode, rule or approval lifts them.

1. **The install folder** except the session workspace carved out of it: `conf.json`, `.env`, the DB,
   app code, bundled servers. If the workspace *is* the install folder or a parent of it, nothing is
   carved out. The active config file and the DB file (plus `-wal`/`-shm`/`-journal`) are also
   protected wherever they live (`manager.protected_files`).
2. **`.memory-graphs`** anywhere — only the memory server touches it.
3. **Writes inside `.git`** — `hooks/` runs code on the next git command. Git tools act on the
   repository path, so they are unaffected.
4. **Secret environment variables** are not passed to MCP server processes
   (`policy.server_environment`: names containing `TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `_KEY`,
   `AUTH`…). A server that needs a secret declares it in its own `env` block in `conf.json`; declared
   values pass through, so who receives what is visible in the config.

The binary-document refusal (`guards.binary_write_refusal`) runs at the same place with status `error`
(a format problem the model fixes with another tool); security refusals use status `denied`.

---

## 7. The Approval Card

`TurnControl.ask_tool_approval` hangs a `ToolApprovalRequest` in the same mailbox as the budget and
context requests; the runner keeps every open card in `TurnRun.approvals` and the snapshot
(`tool_approvals`), so a refreshed page or a remote viewer sees the same cards. The chat feed stacks them
above the timeline in a scrolling column.

A card shows the agent, the tool, the risk badge, the reasons (which rule, or which actions the mode asks
about, plus code-scan findings), the code or arguments, an editable **scope** (one rule per line) and a
deny reason. The scope is shared by all "remember" buttons.

| Button | Effect |
| :--- | :--- |
| 이번만 허용 | Runs this call |
| 이 대화에서 허용 | Adds the scope to `sessions.tool_grants`; later calls it covers run without asking |
| 항상 허용 | Adds the scope to `tool_security.allow` in `conf.json` — **server PC only** |
| 거부 | Refuses this call. The reason, if given, goes to the model verbatim |
| 이 대화에서 거부 | Adds the scope to `sessions.tool_denials`; later calls it covers are refused without asking, in this turn and the next ones |
| 항상 거부 | Adds the scope to `tool_security.deny` in `conf.json` — **server PC only** |

A remote "항상" answer is narrowed to the conversation. When `conf.json` has no `deny` key yet, the first
"항상 거부" copies the built-in deny list in before appending — writing the key replaces the defaults,
so appending alone would silently drop the secret-file and capture-service rules.

The scope is validated before the answer is taken (Antigravity's target validation): an allow scope must
cover the call, a deny scope must block it — otherwise the call just approved would ask again, or the
call just denied would run next time. The scope is pre-filled with the narrowest rules covering what
made the call ask (`Verdict.suggestions`); when the card was raised by an **ask rule**, allowing cannot
be remembered (ask beats allow), so the allow buttons are hidden and the scope is pre-filled for denying
(`policy.narrow_rules`: the call's argument and server actions, or the tool itself). A tool-wide scope is
labelled as such.

No answer within `approval_timeout` seconds denies. Stopping the debate denies every open card. The same
speaker repeating a rejected call in the same turn is denied without a new card (`repeat`).

A call refused by a session denial tells the model that *the user* refused this scope for the
conversation and not to reach the same result through another tool — a configured deny reads as policy,
which models tend to route around.

### 7.1 The conversation's rules

The roster panel's `대화 규칙 N` button (next to `도구 보안`) opens the conversation's grants and denials,
each with a delete button. Deleting takes effect from the next tool call. While a debate runs, the change
goes through the running gate (`OrchestratorEngine.set_tool_rules` → `ToolGate.replace_rules`), which is
then the only writer of the two columns — otherwise a rule a card just added could be overwritten by the
page's older list. With no debate running, the page writes the session row itself. `conf.json`'s
`allow`/`deny` are not listed there; they are edited in the file. Grants and denials do not follow a
session handoff; the mode does.

---

## 8. Audit

`tool_calls` gained four columns (`decision`, `risk`, `rule`, `approver`); see
[Database Schema](../architecture/database-schema.md). `decision` is one of `allow`, `approved`,
`deny`, `hard`, `rejected`, `timeout`. `rule` records where the verdict came from:

| `rule` | Meaning |
| :--- | :--- |
| `mode:<mode>` | the mode default |
| `session:<rules>` | the conversation's grants/denials — registered on this card, or matched later |
| `always:<rules>` | registered into `conf.json` on this card |
| `once` · `repeat` · `unattended` · `gate-error` | markers |
| anything else | a `conf.json` rule, or the kind of hard protection |

**What a person sees** answers two questions, each in one place, in one language
(`policy.tool_outcome` / `policy.describe_verdict`, shared by the chat feed and the session export):

- *Did the tool run, and how did it end?* — one badge on the accordion header: `성공` (ran, succeeded),
  `실패` (ran, the tool reported an error), `차단` (did not run: any security refusal and the binary
  document refusal). Blocked calls use a shield icon, the rest a wrench.
- *Who decided, on what grounds?* — one `판정` line: `자동 허용`, `유저 승인`, `규칙 차단`, `모드 차단`,
  `고정 보호`, `유저 거부`, `응답 없음` (or `판정 오류`), followed by the ground and where the person
  answered — e.g. `유저 거부 · 이 대화 규칙으로 등록 write(design.md) · 서버 PC`, and on later calls
  `유저 거부 · 이 대화 규칙 write(design.md)`. Calls made without a gate show no 판정 line.

The export writes `⛔ <code>tool</code> — 차단` and `**판정**: …` in the same words.

---

## 9. Configuration Reference

See [conf.json Reference](../configuration/conf-json-reference.md#tool_security). Rule strings are
validated when the config loads; a bad line fails the load with every problem listed.

---

## 10. Not in This Release

- Editing `conf.json`'s rules and a security log view in the UI (the conversation's rules can be
  viewed and deleted — §7.1).
- Marking fetched content as untrusted in the observation fed to the model.
- Snapshots before overwriting (Claude Code's checkpoints).
- OS-level isolation of the sandbox kernel (Windows Job Object / AppContainer, bubblewrap on Linux) —
  the real boundary for code execution.
- Taint tracking: once a speech has read outside content, its network calls always ask.
- Pinning MCP tool definitions to detect a server changing its tools.
- An LLM judge mode: models vary in closed networks, the judge itself is injectable, and it adds
  latency to every call.
