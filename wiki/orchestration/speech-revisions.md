# Retrying One Speech and Inline Comments

Two ways to correct one specialist without running the whole turn again. Retrying replaces a finished
speech in place; a comment reaches one agent's next speech. The decision is
[ADR-036](../../lectures/05-adr/ADR-036-retry-one-speech-and-inline-comments.md).

| | Retry | Inline comment | Interjection |
| :--- | :--- | :--- | :--- |
| Target | One finished specialist speech | One agent, through a passage of its speech | Everyone from the next speaker on |
| When | Only when nothing is running in the conversation | Any time | While a debate runs |
| Effect | New version of that speech now | Added to that agent's next prompt | A user message in the shared record |
| Enters the shared record | The new version does | No | Yes |

Both are on the expert page only. The trial and beginner pages pass no callbacks, so their cards have
neither control.

---

## 1. What can be retried

`speech_retry.retryable()` accepts `KIND_SPEECH` rows with `msg_type` `agent` or `error`, so a speech that
got no response can be run again once the endpoint is back. Nothing else qualifies:

- Plans, merges, gates and syntheses decided what came after them. Changing their text would leave the
  record and the flow disagreeing.
- Sub-debate speeches belong to the exchange between two specialists.
- `KIND_FAILURE` and `KIND_INTERRUPTED` rows are notes about a speech, not speeches.

`load_target()` also refuses a speech of a `running`, `failed` or `interrupted` turn. Those turns are resumed
by reading tool rows with an empty `message_id` as tools of an unfinished speech
([Interrupted Turns](turn-recovery.md)), and a retired version's tool rows look the same. Continue or finish
the turn first. The page refuses while any run is active in the conversation.

---

## 2. Versions

The new version goes into the same `messages` row. The id, `created_at`, round, node and `turn_meta` stay, and
`content`, `msg_type`, `started_at`, `finished_at` and `llm_meta` change. Everything that points at the row
keeps working without being told: the ledger's `ledger_through_id`, plan approval, graph replay, and the search
index through its `AFTER UPDATE OF content` trigger.

The version that stops being live moves to `messages.revisions`:

```python
{"n": 1, "content": "...", "msg_type": "agent", "llm_meta": {...},
 "started_at": "...", "finished_at": "...", "tool_record_ids": ["..."]}
```

- **Numbers.** A new attempt gets `len(revisions) + 2`. The live version's number is not stored: it is the
  smallest number no past version uses (`live_number`), so restoring needs no renumbering.
- **Tool records.** The retired version's rows get `message_id = NULL` and their ids go into
  `tool_record_ids`. They keep `turn_id`, so the security audit still counts them. Restoring links them back.
  An export lists them under the conversation's general tool section; the page draws them under their version.
- **Failed attempts.** A retry that ends in `error` changes nothing on the row except `revisions`. A working
  speech is never replaced by a connection notice. The attempt stays as a version, so its reason and any tools it
  ran can be read, but it cannot be restored.
- **Conditions.** `llm_meta.retry` records `{"model", "instruction"}`. The card's version caption and the
  export read it.

The swap and the end-of-conversation note are written in the commit that records the speech
(`SpeechRevision.apply`, called from `TurnRecorder.persist`'s `before_commit`, so a failed commit retries it).

---

## 3. What the retried speech sees

`SpeechRevisionExecutor` rebuilds the moment before the speech from the record:

| Input | Source |
| :--- | :--- |
| Transcript | `load_history` up to that row. Later speeches and the conclusion are not shown. |
| Same-step peers | Removed for parallel and graph strategies. They ran at the same time and were not visible then. |
| Ledger and summary | `TurnModel.memory` of the nearest earlier turn. Today's ledger holds later decisions, and this turn's earlier rounds are in the transcript verbatim. |
| Participants, instructions, attachments | The turn's frozen `TurnModel.config`. |
| Turn instruction | `turn_meta.prompt_tail`, the last prompt message saved when the speech first ran. Rows older than this field get the generic turn prompt. |
| Tools | The agent's usual tools through the conversation's current tool security. Approval cards and questions work as in a turn. |

`retry_notice()` then appends:

- that the record stops before this turn and that **workspace files are in their current state**, which
  cannot be rewound;
- the previous version, clipped to `PREVIOUS_VERSION_CHARS`, only when there is an instruction or a comment
  to apply to it. A bare retry should not be anchored to the text it is replacing;
- the user's instruction;
- the tools the previous version already ran, as observations, so a file write is not repeated blindly.

A graph node with restricted `sees` gets the whole earlier record rather than only its inputs. The node's
instruction still comes from `prompt_tail`.

### Choosing a model

`connection_choices()` offers each distinct connection used by the conversation's agents, plus the global
default when it can answer. The page receives only `connection_id` hashes and labels; the engine resolves a hash
back to a connection, so keys and addresses never reach the browser. `with_connection()` copies the connection
and that model's capacity values onto the agent. Persona, tools and skills stay. A hash that no longer matches
fails the run instead of silently using the original model.

---

## 4. What does not re-run, and how that is disclosed

Later speeches and the conclusion stay as written, on the old version. That is the point of the feature, so
it is stated rather than hidden:

- **In the dialog**, before anything runs.
- **In the record**, as a `system` message at the end of the conversation (`turn_meta.revised_message`). The next
  turn's agents read it and are told to prefer the new version where the two disagree. A failed attempt
  leaves no note.
- **In the decision ledger.** A non-empty ledger is corrected right away with one orchestrator call, shown
  only the changed speech and told to fix items based on the old version. Every agent's system prompt carries the
  ledger, so a stale entry would keep overriding the new text. `ledger_through_id` does not move, and a failed
  correction only shows the usual warning.

Restoring a past version does the same: a note and a ledger correction, with no speech generated.

A speech already folded into `transcript_summary` keeps its old version there. The ledger and the note cover
that case. Tokens are added to the turn's `TurnModel.usage`. Files the retry changes join the turn's checkpoint
only when it is the conversation's latest turn; moving an older turn's end point would absorb later turns'
changes ([Workspace Checkpoints](workspace-checkpoints.md)).

---

## 5. The run

`DebateRunner.retry_speech` and `restore_speech` use the ordinary `TurnRun`, so streaming, reconnecting and
approval cards behave as in a turn. `run.retry_target` holds the message id and changes four things:

| | Debate run | Retry run |
| :--- | :--- | :--- |
| `request_stop`, `interject` | Accepted | Refused; there is no next speaker and no synthesis |
| Feed controls | Stop, emergency stop, interjection input | `다시 실행 취소` only; the input is locked |
| `abort()` | Deletes the turn | Cancels and deletes nothing. Tools already run keep their results |
| `run_finished` | Standard payload | Adds `retry_target`; the page rebuilds from the database and returns to the card |

The turn row's status does not change. Drafts ([ADR-025](../../lectures/05-adr/ADR-025-resume-speeches-by-tool-step.md))
are not written, because no resume path exists for a retry; a server stop during one leaves the old version
live. Streaming reuses the existing card (`CardTimeline._restart_card`). After a reload, the page uses the
snapshot's partial text for a streaming id so chunks do not land after the old version.

`RunBinding.detach()` skips cancelling the current task, because this rebuild runs inside the event consumer.

---

## 6. Inline comments

Selecting text inside an agent's card shows a floating button (`SPEECH_COMMENT_JS`); it sends the card's
`data-mado-speech` id and the selected text through a global NiceGUI event. The quote is the rendered text, so
Markdown markers are gone. Comments are drawn as lines under the card and are not highlighted in the body.
A selection across two cards, a streaming card and a failure notice cannot take a comment.

Rows live in `speech_comments` rather than `TurnControl`, because a comment can be written with no turn running
and can wait across turns.

```
add_comment ──▶ pending ──▶ SpeechRunner.speak reads pending_for(agent)
                                │
                                ▼
                  appended to the last user message of this prompt only
                                │
                  speech recorded ──▶ delivered, in the same commit
                  speech failed   ──▶ still pending
```

- **Only that agent, once.** The comment is added to the prompt alone. It enters neither `messages` nor the
  ledger. A comment on a plan or conclusion goes to the orchestrator's next speech.
- **Not lost.** A speech that ends in `error` or is cut off does not consume it. An agent absent from the next
  turn leaves it pending, and a pending comment can be deleted. A delivered one cannot.
- **Drafts.** A speech resumed from a draft does not rebuild its prompt, so it neither reads nor claims comments.
- **Retry.** A retry carries only the pending comments on that speech and brings the previous version with them.
  A comment on another speech waits, because the retry's record stops before it might apply.
- **Read errors.** A failed read logs a warning and the speech goes on without comments.

`speech_comments_delivered` updates the line to `전달됨` on attached pages; a reload reads the status from the
database. Deleting a conversation or discarding a turn removes its comments, and a fork copies them with new
ids, so pending ones reach the fork's next speech.

---

## 7. Where the code is

| What | Where |
| :--- | :--- |
| Eligibility, versions, model choice, prompt notice | [speech_retry.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/speech_retry.py) |
| Rebuilding the moment, ledger correction | [phases/retry.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/phases/retry.py) |
| Runtime lifetime | `OrchestratorEngine._revising_speech` |
| Writing a revision, delivering comments | `_Speech.after_rows`, `SpeechRunner.speak` |
| Comment rules | [speech_comments.py](file:///d:/MultiAgentDebateOrchestration/app/orchestration/speech_comments.py) |
| Dialogs and selection script | [chat_feed/speech_actions.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/chat_feed/speech_actions.py) |
| Card controls | [chat_feed/cards.py](file:///d:/MultiAgentDebateOrchestration/app/ui/components/chat_feed/cards.py) |
| Tests | `tests/test_speech_retry.py`, `tests/test_speech_comments.py`, `tests/test_speech_cards.py` |
