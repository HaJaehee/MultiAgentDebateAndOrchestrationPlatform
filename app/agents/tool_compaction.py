"""도구 루프 안에서 오래된 큰 파일 읽기 결과를 줄입니다 (ADR-040).

도구 결과는 `role=tool` 메시지로 대화에 남아 발언이 끝날 때까지 판마다 다시 나갑니다. 큰 파일을 연달아 읽으면 수만
자가 컨텍스트 창을 차지하고, 창이 넘치면 `fit_tool_loop_context` 가 그 결과를 부른 모델의 글까지 덩어리째 버립니다.

줄이는 것은 **같은 호출로 다시 얻을 수 있는** 파일 읽기뿐입니다. 명령 실행이나 웹 검색의 결과는 다시 돌리면 달라지거나
대가가 있어 건드리지 않습니다. 모델이 읽는 메시지만 줄고, 도구 카드와 DB 에 남는 도구 기록(`tool_logs`)은 원문 그대로입니다.

앞쪽 메시지를 고치면 프로바이더의 접두사 캐시가 그 자리부터 깨집니다. 그래서 창이 `min_usage` 만큼 찼을 때만 줄이고,
줄일 때는 오래된 것을 한 번에 줄입니다. 그 뒤로는 다시 찰 때까지 앞부분이 그대로라 캐시가 다시 쌓입니다.
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
    """도구 호출의 인자 글. 모델이 같은 호출을 다시 만들 수 있게 그대로 적습니다."""
    arguments = (call.get("function") or {}).get("arguments")
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, default=str)
    return _clip(text, ARGUMENTS_CHARS)


def compacted_output(name: str, arguments: str, output: str) -> str:
    """파일 읽기 결과를 대신할 글: 무엇을 읽었는지, 얼마나 컸는지, 앞뒤 몇 줄, 다시 읽는 법."""
    lines = output.splitlines()
    head = lines[:EXCERPT_HEAD_LINES]
    tail = lines[max(EXCERPT_HEAD_LINES, len(lines) - EXCERPT_TAIL_LINES):]
    parts = [
        f"{COMPACTED_OUTPUT_HEAD} 앞서 `{name}` 로 읽은 결과입니다 (인자: {arguments or '없음'}, 전체 "
        f"{len(lines):,}줄 {len(output):,}자). 컨텍스트를 아끼려고 앞뒤 일부만 남겼습니다. 생략된 내용을 지어내지 마세요. "
        f"내용이 더 필요하거나 이 파일을 고치려면 같은 도구로 다시 읽으세요. 필요한 범위만 읽을 수 있으면 그렇게 하세요.",
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
    """`stale_rounds` 판 이상 지난 큰 파일 읽기 결과를 스텁으로 바꿉니다. `(메시지, 줄인 결과 수)`.

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


def compact_loop_messages(
    model: str, messages: List[Dict[str, Any]], budget: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """창의 사용률을 보고 줄일 때만 줄입니다. 여기서 실패해도 발언은 받은 메시지로 계속합니다.

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
