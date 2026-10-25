"""LLM 토큰 및 응답 시간 계측 모듈: 발언 및 턴 단위 사용량 집계 (v1.5).

모든 LLM 호출은 `LLMCaller._complete_once`를 경유합니다.
해당 지점에서 호출 소요 시간을, 기본 구현인 `_complete_unthrottled`에서 응답의 `usage` 메타데이터를
**현재 활성화된 모든 사용량 측정 컨텍스트(Meter)**에 누적합니다.

측정기는 중첩(Nested) 컨텍스트로 동작합니다. 턴 실행 시 턴 단위 측정기가 활성화되고(`OrchestratorEngine`),
그 내부에서 발언마다 발언 단위 측정기가 추가로 열립니다(`SpeechRunner`).
따라서 전문가 발언에서 소모된 토큰은 발언 측정기와 턴 측정기 양쪽에 모두 누적되며,
결정 장부 갱신, 중간 요약, 라우팅 등 보조 시스템 호출은 턴 측정기에만 반영됩니다.
보조 호출의 소모량은 턴 전체 합계에서 각 발언 합계를 차감하여 정확히 산출할 수 있습니다.

활성 측정기 스택은 `contextvars`로 격리 관리되므로 도구 루프 등에 불필요한 인자 전달을 방지하며,
병렬 발언 태스크는 생성 시 상위 턴 측정기를 상속받고 발언 측정기는 각 태스크 내부에서 독립 격리됩니다.

토큰 집계는 각 LLM 프로바이더의 응답 메타데이터를 기반으로 합니다.
스트리밍 응답에서 프로바이더가 실시간 토큰을 제공하지 않는 경우 LiteLLM의 추정 토크나이저 카운트가 적용될 수 있습니다.
`_complete_unthrottled`를 재정의한 모의 호출기(테스트 대역, MADOWarGame)는 토큰 정보가 없을 경우 호출 횟수와 응답 시간만 계측합니다.
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
    # 프롬프트 캐시 적중 토큰 수 (prompt_tokens에 포함)
    cached_tokens: int = 0
    # 추론/사고(Reasoning) 토큰 수 (completion_tokens에 포함)
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
    """지정된 `meter`에 LLM 호출 사용량을 누적하는 컨텍스트 매니저를 생성합니다."""
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
    """단일 LLM 응답의 토큰 사용량을 활성화된 모든 측정 컨텍스트에 누적합니다."""
    meters = _METERS.get()
    if not meters:
        return
    usage = usage_of(response)
    for meter in meters:
        meter.add(usage)


def note_call(seconds: float) -> None:
    """단일 LLM 호출 횟수와 소요 시간을 활성 측정 컨텍스트에 누적합니다."""
    for meter in _METERS.get():
        meter.calls += 1
        meter.llm_seconds += max(0.0, seconds)


def format_tokens(count: int) -> str:
    """토큰 수치를 축약 표기 형식으로 변환합니다 (`12345` → `12.3k`)."""
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
