# Headless API & MADO as an MCP Server (v1.5)

A debate used to start only from a page (or, for MADOWarGame, by importing the engine). v1.5 adds two
doors that need no browser, so scripts, CI jobs and other agents (Claude Code, an IDE) can hand MADO a
question and read back the conclusion. Both are thin layers over one service,
[app/headless.py](file:///d:/MultiAgentDebateOrchestration/app/headless.py).

---

## 1. REST — `/api/sessions/...`

| Method & path | What it does |
| :--- | :--- |
| `GET /api/sessions?limit=50` | Recent conversations: id, title, strategy, agents, `running` |
| `POST /api/sessions` | Create a conversation. Body: `title`, `strategy`, `max_rounds` (1–10), `agents` (keys; empty = every enabled agent), `custom_instructions`, `workspace_dir`, `converge_early`, `token_budget`, `sub_debates`. `201` |
| `POST /api/sessions/{id}/turns` | Start a turn: `{"prompt": "...", "attended": false}`. `202`; `409` if one is running |
| `POST /api/sessions/{id}/stop` | Ask the running turn to stop and conclude with what it has |
| `GET /api/sessions/{id}/status` | Running: phase, status line, whether a person's answer is awaited. Idle: last turn's status |
| `GET /api/sessions/{id}/result?wait=0` | Last turn: `conclusion`, `artifacts`, `decision_ledger`, `usage`, `ended_early`, `failed_agents`, `files_changed`, times. `wait` (≤ 600 s) waits for a running turn first |
| `GET /api/sessions/{id}/events` | Server-Sent Events: `snapshot` first (the same snapshot a page attaches with), then every engine event, then `run_finished`. Keep-alive comments every 15 s. With no running turn: one `idle` event |

Errors are `{"error": "..."}` with `400` (bad request: unknown strategy or agent, graph strategy,
empty prompt), `404` (no such conversation) or `409` (already running). The API accepts only current
strategy names — the legacy aliases and the "unknown means default" fallback that stored conversations
rely on are not applied to new requests. Graph debates need a graph picked on screen and cannot be
created here.

## 2. Unattended turns

A turn started from the API or MCP has, by default, **nobody to ask**: `TurnControl(attended=False)`.
`control.asks_human()` is the one place that decides, and every asking site uses it:

| Would normally ask | Unattended |
| :--- | :--- |
| Plan approval (ADR-028) | Skipped — the plan is used as written |
| Tool security verdict `ask` | Not run; the agent reads "nobody to ask" (`rule = unattended`) |
| Tool budget / context window limits | Wrap up without asking |
| `ask__ask_user` | Not offered ([Ask the user](../agents/ask-user.md)) |

Stop and interjections still work. Send `"attended": true` when a person will answer the cards from the
web page (the same conversation shows the running turn as usual).

## 3. MCP server — `/mcp`

[app/mcp_server.py](file:///d:/MultiAgentDebateOrchestration/app/mcp_server.py) exposes MADO itself as a
streamable-HTTP MCP server (FastMCP, stateless) mounted at `/mcp`:

| Tool | Purpose |
| :--- | :--- |
| `list_agents` | Agent keys, names, roles, enabled |
| `list_debates` | Recent conversations |
| `start_debate(prompt, title, strategy, max_rounds, agents, workspace_dir, session_id)` | New conversation, or a follow-up turn when `session_id` is given. Returns immediately |
| `get_debate_status(session_id)` | As the REST status |
| `get_debate_result(session_id, wait_seconds)` | Waits up to 600 s, sending MCP progress notifications with the status line; returns `"status": "running"` if not done yet |
| `stop_debate(session_id)` | As the REST stop |

```bash
claude mcp add --transport http mado http://127.0.0.1:8000/mcp
```

The session manager runs inside the FastAPI lifespan. FastMCP allows `run()` once per manager, so the
route forwards to whichever manager the current lifespan created (tests and restarts start a new one).
DNS-rebinding checks are left to `AccessMiddleware`, which already validates `Host` and `Origin`.

## 4. Authentication

Loopback callers are the owner, as for the pages. Remote callers send the owner token
(`MADO_ACCESS_TOKEN`) as `Authorization: Bearer <token>`. It is accepted on `/api/*` and `/mcp` only —
pages still use the login cookie — and a wrong token counts toward the same lockout as a failed login
(`429` while locked). Trial visitors never reach these paths (`app/trial/gate.py`).

## 5. Tests

`tests/test_headless_api.py`: create/start/result/status over ASGI, unattended turns with plan approval
on, the SSE stream, Bearer handling and lockout, and a real MCP client (`streamable_http_client`) calling
`list_agents` → `start_debate` → `get_debate_result`.
