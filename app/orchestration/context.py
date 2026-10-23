"""엔진과 단계 실행기·서비스가 함께 쓰는 것.

`OrchestratorEngine` 은 이것을 하나 만들어 기록(`recording`), 발언(`speech`), 대화 기억
(`conversation`), 턴 준비(`turn_setup`), 단계 실행기(`phases/`)에 나눠 줍니다. 서로를 되부르지
않고 이 묶음만 봅니다.

`llm_caller` 는 실행 중에 갈아 끼울 수 있습니다 — 밖에서 MADO 를 구동하는 쪽(MADOWarGame)이
엔진을 만든 뒤 대역 호출기로 바꿉니다. 그래서 서비스는 호출기를 붙잡아 두지 않고 매번 여기서 읽습니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool
from app.orchestration.state import DebateState
from app.orchestration.tool_gate import ToolGate

if TYPE_CHECKING:
    from app.agents.llm import LLMCaller
    from app.agents.pool import AgentPool


@dataclass
class EngineContext:
    agent_pool: "AgentPool"
    llm_caller: "LLMCaller"
    session_factory: Callable[[], Any]
    # 진행 중인 턴의 도구 보안 문지기 (세션 id → 게이트). 턴이 열 때 만들고 닫을 때
    # 치웁니다 (`run_turn`). 발언뿐 아니라 지명·계획·요약 같은 보조 호출도 에이전트의
    # 도구를 들고 나가므로, 모든 `call_agent` 가 같은 게이트를 거칩니다.
    tool_gates: Dict[str, ToolGate] = field(default_factory=dict)

    def gate_for(self, state: DebateState) -> Optional[ToolGate]:
        """이 턴의 도구 보안 문지기. 턴 밖에서 부른 발언(테스트)이면 None."""
        return self.tool_gates.get(state.session_id)

    @staticmethod
    def mcp_for(state: DebateState) -> Optional[MCPManager]:
        """이 턴이 빌린 MCP 런타임.

        예전에는 전역 매니저 하나를 이 턴의 작업 공간으로 **다시 띄웠습니다**.
        그래서 폴더가 다른 토론이 동시에 돌 수 없었습니다. 지금은 턴이 시작할 때
        그 폴더의 런타임을 빌리고(`run_turn`), 발언마다 그것을 내려보냅니다.

        None 을 돌려주면 `LLMCaller` 가 자기 기본 런타임으로 물러섭니다 — 풀을
        거치지 않고 엔진을 직접 부르는 테스트가 그 경로를 씁니다.
        """
        return get_runtime_pool().get(state.workspace_dir)
