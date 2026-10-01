"""요청을 잘못 보냈을 때 되돌리기 — 긴급 종료.

정지(`TurnControl.request_stop`)는 "여기까지의 논의로 결론을 내라" 는 뜻입니다.
그런데 요청 **자체가** 틀렸다면 (오타, 잘못 붙여넣은 글, 다른 대화에 보낼 뻔한
요청) 그 결론은 받을 이유가 없습니다. 예전에는 합성이 끝날 때까지 기다렸다가,
틀린 요청과 그에 답한 발언을 기록에 남긴 채 다시 쓰는 수밖에 없었습니다. 그
기록은 다음 턴의 맥락으로 계속 따라다닙니다.

여기서 지키려는 것.

1. **그 턴이 만든 것만** 지운다. 앞선 대화는 사람이 고치려는 대상이 아니다.
2. **도구 기록도 함께** 지운다. 발언만 지우면 아무도 가리키지 않는 행이 남는다.
3. 첫 요청을 지워 대화가 비면 **시작 전으로 되돌린다** — 페르소나 잠금까지.
4. 무엇을 지울지는 러너가 알려 주고, 지우는 것은 DB 를 아는 쪽이 한다.
"""

import uuid
from typing import List

import pytest
from sqlalchemy import func, select

from app.database.models import (
    ArtifactModel,
    MessageModel,
    SessionAgentModel,
    SessionModel,
    ToolCallRecordModel,
)
from app.database.session import get_session_factory, init_db
from app.session_ops import discard_turn

DB_URL = "sqlite+aiosqlite:///:memory:"


async def _seed(*, locked: bool = True) -> tuple:
    """두 턴짜리 대화. 앞 턴은 끝났고, 뒤 턴이 방금 취소되었습니다."""
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    sid = f"abort-{uuid.uuid4().hex[:8]}"

    first: List[str] = [str(uuid.uuid4()) for _ in range(2)]
    second: List[str] = [str(uuid.uuid4()) for _ in range(3)]

    async with factory() as db:
        db.add(SessionModel(id=sid, title="T", personas_locked=locked))
        db.add(SessionAgentModel(session_id=sid, agent_key="architect", name="Arch"))
        for index, msg_id in enumerate(first + second):
            db.add(MessageModel(
                id=msg_id, session_id=sid,
                sender_key="user" if index in (0, 2) else "architect",
                sender_name="X", content="...", round_number=0,
                msg_type="user" if index in (0, 2) else "agent",
            ))
        # 취소된 턴의 발언 하나가 도구를 썼습니다.
        db.add(ToolCallRecordModel(
            id=str(uuid.uuid4()), session_id=sid, message_id=second[1],
            agent_key="architect", tool_name="read_file", arguments={}, output="...",
        ))
        # 앞 턴의 도구 기록. 이건 남아야 합니다.
        db.add(ToolCallRecordModel(
            id=str(uuid.uuid4()), session_id=sid, message_id=first[1],
            agent_key="architect", tool_name="write_file", arguments={}, output="...",
        ))
        await db.commit()
    return factory, sid, first, second


async def _counts(factory, sid) -> dict:
    async with factory() as db:
        return {
            "messages": await db.scalar(
                select(func.count()).select_from(MessageModel).where(MessageModel.session_id == sid)),
            "tools": await db.scalar(
                select(func.count()).select_from(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid)),
            "artifacts": await db.scalar(
                select(func.count()).select_from(ArtifactModel).where(ArtifactModel.session_id == sid)),
            "locked": (await db.get(SessionModel, sid)).personas_locked,
        }


@pytest.mark.asyncio
async def test_only_the_aborted_turn_is_removed():
    factory, sid, first, second = await _seed()

    async with factory() as db:
        await discard_turn(db, sid, second)

    async with factory() as db:
        left = [r.id for r in (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid)
        )).scalars().all()]
    assert sorted(left) == sorted(first), "앞선 대화는 그대로 있어야 합니다"


@pytest.mark.asyncio
async def test_the_tool_records_of_that_turn_go_with_it():
    """발언만 지우면 아무도 가리키지 않는 도구 기록이 남습니다."""
    factory, sid, _first, second = await _seed()

    async with factory() as db:
        await discard_turn(db, sid, second)

    counts = await _counts(factory, sid)
    assert counts["tools"] == 1, "앞 턴의 도구 기록은 남고, 취소된 턴의 것만 사라집니다"


@pytest.mark.asyncio
async def test_artifacts_named_by_the_run_are_removed():
    factory, sid, _first, second = await _seed()
    art_id = str(uuid.uuid4())
    async with factory() as db:
        db.add(ArtifactModel(id=art_id, session_id=sid, artifact_type="markdown",
                             title="중간 산출물", content="...", language="markdown"))
        await db.commit()

    async with factory() as db:
        await discard_turn(db, sid, second, [art_id])

    assert (await _counts(factory, sid))["artifacts"] == 0


@pytest.mark.asyncio
async def test_discarding_the_first_turn_puts_the_session_back_to_not_started():
    """첫 요청을 지웠다면 이 대화는 아직 시작하지 않은 것입니다.

    그렇다면 에이전트 구성도 다시 만질 수 있어야 말이 맞습니다.
    """
    factory, sid, first, second = await _seed()

    async with factory() as db:
        started_over = await discard_turn(db, sid, first + second)

    assert started_over is True
    counts = await _counts(factory, sid)
    assert counts["messages"] == 0
    assert counts["locked"] is False


@pytest.mark.asyncio
async def test_the_frozen_agent_snapshot_survives():
    """잠금만 풀고 고정해 둔 구성은 남깁니다. 다음 턴이 그때의 conf.json 으로 다시 고정합니다."""
    factory, sid, first, second = await _seed()

    async with factory() as db:
        await discard_turn(db, sid, first + second)

    async with factory() as db:
        rows = (await db.execute(
            select(SessionAgentModel).where(SessionAgentModel.session_id == sid)
        )).scalars().all()
    assert [r.agent_key for r in rows] == ["architect"]


@pytest.mark.asyncio
async def test_an_earlier_turn_keeps_the_lock():
    """대화가 남아 있으면 시작한 대화입니다. 페르소나는 잠긴 채로 둡니다."""
    factory, sid, _first, second = await _seed()

    async with factory() as db:
        started_over = await discard_turn(db, sid, second)

    assert started_over is False
    assert (await _counts(factory, sid))["locked"] is True


@pytest.mark.asyncio
async def test_unknown_and_empty_ids_are_harmless():
    """스트리밍 중이라 id 가 비어 있는 기록이 섞여 들어올 수 있습니다."""
    factory, sid, first, second = await _seed()

    async with factory() as db:
        await discard_turn(db, sid, ["", None, str(uuid.uuid4())])

    assert (await _counts(factory, sid))["messages"] == len(first) + len(second)


@pytest.mark.asyncio
async def test_a_turn_of_another_session_is_not_touched():
    """id 만 맞으면 지우는 것이 아니라, 이 대화의 것인지도 봅니다."""
    factory, sid, first, second = await _seed()
    other = f"other-{uuid.uuid4().hex[:8]}"
    other_msg = str(uuid.uuid4())
    async with factory() as db:
        db.add(SessionModel(id=other, title="다른 대화"))
        db.add(MessageModel(id=other_msg, session_id=other, sender_key="user",
                            sender_name="User", content="...", msg_type="user"))
        await db.commit()

    async with factory() as db:
        await discard_turn(db, sid, second + [other_msg])

    async with factory() as db:
        assert await db.get(MessageModel, other_msg) is not None


# --------------------------------------------------------------- 러너의 몫
#
# 러너는 지우지 않습니다. 태스크를 끊고, **무엇을 지워야 하는지** 알려 줍니다.
# 기록을 만지는 것은 DB 를 아는 쪽의 일입니다.


class _HangingEngine:
    """발언 두 개를 흘린 뒤 끝나지 않는 토론."""

    def __init__(self):
        self.cancelled = False

    async def run_turn(self, session_id, user_prompt, on_event=None, control=None,
                       skill_designations=None):
        import asyncio

        for index in (1, 2):
            await on_event({
                "type": "message_added",
                "message": {
                    "id": f"m{index}", "sender_key": "user" if index == 1 else "architect",
                    "sender_name": "X", "sender_role": "", "content": "...",
                    "round_number": 0, "msg_type": "user" if index == 1 else "agent",
                },
            })
        try:
            await asyncio.sleep(3600)
        except BaseException:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_abort_reports_what_the_turn_produced_and_kills_it():
    import asyncio

    from app.orchestration.runner import DebateRunner

    engine = _HangingEngine()
    runner = DebateRunner(engine=engine)
    run = runner.start("s1", "틀린 요청입니다")
    await asyncio.sleep(0.05)          # 발언 두 개가 흘러나올 틈

    produced = await runner.abort("s1")

    assert produced["prompt"] == "틀린 요청입니다"
    assert produced["message_ids"] == ["m1", "m2"]
    assert engine.cancelled is True
    assert run.status == "cancelled"
    assert runner.is_running("s1") is False


@pytest.mark.asyncio
async def test_aborting_nothing_says_so():
    """이미 끝난 토론을 지우려 들면 안 됩니다 — 그건 정상적인 기록입니다."""
    from app.orchestration.runner import DebateRunner

    runner = DebateRunner(engine=_HangingEngine())

    assert await runner.abort("없는-세션") is None
