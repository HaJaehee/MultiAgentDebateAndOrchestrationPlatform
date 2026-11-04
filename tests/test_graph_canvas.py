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


def test_a_merge_of_uneven_branches_sits_after_the_longest_branch():
    """가장 짧은 거리로 열을 정하면 취합(M)이 긴 갈래의 B 와 같은 열에 놓여 선이 세로로 꺾입니다."""
    spec = parse_graph({
        "id": "uneven", "name": "길이가 다른 갈래",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "agent": "architect"},
            {"id": "b", "type": "agent", "agent": "coder"},
            {"id": "c", "type": "agent", "agent": "critic"},
            {"id": "m", "type": "merge"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "from": ["start", "out"], "to": ["a", "in"]},
            {"id": "e2", "from": ["a", "out"], "to": ["b", "in"]},
            {"id": "e3", "from": ["b", "out"], "to": ["m", "in"]},
            {"id": "e4", "from": ["start", "out"], "to": ["c", "in"]},
            {"id": "e5", "from": ["c", "out"], "to": ["m", "in"]},
            {"id": "e6", "from": ["m", "out"], "to": ["end", "in"]},
        ],
    })
    layout = auto_layout(spec)
    assert layout["m"][0] > layout["b"][0] > layout["a"][0]
    assert layout["end"][0] > layout["m"][0]
    assert len(set(layout.values())) == len(layout), "노드 자리가 서로 겹치지 않습니다"


def test_a_loop_back_does_not_push_nodes_to_the_right():
    layout = auto_layout(parse_graph(example()))
    assert layout["impl"][0] < layout["merge"][0] < layout["gate"][0] < layout["end"][0], \
        "판정에서 구현으로 되돌아가는 선은 열을 늘리지 않습니다"
    assert len(set(layout.values())) == len(layout)


def test_nodes_of_agents_not_registered_yet_are_marked_as_drafts():
    infos = dict(INFOS, auditor=("컨트랙트 보안관", "취약점 점검", "#e64a19"))
    data = example()
    data["nodes"][3]["agent"] = "auditor"
    canvas = spec_to_canvas(parse_graph(data), infos, drafts=["auditor"])
    nodes = {n["id"]: n["data"] for n in canvas["nodes"]}
    assert nodes["sec"]["draft"] is True and nodes["sec"]["agentName"] == "컨트랙트 보안관"
    assert "draft" not in nodes["impl"], "기존 에이전트 노드에는 표시가 없습니다"


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
