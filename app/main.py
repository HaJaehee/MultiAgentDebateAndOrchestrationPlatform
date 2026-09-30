import asyncio
import contextlib
import logging
import threading
from typing import Any, AsyncGenerator
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from nicegui import app as nicegui_app, ui
import uvicorn
from app.agents.pool import get_agent_pool
from app.about import (
    APP_NAME,
    APP_VERSION,
    APP_VERSION_LABEL,
    AUTHOR,
    AUTHOR_EMAIL,
)
from app.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, get_config, resolve_agent_icon
from app.database.session import get_session_factory, init_db
from app.mcp.manager import get_mcp_manager
from app.mcp.pool import get_runtime_pool
from app.orchestration.runner import get_debate_runner
from app.orchestration.turns import mark_interrupted_turns
from app.trial import setup_trial
from app.ui.app import create_ui
from app.ui.graph_page import create_graph_page
from app.ui.personas_page import create_personas_page

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("multiagent")


def _install_process_guards() -> None:
    """프로세스를 끝낼 수 있는 두 갈래를 로그로 돌려놓습니다.

    토론과 MCP 도구는 백그라운드 태스크와 보조 스레드에서 돕니다. 거기서 아무도
    잡지 않은 예외가 나면, 파이썬은 그것을 알릴 뿐 프로세스를 세우지는 않습니다 —
    다만 아무 흔적 없이 지나가거나(태스크), 콘솔만 어지럽히고(스레드) 정작 무슨
    일이 있었는지는 남지 않습니다. 여기서 로거로 모아 두면 "에이전트가 죽었는데
    이유를 모르겠다" 가 되지 않습니다.

    앱의 다른 부분은 이미 각자 자리에서 실패를 흡수합니다 (도구 호출은
    MCPManager, 발언은 engine._speak, 토론은 DebateRunner). 이건 그 밖으로 새어
    나온 것을 붙잡는 마지막 그물입니다.
    """
    def _on_loop_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        message = context.get("message") or "unhandled exception in the event loop"
        logger.error(
            "Unhandled error in a background task: %s", message,
            exc_info=exc if isinstance(exc, BaseException) else None,
        )

    def _on_thread_exception(args: Any) -> None:
        logger.error(
            "Unhandled error in thread '%s'", getattr(args, "thread", None),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    try:
        asyncio.get_running_loop().set_exception_handler(_on_loop_exception)
    except RuntimeError:  # pragma: no cover - 루프 밖에서 부른 경우
        pass
    threading.excepthook = _on_thread_exception


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI & NiceGUI application lifecycle manager."""
    logger.info("Starting MADO: Multi-Agent Debate & Orchestration Platform...")
    _install_process_guards()

    # 1. Load configuration
    cfg = get_config()
    logger.info(f"Loaded configuration for host={cfg.app.host}:{cfg.app.port}, db={cfg.app.db_url}")
    logger.info(f"Web UI: http://{cfg.app.host}:{cfg.app.port}")

    # 2. Initialize Database Tables
    await init_db(cfg.app.db_url)
    logger.info("SQLite database tables initialized.")

    # 2-b. 서버가 턴 도중에 내려갔다면 그 턴을 "끊김" 으로 적습니다 (ADR-024). 프로세스가
    # 하나뿐이라 지금 "도는 중" 인 턴은 없습니다. 자동으로 다시 돌리지 않습니다 — 그 대화를
    # 연 사람이 이어 가기·결론 내기·버리기를 고릅니다.
    try:
        async with get_session_factory(cfg.app.db_url)() as db:
            await mark_interrupted_turns(db)
    except Exception as exc:  # noqa: BLE001 - 적지 못해도 앱은 떠야 합니다
        logger.error("Could not check for interrupted debate turns: %s: %s", type(exc).__name__, exc)

    # 3. MCP 런타임 준비
    #
    # 기본 작업 공간의 런타임을 미리 띄웁니다. 첫 토론이 서버 기동을 기다리지
    # 않게 하려는 것뿐이고, 다른 폴더를 쓰는 대화는 자기 런타임을 따로 받습니다.
    #
    # 서버 하나가 기동에 실패해도 앱은 떠야 합니다. 도구 없이 토론하는 것과
    # 화면조차 열리지 않는 것은 전혀 다른 이야기입니다 (설정을 고치려면 그
    # 화면이 필요합니다).
    pool = get_runtime_pool()
    try:
        await pool.warm_default()
        logger.info(
            "MCP runtime pool ready (max %d runtimes, idle TTL %.0fs).",
            pool.max_runtimes, pool.idle_ttl,
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        logger.error(
            "The default MCP runtime could not be initialized (%s: %s); starting without MCP tools. "
            "설정 화면에서 서버 설정을 수정한 후 다시 연결하십시오.",
            type(exc).__name__, exc, exc_info=True,
        )

    # 4. Initialize Agent Pool
    agent_pool = get_agent_pool()
    logger.info(f"Agent Pool ready with {len(agent_pool.list_all())} agents.")

    yield

    logger.info("Shutting down MADO: Multi-Agent Debate & Orchestration Platform...")

    # 백그라운드로 돌고 있는 토론 태스크를 먼저 세웁니다.
    #
    # 두 정리 단계는 서로를 막지 않아야 합니다. 앞이 실패했다고 MCP 서버
    # 프로세스를 남겨 두면, 다음 기동 때 같은 작업 공간을 두 프로세스가 붙듭니다.
    try:
        await get_debate_runner().shutdown()
        logger.info("Background debate tasks cancelled.")
    except BaseException as exc:  # noqa: BLE001
        logger.warning("Could not cancel every debate task: %s: %s", type(exc).__name__, exc)

    # 유지 중인 MCP 세션과 서버 프로세스를 **런타임 전부** 정리합니다.
    try:
        await get_runtime_pool().shutdown_all()
        logger.info("MCP sessions closed.")
    except BaseException as exc:  # noqa: BLE001
        logger.warning("Could not close every MCP session: %s: %s", type(exc).__name__, exc)


# 1. Create FastAPI Application
server = FastAPI(
    title=APP_NAME,
    description="MCP-enabled Autonomous Multi-Agent Collaborative Debate & Synthesis Backend",
    version=APP_VERSION,
    lifespan=lifespan,
)


@server.get("/api/health")
async def health_check():
    cfg = get_config()
    pool = get_agent_pool()
    return {
        "status": "healthy",
        "version": APP_VERSION_LABEL,
        "author": {"name": AUTHOR, "email": AUTHOR_EMAIL},
        "app": {"host": cfg.app.host, "port": cfg.app.port, "debug": cfg.app.debug},
        "registered_agents": [a.key for a in pool.list_all()],
    }


# 아이콘 파일을 못 찾았을 때 대신 내려주는 그림. 아바타가 깨진 이미지로 남는
# 것보다, 아무 말 없이 기본 로봇이 서 있는 편이 낫습니다.
FALLBACK_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#94a3b8">'
    '<path d="M20 9V7a2 2 0 0 0-2-2h-3V3.5a1.5 1.5 0 0 0-3 0V5H9a2 2 0 0 0-2 2v2H5.5a1.5 1.5 0 0 0 0 3H7v5'
    'a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-5h1.5a1.5 1.5 0 0 0 0-3H20zm-8.5 2.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0z'
    'm7 0a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM9 16h9v1.5H9V16z"/></svg>'
)


@server.get("/agent-icon")
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


@server.get("/api/agents")
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


@server.get("/api/sessions/{session_id}/personas")
async def session_personas(session_id: str):
    """세션에서 실제로 쓰이는 에이전트 페르소나와 잠금 여부."""
    from sqlalchemy import select

    from app.agents.personas import effective_personas
    from app.agents.pool import get_agent_pool
    from app.database.models import SessionModel
    from app.database.session import get_session_factory

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


@server.get("/api/mcp")
async def mcp_status():
    """MCP 서버별 연결 상태. conf.json 에서 비활성화한 서버도 함께 보고합니다.

    서버는 이제 작업 공간마다 따로 뜹니다. 목록(평평한 모양)은 **기본 작업
    공간**의 것이라 예전 소비자가 그대로 동작하고, 살아 있는 런타임 전부는
    `runtimes` 에 담깁니다.
    """
    cfg = get_config()
    pool = get_runtime_pool()
    status = get_mcp_manager().connection_status()
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


@server.get("/api/skills")
async def list_skills():
    """스킬 폴더의 지금 모습. 꺼진 스킬과 깨진 스킬(`problem`)도 함께 보고합니다."""
    import asyncio

    from app.agents.skills import scan_skills, skills_root

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
            }
            for s in skills
        ],
    }


from app.ui.theme import FAVICON_SVG

# 2. Build NiceGUI Application
create_ui()
create_personas_page()
create_graph_page()
# 체험 서버 (app/trial). 켜져 있지 않으면 화면은 "꺼져 있음" 만 보이고 방문자 통로는 닫혀 있습니다.
# 로그인 폼이 FastAPI 경로라 `ui.run_with` 보다 먼저 붙입니다.
trial_gate = setup_trial(server)
ui.run_with(
    server,
    title=APP_NAME,
    favicon=FAVICON_SVG,
    dark=True,
    # 브라우저가 다시 붙기를 기다리는 시간(기본 3초). 이 안에 붙으면 NiceGUI 가 끊긴
    # 사이의 갱신을 이어 보내고, 넘기면 서버가 그 화면을 지워 브라우저가 **페이지를
    # 새로고침**합니다. 긴 스트리밍으로 서버나 브라우저가 몇 초 바쁘거나, 모니터를 끄고
    # 절전에서 깨어나는 정도로 대화 화면이 통째로 다시 그려지면 안 됩니다. 비용은 닫힌
    # 탭의 화면 정보를 30초 더 들고 있는 정도입니다.
    reconnect_timeout=30.0,
)

# 3. 원격 접속 토큰 (app/security.py)
#
# 맨 바깥에 붙입니다. NiceGUI 와 그 socket.io 가 `server` 안에 붙어 있어, 페이지·웹소켓·
# /api/*·다운로드가 모두 이 계층을 지납니다. Starlette 는 마지막에 더한 미들웨어를 가장
# 바깥에 두므로 반드시 `ui.run_with` 뒤에 둡니다.
from app.security import AccessMiddleware, get_access_control

get_access_control()
server.add_middleware(AccessMiddleware, guest_gate=trial_gate)


def start():
    import argparse
    parser = argparse.ArgumentParser(description="MADO: Multi-Agent Debate & Orchestration Platform")
    parser.add_argument("--host", type=str, default=None, help="Host to bind (overrides conf.json and .env)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (overrides conf.json and .env)")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_PATH, help="Path to config file")
    parser.add_argument("--reload", action="store_true", default=None, help="Enable auto-reload")
    parser.add_argument("--no-reload", action="store_true", default=False, help="Disable auto-reload")
    args, _ = parser.parse_known_args()

    cfg = get_config(config_path=args.config)
    host = args.host if args.host is not None else cfg.app.host
    port = args.port if args.port is not None else cfg.app.port

    # Keep cfg.app in sync with actual bound host/port
    cfg.app.host = host
    cfg.app.port = port

    do_reload = cfg.app.debug
    if args.no_reload:
        do_reload = False
    elif args.reload:
        do_reload = True

    uvicorn.run(
        "app.main:server",
        host=host,
        port=port,
        reload=do_reload,
        reload_dirs=["app"] if do_reload else None,
        reload_excludes=["workspace", "workspace/*", "*.db*", "*.db-wal", "*.db-shm", "mcp_sandbox/*", ".git/*"] if do_reload else None,
    )


if __name__ == "__main__":
    start()

