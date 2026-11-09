"""단계 실행기들이 함께 쓰는 서비스 묶음."""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from app.orchestration.artifacts import format_roster

if TYPE_CHECKING:
    from app.agents.base import Agent
    from app.orchestration.context import EngineContext
    from app.orchestration.state import DebateState
    from app.orchestration.conversation import ConversationMemory
    from app.orchestration.recording import TurnRecorder
    from app.orchestration.speech import SpeechRunner


class PhaseExecutor:
    """계획·토론·합성 단계 실행기의 바탕. 엔진을 되부르지 않고 이 서비스들만 씁니다."""

    def __init__(
        self,
        ctx: "EngineContext",
        recorder: "TurnRecorder",
        speech: "SpeechRunner",
        conversation: "ConversationMemory",
    ) -> None:
        self.ctx = ctx
        self.recorder = recorder
        self.speech = speech
        self.conversation = conversation

    def roster(self, agents: Sequence["Agent"], state: "DebateState", *, with_keys: bool = False) -> str:
        """오케스트레이터에게 제공할 전문가 목록입니다. 각 MCP 서버별로 해당 대화 세션에서 호출 가능한 도구 이름을 함께 표기합니다."""
        return format_roster(
            list(agents), with_keys=with_keys,
            tools_of=lambda agent: self.ctx.reachable_tool_names(agent, state),
        )
