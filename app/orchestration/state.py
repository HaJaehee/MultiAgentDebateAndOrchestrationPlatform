from datetime import datetime
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
    # 발언이 실제로 시작·종료된 시각 (`MessageModel.started_at` / `finished_at`).
    # 걸리는 시간이 없는 기록(사람 발언, 지명 결과)은 두 값이 같습니다.
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # 이 발언이 턴을 마무리한 합성 발언이면 그 턴이 시작된 시각 (`MessageModel.turn_started_at`).
    turn_started_at: Optional[datetime] = None
    # 그래프 토론에서 이 발언을 낸 노드 (`MessageModel.graph_node_id`).
    graph_node_id: Optional[str] = None
    # 노드의 출력으로 나간 핀 (`MessageModel.graph_port`). 출력이 아닌 기록은 None.
    graph_port: Optional[str] = None
    # 이 기록이 턴의 흐름에서 맡은 자리 (`MessageModel.turn_meta`, `app/orchestration/turns.py`).
    turn_meta: Optional[Dict[str, Any]] = None

    @property
    def speaker(self) -> str:
        """프롬프트에 적는 발언자 — "System Architect (High-Level …)".

        역할이 없으면 이름만 씁니다. 유저 발언이 그렇습니다 — 이 앱의 유저는 한 명이고
        요청하는 쪽도 늘 그 유저라, 역할 칸이 이름을 되풀이할 뿐입니다.
        """
        return f"{self.sender_name} ({self.sender_role})" if self.sender_role else self.sender_name


class DebateState(BaseModel):
    session_id: str
    user_prompt: str
    # 이 턴의 기록 (`TurnModel`). 발언·도구 기록이 모두 이것을 가리킵니다.
    turn_id: Optional[str] = None
    # 끊긴 턴을 다시 세운 경우 (ADR-024). `stopped_early` 와 함께 켜지면 사람이 아니라 서버
    # 중단으로 덜 논의된 것이라, 합성 지시와 보고서가 그렇게 적습니다.
    interrupted: bool = False
    # 이 턴이 끊긴 채로 있던 시간의 합(초)과 이어 간 횟수. 보고서의 총 경과에 함께 적습니다.
    paused_seconds: int = 0
    resumed_count: int = 0
    # 도구 단위로 이어 갈 발언의 초안 (ADR-025). 키는 `turns.draft_key`, 값은 {id, kind, started_at,
    # state}. `_speak` 이 자기 자리의 초안을 꺼내 이어 가고, 쓰이지 않은 것은 턴이 끝날 때 정리됩니다.
    resume_drafts: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # 이 턴이 빌린 MCP 런타임의 작업 공간. 런타임 객체 자체가 아니라 **키**를
    # 들고 다닙니다 — 살아 있는 프로세스 묶음의 소유권은 풀에 있고, 상태는
    # 직렬화되어 화면과 기록으로 흘러가기 때문입니다 (`app/mcp/pool.py`).
    workspace_dir: str = ""
    strategy: str = "sequential_debate"
    max_rounds: int = 3
    current_round: int = 0
    custom_instructions: str = ""
    active_agent_keys: List[str] = Field(default_factory=list)
    # 입력창에서 `@전문가 @스킬` 로 지정한 스킬 (전문가 키 → 스킬). 이 턴에만 걸리고, 그 전문가의
    # 발언마다 미리 불립니다 (`OrchestratorEngine._speak`). 개입 메모로 더해질 수 있습니다.
    skill_designations: Dict[str, List[str]] = Field(default_factory=dict)
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
    # `messages` 중 **이번 턴**이 시작되는 자리. 앞쪽은 이전 턴 기록을 맥락으로 불러온
    # 것입니다. 산출물(전문가 코드, 합성 실패 시의 대체 결론)은 이번 턴 발언에서만 모읍니다.
    turn_message_start: int = 0
    # LLM 응답을 받지 못해 이번 턴에서 발언하지 못한 에이전트. 합성 프롬프트와
    # 요약 아티팩트가 "없는 의견"을 있는 것처럼 다루지 않도록 여기에 남깁니다.
    failed_agent_keys: List[str] = Field(default_factory=list)
    # ---- 대화 기억 (`app/orchestration/context_memory.py`) ----
    # 이번 턴 오케스트레이터 계획 발언의 자리. 목표 메시지에 고정합니다.
    plan_index: Optional[int] = None
    # 결정 장부와, 장부에 반영된 발언의 수 (`messages[:ledger_through]`).
    decision_ledger: str = ""
    ledger_through: int = 0
    # 앞선 기록의 누적 요약과, 요약이 덮는 발언의 수 (`messages[:summary_through]`).
    transcript_summary: str = ""
    summary_through: int = 0
    # 이번 턴 참여자 중 가장 작은 컨텍스트 예산. 장부·요약의 크기 상한이 여기에 맞춰집니다.
    # 0 이면 모름 — 그때는 부르는 쪽의 예산을 씁니다.
    memory_budget: int = 0
    artifacts: List[ArtifactItem] = Field(default_factory=list)
    status: str = "idle"  # 'idle', 'planning', 'debating', 'synthesizing', 'completed', 'error'
    current_speaker: Optional[str] = None
    error_message: Optional[str] = None
