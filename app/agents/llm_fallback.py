"""개별 모델의 연결이 실패하면 전역 기본 연결로 물러섭니다 (`llm.fallback`, ADR-030).

에이전트마다 다른 모델을 붙일 수 있습니다. 그 가운데 하나의 키가 만료되거나 서버가 내려가면, 지금까지는
그 에이전트의 발언 자리에 "연결 끊김" 만 남았습니다. 켜 두면 그 자리를 전역 기본 모델(`llm`)이 같은
페르소나로 대신 채웁니다.

## 지어내지 않되, 숨기지도 않습니다

`LLMUnavailableError` 가 대체 응답을 만들지 않는 이유는 그대로입니다 — 조용히 바뀐 답은 다음 에이전트의
입력과 최종 합성까지 오염시킵니다. 폴백은 진짜 모델이 답하는 것이지만, **누가 답했는지가 달라졌다는
사실**은 같은 무게로 남아야 합니다. 그래서:

- 기본은 꺼져 있고(`llm.fallback.enabled`), 에이전트마다 막을 수 있습니다(`allow_fallback: false`).
- 전환할 때마다 `FallbackNotice` 를 올립니다. 엔진은 그것을 발언 기록(`llm_meta`)과 화면에 남깁니다.

알림은 `call_agent` 의 인자가 아니라 컨텍스트 변수(`listening`)로 받습니다. 발언을 여는 쪽이 그 발언
동안만 걸어 두면 됩니다 — `LLM_HOLDER` 가 대화 id 를 넘기는 것과 같은 방식이고, `call_agent` 와 같은
모양으로 만든 호출기들이 인자 하나 때문에 깨지지 않습니다. 걸어 둔 곳이 없으면 로그에만 남습니다.

## 언제 물러서는가

1. **연결의 문제일 때만.** 인증 실패, 모델 이름, 연결 실패, 응답 없음, 서버 오류. 요청 거절(400)과
   컨텍스트 초과는 요청의 문제라 기본 모델도 같은 이유로 실패하고, 한도 초과(429)는 잠깐 뒤에 풀립니다.
2. **모델이 아무것도 내놓기 전에만.** 글이 한 조각이라도 화면에 흘렀거나 도구를 불렀으면 물러서지
   않습니다. 같은 답이 두 번 붙고, 한 모델이 만든 도구 호출 기록을 다른 모델에게 넘기면 Provider 에 따라
   거절하기 때문입니다. 끊긴 발언을 이어 가는 경우도 같은 이유로 물러서지 않습니다. 이 판단은
   `LLMCaller.call_agent` 가 합니다.
3. **물러설 곳이 있을 때만.** 에이전트가 이미 전역 연결을 쓰고 있거나 전역에 연결 정보가 없으면 지금처럼
   실패합니다.

## 무엇을 바꾸는가

연결(모델·주소·키·버전·Provider·추가 머리말과 본문)과 그 모델의 용량·전송 설정(`max_tokens`,
`max_context_window`, `timeout`, `num_retries`, `drop_params`)은 전역 것을 씁니다. 페르소나·도구·스킬·
온도·도구 루프 상한은 에이전트 것 그대로입니다. Provider 고유의 사고 옵션(`native`)을 쓰던 에이전트는
전역의 단계적 사고 설정을 따릅니다.

전역은 **지금의** 설정(`get_config().llm`)입니다. 잠긴 대화의 스냅샷에는 전역 값이 따로 없고, 키를 바꾼
뒤에도 동작해야 하기 때문입니다. 그래서 어떤 모델이 실제로 답했는지는 스냅샷이 아니라 발언마다 적습니다.

## 차단기

죽은 연결을 발언마다 다시 기다리면 그때마다 타임아웃과 재시도를 통째로 치릅니다. 한 번 실패한 연결은
`retry_after` 초 동안 곧바로 기본 모델로 보내고, 지나면 한 번 다시 불러 봅니다. `모델 연결 상태` 창에서
그 연결이 응답하는 것을 확인하면 곧바로 닫습니다.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

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
from app.config import LLMConfig, RootConfig, get_config

logger = logging.getLogger(__name__)

__all__ = [
    "FALLBACK_STATES", "FallbackBreaker", "FallbackNotice", "FallbackRoute", "classify",
    "current_listener", "get_fallback_breaker", "listening", "route_for", "skip_remaining", "substitute_for",
]

# 물러서는 실패. 연결 자체가 안 되는 것만 고릅니다.
FALLBACK_STATES = frozenset({AUTH, MODEL, UNREACHABLE, TIMEOUT, SERVER_ERROR})

# 전역에서 함께 가져오는 것: 그 모델의 용량과 전송 설정.
_CAPACITY_FIELDS = ("max_tokens", "max_context_window", "timeout", "num_retries", "drop_params")


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
    """실패한 연결을 잠깐 건너뛰게 합니다. 연결의 이름표(`connection_id`)로 가릅니다."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # 연결 -> (실패한 시각, 실패 갈래)
        self._tripped: Dict[str, Tuple[float, str]] = {}

    def trip(self, connection: str, state: str) -> None:
        self._tripped[connection] = (self._clock(), state)

    def close(self, connection: str) -> bool:
        """그 연결이 다시 된다는 것을 알았을 때. 건너뛰고 있었으면 True."""
        return self._tripped.pop(connection, None) is not None

    def reset(self) -> None:
        self._tripped.clear()

    def remaining(self, connection: str, retry_after: float) -> Optional[float]:
        """이 연결을 건너뛰는 중이면 남은 시간(초). 시간이 다 됐으면 잊고 None — 다음 호출이 다시 불러 봅니다."""
        entry = self._tripped.get(connection)
        if entry is None:
            return None
        left = retry_after - (self._clock() - entry[0])
        if left <= 0:
            del self._tripped[connection]
            return None
        return left

    def state(self, connection: str, retry_after: float) -> Optional[str]:
        """이 연결을 건너뛰는 중이면 그 실패 갈래."""
        if self.remaining(connection, retry_after) is None:
            return None
        return self._tripped[connection][1]


_breaker: Optional[FallbackBreaker] = None


def get_fallback_breaker() -> FallbackBreaker:
    global _breaker
    if _breaker is None:
        _breaker = FallbackBreaker()
    return _breaker


@dataclass
class FallbackRoute:
    """이 에이전트가 물러설 길. `substitute` 는 전역 연결을 쓰는 같은 에이전트입니다."""

    primary: Agent
    substitute: Agent
    connection: str
    retry_after: float
    breaker: FallbackBreaker

    def known_failure(self) -> Optional[str]:
        """조금 전에 실패해 건너뛰는 중이면 그 실패 갈래."""
        return self.breaker.state(self.connection, self.retry_after)

    def trip(self, state: str) -> None:
        self.breaker.trip(self.connection, state)

    def notice(self, state: str, *, skipped: bool) -> FallbackNotice:
        return FallbackNotice(
            agent_key=self.primary.key, agent_name=self.primary.name,
            intended_model=self.primary.model, model=self.substitute.model,
            state=state, skipped=skipped,
        )


def substitute_for(agent: Agent, llm: LLMConfig) -> Optional[Agent]:
    """전역 연결로 바꾼 사본. 이미 전역 연결을 쓰거나 전역에 연결 정보가 없으면 None."""
    target = global_entry(llm, _CAPACITY_FIELDS)
    if not target.is_live:
        return None
    if connection_signature(target) == connection_signature(agent):
        return None
    update: Dict[str, Any] = {
        name: getattr(target, name) for name in (*CONNECTION_FIELDS, *_CAPACITY_FIELDS)
    }
    if agent.sequential_thinking.enabled and agent.sequential_thinking.mode == "native":
        # Provider 고유의 사고 옵션은 그 모델에 묶인 값입니다. 전역 모델에는 전역의 설정을 씁니다.
        update["sequential_thinking"] = llm.sequential_thinking.model_copy(deep=True)
    return agent.model_copy(update=update)


def route_for(agent: Agent, config: Optional[RootConfig] = None) -> Optional[FallbackRoute]:
    """이 에이전트가 물러설 수 있으면 그 길. 꺼져 있거나 물러설 곳이 없으면 None."""
    config = config or get_config()
    settings = config.llm.fallback
    if not settings.enabled or not agent.allow_fallback:
        return None
    substitute = substitute_for(agent, config.llm)
    if substitute is None:
        return None
    return FallbackRoute(
        primary=agent, substitute=substitute, connection=connection_id(agent),
        retry_after=settings.retry_after, breaker=get_fallback_breaker(),
    )


def skip_remaining(connection: str, config: Optional[RootConfig] = None) -> Optional[float]:
    """이 연결을 지금 건너뛰고 있으면(= 기본 모델이 대신 답하는 중이면) 다시 불러 볼 때까지 남은 시간(초)."""
    config = config or get_config()
    settings = config.llm.fallback
    if not settings.enabled:
        return None
    return get_fallback_breaker().remaining(connection, settings.retry_after)
