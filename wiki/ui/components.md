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

### 1.4. Artifact Viewer ([app/ui/components/artifact_viewer.py](file:///d:/MultiAgentOrchestrator/app/ui/components/artifact_viewer.py))
- **Tabbed Interface**:
  - **Comprehensive Report Tab**: Markdown rendering of the final synthesis report.
  - **Source Code Tab**: Language-highlighted code viewer for extracted scripts.
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
