# Release Notes

Change history for the MADO: Multi-Agent Debate & Orchestration Platform, newest first. Each
entry links to the topic page carrying the full explanation — this file is an index of *what
changed*, not a second copy of the documentation.

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
