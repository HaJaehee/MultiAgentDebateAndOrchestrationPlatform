"""에이전트의 사용자 질문 도구 — 발언 도중 모호한 점을 사용자에게 확인하는 호스트 도구 `ask__ask_user` (v1.5).

전문가 에이전트는 프롬프트 요청이 모호할 경우 임의의 가정으로 논의를 진행할 위험이 있습니다.
그 가정이 잘못되면 남은 토론 라운드 전체가 의도와 다른 방향으로 전개될 수 있습니다.
계획 승인(ADR-028)이 토론 시작 **전에** 작업 방향을 확정하는 단계라면, 본 도구는 토론 진행 **도중**
판단이 갈리는 지점에서 사용자의 의도를 직접 확인하기 위한 장치입니다.

* 대화 세션별로 설정합니다 (로스터 관리 패널의 `에이전트 질문 받기`, `sessions.ask_user`).
  사용자가 직접 대화창을 모니터링하는 대화형 턴(`control.asks_human`)에서만 제공되며,
  API나 MCP를 통한 비대면(unattended) 턴이나 배치 실행에는 제공되지 않습니다.
* 마스터 오케스트레이터 및 동료 자문(Consultation) 요청에 응답 중인 에이전트에게는 제공되지 않습니다.
* 턴당 최대 `MAX_QUESTIONS_PER_TURN`회까지 질문할 수 있습니다. 사용자 답변 대기 시간은
  `QUESTION_WAIT_SECONDS`이며, 제한 시간 내 응답이 없거나 사용자가 '건너뛰기'를 선택한 경우
  "답 없음 — 가정을 밝히고 진행"을 반환하여 발언이 중단되지 않도록 합니다.
* 실제 질문 전송 및 대기는 엔진 층(`SpeechRunner`가 `asking` 컨텍스트로 제공하는 콜백)이 담당하며,
  본 모듈은 도구의 인터페이스 명세 및 유효성 검증, 실행 로직만을 담당합니다.
* 사용자의 답변은 도구 실행 결과로서 해당 에이전트에게 전달되며, 동시에 시스템 개입 메모로 대화 기록에 저장되어
  이후 발언하는 모든 전문가 에이전트의 맥락에도 공유됩니다.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

from app.agents.consultation import current_consultation_scope
from app.config import ASK_TOOL_PREFIX, ORCHESTRATOR_KEY

ASK_USER_TOOL = f"{ASK_TOOL_PREFIX}__ask_user"
MAX_QUESTIONS_PER_TURN = 3
QUESTION_WAIT_SECONDS = 300.0
MAX_OPTIONS = 6
ASK_GUIDANCE_HEAD = "[사람에게 묻기]"
NO_ANSWER = "(답 없음)"

# (에이전트, 질문, 선택지) → 사람의 답. 답이 없으면 None.
Asker = Callable[[Any, str, List[str]], Awaitable[Optional[str]]]


@dataclass
class AskScope:
    asker: Asker
    # 이번 턴에 남은 질문 수. 발언마다 새 범위가 서도 같은 목록을 나눠 써서 턴 전체에서 셉니다.
    remaining: List[int] = field(default_factory=lambda: [MAX_QUESTIONS_PER_TURN])


_ASK_SCOPE: "contextvars.ContextVar[Optional[AskScope]]" = contextvars.ContextVar("mado_ask_scope", default=None)


@contextmanager
def asking(asker: Asker, remaining: List[int]) -> Iterator[AskScope]:
    """이 블록 안의 발언에 `ask__ask_user` 를 줍니다. `remaining` 은 턴 전체가 나눠 쓰는 남은 질문 수입니다."""
    scope = AskScope(asker=asker, remaining=remaining)
    token = _ASK_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ASK_SCOPE.reset(token)


def is_ask_tool(tool_name: str) -> bool:
    return (tool_name or "").strip() in (ASK_USER_TOOL, "ask_user")


def ask_tools_for(agent: Any) -> List[Dict[str, Any]]:
    """이 발언에 줄 `ask__ask_user` 도구. 범위가 없거나, 오케스트레이터거나, 자문 답변 중이거나, 다 썼으면 없음."""
    scope = _ASK_SCOPE.get()
    if scope is None or scope.remaining[0] <= 0 or getattr(agent, "key", "") == ORCHESTRATOR_KEY:
        return []
    # 동료 자문으로 불려 답하는 중이면 묻지 않습니다 — 묻는 쪽은 자문을 청한 전문가입니다.
    consulting = current_consultation_scope()
    if consulting is not None and consulting.depth > 0:
        return []
    return [{
        "type": "function",
        "function": {
            "name": ASK_USER_TOOL,
            "description": (
                "토론을 요청한 사용자에게 구체적인 확인 질문을 전송하고 답변을 수신합니다. 요구사항이 모호하여 "
                "진행 방향에 따라 결론이 크게 달라지는 핵심 분기점에서만 사용하세요. 자체적으로 판단 가능한 사안은 "
                "묻지 말고 가정을 명시하여 진행하세요. 무응답 시에도 가정을 밝히고 계속 진행해야 합니다. "
                f"이번 턴에 남은 질문: {scope.remaining[0]}번."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "한두 문장의 구체적인 질문"},
                    "options": {
                        "type": "array", "items": {"type": "string"}, "maxItems": MAX_OPTIONS,
                        "description": "고를 수 있는 답 (선택). 사람은 이 중 하나를 누르거나 직접 적습니다",
                    },
                },
                "required": ["question"],
            },
        },
    }]


def ask_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    names = {str((t.get("function") or {}).get("name") or "") for t in tools or ()}
    if ASK_USER_TOOL not in names:
        return None
    return "\n".join([
        ASK_GUIDANCE_HEAD,
        f"- 요청이 모호해 결론이 갈리는 지점에서만 `{ASK_USER_TOOL}` 로 사용자에게 확인하세요. 불필요한 질문은 지양하세요.",
        "- 답변을 받으면 그 내용을 따르고, 답변이 없으면 어떤 가정으로 진행하는지 발언에 명시하세요.",
    ])


def _clean(arguments: Any) -> Tuple[str, List[str]]:
    args = arguments if isinstance(arguments, dict) else {}
    question = str(args.get("question") or "").strip()
    raw = args.get("options") if isinstance(args.get("options"), list) else []
    options = [o for o in dict.fromkeys(str(x).strip() for x in raw) if o][:MAX_OPTIONS]
    return question, options


async def run_ask_tool(agent: Any, arguments: Any) -> Tuple[str, str]:
    """`ask__ask_user` 를 실행합니다: 사람에게 묻고 `(도구 결과, 상태)` 를 돌려줍니다."""
    question, options = _clean(arguments)
    if not question:
        return "확인할 질문(`question`)을 입력해 주세요.", "error"
    scope = _ASK_SCOPE.get()
    if scope is None:
        return "현재 상태에서는 사용자에게 질문할 수 없습니다. 자체 가정을 명시하고 진행하세요.", "error"
    if scope.remaining[0] <= 0:
        return "이번 턴에 물을 수 있는 횟수를 모두 소진했습니다. 자체 가정을 명시하고 진행하세요.", "error"
    scope.remaining[0] -= 1
    answer = await scope.asker(agent, question, options)
    if not answer:
        return f"{NO_ANSWER} 사용자의 응답이 없습니다. 자체 가정을 명시하고 계속 진행하세요.", "success"
    return f"유저의 답: {answer}", "success"
