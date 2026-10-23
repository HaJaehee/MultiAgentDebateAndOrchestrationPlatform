"""모듈 사이 의존 방향.

`app/` 의 패키지는 아래 방향으로만 기댑니다. 거꾸로 가는 import 하나가 순환을 만들고, 그
순환을 함수 안 import 로 가리면 어디가 무엇에 기대는지 아무도 모르게 됩니다. 그래서
함수 안 import 도 똑같이 셉니다(`TYPE_CHECKING` 블록만 뺍니다).

    화면 (ui · trial · easy)  ──▶  orchestration  ──▶  agents  ──▶  mcp  ──▶  config
                                     │                   │
                                     └──────────▶  database  ◀────┘

- 토론을 돌리는 쪽(orchestration · agents · mcp · database · config)은 화면도, NiceGUI 도 모릅니다.
  그래야 브라우저가 끊겨도 토론이 백그라운드에서 끝납니다.
- 화면 패키지끼리는 서로 가져다 쓰지 않습니다. 함께 쓰는 것은 `app.ui.shared` 와
  `app.ui.components` 에 둡니다.

`KNOWN_VIOLATIONS` 는 리팩터링(docs/refactoring-plan.md) 중에 아직 남은 위반입니다. 줄이기만
하고 늘리지 않습니다. 고쳐서 사라진 항목이 목록에 남아 있으면 그것도 실패로 알립니다.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

APP = Path(__file__).resolve().parents[1] / "app"

SCREENS = ("app.ui", "app.trial", "app.easy")
ENGINE_SIDE = ("app.orchestration", "app.agents", "app.mcp", "app.database", "app.config")

# (가져오는 쪽 접두어, 가져올 수 없는 접두어들)
RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("app.orchestration", SCREENS + ("app.main",)),
    ("app.agents", SCREENS + ("app.main", "app.orchestration")),
    ("app.mcp", SCREENS + ("app.main", "app.orchestration", "app.agents")),
    ("app.database", SCREENS + ("app.main", "app.orchestration", "app.agents", "app.mcp")),
    ("app.config", SCREENS + ("app.main", "app.orchestration", "app.agents", "app.mcp", "app.database")),
    # 전문가 화면(`app.ui.app`, 나중에 `app.ui.expert`)은 체험·쉬운 화면이 가져다 쓰지 않습니다.
    ("app.trial", ("app.easy", "app.ui.app", "app.ui.expert")),
    ("app.easy", ("app.trial", "app.ui.app", "app.ui.expert")),
    ("app.ui", ("app.trial", "app.easy")),
)

# 토론을 돌리는 쪽은 NiceGUI 를 가져오지 않습니다.
NO_NICEGUI = ENGINE_SIDE

# 아직 남은 위반: (가져오는 모듈, 가져오는 모듈). 줄이기만 합니다.
KNOWN_VIOLATIONS: Set[Tuple[str, str]] = {
    ("app.config", "app.mcp"),  # 도구 규칙 문법 (P7)
    ("app.easy.models", "app.trial"),  # P4
    ("app.easy.pages.build", "app.trial"),
    ("app.easy.pages.common", "app.trial"),
    ("app.easy.pages.home", "app.trial"),
    ("app.easy.pages.run", "app.trial"),
    ("app.easy.pages.session", "app.trial"),
    ("app.easy.sessions", "app.trial"),
    ("app.trial.pages.common", "app.easy"),
    ("app.ui.app", "app.easy"),
    ("app.ui.components.roster", "app.easy"),
}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(APP.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.AST, module: str, is_package: bool) -> Iterator[str]:
    """모듈이 가져오는 이름들(절대 경로). `TYPE_CHECKING` 블록은 건너뜁니다."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child):
                stack.extend(child.orelse)
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    yield alias.name
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = module.split(".")
                    if not is_package:
                        base = base[:-1]
                    base = base[: len(base) - (child.level - 1)]
                    target = ".".join(base + ([child.module] if child.module else []))
                else:
                    target = child.module or ""
                yield target
                # `from app.x import y` 의 y 가 서브모듈일 수 있습니다.
                for alias in child.names:
                    yield f"{target}.{alias.name}"
            stack.append(child)


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def collect() -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = {}
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        graph[module] = set(_imports(tree, module, path.name == "__init__.py"))
    return graph


def violations() -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for module, targets in collect().items():
        for source, banned in RULES:
            if not _under(module, source):
                continue
            for target in targets:
                hit = next((b for b in banned if _under(target, b)), None)
                if hit:
                    found.add((module, hit))
        if any(_under(module, p) for p in NO_NICEGUI):
            if any(_under(t, "nicegui") for t in targets):
                found.add((module, "nicegui"))
    return found


def test_no_new_layer_violations():
    new = sorted(violations() - KNOWN_VIOLATIONS)
    assert not new, "의존 방향을 거스르는 import 가 새로 생겼습니다:\n" + "\n".join(
        f"  {a} → {b}" for a, b in new
    )


def test_known_violations_are_still_real():
    """고쳐서 사라진 위반은 목록에서도 지웁니다 — 그래야 목록이 줄어든 것이 보입니다."""
    stale = sorted(KNOWN_VIOLATIONS - violations())
    assert not stale, "이미 사라진 위반이 KNOWN_VIOLATIONS 에 남아 있습니다:\n" + "\n".join(
        f"  {a} → {b}" for a, b in stale
    )
