"""대화형 워크플로 빌더 — 실행 그래프와 필요한 신규 에이전트를 대화를 통해 함께 설계합니다 (ADR-050).

에이전트 빌더(`app/agents/draft.py`)의 대화형 인터랙션과 적응형 그래프(`graph_draft.py`)의 그래프 규격을 통합합니다.
도우미는 오케스트레이터의 LLM 연결 설정을 활용하는 **도구 없는 사본**(`builder_agent`)이며, 응답 끝에 두 종류의
코드 블록을 첨부합니다:

* ```` ```agents ```` — 새로 추가할 에이전트 전체 목록 (배열)
* ```` ```graph ```` — 실행 그래프 전체 명세 (`draft_view`와 동일한 구조)

사용자가 화면에서 조정한 값은 다음 발화 시 `[지금 설계도]` 형태로 도우미에게 전달됩니다. 대화 기록에는 설계도를
**가장 최근 사용자 메시지에만** 첨부합니다 — 그래프와 여러 에이전트 명세는 용량이 수 KB에 달하므로, 턴마다 누적하면
컨텍스트 한도에 빠르게 도달하기 때문입니다.

이 모듈은 UI 레이어와 분리되어 독립적으로 동작합니다. 저장 로직(`save_workflow`)도 여기에 위치하여 브라우저 없이
단위 테스트가 가능합니다. 핵심 원칙은 **실제 저장(쓰기) 전에 모든 검증을 완료하는 것**입니다 — 그래야 기록 도중
유효성 검사 실패로 불완전한 상태가 남아 롤백해야 하는 문제를 방지할 수 있습니다.
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

# 저장하기 전 그래프의 임시 ID. 실제 저장 시점에 최종 결정됩니다 (`save_workflow`).
DRAFT_GRAPH_ID = "builder-draft"
# 도우미 응답에 문제가 있을 때 자체 수정을 요청하는 최대 시도 횟수 (적응형 그래프와 동일한 규칙).
BUILDER_ATTEMPTS = DRAFT_ATTEMPTS
CURRENT_DRAFT_MARKER = "[지금 설계도]"
CORRECTION_MARKER = "[앞선 설계의 문제]"
COMPACT_NOTE = "(설계도 블록 — 다음 사용자 메시지의 [지금 설계도] 참조)"
# 새 에이전트 키 명명 규칙. `app/agents/draft.py`의 저장 규칙과 같습니다.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_FENCE_RE = re.compile(r"```(agents?|graph)\b[^\n]*")
_BLOCK_START_RE = re.compile(r"```(?:agents?|graph)\b")
_DECODER = json.JSONDecoder()


# ---------------------------------------------------------------------------
# 설계도
# ---------------------------------------------------------------------------


class CompositeDraft(BaseModel):
    """대화 진행에 따라 점진적으로 누적·수정되는 복합 설계도입니다. 화면과 도우미 에이전트가 동일한 상태를 공유합니다."""

    new_agents: List[AgentDraft] = Field(default_factory=list)
    # 사용자가 에이전트 랙에서 제거한 새 에이전트의 키 목록입니다. 도우미가 다시 생성하지 않도록 프롬프트로 전달합니다.
    removed_agents: List[str] = Field(default_factory=list)
    # 도우미가 전달한 원형 그대로의 그래프 데이터입니다. 파싱할 수 없는 응답이 오더라도 마지막으로 정상 파싱된 값을 보존합니다.
    graph_data: Optional[Dict[str, Any]] = None
    # `graph_data`를 검증하고 파싱한 결과입니다. ID는 임시 값(`DRAFT_GRAPH_ID`)이며 최종 저장 시 확정됩니다.
    graph: Optional[GraphSpec] = None
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    worst_case_calls: int = 0
    # 편집기에서 열었을 때 수정 대상이 되는 기본 그래프의 ID입니다. 대화 화면에서 새로 생성하는 경우 빈 문자열입니다.
    base_graph_id: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.new_agents or self.removed_agents or self.graph is not None)

    def agent(self, key: str) -> Optional[AgentDraft]:
        return next((a for a in self.new_agents if a.key == key), None)


@dataclass
class BuilderContext:
    """설계 및 유효성 검사에 사용되는 현재 시스템 구성입니다. 대화 세션 시작 시 한 번 생성하고, 저장 시점에 최신 상태로 다시 조회합니다."""

    # 그래프에 배치할 수 있는 기존 전문가 에이전트 목록 (활성화 상태이며 오케스트레이터 제외).
    candidates: List[Agent]
    # 새 에이전트가 사용할 수 없는 키 집합 — conf.json의 모든 에이전트(비활성화 포함) 및 오케스트레이터.
    taken: Set[str]
    servers: List[Option]
    skills: List[Option]
    # 최대 방문 횟수 상한이 지정되지 않은 노드를 몇 회로 산정할지 결정하는 기준값 (대화 세션의 최대 라운드 또는 편집기의 미리보기 기준값).
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
    """현재 에이전트 풀과 conf.json 구성을 바탕으로 `BuilderContext`를 생성합니다."""
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
    """편집기의 현재 그래프 상태에서 시작하는 복합 설계도를 생성합니다. 기존 노드 좌표를 그대로 계승합니다 (`apply_reply`에서 유지)."""
    graph = spec.model_copy(update={"id": DRAFT_GRAPH_ID}, deep=True)
    return CompositeDraft(graph=graph, graph_data=draft_view(graph), base_graph_id=spec.id)


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------

_PROMPT = """당신은 '워크플로 설계 도우미'입니다. 대화 상대는 개발자가 아닌 일반 직장인입니다. 사용자가 수행하려는 업무의 흐름을 파악하여, 여러 AI 전문가가 순차적으로 또는 동시에 협업하는 **실행 그래프**를 설계합니다. 기존 전문가 중 적합한 역할이 없다면 새로운 전문가(에이전트)도 함께 설계합니다.

## 대화 방식
- 이해하기 쉬운 자연스러운 한국어로 간결하게 대화합니다. 전문 용어를 쓸 때는 바로 뒤 괄호 안에 알기 쉬운 설명을 덧붙입니다.
- 질문은 한 번에 최대 두 개까지만 합니다. 이미 확인한 내용은 불필요하게 다시 묻지 않습니다.
- 2~3회의 대화 왕복 이내에 초안을 신속히 완성합니다. 정보가 부족한 경우 합리적인 기본값으로 초안 그래프를 먼저 그린 뒤 수정할 부분을 확인합니다.
- 설계도를 수정할 때마다 무엇을 왜 변경했는지 1~2문장으로 명확히 설명합니다. 새 전문가를 생성했다면 기존 전문가로는 왜 부족했는지, 어떤 도구를 왜 부여했는지 함께 안내합니다.

## 사용 가능한 기존 전문가 (키: 이름 (역할) · 도구)
{roster}

## 이미 사용 중인 키 — 새 전문가는 이 키를 쓸 수 없습니다
{taken}

## 그래프 규격
{graph_format}

## 설계 규칙
- 작업의 성격에 맞추어 설계합니다. 단순한 작업은 전문가 1~2명만으로 짧게 구성해도 충분합니다. 상호 결과가 독립적인 작업만 병렬로 분기하고, 선행 작업의 결과물을 후속 작업이 활용해야 한다면 직렬로 연결합니다.
- 분기된 갈래는 반드시 merge 노드로 모은 뒤 다음 단계나 최종 합성(end) 노드로 보냅니다.
- 되돌아가는 선(루프)은 반드시 gate 노드를 거치도록 합니다. 루프는 피드백을 반영해 수정할 작업자가 있을 때만 구성합니다 (예: 검토 → 판정 → 수정 → 검토). 동일한 검토자에게 무의미하게 되돌리기만 하는 루프는 만들지 않습니다.
- 루프 내 노드에는 "max_visits" (2~3)를 지정하여 실행 비용 상한을 설정해야 합니다.
- 시작(start)과 최종 합성(end) 노드를 제외하고 노드는 최대 {max_nodes}개까지, 최악의 경우 한 턴당 호출 횟수는 최대 {max_calls}회 이내여야 합니다.
- agent 노드에는 해당 차례에 수행할 작업을 "instruction"에 구체적으로 작성합니다.
- 위 기존 전문가 목록에 적합한 전문가가 있다면 해당 키를 우선 사용합니다. 적합한 전문가가 없을 때만 새로운 전문가를 정의합니다. 오케스트레이터(orchestrator)는 agent 노드가 될 수 없습니다 — 취합(merge)과 판정(gate)이 오케스트레이터의 고유 역할입니다.
- 기존 전문가의 기본 설정(도구, 업무 지시서 등)을 변경해 달라는 요청에는 설계도를 수정하지 말고, 로스터(에이전트 목록) 화면에서 직접 수정할 수 있음을 안내합니다.

## 새 전문가의 네 가지 필수 요소
- 페르소나: name(이름), role(한 줄 역할)
- 업무 지시서: system_prompt — "당신은 ..."으로 시작하는 2인칭 문장. 목표, 구체적 작업 절차(번호 목록), 결과물 양식, 도구 활용 기준, 주의 및 금지 사항 작성.
- 도구: allowed_mcp_servers — 아래 도구 ID 목록에서만 선택. 필요한 도구가 없으면 빈 목록으로 설정.
- 스킬: allowed_skills — 아래 스킬 ID 목록에서만 선택.

## 선택 가능한 도구 (ID만 사용)
{tools}

## 선택 가능한 스킬 (ID만 사용)
{skills}

## 설계도 블록 — 설계도를 변경할 때 응답의 맨 끝에 첨부합니다
변경된 블록만 첨부하세요. 블록을 첨부하지 않으면 해당 부분은 기존 설계를 유지한다는 의미입니다. 블록 외부에는 JSON 코드를 작성하지 마세요.

새 전문가 **전체** 목록 (새로 만든 전문가가 없으면 빈 배열 []):
```agents
[{"key": "영문 소문자와 밑줄로 된 식별자", "name": "이름", "role": "한 줄 역할", "system_prompt": "업무 지시서", "allowed_mcp_servers": ["도구 id"], "allowed_skills": ["스킬 id"], "card_color": "#rrggbb", "icon": "아이콘 이름"}]
```
- card_color는 다음 중 하나를 선택합니다: {colors}
- icon은 다음 중 하나를 선택합니다: {icons}

실행 그래프 **전체**:
```graph
{"name": "그래프 이름", "nodes": [{"id": "review", "type": "agent", "agent": "에이전트키", "label": "검토", "instruction": "..."}], "edges": [{"from": "start", "to": "review"}, {"from": "review", "to": "end"}]}
```

사용자 메시지 끝에 [지금 설계도]가 첨부되어 있다면 사용자가 화면에서 직접 조정한 최신 상태입니다. 해당 값을 최우선으로 반영하며, removed_agents에 명시된 키는 사용자가 다시 요청하지 않는 한 새로 만들지 마세요."""


def _option_lines(options: Sequence[Option], empty: str) -> str:
    lines = [f"- `{o.id}`: {o.label}" + (f" — {o.description}" if o.description else "") for o in options]
    return "\n".join(lines) or empty


def workflow_builder_prompt(ctx: BuilderContext) -> str:
    roster = format_roster(ctx.candidates, with_keys=True) or "- (사용 가능한 전문가가 없습니다. 필요한 전문가를 새로 정의하세요)"
    return (
        _PROMPT.replace("{roster}", roster)
        .replace("{taken}", ", ".join(sorted(ctx.taken)))
        .replace("{graph_format}", GRAPH_FORMAT_GUIDE)
        .replace("{max_nodes}", str(MAX_DRAFT_NODES))
        .replace("{max_calls}", str(MAX_DRAFT_CALLS))
        .replace("{tools}", _option_lines(ctx.servers, "- (현재 사용 가능한 도구가 없습니다. 빈 목록으로 두세요)"))
        .replace("{skills}", _option_lines(ctx.skills, "- (현재 등록된 스킬이 없습니다. 빈 목록으로 두세요)"))
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
    """도우미에게 질문을 전달하고 전체 응답을 반환합니다. 연결할 수 없으면 `LLMUnavailableError`가 발생합니다."""
    agent = builder_agent(pool or get_agent_pool(), workflow_builder_prompt(ctx))
    content, _ = await (llm or LLMCaller()).call_agent(agent, messages, on_chunk=on_chunk)
    return content


def with_composite_draft(text: str, draft: CompositeDraft) -> str:
    """사용자 메시지에 현재 설계도를 첨부합니다. 화면에서 직접 수정한 값을 도우미가 덮어쓰지 않도록 합니다."""
    if draft.is_empty:
        return text
    payload = {
        "agents": [a.model_dump() for a in draft.new_agents],
        "graph": draft_view(draft.graph) if draft.graph is not None else draft.graph_data,
        "removed_agents": list(draft.removed_agents),
    }
    return f"{text}\n\n{CURRENT_DRAFT_MARKER}\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


def request_messages(history: Sequence[Dict[str, str]], draft: CompositeDraft) -> List[Dict[str, str]]:
    """도우미에게 전달할 메시지 이력을 구성합니다. 이전 대화 기록은 유지하고, 마지막 사용자 메시지에만 현재 설계도를 첨부합니다."""
    messages = [dict(m) for m in history]
    for message in reversed(messages):
        if message.get("role") == "user":
            message["content"] = with_composite_draft(message.get("content", ""), draft)
            break
    return messages


def correction_message(problems: Sequence[str]) -> str:
    """이전 응답에서 발견된 문제를 전달하고 설계도 블록의 수정을 요청하는 메시지입니다. 대화 기록에는 남기지 않습니다."""
    return (
        f"{CORRECTION_MARKER} 방금 보낸 설계도에 아래 문제가 있습니다. 고쳐서 설계도 블록을 다시 보내 주세요. "
        "설명은 한두 문장이면 됩니다.\n" + "\n".join(f"- {p}" for p in problems)
    )


# ---------------------------------------------------------------------------
# 답 읽기
# ---------------------------------------------------------------------------


@dataclass
class CompositeReply:
    """도우미 응답을 분해한 결과입니다. `None`은 "이번 응답에 해당 블록이 없음"을 나타내며 현재 값을 그대로 유지합니다."""

    text: str
    agents: Optional[List[Dict[str, Any]]] = None
    graph: Optional[Dict[str, Any]] = None
    # 블록은 존재했으나 정상적으로 읽지 못한 오류 항목입니다. 도우미에게 자가 교정을 요청할 때 활용됩니다.
    broken: List[str] = field(default_factory=list)

    @property
    def has_blocks(self) -> bool:
        return self.agents is not None or self.graph is not None or bool(self.broken)


def _decode_block(text: str, start: int) -> Tuple[Any, int]:
    """`start` 위치 이후 첫 번째 JSON 값과 그 끝 위치를 반환합니다. 읽지 못하면 ValueError가 발생합니다.

    정규식으로 마감 펜스(```)까지 자르지 않습니다 — 업무 지시서 내에 ```가 포함되어 있으면 그곳에서 잘못 잘리기 때문입니다.
    """
    index = start
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text) or text[index] not in "[{":
        raise ValueError("JSON 이 없습니다")
    return _DECODER.raw_decode(text, index)


def split_composite_reply(content: str) -> CompositeReply:
    """도우미 응답을 (사용자에게 보여 줄 안내 텍스트, 새 에이전트 목록, 그래프 명세)로 분리합니다. 동일한 태그가 여럿이면 마지막 블록을 채택합니다.

    `agent` 태그와 단일 객체 형식도 수용하며, 유형을 특정할 수 없는 `json` 태그는 무시합니다.
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
    """스트리밍 중인 응답에서 설계도 코드 블록이 시작되는 지점 이후를 숨깁니다."""
    match = _BLOCK_START_RE.search(text or "")
    return (text[:match.start()] if match else text or "").strip()


def compact_reply(content: str) -> str:
    """대화 기록에 보존할 도우미 응답입니다. 코드 블록은 한 줄 안내로 축약하며, 최신 설계도는 다음 사용자 메시지의 `[지금 설계도]`에 첨부됩니다."""
    reply = split_composite_reply(content)
    if not reply.has_blocks:
        return reply.text
    return f"{reply.text}\n\n{COMPACT_NOTE}".strip()


# ---------------------------------------------------------------------------
# 합치기·검사
# ---------------------------------------------------------------------------


def _carry_positions(new: GraphSpec, old: Optional[GraphSpec]) -> GraphSpec:
    """노드 구성이 동일하게 유지되면 편집기 상의 기존 좌표를 계승합니다. 새로 추가된 노드가 하나라도 있으면 전체 노드를 자동 배치합니다.

    기존 좌표와 자동 배치를 혼용하면 새로 자동 배치된 노드가 사용자가 수동 배치한 노드와 겹칠 수 있습니다.
    """
    if old is None:
        return new
    places = {n.id: n.pos for n in old.nodes if n.pos is not None}
    if not new.nodes or any(n.id not in places for n in new.nodes):
        return new
    nodes = [n.model_copy(update={"pos": places[n.id]}) for n in new.nodes]
    return new.model_copy(update={"nodes": nodes})


def check_composite(draft: CompositeDraft, ctx: BuilderContext) -> CompositeDraft:
    """설계도를 검사하여 발견된 오류·경고 목록과 최악 시나리오의 호출 횟수를 계산한 사본을 반환합니다.

    그래프는 적응형 그래프와 동일한 `draft_report`(검증 및 노드 12개·호출 40회 상한)로 검증합니다. 여기에 새
    에이전트 검사를 추가합니다 — 특히 **키 충돌 방지**. 이미 존재하는 키와 중복된 채로 저장하면 `agent_key_for`가 키를
    임의로 변경하고, 노드는 기존 에이전트를 가리킨 채 검증을 통과하여 의도치 않게 다른 에이전트가 발언하는 결함이 발생할 수 있습니다.
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
    """도우미 응답을 현재 설계도에 병합합니다. (갱신된 설계도, 도우미에게 수정을 요청할 문제 목록) 튜플을 반환합니다.

    문제 목록에는 **이번 응답으로 인해 발생한 문제만** 집계합니다. 코드 블록이 없는 질문 응답이나 사용자가 화면에서
    직접 편집해 발생한 오류에 대해서는 재작성을 요청하지 않습니다 — 사용자의 직접 수정을 덮어쓰는 부작용을 방지하기 위함입니다.
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
        # 사용자가 제외했던 키를 도우미가 다시 포함했다면, 사용자가 재요청한 것으로 간주합니다.
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
    """에이전트 랙에서 새 에이전트를 제거합니다. 해당 키를 참조하던 노드는 검증 오류로 표시됩니다."""
    updated = draft.model_copy(update={
        "new_agents": [a for a in draft.new_agents if a.key != key],
        "removed_agents": list(dict.fromkeys([*draft.removed_agents, key])),
    }, deep=True)
    return check_composite(updated, ctx)


def replace_new_agent(draft: CompositeDraft, key: str, agent: AgentDraft, ctx: BuilderContext) -> CompositeDraft:
    """에이전트 랙에서 수정한 새 에이전트 정보를 반영합니다. 키를 변경했다면 해당 키를 참조하던 그래프 노드도 함께 갱신합니다."""
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
    """agent 노드가 참조하는 에이전트 키를 변경한 새 GraphSpec 사본을 반환합니다."""
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
    """저장 결과 객체입니다. `graph_error`가 설정되어 있다면 에이전트는 등록되었으나 그래프 파일 저장에 실패한 경우입니다."""

    spec: GraphSpec
    agent_keys: List[str]
    renamed: Dict[str, str]
    graph_error: str = ""


def new_graph_id(directory: Optional[Path] = None, now: Optional[datetime] = None) -> str:
    """새 그래프 파일의 ID를 생성합니다. 그래프 이름 기반으로 만들지 않습니다 — `free_graph_id`는 한글을 제거하여 모두 `graph`로 수렴되기 때문입니다."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    return free_graph_id(f"ai-{stamp}", directory)


def prepare_save(
    draft: CompositeDraft,
    ctx: BuilderContext,
    *,
    graph_id: str,
) -> Tuple[List[Tuple[str, AgentDraft]], GraphSpec, Dict[str, str]]:
    """실제 파일 기록 전 최종 검증을 수행합니다. (최종 확정된 키와 에이전트 목록, 저장할 GraphSpec, 변경된 키 매핑)을 반환하며, 검증 실패 시 `SaveRefused`가 발생합니다.

    `ctx`는 저장 시점에 최신 상태로 새로 조회한 컨텍스트여야 합니다. 대화를 나누는 사이에 다른 브라우저 창에서 동일한 키의 에이전트가
    생성되었을 경우 `agent_key_for`를 통해 새 키를 발급받고, 해당 키를 참조하던 노드 역시 함께 갱신합니다.
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
    """새 에이전트를 conf.json에 한 번에 등록하고 그래프 명세를 저장합니다.

    1. 등록할 새 에이전트가 존재할 때, 현재 진행 중인 대화 세션이 하나라도 있다면 저장을 거부합니다 (`save_owner_agent`와 동일한 규칙).
       에이전트 풀은 프로세스 전체에서 단일 인스턴스로 관리되므로, 실행 중인 토론 도중에 발언자 구성이 변경되는 위험을 방지합니다.
    2. 파일 쓰기 전에 모든 검증을 완료합니다 (`prepare_save`).
    3. conf.json에 새 에이전트를 일괄 기록하고 에이전트 풀을 갱신합니다.
    4. 그래프 파일을 저장합니다 (`write_graph=False`인 경우 파일 저장을 건너뛰고 객체만 반환합니다 — 편집기 화면에서 캔버스에 먼저 적용한 뒤
       편집기의 자체 [저장] 버튼을 통해 파일로 기록하도록 지원합니다). 그래프 저장이 실패하더라도 conf.json 변경은 롤백하지 않습니다.
       이미 등록된 에이전트는 남아 있어도 시스템에 해가 없으며, 설정을 통째로 되돌리면 그 사이 다른 창에서 수정한 설정까지 유실될 위험이 있기 때문입니다.
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
    """`add_agents_to_conf_file`에 전달할 개별 항목을 구성합니다. 모델 및 API 키는 지정하지 않아 llm 기본 설정을 상속합니다."""
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
