"""도구 스키마 지연 로딩 — 자주 쓰는 도구만 정의를 싣고, 나머지는 한 줄 목록으로 보여 줍니다 (ADR-039).

MCP 도구의 정의는 요청마다 입력 토큰으로 나갑니다. filesystem·memory·git 만 붙여도 35개에 약 5,300 토큰이고,
한 발언의 도구 루프가 판을 돌 때마다 다시 나갑니다. 스킬(`app/agents/skills.py`)이 설명 한 줄만 보여 주고
본문은 부를 때 주는 것과 같은 방식을 도구 정의에도 씁니다.

* **나누는 기준.** 지침이 이름을 짚는 도구(`tools_named_by_guidance`), `tool_loading.core_tools`, 도구가 적은
  서버(`SMALL_SERVER_TOOLS` 이하)는 정의를 싣습니다. 나머지는 `tools__load_tools` 설명의 목록으로 갑니다.
* **아낄 것이 있을 때만 나눕니다.** 목록으로 아끼는 양이 `tool_loading.min_saving_tokens` 보다 적으면 전부
  싣습니다. 불러오는 한 판은 대화 전체를 다시 보내는 값을 치르므로, 조금 아끼자고 그 위험을 지지 않습니다.
* **불러온 도구는 그 대화의 그 에이전트에게 남습니다** (`remember_loaded`). 발언마다 불러오면 아낀 것보다
  더 쓰게 됩니다. 기억은 프로세스 안에만 있습니다. 재시작으로 잃어도 한 번 더 불러오면 되고, 끊긴 발언은
  초안에서 되살립니다 (`ToolLoop.snapshot`).
* **목록의 도구를 곧바로 불러도 실행합니다.** 호출은 평소처럼 도구 보안을 거치고, 그 뒤로 정의를 싣습니다.
  인자가 틀렸으면 서버가 거절하고, 모델은 다음 판에 정의를 보고 고칩니다.

MCP 서버가 아니라 호스트 도구입니다. 목록이 에이전트와 발언마다 다르고, 실제 효과가 이번 요청의 `tools`
를 바꾸는 것이어서 호스트 밖에서는 할 수 없습니다. 파일·네트워크를 건드리지 않으므로 도구 보안 판정을 받지
않습니다. 늘 거부될 도구는 목록을 만들기 전에 `tool_gate.filter_tools` 가 이미 뺐습니다.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.agents.llm_context import tool_schema_tokens
from app.agents.llm_tools import tools_named_by_guidance
from app.agents.skills import RUN_TOOL_TAIL
from app.config import TOOL_LOADING_PREFIX, get_config

logger = logging.getLogger(__name__)

LOAD_TOOLS_TOOL = f"{TOOL_LOADING_PREFIX}__load_tools"
TOOL_LOADING_GUIDANCE_HEAD = "[도구 불러오기]"

# 이 수 이하의 도구를 가진 서버는 나누지 않습니다. 몇 줄 아끼자고 그 서버의 전부인 도구를 숨길 까닭이 없습니다.
SMALL_SERVER_TOOLS = 3
# 목록 한 줄에 싣는 설명의 상한.
CATALOG_LINE_CHARS = 70
# 프롬프트가 부르라고 시키는 도구 (`build_system_prompt`, sequential thinking 의 mcp 모드).
SEQUENTIAL_THINKING_TOOL = "sequentialthinking"
# 불러온 도구를 기억해 두는 (대화, 에이전트) 수의 상한. 넘으면 오래된 것부터 잊습니다.
MAX_REMEMBERED = 1024

# 목록의 도구를 불러오지 않고 곧바로 불렀다가 실패했을 때 결과 뒤에 붙입니다.
CALLED_BEFORE_LOADING_NOTE = (
    "[도구 불러오기] 이 도구의 정의를 보지 않고 호출했습니다. 이제 정의를 불러왔으니 인자를 확인하고 다시 호출하세요."
)


def _name_of(tool: Dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name") or "")


def _server_of(name: str) -> str:
    return name.split("__", 1)[0] if "__" in name else ""


def _tail_of(name: str) -> str:
    return name.split("__", 1)[-1]


def catalog_line(tool: Dict[str, Any]) -> str:
    """`- git__git_log: Shows the commit logs` — 이름과 설명의 첫 문장만 남깁니다."""
    function = tool.get("function") or {}
    text = " ".join(str(function.get("description") or "").split())
    if text.startswith("[") and "] " in text:
        text = text.split("] ", 1)[1]       # `to_openai_tool` 이 붙인 `[서버]`. 이름에 이미 있습니다.
    for end in (". ", "。"):
        at = text.find(end)
        if at > 0:
            text = text[:at + len(end)].strip().rstrip(".")
            break
    if len(text) > CATALOG_LINE_CHARS:
        text = text[:CATALOG_LINE_CHARS - 1].rstrip() + "…"
    name = _name_of(tool)
    return f"- {name}: {text}" if text else f"- {name}"


def catalog_tool(waiting: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """불러올 수 있는 도구의 목록을 설명에 담은 `tools__load_tools` 정의."""
    catalog = "\n".join(catalog_line(tool) for tool in waiting)
    return {
        "type": "function",
        "function": {
            "name": LOAD_TOOLS_TOOL,
            "description": (
                f"[{TOOL_LOADING_PREFIX}] 아래 도구는 이름과 한 줄 설명만 보입니다. 쓰려면 먼저 이 도구로 "
                f"불러오세요. 쓸 도구를 한 번에 함께 불러오면 다음 판부터 호출할 수 있습니다.\n\n"
                f"불러올 수 있는 도구:\n{catalog}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "불러올 도구의 전체 이름",
                    },
                },
                "required": ["names"],
            },
        },
    }


@dataclass
class ToolShelf:
    """한 발언의 도구: 정의를 싣는 것(`core`, `loaded`, `host`)과 목록에만 있는 것(`waiting`)."""

    core: List[Dict[str, Any]] = field(default_factory=list)
    host: List[Dict[str, Any]] = field(default_factory=list)            # 스킬·자문·질문 — 항상 싣습니다
    waiting: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    loaded: List[Dict[str, Any]] = field(default_factory=list)

    def offered(self) -> List[Dict[str, Any]]:
        """이번 요청에 실을 도구 정의."""
        catalog = [catalog_tool(self.waiting.values())] if self.waiting else []
        return [*self.core, *self.loaded, *self.host, *catalog]

    def loaded_names(self) -> List[str]:
        return [_name_of(tool) for tool in self.loaded]

    def _waiting_names(self, requested: str, whole_server: bool) -> List[str]:
        text = (requested or "").strip()
        if text in self.waiting:
            return [text]
        by_tail = [name for name in self.waiting if _tail_of(name) == text]
        if by_tail or not whole_server:
            return by_tail
        return [name for name in self.waiting if _server_of(name) == text]

    def load(self, names: Iterable[str], *, whole_server: bool = False) -> List[str]:
        """목록의 도구를 정의를 싣는 쪽으로 옮깁니다. 실제로 옮긴 전체 이름 (없는 이름은 건너뜁니다).

        이름은 전체 이름이나 꼬리(`git_log`)로 찾습니다. `whole_server` 면 서버 키(`git`)도 받습니다.
        """
        moved: List[str] = []
        for requested in names:
            for name in self._waiting_names(str(requested), whole_server):
                self.loaded.append(self.waiting.pop(name))
                moved.append(name)
        return moved

    def holds(self, requested: str) -> bool:
        """이미 정의를 싣고 있는 도구인가."""
        text = (requested or "").strip()
        names = [_name_of(tool) for tool in (*self.core, *self.loaded)]
        return any(text in (name, _tail_of(name), _server_of(name)) for name in names)


# ---------------------------------------------------------------------------
# 나누기
# ---------------------------------------------------------------------------


def _core_names(tools: Sequence[Dict[str, Any]], core_tools: Iterable[str]) -> Set[str]:
    wanted = {str(item).strip() for item in core_tools}
    per_server: Dict[str, int] = {}
    for tool in tools:
        server = _server_of(_name_of(tool))
        per_server[server] = per_server.get(server, 0) + 1
    core = set(tools_named_by_guidance(list(tools), (RUN_TOOL_TAIL, SEQUENTIAL_THINKING_TOOL)))
    for tool in tools:
        name = _name_of(tool)
        server = _server_of(name)
        if wanted & {name, _tail_of(name), server} or per_server[server] <= SMALL_SERVER_TOOLS:
            core.add(name)
    return core


def plan_shelf(
    agent: Any,
    mcp_tools: Sequence[Dict[str, Any]],
    host_tools: Sequence[Dict[str, Any]] = (),
    *,
    session_id: Optional[str] = None,
) -> ToolShelf:
    """이 발언의 MCP 도구를 나눕니다. 나눌 이유가 없으면 전부 싣는 진열대를 돌려줍니다.

    여기서 실패해도 발언은 도구 전부를 싣고 계속합니다.
    """
    everything = ToolShelf(core=list(mcp_tools), host=list(host_tools))
    try:
        settings = get_config().tool_loading
        if not settings.enabled or not mcp_tools:
            return everything
        core_names = _core_names(mcp_tools, settings.core_tools)
        core = [tool for tool in mcp_tools if _name_of(tool) in core_names]
        waiting = {_name_of(tool): tool for tool in mcp_tools if _name_of(tool) not in core_names}
        if not waiting:
            return everything
        saving = (
            tool_schema_tokens(agent.model, list(mcp_tools))
            - tool_schema_tokens(agent.model, [*core, catalog_tool(waiting.values())])
        )
        if saving < settings.min_saving_tokens:
            return everything
        shelf = ToolShelf(core=core, host=list(host_tools), waiting=waiting)
        shelf.load(remembered_loaded(session_id, agent.key))
        return shelf
    except Exception as exc:  # noqa: BLE001 - 도구를 나누지 못해도 발언은 막지 않습니다
        logger.warning(
            f"Could not split the tools of {getattr(agent, 'key', '?')}: {type(exc).__name__}: {exc}"
        )
        return everything


# ---------------------------------------------------------------------------
# 대화 안에서 기억하기
# ---------------------------------------------------------------------------

_remembered: "OrderedDict[Tuple[str, str], List[str]]" = OrderedDict()


def remembered_loaded(session_id: Optional[str], agent_key: str) -> List[str]:
    """이 대화에서 이 에이전트가 불러온 도구. 불러온 순서입니다 (요청의 앞부분이 그대로여야 캐시에 남습니다)."""
    if not session_id:
        return []
    return list(_remembered.get((session_id, agent_key)) or ())


def remember_loaded(session_id: Optional[str], agent_key: str, names: Iterable[str]) -> None:
    """불러온 도구를 그 대화의 그 에이전트의 다음 발언에도 싣게 합니다."""
    if not session_id:
        return
    key = (session_id, agent_key)
    known = _remembered.setdefault(key, [])
    known.extend(name for name in names if name not in known)
    _remembered.move_to_end(key)
    while len(_remembered) > MAX_REMEMBERED:
        _remembered.popitem(last=False)


# ---------------------------------------------------------------------------
# 모델에게
# ---------------------------------------------------------------------------


def is_load_tool(tool_name: str) -> bool:
    """도구 불러오기인지. 모델이 접두사를 생략하고 호출한 경우(`load_tools`)도 지원합니다."""
    return (tool_name or "").strip() in (LOAD_TOOLS_TOOL, "load_tools")


def tool_loading_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """목록을 받은 에이전트에게만 붙는 상시 지침. 없으면 None. 목록 자체는 도구 설명에 있습니다."""
    if LOAD_TOOLS_TOOL not in {_name_of(tool) for tool in tools or ()}:
        return None
    return "\n".join([
        TOOL_LOADING_GUIDANCE_HEAD,
        f"- `{LOAD_TOOLS_TOOL}` 설명의 목록에 있는 도구도 쓸 수 있습니다. 필요한 도구가 목록에 있으면 없다고 "
        f"판단하지 말고 불러와 쓰세요.",
        "- 이미 정의가 보이는 도구로 할 수 있는 일이면 불러오지 마세요. 불러오면 한 판을 더 씁니다.",
    ])


def _requested_names(arguments: Any) -> List[str]:
    args = arguments if isinstance(arguments, dict) else {}
    raw = args.get("names", args.get("name"))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [text for text in dict.fromkeys(str(item).strip() for item in raw) if text]


def load_tools_answer(shelf: Optional[ToolShelf], arguments: Any) -> Tuple[str, str, List[str]]:
    """`tools__load_tools` 를 처리합니다: `(도구 결과, 상태, 새로 불러온 이름)`."""
    waiting = list(shelf.waiting) if shelf is not None else []
    available = f"불러올 수 있는 도구: {', '.join(waiting)}" if waiting else "불러올 도구가 남아 있지 않습니다."
    requested = _requested_names(arguments)
    if shelf is None or not requested:
        return f"불러올 도구의 전체 이름(`names`)을 적어 주세요. {available}", "error", []

    loaded: List[str] = []
    already: List[str] = []
    unknown: List[str] = []
    for name in requested:
        moved = shelf.load([name], whole_server=True)
        if moved:
            loaded.extend(moved)
        elif shelf.holds(name):
            already.append(name)
        else:
            unknown.append(name)

    parts: List[str] = []
    if loaded:
        parts.append(f"불러왔습니다: {', '.join(loaded)}. 이제 이 도구들을 호출할 수 있습니다.")
    if already:
        parts.append(f"이미 호출할 수 있습니다: {', '.join(already)}.")
    if unknown:
        remaining = list(shelf.waiting)
        listed = f"불러올 수 있는 도구: {', '.join(remaining)}" if remaining else "불러올 도구가 남아 있지 않습니다."
        parts.append(f"찾지 못한 이름: {', '.join(unknown)}. {listed}")
    return " ".join(parts), ("success" if loaded or already else "error"), loaded
