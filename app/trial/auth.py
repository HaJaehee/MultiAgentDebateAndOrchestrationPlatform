"""체험 방문자의 이름 + PIN 로그인.

## 규칙

* 처음 보는 이름이면 **그 자리에서 등록**합니다. 잘못 친 이름으로 계정이 생기지 않게, 새 이름은
  PIN 을 한 번 더 받아 확인합니다.
* PIN 은 scrypt 로 해시해 둡니다. 원문은 어디에도 남지 않습니다.
* 같은 이름으로 연달아 틀리면 그 이름을 잠급니다. 한 접속 주소가 여러 이름을 돌아가며
  맞춰 보는 것은 주소 단위로 따로 셉니다.
* 운영자가 PIN 을 초기화하면(`pin_hash` 가 빈 값) 다음 로그인에서 새 PIN 을 정합니다.
* 로그인은 서명한 쿠키(`mado_trial`)로 7일 유지됩니다. 쿠키에는 방문자 id 와 발급 시각만
  들어가고, 서명 키는 `data/trial/secret.key` 에 처음 한 번 만들어 둡니다.

주인 토큰(`app/security.py`)과는 서로 모릅니다. 방문자 쿠키로는 주인 화면에 들어갈 수 없고,
주인이라도 체험 화면을 쓰려면 방문자로 로그인합니다 (자기 대화만 보이는 화면이니까요).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.trial.models import TrialUserModel

TRIAL_COOKIE = "mado_trial"
LOGIN_SECONDS = 7 * 24 * 60 * 60
CLOCK_SKEW_SECONDS = 300

MAX_NAME_LENGTH = 40
MAX_PIN_LENGTH = 64

# 이름 하나에 대한 연속 실패. 넘으면 그 이름을 잠급니다.
MAX_PIN_FAILURES = 5
PIN_LOCK_SECONDS = 15 * 60
# 접속 주소 하나에 대한 실패 (여러 이름을 돌아가며 맞춰 보는 경우).
MAX_IP_FAILURES = 20
IP_WINDOW_SECONDS = 15 * 60

# scrypt 비용. 한 번에 약 16MB·수십 ms — 로그인에는 충분히 싸고, 대량 대입에는 비쌉니다.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

SECRET_PATH = DATA_DIR / "trial" / "secret.key"


# ---------------------------------------------------------------------------
# 이름과 PIN
# ---------------------------------------------------------------------------


def clean_name(raw: Optional[str]) -> str:
    """앞뒤 공백을 걷고, 안쪽 공백은 하나로 모읍니다. 호환 문자는 정규화합니다."""
    text = unicodedata.normalize("NFKC", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def name_key(name: str) -> str:
    """같은 사람인지 가리는 열쇠. 대소문자와 공백 차이를 무시합니다."""
    return clean_name(name).casefold()


def name_problem(name: str) -> Optional[str]:
    if not name:
        return "이름을 적어 주세요."
    if len(name) > MAX_NAME_LENGTH:
        return f"이름은 {MAX_NAME_LENGTH}자까지 적을 수 있습니다."
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return "이름에 쓸 수 없는 문자가 들어 있습니다."
    return None


def pin_problem(pin: str, min_length: int) -> Optional[str]:
    if len(pin) < min_length:
        return f"PIN 은 {min_length}자 이상이어야 합니다."
    if len(pin) > MAX_PIN_LENGTH:
        return f"PIN 은 {MAX_PIN_LENGTH}자까지 쓸 수 있습니다."
    return None


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            pin.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---------------------------------------------------------------------------
# 쿠키
# ---------------------------------------------------------------------------

_secret_lock = threading.Lock()


def load_or_create_secret(path: Path = SECRET_PATH) -> bytes:
    """서명 키. 없으면 만들어 둡니다. 지우면 모든 방문자의 로그인이 풀립니다."""
    with _secret_lock:
        try:
            data = path.read_bytes().strip()
            if len(data) >= 32:
                return data
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_hex(32).encode("ascii")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return data


def pin_stamp(pin_hash: str) -> str:
    """PIN 해시의 짧은 지문. 쿠키에 넣어 두면 PIN 이 바뀐 순간 예전 로그인이 모두 풀립니다."""
    return hashlib.sha256((pin_hash or "").encode("utf-8")).hexdigest()[:12]


class TrialCookieSigner:
    """`<방문자 id>.<PIN 지문>.<발급 시각>.<서명>` 을 만들고 검사합니다."""

    def __init__(self, secret: bytes, *, clock: Callable[[], float] = time.time,
                 lifetime: int = LOGIN_SECONDS):
        self._secret = secret
        self._clock = clock
        self.lifetime = lifetime

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def issue(self, user_id: str, stamp: str) -> str:
        payload = f"{user_id}.{stamp}.{int(self._clock())}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, value: Optional[str]) -> Optional[tuple]:
        """맞고 아직 유효하면 `(방문자 id, PIN 지문)`, 아니면 None."""
        if not value or value.count(".") != 3:
            return None
        user_id, stamp, issued_text, signature = value.split(".")
        if not hmac.compare_digest(signature, self._sign(f"{user_id}.{stamp}.{issued_text}")):
            return None
        try:
            issued = int(issued_text)
        except ValueError:
            return None
        now = self._clock()
        if issued > now + CLOCK_SKEW_SECONDS or now - issued > self.lifetime:
            return None
        return (user_id, stamp) if user_id else None


_signer: Optional[TrialCookieSigner] = None


def get_signer() -> TrialCookieSigner:
    global _signer
    if _signer is None:
        _signer = TrialCookieSigner(load_or_create_secret())
    return _signer


def cookie_header(value: str) -> str:
    return f"{TRIAL_COOKIE}={value}; Path=/; Max-Age={LOGIN_SECONDS}; HttpOnly; SameSite=Strict"


def clear_cookie_header() -> str:
    return f"{TRIAL_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"


# ---------------------------------------------------------------------------
# 접속 주소 단위 제한
# ---------------------------------------------------------------------------


class IpThrottle:
    """한 주소에서 최근 창 안에 틀린 횟수. 넘으면 창이 지날 때까지 받지 않습니다."""

    def __init__(self, *, limit: int = MAX_IP_FAILURES, window: float = IP_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._failures: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, ip: str) -> Deque[float]:
        bucket = self._failures.setdefault(ip, deque())
        cutoff = self._clock() - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._trim(ip)) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            self._trim(ip).append(self._clock())

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


_ip_throttle = IpThrottle()


def get_ip_throttle() -> IpThrottle:
    return _ip_throttle


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------


@dataclass
class LoginOutcome:
    """`status`: ok · confirm (새 이름, PIN 확인 필요) · mismatch · wrong · locked · invalid."""

    status: str
    message: str = ""
    user: Optional[TrialUserModel] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite 는 시간대를 버리고 돌려줍니다. UTC 로 다시 붙입니다."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def find_user(db: AsyncSession, name: str) -> Optional[TrialUserModel]:
    result = await db.execute(select(TrialUserModel).where(TrialUserModel.name_key == name_key(name)))
    return result.scalar_one_or_none()


async def login_or_register(
    db: AsyncSession,
    raw_name: str,
    pin: str,
    pin_confirm: Optional[str],
    *,
    min_pin_length: int,
    now: Optional[datetime] = None,
) -> LoginOutcome:
    """이름과 PIN 을 확인합니다. 처음 보는 이름이면 PIN 확인을 거쳐 등록합니다."""
    now = now or _now()
    name = clean_name(raw_name)
    problem = name_problem(name)
    if problem:
        return LoginOutcome("invalid", problem)

    user = await find_user(db, name)
    setting_pin = user is None or not user.pin_hash

    if user is not None:
        locked_until = _aware(user.locked_until)
        if locked_until and locked_until > now:
            minutes = int((locked_until - now).total_seconds() // 60) + 1
            return LoginOutcome("locked", f"PIN 을 여러 번 틀려 잠겼습니다. {minutes}분 뒤에 다시 시도하세요.")

    if setting_pin:
        problem = pin_problem(pin, min_pin_length)
        if problem:
            return LoginOutcome("invalid", problem)
        if pin_confirm is None or pin_confirm == "":
            what = "처음 오셨네요" if user is None else "PIN 이 초기화되었습니다"
            return LoginOutcome("confirm", f"{what}. 확인을 위해 PIN 을 한 번 더 입력하세요.")
        if pin_confirm != pin:
            return LoginOutcome("mismatch", "두 PIN 이 다릅니다. 다시 입력하세요.")
        digest = await asyncio.to_thread(hash_pin, pin)
        if user is None:
            user = TrialUserModel(name=name, name_key=name_key(name), pin_hash=digest)
            db.add(user)
        else:
            user.pin_hash = digest
        user.failed_count = 0
        user.locked_until = None
        user.last_login_at = now
        await db.commit()
        return LoginOutcome("ok", user=user)

    if await asyncio.to_thread(verify_pin, pin, user.pin_hash):
        user.failed_count = 0
        user.locked_until = None
        user.last_login_at = now
        await db.commit()
        return LoginOutcome("ok", user=user)

    user.failed_count = (user.failed_count or 0) + 1
    if user.failed_count >= MAX_PIN_FAILURES:
        user.failed_count = 0
        user.locked_until = now + timedelta(seconds=PIN_LOCK_SECONDS)
        await db.commit()
        return LoginOutcome("locked", f"PIN 을 {MAX_PIN_FAILURES}번 틀려 {PIN_LOCK_SECONDS // 60}분 동안 잠겼습니다.")
    await db.commit()
    left = MAX_PIN_FAILURES - user.failed_count
    return LoginOutcome("wrong", f"PIN 이 맞지 않습니다. {left}번 더 틀리면 잠깁니다.")


def issue_cookie(user: TrialUserModel) -> str:
    return get_signer().issue(user.id, pin_stamp(user.pin_hash))


async def user_from_cookie(db: AsyncSession, value: Optional[str]) -> Optional[TrialUserModel]:
    verified = get_signer().verify(value)
    if not verified:
        return None
    user_id, stamp = verified
    user = await db.get(TrialUserModel, user_id)
    # PIN 을 초기화했거나 바꾼 사람의 옛 로그인은 끊습니다 (초기화는 대개 "남이 쓰고 있다" 는 뜻입니다).
    if user is None or not user.pin_hash or not hmac.compare_digest(stamp, pin_stamp(user.pin_hash)):
        return None
    return user
