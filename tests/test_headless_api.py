"""헤드리스(Headless) 토론 실행: REST `/api/sessions/...`, MCP 서버 `/mcp`, Bearer 토큰 인증 (v1.5).

1. 세션을 생성하고(잘못된 전략·에이전트·라운드는 400), 턴을 시작하며(202, 중복 실행 시 409), 결과를 대기하여 수신한다.
2. API·MCP로 시작한 턴은 비대면(unattended) 턴이므로 계획 승인이 설정되어 있어도 대기하지 않고 자동 진행한다.
3. 이벤트는 SSE(Server-Sent Events) 스트림으로 전송된다 (초기 `snapshot`, 완료 시 `run_finished`).
4. 원격 호출은 마스터 토큰을 `Authorization: Bearer` 헤더로 전달한다. `/api/*` 및 `/mcp`에서만 허용되며, 실패 시 계정 잠금 정책이 동일하게 적용된다.
5. MADO 내장 MCP 서버를 실제 MCP 클라이언트로 호출하여 검증한다 (도구 목록 조회, 토론 시작, 결과 수신).
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
        assert (await c.post("/api/sessions", json={"sub_debate_rounds": 0})).status_code == 400
        assert (await c.post("/api/sessions", json={"sub_debate_rounds": 6})).status_code == 400

        created = await c.post("/api/sessions", json={"title": "API 설계", "agents": ["architect", "critic"]})
        assert created.status_code == 201
        sid = created.json()["session_id"]
        assert created.json()["agents"] == ["orchestrator", "architect", "critic"]

        assert (await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "  "})).status_code == 400
        started = await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "결제 서비스 설계를 검토해 줘"})
        assert started.status_code == 202 and started.json()["attended"] is False
        again = await c.post(f"/api/sessions/{sid}/turns", json={"prompt": "또"})
        assert again.status_code == 409

        # 계획 승인이 설정되어 있어도 비대면(unattended) 턴이므로 대기 없이 완료됩니다.
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
        # 진행 중인 턴이 없으면 `idle` 이벤트 후 대기합니다.
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
    # 웹 화면 경로는 Bearer 인증을 지원하지 않으며 브라우저 로그인 쿠키를 요구합니다.
    assert await _remote(control, "/", {**good, "Accept": "text/html"}) == 303
    assert await _remote(control, "/api/sessions", {}) == 401
    for _ in range(MAX_FAILURES):
        assert await _remote(control, "/api/sessions", {"Authorization": "Bearer wrong-token"}) == 401
    # 잘못된 토큰을 반복 입력하면 로그인 실패 잠금 정책이 동일하게 적용됩니다.
    assert await _remote(control, "/api/sessions", good) == 429
    # 원격 접속이 비활성화된 경우(토큰 미설정) Bearer 헤더도 거부됩니다.
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
