"""앱 조립: FastAPI 서버 하나에 HTTP 엔드포인트, NiceGUI 화면, 체험·쉬운 화면, 접근 제어를 붙입니다.

프로세스 전체에 하나씩인 것들(설정, DB 엔진, MCP 런타임 풀, 에이전트 풀, 엔진, 토론 실행기)은 각자의
`get_*()` 가 처음 불릴 때 만들어 들고 있습니다. 시작 순서는 `lifespan` 한 곳에서 정합니다 —
설정 → DB(→ 끊긴 턴 표시) → 기본 작업 공간의 MCP 런타임 → 에이전트 풀. 끝낼 때는 토론 실행기,
MCP 런타임 순서로 정리합니다. DB 엔진은 처음 부른 쪽의 주소로 고정되므로, 밖에서 이 앱을 띄우는
스크립트는 엔진을 만들기 전에 `get_config(config_path=…)` 와 `init_db(…)`(또는 `get_engine(…)`)를
먼저 부릅니다.
"""

import asyncio
import contextlib
import logging
import threading
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import FastAPI
from nicegui import ui

from app.about import APP_NAME, APP_VERSION
from app.agents.pool import get_agent_pool
from app import mcp_server
from app.api import router as api_router
from app.config import DEFAULT_CONFIG_PATH, get_config
from app.database.session import get_session_factory, init_db
from app.diagnostics import start_loop_watchdog, stop_loop_watchdog
from app.easy import setup_easy
from app.mcp.pool import get_runtime_pool
from app.orchestration.runner import get_debate_runner
from app.orchestration.turns import mark_interrupted_turns
from app.security import AccessMiddleware, get_access_control
from app.trial import setup_trial
from app.ui.expert.page import create_ui
from app.ui.compare_page import create_compare_page
from app.ui.graph_page import create_graph_page
from app.ui.personas_page import create_personas_page
from app.ui.theme import FAVICON_SVG

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
    MCPManager, 발언은 SpeechRunner.speak, 토론은 DebateRunner). 이건 그 밖으로 새어
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
    # 이벤트 루프가 붙잡히면 그 순간의 스택을 남깁니다 (data/diagnostics/stalls.log).
    start_loop_watchdog()

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

    # 5. MADO MCP 서버 활성화 (`/mcp`, `app/mcp_server.py`). SSE 기반 스트리밍 HTTP 세션 관리자가 함께 구동됩니다.
    exits = contextlib.AsyncExitStack()
    await exits.enter_async_context(mcp_server.running())

    yield

    await exits.aclose()

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

    await stop_loop_watchdog()


# 1. Create FastAPI Application
server = FastAPI(
    title=APP_NAME,
    description="MCP-enabled Autonomous Multi-Agent Collaborative Debate & Synthesis Backend",
    version=APP_VERSION,
    lifespan=lifespan,
)


# 2. HTTP 엔드포인트 (`/api/*`, `/agent-icon`)와 MCP 서버 (`/mcp`)
server.include_router(api_router)
mcp_server.mount(server)


# 3. Build NiceGUI Application
create_ui()
create_personas_page()
create_graph_page()
create_compare_page()
# 체험 서버 (app/trial). 켜져 있지 않으면 화면은 "꺼져 있음" 만 보이고 방문자 통로는 닫혀 있습니다.
# 로그인 폼이 FastAPI 경로라 `ui.run_with` 보다 먼저 붙입니다.
trial_gate = setup_trial(server)
# 비엔지니어 화면 (app/easy, `/trial/easy`). 체험 서버의 방문자 통로와 로그인을 그대로 씁니다.
setup_easy()
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

# 4. 원격 접속 토큰 (app/security.py)
#
# 맨 바깥에 붙입니다. NiceGUI 와 그 socket.io 가 `server` 안에 붙어 있어, 페이지·웹소켓·
# /api/*·다운로드가 모두 이 계층을 지납니다. Starlette 는 마지막에 더한 미들웨어를 가장
# 바깥에 두므로 반드시 `ui.run_with` 뒤에 둡니다.
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

