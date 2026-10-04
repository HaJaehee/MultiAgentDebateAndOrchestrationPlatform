"""테스트 전역 설정.

## MCP 런타임을 실제로 띄우지 않습니다

`OrchestratorEngine.run_turn` 은 이제 그 대화의 작업 공간에 해당하는 MCP 런타임을
빌리고 시작합니다(`app/mcp/pool.py`). 그대로 두면 토론을 도는 테스트마다 node·
python 서버 프로세스가 실제로 떠서, 개발 PC 에 무엇이 설치되어 있는지에 결과가
좌우되고 실행 시간도 그만큼 늘어납니다.

그래서 여기서는 **런타임을 띄우는 부분만** 대역으로 바꿉니다. 참조 카운트, 유휴
TTL, 상한, 회수 같은 풀의 나머지 동작은 진짜 코드가 그대로 돕니다 — 그 부분이야
말로 테스트가 확인해야 하는 것이기 때문입니다.

서버를 진짜로 띄워야 하는 테스트는 `real_mcp_runtimes` 픽스처를 받으면 됩니다.
"""

import pytest

from app.mcp import pool as pool_module
from app.mcp.manager import MCPManager


async def _start_without_processes(self, key):
    """서버 설정이 비어 있는 런타임을 등록합니다 (프로세스를 띄우지 않음)."""
    from pathlib import Path

    workspace = Path(key)
    manager = MCPManager({}, workspace=workspace)
    manager.set_runtime_env(self.runtime_env(workspace))
    runtime = pool_module._Runtime(workspace, manager)  # noqa: SLF001 - 테스트 대역
    self._runtimes[key] = runtime  # noqa: SLF001
    return runtime


@pytest.fixture(autouse=True)
def _no_real_mcp_processes(request, monkeypatch):
    if "real_mcp_runtimes" in request.fixturenames:
        return
    monkeypatch.setattr(
        pool_module.MCPRuntimePool, "_start_locked", _start_without_processes, raising=True
    )


@pytest.fixture
def real_mcp_runtimes():
    """MCP 서버를 실제로 띄우는 테스트가 받는 표식 (위 autouse 대역을 끕니다)."""
    return True


@pytest.fixture(autouse=True)
def _no_plan_approval(request, monkeypatch):
    """계획 승인(ADR-028)을 끈 채로 돕니다.

    승인은 기본으로 켜져 있고, 러너로 도는 테스트에는 답할 사람이 없습니다. 그대로 두면 그런
    테스트가 전부 승인 카드 앞에서 대기 시간(기본 10분)만큼 멈춥니다. 승인을 확인하는
    테스트는 `plan_approval` 픽스처를 받아 켭니다.
    """
    if "plan_approval" in request.fixturenames:
        return
    from app.config import PlanApprovalConfig
    from app.orchestration import engine as engine_module

    monkeypatch.setattr(
        engine_module, "plan_approval_settings", lambda: PlanApprovalConfig(enabled=False)
    )


@pytest.fixture
def plan_approval(monkeypatch):
    """계획 승인을 켠 테스트가 받는 설정. 값을 고치면 그 테스트의 다음 계획부터 걸립니다."""
    from app.config import PlanApprovalConfig
    from app.orchestration import engine as engine_module

    settings = PlanApprovalConfig(enabled=True, timeout=30)
    monkeypatch.setattr(engine_module, "plan_approval_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _never_touch_the_real_database():
    """DB 엔진이 아직 없으면 메모리 DB 로 먼저 만들어 둡니다.

    엔진은 프로세스에 하나뿐인 싱글턴이고, **처음 만든 쪽의 주소**로 고정됩니다.
    `OrchestratorEngine()` 이나 `get_session_factory()` 는 주소를 주지 않으면 기본값
    `./multiagent.db` 를 씁니다. 그래서 테스트가 `init_db(":memory:")` 보다 엔진을 먼저
    만들면(또는 앞 테스트가 싱글턴을 비워 두고 끝나면) 그 뒤의 모든 테스트가 **개발자의
    실제 DB** 에 세션을 쓰고 있었습니다 (`resilience-*`, `verify-*`, `memory-*` 세션).
    """
    from app.database import session as db_session

    if db_session._engine is None:  # noqa: SLF001
        db_session.get_engine("sqlite+aiosqlite:///:memory:")
    yield


@pytest.fixture(autouse=True)
def _fresh_runtime_pool():
    """테스트마다 빈 풀에서 시작합니다.

    풀은 프로세스 전역 싱글턴이라, 앞 테스트가 남긴 런타임이 뒤 테스트의 상한
    계산과 유휴 회수에 그대로 섞입니다.
    """
    pool_module._pool = None  # noqa: SLF001
    yield
    pool_module._pool = None  # noqa: SLF001
