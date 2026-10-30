"""MADO 내장 MCP 서버: 타 AI 에이전트가 MADO의 멀티 에이전트 토론을 도구로 호출 (v1.5).

MADO는 다양한 외부 MCP 서버를 연동하는 호스트 역할을 수행함과 동시에, MADO 자체를 Model Context Protocol(MCP Streamable HTTP)
서버로 외부에 제공합니다. Claude Code, Cursor, 타 AI 에이전트가 설계 검토나 교차 검증처럼 다각도의 전문 분석이 필요한 과제를
MADO 토론 팀에 위임하고 정제된 결론을 수신할 수 있습니다. 핵심 오케스트레이션은 `app/headless.py`가 담당하며,
본 모듈은 MCP 도구 명세 및 프로토콜 인터페이스를 제공합니다.

* 엔드포인트: `/mcp` (Streamable HTTP). 로컬 루프백 접속은 인증 없이 즉시 허용되며, 외부 원격 접속 시에는 마스터 토큰을
  `Authorization: Bearer <토큰>` 헤더로 전송합니다 (`app/security.py`).
* 다중 에이전트 토론 특성상 시작(`start_debate`)과 결과 조회(`get_debate_result`)를 분리 제공하며, `wait_seconds` 파라미터로
  완료 대기 중 실시간 진행 상황(라운드, 발언자)에 대한 progress 알림을 수신할 수 있습니다.
* MCP를 통해 호출된 턴은 비대면 턴(`attended=False`)으로 동작합니다: 사용자 승인 카드를 띄우지 않으며, 도구 보안 정책상
  "사용자 확인(Ask)" 대상 호출은 자동 차단됩니다.

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
    return ", ".join(k for k, v in STRATEGY_MAP.items() if not v.uses_graph_file)


@mado_mcp.tool()
async def list_agents() -> List[Dict[str, Any]]:
    """MADO에 등록된 에이전트 목록을 조회합니다 (키, 이름, 역할, 활성화 여부). `start_debate`의 `agents` 인자에 에이전트 키를 지정할 수 있습니다."""
    return [
        {"key": a.key, "name": a.name, "role": a.role, "enabled": bool(getattr(a, "enabled", True))}
        for a in get_agent_pool().list_all()
    ]


@mado_mcp.tool()
async def list_debates(limit: int = 20) -> List[Dict[str, Any]]:
    """최근 대화 세션 목록을 조회합니다 (세션 ID, 제목, 토론 전략, 진행 여부)."""
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
    """토론 턴을 시작하고 세션 ID를 즉시 반환합니다. 결과는 get_debate_result 로 조회합니다.

    기존 session_id 를 지정하면 해당 대화 세션의 맥락(이전 결론 및 결정 장부)을 이어서 토론하며, 미지정 시 새 세션을 생성합니다.
    지원 전략: sequential_debate, adversarial_debate, orchestrator_led, parallel_dispatch.
    agents 목록을 생략하면 활성화된 전체 에이전트가 참여합니다.
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
    """토론 세션의 현재 상태를 조회합니다: 진행 중인 경우 현재 페이즈와 진행 메시지, 완료된 경우 최종 턴 상태 반환."""
    try:
        return await headless.turn_status(session_id)
    except headless.HeadlessError as exc:
        return {"error": str(exc)}


@mado_mcp.tool()
async def get_debate_result(session_id: str, wait_seconds: int = 0, ctx: Optional[Context] = None) -> Dict[str, Any]:
    """최종 턴의 실행 결과를 조회합니다 (결론, 아티팩트 목록, 결정 장부, 토큰 사용량). wait_seconds(최대 600초) 동안 턴 완료를 대기합니다.

    대기 중 진행 상황(라운드, 발언자)에 대한 progress 알림을 전송합니다. 시간 내에 완료되지 않으면
    `"status": "running"`과 현재 진행 상태를 반환하므로 재호출하여 대기할 수 있습니다.
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
    """진행 중인 토론의 중단을 요청하고 현재까지의 발언 내용을 바탕으로 결론을 합성합니다."""
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
