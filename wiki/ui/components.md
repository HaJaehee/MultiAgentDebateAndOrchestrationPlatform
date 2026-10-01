# UI Components Reference

The web application workspace is organized into four primary UI components in [app/ui/components/](file:///d:/MultiAgentDebateOrchestration/app/ui/components/) alongside the dedicated persona editor page.

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

### 1.1. Session Sidebar ([app/ui/components/sidebar.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/sidebar.py))
- **`+ New Chat` Button**: Instantiates a fresh debate session and clears the workspace.
- **Session List**: Displays historical sessions, by default ordered by `updated_at` descending.
- **Sorting** (v0.8.3): a selector next to `세션 목록` — `최근 변경` (the old order, default), `이름순`,
  `시작 시간순` (first user message, the time the card shows), `완료 시간순` — plus a direction
  button. Each key starts in its natural direction (names A→Z, times newest first).
  - *Completion* is when the latest turn's **synthesis** finished (`finished_at` of the newest row
    with `turn_started_at`, which only synthesis rows carry). Interjections after the synthesis or a
    turn in progress do not move it. Conversations older than the timing columns fall back to the
    last orchestrator message. While sorting by completion the card shows `완료 MM-DD HH:MM` instead of
    the start time, so the order does not look wrong.
  - Sessions without the value (`시작 전`, `완료 전`) stay at the bottom in **both** directions; ties
    fall back to most recently changed.
  - Names compare case-insensitively and ignore leading emoji and symbols — `🛒 이커머스 …` sorts
    under `이`, not after every letter.
  - The choice is a viewer preference, so it lives in the browser (`localStorage["mado.sessionSort"]`),
    restored once the page connects; a malformed value falls back to the default. There is no
    `storage_secret`, so NiceGUI's per-user storage is not available.
  - Pure logic in `sort_sessions()` / `title_sort_key()` / `last_completion_times()`, tested in
    `tests/test_session_sort.py`.
- **Badges & Metadata**: Displays the first user message time and colored chips for participating agents.
- **Per-card actions**: ✏️ rename · 💾 export the whole conversation as Markdown ·
  **⑂ continue in a fresh session** · 🗑 delete.
- **⑂ Continue** (v0.5.0): creates a new conversation that inherits the workspace, the
  knowledge graph, the agent roster and the previous conclusion — clearing only the
  transcript. Disabled while that session's debate is running, because the conclusion does
  not exist yet. See
  [Session Handoff](file:///d:/MultiAgentDebateOrchestration/wiki/orchestration/session-handoff.md).

### 1.2. Agent Roster Control ([app/ui/components/roster.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/roster.py))

> The persona button's tooltip element is created once at build time and only its text is swapped
> afterwards. `Element.tooltip()` builds a new `q-tooltip` in whatever slot is current, so calling it
> from a background callback after the page was replaced raised
> `The parent element this slot belongs to has been deleted.`

- **Agent Toggle Cards**: Allows users to include or exclude specific specialists (e.g. toggling the Critic off for faster brainstorming). The Master Orchestrator is fixed and always enabled.
- **Card anatomy**: drag handle · avatar on the top line, then name with `수정됨` / stance /
  `이 대화 전용` badges and role *below* it (v0.9.1: always stacked — side by side, the row wrapped only
  for long names, so cards disagreed in shape) · participation checkbox · ⋮ menu (stance, disable,
  delete) · model line · tool button (icon over `도구 N`, `stack`). The checkbox
  scopes to *this conversation*; everything in the ⋮ menu writes to `conf.json`. They are deliberately
  one layer apart — side by side they are indistinguishable and the mistake is expensive.
- **Card border** (v0.5.1): normally the border reports participation — indigo when enabled, grey when
  not. An agent whose `card_color` was chosen explicitly gets that colour instead, but only while it is
  enabled, so the border never stops answering "is this agent in?" first. Agents with no chosen colour
  look exactly as they did before, which is why an upgrade does not repaint an existing roster.
- **Reordering**: cards are dragged to set `debate_priority`; the lifted card fades and the drop edge
  is marked. See [roster-editing.md](../agents/roster-editing.md#5-speaking-order-by-drag).
- **View options** (v0.9.1): two checkboxes beside the roster title, held on the roster instance only
  (not saved; a reload turns them off).
  - `요약 보기` — each card is just avatar + name, no checkbox or ⋮ menu; role and model are in the
    tooltip. Participation still shows as the dimmed style.
  - `비활성 에이전트 숨기기` — cards unchecked for this conversation are not built, the
    `꺼둔 에이전트` row is hidden, and a `비활성 N개 숨김` note ends the card row. Unchecking a card
    while this is on redraws the row so the card disappears at once.
  - Dragging keeps working in both modes. A drop re-ranks the **full** roster list relative to the
    target card, so hidden agents keep their position in the speaking order.
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
- **그래프 row (v0.9.0)**: shown only for 그래프 토론. A graph picker, a status line with the
  validation summary, **그래프 편집** (enabled once a graph is picked), **새 그래프** (saves a
  start → end graph, stores the choice on the session, then opens the editor) and **현재 카드 순서로
  만들기**. While this row is visible the participation checkboxes show whether each agent is placed in
  the graph and are disabled — in this strategy the graph decides who takes part, and a checkbox that
  changes nothing would mislead. Below it: a read-only preview of the graph, a one-line run summary and
  **크게 보기** — see §1.2.2.

### 1.2.2. Graph execution overlay (v0.9.0)

- **What is drawn**: the preview canvas (`GraphCanvas(readonly=True)`) and the 크게 보기 dialog show the
  same run state, pushed with `set_run(view)` from `GraphRunTracker.view()`:
  - nodes: running (amber border that pulses; a steady ring under `prefers-reduced-motion`), done
    (chip `✓`, `✓ ×2`), gate verdict (chip `예` / `아니오`, with visit count), failed (`⚠ 실패`), not run
    yet (dimmed), and a `상한` badge when a node hit its visit cap;
  - wires: flowed (thicker), flowing into a running node (amber, animated), not flowed (faded); a gate wire
    that flowed is labelled `예 ✓` / `아니오 ✓`.
- **Which graph**: while a turn runs, the graph frozen for that turn (the file may change mid-turn).
  Afterwards the selected file, with the last turn's state only if the file still equals that turn's
  snapshot — an old picture on an edited graph would show wires flowing that no longer exist
  ("지난 턴 이후 그래프가 바뀌어 실행 표시를 지웠습니다").
- **Summary line**: `N단계 진행 중 — 구현, 보안 검토`, `N단계 마침 — 다음 단계 준비 중` (between steps),
  `N단계에서 끝남 · 최종 합성 노드에 닿음 — 최종 합성 중`, and `지난 턴 · N단계까지 실행` once finished.
- **Refresh**: the page rebuilds the tracker from the runner snapshot (its `graph` state and this turn's
  messages) while a turn runs, otherwise from the database and `sessions.graph_snapshot`. Slicing database
  messages at the last synthesis during a run was wrong: the synthesis is written a moment before the turn
  closes, and a page loaded in between drew an empty graph.
- **Chat badges**: cards from a graph node get `구현 · 2회차`, `판정 · 아니오`, `구현 · 방문 상한`
  (`NodeBadges`, counted per turn in record order); the round badge reads `Step N`. The Markdown export
  writes the same name after the speaker (`— 노드 “구현 · 2회차”`), a `그래프` row in the header and
  `N단계` headings.
- **Lessons**:
  - Update Vue Flow nodes and edges **in place**. Replacing them with new objects of the same id leaves
    them unmeasured (0 × 0) and `fitView` does nothing.
  - Measure after layout settles. A canvas created during the dialog's open transition read every handle
    at 0,0, so wires started at the node's top edge; the dialog canvas is drawn on `show`, and `fit()`
    calls `updateNodeInternals()` first. (In the automated browser pane, hidden pages run no animation
    frames, so measurements there stay 0 until a frame is drawn — not a product bug.)
  - Backward wires (a gate's `아니오` back to an earlier node) use a `loop` edge that dips below the nodes
    it spans; the default curve ran straight behind the node row and hid its label.
  - The roster width limits a wide graph to about 30 % zoom, so extra height does not help — hence a
    dialog, not a taller preview.
- **Custom Instructions Box**: Allows injecting ad-hoc guidelines into all agent prompts for the current session.
- **결정 장부 (Decision Ledger, v0.8.3)**: a read-only expansion directly under the custom instructions
  box, captioned with its character count (`비어 있음` when empty). The orchestrator rewrites it after
  rounds and synthesis; every call carries it directly before the turn instruction. Updated
  live from `ledger_updated`, or from the runner snapshot when a page attaches to a running turn (the
  database only has it once the turn ends). See [Conversation Memory](../orchestration/context-memory.md).
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

### 1.2.1. Appearance Editor ([app/ui/components/agent_appearance.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/agent_appearance.py))

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

### 1.3. Chat & Debate Feed ([app/ui/components/chat_feed.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/chat_feed.py))
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
- **@mentions** (v0.8.0, [app/workspace_files.py](file:///d:/MultiAgentDebateOrchestration/app/workspace_files.py),
  [app/ui/mention_input.py](file:///d:/MultiAgentDebateOrchestration/app/ui/mention_input.py)): typing `@` in the
  input opens a list of the turn's active specialists, the usable skills, and this conversation's
  workspace files and folders. `@specialist @skill` designates a skill. See §1.3.4.
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
  silently do nothing while reporting success. [app/ui/clipboard.py](file:///d:/MultiAgentDebateOrchestration/app/ui/clipboard.py)
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

If the page still drops, [Diagnostics](../operations/diagnostics.md) records which side froze: the stack
of whatever held the server's event loop, and the browser's long tasks and disconnect reason.

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
* **`QuietSplitter`** ([app/ui/components/quiet_splitter.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/quiet_splitter.py))
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
   specialists first, then usable skills (matched on name or description), then files and folders ranked
   by name prefix, name substring, path prefix, path substring and subsequence. The full list never goes
   to the browser.
3. `MadoMention.show(id, seq, items)` renders a fixed-position list above the input; answers with an
   old `seq` are dropped.
4. On send — a new turn or an interjection — `with_references()` in `app.py` runs `expand_mentions()`
   and appends a `[@참조]` block with the workspace's absolute path, each file's relative path and size
   (plus "read only the parts you need" at 1 MB or more), folders, named specialists and designated
   skills. Anything dropped is reported with a warning toast. The skill designations are also returned
   as data and passed to the runner with the message.

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
| Skill that is off or broken | Not offered; mentioned anyway, it is dropped and reported |
| Skill with no specialist to pair with | Not designated; reported with an example (`@전문가 @스킬`) |
| Text restored by abort-and-edit | `strip_reference_block()` removes the block; re-sending rebuilds it, never duplicates it |
| Heavy folders | `.git`, `node_modules`, virtualenvs, `dist`, `build`, … and simple rules from the top-level `.gitignore` (negations ignored) are skipped |
| Huge workspace | Scan stops at 20,000 entries (`truncated`) |
| Stale list | Cached for 5 s per workspace; an upload invalidates it immediately |

**Specialist mentions are text.** The block tells the orchestrator and speakers who was named; the
strategy's speaking order is not overridden. The orchestrator is not a mention target.

**Skill mentions are designations.** A skill goes to the specialist mentioned just before it on the same
line (else the first one after it on that line, else the nearest one on an earlier line), and that
specialist's speeches in this turn start with the skill already loaded — even if it is outside the
specialist's `allowed_skills`. See [Skills §9](../agents/skills.md#9-designating-a-skill-from-the-input-bar).

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

### 1.3.5. Workspace file download (v0.8.1)

[app/ui/components/workspace_download.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/workspace_download.py),
with the file logic in `app/workspace_files.py`. Uploads went in; nothing came out — a user on another PC
had no way to take the files a debate produced.

**Where.** The same `WorkspaceDownloadDialog(workspace_root).open` is attached in three places: a
`작업 공간 파일 다운로드` button directly under the workspace input in the roster (enabled only when the
*applied* workspace folder exists, refreshed with the workspace hint), a `작업 공간 파일` button in the
header of every Markdown artifact tab, and a button at the end of the report body. All of them use the
conversation's applied workspace, resolved exactly as the engine does.

**Listing.** The cached index is invalidated first so files an agent just wrote appear. Files are shown
newest first (`WorkspaceEntry.mtime` was added to the scan), filtered on the server by words that must all
appear in the path, in a paginated `ui.table` with multiple selection. Every column header sorts: size and modified time sort
on the numeric `size`/`mtime` fields (the labels are only shown through `:format`, so `9 KB` never lands
after `10 MB`), and their first click is largest/newest first. "Select visible" adds the current
filter's rows; selecting one row offers `파일 받기`, several offer `zip 으로 받기 (n개)`.

**Packing.** `plan_download()` re-resolves every submitted path with `safe_workspace_path()` — the browser's
list is not trusted — expands folders by the same listing rules, drops duplicates, and records what it
rejected. `build_workspace_zip()` runs in `run.io_bound`, refuses more than 5,000 files or 1 GB
uncompressed, writes with `ZIP_DEFLATED`/zip64 using workspace-relative names (non-ASCII names get the
UTF-8 flag), skips files that vanish mid-write, deletes the archive if packing fails, and stores it in
`data/downloads/`, purging archives older than an hour.

**Serving — a stale-content bug found in the browser.** `ui.download.file()` registers a route derived
from a hash of the *file path* with `Cache-Control: public, max-age=3600`. Fetching the same URL again
returned 200 from the browser cache, so re-downloading a file an agent had since rewritten could return the
old content. `serve_once()` registers `/_mado/download/<uuid><ext>` with `single_use=True` and
`max_cache_age=0`, then calls `ui.download.from_url`. Re-verified: the used URL returns 404, a second
download gets a different URL, and after the file changed it returned the new content.

**Verified in a browser** (anchor clicks intercepted and the URLs fetched): the report-tab and report-end
buttons open the dialog; rows came newest first; one PDF downloaded as 2,057 bytes starting `%PDF`; three
files downloaded as a zip starting `PK` whose entries were `uploads/요구사항 정의서.pdf`, `src/cache.py`,
`docs/설계서.md` with the right contents. In a roster harness the button sat below the input and above the
hint, was enabled for an existing folder, and opened the dialog. Real clicks and the browser's save
dialog were not exercised (hidden preview window).

**Protection.** Downloads sit behind the remote access token (§1.3.6); remote users must be logged in.

### 1.3.6. Remote access token (v0.8.2)

[app/security.py](file:///d:/MultiAgentDebateOrchestration/app/security.py) and
[app/ui/components/access_token.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/access_token.py).
Until now MADO had no authentication at all: bound to `0.0.0.0`, anyone on the network could read every
conversation (`/api/sessions/{id}/personas`, `/personas/{id}`), see MCP commands (`/api/mcp`), add an MCP
server with an arbitrary command, run code through the sandbox MCP, and download workspace files.

**Policy (the owner's decisions).** Loopback (`127.0.0.1`, `::1`, IPv4-mapped loopback) needs no token.
Remote clients must log in with the owner token and stay logged in for 7 days. The token is
`MADO_ACCESS_TOKEN` in `.env`, exactly 24 `[A-Za-z0-9]` characters; only length and alphabet are checked,
not strength. No HTTPS for now. Without a valid token every remote request is refused (fail-closed).

**One gate.** `AccessMiddleware` is a pure ASGI middleware added to the FastAPI `server` *after*
`ui.run_with` — Starlette makes the last-added middleware the outermost — so it wraps NiceGUI pages,
NiceGUI's socket.io mount (`/_nicegui_ws/`), `/api/*`, `/agent-icon` and `/_mado/download/*`. A test asserts
`server.user_middleware[0].cls is AccessMiddleware`. For every HTTP and websocket scope:

1. An `Origin` header that does not match `Host` → 403 / websocket close 1008, **including loopback**.
   socket.io is configured with `cors_allowed_origins='*'`, so without this a page opened in the server
   PC's browser could drive MADO with loopback rights.
2. Loopback with a `Host` other than `localhost`/`127.0.0.1`/`::1` (+ `MADO_ALLOWED_HOSTS`) → 403: DNS
   rebinding. Otherwise loopback passes.
3. Remote `/login`: GET renders a static form (no NiceGUI, no websocket); POST reads at most 4 KB,
   compares with `hmac.compare_digest`, and on success sets
   `mado_session=v1.<issued>.<HMAC>; Max-Age=604800; HttpOnly; SameSite=Strict`, then redirects to a
   `next` path restricted to this server (`//evil`, absolute URLs and backslashes fall back to `/`). The
   HMAC key is derived from the token, so changing the token invalidates every cookie. Five failures from
   one IP within 15 minutes lock it for 15 minutes (429), even for the right token. The token never
   appears in a URL, cookie or response.
4. Remote without a valid token configured → 403 page explaining how to enable it.
5. Remote with a valid, unexpired cookie passes; otherwise HTML GETs are redirected to
   `/login?next=…` and everything else gets 401 (websocket close 1008). `/logout` clears the cookie.

`X-Forwarded-For` is ignored on purpose; behind a reverse proxy every client would look like loopback.

**Rotation (loopback only).** `build_access_buttons()` puts a key button right of the info button on
loopback pages and a logout button on remote pages. Hiding is convenience: both handlers re-check
`ui.context.client.ip` on every click. The dialog offers (1) *apply the `.env` token*: re-reads the file
itself with `dotenv_values` (`load_dotenv` only ran at startup, so `os.environ` is stale); a missing or
malformed value is **not applied** and the current token stays, so one mis-click cannot lock the owner
out. (2) *generate and store*: `secrets`-based 24 characters, written to `.env` **first** and applied only
if the write succeeded (otherwise memory and file would diverge and the next restart would silently
switch tokens), shown once in the dialog, never logged. `write_env_token()` replaces only the token line
(duplicates collapsed, `export` prefix handled), keeps other lines, comments and CRLF, splits on `\n`
only (stray `\r` from a bad editor would otherwise become blank lines), and writes via a temp file +
`os.replace`. After either option `disconnect_remote_clients()` tells open remote pages to reload (→
login) and disconnects their sockets, because cookies are only checked when a connection is made.

**Lifting a login lockout (loopback only).** Below the two options the key dialog lists the IPs locked by
failed logins with the minutes left (`AccessControl.locked_ips()`, longest first, expired entries dropped),
each with `해제` and, for several, `모두 해제`. `AccessControl.unlock(ip)` removes the lock **and** the failure
window, so one more typo does not relock at once; the handler re-checks that the caller is loopback and logs
the IPs released. Verified in a browser: five wrong tokens from the LAN address gave 401×5 and then 429 for
the right token; the loopback dialog listed `192.168.45.104 · 15분 남음`, `해제` emptied the list with a
notice, and the remote browser then logged in (API 200).

**Login audit log.** Locks lived only in memory, so a restart or an unlock erased the trace of an attack.
`AuditLog` appends one JSON object per line to `data/security/login_audit.jsonl` (app-owned, outside git and
the offline bundle): `lockout` with `ip`, `at`/`ts`, `failures`, `first_failure_ts`, `last_failure_ts`,
`locked_until_ts`, `lockout_seconds` and the `user_agent` of the request that tripped the lock (truncated to
300 characters); and `unlock` with `ip`, `by: "loopback"` and `remaining_seconds`. Failures below the threshold
are not logged, and **the submitted token is never written**. Records are JSON-encoded, so a User-Agent
containing a newline cannot forge a second record. The file rotates to `.1` above 5 MB; a write failure only
logs a warning and never blocks login handling. The key dialog shows the file path and the last 20 records
(`AuditLog.read`, newest first, broken lines skipped). The audit sink is attached by `get_access_control()`;
an `AccessControl` built without one (tests) records nothing. Verified in a browser: five wrong tokens from
the LAN address produced a `lockout` line with the browser's User-Agent, `해제` in the loopback dialog produced
an `unlock` line (887.9 s remaining), and the dialog history listed both.

**Backward compatibility: a public bind with no token.** A server that was already bound to `0.0.0.0`
before tokens existed has none after the update, so every remote user would be locked out. On the first
loopback page load, `bootstrap_missing_token()` — when `bind_is_public(app.host)` (`0.0.0.0`, `::`, empty
or a non-loopback address), no usable token is applied, `.env` has the key missing or empty, and no OS
environment variable sets it — generates a token, writes it to `.env` (then applies it, same order as the
rotate button), and `show_bootstrap_popup()` shows exactly: "외부 유저 인증 토큰이 없어 새 토큰(`<token>`)으로
서버를 시작했습니다. `.env`에 저장하였습니다." A malformed value the owner wrote is never overwritten, a
lock makes concurrent first pages create one token, and a failed write leaves remote access closed with a
persistent notice. Until that first loopback page, remote requests get the "remote access is off" page,
which now says so. `app.host` is read at page time; with `--host` plus auto-reload the child process sees
conf/`.env` values, not the CLI flag. Verified in a browser against a harness bound to `0.0.0.0` with a
`.env` holding only another key: the remote tab first got the 403 page; opening `127.0.0.1` showed the
popup with the token, `.env` became `LLM_API_KEY=keep-me\r\nMADO_ACCESS_TOKEN=<token>\r\n`; reloading showed
no popup; the remote browser then logged in with that token (page and API 200). 18 unit tests cover the
conditions (public/loopback binds, missing/empty/malformed values, OS variable, 8 concurrent threads,
failed save).

**Verified.** 45 unit tests drive the middleware through `httpx.ASGITransport` with real client addresses
and a fake clock (loopback/remote, rebinding, cross-origin HTTP and websocket, fail-closed, redirects, cookie
flags, open-redirect, lockout and unlock, tampered/expired/rotated cookies, oversized body, `.env` writing).
In a browser, against a harness assembled like `main.py` and bound to the LAN address so the browser was a
genuine remote client: unauthenticated `/` redirected to the login form and `/api/ping` and the socket.io
endpoint returned 401; a wrong token gave 401 with a message; the right token set a cookie invisible to
JavaScript, after which the NiceGUI page worked over socket.io (button clicks round-tripped) and showed a
logout button but no key button. From a loopback tab on the same server the key button generated a new token,
`.env` kept the other key, the notice reported the remote screens sent back, and afterwards the remote
browser's old cookie landed on the login page with 401 from the API, the old token was refused and the new
one accepted. Two defects found this way were fixed: `write_env_token` read the file in text mode, turning
CRLF into LF, and the "screens sent back" count included clients without a connection.

**Limits.** No HTTPS: the token and cookie cross the network in clear text at login (use an SSH tunnel for
encryption). One token is one owner; there are no per-user accounts or per-session ownership. An already open
websocket is not re-checked when a cookie reaches its 7-day expiry; the next reconnect or page load is.

### 1.3.7. Math (v1.1.1)

Models write LaTeX — `$O(n \times m)$`, `$A \leftarrow B$`, `$$\frac{1}{n}\sum t_i$$`, `\(p \cdot q\)`. NiceGUI's
`ui.markdown` does not know it, so it showed the source, and Markdown made it worse: `\(` became `(`, and the
`_` of `x_i … y_j` turned into italics. [`MathMarkdown`](file:///d:/MultiAgentDebateOrchestration/app/ui/math_markdown.py)
replaces `ui.markdown` in speech cards (streaming and final), the artifact viewer (final conclusion), the decision
ledger and the trial result boxes.

**How.** Before Markdown runs, each formula is cut out and replaced by a placeholder; after NiceGUI's own
conversion (`prepare_content`) the placeholder becomes MathML from `latex2mathml` (pure Python, one wheel in the
air-gapped bundle). Browsers draw MathML without scripts (Chrome/Edge 109+, Firefox), and NiceGUI's DOMPurify keeps
it. The element's `content` stays the raw text, so copy buttons and exports keep the LaTeX.

| Written | Treated as |
| :--- | :--- |
| `$$…$$`, `\[…\]` | block formula |
| `\(…\)`, `$…$` | inline formula |
| `$…$` that is not math-like (`$5 에서 $10`, `$100/$200`, `$HOME/$PATH`), `\[1\]` | text — pandoc rules: no space after the opening `$` or before the closing one, no digit right after it, and the content has a command, `^`, `_`, an operator or a bracket, or is one variable (`$n$`) |
| fenced code, inline code | untouched |
| `A \rightarrow B`, `2 \times 3` without `$` | Unicode (`→`, `×`; about 140 commands); `C:\to\file`, `work\times` are left alone |

**Rendering details.** Wide block formulas scroll inside the card. The font is Cambria Math first (sharper rules
and symbols on Windows). Two browser gaps are closed: `\lVert … \rVert` came out as bare `<mo>‖</mo>`, which
browsers stretch to the tallest thing on the line (a `√`), so it gets `stretchy="false"` like LaTeX; and browsers
ignore MathML's `columnalign`/`columnspacing`, so `cases` read as `1x ≥ 0` — CSS adds cell padding and maps
`columnalign` to `text-align`.

**When it cannot convert.** An unsupported formula, or a PC without `latex2mathml`, shows the formula as Unicode
text instead (`\frac{n(n+1)}{2} \le x^2` → `(n(n+1))/(2) ≤ x²`). `latex2mathml` copies `\text{…}` verbatim, so
`<`, `>` outside MathML tags are escaped and link/event attributes are stripped on the server, in addition to the
browser's DOMPurify. Formulas are cached by source, so a streaming card does not convert the same formula again
on every redraw.

### 1.4. Artifact Viewer ([app/ui/components/artifact_viewer.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/artifact_viewer.py))
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

### 1.5. Persona Editor Page ([app/ui/personas_page.py](file:///d:/MultiAgentDebateOrchestration/app/ui/personas_page.py))
- Dedicated page accessible at `/personas/{session_id}`.
- Renders cards for each registered agent with editable fields:
  - **Display Name**
  - **Role Title**
  - **System Instructions**
- **Draft & Reset Controls**: Allows saving drafts to `session_agents` or resetting to `conf.json` defaults.
- **Lock Banner**: If the session has already begun (`personas_locked = true`), inputs are disabled, displaying a read-only warning badge.
- **In-Progress Banner**: If a debate is running for this session, a banner says so and states that
  opening this page does not interrupt it. Navigating here used to kill the running turn.

### 1.6. Graph Editor Page ([app/ui/graph_page.py](file:///d:/MultiAgentDebateOrchestration/app/ui/graph_page.py))

- Page at `/graphs/{graph_id}` editing `data/graphs/<id>.json` for the graph debate strategy
  ([debate-strategies.md §2.5](../orchestration/debate-strategies.md)).
- **Layout**: header (back, file path, graph name, `저장 안 됨` badge, 검증, 다른 이름으로 저장, 저장) ·
  left palette (flow nodes, one button per enabled specialist with its colour, 카드 순서로 다시 채우기,
  화면 맞춤) · canvas · right inspector with the validation report underneath.
- **Canvas** ([graph_canvas.js](file:///d:/MultiAgentDebateOrchestration/app/ui/components/graph_canvas.js),
  [graph_canvas.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/graph_canvas.py)): Vue Flow from
  the bundled `app/ui/static/graph_editor/` module, loaded with `ui.element(component=..., esm=...)`.
  Nodes show a type band (the agent's badge colour for agent nodes), badges for visit cap and "모두
  기다림", a title and a subtitle. Wire colour is the carry (전문 indigo · 요지 teal · 참조 amber); a gate's
  `no` wire is dashed. The selected wire blinks yellow (steady yellow under `prefers-reduced-motion`);
  Vue Flow's default theme paints it `#555`, invisible on the dark canvas, so the rule is written more
  specifically than the theme's `.vue-flow__edge.selected .vue-flow__edge-path`. Invalid connections are refused while dragging: self-loops, into `start`, and
  duplicates. Delete/Backspace removes the selection.
- **The browser holds the edit state.** The server hears only `select` (to fill the inspector), one
  `dirty` event when a clean graph first changes, and reads `getGraph()` on 검증 or 저장. Inspector edits
  go back with `updateNode` / `updateEdge`. Sending every drag event would flood the websocket
  ([roster-editing.md §5.2](../agents/roster-editing.md)). `beforeunload` asks before leaving unsaved
  changes.
- **Conversion** is pure Python and tested without a browser: `spec_to_canvas` places nodes without
  `pos` by distance from `start`, and `canvas_to_spec` keeps only the fields each node type uses.
- **Save** always writes, even with validation errors, so a half-built graph is not lost; the
  notification carries the report and the engine refuses the graph at turn start until it is fixed.
  The report uses a visit cap of 3 for nodes without one, since the page has no session.
- **Lessons**:
  - Fit view is re-run by a `ResizeObserver` until the user pans or zooms. The first render happens
    before the canvas has a size, and the view fitted to 0 × 0 left most nodes off-screen.
  - Vue Flow re-checks **every existing wire** with `is-valid-connection` whenever the edge list is
    replaced, and drops those that fail. The duplicate check must ignore the wire's own `id`; without
    that, connecting one new wire erased all the others.
