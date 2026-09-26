"""원격 접속 토큰 — 토큰을 아는 주인만 다른 PC 에서 MADO 를 씁니다.

MADO 는 인증이 없었습니다. `APP_HOST=0.0.0.0` 으로 열면 같은 망의 누구나 대화를 엿보고,
MCP 서버 실행 명령을 바꾸고, 샌드박스로 코드를 실행하고, 작업 공간 파일을 받을 수 있었습니다.

## 규칙

* **루프백(127.0.0.1, ::1)은 토큰 없이** 들어옵니다. 서버 PC 앞의 사람은 이미 그 PC 의 주인입니다.
* **원격은 토큰으로 로그인해야** 합니다. 로그인은 7일 유지됩니다.
* 토큰이 없거나 형식이 틀리면 **원격은 전부 거부**합니다. 토큰을 깜빡하고 외부에 열었다고
  문이 열리면 안 됩니다.
* 토큰은 `.env` 의 `MADO_ACCESS_TOKEN` 입니다. 영문 대소문자·숫자 **정확히 24자**. 글자 종류와
  길이만 검사하고 강도는 검사하지 않습니다 (`0` 24개도 받습니다 — 주인의 결정).

## 막는 자리

모든 요청이 거치는 **맨 바깥 ASGI 계층** 하나(`AccessMiddleware`)입니다. NiceGUI 는 FastAPI 앱
`server` 안에 붙어 있고, 화면을 움직이는 socket.io 도 그 안에 붙어 있어서, 페이지·웹소켓·
`/api/*`·`/agent-icon`·작업 공간 다운로드가 전부 여기를 지납니다.

* 토큰은 **POST 본문으로만** 받습니다. URL 에 실으면 주소창 기록·서버 로그·Referer 에 남습니다.
* 쿠키에는 토큰이 아니라 **토큰에서 파생한 키로 서명한 발급 시각**이 들어갑니다. 토큰을 바꾸면
  이전 쿠키는 모두 무효가 됩니다. `HttpOnly`, `SameSite=Strict`.
* 비교는 `hmac.compare_digest` (비교 시간으로 토큰을 알아낼 수 없게).
* 같은 IP 에서 15분 안에 5번 틀리면 15분 잠급니다.
* **루프백에도 Host·Origin 을 확인합니다.** 서버 PC 의 브라우저로 연 악성 웹페이지가
  `http://127.0.0.1:8000` 을 부르거나(교차 출처), DNS 리바인딩으로 `evil.example` 을 127.0.0.1 로
  돌려 루프백 권한을 빌려 쓰는 것을 막습니다. socket.io 는 모든 출처를 허용하도록 설정돼 있어
  (`cors_allowed_origins='*'`) 여기서 막아야 합니다.

## 한계

* HTTPS 가 없으면 같은 망에서 트래픽을 볼 수 있는 사람이 로그인 순간의 토큰과 쿠키를 가로챌 수
  있습니다 (주인의 결정: 지금은 HTTPS 를 쓰지 않음). 암호화가 필요하면 SSH 터널로 붙으세요 —
  터널로 들어온 접속은 루프백입니다.
* **리버스 프록시 뒤에 두면 안 됩니다.** 모든 접속이 프록시의 루프백 주소로 보여 전원이 주인이
  됩니다. `X-Forwarded-For` 는 믿지 않습니다.
* 토큰 하나 = 주인 하나입니다. 토큰을 나눠 주면 모든 대화를 나눠 주는 것입니다.

## 방문자 (체험 서버)

`guest_gate` 를 주고 그것이 켜져 있으면, 토큰이 없는 원격 접속은 거부 대신 **방문자**가
됩니다. 방문자는 문지기가 허락한 경로(체험 화면과 그 화면이 쓰는 NiceGUI 자원)만 지나고,
`/` 는 체험 첫 화면으로 돌려보내며, 그 밖(주인 화면·`/api/*`·작업 공간 다운로드)은 막습니다.
방문자가 누구인지는 여기서 가리지 않습니다 — 그건 체험 화면의 로그인(`app/trial/auth.py`)이
합니다. 여기서는 요청마다 `scope["state"]["mado_viewer"]` 에 주인/방문자를 적어, 화면이
주인만 볼 것을 가릴 수 있게 합니다.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import string
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Protocol, Tuple
from urllib.parse import parse_qs, urlsplit

TOKEN_ENV = "MADO_ACCESS_TOKEN"
TOKEN_LENGTH = 24
TOKEN_ALPHABET = string.ascii_letters + string.digits
_TOKEN_RE = re.compile(rf"^[A-Za-z0-9]{{{TOKEN_LENGTH}}}$")

COOKIE_NAME = "mado_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
# 시계가 조금 어긋난 쿠키를 받아 줄 여유(초).
CLOCK_SKEW_SECONDS = 300

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60

LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
MAX_LOGIN_BODY = 4096

# 루프백 요청에 받는 Host 이름. 그 밖의 이름으로 루프백에 오면 DNS 리바인딩으로 봅니다.
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# 추가로 받을 Host 이름 (쉼표 구분). 서버 PC 에서 자기 LAN 이름으로 열어 루프백이 되는 경우 등.
ALLOWED_HOSTS_ENV = "MADO_ALLOWED_HOSTS"

# 요청을 보낸 쪽이 주인인지 방문자인지 (`scope["state"]` 의 키와 값).
VIEWER_STATE_KEY = "mado_viewer"
VIEWER_OWNER = "owner"
VIEWER_GUEST = "guest"


class GuestGate(Protocol):
    """토큰 없는 원격 접속을 방문자로 받을지 정하는 문지기 (`app/trial/gate.py`)."""

    home_path: str

    def enabled(self) -> bool: ...

    def allows(self, path: str) -> bool: ...


def _mark_viewer(scope: Dict[str, Any], role: str) -> None:
    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    state[VIEWER_STATE_KEY] = role


def viewer_role(scope: Any) -> str:
    """이 요청을 보낸 쪽. 미들웨어를 거치지 않은 요청은 방문자로 봅니다 (좁은 쪽)."""
    state = (scope or {}).get("state") if isinstance(scope, dict) else None
    role = state.get(VIEWER_STATE_KEY) if isinstance(state, dict) else None
    return role if role in (VIEWER_OWNER, VIEWER_GUEST) else VIEWER_GUEST


# ---------------------------------------------------------------------------
# 토큰
# ---------------------------------------------------------------------------


def generate_token() -> str:
    """영문 대소문자·숫자 24자. 운영체제의 보안 난수(`secrets`)로 만듭니다."""
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def token_problem(token: Optional[str]) -> Optional[str]:
    """토큰을 쓸 수 없는 이유. 쓸 수 있으면 None. 강도는 보지 않습니다."""
    if token is None or token == "":
        return f".env 에 {TOKEN_ENV} 가 없습니다"
    if len(token) != TOKEN_LENGTH:
        return f"토큰은 정확히 {TOKEN_LENGTH}자여야 합니다 (지금 {len(token)}자)"
    if not _TOKEN_RE.match(token):
        return "토큰에는 영문 대소문자와 숫자만 쓸 수 있습니다"
    return None


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

_ENV_LINE_RE = re.compile(rf"^\s*(?:export\s+)?{TOKEN_ENV}\s*=")


def read_env_token(env_path: Path) -> Optional[str]:
    """`.env` 파일에서 토큰을 **직접** 읽습니다. 없으면 None.

    앱은 시작할 때 `load_dotenv()` 로 한 번 환경변수에 넣으므로, 실행 중에 바뀐 `.env` 는
    `os.environ` 에 없습니다. 따옴표로 감싼 값도 받습니다.
    """
    from dotenv import dotenv_values

    if not Path(env_path).is_file():
        return None
    value = dotenv_values(env_path).get(TOKEN_ENV)
    return value.strip() if isinstance(value, str) else None


def write_env_token(env_path: Path, token: str) -> None:
    """`.env` 의 토큰 줄만 바꾸거나 붙입니다. 다른 줄·주석·줄바꿈 방식은 그대로 둡니다.

    임시 파일에 다 쓴 뒤 바꿔 끼웁니다(`os.replace`). 쓰는 도중에 멈춰도 `.env` 가 반쯤 남지
    않습니다 — `.env` 에는 LLM API 키도 들어 있습니다.
    """
    problem = token_problem(token)
    if problem:
        raise ValueError(problem)
    env_path = Path(env_path)
    # 바이트로 읽습니다. 텍스트로 읽으면 CRLF 가 LF 로 바뀌어 줄바꿈 방식을 알 수 없습니다.
    text = env_path.read_bytes().decode("utf-8") if env_path.is_file() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    # `splitlines()` 는 외톨이 `\r` 도 줄 끝으로 봐 빈 줄을 만들어 냅니다. `\n` 으로만 나누고
    # 줄 끝의 `\r` 은 떼어 냅니다.
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    replaced = False
    out: List[str] = []
    for line in lines:
        if _ENV_LINE_RE.match(line):
            if not replaced:
                out.append(f"{TOKEN_ENV}={token}")
                replaced = True
            continue  # 같은 키가 여러 줄이면 하나만 남깁니다
        out.append(line)
    if not replaced:
        out.append(f"{TOKEN_ENV}={token}")
    body = newline.join(out) + newline

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(env_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        os.replace(tmp, env_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 접속 주소
# ---------------------------------------------------------------------------


def is_loopback(host: Optional[str]) -> bool:
    """접속한 주소가 이 PC 자신인가. `::ffff:127.0.0.1` 같은 IPv4 매핑도 받습니다."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def _hostname(host_header: str) -> str:
    """`Host` 값에서 이름만. `[::1]:8000` → `::1`, `localhost:8000` → `localhost`."""
    value = (host_header or "").strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def allowed_loopback_hosts() -> frozenset:
    extra = {h.strip().lower() for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if h.strip()}
    return LOOPBACK_HOSTNAMES | extra


def same_origin(origin: str, host_header: str) -> bool:
    """`Origin` 이 이 서버(요청의 `Host`)와 같은가. 스킴은 보지 않습니다 (HTTP 만 씁니다)."""
    if not origin or origin == "null":
        return False
    parts = urlsplit(origin)
    return bool(parts.netloc) and parts.netloc.lower() == (host_header or "").strip().lower()


# ---------------------------------------------------------------------------
# 상태: 적용된 토큰, 쿠키, 실패 잠금
# ---------------------------------------------------------------------------


# 감사 로그 파일이 이보다 커지면 `.1` 로 밀어 두고 새로 씁니다 (하나만 보관).
AUDIT_MAX_BYTES = 5 * 1024 * 1024
AUDIT_FILENAME = "login_audit.jsonl"
_MAX_USER_AGENT = 300


def default_audit_path() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "security" / AUDIT_FILENAME


class AuditLog:
    """로그인 잠금 감사 기록 — 한 줄에 사건 하나인 JSON Lines, 덧붙이기만 합니다.

    잠긴 IP 는 메모리에만 있어 재기동하거나 풀면 흔적이 사라졌습니다. 공격을 나중에 살펴볼 수
    있도록 **잠금**과 **해제**를 파일에 남깁니다. 위치는 앱 소유의 `data/security/` 라 git 과
    배포 번들에 들어가지 않습니다.

    기록에는 IP·시각·실패 횟수·첫/마지막 실패 시각·User-Agent 가 들어가고, **입력한 토큰은 절대
    남기지 않습니다.** 값은 JSON 으로 인코딩하므로 User-Agent 에 줄바꿈을 넣어 기록을 위조할 수
    없습니다. 파일을 쓰지 못해도 로그인 처리는 계속합니다 (경고만 남깁니다).
    """

    def __init__(self, path: Optional[Path] = None, *, clock: Callable[[], float] = time.time,
                 max_bytes: int = AUDIT_MAX_BYTES):
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def _target(self) -> Path:
        if self.path is None:
            self.path = default_audit_path()
        return self.path

    def write(self, event: str, ip: str, **fields: Any) -> None:
        import json
        import logging

        now = self._clock()
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
                  + time.strftime("%z", time.localtime(now)),
            "ts": round(now, 3),
            "event": event,
            "ip": ip,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            path = self._target()
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > self._max_bytes:
                    os.replace(path, path.with_name(path.name + ".1"))
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
        except OSError as exc:
            logging.getLogger(__name__).warning("Could not write the login audit log: %s", exc)

    def read(self, limit: int = 50) -> List[Dict[str, Any]]:
        """최근 기록부터. 깨진 줄은 건너뜁니다."""
        import json

        path = self._target()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for raw in reversed(lines):
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return out


@dataclass
class TokenStatus:
    enabled: bool
    reason: str = ""          # 원격이 막힌 이유 (enabled 가 False 일 때)
    source: str = ""          # "startup" | ".env 적용" | "새로 생성"
    applied_at: float = 0.0


class AccessControl:
    """지금 적용된 토큰과, 그것으로 서명한 쿠키를 다룹니다. 프로세스에 하나."""

    def __init__(self, token: Optional[str] = None, *, source: str = "startup",
                 clock: Callable[[], float] = time.time, audit: Optional[AuditLog] = None):
        self._clock = clock
        # 잠금·해제를 남기는 곳. 없으면 기록하지 않습니다 (테스트 등). 앱은 `get_access_control` 이 붙입니다.
        self.audit = audit
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._key: bytes = b""
        self._status = TokenStatus(enabled=False)
        self._failures: Dict[str, Deque[float]] = {}
        self._locked_until: Dict[str, float] = {}
        self.apply(token, source=source)

    # -- 토큰 -----------------------------------------------------------------

    def apply(self, token: Optional[str], *, source: str) -> TokenStatus:
        """토큰을 적용합니다. 형식이 틀리면 원격을 막은 상태가 됩니다. 이전 쿠키는 모두 무효."""
        problem = token_problem(token)
        with self._lock:
            if problem:
                self._token, self._key = None, b""
                self._status = TokenStatus(False, problem, source, self._clock())
            else:
                self._token = token
                self._key = hmac.new(token.encode(), b"mado-session-v1", hashlib.sha256).digest()
                self._status = TokenStatus(True, "", source, self._clock())
            return self._status

    @property
    def status(self) -> TokenStatus:
        return self._status

    def check_token(self, candidate: str) -> bool:
        token = self._token
        if token is None:
            return False
        return hmac.compare_digest((candidate or "").encode(), token.encode())

    # -- 쿠키 -----------------------------------------------------------------

    def issue_cookie(self) -> str:
        if not self._key:
            raise RuntimeError("토큰이 적용되지 않았습니다")
        issued = str(int(self._clock()))
        sig = hmac.new(self._key, f"v1.{issued}".encode(), hashlib.sha256).hexdigest()
        return f"v1.{issued}.{sig}"

    def cookie_valid(self, value: Optional[str]) -> bool:
        key = self._key
        if not key or not value:
            return False
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
            return False
        expected = hmac.new(key, f"v1.{parts[1]}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, parts[2]):
            return False
        age = self._clock() - int(parts[1])
        return -CLOCK_SKEW_SECONDS <= age <= SESSION_SECONDS

    # -- 실패 잠금 --------------------------------------------------------------

    def locked_for(self, ip: str) -> float:
        """이 IP 가 잠겨 있으면 남은 초, 아니면 0."""
        until = self._locked_until.get(ip, 0.0)
        remaining = until - self._clock()
        return remaining if remaining > 0 else 0.0

    def record_failure(self, ip: str, *, user_agent: str = "") -> None:
        now = self._clock()
        locked_record = None
        with self._lock:
            window = self._failures.setdefault(ip, deque())
            window.append(now)
            while window and now - window[0] > FAILURE_WINDOW_SECONDS:
                window.popleft()
            if len(window) >= MAX_FAILURES:
                self._locked_until[ip] = now + LOCKOUT_SECONDS
                locked_record = {
                    "failures": len(window),
                    "first_failure_ts": round(window[0], 3),
                    "last_failure_ts": round(now, 3),
                    "locked_until_ts": round(now + LOCKOUT_SECONDS, 3),
                    "lockout_seconds": LOCKOUT_SECONDS,
                    "user_agent": (user_agent or "")[:_MAX_USER_AGENT],
                }
                window.clear()
        if locked_record is not None and self.audit is not None:
            self.audit.write("lockout", ip, **locked_record)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._locked_until.pop(ip, None)

    def locked_ips(self) -> List[Tuple[str, float]]:
        """지금 잠겨 있는 IP 와 남은 초. 풀린 것은 목록에서 치웁니다."""
        now = self._clock()
        with self._lock:
            for ip in [ip for ip, until in self._locked_until.items() if until <= now]:
                del self._locked_until[ip]
            return sorted(((ip, until - now) for ip, until in self._locked_until.items()),
                          key=lambda item: -item[1])

    def unlock(self, ip: str) -> bool:
        """잠긴 IP 를 풉니다. 실패 횟수도 비워, 한 번 틀렸다고 곧바로 다시 잠기지 않게 합니다.

        서버 PC 의 주인만 부릅니다 (`access_token.py` 가 루프백을 확인). 잠겨 있지 않았으면 False.
        """
        with self._lock:
            until = self._locked_until.pop(ip, None)
            self._failures.pop(ip, None)
        was_locked = until is not None
        if was_locked and self.audit is not None:
            self.audit.write("unlock", ip, by="loopback", remaining_seconds=round(max(until - self._clock(), 0), 1))
        return was_locked


_control: Optional[AccessControl] = None


def get_access_control() -> AccessControl:
    """시작할 때 환경변수(= `.env`, `load_dotenv`)의 토큰으로 만듭니다."""
    global _control
    if _control is None:
        _control = AccessControl(
            os.environ.get(TOKEN_ENV, "").strip() or None, source="startup", audit=AuditLog()
        )
    return _control


# ---------------------------------------------------------------------------
# ASGI 계층
# ---------------------------------------------------------------------------

Scope = Dict[str, Any]
Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]


def _headers(scope: Scope) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        out[key.decode("latin-1").lower()] = value.decode("latin-1")
    return out


def _cookie(headers: Dict[str, str], name: str) -> Optional[str]:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _safe_next(value: Optional[str]) -> str:
    """로그인 뒤 돌아갈 곳. 이 서버 안의 경로만 받습니다 (`//evil` 같은 외부 주소 차단)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


_PAGE_STYLE = (
    "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    "background:#020617;color:#e2e8f0;font:14px system-ui,-apple-system,'Segoe UI',sans-serif}"
    ".card{width:min(380px,92vw);background:#0f172a;border:1px solid #334155;border-radius:14px;"
    "padding:28px;box-shadow:0 20px 50px rgba(0,0,0,.5)}"
    "h1{font-size:17px;margin:0 0 6px}p{color:#94a3b8;font-size:12px;line-height:1.6;margin:0 0 16px}"
    "input{box-sizing:border-box;width:100%;padding:10px 12px;border-radius:8px;border:1px solid #475569;"
    "background:#020617;color:#e2e8f0;font:15px ui-monospace,Consolas,monospace;letter-spacing:.5px}"
    "button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:#4f46e5;"
    "color:#fff;font-weight:600;cursor:pointer}.err{color:#fda4af;font-size:12px;margin:10px 0 0}"
)


def login_page(message: str = "", next_path: str = "/") -> bytes:
    error = f'<p class="err">{escape(message)}</p>' if message else ""
    return (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>MADO 로그인</title><style>{_PAGE_STYLE}</style></head><body><div class=\"card\">"
        "<h1>MADO 원격 접속</h1>"
        "<p>이 서버의 주인만 원격에서 쓸 수 있습니다. 서버 PC 의 <code>.env</code> 에 있는 "
        f"접속 토큰({TOKEN_LENGTH}자)을 입력하세요. 로그인은 7일 유지됩니다.</p>"
        f"<form method=\"post\" action=\"{LOGIN_PATH}\" autocomplete=\"off\">"
        f"<input type=\"hidden\" name=\"next\" value=\"{escape(next_path)}\">"
        f"<input type=\"password\" name=\"token\" maxlength=\"{TOKEN_LENGTH}\" autofocus "
        "placeholder=\"접속 토큰\" required>"
        f"<button type=\"submit\">로그인</button>{error}</form></div></body></html>"
    ).encode("utf-8")


def blocked_page(reason: str) -> bytes:
    return (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        f"<title>MADO 원격 접속 꺼짐</title><style>{_PAGE_STYLE}</style></head><body><div class=\"card\">"
        "<h1>원격 접속이 꺼져 있습니다</h1>"
        f"<p>{escape(reason)}. 서버 PC 에서 MADO 첫 화면을 여세요 — 외부에 열린 서버에 토큰이 없으면 "
        "그때 새 토큰이 만들어집니다. 토큰을 직접 정했다면 오른쪽 위 열쇠 버튼으로 적용하세요.</p>"
        "</div></body></html>"
    ).encode("utf-8")


async def _respond(send: Send, status: int, body: bytes, *,
                   content_type: str = "text/html; charset=utf-8",
                   headers: Iterable[Tuple[str, str]] = ()) -> None:
    raw = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-frame-options", b"DENY"),
        # `no-referrer` 는 안 됩니다: 그 페이지의 폼 POST 에 브라우저가 `Origin: null` 을 실어
        # 로그인이 교차 출처로 거부됩니다. `same-origin` 은 이 서버 밖으로는 Referer 를 보내지 않습니다.
        (b"referrer-policy", b"same-origin"),
    ] + [(k.encode(), v.encode()) for k, v in headers]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive: Receive, limit: int) -> Optional[bytes]:
    chunks: List[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


class AccessMiddleware:
    """맨 바깥에서 모든 HTTP·웹소켓 요청을 거릅니다."""

    def __init__(self, app: Callable[..., Awaitable[None]], control: Optional[AccessControl] = None,
                 guest_gate: Optional[GuestGate] = None):
        self.app = app
        self._control = control
        self.guest_gate = guest_gate

    @property
    def control(self) -> AccessControl:
        return self._control or get_access_control()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        client = scope.get("client") or ("", 0)
        ip = client[0] if client else ""
        host_header = headers.get("host", "")
        origin = headers.get("origin")
        path = scope.get("path", "")

        # 교차 출처 요청은 루프백이든 원격이든 거부합니다 (악성 페이지가 우리 서버를 부르는 경우).
        if origin is not None and not same_origin(origin, host_header):
            await self._deny(scope, send, 403, "cross-origin request refused")
            return

        if is_loopback(ip):
            if _hostname(host_header) not in allowed_loopback_hosts():
                # 루프백으로 왔는데 이름이 낯섭니다: DNS 리바인딩.
                await self._deny(scope, send, 403, "unexpected Host for a loopback connection")
                return
            if kind == "http" and path in (LOGIN_PATH, LOGOUT_PATH):
                await _respond(send, 303, b"", headers=[("location", "/")])
                return
            _mark_viewer(scope, VIEWER_OWNER)
            await self.app(scope, receive, send)
            return

        control = self.control
        if kind == "http" and path == LOGIN_PATH:
            await self._login(scope, receive, send, ip)
            return
        if kind == "http" and path == LOGOUT_PATH:
            await _respond(send, 303, b"", headers=[
                ("location", LOGIN_PATH),
                ("set-cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"),
            ])
            return

        if control.status.enabled and control.cookie_valid(_cookie(headers, COOKIE_NAME)):
            _mark_viewer(scope, VIEWER_OWNER)
            await self.app(scope, receive, send)
            return

        # 체험 서버가 켜져 있으면 토큰 없는 원격 접속은 방문자입니다. 주인 토큰이 없어도
        # (원격 주인 접속이 꺼져 있어도) 방문자 통로는 따로 열립니다.
        gate = self.guest_gate
        if gate is not None and gate.enabled():
            if gate.allows(path):
                _mark_viewer(scope, VIEWER_GUEST)
                await self.app(scope, receive, send)
                return
            if kind == "http" and scope.get("method") == "GET" and "text/html" in headers.get("accept", ""):
                await _respond(send, 303, b"", headers=[("location", gate.home_path)])
                return
            await self._deny(scope, send, 403, "not available to trial visitors")
            return

        if not control.status.enabled:
            await self._deny(scope, send, 403, control.status.reason, page=blocked_page(control.status.reason))
            return

        if kind == "http" and scope.get("method") == "GET" and "text/html" in headers.get("accept", ""):
            query = scope.get("query_string", b"").decode("latin-1")
            target = path + (f"?{query}" if query else "")
            # 돌아갈 곳만 URL 에 둡니다 (토큰은 절대 URL 에 싣지 않습니다).
            location = f"{LOGIN_PATH}?next={_quote(target)}"
            await _respond(send, 303, b"", headers=[("location", location)])
            return
        await self._deny(scope, send, 401, "login required")

    async def _login(self, scope: Scope, receive: Receive, send: Send, ip: str) -> None:
        control = self.control
        if not control.status.enabled:
            await _respond(send, 403, blocked_page(control.status.reason))
            return
        method = scope.get("method")
        if method == "GET":
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            await _respond(send, 200, login_page(next_path=_safe_next((query.get("next") or ["/"])[0])))
            return
        if method != "POST":
            await _respond(send, 405, b"method not allowed", content_type="text/plain")
            return

        remaining = control.locked_for(ip)
        if remaining:
            await _respond(send, 429, login_page(
                f"로그인 실패가 많아 잠겼습니다. {int(remaining // 60) + 1}분 뒤에 다시 시도하세요."
            ))
            return
        body = await _read_body(receive, MAX_LOGIN_BODY)
        form = parse_qs(body.decode("utf-8", "replace")) if body is not None else {}
        token = (form.get("token") or [""])[0]
        next_path = _safe_next((form.get("next") or ["/"])[0])
        if body is None or not control.check_token(token):
            control.record_failure(ip, user_agent=_headers(scope).get("user-agent", ""))
            await _respond(send, 401, login_page("토큰이 맞지 않습니다.", next_path))
            return
        control.record_success(ip)
        cookie = (
            f"{COOKIE_NAME}={control.issue_cookie()}; Path=/; Max-Age={SESSION_SECONDS}; "
            "HttpOnly; SameSite=Strict"
        )
        await _respond(send, 303, b"", headers=[("location", next_path), ("set-cookie", cookie)])

    async def _deny(self, scope: Scope, send: Send, status: int, reason: str,
                    page: Optional[bytes] = None) -> None:
        if scope.get("type") == "websocket":
            # 연결을 받기 전에 닫으면 핸드셰이크가 403 으로 거절됩니다.
            await send({"type": "websocket.close", "code": 1008})
            return
        if page is not None:
            await _respond(send, status, page)
        else:
            await _respond(send, status, reason.encode("utf-8"), content_type="text/plain; charset=utf-8")


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="/")


# ---------------------------------------------------------------------------
# 이미 열려 있는 원격 화면 끊기
# ---------------------------------------------------------------------------


async def disconnect_remote_clients() -> int:
    """토큰을 바꾼 뒤, 열려 있는 원격 화면을 로그인 페이지로 돌려보내고 연결을 끊습니다.

    쿠키 검사는 연결을 맺을 때 하므로, 이미 맺어진 웹소켓은 토큰을 바꿔도 그대로 남습니다.
    화면에 새로고침을 시키고(→ 로그인 페이지), 말을 듣지 않는 화면을 위해 소켓도 끊습니다.
    """
    import asyncio

    from nicegui import core
    from nicegui.client import Client

    count = 0
    for client in list(Client.instances.values()):
        # 연결이 없는 화면(이미 떠난 탭 등)은 세지 않습니다.
        if is_loopback(client.ip) or not client.has_socket_connection:
            continue
        count += 1
        try:
            client.run_javascript("window.location.reload()")
        except Exception:  # noqa: BLE001 - 끊는 것이 목적이라 실패해도 계속합니다
            pass
    if count:
        await asyncio.sleep(1.0)
        for client in list(Client.instances.values()):
            if is_loopback(client.ip):
                continue
            for sid in list(getattr(client, "_socket_to_document_id", {}).keys()):
                try:
                    await core.sio.disconnect(sid)
                except Exception:  # noqa: BLE001
                    pass
    return count
