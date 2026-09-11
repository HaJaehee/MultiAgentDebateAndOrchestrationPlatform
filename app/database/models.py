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
    # 첫 유저 메시지가 기록되는 순간 True 가 되며, 이후 페르소나 수정이 금지됩니다.
    personas_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 이 대화가 쓸 작업 공간. 비어 있으면 conf.json 의 WORKSPACE_DIR 기본값을 씁니다.
    # 페르소나와 달리 잠기지 않습니다 — 토론 도중에도 바꿀 수 있어야 합니다.
    workspace_dir: Mapped[str] = mapped_column(Text, default="", nullable=False)
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

    session: Mapped["SessionModel"] = relationship("SessionModel", back_populates="messages")
    tool_calls: Mapped[List["ToolCallRecordModel"]] = relationship(
        "ToolCallRecordModel", back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )


class ToolCallRecordModel(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(50))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[Any] = mapped_column(JSON, default=dict)
    output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="success")  # 'success', 'error'
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
