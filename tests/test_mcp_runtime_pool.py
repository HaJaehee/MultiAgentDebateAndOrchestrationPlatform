"""작업 공간별 MCP 런타임 풀.

여기서 확인하는 것은 **프로세스 묶음의 소유권**입니다. 누가 빌렸고, 언제 놓았고,
자리가 모자랄 때 무엇을 내리고 무엇을 거절하는가.

서버를 실제로 띄우지는 않습니다 (`tests/conftest.py` 의 대역). 그래도 참조 카운트,
유휴 TTL, 상한, 회수는 전부 진짜 코드가 돕니다 — 이 파일이 보는 것이 그것입니다.
"""

import asyncio

import pytest

from app.mcp.pool import MCPRuntimePool, RuntimeCapacityError, get_runtime_pool


def _pool(**kwargs) -> MCPRuntimePool:
    """대역이 걸린 풀 하나. `conftest` 가 `_start_locked` 를 이미 바꿔 둡니다."""
    return MCPRuntimePool(**kwargs)


# ------------------------------------------------------------------ 빌리고 놓기


@pytest.mark.asyncio
async def test_the_same_workspace_is_one_runtime(tmp_path):
    """같은 폴더를 쓰는 대화는 서버 묶음을 함께 씁니다.

    파일을 이미 공유하는 사이라 프로세스를 따로 띄워도 격리되는 것이 없고,
    프로세스 수만 대화 수만큼 늘어납니다. 그 안에서의 대화 단위 격리는 요청
    메타데이터(`_meta.conversationId`)가 합니다.
    """
    pool = _pool()
    a = await pool.acquire(tmp_path / "ws", holder="session-a")
    b = await pool.acquire(tmp_path / "ws", holder="session-b")

    assert a is b
    assert pool.status()[pool.key_for(tmp_path / "ws")]["holders"] == ["session-a", "session-b"]


@pytest.mark.asyncio
async def test_different_workspaces_are_different_runtimes(tmp_path):
    pool = _pool()
    a = await pool.acquire(tmp_path / "ws-a", holder="session-a")
    b = await pool.acquire(tmp_path / "ws-b", holder="session-b")

    assert a is not b
    assert a.workspace != b.workspace
    assert len(pool.live_workspaces()) == 2


@pytest.mark.asyncio
async def test_releasing_keeps_the_runtime_warm(tmp_path):
    """참조가 0 이 되어도 바로 죽이지 않습니다.

    같은 폴더에서 턴을 연달아 돌릴 때 기동 비용(샌드박스는 IPython 커널을 띄워
    수 초)을 매번 치르지 않기 위해서입니다.
    """
    pool = _pool(idle_ttl=60.0)
    first = await pool.acquire(tmp_path / "ws", holder="session-a")
    await pool.release("session-a")

    assert pool.live_workspaces() == [pool.key_for(tmp_path / "ws")]
    assert pool.status()[pool.key_for(tmp_path / "ws")]["holders"] == []

    second = await pool.acquire(tmp_path / "ws", holder="session-a")
    assert second is first, "따뜻한 런타임을 두고 새로 띄웠습니다"


@pytest.mark.asyncio
async def test_an_expired_idle_runtime_is_reaped(tmp_path):
    pool = _pool(idle_ttl=0.01)
    await pool.acquire(tmp_path / "ws", holder="session-a")
    await pool.release("session-a")
    await asyncio.sleep(0.05)

    # 다음 반납·획득에서 정리됩니다 (별도의 타이머를 돌리지 않습니다).
    await pool.release("nobody")
    assert pool.live_workspaces() == []


@pytest.mark.asyncio
async def test_release_without_a_workspace_drops_everything_that_holder_had(tmp_path):
    """턴이 어떻게 끝나든 반납은 일어나야 하므로, 폴더를 몰라도 놓을 수 있습니다."""
    pool = _pool(idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="session-a")
    await pool.acquire(tmp_path / "ws-b", holder="session-a")

    await pool.release("session-a")

    for key in pool.live_workspaces():
        assert pool.status()[key]["holders"] == []


@pytest.mark.asyncio
async def test_releasing_an_unknown_holder_is_quiet(tmp_path):
    pool = _pool()
    await pool.acquire(tmp_path / "ws", holder="session-a")
    await pool.release("someone-else")  # 예외가 나면 안 됩니다
    assert pool.status()[pool.key_for(tmp_path / "ws")]["holders"] == ["session-a"]


# ------------------------------------------------------------------ 자리 다툼


@pytest.mark.asyncio
async def test_an_idle_runtime_makes_room_for_a_new_one(tmp_path):
    """자리가 모자라면 가장 오래 놀고 있던 것부터 내립니다."""
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="session-a")
    await pool.release("session-a")

    await pool.acquire(tmp_path / "ws-b", holder="session-b")

    assert pool.live_workspaces() == [pool.key_for(tmp_path / "ws-b")]


@pytest.mark.asyncio
async def test_a_busy_runtime_is_never_evicted(tmp_path):
    """쓰고 있는 런타임을 내리면 그 토론의 도구가 도중에 사라집니다.

    조용히 틀리느니 거절합니다 — 예전 `WorkspaceConflictError` 가 하던 역할이
    여기로 옮겨 왔습니다. 다만 이유가 "동시에 두 폴더를 쓸 수 없어서" 가 아니라
    "지금 그만큼 띄울 예산이 없어서" 로 바뀌었습니다.
    """
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="session-a")

    with pytest.raises(RuntimeCapacityError) as excinfo:
        await pool.acquire(tmp_path / "ws-b", holder="session-b")

    message = str(excinfo.value)
    # 사람이 무엇을 기다려야 하는지 알 수 있어야 합니다.
    assert "session-a" in message
    assert str(tmp_path / "ws-a") in message
    assert "MCP_MAX_RUNTIMES" in message

    # 앞선 토론은 멀쩡히 살아 있어야 합니다.
    assert pool.get(tmp_path / "ws-a") is not None


@pytest.mark.asyncio
async def test_the_holder_of_a_full_pool_can_still_reacquire(tmp_path):
    """이미 빌린 폴더를 다시 잡는 것은 자리를 새로 요구하지 않습니다."""
    pool = _pool(max_runtimes=1)
    first = await pool.acquire(tmp_path / "ws", holder="session-a")
    again = await pool.acquire(tmp_path / "ws", holder="session-b")
    assert again is first


# ------------------------------------------------------------------ 조회


@pytest.mark.asyncio
async def test_get_never_starts_a_runtime(tmp_path):
    """화면을 그리는 것만으로 서버가 뜨면 안 됩니다.

    사이드바를 클릭할 때마다 프로세스 묶음이 하나씩 늘어납니다.
    """
    pool = _pool()
    assert pool.get(tmp_path / "ws") is None
    assert pool.live_workspaces() == []


@pytest.mark.asyncio
async def test_default_runtime_is_not_started_either(tmp_path):
    """기본 런타임도 만들어 두기만 하고 띄우지는 않습니다.

    소유자 없이 뜬 프로세스는 아무도 반납하지 않아서 종료 때까지 남습니다.
    """
    pool = _pool()
    manager = pool.default_runtime()
    assert manager is not None
    assert manager.is_initialized is False
    assert pool.default_runtime() is manager


@pytest.mark.asyncio
async def test_shutdown_stops_every_runtime(tmp_path):
    pool = _pool()
    await pool.acquire(tmp_path / "ws-a", holder="a")
    await pool.acquire(tmp_path / "ws-b", holder="b")

    await pool.shutdown_all()

    assert pool.live_workspaces() == []


def test_the_process_wide_pool_is_one_object():
    assert get_runtime_pool() is get_runtime_pool()
