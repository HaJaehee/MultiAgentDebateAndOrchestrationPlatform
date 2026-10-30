"""1단계: 계획.

오케스트레이터가 이번 턴의 목표와 분담을 정하고(`plan`), 사람이 그 계획을 확인하게 합니다
(`approve_plan`, ADR-028). 수정 요청이 오면 계획을 다시 씁니다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.agents.llm_context import context_budget
from app.agents.reasoning import strip_reasoning_trace
from app.database.models import TurnModel
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory
from app.orchestration import plan_gate, turns
from app.orchestration.artifacts import format_roster
from app.orchestration.control import TurnControl, asks_human
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.state import DebateMessage, DebateState

if TYPE_CHECKING:
    from app.orchestration.turn_setup import TurnSetup

logger = logging.getLogger(__name__)



class PlanningPhaseExecutor(PhaseExecutor):
    """오케스트레이터의 목표 분석·분담 계획 수립 및 계획 승인 게이트를 전담합니다."""

    async def plan(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        *,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
        revision: Optional[str] = None,
    ) -> Optional[DebateMessage]:
        """오케스트레이터가 이번 턴의 목표와 역할 분담을 수립합니다. 계획 단계를 건너뛰는 그래프인 경우 None을 반환합니다."""
        orchestrator_agent = setup.orchestrator
        user_prompt = state.user_prompt
        state.status = "planning"
        state.current_speaker = orchestrator_agent.name
        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "planning",
                "speaker": orchestrator_agent.name,
            })

        specialists = [a for a in setup.active_agents if a.key != "orchestrator"]
        roster_block = (
            f"[이번 토론 참여 전문가]\n{format_roster(specialists)}\n\n"
            if specialists else ""
        )
        assign_rule = (
            "위 전문가 각각에게, 목록의 이름 그대로 불러 이번 턴에 맡을 일과 산출물을 "
            "한두 문장으로 지시하세요. 도구가 필요한 일(파일 쓰기 등)은 그 도구를 가진 "
            "전문가에게 맡기고, 목록에 없는 역할에는 일을 주지 마세요."
            if specialists else ""
        )

        plan_record = memory.build_user_record(
            state,
            model=orchestrator_agent.model,
            token_cap=int(context_budget(orchestrator_agent) * memory.USER_RECORD_SHARE),
        ).text
        plan_record_block = f"{plan_record}\n\n" if plan_record else ""

        earlier = state.messages[:state.turn_message_start]
        if revision is not None:
            previous = (
                strip_reasoning_trace(state.messages[state.plan_index].content).strip()
                if state.plan_index is not None else ""
            )
            orch_plan_prompt = [
                {"role": "user", "content": (
                    f"[User Request]: {user_prompt}\n\n"
                    f"{plan_record_block}"
                    f"{roster_block}"
                    f"[이전 계획]\n{previous or '(기록 없음)'}\n\n"
                    f"[유저의 계획 수정 요청]\n{revision}\n\n"
                    "유저가 위 계획을 승인하지 않고 수정을 요청했습니다. 요청을 반영해 이번 토론의 "
                    "핵심 목표, 접근 방향, 각 전문가에게 부여할 발언 지침을 처음부터 끝까지 다시 "
                    "작성하세요. 요청이 언급하지 않은 부분은 이전 계획을 유지하세요. "
                    f"{assign_rule}"
                )}
            ]
        elif earlier:
            history_snippets = []
            for m in earlier:
                if m.msg_type == "error" or not turns.in_main_record(m):
                    continue
                history_snippets.append(f"{m.speaker}: {self.conversation.snippet(m, 250)}")
            history_text = "\n".join(history_snippets[-6:])
            orch_plan_prompt = [
                {"role": "user", "content": (
                    f"[이전 대화 맥락]:\n{history_text}\n\n"
                    f"{plan_record_block}"
                    f"[신규 User Request]:\n{user_prompt}\n\n"
                    f"{roster_block}"
                    "위의 이전 세션 논의 맥락과 새로운 유저 요청을 종합 분석하여 이번 토론의 핵심 목표, "
                    "접근 방향, 각 전문가에게 부여할 발언 지침을 작성하세요. "
                    f"{assign_rule}"
                )}
            ]
        else:
            orch_plan_prompt = [
                {"role": "user", "content": (
                    f"[User Request]: {user_prompt}\n\n"
                    f"{roster_block}"
                    "위 요청을 분석하고 이번 토론의 핵심 목표, 접근 방향, 각 전문가에게 부여할 "
                    f"발언 지침을 작성하세요. {assign_rule}"
                )}
            ]

        plan_message: Optional[DebateMessage] = None
        if setup.graph_spec is None or setup.graph_spec.start.plan:
            plan_message = await self.speech.speak(
                db=db,
                state=state,
                agent=orchestrator_agent,
                prompt_messages=orch_plan_prompt,
                custom_instructions=setup.custom_instructions,
                round_number=0,
                msg_type="orchestrator",
                on_event=on_event,
                control=control,
                kind=turns.KIND_PLAN,
            )
        if plan_message is not None and plan_message.msg_type != "error":
            state.plan_index = len(state.messages) - 1
        return plan_message

    async def approve_plan(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        plan_message: Optional[DebateMessage],
        *,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
    ) -> Optional[DebateMessage]:
        """수립된 계획을 사용자에게 제시하고 승인을 받습니다. 토론 시작의 기준이 될 계획 발언 메시지를 반환합니다."""
        settings = plan_gate.plan_approval_settings()
        specialists = [a for a in setup.active_agents if a.key != "orchestrator"]
        if (
            not settings.enabled
            or not asks_human(control)
            or not specialists
            or plan_message is None
            or plan_message.msg_type == "error"
        ):
            return plan_message

        orchestrator = setup.orchestrator
        await self.recorder.set_turn(db, state, phase="approval")

        async def _open(request: Any) -> None:
            if on_event:
                await on_event({"type": EventType.PLAN_APPROVAL_REQUESTED, **request.describe()})

        notice = ""
        waiting = plan_gate.pending_revision(state.messages, state.turn_message_start)
        while True:
            if waiting is not None:
                revised = await self.plan(
                    db, state, setup, on_event=on_event, control=control, revision=waiting,
                )
                waiting = None
                if revised is not None and revised.msg_type != "error":
                    plan_message = revised
                else:
                    notice = (
                        "오케스트레이터가 응답하지 못해 계획을 다시 쓰지 못했습니다. "
                        "이전 계획을 그대로 다시 보여 드립니다."
                    )
            if control.stop_requested:
                return plan_message

            proposed = await self.plan_proposal(
                db, state, setup, plan_message, specialists, on_event=on_event,
            )
            request = await control.ask_plan_approval(
                agent_key=orchestrator.key,
                agent_name=orchestrator.name,
                payload={
                    "plan_id": plan_message.id,
                    "plan": strip_reasoning_trace(plan_message.content).strip(),
                    "tasks": proposed,
                    "revision": plan_gate.revision_count(state.messages, state.turn_message_start),
                    "notice": notice,
                },
                timeout=float(settings.timeout),
                on_open=_open,
            )
            notice = ""
            if on_event:
                await on_event({
                    "type": EventType.PLAN_APPROVAL_RESOLVED,
                    "id": request.id,
                    "agent_name": orchestrator.name,
                    "decision": request.decision,
                    "wait_seconds": float(settings.timeout),
                })

            if request.decision == plan_gate.DECISION_APPROVE:
                tasks, changes = plan_gate.settle(proposed, request.tasks)
                await self.recorder.record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=orchestrator,
                    round_number=0,
                    msg_type="orchestrator",
                    content=plan_gate.approval_note(tasks, changes),
                    kind=turns.KIND_APPROVAL,
                    data={
                        "plan_id": plan_message.id,
                        "tasks": tasks,
                        "changes": changes,
                        "approver": request.approver,
                    },
                )
                state.plan_tasks = tasks
                state.approval_index = len(state.messages) - 1
                logger.info(
                    f"Plan for session {state.session_id} approved "
                    f"({len(tasks)} task(s), {len(changes)} change(s) by the user)"
                )
                return plan_message

            if request.decision == plan_gate.DECISION_REVISE:
                text = plan_gate.revision_request(
                    proposed,
                    request.comment,
                    plan_gate.clean_comments(proposed, request.task_comments),
                    request.tasks,
                )
                await self.recorder.record_user_message(
                    db=db,
                    state=state,
                    content=text,
                    round_number=0,
                    on_event=on_event,
                    kind=turns.KIND_PLAN_REVISION,
                )
                waiting = plan_gate.revision_body(text)
                continue

            if request.decision == plan_gate.DECISION_STOPPED:
                return plan_message

            minutes = max(1, int(round(float(settings.timeout) / 60)))
            raise plan_gate.PlanApprovalExpired(
                f"계획 승인을 {minutes}분 동안 받지 못해 턴을 멈춰 두었습니다. 아무것도 실행되지 "
                f"않았습니다. '이어서 진행' 을 누르면 같은 계획으로 승인 카드가 다시 열립니다."
            )

    async def turn_config(self, db: Any, state: DebateState) -> Optional[Dict[str, Any]]:
        """턴 기록에 보존된 구성 정보의 현재 값을 조회합니다. 읽기에 실패하면 None을 반환합니다."""
        if not state.turn_id:
            return None
        try:
            row = (await db.execute(
                select(TurnModel.config).where(TurnModel.id == state.turn_id)
            )).first()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not read the config of turn {state.turn_id}: {exc}")
            return None
        return dict(row[0] or {}) if row is not None else None

    async def plan_proposal(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        plan_message: DebateMessage,
        specialists: List[Agent],
        *,
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """계획에 따른 전문가별 업무 분담표(태스크 목록)를 생성합니다. 이미 저장된 분담표가 있으면 재요청하지 않고 기존 값을 반환합니다."""
        config = await self.turn_config(db, state)
        saved = (config or {}).get("plan_gate") or {}
        if saved.get("plan_id") == plan_message.id:
            known = {a.key for a in specialists}
            tasks = [t for t in plan_gate.clean_tasks(saved.get("tasks")) if t["agent"] in known]
            if tasks:
                return tasks

        if on_event:
            await on_event({"type": EventType.PLAN_TASKS_STARTED, "agent_name": setup.orchestrator.name})
        try:
            tasks = await self.ask_orchestrator_for_plan_tasks(
                orchestrator=setup.orchestrator,
                specialists=specialists,
                state=state,
                plan_message=plan_message,
                custom_instructions=setup.custom_instructions,
            )
        except LLMUnavailableError as exc:
            logger.warning(f"Could not get the task list of the plan; showing blank rows: {exc}")
            tasks = plan_gate.blank_tasks(specialists)

        if config is not None:
            config["plan_gate"] = {"plan_id": plan_message.id, "tasks": tasks}
            await self.recorder.set_turn(db, state, config=config)
        return tasks

    async def ask_orchestrator_for_plan_tasks(
        self,
        *,
        orchestrator: Agent,
        specialists: List[Agent],
        state: DebateState,
        plan_message: DebateMessage,
        custom_instructions: str,
    ) -> List[Dict[str, Any]]:
        """계획 본문으로부터 전문가별 구체적 수행 태스크 목록을 추출합니다."""
        planner = self.conversation.tool_less(orchestrator)
        prompt = [{"role": "user", "content": plan_gate.tasks_prompt(
            state.user_prompt,
            strip_reasoning_trace(plan_message.content).strip(),
            format_roster(specialists, with_keys=True),
        )}]
        content, _ = await self.ctx.llm_caller.call_agent(
            planner,
            prompt,
            custom_instructions,
            session_id=state.session_id,
            mcp=self.ctx.mcp_for(state),
            tool_gate=self.ctx.gate_for(state),
            ledger=state.decision_ledger,
        )
        return plan_gate.parse_tasks(content, specialists)
