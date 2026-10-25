"""대화 세션 분기(Fork): 특정 턴 시점의 상태를 기반으로 새 세션을 생성하고 전략·참여자·모델을 변경하여 재실행 (v1.5).

'새 대화로 이어가기'(`session_ops.continue_session`)가 컨텍스트 정리를 위해 과거 발언을 비우고 결론 요약 및 지식 그래프만
계승하는 반면, 세션 분기(Fork)는 **해당 턴 시점까지의 발언 및 작업 공간 기록 전체**를 그대로 복제합니다.
동일한 분기점에서 토론 전략, 모델, 참여자를 변경했을 때의 결과를 비교 분석하기 위한 기능이며, `/compare` 페이지에서
두 세션을 좌우로 나란히 비교할 수 있습니다.

복제 대상 (해당 턴 시점까지):

* 발언 메시지, 도구 실행 기록, 아티팩트, 턴 기록 — 새로운 고유 ID가 부여되며 상호 참조 ID(계획 승인 대상 등)도 자동 갱신됩니다.
* 해당 턴 완료 시점의 대화 기억(결정 장부 및 요약, `TurnModel.memory`). 마지막 턴인 경우 세션의 최신 기억을 사용합니다.
* 에이전트 구성 스냅샷 — 페르소나 잠금이 해제된 상태로 복제되어 다음 턴 시작 전에 모델 및 구성을 변경할 수 있습니다.
* 작업 공간 — 해당 턴 완료 시점의 스냅샷(`app/workspace_checkpoint.export`)을 기반으로 **별도의 독립 디렉터리**를 생성합니다.
  동일 파일 동시 수정으로 인한 충돌을 방지합니다. 스냅샷이 없는 경우 현재 디렉터리를 복사합니다.
* 지식 그래프 — 분기된 새 작업 공간 디렉터리로 복사됩니다.

복제 제외 대상: 대상 턴 이후의 후속 기록, 미완료 발언 초안, 세션별 임시 도구 허용 규칙(새 세션에서 재확인),
기존 턴 체크포인트(새 작업 공간 디렉터리에는 과거 Git ref가 없으므로 롤백 불가).
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
    # 작업 공간을 해당 턴의 스냅샷으로 생성했는지 여부 (False인 경우 현재 디렉터리를 복사)
    from_snapshot: bool
    messages: int
    graph_copied: bool


def _columns(row: Any) -> Dict[str, Any]:
    return {c.key: getattr(row, c.key) for c in type(row).__table__.columns}


def _remap(value: Any, ids: Dict[str, str]) -> Any:
    """데이터 구조 내에서 이전 ID를 참조하는 문자열을 새 ID로 재귀적으로 치환합니다."""
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
    """`turn_id` 시점까지의 대화 세션을 새 세션으로 분기 생성하고 DB에 커밋합니다."""
    source = await db.get(SessionModel, source_session_id)
    if source is None:
        raise ForkError("원본 대화 세션을 찾을 수 없습니다.")
    turns = (await db.execute(
        select(TurnModel).where(TurnModel.session_id == source_session_id).order_by(TurnModel.started_at)
    )).scalars().all()
    cut = next((i for i, t in enumerate(turns) if t.id == turn_id), None)
    if cut is None:
        raise ForkError("분기 대상 턴을 찾을 수 없습니다.")
    turn = turns[cut]
    if turn.status in TURN_UNFINISHED:
        raise ForkError("진행 중이거나 비정상 종료된 턴에서는 세션을 분기할 수 없습니다. 턴 완료 후 다시 시도해 주세요.")
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
            # 작업 공간 롤백 메모는 원본 작업 공간의 내역이므로 분기 세션에는 계승하지 않습니다.
            and not (m.turn_meta or {}).get("reverted_turn"))
    ]
    kept_message_ids = {m.id for m in kept_messages}

    new_id = str(uuid.uuid4())
    ids: Dict[str, str] = {source_session_id: new_id}
    ids.update({t.id: str(uuid.uuid4()) for t in kept_turns})
    ids.update({m.id: str(uuid.uuid4()) for m in kept_messages})

    # 작업 공간: 해당 턴 완료 시점의 스냅샷으로 독립된 디렉터리를 생성합니다.
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
    """비교 화면(/compare) 표시용 세션 요약 데이터 구성: 설정, 최종 턴 결론, 결정 장부, 사용량, 평가."""
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
    """턴에 대한 사용자의 정량 평가(👍 1, 👎 -1, 초기화 0) 및 피드백 메모를 저장합니다."""
    turn = await db.get(TurnModel, turn_id)
    if turn is None:
        return False
    turn.rating = None if score == 0 else {
        "score": 1 if score > 0 else -1, "note": (note or "").strip()[:500], "at": utc_now().isoformat(),
    }
    await db.commit()
    return True
