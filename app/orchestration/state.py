from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from app.agents.base import Agent


class ArtifactItem(BaseModel):
    id: Optional[str] = None
    artifact_type: str = "markdown"  # 'code', 'markdown', 'mermaid', 'json'
    title: str
    content: str
    language: str = "markdown"


class DebateMessage(BaseModel):
    id: Optional[str] = None
    sender_key: str
    sender_name: str
    sender_role: str
    content: str
    round_number: int = 0
    msg_type: str = "agent"  # 'user', 'orchestrator', 'agent', 'system', 'error'
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)


class DebateState(BaseModel):
    session_id: str
    user_prompt: str
    strategy: str = "sequential_debate"
    max_rounds: int = 3
    current_round: int = 0
    custom_instructions: str = ""
    active_agent_keys: List[str] = Field(default_factory=list)
    messages: List[DebateMessage] = Field(default_factory=list)
    tool_records: List[Dict[str, Any]] = Field(default_factory=list)
    is_consensus_reached: bool = False
    # 사용자가 남은 라운드를 건너뛰고 합성으로 넘어가도록 요청했는지. 합성
    # 프롬프트가 "덜 논의된 상태" 를 알고 쓰도록 여기에 남깁니다.
    stopped_early: bool = False
    # 토론 도중 끼어든 사용자 개입 발언의 수.
    interjection_count: int = 0
    # 컨텍스트 창이 넘쳐 사람에게 물어본 결과. 한 턴에 한 번만 묻고 그 답을
    # 나머지 발언에 그대로 씁니다 — 발언마다 물으면 토론을 진행할 수 없습니다.
    # None 이면 아직 묻지 않은 것이고, 값이 있으면 넓혀 준 토큰 수입니다.
    context_grant: Optional[int] = None
    # 컨텍스트 한도로 생략된 기록의 누적 건수 (발언·도구 관측·합성 전사 전부).
    context_dropped: int = 0
    # LLM 응답을 받지 못해 이번 턴에서 발언하지 못한 에이전트. 합성 프롬프트와
    # 요약 아티팩트가 "없는 의견"을 있는 것처럼 다루지 않도록 여기에 남깁니다.
    failed_agent_keys: List[str] = Field(default_factory=list)
    artifacts: List[ArtifactItem] = Field(default_factory=list)
    status: str = "idle"  # 'idle', 'planning', 'debating', 'synthesizing', 'completed', 'error'
    current_speaker: Optional[str] = None
    error_message: Optional[str] = None
