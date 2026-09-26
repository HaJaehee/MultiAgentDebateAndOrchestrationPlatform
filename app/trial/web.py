"""체험 로그인·로그아웃 (평범한 HTML 폼) 과 화면이 쓰는 방문자 확인.

로그인은 NiceGUI 화면이 아니라 폼 POST 로 받습니다. 쿠키는 HTTP 응답으로만 심을 수 있는데,
NiceGUI 화면의 버튼은 웹소켓 위에서 돌아서 응답 헤더를 만들 수 없습니다. 주인 토큰 로그인
(`app/security.py`)과 같은 방식입니다.

교차 출처 POST 는 바깥 미들웨어가 이미 막습니다 (Origin 검사).
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import get_config
from app.database.session import get_session_factory
from app.security import LOGIN_PATH as OWNER_LOGIN_PATH
from app.security import VIEWER_OWNER, viewer_role
from app.trial.auth import (
    MAX_NAME_LENGTH,
    MAX_PIN_LENGTH,
    TRIAL_COOKIE,
    clean_name,
    clear_cookie_header,
    cookie_header,
    get_ip_throttle,
    issue_cookie,
    login_or_register,
    user_from_cookie,
)
from app.trial.gate import TRIAL_HOME

TRIAL_LOGIN_PATH = "/trial/login"
TRIAL_LOGOUT_PATH = "/trial/logout"
MAX_LOGIN_BODY = 4096


@dataclass(frozen=True)
class Visitor:
    id: str
    name: str


def is_owner(request: Request) -> bool:
    return viewer_role(request.scope) == VIEWER_OWNER


async def current_visitor(request: Request) -> Optional[Visitor]:
    """쿠키가 가리키는 방문자. 없거나 풀렸으면 None."""
    async with get_session_factory()() as db:
        user = await user_from_cookie(db, request.cookies.get(TRIAL_COOKIE))
        return Visitor(user.id, user.name) if user is not None else None


def _safe_next(value: Optional[str]) -> str:
    """로그인 뒤 돌아갈 곳. 체험 화면 안쪽만 받습니다."""
    value = (value or "").strip()
    if value.startswith(TRIAL_HOME) and not value.startswith("//") and "\\" not in value:
        return value
    return TRIAL_HOME


_STYLE = """
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b1120;color:#e2e8f0;font-family:system-ui,-apple-system,"Malgun Gothic",sans-serif;padding:16px}
.card{width:100%;max-width:400px;background:#111827;border:1px solid #1f2937;border-radius:14px;padding:28px}
h1{font-size:20px;margin:0 0 6px;font-weight:600}p{color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 18px}
label{display:block;font-size:13px;color:#cbd5e1;margin:12px 0 6px}
input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#f1f5f9;font-size:15px}
input:focus{outline:2px solid #6366f1;border-color:transparent}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:#4f46e5;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#4338ca}.msg{margin-top:14px;font-size:13px;color:#fca5a5}.info{color:#a5b4fc}
.foot{margin-top:18px;font-size:12px;color:#64748b;line-height:1.6}.foot a{color:#818cf8}
"""


def login_html(*, title: str, notice: str, min_pin: int, name: str = "", message: str = "",
               confirm: bool = False, next_path: str = TRIAL_HOME, info: bool = False) -> str:
    confirm_field = (
        "<label for=\"pin_confirm\">PIN 확인</label>"
        f"<input id=\"pin_confirm\" name=\"pin_confirm\" type=\"password\" maxlength=\"{MAX_PIN_LENGTH}\" "
        "autocomplete=\"new-password\" required>"
        if confirm else ""
    )
    msg = f"<div class=\"msg{' info' if info else ''}\">{escape(message)}</div>" if message else ""
    focus_name = "" if name else " autofocus"
    focus_pin = " autofocus" if name else ""
    return (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)} 로그인</title><style>{_STYLE}</style></head><body><div class=\"card\">"
        f"<h1>{escape(title)}</h1>"
        "<p>이름과 PIN 으로 들어옵니다. 처음이면 지금 적는 PIN 으로 등록되고, "
        "다음부터 같은 이름과 PIN 으로 내 대화를 이어 볼 수 있습니다.</p>"
        f"<form method=\"post\" action=\"{TRIAL_LOGIN_PATH}\" autocomplete=\"off\">"
        f"<input type=\"hidden\" name=\"next\" value=\"{escape(next_path)}\">"
        "<label for=\"name\">이름 (사번도 좋습니다)</label>"
        f"<input id=\"name\" name=\"name\" maxlength=\"{MAX_NAME_LENGTH}\" value=\"{escape(name)}\" required{focus_name}>"
        f"<label for=\"pin\">PIN ({min_pin}자 이상)</label>"
        f"<input id=\"pin\" name=\"pin\" type=\"password\" maxlength=\"{MAX_PIN_LENGTH}\" "
        f"autocomplete=\"current-password\" required{focus_pin}>"
        f"{confirm_field}"
        f"<button type=\"submit\">{'등록하고 시작' if confirm else '들어가기'}</button>{msg}</form>"
        f"<div class=\"foot\">{escape(notice)}<br>"
        f"PIN 을 잊었으면 운영자에게 초기화를 부탁하세요. · <a href=\"{OWNER_LOGIN_PATH}\">운영자 로그인</a></div>"
        "</div></body></html>"
    )


def _page(status: int = 200, **kwargs) -> HTMLResponse:
    cfg = get_config().trial
    body = login_html(title=cfg.title, notice=cfg.notice, min_pin=cfg.pin_min_length, **kwargs)
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def disabled_response() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=\"utf-8\"><title>체험 서버 꺼짐</title>"
        "<p style=\"font-family:sans-serif\">이 서버에서는 체험 화면이 꺼져 있습니다.</p>",
        status_code=404,
    )


def register_trial_routes(app: FastAPI) -> None:
    """로그인·로그아웃 경로를 붙입니다. NiceGUI 를 붙이기(`ui.run_with`) **전에** 불러야 합니다."""

    @app.get(TRIAL_LOGIN_PATH, include_in_schema=False)
    async def trial_login_page(request: Request) -> Response:
        if not get_config().trial.enabled:
            return disabled_response()
        if await current_visitor(request) is not None:
            return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
        return _page(next_path=_safe_next(request.query_params.get("next")))

    @app.post(TRIAL_LOGIN_PATH, include_in_schema=False)
    async def trial_login(request: Request) -> Response:
        cfg = get_config().trial
        if not cfg.enabled:
            return disabled_response()
        ip = request.client.host if request.client else ""
        throttle = get_ip_throttle()
        if throttle.blocked(ip):
            return _page(429, message="이 자리에서 로그인 실패가 너무 많습니다. 15분 뒤에 다시 시도하세요.")
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = MAX_LOGIN_BODY + 1
        if declared > MAX_LOGIN_BODY:
            return _page(413, message="입력이 너무 깁니다.")
        body = await request.body()
        if len(body) > MAX_LOGIN_BODY:
            return _page(413, message="입력이 너무 깁니다.")
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        name = (form.get("name") or [""])[0]
        pin = (form.get("pin") or [""])[0]
        confirm = (form.get("pin_confirm") or [None])[0]
        next_path = _safe_next((form.get("next") or [""])[0])

        async with get_session_factory()() as db:
            outcome = await login_or_register(db, name, pin, confirm, min_pin_length=cfg.pin_min_length)
            if outcome.status == "ok" and outcome.user is not None:
                response = RedirectResponse(next_path, status_code=303)
                response.headers.append("set-cookie", cookie_header(issue_cookie(outcome.user)))
                response.headers["Cache-Control"] = "no-store"
                return response

        if outcome.status == "wrong":
            throttle.fail(ip)
        shown = clean_name(name)
        if outcome.status in ("confirm", "mismatch"):
            return _page(name=shown, message=outcome.message, confirm=True, next_path=next_path,
                         info=outcome.status == "confirm")
        status = 401 if outcome.status in ("wrong", "locked") else 400
        return _page(status, name=shown, message=outcome.message, next_path=next_path)

    @app.get(TRIAL_LOGOUT_PATH, include_in_schema=False)
    async def trial_logout() -> Response:
        response = RedirectResponse(TRIAL_LOGIN_PATH, status_code=303)
        response.headers.append("set-cookie", clear_cookie_header())
        return response


def owner_only_response() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=\"utf-8\"><title>운영자 전용</title>"
        "<p style=\"font-family:sans-serif\">운영자만 볼 수 있는 화면입니다. "
        f"<a href=\"{OWNER_LOGIN_PATH}\">운영자 로그인</a></p>",
        status_code=403,
    )
