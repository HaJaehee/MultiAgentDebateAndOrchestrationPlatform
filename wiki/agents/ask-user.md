# Ask the User — `ask__ask_user` (v1.5)

Plan approval ([ADR-028](../orchestration/plan-approval.md)) is the place to correct course **before**
the debate. During it, a specialist facing an ambiguous request used to guess — and a wrong guess sent
the remaining rounds in the wrong direction. `ask__ask_user` lets a specialist ask one short question
mid-speech. Code: [app/agents/ask_user.py](file:///d:/MultiAgentDebateOrchestration/app/agents/ask_user.py).

## 1. When it is offered

All of these must hold:

* the conversation allows it — roster switch `에이전트 질문 받기` (`sessions.ask_user`). New conversations
  on the expert page start with it on; the column defaults to off so conversations from pages without a
  question card (trial, easy) never wait for an answer nobody can give;
* someone is watching the turn (`control.asks_human`; not API/MCP/bench/batch turns);
* the speaker is a specialist — not the orchestrator, not an agent answering a peer consultation;
* questions are left this turn (3 per turn, shared by all speakers).

`ask` is a reserved MCP server name, like `skills` and `peers`.

## 2. What happens

1. The tool loop runs it as a host tool, without a tool-security verdict (it touches nothing).
2. The engine (`SpeechRunner.make_asker`) opens a question card in the feed: the question (Markdown),
   option buttons if given, a free-text box, `건너뛰기`, and a countdown (5 minutes). The card is in the
   run snapshot, so a refreshed page or another screen shows it too. A person looking elsewhere is told through the tab title, the sidebar badge and a desktop notification ([UI Components §1.3.11](../ui/components.md#1311-telling-people-who-are-looking-elsewhere-adr-037)); the question's text stays on the card.
3. The answer comes back as the tool result (`유저의 답: …`). No answer, a skip or a stop returns
   "(답 없음) — state the assumption and continue"; the speech is never cut.
4. An answer is also queued as an interjection (`[<agent> 의 질문에 대한 답] 질문: … 답: …`), recorded
   before the next speaker as the person's words — so it joins the pinned user record and every later
   speaker follows it, not only the one who asked.

## 3. Pieces

| Piece | Where |
| :--- | :--- |
| Tool schema, guidance, cap, `asking()` scope | `app/agents/ask_user.py` |
| Waiting: `UserQuestionRequest`, `ask_user_question`, `resolve_user_question` | `app/orchestration/control.py` |
| Scope per speech, card events, interjection | `app/orchestration/speech.py` |
| Events `user_question_requested` / `user_question_resolved`, snapshot `user_questions` | `app/events.py`, `app/orchestration/turn_run.py` |
| Card | `app/ui/components/chat_feed/approvals.py` |

## 4. Tests

`tests/test_ask_user.py`: offering rules, answers and the cap, the real tool loop (no gate), the mailbox
(answer / skip / timeout, kept apart from limit cards), a runner turn where the answer reaches the next
speaker, and no tool when the conversation or the turn has nobody to ask.
