"""발언에 다는 인라인 코멘트 (ADR-036).

지키려는 것.

1. **그 에이전트에게만** — 코멘트는 발언자의 다음 발언에만 실리고, 다른 에이전트의 프롬프트에는 나타나지 않습니다.
2. **기록으로 판단** — 발언이 기록되는 커밋에서 전달됨으로 적습니다. 실패한 발언에 실렸던 코멘트는 잃지 않습니다.
3. **언제든** — 토론 도중에 달아도, 턴이 돌지 않을 때 달아도 그 에이전트의 다음 발언을 기다립니다.
4. **대화와 함께** — 발언·턴·대화가 지워지면 함께 지워지고, 갈라진 대화에는 따라갑니다.
"""

import asyncio
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from app.database.models import MessageModel, SessionModel, SpeechCommentModel, TurnModel
from app.database.session import get_session_factory
from app.orchestration import speech_comments, turns
from app.orchestration.speech_comments import SpeechCommentError, comment_block, with_comments
from app.session_fork import fork_session
from app.session_ops import discard_turn
from tests.fake_llm import FakeLLMCaller
from tests.test_turn_resume import DB_URL, REQUEST, _engine, _session


class RecordingLLM(FakeLLMCaller):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent: List[Dict[str, Any]] = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.sent.append({"key": agent.key, "text": "\n".join(str(m.get("content")) for m in messages)})
        return await super().call_agent(agent, messages, *args, **kwargs)


async def _finished_turn(llm=None) -> str:
    sid = await _session()
    await _engine(llm or FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    return sid


async def _speech(sid: str, key: str) -> MessageModel:
    async with get_session_factory(DB_URL)() as db:
        rows = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
    return next(m for m in rows if m.sender_key == key and turns.kind_of(m) == turns.KIND_SPEECH)


async def _add(sid: str, message_id: str, quote: str = "그 문장", comment: str = "여기를 고치세요") -> Dict[str, Any]:
    async with get_session_factory(DB_URL)() as db:
        return await speech_comments.add_comment(db, sid, message_id, quote, comment)


async def _status(comment_id: str) -> SpeechCommentModel:
    async with get_session_factory(DB_URL)() as db:
        return await db.get(SpeechCommentModel, comment_id)


# =============================================================== 1. 그 에이전트에게만


@pytest.mark.asyncio
async def test_a_comment_reaches_only_that_agent_on_its_next_speech():
    sid = await _finished_turn()
    target = await _speech(sid, "architect")
    comment = await _add(sid, target.id)

    llm = RecordingLLM()
    await _engine(llm).run_turn(session_id=sid, user_prompt="이어서 진행해 줘")

    carrying = [s["key"] for s in llm.sent if "여기를 고치세요" in s["text"]]
    assert carrying == ["architect"], "딱 한 번, 그 에이전트의 다음 발언에만 실립니다"
    delivered = await _status(comment["id"])
    assert delivered.status == "delivered" and delivered.delivered_message_id and delivered.delivered_at
    assert delivered.delivered_message_id != target.id


@pytest.mark.asyncio
async def test_the_comment_does_not_enter_the_shared_record():
    sid = await _finished_turn()
    await _add(sid, (await _speech(sid, "architect")).id)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="이어서 진행해 줘")

    async with get_session_factory(DB_URL)() as db:
        rows = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().all()
    assert not [m for m in rows if "여기를 고치세요" in (m.content or "")]


@pytest.mark.asyncio
async def test_a_comment_on_the_conclusion_goes_to_the_orchestrator():
    sid = await _finished_turn()
    async with get_session_factory(DB_URL)() as db:
        rows = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().all()
    synthesis = next(m for m in rows if turns.kind_of(m) == turns.KIND_SYNTHESIS)
    await _add(sid, synthesis.id, comment="결론의 위험 항목을 더 다뤄 주세요")

    llm = RecordingLLM()
    await _engine(llm).run_turn(session_id=sid, user_prompt="이어서 진행해 줘")

    assert {s["key"] for s in llm.sent if "결론의 위험 항목" in s["text"]} == {"orchestrator"}


# =============================================================== 2. 기록으로 판단


@pytest.mark.asyncio
async def test_a_failed_speech_does_not_use_up_the_comment():
    sid = await _finished_turn()
    comment = await _add(sid, (await _speech(sid, "architect")).id)

    await _engine(FakeLLMCaller(fail_keys=["architect"])).run_turn(session_id=sid, user_prompt="이어서 진행해 줘")
    assert (await _status(comment["id"])).status == "pending"

    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="다시 진행해 줘")
    assert (await _status(comment["id"])).status == "delivered"


@pytest.mark.asyncio
async def test_delivery_is_announced_with_the_comment_ids():
    sid = await _finished_turn()
    comment = await _add(sid, (await _speech(sid, "architect")).id)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="이어서", on_event=on_event)

    announced = [e for e in events if e.get("type") == "speech_comments_delivered"]
    assert len(announced) == 1 and announced[0]["ids"] == [comment["id"]]
    assert announced[0]["agent_name"]


# =============================================================== 3. 언제든


@pytest.mark.asyncio
async def test_a_comment_added_during_a_turn_reaches_the_agents_next_round():
    sid = await _session()
    added: Dict[str, Any] = {}
    llm = RecordingLLM()

    async def on_event(event):
        # 아키텍트의 첫 발언이 끝나는 순간, 사람이 그 카드에 코멘트를 답니다.
        message = event.get("message") or {}
        if event.get("type") == "message_added" and message.get("sender_key") == "architect" and not added:
            added.update(await _add(sid, message["id"], comment="라운드 사이에 고치세요"))

    await _engine(llm).run_turn(session_id=sid, user_prompt=REQUEST, on_event=on_event)

    speeches = [s for s in llm.sent if s["key"] == "architect"]
    assert sum("라운드 사이에 고치세요" in s["text"] for s in speeches) == 1
    assert "라운드 사이에 고치세요" not in speeches[0]["text"]
    assert (await _status(added["id"])).status == "delivered"


@pytest.mark.asyncio
async def test_parallel_speeches_read_comments_without_tripping_over_each_other():
    from tests.test_turn_resume import CrashingLLM

    sid = await _session(strategy="parallel_dispatch", max_rounds=2)
    await _engine(CrashingLLM()).run_turn(session_id=sid, user_prompt=REQUEST)
    architect = await _speech(sid, "architect")
    coder = await _speech(sid, "coder")
    first = await _add(sid, architect.id, comment="아키텍트에게")
    second = await _add(sid, coder.id, comment="코더에게")

    await _engine(CrashingLLM()).run_turn(session_id=sid, user_prompt="이어서")

    assert (await _status(first["id"])).status == "delivered"
    assert (await _status(second["id"])).status == "delivered"


# =============================================================== 쓰는 규칙


@pytest.mark.asyncio
async def test_what_can_be_commented():
    sid = await _finished_turn()
    target = await _speech(sid, "architect")
    async with get_session_factory(DB_URL)() as db:
        rows = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().all()
    opening = next(m for m in rows if m.sender_key == "user")
    with pytest.raises(SpeechCommentError, match="에이전트의 발언"):
        await _add(sid, opening.id)
    with pytest.raises(SpeechCommentError, match="찾지 못했습니다"):
        await _add(sid, "없는-발언")
    with pytest.raises(SpeechCommentError, match="찾지 못했습니다"):
        await _add("다른-대화", target.id)
    with pytest.raises(SpeechCommentError, match="내용을 적어"):
        await _add(sid, target.id, comment="   ")


@pytest.mark.asyncio
async def test_only_a_waiting_comment_can_be_deleted():
    sid = await _finished_turn()
    target = await _speech(sid, "architect")
    waiting = await _add(sid, target.id)
    async with get_session_factory(DB_URL)() as db:
        assert await speech_comments.delete_pending(db, sid, waiting["id"]) is True
        assert await speech_comments.delete_pending(db, sid, waiting["id"]) is False

    delivered = await _add(sid, target.id)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="이어서")
    async with get_session_factory(DB_URL)() as db:
        assert await speech_comments.delete_pending(db, sid, delivered["id"]) is False
    assert (await _status(delivered["id"])) is not None


def test_the_block_says_where_each_comment_points():
    block = comment_block([
        {"round_number": 2, "quote": "첫 줄\n둘째 줄", "comment": "고치세요"},
        {"round_number": 0, "quote": "", "comment": "전체적으로 짧게"},
    ])
    assert "Round 2 발언의 다음 부분" in block and "> 첫 줄\n> 둘째 줄" in block and "→ 고치세요" in block
    assert "앞선 발언에 대해: 전체적으로 짧게" in block


def test_comments_go_on_the_last_user_message():
    prompt = [{"role": "assistant", "content": "앞"}, {"role": "user", "content": "차례입니다"}]
    comment = [{"round_number": 1, "quote": "", "comment": "고치세요"}]
    out = with_comments(prompt, comment)
    assert len(out) == 2 and out[1]["content"].startswith("차례입니다\n\n[유저의 인라인 코멘트]")
    assert prompt[1]["content"] == "차례입니다", "원본 프롬프트는 건드리지 않습니다"
    assert with_comments(prompt, []) is prompt

    ending_in_blocks = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    out = with_comments(ending_in_blocks, comment)
    assert len(out) == 2 and "고치세요" in out[1]["content"]


# =============================================================== 4. 대화와 함께


@pytest.mark.asyncio
async def test_discarding_a_turn_removes_its_comments():
    sid = await _finished_turn()
    target = await _speech(sid, "architect")
    comment = await _add(sid, target.id)

    async with get_session_factory(DB_URL)() as db:
        await discard_turn(db, sid, [], [], turn_id=target.turn_id)

    assert await _status(comment["id"]) is None


@pytest.mark.asyncio
async def test_discarding_listed_messages_removes_their_comments():
    sid = await _finished_turn()
    target = await _speech(sid, "architect")
    comment = await _add(sid, target.id)

    async with get_session_factory(DB_URL)() as db:
        await discard_turn(db, sid, [target.id], [])

    assert await _status(comment["id"]) is None


@pytest.mark.asyncio
async def test_deleting_the_session_through_the_orm_removes_its_comments():
    sid = await _finished_turn()
    comment = await _add(sid, (await _speech(sid, "architect")).id)
    async with get_session_factory(DB_URL)() as db:
        await db.delete(await db.get(SessionModel, sid))
        await db.commit()
    assert await _status(comment["id"]) is None


@pytest.mark.asyncio
async def test_a_fork_takes_the_comments_along_and_keeps_them_waiting(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = await _session()
    # 갈래는 작업 공간 옆에 새 폴더를 만듭니다. 저장소의 작업 공간 옆에 만들지 않게 합니다.
    async with get_session_factory(DB_URL)() as db:
        (await db.get(SessionModel, sid)).workspace_dir = str(workspace)
        await db.commit()
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    target = await _speech(sid, "architect")
    await _add(sid, target.id)
    async with get_session_factory(DB_URL)() as db:
        turn_id = (await db.execute(select(TurnModel.id).where(TurnModel.session_id == sid))).scalars().first()
        result = await fork_session(db, sid, turn_id)
        grouped = await speech_comments.comments_by_message(db, result.session_id)

    assert len(grouped) == 1
    message_id, comments = next(iter(grouped.items()))
    assert message_id != target.id and comments[0]["status"] == "pending"

    llm = RecordingLLM()
    await _engine(llm).run_turn(session_id=result.session_id, user_prompt="갈래에서 이어서")
    assert [s["key"] for s in llm.sent if "여기를 고치세요" in s["text"]] == ["architect"]


@pytest.mark.asyncio
async def test_a_draft_resume_does_not_claim_a_comment_it_never_read():
    """초안에서 이어 가는 발언은 프롬프트를 새로 만들지 않으므로 코멘트를 전달한 것으로 적지 않습니다."""
    from tests.test_turn_resume import _crash

    sid = await _session()
    turn_id = await _crash(sid, crash_at=("coder", 1), crash_tools=[{
        "tool_name": "filesystem__write_file", "arguments": {"path": "a.py"}, "output": "ok", "status": "success",
    }])
    architect = await _speech(sid, "architect")
    comment = await _add(sid, architect.id)
    await asyncio.sleep(0)

    await _engine(FakeLLMCaller()).resume_turn(sid, turn_id, "continue")

    # 아키텍트는 이어 간 턴의 다음 라운드에 새 프롬프트로 말하므로 그때 전달됩니다.
    assert (await _status(comment["id"])).status == "delivered"
