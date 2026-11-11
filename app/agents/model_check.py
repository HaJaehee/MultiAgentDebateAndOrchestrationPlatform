"""모델 적합성 점검 (ADR-054) — 연결 하나에 작은 요청 몇 개를 보내, 그 모델로 MADO 가 어디까지 되는지 봅니다.

`model_status` 는 "지금 응답하는가" 만 봅니다. 폐쇄망에 새 모델이 들어오면 그다음이 문제입니다. 도구 호출이
`tool_calls` 로 오는지 본문 글자로 새는지, 추론이 본문과 갈라져 오는지, 잘린 답을 잘렸다고 알려 주는지,
서버의 컨텍스트 창이 설정보다 작은지 — 이런 것은 지금까지 토론이 터진 뒤에야 알았습니다
(`wiki/agents/llm-integration.md` §2.1 · §2.6 · §2.7 · §5.2). 여기서는 반입한 날 미리 물어봅니다.

## 원칙

- **결정적 검사만.** 판정에 LLM 을 쓰지 않습니다. 응답의 모양(필드·표식·종료 사유)과 정해 둔 낱말만 봅니다.
  모델의 품질은 여기서 재지 않습니다 (그것은 Bench 의 일입니다).
- **MADO 가 실제로 보내는 모양으로.** 마무리 호출의 `tool_choice="none"`, 지연 로딩 목록(`catalog_tool`) 같은
  요청은 실제 코드가 만드는 것을 그대로 씁니다. 검사가 통과하면 토론에서도 통과해야 뜻이 있습니다.
- **제안만 합니다.** 결과에 따라 런타임이 달라지지 않습니다. 권장 설정은 글과 `Suggestion` 으로 내놓고,
  conf 를 고치는 것은 사람이 합니다. 설정 이름이 붙은 권고는 화면의 `적용` 단추로 한 번에 적을 수 있지만
  (`apply_suggestion`, ADR-056), 그것도 사람이 눌러야 하고 서버 PC 에서만 됩니다.
- **부하.** 생성형 연결 하나에 요청 8건(짧은 답)과 `/models` 조회 1건, 결정 LLM 은 1건입니다. 서버 전체의
  동시 요청 상한(`app.llm_concurrency`)을 지키며 차례로 보냅니다. 재시도는 없습니다.
- **기록에는 크기와 상태만.** 오류 원문은 키를 가린 한두 줄(`safe_detail`)만 남기고, 모델이 쓴 글은 저장하지 않습니다.

결과는 `data/modelcheck/<연결 id>.json` 에 남고, 모델 연결 상태 창과 `python -m app.modelcheck` 가 함께 읽습니다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from app.agents import llm as llm_module
from app.agents.connection import STATE_INFO, classify, connection_id
from app.agents.decision import DecisionCaller, DecisionQuestion
from app.agents.llm_gate import get_llm_gate
from app.agents.llm_recovery import find_leaked_tool_call
from app.agents.llm_tool_calls import ToolCallMixin
from app.agents.model_status import ModelTarget, collect_targets, probe_kwargs, safe_detail
from app.agents.tool_loading import catalog_tool, is_load_tool, tool_loading_guidance
from app.config import (
    DEFAULT_CONFIG_PATH,
    ORCHESTRATOR_KEY,
    RootConfig,
    active_config_path,
    get_config,
    set_agent_allowed_modalities_in_conf_file,
    set_agent_limits_in_conf_file,
    set_tool_loading_enabled_in_conf_file,
)

logger = logging.getLogger(__name__)

# 결과의 갈래
OK = "ok"          # 기대한 대로 동작합니다
INFO = "info"      # 알아 둘 것 — 고칠 것은 없습니다
WARN = "warn"      # 일부 기능이 다르게 동작합니다. 설정을 살펴보세요
FAIL = "fail"      # 이 연결로는 그 기능이 동작하지 않습니다
SKIP = "skip"      # 확인하지 못했습니다

OUTCOME_LABELS: Dict[str, str] = {OK: "통과", INFO: "참고", WARN: "주의", FAIL: "실패", SKIP: "건너뜀"}
# 연결 상태 창과 같은 색조 이름 (`app/ui/components/model_status.py`).
OUTCOME_TONES: Dict[str, str] = {OK: "good", INFO: "idle", WARN: "warn", FAIL: "bad", SKIP: "idle"}

CHECK_TIMEOUT_SECONDS = 60.0
MODELS_TIMEOUT_SECONDS = 10.0
# 짧은 답을 받는 요청의 출력 한도. 추론 모델은 숨은 추론도 이 한도를 함께 쓰므로 너무 작게 잡지 않습니다.
ANSWER_TOKENS = 512
# 잘림을 알려 주는지 보는 요청의 출력 한도.
LENGTH_PROBE_TOKENS = 16

GATE_HOLDER = "model-check"
SYSTEM2 = "system2"
SYSTEM1 = "system1"

PROBE_TOOL_NAME = "probe__lookup_code"
PROBE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROBE_TOOL_NAME,
        "description": "[probe] Looks up the meaning of a short code. Use it whenever a code must be resolved.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "The code to look up, e.g. A7"}},
            "required": ["code"],
        },
    },
}
PROBE_CODE = "A7"
PROBE_MEANING = "청록 등대"

# 권장 설정의 이름 (`Suggestion.setting`). 화면의 적용 단추가 이 이름으로 conf 의 자리를 찾습니다.
SET_MAX_CONTEXT_WINDOW = "max_context_window"
SET_MAX_TOKENS = "max_tokens"
SET_TOOL_LOADING = "tool_loading.enabled"
SET_DROP_IMAGE = "allowed_modalities.image"

RECOMMENDED_REASONING_MAX_TOKENS = 8192


@dataclass(frozen=True)
class Suggestion:
    """권장 설정 하나. `setting` 이 비어 있으면 conf 로 고칠 수 없는 것(서버 쪽 설정)입니다."""

    text: str
    setting: str = ""
    value: Any = None


@dataclass(frozen=True)
class CheckResult:
    key: str
    title: str
    outcome: str
    summary: str
    suggestions: Tuple[Suggestion, ...] = ()
    # 주인에게만 보여 주는 오류 원문 요약 (키는 가린 뒤).
    detail: str = ""


@dataclass(frozen=True)
class AgentUse:
    """이 연결을 쓰는 에이전트 하나와, 판정에 필요한 그 에이전트의 설정."""

    key: str
    name: str
    max_context_window: int
    max_tokens: int
    takes_images: bool


@dataclass(frozen=True)
class ModelProfile:
    """연결 하나의 점검 결과."""

    target_id: str
    model: str
    kind: str
    users: Tuple[str, ...]
    results: Tuple[CheckResult, ...]
    checked_at: float = 0.0
    endpoint: str = ""

    def count(self, outcome: str) -> int:
        return sum(1 for result in self.results if result.outcome == outcome)

    @property
    def worst(self) -> str:
        for outcome in (FAIL, WARN, INFO, OK):
            if self.count(outcome):
                return outcome
        return SKIP

    def suggestions(self) -> List[Suggestion]:
        return [s for result in self.results for s in result.suggestions]

    def to_dict(self, *, detail: bool = True) -> Dict[str, Any]:
        """저장·화면용 사전. `detail` 이 꺼져 있으면 주소와 오류 원문을 담지 않습니다."""
        data: Dict[str, Any] = {
            "target_id": self.target_id, "model": self.model, "kind": self.kind, "users": list(self.users),
            "checked_at": self.checked_at, "worst": self.worst, "summary": summarize(self),
            "results": [
                {
                    "key": r.key, "title": r.title, "outcome": r.outcome, "label": OUTCOME_LABELS[r.outcome],
                    "tone": OUTCOME_TONES[r.outcome], "summary": r.summary,
                    "suggestions": [
                        {"text": s.text, "setting": s.setting, "value": s.value} for s in r.suggestions
                    ],
                    **({"detail": r.detail} if detail and r.detail else {}),
                }
                for r in self.results
            ],
        }
        if detail:
            data["endpoint"] = self.endpoint
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelProfile":
        return cls(
            target_id=str(data["target_id"]), model=str(data.get("model") or ""),
            kind=str(data.get("kind") or SYSTEM2), users=tuple(data.get("users") or ()),
            checked_at=float(data.get("checked_at") or 0.0), endpoint=str(data.get("endpoint") or ""),
            results=tuple(
                CheckResult(
                    key=str(r["key"]), title=str(r.get("title") or r["key"]), outcome=str(r.get("outcome") or SKIP),
                    summary=str(r.get("summary") or ""), detail=str(r.get("detail") or ""),
                    suggestions=tuple(
                        Suggestion(text=str(s.get("text") or ""), setting=str(s.get("setting") or ""), value=s.get("value"))
                        for s in r.get("suggestions") or ()
                    ),
                )
                for r in data.get("results") or ()
            ),
        )


def summarize(profile: ModelProfile) -> str:
    parts = [f"{OUTCOME_LABELS[o]} {profile.count(o)}" for o in (OK, INFO, WARN, FAIL, SKIP) if profile.count(o)]
    return " · ".join(parts) or "결과 없음"


# ---------------------------------------------------------------- 응답 읽기


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first_choice(response: Any) -> Any:
    choices = _get(response, "choices") or []
    return choices[0] if choices else None


def _text_of(message: Any) -> str:
    content = _get(message, "content") or ""
    if isinstance(content, list):  # 블록으로 돌려주는 Provider
        content = "\n".join(
            str(block.get("text") or "") if isinstance(block, Mapping) else str(block) for block in content
        )
    return str(content)


def _reasoning_of(message: Any) -> str:
    return str(_get(message, "reasoning_content") or "")


def _tool_calls_of(message: Any) -> List[Tuple[str, str]]:
    """`(이름, 인자 문자열)` 목록."""
    calls = []
    for call in _get(message, "tool_calls") or []:
        function = _get(call, "function")
        calls.append((str(_get(function, "name") or ""), str(_get(function, "arguments") or "")))
    return calls


def _usage_tokens(usage: Any) -> int:
    try:
        return int(_get(usage, "prompt_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _state_label(exc: BaseException) -> str:
    return STATE_INFO[classify(exc)][0]


# ---------------------------------------------------------------- 요청 보내기


class _Asker:
    """연결 하나에 점검 요청을 보냅니다. 연결 정보는 연결 확인(`probe_kwargs`)과 같고, 관문의 자리를 지킵니다."""

    def __init__(self, target: ModelTarget, timeout_seconds: float) -> None:
        self.target = target
        self.timeout_seconds = timeout_seconds
        self.requests = 0

    def _kwargs(self, messages: List[Dict[str, Any]], max_tokens: int, tools: Optional[List[Dict[str, Any]]],
                tool_choice: str) -> Dict[str, Any]:
        kwargs = probe_kwargs(self.target, self.timeout_seconds)
        kwargs["messages"] = messages
        kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        return kwargs

    async def ask(self, messages: List[Dict[str, Any]], *, max_tokens: int = ANSWER_TOKENS,
                  tools: Optional[List[Dict[str, Any]]] = None, tool_choice: str = "auto") -> Any:
        """스트리밍 없이 한 번. 응답 객체를 돌려주고, 실패는 그대로 올립니다."""
        kwargs = self._kwargs(messages, max_tokens, tools, tool_choice)
        self.requests += 1
        async with get_llm_gate().slot(GATE_HOLDER):
            return await asyncio.wait_for(llm_module.litellm.acompletion(**kwargs), self.timeout_seconds + 2.0)

    async def stream(self, messages: List[Dict[str, Any]], *, max_tokens: int = ANSWER_TOKENS) -> Tuple[int, int]:
        """실제 발언과 같은 방식(`stream=True`, 추가 옵션 없음)으로 한 번. `(글 조각 수, 추론 조각 수)`.

        스트림의 사용량은 여기서 보지 않습니다. LiteLLM 은 서버가 보내지 않아도 마지막 조각에 토크나이저로 센
        값을 채워 넣어, 조각만 보고는 서버가 보고한 것인지 가를 수 없습니다 (1.98 · 1.105 에서 확인).
        """
        kwargs = self._kwargs(messages, max_tokens, None, "auto")
        self.requests += 1

        async def consume() -> Tuple[int, int]:
            text_chunks = reasoning_chunks = 0
            response = await llm_module.litellm.acompletion(**kwargs, stream=True)
            async for chunk in response:
                choice = _first_choice(chunk)
                delta = _get(choice, "delta") if choice is not None else None
                if delta is None:
                    continue
                if _get(delta, "content"):
                    text_chunks += 1
                if _get(delta, "reasoning_content"):
                    reasoning_chunks += 1
            return text_chunks, reasoning_chunks

        async with get_llm_gate().slot(GATE_HOLDER):
            return await asyncio.wait_for(consume(), self.timeout_seconds + 2.0)


# ---------------------------------------------------------------- 검사 (생성형 연결)


def _judge_answer(response: Any) -> Tuple[CheckResult, CheckResult]:
    """첫 응답 하나로 '기본 응답' 과 '추론 분리' 를 함께 봅니다."""
    choice = _first_choice(response)
    message = _get(choice, "message")
    finish = str(_get(choice, "finish_reason") or "")
    text, reasoning = _text_of(message).strip(), _reasoning_of(message).strip()
    usage_note = "사용량 보고됨" if _usage_tokens(_get(response, "usage")) else "사용량 보고 없음(토큰은 추정치로 기록)"

    if text:
        right = "42" in text
        answer = CheckResult(
            "answer", "기본 응답", OK if right else INFO,
            f"응답했습니다 · {usage_note}" if right
            else f"응답은 왔지만 기대한 답(42)이 없습니다 · {usage_note}",
        )
    else:
        answer = CheckResult("answer", "기본 응답", WARN, f"본문이 비어 왔습니다 (finish_reason={finish or '없음'}) · {usage_note}")

    if not text and reasoning:
        if finish == "length":
            reason = CheckResult(
                "reasoning", "추론과 본문", WARN,
                f"추론이 응답 한도({ANSWER_TOKENS} 토큰)를 모두 써서 본문이 없습니다 (추론 {len(reasoning):,}자).",
                (Suggestion(
                    f"이 모델은 숨은 추론이 max_tokens 를 함께 씁니다. max_tokens 를 {RECOMMENDED_REASONING_MAX_TOKENS:,} 이상으로 두세요.",
                    SET_MAX_TOKENS, RECOMMENDED_REASONING_MAX_TOKENS,
                ),),
            )
        else:
            reason = CheckResult(
                "reasoning", "추론과 본문", WARN,
                f"답이 추론(reasoning_content {len(reasoning):,}자) 안에만 있고 본문은 비었습니다.",
                (Suggestion(
                    "서버의 reasoning parser 가 사고 종료 표식을 찾지 못하는 모양입니다. 서버의 파서 설정을 확인하세요 "
                    "(vLLM: --reasoning-parser). MADO 는 결론 표식이 있으면 추론에서 답을 꺼내지만, 그만큼 한 번 더 묻게 됩니다."
                ),),
            )
    elif "<think>" in text or "</think>" in text:
        reason = CheckResult(
            "reasoning", "추론과 본문", WARN,
            "추론이 본문에 섞여 옵니다 (<think> 표식). 사고 과정이 발언과 다음 발언자의 프롬프트에 그대로 실립니다.",
            (Suggestion("서버가 추론을 reasoning_content 로 갈라 주도록 reasoning parser 를 켜세요 (vLLM: --reasoning-parser)."),),
        )
    elif reasoning:
        reason = CheckResult("reasoning", "추론과 본문", OK, f"추론이 본문과 갈라져 옵니다 (추론 {len(reasoning):,}자).")
    else:
        reason = CheckResult("reasoning", "추론과 본문", OK, "추론을 따로 보내지 않는 모델입니다.")
    return answer, reason


async def _check_stream(asker: _Asker) -> CheckResult:
    title = "스트리밍"
    try:
        text_chunks, reasoning_chunks = await asker.stream(
            [{"role": "user", "content": "하나부터 다섯까지 한글로 세어 주세요."}]
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "stream", title, WARN,
            f"스트리밍 요청이 실패했습니다 ({_state_label(exc)}). MADO 는 발언마다 스트리밍 없이 한 번 더 묻습니다 — "
            "글이 한꺼번에 나타나고 발언마다 실패한 요청이 하나씩 더 나갑니다.",
            detail=safe_detail(exc, asker.target),
        )
    if not text_chunks and not reasoning_chunks:
        return CheckResult("stream", title, WARN, "스트림이 열렸지만 글 조각이 오지 않았습니다.")
    return CheckResult(
        "stream", title, OK,
        f"글이 조각으로 옵니다 (본문 {text_chunks}조각"
        + (f", 추론 {reasoning_chunks}조각" if reasoning_chunks else "") + ").",
    )


async def _check_length(asker: _Asker) -> CheckResult:
    title = "잘림 알림"
    try:
        response = await asker.ask(
            [{"role": "user", "content": "1부터 200까지의 수를 한 줄에 하나씩 모두 쓰세요."}],
            max_tokens=LENGTH_PROBE_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("length", title, SKIP, f"요청이 실패했습니다 ({_state_label(exc)}).",
                           detail=safe_detail(exc, asker.target))
    finish = str(_get(_first_choice(response), "finish_reason") or "")
    if finish == "length":
        return CheckResult("length", title, OK, "한도에서 잘리면 finish_reason='length' 로 알려 줍니다.")
    return CheckResult(
        "length", title, WARN,
        f"출력 한도 {LENGTH_PROBE_TOKENS} 토큰에서 끝났는데 finish_reason='{finish or '없음'}' 입니다. "
        "잘린 답 이어 쓰기와 '여기서 잘렸습니다' 안내가 동작하지 않아, 잘린 발언이 끝난 것처럼 기록됩니다.",
        (Suggestion("max_tokens 를 넉넉히 두어 잘림 자체를 줄이세요. 서버나 게이트웨이가 finish_reason 을 바꾸는지도 확인하세요."),),
    )


async def _check_roles(asker: _Asker) -> CheckResult:
    title = "연속된 user 메시지"
    try:
        await asker.ask([
            {"role": "user", "content": "기억할 낱말: 바다"},
            {"role": "user", "content": "방금 낱말을 한 번만 그대로 쓰세요."},
        ])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "roles", title, INFO,
            f"같은 역할이 연달아 오면 거절합니다 ({_state_label(exc)}). MADO 는 합쳐서 보내므로 토론에는 영향이 없습니다.",
            detail=safe_detail(exc, asker.target),
        )
    return CheckResult("roles", title, OK, "같은 역할이 연달아 와도 받습니다.")


def _tool_prompt() -> List[Dict[str, Any]]:
    return [{
        "role": "user",
        "content": (
            f"도구 {PROBE_TOOL_NAME} 를 code=\"{PROBE_CODE}\" 로 호출하세요. "
            "결과를 받으면 그 뜻을 그대로 한 줄로 말하세요."
        ),
    }]


async def _check_tools(asker: _Asker) -> Tuple[CheckResult, Optional[Dict[str, Any]]]:
    """도구 호출이 `tool_calls` 로 오는가. 제대로 왔으면 그 발언을 실제 루프가 되돌려 넣는 모양으로 함께 돌려줍니다."""
    title = "도구 호출"
    parser_hint = Suggestion(
        "서버의 도구 호출 옵션과 파서가 이 모델의 형식과 맞는지 확인하세요 "
        "(vLLM: --enable-auto-tool-choice --tool-call-parser <모델 형식>)."
    )
    try:
        response = await asker.ask(_tool_prompt(), tools=[PROBE_TOOL])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "tools", title, FAIL,
            f"도구 정의가 담긴 요청을 받지 않습니다 ({_state_label(exc)}). 이 연결의 에이전트는 MCP 도구를 쓸 수 없습니다.",
            (parser_hint,), detail=safe_detail(exc, asker.target),
        ), None
    message = _get(_first_choice(response), "message")
    raw_calls = _get(message, "tool_calls") or []
    if raw_calls:
        # 실제 루프와 같은 풀이(`_parse_tool_call`)를 씁니다. 여기서 읽히면 토론에서도 읽힙니다.
        parsed = [ToolCallMixin._parse_tool_call(call) for call in raw_calls]  # noqa: SLF001
        name, arguments, _call_id, readable = parsed[0]
        if not readable:
            size = len(str(arguments.get("raw", "")))
            return CheckResult("tools", title, WARN,
                               f"tool_calls 로 왔지만 인자가 JSON 이 아닙니다 (인자 {size}자).", (parser_hint,)), None
        if name == PROBE_TOOL_NAME and arguments.get("code") == PROBE_CODE:
            turn = ToolCallMixin._assistant_turn(message, parsed)  # noqa: SLF001
            return CheckResult("tools", title, OK, "도구 호출이 tool_calls 로 오고 인자를 읽을 수 있습니다."), turn
        return CheckResult("tools", title, WARN, "tool_calls 로 왔지만 도구 이름이나 인자가 요청과 다릅니다."), None
    if find_leaked_tool_call(_text_of(message)) is not None:
        return CheckResult(
            "tools", title, FAIL,
            "도구 호출이 본문 글자로 새어 나옵니다. 서버가 호출로 해석하지 못해 도구가 실행되지 않습니다.",
            (parser_hint,),
        ), None
    return CheckResult(
        "tools", title, WARN, "도구를 부르지 않고 글로 답했습니다. 도구가 필요한 일을 시켜도 부르지 않을 수 있습니다.",
    ), None


async def _check_tool_result(asker: _Asker, assistant_turn: Dict[str, Any]) -> CheckResult:
    """도구 결과가 담긴 대화를 `tool_choice="none"` 으로 마무리시킵니다 (`_wrap_up_without_tools` 와 같은 모양).

    assistant 턴은 지어내지 않고 **방금 모델이 낸 것**을 실제 루프의 방식(`_assistant_turn`)으로 되돌려 넣습니다.
    추론 모델 가운데에는 도구를 부른 발언의 `reasoning_content` 를 다음 요청에 그대로 돌려받아야 하는 것이
    있어(DeepSeek 의 thinking mode), 손으로 만든 턴은 멀쩡한 모델에서 거절됩니다.
    """
    title = "도구 결과 뒤 마무리"
    messages = [*_tool_prompt(), assistant_turn]
    for call in assistant_turn.get("tool_calls") or []:
        messages.append({
            "role": "tool", "tool_call_id": call["id"], "name": call["function"]["name"],
            "content": f"{PROBE_CODE} = {PROBE_MEANING}",
        })
    try:
        response = await asker.ask(messages, tools=[PROBE_TOOL], tool_choice="none")
    except Exception as exc:  # noqa: BLE001
        first = safe_detail(exc, asker.target)
        try:
            await asker.ask(messages, tools=[PROBE_TOOL], tool_choice="auto")
        except Exception as again:  # noqa: BLE001
            return CheckResult(
                "tool_result", title, FAIL,
                f"도구 결과가 담긴 대화를 받지 않습니다 ({_state_label(again)}). 도구를 한 번 부른 뒤의 발언이 모두 실패합니다.",
                detail=safe_detail(again, asker.target),
            )
        return CheckResult(
            "tool_result", title, WARN,
            f"tool_choice='none' 을 받지 않습니다 ({_state_label(exc)}). 도구 한도에 닿은 발언의 마무리와 "
            "잘린 답 이어 쓰기가 실패합니다.",
            detail=first,
        )
    message = _get(_first_choice(response), "message")
    if _tool_calls_of(message):
        return CheckResult("tool_result", title, WARN,
                           "tool_choice='none' 인데 도구를 다시 불렀습니다. 마무리 호출에서 부른 도구는 실행되지 않고 버려집니다.")
    if PROBE_MEANING.split()[0] in _text_of(message):
        return CheckResult("tool_result", title, OK, "도구 결과를 읽고 답하며, tool_choice='none' 을 지킵니다.")
    return CheckResult("tool_result", title, INFO, "요청은 받았지만 답에 도구 결과가 보이지 않습니다.")


async def _check_catalog(asker: _Asker) -> CheckResult:
    """지연 로딩 목록(ADR-039)을 읽고 도구를 불러오는가. 실제 발언과 같은 목록 도구와 상시 지침을 씁니다."""
    title = "도구 목록에서 불러오기"
    tools = [catalog_tool([PROBE_TOOL])]
    messages = [
        {"role": "system", "content": tool_loading_guidance(tools) or ""},
        {"role": "user", "content": f"코드 {PROBE_CODE} 의 뜻을 도구로 찾아 주세요."},
    ]
    try:
        response = await asker.ask(messages, tools=tools)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("catalog", title, SKIP, f"요청이 실패했습니다 ({_state_label(exc)}).",
                           detail=safe_detail(exc, asker.target))
    names = [name for name, _ in _tool_calls_of(_get(_first_choice(response), "message"))]
    if any(is_load_tool(name) for name in names):
        return CheckResult("catalog", title, OK, "목록을 읽고 필요한 도구를 불러옵니다.")
    if PROBE_TOOL_NAME in names:
        return CheckResult("catalog", title, OK, "목록의 도구를 곧바로 불렀습니다. MADO 는 이 호출도 실행하고 정의를 실어 줍니다.")
    return CheckResult(
        "catalog", title, WARN,
        "목록에 있는 도구를 불러오지 않았습니다. 지연 로딩을 켜 두면 이 모델은 목록의 도구를 없는 것으로 여길 수 있습니다.",
        (Suggestion(
            "이 모델을 주로 쓴다면 tool_loading.enabled 를 false 로 두어 도구 정의를 처음부터 모두 싣거나, "
            "자주 쓰는 도구를 tool_loading.core_tools 에 더하세요.",
            SET_TOOL_LOADING, False,
        ),),
    )


def _red_png(size: int = 32) -> bytes:
    """한 가지 색(빨강)으로 칠한 PNG. 표준 라이브러리만으로 만듭니다."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    row = b"\x00" + b"\xff\x00\x00" * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


async def _check_image(asker: _Asker, uses: Sequence[AgentUse]) -> CheckResult:
    title = "이미지 입력"
    takers = [use.name for use in uses if use.takes_images]
    drop = (Suggestion(
        f"이 연결을 쓰는 {', '.join(takers)} 의 모달에서 이미지를 끄세요. 켜 두면 첨부한 그림이 전달되지 않거나 발언이 한 번 거절됩니다.",
        SET_DROP_IMAGE, False,
    ),) if takers else ()
    data = base64.b64encode(_red_png()).decode("ascii")
    try:
        response = await asker.ask([{"role": "user", "content": [
            {"type": "text", "text": "이 그림은 한 가지 색으로만 칠해져 있습니다. 그 색을 영어 한 단어로 답하세요."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
        ]}])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "image", title, WARN if takers else INFO,
            f"이미지가 담긴 요청을 받지 않습니다 ({_state_label(exc)}).",
            drop, detail=safe_detail(exc, asker.target),
        )
    if "red" in _text_of(_get(_first_choice(response), "message")).lower():
        return CheckResult("image", title, OK, "이미지를 받아 내용을 읽습니다.")
    return CheckResult(
        "image", title, WARN if takers else INFO,
        "요청은 받았지만 그림의 내용을 읽지 못했습니다. 중간(게이트웨이·LiteLLM)에서 그림이 조용히 빠졌을 수 있습니다.",
        drop,
    )


def _bare_model(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


async def _server_window(target: ModelTarget, transport: Optional[httpx.AsyncBaseTransport]) -> Optional[int]:
    """서버가 말하는 컨텍스트 창. OpenAI 호환 `/models` 의 `max_model_len`(vLLM) 따위를 읽고, 없으면 LiteLLM 의 표를 봅니다."""
    if target.api_base:
        headers = {"Authorization": f"Bearer {target.api_key}"} if (target.api_key or "").strip() else {}
        headers.update(dict(target.extra_headers))
        try:
            async with httpx.AsyncClient(timeout=MODELS_TIMEOUT_SECONDS, transport=transport) as client:
                reply = await client.get(target.api_base.rstrip("/") + "/models", headers=headers)
            entries = reply.json().get("data") if reply.status_code == 200 else None
        except Exception:  # noqa: BLE001 - 창 크기를 못 읽는 것은 점검 실패가 아닙니다
            entries = None
        wanted = {target.model, _bare_model(target.model)}
        for entry in entries or []:
            if not isinstance(entry, Mapping) or entry.get("id") not in wanted:
                continue
            for name in ("max_model_len", "context_length", "context_window", "max_context_length"):
                try:
                    value = int(entry.get(name) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    return value
        return None
    try:
        info = llm_module.litellm.get_model_info(target.model)
        value = int(info.get("max_input_tokens") or 0)
        return value or None
    except Exception:  # noqa: BLE001
        return None


async def _check_window(
    target: ModelTarget, uses: Sequence[AgentUse], transport: Optional[httpx.AsyncBaseTransport],
) -> CheckResult:
    title = "컨텍스트 창"
    window = await _server_window(target, transport)
    if window is None:
        return CheckResult(
            "window", title, SKIP,
            "서버가 창 크기를 알려 주지 않습니다. 모델 카드의 값을 max_context_window 에 직접 맞춰 주세요 — "
            "설정이 실제보다 크면 MADO 가 자르기 전에 서버가 먼저 거절합니다.",
        )
    over = [use for use in uses if use.max_context_window > window]
    if over:
        names = ", ".join(f"{use.name}({use.max_context_window:,})" for use in over)
        return CheckResult(
            "window", title, FAIL,
            f"서버의 창은 {window:,} 토큰인데 설정이 더 큽니다: {names}. 긴 토론에서 MADO 가 줄이기 전에 서버가 요청을 거절합니다.",
            (Suggestion(f"max_context_window 를 {window:,} 이하로 낮추세요.", SET_MAX_CONTEXT_WINDOW, window),),
        )
    if not uses:
        return CheckResult("window", title, INFO, f"서버의 창은 {window:,} 토큰입니다. 이 연결을 쓰는 에이전트가 없어 견줄 설정이 없습니다.")
    return CheckResult("window", title, OK, f"서버의 창 {window:,} 토큰 안에 설정이 들어 있습니다.")


# ---------------------------------------------------------------- 검사 (결정 LLM)

_S1_QUESTIONS: Dict[str, DecisionQuestion] = {
    "noul": DecisionQuestion(type="noul", instructions="이 보고는 좋은 소식인가?"),
    "choice": DecisionQuestion(type="choice", instructions="다음에 할 일을 고르시오.", criteria=("go", "stop"),
                               choice_notes=("계속 진행한다", "멈춘다")),
    "score": DecisionQuestion(type="score", instructions="이 보고의 확실함은 어느 정도인가?", criteria=("낮음", "보통", "높음")),
}
_S1_STATE = "시험 12건이 모두 통과했고 남은 결함은 없다."


async def _check_system1(
    target: ModelTarget, timeout_seconds: float, transport: Optional[httpx.AsyncBaseTransport],
) -> List[CheckResult]:
    titles = {"noul": "예/아니오 확률 (noul)", "choice": "선택지 (choice)", "score": "점수 (score)"}
    cfg = target.decision_cfg.model_copy(update={"timeout": timeout_seconds})
    try:
        async with get_llm_gate().slot(GATE_HOLDER):
            response = await asyncio.wait_for(
                DecisionCaller(transport)._evaluate_unthrottled(cfg, _S1_STATE, _S1_QUESTIONS),  # noqa: SLF001
                timeout_seconds + 2.0,
            )
    except Exception as exc:  # noqa: BLE001
        failed = CheckResult("systemone", "결정 LLM 응답", FAIL, f"요청이 실패했습니다 ({_state_label(exc)}).",
                             detail=safe_detail(exc, target))
        return [failed, *(CheckResult(f"s1_{key}", title, SKIP, "응답이 없어 확인하지 못했습니다.") for key, title in titles.items())]

    results = [CheckResult("systemone", "결정 LLM 응답", OK, f"질문 {len(_S1_QUESTIONS)}개에 한 번의 요청으로 답했습니다.")]
    noul = response.answers.get("noul")
    results.append(
        CheckResult("s1_noul", titles["noul"], OK, "0과 1 사이의 확률을 돌려줍니다.")
        if noul is not None and noul.noul is not None and 0.0 <= noul.noul <= 1.0
        else CheckResult("s1_noul", titles["noul"], FAIL,
                         "확률을 읽지 못했습니다. 발언자 선정·gate 분기·서브 토론 판정이 모두 '아니오' 쪽으로 기웁니다.")
    )
    choice = response.answers.get("choice")
    criteria = _S1_QUESTIONS["choice"].criteria or ()
    if choice is None or choice.choice not in criteria:
        results.append(CheckResult("s1_choice", titles["choice"], FAIL, "선택지 이름으로 답하지 않았습니다."))
    elif not choice.probabilities or not set(choice.probabilities) <= set(criteria):
        results.append(CheckResult("s1_choice", titles["choice"], WARN, "선택은 했지만 선택지별 확률이 없거나 이름이 다릅니다."))
    else:
        results.append(CheckResult("s1_choice", titles["choice"], OK, "선택지와 선택지별 확률을 돌려줍니다."))
    score = response.answers.get("score")
    results.append(
        CheckResult("s1_score", titles["score"], OK, "점수를 돌려줍니다.")
        if score is not None and score.score is not None
        else CheckResult("s1_score", titles["score"], FAIL, "점수를 읽지 못했습니다.")
    )
    return results


# ---------------------------------------------------------------- 연결 하나 점검


def uses_of(target: ModelTarget, config: Optional[RootConfig] = None) -> List[AgentUse]:
    """이 연결을 쓰는 켜진 에이전트. 결정 LLM 을 쓰는 전문가는 생성형 연결의 사용자로 세지 않습니다."""
    config = config or get_config()
    uses: List[AgentUse] = []
    for key, cfg in config.agents.items():
        if not cfg.enabled:
            continue
        on_system1 = cfg.decision_llm.enabled and key != ORCHESTRATOR_KEY
        if target.decision_cfg is not None:
            if not cfg.decision_llm.enabled or connection_id(cfg.decision_llm) != target.id:
                continue
        else:
            if on_system1:
                continue
            system2 = cfg.model_copy(update={"decision_llm": type(cfg.decision_llm)()}) if cfg.decision_llm.enabled else cfg
            if connection_id(system2) != target.id:
                continue
        uses.append(AgentUse(
            key=key, name=cfg.name or key, max_context_window=cfg.max_context_window,
            max_tokens=cfg.max_tokens, takes_images="image" in cfg.allowed_modalities,
        ))
    return uses


async def check_target(
    target: ModelTarget,
    config: Optional[RootConfig] = None,
    *,
    timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
    http_transport: Optional[httpx.AsyncBaseTransport] = None,
    wall: Callable[[], float] = time.time,
) -> ModelProfile:
    """연결 하나를 점검합니다. 예외를 올리지 않고 결과로 돌려줍니다."""
    kind = SYSTEM1 if target.decision_cfg is not None else SYSTEM2

    def profile(results: Sequence[CheckResult]) -> ModelProfile:
        return ModelProfile(
            target_id=target.id, model=target.model, kind=kind, users=target.users,
            results=tuple(results), checked_at=wall(), endpoint=target.endpoint,
        )

    if not target.is_live:
        return profile([CheckResult("answer", "기본 응답", SKIP, "연결 정보가 없어 점검하지 않았습니다.")])
    if kind == SYSTEM1:
        return profile(await _check_system1(target, timeout_seconds, http_transport))

    uses = uses_of(target, config)
    asker = _Asker(target, timeout_seconds)
    try:
        first = await asker.ask([{"role": "user", "content": "17 + 25 는 얼마입니까? 숫자만 답하세요."}])
    except Exception as exc:  # noqa: BLE001 - 첫 요청이 안 되면 나머지는 물어볼 것이 없습니다
        logger.info("Model check could not reach %s: %s", target.model, type(exc).__name__)
        return profile([CheckResult(
            "answer", "기본 응답", FAIL,
            f"요청이 실패했습니다 ({_state_label(exc)}). 연결부터 확인하세요 — 나머지 점검은 하지 않았습니다.",
            detail=safe_detail(exc, target),
        )])

    results: List[CheckResult] = list(_judge_answer(first))
    steps: List[Callable[[], Awaitable[CheckResult]]] = [
        lambda: _check_stream(asker),
        lambda: _check_length(asker),
        lambda: _check_roles(asker),
    ]
    for step in steps:
        results.append(await _guarded(step, target))

    assistant_turn: Optional[Dict[str, Any]] = None
    try:
        tools_result, assistant_turn = await _check_tools(asker)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        tools_result = _crashed(exc, target)
    results.append(tools_result)
    if assistant_turn is not None:
        turn = assistant_turn
        results.append(await _guarded(lambda: _check_tool_result(asker, turn), target))
    else:
        results.append(CheckResult("tool_result", "도구 결과 뒤 마무리", SKIP,
                                   "모델이 낸 도구 호출이 없어 확인하지 않았습니다."))
    if tools_result.outcome != FAIL:
        results.append(await _guarded(lambda: _check_catalog(asker), target))
    else:
        results.append(CheckResult("catalog", "도구 목록에서 불러오기", SKIP, "도구 호출이 되지 않아 확인하지 않았습니다."))
    results.append(await _guarded(lambda: _check_image(asker, uses), target))
    results.append(await _guarded(lambda: _check_window(target, uses, http_transport), target))
    logger.info("Model check for %s: %d requests, worst=%s", target.model, asker.requests, profile(results).worst)
    return profile(results)


async def _guarded(step: Callable[[], Awaitable[CheckResult]], target: ModelTarget) -> CheckResult:
    """검사 하나가 터져도 나머지는 계속합니다."""
    try:
        return await step()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return _crashed(exc, target)


def _crashed(exc: BaseException, target: ModelTarget) -> CheckResult:
    logger.warning("Model check step crashed for %s: %r", target.model, exc)
    return CheckResult("error", "점검 오류", SKIP, f"점검 중 오류가 났습니다 ({type(exc).__name__}).",
                       detail=safe_detail(exc, target))


def targets_for_agents(keys: Sequence[str], config: Optional[RootConfig] = None) -> List[ModelTarget]:
    """에이전트 키가 쓰는 연결들. 모르는 키는 `KeyError` 로 알립니다."""
    config = config or get_config()
    targets = collect_targets(config)
    picked: List[ModelTarget] = []
    for key in keys:
        cfg = config.agents.get(key)
        if cfg is None:
            raise KeyError(key)
        for target in targets:
            if any(use.key == key for use in uses_of(target, config)) and target not in picked:
                picked.append(target)
    return picked


# ---------------------------------------------------------------- 권고 적용

APPLICABLE_SETTINGS = (SET_MAX_CONTEXT_WINDOW, SET_MAX_TOKENS, SET_TOOL_LOADING, SET_DROP_IMAGE)


def apply_suggestion(
    target_id: str,
    setting: str,
    value: Any,
    config: Optional[RootConfig] = None,
    *,
    config_path: Optional[str | Path] = None,
) -> List[str]:
    """권고 하나를 conf.json 에 적습니다. 바꾼 것을 한 줄씩 돌려주고, 바꿀 것이 없으면 빈 목록입니다.

    점검이 자동으로 부르지 않습니다. 사람이 `적용` 을 눌렀을 때만 불립니다 (ADR-056).

    - `max_context_window`: 그 연결을 쓰는 에이전트 가운데 값이 더 큰 것만 `value` 로 낮춥니다.
    - `max_tokens`: 그 연결을 쓰는 에이전트 가운데 값이 더 작은 것만 `value` 로 올립니다.
    - `allowed_modalities.image`: 그 연결을 쓰는 에이전트의 모달에서 이미지를 뺍니다.
    - `tool_loading.enabled`: 전역 설정입니다. 다른 모델을 쓰는 에이전트에도 적용됩니다.

    에이전트의 값은 새 대화부터 적용됩니다 (잠긴 대화는 시작할 때의 설정으로 계속 돕니다).
    """
    if setting not in APPLICABLE_SETTINGS:
        raise ValueError(f"적용할 수 없는 설정입니다: {setting}")
    config = config or get_config()
    path = Path(config_path) if config_path is not None else (active_config_path() or Path(DEFAULT_CONFIG_PATH))
    if setting == SET_TOOL_LOADING:
        wanted = bool(value)
        if config.tool_loading.enabled == wanted:
            return []
        set_tool_loading_enabled_in_conf_file(wanted, path)
        return [f"tool_loading.enabled → {'true' if wanted else 'false'} (전역: 모든 에이전트)"]

    target = next((t for t in collect_targets(config) if t.id == target_id), None)
    if target is None:
        raise ValueError("설정에서 그 연결을 찾지 못했습니다. 설정이 바뀌었으면 다시 점검해 주세요.")
    changed: List[str] = []
    for use in uses_of(target, config):
        if setting == SET_MAX_CONTEXT_WINDOW and use.max_context_window > int(value):
            set_agent_limits_in_conf_file(use.key, max_context_window=int(value), config_path=path)
            changed.append(f"{use.name}: max_context_window {use.max_context_window:,} → {int(value):,}")
        elif setting == SET_MAX_TOKENS and use.max_tokens < int(value):
            set_agent_limits_in_conf_file(use.key, max_tokens=int(value), config_path=path)
            changed.append(f"{use.name}: max_tokens {use.max_tokens:,} → {int(value):,}")
        elif setting == SET_DROP_IMAGE and use.takes_images:
            kept = [m for m in config.agents[use.key].allowed_modalities if m != "image"]
            set_agent_allowed_modalities_in_conf_file(use.key, kept, path)
            changed.append(f"{use.name}: 모달에서 이미지를 뺌")
    return changed


# ---------------------------------------------------------------- 저장


def modelcheck_dir() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "modelcheck"


def save_profile(profile: ModelProfile) -> Optional[Path]:
    """결과를 파일로 남깁니다. 쓰지 못해도 점검 결과는 돌려줍니다."""
    try:
        folder = modelcheck_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{profile.target_id}.json"
        path.write_text(json.dumps(profile.to_dict(detail=True), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except OSError as exc:
        logger.warning("Could not save the model check of %s: %s", profile.model, exc)
        return None


def load_profile(target_id: str) -> Optional[ModelProfile]:
    try:
        data = json.loads((modelcheck_dir() / f"{target_id}.json").read_text(encoding="utf-8"))
        return ModelProfile.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- 결과판 (화면)


class ModelCheckBoard:
    """프로세스 전체가 함께 보는 점검 결과판. 점검은 한 번에 한 연결만 돕니다."""

    def __init__(self, *, checker: Callable[..., Awaitable[ModelProfile]] = check_target) -> None:
        self._checker = checker
        self._profiles: Dict[str, ModelProfile] = {}
        self._task: Optional["asyncio.Task[None]"] = None
        self._running_id: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def running_id(self) -> Optional[str]:
        return self._running_id if self.running else None

    def start(self, target_id: str, config: Optional[RootConfig] = None) -> bool:
        """그 연결의 점검을 시작합니다. 다른 점검이 돌고 있거나 모르는 연결이면 False."""
        if self.running:
            return False
        target = next((t for t in collect_targets(config) if t.id == target_id), None)
        if target is None or not target.is_live:
            return False
        self._running_id = target_id
        self._task = asyncio.create_task(self._run(target, config))
        return True

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self, target: ModelTarget, config: Optional[RootConfig]) -> None:
        try:
            profile = await self._checker(target, config)
            self._profiles[target.id] = profile
            save_profile(profile)
        except Exception as exc:  # noqa: BLE001 - 점검이 터져도 화면은 살아 있어야 합니다
            logger.warning("Model check crashed for %s: %r", target.model, exc)
        finally:
            self._running_id = None

    def profile(self, target_id: str) -> Optional[ModelProfile]:
        """마지막 점검 결과. 이 프로세스에서 돌린 적이 없으면 파일에서 읽습니다."""
        if target_id not in self._profiles:
            loaded = load_profile(target_id)
            if loaded is None:
                return None
            self._profiles[target_id] = loaded
        return self._profiles[target_id]


_board: Optional[ModelCheckBoard] = None


def get_model_check_board() -> ModelCheckBoard:
    global _board
    if _board is None:
        _board = ModelCheckBoard()
    return _board


# ---------------------------------------------------------------- 글로 내보내기

_MARKS: Dict[str, str] = {OK: "[통과]", INFO: "[참고]", WARN: "[주의]", FAIL: "[실패]", SKIP: "[건너뜀]"}


def format_profile(profile: ModelProfile, *, detail: bool = True) -> str:
    """콘솔에 찍을 글."""
    lines = [
        f"■ {profile.model}  ({'결정 LLM' if profile.kind == SYSTEM1 else '생성형'})",
        f"  쓰는 곳: {' · '.join(profile.users) or '-'}",
    ]
    if detail and profile.endpoint:
        lines.append(f"  주소: {profile.endpoint}")
    lines.append(f"  결과: {summarize(profile)}")
    for result in profile.results:
        lines.append(f"  {_MARKS[result.outcome]} {result.title} — {result.summary}")
        if detail and result.detail:
            lines.append(f"      원문: {result.detail}")
        for suggestion in result.suggestions:
            lines.append(f"      → {suggestion.text}")
    return "\n".join(lines)
