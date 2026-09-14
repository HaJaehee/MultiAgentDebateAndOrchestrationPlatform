"""발언의 시작·종료 시각.

`messages.created_at` 은 사람에게 보여줄 시각이 아닙니다. 발언 행은 LLM 응답이 다 온
**뒤에** 들어가므로 그 값은 대략 끝난 시각이고, 병렬 라운드에서는 아예
`라운드 기준 시각 + 지시 순번(ms)` 으로 덮어씁니다 — 기록을 다시 읽을 때 순서를
맞추는 **정렬 키**이기 때문입니다. 그래서 시작과 끝은 따로 잽니다.

여기서 고정하는 것.

* 모든 발언이 시작·종료 시각을 남긴다. 실패로 끝난 발언도 (언제 포기했는지가 기록이다).
* 걸리는 시간이 없는 기록(사람 발언)은 시작과 끝이 같다.
* 화면은 스트리밍을 시작할 때 시작 시각을, 끝날 때 종료 시각을 받는다.
* 옛 DB 는 두 컬럼을 NULL 로 받는다 — 마이그레이션한 순간에 시작하고 끝난 것처럼 보이면 안 된다.
* 화면과 저장 문서가 같은 규칙으로 적는다. 옛 발언에는 "시작"·"종료" 라는 이름을 붙이지 않는다.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.models import ArtifactModel, MessageModel, SessionModel
from app.database.session import _add_missing_columns, get_session_factory, init_db
from app.export import (
    build_session_markdown,
    format_duration,
    report_completed_line,
    speech_time_text,
    to_local,
)
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.ui.components.chat_feed import ChatFeed, card_time_text
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _fixed_pool

HOLD = 0.05


class _SlowLLM(FakeLLMCaller):
    """발언마다 잠깐 붙잡아, 시작과 끝 사이에 실제로 시간이 흐르게 합니다."""

    async def call_agent(self, *args, **kwargs):
        await asyncio.sleep(HOLD)
        return await super().call_agent(*args, **kwargs)


async def _session(**kwargs) -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    factory = get_session_factory("sqlite+aiosqlite:///:memory:")
    sid = f"timing-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="Timing", strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator", "architect"],
        ))
        await db.commit()
    return sid


async def _rows(sid: str) -> List[MessageModel]:
    async with get_session_factory()() as db:
        return (await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id == sid)
            .order_by(MessageModel.created_at)
        )).scalars().all()


# --------------------------------------------------------------- 기록


@pytest.mark.asyncio
async def test_every_speech_records_when_it_started_and_finished():
    sid = await _session()
    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=_SlowLLM()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요."
    )

    speeches = [r for r in await _rows(sid) if r.msg_type != "user"]
    assert speeches, "발언이 기록되지 않았습니다"
    for row in speeches:
        assert row.started_at is not None and row.finished_at is not None, row.sender_key
        took = (row.finished_at - row.started_at).total_seconds()
        assert took >= HOLD * 0.8, f"{row.sender_key} 의 발언 구간이 실제 걸린 시간보다 짧습니다"


@pytest.mark.asyncio
async def test_a_person_speaking_takes_no_time():
    """시작과 끝을 같게 적어, "시각이 없는 옛 기록" 과 구분되게 합니다."""
    sid = await _session()
    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요."
    )

    user = next(r for r in await _rows(sid) if r.msg_type == "user")
    assert user.started_at is not None
    assert user.started_at == user.finished_at


@pytest.mark.asyncio
async def test_created_at_is_not_the_start_of_the_speech():
    """이 기능이 `created_at` 을 다시 쓰지 않고 컬럼을 새로 만든 이유.

    발언 행은 응답이 다 온 뒤에 들어갑니다. `created_at` 을 시작 시각으로 읽으면
    모든 발언이 실제보다 늦게 시작한 것으로 보입니다.
    """
    sid = await _session()
    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=_SlowLLM()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요."
    )

    row = next(r for r in await _rows(sid) if r.msg_type == "agent")
    assert row.created_at >= row.finished_at
    assert (row.created_at - row.started_at).total_seconds() >= HOLD * 0.8


@pytest.mark.asyncio
async def test_a_failed_speech_still_records_when_it_gave_up():
    sid = await _session()
    await OrchestratorEngine(
        agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller(fail_keys=["architect"])
    ).run_turn(session_id=sid, user_prompt="캐시를 설계해 주세요.")

    failed = next(r for r in await _rows(sid) if r.msg_type == "error")
    assert failed.started_at is not None and failed.finished_at is not None
    assert failed.finished_at >= failed.started_at


@pytest.mark.asyncio
async def test_the_screen_gets_the_start_first_and_the_end_last():
    """스트리밍 카드는 시작할 때 시작 시각을, 확정될 때 종료 시각을 받습니다."""
    sid = await _session()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=_SlowLLM()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요.", on_event=on_event,
    )

    starts = {e["message"]["id"]: e["message"] for e in events
              if e["type"] == "message_stream_start"}
    finals = {e["message"]["id"]: e["message"] for e in events
              if e["type"] == "message_added" and e["message"]["id"] in starts}
    assert starts and finals.keys() == starts.keys()

    for msg_id, start in starts.items():
        assert start["started_at"] is not None
        assert "finished_at" not in start, "시작 이벤트에 종료 시각이 있으면 거짓입니다"
        final = finals[msg_id]
        assert final["started_at"] == start["started_at"]
        assert final["finished_at"] >= final["started_at"]


# --------------------------------------------------------------- 마이그레이션


@pytest.mark.asyncio
async def test_old_databases_get_the_columns_as_null(tmp_path):
    """옛 발언에 기본값을 넣으면 마이그레이션한 순간에 한꺼번에 시작하고 끝난 것처럼 보입니다."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE messages (id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(36), "
            "sender_key VARCHAR(50), sender_name VARCHAR(100), sender_role VARCHAR(100), "
            "content TEXT, round_number INTEGER, msg_type VARCHAR(30), created_at DATETIME)"
        ))
        await conn.execute(text(
            "INSERT INTO messages VALUES ('old', 's', 'architect', 'A', '', '옛 발언', 1, "
            "'agent', '2026-08-01 10:00:00')"
        ))

    async with engine.begin() as conn:
        await _add_missing_columns(conn)
        await _add_missing_columns(conn)  # 두 번 돌려도 같아야 합니다

    async with engine.connect() as conn:
        columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(messages)"))}
        assert {"started_at", "finished_at", "turn_started_at"} <= columns
        row = (await conn.execute(
            text("SELECT started_at, finished_at, turn_started_at FROM messages WHERE id='old'")
        )).one()
        assert tuple(row) == (None, None, None)
    await engine.dispose()


# --------------------------------------------------------------- 표시 규칙

UTC = timezone.utc
T0 = datetime(2026, 9, 11, 1, 0, 0, tzinfo=UTC)


def _clock(value: datetime) -> str:
    return to_local(value).strftime("%H:%M:%S")


def _stamp(value: datetime) -> str:
    return to_local(value).strftime("%Y-%m-%d %H:%M:%S")


def test_a_finished_speech_shows_both_ends_and_how_long_it_took():
    """경과 시간은 종료 시각 오른쪽에, 이름을 붙여 적습니다.

    숫자만 두면 "12초" 가 경과인지 남은 시간인지 읽는 사람이 짐작해야 합니다.
    """
    msg = {"started_at": T0, "finished_at": T0 + timedelta(seconds=12)}

    label, tip = card_time_text(msg)
    end = _clock(T0 + timedelta(seconds=12))
    assert label == f"{_clock(T0)} → {end} · 경과 12초"
    assert label.index("경과") > label.index(end), "경과는 종료 시각 오른쪽이어야 합니다"
    assert tip == speech_time_text(msg)
    assert speech_time_text(msg) == (
        f"시작 {_stamp(T0)} · 종료 {_stamp(T0 + timedelta(seconds=12))} · 경과 12초"
    )
    # 턴을 마무리한 발언이 아니면 총 경과를 적지 않습니다.
    assert "총 경과" not in speech_time_text(msg)


def test_a_speech_still_streaming_says_so():
    label, tip = card_time_text({"started_at": T0})
    assert label == f"{_clock(T0)} 시작 · 진행 중"
    assert tip == f"시작 {_stamp(T0)}"


def test_a_speech_across_local_midnight_dates_its_end():
    """`23:59:50 → 00:00:12` 만 적으면 시간이 거꾸로 흐른 것처럼 보입니다."""
    start = datetime(2026, 9, 11, 23, 59, 50).astimezone()  # 이 기계 시간대의 자정 직전
    msg = {"started_at": start, "finished_at": start + timedelta(seconds=22)}

    label, _ = card_time_text(msg)
    end = to_local(start + timedelta(seconds=22))
    assert label.startswith("23:59:50 → ")
    assert end.strftime("%m-%d ") in label


def test_an_instant_record_shows_a_single_time():
    label, tip = card_time_text({"started_at": T0, "finished_at": T0})
    assert label == _clock(T0)
    assert "→" not in label and "시작" not in tip
    assert speech_time_text({"started_at": T0, "finished_at": T0}) == _stamp(T0)


def test_an_old_record_is_not_called_a_start_or_an_end():
    """두 컬럼이 없던 발언은 `created_at` 하나뿐인데, 그게 시작인지 끝인지 알 수 없습니다."""
    legacy = {"created_at": T0}

    label, _ = card_time_text(legacy)
    assert label == _clock(T0)
    assert speech_time_text(legacy) == _stamp(T0)
    assert "시작" not in speech_time_text(legacy) and "종료" not in speech_time_text(legacy)


def test_nothing_to_show_hides_the_line():
    assert card_time_text({}) == ("", "")
    assert speech_time_text({}) == ""


def test_timestamps_read_back_from_sqlite_are_treated_as_utc():
    """SQLite 는 오프셋을 버립니다. 시간대 없는 값을 현지 시각으로 읽으면 9시간 어긋납니다."""
    naive = T0.replace(tzinfo=None)
    assert card_time_text({"started_at": naive, "finished_at": naive})[0] == _clock(T0)


@pytest.mark.parametrize("seconds, expected", [
    (0.42, "0.4초"), (7, "7.0초"), (12.4, "12초"), (125, "2분 5초"),
    (120, "2분"), (3600, "1시간"), (3725, "1시간 2분"),
])
def test_durations_read_naturally(seconds, expected):
    assert format_duration(seconds) == expected


# --------------------------------------------------------------- 저장 문서


def test_the_saved_document_carries_start_end_and_duration():
    messages = [
        {"id": "u", "sender_key": "user", "sender_name": "User", "content": "요청",
         "round_number": 0, "msg_type": "user",
         "created_at": T0, "started_at": T0, "finished_at": T0},
        {"id": "a", "sender_key": "architect", "sender_name": "Architect", "content": "제안",
         "round_number": 1, "msg_type": "agent",
         "created_at": T0 + timedelta(seconds=31),
         "started_at": T0 + timedelta(seconds=1), "finished_at": T0 + timedelta(seconds=31)},
        {"id": "old", "sender_key": "critic", "sender_name": "Critic", "content": "옛 발언",
         "round_number": 1, "msg_type": "agent", "created_at": T0 + timedelta(minutes=5)},
    ]

    doc = build_session_markdown({"title": "시각"}, messages)

    assert f"*{_stamp(T0)}*" in doc
    assert (
        f"*시작 {_stamp(T0 + timedelta(seconds=1))} · "
        f"종료 {_stamp(T0 + timedelta(seconds=31))} · 경과 30초*"
    ) in doc
    # 옛 발언은 시각 하나만, 이름 없이.
    assert f"*{_stamp(T0 + timedelta(minutes=5))}*" in doc


# --------------------------------------------------------------- 화면 카드


class _Widget:
    def __init__(self):
        self.text, self.visible, self.is_deleted = "", True, False

    def set_text(self, value):
        self.text = value

    def set_visibility(self, value):
        self.visible = value

    def set_content(self, value):
        self.text = value


def test_finishing_a_streamed_card_replaces_in_progress_with_the_end_time(monkeypatch):
    async def noop(*_args):
        pass

    feed = ChatFeed(noop, on_interject=noop, on_stop=noop)
    monkeypatch.setattr(feed, "_scroll_to_bottom", lambda *a, **k: None)
    monkeypatch.setattr(feed, "_apply_clamp", lambda *a, **k: None)
    label, tip = _Widget(), _Widget()
    label.set_text(card_time_text({"started_at": T0})[0])
    feed._active_streams["m1"] = {
        "id": "m1", "content": "쓰는 중", "markdown": _Widget(), "msg_type": "agent",
        "time_label": label, "time_tip": tip, "started_at": T0, "tool_container": None,
    }
    assert "진행 중" in label.text

    # 확정본에 시작 시각이 빠져 있어도 스트리밍 시작 때 받은 값으로 채웁니다.
    feed._finalize_streaming_message({
        "id": "m1", "content": "다 썼습니다", "msg_type": "agent",
        "finished_at": T0 + timedelta(seconds=4),
    })

    assert label.text == f"{_clock(T0)} → {_clock(T0 + timedelta(seconds=4))} · 경과 4.0초"
    assert "진행 중" not in label.text
    assert tip.text.startswith("시작 ")


# --------------------------------------------------------------- 종합 보고서


async def _artifacts(sid: str) -> List[ArtifactModel]:
    async with get_session_factory()() as db:
        return (await db.execute(
            select(ArtifactModel).where(ArtifactModel.session_id == sid)
        )).scalars().all()


@pytest.mark.asyncio
async def test_the_final_report_says_when_it_was_completed():
    """보고서는 아티팩트로 떼어져 돌아다니므로, 언제 나온 결론인지를 본문에 적습니다.

    그 시각은 **합성 발언이 끝난 시각**과 같아야 합니다 — 화면 카드와 저장 문서가
    말하는 종료 시각과 보고서가 말하는 완료 시각이 다르면 어느 쪽도 믿을 수 없습니다.
    """
    sid = await _session()
    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요."
    )

    report = next(a for a in await _artifacts(sid) if "최종 결론" in a.title)
    synthesis = [r for r in await _rows(sid) if r.msg_type == "orchestrator"][-1]

    total = format_duration((synthesis.finished_at - synthesis.turn_started_at).total_seconds())
    expected = f"*보고서 완료: {_stamp(synthesis.finished_at)} · 총 경과 {total}*"
    assert report.content.rstrip().endswith(expected)
    # 결론 본문은 그대로 남고, 완료 줄은 구분선 뒤에 붙습니다.
    assert report.content.startswith(synthesis.content.rstrip())
    assert f"---\n\n{expected}" in report.content


@pytest.mark.asyncio
async def test_the_completion_line_does_not_leak_into_extracted_artifacts():
    """코드·다이어그램은 원문에서 뽑습니다. 완료 줄이 그쪽에 섞이면 실행되는 코드가 깨집니다."""
    sid = await _session()
    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요."
    )

    others = [a for a in await _artifacts(sid) if "최종 결론" not in a.title]
    assert others, "코드나 다이어그램 아티팩트가 뽑히지 않았습니다"
    for art in others:
        assert "보고서 완료" not in art.content, art.title


@pytest.mark.asyncio
async def test_a_failed_synthesis_is_not_called_a_completed_report():
    """합성에 실패한 아티팩트는 실패 안내입니다. "보고서 완료" 라고 적으면 거짓입니다."""
    sid = await _session()
    await OrchestratorEngine(
        agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller(fail_keys=["orchestrator"])
    ).run_turn(session_id=sid, user_prompt="캐시를 설계해 주세요.")

    failed = next(a for a in await _artifacts(sid) if a.artifact_type == "markdown")
    assert "합성 실패" in failed.title
    assert "보고서 완료" not in failed.content


def test_no_completion_time_means_no_line():
    assert report_completed_line(None) == ""
    assert report_completed_line(T0) == f"*보고서 완료: {_stamp(T0)}*"


# --------------------------------------------------------------- 턴의 총 경과


@pytest.mark.asyncio
async def test_only_the_synthesis_speech_carries_the_turn_start():
    """`turn_started_at` 은 턴을 마무리한 합성 발언에만 있고, 그 값은 턴을 연 요청의 시각입니다."""
    sid = await _session()
    await OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=_SlowLLM()).run_turn(
        session_id=sid, user_prompt="캐시를 설계해 주세요."
    )

    rows = await _rows(sid)
    opening = next(r for r in rows if r.msg_type == "user")
    marked = [r for r in rows if r.turn_started_at is not None]

    assert len(marked) == 1, "턴을 마무리하는 발언은 하나뿐입니다"
    synthesis = marked[0]
    assert synthesis is rows[-1] or synthesis.created_at == max(r.created_at for r in rows)
    assert synthesis.turn_started_at == opening.started_at
    # 턴에는 계획·토론·합성이 다 들어 있으니, 총 경과는 합성 발언 하나보다 깁니다.
    turn = (synthesis.finished_at - synthesis.turn_started_at).total_seconds()
    speech = (synthesis.finished_at - synthesis.started_at).total_seconds()
    assert turn > speech


@pytest.mark.asyncio
async def test_an_interjection_right_after_planning_does_not_restart_the_turn():
    """이 값을 기록에서 추론하지 않고 따로 적는 이유.

    계획 직후의 개입은 턴을 연 요청과 똑같이 `msg_type="user"`, `round_number=0` 으로
    들어갑니다. "마지막 round 0 사람 발언" 을 턴 시작으로 읽었다면 총 경과가 조용히
    짧아졌을 것입니다.
    """
    sid = await _session()
    control = TurnControl()

    class InterjectDuringPlanning(_SlowLLM):
        planned = False

        async def call_agent(self, agent, messages, *args, **kwargs):
            if agent.key == "orchestrator" and not self.planned:
                self.planned = True
                control.add_note("계획 직후에 끼어든 개입")
            return await super().call_agent(agent, messages, *args, **kwargs)

    await OrchestratorEngine(
        agent_pool=_fixed_pool(), llm_caller=InterjectDuringPlanning()
    ).run_turn(session_id=sid, user_prompt="캐시를 설계해 주세요.", control=control)

    rows = await _rows(sid)
    users = [r for r in rows if r.msg_type == "user"]
    assert len(users) == 2 and all(u.round_number == 0 for u in users),         "이 시나리오는 두 사람 발언이 모두 round 0 이어야 성립합니다"
    opening, interjection = users
    assert interjection.started_at > opening.started_at

    synthesis = next(r for r in rows if r.turn_started_at is not None)
    assert synthesis.turn_started_at == opening.started_at


def test_the_saved_document_puts_the_turn_total_after_the_synthesis_end():
    """저장 문서도 보고서와 같은 자리에 총 경과를 적습니다 — 종료·경과 오른쪽."""
    opening = T0
    messages = [
        {"id": "u", "sender_key": "user", "sender_name": "User", "content": "요청",
         "round_number": 0, "msg_type": "user",
         "created_at": opening, "started_at": opening, "finished_at": opening},
        {"id": "a", "sender_key": "architect", "sender_name": "Architect", "content": "제안",
         "round_number": 1, "msg_type": "agent", "created_at": T0 + timedelta(minutes=5),
         "started_at": T0 + timedelta(minutes=1), "finished_at": T0 + timedelta(minutes=5)},
        {"id": "s", "sender_key": "orchestrator", "sender_name": "Orchestrator",
         "content": "합성", "round_number": 2, "msg_type": "orchestrator",
         "created_at": T0 + timedelta(minutes=12, seconds=5),
         "started_at": T0 + timedelta(minutes=10),
         "finished_at": T0 + timedelta(minutes=12, seconds=5),
         "turn_started_at": opening},
    ]

    doc = build_session_markdown({"title": "턴 시각"}, messages)

    synth_line = (
        f"*시작 {_stamp(T0 + timedelta(minutes=10))} · "
        f"종료 {_stamp(T0 + timedelta(minutes=12, seconds=5))} · "
        f"경과 2분 5초 · 총 경과 12분 5초*"
    )
    assert synth_line in doc
    assert doc.count("총 경과") == 1, "턴을 마무리한 발언에만 붙어야 합니다"


def test_the_report_puts_the_turn_total_right_of_the_completion_time():
    line = report_completed_line(T0 + timedelta(minutes=12, seconds=5), T0)
    assert line == f"*보고서 완료: {_stamp(T0 + timedelta(minutes=12, seconds=5))} · 총 경과 12분 5초*"
    assert line.index("총 경과") > line.index("보고서 완료")
    # 턴 시작을 모르면 완료 시각만 적습니다 — 짐작한 총 경과를 적느니 비웁니다.
    assert "총 경과" not in report_completed_line(T0)
