"""서브 토론 — 쟁점 하나를 전문가 두 명이 따로 다루고 결론만 합류시킨다 (ADR-034).

지키려는 것.

1. **꺼져 있으면 아무 일도 없다** — 켜지 않은 대화는 호출도 프롬프트도 그대로다.
2. **쟁점이 있을 때만 묻는다** — 장부에 미해결 쟁점이 없으면 LLM 을 부르지 않는다. 형식을 어긴 답으로 열지 않는다.
3. **결론만 합류한다** — 안의 발언은 화면과 저장 문서에만 남고, 다른 발언자·장부·합성에는 결론만 간다. 그래프
   노드는 선이 무엇을 싣든 결론을 받는다.
4. **유한하다** — 정해 둔 라운드까지, 턴당 두 번, 예산을 다 썼으면 열지 않고 더 돌지 않는다. 가지를 다시 치지 않는다.
   라운드 수는 본 토론의 라운드 수와 따로 정하고, 서브 토론이 본 토론의 라운드를 쓰지 않는다.
5. **실패와 재개** — 응답을 못 받으면 있는 만큼으로 닫고 지어내지 않는다. 끊기면 다시 묻지 않고 남은 발언부터 이어 간다.
"""

import asyncio
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app import graph_store
from app.agents.pool import AgentPool
from app.config import (
    DEFAULT_SUB_DEBATE_ROUNDS,
    MAX_SUB_DEBATE_ROUNDS,
    PEERS_TOOL_PREFIX,
    AgentConfig,
    clamp_sub_debate_rounds,
)
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.export import build_session_markdown
from app.orchestration import turns
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.graph import parse_graph
from app.orchestration.phases.routing import parse_sub_debate
from app.orchestration.phases.sub_debate import (
    MAX_SUB_DEBATES_PER_TURN,
    SPEECHES_PER_ROUND,
    SUB_DEBATE_CONCLUSION_MARKER,
    SUB_DEBATE_PROMPT_MARKER,
    SUB_DEBATE_TOOL_ITERATIONS,
    SubDebateRunner,
)
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"
# 서브 토론 안의 발언 지시 ("[서브 토론 1/4]"). 판단·결론 호출과 구분합니다.
SIDE_SPEECH = re.compile(r"\[서브 토론 \d")
DEFAULT_SPEECHES = DEFAULT_SUB_DEBATE_ROUNDS * SPEECHES_PER_ROUND

OPEN_LEDGER = """## 요구사항·제약
- 없음

## 결정 사항
- 없음

## 기각된 대안
- 없음

## 미해결 쟁점
- 인증 토큰을 어디에 저장할지

## 담당·다음 할 일
- 없음
"""
OPEN_ANSWER = json.dumps({
    "open": True, "issue": "토큰 저장 위치", "agents": ["architect", "critic"], "reason": "두 분야가 부딪힙니다",
}, ensure_ascii=False)


def _pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=name, role=role, model="fake/model", api_key="k")
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("architect", "System Architect", "Architecture"),
            ("coder", "Senior Engineer", "Implementation"),
            ("critic", "Quality Critic", "Review"),
        )
    })


class IssueLLM(FakeLLMCaller):
    """장부에 쟁점을 남기고, 서브 토론을 열겠다고 답합니다. 서브 토론 발언은 알아볼 수 있게 적습니다."""

    def __init__(self, *, ledger: str = OPEN_LEDGER, answer: str = OPEN_ANSWER,
                 fail_side: Tuple[str, ...] = (), crash_at_side: Optional[int] = None,
                 on_side=None, **kwargs):
        super().__init__(**kwargs)
        self.ledger, self.answer = ledger, answer
        self.fail_side = set(fail_side)
        self.crash_at_side = crash_at_side
        self.on_side = on_side
        self.side_count = 0
        self.sent: List[Tuple[str, List[Dict[str, Any]]]] = []

    def _text(self, messages) -> str:
        return "\n".join(m["content"] for m in messages)

    @staticmethod
    def _is_side_speech(last: str) -> bool:
        return SIDE_SPEECH.search(last) is not None

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.sent.append((agent.key, messages))
        last = messages[-1]["content"]
        if self._is_side_speech(last):
            self.side_count += 1
            if self.crash_at_side == self.side_count:
                raise asyncio.CancelledError()
            if self.on_side is not None:
                self.on_side(self.side_count)
            if agent.key in self.fail_side:
                from app.agents.llm_types import LLMUnavailableError

                raise LLMUnavailableError(agent, "APIConnectionError: 500")
        return await super().call_agent(agent, messages, *args, **kwargs)

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"]
        if "[결정 장부 갱신]" in last:
            return self.ledger
        if SUB_DEBATE_PROMPT_MARKER in last:
            return self.answer
        if SUB_DEBATE_CONCLUSION_MARKER in last:
            return "합의: 토큰은 서버 세션에 둡니다."
        if self._is_side_speech(last):
            return f"{agent.name} 의 서브 토론 비밀 발언 {self.side_count}"
        if last.startswith("[판정]"):
            return '{"decision": "yes", "reason": "ok"}'
        return super()._reply_for(agent, messages)

    def asked(self) -> int:
        return sum(1 for _k, m in self.sent if SUB_DEBATE_PROMPT_MARKER in m[-1]["content"])

    def prompts_with(self, needle: str) -> List[str]:
        return [self._text(m) for _k, m in self.sent if needle in m[-1]["content"]]

    def side_prompts(self) -> List[str]:
        return [self._text(m) for _k, m in self.sent if self._is_side_speech(m[-1]["content"])]


async def _session(strategy="sequential_debate", max_rounds=2, sub_debates=True,
                   agents=("orchestrator", "architect", "coder", "critic"), **columns) -> str:
    await init_db(DB_URL)
    sid = f"side-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="Side", strategy=strategy, max_rounds=max_rounds, active_agents=list(agents),
            sub_debates=sub_debates, converge_early=False, **columns,
        ))
        await db.commit()
    return sid


async def _run(llm, sid, control=None):
    return await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="로그인을 설계해줘", control=control,
    )


async def _rows(sid):
    async with get_session_factory(DB_URL)() as db:
        return (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()


def _kinds(rows) -> List[str]:
    return [(m.turn_meta or {}).get("kind") for m in rows]


# =============================================================== 1. 꺼져 있으면


@pytest.mark.asyncio
async def test_off_by_default_and_nothing_changes_when_off():
    assert SessionModel(id="x").sub_debates in (None, False), "켜야 돕니다"
    sid = await _session(sub_debates=False)
    llm = IssueLLM()
    await _run(llm, sid)
    assert llm.asked() == 0 and llm.side_count == 0
    assert not any(k and k.startswith("subdebate") for k in _kinds(await _rows(sid)))


# =============================================================== 2. 언제 여는가


@pytest.mark.asyncio
async def test_without_open_issues_the_model_is_not_even_asked():
    sid = await _session()
    llm = IssueLLM(ledger=OPEN_LEDGER.replace("- 인증 토큰을 어디에 저장할지", "- 없음"))
    await _run(llm, sid)
    assert llm.asked() == 0


@pytest.mark.parametrize("content", [
    '{"open": false}',
    "네, 여는 편이 좋겠습니다. architect 와 critic 이요.",
    '{"open": "true", "issue": "x", "agents": ["architect", "critic"]}',
    '{"open": true, "issue": "x", "agents": ["architect"]}',
    '{"open": true, "issue": "x", "agents": ["architect", "architect"]}',
    '{"open": true, "issue": "x", "agents": ["architect", "coder", "critic"]}',
    '{"open": true, "issue": "x", "agents": ["architect", "orchestrator"]}',
    '{"open": true, "issue": " ", "agents": ["architect", "critic"]}',
    '{"open": true, "issue": "x", "agents": "architect, critic"}',
])
def test_only_a_well_formed_yes_opens_one(content):
    agents = [a for a in _pool().list_all() if a.key != "orchestrator"]
    assert parse_sub_debate(content, agents) is None


def test_a_well_formed_yes_names_the_two_and_the_issue():
    agents = [a for a in _pool().list_all() if a.key != "orchestrator"]
    pair, issue, reason = parse_sub_debate("좋습니다.\n" + OPEN_ANSWER, agents)
    assert [a.key for a in pair] == ["architect", "critic"]
    assert issue == "토큰 저장 위치" and reason == "두 분야가 부딪힙니다"


@pytest.mark.asyncio
async def test_a_no_leaves_no_trace():
    sid = await _session()
    llm = IssueLLM(answer='{"open": false}')
    await _run(llm, sid)
    assert llm.asked() == 1 and llm.side_count == 0
    assert not any(k and k.startswith("subdebate") for k in _kinds(await _rows(sid)))


@pytest.mark.asyncio
async def test_it_needs_two_specialists():
    sid = await _session(agents=("orchestrator", "coder"))
    llm = IssueLLM()
    await _run(llm, sid)
    assert llm.asked() == 0


@pytest.mark.asyncio
async def test_a_spent_token_budget_opens_nothing():
    """예산을 다 쓰면 라운드 경계에서 끝나므로, 쓰고 있는 예산이 있는 턴만 봅니다."""
    sid = await _session(token_budget=10 ** 9)
    llm = IssueLLM(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    runner = SubDebateRunner(None, None, None, None)
    assert runner is not None
    await _run(llm, sid)
    assert llm.asked() == 1, "예산이 남아 있으면 엽니다"

    from app.orchestration.turn_setup import TurnSetup
    from app.orchestration.state import DebateState

    agents = _pool().list_all()
    setup = TurnSetup(
        strategy_name="sequential_debate", max_rounds=2, parallel_limit=3, active_keys=[a.key for a in agents],
        custom_instructions="", active_agents=agents, orchestrator=agents[0], graph_spec=None,
        memory_budget=0, token_budget=10, sub_debates=True,
    )
    state = DebateState(session_id="s", user_prompt="p", decision_ledger=OPEN_LEDGER)
    state.usage.prompt_tokens = 10
    assert await runner.open_if_needed(None, state, setup, 1, control=None, on_event=None) is None


@pytest.mark.asyncio
async def test_at_most_two_per_turn():
    sid = await _session(max_rounds=5)
    llm = IssueLLM()
    await _run(llm, sid)
    assert llm.asked() == MAX_SUB_DEBATES_PER_TURN
    assert _kinds(await _rows(sid)).count(turns.KIND_SUBDEBATE_OPEN) == MAX_SUB_DEBATES_PER_TURN


@pytest.mark.asyncio
async def test_not_after_the_last_round():
    sid = await _session(max_rounds=1)
    llm = IssueLLM()
    await _run(llm, sid)
    assert llm.asked() == 0, "합류할 다음 차례가 없습니다"


# =============================================================== 3. 흐름과 합류


@pytest.mark.asyncio
async def test_two_specialists_take_turns_and_the_orchestrator_concludes():
    sid = await _session()
    llm = IssueLLM()
    state = await _run(llm, sid)

    rows = await _rows(sid)
    side = [m for m in rows if (m.turn_meta or {}).get("kind", "").startswith("subdebate")]
    assert [(m.turn_meta["kind"], m.sender_key) for m in side] == [
        (turns.KIND_SUBDEBATE_OPEN, "orchestrator"),
        (turns.KIND_SUBDEBATE_SPEECH, "architect"),
        (turns.KIND_SUBDEBATE_SPEECH, "critic"),
        (turns.KIND_SUBDEBATE_SPEECH, "architect"),
        (turns.KIND_SUBDEBATE_SPEECH, "critic"),
        (turns.KIND_SUBDEBATE_CONCLUSION, "orchestrator"),
    ], "기본은 2라운드입니다"
    assert {m.round_number for m in side} == {1}, "라운드 1 경계에서 열렸습니다"
    assert side[0].turn_meta["issue"] == "토큰 저장 위치"
    assert side[-1].content.startswith("[서브 토론 결론 · 토큰 저장 위치]"), "결론만 읽어도 무슨 쟁점인지 압니다"
    assert state.status == "completed" and not state.failed_agent_keys


@pytest.mark.asyncio
async def test_the_participants_see_the_issue_the_exchange_and_not_the_whole_transcript():
    sid = await _session()
    llm = IssueLLM()
    await _run(llm, sid)

    speech_prompts = llm.side_prompts()
    assert len(speech_prompts) == DEFAULT_SPEECHES
    assert "쟁점: 토큰 저장 위치" in speech_prompts[0] and "Quality Critic" in speech_prompts[0]
    assert "System Architect 의 서브 토론 비밀 발언 1" in speech_prompts[1], "두 번째 사람은 첫 발언을 읽습니다"
    assert "[직전 논의]" in speech_prompts[0], "쟁점이 나온 논의는 함께 받습니다"


@pytest.mark.asyncio
async def test_only_the_conclusion_joins_the_main_debate():
    sid = await _session()
    llm = IssueLLM()
    await _run(llm, sid)

    secret = "서브 토론 비밀 발언"
    round_two = [m for k, m in llm.sent if "Round 2 of 2" in m[-1]["content"]]
    assert len(round_two) == 3
    for prompt in round_two:
        text = "\n".join(m["content"] for m in prompt)
        assert secret not in text, "참여자를 포함해 다음 라운드에는 안의 발언이 없습니다"
        assert "합의: 토큰은 서버 세션에 둡니다." in text, "결론은 모두가 읽습니다"

    ledger_prompts = llm.prompts_with("[결정 장부 갱신]")
    assert any("합의: 토큰은 서버 세션에 둡니다." in p for p in ledger_prompts)
    assert not any(secret in p for p in ledger_prompts), "합의되지 않은 중간 입장이 장부로 가지 않습니다"

    synthesis = llm.prompts_with("최종 합의 보고서")[0]
    assert secret not in synthesis and "합의: 토큰은 서버 세션에 둡니다." in synthesis


@pytest.mark.asyncio
async def test_the_ledger_is_updated_right_after_the_conclusion():
    sid = await _session()
    llm = IssueLLM()
    await _run(llm, sid)
    order = []
    for _k, messages in llm.sent:
        last = messages[-1]["content"]
        if SUB_DEBATE_CONCLUSION_MARKER in last:
            order.append("conclusion")
        elif "[결정 장부 갱신]" in last:
            order.append("ledger")
        elif "Round 2 of 2" in last:
            order.append("round2")
    at = order.index("conclusion")
    assert order[at + 1] == "ledger" and order[at + 2] == "round2", "다음 발언자가 낡은 장부를 읽지 않습니다"


@pytest.mark.asyncio
async def test_a_side_speech_does_not_count_as_answering_the_main_debate():
    """서브 토론에서 말했다고 앞선 본 토론 발언이 '이미 읽은 것' 으로 바뀌면 요지로 줄어듭니다."""
    sid = await _session()
    long_body = "가" * 900 + "\n\n## 요지\n- 코더의 요지\n"
    llm = IssueLLM(replies={"coder": long_body})
    await _run(llm, sid)
    architect_round_two = next(
        m for k, m in llm.sent if k == "architect" and "Round 2 of 2" in m[-1]["content"]
    )
    text = "\n".join(m["content"] for m in architect_round_two)
    assert "가" * 900 in text, "아키텍트는 라운드 1 뒤의 코더 발언에 아직 답하지 않았습니다"


def test_inside_a_sub_debate_no_further_branches_and_fewer_tools():
    agent = _pool().get("architect").model_copy(update={
        "allowed_mcp_servers": ["filesystem", PEERS_TOOL_PREFIX], "allow_peer_consultation": True,
        "max_tool_iterations": 25,
    })
    narrowed = SubDebateRunner.narrowed(agent)
    assert narrowed.allowed_mcp_servers == ["filesystem"], "도구는 쓰되 자문은 뺍니다"
    assert narrowed.allow_peer_consultation is False
    assert narrowed.max_tool_iterations == SUB_DEBATE_TOOL_ITERATIONS
    assert agent.allowed_mcp_servers == ["filesystem", PEERS_TOOL_PREFIX], "대화의 에이전트는 그대로입니다"


# =============================================================== 3-b. 라운드 수


def _side_speakers(rows) -> List[str]:
    return [m.sender_key for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SUBDEBATE_SPEECH]


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [1, 3])
async def test_the_setting_decides_how_many_rounds_they_exchange(rounds):
    sid = await _session(sub_debate_rounds=rounds)
    llm = IssueLLM()
    await _run(llm, sid)
    rows = await _rows(sid)
    assert _side_speakers(rows) == ["architect", "critic"] * rounds, "한 라운드에 두 사람이 한 번씩 말합니다"
    assert _kinds(rows).count(turns.KIND_SUBDEBATE_CONCLUSION) == 1
    opening = next(m for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SUBDEBATE_OPEN)
    assert f"최대 {rounds}라운드" in opening.content
    assert f"[서브 토론 {rounds * 2}/{rounds * 2}]" in llm.side_prompts()[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_rounds, side_rounds, opened", [(2, 4, 1), (3, 1, 2)])
async def test_side_rounds_are_set_and_counted_apart_from_the_main_rounds(max_rounds, side_rounds, opened):
    """어느 쪽을 늘리거나 줄여도 다른 쪽의 수는 그대로입니다."""
    sid = await _session(max_rounds=max_rounds, sub_debate_rounds=side_rounds)
    llm = IssueLLM()
    state = await _run(llm, sid)
    rows = await _rows(sid)

    assert len(_side_speakers(rows)) == opened * side_rounds * SPEECHES_PER_ROUND
    main = [m for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SPEECH]
    assert sorted({m.round_number for m in main}) == list(range(1, max_rounds + 1)), "본 토론 라운드를 쓰지 않습니다"
    assert len(main) == max_rounds * 3, "전문가 셋이 라운드마다 한 번씩"
    assert state.current_round == max_rounds and state.max_rounds == max_rounds
    assert any(f"Round {max_rounds} of {max_rounds}" in m[-1]["content"] for _k, m in llm.sent)


@pytest.mark.asyncio
async def test_a_budget_spent_inside_it_stops_before_the_next_round_and_still_concludes():
    sid = await _session(token_budget=10 ** 6, sub_debate_rounds=4)
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    llm = IssueLLM(usage=usage)

    def spend(count: int) -> None:
        if count == 2:
            llm.usage = {"prompt_tokens": 10 ** 7, "completion_tokens": 1, "total_tokens": 10 ** 7 + 1}

    llm.on_side = spend
    state = await _run(llm, sid)
    rows = await _rows(sid)
    assert _side_speakers(rows) == ["architect", "critic"], "라운드 하나를 마치고 멈춥니다"
    assert llm.prompts_with(SUB_DEBATE_CONCLUSION_MARKER), "그때까지의 발언으로 결론을 씁니다"
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_a_resumed_sub_debate_keeps_the_rounds_its_turn_started_with():
    sid = await _session(sub_debate_rounds=2)
    turn_id = await _crash_inside(sid, 2)
    async with get_session_factory(DB_URL)() as db:
        (await db.get(SessionModel, sid)).sub_debate_rounds = MAX_SUB_DEBATE_ROUNDS
        await db.commit()

    await OrchestratorEngine(agent_pool=_pool(), llm_caller=IssueLLM()).resume_turn(sid, turn_id, "continue")
    assert len(_side_speakers(await _rows(sid))) == 2 * SPEECHES_PER_ROUND, "끊긴 턴은 고정해 둔 구성으로 마칩니다"


def test_the_opener_and_the_closer_get_their_own_instruction_at_any_length():
    architect, critic = _pool().get("architect"), _pool().get("critic")
    first = SubDebateRunner.speech_instruction(architect, critic, "쟁점", 0, 2)
    assert "[서브 토론 1/2]" in first and "입장과 근거를 먼저" in first
    assert "마무리" in SubDebateRunner.speech_instruction(critic, architect, "쟁점", 1, 2), "1라운드면 둘째가 마지막입니다"
    middle = SubDebateRunner.speech_instruction(critic, architect, "쟁점", 1, 6)
    assert "[서브 토론 2/6]" in middle and "마무리" not in middle and "되풀이하지" in middle
    assert "마무리" in SubDebateRunner.speech_instruction(critic, architect, "쟁점", 5, 6)


@pytest.mark.parametrize("value, expected", [
    (None, DEFAULT_SUB_DEBATE_ROUNDS), (0, 1), (-3, 1), (1, 1), (3, 3), (99, MAX_SUB_DEBATE_ROUNDS),
])
def test_the_rounds_stay_in_range(value, expected):
    assert clamp_sub_debate_rounds(value) == expected


# =============================================================== 4. 그래프


def _graph() -> Dict[str, Any]:
    return {
        "id": "two-steps", "name": "둘", "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "agent": "architect", "label": "첫째"},
            {"id": "b", "type": "agent", "agent": "critic", "label": "둘째"},
            {"id": "g", "type": "gate", "label": "판정", "question": "됐는가?"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "from": ["start", "out"], "to": ["a", "in"]},
            {"id": "e2", "from": ["a", "out"], "to": ["b", "in"], "carry": "digest"},
            {"id": "e3", "from": ["b", "out"], "to": ["g", "in"]},
            {"id": "e4", "from": ["g", "yes"], "to": ["end", "in"]},
        ],
    }


@pytest.mark.asyncio
async def test_in_a_graph_the_conclusion_reaches_nodes_whatever_their_wires_carry(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    graph_store.save_graph(parse_graph(_graph()))
    sid = await _session(strategy="graph_debate", max_rounds=3, graph_id="two-steps")
    llm = IssueLLM()
    state = await _run(llm, sid)

    node = next(m for k, m in llm.sent if "“둘째”" in m[-1]["content"])
    assert "합의: 토큰은 서버 세션에 둡니다." in "\n".join(x["content"] for x in node)
    gate = llm.prompts_with("[판정]")[0]
    assert "합의: 토큰은 서버 세션에 둡니다." in gate, "판정도 이 결론을 알고 판단합니다"
    assert state.status == "completed"
    rows = await _rows(sid)
    assert [(m.graph_node_id, m.round_number) for m in rows if m.graph_node_id] == [
        ("a", 1), ("b", 2), ("g", 3),
    ], "서브 토론은 노드가 아니라 단계 사이에 있어 재생에 끼어들지 않습니다"


@pytest.mark.asyncio
async def test_a_graph_turn_resumes_after_a_crash_inside_a_sub_debate(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    graph_store.save_graph(parse_graph(_graph()))
    sid = await _session(strategy="graph_debate", max_rounds=3, graph_id="two-steps")
    with pytest.raises(asyncio.CancelledError):
        await _run(IssueLLM(crash_at_side=2), sid)
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalars().first()
        await turns.mark_interrupted_turns(db)

    llm = IssueLLM(answer='{"open": false}')
    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).resume_turn(sid, turn.id, "continue")
    assert state.status == "completed"
    rows = await _rows(sid)
    assert _kinds(rows).count(turns.KIND_SUBDEBATE_CONCLUSION) == 1, "끊긴 서브 토론이 이어져 닫힙니다"
    assert [(m.graph_node_id, m.round_number) for m in rows if m.graph_node_id] == [("a", 1), ("b", 2), ("g", 3)]


# =============================================================== 5. 실패·정지·재개


@pytest.mark.asyncio
async def test_a_failed_speaker_ends_it_with_what_was_said_and_nothing_invented():
    sid = await _session()
    llm = IssueLLM(fail_side=("critic",))
    state = await _run(llm, sid)
    rows = await _rows(sid)
    speeches = [m for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SUBDEBATE_SPEECH]
    assert [(m.sender_key, m.msg_type) for m in speeches] == [("architect", "agent"), ("critic", "error")]
    conclusion = next(m for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SUBDEBATE_CONCLUSION)
    assert conclusion.msg_type == "orchestrator"
    assert "critic" in state.failed_agent_keys, "응답을 받지 못했다는 사실은 합성까지 갑니다"


@pytest.mark.asyncio
async def test_with_no_answer_at_all_it_closes_without_a_conclusion():
    sid = await _session()
    llm = IssueLLM(fail_side=("architect",))
    state = await _run(llm, sid)
    assert not llm.prompts_with(SUB_DEBATE_CONCLUSION_MARKER), "받은 발언이 없으면 결론을 지어내지 않습니다"
    rows = await _rows(sid)
    closing = next(m for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SUBDEBATE_CONCLUSION)
    assert closing.msg_type == "error" and "결론 없이 닫습니다" in closing.content
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_a_stop_ends_it_without_a_conclusion_and_goes_to_synthesis():
    sid = await _session()
    control = TurnControl()
    llm = IssueLLM(on_side=lambda count: control.request_stop() if count == 1 else None)
    state = await _run(llm, sid, control=control)
    assert llm.side_count == 1 and not llm.prompts_with(SUB_DEBATE_CONCLUSION_MARKER)
    assert state.stopped_early is True and state.artifacts
    assert not [m for k, m in llm.sent if "Round 2 of 2" in m[-1]["content"]]


@pytest.mark.asyncio
async def test_an_interjection_during_it_reaches_the_next_speaker():
    sid = await _session()
    control = TurnControl()
    llm = IssueLLM(on_side=lambda count: control.add_note("쿠키는 쓰지 마세요") if count == 1 else None)
    await _run(llm, sid, control=control)
    speech_prompts = llm.side_prompts()
    assert "쿠키는 쓰지 마세요" not in speech_prompts[0]
    assert "쿠키는 쓰지 마세요" in speech_prompts[1], "사람의 말은 어디서든 다음 발언자에게 갑니다"


async def _crash_inside(sid: str, at: int) -> str:
    with pytest.raises(asyncio.CancelledError):
        await _run(IssueLLM(crash_at_side=at), sid)
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalars().first()
        await turns.mark_interrupted_turns(db)
        return turn.id


@pytest.mark.asyncio
async def test_an_interrupted_sub_debate_continues_without_asking_again():
    sid = await _session()
    turn_id = await _crash_inside(sid, 2)

    llm = IssueLLM()
    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).resume_turn(sid, turn_id, "continue")
    assert state.status == "completed"

    rows = await _rows(sid)
    assert _kinds(rows).count(turns.KIND_SUBDEBATE_OPEN) == 1, "같은 경계에 두 번 열지 않습니다"
    speeches = [m.sender_key for m in rows if (m.turn_meta or {}).get("kind") == turns.KIND_SUBDEBATE_SPEECH]
    assert speeches == ["architect", "critic", "architect", "critic"], "남은 차례부터, 같은 순서로 이어 갑니다"
    assert _kinds(rows).count(turns.KIND_SUBDEBATE_CONCLUSION) == 1
    round_one = [m for m in rows if m.round_number == 1 and (m.turn_meta or {}).get("kind") == turns.KIND_SPEECH]
    assert len(round_one) == 3, "끝난 라운드의 발언은 다시 하지 않습니다"


@pytest.mark.asyncio
async def test_a_concluded_sub_debate_is_not_repeated_on_resume():
    sid = await _session()

    class CrashInRoundTwo(IssueLLM):
        async def call_agent(self, agent, messages, *args, **kwargs):
            if "Round 2 of 2" in messages[-1]["content"]:
                raise asyncio.CancelledError()
            return await super().call_agent(agent, messages, *args, **kwargs)

    with pytest.raises(asyncio.CancelledError):
        await _run(CrashInRoundTwo(), sid)
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalars().first()
        await turns.mark_interrupted_turns(db)

    llm = IssueLLM()
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).resume_turn(sid, turn.id, "continue")
    assert llm.side_count == 0
    assert _kinds(await _rows(sid)).count(turns.KIND_SUBDEBATE_OPEN) == 1


def test_the_record_tells_where_a_sub_debate_stopped():
    messages = [
        {"round_number": 1, "turn_meta": {"kind": turns.KIND_SPEECH}},
        {"round_number": 1, "turn_meta": {"kind": turns.KIND_SUBDEBATE_OPEN, "issue": "쟁점", "agents": ["a", "b"]}},
        {"round_number": 1, "turn_meta": {"kind": turns.KIND_SUBDEBATE_SPEECH}},
    ]
    record = turns.sub_debate_at(messages, 1)
    assert record.issue == "쟁점" and record.agents == ["a", "b"] and len(record.speeches) == 1
    assert record.concluded is False
    assert turns.sub_debate_at(messages, 2) is None
    assert turns.sub_debate_at(messages + [
        {"round_number": 1, "turn_meta": {"kind": turns.KIND_SUBDEBATE_CONCLUSION}},
    ], 1).concluded is True


def test_a_failed_side_speech_counts_as_a_failed_agent_when_rebuilt():
    """도는 중의 `failed_agent_keys` 와 끊긴 턴을 다시 센 값이 같아야 합니다."""
    assert turns.failed_agents([
        {"sender_key": "critic", "msg_type": "error", "turn_meta": {"kind": turns.KIND_SUBDEBATE_SPEECH}},
    ]) == ["critic"]


def test_only_speeches_inside_are_left_out_of_the_main_record():
    assert turns.in_main_record({"turn_meta": {"kind": turns.KIND_SPEECH}})
    assert turns.in_main_record({"turn_meta": {"kind": turns.KIND_SUBDEBATE_OPEN}})
    assert turns.in_main_record({"turn_meta": {"kind": turns.KIND_SUBDEBATE_CONCLUSION}})
    assert turns.in_main_record({})
    assert not turns.in_main_record({"turn_meta": {"kind": turns.KIND_SUBDEBATE_SPEECH}})


# =============================================================== 6. 다시 보기


@pytest.mark.asyncio
async def test_the_saved_document_keeps_the_whole_exchange_and_labels_it():
    sid = await _session()
    await _run(IssueLLM(), sid)
    rows = await _rows(sid)
    text = build_session_markdown(
        {"id": sid, "title": "Side", "strategy": "sequential_debate", "max_rounds": 2},
        [
            {
                "sender_key": m.sender_key, "sender_name": m.sender_name, "sender_role": m.sender_role,
                "content": m.content, "round_number": m.round_number, "msg_type": m.msg_type,
                "turn_meta": m.turn_meta,
            }
            for m in rows
        ],
        [], [],
    )
    assert "서브 토론 비밀 발언" in text, "맥락에서 뺀 것이지 기록에서 뺀 것이 아닙니다"
    assert "— 서브 토론 결론" in text and "— 서브 토론\n" in text
