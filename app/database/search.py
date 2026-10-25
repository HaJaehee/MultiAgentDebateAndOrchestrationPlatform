"""대화 검색: 세션 제목과 발언 본문에서 낱말을 찾습니다 (v1.5).

대화가 쌓이면 "그때 캐시 무효화를 어떻게 정했더라" 를 찾을 길이 정렬뿐이었습니다. 여기서 제목과 발언을
함께 찾아, 사이드바가 대화별로 한 줄씩(가장 최근에 맞은 발언 한 구절과 맞은 발언 수) 보여 줍니다.

## 어떻게 찾나

* **세 글자 이상인 낱말** — SQLite FTS5 의 `trigram` 색인(`messages_fts`)으로 찾습니다. 형태소 분석기
  없이 한국어 부분 일치가 되고, 새 의존성도 없습니다. 색인은 `messages` 의 트리거가 맞춰 두므로 발언을
  기록·고치·지우는 어느 길로 가든 따라갑니다.
* **두 글자 이하인 낱말** — trigram 은 세 글자보다 짧은 검색어를 찾지 못합니다. 한국어 낱말은 두
  글자가 흔해("캐시", "보안") 이쪽은 `LIKE` 로 찾습니다. 개인 PC 의 대화 수백 건 규모에서는 충분히 빠릅니다.
* 낱말이 여럿이면 모두 들어 있는 발언만 찾습니다(AND). 실패 안내(`error`)는 찾지 않습니다.

FTS5 나 trigram 이 없는 SQLite 면 모든 낱말을 `LIKE` 로 찾습니다 (`fts_available`). 검색어는 바인딩
매개변수로만 SQL 에 갑니다 (`tests/test_sql_strings.py`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, column, literal_column, select, table, text

from app.database.models import MessageModel, SessionModel
from app.timestamps import as_utc

logger = logging.getLogger(__name__)

FTS_TABLE = "messages_fts"
# 이보다 짧은 낱말은 trigram 색인이 찾지 못합니다.
FTS_MIN_CHARS = 3
# 한 번에 받는 낱말 수와 결과 수의 상한.
MAX_TERMS = 6
DEFAULT_LIMIT = 30
SNIPPET_BEFORE, SNIPPET_AFTER = 40, 90
LIKE_ESCAPE = "\\"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 색인은 `messages` 를 내용 테이블로 쓰는 외부 내용(external content) FTS5 표입니다. 본문을 두 번 저장하지
# 않고, 아래 트리거가 발언의 추가·수정·삭제를 색인에 옮깁니다 (SQLite 문서의 external content 예와 같은 모양).
_SCHEMA = (
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
_REBUILD = "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')"
_EXISTS = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"

# 질의에서 색인을 가리키는 가벼운 표 (ORM 모델이 아닙니다 — 만들고 지우는 것은 위 문장이 합니다).
_fts = table(FTS_TABLE, column("rowid"), column("content"))

# 색인을 쓸 수 있는가. `ensure_search_index` 가 정합니다 (프로세스에 DB 엔진은 하나입니다).
fts_available = False


async def ensure_search_index(conn: Any) -> None:
    """색인과 트리거를 만들고, 처음 만들었으면 기존 발언으로 채웁니다. 못 만들면 `LIKE` 로만 찾습니다."""
    global fts_available
    existed = (await conn.execute(text(_EXISTS), {"name": FTS_TABLE})).first() is not None
    try:
        for statement in _SCHEMA:
            await conn.execute(text(statement))
        if not existed:
            await conn.execute(text(_REBUILD))
    except Exception as exc:  # noqa: BLE001 - 색인이 없어도 검색은 LIKE 로 됩니다
        logger.warning(f"Full-text search index unavailable ({exc}); searching with LIKE only")
        fts_available = False
        return
    fts_available = True


@dataclass
class SearchHit:
    session_id: str
    title: str
    # 맞은 발언 (가장 최근 것). 제목만 맞았으면 None.
    message_id: Optional[str]
    sender_name: str
    snippet: str
    at: Optional[datetime]
    # 이 대화에서 맞은 발언 수
    matches: int = 0


def terms_of(query: str) -> List[str]:
    """검색어를 낱말로. 겹치는 낱말은 한 번만, 많아야 `MAX_TERMS` 개."""
    return list(dict.fromkeys(w for w in (query or "").split() if w))[:MAX_TERMS]


def _like(term: str) -> str:
    escaped = term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


def _phrase(term: str) -> str:
    """FTS5 질의 문법에 걸리지 않도록 낱말 하나를 큰따옴표 구절로."""
    return '"' + term.replace('"', '""') + '"'


def snippet_of(content: str, terms: List[str]) -> str:
    """처음 맞은 낱말 앞뒤 한 구절. 줄바꿈과 겹친 공백은 하나로 접습니다."""
    flat = re.sub(r"\s+", " ", content or "").strip()
    lower = flat.casefold()
    found = [i for i in (lower.find(t.casefold()) for t in terms) if i >= 0]
    if not found:
        return flat[:SNIPPET_BEFORE + SNIPPET_AFTER]
    at = min(found)
    start, end = max(0, at - SNIPPET_BEFORE), at + SNIPPET_AFTER
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


async def search_sessions(db: Any, query: str, limit: int = DEFAULT_LIMIT) -> List[SearchHit]:
    """제목이나 발언에 낱말이 모두 들어 있는 대화. 최근에 맞은 대화부터, 대화마다 한 건."""
    terms = terms_of(query)
    if not terms:
        return []
    long_terms = [t for t in terms if len(t) >= FTS_MIN_CHARS] if fts_available else []
    like_terms = [t for t in terms if t not in long_terms]

    stmt = (
        select(
            MessageModel.id, MessageModel.session_id, MessageModel.sender_name, MessageModel.content,
            MessageModel.created_at, SessionModel.title,
        )
        .join(SessionModel, SessionModel.id == MessageModel.session_id)
        .where(MessageModel.msg_type != "error")
    )
    if long_terms:
        # 낱말마다 큰따옴표 구절로 감싸 AND 로 잇습니다.
        stmt = stmt.join(_fts, _fts.c.rowid == literal_column("messages.rowid")).where(
            _fts.c.content.op("MATCH")(" AND ".join(_phrase(t) for t in long_terms))
        )
    for term in like_terms:
        stmt = stmt.where(MessageModel.content.like(_like(term), escape=LIKE_ESCAPE))
    rows = (await db.execute(stmt.order_by(MessageModel.created_at.desc()).limit(limit * 20))).all()

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
        .where(and_(*(SessionModel.title.like(_like(t), escape=LIKE_ESCAPE) for t in terms)))
        .order_by(SessionModel.updated_at.desc())
    )).all()
    for session_id, title, updated_at in title_rows:
        if session_id not in hits:
            hits[session_id] = SearchHit(
                session_id=session_id, title=title or "", message_id=None, sender_name="",
                snippet="", at=updated_at, matches=0,
            )

    # SQLite 는 시간대를 버리고 돌려줍니다. UTC 로 다시 붙여 비교합니다.
    ordered = sorted(hits.values(), key=lambda h: as_utc(h.at) or EPOCH, reverse=True)
    return ordered[:limit]
