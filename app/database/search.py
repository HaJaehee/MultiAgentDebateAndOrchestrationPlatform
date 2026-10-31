"""대화 세션 및 발언 전문 검색: 세션 제목과 발언 본문 통합 검색 (v1.5, ADR-041).

세션이 누적되었을 때 특정 의사결정 내역이나 키워드를 신속하게 탐색할 수 있도록 지원합니다.
사이드바에서 세션 제목과 발언 본문을 통합 검색하며, 세션별로 최근 매칭된 발언 스니펫과 매칭 건수를 반환합니다.

## 규칙은 DB 와 무관합니다

모든 낱말이 **부분 문자열로**(대소문자 무시) 들어 있는 발언을 찾습니다(AND). 시스템 오류 메시지(`msg_type="error"`)는
제외합니다. 형태소 분석기 없이 한국어 조사가 붙은 말("캐시를")과 합성어("분산캐시")도 찾으려면 부분 문자열이어야 합니다.
모든 검색어는 SQL 인젝션 방지를 위해 바인딩 파라미터로 안전하게 전달됩니다.

## 찾는 길은 DB 마다 다릅니다 (`SearchBackend`)

* **SQLite (`Fts5Backend`)** — 3글자 이상은 FTS5 `trigram` 색인(`messages_fts`)으로, 2글자 이하는 trigram 이 다루지 못해
  `LIKE` 로 찾습니다. 색인은 `messages` 트리거로 실시간 동기화됩니다. FTS5 를 쓸 수 없으면 모두 `LIKE` 로 찾습니다.
* **PostgreSQL (`PostgresBackend`)** — `ILIKE` 가 규칙 그 자체이고, 찾는 순서는 셋입니다. 측정값은 ADR-041 에 있습니다.
  1. 3글자 이상이 있으면 `pg_trgm` GIN 색인으로 찾습니다. 드문 낱말에 빠릅니다.
  2. 최근 발언부터 훑다 상한에서 멈춥니다(`created_at` 색인). 흔한 낱말과 짧은 낱말에 빠릅니다.
  3. 훑기가 `SCAN_SEARCH_TIMEOUT_MS` 를 넘기면 `tsvector` GIN 색인으로 **낱말 첫머리가 맞는 발언만** 찾아 돌려주고
     `SearchResults.partial` 로 알립니다. 검색 한 번이 DB 를 오래 붙잡아 다른 사람의 요청까지 늦추지 않게 하는 안전장치입니다.
* **그 밖의 DB (`SearchBackend`)** — 색인 없이 훑습니다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import column, func, literal_column, select, table, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.database.models import MessageModel, SessionModel
from app.timestamps import as_utc

logger = logging.getLogger(__name__)

FTS_TABLE = "messages_fts"
# Trigram 인덱스 최소 글자 수 (이보다 짧은 단어는 LIKE 검색으로 처리)
FTS_MIN_CHARS = 3
# 최대 검색어 단어 수 및 최대 반환 세션 수
MAX_TERMS = 6
DEFAULT_LIMIT = 30
SNIPPET_BEFORE, SNIPPET_AFTER = 40, 90
LIKE_ESCAPE = "\\"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# PostgreSQL 에서 trigram 색인으로 찾을 때 주는 시간(밀리초). 드문 낱말은 수십 밀리초에 끝나고, 넘기면 흔한 낱말입니다.
INDEX_SEARCH_TIMEOUT_MS = 1000
# 최근 발언부터 훑을 때, 그리고 낱말 색인으로 찾을 때 주는 시간(밀리초). 훑기를 넘기면 일부만 찾습니다.
SCAN_SEARCH_TIMEOUT_MS = 3000
# `statement_timeout` 이 질의를 멈췄을 때의 SQLSTATE (query_canceled).
_SQLSTATE_QUERY_CANCELED = "57014"
# 낱말 색인에 넣는 본문의 앞부분(글자). `to_tsvector` 는 결과가 1MB 를 넘으면 오류를 내고, 그러면 발언 기록(INSERT)이
# 실패합니다. 발언을 잃는 것과 아주 긴 발언의 뒷부분이 비상 경로에서 빠지는 것은 비교가 되지 않습니다.
WORD_INDEX_CHARS = 100_000
# 질의의 식이 색인의 식과 글자까지 같아야 색인을 탑니다. 그래서 바인딩 매개변수를 쓰지 않습니다.
_WORD_VECTOR = f"to_tsvector('simple', left({{content}}, {WORD_INDEX_CHARS}))"

# `messages` 테이블을 external content로 사용하는 FTS5 가상 테이블.
# 본문 중복 저장을 방지하고 트리거를 통해 CUD 변경 사항을 인덱스에 실시간 반영합니다.
_FTS5_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "content, content='messages', content_rowid='rowid', tokenize='trigram')",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
    END""",
)
_FTS5_REBUILD = "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')"
_FTS5_EXISTS = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"

# 질의에서 색인을 가리키는 가벼운 표 (ORM 모델이 아닙니다 — 만들고 지우는 것은 위 문장이 합니다).
_fts = table(FTS_TABLE, column("rowid"), column("content"))

_PG_TRGM_EXTENSION = "CREATE EXTENSION IF NOT EXISTS pg_trgm"
_PG_TRGM_INDEX = "CREATE INDEX IF NOT EXISTS ix_messages_content_trgm ON messages USING gin (content gin_trgm_ops)"
_PG_WORD_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_messages_content_words ON messages USING gin (("
    + _WORD_VECTOR.format(content="content") + "))"
)
# 흔한 낱말은 최근 발언부터 훑다 상한(`limit * 20`)에서 멈춥니다. 이 색인이 없으면 맞는 행을 전부 모아 정렬합니다.
_PG_RECENCY_INDEX = "CREATE INDEX IF NOT EXISTS ix_messages_created_at ON messages (created_at)"


@dataclass
class SearchHit:
    session_id: str
    title: str
    # 매칭된 가장 최근 발언 정보 (제목만 일치한 경우 None)
    message_id: Optional[str]
    sender_name: str
    snippet: str
    at: Optional[datetime]
    # 해당 세션 내 매칭 발언 건수
    matches: int = 0


class SearchResults(List[SearchHit]):
    """검색 결과. `partial` 이면 전부 훑지 못하고 낱말 첫머리가 맞는 발언만 찾은 것입니다."""

    partial: bool = False


def terms_of(query: str) -> List[str]:
    """입력된 검색 쿼리를 개별 키워드 리스트로 분리합니다 (중복 제거, 최대 MAX_TERMS개)."""
    return list(dict.fromkeys(w for w in (query or "").split() if w))[:MAX_TERMS]


def _like(term: str) -> str:
    escaped = term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


def _contains(col: Any, term: str) -> Any:
    """규칙 그 자체: 대소문자를 무시한 부분 문자열. SQLite 의 `LIKE` 와 달리 PostgreSQL 의 `LIKE` 는 대소문자를 가립니다."""
    return col.ilike(_like(term), escape=LIKE_ESCAPE)


def _phrase(term: str) -> str:
    """FTS5 특수문자 충돌을 방지하기 위해 단어를 안전한 구문(Phrase)으로 이스케이프합니다."""
    return '"' + term.replace('"', '""') + '"'


def _word_prefix(term: str) -> str:
    """`to_tsquery` 의 낱말 첫머리 구절. 낱말을 따옴표로 감싸 tsquery 연산자(`&`, `|`, `!`)가 글자로 읽히게 합니다."""
    return "'" + term.replace("\\", "\\\\").replace("'", "''") + "':*"


def snippet_of(content: str, terms: List[str]) -> str:
    """첫 번째 매칭 키워드 주변의 텍스트 스니펫을 추출하고 줄바꿈/공백을 정규화합니다."""
    flat = re.sub(r"\s+", " ", content or "").strip()
    lower = flat.casefold()
    found = [i for i in (lower.find(t.casefold()) for t in terms) if i >= 0]
    if not found:
        return flat[:SNIPPET_BEFORE + SNIPPET_AFTER]
    at = min(found)
    start, end = max(0, at - SNIPPET_BEFORE), at + SNIPPET_AFTER
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


class SearchBackend:
    """DB 하나의 색인과 찾는 길. 이 기본형은 색인 없이 훑습니다 — 어느 DB 에서나 규칙대로 돕니다."""

    async def ensure_index(self, conn: Any) -> None:
        """색인을 준비합니다. 못 만들어도 예외를 올리지 않고 훑는 쪽으로 돕니다."""

    def narrow(self, stmt: Any, terms: List[str]) -> Any:
        """발언 질의에 낱말 조건을 답니다."""
        return stmt.where(*(_contains(MessageModel.content, t) for t in terms))

    async def message_rows(self, db: Any, stmt: Any, terms: List[str]) -> Tuple[Sequence[Any], bool]:
        """맞는 발언 행과, 전부 찾지 못했는지 여부."""
        return (await db.execute(self.narrow(stmt, terms))).all(), False


class Fts5Backend(SearchBackend):
    """SQLite: 3글자 이상은 FTS5 trigram, 그보다 짧은 낱말은 LIKE."""

    def __init__(self) -> None:
        # 색인을 쓸 수 있는가. `ensure_index` 가 정합니다.
        self.indexed = False

    async def ensure_index(self, conn: Any) -> None:
        existed = (await conn.execute(text(_FTS5_EXISTS), {"name": FTS_TABLE})).first() is not None
        try:
            for statement in _FTS5_SCHEMA:
                await conn.execute(text(statement))
            if not existed:
                await conn.execute(text(_FTS5_REBUILD))
        except Exception as exc:  # noqa: BLE001 - 색인이 없어도 검색은 LIKE 로 됩니다
            logger.warning(f"Full-text search index unavailable ({exc}); searching with LIKE only")
            self.indexed = False
            return
        self.indexed = True

    def narrow(self, stmt: Any, terms: List[str]) -> Any:
        long_terms = [t for t in terms if len(t) >= FTS_MIN_CHARS] if self.indexed else []
        if long_terms:
            # 낱말마다 큰따옴표 구절로 감싸 AND 로 잇습니다.
            stmt = stmt.join(_fts, _fts.c.rowid == literal_column("messages.rowid")).where(
                _fts.c.content.op("MATCH")(" AND ".join(_phrase(t) for t in long_terms))
            )
        return super().narrow(stmt, [t for t in terms if t not in long_terms])


class PostgresBackend(SearchBackend):
    """PostgreSQL: `ILIKE` 를 `pg_trgm` GIN 과 `created_at` 색인으로 찾고, 너무 오래 걸리면 `tsvector` GIN 으로 일부라도 찾습니다."""

    def __init__(self) -> None:
        # 낱말 색인이 있는가. 없으면 시간 상한 없이 끝까지 훑습니다 (돌아갈 곳이 없으므로).
        self.word_index = False

    async def ensure_index(self, conn: Any) -> None:
        if await self._attempt(conn, _PG_TRGM_EXTENSION, "pg_trgm extension"):
            await self._attempt(conn, _PG_TRGM_INDEX, "trigram index")
            # C 로캘에서 pg_trgm 은 한국어를 글자로 보지 않습니다. 결과는 맞지만(ILIKE 가 판정합니다) 색인을 타지 못합니다.
            if not (await conn.execute(text("SELECT cardinality(show_trgm('가나다'))"))).scalar():
                logger.warning(
                    "pg_trgm cannot index Korean under this database's LC_CTYPE (C/POSIX). Searches stay correct "
                    "but scan every message. Create the database with a UTF-8 locale to use the index."
                )
        self.word_index = await self._attempt(conn, _PG_WORD_INDEX, "word index")
        await self._attempt(conn, _PG_RECENCY_INDEX, "recency index")

    @staticmethod
    async def _attempt(conn: Any, statement: str, what: str) -> bool:
        """SAVEPOINT 안에서 실행합니다. PostgreSQL 은 실패한 문장 뒤의 트랜잭션 전체를 막으므로, 그냥 잡으면 기동이 깨집니다."""
        try:
            async with conn.begin_nested():
                await conn.execute(text(statement))
            return True
        except SQLAlchemyError as exc:
            logger.warning(f"Search {what} unavailable ({exc}); searches will scan messages instead")
            return False

    def narrow_by_words(self, stmt: Any, terms: List[str]) -> Any:
        """낱말 색인으로 찾습니다. 첫머리만 맞으므로("캐시" → "캐시를" O, "분산캐시" X) 비상 경로에서만 씁니다."""
        vector = literal_column(_WORD_VECTOR.format(content="messages.content"))
        simple = literal_column("'simple'")
        return stmt.where(*(vector.op("@@")(func.to_tsquery(simple, _word_prefix(t))) for t in terms))

    async def message_rows(self, db: Any, stmt: Any, terms: List[str]) -> Tuple[Sequence[Any], bool]:
        if not self.word_index:
            return await super().message_rows(db, stmt, terms)
        exact = self.narrow(stmt, terms)
        if any(len(t) >= FTS_MIN_CHARS for t in terms):
            rows = await self._within_time_limit(db, exact, INDEX_SEARCH_TIMEOUT_MS)
            if rows is not None:
                return rows, False
        rows = await self._within_time_limit(db, exact, SCAN_SEARCH_TIMEOUT_MS, recent_first=True)
        if rows is not None:
            return rows, False
        logger.info(f"Exact message search exceeded {SCAN_SEARCH_TIMEOUT_MS} ms; falling back to the word index")
        # 여기까지 오는 것은 드문 짧은 낱말이고, 낱말 색인은 그런 낱말에 빠릅니다. 그래도 넘기면 찾지 못한 채로 끝내고 알립니다.
        return await self._within_time_limit(db, self.narrow_by_words(stmt, terms), SCAN_SEARCH_TIMEOUT_MS) or [], True

    @staticmethod
    async def _within_time_limit(
        db: Any, stmt: Any, limit_ms: int, *, recent_first: bool = False,
    ) -> Optional[Sequence[Any]]:
        """시간 상한 안에 끝난 질의의 행. 넘으면 None.

        `recent_first` 는 플래너가 `created_at` 색인을 따라가게 합니다. 부분 문자열은 통계로 빈도를 알 수 없어, 그대로 두면 흔한
        낱말도 드물다고 보고 맞는 행을 전부 모아 정렬합니다.
        """
        savepoint = await db.begin_nested()
        try:
            await db.execute(
                text("SELECT set_config('statement_timeout', :limit, true)"), {"limit": str(limit_ms)},
            )
            if recent_first:
                await db.execute(text(
                    "SELECT set_config('enable_bitmapscan', 'off', true), set_config('enable_seqscan', 'off', true)"
                ))
            return (await db.execute(stmt)).all()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _SQLSTATE_QUERY_CANCELED:
                raise
            return None
        finally:
            # 읽기만 했으므로 잃는 것은 없고, 위의 설정이 모두 되돌아갑니다 (`set_config(..., true)` 는 트랜잭션 끝까지 남습니다).
            await savepoint.rollback()


_BACKENDS: Dict[str, SearchBackend] = {"sqlite": Fts5Backend(), "postgresql": PostgresBackend()}
_DEFAULT_BACKEND = SearchBackend()


def backend_for(dialect_name: str) -> SearchBackend:
    return _BACKENDS.get(dialect_name, _DEFAULT_BACKEND)


async def ensure_search_index(conn: Any) -> None:
    """이 DB 의 검색 색인을 준비합니다 (`init_db`)."""
    await backend_for(conn.dialect.name).ensure_index(conn)


async def search_sessions(db: Any, query: str, limit: int = DEFAULT_LIMIT) -> SearchResults:
    """제목 또는 발언에 모든 키워드가 포함된 대화 세션 목록을 검색합니다."""
    results = SearchResults()
    terms = terms_of(query)
    if not terms:
        return results

    stmt = (
        select(
            MessageModel.id, MessageModel.session_id, MessageModel.sender_name, MessageModel.content,
            MessageModel.created_at, SessionModel.title,
        )
        .join(SessionModel, SessionModel.id == MessageModel.session_id)
        .where(MessageModel.msg_type != "error")
        .order_by(MessageModel.created_at.desc())
        .limit(limit * 20)
    )
    rows, results.partial = await backend_for(db.get_bind().dialect.name).message_rows(db, stmt, terms)

    hits: Dict[str, SearchHit] = {}
    for message_id, session_id, sender, content, created_at, title in rows:
        hit = hits.get(session_id)
        if hit is None:
            hits[session_id] = SearchHit(
                session_id=session_id, title=title or "", message_id=message_id, sender_name=sender or "",
                snippet=snippet_of(content, terms), at=created_at, matches=1,
            )
        else:
            hit.matches += 1

    title_rows = (await db.execute(
        select(SessionModel.id, SessionModel.title, SessionModel.updated_at)
        .where(*(_contains(SessionModel.title, t) for t in terms))
        .order_by(SessionModel.updated_at.desc())
    )).all()
    for session_id, title, updated_at in title_rows:
        if session_id not in hits:
            hits[session_id] = SearchHit(
                session_id=session_id, title=title or "", message_id=None, sender_name="",
                snippet="", at=updated_at, matches=0,
            )

    # SQLite 는 시간대를 버리고 돌려줍니다. UTC 로 다시 붙여 비교합니다.
    results.extend(sorted(hits.values(), key=lambda h: as_utc(h.at) or EPOCH, reverse=True)[:limit])
    return results
