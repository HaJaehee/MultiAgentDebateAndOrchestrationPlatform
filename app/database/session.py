import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from app.database.models import Base

logger = logging.getLogger(__name__)

# create_all 은 기존 테이블에 컬럼을 추가하지 않습니다. 이미 만들어진 DB 를 쓰는
# 배포본을 위해, 나중에 도입된 컬럼만 최소한으로 채워 넣습니다.
_ADDED_COLUMNS = {
    "messages": {
        # 발언의 실제 시작·종료 시각. NULL 이면 "이 컬럼이 생기기 전의 발언" 이라
        # 기본값을 넣지 않습니다 — 넣으면 옛 발언이 마이그레이션한 순간에 한꺼번에
        # 시작하고 끝난 것처럼 보입니다.
        "started_at": "DATETIME",
        "finished_at": "DATETIME",
        # 턴을 마무리한 합성 발언에만 채워집니다 (`MessageModel.turn_started_at`).
        "turn_started_at": "DATETIME",
    },
    "sessions": {
        "personas_locked": "BOOLEAN NOT NULL DEFAULT 0",
        "workspace_dir": "TEXT NOT NULL DEFAULT ''",
        # 비어 있으면 "그때 무엇이 있었는지 모른다" 는 뜻입니다. 화면은 그 경우
        # 지금 있는 에이전트를 모두 새것으로 보고 켜 둡니다.
        "known_agents": "TEXT NOT NULL DEFAULT '[]'",
        # 병렬 지시 전략의 동시 실행 상한. 다른 전략에서는 읽히지 않습니다.
        "parallel_limit": "INTEGER NOT NULL DEFAULT 3",
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


def get_engine(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(db_url, echo=False, future=True)
        if _engine.dialect.name == "sqlite":
            configure_sqlite(_engine, db_url)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_session_factory(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        get_engine(db_url)
    assert _sessionmaker is not None
    return _sessionmaker


async def _add_missing_columns(conn) -> None:
    """기존 DB 에 없는 컬럼을 추가합니다 (SQLite 기준, 멱등)."""
    for table, columns in _ADDED_COLUMNS.items():
        exists = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
        )
        if exists.first() is None:
            continue  # create_all 이 방금 만든 최신 스키마
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        present = {row[1] for row in result}
        for column, ddl in columns.items():
            if column not in present:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                logger.info(f"Migrated schema: added {table}.{column}")


async def init_db(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> None:
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            await _add_missing_columns(conn)
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
