import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import litellm
from app.agents.base import Agent
from app.mcp.manager import MCPManager, get_mcp_manager

logger = logging.getLogger(__name__)

# Suppress litellm verbose logging
litellm.suppress_debug_info = True

# Placeholder key for endpoints that require no authentication (Ollama, vLLM, LM Studio...)
LOCAL_API_KEY_PLACEHOLDER = "sk-no-key-required"

# Marker emitted by the sequential-thinking protocol, used to hide steps when show_steps = false
CONCLUSION_MARKERS = ("## 최종 결론", "## Final Conclusion", "## 최종결론")

# `native` 모드에서 `_compose_content` 가 추론 트레이스 앞에 붙이는 머리표.
# 그 아래로 인용(`>`) 줄이 이어지고, 빈 줄 뒤부터가 실제 답변입니다.
NATIVE_REASONING_HEADER = "> **[Sequential Thinking]**"


def strip_reasoning_trace(content: str) -> str:
    """발언 본문에서 사고 과정을 떼고 결론만 남깁니다. 못 찾으면 원문 그대로.

    사고 과정은 **그것을 쓴 에이전트에게만** 의미가 있습니다. 다음 발언자에게
    필요한 것은 앞사람이 무엇을 결론지었고 근거가 무엇인가이지, 그가 5단계로
    어떻게 거기 도달했는가가 아닙니다 (순차 토론 지침도 "결론을 입력으로
    받으라" 고 말합니다).

    그런데 트레이스는 발언 본문의 일부라, 그대로 두면 전사를 읽는 모든 자리로
    복사됩니다 — 다음 발언자의 맥락, 최종 합성 전사, 발언자 지명·과업 분배의
    요약까지. 라운드가 몇 번만 돌아도 전사의 대부분이 '남이 어떻게 생각했는지'
    로 채워지고, 그러면 `fit_context_window` 가 정작 필요한 초반 논의부터
    버리기 시작합니다.

    그래서 **기록에는 남기고 프롬프트에서만 뗍니다.** DB 와 화면은 전문을 그대로
    들고 있으므로 `show_steps` 는 영향을 받지 않습니다.

    두 가지 모양을 처리합니다.

    * `prompt`/`mcp` 모드 — `Thought 1..N` 뒤에 `## 최종 결론` 이 옵니다.
      마커부터 끝까지가 결론입니다.
    * `native` 모드 — `_compose_content` 가 붙인 인용 블록이 본문 앞에 옵니다.
      그 블록만 걷어냅니다.
    """
    text = content or ""
    if not text.strip():
        return text

    # 1) native 모드의 인용 블록. 머리표로 시작할 때만 손댑니다 — 답변 자체가
    #    인용문으로 시작하는 경우를 잘라내지 않기 위해서입니다.
    if text.lstrip().startswith(NATIVE_REASONING_HEADER):
        lines = text.lstrip().splitlines()
        cut = 0
        for i, line in enumerate(lines):
            if line.startswith(">") or not line.strip():
                cut = i + 1
                continue
            break
        remainder = "\n".join(lines[cut:]).strip()
        # 인용 블록이 전부였다면(=답변이 비었다면) 원문을 지킵니다. 빈 발언을
        # 넘기느니 사고 과정이라도 넘기는 편이 낫습니다.
        if remainder:
            text = remainder

    # 2) 단계적 사고 프로토콜의 결론 마커. 여러 마커 중 **가장 먼저 나오는**
    #    위치를 씁니다 (마커 목록의 순서가 아니라 본문에서의 위치).
    found = [idx for idx in (text.find(m) for m in CONCLUSION_MARKERS) if idx != -1]
    if found:
        return text[min(found):].strip()
    return text


class LLMUnavailableError(RuntimeError):
    """LLM 엔드포인트에 닿지 못했을 때 올라옵니다.

    예전에는 여기서 내장 시뮬레이터가 그럴듯한 페르소나 답변을 지어냈습니다.
    엔드포인트가 500 을 돌려준 뒤에도 토론은 멀쩡히 굴러가는 것처럼 보였고,
    그 지어낸 발언이 다음 에이전트의 입력과 최종 합성 보고서까지 오염시켰습니다.
    모르는 것은 모른다고 말하는 편이 낫습니다.
    """

    def __init__(self, agent: Agent, reason: str):
        self.agent_key = agent.key
        self.agent_name = agent.name
        self.model = agent.model
        self.endpoint = agent.endpoint_label
        self.reason = reason.strip() or "원인을 확인할 수 없습니다"
        super().__init__(f"{agent.name} ({agent.model} @ {self.endpoint}): {self.reason}")


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


def estimate_tokens(model: str, messages: List[Dict[str, Any]]) -> int:
    """메시지 목록의 토큰 수. 모델을 모르면 글자 수로 어림잡습니다."""
    try:
        return int(litellm.token_counter(model=model, messages=messages))
    except Exception:  # noqa: BLE001 - 토큰 계산 실패가 호출을 막아서는 안 됩니다
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        # 한글은 토크나이저에 따라 글자당 1~1.5 토큰입니다. 넉넉히 잡습니다.
        return chars // 2 + len(messages) * 4


def fit_context_window(
    agent: Agent,
    messages: List[Dict[str, Any]],
    memory_tool: Optional[str] = None,
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
    budget = context_budget(agent)
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


class ToolCallLog(dict):
    """Dictionary representing a single tool call and its execution result."""
    pass


# 도구 예산이 바닥났을 때 "얼마나 더 허용할지" 를 정해 주는 콜백.
#
# 0 을 돌려주면 확장 없음 — 에이전트는 도구를 떼고 지금까지 얻은 것으로 결론을
# 씁니다. 이 자리에 사람이 있습니다 (엔진이 화면에 물어보는 통로를 끼웁니다).
BudgetArbiter = Callable[[Dict[str, Any]], Awaitable[int]]

# 컨텍스트 창이 넘쳐 기록을 버려야 할 때 "얼마나 넓혀 줄지" 를 정해 주는 콜백.
#
# 0 을 돌려주면 넓히지 않음 — 오래된 것부터 생략하고 진행하거나(무응답), 도구를
# 떼고 결론만 씁니다(명시적 마무리). 어느 쪽인지는 반환값과 함께 오는 `wrap_up`
# 플래그가 정합니다. 도구 예산 쪽과 달리 답이 세 갈래라 dict 를 돌려받습니다.
ContextArbiter = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


# ---------------------------------------------------------------- 도구 예산 고지

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
    "[도구 호출 예산 확장] 사용자가 상한을 {extra}회 늘려 총 {limit}회가 되었습니다 "
    "(남은 호출 {remaining}회). 늘어난 몫은 결론을 내는 데 꼭 필요한 확인에만 쓰고, "
    "같은 탐색을 반복하지 마세요."
)

# 예산이 바닥나 도구 없이 마무리했다는 사실을 발언 끝에 남깁니다. 화면과 기록
# 모두에서 "왜 여기서 멈췄는가" 가 보여야, 사람이 상한을 올릴지 판단할 수 있습니다.
# ------------------------------------------------------------ 컨텍스트 창 포화

# 사용률이 이 띠를 **처음 넘을 때만** 고지합니다. 매 판 떠들면 프롬프트만 늘어나고
# (그것도 컨텍스트입니다) 모델은 곧 무시하기 시작합니다.
CONTEXT_PRESSURE_BANDS = (0.5, 0.7, 0.85, 0.95)

# 오프로딩을 권할 띠. 95% 에서는 권하지 않습니다 — 자리가 없는데 메시지를 더
# 얹는 것은 역효과입니다.
CONTEXT_OFFLOAD_BAND = 0.85

# 상향의 하드 상한. 지금 나와 있는 가장 큰 모델을 넉넉히 덮습니다.
CONTEXT_WINDOW_CEILING = 2_000_000

# 메모리 MCP 의 도구 이름. 서버 키(`memory`)가 아니라 **도구 이름 꼬리**로 찾습니다.
# 서버를 껐거나, 연결에 실패했거나, conf.json 에서 키 이름을 바꿨을 때 없는 도구를
# 부르라고 시키지 않기 위해서입니다 (`memory__add_observations` 의 `__` 뒤).
MEMORY_WRITE_TOOLS = ("add_observations", "create_entities")
MEMORY_SEARCH_TOOLS = ("search_nodes", "open_nodes")

# 파일 **뒤에 덧붙일 수 있는** 도구와, **통째로 덮어쓰는** 도구.
#
# 둘을 가르는 이유는 잘린 도구 호출을 어떻게 만회하라고 말할지가 달라지기
# 때문입니다. 공식 filesystem 서버의 `write_file` 은 "completely overwrite" 라,
# 그걸로 이어쓰려 하면 앞부분을 매번 통째로 다시 써야 합니다 — 나눌수록 호출이
# 커지고 같은 자리에서 또 잘립니다. 나누기가 뜻을 가지려면 덧붙이는 도구가
# 있어야 하고, 없으면 아예 다른 조언을 해야 합니다.
APPEND_TOOLS = ("edit_file", "edit_text_file", "append_file", "patch_file", "str_replace")
FILE_WRITE_TOOLS = ("write_file", "write_text_file", "create_file")


def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    """OpenAI 도구 스키마 목록에서 함수 이름만 뽑습니다."""
    names = []
    for tool in tools or []:
        name = (tool.get("function") or {}).get("name")
        if name:
            names.append(str(name))
    return names


def _find_tool(tools: Optional[List[Dict[str, Any]]], suffixes: Tuple[str, ...]) -> Optional[str]:
    """이름이 `suffixes` 중 하나로 끝나는 도구의 **전체 이름**을 돌려줍니다.

    전체 이름이어야 지시문에 모델이 실제로 부를 이름을 그대로 적어 줄 수 있습니다.
    """
    for name in _tool_names(tools):
        tail = name.split("__", 1)[-1]
        if tail in suffixes:
            return name
    return None


def memory_write_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """이 발언이 쓸 수 있는 메모리 기록 도구의 이름. 없으면 None."""
    return _find_tool(tools, MEMORY_WRITE_TOOLS)


def memory_search_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """이 발언이 쓸 수 있는 메모리 검색 도구의 이름. 없으면 None."""
    return _find_tool(tools, MEMORY_SEARCH_TOOLS)


def append_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """파일 뒤에 덧붙일 수 있는 도구의 이름. 없으면 None."""
    return _find_tool(tools, APPEND_TOOLS)


def file_write_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """파일을 통째로 쓰는(덮어쓰는) 도구의 이름. 없으면 None."""
    return _find_tool(tools, FILE_WRITE_TOOLS)


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


def context_budget(agent: Agent, window: Optional[int] = None) -> int:
    """전사가 쓸 수 있는 토큰. 응답 분량과 여유를 뺀 값입니다.

    `fit_context_window` 가 쓰던 계산과 같습니다. 여러 곳에서 같은 식을 되풀이하면
    한 곳만 고쳤을 때 서로 다른 기준으로 자르게 됩니다.
    """
    return (window or agent.max_context_window) - agent.max_tokens - 512


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
    """
    budget = context_budget(agent, window)
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

    dropped = 0
    # 마지막 덩어리는 남깁니다 — 그것이 지금 판단의 근거입니다.
    while len(blocks) > 1:
        probe = head + [m for b in blocks for m in b]
        if estimate_tokens(agent.model, probe) <= budget:
            break
        blocks.pop(0)
        dropped += 1

    if not dropped:
        return messages, 0

    logger.warning(
        f"Tool-loop context trim for {agent.name}: dropped {dropped} block(s) "
        f"(max_context_window={window or agent.max_context_window})"
    )
    notice = {"role": "user", "content": context_trim_notice(dropped, memory_tool)}
    trimmed = head + [notice] + [m for b in blocks for m in b]
    return merge_consecutive_roles(trimmed), dropped


CONTEXT_WIDENED_INSTRUCTION = (
    "[컨텍스트 확장] 사용자가 이 발언의 컨텍스트 한도를 {extra:,} 토큰 늘려 "
    "총 {window:,} 토큰이 되었습니다. 앞선 기록이 생략되지 않고 그대로 남았습니다."
)

TRUNCATED_TOOL_CALL_NOTICE = (
    "[출력 잘림] 직전 도구 호출은 응답 한도(max_tokens={max_tokens:,})에 걸려 "
    "**인자를 끝까지 쓰지 못한 채 잘렸습니다.** 그래서 그 호출은 의도한 대로 "
    "실행되지 않았습니다 (도구 서버가 인자를 읽지 못했다고 답했을 것입니다).\n"
    "같은 내용을 그대로 다시 보내지 마세요 — 또 같은 자리에서 잘립니다.{advice}"
)


# 파일 쓰기 도구를 가진 에이전트의 시스템 프롬프트에 상시로 붙는 두어 줄.
#
# `truncation_advice` 는 **잘린 뒤에** 하는 말이고 이쪽은 **잘리기 전에** 하는 말입니다.
# 사후 수습만으로는 부족했습니다 — 잘린 호출 한 번은 실패한 도구 실행, 낭비된 도구
# 예산, 되돌려 보낼 수 없는 인자, 그리고 다시 쓰는 한 판을 뜻합니다.
#
# 예전에는 "인자를 짧게 쓰라" 는 상시 지시를 일부러 넣지 않았습니다. 모델은 자기
# 출력이 몇 토큰인지 셀 수 없어서 지켜지지 않고, 지키려다 내용만 부실해지기
# 때문입니다. 그런데 여기 적는 것은 **크기가 아니라 전략**입니다 — 어느 도구를
# 고르고 어떤 단위로 나눌 것인가. 토큰을 셀 필요가 없으므로 모델이 실제로 따를 수
# 있고, 요즘 코딩 에이전트들이 전체 쓰기 대신 편집 도구를 주력으로 두는 것과 같은
# 방향입니다.
FILE_WRITING_HEAD = (
    "[파일 쓰기] 파일을 한 번의 도구 호출로 다 쓰려 하지 마세요. **도구 호출의 인자도 "
    "응답 한도(max_tokens)를 함께 씁니다.** 내용이 길면 인자가 중간에서 잘리고 그 호출은 "
    "실패합니다."
)


def file_writing_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """파일 쓰기 도구를 **가진** 에이전트에게만 붙는 상시 지침. 없으면 None.

    도구를 이름 꼬리로 찾는 것은 `truncation_advice` 와 같은 규칙이고, 갈래도 같습니다 —
    덧붙일 수 있으면 나누어 덧붙이라고, 덮어쓰기뿐이면 파일을 쪼개라고 합니다.
    도구가 없으면 None 이라, 파일을 만지지 않는 에이전트(비평가 등)의 프롬프트는
    한 글자도 늘지 않습니다.
    """
    append, write = append_tool(tools), file_write_tool(tools)
    if not append and not write:
        return None

    if append and write:
        body = (
            f"- 먼저 `{write}` 로 첫 부분(도입·목차·첫 절)만 만들고, 이어지는 부분은 "
            f"`{append}` 로 **뒤에 덧붙이세요.** 절이나 챕터 단위로 나누면 됩니다.\n"
            f"- 한 번의 호출에는 한 덩어리만 담으세요. `{write}` 로 파일 전체를 다시 쓰는 "
            f"방식으로 이어붙이면 호출이 점점 커져 결국 잘립니다."
        )
    elif append:
        body = (
            f"- 긴 내용은 `{append}` 로 절이나 챕터 단위로 **나누어 덧붙이세요.** "
            f"한 번의 호출에는 한 덩어리만 담습니다."
        )
    else:
        body = (
            f"- `{write}` 는 덮어쓰기라 이어붙일 수 없습니다. 내용이 길면 **파일을 여러 개로 "
            f"나누어**(예: `01-개요.md`, `02-설계.md`) 각각 한 번에 쓰세요."
        )
    return f"{FILE_WRITING_HEAD}\n{body}"


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
    append, write = append_tool(tools), file_write_tool(tools)
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
        return text + tail

    if write:
        return (
            f"\n지금 쓸 수 있는 파일 도구는 `{write}` 뿐이고 이것은 **덮어쓰기**입니다. "
            f"나누어 써도 앞부분을 매번 다시 써야 하므로 소용이 없습니다. 대신 내용을 "
            f"**여러 파일로 쪼개** 각각 한 번에 쓰거나(예: 1부/2부), 내용 자체를 줄이세요."
            + tail
        )

    return "\n인자를 더 짧게 만들어 다시 호출하세요."

# 발언이 응답 한도에서 끊겼음을 기록과 화면에 남깁니다.
#
# `TRUNCATED_TOOL_CALL_NOTICE` 는 **모델에게** 하는 말이고(다음 판에서 만회하라),
# 이쪽은 **사람에게** 하는 말입니다. 도구 호출이 잘리면 도구 서버가 거절해 주지만,
# 그냥 긴 글이 잘리면 아무도 이의를 제기하지 않습니다 — 문장 중간에서 끝난 발언이
# 그대로 저장되고, 읽는 사람은 그것이 잘린 것인지 원래 그렇게 끝난 것인지 알 수
# 없습니다. 다음 발언자와 최종 합성도 마찬가지고요.
TRUNCATED_ANSWER_FOOTER = (
    "> ⚠️ **응답 한도(max_tokens={max_tokens:,})에 걸려 이 발언은 여기서 잘렸습니다.** "
    "끝맺지 못한 문장이나 닫히지 않은 코드 블록이 있을 수 있습니다. "
    "이어서 받으려면 남은 부분을 다시 요청하거나, 에이전트 설정의 `max_tokens` 를 올리세요."
)

# 이어받기를 다 쓰고도 끝나지 않았을 때. 위와 달리 "이미 N번 이어받았다" 를 밝힙니다 —
# 그래야 사람이 `max_continuations` 를 더 올릴지, `max_tokens` 를 올릴지, 아니면 애초에
# 요구 범위를 좁힐지 판단할 수 있습니다.
CONTINUED_BUT_STILL_TRUNCATED_FOOTER = (
    "> ⚠️ **이어받기 {used}회를 모두 쓰고도 끝나지 않아 여기서 멈췄습니다** "
    "(응답 한도 max_tokens={max_tokens:,}). 끝맺지 못한 문장이나 닫히지 않은 코드 블록이 "
    "있을 수 있습니다. `max_tokens` 를 올리거나, 요구 범위를 좁혀 다시 물어보세요."
)

# 이어받기를 요청하는 지시문.
#
# **이어붙일 것**이라는 사실을 분명히 해야 합니다. 그러지 않으면 모델은 새 답변을
# 시작하는 것으로 알고 "네, 이어서 설명드리겠습니다" 같은 서두를 붙이거나 앞부분을
# 요약해 되풀이합니다. 둘 다 이음매에 그대로 남습니다.
CONTINUE_ANSWER_INSTRUCTION = (
    "[이어쓰기] 바로 위 당신의 글은 응답 한도에 걸려 **문장 중간에서 잘렸습니다.**\n"
    "그 마지막 글자 **바로 다음부터** 이어서 계속 쓰세요.\n"
    "- 이 답변은 앞 글에 **그대로 이어 붙습니다.** 인사말·머리말·'이어서 쓰겠습니다' 같은 "
    "말을 앞에 넣지 마세요.\n"
    "- 앞에서 이미 쓴 내용을 요약하거나 되풀이하지 마세요. 잘린 지점부터 새 내용만 쓰세요.\n"
    "- 코드 블록이나 표 안에서 잘렸다면 그 안에서 계속 쓰고, 끝나면 제대로 닫으세요.\n"
    "- 문서를 끝까지 마무리하세요."
)

BUDGET_WRAP_UP_FOOTER = (
    "> ⚠️ **도구 호출 상한({limit}회)에 도달**해 도구 없이 마무리한 발언입니다 "
    "(도구 {tool_calls}건 실행). 더 확인이 필요하면 에이전트 설정의 "
    "`max_tool_iterations` 를 올리거나, 다음 요청에서 범위를 좁혀 다시 물어보세요."
)


# ---------------------------------------------------------------- 실패 지문

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
        budget = context_budget(agent)
        verdict = "  <-- OVER BUDGET" if budget > 0 and used > budget else ""

        lines = [
            f"Request fingerprint for {agent.name} "
            f"(model={agent.model}, api_base={agent.api_base or 'provider default'}):",
            f"  messages={len(messages)} roles={_role_runs(messages)}",
            f"  tokens~{used:,} / budget {budget:,} "
            f"(window {agent.max_context_window:,}, max_tokens {agent.max_tokens:,}){verdict}",
            f"  tools={len(tools or [])} "
            f"tool_choice={tool_choice if tools else '(none sent)'}",
            f"  largest: {largest}",
        ]
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - 지문을 못 만든다고 실패를 덮지 않습니다
        return f"Request fingerprint unavailable for {agent.name} ({type(exc).__name__}: {exc})"



class LLMCaller:
    """Executes LLM completions with an MCP tool-calling loop.

    호출이 실패하면 `LLMUnavailableError` 를 올립니다. 대체 응답을 만들어 내지 않습니다.
    """

    def __init__(self, mcp_manager: Optional[MCPManager] = None):
        # 기본 런타임. 실제 토론은 발언마다 **그 대화의 작업 공간에 해당하는**
        # 런타임을 `call_agent(mcp=...)` 로 받아 씁니다. 이 값은 그것이 주어지지
        # 않았을 때의 폴백입니다.
        self.mcp_manager = mcp_manager or get_mcp_manager()

    def build_system_prompt(
        self,
        agent: Agent,
        custom_instructions: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """System prompt = persona + sequential thinking + file-writing rule + session instructions.

        세션 지침이 맨 뒤인 것은 그것이 가장 구체적인 지시이기 때문입니다. 파일 쓰기
        지침은 그 앞에 두어, 사람이 세션 지침으로 다르게 시키면 그쪽이 뒤에 옵니다.
        """
        parts = [agent.system_prompt]

        st = agent.sequential_thinking
        if st.enabled and st.mode in ("prompt", "mcp"):
            parts.append(st.render_prompt())
            if st.mode == "mcp":
                parts.append(
                    f"각 사고 단계는 반드시 '{st.mcp_server}' MCP 서버의 sequentialthinking 도구를 호출해 기록한 뒤 진행하세요."
                )

        guidance = file_writing_guidance(tools)
        if guidance:
            parts.append(guidance)

        if custom_instructions:
            parts.append(f"[Session Custom Instructions]:\n{custom_instructions}")

        return "\n\n".join(p for p in parts if p)

    def resolve_tool_servers(self, agent: Agent) -> List[str]:
        """MCP servers this agent may use, including the sequential-thinking server when required."""
        servers = list(agent.allowed_mcp_servers)
        st = agent.sequential_thinking
        if st.enabled and st.mode == "mcp" and st.mcp_server not in servers:
            servers.append(st.mcp_server)
        return servers

    async def call_agent(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[BudgetArbiter] = None,
        context_arbiter: Optional[ContextArbiter] = None,
        on_context_trim: Optional[Callable[[int], Any]] = None,
        mcp: Optional[MCPManager] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Executes a turn for the given agent.
        Returns (response_text, tool_call_logs).

        `mcp` 는 이 발언이 쓸 MCP 런타임입니다. 대화마다 작업 공간이 다를 수
        있고 런타임은 작업 공간마다 따로 뜨므로, 어느 것을 쓸지는 턴을 여는
        쪽(`OrchestratorEngine.run_turn`)이 정해 내려보냅니다. 주지 않으면
        기본 런타임을 씁니다.

        `session_id` 는 MCP 도구 호출의 스코프로 함께 보내집니다. 이걸 빠뜨리면
        서버가 대화를 구분할 수 없어 다른 대화의 상태(지식 그래프 등)를 봅니다.
        발언자(`agent.key`)도 함께 실려서, 커널처럼 에이전트 단위로 나뉘어야 하는
        상태를 서버가 구분할 수 있습니다 (`MCPManager.compose_scope`).

        `budget_arbiter` 는 도구 호출 상한에 닿았을 때 사람에게 확장을 물어보는
        통로입니다. 주지 않으면 확장 없이, 도구를 떼고 결론만 받아 마무리합니다
        (`_run_litellm_loop`). 어느 쪽이든 발언이 통째로 버려지지는 않습니다.

        `context_arbiter` 는 컨텍스트 창이 넘쳐 기록을 버려야 할 때의 같은 통로이고,
        `on_context_trim` 은 실제로 생략이 일어났음을 화면에 알리는 콜백입니다.
        """
        # Retrieve available tools for this agent
        #
        # 시스템 프롬프트보다 먼저 구합니다. 파일 쓰기 지침이 이 에이전트가 실제로
        # 가진 도구의 이름을 짚기 때문입니다 (`file_writing_guidance`).
        #
        # 자르기보다도 먼저입니다. 무엇이 잘렸는지 알리는 문구가 "메모리 그래프에서
        # 찾아보라" 로 바뀌려면, 이 에이전트가 그 도구를 실제로 갖고 있는지 알아야
        # 합니다 (서버를 껐거나 연결에 실패했으면 없는 도구를 가리키게 됩니다).
        mcp = mcp or self.mcp_manager
        tools = mcp.get_openai_tools_for_servers(self.resolve_tool_servers(agent))

        formatted_messages: List[Dict[str, Any]] = [
            {"role": "system",
             "content": self.build_system_prompt(agent, custom_instructions, tools)}
        ]
        formatted_messages.extend(messages)

        # 순서 주의: 먼저 한도에 맞춰 자르고, 그 다음 role 을 합칩니다. 생략 안내가
        # user 로 들어가므로 합치기를 나중에 해야 교대가 보장됩니다.
        formatted_messages, trimmed = fit_context_window(
            agent, formatted_messages, memory_search_tool(tools)
        )
        if trimmed and on_context_trim:
            on_context_trim(trimmed)
        formatted_messages = merge_consecutive_roles(formatted_messages)

        # Real endpoint if an API URL, an API key, or a keyless local runtime is configured
        if not agent.is_live:
            raise LLMUnavailableError(
                agent,
                "api_base 도 api_key 도 설정되어 있지 않습니다. conf.json 의 llm 또는 "
                f"agents.{agent.key} 에 엔드포인트를 지정하세요.",
            )

        try:
            content, logs = await self._run_litellm_loop(
                agent, formatted_messages, tools, on_tool_call, on_chunk=on_chunk,
                session_id=session_id, budget_arbiter=budget_arbiter,
                context_arbiter=context_arbiter, on_context_trim=on_context_trim,
                mcp=mcp,
            )
        except LLMUnavailableError:
            raise
        except Exception as e:
            logger.error(
                f"LLM call failed for {agent.name} "
                f"(model={agent.model}, api_base={agent.api_base or 'provider default'}): {e}"
            )
            raise LLMUnavailableError(agent, f"{type(e).__name__}: {e}") from e

        return self._apply_show_steps(agent, content), logs

    def _apply_show_steps(self, agent: Agent, content: str) -> str:
        """Strips the reasoning steps when sequential_thinking.show_steps is disabled.

        `show_steps = true` 여도 사고 과정은 **다음 발언자의 프롬프트에는** 실리지
        않습니다 (`strip_reasoning_trace`). 여기서 정하는 것은 기록과 화면에
        무엇을 남길지입니다.
        """
        st = agent.sequential_thinking
        if not content or not st.enabled or st.show_steps:
            return content
        return strip_reasoning_trace(content)

    def build_completion_kwargs(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        """Maps the agent configuration onto LiteLLM completion parameters.

        `tool_choice` 는 도구를 넘기되 **부르지는 못하게** 해야 하는 자리를 위해
        열어 둡니다 (`"none"`). 자세한 사연은 `_wrap_up_without_tools` 에 있습니다.
        """
        kwargs: Dict[str, Any] = {
            "model": agent.model,
            "messages": messages,
            "temperature": agent.temperature,
            "max_tokens": agent.max_tokens,
        }

        # Endpoint / credentials
        api_key = (agent.api_key or "").strip()
        if not api_key and agent.api_base:
            # Keyless local servers still need a non-empty value for OpenAI-compatible clients
            api_key = LOCAL_API_KEY_PLACEHOLDER
        if api_key:
            kwargs["api_key"] = api_key
        if agent.api_base:
            kwargs["api_base"] = agent.api_base
        if agent.api_version:
            kwargs["api_version"] = agent.api_version
        if agent.provider:
            kwargs["custom_llm_provider"] = agent.provider

        # Sampling & transport options
        if agent.top_p is not None:
            kwargs["top_p"] = agent.top_p
        if agent.timeout:
            kwargs["timeout"] = agent.timeout
        if agent.num_retries:
            kwargs["num_retries"] = agent.num_retries
        if agent.drop_params:
            kwargs["drop_params"] = True
        if agent.extra_headers:
            kwargs["extra_headers"] = dict(agent.extra_headers)
        if agent.extra_body:
            kwargs["extra_body"] = dict(agent.extra_body)

        # Native (provider-side) sequential thinking
        st = agent.sequential_thinking
        if st.enabled and st.mode == "native":
            if st.reasoning_effort:
                kwargs["reasoning_effort"] = st.reasoning_effort
            if st.thinking_budget_tokens:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": st.thinking_budget_tokens}
                # Anthropic extended thinking requires temperature = 1
                if "claude" in agent.model.lower() or "anthropic" in agent.model.lower():
                    kwargs["temperature"] = 1.0
                if agent.max_tokens <= st.thinking_budget_tokens:
                    kwargs["max_tokens"] = st.thinking_budget_tokens + agent.max_tokens

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        return kwargs

    def _compose_content(self, agent: Agent, message: Any) -> str:
        """Merges provider-side reasoning traces (native thinking) with the answer text."""
        content = getattr(message, "content", None) or ""
        if isinstance(content, list):  # some providers return content blocks
            content = "\n".join(
                block.get("text", "") if isinstance(block, dict) else str(block) for block in content
            )

        st = agent.sequential_thinking
        reasoning = getattr(message, "reasoning_content", None) or ""
        if st.enabled and st.mode == "native" and st.show_steps and reasoning:
            content = f"> **[Sequential Thinking]**\n>\n> {reasoning.strip().replace(chr(10), chr(10) + '> ')}\n\n{content}"

        return content

    # ------------------------------------------------------------ 한 판 호출

    @staticmethod
    async def _emit_chunk(on_chunk: Callable[[str], Any], text: str) -> None:
        if asyncio.iscoroutinefunction(on_chunk):
            await on_chunk(text)
        else:
            on_chunk(text)

    async def _complete_once(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        on_chunk: Optional[Callable[[str], Any]] = None,
        tool_choice: str = "auto",
    ) -> Tuple[Any, str]:
        """LLM 한 판. `(message, finish_reason)`.

        `finish_reason` 을 함께 돌려주는 이유: `"length"` 는 **모델이 하던 말을
        끝내지 못하고 잘렸다**는 뜻입니다. 그 판이 도구 호출이었다면 인자 JSON 이
        중간에서 끊기고, 도구는 인자를 읽지 못해 실패합니다. 그 사실을 모르면
        모델은 같은 호출을 그대로 다시 시도하다 예산만 태웁니다.

        스트리밍이 안 되는 엔드포인트면 한 번만 비스트리밍으로 되묻습니다.
        """
        kwargs = self.build_completion_kwargs(agent, messages, tools, tool_choice)
        streamed_any = False
        try:
            response = await litellm.acompletion(**kwargs, stream=True)
            chunks = []
            async for chunk in response:
                chunks.append(chunk)
                content_delta = (
                    chunk.choices[0].delta.content
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content
                    else None
                )
                if content_delta and on_chunk:
                    streamed_any = True
                    await self._emit_chunk(on_chunk, content_delta)
            complete_response = litellm.stream_chunk_builder(chunks, messages=messages)
            return complete_response.choices[0].message, self._finish_reason(complete_response)
        except Exception as exc:
            # 이미 화면에 흘려보낸 조각이 있으면 비스트리밍으로 다시 부르지 않습니다.
            # 같은 답변이 두 번 붙어 버리고, 무엇보다 이 실패는 삼킬 것이 아니라
            # 발언자에게 그대로 전달되어야 합니다 (LLMUnavailableError).
            if streamed_any:
                logger.error(request_fingerprint(agent, messages, tools, tool_choice))
                raise
            logger.warning(
                f"Streaming completion failed or not supported for {agent.name} ({exc}); "
                f"retrying without stream"
            )
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception:
                # 스트리밍도 비스트리밍도 안 됩니다. 이건 엔드포인트가 요청 자체를
                # 거절한 것이므로, 우리가 무엇을 보냈는지 남깁니다 — 상대가 이유를
                # 알려 주지 않을 때 유일하게 남는 단서입니다.
                logger.error(request_fingerprint(agent, messages, tools, tool_choice))
                raise
            message = response.choices[0].message
            if message.content and on_chunk:
                await self._emit_chunk(on_chunk, message.content)
            return message, self._finish_reason(response)

    @staticmethod
    def _finish_reason(response: Any) -> str:
        """응답이 왜 끝났는지. 모르면 빈 문자열 — 판단을 막지는 않습니다."""
        try:
            return str(getattr(response.choices[0], "finish_reason", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _finish_truncated_answer(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        segments: List[str],
        finish_reason: str,
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """`max_tokens` 에 걸려 잘린 발언을 이어받아 `segments` 를 채웁니다.

        잘린 발언에 표시만 남기는 것으로 충분하지 않은 자리가 있습니다. **최종 합성
        보고서**가 그렇습니다 — 그것이 이 대화의 산출물인데, 문장 중간에서 끊긴 채로
        저장되면 표시가 붙어 있어도 결국 처음부터 다시 돌려야 합니다.

        그래서 잘렸으면 한 판 더 부릅니다. 지금까지 쓴 글을 assistant 발언으로 넣고
        "그 마지막 글자 바로 다음부터 이어서" 라고 지시한 뒤, 돌아온 조각을 **구분자
        없이 그대로 이어 붙입니다.** `segments` 의 다른 조각들은 빈 줄로 이어지지만
        여기는 한 문장이 반으로 갈린 자리라, 빈 줄을 넣으면 그 자리가 문단 경계로
        보이게 됩니다.

        멈추는 조건이 셋입니다. 끝까지 받았거나(`finish_reason` 이 `length` 가 아님),
        `max_continuations` 를 다 썼거나, 이어받기 호출 자체가 실패했거나. 어느
        쪽이든 **그때까지 받은 글은 남깁니다** — 이어받기는 발언을 더 낫게 하려는
        것이지, 실패하면 앞의 것까지 버리라는 것이 아닙니다.

        도구는 넘기지 않습니다. 지금은 쓰던 글을 마저 쓰는 자리이지 새로 무언가를
        확인할 자리가 아닙니다.
        """
        if finish_reason != "length":
            return
        if not segments or agent.max_continuations <= 0:
            segments.append(TRUNCATED_ANSWER_FOOTER.format(max_tokens=agent.max_tokens))
            return

        convo = list(messages)
        used = 0
        for _ in range(agent.max_continuations):
            convo.append({"role": "assistant", "content": segments[-1]})
            convo.append({"role": "user", "content": CONTINUE_ANSWER_INSTRUCTION})
            try:
                message, finish_reason = await self._complete_once(agent, convo, None, on_chunk)
            except Exception as exc:  # noqa: BLE001 - 이어받기 실패로 앞의 글을 버리지 않습니다
                logger.warning(f"Continuation failed for {agent.name}: {type(exc).__name__}: {exc}")
                break

            piece = self._compose_content(agent, message)
            if not piece.strip():
                logger.warning(f"Continuation returned nothing for {agent.name}; stopping")
                break

            used += 1
            segments[-1] = segments[-1] + piece      # 한 문장이 갈린 자리입니다
            if finish_reason != "length":
                logger.info(f"Continued a truncated answer for {agent.name}: {used} time(s)")
                return

        if used:
            segments.append(CONTINUED_BUT_STILL_TRUNCATED_FOOTER.format(
                used=used, max_tokens=agent.max_tokens))
        else:
            segments.append(TRUNCATED_ANSWER_FOOTER.format(max_tokens=agent.max_tokens))

    # ------------------------------------------------------------ 도구 예산

    @staticmethod
    def _append_budget_notice(messages: List[Dict[str, Any]], text: str) -> None:
        """예산 고지를 대화 끝에 붙입니다.

        마지막 메시지가 user 면 그 안에 이어 붙입니다. 새 메시지로 넣으면 user 가
        연달아 두 번이 되어 Anthropic·Gemini 와 여러 OpenAI 호환 셔임이 400 으로
        거절합니다 (`merge_consecutive_roles` 의 사연과 같습니다). 도구 결과 뒤라면
        role 이 tool 이므로 그냥 새로 붙입니다.
        """
        last = messages[-1] if messages else None
        if last is not None and last.get("role") == "user" and isinstance(last.get("content"), str):
            messages[-1] = {**last, "content": f"{last['content']}\n\n{text}"}
            return
        messages.append({"role": "user", "content": text})

    async def _ask_context(
        self,
        arbiter: "ContextArbiter",
        agent: Agent,
        window: int,
        messages: List[Dict[str, Any]],
        tool_calls: int,
    ) -> Dict[str, Any]:
        """컨텍스트가 넘쳤음을 사람에게 알리고 답을 받아 옵니다.

        묻다가 실패해도 발언은 살립니다 — 답을 못 받은 것으로 보고 트림으로
        넘어갑니다 (도구 예산 쪽 중재자와 같은 태도입니다).
        """
        try:
            answer = await arbiter({
                "agent_key": agent.key,
                "agent_name": agent.name,
                "window": window,
                "used": estimate_tokens(agent.model, messages),
                "budget": context_budget(agent, window),
                "headroom": context_headroom(agent),
                "tool_calls": tool_calls,
            })
            return answer or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Context arbiter failed for {agent.name}: {exc}")
            return {}

    async def _wrap_up_without_tools(
        self,
        agent: Agent,
        current_messages: List[Dict[str, Any]],
        segments: List[str],
        tool_logs: List[Dict[str, Any]],
        limit: int,
        on_chunk: Optional[Callable[[str], Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """예산이 바닥난 자리에서 도구 없이 결론만 받아 냅니다.

        예전에는 여기서 `LLMUnavailableError` 를 올렸습니다. 그러면 그때까지
        스트리밍으로 흘러나온 글과 실행된 도구 관측이 전부 실패 안내로 덮여
        사라졌습니다 — 도구를 많이 쓴 긴 발언일수록 잃는 것이 컸고, 정작 원인은
        엔드포인트 장애가 아니라 우리가 정한 상한이었습니다. 상한은 폭주를
        막으라고 있는 것이지, 한 일을 버리라고 있는 것이 아닙니다.
        """
        logger.warning(
            f"Tool budget exhausted for {agent.name}: {limit} call(s), "
            f"{len(tool_logs)} tool execution(s). Asking for a final answer without tools."
        )
        self._append_budget_notice(
            current_messages,
            BUDGET_EXHAUSTED_INSTRUCTION.format(limit=limit, tool_calls=len(tool_logs)),
        )
        try:
            # 도구 목록은 **넘기되 부르지 못하게** 합니다 (`tool_choice="none"`).
            #
            # 예전에는 `tools` 를 아예 빼고 불렀습니다. 의도는 옳았습니다 — 상한을
            # 알려 주고도 목록을 함께 주면 모델은 또 부르려 하고, 그 호출은 실행되지
            # 않은 채 버려지니까요. 그런데 이 시점의 `current_messages` 에는 이미
            # 실행한 도구의 `tool_calls` assistant 메시지와 `tool` 결과가 쌓여
            # 있습니다. **Anthropic 은 tool_use/tool_result 가 든 대화를 `tools`
            # 없이 보내면 400 으로 거절합니다** ("Requests which include `tool_use`
            # or `tool_result` blocks must define tools"). OpenAI 는 받아주므로,
            # 도구를 많이 쓴 긴 발언에서 claude 계열 에이전트만 이유 없이 죽는
            # 모양으로 나타났습니다.
            #
            # `tool_choice="none"` 은 두 요구를 모두 만족시킵니다 — 도구는 정의되어
            # 있고, 모델은 그것을 부를 수 없습니다.
            message, finish_reason = await self._complete_once(
                agent, current_messages, tools, on_chunk,
                tool_choice="none" if tools else "auto",
            )
            final = self._compose_content(agent, message).strip()
            if final:
                segments.append(final)
            # 마무리 발언도 한도에 걸릴 수 있습니다. 예산 소진 꼬리표와 둘 다 붙는
            # 것이 맞습니다 — 서로 다른 두 한도에 걸린 것이고, 사람이 올릴 손잡이도
            # 각각 다릅니다 (`max_tool_iterations` 와 `max_tokens`).
            if finish_reason == "length":
                logger.warning(
                    f"Truncated wrap-up answer from {agent.name}: max_tokens={agent.max_tokens}"
                )
                await self._finish_truncated_answer(
                    agent, current_messages, segments, finish_reason, on_chunk
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Final tool-free answer failed for {agent.name}: {exc}")
            if not segments:
                raise LLMUnavailableError(
                    agent,
                    f"도구 호출 상한 {limit}회를 모두 쓴 뒤의 마무리 호출도 실패했습니다: "
                    f"{type(exc).__name__}: {exc}",
                ) from exc

        segments.append(BUDGET_WRAP_UP_FOOTER.format(limit=limit, tool_calls=len(tool_logs)))
        return "\n\n".join(segments), tool_logs

    # ------------------------------------------------------------ 도구 실행 방어

    @staticmethod
    def _parse_tool_call(tc: Any) -> Tuple[str, Dict[str, Any], str, bool]:
        """모델이 돌려준 tool_call 하나를 `(이름, 인자, id, 읽었는가)` 로 풉니다.

        프로바이더마다 모양이 다르고(객체/딕셔너리), 인자는 모델이 만든
        문자열이라 깨져 있을 수 있습니다. 여기서 터지면 발언 전체가 날아가므로
        어떤 모양이 와도 읽을 수 있는 만큼만 읽고 나머지는 비웁니다.

        네 번째 값은 **인자를 온전히 읽었는가**입니다. 못 읽었다는 사실은 도구
        실행뿐 아니라 그 발언을 되돌려 보낼 때도 필요합니다 — 우리가 읽지 못한
        JSON 을 엔드포인트라고 읽을 수 있는 것은 아니기 때문입니다
        (`_assistant_turn` 참고).
        """
        def _get(obj: Any, key: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        try:
            function = _get(tc, "function") or {}
            fn_name = _get(function, "name") or ""
            fn_args_raw = _get(function, "arguments")
            call_id = _get(tc, "id") or ""
        except Exception:  # noqa: BLE001 - 알 수 없는 모양이면 통째로 포기합니다
            return "", {}, "", False

        parsed_ok = True
        if isinstance(fn_args_raw, dict):
            fn_args: Dict[str, Any] = fn_args_raw
        elif isinstance(fn_args_raw, str):
            try:
                parsed = json.loads(fn_args_raw or "{}")
                fn_args = parsed if isinstance(parsed, dict) else {"input": parsed}
            except Exception:  # noqa: BLE001 - 모델이 만든 JSON 은 자주 깨집니다
                fn_args = {"raw": fn_args_raw}
                parsed_ok = False
        elif fn_args_raw is None:
            fn_args = {}
        else:
            fn_args = {"input": fn_args_raw}

        # tool_call_id 가 비면 OpenAI 호환 엔드포인트가 다음 요청을 거절합니다.
        return (
            str(fn_name),
            fn_args,
            str(call_id) or f"call_{abs(hash((fn_name, id(tc)))):x}",
            parsed_ok,
        )

    @staticmethod
    def _assistant_turn(message: Any, parsed: List[Tuple[str, Dict[str, Any], str, bool]]) -> Dict[str, Any]:
        """도구를 부른 발언을 **우리가 실제로 실행한 모양으로** 정규화해 되돌립니다.

        예전에는 `message.model_dump()` 를 그대로 다시 넣었습니다. 그러면 모델이
        만든 인자 문자열이 손대지 않은 채 다음 요청마다 계속 실려 나갑니다.
        `max_tokens` 에 걸려 JSON 이 중간에서 잘린 경우 — 실제로 겪었습니다,
        `filesystem__write_file` 이 `... if path:` 에서 끊겼습니다 — **우리가
        읽지 못했다고 인정한 그 JSON 을 엔드포인트에 다시 보내는 셈**이고,
        vLLM 은 그것을 채팅 템플릿에 렌더링하다 400 으로 거절했습니다.

        두 가지를 바로잡습니다.

        1. 인자는 항상 `json.dumps` 로 다시 씁니다. 나가는 요청에는 언제나 온전한
           JSON 만 실립니다. 덤으로 **id 도 맞춰집니다** — `_parse_tool_call` 은
           id 가 비면 하나 지어내는데, 예전에는 `tool` 결과만 그 지어낸 id 를 쓰고
           assistant 쪽은 빈 id 그대로여서 짝이 어긋났습니다 (그 자체로 400 입니다).
        2. 못 읽은 인자는 짧은 표시로 바꿉니다. 잘린 14KB 를 되돌려 보내 봐야
           모델에게는 아무 정보도 아니고, 토큰만 먹고, 같은 생성을 다시 유도합니다.
           무슨 일이 있었는지는 `TRUNCATED_TOOL_CALL_NOTICE` 가 말로 설명합니다.
        """
        turn = message.model_dump() if hasattr(message, "model_dump") else dict(message)
        calls = []
        for fn_name, fn_args, call_id, parsed_ok in parsed:
            if parsed_ok:
                arguments = json.dumps(fn_args, ensure_ascii=False)
            else:
                dropped = len(str(fn_args.get("raw", "")))
                arguments = json.dumps(
                    {"_unreadable": f"인자 {dropped:,}자를 읽지 못해 생략했습니다"},
                    ensure_ascii=False,
                )
            calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": fn_name or "unknown_tool", "arguments": arguments},
            })
        turn["tool_calls"] = calls
        return turn

    async def _execute_tool_safely(
        self,
        agent: Agent,
        fn_name: str,
        fn_args: Dict[str, Any],
        session_id: Optional[str],
        mcp: Optional[MCPManager] = None,
    ) -> Tuple[str, str]:
        """MCP 도구를 부르고, 결과를 항상 (문자열, 상태) 로 돌려줍니다.

        `MCPManager.execute_tool` 이 이미 대부분을 흡수하지만, 매니저 자체가
        교체되거나(테스트 더블) 도구 이름 조회 중에 터질 수도 있습니다. 도구
        실패는 발언을 끝낼 이유가 아니라는 규칙을 이 자리에서 한 번 더 지킵니다.
        취소만은 그대로 올려 보냅니다.
        """
        try:
            output, status = await (mcp or self.mcp_manager).execute_tool(
                fn_name, fn_args, scope=session_id, actor=agent.key
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 도구 실패로 발언을 죽이지 않습니다
            logger.error(
                f"Tool '{fn_name}' raised out of the MCP manager for {agent.name}: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return (
                f"도구 '{fn_name}' 실행이 실패했습니다 ({type(exc).__name__}: {exc}). "
                f"다른 방법을 쓰거나, 이미 확보한 정보만으로 결론을 내세요.",
                "error",
            )

        if not isinstance(output, str):
            output = str(output)
        return output, (status or "error")

    @staticmethod
    async def _notify_tool_call(
        agent: Agent,
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]],
        call_log: Dict[str, Any],
    ) -> None:
        """도구 실행을 화면·기록 쪽에 알립니다. 알리다 실패해도 발언은 계속됩니다.

        콜백은 UI 이벤트를 타고 나갑니다. 브라우저가 닫혔거나 큐가 막혔을 때
        여기서 올라온 예외가 발언을 끝내면, 실제로 실행된 도구의 관측을 잃습니다.
        """
        if not on_tool_call:
            return
        try:
            if asyncio.iscoroutinefunction(on_tool_call):
                await on_tool_call(call_log)
            else:
                result = on_tool_call(call_log)
                if asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.warning(
                f"Tool-call notification failed for {agent.name} "
                f"({call_log.get('tool_name')}): {type(exc).__name__}: {exc}"
            )

    async def _run_litellm_loop(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        max_tool_iterations: Optional[int] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[BudgetArbiter] = None,
        context_arbiter: Optional[ContextArbiter] = None,
        on_context_trim: Optional[Callable[[int], Any]] = None,
        mcp: Optional[MCPManager] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """도구 루프. 상한은 두 겹의 안전장치와 함께 돕니다.

        1. **미리 고지** — 매 판이 시작되기 전에 남은 호출 횟수를 에이전트에게
           알립니다. 한계에 가까워질수록 촘촘해집니다 (`tool_budget_notice`).
           남은 예산을 알아야 모델이 결론을 쓸 자리를 스스로 남겨 둡니다.
        2. **소진해도 버리지 않음** — 다 쓰면 `budget_arbiter` 로 사람에게 상한
           확장을 묻고, 확장을 받으면 이어서 돕니다. 못 받으면 도구를 떼고
           "지금까지 얻은 것으로 즉시 결론을 내라" 고 한 판 더 부릅니다. 어느
           쪽이든 그때까지의 발언과 도구 기록은 그대로 살아남습니다.

        컨텍스트 창도 같은 두 겹으로 지킵니다. 매 판 직전에 `fit_tool_loop_context`
        로 창 안에 맞추고(짝 단위로 덜어냅니다 — 고아 `tool` 메시지는 400 입니다),
        사용률이 띠를 넘으면 `context_pressure_notice` 로 알립니다. 기록을 실제로
        버려야 하는 첫 순간에는 `context_arbiter` 로 사람에게 묻습니다.
        """
        tool_logs: List[Dict[str, Any]] = []
        current_messages = list(messages)
        limit = max_tool_iterations or agent.max_tool_iterations
        used = 0

        # 컨텍스트 쪽 상태. `window` 는 사람이 넓혀 줄 수 있으므로 에이전트 설정을
        # 그대로 쓰지 않고 이 발언 동안의 유효값으로 들고 다닙니다.
        window = agent.max_context_window
        announced_bands: set = set()
        memory_write = memory_write_tool(tools)
        memory_search = memory_search_tool(tools)
        context_asked = False

        # 이터레이션마다 나온 본문을 모읍니다.
        #
        # 도구를 부르는 판의 텍스트("먼저 파일을 확인하겠습니다…", 도구 결과에 대한
        # 관측과 판단)도 엄연히 그 에이전트의 발언입니다. 화면에는 `on_chunk` 으로
        # 이미 흘러가 있고요. 그런데 예전에는 마지막 판(도구를 부르지 않은 판)의
        # content 만 돌려주었습니다. 그 값이 화면 카드를 통째로 덮어쓰고 DB 에도
        # 그대로 들어가므로, 도구를 많이 부른 긴 발언일수록 사람이 읽고 있던 글이
        # 발언이 끝나는 순간 사라졌습니다 (짧은 답변은 이터레이션이 한 번이라
        # 마지막 판 == 전체였고, 그래서 이 손실이 오래 눈에 띄지 않았습니다).
        segments: List[str] = []

        while True:
            while used < limit:
                # --- 컨텍스트: 넘치면 짝 단위로 덜어내고, 버릴 것이 생기면 물어봅니다.
                budget = context_budget(agent, window)
                over = budget > 0 and estimate_tokens(agent.model, current_messages) > budget

                if over and context_arbiter is not None and not context_asked:
                    context_asked = True
                    answer = await self._ask_context(
                        context_arbiter, agent, window, current_messages, len(tool_logs)
                    )
                    granted = int(answer.get("granted") or 0)
                    if granted > 0:
                        window += granted
                        logger.info(
                            f"Context window for {agent.name} widened by {granted} to {window} "
                            f"by the user"
                        )
                        self._append_budget_notice(
                            current_messages,
                            CONTEXT_WIDENED_INSTRUCTION.format(extra=granted, window=window),
                        )
                        budget = context_budget(agent, window)
                        over = budget > 0 and estimate_tokens(agent.model, current_messages) > budget
                    elif answer.get("wrap_up"):
                        # 넓히지 않고 여기서 접겠다는 뜻입니다. 마무리 호출도 넘친
                        # 메시지로 나가면 400 이므로 먼저 창 안에 맞춥니다.
                        current_messages, dropped = fit_tool_loop_context(
                            agent, current_messages, window, memory_search
                        )
                        if dropped and on_context_trim:
                            on_context_trim(dropped)
                        return await self._wrap_up_without_tools(
                            agent, current_messages, segments, tool_logs, limit, on_chunk,
                            tools=tools,
                        )

                if over:
                    current_messages, dropped = fit_tool_loop_context(
                        agent, current_messages, window, memory_search
                    )
                    if dropped and on_context_trim:
                        on_context_trim(dropped)

                if tools:
                    notice = tool_budget_notice(
                        remaining=limit - used, limit=limit, used=used, tool_calls=len(tool_logs)
                    )
                    if notice:
                        self._append_budget_notice(current_messages, notice)

                    pressure = context_pressure_notice(
                        used=estimate_tokens(agent.model, current_messages),
                        budget=context_budget(agent, window),
                        announced=announced_bands,
                        memory_tool=memory_write,
                        tool_calls_left=limit - used,
                    )
                    if pressure:
                        self._append_budget_notice(current_messages, pressure)

                message, finish_reason = await self._complete_once(
                    agent, current_messages, tools, on_chunk
                )
                used += 1

                segment = self._compose_content(agent, message)
                if segment.strip():
                    segments.append(segment.strip())

                # Check for tool calls
                tool_calls = getattr(message, "tool_calls", None)
                if not tool_calls:
                    # 도구를 부르지 않고 끝난 판입니다. 여기서 잘렸다면 이의를
                    # 제기해 줄 도구 서버가 없으므로 우리가 표시를 남깁니다.
                    if finish_reason == "length":
                        logger.warning(
                            f"Truncated answer from {agent.name}: finish_reason='length', "
                            f"max_tokens={agent.max_tokens}"
                        )
                    await self._finish_truncated_answer(
                        agent, current_messages, segments, finish_reason, on_chunk
                    )
                    return "\n\n".join(segments), tool_logs

                # 실행하기 전에 먼저 다 풀어 둡니다. 되돌려 보낼 발언이 **실제로
                # 실행한 인자와 같은 모양**이어야 하기 때문입니다 (`_assistant_turn`).
                parsed = [self._parse_tool_call(tc) for tc in tool_calls]
                truncated = finish_reason == "length" or any(
                    not parsed_ok for _n, _a, _i, parsed_ok in parsed
                )
                if truncated:
                    logger.warning(
                        f"Truncated tool call from {agent.name}: finish_reason={finish_reason!r}, "
                        f"max_tokens={agent.max_tokens}, "
                        f"tools={[n for n, _a, _i, _ok in parsed]}"
                    )

                # Append assistant message with tool calls to context
                current_messages.append(self._assistant_turn(message, parsed))

                # Execute all requested tool calls
                #
                # 이 구간은 무슨 일이 있어도 예외를 올리지 않습니다. 도구가
                # 실패하면 그 사실을 `role="tool"` 결과로 되돌려 주어야 모델이
                # 읽고 스스로 고칩니다. 여기서 예외를 올리면 발언 전체가
                # LLMUnavailableError 로 덮여, 이미 흘러간 글과 성공한 도구
                # 관측까지 통째로 사라집니다.
                for fn_name, fn_args, call_id, _parsed_ok in parsed:
                    if not fn_name:
                        # 도구 이름조차 못 읽었습니다. 모델에게 그대로 알리고
                        # 다음 판으로 넘깁니다 (고아 tool_call 을 남기면 다음
                        # 요청이 400 으로 거절됩니다).
                        output, status = (
                            "이 도구 호출은 이름을 읽을 수 없어 실행하지 못했습니다. "
                            "도구 이름과 인자를 다시 정확히 지정해 호출하세요.",
                            "error",
                        )
                    else:
                        output, status = await self._execute_tool_safely(
                            agent, fn_name, fn_args, session_id, mcp,
                        )

                    call_log = {
                        "tool_name": fn_name or "(unknown)",
                        "arguments": fn_args,
                        "output": output,
                        "status": status,
                    }
                    tool_logs.append(call_log)
                    await self._notify_tool_call(agent, on_tool_call, call_log)

                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": fn_name or "unknown_tool",
                        "content": output,
                    })

                # 잘렸다는 사실은 **말로** 알려야 합니다. 도구 서버가 돌려준
                # "Input validation error" 만으로는 모델이 원인을 알 수 없어,
                # 같은 호출을 그대로 다시 시도하다 예산을 태웁니다.
                if truncated:
                    self._append_budget_notice(
                        current_messages,
                        TRUNCATED_TOOL_CALL_NOTICE.format(
                            max_tokens=agent.max_tokens,
                            advice=truncation_advice(tools),
                        ),
                    )

            # 예산 소진. 여기서 예외를 올리면 지금까지의 발언이 통째로 사라집니다.
            # 사람에게 한 번 묻고, 답이 없으면 에이전트에게 즉시 끝내라고 합니다.
            granted = 0
            if budget_arbiter is not None:
                try:
                    granted = int(await budget_arbiter({
                        "agent_key": agent.key,
                        "agent_name": agent.name,
                        "limit": limit,
                        "used": used,
                        "tool_calls": len(tool_logs),
                    }) or 0)
                except Exception as exc:  # noqa: BLE001 - 묻다가 실패해도 발언은 살립니다
                    logger.warning(f"Tool budget arbiter failed for {agent.name}: {exc}")
                    granted = 0

            if granted > 0:
                limit += granted
                logger.info(
                    f"Tool budget for {agent.name} extended by {granted} to {limit} by the user"
                )
                self._append_budget_notice(
                    current_messages,
                    BUDGET_EXTENDED_INSTRUCTION.format(
                        extra=granted, limit=limit, remaining=limit - used
                    ),
                )
                continue

            return await self._wrap_up_without_tools(
                agent, current_messages, segments, tool_logs, limit, on_chunk, tools=tools
            )
