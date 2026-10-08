"""비엔지니어 화면의 테이블.

체험 서버와 같은 원칙입니다. 코어 테이블(`sessions` 등)에는 칸을 하나도 더하지 않고, 누가 어느
대화를 가졌는지와 방문자가 만든 에이전트만 따로 적습니다. 대화 자체는 보통 대화와 같은 테이블에
있어 주인 화면에도 보입니다 (체험 대화와 같습니다).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base, utc_now
from app.trial import models as _trial_models  # noqa: F401 - `trial_users` 를 외래 키보다 먼저 등록합니다


def _uuid() -> str:
    return str(uuid.uuid4())


class EasySessionModel(Base):
    """쉬운 화면에서 시작한 대화 하나와 그 주인."""

    __tablename__ = "easy_sessions"

    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), primary_key=True)
    # 방문자 id (`trial_users.id`). 비어 있으면 주인(서버 PC 또는 접속 토큰)이 만든 대화입니다.
    user_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EasyAgentModel(Base):
    """방문자가 대화로 만든 에이전트. 본인의 대화에만 쓰고, conf.json 에는 들어가지 않습니다.

    주인이 만든 에이전트는 여기가 아니라 conf.json 에 바로 적힙니다 (`builder.save_owner_agent`).
    """

    __tablename__ = "easy_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("trial_users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[str] = mapped_column(String(200), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    allowed_mcp_servers: Mapped[Any] = mapped_column(JSON, default=list)
    allowed_skills: Mapped[Any] = mapped_column(JSON, default=list)
    card_color: Mapped[str] = mapped_column(String(20), default="")
    icon: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
