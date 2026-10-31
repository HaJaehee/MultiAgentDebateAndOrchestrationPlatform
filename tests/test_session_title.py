"""새 대화의 첫 제목 — 오케스트레이터의 첫 공식 발언 첫 문장, 그다음은 사람의 몫.

여기서 고정하는 것:

* **무엇을 문장으로 치는가** — 사고 과정, 머리글, 이름표, 인용, 코드와 표는 문장이 아닙니다.
* **언제 적는가** — 자리표시 제목인 새 대화의 공식 발언에서만, 계획 승인 카드보다 먼저. 앞선 턴에 공식 발언이 있는
  대화와 사람이 이름을 지은 대화는 건드리지 않습니다.
* **언제 되돌리는가** — 그 발언이 지워져 공식 발언이 하나도 남지 않을 때만. 사람이 고친 제목은 그대로입니다.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import select

from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.events import EventType
from app.orchestration.control import TurnControl
from app.orchestration.recording import TurnRecorder
from app.orchestration.session_title import (
    API_SESSION_TITLE,
    DEFAULT_SESSION_TITLE,
    MAX_TITLE_CHARS,
    PLACEHOLDER_TITLES,
    is_new_conversation,
    title_from_speech,
)
from app.orchestration.state import DebateMessage
from app.session_ops import discard_turn
from app.ui.components.sidebar import event_changes_session_list
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _engine

DB_URL = "sqlite+aiosqlite:///:memory:"
FIRST_PLAN = "## 토론 계획\n\n캐시 설계를 먼저 확정합니다. 그다음 구현합니다."
SECOND_PLAN = "로그 수집 구조를 정리합니다. 그다음 저장소를 고릅니다."


# ------------------------------------------------------------------ 무엇을 문장으로 치는가


@pytest.mark.parametrize("content, expected", [
    ("이번 토론의 목표는 캐시 설계입니다. 다음으로 넘어갑니다.", "이번 토론의 목표는 캐시 설계입니다"),
    ("## 토론 계획\n\n### 핵심 목표\n캐시 설계를 확정합니다.\n\n나머지", "캐시 설계를 확정합니다"),
    ("**핵심 목표**\n\n캐시 설계를 확정합니다.", "캐시 설계를 확정합니다"),
    ("1. **핵심 목표**\n   캐시 설계를 확정합니다.", "캐시 설계를 확정합니다"),
    ("1. 핵심 목표\n캐시 설계를 확정합니다.", "캐시 설계를 확정합니다"),
    ("핵심 목표:\n캐시 설계를 확정합니다.", "캐시 설계를 확정합니다"),
    ("**핵심 목표:** 캐시 **설계**를 `확정`합니다. 둘째", "핵심 목표: 캐시 설계를 확정합니다"),
    ("- 캐시 설계를 먼저 확정하고 구현으로 넘어갑니다.\n- 둘째", "캐시 설계를 먼저 확정하고 구현으로 넘어갑니다"),
    ("**이번 목표는 캐시 설계입니다.**\n둘째", "이번 목표는 캐시 설계입니다"),
    ("**목표:** 캐시 **설계**\n둘째", "목표: 캐시 설계"),
    ("Python 3.12 와 v1.5.1 을 기준으로 정리합니다. 다음", "Python 3.12 와 v1.5.1 을 기준으로 정리합니다"),
    ("어떤 캐시가 맞을까요? 먼저 봅니다.", "어떤 캐시가 맞을까요?"),
    ("[설계서](docs/a.md)를 기준으로 합니다.", "설계서를 기준으로 합니다"),
    ("```mermaid\ngraph TD\n A-->B.\n```\n\n다이어그램 다음 문장입니다.", "다이어그램 다음 문장입니다"),
    ("| 항목 | 값 |\n|---|---|\n| a | b |\n\n표 다음 문장입니다.", "표 다음 문장입니다"),
    ("---\n\n구분선 다음 문장입니다.", "구분선 다음 문장입니다"),
    ("마침표 없는 첫 줄\n둘째 줄입니다.", "마침표 없는 첫 줄"),
    ("A. 캐시 설계를 확정합니다. 둘째.", "A. 캐시 설계를 확정합니다"),
    ("다음과 같이 진행하겠습니다:\n- 첫째", "다음과 같이 진행하겠습니다"),
])
def test_the_first_sentence(content, expected):
    assert title_from_speech(content) == expected


@pytest.mark.parametrize("content", [
    "Thought 1: 분해한다.\nThought 2: 비교한다.\n---\n## 최종 결론\n캐시를 Redis 로 정합니다.\n둘째.",
    "> **[Sequential Thinking]**\n>\n> 캐시를 본다.\n\n캐시를 Redis 로 정합니다.",
    "<think>속으로 따져 본다.</think>\n캐시를 Redis 로 정합니다.",
    "<thinking>\n속으로 따져 본다.\n</thinking>\n\n캐시를 Redis 로 정합니다.",
    "> ⚠️ **첨부 전달 실패** — 내용 없이 진행합니다: a.png\n\n캐시를 Redis 로 정합니다.",
])
def test_thought_and_notices_are_not_sentences(content):
    assert title_from_speech(content) == "캐시를 Redis 로 정합니다"


@pytest.mark.parametrize("content", [
    "",
    "   \n  ",
    "Thought 1: 분해한다.\nThought 2: 비교한다.",
    "Revision of Thought 2: 고친다.",
    "> **[Sequential Thinking]**\n>\n> 캐시를 본다.",
    "<think>끝나지 못한 생각",
    "```\n코드뿐\n```",
])
def test_no_official_sentence_means_no_title(content):
    assert title_from_speech(content) == ""


@pytest.mark.parametrize("content, expected", [
    ("## 캐시 설계 계획", "캐시 설계 계획"),
    ("**핵심 목표**", "핵심 목표"),
    ("### 첫 머리글\n\n## 둘째 머리글", "첫 머리글"),
])
def test_only_a_heading_is_better_than_nothing(content, expected):
    assert title_from_speech(content) == expected


def test_a_long_sentence_is_clipped():
    title = title_from_speech("캐시 " * 100 + "입니다.")
    assert len(title) == MAX_TITLE_CHARS
    assert title.endswith("…")


def _message(sender_key: str, msg_type: str) -> DebateMessage:
    return DebateMessage(sender_key=sender_key, sender_name="n", sender_role="r", content="글", msg_type=msg_type)


def test_a_failed_plan_does_not_make_a_conversation_old():
    assert is_new_conversation([]) is True
    assert is_new_conversation([_message("user", "user"), _message("orchestrator", "error")]) is True
    assert is_new_conversation([_message("architect", "agent")]) is True
    assert is_new_conversation([_message("orchestrator", "orchestrator")]) is False


# ------------------------------------------------------------------ 언제 적는가


class ScriptedLLM(FakeLLMCaller):
    """오케스트레이터의 계획을 턴마다 정해 주는 대역."""

    def __init__(self, *plans: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.plans = list(plans)

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        if agent.key == "orchestrator" and "발언 지침을 작성하세요" in last and self.plans:
            return self.plans.pop(0)
        return super()._reply_for(agent, messages)


async def _session(title: str = DEFAULT_SESSION_TITLE) -> str:
    await init_db(DB_URL)
    sid = f"title-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title=title, strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator", "architect"],
        ))
        await db.commit()
    return sid


async def _title(sid: str) -> str:
    async with get_session_factory(DB_URL)() as db:
        return (await db.get(SessionModel, sid)).title


async def _rename(sid: str, title: str) -> None:
    async with get_session_factory(DB_URL)() as db:
        (await db.get(SessionModel, sid)).title = title
        await db.commit()


async def _turn(sid: str, llm: FakeLLMCaller, **kwargs: Any) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    async def on_event(event: Dict[str, Any]) -> None:
        events.append(event)

    await _engine(llm_caller=llm).run_turn(session_id=sid, user_prompt="요청", on_event=on_event, **kwargs)
    return events


def _titled(events: List[Dict[str, Any]]) -> List[str]:
    return [e["title"] for e in events if e["type"] == EventType.SESSION_TITLED]


@pytest.mark.asyncio
async def test_a_new_conversation_takes_the_first_sentence_of_the_plan():
    sid = await _session()
    events = await _turn(sid, ScriptedLLM(FIRST_PLAN))

    assert await _title(sid) == "캐시 설계를 먼저 확정합니다"
    assert _titled(events) == ["캐시 설계를 먼저 확정합니다"]


@pytest.mark.asyncio
async def test_the_screen_hears_before_the_plan_is_shown():
    sid = await _session()
    events = await _turn(sid, ScriptedLLM(FIRST_PLAN))
    types = [e["type"] for e in events]
    orchestrator_added = next(
        i for i, e in enumerate(events)
        if e["type"] == EventType.MESSAGE_ADDED and e["message"]["sender_key"] == "orchestrator"
    )
    assert types.index(EventType.SESSION_TITLED) < orchestrator_added


@pytest.mark.asyncio
async def test_an_api_conversation_without_a_name_gets_one_too():
    assert API_SESSION_TITLE in PLACEHOLDER_TITLES
    sid = await _session(API_SESSION_TITLE)
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    assert await _title(sid) == "캐시 설계를 먼저 확정합니다"


@pytest.mark.asyncio
async def test_a_named_conversation_keeps_its_name():
    """사람이 먼저 지은 이름, 쉬운·체험 화면이 정해 둔 이름."""
    sid = await _session("직접 지은 이름")
    events = await _turn(sid, ScriptedLLM(FIRST_PLAN))

    assert await _title(sid) == "직접 지은 이름"
    assert _titled(events) == []


@pytest.mark.asyncio
async def test_the_title_is_not_rewritten_by_the_next_turn():
    sid = await _session()
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    events = await _turn(sid, ScriptedLLM(SECOND_PLAN))

    assert await _title(sid) == "캐시 설계를 먼저 확정합니다"
    assert _titled(events) == []


@pytest.mark.asyncio
async def test_after_the_first_title_the_person_owns_it():
    sid = await _session()
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    await _rename(sid, "내 이름")
    await _turn(sid, ScriptedLLM(SECOND_PLAN))
    assert await _title(sid) == "내 이름"


@pytest.mark.asyncio
async def test_renaming_while_the_plan_is_written_wins():
    sid = await _session()

    class RenamesMidSpeech(ScriptedLLM):
        async def call_agent(self, agent, messages, *args, **kwargs):
            await _rename(sid, "말하는 도중 지은 이름")
            return await super().call_agent(agent, messages, *args, **kwargs)

    events = await _turn(sid, RenamesMidSpeech(FIRST_PLAN))
    assert await _title(sid) == "말하는 도중 지은 이름"
    assert _titled(events) == []


@pytest.mark.asyncio
async def test_an_old_conversation_with_the_default_name_is_left_alone():
    """이 기능 이전에 이름 없이 오래 써 온 대화. 다음 턴이 시작됐다고 이름이 바뀌면 찾지 못합니다."""
    sid = await _session()
    async with get_session_factory(DB_URL)() as db:
        db.add(MessageModel(
            id=str(uuid.uuid4()), session_id=sid, sender_key="orchestrator", sender_name="O", sender_role="R",
            content="예전 계획입니다.", round_number=0, msg_type="orchestrator",
        ))
        await db.commit()

    events = await _turn(sid, ScriptedLLM(FIRST_PLAN))
    assert await _title(sid) == DEFAULT_SESSION_TITLE
    assert _titled(events) == []


@pytest.mark.asyncio
async def test_a_failed_plan_leaves_the_name_for_the_next_turn():
    sid = await _session()
    try:
        await _turn(sid, ScriptedLLM(FIRST_PLAN, fail_keys=["orchestrator"]))
    except Exception:  # noqa: BLE001 - 턴이 어떻게 끝나든 제목만 봅니다
        pass
    assert await _title(sid) == DEFAULT_SESSION_TITLE

    await _turn(sid, ScriptedLLM(SECOND_PLAN))
    assert await _title(sid) == "로그 수집 구조를 정리합니다"


@pytest.mark.asyncio
async def test_a_plan_that_is_all_thought_waits_for_the_next_official_speech():
    sid = await _session()
    events = await _turn(sid, ScriptedLLM("Thought 1: 분해한다.\nThought 2: 비교한다."))

    assert await _title(sid) == "세 전문가의 의견을 통합했습니다"
    assert _titled(events) == ["세 전문가의 의견을 통합했습니다"]


@pytest.mark.asyncio
async def test_the_approval_card_opens_with_the_new_title(plan_approval):
    """승인 대기 알림이 대화 제목을 읽습니다. 카드가 열릴 때 이미 적혀 있어야 합니다."""
    sid = await _session()
    control = TurnControl()
    titles_at_card: List[str] = []

    async def on_event(event: Dict[str, Any]) -> None:
        if event["type"] == EventType.PLAN_APPROVAL_REQUESTED:
            titles_at_card.append(await _title(sid))
            control.resolve_plan_approval(event["id"], "approve", tasks=event["tasks"])

    await _engine(llm_caller=ScriptedLLM(FIRST_PLAN)).run_turn(
        session_id=sid, user_prompt="요청", on_event=on_event, control=control,
    )
    assert titles_at_card == ["캐시 설계를 먼저 확정합니다"]


@pytest.mark.asyncio
async def test_a_title_write_that_fails_does_not_stop_the_turn():
    class BrokenDB:
        rolled_back = False

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("database is locked")

        async def rollback(self):
            self.rolled_back = True

    db = BrokenDB()
    recorder = TurnRecorder.__new__(TurnRecorder)
    assert await recorder.title_new_session(db, "s", "제목") is False
    assert db.rolled_back is True


def test_the_list_redraws_for_the_new_title():
    assert event_changes_session_list({"type": EventType.SESSION_TITLED, "title": "t"}) is True


# ------------------------------------------------------------------ 언제 되돌리는가


async def _only_turn_id(sid: str) -> Tuple[str, ...]:
    async with get_session_factory(DB_URL)() as db:
        rows = await db.execute(select(TurnModel.id).where(TurnModel.session_id == sid))
        return tuple(rows.scalars().all())


async def _discard(sid: str, turn_id: str) -> bool:
    async with get_session_factory(DB_URL)() as db:
        return await discard_turn(db, sid, [], [], turn_id=turn_id)


@pytest.mark.asyncio
async def test_discarding_the_plan_takes_its_title_with_it():
    """계획 카드의 거부. 고쳐 보낸 요청의 대화가 거부한 계획의 이름을 달고 있으면 안 됩니다."""
    sid = await _session()
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    (turn_id,) = await _only_turn_id(sid)

    assert await _discard(sid, turn_id) is True
    assert await _title(sid) == DEFAULT_SESSION_TITLE

    await _turn(sid, ScriptedLLM(SECOND_PLAN))
    assert await _title(sid) == "로그 수집 구조를 정리합니다"


@pytest.mark.asyncio
async def test_discarding_keeps_a_name_the_person_gave():
    sid = await _session()
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    await _rename(sid, "내 이름")
    (turn_id,) = await _only_turn_id(sid)

    await _discard(sid, turn_id)
    assert await _title(sid) == "내 이름"


@pytest.mark.asyncio
async def test_discarding_a_later_turn_keeps_the_title():
    """제목을 낳은 첫 턴은 그대로 있습니다."""
    sid = await _session()
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    (first_turn,) = await _only_turn_id(sid)
    await _turn(sid, ScriptedLLM(SECOND_PLAN))
    later = next(t for t in await _only_turn_id(sid) if t != first_turn)

    await _discard(sid, later)
    assert await _title(sid) == "캐시 설계를 먼저 확정합니다"


@pytest.mark.asyncio
async def test_discarding_by_message_ids_works_the_same():
    """긴급 종료는 턴 id 가 아직 없을 때 발언 id 로 지웁니다."""
    sid = await _session()
    await _turn(sid, ScriptedLLM(FIRST_PLAN))
    async with get_session_factory(DB_URL)() as db:
        ids = list((await db.execute(select(MessageModel.id).where(MessageModel.session_id == sid))).scalars().all())
        await discard_turn(db, sid, ids)
    assert await _title(sid) == DEFAULT_SESSION_TITLE
