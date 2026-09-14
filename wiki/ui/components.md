# UI Components Reference

The web application workspace is organized into four primary UI components in [app/ui/components/](file:///d:/MultiAgentOrchestrator/app/ui/components/) alongside the dedicated persona editor page.

```text
+-----------------------------------------------------------------------------------+
|  Header Bar: App Title | MCP Server Status Chips (FS/Mem/Git/Sandbox) | Refresh  |
+---------------------+---------------------------------------+---------------------+
|                     | Top: Agent Roster & Controls          |                     |
|                     | [x] Architect [x] Coder [x] Critic    |                     |
|                     | Strategy: [Sequential v] Rounds: [3]  |                     |
| Left Sidebar:       +---------------------------------------+ Right Panel:        |
| - [+ New Chat]      | Center: Chat & Debate Feed            | Artifact Viewer     |
| - Session 1         | - [User] Prompt...                    | [Report] [Code]     |
| - Session 2         | - [Orch] Planning...                  | [Mermaid] [JSON]    |
| - Session 3         | - [Architect] Design...               |                     |
|                     |   v [Tool: write_file] (Accordion)    | Content &           |
|                     | - [Coder] Implementation...           | One-click Copy/DL   |
|                     | - [Critic] Audit...                   |                     |
|                     | - [Orch] Final Synthesis...           |                     |
|                     +---------------------------------------+                     |
|                     | Input: [ Type message here...    ] [>]|                     |
+---------------------+---------------------------------------+---------------------+
```

---

## 1. Component Breakdown

> Every component exposes an `alive` property and silently ignores updates once its page has been
> deleted. Debates outlive the page that started them (see
> [engine-lifecycle.md](../orchestration/engine-lifecycle.md#4-who-owns-the-running-turn)), so a
> late event arriving at a closed tab must not raise.
>
> Until v0.5.2 the sidebar was the exception — it checked only that its container *existed*, not
> that it was still alive. NiceGUI is deliberately silent when an element *update* lands on a
> deleted client (an async callback resuming after teardown is not a user bug), but **creating an
> element, `clear()`, `ui.notify` and `ui.download` all warn**: `Client has been deleted but is
> still being used`. Almost everything the sidebar draws happens *after* an `await`, so
> `refresh_list()` now reads first and draws second, re-checking `alive` in between — which also
> removes the blank-then-filled flash the old order produced. That warning is emitted once per
> process, so leaving a benign one in place would hide the next real use-after-free.

### 1.1. Session Sidebar ([app/ui/components/sidebar.py](file:///d:/MultiAgentOrchestrator/app/ui/components/sidebar.py))
- **`+ New Chat` Button**: Instantiates a fresh debate session and clears the workspace.
- **Session List**: Displays historical sessions ordered by `updated_at` descending.
- **Badges & Metadata**: Displays the first user message time and colored chips for participating agents.
- **Per-card actions**: ✏️ rename · 💾 export the whole conversation as Markdown ·
  **⑂ continue in a fresh session** · 🗑 delete.
- **⑂ Continue** (v0.5.0): creates a new conversation that inherits the workspace, the
  knowledge graph, the agent roster and the previous conclusion — clearing only the
  transcript. Disabled while that session's debate is running, because the conclusion does
  not exist yet. See
  [Session Handoff](file:///d:/MultiAgentOrchestrator/wiki/orchestration/session-handoff.md).

### 1.2. Agent Roster Control ([app/ui/components/roster.py](file:///d:/MultiAgentOrchestrator/app/ui/components/roster.py))

> The persona button's tooltip element is created once at build time and only its text is swapped
> afterwards. `Element.tooltip()` builds a new `q-tooltip` in whatever slot is current, so calling it
> from a background callback after the page was replaced raised
> `The parent element this slot belongs to has been deleted.`

- **Agent Toggle Cards**: Allows users to include or exclude specific specialists (e.g. toggling the Critic off for faster brainstorming). The Master Orchestrator is fixed and always enabled.
- **Card anatomy**: drag handle · avatar · name with `수정됨` / stance / `이 대화 전용` badges · role ·
  participation checkbox · ⋮ menu (stance, disable, delete) · model line · tool button. The checkbox
  scopes to *this conversation*; everything in the ⋮ menu writes to `conf.json`. They are deliberately
  one layer apart — side by side they are indistinguishable and the mistake is expensive.
- **Card border** (v0.5.1): normally the border reports participation — indigo when enabled, grey when
  not. An agent whose `card_color` was chosen explicitly gets that colour instead, but only while it is
  enabled, so the border never stops answering "is this agent in?" first. Agents with no chosen colour
  look exactly as they did before, which is why an upgrade does not repaint an existing roster.
- **Reordering**: cards are dragged to set `debate_priority`; the lifted card fades and the drop edge
  is marked. See [roster-editing.md](../agents/roster-editing.md#5-speaking-order-by-drag).
- **Speaking-order preview** (v0.5.0): a line under the cards showing the order this round will
  actually run in, plus a note saying how much to trust it. Only Sequential Debate follows card
  order; Adversarial interleaves the camps and the orchestrator-led/parallel strategies decide per
  round. It refreshes on drag, stance change, participation toggle, strategy change, parallel-limit
  change, and session switch — and it computes the order by calling the engine's own
  `strategy.get_speakers_for_round()` rather than reimplementing it. See
  [debate-strategies.md §1-b](../orchestration/debate-strategies.md).
- **Card width**: `min-w-[270px] max-w-[340px]`. The name row also carries `min-w-0 overflow-hidden`
  so the *name* truncates when space runs out. Without it the row could not shrink below its content,
  `truncate` never engaged, and the stance badge overflowed onto the checkbox and ⋮ button.
- **Dynamic Live Refresh**: Rendered inside a reactive container (`cards_row`). When personas are updated via the persona editor or config reloads, `refresh_agent_cards()` rebuilds the cards in place, so labels, roles, order, and badges update without a page reload.
- **Configuration Tooltips**: Hovering over an agent card reveals its configured model, endpoint URL, and sequential thinking mode.
- **Strategy Dropdown**: Selects between `sequential_debate`, `adversarial_debate`,
  `orchestrator_led`, and `parallel_dispatch`. Populated from `STRATEGY_MAP`, so a newly
  registered strategy appears without touching the UI.
- **Agent Cards**: Drag to reorder (writes `debate_priority` to `conf.json`); the ⋮ menu sets `debate_stance` and can disable or delete the agent.
- **Max Rounds Slider**: Sets the maximum debate depth ($1$ to $10$ rounds).
- **동시 실행 (Parallel Limit)**: How many agents run at once inside one round, stored as
  `sessions.parallel_limit`. Shown **only** while the selected strategy declares
  `orchestrator_dispatches_parallel` — every other strategy runs one speaker at a time and would
  never read it, and a control that does nothing is worse than no control. Lower it for a local
  single-GPU endpoint.
- **Custom Instructions Box**: Allows injecting ad-hoc guidelines into all agent prompts for the current session.
- **Persona Settings Button**: Links directly to `/personas/{session_id}`. Displays a lock icon if debate has commenced.
- **MCP Server Chips**: Displays real-time connection states (Green/Orange/Red) and opens diagnostic tooltips on hover. The tooltip names the transport and shows `command:` for a local server, `url:` for a remote one.
- **Add server dialog**: one button covers both kinds. A `로컬 프로세스 (stdio)` / `원격 (HTTP)`
  toggle swaps the field set — command/args/env against url/headers/transport — because the two
  ask for entirely different things and showing both at once leaves the reader guessing which
  half matters. There is deliberately no separate "Remote MCP" button: what the user knows first
  is that they want to attach a server, not that it speaks HTTP.
- **Workspace Field**: The folder every workspace-bound MCP server (`filesystem`, `git`, `memory`,
  `sandbox`) shares, for *this conversation*. Applying it restarts those servers, since each one
  receives its root at spawn time. The value is stored on the session row — `conf.json` is never
  written. Blocked while a debate is running, here or in another conversation.

### 1.2.1. Appearance Editor ([app/ui/components/agent_appearance.py](file:///d:/MultiAgentOrchestrator/app/ui/components/agent_appearance.py))

One editor serves both places a card's colour and icon can be set — the **에이전트 추가** dialog and
the persona editor — because two copies would drift on how a value is picked or written.

- **Live preview**: an avatar rendered with `style_for_agent()`, the same call the roster and the feed
  make, so what the preview shows is what will be drawn. It is re-created rather than mutated on every
  change: a Material icon and an `img:` image are not the same Quasar property.
- **Colour**: twelve swatches plus a colour picker. Whatever is chosen is stored verbatim, since
  NiceGUI accepts a hex CSS colour and a Quasar palette name through the same `color` argument.
- **Icon**: a Material-icon combobox (free text allowed) or **이미지 업로드**. An upload is written to
  `data/agent_icons/` the moment it is selected, and the returned relative path becomes the value.
- **Change notifications are attached after the widgets are built.** NiceGUI's inputs fire
  `on_value_change` when a value is assigned programmatically too. Wired during construction, the icon
  box — empty, because an uploaded path is not in the Material list — would report itself as a user
  selection and erase the image it was created to display. The same reason a `_syncing` flag guards
  every write-back from code.

### 1.3. Chat & Debate Feed ([app/ui/components/chat_feed.py](file:///d:/MultiAgentOrchestrator/app/ui/components/chat_feed.py))
- **Color-Coded Message Timeline**: Displays user prompts, orchestrator guidance, and specialist contributions with distinct avatars, roles, and colors.
- **Per-conversation styling** (v0.5.1): the feed does not resolve colours from `conf.json`. The roster
  hands it the agents it is drawing (`set_agent_styles()`) every time it redraws, so a locked
  conversation renders from its frozen snapshot — recolouring an agent today leaves yesterday's
  transcript as it was recorded. An agent with a chosen colour also gets that colour on its message
  border; the red border of a failed turn is never overwritten, because there the colour is the
  message.
- **Real-Time Token Streaming**: Supports incremental token streaming (`start_streaming_message()`, `append_stream_chunk()`, and `_finalize_streaming_message()`). Agent messages stream directly into reactive markdown cards as LLM completion chunks arrive.
- **Speech times** (v0.6.1.2): under the speaker's name each card shows when the speech started
  and finished and how long it took — `10:56:22 → 10:58:27 · 경과 2분 5초`, elapsed to the right of the end time — with the full dated line
  in a tooltip. A streaming card reads `10:56:22 시작 · 진행 중` and is rewritten in place by
  `_finalize_streaming_message()`. Records that take no time (a person's message) and rows from
  before the columns existed show a single time; the latter are never labelled a start or an end,
  because their only timestamp is `created_at`, which is an ordering key written after the reply.
  The end carries a date when it falls on a different local day, so a speech across midnight does
  not read as time running backwards. The Markdown export uses the same rules
  (`app/timestamps.py` `speech_timing` / `speech_time_text`) so the two never disagree.
- **Folding Tool Accordions**: Each MCP tool call (input arguments and execution outputs) renders inside an expandable Quasar accordion, preserving timeline readability.
- **Status & Progress Banner**: Shows real-time speaker indicators (e.g. `[Senior Python Engineer] 발언 및 분석 중...`) and round counters during execution.
- **Liveness indicators**: while a turn is running the feed says so in three places at once —
  the status bar pulses and brightens (`.feed-status-live`), a sweeping bar runs under it
  (`.feed-progress`), and a strip above the input shows bouncing dots, the current speaker, and
  an **elapsed seconds** counter. The counter is driven by a 1-second `ui.timer`, not by server
  events, because the window that reads as "frozen" — waiting for the first token, or a tool
  call that takes half a minute — is exactly the window in which no event arrives. The strip
  lives *outside* the scroll area: inside it, it would scroll out of view the moment a card is
  expanded, which is when it is needed most. Each phase restarts the counter, so it reads "how
  long has this speaker been going", not "how long since the turn started".
- **Auto-scroll while streaming**: the feed follows new output only while `ChatFeed.following`
  holds, which two things can revoke.
  - **An expanded card.** Expanding is how a reader says "I am reading this", and chunks keep
    arriving from other agents meanwhile. Cards drawn open by the renderer — a speech being
    streamed — do not count: only `_toggle_card()` (a real click) records into
    `_user_expanded`, otherwise following would be off for the whole debate. Finalising a
    stream leaves a user-expanded card open, and the jump button never collapses one.
  - **A wheel or touch gesture** on the scroll area (`_handle_manual_scroll`, bound with
    `throttle=0.3`). Direction is deliberately ignored: judging it would re-attach every time
    the reader nudged downward near the bottom. Watching the scroll *position* instead would
    not work at all — our own auto-scroll moves it, so the feed would detach from itself on
    every chunk. Without this, collapsing the last card re-armed auto-scroll and there was no
    way to look back through output that was still pouring in.
  While paused, an amber button in the status bar names the reason — `맨 아래로 (N개 펼침)` or
  `맨 아래로 · 따라가기 재개` — and clicking it clears what it can: a manual scroll, never the
  reader's open cards.
- **Reaching the actual bottom**: scrolling uses `scroll_to(pixels=SCROLL_TO_BOTTOM_PX)` — a
  number larger than any transcript, which the browser clamps — and never `percent=1.0`.
  Quasar converts a percentage using its own *cached* content height, which does not yet
  include the text that just arrived, so percent-scrolling landed a card's height short (over
  200px, measured) and stayed there after the turn ended. The command still executes before Vue
  patches the DOM, so a second pass runs shortly after: `_scroll_to_bottom()` raises a flag and
  a 0.2s timer created in `build_ui()` acts on it. The timer must be created there — one made
  inside the streaming consumer task never runs at all (verified: its callback never fired).
- **Input Bar**: an auto-growing textarea. **Enter sends, Shift+Enter breaks the line** — bound
  as `keydown.enter.exact.prevent` (`SUBMIT_KEY_EVENT`). `.exact` makes Vue skip the handler
  entirely while a system modifier is held, so Shift+Enter reaches the browser's default and
  inserts a newline; `.prevent` then stops plain Enter from leaving a stray newline behind in
  the box it is about to clear. The order matters: `.prevent.exact` would call preventDefault
  before the modifier is checked, killing the line break it is supposed to allow.
- **@mentions** (v0.8.0, [app/workspace_files.py](file:///d:/MultiAgentOrchestrator/app/workspace_files.py),
  [app/ui/mention_input.py](file:///d:/MultiAgentOrchestrator/app/ui/mention_input.py)): typing `@` in the
  input opens a list of this conversation's workspace files and folders plus the turn's active
  specialists. See §1.3.4.
- **Workspace upload button** (`upload_file`, left of the input): saves files into
  `<workspace>/uploads/` and inserts `@path` into the input. See §1.3.4.
- **Abort & edit** (`긴급 종료`): sits next to `정지` while a turn runs, and does the opposite —
  `정지` asks for a conclusion from what has been said, while this one is for a request that was
  wrong to begin with (a typo, the wrong paste, a prompt meant for another conversation).
  Confirmed through a dialog, it kills the task mid-speech, deletes that turn's messages, tool
  records and artifacts, and puts the submitted text back in the input box to be corrected. If
  nothing is left in the conversation it also unlocks the personas — the debate never happened,
  so the roster should be editable again. `DebateRunner.abort()` reports what to delete;
  `app.session_ops.discard_turn()` does the deleting.
- **Failure Cards**: A message with `msg_type="error"` — an agent whose endpoint never answered — renders on a rose background with an `응답 없음` badge. `finalize_streaming_message()` restyles the card in place when a turn that had started streaming ends in failure.
- **Reattachment**: `render_all(messages, streaming_ids=…)` rebuilds the whole feed from a
  snapshot and re-registers any still-streaming card, so a page opened mid-debate keeps receiving
  chunks.
- **Collapse on completion**: a finished speech is clamped to its first three lines, and its tool
  accordions are hidden with it — clamping the prose while leaving five accordions open saves
  nothing. A card being written stays expanded; watching generation is the point of the screen, and
  a clamped card would just cycle three lines. Reloading re-renders finished speeches collapsed.
  The expand control sits top-right and only appears on speeches long enough to clamp
  (`is_clampable()`); a chevron next to a one-line "no objection" is pure noise.
  Clamping uses `max-height`, not `-webkit-line-clamp`, which requires `display: -webkit-box` and
  would break the block layout of mixed markdown. The cut edge is faded with `mask-image` rather than
  an overlaid gradient, so it needs no per-speaker background colour.
- **Copy button**: a `content_copy` button on every card writes the **markdown source** — not the
  rendered text — to the clipboard, whether or not the card is collapsed.
  `navigator.clipboard` exists only in secure contexts (HTTPS or localhost). With the default
  `host = "0.0.0.0"`, a colleague opening `http://<ip>:8000` has no such API, and the button would
  silently do nothing while reporting success. [app/ui/clipboard.py](file:///d:/MultiAgentOrchestrator/app/ui/clipboard.py)
  falls back to a hidden textarea and `execCommand('copy')`; the artifact viewer's copy button uses
  the same helper.

### 1.3.1. Streaming without taking the page down (v0.7.2)

Long speeches used to make the page reload itself. Every LLM token reached `append_stream_chunk()`,
which called `markdown.set_content()` with the card's **entire** content and scrolled. NiceGUI converts
Markdown to HTML on the server, synchronously on the event loop (Pygments included for code blocks),
and sends the whole HTML again; a scroll is a separate `run_javascript` message that is never merged.
For a 20,000-character report that was 6,667 full conversions and 82 s of server CPU, over half the
event loop near the end.

A busy server and a busy browser drop the websocket, and NiceGUI reloads the page on reconnect in two
cases: the server already deleted the client (`reconnect_timeout`, 3 s by default), or more messages
were sent during the gap than `message_history_length` (1,000) can replay — the browser logs
`reloading because outbox rewind failed`.

Three changes, each closing a different link:

| Where | Change |
| :--- | :--- |
| `ChatFeed` | A chunk only appends and raises a flag. `_flush_streams()`, on a `STREAM_RENDER_INTERVAL` (0.25 s) timer created in `build_ui`, redraws each changed card once and scrolls once. (A timer created from the background consumer never runs — the same reason `_settle_scroll` uses a flag.) The final `message_added` still draws immediately. |
| `OrchestratorEngine._speak` | Chunk events are coalesced to one per `STREAM_EVENT_INTERVAL` (0.1 s), so a briefly slow page no longer overflows its subscriber queue and gets silently dropped mid-stream. A short delayed flush is armed on the first pending chunk — "send when the next chunk arrives" would hide the line written before a 30-second tool call — and it is drained or cancelled on every exit path, so no chunk lands after the message is final (the runner's snapshot would append it to the final text a second time). |
| `app/main.py` | `reconnect_timeout=30.0`: a few seconds of busyness, a monitor switching off, or waking from sleep resumes the page instead of rebuilding it. |

Database writes were not touched: they were already one row per speech, written after the stream ends.
Nothing writes per chunk.

Measured in a browser, same 8,000-character report fed at 200 chunks/s on a wall clock:

| | Before | After |
| :--- | ---: | ---: |
| Full Markdown renders | 2,668 | 56 |
| Messages to the browser | 3,556 (194/s) | 173 (12/s) |
| Gap the 1,000-message history can bridge | 5.1 s | 81 s |
| Worst event-loop stall | 41 ms | 14 ms |
| Stream duration (target 13.3 s) | 18.3 s | 14.0 s |

The old version could not even keep pace with its own feed.

### 1.3.2. Many cards, a resize, and a splitter drag (v0.7.2)

With many speech cards in the feed, collapsing the session drawer or dragging the artifact
splitter stuttered. Any width change makes the browser re-lay out **every** card — off-screen ones
included, and collapsed ones in full, because `.chat-body-clamped` only caps `max-height`. With 150
cards (37,011 DOM nodes) one width change cost 45–95 ms; the drawer animation and a drag pay that per
frame.

* **`content-visibility: auto`** on `.debate-timeline > .q-card` lets the browser lay out only the
  cards near the viewport. On the same 150 cards a width change dropped to 1.3 ms at the latest
  speech, 3.6 ms in the middle and 6.1 ms at the top, and barely grows with more cards.
  `contain-intrinsic-size: auto 220px` estimates never-rendered cards and remembers real heights
  afterwards so the scrollbar jumps less. Off-screen text stays in the DOM, so copy and find are
  unaffected.
* **`QuietSplitter`** ([app/ui/components/quiet_splitter.py](file:///d:/MultiAgentOrchestrator/app/ui/components/quiet_splitter.py))
  sets `LOOPBACK = False`. Quasar's `QSplitter` writes the panel's `style.width` directly on every
  mouse move and emits `update:modelValue` **once, on release** (it is not `emit-immediately`). With
  loopback on, the server sent that value straight back, laying out both panes once more at the moment
  of release. Driving NiceGUI's change handler with three values gave 3 echoed updates from
  `ui.splitter` and 0 from `QuietSplitter`, with `splitter.value` still up to date; values set from the
  server still reach the browser.

  > **Correction.** An earlier version of this page, and of the v0.7.2 notes, said the value went to
  > the server every 50 ms during a drag (NiceGUI's `throttle=0.05`) and was echoed 20 times a second.
  > Quasar's `pan()` handler shows otherwise: during a drag nothing is emitted. The echo removed here is
  > one per drag, so its effect is far smaller than claimed.

### 1.3.3. Dragging the splitter without re-laying out the panes

`content-visibility` made drawer toggles smooth, but a splitter drag still stuttered, because every
mouse move changes `style.width` and the browser re-lays out whatever is inside both panes — and the
artifact pane is not a list of cards, so `content-visibility` on cards does nothing for it.

**A. Freeze the panes while dragging** (`SPLITTER_FREEZE_JS`, class `mado-freeze-on-drag` on
`QuietSplitter`). On `mousedown` on the separator (listened for in the capture phase — Quasar's pan
directive stops propagation) each pane's content is pinned to its current pixel width and the panels
clip overflow; on `mouseup`, `touchend`, `touchcancel` or window `blur` the original inline widths are
restored and the browser lays out once. The trade-off is that content does not follow the drag: the
shrinking side is clipped and the growing side shows empty space until release.

In the browser the freeze engaged and released exactly (pinned `905.953px` / `655.047px`, restored to
`""`, `overflow` back to `auto`). Per-move layout with every card forced to render fell from 11.7 to
5.8 ms at 60 cards and from 35.4 to 15.5 ms at 180. It did **not** reach zero: hiding the frozen
subtree drops the cost to 0 ms and setting the same width every time costs 0 ms, yet neither a pinned
height nor `contain: strict` let Blink reuse the frozen feed's layout. The remainder is entirely in the
feed pane (artifact pane frozen: 0.3 ms) and scales with *rendered* cards, which `content-visibility`
keeps to the few near the viewport on a real screen.

**B. Show Mermaid diagrams as images** (`MERMAID_IMAGE_JS`). Mermaid inserts its SVG into the DOM and
draws node labels as HTML inside `foreignObject`, so each width change rescaled the diagram and re-laid
out every label. When an SVG appears directly under `.mado-mermaid`, a serialised copy with explicit
`width`/`height` from its `viewBox` is added as an `<img>`; the original SVG is hidden with
`display:none` **only after the image loads** (on error the image is removed and the SVG stays).
Hiding rather than removing keeps copy/download working — `MadoMermaid.getSvgData` still finds the SVG
and sizes it from `viewBox`, which is why an SVG without a `viewBox` is left alone. Verified with a
60-node flowchart (1,015 SVG nodes, 133 `foreignObject` labels): the image loaded at 215×7646, the SVG
was hidden, `getSvgData` returned export data, and a screenshot showed the Korean labels rendered in
the image. The artifact pane's per-move layout while frozen was 0.3 ms. Text inside a diagram can no
longer be selected by dragging.

**C. Skip off-screen blocks of a long report.** Markdown artifacts get `artifact-report`, and every
top-level block of their rendered Markdown (paragraph, heading, list, code block) gets
`content-visibility: auto`. On a 135-block report a width change dropped from 6.2 to 3.5 ms with the top
15 blocks rendered; the gap grows with length. Code and JSON artifacts are a single `<pre>` and have
nothing to split.

The first version of C used `contain-intrinsic-size: auto 3em`, which estimates **width** as well. The
report sits in a NiceGUI column with `align-items: flex-start`, which sizes to content — so the whole
report **collapsed from 539 px to 52 px** while its blocks were skipped, and on a real screen its width
would have shifted as different blocks rendered. It also clipped long code lines: a skipped block paints
only inside its own box, and the `<pre>` did not scroll on its own (a 600-character line in a 52 px box).
The shipped rules make the report and its Markdown `width: 100%`, estimate only
`contain-intrinsic-block-size: auto 3em`, and give `pre` `overflow-x: auto`; re-measured, the report is
539 px either way and the long line is reachable by scrolling.


> Measured with a forced synchronous layout, not animation frames: the preview window was hidden, so
> nothing painted. A hidden page also skips *visible* cards under `content-visibility`, so the cards
> within one viewport height were forced to render before measuring; forcing all cards brought the
> cost back to ~45 ms, confirming the method.

### 1.3.4. @mentions and workspace upload (v0.8.0)

**Paths, never contents.** A user message is copied into every speaker's transcript and the
synthesis each round. Inlining a file there would recreate, on the input side, the context saturation
removed from the synthesis. So a mention sends only a path and the agent reads what it needs with its
own tools. File type is therefore irrelevant: PDFs and Office documents are listed and can be
mentioned; reading them is the job of whichever MCP server handles the format (e.g. an office MCP).

**Flow.**

1. The browser script (`MENTION_JS`) watches `input`, `click` and `compositionend` on the element with
   class `mado-mention-input`. When the text before the caret ends in `@fragment` (preceded by the
   start of text, whitespace or an opening bracket), it sends `emitEvent('mado_mention_query',
   {id, seq, query})`.
2. `ChatFeed._handle_mention_query` checks `id` against its own `data-mado-mention` value and asks
   the page's provider. The provider scans the workspace off the event loop
   (`run.io_bound(WorkspaceIndex.get)`), and `suggest_mentions()` returns at most 30 items — active
   specialists first, then files and folders ranked by name prefix, name substring, path prefix, path
   substring and subsequence. The full list never goes to the browser.
3. `MadoMention.show(id, seq, items)` renders a fixed-position list above the input; answers with an
   old `seq` are dropped.
4. On send — a new turn or an interjection — `with_references()` in `app.py` runs `expand_mentions()`
   and appends a `[@참조]` block with the workspace's absolute path, each file's relative path and size
   (plus "read only the parts you need" at 1 MB or more), folders, and named specialists. Anything
   dropped is reported with a warning toast.

**Keyboard.** The input's Enter is already bound to send (`keydown.enter.exact.prevent`), and Vue
attaches that listener to the native element. While the list is open, Enter must pick instead, so the
script listens for `keydown` on `document` **in the capture phase** and stops propagation before the
event reaches the element. With no candidates it does not intercept, so Enter still sends. Keys during
IME composition (`isComposing` / keyCode 229) are left alone, so the Enter that commits Hangul is not
turned into a pick. Esc closes the list and suppresses it for that same `@`.

**Identity.** NiceGUI 3 does not render DOM ids on elements, and QInput forwards attributes to the
inner `<textarea>` rather than the outer `<label>`. The feed sets `data-mado-mention="feed-…"` via
props, and the script reads it from the native input first. The first browser run found both issues:
the popup never opened because `host.id` was empty.

**Safety.**

| Case | Handling |
| :--- | :--- |
| Absolute path, drive letter, `..`, symlink leading outside | `safe_workspace_path()` refuses it (compares after `resolve()`); reported as outside the workspace |
| Path-like token that does not exist | Dropped, reported |
| Specialist switched off for this conversation | Dropped, reported; not offered in the list |
| `@` inside fenced or inline code, e-mail addresses | Not a mention (`@app.get` in pasted code stays text) |
| Text restored by abort-and-edit | `strip_reference_block()` removes the block; re-sending rebuilds it, never duplicates it |
| Heavy folders | `.git`, `node_modules`, virtualenvs, `dist`, `build`, … and simple rules from the top-level `.gitignore` (negations ignored) are skipped |
| Huge workspace | Scan stops at 20,000 entries (`truncated`) |
| Stale list | Cached for 5 s per workspace; an upload invalidates it immediately |

**Specialist mentions are text.** The block tells the orchestrator and speakers who was named; the
strategy's speaking order is not overridden. The orchestrator is not a mention target.

**Upload.** `store_workspace_upload()` writes to `<workspace>/uploads/`. The name is stripped of path
components, characters Windows forbids and reserved device names; an existing name becomes
`name (2).ext` and the file is opened with `xb`, so a concurrent upload that took the same name fails
instead of overwriting. Files are capped at 100 MB (also enforced by `ui.upload`). After writing, the
workspace's cached listing is invalidated and `ChatFeed.insert_mention()` puts `@path` at the caret.

**Verified in a browser** with the real `ChatFeed` against a temporary workspace (events dispatched by
script because the preview window was hidden, so real keystrokes and IME composition were not
exercised): `@sp` listed `docs/spec.md`, `src/cache.py` and the PDF; `@` listed the specialist, both
folders and all files; ArrowDown + Enter on `@src` inserted `@src/cache.py ` and sent nothing; picking
`@sys` inserted `@"System Architect"`; Esc closed the list; the next Enter sent, and the server received
the typed value with the reference block attached; a simulated upload inserted
`@"uploads/새 설계서.docx"` and `@새` listed it at once.

### 1.4. Artifact Viewer ([app/ui/components/artifact_viewer.py](file:///d:/MultiAgentOrchestrator/app/ui/components/artifact_viewer.py))
- **Tabs accumulate across turns.** `add_artifacts()` appends a finished turn's artifacts (skipping ids
  already shown) and opens that turn's report; `render_artifacts()` is only for rebuilding from a full
  list. See [Artifact Synthesis §5](../orchestration/artifact-generation.md).
- **Tabbed Interface**:
  - **Final Conclusion Tab**: Markdown rendering of the orchestrator's conclusion, or — if the
    synthesis was empty or failed — each specialist's latest speech.
  - **Source Code Tab**: Language-highlighted code from this turn's specialist speeches.
  - **Architecture Diagram Tab**: Interactive SVG rendering of Mermaid diagrams. If Mermaid rejects
    the source, the panel shows the parse error and the raw diagram text rather than going blank.
  - **JSON Summary Tab**: Structured session metadata.
- **Action Toolbar**: Includes one-click **"Copy to Clipboard"** and **"Download File"** buttons for all extracted artifacts.

### 1.5. Persona Editor Page ([app/ui/personas_page.py](file:///d:/MultiAgentOrchestrator/app/ui/personas_page.py))
- Dedicated page accessible at `/personas/{session_id}`.
- Renders cards for each registered agent with editable fields:
  - **Display Name**
  - **Role Title**
  - **System Instructions**
- **Draft & Reset Controls**: Allows saving drafts to `session_agents` or resetting to `conf.json` defaults.
- **Lock Banner**: If the session has already begun (`personas_locked = true`), inputs are disabled, displaying a read-only warning badge.
- **In-Progress Banner**: If a debate is running for this session, a banner says so and states that
  opening this page does not interrupt it. Navigating here used to kill the running turn.
