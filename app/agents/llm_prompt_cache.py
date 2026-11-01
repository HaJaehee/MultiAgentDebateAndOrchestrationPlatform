"""Claude 의 명시적 프롬프트 캐싱: 요청의 어느 자리에 `cache_control` 표시를 달지 정합니다 (ADR-045).

OpenAI·Gemini·vLLM 은 같은 접두를 알아서 캐시합니다. Claude 는 표시를 단 자리까지만 캐시하고, 표시는 한 요청에
4개까지입니다. 캐시는 도구 → 시스템 → 메시지 순서의 접두를 바이트 단위로 비교하므로, **다음 요청에서도 그대로일 자리**에
달아야 합니다. 바뀌는 글 뒤에 달면 쓰기 할증만 내고 한 번도 읽히지 않습니다.

| 자리 | 어디서 다시 읽히는가 |
| :--- | :--- |
| 마지막 도구 정의 | 같은 모델과 같은 도구를 쓰는 다른 에이전트 |
| 시스템 프롬프트 | 이 에이전트의 모든 호출 |
| 목표 메시지의 끝 | 뒤따르는 기록이 바뀌어도 (긴 발언이 요지로 줄거나 창에 맞춰 잘릴 때) |
| 마지막 자기 발언 | 다음 라운드 |
| 도구 루프의 마지막 메시지 | 같은 발언의 다음 판 |

마지막 자기 발언이 라운드의 경계인 이유: 그 앞의 기록은 다음 라운드에도 그대로입니다. 그 뒤의 발언은 아직 답하지 않은
새 발언이라 원문으로 실리고, 이 발언자가 다시 말하고 나면 요지로 바뀝니다 (`build_context_for_agent` 의 `body_for`).

발언을 시작하는 판의 마지막 메시지(장부와 라운드 번호가 든 이번 차례 지시)에는 달지 않습니다. 도구를 부르지 않고 끝나는
발언이면 다시 읽히지 않기 때문입니다. 도구를 불렀으면 다음 판의 표시가 그 지시까지 함께 덮습니다.
"""

from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import litellm

from app.agents.base import Agent

MAX_CACHE_MARKS = 4

# LiteLLM 이 표시를 Claude 형식으로 옮겨 보내는 연결. 사내 게이트웨이 경로(`openai/claude-…`)는 LiteLLM 이 표시를
# 지우고 보내므로 달아 봐야 소용이 없고, 메시지 모양만 바뀌어 엄격한 서버에 거절될 수 있어 넣지 않습니다.
CLAUDE_ROUTES = frozenset({"anthropic", "bedrock", "bedrock_converse", "vertex_ai", "azure_ai"})


@lru_cache(maxsize=256)
def route_takes_cache_marks(model: str, provider: Optional[str] = None) -> bool:
    """이 연결에 표시를 달아도 되는지.

    Anthropic 경로가 아니면 이름에 `claude` 가 있어야 합니다. Bedrock 에는 Llama·Nova 같은 다른 모델도 있습니다.

    LiteLLM 의 모델 표에 있으면 표를 따릅니다. 캐싱이 없는 옛 모델(Bedrock 의 Claude Opus 3 등)이 요청을 거절하지 않게
    하기 위해서입니다. 표에 없으면 답니다. 표는 새 모델보다 늦고, 옛 이름은 표에서 빠지기도 합니다.
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
    """이 에이전트의 요청에 달 표시. 달지 않는 연결이면 None."""
    if agent.prompt_cache == "off" or not route_takes_cache_marks(agent.model, agent.provider):
        return None
    return {"type": "ephemeral", "ttl": "1h"} if agent.prompt_cache == "1h" else {"type": "ephemeral"}


def _marked(msg: Dict[str, Any], mark: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """`msg` 의 끝에 표시를 단 사본. 달 글이 없으면 None 입니다 (빈 글에 달면 Anthropic 이 400 으로 거절합니다)."""
    content = msg.get("content")
    if msg.get("role") == "tool":
        # 도구 결과는 글 하나입니다. LiteLLM 이 메시지의 표시를 tool_result 와 Bedrock 의 cachePoint 로 옮깁니다.
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
    """목표 메시지(시스템 바로 다음)의 끝에 표시를 답니다. `merge_consecutive_roles` **전에** 불러야 합니다.

    합치고 나면 목표와 뒤따르는 남의 발언이 한 글이 되어 경계를 찾을 수 없습니다. 먼저 블록으로 바꿔 두면 합칠 때도 경계가
    남고, 창에 맞추다 덧붙는 생략 안내도 목표 글을 바꾸지 않습니다.

    다른 표시와 달리 이 표시는 메시지에 남습니다. 두 자르기 모두 이 자리를 남기므로 발언 동안 그대로이고, 끊긴 발언을
    이어 갈 때도 같은 모양으로 돌아옵니다.

    뒤따르는 메시지가 없으면 한 번으로 끝나는 프롬프트(장부 갱신·요약)이므로 달지 않습니다.
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
    """표시를 단 요청용 사본 `(messages, tools)`. 표시를 달지 않는 연결이면 받은 그대로입니다.

    원본은 건드리지 않습니다. 도구 루프가 같은 목록에 판을 쌓으므로 원본에 달면 표시가 판마다 늘어납니다.

    자리가 모자라면 뒤쪽이 먼저입니다. 시스템의 표시가 앞선 도구까지 함께 덮으므로, 다섯 자리가 다 있을 때 빠지는
    도구의 표시는 다른 에이전트와 나눠 쓰는 몫만 잃습니다.
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
