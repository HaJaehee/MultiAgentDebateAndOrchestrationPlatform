"""2단계: 전문가 토론.

라운드를 돌리며 전략에 따라 발언 순서를 정하고(오케스트레이터 지명 포함), 라운드 사이에 결정 장부를
갱신합니다. 병렬 지시 라운드는 `phases/parallel.py`, 그래프 토론은 `phases/graph.py`, 오케스트레이터가 그리는
그래프는 `phases/graph_drafting.py`, 라운드·단계 사이의 서브 토론은 `phases/sub_debate.py` 가 맡습니다.

각 라운드 종료 시점(`_close_round`)에 조기 종료 조건을 평가합니다 (v1.5): 결정 장부가 수렴했거나
(`context_memory.ledger_converged`), 턴 토큰 예산이 소진되었으면 이를 세션에 기록하고 조기 결론 합성으로 진행합니다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.llm_context import context_budget
from app.agents.llm_types import LLMUnavailableError
from app.agents.reasoning import strip_reasoning_trace
from app.config import ORCHESTRATOR_KEY
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory, diagnostic_loop, turns
from app.orchestration.artifacts import format_roster
from app.orchestration.control import TurnControl
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.phases.graph import GraphRunner
from app.orchestration.phases.graph_drafting import GraphDrafter
from app.orchestration.phases.parallel import ParallelRoundRunner
from app.orchestration.phases.routing import parse_speaker_selection
from app.orchestration.phases.sub_debate import SubDebateRunner
from app.orchestration.state import DebateState
from app.orchestration.strategies import (
    BaseDebateStrategy,
    get_strategy,
    instruction_for_turn,
    speakers_for_round,
)

if TYPE_CHECKING:
    from app.orchestration.turn_setup import TurnSetup

logger = logging.getLogger(__name__)


class DebatePhaseExecutor(PhaseExecutor):
    """전문가 토론 라운드(순차·대립·오케스트레이터 지명·병렬 지시·그래프 토론) 실행을 전담합니다."""

    def __init__(self, *services) -> None:
        super().__init__(*services)
        # 라운드를 병렬로 도는 전략과 그래프로 도는 전략은 각자의 실행기가 맡습니다.
        self.parallel = ParallelRoundRunner(*services)
        self.graph = GraphRunner(*services)
        self.drafter = GraphDrafter(*services)
        self.sub_debates = SubDebateRunner(*services)

    async def debate(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        start_value: List[str],
        resume: bool = False,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        mark_boundary: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> bool:
        """전문가 토론 라운드를 진행합니다. 사용자가 중단 요청을 한 경우 True를 반환합니다.

        `mark_boundary(name)` 콜백은 라운드/단계 개시 시점에 호출됩니다 (작업 공간 경계 스냅샷 기록 및 슬롯 양보, ADR-043/044).
        """
        redo = dict(redo or {})
        strategy = get_strategy(setup.strategy_name)
        orchestrator_agent = setup.orchestrator
        max_rounds = setup.max_rounds
        state.status = "debating"

        await self.recorder.apply_interjections(
            db=db, state=state, control=control, round_number=0, on_event=on_event
        )

        async def _sub_debate(boundary: int) -> None:
            await self.sub_debates.run_if_needed(
                db, state, setup, boundary, control=control, on_event=on_event, redo=redo,
            )

        # 오케스트레이터가 그리는 전략은 여기서 그래프가 정해집니다. 이어 가는 턴은 기록된 그래프를 씁니다.
        if strategy.draws_graph and setup.graph_spec is None:
            if control is not None and control.stop_requested:
                return True
            setup.graph_spec = await self.drafter.graph_for_turn(db, state, setup, on_event=on_event)

        stopped_early = False
        if setup.graph_spec is not None:
            replay = None
            if resume:
                replay = turns.replay_graph(
                    setup.graph_spec,
                    max_rounds,
                    start_value,
                    state.messages[state.turn_message_start:],
                )
            return await self.graph.run_graph(
                db=db,
                state=state,
                spec=setup.graph_spec,
                orchestrator=orchestrator_agent,
                active_agents=setup.active_agents,
                parallel_limit=setup.parallel_limit,
                max_visits=max_rounds,
                control=control,
                on_event=on_event,
                start_value=start_value,
                replay=replay,
                redo=redo,
                between_steps=_sub_debate,
                mark_boundary=mark_boundary,
            )

        start_round, spoken, record = 1, set(), turns.RoundRecord()
        if resume:
            turn_messages = state.messages[state.turn_message_start:]
            # 끊기기 전에 이미 남은 라운드를 건너뛰기로 했으면 토론은 끝난 것입니다.
            early = turns.early_end(turn_messages)
            if early is not None:
                return early[0] == turns.EARLY_END_BUDGET
            last_round, spoken_keys = turns.round_progress(turn_messages)
            if last_round:
                start_round, spoken = last_round, spoken_keys
                record = turns.round_record(turn_messages, last_round)

        # 직전 라운드 종료 시 갱신된 결정 장부. 수렴 판정은 두 라운드의 장부를 비교합니다.
        round_ledger: Optional[str] = None
        round_ledgers: Dict[int, str] = (
            diagnostic_loop.round_ledgers_from_messages(
                state.messages[state.turn_message_start:],
                fallback_round=start_round - 1,
                fallback_ledger=state.decision_ledger,
            )
            if resume
            else {}
        )

        for round_num in range(start_round, max_rounds + 1):
            resuming_round = resume and round_num == start_round
            if control is not None and control.stop_requested:
                stopped_early = True
                break

            state.current_round = round_num
            if mark_boundary is not None:
                await mark_boundary(f"round-{round_num}")
            if on_event:
                await on_event({
                    "type": EventType.ROUND_STARTED,
                    "round": round_num,
                    "max_rounds": max_rounds,
                })

            if strategy.orchestrator_dispatches_parallel:
                stopped_early = await self.parallel.run_parallel_round(
                    db=db,
                    state=state,
                    strategy=strategy,
                    orchestrator=orchestrator_agent,
                    active_agents=setup.active_agents,
                    round_num=round_num,
                    custom_instructions=setup.custom_instructions,
                    parallel_limit=setup.parallel_limit,
                    control=control,
                    on_event=on_event,
                    resume=record if resuming_round else None,
                    done=spoken if resuming_round else None,
                    redo=redo,
                )
                if stopped_early:
                    break
                if round_num < max_rounds and not (control is not None and control.stop_requested):
                    ending, round_ledger = await self._close_round(
                        db,
                        state,
                        setup,
                        round_num,
                        on_event=on_event,
                        previous=round_ledger,
                        round_ledgers=round_ledgers,
                        control=control,
                    )
                    if ending is not None:
                        stopped_early = ending == turns.EARLY_END_BUDGET
                        break
                    await _sub_debate(round_num)
                continue

            if resuming_round and record.nominated is not None:
                by_key = {a.key: a for a in setup.active_agents}
                speakers = [by_key[key] for key in record.nominated if key in by_key]
            else:
                speakers = await self.select_speakers(
                    db=db,
                    state=state,
                    strategy=strategy,
                    orchestrator=orchestrator_agent,
                    active_agents=setup.active_agents,
                    round_num=round_num,
                    custom_instructions=setup.custom_instructions,
                    on_event=on_event,
                )

            skip = spoken if resuming_round else set()
            for speaker_index, agent in enumerate(speakers):
                if agent.key in skip:
                    continue
                await self.recorder.apply_interjections(
                    db=db,
                    state=state,
                    control=control,
                    round_number=round_num,
                    on_event=on_event,
                )
                if control is not None and control.stop_requested:
                    stopped_early = True
                    break

                state.current_speaker = agent.name
                if on_event:
                    await on_event({
                        "type": EventType.STATUS_CHANGED,
                        "status": "debating",
                        "speaker": agent.name,
                        "round": round_num,
                    })

                instruction = instruction_for_turn(strategy, agent, speakers, speaker_index, state)
                if agent.key in redo:
                    instruction = f"{turns.resume_notice(redo.pop(agent.key))}\n\n{instruction}".strip()
                await self.speech.speak(
                    db=db,
                    state=state,
                    agent=agent,
                    prompt_messages=await self.conversation.context_for_speech(
                        state,
                        agent,
                        instruction,
                        orchestrator=orchestrator_agent,
                        on_event=on_event,
                    ),
                    custom_instructions=setup.custom_instructions,
                    round_number=round_num,
                    msg_type="agent",
                    on_event=on_event,
                    control=control,
                )

            if stopped_early:
                break

            if round_num < max_rounds and not (control is not None and control.stop_requested):
                ending, round_ledger = await self._close_round(
                    db,
                    state,
                    setup,
                    round_num,
                    on_event=on_event,
                    previous=round_ledger,
                    round_ledgers=round_ledgers,
                    control=control,
                )
                if ending is not None:
                    stopped_early = ending == turns.EARLY_END_BUDGET
                    break
                await _sub_debate(round_num)
        return stopped_early

    async def _close_round(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        round_num: int,
        *,
        on_event: Optional[EventCallback],
        previous: Optional[str],
        round_ledgers: Optional[Dict[int, str]] = None,
        control: Optional[TurnControl] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """라운드를 종료하고 결정 장부를 갱신한 뒤, 조기 종료 조건을 검사합니다.

        반환값: (조기 종료 사유, 현재 라운드 결정 장부). 사유는 `turns.EARLY_END_*` 상수 또는 None입니다. 장부
        갱신에 실패하면 현재 장부는 None이 되며, 다음 라운드에서 수렴 판정을 건너뜁니다.

        * 예산 초과(budget): 턴 토큰 예산(`token_budget`) 한도에 도달하여 조기 종료합니다 (미완료/불완전 논의 처리).
        * 합의 수렴(converged): 직전 라운드와 현재 라운드의 결정 장부 모두 미해결 쟁점이 없고 추가 결정 사항이
          발생하지 않아 조기 종료합니다 (합의 완료로 정상 처리).
        """
        updated = await self.conversation.update_ledger(
            state=state, orchestrator=setup.orchestrator, on_event=on_event, reason=f"Round {round_num}",
        )
        current = state.decision_ledger if updated else None
        if current is not None and round_ledgers is not None:
            round_ledgers[round_num] = current
        remaining = setup.max_rounds - round_num
        used = state.usage.total_tokens
        if setup.token_budget and used >= setup.token_budget:
            reason = turns.EARLY_END_BUDGET
            state.budget_stopped = True
            content = (
                f"[토큰 예산] 이번 턴에서 소모한 토큰이 {used:,}개로 설정 예산 {setup.token_budget:,}개에 도달했습니다. "
                f"남은 {remaining}라운드를 건너뛰고 지금까지의 토론 내용을 바탕으로 결론을 합성합니다."
            )
        elif (
            setup.converge_early and current is not None and previous is not None
            and memory.ledger_converged(previous, current)
        ):
            reason = turns.EARLY_END_CONVERGED
            state.converged_round = round_num
            content = (
                f"[합의 판정] Round {round_num - 1}·{round_num}의 결정 장부에 미해결 쟁점이 없고 새로운 결정 사항도 "
                f"추가되지 않았습니다. 남은 {remaining}라운드를 건너뛰고 결론 합성으로 진행합니다."
            )
        else:
            if not (control is not None and control.stop_requested):
                stances = {a.key: a.debate_stance for a in setup.active_agents}
                turn_messages = state.messages[state.turn_message_start:]
                # 재설계 계획을 쓰다가 끊긴 턴을 이어 가는 중이면 안내는 이미 남았으니 계획만 다시 씁니다.
                report = diagnostic_loop.pending_redesign(turn_messages, round_num)
                if report is None:
                    report = diagnostic_loop.observe_stagnation(
                        turn_messages,
                        round_num=round_num,
                        ledger_by_round=round_ledgers,
                        stances_by_key=stances,
                    )
                    if report is not None:
                        await self._record_stagnation(
                            db, state, setup, report,
                            remaining_rounds=remaining, current_ledger=current, on_event=on_event,
                        )
                if report is not None:
                    await self._redesign_plan(
                        db,
                        state,
                        setup,
                        report,
                        remaining_rounds=remaining,
                        current_ledger=current,
                        stances=stances,
                        on_event=on_event,
                        control=control,
                    )
            return None, current

        logger.info(f"Session {state.session_id}: ending the debate after round {round_num} ({reason})")
        await self.recorder.record_note(
            db=db,
            state=state,
            on_event=on_event,
            agent=setup.orchestrator,
            round_number=round_num,
            msg_type="orchestrator",
            content=content,
            kind=turns.KIND_EARLY_END,
            data={"reason": reason, "round": round_num},
        )
        if on_event:
            await on_event({
                "type": EventType.DEBATE_ENDED_EARLY,
                "reason": reason,
                "round": round_num,
                "max_rounds": setup.max_rounds,
            })
        return reason, current

    async def _record_stagnation(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        report: diagnostic_loop.StagnationReport,
        *,
        remaining_rounds: int,
        current_ledger: Optional[str],
        on_event: Optional[EventCallback],
    ) -> None:
        """외부 관찰자가 감지한 정체를 안내로 남기고 알립니다 (ADR-049). 이 안내가 같은 라운드의 중복 판정을 막습니다."""
        logger.info(
            f"Session {state.session_id}: escalating to redesign after round {report.round_number} "
            f"(streak={report.streak}, signatures={list(report.signatures)})"
        )
        meta = report.as_meta()
        if current_ledger:
            meta["ledger"] = current_ledger
        await self.recorder.record_note(
            db=db,
            state=state,
            on_event=on_event,
            agent=setup.orchestrator,
            round_number=report.round_number,
            msg_type="orchestrator",
            content=diagnostic_loop.format_stagnation_note(report, remaining_rounds),
            kind=turns.KIND_STAGNATION,
            data=meta,
        )
        if on_event:
            await on_event({
                "type": EventType.STAGNATION_DETECTED,
                **report.as_meta(),
                "max_rounds": setup.max_rounds,
            })

    async def _redesign_plan(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        report: diagnostic_loop.StagnationReport,
        *,
        remaining_rounds: int,
        current_ledger: Optional[str],
        stances: Dict[str, str],
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
    ) -> None:
        """오케스트레이터가 정체를 끊을 재설계 계획을 써서 기존 계획을 대체합니다 (ADR-049).

        참여 에이전트들의 설득형 발언 원문은 프롬프트에서 격리하고, 원래 요구사항·실행 실패 증거·시도 이력만
        넘깁니다. 계획 승인(ADR-028)은 토론 전의 관문이라 여기서는 다시 묻지 않습니다. 승인은 그것이 가리키던
        계획에만 유효하므로 승인된 분담도 함께 내려놓습니다 (`turn_setup.restore_turn_state` 와 같은 기준).
        """
        planner = self.conversation.tool_less(setup.orchestrator)
        state.status = "planning"
        state.current_speaker = planner.name
        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "planning",
                "speaker": planner.name,
                "round": report.round_number,
            })

        user_record = memory.build_user_record(
            state,
            model=planner.model,
            token_cap=int(context_budget(planner) * memory.USER_RECORD_SHARE),
        ).text
        previous_plan = (
            strip_reasoning_trace(state.messages[state.plan_index].content).strip()
            if state.plan_index is not None and 0 <= state.plan_index < len(state.messages)
            else ""
        )
        turn_messages = state.messages[state.turn_message_start:]
        ledgers = diagnostic_loop.round_ledgers_from_messages(
            turn_messages,
            fallback_round=report.round_number,
            fallback_ledger=current_ledger or state.decision_ledger,
        )
        attempts = diagnostic_loop.collect_round_attempts(
            turn_messages,
            up_to_round=report.round_number,
            ledger_by_round=ledgers,
            stances_by_key=stances,
        )
        attempt_cap = memory.memory_cap(
            diagnostic_loop.ATTEMPT_HISTORY_SHARE,
            diagnostic_loop.ATTEMPT_HISTORY_MAX_CHARS,
            state.memory_budget or context_budget(planner),
        )
        attempt_history = diagnostic_loop.format_attempt_history(attempts, max_chars=attempt_cap)
        specialists = [a for a in setup.active_agents if a.key != ORCHESTRATOR_KEY]
        roster_block = format_roster(specialists) if specialists else "(전문가 없음)"

        redesigned = await self.speech.speak(
            db=db,
            state=state,
            agent=planner,
            prompt_messages=diagnostic_loop.redesign_prompt(
                user_prompt=state.user_prompt,
                user_record=user_record,
                previous_plan=previous_plan,
                report=report,
                attempt_history=attempt_history,
                ledger=current_ledger or state.decision_ledger,
                roster_block=roster_block,
                remaining_rounds=remaining_rounds,
            ),
            custom_instructions=setup.custom_instructions,
            round_number=report.round_number,
            msg_type="orchestrator",
            on_event=on_event,
            control=control,
            kind=turns.KIND_PLAN,
        )
        state.status = "debating"
        if redesigned.msg_type != "error":
            state.plan_index = len(state.messages) - 1
            state.plan_tasks = []
            state.approval_index = None

    async def select_speakers(
        self,
        *,
        db: Any,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        active_agents: List[Agent],
        round_num: int,
        custom_instructions: str,
        on_event: Optional[EventCallback],
    ) -> List[Agent]:
        """이번 라운드의 발언 대상자와 발언 순서를 결정합니다."""
        fallback, problem = speakers_for_round(strategy, active_agents, round_num, state)

        async def _fall_back(why: str) -> List[Agent]:
            await self.recorder.record_note(
                db=db,
                state=state,
                on_event=on_event,
                agent=orchestrator,
                round_number=round_num,
                msg_type="error",
                content=(
                    f"[발언자 지명 실패] {why}\n"
                    f"우선순위 순서로 진행합니다: {', '.join(a.name for a in fallback)}"
                ),
                kind=turns.KIND_NOMINATION,
                data={"speakers": [a.key for a in fallback], "fallback": True},
            )
            return fallback

        if problem:
            return await _fall_back(problem)
        if not strategy.orchestrator_selects_speakers or len(fallback) <= 1:
            return fallback

        try:
            picked, reason = await self.ask_orchestrator_for_speakers(
                orchestrator=orchestrator,
                candidates=fallback,
                state=state,
                round_num=round_num,
                custom_instructions=custom_instructions,
            )
        except LLMUnavailableError as exc:
            logger.warning(f"Speaker selection failed; using the deterministic order: {exc}")
            return await _fall_back(str(exc))

        if not picked:
            logger.warning("Orchestrator named no known agent; using the deterministic order.")
            return await _fall_back(
                "오케스트레이터의 응답에서 이번 라운드에 부를 에이전트를 찾지 못했습니다."
            )

        skipped = [a.name for a in fallback if a not in picked]
        summary = f"[Round {round_num} 발언권] {' → '.join(a.name for a in picked)}"
        if skipped:
            summary += f"\n(이번 라운드 미지명: {', '.join(skipped)})"
        if reason:
            summary += f"\n\n{reason}"
        await self.recorder.record_note(
            db=db,
            state=state,
            on_event=on_event,
            agent=orchestrator,
            round_number=round_num,
            msg_type="orchestrator",
            content=summary,
            kind=turns.KIND_NOMINATION,
            data={"speakers": [a.key for a in picked]},
        )
        return picked

    async def ask_orchestrator_for_speakers(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        custom_instructions: str,
    ) -> Tuple[List[Agent], str]:
        """오케스트레이터에게 이번 라운드의 발언 대상자 지명 및 순서 결정을 요청합니다."""
        selector = self.conversation.tool_less(orchestrator)

        roster = format_roster(candidates, with_keys=True)
        recent = [
            f"{m.speaker}: {self.conversation.snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error" and turns.in_main_record(m)
        ][-8:]

        decision_caller = getattr(self.ctx.llm_caller, "decision_caller", None)
        if orchestrator.decision_llm.enabled and decision_caller is not None:
            state_parts = [
                f"[목표]\n{state.user_prompt}",
                self.conversation.routing_record(state, selector).strip(),
                "[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)"),
            ]
            if state.decision_ledger:
                state_parts.append(f"[결정 장부]\n{state.decision_ledger}")
            if custom_instructions and custom_instructions.strip():
                state_parts.append(f"[추가 지시]\n{custom_instructions.strip()}")
            return await decision_caller.select_speakers(
                orchestrator,
                "\n\n".join(part for part in state_parts if part),
                candidates,
                round_num,
                state.max_rounds,
            )

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            + self.conversation.routing_record(state, selector) +
            "[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[이번 라운드에 부를 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 입니다. 논의를 진전시키기 위해 "
            f"이번 라운드에 **꼭 필요한 에이전트만** 골라 발언 순서를 정하세요. 전원을 부를 "
            f"필요는 없고, 한 명만 불러도 됩니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            f'{{"speakers": ["에이전트키", ...], "reason": "한두 문장으로 지명 사유"}}'
        )}]

        content, _ = await self.ctx.llm_caller.call_agent(
            selector,
            prompt,
            custom_instructions,
            session_id=state.session_id,
            mcp=self.ctx.mcp_for(state),
            tool_gate=self.ctx.gate_for(state),
            ledger=state.decision_ledger,
        )
        return parse_speaker_selection(content, candidates)

