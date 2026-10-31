"""화면 하나가 백그라운드 토론 하나에 붙어 이벤트를 받는 통로.

토론은 화면이 아니라 프로세스(`DebateRunner`)가 돌립니다. 화면은 그 실행의 구독 큐를 하나 받아
이벤트를 자기 화면에 그리고, 화면이 사라지면 구독만 끊습니다 — 토론은 계속됩니다. 전문가 화면,
체험 화면, 쉬운 화면이 같은 규칙으로 붙고 떨어지도록 한 곳에 둡니다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from app.events import Event, EventType
from app.orchestration.turn_run import TurnRun

logger = logging.getLogger(__name__)


def _current_task() -> Optional[asyncio.Task]:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class RunBinding:
    """한 화면의 구독. `attach` 로 붙고, 다른 실행에 붙거나 `detach` 하면 앞의 구독을 끊습니다.

    `apply` 는 이벤트 하나를 그 화면에 그리는 함수입니다. 클라이언트 컨텍스트 안에서 불리므로
    `ui.notify` 같은 호출을 그대로 쓸 수 있습니다. `alive` 가 False 를 돌려주면(화면의 피드가
    지워짐) 거기서 멈춥니다.
    """

    def __init__(
        self,
        client: Any,
        apply: Callable[[Event], Awaitable[None]],
        *,
        alive: Callable[[], bool] = lambda: True,
        name: str = "page",
    ) -> None:
        self.client = client
        self.apply = apply
        self.alive = alive
        self.name = name
        self.run: Optional[TurnRun] = None
        self._queue: Optional["asyncio.Queue[Dict[str, Any]]"] = None
        self._task: Optional[asyncio.Task] = None
        client.on_delete(self.detach)

    def attach(self, run: TurnRun) -> None:
        """진행 중인 토론에 이 화면을 붙입니다 (새로고침 후 재접속 포함). 끝난 실행이면 붙지 않습니다."""
        self.detach()
        if run.status != "running":
            return
        self.run = run
        self._queue = run.subscribe()
        self._task = asyncio.create_task(self._consume(run, self._queue))

    def detach(self) -> None:
        if self.run is not None and self._queue is not None:
            self.run.unsubscribe(self._queue)
        # 이벤트를 그리던 중에 화면을 다시 세우는 경우(발언 다시 실행이 끝났을 때) 그 태스크는 곧 스스로 끝납니다.
        # 자기 자신을 취소하면 다시 세우는 일이 첫 await 에서 끊깁니다.
        if self._task is not None and not self._task.done() and self._task is not _current_task():
            self._task.cancel()
        self.run = self._queue = self._task = None

    async def _consume(self, run: TurnRun, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        """구독 큐를 화면에 흘려보냅니다. 이 태스크는 화면의 것입니다."""
        try:
            while True:
                event = await queue.get()
                if self.client.is_deleted or not self.alive():
                    break
                try:
                    # 이벤트를 그리는 중의 ui.notify 등은 클라이언트 컨텍스트가 있어야 합니다.
                    with self.client:
                        await self.apply(event)
                except RuntimeError as exc:
                    # 갱신 도중 화면이 사라진 경우. 토론은 계속됩니다.
                    logger.debug("Stopped feeding a closed %s: %s", self.name, exc)
                    break
                if event.get("type") == EventType.RUN_FINISHED:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 화면 쪽 오류로 토론을 건드리지 않습니다
            logger.warning("Debate event consumer for the %s stopped: %s", self.name, exc, exc_info=True)
        finally:
            run.unsubscribe(queue)
            if self._queue is queue:
                self.run = self._queue = self._task = None
