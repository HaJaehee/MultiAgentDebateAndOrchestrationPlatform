import asyncio
import json
import logging
import math
import re
import uuid
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
import litellm
from app.agents.base import Agent
from app.agents.llm_gate import get_llm_gate
from app.agents.skills import (
    DESIGNATED_SKILL_NOTE,
    LOAD_SKILL_TOOL,
    is_skill_tool,
    offered_skills,
    run_skill_tool,
    skill_guidance,
    skill_tools_for,
)
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


def conclusion_from_reasoning(reasoning: str) -> str:
    """사고(reasoning) 안에 쓰인 결론. 결론 마커가 없으면 빈 문자열.

    `prompt` 모드의 사고 프로토콜은 `Thought 1..N` 뒤에 `## 최종 결론` 을 두라고 합니다.
    서버의 reasoning parser 가 사고 종료 표식을 못 찾으면 그 **전부**가 `reasoning_content`
    로 분류되는데, 그때 이 마커가 있으면 결론까지 다 쓰인 것입니다.
    """
    text = reasoning or ""
    found = [idx for idx in (text.find(m) for m in CONCLUSION_MARKERS) if idx != -1]
    return text[min(found):].strip() if found else ""


def reasoning_quote(reasoning: str) -> str:
    """사고를 인용 블록으로. `native` 모드가 본문 앞에 붙이는 모양과 같습니다.

    같은 모양이어야 `strip_reasoning_trace` 가 다음 발언자의 프롬프트에서 걷어냅니다.
    """
    return f"{NATIVE_REASONING_HEADER}\n>\n> " + (reasoning or "").strip().replace("\n", "\n> ")


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

# 발언 초안 저장 (ADR-025). 도구 루프가 한 판을 마칠 때마다 이어 갈 수 있는 상태를 넘깁니다.
# 받는 쪽(`OrchestratorEngine._speak`)이 DB 에 남기고, 서버가 다시 뜨면 그 상태로 이어 갑니다.
SpeechCheckpoint = Callable[[Dict[str, Any]], Awaitable[None]]

# 초안의 형식. 필드를 바꾸면 올립니다 — 모르는 형식의 초안은 이어 가지 않고 발언을 처음부터 합니다.
SPEECH_STATE_VERSION = 1

# 도구 단위로 이어 가는 발언의 첫 판 앞에 붙는 안내.
RESUMED_SPEECH_NOTICE = (
    "[재개] 서버가 다시 시작되어 이 발언이 잠시 끊겼다가 이어집니다. 위 대화는 끊기기 전까지의 "
    "진행 그대로입니다 — 앞서 쓴 글을 되풀이하거나 이미 결과를 받은 도구를 다시 실행하지 말고, "
    "멈춘 자리에서 이어 가세요."
)

# 끊긴 순간 실행 중이던 도구 호출에 채우는 결과. 짝 없는 tool_call 을 남기면 다음 요청이 400 입니다.
UNKNOWN_TOOL_RESULT = (
    "[결과 모름] 서버가 다시 시작되어 이 도구 호출의 결과를 받지 못했습니다. 실행되었는지 알 수 "
    "없습니다. 결과가 필요하면(특히 파일 쓰기·명령 실행) 먼저 지금 상태를 확인한 뒤 다시 호출할지 "
    "정하세요."
)


def close_open_tool_calls(messages: List[Dict[str, Any]]) -> List[str]:
    """마지막 assistant 발언이 부른 도구 중 결과가 없는 것에 "결과 모름" 을 채웁니다. 채운 도구 이름.

    도구를 부른 발언과 그 결과는 짝이 맞아야 합니다 (`_assistant_turn`). 서버가 도구를 실행하는
    도중에 내려가면 부른 기록만 있고 결과가 없는 호출이 남습니다. 그 자리를 비워 두면 요청이
    거절되고, 지어낸 결과를 넣으면 모델이 그것을 사실로 읽습니다. 모른다고 적습니다.
    """
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls") or []
        if not calls:
            return []
        answered = {
            m.get("tool_call_id") for m in messages[index + 1:] if m.get("role") == "tool"
        }
        missing: List[str] = []
        for call in calls:
            call_id = call.get("id")
            if call_id in answered:
                continue
            name = ((call.get("function") or {}).get("name")) or "unknown_tool"
            messages.append({
                "role": "tool", "tool_call_id": call_id, "name": name, "content": UNKNOWN_TOOL_RESULT,
            })
            missing.append(name)
        return missing
    return []


BUDGET_EXTENDED_INSTRUCTION = (
    "[도구 호출 예산 확장] 유저가 상한을 {extra}회 늘려 총 {limit}회가 되었습니다 "
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
# `write_workspace_file` 은 샌드박스 서버의 쓰기 도구입니다. 빠져 있던 동안 `sandbox` 만
# 가진 에이전트는 분할 쓰기 지침(`file_writing_guidance`)을 한 줄도 받지 못했습니다.
FILE_WRITE_TOOLS = ("write_file", "write_text_file", "create_file", "write_workspace_file")

# pair_slide MCP 서버의 진입 도구들. 바이너리 문서를 만들 수 있는 유일한 길이라,
# 프롬프트에서 이름을 그대로 짚어 줍니다.
SLIDE_TOOLS = ("slide_open", "slide_add", "slide_export")
SHEET_TOOLS = ("sheet_write_table",)
# 사람이 화면에서 슬라이드에 적어 둔 부탁을 읽고 닫는 도구. 진입 도구와 따로 찾는
# 이유는 **없어도 발표자료는 만들어지기** 때문입니다 — 있을 때만 한 줄을 더합니다.
SLIDE_COMMENT_TOOLS = ("slide_comments",)
SLIDE_RESOLVE_TOOLS = ("slide_resolve_comment",)


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


def is_file_writing_call(name: str) -> bool:
    """이 호출이 파일에 내용을 쓰는 도구인가 (덮어쓰기든 덧붙이기든)."""
    tail = (name or "").split("__", 1)[-1]
    return tail in FILE_WRITE_TOOLS or tail in APPEND_TOOLS


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
    있는 쪽으로 밀려나, 도구를 많이 부르는 긴 발언에서 장부와 지시(전략 지침·병렬 과업·요지
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


CONTEXT_WIDENED_INSTRUCTION = (
    "[컨텍스트 확장] 유저가 이 발언의 컨텍스트 한도를 {extra:,} 토큰 늘려 "
    "총 {window:,} 토큰이 되었습니다. 앞선 기록이 생략되지 않고 그대로 남았습니다."
)

TRUNCATED_TOOL_CALL_NOTICE = (
    "[출력 잘림] 직전 도구 호출은 응답 한도(max_tokens={max_tokens})에 걸려 "
    "**인자를 끝까지 쓰지 못한 채 잘렸습니다.** 그래서 그 호출은 의도한 대로 "
    "실행되지 않았습니다 (도구 서버가 인자를 읽지 못했다고 답했을 것입니다).\n"
    "같은 내용을 그대로 다시 보내지 마세요 — 또 같은 자리에서 잘립니다.{advice}"
)

# ---------------------------------------------------------------- 사고만 하다 한도에 닿음
#
# 추론 모델(reasoning_content 를 따로 주는 모델)은 속으로 생각하는 토큰도 `max_tokens` 에
# 넣어 셉니다. 생각이 길면 본문을 한 글자도 못 쓰고 `length` 로 끝납니다. 예전에는 그
# 발언에 "잘렸습니다" 꼬리표만 남았습니다 — 이어받을 글이 없었으니까요.

# 답을 다시 요청할 때. 사고를 처음부터 다시 하면 또 같은 자리에서 끝나므로, 직전 사고의
# 끝부분을 건네고 거기서 결론만 쓰라고 합니다.
ANSWER_AFTER_REASONING_INSTRUCTION = (
    "[답변 없음] 직전 응답은 사고(reasoning)에 응답 한도(max_tokens={max_tokens})를 모두 써서 "
    "**본문을 한 글자도 쓰지 못했습니다.** 사고는 이미 충분히 했습니다. 처음부터 다시 "
    "생각하지 말고, 곧바로 최종 답변 본문을 쓰세요. 사고는 최소한으로 줄이세요."
)
# 한도와 무관하게, 답이 사고 안에만 있고 본문이 빈 채로 **정상 종료**했을 때.
#
# vLLM 의 reasoning parser(Qwen3·DeepSeek-R1 등)는 모델이 사고 종료 표식을 내지 않거나 답을
# 사고 블록 안에 쓰면 출력 전체를 `reasoning_content` 로 분류합니다. `finish_reason` 은
# `stop` 이라 v0.7.0 의 한도 복구에도 걸리지 않았고, `prompt` 모드는 사고를 버리므로 발언이
# 로그 한 줄 없이 **빈 카드**로 남았습니다.
ANSWER_ONLY_IN_REASONING_INSTRUCTION = (
    "[답변 없음] 직전 응답은 끝까지 쓰였지만 **답이 사고(reasoning) 안에만 있고 본문이 "
    "비어** 있었습니다. 유저에게는 본문만 보입니다. 사고를 다시 하지 말고, 최종 답변을 "
    "본문으로 쓰세요."
)
ANSWER_ONLY_IN_REASONING_FOOTER = (
    "> ⚠️ **모델이 답을 사고(reasoning) 안에만 쓰고 본문을 비워 두었습니다.** 답을 본문으로 "
    "다시 요청해도 오지 않았습니다. 서버의 reasoning parser 가 이 모델의 사고 종료 표식과 맞지 "
    "않을 수 있습니다."
)
REASONING_CARRY_NOTE = "\n\n[직전 사고의 끝부분 — 여기서 결론만 이어 쓰세요]\n{reasoning}"
# 건네는 사고의 최대 길이(글자). 통째로 넣으면 그것이 다시 한도를 먹습니다.
REASONING_CARRY_CHARS = 2000
ANSWER_NOW_AGAIN = "\n\n[다시 요청] 이번에도 본문이 비었습니다. 사고 없이 결론부터 짧게 쓰세요."
# 이어받기 호출마저 사고에 한도를 다 썼을 때 한 번 더 부탁하는 말.
CONTINUE_WITHOUT_REASONING = (
    "\n\n[다시 요청] 직전 이어받기는 사고에 한도를 다 써서 글이 오지 않았습니다. "
    "사고 없이 곧바로 이어 쓰세요."
)

# ---------------------------------------------------------------- 본문으로 새어 나온 도구 호출
#
# 서버의 도구 파서가 모델이 만든 호출을 해석하지 못하면, 호출 표식이 `tool_calls` 가
# 아니라 **본문 글자로** 흘러나옵니다 (호출이 한도에 걸려 잘렸거나, 파서가 그 모델의
# 형식과 맞지 않을 때). 예전에는 그것을 평범한 답변으로 받았습니다. 도구는 실행되지 않았고,
# 잘렸으면 이어받기가 **호출 표식을 산문처럼 이어 썼고**, 카드에는 표식이 그대로 남았습니다.
#
# 표식은 모델 계열마다 다릅니다. 여기 있는 것은 널리 쓰이는 형식이고, 새 형식은 이 목록에
# 더하면 됩니다. 산문에서 우연히 나올 수 있는 모양은 넣지 않습니다.
LEAKED_TOOL_CALL_MARKERS = (
    re.compile(r"<tool_call>\s*[\[{]"),             # Hermes · Qwen 2.5/3 계열
    re.compile(r"\[TOOL_CALLS\]"),                   # Mistral
    re.compile(r"<\|python_tag\|>"),                 # Llama 3.1 내장 도구
    re.compile(r"<function=[\w.\-]+>\s*\{"),         # Llama 3.x 사용자 정의 함수 형식
    re.compile(r"<｜tool▁calls▁begin｜>"),            # DeepSeek V3 · R1
    re.compile(r"to=functions\.[\w.\-]+"),           # gpt-oss (harmony)
)
_FENCED_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)

# 새어 나온 호출을 모델에게 알리고 다시 부르게 하는 횟수. 같은 일이 되풀이되면 서버
# 파서와 모델 형식이 맞지 않는 것이라, 무한히 다시 시켜도 소용이 없습니다.
MAX_LEAKED_TOOL_CALL_RETRIES = 2

LEAKED_TOOL_CALL_NOTICE = (
    "[도구 호출 실패] 직전 응답에서 도구 호출이 **본문 글자로** 나왔습니다. 서버가 그것을 "
    "도구 호출로 해석하지 못해 **실행되지 않았습니다.**{cause}\n"
    "도구가 필요하면 정식 도구 호출로 다시 부르세요. 호출 표식을 본문에 직접 쓰지 마세요."
)
# 새어 나온 호출만 있던 판을 대화에 되돌려 넣을 때의 자리표시. 빈 assistant 본문은
# 몇몇 엔드포인트가 거절합니다.
LEAKED_TOOL_CALL_PLACEHOLDER = "(도구를 호출하려 했지만 호출 표식이 본문으로 나와 실행되지 않았습니다.)"
LEAKED_TOOL_CALL_FOOTER = (
    "> ⚠️ **도구 호출이 본문 글자로 새어 나와 실행하지 못했습니다.** 서버가 모델의 호출 표식을 "
    "도구 호출로 해석하지 못했습니다 — 응답 한도에 걸려 호출이 잘렸거나, 서버의 도구 파서가 이 "
    "모델의 형식과 맞지 않을 수 있습니다. 새어 나온 표식은 이 발언에서 지웠습니다."
)
REASONING_EXHAUSTED_FOOTER = (
    "> ⚠️ **이 발언은 사고(reasoning)에 응답 한도(max_tokens={max_tokens})를 모두 써서 본문을 "
    "받지 못했습니다.** 답을 다시 요청해도 본문이 오지 않았습니다. `max_tokens` 를 올리거나, "
    "추론 강도를 낮추거나, 요구 범위를 좁혀 다시 물어보세요."
)


def find_leaked_tool_call(text: str) -> Optional[int]:
    """본문에 새어 나온 도구 호출 표식이 시작하는 위치. 없으면 None.

    코드 블록 안은 보지 않습니다 — 도구 호출 형식을 **설명하는** 답변이 그것을 예시로
    적는 것은 정상입니다. 표식 앞에 같은 줄의 특수 토큰(`<|start|>...` 따위)이 붙어
    있으면 그 자리부터로 잡아, 남기는 본문에 부스러기가 섞이지 않게 합니다.
    """
    if not text:
        return None
    fences = [(m.start(), m.end()) for m in _FENCED_BLOCK.finditer(text)]
    earliest: Optional[int] = None
    for pattern in LEAKED_TOOL_CALL_MARKERS:
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in fences):
                continue
            if earliest is None or match.start() < earliest:
                earliest = match.start()
            break
    if earliest is None:
        return None
    line_start = text.rfind("\n", 0, earliest) + 1
    special = text.find("<|", line_start, earliest)
    return special if special != -1 else earliest


def leaked_tool_call_notice(
    max_tokens: str, finish_reason: str, tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """새어 나온 호출을 모델에게 알리는 말. 한도에 걸린 것이면 그 사실과 대처를 붙입니다."""
    cause = ""
    if finish_reason == "length":
        cause = (
            f" 응답 한도(max_tokens={max_tokens})에 걸려 호출이 중간에 잘린 탓일 가능성이 "
            f"큽니다 — 같은 호출을 그대로 다시 보내면 또 잘립니다." + truncation_advice(tools)
        )
    return LEAKED_TOOL_CALL_NOTICE.format(cause=cause)


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


def slide_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """이 발언이 쓸 수 있는 발표자료 생성 도구의 이름. 없으면 None."""
    return _find_tool(tools, SLIDE_TOOLS)


def sheet_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """이 발언이 쓸 수 있는 스프레드시트 생성 도구의 이름. 없으면 None."""
    return _find_tool(tools, SHEET_TOOLS)


BINARY_DOC_HEAD = (
    "[바이너리 문서(.pptx / .xlsx / .docx / .pdf)]\n"
    ".pptx 와 .xlsx 는 XML 을 담은 ZIP 컨테이너입니다. 텍스트가 아니므로 파일 쓰기 "
    "도구로는 만들 수 없습니다 — 그렇게 쓴 파일은 PowerPoint / Excel 이 열지 못합니다."
)


def binary_file_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """바이너리 문서를 어떻게 만들(거나 만들지 말) 것인가. 해당 없으면 None.

    `file_writing_guidance` 와 같은 규율입니다 — 도구를 이름 꼬리로 찾고, 실제로 가진
    도구의 **전체 이름**을 짚어 주며, 아무것도 해당하지 않으면 None 이라 프롬프트가
    한 글자도 늘지 않습니다.

    갈래가 셋인 이유:

    - 발표자료 · 스프레드시트 도구가 있으면 그 이름과 순서를 알려 줍니다.
    - 쓰기 도구만 있으면 **못 만든다고 사실대로 말하라**고 시킵니다. 이 갈래가 없으면
      모델은 `.pptx` 를 텍스트로 써 놓고 만들었다고 보고합니다. 원래의 버그입니다.
    - 둘 다 가졌으면 앞의 것에 "쓰기 도구로 쓰지 말라" 한 줄을 더합니다. 도구가
      둘 다 보이면 익숙한 쪽으로 손이 가기 때문입니다.
    """
    deck, sheet = slide_tool(tools), sheet_tool(tools)
    comments = _find_tool(tools, SLIDE_COMMENT_TOOLS)
    resolve = _find_tool(tools, SLIDE_RESOLVE_TOOLS)
    writer = file_write_tool(tools)
    if not deck and not sheet and not writer:
        return None

    lines = []
    if deck or sheet:
        if deck:
            lines.append(
                f"- 발표자료는 `{deck}` 로 시작합니다. 첫 인자 `name` 이 문서 이름이고, "
                f"그 뒤의 모든 호출에서 **같은 값**을 씁니다."
            )
            lines.append(
                "- 문서는 서버에 살아 있고 유저가 브라우저에서 같이 고칩니다. 열면 "
                "화면 주소가 돌아오니 **유저에게 그 주소를 알려 주세요.** 고치기 전에는 "
                "반드시 읽어서 최신 `rev` 를 받고, 쓸 때 그 값을 함께 보냅니다."
            )
            lines.append(
                "- 슬라이드는 한 호출에 한 장씩 쌓습니다. 내보내기 도구는 파일을 "
                "**다시 열어** 실제 내용을 보고하니, 그 판정을 읽기 전에는 완료했다고 "
                "말하지 마세요."
            )
        if comments:
            # 코멘트는 사람 → 에이전트 통로입니다. 읽으라고 시키지 않으면 사람은
            # 대답 없는 곳에 계속 적게 되고, 기능이 있다는 사실만 남습니다.
            tail = f" 처리한 것은 `{resolve}` 로 표시하세요." if resolve else ""
            lines.append(
                f"- 유저가 화면에서 슬라이드에 부탁을 적어 둘 수 있습니다. 고치기 전에 "
                f"`{comments}` 로 읽고 **그것부터 처리하세요** — 유저가 직접 짚은 것이라 "
                f"당신이 짐작한 개선보다 우선합니다.{tail}"
            )
        if sheet:
            lines.append(f"- 스프레드시트는 `{sheet}` 에 열 이름과 행 데이터를 넘겨 만듭니다.")
        if writer:
            lines.append(
                f"- `{writer}` 로 .pptx / .xlsx 를 쓰려는 시도는 거부됩니다. 시도하지 마세요."
            )
    else:
        lines.append(
            f"- 이 환경에는 그런 파일을 만들 수 있는 도구가 없습니다. `{writer}` 로 "
            f"마크다운(.md)이나 CSV 로 쓰고, 요청한 형식의 파일은 만들지 못했다고 "
            f"유저에게 그대로 알리세요. 만들었다고 말하지 마세요."
        )

    return BINARY_DOC_HEAD + "\n" + "\n".join(lines)


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


def _message_text(message: Any) -> str:
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
    output = reasoning + len(_message_text(message))
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
    text_chars = len(_message_text(message))
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


# 발언이 응답 한도에서 끊겼음을 기록과 화면에 남깁니다.
#
# `TRUNCATED_TOOL_CALL_NOTICE` 는 **모델에게** 하는 말이고(다음 판에서 만회하라),
# 이쪽은 **사람에게** 하는 말입니다. 도구 호출이 잘리면 도구 서버가 거절해 주지만,
# 그냥 긴 글이 잘리면 아무도 이의를 제기하지 않습니다 — 문장 중간에서 끝난 발언이
# 그대로 저장되고, 읽는 사람은 그것이 잘린 것인지 원래 그렇게 끝난 것인지 알 수
# 없습니다. 다음 발언자와 최종 합성도 마찬가지고요.
TRUNCATED_ANSWER_FOOTER = (
    "> ⚠️ **응답 한도(max_tokens={max_tokens})에 걸려 이 발언은 여기서 잘렸습니다.** "
    "끝맺지 못한 문장이나 닫히지 않은 코드 블록이 있을 수 있습니다. "
    "이어서 받으려면 남은 부분을 다시 요청하거나, 에이전트 설정의 `max_tokens` 를 올리세요."
)

# 이어받기를 다 쓰고도 끝나지 않았을 때. 위와 달리 "이미 N번 이어받았다" 를 밝힙니다 —
# 그래야 사람이 `max_continuations` 를 더 올릴지, `max_tokens` 를 올릴지, 아니면 애초에
# 요구 범위를 좁힐지 판단할 수 있습니다.
CONTINUED_BUT_STILL_TRUNCATED_FOOTER = (
    "> ⚠️ **이어받기 {used}회를 모두 쓰고도 끝나지 않아 여기서 멈췄습니다** "
    "(응답 한도 max_tokens={max_tokens}). 끝맺지 못한 문장이나 닫히지 않은 코드 블록이 "
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
        """System prompt = persona + sequential thinking + file/skill rules + session instructions.

        세션 지침을 가장 마지막에 배치하는 이유는 해당 지시가 가장 구체적이기 때문입니다. 파일 작성 및 스킬 관련
        지침을 그 앞쪽에 배치하여, 사용자가 세션 지침으로 다른 방식을 지정하면 해당 지시가 우선하도록 구성합니다.

        결정 장부는 여기 넣지 않습니다. 라운드마다 바뀌는 글이 시스템 프롬프트에 있으면
        그 뒤 전체가 프롬프트 캐시에서 빠집니다 (`place_ledger_last`).
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

        binary = binary_file_guidance(tools)
        if binary:
            parts.append(binary)

        skills = skill_guidance(tools)
        if skills:
            parts.append(skills)

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
        ledger: str = "",
        tool_gate: Optional[Any] = None,
        checkpoint: Optional[SpeechCheckpoint] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        preload_skills: Sequence[str] = (),
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Executes a turn for the given agent.
        Returns (response_text, tool_call_logs).

        `preload_skills` 는 유저가 이 에이전트에게 지정한 스킬입니다 (입력창의 `@전문가 @스킬`).
        첫 LLM 호출 전에 호스트가 `load_skill` 을 대신 불러 결과를 넣어 둡니다 (`_preload_skills`).
        이어 가는 발언에서는 이미 메시지에 들어 있으므로 다시 부르지 않습니다.

        `checkpoint` 는 도구 루프가 한 판을 마칠 때마다(도구를 부른 직후, 도구 결과를 받을 때마다)
        이어 갈 수 있는 상태를 넘겨받는 콜백입니다 (ADR-025). `resume_state` 는 그렇게 남긴 상태로,
        주면 프롬프트를 새로 만들지 않고 그 상태의 메시지에서 다음 LLM 호출부터 이어 갑니다.

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

        `ledger` 는 이 대화의 결정 장부입니다. 마지막 사용자 메시지 앞에 붙습니다
        (`place_ledger_last`) — 시스템 프롬프트에 두면 갱신될 때마다 캐시가 깨집니다.

        `tool_gate` 는 도구 보안 문지기입니다 (`app/orchestration/tool_gate.py`). 늘 거부될
        도구를 목록에서 빼고, 호출마다 허용·묻기·거부를 판정합니다. 주지 않으면 판정 없이
        실행하고, 매니저의 고정 보호만 걸립니다.
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
        if tool_gate is not None:
            tools = tool_gate.filter_tools(agent.key, tools, mcp)
        # 스킬 도구는 발언이 진행될 때마다 스킬 디렉터리와 활성화 여부를 새로 조회하여 동적으로 생성합니다 (`app/agents/skills.py`).
        tools = tools + skill_tools_for(agent)

        if resume_state is not None:
            # 끊긴 발언을 이어 갑니다. 모델이 보던 메시지를 그대로 씁니다 — 프롬프트를 다시 만들면
            # 그 사이 바뀐 장부·요약이 섞여, 모델이 보던 것과 다른 대화가 됩니다.
            formatted_messages = [dict(m) for m in resume_state.get("messages") or []]
            turn_anchor = str(resume_state.get("turn_anchor") or "")
        else:
            formatted_messages = [
                {"role": "system",
                 "content": self.build_system_prompt(agent, custom_instructions, tools)}
            ]
            prompt = place_ledger_last(messages, ledger)
            formatted_messages.extend(prompt)
            # 발언을 시작할 때의 마지막 사용자 메시지(장부 + 이번 차례 지시). 도구 루프가 넘쳐
            # 이것을 버리게 되면 다시 붙입니다 (`fit_tool_loop_context` 의 `keep`).
            turn_anchor = (
                prompt[-1]["content"]
                if prompt and prompt[-1].get("role") == "user" and isinstance(prompt[-1].get("content"), str)
                else ""
            )

            # 순서 주의: 먼저 한도에 맞춰 자르고, 그 다음 role 을 합칩니다. 생략 안내가
            # user 로 들어가므로 합치기를 나중에 해야 교대가 보장됩니다.
            formatted_messages, trimmed = fit_context_window(
                agent, formatted_messages, memory_search_tool(tools), tools=tools,
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
                mcp=mcp, turn_anchor=turn_anchor, tool_gate=tool_gate,
                checkpoint=checkpoint, resume_state=resume_state, preload_skills=preload_skills,
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
            "max_tokens": effective_max_tokens(agent),
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

        서버 전체의 동시 요청 상한(`app.llm_concurrency`)이 있으면 자리가 날 때까지
        여기서 기다립니다. 스트리밍이 끝날 때까지 자리를 쥡니다 (`app/agents/llm_gate.py`).
        """
        async with get_llm_gate().slot():
            return await self._complete_unthrottled(agent, messages, tools, on_chunk, tool_choice)

    async def _complete_unthrottled(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        on_chunk: Optional[Callable[[str], Any]],
        tool_choice: str,
    ) -> Tuple[Any, str]:
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
        *,
        last_text: Optional[str] = None,
        reasoning: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
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

        `last_text` 는 **한도에 닿은 그 응답**의 본문입니다. 비어 있으면 이어받을 글이
        없습니다 — 추론 모델이 속으로 생각하는 데 한도를 다 쓴 경우입니다. 예전에는 그때도
        `segments[-1]` 을 이어받았는데, 도구를 부른 발언이면 그것은 **앞선 판의 글**이라
        엉뚱한 자리에 이어 붙였고, 도구를 안 부른 발언이면 목록이 비어 꼬리표만 남았습니다.
        그 경우는 `_recover_empty_answer` 가 답을 다시 받아 옵니다.

        `tools` 는 넘기되 **부르지 못하게** 합니다 (`tool_choice="none"`). 이어받기는 도구를
        쓴 뒤의 대화에서도 일어나는데, Anthropic 은 tool_use/tool_result 가 든 대화를
        `tools` 없이 보내면 400 으로 거절합니다 — `_wrap_up_without_tools` 가 겪은 것과
        같습니다. 예전에는 도구 없이 불러, claude 계열에서 도구를 쓴 발언의 이어받기가
        조용히 실패했습니다.
        """
        if finish_reason != "length":
            return
        label = max_tokens_label(agent)
        if last_text is not None and not last_text.strip():
            await self._recover_empty_answer(agent, messages, segments, on_chunk, reasoning, tools)
            return
        if not segments or agent.max_continuations <= 0:
            segments.append(TRUNCATED_ANSWER_FOOTER.format(max_tokens=label))
            return

        used = 0
        nudged = False
        for _ in range(agent.max_continuations):
            # 매번 새로 조립합니다. 예전에는 대화에 이어 붙여, 부를 때마다 지금까지의 글
            # 전체가 assistant 턴으로 한 벌씩 더 쌓였습니다.
            convo = list(messages) + [
                {"role": "assistant", "content": segments[-1]},
                {"role": "user", "content": CONTINUE_ANSWER_INSTRUCTION
                    + (CONTINUE_WITHOUT_REASONING if nudged else "")},
            ]
            try:
                message, finish_reason = await self._complete_once(
                    agent, convo, tools, on_chunk, tool_choice="none" if tools else "auto",
                )
            except Exception as exc:  # noqa: BLE001 - 이어받기 실패로 앞의 글을 버리지 않습니다
                logger.warning(f"Continuation failed for {agent.name}: {type(exc).__name__}: {exc}")
                break

            piece = self._compose_content(agent, message)
            if not piece.strip():
                # 이어받기 호출도 사고에 한도를 다 썼을 수 있습니다. 한 번은 사고 없이
                # 이어 쓰라고 다시 부탁합니다 (남은 횟수 안에서).
                if getattr(message, "reasoning_content", None) and not nudged:
                    nudged = True
                    logger.warning(
                        f"Continuation for {agent.name} spent max_tokens on reasoning; "
                        f"asking once more without it"
                    )
                    continue
                logger.warning(f"Continuation returned nothing for {agent.name}; stopping")
                break

            used += 1
            segments[-1] = segments[-1] + piece      # 한 문장이 갈린 자리입니다
            if finish_reason != "length":
                logger.info(f"Continued a truncated answer for {agent.name}: {used} time(s)")
                return

        if used:
            segments.append(CONTINUED_BUT_STILL_TRUNCATED_FOOTER.format(used=used, max_tokens=label))
        else:
            segments.append(TRUNCATED_ANSWER_FOOTER.format(max_tokens=label))

    async def _recover_empty_answer(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        segments: List[str],
        on_chunk: Optional[Callable[[str], Any]],
        reasoning: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        cause: str = "length",
    ) -> bool:
        """본문이 빈 응답에서 답을 다시 받아 옵니다. 받았으면 True.

        원인이 둘이고, 모델에게 하는 말과 사람에게 남기는 꼬리표가 다릅니다.

        * `cause="length"` — 사고만 하다 응답 한도에 닿았습니다. 추론 모델은 속으로 생각한
          토큰도 `max_tokens` 에 넣어 셉니다.
        * `cause="stop"` — 끝까지 썼는데 답이 사고 안에만 있습니다 (`_answer_left_in_reasoning`).

        다시 부를 때 그냥 같은 질문을 던지면 같은 길이로 다시 생각하다 같은 자리에서
        끝납니다. 그래서 (1) 본문이 비었다는 사실과 원인을 알리고, (2) 직전 사고의 끝부분을
        건네 "여기서 결론만 쓰라" 고 하고, (3) 그래도 비면 사고 없이 결론부터 쓰라고 한 번
        더 부탁합니다. `max_continuations` 번까지만 부릅니다. 끝내 본문이 없으면 원인을
        밝히는 꼬리표를 남깁니다.

        "받았다" 는 **본문**을 받았다는 뜻입니다. `native` 모드는 사고를 인용 블록으로 본문
        앞에 붙이므로, 합친 글이 아니라 모델이 준 본문 자체를 봐야 합니다.
        """
        label = max_tokens_label(agent)
        if cause == "length":
            logger.warning(
                f"{agent.name} reached max_tokens with no answer text "
                f"(reasoning={len(reasoning or ''):,} chars, max_tokens={effective_max_tokens(agent)}); "
                f"asking for the answer"
            )
            instruction = ANSWER_AFTER_REASONING_INSTRUCTION.format(max_tokens=label)
            footer = REASONING_EXHAUSTED_FOOTER.format(max_tokens=label)
        else:
            logger.warning(
                f"{agent.name} finished with no answer text but {len(reasoning or ''):,} chars of "
                f"reasoning and no conclusion marker in it; asking for the answer"
            )
            instruction = ANSWER_ONLY_IN_REASONING_INSTRUCTION
            footer = ANSWER_ONLY_IN_REASONING_FOOTER

        if agent.max_continuations <= 0:
            segments.append(footer)
            return False

        tail = (reasoning or "").strip()[-REASONING_CARRY_CHARS:]
        if tail:
            instruction += REASONING_CARRY_NOTE.format(reasoning=tail)

        for attempt in range(agent.max_continuations):
            convo = list(messages)
            self._append_budget_notice(convo, instruction + (ANSWER_NOW_AGAIN if attempt else ""))
            try:
                message, finish_reason = await self._complete_once(
                    agent, convo, tools, on_chunk, tool_choice="none" if tools else "auto",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Answer recovery failed for {agent.name}: {type(exc).__name__}: {exc}")
                break

            answer = _message_text(message).strip()
            if answer:
                segments.append(self._compose_content(agent, message).strip())
                logger.info(
                    f"Recovered an answer for {agent.name} after an empty response "
                    f"({cause}, {attempt + 1} call(s))"
                )
                if finish_reason == "length":
                    # 되받은 답도 잘렸으면 평소처럼 이어받습니다.
                    await self._finish_truncated_answer(
                        agent, convo, segments, finish_reason, on_chunk,
                        last_text=answer, tools=tools,
                    )
                return True

        segments.append(footer)
        return False

    async def _answer_left_in_reasoning(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        segments: List[str],
        on_chunk: Optional[Callable[[str], Any]],
        reasoning: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        reasoning_shown: bool = False,
    ) -> None:
        """본문 없이 **정상 종료**했는데 사고(reasoning)에만 글이 있는 응답을 수습합니다.

        v0.7.0 은 본문이 빈 채 **한도에 닿은** 경우만 복구했습니다. 그런데 한도와 무관하게,
        서버의 reasoning parser 가 사고 종료 표식을 못 찾으면 출력 전체가 사고로 분류되고
        `finish_reason` 은 `stop` 입니다. `prompt` 모드는 사고를 버리므로 발언이 로그 한 줄
        없이 빈 카드로 남았습니다 — 사고 안에 `## 최종 결론` 이 멀쩡히 들어 있어도요.

        1. 사고에 결론 마커가 있으면, 서버가 분류를 틀렸을 뿐 답은 다 쓰인 것입니다. 그대로
           본문으로 옮깁니다 (추가 호출 없음). 원래 본문에 왔어야 할 글이므로 `show_steps`
           도 평소처럼 적용됩니다.
        2. 없으면 사고만 한 것이라 답을 다시 요청합니다 (`_recover_empty_answer`).
        3. 그래도 없으면 꼬리표로 알리고, `show_steps` 가 켜져 있으면 받은 사고를 인용 블록으로
           함께 남깁니다 — 빈 카드보다는 모델이 무엇을 생각했는지라도 보이는 편이 낫습니다.
           이미 `native` 모드가 사고를 본문 앞에 붙였다면(`reasoning_shown`) 다시 붙이지 않습니다.
        """
        if conclusion_from_reasoning(reasoning):
            logger.warning(
                f"{agent.name} returned its whole answer inside reasoning ({len(reasoning):,} chars) "
                f"with no answer text; the conclusion was found there and used as the answer"
            )
            segments.append(reasoning.strip())
            return

        recovered = await self._recover_empty_answer(
            agent, messages, segments, on_chunk, reasoning, tools, cause="stop",
        )
        if not recovered and not reasoning_shown and agent.sequential_thinking.show_steps:
            segments.insert(len(segments) - 1, reasoning_quote(reasoning))

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
        tools: Optional[List[Dict[str, Any]]] = None,
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
                "budget": context_budget(agent, window, tools),
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
            # 도구를 못 부르게 했는데도 호출 표식을 본문으로 쓰는 모델이 있습니다. 여기서는
            # 다시 부르게 할 수 없으니, 표식을 지우고 그 사실만 남깁니다.
            leak_at = find_leaked_tool_call(final) if tools else None
            if leak_at is not None:
                logger.warning(
                    f"Tool call leaked into the wrap-up answer of {agent.name}; it cannot run here"
                )
                final = final[:leak_at].rstrip()
            if final:
                segments.append(final)
            # 한도가 아닌데 본문이 없고 사고에만 글이 있으면, 답이 사고로 분류된 것입니다.
            answer_text = _message_text(message).strip()
            final_reasoning = getattr(message, "reasoning_content", None) or ""
            if (leak_at is None and finish_reason != "length"
                    and not answer_text and final_reasoning.strip()):
                await self._answer_left_in_reasoning(
                    agent, current_messages, segments, on_chunk, final_reasoning, tools,
                    reasoning_shown=bool(final),
                )
            # 마무리 발언도 한도에 걸릴 수 있습니다. 예산 소진 꼬리표와 둘 다 붙는
            # 것이 맞습니다 — 서로 다른 두 한도에 걸린 것이고, 사람이 올릴 손잡이도
            # 각각 다릅니다 (`max_tool_iterations` 와 `max_tokens`).
            if finish_reason == "length" and leak_at is None:
                logger.warning(
                    f"Truncated wrap-up answer from {agent.name}: "
                    f"max_tokens={effective_max_tokens(agent)}, "
                    + completion_budget_report(
                        message, None, None, estimate_tokens(agent.model, current_messages),
                        agent.max_context_window, tool_schema_tokens(agent.model, tools),
                    )
                )
                await self._finish_truncated_answer(
                    agent, current_messages, segments, finish_reason, on_chunk,
                    # 합친 글이 아니라 모델이 준 본문으로 판단합니다. `native` 모드는 사고를
                    # 인용 블록으로 앞에 붙이므로, 합친 글로 보면 빈 본문을 놓칩니다.
                    last_text=answer_text,
                    reasoning=final_reasoning,
                    tools=tools,
                )
            if leak_at is not None:
                segments.append(LEAKED_TOOL_CALL_FOOTER)
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

    @staticmethod
    async def _check_tool_gate(
        tool_gate: Optional[Any],
        agent: Agent,
        fn_name: str,
        fn_args: Dict[str, Any],
        mcp: Any,
    ) -> Optional[Any]:
        """도구 보안 판정. 문지기가 없으면 None (판정 없이 실행)."""
        if tool_gate is None:
            return None
        return await tool_gate.check(agent, fn_name, fn_args, mcp)

    async def _execute_tool_safely(
        self,
        agent: Agent,
        fn_name: str,
        fn_args: Dict[str, Any],
        session_id: Optional[str],
        mcp: Optional[MCPManager] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str]:
        """MCP 도구(또는 스킬 도구)를 호출하고, 실행 결과를 항상 (문자열, 상태) 튜플 형태로 반환합니다.

        `MCPManager.execute_tool`에서 예외를 대부분 처리하지만, 매니저 객체가 교체되거나
        도구 명칭 조회 과정에서 예외가 발생할 가능성이 있습니다. 개별 도구 호출 실패가
        전체 발언의 중단으로 이어져서는 안 된다는 원칙을 본 단계에서 재차 보장합니다.
        단, 작업 취소 예외는 상위로 그대로 전달합니다.

        스킬 도구는 외부 MCP 서버가 아닌 애플리케이션 프로세스 내부에서 직접 실행합니다 (`app/agents/skills.py`).
        스크립트가 복사될 작업 공간은 해당 발언이 사용하는 런타임의 작업 공간이며, 실행 가능 여부는 해당 발언에
        실제로 부여된 도구 목록(`tools`)을 기준으로 판정합니다.
        """
        runtime = mcp or self.mcp_manager
        try:
            if is_skill_tool(fn_name):
                output, status = await run_skill_tool(
                    agent, fn_name, fn_args,
                    workspace=getattr(runtime, "workspace", None), tools=tools,
                )
            else:
                output, status = await runtime.execute_tool(
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

    async def _preload_skills(
        self,
        agent: Agent,
        names: Sequence[str],
        messages: List[Dict[str, Any]],
        tool_logs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]],
        session_id: Optional[str],
        mcp: Optional[MCPManager],
    ) -> bool:
        """지정한 스킬의 `load_skill` 을 모델 대신 불러, 부른 기록과 결과를 `messages` 끝에 붙입니다.

        모델이 직접 부른 것과 같은 모양입니다 — 도구를 부른 assistant 발언 하나와 그 결과들. 그래서
        화면에도 도구 카드로 남고, 이어 가기와 컨텍스트 자르기도 다른 호출과 똑같이 다룹니다. 이번
        발언의 도구 목록에 없는 스킬(그사이 꺼졌거나 지워진 것)은 건너뜁니다 — 내놓지 않은 도구를
        부른 기록을 만들면 요청이 거절될 수 있습니다. 하나라도 불렀으면 True.
        """
        offered = set(offered_skills(tools))
        wanted = [name for name in dict.fromkeys(names) if name in offered]
        skipped = [name for name in dict.fromkeys(names) if name not in offered]
        if skipped:
            logger.info(f"Designated skill(s) not available to {agent.name} now: {', '.join(skipped)}")
        if not wanted:
            return False

        calls = [
            (name, {"skill": name}, f"call_{uuid.uuid4().hex[:12]}")
            for name in wanted
        ]
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": LOAD_SKILL_TOOL, "arguments": json.dumps(args, ensure_ascii=False)},
                }
                for _name, args, call_id in calls
            ],
        })
        for _name, args, call_id in calls:
            output, status = await self._execute_tool_safely(
                agent, LOAD_SKILL_TOOL, args, session_id, mcp, tools=tools,
            )
            if status == "success":
                output = f"{DESIGNATED_SKILL_NOTE}\n\n{output}"
            call_log = {
                "tool_name": LOAD_SKILL_TOOL,
                "arguments": args,
                "output": output,
                "status": status,
                "security": {},
            }
            tool_logs.append(call_log)
            await self._notify_tool_call(agent, on_tool_call, call_log)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": LOAD_SKILL_TOOL,
                "content": output,
            })
        return True

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
        turn_anchor: str = "",
        tool_gate: Optional[Any] = None,
        checkpoint: Optional[SpeechCheckpoint] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        preload_skills: Sequence[str] = (),
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
        # 도구 정의는 요청마다 나가는 입력이라 컨텍스트 예산에서 뺍니다 (`context_budget`).
        schema_tokens = tool_schema_tokens(agent.model, tools)
        # 호출 표식이 본문으로 새어 나와 다시 부르게 한 횟수 (`MAX_LEAKED_TOOL_CALL_RETRIES`).
        leaked_retries = 0

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

        # 끊긴 발언을 이어 가면 판 단위로 남긴 상태에서 시작합니다 (ADR-025). 끊긴 순간 실행 중이던
        # 도구에는 "결과 모름" 을 채우고, 이어진다는 사실을 모델에게 알립니다.
        if resume_state is not None:
            current_messages = [dict(m) for m in resume_state.get("messages") or current_messages]
            segments = list(resume_state.get("segments") or [])
            used = int(resume_state.get("used") or 0)
            limit = max(limit, int(resume_state.get("limit") or 0))
            window = int(resume_state.get("window") or window)
            tool_logs = [dict(log) for log in resume_state.get("tool_logs") or []]
            leaked_retries = int(resume_state.get("leaked_retries") or 0)
            context_asked = bool(resume_state.get("context_asked"))
            announced_bands = set(resume_state.get("announced_bands") or [])
            unknown = close_open_tool_calls(current_messages)
            notice = RESUMED_SPEECH_NOTICE
            if unknown:
                notice += f" 결과를 받지 못한 호출: {', '.join(unknown)}."
            self._append_budget_notice(current_messages, notice)

        async def save_progress() -> None:
            """지금까지를 이어 갈 수 있게 넘깁니다. 넘기다 실패해도 발언은 계속됩니다."""
            if checkpoint is None:
                return
            try:
                await checkpoint({
                    "version": SPEECH_STATE_VERSION,
                    "messages": current_messages,
                    "segments": segments,
                    "used": used,
                    "limit": limit,
                    "window": window,
                    "tool_logs": tool_logs,
                    "leaked_retries": leaked_retries,
                    "context_asked": context_asked,
                    "announced_bands": sorted(announced_bands, key=str),
                    "turn_anchor": turn_anchor,
                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Could not save the progress of {agent.name}'s speech: {exc}")

        # 유저가 지정한 스킬을 첫 판 전에 불러 둡니다. 이어 가는 발언은 이미 들어 있습니다.
        if resume_state is None and preload_skills:
            if await self._preload_skills(
                agent, preload_skills, current_messages, tool_logs, tools,
                on_tool_call=on_tool_call, session_id=session_id, mcp=mcp,
            ):
                await save_progress()

        while True:
            while used < limit:
                # --- 컨텍스트: 넘치면 짝 단위로 덜어내고, 버릴 것이 생기면 물어봅니다.
                budget = context_budget(agent, window, tools)
                over = budget > 0 and estimate_tokens(agent.model, current_messages) > budget

                if over and context_arbiter is not None and not context_asked:
                    context_asked = True
                    answer = await self._ask_context(
                        context_arbiter, agent, window, current_messages, len(tool_logs), tools
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
                        budget = context_budget(agent, window, tools)
                        over = budget > 0 and estimate_tokens(agent.model, current_messages) > budget
                    elif answer.get("wrap_up"):
                        # 넓히지 않고 여기서 접겠다는 뜻입니다. 마무리 호출도 넘친
                        # 메시지로 나가면 400 이므로 먼저 창 안에 맞춥니다.
                        current_messages, dropped = fit_tool_loop_context(
                            agent, current_messages, window, memory_search, tools=tools,
                            keep=turn_anchor,
                        )
                        if dropped and on_context_trim:
                            on_context_trim(dropped)
                        return await self._wrap_up_without_tools(
                            agent, current_messages, segments, tool_logs, limit, on_chunk,
                            tools=tools,
                        )

                if over:
                    current_messages, dropped = fit_tool_loop_context(
                        agent, current_messages, window, memory_search, tools=tools,
                        keep=turn_anchor,
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
                        budget=context_budget(agent, window, tools),
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
                    # 도구 호출이 본문 글자로 새어 나왔는지 먼저 봅니다. 그렇다면 이것은
                    # 답변이 아니라 **실행되지 못한 호출**입니다. 평범한 답으로 받으면 도구는
                    # 돌지 않고, 잘렸을 때는 이어받기가 호출 표식을 산문처럼 이어 씁니다.
                    leak_at = find_leaked_tool_call(segment) if tools else None
                    if leak_at is not None:
                        kept = segment[:leak_at].strip()
                        if segment.strip():
                            segments.pop()          # 방금 넣은 이 판의 글을 표식 앞까지로 바꿉니다
                        if kept:
                            segments.append(kept)
                        logger.warning(
                            f"Tool call leaked into the text of {agent.name}'s response and was "
                            f"not executed: finish_reason={finish_reason!r}, "
                            f"max_tokens={effective_max_tokens(agent)}, "
                            + completion_budget_report(
                                message, None, None,
                                estimate_tokens(agent.model, current_messages), window, schema_tokens,
                            )
                        )
                        if leaked_retries < MAX_LEAKED_TOOL_CALL_RETRIES:
                            leaked_retries += 1
                            current_messages.append(
                                {"role": "assistant", "content": kept or LEAKED_TOOL_CALL_PLACEHOLDER}
                            )
                            self._append_budget_notice(
                                current_messages,
                                leaked_tool_call_notice(max_tokens_label(agent), finish_reason, tools),
                            )
                            continue
                        segments.append(LEAKED_TOOL_CALL_FOOTER)
                        return "\n\n".join(segments), tool_logs

                    answer_text = _message_text(message).strip()
                    reasoning = getattr(message, "reasoning_content", None) or ""

                    # 한도에 닿지 않았는데 본문이 없고 사고에만 글이 있으면, 서버가 답까지
                    # 사고로 분류한 것입니다. 그대로 두면 빈 카드가 됩니다.
                    if finish_reason != "length" and not answer_text and reasoning.strip():
                        await self._answer_left_in_reasoning(
                            agent, current_messages, segments, on_chunk, reasoning, tools,
                            reasoning_shown=bool(segment.strip()),
                        )
                        return "\n\n".join(segments), tool_logs

                    # 도구를 부르지 않고 끝난 판입니다. 여기서 잘렸다면 이의를
                    # 제기해 줄 도구 서버가 없으므로 우리가 표시를 남깁니다.
                    if finish_reason == "length":
                        logger.warning(
                            f"Truncated answer from {agent.name}: finish_reason='length', "
                            f"max_tokens={effective_max_tokens(agent)}, "
                            + completion_budget_report(
                                message, None, None,
                                estimate_tokens(agent.model, current_messages), window, schema_tokens,
                            )
                        )
                    await self._finish_truncated_answer(
                        agent, current_messages, segments, finish_reason, on_chunk,
                        # 합친 글이 아니라 모델이 준 본문으로 판단합니다 (위 마무리와 같은 이유).
                        last_text=answer_text,
                        reasoning=reasoning,
                        tools=tools,
                    )
                    return "\n\n".join(segments), tool_logs

                # 실행하기 전에 먼저 다 풀어 둡니다. 되돌려 보낼 발언이 **실제로
                # 실행한 인자와 같은 모양**이어야 하기 때문입니다 (`_assistant_turn`).
                parsed = [self._parse_tool_call(tc) for tc in tool_calls]
                # 두 가지를 구분합니다. 예전에는 둘을 하나로 묶어, 인자가 멀쩡히 읽혀
                # 실행까지 된 호출에도 "잘려서 실행되지 않았다" 고 알렸습니다.
                #
                # * 인자를 못 읽었다 — 호출이 실제로 잘렸고 실행되지 않았습니다.
                # * 인자는 읽혔는데 응답이 한도에 닿았다 — 호출은 실행됩니다. 다만
                #   한도에 닿은 자리가 마지막 호출의 끝이라, 그 끝이 잘렸을 수는 있습니다.
                arguments_cut = any(not parsed_ok for _n, _a, _i, parsed_ok in parsed)
                limit_reached = finish_reason == "length" and not arguments_cut
                if arguments_cut or limit_reached:
                    spent = completion_budget_report(
                        message, tool_calls, parsed,
                        estimate_tokens(agent.model, current_messages), window, schema_tokens,
                    )
                    if arguments_cut:
                        logger.warning(
                            f"Truncated tool call from {agent.name}: "
                            f"finish_reason={finish_reason!r}, "
                            f"max_tokens={effective_max_tokens(agent)}, {spent}"
                        )
                    else:
                        logger.warning(
                            f"Response from {agent.name} reached max_tokens after readable "
                            f"tool call(s); they were executed (the last one may be cut): "
                            f"finish_reason={finish_reason!r}, "
                            f"max_tokens={effective_max_tokens(agent)}, {spent}"
                        )

                # Append assistant message with tool calls to context
                current_messages.append(self._assistant_turn(message, parsed))
                # 도구를 실행하기 전에 남깁니다. 실행 도중 끊기면 무엇을 부르던 중이었는지 압니다.
                await save_progress()

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
                        security: Dict[str, Any] = {}
                    else:
                        # 도구 보안: 허용·묻기·거부. 거부되면 실행하지 않고, 그 이유가 곧
                        # 도구 결과가 됩니다 — 모델이 읽고 다른 길을 찾습니다.
                        #
                        # 스킬 도구는 별도의 게이트 보안 검사를 거치지 않습니다. 호스트 프로세스가
                        # 스킬 디렉터리 내의 지침 파일만을 참조하며 작업 공간이나 외부 네트워크에 접근하지 않고,
                        # 사용 권한은 `allowed_skills` 및 활성화 설정을 통해 사전에 통제되기 때문입니다.
                        # (스킬 내부의 스크립트를 실제로 **실행**하는 단계는 샌드박스 도구를 통해 수행되므로,
                        # 해당 도구 호출 시 정상적으로 보안 판정을 받게 됩니다.)
                        gate = None if is_skill_tool(fn_name) else await self._check_tool_gate(
                            tool_gate, agent, fn_name, fn_args, mcp or self.mcp_manager,
                        )
                        if gate is not None and not gate.allowed:
                            output, status = gate.output, gate.status or "denied"
                        else:
                            output, status = await self._execute_tool_safely(
                                agent, fn_name, fn_args, session_id, mcp, tools=tools,
                            )
                        security = gate.audit if gate is not None else {}

                    call_log = {
                        "tool_name": fn_name or "(unknown)",
                        "arguments": fn_args,
                        "output": output,
                        "status": status,
                        "security": security,
                    }
                    tool_logs.append(call_log)
                    await self._notify_tool_call(agent, on_tool_call, call_log)

                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": fn_name or "unknown_tool",
                        "content": output,
                    })
                    # 도구 하나가 끝날 때마다 남깁니다. 끊기면 그 다음 도구부터 이어 갑니다.
                    await save_progress()

                # 잘렸다는 사실은 **말로** 알려야 합니다. 도구 서버가 돌려준
                # "Input validation error" 만으로는 모델이 원인을 알 수 없어,
                # 같은 호출을 그대로 다시 시도하다 예산을 태웁니다.
                # 사고가 한도를 크게 먹었으면 "나누어 쓰라" 만으로는 다시 걸립니다.
                # 두 경우 모두 같은 뿌리이므로 같은 한 단락을 덧붙입니다.
                if arguments_cut:
                    self._append_budget_notice(
                        current_messages,
                        TRUNCATED_TOOL_CALL_NOTICE.format(
                            max_tokens=max_tokens_label(agent),
                            advice=truncation_advice(tools),
                        ) + reasoning_heavy_note(message, tool_calls),
                    )
                elif limit_reached:
                    self._append_budget_notice(
                        current_messages,
                        limit_reached_notice(max_tokens_label(agent), parsed[-1][0], tools)
                        + reasoning_heavy_note(message, tool_calls),
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
                # 사람이 늘려 준 상한도 끊겼다 이어 갈 때 남아 있어야 합니다.
                await save_progress()
                continue

            return await self._wrap_up_without_tools(
                agent, current_messages, segments, tool_logs, limit, on_chunk, tools=tools
            )
