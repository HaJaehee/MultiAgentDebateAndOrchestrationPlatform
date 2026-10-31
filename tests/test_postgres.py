"""PostgreSQL 호환 (ADR-041).

SQLite 는 외래 키도, 문자열 길이도, NUL 문자도 검사하지 않습니다. 그래서 `db_url` 만 바꾸면 되는 것처럼 보이던 코드가 실제 서버에서는
발언 기록 실패로 나타납니다. 앞부분은 서버 없이 보는 시험이고, 뒷부분은 `MADO_TEST_POSTGRES_URL` 이 있을 때 실제 서버로 돕니다.

    MADO_TEST_POSTGRES_URL=postgresql+asyncpg://user@host/mado_test pytest tests/test_postgres.py tests/test_session_search.py
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from app.database import search
from app.database import session as db_session
from app.database.models import (
    ArtifactModel,
    Base,
    MessageModel,
    SessionAgentModel,
    SessionModel,
    SpeechDraftModel,
    ToolCallRecordModel,
    TurnModel,
)
from app.database.search import search_sessions
from app.database.session import (
    _ADDED_COLUMNS,
    _column_ddl_for,
    clean_parameters,
    engine_options,
    instance_presence,
    no_other_instance,
    prepare_schema,
    safe_db_url,
)
from app.easy.models import EasySessionModel
from app.mcp import manager as mcp_manager
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.tool_audit import blocked_tool_calls
from app.timestamps import as_utc
from app.trial import store as trial_store
from app.trial.models import TrialFeedbackModel, TrialSessionModel, TrialUserModel
from tests.fake_llm import FakeLLMCaller
from tests.pg_support import POSTGRES_URL, empty_schema, postgres_database
from tests.test_graph_debate import _pool

T0 = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
PG = postgresql.dialect()


# =============================================================== 1. 서버 없이 보는 것


def test_no_column_has_a_length_limit_on_postgres():
    """SQLite 는 길이를 본 적이 없습니다. PostgreSQL 만 검사하면 긴 역할 설명 하나로 기록이 실패합니다."""
    ddl = "\n".join(str(CreateTable(t).compile(dialect=PG)) for t in Base.metadata.sorted_tables)
    assert "CREATE TABLE messages" in ddl and "VARCHAR" not in ddl
    assert "VARCHAR(255)" in str(CreateTable(SessionModel.__table__).compile(dialect=sqlite.dialect()))


def test_every_foreign_key_to_a_session_cascades():
    """대화 삭제는 `DELETE FROM sessions` 입니다. 하나라도 빠지면 PostgreSQL 에서 그 삭제가 외래 키 위반으로 실패합니다."""
    keys = [
        fk for table in Base.metadata.sorted_tables for fk in table.foreign_keys
        if fk.column.table.name == "sessions"
    ]
    assert len(keys) >= 8
    assert [fk.parent for fk in keys if fk.ondelete != "CASCADE"] == []


def test_added_columns_are_translated_from_the_model():
    translate = lambda t, c: _column_ddl_for(PG, t, c, _ADDED_COLUMNS[t][c])  # noqa: E731
    assert translate("messages", "started_at") == "TIMESTAMP WITH TIME ZONE"
    assert translate("messages", "graph_port") == "TEXT"
    assert translate("messages", "llm_meta") == "JSON"
    assert translate("sessions", "personas_locked") == "BOOLEAN NOT NULL DEFAULT false"
    assert translate("sessions", "converge_early") == "BOOLEAN NOT NULL DEFAULT true"
    assert translate("sessions", "parallel_limit") == "INTEGER NOT NULL DEFAULT 3"
    assert translate("sessions", "known_agents") == "JSON NOT NULL DEFAULT '[]'"
    assert translate("sessions", "graph_id") == "TEXT NOT NULL DEFAULT ''"
    assert translate("session_agents", "config_snapshot") == "JSON"
    for table, columns in _ADDED_COLUMNS.items():
        for column in columns:
            assert column in Base.metadata.tables[table].c, f"{table}.{column} 이 모델에 없습니다"
            assert "DATETIME" not in translate(table, column)


def test_added_columns_stay_as_written_on_sqlite():
    ddl = _ADDED_COLUMNS["sessions"]["personas_locked"]
    assert _column_ddl_for(sqlite.dialect(), "sessions", "personas_locked", ddl) == ddl


def test_nul_characters_are_removed_from_every_parameter_shape():
    assert clean_parameters("앞\x00뒤") == "앞뒤"
    assert clean_parameters(("a\x00", 1, None)) == ("a", 1, None)
    assert clean_parameters([("a\x00",), ("b",)]) == [("a",), ("b",)]
    assert clean_parameters({"k": "a\x00b", "n": 3}) == {"k": "ab", "n": 3}
    text_without_nul = "그대로"
    assert clean_parameters(text_without_nul) is text_without_nul


def test_a_time_without_a_zone_means_utc():
    """이 앱의 규칙(`as_utc`)입니다. asyncpg 는 서버 PC 의 현지 시각으로 읽어 9시간을 밀어 적습니다."""
    naive = datetime(2026, 3, 5, 14, 30)
    assert clean_parameters((naive,)) == (naive.replace(tzinfo=timezone.utc),)
    aware = datetime(2026, 3, 5, 14, 30, tzinfo=timezone(timedelta(hours=9)))
    assert clean_parameters(aware) is aware


def test_a_password_never_reaches_the_log():
    assert "s3cret" not in safe_db_url("postgresql+asyncpg://mado:s3cret@db.internal/mado")
    assert "db.internal" in safe_db_url("postgresql+asyncpg://mado:s3cret@db.internal/mado")
    assert safe_db_url("sqlite+aiosqlite:///./multiagent.db") == "sqlite+aiosqlite:///./multiagent.db"


def test_only_server_databases_get_pool_options():
    assert engine_options("sqlite+aiosqlite:///./multiagent.db") == {}
    options = engine_options("postgresql+asyncpg://u:p@h/mado")
    assert options["pool_pre_ping"] is True and options["pool_size"] >= 1 and options["max_overflow"] >= 0


def test_a_server_database_name_is_not_treated_as_a_protected_file(monkeypatch):
    """주소의 `database` 는 PostgreSQL 에서 파일이 아니라 DB 이름입니다."""
    monkeypatch.setattr(
        mcp_manager, "get_config", lambda: SimpleNamespace(app=SimpleNamespace(db_url="postgresql+asyncpg://u:p@h/madodb"))
    )
    assert not [p for p in mcp_manager.protected_files() if "madodb" in p.name]


def test_each_dialect_gets_its_own_search_backend():
    assert isinstance(search.backend_for("sqlite"), search.Fts5Backend)
    assert isinstance(search.backend_for("postgresql"), search.PostgresBackend)
    assert type(search.backend_for("mysql")) is search.SearchBackend


def test_the_postgres_query_uses_ilike_and_the_exact_index_expression():
    backend = search.backend_for("postgresql")
    stmt = select(MessageModel.id)

    exact = str(backend.narrow(stmt, ["캐시", "무효화"]).compile(dialect=PG))
    assert exact.count("ILIKE") == 2

    words = str(backend.narrow_by_words(stmt, ["보안"]).compile(dialect=PG))
    expression = f"to_tsvector('simple', left(messages.content, {search.WORD_INDEX_CHARS}))"
    assert expression in words and "to_tsquery('simple'," in words
    # 식이 글자까지 같아야 색인을 탑니다.
    assert expression.replace("messages.content", "content") in search._PG_WORD_INDEX


def test_word_prefix_keeps_tsquery_operators_as_text():
    assert search._word_prefix("보안") == "'보안':*"
    assert search._word_prefix("a'b\\c") == "'a''b\\\\c':*"


# =============================================================== 2. 실제 서버


@pytest_asyncio.fixture
async def factory(monkeypatch):
    async with postgres_database(monkeypatch) as pg_factory:
        yield pg_factory


async def _new_session(db, **columns) -> str:
    sid = f"s-{uuid.uuid4().hex[:8]}"
    db.add(SessionModel(id=sid, title=columns.pop("title", "대화"), **columns))
    await db.flush()
    return sid


def _message(sid: str, content: str, **columns) -> MessageModel:
    columns.setdefault("sender_key", "architect")
    columns.setdefault("sender_name", "Arch")
    return MessageModel(session_id=sid, content=content, **columns)


async def _explain(db, stmt) -> str:
    conn = await db.connection()
    sql = str(stmt.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True}))
    await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
    return "\n".join(row[0] for row in await conn.exec_driver_sql("EXPLAIN " + sql))


@pytest.mark.asyncio
async def test_a_full_turn_runs_and_can_be_searched_and_deleted(factory):
    tool = {"tool_name": "filesystem__write_file", "arguments": {"path": "a.py"}, "output": "ok", "status": "success"}
    async with factory() as db:
        sid = await _new_session(db, strategy="sequential_debate", max_rounds=1, active_agents=["orchestrator", "architect", "coder"])
        await db.commit()

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=FakeLLMCaller(tool_calls={"coder": [tool]})).run_turn(
        session_id=sid, user_prompt="분산 캐시를 설계",
    )
    assert state.status == "completed"

    async with factory() as db:
        assert [h.session_id for h in await search_sessions(db, "분산 캐시")] == [sid]
        records = (await db.execute(select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid))).scalars().all()
        assert len(records) == 1 and records[0].message_id is not None, "도구 기록이 발언에 이어집니다"
        await db.execute(delete(SessionModel).where(SessionModel.id == sid))
        await db.commit()
        assert (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).first() is None


@pytest.mark.asyncio
async def test_the_indexes_exist_and_are_usable(factory):
    async with factory() as db:
        names = set((await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'messages'"))).scalars())
        assert {"ix_messages_content_trgm", "ix_messages_content_words", "ix_messages_created_at"} <= names

    backend = search.backend_for("postgresql")
    stmt = select(MessageModel.id)
    async with factory() as db:
        assert "ix_messages_content_trgm" in await _explain(db, backend.narrow(stmt, ["무효화"]))
    async with factory() as db:
        assert "ix_messages_content_words" in await _explain(db, backend.narrow_by_words(stmt, ["보안"]))


@pytest.mark.asyncio
async def test_a_slow_scan_falls_back_to_word_starts_and_says_so(factory, monkeypatch):
    async with factory() as db:
        sid = await _new_session(db)
        db.add(_message(sid, "보안 점검을 마쳤습니다"))
        db.add(_message(sid, "정보보안 정책은 없습니다"))
        await db.flush()
        for _ in range(4000):
            db.add(_message(sid, uuid.uuid4().hex * 40))
        await db.commit()

    async with factory() as db:
        exact = await search_sessions(db, "보안")
        assert exact.partial is False and exact[0].matches == 2
        await _settings_are_back(db)

    # 정확히 찾는 두 길이 모두 시간을 넘깁니다.
    monkeypatch.setattr(search, "INDEX_SEARCH_TIMEOUT_MS", 1)
    monkeypatch.setattr(search, "SCAN_SEARCH_TIMEOUT_MS", 1)
    real_limit = search.PostgresBackend._within_time_limit

    async def words_get_time(db, stmt, limit_ms, *, recent_first=False):
        spare = 5000 if "to_tsvector" in str(stmt) else limit_ms
        return await real_limit(db, stmt, spare, recent_first=recent_first)

    monkeypatch.setattr(search.PostgresBackend, "_within_time_limit", staticmethod(words_get_time))
    async with factory() as db:
        partial = await search_sessions(db, "보안")
        assert partial.partial is True
        assert partial[0].matches == 1 and partial[0].snippet.startswith("보안 점검"), "낱말 첫머리만 맞습니다"
        await _settings_are_back(db)
        # 비상 경로 뒤에도 트랜잭션은 멀쩡합니다.
        assert await db.scalar(select(SessionModel.title).where(SessionModel.id == sid)) == "대화"


async def _settings_are_back(db) -> None:
    """시간 상한과 플래너 설정은 그 질의에만 걸립니다."""
    for name, value in (("statement_timeout", "0"), ("enable_seqscan", "on"), ("enable_bitmapscan", "on")):
        assert (await db.execute(text(f"SHOW {name}"))).scalar() == value, name


@pytest.mark.asyncio
async def test_a_common_word_is_found_exactly_by_walking_recent_speeches(factory, monkeypatch):
    """색인 길이 시간을 넘겨도 최근 발언부터 훑어 정확한 결과를 돌려줍니다. 흔한 낱말이 이 길로 옵니다."""
    async with factory() as db:
        sid = await _new_session(db)
        for i in range(800):
            db.add(_message(sid, f"분산캐시 무효화 {uuid.uuid4().hex * 20}", created_at=T0 + timedelta(seconds=i)))
        await db.commit()

    real_limit = search.PostgresBackend._within_time_limit

    async def the_index_path_times_out(db, stmt, limit_ms, *, recent_first=False):
        return await real_limit(db, stmt, limit_ms, recent_first=True) if recent_first else None

    monkeypatch.setattr(search.PostgresBackend, "_within_time_limit", staticmethod(the_index_path_times_out))
    async with factory() as db:
        hits = await search_sessions(db, "산캐시 무효화")
        assert hits.partial is False
        assert hits[0].matches == 600, "상한까지 최근 발언부터 찾습니다"
        assert "산캐시" in hits[0].snippet, "낱말 가운데도 맞습니다"
        await _settings_are_back(db)


@pytest.mark.asyncio
async def test_walking_recent_speeches_turns_off_the_plans_that_collect_every_match(factory):
    """결과는 같고 속도만 다른 설정이라 결과로는 확인할 수 없습니다. 그 질의 안에서 설정을 직접 읽습니다."""
    read = select(func.current_setting("enable_bitmapscan"), func.current_setting("enable_seqscan"))
    within = search.PostgresBackend._within_time_limit
    async with factory() as db:
        assert (await within(db, read, 1000))[0] == ("on", "on")
        assert (await within(db, read, 1000, recent_first=True))[0] == ("off", "off")
        await _settings_are_back(db)


@pytest.mark.asyncio
async def test_walking_recent_speeches_uses_the_recency_index(factory):
    backend = search.backend_for("postgresql")
    stmt = select(MessageModel.id).order_by(MessageModel.created_at.desc()).limit(600)
    async with factory() as db:
        await db.execute(text("SELECT set_config('enable_bitmapscan', 'off', true)"))
        assert "ix_messages_created_at" in await _explain(db, backend.narrow(stmt, ["보안"]))


@pytest.mark.asyncio
async def test_a_tsquery_operator_in_the_query_is_only_text(factory):
    backend = search.backend_for("postgresql")
    async with factory() as db:
        for term in ("a&b", "x|y", "!z", "(q", "'", "\\", ":*"):
            await db.execute(backend.narrow_by_words(select(MessageModel.id), [term]))


@pytest.mark.asyncio
async def test_without_pg_trgm_the_server_still_starts_and_searches(monkeypatch):
    monkeypatch.setattr(search, "_PG_TRGM_EXTENSION", "CREATE EXTENSION no_such_extension_for_mado_test")
    async with postgres_database(monkeypatch) as factory:
        async with factory() as db:
            names = set((await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'messages'"))).scalars())
            assert "ix_messages_content_trgm" not in names and "ix_messages_content_words" in names
            sid = await _new_session(db)
            db.add(_message(sid, "캐시 무효화 정책"))
            await db.commit()
            assert [h.session_id for h in await search_sessions(db, "무효화")] == [sid]


@pytest.mark.asyncio
async def test_deleting_a_session_takes_everything_with_it(factory):
    """주인 화면의 삭제는 `DELETE FROM sessions` 한 줄입니다. 체험·쉬운 화면의 표가 막으면 안 됩니다."""
    async with factory() as db:
        user = TrialUserModel(name="방문자", name_key=f"k-{uuid.uuid4().hex[:6]}")
        db.add(user)
        sid = await _new_session(db)
        await db.flush()
        message = _message(sid, "발언")
        db.add(message)
        turn = TurnModel(session_id=sid)
        db.add(turn)
        await db.flush()
        db.add_all([
            ToolCallRecordModel(session_id=sid, message_id=message.id, agent_key="a", tool_name="t"),
            ArtifactModel(session_id=sid),
            SessionAgentModel(session_id=sid, agent_key="a"),
            SpeechDraftModel(id=str(uuid.uuid4()), session_id=sid, turn_id=turn.id, agent_key="a"),
            TrialSessionModel(session_id=sid, user_id=user.id),
            TrialFeedbackModel(session_id=sid, user_id=user.id),
            EasySessionModel(session_id=sid),
        ])
        await db.commit()

        await db.execute(delete(SessionModel).where(SessionModel.id == sid))
        await db.commit()

        for model in (MessageModel, ToolCallRecordModel, ArtifactModel, SessionAgentModel, SpeechDraftModel,
                      TrialSessionModel, TrialFeedbackModel, EasySessionModel, TurnModel):
            assert (await db.execute(select(model))).first() is None, model.__name__


@pytest.mark.asyncio
async def test_a_visitor_deleting_their_own_session(factory):
    async with factory() as db:
        user = TrialUserModel(name="방문자", name_key=f"k-{uuid.uuid4().hex[:6]}")
        db.add(user)
        sid = await _new_session(db)
        await db.flush()
        db.add_all([TrialSessionModel(session_id=sid, user_id=user.id), TrialFeedbackModel(session_id=sid, user_id=user.id)])
        await db.commit()

        assert await trial_store.delete_trial_session(db, user.id, sid) is True

        assert await db.get(SessionModel, sid) is None
        assert (await db.execute(select(TrialFeedbackModel))).first() is None


@pytest.mark.asyncio
async def test_deleting_a_speech_keeps_the_tool_record_it_ran(factory):
    async with factory() as db:
        sid = await _new_session(db)
        message = _message(sid, "발언")
        db.add(message)
        await db.flush()
        record = ToolCallRecordModel(session_id=sid, message_id=message.id, agent_key="a", tool_name="t")
        db.add(record)
        await db.commit()
        record_id, message_id = record.id, message.id

    async with factory() as db:
        await db.execute(delete(MessageModel).where(MessageModel.id == message_id))
        await db.commit()
        kept = await db.get(ToolCallRecordModel, record_id)
        assert kept is not None and kept.message_id is None


@pytest.mark.asyncio
async def test_long_values_are_stored_whole(factory):
    long_role = "역할 설명 " * 100
    async with factory() as db:
        sid = await _new_session(db, title="제목" * 400, graph_id="g" * 200, tool_mode="m" * 50)
        db.add(SessionAgentModel(session_id=sid, agent_key="k" * 200, name="이름" * 100, role=long_role, card_color="c" * 80))
        db.add(_message(sid, "본문", sender_key="k" * 200, sender_name="이름" * 100, sender_role=long_role, graph_port="p" * 20))
        db.add(ToolCallRecordModel(session_id=sid, agent_key="k" * 200, tool_name="서버__도구" * 40, status="s" * 40))
        db.add(ArtifactModel(session_id=sid, title="제목" * 400, language="l" * 80, artifact_type="t" * 40))
        await db.commit()

    async with factory() as db:
        assert (await db.get(SessionModel, sid)).title == "제목" * 400
        assert (await db.scalar(select(MessageModel.sender_role).where(MessageModel.session_id == sid))) == long_role


@pytest.mark.asyncio
async def test_nul_characters_do_not_lose_a_speech(factory):
    async with factory() as db:
        sid = await _new_session(db, title="제\x00목")
        db.add(_message(sid, "바이너리\x00조각"))
        db.add(_message(sid, "또\x00하나"))  # 여러 행을 한 번에 넣는 경로
        db.add(ToolCallRecordModel(session_id=sid, agent_key="a", tool_name="t", output="출력\x00", arguments={"x": "인자\x00"}))
        await db.commit()

    async with factory() as db:
        assert (await db.get(SessionModel, sid)).title == "제목"
        contents = set((await db.execute(select(MessageModel.content).where(MessageModel.session_id == sid))).scalars())
        assert contents == {"바이너리조각", "또하나"}
        record = (await db.execute(select(ToolCallRecordModel))).scalars().one()
        assert record.output == "출력"
        assert record.arguments == {"x": "인자\x00"}, "JSON 은 이스케이프되어 그대로 왕복합니다"


@pytest.mark.asyncio
async def test_times_come_back_aware_and_exact(factory):
    async with factory() as db:
        sid = await _new_session(db)
        db.add(_message(sid, "발언", created_at=T0, started_at=T0 - timedelta(seconds=5)))
        await db.commit()

    async with factory() as db:
        row = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().one()
        assert row.created_at.tzinfo is not None and as_utc(row.created_at) == T0
        assert row.created_at - row.started_at == timedelta(seconds=5)


@pytest.mark.asyncio
async def test_a_time_without_a_zone_is_stored_as_utc(factory):
    async with factory() as db:
        sid = await _new_session(db)
        db.add(_message(sid, "발언", created_at=datetime(2026, 3, 5, 14, 30)))
        await db.commit()

    async with factory() as db:
        stored = await db.scalar(select(MessageModel.created_at).where(MessageModel.session_id == sid))
        assert stored == datetime(2026, 3, 5, 14, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_the_audit_screen_reads_json_arguments(factory):
    """PostgreSQL 의 json 타입에는 같음 비교가 없습니다. `PARTITION BY arguments` 면 화면이 오류로 멈춥니다."""
    denied = dict(agent_key="a", tool_name="filesystem__write_file", status="denied", decision="deny", created_at=T0)
    async with factory() as db:
        sid = await _new_session(db)
        fork = await _new_session(db)
        db.add(ToolCallRecordModel(session_id=sid, arguments={"path": "a.py"}, **denied))
        db.add(ToolCallRecordModel(session_id=fork, arguments={"path": "a.py"}, **denied))
        db.add(ToolCallRecordModel(session_id=sid, arguments={"path": "b.py"}, **denied))
        await db.commit()

    calls, total = await blocked_tool_calls(factory)

    assert total == 2, "갈라 낸 대화의 복사본은 한 번만 셉니다"
    assert sorted(c.arguments_text for c in calls) == ['{"path": "a.py"}', '{"path": "b.py"}']


@pytest.mark.asyncio
async def test_an_older_database_gets_its_missing_columns(factory):
    """옛 스키마에서 컬럼을 빼 두고 다시 기동합니다. `_ADDED_COLUMNS` 의 SQLite 문법이 그대로 나가면 실패합니다."""
    dropped = {
        "messages": ["started_at", "turn_meta", "graph_port"],
        "sessions": ["converge_early", "personas_locked", "known_agents", "parallel_limit", "graph_id"],
        "turns": ["stopped_early", "usage"],
        "session_agents": ["config_snapshot", "card_color"],
        "tool_calls": ["decision", "turn_id"],
    }
    async with factory() as db:
        sid = await _new_session(db)
        await db.commit()
    engine = db_session.get_engine(POSTGRES_URL)
    async with engine.begin() as conn:
        for table, columns in dropped.items():
            for column in columns:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    async with engine.begin() as conn:
        await prepare_schema(conn)

    async with factory() as db:
        kinds = {
            (row[0], row[1]): (row[2], row[3])
            for row in await db.execute(text(
                "SELECT table_name, column_name, data_type, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            ))
        }
        assert kinds[("messages", "started_at")][0] == "timestamp with time zone"
        assert kinds[("sessions", "known_agents")][0] == "json"
        assert kinds[("sessions", "converge_early")] == ("boolean", "true")
        # 있던 행은 기본값을 받고, ORM 은 그대로 읽고 씁니다.
        session = await db.get(SessionModel, sid)
        assert session.converge_early is True and session.personas_locked is False and session.known_agents == []
        session.known_agents = ["architect"]
        db.add(_message(sid, "새 발언", started_at=T0, turn_meta={"kind": "speech"}))
        await db.commit()
        assert (await db.get(SessionModel, sid)).known_agents == ["architect"]


@pytest.mark.asyncio
async def test_processes_starting_together_do_not_collide(factory):
    await empty_schema()

    async def start_one() -> None:
        engine = create_async_engine(POSTGRES_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await prepare_schema(conn)
        finally:
            await engine.dispose()

    await asyncio.gather(*(start_one() for _ in range(6)))

    async with factory() as db:
        sid = await _new_session(db)
        db.add(_message(sid, "캐시 무효화 정책"))
        await db.commit()
        assert [h.session_id for h in await search_sessions(db, "무효화")] == [sid]


@pytest.mark.asyncio
async def test_a_second_process_can_see_the_first_is_alive(factory):
    """다른 프로세스가 돌리는 턴을 죽은 턴으로 적지 않으려면, 그 프로세스가 있는지 알아야 합니다."""
    async with factory() as db:
        assert await no_other_instance(db) is True

    async with instance_presence():
        async with factory() as db:
            assert await no_other_instance(db) is False

    async with factory() as db:
        assert await no_other_instance(db) is True


@pytest.mark.asyncio
async def test_sqlite_has_no_instance_lock():
    url = "sqlite+aiosqlite:///:memory:"
    await db_session.init_db(url)
    async with db_session.get_session_factory(url)() as db:
        assert await no_other_instance(db) is True
    async with instance_presence():
        pass
