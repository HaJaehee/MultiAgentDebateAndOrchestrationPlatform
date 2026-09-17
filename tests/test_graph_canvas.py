"""그래프 편집기 — 파일과 캔버스 사이의 변환 (app/ui/components/graph_canvas.py).

편집 상태는 브라우저가 들고 있고, 저장할 때 `getGraph()` 로 읽어 파일로 씁니다. 지키려는 것.

1. 파일 → 캔버스 → 파일로 돌아와도 그래프가 그대로다 (좌표·선 종류·노드 설정).
2. 좌표가 없는 그래프(카드 순서로 만든 것)는 시작에서의 거리대로 자동 배치된다.
3. 노드 종류에 맞지 않는 필드는 파일에 남지 않는다.
4. 없는 에이전트를 가리키는 노드는 이름이 비어 화면에서 경고로 보인다.
"""

import pytest

from app import graph_store
from app.orchestration.graph import graph_from_card_order, parse_graph, validate_graph
from app.ui.components.graph_canvas import auto_layout, canvas_to_spec, spec_to_canvas
from tests.test_graph_debate import AGENTS, example

INFOS = {
    "architect": ("System Architect", "Architecture", "#009688"),
    "coder": ("Senior Engineer", "Implementation", "#673ab7"),
    "critic": ("Quality Critic", "Review", "#ff8f00"),
}


def _as_browser_returns(canvas):
    """`graph_canvas.js` 의 getGraph() 가 돌려주는 모양으로 바꿉니다 (빈 값은 빼고)."""
    fields = ["type", "label", "agent", "instruction", "question", "default", "wait", "max_visits", "sees", "plan"]
    nodes = []
    for node in canvas["nodes"]:
        out = {"id": node["id"], "pos": [round(node["position"]["x"]), round(node["position"]["y"])]}
        for field in fields:
            value = node["data"].get(field)
            if value not in (None, ""):
                out[field] = value
        nodes.append(out)
    edges = [
        {"id": e["id"], "from": [e["source"], e["sourceHandle"]], "to": [e["target"], e["targetHandle"]],
         "carry": e["data"]["carry"]}
        for e in canvas["edges"]
    ]
    return {"nodes": nodes, "edges": edges}


def test_a_graph_survives_the_round_trip_through_the_canvas():
    data = example()
    for i, node in enumerate(data["nodes"]):
        node["pos"] = [10 * i, 20 * i]
    spec = parse_graph(data)

    canvas = spec_to_canvas(spec, INFOS)
    back = canvas_to_spec(spec.id, spec.name, _as_browser_returns(canvas))

    assert back.model_dump() == spec.model_dump()
    assert validate_graph(back, AGENTS).ok


def test_agent_nodes_carry_their_display_name_and_colour():
    canvas = spec_to_canvas(parse_graph(example()), INFOS)
    impl = next(n for n in canvas["nodes"] if n["id"] == "impl")
    assert impl["data"]["agentName"] == "Senior Engineer"
    assert impl["data"]["color"] == "#673ab7"

    data = example()
    data["nodes"][2]["agent"] = "ghost"
    ghost = next(n for n in spec_to_canvas(parse_graph(data), INFOS)["nodes"] if n["id"] == "impl")
    assert ghost["data"]["agentName"] == "", "이름이 비면 노드가 경고색으로 보입니다"


def test_graphs_without_positions_are_laid_out_by_distance_from_start():
    spec = graph_from_card_order("cards", "카드", ["architect", "coder", "critic"])
    for node in spec.nodes:
        node.pos = None
    layout = auto_layout(spec)
    xs = [layout[n][0] for n in ("start", "n1", "n2", "n3", "end")]
    assert xs == sorted(xs) and len(set(xs)) == 5, "일렬 그래프는 왼쪽에서 오른쪽으로"

    split = auto_layout(parse_graph(example()))
    assert split["impl"][0] == split["sec"][0] and split["impl"][1] != split["sec"][1], "갈라진 노드는 같은 열, 다른 행"


def test_fields_that_do_not_belong_to_a_node_type_are_dropped():
    raw = {
        "nodes": [
            {"id": "start", "type": "start", "pos": [0, 0], "plan": False, "agent": "coder"},
            {"id": "g", "type": "gate", "pos": [100, 0], "question": "됐나?", "agent": "coder", "sees": "all"},
            {"id": "end", "type": "end", "pos": [200, 0]},
        ],
        "edges": [
            {"id": "e1", "from": ["start", "out"], "to": ["g", "in"], "carry": "digest"},
            {"id": "e2", "from": ["g", "yes"], "to": ["end", "in"], "carry": "full"},
        ],
    }
    spec = canvas_to_spec("x", "X", raw)
    gate = spec.node("g")
    assert gate.agent is None and gate.sees == "inputs", "판정으로 바꾼 노드에 에이전트 설정이 남지 않습니다"
    assert spec.start.plan is False and spec.start.agent is None
    assert spec.edges[0].carry == "digest"


def test_a_broken_canvas_payload_is_a_readable_error():
    with pytest.raises(ValueError, match="그래프 형식"):
        canvas_to_spec("x", "X", {"nodes": [{"id": "a", "type": "robot"}], "edges": []})


def test_a_new_blank_graph_is_valid_and_saved(tmp_path, monkeypatch):
    from app.ui.graph_page import new_blank_graph

    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    monkeypatch.setattr("app.ui.graph_page.free_graph_id", lambda base: graph_store.free_graph_id(base, tmp_path))
    monkeypatch.setattr("app.ui.graph_page.save_graph", lambda spec: graph_store.save_graph(spec, tmp_path))
    spec = new_blank_graph()
    assert (tmp_path / f"{spec.id}.json").is_file()
    report = validate_graph(spec, AGENTS)
    assert report.ok and report.max_calls == 2, "계획 1 + 합성 1"
