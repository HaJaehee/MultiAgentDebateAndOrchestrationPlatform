"""세션 카드에 찍히는 시각.

카드에 있던 것은 세션 행이 만들어진 시각이었습니다. 새 세션을 열어 두고 나중에
말을 걸면 둘이 크게 벌어져서, 목록에서 "이 대화 언제 했더라" 를 찾을 때 쓸모가
없었습니다. 사람이 기억하는 것은 **말을 건 시각**입니다.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.timestamps import as_utc
from app.ui.components.sidebar import (
    event_changes_session_list,
    first_user_message_times,
    to_local,
)

DB_URL = "sqlite+aiosqlite:///:memory:"


async def _seed(rows) -> str:
    """세션 하나와 그 발언들을 넣고 세션 id 를 돌려줍니다."""
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    sid = f"times-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(id=sid, title="T", created_at=datetime(2026, 1, 1, 0, 0)))
        for sender_key, when in rows:
            db.add(MessageModel(
                id=str(uuid.uuid4()), session_id=sid, sender_key=sender_key,
                sender_name=sender_key, content="...", created_at=when,
                msg_type="user" if sender_key == "user" else "agent",
            ))
        await db.commit()
    return sid


@pytest.mark.asyncio
async def test_time_is_the_first_user_message_not_the_row_creation():
    """세션은 1월 1일에 만들어졌지만 말을 건 것은 3월 5일입니다."""
    sid = await _seed([
        ("user", datetime(2026, 3, 5, 14, 30)),
        ("architect", datetime(2026, 3, 5, 14, 31)),
    ])

    async with get_session_factory(DB_URL)() as db:
        times = await first_user_message_times(db)

    assert as_utc(times[sid]) == as_utc(datetime(2026, 3, 5, 14, 30))


@pytest.mark.asyncio
async def test_later_interjections_do_not_move_the_start():
    """토론 중 개입도 사용자 발언이지만, 대화가 시작된 시각은 첫 발언입니다."""
    sid = await _seed([
        ("user", datetime(2026, 3, 5, 14, 30)),      # 최초 요청
        ("coder", datetime(2026, 3, 5, 14, 31)),
        ("user", datetime(2026, 3, 5, 15, 10)),      # 토론 중 개입
    ])

    async with get_session_factory(DB_URL)() as db:
        times = await first_user_message_times(db)

    assert as_utc(times[sid]) == as_utc(datetime(2026, 3, 5, 14, 30))


@pytest.mark.asyncio
async def test_agent_only_session_has_no_start_time():
    """사용자가 아직 말하지 않았으면 시작 시각이 없습니다 (카드에는 '시작 전')."""
    sid = await _seed([("orchestrator", datetime(2026, 3, 5, 14, 31))])

    async with get_session_factory(DB_URL)() as db:
        times = await first_user_message_times(db)

    assert sid not in times


@pytest.mark.asyncio
async def test_each_session_gets_its_own_start():
    first = await _seed([("user", datetime(2026, 3, 1, 9, 0))])
    second = await _seed([("user", datetime(2026, 3, 2, 18, 45))])

    async with get_session_factory(DB_URL)() as db:
        times = await first_user_message_times(db)

    assert as_utc(times[first]) == as_utc(datetime(2026, 3, 1, 9, 0))
    assert as_utc(times[second]) == as_utc(datetime(2026, 3, 2, 18, 45))


# --------------------------------------------------------------- 시간대


def test_naive_timestamps_are_read_as_utc_and_shown_locally():
    """SQLite 는 오프셋을 버립니다. 그대로 찍으면 한국에서 9시간 전으로 보입니다."""
    stored = datetime(2026, 3, 5, 5, 30)              # UTC 벽시계
    local = to_local(stored)

    assert local.tzinfo is not None
    assert local == stored.replace(tzinfo=timezone.utc)
    # 같은 순간을 가리키되, 표시용 벽시계는 이 기계의 시간대를 따릅니다.
    assert local.utcoffset() == datetime.now().astimezone().utcoffset()


def test_aware_timestamps_are_left_as_they_are():
    aware = datetime(2026, 3, 5, 5, 30, tzinfo=timezone(timedelta(hours=9)))
    assert to_local(aware) == aware


# --------------------------------------------------------------- 언제 다시 그리는가
#
# 시각을 옳게 계산해도 목록을 다시 그리지 않으면 화면에는 옛 값이 남습니다.
# 카드의 시각은 **사용자 발언 행**에서 오고 그 행은 토론이 시작된 뒤에 생기는데,
# 예전에는 `turn_completed` 에서만 다시 그렸습니다. 그래서 첫 요청을 보내도 카드는
# 토론이 끝날 때까지 '시작 전' 이었고, 새로고침해야 시각이 나타났습니다.


def test_the_first_user_message_redraws_the_list():
    """이 대화의 '시작 시각' 이 생기는 순간입니다."""
    assert event_changes_session_list({
        "type": "message_added",
        "message": {"id": "m1", "sender_key": "user", "content": "설계해 주세요"},
    })


def test_an_agent_speech_does_not_redraw_the_list():
    """목록에 보이지 않는 것 때문에 매 발언마다 다시 그리지 않습니다."""
    assert not event_changes_session_list({
        "type": "message_added",
        "message": {"id": "m2", "sender_key": "architect", "content": "..."},
    })
    assert not event_changes_session_list({"type": "message_stream_chunk", "delta": "..."})
    assert not event_changes_session_list({"type": "round_started", "round": 2})


def test_the_end_of_a_run_redraws_the_list():
    """진행 중 표시(스피너)를 내려야 합니다.

    오류나 취소로 끝나면 `turn_completed` 는 오지 않습니다. `run_finished` 까지
    보지 않으면 실패한 대화의 카드가 계속 돌아갑니다.
    """
    assert event_changes_session_list({"type": "turn_completed", "status": "completed"})
    assert event_changes_session_list({"type": "run_finished", "status": "failed"})


def test_a_malformed_event_does_not_crash_the_handler():
    """이벤트는 여러 곳에서 만들어집니다. 없는 키에 걸려 화면이 멈추면 안 됩니다."""
    assert not event_changes_session_list({})
    assert not event_changes_session_list({"type": "message_added"})
    assert not event_changes_session_list({"type": "message_added", "message": None})


@pytest.mark.asyncio
async def test_the_time_is_readable_the_moment_the_event_arrives():
    """다시 그리라는 신호가 왔을 때 **다른 연결에서도** 그 값이 보여야 합니다.

    사이드바는 자기 DB 세션을 새로 열어 시각을 읽습니다. 엔진이 발언 행을 커밋하기
    전에 이벤트를 흘리면, 목록은 신호를 받고도 여전히 '시작 전' 을 그립니다 —
    고치기 전과 똑같은 증상이면서 원인만 다른 상태가 됩니다.
    """
    from app.agents.pool import AgentPool
    from app.config import AgentConfig
    from app.orchestration.engine import OrchestratorEngine
    from tests.fake_llm import FakeLLMCaller

    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    sid = f"live-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(id=sid, title="T", max_rounds=1,
                            active_agents=["orchestrator", "coder"]))
        await db.commit()

    seen_at_event = []

    async def on_event(event):
        if not event_changes_session_list(event):
            return
        # 사이드바가 하는 것과 같은 일: 별도의 세션으로 다시 읽습니다.
        async with factory() as db:
            seen_at_event.append(sid in await first_user_message_times(db))

    pool = AgentPool({
        "orchestrator": AgentConfig(name="Orch", role="Lead", api_key="k"),
        "coder": AgentConfig(name="Dev", role="Implementation", api_key="k"),
    })
    engine = OrchestratorEngine(agent_pool=pool, llm_caller=FakeLLMCaller())
    await engine.run_turn(sid, "설계해 주세요.", on_event=on_event)

    assert seen_at_event, "다시 그릴 신호가 한 번도 오지 않았습니다"
    assert seen_at_event[0] is True, "첫 신호 때 이미 시작 시각이 보여야 합니다"
