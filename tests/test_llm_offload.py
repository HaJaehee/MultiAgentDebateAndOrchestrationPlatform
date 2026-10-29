"""스트리밍이 끝난 뒤 조각을 응답 하나로 조립하는 일(`litellm.stream_chunk_builder`)이 이벤트 루프를 붙잡지 않는가.

조립은 동기 함수입니다. 루프 위에서 돌면 끝날 때까지 같은 프로세스의 다른 발언 스트림, 다른 대화, 화면 갱신,
승인·정지 처리가 모두 멈춥니다. 그 발언은 어차피 조립을 기다려야 하지만, 나머지는 기다릴 이유가 없습니다.

1. 조립하는 동안 다른 코루틴이 계속 돕니다.
2. 답·끝난 이유·사용량은 전과 같습니다.
3. 조립 중에 취소하면 기다리던 쪽이 바로 빠집니다 (스레드는 끝까지 돌고 결과만 버려집니다).
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import app.agents.llm as llm_module
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_usage import measuring

BUILD_SECONDS = 0.5


def _agent() -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", model="openai/gpt-4o", api_key="k",
                 max_tokens=1024, max_context_window=32768)


class _NoTools:
    def get_openai_tools_for_servers(self, servers):
        return []


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text, tool_calls=None, reasoning_content=None,
                           model_dump=lambda: {"role": "assistant", "content": text})


def _install(monkeypatch, built: List[Dict[str, Any]]) -> None:
    """조각 세 개를 흘리는 모델과, BUILD_SECONDS 동안 CPU 를 쥐고 있는 조립 함수."""

    async def fake_acompletion(**kwargs):
        async def _stream():
            for piece in ("가", "나", "다"):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])

        return _stream()

    def slow_builder(chunks, messages=None):
        built.append({"chunks": len(chunks), "messages": messages, "thread": threading.current_thread().name})
        time.sleep(BUILD_SECONDS)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=_message("가나다"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
        )

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", slow_builder)


async def _complete(caller: LLMCaller, messages: List[Dict[str, Any]]):
    return await caller._complete_unthrottled(_agent(), messages, None, None, "auto")


@pytest.mark.asyncio
async def test_other_coroutines_keep_running_while_the_answer_is_assembled(monkeypatch):
    built: List[Dict[str, Any]] = []
    _install(monkeypatch, built)
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    start = ticks
    try:
        await _complete(LLMCaller(mcp_manager=_NoTools()), [{"role": "user", "content": "질문"}])
    finally:
        done.set()
        await task

    # 루프를 붙잡았다면 0.5초 동안 한 번도 돌지 못합니다. 넉넉히 잡아 다섯 번.
    assert ticks - start >= 5, f"조립하는 동안 다른 코루틴이 {ticks - start}번밖에 돌지 못했습니다"
    assert built[0]["thread"] != threading.main_thread().name


@pytest.mark.asyncio
async def test_the_answer_finish_reason_and_usage_are_unchanged(monkeypatch):
    built: List[Dict[str, Any]] = []
    _install(monkeypatch, built)
    messages = [{"role": "user", "content": "질문"}]

    with measuring() as meter:
        message, finish = await _complete(LLMCaller(mcp_manager=_NoTools()), messages)

    assert message.content == "가나다" and finish == "stop"
    assert (meter.prompt_tokens, meter.completion_tokens) == (11, 3), "사용량은 루프로 돌아와 기록합니다"
    assert built[0]["chunks"] == 3
    assert built[0]["messages"] == messages and built[0]["messages"] is not messages, "목록은 복사해 넘깁니다"


@pytest.mark.asyncio
async def test_cancelling_during_assembly_returns_at_once(monkeypatch):
    built: List[Dict[str, Any]] = []
    _install(monkeypatch, built)

    task = asyncio.create_task(_complete(LLMCaller(mcp_manager=_NoTools()), [{"role": "user", "content": "질문"}]))
    while not built:
        await asyncio.sleep(0.01)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - started < BUILD_SECONDS / 2, "취소는 조립이 끝나기를 기다리지 않습니다"
    await asyncio.sleep(BUILD_SECONDS)  # 스레드가 끝나게 둡니다
