"""사고 과정(Sequential Thinking)은 기록에 남고 프롬프트에는 실리지 않는다.

`show_steps` 는 **사람이 무엇을 볼지**를 정하는 스위치입니다. 모델이 무엇을
읽을지를 정하는 스위치가 아닙니다. 그런데 예전에는 둘이 붙어 있었습니다 —
`show_steps: true` 면 `Thought 1..N` 전문이 발언 본문이 되고, 그 본문을
`_build_context_for_agent` 가 다음 발언자 전원의 맥락으로 그대로 복사했습니다.

그 결과:

* 라운드가 갈수록 전사의 대부분이 '남이 어떻게 생각했는지' 로 채워지고,
  `fit_context_window` 가 정작 필요한 초반 목표·설계 논의부터 버렸습니다.
* 발언자 지명·과업 분배·계획 프롬프트는 발언을 250~300자로 잘라 넣는데,
  그 앞머리가 전부 "Thought 1: ..." 이라 **결론은 한 글자도 실리지 않았습니다.**

다음 발언자에게 필요한 것은 앞사람의 결론과 근거이지, 그가 5단계로 어떻게
거기 도달했는가가 아닙니다 (순차 토론 지침도 "결론을 입력으로 받으라" 고
말합니다). 그래서 사고 과정은 DB 와 화면에만 남기고 프롬프트에서 뗍니다.
"""

import uuid

import pytest

from app.agents.base import Agent
from app.agents.llm import LLMCaller, strip_reasoning_trace
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateMessage, DebateState


PROMPT_MODE = (
    "Thought 1: 요구사항을 분해한다.\n"
    "Thought 2: 후보 기술을 비교한다.\n"
    "Revision of Thought 2: 비용 축을 빠뜨렸다.\n"
    "\n---\n"
    "## 최종 결론\n"
    "Redis 를 세션 캐시로 씁니다."
)

NATIVE_MODE = (
    "> **[Sequential Thinking]**\n"
    ">\n"
    "> 캐시 계층을 먼저 본다.\n"
    "> TTL 이 관건이다.\n"
    "\n"
    "Redis 를 세션 캐시로 씁니다."
)


# ------------------------------------------------------------------ 헬퍼 자체


def test_strips_prompt_mode_protocol():
    assert strip_reasoning_trace(PROMPT_MODE) == "## 최종 결론\nRedis 를 세션 캐시로 씁니다."


def test_strips_native_mode_quote_block():
    assert strip_reasoning_trace(NATIVE_MODE) == "Redis 를 세션 캐시로 씁니다."


def test_strips_both_shapes_at_once():
    both = NATIVE_MODE.replace(
        "Redis 를 세션 캐시로 씁니다.",
        "Thought 1: 재확인\n---\n## Final Conclusion\nDone.",
    )
    assert strip_reasoning_trace(both) == "## Final Conclusion\nDone."


def test_leaves_ordinary_speech_alone():
    plain = "그냥 평범한 발언입니다.\n\n```python\nprint(1)\n```"
    assert strip_reasoning_trace(plain) == plain


def test_does_not_eat_an_answer_that_starts_with_a_quote():
    """답변 자체가 인용문으로 시작할 수 있습니다. 머리표가 있을 때만 손댑니다."""
    quoted = "> 사용자의 요구를 그대로 인용하면:\n\n이렇게 하겠습니다."
    assert strip_reasoning_trace(quoted) == quoted


def test_keeps_the_trace_when_there_is_nothing_else():
    """생각만 있고 답이 없으면 원문을 지킵니다. 빈 발언을 넘길 수는 없습니다."""
    only = "> **[Sequential Thinking]**\n>\n> 생각만 하다 끝났다"
    assert strip_reasoning_trace(only) == only


def test_earliest_marker_wins_regardless_of_marker_order():
    """마커 목록의 순서가 아니라 본문에서의 위치가 기준입니다."""
    text = "Thought 1\n## Final Conclusion\nA\n## 최종 결론\nB"
    assert strip_reasoning_trace(text).startswith("## Final Conclusion")


# ----------------------------------------------- show_steps 와의 관계 (독립)


def test_show_steps_true_still_keeps_the_trace_in_the_record():
    """화면·DB 에 남는 본문은 `show_steps` 가 정합니다. 프롬프트와는 별개입니다."""
    caller = LLMCaller.__new__(LLMCaller)
    agent = Agent(
        key="coder", name="Coder", role="Dev",
        sequential_thinking={"enabled": True, "mode": "prompt", "show_steps": True},
    )
    assert caller._apply_show_steps(agent, PROMPT_MODE) == PROMPT_MODE
    assert "Thought 1" in caller._apply_show_steps(agent, PROMPT_MODE)


# ------------------------------------------------------- 엔진의 프롬프트 자리


def _state_with_reasoning() -> DebateState:
    state = DebateState(
        session_id="s1", user_prompt="세션 캐시를 설계하라",
        strategy="sequential_debate", max_rounds=2, current_round=1,
    )
    state.messages.append(DebateMessage(
        id="m1", sender_key="architect", sender_name="Architect", sender_role="Design",
        content=PROMPT_MODE, round_number=1, msg_type="agent",
    ))
    state.messages.append(DebateMessage(
        id="m2", sender_key="coder", sender_name="Coder", sender_role="Dev",
        content=NATIVE_MODE, round_number=1, msg_type="agent",
    ))
    return state


def _engine() -> OrchestratorEngine:
    pool = AgentPool({
        key: AgentConfig(name=key.title(), role="R", model="fake/model", api_key="k")
        for key in ("orchestrator", "architect", "coder", "critic")
    })
    return OrchestratorEngine(agent_pool=pool)


def test_next_speaker_context_carries_conclusions_only():
    engine = _engine()
    state = _state_with_reasoning()
    critic = engine.agent_pool.get("critic")

    context = engine._build_context_for_agent(state, critic)
    blob = "\n".join(str(m["content"]) for m in context)

    assert "Thought 1" not in blob
    assert "Revision of Thought" not in blob
    assert "[Sequential Thinking]" not in blob
    assert "TTL 이 관건이다" not in blob
    # 결론은 두 발언 모두 살아 있어야 합니다.
    assert blob.count("Redis 를 세션 캐시로 씁니다.") == 2


def test_an_agent_does_not_get_its_own_trace_back_either():
    """자기 발언도 같습니다 — 기록에는 있고 프롬프트에는 없습니다."""
    engine = _engine()
    state = _state_with_reasoning()
    architect = engine.agent_pool.get("architect")

    context = engine._build_context_for_agent(state, architect)
    own = [m for m in context if m["role"] == "assistant"]

    assert own, "자기 발언이 assistant 역할로 들어가야 합니다"
    assert "Thought 1" not in own[0]["content"]
    assert "Redis 를 세션 캐시로 씁니다." in own[0]["content"]
    # 기록 자체는 손대지 않습니다.
    assert "Thought 1" in state.messages[0].content


def test_synthesis_transcript_carries_conclusions_only():
    engine = _engine()
    state = _state_with_reasoning()

    prompt = engine._build_synthesis_prompt(state)
    blob = "\n".join(str(m["content"]) for m in prompt)

    assert "Thought 1" not in blob
    assert "[Sequential Thinking]" not in blob
    assert "Redis 를 세션 캐시로 씁니다." in blob


@pytest.mark.asyncio
async def test_speaker_selection_prompt_sees_the_conclusion_not_the_preamble():
    """250~300자로 자르는 자리들. 자르기 **전에** 떼지 않으면 결론이 안 실립니다."""
    engine = _engine()
    state = _state_with_reasoning()
    orchestrator = engine.agent_pool.get_orchestrator()

    captured: list = []

    class _Recorder:
        async def call_agent(self, agent, messages, custom_instructions="", **kwargs):
            captured.append(messages)
            return '{"speakers": ["critic"], "reason": "검증 차례"}', []

    engine.llm_caller = _Recorder()
    await engine._ask_orchestrator_for_speakers(
        orchestrator=orchestrator,
        candidates=[engine.agent_pool.get("critic")],
        state=state,
        round_num=2,
        custom_instructions="",
    )

    blob = "\n".join(str(m["content"]) for m in captured[0])
    assert "Thought 1" not in blob
    assert "Redis 를 세션 캐시로 씁니다." in blob


@pytest.mark.asyncio
async def test_the_full_trace_is_still_what_gets_persisted():
    """프롬프트에서 뗀다고 기록까지 사라지면 안 됩니다 (화면이 그걸 그립니다)."""
    await init_db("sqlite+aiosqlite:///:memory:")
    factory = get_session_factory("sqlite+aiosqlite:///:memory:")
    sid = f"reasoning-{uuid.uuid4().hex[:8]}"

    async with factory() as db:
        db.add(SessionModel(id=sid, title="T", strategy="sequential_debate", max_rounds=1,
                            active_agents=["orchestrator", "architect"]))
        await db.commit()

    engine = _engine()
    state = DebateState(session_id=sid, user_prompt="q", strategy="sequential_debate",
                        max_rounds=1, current_round=1)

    class _Thinker:
        async def call_agent(self, agent, messages, custom_instructions="", **kwargs):
            return PROMPT_MODE, []

    engine.llm_caller = _Thinker()

    async with factory() as db:
        await engine._speak(
            db=db, state=state, agent=engine.agent_pool.get("architect"),
            prompt_messages=[{"role": "user", "content": "q"}],
            custom_instructions="", round_number=1, msg_type="agent", on_event=None,
        )
        rows = (await db.execute(
            MessageModel.__table__.select().where(MessageModel.session_id == sid)
        )).fetchall()

    assert len(rows) == 1
    assert "Thought 1" in rows[0].content
    assert "Revision of Thought" in rows[0].content
