"""그래프 토론 편집 캔버스 — Vue Flow 를 NiceGUI 요소로.

Vue Flow 는 `app/ui/static/graph_editor/index.js` 에 하나의 ES 모듈로 묶여 저장소에 실려 있습니다
(폐쇄망, 다시 만드는 법은 그 폴더의 BUILD.md). `vue` 는 NiceGUI 가 올린 것을 함께 씁니다.

그래프 파일(`GraphSpec`)과 캔버스가 쓰는 모양은 다릅니다. 그 사이의 변환은 여기 순수 함수로
두어 브라우저 없이 테스트합니다 — `spec_to_canvas`, `canvas_to_spec`.
"""

from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from nicegui import ui

from app.orchestration.graph import GraphSpec, parse_graph

ASSETS = Path(__file__).resolve().parent.parent / "static" / "graph_editor"

# 좌표가 없는 그래프(카드 순서로 만든 것, 손으로 쓴 JSON)를 놓을 때의 간격.
LAYOUT_DX = 230
LAYOUT_DY = 130


def agent_infos(agents: List[Any]) -> Dict[str, Tuple[str, str, str]]:
    """노드에 보일 에이전트 정보 {키: (이름, 역할, 머리띠 색 hex)}. 오케스트레이터는 에이전트 노드가 될 수 없어 뺍니다."""
    from app.agents.base import style_for_agent

    out: Dict[str, Tuple[str, str, str]] = {}
    for agent in agents:
        if agent.key == "orchestrator":
            continue
        style = style_for_agent(agent.key, getattr(agent, "card_color", None), getattr(agent, "icon", None))
        out[agent.key] = (agent.name, agent.role, style.get("badge_color", ""))
    return out


def auto_layout(spec: GraphSpec) -> Dict[str, Tuple[float, float]]:
    """좌표가 없는 노드의 자리. `start` 에서의 거리(단계)로 열을, 같은 열 안의 순서로 행을 정합니다."""
    depth: Dict[str, int] = {}
    start = spec.start
    queue = deque([start.id] if start else [])
    if start:
        depth[start.id] = 0
    while queue:
        node_id = queue.popleft()
        for edge in spec.out_edges(node_id):
            target = edge.target[0]
            if target not in depth:
                depth[target] = depth[node_id] + 1
                queue.append(target)
    deepest = max(depth.values(), default=0)
    rows: Dict[int, int] = {}
    positions: Dict[str, Tuple[float, float]] = {}
    for node in spec.nodes:
        column = depth.get(node.id, deepest + 1)
        row = rows.get(column, 0)
        rows[column] = row + 1
        positions[node.id] = (40 + LAYOUT_DX * column, 40 + LAYOUT_DY * row)
    return positions


def spec_to_canvas(spec: GraphSpec, agents: Mapping[str, Tuple[str, str, str]]) -> Dict[str, Any]:
    """그래프 파일 → 캔버스 초기값. 없는 에이전트는 이름이 비어 노드가 경고색으로 보입니다."""
    layout = auto_layout(spec)
    nodes = []
    for node in spec.nodes:
        x, y = node.pos if node.pos is not None else layout[node.id]
        data: Dict[str, Any] = {
            "type": node.type, "label": node.label, "instruction": node.instruction,
            "wait": node.wait, "sees": node.sees,
        }
        if node.max_visits:
            data["max_visits"] = node.max_visits
        if node.type == "agent":
            name, role, color = agents.get(node.agent or "", ("", "", ""))
            data.update(agent=node.agent or "", agentName=name, agentRole=role, color=color)
        if node.type == "gate":
            data.update(question=node.question, default=node.default)
        if node.type == "start":
            data["plan"] = node.plan
        nodes.append({"id": node.id, "position": {"x": x, "y": y}, "data": data})
    edges = [
        {
            "id": edge.id,
            "source": edge.source[0], "sourceHandle": edge.source[1],
            "target": edge.target[0], "targetHandle": edge.target[1],
            "data": {"carry": edge.carry},
        }
        for edge in spec.edges
    ]
    return {"nodes": nodes, "edges": edges}


def canvas_to_spec(graph_id: str, name: str, canvas: Mapping[str, Any]) -> GraphSpec:
    """캔버스가 돌려준 그래프(`getGraph()`) → 그래프 파일. 모양이 틀리면 ValueError.

    노드 종류에 맞지 않는 필드는 버립니다 — 에이전트 노드를 판정으로 바꿨다가 남은 `agent` 같은 것이
    파일에 남으면, 읽는 사람이 무엇이 유효한지 알 수 없습니다.
    """
    keep = {
        "start": {"label", "plan"},
        "agent": {"label", "agent", "instruction", "wait", "max_visits", "sees"},
        "merge": {"label", "instruction", "wait", "max_visits"},
        "gate": {"label", "question", "default", "wait", "max_visits"},
        "end": {"label", "wait"},
    }
    nodes = []
    for raw in canvas.get("nodes", []):
        kind = raw.get("type")
        node = {"id": raw.get("id"), "type": kind}
        for field in keep.get(kind, set()):
            value = raw.get(field)
            if value not in (None, ""):
                node[field] = value
        if raw.get("pos") is not None:
            node["pos"] = raw["pos"]
        nodes.append(node)
    return parse_graph({
        "id": graph_id,
        "name": name,
        "nodes": nodes,
        "edges": [
            {"id": e.get("id"), "from": e.get("from"), "to": e.get("to"), "carry": e.get("carry", "full")}
            for e in canvas.get("edges", [])
        ],
    })


GRAPH_EDITOR_CSS = """
.gcanvas { width: 100%; height: 100%; background: #0b1020; }
.gcanvas .vue-flow__background, .gcanvas .vue-flow { background: #0b1020; }
.gcanvas .vue-flow__pane { background-image: radial-gradient(#243049 1px, transparent 1px); background-size: 22px 22px; }
.gnode { width: 190px; background: #151a2d; color: #e5e8f3; border: 1.5px solid #3b4566; border-radius: 10px;
         box-shadow: 0 6px 18px rgba(0,0,0,.35); font-family: inherit; position: relative; }
.gnode-selected { border-color: #a5b4fc; box-shadow: 0 0 0 2px rgba(165,180,252,.45); }
.gnode-invalid { border-color: #f87171; }
.gnode-band { display: flex; justify-content: space-between; gap: 6px; background: #4f46e5; color: #fff;
              font: 600 10px/1 ui-monospace, Consolas, monospace; letter-spacing: .06em; padding: 5px 9px;
              border-radius: 8px 8px 0 0; }
.gnode-merge .gnode-band, .gnode-gate .gnode-band { background: #475069; }
.gnode-start .gnode-band, .gnode-end .gnode-band { background: #0c8577; }
.gnode-badges { font-weight: 400; opacity: .9; }
.gnode-title { font-size: 13px; font-weight: 600; padding: 7px 10px 0; line-height: 1.35; word-break: keep-all; }
.gnode-sub { font: 11px ui-monospace, Consolas, monospace; color: #9ba3bf; padding: 2px 10px 8px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gnode .gpin { width: 11px; height: 11px; background: #151a2d; border: 2px solid #9aa2c0; }
.gnode .gpin.vue-flow__handle-right { background: #9aa2c0; }
.gnode .gpin-yes { border-color: #4ad37f; background: #4ad37f !important; }
.gnode .gpin-no { border-color: #fb8c4c; background: #fb8c4c !important; }
.gport { position: absolute; right: 12px; font: 10px ui-monospace, Consolas, monospace; transform: translateY(-50%); }
.gport-yes { top: 46%; color: #4ad37f; } .gport-no { top: 80%; color: #fb8c4c; }
.gedge .vue-flow__edge-path { stroke-width: 2.4; }
.gedge-full .vue-flow__edge-path { stroke: #8c88ff; }
.gedge-digest .vue-flow__edge-path { stroke: #38c7b4; }
.gedge-refs .vue-flow__edge-path { stroke: #eea83d; }
.gedge-no .vue-flow__edge-path { stroke-dasharray: 7 6; }
/* 고른 선. Vue Flow 기본 테마가 고른 선을 #555(어두운 회색)로 칠해 어두운 바탕에서 보이지 않았습니다 —
   그 규칙(.vue-flow__edge.selected …)보다 구체적으로 적어 노란색으로 깜빡이게 합니다. */
.gcanvas .vue-flow__edge.gedge.selected .vue-flow__edge-path,
.gcanvas .vue-flow__edge.gedge:focus .vue-flow__edge-path,
.gcanvas .vue-flow__edge.gedge:focus-visible .vue-flow__edge-path {
  stroke: #facc15; stroke-width: 4; filter: drop-shadow(0 0 4px rgba(250, 204, 21, .75));
  animation: gedge-blink 1s ease-in-out infinite;
}
.gcanvas .vue-flow__edge.gedge.selected .vue-flow__edge-textbg { fill: #3f3510; stroke: #facc15; stroke-width: 1; }
.gcanvas .vue-flow__edge.gedge.selected .vue-flow__edge-text { fill: #fde68a; }
@keyframes gedge-blink { 0%, 100% { stroke: #facc15; opacity: 1; } 50% { stroke: #fef08a; opacity: .35; } }
@media (prefers-reduced-motion: reduce) {
  .gcanvas .vue-flow__edge.gedge.selected .vue-flow__edge-path { animation: none; }
}
.gedge .vue-flow__edge-textbg { fill: #151a2d; }
.gedge .vue-flow__edge-text { fill: #cbd2ea; font-size: 11px; }
.vue-flow__connectionline .vue-flow__connection-path { stroke: #a5b4fc; stroke-width: 2; }
/* 실행 표시 (로스터 미리보기). 안 돈 노드와 선은 흐리게, 도는 노드는 숨 쉬듯 빛나게. */
.gnode-chip { position: absolute; top: -10px; right: 8px; background: #1e2640; color: #cbd2ea;
              border: 1px solid #3b4566; border-radius: 999px; font: 600 10px/1 ui-monospace, Consolas, monospace;
              padding: 3px 7px; white-space: nowrap; }
.gnode-chip-yes { color: #4ad37f; border-color: #2f7a4d; }
.gnode-chip-no { color: #fb8c4c; border-color: #8a4a26; }
.gnode-run-idle { opacity: .45; }
.gnode-run-done { border-color: #4b5a86; }
.gnode-run-error { border-color: #f87171; }
.gnode-run-error .gnode-chip { color: #fca5a5; border-color: #7f1d1d; }
.gnode-run-running { border-color: #fbbf24; animation: gnode-pulse 1.6s ease-in-out infinite; }
.gnode-run-running .gnode-chip { color: #fbbf24; border-color: #92400e; }
@keyframes gnode-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(251,191,36,.55); } 50% { box-shadow: 0 0 0 7px rgba(251,191,36,0); } }
@media (prefers-reduced-motion: reduce) { .gnode-run-running { animation: none; box-shadow: 0 0 0 3px rgba(251,191,36,.5); } }
.gedge-run-idle { opacity: .28; }
.gedge-run-taken .vue-flow__edge-path { stroke-width: 3; }
.gedge-run-active .vue-flow__edge-path { stroke: #fbbf24; stroke-width: 3; }
.gcanvas-readonly .gnode { cursor: default; }
.gcanvas-readonly .gpin { pointer-events: none; }
"""


class GraphCanvas(ui.element, component="graph_canvas.js", esm={"vue-flow": str(ASSETS)}):
    """편집 캔버스. 브라우저가 편집 상태를 들고, 서버는 선택·변경 알림과 저장 때 읽기만 받습니다."""

    def __init__(self, graph: Dict[str, Any], *, readonly: bool = False, run_view: Optional[Dict[str, Any]] = None):
        super().__init__()
        self._props["graph"] = graph
        self._props["readonly"] = readonly
        self._props["run-view"] = run_view
        if readonly:
            self.classes("gcanvas-readonly")

    def set_run(self, view: Optional[Dict[str, Any]]) -> None:
        """실행 상태만 바꿉니다 (`GraphRunTracker.view()`). 노드 자리와 화면 위치는 그대로입니다."""
        self._props["run-view"] = view
        self.run_method("setRun", view)

    @staticmethod
    def add_styles() -> None:
        """이 페이지에 Vue Flow 기본 스타일과 편집기 스타일을 올립니다."""
        ui.add_css((ASSETS / "vue-flow.css").read_text(encoding="utf-8"))
        ui.add_css(GRAPH_EDITOR_CSS)
