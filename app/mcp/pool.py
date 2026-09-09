"""작업 공간별 MCP 런타임 풀.

## 왜 이것이 있는가

MCP 서버는 자기가 볼 폴더를 **기동 시점에** 받습니다 — filesystem 은 허용 경로를
argv 로, sandbox 는 `SANDBOX_WORKSPACE` 를 env 로. 프로세스가 살아 있는 동안에는
바꿀 수 없습니다. 그래서 매니저가 프로세스 전체에 하나뿐이던 시절에는 작업 공간이
다른 토론 두 개가 동시에 돌 수 없었고, 두 번째 토론은 조용히 남의 폴더를 쓰느니
거절되었습니다(`WorkspaceConflictError`).

여기서는 매니저를 여러 개 둡니다. 하나가 폴더 하나를 봅니다.

## 무엇을 격리하고, 무엇을 공유하는가

격리의 층을 나눠서 봐야 합니다.

    node.exe / python.exe          공유   실행 중 읽기 전용입니다. 복제해도 얻는 것이 없습니다.
    MCP_NODE_HOME (node_modules)   공유   같은 이유. 다만 런타임별로 덮어쓸 수 있게 열어 둡니다.
    서버 프로세스와 그 환경          격리   여기가 실제로 새던 자리입니다.

즉 "node 런타임을 세션별로 격리한다" 는 node 를 여러 벌 설치하는 것이 아니라,
**폴더마다 별도의 node/python 프로세스 묶음을 자기 환경으로 띄우는 것**입니다.
힙, cwd, TMP, `WORKSPACE_DIR`, 메모리 그래프 경로, 샌드박스의 IPython 커널이
그 환경에 딸려 갑니다.

## 왜 세션이 아니라 작업 공간을 키로 쓰는가

같은 폴더를 보는 세션끼리는 파일을 이미 공유합니다. 프로세스를 따로 띄워도
격리되는 것이 없고, 프로세스 수만 세션 수만큼 늘어납니다. 그 안에서의 대화 단위
격리는 요청 메타데이터(`_meta.conversationId`)가 이미 하고 있습니다
(`app/mcp/manager.py` 의 `compose_scope`).

세션 하나만 완전히 떼어 놓고 싶으면 그 세션에 전용 폴더를 주면 됩니다. 그러면
이 풀이 알아서 별도 묶음을 띄웁니다.

## 수명

`acquire()` 로 참조를 잡고 `release()` 로 놓습니다. 참조가 0 이 되어도 바로 죽이지
않고 `MCP_RUNTIME_IDLE_TTL` 동안 살려 둡니다 — 같은 폴더에서 턴을 연달아 돌릴 때
기동 비용(샌드박스는 IPython 커널을 띄워 수 초)을 매번 치르지 않기 위해서입니다.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.config import get_config, resolve_workspace_dir
from app.mcp.manager import MCPManager

logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    """환경 변수를 양의 정수로 읽습니다. 비었거나 이상하면 기본값."""
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# 동시에 띄워 둘 수 있는 런타임(=서로 다른 작업 공간)의 수.
#
# 런타임 하나가 conf.json 의 켜진 서버 수만큼 프로세스를 띄웁니다(기본 4개:
# node 2 + python 2). 여기에 샌드박스가 자기 안에서 IPython 커널을 더 띄웁니다.
# 그래서 이 값은 "동시에 돌릴 토론 수" 가 아니라 **메모리 예산**입니다.
MAX_RUNTIMES = _positive_int_env("MCP_MAX_RUNTIMES", 4)

# 아무도 안 쓰는 런타임을 살려 두는 시간(초).
IDLE_TTL = _positive_float_env("MCP_RUNTIME_IDLE_TTL", 300.0)

# 샌드박스 커널은 런타임마다 따로 뜹니다. conf.json 의 값을 그대로 N 벌 두면
# 커널 수가 곱으로 붙으므로, 런타임 상한으로 나눠 예산을 유지합니다.
SANDBOX_NAMESPACE_BUDGET = _positive_int_env("SANDBOX_MAX_NAMESPACES", 16)


class RuntimeCapacityError(RuntimeError):
    """런타임 자리가 다 찼을 때.

    조용히 남의 폴더를 쓰느니 거절합니다 — 예전 `WorkspaceConflictError` 가 하던
    역할을 이어받습니다. 다만 이유가 바뀌었습니다: 이제는 "동시에 두 폴더를 쓸 수
    없어서" 가 아니라 "지금 그만큼 띄울 예산이 없어서" 입니다.
    """


class _Runtime:
    """풀이 들고 있는 런타임 하나 (매니저 + 그것을 붙잡고 있는 사람들)."""

    def __init__(self, workspace: Path, manager: MCPManager):
        self.workspace = workspace
        self.manager = manager
        self.holders: Set[str] = set()
        self.idle_since: Optional[float] = time.monotonic()

    @property
    def in_use(self) -> bool:
        return bool(self.holders)

    def idle_for(self) -> float:
        return 0.0 if self.idle_since is None else time.monotonic() - self.idle_since


class MCPRuntimePool:
    """작업 공간 경로 -> 그 폴더를 보는 MCP 서버 묶음."""

    def __init__(self, max_runtimes: int = MAX_RUNTIMES, idle_ttl: float = IDLE_TTL):
        self.max_runtimes = max(1, int(max_runtimes))
        self.idle_ttl = float(idle_ttl)
        self._runtimes: Dict[str, _Runtime] = {}
        # 획득·반납·회수가 서로를 밟지 않게 하는 하나의 락. 런타임을 띄우는 데
        # 수 초가 걸리므로 그 사이 다른 세션이 같은 폴더를 또 띄우지 않도록
        # 기동까지 락 안에서 합니다.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 키

    @staticmethod
    def key_for(workspace: Optional[str | Path]) -> str:
        """작업 공간 값을 런타임 키(정규화된 절대 경로 문자열)로 바꿉니다."""
        return str(resolve_workspace_dir(str(workspace) if workspace else None))

    # ------------------------------------------------------------------ 런타임 환경

    def runtime_env(self, workspace: Path) -> Dict[str, str]:
        """이 런타임의 서버 프로세스에만 걸리는 환경변수.

        여기 있는 것과 없는 것을 구분해서 봐야 합니다.

        **넣는 것** — 런타임마다 달라야 하는 상태의 자리입니다. 임시 파일이
        섞이면 같은 이름의 중간 산출물을 두 토론이 서로 덮어씁니다.

        **넣지 않는 것** — `HOME` / `USERPROFILE`. git 이 사용자 설정을,
        파이썬이 사용자 사이트 패키지를 거기서 찾습니다. 옮기면 격리가 아니라
        고장입니다.

        `NODE_BIN` / `MCP_NODE_HOME` 도 건드리지 않습니다. 실행 중 읽기 전용이라
        공유해도 새지 않습니다. 런타임별로 다른 node 나 다른 node_modules 가
        필요해지면 이 사전에 얹는 것만으로 열립니다.
        """
        private = Path(workspace) / ".mado"
        # 런타임을 나눠 띄우는 만큼 커널 예산도 나눕니다.
        namespaces = max(2, SANDBOX_NAMESPACE_BUDGET // self.max_runtimes)
        return {
            "WORKSPACE_DIR": str(workspace),
            "TMP": str(private / "tmp"),
            "TEMP": str(private / "tmp"),
            "TMPDIR": str(private / "tmp"),
            "npm_config_cache": str(private / "npm-cache"),
            "SANDBOX_MAX_NAMESPACES": str(namespaces),
        }

    # ------------------------------------------------------------------ 획득 / 반납

    async def acquire(self, workspace: Optional[str | Path], holder: str) -> MCPManager:
        """이 작업 공간의 런타임을 빌립니다. 없으면 띄웁니다.

        `holder` 는 보통 세션 id 입니다. 같은 holder 가 두 번 잡아도 참조는
        하나입니다 (집합이라서). 반드시 `release(holder)` 로 짝을 맞추세요.
        """
        key = self.key_for(workspace)
        async with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                await self._evict_for_room_locked(key)
                runtime = await self._start_locked(key)
            runtime.holders.add(holder)
            runtime.idle_since = None
            return runtime.manager

    async def release(self, holder: str, workspace: Optional[str | Path] = None) -> None:
        """빌린 런타임을 놓습니다. 프로세스를 바로 죽이지는 않습니다.

        `workspace` 를 주면 그 런타임만, 주지 않으면 이 holder 가 잡고 있던
        전부를 놓습니다. 턴이 어떻게 끝나든(정상·정지·취소·예외) 불려야 하므로
        아무것도 못 찾아도 조용히 지나갑니다.
        """
        async with self._lock:
            keys = [self.key_for(workspace)] if workspace is not None else list(self._runtimes)
            for key in keys:
                runtime = self._runtimes.get(key)
                if runtime is None or holder not in runtime.holders:
                    continue
                runtime.holders.discard(holder)
                if not runtime.in_use:
                    runtime.idle_since = time.monotonic()
                    logger.info(
                        f"MCP runtime for '{key}' is idle; keeping it warm for {self.idle_ttl:.0f}s"
                    )
            await self._reap_expired_locked()

    # ------------------------------------------------------------------ 내부

    async def _start_locked(self, key: str) -> _Runtime:
        workspace = Path(key)
        env = self.runtime_env(workspace)
        try:
            server_configs = get_config().mcp_servers_for_workspace(workspace, env)
        except Exception as e:  # noqa: BLE001 - 설정을 못 읽어도 앱은 떠 있어야 합니다
            logger.error(f"Could not resolve MCP servers for '{key}': {e}", exc_info=True)
            server_configs = {}

        manager = MCPManager(server_configs, workspace=workspace)
        manager.set_runtime_env(env)
        runtime = _Runtime(workspace, manager)
        # 서버가 하나도 못 떠도 런타임은 등록합니다. 도구 없이 토론하는 것과
        # 토론이 시작조차 안 되는 것은 다른 이야기입니다.
        self._runtimes[key] = runtime
        try:
            await manager.initialize()
        except asyncio.CancelledError:
            self._runtimes.pop(key, None)
            raise
        except BaseException as e:  # noqa: BLE001
            logger.error(
                f"MCP runtime for '{key}' could not be initialized ({type(e).__name__}: {e}); "
                f"continuing without its tools.",
                exc_info=True,
            )
        return runtime

    async def _stop_locked(self, key: str) -> None:
        runtime = self._runtimes.pop(key, None)
        if runtime is None:
            return
        try:
            await runtime.manager.shutdown()
            logger.info(f"MCP runtime for '{key}' stopped.")
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - 정리 실패로 다음 동작을 막지 않습니다
            logger.warning(f"Could not stop the MCP runtime for '{key}': {type(e).__name__}: {e}")

    async def _reap_expired_locked(self) -> None:
        """TTL 을 넘긴 유휴 런타임을 정리합니다."""
        for key, runtime in list(self._runtimes.items()):
            if not runtime.in_use and runtime.idle_for() >= self.idle_ttl:
                await self._stop_locked(key)

    async def _evict_for_room_locked(self, wanted: str) -> None:
        """새 런타임 자리를 만듭니다. 못 만들면 거절합니다."""
        await self._reap_expired_locked()
        if len(self._runtimes) < self.max_runtimes:
            return

        # 오래 놀고 있던 것부터 내립니다.
        idle = sorted(
            (r for r in self._runtimes.values() if not r.in_use),
            key=lambda r: r.idle_for(),
            reverse=True,
        )
        while idle and len(self._runtimes) >= self.max_runtimes:
            await self._stop_locked(str(idle.pop(0).workspace))

        if len(self._runtimes) < self.max_runtimes:
            return

        busy = [
            f"{r.workspace} ({', '.join(sorted(r.holders))})"
            for r in self._runtimes.values() if r.in_use
        ]
        raise RuntimeCapacityError(
            f"MCP 런타임 자리가 다 찼습니다 (상한 {self.max_runtimes}개). "
            f"'{wanted}' 를 띄우려면 먼저 다음 중 하나가 끝나야 합니다: "
            f"{'; '.join(busy)}. "
            f"상한은 MCP_MAX_RUNTIMES 환경변수로 올릴 수 있지만, 런타임 하나마다 "
            f"MCP 서버 프로세스 묶음이 통째로 하나 더 뜬다는 점을 감안하세요."
        )

    # ------------------------------------------------------------------ 조회 / 종료

    def get(self, workspace: Optional[str | Path]) -> Optional[MCPManager]:
        """이미 떠 있는 런타임. 없으면 None (**띄우지 않습니다**).

        화면이 "이 대화의 도구는 지금 어떤 상태인가" 를 물을 때 씁니다. 화면을
        그리는 것만으로 서버가 뜨면, 사이드바를 클릭할 때마다 프로세스 묶음이
        하나씩 늘어납니다.
        """
        runtime = self._runtimes.get(self.key_for(workspace))
        return runtime.manager if runtime else None

    def default_runtime(self) -> MCPManager:
        """기본 작업 공간의 런타임. 아직 없으면 **빈** 매니저를 만들어 둡니다.

        띄우지는 않습니다 (`initialize()` 를 부르지 않습니다). 소유자 없이 뜬
        프로세스는 아무도 반납하지 않아서 종료 때까지 남습니다.
        """
        key = self.key_for(None)
        runtime = self._runtimes.get(key)
        if runtime is None:
            workspace = Path(key)
            manager = MCPManager({}, workspace=workspace)
            manager.set_runtime_env(self.runtime_env(workspace))
            runtime = _Runtime(workspace, manager)
            self._runtimes[key] = runtime
        return runtime.manager

    async def warm_default(self) -> MCPManager:
        """기본 작업 공간의 런타임을 미리 띄웁니다 (기동 시 1회).

        첫 토론이 서버 기동을 기다리지 않게 하는 것이 목적입니다. 아무도
        붙잡고 있지 않으므로 TTL 이 지나면 알아서 정리됩니다.
        """
        key = self.key_for(None)
        async with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is not None and runtime.manager.is_initialized:
                return runtime.manager
            self._runtimes.pop(key, None)
            runtime = await self._start_locked(key)
            return runtime.manager

    def status(self) -> Dict[str, Dict]:
        """런타임별 상태 (UI/헬스체크용)."""
        return {
            key: {
                "workspace": str(runtime.workspace),
                "holders": sorted(runtime.holders),
                "idle_seconds": round(runtime.idle_for(), 1) if not runtime.in_use else 0.0,
                "initialized": runtime.manager.is_initialized,
                "servers": runtime.manager.connection_status(),
            }
            for key, runtime in self._runtimes.items()
        }

    def live_workspaces(self) -> List[str]:
        return list(self._runtimes)

    async def reload_all(self) -> Dict[str, Dict]:
        """conf.json 을 다시 읽어 **살아 있는 런타임 전부**를 다시 띄웁니다.

        서버 프로세스는 작업 공간마다 나뉘지만, 무엇을 어떻게 띄울지는 여전히
        파일 하나가 정합니다. 한 런타임만 갱신하면 화면은 새 구성을, 다른 폴더의
        도구는 옛 서버를 가리키게 됩니다.

        이 동작은 토론이 하나도 돌고 있지 않을 때만 허용됩니다 (그 판단은 UI 가
        합니다). 아직 뜨지 않은 런타임은 다음에 뜰 때 어차피 새 설정을 읽습니다.
        """
        async with self._lock:
            for key, runtime in list(self._runtimes.items()):
                if not runtime.manager.is_initialized:
                    continue
                try:
                    await runtime.manager.reload_from_config()
                except asyncio.CancelledError:
                    raise
                except BaseException as e:  # noqa: BLE001 - 하나가 실패해도 나머지는 갱신합니다
                    logger.error(
                        f"Could not reload the MCP runtime for '{key}': {type(e).__name__}: {e}",
                        exc_info=True,
                    )
            return self.status()

    async def shutdown_all(self) -> None:
        """서버 종료 시 모든 런타임을 정리합니다."""
        async with self._lock:
            for key in list(self._runtimes):
                await self._stop_locked(key)


_pool: Optional[MCPRuntimePool] = None


def get_runtime_pool() -> MCPRuntimePool:
    global _pool
    if _pool is None:
        _pool = MCPRuntimePool()
    return _pool
