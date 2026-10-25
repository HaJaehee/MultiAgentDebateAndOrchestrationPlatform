"""화면 없이 토론 돌리기: REST `/api/sessions/...`, MCP 서버 `/mcp`, Bearer 토큰 (v1.5).

1. 대화를 만들고(잘못된 전략·에이전트·라운드는 400), 턴을 시작하고(202, 돌고 있으면 409), 결과를 기다려 받는다.
2. API·MCP 로 시작한 턴은 답할 사람이 없는 턴이다 — 계획 승인이 켜져 있어도 기다리지 않는다.
3. 이벤트는 SSE 로 — 처음에 `snapshot`, 끝에 `run_finished`.
4. 원격 호출은 주인 토큰을 Bearer 로 보낸다. `/api/*` 와 `/mcp` 에서만 받고, 틀리면 실패 잠금에 센다.
5. MADO 의 MCP 서버를 실제 MCP 클라이언트로 부른다 (도구 목록, 토론 시작, 결과).
"""

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app import headless, mcp_server
from app.api import router
from app.database.session import init_db
from app.orchestration.control import TurnControl, asks_human
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from app.security import MAX_FAILURES, AccessControl, AccessMiddleware
from tests.fake_llm import FakeLLMCaller

TOKEN = "Abc123Def456Ghi789Jkl012"
REMOTE = ("10.0.0.5", 50000)


@pytest_asyncio.fixture
async def runner(monkeypatch):
    await init_db("sqlite+aiosqlite:///:memory:")
    debate_runner = DebateRunner(engine=OrchestratorEngine(llm_caller=FakeLLMCaller()))
    monkeypatch.setattr(headless, "get_debate_runner", lambda: debate_runner)
    yield debate_runner
    await debate_runner.shutdown()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    mcp_server.mount(app)
    return app


def _client(app, *, client=("127.0.0.1", 50000), headers=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client), base_url="http://localhost:8000", headers=headers,
    )


def test_unattended_controls_do_not_ask_people():
    assert asks_human(TurnControl()) and not asks_human(TurnControl(attended=False)) and not asks_human(None)


@pytest.mark.asyncio
async def test_create_start_and_read_the_result(runner, plan_approval):
    async with _client(_app()) as c:
        bad = await c.post("/api/sessions", json={"strategy": "graph_debate"})
        assert bad.status_code == 400 and "그래프" in bad.json()["error"]
        assert (await c.post("/api/sessions", json={"agents": ["nobody"]})).status_code == 400
        assert (await c.post("/api/sessions", json={"max_rounds": 0})).status_code == 400

        created = await c.post("/api/sessions", json={"title": "API 설계", "agents": ["architect", "critic"]})
        assert created.status_code == 201
        sid = created.json()["session_id"]
        assert created.json()["agents"] == ["orchestrator", "architect", "critic"]

        assert (await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "  "})).status_code == 400
        started = await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "결제 서비스 설계를 검토해 줘"})
        assert started.status_code == 202 and started.json()["attended"] is False
        again = await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "또"})
        assert again.status_code == 409

        # 계획 승인이 켜져 있지만 답할 사람이 없는 턴이므로 기다리지 않고 끝납니다.
        result = (await c.get(f"/api/sessions/{sid}/result", params={"wait": 20})).json()
        assert result["status"] == "completed", result
        assert "최종 합의 요약" in result["conclusion"]
        assert result["request"] == "결제 서비스 설계를 검토해 줘"
        assert {a["type"] for a in result["artifacts"]} >= {"markdown", "json"}
        assert result["ended_early"] == {"reason": "converged", "round": 2}
        assert result["failed_agents"] == [] and result["decision_ledger"]

        status = (await c.get(f"/api/sessions/{sid}/status")).json()
        assert status["running"] is False and status["status"] == "completed"
        listed = (await c.get("/api/sessions")).json()["sessions"]
        assert any(s["session_id"] == sid and s["title"] == "API 설계" for s in listed)
        assert (await c.get(f"/api/sessions/{uuid.uuid4()}/result")).status_code == 404


@pytest.mark.asyncio
async def test_events_stream_from_snapshot_to_the_end(runner):
    async with _client(_app()) as c:
        sid = (await c.post("/api/sessions", json={"max_rounds": 1})).json()["session_id"]
        # 돌고 있는 턴이 없으면 `idle` 하나로 끝납니다.
        idle = await c.get(f"/api/sessions/{sid}/events")
        assert idle.headers["content-type"].startswith("text/event-stream")
        assert "event: idle" in idle.text

        await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "짧게 검토"})
        stream = await c.get(f"/api/sessions/{sid}/events")
    events = [line.removeprefix("event: ") for line in stream.text.splitlines() if line.startswith("event: ")]
    assert events[0] == "snapshot" and events[-1] == "run_finished"
    assert "turn_completed" in events
    data = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
    assert data[0]["session_id"] == sid and isinstance(data[0]["streaming_ids"], list)


async def _remote(control, path, headers):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    transport = httpx.ASGITransport(app=AccessMiddleware(inner, control), client=REMOTE)
    async with httpx.AsyncClient(transport=transport, base_url="http://mado.lan:8000") as c:
        return (await c.get(path, headers=headers)).status_code


@pytest.mark.asyncio
async def test_remote_callers_use_the_owner_token_as_a_bearer():
    control = AccessControl(TOKEN)
    good = {"Authorization": f"Bearer {TOKEN}"}
    assert await _remote(control, "/api/sessions", good) == 200
    assert await _remote(control, "/mcp", good) == 200
    # 화면 경로는 Bearer 로 열지 않습니다 (브라우저는 로그인 쿠키로).
    assert await _remote(control, "/", {**good, "Accept": "text/html"}) == 303
    assert await _remote(control, "/api/sessions", {}) == 401
    for _ in range(MAX_FAILURES):
        assert await _remote(control, "/api/sessions", {"Authorization": "Bearer wrong-token"}) == 401
    # 틀린 토큰은 로그인과 같은 잠금에 셉니다. 잠긴 동안은 맞는 토큰도 받지 않습니다.
    assert await _remote(control, "/api/sessions", good) == 429
    # 원격 주인 접속이 꺼져 있으면(토큰 없음) Bearer 도 소용없습니다.
    assert await _remote(AccessControl(None), "/api/sessions", good) == 403


@pytest.mark.asyncio
async def test_the_mcp_server_runs_a_debate_for_another_agent(runner):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = _app()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", timeout=30)

    async with mcp_server.running(), http:
        async with streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"list_agents", "start_debate", "get_debate_result", "get_debate_status",
                        "stop_debate", "list_debates"} <= tools

                listed = await session.call_tool("list_agents", {})
                assert {"orchestrator", "architect"} <= {a["key"] for a in listed.structuredContent["result"]}

                started = await session.call_tool("start_debate", {"prompt": "캐시 전략을 검토해 줘", "max_rounds": 1})
                sid = json.loads(started.content[0].text)["session_id"]
                done = await session.call_tool("get_debate_result", {"session_id": sid, "wait_seconds": 20})
                result = json.loads(done.content[0].text)
    assert result["status"] == "completed" and "최종 합의 요약" in result["conclusion"]

    bad = await mcp_server.start_debate(prompt="x", strategy="nope")
    assert "error" in bad and "sequential_debate" in bad["strategies"]
