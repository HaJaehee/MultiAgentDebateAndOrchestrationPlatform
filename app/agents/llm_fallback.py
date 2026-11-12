"""개별 모델 연결이 실패하면 전역 기본 연결 또는 지정된 대체 연결로 폴백합니다 (`llm.fallback`, ADR-030).

에이전트마다 다른 모델을 연결할 수 있습니다. 그중 하나의 API 키가 만료되거나 서버가 내려가면, 지금까지는
해당 에이전트의 발언 차례에 "연결 끊김" 오류만 남았습니다. 폴백을 활성화하면 그 자리를 전역 기본 모델(`llm`)이나
대체 연결이 동일한 페르소나로 대신 응답합니다.

## 지어내지 않되, 숨기지도 않습니다

`LLMUnavailableError`가 대체 응답을 만들지 않는 이유는 그대로입니다 — 조용히 바뀐 답변은 다음 에이전트의
입력과 최종 합성까지 오염시킵니다. 폴백은 실제 모델이 답하는 것이지만, **누가 답했는지가 달라졌다는
사실**은 실패와 같은 무게로 남아야 합니다. 그래서:

- 기본은 꺼져 있고(`llm.fallback.enabled`), 에이전트마다 막을 수 있습니다(`allow_fallback: false`).
- 전환할 때마다 `FallbackNotice` 를 올립니다. 엔진은 그것을 발언 기록(`llm_meta`)과 화면에 남깁니다.

알림은 `call_agent` 의 인자가 아니라 컨텍스트 변수(`listening`)로 받습니다. 발언을 여는 쪽이 그 발언
동안만 걸어 두면 됩니다 — `LLM_HOLDER` 가 대화 id 를 넘기는 것과 같은 방식이고, `call_agent` 와 같은
모양으로 만든 호출기들이 인자 하나 때문에 깨지지 않습니다. 걸어 둔 곳이 없으면 로그에만 남습니다.

## 언제 폴백하는가

1. **연결 계층 문제일 때만.** 인증 실패, 모델명 오류, 연결 실패, 응답 시간 초과, 서버 오류가 대상입니다. 요청 거절(400)과
   컨텍스트 초과는 요청 자체의 문제라 대체 모델도 동일하게 실패하며, 속도 제한(429)은 잠시 뒤 해소됩니다.
2. **모델이 아무 결과도 내놓기 전에만.** 텍스트가 한 청크라도 화면에 스트리밍되었거나 도구를 호출한 뒤에는 폴백하지
   않습니다. 같은 응답이 중복 출력되거나, 한 모델이 만든 도구 호출 이력을 다른 모델에 넘겨 Provider가
   거절하는 문제를 막기 위함입니다. 중단된 발언을 이어 가는 경우에도 같은 이유로 폴백하지 않습니다. 이 판단은
   `LLMCaller.call_agent`가 담당합니다.
3. **전환 가능한 대상이 있을 때만.** 에이전트가 이미 전역 연결을 쓰고 있거나 대체 연결 정보가 없으면 기존과 동일하게
   실패합니다.

## 무엇을 교체하는가

연결 정보(모델명, API 주소, 키, 버전, 프로바이더, 추가 헤더 및 본문)와 해당 모델의 처리 한도 및 통신 제어 설정(`max_tokens`,
`max_context_window`, `timeout`, `num_retries`, `drop_params`, `prompt_cache`)은 대체 연결 설정으로 교체합니다. 페르소나, 도구, 스킬,
온도(temperature), 도구 루프 상한은 기존 에이전트 설정을 유지합니다. 프로바이더 고유의 사고 옵션(`native`)을 사용하던 에이전트는
전역 또는 대상 연결의 단계적 사고 설정을 따릅니다.

전역 연결은 **현재 시점의** 설정(`get_config().llm`)을 사용합니다. 잠긴 대화 스냅샷에는 전역 값이 별도로 없고, API 키를 교체한
뒤에도 정상 동작해야 하기 때문입니다. 따라서 실제로 어느 모델이 응답했는지는 스냅샷이 아니라 각 발언 기록에 남깁니다.

## 폴백 연결이 여러 개일 때 (ADR-056)

`llm.fallback.order`에 연결을 순서대로 지정하면 나열된 순서대로 폴백을 시도합니다. 에이전트 키는 해당 에이전트의 연결을, `"llm"`은
전역 기본 연결을 가리킵니다. 비워 두면 전역 연결 하나만 사용합니다. 한 후보가 **아무 결과도 내놓기 전에** 연결 문제로 실패하면
다음 후보로 넘어가며, 응답이 스트리밍되었거나 도구를 호출한 뒤의 실패는 넘기지 않습니다(위 2번과 동일한 이유). 후보 전환 시마다
알림을 발행하므로 발언 기록에는 최종 응답한 모델이 남습니다. 목록에 없는 연결로는 폴백하지 않습니다 — 어느 데이터가 어떤 서버로
전달될 수 있는지는 사용자가 명시한 목록으로만 제한됩니다. 결정 LLM을 사용하는 전문가와 비활성화된 에이전트는 후보가 되지 않습니다.

## 차단기

응답하지 않는 연결을 매 발언마다 다시 호출하면 매번 타임아웃과 재시도 지연을 모두 겪게 됩니다. 한 번 실패한 연결은
`retry_after`초 동안 즉시 대체 모델로 우회하고, 시간이 지나면 한 차례 다시 시도합니다. `모델 연결 상태` 화면에서
해당 연결의 정상 응답을 확인하면 즉시 차단기를 해제합니다. 폴백 체인의 후보도 실패하면 동일한 차단기에 등록되어 일정 시간 건너뜁니다.
단, **마지막 후보**는 차단기에 등록하거나 건너뛰지 않습니다 — 더 이상 전환할 대상이 없으므로 매번 직접 시도합니다.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.connection import (
    AUTH,
    CONNECTION_FIELDS,
    MODEL,
    SERVER_ERROR,
    STATE_INFO,
    TIMEOUT,
    UNREACHABLE,
    classify,
    connection_id,
    connection_signature,
    global_entry,
)
from app.config import ORCHESTRATOR_KEY, LLMConfig, RootConfig, get_config

logger = logging.getLogger(__name__)

__all__ = [
    "FALLBACK_STATES", "GLOBAL_CONNECTION", "FallbackBreaker", "FallbackCandidate", "FallbackNotice", "FallbackRoute",
    "classify", "current_listener", "get_fallback_breaker", "listening", "route_for", "skip_remaining",
    "substitute_for", "substitutes_for",
]

# `llm.fallback.order`에서 전역 기본 연결을 가리키는 예약어입니다.
GLOBAL_CONNECTION = "llm"

# 폴백 전환 대상 실패 유형입니다. 연결 자체가 불가능한 오류만 포함합니다.
FALLBACK_STATES = frozenset({AUTH, MODEL, UNREACHABLE, TIMEOUT, SERVER_ERROR})

# 전역에서 함께 가져오는 것: 그 모델의 용량과 전송 설정.
_CAPACITY_FIELDS = ("max_tokens", "max_context_window", "timeout", "num_retries", "drop_params", "prompt_cache")


@dataclass(frozen=True)
class FallbackNotice:
    """한 번의 전환. `skipped` 는 조금 전에 실패한 연결이라 원래 모델을 아예 부르지 않았다는 뜻입니다."""

    agent_key: str
    agent_name: str
    intended_model: str
    model: str
    state: str
    skipped: bool = False

    @property
    def label(self) -> str:
        return STATE_INFO[self.state][0]

    def as_meta(self) -> Dict[str, Any]:
        """발언 기록(`MessageModel.llm_meta`)에 남는 모양. 주소와 키는 담지 않습니다."""
        return {
            "fallback": True,
            "model": self.model,
            "intended_model": self.intended_model,
            "reason": self.state,
            "reason_label": self.label,
        }


# 전환을 알릴 곳. 동기 함수든 코루틴 함수든 됩니다. 병렬 발언은 태스크마다 컨텍스트가 따로입니다.
FallbackListener = Callable[[FallbackNotice], Any]
_LISTENER: "contextvars.ContextVar[Optional[FallbackListener]]" = contextvars.ContextVar(
    "mado_fallback_listener", default=None
)


@contextmanager
def listening(listener: Optional[FallbackListener]) -> Iterator[None]:
    """이 블록 안에서 일어나는 전환을 `listener` 에게 알립니다."""
    token = _LISTENER.set(listener)
    try:
        yield
    finally:
        _LISTENER.reset(token)


def current_listener() -> Optional[FallbackListener]:
    return _LISTENER.get()


class FallbackBreaker:
    """실패한 연결을 일정 시간 건너뛰도록 관리합니다. 연결 식별자(`connection_id`) 기준으로 구분합니다."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # 연결 -> (실패 시각, 실패 유형)
        self._tripped: Dict[str, Tuple[float, str]] = {}

    def trip(self, connection: str, state: str) -> None:
        self._tripped[connection] = (self._clock(), state)

    def close(self, connection: str) -> bool:
        """해당 연결이 복구되었음을 확인했을 때 호출합니다. 건너뛰던 상태였다면 True를 반환합니다."""
        return self._tripped.pop(connection, None) is not None

    def reset(self) -> None:
        self._tripped.clear()

    def remaining(self, connection: str, retry_after: float) -> Optional[float]:
        """해당 연결을 건너뛰는 중이면 남은 시간(초)을 반환합니다. 만료되었으면 기록을 지우고 None을 반환해 다음 호출 시 재시도합니다."""
        entry = self._tripped.get(connection)
        if entry is None:
            return None
        left = retry_after - (self._clock() - entry[0])
        if left <= 0:
            del self._tripped[connection]
            return None
        return left

    def state(self, connection: str, retry_after: float) -> Optional[str]:
        """해당 연결을 건너뛰는 중이면 실패 유형을 반환합니다."""
        if self.remaining(connection, retry_after) is None:
            return None
        return self._tripped[connection][1]


_breaker: Optional[FallbackBreaker] = None


def get_fallback_breaker() -> FallbackBreaker:
    global _breaker
    if _breaker is None:
        _breaker = FallbackBreaker()
    return _breaker


@dataclass(frozen=True)
class FallbackCandidate:
    """폴백 후보 연결 하나를 나타냅니다. `name`은 `llm.fallback.order`에 지정된 이름이고 `agent`는 해당 연결로 교체한 에이전트 사본입니다."""

    name: str
    agent: Agent

    @property
    def connection(self) -> str:
        return connection_id(self.agent)

    @property
    def is_global(self) -> bool:
        return self.name == GLOBAL_CONNECTION

    @property
    def title(self) -> str:
        """오류 메시지에 표시하는 이름입니다: 전역 연결은 '기본 모델', 다른 에이전트의 연결은 '대체 모델'입니다."""
        return "기본 모델" if self.is_global else "대체 모델"


@dataclass
class FallbackRoute:
    """이 에이전트의 폴백 경로입니다. `candidates`는 시도할 폴백 순서대로 정렬되어 있습니다(최소 1개 이상)."""

    primary: Agent
    connection: str
    retry_after: float
    breaker: FallbackBreaker
    candidates: List[FallbackCandidate] = field(default_factory=list)

    @property
    def substitute(self) -> Agent:
        """최우선 폴백 연결을 적용한 에이전트 사본입니다."""
        return self.candidates[0].agent

    def known_failure(self) -> Optional[str]:
        """직전에 실패해 건너뛰는 중이면 해당 실패 유형을 반환합니다."""
        return self.breaker.state(self.connection, self.retry_after)

    def trip(self, state: str) -> None:
        self.breaker.trip(self.connection, state)

    def is_last(self, candidate: FallbackCandidate) -> bool:
        return candidate is self.candidates[-1]

    def skipping(self, candidate: FallbackCandidate) -> bool:
        """이 후보를 직전 실패 때문에 건너뛸지 여부를 반환합니다. 마지막 후보는 건너뛰지 않습니다."""
        if self.is_last(candidate):
            return False
        return self.breaker.state(candidate.connection, self.retry_after) is not None

    def trip_candidate(self, candidate: FallbackCandidate, state: str) -> None:
        """후보 연결이 실패했을 때 호출합니다. 다음 발언이 타임아웃을 반복하지 않도록 일정 시간 건너뜁니다(마지막 후보는 차단기에 등록하지 않습니다)."""
        if not self.is_last(candidate):
            self.breaker.trip(candidate.connection, state)

    def notice(self, state: str, *, skipped: bool, candidate: Optional[FallbackCandidate] = None) -> FallbackNotice:
        answering = candidate.agent if candidate is not None else self.substitute
        return FallbackNotice(
            agent_key=self.primary.key, agent_name=self.primary.name,
            intended_model=self.primary.display_model, model=answering.display_model,
            state=state, skipped=skipped,
        )


def _substitute_from(agent: Agent, target: Any, *, llm: Optional[LLMConfig]) -> Optional[Agent]:
    """`target`의 연결 및 용량 설정으로 교체한 사본을 반환합니다. 연결 정보가 없거나 이미 동일한 연결이면 None을 반환합니다.

    `llm`은 전역 연결로 폴백할 때만 전달합니다. 이 경우 결정 LLM과 Provider 고유 사고 옵션도 전역 설정을 따릅니다.
    """
    if not target.is_live:
        return None
    if connection_signature(target) == connection_signature(agent):
        return None
    update: Dict[str, Any] = {
        name: getattr(target, name) for name in (*CONNECTION_FIELDS, *_CAPACITY_FIELDS)
    }
    if llm is not None and (agent.decision_llm.enabled != llm.decision_llm.enabled or llm.decision_llm.enabled):
        update["decision_llm"] = llm.decision_llm.model_copy(deep=True)
    if agent.sequential_thinking.enabled and agent.sequential_thinking.mode == "native":
        # Provider 고유의 사고 옵션은 해당 모델에 종속됩니다. 폴백한 모델에는 대상 연결의 설정을 적용합니다.
        source = llm.sequential_thinking if llm is not None else target.sequential_thinking
        update["sequential_thinking"] = source.model_copy(deep=True)
    return agent.model_copy(update=update)


def substitute_for(agent: Agent, llm: LLMConfig) -> Optional[Agent]:
    """전역 연결로 교체한 에이전트 사본을 반환합니다. 이미 전역 연결을 쓰거나 전역 연결 정보가 없으면 None을 반환합니다."""
    return _substitute_from(agent, global_entry(llm, _CAPACITY_FIELDS), llm=llm)


_warned_unknown: set = set()


def _warn_unknown(name: str) -> None:
    """폴백 목록에 존재하지 않는 이름이 있으면 한 번만 경고 로그를 남깁니다. 설정 로드 시 차단하지는 않습니다 —
    화면에서 에이전트를 삭제했다는 이유로 앱이 시작되지 않아서는 안 되기 때문입니다. 해당 이름은 건너뜁니다."""
    if name not in _warned_unknown:
        _warned_unknown.add(name)
        logger.warning(
            f"llm.fallback.order names '{name}', which is neither '{GLOBAL_CONNECTION}' nor an agent key; it is skipped"
        )


def substitutes_for(agent: Agent, config: RootConfig) -> List[FallbackCandidate]:
    """`llm.fallback.order`에 지정된 순서대로 폴백 후보 목록을 구성합니다. 순서를 비워 두면 전역 연결 하나만 사용합니다.

    자기 자신의 연결, 이미 앞에 포함된 연결, 연결 정보가 없는 항목은 제외합니다. 비활성화된 에이전트와 결정 LLM을 사용하는 전문가도
    제외합니다 — 전자는 사용하지 않도록 설정된 연결이고, 후자는 생성형 발언을 대신 처리할 수 없기 때문입니다.
    """
    seen = {connection_signature(agent)}
    candidates: List[FallbackCandidate] = []
    for name in config.llm.fallback.order or [GLOBAL_CONNECTION]:
        if name == GLOBAL_CONNECTION:
            substitute = substitute_for(agent, config.llm)
        else:
            entry = config.agents.get(name)
            if entry is None:
                _warn_unknown(name)
                continue
            if not entry.enabled:
                continue
            if entry.decision_llm.enabled and name != ORCHESTRATOR_KEY:
                continue
            substitute = _substitute_from(agent, entry, llm=None)
        if substitute is None:
            continue
        signature = connection_signature(substitute)
        if signature in seen:
            continue
        seen.add(signature)
        candidates.append(FallbackCandidate(name=name, agent=substitute))
    return candidates


def route_for(agent: Agent, config: Optional[RootConfig] = None) -> Optional[FallbackRoute]:
    """이 에이전트가 사용할 수 있는 폴백 경로를 반환합니다. 기능이 꺼져 있거나 폴백 대상이 없으면 None을 반환합니다."""
    config = config or get_config()
    settings = config.llm.fallback
    if not settings.enabled or not agent.allow_fallback:
        return None
    candidates = substitutes_for(agent, config)
    if not candidates:
        return None
    return FallbackRoute(
        primary=agent, connection=connection_id(agent), retry_after=settings.retry_after,
        breaker=get_fallback_breaker(), candidates=candidates,
    )


def skip_remaining(connection: str, config: Optional[RootConfig] = None) -> Optional[float]:
    """해당 연결을 현재 건너뛰는 중이라면(= 대체 모델이 대신 응답 중이라면) 재시도까지 남은 시간(초)을 반환합니다."""
    config = config or get_config()
    settings = config.llm.fallback
    if not settings.enabled:
        return None
    return get_fallback_breaker().remaining(connection, settings.retry_after)
