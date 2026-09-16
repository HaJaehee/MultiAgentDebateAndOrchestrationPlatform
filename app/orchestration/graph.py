"""그래프 토론 — 에이전트를 선으로 이어 "누구의 출력이 누구의 입력인가" 를 사람이 그립니다.

다른 네 전략은 카드 정렬(`debate_priority`)과 진영(`debate_stance`)으로 순서를 정합니다. 이
전략은 순서 대신 **그래프**를 받습니다. 병렬·합류·반복이 전략 코드가 아니라 그림으로 정해집니다.

이 모듈은 LLM 을 부르지 않습니다. 그래프 모양(스키마), 검증, 그리고 단계마다 어느 노드를 돌릴지
정하는 스케줄러만 있습니다. 발언은 `OrchestratorEngine._run_graph` 가 합니다. 그래서 단계
전개를 가짜 LLM 없이 테스트할 수 있습니다.

## 노드·핀·선

| 종류 | 입력 핀 | 출력 핀 | 하는 일 |
| --- | --- | --- | --- |
| `start` | — | `out` | 이번 턴 요청(과 계획) |
| `agent` | `in` | `out` | 에이전트 한 명의 발언 |
| `merge` | `in` | `out` | 오케스트레이터가 들어온 발언을 합침 |
| `gate` | `in` | `yes`, `no` | 오케스트레이터가 질문에 예/아니오로 판정 |
| `end` | `in` | — | 여기 닿으면 그래프를 끝내고 최종 합성으로 |

선은 무엇을 싣고 가는지(`carry`)를 가집니다 — `full`(전문), `digest`(`## 요지` 만), `refs`(원문이되
긴 코드는 한 줄 참조). 블루프린트가 핀 색으로 자료형을 구분하는 자리입니다.

## 실행: 단계(superstep)

1. 직전 단계에서 새 입력이 도착한 노드가 이번 단계에 활성입니다. 대기 방식이 `all` 이면 **순방향**
   입력 선이 전부 한 번 이상 도착해야 합니다. 되돌림 선은 기다리지 않습니다 — 기다리면 루프가 아직
   돌지 않았다는 이유로 영원히 멈춥니다.
2. 활성 노드는 동시에 돕니다. 출력은 단계가 끝나야 다음 노드에 전달됩니다.
3. `end` 가 활성이 되면 그 단계는 돌리지 않고 끝냅니다.
4. 방문 상한(`max_visits`)에 닿은 노드는 더 이상 활성이 되지 않습니다. 새로 할 노드가 없으면 끝납니다.
   노드에 상한을 적지 않으면 세션의 "최대 라운드" 가 상한입니다 — 그래프 토론에서 그 손잡이는
   "한 노드가 한 턴에 몇 번까지 다시 불릴 수 있나" 입니다. 단계 전체의 상한은 모든 노드의 상한을
   더한 값이라(`GraphScheduler.max_steps`), 상한 안에서는 그래프가 도중에 잘리지 않습니다.

되돌림 선은 `start` 에서 깊이 우선으로 훑을 때 **지금 경로 위의 노드로 돌아가는** 선입니다.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field, field_validator

NodeType = Literal["start", "agent", "merge", "gate", "end"]
Carry = Literal["full", "digest", "refs"]
Wait = Literal["all", "any"]

GRAPH_SCHEMA_VERSION = 1
ORCHESTRATOR_KEY = "orchestrator"

INPUT_PORTS: Dict[str, Tuple[str, ...]] = {
    "start": (),
    "agent": ("in",),
    "merge": ("in",),
    "gate": ("in",),
    "end": ("in",),
}
OUTPUT_PORTS: Dict[str, Tuple[str, ...]] = {
    "start": ("out",),
    "agent": ("out",),
    "merge": ("out",),
    "gate": ("yes", "no"),
    "end": (),
}
NODE_TYPE_LABELS = {
    "start": "시작", "agent": "에이전트", "merge": "취합", "gate": "판정", "end": "최종 합성",
}
CARRY_LABELS = {"full": "전문", "digest": "요지", "refs": "참조"}


class GraphNode(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    type: NodeType
    label: str = ""
    # agent 노드: 발언할 에이전트 키.
    agent: Optional[str] = None
    # agent·merge 노드에 붙는 이 차례 지시문.
    instruction: str = ""
    # gate 노드: 판정 질문과, 판정을 읽지 못했을 때 갈 갈래.
    question: str = ""
    default: Literal["yes", "no"] = "yes"
    # 입력이 여럿일 때: 전부 도착해야(all) / 하나라도 도착하면(any).
    wait: Wait = "any"
    # 한 턴에 이 노드가 불릴 수 있는 최대 횟수. 비우면 세션의 "최대 라운드".
    max_visits: Optional[int] = Field(default=None, ge=1, le=20)
    # agent 노드: 들어온 선만 볼지(inputs), 기존 전사 전체를 볼지(all).
    sees: Literal["inputs", "all"] = "inputs"
    # start 노드: 오케스트레이터 계획을 함께 내보낼지.
    plan: bool = True
    # 편집기 좌표. 엔진은 읽지 않습니다.
    pos: Optional[Tuple[float, float]] = None

    @property
    def display(self) -> str:
        return self.label.strip() or (self.agent or NODE_TYPE_LABELS.get(self.type, self.type))


class GraphEdge(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    source: Tuple[str, str] = Field(alias="from")
    target: Tuple[str, str] = Field(alias="to")
    carry: Carry = "full"

    model_config = {"populate_by_name": True}

    def dump(self) -> Dict[str, Any]:
        return {"id": self.id, "from": list(self.source), "to": list(self.target), "carry": self.carry}


class GraphSpec(BaseModel):
    version: int = GRAPH_SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=64)
    name: str = ""
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]*", value):
            raise ValueError("그래프 id 는 영문·숫자·_·- 만 쓸 수 있습니다")
        return value

    def dump(self) -> Dict[str, Any]:
        """파일·스냅샷에 쓰는 모양. 선은 `from`/`to` 이름으로 씁니다."""
        data = self.model_dump(exclude={"edges"})
        data["edges"] = [edge.dump() for edge in self.edges]
        return data

    def node(self, node_id: str) -> Optional[GraphNode]:
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def start(self) -> Optional[GraphNode]:
        return next((n for n in self.nodes if n.type == "start"), None)

    def agent_keys(self) -> List[str]:
        """그래프에 놓인 에이전트 (처음 나온 순서). 그래프 토론에서는 이들이 곧 참여자입니다."""
        keys: List[str] = []
        for node in self.nodes:
            if node.type == "agent" and node.agent and node.agent not in keys:
                keys.append(node.agent)
        return keys

    def in_edges(self, node_id: str) -> List[GraphEdge]:
        return [e for e in self.edges if e.target[0] == node_id]

    def out_edges(self, node_id: str, port: Optional[str] = None) -> List[GraphEdge]:
        return [
            e for e in self.edges
            if e.source[0] == node_id and (port is None or e.source[1] == port)
        ]


# ---------------------------------------------------------------- 구조


def back_edges(spec: GraphSpec) -> Set[str]:
    """되돌림 선의 id. `start` 에서 깊이 우선으로 훑어, 지금 경로 위의 노드로 돌아가는 선.

    `start` 에서 닿지 않는 부분은 선이 적힌 순서대로 이어서 훑습니다. 결과가 선 순서에 따라
    달라지지 않도록 선은 적힌 순서를 지킵니다.
    """
    adjacency: Dict[str, List[GraphEdge]] = {n.id: [] for n in spec.nodes}
    for edge in spec.edges:
        if edge.source[0] in adjacency:
            adjacency[edge.source[0]].append(edge)

    result: Set[str] = set()
    state: Dict[str, int] = {}  # 0 = 처음, 1 = 경로 위, 2 = 끝남

    def visit(root: str) -> None:
        stack: List[Tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            node_id, index = stack[-1]
            edges = adjacency.get(node_id, [])
            if index >= len(edges):
                state[node_id] = 2
                stack.pop()
                continue
            stack[-1] = (node_id, index + 1)
            edge = edges[index]
            target = edge.target[0]
            if target not in adjacency:
                continue
            if state.get(target) == 1:
                result.add(edge.id)
            elif target not in state:
                state[target] = 1
                stack.append((target, 0))

    start = spec.start
    order = ([start.id] if start else []) + [n.id for n in spec.nodes]
    for node_id in order:
        if node_id not in state:
            visit(node_id)
    return result


def _reachable(spec: GraphSpec, roots: Iterable[str], forward: bool = True) -> Set[str]:
    seen: Set[str] = set()
    todo = [r for r in roots]
    while todo:
        node_id = todo.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        for edge in spec.edges:
            a, b = (edge.source[0], edge.target[0]) if forward else (edge.target[0], edge.source[0])
            if a == node_id and b not in seen:
                todo.append(b)
    return seen


def _cycles_without_gate(spec: GraphSpec) -> List[List[str]]:
    """판정 노드를 하나도 거치지 않는 순환.

    "모든 순환이 판정을 지난다" 는 "판정 노드를 빼고 남은 그래프에 순환이 없다" 와 같습니다.
    강하게 연결된 묶음에 판정이 **하나라도 있는지** 로 보면 틀립니다 — `구현 → 취합 → 구현` 이
    판정이 있는 더 큰 루프와 한 묶음이 되면, 판정을 건너뛰는 이 작은 루프가 통과합니다.
    그래서 판정 노드를 뺀 그래프에서 묶음(Tarjan)을 찾습니다.
    """
    gates = {n.id for n in spec.nodes if n.type == "gate"}
    ids = [n.id for n in spec.nodes if n.id not in gates]
    graph: Dict[str, List[str]] = {i: [] for i in ids}
    for edge in spec.edges:
        if edge.source[0] in graph and edge.target[0] in graph:
            graph[edge.source[0]].append(edge.target[0])

    index_of: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    counter = [0]
    components: List[List[str]] = []

    def strongconnect(v: str) -> None:
        # 재귀 대신 명시적 스택 — 노드가 많아도 재귀 한도에 걸리지 않게.
        work = [(v, 0)]
        index_of[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, i = work[-1]
            children = graph[node]
            if i < len(children):
                work[-1] = (node, i + 1)
                w = children[i]
                if w not in index_of:
                    index_of[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, 0))
                elif w in on_stack:
                    low[node] = min(low[node], index_of[w])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                components.append(component)

    for v in ids:
        if v not in index_of:
            strongconnect(v)

    loops = []
    for component in components:
        if len(component) > 1 or component[0] in graph[component[0]]:
            loops.append(sorted(component, key=ids.index))
    return loops


# ---------------------------------------------------------------- 검증


@dataclass
class GraphReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    max_calls: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.errors:
            return f"오류 {len(self.errors)}건: {self.errors[0]}"
        text = f"오류 없음 · 한 턴에 최대 {self.max_calls}번 호출"
        if self.warnings:
            text += f" · 경고 {len(self.warnings)}건"
        return text


def visit_cap(node: GraphNode, default_max_visits: int) -> int:
    return node.max_visits or max(1, int(default_max_visits))


def validate_graph(
    spec: GraphSpec,
    agents: Dict[str, bool],
    *,
    default_max_visits: int = 3,
) -> GraphReport:
    """저장할 때와 턴을 시작할 때 같은 검사를 합니다. `agents` 는 {키: 켜져 있는가}.

    오류가 있으면 턴을 시작하지 않습니다. 경고는 보여 주되 진행합니다.
    """
    report = GraphReport()
    names = {n.id: n.display for n in spec.nodes}

    def name(node_id: str) -> str:
        return f"“{names.get(node_id, node_id)}”"

    seen: Set[str] = set()
    for node in spec.nodes:
        if node.id in seen:
            report.errors.append(f"노드 id {node.id} 가 두 번 쓰였습니다")
        seen.add(node.id)

    starts = [n for n in spec.nodes if n.type == "start"]
    ends = [n for n in spec.nodes if n.type == "end"]
    if len(starts) != 1:
        report.errors.append(
            "시작 노드가 없습니다" if not starts else f"시작 노드는 하나여야 합니다 (지금 {len(starts)}개)"
        )
    if not ends:
        report.errors.append("최종 합성 노드가 없습니다")

    for node in spec.nodes:
        if node.type == "agent":
            if not node.agent:
                report.errors.append(f"{name(node.id)} 노드에 에이전트를 고르세요")
            elif node.agent == ORCHESTRATOR_KEY:
                report.errors.append(
                    f"{name(node.id)}: 오케스트레이터는 에이전트 노드가 될 수 없습니다 — 취합·판정 노드를 쓰세요"
                )
            elif node.agent not in agents:
                report.errors.append(f"{name(node.id)} 노드의 에이전트 {node.agent} 가 없습니다")
            elif not agents[node.agent]:
                report.errors.append(f"{name(node.id)} 노드의 에이전트 {node.agent} 가 꺼져 있습니다")
        if node.type == "gate" and not node.question.strip():
            report.errors.append(f"{name(node.id)} 판정 노드에 질문을 적으세요")

    edge_ids: Set[str] = set()
    node_types = {n.id: n.type for n in spec.nodes}
    for edge in spec.edges:
        if edge.id in edge_ids:
            report.errors.append(f"선 id {edge.id} 가 두 번 쓰였습니다")
        edge_ids.add(edge.id)
        (src, src_port), (dst, dst_port) = edge.source, edge.target
        if src not in node_types or dst not in node_types:
            report.errors.append(f"선 {edge.id} 가 없는 노드를 잇습니다")
            continue
        if src_port not in OUTPUT_PORTS[node_types[src]]:
            report.errors.append(f"선 {edge.id}: {name(src)} 에는 출력 핀 {src_port} 가 없습니다")
        if dst_port not in INPUT_PORTS[node_types[dst]]:
            report.errors.append(f"선 {edge.id}: {name(dst)} 에는 입력 핀 {dst_port} 가 없습니다")

    if report.errors:
        return report

    start = starts[0]
    from_start = _reachable(spec, [start.id])
    to_end = _reachable(spec, [e.id for e in ends], forward=False)
    if not any(e.id in from_start for e in ends):
        report.errors.append("시작에서 최종 합성에 닿는 길이 없습니다")
    for loop in _cycles_without_gate(spec):
        report.errors.append(
            f"{' → '.join(name(n) for n in loop)} 루프는 멈출 조건이 없습니다 — 판정 노드를 넣으세요"
        )

    for node in spec.nodes:
        if node.type == "start":
            continue
        if node.id not in from_start:
            report.warnings.append(f"{name(node.id)} 은(는) 시작에서 닿지 않아 불리지 않습니다")
        elif node.type != "end" and node.id not in to_end:
            report.warnings.append(f"{name(node.id)} 에서 최종 합성으로 가는 길이 없습니다")
        if node.type == "gate":
            for port, label in (("yes", "예"), ("no", "아니오")):
                if not spec.out_edges(node.id, port):
                    report.warnings.append(f"{name(node.id)} 의 “{label}” 갈래가 비어 있어 거기서 멈춥니다")

    calls = 0
    for node in spec.nodes:
        if node.type in ("agent", "merge", "gate") and node.id in from_start:
            calls += visit_cap(node, default_max_visits)
    if start.plan:
        calls += 1
    report.max_calls = calls + 1  # 최종 합성
    return report


def parse_graph(data: Any) -> GraphSpec:
    """파일·스냅샷에서 읽은 dict 를 그래프로. 모양이 틀리면 ValueError."""
    from pydantic import ValidationError

    try:
        return GraphSpec.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"그래프 형식이 틀렸습니다 ({where}: {first.get('msg', exc)})") from exc


def graph_from_card_order(
    graph_id: str,
    name: str,
    agent_keys: Sequence[str],
    *,
    carry: Carry = "full",
) -> GraphSpec:
    """정렬된 카드를 일렬로 이은 그래프. 빈 캔버스 대신 기존 순차 토론에서 출발합니다."""
    nodes = [GraphNode(id="start", type="start", label="시작", pos=(20, 160))]
    edges: List[GraphEdge] = []
    previous = "start"
    for index, key in enumerate(k for k in agent_keys if k != ORCHESTRATOR_KEY):
        node_id = f"n{index + 1}"
        nodes.append(GraphNode(id=node_id, type="agent", agent=key, pos=(240 + 220 * index, 160)))
        edges.append(GraphEdge(id=f"e{index + 1}", source=(previous, "out"), target=(node_id, "in"), carry=carry))
        previous = node_id
    nodes.append(GraphNode(id="end", type="end", label="최종 합성", pos=(240 + 220 * (len(nodes) - 1), 160)))
    edges.append(GraphEdge(id=f"e{len(edges) + 1}", source=(previous, "out"), target=("end", "in")))
    return GraphSpec(id=graph_id, name=name, nodes=nodes, edges=edges)


# ---------------------------------------------------------------- 스케줄러


@dataclass
class Activation:
    node: GraphNode
    # 들어온 선마다 최신 값 (선 순서). 값은 발언 id 목록입니다.
    inputs: List[Tuple[GraphEdge, List[str]]]
    visit: int


class GraphScheduler:
    """받은편지함과 방문 수를 들고, 단계마다 돌릴 노드를 정합니다. LLM 을 모릅니다.

    값은 **발언 id 목록**입니다. 에이전트·취합 노드는 자기 발언 하나, 판정 노드는 판정 기록과
    판정한 입력을 함께 내보냅니다 — 되돌려 받은 에이전트가 무엇 때문에 되돌아왔는지와 무엇을
    고쳐야 하는지를 함께 봐야 합니다.
    """

    def __init__(self, spec: GraphSpec, default_max_visits: int):
        self.spec = spec
        self.default_max_visits = max(1, int(default_max_visits))
        self.back = back_edges(spec)
        self.inbox: Dict[str, Dict[str, List[str]]] = {n.id: {} for n in spec.nodes}
        self.fresh: Dict[str, Set[str]] = {n.id: set() for n in spec.nodes}
        self.visits: Dict[str, int] = {n.id: 0 for n in spec.nodes}
        self.exhausted: List[str] = []

    def deliver(self, node_id: str, port: str, value: Sequence[str]) -> List[str]:
        """노드의 출력을 선을 따라 보냅니다. 받은 노드 id 목록."""
        received = []
        for edge in self.spec.out_edges(node_id, port):
            target = edge.target[0]
            if target not in self.inbox:
                continue
            self.inbox[target][edge.id] = list(value)
            self.fresh[target].add(edge.id)
            received.append(target)
        return received

    def _waiting(self, node: GraphNode) -> bool:
        if node.wait != "all" or self.visits[node.id]:
            return False
        forward = [e for e in self.spec.in_edges(node.id) if e.id not in self.back]
        return any(e.id not in self.inbox[node.id] for e in forward)

    def ready(self) -> List[GraphNode]:
        """이번 단계에 돌 노드 (노드 순서). 상한에 닿은 노드는 새 입력을 버리고 `exhausted` 에 남깁니다."""
        nodes = []
        for node in self.spec.nodes:
            if not self.fresh[node.id] or node.type == "start":
                continue
            if node.type != "end" and self.visits[node.id] >= visit_cap(node, self.default_max_visits):
                self.fresh[node.id].clear()
                self.exhausted.append(node.id)
                continue
            if self._waiting(node):
                continue
            nodes.append(node)
        return nodes

    def activate(self, node: GraphNode) -> Activation:
        self.visits[node.id] += 1
        self.fresh[node.id].clear()
        inputs = [
            (edge, self.inbox[node.id][edge.id])
            for edge in self.spec.in_edges(node.id) if edge.id in self.inbox[node.id]
        ]
        return Activation(node=node, inputs=inputs, visit=self.visits[node.id])

    def pending(self) -> List[str]:
        """입력을 받았지만 "모두 기다림" 때문에 아직 못 도는 노드."""
        return [
            n.id for n in self.spec.nodes
            if self.fresh[n.id] and n.type != "start" and self._waiting(n)
        ]

    def max_steps(self) -> int:
        """단계 상한. 모든 노드가 상한만큼 불려도 이보다 많은 단계는 필요 없습니다."""
        return sum(
            visit_cap(n, self.default_max_visits)
            for n in self.spec.nodes if n.type in ("agent", "merge", "gate")
        ) + 1
