"""체험 방문자의 이름 + PIN 로그인 (app/trial/auth.py, app/trial/web.py).

1. 처음 보는 이름은 PIN 을 한 번 더 받아 확인한 뒤에만 등록된다.
2. PIN 은 해시로만 남고, 연달아 틀리면 그 이름이 잠긴다.
3. 쿠키는 서명·만료·PIN 지문으로 검사한다 — PIN 을 초기화하거나 바꾸면 예전 로그인이 모두 풀린다.
4. 로그인 폼은 쿠키를 심고, 돌아갈 곳은 체험 화면 안쪽만 받는다.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from types import SimpleNamespace

from app.config import TrialConfig
from app.database.models import Base
from app.trial import auth, web
from app.trial import models as trial_models  # noqa: F401 - 테이블 등록
from app.trial.auth import (
    MAX_PIN_FAILURES,
    TRIAL_COOKIE,
    IpThrottle,
    TrialCookieSigner,
    clean_name,
    hash_pin,
    login_or_register,
    name_key,
    name_problem,
    pin_stamp,
    user_from_cookie,
    verify_pin,
)
from app.trial.store import reset_pin


@pytest_asyncio.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _signer(monkeypatch):
    monkeypatch.setattr(auth, "_signer", TrialCookieSigner(b"k" * 64))


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------ 이름과 PIN


def test_names_are_trimmed_and_compared_without_case_or_spacing():
    assert clean_name("  홍  길동 ") == "홍 길동"
    assert name_key("Kim  MinSu") == name_key("kim minsu")
    assert name_problem("") and name_problem("a" * 41) and name_problem("탭\t이름")
    assert name_problem("20251234") is None


def test_pins_are_stored_only_as_salted_scrypt_hashes():
    first, second = hash_pin("1234"), hash_pin("1234")
    assert "1234" not in first
    assert first != second, "같은 PIN 이라도 소금이 달라야 합니다"
    assert verify_pin("1234", first) and not verify_pin("1235", first)
    assert not verify_pin("1234", "garbage") and not verify_pin("1234", "")


# ------------------------------------------------------------------ 등록과 로그인


async def _login(factory, name, pin, confirm=None, **kw):
    async with factory() as db:
        return await login_or_register(db, name, pin, confirm, min_pin_length=4, **kw)


@pytest.mark.asyncio
async def test_a_new_name_is_registered_only_after_the_pin_is_confirmed(db_factory):
    assert (await _login(db_factory, "홍길동", "1234")).status == "confirm"
    assert (await _login(db_factory, "홍길동", "1234", "1243")).status == "mismatch"
    async with db_factory() as db:
        assert await auth.find_user(db, "홍길동") is None, "확인 전에는 계정이 생기면 안 됩니다"
    outcome = await _login(db_factory, "홍길동", "1234", "1234")
    assert outcome.status == "ok" and outcome.user.name == "홍길동"
    # 다음부터는 확인 없이 들어오고, 공백·대소문자가 달라도 같은 사람입니다.
    assert (await _login(db_factory, " 홍길동 ", "1234")).status == "ok"


@pytest.mark.asyncio
async def test_short_pins_are_refused_before_registration(db_factory):
    outcome = await _login(db_factory, "kim", "12", "12")
    assert outcome.status == "invalid" and "4자" in outcome.message


@pytest.mark.asyncio
async def test_repeated_wrong_pins_lock_the_name_even_for_the_right_pin(db_factory):
    await _login(db_factory, "lee", "0000", "0000")
    now = datetime.now(timezone.utc)
    for _ in range(MAX_PIN_FAILURES - 1):
        assert (await _login(db_factory, "lee", "9999", now=now)).status == "wrong"
    assert (await _login(db_factory, "lee", "9999", now=now)).status == "locked"
    assert (await _login(db_factory, "lee", "0000", now=now)).status == "locked"
    later = now + timedelta(seconds=auth.PIN_LOCK_SECONDS + 1)
    assert (await _login(db_factory, "lee", "0000", now=later)).status == "ok"


@pytest.mark.asyncio
async def test_a_reset_pin_is_set_again_on_the_next_login(db_factory):
    user = (await _login(db_factory, "park", "1111", "1111")).user
    async with db_factory() as db:
        await reset_pin(db, user.id)
    assert (await _login(db_factory, "park", "2222")).status == "confirm"
    assert (await _login(db_factory, "park", "2222", "2222")).status == "ok"
    assert (await _login(db_factory, "park", "1111")).status == "wrong"


# ------------------------------------------------------------------ 쿠키


def test_cookies_are_signed_and_expire():
    clock = Clock()
    signer = TrialCookieSigner(b"s" * 64, clock=clock, lifetime=100)
    value = signer.issue("user-1", "stamp")
    assert signer.verify(value) == ("user-1", "stamp")
    assert signer.verify(value.replace("user-1", "user-2")) is None, "id 를 바꾸면 서명이 맞지 않습니다"
    assert TrialCookieSigner(b"t" * 64, clock=clock).verify(value) is None, "다른 키로 서명한 쿠키"
    clock.now += 101
    assert signer.verify(value) is None
    assert signer.verify("") is None and signer.verify("a.b") is None


@pytest.mark.asyncio
async def test_resetting_or_changing_the_pin_signs_every_old_login_out(db_factory):
    user = (await _login(db_factory, "choi", "1234", "1234")).user
    old = auth.issue_cookie(user)
    async with db_factory() as db:
        assert (await user_from_cookie(db, old)).id == user.id
        await reset_pin(db, user.id)
        assert await user_from_cookie(db, old) is None, "초기화한 순간 끊겨야 합니다"
    await _login(db_factory, "choi", "5678", "5678")
    async with db_factory() as db:
        assert await user_from_cookie(db, old) is None, "새 PIN 을 정한 뒤에도 옛 쿠키가 살아나면 안 됩니다"


def test_the_pin_stamp_changes_with_the_hash():
    assert pin_stamp(hash_pin("1234")) != pin_stamp(hash_pin("1234"))


def test_the_ip_throttle_blocks_within_the_window_only():
    clock = Clock()
    throttle = IpThrottle(limit=3, window=60, clock=clock)
    for _ in range(3):
        throttle.fail("10.0.0.9")
    assert throttle.blocked("10.0.0.9") and not throttle.blocked("10.0.0.8")
    clock.now += 61
    assert not throttle.blocked("10.0.0.9")


# ------------------------------------------------------------------ 로그인 폼


@pytest_asyncio.fixture
async def login_app(db_factory, monkeypatch):
    cfg = SimpleNamespace(trial=TrialConfig(enabled=True))
    monkeypatch.setattr(web, "get_config", lambda: cfg)
    monkeypatch.setattr(web, "get_session_factory", lambda: db_factory)
    monkeypatch.setattr(auth, "_ip_throttle", IpThrottle())
    app = FastAPI()
    web.register_trial_routes(app)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://mado.corp", follow_redirects=False) as client:
        yield client, cfg


@pytest.mark.asyncio
async def test_the_login_form_registers_sets_a_cookie_and_returns_to_the_trial(login_app):
    client, _ = login_app
    page = await client.get("/trial/login")
    assert page.status_code == 200 and 'name="pin"' in page.text and "pin_confirm" not in page.text

    first = await client.post("/trial/login", data={"name": "정다은", "pin": "4321", "next": "/trial/s/abc"})
    assert first.status_code == 200 and 'name="pin_confirm"' in first.text
    assert "정다은" in first.text, "확인 단계에서 이름을 다시 적게 하면 안 됩니다"

    done = await client.post("/trial/login", data={
        "name": "정다은", "pin": "4321", "pin_confirm": "4321", "next": "/trial/s/abc",
    })
    assert done.status_code == 303 and done.headers["location"] == "/trial/s/abc"
    cookie = done.headers["set-cookie"]
    assert cookie.startswith(f"{TRIAL_COOKIE}=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie

    client.cookies.set(TRIAL_COOKIE, cookie.split(";")[0].split("=", 1)[1])
    again = await client.get("/trial/login")
    assert again.status_code == 303 and again.headers["location"] == "/trial"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/api/agents", "/trial\\..\\x"])
async def test_the_login_never_sends_the_visitor_outside_the_trial(login_app, target):
    client, _ = login_app
    await client.post("/trial/login", data={"name": "a1", "pin": "1234", "pin_confirm": "1234"})
    done = await client.post("/trial/login", data={"name": "a1", "pin": "1234", "next": target})
    assert done.headers["location"] == "/trial"


@pytest.mark.asyncio
async def test_a_wrong_pin_is_refused_and_counted(login_app):
    client, _ = login_app
    await client.post("/trial/login", data={"name": "b2", "pin": "1234", "pin_confirm": "1234"})
    wrong = await client.post("/trial/login", data={"name": "b2", "pin": "0000"})
    assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
    assert "더 틀리면" in wrong.text


@pytest.mark.asyncio
async def test_the_login_is_closed_when_the_trial_is_off(login_app):
    client, cfg = login_app
    cfg.trial.enabled = False
    assert (await client.get("/trial/login")).status_code == 404
    assert (await client.post("/trial/login", data={"name": "x", "pin": "1234"})).status_code == 404


@pytest.mark.asyncio
async def test_logout_clears_the_cookie(login_app):
    client, _ = login_app
    out = await client.get("/trial/logout")
    assert out.status_code == 303 and out.headers["location"] == "/trial/login"
    assert "Max-Age=0" in out.headers["set-cookie"]


@pytest.mark.asyncio
async def test_an_oversized_login_body_is_refused(login_app):
    client, _ = login_app
    big = await client.post("/trial/login", data={"name": "x" * 5000, "pin": "1234"})
    assert big.status_code == 413
