"""적응형 그래프 토론: 오케스트레이터가 이번 턴의 실행 그래프를 그립니다 (ADR-034).

계획과 승인이 끝난 뒤, 토론이 시작되기 직전에 한 번 그립니다. 승인된 태스크 분담을 입력으로 받기 위해서입니다.
그린 그래프는 기록에 값으로 남기고(`KIND_GRAPH_DRAFT`) 턴이 끝날 때까지 고정합니다 — 끊긴 턴은 다시 묻지 않고
이 기록으로 이어 가고, 스케줄러는 사람이 그린 그래프와 똑같이 기록에서 다시 세워집니다.

그리지 못해도 턴은 돕니다. 쓸 수 있는 기준 그래프나 카드 순서로 물러서고, 그 사실을 피드에 남깁니다.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.agents.reasoning import strip_reasoning_trace
from app.events import EventCallback, EventType
from app.graph_store import load_graph
from app.orchestration import graph_draft, turns
from app.orchestration.artifacts import format_roster
from app.orchestration.graph import GraphSpec, parse_graph
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.phases.graph import freeze_graph
from app.orchestration.state import DebateState
from app.orchestration.strategies import order_by_priority, specialists_of

if TYPE_CHECKING:
    from app.orchestration.turn_setup import TurnSetup

logger = logging.getLogger(__name__)


class GraphDrafter(PhaseExecutor):
    """`draws_graph` 전략의 그래프 — 기록에 있으면 그것을, 없으면 오케스트레이터에게 그리게 합니다."""

    async def graph_for_turn(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        *,
        on_event: Optional[EventCallback],
    ) -> GraphSpec:
        """이 턴에 돌 그래프를 정해 기록하고 고정합니다. 끊겼던 턴이면 기록된 것을 그대로 돌려줍니다."""
        recorded = turns.drawn_graph(state.messages[state.turn_message_start:])
        if recorded is not None:
            return parse_graph(recorded)

        candidates = order_by_priority(specialists_of(setup.active_agents))
        keys = [a.key for a in candidates]
        graph_id = f"drawn-{(state.turn_id or uuid.uuid4().hex)[:8]}"
        base = self.base_graph(setup)

        draft: Optional[graph_draft.GraphDraft] = None
        why = ""
        if len(candidates) > 1 or base is not None:
            draft, why = await self.ask_orchestrator(state, setup, candidates, graph_id, base, on_event=on_event)

        if draft is not None:
            report = graph_draft.draft_report(draft.spec, keys, setup.max_rounds)
            spec = draft.spec
            content = graph_draft.draft_note(spec, draft.reason, report)
        else:
            spec = graph_draft.fallback_graph(graph_id, keys, base, setup.max_rounds)
            report = graph_draft.draft_report(spec, keys, setup.max_rounds)
            content = (
                graph_draft.fallback_note(spec, why, report) if why
                else graph_draft.draft_note(spec, "참여 전문가가 한 명 이하라 그리지 않고 바로 진행합니다.", report)
            )
        await self.recorder.record_note(
            db=db,
            state=state,
            on_event=on_event,
            agent=setup.orchestrator,
            round_number=0,
            # 물러선 기록은 지명·분배 실패와 같이 맥락에 넣지 않습니다.
            msg_type="error" if why else "orchestrator",
            content=content,
            kind=turns.KIND_GRAPH_DRAFT,
            data={"spec": spec.dump(), "fallback": bool(why)},
        )
        await freeze_graph(db, state.session_id, spec)
        return spec

    @staticmethod
    def base_graph(setup: "TurnSetup") -> Optional[GraphSpec]:
        """대화에 골라 둔 기준 그래프. 없거나 읽지 못하면 None — 처음부터 그립니다."""
        if not setup.base_graph_id:
            return None
        try:
            return load_graph(setup.base_graph_id)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(f"Could not read the base graph {setup.base_graph_id}; drawing from scratch: {exc}")
            return None

    async def ask_orchestrator(
        self,
        state: DebateState,
        setup: "TurnSetup",
        candidates: List[Agent],
        graph_id: str,
        base: Optional[GraphSpec],
        *,
        on_event: Optional[EventCallback],
    ) -> Tuple[Optional[graph_draft.GraphDraft], str]:
        """그래프를 받습니다. `(그래프, "")` 또는 `(None, 그리지 못한 이유)`.

        틀린 답에는 틀린 곳을 알려 주고 한 번 더 받습니다. 도구와 단계적 사고를 끈 사본으로 부릅니다 —
        발언이 아니라 정해진 형식의 답을 받는 호출입니다 (`ConversationMemory.tool_less`).
        """
        drafter = self.conversation.tool_less(setup.orchestrator)
        keys = [a.key for a in candidates]
        plan = (
            strip_reasoning_trace(state.messages[state.plan_index].content).strip()
            if state.plan_index is not None else ""
        )
        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "debating",
                "speaker": f"{setup.orchestrator.name} (실행 그래프 작성)",
                "round": 0,
            })

        errors: List[str] = []
        previous = ""
        for _attempt in range(graph_draft.DRAFT_ATTEMPTS):
            try:
                content, _ = await self.ctx.llm_caller.call_agent(
                    drafter,
                    graph_draft.draft_prompt(
                        goal=state.user_prompt,
                        record=self.conversation.routing_record(state, drafter),
                        plan=plan,
                        roster=format_roster(candidates, with_keys=True),
                        max_rounds=setup.max_rounds,
                        parallel_limit=setup.parallel_limit,
                        base=base,
                        previous=previous,
                        errors=errors,
                    ),
                    setup.custom_instructions,
                    session_id=state.session_id,
                    mcp=self.ctx.mcp_for(state),
                    tool_gate=self.ctx.gate_for(state),
                    ledger=state.decision_ledger,
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Could not draw the execution graph: {exc}")
                return None, str(exc)
            previous = content
            try:
                draft = graph_draft.parse_draft(content, graph_id, setup.max_rounds)
            except ValueError as exc:
                errors = [str(exc)]
                continue
            report = graph_draft.draft_report(draft.spec, keys, setup.max_rounds)
            if report.ok:
                return draft, ""
            errors = list(report.errors)
        logger.warning(f"The drawn execution graph was not usable: {errors}")
        return None, "오케스트레이터가 그린 그래프를 쓸 수 없습니다: " + " · ".join(errors[:3])
