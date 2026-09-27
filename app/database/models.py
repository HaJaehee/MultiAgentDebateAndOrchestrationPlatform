import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(255), default="New Debate Session")
    strategy: Mapped[str] = mapped_column(String(50), default="sequential_debate")
    max_rounds: Mapped[int] = mapped_column(Integer, default=3)
    # 병렬 지시 전략에서 한 라운드에 동시에 띄울 에이전트 수의 상한.
    #
    # 상한이 필요한 이유는 엔드포인트 쪽에 있습니다. 로컬 단일 GPU 런타임
    # (Ollama·vLLM·LM Studio)에 동시 요청을 다섯 개 던지면 큐에 쌓이거나 메모리가
    # 터지고, 그 실패는 "에이전트가 응답하지 못했다" 로 나타납니다. 상한을 넘는
    # 지시는 버리지 않고 순차적으로 밀려 실행됩니다.
    parallel_limit: Mapped[int] = mapped_column(Integer, default=3)
    active_agents: Mapped[List[str]] = mapped_column(JSON, default=list)
    # 이 대화의 로스터를 마지막으로 저장할 때 **존재하던** 에이전트 전부.
    #
    # `active_agents` 는 켜 둔 것만 담는 허용 목록이라, 목록에 없는 키가 "사용자가
    # 끈 에이전트" 인지 "그때는 없던 에이전트" 인지 구분할 수 없습니다. 그래서
    # conf.json 에 에이전트를 새로 추가하면 기존 대화에서 전부 꺼진 것으로 보였습니다.
    # 그때 무엇이 있었는지를 함께 적어 두면 둘을 가릴 수 있습니다.
    known_agents: Mapped[List[str]] = mapped_column(JSON, default=list)
    custom_instructions: Mapped[str] = mapped_column(Text, default="")
    # 결정 장부 — 오케스트레이터가 라운드마다 갱신하고, 시스템 프롬프트의 커스텀 지침 바로
    # 뒤에 들어갑니다 (`app/orchestration/context_memory.py`). 턴이 정상 종료될 때만 씁니다.
    decision_ledger: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 장부에 반영된 마지막 발언. 비어 있으면 아직 아무것도 반영하지 않았습니다.
    ledger_through_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # 컨텍스트 한도로 접은 앞선 기록의 누적 요약과, 요약이 덮는 마지막 발언.
    transcript_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_through_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # 첫 유저 메시지가 기록되는 순간 True 가 되며, 이후 페르소나 수정이 금지됩니다.
    personas_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 이 대화가 쓸 작업 공간. 비어 있으면 conf.json 의 WORKSPACE_DIR 기본값을 씁니다.
    # 페르소나와 달리 잠기지 않습니다 — 토론 도중에도 바꿀 수 있어야 합니다.
    workspace_dir: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 그래프 토론: 이 대화가 쓰는 그래프 파일 id (`data/graphs/<id>.json`), 그리고 **마지막 턴이
    # 실제로 돈** 그래프. 턴이 시작될 때 굳혀, 토론 중에 파일을 고치거나 지워도 흔들리지 않습니다.
    graph_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    graph_snapshot: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 도구 보안 모드 (`read_only` · `default` · `review` · `auto`). 비어 있으면 conf.json 의
    # `tool_security.mode` 를 따릅니다. 페르소나와 달리 잠기지 않습니다.
    tool_mode: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    # 승인 카드의 "이 대화에서 허용"·"이 대화에서 거부" 로 쌓인 규칙 (`read(src/**)` 같은
    # 문자열). 로스터의 규칙 창에서 보고 지울 수 있습니다.
    tool_grants: Mapped[List[str]] = mapped_column(JSON, default=list)
    tool_denials: Mapped[List[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    messages: Mapped[List["MessageModel"]] = relationship(
        "MessageModel", back_populates="session", cascade="all, delete-orphan", order_by="MessageModel.created_at", lazy="selectin"
    )
    artifacts: Mapped[List["ArtifactModel"]] = relationship(
        "ArtifactModel", back_populates="session", cascade="all, delete-orphan", order_by="ArtifactModel.created_at", lazy="selectin"
    )
    tool_calls: Mapped[List["ToolCallRecordModel"]] = relationship(
        "ToolCallRecordModel", back_populates="session", cascade="all, delete-orphan", order_by="ToolCallRecordModel.created_at", lazy="selectin"
    )
    agent_personas: Mapped[List["SessionAgentModel"]] = relationship(
        "SessionAgentModel", back_populates="session", cascade="all, delete-orphan", order_by="SessionAgentModel.agent_key", lazy="selectin"
    )
    # 턴 기록 (ADR-024). 세션을 ORM 으로 지울 때(`db.delete(session)`, 체험 서버) 함께 지워지도록
    # 관계를 둡니다. SQLite 가 외래키를 검사하지 않아, 관계가 없으면 턴 행만 남습니다.
    turns: Mapped[List["TurnModel"]] = relationship(
        "TurnModel", back_populates="session", cascade="all, delete-orphan", order_by="TurnModel.started_at", lazy="selectin"
    )


# 턴의 상태 (`TurnModel.status`).
#
#   running     : 도는 중. 서버가 뜰 때 이 값으로 남아 있으면 죽은 턴입니다 (프로세스가 하나뿐).
#   completed   : 합성까지 마쳤습니다.
#   failed      : 엔진이 예외로 멈췄습니다. 끊긴 턴과 똑같이 이어 가거나 마무리할 수 있습니다.
#   interrupted : 서버가 턴 도중에 내려갔습니다.
#   abandoned   : 끊긴 채로 두고 새 요청을 보냈습니다. 기록은 남기되 더는 묻지 않습니다.
TURN_RUNNING = "running"
TURN_COMPLETED = "completed"
TURN_FAILED = "failed"
TURN_INTERRUPTED = "interrupted"
TURN_ABANDONED = "abandoned"
# 사람이 이어 가기·마무리·버리기를 고를 수 있는 턴.
TURN_UNFINISHED = (TURN_FAILED, TURN_INTERRUPTED)


class TurnModel(Base):
    """토론 한 턴 — 사람의 요청 하나로 시작해 합성으로 끝나는 단위 (ADR-024).

    예전에는 턴을 따로 기록하지 않았습니다. 턴이 끝났다는 표시는 합성 발언의
    `turn_started_at` 하나뿐이라, 서버가 턴 도중에 내려가면 그 턴은 흔적 없이 피드에 남아
    다음 요청의 맥락에 섞였습니다. 이 행이 있어야 끊긴 턴을 알아보고, 이어 가거나
    마무리하거나 버릴 수 있습니다.

    여는 요청과 **같은 커밋**에 들어갑니다. 턴에 딸린 발언과 도구 기록은 `turn_id` 로 이 행을
    가리킵니다.
    """

    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=TURN_RUNNING, nullable=False)
    # planning · debating · synthesizing · completed. 끊긴 턴을 어디서부터 이을지 정하는 첫 근거입니다.
    phase: Mapped[str] = mapped_column(String(20), default="planning", nullable=False)
    opening_message_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # 턴이 시작될 때의 구성 — 참여자·전략·라운드 수·동시 실행 상한·커스텀 지침·작업 공간.
    # 끊긴 사이에 로스터를 바꿨더라도 그 턴은 이 구성으로 마칩니다.
    config: Mapped[Any] = mapped_column(JSON, default=dict)
    # 엔진이 예외로 멈췄을 때의 사유 (`failed`).
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 끊긴 채로 있던 시간의 합(초)과 이어 간 횟수. 보고서의 총 경과에 함께 적습니다.
    paused_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resumed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 합성에 들어갈 때 정지(사람의 정지 요청)로 덜 논의된 턴이었는지. 합성 도중에 끊긴 턴을
    # 이어 갈 때 합성 지시와 합의 판정이 이 값을 씁니다 — 정지 요청 자체는 메모리에만 있었습니다.
    stopped_early: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped["SessionModel"] = relationship("SessionModel", back_populates="turns")
    # 끝나지 못한 발언의 초안 (ADR-025). 턴을 ORM 으로 지우면 함께 지워집니다. 초안은 크므로
    # 턴을 읽을 때 함께 싣지 않습니다 (lazy="select" — 지울 때만 불러옵니다).
    drafts: Mapped[List["SpeechDraftModel"]] = relationship(
        "SpeechDraftModel", back_populates="turn", cascade="all, delete-orphan", lazy="select"
    )


class SpeechDraftModel(Base):
    """도구를 쓰는 중인 발언의 초안 — 서버가 내려가도 도구 단위로 이어 가기 위해 (ADR-025).

    도구 루프가 도구를 부를 때와 도구 결과를 받을 때마다 **모델이 보고 있던 메시지 그대로**와
    루프의 셈(부른 횟수, 사람이 늘려 준 상한, 넓혀 준 창, 판마다 나온 글)을 남깁니다. 발언이
    기록되면 같은 커밋에서 지워지므로, 남아 있는 초안은 곧 끝나지 못한 발언입니다. 끊긴 턴을
    이어 가면 그 발언은 처음부터가 아니라 마지막으로 남긴 판 다음부터 이어집니다.

    `id` 는 그 발언이 기록될 `messages.id` 입니다. 끊기기 전에 실행된 도구 기록이 이어 간 발언에
    그대로 이어지도록 미리 정해 둡니다.
    """

    __tablename__ = "speech_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    turn_id: Mapped[str] = mapped_column(String(36), ForeignKey("turns.id", ondelete="CASCADE"), index=True)
    # 이어 갈 때 어느 발언의 초안인지 맞추는 값 (`OrchestratorEngine._speak`).
    agent_key: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20), default="speech")
    round_number: Mapped[int] = mapped_column(Integer, default=0)
    graph_node_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 발언이 실제로 시작된 시각. 이어 간 발언의 카드는 처음 시작한 시각부터 셉니다.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # 도구 루프의 상태 (`app/agents/llm.py` 의 `SPEECH_STATE_VERSION`)와, 그 발언이 이미 기록한
    # 도구 호출의 id (`tool_record_ids`).
    state: Mapped[Any] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    turn: Mapped["TurnModel"] = relationship("TurnModel", back_populates="drafts")


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    sender_key: Mapped[str] = mapped_column(String(50))  # e.g., 'user', 'orchestrator', 'architect'
    sender_name: Mapped[str] = mapped_column(String(100))
    sender_role: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    round_number: Mapped[int] = mapped_column(Integer, default=0)
    msg_type: Mapped[str] = mapped_column(String(30), default="agent")  # 'user', 'orchestrator', 'agent', 'system'
    # **정렬 키**입니다. 발언 시작 시각이 아닙니다.
    #
    # 발언 행은 LLM 응답이 다 온 **뒤에** 들어가므로 이 값은 대략 끝난 시각이고,
    # 병렬 라운드에서는 아예 `라운드 기준 시각 + 지시 순번(ms)` 으로 덮어씁니다 —
    # 완료 순서가 제각각이라 커밋 시각을 그대로 쓰면 새로고침할 때마다 발언 순서가
    # 달라지기 때문입니다 (`OrchestratorEngine._speak`). 기록을 다시 읽을 때
    # `order_by(created_at)` 이 이것을 씁니다. 사람에게 보여줄 시각은 아래 둘입니다.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # 발언이 실제로 시작된 시각과 끝난 시각 (벽시계). 병렬 라운드에서는 여러
    # 발언의 구간이 겹칩니다 — 그게 사실입니다.
    #
    # NULL 이면 이 컬럼이 생기기 전에 기록된 발언입니다. 그때는 `created_at` 하나만
    # 있고, 그것이 시작인지 끝인지 알 수 없으므로 화면과 문서는 한 시각만 적습니다.
    # 사람 발언이나 지명 기록처럼 걸리는 시간이 없는 것은 두 값이 같습니다.
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 이 발언이 **한 턴을 마무리한 합성 발언**일 때만 채웁니다: 그 턴을 연 사람 요청이
    # 기록된 시각. `finished_at - turn_started_at` 이 그 턴의 총 경과 시간입니다.
    #
    # 기록에서 거꾸로 추론하지 않고 따로 적는 이유: 토론 도중의 사람 개입도 똑같이
    # `msg_type="user"` 로 들어가고, 계획 직후의 개입은 `round_number=0` 이라 턴을 연
    # 요청과 구분되지 않습니다. 추론하면 개입이 있던 턴의 총 경과가 조용히 짧아집니다.
    # 다른 발언은 NULL 이고, 그래서 이 값이 곧 "이 행이 턴을 마무리했다" 는 표시입니다.
    turn_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 그래프 토론에서 이 발언을 낸 노드. 같은 에이전트가 여러 노드에 놓일 수 있어 발언자만으로는
    # 어느 노드였는지 알 수 없습니다. 그래프 토론이 아니면 NULL.
    graph_node_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 이 발언이 노드의 **출력**으로 나간 핀 — 에이전트·취합은 `out`, 판정은 `yes`/`no`. 노드에 붙었지만
    # 출력이 아닌 기록(방문 상한 안내)은 NULL. 새로고침한 화면이 어느 선으로 흘렀는지 다시 그리는 근거입니다.
    graph_port: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # 이 기록이 속한 턴 (`TurnModel`). 이 컬럼이 생기기 전의 기록은 NULL.
    turn_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    # 이 기록이 턴의 흐름에서 맡은 자리 (`app/orchestration/turns.py` 의 KIND_*). 끊긴 턴을 다시
    # 세울 때 기록을 문장이 아니라 이것으로 읽습니다 — 지명·분배는 누가 불렸고 무엇을 맡았는지를
    # 값으로 함께 적습니다. 화면에 보이는 문장은 `content` 에 그대로 있습니다.
    turn_meta: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    session: Mapped["SessionModel"] = relationship("SessionModel", back_populates="messages")
    tool_calls: Mapped[List["ToolCallRecordModel"]] = relationship(
        "ToolCallRecordModel", back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )


class ToolCallRecordModel(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # 이 호출을 낸 발언. 도구는 실행하는 **즉시** 기록되고(발언이 끝나기 전에 서버가 죽어도
    # 무엇을 실행했는지 남도록), 발언이 기록될 때 같은 커밋에서 여기가 채워집니다. 턴이
    # 끊긴 뒤에도 NULL 이면 끝나지 못한 발언이 실행한 것입니다.
    message_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    turn_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    agent_key: Mapped[str] = mapped_column(String(50))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[Any] = mapped_column(JSON, default=dict)
    output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="success")  # 'success', 'error', 'denied'
    # 도구 보안 판정 (`app/orchestration/tool_gate.py`). 비어 있으면 판정 없이 실행된 호출.
    #   decision : allow · approved · deny · hard · rejected · timeout
    #   rule     : 걸린 규칙 원문, 허락한 범위, 또는 `mode:<모드>`
    #   approver : 사람이 답했으면 local · remote
    decision: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    risk: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    rule: Mapped[str] = mapped_column(Text, default="", nullable=False)
    approver: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped["SessionModel"] = relationship("SessionModel", back_populates="tool_calls")
    message: Mapped[Optional["MessageModel"]] = relationship("MessageModel", back_populates="tool_calls")


class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(30), default="markdown")  # 'code', 'markdown', 'mermaid', 'json'
    title: Mapped[str] = mapped_column(String(255), default="Synthesized Artifact")
    content: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(50), default="markdown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped["SessionModel"] = relationship("SessionModel", back_populates="artifacts")


class SessionAgentModel(Base):
    """세션별 에이전트 페르소나 / 시스템 프롬프트 / 운영 설정 스냅샷.

    첫 유저 메시지 전에는 유저가 편집한 값을 담는 초안이고, 첫 메시지가 기록되는
    순간 그 시점의 유효값(초안이 없으면 conf.json 기본값)이 모든 에이전트에 대해
    기록되고 세션이 잠깁니다. 이후 세션을 다시 열면 여기 저장된 값이 사용됩니다.

    `config_snapshot` 은 그 시점의 `AgentConfig` 전체입니다 — 모델·엔드포인트·키·
    샘플링 값·도구 권한까지. 이것이 있어야 **시작한 대화가 자기완결적**입니다.
    conf.json 에서 그 에이전트를 지우거나 모델을 바꿔도 이 대화는 잠글 때의
    구성 그대로 이어집니다.
    """

    __tablename__ = "session_agents"
    __table_args__ = (UniqueConstraint("session_id", "agent_key", name="uq_session_agent"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(150), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # 이 대화에서 이 에이전트가 갖는 겉모습. `config_snapshot` 안에도 같은 값이
    # 들어 있지만, 카드를 그리는 데 필요한 두 값만은 컬럼으로 따로 둡니다 —
    # 스냅샷이 없던 옛 대화에도 붙고, JSON 을 풀지 않고도 읽힙니다.
    card_color: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    icon_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 잠글 때 굳힌 `AgentConfig` 전체. None 이면 이 컬럼이 생기기 전에 잠긴 대화라
    # 살아 있는 conf.json 을 그대로 씁니다 (지금까지 그래 왔던 대로).
    config_snapshot: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    session: Mapped["SessionModel"] = relationship("SessionModel", back_populates="agent_personas")
