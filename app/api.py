"""HTTP 엔드포인트: `/api/*` 와 에이전트 아이콘.

화면은 NiceGUI(웹소켓)로 돌고, 여기 있는 것은 상태 확인·진단 보고·아이콘처럼 HTTP 로 받는 편이 나은
것들입니다. 접근 제어는 앱 바깥 계층(`app/security.py` 의 `AccessMiddleware`)이 모든 경로에 똑같이 겁니다.

`/api/sessions/...` 는 화면 없이 토론을 돌리는 길입니다 (v1.5, `app/headless.py`). 서버 PC 에서는 그냥 부르고,
다른 PC 에서는 주인 토큰을 `Authorization: Bearer` 로 보냅니다.
"""

import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.about import APP_VERSION_LABEL, AUTHOR, AUTHOR_EMAIL
from app.agents.personas import effective_personas
from app.agents.pool import get_agent_pool
from app.agents.skills import scan_skills, skills_root
from app.config import PROJECT_ROOT, get_config, resolve_agent_icon
from app.database.models import SessionModel
from app.database.session import get_session_factory
from app.diagnostics import MAX_CLIENT_REPORT_BYTES, loop_health, parse_client_body, record_client_report
from app import headless
from app.mcp.pool import get_runtime_pool
from app.orchestration.strategies import DEFAULT_STRATEGY

router = APIRouter()

# 아이콘 파일을 못 찾았을 때 대신 내려주는 그림. 아바타가 깨진 이미지로 남는
# 것보다, 아무 말 없이 기본 로봇이 서 있는 편이 낫습니다.
FALLBACK_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#94a3b8">'
    '<path d="M20 9V7a2 2 0 0 0-2-2h-3V3.5a1.5 1.5 0 0 0-3 0V5H9a2 2 0 0 0-2 2v2H5.5a1.5 1.5 0 0 0 0 3H7v5'
    'a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-5h1.5a1.5 1.5 0 0 0 0-3H20zm-8.5 2.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0z'
    'm7 0a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM9 16h9v1.5H9V16z"/></svg>'
)


@router.get("/api/health")
async def health_check():
    cfg = get_config()
    pool = get_agent_pool()
    return {
        "status": "healthy",
        "version": APP_VERSION_LABEL,
        "author": {"name": AUTHOR, "email": AUTHOR_EMAIL},
        "app": {"host": cfg.app.host, "port": cfg.app.port, "debug": cfg.app.debug},
        "registered_agents": [a.key for a in pool.list_all()],
        # 이벤트 루프가 붙잡혔던 기록의 요약 (`app/diagnostics.py`). 감시가 꺼져 있으면 null.
        "event_loop": loop_health(),
    }


@router.post("/api/diagnostics/client")
async def diagnostics_client(request: Request) -> Response:
    """브라우저가 보내는 진단 보고 — 긴 작업, 연결 끊김과 그 사유 (`app/ui/diagnostics_script.py`).

    웹소켓이 아니라 HTTP 로 받습니다. 소켓이 끊긴 그 순간의 보고가 가장 중요한데, 그때는 소켓으로
    보낼 수 없기 때문입니다. 접근 제어는 다른 `/api/*` 와 같습니다 (주인만, 같은 출처만).
    """
    if int(request.headers.get("content-length") or 0) > MAX_CLIENT_REPORT_BYTES:
        return Response(status_code=413)
    report, problem = parse_client_body(await request.body())
    if problem:
        return Response(status_code=400)
    if isinstance(report, dict):
        report.setdefault("user_agent", request.headers.get("user-agent", ""))
    record_client_report(report, request.client.host if request.client else "")
    return Response(status_code=204)


@router.get("/agent-icon")
async def agent_icon(src: str = ""):
    """에이전트 아이콘 이미지. `src` 는 conf.json 에 적힌 경로입니다.

    내려주는 것은 **프로젝트 폴더 안의 이미지 파일** 뿐입니다. 경로가 설정에서
    오는 값이라, 그 밖을 가리키면 (`../..`, 절대 경로) 파일을 읽어 주지 않습니다.

    못 찾으면 404 가 아니라 기본 그림을 200 으로 돌려줍니다. 화면은 이미 그려진
    뒤이고, 여기서 실패하면 아바타 자리가 깨진 이미지로 남습니다.
    """
    path = resolve_agent_icon(src)
    if path is not None:
        try:
            path.relative_to(PROJECT_ROOT)
        except ValueError:
            path = None

    if path is None:
        return Response(
            content=FALLBACK_ICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )
    # 파일 이름에 내용 해시가 들어가므로 오래 캐시해도 안전합니다. 손으로 적은
    # 경로까지 그렇지는 않아 하루로 둡니다.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/agents")
async def list_agents():
    pool = get_agent_pool()
    return [
        {
            "key": a.key,
            "name": a.name,
            "role": a.role,
            "model": a.model,
            "api_base": a.api_base,
            "api_version": a.api_version,
            "provider": a.provider,
            "has_api_key": bool(a.api_key and a.api_key.strip()),
            "mode": "live" if a.is_live else "unconfigured",
            "temperature": a.temperature,
            "max_tokens": a.max_tokens,
            "sequential_thinking": a.sequential_thinking.model_dump(exclude={"prompt_template"}),
            "allowed_mcp_servers": a.allowed_mcp_servers,
            "allowed_skills": a.allowed_skills,
            "card_color": a.card_color,
            "icon": a.icon,
        }
        for a in pool.list_all()
    ]


@router.get("/api/sessions/{session_id}/personas")
async def session_personas(session_id: str):
    """세션에서 실제로 쓰이는 에이전트 페르소나와 잠금 여부."""
    async with get_session_factory()() as db:
        result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
        session_model = result.scalar_one_or_none()
        if session_model is None:
            return JSONResponse({"detail": "session not found"}, status_code=404)
        personas = await effective_personas(db, session_id, get_agent_pool())

    return {
        "session_id": session_id,
        "personas_locked": bool(session_model.personas_locked),
        "agents": [p.model_dump() for p in personas.values()],
    }


@router.get("/api/mcp")
async def mcp_status():
    """MCP 서버별 연결 상태. conf.json 에서 비활성화한 서버도 함께 보고합니다.

    서버는 작업 공간마다 따로 뜹니다. 목록(평평한 모양)은 **기본 작업 공간**의 것이고,
    살아 있는 런타임 전부는 `runtimes` 에 담깁니다.
    """
    cfg = get_config()
    pool = get_runtime_pool()
    status = pool.default_runtime().connection_status()
    servers = [
        {
            "name": name,
            "enabled": server_cfg.enabled,
            "command": server_cfg.command,
            **(
                status.get(name)
                or {"connected": False, "available": False, "tool_count": 0, "error": None}
            ),
        }
        for name, server_cfg in cfg.mcp_servers.items()
    ]
    return {
        "servers": servers,
        "runtimes": pool.status(),
        "max_runtimes": pool.max_runtimes,
        "idle_ttl_seconds": pool.idle_ttl,
    }


@router.get("/api/skills")
async def list_skills():
    """스킬 디렉터리의 현재 상태 목록을 반환합니다. 비활성화된 스킬 및 오류가 발생한 스킬(`problem`) 정보도 함께 반환합니다."""
    skills = await asyncio.to_thread(scan_skills)
    return {
        "dir": str(skills_root()),
        "skills": [
            {
                "name": s.name,
                "title": s.title,
                "description": s.description,
                "enabled": s.enabled,
                "usable": s.usable,
                "problem": s.problem or None,
                "files": list(s.files),
                "scripts": list(s.scripts),
            }
            for s in skills
        ],
    }


# --------------------------------------------------------------------- 화면 없는 토론 (v1.5)


class NewSessionBody(BaseModel):
    title: str = ""
    strategy: str = DEFAULT_STRATEGY
    max_rounds: int = 3
    agents: Optional[List[str]] = Field(default=None, description="참여 에이전트 키. 비우면 켜진 에이전트 전부")
    custom_instructions: str = ""
    workspace_dir: str = ""
    converge_early: bool = True
    token_budget: int = 0


class TurnBody(BaseModel):
    prompt: str
    # 사람이 화면에서 승인 카드에 답할 것인가. 아니면 묻지 않고 "물어볼 사람이 없을 때" 의 규칙을 따릅니다.
    attended: bool = False


def _error(exc: headless.HeadlessError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@router.get("/api/sessions")
async def api_list_sessions(limit: int = 50):
    return {"sessions": await headless.list_sessions(limit)}


@router.post("/api/sessions", status_code=201)
async def api_create_session(body: NewSessionBody):
    try:
        return await headless.create_session(**body.model_dump())
    except headless.HeadlessError as exc:
        return _error(exc)


@router.post("/api/sessions/{session_id}/turns", status_code=202)
async def api_start_turn(session_id: str, body: TurnBody):
    try:
        return await headless.start_turn(session_id, body.prompt, attended=body.attended)
    except headless.HeadlessError as exc:
        return _error(exc)


@router.post("/api/sessions/{session_id}/stop")
async def api_stop_turn(session_id: str):
    return {"session_id": session_id, "stop_requested": headless.stop_turn(session_id)}


@router.get("/api/sessions/{session_id}/status")
async def api_turn_status(session_id: str):
    try:
        return await headless.turn_status(session_id)
    except headless.HeadlessError as exc:
        return _error(exc)


@router.get("/api/sessions/{session_id}/result")
async def api_turn_result(session_id: str, wait: float = 0):
    """마지막 턴의 결과. `wait` 초만큼 진행 중인 턴이 끝나기를 기다린 뒤 답합니다 (최대 600)."""
    try:
        if wait > 0:
            await headless.wait_for_turn(session_id, min(wait, 600.0))
        return await headless.turn_result(session_id)
    except headless.HeadlessError as exc:
        return _error(exc)


@router.get("/api/sessions/{session_id}/events")
async def api_turn_events(session_id: str):
    """진행 중인 턴의 이벤트를 Server-Sent Events 로. 처음에 `snapshot`, 끝에 `run_finished`."""

    async def stream():
        async for event in headless.events(session_id):
            if event is None:
                yield ": keepalive\n\n"
                continue
            payload = json.dumps(event, ensure_ascii=False, default=str)
            yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
