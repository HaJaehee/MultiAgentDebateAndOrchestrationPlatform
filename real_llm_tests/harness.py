"""실제 LLM 시험 하네스.

대상(기본: `dist/MultiAgentDebateOrchestration_bundle`)의 코드를 **그 대상의 파이썬·MCP 서버·.env 연결**로 돌리되,
대상 폴더의 사용자 자료(conf.json, multiagent.db, workspace/, data/)는 건드리지 않습니다.

방법은 "미러 루트" 입니다. 실행 폴더(`<run>/root`)에 대상의 `app/` 등 코드만 복사하고 그 폴더를 프로젝트 루트로
삼습니다. `PROJECT_ROOT` 가 `app/` 의 부모라서, data/·workspace/·strategies/·host_tools/ 가 전부 실행 폴더
아래에 생깁니다. node 런타임·MCP 서버·파이썬 런타임처럼 읽기만 하는 큰 폴더는 환경 변수로 대상을 가리킵니다
(`run_mado.bat` 이 하는 일과 같습니다). 코드가 같은지는 파일 해시로 확인해 보고서에 남깁니다.

LLM 요청은 가로채 기록만 하고 그대로 보냅니다 (`litellm.acompletion`, `LLMCaller._complete_unthrottled`,
`litellm.stream_chunk_builder`, `DecisionCaller._post_systemone` 류). 대역은 쓰지 않습니다.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_TARGET = REPO / "dist" / "MultiAgentDebateOrchestration_bundle"
FIXTURES = HERE / "fixtures"

# 대상에서 미러로 복사하는 것 (코드와, 코드가 프로젝트 루트 기준으로 찾는 작은 자료).
MIRRORED = ("app", "skills", "trial_templates", "mcp_servers", "docs/examples", "conf.example.json")
# 대상의 예제 플러그인을 미러의 플러그인 폴더에 놓습니다 (사용자가 번들에서 하는 설치와 같은 동작, ADR-042).
EXAMPLE_PLUGINS = {
    "docs/examples/plugins/strategies/consensus_voting.py": "strategies/consensus_voting.py",
    "docs/examples/plugins/host_tools/glossary.py": "host_tools/glossary.py",
    "docs/examples/plugins/host_tools/ticket.py": "host_tools/ticket.py",
}

_CURRENT_AGENT: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("real_llm_agent", default=None)


# --------------------------------------------------------------------------- 기록


@dataclass
class LLMRequest:
    """실제로 보낸 LLM 요청 하나 (`litellm.acompletion` 인자)."""

    index: int
    at: float
    agent: Optional[str]
    model: str
    messages: List[Dict[str, Any]]
    tool_names: List[str]
    tools: List[Dict[str, Any]]
    stream: bool
    db_checked_out: Optional[int]
    error: str = ""
    seconds: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)
    response_text: str = ""
    response_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    assembled_on_main_thread: Optional[bool] = None

    def text(self) -> str:
        """요청의 모든 메시지 글 (검색용)."""
        return "\n".join(_message_text(m) for m in self.messages)

    def last_user(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") == "user":
                return _message_text(message)
        return ""

    def system(self) -> str:
        return "\n".join(_message_text(m) for m in self.messages if m.get("role") == "system")


@dataclass
class Assembly:
    """스트림 조립(`stream_chunk_builder`) 한 번."""

    thread: str
    on_main_thread: bool
    seconds: float
    usage: Dict[str, Any]
    chunks: int


@dataclass
class DecisionCall:
    """System 1(Jev/Laya) 호출 하나."""

    url: str
    payload: Dict[str, Any]
    status: Optional[int] = None
    response: Any = None
    error: str = ""
    seconds: float = 0.0


@dataclass
class Check:
    adr: str
    name: str
    ok: bool
    detail: str = ""
    test: str = ""


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    parts.append(f"<{block.get('type', 'block')}>")
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _shrink(value: Any, limit: int = 400) -> Any:
    """기록용 사본: base64 같은 긴 덩어리는 길이만 남깁니다."""
    if isinstance(value, str):
        if len(value) > 2000 and (value.startswith("data:") or " " not in value[:1000]):
            return f"<{len(value)} chars: {value[:60]}…>"
        return value
    if isinstance(value, dict):
        return {k: _shrink(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, limit) for v in value]
    return value


# --------------------------------------------------------------------------- 하네스


class Harness:
    """한 pytest 실행에 하나. 대상 코드를 미러 루트에서 세우고 실제 요청을 기록합니다."""

    def __init__(self, target: Path, run_dir: Path, code: Optional[Path] = None) -> None:
        # `target` 은 환경(파이썬·node·MCP·샌드박스·conf.json·.env)을, `code` 는 미러로 복사할 코드를 줍니다.
        self.target = target
        self.code = code or target
        self.run_dir = run_dir
        self.root = run_dir / "root"
        self.workspaces = run_dir / "workspaces"
        self.records_dir = run_dir / "records"
        self.conf_path = self.root / "conf.json"
        self.db_path = run_dir / "mado.db"
        self.requests: List[LLMRequest] = []
        self.assemblies: List[Assembly] = []
        self.decisions: List[DecisionCall] = []
        self.checks: List[Check] = []
        self.notes: List[Dict[str, str]] = []
        self.current_test = ""
        self.loop = asyncio.new_event_loop()
        self.engine: Any = None
        self.code_identity: Dict[str, Any] = {}
        self._main_thread = threading.get_ident()
        self._failed_before = 0
        # 도구 실행 앞뒤에서 "서버가 죽었다" 를 흉내 내는 훅 (`crash_turn`). 한 번 걸리면 스스로 풀립니다.
        self.inherits_system1 = False
        # 대상 conf 에 없어 `conf.example.json` 에서 채워 넣은 표준 에이전트 키 (`_write_conf`).
        self.supplemented_agents: List[str] = []
        self.fault: Optional[Callable[[str, Any, str, Dict[str, Any]], bool]] = None

    @property
    def s2(self) -> str:
        """생성형(System 2) 모델의 이름. 확인·관찰 문구에 씁니다 — 어느 모델로 돌린 기록인지 보고서만 보고 알 수 있게."""
        model = str(self.code_identity.get("model") or "")
        return model.split("/", 1)[-1] or "System 2 모델"

    # ------------------------------------------------------------------ 세우기

    def bootstrap(self) -> "Harness":
        if "app" in sys.modules:
            raise RuntimeError("`app` 이 이미 불려 있습니다. 하네스보다 먼저 app 을 import 하면 대상을 고를 수 없습니다.")
        if not (self.code / "app" / "main.py").is_file():
            raise RuntimeError(f"코드 폴더에 app/ 이 없습니다: {self.code}")
        if not (self.target / "conf.json").is_file():
            raise RuntimeError(f"대상 폴더에 conf.json 이 없습니다: {self.target}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for folder in (self.root, self.workspaces, self.records_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._mirror()
        self._environment()
        self._write_conf()
        os.chdir(self.root)
        sys.path.insert(0, str(self.root))
        asyncio.set_event_loop(self.loop)
        self.run(self._start(), timeout=300)
        return self

    def _mirror(self) -> None:
        for name in MIRRORED:
            source, dest = self.code / name, self.root / name
            if not source.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
        for source, dest in EXAMPLE_PLUGINS.items():
            if (self.code / source).is_file():
                (self.root / dest).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.code / source, self.root / dest)
        code_digest = tree_digest(self.code / "app")
        self.code_identity = {
            "target": str(self.target),
            "code": str(self.code),
            "app_tree_sha256": code_digest,
            "mirror_matches_code": code_digest == tree_digest(self.root / "app"),
            "code_is_target_code": code_digest == tree_digest(self.target / "app"),
        }

    def _environment(self) -> None:
        """대상의 `.env` 와 `run_mado.bat` 이 주는 환경을 그대로 만듭니다 (개발 PC 의 값이 섞이지 않게 빈 값까지)."""
        from dotenv import dotenv_values

        env_file = self.target / ".env"
        if env_file.is_file():
            for key, value in dotenv_values(env_file).items():
                os.environ[key] = value or ""
        node = self.target / "node_runtime" / "node.exe"
        if node.is_file():
            os.environ["NODE_BIN"] = str(node)
            os.environ["PATH"] = f"{node.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["PYTHON_BIN"] = sys.executable
        os.environ["SANDBOX_KERNEL_PYTHON"] = os.environ.get("SANDBOX_KERNEL_PYTHON") or sys.executable
        os.environ["MCP_NODE_HOME"] = str(self.target / "mcp_node")
        os.environ["MCP_SANDBOX_HOME"] = str(self.target / "mcp_sandbox")
        os.environ["WORKSPACE_DIR"] = str(self.root / "workspace")
        os.environ["APP_DB_URL"] = ""
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["PYTHONUTF8"] = "1"

    def _write_conf(self) -> None:
        """대상의 conf.json 을 바탕으로 시험용 사본을 만듭니다. 코드가 아니라 설정만 다릅니다."""
        raw = json.loads((self.target / "conf.json").read_text(encoding="utf-8-sig"))
        raw.setdefault("app", {})["db_url"] = f"sqlite+aiosqlite:///{self.db_path.as_posix()}"
        # 같은 연결의 다른 모델로 돌려 볼 때 (폐쇄망에서 모델 여럿을 차례로 받아 볼 때). 연결 정보는 대상의 것 그대로입니다.
        model_override = os.environ.get("MADO_REAL_LLM_MODEL", "").strip()
        if model_override:
            raw.setdefault("llm", {})["model"] = model_override
        servers = raw.setdefault("mcp_servers", {})
        # 실제 웹 검색은 시험을 비결정적으로 만들고 밖으로 나갑니다. 끕니다.
        if "web_search" in servers:
            servers["web_search"]["enabled"] = False
        # 외부 내용·도구 미디어 시험용 로컬 stdio 서버 (ADR-035, ADR-047).
        servers["webdocs"] = {
            "command": "${PYTHON_BIN:-python}",
            "args": [str(FIXTURES / "webdocs_server.py")],
        }
        security = raw.setdefault("tool_security", {})
        security["allow"] = list(security.get("allow") or []) + ["mcp(webdocs/fetch_page)"]
        security["untrusted_sources"] = list(security.get("untrusted_sources") or []) + ["mcp(webdocs/read_doc)"]
        agents = raw.setdefault("agents", {})
        self._supplement_standard_agents(agents)
        agents["rt_reader"] = {
            "name": "Log Reader", "role": "Reads files and reports facts",
            "temperature": 0.0, "max_tokens": 1500, "max_context_window": 24000,
            "debate_priority": 60, "allowed_mcp_servers": ["filesystem"],
            "system_prompt": "파일을 읽고 사실만 보고합니다. 읽지 않은 내용은 추측하지 않습니다.",
        }
        agents["rt_researcher"] = {
            "name": "Web Researcher", "role": "Fetches outside documents and summarizes them",
            "temperature": 0.0, "debate_priority": 61,
            "allowed_mcp_servers": ["webdocs", "sandbox", "filesystem"],
            "system_prompt": "지시된 문서를 도구로 가져와 요약합니다.",
        }
        agents["rt_viewer"] = {
            "name": "Media Viewer", "role": "Reads attached images and documents",
            "temperature": 0.0, "debate_priority": 62,
            "allowed_mcp_servers": ["webdocs"], "allowed_modalities": ["image", "document"],
            "system_prompt": "첨부된 그림과 문서를 직접 보고 사실만 답합니다.",
        }
        agents["rt_plugin_user"] = {
            "name": "Plugin User", "role": "Uses host tool plugins",
            "temperature": 0.0, "debate_priority": 63,
            "allowed_mcp_servers": ["glossary", "ticket"],
            "system_prompt": "용어는 도구로 확인하고 답합니다.",
        }
        # 대상 conf 의 전역 `llm.decision_llm.enabled` 가 켜져 있으면, 따로 끄지 않은 에이전트는 전부 System 1(Jev)
        # 판정 에이전트가 됩니다 (도구도 생성형 모델도 쓰지 않습니다). 시험용 전문가는 System 2 로 고정합니다.
        for key, agent in agents.items():
            if key.startswith("rt_"):
                agent["decision_llm"] = {"enabled": False}
        self.inherits_system1 = bool(((raw.get("llm") or {}).get("decision_llm") or {}).get("enabled"))
        self.conf_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _supplement_standard_agents(self, agents: Dict[str, Any]) -> None:
        """시나리오가 이름으로 부르는 표준 에이전트(architect·coder·critic)가 대상 conf 에 없으면 예제 설정에서 채웁니다.

        번들은 `conf.example.json` 으로 conf 를 만들기 때문에 보통은 다 있습니다. 운영 conf 에서 지웠거나 이름을
        바꾼 경우에만 채우고, 있는 것은 건드리지 않습니다 (꺼 둔 것은 이 사본에서만 켭니다). 연결은 전역 `llm` 을
        물려받게 개별 모델·주소·키는 비웁니다 — 시험하려는 것은 대상의 모델입니다.
        """
        example_path = self.code / "conf.example.json"
        if not example_path.is_file():
            return
        example = json.loads(example_path.read_text(encoding="utf-8-sig")).get("agents") or {}
        for key in ("architect", "coder", "critic"):
            current = agents.get(key)
            if isinstance(current, dict):
                if current.get("enabled") is False:
                    current["enabled"] = True
                    self.supplemented_agents.append(f"{key}(켬)")
                continue
            template = example.get(key)
            if not isinstance(template, dict):
                continue
            agents[key] = {
                name: value for name, value in template.items()
                if name not in ("model", "api_key", "api_base", "api_version", "provider") and not name.startswith("//")
            }
            self.supplemented_agents.append(key)

    async def _start(self) -> None:
        from functools import partial

        from app.config import PROJECT_ROOT, get_config
        from app.database.session import get_engine, get_session_factory, init_db

        if Path(PROJECT_ROOT).resolve() != self.root.resolve():
            raise RuntimeError(f"프로젝트 루트가 미러가 아닙니다: {PROJECT_ROOT}")
        cfg = get_config(reload=True, config_path=self.conf_path)
        engine = get_engine(cfg.app.db_url)
        bound = engine.url.database or ""
        if Path(bound).resolve() != self.db_path.resolve():
            raise RuntimeError(f"DB 가 실행 폴더에 묶이지 않았습니다: {bound}")
        await init_db(cfg.app.db_url)

        from app.plugins import load_plugins

        await asyncio.to_thread(load_plugins)

        from app.mcp.pool import get_runtime_pool
        from app.orchestration.engine import get_orchestrator_engine
        from app.session_ops import maintain_workspace

        get_runtime_pool().idle_hook = partial(maintain_workspace, get_session_factory())
        self.engine = get_orchestrator_engine()
        self._install_recorders()
        self.code_identity["model"] = cfg.llm.model or os.environ.get("LLM_MODEL", "")
        self.code_identity["supplemented_agents"] = list(self.supplemented_agents)
        self.code_identity["decision_backend"] = cfg.llm.decision_llm.backend
        self.code_identity["python"] = sys.version.split()[0]
        from importlib.metadata import version

        self.code_identity["packages"] = {
            name: version(name) for name in ("litellm", "sqlalchemy", "nicegui", "mcp")
        }

    # ------------------------------------------------------------------ 요청 기록

    def _install_recorders(self) -> None:
        import litellm

        from app.agents.llm import LLMCaller
        from app.database.session import get_engine

        harness = self
        original_acompletion = litellm.acompletion
        original_builder = litellm.stream_chunk_builder
        original_complete = LLMCaller._complete_unthrottled

        def _checked_out() -> Optional[int]:
            try:
                return int(get_engine().sync_engine.pool.checkedout())
            except Exception:  # noqa: BLE001 - 풀 종류에 따라 없을 수 있습니다
                return None

        async def recording_complete(self, agent, messages, tools, on_chunk, tool_choice):
            token = _CURRENT_AGENT.set(getattr(agent, "key", None))
            try:
                return await original_complete(self, agent, messages, tools, on_chunk, tool_choice)
            finally:
                _CURRENT_AGENT.reset(token)

        async def recording_acompletion(*args, **kwargs):
            tools = kwargs.get("tools") or []
            record = LLMRequest(
                index=len(harness.requests),
                at=time.time(),
                agent=_CURRENT_AGENT.get(),
                model=str(kwargs.get("model")),
                messages=_shrink(copy.deepcopy(kwargs.get("messages") or [])),
                tool_names=[(t.get("function") or {}).get("name", "") for t in tools],
                tools=copy.deepcopy(tools),
                stream=bool(kwargs.get("stream")),
                db_checked_out=_checked_out(),
            )
            harness.requests.append(record)
            began = time.monotonic()
            try:
                return await original_acompletion(*args, **kwargs)
            except Exception as exc:
                record.error = f"{type(exc).__name__}: {exc}"[:500]
                raise
            finally:
                record.seconds = time.monotonic() - began

        def recording_builder(chunks, *args, **kwargs):
            began = time.monotonic()
            result = original_builder(chunks, *args, **kwargs)
            usage = getattr(result, "usage", None)
            try:
                usage_dict = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage or {})
            except Exception:  # noqa: BLE001
                usage_dict = {}
            on_main = threading.get_ident() == harness._main_thread
            harness.assemblies.append(Assembly(
                thread=threading.current_thread().name,
                on_main_thread=on_main,
                seconds=time.monotonic() - began,
                usage=_shrink(usage_dict),
                chunks=len(chunks or []),
            ))
            # 이 조립이 어느 요청의 것인지: 같은 에이전트의, 아직 답이 붙지 않은 가장 최근 요청.
            # (`asyncio.to_thread` 가 컨텍스트 변수를 스레드로 옮겨 주므로 여기서도 발언자를 압니다.)
            agent = _CURRENT_AGENT.get()
            for record in reversed(harness.requests):
                if record.agent == agent and record.assembled_on_main_thread is None:
                    record.assembled_on_main_thread = on_main
                    record.usage = _shrink(usage_dict)
                    try:
                        message = result.choices[0].message
                        record.response_text = str(getattr(message, "content", "") or "")
                        record.response_tool_calls = [
                            {"name": call.function.name, "arguments": call.function.arguments}
                            for call in (getattr(message, "tool_calls", None) or [])
                        ]
                    except Exception:  # noqa: BLE001
                        pass
                    break
            return result

        original_execute = LLMCaller._execute_tool_safely

        async def faulting_execute(self, agent, fn_name, fn_args, session_id, mcp=None, tools=None, loop=None):
            """도구 실행 앞이나 뒤에서 태스크가 취소된 것처럼 끊습니다 (서버 종료·프로세스 죽음의 재현)."""
            fault = harness.fault
            if fault is not None and fault("before", agent, fn_name, fn_args):
                harness.fault = None
                raise asyncio.CancelledError("real-llm fault: before the tool ran")
            result = await original_execute(self, agent, fn_name, fn_args, session_id, mcp, tools=tools, loop=loop)
            fault = harness.fault
            if fault is not None and fault("after", agent, fn_name, fn_args):
                harness.fault = None
                raise asyncio.CancelledError("real-llm fault: after the tool ran")
            return result

        LLMCaller._execute_tool_safely = faulting_execute
        litellm.acompletion = recording_acompletion
        litellm.stream_chunk_builder = recording_builder
        LLMCaller._complete_unthrottled = recording_complete
        self._install_decision_recorder()

    def _install_decision_recorder(self) -> None:
        """System 1 호출(httpx POST)을 기록합니다. 보내는 내용과 받은 내용을 그대로 둡니다."""
        import httpx

        harness = self
        original_post = httpx.AsyncClient.post

        async def recording_post(client, url, *args, **kwargs):
            text = str(url)
            if "systemone" not in text and "/predict" not in text:
                return await original_post(client, url, *args, **kwargs)
            call = DecisionCall(url=text, payload=_shrink(copy.deepcopy(kwargs.get("json") or {})))
            harness.decisions.append(call)
            began = time.monotonic()
            try:
                response = await original_post(client, url, *args, **kwargs)
            except Exception as exc:
                call.error = f"{type(exc).__name__}: {exc}"[:500]
                raise
            finally:
                call.seconds = time.monotonic() - began
            call.status = response.status_code
            try:
                call.response = _shrink(response.json())
            except Exception:  # noqa: BLE001
                call.response = response.text[:2000]
            return response

        httpx.AsyncClient.post = recording_post

    # ------------------------------------------------------------------ 돌리기

    def run(self, coro: Awaitable[Any], timeout: float = 1500) -> Any:
        """하나뿐인 이벤트 루프에서 코루틴을 돌립니다 (DB 엔진·MCP 프로세스가 루프에 묶여 있어 루프를 바꾸지 않습니다)."""
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))

    def workspace(self, name: str, files: Optional[Dict[str, str | bytes]] = None) -> Path:
        path = self.workspaces / name
        path.mkdir(parents=True, exist_ok=True)
        for rel, content in (files or {}).items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        return path

    async def new_session(
        self, name: str, *, agents: Sequence[str], strategy: str = "sequential_debate", max_rounds: int = 2,
        files: Optional[Dict[str, str | bytes]] = None, **options: Any,
    ) -> str:
        from app.headless import create_session

        hands_off = options.pop("orchestrator_hands_off", False)
        workspace = self.workspace(name, files)
        created = await create_session(
            title=f"[real-llm] {name}", strategy=strategy, max_rounds=max_rounds, agents=list(agents),
            workspace_dir=str(workspace), **options,
        )
        if hands_off:
            await self.freeze_agent(created["session_id"], "orchestrator", allowed_mcp_servers=[])
        return created["session_id"]

    async def freeze_agent(self, session_id: str, agent_key: str, **updates: Any) -> Dict[str, Any]:
        """이 대화에서만 한 에이전트의 설정을 바꿔 고정합니다 (conf 와 다른 대화는 그대로).

        `orchestrator_hands_off` 가 쓰는 길입니다. 번들 conf 의 오케스트레이터는 filesystem 도구를 갖고 있어, 실제 모델이
        계획 단계에서 전문가의 일을 직접 해 버리곤 합니다 (파일을 읽고, 심지어 고칩니다). 전문가의 도구 동작을 보려는
        시험에서는 오케스트레이터의 도구를 떼어 그 동작이 전문가의 발언에서 일어나게 합니다.
        """
        from sqlalchemy import select

        from app.agents.personas import freeze_personas
        from app.agents.pool import get_agent_pool
        from app.database.models import SessionAgentModel, SessionModel

        async with self.session_factory()() as db:
            session = await db.get(SessionModel, session_id)
            await freeze_personas(db, session, get_agent_pool())
            rows = (await db.execute(
                select(SessionAgentModel).where(SessionAgentModel.session_id == session_id)
            )).scalars().all()
            frozen: Dict[str, Any] = {}
            for row in rows:
                if row.agent_key == agent_key and isinstance(row.config_snapshot, dict):
                    snapshot = dict(row.config_snapshot)
                    for key, value in updates.items():
                        if isinstance(value, dict) and isinstance(snapshot.get(key), dict):
                            snapshot[key] = {**snapshot[key], **value}
                        else:
                            snapshot[key] = value
                    row.config_snapshot = snapshot
                    frozen = snapshot
            await db.commit()
            return frozen

    async def turn(
        self, session_id: str, prompt: str, *, timeout: float = 1500, **kwargs: Any,
    ) -> "TurnOutcome":
        """무인(unattended) 턴 하나를 실제 LLM 으로 돌립니다. 이벤트·요청·루프 지연을 함께 모읍니다."""
        events: List[Dict[str, Any]] = []
        chunks = 0

        async def on_event(event: Dict[str, Any]) -> None:
            nonlocal chunks
            if event.get("type") == "message_stream_chunk":
                chunks += 1
                return
            events.append({k: v for k, v in event.items() if k not in ("messages",)})

        first_request = len(self.requests)
        first_assembly = len(self.assemblies)
        first_decision = len(self.decisions)
        lag = LoopLag()
        watcher = asyncio.ensure_future(lag.watch())
        began = time.monotonic()
        error: Optional[BaseException] = None
        state = None
        try:
            state = await asyncio.wait_for(
                self.engine.run_turn(session_id=session_id, user_prompt=prompt, on_event=on_event, **kwargs),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 실패도 결과로 돌려줍니다
            error = exc
        finally:
            watcher.cancel()
        return TurnOutcome(
            session_id=session_id, state=state, error=error, events=events, stream_chunks=chunks,
            requests=self.requests[first_request:], assemblies=self.assemblies[first_assembly:],
            decisions=self.decisions[first_decision:], seconds=time.monotonic() - began, lag=lag,
        )

    async def crash_turn(
        self, session_id: str, prompt: str, fault: Callable[[str, Any, str, Dict[str, Any]], bool],
        *, timeout: float = 900, **kwargs: Any,
    ) -> "TurnOutcome":
        """턴을 돌리다 `fault` 가 참을 돌려주는 도구 호출에서 끊습니다. 끊겼으면 `outcome.crashed` 가 참입니다."""
        first_request = len(self.requests)
        self.fault = fault
        began = time.monotonic()
        crashed = False
        error: Optional[BaseException] = None
        task = asyncio.ensure_future(self.engine.run_turn(session_id=session_id, user_prompt=prompt, **kwargs))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            crashed = True
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            self.fault = None
        outcome = TurnOutcome(
            session_id=session_id, state=None, error=error, events=[], stream_chunks=0,
            requests=self.requests[first_request:], assemblies=[], decisions=[],
            seconds=time.monotonic() - began, lag=LoopLag(),
        )
        outcome.crashed = crashed
        return outcome

    async def restart_server(self) -> Dict[str, int]:
        """서버를 껐다 켠 것과 같은 상태를 만듭니다: MCP 서버 프로세스(커널 포함)를 모두 내리고, 돌던 턴을 "끊김" 으로 적습니다."""
        from app.mcp.pool import get_runtime_pool
        from app.orchestration.turns import mark_interrupted_turns

        await get_runtime_pool().shutdown_all()
        async with self.session_factory()() as db:
            return await mark_interrupted_turns(db)

    async def resume(self, session_id: str, turn_id: str, mode: str, *, timeout: float = 1500) -> "TurnOutcome":
        events: List[Dict[str, Any]] = []

        async def on_event(event: Dict[str, Any]) -> None:
            if event.get("type") != "message_stream_chunk":
                events.append({k: v for k, v in event.items() if k not in ("messages",)})

        first_request = len(self.requests)
        began = time.monotonic()
        error: Optional[BaseException] = None
        state = None
        try:
            state = await asyncio.wait_for(
                self.engine.resume_turn(session_id, turn_id, mode, on_event=on_event), timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            error = exc
        return TurnOutcome(
            session_id=session_id, state=state, error=error, events=events, stream_chunks=0,
            requests=self.requests[first_request:], assemblies=[], decisions=[],
            seconds=time.monotonic() - began, lag=LoopLag(),
        )

    # ------------------------------------------------------------------ DB 읽기

    def session_factory(self):
        from app.database.session import get_session_factory

        return get_session_factory()

    async def rows(self, model: Any, *where: Any, order_by: Any = None) -> List[Any]:
        from sqlalchemy import select

        async with self.session_factory()() as db:
            stmt = select(model)
            for clause in where:
                stmt = stmt.where(clause)
            if order_by is not None:
                stmt = stmt.order_by(*order_by) if isinstance(order_by, (list, tuple)) else stmt.order_by(order_by)
            return list((await db.execute(stmt)).scalars().all())

    async def messages(self, session_id: str) -> List[Any]:
        from app.database.models import MessageModel

        return await self.rows(
            MessageModel, MessageModel.session_id == session_id, order_by=MessageModel.created_at,
        )

    async def turns(self, session_id: str) -> List[Any]:
        from app.database.models import TurnModel

        return await self.rows(TurnModel, TurnModel.session_id == session_id, order_by=TurnModel.started_at)

    async def tool_calls(self, session_id: str) -> List[Any]:
        from app.database.models import ToolCallRecordModel

        return await self.rows(
            ToolCallRecordModel, ToolCallRecordModel.session_id == session_id,
            order_by=ToolCallRecordModel.created_at,
        )

    async def revisions(self, session_id: str) -> List[Any]:
        from app.database.models import LedgerRevisionModel

        return await self.rows(
            LedgerRevisionModel, LedgerRevisionModel.session_id == session_id, order_by=LedgerRevisionModel.seq,
        )

    async def session(self, session_id: str) -> Any:
        from app.database.models import SessionModel

        async with self.session_factory()() as db:
            return await db.get(SessionModel, session_id)

    # ------------------------------------------------------------------ 판정 기록

    def check(self, adr: str, name: str, ok: Any, detail: Any = "") -> bool:
        """확인 하나를 기록합니다. 실패해도 바로 멈추지 않고, 시험 끝에 `settle()` 이 모아 실패시킵니다."""
        self.checks.append(Check(adr=adr, name=name, ok=bool(ok), detail=str(detail)[:1500], test=self.current_test))
        return bool(ok)

    def note(self, adr: str, text: Any) -> None:
        """통과·실패가 아닌 관찰 (모델의 행동, 수치)."""
        self.notes.append({"adr": adr, "text": str(text)[:3000], "test": self.current_test})

    def provider_outage(self) -> str:
        """제공자가 요청 자체를 받지 않은 흔적 (잔액 부족·인증 실패). 있으면 그 사유, 없으면 빈 문자열.

        이런 실패는 앱이 "연결 끊김" 발언으로 정직하게 기록하고 턴을 끝까지 돌리므로(ADR-007), 시험은 겉으로 멀쩡히
        끝납니다. 그 결과로 통과·실패를 판정하면 틀립니다. 그래서 `settle()` 이 먼저 이것을 봅니다.
        """
        marks = ("Insufficient Balance", "insufficient_balance", "AuthenticationError", "invalid api key",
                 "Incorrect API key")
        for request in self.requests:
            if request.error and any(mark.lower() in request.error.lower() for mark in marks):
                return f"요청 {request.index} ({request.agent}): {request.error[:200]}"
        return ""

    def settle(self) -> None:
        outage = self.provider_outage()
        if outage:
            self._failed_before = len(self.checks)
            raise AssertionError(
                "LLM 제공자가 요청을 받지 않았습니다 (잔액 부족 또는 인증 실패). 이 실행의 확인 결과는 믿을 수 없습니다 — "
                f"연결을 고친 뒤 다시 돌리십시오.\n  {outage}"
            )
        failed = [c for c in self.checks[self._failed_before:] if not c.ok]
        self._failed_before = len(self.checks)
        if failed:
            lines = "\n".join(f"  [{c.adr}] {c.name}: {c.detail}" for c in failed)
            raise AssertionError(f"{len(failed)}개 확인이 실패했습니다:\n{lines}")

    def dump(self, name: str, requests: Iterable[LLMRequest], extra: Optional[Dict[str, Any]] = None) -> Path:
        """요청 기록을 파일로 남깁니다 (사람이 프롬프트를 직접 읽어 볼 수 있게)."""
        path = self.records_dir / f"{name}.json"
        payload = {
            "requests": [
                {
                    "index": r.index, "agent": r.agent, "model": r.model, "tools": r.tool_names,
                    "db_checked_out": r.db_checked_out, "seconds": round(r.seconds, 2), "error": r.error,
                    "usage": r.usage, "messages": r.messages,
                    "response_text": r.response_text, "response_tool_calls": r.response_tool_calls,
                }
                for r in requests
            ],
            **(extra or {}),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def write_report(self) -> Path:
        path = self.run_dir / "report.json"
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "code": self.code_identity,
            "llm_requests": len(self.requests),
            "decision_calls": len(self.decisions),
            "checks": [c.__dict__ for c in self.checks],
            "notes": self.notes,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ 끝내기

    def shutdown(self) -> None:
        async def close() -> None:
            from app.database.session import get_engine
            from app.mcp.pool import get_runtime_pool
            from app.orchestration.runner import get_debate_runner

            try:
                await get_debate_runner().shutdown()
            except BaseException:  # noqa: BLE001
                pass
            try:
                await get_runtime_pool().shutdown_all()
            except BaseException:  # noqa: BLE001
                pass
            try:
                await get_engine().dispose()
            except BaseException:  # noqa: BLE001
                pass

        try:
            self.run(close(), timeout=120)
        finally:
            self.write_report()


@dataclass
class TurnOutcome:
    session_id: str
    state: Any
    error: Optional[BaseException]
    events: List[Dict[str, Any]]
    stream_chunks: int
    requests: List[LLMRequest]
    assemblies: List[Assembly]
    decisions: List[DecisionCall]
    seconds: float
    lag: "LoopLag"
    crashed: bool = False

    def events_of(self, kind: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("type") == kind]

    def requests_by(self, agent: str) -> List[LLMRequest]:
        return [r for r in self.requests if r.agent == agent]


class LoopLag:
    """이벤트 루프가 얼마나 붙잡혔는지 재는 표본기 (ADR-031·033 의 주장: 루프는 계속 차례를 받는다)."""

    INTERVAL = 0.02

    def __init__(self) -> None:
        self.samples: List[float] = []

    async def watch(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            began = loop.time()
            await asyncio.sleep(self.INTERVAL)
            self.samples.append(max(0.0, loop.time() - began - self.INTERVAL))

    @property
    def worst(self) -> float:
        return max(self.samples, default=0.0)

    def over(self, seconds: float) -> int:
        return sum(1 for s in self.samples if s > seconds)


# --------------------------------------------------------------------------- 도우미


LEDGER_UPDATE_MARKER = "[결정 장부 갱신]"


def _ledger_items(text: str) -> Dict[str, List[str]]:
    """칸별 항목. 앱(`context_memory.ledger_items`)처럼 맨 앞이 `- `·`* ` 인 줄만 셉니다 (들여 쓴 줄은 앞 항목의 덧붙임)."""
    sections: Dict[str, List[str]] = {}
    name: Optional[str] = None
    for line in (text or "").splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            sections[name] = []
        elif name is not None and line.startswith(("- ", "* ")):
            sections[name].append(line[2:].strip())
    return sections


def _ledger_pairs(requests: Sequence[LLMRequest], revisions: Sequence[Any]):
    """(모델이 쓴 장부 본문, 저장된 개정) 짝. 짝이 안 맞으면 None.

    `revisions` 는 이 요청들이 만든 개정만 순서대로 넘깁니다 (직접 수정 `edit` 은 모델 요청이 없어 뺍니다).
    """
    raws = [r for r in requests if LEDGER_UPDATE_MARKER in r.last_user() and r.response_text]
    stored = [r for r in revisions if r.kind != "edit"]
    if len(raws) != len(stored):
        return None
    pairs = []
    for request, revision in zip(raws, stored):
        text = request.response_text
        start = text.find("## ")
        pairs.append((text[start:].strip() if start >= 0 else text, revision))
    return pairs


def ledger_sizes(requests: Sequence[LLMRequest], revisions: Sequence[Any]) -> List[Dict[str, Any]]:
    """개정마다 모델이 쓴 장부 길이와 저장된 길이. 저장이 더 짧으면 서버가 상한에 맞춰 줄인 것입니다."""
    return [
        {"seq": rev.seq, "kind": rev.kind, "round": rev.round_number, "written": len(body), "stored": len(rev.ledger or "")}
        for body, rev in (_ledger_pairs(requests, revisions) or [])
    ]


def ledger_clip_losses(requests: Sequence[LLMRequest], revisions: Sequence[Any]) -> List[Dict[str, Any]]:
    """모델이 쓴 장부 원문과 저장된 장부를 대조해, 글자 상한에 맞추며 사라진 항목을 돌려줍니다.

    처음 구현은 상한을 넘으면 **뒤에서부터** 잘라, 뒤쪽의 `미해결 쟁점`·`담당·다음 할 일` 이 사라지고 잘린 쟁점이 번호
    이력에 "닫힘" 으로 남았습니다. 지금은 칸 단위로 줄여(`context_memory.fit_ledger`) 기각된 대안부터 빼고 쟁점은 본문만
    줄입니다. 그래서 끝이 `…` 인 저장 항목은 그 앞부분이 같으면 같은 항목으로 봅니다.
    """
    import re

    pairs = _ledger_pairs(requests, revisions)
    if pairs is None:
        raws = [r for r in requests if LEDGER_UPDATE_MARKER in r.last_user() and r.response_text]
        stored = [r for r in revisions if r.kind != "edit"]
        return [{"mismatch": f"장부 요청 {len(raws)}건 · 개정 {len(stored)}건 — 짝을 맞출 수 없어 대조하지 않았습니다"}]

    def bare(item: str) -> str:
        return re.sub(r"^\[I-\d+\]\s*", "", item).strip()

    def kept_as(item: str, have: Sequence[str]) -> bool:
        text = bare(item)
        return any(text == h or (h.endswith("…") and text.startswith(h[:-1].rstrip())) for h in have)

    losses: List[Dict[str, Any]] = []
    for body, revision in pairs:
        written, kept = _ledger_items(body), _ledger_items(revision.ledger or "")
        gone: Dict[str, List[str]] = {}
        for name, items in written.items():
            have = [bare(item) for item in kept.get(name, [])]
            missing = [item for item in items if bare(item) != "없음" and not kept_as(item, have)]
            if missing:
                gone[name] = missing
        if gone:
            losses.append({
                "seq": revision.seq, "kind": revision.kind, "round": revision.round_number,
                "written_chars": len(body), "stored_chars": len(revision.ledger or ""),
                "lost": {name: len(items) for name, items in gone.items()},
                "lost_open_issues": gone.get("미해결 쟁점", []),
                "sections_dropped": [name for name in written if name not in kept],
            })
    return losses


def tree_digest(folder: Path) -> str:
    """폴더 안 `.py` 파일들의 내용 해시 (줄 끝 차이는 무시)."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(folder).as_posix().encode("utf-8"))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (proc.stdout or "").strip()


def default_run_dir() -> Path:
    base = os.environ.get("MADO_REAL_LLM_RUN_DIR")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if base:
        return Path(base) / stamp
    import tempfile

    return Path(tempfile.gettempdir()) / "mado-real-llm" / stamp


def default_target() -> Path:
    """환경을 빌릴 MADO 폴더. 정하지 않으면 개발 저장소의 번들, 그것이 없으면 이 폴더가 놓인 설치본 자신입니다.

    뒤의 경우가 폐쇄망입니다: 소스 패키지(`package_source.py --with-acceptance`)로 설치본에 `real_llm_tests/` 를
    함께 풀었을 때, 그 설치본의 conf·.env·런타임으로 돕니다.
    """
    chosen = os.environ.get("MADO_REAL_LLM_TARGET")
    if chosen:
        return Path(chosen).resolve()
    if DEFAULT_TARGET.is_dir():
        return DEFAULT_TARGET.resolve()
    return REPO.resolve()


def default_code(target: Path) -> Path:
    """미러로 복사할 코드(`app/` 등)의 출처. 비우면 대상의 코드입니다.

    저장소에서 고친 코드를 번들의 환경(파이썬·node·conf·.env)으로 돌려 볼 때 저장소 루트를 줍니다.
    """
    value = os.environ.get("MADO_REAL_LLM_CODE")
    return Path(value).resolve() if value else target
