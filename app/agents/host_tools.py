"""호스트 도구 레지스트리 — 앱이 직접 처리하는 도구를 한 파이프라인으로 다룹니다 (ADR-042).

MCP 서버의 도구는 다른 프로세스에서 돌지만, 호스트 도구는 도구 루프 안에서 돕니다. 목록이 에이전트와 발언마다
달라지거나(`skills`), 사람에게 묻거나(`ask`), 다른 에이전트를 부르거나(`peers`), 이번 요청의 `tools` 를
바꾸기(`tools`) 때문입니다. 모델에게는 MCP 도구와 똑같은 `접두사__이름` 입니다.

제공자 하나(`HostToolProvider`)가 도구 하나의 네 자리를 함께 답합니다.

* **이름** — `prefix` 는 도구 이름의 접두사이자 MCP 서버로 쓸 수 없는 예약어입니다 (`owns`).
* **정의** — 이번 발언에 줄 도구 (`enabled_for`, `tools_for`).
* **지침** — 도구를 가진 에이전트의 시스템 프롬프트에 붙는 글 (`guidance`).
* **실행** — 호출 하나를 받아 `(결과, 상태)` 를 돌려줍니다 (`run`). 실패는 관측입니다. 터져도 발언은 계속됩니다.

**플러그인은 기본으로 도구 보안 판정을 받습니다** (`gated`). 내장 도구는 파일과 네트워크에 닿지 않거나 자기
규칙으로 통제되어 판정을 건너뛰지만, 외부 코드가 무엇을 하는지 앱은 모릅니다. 그래서 등급을 밝히지 않은 플러그인은
'등급을 모르는 도구' 입니다. 기본 모드에서는 묻고, 읽기 전용에서는 거부되며, 물을 사람이 없으면 거부됩니다.
규칙은 `mcp(접두사/이름)` 으로 겁니다.

**플러그인은 `allowed_mcp_servers` 로 켭니다** (`peers` 와 같습니다). 그래서 체험·자문·합성처럼 도구를 비운
자리에서는 저절로 빠지고, 도구 할당 창에 한 줄로 나옵니다. 받지 않은 에이전트가 이름을 짐작해 불러도 실행하지
않습니다 (`run_host_tool`).

새 도구는 이 파일을 고치지 않고 `host_tools/` 폴더나 entry point 로 더합니다 (`app/plugins.py`).
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, FrozenSet, Iterator, List, Optional, Tuple

from app.agents.ask_user import ask_guidance, ask_tools_for, is_ask_tool, run_ask_tool
from app.agents.consultation import (
    consultation_guidance,
    consultation_tools_for,
    is_consultation_tool,
    run_consultation_tool,
)
from app.agents.skills import is_skill_tool, run_skill_tool, skill_guidance, skill_tools_for
from app.agents.tool_loading import is_load_tool, tool_loading_guidance
from app.config import (
    ASK_TOOL_PREFIX,
    PEERS_TOOL_PREFIX,
    RESERVED_SERVER_NAMES,
    SKILL_TOOL_PREFIX,
    TOOL_LOADING_PREFIX,
    get_config,
)
from app.mcp.client import clip_tool_output
from app.mcp.policy import RISK_LABELS, UNKNOWN, ToolMeta

logger = logging.getLogger(__name__)

# 도구 이름의 접두사. `__` 는 접두사와 이름을 가르는 표시라 안에 들어갈 수 없습니다.
PREFIX_PATTERN = re.compile(r"[a-z][a-z0-9-]*(?:_[a-z0-9-]+)*")
TAIL_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class HostToolCall:
    """호스트 도구 호출 하나. 제공자는 필요한 것만 읽습니다."""

    agent: Any
    name: str                                       # 모델이 부른 이름 그대로
    arguments: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    workspace: Optional[Path] = None                # 이번 발언의 작업 공간
    tools: Optional[List[Dict[str, Any]]] = None    # 이번 발언에 실제로 실린 도구 정의
    llm_caller: Optional[Any] = None
    loop: Optional[Any] = None                      # 도구 루프. `tools__load_tools` 처럼 요청을 바꾸는 도구만 씁니다


class HostToolProvider(ABC):
    """호스트 도구 한 묶음.

    플러그인은 `prefix`·`tails`·`tools_for`·`run` 만 채우면 됩니다. `tools_for` 는 발언마다 불리므로
    가벼워야 하고, 객체는 대화 전체가 함께 쓰므로 대화별 상태를 속성에 두면 안 됩니다.
    """

    prefix: str = ""
    # 접두사 뒤의 도구 이름들. 이 밖의 이름은 이 제공자의 도구가 아닙니다.
    tails: Collection[str] = ()
    # 예약어 안내에 쓰는 이름 (예: "사내 용어집 도구"). 비어 있으면 접두사로 짓습니다.
    label: str = ""
    # 도구 할당 창에 보이는 한 줄 설명.
    summary: str = ""
    # 도구 보안 판정을 받는가. 받지 않으려면 판정 없이 실행해도 안전한 이유가 있어야 합니다.
    gated: bool = True
    # 판정에 쓰는 등급 (`app.mcp.policy` 의 read · state · write · net …). 모르면 UNKNOWN 그대로 둡니다.
    risk: str = UNKNOWN
    # 에이전트의 `allowed_mcp_servers` 에 접두사를 넣어 켜는 도구인가.
    assignable: bool = True

    def owns(self, tool_name: str) -> bool:
        """이 제공자의 도구 이름인가. 플러그인은 전체 이름만 받습니다 (맨이름이 MCP 도구를 가로채지 않게)."""
        head, _, tail = (tool_name or "").strip().partition("__")
        return head == self.prefix and tail in self.tails

    def enabled_for(self, agent: Any) -> bool:
        """이 에이전트에게 주는 도구인가."""
        return self.prefix in (getattr(agent, "allowed_mcp_servers", None) or ())

    def risk_of(self, tool_name: str) -> str:
        """이 도구의 등급. 도구마다 다르면 고쳐 씁니다."""
        return self.risk

    def definition(
        self, tail: str, description: str, parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """OpenAI 형식의 도구 정의를 짓습니다."""
        return {
            "type": "function",
            "function": {
                "name": f"{self.prefix}__{tail}",
                "description": f"[{self.prefix}] {description}",
                "parameters": parameters or {"type": "object", "properties": {}},
            },
        }

    @abstractmethod
    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        """이번 발언에 줄 도구 정의. 줄 것이 없으면 빈 목록."""

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """이 도구를 가진 에이전트의 시스템 프롬프트에 붙일 글. `tools` 에 이 도구가 없으면 None 이어야 합니다."""
        return None

    @abstractmethod
    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        """호출 하나를 실행하고 `(결과, 상태)` 를 돌려줍니다. 상태는 `success` · `error` 입니다."""


# ---------------------------------------------------------------------------
# 내장 도구
# ---------------------------------------------------------------------------


class _BuiltinTools(HostToolProvider):
    """앱에 든 도구. 주고 말고는 각 모듈이 정하고, 모델이 접두사를 빼고 불러도 받습니다."""

    gated = False
    assignable = False

    def enabled_for(self, agent: Any) -> bool:
        return True


class _SkillTools(_BuiltinTools):
    prefix = SKILL_TOOL_PREFIX

    def owns(self, tool_name: str) -> bool:
        return is_skill_tool(tool_name)

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return skill_tools_for(agent)

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        return skill_guidance(tools)

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        return await run_skill_tool(
            call.agent, call.name, call.arguments, workspace=call.workspace, tools=call.tools,
        )


class _PeerTools(_BuiltinTools):
    prefix = PEERS_TOOL_PREFIX
    assignable = True

    def owns(self, tool_name: str) -> bool:
        return is_consultation_tool(tool_name)

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return consultation_tools_for(agent)

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        return consultation_guidance(tools)

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        return await run_consultation_tool(call.agent, call.name, call.arguments, llm_caller=call.llm_caller)


class _AskTools(_BuiltinTools):
    prefix = ASK_TOOL_PREFIX

    def owns(self, tool_name: str) -> bool:
        return is_ask_tool(tool_name)

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return ask_tools_for(agent)

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        return ask_guidance(tools)

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        return await run_ask_tool(call.agent, call.arguments)


class _ToolLoadingTools(_BuiltinTools):
    """`tools__load_tools`. 정의는 진열대가 목록을 담아 직접 싣습니다 (`ToolShelf.offered`)."""

    prefix = TOOL_LOADING_PREFIX

    def owns(self, tool_name: str) -> bool:
        return is_load_tool(tool_name)

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return []

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        return tool_loading_guidance(tools)

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        if call.loop is None:
            return "지금은 도구를 불러올 수 없습니다. 이미 정의가 보이는 도구로 진행하세요.", "error"
        return call.loop.answer_load_tools(call.arguments)


# ---------------------------------------------------------------------------
# 레지스트리
# ---------------------------------------------------------------------------

# 등록 순서가 곧 도구 정의와 지침이 실리는 순서입니다. 요청의 앞부분이 그대로여야 프롬프트 캐시에 남습니다.
_providers: Dict[str, HostToolProvider] = {
    provider.prefix: provider
    for provider in (_SkillTools(), _PeerTools(), _AskTools(), _ToolLoadingTools())
}


def register_host_tool(provider: HostToolProvider) -> None:
    """제공자를 더합니다. 쓸 수 없으면 ValueError 이고, 그 글이 화면과 로그에 그대로 나갑니다."""
    if not isinstance(provider, HostToolProvider):
        raise ValueError(f"HostToolProvider 가 아닙니다: {type(provider).__name__}")
    prefix = str(provider.prefix or "")
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError(
            f"접두사 '{prefix}' 를 쓸 수 없습니다. 영문 소문자로 시작하고 소문자·숫자·`-`·`_` 만 쓰며, `__` 를 넣을 수 없습니다."
        )
    tails = list(provider.tails or ())
    bad = [tail for tail in tails if not (isinstance(tail, str) and TAIL_PATTERN.fullmatch(tail))]
    if not tails or bad:
        raise ValueError(f"'{prefix}' 의 도구 이름(tails)이 없거나 잘못되었습니다: {bad or '비어 있음'}")
    if prefix in RESERVED_SERVER_NAMES:
        raise ValueError(f"접두사 '{prefix}' 는 이미 {RESERVED_SERVER_NAMES[prefix]}으로 쓰입니다.")
    if prefix in get_config().mcp_servers:
        raise ValueError(
            f"접두사 '{prefix}' 는 conf.json 의 MCP 서버 이름과 겹칩니다. 켜는 스위치와 도구 이름이 함께 겹치므로 둘 중 하나를 바꾸세요."
        )
    _providers[prefix] = provider
    RESERVED_SERVER_NAMES[prefix] = provider.label or f"플러그인 도구 이름({prefix}__)"


def unregister_host_tool(prefix: str) -> None:
    """제공자를 뺍니다. 시험이 뒷정리에 씁니다 (실행 중인 앱은 플러그인을 내리지 않습니다)."""
    if _providers.pop(prefix, None) is not None:
        RESERVED_SERVER_NAMES.pop(prefix, None)


def host_tool_providers() -> List[HostToolProvider]:
    """등록된 제공자. 등록 순서입니다."""
    return list(_providers.values())


def assignable_plugin_tools() -> List[HostToolProvider]:
    """도구 할당 창에 한 줄씩 더할 플러그인. peers 는 자문 상대 고르기가 붙어 화면이 따로 그립니다."""
    return [p for p in _providers.values() if p.assignable and p.prefix != PEERS_TOOL_PREFIX]


def host_tool_switches() -> FrozenSet[str]:
    """`allowed_mcp_servers` 에 적혀도 MCP 서버가 아닌 이름."""
    return frozenset(p.prefix for p in _providers.values() if p.assignable)


def _tool_name(tool: Dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name") or "")


def _owners(tool_name: str) -> Iterator[HostToolProvider]:
    for provider in _providers.values():
        try:
            if provider.owns(tool_name):
                yield provider
        except Exception as exc:  # noqa: BLE001 - 플러그인 하나가 모든 도구 호출을 막으면 안 됩니다
            logger.warning(f"Host tool provider '{provider.prefix}' failed on owns(): {type(exc).__name__}: {exc}")


def find_host_tool(tool_name: str) -> Optional[HostToolProvider]:
    """이 이름을 처리할 제공자. MCP 도구면 None."""
    return next(_owners(tool_name), None)


def host_tools_for(agent: Any) -> List[Dict[str, Any]]:
    """이 발언에 실을 호스트 도구 정의. 제공자 하나가 실패해도 나머지로 발언을 이어 갑니다.

    제공자가 자기 것이 아닌 이름의 도구를 내놓으면 버립니다. 실어 주면 그 호출이 MCP 서버로 가거나
    다른 제공자에게 가서, 정의를 지은 쪽과 실행하는 쪽이 갈라집니다.
    """
    tools: List[Dict[str, Any]] = []
    for provider in _providers.values():
        try:
            if not provider.enabled_for(agent):
                continue
            for tool in provider.tools_for(agent) or ():
                if provider.owns(_tool_name(tool)):
                    tools.append(tool)
                else:
                    logger.warning(f"Host tool provider '{provider.prefix}' offered a foreign tool: {_tool_name(tool)!r}")
        except Exception as exc:  # noqa: BLE001 - 도구 하나를 못 줘도 발언은 막지 않습니다
            logger.warning(
                f"Host tool provider '{provider.prefix}' could not list tools for "
                f"{getattr(agent, 'key', '?')}: {type(exc).__name__}: {exc}"
            )
    return tools


def host_tool_guidance(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    """이 발언의 도구에 딸린 상시 지침. 도구를 가진 제공자만 글을 돌려줍니다."""
    parts: List[str] = []
    for provider in _providers.values():
        try:
            text = provider.guidance(tools)
        except Exception as exc:  # noqa: BLE001 - 지침 한 줄 때문에 발언을 막지 않습니다
            logger.warning(f"Host tool provider '{provider.prefix}' failed on guidance(): {type(exc).__name__}: {exc}")
            continue
        if text:
            parts.append(str(text))
    return parts


def host_tool_meta(tool_name: str) -> Optional[ToolMeta]:
    """도구 보안 판정에 쓸 정보. 판정을 받는 호스트 도구가 아니면 None.

    믿는 서버로 두지 않습니다 (`trusted=False`). 플러그인 도구 이름이 `read_file` 이어도 알려진 도구로
    풀리면 안 됩니다. 등급은 플러그인이 밝힌 값을 씁니다. 앱과 같은 권한으로 도는 코드라 이 값을 믿는다고
    더 내주는 것이 없습니다.
    """
    provider = find_host_tool(tool_name)
    if provider is None or not provider.gated:
        return None
    try:
        risk = provider.risk_of(tool_name)
    except Exception:  # noqa: BLE001 - 등급을 모르면 모르는 도구입니다
        risk = UNKNOWN
    return ToolMeta(
        server=provider.prefix,
        tool=tool_name.split("__", 1)[-1],
        trusted=False,
        declared_risk=risk if risk in RISK_LABELS else UNKNOWN,
    )


async def run_host_tool(provider: HostToolProvider, call: HostToolCall) -> Tuple[str, str]:
    """호스트 도구 하나를 실행합니다. 예외는 부르는 쪽(`_execute_tool_safely`)이 관측으로 바꿉니다."""
    if not provider.enabled_for(call.agent):
        return f"'{call.name}' 은 이 에이전트에게 할당되지 않은 도구입니다. 다른 방법으로 진행하세요.", "error"
    output, status = await provider.run(call)
    text = output if isinstance(output, str) else str(output)
    # 내장 도구는 스스로 줄입니다. 줄인 글을 또 줄이면 몇 자를 생략했다는 안내가 틀어집니다.
    return (text if isinstance(provider, _BuiltinTools) else clip_tool_output(text)), status
