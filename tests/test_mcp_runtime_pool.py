"""작업 공간별 MCP 런타임 풀.

여기서 확인하는 것은 **프로세스 묶음의 소유권**입니다. 누가 빌렸고, 언제 놓았고,
자리가 모자랄 때 무엇을 내리고 무엇을 거절하는가.

서버를 실제로 띄우지는 않습니다 (`tests/conftest.py` 의 대역). 그래도 참조 카운트,
유휴 TTL, 상한, 회수는 전부 진짜 코드가 돕니다 — 이 파일이 보는 것이 그것입니다.
"""

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.database.models import SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.events import EventType
from app.mcp import pool as pool_module
from app.mcp.pool import MCPRuntimePool, RuntimeCapacityError, RuntimeWaitAborted, get_runtime_pool
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from app.orchestration.turn_run import TurnRun
from app.ui.expert.events import ExpertEventView
from tests.fake_llm import FakeLLMCaller


def _pool(**kwargs) -> MCPRuntimePool:
    """대역이 걸린 풀 하나. `conftest` 가 `_start_locked` 를 이미 바꿔 둡니다."""
    return MCPRuntimePool(**kwargs)


async def _until(condition, timeout: float = 3.0) -> None:
    """조건이 참이 될 때까지 기다립니다. 시간이 다 되면 실패합니다."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "조건이 끝내 참이 되지 않았습니다"
        await asyncio.sleep(0.005)


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
    pool = _pool(max_runtimes=1, idle_ttl=60.0, wait_timeout=0)
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


# ------------------------------------------------------------------ 줄 서기


@pytest.mark.asyncio
async def test_a_full_pool_makes_the_next_turn_wait_instead_of_failing(tmp_path):
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="a")
    told = []

    async def on_wait(ahead):
        told.append(ahead)

    waiting = asyncio.create_task(pool.acquire(tmp_path / "ws-b", holder="b", on_wait=on_wait))
    await _until(lambda: told)
    assert not waiting.done()
    assert [w["holder"] for w in pool.waiting()] == ["b"]
    assert told == [0]

    await pool.release("a")
    manager = await asyncio.wait_for(waiting, 3)

    assert manager.workspace == (tmp_path / "ws-b").resolve()
    assert pool.live_workspaces() == [pool.key_for(tmp_path / "ws-b")]
    assert pool.waiting() == []


@pytest.mark.asyncio
async def test_the_line_is_first_come_first_served(tmp_path):
    """늦게 온 턴이 빈자리를 먼저 가져가지 않고, 순번이 당겨지면 알려 줍니다."""
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="a")
    told = []

    async def on_wait(ahead):
        told.append(ahead)

    first = asyncio.create_task(pool.acquire(tmp_path / "ws-b", holder="b"))
    await _until(pool.waiting)
    second = asyncio.create_task(pool.acquire(tmp_path / "ws-c", holder="c", on_wait=on_wait))
    await _until(lambda: told == [1])

    await pool.release("a")
    await asyncio.wait_for(first, 3)
    # 빈자리가 난 바로 그때 온 턴도 줄 뒤로 갑니다.
    await pool.release("b")
    late = asyncio.create_task(pool.acquire(tmp_path / "ws-d", holder="d"))
    await asyncio.wait_for(second, 3)

    assert told == [1, 0]
    assert pool.live_workspaces() == [pool.key_for(tmp_path / "ws-c")]
    assert [w["holder"] for w in pool.waiting()] == ["d"]
    late.cancel()


@pytest.mark.asyncio
async def test_a_live_workspace_never_queues(tmp_path):
    """떠 있는 폴더는 자리를 새로 요구하지 않으므로 줄이 있어도 바로 함께 씁니다."""
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    first = await pool.acquire(tmp_path / "ws-a", holder="a")
    waiting = asyncio.create_task(pool.acquire(tmp_path / "ws-b", holder="b"))
    await _until(pool.waiting)

    assert await asyncio.wait_for(pool.acquire(tmp_path / "ws-a", holder="a2"), 3) is first
    waiting.cancel()


@pytest.mark.asyncio
async def test_waiting_too_long_gives_up_and_names_the_holders(tmp_path):
    pool = _pool(max_runtimes=1, idle_ttl=60.0, wait_timeout=0.05)
    await pool.acquire(tmp_path / "ws-a", holder="session-a")

    with pytest.raises(RuntimeCapacityError) as excinfo:
        await pool.acquire(tmp_path / "ws-b", holder="session-b")

    assert "session-a" in str(excinfo.value) and "MCP_RUNTIME_WAIT" in str(excinfo.value)
    assert pool.waiting() == []


@pytest.mark.asyncio
async def test_leaving_the_line_lets_the_next_one_through(tmp_path):
    """맨 앞이 취소돼도 줄이 막히지 않습니다."""
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="a")
    head = asyncio.create_task(pool.acquire(tmp_path / "ws-b", holder="b"))
    await _until(pool.waiting)
    tail = asyncio.create_task(pool.acquire(tmp_path / "ws-c", holder="c"))
    await _until(lambda: len(pool.waiting()) == 2)

    head.cancel()
    await _until(lambda: len(pool.waiting()) == 1)
    await pool.release("a")

    await asyncio.wait_for(tail, 3)
    assert pool.live_workspaces() == [pool.key_for(tmp_path / "ws-c")]


@pytest.mark.asyncio
async def test_a_stop_request_leaves_the_line(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_module, "ABORT_POLL_SECONDS", 0.01)
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    await pool.acquire(tmp_path / "ws-a", holder="a")
    stop = []
    waiting = asyncio.create_task(pool.acquire(tmp_path / "ws-b", holder="b", should_abort=lambda: bool(stop)))
    await _until(pool.waiting)

    stop.append(True)

    with pytest.raises(RuntimeWaitAborted):
        await asyncio.wait_for(waiting, 3)
    assert pool.waiting() == []


# ------------------------------------------------------------------ 라운드 경계의 양보


async def _with_overdue_waiter(tmp_path, **kwargs):
    """ws-a 를 쓰는 턴과, 충분히 기다린 ws-b 턴이 있는 풀."""
    pool = _pool(max_runtimes=1, idle_ttl=60.0, **kwargs)
    manager = await pool.acquire(tmp_path / "ws-a", holder="a")
    waiting = asyncio.create_task(pool.acquire(tmp_path / "ws-b", holder="b"))
    await _until(pool.waiting)
    pool._waiters[0].since -= 100  # noqa: SLF001 - 오래 기다린 것으로 칩니다
    pool._runtimes[pool.key_for(tmp_path / "ws-a")].busy_since -= 100  # noqa: SLF001
    return pool, manager, waiting


@pytest.mark.asyncio
async def test_a_turn_gives_way_at_a_round_boundary_and_gets_the_same_manager_back(tmp_path):
    """자문 범위처럼 매니저를 들고 있는 쪽이 있으므로, 돌아온 뒤에도 같은 객체여야 합니다."""
    pool, manager, waiting = await _with_overdue_waiter(tmp_path, yield_after=50.0)

    giving_way = asyncio.create_task(pool.yield_slot(tmp_path / "ws-a", "a"))
    await asyncio.wait_for(waiting, 3)

    assert pool.get(tmp_path / "ws-a") is None
    assert not giving_way.done()
    assert [w["holder"] for w in pool.waiting()] == ["a"], "자리를 내준 턴은 줄 맨 뒤에 섭니다"

    await pool.release("b")

    assert await asyncio.wait_for(giving_way, 3) is True
    assert pool.get(tmp_path / "ws-a") is manager
    assert pool.status()[pool.key_for(tmp_path / "ws-a")]["holders"] == ["a"]


@pytest.mark.asyncio
async def test_giving_way_waits_as_long_as_it_takes(tmp_path):
    """이미 시작한 턴은 자리 때문에 실패로 끝나지 않습니다 (`wait_timeout` 을 적용하지 않습니다)."""
    pool, _manager, waiting = await _with_overdue_waiter(tmp_path, yield_after=50.0, wait_timeout=1000.0)
    pool.wait_timeout = 0.01

    giving_way = asyncio.create_task(pool.yield_slot(tmp_path / "ws-a", "a"))
    await asyncio.wait_for(waiting, 3)
    await asyncio.sleep(0.05)

    assert not giving_way.done()
    await pool.release("b")
    assert await asyncio.wait_for(giving_way, 3) is True


@pytest.mark.asyncio
async def test_no_one_waiting_means_no_yield(tmp_path):
    pool = _pool(max_runtimes=1, idle_ttl=60.0, yield_after=0.01)
    manager = await pool.acquire(tmp_path / "ws-a", holder="a")
    await asyncio.sleep(0.02)

    assert await pool.yield_slot(tmp_path / "ws-a", "a") is False
    assert pool.get(tmp_path / "ws-a") is manager


@pytest.mark.asyncio
async def test_a_short_wait_does_not_cost_anyone_their_kernels(tmp_path):
    """막 줄을 선 턴 때문에 라운드마다 서버를 다시 띄우지 않습니다."""
    pool, manager, waiting = await _with_overdue_waiter(tmp_path, yield_after=50.0)
    pool._waiters[0].since += 100  # noqa: SLF001 - 방금 줄을 선 것으로 되돌립니다

    assert await pool.yield_slot(tmp_path / "ws-a", "a") is False
    assert pool.get(tmp_path / "ws-a") is manager
    waiting.cancel()


@pytest.mark.asyncio
async def test_a_turn_that_just_got_the_slot_keeps_it_for_a_while(tmp_path):
    pool, manager, waiting = await _with_overdue_waiter(tmp_path, yield_after=50.0)
    pool._runtimes[pool.key_for(tmp_path / "ws-a")].busy_since += 100  # noqa: SLF001

    assert await pool.yield_slot(tmp_path / "ws-a", "a") is False
    waiting.cancel()


@pytest.mark.asyncio
async def test_a_shared_runtime_does_not_yield(tmp_path):
    """같은 폴더의 다른 턴은 지금 라운드 경계에 있지 않습니다. 내리면 도구가 도중에 사라집니다."""
    pool, manager, waiting = await _with_overdue_waiter(tmp_path, yield_after=50.0)
    await pool.acquire(tmp_path / "ws-a", holder="a2")

    assert await pool.yield_slot(tmp_path / "ws-a", "a") is False
    assert pool.get(tmp_path / "ws-a") is manager
    waiting.cancel()


@pytest.mark.asyncio
async def test_yielding_can_be_turned_off(tmp_path):
    pool, manager, waiting = await _with_overdue_waiter(tmp_path, yield_after=0)

    assert await pool.yield_slot(tmp_path / "ws-a", "a") is False
    assert pool.get(tmp_path / "ws-a") is manager
    waiting.cancel()


# ------------------------------------------------------------------ 엔진과 함께


async def _session_in(workspace: Path, max_rounds: int = 1) -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    sid = f"queue-{uuid.uuid4().hex[:8]}"
    async with get_session_factory("sqlite+aiosqlite:///:memory:")() as db:
        db.add(SessionModel(
            id=sid, title="줄", strategy="sequential_debate", max_rounds=max_rounds,
            active_agents=["orchestrator", "architect", "coder", "critic"], workspace_dir=str(workspace),
        ))
        await db.commit()
    return sid


class _Blocking(FakeLLMCaller):
    """첫 호출을 `gate` 가 열릴 때까지 붙들고, 호출마다 받은 MCP 런타임을 기록합니다."""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.started = asyncio.Event()
        self.runtimes = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.runtimes.append(kwargs.get("mcp"))
        self.started.set()
        await self.gate.wait()
        return await super().call_agent(agent, messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_two_turns_take_turns_on_one_slot(tmp_path, monkeypatch):
    """자리가 하나뿐이어도 두 토론이 모두 끝나고, 어느 발언도 남의 폴더 도구를 받지 않습니다."""
    monkeypatch.setattr(pool_module, "_pool", _pool(max_runtimes=1, idle_ttl=60.0, yield_after=0.01))
    ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
    sid_a, sid_b = await _session_in(ws_a, max_rounds=2), await _session_in(ws_b)
    gate = asyncio.Event()
    caller_a = _Blocking(gate)
    events_a, events_b = [], []

    async def on_a(event):
        events_a.append(event)

    async def on_b(event):
        events_b.append(event)

    turn_a = asyncio.create_task(OrchestratorEngine(llm_caller=caller_a).run_turn(
        session_id=sid_a, user_prompt="A", on_event=on_a,
    ))
    await asyncio.wait_for(caller_a.started.wait(), 5)
    turn_b = asyncio.create_task(OrchestratorEngine(llm_caller=FakeLLMCaller()).run_turn(
        session_id=sid_b, user_prompt="B", on_event=on_b,
    ))
    await _until(lambda: events_b)
    assert events_b[0] == {"type": EventType.RUNTIME_QUEUED, "ahead": 0}
    await asyncio.sleep(0.02)  # 줄의 맨 앞이 양보를 받을 만큼 기다립니다

    gate.set()
    await asyncio.wait_for(asyncio.gather(turn_a, turn_b), 15)

    types_a = [e["type"] for e in events_a]
    assert EventType.RUNTIME_QUEUED in types_a, "A 가 라운드 경계에서 자리를 내주지 않았습니다"
    assert types_a.index(EventType.RUNTIME_QUEUED) < types_a.index(EventType.TURN_COMPLETED)
    assert {runtime.workspace for runtime in caller_a.runtimes} == {ws_a.resolve()}
    assert len({id(runtime) for runtime in caller_a.runtimes}) == 1


@pytest.mark.asyncio
async def test_stopping_a_queued_turn_starts_nothing(tmp_path, monkeypatch):
    """줄에 선 턴을 정지하면 취소로 끝납니다. 요청은 기록되지 않았으므로 턴도 남지 않습니다."""
    monkeypatch.setattr(pool_module, "ABORT_POLL_SECONDS", 0.01)
    pool = _pool(max_runtimes=1, idle_ttl=60.0)
    monkeypatch.setattr(pool_module, "_pool", pool)
    await pool.acquire(tmp_path / "ws-a", holder="someone")
    sid = await _session_in(tmp_path / "ws-b")
    runner = DebateRunner(OrchestratorEngine(llm_caller=FakeLLMCaller()))

    run = runner.start(sid, "요청")
    await _until(pool.waiting)
    assert "자리를 기다리는 중" in run.status_text and run.round_info == "Queue"
    assert run.turn_id is None
    assert runner.request_stop(sid)
    await _until(lambda: run.status != "running")

    assert run.status == "cancelled" and "시작하지 않았습니다" in run.error
    async with get_session_factory("sqlite+aiosqlite:///:memory:")() as db:
        assert (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).first() is None


def test_the_queue_shows_up_in_the_run_snapshot():
    run = TurnRun("s", "요청", None)
    run.apply({"type": EventType.RUNTIME_QUEUED, "ahead": 2})
    assert "앞에 2개 대기" in run.status_text and run.busy


def _expert_view(run: TurnRun):
    page = SimpleNamespace(
        chat_feed=MagicMock(), roster_control=MagicMock(), sidebar=SimpleNamespace(refresh_list=AsyncMock()),
        runner=SimpleNamespace(get=lambda session_id: run), current_session_id=run.session_id,
    )
    return ExpertEventView(page), page.chat_feed


@pytest.mark.asyncio
async def test_a_run_that_never_opened_a_turn_returns_its_request_to_the_input():
    """줄에서 나온 턴의 요청은 어디에도 기록되지 않았습니다. 돌려주지 않으면 글이 사라집니다."""
    run = TurnRun("s", "요청", None)
    view, feed = _expert_view(run)

    await view.apply({"type": EventType.RUN_FINISHED, "status": "cancelled"})

    feed.restore_input.assert_called_once_with("요청")


@pytest.mark.asyncio
async def test_a_run_with_an_opened_turn_keeps_its_request_in_the_record():
    run = TurnRun("s", "요청", None)
    run.turn_id = "t"
    view, feed = _expert_view(run)

    await view.apply({"type": EventType.RUN_FINISHED, "status": "cancelled"})

    feed.restore_input.assert_not_called()


@pytest.mark.asyncio
async def test_stopping_in_line_does_not_promise_a_synthesis():
    run = TurnRun("s", "요청", None)
    run.apply({"type": EventType.RUNTIME_QUEUED, "ahead": 0})
    view, feed = _expert_view(run)

    await view.apply({"type": EventType.STOP_REQUESTED})

    assert "대기를 취소" in feed.set_busy.call_args.args[1]


# ------------------------------------------------------------------ 쉬는 작업 공간


def _recording_hook(pool: MCPRuntimePool) -> list:
    seen: list = []

    async def hook(workspace):
        seen.append(workspace)

    pool.idle_hook = hook
    return seen


@pytest.mark.asyncio
async def test_the_idle_hook_runs_once_the_workspace_has_rested(tmp_path):
    """반납이 더 오지 않는 서버에서도 시간이 되면 불립니다. 런타임은 그대로 둡니다."""
    pool = _pool(idle_ttl=0.01)
    seen = _recording_hook(pool)
    await pool.acquire(tmp_path / "ws", holder="a")
    await pool.release("a")

    await _until(lambda: seen)
    assert seen == [Path(pool.key_for(tmp_path / "ws"))]


@pytest.mark.asyncio
async def test_using_the_workspace_again_cancels_the_idle_hook(tmp_path):
    pool = _pool(idle_ttl=0.05)
    seen = _recording_hook(pool)
    await pool.acquire(tmp_path / "ws", holder="a")
    await pool.release("a")
    await pool.acquire(tmp_path / "ws", holder="a")

    await asyncio.sleep(0.1)
    assert seen == []


@pytest.mark.asyncio
async def test_the_idle_hook_waits_for_the_last_holder(tmp_path):
    pool = _pool(idle_ttl=0.01)
    seen = _recording_hook(pool)
    await pool.acquire(tmp_path / "ws", holder="a")
    await pool.acquire(tmp_path / "ws", holder="b")
    await pool.release("a")

    await asyncio.sleep(0.05)
    assert seen == []

    await pool.release("b")
    await _until(lambda: seen)


@pytest.mark.asyncio
async def test_a_failing_idle_hook_is_only_a_warning(tmp_path, caplog):
    pool = _pool(idle_ttl=0.01)
    called = []

    async def hook(workspace):
        called.append(workspace)
        raise ValueError("정리 실패")

    pool.idle_hook = hook
    await pool.acquire(tmp_path / "ws", holder="a")
    await pool.release("a")

    await _until(lambda: called)
    await _until(lambda: "정리 실패" in caplog.text)
    assert pool.live_workspaces() == [pool.key_for(tmp_path / "ws")]


@pytest.mark.asyncio
async def test_shutdown_stops_running_maintenance(tmp_path):
    pool = _pool(idle_ttl=0.01)
    running, stopped = asyncio.Event(), []

    async def hook(workspace):
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append(workspace)
            raise

    pool.idle_hook = hook
    await pool.acquire(tmp_path / "ws", holder="a")
    await pool.release("a")
    await asyncio.wait_for(running.wait(), 3)

    await asyncio.wait_for(pool.shutdown_all(), 3)

    assert stopped and pool.live_workspaces() == []


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
