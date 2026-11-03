"""도구 스키마 지연 로딩 (Lazy Tool Schemas) — 핵심 도구만 우선 주입하고 나머지는 동적 로드 카탈로그로 제공합니다 (ADR-039).

MCP 도구 스키마는 매 API 요청마다 입력 토큰으로 소비됩니다. filesystem·memory·git 서버만 연결해도 약 35개 도구에 5,300 토큰에 달하며,
도구 호출 루프가 반복될 때마다 중복 청구됩니다. 스킬(`app/agents/skills.py`)이 설명 요약만 노출하고 본문은 동적 로드하는 것과 동일한 원리를 도구 스키마에도 적용합니다.

* **분할 기준**: 시스템 지침에서 명시적으로 언급된 도구(`tools_named_by_guidance`), 설정된 핵심 도구(`tool_loading.core_tools`),
  소형 서버(도구 3개 이하)는 즉시 전체 스키마를 주입합니다. 나머지는 `tools__load_tools` 설명 내 카탈로그 목록으로 제공됩니다.
* **절감 효과 기준**: 절감 가능한 토큰 수가 임계치(`tool_loading.min_saving_tokens`) 미만이면 전체 스키마를 즉시 주입합니다.
  도구 로드를 위한 추가 턴 왕복(round-trip) 비용이 발생할 수 있기 때문입니다.
* **세션 캐싱**: 동적으로 로드된 도구는 해당 세션의 에이전트 캐시에 유지됩니다 (`remember_loaded`). 매 턴 반복 로드 오버헤드를 방지합니다.
* **직접 호출 허용**: 모델이 카탈로그에 명시된 대기 도구를 사전 로드 없이 직접 호출하더라도, 보안 검증을 거쳐 정상 실행 후 다음 턴에 상세 스키마를 주입합니다.

본 기능은 외부 MCP가 아닌 인프로세스 호스트 도구로 구현됩니다. 에이전트 및 발언 상태에 따라 동적으로 `tools` 스키마를 조정해야 하기 때문입니다.
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
    """도구 스키마 카탈로그용 단일 라인 요약을 생성합니다 (도구명 및 첫 문장 설명)."""
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
    """동적 로딩 가능한 도구 목록을 설명에 포함하는 `tools__load_tools` 호스트 도구 스키마를 생성합니다."""
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
    """발언에 제공할 도구 모음: 전체 스키마를 주입하는 도구(`core`, `loaded`, `host`)와 지연 로딩 대기 목록(`waiting`)."""

    core: List[Dict[str, Any]] = field(default_factory=list)
    host: List[Dict[str, Any]] = field(default_factory=list)            # 스킬·자문·질문 — 항상 싣습니다
    waiting: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    loaded: List[Dict[str, Any]] = field(default_factory=list)

    def offered(self) -> List[Dict[str, Any]]:
        """현재 요청에 실제 주입할 활성 도구 스키마 목록을 반환합니다."""
        catalog = [catalog_tool(self.waiting.values())] if self.waiting else []
        return [*self.core, *self.loaded, *self.host, *catalog]

    def reachable(self) -> List[Dict[str, Any]]:
        """이번 발언에서 부를 수 있는 도구 전부. 불러오기를 기다리는 도구(`waiting`)도 넣습니다."""
        return [*self.core, *self.loaded, *self.host, *self.waiting.values()]

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
        """대기 카탈로그의 도구를 활성 스키마 목록으로 이동합니다. 성공적으로 로드된 전체 도구명 목록을 반환합니다.

        이름은 전체 이름이나 꼬리(`git_log`)로 찾습니다. `whole_server` 면 서버 키(`git`)도 받습니다.
        """
        moved: List[str] = []
        for requested in names:
            for name in self._waiting_names(str(requested), whole_server):
                self.loaded.append(self.waiting.pop(name))
                moved.append(name)
        return moved

    def holds(self, requested: str) -> bool:
        """해당 도구의 전체 스키마가 이미 주입되어 있는지 확인합니다."""
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
    """현재 발언의 MCP 도구 스키마를 활성 목록과 지연 로딩 목록으로 분할합니다. 분할 필요가 없을 경우 전체 스키마를 유지합니다.

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
    """해당 세션에서 특정 에이전트가 동적으로 로드한 도구 목록을 반환합니다 (캐시 보존 순서)."""
    if not session_id:
        return []
    return list(_remembered.get((session_id, agent_key)) or ())


def remember_loaded(session_id: Optional[str], agent_key: str, names: Iterable[str]) -> None:
    """로드된 도구를 해당 대화 세션의 에이전트 캐시에 등록하여 후속 발언에서도 스키마가 유지되도록 합니다."""
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
    """지연 로딩 목록이 제공된 에이전트에게 주입되는 동적 도구 로딩 관련 상시 시스템 지침."""
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
    """`tools__load_tools` 호출을 처리하여 요청된 도구 스키마들을 동적으로 로드합니다: `(결과 메시지, 상태, 새로 로드된 도구 목록)`."""
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
