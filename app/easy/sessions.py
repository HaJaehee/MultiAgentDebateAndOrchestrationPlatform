"""과제 수행 세션 — 참여 에이전트 선택, 잠긴 세션 생성, 실행 기록 및 맞춤 에이전트 관리.

체험 템플릿(`app/trial/templates.py`의 `create_trial_session`)과 동일한 메커니즘을 사용합니다. 참여 에이전트 구성을
`session_agents.config_snapshot`에 불변 스냅샷으로 고정한 **잠긴 세션(locked session)**을 생성하여 엔진에 전달합니다 (ADR-011).
이를 통해 `conf.json`에 영구 등록되지 않은 시연 에이전트나 방문자 생성 에이전트도 세션 내에서 정상적으로 발언할 수 있습니다.

체험 세션과의 주요 차이점은 실제 도구 연결 여부입니다. 비엔지니어가 챗봇과 에이전트를 명확히 구분할 수 있도록 실제 도구를 통한
행동 및 관찰 과정을 시각화합니다:

* 소유자 — `conf.json`의 에이전트 설정을 그대로 유지하며, 도구 보안 정책도 `conf.json`의 설정을 그대로 따릅니다.
* 방문자 — 도구는 안전한 `GUEST_SERVERS`만 허용되며, 세션은 읽기 전용(`read_only`) 모드로 실행됩니다.

토론 전략은 '차례로 검토(순차 토론)' 1라운드로 고정됩니다. 한 번에 한 에이전트씩 순차적으로 발언해야 실시간 도구 실행 결과를
해당 발언과 정확히 매핑할 수 있기 때문입니다 (`loop.py`). 추가 작업은 동일한 세션에서 후속 요청으로 이어갈 수 있습니다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import Agent
from app.agents.personas import config_snapshot_of
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import MessageModel, SessionAgentModel, SessionModel, ToolCallRecordModel
from app.easy.builder import AgentDraft
from app.easy.catalog import DEMO_AGENT, GUEST_SERVERS, MAX_RUN_AGENTS, easy_workspace
from app.easy.models import EasyAgentModel, EasySessionModel
from app.trial.pages.session import load_feed_messages

ORCHESTRATOR = "orchestrator"
DEMO_REF = "demo"

# 모든 참여 에이전트 공통 프롬프트 지침. 도구 호출 전에 의도를 먼저 설명하도록 유도하여 UI에 '생각' 단계가 누락되지 않도록 합니다.
_INSTRUCTIONS = [
    "모든 답변은 한국어로 작성하며, 전문 용어 대신 알기 쉬운 표현을 사용합니다.",
    "도구를 사용하기 전에, 무엇을 어떤 목적으로 실행하려는지 한두 문장으로 먼저 설명하십시오.",
    "도구 결과를 확인하면 무엇을 알게 되었는지 한 문장으로 정리하고, 다음에 수행할 작업을 기술하십시오.",
    "직접 검증 가능한 정보는 추측하지 말고 도구를 활용해 직접 확인하십시오.",
]
_GUEST_INSTRUCTION = (
    "작업 폴더는 읽기만 가능합니다. 파일 생성/수정이나 코드 실행이 제한되면, 해당 사실과 함께 "
    "대안으로 수행한 작업을 결과에 기술하십시오."
)


def session_instructions(guest: bool) -> str:
    return "\n".join(_INSTRUCTIONS + ([_GUEST_INSTRUCTION] if guest else []))


@dataclass(frozen=True)
class Choice:
    """과제에 참여 가능한 에이전트 선택 항목. 풀 에이전트도 화면에는 설계도 형태로 일관되게 표시됩니다."""

    ref: str        # "demo" · "pool:<키>" · "my:<id>"
    key: str        # 대화 안에서의 에이전트 키
    draft: AgentDraft
    from_pool: bool = False


def _draft_of_row(row: EasyAgentModel) -> AgentDraft:
    return AgentDraft(
        name=row.name, role=row.role, system_prompt=row.system_prompt,
        allowed_mcp_servers=list(row.allowed_mcp_servers or []), allowed_skills=list(row.allowed_skills or []),
        card_color=row.card_color, icon=row.icon,
    )


def _draft_of_agent(agent: Agent) -> AgentDraft:
    return AgentDraft(
        key=agent.key, name=agent.name, role=agent.role, system_prompt=agent.system_prompt,
        allowed_mcp_servers=list(agent.allowed_mcp_servers), allowed_skills=list(agent.allowed_skills),
        card_color=agent.card_color or "", icon=agent.icon or "",
    )


async def list_guest_agents(db: AsyncSession, user_id: str) -> List[EasyAgentModel]:
    return list((await db.execute(
        select(EasyAgentModel).where(EasyAgentModel.user_id == user_id).order_by(EasyAgentModel.created_at)
    )).scalars().all())


async def delete_guest_agent(db: AsyncSession, user_id: str, agent_id: str) -> bool:
    result = await db.execute(
        delete(EasyAgentModel).where(EasyAgentModel.id == agent_id, EasyAgentModel.user_id == user_id)
    )
    await db.commit()
    return bool(result.rowcount)


async def agent_choices(db: AsyncSession, *, user_id: str, owner: bool, pool: AgentPool) -> List[Choice]:
    """선택 가능한 에이전트 목록을 조회합니다. 시연 에이전트가 최우선 배치되며, 소유자에게는 conf.json 전문가 풀이, 방문자에게는 내 에이전트 목록이 제공됩니다."""
    choices = [Choice(DEMO_REF, DEMO_AGENT["key"], AgentDraft.model_validate(DEMO_AGENT))]
    if owner:
        choices += [
            Choice(f"pool:{agent.key}", agent.key, _draft_of_agent(agent), from_pool=True)
            for agent in pool.list_all() if agent.key not in (ORCHESTRATOR, DEMO_AGENT["key"])
        ]
    else:
        choices += [
            Choice(f"my:{row.id}", f"my_{row.id[:8]}", _draft_of_row(row))
            for row in await list_guest_agents(db, user_id)
        ]
    return choices


# ---------------------------------------------------------------------------
# 대화 만들기
# ---------------------------------------------------------------------------


def _snapshot(choice: Choice, pool: AgentPool, priority: int, guest: bool) -> Dict[str, Any]:
    """참여 에이전트 1인의 불변 스냅샷 구성을 생성합니다.

    소유자가 선택한 conf.json 등록 에이전트는 기존 설정을 그대로 유지합니다. 그 외(시연 에이전트, 방문자 생성 에이전트)는
    오케스트레이터의 LLM 연결 설정을 상속받되 페르소나, 도구, 스킬만 설계도 값으로 오버라이드합니다. 순차적 사고는
    비활성화합니다 — 설계도에는 포함되지 않는 내부 설정이며, 화면에서 '생각' 단계를 시각화할 때 내용이 중복될 수 있기 때문입니다.
    """
    live = pool.get(choice.key) if choice.from_pool else None
    if live is not None and not guest:
        snapshot = config_snapshot_of(live)
    else:
        draft = choice.draft
        servers = [s for s in draft.allowed_mcp_servers if not guest or s in GUEST_SERVERS]
        snapshot = config_snapshot_of(pool.get_orchestrator())
        snapshot.update(
            name=draft.name,
            role=draft.role,
            system_prompt=draft.system_prompt,
            allowed_mcp_servers=servers,
            allowed_skills=list(draft.allowed_skills),
            card_color=draft.card_color or None,
            icon=draft.icon or None,
            debate_stance="neutral",
            enabled=True,
            sequential_thinking={**snapshot["sequential_thinking"], "enabled": False},
        )
    snapshot["debate_priority"] = priority
    return AgentConfig.model_validate(snapshot).model_dump(mode="json")


async def create_easy_session(
    db: AsyncSession,
    *,
    user_id: str,
    guest: bool,
    title: str,
    choices: Sequence[Choice],
    pool: AgentPool,
) -> Tuple[str, str]:
    """잠긴 세션을 생성하고 (세션 ID, 작업 디렉터리 경로) 튜플을 반환합니다."""
    if not choices:
        raise ValueError("과제를 수행할 에이전트를 최소 1명 이상 선택해 주세요.")
    if len(choices) > MAX_RUN_AGENTS:
        raise ValueError(f"에이전트는 한 번에 최대 {MAX_RUN_AGENTS}명까지 선택할 수 있습니다.")

    orchestrator = config_snapshot_of(pool.get_orchestrator())
    # 오케스트레이터는 계획 수립 및 종합 정리만 수행하므로 도구를 연결하지 않습니다 (체험 세션과 동일).
    orchestrator.update(allowed_mcp_servers=[], allowed_skills=[])
    participants: List[Tuple[str, Dict[str, Any]]] = [
        (ORCHESTRATOR, AgentConfig.model_validate(orchestrator).model_dump(mode="json"))
    ]
    for index, choice in enumerate(choices):
        participants.append((choice.key, _snapshot(choice, pool, priority=(index + 1) * 10, guest=guest)))

    workspace = easy_workspace(guest)
    keys = [key for key, _ in participants]
    sid = str(uuid.uuid4())
    db.add(SessionModel(
        id=sid,
        title=(title or "에이전트 과제 수행")[:255],
        strategy="sequential_debate",
        max_rounds=1,
        parallel_limit=1,
        active_agents=keys,
        known_agents=keys,
        custom_instructions=session_instructions(guest),
        workspace_dir=str(workspace),
        # 빈 문자열이면 conf.json의 `tool_security.mode`를 따릅니다 (소유자는 평소 승인 카드를 그대로 활용).
        tool_mode="read_only" if guest else "",
        personas_locked=True,
    ))
    for key, snapshot in participants:
        db.add(SessionAgentModel(
            session_id=sid,
            agent_key=key,
            name=snapshot["name"],
            role=snapshot["role"],
            system_prompt=snapshot.get("system_prompt") or "",
            card_color=snapshot.get("card_color") or "",
            icon_path=snapshot.get("icon") or "",
            config_snapshot=snapshot,
        ))
    db.add(EasySessionModel(session_id=sid, user_id=user_id))
    await db.commit()
    return sid, str(workspace)


# ---------------------------------------------------------------------------
# 내 기록
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    title: str
    created_at: Optional[datetime]


async def list_easy_sessions(db: AsyncSession, user_id: str, limit: int = 30) -> List[SessionRow]:
    rows = (await db.execute(
        select(SessionModel.id, SessionModel.title, EasySessionModel.created_at)
        .join(EasySessionModel, EasySessionModel.session_id == SessionModel.id)
        .where(EasySessionModel.user_id == user_id)
        .order_by(EasySessionModel.created_at.desc())
        .limit(limit)
    )).all()
    return [SessionRow(sid, title, created) for sid, title, created in rows]


async def owned_session(db: AsyncSession, user_id: str, session_id: str, *, owner: bool) -> Optional[SessionModel]:
    """현재 사용자가 접근 가능한 비엔지니어 세션을 조회합니다. 소유자는 모든 비엔지니어 세션을 열 수 있고, 방문자는 본인이 생성한 세션만 접근 가능합니다."""
    row = await db.get(EasySessionModel, session_id)
    if row is None or (not owner and row.user_id != user_id):
        return None
    return await db.get(SessionModel, session_id)


async def load_messages(db: AsyncSession, session_id: str) -> List[Dict[str, Any]]:
    """세션의 발언 목록과 각 발언별 도구 실행 기록, 턴 메타데이터(turn_meta)를 조합하여 조회합니다."""
    messages = await load_feed_messages(db, session_id)
    metas = dict((await db.execute(
        select(MessageModel.id, MessageModel.turn_meta).where(MessageModel.session_id == session_id)
    )).all())
    records = (await db.execute(
        select(ToolCallRecordModel)
        .where(ToolCallRecordModel.session_id == session_id)
        .order_by(ToolCallRecordModel.created_at)
    )).scalars().all()
    by_message: Dict[str, List[Dict[str, Any]]] = {}
    for tc in records:
        if not tc.message_id:
            continue
        by_message.setdefault(tc.message_id, []).append({
            "tool_name": tc.tool_name,
            "arguments": tc.arguments,
            "output": tc.output,
            "status": tc.status,
            "security": {
                "decision": tc.decision or "", "risk": tc.risk or "",
                "rule": tc.rule or "", "approver": tc.approver or "",
            },
        })
    for message in messages:
        message["tool_calls"] = by_message.get(message["id"], [])
        message["turn_meta"] = metas.get(message["id"])
    return messages
