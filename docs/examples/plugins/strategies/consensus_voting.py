"""예제 토론 전략: 첫 라운드에 안을 내고, 다음 라운드부터 그 안에 투표합니다.

이 파일을 프로젝트 루트의 `strategies/` 폴더에 복사하고 서버를 다시 시작하면 전략 선택 상자에 나타납니다.
설명: docs/user_manual/03-core/11-plugins.md
"""

from typing import List

from app.agents.base import Agent
from app.orchestration.state import DebateState
from app.orchestration.strategies import BaseDebateStrategy, order_by_priority, specialists_of


class ConsensusVotingStrategy(BaseDebateStrategy):
    # 대화에 저장되는 이름입니다. 바꾸면 이 전략을 쓰던 대화는 기본 전략으로 돌아갑니다.
    name = "consensus_voting"
    display_name = "합의 투표 (Consensus Voting)"

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState,
    ) -> List[Agent]:
        """이번 라운드의 발언자. 라운드마다 불리므로 LLM 호출이나 파일 읽기 없이 바로 답합니다.

        에이전트 키를 직접 적지 않습니다. 사람이 화면에서 만든 에이전트도 같은 규칙으로 불려야 합니다.
        오케스트레이터는 라운드 밖에 서므로 `specialists_of` 로 뺍니다.
        """
        speakers = order_by_priority(specialists_of(active_agents)) or active_agents
        # 투표 라운드는 순서를 뒤집어, 첫 라운드에 먼저 말한 사람의 안으로 쏠리는 것을 줄입니다.
        return speakers if round_num == 1 else list(reversed(speakers))

    def turn_instruction(
        self, agent: Agent, speakers: List[Agent], index: int, state: DebateState,
    ) -> str:
        """이 발언 차례에 덧붙일 지침. 객체 하나를 모든 대화가 함께 쓰므로 대화별 값은 `state` 에서 읽습니다."""
        if state.current_round <= 1:
            return "[제안] 당신의 전문 영역에서 구체적인 안을 하나 내고, 그 근거와 가정을 밝히세요."
        return "[투표] 앞서 나온 안 중 하나를 고르고 그 이유를 밝히세요. 자신의 안을 골랐다면 약점도 함께 적으세요."
