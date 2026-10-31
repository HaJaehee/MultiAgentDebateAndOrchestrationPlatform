"""대화 세션 및 발언 검색 테스트 (app/database/search.py, v1.5, ADR-041).

같은 시험을 SQLite 와 PostgreSQL 에서 돌립니다. 찾는 길은 달라도 **규칙과 결과는 같아야** 하기 때문입니다
(PostgreSQL 쪽은 `MADO_TEST_POSTGRES_URL` 이 있을 때만 돕니다).

1. 3글자 이상과 2글자 이하가 섞인 다중 키워드 AND 조건, 대소문자 무시 검증.
2. 발언 테이블(messages) CUD 이벤트 시 색인 실시간 동기화 검증.
3. 세션별 단일 항목 집계: 최근 매칭 발언 스니펫, 매칭 건수, 제목 단독 매칭 세션 지원 검증.
4. 오류 메시지 제외 필터링 및 특수문자/따옴표/와일드카드 리터럴 처리 검증.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from nicegui import ui
from sqlalchemy import delete, update

from app.database import search
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.database.search import search_sessions, snippet_of, terms_of
from app.ui.components import sidebar as sidebar_module
from tests.pg_support import postgres_database

T0 = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def factory(request, monkeypatch):
    if request.param == "postgresql":
        async with postgres_database(monkeypatch) as pg_factory:
            yield pg_factory
        return
    await init_db(SQLITE_URL)
    yield get_session_factory(SQLITE_URL)


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
async def test_this_sqlite_has_the_trigram_index():
    await init_db(SQLITE_URL)
    assert search.backend_for("sqlite").indexed, "이 PC 의 SQLite 는 FTS5 trigram 을 지원해야 합니다"


@pytest.mark.asyncio
async def test_long_and_short_terms_together(factory):
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
async def test_a_term_matches_inside_a_word(factory):
    """형태소 분석기가 없으므로 합성어 안의 낱말도 찾아야 합니다. tsvector 만 쓰면 놓치는 경우입니다."""
    sid = await _session(factory, "제목", "분산찹쌀떡 구성과 정보꿀벌 정책")

    async with factory() as db:
        assert [h.session_id for h in _mine(await search_sessions(db, "꿀벌"), sid)] == [sid]
        assert [h.session_id for h in _mine(await search_sessions(db, "산찹쌀"), sid)] == [sid]


@pytest.mark.asyncio
async def test_one_row_per_session_newest_hit_and_titles(factory):
    a = await _session(factory, "아키텍처", "레디스 캐시 도입", "레디스 캐시 제거", "레디스 대신 메모리")
    t = await _session(factory, "레디스 운영 메모")
    async with factory() as db:
        hits = _mine(await search_sessions(db, "레디스"), a, t)
    by_id = {h.session_id: h for h in hits}
    assert by_id[a].matches == 3 and by_id[a].message_id == f"{a}-m2"   # 가장 최근에 맞은 발언
    assert by_id[t].message_id is None and by_id[t].matches == 0        # 제목만 맞음
    assert [h.session_id for h in hits] == [a, t]                       # 최근에 맞은 대화부터


@pytest.mark.asyncio
async def test_the_index_follows_inserts_updates_and_deletes(factory):
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
async def test_errors_are_skipped_and_query_syntax_is_literal(factory):
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


@pytest.mark.asyncio
async def test_case_is_ignored_for_long_and_short_terms(factory):
    """규칙은 대소문자 무시입니다. PostgreSQL 의 LIKE 는 대소문자를 가리므로 `ilike` 로 적어야 같은 결과가 나옵니다."""
    # 메모리 DB 는 시험 사이에 공유되어, 흔한 낱말이면 더 새로운 발언이 상한을 먼저 채웁니다.
    sid = await _session(factory, "Kubezyx 운영", "Zyxredis Cluster 를 QZ 로 튜닝")

    async def found(query):
        async with factory() as db:
            return [h.session_id for h in _mine(await search_sessions(db, query), sid)]

    assert await found("ZYXREDIS") == [sid]  # 색인 길이
    assert await found("qz") == [sid]        # 2글자 (LIKE)
    assert await found("KUBEZYX") == [sid]   # 제목


@pytest.mark.asyncio
async def test_results_are_complete_unless_marked_partial(factory):
    sid = await _session(factory, "제목", "꿀벌 점검")
    async with factory() as db:
        hits = await search_sessions(db, "꿀벌")
    assert hits.partial is False and _mine(hits, sid)


@pytest.mark.asyncio
async def test_the_sidebar_says_when_results_are_partial(ui_page, monkeypatch):
    """낱말 첫머리만 찾았으면 결과가 적을 수 있다고 사람에게 말해야 합니다."""
    await init_db(SQLITE_URL)

    async def noop(*_args) -> None:
        return None

    async def labels_for(results) -> list:
        async def fake_search(_db, _query):
            return results

        monkeypatch.setattr(sidebar_module, "search_sessions", fake_search)
        with ui_page:
            sidebar = sidebar_module.SessionSidebar(on_session_selected=noop, on_new_session=noop)
            sidebar.container = ui.column()
            sidebar.search_query = "꿀벌"
            await sidebar._render_search()
        return [child.text for child in sidebar.container.default_slot.children if hasattr(child, "text")]

    partial = search.SearchResults()
    partial.partial = True
    assert any("낱말 첫머리" in text for text in await labels_for(partial))
    assert not any("낱말 첫머리" in text for text in await labels_for(search.SearchResults()))
