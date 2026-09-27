"""대화를 되돌리고, 이어받는 작업.

토론은 한번 시작하면 끝까지 가는 것이 원래 설계였습니다. 정지(`TurnControl`)조차
"남은 라운드를 건너뛰고 지금까지의 것으로 합성하라" 는 뜻이라, 요청 자체가 틀렸을
때 — 오타, 잘못 붙여넣은 글, 다른 대화에 보낼 뻔한 요청 — 할 수 있는 것이 없었습니다.
합성이 끝날 때까지 기다렸다가, 틀린 요청과 그에 답한 발언들을 기록에 남긴 채 다시
쓰는 것뿐이었습니다. 그 기록은 다음 턴의 맥락으로 계속 따라다닙니다.

여기서는 그 턴이 남긴 것을 지웁니다. 지우는 범위는 **그 턴이 만든 것**뿐입니다.
앞선 턴의 발언은 그대로 둡니다 — 사람이 고치려는 것은 방금 보낸 요청이지 대화
전체가 아닙니다.

반대 방향의 문제도 있습니다. 라운드가 쌓여 컨텍스트가 가득 차면 에이전트들이
헛돌기 시작하는데, 그때 할 수 있는 것은 새 대화를 여는 것뿐이었습니다. 그러면
작업 공간은 다시 지정해야 하고, 지식 그래프는 대화 단위로 나뉘어 있어 통째로
사라졌습니다 (`memory` 서버는 `_meta.conversationId` 로 그래프를 가르고, 모델이
`graph_id` 를 적어도 메타가 이깁니다 — 남의 그래프를 열지 못하게 하려는 것이라
"이전 대화를 읽어라" 는 지시도 닿지 않습니다).

`continue_session()` 이 그 자리를 메웁니다. **새로 만드는 것은 컨텍스트뿐**이고
작업 공간·지식 그래프·에이전트 구성·이전 결론은 따라옵니다.
"""

import logging
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import delete, func, select

from app.database.models import (
    ArtifactModel,
    MessageModel,
    SessionAgentModel,
    SessionModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.mcp.manager import carry_over_memory_graph

logger = logging.getLogger(__name__)

# 인수인계 쪽지에 실을 이전 결론의 상한(문자).
#
# 새 대화를 여는 이유가 컨텍스트 포화인데, 그 첫 발언이 이전 대화를 통째로
# 들고 오면 시작하자마자 같은 자리로 돌아갑니다. 결론만, 그것도 잘라서 옮기고
# 나머지는 작업 공간의 파일과 이어받은 지식 그래프에 맡깁니다.
HANDOFF_SYNTHESIS_LIMIT = 8000

# 새 대화 제목에 붙는 꼬리표. 목록에서 원본과 이어받은 것을 구분합니다.
HANDOFF_TITLE_SUFFIX = " (이어서)"


def _clean(ids: Iterable[str]) -> List[str]:
    """빈 값과 중복을 걷어냅니다 (스트리밍 중 기록은 id 가 비어 있을 수 있습니다)."""
    seen: List[str] = []
    for value in ids or ():
        if value and value not in seen:
            seen.append(value)
    return seen


async def discard_turn(
    db,
    session_id: str,
    message_ids: Sequence[str],
    artifact_ids: Sequence[str] = (),
    turn_id: Optional[str] = None,
) -> bool:
    """한 턴이 남긴 발언·도구 기록·산출물을 지웁니다.

    `turn_id` 를 주면 그 턴의 기록(`TurnModel`)과 그 턴에 딸린 발언·도구 기록을 **전부**
    지웁니다. 끊긴 턴을 버릴 때는 지울 발언을 화면의 스냅샷이 아니라 기록이 압니다 — 끝나지
    못한 발언이 남긴 도구 기록(발언 id 가 빈 것)도 여기서 함께 사라집니다.

    돌려주는 값은 **이 대화가 시작 전 상태로 돌아갔는지**입니다. 남은 발언이
    하나도 없으면 페르소나 잠금을 풀어 줍니다 — 첫 요청을 지웠다면 이 대화는
    아직 시작하지 않은 것이고, 그렇다면 에이전트 구성도 다시 만질 수 있어야
    말이 맞습니다. 굳혀 둔 구성 스냅샷(`session_agents`)은 지우지 않습니다.
    다음 턴이 시작될 때 그 시점의 `conf.json` 으로 다시 굳고 다시 잠깁니다.

    도구 기록을 먼저 지웁니다. `messages.id` 를 가리키는 행이라, 발언을 먼저
    지우면 아무도 가리키지 않는 기록이 남습니다 (SQLite 가 외래키를 검사하지
    않아 조용히 남을 뿐입니다).
    """
    if turn_id:
        await db.execute(delete(ToolCallRecordModel).where(
            ToolCallRecordModel.turn_id == turn_id,
            ToolCallRecordModel.session_id == session_id,
        ))
        await db.execute(delete(MessageModel).where(
            MessageModel.turn_id == turn_id,
            MessageModel.session_id == session_id,
        ))
        await db.execute(delete(TurnModel).where(
            TurnModel.id == turn_id,
            TurnModel.session_id == session_id,
        ))

    ids = _clean(message_ids)
    if ids:
        await db.execute(
            delete(ToolCallRecordModel).where(ToolCallRecordModel.message_id.in_(ids))
        )
        await db.execute(
            delete(MessageModel).where(
                MessageModel.id.in_(ids),
                MessageModel.session_id == session_id,
            )
        )

    art_ids = _clean(artifact_ids)
    if art_ids:
        await db.execute(
            delete(ArtifactModel).where(
                ArtifactModel.id.in_(art_ids),
                ArtifactModel.session_id == session_id,
            )
        )

    remaining = await db.scalar(
        select(func.count())
        .select_from(MessageModel)
        .where(MessageModel.session_id == session_id)
    )
    started_over = not remaining

    if started_over:
        session = await db.get(SessionModel, session_id)
        if session is not None and session.personas_locked:
            session.personas_locked = False

    await db.commit()
    logger.info(
        f"Discarded a turn from session {session_id}: "
        f"{len(ids)} message(s), {len(art_ids)} artifact(s)"
        + (" — the conversation is back to 'not started'" if started_over else "")
    )
    return started_over


# ---------------------------------------------------------------------------
# 세션 이어받기
# ---------------------------------------------------------------------------


def _trim(text: str, limit: int = HANDOFF_SYNTHESIS_LIMIT) -> str:
    """앞부분만 남깁니다. 최종 합성은 결론이 앞에, 부록이 뒤에 옵니다."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n... (이전 결론이 길어 {len(text) - limit:,}자를 줄였습니다. 전문은 원본 대화에 있습니다.)"


def build_handoff_note(
    *,
    source_title: str,
    synthesis: str,
    artifact_titles: Sequence[str],
    workspace: str,
    memory_carried: bool,
    message_count: int,
) -> str:
    """새 대화의 첫 발언으로 들어갈 인수인계 쪽지.

    무엇을 물려받았고 무엇을 물려받지 못했는지 **정확히** 적습니다. 이어받기가
    반쪽만 성공했는데 그 사실이 어디에도 없으면, 에이전트는 없는 지식 그래프를
    조회하다 빈손으로 돌아와 그냥 지어내기 시작합니다.
    """
    lines = [
        "## 이전 세션 인수인계",
        "",
        f"- 원본 대화: **{source_title or '제목 없음'}** (발언 {message_count}건)",
        f"- 작업 공간: `{workspace or '기본값'}` — 이전 세션이 만든 파일과 git 기록이 그대로 있습니다.",
    ]
    if memory_carried:
        lines.append(
            "- 지식 그래프: **이어받았습니다.** `memory` 도구로 이전 세션이 기록한 "
            "사실을 그대로 조회할 수 있습니다."
        )
    else:
        lines.append(
            "- 지식 그래프: 이어받지 못했습니다 (이전 세션이 아무것도 기록하지 않았거나 "
            "memory 서버가 꺼져 있습니다). 없는 기억을 있다고 가정하지 마세요."
        )
    if artifact_titles:
        lines.append("- 이전 산출물: " + ", ".join(f"`{t}`" for t in artifact_titles))

    lines += ["", "### 이전 세션의 최종 결론", ""]
    lines.append(_trim(synthesis) if synthesis.strip() else "(최종 합성까지 가지 못한 대화입니다.)")
    lines += [
        "",
        "---",
        "",
        "위 결론은 **이미 합의된 것**입니다. 처음부터 다시 논쟁하지 말고 그 위에서 이어가세요. "
        "확인이 필요한 것은 기억에 의존하지 말고 `filesystem`·`git` 도구로 작업 공간을 직접 보고, "
        "`memory` 도구로 이어받은 지식 그래프를 조회하세요. 이 쪽지에 없는 세부는 그 두 곳에 있습니다.",
    ]
    return "\n".join(lines)


async def continue_session(
    db,
    source_session_id: str,
    *,
    orchestrator_name: str = "Master Orchestrator",
    orchestrator_role: str = "Moderator & Synthesizer",
) -> Optional[Dict[str, Any]]:
    """이전 대화를 이어받는 새 대화를 만듭니다. 원본을 찾지 못하면 None.

    컨텍스트가 가득 차 에이전트들이 헛돌기 시작할 때 쓰는 길입니다. 새로 만드는
    것은 **컨텍스트뿐**이고, 쌓아 온 것은 전부 따라옵니다.

    물려받는 것:

    * 작업 공간 — 같은 폴더. 파일과 git 기록이 그대로입니다.
    * 지식 그래프 — memory 서버의 대화별 파일을 복사합니다 (`carry_over_memory_graph`).
      경계는 호스트가 정하므로 옮기는 것도 호스트가 합니다.
    * 에이전트 구성 — 참여자, 페르소나 초안, 굳혀 둔 `config_snapshot` 까지.
      conf.json 에서 사라진 에이전트도 스냅샷으로 계속 발언합니다.
    * 전략·라운드 수·동시 실행 상한·커스텀 지침·결정 장부.
    * 이전 세션의 최종 결론 — 오케스트레이터의 첫 발언(인수인계 쪽지)으로 들어갑니다.

    물려받지 않는 것:

    * 발언 기록. 그게 비우려던 것입니다.
    * `personas_locked`. 새 대화는 아직 시작하지 않았으므로 에이전트를 다시
      만질 수 있어야 합니다 — 컨텍스트가 터진 원인이 모델 선택일 수도 있습니다.
    """
    source = await db.get(SessionModel, source_session_id)
    if source is None:
        logger.warning(f"Cannot continue a session that does not exist: {source_session_id}")
        return None

    new_id = str(uuid.uuid4())
    title = (source.title or "Untitled Debate")
    if not title.endswith(HANDOFF_TITLE_SUFFIX):
        title += HANDOFF_TITLE_SUFFIX

    db.add(SessionModel(
        id=new_id,
        title=title,
        strategy=source.strategy,
        max_rounds=source.max_rounds,
        parallel_limit=source.parallel_limit,
        active_agents=list(source.active_agents or []),
        known_agents=list(source.known_agents or []),
        custom_instructions=source.custom_instructions or "",
        # 결정 장부는 따라옵니다 — 비우려던 것은 발언 기록이지 합의된 상태가 아닙니다.
        # 반영 지점은 비워 둡니다 (새 대화에는 그 발언이 없습니다). 요약은 옛 발언을
        # 덮는 것이라 따라오지 않습니다.
        decision_ledger=source.decision_ledger or "",
        workspace_dir=source.workspace_dir or "",
        # 도구 보안 모드는 따라오고, "이 대화에서 허용" 은 따라오지 않습니다. 허락은
        # 그 대화에서 사람이 본 호출에 대한 것이라, 새 대화에서 다시 묻는 편이 맞습니다.
        tool_mode=source.tool_mode or "",
        # 아직 시작하지 않은 대화입니다. 첫 요청이 들어올 때 그 시점의 구성으로
        # 다시 굳고 다시 잠깁니다.
        personas_locked=False,
    ))

    # 페르소나와 굳혀 둔 구성을 초안으로 옮깁니다. 살아 있는 에이전트는 다음
    # 잠금 때 conf.json 의 현재 값으로 다시 굳고, conf.json 에서 사라진
    # 에이전트만 이 스냅샷으로 계속 발언합니다.
    rows = (await db.execute(
        select(SessionAgentModel).where(SessionAgentModel.session_id == source_session_id)
    )).scalars().all()
    for row in rows:
        db.add(SessionAgentModel(
            id=str(uuid.uuid4()),
            session_id=new_id,
            agent_key=row.agent_key,
            name=row.name,
            role=row.role,
            system_prompt=row.system_prompt,
            config_snapshot=row.config_snapshot,
        ))

    # 이전 세션의 최종 결론 = 마지막 오케스트레이터 발언 (실패로 끝난 것은 제외).
    synthesis = (await db.execute(
        select(MessageModel.content)
        .where(
            MessageModel.session_id == source_session_id,
            MessageModel.msg_type == "orchestrator",
        )
        .order_by(MessageModel.created_at.desc())
        .limit(1)
    )).scalar_one_or_none() or ""

    if not synthesis.strip():
        # 합성 발언이 없는 대화도 있습니다 — 합성 호출이 실패했지만 아티팩트는
        # 뽑힌 경우, 또는 밖에서 가져다 넣은 기록. 최종 보고서는 마크다운
        # 아티팩트로도 남으므로 그쪽을 봅니다. 결론을 못 넘기면 이어받기의
        # 값어치가 절반으로 줄어듭니다.
        synthesis = (await db.execute(
            select(ArtifactModel.content)
            .where(
                ArtifactModel.session_id == source_session_id,
                ArtifactModel.artifact_type == "markdown",
            )
            .order_by(ArtifactModel.created_at.desc())
            .limit(1)
        )).scalar_one_or_none() or ""

    message_count = await db.scalar(
        select(func.count()).select_from(MessageModel)
        .where(MessageModel.session_id == source_session_id)
    ) or 0

    artifact_titles = list((await db.execute(
        select(ArtifactModel.title)
        .where(ArtifactModel.session_id == source_session_id)
        .order_by(ArtifactModel.created_at)
    )).scalars().all())

    # 그래프를 못 옮겼다고 새 대화를 못 만들 이유는 없습니다. 대신 **못 옮겼다는
    # 사실이 쪽지에 적혀야** 합니다 — 조용히 빈 그래프로 시작하면 에이전트는
    # 없는 기억을 조회하다 빈손으로 돌아와 지어내기 시작합니다.
    try:
        memory_carried = carry_over_memory_graph(
            source_session_id, new_id, source.workspace_dir or ""
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Carrying the knowledge graph over failed: {type(e).__name__}: {e}")
        memory_carried = False

    note = build_handoff_note(
        source_title=source.title or "",
        synthesis=synthesis,
        artifact_titles=artifact_titles,
        workspace=source.workspace_dir or "",
        memory_carried=memory_carried,
        message_count=int(message_count),
    )

    # 오케스트레이터의 발언으로 넣습니다. 사용자가 쓴 글이 아니므로 user 로
    # 넣을 수 없고(사이드바의 '시작 시각' 도 그 행에서 옵니다), 중재자가 이번
    # 토론의 출발점을 정리해 둔 것이 이 쪽지의 성격에 맞습니다.
    # 앱이 지어 넣는 쪽지라 걸리는 시간이 없습니다 (시작 = 끝).
    noted_at = utc_now()
    db.add(MessageModel(
        id=str(uuid.uuid4()),
        session_id=new_id,
        sender_key="orchestrator",
        sender_name=orchestrator_name,
        sender_role=orchestrator_role,
        content=note,
        round_number=0,
        msg_type="orchestrator",
        started_at=noted_at,
        finished_at=noted_at,
    ))

    await db.commit()
    logger.info(
        f"Continued session {source_session_id} as {new_id} "
        f"(workspace={source.workspace_dir or 'default'}, "
        f"memory_graph={'carried' if memory_carried else 'not carried'}, "
        f"{len(rows)} persona row(s))"
    )
    return {
        "session_id": new_id,
        "title": title,
        "memory_carried": memory_carried,
        "workspace": source.workspace_dir or "",
        "source_session_id": source_session_id,
    }
