"""호스트 도구 레지스트리 — 애플리케이션 내부에서 직접 처리하는 호스트 도구를 단일 파이프라인으로 관리합니다 (ADR-042).

MCP 서버 도구는 외부 독립 프로세스에서 실행되지만, 호스트 도구는 에이전트의 도구 실행 루프 내부에서 직접 구동됩니다.
에이전트 및 발언별로 도구 목록이 동적으로 달라지거나(`skills`), 사용자에게 질문하거나(`ask`), 타 에이전트에게 동료 자문을
요청하거나(`peers`), 현재 요청의 `tools` 스키마를 동적으로 변경(`tools`)하기 위함입니다. LLM 모델에게는 표준 MCP 도구와
동일한 `접두사__도구명` 형식으로 노출됩니다.

도구 제공자 추상 클래스(`HostToolProvider`)가 도구의 주요 4대 요소를 일관되게 제공합니다.

* **식별자** — `prefix`는 도구 이름의 고유 접두사이자 타 MCP 서버와 중복될 수 없는 예약어입니다 (`owns`).
* **스키마 정의** — 현재 발언 차례에 제공할 도구 JSON 스키마 (`enabled_for`, `tools_for`).
* **시스템 지침** — 도구가 할당된 에이전트의 시스템 프롬프트에 주입할 동적 지침 (`guidance`).
* **실행 로직** — 도구 호출 요청을 수신하여 `(결과 문자열, 상태)` 튜플을 반환합니다 (`run`). 도구 실패는 예외 대신
  관측 메모(Observation)로 변환되어 모델이 오류를 확인하고 자가 교정할 수 있도록 유도합니다.

**플러그인 호스트 도구는 기본적으로 도구 보안 정책의 검증을 거칩니다** (`gated=True`). 내장 호스트 도구는 자체 검증 규칙이
있어 게이트 판정을 건너뛰지만, 외부 플러그인 코드는 메인 앱 권한으로 실행되므로 필수 검증 대상입니다. 위험 등급을
선언하지 않은 플러그인은 '미분류 도구'로 취급되어 기본 모드에서는 사용자 승인을 요구하고, 읽기 전용 및 비대면 환경에서는
호출이 거부됩니다. 세부 정책 규칙은 `mcp(접두사/도구명)` 형식으로 설정합니다.

**플러그인은 `allowed_mcp_servers`를 통해 활성화됩니다** (`peers`와 동일). 따라서 체험 세션이나 자문, 최종 합성 등
도구가 비활성화된 단계에서는 자동으로 제외되며, 에이전트 도구 설정 패널에 표시됩니다. 권한이 없는 에이전트가 호출을
시도하더라도 실행이 안전하게 거부됩니다 (`run_host_tool`).

신규 도구는 본 소스코드를 수정하지 않고 `host_tools/` 디렉터리 또는 패키지 엔트리포인트를 통해 플러그인으로 확장합니다 (`app/plugins.py`).
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
    """단일 호스트 도구 호출 요청 컨텍스트. 제공자는 필요한 필드만 선별하여 참조합니다."""

    agent: Any
    name: str                                       # 모델이 호출한 전체 도구 식별자
    arguments: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    workspace: Optional[Path] = None                # 현재 세션의 작업 공간 디렉터리
    tools: Optional[List[Dict[str, Any]]] = None    # 현재 턴에 실제로 주입된 도구 스키마 목록
    llm_caller: Optional[Any] = None
    loop: Optional[Any] = None                      # 활성 도구 루프 인스턴스 (`tools__load_tools` 등 동적 스키마 변경 시 참조)


class HostToolProvider(ABC):
    """호스트 도구 제공자 기본 추상 클래스.

    플러그인 구현체는 `prefix`, `tails`, `tools_for`, `run`을 필수로 구현해야 합니다. `tools_for`는 매 발언마다
    호출되므로 가벼워야 하며, 제공자 인스턴스는 모든 대화 세션이 공유하므로 세션 가변 상태를 속성에 저장하지 마십시오.
    """

    prefix: str = ""
    # 접두사 뒤에 붙는 세부 도구 식별자 튜플.
    tails: Collection[str] = ()
    # 예약어 충돌 안내 시 사용되는 표시 명칭 (예: "사내 용어집 도구"). 미지정 시 접두사로 대체됩니다.
    label: str = ""
    # 에이전트 도구 설정 패널에 표시되는 요약 설명.
    summary: str = ""
    # 도구 보안 정책 검증 대상 여부. False 설정 시 정책 검증을 생략합니다.
    gated: bool = True
    # 도구 보안 판정용 위험 등급 (`app.mcp.policy`의 read, state, write, net 등). 미지정 시 UNKNOWN 처리.
    risk: str = UNKNOWN
    # 에이전트의 `allowed_mcp_servers`에 접두사를 등록하여 활성화 가능한 도구인지 여부.
    assignable: bool = True

    def owns(self, tool_name: str) -> bool:
        """본 제공자가 지원하는 도구 식별자인지 검사합니다. 플러그인은 이름 충돌 방지를 위해 접두사가 포함된 전체 이름만 허용합니다."""
        head, _, tail = (tool_name or "").strip().partition("__")
        return head == self.prefix and tail in self.tails

    def enabled_for(self, agent: Any) -> bool:
        """해당 에이전트에게 본 도구 권한이 부여되었는지 확인합니다."""
        return self.prefix in (getattr(agent, "allowed_mcp_servers", None) or ())

    def risk_of(self, tool_name: str) -> str:
        """특정 도구의 위험 등급을 반환합니다. 도구별로 등급이 상이할 경우 오버라이드합니다."""
        return self.risk

    def definition(
        self, tail: str, description: str, parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """OpenAI 규격의 도구 JSON 스키마를 생성합니다."""
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
        """현재 발언 차례에 제공할 도구 스키마 목록을 반환합니다. 대상 도구가 없으면 빈 리스트를 반환합니다."""

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """본 도구를 보유한 에이전트의 시스템 프롬프트에 동적 주입할 지침을 반환합니다. 도구가 미포함된 경우 None을 반환해야 합니다."""
        return None

    @abstractmethod
    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        """단일 도구 호출을 실행하고 `(결과 문자열, 상태)` 튜플을 반환합니다. 상태는 `success` 또는 `error`입니다."""


# ---------------------------------------------------------------------------
# 내장 도구
# ---------------------------------------------------------------------------


class _BuiltinTools(HostToolProvider):
    """내장 호스트 도구 제공자 기본 클래스. 하위 모듈이 권한을 판별하며 접두사가 생략된 원본 호출도 허용합니다."""

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
    """호스트 도구 제공자를 레지스트리에 등록합니다. 유효하지 않은 경우 ValueError가 발생하며 UI 및 로그에 출력됩니다."""
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
    """등록된 제공자를 레지스트리에서 등록 해제합니다. 주로 단위 테스트 사후 정리에 사용됩니다."""
    if _providers.pop(prefix, None) is not None:
        RESERVED_SERVER_NAMES.pop(prefix, None)


def host_tool_providers() -> List[HostToolProvider]:
    """등록된 모든 호스트 도구 제공자 목록을 등록 순서대로 반환합니다."""
    return list(_providers.values())


def assignable_plugin_tools() -> List[HostToolProvider]:
    """에이전트 도구 설정 패널에 표시할 플러그인 목록을 반환합니다 (peers는 전용 UI가 제공되므로 제외)."""
    return [p for p in _providers.values() if p.assignable and p.prefix != PEERS_TOOL_PREFIX]


def host_tool_switches() -> FrozenSet[str]:
    """`allowed_mcp_servers` 목록에 포함될 수 있으나 외부 MCP 서버가 아닌 호스트 도구 예약어 목록."""
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
    """지정된 도구 이름을 처리할 전용 호스트 도구 제공자를 조회합니다. 일반 MCP 도구인 경우 None을 반환합니다."""
    return next(_owners(tool_name), None)


def host_tools_for(agent: Any) -> List[Dict[str, Any]]:
    """현재 발언 차례에 주입할 호스트 도구 스키마 목록을 취합합니다. 특정 제공자에서 예외가 발생하더라도
    나머지 정상 도구들로 발언을 계속 진행합니다. 제공자가 등록되지 않은 타 도구 스키마를 반환한 경우
    호출 라우팅 불일치를 방지하기 위해 해당 항목을 필터링합니다.
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
    """현재 발언 차례의 도구 구성에 부합하는 호스트 도구 시스템 지침들을 취합하여 반환합니다."""
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
    """도구 보안 정책 검증에 사용할 메타데이터를 반환합니다. 검증 대상이 아닌 호스트 도구는 None을 반환합니다.

    보안 신뢰 플래그는 기본적으로 비활성화합니다 (`trusted=False`). 플러그인 도구 이름이 `read_file`과 같더라도
    내장 도구 특권을 얻지 못하도록 차단합니다. 위험 등급은 플러그인이 선언한 값을 사용합니다.
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
    """단일 호스트 도구를 실행합니다. 런타임 예외는 호출자(`_execute_tool_safely`)에서 관측 메모(Observation)로 변환됩니다."""
    if not provider.enabled_for(call.agent):
        return f"'{call.name}' 은 이 에이전트에게 할당되지 않은 도구입니다. 다른 방법으로 진행하세요.", "error"
    output, status = await provider.run(call)
    text = output if isinstance(output, str) else str(output)
    # 내장 도구는 스스로 줄입니다. 줄인 글을 또 줄이면 몇 자를 생략했다는 안내가 틀어집니다.
    return (text if isinstance(provider, _BuiltinTools) else clip_tool_output(text)), status
