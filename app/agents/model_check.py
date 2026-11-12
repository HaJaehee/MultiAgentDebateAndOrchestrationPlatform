"""모델 적합성 점검 (ADR-054) — 단일 연결에 가벼운 요청 몇 건을 보내 해당 모델이 MADO에서 어느 기능까지 지원하는지 확인합니다.

`model_status`는 "현재 응답하는가"만 확인합니다. 폐쇄망에 새 모델을 반입한 뒤에는 그다음 동작이 관건입니다.
도구 호출이 `tool_calls`로 오는지 본문 텍스트로 새는지, 추론이 본문과 분리되어 오는지, 출력 한도에서 잘렸을 때
잘림 여부를 알려 주는지, 서버의 컨텍스트 창이 설정보다 작은지 등은 지금까지 실제 토론이 실패한 뒤에야
알 수 있었습니다(`wiki/agents/llm-integration.md` §2.1 · §2.6 · §2.7 · §5.2). 이 모듈에서는 모델 반입 당일 이를 사전에 점검합니다.

## 원칙

- **결정적 검사만 수행합니다.** 판정에 LLM을 쓰지 않으며, 응답 구조(필드·마커·종료 사유)와 정해 둔 키워드만 확인합니다.
  모델 품질은 여기서 평가하지 않습니다(품질 평가는 Bench의 역할입니다).
- **MADO가 실제로 보내는 형식 그대로 테스트합니다.** 마무리 호출의 `tool_choice="none"`, 지연 로딩 목록(`catalog_tool`) 등의
  요청은 실제 런타임 코드가 생성하는 형식을 그대로 사용합니다. 점검을 통과한 항목은 실제 토론에서도 동작해야 의미가 있기 때문입니다.
- **제안만 제공합니다.** 점검 결과에 따라 런타임 동작이 자동으로 바뀌지 않습니다. 권장 설정은 안내 문구와 `Suggestion`으로 제시하며,
  conf.json 수정은 사용자가 직접 결정합니다. 설정 키가 연결된 권고는 화면의 `적용` 버튼으로 한 번에 반영할 수 있으나
  (`apply_suggestion`, ADR-056), 이 역시 사용자가 눌러야 실행되며 서버 PC에서만 허용됩니다.
- **부하.** 생성형 연결 하나당 짧은 응답 요청 8건과 `/models` 조회 1건, 결정 LLM은 요청 1건만 보냅니다.
  서버 전체의 동시 요청 상한(`app.llm_concurrency`)을 준수하며 순차적으로 전송하고, 재시도는 하지 않습니다.
- **기록에는 크기와 상태만 남깁니다.** 오류 원문은 API 키를 마스킹한 한두 줄(`safe_detail`)만 기록하고, 모델이 생성한 본문은 저장하지 않습니다.

결과는 `data/modelcheck/<연결 id>.json`에 저장되며, 모델 연결 상태 화면과 `python -m app.modelcheck`가 함께 읽습니다.
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

# 점검 결과 등급
OK = "ok"          # 기대한 대로 동작합니다
INFO = "info"      # 참고 사항 — 수정할 내용은 없습니다
WARN = "warn"      # 일부 기능이 다르게 동작합니다. 설정을 살펴보세요
FAIL = "fail"      # 이 연결로는 해당 기능이 동작하지 않습니다
SKIP = "skip"      # 확인하지 못했습니다

OUTCOME_LABELS: Dict[str, str] = {OK: "통과", INFO: "참고", WARN: "주의", FAIL: "실패", SKIP: "건너뜀"}
# 연결 상태 화면과 동일한 톤 이름 (`app/ui/components/model_status.py`).
OUTCOME_TONES: Dict[str, str] = {OK: "good", INFO: "idle", WARN: "warn", FAIL: "bad", SKIP: "idle"}

CHECK_TIMEOUT_SECONDS = 60.0
MODELS_TIMEOUT_SECONDS = 10.0
# 짧은 응답을 확인하는 요청의 출력 한도입니다. 추론 모델은 숨겨진 추론 토큰도 이 한도를 함께 소비하므로 너무 작게 잡지 않습니다.
ANSWER_TOKENS = 512
# 잘림(length) 종료 사유를 반환하는지 확인하는 요청의 출력 한도입니다.
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

# 권장 설정 키(`Suggestion.setting`). 화면의 '적용' 버튼이 이 키로 conf.json의 해당 항목을 찾습니다.
SET_MAX_CONTEXT_WINDOW = "max_context_window"
SET_MAX_TOKENS = "max_tokens"
SET_TOOL_LOADING = "tool_loading.enabled"
SET_DROP_IMAGE = "allowed_modalities.image"

RECOMMENDED_REASONING_MAX_TOKENS = 8192


@dataclass(frozen=True)
class Suggestion:
    """권장 설정 항목. `setting`이 비어 있으면 conf.json으로 바꿀 수 없는 항목(서버 측 설정)입니다."""

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
    # 소유자(서버 측 관리자)에게만 표시하는 오류 원문 요약(키는 마스킹됨).
    detail: str = ""


@dataclass(frozen=True)
class AgentUse:
    """이 연결을 사용하는 에이전트 정보와 판정에 필요한 해당 에이전트 설정."""

    key: str
    name: str
    max_context_window: int
    max_tokens: int
    takes_images: bool


@dataclass(frozen=True)
class ModelProfile:
    """단일 연결의 적합성 점검 결과."""

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
        """저장 및 화면 표시용 딕셔너리입니다. `detail`이 False면 엔드포인트 주소와 오류 원문을 포함하지 않습니다."""
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


# ---------------------------------------------------------------- 응답 파싱


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first_choice(response: Any) -> Any:
    choices = _get(response, "choices") or []
    return choices[0] if choices else None


def _text_of(message: Any) -> str:
    content = _get(message, "content") or ""
    if isinstance(content, list):  # 콘텐츠 블록 목록으로 반환하는 Provider
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
    """단일 연결에 점검 요청을 보냅니다. 연결 정보는 연결 상태 확인(`probe_kwargs`)과 같으며, LLM 게이트 슬롯 제한을 준수합니다."""

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
        """스트리밍 없이 단건 요청을 보냅니다. 응답 객체를 반환하며, 예외가 발생하면 그대로 전파합니다."""
        kwargs = self._kwargs(messages, max_tokens, tools, tool_choice)
        self.requests += 1
        async with get_llm_gate().slot(GATE_HOLDER):
            return await asyncio.wait_for(llm_module.litellm.acompletion(**kwargs), self.timeout_seconds + 2.0)

    async def stream(self, messages: List[Dict[str, Any]], *, max_tokens: int = ANSWER_TOKENS) -> Tuple[int, int]:
        """실제 발언과 동일한 방식(`stream=True`, 추가 옵션 없음)으로 스트리밍 요청을 보냅니다. `(본문 청크 수, 추론 청크 수)`를 반환합니다.

        스트림 응답의 토큰 사용량은 여기서 확인하지 않습니다. LiteLLM은 서버가 보고하지 않더라도 마지막 청크에 토크나이저로 계산한
        값을 채워 넣으므로, 청크만으로는 서버 보고 여부를 구분할 수 없습니다(1.98 · 1.105에서 확인).
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
    """첫 응답 하나로 '기본 응답'과 '추론과 본문'을 함께 점검합니다."""
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
        answer = CheckResult("answer", "기본 응답", WARN, f"본문이 비어 있습니다 (finish_reason={finish or '없음'}) · {usage_note}")

    if not text and reasoning:
        if finish == "length":
            reason = CheckResult(
                "reasoning", "추론과 본문", WARN,
                f"추론이 응답 한도({ANSWER_TOKENS} 토큰)를 모두 소진해 본문이 비어 있습니다(추론 {len(reasoning):,}자).",
                (Suggestion(
                    f"이 모델은 숨겨진 추론 토큰도 max_tokens 한도를 함께 사용합니다. max_tokens를 {RECOMMENDED_REASONING_MAX_TOKENS:,} 이상으로 설정하세요.",
                    SET_MAX_TOKENS, RECOMMENDED_REASONING_MAX_TOKENS,
                ),),
            )
        else:
            reason = CheckResult(
                "reasoning", "추론과 본문", WARN,
                f"답변이 추론 필드(reasoning_content {len(reasoning):,}자)에만 있고 본문은 비어 있습니다.",
                (Suggestion(
                    "서버의 reasoning parser가 사고 종료 마커를 찾지 못하는 것으로 보입니다. 서버의 파서 설정을 확인하세요"
                    "(vLLM: --reasoning-parser). MADO는 결론 마커가 있으면 추론에서 답을 추출하지만, 그만큼 재질의가 발생할 수 있습니다."
                ),),
            )
    elif "<think>" in text or "</think>" in text:
        reason = CheckResult(
            "reasoning", "추론과 본문", WARN,
            "추론 내용이 본문에 섞여 반환됩니다(<think> 태그). 사고 과정이 발언과 다음 발언자의 프롬프트에 그대로 포함됩니다.",
            (Suggestion("서버가 추론을 reasoning_content로 분리해 반환하도록 reasoning parser를 활성화하세요(vLLM: --reasoning-parser)."),),
        )
    elif reasoning:
        reason = CheckResult("reasoning", "추론과 본문", OK, f"추론이 본문과 분리되어 반환됩니다(추론 {len(reasoning):,}자).")
    else:
        reason = CheckResult("reasoning", "추론과 본문", OK, "추론을 별도 필드로 반환하지 않는 모델입니다.")
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
            f"스트리밍 요청이 실패했습니다({_state_label(exc)}). MADO가 발언마다 비스트리밍으로 재요청하게 되어 "
            "답변이 한꺼번에 표시되고 매 발언마다 실패 요청이 한 건씩 추가됩니다.",
            detail=safe_detail(exc, asker.target),
        )
    if not text_chunks and not reasoning_chunks:
        return CheckResult("stream", title, WARN, "스트림은 열렸지만 텍스트 청크가 수신되지 않았습니다.")
    return CheckResult(
        "stream", title, OK,
        f"스트리밍 청크가 정상 수신됩니다(본문 {text_chunks}개"
        + (f", 추론 {reasoning_chunks}개" if reasoning_chunks else "") + ").",
    )


async def _check_length(asker: _Asker) -> CheckResult:
    title = "잘림 알림"
    try:
        response = await asker.ask(
            [{"role": "user", "content": "1부터 200까지의 수를 한 줄에 하나씩 모두 쓰세요."}],
            max_tokens=LENGTH_PROBE_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("length", title, SKIP, f"요청이 실패했습니다({_state_label(exc)}).",
                           detail=safe_detail(exc, asker.target))
    finish = str(_get(_first_choice(response), "finish_reason") or "")
    if finish == "length":
        return CheckResult("length", title, OK, "출력 한도에서 잘리면 finish_reason='length'로 알려 줍니다.")
    return CheckResult(
        "length", title, WARN,
        f"출력 한도 {LENGTH_PROBE_TOKENS} 토큰에서 종료되었지만 finish_reason='{finish or '없음'}'입니다. "
        "잘린 응답 이어 쓰기와 '여기서 잘렸습니다' 안내가 동작하지 않아, 중단된 발언이 정상 완료된 것처럼 기록됩니다.",
        (Suggestion("max_tokens를 넉넉하게 설정해 잘림 발생을 줄이세요. 서버나 게이트웨이가 finish_reason을 변경하는지도 확인하세요."),),
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
            f"동일한 역할(role)의 메시지가 연속되면 거절합니다({_state_label(exc)}). MADO는 연속 메시지를 병합해 전송하므로 실제 토론에는 영향이 없습니다.",
            detail=safe_detail(exc, asker.target),
        )
    return CheckResult("roles", title, OK, "동일한 역할의 메시지가 연속되어도 정상 처리합니다.")


def _tool_prompt() -> List[Dict[str, Any]]:
    return [{
        "role": "user",
        "content": (
            f"도구 {PROBE_TOOL_NAME}를 code=\"{PROBE_CODE}\"로 호출하세요. "
            "결과를 받으면 그 뜻을 그대로 한 줄로 답하세요."
        ),
    }]


async def _check_tools(asker: _Asker) -> Tuple[CheckResult, Optional[Dict[str, Any]]]:
    """도구 호출이 `tool_calls`로 반환되는지 점검합니다. 정상 반환 시 해당 어시스턴트 턴을 실제 루프 형식으로 함께 반환합니다."""
    title = "도구 호출"
    parser_hint = Suggestion(
        "서버의 도구 호출 옵션과 파서가 이 모델의 형식과 맞는지 확인하세요"
        "(vLLM: --enable-auto-tool-choice --tool-call-parser <모델 형식>)."
    )
    try:
        response = await asker.ask(_tool_prompt(), tools=[PROBE_TOOL])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "tools", title, FAIL,
            f"도구 정의가 포함된 요청을 받지 못합니다({_state_label(exc)}). 이 연결을 사용하는 에이전트는 MCP 도구를 쓸 수 없습니다.",
            (parser_hint,), detail=safe_detail(exc, asker.target),
        ), None
    message = _get(_first_choice(response), "message")
    raw_calls = _get(message, "tool_calls") or []
    if raw_calls:
        # 실제 루프와 동일한 파서(`_parse_tool_call`)를 사용합니다. 여기서 파싱되면 실제 토론에서도 파싱됩니다.
        parsed = [ToolCallMixin._parse_tool_call(call) for call in raw_calls]  # noqa: SLF001
        name, arguments, _call_id, readable = parsed[0]
        if not readable:
            size = len(str(arguments.get("raw", "")))
            return CheckResult("tools", title, WARN,
                               f"tool_calls로 반환되었으나 인자가 JSON 형식이 아닙니다(인자 {size}자).", (parser_hint,)), None
        if name == PROBE_TOOL_NAME and arguments.get("code") == PROBE_CODE:
            turn = ToolCallMixin._assistant_turn(message, parsed)  # noqa: SLF001
            return CheckResult("tools", title, OK, "도구 호출이 tool_calls로 반환되고 인자도 정상적으로 파싱됩니다."), turn
        return CheckResult("tools", title, WARN, "tool_calls로 반환되었으나 도구 이름 또는 인자가 요청과 일치하지 않습니다."), None
    if find_leaked_tool_call(_text_of(message)) is not None:
        return CheckResult(
            "tools", title, FAIL,
            "도구 호출이 본문 텍스트로 새어 나옵니다. 서버가 tool_call로 파싱하지 못해 도구가 실행되지 않습니다.",
            (parser_hint,),
        ), None
    return CheckResult(
        "tools", title, WARN, "도구를 호출하지 않고 일반 텍스트로 응답했습니다. 도구가 필요한 작업에서도 도구를 부르지 않을 수 있습니다.",
    ), None


async def _check_tool_result(asker: _Asker, assistant_turn: Dict[str, Any]) -> CheckResult:
    """도구 결과가 포함된 대화를 `tool_choice="none"`으로 마무리합니다(`_wrap_up_without_tools`와 동일한 형식).

    assistant 턴은 임의로 만들지 않고 **방금 모델이 반환한 응답**을 실제 루프 방식(`_assistant_turn`)으로 되돌려 넣습니다.
    일부 추론 모델(예: DeepSeek thinking mode)은 도구 호출 턴의 `reasoning_content`를 다음 요청에 그대로 전달받아야 하므로,
    수동으로 구성한 턴을 넣으면 정상 모델에서도 요청이 거절될 수 있습니다.
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
                f"도구 결과가 포함된 대화를 처리하지 못합니다({_state_label(again)}). 도구를 한 번 호출한 뒤의 후속 발언이 모두 실패합니다.",
                detail=safe_detail(again, asker.target),
            )
        return CheckResult(
            "tool_result", title, WARN,
            f"tool_choice='none'을 지원하지 않습니다({_state_label(exc)}). 도구 한도에 도달한 발언의 마무리와 "
            "잘린 응답 이어 쓰기가 실패합니다.",
            detail=first,
        )
    message = _get(_first_choice(response), "message")
    if _tool_calls_of(message):
        return CheckResult("tool_result", title, WARN,
                           "tool_choice='none'인데도 도구를 다시 호출했습니다. 마무리 단계에서 호출된 도구는 실행되지 않고 무시됩니다.")
    if PROBE_MEANING.split()[0] in _text_of(message):
        return CheckResult("tool_result", title, OK, "도구 결과를 읽고 응답하며, tool_choice='none'을 준수합니다.")
    return CheckResult("tool_result", title, INFO, "요청은 수락했지만 응답에 도구 결과가 반영되지 않았습니다.")


async def _check_catalog(asker: _Asker) -> CheckResult:
    """지연 로딩 목록(ADR-039)을 읽고 도구를 불러오는지 확인합니다. 실제 발언과 동일한 목록 도구 및 시스템 안내를 사용합니다."""
    title = "도구 목록에서 불러오기"
    tools = [catalog_tool([PROBE_TOOL])]
    messages = [
        {"role": "system", "content": tool_loading_guidance(tools) or ""},
        {"role": "user", "content": f"코드 {PROBE_CODE}의 뜻을 도구로 조회해 주세요."},
    ]
    try:
        response = await asker.ask(messages, tools=tools)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("catalog", title, SKIP, f"요청이 실패했습니다({_state_label(exc)}).",
                           detail=safe_detail(exc, asker.target))
    names = [name for name, _ in _tool_calls_of(_get(_first_choice(response), "message"))]
    if any(is_load_tool(name) for name in names):
        return CheckResult("catalog", title, OK, "도구 목록을 읽고 필요한 도구를 불러옵니다.")
    if PROBE_TOOL_NAME in names:
        return CheckResult("catalog", title, OK, "목록의 도구를 곧바로 호출했습니다. MADO는 이 호출도 실행하고 도구 정의를 함께 로드합니다.")
    return CheckResult(
        "catalog", title, WARN,
        "목록에 있는 도구를 불러오지 않았습니다. 지연 로딩이 켜져 있으면 이 모델은 목록 내 도구를 없는 것으로 인식할 수 있습니다.",
        (Suggestion(
            "이 모델을 주로 사용한다면 tool_loading.enabled를 false로 설정해 모든 도구 정의를 처음부터 포함하거나, "
            "자주 쓰는 도구를 tool_loading.core_tools에 추가하세요.",
            SET_TOOL_LOADING, False,
        ),),
    )


def _red_png(size: int = 32) -> bytes:
    """단색(빨강) PNG 이미지를 생성합니다. 표준 라이브러리만 사용합니다."""
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
        f"이 연결을 사용하는 {', '.join(takers)}의 입력 모달리티(allowed_modalities)에서 이미지를 비활성화하세요. 켜 두면 첨부 이미지가 전달되지 않거나 발언 요청이 거절될 수 있습니다.",
        SET_DROP_IMAGE, False,
    ),) if takers else ()
    data = base64.b64encode(_red_png()).decode("ascii")
    try:
        response = await asker.ask([{"role": "user", "content": [
            {"type": "text", "text": "이 이미지는 한 가지 색으로만 칠해져 있습니다. 그 색 이름을 영어 한 단어로 답하세요."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
        ]}])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "image", title, WARN if takers else INFO,
            f"이미지가 포함된 요청을 처리하지 못합니다({_state_label(exc)}).",
            drop, detail=safe_detail(exc, asker.target),
        )
    if "red" in _text_of(_get(_first_choice(response), "message")).lower():
        return CheckResult("image", title, OK, "이미지를 입력받아 내용을 인식합니다.")
    return CheckResult(
        "image", title, WARN if takers else INFO,
        "요청은 수락되었지만 이미지 내용을 인식하지 못했습니다. 중간 경로(게이트웨이·LiteLLM)에서 이미지가 누락되었을 수 있습니다.",
        drop,
    )


def _bare_model(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


async def _server_window(target: ModelTarget, transport: Optional[httpx.AsyncBaseTransport]) -> Optional[int]:
    """서버가 알려 주는 컨텍스트 창 크기입니다. OpenAI 호환 `/models` 응답의 `max_model_len`(vLLM 등)을 우선 읽고, 없으면 LiteLLM 모델 정보를 조회합니다."""
    if target.api_base:
        headers = {"Authorization": f"Bearer {target.api_key}"} if (target.api_key or "").strip() else {}
        headers.update(dict(target.extra_headers))
        try:
            async with httpx.AsyncClient(timeout=MODELS_TIMEOUT_SECONDS, transport=transport) as client:
                reply = await client.get(target.api_base.rstrip("/") + "/models", headers=headers)
            entries = reply.json().get("data") if reply.status_code == 200 else None
        except Exception:  # noqa: BLE001 - 창 크기를 조회하지 못하더라도 점검 실패로 처리하지 않습니다
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
            "서버가 컨텍스트 창 크기를 제공하지 않습니다. 모델 카드의 값을 max_context_window에 직접 맞춰 주세요 — "
            "설정값이 실제 한도보다 크면 MADO가 컨텍스트를 줄이기 전에 서버가 먼저 요청을 거절합니다.",
        )
    over = [use for use in uses if use.max_context_window > window]
    if over:
        names = ", ".join(f"{use.name}({use.max_context_window:,})" for use in over)
        return CheckResult(
            "window", title, FAIL,
            f"서버의 컨텍스트 창은 {window:,} 토큰인데 에이전트 설정이 더 큽니다: {names}. 긴 토론에서 MADO가 컨텍스트를 축소하기 전에 서버가 요청을 거절합니다.",
            (Suggestion(f"max_context_window를 {window:,} 이하로 낮추세요.", SET_MAX_CONTEXT_WINDOW, window),),
        )
    if not uses:
        return CheckResult("window", title, INFO, f"서버의 컨텍스트 창은 {window:,} 토큰입니다. 이 연결을 사용하는 에이전트가 없어 비교할 설정이 없습니다.")
    return CheckResult("window", title, OK, f"서버의 컨텍스트 창({window:,} 토큰) 범위 내에 에이전트 설정이 들어 있습니다.")


# ---------------------------------------------------------------- 검사 (결정 LLM)

_S1_QUESTIONS: Dict[str, DecisionQuestion] = {
    "noul": DecisionQuestion(type="noul", instructions="이 보고는 좋은 소식인가?"),
    "choice": DecisionQuestion(type="choice", instructions="다음에 할 일을 고르시오.", criteria=("go", "stop"),
                               choice_notes=("계속 진행한다", "멈춘다")),
    "score": DecisionQuestion(type="score", instructions="이 보고의 확실함은 어느 정도인가?", criteria=("낮음", "보통", "높음")),
}
_S1_STATE = "테스트 12건이 모두 통과했고 남은 결함은 없다."


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
        failed = CheckResult("systemone", "결정 LLM 응답", FAIL, f"요청이 실패했습니다({_state_label(exc)}).",
                             detail=safe_detail(exc, target))
        return [failed, *(CheckResult(f"s1_{key}", title, SKIP, "응답이 없어 확인하지 못했습니다.") for key, title in titles.items())]

    results = [CheckResult("systemone", "결정 LLM 응답", OK, f"단일 요청으로 질문 {len(_S1_QUESTIONS)}건에 응답했습니다.")]
    noul = response.answers.get("noul")
    results.append(
        CheckResult("s1_noul", titles["noul"], OK, "0과 1 사이의 확률을 반환합니다.")
        if noul is not None and noul.noul is not None and 0.0 <= noul.noul <= 1.0
        else CheckResult("s1_noul", titles["noul"], FAIL,
                         "확률을 읽지 못했습니다. 발언자 선정·gate 분기·서브 토론 판정이 모두 '아니오' 쪽으로 치우칩니다.")
    )
    choice = response.answers.get("choice")
    criteria = _S1_QUESTIONS["choice"].criteria or ()
    if choice is None or choice.choice not in criteria:
        results.append(CheckResult("s1_choice", titles["choice"], FAIL, "선택지 이름으로 응답하지 않았습니다."))
    elif not choice.probabilities or not set(choice.probabilities) <= set(criteria):
        results.append(CheckResult("s1_choice", titles["choice"], WARN, "선택지는 반환했으나 선택지별 확률이 없거나 이름이 일치하지 않습니다."))
    else:
        results.append(CheckResult("s1_choice", titles["choice"], OK, "선택지와 선택지별 확률을 모두 반환합니다."))
    score = response.answers.get("score")
    results.append(
        CheckResult("s1_score", titles["score"], OK, "점수를 반환합니다.")
        if score is not None and score.score is not None
        else CheckResult("s1_score", titles["score"], FAIL, "점수를 읽지 못했습니다.")
    )
    return results


# ---------------------------------------------------------------- 연결 하나 점검


def uses_of(target: ModelTarget, config: Optional[RootConfig] = None) -> List[AgentUse]:
    """이 연결을 사용하는 활성화된 에이전트 목록을 반환합니다. 결정 LLM을 별도로 쓰는 전문가는 생성형 연결의 사용자에 포함하지 않습니다."""
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
    """단일 연결을 점검합니다. 예외를 전파하지 않고 결과 객체로 반환합니다."""
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
        first = await asker.ask([{"role": "user", "content": "17 + 25는 얼마입니까? 숫자만 답하세요."}])
    except Exception as exc:  # noqa: BLE001 - 첫 요청이 실패하면 후속 점검을 진행할 수 없습니다
        logger.info("Model check could not reach %s: %s", target.model, type(exc).__name__)
        return profile([CheckResult(
            "answer", "기본 응답", FAIL,
            f"요청이 실패했습니다({_state_label(exc)}). 먼저 연결 설정을 확인하세요 — 나머지 점검은 건너뛰었습니다.",
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
                                   "모델이 유효한 도구 호출을 생성하지 않아 확인하지 않았습니다."))
    if tools_result.outcome != FAIL:
        results.append(await _guarded(lambda: _check_catalog(asker), target))
    else:
        results.append(CheckResult("catalog", "도구 목록에서 불러오기", SKIP, "도구 호출이 실패해 확인하지 않았습니다."))
    results.append(await _guarded(lambda: _check_image(asker, uses), target))
    results.append(await _guarded(lambda: _check_window(target, uses, http_transport), target))
    logger.info("Model check for %s: %d requests, worst=%s", target.model, asker.requests, profile(results).worst)
    return profile(results)


async def _guarded(step: Callable[[], Awaitable[CheckResult]], target: ModelTarget) -> CheckResult:
    """개별 점검 항목에서 오류가 나더라도 나머지 점검은 계속 진행합니다."""
    try:
        return await step()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return _crashed(exc, target)


def _crashed(exc: BaseException, target: ModelTarget) -> CheckResult:
    logger.warning("Model check step crashed for %s: %r", target.model, exc)
    return CheckResult("error", "점검 오류", SKIP, f"점검 중 오류가 발생했습니다({type(exc).__name__}).",
                       detail=safe_detail(exc, target))


def targets_for_agents(keys: Sequence[str], config: Optional[RootConfig] = None) -> List[ModelTarget]:
    """지정한 에이전트 키들이 사용하는 연결 목록을 반환합니다. 알 수 없는 키는 `KeyError`를 발생시킵니다."""
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
    """권장 설정 하나를 conf.json에 반영합니다. 변경된 내역을 한 줄씩 반환하며, 바꿀 항목이 없으면 빈 목록을 반환합니다.

    점검 시 자동으로 호출되지 않고, 사용자가 `적용` 버튼을 눌렀을 때만 실행됩니다(ADR-056).

    - `max_context_window`: 이 연결을 사용하는 에이전트 중 설정값이 `value`보다 큰 항목만 `value`로 낮춥니다.
    - `max_tokens`: 이 연결을 사용하는 에이전트 중 설정값이 `value`보다 작은 항목만 `value`로 높입니다.
    - `allowed_modalities.image`: 이 연결을 사용하는 에이전트의 입력 모달리티에서 이미지를 제외합니다.
    - `tool_loading.enabled`: 전역 설정이므로 다른 모델을 쓰는 에이전트에도 함께 적용됩니다.

    에이전트 설정은 새 대화부터 반영됩니다(진행 중인 잠긴 대화는 시작 시점의 설정으로 계속 실행됩니다).
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
        raise ValueError("설정에서 해당 연결을 찾을 수 없습니다. 설정이 변경되었다면 다시 점검해 주세요.")
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
            changed.append(f"{use.name}: 입력 모달리티에서 image 제외")
    return changed


# ---------------------------------------------------------------- 저장


def modelcheck_dir() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "modelcheck"


def save_profile(profile: ModelProfile) -> Optional[Path]:
    """점검 결과를 파일로 저장합니다. 저장에 실패하더라도 점검 결과는 그대로 반환합니다."""
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


# ---------------------------------------------------------------- 결과 보드 (화면)


class ModelCheckBoard:
    """프로세스 전체가 공유하는 점검 결과 보드입니다. 한 번에 하나의 연결만 점검합니다."""

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
        """해당 연결의 점검을 시작합니다. 다른 점검이 실행 중이거나 알 수 없는 연결이면 False를 반환합니다."""
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
        except Exception as exc:  # noqa: BLE001 - 점검 중 예외가 발생해도 화면은 정상 유지되어야 합니다
            logger.warning("Model check crashed for %s: %r", target.model, exc)
        finally:
            self._running_id = None

    def profile(self, target_id: str) -> Optional[ModelProfile]:
        """마지막 점검 결과를 반환합니다. 현재 프로세스에서 실행한 이력이 없으면 파일에서 불러옵니다."""
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


# ---------------------------------------------------------------- 텍스트 내보내기

_MARKS: Dict[str, str] = {OK: "[통과]", INFO: "[참고]", WARN: "[주의]", FAIL: "[실패]", SKIP: "[건너뜀]"}


def format_profile(profile: ModelProfile, *, detail: bool = True) -> str:
    """콘솔 출력용 문자열을 생성합니다."""
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
