"""보안 감사 조회: 규칙·모드·고정 보호가 막은 도구 호출을 대화를 가로질러 모읍니다.

읽기만 합니다. 기록은 `tool_calls` 의 판정 칸(`decision`·`risk`·`rule`)이고, 한 줄로 옮기는 말은
채팅 피드와 저장 문서가 쓰는 `describe_verdict` 그대로입니다. 그래서 같은 호출이 어디서든 같게 읽힙니다.

모이는 것은 **설정이 막은 호출**입니다. 사람이 카드에서 거부했거나 답하지 않은 호출(`rejected`·`timeout`)은
결정한 쪽이 사람이므로 넣지 않습니다. 문서 형식 거부도 `decision = hard` 로 적히지만 보안 판정이 아니라
모델이 다른 도구로 고칠 형식 문제여서 뺍니다 (`status = error`, 보안 거부는 `status = denied`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

from sqlalchemy import Text, cast, func, select

from app.database.models import SessionModel, ToolCallRecordModel
from app.mcp.policy import describe_verdict

AUDIT_DECISIONS = ("deny", "hard")

# 한 번에 읽는 줄 수. '더 보기' 로 이만큼씩 늘립니다.
AUDIT_PAGE_SIZE = 100

# 줄마다 들고 있을 인자의 상한(문자). `write_file` 의 인자에는 파일 내용이 통째로 들어 있습니다.
ARGUMENTS_TEXT_LIMIT = 4000


@dataclass(frozen=True)
class BlockedCall:
    """막힌 호출 한 줄. 화면은 이 값만 봅니다."""

    id: str
    session_id: str
    session_title: str
    message_id: Optional[str]
    agent_key: str
    tool_name: str
    arguments_text: str
    decision: str
    risk: str
    rule: str
    verdict: str
    refusal: str
    created_at: datetime


def _arguments_text(arguments: Any) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) <= ARGUMENTS_TEXT_LIMIT:
        return text
    return f"{text[:ARGUMENTS_TEXT_LIMIT]}… (총 {len(text):,}자)"


async def blocked_tool_calls(
    session_factory: Callable[[], Any],
    *,
    session_id: Optional[str] = None,
    decision: str = "",
    limit: int = AUDIT_PAGE_SIZE,
) -> Tuple[List[BlockedCall], int]:
    """최근 것부터 막힌 호출과 그 전체 건수. `session_id` 를 주면 그 대화만, `decision` 을 주면 그 판정만.

    갈라 낸 대화는 원본의 기록을 시각까지 그대로 복사합니다 (`app.session_fork`). 같은 호출이 대화마다
    따로 세어지면 한 번 막힌 것이 두 번으로 읽히므로, 복사본은 가장 먼저 만들어진 대화의 것만 남깁니다.
    순위를 대화 단위로 매기는 이유: 시각 해상도가 낮은 환경에서는 한 대화의 연속 호출도 시각이 같을 수 있는데,
    그것은 복사본이 아니라 따로 막힌 호출입니다.
    """
    decisions = (decision,) if decision else AUDIT_DECISIONS
    if not set(decisions) <= set(AUDIT_DECISIONS):
        raise ValueError(f"'{decision}' 는 감사 대상 판정이 아닙니다 ({' · '.join(AUDIT_DECISIONS)}).")

    call = ToolCallRecordModel
    # `SessionModel` 은 통째로 읽지 않습니다. 관계가 selectin 이라 대화 내용까지 딸려 옵니다.
    ranked = select(
        call.id, call.session_id, SessionModel.title.label("session_title"), call.message_id, call.agent_key,
        call.tool_name, call.arguments, call.decision, call.risk, call.rule, call.output, call.created_at,
        func.dense_rank().over(
            # PostgreSQL 의 json 타입에는 같음 비교가 없어 글자로 견줍니다. 복사본은 같은 직렬화기로 적혀 글자까지 같습니다.
            partition_by=(call.agent_key, call.tool_name, cast(call.arguments, Text), call.created_at),
            order_by=(SessionModel.created_at, SessionModel.id),
        ).label("session_rank"),
    ).join(SessionModel, SessionModel.id == call.session_id).where(
        call.decision.in_(decisions), call.status == "denied",
    )
    if session_id:
        ranked = ranked.where(call.session_id == session_id)
    ranked = ranked.subquery()
    originals = ranked.c.session_rank == 1

    async with session_factory() as db:
        total = (await db.execute(select(func.count()).select_from(ranked).where(originals))).scalar_one()
        rows = (await db.execute(
            select(ranked).where(originals).order_by(ranked.c.created_at.desc(), ranked.c.id).limit(limit)
        )).all()

    return [
        BlockedCall(
            id=row.id,
            session_id=row.session_id,
            session_title=row.session_title or "",
            message_id=row.message_id,
            agent_key=row.agent_key,
            tool_name=row.tool_name,
            arguments_text=_arguments_text(row.arguments),
            decision=row.decision,
            risk=row.risk,
            rule=row.rule,
            verdict=describe_verdict({"decision": row.decision, "rule": row.rule}),
            refusal=row.output or "",
            created_at=row.created_at,
        )
        for row in rows
    ], int(total)
