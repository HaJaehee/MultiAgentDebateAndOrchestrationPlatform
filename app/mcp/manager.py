import asyncio
import json
import logging
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from app.config import MCPServerConfig, PROJECT_ROOT, get_config, resolve_workspace_dir
from app.mcp.client import (
    MCPClientConnection,
    MCPToolDefinition,
    MCPToolError,
    clip_tool_output,
)
from app.mcp.guards import binary_write_refusal

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def find_git_executable() -> Optional[str]:
    """Finds git executable from PATH or standard Windows installation locations.

    `os.environ["PATH"]` 과 `GIT_PYTHON_GIT_EXECUTABLE` 을 고치므로 한 번만
    돕니다. 런타임이 여러 개 뜨면 작업 공간마다 불리는데, 그때마다 전역 PATH 를
    다시 조립할 이유가 없습니다.
    """
    which_git = shutil.which("git")
    if which_git:
        return which_git

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "git.exe",
        Path(__file__).resolve().parent.parent.parent / "git_runtime" / "cmd" / "git.exe",
        Path(__file__).resolve().parent.parent.parent / "git_runtime" / "bin" / "git.exe",
    ]
    for c in candidates:
        try:
            if c.is_file():
                git_dir = str(c.parent)
                current_path = os.environ.get("PATH", "")
                if git_dir not in current_path:
                    os.environ["PATH"] = f"{git_dir};{current_path}"
                os.environ["GIT_PYTHON_GIT_EXECUTABLE"] = str(c)
                logger.info(f"Auto-detected Git executable at: {c}")
                return str(c)
        except Exception:
            continue
    return None


# 포크한 memory MCP 서버의 소스 위치. 실제로 실행되는 사본은
# `${MCP_NODE_HOME}` 안에 둡니다 — @modelcontextprotocol/sdk 와 zod 를
# `node_modules` 에서 찾으려면 그 옆에 있어야 하기 때문입니다.
VENDORED_MEMORY_SERVER = PROJECT_ROOT / "mcp_servers" / "memory_scoped" / "index.mjs"
# conf.json 이 이 이름으로 가리키는 인자를 포크 사본의 자리로 인식합니다.
VENDORED_MEMORY_FILENAME = "memory-scoped.mjs"
# 갱신되지 않은 conf.json 이 아직 가리키고 있을 수 있는 공식 서버 진입점.
OFFICIAL_MEMORY_PACKAGE = "@modelcontextprotocol/server-memory"


def sync_vendored_servers(server_configs: Dict[str, MCPServerConfig]) -> None:
    """설정이 가리키는 자리에 포크한 서버 사본을 최신 상태로 놓습니다.

    설정에 적힌 경로를 그대로 씁니다. 사용자가 `MCP_NODE_HOME` 을 어디로 잡았든
    따라가고, 상대 경로면 프로젝트 루트 기준의 절대 경로로 바꿔 설정에 되돌려
    놓습니다. 상대 경로를 그대로 넘기면 자식 프로세스가 자기 cwd 로 풀어서
    "설정은 하나인데 서버마다 다른 파일을 본다" 가 됩니다.

    setup_mcp.py 를 다시 돌리지 않은 기존 설치에서도 서버가 뜨도록 기동 때마다
    확인합니다. 파일 내용이 같으면 아무것도 하지 않습니다.
    """
    if not VENDORED_MEMORY_SERVER.is_file():
        return

    source = VENDORED_MEMORY_SERVER.read_bytes()
    for name, cfg in server_configs.items():
        if any(OFFICIAL_MEMORY_PACKAGE in arg for arg in cfg.args):
            # 소스만 갱신한 설치본에서 일어납니다. apply_update.ps1 은 그 망의
            # 엔드포인트가 들어 있는 conf.json 을 일부러 덮지 않기 때문입니다.
            # 조용히 두면 공식 서버가 그대로 떠서 대화 간 격리 없이 동작합니다.
            logger.warning(
                f"MCP server '{name}' still points at the official memory server; conversations "
                f"will share one knowledge graph. Update conf.json to "
                f"args = [\"${{MCP_NODE_HOME:-./mcp_node}}/{VENDORED_MEMORY_FILENAME}\"] with "
                f"env = {{ MEMORY_GRAPH_DIR = \"${{WORKSPACE_DIR:-./workspace}}/.memory-graphs\" }}."
            )
        for i, arg in enumerate(cfg.args):
            if not arg.endswith(VENDORED_MEMORY_FILENAME):
                continue
            target = Path(arg).expanduser()
            if not target.is_absolute():
                target = (PROJECT_ROOT / target).resolve()
            cfg.args[i] = str(target)
            try:
                if target.is_file() and target.read_bytes() == source:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source)
                logger.info(f"Installed forked memory MCP server at: {target}")
            except OSError as e:
                logger.warning(f"Could not install forked memory MCP server at '{target}': {e}")


def absolutize_local_script_args(server_configs: Dict[str, MCPServerConfig]) -> None:
    """설정에 적힌 상대 경로 인자를 프로젝트 루트 기준 절대 경로로 바꿉니다.

    자식 프로세스는 우리 cwd 를 물려받습니다 (`client.py` 의 `_get_server_params` 는
    `cwd=` 를 넘기지 않습니다). 그런데 그 cwd 가 프로젝트 루트라는 보장이 없습니다 —
    `run_mado.ps1` 은 `Set-Location` 없이 `python -m app.main` 을 돌리고, 개발 PC 에서
    다른 폴더에서 띄우는 일도 흔합니다. 그러면 `./mcp_servers/memory_scoped/index.mjs` 같은
    기본값이 엉뚱한 곳을 가리켜 서버가 조용히 안 뜹니다. 지금까지는 런처가 절대
    경로 환경변수(`MCP_NODE_HOME` 등)를 넣어 주어 가려져 있던 구멍입니다.

    `sync_vendored_servers` 와 일부러 갈라 둡니다. 그쪽은 "포크한 파일을 제자리에
    놓는" 일이고 이쪽은 "모든 경로를 고치는" 일이라, 한 함수에 섞으면 다음 사람이
    하나를 고치다 다른 하나를 깹니다.

    바꾸는 것은 **실제로 존재하는 파일을 가리키는** 상대 경로뿐입니다. 그 조건이
    `-m`, `--repository`, 플래그 값 같은 인자를 건드리지 않게 막아 줍니다. 멱등합니다.
    """
    for cfg in server_configs.values():
        if not cfg.args:
            continue
        for i, arg in enumerate(cfg.args):
            if not isinstance(arg, str) or not arg:
                continue
            if "/" not in arg and "\\" not in arg:
                continue  # 경로처럼 보이지 않음 (플래그, 모듈명 등)
            candidate = Path(arg).expanduser()
            if candidate.is_absolute():
                continue
            resolved = (PROJECT_ROOT / candidate).resolve()
            if resolved.exists():
                cfg.args[i] = str(resolved)


# 작업 공간 준비는 경로마다 한 번씩만. 같은 폴더를 쓰는 런타임 둘이 동시에
# `git init` 을 돌리면 서로의 절반 만들어진 저장소를 봅니다.
_workspace_locks: Dict[str, asyncio.Lock] = {}


async def ensure_workspace_async(path: str | Path) -> None:
    """`ensure_workspace` 를 이벤트 루프 밖에서, 경로마다 직렬화해 돌립니다.

    안쪽은 `subprocess.run` 여러 번입니다. 루프에서 그대로 돌리면 런타임 하나가
    뜨는 동안 화면 전체가 멈추고, 런타임 N 개가 동시에 뜨면 그만큼 곱해집니다.
    """
    key = str(Path(path).expanduser().resolve())
    lock = _workspace_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await asyncio.to_thread(ensure_workspace, key)


def ensure_workspace(path: str) -> None:
    """에이전트 공용 작업 공간을 만들고, 없으면 git 저장소로 초기화합니다.

    git MCP 서버는 대상이 유효한 git 저장소가 아니면 "is not a valid Git
    repository" 로 기동에 실패합니다. 작업 공간은 앱이 소유하는 디렉터리이고
    git 서버가 기본 활성이므로, 여기서 만들어 둡니다.
    """
    workspace = Path(path).expanduser().resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"Could not create workspace '{workspace}': {e}")
        return

    git_bin = find_git_executable()
    if not git_bin:
        logger.warning(
            f"git not found on PATH or standard locations; '{workspace}' stays a plain directory and "
            "the git MCP server will fail to start."
        )
        return

    os.environ["GIT_PYTHON_GIT_EXECUTABLE"] = git_bin

    # Configure safe.directory in corporate environments
    try:
        subprocess.run(
            [git_bin, "config", "--global", "--add", "safe.directory", "*"],
            capture_output=True, timeout=10, check=False,
        )
    except Exception:
        pass

    if (workspace / ".git").exists():
        try:
            subprocess.run(
                [git_bin, "-C", str(workspace), "config", "user.name", "multiagent"],
                capture_output=True, timeout=10, check=False,
            )
            subprocess.run(
                [git_bin, "-C", str(workspace), "config", "user.email", "multiagent@localhost"],
                capture_output=True, timeout=10, check=False,
            )
        except Exception:
            pass
        return

    try:
        subprocess.run(
            [git_bin, "init", "-q", str(workspace)],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            [git_bin, "-C", str(workspace), "config", "user.name", "multiagent"],
            check=False, capture_output=True, timeout=10,
        )
        subprocess.run(
            [git_bin, "-C", str(workspace), "config", "user.email", "multiagent@localhost"],
            check=False, capture_output=True, timeout=10,
        )
        keep = workspace / ".gitkeep"
        keep.touch()
        subprocess.run(
            [git_bin, "-C", str(workspace), "add", ".gitkeep"],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            [git_bin, "-C", str(workspace), "-c", "user.name=multiagent",
             "-c", "user.email=multiagent@localhost", "commit", "-q", "-m", "workspace 초기화"],
            check=True, capture_output=True, timeout=30,
        )
        logger.info(f"Initialized agent workspace as a git repository: {workspace}")
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"Could not initialize git repository at '{workspace}': {e}")



# ---------------------------------------------------------------------------
# 지식 그래프 이어받기
# ---------------------------------------------------------------------------
# memory 서버는 대화마다 파일 하나(`<MEMORY_GRAPH_DIR>/<대화 id>.jsonl`)를 씁니다.
# 그 경계는 호스트가 `_meta.conversationId` 로 정하고, 서버는 메타를 인자보다
# 우선합니다 — 모델이 `graph_id` 에 아무 값이나 적어 남의 그래프를 여는 것을
# 막기 위해서입니다.
#
# 그래서 새 대화는 빈 그래프로 시작하고, "이전 대화의 그래프를 읽어라" 고
# 지시해도 닿지 않습니다. 컨텍스트가 가득 차 새 대화로 넘어가야 할 때 그동안
# 쌓은 사실이 통째로 사라진다는 뜻입니다.
#
# 경계를 정하는 것이 호스트이므로, 옮기는 것도 호스트가 합니다. 파일 하나를
# 복사하면 새 대화가 그 그래프를 자기 것으로 이어받습니다.

# memory 서버가 그래프 폴더를 받는 환경변수 이름.
MEMORY_GRAPH_DIR_ENV = "MEMORY_GRAPH_DIR"


def memory_graph_dir(workspace: Path) -> Optional[Path]:
    """이 작업 공간에서 memory 서버가 그래프를 두는 폴더. 없으면 None.

    서버 이름을 박아 두지 않고 `MEMORY_GRAPH_DIR` 를 쓰는 서버를 찾습니다.
    사용자가 서버 키를 'memory' 가 아닌 다른 이름으로 등록해도 따라갑니다.

    `mcp_servers_for_workspace()` 는 순수 함수입니다 (환경변수를 바꾸지 않습니다).
    그래서 여기서는 그냥 부르고 결과만 읽으면 됩니다.
    """
    try:
        servers = get_config().mcp_servers_for_workspace(workspace)
    except Exception as e:  # noqa: BLE001 - 설정을 못 읽어도 이어받기는 계속됩니다
        logger.warning(f"Could not resolve MCP servers for '{workspace}': {e}")
        return None

    for cfg in servers.values():
        configured = (cfg.env or {}).get(MEMORY_GRAPH_DIR_ENV, "").strip()
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return None


def carry_over_memory_graph(
    source_session_id: str,
    target_session_id: str,
    workspace: str | Path,
) -> bool:
    """이전 대화의 지식 그래프를 새 대화의 것으로 복사합니다.

    복사입니다 — 옮기는 것이 아닙니다. 원본 대화를 나중에 다시 열어도 그때
    쌓아 둔 사실이 그대로 있어야 합니다.

    이미 내용이 있는 대상은 덮지 않습니다. 실패는 전부 흡수하고 False 를
    돌려줍니다: 그래프를 못 옮겼다고 새 대화를 못 만들 이유는 없고, 대신
    **옮겼는지 여부를 호출자가 사람에게 알려야** 합니다. 조용히 빈 그래프로
    시작하는 것이 제일 나쁩니다.
    """
    if not source_session_id or not target_session_id:
        return False
    if source_session_id == target_session_id:
        return False

    graph_dir = memory_graph_dir(resolve_workspace_dir(str(workspace) or None))
    if graph_dir is None:
        logger.info("No MCP server declares MEMORY_GRAPH_DIR; nothing to carry over.")
        return False

    source = graph_dir / f"{source_session_id}.jsonl"
    target = graph_dir / f"{target_session_id}.jsonl"
    try:
        if not source.is_file() or source.stat().st_size == 0:
            logger.info(f"Source conversation has no knowledge graph yet: {source}")
            return False
        if target.exists() and target.stat().st_size > 0:
            logger.warning(f"Target knowledge graph already has content; leaving it alone: {target}")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError as e:
        logger.warning(f"Could not carry the knowledge graph over to '{target}': {e}")
        return False

    logger.info(
        f"Carried the knowledge graph from {source_session_id} to {target_session_id} "
        f"({source.stat().st_size:,} bytes)"
    )
    return True


# 서버마다 상태의 경계가 다릅니다. 어디까지 공유하고 어디부터 나눌지는 호스트가
# 정합니다 — 서버는 자기가 무엇에 묶여 있는지 알 수 없고, 모델에게 맡기면 잊습니다.
#
#   filesystem / git : 폴더 하나를 공유합니다. 경로가 곧 경계이므로 스코프가 없습니다.
#   memory           : 대화 단위. 합의된 사실은 그 토론의 참가자 전원이 함께 봐야 합니다.
#   sandbox          : 발언자 단위. 커널 변수는 어디에도 기록되지 않기 때문입니다 —
#                      다음 발언자의 컨텍스트에는 앞 발언의 본문만 들어가고 도구 실행
#                      로그는 들어가지 않으므로(engine._build_context_for_agent), 커널을
#                      공유하면 자기가 존재도 모르는 변수를 물려받게 됩니다. 에이전트
#                      사이의 인계는 작업 공간 파일로 합니다. 그건 filesystem·git diff·
#                      아티팩트 뷰어에 남아서 검증할 수 있습니다.
AGENT_SCOPED_SERVERS = frozenset({"sandbox"})


def compose_scope(server_name: str, scope: Optional[str], actor: Optional[str]) -> Optional[str]:
    """이 서버에 보낼 스코프를 만듭니다 (`AGENT_SCOPED_SERVERS` 는 발언자까지 포함)."""
    if not scope:
        return None
    if server_name in AGENT_SCOPED_SERVERS and actor:
        return f"{scope}-{actor}"
    return scope


class MCPManager:
    """작업 공간 하나를 보는 MCP 서버 묶음.

    **인스턴스 하나 = 프로세스 묶음 하나 = 작업 공간 하나** 입니다. 예전에는
    프로세스 전체에 하나뿐이어서 작업 공간을 바꾸려면 서버를 통째로 다시 띄워야
    했고, 그래서 서로 다른 폴더를 쓰는 토론이 동시에 돌 수 없었습니다. 지금은
    `MCPRuntimePool` 이 작업 공간마다 이 객체를 하나씩 들고 있습니다
    (`app/mcp/pool.py`).

    작업 공간은 **기동 시점에 고정**됩니다. filesystem 은 허용 경로를 argv 로,
    sandbox 는 `SANDBOX_WORKSPACE` 를 env 로 받고, 둘 다 프로세스가 살아 있는
    동안에는 바꿀 수 없기 때문입니다. 바꾸고 싶으면 이 런타임을 반납하고 다른
    런타임을 받습니다.
    """

    def __init__(
        self,
        server_configs: Optional[Dict[str, MCPServerConfig]] = None,
        workspace: Optional[str | Path] = None,
    ):
        # 작업 공간을 인자로 받습니다. 예전에는 `os.environ["WORKSPACE_DIR"]` 을
        # 읽었는데, 그 값은 프로세스에 하나뿐이라 런타임이 둘 이상이면 서로의
        # 폴더를 가리킵니다.
        self._workspace = resolve_workspace_dir(str(workspace) if workspace else None)
        self.server_configs = server_configs or {}
        self.clients: Dict[str, MCPClientConnection] = {}
        self._tool_lookup: Dict[str, Tuple[MCPClientConnection, str]] = {}  # qualified_name -> (client, tool_name)
        self._initialized = False
        # 이 런타임의 서버들에만 걸리는 환경변수. 풀이 채워 넣습니다.
        self._runtime_env: Dict[str, str] = {}
        # initialize / shutdown / reconnect 직렬화. 이게 없으면 한쪽의
        # `shutdown()` 이 다른 쪽이 방금 채운 `self.clients` 를 지웁니다.
        self._lifecycle = asyncio.Lock()

    @property
    def workspace(self) -> Path:
        """이 런타임의 서버들이 보고 있는 작업 공간."""
        return self._workspace

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def reload_from_config(self) -> Dict[str, Any]:
        """conf.json 을 다시 읽어 서버 목록을 이 작업 공간으로 다시 띄웁니다.

        화면에서 서버를 추가·삭제하거나 켜고 끈 뒤에 부릅니다. 살아 있는 서버만
        골라 손대지 않고 전부 다시 띄웁니다. 서버 프로세스는 명령·인자·환경을
        기동 시점에 받고, 어느 것이 바뀌었는지는 설정 파일 쪽 사정이라 여기서
        정확히 알 수 없기 때문입니다. 이 동작은 토론이 하나도 돌고 있지 않을
        때만 허용되므로(그 판단은 UI 가 합니다) 중간에 도구가 사라지는 일은
        없습니다.

        돌려주는 값은 다시 띄운 뒤의 연결 상태 요약입니다.
        """
        self.server_configs = get_config().mcp_servers_for_workspace(
            self.workspace, self.runtime_env,
        )
        await self.initialize()
        return self.connection_status()

    @property
    def runtime_env(self) -> Dict[str, str]:
        """이 런타임의 서버들에만 걸리는 환경변수.

        `MCPRuntimePool` 이 채워 넣습니다. 풀 없이 직접 만든 매니저는 비어 있고,
        그때는 conf.json 의 기본값과 프로세스 환경을 그대로 씁니다.
        """
        return dict(self._runtime_env)

    def set_runtime_env(self, env: Optional[Mapping[str, str]]) -> None:
        """런타임 전용 환경변수를 지정합니다 (`initialize()` 전에 부릅니다)."""
        self._runtime_env = {k: str(v) for k, v in (env or {}).items()}

    async def initialize(self) -> None:
        """Initializes all configured MCP clients and discovers tools.

        각 클라이언트는 여기서 연 세션을 `shutdown()` 까지 유지합니다.

        `sync_vendored_servers` 는 여기서 부릅니다. 그것은 파일을 놓는 일이자
        **설정에 적힌 상대 경로를 절대 경로로 고치는** 일이고, 후자는 서버를
        띄우기 직전마다 필요합니다 — 런타임은 conf.json 원문을 자기 작업 공간으로
        새로 풀기 때문에, 한 번 고쳐 둔 설정을 물려받지 않습니다. 상대 경로를 그대로
        넘기면 자식 프로세스가 자기 cwd 로 풀어서 엉뚱한 파일을 봅니다.

        파일 쓰기 자체는 내용이 같으면 건너뜁니다. 런타임 기동은 풀의 락이
        직렬화하므로(`app/mcp/pool.py`) 같은 파일에 동시에 쓰는 일도 없습니다.
        """
        async with self._lifecycle:
            await self._shutdown_locked()
            self._tool_lookup.clear()

            await ensure_workspace_async(self.workspace)
            sync_vendored_servers(self.server_configs)
            absolutize_local_script_args(self.server_configs)

            for name in self.server_configs:
                await self._start_client(name)

            self._rebuild_tool_lookup()
            self._initialized = True

        tool_total = sum(len(c.tools) for c in self.clients.values())
        connected = [n for n, c in self.clients.items() if c.is_connected]
        failed = [n for n in self.clients if n not in connected]
        logger.info(
            f"MCP runtime for '{self.workspace}' initialized. Connected: {connected or '-'} | "
            f"Unavailable: {failed or '-'} | Total registered tools: {tool_total}"
        )

    async def shutdown(self) -> None:
        """열려 있는 모든 MCP 세션과 서버 프로세스를 정리합니다."""
        async with self._lifecycle:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        """`_lifecycle` 을 이미 쥐고 있는 쪽이 부르는 정리."""
        for name, client in list(self.clients.items()):
            try:
                await client.close()
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 - 종료 경로의 오류는 무시
                logger.warning(f"Error closing MCP server '{name}': {type(e).__name__}: {e}")
        self.clients.clear()
        self._initialized = False

    async def _start_client(self, name: str) -> None:
        """서버 하나를 띄우고 도구를 검색합니다. 실패해도 예외를 올리지 않습니다."""
        cfg = self.server_configs[name]
        client = MCPClientConnection(
            server_name=name,
            command=cfg.command,
            args=cfg.args,
            env=cfg.env,
            url=cfg.url,
            headers=cfg.headers,
            transport=cfg.transport,
            timeout=cfg.timeout,
            workspace=self.workspace,
        )
        self.clients[name] = client
        try:
            await client.discover_tools()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - 한 서버 때문에 앱이 못 뜨면 안 됩니다
            logger.error(
                f"Failed to discover tools for MCP server '{name}': {type(e).__name__}: {e}",
                exc_info=True,
            )

    def _rebuild_tool_lookup(self) -> None:
        """모든 클라이언트의 도구로 조회 인덱스를 다시 만듭니다."""
        self._tool_lookup.clear()
        for client in self.clients.values():
            for tool in client.tools:
                self._tool_lookup[tool.qualified_name] = (client, tool.name)
                # Also allow plain tool name if unambiguous
                if tool.name not in self._tool_lookup:
                    self._tool_lookup[tool.name] = (client, tool.name)

    async def reconnect(self, server_name: Optional[str] = None) -> bool:
        """서버 하나(또는 실패한 서버 전부)를 다시 띄웁니다.

        UI 의 재연결 버튼용입니다. 하나라도 새로 붙었으면 True 를 반환합니다.
        """
        if server_name is not None:
            targets = [server_name] if server_name in self.server_configs else []
        else:
            targets = [n for n in self.server_configs
                       if n not in self.clients or not self.clients[n].is_connected]

        if not targets:
            return False

        async with self._lifecycle:
            for name in targets:
                existing = self.clients.pop(name, None)
                if existing is not None:
                    try:
                        await existing.close()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"Error closing MCP server '{name}' before reconnect: {e}")
                await self._start_client(name)

            self._rebuild_tool_lookup()
        reconnected = [n for n in targets if self.clients[n].is_connected]
        logger.info(f"MCP reconnect requested for {targets} -> connected: {reconnected or '-'}")
        return bool(reconnected)

    def connection_status(self) -> Dict[str, Dict[str, Any]]:
        """서버별 연결 상태 요약 (UI/헬스체크용)."""
        return {
            name: {
                "connected": client.is_connected,
                "available": client.is_available,
                "tool_count": len(client.tools),
                # 화면은 "이 서버가 어디에 있는가" 를 보여줍니다. 로컬이면 실행
                # 명령, 원격이면 주소입니다.
                "command": client.endpoint_label,
                "endpoint": client.endpoint_label,
                "transport": client.transport,
                "remote": client.is_remote,
                "error": client.connect_error,
            }
            for name, client in self.clients.items()
        }

    def get_tools_for_servers(self, allowed_servers: List[str]) -> List[MCPToolDefinition]:
        """Returns tool definitions available for the specified server names."""
        tools: List[MCPToolDefinition] = []
        for srv_name in allowed_servers:
            if srv_name in self.clients:
                tools.extend(self.clients[srv_name].tools)
        return tools

    def get_openai_tools_for_servers(self, allowed_servers: List[str]) -> List[Dict[str, Any]]:
        """Returns OpenAI/LiteLLM function schemas for allowed servers."""
        tools = self.get_tools_for_servers(allowed_servers)
        return [t.to_openai_tool() for t in tools]

    async def execute_tool(
        self, tool_name: str, arguments: Any, scope: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Executes a tool by qualified name (e.g. 'filesystem__read_file') or plain name ('read_file').
        Returns (output_str, status ['success'|'error']).

        `scope` 는 이 호출이 속한 대화(세션)의, `actor` 는 지금 발언 중인 에이전트의
        식별자입니다. 둘을 어떻게 조합해 서버에 보낼지는 `compose_scope()` 가 정합니다.

        **여기가 도구 실패의 경계선입니다.** 어떤 서버가 어떻게 터지든 이 함수는
        `(설명, "error")` 를 돌려줄 뿐 예외를 올리지 않습니다. 도구 하나가 실패한
        것은 에이전트가 읽고 고칠 관측이지, 발언·토론·프로세스를 끝낼 이유가
        아니기 때문입니다. 유일한 예외는 `CancelledError` 입니다 — 그건 실패가
        아니라 "그만두라" 는 지시여서, 삼키면 취소도 서버 종료도 먹지 않습니다.
        """
        # Ensure arguments are dict
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {"input": arguments}
        elif not isinstance(arguments, dict):
            arguments = {}

        # 텍스트 도구로 .pptx/.xlsx 를 쓰려는 시도는 여기서 끊습니다. 흘려보내면
        # 도구는 성공을 보고하고 사용자는 열리지 않는 파일을 받습니다. 자세한 이유는
        # `app/mcp/guards.py` 를 보세요.
        refusal = binary_write_refusal(tool_name, arguments, self._tool_lookup.keys())
        if refusal:
            logger.info(f"Refused binary write via '{tool_name}' (actor={actor}).")
            return refusal, "error"

        client: Optional[MCPClientConnection] = None
        actual_tool_name = tool_name

        if tool_name in self._tool_lookup:
            client, actual_tool_name = self._tool_lookup[tool_name]
        elif "__" in tool_name:
            # Check if qualified name split works e.g. server__tool
            server_name, split_tool_name = tool_name.split("__", 1)
            if server_name in self.clients:
                client, actual_tool_name = self.clients[server_name], split_tool_name

        if client is None:
            known = sorted({name for name in self._tool_lookup})
            return (
                f"Unknown tool: '{tool_name}'. Available tools: {known}",
                "error",
            )

        try:
            result = await client.execute_tool(
                actual_tool_name, arguments,
                scope=compose_scope(client.server_name, scope, actor),
            )
            return clip_tool_output(result if isinstance(result, str) else str(result)), "success"
        except MCPToolError as e:
            # 서버가 보고한 실패 메시지를 그대로 전달합니다 (모델이 읽고 교정).
            return clip_tool_output(e.message), "error"
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - 위 docstring 참고
            logger.warning(
                f"Tool '{tool_name}' on '{client.server_name}' failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            return (
                f"Tool execution failed ({type(e).__name__}): {e}\n"
                f"이 도구는 지금 쓸 수 없습니다. 다른 방법을 쓰거나, 이미 확보한 "
                f"정보만으로 결론을 내세요.",
                "error",
            )


def get_mcp_manager() -> MCPManager:
    """기본 작업 공간의 런타임.

    예전에는 이것이 프로세스 전체의 유일한 매니저였습니다. 지금은
    `MCPRuntimePool` 이 작업 공간마다 하나씩 들고 있고, 이 함수는 그중 기본
    작업 공간의 것을 돌려줍니다. 토론 경로는 세션의 작업 공간으로 런타임을
    받아 쓰므로 이 함수를 쓰지 않습니다 — 아직 어느 대화에도 속하지 않은
    화면(설정 패널의 기본값 표시 등)과 옛 호출자를 위한 자리입니다.

    **아직 떠 있지 않으면 빈 런타임을 돌려줍니다.** 여기서 서버를 띄우지는
    않습니다. 기동은 소유자가 분명한 자리(풀의 `acquire`)에서만 일어나야
    반납도 그 짝을 맞출 수 있습니다.
    """
    from app.mcp.pool import get_runtime_pool

    return get_runtime_pool().default_runtime()
