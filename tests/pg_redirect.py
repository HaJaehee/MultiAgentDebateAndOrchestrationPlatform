"""전체 시험을 실제 PostgreSQL 로 돌리는 pytest 플러그인 (ADR-041).

    MADO_TEST_POSTGRES_URL=postgresql+asyncpg://user@host/mado_test pytest -p tests.pg_redirect tests

대부분의 시험은 메모리 SQLite 를 씁니다. 그 엔진 생성을 PostgreSQL 로 돌리고 시험마다 모든 표를 비웁니다 (메모리 DB 는 엔진마다
새것이었으므로). SQLite 는 외래 키도 길이도 NUL 도 검사하지 않아, 이렇게 돌려야 드러나는 결함이 있습니다. 파일 SQLite 를 직접 쓰는
시험(옛 DB 이관, WAL 설정 등)은 그대로 SQLite 로 돕니다. 시험이 표를 비우므로 DB 이름에 `test` 가 들어 있어야 합니다.

연결을 재사용하지 않습니다. 시험마다 이벤트 루프가 새것이고 asyncpg 연결은 루프에 묶여 있기 때문입니다.
"""

import asyncio
import os

import asyncpg
import pytest
import sqlalchemy.ext.asyncio as sa_async
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

POSTGRES_URL = os.environ["MADO_TEST_POSTGRES_URL"]
if "test" not in (make_url(POSTGRES_URL).database or ""):
    raise pytest.UsageError("MADO_TEST_POSTGRES_URL 의 DB 이름에 test 가 없습니다 (시험이 표를 비웁니다)")

MEMORY_SQLITE = "sqlite+aiosqlite:///:memory:"

# 메모리 SQLite 자체를 확인하는 시험.
SQLITE_ONLY = {"test_a_memory_database_keeps_working", "test_this_sqlite_has_the_trigram_index"}

_create_async_engine = sa_async.create_async_engine


def _redirected(url, *args, **kwargs):
    if str(url).startswith(MEMORY_SQLITE):
        return _create_async_engine(POSTGRES_URL, *args, **{**kwargs, "poolclass": NullPool})
    return _create_async_engine(url, *args, **kwargs)


# conftest 와 시험 모듈이 이 함수를 가져가기 전에 바꿔 둡니다 (`-p` 는 그보다 먼저 올라옵니다).
sa_async.create_async_engine = _redirected


async def _truncate_all_tables() -> None:
    conn = await asyncpg.connect(POSTGRES_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if rows:
            await conn.execute("TRUNCATE " + ", ".join(f'"{row[0]}"' for row in rows) + " CASCADE")
    finally:
        await conn.close()


def pytest_collection_modifyitems(items) -> None:
    skip = pytest.mark.skip(reason="메모리 SQLite 자체를 확인하는 시험입니다")
    for item in items:
        if item.name in SQLITE_ONLY:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _empty_postgres_tables():
    asyncio.run(_truncate_all_tables())
    yield
