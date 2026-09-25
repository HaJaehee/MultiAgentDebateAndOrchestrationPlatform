import asyncio
import inspect
import json
import logging
import os
import signal
import sys
import threading
from collections import deque
from functools import lru_cache

import anyio
from pathlib import Path
from typing import Any, Dict, List, Optional
import mcp.types as types
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

from app.config import strip_extended_path_prefix
from app.mcp.policy import server_environment

logger = logging.getLogger(__name__)


def make_list_roots_handler(workspace: Path):
    """이 연결이 보는 작업 공간을 Roots 로 알려주는 콜백을 만듭니다.

    예전에는 전역 `WORKSPACE_DIR` 을 읽었습니다. 작업 공간이 서로 다른 런타임이
    동시에 살아 있으면 그 값 하나로는 답할 수 없습니다 — 어느 서버가 물어봤든
    마지막에 설정된 폴더를 알려주게 됩니다. 그래서 연결을 만들 때 폴더를 묶어 둡니다.
    """
    # 윈도우의 `\\?\` 접두사를 걷어내고 씁니다. 붙어 있으면 `as_uri()` 가
    # `file://?/C:/...` 를 만들고 서버 쪽 URL 검증이 거절합니다 — 그러면 이 서버는
    # 허용 디렉터리를 못 받은 채 뜹니다.
    root = strip_extended_path_prefix(Path(workspace).expanduser().resolve())

    async def _handle_list_roots(context: Any = None) -> types.ListRootsResult:
        return types.ListRootsResult(
            roots=[types.Root(uri=types.AnyUrl(root.as_uri()), name="workspace")]
        )

    return _handle_list_roots


class MCPToolError(Exception):
    """서버가 `isError: true` 로 보고한 도구 실행 실패.

    MCP 는 프로토콜 오류(JSON-RPC error)와 도구 실행 오류를 구분합니다.
    전자는 클라이언트에서 소비되고 끝나지만, 후자는 성공 응답과 똑같이 LLM
    컨텍스트에 주입되어야 모델이 원인을 보고 스스로 고칠 수 있습니다.
    그래서 서버 메시지를 그대로 담아 전달합니다.
    """

    def __init__(self, server_name: str, tool_name: str, message: str):
        self.server_name = server_name
        self.tool_name = tool_name
        self.message = message
        super().__init__(message)


def _describe_exception(exc: Optional[BaseException]) -> str:
    """ExceptionGroup 을 펼쳐 실제 원인을 문자열로 만듭니다."""
    if exc is None:
        return ""
    nested = getattr(exc, "exceptions", None)
    if nested:
        return " / ".join(_describe_exception(sub) for sub in nested)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

# 서버 기동 후 initialize 응답까지 기다리는 한도(초). IPython 커널을 띄우는
# 샌드박스 서버처럼 초기화가 무거운 경우를 감안한 값입니다.
STARTUP_TIMEOUT = 60.0

# 종료 신호를 보낸 뒤 프로세스가 정리되기를 기다리는 한도(초).
SHUTDOWN_TIMEOUT = 10.0


def _positive_float_env(name: str, default: float) -> float:
    """환경 변수를 양수 실수로 읽습니다. 비었거나 이상하면 기본값."""
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# 도구 한 번에 허용하는 시간(초). 이게 없으면 응답하지 않는 서버(무한 루프에 빠진
# 스크립트, 끊긴 원격, 입력을 기다리는 CLI) 하나가 토론 전체를 영원히 붙잡습니다.
# 태스크가 멈춰 있으면 취소도 종료도 먹지 않아서, 겉보기에는 서버가 죽은 것과
# 구분되지 않습니다.
TOOL_CALL_TIMEOUT = _positive_float_env("MCP_TOOL_TIMEOUT", 180.0)

# 도구 결과 한 건의 상한(문자). 서버가 수십 MB를 돌려주는 일이 실제로 있습니다
# (큰 파일 읽기, 재귀 디렉터리 목록). 그대로 두면 컨텍스트 계산·DB 기록·화면
# 렌더링이 차례로 무너지고, 그 무게가 프로세스 전체를 끌고 갑니다.
MAX_TOOL_OUTPUT_CHARS = int(_positive_float_env("MCP_TOOL_MAX_CHARS", 200_000))


def clip_tool_output(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """지나치게 긴 도구 결과를 앞뒤만 남기고 줄입니다.

    가운데를 버리는 이유: 원인은 대개 첫머리(무엇을 읽었는가)와 끝(오류 메시지,
    종료 코드)에 있습니다.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-(limit // 3):]
    dropped = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n"
        f"... [출력이 너무 길어 중간 {dropped:,}자를 생략했습니다 "
        f"(전체 {len(text):,}자)] ...\n\n"
        + tail
    )


class _StderrTee:
    """서버 stderr 를 콘솔로 흘려보내면서 마지막 몇 줄을 보관합니다.

    기동에 실패하면 anyio 는 "unhandled errors in a TaskGroup" 같은 예외를
    올릴 뿐 원인을 담고 있지 않습니다. 진짜 원인(`No module named ...`,
    `command not found` 등)은 서버가 stderr 로 내보내므로 그 꼬리를 남겨
    UI 툴팁과 로그에 붙입니다.

    `anyio.open_process` 는 실제 파일 디스크립터를 요구하므로 파이썬 객체를
    그대로 넘길 수 없습니다. 파이프를 만들어 읽기 쪽을 데몬 스레드가 비웁니다.
    """

    def __init__(self, head_lines: int = 4, tail_lines: int = 8):
        # 원인은 대개 첫 줄에 있고(예: "Cannot find module ..."), 그 뒤로 스택
        # 트레이스가 길게 이어집니다. 꼬리만 보관하면 정작 원인이 밀려나므로
        # 머리와 꼬리를 따로 붙잡습니다.
        self._head: List[str] = []
        self._head_limit = head_lines
        self._lines: deque = deque(maxlen=tail_lines)
        read_fd, write_fd = os.pipe()
        self._handle = os.fdopen(write_fd, "w", buffering=1, encoding="utf-8", errors="replace")
        self._thread = threading.Thread(target=self._pump, args=(read_fd,), daemon=True)
        self._thread.start()

    def _pump(self, read_fd: int) -> None:
        try:
            with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    stripped = line.rstrip()
                    if "does not support MCP Roots" in stripped:
                        continue
                    if stripped:
                        if len(self._head) < self._head_limit:
                            self._head.append(stripped)
                        else:
                            self._lines.append(stripped)
                    sys.stderr.write(line)
        except Exception:  # noqa: BLE001 - 로깅 보조 경로가 앱을 막으면 안 됩니다
            pass

    @property
    def handle(self):
        return self._handle

    @property
    def tail(self) -> str:
        head = list(self._head)
        rest = list(self._lines)
        if not rest:
            return "\n".join(head)
        return "\n".join(head + ["..."] + rest)

    def close(self, drain: bool = False) -> None:
        try:
            self._handle.close()
        except Exception:  # noqa: BLE001
            pass
        if drain:
            # 쓰기 쪽을 닫으면 pump 가 EOF 로 끝납니다. 자식이 아직 살아 있으면
            # 자기 복제본을 들고 있으므로 타임아웃으로 빠져나옵니다.
            self._thread.join(timeout=1.0)


class MCPToolDefinition(BaseModel):
    server_name: str
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = {}
    # MCP 도구 annotations (`readOnlyHint` 등). 도구 보안 판정이 **신뢰하는 서버의 것만**
    # 씁니다 — 명세도 신뢰할 수 없는 서버의 힌트를 믿지 말라고 합니다 (`app/mcp/policy.py`).
    annotations: Optional[Dict[str, Any]] = None

    @property
    def qualified_name(self) -> str:
        """Returns unique tool identifier combining server and tool name."""
        return f"{self.server_name}__{self.name}"

    def to_openai_tool(self) -> Dict[str, Any]:
        """Converts MCP Tool definition to LiteLLM/OpenAI tool format."""
        schema = self.input_schema if self.input_schema else {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": f"[{self.server_name}] {self.description or self.name}",
                "parameters": schema,
            },
        }


# ---------------------------------------------------------------------------
# 요청 스코프 (_meta)
# ---------------------------------------------------------------------------
# 어떤 대화의 호출인지는 **호스트인 우리가** 압니다. 모델에게 인자로 물어보면,
# 모델이 잊거나 잘못 적는 순간 다른 대화의 상태를 건드립니다. 그래서 스코프는
# 매 호출의 `_meta` 에 우리가 실어 보냅니다.
#
# 키를 여러 개 넣는 이유: 서버마다 찾는 이름이 다릅니다 (샌드박스는
# conversationId/sessionId 계열을 훑고, memory 포크는 graphId 도 봅니다).
def build_scope_meta(scope: Optional[str]) -> Optional[Dict[str, Any]]:
    """대화 식별자를 MCP 요청 메타데이터로 만듭니다. 스코프가 없으면 None."""
    scope = (scope or "").strip()
    if not scope:
        return None
    return {"conversationId": scope, "sessionId": scope, "graphId": scope}


@lru_cache(maxsize=1)
def _session_supports_meta() -> bool:
    """설치된 mcp SDK 의 `call_tool` 이 `meta=` 를 받는지 확인합니다.

    requirements 는 mcp>=1.29 를 요구하지만, 이미 설치된 환경이나 폐쇄망 반입본이
    더 낮은 버전일 수 있습니다. 그때는 스코프 없이 호출합니다 — 격리가 빠질 뿐
    도구 자체는 계속 동작해야 합니다.
    """
    try:
        supported = "meta" in inspect.signature(ClientSession.call_tool).parameters
    except (TypeError, ValueError):  # pragma: no cover - 서명을 못 읽는 경우
        return False
    if not supported:
        logger.warning(
            "Installed mcp SDK does not accept `meta=` in call_tool; MCP calls will be sent "
            "without a conversation scope (memory graphs and sandbox namespaces will be shared)."
        )
    return supported


class MCPClientConnection:
    """단일 MCP 서버 프로세스의 수명주기와 도구 호출을 관리합니다.

    세션은 최초 연결 시 한 번 열고 계속 유지합니다. 도구 호출마다 프로세스를
    새로 띄우면 서버가 들고 있는 상태(예: 코드 실행 샌드박스의 네임스페이스별
    IPython 커널과 그 안의 변수/데이터프레임)가 매번 사라지고, 호출마다 기동
    비용을 다시 치르게 됩니다.

    stdio 세션은 **자신을 연 태스크(`_serve`)가 끝까지 소유**합니다. anyio 의
    cancel scope 는 태스크에 묶여 있어, 컨텍스트를 연 태스크가 아닌 곳에서
    빠져나오면 런타임 에러가 나기 때문입니다. 다른 태스크는 `_session` 으로
    요청만 보냅니다 (ClientSession 이 요청/응답 다중화를 처리합니다).
    """

    def __init__(
        self,
        server_name: str,
        command: str = "",
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        *,
        url: str = "",
        headers: Optional[Dict[str, str]] = None,
        transport: str = "auto",
        timeout: float = 30.0,
        workspace: Optional[Path] = None,
    ):
        self.server_name = server_name
        # 이 연결이 보는 작업 공간. Roots 응답이 여기서 나옵니다. 주지 않으면
        # 서버 env 에 적힌 값을, 그것도 없으면 전역을 씁니다 (옛 호출자 호환).
        self.workspace = Path(
            workspace
            or (env or {}).get("WORKSPACE_DIR")
            or os.environ.get("WORKSPACE_DIR", "./workspace")
        ).expanduser().resolve()
        self._list_roots = make_list_roots_handler(self.workspace)
        self.command = command
        self.args = args or []
        self.env = env or {}
        # 원격 서버. 프로세스를 띄우는 대신 이미 떠 있는 주소에 붙습니다.
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self._configured_transport = transport or "auto"
        self._tools: List[MCPToolDefinition] = []
        self._is_available = False

        # 살아있는 세션과 그것을 소유한 태스크
        self._session: Optional[ClientSession] = None
        self._owner_task: Optional[asyncio.Task] = None
        self._ready: Optional[asyncio.Event] = None
        self._shutdown: Optional[asyncio.Event] = None
        self._connect_error: Optional[BaseException] = None
        self._stderr_tail: str = ""
        # 자식 프로세스 자체를 들고 있습니다. pid 만 남겨 두고 나중에 os.kill 을
        # 부르면, 그 사이 자식이 이미 끝나 있을 때 운영체제가 재사용한 **다른**
        # 프로세스를 죽입니다 (윈도우는 pid 를 빠르게 돌려씁니다). 실제로 그
        # 자리에 있을 수 있는 것이 uvicorn 리로더의 워커라, 도구 하나 정리하다
        # 서버가 통째로 사라지는 경로가 됩니다.
        self._process: Any = None
        self._process_pid: Optional[int] = None
        # connect / teardown 직렬화 (도구 호출 자체는 동시 실행 가능)
        self._lock = asyncio.Lock()

    @property
    def is_remote(self) -> bool:
        return bool((self.url or "").strip())

    @property
    def transport(self) -> str:
        """실제로 쓸 전송 방식. `auto` 는 주소 유무로 갈립니다."""
        if self._configured_transport != "auto":
            return self._configured_transport
        return "http" if self.is_remote else "stdio"

    @property
    def connect_attempts(self) -> List[str]:
        """붙어 볼 전송 방식의 순서.

        원격을 `auto` 로 두면 두 번 시도합니다. 두 규격(Streamable HTTP 와 옛
        HTTP+SSE)이 같은 주소를 쓰는 일이 흔해서, 붙여 보기 전에는 어느 쪽인지
        알 수 없기 때문입니다. 설정에 못 박아 두면 한 번만 갑니다.
        """
        if self.is_remote and self._configured_transport == "auto":
            return ["http", "sse"]
        return [self.transport]

    @property
    def endpoint_label(self) -> str:
        """이 서버가 어디에 있는지 한 줄로 (연결 실패 로그와 화면 표시용)."""
        if self.is_remote:
            return self.url
        return " ".join([self.command, *self.args]).strip()

    @property
    def tools(self) -> List[MCPToolDefinition]:
        return self._tools

    @property
    def is_available(self) -> bool:
        return self._is_available

    @property
    def is_connected(self) -> bool:
        """실제 stdio 세션이 살아있는지 여부 (mock 폴백과 구분됩니다)."""
        return self._session is not None

    @property
    def connect_error(self) -> Optional[str]:
        """마지막 기동 실패 사유 (UI 표시용). 성공했으면 None.

        서버가 stderr 로 남긴 마지막 줄들을 우선합니다. anyio 예외 문구보다
        원인을 훨씬 정확히 담고 있기 때문입니다.
        """
        if self._connect_error is None and not self._stderr_tail:
            return None
        if self._stderr_tail:
            return self._stderr_tail
        return _describe_exception(self._connect_error)

    def _get_server_params(self) -> StdioServerParameters:
        # 부모의 비밀(원격 접속 토큰, LLM API 키)은 물려주지 않습니다. 서버가 필요한
        # 비밀은 conf.json 의 그 서버 `env` 에 명시합니다 (`server_environment`).
        return StdioServerParameters(
            command=self.command,
            args=self.args,
            env=server_environment(os.environ, self.env),
        )

    async def _serve(self) -> None:
        """세션을 열고 종료 신호가 올 때까지 살려둡니다.

        컨텍스트 진입과 이탈이 모두 이 태스크 안에서 일어나야 합니다.

        원격 서버는 `auto` 일 때 Streamable HTTP 로 먼저 붙고, 실패하면 옛
        HTTP+SSE 로 한 번 더 시도합니다. 두 규격이 같은 주소를 쓰는 일이 흔해서,
        붙여 보기 전에는 어느 쪽인지 알 수 없습니다. 설정에 `transport` 를 적어
        두면 이 왕복을 건너뜁니다.
        """
        attempts = self.connect_attempts

        for index, transport in enumerate(attempts):
            try:
                await self._serve_once(transport)
                return
            except asyncio.CancelledError:
                # 종료 중입니다. 다른 전송 방식으로 다시 붙어 볼 일이 아닙니다.
                if self._ready is not None:
                    self._ready.set()
                raise
            except BaseException as exc:  # noqa: BLE001 - 기동 실패는 폴백으로 흡수
                self._connect_error = exc
                # 이미 한 번 붙었던 세션이 끊긴 것이라면 다른 규격을 시도할 일이
                # 아닙니다. 그건 서버가 죽은 것이지 규격이 틀린 것이 아닙니다
                # (`_ready` 는 세션이 열렸을 때만 서 있습니다).
                already_connected = self._ready is not None and self._ready.is_set()
                if index + 1 >= len(attempts) or already_connected:
                    if self._ready is not None:
                        # 기동에 실패한 경우에도 대기 중인 connect() 를 깨웁니다.
                        self._ready.set()
                    return
                logger.info(
                    f"MCP server '{self.server_name}' did not answer over {transport}; "
                    f"trying {attempts[index + 1]} ({_describe_exception(exc)})"
                )

    async def _serve_once(self, transport: str) -> None:
        """한 가지 전송 방식으로 세션을 열고 종료까지 유지합니다.

        어느 쪽으로 끝나든 세션 참조를 지웁니다. 이걸 각 전송 방식 안에 두면
        하나를 더할 때마다 빠뜨리게 되고, 남은 참조는 '살아 있는 세션' 으로
        읽혀 다음 도구 호출이 닫힌 스트림에 나갑니다.
        """
        try:
            if transport == "stdio":
                await self._serve_stdio()
            else:
                await self._serve_http(transport)
        finally:
            self._session = None

    async def _serve_http(self, transport: str) -> None:
        """원격 서버. 띄울 프로세스도, 볼 stderr 도 없습니다."""
        headers = dict(self.headers) if self.headers else None
        if transport == "sse":
            cm = sse_client(self.url, headers=headers, timeout=self.timeout)
        else:
            cm = streamablehttp_client(self.url, headers=headers, timeout=self.timeout)

        async with cm as streams:
            # streamablehttp_client 는 (read, write, get_session_id) 를 돌려주고
            # sse_client 는 (read, write) 를 돌려줍니다.
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(
                read_stream,
                write_stream,
                list_roots_callback=self._list_roots,
            ) as session:
                await session.initialize()
                self._session = session
                self._connect_error = None
                self._stderr_tail = ""
                self._ready.set()
                await self._shutdown.wait()

    async def _serve_stdio(self) -> None:
        tee = _StderrTee()
        connected = False
        try:
            cm = stdio_client(self._get_server_params(), errlog=tee.handle)
            async with cm as (
                read_stream,
                write_stream,
            ):
                gen = getattr(cm, "gen", None)
                frame = gen.ag_frame if gen else None
                proc = frame.f_locals.get("process") if frame else None
                self._process = proc
                self._process_pid = getattr(proc, "pid", None)
                async with ClientSession(
                    read_stream,
                    write_stream,
                    list_roots_callback=self._list_roots,
                ) as session:
                    await session.initialize()
                    self._session = session
                    self._connect_error = None
                    self._stderr_tail = ""
                    connected = True
                    self._ready.set()
                    # 종료 요청이 올 때까지 프로세스를 유지합니다.
                    await self._shutdown.wait()
        finally:
            # 기동에 실패했을 때만 stderr 를 회수합니다 (정상 종료 경로는 대기 없이 닫음).
            tee.close(drain=not connected)
            if not connected:
                self._stderr_tail = tee.tail

    async def connect(self) -> bool:
        """세션이 없으면 새로 열고, 사용 가능해졌는지 반환합니다."""
        async with self._lock:
            if self._session is not None:
                return True

            await self._teardown_locked()

            self._ready = asyncio.Event()
            self._shutdown = asyncio.Event()
            self._connect_error = None
            self._owner_task = asyncio.create_task(self._serve(), name=f"mcp-{self.server_name}")

            try:
                await asyncio.wait_for(self._ready.wait(), timeout=STARTUP_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    f"MCP server '{self.server_name}' did not initialize within {STARTUP_TIMEOUT}s"
                )
                await self._teardown_locked()
                return False

            if self._session is None:
                logger.warning(
                    f"Could not connect to MCP server '{self.server_name}' "
                    f"({self.transport}: {self.endpoint_label}): "
                    f"{self.connect_error or _describe_exception(self._connect_error)}"
                )
                await self._teardown_locked()
                return False

            logger.info(f"MCP session established for '{self.server_name}'")
            return True

    async def close(self) -> None:
        """세션을 닫고 서버 프로세스를 정리합니다."""
        async with self._lock:
            await self._teardown_locked()

    async def _teardown_locked(self) -> None:
        """호출자가 `self._lock` 을 들고 있어야 합니다."""
        task, self._owner_task = self._owner_task, None
        proc, self._process = self._process, None
        proc_pid, self._process_pid = self._process_pid, None

        if self._shutdown is not None:
            self._shutdown.set()

        # 자식 프로세스를 먼저 종료하여 I/O 파이프와 task 가 즉시 정리되도록 합니다.
        self._terminate_process(proc, proc_pid)

        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=SHUTDOWN_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"MCP server '{self.server_name}' did not shut down cleanly; cancelled")
                task.cancel()
            except (Exception, BaseException):  # noqa: BLE001 - 종료 경로의 오류는 무시
                pass

        self._session = None
        self._ready = None
        self._shutdown = None

    @staticmethod
    def _terminate_process(proc: Any, proc_pid: Optional[int]) -> None:
        """서버 프로세스를 끝냅니다. 확실히 우리 자식일 때만 신호를 보냅니다.

        프로세스 객체가 있으면 그것만 씁니다 (`returncode` 로 이미 끝났는지 볼 수
        있으므로 pid 재사용에 걸리지 않습니다). 객체를 못 잡았을 때만 pid 로
        갑니다 — 그 경로도 실패는 전부 삼킵니다. 정리하다 올라온 예외가 종료
        경로를 타고 나가면, 도구 하나가 실패했을 뿐인데 앱이 함께 내려갑니다.
        """
        if proc is not None:
            if getattr(proc, "returncode", None) is not None:
                return  # 이미 끝났습니다. 신호를 보낼 대상이 없습니다.
            for method in ("kill", "terminate"):
                fn = getattr(proc, method, None)
                if fn is None:
                    continue
                try:
                    fn()
                    return
                except (ProcessLookupError, OSError, ValueError, RuntimeError):
                    continue
                except BaseException:  # noqa: BLE001 - 정리 경로는 절대 터지지 않습니다
                    logger.debug("Ignoring an error while terminating an MCP server", exc_info=True)
                    return
            return

        if proc_pid is None:
            return
        try:
            os.kill(proc_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except BaseException:  # noqa: BLE001
            pass

    def _is_session_dead(self, exc: BaseException) -> bool:
        """예외가 '세션이 끊겼다'는 신호인지 판정합니다.

        서버 프로세스가 죽어도 소유 태스크는 곧바로 끝나지 않으므로
        (`_shutdown` 대기 중), 태스크 상태만으로는 판정할 수 없습니다.
        스트림이 닫혔다는 anyio 예외가 가장 이른 신호입니다.
        """
        if self._session is None:
            return True
        task = self._owner_task
        if task is None or task.done():
            return True
        return isinstance(
            exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
        )

    async def discover_tools(self) -> List[MCPToolDefinition]:
        """세션을 열고 도구 목록을 받아 캐시합니다."""
        if not await self.connect():
            self._is_available = False
            # 외부 서버를 띄울 수 없으면 시뮬레이션/테스트용 mock 도구로 폴백합니다.
            self._tools = self._get_fallback_mock_tools()
            return self._tools

        session = self._session
        try:
            result = await asyncio.wait_for(session.list_tools(), timeout=STARTUP_TIMEOUT)
            discovered: List[MCPToolDefinition] = []
            for t in result.tools:
                input_schema = getattr(t, "inputSchema", getattr(t, "input_schema", {}))
                if hasattr(input_schema, "model_dump"):
                    input_schema = input_schema.model_dump()
                elif not isinstance(input_schema, dict):
                    input_schema = dict(input_schema) if input_schema else {}

                annotations = getattr(t, "annotations", None)
                if hasattr(annotations, "model_dump"):
                    annotations = annotations.model_dump(exclude_none=True)
                discovered.append(
                    MCPToolDefinition(
                        server_name=self.server_name,
                        name=t.name,
                        description=t.description or "",
                        input_schema=input_schema,
                        annotations=dict(annotations) if isinstance(annotations, dict) and annotations else None,
                    )
                )
            self._tools = discovered
            self._is_available = True
            logger.info(f"Discovered {len(discovered)} tools from MCP server '{self.server_name}'")
            return self._tools
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - 한 서버의 실패가 나머지를 막지 않습니다
            logger.warning(
                f"Tool discovery failed for MCP server '{self.server_name}': "
                f"{_describe_exception(e)}"
            )
            await self.close()
            self._is_available = False
            self._tools = self._get_fallback_mock_tools()
            return self._tools

    def _get_fallback_mock_tools(self) -> List[MCPToolDefinition]:
        """Provide mock tool definitions if external server is offline or fails."""
        if "filesystem" in self.server_name:
            return [
                MCPToolDefinition(
                    server_name=self.server_name,
                    name="read_file",
                    description="Read file contents from workspace",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "Relative file path"}},
                        "required": ["path"],
                    },
                ),
                MCPToolDefinition(
                    server_name=self.server_name,
                    name="write_file",
                    description="Write content to a file in workspace",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                            "content": {"type": "string", "description": "File content"},
                        },
                        "required": ["path", "content"],
                    },
                ),
                MCPToolDefinition(
                    server_name=self.server_name,
                    name="list_directory",
                    description="List files in a directory",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "Directory path", "default": "."}},
                    },
                ),
            ]
        elif "search" in self.server_name:
            return [
                MCPToolDefinition(
                    server_name=self.server_name,
                    name="search",
                    description="Search the web for technical documentation and libraries",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "Search query"}},
                        "required": ["query"],
                    },
                )
            ]
        return []

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any], scope: Optional[str] = None
    ) -> str:
        """유지 중인 세션으로 도구를 실행합니다. 세션이 끊겼으면 1회 재연결합니다.

        `scope` 는 이 호출이 속한 대화의 식별자입니다. 요청 `_meta` 로 실려가서,
        서버가 대화별로 상태를 나눠 담는 근거가 됩니다 (memory 의 그래프,
        샌드박스의 네임스페이스).
        """
        if not self._is_available:
            # Execute fallback simulation if server unavailable
            return self._simulate_tool_execution(tool_name, arguments)

        for attempt in (1, 2):
            session = self._session
            if session is None:
                if not await self.connect():
                    return self._simulate_tool_execution(tool_name, arguments)
                session = self._session
                if session is None:
                    return self._simulate_tool_execution(tool_name, arguments)

            try:
                meta = build_scope_meta(scope)
                if meta and _session_supports_meta():
                    call = session.call_tool(tool_name, arguments=arguments, meta=meta)
                else:
                    call = session.call_tool(tool_name, arguments=arguments)
                # 한도를 두지 않으면 응답하지 않는 서버 하나가 이 발언을,
                # 나아가 토론 전체를 영원히 붙잡습니다.
                res = await asyncio.wait_for(call, timeout=TOOL_CALL_TIMEOUT)
                text = clip_tool_output(self._extract_result_text(res))
                if getattr(res, "isError", False):
                    # 도구는 실행됐지만 실패했다. 서버 메시지를 그대로 올려
                    # 모델이 보고 스스로 고칠 수 있게 한다.
                    raise MCPToolError(self.server_name, tool_name, text)
                return text
            except MCPToolError:
                raise
            except asyncio.CancelledError:
                # 취소는 실패가 아니라 "이 토론은 그만" 이라는 지시입니다.
                # 여기서 삼키면 사용자의 취소도 서버 종료도 먹지 않고,
                # lifespan 이 끝나지 않는 태스크를 기다리다 멈춥니다.
                raise
            except asyncio.TimeoutError as e:
                # 세션은 살아 있을지 몰라도 이 호출은 버려졌습니다. 서버가 뒤늦게
                # 응답하면 다음 호출의 답과 뒤섞이므로 세션을 접고 다시 엽니다.
                logger.error(
                    f"Tool '{tool_name}' on '{self.server_name}' did not answer within "
                    f"{TOOL_CALL_TIMEOUT:.0f}s; dropping the call and reconnecting"
                )
                await self.close()
                raise MCPToolError(
                    self.server_name,
                    tool_name,
                    f"도구 '{tool_name}' 이(가) {TOOL_CALL_TIMEOUT:.0f}초 안에 응답하지 "
                    f"않아 호출을 중단했습니다. 더 작은 범위로 나누어 다시 시도하거나 "
                    f"다른 방법을 쓰세요.",
                ) from e
            except BaseException as e:  # noqa: BLE001
                # 세션이 죽은 경우에만 재시도합니다. 도구 자체가 실패한 것이라면
                # 재실행이 부작용(파일 쓰기 등)을 두 번 일으킬 수 있습니다.
                if attempt == 1 and self._is_session_dead(e):
                    logger.warning(
                        f"MCP session for '{self.server_name}' dropped during "
                        f"'{tool_name}'; reconnecting once ({type(e).__name__})"
                    )
                    await self.close()
                    continue
                # 호출자(MCPManager)가 'error' 상태로 보고할 수 있도록 전파합니다.
                logger.error(
                    f"Error executing tool '{tool_name}' on '{self.server_name}': "
                    f"{type(e).__name__}: {e}"
                )
                raise

        return self._simulate_tool_execution(tool_name, arguments)

    @staticmethod
    def _extract_result_text(res: Any) -> str:
        """CallToolResult 에서 텍스트 콘텐츠를 뽑아냅니다."""
        if hasattr(res, "content"):
            texts = []
            for item in res.content:
                if hasattr(item, "text"):
                    texts.append(item.text)
                else:
                    texts.append(str(item))
            return "\n".join(texts)
        return str(res)


    def _simulate_tool_execution(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Simulates tool execution locally for offline/test environments."""
        if tool_name == "read_file":
            path = arguments.get("path", "")
            return f"[Simulated File Read: '{path}'] (File exists. Length: 120 lines. Status: Ready)"
        elif tool_name == "write_file":
            path = arguments.get("path", "")
            content_len = len(arguments.get("content", ""))
            return f"[Simulated File Write: '{path}'] (Successfully wrote {content_len} bytes)"
        elif tool_name == "list_directory":
            path = arguments.get("path", ".")
            return f"[Simulated Dir List: '{path}'] ['app/', 'tests/', 'conf.json', 'README.md', 'requirements.txt']"
        elif tool_name == "search":
            query = arguments.get("query", "")
            return f"[Simulated Web Search: '{query}'] Found 3 references: FastAPI docs, NiceGUI documentation, Python stdlib guidelines."
        return f"[Simulated Tool Execution: {self.server_name}.{tool_name}({json.dumps(arguments)})] Success."
