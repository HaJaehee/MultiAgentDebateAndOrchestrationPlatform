"""외부 플러그인 아키텍처 — 코드 수정 없이 토론 전략 및 호스트 도구를 확장합니다 (ADR-042).

플러그인은 다음 두 경로를 통해 탐색 및 로드됩니다:

* **로컬 디렉터리**: `plugins.strategies_dir` (기본 `strategies/`), `plugins.host_tools_dir` (기본 `host_tools/`).
  단일 파일(`voting.py`) 또는 패키지 디렉터리(`voting/__init__.py`) 단위로 탐색하며, `_` 또는 `.`으로 시작하는 항목은 제외합니다.
  내부에 정의된 구체 구현체(`BaseDebateStrategy`, `HostToolProvider` 상속 클래스)를 자동으로 등록합니다.
* **파이썬 Entry Point**: `mado.strategies`, `mado.host_tools` 엔트리 포인트 그룹에 등록된 패키지들을 로드합니다.

**보안 및 실행 격리 원칙**:
플러그인은 호스트 애플리케이션과 동일한 프로세스 권한으로 동작하므로 다음 보안 규칙을 엄격히 적용합니다:
* 서버 시작 시점에만 1회 로드합니다 (실시간 동적 핫 리로딩 금지).
* 기본 작업 공간(`workspace/`) 내부의 플러그인 디렉터리는 탐색 대상에서 원천 차단합니다 (`app.mcp.manager.protected_files`).
* `plugins.disabled` 목록에 포함된 플러그인은 임포트 자체를 차단합니다.
* 플러그인 로드 오류가 전체 애플리케이션 기동 실패로 전파되지 않도록 격리 처리하며, 오류 내역은 `plugin_records()` 및 관리 API로 제공합니다.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Iterator, List, Optional, Tuple

from app.agents.host_tools import HostToolProvider, register_host_tool
from app.config import RootConfig, get_config, resolve_workspace_dir
from app.orchestration.strategies import BaseDebateStrategy, register_strategy

logger = logging.getLogger(__name__)

STRATEGY_KIND = "strategy"
HOST_TOOL_KIND = "host_tool"
STRATEGY_GROUP = "mado.strategies"
HOST_TOOL_GROUP = "mado.host_tools"


@dataclass(frozen=True)
class PluginRecord:
    """플러그인 로드 결과 모델. 로드 실패 시 `problem` 필드에 오류 원인이 기록됩니다."""

    kind: str
    source: str         # 파일·폴더 경로, 또는 `mado.strategies:이름`
    name: str = ""      # 등록된 이름 (전략 이름, 호스트 도구 접두사)
    problem: str = ""


@dataclass(frozen=True)
class _Kind:
    kind: str
    group: str
    base: type
    register: Callable[[Any], None]
    name_of: Callable[[Any], str]


_KINDS = (
    _Kind(STRATEGY_KIND, STRATEGY_GROUP, BaseDebateStrategy, register_strategy, lambda s: s.name),
    _Kind(HOST_TOOL_KIND, HOST_TOOL_GROUP, HostToolProvider, register_host_tool, lambda p: p.prefix),
)

_records: List[PluginRecord] = []
_loaded = False


def plugin_records() -> List[PluginRecord]:
    """이번 실행에서 읽은 플러그인의 결과."""
    return list(_records)


def plugin_problems() -> List[PluginRecord]:
    """등록되지 못한 것들."""
    return [record for record in _records if record.problem]


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _import_file(kind: str, path: Path) -> ModuleType:
    """단일 파일 또는 패키지 디렉터리를 동적으로 임포트합니다. 네임스페이스 충돌을 방지하기 위해 전용 모듈명을 사용합니다."""
    module_name = f"mado_plugin_{kind}_{path.stem}"
    if path.is_dir():
        spec = importlib.util.spec_from_file_location(
            module_name, path / "__init__.py", submodule_search_locations=[str(path)],
        )
    else:
        spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} 를 읽을 수 없습니다")
    module = importlib.util.module_from_spec(spec)
    # dataclass 와 상대 import 는 실행 중에 자기 모듈을 `sys.modules` 에서 찾습니다.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _classes_in(module: ModuleType, base: type) -> List[type]:
    """모듈 내에 직접 정의된 구체 구현체 클래스들을 추출합니다. 단순 임포트된 기반 클래스는 제외합니다."""
    own = module.__name__
    return [
        value for value in vars(module).values()
        if inspect.isclass(value) and issubclass(value, base) and not inspect.isabstract(value)
        and (value.__module__ == own or value.__module__.startswith(f"{own}."))
    ]


def _adopt(kind: _Kind, source: str, loaded: Any) -> List[PluginRecord]:
    """임포트된 모듈에서 전략 및 호스트 도구 구현체를 추출하여 등록합니다. 개별 등록 실패 시에도 나머지는 정상 등록됩니다."""
    if isinstance(loaded, ModuleType):
        makers: List[Callable[[], Any]] = list(_classes_in(loaded, kind.base))
    elif inspect.isclass(loaded) and issubclass(loaded, kind.base):
        makers = [loaded]
    elif isinstance(loaded, kind.base):
        makers = [lambda: loaded]
    else:
        makers = []
    if not makers:
        return [PluginRecord(kind.kind, source, problem=f"{kind.base.__name__} 의 구체 클래스를 찾지 못했습니다.")]

    records: List[PluginRecord] = []
    for make in makers:
        try:
            instance = make()
            kind.register(instance)
            records.append(PluginRecord(kind.kind, source, name=kind.name_of(instance)))
        except Exception as exc:  # noqa: BLE001 - 플러그인의 실패로 앱을 세우지 않습니다
            records.append(PluginRecord(kind.kind, source, problem=_describe(exc)))
    return records


def _inside_default_workspace(folder: Path) -> bool:
    try:
        return folder.resolve().is_relative_to(resolve_workspace_dir().resolve())
    except OSError:
        return True


def _folder_entries(folder: Path) -> Iterator[Tuple[str, Path]]:
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith(("_", ".")):
            continue
        if path.is_dir() and (path / "__init__.py").is_file():
            yield path.name, path
        elif path.is_file() and path.suffix == ".py":
            yield path.stem, path


def _load_folder(kind: _Kind, folder: Path, disabled: Iterable[str]) -> List[PluginRecord]:
    if not folder.is_dir():
        return []
    if _inside_default_workspace(folder):
        return [PluginRecord(
            kind.kind, str(folder),
            problem="기본 작업 공간 안의 폴더는 읽지 않습니다. 에이전트가 쓸 수 있는 곳의 코드를 앱 권한으로 실행하게 됩니다.",
        )]
    records: List[PluginRecord] = []
    try:
        entries = list(_folder_entries(folder))
    except OSError as exc:
        return [PluginRecord(kind.kind, str(folder), problem=_describe(exc))]
    off = set(disabled)
    for name, path in entries:
        if name in off:
            continue
        try:
            module = _import_file(kind.kind, path)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - 깨진 파일 하나로 앱을 세우지 않습니다
            records.append(PluginRecord(kind.kind, str(path), problem=_describe(exc)))
            continue
        records.extend(_adopt(kind, str(path), module))
    return records


def _load_entry_points(kind: _Kind, disabled: Iterable[str]) -> List[PluginRecord]:
    off = set(disabled)
    records: List[PluginRecord] = []
    for entry in importlib.metadata.entry_points(group=kind.group):
        if entry.name in off:
            continue
        source = f"{kind.group}:{entry.name}"
        try:
            loaded = entry.load()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - 깨진 패키지 하나로 앱을 세우지 않습니다
            records.append(PluginRecord(kind.kind, source, problem=_describe(exc)))
            continue
        records.extend(_adopt(kind, source, loaded))
    return records


def load_plugins(config: Optional[RootConfig] = None, *, reload: bool = False) -> List[PluginRecord]:
    """구성된 모든 플러그인을 탐색하여 레지스트리에 등록합니다. 서버 기동 시 1회 실행되며 중복 로드를 방지합니다.

    `reload` 는 시험이 씁니다. 실행 중인 앱은 등록한 것을 내리지 않습니다.
    """
    global _loaded
    if _loaded and not reload:
        return plugin_records()
    settings = (config or get_config()).plugins
    records: List[PluginRecord] = []
    for kind in _KINDS:
        folder = settings.folders.get(kind.kind)
        if folder is not None:
            records.extend(_load_folder(kind, folder, settings.disabled))
        records.extend(_load_entry_points(kind, settings.disabled))
    _records[:] = records
    _loaded = True
    for record in records:
        if record.problem:
            logger.warning(f"Plugin {record.source} was not loaded: {record.problem}")
        else:
            logger.info(f"Loaded plugin {record.kind} '{record.name}' from {record.source}")
    return plugin_records()
