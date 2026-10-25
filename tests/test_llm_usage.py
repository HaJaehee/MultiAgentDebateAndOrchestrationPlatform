"""LLM 토큰 및 응답 시간 계측 테스트 (app/agents/llm_usage.py, v1.5).

1. 프로바이더별 응답 규격(OpenAI, Anthropic) 사용량 파싱 검증.
2. 중첩(Nested) 측정 컨텍스트 검증: 내부(발언) 호출 누적 및 외부(턴) 동시 반영, 컨텍스트 종료 후 계측 차단.
3. 호출기 유형별 계측: 표준 호출기의 토큰/시간 측정 및 모의 호출기의 호출 횟수/시간 계측 검증.
4. 발언 메타(`llm_meta.usage`) 및 턴 레코드(`turns.usage`) 영속화 검증 (보조 시스템 호출 포함).
5. UI 카드 및 마크다운 내보내기 시 통일된 사용량 표기 검증.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.agents import llm_usage
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_usage import Usage, measuring, usage_line, usage_of
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.export import build_session_markdown
from app.orchestration import turns
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import fallback_sentence
from app.ui.components.chat_feed.common import card_time_text
from tests.fake_llm import FakeLLMCaller

PER_CALL = {"prompt_tokens": 1000, "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 600}}


def test_usage_is_read_from_openai_and_anthropic_shapes():
    openai = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=120, completion_tokens=30,
        prompt_tokens_details=SimpleNamespace(cached_tokens=100),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
    ))
    assert usage_of(openai).model_dump() == {
        "prompt_tokens": 120, "completion_tokens": 30, "cached_tokens": 100, "reasoning_tokens": 12,
        "calls": 0, "llm_seconds": 0.0,
    }
    anthropic = {"usage": {"prompt_tokens": 50, "completion_tokens": 5, "cache_read_input_tokens": 40}}
    assert usage_of(anthropic).cached_tokens == 40
    assert usage_of(SimpleNamespace()).empty
    assert usage_of({"usage": {"prompt_tokens": "x", "completion_tokens": None}}).empty


def test_meters_nest_and_stop_counting_when_closed():
    with measuring() as turn:
        llm_usage.note_call(1.0)
        with measuring() as speech:
            llm_usage.note_response({"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
            llm_usage.note_call(2.0)
        llm_usage.note_response({"usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    llm_usage.note_call(9.0)  # 측정 컨텍스트(measuring)가 활성화되어 있지 않음

    assert (speech.prompt_tokens, speech.completion_tokens, speech.calls, speech.llm_seconds) == (10, 2, 1, 2.0)
    assert (turn.prompt_tokens, turn.completion_tokens, turn.calls, turn.llm_seconds) == (15, 3, 2, 3.0)
    assert turn.minus(speech).prompt_tokens == 5


def test_usage_round_trips_through_records():
    used = Usage(prompt_tokens=3, completion_tokens=4, calls=1, llm_seconds=1.234)
    assert used.as_meta()["llm_seconds"] == 1.23
    assert Usage.of(used.as_meta()).total_tokens == 7
    assert Usage.of(None).empty and Usage.of("garbage").empty


def _agent() -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", model="openai/gpt-4o", api_key="k",
                 max_tokens=1024, max_context_window=32768, max_tool_iterations=2)


class _NoTools:
    def get_openai_tools_for_servers(self, servers):
        return []


def _message(text):
    return SimpleNamespace(content=text, tool_calls=None, reasoning_content=None,
                           model_dump=lambda: {"role": "assistant", "content": text})


@pytest.mark.asyncio
async def test_the_default_completion_records_tokens_and_time():
    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")  # 비스트리밍 경로로
        return SimpleNamespace(
            choices=[SimpleNamespace(message=_message("답입니다"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=321, completion_tokens=12),
        )

    with patch("litellm.acompletion", side_effect=fake_acompletion), measuring() as meter:
        content, _ = await LLMCaller(mcp_manager=_NoTools()).call_agent(
            _agent(), [{"role": "user", "content": "질문"}]
        )

    assert content == "답입니다"
    assert (meter.prompt_tokens, meter.completion_tokens) == (321, 12)
    # 스트리밍 시도와 비스트리밍 재시도는 `_complete_unthrottled` 한 번 안에서 일어납니다.
    assert meter.calls == 1 and meter.llm_seconds >= 0


@pytest.mark.asyncio
async def test_an_overridden_completion_counts_calls_and_time_but_no_tokens():
    class Scripted(LLMCaller):
        async def _complete_unthrottled(self, agent, messages, tools, on_chunk, tool_choice):
            return _message("대역의 답"), "stop"

    with measuring() as meter:
        await Scripted(mcp_manager=_NoTools()).call_agent(_agent(), [{"role": "user", "content": "질문"}])

    assert meter.calls == 1 and meter.total_tokens == 0
    # 토큰을 모르면 시간만 적습니다.
    assert usage_line(Usage(calls=1, llm_seconds=2.0)) == "LLM 2.0초"


async def _session(db_url: str) -> str:
    await init_db(db_url)
    sid = f"usage-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid, title="사용량", strategy="sequential_debate", max_rounds=2,
            active_agents=["orchestrator", "architect", "coder", "critic"],
        ))
        await db.commit()
    return sid


@pytest.mark.asyncio
async def test_speeches_and_the_turn_record_their_usage():
    db_url = "sqlite+aiosqlite:///:memory:"
    sid = await _session(db_url)
    caller = FakeLLMCaller(usage=PER_CALL)
    events = []

    async def on_event(event):
        events.append(event)

    await OrchestratorEngine(llm_caller=caller).run_turn(session_id=sid, user_prompt="설계해 줘", on_event=on_event)

    async with get_session_factory(db_url)() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        totals = await turns.usage_by_session(db)

    speeches = [m for m in messages if m.sender_key != "user" and m.llm_meta]
    assert speeches, "발언마다 사용량이 남아야 합니다"
    for m in speeches:
        assert m.llm_meta["usage"]["prompt_tokens"] == 1000 and m.llm_meta["usage"]["calls"] == 1
        # 사용량만 있는 기록을 "기본 모델이 대신 답함" 으로 읽지 않습니다.
        assert fallback_sentence(m.llm_meta) == "" and "fallback" not in m.llm_meta
    assert not [m for m in messages if m.sender_key == "user" and m.llm_meta]

    # 턴 합계는 모든 호출(계획·발언·장부·합성…)을 셉니다. 발언 합보다 크고, 대역이 받은 호출 수와 같습니다.
    total = Usage.of(turn.usage)
    assert total.calls == len(caller.calls)
    assert total.prompt_tokens == 1000 * len(caller.calls)
    assert total.cached_tokens == 600 * len(caller.calls)
    assert total.calls > len(speeches)
    assert totals[sid].prompt_tokens == total.prompt_tokens

    completed = next(e for e in events if e["type"] == "turn_completed")
    assert completed["usage"]["calls"] == total.calls


@pytest.mark.asyncio
async def test_a_turn_without_reported_usage_leaves_no_usage_rows():
    """사용량을 알리지 않는 호출기(대역)만 썼으면 발언에도 턴에도 사용량을 적지 않습니다."""
    db_url = "sqlite+aiosqlite:///:memory:"
    sid = await _session(db_url)
    await OrchestratorEngine(llm_caller=FakeLLMCaller()).run_turn(session_id=sid, user_prompt="설계해 줘")
    async with get_session_factory(db_url)() as db:
        metas = (await db.execute(select(MessageModel.llm_meta).where(MessageModel.session_id == sid))).scalars().all()
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        totals = await turns.usage_by_session(db)
    assert all(meta is None for meta in metas)
    assert turn.usage is None and sid not in totals


def test_cards_and_exports_show_the_same_usage_wording():
    meta = {"usage": Usage(prompt_tokens=12_345, completion_tokens=1_100, cached_tokens=10_000,
                           calls=2, llm_seconds=8.24).as_meta()}
    msg = {"id": "m1", "sender_key": "coder", "sender_name": "Dev", "sender_role": "Impl",
           "msg_type": "agent", "content": "본문",
           "started_at": "2026-10-25T01:00:00+00:00", "finished_at": "2026-10-25T01:00:30+00:00",
           "llm_meta": meta}
    label, tip = card_time_text(msg)
    assert label.endswith("· 토큰 13.4k")
    assert "입력 12.3k (캐시 10.0k) · 출력 1.1k · LLM 8.2초" in tip

    document = build_session_markdown(
        {"title": "t", "strategy": "sequential_debate",
         "usage": Usage(prompt_tokens=50_000, completion_tokens=2_000)},
        [msg],
    )
    assert "| LLM 사용량 (누적) | 입력 50.0k · 출력 2.0k |" in document
    assert "입력 12.3k (캐시 10.0k) · 출력 1.1k · LLM 8.2초*" in document

    # 사용량을 모르는 발언은 예전 그대로입니다.
    plain = dict(msg, llm_meta=None)
    assert "토큰" not in card_time_text(plain)[0]
