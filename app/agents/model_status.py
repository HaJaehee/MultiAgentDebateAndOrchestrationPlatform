"""모델 연결 상태 — 설정에 있는 모델마다 아주 짧은 요청을 보내 지금 응답하는지 봅니다.

`is_live` 는 "연결 정보가 적혀 있다" 까지만 말해 줍니다. 키가 틀렸는지, 모델 이름이 맞는지,
주소에 닿는지는 불러 봐야 압니다. 그 실패는 토론 한가운데에서 "에이전트가 응답하지 못했다" 로
처음 드러나는데, 그때는 어느 모델이 왜 안 되는지 사람이 알기 어렵습니다.

## 무엇을 한 줄로 보는가

전역 기본값(`llm`)과 켜져 있는 에이전트를 훑어, 모델·주소·키·버전·Provider·추가 머리말과 본문이
모두 같은 것을 **연결 하나**로 묶습니다. 에이전트 다섯이 같은 모델을 쓰면 요청은 한 번이고, 한
에이전트만 모델이 다르면 그 에이전트가 따로 한 줄이 됩니다. 값은 환경변수가 풀린 뒤의 것이라,
`.env` 로 준 것과 `conf.json` 에 적은 것을 가리지 않습니다.

## 부하와 비용

- 요청은 `ping` 한 마디에 출력 `PROBE_MAX_TOKENS` 토큰입니다. 도구·스트리밍·재시도는 없습니다.
- 서버 전체의 동시 요청 상한(`app.llm_concurrency`)을 그대로 지킵니다. 자리가 곧 나지 않으면
  끼어들지 않고 **확인 보류**로 둡니다 — 토론이 도는 중이면 그 모델은 일하고 있는 것입니다.
  상한이 있으면 한 번에 하나씩만 확인합니다 (우리끼리 줄을 서서 보류로 보이지 않게).
- 결과는 `CACHE_SECONDS` 동안 다시 씁니다. 여러 화면이 동시에 열어도 확인은 한 판만 돕니다.
  다시 확인을 눌러도 `MIN_INTERVAL_SECONDS` 안에는 새로 보내지 않습니다 (체험 방문자도 누를 수 있습니다).

## 폴백과 닿는 곳

`llm.fallback` 이 켜져 있으면 실제 호출에서 실패한 개별 연결은 잠깐 전역 기본 모델이 대신 답합니다
(`app/agents/llm_fallback.py`). 그런 줄에는 그 사실과 다시 불러 볼 때까지 남은 시간을 함께 보여 주고,
여기서 그 연결이 응답하는 것을 확인하면 곧바로 원래 모델로 돌아가게 합니다. 연결의 이름표
(`connection_id`)와 실패 갈래(`classify`)는 두 모듈이 함께 씁니다.

## 누가 무엇을 보는가

상태와 모델 이름, 쓰는 에이전트는 누구에게나 같습니다. 주소와 오류 원문은 주인에게만 보여 줍니다
(`describe_row(detail=...)`). 사내 게이트웨이 주소와 Provider 가 돌려준 문장은 방문자에게 줄 것이
아닙니다. 키는 누구에게도 내보내지 않고, 오류 원문에 섞여 온 것도 가립니다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.agents import llm as llm_module
from app.agents import llm_fallback
from app.agents.connection import (
    BUSY,
    CHECKING,
    ERROR,
    GLOBAL_USER,
    NOT_CONFIGURED,
    OK,
    STATE_INFO,
    UNKNOWN,
    classify,
    connection_id,
    connection_signature,
    global_entry,
)
from app.agents.llm_gate import get_llm_gate
from app.config import AgentConfig, RootConfig, get_config

logger = logging.getLogger(__name__)

PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 30.0
# 동시 요청 상한에 걸려 있을 때 자리를 기다리는 시간. 넘으면 확인 보류입니다.
GATE_WAIT_SECONDS = 3.0
CACHE_SECONDS = 180.0
MIN_INTERVAL_SECONDS = 10.0

GATE_HOLDER = "model-status"
DEFAULT_ENDPOINT = "Provider 기본 주소"



# 오류 원문에 섞여 오는 키 모양. Provider 는 틀린 키의 앞뒤를 되돌려 보여 주곤 합니다.
_KEY_LIKE = re.compile(r"\b(?:sk|pk|rk|key|AIza|gsk|xai|hf)[-_][A-Za-z0-9_\-*.]{6,}|\bAIza[A-Za-z0-9_\-*]{6,}")
_DETAIL_LIMIT = 300


@dataclass(frozen=True)
class ModelTarget:
    """연결 하나. `id` 는 화면이 줄을 가리키는 이름이고, 키 값을 되짚을 수 없는 해시입니다."""

    id: str
    model: str
    api_base: Optional[str]
    api_key: Optional[str] = field(repr=False)
    api_version: Optional[str]
    provider: Optional[str]
    extra_headers: Tuple[Tuple[str, str], ...] = field(repr=False)
    extra_body: str = field(repr=False)
    users: Tuple[str, ...]
    is_live: bool

    @property
    def endpoint(self) -> str:
        return self.api_base or DEFAULT_ENDPOINT


@dataclass(frozen=True)
class ModelStatus:
    target_id: str
    state: str
    latency_ms: Optional[int] = None
    # 주인에게만 보여 주는 오류 원문 요약 (키는 가린 뒤).
    detail: str = ""
    checked_at: float = 0.0


@dataclass(frozen=True)
class ModelRow:
    """한 줄: 연결과 그 연결의 마지막 결과."""

    target: ModelTarget
    state: str
    latency_ms: Optional[int] = None
    detail: str = ""
    age_seconds: Optional[float] = None
    # 이 연결이 조금 전 실제 호출에서 실패해, 전역 기본 모델이 대신 답하고 있으면 다시 불러 볼 때까지
    # 남은 시간(초) (`llm.fallback`, `app/agents/llm_fallback.py`). 아니면 None.
    skip_seconds: Optional[float] = None


# ---------------------------------------------------------------- 설정에서 연결 뽑기


def collect_targets(config: Optional[RootConfig] = None) -> List[ModelTarget]:
    """확인할 연결 목록. 전역 기본값이 맨 앞이고, 그 뒤는 설정에 적힌 에이전트 순서입니다."""
    config = config or get_config()
    entries: List[Tuple[str, AgentConfig]] = [(GLOBAL_USER, global_entry(config.llm))]
    entries += [(cfg.name or key, cfg) for key, cfg in config.agents.items() if cfg.enabled]

    grouped: Dict[Tuple[Any, ...], Tuple[AgentConfig, List[str]]] = {}
    for user, cfg in entries:
        grouped.setdefault(connection_signature(cfg), (cfg, []))[1].append(user)

    targets: List[ModelTarget] = []
    for signature, (cfg, users) in grouped.items():
        targets.append(ModelTarget(
            id=connection_id(cfg),
            model=cfg.model, api_base=cfg.api_base, api_key=cfg.api_key, api_version=cfg.api_version,
            provider=cfg.provider, extra_headers=signature[5], extra_body=signature[6],
            users=tuple(users), is_live=cfg.is_live,
        ))
    return targets


# ---------------------------------------------------------------- 한 번 불러 보기


def probe_kwargs(target: ModelTarget, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """확인 요청. 실제 호출(`LLMCaller.build_completion_kwargs`)과 같은 연결 정보를 쓰되 가장 가볍게 보냅니다.

    온도와 사고 옵션은 넣지 않습니다. 모델마다 받는 값이 달라, 연결과 무관한 이유로 거절될 수 있습니다.
    """
    kwargs: Dict[str, Any] = {
        "model": target.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "timeout": timeout_seconds,
        "num_retries": 0,
        "drop_params": True,
    }
    api_key = (target.api_key or "").strip()
    if not api_key and target.api_base:
        api_key = llm_module.LOCAL_API_KEY_PLACEHOLDER
    if api_key:
        kwargs["api_key"] = api_key
    if target.api_base:
        kwargs["api_base"] = target.api_base
    if target.api_version:
        kwargs["api_version"] = target.api_version
    if target.provider:
        kwargs["custom_llm_provider"] = target.provider
    if target.extra_headers:
        kwargs["extra_headers"] = dict(target.extra_headers)
    extra_body = json.loads(target.extra_body)
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def safe_detail(exc: BaseException, target: ModelTarget) -> str:
    """주인에게 보여 줄 오류 원문. 키는 가리고 한두 줄로 줄입니다."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    key = (target.api_key or "").strip()
    if key:
        text = text.replace(key, "[키 가림]")
    for _name, value in target.extra_headers:
        if len(value) >= 8:
            text = text.replace(value, "[머리말 가림]")
    text = _KEY_LIKE.sub("[키 가림]", text)
    return text if len(text) <= _DETAIL_LIMIT else text[:_DETAIL_LIMIT].rstrip() + " …"


async def probe(
    target: ModelTarget, *,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    gate_wait_seconds: float = GATE_WAIT_SECONDS,
    wall: Callable[[], float] = time.time,
) -> ModelStatus:
    """연결 하나를 확인합니다. 예외를 올리지 않고 결과로 돌려줍니다."""
    if not target.is_live:
        return ModelStatus(target.id, NOT_CONFIGURED, checked_at=wall())

    gate = get_llm_gate()
    try:
        await asyncio.wait_for(gate.acquire(GATE_HOLDER), gate_wait_seconds)
    except asyncio.TimeoutError:
        return ModelStatus(target.id, BUSY, checked_at=wall())

    started = time.monotonic()
    try:
        await asyncio.wait_for(
            llm_module.litellm.acompletion(**probe_kwargs(target, timeout_seconds)),
            timeout_seconds + 2.0,
        )
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 한 줄의 상태가 됩니다
        state = classify(exc)
        logger.info("Model status probe failed for %s (%s): %s", target.model, state, type(exc).__name__)
        return ModelStatus(target.id, state, detail=safe_detail(exc, target), checked_at=wall())
    finally:
        gate.release(GATE_HOLDER)
    return ModelStatus(
        target.id, OK, latency_ms=int(round((time.monotonic() - started) * 1000)), checked_at=wall(),
    )


# ---------------------------------------------------------------- 결과판


class ModelStatusBoard:
    """프로세스 전체가 함께 보는 결과판. 확인은 한 번에 한 판만 돕니다."""

    def __init__(
        self, *,
        prober: Callable[[ModelTarget], Any] = probe,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._prober = prober
        self._clock = clock
        self._results: Dict[str, ModelStatus] = {}
        self._result_times: Dict[str, float] = {}
        self._checking: Set[str] = set()
        self._task: Optional["asyncio.Task[None]"] = None
        self._finished_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, config: Optional[RootConfig] = None, *, force: bool = False) -> bool:
        """확인을 시작합니다. 새로 시작했으면 True.

        이미 돌고 있으면 그 판에 올라타고, 방금 확인한 결과가 있으면 그것을 씁니다. `force` 는 "다시 확인"
        이지만 `MIN_INTERVAL_SECONDS` 안에는 새로 보내지 않습니다. 설정이 바뀌어 처음 보는 연결이 생기면
        시간과 상관없이 확인합니다.
        """
        if self.running:
            return False
        targets = collect_targets(config)
        if self._finished_at is not None and all(t.id in self._results for t in targets):
            age = self._clock() - self._finished_at
            if age < (MIN_INTERVAL_SECONDS if force else CACHE_SECONDS):
                return False
        self._checking = {t.id for t in targets if t.is_live}
        self._task = asyncio.create_task(self._run(targets))
        return True

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self, targets: Sequence[ModelTarget]) -> None:
        async def one(target: ModelTarget) -> None:
            try:
                result = await self._prober(target)
            except Exception as exc:  # noqa: BLE001 - 한 줄의 실패가 나머지 줄의 확인을 막지 않게
                logger.warning("Model status probe crashed for %s: %r", target.model, exc)
                result = ModelStatus(target.id, ERROR, detail=safe_detail(exc, target))
            finally:
                self._checking.discard(target.id)
            self._results[target.id] = result
            self._result_times[target.id] = self._clock()
            if result.state == OK:
                # 다시 된다는 것을 방금 확인했습니다. 기본 모델로 돌려 보내던 것을 그만둡니다.

                if llm_fallback.get_fallback_breaker().close(target.id):
                    logger.info("Model %s answers again; fallback for it is over", target.model)

        try:
            if get_llm_gate().limit > 0:
                # 상한이 있으면 우리끼리 자리를 다투지 않게 하나씩 확인합니다.
                for target in targets:
                    await one(target)
            else:
                await asyncio.gather(*(one(target) for target in targets))
        finally:
            # 설정에서 사라진 연결의 결과는 버립니다.
            keep = {t.id for t in targets}
            self._results = {k: v for k, v in self._results.items() if k in keep}
            self._result_times = {k: v for k, v in self._result_times.items() if k in keep}
            self._checking.clear()
            self._finished_at = self._clock()

    def rows(self, config: Optional[RootConfig] = None) -> List[ModelRow]:
        """지금 설정의 연결마다 한 줄. 확인 중인 줄은 앞선 결과가 있어도 '확인 중' 입니다."""

        now = self._clock()
        rows: List[ModelRow] = []
        for target in collect_targets(config):
            skip = llm_fallback.skip_remaining(target.id, config)
            if target.id in self._checking:
                rows.append(ModelRow(target, CHECKING, skip_seconds=skip))
                continue
            result = self._results.get(target.id)
            if result is None:
                rows.append(ModelRow(target, UNKNOWN if target.is_live else NOT_CONFIGURED, skip_seconds=skip))
                continue
            rows.append(ModelRow(
                target, result.state, result.latency_ms, result.detail,
                max(0.0, now - self._result_times.get(target.id, now)), skip,
            ))
        return rows


_board: Optional[ModelStatusBoard] = None


def get_model_status_board() -> ModelStatusBoard:
    global _board
    if _board is None:
        _board = ModelStatusBoard()
    return _board


# ---------------------------------------------------------------- 화면에 내보낼 글


def _latency_text(latency_ms: Optional[int]) -> str:
    if latency_ms is None:
        return ""
    return f"{latency_ms}ms" if latency_ms < 1000 else f"{latency_ms / 1000:.1f}초"


def _age_text(age_seconds: Optional[float]) -> str:
    if age_seconds is None:
        return ""
    if age_seconds < 5:
        return "방금 확인"
    if age_seconds < 60:
        return f"{int(age_seconds)}초 전 확인"
    return f"{int(age_seconds // 60)}분 전 확인"


def describe_row(row: ModelRow, *, detail: bool) -> Dict[str, Any]:
    """한 줄을 화면이 그대로 쓸 글로 바꿉니다. `detail` 이 꺼져 있으면 주소와 오류 원문을 아예 담지 않습니다."""
    label, tone, hint = STATE_INFO.get(row.state, STATE_INFO[ERROR])
    view: Dict[str, Any] = {
        "id": row.target.id,
        "state": row.state,
        "label": label,
        "tone": tone,
        "hint": hint,
        "model": row.target.model,
        "users": list(row.target.users),
        "latency": _latency_text(row.latency_ms),
        "age": _age_text(row.age_seconds),
    }
    if row.skip_seconds is not None:
        view["fallback"] = (
            f"실제 호출에서 연결에 실패해 기본 모델이 대신 답하고 있습니다. "
            f"{max(1, int(round(row.skip_seconds)))}초 뒤 이 모델을 다시 불러 봅니다."
        )
    if detail:
        view["endpoint"] = row.target.endpoint
        view["has_key"] = bool((row.target.api_key or "").strip())
        view["error"] = row.detail
    return view


def summarize(rows: Sequence[ModelRow]) -> str:
    total = len(rows)
    if any(row.state == CHECKING for row in rows):
        done = sum(1 for row in rows if row.state not in (CHECKING, UNKNOWN))
        return f"확인 중 — {total}개 중 {done}개 끝남"
    good = sum(1 for row in rows if row.state == OK)
    return f"연결 {total}개 중 {good}개 응답"
