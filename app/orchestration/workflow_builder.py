"""대화로 워크플로 만들기 — 실행 그래프와 필요한 새 에이전트를 함께 설계합니다 (ADR-050).

에이전트 빌더(`app/agents/draft.py`)의 대화 방식과 적응형 그래프(`graph_draft.py`)의 그래프 규격을 합칩니다.
도우미는 오케스트레이터의 연결 설정을 쓰는 **도구 없는 사본**이고(`builder_agent`), 답 끝에 두 종류의
블록을 붙입니다.

* ```` ```agents ```` — 새로 만들 에이전트 전체 목록 (배열)
* ```` ```graph ```` — 실행 그래프 전체 (`draft_view` 와 같은 모양)

화면에서 고친 값은 다음 말과 함께 `[지금 설계도]` 로 돌아갑니다. 기록에는 설계도를 **가장 최근 사용자
메시지에만** 붙입니다 — 그래프와 여러 에이전트는 수 KB 라, 턴마다 쌓으면 금방 맥락 한도에 닿습니다.

이 모듈은 화면을 모릅니다. 저장(`save_workflow`)도 여기 있어 브라우저 없이 시험합니다. 원칙은 **쓰기 전에
모든 검사를 끝내는 것** — 그래야 쓰는 도중 검증 때문에 실패해 되돌릴 일이 없습니다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field

from app.agents.base import CARD_COLOR_CHOICES, ICON_CHOICES, Agent
from app.agents.catalog import Option
from app.agents.draft import (
    AgentDraft,
    SaveRefused,
    agent_key_for,
    builder_agent,
    merge_draft,
    require_complete,
    sanitize_draft,
)
from app.agents.llm import LLMCaller
from app.agents.pool import AgentPool, get_agent_pool, reload_agent_pool
from app.agents.reasoning import strip_reasoning_trace
from app.config import (
    DEFAULT_CONFIG_PATH,
    ORCHESTRATOR_KEY,
    active_config_path,
    add_agents_to_conf_file,
    get_config,
)
from app.graph_store import free_graph_id, save_graph
from app.orchestration.artifacts import format_roster
from app.orchestration.graph import GraphNode, GraphSpec
from app.orchestration.graph_draft import (
    DRAFT_ATTEMPTS,
    GRAPH_FORMAT_GUIDE,
    MAX_DRAFT_CALLS,
    MAX_DRAFT_NODES,
    draft_report,
    draft_view,
    spec_from_draft,
)
from app.orchestration.strategies import order_by_priority, specialists_of

# 저장하기 전 그래프의 임시 id. 저장할 때 정합니다 (`save_workflow`).
DRAFT_GRAPH_ID = "builder-draft"
# 도우미 응답에 문제가 있을 때 고쳐 받는 횟수까지 포함한 시도 수 (적응형 그래프와 같은 규칙).
BUILDER_ATTEMPTS = DRAFT_ATTEMPTS
CURRENT_DRAFT_MARKER = "[지금 설계도]"
CORRECTION_MARKER = "[앞선 설계의 문제]"
COMPACT_NOTE = "(설계도 블록 — 다음 사용자 메시지의 [지금 설계도] 참조)"
# 새 에이전트 키 규칙. `app/agents/draft.py` 의 저장 규칙과 같습니다.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_FENCE_RE = re.compile(r"```(agents?|graph)\b[^\n]*")
_BLOCK_START_RE = re.compile(r"```(?:agents?|graph)\b")
_DECODER = json.JSONDecoder()


# ---------------------------------------------------------------------------
# 설계도
# ---------------------------------------------------------------------------


class CompositeDraft(BaseModel):
    """대화 중에 쌓이고 고쳐지는 설계도. 화면과 도우미가 같은 값을 봅니다."""

    new_agents: List[AgentDraft] = Field(default_factory=list)
    # 사용자가 랙에서 뺀 새 에이전트 키. 도우미가 다시 만들어 넣지 않도록 함께 알립니다.
    removed_agents: List[str] = Field(default_factory=list)
    # 도우미가 보낸 모양 그대로의 그래프. 읽지 못한 답이 와도 마지막으로 읽은 값을 지킵니다.
    graph_data: Optional[Dict[str, Any]] = None
    # `graph_data` 를 읽은 결과. id 는 임시 값(`DRAFT_GRAPH_ID`)이고 저장할 때 정합니다.
    graph: Optional[GraphSpec] = None
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    worst_case_calls: int = 0
    # 편집기에서 열었을 때 고쳐 쓰는 그래프의 id. 대화 화면에서 새로 만들면 빈 값입니다.
    base_graph_id: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.new_agents or self.removed_agents or self.graph is not None)

    def agent(self, key: str) -> Optional[AgentDraft]:
        return next((a for a in self.new_agents if a.key == key), None)


@dataclass
class BuilderContext:
    """설계와 검사에 쓰는 지금의 구성. 대화 중에는 열 때 한 번, 저장할 때 다시 만듭니다."""

    # 그래프에 놓을 수 있는 기존 전문가 (켜진 것, 오케스트레이터 제외).
    candidates: List[Agent]
    # 새 에이전트가 쓸 수 없는 키 — conf.json 의 모든 에이전트(꺼진 것 포함)와 오케스트레이터.
    taken: Set[str]
    servers: List[Option]
    skills: List[Option]
    # 상한을 적지 않은 노드를 몇 번으로 셀지 (대화의 최대 라운드 또는 편집기의 미리보기 값).
    max_visits: int = 3

    @property
    def candidate_keys(self) -> List[str]:
        return [a.key for a in self.candidates]


def builder_context(
    servers: Sequence[Option],
    skills: Sequence[Option],
    *,
    max_visits: int = 3,
    pool: Optional[AgentPool] = None,
    configured: Optional[Iterable[str]] = None,
) -> BuilderContext:
    """지금 풀과 conf.json 으로 `BuilderContext` 를 만듭니다."""
    pool = pool or get_agent_pool()
    if configured is None:
        configured = get_config().agents.keys()
    return BuilderContext(
        candidates=order_by_priority(specialists_of(pool.list_all())),
        taken=set(configured) | {ORCHESTRATOR_KEY},
        servers=list(servers),
        skills=list(skills),
        max_visits=max(1, int(max_visits)),
    )


def draft_from_graph(spec: GraphSpec) -> CompositeDraft:
    """편집기의 지금 그래프에서 시작하는 설계도. 좌표는 그대로 들고 갑니다 (`apply_reply` 가 이어 씁니다)."""
    graph = spec.model_copy(update={"id": DRAFT_GRAPH_ID}, deep=True)
    return CompositeDraft(graph=graph, graph_data=draft_view(graph), base_graph_id=spec.id)


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------

_PROMPT = """당신은 '워크플로 설계 도우미'입니다. 대화 상대는 개발자가 아닌 일반 직장인입니다. 사용자가 맡기려는 업무의 흐름을 파악해, 여러 AI 전문가가 차례로 또는 동시에 일하는 **실행 그래프**를 설계합니다. 쓸 수 있는 전문가 중에 맞는 사람이 없으면 새 전문가(에이전트)도 함께 설계합니다.

## 대화 방식
- 알기 쉬운 한국어로 간결하게 대화합니다. 전문 용어를 쓸 때는 바로 뒤 괄호 안에 쉬운 설명을 덧붙입니다.
- 질문은 한 번에 최대 두 개까지만 합니다. 이미 확인한 내용은 다시 묻지 않습니다.
- 두세 번의 대화 안에 초안을 완성합니다. 정보가 부족하면 합리적인 기본값으로 먼저 그린 뒤 고칠 부분을 묻습니다.
- 설계도를 바꿀 때마다 무엇을 왜 바꿨는지 한두 문장으로 설명합니다. 새 전문가를 만들었다면 왜 기존 전문가로는 부족했는지, 어떤 도구를 왜 주었는지 알려 줍니다.

## 쓸 수 있는 전문가 (키: 이름 (역할) · 도구)
{roster}

## 이미 쓰인 키 — 새 전문가는 이 키를 쓸 수 없습니다
{taken}

## 그래프 규격
{graph_format}

## 설계 규칙
- 작업의 성격에 맞춥니다. 단순하면 짧게 — 전문가 한두 명으로 끝나도 됩니다. 서로의 결과가 필요 없는 일만 병렬로 가르고, 뒤 일이 앞 결과를 쓰면 줄지어 잇습니다.
- 갈라진 갈래는 merge 로 모은 뒤 다음 단계나 최종 합성으로 보냅니다.
- 되돌아가는 선은 반드시 gate 를 거칩니다. 루프는 고칠 사람이 있을 때만 만듭니다 (검토 → 판정 → 수정 → 검토). 같은 검토자에게 되돌리기만 하는 루프는 만들지 않습니다.
- 루프 안의 노드에는 "max_visits" (2~3) 를 적습니다. 그래야 비용이 정해집니다.
- 시작·최종 합성을 빼고 노드는 {max_nodes}개까지, 최악의 경우 한 턴의 호출은 {max_calls}번까지입니다.
- agent 노드에는 그 차례에 할 일을 "instruction" 에 구체적으로 적습니다.
- 위 목록에 맞는 전문가가 있으면 그 키를 씁니다. 없을 때만 새 전문가를 만듭니다. 오케스트레이터(orchestrator)는 agent 노드가 될 수 없습니다 — 취합(merge)과 판정(gate)이 그 역할입니다.
- 이미 있는 전문가의 설정(도구, 업무 지시서 등)을 바꿔 달라는 요청에는 설계도를 바꾸지 말고, 로스터 화면에서 직접 고칠 수 있다고 안내합니다.

## 새 전문가의 네 가지 요소
- 페르소나: name(이름), role(한 줄 역할)
- 업무 지시서: system_prompt — "당신은 ..." 으로 시작하는 2인칭. 목표, 작업 절차(번호 목록), 결과물 양식, 도구를 쓸 때, 주의·금지 사항을 적습니다.
- 도구: allowed_mcp_servers — 아래 목록의 id 만 씁니다. 도구가 없으면 글로만 일합니다.
- 스킬: allowed_skills — 아래 목록의 id 만 씁니다.

## 고를 수 있는 도구 (id 만 씁니다)
{tools}

## 고를 수 있는 스킬 (id 만 씁니다)
{skills}

## 설계도 블록 — 설계도를 바꿀 때 답의 맨 끝에 붙입니다
바꾼 블록만 붙입니다. 블록을 붙이지 않으면 그 부분은 그대로라는 뜻입니다. 블록 밖에는 JSON 을 쓰지 않습니다.

새 전문가 **전체** 목록 (새 전문가가 없으면 빈 배열 []):
```agents
[{"key": "영문 소문자와 밑줄로 된 식별자", "name": "이름", "role": "한 줄 역할", "system_prompt": "업무 지시서", "allowed_mcp_servers": ["도구 id"], "allowed_skills": ["스킬 id"], "card_color": "#rrggbb", "icon": "아이콘 이름"}]
```
- card_color 는 다음 중 하나입니다: {colors}
- icon 은 다음 중 하나입니다: {icons}

실행 그래프 **전체**:
```graph
{"name": "그래프 이름", "nodes": [{"id": "review", "type": "agent", "agent": "에이전트키", "label": "검토", "instruction": "..."}], "edges": [{"from": "start", "to": "review"}, {"from": "review", "to": "end"}]}
```

사용자 메시지 끝에 [지금 설계도]가 붙어 있으면 사용자가 화면에서 직접 고친 최신 값입니다. 그 값을 가장 먼저 따르고, removed_agents 에 있는 키는 사용자가 다시 요청하지 않는 한 만들지 않습니다."""


def _option_lines(options: Sequence[Option], empty: str) -> str:
    lines = [f"- `{o.id}`: {o.label}" + (f" — {o.description}" if o.description else "") for o in options]
    return "\n".join(lines) or empty


def workflow_builder_prompt(ctx: BuilderContext) -> str:
    roster = format_roster(ctx.candidates, with_keys=True) or "- (없습니다. 필요한 전문가를 새로 만드세요)"
    return (
        _PROMPT.replace("{roster}", roster)
        .replace("{taken}", ", ".join(sorted(ctx.taken)))
        .replace("{graph_format}", GRAPH_FORMAT_GUIDE)
        .replace("{max_nodes}", str(MAX_DRAFT_NODES))
        .replace("{max_calls}", str(MAX_DRAFT_CALLS))
        .replace("{tools}", _option_lines(ctx.servers, "- (지금 쓸 수 있는 도구가 없습니다. 빈 목록으로 두세요)"))
        .replace("{skills}", _option_lines(ctx.skills, "- (등록된 스킬이 없습니다. 빈 목록으로 두세요)"))
        .replace("{colors}", ", ".join(c["hex"] for c in CARD_COLOR_CHOICES))
        .replace("{icons}", ", ".join(ICON_CHOICES))
    )


async def ask_workflow_builder(
    messages: List[Dict[str, str]],
    ctx: BuilderContext,
    *,
    on_chunk: Optional[Callable[[str], Any]] = None,
    llm: Optional[Any] = None,
    pool: Optional[AgentPool] = None,
) -> str:
    """도우미에게 묻고 전체 답을 돌려줍니다. 연결할 수 없으면 `LLMUnavailableError`."""
    agent = builder_agent(pool or get_agent_pool(), workflow_builder_prompt(ctx))
    content, _ = await (llm or LLMCaller()).call_agent(agent, messages, on_chunk=on_chunk)
    return content


def with_composite_draft(text: str, draft: CompositeDraft) -> str:
    """사용자 말에 지금 설계도를 붙입니다. 화면에서 고친 값을 도우미가 덮어쓰지 않게 합니다."""
    if draft.is_empty:
        return text
    payload = {
        "agents": [a.model_dump() for a in draft.new_agents],
        "graph": draft_view(draft.graph) if draft.graph is not None else draft.graph_data,
        "removed_agents": list(draft.removed_agents),
    }
    return f"{text}\n\n{CURRENT_DRAFT_MARKER}\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


def request_messages(history: Sequence[Dict[str, str]], draft: CompositeDraft) -> List[Dict[str, str]]:
    """도우미에게 보낼 대화. 기록은 그대로 두고, 마지막 사용자 말에만 지금 설계도를 붙입니다."""
    messages = [dict(m) for m in history]
    for message in reversed(messages):
        if message.get("role") == "user":
            message["content"] = with_composite_draft(message.get("content", ""), draft)
            break
    return messages


def correction_message(problems: Sequence[str]) -> str:
    """앞선 답의 문제를 알려 주고 블록을 다시 받는 말. 기록에는 남기지 않습니다."""
    return (
        f"{CORRECTION_MARKER} 방금 보낸 설계도에 아래 문제가 있습니다. 고쳐서 설계도 블록을 다시 보내 주세요. "
        "설명은 한두 문장이면 됩니다.\n" + "\n".join(f"- {p}" for p in problems)
    )


# ---------------------------------------------------------------------------
# 답 읽기
# ---------------------------------------------------------------------------


@dataclass
class CompositeReply:
    """도우미 답을 나눈 결과. `None` 은 "이번 답에 그 블록이 없음" — 지금 값을 그대로 둡니다."""

    text: str
    agents: Optional[List[Dict[str, Any]]] = None
    graph: Optional[Dict[str, Any]] = None
    # 블록은 있었지만 읽지 못한 것 (사람이 읽을 문장). 도우미에게 고쳐 달라고 돌려줍니다.
    broken: List[str] = field(default_factory=list)

    @property
    def has_blocks(self) -> bool:
        return self.agents is not None or self.graph is not None or bool(self.broken)


def _decode_block(text: str, start: int) -> Tuple[Any, int]:
    """`start` 뒤의 첫 JSON 값과 그 끝 위치. 읽지 못하면 ValueError.

    정규식으로 닫는 펜스까지 자르지 않습니다 — 업무 지시서 안에 ``` 가 있으면 거기서 잘리기 때문입니다.
    """
    index = start
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text) or text[index] not in "[{":
        raise ValueError("JSON 이 없습니다")
    return _DECODER.raw_decode(text, index)


def split_composite_reply(content: str) -> CompositeReply:
    """도우미 답을 (보여 줄 말, 새 에이전트 목록, 그래프) 로 나눕니다. 태그마다 마지막 블록을 씁니다.

    `agent` 태그도 받고, 배열 대신 객체 하나가 와도 받습니다. `json` 태그는 받지 않습니다 — 어느 블록인지
    알 수 없습니다.
    """
    text = strip_reasoning_trace(content or "")
    reply = CompositeReply(text="")
    spans: List[Tuple[int, int]] = []
    position = 0
    for match in _FENCE_RE.finditer(text):
        if match.start() < position:
            continue
        kind = "graph" if match.group(1) == "graph" else "agents"
        label = "그래프" if kind == "graph" else "새 에이전트"
        value: Any = None
        try:
            value, end = _decode_block(text, match.end())
            close = text.find("```", end)
        except ValueError as exc:
            reply.broken.append(f"{label} 블록의 JSON 을 읽지 못했습니다 ({exc})")
            close = text.find("```", match.end())
            value = None
        block_end = close + 3 if close != -1 else len(text)
        spans.append((match.start(), block_end))
        position = block_end
        if value is None:
            continue
        if kind == "graph":
            if isinstance(value, dict):
                reply.graph = value
            else:
                reply.broken.append("그래프 블록은 JSON 객체여야 합니다")
        else:
            items = [value] if isinstance(value, dict) else value
            if isinstance(items, list) and all(isinstance(item, dict) for item in items):
                reply.agents = list(items)
            else:
                reply.broken.append("새 에이전트 블록은 객체의 배열이어야 합니다")

    visible = text
    for start, end in reversed(spans):
        visible = visible[:start] + visible[end:]
    reply.text = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return reply


def streaming_text(text: str) -> str:
    """스트리밍 중인 답에서 설계도 블록이 시작되는 곳부터 가립니다."""
    match = _BLOCK_START_RE.search(text or "")
    return (text[:match.start()] if match else text or "").strip()


def compact_reply(content: str) -> str:
    """기록에 남길 도우미 답. 블록은 한 줄로 줄입니다 — 최신 설계도는 다음 말의 `[지금 설계도]` 에 있습니다."""
    reply = split_composite_reply(content)
    if not reply.has_blocks:
        return reply.text
    return f"{reply.text}\n\n{COMPACT_NOTE}".strip()


# ---------------------------------------------------------------------------
# 합치기·검사
# ---------------------------------------------------------------------------


def _carry_positions(new: GraphSpec, old: Optional[GraphSpec]) -> GraphSpec:
    """노드가 그대로면 편집기 좌표를 이어 씁니다. 하나라도 새 노드가 있으면 전부 자동 배치합니다.

    섞어 두면 자동 배치한 새 노드가 사람이 놓은 노드 위에 겹칩니다.
    """
    if old is None:
        return new
    places = {n.id: n.pos for n in old.nodes if n.pos is not None}
    if not new.nodes or any(n.id not in places for n in new.nodes):
        return new
    nodes = [n.model_copy(update={"pos": places[n.id]}) for n in new.nodes]
    return new.model_copy(update={"nodes": nodes})


def check_composite(draft: CompositeDraft, ctx: BuilderContext) -> CompositeDraft:
    """설계도를 검사해 오류·경고·최악 호출 수를 채운 사본을 돌려줍니다.

    그래프는 적응형 그래프와 같은 `draft_report`(검증 + 노드 12개·호출 40회 상한)로 봅니다. 여기에 새
    에이전트 검사를 더합니다 — 특히 **키 충돌**. 이미 있는 키와 겹친 채 저장하면 `agent_key_for` 가 키를
    바꾸고, 노드는 기존 에이전트를 가리킨 채 검증을 통과해 오류 없이 다른 에이전트가 발언합니다.
    """
    errors: List[str] = []
    warnings: List[str] = []
    seen: Set[str] = set()
    for agent in draft.new_agents:
        label = agent.name or agent.key or "이름 없는 에이전트"
        if not agent.key:
            errors.append(f"새 에이전트 “{label}” 에 키가 없습니다")
        elif not KEY_PATTERN.fullmatch(agent.key):
            errors.append(f"새 에이전트 키 {agent.key} 는 영문 소문자로 시작하고 소문자·숫자·밑줄만 써야 합니다")
        elif agent.key in ctx.taken:
            errors.append(f"새 에이전트 키 {agent.key} 는 이미 있습니다 — 다른 키를 쓰거나 기존 에이전트를 쓰세요")
        elif agent.key in seen:
            errors.append(f"새 에이전트 키 {agent.key} 가 두 번 쓰였습니다")
        seen.add(agent.key)
        missing = [name for value, name in (
            (agent.name, "이름"), (agent.role, "한 줄 역할"), (agent.system_prompt, "업무 지시서"),
        ) if not value]
        if missing:
            errors.append(f"새 에이전트 “{label}” 의 {', '.join(missing)} 이(가) 비어 있습니다")

    calls = 0
    if draft.graph is not None:
        report = draft_report(draft.graph, ctx.candidate_keys + [a.key for a in draft.new_agents if a.key],
                              ctx.max_visits)
        errors.extend(report.errors)
        warnings.extend(report.warnings)
        calls = report.max_calls
        used = set(draft.graph.agent_keys())
        for agent in draft.new_agents:
            if agent.key and agent.key not in used:
                warnings.append(f"새 에이전트 {agent.key} 는 그래프에 쓰이지 않습니다 — 저장하면 쓰이지 않는 에이전트가 등록됩니다")
    return draft.model_copy(update={"errors": errors, "warnings": warnings, "worst_case_calls": calls})


def apply_reply(current: CompositeDraft, reply: CompositeReply, ctx: BuilderContext) -> Tuple[CompositeDraft, List[str]]:
    """도우미 답을 설계도에 합칩니다. (새 설계도, 도우미에게 고쳐 달라고 할 문제) 를 돌려줍니다.

    문제는 **이번 답이 만든 것만** 셉니다. 블록이 없는 답(질문만 한 답)이나 사용자가 화면에서 고쳐 생긴
    오류로는 다시 쓰게 하지 않습니다 — 사용자 수정을 덮어씁니다.
    """
    draft = current.model_copy(deep=True)
    problems: List[str] = list(reply.broken)
    server_ids = [o.id for o in ctx.servers]
    skill_ids = [o.id for o in ctx.skills]

    if reply.agents is not None:
        merged: Dict[str, AgentDraft] = {}
        for data in reply.agents:
            key = str(data.get("key") or "").strip()
            base = current.agent(key) if key else None
            agent = sanitize_draft(merge_draft(base or AgentDraft(), data), server_ids, skill_ids)
            merged[agent.key] = agent
        draft.new_agents = list(merged.values())
        # 도우미가 뺀 키를 다시 만들었다면 사용자가 다시 요청한 것입니다.
        draft.removed_agents = [k for k in current.removed_agents if k not in merged]

    if reply.graph is not None:
        try:
            spec = spec_from_draft(reply.graph, DRAFT_GRAPH_ID, ctx.max_visits)
        except ValueError as exc:
            problems.append(f"그래프를 읽지 못했습니다: {exc}")
        else:
            draft.graph = _carry_positions(spec, current.graph)
            draft.graph_data = reply.graph

    draft = check_composite(draft, ctx)
    if reply.has_blocks and draft.graph is not None:
        problems.extend(e for e in draft.errors if e not in problems)
    return draft, problems


def remove_new_agent(draft: CompositeDraft, key: str, ctx: BuilderContext) -> CompositeDraft:
    """랙에서 새 에이전트를 뺍니다. 그 키를 쓰던 노드는 검사 오류로 보입니다."""
    updated = draft.model_copy(update={
        "new_agents": [a for a in draft.new_agents if a.key != key],
        "removed_agents": list(dict.fromkeys([*draft.removed_agents, key])),
    }, deep=True)
    return check_composite(updated, ctx)


def replace_new_agent(draft: CompositeDraft, key: str, agent: AgentDraft, ctx: BuilderContext) -> CompositeDraft:
    """랙에서 고친 새 에이전트를 바꿔 끼웁니다. 키를 바꿨으면 그 키를 쓰던 노드도 함께 바꿉니다."""
    fixed = sanitize_draft(agent, [o.id for o in ctx.servers], [o.id for o in ctx.skills])
    agents = [fixed if a.key == key else a for a in draft.new_agents]
    graph = draft.graph
    if graph is not None and fixed.key != key:
        graph = rename_agents(graph, {key: fixed.key})
    updated = draft.model_copy(update={
        "new_agents": agents, "graph": graph,
        "graph_data": draft_view(graph) if graph is not None else draft.graph_data,
    }, deep=True)
    return check_composite(updated, ctx)


def rename_agents(spec: GraphSpec, renamed: Dict[str, str]) -> GraphSpec:
    """agent 노드가 가리키는 키를 바꾼 사본."""
    if not renamed:
        return spec
    nodes: List[GraphNode] = [
        n.model_copy(update={"agent": renamed[n.agent]}) if n.type == "agent" and n.agent in renamed else n
        for n in spec.nodes
    ]
    return spec.model_copy(update={"nodes": nodes})


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------


@dataclass
class SavedWorkflow:
    """저장 결과. `graph_error` 가 있으면 에이전트는 등록했지만 그래프 파일을 쓰지 못한 것입니다."""

    spec: GraphSpec
    agent_keys: List[str]
    renamed: Dict[str, str]
    graph_error: str = ""


def new_graph_id(directory: Optional[Path] = None, now: Optional[datetime] = None) -> str:
    """새 그래프 파일의 id. 이름에서 만들지 않습니다 — `free_graph_id` 는 한글을 걷어내 전부 `graph` 가 됩니다."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    return free_graph_id(f"ai-{stamp}", directory)


def prepare_save(
    draft: CompositeDraft,
    ctx: BuilderContext,
    *,
    graph_id: str,
) -> Tuple[List[Tuple[str, AgentDraft]], GraphSpec, Dict[str, str]]:
    """쓰기 전 최종 검사. (최종 키와 에이전트, 저장할 그래프, 바뀐 키) — 틀리면 `SaveRefused`.

    `ctx` 는 저장하는 지금 다시 만든 것이어야 합니다. 대화하는 사이 다른 창에서 같은 키의 에이전트가
    생겼으면 `agent_key_for` 로 새 키를 받고, 그 키를 쓰던 노드도 함께 바꿉니다.
    """
    if draft.graph is None:
        raise SaveRefused("아직 그래프가 없습니다. 도우미와 대화해 그래프를 먼저 만들어 주세요.")

    server_ids = [o.id for o in ctx.servers]
    skill_ids = [o.id for o in ctx.skills]
    used = set(ctx.taken)
    agents: List[Tuple[str, AgentDraft]] = []
    renamed: Dict[str, str] = {}
    for raw in draft.new_agents:
        agent = sanitize_draft(raw, server_ids, skill_ids)
        try:
            require_complete(agent)
        except SaveRefused as exc:
            raise SaveRefused(f"새 에이전트 “{agent.name or agent.key}”: {exc}") from exc
        key = agent.key if KEY_PATTERN.fullmatch(agent.key) and agent.key not in used else agent_key_for(agent, used)
        if key != agent.key:
            renamed[agent.key] = key
        used.add(key)
        agents.append((key, agent.model_copy(update={"key": key})))

    spec = rename_agents(draft.graph, renamed).model_copy(update={"id": graph_id})
    final = check_composite(
        draft.model_copy(update={"new_agents": [a for _, a in agents], "graph": spec}),
        BuilderContext(ctx.candidates, set(ctx.taken), ctx.servers, ctx.skills, ctx.max_visits),
    )
    if final.errors:
        raise SaveRefused(f"설계도에 오류가 {len(final.errors)}건 있습니다: {final.errors[0]}")
    return agents, spec, renamed


def save_workflow(
    draft: CompositeDraft,
    ctx: BuilderContext,
    *,
    graph_id: str,
    running: Sequence[str] = (),
    write_graph: bool = True,
    config_path: Optional[str | Path] = None,
    directory: Optional[Path] = None,
) -> SavedWorkflow:
    """새 에이전트를 conf.json 에 한 번에 등록하고 그래프를 저장합니다.

    1. 새 에이전트가 있으면, 어느 대화든 돌고 있을 때 거부합니다 (`save_owner_agent` 와 같은 규칙). 에이전트
       풀은 프로세스 전체가 하나라서, 돌고 있는 토론의 발언자 목록이 도중에 바뀝니다.
    2. 쓰기 전에 모든 검사를 끝냅니다 (`prepare_save`).
    3. conf.json 에 한 번에 쓰고 풀을 다시 채웁니다.
    4. 그래프 파일을 씁니다 (`write_graph=False` 면 쓰지 않고 돌려만 줍니다 — 편집기는 캔버스에 적용하고
       자기 [저장] 으로 씁니다). 실패해도 conf.json 은 되돌리지 않습니다. 등록된 에이전트는 남아도 해가
       없고, 통째로 되돌리면 그 사이 다른 창이 고친 설정까지 지워집니다.
    """
    if draft.new_agents and running:
        raise SaveRefused(
            "진행 중인 대화가 있어 지금은 conf.json 에 에이전트를 추가할 수 없습니다. "
            "모든 대화가 끝난 뒤 다시 시도해 주세요."
        )
    agents, spec, renamed = prepare_save(draft, ctx, graph_id=graph_id)

    keys: List[str] = []
    if agents:
        path = config_path or active_config_path() or DEFAULT_CONFIG_PATH
        try:
            keys = add_agents_to_conf_file([_conf_entry(key, agent) for key, agent in agents], path)
        except (OSError, ValueError) as exc:
            raise SaveRefused(f"conf.json 에 에이전트를 추가하지 못했습니다: {exc}") from exc
        reload_agent_pool()

    graph_error = ""
    if write_graph:
        try:
            save_graph(spec, directory)
        except OSError as exc:
            graph_error = (
                f"에이전트 {len(keys)}명은 등록했지만 그래프를 저장하지 못했습니다: {exc}" if keys
                else f"그래프를 저장하지 못했습니다: {exc}"
            )
    return SavedWorkflow(spec=spec, agent_keys=keys, renamed=renamed, graph_error=graph_error)


def _conf_entry(key: str, agent: AgentDraft) -> Dict[str, Any]:
    """`add_agents_to_conf_file` 의 한 항목. 모델·API 키는 적지 않아 llm 기본 설정을 상속합니다."""
    return {
        "agent_key": key,
        "name": agent.name,
        "role": agent.role,
        "system_prompt": agent.system_prompt,
        "allowed_mcp_servers": list(agent.allowed_mcp_servers),
        "allowed_skills": list(agent.allowed_skills),
        "card_color": agent.card_color or None,
        "icon": agent.icon or None,
    }
