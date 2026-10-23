"""요청을 모델의 컨텍스트 창에 맞추기: 토큰 어림, 창의 여유, 넘칠 때 오래된 것부터 줄이기,
도구 루프 중의 맞춤, 장부를 마지막 지시 앞에 두기.
"""

import json
import logging
import math
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import litellm

from app.agents.base import Agent
from app.agents.llm_budget import TOOL_BUDGET_FINAL_COUNTDOWN

logger = logging.getLogger(__name__)


LEDGER_HEADER = (
    "[Session Decision Ledger]: 오케스트레이터가 이 대화의 토론을 라운드마다 정리한 결정 "
    "장부입니다. 앞선 발언이 컨텍스트에서 생략돼도 남습니다. 유저 발언이나 세션 지침과 "
    "어긋나면 그쪽을 따르세요."
)

def place_ledger_last(messages: List[Dict[str, Any]], ledger: str) -> List[Dict[str, Any]]:
    """결정 장부를 **마지막 사용자 메시지의 맨 앞**에 붙입니다. 장부가 없으면 그대로.

    프롬프트 캐싱(OpenAI·Gemini·vLLM 의 접두 캐시)은 앞부분이 같은 요청만 싸게 처리합니다.
    장부는 라운드마다 바뀌므로, 시스템 프롬프트에 두면 그 뒤의 기록 전체가 매번 새로
    계산됩니다. 끝에 두면 시스템 프롬프트와 기록은 그대로 캐시를 탑니다.

    마지막 사용자 메시지는 대개 "이번 차례" 지시라, 장부 → 지시 순서가 됩니다. 지시가 맨
    끝에 남아야 모델이 무엇을 하라는지 놓치지 않습니다. 마지막이 사용자 메시지가 아니면
    장부를 새 사용자 메시지로 덧붙입니다. `fit_context_window` 는 마지막 메시지를 남기므로
    장부도 잘리지 않습니다.
    """
    if not ledger or not ledger.strip():
        return list(messages)
    block = f"{LEDGER_HEADER}\n{ledger.strip()}"
    out = [dict(m) for m in messages]
    if out and out[-1].get("role") == "user" and isinstance(out[-1].get("content"), str):
        out[-1]["content"] = f"{block}\n\n{out[-1]['content']}"
    else:
        out.append({"role": "user", "content": block})
    return out

def merge_consecutive_roles(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 role 이 연달아 오면 하나로 합칩니다.

    토론 기록은 본질적으로 다자 대화인데 OpenAI 형식에는 "다른 에이전트" role 이
    없습니다. 그래서 남의 발언은 전부 user, 자기 발언만 assistant 로 넣게 되고,
    발언자가 셋이면 user 가 연달아 3~5개씩 나갑니다.

    OpenAI 는 이것을 받아주지만 **Anthropic·Gemini 와 상당수 OpenAI 호환 셔임
    (llama.cpp server, 일부 vLLM 챗 템플릿)은 400 Bad Request 로 거절합니다**
    ("roles must alternate between user and assistant"). 라운드가 늘수록 연속
    구간이 길어지므로, 이런 엔드포인트에서는 오케스트레이터만 응답하고 전문가
    에이전트는 전부 실패합니다.

    발언마다 `[이름 (역할)]:` 머리표가 이미 붙어 있어, 합쳐도 누가 말했는지는
    그대로 남습니다. tool 호출이 얽힌 메시지는 건드리지 않습니다.
    """
    merged: List[Dict[str, Any]] = []
    for msg in messages:
        prev = merged[-1] if merged else None
        mergeable = (
            prev is not None
            and prev.get("role") == msg.get("role")
            and msg.get("role") in ("user", "assistant")
            and not prev.get("tool_calls") and not msg.get("tool_calls")
            and isinstance(prev.get("content"), str) and isinstance(msg.get("content"), str)
        )
        if mergeable:
            prev["content"] = f"{prev['content']}\n\n{msg['content']}"
        else:
            merged.append(dict(msg))
    return merged

def _rough_tokens(text: str) -> int:
    """토크나이저 없이 어림한 토큰 수. 모자라게 세느니 넘치게 셉니다.

    ASCII 는 영어 기준 글자 3~4개가 한 토큰이라 3개로 나누고, 그 밖의 글자(한글·한자
    등)는 토크나이저에 따라 글자당 1~1.5 토큰이라 1.5 로 셉니다.

    예전 대체 계산은 `전체 글자 수 // 2` 였습니다. 주석은 "넉넉히 잡는다" 고 했지만
    한글에서는 실제의 절반 이하로 세어, 컨텍스트 자르기가 해야 할 때 멈춰 있었습니다.
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 3 + (len(text) - ascii_chars) * 1.5)

def _countable_text(msg: Dict[str, Any]) -> str:
    """토큰을 어림할 때 셀 글자. 본문뿐 아니라 **도구 호출 인자**까지 셉니다.

    큰 파일을 쓴 assistant 턴은 content 가 비어 있고 무게가 전부 인자에 있습니다.
    예전 대체 계산은 content 만 세서, 1만 8천 자짜리 호출을 4토큰으로 셌습니다.
    """
    parts: List[str] = []
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            parts.append(str(block.get("text") or "") if isinstance(block, dict) else str(block))
    elif content:
        parts.append(str(content))
    for call in msg.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
        if isinstance(function, dict):
            name, args = function.get("name"), function.get("arguments")
        else:
            name, args = getattr(function, "name", ""), getattr(function, "arguments", "")
        parts.append(str(name or ""))
        parts.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str))
    if msg.get("name"):
        parts.append(str(msg["name"]))
    return "\n".join(parts)

def estimate_tokens(model: str, messages: List[Dict[str, Any]]) -> int:
    """메시지 목록의 토큰 수. 토크나이저를 못 쓰면 글자 수로 어림잡습니다."""
    try:
        return int(litellm.token_counter(model=model, messages=messages))
    except Exception:  # noqa: BLE001 - 토큰 계산 실패가 호출을 막아서는 안 됩니다
        return sum(_rough_tokens(_countable_text(m)) + 4 for m in messages)

@lru_cache(maxsize=64)
def _schema_tokens(model: str, payload: str) -> int:
    try:
        return int(litellm.token_counter(model=model, text=payload))
    except Exception:  # noqa: BLE001
        return _rough_tokens(payload)

def tool_schema_tokens(model: str, tools: Optional[List[Dict[str, Any]]]) -> int:
    """요청마다 함께 나가는 도구 정의의 토큰 수. 도구가 없으면 0.

    컨텍스트 예산은 대화 메시지만 셌습니다. 그런데 도구 정의는 **매 요청에** 실려
    나가고, filesystem·memory·git 만 붙여도 35개에 5천 토큰이 넘습니다. 출력용 여유는
    `max_tokens + 512` 뿐이라, 대화가 예산까지 차면 실제 요청은 창을 그만큼 넘겼습니다
    — 서버는 400 을 내거나, 남은 창만큼만 출력하게 해 곧바로 `finish_reason='length'`
    를 냈습니다. 같은 도구 목록은 한 발언 동안 몇 번이고 다시 세므로 기억해 둡니다.
    """
    if not tools:
        return 0
    payload = json.dumps(tools, ensure_ascii=False, sort_keys=True, default=str)
    return _schema_tokens(model, payload)

def effective_max_tokens(agent: Agent) -> int:
    """이 에이전트의 요청에 **실제로 실리는** `max_tokens`.

    `native` 모드에서 사고 예산이 `max_tokens` 이상이면, 사고에 다 쓰고 답할 자리가
    남지 않으므로 요청에는 둘을 더한 값이 나갑니다. 예전에는 요청만 그 값을 쓰고,
    컨텍스트 예산·모델 안내·로그·발언 꼬리표는 설정값 `max_tokens` 를 그대로 썼습니다.
    출력 몫을 적게 떼어 두니 창을 넘길 수 있었고, 사람과 모델은 틀린 숫자를 봤습니다.
    그래서 요청을 만드는 쪽(`build_completion_kwargs`)과 나머지가 모두 여기를 봅니다.
    """
    st = agent.sequential_thinking
    budget = st.thinking_budget_tokens if (st.enabled and st.mode == "native") else None
    if budget and agent.max_tokens <= budget:
        return budget + agent.max_tokens
    return agent.max_tokens

def max_tokens_label(agent: Agent) -> str:
    """사람·모델에게 보여줄 응답 한도. 사고 예산이 더해졌으면 그 사실을 함께 적습니다.

    실제 값만 적으면, conf.json 에 4096 이라고 적은 사람이 8,192 를 보고 어디서 온
    숫자인지 모릅니다. 설정값만 적으면 틀린 숫자입니다. 그래서 둘 다 적습니다.
    """
    effective = effective_max_tokens(agent)
    if effective == agent.max_tokens:
        return f"{effective:,}"
    return f"{effective:,} (설정 {agent.max_tokens:,} + 사고 예산 {effective - agent.max_tokens:,})"

def fit_context_window(
    agent: Agent,
    messages: List[Dict[str, Any]],
    memory_tool: Optional[str] = None,
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """`max_context_window` 안에 들어가도록 가운데 발언부터 덜어냅니다.

    라운드가 쌓이면 전사(transcript)가 그대로 길어져 컨텍스트 한도를 넘고,
    엔드포인트는 400 ("maximum context length ... however you requested ...") 을
    돌려줍니다. 지금까지 이 설정은 conf.json 에 선언만 되어 있고 아무데서도
    읽히지 않았습니다.

    맨 앞(목표)과 맨 뒤(이번 차례 지시)는 남깁니다. 그 사이를 오래된 것부터
    덜어내고, 무엇이 빠졌는지 모델에게 알려 줍니다.

    `(messages, 생략된 발언 수)` 를 돌려줍니다. 생략 건수를 밖으로 내보내는 것은
    화면에 알리기 위해서입니다 — 예전에는 `logger.warning` 만 남아, 토론 기록이
    사라지는 것을 보고 있는 사람이 알 방법이 없었습니다.
    """
    budget = context_budget(agent, tools=tools)
    if budget <= 0 or len(messages) <= 3:
        return messages, 0
    if estimate_tokens(agent.model, messages) <= budget:
        return messages, 0

    head, tail = messages[:2], messages[-1:]      # system + 목표, 이번 차례 지시
    middle = messages[2:-1]
    dropped = 0
    while middle and estimate_tokens(agent.model, head + middle + tail) > budget:
        middle.pop(0)
        dropped += 1

    if dropped:
        notice = {"role": "user", "content": context_trim_notice(dropped, memory_tool)}
        logger.warning(
            f"Context window trim for {agent.name}: dropped {dropped} message(s) "
            f"(max_context_window={agent.max_context_window})"
        )
        return head + [notice] + middle + tail, dropped
    return head + middle + tail, 0

# 사용률이 이 띠를 **처음 넘을 때만** 고지합니다. 매 판 떠들면 프롬프트만 늘어나고
# (그것도 컨텍스트입니다) 모델은 곧 무시하기 시작합니다.
CONTEXT_PRESSURE_BANDS = (0.5, 0.7, 0.85, 0.95)

# 오프로딩을 권할 띠. 95% 에서는 권하지 않습니다 — 자리가 없는데 메시지를 더
# 얹는 것은 역효과입니다.
CONTEXT_OFFLOAD_BAND = 0.85

# 상향의 하드 상한. 지금 나와 있는 가장 큰 모델을 넉넉히 덮습니다.
CONTEXT_WINDOW_CEILING = 2_000_000

def context_headroom(agent: Agent) -> Optional[int]:
    """모델의 **실제** 입력 한도까지 남은 여유. 한도를 모르면 None.

    컨텍스트 창은 도구 상한과 달리 우리가 정하는 숫자가 아닙니다. `max_context_window`
    를 모델의 실제 한도 위로 올리면, 깔끔하던 트림이 엔드포인트 400 으로 바뀝니다.
    그래서 올려도 되는 폭을 프로바이더 쪽 값으로 확인합니다.

    매핑되지 않은 모델(사설 게이트웨이, vLLM, 일부 별칭)에서는 조회가 실패합니다.
    그때는 None 을 돌려주고, 호출부가 사용자에게 판단을 넘깁니다 — 자기 엔드포인트는
    사용자가 더 잘 압니다. 예외를 삼키는 것은 `estimate_tokens` 의 선례와 같습니다.
    """
    try:
        info = litellm.get_model_info(agent.model)
    except Exception:  # noqa: BLE001 - 한도를 몰라도 호출을 막지는 않습니다
        return None
    real = info.get("max_input_tokens") or info.get("max_tokens")
    if not real:
        return None
    return max(0, int(real) - agent.max_context_window)

def context_budget(
    agent: Agent,
    window: Optional[int] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """전사가 쓸 수 있는 토큰. 응답 분량과 여유를 뺀 값입니다.

    `fit_context_window` 가 쓰던 계산과 같습니다. 여러 곳에서 같은 식을 되풀이하면
    한 곳만 고쳤을 때 서로 다른 기준으로 자르게 됩니다.
    """
    return (
        (window or agent.max_context_window)
        - effective_max_tokens(agent)
        - 512
        - tool_schema_tokens(agent.model, tools)
    )

def context_pressure_notice(
    *,
    used: int,
    budget: int,
    announced: set,
    memory_tool: Optional[str] = None,
    tool_calls_left: Optional[int] = None,
) -> Optional[str]:
    """컨텍스트 사용률 고지. 새로 넘은 띠가 없으면 None.

    `announced` 는 이미 알린 띠의 집합이고, **이 함수가 직접 갱신합니다**. 호출부가
    기억을 따로 들고 있으면 두 곳에서 같은 상태를 관리하게 됩니다.

    `memory_tool` 이 있으면 85% 띠에서 "잘리기 전에 그래프로 옮겨 두라"를 덧붙입니다.
    메모리에 쓴다고 컨텍스트가 그 자리에서 줄지는 않습니다 — 대화 메시지는 그대로
    남습니다. 줄어드는 것은 **잘려도 잃지 않게 되는 것**이고, 문구도 그렇게 씁니다.
    """
    if budget <= 0:
        return None
    ratio = used / budget
    crossed = [b for b in CONTEXT_PRESSURE_BANDS if ratio >= b and b not in announced]
    if not crossed:
        return None

    band = max(crossed)
    announced.update(crossed)
    remaining = max(0, budget - used)
    head = (
        f"[컨텍스트] 이번 발언의 맥락이 한도의 **{int(ratio * 100)}%** 를 쓰고 있습니다 "
        f"(남은 여유 약 {remaining:,} 토큰)."
    )

    if band >= 0.95:
        return (
            f"{head} **여유가 거의 없습니다.** 도구를 더 부르지 말고 지금 결론을 쓰세요. "
            f"길게 쓰면 그만큼 앞선 기록이 잘려 나갑니다."
        )
    if band >= 0.85:
        tail = (
            "곧 앞선 발언과 도구 관측이 오래된 것부터 생략됩니다. 결론을 준비하세요."
        )
        if memory_tool and (tool_calls_left is None or tool_calls_left > TOOL_BUDGET_FINAL_COUNTDOWN):
            tail += (
                "\n\n생략되기 **전에** 지금까지의 핵심 결론·근거·미해결 쟁점을 "
                f"`{memory_tool}` 로 그래프에 한 번에 압축해 남기세요 (호출은 한 번만). "
                f"이 그래프는 이 대화의 다른 에이전트와 최종 합성도 함께 읽습니다. "
                f"옮겨 두면 잘려도 잃지 않습니다."
            )
        return f"{head} {tail}"
    if band >= 0.7:
        return (
            f"{head} 출력이 큰 도구 호출(파일 전체 읽기, 긴 목록)은 자제하고 "
            f"필요한 범위만 요청하세요."
        )
    return f"{head} 요점 위주로 쓰고, 이미 나온 내용을 그대로 옮겨 적지 마세요."

def context_trim_notice(dropped: int, memory_tool: Optional[str] = None) -> str:
    """생략 안내문. 메모리가 있으면 어디서 찾을 수 있는지까지 알려 줍니다."""
    text = (
        f"[앞선 기록 {dropped}건은 컨텍스트 한도로 생략되었습니다. "
        f"남은 기록만으로 판단하고, 생략된 내용을 지어내지 마세요.]"
    )
    if memory_tool:
        text = text[:-1] + f" 필요하면 `{memory_tool}` 로 그래프에서 찾아보세요.]"
    return text

def _is_tool_result(msg: Dict[str, Any]) -> bool:
    return msg.get("role") == "tool"

def _opens_tool_calls(msg: Dict[str, Any]) -> bool:
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))

def fit_tool_loop_context(
    agent: Agent,
    messages: List[Dict[str, Any]],
    window: Optional[int] = None,
    memory_tool: Optional[str] = None,
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    keep: str = "",
) -> Tuple[List[Dict[str, Any]], int]:
    """도구 루프의 대화를 창 안에 맞춥니다. `(messages, 생략된 덩어리 수)`.

    `fit_context_window` 는 발언 시작 전 **한 번만** 돕니다. 그런데 루프는 매 판
    assistant + tool 메시지를 쌓고, 도구 출력은 수십 KB가 되기도 합니다. 그래서 긴
    루프는 창을 넘기고 엔드포인트가 400 을 돌려주며, 그 발언이 통째로 사라졌습니다.

    여기서 메시지를 함부로 버릴 수 없습니다. `tool` 메시지를 앞선
    assistant(`tool_calls`) 없이 남기면 OpenAI 호환 엔드포인트가 400 을 돌려줍니다
    ("messages with role 'tool' must be a response to a preceding message with
    'tool_calls'"). 그래서 **assistant 와 거기 딸린 tool 결과들을 한 덩어리로**
    덜어냅니다.

    맨 앞(system + 목표)과 맨 뒤(가장 최근 덩어리)은 남깁니다. 최근 관측이 지금
    판단의 근거라, 잘라야 한다면 앞쪽을 버리는 편이 낫습니다.

    돌려주기 전에 `merge_consecutive_roles` 를 한 번 태웁니다. 생략 안내는 user 인데
    바로 앞 `head[1]` 도 user 라, 그냥 끼우면 user 가 연달아 두 번이 됩니다 — 발언
    시작 전 경로(`fit_context_window` 의 호출부)가 자르기 뒤에 합치기를 두는 것과
    같은 이유입니다. 그쪽은 지켜졌는데 루프 안쪽만 빠져 있어서, OpenAI 호환 셔임에서
    긴 도구 루프가 400 으로 끊겼습니다.

    `keep` 은 발언을 시작할 때의 마지막 사용자 메시지 — 결정 장부와 이번 차례 지시입니다
    (`place_ledger_last`). 도구를 한 번 부르면 그 뒤로 도구 묶음이 쌓여 이 메시지가 버릴 수
    있는 쪽으로 밀려나, 도구를 많이 부르는 긴 발언에서 장부와 지시(전략 지침·병렬 태스크·요지
    작성 요구)가 함께 사라졌습니다. 그래서 버린 것 중에 이것이 있었으면 **생략 안내에 원문
    그대로 다시 붙입니다.** 생략 안내는 목표 메시지에 합쳐지고 그 자리는 다음 자르기에서도
    버려지지 않으므로, 한 번 되살리면 그 뒤로 계속 남습니다. 잘리지 않았으면 아무것도 바꾸지
    않습니다 — 평소 요청의 모양(과 프롬프트 캐시)은 그대로입니다.
    """
    budget = context_budget(agent, window, tools)
    if budget <= 0 or len(messages) <= 3:
        return messages, 0
    if estimate_tokens(agent.model, messages) <= budget:
        return messages, 0

    head = messages[:2]                  # system + 최초 지시
    rest = messages[2:]

    # rest 를 덩어리로 묶습니다. 도구 호출을 여는 assistant 에서 새 덩어리가
    # 시작되고, 뒤따르는 tool 결과들이 같은 덩어리에 붙습니다.
    blocks: List[List[Dict[str, Any]]] = []
    for msg in rest:
        if _opens_tool_calls(msg) or not blocks:
            blocks.append([msg])
        elif _is_tool_result(msg) and blocks:
            blocks[-1].append(msg)
        else:
            blocks.append([msg])

    def still_has_keep() -> bool:
        # 앞선 자르기에서 이미 다시 붙였으면(자리가 모자라 줄여 붙였을 수도 있어 원문과 다를
        # 수 있습니다) 그 표시로 알아봅니다. 그 자리는 목표 메시지라 버려지지 않습니다.
        return any(
            isinstance(m.get("content"), str)
            and (keep in m["content"] or KEPT_TURN_NOTICE in m["content"])
            for m in head + [m for b in blocks for m in b]
        )

    dropped = 0
    reserve = 0
    # 마지막 덩어리는 남깁니다 — 그것이 지금 판단의 근거입니다.
    while len(blocks) > 1:
        probe = head + [m for b in blocks for m in b]
        if estimate_tokens(agent.model, probe) + reserve <= budget:
            break
        blocks.pop(0)
        dropped += 1
        if keep and not reserve and not still_has_keep():
            # 다시 붙일 몫만큼 더 비웁니다. 붙이고 나서 넘치면 400 입니다.
            reserve = estimate_tokens(agent.model, [{"role": "user", "content": keep}])

    if not dropped:
        return messages, 0

    notice_text = context_trim_notice(dropped, memory_tool)
    restored = ""
    if reserve:
        # 더 버릴 것이 없을 수 있습니다(마지막 묶음 하나만 남은 경우). 남은 자리에 맞춰
        # 가운데를 덜어 붙입니다 — 앞은 장부, 끝은 이번 차례 지시입니다. 자리가 거의 없으면
        # 붙이지 않습니다. 넘친 요청은 400 이라, 붙여서 발언 전체를 잃는 것보다 낫습니다.
        base = head + [{"role": "user", "content": f"{notice_text}\n\n{KEPT_TURN_NOTICE}"}]
        room = budget - estimate_tokens(agent.model, base + [m for b in blocks for m in b])
        if room >= KEPT_TURN_MIN_TOKENS:
            restored = _clip_middle_to_tokens(agent.model, keep, room)
            notice_text += f"\n\n{KEPT_TURN_NOTICE}\n{restored}"

    logger.warning(
        f"Tool-loop context trim for {agent.name}: dropped {dropped} block(s) "
        f"(max_context_window={window or agent.max_context_window})"
        + (
            ("; restored the turn instruction and ledger" + (" (clipped)" if restored != keep else ""))
            if restored else ("; no room to restore the turn instruction and ledger" if reserve else "")
        )
    )
    notice = {"role": "user", "content": notice_text}
    trimmed = head + [notice] + [m for b in blocks for m in b]
    return merge_consecutive_roles(trimmed), dropped

KEPT_TURN_NOTICE = (
    "[생략된 기록에 있던 이번 차례 지시와 결정 장부를 다시 붙입니다. 여전히 유효합니다]"
)

# 이보다 자리가 없으면 다시 붙이지 않습니다. 몇 단어로 줄인 지시는 오히려 오해를 부릅니다.
KEPT_TURN_MIN_TOKENS = 48

def _clip_middle_to_tokens(model: str, text: str, cap: int) -> str:
    """토큰 상한에 맞게 가운데를 덜어냅니다. 앞(장부)과 끝(이번 차례 지시)을 남깁니다."""
    def cost(value: str) -> int:
        return estimate_tokens(model, [{"role": "user", "content": value}])

    if cost(text) <= cap:
        return text
    marker = "\n…(자리가 모자라 가운데를 생략했습니다)…\n"
    keep = len(text)
    while keep > 0:
        keep = int(keep * 0.8)
        head, tail = text[: keep // 2], text[len(text) - keep // 2:] if keep // 2 else ""
        clipped = f"{head}{marker}{tail}"
        if cost(clipped) <= cap:
            return clipped
    return ""
