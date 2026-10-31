"""예제 토론 전략: 첫 번째 라운드에서 제안을 제시하고, 이후 라운드부터 제안들에 대해 교차 투표합니다.

이 파일을 프로젝트 루트의 `strategies/` 디렉터리에 복사하고 서버를 재시작하면 토론 전략 선택 메뉴에 표시됩니다.
참조 가이드: docs/user_manual/03-core/11-plugins.md
"""

from typing import List

from app.agents.base import Agent
from app.orchestration.state import DebateState
from app.orchestration.strategies import BaseDebateStrategy, order_by_priority, specialists_of


class ConsensusVotingStrategy(BaseDebateStrategy):
    # 대화 세션에 영구 저장되는 전략 식별자입니다. 이름을 변경하면 기존 세션은 기본 전략으로 안전하게 폴백합니다.
    name = "consensus_voting"
    display_name = "합의 투표 (Consensus Voting)"

    def get_speakers_for_round(
        self, active_agents: List[Agent], round_num: int, state: DebateState,
    ) -> List[Agent]:
        """이번 라운드의 발언자 목록을 반환합니다. 매 라운드 호출되므로 I/O 없이 순수 인메모리 연산으로 즉시 반환해야 합니다.

        하드코딩된 에이전트 키 대신 활성 목록을 순회하여 사용자가 UI에서 동적으로 추가한 전문가도 동일하게 참여하도록 합니다.
        마스터 오케스트레이터는 라운드 발언 대상이 아니므로 `specialists_of`로 제외합니다.
        """
        speakers = order_by_priority(specialists_of(active_agents)) or active_agents
        # 투표 라운드에서는 발언 순서를 역순으로 뒤집어, 1라운드 선발언자의 제안으로 편향되는 현상을 완화합니다.
        return speakers if round_num == 1 else list(reversed(speakers))

    def turn_instruction(
        self, agent: Agent, speakers: List[Agent], index: int, state: DebateState,
    ) -> str:
        """현재 발언 차례에 주입할 전략별 지침을 생성합니다. 전략 인스턴스는 공유되므로 세션별 상태는 `state`에서 조회합니다."""
        if state.current_round <= 1:
            return "[제안] 당신의 전문 영역에서 구체적인 안을 하나 내고, 그 근거와 가정을 밝히세요."
        return "[투표] 앞서 나온 안 중 하나를 고르고 그 이유를 밝히세요. 자신의 안을 골랐다면 약점도 함께 적으세요."
