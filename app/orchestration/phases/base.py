"""단계 실행기들이 함께 쓰는 서비스 묶음."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.orchestration.context import EngineContext
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
