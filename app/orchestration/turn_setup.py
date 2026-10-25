"""턴 준비: 구성과 참여자, 스킬 지정, 도구 보안 문지기, 턴 상태의 초기화와 복원.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional

from app.agents.base import Agent
from app.agents.llm_context import context_budget
from app.agents.personas import frozen_agents, prepare_agents_for_turn
from app.config import SKILL_NAME_PATTERN
from app.database.models import SessionModel
from app.events import EventCallback
from app.orchestration import plan_gate, turns
from app.orchestration.context import EngineContext
from app.orchestration.control import TurnControl
from app.orchestration.graph import GraphSpec, GraphTurnError
from app.orchestration.state import DebateState
from app.orchestration.strategies import get_strategy, resolve_strategy_name
from app.orchestration.tool_gate import ToolGate

logger = logging.getLogger(__name__)


@dataclass
class TurnSetup:
    """한 턴의 구성과 참여자 (`TurnPreparer.setup`)."""

    strategy_name: str
    max_rounds: int
    parallel_limit: int
    active_keys: List[str]
    custom_instructions: str
    active_agents: List[Agent]
    orchestrator: Agent
    graph_spec: Optional[GraphSpec]
    memory_budget: int
    # 입력창에서 @전문가 @스킬 로 지정한 것 (전문가 키 → 스킬). 이 턴에만 겁니다.
    skill_designations: Dict[str, List[str]] = field(default_factory=dict)
    # 동료 자문(peers)의 상대가 될 수 있는 이 대화의 에이전트 전부. 참여자는 이번 턴의 사본이고,
    # 참여하지 않는 에이전트는 첫 턴에 고정한 스냅샷입니다 — 자문 상대를 고른 에이전트는 이번
    # 토론에 참여하지 않는 전문가에게도 물을 수 있습니다 (`peer_consultation.targets`).
    consult_pool: List[Agent] = field(default_factory=list)
    # 결정 장부가 수렴하면 일찍 끝낼지, 턴 토큰 예산(0 = 없음) (v1.5).
    converge_early: bool = True
    token_budget: int = 0
    # 전문가가 발언 도중 사람에게 물을 수 있는지 (`ask__ask_user`, v1.5).
    ask_user: bool = False

    def config(self, workspace: Path) -> Dict[str, Any]:
        """턴 기록에 고정해 둘 구성 (`TurnModel.config`). 끊긴 턴은 이 구성으로 마칩니다."""
        return {
            "strategy": self.strategy_name,
            "max_rounds": self.max_rounds,
            "parallel_limit": self.parallel_limit,
            "active_agents": list(self.active_keys),
            "custom_instructions": self.custom_instructions,
            "workspace_dir": str(workspace),
            "skill_designations": {k: list(v) for k, v in self.skill_designations.items()},
            "converge_early": self.converge_early,
            "token_budget": self.token_budget,
            "ask_user": self.ask_user,
        }


def clean_skill_designations(
    designations: Optional[Dict[str, Any]], active_keys: List[str],
) -> Dict[str, List[str]]:
    """스킬 지정에서 이 턴에 쓸 수 있는 것만 남깁니다.

    참여하지 않는 전문가와 오케스트레이터(지목 대상이 아닙니다), 스킬 이름 규칙에 맞지 않는
    이름은 뺍니다. 지정은 화면이 보낸 값이고, 이름은 그대로 `allowed_skills` 에 더해지므로
    폴더 이름 규칙으로 거릅니다. 스킬이 실제로 있는지·켜져 있는지는 발언할 때 봅니다 (실시간).
    """
    active = set(active_keys) - {"orchestrator"}
    out: Dict[str, List[str]] = {}
    for key, names in (designations or {}).items():
        if key not in active or not isinstance(names, (list, tuple)):
            continue
        kept = [
            n for n in dict.fromkeys(str(x) for x in names)
            if SKILL_NAME_PATTERN.fullmatch(n)
        ]
        if kept:
            out[key] = kept
    return out


def new_state(session_id: str, user_prompt: str, workspace: Path, setup: "TurnSetup") -> DebateState:
    return DebateState(
        session_id=session_id,
        user_prompt=user_prompt,
        workspace_dir=str(workspace),
        strategy=setup.strategy_name,
        max_rounds=setup.max_rounds,
        current_round=0,
        custom_instructions=setup.custom_instructions,
        active_agent_keys=setup.active_keys,
        status="planning",
        memory_budget=setup.memory_budget,
        skill_designations={k: list(v) for k, v in setup.skill_designations.items()},
        ask_user=setup.ask_user,
    )


def restore_turn_state(state: DebateState) -> None:
    """끊긴 턴의 기록에서, 도는 동안 상태가 들고 있던 값을 다시 셉니다."""
    turn_messages = state.messages[state.turn_message_start:]
    state.plan_index = turns.plan_position(state.messages, state.turn_message_start)
    # 승인은 그것이 가리키는 계획에만 유효합니다 (ADR-028). 승인 뒤에 계획이 바뀐 기록이면
    # 승인받지 않은 것으로 봅니다.
    state.plan_tasks, state.approval_index = [], None
    approval = plan_gate.approval_record(state.messages, state.turn_message_start)
    if approval is not None and state.plan_index is not None:
        index, meta = approval
        if meta.get("plan_id") == state.messages[state.plan_index].id:
            state.plan_tasks = plan_gate.clean_tasks(meta.get("tasks"))
            state.approval_index = index
    state.failed_agent_keys = turns.failed_agents(turn_messages)
    state.fallback_agent_keys = turns.fallback_agents(turn_messages)
    state.interjection_count = turns.count_kind(turn_messages, turns.KIND_INTERJECTION)
    state.current_round = max(
        (m.round_number for m in turn_messages if m.sender_key != "user"), default=0
    )
    early = turns.early_end(turn_messages)
    if early is not None:
        reason, round_number = early
        state.converged_round = round_number if reason == turns.EARLY_END_CONVERGED else None
        state.budget_stopped = reason == turns.EARLY_END_BUDGET


class TurnPreparer:
    """턴의 구성과 참여자를 세우고, 이 턴의 도구 보안 문지기를 세웁니다."""

    def __init__(
        self, ctx: EngineContext, graph_for_turn: Callable[..., Awaitable[GraphSpec]],
    ) -> None:
        self.ctx = ctx
        # 그래프 토론이면 이 턴에 쓸 그래프를 읽고 검사해 고정합니다 (`GraphRunner.graph_for_turn`).
        self.graph_for_turn = graph_for_turn

    def rule_saver(
        self, session_id: str,
    ) -> Callable[[List[str], List[str]], Coroutine[Any, Any, None]]:
        """"이 대화에서 허용·거부" 목록을 세션에 저장하는 함수. 다음 턴의 게이트가 읽습니다."""

        async def save(grants: List[str], denials: List[str]) -> None:
            async with self.ctx.session_factory() as db:
                row = await db.get(SessionModel, session_id)
                if row is not None:
                    row.tool_grants = list(grants)
                    row.tool_denials = list(denials)
                    await db.commit()

        return save

    async def setup(
        self,
        db,
        session_model: SessionModel,
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        config: Optional[Dict[str, Any]] = None,
        graph_spec: Optional[GraphSpec] = None,
        skill_designations: Optional[Dict[str, List[str]]] = None,
    ) -> "TurnSetup":
        """턴의 구성과 참여자를 세웁니다. 도구 보안 문지기도 여기서 섭니다.

        `config` 가 없으면 지금 대화 설정으로(새 턴), 있으면 그 구성으로(끊긴 턴) 세웁니다.
        스킬 지정도 같습니다 — 새 턴은 `skill_designations` 를, 끊긴 턴은 고정해 둔 것을 씁니다.
        도구 보안(모드·허용·거부)은 어느 쪽이든 **지금** 대화 설정을 따릅니다 — 사람이 끊긴
        사이에 조인 보안을 이어 가는 턴이 우회하면 안 됩니다.
        """
        session_id = session_model.id
        if config is None:
            # 옛 이름으로 저장된 대화도 지금 쓰는 전략으로 옮겨 돌립니다.
            strategy_name = resolve_strategy_name(session_model.strategy)
            max_rounds = session_model.max_rounds
            # 병렬 지시 전략에서만 읽힙니다. 0 이나 음수는 "동시에 아무도 못 돈다"
            # 는 뜻이 되어 라운드가 통째로 비므로 최소 1 로 올립니다.
            parallel_limit = max(1, int(session_model.parallel_limit or 3))
            active_keys = session_model.active_agents or ["orchestrator", "architect", "coder", "critic"]
            custom_instructions = session_model.custom_instructions or ""
            designations = skill_designations
            converge_early = bool(session_model.converge_early)
            token_budget = max(0, int(session_model.token_budget or 0))
            ask_user = bool(session_model.ask_user)
        else:
            strategy_name = resolve_strategy_name(config.get("strategy"))
            max_rounds = int(config.get("max_rounds") or session_model.max_rounds or 3)
            parallel_limit = max(1, int(config.get("parallel_limit") or 3))
            active_keys = list(config.get("active_agents") or session_model.active_agents or [])
            custom_instructions = str(config.get("custom_instructions") or "")
            designations = config.get("skill_designations")
            converge_early = bool(config.get("converge_early", True))
            token_budget = max(0, int(config.get("token_budget") or 0))
            ask_user = bool(config.get("ask_user", False))

        # 도구 보안: 이 대화의 모드와 "이 대화에서 허용·거부" 목록으로 문지기를 세웁니다.
        self.ctx.tool_gates[session_id] = ToolGate(
            session_id=session_id,
            mode=session_model.tool_mode or "",
            grants=list(session_model.tool_grants or []),
            denials=list(session_model.tool_denials or []),
            control=control,
            on_event=on_event,
            save_rules=self.rule_saver(session_id),
        )

        # Ensure orchestrator is in active keys
        if "orchestrator" not in active_keys:
            active_keys = ["orchestrator"] + list(active_keys)

        # 그래프 토론은 그래프에 놓인 에이전트가 곧 참여자입니다 (로스터 체크박스가 아니라).
        if get_strategy(strategy_name).runs_graph:
            if graph_spec is None:
                graph_spec = await self.graph_for_turn(db, session_model, max_rounds)
            active_keys = ["orchestrator"] + graph_spec.agent_keys()
        else:
            graph_spec = None

        # 세션 페르소나를 적용합니다. 첫 턴이면 이 시점에 기록되고 잠깁니다.
        active_agents = await prepare_agents_for_turn(
            db, session_model, self.ctx.agent_pool, active_keys
        )
        if graph_spec is not None:
            present = {a.key for a in active_agents}
            missing = [k for k in graph_spec.agent_keys() if k not in present]
            if missing:
                raise GraphTurnError(
                    f"그래프의 에이전트를 준비하지 못했습니다: {', '.join(missing)}"
                )
        orchestrator_agent = next(
            (a for a in active_agents if a.key == "orchestrator"),
            self.ctx.agent_pool.get_orchestrator(),
        )
        present_keys = {a.key for a in active_agents}
        consult_pool = list(active_agents) + [
            a for a in await frozen_agents(db, session_id, self.ctx.agent_pool) if a.key not in present_keys
        ]
        # 장부와 요약은 모든 참여자의 프롬프트에 실립니다. 상한을 가장 작은 창에 맞춥니다.
        memory_budget = min(
            (context_budget(a) for a in active_agents), default=context_budget(orchestrator_agent)
        )
        return TurnSetup(
            strategy_name=strategy_name,
            max_rounds=max_rounds,
            parallel_limit=parallel_limit,
            active_keys=list(active_keys),
            custom_instructions=custom_instructions,
            active_agents=active_agents,
            orchestrator=orchestrator_agent,
            graph_spec=graph_spec,
            memory_budget=memory_budget,
            skill_designations=clean_skill_designations(designations, active_keys),
            consult_pool=consult_pool,
            converge_early=converge_early,
            token_budget=token_budget,
            ask_user=ask_user,
        )
