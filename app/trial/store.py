"""체험 화면이 읽고 쓰는 것 — 내 대화, 내 사본, 평가, 결과.

모든 조회는 **방문자 id 로 거릅니다.** 대화 id 는 주소창에 드러나므로, 남의 대화 id 를 적어
넣어도 "없는 대화" 로 보여야 합니다. 그래서 대화를 가져오는 길은 `owned_session()` 하나입니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MessageModel, SessionModel
from app.trial.models import (
    TrialFeedbackModel,
    TrialSessionModel,
    TrialTemplateCopyModel,
    TrialUserModel,
)
from app.trial.templates import TrialTemplate, parse_template


def aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 대화
# ---------------------------------------------------------------------------


@dataclass
class SessionRow:
    session_id: str
    title: str
    template_ref: str
    template_title: str
    created_at: Optional[datetime]
    # done · started (돌았지만 결과가 없음) · empty (아직 요청 전). 진행 중인지는 러너가 압니다.
    state: str


async def _session_states(db: AsyncSession, session_ids: List[str]) -> Dict[str, str]:
    if not session_ids:
        return {}
    done = set((await db.execute(
        select(MessageModel.session_id)
        .where(MessageModel.session_id.in_(session_ids), MessageModel.turn_started_at.is_not(None))
        .distinct()
    )).scalars().all())
    started = set((await db.execute(
        select(MessageModel.session_id)
        .where(MessageModel.session_id.in_(session_ids), MessageModel.msg_type == "user")
        .distinct()
    )).scalars().all())
    return {sid: "done" if sid in done else "started" if sid in started else "empty" for sid in session_ids}


async def list_user_sessions(db: AsyncSession, user_id: str) -> List[SessionRow]:
    rows = (await db.execute(
        select(TrialSessionModel, SessionModel.title)
        .join(SessionModel, SessionModel.id == TrialSessionModel.session_id)
        .where(TrialSessionModel.user_id == user_id)
        .order_by(TrialSessionModel.created_at.desc())
    )).all()
    states = await _session_states(db, [r[0].session_id for r in rows])
    return [
        SessionRow(
            session_id=trial.session_id, title=title, template_ref=trial.template_ref,
            template_title=trial.template_title, created_at=aware(trial.created_at),
            state=states.get(trial.session_id, "empty"),
        )
        for trial, title in rows
    ]


async def owned_session(db: AsyncSession, user_id: str, session_id: str) -> Optional[TrialSessionModel]:
    """이 방문자의 대화면 그 표시를, 아니면(없거나 남의 것) None."""
    row = (await db.execute(
        select(TrialSessionModel)
        .join(SessionModel, SessionModel.id == TrialSessionModel.session_id)
        .where(TrialSessionModel.session_id == session_id, TrialSessionModel.user_id == user_id)
    )).scalar_one_or_none()
    return row


async def delete_trial_session(db: AsyncSession, user_id: str, session_id: str) -> bool:
    """내 대화를 지웁니다. 발언·산출물·페르소나는 코어의 관계 설정대로 함께 지워집니다."""
    if await owned_session(db, user_id, session_id) is None:
        return False
    session = await db.get(SessionModel, session_id)
    if session is not None:
        await db.delete(session)
    await db.execute(delete(TrialFeedbackModel).where(TrialFeedbackModel.session_id == session_id))
    await db.execute(delete(TrialSessionModel).where(TrialSessionModel.session_id == session_id))
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# 결과
# ---------------------------------------------------------------------------


@dataclass
class SessionResult:
    final: str = ""
    final_at: Optional[datetime] = None
    turn_seconds: Optional[float] = None
    ledger: str = ""
    user_turns: int = 0


async def session_result(db: AsyncSession, session_id: str) -> SessionResult:
    """가장 최근 턴의 합성 발언과 결정 장부."""
    result = SessionResult()
    session = await db.get(SessionModel, session_id)
    if session is None:
        return result
    result.ledger = session.decision_ledger or ""
    final = (await db.execute(
        select(MessageModel)
        .where(MessageModel.session_id == session_id, MessageModel.turn_started_at.is_not(None))
        .order_by(MessageModel.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if final is not None:
        result.final = final.content or ""
        result.final_at = aware(final.finished_at or final.created_at)
        started, finished = aware(final.turn_started_at), aware(final.finished_at)
        if started and finished:
            result.turn_seconds = max(0.0, (finished - started).total_seconds())
    result.user_turns = int((await db.execute(
        select(func.count()).select_from(MessageModel)
        .where(MessageModel.session_id == session_id, MessageModel.msg_type == "user")
    )).scalar() or 0)
    return result


_LEDGER_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$")


def ledger_sections(ledger: str) -> Dict[str, str]:
    """결정 장부를 제목별로 나눕니다 (`## 결정 사항` → "결정 사항")."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in (ledger or "").splitlines():
        match = _LEDGER_HEADING.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


_NOTHING = re.compile(r"^[-*\s]*(없음|none|n/?a)?[.\s]*$", re.IGNORECASE)


def meaningful(text: str) -> bool:
    """"- 없음" 같은 빈 칸이 아닌지."""
    return any(not _NOTHING.match(line) for line in (text or "").splitlines() if line.strip())


def agreed_and_open(ledger: str) -> Dict[str, str]:
    """결과 화면의 두 칸 — 모두 동의한 것과 의견이 갈린 것."""
    sections = ledger_sections(ledger)

    def pick(*needles: str) -> str:
        for title, body in sections.items():
            if any(n in title for n in needles) and meaningful(body):
                return body
        return ""

    return {"agreed": pick("결정"), "open": pick("미해결", "쟁점", "이견")}


# ---------------------------------------------------------------------------
# 사본
# ---------------------------------------------------------------------------


async def create_copy(db: AsyncSession, user_id: str, template: TrialTemplate, base_ref: str) -> TrialTemplateCopyModel:
    data = template.model_dump(mode="json")
    data["title"] = (f"{template.title} (내 사본)")[:80]
    copy = TrialTemplateCopyModel(user_id=user_id, base_ref=base_ref, title=data["title"])
    db.add(copy)
    await db.flush()
    data["id"] = f"copy-{copy.id[:8]}"
    copy.data = data
    await db.commit()
    return copy


async def get_copy(db: AsyncSession, user_id: str, copy_id: str) -> Optional[TrialTemplateCopyModel]:
    copy = await db.get(TrialTemplateCopyModel, copy_id)
    return copy if copy is not None and copy.user_id == user_id else None


def copy_template(copy: TrialTemplateCopyModel) -> TrialTemplate:
    return parse_template(dict(copy.data or {}))


async def list_copies(db: AsyncSession, user_id: str) -> List[TrialTemplateCopyModel]:
    return list((await db.execute(
        select(TrialTemplateCopyModel)
        .where(TrialTemplateCopyModel.user_id == user_id)
        .order_by(TrialTemplateCopyModel.updated_at.desc())
    )).scalars().all())


async def save_copy(db: AsyncSession, copy: TrialTemplateCopyModel, template: TrialTemplate) -> None:
    copy.data = template.model_dump(mode="json")
    copy.title = template.title
    copy.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def delete_copy(db: AsyncSession, user_id: str, copy_id: str) -> bool:
    copy = await get_copy(db, user_id, copy_id)
    if copy is None:
        return False
    await db.delete(copy)
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# 평가
# ---------------------------------------------------------------------------


async def get_feedback(db: AsyncSession, user_id: str, session_id: str) -> Optional[TrialFeedbackModel]:
    return (await db.execute(
        select(TrialFeedbackModel)
        .where(TrialFeedbackModel.session_id == session_id, TrialFeedbackModel.user_id == user_id)
    )).scalar_one_or_none()


async def save_feedback(
    db: AsyncSession, user_id: str, session_id: str, template_ref: str, rating: int, comment: str,
) -> TrialFeedbackModel:
    rating = 1 if rating > 0 else -1 if rating < 0 else 0
    feedback = await get_feedback(db, user_id, session_id)
    if feedback is None:
        feedback = TrialFeedbackModel(session_id=session_id, user_id=user_id, template_ref=template_ref)
        db.add(feedback)
    feedback.rating = rating
    feedback.comment = (comment or "").strip()[:2000]
    feedback.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return feedback


# ---------------------------------------------------------------------------
# 운영자
# ---------------------------------------------------------------------------


async def reset_pin(db: AsyncSession, user_id: str) -> bool:
    """PIN 을 지우고 잠금을 풉니다. 다음 로그인의 PIN 이 새 PIN 이 되고, 지금 로그인은 끊깁니다."""
    user = await db.get(TrialUserModel, user_id)
    if user is None:
        return False
    user.pin_hash = ""
    user.failed_count = 0
    user.locked_until = None
    await db.commit()
    return True


async def unlock_user(db: AsyncSession, user_id: str) -> bool:
    user = await db.get(TrialUserModel, user_id)
    if user is None:
        return False
    user.failed_count = 0
    user.locked_until = None
    await db.commit()
    return True
