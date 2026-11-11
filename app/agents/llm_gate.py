"""서버 전체의 LLM 동시 요청 상한.

`parallel_limit` 은 대화 **하나** 안에서 동시에 발언하는 수를 묶습니다. 대화가 여럿
돌면 요청은 그 곱으로 늘어납니다. 단일 GPU 런타임은 큐에 쌓다가 메모리가 터지고,
여러 사람이 함께 쓰는 체험 서버는 사람 수만큼 요청이 겹칩니다. 그 실패는 사람에게
"에이전트가 응답하지 못했다" 로만 보입니다.

여기서는 LLM 호출 한 판(`LLMCaller._complete_once`)을 자리 하나로 칩니다. 자리가 없으면
버리지 않고 **온 순서대로** 기다립니다. 누가 기다리는지는 대화 id 로 남겨 두어, 화면이
"앞에 몇 건이 기다리는지" 를 보여 줄 수 있게 합니다 — 멈춘 것과 기다리는 것은 사람에게
전혀 다른 일입니다.

대화 id 는 인자로 받지 않고 `LLM_HOLDER` 컨텍스트 변수로 받습니다. 엔진이 턴을 시작할 때
한 번 걸면, 병렬 발언 태스크는 만들어질 때 그대로 물려받습니다. 호출 경로마다 인자를
늘리지 않아도 됩니다.

상한은 `conf.json` 의 `app.llm_concurrency` 입니다. 0 이면 제한하지 않고 세기만 합니다.

## 연결별 줄 (ADR-056)

서버 전체의 줄 하나로는 모델마다 서버가 다른 배치를 맞출 수 없습니다. GPU 한 장에 뜬 모델은 한 번에 하나만 받아야
하는데 전체 상한을 1 로 두면 옆의 큰 서버까지 한 줄에 섭니다. `ConnectionGates` 는 **연결마다**(모델·주소·키가 같은
것, `connection_id`) 줄을 하나씩 둡니다. 상한은 연결에 따로 적지 않고 부르는 에이전트의 `max_concurrency` 를 씁니다 —
요청마다 "지금 이 연결에서 도는 요청이 내 상한보다 적은가" 를 보고, 아니면 온 순서대로 기다립니다. 같은 연결을 쓰는
에이전트들이 다른 값을 가져도 상한이 요청마다 뒤집히지 않습니다.

한 판은 연결의 줄을 먼저 서고, 자리를 받은 뒤 서버 전체의 줄을 섭니다. 반대로 하면 연결을 기다리는 동안 전체의 자리
하나를 쥐고 놀립니다.
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

# 지금 LLM 을 부르는 쪽이 어느 대화인지. 비어 있으면 대화 밖의 호출입니다.
LLM_HOLDER: contextvars.ContextVar[str] = contextvars.ContextVar("mado_llm_holder", default="")


@dataclass
class _Waiter:
    holder: str
    future: "asyncio.Future[None]"
    # 이 요청이 스스로 들고 온 상한. None 이면 관문의 상한을 씁니다.
    limit: Optional[int] = None


class LLMGate:
    """온 순서대로 자리를 나눠 주는 세마포어. 누가 쥐고 누가 기다리는지 보입니다."""

    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit or 0))
        self._active: List[str] = []
        self._waiters: Deque[_Waiter] = deque()

    # ------------------------------------------------------------ 설정

    def configure(self, limit: int) -> None:
        """상한을 바꿉니다. 늘었으면 기다리던 요청을 그만큼 바로 들여보냅니다."""
        limit = max(0, int(limit or 0))
        if limit != self.limit:
            self.limit = limit
            self._wake()

    # ------------------------------------------------------------ 자리

    def _has_room(self, limit: Optional[int] = None) -> bool:
        cap = self.limit if limit is None else max(0, int(limit or 0))
        return cap <= 0 or len(self._active) < cap

    def _wake(self) -> None:
        while self._waiters:
            waiter = self._waiters[0]
            if waiter.future.done():
                # 기다리다 취소된 요청. 자리를 주지 않고 넘어갑니다.
                self._waiters.popleft()
                continue
            # 맨 앞 요청이 못 들어가면 뒤의 요청도 기다립니다 (상한이 더 넉넉해도 새치기하지 않습니다).
            if not self._has_room(waiter.limit):
                break
            self._waiters.popleft()
            self._active.append(waiter.holder)
            waiter.future.set_result(None)

    async def acquire(self, holder: str = "", limit: Optional[int] = None) -> None:
        """자리를 받습니다. `limit` 을 주면 관문의 상한 대신 그 값으로 자리가 있는지 봅니다 (연결별 줄)."""
        # 앞에 기다리는 요청이 있으면 자리가 비어 있어도 줄을 섭니다 (새치기 금지).
        if not self._waiters and self._has_room(limit):
            self._active.append(holder)
            return
        waiter = _Waiter(holder, asyncio.get_running_loop().create_future(), limit)
        self._waiters.append(waiter)
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # 자리를 받은 바로 그 순간에 취소됐습니다. 받은 자리를 돌려줍니다.
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
        """LLM 호출 한 판 동안 자리를 쥡니다."""
        who = LLM_HOLDER.get() if holder is None else holder
        await self.acquire(who, limit)
        try:
            yield
        finally:
            self.release(who)

    # ------------------------------------------------------------ 보기

    def _live_waiters(self) -> List[_Waiter]:
        return [w for w in self._waiters if not w.future.done()]

    def position(self, holder: str) -> Optional[int]:
        """이 대화의 요청이 기다리고 있으면 그 앞에 선 요청 수. 기다리지 않으면 None."""
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
    except Exception:  # noqa: BLE001 - 설정을 못 읽어도 LLM 호출을 막지는 않습니다
        return 0


def get_llm_gate() -> LLMGate:
    """프로세스 전체의 관문. 부를 때마다 설정의 상한을 따라갑니다 (화면에서 conf.json 을 고친 경우)."""
    global _gate
    if _gate is None:
        _gate = LLMGate(_configured_limit())
    else:
        _gate.configure(_configured_limit())
    return _gate


class ConnectionGates:
    """연결마다 하나의 줄. 상한은 요청이 들고 옵니다 (그 에이전트의 `max_concurrency`)."""

    def __init__(self) -> None:
        self._gates: Dict[str, LLMGate] = {}

    def gate(self, connection: str) -> LLMGate:
        gate = self._gates.get(connection)
        if gate is None:
            gate = self._gates[connection] = LLMGate(0)
        return gate

    @asynccontextmanager
    async def slot(self, connection: str, limit: int, holder: Optional[str] = None) -> AsyncIterator[None]:
        """그 연결의 자리를 한 판 동안 쥡니다. `limit` 이 0 이면 기다리지 않고 세기만 합니다."""
        async with self.gate(connection).slot(holder, limit=max(0, int(limit or 0))):
            yield

    def position(self, holder: str) -> Optional[int]:
        """이 대화의 요청이 어느 연결의 줄에서 기다리고 있으면 그 앞에 선 요청 수. 기다리지 않으면 None."""
        found = [p for p in (gate.position(holder) for gate in self._gates.values()) if p is not None]
        return min(found) if found else None

    def snapshot(self) -> Dict[str, Tuple[int, int]]:
        """연결 → (도는 요청 수, 기다리는 요청 수). 한가한 연결은 빼고 돌려줍니다."""
        busy: Dict[str, Tuple[int, int]] = {}
        for connection, gate in self._gates.items():
            snap = gate.snapshot()
            if snap["active"] or snap["waiting"]:
                busy[connection] = (snap["active"], snap["waiting"])
        return busy


_connection_gates: Optional[ConnectionGates] = None


def get_connection_gates() -> ConnectionGates:
    """프로세스 전체의 연결별 줄."""
    global _connection_gates
    if _connection_gates is None:
        _connection_gates = ConnectionGates()
    return _connection_gates
