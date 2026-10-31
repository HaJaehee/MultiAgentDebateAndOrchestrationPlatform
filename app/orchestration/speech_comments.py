"""발언 인라인 코멘트 관리 모듈 (ADR-036).

일반 개입 메모는 전체 에이전트가 공유하지만, 특정 발언 문장에 작성된 인라인 코멘트는
**해당 발언을 작성한 에이전트의 다음 발언 턴에만 선별 주입**됩니다. 타 에이전트의 맥락 오염과 불필요한 논점 일탈을 방지합니다.

전달 상태는 DB 트랜잭션을 통해 신뢰성 있게 관리됩니다. 에이전트 발언 시작 시 대기 중인 코멘트를 조회하여 프롬프트 말미에 주입하고
(`SpeechRunner.speak`), 발언 완료 트랜잭션 내에서 전달 완료로 기록합니다.
발언 실패나 서버 중단 시에도 코멘트는 대기 상태로 안전하게 보존되어 후속 발언에 정상 반영됩니다.
"""

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import delete, select, update

from app.database.models import (
    COMMENT_DELIVERED,
    COMMENT_PENDING,
    MessageModel,
    SpeechCommentModel,
    utc_now,
)

MAX_QUOTE_CHARS = 1000
MAX_COMMENT_CHARS = 4000

# 코멘트를 받을 수 없는 발언자. 에이전트가 아니므로 전달할 곳이 없습니다.
_NOT_AGENTS = ("user", "system")


class SpeechCommentError(ValueError):
    """코멘트를 달 수 없습니다. 문구는 화면에 그대로 나갑니다."""


def commentable(msg: Dict[str, Any]) -> bool:
    """코멘트를 달 수 있는 발언인가. 실패 안내에는 고칠 글이 없습니다."""
    return (
        msg.get("sender_key") not in _NOT_AGENTS
        and msg.get("msg_type") not in ("user", "system", "error")
    )


def describe(row: SpeechCommentModel) -> Dict[str, Any]:
    """화면이 그리는 모양."""
    return {
        "id": row.id, "message_id": row.message_id, "agent_key": row.agent_key,
        "round_number": row.round_number, "quote": row.quote, "comment": row.comment,
        "status": row.status, "delivered_message_id": row.delivered_message_id,
    }


async def add_comment(db, session_id: str, message_id: str, quote: str, comment: str) -> Dict[str, Any]:
    """코멘트를 저장합니다. 진행 중인 턴에도 그 에이전트의 다음 발언부터 실립니다."""
    text = (comment or "").strip()
    if not text:
        raise SpeechCommentError("코멘트 내용을 적어 주십시오.")
    message = await db.get(MessageModel, message_id)
    if message is None or message.session_id != session_id:
        raise SpeechCommentError("코멘트를 달 발언을 찾지 못했습니다.")
    if not commentable({"sender_key": message.sender_key, "msg_type": message.msg_type}):
        raise SpeechCommentError("에이전트의 발언에만 코멘트를 달 수 있습니다.")
    row = SpeechCommentModel(
        session_id=session_id, message_id=message_id, agent_key=message.sender_key,
        round_number=message.round_number, quote=(quote or "").strip()[:MAX_QUOTE_CHARS],
        comment=text[:MAX_COMMENT_CHARS], status=COMMENT_PENDING,
    )
    db.add(row)
    await db.commit()
    return describe(row)


async def delete_pending(db, session_id: str, comment_id: str) -> bool:
    """아직 전달되지 않은 코멘트를 지웁니다. 이미 전달됐거나 없으면 False."""
    result = await db.execute(delete(SpeechCommentModel).where(
        SpeechCommentModel.id == comment_id,
        SpeechCommentModel.session_id == session_id,
        SpeechCommentModel.status == COMMENT_PENDING,
    ))
    await db.commit()
    return bool(result.rowcount)


async def comments_by_message(db, session_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """이 대화의 코멘트 전부 {발언 id: 코멘트들} (단 순서)."""
    rows = (await db.execute(
        select(SpeechCommentModel)
        .where(SpeechCommentModel.session_id == session_id)
        .order_by(SpeechCommentModel.created_at)
    )).scalars().all()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.message_id, []).append(describe(row))
    return grouped


async def pending_for(
    db, session_id: str, agent_key: str, *, message_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """이 에이전트에게 아직 전달되지 않은 코멘트. `message_id` 를 주면 그 발언에 달린 것만."""
    stmt = select(SpeechCommentModel).where(
        SpeechCommentModel.session_id == session_id,
        SpeechCommentModel.agent_key == agent_key,
        SpeechCommentModel.status == COMMENT_PENDING,
    )
    if message_id is not None:
        stmt = stmt.where(SpeechCommentModel.message_id == message_id)
    rows = (await db.execute(stmt.order_by(SpeechCommentModel.created_at))).scalars().all()
    return [describe(row) for row in rows]


async def mark_delivered(db, comment_ids: Sequence[str], message_id: str) -> None:
    """전달됨으로 적습니다. 커밋은 발언 기록과 함께 합니다."""
    if not comment_ids:
        return
    await db.execute(
        update(SpeechCommentModel)
        .where(SpeechCommentModel.id.in_(list(comment_ids)), SpeechCommentModel.status == COMMENT_PENDING)
        .values(status=COMMENT_DELIVERED, delivered_message_id=message_id, delivered_at=utc_now())
    )


def comment_block(comments: Sequence[Dict[str, Any]]) -> str:
    """프롬프트에 붙일 코멘트 묶음."""
    lines = [
        "[유저의 인라인 코멘트] 유저가 당신의 앞선 발언을 읽고 아래 부분에 의견을 달았습니다. 다른 참여자에게는 "
        "보이지 않는 당신만의 피드백입니다. 이번 발언에서 직접 반영하고, 어떻게 반영했는지 짧게 밝히세요."
    ]
    for item in comments:
        round_number = int(item.get("round_number") or 0)
        where = f"Round {round_number} 발언" if round_number else "앞선 발언"
        quote = str(item.get("quote") or "").strip()
        if quote:
            quoted = "\n".join(f"> {line}" for line in quote.splitlines())
            lines.append(f"- {where}의 다음 부분에 대해:\n{quoted}\n  → {item.get('comment')}")
        else:
            lines.append(f"- {where}에 대해: {item.get('comment')}")
    return "\n".join(lines)


def with_comments(
    prompt_messages: List[Dict[str, Any]], comments: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """코멘트를 프롬프트의 마지막 유저 메시지에 덧붙인 사본. 글이 아닌 메시지로 끝나면 한 통 더 붙입니다."""
    if not comments:
        return prompt_messages
    block = comment_block(comments)
    messages = list(prompt_messages)
    last = messages[-1] if messages else None
    if last is not None and last.get("role") == "user" and isinstance(last.get("content"), str):
        messages[-1] = {**last, "content": f"{last['content']}\n\n{block}"}
    else:
        messages.append({"role": "user", "content": block})
    return messages
