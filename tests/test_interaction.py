"""토론이 도는 중에 사람이 끼어드는 두 가지 경로에 대한 회귀 테스트.

1. **정지** — 태스크를 죽이지 않습니다. 진행 중인 발언은 끝까지 받고, 남은
   라운드를 건너뛰어 지금까지의 토론으로 합성까지 마칩니다. (`cancel()` 은
   여전히 "다 버리고 즉시 중단" 이고, 그건 별개입니다.)
2. **개입** — 발언과 발언 사이에 유저 발언으로 들어가, 다음 발언자의 맥락과
   최종 합성 전사에 실립니다.
"""

from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import select

from app.database.models import MessageModel
from app.database.session import get_session_factory
from app.orchestration.control import TurnControl
from app.orchestration.runner import DebateRunner
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _engine, _make_session


class HookedCaller(FakeLLMCaller):
    """어떤 에이전트가 발언을 마친 직후에 콜백을 부르는 LLM 대역.

    "3라운드짜리 토론이 도는 중간에 사람이 버튼을 눌렀다" 를 실제 타이밍에
    의존하지 않고 재현하기 위한 장치입니다. 오간 프롬프트도 그대로 모아 둡니다.
    """

    def __init__(self, *, after: str, hook, **kwargs):
        super().__init__(**kwargs)
        self.after = after
        self.hook = hook
        self.prompts: List[Tuple[str, List[Dict[str, Any]]]] = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.prompts.append((agent.key, messages))
        result = await super().call_agent(agent, messages, *args, **kwargs)
        if agent.key == self.after:
            self.hook()
        return result

    def prompt_for(self, agent_key: str, occurrence: int = 0) -> List[Dict[str, Any]]:
        found = [msgs for key, msgs in self.prompts if key == agent_key]
        assert len(found) > occurrence, f"{agent_key} 의 {occurrence + 1}번째 발언이 없습니다"
        return found[occurrence]


def _contains(prompt: List[Dict[str, Any]], needle: str) -> bool:
    return any(needle in m.get("content", "") for m in prompt)


# --------------------------------------------------------------- 정지


@pytest.mark.asyncio
async def test_stop_request_skips_remaining_rounds_but_still_synthesizes():
    """정지 요청은 토론을 죽이는 대신 합성으로 건너뜁니다."""
    sid = await _make_session(max_rounds=3, strategy="sequential_debate")

    holder: Dict[str, Any] = {}
    caller = HookedCaller(after="architect", hook=lambda: holder["run"].request_stop())
    runner = DebateRunner(_engine(llm_caller=caller))

    run = runner.start(sid, "분산 캐시 설계")
    holder["run"] = run          # 태스크는 아직 첫 await 전이라 안전합니다
    await run.task

    # 태스크가 취소된 것이 아니라 정상 종료입니다.
    assert run.status == "completed"

    # 아키텍트까지만 발언하고 남은 발언자와 라운드는 건너뛰었습니다.
    assert "coder" not in caller.calls
    assert "critic" not in caller.calls
    # 계획 발언과 최종 합성은 그대로 있고, 합성 뒤에 결정 장부를 한 번 갱신합니다.
    # 정지로 건너뛴 라운드의 장부 갱신은 없습니다.
    assert caller.calls.count("orchestrator") == 3
    assert _contains(caller.prompt_for("orchestrator", occurrence=2), "[결정 장부 갱신]")

    snapshot = run.snapshot()
    assert snapshot["stop_requested"] is True
    assert snapshot["round_info"] == "Stopped"
    assert snapshot["artifacts"], "정지해도 지금까지의 토론으로 산출물이 나와야 합니다"


@pytest.mark.asyncio
async def test_stopped_synthesis_prompt_admits_the_debate_was_cut_short():
    """덜 논의된 상태라는 사실이 합성 프롬프트에 실려야 합니다.

    이 문구가 없으면 오케스트레이터가 열리지도 않은 라운드의 결론을 지어냅니다.
    """
    sid = await _make_session(max_rounds=3, strategy="sequential_debate")

    control = TurnControl()
    caller = HookedCaller(after="architect", hook=control.request_stop)
    state = await _engine(llm_caller=caller).run_turn(
        session_id=sid, user_prompt="이벤트 드리븐 아키텍처", control=control
    )

    assert state.stopped_early is True
    assert state.current_round == 1
    # 도중에 끊었으므로 합의에 이른 것으로 치지 않습니다.
    assert state.is_consensus_reached is False

    synthesis_prompt = caller.prompt_for("orchestrator", occurrence=1)
    assert _contains(synthesis_prompt, "일찍 토론을 정지")
    assert _contains(synthesis_prompt, "1/3 라운드")


@pytest.mark.asyncio
async def test_stop_requested_before_the_debate_still_yields_artifacts():
    """첫 라운드에 들어가기도 전에 눌러도 기획 발언 + 합성은 나옵니다."""
    sid = await _make_session(max_rounds=2)

    control = TurnControl()
    control.request_stop()
    caller = HookedCaller(after="nobody", hook=lambda: None)
    state = await _engine(llm_caller=caller).run_turn(
        session_id=sid, user_prompt="설계해줘", control=control
    )

    assert state.stopped_early is True
    # 계획 → 합성 → 결정 장부 갱신. 라운드가 없었으므로 라운드 장부 갱신도 없습니다.
    assert caller.calls == ["orchestrator", "orchestrator", "orchestrator"]
    assert _contains(caller.prompt_for("orchestrator", occurrence=2), "[결정 장부 갱신]")
    assert state.artifacts


@pytest.mark.asyncio
async def test_stop_is_inert_once_the_turn_is_over():
    """끝난 토론이나 없는 세션에 대고 눌러도 조용히 False 입니다."""
    sid = await _make_session(max_rounds=1)
    runner = DebateRunner(_engine(llm_caller=FakeLLMCaller()))

    run = runner.start(sid, "설계해줘")
    await run.task

    assert runner.request_stop(sid) is False
    assert runner.interject(sid, "늦은 개입") is False
    assert runner.request_stop("존재하지-않는-세션") is False


# --------------------------------------------------------------- 개입


@pytest.mark.asyncio
async def test_interjection_reaches_the_next_speaker_but_not_the_previous_one():
    """개입은 다음 발언자의 맥락에 실립니다. 이미 끝난 발언은 건드리지 않습니다."""
    sid = await _make_session(max_rounds=1, strategy="sequential_debate")

    control = TurnControl()
    caller = HookedCaller(
        after="architect",
        hook=lambda: control.add_note("Redis 말고 Postgres 로 갑시다"),
    )
    state = await _engine(llm_caller=caller).run_turn(
        session_id=sid, user_prompt="캐시 계층 설계", control=control
    )

    assert state.interjection_count == 1

    # 아키텍트는 개입 전에 발언했으므로 그의 프롬프트에는 없습니다.
    assert not _contains(caller.prompt_for("architect"), "Postgres")
    # 바로 다음 발언자인 코더부터 보게 됩니다.
    assert _contains(caller.prompt_for("coder"), "Postgres")
    assert _contains(caller.prompt_for("critic"), "Postgres")
    # 최종 합성 전사에도 남습니다.
    assert _contains(caller.prompt_for("orchestrator", occurrence=1), "Postgres")

    # 유저 발언으로 DB 에 남아, 새로고침이나 다음 턴에서도 보입니다.
    async with get_session_factory()() as db:
        res = await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id == sid, MessageModel.sender_key == "user")
            .order_by(MessageModel.created_at)
        )
        user_msgs = res.scalars().all()
    assert [m.content for m in user_msgs][0] == "캐시 계층 설계"
    assert "Postgres" in user_msgs[1].content
    assert user_msgs[1].msg_type == "user"


@pytest.mark.asyncio
async def test_runner_queues_interjections_and_tells_the_screens():
    """개입은 대기 상태로 구독자에게 먼저 알려지고, 반영될 때 발언으로 도착합니다."""
    sid = await _make_session(max_rounds=1)
    runner = DebateRunner(_engine(llm_caller=FakeLLMCaller()))

    run = runner.start(sid, "설계해줘")
    queue = run.subscribe()

    assert run.interject("  보안 관점도 봐 주세요  ") is True
    assert run.control.pending_notes == ["보안 관점도 봐 주세요"]
    assert run.interject("   ") is False, "빈 개입은 대기열에 넣지 않습니다"

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(e["type"] == "interjection_queued" and e["pending"] == 1 for e in events)

    await run.task
    assert run.control.pending_notes == []
    assert any(
        m["sender_key"] == "user" and "보안 관점" in m["content"]
        for m in run.snapshot()["messages"]
    )


@pytest.mark.asyncio
async def test_interjection_arriving_during_synthesis_is_kept_not_dropped():
    """합성이 시작된 뒤에 도착한 개입은 이번 턴에 실을 자리가 없습니다.

    그대로 버리면 화면은 "다음 발언 차례에 반영됩니다" 라고 알린 채 턴이 끝납니다.
    기록에 남겨 두면 다음 턴이 맥락으로 읽어 갑니다.
    """
    sid = await _make_session(max_rounds=1)
    control = TurnControl()

    class SynthesisHook(FakeLLMCaller):
        async def call_agent(self, agent, messages, *args, **kwargs):
            last = messages[-1]["content"] if messages else ""
            if "최종 합의 보고서" in last:
                control.add_note("합성 중에 도착한 개입")
            return await super().call_agent(agent, messages, *args, **kwargs)

    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await _engine(llm_caller=SynthesisHook()).run_turn(
        session_id=sid, user_prompt="설계해줘", on_event=on_event, control=control
    )

    assert state.interjection_count == 1
    assert control.pending_notes == [], "대기열에 남겨 두면 영영 반영되지 않습니다"
    # 합성 발언 뒤에 유저 발언으로 붙습니다.
    assert state.messages[-1].sender_key == "user"
    assert "합성 중에 도착한 개입" in state.messages[-1].content
    # 화면에 사실대로 알립니다.
    deferred = [e for e in events if e["type"] == "interjections_deferred"]
    assert deferred and deferred[0]["count"] == 1

    # 다음 턴이 읽을 수 있도록 DB 에도 남습니다.
    async with get_session_factory()() as db:
        res = await db.execute(
            select(MessageModel).where(
                MessageModel.session_id == sid, MessageModel.sender_key == "user"
            )
        )
        assert any("합성 중에 도착한 개입" in m.content for m in res.scalars().all())


@pytest.mark.asyncio
async def test_interjection_during_synthesis_is_announced_as_deferred():
    """대기 알림도 사실대로 나가야 합니다 (이번 턴이 아니라 다음 요청부터)."""
    sid = await _make_session(max_rounds=1)
    runner = DebateRunner(_engine(llm_caller=FakeLLMCaller()))
    run = runner.start(sid, "설계해줘")
    queue = run.subscribe()

    run.apply({"type": "status_changed", "status": "synthesizing", "speaker": "Orchestrator"})
    assert run.interject("합성 중 개입") is True

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    queued = [e for e in events if e["type"] == "interjection_queued"]
    assert queued and queued[-1]["deferred"] is True

    await run.task


# --------------------------------------------------------------- 잠금 창


@pytest.mark.asyncio
async def test_running_sessions_marks_the_window_where_mcp_config_is_locked():
    """MCP 서버 구성을 잠글 창을 정하는 것이 이 목록입니다.

    토론이 도는 동안은 그 도구들이 쓰이는 중이라 서버를 내리거나 다시 띄우면
    안 됩니다. 창이 닫히는 시점은 최종 아티팩트가 나온 뒤여야 합니다.
    """
    sid = await _make_session(max_rounds=1)
    runner = DebateRunner(_engine(llm_caller=FakeLLMCaller()))
    assert runner.running_sessions() == []

    run = runner.start(sid, "설계해줘")
    assert runner.running_sessions() == [sid]

    await run.task
    assert run.snapshot()["artifacts"], "아티팩트가 나오기 전에 잠금이 풀리면 안 됩니다"
    assert runner.running_sessions() == []


@pytest.mark.asyncio
async def test_stopped_debate_also_releases_the_lock():
    """사용자가 정지시킨 토론도 합성까지 마친 뒤 잠금을 풉니다."""
    sid = await _make_session(max_rounds=3, strategy="sequential_debate")

    holder: Dict[str, Any] = {}
    caller = HookedCaller(after="architect", hook=lambda: holder["run"].request_stop())
    runner = DebateRunner(_engine(llm_caller=caller))

    run = runner.start(sid, "설계해줘")
    holder["run"] = run
    assert runner.running_sessions() == [sid]

    await run.task
    assert run.snapshot()["artifacts"]
    assert runner.running_sessions() == []


# --------------------------------------------------------------- TurnControl 단위


def test_turn_control_drains_once():
    control = TurnControl()
    assert control.stop_requested is False
    assert control.add_note("첫 번째") is True
    assert control.add_note("") is False
    assert control.add_note("   \n ") is False
    assert control.add_note("두 번째") is True

    assert control.drain_notes() == ["첫 번째", "두 번째"]
    assert control.drain_notes() == [], "한 번 꺼낸 메모가 다시 나오면 안 됩니다"

    control.request_stop()
    assert control.stop_requested is True
