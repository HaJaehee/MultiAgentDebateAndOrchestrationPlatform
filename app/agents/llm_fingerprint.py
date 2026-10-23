"""요청의 지문: 400 같은 거절이 났을 때 무엇이 얼마나 실렸는지 로그 한 줄로 남깁니다.
"""

import logging
from typing import Any, Dict, List, Optional

from app.agents.base import Agent
from app.agents.llm_context import context_budget, estimate_tokens, max_tokens_label, tool_schema_tokens

logger = logging.getLogger(__name__)


# 지문에 이름을 올릴 '가장 큰 메시지' 의 개수.
FINGERPRINT_LARGEST = 3

def _role_runs(messages: List[Dict[str, Any]]) -> str:
    """role 배열을 런렝스로 압축합니다: `system,user,assistant,tool*3,user`.

    role 을 하나씩 나열하면 도구를 많이 쓴 요청에서 줄이 화면을 넘깁니다. 그런데
    진단에 필요한 것은 **모양**입니다 — user 가 연달아 있는지, tool 결과가 앞선
    assistant 없이 떠 있는지, 마지막이 무엇인지.
    """
    runs: List[List[Any]] = []
    for msg in messages:
        role = str(msg.get("role", "?"))
        if runs and runs[-1][0] == role:
            runs[-1][1] += 1
        else:
            runs.append([role, 1])
    return ",".join(r if n == 1 else f"{r}*{n}" for r, n in runs)

def _message_size(msg: Dict[str, Any]) -> int:
    """이 메시지가 요청에서 차지하는 글자 수. 도구 호출 인자까지 셉니다."""
    content = msg.get("content")
    size = len(content) if isinstance(content, str) else (0 if content is None else len(str(content)))
    for tc in msg.get("tool_calls") or []:
        function = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
        if function is None:
            continue
        args = function.get("arguments") if isinstance(function, dict) else getattr(function, "arguments", "")
        size += len(str(args or ""))
    return size

def _message_label(index: int, msg: Dict[str, Any]) -> str:
    """`[3] tool filesystem__read_text_file` 처럼 어느 자리의 무엇인지."""
    role = str(msg.get("role", "?"))
    if role == "assistant" and msg.get("tool_calls"):
        return f"[{index}] assistant(tool_calls*{len(msg['tool_calls'])})"
    name = msg.get("name")
    return f"[{index}] {role}" + (f" {name}" if name else "")

def request_fingerprint(
    agent: Agent,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: str = "auto",
) -> str:
    """실패한 요청이 **어떤 모양이었는지** 한 덩어리로 남깁니다.

    엔드포인트가 이유를 알려 주지 않을 때를 위해 있습니다. 실제로 게이트웨이가
    vLLM 의 400 본문을 버리고 자기 500 으로 감싸 보낸 일이 있었고, 그때 남은 것은
    `client error: 400, message='Bad Request'` 뿐이라 우리가 무엇을 보냈는지조차
    알 수 없었습니다. 상대가 말해 주지 않으면 우리 쪽 기록으로 좁혀야 합니다.

    **내용은 담지 않고 크기만 담습니다.** 도구가 읽어 온 파일이 통째로 로그에
    복사되면 그것대로 문제이고, 진단에 필요한 것은 내용이 아니라 분량입니다.

    한 줄로 답이 나오는 것들: 토큰이 예산을 넘었는가(컨텍스트 초과 400),
    user 가 연달아 있는가(role 교대 400), tool 결과가 고아인가, 도구 목록을
    싣고도 부르지 못하게 했는가, 어느 도구 결과가 요청을 부풀렸는가.
    """
    try:
        ranked = sorted(
            ((_message_size(m), i, m) for i, m in enumerate(messages)),
            key=lambda item: item[0],
            reverse=True,
        )
        largest = "; ".join(
            f"{_message_label(i, m)} {size:,} chars"
            for size, i, m in ranked[:FINGERPRINT_LARGEST] if size
        ) or "(all empty)"

        used = estimate_tokens(agent.model, messages)
        budget = context_budget(agent, tools=tools)
        schema = tool_schema_tokens(agent.model, tools)
        verdict = "  <-- OVER BUDGET" if budget > 0 and used > budget else ""

        lines = [
            f"Request fingerprint for {agent.name} "
            f"(model={agent.model}, api_base={agent.api_base or 'provider default'}):",
            f"  messages={len(messages)} roles={_role_runs(messages)}",
            f"  tokens~{used:,} / budget {budget:,} "
            f"(window {agent.max_context_window:,}, max_tokens {max_tokens_label(agent)}, "
            f"tool schemas≈{schema:,}){verdict}",
            f"  tools={len(tools or [])} "
            f"tool_choice={tool_choice if tools else '(none sent)'}",
            f"  largest: {largest}",
        ]
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - 지문을 못 만든다고 실패를 덮지 않습니다
        return f"Request fingerprint unavailable for {agent.name} ({type(exc).__name__}: {exc})"
