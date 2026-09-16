"""세션 목록 정렬 — 최근 변경 · 이름 · 시작 시간 · (마지막 턴) 완료 시간.

지키려는 것.

1. 기준 값이 없는 세션(시작 전 · 완료 전)은 방향과 상관없이 맨 뒤에 온다.
2. 완료 시각은 **마지막 턴을 마무리한 합성 발언이 끝난 시각**이다. 합성 뒤에 들어온 개입이나
   진행 중인 다음 턴의 발언은 완료를 옮기지 않는다.
3. 시각 컬럼이 생기기 전의 대화도 완료 시각이 있다.
4. 브라우저에 남긴 정렬 값이 깨져 있으면 기본값으로 돌아간다.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.ui.components.sidebar import (
    SORT_KEYS,
    SessionSort,
    last_completion_times,
    sort_sessions,
    title_sort_key,
)

DB_URL = "sqlite+aiosqlite:///:memory:"
UTC = timezone.utc


def _s(sid, title, updated):
    return SimpleNamespace(id=sid, title=title, updated_at=updated)


def _t(day, hour=0):
    return datetime(2026, 9, day, hour, tzinfo=UTC)


SESSIONS = [
    _s("a", "banana", _t(3)),
    _s("b", "Apple", _t(1)),
    _s("c", "가나다", _t(2)),
    _s("d", None, _t(4)),
]


def _ids(items):
    return [s.id for s in items]


# ----------------------------------------------------------------- 순수 정렬


def test_default_is_the_old_order_most_recently_changed_first():
    assert _ids(sort_sessions(SESSIONS, SessionSort(), started={}, completed={})) == ["d", "a", "c", "b"]


def test_title_sorts_case_insensitively_and_reverses():
    asc = sort_sessions(SESSIONS, SessionSort.default_for("title"), started={}, completed={})
    # "Untitled Debate" 는 제목이 없는 세션이 화면에 보이는 이름입니다.
    assert _ids(asc) == ["b", "a", "d", "c"]
    desc = sort_sessions(SESSIONS, SessionSort("title", True), started={}, completed={})
    assert _ids(desc) == ["c", "d", "a", "b"]


def test_leading_emoji_and_symbols_do_not_decide_the_name_order():
    """"🛒 이커머스 …" 는 "이" 에서 찾습니다. 코드 포인트대로면 목록 맨 끝으로 갑니다."""
    assert title_sort_key("🛒 이커머스 토론") == title_sort_key("이커머스 토론")
    assert title_sort_key("[WIP] API") == "wip] api"
    assert title_sort_key("🔥🔥") == "🔥🔥", "기호뿐인 제목은 그대로 둡니다"
    items = [_s("x", "🛒 이커머스", _t(1)), _s("y", "가계부", _t(2)), _s("z", "하루", _t(3))]
    assert _ids(sort_sessions(items, SessionSort("title", False), started={}, completed={})) == ["y", "x", "z"]


@pytest.mark.parametrize("descending", [True, False])
def test_sessions_without_the_value_stay_at_the_bottom_in_both_directions(descending):
    started = {"a": _t(10), "b": _t(12)}
    result = sort_sessions(SESSIONS, SessionSort("started", descending), started=started, completed={})
    assert _ids(result)[:2] == (["b", "a"] if descending else ["a", "b"])
    assert _ids(result)[2:] == ["d", "c"], "시작 전 세션은 최근 변경 순으로 맨 뒤"


def test_completion_sort_uses_the_completion_times():
    completed = {"b": _t(20), "c": _t(15), "d": _t(18)}
    result = sort_sessions(SESSIONS, SessionSort.default_for("completed"), started={}, completed=completed)
    assert _ids(result) == ["b", "d", "c", "a"]


def test_naive_and_aware_times_compare_without_raising():
    """SQLite 는 시간대를 떼고 돌려주기도 합니다."""
    started = {"a": datetime(2026, 9, 10), "b": _t(12)}
    result = sort_sessions(SESSIONS, SessionSort("started", True), started=started, completed={})
    assert _ids(result)[:2] == ["b", "a"]


def test_every_key_has_a_default_direction():
    assert SessionSort.default_for("title").descending is False
    for key in ("updated", "started", "completed"):
        assert SessionSort.default_for(key).descending is True
    assert [k for k, _l, _d in SORT_KEYS] == ["updated", "title", "started", "completed"]


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"key": "size"}', 42])
def test_a_broken_saved_preference_is_ignored(raw):
    assert SessionSort.from_json(raw) is None


def test_a_saved_preference_round_trips():
    sort = SessionSort("started", False)
    assert SessionSort.from_json(sort.to_json()) == sort


# ----------------------------------------------------------------- 완료 시각


async def _session_with(rows) -> str:
    """rows: (sender_key, msg_type, created_at, started_at, finished_at, turn_started_at)"""
    await init_db(DB_URL)
    sid = f"sort-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(id=sid, title="T"))
        for sender, msg_type, created, started, finished, turn_started in rows:
            db.add(MessageModel(
                id=str(uuid.uuid4()), session_id=sid, sender_key=sender, sender_name=sender,
                content="...", msg_type=msg_type, created_at=created,
                started_at=started, finished_at=finished, turn_started_at=turn_started,
            ))
        await db.commit()
    return sid


def _naive(value):
    return value.replace(tzinfo=None) if value is not None and value.tzinfo else value


@pytest.mark.asyncio
async def test_completion_is_when_the_latest_synthesis_finished():
    sid = await _session_with([
        ("user", "user", _t(1, 9), _t(1, 9), _t(1, 9), None),
        ("orchestrator", "orchestrator", _t(1, 10), _t(1, 9), _t(1, 10), _t(1, 9)),   # 1턴 합성
        ("user", "user", _t(2, 9), _t(2, 9), _t(2, 9), None),
        ("orchestrator", "orchestrator", _t(2, 11), _t(2, 10), _t(2, 11), _t(2, 9)),  # 2턴 합성
        ("user", "user", _t(2, 12), _t(2, 12), _t(2, 12), None),                        # 합성 뒤 개입
        ("architect", "agent", _t(3, 9), _t(3, 8), _t(3, 9), None),                     # 3턴 진행 중
    ])
    async with get_session_factory(DB_URL)() as db:
        times = await last_completion_times(db)
    assert _naive(times[sid]) == _naive(_t(2, 11))


@pytest.mark.asyncio
async def test_a_session_whose_turn_never_finished_has_no_completion():
    sid = await _session_with([
        ("user", "user", _t(1, 9), _t(1, 9), _t(1, 9), None),
        ("orchestrator", "orchestrator", _t(1, 10), _t(1, 9), _t(1, 10), None),  # 계획만
    ])
    async with get_session_factory(DB_URL)() as db:
        assert sid not in await last_completion_times(db)


@pytest.mark.asyncio
async def test_sessions_from_before_the_timing_columns_use_the_last_orchestrator_message():
    sid = await _session_with([
        ("user", "user", _t(1, 9), None, None, None),
        ("orchestrator", "orchestrator", _t(1, 10), None, None, None),
        ("architect", "agent", _t(1, 11), None, None, None),
        ("orchestrator", "orchestrator", _t(1, 12), None, None, None),
    ])
    async with get_session_factory(DB_URL)() as db:
        times = await last_completion_times(db)
    assert _naive(times[sid]) == _naive(_t(1, 12))
