"""AnythingLLM 사내/로컬 문서 검색 MCP 서버 (`kb`).

AnythingLLM 작업 공간(Workspace)에 적재된 문서에서 질의와 유사한 텍스트 청크를 검색하여 **원문과 근거 출처**를 제공합니다.
MADO 토론 에이전트가 `kb__search` 도구로 호출합니다 (conf.json 의 `mcp_servers.kb`).

AnythingLLM 개발자 API 중 벡터 유사도 검색(`POST /api/v1/workspace/{slug}/vector-search`)과 작업 공간 목록 조회
(`GET /api/v1/workspaces`) 엔드포인트만 사용합니다. 본 서버는 검색된 원문 청크와 근거 메타데이터만 제공하며,
최종 응답은 토론 에이전트가 자체 모델 맥락에서 직접 작성합니다 (AnythingLLM 채팅 API 사용 시 외부 LLM 개입으로 인한
출처 불투명성 및 MADO 토큰 계측/폴백 제어 이탈 방지).

설정은 환경변수를 통해 전달됩니다. MADO는 보안 격리를 위해 상위 환경변수를 직접 상속하지 않으므로,
conf.json의 `env` 블록에 명시적으로 지정해야 합니다 (`"ANYTHINGLLM_API_KEY": "${ANYTHINGLLM_API_KEY}"`).

  ANYTHINGLLM_URL              AnythingLLM 서버 주소 (기본값: http://localhost:3001, 끝에 /api 또는 /api/v1 허용)
  ANYTHINGLLM_API_KEY          개발자 API 키 (필수, AnythingLLM 설정 -> Developer API 메뉴에서 발급)
  ANYTHINGLLM_WORKSPACE        기본 대상 작업 공간 slug (생략 시 단일 작업 공간 자동 선택)
  ANYTHINGLLM_WORKSPACES       검색을 허용할 작업 공간 slug 목록 (쉼표 구분, 생략 시 전체 허용)
  ANYTHINGLLM_TOP_N            단일 검색 시 반환할 청크 수 기본값 (기본 5, 최대 20)
  ANYTHINGLLM_SCORE_THRESHOLD  유사도 점수 임계값 (0.0~1.0, 미달 항목 제외, 생략 시 서버 설정 준용)
  ANYTHINGLLM_TIMEOUT          HTTP 요청 대기 제한 시간(초, 기본 30)

검색 실패 시 환각 없이 명확한 예외 메시지를 반환합니다 (API 키 누락/거부, 연결 실패, 미등록 작업 공간 등).

두 도구 모두 어노테이션에 읽기 전용·닫힌 세계(`readOnlyHint=True`, `openWorldHint=False`)가 명시되어 있어
MADO 도구 보안 엔진에서 안전한 읽기(read) 권한으로 판정되며, 사용자 승인 팝업 없이 즉시 실행됩니다.

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

# AnythingLLM이 청크 임베딩 시 첨부하는 <document_metadata> 헤더 블록을 제거합니다 (출처는 별도 표기).
_HEADER_META = re.compile(r"^\s*<document_metadata>.*?</document_metadata>\s*", re.DOTALL)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class KBError(Exception):
    """AnythingLLM 문서 검색 실패 시 발생하는 예외 클래스 (에이전트에 도구 실행 오류로 반환)."""


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
        """도구 호출 시마다 환경변수를 동적으로 재평가합니다 (재기동 없이 설정 변경 반영)."""
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
        raise KBError(f"{name} 환경변수 값이 유효한 숫자가 아닙니다: '{raw}'. conf.json의 mcp_servers.kb.env 설정을 확인하세요.") from None


def normalize_base_url(url: str) -> str:
    """URL 형식 정규화: `http://host:3001`, `.../api`, `.../api/v1/`, `host:3001` 등을 표준 베이스 URL로 통일합니다."""
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
            "ANYTHINGLLM_API_KEY가 설정되지 않아 검색할 수 없습니다. AnythingLLM 관리자 설정의 Developer API에서 "
            "키를 발급받아 .env에 등록하고, conf.json의 mcp_servers.kb.env에 ${ANYTHINGLLM_API_KEY}를 전달하세요."
        )
    url = f"{settings.base_url}/api/v1{path}"
    # 로컬 루프백 호스트(localhost, 127.0.0.1) 연결 시 사내 프록시(HTTP_PROXY 등) 우회 설정.
    # 프록시가 활성화된 PC에서도 로컬 엔드포인트에 안정적으로 직결되도록 처리합니다.
    try:
        trust_env = httpx.URL(settings.base_url).host not in _LOOPBACK_HOSTS
    except httpx.InvalidURL:
        raise KBError(f"ANYTHINGLLM_URL 형식이 올바르지 않습니다: '{settings.base_url}'.") from None
    try:
        async with httpx.AsyncClient(timeout=settings.timeout, trust_env=trust_env) as client:
            response = await client.request(
                method, url, json=body,
                headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        raise KBError(
            f"AnythingLLM 서버({settings.base_url}) 응답 시간이 초과되었습니다 ({settings.timeout:g}초 대기)."
        ) from None
    except httpx.HTTPError as exc:
        raise KBError(
            f"AnythingLLM 서버({settings.base_url})에 연결할 수 없습니다 ({type(exc).__name__}). "
            "서버 주소 및 AnythingLLM 서비스 구동 상태를 확인하세요."
        ) from None

    if response.status_code in (401, 403):
        raise KBError(
            f"AnythingLLM 서버가 API 키를 거부했습니다 (HTTP {response.status_code}). "
            "ANYTHINGLLM_API_KEY 설정을 확인하세요."
        )
    if response.status_code >= 400:
        raise KBError(
            f"AnythingLLM 서버가 요청을 거부했습니다 (HTTP {response.status_code}): "
            f"{_reason(response)}"
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise KBError(
            f"AnythingLLM 서버 응답을 파싱할 수 없습니다 (JSON 형식이 아님). "
            f"ANYTHINGLLM_URL({settings.base_url})이 올바른 엔드포인트인지 확인하세요."
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
                f"작업 공간 '{slug}'은 검색할 수 없습니다. 검색 가능한 작업 공간: {', '.join(settings.allowed)}"
            )
        return slug
    available = await _workspaces(settings)
    if len(available) == 1:
        return available[0]["slug"]
    if not available:
        raise KBError("검색 가능한 AnythingLLM 작업 공간이 없습니다. AnythingLLM에서 작업 공간을 생성하고 문서를 적재해 주세요.")
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
            "질의 표현을 바꾸어 다시 검색하거나, 문서에서 근거를 찾지 못했다고 답변하세요. 결과에 없는 내용을 지어내지 마세요."
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
    """AnythingLLM 작업 공간의 문서에서 질의와 유사한 청크를 검색하여 원문과 근거 출처를 반환합니다.

    사내 규정, 아키텍처 설계서, 보고서 등 지식 문서를 토론 근거 자료로 인용할 때 사용합니다. query에는
    검색할 내용을 문장이나 핵심 키워드로 지정합니다 (예: "외부 반출 승인 절차"). top_n은 반환받을 청크 수
    (1~20, 0이면 기본 설정값), workspace는 대상 작업 공간 슬러그(slug)입니다 (생략 시 기본 작업 공간 선택,
    전체 목록은 list_workspaces 참조). 답변 시 [번호]와 문서 제목을 밝혀 근거를 제시하고, 결과에 없는 내용을
    임의로 지어내지 마세요. 일치하는 결과가 없으면 질의 표현을 재구성하거나 근거를 찾지 못했다고 명시하세요.
    """
    settings = Settings.from_env()
    text = (query or "").strip()
    if not text:
        raise KBError("query 검색어가 비어 있습니다. 검색할 내용을 입력하세요.")
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
    """검색 가능한 AnythingLLM 작업 공간 목록(slug 및 이름)을 조회합니다. search 도구의 workspace 인자에 slug 값을 지정할 수 있습니다."""
    settings = Settings.from_env()
    available = await _workspaces(settings)
    if not available:
        return "검색 가능한 AnythingLLM 작업 공간이 없습니다."
    lines = [f"AnythingLLM 작업 공간 {len(available)}개"]
    for w in available:
        mark = " (기본)" if w["slug"] == settings.workspace else ""
        name = f" — {w['name']}" if w["name"] and w["name"] != w["slug"] else ""
        lines.append(f"- {w['slug']}{name}{mark}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
