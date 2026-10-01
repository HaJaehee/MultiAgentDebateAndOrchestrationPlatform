"""세션별 에이전트 페르소나 / 시스템 프롬프트 관리.

`conf.json` 의 에이전트 정의는 서버 전역 기본값입니다. 세션마다 다른 페르소나로
토론시키려면 서버를 재기동해야 했는데, 이 모듈이 그 제약을 없앱니다.

수명주기:

1. **세션 생성 직후 (열림)** — 유저가 페르소나 편집 페이지에서 이름/역할/시스템
   프롬프트를 고칩니다. 편집분은 `session_agents` 에 초안으로 저장됩니다.
   저장하지 않은 에이전트는 `conf.json` 기본값을 그대로 씁니다.
2. **첫 유저 메시지 (잠금)** — 그 시점의 유효값이 *모든* 에이전트에 대해
   기록되고 `sessions.personas_locked` 가 True 가 됩니다. 토론 도중 페르소나가
   바뀌면 앞선 발언과 뒤의 발언이 서로 다른 인격에서 나오게 되므로, 기록을
   해석할 수 없게 됩니다.
3. **세션 재개** — 저장된 값이 그대로 사용됩니다. 그 사이 `conf.json` 이
   바뀌었더라도 세션은 잠글 때의 페르소나를 유지합니다.

**시작한 대화는 자기완결적입니다.** 잠그는 시점에 인격(이름·역할·시스템 프롬프트)만이
아니라 `AgentConfig` 전체 — 모델·엔드포인트·API 키·샘플링 값·도구 권한·단계적 사고까지 —
를 `session_agents.config_snapshot` 에 고정합니다. 그 뒤 `conf.json` 에서 그 에이전트를
지우거나, 끄거나, 모델을 바꿔도 이 대화는 잠글 때의 구성 그대로 이어집니다.

그래서 편집 화면의 잠금 규칙과 실행이 같은 말을 합니다 — "이미 시작한 대화에는 영향이
없다" 가 인격뿐 아니라 구성 전체에 대해 참입니다.

바꾼 게이트웨이 주소나 새 API 키를 옛 대화에도 먹여야 한다면 `resync_agent_configs()`
가 스냅샷을 지금 `conf.json` 값으로 다시 고정합니다 (인격은 그대로 둡니다).
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import Agent, style_for_agent
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import SessionAgentModel, SessionModel

logger = logging.getLogger(__name__)

# 세션별로 편집할 수 있는 항목. 나머지 운영 설정은 conf.json 이 정본이되, 대화를
# 잠그는 순간 `config_snapshot` 으로 함께 고정됩니다.
EDITABLE_FIELDS = ("name", "role", "system_prompt", "card_color", "icon")


class PersonasLockedError(RuntimeError):
    """첫 유저 메시지 이후 페르소나를 수정하려 할 때 발생합니다."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(
            f"세션 '{session_id}' 은 이미 토론이 시작되어 페르소나가 고정되었습니다."
        )


class AgentPersona(BaseModel):
    """한 에이전트의 세션 내 인격."""

    agent_key: str
    name: str
    role: str
    system_prompt: str = ""
    # 카드 색과 아이콘. 인격과 같은 수명을 갖습니다 — 대화가 잠기면 함께 고정되고,
    # 그 뒤 conf.json 에서 색을 바꿔도 이 대화의 기록은 그대로입니다.
    card_color: str = ""
    icon: str = ""
    # conf.json 기본값과 다른지 여부 (UI 표시용).
    # "저장된 행이 있는가" 가 아니라 "값이 실제로 다른가" 입니다. 세션을 잠글 때
    # 손대지 않은 에이전트까지 전부 스냅샷되므로, 행의 존재만으로는 판단할 수 없습니다.
    is_customized: bool = Field(default=False)


def _differs(a: AgentPersona, b: AgentPersona) -> bool:
    """편집 가능한 항목 중 하나라도 다르면 True."""
    return any(getattr(a, f) != getattr(b, f) for f in EDITABLE_FIELDS)


def persona_from_agent(agent: Agent) -> AgentPersona:
    """conf.json 기반 에이전트에서 기본 페르소나를 만듭니다."""
    return AgentPersona(
        agent_key=agent.key,
        name=agent.name,
        role=agent.role,
        system_prompt=agent.system_prompt or "",
        card_color=agent.card_color or "",
        icon=agent.icon or "",
    )


def default_personas(pool: AgentPool) -> Dict[str, AgentPersona]:
    """conf.json 기준 기본 페르소나 전체."""
    return {a.key: persona_from_agent(a) for a in pool.list_all()}


async def _stored_rows(db: AsyncSession, session_id: str) -> List[SessionAgentModel]:
    """이 세션의 `session_agents` 행 전부 (저장된 순서)."""
    result = await db.execute(
        select(SessionAgentModel)
        .where(SessionAgentModel.session_id == session_id)
        .order_by(SessionAgentModel.created_at, SessionAgentModel.id)
    )
    return list(result.scalars().all())


def _persona_from_row(row: SessionAgentModel) -> AgentPersona:
    return AgentPersona(
        agent_key=row.agent_key,
        name=row.name,
        role=row.role,
        system_prompt=row.system_prompt or "",
        card_color=row.card_color or "",
        icon=row.icon_path or "",
    )


async def load_stored_personas(db: AsyncSession, session_id: str) -> Dict[str, AgentPersona]:
    """DB 에 저장된 페르소나(초안 또는 잠긴 스냅샷)를 읽습니다."""
    return {row.agent_key: _persona_from_row(row) for row in await _stored_rows(db, session_id)}


async def effective_personas(
    db: AsyncSession, session_id: str, pool: AgentPool
) -> Dict[str, AgentPersona]:
    """이 세션에서 실제로 쓰이는 페르소나.

    저장분이 있으면 그것을, 없으면 `conf.json` 기본값을 씁니다. 세션을 잠근 뒤에
    `conf.json` 에 새로 추가된 에이전트는 저장분이 없으므로 기본값으로 참여합니다.
    """
    defaults = default_personas(pool)
    personas = dict(defaults)
    for key, stored in (await load_stored_personas(db, session_id)).items():
        base = defaults.get(key)
        personas[key] = stored.model_copy(
            update={"is_customized": base is None or _differs(stored, base)}
        )
    return personas


async def save_persona(
    db: AsyncSession,
    session_model: SessionModel,
    agent_key: str,
    name: str,
    role: str,
    system_prompt: str,
    card_color: str = "",
    icon: str = "",
) -> AgentPersona:
    """페르소나 초안을 저장합니다. 잠긴 세션이면 거부합니다."""
    if session_model.personas_locked:
        raise PersonasLockedError(session_model.id)

    result = await db.execute(
        select(SessionAgentModel).where(
            SessionAgentModel.session_id == session_model.id,
            SessionAgentModel.agent_key == agent_key,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SessionAgentModel(session_id=session_model.id, agent_key=agent_key)
        db.add(row)

    row.name = name.strip()
    row.role = role.strip()
    row.system_prompt = system_prompt.strip()
    row.card_color = (card_color or "").strip()
    row.icon_path = (icon or "").strip()
    await db.commit()

    logger.info(f"Persona saved for session={session_model.id} agent={agent_key}")
    return AgentPersona(
        agent_key=agent_key,
        name=row.name,
        role=row.role,
        system_prompt=row.system_prompt,
        card_color=row.card_color,
        icon=row.icon_path,
    )


async def reset_persona(db: AsyncSession, session_model: SessionModel, agent_key: str) -> None:
    """초안을 지워 `conf.json` 기본값으로 되돌립니다."""
    if session_model.personas_locked:
        raise PersonasLockedError(session_model.id)

    await db.execute(
        delete(SessionAgentModel).where(
            SessionAgentModel.session_id == session_model.id,
            SessionAgentModel.agent_key == agent_key,
        )
    )
    await db.commit()
    logger.info(f"Persona reset for session={session_model.id} agent={agent_key}")


async def freeze_personas(
    db: AsyncSession, session_model: SessionModel, pool: AgentPool
) -> Dict[str, AgentPersona]:
    """첫 유저 메시지 시점의 유효 구성을 전부 기록하고 세션을 잠급니다.

    인격뿐 아니라 `AgentConfig` 전체를 `config_snapshot` 에 고정합니다. 이 대화는
    이 순간부터 conf.json 에 의존하지 않습니다 — 에이전트가 지워지든 모델이
    바뀌든 잠글 때의 구성 그대로 이어집니다.

    이미 잠긴 세션이면 저장된 값을 그대로 돌려줍니다 (멱등).
    """
    if session_model.personas_locked:
        return await effective_personas(db, session_model.id, pool)

    personas = await effective_personas(db, session_model.id, pool)
    rows = {row.agent_key: row for row in await _stored_rows(db, session_model.id)}
    live = {agent.key: agent for agent in pool.list_all()}

    for key, persona in personas.items():
        row = rows.get(key)
        if row is None:
            # 초안이 없던 에이전트. conf.json 기본값이 그대로 이 대화의 값이 됩니다.
            row = SessionAgentModel(
                session_id=session_model.id,
                agent_key=key,
                name=persona.name,
                role=persona.role,
                system_prompt=persona.system_prompt,
                card_color=persona.card_color,
                icon_path=persona.icon,
            )
            db.add(row)
        # 초안이 이미 있으면 인격은 그대로 두고 운영 설정만 고정합니다.
        agent = live.get(key)
        if agent is not None:
            merged = _with_persona(agent, persona)
            row.config_snapshot = config_snapshot_of(merged)
            # 카드 색·아이콘은 컬럼에도 함께 고정합니다. 손대지 않은 에이전트는
            # 지금 conf.json 값이 그대로 이 대화의 겉모습이 됩니다.
            row.card_color = merged.card_color or ""
            row.icon_path = merged.icon or ""

    session_model.personas_locked = True
    await db.commit()
    logger.info(
        f"Agent configuration frozen for session={session_model.id}: {sorted(personas.keys())}"
    )
    return personas


def _with_persona(agent: Agent, persona: Optional[AgentPersona]) -> Agent:
    """에이전트에 페르소나를 입힌 사본 (원본은 건드리지 않습니다)."""
    if persona is None:
        return agent
    card_color = persona.card_color or agent.card_color or ""
    icon = persona.icon or agent.icon or ""
    # 색·아이콘을 바꿨으면 그것을 푼 값(`avatar` / `color` / `badge_color`)도 함께
    # 바꿔야 합니다. 이 사본이 그대로 화면으로 나가기 때문입니다.
    return agent.model_copy(
        update={
            "name": persona.name or agent.name,
            "role": persona.role or agent.role,
            "system_prompt": persona.system_prompt or agent.system_prompt,
            "card_color": card_color or None,
            "icon": icon or None,
            **style_for_agent(agent.key, card_color, icon),
        }
    )


def apply_personas(agents: List[Agent], personas: Dict[str, AgentPersona]) -> List[Agent]:
    """에이전트 목록에 페르소나를 입힌 사본을 만듭니다 (원본 풀은 건드리지 않음)."""
    return [_with_persona(agent, personas.get(agent.key)) for agent in agents]


# --------------------------------------------------------------- 구성 스냅샷


def config_snapshot_of(agent: Agent) -> Dict[str, Any]:
    """`Agent` 에서 `AgentConfig` 로 되돌릴 수 있는 값만 JSON 으로 뽑습니다.

    사람이 정한 카드 색(`card_color`)과 아이콘(`icon`)은 여기 담깁니다 — 이 대화가
    그 겉모습으로 기록되었기 때문입니다. 반면 그것을 화면용으로 푼 값
    (`avatar` / `color` / `badge_color`)은 담지 않습니다. 앞의 둘에서 언제든 다시
    만들 수 있고, 오히려 파일 경로가 두 군데에 적히는 편이 위험합니다.
    """
    config = AgentConfig.model_validate(
        {field: getattr(agent, field) for field in AgentConfig.model_fields}
    )
    return config.model_dump(mode="json")


def agent_from_snapshot(agent_key: str, snapshot: Any) -> Optional[Agent]:
    """스냅샷에서 에이전트를 복원합니다. 읽을 수 없으면 None.

    스냅샷이 깨졌다고 대화를 못 열게 하지는 않습니다. 부르는 쪽이 살아 있는 풀로
    물러섭니다.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    try:
        return Agent.from_config(agent_key, AgentConfig.model_validate(snapshot))
    except Exception as exc:  # noqa: BLE001 - 깨진 스냅샷으로 대화를 막지 않습니다
        logger.warning(f"Could not restore agent '{agent_key}' from its snapshot: {exc}")
        return None


async def frozen_agents(
    db: AsyncSession, session_id: str, pool: AgentPool
) -> List[Agent]:
    """이 대화가 잠길 때 고정된 에이전트 전부.

    스냅샷이 없는 행은 이 기능이 생기기 전에 잠긴 대화입니다. 그런 행은 살아 있는
    풀에서 찾아 페르소나만 입혀 씁니다 — 그 대화가 지금까지 돌아왔던 그대로입니다.
    풀에도 없으면 되살릴 방법이 없으므로 건너뜁니다.

    순서는 화면에 그대로 나가므로 결정적이어야 합니다. `session_agents` 의 행들은
    잠글 때 한 커밋에 들어가 `created_at` 이 같고, 그러면 정렬이 무작위 UUID 로
    떨어져 카드가 열 때마다 뒤바뀝니다. 오케스트레이터를 맨 앞에 두고, 그 뒤는
    conf.json 순서를 따르며, 이 대화에만 남은 에이전트는 맨 뒤에 모읍니다.
    """
    agents: List[Agent] = []
    for row in await _stored_rows(db, session_id):
        agent = agent_from_snapshot(row.agent_key, row.config_snapshot)
        if agent is None:
            live = pool.get(row.agent_key)
            if live is None:
                continue
            agent = _with_persona(live, _persona_from_row(row))
        agents.append(agent)
    pool_order = {agent.key: i for i, agent in enumerate(pool.list_all())}
    agents.sort(
        key=lambda a: (a.key != "orchestrator", pool_order.get(a.key, len(pool_order)), a.key)
    )
    return agents


async def session_roster_agents(
    db: AsyncSession, session_model: SessionModel, pool: AgentPool
) -> List[Agent]:
    """이 대화의 로스터에 보여야 할 에이전트.

    잠긴 대화는 잠글 때 고정된 구성, 아직 시작하지 않은 대화는 살아 있는 풀입니다.
    화면이 이것을 쓰지 않으면 지워진 에이전트가 카드 없이 발언하게 됩니다.
    """
    if not session_model.personas_locked:
        return pool.list_all()
    agents = await frozen_agents(db, session_model.id, pool)
    return agents or pool.list_all()


async def resync_agent_configs(
    db: AsyncSession, session_model: SessionModel, pool: AgentPool
) -> List[str]:
    """잠긴 대화의 구성 스냅샷을 지금 conf.json 값으로 다시 고정합니다.

    인격(이름·역할·시스템 프롬프트)은 건드리지 않습니다. 바뀌는 것은 모델·
    엔드포인트·키·도구처럼 운영에 속하는 값뿐입니다.

    스냅샷이 없으면 옛 대화를 이어갈 수 없게 되는 상황 — 게이트웨이 주소가
    바뀌었거나 API 키가 만료된 경우 — 을 위한 탈출구입니다. 지금 conf.json 에
    없는 에이전트는 그대로 둡니다. 이 대화에서만 살아 있는 에이전트를 지우는 것이
    이 함수의 일은 아닙니다.

    바꾼 에이전트 키 목록을 돌려줍니다.
    """
    if not session_model.personas_locked:
        return []

    live = {agent.key: agent for agent in pool.list_all()}
    updated: List[str] = []
    for row in await _stored_rows(db, session_model.id):
        agent = live.get(row.agent_key)
        if agent is None:
            continue
        row.config_snapshot = config_snapshot_of(_with_persona(agent, _persona_from_row(row)))
        updated.append(row.agent_key)

    if updated:
        await db.commit()
        logger.info(
            f"Agent configuration re-synced for session={session_model.id}: {sorted(updated)}"
        )
    return updated


async def prepare_agents_for_turn(
    db: AsyncSession,
    session_model: SessionModel,
    pool: AgentPool,
    active_keys: List[str],
) -> List[Agent]:
    """토론 한 턴에 쓸 에이전트를 준비합니다.

    첫 턴이면 그 시점의 구성을 통째로 기록하고 잠급니다. 이후 턴은 그 스냅샷을
    그대로 씁니다 — conf.json 이 그 사이 어떻게 바뀌었든.
    """
    personas = await freeze_personas(db, session_model, pool)
    frozen = {agent.key: agent for agent in await frozen_agents(db, session_model.id, pool)}

    ordered = ["orchestrator"] + [k for k in active_keys if k != "orchestrator"]
    agents: List[Agent] = []
    seen = set()
    for key in ordered:
        if key in seen:
            continue
        seen.add(key)
        agent = frozen.get(key)
        if agent is None:
            # 스냅샷이 없는 옛 대화, 또는 잠근 뒤 사용자가 합류시킨 에이전트.
            live = pool.get(key)
            if live is None:
                continue
            agent = _with_persona(live, personas.get(key))
        agents.append(agent)
    return agents
