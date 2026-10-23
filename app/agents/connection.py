"""모델 연결의 정체와 실패의 갈래.

같은 모델·주소·키를 쓰는 에이전트는 한 연결입니다 (`connection_signature` · `connection_id`). LiteLLM 이 올린
예외는 사람이 고칠 수 있는 갈래(인증 실패, 모델 이름, 연결 실패, 응답 없음 …)로 나눕니다 (`classify`).
연결 상태 창(`model_status`)과 전역 모델 폴백(`llm_fallback`)이 함께 씁니다.
"""

import asyncio
import hashlib
import json
from typing import Any, Dict, Optional, Sequence, Tuple

from app.config import AgentConfig, LLMConfig

# 상태: (화면 표기, 색조, 사람이 할 일)
GLOBAL_USER = "전역 기본값"


OK = "ok"


NOT_CONFIGURED = "not_configured"


BUSY = "busy"


AUTH = "auth"


MODEL = "model"


RATE_LIMITED = "rate_limited"


UNREACHABLE = "unreachable"


TIMEOUT = "timeout"


REJECTED = "rejected"


SERVER_ERROR = "server_error"


ERROR = "error"


CHECKING = "checking"


UNKNOWN = "unknown"


STATE_INFO: Dict[str, Tuple[str, str, str]] = {
    OK: ("연결됨", "good", "모델이 응답했습니다."),
    NOT_CONFIGURED: ("연결 정보 없음", "idle",
                     "주소(api_base)도 키(api_key)도 없습니다. .env 나 conf.json 에 적어 주세요."),
    BUSY: ("확인 보류", "warn",
           "LLM 서버가 다른 요청을 처리하고 있어 끼어들지 않았습니다. 잠시 뒤에 다시 확인해 주세요."),
    AUTH: ("인증 실패", "bad", "키가 틀렸거나 이 모델을 쓸 권한이 없습니다."),
    MODEL: ("모델 이름 확인", "bad",
            "이 이름의 모델을 찾지 못했습니다. 이름과 Provider 접두사(openai/ · gemini/ 등)를 확인해 주세요."),
    RATE_LIMITED: ("한도 초과", "warn", "연결은 되지만 요청 한도에 걸렸습니다. 잠시 뒤에는 응답할 수 있습니다."),
    UNREACHABLE: ("연결 실패", "bad", "주소에 닿지 못했습니다. 주소와 네트워크, 서버가 떠 있는지 확인해 주세요."),
    TIMEOUT: ("응답 없음", "bad", "정해진 시간 안에 응답이 오지 않았습니다."),
    REJECTED: ("요청 거절", "warn", "서버에 닿았지만 요청을 받아들이지 않았습니다. 모델 이름과 설정을 확인해 주세요."),
    SERVER_ERROR: ("서버 오류", "bad", "LLM 서버가 오류를 돌려주었습니다."),
    ERROR: ("확인 실패", "bad", "알 수 없는 이유로 확인하지 못했습니다."),
    CHECKING: ("확인 중", "idle", "응답을 기다리고 있습니다."),
    UNKNOWN: ("확인 전", "idle", "아직 확인하지 않았습니다."),
}


CONNECTION_FIELDS = ("model", "api_key", "api_base", "api_version", "provider", "extra_headers", "extra_body")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == {} or value == []


def global_entry(llm: LLMConfig, extra_fields: Sequence[str] = ()) -> AgentConfig:
    """전역 기본값을, 아무것도 덮어쓰지 않은 에이전트 하나로 봅니다 (새 에이전트가 갖게 될 연결).

    `extra_fields` 는 연결 말고도 전역에서 가져올 항목입니다 (폴백이 모델 용량을 함께 옮길 때 씁니다).
    전역에 비어 있는 값은 `AgentConfig` 의 기본값이 됩니다.
    """
    names = (*CONNECTION_FIELDS, *extra_fields)
    values = {name: getattr(llm, name) for name in names if not _blank(getattr(llm, name))}
    return AgentConfig(name=GLOBAL_USER, role="", **values)


def connection_signature(cfg: Any) -> Tuple[Any, ...]:
    """연결을 가르는 값 전부. `AgentConfig` 와 `Agent` 어느 쪽을 넣어도 같은 값이 나옵니다."""
    return (
        cfg.model, cfg.api_base or "", cfg.api_key or "", cfg.api_version or "", cfg.provider or "",
        tuple(sorted((str(k), str(v)) for k, v in (cfg.extra_headers or {}).items())),
        json.dumps(cfg.extra_body or {}, sort_keys=True, ensure_ascii=False, default=str),
    )


def connection_id(cfg: Any) -> str:
    """연결의 이름표. 키 값을 되짚을 수 없는 해시이고, 화면의 줄과 폴백 차단기가 함께 씁니다."""
    return hashlib.sha256(repr(connection_signature(cfg)).encode("utf-8")).hexdigest()[:12]


_BY_CLASS: Tuple[Tuple[str, str], ...] = (
    # 구체적인 것이 앞에 옵니다 (LiteLLM 의 예외는 서로 물려받습니다).
    ("AuthenticationError", AUTH),
    ("PermissionDeniedError", AUTH),
    ("NotFoundError", MODEL),
    ("RateLimitError", RATE_LIMITED),
    ("Timeout", TIMEOUT),
    ("ContextWindowExceededError", REJECTED),
    ("ContentPolicyViolationError", REJECTED),
    ("UnprocessableEntityError", REJECTED),
    ("BadRequestError", REJECTED),
    ("ServiceUnavailableError", SERVER_ERROR),
    ("BadGatewayError", SERVER_ERROR),
    ("InternalServerError", SERVER_ERROR),
    ("APIConnectionError", UNREACHABLE),
)


_CONNECT_WORDS = (
    "connection error", "connection refused", "connect call failed", "cannot connect", "connecterror",
    "all connection attempts failed", "getaddrinfo", "name or service not known", "nodename nor servname",
)


def _by_status_code(code: Any) -> Optional[str]:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    if code in (401, 403):
        return AUTH
    if code == 404:
        return MODEL
    if code == 408:
        return TIMEOUT
    if code == 429:
        return RATE_LIMITED
    if code in (400, 422):
        return REJECTED
    if code >= 500:
        return SERVER_ERROR
    return None


def classify(exc: BaseException) -> str:
    """LiteLLM 이 올린 예외를 사람이 고칠 수 있는 갈래로 나눕니다."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TIMEOUT
    names = {cls.__name__ for cls in type(exc).__mro__}
    state = next((value for name, value in _BY_CLASS if name in names), None)
    state = state or _by_status_code(getattr(exc, "status_code", None)) or ERROR
    text = str(exc).lower()
    if state in (SERVER_ERROR, ERROR) and any(word in text for word in _CONNECT_WORDS):
        # 서버가 꺼져 있어 닿지 못한 것을 '서버 오류' 로 올리는 Provider 어댑터가 있습니다.
        return UNREACHABLE
    if state in (REJECTED, ERROR, UNREACHABLE):
        # Provider 마다 같은 실패를 다른 상태 코드로 돌려줍니다 (틀린 키에 400 을 주는 곳도 있습니다).
        if "provider not provided" in text or "llm provider not" in text:
            return MODEL
        if "api key" in text or "api_key" in text or "unauthorized" in text or "unauthenticated" in text:
            return AUTH
        if "model" in text and ("not found" in text or "does not exist" in text or "not exist" in text
                                or "is not supported" in text or "unknown model" in text):
            return MODEL
    return state
