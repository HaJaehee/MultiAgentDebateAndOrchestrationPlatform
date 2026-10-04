"""토론 전략 — 한 라운드에서 누가, 어떤 순서로, 어떤 지침을 받고 발언하는가.

예전에는 순서가 `{"architect": 0, "coder": 1, "critic": 2}` 처럼 에이전트 키를
문자열로 박아 정해졌습니다. 화면에서 에이전트를 만들 수 있게 되면서 그 방식은
무너졌습니다 — 표에 없는 키는 전부 우선순위 10 으로 묶여 언제나 맨 뒤로 밀렸고,
디베이트에서는 제안자도 비판자도 아닌 `others` 로 빠졌습니다.

이제 순서와 진영은 에이전트 자신이 들고 다닙니다 (`debate_priority`,
`debate_stance`). 전략은 그 값을 읽어 배치만 합니다. 여기에 에이전트 키가
나타나는 곳은 오케스트레이터를 라운드에서 빼는 한 줄뿐입니다 — 그것은 역할이
아니라 이 시스템의 구조입니다 (계획과 합성을 맡고 라운드 밖에 섭니다).

전략은 순서만 정하지 않습니다. `turn_instruction()` 이 발언 차례마다 붙는 지침을
돌려주고, 그것이 전략들의 실제 차이입니다.

한때 '자유 토론' 과 '순차 검증' 이 따로 있었습니다. 순서가 키 하드코딩으로 정해지던
시절에는 둘의 발언 순서가 달랐지만, 순서가 `debate_priority` 하나로 정리되면서 두
전략은 같은 순서로 같은 사람들을 부르게 되었습니다. 이름과 달리 '자유 토론' 도 결국
정해진 순서대로 도는 것이었습니다. 그래서 하나로 합치고, 하는 일 그대로 '순차 토론'
이라 부릅니다. 예전 이름으로 저장된 대화는 `LEGACY_STRATEGY_ALIASES` 가 받습니다.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from app.agents.base import Agent
from app.orchestration.state import DebateState

# 오케스트레이터는 라운드 밖에 섭니다. 계획(round 0)과 최종 합성이 그 자리입니다.
ORCHESTRATOR_KEY = "orchestrator"


def order_by_priority(agents: List[Agent]) -> List[Agent]:
    """`debate_priority` 오름차순. 같은 값이면 원래 순서(= conf.json 순서)를 지킵니다.

    파이썬 `sorted` 는 안정 정렬이라, 아무도 순서를 지정하지 않은 설정에서는
    (전원이 기본값이므로) 파일에 적힌 순서가 그대로 나옵니다.
    """
    return sorted(agents, key=lambda a: a.debate_priority)


def specialists_of(active_agents: List[Agent]) -> List[Agent]:
    """오케스트레이터를 뺀 발언자 후보."""
    return [a for a in active_agents if a.key != ORCHESTRATOR_KEY]


class BaseDebateStrategy(ABC):
    # 오케스트레이터가 매 라운드 발언자를 고르는 전략인지. True 면 엔진이 LLM 에게
    # 물어보고, 실패하면 이 전략의 `get_speakers_for_round()` 로 물러섭니다.
    orchestrator_selects_speakers: bool = False

    # 한 라운드의 발언자들이 **동시에** 도는 전략인지. True 면 엔진이 라운드마다
    # 태스크를 나눠 주고(dispatch), 지목된 에이전트를 함께 띄운 뒤(gather),
    # 오케스트레이터가 결과를 취합합니다. False 면 한 명씩 순서대로 돕니다.
    orchestrator_dispatches_parallel: bool = False

    # 순서 대신 사람이 그린 그래프를 따라 도는 전략인지. True 면 엔진이 라운드 루프 대신
    # 그래프 스케줄러로 돕니다 (`OrchestratorEngine._run_graph`, `app/orchestration/graph.py`).
    runs_graph: bool = False

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        pass

    @abstractmethod
    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState
    ) -> List[Agent]:
        """Returns the list of specialist agents who should speak in the given round."""
        pass

    def turn_instruction(
        self,
        agent: Agent,
        speakers: List[Agent],
        index: int,
        state: DebateState,
    ) -> str:
        """이 발언 차례에 덧붙일 지침. 빈 문자열이면 아무것도 붙이지 않습니다.

        `index` 는 이번 라운드에서 이 에이전트가 몇 번째 발언자인지입니다.
        """
        return ""


class SequentialDebateStrategy(BaseDebateStrategy):
    """순차 토론: `debate_priority` 순으로 앞사람의 결론을 이어받아 진행합니다.

    한 라운드에 활성 전문가 전원이 한 번씩, 정해진 순서로 발언합니다. 각자는 직전
    발언을 **입력으로** 받아 그 위에 쌓거나 그것을 검증합니다. 이 인수인계 지침이
    없으면 라운드가 그냥 독백 세 개가 됩니다 — 같은 맥락을 보고 각자 따로 말하고,
    아무도 앞사람의 결론을 책임지지 않습니다.
    """

    name = "sequential_debate"
    display_name = "순차 토론 (Sequential Debate)"

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState
    ) -> List[Agent]:
        specialists = order_by_priority(specialists_of(active_agents))
        return specialists if specialists else active_agents

    def turn_instruction(
        self, agent: Agent, speakers: List[Agent], index: int, state: DebateState
    ) -> str:
        if index == 0:
            return (
                "[순차 토론] 당신이 이번 라운드의 첫 순서입니다. 뒤 순서가 이어받아 검증할 수 "
                "있도록, 결론과 그 근거·가정을 명확히 구분해 제시하세요."
            )
        previous = speakers[index - 1]
        instruction = (
            f"[순차 토론] 바로 앞 순서인 {previous.name}({previous.role})의 발언을 **입력으로** "
            f"받아 이어가세요. 새 주제를 여는 대신, 그 결론이 옳은지 당신의 전문 영역에서 "
            f"검증하고 필요한 부분을 보강하거나 반증하세요."
        )
        if index == len(speakers) - 1:
            instruction += " 당신이 이번 라운드의 마지막 순서이므로, 라운드의 결론을 정리해 남기세요."
        return instruction


class AdversarialDebateStrategy(BaseDebateStrategy):
    """디베이트: 제안자와 비판자가 번갈아 발언합니다.

    진영은 `debate_stance` 가 정합니다 (`proponent` / `critic` / `neutral`).
    각 진영 안에서는 `debate_priority` 순입니다. 어느 쪽도 아닌 에이전트는 대립이
    한 바퀴 돈 뒤에 발언합니다.

    양쪽 중 한쪽이 비어 있으면 대립 구도가 성립하지 않으므로, 그냥 우선순위 순의
    한 줄로 돌려줍니다. 진영을 지정하지 않은 설정에서 아무도 발언하지 못하는 일이
    없어야 합니다.
    """

    name = "adversarial_debate"
    display_name = "디베이트 (Debate & Critique)"

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState
    ) -> List[Agent]:
        specialists = order_by_priority(specialists_of(active_agents))
        if not specialists:
            return active_agents

        proponents = [a for a in specialists if a.debate_stance == "proponent"]
        critics = [a for a in specialists if a.debate_stance == "critic"]
        others = [a for a in specialists if a.debate_stance == "neutral"]

        if not proponents or not critics:
            # 진영이 한쪽뿐이면 번갈아 세울 것이 없습니다.
            return specialists

        speakers: List[Agent] = []
        for i in range(max(len(proponents), len(critics))):
            if i < len(proponents):
                speakers.append(proponents[i])
            if i < len(critics):
                speakers.append(critics[i])
        speakers.extend(others)
        return speakers

    def turn_instruction(
        self, agent: Agent, speakers: List[Agent], index: int, state: DebateState
    ) -> str:
        if agent.debate_stance == "proponent":
            return (
                "[디베이트·제안] 당신은 제안하고 방어하는 쪽입니다. 구체적인 안을 내고, "
                "앞서 제기된 비판이 있다면 회피하지 말고 정면으로 답하세요."
            )
        if agent.debate_stance == "critic":
            return (
                "[디베이트·비판] 당신은 검증하는 쪽입니다. 직전 제안의 가정·빈틈·실패 조건을 "
                "구체적으로 짚고, 막연한 우려 대신 재현 가능한 반례나 근거를 제시하세요."
            )
        return ""


class OrchestratorLedStrategy(BaseDebateStrategy):
    """오케스트레이터 지명: 매 라운드 오케스트레이터가 발언자와 순서를 정합니다.

    지금까지의 토론을 보고 **지금 필요한 에이전트만** 부릅니다. 전원이 매 라운드
    한 번씩 말하는 다른 전략과 달리, 한 라운드에 한 명만 부를 수도 있습니다.

    실제 선택은 엔진이 합니다 (`orchestrator_selects_speakers`). LLM 을 부르는
    일이라 순수 함수인 전략 객체가 할 수 없고, 실패하면 여기 있는 우선순위 순서로
    물러섭니다 — 엔드포인트가 없거나 응답이 이상해도 토론은 돌아야 합니다.
    """

    name = "orchestrator_led"
    display_name = "오케스트레이터 지명 (Orchestrator-Led)"
    orchestrator_selects_speakers = True

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState
    ) -> List[Agent]:
        specialists = order_by_priority(specialists_of(active_agents))
        return specialists if specialists else active_agents

    def turn_instruction(
        self, agent: Agent, speakers: List[Agent], index: int, state: DebateState
    ) -> str:
        return (
            "[지명 발언] 오케스트레이터가 이번 순서에 당신을 지목했습니다. 지목 사유에 해당하는 "
            "부분을 먼저 처리한 뒤 의견을 이어가세요."
        )


class ParallelDispatchStrategy(BaseDebateStrategy):
    """병렬 지시: 오케스트레이터가 태스크를 나눠 주고 여러 에이전트가 동시에 답합니다.

    다른 세 전략은 한 명이 끝나야 다음 사람이 시작합니다. 그래서 라운드 시간은
    발언 시간의 합이고, 뒷사람은 앞사람의 결론을 읽고 이어갑니다. 여기서는
    반대입니다 — 오케스트레이터가 **서로 겹치지 않는 태스크**을 나눠 주고, 지목된
    에이전트들이 같은 시각에 각자의 일을 합니다. 라운드 시간은 가장 느린 한 명의
    시간이고, 서로의 이번 라운드 결과는 볼 수 없습니다.

    그래서 두 가지가 반드시 따라붙습니다.

    * **지시** — 같은 질문을 여럿에게 동시에 던지면 답이 겹칩니다. 무엇을 맡았는지
      개별로 적어 주고, 다른 사람이 무엇을 맡았는지도 알려 줘야 중복이 줄어듭니다.
    * **취합** — 아무도 서로를 못 봤으므로 라운드 끝에 오케스트레이터가 결과를
      합치고 충돌을 정리합니다. 이 접합부가 없으면 라운드는 그냥 독백 묶음이 되고,
      모순이 최종 합성까지 그대로 실려 갑니다.

    실제 분배와 취합은 엔진이 합니다 (`orchestrator_dispatches_parallel`). LLM 을
    부르는 일이라 순수 함수인 전략 객체가 할 수 없습니다. 분배에 실패하면 우선순위
    순의 전원을 태스크 없이 동시에 돌리는 것으로 물러섭니다 — 지시를 못 받았을 뿐,
    병렬이라는 성질은 남깁니다.
    """

    name = "parallel_dispatch"
    display_name = "병렬 지시 (Orchestrator Parallel Dispatch)"
    orchestrator_dispatches_parallel = True

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState
    ) -> List[Agent]:
        specialists = order_by_priority(specialists_of(active_agents))
        return specialists if specialists else active_agents

    def turn_instruction(
        self, agent: Agent, speakers: List[Agent], index: int, state: DebateState
    ) -> str:
        """분배가 실패했을 때 붙는 지침. 정상 경로에서는 엔진이 개별 태스크를 씁니다."""
        return (
            "[병렬 라운드] 오케스트레이터의 태스크 분배를 받지 못했습니다. 다른 에이전트가 "
            "지금 동시에 발언 중이라 그들의 이번 라운드 결과는 볼 수 없습니다. 당신의 전문 "
            "영역에 한정해 기여하고, 남의 결론이 필요하면 가정으로 명시하세요."
        )


class GraphDebateStrategy(BaseDebateStrategy):
    """그래프 토론: 에이전트를 선으로 이어 누구의 출력이 누구의 입력이 되는지 사람이 그립니다.

    순서·진영·지명·분배 대신 그래프가 정합니다 — 병렬(한 노드에서 여러 노드로 갈라짐), 합류
    (취합 노드), 반복(판정 노드의 "아니오" 선). 참여자도 그래프에 놓인 에이전트입니다.

    실제 실행은 엔진이 합니다 (`runs_graph`). 여기 있는 순서는 로스터 미리보기에서 그래프를
    읽지 못했을 때 보여 줄 기본값일 뿐, 토론에 쓰이지 않습니다.
    """

    name = "graph_debate"
    display_name = "그래프 토론 (Graph)"
    runs_graph = True

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState
    ) -> List[Agent]:
        specialists = order_by_priority(specialists_of(active_agents))
        return specialists if specialists else active_agents


STRATEGY_MAP = {
    "sequential_debate": SequentialDebateStrategy(),
    "adversarial_debate": AdversarialDebateStrategy(),
    "orchestrator_led": OrchestratorLedStrategy(),
    "parallel_dispatch": ParallelDispatchStrategy(),
    "graph_debate": GraphDebateStrategy(),
}

DEFAULT_STRATEGY = "sequential_debate"

# 이미 저장된 대화가 들고 있는 예전 이름. `sessions.strategy` 는 문자열이라, 이
# 표가 없으면 옛 대화가 전략을 잃고 기본값으로 떨어집니다 (그리고 화면의 선택
# 상자는 아는 값이 아니어서 빈칸이 됩니다).
LEGACY_STRATEGY_ALIASES = {
    "free_debate": "sequential_debate",
    "sequential_review": "sequential_debate",
}


def resolve_strategy_name(strategy_name: Optional[str]) -> str:
    """저장된 전략 이름을 지금 쓰는 이름으로 옮깁니다. 모르는 이름은 기본값."""
    name = (strategy_name or "").strip()
    if name in STRATEGY_MAP:
        return name
    return LEGACY_STRATEGY_ALIASES.get(name, DEFAULT_STRATEGY)


def get_strategy(strategy_name: Optional[str]) -> BaseDebateStrategy:
    return STRATEGY_MAP[resolve_strategy_name(strategy_name)]
