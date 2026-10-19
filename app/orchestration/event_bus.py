"""이벤트 버스 추상화 — 토론 및 시스템 이벤트를 세션 및 브라우저 구독자에게 브로드캐스트합니다.

폐쇄망 환경에서 기본으로 동작하는 Python 내장 비동기 큐 기반(`InMemoryEventBus`)과
엔터프라이즈 확장을 위한 플러그형 프로토콜(`BaseEventBus`)을 제공합니다.
느린 구독자(Slow Consumer)가 발생하더라도 전체 토론 파이프라인이 블로킹되지 않도록
버퍼 상한(`maxsize`) 초과 시 해당 큐를 안전하게 탈락(Drop)시킵니다.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_MAXSIZE = 2000


class BaseEventBus(ABC):
    """이벤트 버스 브로커 인터페이스 (In-Memory, Redis, IPC 등으로 교체 가능)."""

    @abstractmethod
    async def publish(self, channel: str, event: Dict[str, Any]) -> None:
        """이벤트를 채널 구독자들에게 발행합니다."""

    @abstractmethod
    def publish_nowait(self, channel: str, event: Dict[str, Any]) -> None:
        """비동기 대기 없이 이벤트를 즉시 팬아웃합니다."""

    @abstractmethod
    def subscribe(self, channel: str, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> asyncio.Queue[Dict[str, Any]]:
        """채널에 새 구독 큐를 생성하여 반환합니다."""

    @abstractmethod
    def unsubscribe(self, channel: str, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        """구독 큐를 해제합니다."""

    @abstractmethod
    def subscriber_count(self, channel: str) -> int:
        """채널의 현재 구독자 수를 반환합니다."""

    @abstractmethod
    def clear(self, channel: Optional[str] = None) -> None:
        """특정 채널 또는 전체 채널의 구독자를 정리합니다."""


class InMemoryEventBus(BaseEventBus):
    """Python 내장 asyncio.Queue 기반의 인메모리 이벤트 버스 (폐쇄망 기본 구현)."""

    def __init__(self) -> None:
        self._channels: Dict[str, Set[asyncio.Queue[Dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    def subscribe(self, channel: str, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> asyncio.Queue[Dict[str, Any]]:
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        subscribers = self._channels.setdefault(channel, set())
        subscribers.add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        subscribers = self._channels.get(channel)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._channels.pop(channel, None)

    def subscriber_count(self, channel: str) -> int:
        subscribers = self._channels.get(channel)
        return len(subscribers) if subscribers is not None else 0

    def publish_nowait(self, channel: str, event: Dict[str, Any]) -> None:
        subscribers = self._channels.get(channel)
        if not subscribers:
            return

        for queue in list(subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "Dropping slow subscriber queue on channel '%s' (maxsize reached)", channel
                )
                subscribers.discard(queue)

        if not subscribers:
            self._channels.pop(channel, None)

    async def publish(self, channel: str, event: Dict[str, Any]) -> None:
        self.publish_nowait(channel, event)

    def clear(self, channel: Optional[str] = None) -> None:
        if channel is None:
            self._channels.clear()
        else:
            self._channels.pop(channel, None)


_global_event_bus: Optional[BaseEventBus] = None


def get_event_bus() -> BaseEventBus:
    """전역 기본 이벤트 버스 인스턴스를 반환합니다."""
    global _global_event_bus
    if _global_event_bus is None:
        _global_event_bus = InMemoryEventBus()
    return _global_event_bus


def set_event_bus(bus: BaseEventBus) -> None:
    """테스트 또는 사용자 정의 브로커 설정을 위해 전역 이벤트 버스를 교체합니다."""
    global _global_event_bus
    _global_event_bus = bus
