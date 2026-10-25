"""LLM 사용량 계측: 토큰과 호출 시간을 발언·턴 단위로 모읍니다.

모든 LLM 호출은 `LLMCaller._complete_once` 를 지나갑니다. 거기서 호출 시간을, 기본 구현의
`_complete_unthrottled` 에서 응답의 `usage` 를 **지금 열려 있는 계량기 모두에** 더합니다.

계량기는 겹쳐 열립니다. 턴이 하나를 열고(`OrchestratorEngine`), 그 안에서 발언이 하나를 더
엽니다(`SpeechRunner`). 그래서 발언의 토큰은 발언 계량기와 턴 계량기에 함께 들어가고, 장부·요약·
라우팅 같은 보조 호출은 턴 계량기에만 들어갑니다. 보조 호출 몫은 턴 합계에서 발언 합계를 빼서
얻습니다.

계량기 목록은 컨텍스트 변수에 둡니다. 도구 루프 곳곳에 인자를 늘리지 않으려는 것이고
(`_SpeechProgress` 와 같은 방식), 병렬 발언 태스크는 만들어질 때 턴 계량기를 물려받고 자기
발언 계량기는 자기 태스크 안에서만 봅니다.

토큰 수는 Provider 가 알려 준 값입니다. 스트리밍 응답에 Provider 가 사용량을 싣지 않으면
LiteLLM 이 토크나이저로 센 값이라 실제와 조금 다를 수 있습니다. `_complete_unthrottled` 를
바꿔 끼운 호출기(테스트 대역, MADOWarGame)는 토큰을 알리지 않으므로 호출 수와 시간만 셉니다.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from pydantic import BaseModel

_FIELDS = (
    "prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "calls", "llm_seconds",
)


class Usage(BaseModel):
    """토큰과 호출 시간의 합. 발언 기록(`llm_meta.usage`)과 턴 기록(`turns.usage`)에 이 모양으로 남습니다."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 입력 중 프롬프트 캐시에서 읽은 몫 (입력에 이미 들어 있습니다)
    cached_tokens: int = 0
    # 출력 중 사고 과정의 몫 (출력에 이미 들어 있습니다)
    reasoning_tokens: int = 0
    calls: int = 0
    llm_seconds: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def empty(self) -> bool:
        return self.calls == 0 and self.total_tokens == 0

    def add(self, other: "Usage") -> None:
        for name in _FIELDS:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def minus(self, other: "Usage") -> "Usage":
        """뺀 값. 음수는 0 으로 (반올림 차이로 시간이 조금 넘칠 수 있습니다)."""
        return Usage(**{name: max(0, getattr(self, name) - getattr(other, name)) for name in _FIELDS})

    def as_meta(self) -> Dict[str, Any]:
        data = self.model_dump()
        data["llm_seconds"] = round(self.llm_seconds, 2)
        return data

    @classmethod
    def of(cls, data: Any) -> "Usage":
        """기록에서 읽은 값. 모양이 어긋나면 빈 값입니다."""
        if not isinstance(data, Mapping):
            return cls()
        values: Dict[str, Any] = {}
        for name in _FIELDS:
            raw = data.get(name)
            try:
                values[name] = float(raw) if name == "llm_seconds" else int(raw)
            except (TypeError, ValueError):
                continue
        return cls(**values)


_METERS: "contextvars.ContextVar[Tuple[Usage, ...]]" = contextvars.ContextVar("mado_usage_meters", default=())


@contextmanager
def measuring(meter: Optional[Usage] = None) -> Iterator[Usage]:
    """이 블록 안의 LLM 호출을 `meter` 에 더합니다 (주지 않으면 새 계량기). 바깥 계량기에도 그대로 더해집니다."""
    meter = meter if meter is not None else Usage()
    token = _METERS.set(_METERS.get() + (meter,))
    try:
        yield meter
    finally:
        _METERS.reset(token)


def _read(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_of(response: Any) -> Usage:
    """LiteLLM 응답의 `usage` 를 읽습니다. 없으면 빈 값입니다.

    캐시 토큰은 OpenAI 식(`prompt_tokens_details.cached_tokens`)과 Anthropic 식
    (`cache_read_input_tokens`)을 모두 봅니다.
    """
    raw = _read(response, "usage")
    if raw is None:
        return Usage()
    cached = _count(_read(_read(raw, "prompt_tokens_details"), "cached_tokens")) or _count(
        _read(raw, "cache_read_input_tokens")
    )
    return Usage(
        prompt_tokens=_count(_read(raw, "prompt_tokens")),
        completion_tokens=_count(_read(raw, "completion_tokens")),
        cached_tokens=cached,
        reasoning_tokens=_count(_read(_read(raw, "completion_tokens_details"), "reasoning_tokens")),
    )


def note_response(response: Any) -> None:
    """응답 하나의 토큰을 열려 있는 계량기 모두에 더합니다."""
    meters = _METERS.get()
    if not meters:
        return
    usage = usage_of(response)
    for meter in meters:
        meter.add(usage)


def note_call(seconds: float) -> None:
    """LLM 호출 한 번(성공이든 실패든)과 걸린 시간을 더합니다."""
    for meter in _METERS.get():
        meter.calls += 1
        meter.llm_seconds += max(0.0, seconds)


def format_tokens(count: int) -> str:
    """`12345` → `12.3k`. 화면과 저장 문서가 같은 표기를 씁니다."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def usage_line(usage: Any) -> str:
    """`입력 12.3k · 출력 1.1k · LLM 8.2초`. 토큰을 모르면 시간만, 둘 다 없으면 빈 문자열."""
    value = usage if isinstance(usage, Usage) else Usage.of(usage)
    if value.empty:
        return ""
    parts = []
    if value.total_tokens:
        parts.append(f"입력 {format_tokens(value.prompt_tokens)}")
        if value.cached_tokens:
            parts[-1] += f" (캐시 {format_tokens(value.cached_tokens)})"
        parts.append(f"출력 {format_tokens(value.completion_tokens)}")
    if value.llm_seconds:
        parts.append(f"LLM {value.llm_seconds:.1f}초")
    return " · ".join(parts)
