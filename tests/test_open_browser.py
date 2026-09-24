"""실행 스크립트가 띄우는 브라우저 대기 스크립트.

여기서 지키려는 것.

* **주소가 한 곳에서만 온다.** conf.json 의 `[app]` 이 정본이고, 실행 스크립트가
  `--port` 로 덮어썼으면 그쪽을 따릅니다. 두 곳에 같은 값을 적어 두면 언젠가
  반드시 어긋납니다.
* **바인딩 주소를 그대로 열지 않는다.** `0.0.0.0` 은 접속 주소가 아닙니다.
* **서버가 뜬 뒤에 연다.** 곧바로 열면 "연결할 수 없음" 이 뜹니다.
"""

import socket
import threading
import time

import pytest

import open_browser


class _FakeApp:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port


@pytest.fixture()
def conf_app(monkeypatch):
    """conf.json 대신 쓸 [app] 값."""
    holder = {"app": _FakeApp("127.0.0.1", 8000)}

    class _Cfg:
        @property
        def app(self):
            return holder["app"]

    import app.config

    monkeypatch.setattr(app.config, "get_config", lambda *a, **k: _Cfg())
    return holder


# --------------------------------------------------------------- 주소 결정


def test_target_comes_from_the_config(conf_app):
    assert open_browser.resolve_target([]) == ("127.0.0.1", 8000)


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--port", "9000"], ("127.0.0.1", 9000)),
        (["--port=9000"], ("127.0.0.1", 9000)),
        (["-p", "9000"], ("127.0.0.1", 9000)),
        (["--host", "10.0.0.5"], ("10.0.0.5", 8000)),
        (["--no-reload", "--port", "9000"], ("127.0.0.1", 9000)),
    ],
)
def test_launcher_arguments_win_over_the_config(conf_app, argv, expected):
    """실행 스크립트가 app.main 에 넘긴 인자를 그대로 받습니다."""
    assert open_browser.resolve_target(argv) == expected


def test_wildcard_bind_address_becomes_loopback(conf_app):
    """0.0.0.0 은 바인딩 주소이지 접속 주소가 아닙니다."""
    conf_app["app"] = _FakeApp("0.0.0.0", 8000)
    assert open_browser.resolve_target([]) == ("127.0.0.1", 8000)


def test_unparsable_port_falls_back_to_the_config(conf_app):
    assert open_browser.resolve_target(["--port", "그냥글자"]) == ("127.0.0.1", 8000)


def test_no_config_and_no_port_means_no_browser(monkeypatch):
    import app.config

    def _boom(*a, **k):
        raise RuntimeError("conf.json 없음")

    monkeypatch.setattr(app.config, "get_config", _boom)
    assert open_browser.resolve_target([]) is None
    # 설정을 못 읽어도 인자로 포트를 받았으면 그것으로 진행합니다.
    assert open_browser.resolve_target(["--port", "9000"]) == ("127.0.0.1", 9000)


# --------------------------------------------------------------- 기다리기


def test_wait_returns_as_soon_as_the_port_answers():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert open_browser.wait_for_server("127.0.0.1", port, timeout=5) is True
    finally:
        server.close()


def test_wait_gives_up_when_the_server_never_comes_up():
    # 아무도 듣고 있지 않은 포트.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    started = time.monotonic()
    assert open_browser.wait_for_server("127.0.0.1", port, timeout=0.8) is False
    assert time.monotonic() - started < 5, "제한 시간을 넘겨 붙잡고 있으면 안 됩니다"


def test_wait_survives_a_server_that_starts_late():
    """서버가 늦게 떠도 열립니다. 곧바로 열면 연결 실패 화면을 보게 됩니다.

    포트를 잡아 두되 `listen()` 만 늦게 겁니다. 포트를 놓았다가 다시 잡으면 그
    틈에 다른 프로세스가 가져갈 수 있어, 테스트가 이따금 이유 없이 실패합니다.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))          # 포트는 처음부터 우리 것입니다
    port = server.getsockname()[1]

    def listen_later():
        time.sleep(0.8)
        server.listen(1)                   # 여기서부터 연결을 받습니다

    thread = threading.Thread(target=listen_later, daemon=True)
    thread.start()
    try:
        assert open_browser.wait_for_server("127.0.0.1", port, timeout=15) is True
    finally:
        thread.join(timeout=5)
        server.close()


# --------------------------------------------------------------- 전체 흐름


def test_main_opens_the_running_server(conf_app, monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    opened = []
    monkeypatch.setattr(open_browser.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.delenv("MADO_NO_BROWSER", raising=False)

    try:
        assert open_browser.main(["--port", str(port)]) == 0
    finally:
        server.close()

    assert opened == [f"http://127.0.0.1:{port}"]


def test_main_respects_the_opt_out(conf_app, monkeypatch):
    opened = []
    monkeypatch.setattr(open_browser.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setenv("MADO_NO_BROWSER", "1")

    assert open_browser.main([]) == 0
    assert opened == [], "MADO_NO_BROWSER 를 설정했는데 브라우저가 열렸습니다"


def test_main_gives_up_quietly_when_the_server_fails(conf_app, monkeypatch):
    """서버가 못 뜨면 콘솔에 그쪽 오류가 이미 있습니다. 여기서 더 시끄럽게 굴지 않습니다."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    opened = []
    monkeypatch.setattr(open_browser.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.delenv("MADO_NO_BROWSER", raising=False)
    monkeypatch.setenv("MADO_BROWSER_TIMEOUT", "0.8")

    assert open_browser.main(["--port", str(port)]) == 1
    assert opened == []
