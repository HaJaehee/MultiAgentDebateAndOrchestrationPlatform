"""도구 루프 내 오래된 대용량 파일 읽기 결과 압축 (Compaction) 모듈 (ADR-040).

도구 실행 결과는 `role=tool` 메시지로 대화 이력에 유지되어 발언 루프마다 반복 전송됩니다. 대용량 파일을 반복 조회하면
수만 자의 텍스트가 컨텍스트 윈도우를 차지하며, 한도 초과 시 `fit_tool_loop_context`에 의해 모델 발언 전체가 절삭될 수 있습니다.

압축 대상은 **동일한 인자로 재현 가능한 파일 읽기 관측**에 한정됩니다. 셸 실행이나 웹 검색 결과는 부수 효과 및 비결정성으로 인해
압축 대상에서 제외됩니다. 또한 LLM에 주입되는 프롬프트 메시지만 압축 스텁으로 치환되며, UI 도구 카드 및 DB의 도구 로그(`tool_logs`)는 원문을 온전히 보존합니다.

프롬프트 앞부분 수정 시 발생하는 공급자 캐시(Prefix Cache) 무효화를 최소화하기 위해, 컨텍스트 사용량이 임계치(`min_usage`)에 도달했을 때만
오래된 관측들을 일괄 압축합니다.
"""

import json
import logging
from typing import Any, Dict, List, Tuple

from app.agents.llm_context import estimate_tokens
from app.agents.llm_tools import FILE_READ_TOOLS
from app.config import get_config

logger = logging.getLogger(__name__)


# 줄인 결과의 머리표. 이것으로 시작하면 다시 줄이지 않습니다.
COMPACTED_OUTPUT_HEAD = "[관측 압축]"

# 같은 호출로 다시 읽을 수 있는 도구 (이름 꼬리).
REREADABLE_TOOLS = (*FILE_READ_TOOLS, "read_multiple_files")

# 스텁에 남기는 앞뒤 줄 수와 줄당 글자 수. 줄바꿈 없는 큰 파일(압축된 JSON 등)도 이 안에 들어옵니다.
EXCERPT_HEAD_LINES = 12
EXCERPT_TAIL_LINES = 3
EXCERPT_LINE_CHARS = 160
# 스텁에 적는 호출 인자의 상한. `read_multiple_files` 의 경로 목록이 길어질 수 있습니다.
ARGUMENTS_CHARS = 300


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _arguments_text(call: Dict[str, Any]) -> str:
    """도구 호출 인자를 문자열로 포맷팅합니다. 모델이 필요 시 동일한 도구 호출을 재현할 수 있도록 원형을 유지합니다."""
    arguments = (call.get("function") or {}).get("arguments")
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, default=str)
    return _clip(text, ARGUMENTS_CHARS)


def compacted_output(name: str, arguments: str, output: str) -> str:
    """대용량 파일 읽기 결과를 대체할 압축 스텁 텍스트를 생성합니다: 대상 파일, 크기 메타데이터, 선두/후미 발췌본 및 재조회 안내."""
    lines = output.splitlines()
    head = lines[:EXCERPT_HEAD_LINES]
    tail = lines[max(EXCERPT_HEAD_LINES, len(lines) - EXCERPT_TAIL_LINES):]
    # 도구가 잘라서 돌려준 것이 아니라 앱이 나중에 줄였다는 것을 밝힙니다. 그렇게 적지 않았더니 실제 DeepSeek 시험에서 모델이
    # "read_text_file 이 앞 12줄·끝 3줄만 돌려준다" 고 결론 내리고 다시 읽어도 같을 것이라며 다시 읽지 않았습니다. 또 그때 본문에
    # 적어 둔 값을 믿어도 된다고 적지 않았더니, 다 읽고 값을 적은 모델이 스텁에 그 줄이 안 보인다며 "관측이 아니라 오기" 로
    # 철회하고 같은 파일을 18번 다시 읽다 컨텍스트가 넘쳤습니다 (2026-11-11).
    parts = [
        f"{COMPACTED_OUTPUT_HEAD} 앞서 `{name}` 로 읽은 결과입니다 (인자: {arguments or '없음'}, 전체 "
        f"{len(lines):,}줄 {len(output):,}자). 도구는 원문 전체를 돌려줬고 당신은 그때 전체를 봤습니다. 컨텍스트를 아끼려고 "
        f"앱이 나중에 앞뒤 일부만 남기고 줄였습니다. 그때 본문에 적어 둔 값은 실제로 본 것이니 그대로 쓰고, 확인하려고 다시 "
        f"읽지 마세요. 적어 두지 않은 내용이 필요하거나 이 파일을 고치려면 같은 도구로 다시 읽으세요 — 원문 전체를 다시 "
        f"받습니다. 생략된 내용을 지어내지 마세요. 필요한 범위만 읽을 수 있으면 그렇게 하세요.",
        f"--- 앞 {len(head)}줄",
        *(_clip(line, EXCERPT_LINE_CHARS) for line in head),
    ]
    if tail:
        parts += [f"--- 끝 {len(tail)}줄", *(_clip(line, EXCERPT_LINE_CHARS) for line in tail)]
    return "\n".join(parts)


def _opens_round(msg: Dict[str, Any]) -> bool:
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))


def compact_stale_tool_outputs(
    messages: List[Dict[str, Any]], *, stale_rounds: int, min_chars: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """지정된 라운드 수(`stale_rounds`) 이상 경과한 대용량 파일 읽기 관측을 스텁으로 치환합니다. `(압축된 메시지 목록, 압축 건수)`를 반환합니다.

    나이는 그 뒤에 나온 도구 호출 판의 수입니다. 메시지만 보고 세므로 끊긴 발언을 이어 가도 그대로입니다. 가장 최근
    판의 나이는 0 입니다. 받은 목록과 메시지는 고치지 않으며, 줄일 것이 없으면 받은 목록을 그대로 돌려줍니다.
    """
    rounds = sum(1 for msg in messages if _opens_round(msg))
    seen = 0
    calls: Dict[Any, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    compacted = 0
    for msg in messages:
        if _opens_round(msg):
            seen += 1
            calls = {call.get("id"): call for call in msg["tool_calls"]}
        elif msg.get("role") == "tool" and rounds - seen >= stale_rounds:
            name = str(msg.get("name") or "")
            content = msg.get("content")
            if (
                name.split("__", 1)[-1] in REREADABLE_TOOLS
                and isinstance(content, str)
                and len(content) >= min_chars
                and not content.startswith(COMPACTED_OUTPUT_HEAD)
            ):
                stub = compacted_output(name, _arguments_text(calls.get(msg.get("tool_call_id")) or {}), content)
                if len(stub) < len(content):
                    out.append({**msg, "content": stub})
                    compacted += 1
                    continue
        out.append(msg)
    return (out, compacted) if compacted else (messages, 0)


def reclaimable_tokens(model: str, messages: List[Dict[str, Any]], used: int) -> int:
    """자리가 모자랄 때 압축으로 되찾을 토큰. 압축이 꺼졌거나 줄일 것이 없으면 0.

    예산을 넘기면 `compact_loop_messages` 가 가장 최근 판만 남기고 줄이므로, 같은 방식으로 줄인 뒤의 크기와 비교합니다.
    컨텍스트 사용률 고지가 띠를 넘을 때만 부릅니다 (`context_pressure_notice`).
    """
    settings = get_config().tool_output_compaction
    if not settings.enabled:
        return 0
    shrunk, count = compact_stale_tool_outputs(messages, stale_rounds=1, min_chars=settings.min_chars)
    if not count:
        return 0
    return max(0, used - estimate_tokens(model, shrunk))


def compact_loop_messages(
    model: str, messages: List[Dict[str, Any]], budget: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """컨텍스트 윈도우 사용률을 점검하여 임계치 초과 시에만 선택적으로 압축을 수행합니다. 실패하더라도 원본 메시지로 발언을 계속 진행합니다.

    예산을 넘겼으면 다음 차례는 덩어리째 버리기이므로, 가장 최근 판만 남기고 줄입니다. 스텁은 무엇을 읽었는지와 다시
    읽는 법을 남기지만 버리면 부른 기록까지 사라집니다.

    줄일 것이 없으면 토큰을 세지 않습니다. 세는 동안 이벤트 루프가 멈추는데, 파일을 읽지 않는 발언이 그 값을 치를 까닭이 없습니다.
    """
    try:
        settings = get_config().tool_output_compaction
        if not settings.enabled or budget <= 0:
            return messages, 0
        all_but_latest, count = compact_stale_tool_outputs(messages, stale_rounds=1, min_chars=settings.min_chars)
        if not count:
            return messages, 0
        used = estimate_tokens(model, messages)
        if used > budget:
            return all_but_latest, count
        if used < budget * settings.min_usage:
            return messages, 0
        return compact_stale_tool_outputs(messages, stale_rounds=settings.stale_rounds, min_chars=settings.min_chars)
    except Exception as exc:  # noqa: BLE001 - 줄이지 못해도 창 맞춤이 뒤에서 지킵니다
        logger.warning(f"Could not compact stale tool outputs: {exc}")
        return messages, 0
