"""에이전트가 사람에게 묻기 — 발언 도중 모호한 점을 사람에게 묻는 호스트 도구 `ask__ask_user` (v1.5).

전문가는 요청이 모호하면 추측으로 채우고 지나갔습니다. 그 추측이 틀리면 남은 라운드 전체가 엉뚱한 방향으로
갑니다. 계획 승인(ADR-028)은 토론 **전에** 묻는 자리이고, 이 도구는 토론 **도중** 막혔을 때 묻는 자리입니다.

* 켜는 곳은 대화입니다 (로스터의 `에이전트 질문 받기`, `sessions.ask_user`). 그리고 사람이 지켜보는 턴에서만
  줍니다 — API·MCP 로 시작한 턴이나 배치 실행에는 물어볼 사람이 없습니다 (`control.asks_human`).
* 오케스트레이터와, 동료 자문으로 불려 답하는 에이전트에게는 주지 않습니다.
* 한 턴에 `MAX_QUESTIONS_PER_TURN` 번까지. 답을 기다리는 시간은 `QUESTION_WAIT_SECONDS` 이고, 그 안에 답이
  없거나 사람이 건너뛰면 "답 없음 — 가정을 밝히고 진행" 을 돌려줍니다. 발언은 끊기지 않습니다.
* 묻고 기다리는 일은 엔진이 합니다 (`SpeechRunner` 가 `asking` 으로 묻는 함수를 걸어 둡니다). 이 모듈은 도구의
  모양과 실행만 압니다 — 에이전트 층은 엔진을 모릅니다.
* 사람의 답은 도구 결과로 그 에이전트에게 가고, 같은 문장이 개입 메모로 대화에도 남아 뒤의 발언자들도 봅니다.
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
                "토론을 지켜보는 사람(요청한 유저)에게 짧은 질문을 하나 하고 답을 받습니다. 요청이 모호해서 "
                "어느 쪽으로 가느냐에 따라 결론이 크게 달라질 때만 쓰세요 — 스스로 정할 수 있는 것은 묻지 말고, "
                "가정을 밝히고 진행하세요. 답이 없으면 그렇다고 돌아오니, 그때는 가정을 밝히고 진행하세요. "
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
        f"- 요청이 모호해 결론이 갈리는 지점에서만 `{ASK_USER_TOOL}` 로 유저에게 짧게 물으세요. 남발하지 마세요.",
        "- 답을 받으면 그 답을 따르고, 답이 없으면 어떤 가정으로 진행하는지 발언에 밝히세요.",
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
        return "물어볼 질문(`question`)을 적어 주십시오.", "error"
    scope = _ASK_SCOPE.get()
    if scope is None:
        return "지금은 유저에게 물을 수 없습니다. 가정을 밝히고 진행하세요.", "error"
    if scope.remaining[0] <= 0:
        return "이번 턴에 물을 수 있는 횟수를 다 썼습니다. 가정을 밝히고 진행하세요.", "error"
    scope.remaining[0] -= 1
    answer = await scope.asker(agent, question, options)
    if not answer:
        return f"{NO_ANSWER} 유저가 답하지 않았습니다. 어떤 가정으로 진행하는지 밝히고 계속하세요.", "success"
    return f"유저의 답: {answer}", "success"
