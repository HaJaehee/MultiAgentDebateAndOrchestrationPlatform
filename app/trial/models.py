"""체험 서버의 테이블.

코어 테이블(`sessions` 등)에는 칸을 하나도 더하지 않습니다. 누가 어느 대화를 가졌는지는
`trial_sessions` 가 따로 적습니다. 주인 화면은 체험 대화도 보통 대화처럼 보고, 체험을
끄면 이 테이블들은 그냥 읽히지 않을 뿐입니다.

SQLite 의 외래 키 강제는 켜져 있지 않습니다(`app/database/session.py`). 그래서 주인 화면에서
대화를 지우면 `trial_sessions` 행이 남을 수 있습니다. 체험 쪽은 늘 `sessions` 와 **이어서**
읽으므로(내부 조인) 그런 행은 보이지 않습니다.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base, utc_now


def _uuid() -> str:
    return str(uuid.uuid4())


class TrialUserModel(Base):
    """이름과 PIN 으로 들어오는 방문자."""

    __tablename__ = "trial_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 화면에 보이는 이름. 처음 적은 그대로 둡니다.
    name: Mapped[str] = mapped_column(String(40))
    # 같은 사람인지 가리는 열쇠 (공백 정리 + 대소문자 무시). `auth.name_key`.
    name_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    # scrypt 해시. 비어 있으면 운영자가 PIN 을 초기화한 것이고, 다음 로그인의 PIN 이 새 PIN 이 됩니다.
    pin_hash: Mapped[str] = mapped_column(Text, default="")
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class TrialSessionModel(Base):
    """체험 대화 하나의 주인과 출처 템플릿."""

    __tablename__ = "trial_sessions"

    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("trial_users.id"), index=True)
    # `t:<공식 템플릿 id>` 또는 `c:<사본 id>`.
    template_ref: Mapped[str] = mapped_column(String(80), default="")
    # 템플릿이 나중에 지워져도 통계와 목록에 이름이 남도록 적어 둡니다.
    template_title: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TrialTemplateCopyModel(Base):
    """방문자가 템플릿을 복사해 고친 것. 본인만 보고 씁니다."""

    __tablename__ = "trial_template_copies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("trial_users.id"), index=True)
    base_ref: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    # 템플릿 전체 (`templates.TrialTemplate` 의 JSON). 공식 템플릿이 바뀌어도 사본은 그대로입니다.
    data: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class TrialFeedbackModel(Base):
    """결과에 대한 평가. 한 사람이 한 대화에 하나 (고치면 덮어씁니다)."""

    __tablename__ = "trial_feedback"
    __table_args__ = (UniqueConstraint("session_id", "user_id", name="uq_trial_feedback"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("trial_users.id"), index=True)
    template_ref: Mapped[str] = mapped_column(String(80), default="")
    # 1 = 좋아요, -1 = 아쉬워요, 0 = 의견만.
    rating: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
