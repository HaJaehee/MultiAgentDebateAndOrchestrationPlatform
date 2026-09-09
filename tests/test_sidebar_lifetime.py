"""사이드바가 사라진 페이지에 그리지 않는지.

토론은 백그라운드에서 계속 굴러가고, 이벤트 소비자는 목록이 바뀌는 이벤트마다
`refresh_list()` 를 부릅니다. 그 안에는 DB 조회라는 `await` 가 있고, 그 사이에
사람이 새로고침하면 NiceGUI 가 클라이언트를 지웁니다. 깨어난 코드가 그대로
그리면 죽은 클라이언트에 세션 카드를 만들게 되고, NiceGUI 는

    Client has been deleted but is still being used.

로 한 번 경고하고 그 갱신을 버립니다. 화면이 깨지지는 않지만, 이 경고는 프로세스당
한 번뿐이라 이 자리가 그것을 먹고 있으면 나중의 진짜 use-after-free 가 로그에서
사라집니다.
"""

from types import SimpleNamespace

import pytest

from app.database.session import init_db
from app.ui.components.sidebar import SessionSidebar

DB_URL = "sqlite+aiosqlite:///:memory:"


class _FakeContainer:
    """`ui.column` 대신. 화면 작업이 일어났는지만 봅니다."""

    def __init__(self) -> None:
        self.is_deleted = False
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1


async def _sidebar() -> SessionSidebar:
    await init_db(DB_URL)

    async def _noop(*_args) -> None:
        return None

    sidebar = SessionSidebar(on_session_selected=_noop, on_new_session=_noop)
    sidebar.container = _FakeContainer()
    return sidebar


def _factory(on_read=None):
    """조회 한 번을 흉내 내는 세션 팩토리. `on_read` 는 조회 도중에 벌어지는 일."""
    reads = []

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: [])

        def all(self):
            return []

    class _DB:
        async def execute(self, *_args, **_kwargs):
            reads.append(1)
            if on_read is not None:
                on_read()
            return _Result()

    class _Factory:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *_exc):
            return False

    return (lambda: _Factory()), reads


@pytest.mark.asyncio
async def test_a_dead_page_is_not_drawn_on():
    sidebar = await _sidebar()
    sidebar.container.is_deleted = True
    factory, reads = _factory()
    sidebar.session_factory = factory

    await sidebar.refresh_list()

    assert reads == [], "죽은 페이지를 위해 DB 를 읽을 이유도 없습니다"
    assert sidebar.container.cleared == 0


@pytest.mark.asyncio
async def test_a_page_that_dies_during_the_read_is_not_drawn_on():
    """이것이 실제로 걸리는 자리입니다 — 조회를 기다리는 사이의 새로고침."""
    sidebar = await _sidebar()
    container = sidebar.container
    factory, reads = _factory(on_read=lambda: setattr(container, "is_deleted", True))
    sidebar.session_factory = factory

    await sidebar.refresh_list()

    assert reads, "조회는 시작되었습니다"
    # 예전에는 `clear()` 가 조회보다 **앞에** 있어서, 여기서 목록을 비운 뒤
    # 죽은 클라이언트에 카드를 새로 만들었습니다.
    assert container.cleared == 0, "읽고 난 뒤에 다시 확인하고 그려야 합니다"


@pytest.mark.asyncio
async def test_alive_reads_the_container_not_just_its_presence():
    sidebar = await _sidebar()
    assert sidebar.alive is True

    sidebar.container.is_deleted = True
    assert sidebar.alive is False, "존재만이 아니라 삭제 여부까지 봐야 합니다"

    sidebar.container = None
    assert sidebar.alive is False


@pytest.mark.asyncio
async def test_notify_is_dropped_when_the_page_is_gone(monkeypatch):
    """`ui.notify` 는 클라이언트에 직접 보내는 메시지라 경고를 남기는 갈래입니다."""
    import app.ui.components.sidebar as sidebar_module

    sent = []
    monkeypatch.setattr(sidebar_module.ui, "notify", lambda *a, **k: sent.append(a))

    sidebar = await _sidebar()
    sidebar._notify("살아 있을 때")
    assert len(sent) == 1

    sidebar.container.is_deleted = True
    sidebar._notify("사라진 뒤")
    assert len(sent) == 1, "사라진 페이지에는 알리지 않습니다"
