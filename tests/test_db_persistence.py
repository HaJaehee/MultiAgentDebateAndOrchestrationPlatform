"""DB 잠금에 발언과 산출물을 잃지 않는지.

실제로 겪은 일입니다. 자리를 비운 사이(화면 잠금) 최종 합성 보고서가
`database is locked` 로 기록되지 않았습니다. 화면에는 흘러갔지만 DB 에는 없어서,
새로고침하거나 앱을 다시 켜면 보고서가 사라졌습니다.

두 겹이 모두 비어 있었습니다.

1. SQLite 를 아무 설정 없이 써서, 누군가 파일을 잡으면 5초만 기다리고 실패했습니다.
   윈도우는 사용자가 없을 때 백신 검사·색인·백업을 돌리고, 그런 프로그램은 5초를
   쉽게 넘깁니다.
2. 기록이 실패하면 로그 한 줄을 남기고 넘어갔습니다. 다시 시도하지도, 다른 곳에
   남기지도 않았습니다.
"""

import asyncio
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database.session as dbs
import app.orchestration.engine as eng
from app.agents.base import Agent
from app.database.models import Base, MessageModel
from app.orchestration.engine import OrchestratorEngine, save_unpersisted
from app.orchestration.state import DebateState
from tests.fake_llm import FakeLLMCaller


def _file_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'mado.db'}"


def _hold_write_lock(path: Path, seconds: float) -> threading.Thread:
    """바깥 프로그램 흉내: DB 파일의 쓰기 잠금을 잠시 잡습니다."""
    held = threading.Event()

    def hold():
        other = sqlite3.connect(str(path), timeout=0)
        other.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(seconds)
        other.rollback()
        other.close()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    held.wait()
    return thread


# =========================================================== 1. SQLite 설정


@pytest.mark.asyncio
async def test_a_file_database_gets_wal_and_a_long_busy_timeout(tmp_path):
    url = _file_url(tmp_path)
    engine = create_async_engine(url)
    assert dbs.configure_sqlite(engine, url) is True

    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        sync = (await conn.execute(text("PRAGMA synchronous"))).scalar()
    await engine.dispose()

    assert mode == "wal", "쓰기마다 저널 파일을 만들고 지우지 않아야 합니다"
    assert busy == dbs.SQLITE_BUSY_TIMEOUT_MS == 30_000, "기본값 5초는 바깥 프로그램을 못 버팁니다"
    assert sync == 1, "WAL 에서는 synchronous=NORMAL(1)"


@pytest.mark.asyncio
async def test_a_memory_database_keeps_working():
    """테스트 대부분이 메모리 DB 를 씁니다. WAL 은 파일 DB 에만 의미가 있습니다."""
    url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url)
    assert dbs.configure_sqlite(engine, url) is False

    async with engine.connect() as conn:
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    await engine.dispose()
    assert busy == 30_000, "대기 시간은 메모리 DB 에도 적용됩니다"


@pytest.mark.asyncio
async def test_wal_is_not_turned_on_for_a_network_location(tmp_path, monkeypatch):
    """네트워크 파일 시스템에서 WAL 은 DB 를 깨뜨릴 수 있습니다."""
    monkeypatch.setattr(dbs, "is_network_location", lambda path: True)
    url = _file_url(tmp_path)
    engine = create_async_engine(url)
    assert dbs.configure_sqlite(engine, url) is False

    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    await engine.dispose()
    assert mode != "wal"
    assert busy == 30_000, "WAL 을 못 켜도 대기 시간은 늘립니다"


def test_unc_paths_are_network_locations(tmp_path):
    assert dbs.is_network_location(Path(r"\\fileserver\share\mado\multiagent.db")) is True
    assert dbs.is_network_location(tmp_path / "multiagent.db") is False


def test_non_sqlite_urls_are_left_alone():
    assert dbs.sqlite_file_path("postgresql+asyncpg://u:p@host/db") is None
    assert dbs.sqlite_file_path("sqlite+aiosqlite:///:memory:") is None
    assert dbs.sqlite_file_path("sqlite+aiosqlite:///./multiagent.db") == Path("./multiagent.db")


@pytest.mark.asyncio
async def test_the_timeout_actually_waits_out_a_held_lock(tmp_path, monkeypatch):
    """설정이 실제로 먹는지. 잠금을 1.5초 잡는 동안 쓰기가 기다렸다 성공해야 합니다.

    (실제 값 30초로 재면 테스트가 길어지므로 대기 시간만 줄여서 봅니다.)
    """
    url = _file_url(tmp_path)

    async def write_while_locked(timeout_ms: int) -> str:
        monkeypatch.setattr(dbs, "SQLITE_BUSY_TIMEOUT_MS", timeout_ms)
        engine = create_async_engine(url)
        dbs.configure_sqlite(engine, url)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS t (v TEXT)"))
        holder = _hold_write_lock(tmp_path / "mado.db", 1.5)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO t VALUES ('보고서')"))
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        finally:
            holder.join()
            await engine.dispose()

    assert "database is locked" in await write_while_locked(200), "짧게 기다리면 실패해야 비교가 됩니다"
    assert await write_while_locked(5_000) == "ok"


# =========================================================== 2. 다시 시도와 파일로 남기기


class _FlakyDB:
    """앞의 몇 번은 커밋이 `database is locked` 로 실패하는 세션."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.pending: list = []
        self.committed: list = []
        self.rollbacks = 0
        self.builds = 0

    def add_all(self, rows):
        self.builds += 1
        self.pending = list(rows)

    async def commit(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            self.pending = []
            raise sqlite3.OperationalError("database is locked")
        self.committed.extend(self.pending)
        self.pending = []

    async def rollback(self):
        self.rollbacks += 1
        self.pending = []


def _engine() -> OrchestratorEngine:
    # `_persist` 는 풀도 LLM 도 쓰지 않습니다. 생성자의 전역 준비를 건너뜁니다.
    return OrchestratorEngine.__new__(OrchestratorEngine)


@pytest.fixture
def no_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(eng, "PERSIST_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(eng, "UNSAVED_DIR", tmp_path / "unsaved")
    return tmp_path / "unsaved"


async def _persist(db, events, content="보고서 본문", **overrides):
    async def on_event(event):
        events.append(event)

    kwargs = dict(
        what="the final synthesis report", label="최종 합성 보고서", kind="synthesis",
        session_id="sess-1234abcd", fallback_title="Master Orchestrator — Round 3",
        fallback_body=content, on_event=on_event,
    )
    kwargs.update(overrides)
    return await _engine()._persist(db, lambda: [MessageModel(id="m1", content=content)], **kwargs)


@pytest.mark.asyncio
async def test_a_transient_lock_is_retried_and_nothing_is_lost(no_wait):
    db, events = _FlakyDB(fail_times=2), []

    assert await _persist(db, events) is True
    assert len(db.committed) == 1 and db.committed[0].content == "보고서 본문"
    assert db.rollbacks == 2, "실패할 때마다 롤백해야 다음 커밋이 됩니다"
    assert db.builds == 3, "시도할 때마다 새 행을 만듭니다"
    assert events == [] and not no_wait.exists(), "결국 기록됐으면 알릴 것도 파일도 없습니다"


@pytest.mark.asyncio
async def test_a_lock_that_never_clears_leaves_the_report_in_a_file(no_wait):
    """이것이 이번 수정의 핵심입니다 — 예전에는 여기서 보고서가 사라졌습니다."""
    db, events = _FlakyDB(fail_times=99), []

    assert await _persist(db, events, content="## 최종 합의\n\n세 계층으로 나눕니다.") is False

    files = list(no_wait.glob("*.md"))
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "세 계층으로 나눕니다." in saved
    assert "sess-1234abcd" in saved and "database is locked" in saved
    assert "synthesis" in files[0].name and "sess-123" in files[0].name

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "persist_failed"
    assert event["label"] == "최종 합성 보고서"
    assert event["saved_to"] == str(files[0])
    assert "database is locked" in event["error"]


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(no_wait):
    class Cancelled(_FlakyDB):
        async def commit(self):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _persist(Cancelled(0), [])


def test_two_failures_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    kwargs = dict(kind="synthesis", session_id="s", title="t", error=RuntimeError("x"),
                  directory=tmp_path)
    first = save_unpersisted(body="첫째", **kwargs)
    second = save_unpersisted(body="둘째", **kwargs)
    assert first != second
    assert {p.read_text(encoding="utf-8").rsplit("\n", 1)[-1] for p in (first, second)} == {"첫째", "둘째"}


def test_an_unwritable_folder_returns_none_instead_of_raising(tmp_path):
    blocker = tmp_path / "unsaved"
    blocker.write_text("폴더가 아니라 파일", encoding="utf-8")
    assert save_unpersisted(kind="synthesis", session_id="s", title="t", body="b",
                            error=RuntimeError("x"), directory=blocker) is None


# =========================================================== 3. 실제 발언 경로


@pytest.mark.asyncio
async def test_the_synthesis_speech_is_labelled_as_the_report(no_wait):
    """`_speak` 가 합성 발언(턴 시작 시각이 주어진 발언)을 보고서로 밝히는지."""
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.llm_caller = FakeLLMCaller(replies={"orchestrator": "## 최종 합의\n\n결론입니다."})
    events = []

    async def on_event(event):
        events.append(event)

    state = DebateState(session_id="sess-synth01", user_prompt="설계해줘")
    agent = Agent(key="orchestrator", name="Master Orchestrator", role="Moderator",
                  model="fake/model", api_key="k")
    await engine._speak(
        db=_FlakyDB(fail_times=99), state=state, agent=agent,
        prompt_messages=[{"role": "user", "content": "합성해줘"}], custom_instructions="",
        round_number=3, msg_type="orchestrator", on_event=on_event,
        turn_started_at=datetime.now(timezone.utc),
    )

    failed = [e for e in events if e["type"] == "persist_failed"]
    assert len(failed) == 1 and failed[0]["label"] == "최종 합성 보고서"
    assert "결론입니다." in Path(failed[0]["saved_to"]).read_text(encoding="utf-8")
    assert any(e["type"] == "message_added" for e in events), "화면에는 그대로 흘러가야 합니다"


@pytest.mark.asyncio
async def test_retry_succeeds_against_a_real_lock_on_a_real_database(tmp_path, monkeypatch):
    """끝에서 끝까지: 진짜 파일 DB, 진짜 잠금, 진짜 세션.

    대기 시간을 일부러 짧게 잡아 첫 시도가 `database is locked` 로 실패하게 하고,
    잠금이 풀린 뒤의 재시도가 같은 발언을 **한 번만** 기록하는지 봅니다.
    """
    monkeypatch.setattr(dbs, "SQLITE_BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(eng, "PERSIST_RETRY_DELAYS", (1.2, 1.2))
    monkeypatch.setattr(eng, "UNSAVED_DIR", tmp_path / "unsaved")

    url = _file_url(tmp_path)
    engine = create_async_engine(url)
    dbs.configure_sqlite(engine, url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    holder = _hold_write_lock(tmp_path / "mado.db", 0.8)
    events = []

    async def on_event(event):
        events.append(event)

    async with factory() as db:
        ok = await _engine()._persist(
            db, lambda: [MessageModel(id="synth-1", session_id="s", sender_key="orchestrator",
                                      sender_name="O", sender_role="R", content="보고서",
                                      round_number=3, msg_type="orchestrator")],
            what="the final synthesis report", label="최종 합성 보고서", kind="synthesis",
            session_id="s", fallback_title="t", fallback_body="보고서", on_event=on_event,
        )
    holder.join()

    async with factory() as db:
        rows = (await db.execute(select(MessageModel).where(MessageModel.id == "synth-1"))).scalars().all()
    await engine.dispose()

    assert ok is True
    assert len(rows) == 1 and rows[0].content == "보고서"
    assert events == [] and not (tmp_path / "unsaved").exists()
