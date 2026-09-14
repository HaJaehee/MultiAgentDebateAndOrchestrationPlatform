"""스트리밍이 화면을 무너뜨리지 않는지.

LLM 토큰 하나마다 카드 전체를 다시 그리던 탓에, 긴 발언이 흐르는 동안 서버와 브라우저가
둘 다 바빠 웹소켓이 끊기고 NiceGUI 가 페이지를 강제로 새로고침했습니다.

* 화면: 조각마다 카드 **전체 내용**으로 `set_content()` — 서버에서 마크다운 전체를 다시
  변환하고 전체 HTML 을 다시 보냈습니다. 20,000자 보고서 한 편에 변환 6,667번, CPU 82초.
* 엔진: 토큰마다 이벤트 한 통 — 화면이 조금만 느려도 구독 큐가 차서 그 화면이 조용히
  구독에서 빠졌습니다.
"""

import asyncio
from types import SimpleNamespace

import pytest

import app.orchestration.engine as eng
from app.agents.base import Agent
from app.agents.llm import LLMUnavailableError
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateState
from app.ui.components.chat_feed import STREAM_RENDER_INTERVAL, ChatFeed


# =========================================================== 화면: 모아서 그리기


class _Markdown:
    def __init__(self):
        self.is_deleted = False
        self.renders = []

    def set_content(self, content):
        self.renders.append(content)


async def _noop(*_args):
    return None


def _feed(*stream_ids):
    feed = ChatFeed(on_send_message=_noop)
    feed.message_container = SimpleNamespace(is_deleted=False)     # 살아 있는 페이지
    for msg_id in stream_ids:
        feed._active_streams[msg_id] = {"content": "", "markdown": _Markdown()}
    return feed


def test_chunks_do_not_render_by_themselves():
    """이것이 핵심입니다 — 조각 하나가 곧 전체 변환 한 번이던 것을 끊습니다."""
    feed = _feed("m1")
    for _ in range(500):
        feed.append_stream_chunk("m1", "가")
    assert feed._active_streams["m1"]["markdown"].renders == []


def test_a_flush_draws_each_changed_card_once_with_everything_so_far():
    feed = _feed("m1", "m2")
    for _ in range(300):
        feed.append_stream_chunk("m1", "가")
    feed.append_stream_chunk("m2", "나")

    feed._flush_streams()

    assert feed._active_streams["m1"]["markdown"].renders == ["가" * 300]
    assert feed._active_streams["m2"]["markdown"].renders == ["나"]


def test_nothing_new_means_nothing_is_redrawn():
    feed = _feed("m1")
    feed.append_stream_chunk("m1", "가")
    feed._flush_streams()
    feed._flush_streams()
    feed._flush_streams()
    assert len(feed._active_streams["m1"]["markdown"].renders) == 1


def test_only_the_cards_that_changed_are_redrawn():
    feed = _feed("m1", "m2")
    feed.append_stream_chunk("m1", "가")
    feed.append_stream_chunk("m2", "나")
    feed._flush_streams()

    feed.append_stream_chunk("m1", "다")
    feed._flush_streams()

    assert feed._active_streams["m1"]["markdown"].renders == ["가", "가다"]
    assert feed._active_streams["m2"]["markdown"].renders == ["나"], "안 바뀐 카드는 건드리지 않습니다"


def test_a_closed_page_is_not_drawn_on():
    feed = _feed("m1")
    feed.append_stream_chunk("m1", "가")
    feed.message_container.is_deleted = True
    feed._flush_streams()
    assert feed._active_streams["m1"]["markdown"].renders == []


def test_the_render_interval_is_short_enough_to_look_live():
    assert 0.1 <= STREAM_RENDER_INTERVAL <= 0.5


# =========================================================== 엔진: 조각 이벤트 모으기


class _OkDB:
    def add_all(self, rows):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _agent():
    return Agent(key="architect", name="System Architect", role="Architecture",
                 model="fake/model", api_key="k")


async def _speak(call_agent, monkeypatch, interval=0.05):
    """`call_agent` 를 갈아 끼운 엔진으로 발언 하나를 돌리고 이벤트를 돌려줍니다."""
    monkeypatch.setattr(eng, "STREAM_EVENT_INTERVAL", interval)
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.llm_caller = SimpleNamespace(call_agent=call_agent)
    events = []

    async def on_event(event):
        events.append(event)

    await engine._speak(
        db=_OkDB(), state=DebateState(session_id="s-stream", user_prompt="설계해줘"),
        agent=_agent(), prompt_messages=[{"role": "user", "content": "설계해줘"}],
        custom_instructions="", round_number=1, msg_type="agent", on_event=on_event,
    )
    return events


def _chunks(events):
    return [e for e in events if e["type"] == "message_stream_chunk"]


def _index(events, etype):
    return next(i for i, e in enumerate(events) if e["type"] == etype)


@pytest.mark.asyncio
async def test_a_burst_of_tokens_becomes_a_few_events(monkeypatch):
    async def call_agent(agent, messages, instructions, *, on_chunk=None, **_kw):
        for _ in range(400):
            await on_chunk("가")
        return "가" * 400, []

    events = await _speak(call_agent, monkeypatch)

    chunks = _chunks(events)
    assert len(chunks) <= 3, f"토큰 400개가 이벤트 {len(chunks)}통이 됐습니다"
    assert "".join(c["delta"] for c in chunks) == "가" * 400, "모아도 한 글자도 잃지 않습니다"


@pytest.mark.asyncio
async def test_text_before_a_slow_tool_is_shown_during_the_wait(monkeypatch):
    """"다음 조각이 오면 보낸다" 였다면 도구가 도는 30초 동안 이 줄이 안 보였습니다."""
    seen_during_pause = []

    async def call_agent(agent, messages, instructions, *, on_chunk=None, **_kw):
        await on_chunk("파일을 확인하겠습니다.")
        await asyncio.sleep(0.3)                       # 오래 걸리는 도구 흉내
        seen_during_pause.extend(_chunks(events_ref))
        await on_chunk(" 확인했습니다.")
        return "파일을 확인하겠습니다. 확인했습니다.", []

    events_ref = []

    monkeypatch.setattr(eng, "STREAM_EVENT_INTERVAL", 0.05)
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.llm_caller = SimpleNamespace(call_agent=call_agent)

    async def on_event(event):
        events_ref.append(event)

    await engine._speak(
        db=_OkDB(), state=DebateState(session_id="s-pause", user_prompt="p"),
        agent=_agent(), prompt_messages=[{"role": "user", "content": "p"}],
        custom_instructions="", round_number=1, msg_type="agent", on_event=on_event,
    )

    assert any("파일을 확인하겠습니다." in c["delta"] for c in seen_during_pause)


@pytest.mark.asyncio
async def test_no_chunk_arrives_after_the_message_is_final(monkeypatch):
    """예약이 남으면 확정된 발언 뒤에 조각이 오고, 러너가 그것을 확정본에 또 붙입니다."""
    async def call_agent(agent, messages, instructions, *, on_chunk=None, **_kw):
        for piece in ("앞", "부", "분"):
            await on_chunk(piece)
        return "앞부분", []

    events = await _speak(call_agent, monkeypatch, interval=0.2)
    await asyncio.sleep(0.4)                           # 예약이 살아 있었다면 여기서 도착합니다

    added = _index(events, "message_added")
    assert all(i < added for i, e in enumerate(events) if e["type"] == "message_stream_chunk")
    assert "".join(c["delta"] for c in _chunks(events)) == "앞부분"


@pytest.mark.asyncio
async def test_a_failed_speech_also_leaves_no_late_chunk(monkeypatch):
    async def call_agent(agent, messages, instructions, *, on_chunk=None, **_kw):
        await on_chunk("쓰다가")
        raise LLMUnavailableError(agent, "client error: 400")

    events = await _speak(call_agent, monkeypatch, interval=0.2)
    await asyncio.sleep(0.4)

    added = _index(events, "message_added")
    assert events[added]["message"]["msg_type"] == "error"
    assert all(i < added for i, e in enumerate(events) if e["type"] == "message_stream_chunk")


@pytest.mark.asyncio
async def test_cancellation_does_not_leave_a_flush_behind(monkeypatch):
    async def call_agent(agent, messages, instructions, *, on_chunk=None, **_kw):
        await on_chunk("중단 직전")
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _speak(call_agent, monkeypatch, interval=0.1)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    await asyncio.sleep(0.25)
    assert all(t.done() for t in pending), "취소된 발언의 예약이 살아남으면 안 됩니다"
