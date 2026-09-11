import logging
from typing import AsyncGenerator
from sqlalchemy import text
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


def get_engine(db_url: str = "sqlite+aiosqlite:///./multiagent.db") -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(db_url, echo=False, future=True)
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
