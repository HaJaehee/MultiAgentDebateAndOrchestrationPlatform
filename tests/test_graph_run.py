"""그래프 실행 표시 — 도는 화면 · 새로고침한 화면 · 다시 연 화면이 같은 그림을 그리는가.

`app/orchestration/graph_run.py` 는 상태의 근거를 발언 기록(노드 id + 나간 핀) 하나로 둡니다. 여기서
지키는 것.

1. 엔진 이벤트를 하나씩 받은 그림과, 끝난 뒤 DB 기록으로 다시 만든 그림이 같다.
2. 도중에 새로고침해도(러너 스냅샷 + 기록) 도는 노드와 흐른 선이 같다.
3. 판정 갈래는 기록에 남는다 — 새로고침한 화면이 "아니오" 로 되돌아간 선을 다시 그린다.
4. 채팅 카드의 노드 배지는 방문 횟수를 센다 ("구현 · 2회차").
"""

from typing import Any, Dict, List

import pytest

from app import graph_store
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.graph import parse_graph
from app.orchestration.graph_run import GraphRunTracker, NodeBadges, last_graph_turn, node_labels
from app.orchestration.runner import TurnRun
from tests.test_graph_debate import GraphLLM, _pool, _rows, _session, example, graphs  # noqa: F401

GATE_NO_THEN_YES = ['{"decision": "no", "reason": "인증 누락"}', '{"decision": "yes", "reason": "해결됨"}']


async def _run_example(gate_answers) -> Dict[str, Any]:
    graph_store.save_graph(parse_graph(example()))
    sid = await _session()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    await OrchestratorEngine(agent_pool=_pool(), llm_caller=GraphLLM(gate_answers=gate_answers)).run_turn(
        session_id=sid, user_prompt="캐시 서비스를 만들어줘", on_event=on_event,
    )
    return {"sid": sid, "events": events}


def _as_dicts(rows) -> List[Dict[str, Any]]:
    return [
        {
            "id": m.id, "graph_node_id": m.graph_node_id, "graph_port": m.graph_port,
            "msg_type": m.msg_type, "round_number": m.round_number, "turn_started_at": m.turn_started_at,
        }
        for m in rows
    ]


@pytest.mark.asyncio
async def test_the_live_picture_matches_the_one_rebuilt_from_the_database(graphs):
    run = await _run_example(GATE_NO_THEN_YES)
    live = GraphRunTracker()
    for event in run["events"]:
        live.observe(event)

    rows, session = await _rows(run["sid"])
    assert [m.graph_port for m in rows if m.graph_node_id == "gate"] == ["no", "yes"], "판정 갈래가 기록에 남습니다"
    rebuilt = GraphRunTracker.from_history(session.graph_snapshot, last_graph_turn(_as_dicts(rows), running=False))

    view = live.view()
    nodes = view["nodes"]
    assert nodes["impl"]["visits"] == 2 and nodes["sec"]["visits"] == 1
    assert nodes["gate"]["decision"] == "yes" and nodes["gate"]["visits"] == 2
    assert nodes["start"]["state"] == "done" and nodes["end"]["state"] == "done"
    assert all(e == "taken" for e in view["edges"].values()), "이 전개에서는 모든 선이 한 번씩 흘렀습니다"
    assert live.finished == "end" and not view["running"]

    rebuilt_view = rebuilt.view()
    assert rebuilt_view["nodes"] == nodes and rebuilt_view["edges"] == view["edges"]
    assert rebuilt_view["finished"] is None, "왜 멈췄는지는 기록에 없어, 다시 연 화면은 단계까지만 말합니다"
    assert rebuilt.summary() == "7단계까지 실행"
    assert live.summary() == "7단계까지 실행 · 최종 합성 노드에 닿음"
    assert live.summary(active=True) == "7단계에서 끝남 · 최종 합성 노드에 닿음 — 최종 합성 중", (
        "합성이 도는 동안에는 끝난 것처럼 읽히면 안 됩니다"
    )
    between = GraphRunTracker()
    for event in run["events"]:
        between.observe(event)
        if event["type"] == "ledger_update_started":
            break
    assert between.summary(active=True) == "1단계 마침 — 다음 단계 준비 중"


@pytest.mark.asyncio
async def test_a_page_refreshed_mid_run_draws_the_same_running_nodes(graphs):
    run = await _run_example(GATE_NO_THEN_YES)
    events = run["events"]
    # 2단계에서 구현의 발언이 확정되고, 보안 검토는 아직 스트리밍 중인 순간.
    step2 = next(i for i, e in enumerate(events) if e["type"] == "graph_step_started" and e["step"] == 2)
    impl_done = next(
        i for i, e in enumerate(events)
        if i > step2 and e["type"] == "message_added" and e["message"].get("graph_node_id") == "impl"
    )
    sec_started = next(
        i for i, e in enumerate(events)
        if i > step2 and e["type"] == "message_stream_start" and e["message"].get("graph_node_id") == "sec"
    )
    cut = max(impl_done, sec_started) + 1

    live = GraphRunTracker()
    turn = TurnRun("s", "캐시")
    for event in events[:cut]:
        live.observe(event)
        turn.apply(event)

    view = live.view()
    assert live.running == ["sec"], "확정된 노드는 빠지고 스트리밍 중인 노드는 남습니다"
    assert view["nodes"]["sec"]["state"] == "running" and view["nodes"]["impl"]["state"] == "done"
    assert view["edges"]["e3"] == "active" and view["edges"]["e2"] == "taken"
    assert view["edges"]["e8"] == "idle" and view["nodes"]["gate"]["state"] == "idle"

    snapshot = turn.snapshot()
    refreshed = GraphRunTracker.from_history(
        snapshot["graph"]["spec"], snapshot["messages"], state=snapshot["graph"],
    )
    assert refreshed.view() == view
    assert refreshed.summary() == "2단계 진행 중 — 보안 검토"


@pytest.mark.asyncio
async def test_a_loop_cut_by_the_visit_cap_shows_the_cap_and_the_no_branch(graphs):
    run = await _run_example(['{"decision": "no", "reason": "아직"}'] * 10)
    live = GraphRunTracker()
    for event in run["events"]:
        live.observe(event)
    view = live.view()
    assert view["nodes"]["impl"]["visits"] == 3 and view["nodes"]["impl"]["capped"]
    assert view["nodes"]["gate"]["decision"] == "no"
    assert view["edges"]["e7"] == "idle" and view["edges"]["e8"] == "taken"
    assert view["nodes"]["end"]["state"] == "idle", "최종 합성 노드에 닿지 않고 멈췄습니다"
    assert live.finished == "idle"

    rows, _session_row = await _rows(run["sid"])
    notes = [m for m in rows if m.graph_node_id == "impl" and m.graph_port is None]
    assert len(notes) == 1 and "최대 3회까지" in notes[0].content, "상한 안내는 출력이 아닙니다"


def _msg(msg_id, node=None, port=None, **extra) -> Dict[str, Any]:
    return {"id": msg_id, "graph_node_id": node, "graph_port": port, "msg_type": "agent", **extra}


def test_node_badges_count_visits_per_turn():
    badges = NodeBadges(node_labels(example()))
    seen = [badges.observe(m) for m in (
        _msg("u", msg_type="user"),
        _msg("a", "impl", "out"),
        _msg("a", "impl", "out"),  # 스트리밍 시작과 확정이 같은 id 로 옵니다
        _msg("b", "gate", "no"),
        _msg("c", "impl", "out"),
        _msg("d", "gate", "yes"),
        _msg("e", "impl", None),
        _msg("f", turn_started_at="2026-09-17T00:00:00"),
        _msg("g", "impl", "out"),
        _msg("h", "ghost", "out"),
    )]
    assert seen == [
        None, "구현 · 1회차", "구현 · 1회차", "판정 · 아니오", "구현 · 2회차", "판정 · 예 (2회차)",
        "구현 · 방문 상한", None, "구현 · 1회차", "ghost · 1회차",
    ]


def test_the_turn_to_draw_is_the_running_one_or_the_last_graph_turn():
    closer = {"turn_started_at": "t"}
    old = [_msg("1", "impl", "out"), _msg("2", **closer)]
    plain = [_msg("3"), _msg("4", **closer)]
    assert [m["id"] for m in last_graph_turn(old + plain, running=False)] == ["1", "2"], (
        "그래프가 아닌 턴이 뒤에 있어도 마지막 그래프 턴을 그립니다"
    )
    assert last_graph_turn(old + [_msg("5", msg_type="user")], running=True) == [
        _msg("5", msg_type="user")
    ], "새 턴이 시작되면 지난 턴의 그림을 지웁니다"
    assert last_graph_turn([], running=False) == []


def test_the_markdown_export_names_the_node_of_each_speech():
    from app.export import build_session_markdown

    session = {"title": "그래프", "strategy": "graph_debate", "graph_snapshot": parse_graph(example()).dump()}
    base = {"sender_key": "coder", "sender_name": "Senior Engineer", "sender_role": "Impl", "msg_type": "agent"}
    messages = [
        {**base, "id": "1", "content": "구현 1", "round_number": 2, "graph_node_id": "impl", "graph_port": "out"},
        {**base, "id": "2", "content": "구현 2", "round_number": 5, "graph_node_id": "impl", "graph_port": "out"},
        {**base, "id": "3", "content": "[판정 · 판정] **예**", "round_number": 7, "sender_key": "orchestrator",
         "sender_name": "Master Orchestrator", "msg_type": "orchestrator", "graph_node_id": "gate", "graph_port": "yes"},
    ]
    md = build_session_markdown(session, messages)
    assert "| 그래프 | 설계 → 병렬 구현·검토 → 판정 루프 (data/graphs/review-loop.json) |" in md
    assert "Senior Engineer (Impl) — 노드 “구현 · 1회차”" in md
    assert "Senior Engineer (Impl) — 노드 “구현 · 2회차”" in md
    assert "노드 “판정 · 예”" in md
    assert "### 5단계" in md and "Round 5" not in md
