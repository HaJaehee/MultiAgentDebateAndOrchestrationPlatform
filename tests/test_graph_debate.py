"""그래프 토론 — 에이전트를 선으로 이어 도는 다섯 번째 전략 (app/orchestration/graph.py).

지키려는 것.

1. **검증** — 돌 수 없는 그래프(시작·끝 없음, 없는/꺼진 에이전트, 판정 없는 루프, 틀린 핀)는
   턴을 시작하기 전에 사람이 읽을 문장으로 거절한다. 사용자 발언도 기록하지 않는다.
2. **단계 전개** — 설계안의 예시(설계 → 구현·보안 검토 동시 → 취합 → 판정 → 되돌림)가 정확히
   그 순서로 돈다. "모두 기다림" 은 되돌림 선을 기다리지 않는다.
3. **맥락은 선을 따른다** — 노드는 들어온 선만, 선 종류(전문·요지·참조)대로 받는다. 루프로
   다시 불린 노드는 자기 직전 발언과 판정 의견을 함께 본다.
4. **멈춤** — 판정을 읽지 못하면 기본 갈래로 가며 그 사실을 남기고, 방문 상한·정지 요청에도
   합성까지 간다.
5. 기존 네 전략은 이 전략과 무관하게 그대로 돈다 (나머지 테스트 전체).
"""

import json
import uuid
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import select

from app.agents.base import Agent
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.control import TurnControl
from app.orchestration.engine import GraphTurnError, OrchestratorEngine, parse_gate_decision
from app.orchestration.graph import (
    GraphScheduler,
    back_edges,
    graph_from_card_order,
    parse_graph,
    validate_graph,
)
from app import graph_store
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"
AGENTS = {"architect": True, "coder": True, "critic": True}

DIGEST = "\n## 요지\n- {name} 요지 {n}\n"


def example(**overrides) -> Dict[str, Any]:
    """설계안의 예시 그래프."""
    data = {
        "id": "review-loop",
        "name": "설계 → 병렬 구현·검토 → 판정 루프",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "design", "type": "agent", "agent": "architect", "label": "설계", "max_visits": 1},
            {"id": "impl", "type": "agent", "agent": "coder", "label": "구현", "max_visits": 3,
             "instruction": "판정 의견이 있으면 그것부터 고치세요."},
            {"id": "sec", "type": "agent", "agent": "critic", "label": "보안 검토", "max_visits": 1},
            {"id": "merge", "type": "merge", "label": "취합", "wait": "all"},
            {"id": "gate", "type": "gate", "label": "판정", "question": "치명적 결함이 없는가?"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "from": ["start", "out"], "to": ["design", "in"]},
            {"id": "e2", "from": ["design", "out"], "to": ["impl", "in"]},
            {"id": "e3", "from": ["design", "out"], "to": ["sec", "in"], "carry": "digest"},
            {"id": "e4", "from": ["impl", "out"], "to": ["merge", "in"]},
            {"id": "e5", "from": ["sec", "out"], "to": ["merge", "in"], "carry": "refs"},
            {"id": "e6", "from": ["merge", "out"], "to": ["gate", "in"]},
            {"id": "e7", "from": ["gate", "yes"], "to": ["end", "in"]},
            {"id": "e8", "from": ["gate", "no"], "to": ["impl", "in"], "carry": "digest"},
        ],
    }
    data.update(overrides)
    return data


# =============================================================== 1. 검증


def test_the_example_is_valid_and_its_loop_edge_is_found():
    spec = parse_graph(example())
    report = validate_graph(spec, AGENTS, default_max_visits=3)
    assert report.ok and not report.warnings
    assert back_edges(spec) == {"e8"}
    # 설계 1 + 구현 3 + 보안 1 + 취합 3 + 판정 3 + 계획 1 + 합성 1
    assert report.max_calls == 13
    assert spec.agent_keys() == ["architect", "coder", "critic"]


def _without(data, node_ids=(), edge_ids=()):
    data["nodes"] = [n for n in data["nodes"] if n["id"] not in node_ids]
    data["edges"] = [e for e in data["edges"] if e["id"] not in edge_ids]
    return data


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: _without(d, ["start"], ["e1"]), "시작 노드가 없습니다"),
    (lambda d: d["nodes"].append({"id": "s2", "type": "start"}) or d, "시작 노드는 하나여야"),
    (lambda d: _without(d, ["end"], ["e7"]), "최종 합성 노드가 없습니다"),
    (lambda d: d["nodes"][1].update(agent="ghost") or d, "에이전트 ghost 가 없습니다"),
    (lambda d: d["nodes"][1].update(agent="orchestrator") or d, "오케스트레이터는 에이전트 노드가 될 수 없습니다"),
    (lambda d: d["nodes"][5].update(question=" ") or d, "판정 노드에 질문을 적으세요"),
    (lambda d: d["edges"][0].update({"from": ["start", "yes"]}) or d, "출력 핀 yes 가 없습니다"),
    # 판정이 있는 큰 루프와 얽혀 있어도, 판정을 건너뛰는 작은 루프는 잡아야 합니다.
    (lambda d: d["edges"].append({"id": "e9", "from": ["merge", "out"], "to": ["impl", "in"]}) or d,
     "멈출 조건이 없습니다"),
    (lambda d: d["edges"].append({"id": "e9", "from": ["impl", "out"], "to": ["impl", "in"]}) or d,
     "멈출 조건이 없습니다"),
    (lambda d: _without(d, [], ["e7"]), "최종 합성에 닿는 길이 없습니다"),
])
def test_graphs_that_cannot_run_are_refused_with_a_readable_reason(mutate, expected):
    report = validate_graph(parse_graph(mutate(example())), AGENTS)
    assert not report.ok
    assert any(expected in e for e in report.errors), report.errors


def test_a_disabled_agent_is_named_as_disabled_not_missing():
    report = validate_graph(parse_graph(example()), {**AGENTS, "critic": False})
    assert any("critic 가 꺼져 있습니다" in e for e in report.errors)


def test_dead_ends_are_warnings_not_errors():
    data = example()
    data["nodes"].append({"id": "lonely", "type": "agent", "agent": "critic", "label": "혼자"})
    data["edges"] = [e for e in data["edges"] if e["id"] != "e8"]  # 아니오 갈래 비움
    report = validate_graph(parse_graph(data), AGENTS)
    assert report.ok
    assert any("“혼자” 은(는) 시작에서 닿지 않아" in w for w in report.warnings)
    assert any("“아니오” 갈래가 비어" in w for w in report.warnings)


def test_bad_ids_and_shapes_are_rejected_on_parse():
    with pytest.raises(ValueError, match="그래프 형식"):
        parse_graph(example(id="../evil"))
    with pytest.raises(ValueError, match="그래프 형식"):
        parse_graph({"id": "x", "nodes": [{"id": "a", "type": "robot"}]})


def test_card_order_becomes_a_valid_chain():
    spec = graph_from_card_order("from-cards", "카드 순서", ["orchestrator", "architect", "coder", "critic"])
    assert [n.agent for n in spec.nodes if n.type == "agent"] == ["architect", "coder", "critic"]
    assert validate_graph(spec, AGENTS).ok


# =============================================================== 2. 스케줄러


def _drive(spec, gate_answers, max_visits=3, limit=20) -> List[List[Tuple[str, int]]]:
    """LLM 없이 스케줄러만 돌립니다. 판정은 주어진 순서대로 답합니다."""
    scheduler = GraphScheduler(spec, max_visits)
    scheduler.deliver("start", "out", ["request"])
    answers = iter(gate_answers)
    steps = []
    for _ in range(limit):
        ready = scheduler.ready()
        if not ready or any(n.type == "end" for n in ready):
            break
        activations = [scheduler.activate(n) for n in ready]
        steps.append([(a.node.id, a.visit) for a in activations])
        for a in activations:
            if a.node.type == "gate":
                scheduler.deliver(a.node.id, next(answers), [f"gate{a.visit}"])
            else:
                scheduler.deliver(a.node.id, "out", [f"{a.node.id}{a.visit}"])
    return steps


def test_the_example_unfolds_step_by_step_as_designed():
    steps = _drive(parse_graph(example()), ["no", "yes"])
    assert steps == [
        [("design", 1)],
        [("impl", 1), ("sec", 1)],
        [("merge", 1)],
        [("gate", 1)],
        [("impl", 2)],
        [("merge", 2)],
        [("gate", 2)],
    ]


def test_wait_all_does_not_wait_for_the_loop_edge():
    """취합이 되돌림 선까지 기다리면 루프가 아직 안 돌았다는 이유로 영원히 멈춥니다."""
    data = example()
    data["nodes"][4]["wait"] = "all"
    data["edges"].append({"id": "e9", "from": ["gate", "no"], "to": ["merge", "in"]})
    steps = _drive(parse_graph(data), ["yes"])
    assert ("merge", 1) in steps[2]


def test_a_node_stops_being_called_at_its_visit_cap():
    spec = parse_graph(example())
    steps = _drive(spec, ["no"] * 10)
    impl_visits = [v for step in steps for node, v in step if node == "impl"]
    assert impl_visits == [1, 2, 3], "구현은 최대 3회"


def test_a_join_that_never_gets_all_inputs_is_reported_as_pending():
    data = example()
    data["nodes"].append({"id": "orphan", "type": "agent", "agent": "critic"})
    data["edges"].append({"id": "e9", "from": ["orphan", "out"], "to": ["merge", "in"]})
    spec = parse_graph(data)
    scheduler = GraphScheduler(spec, 3)
    scheduler.deliver("start", "out", ["r"])
    for _ in range(3):
        for node in scheduler.ready():
            scheduler.activate(node)
            scheduler.deliver(node.id, "out", [node.id])
    assert scheduler.ready() == []
    assert scheduler.pending() == ["merge"]


# =============================================================== 파일


def test_graph_files_round_trip_atomically(tmp_path):
    spec = parse_graph(example())
    graph_store.save_graph(spec, tmp_path)
    assert graph_store.load_graph("review-loop", tmp_path).dump() == spec.dump()
    assert [p.name for p in tmp_path.iterdir()] == ["review-loop.json"], "임시 파일이 남으면 안 됩니다"
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert graph_store.list_graphs(tmp_path) == [("review-loop", spec.name)]
    assert graph_store.free_graph_id("review-loop", tmp_path) == "review-loop-2"
    with pytest.raises(ValueError):
        graph_store.load_graph("../conf", tmp_path)


def test_a_file_whose_inner_id_differs_is_refused(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(example(id="b")), encoding="utf-8")
    with pytest.raises(ValueError, match="파일 이름과 다릅니다"):
        graph_store.load_graph("a", tmp_path)


@pytest.mark.parametrize("content,expected", [
    ('{"decision": "yes", "reason": "문제 없음"}', ("yes", "문제 없음")),
    ('판정합니다.\n```json\n{"decision": "no", "reason": "인증 누락"}\n```', ("no", "인증 누락")),
    ('{"decision": "아니오", "reason": "x"}', ("no", "x")),
    ("예\n근거는 충분합니다", ("yes", "")),
    ("no problem at all", None),
    ('{"decision": "maybe"}', None),
])
def test_gate_answers_are_read_strictly(content, expected):
    assert parse_gate_decision(content) == expected


# =============================================================== 3. 엔진


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


class GraphLLM(FakeLLMCaller):
    """발언마다 요지를 붙이고, 판정에는 주어진 답을 차례로 줍니다. 보낸 프롬프트를 모읍니다."""

    def __init__(self, gate_answers=(), **kwargs):
        super().__init__(**kwargs)
        self.gate_answers = list(gate_answers)
        self.sent: List[Tuple[str, List[Dict[str, Any]]]] = []
        self.counter: Dict[str, int] = {}

    async def call_agent(self, agent, messages, custom_instructions="", *args, **kwargs):
        self.sent.append((agent.key, messages))
        return await super().call_agent(agent, messages, custom_instructions, *args, **kwargs)

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"]
        if last.startswith("[판정]"):
            answer = self.gate_answers.pop(0) if self.gate_answers else "읽을 수 없는 답"
            return answer
        if "[Graph Step]" in last:
            self.counter[agent.key] = self.counter.get(agent.key, 0) + 1
            n = self.counter[agent.key]
            return f"{agent.name} 의 {n}번째 발언 본문 " + "가" * 800 + DIGEST.format(name=agent.name, n=n)
        return super()._reply_for(agent, messages)

    def prompts(self, needle: str) -> List[List[Dict[str, Any]]]:
        """노드 발언 프롬프트 중 마지막 지시에 `needle` 이 든 것 (장부·판정 호출 제외)."""
        return [
            m for _k, m in self.sent
            if "[Graph Step]" in m[-1]["content"] and needle in m[-1]["content"]
        ]


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    return tmp_path


async def _session(graph_id="review-loop", **columns) -> str:
    await init_db(DB_URL)
    sid = f"graph-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="Graph", strategy="graph_debate", graph_id=graph_id,
            max_rounds=3, active_agents=["orchestrator", "architect"], **columns,
        ))
        await db.commit()
    return sid


async def _rows(sid):
    async with get_session_factory(DB_URL)() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        session = await db.get(SessionModel, sid)
        return messages, session


@pytest.mark.asyncio
async def test_the_example_graph_runs_end_to_end(graphs):
    graph_store.save_graph(parse_graph(example()))
    sid = await _session()
    llm = GraphLLM(gate_answers=['{"decision": "no", "reason": "인증 누락"}', '{"decision": "yes", "reason": "해결됨"}'])
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="캐시 서비스를 만들어줘", on_event=on_event,
    )

    steps = [[n["id"] for n in e["nodes"]] for e in events if e["type"] == "graph_step_started"]
    assert steps == [["design"], ["impl", "sec"], ["merge"], ["gate"], ["impl"], ["merge"], ["gate"]]
    assert [e["decision"] for e in events if e["type"] == "graph_gate_decided"] == ["no", "yes"]
    assert [e["reason"] for e in events if e["type"] == "graph_finished"] == ["end"]
    assert state.status == "completed" and state.artifacts

    messages, session = await _rows(sid)
    by_node = [(m.graph_node_id, m.round_number) for m in messages if m.graph_node_id]
    assert by_node == [
        ("design", 1), ("impl", 2), ("sec", 2), ("merge", 3), ("gate", 4),
        ("impl", 5), ("merge", 6), ("gate", 7),
    ], "기록 순서는 완료 순서가 아니라 노드 순서"
    assert session.graph_snapshot["id"] == "review-loop", "실제로 돈 그래프를 굳혀 둡니다"
    # 참여자는 로스터 체크박스(architect 만)가 아니라 그래프입니다.
    assert {"coder", "critic"} <= {k for k, _m in llm.sent}


@pytest.mark.asyncio
async def test_each_node_sees_only_what_its_wires_carry(graphs):
    graph_store.save_graph(parse_graph(example()))
    sid = await _session()
    llm = GraphLLM(gate_answers=['{"decision": "no", "reason": "인증 누락"}', '{"decision": "yes", "reason": "ok"}'])
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="캐시")

    def text(prompt):
        return "\n".join(m["content"] for m in prompt)

    sec = text(llm.prompts("“보안 검토”")[0])
    assert "System Architect 요지 1" in sec and "1번째 발언 본문" not in sec, "요지 선은 요지만"
    assert "Senior Engineer" not in sec, "같은 단계의 구현 발언은 보이지 않습니다"

    first_impl = text(llm.prompts("“구현”")[0])
    assert "System Architect 의 1번째 발언 본문" in first_impl, "전문 선은 원문"

    second_impl_prompt = llm.prompts("“구현” (2회차)")[0]
    second_impl = text(second_impl_prompt)
    assert "[판정 · 판정] **아니오** — 인증 누락" in second_impl, "되돌아온 이유를 봅니다"
    assert "되돌림" in second_impl
    assert any(m["role"] == "assistant" and "이 노드의 직전 발언" in m["content"] for m in second_impl_prompt)
    assert "판정 의견이 있으면 그것부터 고치세요." in second_impl

    merge = text(llm.prompts("“취합”")[0])
    assert "Senior Engineer 의 1번째 발언 본문" in merge and "Quality Critic 의 1번째 발언 본문" in merge


@pytest.mark.asyncio
async def test_an_unreadable_gate_answer_takes_the_default_branch_and_says_so(graphs):
    data = example()
    data["nodes"][5]["default"] = "yes"
    graph_store.save_graph(parse_graph(data))
    sid = await _session()
    llm = GraphLLM(gate_answers=["음… 잘 모르겠습니다"])
    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="캐시")

    notes = [m.content for m in state.messages if m.graph_node_id == "gate"]
    assert len(notes) == 1 and "기본 갈래 “예” 로 진행합니다" in notes[0]
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_a_loop_that_never_passes_stops_at_the_visit_cap_and_still_synthesizes(graphs):
    graph_store.save_graph(parse_graph(example()))
    sid = await _session()
    llm = GraphLLM(gate_answers=['{"decision": "no", "reason": "아직"}'] * 10)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="캐시", on_event=on_event,
    )
    assert len(llm.prompts("“구현”")) == 3
    assert any("최대 3회까지 불려" in m.content for m in state.messages)
    assert events[-1]["type"] == "turn_completed"
    assert any(e["type"] == "graph_finished" and e["reason"] == "idle" for e in events)
    assert state.artifacts, "멈춰도 합성까지 갑니다"


@pytest.mark.asyncio
async def test_without_a_graph_the_turn_does_not_start_or_record_the_request(graphs):
    sid = await _session(graph_id="")
    with pytest.raises(GraphTurnError, match="그래프가 없습니다"):
        await OrchestratorEngine(agent_pool=_pool(), llm_caller=GraphLLM()).run_turn(session_id=sid, user_prompt="x")
    messages, _session_row = await _rows(sid)
    assert messages == []


@pytest.mark.asyncio
async def test_an_invalid_graph_file_is_refused_with_its_errors(graphs):
    data = example()
    data["edges"].append({"id": "e9", "from": ["merge", "out"], "to": ["impl", "in"]})
    graph_store.save_graph(parse_graph(data))
    sid = await _session()
    with pytest.raises(GraphTurnError, match="멈출 조건이 없습니다"):
        await OrchestratorEngine(agent_pool=_pool(), llm_caller=GraphLLM()).run_turn(session_id=sid, user_prompt="x")
    with pytest.raises(GraphTurnError, match="찾을 수 없습니다"):
        await OrchestratorEngine(agent_pool=_pool(), llm_caller=GraphLLM()).run_turn(
            session_id=await _session(graph_id="nope"), user_prompt="x",
        )


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_graph_and_synthesizes(graphs):
    graph_store.save_graph(parse_graph(example()))
    sid = await _session()
    control = TurnControl()

    class StopAfterDesign(GraphLLM):
        async def call_agent(self, agent, messages, *args, **kwargs):
            result = await super().call_agent(agent, messages, *args, **kwargs)
            if agent.key == "architect":
                control.request_stop()
            return result

    llm = StopAfterDesign()
    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="캐시", control=control,
    )
    assert state.stopped_early is True
    assert not llm.prompts("“구현”")
    assert state.artifacts


@pytest.mark.asyncio
async def test_a_start_without_plan_hands_the_request_straight_to_the_first_node(graphs):
    data = example()
    data["nodes"][0]["plan"] = False
    graph_store.save_graph(parse_graph(data))
    sid = await _session()
    llm = GraphLLM(gate_answers=['{"decision": "yes", "reason": "ok"}'])
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="캐시")

    assert llm.sent[0][0] == "architect", "계획 호출이 없습니다"
    first = "\n".join(m["content"] for m in llm.sent[0][1])
    assert "이번 턴 요청 — 위 [User Goal / Current Request]" in first
