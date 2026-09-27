# Interrupted Turns: Recording, Detection & Resume

What happens when the server goes down in the middle of a debate turn — a shutdown, a reboot, a power
cut — and how the turn is picked up again. Design records: [ADR-024](../../lectures/05-adr/ADR-024-resume-interrupted-turns.md)
(turn-level recovery) and [ADR-025](../../lectures/05-adr/ADR-025-resume-speeches-by-tool-step.md)
(continuing a speech after its last finished tool).

Code: [app/orchestration/turns.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/turns.py) (records,
detection, pure reconstruction), `OrchestratorEngine.resume_turn` in
[app/orchestration/engine.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/engine.py), and the
checkpointing tool loop in [app/agents/llm.py](file:///d:/MultiAgentDebateOrchestration/app/agents/llm.py).

---

## 1. What used to be lost

A turn was never recorded as such — the only mark of a finished turn was `turn_started_at` on its
synthesis message. When the process died mid-turn:

- the speech in progress vanished (cancellation is re-raised, not written), finished speeches stayed;
- the half turn sat in the feed with no marker and leaked into the next request's planning context;
- tools the cut speech had already run (approved file writes included) left their effects but no
  record, because tool rows were committed together with the speech row.

## 2. What is recorded now

| Record | Where | Purpose |
| :--- | :--- | :--- |
| **Turn** | `turns` | Status (`running`, `completed`, `failed`, `interrupted`, `abandoned`), phase (`planning`, `debating`, `synthesizing`, `completed`), opening message, the configuration the turn started with (participants, strategy, rounds, parallel limit, custom instructions, workspace), `stopped_early`, pause time and resume count. Written in the **same commit** as the opening request. |
| **Turn id** | `messages.turn_id`, `tool_calls.turn_id` | Every record belongs to its turn explicitly — no inference from ordering. |
| **Flow role** | `messages.turn_meta` | `opening`, `interjection`, `plan`, `speech`, `merge`, `gate`, `nomination` (+`speakers`), `assignment` (+`tasks`), `note`, `failure`, `interrupted`, `synthesis`. Recovery reads these values, never the human-facing sentences. |
| **Tool call, immediately** | `tool_calls` | Committed the moment the tool returns, with `message_id` empty; linked to its speech in the speech's own commit. |
| **Speech draft** | `speech_drafts` | For a speech that uses tools: the exact messages the model was looking at plus the loop's counters, saved after it asks for tools and after every tool result. Deleted in the speech's own commit. See §5. |

## 3. Detection and the three choices

On start-up (`lifespan` → `mark_interrupted_turns`) every turn still marked `running` is dead — the app is
a single process. A turn whose synthesis was already recorded becomes `completed`; the rest become
`interrupted`. A turn that stopped on an engine exception is marked `failed` and handled the same way.

Nothing restarts automatically: LLM cost and tool execution must not start without a person. Opening
the conversation shows a violet bar above the feed (the session list shows a clock icon):

| Button | What it does |
| :--- | :--- |
| **이어서 진행** (continue) | Recomputes the cursor from the records and continues from the next unfinished speech. |
| **지금까지로 결론** (finish) | Synthesizes from the recorded speeches only. Same meaning as *stop*: never marked consensus; the report says the debate was cut by a restart. |
| **버리기** (discard) | Deletes everything the turn left and puts the request back into the input box, like abort. Not offered to trial visitors. |

Sending a new request instead marks the unfinished turn `abandoned`; its records stay but it is no longer offered.

## 4. Rebuilding the cursor

The turn continues with **the configuration it started with** (`turns.config`), even if the roster was
edited meanwhile; the graph comes from `sessions.graph_snapshot`. Tool security follows the *current*
settings — tightening them while the turn was down must not be bypassed.

| Cut during | Rebuilt from | Continues with |
| :--- | :--- | :--- |
| Planning | No `plan` record | The plan |
| Sequential / debate round | `round_progress` — last round and who already spoke in it (by agent key) | The agents of that round who have not spoken |
| Orchestrator-led round | `round_record(...).nominated` | The recorded nomination; asked again only if the cut came before it |
| Parallel dispatch round | `round_record(...).tasks`, `.merged` | Only the agents without a speech, prompted from the records *before* this round's speeches; then the merge |
| Graph debate | `replay_graph` — replays the deterministic scheduler through the recorded node outputs | Only the nodes of the cut step that produced no output, prompted from the records before that step |
| Synthesis | `turns.stopped_early` | The synthesis only |

Before continuing, the decision ledger is folded once from its recorded position (one extra LLM call).

## 5. Continuing a speech after its last finished tool

A coder writing several files can run for minutes. Redoing such a speech from the start wastes those
minutes and tokens, so a speech that uses tools keeps a **draft** (`speech_drafts`):

1. The tool loop calls `checkpoint(state)` right after the model asks for tools and after each tool result.
   `state` holds the messages exactly as sent (system prompt, context, assistant turns incl. provider
   fields, tool results, notices), the text segments so far, calls used, the limit (with any extension a
   person granted), the context window (with any widening) and the tool logs.
2. The engine stores it under the speech's future message id, together with the ids of the tool rows
   already written. The speech row's commit deletes the draft.
3. On **continue**, a speech whose draft key (agent, kind, round/step, graph node) matches picks up the
   draft: no new prompt is built, the loop starts from the saved messages, tool calls that were asked for
   but never answered get `[결과 모름]` (result unknown — check before repeating), and a resume notice
   is appended. The message keeps the draft's id and original start time, and the tool rows written
   before the cut are linked to it.
4. Drafts that are not used — *finish*, a stop before that speaker's turn, an unreadable version — turn
   their tool rows into an "interrupted" note, and are deleted. Speeches without tools never create a draft
   and are redone as before, with the tools they ran passed in as observations.

## 6. What is still not recovered

- The one tool call running at the moment of the cut: its result is unknown and it may run again. MCP
  tools have no idempotency keys, so exactly-once is impossible.
- State inside tool servers (e.g. sandbox kernel variables) does not survive the restart even though the
  replayed history mentions it.
- The partial text of an answer being streamed when the server died (no tool step follows it).
- Pending interjections, a stop request, one-off approvals and context-widening answers that lived only in memory.

The report's total elapsed time is wall-clock from the request; a resumed turn adds
"(서버 중단 N 포함, M회 재개)".
