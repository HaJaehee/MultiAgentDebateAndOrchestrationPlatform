"""체험 화면의 저장소 (app/trial/store.py, app/trial/stats.py).

1. 대화는 주인에게만 보인다 — 남의 대화 id 를 알아도 "없는 대화" 다.
2. 주인 화면에서 지운 대화는 체험 목록에서도 사라진다 (남은 표시 행은 읽히지 않는다).
3. 사본과 평가는 사람마다 따로다.
4. 결과 화면은 가장 최근 합성 발언과 결정 장부의 두 칸(합의·이견)을 보여 준다.
5. 운영자 통계는 기록에서 센다.
6. 서버 중단으로 끝나지 못한 요청은 목록에서 먼저 보이고, 이어서 마친 결과는 중단 시간을 함께 적는다 (ADR-024).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import TURN_INTERRUPTED, Base, MessageModel, SessionModel, TurnModel
from app.trial import models as trial_models  # noqa: F401 - 테이블 등록
from app.trial.models import TrialSessionModel, TrialUserModel
from app.trial.stats import usage_report
from app.trial.store import (
    agreed_and_open,
    copy_template,
    create_copy,
    delete_copy,
    delete_trial_session,
    get_copy,
    list_copies,
    list_user_sessions,
    owned_session,
    save_copy,
    save_feedback,
    session_result,
)
from app.trial.templates import parse_template
from tests.fake_llm import LEDGER_REPLY

T0 = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _user(db, name):
    user = TrialUserModel(name=name, name_key=name.casefold(), pin_hash="h")
    db.add(user)
    await db.commit()
    return user


async def _session(db, user, *, ref="t:demo", title="데모", finished=False, asked=True):
    sid = str(uuid.uuid4())
    db.add(SessionModel(id=sid, title=title, decision_ledger=LEDGER_REPLY if finished else ""))
    db.add(TrialSessionModel(session_id=sid, user_id=user.id, template_ref=ref, template_title=title))
    if asked:
        db.add(MessageModel(session_id=sid, sender_key="user", sender_name="User", content="요청",
                            msg_type="user", created_at=T0))
    if finished:
        db.add(MessageModel(session_id=sid, sender_key="orchestrator", sender_name="사회자", content="## 결론",
                            msg_type="orchestrator", created_at=T0 + timedelta(minutes=3),
                            turn_started_at=T0, finished_at=T0 + timedelta(minutes=3)))
    await db.commit()
    return sid


def _template():
    return parse_template({
        "id": "demo", "title": "데모",
        "participants": [{"key": "orchestrator", "name": "사회자"}, {"key": "a", "name": "검토자"}],
        "inputs": [{"id": "q", "label": "질문"}], "prompt": "{q}",
    })


@pytest.mark.asyncio
async def test_conversations_are_visible_only_to_their_owner(db):
    kim, lee = await _user(db, "kim"), await _user(db, "lee")
    mine = await _session(db, kim, finished=True)
    started = await _session(db, kim)
    empty = await _session(db, kim, asked=False)
    theirs = await _session(db, lee)

    rows = await list_user_sessions(db, kim.id)
    assert {r.session_id: r.state for r in rows} == {mine: "done", started: "started", empty: "empty"}
    assert await owned_session(db, kim.id, theirs) is None
    assert await owned_session(db, kim.id, mine) is not None
    assert not await delete_trial_session(db, kim.id, theirs), "남의 대화는 지울 수 없습니다"
    assert await db.get(SessionModel, theirs) is not None


@pytest.mark.asyncio
async def test_deleting_removes_the_conversation_and_its_trial_rows(db):
    kim = await _user(db, "kim")
    sid = await _session(db, kim, finished=True)
    await save_feedback(db, kim.id, sid, "t:demo", 1, "좋아요")
    assert await delete_trial_session(db, kim.id, sid)
    assert await db.get(SessionModel, sid) is None
    assert await db.get(TrialSessionModel, sid) is None
    assert await list_user_sessions(db, kim.id) == []


@pytest.mark.asyncio
async def test_a_conversation_deleted_on_the_owner_screen_disappears_from_the_trial(db):
    kim = await _user(db, "kim")
    sid = await _session(db, kim)
    await db.delete(await db.get(SessionModel, sid))  # 주인 화면의 삭제는 trial_sessions 를 모릅니다
    await db.commit()
    assert await list_user_sessions(db, kim.id) == []
    assert await owned_session(db, kim.id, sid) is None


@pytest.mark.asyncio
async def test_copies_belong_to_one_person(db):
    kim, lee = await _user(db, "kim"), await _user(db, "lee")
    copy = await create_copy(db, kim.id, _template(), "t:demo")
    assert copy.title == "데모 (내 사본)"
    assert copy_template(copy).id.startswith("copy-")
    assert await get_copy(db, lee.id, copy.id) is None
    assert [c.id for c in await list_copies(db, kim.id)] == [copy.id]

    edited = copy_template(copy).model_copy(update={"title": "내 검토", "max_rounds": 3})
    await save_copy(db, copy, edited)
    again = await get_copy(db, kim.id, copy.id)
    assert copy_template(again).title == "내 검토" and copy_template(again).max_rounds == 3

    assert not await delete_copy(db, lee.id, copy.id)
    assert await delete_copy(db, kim.id, copy.id)
    assert await list_copies(db, kim.id) == []


@pytest.mark.asyncio
async def test_feedback_is_one_per_person_per_conversation(db):
    kim = await _user(db, "kim")
    sid = await _session(db, kim, finished=True)
    await save_feedback(db, kim.id, sid, "t:demo", 1, "")
    again = await save_feedback(db, kim.id, sid, "t:demo", -5, "  너무 길어요  ")
    assert again.rating == -1 and again.comment == "너무 길어요"
    report = await usage_report(db)
    assert (report.templates[0].up, report.templates[0].down) == (0, 1)


@pytest.mark.asyncio
async def test_the_result_is_the_latest_synthesis_with_its_ledger(db):
    kim = await _user(db, "kim")
    sid = await _session(db, kim, finished=True)
    result = await session_result(db, sid)
    assert result.final == "## 결론"
    assert result.turn_seconds == 180
    assert result.user_turns == 1
    boxes = agreed_and_open(result.ledger)
    assert "FastAPI" in boxes["agreed"]
    assert boxes["open"] == "", "'- 없음' 은 의견이 갈린 것으로 보이면 안 됩니다"



@pytest.mark.asyncio
async def test_an_interrupted_request_comes_first_in_the_list(db):
    kim = await _user(db, "kim")
    sid = await _session(db, kim, finished=True)  # 이전 결과가 있는 대화
    db.add(TurnModel(session_id=sid, status=TURN_INTERRUPTED, phase="debating", started_at=T0))
    await db.commit()
    rows = await list_user_sessions(db, kim.id)
    assert [r.state for r in rows] == ["interrupted"], "할 일(이어서 진행·결론 내기)이 남은 대화입니다"


@pytest.mark.asyncio
async def test_a_resumed_result_names_the_pause_inside_its_time(db):
    kim = await _user(db, "kim")
    sid = await _session(db, kim, finished=True)
    turn = TurnModel(session_id=sid, status="completed", phase="completed", started_at=T0,
                     paused_seconds=120, resumed_count=1)
    db.add(turn)
    await db.commit()
    final = next(m for m in (await db.get(SessionModel, sid)).messages if m.turn_started_at is not None)
    final.turn_id = turn.id
    await db.commit()

    result = await session_result(db, sid)
    assert result.turn_seconds == 180
    assert (result.paused_seconds, result.resumed_count) == (120, 1)

def test_ledger_boxes_pick_decisions_and_open_issues():
    ledger = "## 결정 사항\n- A안으로 간다\n\n## 미해결 쟁점\n- 예산 출처\n"
    assert agreed_and_open(ledger) == {"agreed": "- A안으로 간다", "open": "- 예산 출처"}
    assert agreed_and_open("") == {"agreed": "", "open": ""}


@pytest.mark.asyncio
async def test_usage_is_counted_from_the_records(db):
    kim, lee = await _user(db, "kim"), await _user(db, "lee")
    await _session(db, kim, ref="t:a", title="A", finished=True)
    await _session(db, kim, ref="t:a", title="A")
    await _session(db, lee, ref="t:b", title="B", asked=False)
    report = await usage_report(db)
    assert (report.users, report.sessions, report.completed) == (2, 3, 1)
    a = next(t for t in report.templates if t.ref == "t:a")
    assert (a.sessions, a.completed, a.unfinished) == (2, 1, 1)
    assert a.average_seconds == 180
    assert {p.name: p.sessions for p in report.people} == {"kim": 2, "lee": 1}
