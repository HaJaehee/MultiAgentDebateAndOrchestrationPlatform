"""AnythingLLM 문서 검색 MCP 서버 (`mcp_servers/anythingllm_kb`, conf.json 의 `kb`).

진짜 AnythingLLM 대신 같은 경로·같은 모양으로 답하는 페이크(표준 라이브러리 HTTP 서버)를
띄워 확인합니다. 지키려는 것.

1. 검색 결과는 원문과 출처(제목·주소·점수)를 번호와 함께 돌려줍니다 — 에이전트가 인용할 수 있게.
2. 검색하지 못하면 도구 오류로 그 이유를 돌려줍니다. 결과를 지어내지 않습니다
   (키 없음·거부, 연결 실패, 모르는 작업 공간, 빈 질의).
3. 도구는 읽기 전용·닫힌 세계로 표시되어 MADO 의 도구 보안이 읽기로 판정합니다 — 물을 사람이
   없는 턴에서도, read_only 모드에서도 묻지 않고 실행됩니다.
4. 실제 도구 루프에서 `kb__search` 의 결과가 모델에게 관측으로 돌아가고, 도구 기록에 판정이 남습니다.
5. conf.example.json 의 `kb` 는 꺼져 있고 이 서버를 가리킵니다.
"""

import importlib.util
import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import pytest

import app.orchestration.tool_gate as gate_module
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.config import MCPServerConfig, RootConfig, load_config
from app.mcp import manager as manager_module
from app.mcp.manager import MCPManager
from app.mcp.policy import KNOWN_TOOLS, READ, profile_call
from app.orchestration.tool_gate import ToolGate

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp_servers" / "anythingllm_kb" / "server.py"


def _load_server():
    """패키지가 아닌 서버 파일을 모듈로 읽습니다. dataclass 가 자기 모듈을 찾으므로 먼저 등록합니다."""
    spec = importlib.util.spec_from_file_location("anythingllm_kb_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


kb = _load_server()


# ---------------------------------------------------------------------------
# 페이크 AnythingLLM
# ---------------------------------------------------------------------------

DOCS: Dict[str, List[Dict[str, Any]]] = {
    "mado-docs": [
        {
            "id": "c1",
            # AnythingLLM 은 조각 앞에 문서 정보를 이 머리로 붙여 둡니다.
            "text": "<document_metadata>\nsourceDocument: 반출규정.pdf\n</document_metadata>\n\n"
                    "외부 반출은 보안 담당자의 사전 승인이 필요하다.",
            "metadata": {
                "title": "반출규정.pdf", "url": "file://C:/docs/반출규정.pdf",
                "docSource": "a file uploaded by the user.", "chunkSource": "localfiles://반출규정.pdf",
            },
            "distance": 0.18, "score": 0.82,
        },
        {
            "id": "c2",
            "text": "승인 기록은 3년간 보관한다.",
            "metadata": {"title": "보관지침.md", "chunkSource": "custom-documents/보관지침.json"},
            "distance": 0.35, "score": 0.65,
        },
    ],
    "empty": [],
}
WORKSPACES = [
    {"id": 1, "name": "MADO 문서", "slug": "mado-docs", "threads": []},
    {"id": 2, "name": "빈 공간", "slug": "empty", "threads": []},
]


class FakeAnythingLLM:
    """`/api/v1/workspaces` 와 `/api/v1/workspace/{slug}/vector-search` 만 답합니다.

    응답 모양과 거절 문구는 AnythingLLM 의 `server/endpoints/api/workspace/index.js` 를 따릅니다.
    """

    def __init__(self) -> None:
        self.api_key = "test-key"
        self.requests: List[Tuple[str, str, Optional[Dict[str, Any]], Optional[str]]] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        # 기본 0.5초면 테스트마다 끄는 데 0.5초씩 기다립니다.
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def searches(self) -> List[Dict[str, Any]]:
        return [body or {} for method, path, body, _ in self.requests if path.endswith("/vector-search")]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {fake.api_key}"

            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path, None, self.headers.get("Authorization")))
                if not self._authorized():
                    return self._send(403, {"error": "No valid api key found."})
                if self.path == "/api/v1/workspaces":
                    return self._send(200, {"workspaces": WORKSPACES})
                return self._send(404, {"error": "Not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", self.path, body, self.headers.get("Authorization")))
                if not self._authorized():
                    return self._send(403, {"error": "No valid api key found."})
                match = re.fullmatch(r"/api/v1/workspace/([^/]+)/vector-search", self.path)
                if not match:
                    return self._send(404, {"error": "Not found"})
                slug = unquote(match.group(1))
                if slug not in DOCS:
                    return self._send(400, {"message": f"Workspace {slug} is not a valid workspace."})
                if not body.get("query"):
                    return self._send(400, {"message": "Query parameter cannot be empty."})
                if not DOCS[slug]:
                    return self._send(200, {"results": [], "message": "No embeddings found for this workspace."})
                threshold = float(body.get("scoreThreshold", 0.25))
                hits = [r for r in DOCS[slug] if r["score"] >= threshold]
                return self._send(200, {"results": hits[: int(body.get("topN", 4))]})

        return Handler


@pytest.fixture
def fake():
    server = FakeAnythingLLM()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def kb_env(monkeypatch, fake):
    """서버 함수를 이 프로세스에서 직접 부를 때의 환경. 개발 PC 의 ANYTHINGLLM_* 은 지웁니다."""
    for name in [n for n in os.environ if n.startswith("ANYTHINGLLM_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("ANYTHINGLLM_URL", fake.url)
    monkeypatch.setenv("ANYTHINGLLM_API_KEY", "test-key")
    monkeypatch.setenv("ANYTHINGLLM_WORKSPACE", "mado-docs")
    return monkeypatch


@pytest.fixture
def security(monkeypatch):
    """게이트와 매니저가 읽는 설정을 테스트가 정합니다 (개발 PC 의 conf.json 과 무관)."""
    config = RootConfig.model_validate({"agents": {"orchestrator": {"name": "O", "role": "r"}}})
    monkeypatch.setattr(gate_module, "get_config", lambda *a, **k: config)
    monkeypatch.setattr(manager_module, "get_config", lambda *a, **k: config)
    return config


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 1. 결과 — 원문과 출처
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_numbered_chunks_with_their_sources(kb_env, fake):
    out = await kb.search("반출 승인 절차")

    assert out.startswith('[AnythingLLM 작업 공간 `mado-docs`] "반출 승인 절차" — 조각 2개')
    assert "[1] 반출규정.pdf · 출처: file://C:/docs/반출규정.pdf · 점수 0.82" in out
    assert "외부 반출은 보안 담당자의 사전 승인이 필요하다." in out
    assert "<document_metadata>" not in out, "출처는 따로 적으므로 임베딩용 머리는 걷어냅니다"
    assert "[2] 보관지침.md · 출처: custom-documents/보관지침.json · 점수 0.65" in out
    assert fake.requests == [(
        "POST", "/api/v1/workspace/mado-docs/vector-search",
        {"query": "반출 승인 절차", "topN": 5}, "Bearer test-key",
    )]


@pytest.mark.asyncio
async def test_top_n_and_the_score_threshold_are_forwarded(kb_env, fake):
    kb_env.setenv("ANYTHINGLLM_SCORE_THRESHOLD", "0.7")

    out = await kb.search("반출", top_n=1)
    assert "[1] 반출규정.pdf" in out and "[2]" not in out
    await kb.search("반출", top_n=99)

    assert fake.searches() == [
        {"query": "반출", "topN": 1, "scoreThreshold": 0.7},
        {"query": "반출", "topN": kb.MAX_TOP_N, "scoreThreshold": 0.7},
    ]


@pytest.mark.asyncio
async def test_an_empty_workspace_is_reported_not_filled_in(kb_env):
    out = await kb.search("반출", workspace="empty")

    assert "찾은 조각이 없습니다 (AnythingLLM: No embeddings found for this workspace.)" in out
    assert "지어내지 마세요" in out and "[1]" not in out


# ---------------------------------------------------------------------------
# 2. 실패 — 이유를 그대로, 결과는 지어내지 않음
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_key_is_an_error(kb_env, fake):
    fake.api_key = "rotated"
    with pytest.raises(kb.KBError, match="API 키를 거부.*HTTP 403"):
        await kb.search("반출")


@pytest.mark.asyncio
async def test_a_missing_key_is_an_error_and_nothing_is_sent(kb_env, fake):
    kb_env.delenv("ANYTHINGLLM_API_KEY")
    with pytest.raises(kb.KBError, match="ANYTHINGLLM_API_KEY"):
        await kb.search("반출")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_an_unknown_workspace_passes_anythingllms_reason_through(kb_env):
    with pytest.raises(kb.KBError, match=r"HTTP 400.*nope is not a valid workspace"):
        await kb.search("반출", workspace="nope")


@pytest.mark.asyncio
async def test_an_empty_query_is_refused_before_asking(kb_env, fake):
    with pytest.raises(kb.KBError, match="비어 있습니다"):
        await kb.search("   ")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_an_unreachable_anythingllm_is_an_error(kb_env):
    kb_env.setenv("ANYTHINGLLM_URL", f"http://127.0.0.1:{_closed_port()}")
    kb_env.setenv("ANYTHINGLLM_TIMEOUT", "3")
    with pytest.raises(kb.KBError, match="연결하지 못해 검색하지 못했습니다"):
        await kb.search("반출")


@pytest.mark.asyncio
async def test_a_setting_that_is_not_a_number_is_named(kb_env):
    kb_env.setenv("ANYTHINGLLM_TOP_N", "many")
    with pytest.raises(kb.KBError, match="ANYTHINGLLM_TOP_N.*'many'"):
        await kb.search("반출")


@pytest.mark.asyncio
async def test_a_page_that_is_not_anythingllm_is_an_error(kb_env, fake):
    kb_env.setenv("ANYTHINGLLM_URL", fake.url + "/not-here")
    with pytest.raises(kb.KBError, match="HTTP 404"):
        await kb.search("반출")


# ---------------------------------------------------------------------------
# 작업 공간 — 기본값, 허용 목록
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_workspaces_limit_search_and_listing(kb_env, fake):
    kb_env.setenv("ANYTHINGLLM_WORKSPACES", "mado-docs")

    with pytest.raises(kb.KBError, match="'empty'은 검색할 수 없습니다"):
        await kb.search("반출", workspace="empty")
    assert fake.searches() == [], "허용 목록 밖은 묻지도 않습니다"

    listing = await kb.list_workspaces()
    assert listing == "AnythingLLM 작업 공간 1개\n- mado-docs — MADO 문서 (기본)"


@pytest.mark.asyncio
async def test_without_a_default_the_only_workspace_is_used_or_one_must_be_chosen(kb_env, fake):
    kb_env.delenv("ANYTHINGLLM_WORKSPACE")

    with pytest.raises(kb.KBError, match="여럿이라.*mado-docs, empty"):
        await kb.search("반출")

    kb_env.setenv("ANYTHINGLLM_WORKSPACES", "mado-docs")
    out = await kb.search("반출")
    assert out.startswith("[AnythingLLM 작업 공간 `mado-docs`]")


@pytest.mark.parametrize("raw, expected", [
    ("", "http://localhost:3001"),
    ("http://localhost:3001/", "http://localhost:3001"),
    ("http://kb.corp:3001/api", "http://kb.corp:3001"),
    ("https://kb.corp/api/v1/", "https://kb.corp"),
    ("localhost:3001", "http://localhost:3001"),
])
def test_the_address_is_normalized(raw, expected):
    assert kb.normalize_base_url(raw) == expected


# ---------------------------------------------------------------------------
# 3·4. MADO 를 거쳐서 — stdio, 도구 보안, 도구 루프
# ---------------------------------------------------------------------------


def _kb_manager(fake: FakeAnythingLLM, workspace: Path) -> MCPManager:
    env = {
        "ANYTHINGLLM_URL": fake.url,
        "ANYTHINGLLM_API_KEY": "test-key",
        "ANYTHINGLLM_WORKSPACE": "mado-docs",
        "PYTHONIOENCODING": "utf-8",
    }
    return MCPManager(
        {"kb": MCPServerConfig(command=sys.executable, args=[str(SERVER)], env=env)},
        workspace=workspace,
    )


@pytest.mark.asyncio
async def test_over_stdio_the_tools_are_judged_read_only_and_run_unattended(fake, security, tmp_path):
    manager = _kb_manager(fake, tmp_path)
    await manager.initialize()
    try:
        client = manager.clients["kb"]
        assert client.is_connected, client.last_error if hasattr(client, "last_error") else ""
        assert {t.name for t in client.tools} == {"search", "list_workspaces"}

        for tool in ("kb__search", "kb__list_workspaces"):
            # 판정표는 도구 이름만으로 찾습니다. 이름이 겹치면 annotations 대신 그 표의 등급을 받습니다.
            assert tool.split("__", 1)[1] not in KNOWN_TOOLS
            meta = manager.tool_meta(tool)
            assert meta is not None and meta.trusted
            assert meta.annotations["readOnlyHint"] is True
            assert meta.annotations["openWorldHint"] is False
            assert profile_call(meta, {}, tmp_path).risk == READ

        agent = Agent(key="architect", name="Architect", role="r", api_key="sk-test")
        for mode in ("default", "read_only"):
            gate = ToolGate(session_id="s", mode=mode, control=None)  # 물을 사람이 없습니다
            result = await gate.check(agent, "kb__search", {"query": "반출"}, manager)
            assert result.allowed, result.output
            assert result.audit["decision"] == "allow" and result.audit["rule"] == f"mode:{mode}"

        output, status = await manager.execute_tool("kb__search", {"query": "반출 승인 절차"})
        assert status == "success"
        assert "[1] 반출규정.pdf" in output and "사전 승인" in output

        listing, status = await manager.execute_tool("kb__list_workspaces", {})
        assert status == "success" and "- mado-docs — MADO 문서 (기본)" in listing

        fake.api_key = "rotated"
        output, status = await manager.execute_tool("kb__search", {"query": "반출 승인 절차"})
        assert status == "error"
        assert "API 키를 거부" in output and "[1]" not in output
    finally:
        await manager.shutdown()


class _Msg(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                 for c in self.tool_calls]
        return out


def _call(cid: str, name: str, arguments: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id=cid, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)))


class _Scripted(LLMCaller):
    """모델 자리에 각본을 넣습니다 (`_complete_unthrottled` — MADOWarGame 의 가짜 두뇌와 같은 지점)."""

    def __init__(self, manager: MCPManager, script: List[Dict[str, Any]]):
        super().__init__(mcp_manager=manager)
        self.script = list(script)
        self.sent: List[Dict[str, Any]] = []

    async def _complete_unthrottled(self, agent, messages, tools, on_chunk, tool_choice):
        self.sent.append({"messages": [dict(m) for m in messages], "tools": list(tools or [])})
        step = self.script.pop(0)
        return _Msg(content=step.get("content", ""), tool_calls=step.get("calls"), reasoning_content=None), "stop"


@pytest.mark.asyncio
async def test_in_the_tool_loop_the_chunks_reach_the_model_and_the_verdict_is_recorded(fake, security, tmp_path):
    manager = _kb_manager(fake, tmp_path)
    await manager.initialize()
    try:
        caller = _Scripted(manager, [
            {"content": "규정을 찾아보겠습니다.",
             "calls": [_call("k1", "kb__search", {"query": "외부 반출 승인"})]},
            {"content": "[1] 반출규정.pdf 에 따르면 사전 승인이 필요합니다."},
        ])
        agent = Agent(key="architect", name="Architect", role="r", model="openai/gpt-4o",
                      api_key="sk-test", allowed_mcp_servers=["kb"])

        content, logs = await caller.call_agent(
            agent, [{"role": "user", "content": "외부 반출 절차를 정리해 줘"}],
            mcp=manager, tool_gate=ToolGate(session_id="s", control=None),
        )

        offered = {t["function"]["name"] for t in caller.sent[0]["tools"]}
        assert {"kb__search", "kb__list_workspaces"} <= offered
        assert len(logs) == 1
        assert logs[0]["tool_name"] == "kb__search" and logs[0]["status"] == "success"
        assert logs[0]["security"]["decision"] == "allow"
        observation = next(m for m in caller.sent[1]["messages"] if m.get("role") == "tool")
        assert "반출규정.pdf" in observation["content"] and "사전 승인" in observation["content"]
        assert "사전 승인이 필요합니다" in content
        assert fake.searches()[-1]["query"] == "외부 반출 승인"
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# 5. 설정 예시
# ---------------------------------------------------------------------------


def test_the_example_config_ships_kb_switched_off_and_pointing_at_this_server():
    cfg = load_config(ROOT / "conf.example.json")
    kb_cfg = cfg.mcp_servers["kb"]

    assert kb_cfg.enabled is False and not kb_cfg.is_remote
    assert (ROOT / kb_cfg.args[0]).resolve() == SERVER.resolve()
    assert {"ANYTHINGLLM_URL", "ANYTHINGLLM_API_KEY", "ANYTHINGLLM_WORKSPACE"} <= set(kb_cfg.env)
    assert "kb" in cfg.agents["architect"].allowed_mcp_servers
