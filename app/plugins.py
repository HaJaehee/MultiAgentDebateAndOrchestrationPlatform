"""외부 플러그인 — 코드를 고치지 않고 토론 전략과 호스트 도구를 더합니다 (ADR-042).

두 곳에서 찾습니다.

* **폴더** — `plugins.strategies_dir`(기본 `strategies/`), `plugins.host_tools_dir`(기본 `host_tools/`). 파일 하나
  (`voting.py`)나 패키지 폴더 하나(`voting/__init__.py`)가 플러그인 하나입니다. `_` 나 `.` 으로 시작하는 이름은
  건너뜁니다. 그 안의 구체 클래스(`BaseDebateStrategy`, `HostToolProvider` 의 하위 클래스)를 전부 등록합니다.
* **entry point** — `mado.strategies`, `mado.host_tools` 그룹. `pip install` 한 패키지가 클래스, 객체, 모듈을 가리킵니다.

**앱과 같은 권한으로 도는 코드입니다.** 그래서 이렇게 다룹니다.

* 서버를 시작할 때 한 번만 읽습니다. 스킬처럼 실시간으로 반영하면 파일을 쓸 수 있는 누구나 곧바로 코드를 실행합니다.
* 폴더는 도구로 건드릴 수 없습니다 (`app.mcp.manager.protected_files`). 기본 작업 공간 안의 폴더는 아예 읽지 않습니다.
  에이전트가 코드를 실행해 쓸 수 있는 곳이라, 다음 시작 때 앱 권한을 얻는 길이 됩니다.
* `plugins.disabled` 에 적은 이름은 import 도 하지 않습니다. entry point 는 패키지를 지우지 않고도 이렇게 끕니다.

**깨진 플러그인이 앱을 막지 않습니다.** 설정을 고치려면 화면이 떠야 합니다. 실패는 로그와 `plugin_records()` 에 남고,
`GET /api/plugins` 와 로스터의 전략 칸에서 볼 수 있습니다.
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
    """플러그인에서 나온 것 하나의 결과. `problem` 이 있으면 등록되지 않았습니다."""

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
    """파일이나 패키지 폴더 하나를 import 합니다. 이름은 앱의 모듈과 겹치지 않게 짓습니다."""
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
    """모듈이 정의한 구체 클래스. 모듈이 가져다 쓴 다른 클래스(`BaseDebateStrategy` 등)는 뺍니다."""
    own = module.__name__
    return [
        value for value in vars(module).values()
        if inspect.isclass(value) and issubclass(value, base) and not inspect.isabstract(value)
        and (value.__module__ == own or value.__module__.startswith(f"{own}."))
    ]


def _adopt(kind: _Kind, source: str, loaded: Any) -> List[PluginRecord]:
    """import 한 것에서 전략·호스트 도구를 꺼내 등록합니다. 하나가 실패해도 나머지는 등록합니다."""
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
    """플러그인을 읽어 등록합니다. 시작할 때 한 번이면 되고, 또 불러도 다시 읽지 않습니다.

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
