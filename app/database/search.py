"""대화 세션 및 발언 전문 검색: 세션 제목과 발언 본문 통합 검색 (v1.5).

세션이 누적되었을 때 특정 의사결정 내역이나 키워드를 신속하게 탐색할 수 있도록 지원합니다.
사이드바에서 세션 제목과 발언 본문을 통합 검색하며, 세션별로 최근 매칭된 발언 스니펫과 매칭 건수를 반환합니다.

## 검색 전략

* **3글자 이상 단어** — SQLite FTS5의 `trigram` 토크나이저 인덱스(`messages_fts`)를 사용합니다.
  외부 형태소 분석기 의존성 없이 한국어/영문 부분 문자열 일치가 가능합니다.
  인덱스는 `messages` 테이블 트리거를 통해 생성·수정·삭제 시 실시간 자동 동기화됩니다.
* **2글자 이하 단어** — FTS5 trigram은 3글자 미만 검색어를 지원하지 않으므로("캐시", "보안" 등),
  단문 키워드는 `LIKE` 연산자를 활용하여 검색합니다.
* 다중 검색어 입력 시 모든 키워드를 포함하는 발언만 필터링합니다(AND). 시스템 오류 메시지(`msg_type="error"`)는 검색에서 제외됩니다.

SQLite 환경에서 FTS5나 trigram을 지원하지 않는 경우 자동으로 `LIKE` 검색으로 대체됩니다 (`fts_available`).
모든 검색어는 SQL 인젝션 방지를 위해 바인딩 파라미터로 안전하게 전달됩니다.
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
# Trigram 인덱스 최소 글자 수 (이보다 짧은 단어는 LIKE 검색으로 처리)
FTS_MIN_CHARS = 3
# 최대 검색어 단어 수 및 최대 반환 세션 수
MAX_TERMS = 6
DEFAULT_LIMIT = 30
SNIPPET_BEFORE, SNIPPET_AFTER = 40, 90
LIKE_ESCAPE = "\\"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# `messages` 테이블을 external content로 사용하는 FTS5 가상 테이블.
# 본문 중복 저장을 방지하고 트리거를 통해 CUD 변경 사항을 인덱스에 실시간 반영합니다.
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
    """FTS5 인덱스 및 동기화 트리거를 생성하고 초기 데이터를 색인합니다. 미지원 환경에서는 LIKE 모드로 전환합니다."""
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
    # 매칭된 가장 최근 발언 정보 (제목만 일치한 경우 None)
    message_id: Optional[str]
    sender_name: str
    snippet: str
    at: Optional[datetime]
    # 해당 세션 내 매칭 발언 건수
    matches: int = 0


def terms_of(query: str) -> List[str]:
    """입력된 검색 쿼리를 개별 키워드 리스트로 분리합니다 (중복 제거, 최대 MAX_TERMS개)."""
    return list(dict.fromkeys(w for w in (query or "").split() if w))[:MAX_TERMS]


def _like(term: str) -> str:
    escaped = term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


def _phrase(term: str) -> str:
    """FTS5 특수문자 충돌을 방지하기 위해 단어를 안전한 구문(Phrase)으로 이스케이프합니다."""
    return '"' + term.replace('"', '""') + '"'


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


async def search_sessions(db: Any, query: str, limit: int = DEFAULT_LIMIT) -> List[SearchHit]:
    """제목 또는 발언에 모든 키워드가 포함된 대화 세션 목록을 검색합니다."""
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
