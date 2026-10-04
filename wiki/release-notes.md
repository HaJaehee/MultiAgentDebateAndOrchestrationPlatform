# Release Notes

Change history for the MADO: Multi-Agent Debate & Orchestration Platform, newest first. Each
entry links to the topic page carrying the full explanation — this file is an index of *what
changed*, not a second copy of the documentation.

---

## Unreleased

**Plan approval.** After the orchestrator's plan the engine opens an approval card and no specialist speaks
until a human answers. The card lists one task per specialist (taken from the plan by one extra tool-less
call), with an optional completion criterion and, where the choice is a matter of preference, alternatives.
The human edits tasks and criteria in place and approves in one click; writing a comment swaps the approve
button for a revise button, and the orchestrator rewrites the plan (the rewritten plan replaces the old one
in every later prompt). Rejecting runs the existing abort. With no answer within `plan_approval.timeout`
(default 10 minutes) the turn is parked with nothing executed and "이어서 진행" reopens the same card.
Approved tasks are pinned with the plan, appended to each specialist's turn prompt and carried by routing
calls; the synthesis prompt lists each task with what the records show (speeches counted, response failures)
and the report gains a per-task completion check. `plan_approval.enabled = false` restores the old flow, and
a run with no human channel is not gated. This is the planning harness's plan → approval → execute → check
loop moved into the engine, where the model cannot skip it.
→ [Plan Approval](orchestration/plan-approval.md) · ADR-028

## v1.1.2

**Long files are appended, not edited into place.** The bundled sandbox is now AirgappedPySandbox v0.8.0,
which adds `append_workspace_file`: it adds a chunk verbatim to the end of a workspace file and answers with
the file's last 8 lines. The standing file-writing rule now names it first — write the first part with the
sandbox's `write_workspace_file`, append the rest — instead of the filesystem server's `edit_file`, which is
find-and-replace and put "appended" chunks at the first matching line or at the top of the file (long TSX
files broke this way). Agents with the filesystem server only still get `edit_file`. The tool gate judges
the new tool as a write, and appending text to `.pptx`/`.xlsx`/… is refused like any text write.
→ [LLM Integration §2.2, §2.5](agents/llm-integration.md) · [Bundled Servers](mcp/bundled-servers.md)

**Diagnostics for "Connection lost".** When the server's event loop is held for more than a second, a
watchdog thread writes the stack of what held it to `data/diagnostics/stalls.log` (and notes when the loop
was idle but not given a turn). The main page reports long main-thread tasks, socket disconnects with their
reason, reconnections and failed reconnects over HTTP to `client.log` — HTTP so the report gets out while
the socket is down. `/api/health` gains an `event_loop` summary. Nothing is written while healthy;
`MADO_DIAGNOSTICS=0` turns the watchdog off.
→ [Diagnostics](operations/diagnostics.md)

**Math in speeches and conclusions.** LaTeX written by the models — `$O(n \times m)$`, `$A \leftarrow B$`,
`$$\frac{1}{n}\sum t_i$$`, `\(…\)`, `\[…\]` — is drawn as MathML (via the pure-Python `latex2mathml`, a new
requirement) in speech cards, the final conclusion, the decision ledger and the trial result. Formulas are cut
out before Markdown so `\(` and `x_i` survive; prices (`$5 에서 $10`) and code are left alone; bare `\rightarrow`,
`\times` become `→`, `×`. Without the package, formulas fall back to Unicode text. Copy and export keep the LaTeX.
→ [UI Components §1.3.7](ui/components.md)

---

## v1.1.1

**Designate a skill from the input bar.** `@specialist @skill` makes that specialist use that skill in this
turn: the `@` list now offers the usable skills, a skill pairs with the specialist mentioned just before it on
the same line, and each of that specialist's speeches starts with the skill already loaded by the host (a
normal `skills__load_skill` tool card). It works outside the specialist's `allowed_skills` for that turn only,
never for a skill that is switched off, and survives resuming an interrupted turn.
→ [Skills §9](agents/skills.md#9-designating-a-skill-from-the-input-bar) · [UI Components §1.3.4](ui/components.md) · ADR-027

---

## v1.1

**Skills: instructions an agent loads when the work calls for them.** A skill is a folder under `skills/`
with a `SKILL.md` (front matter `name`, `description`, then the instructions) and optional supporting files.
Agents see only each skill's one-line description, inside the new `skills__load_skill` tool; when a task
matches they load the body, and read supporting files with `skills__read_skill_file`. Which agent may use
which skill is `agents.<key>.allowed_skills` (card button **스킬 N**, frozen with the agent like tool
assignment). The skills themselves are **live**: edits, new folders and the on/off switch (`skills.disabled`,
roster panel **스킬** section) reach running conversations from their next speech. Skill tools are host
tools, not MCP, and skip the tool gate; `mcp_servers.skills` is reserved. Ships with a `mermaid-diagrams`
skill matching MADO's Mermaid checker. `GET /api/skills` lists them.
→ [Skills](agents/skills.md) · [conf.json Reference §2.7](configuration/conf-json-reference.md) · ADR-026

**Skills with scripts.** When an agent that holds `run_python_file` loads a skill carrying Python scripts, the
folder is copied into the conversation's workspace at `.mado/skills/<name>/` (changed files only) and the
loaded text names the paths to run. The run itself is an ordinary sandbox call, judged by the tool gate. Ships
with `csv-profile`, which summarises the workspace's CSV files column by column (UTF-8 and CP949).
→ [Skills §5](agents/skills.md#5-scripts)

**Larger small text in the roster.** Agent cards, the tool and skill assignment dialogs, and the MCP
server and skill chips set their small text one pixel larger. Badge sizes given as props (which never took
effect) are now classes. → [UI Components](ui/components.md)

**Fixed.** `run_mado.ps1` / `.bat` failed on a PC without the bundled `python_runtime` (`Start-Process : The
system cannot find the file specified`); they now fall back to `python` / `node` on `PATH`. In `run_mado.bat`
the title's `&` no longer runs as a command, and the address line reads `conf.json` again (it always showed
the fallback text). → [Air-gap Packaging §3](operations/airgap-packaging.md)

---

## v1.0

**Interrupted turns can be continued, finished or discarded.** Every turn is recorded (`turns`) in the same
commit as its request, and every message and tool call points to it with its role in the flow
(`turn_meta`). At start-up a turn still marked running is marked interrupted; a turn stopped by an engine
exception is marked failed. Opening such a conversation shows a bar with *이어서 진행* (continue from the
records), *지금까지로 결론* (synthesize what was said; never consensus) and *버리기* (discard). Rounds,
nominations, parallel assignments and graph steps are rebuilt from the records; the turn finishes with the
configuration it started with. The report names the pause inside the total elapsed time. Trial visitors get
continue and finish. → [Interrupted Turns](orchestration/turn-recovery.md) · ADR-024

**A cut speech continues after its last finished tool.** A speech that uses tools saves the tool loop's state
after it asks for tools and after each result (`speech_drafts`). On continue it resumes from those exact
messages instead of starting over; a call that was running at the cut is answered "result unknown".
→ [Interrupted Turns §5](orchestration/turn-recovery.md) · [LLM Integration §2.8](agents/llm-integration.md) · ADR-025

**Tool calls are recorded as they run**, not with the finished speech, so a cut speech no longer loses the
record of tools it ran. → [Database Schema §2.3](architecture/database-schema.md)

**Fixed.** A parallel round recorded a speech's cancellation as a failure and carried on; it now re-raises it
after the other speeches are recorded, like graph steps. The summary JSON's `consensus_reached` no longer
reports a stopped turn as consensus.

---

## v0.10.0

**Tool security: allow, ask, deny.** Every MCP tool call now passes a gate before it runs. Calls become
actions — `read(path)`, `write(path)`, `delete(path)`, `exec(code)`, `net(host)`, `mcp(server/tool)` —
and are judged in the order hard protection → deny → ask → allow → mode default, so one rule such as
`read(**/.env)` covers the filesystem tools and a sandbox `open('.env')` alike. Four modes per
conversation (roster panel `도구 보안`): `read_only`, `default` (workspace writes and clean code run
without asking; deletes, outside paths, network, flagged code and unknown tools ask), `review`, `auto`.
Sandbox code is scanned before it runs; subprocess, `eval`, shell escapes and unparseable code ask
instead of being guessed safe. "Ask" raises an approval card: allow or deny, each once, for this conversation, or always
(`conf.json`, server PC only), with a deny reason the model reads verbatim; no answer within
`approval_timeout` denies. Conversation grants and denials are stored on the session, carry into later
turns, and are listed and deletable from the roster's `대화 규칙` button. Tools that can never run under the policy leave the tool list.
Rules live in `tool_security` in `conf.json`; per-agent tightening in `tool_security.agents`.
Every call records its verdict (`tool_calls.decision/risk/rule/approver`), shown in the tool accordion
and the session export. → [Tool Security](mcp/tool-security.md)

**Hard protections close three holes.** Other conversations' knowledge graphs (`.memory-graphs`) are no
longer readable through file tools; the MADO install folder (config, `.env`, DB, app code) is out of tool
reach even when a session workspace points at it; writes inside `.git` are refused. MCP servers no longer
inherit MADO's secret environment variables (access token, LLM API keys) — a server that needs a secret
declares it in its `env` block. → [Tool Security §6](mcp/tool-security.md)

---

## v0.9.1

**Roster view options: summary and hide-inactive.** Two checkboxes next to the roster title. `요약 보기`
shrinks each card to its avatar and name (role and model move to the tooltip). `비활성 에이전트 숨기기`
hides the cards unchecked for this conversation and the `꺼둔 에이전트` row, leaving a `비활성 N개 숨김`
note. Both only change the view — nothing is written to `conf.json` — and cards can still be dragged
to reorder while either is on; hidden agents keep their place in the speaking order.
→ [UI Components §1.2](ui/components.md)

---

## v0.9.0.1

**The selected wire in the graph editor is visible.** Vue Flow's default theme painted the selected wire
dark gray (`#555`), which nearly vanished on the dark canvas and overrode the carry colours. It now
blinks yellow with a glow, and its label turns yellow too; with reduced motion enabled it stays yellow
without blinking. → [UI Components §1.6](ui/components.md)

---

## v0.9.0

**Graph debate — engine (a fifth strategy).** Agents can be wired into a graph: who feeds whom, parallel
branches, joins, and review loops closed by yes/no gates. Graphs live in `data/graphs/<id>.json`; a
session picks one and each turn freezes it. Nodes run in supersteps, see only what their incoming
wires carry (full text, digest or code references), and loops stop at gates, per-node visit caps or the
step cap. Invalid graphs — including a loop that bypasses every gate — are refused before the request
is recorded. The roster offers a graph picker, the validation summary, "그래프 편집", "새 그래프" and
"현재 카드 순서로 만들기"; while this strategy is selected the participation checkboxes follow the
graph and are locked. The card sort and the other four strategies are unchanged.
→ [Debate Strategies §2.5](orchestration/debate-strategies.md)

**Graph editor page.** `/graphs/<id>` edits a graph on a canvas like a blueprint: add start, agent,
merge, gate and end nodes from the palette, drag wires between ports (a gate has yes and no), and set
each node and wire in the inspector — agent, instruction, what it sees, join rule, visit cap, gate
question and default branch, and whether a wire carries the full text, the digest or code references.
Validation shows the same report the engine applies, with the worst-case number of calls per turn. The
edit state lives in the browser and the server reads it only on save, so dragging never floods the
websocket; leaving with unsaved changes asks first. Graph files now write only the fields that differ
from the defaults, so a hand-opened file shows what was actually set.
→ [UI Components §1.6](ui/components.md)

**Watching a graph run.** While a graph debate runs, the roster preview shows it live: the running node
pulses, finished nodes show how often they ran, gates show their verdict, wires that carried output are
drawn solid (the ones feeding a running node animate), and the rest fade. 크게 보기 opens the same view in
a wide dialog. Chat cards name their node (`구현 · 2회차`, `판정 · 아니오`), and so does the Markdown
export. A refreshed page or a reopened conversation draws the same picture: each node output now records
the port it left through (`messages.graph_port`), and the state is counted from the record.
→ [UI Components §1.2.2](ui/components.md) · [Debate Strategies §2.5](orchestration/debate-strategies.md)

**Graph editor library, bundled for air-gapped networks.** [Vue Flow](https://vueflow.dev) 1.48.2 and its
dependencies are shipped as one ES module in `app/ui/static/graph_editor/` (156 KB) that imports nothing but
`vue`, which NiceGUI already provides — the editor needs no CDN. A spike page confirmed nodes render,
dragging and wiring work, Korean text round-trips and every request stays on the local server. The
licence notices (MIT · ISC · BSD-3-Clause) and rebuild instructions travel with it, and
`package_source.py` now aborts if any of these required files is missing from the package.
→ [Air-Gapped Packaging](operations/airgap-packaging.md)

---

## v0.8.3

**Long conversations no longer forget what the user said.** Every speaker's context carried the whole
session verbatim, and past the window `fit_context_window` dropped whole messages oldest-first — so
feedback given in turn 1 ("don't use Redis") was the first thing to go, along with this turn's
assignments and earlier decisions. Agents with smaller windows forgot more than the others. Three
layers now keep what matters:

- **Pinned user record.** Every user message of the session sits in the goal message, which neither
  trim ever drops, and is replaced by a short reference in the transcript so it is not sent twice. This
  turn's orchestrator plan is pinned the same way. Both have a share cap so a pasted document cannot
  push the head out of the window. Later turns' plan prompts, speaker selection, parallel dispatch and
  the synthesis prompt carry the record too.
- **Decision ledger.** After each round (except the last) and after synthesis the orchestrator rewrites
  requirements, decisions, rejected alternatives, open issues and owners. It is placed **directly before
  the turn instruction** in every call — not in the system prompt, whose custom instructions are
  injected exactly as before — so ledger updates do not break the provider's prompt cache. It is saved
  only when a turn completes, so an aborted turn leaves nothing behind; a failed update keeps the
  previous ledger. It is shown read-only under the custom instructions box and carried over when a
  session is continued.
- **Summaries instead of drops.** When a request would exceed the window, the oldest messages are
  folded into a rolling summary pinned in the goal message; an agent whose window fits the original
  keeps reading it. Dropping remains the fallback when summarizing fails.

**Speeches are passed on in proportion to what the listener needs.** Speakers end with a short
`## 요지` (no extra call). Each agent receives speeches made since it last spoke in full; older long
speeches by others arrive as their digest; its own speeches, short ones and ones that `@`-mention it
arrive in full with long code blocks replaced by a one-line reference (lines, first line, likely file).
Diagrams are never replaced, and the synthesis still reads everything verbatim. In a test with three
specialists over three rounds the critic's last prompt shrank to about 54 % of the verbatim transcript.
The round counter moved from the goal message to the end as well, so the transcript prefix stays
cacheable between rounds.

**Long tool loops keep the ledger and the turn instruction.** A speech's last user message (ledger +
"your turn" instruction with the strategy guidance, parallel task and digest request) was pushed behind
the tool results and dropped once the loop overflowed the window. It is now restored into the elision
notice when that happens — once, clipped to fit if the window is nearly full, and never at the cost of
an overflowing request.

Reproduced as a test: 8k window, ~2,100-token speeches, a turn-1 constraint, a three-round turn 2. Before,
the constraint reached the endpoint in none of the nine turn-2 speeches; now in all nine, with nothing
dropped.

The test suite also stopped writing into the developer's real `multiagent.db`: the database engine is a
singleton fixed by whoever creates it first, and some tests created it with the default path.
`tests/conftest.py` now creates it on `:memory:` before each test.

→ [Conversation Memory](orchestration/context-memory.md) · [Database Schema §2.1](architecture/database-schema.md)

**Also since v0.8.2**

- Remote login was refused with `cross-origin request refused` even with the right token. The login page
  sent `Referrer-Policy: no-referrer`, which makes browsers send `Origin: null` on its form POST. The
  policy is now `same-origin` (still no Referer to other sites). → [UI Components §1.3.6](ui/components.md)
- The workspace download list sorts by size and modified time as well as path, on the numeric values
  (`9 KB` no longer sorts after `10 MB`). → [UI Components §1.3.5](ui/components.md)
- The session list can be sorted by name, start time, or completion time of the latest turn (when its
  synthesis finished), in either direction, besides the previous most-recently-changed order. Sessions
  not yet started or completed stay at the bottom; leading emoji are ignored for names; the choice is
  remembered per browser. → [UI Components §1.1](ui/components.md)
- `conf.example.json` defaults `max_tokens` to 16000 (`llm` and the orchestrator), so file-writing
  agents and the final synthesis are not cut off at 4096.

---

## v0.8.2

**Remote access now requires the owner token.** MADO had no authentication: bound to `0.0.0.0`, anyone on the
network could read conversations, change MCP commands, run code through the sandbox and download files. Now
the server PC itself (loopback) needs nothing, while other PCs log in at `/login` with `MADO_ACCESS_TOKEN`
from `.env` (exactly 24 letters/digits) and stay logged in for 7 days. Without a valid token remote access is
refused entirely. One outer ASGI layer covers pages, NiceGUI's websocket, `/api/*` and downloads; cookies are
signed with a key derived from the token, logins lock after five failures, and Origin/Host are checked even on
loopback against malicious local pages and DNS rebinding. A key button next to the info button — shown and
honoured only on the server PC — applies the token from `.env` (keeping the current one if that value is
invalid) or generates and saves a new one; either way every remote login is ended. No HTTPS yet; use an SSH
tunnel when the network is not trusted, and do not put MADO behind a reverse proxy.

For servers already bound to `0.0.0.0` without a token, the first page opened on the server PC generates one,
saves it to `.env` and shows it in a popup ("외부 유저 인증 토큰이 없어 새 토큰(`…`)으로 서버를 시작했습니다.
`.env`에 저장하였습니다."). A malformed token written by the owner is never overwritten.

The key dialog also lists IPs locked after failed logins and lets the server PC's owner lift a lock
immediately instead of waiting 15 minutes. Lockouts and unlocks are kept as an audit trail in
`data/security/login_audit.jsonl` (IP, times, failure count, User-Agent — never the submitted token), and the
dialog shows the most recent entries.

→ [UI Components §1.3.6](ui/components.md) · [Environment Variables §3.1](configuration/environment-variables.md)

---

## v0.8.1

**Download workspace files.** A `작업 공간 파일 다운로드` button under the workspace input, and a
`작업 공간 파일` button on report tabs (also at the end of the report), open a list of the applied
workspace's files, newest first. One file downloads as is; several download as a zip with
workspace-relative paths (max 5,000 files / 1 GB). Paths are re-checked on the server, so nothing outside
the workspace is packed. A stale-content bug in NiceGUI's default download (path-derived URL cached for an
hour) was found in the browser and avoided: each download gets a fresh, single-use, uncached URL.

→ [UI Components §1.3.5](ui/components.md)

**Flowcharts with sequence-diagram syntax no longer slip through.** A diagram failed to render with
`Parse error … Note right of Validator: Expecting 'SEMI', … got 'NODE_STRING'`: the model had put a
sequence-diagram `Note` inside a flowchart. The syntax checker had no rule for mixed diagram kinds, so
it passed the diagram and no repair was requested — and a diagram taken from a specialist's speech
(when the synthesis has none or fails) was never checked at all.

- New lint rule `sequence-syntax-in-flowchart` (`note`/`participant`/`actor`/`activate`/`loop`/`alt`/
  `opt`/`par`/`critical`/`break`/`rect`/`else`/`and` followed by text, and `->>`, `-x`, `-)`),
  calibrated case by case against the Mermaid parser NiceGUI ships so that these words used as node
  names are not flagged.
- `Note right of|left of|over X: text` in a flowchart is rewritten without an LLM as
  `X -.- mado_note_1["text"]`; the converted form parses.
- Every diagram artifact is checked after that; one that still fails is titled `⚠ …`, including
  specialist diagrams that never get LLM repair.

**An edge-case pass over the Mermaid pipeline.** 121 cases were checked against the real parser, raw
and normalised; defects found and fixed:

- Valid diagrams flagged: YAML front matter or a BOM before the declaration, asymmetric shapes `A>x]`,
  `opt:::red` class shorthand, `end;`.
- Normalisation changed visible text: `[결제 (PG)]` was quoted in sequence, state, gantt, class, ER,
  journey and timeline diagrams, where it is valid. Label quoting now runs only for flowcharts and
  mindmaps.
- A backtick in a converted note broke it; re-normalising reused `mado_note_1`.
- Now fixed or flagged: `rgb()`/`rgba()` in `style`/`classDef`/`linkStyle` (converted to hex),
  parentheses inside cylinder, slanted, double-circle and asymmetric shapes and bare subgraph titles,
  and `subgraph id "title"`.
- **Code-block extraction:** a fence with an info string (```` ```mermaid title="…" ````) was not
  recognised and shifted every later fence, losing the following block; `~~~` fences were ignored;
  untagged blocks starting with front matter, `%%{init}%%` or a newer kind (`kanban`) were not seen as
  Mermaid. Extraction is now a line-based scanner.

The final run had no false positives, no diagram made worse, no text changed outside flowcharts and
mindmaps, and normalisation is idempotent. The table is frozen as `tests/test_mermaid_edge_cases.py`.

→ [Artifact Synthesis §2.2, §3.1, §3.1.1](orchestration/artifact-generation.md)

---

## v0.8.0

**Mention workspace files and specialists with `@`.** Typing `@` in the input lists this
conversation's workspace files and folders and the turn's active specialists; arrow keys and
Enter/Tab pick (Enter picks rather than sends while the list is open), Esc closes. On send, the
mentions become a `[@참조]` block at the end of the message: the workspace path, each file's relative
path and size, folders, and the named specialists. **Only paths are sent, never file contents** — a
user message is copied into every transcript and the synthesis each round, so contents would saturate
the context. Any file type can be mentioned, PDFs and Office documents included; reading them is up to
the MCP server that handles the format.

Safeguards: paths outside the workspace (absolute, `..`, outward symlinks) are refused; missing paths
and specialists switched off for the conversation are dropped with a warning; `@` inside code and
e-mail addresses is not a mention; the listing skips heavy folders and simple top-level `.gitignore`
rules, stops at 20,000 entries, is filtered on the server (30 results) and cached for 5 s; text
returned by abort-and-edit loses its block so re-sending does not duplicate it. Naming a specialist is
passed on as text and does not override the strategy's speaking order.

**Upload files into the workspace.** A button left of the input saves files to
`<workspace>/uploads/` (never overwriting — `name (2).ext`; 100 MB per file) and inserts `@path`.
The listing cache is cleared, so the file is offered immediately.

→ [UI Components §1.3.4](ui/components.md) · [Debate turn (manual)](../docs/user_manual/04-workflows/01-debate-turn.md)

---

## v0.7.2

**Debate results no longer disappear as turns accumulate.** Two bugs combined. The artifact viewer
replaced its tabs with each finished turn's artifacts (and a reload during a run did the same), so a
long session showed only the latest turn. And when the orchestrator's synthesis came back empty, the
empty body was saved under the normal report title — so that one empty tab was all that remained on
screen. Earlier reports were still in the database.

Now tabs are appended, titles carry the turn's finish time, and the latest report opens. An empty
synthesis — including one that is only a limit notice or only a reasoning block — is recorded as
`합성 실패 (빈 응답)` and filled with each specialist's latest speech from the turn, gathered without an
LLM; the turn is not marked as consensus.

**The orchestrator writes the conclusion and one overall diagram, not code.** The synthesis prompt and
the default orchestrator persona used to demand complete runnable source code; the orchestrator
re-emitted the specialists' code, exhausting its response limit and feeding the next turn's transcript
a code dump. Code tabs now come from this turn's specialist speeches (latest per specialist, de-duplicated,
at most 12). Update `system_prompt` in an existing `conf.json` if it still asks the orchestrator for code.

→ [Artifact Synthesis §1, §2, §5](orchestration/artifact-generation.md)

**The round-0 plan knows who is in the debate.** The planning prompt now lists this turn's specialists
— name, role and MCP tool server names, no system prompts — and asks the orchestrator to give each one,
by that name, a task and a deliverable, sending tool work to an agent that has the tool. The first-turn
prompt used to hard-code "(Architect, Coder, Critic)" regardless of the session's roster, and later
turns had no list. Speaker selection and parallel dispatch now build their roster with the same
function and also show tool servers.

→ [Engine Lifecycle, Phase 1](orchestration/engine-lifecycle.md)

**The example `timeout` is 600 s, not 120.** It is the wait between response chunks, not a limit on the
whole response. A tool call writing a long file can stream nothing until its arguments are complete, so
with `max_tokens` at the recommended 16,000 a 120 s wait cut speeches mid-generation with
`MidStreamFallbackError … Timeout on reading data from socket`. Raise `timeout` in an existing
`conf.json` the same way (above `max_tokens ÷ tokens per second`).

→ [conf.json Reference](configuration/conf-json-reference.md)

**Streaming a long speech no longer makes the page reload itself.**

Each LLM token redrew its whole card: NiceGUI converted the card's entire Markdown to HTML on the
server's event loop and resent all of it, plus a separate scroll message per token. A 20,000-character
report cost 82 s of server CPU. With server and browser both saturated the websocket dropped, and
NiceGUI reloads on reconnect when the server has already let the page go (3 s) or when more than 1,000
messages went out during the gap — which at ~200 messages per second is about five seconds.

The feed now appends chunks and redraws changed cards every 0.25 s; the engine coalesces chunk events
to one per 0.1 s (with a delayed flush so text before a slow tool still appears, drained before the
message is finalised); and the server waits 30 s for a reconnect instead of 3. For the same stream,
renders fell from 2,668 to 56 and browser messages from 3,556 to 173, stretching the gap the reconnect
history can bridge from 5 s to 81 s. Formatting still renders live.

Database writes were checked and left alone — one row per speech, after the stream ends.

**A long feed no longer stutters when the drawer or splitter moves.** Any width change re-laid out
every card, off-screen and collapsed ones in full: 45–95 ms per change with 150 cards. Cards now use
`content-visibility: auto`, so only those near the viewport are laid out (1–6 ms on the same feed).
The main splitter no longer has the server echo its value back (`QuietSplitter`, `LOOPBACK = False`).
Quasar emits the value once, on release, so this removes one extra layout per drag — not "20 times a
second" as first written here; that claim was wrong.

**Splitter drags do far less work per move.** The panes' content is pinned to its width while dragging
and laid out properly once on release (it does not follow the drag live). This roughly halves per-move
layout even with every card rendered — not zero, as Blink still revisits the frozen feed — and on a real
screen `content-visibility` keeps that remainder to the few cards near the viewport. Mermaid diagrams
in the artifact pane are shown as images — the SVG stays hidden for copy and download — so a width
change no longer re-lays out every node label. Long Markdown reports skip layout for blocks off screen;
a first version of that rule collapsed the report to 52 px wide and clipped long code lines, both
caught in the browser and fixed before shipping.

→ [UI Components §1.3.1, §1.3.2, §1.3.3](ui/components.md)

---

## v0.7.1

**A locked database no longer costs a final report.**

While the screen was locked, a final synthesis report failed to save with `database is locked`. It
had reached the screen, so nothing looked wrong until the page was reloaded and the report was gone.
Two things were missing, and either alone would have been enough to lose it.

**SQLite ran on its defaults.** A 5-second wait for a lock, and a journal file created and deleted on
every write. On Windows, a virus scan, the search indexer, or a backup agent opening the file for a
few seconds is enough — and Windows schedules exactly those while the user is away. Every connection
now gets `busy_timeout=30000`, `journal_mode=WAL`, and `synchronous=NORMAL`. WAL is skipped when the
database sits on a network location, where it can corrupt the file. Against a real 7-second lock the
write that used to fail after 5.5 s now waits and succeeds.

**A failed write was logged and dropped.** Every engine write now retries (2 s, then 5 s, on top of
SQLite's own wait), rolling back between attempts. If it still fails, the content is written to
`data/unsaved/` as Markdown — the report under `synthesis` — and a notification stays on screen until
closed, because this happens when nobody is looking. It never stops the debate.

The database page used to show a WAL comment and `connect_args` that were never in the code; it now
shows what the engine actually does.

→ [Database Schema §3](architecture/database-schema.md) ·
[Project Layout](../docs/user_manual/05-reference/02-project-layout.md)

---

## v0.7.0

v0.6.1.2 stopped telling a tool call that had run that it had not. Following that log line further
turned up five more ways `max_tokens` and the context window went wrong — each small on its own, all
showing up as the same symptom: a response cut short or gone, with nothing in the log to say which.

**Tool definitions now count against the context budget.** They are sent with every request —
filesystem, memory and git alone are 35 tools and about 5,300 tokens — but the budget counted only
the messages and kept `max_tokens + 512` for output. A conversation filled to the budget overran the
window by the size of the tool list, and the server either answered 400 or let the model write only
what was left, which reads as `finish_reason='length'` on a response that had barely started. The
budget now subtracts the tool definitions everywhere it is used, including the synthesis transcript.

**A turn whose whole budget went to reasoning gets its answer asked for again.** A reasoning model
counts hidden reasoning against `max_tokens`; think long enough and the body is empty. The turn used
to end as a lone footer — and after a tool loop, continuation glued onto the text from *before* the
tool call. Now the model is told its body was empty, handed the tail of its own reasoning with
"conclude from here", and asked once more without deliberating if needed, within
`max_continuations`. If nothing comes, the footer says reasoning used the limit rather than calling
it a cut.

The same blank card had a second cause unrelated to the limit: when a reasoning parser misses the
end-of-thinking marker, the whole output — answer included — arrives as `reasoning_content` with
`finish_reason: "stop"`. `prompt` mode discards reasoning, so the turn was empty and nothing was
logged. If that reasoning contains the protocol's `## 최종 결론`, it now becomes the body without
another call; if not, the answer is asked for again; and if that fails, the footer says the answer
was left in the reasoning and — with `show_steps` on — shows the reasoning rather than a blank card.

**`native` mode reports the `max_tokens` it actually sends.** With a thinking budget at least as
large as `max_tokens`, the request carries both added together, but the budget, notices, logs and
footers used the configured number. One function now decides it, and people see
`8,192 (설정 4,096 + 사고 예산 4,096)`.

**The token-count fallback no longer undercounts.** When `litellm.token_counter` raises, the old
fallback counted Korean at half its size and a large `write_file` turn — empty `content`, everything
in the arguments — as 4 tokens. It now counts tool-call arguments and weights characters by script.

**A tool call that leaks into the text is no longer taken as the answer.** When a server's parser
cannot read a call, the markup arrives as ordinary text. The tool never ran, a cut call was
"continued" as prose, and the raw markup stayed on the card. Common formats (Hermes/Qwen, Mistral,
Llama, DeepSeek, gpt-oss) are now recognised outside code blocks; the markup is removed, the model is
told the call did not run, and it is asked to call properly — at most twice, then a footer explains.

Found on the way: **continuation after tool use now keeps the tools defined** (with
`tool_choice="none"`). It used to leave them out, which Anthropic rejects when the conversation holds
tool calls, so Claude agents' continuations after any tool use silently failed.

→ [LLM Integration §2.4, §2.6, §2.7, §5.1](agents/llm-integration.md)

---

## v0.6.1.2

**Every speech now records when it started and when it finished, and both are shown.**

The obvious shortcut was `messages.created_at`, and it would have been wrong. The row is inserted
*after* the LLM reply arrives, so that value is roughly the end of the speech, not the start. In a
parallel round it is not a time at all: it is overwritten with the round's base time plus the
dispatch index in milliseconds, because completion order varies and a reload has to replay the
speeches in the order they were assigned. It is an ordering key, and it stays one.

So `started_at` and `finished_at` are new, real wall-clock columns. The start is taken before the
stream opens; the end is taken after the reply — including a reply that failed, since how long an
endpoint took to give up is worth knowing — and outside the write lock, so time spent waiting to
commit is not counted as speaking. A person's message and a speaker-selection note take no time
and record the same instant twice. In a parallel round the intervals overlap, which is the truth.

The migration adds both columns **without a default**. Backfilling would make every old speech
appear to start and finish at the moment of migration. Rows without them are shown with their
single `created_at` value and are never labelled a start or an end.

Each chat card shows `10:56:22 → 10:58:27 · 경과 2분 5초` under the speaker's name — the elapsed
time sits to the right of the end time and is labelled, because a bare `2분 5초` leaves the reader
guessing whether it is elapsed or remaining. The full dated line is in a tooltip; a streaming card
reads `시작 · 진행 중` and is rewritten when it finishes. The Markdown export writes
`시작 … · 종료 … · 경과 …` with dates.

The **final synthesis report** now ends with `*보고서 완료: … · 총 경과 …*`. The report is copied
and forwarded on its own, so when the conclusion was reached — and how long the turn took to reach
it — has to be written inside it. Completion is the synthesis speech's `finished_at`, taken after
diagram self-repair, so it matches that card's end time exactly. Code and diagram artifacts are
still cut from the original text, and a failed synthesis gets no completion line — it is a failure
notice, not a completed report. In the Markdown export the same total follows the synthesis
speech's own line: `시작 … · 종료 … · 경과 … · 총 경과 …`.

The turn total runs from **when the opening request was recorded** to the end of synthesis, so a
reader of the transcript can recompute it from the two times shown. It is not inferred from the
record. An interjection made during a debate is also a `user` message, and one made right after
planning is recorded with `round_number=0` — exactly like the request that opened the turn —
so "the last round-0 user message" would silently shorten the total of any turn someone spoke up
in. Instead the synthesis row records `turn_started_at` explicitly; it is `NULL` on every other
row, which also marks which row closed a turn.

All of this goes through one set of pure helpers in the new `app/timestamps.py`, so the screen,
the saved document and the report cannot disagree about a time. They began in `app/export.py`, but
the engine importing them from there closed a cycle — `app.export` → strategies → the
orchestration package → the engine → a half-loaded `app.export` — that only failed when something
imported `app.export` first. `app.main` happened to import in a safe order, which is exactly why it
would have surfaced later as a startup crash rather than now. `app.timestamps` imports nothing from
the app; `app.export` re-exports the names so existing imports keep working.

**A tool call that ran is no longer told it did not.** A log line
`Truncated tool call ... finish_reason='length', tools=['filesystem__read_file']` looked as if reading
a file had exceeded `max_tokens`. It had not: `finish_reason` describes the whole response, and the
`tools` list only named what that response was asking for. Something else in the same response —
reasoning written before the call, hidden `reasoning_content`, another call — had used the budget.
The loop treated `length` alone as proof the arguments were cut, so a read that parsed and ran was
followed by "this call did not run, do not resend it" beneath its own result, with advice to split a
file write nobody made. Now arguments that would not parse still get that notice; `length` with
readable arguments gets a truthful one — the calls ran, the last one sits at the cut point so check
its result, and anything planned after it never went out — with write advice only when that last call
was a write. The log line now reports, in sizes only, what used the budget: text, reasoning, the
prompt against the window, and each call's argument size.

→ [Database Schema §2.2](architecture/database-schema.md) · [UI Components §1.3](ui/components.md) ·
[Artifact Generation §2.1](orchestration/artifact-generation.md) ·
[LLM Integration §2.2](agents/llm-integration.md)

---

## v0.6.1.1

**The MCP status chips follow the runtime instead of the lock.**

v0.6.0 moved server startup from application boot to the first debate in a workspace, which means
the panel's state now genuinely changes while a page is open. The chips did not notice: the only
periodic path that redrew them ran when the *lock* state changed, and a debate locks the panel on
its first message and keeps the same reason until it ends. So the servers came up in two or three
seconds and the chips kept saying "미기동" until the whole turn finished.

The two-second timer now compares a fingerprint of the runtime state — connected flag and tool
count per server — alongside the lock reason, and redraws only when either moved, so the early
return that made the timer cheap is still there.

Fixed alongside it: a session whose workspace had no runtime yet fell back to the **default**
runtime's status, drawing another workspace's servers as though they were this conversation's.
A conversation with no servers running now says so, and the reconnect button explains that the
group starts with the first debate rather than starting one nobody would release.

→ [Runtime Isolation](mcp/runtime-isolation.md)

---

## v0.6.1

**A turn cut off at `max_tokens` is now continued, not just labelled.**

v0.5.3 taught the loop to notice `finish_reason: "length"` and mark the turn so a reader could see
it had been cut. That is enough for one speaker's turn in a debate — the next speaker works around
it. It is not enough for the **synthesis report**, which *is* the deliverable: a report that stops
mid-sentence has to be regenerated whether or not it carries a marker.

So the turn is continued — and every turn, not only the report: the hook sits on the tool-free
return, the ordinary end of any turn, so a specialist turn that used tools first is repaired the
same way. What was written so far goes back as an `assistant` turn, an instruction
says to resume from the last character with no preamble and no re-summarising, and the returned
piece is concatenated **with no separator** — other segments are joined by a blank line, but this
one resumes a sentence that was cut in half. Up to `max_continuations` times (default 2, `0`
disables). No tools are offered: the model is finishing a sentence, not going looking for something.

It stops when the answer finishes, when the budget is spent, or when the continuation call itself
fails — and whatever already arrived is always kept, because a failed repair must not cost the text
it was repairing. Running out of continuations leaves a footer naming how many were used, since the
reader is choosing between raising `max_continuations`, raising `max_tokens`, and asking for less.

And the rule now arrives *before* the failure: agents holding a file-writing tool carry two lines
in their system prompt saying to build a long file in sections rather than one call — naming the
append tool they actually have. Not a size limit (a model cannot count its own output tokens) but a
strategy: which tool, and what unit to split on. Agents with no file tool get nothing added.

→ [LLM Integration §2.4, §2.5](agents/llm-integration.md) ·
[conf.json Reference](configuration/conf-json-reference.md)

---

## v0.6.0

Until now the platform held one `MCPManager` for the whole process. An MCP server is told which
folder it may touch **at spawn time** — `filesystem` takes it as `argv`, `sandbox` as an
environment variable — so one manager could only ever look at one workspace. A second debate in a
different folder was refused outright (`WorkspaceConflictError`), which is the honest thing to do
when the alternative is silently reading someone else's files, but it made multi-session use
impossible.

**MCP servers are now pooled per workspace.** `MCPRuntimePool` hands out one manager per folder,
reference counted by session id: sessions sharing a folder share the processes, a different folder
gets its own group, and a turn borrows its runtime for the whole turn and returns it in a
`finally`. Idle runtimes stay warm for `MCP_RUNTIME_IDLE_TTL` so consecutive turns do not pay the
startup cost again, and `MCP_MAX_RUNTIMES` caps how many groups may exist — a memory budget, since
each one is a whole set of server processes. A runtime in use is never evicted; when nothing can be
freed the pool raises `RuntimeCapacityError` naming what holds the slots.

Worth being precise about what "isolating the Node runtime" means here: `node.exe` and
`node_modules` are read-only while running and stay shared. What was leaking was **process state** —
heap, cwd, `TMP`, `WORKSPACE_DIR`, the memory-graph directory, the sandbox's kernels — so that is
what the pool separates, by starting each group with its own environment. `HOME` is deliberately
left alone; git and Python read user configuration from it.

Two globals had to become pure first, or a second runtime would simply overwrite the first.
`mcp_servers_for_workspace()` used to assign `os.environ["WORKSPACE_DIR"]` so children would
inherit it — whichever call came last won. It now substitutes through an overrides mapping and
writes the path into each server's `env` explicitly. The MCP **Roots** callback read the same
global; it is now bound per connection, so each server is told about its own folder.

Fixing that surfaced a Windows bug that had been there all along: `Path.resolve()` can return an
extended-length path (`\\?\C:\...`), and `as_uri()` turns that into `file://?/C:/...`, which the
server rejects. The filesystem server then came up with no allowed directories, and the only trace
was one `Failed to request initial roots` line.

→ [Runtime Isolation](mcp/runtime-isolation.md)

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
