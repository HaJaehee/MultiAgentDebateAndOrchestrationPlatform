"""Claude 명시적 프롬프트 캐싱: 요청 페이로드 내 cache_control 마커 주입 위치를 정의합니다 (ADR-045).

공통 접두사를 자동 캐시하는 OpenAI·Gemini·vLLM과 달리, Claude는 명시적으로 마커가 지정된 지점까지만
캐시를 생성하며, 마커는 단일 요청당 최대 4개로 제한됩니다. 캐시는 도구 정의 → 시스템 프롬프트 → 메시지
순서의 접두사를 바이트 단위로 비교하므로, **후속 요청에서도 불변으로 유지되는 지점**에 마커를 배치해야 합니다.
가변 텍스트 뒤에 마커를 배치하면 캐시 생성 할증만 발생하고 캐시 적중(Hit)이 전혀 발생하지 않습니다.

| 마커 위치 | 캐시 재사용 대상 (Hit 대상) |
| :--- | :--- |
| 마지막 도구 정의 | 동일 모델 및 동일 도구를 공유하는 타 에이전트 호출 |
| 시스템 프롬프트 | 해당 에이전트의 모든 호출 |
| 목표(Goal) 메시지 끝 | 대화 이력이 갱신되더라도 이후 라운드에서 재사용 (발언 요약 축약 및 컨텍스트 자르기 발생 시) |
| 마지막 자체 발언 | 다음 라운드 발언 호출 |
| 도구 루프의 마지막 메시지 | 동일 발언 내 후속 도구 호출 루프 반복 |

마지막 자체 발언이 라운드 경계가 되는 이유: 해당 발언 이전의 대화 이력은 다음 라운드에서도 불변으로 유지됩니다.
그 이후 발언들은 아직 응답하지 않은 신규 발언이므로 전문으로 제공되며, 해당 에이전트가 후속 발언을 마치면 요약문으로 축약됩니다 (`build_context_for_agent`의 `body_for`).

발언 시작 호출의 마지막 메시지(결정 장부와 라운드 정보가 포함된 턴 지시문)에는 마커를 지정하지 않습니다. 도구를 호출하지 않고
종료되는 단일 턴 발언의 경우 후속 재사용이 발생하지 않기 때문입니다. 도구를 호출한 경우에는 후속 루프 호출 시의 마커가 해당 지시문까지 포함하여 캐시합니다.
"""

from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import litellm

from app.agents.base import Agent

MAX_CACHE_MARKS = 4

# LiteLLM이 마커를 Claude 캐시 규격으로 변환 전달하는 엔드포인트 목록. 사내 게이트웨이 경로(`openai/claude-…`)는 LiteLLM이 마커를
# 제거하고 전달하므로 실효성이 없고, 페이로드 구조만 변경되어 엄격한 프록시 서버에서 거절될 수 있어 제외합니다.
CLAUDE_ROUTES = frozenset({"anthropic", "bedrock", "bedrock_converse", "vertex_ai", "azure_ai"})


@lru_cache(maxsize=256)
def route_takes_cache_marks(model: str, provider: Optional[str] = None) -> bool:
    """해당 연결 엔드포인트가 프롬프트 캐시 마커 주입 대상인지 판별합니다.

    Anthropic 경로가 아닐 경우 모델명에 `claude`가 포함되어야 합니다. Bedrock 등에는 Llama, Nova 등 타 공급자 모델도 공존하기 때문입니다.

    LiteLLM의 모델 정보 테이블에 등록되어 있다면 해당 테이블을 따릅니다. 프롬프트 캐싱을 지원하지 않는 구형 모델(Bedrock의 Claude Opus 3 등)에서
    요청이 거절되는 문제를 방지하기 위함입니다. 테이블에 미등록된 경우 주입을 허용합니다. 모델 테이블 갱신이 신규 모델 출시보다 지연될 수 있기 때문입니다.
    """
    name = model.lower()
    prefix, slash, _ = name.partition("/")
    route = (provider or "").strip().lower() or (prefix if slash else "anthropic" if "claude" in name else "")
    if route not in CLAUDE_ROUTES or (route != "anthropic" and "claude" not in name):
        return False
    try:
        info = litellm.get_model_info(model=model, custom_llm_provider=provider or None)
    except Exception:  # noqa: BLE001 - 표에 없는 모델이나 별칭입니다
        return True
    return bool(info.get("supports_prompt_caching"))


def cache_mark(agent: Agent) -> Optional[Dict[str, str]]:
    """해당 에이전트의 요청에 주입할 캐시 마커 딕셔너리를 반환합니다. 비활성화 대상이면 None."""
    if agent.prompt_cache == "off" or not route_takes_cache_marks(agent.model, agent.provider):
        return None
    return {"type": "ephemeral", "ttl": "1h"} if agent.prompt_cache == "1h" else {"type": "ephemeral"}


def _marked(msg: Dict[str, Any], mark: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """`msg`의 마지막 텍스트 블록에 캐시 마커를 주입한 사본을 반환합니다. 주입 대상 텍스트가 없으면 None을 반환합니다 (빈 콘텐츠 블록에 지정 시 Anthropic API에서 400 Bad Request가 발생합니다)."""
    content = msg.get("content")
    if msg.get("role") == "tool":
        # 도구 실행 결과는 단일 문자열입니다. LiteLLM이 메시지의 마커를 Anthropic의 tool_result 및 Bedrock의 cachePoint로 변환합니다.
        return {**msg, "cache_control": mark} if str(content or "").strip() else None
    if isinstance(content, str):
        if not content.strip():
            return None
        return {**msg, "content": [{"type": "text", "text": content, "cache_control": mark}]}
    if isinstance(content, list):
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
                return {**msg, "content": [*content[:index], {**block, "cache_control": mark}, *content[index + 1:]]}
    return None


def mark_goal(agent: Agent, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """목표(Goal) 메시지(시스템 프롬프트 직후 사용자 메시지) 끝에 캐시 마커를 주입합니다. `merge_consecutive_roles` 실행 전에 호출해야 합니다.

    메시지를 병합한 후에는 목표 메시지와 후속 발언이 단일 텍스트로 합쳐져 경계를 식별할 수 없습니다. 사전에 블록 구조로 변환해 두면
    병합 시에도 블록 경계가 유지되며, 컨텍스트 윈도우 조절을 위한 생략 안내문이 추가되더라도 목표 텍스트 자체는 변동되지 않습니다.

    타 마커와 달리 이 마커는 메시지 구조 내에 보존됩니다. 두 가지 컨텍스트 자르기 로직 모두 이 위치를 보존하므로 발언 진행 중에도 불변으로 유지되며,
    중단된 발언을 재개할 때도 동일한 구조로 복원됩니다.

    후속 메시지가 없는 단일 턴 프롬프트(장부 갱신, 요약 등)는 재사용되지 않으므로 마커를 주입하지 않습니다.
    """
    mark = cache_mark(agent)
    if mark is None or len(messages) < 3 or messages[0].get("role") != "system" or messages[1].get("role") != "user":
        return messages
    goal = _marked(messages[1], mark)
    return messages if goal is None else [messages[0], goal, *messages[2:]]


def _holds_mark(msg: Dict[str, Any]) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and "cache_control" in b for b in content)


def _last_own_speech(messages: List[Dict[str, Any]]) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            return index
    return None


def with_cache_marks(
    agent: Agent,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """캐시 마커가 주입된 요청 페이로드 사본 `(messages, tools)`를 생성합니다. 마커 주입 대상이 아니면 원본을 그대로 반환합니다.

    원본 메시지 목록은 수정하지 않습니다. 도구 실행 루프가 동일 메시지 목록에 호출 단계를 누적하므로 원본을 수정하면 루프마다 마커가 중복 누적됩니다.

    마커 개수 한도(최대 4개) 초과 시 후속 컨텍스트 위치를 우선시합니다. 시스템 프롬프트의 마커가 선행 도구 정의까지 포함하여 캐시하므로,
    5개 위치가 모두 존재할 때 도구 정의 마커가 제외되더라도 타 에이전트 간 도구 정의 공유 캐시만 제한되고 에이전트 내부 캐시 효율은 온전히 유지됩니다.
    """
    mark = cache_mark(agent)
    if mark is None:
        return messages, tools

    marked = list(messages)
    room = MAX_CACHE_MARKS - sum(1 for m in messages if _holds_mark(m))
    places: List[Optional[int]] = []
    if any(m.get("role") == "tool" for m in messages):
        places.append(len(messages) - 1)
    places.append(_last_own_speech(messages))
    if messages and messages[0].get("role") == "system":
        places.append(0)
    for place in places:
        if place is None or room <= 0:
            continue
        replaced = _marked(marked[place], mark)
        if replaced is not None:
            marked[place] = replaced
            room -= 1

    if tools and room > 0:
        tools = [*tools[:-1], {**tools[-1], "cache_control": mark}]
    return marked, tools
