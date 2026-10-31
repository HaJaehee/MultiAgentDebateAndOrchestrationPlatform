"""실제 PostgreSQL 로 도는 시험의 준비물.

`MADO_TEST_POSTGRES_URL` (`postgresql+asyncpg://…/mado_test`) 이 없으면 그 시험은 건너뜁니다. 시험마다 `public` 스키마를
지우고 시작하므로, 실수로 운영 DB 를 가리키지 않도록 DB 이름에 `test` 가 들어 있어야 합니다.
"""

import contextlib
import os
from typing import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.database import session as db_session

POSTGRES_URL = os.environ.get("MADO_TEST_POSTGRES_URL", "")


async def empty_schema() -> None:
    cleaner = create_async_engine(POSTGRES_URL, poolclass=NullPool)
    try:
        async with cleaner.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await cleaner.dispose()


@contextlib.asynccontextmanager
async def postgres_database(monkeypatch) -> AsyncIterator:
    """앱의 DB 싱글턴을 빈 PostgreSQL 로 잠시 바꿉니다. 세션 팩토리를 돌려줍니다."""
    if not POSTGRES_URL:
        pytest.skip("MADO_TEST_POSTGRES_URL 이 없습니다")
    if "test" not in (make_url(POSTGRES_URL).database or ""):
        pytest.fail("MADO_TEST_POSTGRES_URL 의 DB 이름에 test 가 없습니다 (시험이 스키마를 지웁니다)")
    await empty_schema()
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_sessionmaker", None)
    await db_session.init_db(POSTGRES_URL)
    try:
        yield db_session.get_session_factory(POSTGRES_URL)
    finally:
        await db_session.get_engine(POSTGRES_URL).dispose()
