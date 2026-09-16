# Conversation Memory: Pinned User Record, Decision Ledger & Summaries (v0.8.3)

[app/orchestration/context_memory.py](file:///d:/MultiAgentOrchestrator/app/orchestration/context_memory.py)
holds the pure functions; [`OrchestratorEngine`](file:///d:/MultiAgentOrchestrator/app/orchestration/engine.py)
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

**This turn's plan** is pinned the same way (`build_plan_pin`, `PLAN_PIN_SHARE = 0.15`, skipped rather
than clipped when too large), with a reference left in its transcript position.

### 2.2. Decision ledger

The orchestrator rewrites a structured ledger with five fixed sections — `요구사항·제약`, `결정 사항`,
`기각된 대안`, `미해결 쟁점`, `담당·다음 할 일`.

- **Where it goes**: a `[Session Decision Ledger]` block at the **start of the last user message** —
  directly before "this is your turn" — added in one place, `LLMCaller.call_agent` →
  `place_ledger_last()`. If the last message is not a user message the block becomes its own user
  message. `fit_context_window` always keeps the last message, so the ledger is not trimmed before a
  speech; a very long tool loop that has to trim its own blocks can drop it. It is kept out of the
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
- **Call**: a copy of the orchestrator with tools and sequential thinking off (`_tool_less`, now shared
  with speaker selection and dispatch).
- **Parsing** (`parse_ledger`): reasoning trace and fences stripped, preamble before the first `## `
  dropped. No `## ` heading at all means it is not a ledger → **the previous ledger is kept** and a
  `ledger_update_failed` event is sent. The debate never stops over the ledger.
- **Size**: `min(LEDGER_MAX_CHARS = 5000, 10 % of the smallest budget among this turn's agents)`
  (`memory_cap`, `state.memory_budget`), because it rides in every agent's system prompt.
- **Order of events after synthesis**: `artifacts_synthesized` is sent as soon as the artifacts are
  saved, *before* the ledger update — otherwise the artifact tabs would lag the report by one LLM call.

### 2.3. Summarize instead of drop

Before building a speaker's context (`_context_for_speech`) and before synthesis
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
speaker sees three tiers (`_build_context_for_agent` → `body_for`):

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

### 2.5. Prompt caching

OpenAI, Gemini, vLLM and similar serve a request cheaper and faster when its **prefix** matches an
earlier one. What changes often therefore goes last:

- The system prompt (persona, file-writing rule, custom instructions) is constant within a session.
- `[Debate Progress]: Round n of N` moved from the goal message to the start of the last message. In the
  goal message it changed every round and pushed the entire transcript out of the cache.
- The decision ledger sits just before the instruction (§2.2).
- The goal message (request, user record, plan, summary) changes only on a new turn, an interjection
  or a fold. The transcript is appended to; the only rewrite is a new speech turning old (full → digest)
  for this agent, which leaves everything before it cached.

Anthropic needs explicit `cache_control` markers, which MADO does not send yet.

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

## 4. UI

The roster shows a read-only **결정 장부** expansion directly under the custom instructions box, with
the character count as its caption (`비어 있음` when empty). It updates live on `ledger_updated`; a page
attached to a running turn takes it from the runner snapshot (`decision_ledger`), since the database
only has it after the turn ends. Wrong entries are corrected by saying so in the chat — the next update
applies it, and the user record outranks the ledger in every prompt.

Events: `ledger_update_started`, `ledger_updated`, `ledger_update_failed`, `context_summarizing`,
`context_summarized`, `context_summary_failed` — status text in the runner, toasts on the page.

## 5. Tests

[tests/test_context_memory.py](file:///d:/MultiAgentOrchestrator/tests/test_context_memory.py). The
reproduction test passes the prompts the engine sent through the real `fit_context_window` and checks
the constraint in what would reach the endpoint; disabling the pin makes it fail. Other tests pin the
tiers message by message, digest parsing, code references, the ledger's position in what `call_agent`
actually sends, and that everything but the last message is identical between rounds.

`tests/conftest.py` now creates the database engine on `:memory:` before every test if none exists.
The engine is a process-wide singleton fixed by whoever creates it first, and `OrchestratorEngine()`
defaults to `./multiagent.db` — a test that built the engine before `init_db(":memory:")`, or that ran
after a test reset the singleton, wrote sessions into the developer's real database.
