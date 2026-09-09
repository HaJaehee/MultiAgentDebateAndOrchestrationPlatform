# Release Notes

Change history for the MADO: Multi-Agent Debate & Orchestration Platform, newest first. Each
entry links to the topic page carrying the full explanation — this file is an index of *what
changed*, not a second copy of the documentation.

---

## v0.5.3

v0.5.2 fixed two request-shaping bugs by reading the code. This release is about the case where
reading the code is not enough — when the endpoint refuses and *will not say why*.

**A failed LLM call now records what we sent.** An endpoint does not always
say why it refused — a gateway in front of vLLM was seen turning an upstream 400 into its own 500
and discarding the reason, which also made LiteLLM retry a deterministic error twice. When the
endpoint will not explain, the remaining evidence is our own request: message count, run-length
encoded roles, estimated tokens against the budget, tools and `tool_choice`, and the largest
messages by name and size. Sizes only — never content.

That fingerprint immediately paid for itself. It showed a request nowhere near its context budget
whose largest message was an `assistant(tool_calls*1)` of 14,546 characters — a
`filesystem__write_file` whose arguments JSON had been **cut off by `max_tokens` mid-string**. We
parsed what we could, the MCP server refused the fragment, and then we appended the assistant turn
to the context *verbatim* — resending JSON we had ourselves failed to read, on every following
request in that turn. `finish_reason` was never inspected, so nothing noticed.

Tool calls are now re-serialised from the arguments actually executed (unreadable ones collapse to
a short marker), which also fixes a mismatched `tool_call_id` when the provider sends an empty one,
and the agent is told it was truncated and what to do instead — resolved from the tools it
actually holds, because "split the write across several calls" only works when something can
*append*. With an overwriting `write_file` alone, splitting resends everything written so far
on every call and hits the same limit again, so that case is told to write several files.

The same `finish_reason` closes the other half: a plain answer cut off at `max_tokens` used to end
mid-sentence with nothing to say so. It now carries a footer — addressed to the reader, not the
model, because by then the turn is over and there is nothing left to recover.

→ [LLM Integration §2.1, §2.2, §2.3](agents/llm-integration.md)

The order matters more than either change: the fingerprint was built first, the next failure
produced one, and the fingerprint named the culprit in a single line. Diagnosis before repair, and
the diagnosis was worth shipping on its own.

---

## v0.5.2

Two request-shaping bugs that made agents fail with a **400 Bad Request** for no visible reason.
Both live in the MCP tool loop, both were already solved *outside* it, and both were selective
enough to look like an endpoint outage: one hit Anthropic models only, the other hit
OpenAI-compatible shims only, and neither could occur until a turn had run long enough.

**The wrap-up call dropped `tools` while the conversation still carried tool blocks.** When an
agent exhausts its tool budget, `_wrap_up_without_tools()` asks for a closing answer with no
further tool use — previously by omitting `tools` from the request. By that point the message
list holds the `tool_calls` assistant messages and `tool` results of every call already made, and
Anthropic refuses a conversation containing `tool_use` / `tool_result` blocks when the request
defines no tools. OpenAI accepts it. So a gpt-4o orchestrator was fine while a Claude specialist
died — but only on the tool-heavy turns that reach the budget. The tools are now sent with
`tool_choice: "none"`: defined, but uncallable.

**The in-loop context trim re-created consecutive `user` turns.** `merge_consecutive_roles()`
exists because Anthropic, Gemini and several OpenAI-compatible shims reject two `user` messages
in a row. The pre-turn path is careful about it — trim first, merge second. The loop's own trim,
`fit_tool_loop_context()`, inserts the same kind of elision notice as a `user` message directly
after the goal (also `user`) and went out unmerged. It now merges before returning.

Neither fix changes what an agent is allowed to do; they change what leaves the process.

Also in this release: **the session sidebar now checks that its page is still alive.** It was the
one component without an `alive` property, and nearly everything it draws happens after an `await`
— so a refresh mid-debate could leave it building session cards on a client NiceGUI had already
deleted (`Client has been deleted but is still being used`). Nothing broke, but that warning fires
once per process, so a benign one hides the next real bug. `refresh_list()` now reads first and
draws second, re-checking in between.

→ [LLM Integration §5.3](agents/llm-integration.md) · [UI Components §1](ui/components.md)

---

## v0.5.1

One change: **an agent's card colour and icon are now chosen, not derived.**

**Before.** Appearance came from the agent key alone — a fixed table for the five built-in keys, and
`crc32(key)` into a six-colour palette for everything else. There was no way to change it, and the
*border* was not per-agent at all: the roster border reported enabled/disabled, and the feed border
reported the message kind (user / orchestrator / error / other). So a user who added three agents got
three colours the system picked, on cards whose outline said nothing about who was speaking.

**Now.** Two optional keys per agent, `card_color` and `icon`, chosen from the UI and written back to
`conf.json`. One editor serves both entry points — the **에이전트 추가** dialog and the persona
editor — with a live preview, twelve swatches plus a colour picker, a Material-icon box, and
**image upload**.

### Where the image goes

An uploaded icon is copied into `data/agent_icons/` under the project root (created on demand) and
`conf.json` stores only the relative path, so the folder survives being moved or carried onto an
air-gapped machine. The filename is `<key>-<content-sha1[:10]>.<ext>` — naming the file ourselves
removes path traversal and name collisions in one move, and re-uploading the same image is a no-op.

### The border rule

Only agents whose colour was chosen explicitly get a coloured border. Without that condition every
existing installation would repaint on upgrade, and the roster border would stop answering "is this
agent participating?" first. A failed turn's red border is never overwritten — there the colour *is*
the message.

### Frozen with the conversation

`session_agents` gains `card_color` and `icon_path`, and both values also enter `config_snapshot`.
Recolouring an agent tomorrow leaves today's transcript exactly as it was recorded — the same reason
personas freeze. The feed no longer resolves colours from `conf.json` at all; the roster hands it the
agents it is drawing, which for a locked conversation are the frozen ones. Existing databases pick up
the two columns through the startup migration.

### Three layers of fallback

A missing icon must never leave a broken avatar, so:

1. At render time, `avatar` becomes `img:` **only** if the path resolves to a real image inside the
   project folder. Anything else falls back to the key-derived icon, with a warning in the log.
2. If the file disappears after the page was drawn, `/agent-icon` answers with a default robot SVG at
   **200, not 404**.
3. That route repeats the containment and file-type checks, so an icon path cannot become a way to
   read arbitrary files.

→ [Agent Pool §1](agents/agent-pool-and-roles.md) ·
[Session Personas §3, §6](agents/session-personas.md) ·
[conf.json Reference §2.4](configuration/conf-json-reference.md) ·
[Database Schema §2.5](architecture/database-schema.md) ·
[UI Components §1.2.1](ui/components.md) ·
[NiceGUI + FastAPI §2](ui/nicegui-fastapi.md)

### Operational impact

| | |
| :--- | :--- |
| New Python dependencies | **None** — `requirements.txt` unchanged |
| Runtime / wheels / Node re-packaging | **Not required** |
| Schema change | `session_agents.card_color` · `icon_path`, added automatically at startup |
| New folder | `data/agent_icons/`, created on first upload and gitignored |
| Update path | `python package_source.py` |
| Test suite | 500 → **528** tests |

---

## v0.5.0

Seven changes, in the order they were made. Four are fixes to failures users hit in practice;
three are new capabilities that grew out of those failures.

### 1. A tool error no longer kills the agent — or the backend

**Symptom.** An MCP tool error took the speaking agent down with it, and sometimes the whole
server.

**Cause.** One layer of defence with two holes. `except Exception` does not catch
`BaseExceptionGroup`, which is exactly what the `anyio` stdio paths raise — it passed through
`MCPManager`, `_speak()` and `DebateRunner` alike, and the debate task died leaving the screen
stuck on "토론 중...". Separately, `except (Exception, BaseException)` swallowed
`CancelledError`, so shutdown waited forever on a task that would never stop and Uvicorn was
force-killed.

**Now.** One rule — *a tool failure is an observation, never a reason to end a turn, a debate
or the process; cancellation is the sole exception* — enforced at six layers. Plus
`MCP_TOOL_TIMEOUT` (180 s) so an unresponsive server cannot hold the turn open forever,
`MCP_TOOL_MAX_CHARS` (200 k) so a huge result cannot break token counting and the browser, and
process teardown by **handle** instead of pid (a recycled pid could otherwise hit a Uvicorn
worker).

→ [MCP Resilience §4](mcp/error-handling-resilience.md) ·
[Environment Variables §3.5](configuration/environment-variables.md)

### 2. Exported diagram HTML opened as a blank white page

**Cause.** The "standalone" export fetched the Mermaid renderer from a CDN on every open. On an
air-gapped network the script never loaded, `mermaid.initialize(...)` threw on the first line
and **took the whole inline script with it** — toolbar and zoom included. What remained was the
raw source in the dark theme's white text on the always-light diagram panel: white on white.

**Now.** The already-rendered SVG is embedded and the CDN tag is not emitted at all — a
downloaded file contains zero external URLs. When the SVG cannot be captured, the fallback
checks `typeof mermaid` first and the panel pins its text colour, so the failure is a readable
warning instead of a blank page.

→ [Artifact Synthesis §4](orchestration/artifact-generation.md)

### 3. Reasoning traces stay out of other agents' prompts

`show_steps` was doing two jobs: what people see, and what models read. With it on, the whole
`Thought 1..N` body was copied into every later speaker's context — so within a few rounds
`fit_context_window()` was discarding the goal to make room for other agents' reasoning, and
the prompts that quote turns at 250–300 characters got nothing but `Thought 1: ...` preamble.

`show_steps` now decides only what the timeline and database keep. Prompts get conclusions.
Measured: **5,556 → 1,848 tokens** on a 3-specialist × 3-round transcript.

→ [LLM Integration §3](agents/llm-integration.md)

### 4. Launchers are tracked in the repository

`run_mado.bat` / `.ps1` were generated into the bundle only, so a source checkout had nothing
to run. A byte-identical snapshot is now committed — with `package_offline.py` still the source
of truth.

→ [Air-gap Packaging §3](operations/airgap-packaging.md)

### 5. The roster shows the real speaking order

Only Sequential Debate speaks in card order. Adversarial interleaves the camps, neutrals always
go last, and the orchestrator-led/parallel strategies decide per round — so dragging a card
could look like it did nothing. The roster now renders the computed order plus a note saying
how much to trust it, refreshed on every input that can change it. It calls the engine's own
`get_speakers_for_round()` rather than reimplementing the ordering.

→ [Debate Strategies §1-b](orchestration/debate-strategies.md) ·
[UI Components §1.2](ui/components.md)

### 6. Session handoff (⑂)

When the context fills up the only remedy is a new conversation, which used to mean losing the
workspace binding and the knowledge graph. The graph could not simply be "referenced": the
memory server resolves the graph id from host metadata *over* model arguments, deliberately, so
no prompt can reach another conversation's graph.

Since the host owns the boundary, the host moves it. **⑂** creates a session that inherits the
workspace, the graph file, the roster with its `config_snapshot`, the strategy settings and the
previous conclusion — clearing only the transcript.

→ [Session Handoff](orchestration/session-handoff.md) · [UI Components §1.1](ui/components.md)

### 7. The orchestrator debugs its own diagrams

A diagram that failed to parse became an artifact as-is; the user found out on opening the tab,
after the debate had ended. The synthesis is now linted before it is recorded, and broken blocks
go back to their author with the renderer's complaint attached — up to two attempts, keeping the
original if it never converges.

The linter is deliberately incomplete. A missed error costs what it always cost; a false
positive spends an LLM call and lets a model with nothing to fix ruin a working diagram. Every
rule was calibrated against the real `mermaid.parse()`: **30 valid diagrams flagged zero times,
13 rejected diagrams all caught.**

→ [Artifact Synthesis §3](orchestration/artifact-generation.md)

### Operational impact

| | |
| :--- | :--- |
| New Python dependencies | **None** — `requirements.txt` unchanged; new code uses only the standard library |
| Runtime / wheels / Node re-packaging | **Not required** |
| Update path | `python package_source.py` — 391 KB source-only package |
| Test suite | 379 → **500** tests |

---

## Earlier versions

| Version | Highlights |
| :--- | :--- |
| v0.4.1 | Context-window saturation guard |
| v0.4.0 | Tool-call budget guard |
| v0.3.0 | Multi-format Mermaid export (PNG / SVG / HTML / StarUML `.mdj`) and the interactive viewer |
