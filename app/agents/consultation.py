"""동료 전문가 자문 (Agent-as-a-Tool) — 발언 도중 다른 전문가 에이전트에게 직접 질의·검증하는 호스트 도구입니다.

기존 MADO의 에이전트 간 소통은 라운드 단위의 공유 전사(Transcript)와 오케스트레이터의 지명/분배를 통해서만
이루어졌습니다. 그러나 아키텍트가 발언 도중 보안 전문가에게 특정 인증 정책의 취약점 여부를 즉시 확인하거나,
코더가 아키텍트에게 인터페이스 계약을 물어본 뒤 코드를 완성해야 하는 경우, 다음 라운드까지 기다리면
불필요한 라운드가 소모됩니다.

본 모듈은 `skills__load_skill` 과 동일한 **호스트 내장 도구(`peers__consult_agent`)** 방식으로
동료 전문가 자문을 제공합니다:

1. **안전한 깊이 상한(`max_depth`) 및 순환 참조 방지(`history`)**:
   - 기본 깊이 상한은 `1`(A → B 자문까지만 허용, B는 다시 다른 에이전트에게 자문 불가)입니다.
   - `max_depth` 를 2 이상으로 높이더라도 이미 호출 사슬(`history`)에 포함된 에이전트는 자문 대상에서
     제외하여 A → B → A 무한 루프를 원천 차단합니다.
2. **외부 패키지 불필요 (폐쇄망 호환)**:
   - 별도의 MCP 서브프로세스나 외부 브로커 없이 `LLMCaller` 의 도구 루프 내부에서 직접 실행됩니다.
3. **도구 예산 및 이벤트 연동**:
   - 피자문 에이전트(B)의 도구 반복 상한은 묻는 쪽(A) 설정의 `peer_consultation.max_tool_iterations`
     (기본 `MAX_CONSULTATION_TOOL_ITERATIONS` = 3회, 0 이면 도구 없이 답함)로 제한하여 단일 자문 호출이
     전체 턴을 지연시키지 않도록 방어합니다.
   - `on_event` 콜백이 주어지면 `peer_consultation_started` / `peer_consultation_finished` 이벤트를
     발행하여 실시간 관측성을 제공합니다.
4. **자문 상대 고르기** (`peer_consultation.targets`):
   - 고르지 않았으면(None) 이번 턴에 참여한 다른 전문가 전원(`ConsultationScope.peers`)입니다.
   - 골랐으면 그 에이전트만 — 이번 토론에 참여하지 않는 전문가도 이 대화의 에이전트
     (`ConsultationScope.pool`, 첫 턴에 고정한 스냅샷)라면 답할 수 있습니다.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from app.config import DEFAULT_PEER_TOOL_ITERATIONS, PEERS_TOOL_PREFIX
from app.events import EventType
from app.mcp.client import clip_tool_output

if TYPE_CHECKING:
    from app.agents.base import Agent

logger = logging.getLogger(__name__)

CONSULT_AGENT_TOOL = f"{PEERS_TOOL_PREFIX}__consult_agent"
_TOOL_TAILS = {"consult_agent"}

DEFAULT_MAX_CONSULTATION_DEPTH = 1
# 자문받는 에이전트의 도구 상한 기본값. 에이전트마다 `peer_consultation.max_tool_iterations` 로 설정합니다.
MAX_CONSULTATION_TOOL_ITERATIONS = DEFAULT_PEER_TOOL_ITERATIONS

# 화면에 표시되는 도구 뱃지와 설명. 이 도구는 별도 MCP 서버가 아니지만 `allowed_mcp_servers` 에
# "peers" 를 추가하여 활성화하므로, 설정 패널의 MCP 서버 목록과 에이전트 카드의 도구 할당 영역에
# 내장 도구 형태로 함께 안내합니다 (conf.json 의 mcp_servers 에 별도 선언하지 않아도 항상 표시됩니다).
PEERS_TOOL_BADGE = "내장"
PEERS_TOOL_SUMMARY = "동료 전문가 자문 — 토론 중 다른 전문가에게 질문"
PEERS_TOOL_TOOLTIP = "\n".join([
    f"{PEERS_TOOL_PREFIX}: 앱에 내장된 동료 전문가 자문 도구입니다 (별도 MCP 서버가 아닙니다).",
    f"해당 기능을 활성화한 전문가는 토론 중 {CONSULT_AGENT_TOOL} 로 다른 전문가에게 질문하고,",
    "전달받은 답변을 자신의 발언에 반영할 수 있습니다.",
    "· 에이전트 카드의 '도구' 항목에서 활성화합니다 (conf.json 의 allowed_mcp_servers 에 \"peers\").",
    "· 동일 창의 '에이전트 선택' 에서 질문할 대상과 자문받는 에이전트의 도구 호출 상한을 설정합니다.",
    f"  대상을 지정하지 않으면 이번 턴에 참여한 다른 전문가 전원이 대상이 되며, 도구 호출은 기본 최대 {MAX_CONSULTATION_TOOL_ITERATIONS}회까지 허용됩니다.",
    f"· 오케스트레이터는 쓰지 않습니다. 자문은 {DEFAULT_MAX_CONSULTATION_DEPTH}단계까지입니다.",
    "· 대화가 시작되면 해당 대화의 자문 설정은 고정됩니다 (첫 발언 시점의 구성 유지).",
    "· 별도 프로세스를 기동하지 않으므로 conf.json 의 mcp_servers 에는 등록하지 않습니다.",
])

CONSULTATION_GUIDANCE_HEAD = "[동료 전문가 자문]"


@dataclass(frozen=True)
class ConsultationScope:
    """현재 턴 또는 발언에서 동료 전문가 자문에 필요한 실행 문맥입니다.

    `peers` 는 이번 턴에 참여한 에이전트, `pool` 은 이 대화의 에이전트 전부(참여자는 이번 턴의 사본,
    나머지는 첫 턴에 고정한 스냅샷)입니다. 자문 상대를 고른 에이전트는 `pool` 에서 찾습니다.
    """

    peers: Tuple["Agent", ...] = ()
    pool: Tuple["Agent", ...] = ()
    llm_caller: Optional[Any] = None
    custom_instructions: str = ""
    session_id: Optional[str] = None
    mcp: Optional[Any] = None
    tool_gate: Optional[Any] = None
    on_event: Optional[Callable[[Dict[str, Any]], Any]] = None
    depth: int = 0
    max_depth: int = DEFAULT_MAX_CONSULTATION_DEPTH
    force_enable: bool = False
    history: Tuple[str, ...] = field(default_factory=tuple)


_CONSULTATION_SCOPE: contextvars.ContextVar[Optional[ConsultationScope]] = contextvars.ContextVar(
    "mado_consultation_scope", default=None
)


def current_consultation_scope() -> Optional[ConsultationScope]:
    """현재 컨텍스트에 활성화된 `ConsultationScope` 를 반환합니다. 없으면 None."""
    return _CONSULTATION_SCOPE.get()


@contextmanager
def consultation_scope(
    peers: Sequence["Agent"],
    *,
    pool: Sequence["Agent"] = (),
    llm_caller: Optional[Any] = None,
    custom_instructions: str = "",
    session_id: Optional[str] = None,
    mcp: Optional[Any] = None,
    tool_gate: Optional[Any] = None,
    on_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    depth: int = 0,
    max_depth: int = DEFAULT_MAX_CONSULTATION_DEPTH,
    force_enable: bool = False,
    history: Sequence[str] = (),
) -> Iterator[ConsultationScope]:
    """발언 또는 턴 동안 동료 전문가 자문 문맥을 활성화합니다. `pool` 이 비면 `peers` 를 씁니다."""
    scope = ConsultationScope(
        peers=tuple(peers),
        pool=tuple(pool) or tuple(peers),
        llm_caller=llm_caller,
        custom_instructions=custom_instructions,
        session_id=session_id,
        mcp=mcp,
        tool_gate=tool_gate,
        on_event=on_event,
        depth=depth,
        max_depth=max_depth,
        force_enable=force_enable,
        history=tuple(history),
    )
    token = _CONSULTATION_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CONSULTATION_SCOPE.reset(token)


def is_consultation_tool(tool_name: str) -> bool:
    """동료 자문 도구(`peers__consult_agent` 또는 `consult_agent`)인지 판별합니다."""
    name = (tool_name or "").strip()
    if "__" in name:
        prefix, tail = name.split("__", 1)
        return prefix == PEERS_TOOL_PREFIX and tail in _TOOL_TAILS
    return name in _TOOL_TAILS


def _is_consultation_enabled_for(
    agent: Any,
    scope: Optional[ConsultationScope],
    explicit_peers: Optional[Sequence["Agent"]],
) -> bool:
    """해당 에이전트가 동료 자문 도구를 사용할 수 있는지 확인합니다."""
    if agent is None or getattr(agent, "key", "") == "orchestrator":
        return False
    if scope is not None and scope.depth >= scope.max_depth:
        return False
    if explicit_peers is not None:
        return True
    if getattr(agent, "allow_peer_consultation", False):
        return True
    allowed_servers = getattr(agent, "allowed_mcp_servers", None) or ()
    if PEERS_TOOL_PREFIX in allowed_servers:
        return True
    if scope is not None and scope.force_enable:
        return True
    return False


def peer_targets_of(agent: Any) -> Optional[List[str]]:
    """묻는 쪽이 고른 자문 상대 (`peer_consultation.targets`). 고르지 않았으면 None."""
    settings = getattr(agent, "peer_consultation", None)
    targets = getattr(settings, "targets", None)
    return list(targets) if targets is not None else None


def peer_tool_limit_of(agent: Any) -> int:
    """묻는 쪽이 정한, 자문받는 에이전트의 도구 호출 상한 (`peer_consultation.max_tool_iterations`)."""
    settings = getattr(agent, "peer_consultation", None)
    limit = getattr(settings, "max_tool_iterations", MAX_CONSULTATION_TOOL_ITERATIONS)
    return max(0, int(limit))


def available_peers_for(
    agent: Any,
    peers: Optional[Sequence["Agent"]] = None,
) -> List["Agent"]:
    """`agent` 가 현재 자문을 구할 수 있는 동료 전문가 목록을 반환합니다.

    `peers` 를 직접 전달하지 않은 경우, 자문 상대를 지정하지 않은 에이전트는 이번 턴 참여자 전체에서 찾고,
    특정 상대를 지정한 에이전트는 대화의 전체 에이전트 풀(`scope.pool`) 중 지정된 대상만 선별합니다.
    어느 쪽이든 자기 자신, 오케스트레이터(`orchestrator`), 비활성화된 에이전트, 이미 호출 사슬(`history`)에
    포함된 에이전트는 대상에서 제외됩니다. 지정한 에이전트가 대화 풀에 없거나 비활성화되어 있으면 목록에서 자동으로 제외됩니다.
    """
    if agent is None:
        return []
    scope = current_consultation_scope()
    if peers is not None:
        source: Sequence["Agent"] = peers
    elif scope is None:
        source = ()
    else:
        targets = peer_targets_of(agent)
        if targets is None:
            source = scope.peers
        else:
            wanted = set(targets)
            source = [p for p in scope.pool if getattr(p, "key", "") in wanted]
    blocked = {getattr(agent, "key", ""), "orchestrator"}
    if scope is not None:
        blocked.update(scope.history)

    result: List["Agent"] = []
    seen: set[str] = set()
    for peer in source:
        key = getattr(peer, "key", "")
        if not key or key in blocked or key in seen:
            continue
        if not getattr(peer, "enabled", True):
            continue
        seen.add(key)
        result.append(peer)
    return result


def consultation_tools_for(
    agent: Any,
    peers: Optional[Sequence["Agent"]] = None,
) -> List[Dict[str, Any]]:
    """이 에이전트에게 제공할 `peers__consult_agent` OpenAI 도구 스키마를 생성합니다."""
    scope = current_consultation_scope()
    if not _is_consultation_enabled_for(agent, scope, peers):
        return []

    peer_list = available_peers_for(agent, peers)
    if not peer_list:
        return []

    roster_lines = [f"- `{p.key}`: {p.name} ({p.role})" for p in peer_list]
    description = (
        "이 대화의 다른 동료 전문가 에이전트에게 구체적인 질문을 보내 자문이나 검증을 받습니다.\n"
        "발언을 확정하기 전에 타 분야 전문가의 의견이나 인터페이스·보안·구현 타당성 확인이 꼭 필요할 때만 호출하세요.\n\n"
        "자문 가능한 동료 전문가:\n" + "\n".join(roster_lines)
    )
    return [
        {
            "type": "function",
            "function": {
                "name": CONSULT_AGENT_TOOL,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_key": {
                            "type": "string",
                            "enum": [p.key for p in peer_list],
                            "description": "자문을 구할 동료 전문가의 키(key)",
                        },
                        "question": {
                            "type": "string",
                            "description": "동료 전문가에게 물어볼 구체적인 질문 또는 검증 요청",
                        },
                        "context": {
                            "type": "string",
                            "description": "질문을 이해하는 데 필요한 배경 맥락이나 코드·설계 발췌 (선택)",
                        },
                    },
                    "required": ["agent_key", "question"],
                },
            },
        }
    ]


def consultation_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """동료 자문 도구를 보유한 에이전트의 시스템 프롬프트에 추가되는 안내문입니다."""
    names = {str((t.get("function") or {}).get("name") or "") for t in tools or ()}
    if CONSULT_AGENT_TOOL not in names:
        return None
    return "\n".join([
        CONSULTATION_GUIDANCE_HEAD,
        f"- 다른 전문가의 도메인 지식이나 교차 검증이 필요하면 `{CONSULT_AGENT_TOOL}` 도구로 질문하여 답변을 받아 반영하십시오.",
        "- 질문(`question`)은 핵심 쟁점 위주로 구체적이고 간결하게 작성하고, 동일한 질문을 반복 호출하지 마십시오.",
    ])


async def _emit_consultation_event(
    scope: ConsultationScope, payload: Dict[str, Any]
) -> None:
    if scope.on_event is None:
        return
    try:
        res = scope.on_event(payload)
        if asyncio.iscoroutine(res):
            await res
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        logger.debug(f"Consultation event callback failed: {exc}", exc_info=True)


async def run_consultation_tool(
    agent: "Agent",
    tool_name: str,
    arguments: Any,
    *,
    llm_caller: Optional[Any] = None,
) -> Tuple[str, str]:
    """`peers__consult_agent` 도구를 실행하고 `(응답 문자열, 상태)` 를 반환합니다."""
    args = arguments if isinstance(arguments, dict) else {}
    target_key = str(args.get("agent_key") or args.get("agent") or "").strip()
    question = str(args.get("question") or args.get("query") or "").strip()
    context = str(args.get("context") or "").strip()

    if not target_key:
        return "자문을 구할 동료 전문가의 `agent_key` 를 지정해 주십시오.", "error"
    if not question:
        return "동료 전문가에게 물어볼 질문(`question`)을 입력해 주십시오.", "error"
    if target_key == getattr(agent, "key", ""):
        return "자기 자신에게는 자문을 요청할 수 없습니다. 다른 동료 전문가를 지정하십시오.", "error"

    scope = current_consultation_scope()
    if scope is None:
        return "현재 실행 문맥에서는 동료 전문가 자문이 활성화되어 있지 않습니다.", "error"

    if scope.depth >= scope.max_depth:
        return (
            f"동료 전문가 자문 깊이 상한(max_depth={scope.max_depth})에 도달하여 "
            f"추가 자문을 호출할 수 없습니다. 현재 확보한 정보로 답변을 작성하십시오.",
            "error",
        )

    if target_key in scope.history:
        chain = " -> ".join([*scope.history, agent.key, target_key])
        return (
            f"순환 자문 호출({chain})은 허용되지 않습니다. 이미 자문을 요청한 에이전트에게 되물을 수 없습니다.",
            "error",
        )

    peers = available_peers_for(agent)
    target = next((p for p in peers if p.key == target_key), None)
    if target is None:
        available = ", ".join(p.key for p in peers) or "없음"
        return (
            f"'{target_key}' 에이전트에게 자문을 구할 수 없습니다. "
            f"현재 자문 가능한 동료 전문가: {available}",
            "error",
        )

    caller = llm_caller or scope.llm_caller
    if caller is None:
        return "LLM 호출기가 연결되어 있지 않아 동료 전문가 자문을 실행할 수 없습니다.", "error"

    # 피자문 에이전트가 과도한 도구 루프를 돌지 않도록 반복 상한을 묶고,
    # 다음 깊이가 상한에 닿으면 자문 권한도 끕니다.
    next_depth = scope.depth + 1
    filtered_servers = [
        s for s in (target.allowed_mcp_servers or [])
        if s != PEERS_TOOL_PREFIX or next_depth < scope.max_depth
    ]
    # 상한은 질문하는 쪽이 설정합니다 (`peer_consultation.max_tool_iterations`). 0 이면 도구 없이 직접 답변하도록
    # 구성합니다 (도구 루프 상한을 0 으로 두면 응답을 생성하기 전에 루프가 종료되기 때문입니다).
    tool_limit = peer_tool_limit_of(agent)
    update: Dict[str, Any] = {
        "max_tool_iterations": max(1, min(target.max_tool_iterations, tool_limit)),
        "allowed_mcp_servers": filtered_servers,
        "allow_peer_consultation": (
            getattr(target, "allow_peer_consultation", False) and next_depth < scope.max_depth
        ),
    }
    if tool_limit == 0:
        update["allowed_mcp_servers"] = []
        update["allowed_skills"] = []
        update["allow_peer_consultation"] = False
        st = getattr(target, "sequential_thinking", None)
        if st is not None and st.enabled and st.mode == "mcp":
            update["sequential_thinking"] = st.model_copy(update={"enabled": False})
    consulted_agent = target.model_copy(update=update)

    prompt_parts = [
        "[동료 전문가 자문 요청]",
        f"- 요청자: {agent.name} ({agent.role})",
        f"- 질문: {question}",
    ]
    if context:
        prompt_parts.append(f"- 배경 맥락:\n{context}")
    prompt_parts.append(
        "\n위 질문에 대해 당신의 전문 분야 관점에서 핵심 판단, 근거, 주의사항을 간결하고 명확하게 답변하십시오."
    )
    prompt_messages = [{"role": "user", "content": "\n".join(prompt_parts)}]

    await _emit_consultation_event(scope, {
        "type": EventType.PEER_CONSULTATION_STARTED,
        "caller_key": agent.key,
        "caller_name": agent.name,
        "target_key": target.key,
        "target_name": target.name,
        "question": question,
        "depth": next_depth,
    })

    try:
        with consultation_scope(
            scope.peers,
            pool=scope.pool,
            llm_caller=caller,
            custom_instructions=scope.custom_instructions,
            session_id=scope.session_id,
            mcp=scope.mcp,
            tool_gate=scope.tool_gate,
            on_event=scope.on_event,
            depth=next_depth,
            max_depth=scope.max_depth,
            force_enable=scope.force_enable and next_depth < scope.max_depth,
            history=(*scope.history, agent.key),
        ):
            answer, _tool_logs = await caller.call_agent(
                consulted_agent,
                prompt_messages,
                scope.custom_instructions,
                session_id=scope.session_id,
                mcp=scope.mcp,
                tool_gate=scope.tool_gate,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Peer consultation from {agent.key} to {target.key} failed: {exc}")
        await _emit_consultation_event(scope, {
            "type": EventType.PEER_CONSULTATION_FINISHED,
            "caller_key": agent.key,
            "target_key": target.key,
            "status": "error",
            "error": str(exc),
            "depth": next_depth,
        })
        return (
            f"동료 전문가 '{target.name}' 자문 호출 중 오류가 발생했습니다 ({type(exc).__name__}: {exc}). "
            f"자문 없이 현재 확보한 정보로 판단을 이어가십시오.",
            "error",
        )

    await _emit_consultation_event(scope, {
        "type": EventType.PEER_CONSULTATION_FINISHED,
        "caller_key": agent.key,
        "target_key": target.key,
        "status": "success",
        "depth": next_depth,
    })

    body = (answer or "").strip() or "(응답 본문이 비어 있습니다.)"
    formatted = f"[{target.name} ({target.role}) 자문 응답]\n\n{body}"
    return clip_tool_output(formatted), "success"
