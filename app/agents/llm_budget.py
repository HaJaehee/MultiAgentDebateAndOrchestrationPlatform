"""한도에 닿았을 때 모델에게 주는 안내: 도구 호출 상한, 응답 한도(max_tokens) 잘림, 사고 과정이
응답 예산을 먹은 경우의 보고.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.agents.llm_tools import edit_tool, file_tools, is_file_writing_call

logger = logging.getLogger(__name__)


# 도구를 부를 수 있는 횟수가 이 값들에 닿을 때마다 에이전트에게 알립니다.
# 상한보다 큰 값은 그냥 지나갑니다 (상한 30 인 에이전트에게 "100회 남음" 을
# 알릴 일은 없습니다).
TOOL_BUDGET_MILESTONES = (100, 80, 60, 50, 40, 30, 20, 10)

# 여기서부터는 매 호출마다 알립니다. 5 → 4 → 3 → 2 → 1 → 불가.
#
# 사다리를 촘촘하게 만드는 이유는, 모델이 "아직 여유가 있다" 고 믿은 채로
# 탐색을 이어가다 마지막 판에서 갑자기 끊기는 것을 막기 위해서입니다. 남은
# 횟수를 알면 결론을 쓸 자리를 스스로 남겨 둡니다.
TOOL_BUDGET_FINAL_COUNTDOWN = 5

def tool_budget_notice(*, remaining: int, limit: int, used: int, tool_calls: int) -> Optional[str]:
    """이번 판에 에이전트에게 붙일 예산 고지. 알릴 시점이 아니면 None.

    첫 판에는 총량을, 그 뒤로는 사다리(`TOOL_BUDGET_MILESTONES`)에 닿을 때와
    마지막 5회부터 매번 남은 횟수를 알립니다.
    """
    if remaining <= 0:
        return None

    if used == 0:
        return (
            f"[도구 호출 예산] 이번 발언에서 도구를 부를 수 있는 횟수는 **총 {limit}회**입니다. "
            f"한 번에 여러 도구를 함께 불러도 그 묶음이 1회입니다. "
            f"예산을 다 쓰면 도구 없이 결론만 써야 하므로, 꼭 필요한 호출부터 하고 "
            f"결론을 쓸 여유를 남겨 두세요."
        )

    announce = remaining <= TOOL_BUDGET_FINAL_COUNTDOWN or (
        remaining in TOOL_BUDGET_MILESTONES and remaining < limit
    )
    if not announce:
        return None

    head = (
        f"[도구 호출 예산] 남은 호출 **{remaining}회** "
        f"(총 {limit}회 중 {used}회 사용, 도구 {tool_calls}건 실행)."
    )
    if remaining == 1:
        return (
            f"{head} **마지막 기회입니다.** 이번 한 번을 쓰고 나면 도구를 전혀 쓸 수 없습니다. "
            f"정말 결론에 필요한 것이 아니면 지금 바로 답을 작성하세요."
        )
    if remaining <= TOOL_BUDGET_FINAL_COUNTDOWN:
        return (
            f"{head} 마무리를 준비하세요 — 남은 호출은 결론에 반드시 필요한 것에만 쓰고, "
            f"확인하지 못한 것은 확인하지 못했다고 적으세요."
        )
    return f"{head} 남은 횟수 안에서 결론까지 낼 수 있도록 계획을 조정하세요."

BUDGET_EXHAUSTED_INSTRUCTION = (
    "[도구 호출 예산 소진] 도구 호출 상한 {limit}회를 모두 썼습니다. "
    "지금부터 도구는 사용할 수 없습니다.\n"
    "새로 도구를 부르려 하지 말고, 지금까지 실행한 {tool_calls}건의 관측만으로 "
    "이번 발언의 결론을 **지금 작성하세요**.\n"
    "확인하지 못한 것은 확인하지 못했다고 적고, 무엇이 남았는지 다음 발언자에게 넘기세요. "
    "없는 사실을 지어내지 마세요."
)

BUDGET_EXTENDED_INSTRUCTION = (
    "[도구 호출 예산 확장] 유저가 상한을 {extra}회 늘려 총 {limit}회가 되었습니다 "
    "(남은 호출 {remaining}회). 늘어난 몫은 결론을 내는 데 꼭 필요한 확인에만 쓰고, "
    "같은 탐색을 반복하지 마세요."
)

# 응답은 한도에 닿았지만 도구 호출의 인자는 **읽을 수 있었던** 경우.
#
# 예전에는 이것도 위의 고지문으로 알렸습니다. 그런데 도구는 실제로 실행되어 결과가
# 바로 위에 있는데 "실행되지 않았습니다, 다시 보내지 마세요" 라고 하면 거짓입니다.
# 게다가 `read_file` 한 번에 "파일을 여러 개로 쪼개 쓰라" 는 조언까지 붙어, 모델은
# 받은 파일 내용과 정반대인 말을 동시에 받았습니다.
#
# 그렇다고 아무 말도 안 할 수는 없습니다. 한도에 닿은 자리는 **마지막 호출의 인자
# 끝**이고, 일부 프로바이더는 잘린 JSON 을 닫아 "읽을 수 있게" 고쳐 줍니다. 그러면
# 인자는 파싱되지만 끝이 잘려 있습니다 (파일 내용이 중간에서 끝나는 식으로). 그래서
# "실행됐다" 와 "마지막 호출의 끝은 확인하라" 를 함께 말합니다.
LIMIT_REACHED_AFTER_TOOL_CALLS_NOTICE = (
    "[응답 한도 도달] 직전 응답은 응답 한도(max_tokens={max_tokens})에 닿아 멈췄습니다. "
    "그 안의 도구 호출은 인자를 읽을 수 있어 **실행되었고**, 결과는 위에 있습니다. "
    "다시 부를 필요는 없습니다.\n"
    "다만 한도에 닿은 자리는 마지막 호출(`{last_call}`)의 인자 끝입니다. 그 끝부분이 "
    "잘렸을 수 있으니 결과가 의도와 맞는지 확인하세요.{write_hint}\n"
    "같은 응답에서 이어서 하려던 호출이나 설명이 있었다면 그것은 나가지 않았습니다. "
    "도구를 부르기 전의 사고·설명도 같은 한도를 씁니다 — 짧게 쓰세요."
)

def truncation_advice(tools: Optional[List[Dict[str, Any]]] = None) -> str:
    """잘린 뒤에 무엇을 하라고 할지. **이 발언이 실제로 가진 도구**에 따라 다릅니다.

    "나누어 쓰세요" 만으로는 부족합니다. 덮어쓰기 도구로 나누면 이렇게 됩니다.

        1회차  write_file(1부)              5,000자
        2회차  write_file(1부+2부)         10,000자   ← 앞부분을 다시 다 씀
        3회차  write_file(1부+2부+3부)     15,000자   ← 여기서 또 잘림

    호출이 커지면서 제곱으로 늘고, 나눈 보람도 없이 같은 한도에 다시 걸립니다.
    그러니 **덧붙이는 도구가 있는지 확인하고 그 이름을 짚어 주어야** 합니다.
    없으면 나누라는 말 자체가 해로우므로 다른 길(파일을 여러 개로 쪼개기)을
    안내합니다.

    도구를 이름 꼬리로 찾는 것은 메모리 쪽(`memory_write_tool`)과 같은 방식입니다 —
    서버 키가 무엇이든, 꺼져 있든, 없는 도구를 부르라고 시키지 않기 위해서입니다.
    """
    append, write = file_tools(tools)
    tail = "\n그 밖의 호출이라면 인자를 더 짧게 만들어 다시 호출하세요."

    if append:
        text = (
            f"\n파일을 쓰는 중이었다면 **나누어** 쓰세요 — 첫 호출로 앞부분만 쓰고, "
            f"그 다음부터는 `{append}` 로 **뒤에 덧붙이세요**."
        )
        if write:
            text += (
                f" `{write}` 로 이어쓰려 하면 앞부분까지 매번 통째로 다시 써야 해서, "
                f"호출이 점점 커지다 같은 자리에서 또 잘립니다."
            )
        edit = edit_tool(tools)
        if edit:
            text += (
                f"\n이미 있는 파일의 일부를 고치던 중이었다면 전체를 다시 보내지 말고 `{edit}` 로 "
                f"바꿀 부분만 보내세요."
            )
        return text + tail

    if write:
        return (
            f"\n지금 쓸 수 있는 파일 도구는 `{write}` 뿐이고 이것은 **덮어쓰기**입니다. "
            f"나누어 써도 앞부분을 매번 다시 써야 하므로 소용이 없습니다. 대신 내용을 "
            f"**여러 파일로 쪼개** 각각 한 번에 쓰거나(예: 1부/2부), 내용 자체를 줄이세요."
            + tail
        )

    return "\n인자를 더 짧게 만들어 다시 호출하세요."

def limit_reached_notice(
    max_tokens: Any, last_call: str, tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """응답이 한도에 닿았지만 호출 인자는 읽혔을 때 모델에게 할 말.

    파일 쓰기 조언은 **마지막 호출이 쓰기일 때만** 붙입니다. 파일을 읽다가 한도에
    닿은 모델에게 "나누어 쓰라" 고 하면, 하지도 않은 일을 고치라는 말이 됩니다.
    """
    write_hint = ""
    if is_file_writing_call(last_call):
        write_hint = (
            " 파일을 썼다면 내용이 끝까지 들어갔는지 읽어서 확인하고, 끝이 잘렸다면 "
            "같은 내용을 통째로 다시 보내지 마세요." + truncation_advice(tools)
        )
    label = f"{max_tokens:,}" if isinstance(max_tokens, int) else str(max_tokens)
    return LIMIT_REACHED_AFTER_TOOL_CALLS_NOTICE.format(
        max_tokens=label, last_call=last_call or "unknown_tool", write_hint=write_hint,
    )

def message_text(message: Any) -> str:
    """응답 본문. 프로바이더에 따라 블록 목록으로 오기도 합니다."""
    content = getattr(message, "content", None) or ""
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)

def _raw_arguments(call: Any) -> Any:
    """tool_call 하나의 인자 원문 (객체든 딕셔너리든)."""
    function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
    if isinstance(function, dict):
        return function.get("arguments")
    return getattr(function, "arguments", None)

def _argument_chars(raw: Any) -> int:
    """도구 호출 인자 원문의 글자 수. 지문·예산 보고·사고 비중이 같은 잣대를 씁니다."""
    if isinstance(raw, str):
        return len(raw)
    if raw is None:
        return 0
    return len(json.dumps(raw, ensure_ascii=False, default=str))

# 한 응답의 출력 중 사고(reasoning)가 이 비율 이상이면, 잘린 도구 호출 고지에 "사고를
# 줄이라" 를 덧붙입니다.
#
# 추론 모델은 숨은 사고도 `max_tokens` 에 넣어 셉니다. 실제로 사고 8,605자 + 파일 쓰기
# 인자 17,672자로 8,192 토큰에 닿은 일이 있었습니다 — 사고가 출력의 3분의 1이었습니다.
# 그런데 모델이 받은 말은 "나누어 쓰라" 뿐이라, 다음 판에서 또 그만큼 생각하고 조금 작은
# 조각을 쓰면 다시 걸립니다. 로그의 `reasoning=` 은 사람만 봤습니다.
#
# 25% 아래에서는 붙이지 않습니다. 사고가 조금뿐인 모델에게 "생각을 줄이라" 는 소음이고,
# 줄일 것도 없는데 줄이려다 판단이 부실해집니다.
#
# 글자로 재는 것은 `completion_budget_report` 와 같은 잣대를 쓰기 위해서입니다. 인자는
# JSON 이스케이프(`\n` 가 두 글자)로 부풀어 세어지므로 사고의 비중은 실제보다 **작게**
# 나옵니다 — 틀리더라도 덜 알리는 쪽으로 틀립니다.
REASONING_HEAVY_SHARE = 0.25

REASONING_HEAVY_TOOL_CALL_NOTE = (
    "\n또 이번 응답은 출력의 약 {percent}%를 **사고(reasoning)에 썼습니다.** 사고와 도구 "
    "호출 인자는 같은 응답 한도를 나눠 씁니다. 다음 호출에서는 사고를 짧게 끝내고 곧바로 "
    "도구를 부르세요. 특히 **파일에 쓸 내용을 사고 안에서 미리 써 보지 마세요** — 같은 글을 "
    "두 번 쓰는 셈이라 한도가 그만큼 사라집니다."
)

def reasoning_share(message: Any, raw_calls: Optional[List[Any]] = None) -> float:
    """이번 응답의 출력(본문 + 사고 + 호출 인자) 중 사고가 차지한 비율, 0.0~1.0."""
    reasoning = len(getattr(message, "reasoning_content", None) or "")
    if not reasoning:
        return 0.0
    output = reasoning + len(message_text(message))
    output += sum(_argument_chars(_raw_arguments(call)) for call in raw_calls or [])
    return reasoning / output if output else 0.0

def reasoning_heavy_note(message: Any, raw_calls: Optional[List[Any]] = None) -> str:
    """사고가 출력 예산을 크게 먹었으면 덧붙일 한 단락. 아니면 빈 문자열."""
    share = reasoning_share(message, raw_calls)
    if share < REASONING_HEAVY_SHARE:
        return ""
    return REASONING_HEAVY_TOOL_CALL_NOTE.format(percent=round(share * 100))

def completion_budget_report(
    message: Any,
    raw_calls: Optional[List[Any]],
    parsed: Optional[List[Tuple[str, Dict[str, Any], str, bool]]],
    prompt_tokens: int,
    window: int,
    schema_tokens: int = 0,
) -> str:
    """응답 한 번의 출력 예산을 **무엇이** 썼는지. 크기만 적고 내용은 적지 않습니다.

    `finish_reason='length'` 로그에 도구 이름만 있으면, 20토큰짜리 `read_file` 호출이
    4,096토큰 한도에 걸린 것처럼 보입니다. 실제로 예산을 먹는 것은 그 호출이 아니라
    같은 응답 안의 다른 것들이고, 무엇인지에 따라 대처가 다릅니다.

    * `text` 가 크다       — 호출 앞에 긴 사고·설명을 썼습니다 (prompt 모드의 Thought).
    * `reasoning` 이 크다  — 추론 모델의 숨은 사고입니다. 화면에는 안 보입니다.
    * 어떤 호출의 `args` 가 크다 — 긴 파일 쓰기 같은 호출 자체입니다.
    * 셋 다 작은데 `prompt` 가 창에 가깝다 — 서버가 남은 창만큼만 출력을 허락했습니다.

    내용을 적지 않는 것은 `request_fingerprint` 와 같은 규칙입니다. 로그는 사람의
    코드와 문서가 그대로 새어 나가도 되는 곳이 아닙니다.
    """
    text_chars = len(message_text(message))
    reasoning_chars = len(getattr(message, "reasoning_content", None) or "")

    calls = []
    for index, (name, _args, _call_id, parsed_ok) in enumerate(parsed or []):
        raw = _raw_arguments(raw_calls[index]) if raw_calls and index < len(raw_calls) else None
        arg_chars = _argument_chars(raw)
        mark = "" if parsed_ok else ", unreadable"
        calls.append(f"{name or 'unknown_tool'}(args {arg_chars:,} chars{mark})")

    # 도구 정의도 요청마다 나가는 입력입니다. 따로 적어야 "메시지는 작은데 창이 찼다" 가 보입니다.
    tools_part = f" + tools≈{schema_tokens:,} tok" if schema_tokens else ""
    report = (
        f"text={text_chars:,} chars, reasoning={reasoning_chars:,} chars, "
        f"prompt≈{prompt_tokens:,} tok{tools_part} (window {window:,})"
    )
    if calls:
        report += f", calls=[{', '.join(calls)}]"
    return report
