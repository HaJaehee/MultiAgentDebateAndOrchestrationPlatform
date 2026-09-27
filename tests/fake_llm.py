"""테스트용 LLM 스텁.

예전에는 제품 코드에 내장 시뮬레이터가 있어서 테스트가 그걸 그대로 썼습니다.
그 시뮬레이터는 실제 엔드포인트가 500 을 돌려줄 때도 그럴듯한 페르소나 발언을
지어내 토론 전체를 오염시켰기 때문에 제거했습니다. 테스트 대역은 테스트에 둡니다.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.llm import LLMUnavailableError


ARCHITECT_REPLY = """### 아키텍처 제안

```mermaid
graph TD
    Client[Client] --> API[FastAPI Controller]
    API --> Core[Domain Core Engine]
    Core --> Repo[(Storage Repository)]
```
"""

SYNTHESIS_REPLY = """## 최종 합의 요약

세 전문가의 의견을 통합했습니다.

```mermaid
flowchart LR
    A[요청] --> B[오케스트레이터]
    B --> C[전문가 토론]
    C --> D[합성 산출물]
```

구현은 전문가 발언과 `app/main.py` 에 있습니다.
"""

LEDGER_REPLY = """## 요구사항·제약
- 사용자 요청을 따른다

## 결정 사항
- FastAPI 계층 구조로 간다 (아키텍트 제안, 이견 없음)

## 기각된 대안
- 없음

## 미해결 쟁점
- 없음

## 담당·다음 할 일
- 코더: 구현
"""

SUMMARY_REPLY = "앞선 라운드에서 아키텍트가 FastAPI 계층 구조를 제안했고 코더가 구현을 맡았습니다."


class FakeLLMCaller:
    """`LLMCaller` 와 같은 시그니처로 결정적인 응답을 돌려줍니다."""

    def __init__(
        self,
        *,
        fail_keys: Optional[List[str]] = None,
        replies: Optional[Dict[str, str]] = None,
        tool_calls: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ):
        # 이 키를 가진 에이전트는 엔드포인트가 죽은 것처럼 굴게 합니다.
        self.fail_keys = set(fail_keys or ())
        self.replies = replies or {}
        # 에이전트 키 -> 그 에이전트가 발언할 때마다 실행한 것으로 칠 도구 목록.
        # 실제 루프와 같은 방식으로, 같은 dict 를 콜백과 반환값에 함께 씁니다.
        self.tool_calls = tool_calls or {}
        self.calls: List[str] = []
        # 도구 예산을 다 쓴 척할 에이전트 키. 그 발언에서 `budget_arbiter` 를
        # 실제로 불러, 화면까지 이어지는 통로가 살아 있는지 확인합니다.
        self.exhaust_budget_for: set = set()
        self.budget_arbiters: List[Optional[Callable[[Dict[str, Any]], Any]]] = []
        self.budget_grants: List[int] = []
        # 각 발언이 어떤 대화 스코프로 도구를 부를지 (MCP _meta 로 나가는 값)
        self.scopes: List[Optional[str]] = []
        # 각 발언이 받은 MCP 런타임 (작업 공간별로 다른 객체여야 합니다)
        self.runtimes: List[Any] = []
        self.tool_gates: List[Any] = []
        # 각 호출이 시스템 프롬프트에 실을 결정 장부 (`LLMCaller.build_system_prompt`)
        self.ledgers: List[str] = []

    def _reply_for(self, agent: Agent, messages: List[Dict[str, Any]]) -> str:
        if agent.key in self.replies:
            return self.replies[agent.key]
        last = messages[-1]["content"] if messages else ""
        if "[결정 장부 갱신]" in last:
            return LEDGER_REPLY
        if "[대화 요약 갱신]" in last:
            return SUMMARY_REPLY
        if "최종 합의 보고서" in last:
            return SYNTHESIS_REPLY
        if agent.key == "architect":
            return ARCHITECT_REPLY
        return f"### [{agent.name}] 의견\n\n{agent.role} 관점에서 검토했습니다."

    async def call_agent(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[Callable[[Dict[str, Any]], Any]] = None,
        context_arbiter: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_context_trim: Optional[Callable[[int], Any]] = None,
        mcp: Any = None,
        ledger: str = "",
        tool_gate: Any = None,
        checkpoint: Any = None,
        resume_state: Any = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        self.calls.append(agent.key)
        # 이 발언이 받은 도구 보안 문지기. 턴마다 하나가 모든 발언에 걸리는지 테스트가 읽습니다.
        self.tool_gates.append(tool_gate)
        self.ledgers.append(ledger)
        self.scopes.append(session_id)
        # 이 발언이 어느 MCP 런타임을 받았는지. 대화마다 작업 공간이 다르면
        # 런타임도 달라야 한다는 것을 확인하는 테스트가 읽습니다.
        self.runtimes.append(mcp)
        # 도구 예산이 바닥났을 때 사람에게 물어보는 통로. 진짜 호출기는 상한에
        # 닿았을 때만 씁니다. 대역은 "받았다" 는 사실만 기록하고, 예산 소진을
        # 흉내 내야 하는 테스트가 직접 부릅니다.
        self.budget_arbiters.append(budget_arbiter)
        if self.exhaust_budget_for and agent.key in self.exhaust_budget_for and budget_arbiter:
            self.budget_grants.append(await budget_arbiter({
                "agent_key": agent.key,
                "agent_name": agent.name,
                "limit": agent.max_tool_iterations,
                "used": agent.max_tool_iterations,
                "tool_calls": agent.max_tool_iterations,
            }))
        if agent.key in self.fail_keys:
            raise LLMUnavailableError(agent, "APIConnectionError: 500 Internal Server Error")

        tool_logs: List[Dict[str, Any]] = []
        for spec in self.tool_calls.get(agent.key, []):
            call_log = dict(spec)
            tool_logs.append(call_log)
            if on_tool_call:
                if asyncio.iscoroutinefunction(on_tool_call):
                    await on_tool_call(call_log)
                else:
                    on_tool_call(call_log)

        content = self._reply_for(agent, messages)
        if on_chunk:
            for piece in _in_pieces(content):
                if asyncio.iscoroutinefunction(on_chunk):
                    await on_chunk(piece)
                else:
                    on_chunk(piece)
        return content, tool_logs


def _in_pieces(text: str, size: int = 40) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]
