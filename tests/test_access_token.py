"""원격 접속 토큰 (app/security.py, app/ui/components/access_token.py).

루프백은 토큰 없이, 원격은 토큰 로그인(7일)으로만. 실제 ASGI 요청을 접속 주소를 바꿔 가며 보냅니다.
"""

import asyncio
import io
import re
from pathlib import Path

import httpx
import pytest

from app.security import (
    COOKIE_NAME,
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    SESSION_SECONDS,
    TOKEN_ENV,
    AccessControl,
    AccessMiddleware,
    generate_token,
    is_loopback,
    read_env_token,
    same_origin,
    token_problem,
    write_env_token,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "Abc123Def456Ghi789Jkl012"
REMOTE = ("10.0.0.5", 50000)
LOCAL = ("127.0.0.1", 50000)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


async def inner_app(scope, receive, send):
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"inner"})


def _client(control, client_addr, host="localhost:8000"):
    transport = httpx.ASGITransport(app=AccessMiddleware(inner_app, control), client=client_addr)
    return httpx.AsyncClient(transport=transport, base_url=f"http://{host}", follow_redirects=False)


async def _ws(control, client_addr, headers):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket", "path": "/_nicegui_ws/socket.io/", "client": client_addr,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "query_string": b"",
    }
    await AccessMiddleware(inner_app, control)(scope, receive, send)
    return sent[0]["type"], sent[0].get("code")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 토큰 형식


@pytest.mark.parametrize("token", ["0" * 24, TOKEN, "Z" * 24])
def test_any_24_alphanumerics_are_accepted_without_strength_checks(token):
    assert token_problem(token) is None


@pytest.mark.parametrize("token", [None, "", "a" * 23, "a" * 25, "a" * 23 + "!", "a" * 23 + " ", "가" * 24,
                                  "a" * 23 + "٣", "a" * 23 + "_"])
def test_wrong_length_or_characters_are_refused(token):
    assert token_problem(token)


def test_generated_tokens_are_24_alphanumerics_and_random():
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9]{24}", t) for t in tokens)


# ------------------------------------------------------------------ .env


def test_writing_keeps_other_lines_comments_and_crlf(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"# \xec\xa3\xbc\xec\x84\x9d\r\nLLM_API_KEY=secret\r\nMADO_ACCESS_TOKEN=old\r\nexport MADO_ACCESS_TOKEN=dup\r\n")
    write_env_token(env, TOKEN)
    text = env.read_bytes().decode("utf-8")
    assert text == f"# 주석\r\nLLM_API_KEY=secret\r\n{TOKEN_ENV}={TOKEN}\r\n"
    assert read_env_token(env) == TOKEN
    assert [p.name for p in tmp_path.iterdir()] == [".env"], "임시 파일이 남으면 안 됩니다"


def test_stray_carriage_returns_do_not_turn_into_blank_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\r\nMADO_ACCESS_TOKEN=old\r\r\n")
    write_env_token(env, TOKEN)
    assert env.read_bytes() == f"A=1\r\n{TOKEN_ENV}={TOKEN}\r\n".encode()


def test_writing_appends_when_missing_and_creates_the_file(tmp_path):
    env = tmp_path / ".env"
    write_env_token(env, TOKEN)
    assert read_env_token(env) == TOKEN
    env.write_text("A=1", encoding="utf-8")
    write_env_token(env, TOKEN)
    assert env.read_text(encoding="utf-8") == f"A=1\n{TOKEN_ENV}={TOKEN}\n"


def test_an_invalid_token_is_never_written(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        write_env_token(env, "short")
    assert env.read_text(encoding="utf-8") == "A=1\n"


def test_reading_accepts_quotes_and_missing_file(tmp_path):
    env = tmp_path / ".env"
    assert read_env_token(env) is None
    env.write_text(f'{TOKEN_ENV}="{TOKEN}"\n', encoding="utf-8")
    assert read_env_token(env) == TOKEN


# ------------------------------------------------------------------ 주소


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("10.0.0.5", False), ("192.168.0.2", False), ("", False), ("testclient", False), ("localhost", False),
])
def test_loopback_is_decided_by_the_connection_address(host, expected):
    assert is_loopback(host) is expected


def test_same_origin_compares_host_and_port():
    assert same_origin("http://10.0.0.1:8000", "10.0.0.1:8000")
    assert not same_origin("http://evil.example", "10.0.0.1:8000")
    assert not same_origin("http://10.0.0.1:9000", "10.0.0.1:8000")
    assert not same_origin("null", "10.0.0.1:8000")


# ------------------------------------------------------------------ 루프백


def test_loopback_needs_no_token_even_when_none_is_configured():
    async def go():
        async with _client(AccessControl(None), LOCAL) as c:
            r = await c.get("/")
            return r.status_code, r.text
    assert run(go()) == (200, "inner")


def test_loopback_with_a_foreign_host_name_is_refused_dns_rebinding():
    async def go():
        async with _client(AccessControl(TOKEN), LOCAL, host="evil.example:8000") as c:
            return (await c.get("/")).status_code
    assert run(go()) == 403


def test_a_cross_origin_request_to_loopback_is_refused():
    async def go():
        async with _client(AccessControl(TOKEN), LOCAL) as c:
            bad = await c.post("/api/x", headers={"origin": "http://evil.example"})
            good = await c.post("/api/x", headers={"origin": "http://localhost:8000"})
            return bad.status_code, good.status_code
    assert run(go()) == (403, 200)


def test_a_cross_origin_websocket_to_loopback_is_refused():
    control = AccessControl(TOKEN)
    assert run(_ws(control, LOCAL, {"host": "127.0.0.1:8000", "origin": "http://evil.example"})) == ("websocket.close", 1008)
    assert run(_ws(control, LOCAL, {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"}))[0] == "websocket.accept"


# ------------------------------------------------------------------ 원격


def test_remote_is_refused_entirely_without_a_valid_token():
    async def go():
        async with _client(AccessControl("too-short"), REMOTE, host="10.0.0.1:8000") as c:
            page = await c.get("/", headers={"accept": "text/html"})
            login = await c.post("/login", data={"token": "too-short"})
            return page.status_code, "원격 접속이 꺼져 있습니다" in page.text, login.status_code
    assert run(go()) == (403, True, 403)
    assert run(_ws(AccessControl(None), REMOTE, {"host": "10.0.0.1:8000"})) == ("websocket.close", 1008)


def test_remote_without_login_is_sent_to_the_login_page_and_apis_are_closed():
    async def go():
        async with _client(AccessControl(TOKEN), REMOTE, host="10.0.0.1:8000") as c:
            page = await c.get("/personas/abc?x=1", headers={"accept": "text/html"})
            api = await c.get("/api/agents")
            download = await c.get("/_mado/download/abc.zip")
            form = await c.get(page.headers["location"])
            return page, api.status_code, download.status_code, form
    page, api, download, form = run(go())
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=/personas/abc%3Fx%3D1"
    assert (api, download) == (401, 401)
    assert form.status_code == 200 and 'method="post"' in form.text and 'value="/personas/abc?x=1"' in form.text
    assert run(_ws(AccessControl(TOKEN), REMOTE, {"host": "10.0.0.1:8000"})) == ("websocket.close", 1008)


def test_login_sets_a_7_day_http_only_strict_cookie_that_opens_everything():
    async def go():
        async with _client(AccessControl(TOKEN), REMOTE, host="10.0.0.1:8000") as c:
            r = await c.post("/login", data={"token": TOKEN, "next": "/personas/abc"},
                             headers={"origin": "http://10.0.0.1:8000"})
            cookie = r.headers["set-cookie"]
            value = cookie.split(";")[0].split("=", 1)[1]
            after = await c.get("/api/agents", headers={"cookie": f"{COOKIE_NAME}={value}"})
            return r, cookie, value, after
    r, cookie, value, after = run(go())
    assert r.status_code == 303 and r.headers["location"] == "/personas/abc"
    assert f"Max-Age={SESSION_SECONDS}" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert TOKEN not in cookie and TOKEN not in r.headers["location"], "토큰은 쿠키·주소에 들어가지 않습니다"
    assert after.status_code == 200 and after.text == "inner"
    control = AccessControl(TOKEN)
    assert run(_ws(control, REMOTE, {"host": "10.0.0.1:8000", "cookie": f"{COOKIE_NAME}={value}"}))[0] == "websocket.accept"


def test_login_redirect_never_leaves_the_server():
    async def go():
        async with _client(AccessControl(TOKEN), REMOTE, host="10.0.0.1:8000") as c:
            return [
                (await c.post("/login", data={"token": TOKEN, "next": nxt})).headers["location"]
                for nxt in ("//evil.example/x", "http://evil.example", "\\\\evil", "")
            ]
    assert run(go()) == ["/", "/", "/", "/"]


def test_cross_origin_login_post_is_refused():
    async def go():
        async with _client(AccessControl(TOKEN), REMOTE, host="10.0.0.1:8000") as c:
            return (await c.post("/login", data={"token": TOKEN}, headers={"origin": "http://evil.example"})).status_code
    assert run(go()) == 403


def test_five_failures_lock_the_ip_for_15_minutes_even_for_the_right_token():
    clock = Clock()
    control = AccessControl(TOKEN, clock=clock)

    async def go():
        async with _client(control, REMOTE, host="10.0.0.1:8000") as c:
            codes = [(await c.post("/login", data={"token": "x" * 24})).status_code for _ in range(MAX_FAILURES)]
            locked = await c.post("/login", data={"token": TOKEN})
            clock.now += LOCKOUT_SECONDS + 1
            unlocked = await c.post("/login", data={"token": TOKEN})
            return codes, locked.status_code, unlocked.status_code
    codes, locked, unlocked = run(go())
    assert codes == [401] * MAX_FAILURES
    assert locked == 429
    assert unlocked == 303


def test_tampered_expired_and_rotated_cookies_are_refused():
    clock = Clock()
    control = AccessControl(TOKEN, clock=clock)
    cookie = control.issue_cookie()
    assert control.cookie_valid(cookie)
    head, sig = cookie.rsplit(".", 1)
    assert not control.cookie_valid(f"{head}.{'0' * len(sig)}")
    assert not control.cookie_valid("v1.9999999999." + sig)
    clock.now += SESSION_SECONDS + 1
    assert not control.cookie_valid(cookie), "7일이 지나면 다시 로그인"
    clock.now -= SESSION_SECONDS + 1
    control.apply(generate_token(), source="새로 생성")
    assert not control.cookie_valid(cookie), "토큰을 바꾸면 이전 로그인은 모두 무효"


def test_logout_clears_the_cookie():
    async def go():
        async with _client(AccessControl(TOKEN), REMOTE, host="10.0.0.1:8000") as c:
            return await c.get("/logout")
    r = run(go())
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert f"{COOKIE_NAME}=;" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]


def test_an_oversized_login_body_is_a_failure_not_a_crash():
    async def go():
        async with _client(AccessControl(TOKEN), REMOTE, host="10.0.0.1:8000") as c:
            return (await c.post("/login", content=b"token=" + b"a" * 10000,
                                 headers={"content-type": "application/x-www-form-urlencoded"})).status_code
    assert run(go()) == 401


# ------------------------------------------------------------------ 토큰 갱신 (루프백 버튼)


from app.ui.components.access_token import apply_env_token, generate_and_store_token  # noqa: E402


def test_applying_an_invalid_env_token_keeps_the_current_one(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{TOKEN_ENV}=short\n", encoding="utf-8")
    control = AccessControl(TOKEN)
    cookie = control.issue_cookie()
    with pytest.raises(ValueError):
        apply_env_token(control, env)
    assert control.status.enabled and control.cookie_valid(cookie), "잘못 누른 한 번으로 주인이 잠기면 안 됩니다"


def test_applying_the_env_token_switches_and_invalidates_logins(tmp_path):
    env = tmp_path / ".env"
    new = "0" * 24
    env.write_text(f"{TOKEN_ENV}={new}\n", encoding="utf-8")
    control = AccessControl(TOKEN)
    cookie = control.issue_cookie()
    assert apply_env_token(control, env) == new
    assert control.check_token(new) and not control.check_token(TOKEN)
    assert not control.cookie_valid(cookie)


def test_generation_saves_first_and_applies_only_on_success(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    control = AccessControl(TOKEN)
    token = generate_and_store_token(control, env)
    assert read_env_token(env) == token and control.check_token(token)

    import app.ui.components.access_token as module

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(module, "write_env_token", boom)
    with pytest.raises(OSError):
        generate_and_store_token(control, env)
    assert control.check_token(token), "저장에 실패하면 메모리의 토큰도 바꾸지 않습니다"


def test_the_middleware_is_the_outermost_layer_and_buttons_are_wired():
    from app.main import server

    assert server.user_middleware[0].cls is AccessMiddleware
    app_src = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
    assert app_src.index('info_btn = ui.button(icon="info"') < app_src.index("build_access_buttons()")
    button_src = io.open(ROOT / "app" / "ui" / "components" / "access_token.py", encoding="utf-8").read()
    assert button_src.count("if not _caller_is_loopback():") == 4, "버튼 생성과 세 처리 함수(적용·생성·잠금 해제) 모두 루프백을 확인"


# ------------------------------------------------------------------ 하위 호환: 토큰 없이 외부에 연 서버


from app.ui.components.access_token import bind_is_public, bootstrap_missing_token  # noqa: E402


@pytest.mark.parametrize("host,public", [
    ("0.0.0.0", True), ("::", True), ("", True), ("192.168.0.10", True), ("[::]", True),
    ("127.0.0.1", False), ("localhost", False), ("::1", False), ("127.0.1.1", False),
])
def test_which_binds_count_as_public(host, public):
    assert bind_is_public(host) is public


@pytest.fixture
def no_os_token(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)


@pytest.mark.parametrize("env_text", [None, "LLM_API_KEY=keep\n", f"LLM_API_KEY=keep\n{TOKEN_ENV}=\n"])
def test_a_public_server_without_a_token_starts_with_a_new_one_saved_to_env(tmp_path, no_os_token, env_text):
    env = tmp_path / ".env"
    if env_text is not None:
        env.write_text(env_text, encoding="utf-8")
    control = AccessControl(None)

    token = bootstrap_missing_token(control, env, "0.0.0.0")

    assert token and token_problem(token) is None
    assert control.status.enabled and control.check_token(token)
    assert read_env_token(env) == token
    if env_text:
        assert "LLM_API_KEY=keep" in env.read_text(encoding="utf-8")
    assert bootstrap_missing_token(control, env, "0.0.0.0") is None, "다음 화면부터는 다시 만들지 않습니다"


def test_no_token_is_created_for_a_loopback_server(tmp_path, no_os_token):
    env = tmp_path / ".env"
    control = AccessControl(None)
    assert bootstrap_missing_token(control, env, "127.0.0.1") is None
    assert not env.exists() and not control.status.enabled


def test_a_malformed_token_written_by_the_owner_is_not_overwritten(tmp_path, no_os_token):
    env = tmp_path / ".env"
    env.write_text(f"{TOKEN_ENV}=mine-too-short\n", encoding="utf-8")
    control = AccessControl("mine-too-short")
    assert bootstrap_missing_token(control, env, "0.0.0.0") is None
    assert env.read_text(encoding="utf-8") == f"{TOKEN_ENV}=mine-too-short\n"
    assert not control.status.enabled


def test_an_existing_valid_token_or_os_variable_is_left_alone(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert bootstrap_missing_token(AccessControl(TOKEN), env, "0.0.0.0") is None
    monkeypatch.setenv(TOKEN_ENV, "set-by-the-os")
    assert bootstrap_missing_token(AccessControl(None), env, "0.0.0.0") is None
    assert not env.exists()


def test_concurrent_first_pages_create_only_one_token(tmp_path, no_os_token):
    import threading

    env = tmp_path / ".env"
    control = AccessControl(None)
    results = []
    threads = [threading.Thread(target=lambda: results.append(bootstrap_missing_token(control, env, "0.0.0.0")))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    created = [r for r in results if r]
    assert len(created) == 1 and read_env_token(env) == created[0]


def test_a_failed_save_leaves_remote_access_closed(tmp_path, no_os_token, monkeypatch):
    import app.ui.components.access_token as module

    def boom(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(module, "write_env_token", boom)
    control = AccessControl(None)
    with pytest.raises(OSError):
        bootstrap_missing_token(control, tmp_path / ".env", "0.0.0.0")
    assert not control.status.enabled


def test_the_popup_message_is_the_one_the_owner_asked_for():
    src = io.open(ROOT / "app" / "ui" / "components" / "access_token.py", encoding="utf-8").read()
    assert 'f"외부 유저 인증 토큰이 없어 새 토큰(`{token}`)으로 서버를 시작했습니다. `.env`에 저장하였습니다."' in src


# ------------------------------------------------------------------ 잠긴 IP 해제 (루프백 관리자)


def test_locked_ips_are_listed_with_remaining_time_and_expire_from_the_list():
    clock = Clock()
    control = AccessControl(TOKEN, clock=clock)
    for _ in range(MAX_FAILURES):
        control.record_failure("10.0.0.5")
    clock.now += 60
    for _ in range(MAX_FAILURES):
        control.record_failure("10.0.0.6")
    locked = control.locked_ips()
    assert [ip for ip, _ in locked] == ["10.0.0.6", "10.0.0.5"], "남은 시간이 긴 것부터"
    assert locked[1][1] == pytest.approx(LOCKOUT_SECONDS - 60)
    clock.now += LOCKOUT_SECONDS
    assert control.locked_ips() == []


def test_unlocking_lets_the_right_token_in_immediately_and_resets_the_count():
    clock = Clock()
    control = AccessControl(TOKEN, clock=clock)

    async def go():
        async with _client(control, REMOTE, host="10.0.0.1:8000") as c:
            for _ in range(MAX_FAILURES):
                await c.post("/login", data={"token": "x" * 24})
            locked = (await c.post("/login", data={"token": TOKEN})).status_code
            assert control.unlock(REMOTE[0]) is True
            assert control.unlock(REMOTE[0]) is False, "이미 풀린 IP"
            # 실패 횟수도 비웠으므로 한 번 더 틀려도 곧바로 잠기지 않습니다.
            once_more = (await c.post("/login", data={"token": "x" * 24})).status_code
            ok = await c.post("/login", data={"token": TOKEN})
            return locked, once_more, ok.status_code
    assert run(go()) == (429, 401, 303)
    assert control.locked_ips() == []


def test_unlock_is_loopback_only_in_the_ui():
    src = io.open(ROOT / "app" / "ui" / "components" / "access_token.py", encoding="utf-8").read()
    handler = src[src.index("def unlock_ips("):src.index("def render_locks(")]
    assert "if not _caller_is_loopback():" in handler.split("control.unlock")[0]


# ------------------------------------------------------------------ 잠금 감사 기록


import json  # noqa: E402

from app.security import AuditLog, get_access_control  # noqa: E402


def test_lockouts_and_unlocks_are_appended_to_the_audit_log(tmp_path):
    clock = Clock()
    audit = AuditLog(tmp_path / "security" / "login_audit.jsonl", clock=clock)
    control = AccessControl(TOKEN, clock=clock, audit=audit)

    async def go():
        async with _client(control, REMOTE, host="10.0.0.1:8000") as c:
            for _ in range(MAX_FAILURES):
                clock.now += 10
                await c.post("/login", data={"token": "Guess0000000000000000000"},
                             headers={"user-agent": "attacker-bot/1.0\n{\"event\":\"forged\"}"})
    run(go())
    clock.now += 30
    control.unlock(REMOTE[0])
    control.unlock(REMOTE[0])  # 이미 풀림 → 기록하지 않음

    lines = (tmp_path / "security" / "login_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "줄바꿈이 든 User-Agent 가 기록을 늘리거나 위조하면 안 됩니다"
    lockout, unlock = (json.loads(line) for line in lines)
    assert lockout["event"] == "lockout" and lockout["ip"] == REMOTE[0]
    assert lockout["failures"] == MAX_FAILURES
    assert lockout["last_failure_ts"] - lockout["first_failure_ts"] == pytest.approx(40)
    assert lockout["locked_until_ts"] == pytest.approx(lockout["last_failure_ts"] + LOCKOUT_SECONDS)
    assert lockout["user_agent"].startswith("attacker-bot/1.0\n")
    assert "Guess" not in "\n".join(lines), "입력한 토큰은 절대 기록하지 않습니다"
    assert unlock["event"] == "unlock" and unlock["by"] == "loopback"
    assert unlock["remaining_seconds"] == pytest.approx(LOCKOUT_SECONDS - 30)
    assert [r["event"] for r in audit.read()] == ["unlock", "lockout"], "최근 것부터"


def test_failures_below_the_threshold_are_not_logged(tmp_path):
    audit = AuditLog(tmp_path / "a.jsonl")
    control = AccessControl(TOKEN, audit=audit)
    for _ in range(MAX_FAILURES - 1):
        control.record_failure("10.0.0.9")
    assert not (tmp_path / "a.jsonl").exists()


def test_the_audit_log_rotates_and_survives_broken_lines(tmp_path):
    path = tmp_path / "a.jsonl"
    audit = AuditLog(path, max_bytes=400)
    for i in range(10):
        audit.write("lockout", f"10.0.0.{i}", failures=5)
    assert path.with_name("a.jsonl.1").exists()
    assert path.stat().st_size <= 400
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert audit.read()[0]["ip"] == "10.0.0.9"


def test_an_unwritable_audit_log_does_not_break_login(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    control = AccessControl(TOKEN, audit=AuditLog(blocker / "sub" / "a.jsonl"))
    for _ in range(MAX_FAILURES):
        control.record_failure("10.0.0.7")
    assert control.locked_for("10.0.0.7") > 0


def test_the_app_writes_its_audit_log_under_data_security(monkeypatch):
    import app.security as security

    monkeypatch.setattr(security, "_control", None)
    control = get_access_control()
    from app.config import DATA_DIR

    assert control.audit is not None
    assert control.audit._target() == DATA_DIR / "security" / "login_audit.jsonl"
    monkeypatch.setattr(security, "_control", None)
