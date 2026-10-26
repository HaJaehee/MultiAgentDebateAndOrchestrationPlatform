"""AnythingLLM 문서 검색 MCP 서버 (`kb`).

AnythingLLM 작업 공간에 넣어 둔 문서에서 질의와 가까운 조각을 찾아, **원문과 출처**를 돌려줍니다.
MADO 의 에이전트가 `kb__search` 로 부릅니다 (conf.json 의 `mcp_servers.kb`).

AnythingLLM 개발자 API 중 검색(`POST /api/v1/workspace/{slug}/vector-search`)과 작업 공간 목록
(`GET /api/v1/workspaces`)만 씁니다. 답은 만들지 않습니다 — 답은 에이전트가 자기 모델로 쓰고, 이
서버는 근거를 줍니다. AnythingLLM 의 채팅 API 를 쓰면 다른 LLM 이 답을 만들어 출처가 흐려지고,
MADO 의 토큰 계측과 폴백 밖에서 토큰을 씁니다.

설정은 환경변수입니다. MADO 는 부모 환경의 비밀을 MCP 서버에 넘기지 않으므로 conf.json 의 이
서버 `env` 에 적습니다 (`"ANYTHINGLLM_API_KEY": "${ANYTHINGLLM_API_KEY}"`).

  ANYTHINGLLM_URL              AnythingLLM 주소. 기본 http://localhost:3001 (끝의 /api, /api/v1 은 있어도 됨)
  ANYTHINGLLM_API_KEY          개발자 API 키 (필수). AnythingLLM 설정의 Developer API 에서 만듭니다.
  ANYTHINGLLM_WORKSPACE        기본 작업 공간 slug. 비우면 검색할 수 있는 작업 공간이 하나일 때 그것을 씁니다.
  ANYTHINGLLM_WORKSPACES       검색을 허용할 작업 공간 slug (쉼표로 구분). 비우면 전부.
  ANYTHINGLLM_TOP_N            한 번에 돌려줄 조각 수 기본값 (기본 5, 최대 20)
  ANYTHINGLLM_SCORE_THRESHOLD  이 점수(0~1) 미만의 조각은 버림. 비우면 작업 공간 설정을 따름
  ANYTHINGLLM_TIMEOUT          요청 하나를 기다리는 초 (기본 30)

검색하지 못하면 도구 오류로 그 이유를 돌려줍니다 (키 없음·거부, 연결 실패, 모르는 작업 공간 …).
결과를 지어내지 않습니다.

두 도구 모두 읽기 전용·닫힌 세계(`readOnlyHint=True`, `openWorldHint=False`)로 표시합니다. 정해진
문서 묶음만 읽기 때문입니다. MADO 의 도구 보안은 이 표시로 읽기로 판정하므로 검색할 때마다 승인
카드가 뜨지 않습니다 (표시가 없으면 `unknown`, `openWorldHint` 를 생략하면 명세 기본값이 True 라
`net` 이 되어 매번 묻습니다).

실행: python server.py  (stdio)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("anythingllm-kb")

DEFAULT_URL = "http://localhost:3001"
DEFAULT_TOP_N = 5
MAX_TOP_N = 20
DEFAULT_TIMEOUT = 30.0

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
)

# AnythingLLM 은 조각을 임베딩할 때 문서 정보를 이 머리로 붙여 둡니다. 출처는 따로 적으므로 걷어냅니다.
_HEADER_META = re.compile(r"^\s*<document_metadata>.*?</document_metadata>\s*", re.DOTALL)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class KBError(Exception):
    """검색하지 못한 이유. 도구 오류로 에이전트에게 그대로 갑니다."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    workspace: str
    allowed: Tuple[str, ...]
    top_n: int
    score_threshold: Optional[float]
    timeout: float

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """호출 때마다 읽습니다. 서버를 다시 띄우지 않고 값을 바꿔 시험할 수 있습니다."""
        env = os.environ if env is None else env
        threshold = (env.get("ANYTHINGLLM_SCORE_THRESHOLD") or "").strip()
        return cls(
            base_url=normalize_base_url(env.get("ANYTHINGLLM_URL", "")),
            api_key=(env.get("ANYTHINGLLM_API_KEY") or "").strip(),
            workspace=(env.get("ANYTHINGLLM_WORKSPACE") or "").strip(),
            allowed=tuple(
                s.strip() for s in (env.get("ANYTHINGLLM_WORKSPACES") or "").split(",") if s.strip()
            ),
            top_n=_number(env, "ANYTHINGLLM_TOP_N", DEFAULT_TOP_N, int),
            score_threshold=_number(env, "ANYTHINGLLM_SCORE_THRESHOLD", 0.0, float) if threshold else None,
            timeout=_number(env, "ANYTHINGLLM_TIMEOUT", DEFAULT_TIMEOUT, float),
        )


def _number(env: Mapping[str, str], name: str, default: Any, kind: type) -> Any:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        raise KBError(f"{name} 값이 숫자가 아닙니다: '{raw}'. conf.json 의 mcp_servers.kb.env 를 고치세요.") from None


def normalize_base_url(url: str) -> str:
    """`http://host:3001`, `.../api`, `.../api/v1/`, `host:3001` 을 모두 `http://host:3001` 로 맞춥니다."""
    text = (url or "").strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text and "://" not in text:
        text = "http://" + text
    return text or DEFAULT_URL


# --------------------------------------------------------------------- HTTP


async def _request(settings: Settings, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not settings.api_key:
        raise KBError(
            "ANYTHINGLLM_API_KEY 가 비어 있어 검색하지 못했습니다. AnythingLLM 설정의 Developer API 에서 "
            "키를 만들어 .env 에 넣고, conf.json 의 mcp_servers.kb.env 에서 ${ANYTHINGLLM_API_KEY} 로 넘기세요."
        )
    url = f"{settings.base_url}/api/v1{path}"
    # 이 PC 안의 AnythingLLM 에 붙을 때는 HTTP_PROXY 같은 프록시 설정을 타지 않게 합니다.
    # 사내 PC 에 프록시가 잡혀 있으면 localhost 요청이 프록시로 가서 실패합니다.
    try:
        trust_env = httpx.URL(settings.base_url).host not in _LOOPBACK_HOSTS
    except httpx.InvalidURL:
        raise KBError(f"ANYTHINGLLM_URL 이 주소 형식이 아닙니다: '{settings.base_url}'.") from None
    try:
        async with httpx.AsyncClient(timeout=settings.timeout, trust_env=trust_env) as client:
            response = await client.request(
                method, url, json=body,
                headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        raise KBError(
            f"AnythingLLM({settings.base_url})이 {settings.timeout:g}초 안에 답하지 않아 검색하지 못했습니다."
        ) from None
    except httpx.HTTPError as exc:
        raise KBError(
            f"AnythingLLM({settings.base_url})에 연결하지 못해 검색하지 못했습니다 ({type(exc).__name__}). "
            "주소가 맞는지, AnythingLLM 이 켜져 있는지 확인하세요."
        ) from None

    if response.status_code in (401, 403):
        raise KBError(
            f"AnythingLLM 이 API 키를 거부해 검색하지 못했습니다 (HTTP {response.status_code}). "
            "ANYTHINGLLM_API_KEY 를 확인하세요."
        )
    if response.status_code >= 400:
        raise KBError(
            f"AnythingLLM 이 요청을 거절해 검색하지 못했습니다 (HTTP {response.status_code}): "
            f"{_reason(response)}"
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise KBError(
            f"AnythingLLM 응답을 읽지 못해 검색하지 못했습니다 — JSON 이 아닙니다. "
            f"ANYTHINGLLM_URL({settings.base_url})이 AnythingLLM 주소인지 확인하세요."
        )
    return data


def _reason(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("message", "error"):
            if data.get(key):
                return str(data[key])
    return (response.text or "").strip()[:300] or "(사유 없음)"


# --------------------------------------------------------------------- 작업 공간


async def _workspaces(settings: Settings) -> List[Dict[str, str]]:
    data = await _request(settings, "GET", "/workspaces")
    found = [
        {"slug": str(w.get("slug") or ""), "name": str(w.get("name") or "")}
        for w in (data.get("workspaces") or [])
        if isinstance(w, dict) and w.get("slug")
    ]
    if settings.allowed:
        found = [w for w in found if w["slug"] in settings.allowed]
    return found


async def _resolve_workspace(settings: Settings, requested: str) -> str:
    slug = (requested or "").strip() or settings.workspace
    if slug:
        if settings.allowed and slug not in settings.allowed:
            raise KBError(
                f"작업 공간 '{slug}'은 검색할 수 없습니다. 검색할 수 있는 작업 공간: {', '.join(settings.allowed)}"
            )
        return slug
    available = await _workspaces(settings)
    if len(available) == 1:
        return available[0]["slug"]
    if not available:
        raise KBError("검색할 수 있는 AnythingLLM 작업 공간이 없습니다. AnythingLLM 에서 작업 공간을 만들고 문서를 넣으세요.")
    raise KBError(
        "작업 공간이 여럿이라 workspace 인자로 하나를 골라야 합니다: "
        + ", ".join(w["slug"] for w in available)
    )


# --------------------------------------------------------------------- 결과 형식


def _source(meta: Mapping[str, Any]) -> str:
    for key in ("url", "chunkSource", "docSource"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return ""


def _score(item: Mapping[str, Any]) -> str:
    score = item.get("score")
    return f"{score:.2f}" if isinstance(score, (int, float)) else ""


def format_results(slug: str, query: str, results: List[Any], message: str = "") -> str:
    head = f'[AnythingLLM 작업 공간 `{slug}`] "{query}"'
    items = [r for r in results if isinstance(r, dict)]
    if not items:
        note = f" (AnythingLLM: {message})" if message else ""
        return (
            f"{head} — 찾은 조각이 없습니다{note}.\n"
            "질의를 바꿔 다시 찾거나, 문서에서 찾지 못했다고 밝히세요. 문서에 있다고 지어내지 마세요."
        )
    blocks = [f"{head} — 조각 {len(items)}개. 인용할 때는 [번호]와 문서 제목을 밝히세요."]
    for index, item in enumerate(items, start=1):
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        title = str(meta.get("title") or "").strip() or _source(meta) or f"조각 {item.get('id', index)}"
        parts = [f"[{index}] {title}"]
        source = _source(meta)
        if source and source != title:
            parts.append(f"출처: {source}")
        if _score(item):
            parts.append(f"점수 {_score(item)}")
        text = _HEADER_META.sub("", str(item.get("text") or "")).strip()
        blocks.append(" · ".join(parts) + "\n" + (text or "(본문 없음)"))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------- 도구


@mcp.tool(annotations=READ_ONLY)
async def search(query: str, top_n: int = 0, workspace: str = "") -> str:
    """AnythingLLM 작업 공간의 문서에서 질의와 가까운 조각을 찾아 원문과 출처를 돌려줍니다.

    규정·설계 문서·보고서처럼 대화의 작업 공간 밖에 있는 자료를 근거로 삼을 때 씁니다. query 에는
    찾는 내용을 문장이나 핵심어로 적습니다 (예: "외부 반출 승인 절차"). top_n 은 돌려받을 조각 수
    (1~20, 0 이면 기본값), workspace 는 작업 공간 slug 입니다 (비우면 기본 작업 공간, 목록은
    list_workspaces). 인용할 때는 [번호]와 문서 제목을 밝히고, 결과에 없는 내용을 문서에 있다고
    쓰지 마세요. 찾지 못했으면 질의를 바꿔 다시 찾거나 찾지 못했다고 말하세요.
    """
    settings = Settings.from_env()
    text = (query or "").strip()
    if not text:
        raise KBError("query 가 비어 있습니다. 찾을 내용을 적으세요.")
    slug = await _resolve_workspace(settings, workspace)
    n = top_n if isinstance(top_n, int) and top_n > 0 else settings.top_n
    body: Dict[str, Any] = {"query": text, "topN": max(1, min(n, MAX_TOP_N))}
    if settings.score_threshold is not None:
        body["scoreThreshold"] = settings.score_threshold
    data = await _request(settings, "POST", f"/workspace/{quote(slug, safe='')}/vector-search", body)
    results = data.get("results")
    return format_results(slug, text, results if isinstance(results, list) else [], str(data.get("message") or ""))


@mcp.tool(annotations=READ_ONLY)
async def list_workspaces() -> str:
    """검색할 수 있는 AnythingLLM 작업 공간의 slug 와 이름을 돌려줍니다. search 의 workspace 인자에 slug 를 씁니다."""
    settings = Settings.from_env()
    available = await _workspaces(settings)
    if not available:
        return "검색할 수 있는 AnythingLLM 작업 공간이 없습니다."
    lines = [f"AnythingLLM 작업 공간 {len(available)}개"]
    for w in available:
        mark = " (기본)" if w["slug"] == settings.workspace else ""
        name = f" — {w['name']}" if w["name"] and w["name"] != w["slug"] else ""
        lines.append(f"- {w['slug']}{name}{mark}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
