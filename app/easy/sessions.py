"""일을 맡기는 대화 — 참여자 고르기, 잠긴 대화 만들기, 내 기록과 내 에이전트.

체험 템플릿(`app/trial/templates.py` `create_trial_session`)과 같은 방식입니다. 참여자 구성을
`session_agents.config_snapshot` 에 미리 고정해 **잠긴 대화**를 만들어 엔진에 넘기면, 엔진은 평소대로
돌립니다 (ADR-011). conf.json 에 없는 참여자(시연 에이전트, 방문자의 에이전트)도 그렇게 발언합니다.

체험과 다른 점은 도구입니다. 비엔지니어가 에이전트를 챗봇과 구별하는 것은 행동하고 관찰하는 장면이라
도구를 붙입니다.

* 주인 — conf.json 의 에이전트는 설정 그대로, 도구 보안은 conf.json 의 기본 모드를 따릅니다.
* 방문자 — 도구는 `GUEST_SERVERS` 만 남기고, 대화는 읽기 전용(`read_only`)으로 돕니다.

진행 방식은 '차례로 검토' 한 번으로 고정합니다. 한 번에 한 사람만 말해야 화면이 도구 결과를 그 발언에
이어 붙일 수 있습니다 (`loop.py`). 더 시킬 것은 같은 대화에 이어서 요청합니다.
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

# 모든 참여자에게 주는 지침. 도구를 부르기 전에 생각을 말하게 해야 화면에 '생각' 단계가 생깁니다.
_INSTRUCTIONS = [
    "모든 답은 한국어로, 전문 용어 없이 쉬운 말로 씁니다.",
    "도구를 쓰기 전에, 무엇을 왜 하려는지 한두 문장으로 먼저 말하십시오.",
    "도구 결과를 받으면 무엇을 알게 되었는지 한 문장으로 적고, 다음에 할 일을 정하십시오.",
    "확인할 수 있는 것은 추측하지 말고 도구로 직접 확인하십시오.",
]
_GUEST_INSTRUCTION = (
    "작업 폴더는 읽기만 할 수 있습니다. 파일 쓰기나 코드 실행이 거부되면, 그 사실과 대신 한 일을 "
    "결과에 적으십시오."
)


def session_instructions(guest: bool) -> str:
    return "\n".join(_INSTRUCTIONS + ([_GUEST_INSTRUCTION] if guest else []))


@dataclass(frozen=True)
class Choice:
    """일을 맡길 수 있는 에이전트 하나. 풀 에이전트도 화면에는 설계도 모양으로 보입니다."""

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
    """고를 수 있는 에이전트. 시연 에이전트가 맨 앞이고, 주인은 conf.json 의 에이전트, 방문자는 내 에이전트."""
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
    """참여자 한 명의 고정 구성.

    주인이 고른 conf.json 에이전트는 그 설정 그대로입니다. 나머지(시연 에이전트, 방문자의 에이전트)는
    오케스트레이터의 연결 설정을 빌리고 인격·도구·스킬만 설계도의 것으로 바꿉니다. 단계적 사고는
    끕니다 — 설계도에 없는 설정이고, 화면이 '생각' 단계를 따로 보여 주므로 `Thought 1..N` 이 겹칩니다.
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
    """잠긴 대화를 만들고 (대화 id, 작업 폴더) 를 돌려줍니다."""
    if not choices:
        raise ValueError("일을 맡길 에이전트를 한 명 이상 골라 주십시오.")
    if len(choices) > MAX_RUN_AGENTS:
        raise ValueError(f"한 번에 {MAX_RUN_AGENTS}명까지 고를 수 있습니다.")

    orchestrator = config_snapshot_of(pool.get_orchestrator())
    # 사회자는 계획과 정리만 합니다. 도구는 일을 맡은 에이전트에게만 붙입니다 (체험과 같습니다).
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
        title=(title or "에이전트에게 맡긴 일")[:255],
        strategy="sequential_debate",
        max_rounds=1,
        parallel_limit=1,
        active_agents=keys,
        known_agents=keys,
        custom_instructions=session_instructions(guest),
        workspace_dir=str(workspace),
        # 비우면 conf.json 의 `tool_security.mode` 를 따릅니다 (주인은 평소의 승인 카드를 그대로 받습니다).
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
    """이 사람이 열 수 있는 쉬운 화면의 대화. 주인은 모든 쉬운 대화를, 방문자는 자기 것만 엽니다."""
    row = await db.get(EasySessionModel, session_id)
    if row is None or (not owner and row.user_id != user_id):
        return None
    return await db.get(SessionModel, session_id)


async def load_messages(db: AsyncSession, session_id: str) -> List[Dict[str, Any]]:
    """발언과 그 발언이 실행한 도구 기록, 그리고 기록의 종류(`turn_meta` — 계획 승인 기록을 가립니다)."""
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
