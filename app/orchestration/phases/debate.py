"""2단계: 전문가 토론.

라운드를 돌리며 전략에 따라 발언 순서를 정하고(오케스트레이터 지명 포함), 라운드 사이에 결정 장부를
갱신합니다. 병렬 지시 라운드는 `phases/parallel.py`, 그래프 토론은 `phases/graph.py` 가 맡습니다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.events import EventCallback, EventType
from app.orchestration import turns
from app.orchestration.artifacts import format_roster
from app.orchestration.control import TurnControl
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.phases.graph import GraphRunner
from app.orchestration.phases.parallel import ParallelRoundRunner
from app.orchestration.phases.routing import parse_speaker_selection
from app.orchestration.state import DebateState
from app.orchestration.strategies import BaseDebateStrategy, get_strategy

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
    ) -> bool:
        """전문가 토론 라운드를 진행합니다. 사용자가 중단 요청을 한 경우 True를 반환합니다."""
        redo = dict(redo or {})
        strategy = get_strategy(setup.strategy_name)
        orchestrator_agent = setup.orchestrator
        max_rounds = setup.max_rounds
        state.status = "debating"

        await self.recorder.apply_interjections(
            db=db, state=state, control=control, round_number=0, on_event=on_event
        )

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
            )

        start_round, spoken, record = 1, set(), turns.RoundRecord()
        if resume:
            turn_messages = state.messages[state.turn_message_start:]
            last_round, spoken_keys = turns.round_progress(turn_messages)
            if last_round:
                start_round, spoken = last_round, spoken_keys
                record = turns.round_record(turn_messages, last_round)

        for round_num in range(start_round, max_rounds + 1):
            resuming_round = resume and round_num == start_round
            if control is not None and control.stop_requested:
                stopped_early = True
                break

            state.current_round = round_num
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
                    await self.conversation.update_ledger(
                        state=state,
                        orchestrator=orchestrator_agent,
                        on_event=on_event,
                        reason=f"Round {round_num}",
                    )
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

                instruction = strategy.turn_instruction(agent, speakers, speaker_index, state)
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
                await self.conversation.update_ledger(
                    state=state,
                    orchestrator=orchestrator_agent,
                    on_event=on_event,
                    reason=f"Round {round_num}",
                )
        return stopped_early

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
        fallback = strategy.get_speakers_for_round(active_agents, round_num, state)
        if not strategy.orchestrator_selects_speakers or len(fallback) <= 1:
            return fallback

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
            for m in state.messages if m.msg_type != "error"
        ][-8:]

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

