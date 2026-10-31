import contextlib
import ctypes
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Optional
from sqlalchemy import Boolean, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from app.config import DEFAULT_SUB_DEBATE_ROUNDS, get_config
from app.database.models import Base
from app.database.search import ensure_search_index

logger = logging.getLogger(__name__)

# create_all 은 기존 테이블에 컬럼을 추가하지 않습니다. 이미 만들어진 DB 를 쓰는
# 배포본을 위해, 나중에 도입된 컬럼만 최소한으로 채워 넣습니다.
#
# 값은 SQLite 문법입니다. 다른 DB 에서는 타입을 모델에서 읽고 나머지만 옮깁니다 (`_column_ddl_for`).
_ADDED_COLUMNS = {
    "messages": {
        # 발언의 실제 시작·종료 시각. NULL 이면 "이 컬럼이 생기기 전의 발언" 이라
        # 기본값을 넣지 않습니다 — 넣으면 옛 발언이 마이그레이션한 순간에 한꺼번에
        # 시작하고 끝난 것처럼 보입니다.
        "started_at": "DATETIME",
        "finished_at": "DATETIME",
        # 턴을 마무리한 합성 발언에만 채워집니다 (`MessageModel.turn_started_at`).
        "turn_started_at": "DATETIME",
        # 그래프 토론의 노드 (v0.9.0). 그 밖의 발언은 NULL.
        "graph_node_id": "VARCHAR(64)",
        "graph_port": "VARCHAR(8)",
        # 턴 기록 (ADR-024). 이 컬럼 이전의 기록은 NULL.
        "turn_id": "VARCHAR(36)",
        "turn_meta": "JSON",
        # 폴백 모델 응답 정보 및 발언별 토큰/호출 시간 메타데이터 (둘 다 없으면 NULL).
        "llm_meta": "JSON",
        # 발언 하나만 다시 실행했을 때의 지난 버전들 (ADR-036). 다시 실행한 적 없으면 NULL.
        "revisions": "JSON",
    },
    "sessions": {
        "personas_locked": "BOOLEAN NOT NULL DEFAULT 0",
        "workspace_dir": "TEXT NOT NULL DEFAULT ''",
        # 비어 있으면 "그때 무엇이 있었는지 모른다" 는 뜻입니다. 화면은 그 경우
        # 지금 있는 에이전트를 모두 새것으로 보고 켜 둡니다.
        "known_agents": "TEXT NOT NULL DEFAULT '[]'",
        # 병렬 지시 전략의 동시 실행 상한. 다른 전략에서는 읽히지 않습니다.
        "parallel_limit": "INTEGER NOT NULL DEFAULT 3",
        # 합의 수렴 시 조기 종료 및 턴 토큰 예산 한도 (v1.5).
        "converge_early": "BOOLEAN NOT NULL DEFAULT 1",
        "token_budget": "INTEGER NOT NULL DEFAULT 0",
        # 에이전트의 사용자 질문 허용 여부 (v1.5, 이전 대화는 비활성화).
        "ask_user": "BOOLEAN NOT NULL DEFAULT 0",
        # 서브 토론 허용 여부와 한 번의 최대 라운드 수 (ADR-034).
        "sub_debates": "BOOLEAN NOT NULL DEFAULT 0",
        "sub_debate_rounds": f"INTEGER NOT NULL DEFAULT {DEFAULT_SUB_DEBATE_ROUNDS}",
        # 세션 분기(fork) 생성 시 원본 세션 및 턴 출처 정보 (v1.5).
        "forked_from": "JSON",
        # 대화 기억 (v0.8.3). 비어 있으면 장부·요약이 아직 없는 대화입니다.
        "decision_ledger": "TEXT NOT NULL DEFAULT ''",
        "ledger_through_id": "VARCHAR(36)",
        "transcript_summary": "TEXT NOT NULL DEFAULT ''",
        "summary_through_id": "VARCHAR(36)",
        # 그래프 토론 (v0.9.0).
        "graph_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "graph_snapshot": "JSON",
        # 도구 보안. 빈 모드는 conf.json 의 기본값을 따릅니다.
        "tool_mode": "VARCHAR(20) NOT NULL DEFAULT ''",
        "tool_grants": "TEXT NOT NULL DEFAULT '[]'",
        "tool_denials": "TEXT NOT NULL DEFAULT '[]'",
    },
    "tool_calls": {
        # 도구 보안 판정. 빈 값은 "판정 없이 실행된 호출" 입니다 (이 컬럼 이전 기록 포함).
        "decision": "VARCHAR(20) NOT NULL DEFAULT ''",
        "risk": "VARCHAR(20) NOT NULL DEFAULT ''",
        "rule": "TEXT NOT NULL DEFAULT ''",
        "approver": "VARCHAR(20) NOT NULL DEFAULT ''",
        # 턴 기록 (ADR-024). 발언이 끝나기 전에 기록된 호출도 어느 턴의 것인지 압니다.
        "turn_id": "VARCHAR(36)",
    },
    "turns": {
        # 합성에 들어갈 때 정지로 덜 논의된 턴이었는지 (ADR-024 2단계).
        "stopped_early": "BOOLEAN NOT NULL DEFAULT 0",
        # 턴별 누적 토큰 및 호출 소요 시간 총합 (이전 턴은 NULL).
        "usage": "JSON",
        # 작업 공간 체크포인트 정보 (이전 턴은 NULL).
        "checkpoint": "JSON",
        # 턴 완료 시점의 대화 기억 스냅샷 및 사용자 평가 (v1.5).
        "memory": "JSON",
        "rating": "JSON",
    },
    "session_agents": {
        # NULL 이면 "이 컬럼이 생기기 전에 잠긴 대화" 입니다. 그런 대화는 예전처럼
        # 살아 있는 conf.json 을 그대로 씁니다. 빈 JSON 을 기본값으로 넣으면 그
        # 구분이 사라지므로 nullable 로 둡니다.
        "config_snapshot": "TEXT",
        # 카드 색과 아이콘. 빈 문자열이면 "정하지 않음" 이고, 그때는 에이전트
        # 키에서 자동으로 정해집니다 (`style_for_agent`).
        "card_color": "VARCHAR(40) NOT NULL DEFAULT ''",
        "icon_path": "TEXT NOT NULL DEFAULT ''",
    },
}

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# ---------------------------------------------------------------------------
# SQLite 동시성
# ---------------------------------------------------------------------------
# 예전에는 아무 설정 없이 파이썬 SQLite 의 기본값을 썼습니다. 그러면:
#
# * 누군가 파일을 잡고 있으면 **5초만** 기다리고 `database is locked` 를 냅니다.
# * 기록할 때마다 `multiagent.db-journal` 을 새로 만들었다 지웁니다. 윈도우에서는
#   그 파일을 다른 프로그램이 잠깐 열기만 해도 기록이 실패합니다.
#
# 실제로 자리를 비운 사이(화면 잠금) 최종 합성 보고서가 `database is locked` 로 기록되지
# 않았습니다. 윈도우는 사용자가 없을 때 백신 예약 검사·검색 색인·백업·동기화를 돌리는데,
# 그런 프로그램이 파일을 몇 초만 잡아도 5초 대기는 그대로 넘어갑니다.

# 잠금을 기다리는 시간(밀리초). 발언 하나를 기록하는 트랜잭션은 수 밀리초라, 30초를
# 기다려야 할 만큼 잡고 있는 쪽은 이 앱이 아니라 바깥 프로그램입니다. 그 정도는 버팁니다.
SQLITE_BUSY_TIMEOUT_MS = 30_000

# GetDriveTypeW 가 네트워크 드라이브에 돌려주는 값.
_DRIVE_REMOTE = 4


def sqlite_file_path(db_url: str) -> Optional[Path]:
    """SQLite 파일 DB 의 경로. 메모리 DB 이거나 SQLite 가 아니면 None."""
    url = make_url(db_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database or ""
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database)


def is_network_location(path: Path) -> bool:
    """네트워크 공유 위에 있는 경로인가 (UNC 경로, 또는 윈도우의 네트워크 드라이브).

    WAL 은 같은 컴퓨터의 프로세스끼리 공유 메모리(`-shm`)로 조율합니다. 네트워크 파일
    시스템에서는 그 조율이 보장되지 않아 **DB 가 깨질 수 있습니다.** 그래서 이런 곳에서는
    WAL 을 켜지 않습니다. 판단할 수 없으면 로컬로 봅니다.
    """
    text_path = str(path if path.is_absolute() else Path.cwd() / path)
    if text_path.startswith("\\\\") or text_path.startswith("//"):
        return True
    if sys.platform == "win32":
        drive = os.path.splitdrive(text_path)[0]
        if drive and not drive.startswith("\\\\"):
            try:
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_REMOTE
            except Exception:  # noqa: BLE001 - 판단 못 하면 로컬로 봅니다
                return False
    return False


def configure_sqlite(engine: AsyncEngine, db_url: str) -> bool:
    """연결마다 대기 시간과 저널 모드를 맞춥니다. WAL 을 켰으면 True.

    * `busy_timeout` — 잠겨 있으면 30초까지 기다립니다 (기본 5초).
    * `journal_mode=WAL` — 쓰기마다 저널 파일을 만들고 지우지 않습니다. 읽기와 쓰기가
      서로를 막지도 않습니다. 파일 DB 이고 로컬 디스크일 때만 켭니다.
    * `synchronous=NORMAL` — WAL 에서 권장되는 값입니다. 전원이 나가면 마지막 몇
      트랜잭션을 잃을 수 있지만 DB 가 깨지지는 않습니다.

    WAL 은 DB 옆에 `-wal`·`-shm` 파일을 둡니다. 패키징 스크립트는 이미 둘을 제외합니다.
    """
    path = sqlite_file_path(db_url)
    use_wal = path is not None and not is_network_location(path)
    if path is not None and not use_wal:
        logger.warning(
            f"SQLite database {path} is on a network location; leaving journal_mode as is "
            f"(WAL is unsafe on network file systems). busy_timeout is still applied."
        )

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            if use_wal:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    return use_wal


# ---------------------------------------------------------------------------
# PostgreSQL (ADR-041)
# ---------------------------------------------------------------------------

# 연결을 이만큼(초) 쓰고 새로 맺습니다. 방화벽이나 로드밸런서가 오래 쉰 연결을 말없이 끊는 환경에 대비합니다.
DB_POOL_RECYCLE_SECONDS = 1800

# `pg_advisory_*` 잠금 번호. 값 자체에 뜻은 없고("MADO"), 이 앱의 잠금끼리 겹치지만 않으면 됩니다.
SCHEMA_LOCK_KEY = 0x4D41444F
INSTANCE_LOCK_KEY = SCHEMA_LOCK_KEY + 1


def safe_db_url(db_url: str) -> str:
    """로그에 적을 수 있는 DB 주소 (비밀번호를 가립니다)."""
    return make_url(db_url).render_as_string(hide_password=True)


def engine_options(db_url: str) -> Dict[str, Any]:
    """서버 DB 의 연결 풀 설정. SQLite 는 파일을 여는 것이라 드라이버 기본값을 그대로 씁니다.

    `pool_pre_ping` 은 DB 재시작이나 장애 조치 뒤의 죽은 연결을 쓰기 전에 걸러 냅니다. 그렇지 않으면 그 시점의 발언 기록이
    다시 시도 끝에 실패합니다.
    """
    if make_url(db_url).get_backend_name() == "sqlite":
        return {}
    app = get_config().app
    return {
        "pool_pre_ping": True,
        "pool_recycle": DB_POOL_RECYCLE_SECONDS,
        "pool_size": app.db_pool_size,
        "max_overflow": app.db_max_overflow,
    }


def clean_parameters(parameters: Any) -> Any:
    """드라이버로 가는 매개변수를 SQLite 에서 기대하던 뜻으로 맞춥니다 (`configure_postgresql`)."""
    if isinstance(parameters, str):
        return parameters.replace("\x00", "") if "\x00" in parameters else parameters
    if isinstance(parameters, datetime):
        return parameters if parameters.tzinfo is not None else parameters.replace(tzinfo=timezone.utc)
    if isinstance(parameters, dict):
        return {key: clean_parameters(value) for key, value in parameters.items()}
    if isinstance(parameters, (list, tuple)):
        return type(parameters)(clean_parameters(value) for value in parameters)
    return parameters


def configure_postgresql(engine: AsyncEngine) -> None:
    """SQLite 가 말없이 받던 두 가지를 기록 직전에 맞춥니다.

    * **NUL 문자** — PostgreSQL 의 TEXT 는 받지 않습니다. 바이너리를 읽은 도구 출력이나 모델 응답에 섞이면 그 발언 기록이 통째로
      실패합니다. 뜻이 없는 글자이므로 지웁니다. JSON 값은 이미 이스케이프되어 있어 해당하지 않습니다.
    * **시간대 없는 시각** — 이 앱에서 시간대 없는 시각은 UTC 입니다 (`as_utc`). asyncpg 는 이를 서버 PC 의 현지 시각으로 읽으므로,
      그대로 두면 한국에서 9시간 밀려 기록됩니다.
    """

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def _clean_parameters(_conn, _cursor, statement, parameters, _context, _executemany):
        return statement, clean_parameters(parameters)


async def no_other_instance(db: AsyncSession) -> bool:
    """이 DB 를 쓰는 다른 MADO 프로세스가 없는가. 이 트랜잭션이 끝날 때까지 새 프로세스의 등록을 막습니다.

    서버가 뜰 때 "도는 중" 으로 남은 턴은 죽은 턴으로 정리합니다. 프로세스가 둘이면 다른 프로세스가 지금 돌리는 턴까지 끊김으로
    적게 되므로, 그 정리는 혼자일 때만 합니다. SQLite 는 한 기계의 파일이라 여기서 보지 않습니다.
    """
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": INSTANCE_LOCK_KEY}))


@contextlib.asynccontextmanager
async def instance_presence() -> AsyncIterator[None]:
    """이 프로세스가 살아 있는 동안 공유 잠금을 쥡니다 (`no_other_instance` 가 봅니다).

    잠금은 연결에 묶이므로 전용 연결을 하나 들고 있습니다. PgBouncer 의 transaction 모드에서는 이 잠금이 유지되지 않습니다.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield
        return
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock_shared(:key)"), {"key": INSTANCE_LOCK_KEY})
        await conn.commit()
        try:
            yield
        finally:
            # 연결이 풀로 돌아가도 세션 잠금은 남으므로 직접 풉니다.
            try:
                await conn.execute(text("SELECT pg_advisory_unlock_shared(:key)"), {"key": INSTANCE_LOCK_KEY})
            except SQLAlchemyError as exc:
                logger.debug(f"Could not release the instance lock: {exc}")


def get_engine(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(db_url, echo=False, future=True, **engine_options(db_url))
        if _engine.dialect.name == "sqlite":
            configure_sqlite(_engine, db_url)
        elif _engine.dialect.name == "postgresql":
            configure_postgresql(_engine)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_session_factory(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine(db_url)
    assert _sessionmaker is not None
    return _sessionmaker


# 앱에서 SQL 을 문자열로 잇는 곳은 아래 스키마 보강뿐입니다. 테이블·컬럼 이름과 컬럼 정의는
# 바인딩 매개변수가 될 수 없어 문장에 그대로 들어가므로, 값은 이 파일의 상수(`_ADDED_COLUMNS`)만
# 쓰고, 그래도 모양이 어긋나면 실행하지 않습니다. 나머지는 모두 ORM 이나 바인딩 매개변수를 씁니다.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DDL = re.compile(r"[A-Za-z0-9_ ()'\[\],.]*")


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Not a plain SQL identifier: {name!r}")
    return name


def _column_ddl(ddl: str) -> str:
    """`TEXT NOT NULL DEFAULT '[]'` 같은 컬럼 정의만 받습니다 (`;`·주석·따옴표 짝 어긋남 거절)."""
    if not _DDL.fullmatch(ddl) or "--" in ddl or ddl.count("'") % 2:
        raise ValueError(f"Not a plain column definition: {ddl!r}")
    return ddl


def _column_ddl_for(dialect: Any, table: str, column: str, ddl: str) -> str:
    """`_ADDED_COLUMNS` 의 SQLite 컬럼 정의를 이 DB 의 문법으로 옮깁니다.

    SQLite 의 `DATETIME` 이나 `BOOLEAN ... DEFAULT 0` 은 PostgreSQL 에서 오류입니다. 타입은 모델이 정본이고, 뒤따르는
    `NOT NULL DEFAULT ...` 만 그대로 옮깁니다.
    """
    if dialect.name == "sqlite":
        return _column_ddl(ddl)
    model_type = Base.metadata.tables[table].c[column].type
    _sqlite_type, _, rest = ddl.partition(" ")
    if isinstance(model_type, Boolean):
        rest = rest.replace("DEFAULT 0", "DEFAULT false").replace("DEFAULT 1", "DEFAULT true")
    return _column_ddl(f"{model_type.compile(dialect=dialect)} {rest}".strip())


async def _add_missing_columns(conn) -> None:
    """기존 DB 에 없는 컬럼을 추가합니다 (멱등)."""

    def present_columns(sync_conn) -> Dict[str, set]:
        inspector = inspect(sync_conn)
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in _ADDED_COLUMNS
            if inspector.has_table(table)  # 없으면 create_all 이 최신 스키마로 만듭니다
        }

    present = await conn.run_sync(present_columns)
    quote = conn.dialect.identifier_preparer.quote
    for table, columns in _ADDED_COLUMNS.items():
        for column, ddl in columns.items():
            if table in present and column not in present[table]:
                await conn.execute(text(
                    f"ALTER TABLE {quote(_identifier(table))} ADD COLUMN {quote(_identifier(column))} "
                    f"{_column_ddl_for(conn.dialect, table, column, ddl)}"
                ))
                logger.info(f"Migrated schema: added {table}.{column}")


async def _prepare_postgresql(conn) -> None:
    """스키마를 고치는 기동을 한 번에 하나씩 돌리고, 이 DB 가 한국어를 담을 수 있는지 확인합니다.

    프로세스 둘이 같은 DB 에 함께 뜨면 `CREATE TABLE`·`CREATE EXTENSION` 의 `IF NOT EXISTS` 만으로는 경합을 막지 못합니다
    (둘 다 없다고 보고 만들다 한쪽이 실패합니다). 잠금은 이 트랜잭션이 끝날 때 풀립니다.
    """
    await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
    encoding = (await conn.execute(text("SHOW server_encoding"))).scalar()
    if encoding != "UTF8":
        logger.error(
            f"This database uses the {encoding} encoding. Characters it cannot represent will fail to save; "
            "create the database with ENCODING 'UTF8'."
        )


async def prepare_schema(conn) -> None:
    """테이블과 컬럼, 검색 색인을 이 DB 에 맞춥니다."""
    if conn.dialect.name == "postgresql":
        await _prepare_postgresql(conn)
    await _add_missing_columns(conn)
    await conn.run_sync(Base.metadata.create_all)
    # 세션 검색을 위한 색인 (`app/database/search.py`).
    await ensure_search_index(conn)


async def init_db(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> None:
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await prepare_schema(conn)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
