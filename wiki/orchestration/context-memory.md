# Conversation Memory: Pinned User Record, Decision Ledger & Summaries (v0.8.3)

[app/orchestration/context_memory.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/context_memory.py)
holds the pure functions; [`OrchestratorEngine`](file:///d:/MultiAgentDebateOrchestration/app/orchestration/engine.py)
makes the LLM calls (section `대화 기억`).

---

## 1. The problem

Every speaker's context carried **every message of the session, verbatim**, previous turns included
(they are reloaded from the database at the start of a turn). When that exceeded the window,
[`fit_context_window()`](../agents/llm-integration.md) dropped whole messages **oldest first** — the
only criterion was age. The first things to go were exactly the things said once and never repeated:

| Lost | Consequence |
| :--- | :--- |
| Turn-1 user feedback ("don't use Redis") | Turn 5 violates the user's instruction. Only the goal and the *current* request survived. |
| This turn's orchestrator plan | Later speakers no longer know what they were assigned. |
| Decisions, their reasons, rejected alternatives | Settled debates restart. |

Agents have different models and windows, so they **forgot different amounts** and talked past each
other. The synthesis transcript was filled newest-first, so the final report could miss early
requirements too.

A reproduction is kept as a test: an 8k window, specialist speeches of about 2,100 tokens, a
constraint given in turn 1 and a three-round turn 2. With the old behaviour the constraint reached the
endpoint in **none** of the nine turn-2 speeches (3–14 messages dropped each); with this change it
reaches all nine, exactly once, and nothing is dropped.

## 2. Principle: forget the conversation, keep the state

Three layers, cheapest and most authoritative first. Human words outrank anything an LLM derived,
and the prompts say so.

### 2.1. Pinned user record

`build_user_record()` collects every user message of the session (the current turn's opening request
excluded — it is already the goal header) and places it in the **goal message**, the first message
after the system prompt. Both trims — `fit_context_window` and `fit_tool_loop_context` — keep
`messages[:2]`, so the record cannot be dropped. Where those messages sit in the transcript they become
a reference, `(사용자 발언 #n — 위 [사용자 발언 기록]에 전문이 있습니다)`, so chronology is still
readable and the text is never paid for twice.

- Labels tell turns and interjections apart: `이전 턴 요청`, `이번 턴 토론 중 개입`.
- **Share cap** `USER_RECORD_SHARE = 0.25` of the speaker's budget. A pasted spec must not push the
  head out of the window — nothing can trim the head, so that would be a 400. Over the cap, the newest
  messages are pinned first; a message that does not fit is skipped (older short ones are still
  pinned) and simply stays verbatim in the transcript, trimmable as before. The record says how many
  were left out.
- The plan prompt of later turns carries the record in full; it used to get 250-character snippets of
  the last six messages. Speaker selection and parallel dispatch get it with a smaller share
  (`ROUTING_RECORD_SHARE = 0.1`).
- The synthesis prompt pins it in front of the transcript and subtracts its size from the transcript
  budget.

- A person's direct edit of the ledger (§4) is pinned here too, labelled `결정 장부 직접 수정`. It is a `system`
  note rather than a user message, but it carries the same authority.

**This turn's plan** is pinned the same way (`build_plan_pin`, `PLAN_PIN_SHARE = 0.15`, skipped rather
than clipped when too large), with a reference left in its transcript position.

### 2.2. Decision ledger

The orchestrator rewrites a structured ledger with five fixed sections — `요구사항·제약`, `결정 사항`,
`기각된 대안`, `미해결 쟁점`, `담당·다음 할 일`.

- **Where it goes**: a `[Session Decision Ledger]` block at the **start of the last user message** —
  directly before "this is your turn" — added in one place, `LLMCaller.call_agent` →
  `place_ledger_last()`. If the last message is not a user message the block becomes its own user
  message. `fit_context_window` always keeps the last message, so the ledger is not trimmed before a
  speech. Inside a long tool loop that message is pushed back behind the tool blocks and can be
  dropped — together with the turn instruction (strategy guidance, the parallel task, the digest
  request); when that happens it is restored (§2.2.1). It is kept out of the
  system prompt, which holds persona + custom instructions exactly as before, so the system prompt
  never changes when the ledger does (see §2.5). A first version put the ledger right after the custom
  instructions in the system prompt; every ledger update then invalidated the provider's prompt cache
  for the whole request.
- **When**: after each round except the last, and after synthesis. The last round is skipped because
  synthesis reads the transcript directly and the post-synthesis update folds that round anyway; a
  round that ends with a stop request is skipped for the same reason. Parallel rounds update after the
  merge. So a turn costs `max_rounds` extra orchestrator calls (one with a single round).
- **Input**: previous ledger + pinned user record + this turn's request + only the messages since the
  last update (`ledger_through`), newest first if they do not all fit. Pinned user messages appear as
  references.
- **Call**: a copy of the orchestrator with tools and sequential thinking off (`ConversationMemory.tool_less`, now shared
  with speaker selection and dispatch).
- **Parsing** (`parse_ledger`): reasoning trace and fences stripped, preamble before the first `## `
  dropped. No `## ` heading at all means it is not a ledger → **the previous ledger is kept** and a
  `ledger_update_failed` event is sent. The debate never stops over the ledger.
- **Size**: `min(LEDGER_MAX_CHARS = 5000, 10 % of the smallest budget among this turn's agents)`
  (`memory_cap`, `state.memory_budget`), because it rides in every call of every agent.
- **Order of events after synthesis**: `artifacts_synthesized` is sent as soon as the artifacts are
  saved, *before* the ledger update — otherwise the artifact tabs would lag the report by one LLM call.

#### 2.2.1. Keeping the ledger and the turn instruction through a long tool loop

The last user message at the start of a speech — ledger + turn instruction — is remembered by
`call_agent` as the *turn anchor* and passed to `fit_tool_loop_context(keep=)`. That trim keeps the head
(system + goal) and the latest tool block and drops older blocks. If a dropped block held the anchor,
the anchor is appended verbatim to the elision notice under
`[생략된 기록에 있던 이번 차례 지시와 결정 장부를 다시 붙입니다. 여전히 유효합니다]`.

- The notice is merged into the goal message, which no later trim drops, so one restoration lasts for
  the rest of the speech. A later trim recognizes it by that header and does not add a second copy.
- Its size is reserved first: blocks keep being dropped until the anchor fits. When only the latest
  block is left and there is still not enough room, the anchor is clipped in the middle (the ledger's
  start and the instruction's end survive); with fewer than 48 tokens of room it is not restored at all
  and the log says so — an overflowing request would be a 400 and lose the whole speech.
- Nothing changes when the anchor survives. On a first speech the anchor is merged into the goal
  message anyway (no earlier `assistant` message separates them), and a request that is not trimmed
  keeps its shape — trimming already breaks the prompt cache, so restoring costs nothing extra there.
- Before this, the same loss applied to the turn instruction regardless of the ledger.

#### 2.2.2. Fixed issue numbers and the revision history (ADR-051)

The model rewrites the whole ledger every time, so an open issue used to have no identity: the same issue came
back in other words each round, and the sub-debate and the stagnation observer found issues by list position or
text. Items under `미해결 쟁점` now carry a number, `- [I-3] 내용`, and every ledger change is saved as a
revision. Code: [`ledger_history.py`](file:///d:/MultiAgentDebateOrchestration/app/orchestration/ledger_history.py).
The decision is [ADR-051](../../lectures/05-adr/ADR-051-fixed-issue-ids-and-ledger-revisions.md).

- **The server numbers issues** (`assign_issue_ids`). The prompt only asks the model to keep earlier `[I-n]` tags and
  to write new issues without one. For each open item, in order:
  1. a tag that is at most the highest number used so far and not yet used in this ledger stays;
  2. otherwise, text that matches a known issue after normalizing (tags, parenthesized notes, punctuation and spacing
     removed, the first 80 characters; `normalize_issue`, moved here from `diagnostic_loop`) gets that issue's
     number, **closed issues included**;
  3. otherwise the issue gets the next number.
  Tags in other sections are removed. `- 없음`, the per-section clip note
  `- …(장부가 길어 이 칸의 항목 N건을 생략했습니다)` (`ledger_clip_note`), and the legacy clip note
  `- …(장부가 길어 뒷부분을 생략했습니다)` are not issues; the clip note used to be counted by
  `open_issue_count`, which also affected convergence.
- **Fitting to the character cap without losing open issues (`fit_ledger`).** A plain character slice at the end of the
  ledger (`clip_chars`) cut off `미해결 쟁점` first because that section sits last. `parse_ledger` now trims by section
  in six steps (`LEDGER_TRIM_STEPS`) until the ledger fits `max_chars`: (1) shorten items in `결정 사항`, `수립된 가정`
  and `해결된 쟁점` to 120 chars; (2) drop the oldest items in `해결된 쟁점` (leaving `- …(장부가 길어 이 칸의 항목 N건을 생략했습니다)`);
  (3) shorten `결정 사항` and `수립된 가정` items to 80 chars; (4) drop the oldest items in `수립된 가정`, then `결정 사항`;
  (5–6) shorten open-issue descriptions to 120 chars and then 80 chars while keeping every `- [I-n]` item in place.
  Open issues are never dropped; only if the open issues alone still exceed the cap after 80-char shortening does a
  final `clip_chars` guard run.
- **Closed and reopened.** An issue that disappears from the list is closed. Matching runs against the whole
  conversation (`IssueIndex`), so an issue reopened with the same text gets its old number. The ledger prompt also
  lists up to five recently closed issues, each clipped to 80 characters, under `[최근에 닫힌 쟁점]`, and asks the
  model to reuse one of those numbers only if that issue was raised again; that is the only way a reworded reopened
  issue keeps its number. The list's tokens come out of the update's room.
- **Numbers are never reused.** The next number is one past the largest of the revisions' `issue_high`, the tags in
  the current ledger text and the in-memory index. Reading the ledger text covers a ledger carried over by
  `continue_session` and a revision whose save failed. A discarded turn's revisions are deleted with it, so its
  numbers may come back; the ledger is only saved at the end of a turn, so no saved text points at them.
- **When the index is built.** The first ledger update of a turn reads the conversation's revisions once into
  `state.issue_index`; later updates in the turn work in memory. A resumed turn builds its state again and so reads
  its own earlier revisions too, which keeps `I-5` as `I-5` even though the session column still holds the previous
  turn's ledger.
- **Revisions** (`ledger_revisions`, [Database Schema §2.6.1](../architecture/database-schema.md#261-ledger_revisions-table-ledgerrevisionmodel)).
  `update_ledger(db=, kind=)` saves a row right after each successful update, and the six callers name the kind:
  `round` (end of a round), `step` (graph step), `subdebate`, `final` (after synthesis), `resume` and `retry`
  (`_correct_ledger`, recorded with the retried turn's id). A direct edit records `edit`. The row holds the full
  ledger, the open issues as JSON, the counts of decisions and rejected alternatives, `issue_high`, and a per-session
  `seq` used for ordering (`created_at` can repeat: the clock on Windows ticks about every 15 ms).
- **Saving never stops the debate.** Like other writes here, the insert is committed at once and rolled back on
  failure, followed by `persist_failed` ("결정 장부 개정 이력"). Every engine write commits where it happens, so at the
  six call sites nothing else is pending and the rollback drops only that row; a test asserts the session is clean
  on every call. SAVEPOINTs are not used: SQLite drivers need an engine-wide workaround for them. All values are set in
  Python and the row is not read back, so no expired object is touched after a rollback.
- **Event.** `ledger_updated` now carries `revision: {id, turn_id, round, kind, reason, open, opened, closed, decided,
  rejected, created_at, seq, message_id}`. `opened` and `closed` compare with the ledger before the update.
- **Per-round deltas** (`round_deltas`). For each (turn, round) the last revision is compared with the revision just
  before that round: decisions `+n`, issues closed, issues opened. `retry` and `edit` revisions form no round of their
  own but are the baseline for the next one. When a round has two revisions (round end and sub-debate, or early
  convergence and synthesis), the last one counts.
- **Consumers.** The sub-debate prompt lists the tagged issues and asks for `"issue_id": "I-3"`; `parse_sub_debate`
  accepts it (or a tag in front of `issue`) only for an issue that is open now and fills a missing `issue` from the
  ledger. `evaluate_sub_debate` strips the tag from the item it picks and returns the number separately. The opening
  note stores `issue_no`, and the conclusion header reads `[서브 토론 결론 · I-3 · …]`. The stagnation observer signs
  a repeated patch with `issue:I-3` when the issue has a number, so rewording no longer breaks the match. Convergence
  and consensus only count items and did not change. The trial page shows the ledger as before, tags included.

### 2.3. Summarize instead of drop

Before building a speaker's context (`ConversationMemory.context_for_speech`) and before synthesis
(`_ensure_synthesis_room`), the engine estimates the request. If it exceeds the budget,
`choose_fold_cut()` picks how many of the oldest unsummarized messages to fold so the request drops
to `SUMMARY_TARGET_FILL = 0.6` of the budget, with room for the summary to grow. The orchestrator
(tool-less copy) folds them into a **rolling summary** in batches (at most `SUMMARY_MAX_BATCHES = 3`
calls; progress is kept per batch). The summary is pinned in the goal message as `[앞선 논의 요약]`,
and the transcript starts where the summary ends.

- **Per agent**: the summary is used only when the full transcript does not fit *that* agent. Agents
  with large windows keep reading the original.
- `SUMMARY_KEEP_RECENT = 1`: the latest message is never folded.
- A fold that would free less than `min(needed, 10 % of budget)` is skipped. Without it a large head
  made every speech fold one message — one extra call per speech.
- Size: `min(SUMMARY_MAX_CHARS = 6000, 20 % of the smallest budget)`.
- If summarizing fails, `context_summary_failed` is sent and `fit_context_window` drops the oldest
  messages as before. Dropping remains the last line of defence.

Calibration came from the reproduction: with the first constants (fill 0.8, keep 3 recent, fixed
6,000/5,000-character caps) the 8k scenario called the summarizer on **every** speech and still
dropped messages — three recent speeches alone exceeded the target, and a fixed-size summary took most
of the window. With the current values it folds on roughly every other speech (inherent when one
speech is a third of the window) and nothing is dropped. At 128k windows folds are rare.

### 2.4. How much of each speech is passed on

Every speech used to reach every later speaker verbatim, code included, round after round. Now each
speaker sees three tiers (`ConversationMemory.build_context_for_agent` → `body_for`):

| Tier | Which messages | What is sent |
| :--- | :--- | :--- |
| **New** | after this agent's own latest speech (at most back to the start of this turn) | the full body, code included — the agent has not answered it yet, and a critic has to read fresh code to review it |
| **Old and long** | other agents' speeches over 700 characters, not `@`-mentioning this agent | only the speaker's `## 요지` section, headed `## 요지 (전문 N자 중 요지만 싣습니다)` |
| **Everything else** | short speeches, speeches that mention `@this agent`, the agent's own speeches, old speeches without a digest | the body with long code blocks replaced by references |

**Digests** cost no extra call: every turn instruction ends with `DIGEST_INSTRUCTION`, asking for a
3–5 line `## 요지` (conclusion, grounds, requests to others, files written) and saying that later rounds
may see only that. `extract_digest()` takes the last `## 요지` / `### 요지` / `**요지**` section up to
the next level-1/2 heading, after the reasoning trace is stripped, clipped at 800 characters. A speech
without one is not summarized by an LLM; it falls back to the code-referenced body, so nothing is lost
and nothing is invented. Short routing snippets (plan history, speaker selection, parallel dispatch)
also prefer the digest to the first 250–300 characters, which for a long speech were only its preamble.

**Code references** (`reference_code_blocks`): a fenced block of 15+ lines or 800+ characters keeps its
fences but its contents become one line — line count, language, first line, and likely files (a
`` `path` `` in the three lines above the fence, the fence's `title=`, and paths this speech wrote with a
file tool). Mermaid is never replaced: a diagram is debate content. An unclosed fence (an answer cut off
at `max_tokens`) is handled. The original stays in the database, the chat, the artifact tab
(`_debate_code_artifacts` reads raw content) and usually the workspace file. The ledger and summary
inputs use the same references; the synthesis transcript stays verbatim.

Measured by a test (three specialists, three rounds, each speech ≈ 1,100 tokens of prose + a 31-line
code block + a digest): the critic's last prompt is about **54 %** of what sending the eight earlier
speeches verbatim would cost. Four of them arrive as digests; code is sent for the two new ones only.

#### 2.4.0. What the app saw: the `[앱 기록]` block

A speech's text is the speaker's claim. The app also knows what the speaker actually did: which tools it ran and what came
back, and which @mentioned files went into its request as content (`app/agents/media.py`). Until v2.0 only the speaker's own
request carried that. In a 2026-11-09 run with deepseek-v4-flash, Architect correctly quoted a tool result and the text inside
an attached image; Critic rejected both as hallucinations ("no raw trace", "the user message says the content was not
attached"), and Orchestrator concluded "lookup failed" / "not received".

`app/orchestration/observed.py` now hands those facts on:

- **Recorded at the end of each speech** in `llm_meta["observed"]` (`observed_meta`): up to 8 tool calls (name, arguments
  clipped to 160 characters, status, output clipped to 200 characters, a flag for outside sources) plus a count of the rest,
  and the attachments the model accepted. `media.recording_sent` collects the latter: `_complete_once` reports the
  attachment blocks of every request the model answered, filtered by agent key so a peer consultation inside the speech does
  not mix in. Files dropped for size or context share, or replaced after the model rejected them, are not recorded.
  `tools__load_tools`, `skills__load_skill` and the sequential-thinking server are left out. A speaker with MCP servers that
  ran nothing gets `"tools": []`, rendered "실행한 도구: 없음", so a claim to have read a file without a read shows up too.
  An @mentioned attachment is rendered with the notice its receiver got ("같은 파일을 도구로 다시 읽지 말라고 안내했습니다",
  `media.ATTACHMENT_HEADER`): without it, a peer called the receiver's "I did not re-read it, as instructed" an invented
  rule. A file a tool returned (`from_tool`, stored under `.mado/tool_media/`) is listed separately, without that notice.
  `llm_meta` follows a re-run or restored version together with the text (`speech_retry.version_entry`), and it is stored, so
  the next turn and a reopened conversation render the same block and keep the prompt prefix (§2.5). The raw `tool_calls`
  are loaded only for the turn being resumed.
- **Rendered for others** by `peer_view` after the body: speech context (§2.4 tiers), synthesis transcript, ledger and summary
  input (`render_message`), graph inputs and sub-debate speeches and conclusions. Not for the agent's own speeches: in the
  assistant slot the model starts imitating the block. A speech this agent has already answered gets the short form (tool
  name → status, no arguments or output), like the digest; graph inputs carried as a digest too.
- **Not forgeable from the text.** `neutralize` replaces `[앱 기록]` (also full-width brackets, spaces and zero-width
  characters) inside speech bodies and tool outputs with "(발언자가 적은 '앱 기록' 표시)". Outside-source outputs are wrapped in
  `<untrusted_content>` (ADR-047).

The planning roster gives the matching facts before anyone speaks (`format_roster`, `PhaseExecutor.roster`): tool names inside
each MCP server — `probe(lookup_code)`, at most 6 then "등 N개", "(지금 부를 수 있는 도구 없음)" when the server offers none
— and the input formats an expert receives directly, with a note that other experts do not get that content. With only the
server name, Orchestrator read `probe` as the tool name and planned that `lookup_code` did not exist. The shared reference
block of an @mention now says that experts with the format enabled get the content separately and that their speech's
`[앱 기록]` lists it (`build_reference_block`).

#### 2.4.1. Speech numbers and citations (ADR-052)

Every agent and orchestrator speech gets a number that is fixed for the conversation (`messages.speech_no`,
`DebateMessage.tag` → `S14`); user messages and notes get none. Transcripts carry it in the header,
`[System Architect (High-Level …) · S14]:` (`DebateMessage.speaker_label`), in speech context, ledger and summary input
(`render_message`), the synthesis transcript and fallback report, graph inputs, and sub-debate speeches and
conclusions. The decision is [ADR-052](../../lectures/05-adr/ADR-052-round-groups-and-citation-labels.md).

- **Assigning.** The first number a turn hands out aligns the counter once with the largest of the recorded
  `speech_no`, the numbers reserved in this turn's speech drafts and the in-memory counter
  (`turns.ensure_speech_counter`, which closes its read transaction for ADR-041); after that the counter lives in
  `state.speech_counter`. A draft stores its reserved number (`DRAFT_SPEECH_NO`), and a speech resumed from it keeps
  that number, so new numbers start above the drafts. A number reserved for a speech cut before any draft may come
  back; no prompt ever carried it. Parallel rounds reserve numbers in assignment order and graph steps in activation
  order before the speeches start. Failed speeches keep their number, a retried speech keeps its original number, and
  a fork copies the column.
- **Cache.** The number is stored on the message, so a header never changes between rounds and the prompt prefix stays
  identical (§2.5). `load_history` reads it back, so a reopened conversation's first turn builds the same headers.
- **Asking for citations.** `CITE_INSTRUCTION` asks speakers to point at another speech as `[S14]` and at a ledger issue
  as `[I-3]`, and not to write transcript headers themselves. It goes before `DIGEST_INSTRUCTION` in speech turns, and
  into graph node, sub-debate and synthesis instructions.
- **Continuing a session.** Speech numbers restart in the new conversation, so `continue_session` strips `[S…]` from
  the carried ledger (`session_ops.strip_speech_refs`); issue numbers stay.
- The Markdown export adds `· S14` to each speech header. Speeches folded into the summary may lose their number there.

### 2.5. Prompt caching

Providers serve a request cheaper and faster when its **prefix** matches an earlier one. What changes
often therefore goes last:

- The system prompt (persona, file-writing rule, custom instructions) is constant within a session.
- `[Debate Progress]: Round n of N` moved from the goal message to the start of the last message. In the
  goal message it changed every round and pushed the entire transcript out of the cache.
- The decision ledger sits just before the instruction (§2.2).
- The goal message (request, user record, plan, summary) changes only on a new turn, an interjection
  or a fold. The transcript is appended to; the only rewrite is a new speech turning old (full → digest)
  for this agent, which leaves everything before it cached.

OpenAI, Gemini and vLLM cache such a prefix on their own. Claude caches only up to an explicit
`cache_control` marker, so MADO marks the places that stay the same on the next request
([LLM Integration §7](../agents/llm-integration.md#7-claude-prompt-cache-markers-adr-045)):

| Marker | Read again by |
| :--- | :--- |
| Last tool definition | other agents with the same model and tools |
| System prompt | every call of this agent |
| End of the goal message | later rounds, even after the transcript behind it changes |
| The agent's last own speech | the next round. What came before it stays as it is; what came after is still "new" and turns into digests once this agent has spoken again (§2.4) |
| Last message of the tool loop | the next call of the same speech |

The turn instruction (ledger, round number) is not marked on the first call of a speech. If the speech
ends without a tool call, nothing would read it again. If it calls a tool, the next call's marker covers it.

A new user turn changes the goal message, so the transcript behind it is written to the cache once more.
Only the tools and the system prompt survive that.

### 2.6. Convergence: ending early (v1.5)

The ledger is also the debate's convergence signal. After each round (except the last) the engine
updates it anyway; `DebatePhaseExecutor._close_round` then compares it with the previous round's ledger
**of the same turn** (`context_memory.ledger_converged`):

* `미해결 쟁점` is empty in **both** ledgers, and
* `결정 사항` did not grow (fewer is fine — items get merged).

Decision wording is deliberately not compared: the model rewrites the ledger each time, so the same
decision is phrased differently. A ledger whose sections cannot be read never counts as converged —
ending too early costs more than one extra round. No extra LLM call is made.

When it converges, the remaining rounds are skipped: an orchestrator note (`turn_meta.kind = early_end`,
`reason = converged`), the event `debate_ended_early`, and a synthesis instruction saying the debate
**concluded** (not "was cut short"). Round 1 can never converge (no earlier ledger in the turn), so a
3-round debate can end after round 2. Parallel rounds are judged after their interim merge; graph
debates are not affected (gates decide their end). Switch: roster `합의되면 일찍 끝내기`
(`sessions.converge_early`, default on, frozen into the turn config).

The same boundary checks the **turn token budget** (`sessions.token_budget`, 0 = none, roster
`토큰 예산`): once the turn's input+output tokens ([usage](../agents/llm-integration.md#6-usage-metering-v15))
reach it, the debate ends with `reason = budget` — which, unlike convergence, counts as stopped early.

`is_consensus_reached` now means consensus: everyone spoke, nothing stopped the turn, the synthesis
exists, **and the final ledger has no open issues**. The synthesis JSON's former `consensus_reached`
(clean completion) is `completed_cleanly`. A resumed turn that had already ended early goes straight to
synthesis (`turns.early_end`).

## 3. Persistence

| Column (`sessions`) | Meaning |
| :--- | :--- |
| `decision_ledger` | The ledger text. |
| `ledger_through_id` | Last message folded into the ledger. `NULL` = nothing yet. |
| `transcript_summary` | The rolling summary. |
| `summary_through_id` | Last message the summary covers. |

- Written by `_persist_memory()` **only when a turn runs to the end**. Abort ("요청 되돌리기")
  cancels the turn and then deletes its messages (`session_ops.discard_turn`); a ledger already saved
  from those messages would keep decisions nobody made. `updated_at` is left unchanged so the sidebar
  order does not move.
- Positions are stored as message ids, not counts, and resolved against the reloaded transcript
  (`through_index`). If a turn crashed before saving, the next turn folds from the last saved point.
- A ledger anchor that no longer exists counts as "everything so far is folded" — the whole history is
  not replayed into one update. A summary whose anchor is gone is discarded (it would overlap or skip
  the originals) and rebuilt if needed.
- **Continuing a session** (`continue_session`) carries the ledger with `ledger_through_id = NULL`; the
  summary stays behind, since it covers messages the new session does not have.
- Old databases get the four columns through `_ADDED_COLUMNS`.
- The same four values are also stored on the turn (`turns.memory`) when it completes, so a fork at an
  earlier turn starts from that turn's memory ([Session Handoff §7](session-handoff.md)).
- A direct edit (§4.1) is the one write outside a turn. It changes `decision_ledger` on the session and in the
  latest turn's `memory`, and leaves `ledger_through_id` and `updated_at` alone.
- Unlike the column, the revision history (§2.2.2) is written **during** the turn, after every update. Deleting a
  conversation, discarding a turn (`session_ops.discard_turn`, by `turn_id`) and the trial server's ORM delete remove
  revisions with it; a fork copies them with the same boundary as messages, with new turn and note ids.

## 4. UI

The roster shows a **결정 장부** expansion directly under the custom instructions box, with the character
count as its caption (`비어 있음` when empty). It updates live on `ledger_updated`; a page attached to a running
turn takes it from the runner snapshot (`decision_ledger`), since the database only has it after the turn ends.

Events: `ledger_update_started`, `ledger_updated`, `ledger_update_failed`, `context_summarizing`,
`context_summarized`, `context_summary_failed` — status text in the runner, toasts on the page.

### 4.1. Editing it directly

`직접 고치기` opens one input per item, grouped by section. Text can be changed, items added or removed, and an
item under `미해결 쟁점` is closed by unchecking it. The decision is
[ADR-038](../../lectures/05-adr/ADR-038-edit-the-ledger-directly.md).

| | Direct edit | Chat correction |
| :--- | :--- | :--- |
| When | Only when no run is active in the conversation | Any time |
| Takes effect | The next speech, with no LLM call | The next ledger update |
| Authority over later updates | The pinned user record | The pinned user record |

- **Format.** [`ledger_edit.py`](file:///d:/MultiAgentDebateOrchestration/app/orchestration/ledger_edit.py) parses
  by the same rules as `ledger_items`, and rewrites the text on save. A section left without items becomes
  `- 없음`, and a fixed section the model dropped returns in its place, so convergence can read the ledger.
  Indented lines, lines outside a list and sections the model invented are kept.
- **No overlap with a turn.** A turn holds the ledger in memory and overwrites the column at its end, so the button
  is disabled during any run, and an open editor closes with a notice when a run starts or the ledger changes. The
  save itself only applies if the column still holds the text the editor opened with. On a conflict the page
  loads the newer ledger and the edit is not kept.
- **Surviving the next rewrite.** The save adds a `system` note at the end of the conversation
  (`turn_meta.kind = ledger_edit`) listing what was removed, added or closed, clipped to `NOTE_MAX_CHARS`. The note
  appears in the timeline and in the user record (§2.1). Without it, the orchestrator could restore an entry
  because it cannot tell who wrote it. A note alone does not trigger a ledger update.
- **Later paths.** The latest turn's `memory` gets the same text, so retrying a speech in the next turn or forking at
  this turn starts from the edit, with the note inside the same boundary. Earlier turns keep what was true then. An
  interrupted turn has no `memory`; resuming it reads the session column.
- **Limit.** A save may not grow the ledger past the cap a turn applies to the model (`ledger_edit.char_limit`). A
  ledger already over it can still be shortened.
- **Issue numbers.** Each open issue shows its number as a read-only chip; the text box holds the text alone
  (`LedgerItem.issue_no`). The note compares open issues by number (`고침` / `닫음` / `넣음`), and an item that only
  gained a number is no change. A new issue is numbered on save from the conversation's index, so typing a closed
  issue's text again brings its old number back. `save_edit` returns `SavedEdit(note, ledger, revision)` and records an
  `edit` revision with the note's id; the panel shows the saved ledger, numbers included.
- **Not covered.** An empty ledger has no edit button; the custom instructions take text in advance. There is no
  HTTP route, and the trial and beginner pages only read. A correction after a retried speech
  (`_correct_ledger`) sees the edit only as the previous ledger, the same as a later chat message.

## 5. Tests

[tests/test_context_memory.py](file:///d:/MultiAgentDebateOrchestration/tests/test_context_memory.py). The
reproduction test passes the prompts the engine sent through the real `fit_context_window` and checks
the constraint in what would reach the endpoint; disabling the pin makes it fail. Other tests pin the
tiers message by message, digest parsing, code references, the ledger's position in what `call_agent`
actually sends, and that everything but the last message is identical between rounds.

[tests/test_ledger_edit.py](file:///d:/MultiAgentDebateOrchestration/tests/test_ledger_edit.py) covers direct edits:
the format round trip, conflicts, the pin in later prompts, forks, retries and resumes, and the panel.

[tests/test_ledger_history.py](file:///d:/MultiAgentDebateOrchestration/tests/test_ledger_history.py) covers issue
numbers (kept, matched by text, new, duplicated, made up, reopened, the clip note), `summarize`, `round_deltas`, the
revisions a three-round turn saves and the event it sends, numbering across turns and after a resume, a failed save,
and deletion and forks. Its autouse fixture wraps `record_revision` and fails if the DB session holds anything
uncommitted when a revision is recorded.

`tests/conftest.py` now creates the database engine on `:memory:` before every test if none exists.
The engine is a process-wide singleton fixed by whoever creates it first, and `OrchestratorEngine()`
defaults to `./multiagent.db` — a test that built the engine before `init_db(":memory:")`, or that ran
after a test reset the singleton, wrote sessions into the developer's real database.
