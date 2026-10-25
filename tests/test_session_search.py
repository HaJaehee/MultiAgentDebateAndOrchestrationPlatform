"""대화 검색 (app/database/search.py, v1.5).

1. 세 글자 이상은 전문 색인으로, 두 글자 이하는 LIKE 로 찾고, 낱말이 여럿이면 모두 들어 있어야 한다.
2. 색인은 발언의 추가·수정·삭제를 따라간다 (트리거). 이미 있던 발언은 색인을 처음 만들 때 채운다.
3. 대화마다 한 건 — 가장 최근에 맞은 발언의 한 구절과 맞은 발언 수. 제목만 맞은 대화도 나온다.
4. 실패 안내는 찾지 않고, 검색어의 따옴표·와일드카드는 글자 그대로 찾는다.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, update

from app.database import search
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.database.search import search_sessions, snippet_of, terms_of

T0 = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)


async def _db():
    url = "sqlite+aiosqlite:///:memory:"
    await init_db(url)
    return get_session_factory(url)


def _mine(hits, *sids):
    """이 시험이 만든 대화의 결과만 (메모리 DB 는 시험 사이에 공유될 수 있습니다)."""
    return [h for h in hits if h.session_id in sids]


async def _session(factory, title: str, *speeches: str, msg_type: str = "agent") -> str:
    sid = f"s-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(id=sid, title=title, updated_at=T0))
        for i, text in enumerate(speeches):
            db.add(MessageModel(
                id=f"{sid}-m{i}", session_id=sid, sender_key="architect", sender_name="System Architect",
                content=text, msg_type=msg_type, created_at=T0 + timedelta(minutes=i),
            ))
        await db.commit()
    return sid


def test_terms_and_snippets():
    assert terms_of("  캐시  무효화 캐시 ") == ["캐시", "무효화"]
    long = "가" * 100 + " 캐시 무효화는 TTL 로 한다 " + "나" * 200
    snippet = snippet_of(long, ["무효화"])
    assert snippet.startswith("…") and snippet.endswith("…") and "캐시 무효화는 TTL" in snippet
    assert snippet_of("줄\n바꿈\n\n있는   글", ["없는말"]) == "줄 바꿈 있는 글"


@pytest.mark.asyncio
async def test_long_and_short_terms_together():
    factory = await _db()
    assert search.fts_available, "이 PC 의 SQLite 는 FTS5 trigram 을 지원해야 합니다"
    a = await _session(factory, "결제 설계", "캐시 무효화는 TTL 로 한다", "보안 검토는 다음 턴에")
    b = await _session(factory, "다른 대화", "무효화 정책 없음")

    async with factory() as db:
        both = _mine(await search_sessions(db, "캐시 무효화"), a, b)      # 두 글자(LIKE) + 세 글자(색인)
        short = _mine(await search_sessions(db, "보안"), a, b)
        none = _mine(await search_sessions(db, "캐시 없는낱말"), a, b)
    assert [h.session_id for h in both] == [a]
    assert both[0].message_id == f"{a}-m0" and "캐시 무효화" in both[0].snippet and both[0].matches == 1
    assert [h.session_id for h in short] == [a] and short[0].message_id == f"{a}-m1"
    assert none == []


@pytest.mark.asyncio
async def test_one_row_per_session_newest_hit_and_titles():
    factory = await _db()
    a = await _session(factory, "아키텍처", "레디스 캐시 도입", "레디스 캐시 제거", "레디스 대신 메모리")
    t = await _session(factory, "레디스 운영 메모")
    async with factory() as db:
        hits = _mine(await search_sessions(db, "레디스"), a, t)
    by_id = {h.session_id: h for h in hits}
    assert by_id[a].matches == 3 and by_id[a].message_id == f"{a}-m2"   # 가장 최근에 맞은 발언
    assert by_id[t].message_id is None and by_id[t].matches == 0        # 제목만 맞음
    assert [h.session_id for h in hits] == [a, t]                       # 최근에 맞은 대화부터


@pytest.mark.asyncio
async def test_the_index_follows_inserts_updates_and_deletes():
    factory = await _db()
    sid = await _session(factory, "추적", "처음 문장")
    async with factory() as db:
        await db.execute(update(MessageModel).where(MessageModel.id == f"{sid}-m0").values(content="고쳐 쓴 문장 쿠버네티스"))
        await db.commit()
        assert [h.session_id for h in _mine(await search_sessions(db, "쿠버네티스"), sid)] == [sid]
        assert _mine(await search_sessions(db, "처음 문장"), sid) == []
        await db.execute(delete(MessageModel).where(MessageModel.session_id == sid))
        await db.commit()
        assert _mine(await search_sessions(db, "쿠버네티스"), sid) == []


@pytest.mark.asyncio
async def test_errors_are_skipped_and_query_syntax_is_literal():
    factory = await _db()
    failed = await _session(factory, "실패", "엔드포인트 연결 실패", msg_type="error")
    odd = await _session(factory, "기호", 'name="x" AND 100%_done OR NEAR(a b)')

    async def found(query):
        async with factory() as db:
            return [h.session_id for h in _mine(await search_sessions(db, query), failed, odd)]

    assert await found("엔드포인트") == []
    assert await found('"x"') == [odd]
    assert await found("100%_") == [odd]
    assert await found("NEAR(a") == [odd]
    # `_` 가 와일드카드였다면 `"x` 에 맞았을 것입니다. 글자 `_` 로만 찾습니다.
    assert await found("_x") == []
    async with factory() as db:
        assert await search_sessions(db, "   ") == []
