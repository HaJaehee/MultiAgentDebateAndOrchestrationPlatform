"""오케스트레이터가 그리는 실행 그래프 — 프롬프트, 응답 읽기, 허용 범위 (ADR-034).

그래프 토론의 그래프는 사람이 그립니다. 적응형 그래프 토론 전략에서는 오케스트레이터가 계획을 세운 뒤 이번 턴의
그래프를 JSON 으로 그립니다. 대화에 그래프를 골라 두었으면 그것을 기준으로 고쳐 씁니다.

그린 그래프도 **같은 스키마, 같은 검사**를 지납니다 (`validate_graph`). 모델이 쓴 값이라 몇 가지를 더 봅니다.

* 모델이 틀리기 쉬운 것은 읽을 때 채웁니다 — 시작·최종 합성 노드, 선 id, 핀 이름, 갈래가 모이는 취합의 "모두 기다림".
* 비용은 사람이 정한 한도 안에 둡니다 — 방문 상한은 세션의 "최대 라운드" 를 넘지 못하고, 노드 수와 최악의
  호출 수에 상한이 있습니다 (`MAX_DRAFT_NODES`, `MAX_DRAFT_CALLS`).

이 모듈은 LLM 을 부르지 않습니다. 부르는 쪽은 `phases/graph_drafting.py` 입니다.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.agents.reasoning import strip_reasoning_trace
from app.orchestration.graph import (
    CARRY_LABELS,
    GraphReport,
    GraphSpec,
    graph_from_card_order,
    parse_graph,
    validate_graph,
)

# 응답이 틀렸을 때 틀린 곳을 알려 주고 다시 받는 횟수까지 포함한 시도 수.
DRAFT_ATTEMPTS = 2
# 시작·최종 합성을 뺀 노드 수의 상한. 사람이 읽고 말 수 있는 크기여야 합니다.
MAX_DRAFT_NODES = 12
# 한 턴의 최악 호출 수 상한 (`GraphReport.max_calls`). 그린 그래프가 비용을 정하게 두지 않습니다.
MAX_DRAFT_CALLS = 40
DRAFT_PROMPT_MARKER = "[실행 그래프 작성]"

_NODE_FIELDS = ("id", "type", "label", "agent", "instruction", "question", "default", "wait", "max_visits", "sees")


@dataclass
class GraphDraft:
    spec: GraphSpec
    reason: str = ""


def draft_view(spec: GraphSpec) -> Dict[str, Any]:
    """답으로 받는 모양과 같은 모양의 그래프. 기준 그래프를 프롬프트에 실을 때 씁니다."""
    nodes = []
    for node in spec.nodes:
        if node.type in ("start", "end"):
            continue
        item = {k: v for k, v in node.dump().items() if k in _NODE_FIELDS}
        nodes.append(item)
    edges = []
    for edge in spec.edges:
        item = {"from": edge.source[0], "to": edge.target[0]}
        if edge.source[1] != "out":
            item["port"] = edge.source[1]
        if edge.carry != "full":
            item["carry"] = edge.carry
        edges.append(item)
    return {"name": spec.name, "nodes": nodes, "edges": edges}


def draft_prompt(
    *,
    goal: str,
    record: str,
    plan: str,
    roster: str,
    max_rounds: int,
    parallel_limit: int,
    base: Optional[GraphSpec] = None,
    previous: str = "",
    errors: Sequence[str] = (),
) -> List[Dict[str, str]]:
    """그래프를 그려 달라는 프롬프트. `errors` 가 있으면 앞선 답(`previous`)의 틀린 곳을 알려 줍니다."""
    base_block = (
        "[기준 그래프] 유저가 이 대화에 골라 둔 그래프입니다. 이번 작업에 맞게 **고쳐 쓰세요**. 바꿀 이유가 "
        "없는 부분은 그대로 두고, 그대로 충분하면 그대로 답해도 됩니다.\n"
        f"{json.dumps(draft_view(base), ensure_ascii=False)}\n\n"
        if base is not None else ""
    )
    retry_block = (
        f"[앞선 답의 문제] 아래 문제를 고쳐 그래프 전체를 다시 쓰세요.\n"
        + "\n".join(f"- {e}" for e in errors)
        + f"\n\n[앞선 답]\n{previous[:4000]}\n\n"
        if errors else ""
    )
    content = (
        f"{DRAFT_PROMPT_MARKER} 멀티 에이전트 토론의 이번 턴을 어떤 흐름으로 돌릴지 그래프로 그립니다.\n\n"
        f"[목표]\n{goal}\n\n"
        f"{record}"
        f"[이번 턴 계획]\n{plan or '(계획 없음)'}\n\n"
        f"[쓸 수 있는 전문가]\n{roster}\n\n"
        f"{base_block}"
        f"{retry_block}"
        "노드 종류:\n"
        '- agent: 전문가 한 명의 발언. "agent" 에 키, "label" 에 짧은 이름, "instruction" 에 이 차례의 구체적 지시.\n'
        '- merge: 오케스트레이터가 들어온 발언을 하나로 합칩니다. 갈라진 갈래를 모을 때 씁니다.\n'
        '- gate: 오케스트레이터가 예/아니오로 판정합니다. "question" 필수, "default" 는 판정을 못 읽었을 때 갈 쪽.\n'
        "- 시작과 최종 합성은 id 가 start, end 인 노드로 이미 있습니다. 선에서 그 id 만 쓰세요.\n\n"
        "선: {\"from\": 노드 id, \"to\": 노드 id, \"port\": \"yes\"|\"no\" (판정에서 나가는 선만), "
        "\"carry\": \"full\"(전문, 기본)|\"digest\"(요지만)|\"refs\"(긴 코드는 참조로)}\n\n"
        "규칙:\n"
        "- 작업의 성격에 맞추세요. 단순하면 짧게 — 전문가 한두 명으로 끝나도 됩니다. 서로의 결과가 필요 없는 일만 "
        f"병렬로 가르고(동시 실행 상한 {parallel_limit}), 뒤 일이 앞 결과를 쓰면 줄지어 이으세요.\n"
        "- 갈라진 갈래는 merge 로 모은 뒤 최종 합성으로 보내세요.\n"
        "- 되돌아가는 선은 반드시 gate 를 거쳐야 합니다. 검토 후 고칠 일이 실제로 있을 때만 루프를 만드세요.\n"
        f"- 한 노드는 한 턴에 최대 {max_rounds}번까지 불립니다. 시작·최종 합성을 빼고 노드는 {MAX_DRAFT_NODES}개까지입니다.\n"
        "- 위 목록의 전문가만 쓰세요. 유저가 승인한 태스크 분담이 있으면 그 전문가를 빠뜨리지 마세요.\n\n"
        "다음 JSON 형식으로만 답하세요:\n"
        '{"name": "그래프 이름", "reason": "이 흐름을 고른 이유 한두 문장", '
        '"nodes": [{"id": "design", "type": "agent", "agent": "에이전트키", "label": "설계", "instruction": "..."}], '
        '"edges": [{"from": "start", "to": "design"}, {"from": "design", "to": "end"}]}'
    )
    return [{"role": "user", "content": content}]


def _endpoint(raw: Any) -> Tuple[str, Optional[str]]:
    """선의 한쪽 끝. `"design"` 도, 파일 형식의 `["design", "out"]` 도 받습니다."""
    if isinstance(raw, (list, tuple)) and raw:
        return str(raw[0]), (str(raw[1]) if len(raw) > 1 else None)
    return str(raw or ""), None


def _clean_nodes(raw_nodes: Iterable[Any], max_visits: int) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        node = {k: raw[k] for k in _NODE_FIELDS if raw.get(k) not in (None, "")}
        node["id"] = str(node.get("id") or "")
        if node["id"] in ("start", "end"):
            node.setdefault("type", node["id"])
        if "max_visits" in node:
            try:
                node["max_visits"] = max(1, min(int(node["max_visits"]), max_visits))
            except (TypeError, ValueError):
                del node["max_visits"]
        nodes.append(node)
    types = {n.get("type") for n in nodes}
    if "start" not in types:
        nodes.insert(0, {"id": "start", "type": "start", "label": "시작"})
    if "end" not in types:
        nodes.append({"id": "end", "type": "end", "label": "최종 합성"})
    return nodes


def _clean_edges(raw_edges: Iterable[Any], node_types: Dict[str, Any]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            continue
        source, source_port = _endpoint(raw.get("from"))
        target, _target_port = _endpoint(raw.get("to"))
        port = "out"
        if node_types.get(source) == "gate":
            # 판정의 갈래를 빠뜨렸으면 그대로 둡니다 — 검사가 "출력 핀 out 이 없습니다" 로 알려 줍니다.
            port = str(raw.get("port") or source_port or "out").strip().lower()
        carry = raw.get("carry")
        edges.append({
            "id": f"e{len(edges) + 1}",
            "from": [source, port],
            "to": [target, "in"],
            "carry": carry if carry in CARRY_LABELS else "full",
        })
    return edges


def parse_draft(content: str, graph_id: str, max_visits: int) -> GraphDraft:
    """응답에서 그래프를 읽습니다. 읽을 수 없으면 사람이 읽을 문장의 ValueError.

    문장은 다시 그려 달라는 프롬프트에도 그대로 들어갑니다.
    """
    text = strip_reasoning_trace(content or "")
    block = re.search(r"\{.*\}", text, re.DOTALL)
    if block is None:
        raise ValueError("응답에 JSON 이 없습니다")
    try:
        data = json.loads(block.group(0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"JSON 을 읽지 못했습니다 ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise ValueError("nodes 와 edges 목록이 없습니다")

    nodes = _clean_nodes(data["nodes"], max_visits)
    edges = _clean_edges(data["edges"], {n["id"]: n.get("type") for n in nodes})
    # 갈라진 갈래를 모으는 취합은 "모두 기다림" 이 기본입니다. 비워 두면 먼저 도착한 갈래만 보고 취합합니다.
    # 판정의 예·아니오 두 갈래가 모이는 자리는 빼야 합니다 — 둘은 함께 오지 않습니다.
    sources = {n["id"]: n.get("type") for n in nodes}
    for node in nodes:
        if node.get("type") == "merge" and "wait" not in node:
            incoming = [e for e in edges if e["to"][0] == node["id"] and sources.get(e["from"][0]) != "gate"]
            if len(incoming) > 1:
                node["wait"] = "all"

    spec = parse_graph({
        "id": graph_id,
        "name": str(data.get("name") or "오케스트레이터 그래프").strip()[:80],
        "nodes": nodes,
        "edges": edges,
    })
    return GraphDraft(spec=spec, reason=str(data.get("reason") or "").strip())


def draft_report(spec: GraphSpec, candidates: Iterable[str], max_rounds: int) -> GraphReport:
    """그린 그래프를 돌려도 되는지. 사람이 그린 그래프의 검사에 비용 상한을 더합니다."""
    report = validate_graph(spec, {key: True for key in candidates}, default_max_visits=max_rounds)
    if not spec.agent_keys():
        report.errors.append("전문가 노드가 하나도 없습니다")
    working = sum(1 for n in spec.nodes if n.type in ("agent", "merge", "gate"))
    if working > MAX_DRAFT_NODES:
        report.errors.append(f"노드가 {working}개입니다 — {MAX_DRAFT_NODES}개까지만 쓰세요")
    if report.max_calls > MAX_DRAFT_CALLS:
        report.errors.append(
            f"최악의 경우 한 턴에 {report.max_calls}번 호출합니다 — {MAX_DRAFT_CALLS}번 이내로 줄이세요"
        )
    return report


def fallback_graph(
    graph_id: str, candidates: Sequence[str], base: Optional[GraphSpec], max_rounds: int,
) -> GraphSpec:
    """그리지 못했을 때 돌 그래프: 쓸 수 있는 기준 그래프, 없으면 카드 순서로 이은 줄."""
    if base is not None and validate_graph(base, {k: True for k in candidates}, default_max_visits=max_rounds).ok:
        return base
    return graph_from_card_order(graph_id, f"카드 순서 · {len(candidates)}명", candidates)


def draft_note(spec: GraphSpec, reason: str, report: GraphReport) -> str:
    """그린 그래프를 기록과 피드에 남기는 문장. 그림은 로스터의 실행 표시가 그립니다."""
    lines = [f"[실행 그래프] **{spec.name or spec.id}**"]
    if reason:
        lines += ["", reason]
    lines.append("")
    lines += _flow_lines(spec)
    lines += ["", f"한 턴에 최대 {report.max_calls}번 호출"]
    lines += [f"- ⚠️ {w}" for w in report.warnings]
    return "\n".join(lines)


def fallback_note(spec: GraphSpec, why: str, report: GraphReport) -> str:
    return "\n".join([
        f"[실행 그래프 작성 실패] {why}",
        "",
        f"대신 “{spec.name or spec.id}” 로 진행합니다.",
        "",
        *_flow_lines(spec),
        "",
        f"한 턴에 최대 {report.max_calls}번 호출",
    ])


def _flow_lines(spec: GraphSpec) -> List[str]:
    """노드마다 "A → B, C" 한 줄. 판정의 갈래는 예/아니오를 붙입니다."""
    names = {n.id: n.display for n in spec.nodes}
    lines: List[str] = []
    for node in spec.nodes:
        for port, label in (("out", ""), ("yes", " (예)"), ("no", " (아니오)")):
            targets = [names.get(e.target[0], e.target[0]) for e in spec.out_edges(node.id, port)]
            if targets:
                lines.append(f"- {node.display}{label} → {', '.join(targets)}")
    return lines
