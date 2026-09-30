"""방문자 통로 (app/security.py 의 GuestGate, app/trial/gate.py).

체험 서버가 켜지면 토큰 없는 원격 접속은 거부 대신 방문자가 됩니다. 방문자에게 열리는 것은
체험 화면과 그 화면이 쓰는 NiceGUI 자원뿐이어야 합니다. 주인 화면·API·작업 공간 다운로드가
하나라도 새면 "남의 대화가 보이는" 사고가 납니다.
"""

import asyncio

import httpx
import nicegui
import pytest

from app.security import (
    COOKIE_NAME,
    VIEWER_GUEST,
    VIEWER_OWNER,
    AccessControl,
    AccessMiddleware,
    viewer_role,
)
from app.trial.gate import TrialGuestGate, guest_path_allowed

TOKEN = "Abc123Def456Ghi789Jkl012"
REMOTE = ("10.0.0.5", 50000)
LOCAL = ("127.0.0.1", 50000)
STATIC = f"/_nicegui/{nicegui.__version__}/static/nicegui.js"

seen_roles = []


async def inner_app(scope, receive, send):
    seen_roles.append(viewer_role(scope))
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"inner"})


class Gate:
    home_path = "/trial"

    def __init__(self, enabled=True):
        self.on = enabled

    def enabled(self):
        return self.on

    def allows(self, path):
        return guest_path_allowed(path)


def _client(control, addr, gate, cookies=None):
    transport = httpx.ASGITransport(app=AccessMiddleware(inner_app, control, guest_gate=gate), client=addr)
    return httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False,
                             cookies=cookies or {})


def run(coro):
    return asyncio.run(coro)


async def _get(control, path, *, addr=REMOTE, gate=None, html=True, cookies=None):
    async with _client(control, addr, gate if gate is not None else Gate(), cookies) as client:
        headers = {"accept": "text/html"} if html else {}
        return await client.get(path, headers=headers)


async def _ws(control, path, gate):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": path, "client": REMOTE, "headers": [(b"host", b"mado:8000")],
             "query_string": b""}
    await AccessMiddleware(inner_app, control, guest_gate=gate)(scope, receive, send)
    return sent[0]["type"]


# ------------------------------------------------------------------ 열리는 것


@pytest.mark.parametrize("path", [
    "/trial", "/trial/login", "/trial/s/abc", "/trial/start/t:report-review", STATIC,
    f"/_nicegui/{nicegui.__version__}/components/x.js", "/_nicegui/client/abc/upload/1",
    "/favicon.ico", "/agent-icon",
])
def test_visitors_reach_the_trial_and_its_assets(path):
    seen_roles.clear()
    response = run(_get(AccessControl(TOKEN), path, html=False))
    assert response.status_code == 200, path
    assert seen_roles == [VIEWER_GUEST]


def test_the_socket_is_open_to_visitors():
    assert run(_ws(AccessControl(TOKEN), "/_nicegui_ws/socket.io/", Gate())) == "websocket.accept"


# ------------------------------------------------------------------ 막히는 것


@pytest.mark.parametrize("path", [
    "/api/agents", "/api/mcp", "/api/sessions/abc/personas", "/personas/abc", "/graphs/g1",
    "/_nicegui/auto/static/deadbeef/secret.txt", "/_nicegui/auto/media/deadbeef/a.mp4",
    "/_mado/download/abc.txt", "/trial/../api/agents", "/trialx", "/docs",
])
def test_visitors_never_reach_owner_pages_apis_or_downloads(path):
    seen_roles.clear()
    response = run(_get(AccessControl(TOKEN), path, html=False))
    assert response.status_code == 403, path
    assert seen_roles == []


def test_the_owner_home_sends_visitors_to_the_trial():
    response = run(_get(AccessControl(TOKEN), "/"))
    assert response.status_code == 303 and response.headers["location"] == "/trial"


def test_owner_sockets_are_not_opened_by_other_paths():
    assert run(_ws(AccessControl(TOKEN), "/some/other/socket", Gate())) == "websocket.close"


# ------------------------------------------------------------------ 주인은 그대로


def test_the_owner_keeps_everything_and_is_marked_as_owner():
    control = AccessControl(TOKEN)
    seen_roles.clear()
    cookie = {COOKIE_NAME: control.issue_cookie()}
    assert run(_get(control, "/api/agents", html=False, cookies=cookie)).status_code == 200
    assert run(_get(control, "/trial", html=False, cookies=cookie)).status_code == 200
    assert seen_roles == [VIEWER_OWNER, VIEWER_OWNER]


def test_loopback_is_the_owner():
    seen_roles.clear()
    assert run(_get(AccessControl(None), "/api/agents", addr=LOCAL, html=False)).status_code == 200
    assert seen_roles == [VIEWER_OWNER]


def test_the_owner_login_stays_reachable_for_remote_owners():
    response = run(_get(AccessControl(TOKEN), "/login"))
    assert response.status_code == 200 and "MADO 원격 접속" in response.text


# ------------------------------------------------------------------ 꺼져 있을 때


def test_with_the_trial_off_remote_access_is_exactly_as_before():
    control = AccessControl(TOKEN)
    off = Gate(enabled=False)
    response = run(_get(control, "/trial", gate=off))
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert run(_get(control, "/api/agents", gate=off, html=False)).status_code == 401


def test_without_an_owner_token_the_trial_still_opens_but_the_owner_side_stays_shut():
    control = AccessControl(None)
    assert run(_get(control, "/trial", html=False)).status_code == 200
    assert run(_get(control, "/")).headers["location"] == "/trial"
    assert run(_get(control, "/api/agents", html=False)).status_code == 403
    off = run(_get(control, "/trial", gate=Gate(enabled=False)))
    assert off.status_code == 403, "체험이 꺼져 있으면 예전처럼 원격 전체가 닫힙니다"


def test_requests_that_skip_the_middleware_are_treated_as_visitors():
    assert viewer_role({"type": "http"}) == VIEWER_GUEST
    assert viewer_role({"state": {"mado_viewer": "admin"}}) == VIEWER_GUEST


def test_the_real_gate_follows_the_config(monkeypatch):
    from types import SimpleNamespace

    import app.config as config_module
    from app.config import TrialConfig

    cfg = SimpleNamespace(trial=TrialConfig(enabled=True))
    monkeypatch.setattr(config_module, "get_config", lambda *a, **k: cfg)
    gate = TrialGuestGate()
    assert gate.enabled() and gate.allows("/trial") and not gate.allows("/")
    cfg.trial.enabled = False
    assert not gate.enabled()


def test_the_app_wires_the_gate_into_the_outermost_middleware():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "server.add_middleware(AccessMiddleware, guest_gate=trial_gate)" in source
    assert source.index("trial_gate = setup_trial(server)") < source.index("ui.run_with(")
