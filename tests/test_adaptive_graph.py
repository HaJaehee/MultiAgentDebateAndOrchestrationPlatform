"""적응형 그래프 토론 — 오케스트레이터가 턴마다 실행 그래프를 그리는 여섯 번째 전략 (ADR-034).

지키려는 것.

1. **같은 검사** — 그린 그래프도 사람이 그린 그래프와 같은 검사를 지나고, 비용은 사람이 정한 한도 안에 둔다.
2. **틀려도 돈다** — 틀린 답에는 틀린 곳을 알려 한 번 더 받고, 그래도 안 되면 기준 그래프나 카드 순서로 물러서며
   그 사실을 남긴다. 턴은 멈추지 않는다.
3. **참여자는 체크박스** — 체크하지 않은 전문가는 그릴 수 없다.
4. **기록으로 고정** — 그린 그래프는 기록에 값으로 남아, 끊긴 턴은 다시 묻지 않고 같은 그래프로 이어 간다.
5. 다른 다섯 전략은 그래프를 그리지 않는다.
"""

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app import graph_store
from app.agents.llm_types import LLMUnavailableError
from app.agents.pool import AgentPool
from app.bench import load_scenario
from app.config import AgentConfig
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.orchestration import graph_draft, turns
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.graph import GraphNode, GraphSpec, graph_from_card_order, parse_graph
from app.orchestration.strategies import STRATEGY_MAP
from app.ui.components.sidebar import active_agent_count
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"
KEYS = ["architect", "coder", "critic"]


def drawn(**overrides) -> Dict[str, Any]:
    """설계 → 구현·검토 동시 → 취합 → 판정 → (아니오면 구현으로) 를 그린 답."""
    data = {
        "name": "설계 후 병렬 검토",
        "reason": "구현과 보안 검토는 서로의 결과가 필요 없습니다.",
        "nodes": [
            {"id": "design", "type": "agent", "agent": "architect", "label": "설계"},
            {"id": "impl", "type": "agent", "agent": "coder", "label": "구현"},
            {"id": "sec", "type": "agent", "agent": "critic", "label": "보안 검토", "max_visits": 1},
            {"id": "merge", "type": "merge", "label": "취합"},
            {"id": "gate", "type": "gate", "label": "판정", "question": "치명적 결함이 없는가?"},
        ],
        "edges": [
            {"from": "start", "to": "design"},
            {"from": "design", "to": "impl"},
            {"from": "design", "to": "sec", "carry": "digest"},
            {"from": "impl", "to": "merge"},
            {"from": "sec", "to": "merge"},
            {"from": "merge", "to": "gate"},
            {"from": "gate", "to": "end", "port": "yes"},
            {"from": "gate", "to": "impl", "port": "no", "carry": "digest"},
        ],
    }
    data.update(overrides)
    return data


# =============================================================== 1. 답 읽기


def _parse(data: Any, max_visits: int = 3) -> graph_draft.GraphDraft:
    return graph_draft.parse_draft(json.dumps(data, ensure_ascii=False), "drawn-test", max_visits)


def test_a_drawn_answer_becomes_a_runnable_graph():
    draft = _parse(drawn())
    spec = draft.spec
    assert draft.reason == "구현과 보안 검토는 서로의 결과가 필요 없습니다."
    assert spec.id == "drawn-test" and spec.name == "설계 후 병렬 검토"
    assert spec.start.id == "start" and spec.start.plan is True, "시작 노드는 읽을 때 채웁니다"
    assert spec.node("end").type == "end"
    assert graph_draft.draft_report(spec, KEYS, 3).ok
    edge = next(e for e in spec.edges if e.source[0] == "gate" and e.target[0] == "impl")
    assert edge.source == ("gate", "no") and edge.target == ("impl", "in") and edge.carry == "digest"
    assert next(e for e in spec.edges if e.source[0] == "start").source == ("start", "out")


def test_a_merge_of_two_branches_waits_for_both_unless_told_otherwise():
    assert _parse(drawn()).spec.node("merge").wait == "all"
    data = drawn()
    data["nodes"][3]["wait"] = "any"
    assert _parse(data).spec.node("merge").wait == "any", "모델이 정한 값은 그대로 둡니다"


def test_a_merge_of_the_two_gate_branches_does_not_wait_for_both():
    """판정의 예·아니오 갈래는 함께 오지 않습니다. 둘 다 기다리면 영원히 멈춥니다."""
    data = {
        "nodes": [
            {"id": "a", "type": "agent", "agent": "coder"},
            {"id": "g", "type": "gate", "question": "q"},
            {"id": "m", "type": "merge"},
        ],
        "edges": [
            {"from": "start", "to": "a"}, {"from": "a", "to": "g"},
            {"from": "g", "to": "m", "port": "yes"}, {"from": "g", "to": "m", "port": "no"},
            {"from": "m", "to": "end"},
        ],
    }
    assert _parse(data).spec.node("m").wait == "any"


def test_visit_caps_cannot_exceed_the_session_limit():
    data = drawn()
    data["nodes"][1]["max_visits"] = 20
    data["nodes"][2]["max_visits"] = "많이"
    spec = _parse(data, max_visits=2).spec
    assert spec.node("impl").max_visits == 2, "최대 라운드가 곧 사람이 정한 한도입니다"
    assert spec.node("sec").max_visits is None


def test_file_style_endpoints_and_unknown_carries_are_tolerated():
    data = drawn()
    data["edges"][0] = {"from": ["start", "out"], "to": ["design", "in"], "carry": "전문"}
    data["edges"][6] = {"from": ["gate", "yes"], "to": ["end", "in"]}
    spec = _parse(data).spec
    assert spec.edges[0].carry == "full"
    assert spec.edges[6].source == ("gate", "yes")


@pytest.mark.parametrize("content,expected", [
    ("그래프를 그릴 수 없습니다", "JSON 이 없습니다"),
    ('{"nodes": [}', "JSON 을 읽지 못했습니다"),
    ('{"name": "x"}', "nodes 와 edges"),
    ('{"nodes": [{"id": "a", "type": "robot"}], "edges": []}', "그래프 형식"),
])
def test_unreadable_answers_are_refused_with_a_sentence(content, expected):
    with pytest.raises(ValueError, match=expected):
        graph_draft.parse_draft(content, "drawn-test", 3)


def test_an_answer_wrapped_in_prose_and_a_fence_is_read():
    content = "이렇게 그렸습니다.\n```json\n" + json.dumps(drawn(), ensure_ascii=False) + "\n```"
    assert graph_draft.parse_draft(content, "drawn-test", 3).spec.agent_keys() == KEYS


# =============================================================== 2. 허용 범위


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d["nodes"][0].update(agent="ghost"), "ghost 가 없습니다"),
    (lambda d: d["nodes"][0].update(agent="orchestrator"), "오케스트레이터는 에이전트 노드가 될 수 없습니다"),
    (lambda d: d["edges"].append({"from": "impl", "to": "design"}), "멈출 조건이 없습니다"),
    (lambda d: d["edges"].pop(6), "최종 합성에 닿는 길이 없습니다"),
    # 판정에서 갈래를 적지 않은 선.
    (lambda d: d["edges"][6].pop("port"), "출력 핀 out"),
])
def test_a_drawn_graph_passes_the_same_checks_as_a_hand_drawn_one(mutate, expected):
    data = drawn()
    mutate(data)
    report = graph_draft.draft_report(_parse(data).spec, KEYS, 3)
    assert not report.ok
    assert any(expected in e for e in report.errors), report.errors


def test_a_graph_without_a_single_specialist_is_refused():
    spec = _parse({"nodes": [], "edges": [{"from": "start", "to": "end"}]}).spec
    assert any("전문가 노드가 하나도 없습니다" in e for e in graph_draft.draft_report(spec, KEYS, 3).errors)


def test_drawn_graphs_cannot_grow_past_the_limits():
    nodes = [{"id": f"n{i}", "type": "agent", "agent": "coder"} for i in range(graph_draft.MAX_DRAFT_NODES + 1)]
    chain = ["start"] + [n["id"] for n in nodes] + ["end"]
    edges = [{"from": a, "to": b} for a, b in zip(chain, chain[1:])]
    report = graph_draft.draft_report(_parse({"nodes": nodes, "edges": edges}).spec, KEYS, 1)
    assert any(f"{graph_draft.MAX_DRAFT_NODES}개까지만" in e for e in report.errors)

    nodes = nodes[:graph_draft.MAX_DRAFT_NODES]
    chain = ["start"] + [n["id"] for n in nodes] + ["end"]
    edges = [{"from": a, "to": b} for a, b in zip(chain, chain[1:])]
    report = graph_draft.draft_report(_parse({"nodes": nodes, "edges": edges}, max_visits=5).spec, KEYS, 5)
    assert any("번 호출합니다" in e for e in report.errors), "12노드 × 최대 라운드 5 는 호출 상한을 넘습니다"


def test_the_fallback_is_the_base_graph_when_it_can_run_and_the_card_order_when_not():
    base = _parse(drawn()).spec
    assert graph_draft.fallback_graph("g", KEYS, base, 3) is base
    chain = graph_draft.fallback_graph("g", ["architect", "coder"], base, 3)
    assert chain.agent_keys() == ["architect", "coder"], "체크하지 않은 critic 이 든 기준 그래프는 쓰지 않습니다"
    assert graph_draft.fallback_graph("g", ["coder"], None, 3).agent_keys() == ["coder"]


def test_the_prompt_lists_only_what_it_should():
    base = _parse(drawn()).spec
    prompt = graph_draft.draft_prompt(
        goal="캐시", record="", plan="계획", roster="- coder", max_rounds=3, parallel_limit=2, base=base,
        previous="앞선 답", errors=["에이전트 ghost 가 없습니다"],
    )[0]["content"]
    assert prompt.startswith(graph_draft.DRAFT_PROMPT_MARKER)
    assert "[기준 그래프]" in prompt and '"impl"' in prompt
    assert '"pos"' not in prompt, "편집기 좌표는 모델에게 필요 없습니다"
    assert "에이전트 ghost 가 없습니다" in prompt and "앞선 답" in prompt
    fresh = graph_draft.draft_prompt(
        goal="캐시", record="", plan="", roster="- coder", max_rounds=3, parallel_limit=2,
    )[0]["content"]
    assert "[기준 그래프]" not in fresh and "[앞선 답의 문제]" not in fresh


# =============================================================== 3. 전략


def test_only_the_new_strategy_draws_and_the_other_five_stay_as_they_were():
    assert STRATEGY_MAP["adaptive_graph"].runs_graph and STRATEGY_MAP["adaptive_graph"].draws_graph
    assert not STRATEGY_MAP["adaptive_graph"].uses_graph_file
    assert STRATEGY_MAP["graph_debate"].uses_graph_file and not STRATEGY_MAP["graph_debate"].draws_graph
    for name in ("sequential_debate", "adversarial_debate", "orchestrator_led", "parallel_dispatch"):
        assert not STRATEGY_MAP[name].runs_graph and not STRATEGY_MAP[name].draws_graph, name


def test_the_session_card_counts_the_checkboxes_not_a_graph_file():
    session = SessionModel(id="x", strategy="adaptive_graph", graph_id="", active_agents=["orchestrator", "coder"])
    assert active_agent_count(session) == 2


def test_the_benchmark_accepts_it_because_it_needs_no_graph_file(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"prompt": "p", "variants": [
        {"label": "a", "strategy": "adaptive_graph", "sub_debates": True},
    ]}), encoding="utf-8")
    assert load_scenario(path).variants[0].sub_debates is True
    path.write_text(json.dumps({"prompt": "p", "variants": [{"label": "a", "strategy": "graph_debate"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="지원되지 않는 전략"):
        load_scenario(path)


# =============================================================== 4. 엔진


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


class DrawingLLM(FakeLLMCaller):
    """그래프를 그려 달라는 호출에 주어진 답을 차례로 줍니다. 판정은 늘 예입니다. 보낸 프롬프트를 모읍니다."""

    def __init__(self, drawings=(), *, draw_fails: bool = False, crash_at: Optional[Tuple[str, int]] = None, **kwargs):
        super().__init__(**kwargs)
        self.drawings = list(drawings)
        self.draw_fails = draw_fails
        self.crash_at = crash_at
        self.speeches: Dict[str, int] = {}
        self.sent: List[Tuple[str, List[Dict[str, Any]]]] = []
        self.draw_prompts: List[str] = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.sent.append((agent.key, messages))
        last = messages[-1]["content"]
        if graph_draft.DRAFT_PROMPT_MARKER in last:
            self.draw_prompts.append(last)
            if self.draw_fails:
                raise LLMUnavailableError(agent, "APIConnectionError: 500")
        elif "[Graph Step]" in last:
            self.speeches[agent.key] = self.speeches.get(agent.key, 0) + 1
            if self.crash_at == (agent.key, self.speeches[agent.key]):
                raise asyncio.CancelledError()
        return await super().call_agent(agent, messages, *args, **kwargs)

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"]
        if graph_draft.DRAFT_PROMPT_MARKER in last:
            answer = self.drawings.pop(0) if self.drawings else "그릴 수 없습니다"
            return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        if last.startswith("[판정]"):
            return '{"decision": "yes", "reason": "ok"}'
        return super()._reply_for(agent, messages)


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    return tmp_path


async def _session(agents=("orchestrator", "architect", "coder", "critic"), graph_id="", **columns) -> str:
    await init_db(DB_URL)
    sid = f"draw-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="Draw", strategy="adaptive_graph", graph_id=graph_id,
            max_rounds=3, active_agents=list(agents), **columns,
        ))
        await db.commit()
    return sid


async def _rows(sid):
    async with get_session_factory(DB_URL)() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        return messages, await db.get(SessionModel, sid)


async def _run(llm, sid):
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="캐시 서비스를 만들어줘", on_event=on_event,
    )
    return state, events


def _steps(events):
    return [[n["id"] for n in e["nodes"]] for e in events if e["type"] == "graph_step_started"]


@pytest.mark.asyncio
async def test_the_orchestrator_plans_then_draws_then_the_graph_runs(graphs):
    sid = await _session()
    llm = DrawingLLM([drawn()])
    state, events = await _run(llm, sid)

    assert _steps(events) == [["design"], ["impl", "sec"], ["merge"], ["gate"]]
    assert state.status == "completed" and state.artifacts
    # 계획이 먼저입니다. 그래프는 계획과 승인된 분담을 입력으로 받습니다.
    first_prompts = [m[-1]["content"] for _k, m in llm.sent[:2]]
    assert graph_draft.DRAFT_PROMPT_MARKER not in first_prompts[0]
    assert graph_draft.DRAFT_PROMPT_MARKER in first_prompts[1]
    assert "[이번 턴 계획]" in llm.draw_prompts[0] and "Senior Engineer" in llm.draw_prompts[0]

    messages, session = await _rows(sid)
    note = next(m for m in messages if m.turn_meta and m.turn_meta.get("kind") == turns.KIND_GRAPH_DRAFT)
    assert note.round_number == 0 and note.msg_type == "orchestrator"
    assert "설계 후 병렬 검토" in note.content and "구현과 보안 검토는" in note.content
    assert "- 판정 (아니오) → 구현" in note.content, "어떤 흐름인지 글로도 읽힙니다"
    assert note.turn_meta["fallback"] is False
    assert session.graph_snapshot["name"] == "설계 후 병렬 검토", "다시 연 화면이 이 그래프를 그립니다"
    started = next(e for e in events if e["type"] == "graph_started")
    assert started["spec"]["id"].startswith("drawn-")
    assert list(graphs.iterdir()) == [], "그린 그래프는 파일을 만들지 않습니다"


@pytest.mark.asyncio
async def test_a_wrong_answer_is_corrected_once_with_what_was_wrong(graphs):
    """체크하지 않은 전문가를 그리면 거절하고, 틀린 곳을 알려 다시 받습니다."""
    sid = await _session(agents=("orchestrator", "architect", "coder"))
    valid = {
        "name": "둘이서",
        "nodes": [
            {"id": "a", "type": "agent", "agent": "architect"},
            {"id": "b", "type": "agent", "agent": "coder"},
        ],
        "edges": [{"from": "start", "to": "a"}, {"from": "a", "to": "b"}, {"from": "b", "to": "end"}],
    }
    llm = DrawingLLM([drawn(), valid])
    _state, events = await _run(llm, sid)

    assert len(llm.draw_prompts) == 2
    assert "에이전트 critic 가 없습니다" in llm.draw_prompts[1], "모델이 무엇을 고칠지 압니다"
    assert "Quality Critic" not in llm.draw_prompts[0], "체크하지 않은 전문가는 후보에 없습니다"
    assert _steps(events) == [["a"], ["b"]]
    assert "critic" not in llm.calls


@pytest.mark.asyncio
async def test_two_bad_answers_fall_back_to_the_card_order_and_say_so(graphs):
    sid = await _session()
    llm = DrawingLLM(["그래프는 어렵습니다", drawn(nodes=[], edges=[])])
    state, events = await _run(llm, sid)

    assert len(llm.draw_prompts) == 2, "다시 받는 것은 한 번까지입니다"
    assert _steps(events) == [["n1"], ["n2"], ["n3"]]
    assert state.status == "completed" and state.artifacts, "그리지 못해도 턴은 끝까지 갑니다"
    messages, _session_row = await _rows(sid)
    note = next(m for m in messages if m.turn_meta and m.turn_meta.get("kind") == turns.KIND_GRAPH_DRAFT)
    assert note.msg_type == "error" and "[실행 그래프 작성 실패]" in note.content
    assert note.turn_meta["fallback"] is True


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_does_not_stop_the_turn(graphs):
    sid = await _session()
    llm = DrawingLLM(draw_fails=True)
    state, events = await _run(llm, sid)
    assert len(llm.draw_prompts) == 1, "연결 실패는 다시 받아도 소용없습니다"
    assert _steps(events) == [["n1"], ["n2"], ["n3"]] and state.status == "completed"


@pytest.mark.asyncio
async def test_a_chosen_graph_is_the_base_to_change_and_the_fallback(graphs):
    base = parse_graph({
        "id": "base", "name": "기준", "nodes": [
            {"id": "start", "type": "start", "pos": [1, 2]},
            {"id": "only", "type": "agent", "agent": "coder", "label": "기준 노드"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "from": ["start", "out"], "to": ["only", "in"]},
            {"id": "e2", "from": ["only", "out"], "to": ["end", "in"]},
        ],
    })
    graph_store.save_graph(base)
    sid = await _session(graph_id="base")
    llm = DrawingLLM(["못 그립니다", "못 그립니다"])
    _state, events = await _run(llm, sid)

    assert "[기준 그래프]" in llm.draw_prompts[0] and "기준 노드" in llm.draw_prompts[0]
    assert _steps(events) == [["only"]], "그리지 못하면 고른 그래프로 돕니다"
    _messages, session = await _rows(sid)
    assert session.graph_snapshot["id"] == "base"


@pytest.mark.asyncio
async def test_a_missing_base_graph_means_drawing_from_scratch(graphs):
    sid = await _session(graph_id="nope")
    llm = DrawingLLM([drawn()])
    _state, events = await _run(llm, sid)
    assert "[기준 그래프]" not in llm.draw_prompts[0]
    assert _steps(events)[0] == ["design"]


@pytest.mark.asyncio
async def test_one_specialist_needs_no_drawing(graphs):
    sid = await _session(agents=("orchestrator", "coder"))
    llm = DrawingLLM()
    state, events = await _run(llm, sid)
    assert llm.draw_prompts == []
    assert _steps(events) == [["n1"]] and state.status == "completed"
    messages, _session_row = await _rows(sid)
    note = next(m for m in messages if m.turn_meta and m.turn_meta.get("kind") == turns.KIND_GRAPH_DRAFT)
    assert note.msg_type == "orchestrator" and note.turn_meta["fallback"] is False


@pytest.mark.asyncio
async def test_a_stop_during_planning_never_draws(graphs):
    from app.orchestration.control import TurnControl

    sid = await _session()
    control = TurnControl()

    class StopAfterPlan(DrawingLLM):
        async def call_agent(self, agent, messages, *args, **kwargs):
            result = await super().call_agent(agent, messages, *args, **kwargs)
            control.request_stop()
            return result

    llm = StopAfterPlan([drawn()])
    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="캐시", control=control,
    )
    assert llm.draw_prompts == [] and state.stopped_early is True


@pytest.mark.asyncio
async def test_an_interrupted_turn_continues_on_the_graph_it_drew(graphs):
    sid = await _session()
    with pytest.raises(asyncio.CancelledError):
        await OrchestratorEngine(agent_pool=_pool(), llm_caller=DrawingLLM([drawn()], crash_at=("coder", 1))).run_turn(
            session_id=sid, user_prompt="캐시",
        )
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalars().first()
        await turns.mark_interrupted_turns(db)
        unfinished = await turns.unfinished_turn(db, sid)
    assert unfinished.can_continue is True

    llm = DrawingLLM()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).resume_turn(
        sid, turn.id, "continue", on_event=on_event,
    )
    assert llm.draw_prompts == [], "그래프는 기록에 있습니다. 다시 그리면 이미 돈 노드와 어긋납니다"
    assert _steps(events)[0] == ["impl"], "끊긴 단계의 못 끝낸 노드부터 이어 갑니다"
    assert state.status == "completed"
    assert "architect" not in llm.calls and "critic" not in llm.calls, "끝난 노드는 다시 돌지 않습니다"


@pytest.mark.asyncio
async def test_an_interruption_before_the_drawing_draws_on_resume(graphs):
    sid = await _session()

    class CrashWhileDrawing(DrawingLLM):
        async def call_agent(self, agent, messages, *args, **kwargs):
            if graph_draft.DRAFT_PROMPT_MARKER in messages[-1]["content"]:
                raise asyncio.CancelledError()
            return await super().call_agent(agent, messages, *args, **kwargs)

    with pytest.raises(asyncio.CancelledError):
        await OrchestratorEngine(agent_pool=_pool(), llm_caller=CrashWhileDrawing()).run_turn(
            session_id=sid, user_prompt="캐시",
        )
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalars().first()
        await turns.mark_interrupted_turns(db)

    llm = DrawingLLM([drawn()])
    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).resume_turn(sid, turn.id, "continue")
    assert len(llm.draw_prompts) == 1 and state.status == "completed"


def test_the_recorded_graph_is_read_back_from_the_last_record():
    assert turns.drawn_graph([{"turn_meta": {"kind": "plan"}}]) is None
    spec = graph_from_card_order("g", "n", ["coder"])
    assert isinstance(spec, GraphSpec) and isinstance(spec.nodes[0], GraphNode)
    assert turns.drawn_graph([
        {"turn_meta": {"kind": turns.KIND_GRAPH_DRAFT, "spec": spec.dump()}},
    ])["id"] == "g"
