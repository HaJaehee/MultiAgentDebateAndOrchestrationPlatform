"""운영자 사용량 화면의 숫자.

따로 쌓는 기록은 없습니다. 대화(`sessions`)·발언(`messages`)·체험 표시(`trial_sessions`)·
평가(`trial_feedback`)에서 그때그때 셉니다. 어느 템플릿에서 사람들이 실망하고 어디서 토론이
끝나지 못하는지가 보여야 템플릿을 고칠 수 있습니다.

"완료" 는 합성 발언(`turn_started_at` 이 있는 발언)이 하나라도 있는 대화, "미완료" 는 요청은
했지만 합성까지 가지 못한 대화입니다 (오류·중단·아직 진행 중).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MessageModel, SessionModel
from app.trial.models import TrialFeedbackModel, TrialSessionModel, TrialUserModel
from app.trial.store import aware


@dataclass
class TemplateUsage:
    ref: str
    title: str
    sessions: int = 0
    completed: int = 0
    unfinished: int = 0
    turns: int = 0
    seconds_total: float = 0.0
    seconds_count: int = 0
    up: int = 0
    down: int = 0

    @property
    def average_seconds(self) -> Optional[float]:
        return self.seconds_total / self.seconds_count if self.seconds_count else None


@dataclass
class UserUsage:
    user_id: str
    name: str
    sessions: int = 0
    last_login_at: Optional[datetime] = None
    locked_until: Optional[datetime] = None
    has_pin: bool = True


@dataclass
class FeedbackNote:
    when: Optional[datetime]
    user: str
    template: str
    rating: int
    comment: str
    session_id: str


@dataclass
class UsageReport:
    users: int = 0
    sessions: int = 0
    completed: int = 0
    templates: List[TemplateUsage] = field(default_factory=list)
    people: List[UserUsage] = field(default_factory=list)
    feedback: List[FeedbackNote] = field(default_factory=list)


async def usage_report(db: AsyncSession, *, feedback_limit: int = 30) -> UsageReport:
    report = UsageReport()

    trial_rows = (await db.execute(
        select(TrialSessionModel).join(SessionModel, SessionModel.id == TrialSessionModel.session_id)
    )).scalars().all()
    session_ids = [row.session_id for row in trial_rows]

    finals: Dict[str, List[MessageModel]] = {}
    started: set = set()
    if session_ids:
        for msg in (await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id.in_(session_ids), MessageModel.turn_started_at.is_not(None))
        )).scalars().all():
            finals.setdefault(msg.session_id, []).append(msg)
        started = set((await db.execute(
            select(MessageModel.session_id)
            .where(MessageModel.session_id.in_(session_ids), MessageModel.msg_type == "user")
            .distinct()
        )).scalars().all())

    by_template: Dict[str, TemplateUsage] = {}
    sessions_per_user: Dict[str, int] = {}
    for row in trial_rows:
        usage = by_template.setdefault(row.template_ref, TemplateUsage(ref=row.template_ref, title=row.template_title))
        usage.sessions += 1
        sessions_per_user[row.user_id] = sessions_per_user.get(row.user_id, 0) + 1
        messages = finals.get(row.session_id, [])
        if messages:
            usage.completed += 1
            report.completed += 1
        elif row.session_id in started:
            usage.unfinished += 1
        usage.turns += len(messages)
        for msg in messages:
            begin, end = aware(msg.turn_started_at), aware(msg.finished_at)
            if begin and end:
                usage.seconds_total += max(0.0, (end - begin).total_seconds())
                usage.seconds_count += 1

    feedback_rows = (await db.execute(
        select(TrialFeedbackModel, TrialUserModel.name)
        .join(TrialUserModel, TrialUserModel.id == TrialFeedbackModel.user_id)
        .order_by(TrialFeedbackModel.updated_at.desc())
    )).all()
    titles = {ref: usage.title for ref, usage in by_template.items()}
    for feedback, user_name in feedback_rows:
        usage = by_template.get(feedback.template_ref)
        if usage is not None:
            if feedback.rating > 0:
                usage.up += 1
            elif feedback.rating < 0:
                usage.down += 1
        if feedback.comment and len(report.feedback) < feedback_limit:
            report.feedback.append(FeedbackNote(
                when=aware(feedback.updated_at), user=user_name,
                template=titles.get(feedback.template_ref, feedback.template_ref),
                rating=feedback.rating, comment=feedback.comment, session_id=feedback.session_id,
            ))

    users = (await db.execute(select(TrialUserModel).order_by(TrialUserModel.created_at))).scalars().all()
    report.users = len(users)
    report.people = sorted(
        (
            UserUsage(
                user_id=u.id, name=u.name, sessions=sessions_per_user.get(u.id, 0),
                last_login_at=aware(u.last_login_at), locked_until=aware(u.locked_until),
                has_pin=bool(u.pin_hash),
            )
            for u in users
        ),
        key=lambda u: (u.last_login_at is None, -(u.last_login_at.timestamp() if u.last_login_at else 0)),
    )
    report.sessions = len(trial_rows)
    report.templates = sorted(by_template.values(), key=lambda t: -t.sessions)
    return report
