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
  `app.ui.components` 에, 화면이 아닌 것은 아래층(`app.agents.draft`·`app.agents.catalog`)에 둡니다.

`KNOWN_VIOLATIONS` 는 알고도 남겨 둔 위반입니다 (지금은 없습니다). 줄이기만 하고 늘리지 않습니다.
고쳐서 사라진 항목이 목록에 남아 있으면 그것도 실패로 알립니다.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

APP = Path(__file__).resolve().parents[1] / "app"

SCREENS = ("app.ui", "app.trial", "app.easy")

# `app.ui` 안의 두 갈래. 새 모듈은 반드시 어느 한쪽에 넣습니다 (`test_every_ui_module_is_classified`).
EXPERT_UI = (
    "app.ui.expert", "app.ui.personas_page", "app.ui.graph_page",
    "app.ui.components.roster", "app.ui.components.sidebar", "app.ui.components.artifact_viewer",
    "app.ui.components.workspace_download", "app.ui.components.access_token", "app.ui.components.graph_canvas",
    "app.ui.components.turn_changes", "app.ui.compare_page", "app.ui.components.skill_draft_dialog",
)
SHARED_UI = (
    "app.ui.shared", "app.ui.run_binding", "app.ui.components.chat_feed", "app.ui.components.model_status",
    "app.ui.components.agent_appearance", "app.ui.components.quiet_splitter", "app.ui.math_markdown",
    "app.ui.clipboard", "app.ui.theme", "app.ui.mermaid_export", "app.ui.mention_input",
    "app.ui.diagnostics_script", "app.ui.components.agent_builder_chat",
)
ENGINE_SIDE = ("app.orchestration", "app.agents", "app.mcp", "app.database", "app.config")

# (가져오는 쪽 접두어, 가져올 수 없는 접두어들)
RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("app.orchestration", SCREENS + ("app.main",)),
    ("app.agents", SCREENS + ("app.main", "app.orchestration")),
    ("app.mcp", SCREENS + ("app.main", "app.orchestration", "app.agents")),
    ("app.database", SCREENS + ("app.main", "app.orchestration", "app.agents", "app.mcp")),
    ("app.config", SCREENS + ("app.main", "app.orchestration", "app.agents", "app.mcp", "app.database")),
    # 도구 규칙 문법은 설정과 도구 보안 판정이 함께 기대는 바닥입니다.
    ("app.tool_rules", SCREENS + ("app.main", "app.config", "app.orchestration", "app.agents", "app.mcp",
                                  "app.database")),
    # 엔진은 조립만 합니다. 단계 실행기와 서비스는 엔진을 되부르지 않습니다.
    ("app.orchestration.phases", ("app.orchestration.engine", "app.orchestration.runner")),
    ("app.orchestration.context", ("app.orchestration.engine", "app.orchestration.phases")),
    ("app.orchestration.recording", ("app.orchestration.engine", "app.orchestration.phases",
                                     "app.orchestration.speech", "app.orchestration.conversation")),
    ("app.orchestration.speech", ("app.orchestration.engine", "app.orchestration.phases")),
    ("app.orchestration.conversation", ("app.orchestration.engine", "app.orchestration.phases")),
    ("app.orchestration.turn_setup", ("app.orchestration.engine", "app.orchestration.phases")),
    # 쉬운 화면은 체험 사이트(방문자 로그인·주소·표) 위에서 돕니다. 체험 쪽은 쉬운 화면을 모르고,
    # 쉬운 화면은 체험 화면(`app.trial.pages`)을 가져다 쓰지 않습니다. 둘 다 전문가 화면을 모릅니다.
    ("app.trial", ("app.easy",) + EXPERT_UI),
    ("app.easy", ("app.trial.pages",) + EXPERT_UI),
    # 화면 공용 부품은 어느 화면에도 기대지 않습니다.
    *((shared, ("app.trial", "app.easy") + EXPERT_UI) for shared in SHARED_UI),
    # 전문가 화면은 쉬운 화면도 체험 화면도 모릅니다. 대화로 에이전트 만들기의 재료는 아래층에 있습니다
    # (`app.agents.draft`·`app.agents.catalog`·`app.ui.components.agent_builder_chat`).
    *((expert, ("app.trial", "app.easy")) for expert in EXPERT_UI),
)

# 토론을 돌리는 쪽은 NiceGUI 를 가져오지 않습니다. 로스터의 상태 객체도 화면 없이 돕니다.
NO_NICEGUI = ENGINE_SIDE + ("app.ui.components.roster.state",)

# 큰 화면 부품의 하위 컴포넌트가 가져와도 되는 것. 컴포넌트끼리는 서로를 모릅니다. 잇는 것은
# 조립하는 파일(`control.py`, `feed.py`)이고, 나머지는 생성자로 받는 상태·서비스·콜백입니다.
COMPONENT_SIBLINGS = {
    "app.ui.components.roster": ("control", {"common", "state", "changes", "widgets", "builder_dialog"}),
    "app.ui.components.chat_feed": ("feed", {"common"}),
}

# 아직 남은 위반: (가져오는 모듈, 가져오는 모듈). 줄이기만 합니다.
KNOWN_VIOLATIONS: Set[Tuple[str, str]] = set()


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


def test_every_ui_module_is_classified():
    """`app.ui` 의 모듈은 전문가 화면이거나 화면 공용 부품입니다. 어느 쪽인지 정해야 규칙이 걸립니다."""
    loose = sorted(
        m for m in collect()
        if _under(m, "app.ui") and m not in ("app.ui", "app.ui.components")
        and not any(_under(m, p) for p in EXPERT_UI + SHARED_UI)
    )
    assert not loose, f"EXPERT_UI 나 SHARED_UI 에 넣어 주세요: {loose}"


def test_components_do_not_import_each_other():
    """하위 컴포넌트는 공용 모듈만 가져옵니다. 다른 컴포넌트가 필요하면 생성자로 콜백을 받습니다."""
    found = []
    graph = collect()
    for package, (assembler, allowed) in COMPONENT_SIBLINGS.items():
        siblings = {m.rsplit(".", 1)[1] for m in graph if m.startswith(package + ".")}
        for module, targets in graph.items():
            if not module.startswith(package + ".") or module.endswith("." + assembler):
                continue
            own = module.rsplit(".", 1)[1]
            for target in targets:
                if target.startswith(package + "."):
                    name = target[len(package) + 1:].split(".")[0]
                    if name in siblings and name not in allowed and name != own:
                        found.append(f"{module} → {name}")
    assert not found, "컴포넌트끼리 직접 기대고 있습니다:\n" + "\n".join(sorted(set(found)))


def test_ui_components_are_composed_not_mixed():
    """로스터와 피드의 클래스는 mixin 으로 한 객체를 이루지 않습니다.

    한 객체를 여러 파일로 나누면 파일 하나를 읽을 수도, 시험할 수도 없습니다 — 다른 파일이 세운
    속성에 기대기 때문입니다. 상속은 하나까지만 허용합니다 (`FormBuilderChat(BuilderChat)`).
    """
    found = []
    for directory in (APP / "ui" / "components",):
        for path in directory.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ClassDef) and (node.name.endswith("Mixin") or len(node.bases) > 1):
                    found.append(f"{path.relative_to(APP)}: {node.name}")
    assert not found, "\n".join(found)


def test_known_violations_are_still_real():
    """고쳐서 사라진 위반은 목록에서도 지웁니다 — 그래야 목록이 줄어든 것이 보입니다."""
    stale = sorted(KNOWN_VIOLATIONS - violations())
    assert not stale, "이미 사라진 위반이 KNOWN_VIOLATIONS 에 남아 있습니다:\n" + "\n".join(
        f"  {a} → {b}" for a, b in stale
    )
