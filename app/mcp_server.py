"""MADO 를 MCP 서버로: 다른 에이전트가 "토론 팀" 을 도구로 부릅니다 (v1.5).

MADO 는 MCP 서버를 **쓰는** 쪽(호스트)이기도 하지만, 여기서는 반대로 MADO 자신을 MCP 서버로 내놓습니다.
Claude Code·IDE·다른 에이전트가 설계 검토나 위험 분석처럼 여러 관점이 필요한 일을 MADO 에 맡기고 결론을
받아 갑니다. 일은 `app/headless.py` 가 하고, 여기는 MCP 도구의 모양만 정합니다.

* 주소는 `/mcp` (streamable HTTP). 서버 PC 에서는 그냥 붙고, 다른 PC 에서는 주인 토큰을
  `Authorization: Bearer` 로 보냅니다 (`app/security.py`).
* 토론은 몇 분이 걸립니다. 그래서 시작(`start_debate`)과 결과(`get_debate_result`)를 나눴고, 결과는
  `wait_seconds` 만큼 끝나기를 기다리며 진행 상황을 progress 알림으로 보냅니다.
* 이 길로 시작한 턴은 답할 사람이 없는 턴입니다 (`attended=False`): 승인 카드를 띄우지 않고, 도구
  보안이 "묻기" 로 판정한 호출은 실행하지 않습니다.

연결 예 (Claude Code):

    claude mcp add --transport http mado http://127.0.0.1:8000/mcp
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app import headless
from app.agents.pool import get_agent_pool
from app.orchestration.strategies import DEFAULT_STRATEGY, STRATEGY_MAP

MCP_PATH = "/mcp"
# 결과를 기다리는 동안 진행 상황을 확인하는 간격(초)과, 한 번에 기다릴 수 있는 최대 시간(초).
POLL_SECONDS = 2.0
MAX_WAIT_SECONDS = 600

INSTRUCTIONS = (
    "MADO runs a moderated debate between specialist agents (architect, coder, critic, ...) and returns "
    "an orchestrator-written conclusion. Use it for design reviews, trade-off analysis and risk reviews. "
    "Call start_debate, then get_debate_result with wait_seconds (it reports progress while waiting). "
    "A finished session can take a follow-up prompt with start_debate(session_id=...)."
)

mado_mcp = FastMCP(
    "MADO",
    instructions=INSTRUCTIONS,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    # 호스트 이름 검사는 앱 바깥 계층(`AccessMiddleware`)이 이미 합니다 (DNS 리바인딩 포함).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _strategies() -> str:
    return ", ".join(k for k, v in STRATEGY_MAP.items() if not v.runs_graph)


@mado_mcp.tool()
async def list_agents() -> List[Dict[str, Any]]:
    """MADO 에 등록된 에이전트 (키, 이름, 역할, 켜짐 여부). start_debate 의 agents 에 키를 씁니다."""
    return [
        {"key": a.key, "name": a.name, "role": a.role, "enabled": bool(getattr(a, "enabled", True))}
        for a in get_agent_pool().list_all()
    ]


@mado_mcp.tool()
async def list_debates(limit: int = 20) -> List[Dict[str, Any]]:
    """최근 대화 목록 (id, 제목, 전략, 진행 중 여부)."""
    return await headless.list_sessions(limit)


@mado_mcp.tool()
async def start_debate(
    prompt: str,
    title: str = "",
    strategy: str = DEFAULT_STRATEGY,
    max_rounds: int = 3,
    agents: Optional[List[str]] = None,
    workspace_dir: str = "",
    session_id: str = "",
) -> Dict[str, Any]:
    """토론을 시작하고 곧바로 돌아옵니다. 결과는 get_debate_result 로 받습니다.

    session_id 를 주면 그 대화에서 이어 묻습니다 (앞 턴의 결론·결정 장부를 기억합니다). 주지 않으면 새 대화를
    만듭니다. strategy: sequential_debate · adversarial_debate · orchestrator_led · parallel_dispatch.
    agents 를 비우면 켜진 에이전트가 모두 참여합니다.
    """
    try:
        if not session_id:
            created = await headless.create_session(
                title=title or prompt[:60], strategy=strategy, max_rounds=max_rounds,
                agents=agents, workspace_dir=workspace_dir,
            )
            session_id = created["session_id"]
        started = await headless.start_turn(session_id, prompt, attended=False)
    except headless.HeadlessError as exc:
        return {"error": str(exc), "strategies": _strategies()}
    return {**started, "next": "call get_debate_result(session_id, wait_seconds=300)"}


@mado_mcp.tool()
async def get_debate_status(session_id: str) -> Dict[str, Any]:
    """지금 상태: 진행 중이면 단계와 상태 줄, 끝났으면 마지막 턴의 상태."""
    try:
        return await headless.turn_status(session_id)
    except headless.HeadlessError as exc:
        return {"error": str(exc)}


@mado_mcp.tool()
async def get_debate_result(session_id: str, wait_seconds: int = 0, ctx: Optional[Context] = None) -> Dict[str, Any]:
    """마지막 턴의 결과 (결론, 산출물, 결정 장부, 토큰 사용량). wait_seconds 만큼 끝나기를 기다립니다 (최대 600).

    기다리는 동안 progress 알림으로 진행 상황(라운드·발언자)을 보냅니다. 시간 안에 끝나지 않으면
    `"status": "running"` 과 지금 상태를 돌려주므로 다시 부르면 됩니다.
    """
    try:
        deadline = time.monotonic() + max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
        step = 0
        while True:
            status = await headless.turn_status(session_id)
            if not status.get("running"):
                return await headless.turn_result(session_id)
            if time.monotonic() >= deadline:
                return {**status, "status": "running", "next": "call get_debate_result again"}
            if ctx is not None:
                step += 1
                await ctx.report_progress(progress=step, message=str(status.get("status_text") or ""))
            await headless.wait_for_turn(session_id, min(POLL_SECONDS, max(0.1, deadline - time.monotonic())))
    except headless.HeadlessError as exc:
        return {"error": str(exc)}


@mado_mcp.tool()
async def stop_debate(session_id: str) -> Dict[str, Any]:
    """진행 중인 토론을 멈추고 지금까지의 발언으로 결론을 내게 합니다."""
    return {"session_id": session_id, "stop_requested": headless.stop_turn(session_id)}


class _Endpoint:
    """`/mcp` 로 온 요청을 지금 돌고 있는 세션 관리자에 넘깁니다 (Starlette 는 함수가 아닌 것을 ASGI 앱으로 봅니다).

    FastMCP 의 세션 관리자는 한 번만 돌릴 수 있습니다. 앱의 수명(lifespan)이 다시 시작되면(시험, 재기동)
    `running` 이 새 관리자를 만들고, 경로는 그대로 두고 넘길 곳만 바뀝니다.
    """

    manager: Any = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.manager is None:
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": "MCP server is not running".encode()})
            return
        await self.manager.handle_request(scope, receive, send)


_endpoint = _Endpoint()


def mount(server: Any) -> None:
    """FastAPI 앱에 `/mcp` 경로를 붙입니다. 요청은 앱의 lifespan 동안 도는 세션 관리자가 받습니다 (`running`)."""
    server.router.routes.append(Route(MCP_PATH, endpoint=_endpoint))


@asynccontextmanager
async def running() -> AsyncIterator[None]:
    """앱 lifespan 동안 streamable HTTP 세션 관리자를 돌립니다. 수명마다 새 관리자를 만듭니다."""
    mado_mcp._session_manager = None  # noqa: SLF001 - 한 번만 돌릴 수 있는 관리자를 수명마다 새로
    mado_mcp.streamable_http_app()
    manager = mado_mcp.session_manager
    async with manager.run():
        _endpoint.manager = manager
        try:
            yield
        finally:
            _endpoint.manager = None
