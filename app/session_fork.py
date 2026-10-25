"""대화 분기: 한 턴에서 갈라 새 대화를 만들고, 거기서 전략·참여자·모델을 바꿔 다시 돌려 봅니다 (v1.5).

`새 대화로 이어가기`(`session_ops.continue_session`)는 기록을 비우고 결론만 넘깁니다 — 컨텍스트가 찼을 때의
길입니다. 분기는 반대로 **그 턴까지의 기록을 통째로** 가져갑니다. 같은 지점에서 다른 선택을 했다면 어떻게
됐을지 보는 길이고, 두 대화는 비교 화면(`/compare`)에서 나란히 봅니다.

가져가는 것 (그 턴까지):

* 발언·도구 기록·산출물·턴 기록 — id 를 새로 매기고, 기록 안에서 서로를 가리키는 id(계획 승인이 가리키는 계획,
  기억의 반영 지점 등)도 함께 바꿉니다.
* 그 턴이 끝났을 때의 대화 기억(결정 장부·요약, `TurnModel.memory`). 마지막 턴이면 대화의 기억을 씁니다.
* 에이전트 구성 초안 — 이어가기와 같습니다. 잠금은 풀어 두어, 다음 턴 전에 모델·참여자를 바꿀 수 있습니다.
* 작업 공간 — 그 턴이 끝났을 때의 스냅샷(`app/workspace_checkpoint.export`)으로 **따로 된 폴더**를 만듭니다.
  두 대화가 같은 파일을 고치면 비교가 되지 않기 때문입니다. 스냅샷이 없으면(git 없음, 이 기능 이전의 턴)
  지금 모습을 복사하고 그렇게 했다고 알립니다.
* 지식 그래프 — 새 작업 공간의 그래프 폴더로 복사합니다.

가져가지 않는 것: 그 턴 뒤의 기록, 끝나지 못한 발언의 초안, "이 대화에서 허용" 한 도구 규칙(새 대화에서 다시
묻습니다), 갈라 간 턴들의 체크포인트(새 작업 공간에는 그 기록이 없으므로 되돌릴 수 없습니다).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from sqlalchemy import select

from app import workspace_checkpoint
from app.config import resolve_workspace_dir
from app.database.models import (
    TURN_UNFINISHED,
    ArtifactModel,
    MessageModel,
    SessionAgentModel,
    SessionModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
# 비교 화면은 화면 없는 길(`app/headless.py`)과 같은 결과 모양을 씁니다.
from app.headless import turn_result
from app.mcp.manager import memory_graph_dir

logger = logging.getLogger(__name__)

# 이어가기의 " (이어서)" 와 같은 모양입니다.
FORK_TITLE_SUFFIX = " (갈래)"


class ForkError(ValueError):
    pass


@dataclass
class ForkResult:
    session_id: str
    title: str
    workspace: str
    # 작업 공간을 그 턴의 스냅샷으로 만들었는지 (False 면 지금 모습을 복사했습니다)
    from_snapshot: bool
    messages: int
    graph_copied: bool


def _columns(row: Any) -> Dict[str, Any]:
    return {c.key: getattr(row, c.key) for c in type(row).__table__.columns}


def _remap(value: Any, ids: Dict[str, str]) -> Any:
    """기록 안에서 옛 id 를 가리키는 문자열을 새 id 로 바꿉니다 (사전·목록 안까지)."""
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, dict):
        return {k: _remap(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, ids) for v in value]
    return value


def _fork_folder(source: Path, new_id: str) -> Path:
    return source.parent / f"{source.name}-fork-{new_id[:8]}"


def _copy_graph(source_ws: Path, source_sid: str, target_ws: Path, target_sid: str) -> bool:
    source_dir, target_dir = memory_graph_dir(source_ws), memory_graph_dir(target_ws)
    if source_dir is None or target_dir is None:
        return False
    source = source_dir / f"{source_sid}.jsonl"
    if not source.is_file() or source.stat().st_size == 0:
        return False
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_dir / f"{target_sid}.jsonl")
    except OSError as exc:
        logger.warning(f"Could not copy the knowledge graph for a fork: {exc}")
        return False
    return True


async def fork_session(db, source_session_id: str, turn_id: str) -> ForkResult:
    """`turn_id` 까지의 대화를 새 대화로 갈라 냅니다. 커밋까지 합니다."""
    source = await db.get(SessionModel, source_session_id)
    if source is None:
        raise ForkError("원본 대화를 찾지 못했습니다.")
    turns = (await db.execute(
        select(TurnModel).where(TurnModel.session_id == source_session_id).order_by(TurnModel.started_at)
    )).scalars().all()
    cut = next((i for i, t in enumerate(turns) if t.id == turn_id), None)
    if cut is None:
        raise ForkError("갈라 낼 턴을 찾지 못했습니다.")
    turn = turns[cut]
    if turn.status in TURN_UNFINISHED:
        raise ForkError("끝나지 않은 턴에서는 갈라 낼 수 없습니다. 턴이 끝난 뒤에 갈라 주십시오.")
    kept_turns = turns[:cut + 1]
    kept_turn_ids: Set[str] = {t.id for t in kept_turns}
    is_latest = cut == len(turns) - 1

    messages = (await db.execute(
        select(MessageModel).where(MessageModel.session_id == source_session_id).order_by(MessageModel.created_at)
    )).scalars().all()
    # 다음 턴이 시작되기 전까지가 이 턴의 몫입니다 (산출물은 결론 발언보다 조금 뒤에 기록됩니다).
    boundary = None if is_latest else turns[cut + 1].started_at

    def before_boundary(at: Any) -> bool:
        return boundary is None or (at is not None and at < boundary)

    kept_messages = [
        m for m in messages
        if (m.turn_id in kept_turn_ids)
        or (m.turn_id is None and before_boundary(m.created_at)
            # 작업 공간 되돌리기 안내는 원본 폴더의 일입니다. 갈래의 작업 공간은 그 턴이 끝났을 때의 모습입니다.
            and not (m.turn_meta or {}).get("reverted_turn"))
    ]
    kept_message_ids = {m.id for m in kept_messages}

    new_id = str(uuid.uuid4())
    ids: Dict[str, str] = {source_session_id: new_id}
    ids.update({t.id: str(uuid.uuid4()) for t in kept_turns})
    ids.update({m.id: str(uuid.uuid4()) for m in kept_messages})

    # 작업 공간: 그 턴이 끝났을 때의 스냅샷으로 따로 된 폴더를 만듭니다.
    source_ws = resolve_workspace_dir(source.workspace_dir or None)
    target_ws = _fork_folder(source_ws, new_id)
    end_ref = workspace_checkpoint.turn_ref(turn.id, workspace_checkpoint.END)
    from_snapshot = await asyncio.to_thread(workspace_checkpoint.export, source_ws, end_ref, target_ws)
    if not from_snapshot and not await asyncio.to_thread(workspace_checkpoint.copy_folder, source_ws, target_ws):
        target_ws.mkdir(parents=True, exist_ok=True)

    remembered = (
        {
            "decision_ledger": source.decision_ledger or "",
            "ledger_through_id": source.ledger_through_id,
            "transcript_summary": source.transcript_summary or "",
            "summary_through_id": source.summary_through_id,
        }
        if is_latest else dict(turn.memory or {})
    )
    title = (source.title or "Untitled Debate")
    if not title.endswith(FORK_TITLE_SUFFIX):
        title += FORK_TITLE_SUFFIX

    session_fields = _columns(source)
    session_fields.update(
        id=new_id,
        title=title,
        decision_ledger=remembered.get("decision_ledger") or "",
        ledger_through_id=ids.get(remembered.get("ledger_through_id") or "") or None,
        transcript_summary=remembered.get("transcript_summary") or "",
        summary_through_id=ids.get(remembered.get("summary_through_id") or "") or None,
        workspace_dir=str(target_ws),
        # 아직 다음 턴 전입니다. 모델·참여자를 바꿀 수 있게 잠금을 풉니다 (다음 턴에 다시 고정됩니다).
        personas_locked=False,
        # "이 대화에서 허용·거부" 는 원본에서 사람이 본 호출에 대한 답입니다. 새 대화에서 다시 묻습니다.
        tool_grants=[],
        tool_denials=[],
        forked_from={"session_id": source_session_id, "turn_id": turn_id, "title": source.title or ""},
    )
    for key in ("created_at", "updated_at"):
        session_fields.pop(key, None)
    db.add(SessionModel(**session_fields))

    for t in kept_turns:
        fields = _remap(_columns(t), ids)
        fields.update(id=ids[t.id], session_id=new_id, checkpoint=None)
        db.add(TurnModel(**fields))
    for m in kept_messages:
        fields = _columns(m)
        fields.update(
            id=ids[m.id], session_id=new_id, turn_id=ids.get(m.turn_id) if m.turn_id else None,
            turn_meta=_remap(m.turn_meta, ids),
        )
        db.add(MessageModel(**fields))

    tool_rows = (await db.execute(
        select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == source_session_id)
    )).scalars().all()
    for row in tool_rows:
        if row.message_id not in kept_message_ids and row.turn_id not in kept_turn_ids:
            continue
        fields = _columns(row)
        fields.update(
            id=str(uuid.uuid4()), session_id=new_id,
            message_id=ids.get(row.message_id) if row.message_id else None,
            turn_id=ids.get(row.turn_id) if row.turn_id else None,
        )
        db.add(ToolCallRecordModel(**fields))

    artifact_rows = (await db.execute(
        select(ArtifactModel).where(ArtifactModel.session_id == source_session_id)
    )).scalars().all()
    for row in artifact_rows:
        if not before_boundary(row.created_at):
            continue
        fields = _columns(row)
        fields.update(id=str(uuid.uuid4()), session_id=new_id)
        db.add(ArtifactModel(**fields))

    for row in (await db.execute(
        select(SessionAgentModel).where(SessionAgentModel.session_id == source_session_id)
    )).scalars().all():
        fields = _columns(row)
        fields.update(id=str(uuid.uuid4()), session_id=new_id)
        for key in ("created_at", "updated_at"):
            fields.pop(key, None)
        db.add(SessionAgentModel(**fields))

    await db.commit()
    graph_copied = await asyncio.to_thread(_copy_graph, source_ws, source_session_id, target_ws, new_id)
    return ForkResult(
        session_id=new_id, title=title, workspace=str(target_ws), from_snapshot=from_snapshot,
        messages=len(kept_messages), graph_copied=graph_copied,
    )


async def compare_view(db, session_ids: Iterable[str]) -> List[Dict[str, Any]]:
    """비교 화면이 그리는 대화별 요약: 설정, 마지막 턴의 결론·장부·사용량·평가."""
    out: List[Dict[str, Any]] = []
    for sid in session_ids:
        session = await db.get(SessionModel, sid)
        if session is None:
            continue
        result = await turn_result(sid)
        turn = await db.get(TurnModel, result["turn_id"]) if result.get("turn_id") else None
        out.append({
            "session_id": sid,
            "title": session.title,
            "strategy": session.strategy,
            "max_rounds": session.max_rounds,
            "agents": list(session.active_agents or []),
            "forked_from": session.forked_from,
            "rating": turn.rating if turn is not None else None,
            **{k: result.get(k) for k in (
                "turn_id", "status", "request", "conclusion", "decision_ledger", "usage", "ended_early",
                "files_changed", "failed_agents", "started_at", "finished_at",
            )},
        })
    return out


async def rate_turn(db, turn_id: str, score: int, note: str = "") -> bool:
    """사람의 평가를 턴에 남깁니다 (👍 1 · 👎 -1 · 지우기 0)."""
    turn = await db.get(TurnModel, turn_id)
    if turn is None:
        return False
    turn.rating = None if score == 0 else {
        "score": 1 if score > 0 else -1, "note": (note or "").strip()[:500], "at": utc_now().isoformat(),
    }
    await db.commit()
    return True
