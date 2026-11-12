"""서버 전체의 LLM 동시 요청 상한.

`parallel_limit`은 **단일** 대화 안에서 동시에 발언하는 수를 제한합니다. 여러 대화가 동시에
진행되면 요청 수는 대화 수의 배수로 늘어납니다. 단일 GPU 런타임은 큐에 요청이 쌓이다 메모리가 고갈되고,
공용 체험 서버는 사용자 수만큼 요청이 겹칩니다. 그로 인한 실패는 사용자에게
"에이전트가 응답하지 못했다"로만 표시됩니다.

이 모듈에서는 LLM 단일 호출(`LLMCaller._complete_once`)을 하나의 슬롯으로 관리합니다. 여유 슬롯이 없으면
요청을 버리지 않고 **도착 순서(FIFO)**대로 대기합니다. 대기 중인 주체는 대화 ID로 추적하므로, 화면에서
"앞에 몇 건이 대기 중인지"를 보여 줄 수 있습니다 — 멈춘 상태와 대기 상태는 사용자 경험상 전혀 다르기 때문입니다.

대화 ID는 인자로 전달하지 않고 `LLM_HOLDER` 컨텍스트 변수로 전달합니다. 엔진이 턴을 시작할 때
한 번 설정하면, 병렬 발언 태스크는 생성 시점에 컨텍스트를 그대로 상속합니다. 따라서 호출 경로마다 인자를
추가하지 않아도 됩니다.

상한은 `conf.json`의 `app.llm_concurrency`입니다. 0이면 제한하지 않고 활성 수만 집계합니다.

## 연결별 대기열 (ADR-056)

서버 전체 대기열 하나만으로는 모델별·서버별 배치 크기 차이를 맞출 수 없습니다. 단일 GPU에 띄운 모델은 한 번에
요청 1건만 받아야 하는데 전체 상한을 1로 두면 옆의 대형 서버까지 함께 직렬화됩니다. `ConnectionGates`는
**연결마다**(모델·주소·키가 동일한 연결, `connection_id`) 별도의 대기열을 둡니다. 상한은 연결 객체에 별도로 두지 않고
호출하는 에이전트의 `max_concurrency`를 사용합니다 — 요청마다 "현재 이 연결에서 실행 중인 요청이 내 상한보다 적은가"를
확인하고, 초과하면 도착 순서대로 대기합니다. 동일한 연결을 쓰는 에이전트마다 값이 달라도 요청마다 상한이 뒤섞이지 않습니다.

단일 호출은 먼저 연결별 대기열에 진입해 슬롯을 확보한 뒤 서버 전체 대기열에 진입합니다. 반대로 하면 연결 슬롯을
기다리는 동안 서버 전체 슬롯 하나를 점유한 채 유휴 대기하게 됩니다.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 현재 LLM을 호출하는 대화 ID입니다. 비어 있으면 대화 외부에서의 호출입니다.
LLM_HOLDER: contextvars.ContextVar[str] = contextvars.ContextVar("mado_llm_holder", default="")


@dataclass
class _Waiter:
    holder: str
    future: "asyncio.Future[None]"
    # 이 요청이 직접 지정한 상한입니다. None이면 게이트의 기본 상한을 사용합니다.
    limit: Optional[int] = None


class LLMGate:
    """선입선출(FIFO) 순서로 실행 슬롯을 배분하는 세마포어입니다. 현재 점유자와 대기자 목록을 조회할 수 있습니다."""

    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit or 0))
        self._active: List[str] = []
        self._waiters: Deque[_Waiter] = deque()

    # ------------------------------------------------------------ 설정

    def configure(self, limit: int) -> None:
        """상한을 변경합니다. 상한이 늘어나면 대기 중이던 요청을 그만큼 즉시 진입시킵니다."""
        limit = max(0, int(limit or 0))
        if limit != self.limit:
            self.limit = limit
            self._wake()

    # ------------------------------------------------------------ 슬롯

    def _has_room(self, limit: Optional[int] = None) -> bool:
        cap = self.limit if limit is None else max(0, int(limit or 0))
        return cap <= 0 or len(self._active) < cap

    def _wake(self) -> None:
        while self._waiters:
            waiter = self._waiters[0]
            if waiter.future.done():
                # 대기 중 취소된 요청이므로 슬롯을 부여하지 않고 건너뜁니다.
                self._waiters.popleft()
                continue
            # 선두 요청이 진입할 수 없으면 후속 요청도 대기합니다(상한이 더 넉넉해도 추월하지 않고 FIFO 순서를 유지합니다).
            if not self._has_room(waiter.limit):
                break
            self._waiters.popleft()
            self._active.append(waiter.holder)
            waiter.future.set_result(None)

    async def acquire(self, holder: str = "", limit: Optional[int] = None) -> None:
        """슬롯을 획득합니다. `limit`을 지정하면 게이트의 기본 상한 대신 해당 값으로 가용 여부를 판단합니다(연결별 대기열)."""
        # 앞에 대기 중인 요청이 있으면 여유가 있어도 대기열 끝에 합류합니다(순서 추월 방지).
        if not self._waiters and self._has_room(limit):
            self._active.append(holder)
            return
        waiter = _Waiter(holder, asyncio.get_running_loop().create_future(), limit)
        self._waiters.append(waiter)
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # 슬롯을 부여받은 직후 취소되었으므로 획득한 슬롯을 반환합니다.
                self.release(holder)
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            raise

    def release(self, holder: str = "") -> None:
        try:
            self._active.remove(holder)
        except ValueError:
            logger.warning("Released an LLM slot that was not held (holder=%r)", holder)
        self._wake()

    @asynccontextmanager
    async def slot(self, holder: Optional[str] = None, limit: Optional[int] = None) -> AsyncIterator[None]:
        """LLM 단일 호출 동안 슬롯을 점유합니다."""
        who = LLM_HOLDER.get() if holder is None else holder
        await self.acquire(who, limit)
        try:
            yield
        finally:
            self.release(who)

    # ------------------------------------------------------------ 조회

    def _live_waiters(self) -> List[_Waiter]:
        return [w for w in self._waiters if not w.future.done()]

    def position(self, holder: str) -> Optional[int]:
        """이 대화의 요청이 대기 중이면 앞서 기다리는 요청 수를 반환합니다. 대기 중이 아니면 None을 반환합니다."""
        ahead = 0
        for waiter in self._live_waiters():
            if waiter.holder == holder:
                return ahead
            ahead += 1
        return None

    def snapshot(self) -> Dict[str, Any]:
        waiting = self._live_waiters()
        return {
            "limit": self.limit,
            "active": len(self._active),
            "waiting": len(waiting),
            "active_holders": list(self._active),
            "waiting_holders": [w.holder for w in waiting],
        }


_gate: Optional[LLMGate] = None


def _configured_limit() -> int:
    try:
        from app.config import get_config

        return int(get_config().app.llm_concurrency or 0)
    except Exception:  # noqa: BLE001 - 설정을 읽지 못하더라도 LLM 호출을 차단하지 않습니다
        return 0


def get_llm_gate() -> LLMGate:
    """프로세스 전역 LLM 게이트를 반환합니다. 호출할 때마다 설정의 상한을 반영합니다(화면에서 conf.json을 수정한 경우 대비)."""
    global _gate
    if _gate is None:
        _gate = LLMGate(_configured_limit())
    else:
        _gate.configure(_configured_limit())
    return _gate


class ConnectionGates:
    """연결마다 독립적인 대기열을 관리합니다. 상한은 호출 요청이 함께 전달합니다(해당 에이전트의 `max_concurrency`)."""

    def __init__(self) -> None:
        self._gates: Dict[str, LLMGate] = {}

    def gate(self, connection: str) -> LLMGate:
        gate = self._gates.get(connection)
        if gate is None:
            gate = self._gates[connection] = LLMGate(0)
        return gate

    @asynccontextmanager
    async def slot(self, connection: str, limit: int, holder: Optional[str] = None) -> AsyncIterator[None]:
        """해당 연결의 슬롯을 단일 호출 동안 점유합니다. `limit`이 0이면 대기하지 않고 활성 수만 집계합니다."""
        async with self.gate(connection).slot(holder, limit=max(0, int(limit or 0))):
            yield

    def position(self, holder: str) -> Optional[int]:
        """이 대화의 요청이 연결별 대기열 중 하나에서 대기 중이면 앞서 기다리는 요청 수를 반환합니다. 대기 중이 아니면 None을 반환합니다."""
        found = [p for p in (gate.position(holder) for gate in self._gates.values()) if p is not None]
        return min(found) if found else None

    def snapshot(self) -> Dict[str, Tuple[int, int]]:
        """연결별 `(활성 요청 수, 대기 요청 수)` 매핑을 반환합니다. 유휴 연결은 제외합니다."""
        busy: Dict[str, Tuple[int, int]] = {}
        for connection, gate in self._gates.items():
            snap = gate.snapshot()
            if snap["active"] or snap["waiting"]:
                busy[connection] = (snap["active"], snap["waiting"])
        return busy


_connection_gates: Optional[ConnectionGates] = None


def get_connection_gates() -> ConnectionGates:
    """프로세스 전역 연결별 대기열 관리자를 반환합니다."""
    global _connection_gates
    if _connection_gates is None:
        _connection_gates = ConnectionGates()
    return _connection_gates
