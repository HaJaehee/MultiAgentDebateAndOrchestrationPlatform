"""LLM 호출기가 주고받는 모양: 실패 예외와 콜백 형식.

`LLMUnavailableError` — 호출이 실패했다는 사실. 대신 답을 지어내지 않고, 부른 쪽이 실패 발언으로 기록합니다.
"""

from typing import Any, Awaitable, Callable, Dict

from app.agents.base import Agent


class LLMUnavailableError(RuntimeError):
    """LLM 엔드포인트에 닿지 못했을 때 올라옵니다.

    예전에는 여기서 내장 시뮬레이터가 그럴듯한 페르소나 답변을 지어냈습니다.
    엔드포인트가 500 을 돌려준 뒤에도 토론은 멀쩡히 굴러가는 것처럼 보였고,
    그 지어낸 발언이 다음 에이전트의 입력과 최종 합성 보고서까지 오염시켰습니다.
    모르는 것은 모른다고 말하는 편이 낫습니다.
    """

    def __init__(self, agent: Agent, reason: str):
        self.agent_key = agent.key
        self.agent_name = agent.name
        self.model = agent.model
        self.endpoint = agent.endpoint_label
        self.reason = reason.strip() or "원인을 확인할 수 없습니다"
        super().__init__(f"{agent.name} ({agent.model} @ {self.endpoint}): {self.reason}")


class ToolCallLog(dict):
    """Dictionary representing a single tool call and its execution result."""
    pass


# 도구 예산이 바닥났을 때 "얼마나 더 허용할지" 를 정해 주는 콜백.
#
# 0 을 돌려주면 확장 없음 — 에이전트는 도구를 떼고 지금까지 얻은 것으로 결론을
# 씁니다. 이 자리에 사람이 있습니다 (엔진이 화면에 물어보는 통로를 끼웁니다).
BudgetArbiter = Callable[[Dict[str, Any]], Awaitable[int]]


# 컨텍스트 창이 넘쳐 기록을 버려야 할 때 "얼마나 넓혀 줄지" 를 정해 주는 콜백.
#
# 0 을 돌려주면 넓히지 않음 — 오래된 것부터 생략하고 진행하거나(무응답), 도구를
# 떼고 결론만 씁니다(명시적 마무리). 어느 쪽인지는 반환값과 함께 오는 `wrap_up`
# 플래그가 정합니다. 도구 예산 쪽과 달리 답이 세 갈래라 dict 를 돌려받습니다.
ContextArbiter = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


# 발언 초안 저장 (ADR-025). 도구 루프가 한 판을 마칠 때마다 이어 갈 수 있는 상태를 넘깁니다.
# 받는 쪽(`SpeechRunner.speak`)이 DB 에 남기고, 서버가 다시 뜨면 그 상태로 이어 갑니다.
SpeechCheckpoint = Callable[[Dict[str, Any]], Awaitable[None]]
