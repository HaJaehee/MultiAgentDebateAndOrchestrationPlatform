"""비엔지니어 화면 전용 데이터베이스 모델.

체험 서버와 동일한 격리 원칙을 적용합니다. 코어 테이블(`sessions` 등)에는 추가 컬럼을 두지 않고,
세션 소유자 매핑 정보와 방문자가 생성한 에이전트만 별도 테이블로 분리하여 관리합니다. 대화 세션
자체는 기존 공용 세션 테이블(`sessions`)에 저장되므로 관리자(소유자) 화면에서도 확인할 수 있습니다 (체험 세션과 동일).
"""

import uuid
from datetime import datetime
from typing import Any, List

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base, utc_now
from app.trial import models as _trial_models  # noqa: F401 - `trial_users` 를 외래 키보다 먼저 등록합니다


def _uuid() -> str:
    return str(uuid.uuid4())


class EasySessionModel(Base):
    """비엔지니어 화면에서 생성된 대화 세션과 소유자 매핑 모델."""

    __tablename__ = "easy_sessions"

    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), primary_key=True)
    # 방문자 ID (`trial_users.id`). 비어 있으면 소유자(로컬 접속 또는 인증 토큰 사용자)가 생성한 세션입니다.
    user_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EasyAgentModel(Base):
    """방문자가 대화형 빌더로 생성한 에이전트 모델. 생성자 본인의 세션에서만 사용할 수 있으며, conf.json에는 반영되지 않습니다.

    소유자가 생성한 에이전트는 DB가 아닌 conf.json 파일에 직접 저장됩니다 (`builder.save_owner_agent`).
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
