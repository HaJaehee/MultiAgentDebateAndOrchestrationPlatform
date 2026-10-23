"""병렬 지시 라운드: 오케스트레이터가 태스크를 나눠 주고, 맡은 전문가들이 동시에 발언한 뒤 취합합니다."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.database.models import utc_now
from app.events import EventCallback, EventType
from app.orchestration import turns
from app.orchestration.artifacts import format_roster
from app.orchestration.control import TurnControl
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.phases.routing import parse_assignments
from app.orchestration.state import DebateMessage, DebateState
from app.orchestration.strategies import BaseDebateStrategy

logger = logging.getLogger(__name__)


class ParallelRoundRunner(PhaseExecutor):
    """`parallel_dispatch` 전략의 한 라운드 — 분배, 동시 발언, 취합."""

    async def run_parallel_round(
        self,
        *,
        db: Any,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        active_agents: List[Agent],
        round_num: int,
        custom_instructions: str,
        parallel_limit: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        resume: Optional[turns.RoundRecord] = None,
        done: Optional[Set[str]] = None,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> bool:
        """한 라운드를 병렬로 실행합니다. 정지 요청으로 라운드가 중단되었으면 True를 반환합니다."""
        if resume is not None and resume.merged:
            return False
        redo = redo if redo is not None else {}
        done = set(done or ())
        resumed = resume is not None and resume.tasks is not None

        if resumed:
            by_key = {a.key: a for a in active_agents}
            assignments = [(by_key[key], task) for key, task in resume.tasks if key in by_key]
        else:
            candidates = strategy.get_speakers_for_round(active_agents, round_num, state)
            if not candidates:
                return False

            await self.recorder.apply_interjections(
                db=db, state=state, control=control, round_number=round_num, on_event=on_event
            )
            if control is not None and control.stop_requested:
                return True

            assignments = await self.dispatch_parallel_tasks(
                db=db,
                state=state,
                strategy=strategy,
                orchestrator=orchestrator,
                candidates=candidates,
                round_num=round_num,
                custom_instructions=custom_instructions,
                parallel_limit=parallel_limit,
                on_event=on_event,
            )
        if not assignments:
            return False

        board = self.assignment_board(assignments)
        todo = [i for i, (agent, _task) in enumerate(assignments) if agent.key not in done]
        prompt_state = state
        if resumed:
            prompt_state = state.model_copy(update={"messages": [
                m for m in state.messages
                if not (m.round_number == round_num and turns.kind_of(m) in (turns.KIND_SPEECH, turns.KIND_FAILURE))
            ]})
        prompts: Dict[int, List[Dict[str, Any]]] = {}
        for index in todo:
            agent, task = assignments[index]
            instruction = self.parallel_turn_instruction(strategy, agent, task, board)
            if agent.key in redo:
                instruction = f"{turns.resume_notice(redo.pop(agent.key))}\n\n{instruction}"
            prompts[index] = await self.conversation.context_for_speech(
                prompt_state, agent, instruction, orchestrator=orchestrator, on_event=on_event,
            )

        base_time = utc_now()
        db_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(parallel_limit)
        start_index = len(state.messages)

        async def run_one(index: int) -> DebateMessage:
            agent, _task = assignments[index]
            async with semaphore:
                return await self.speech.speak(
                    db=db,
                    state=state,
                    agent=agent,
                    prompt_messages=prompts[index],
                    custom_instructions=custom_instructions,
                    round_number=round_num,
                    msg_type="agent",
                    on_event=on_event,
                    control=control,
                    db_lock=db_lock,
                    created_at=base_time + timedelta(milliseconds=index),
                )

        if on_event and todo:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "debating",
                "speaker": " · ".join(assignments[i][0].name for i in todo),
                "round": round_num,
            })

        results = await asyncio.gather(*(run_one(i) for i in todo), return_exceptions=True)

        spoken = [r for r in results if isinstance(r, DebateMessage)]
        produced = state.messages[start_index:]
        if {m.id for m in spoken} == {m.id for m in produced}:
            state.messages[start_index:] = spoken

        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result

        for index, result in zip(todo, results):
            agent = assignments[index][0]
            if isinstance(result, BaseException):
                logger.error(
                    f"Parallel turn for '{agent.key}' failed: {type(result).__name__}: {result}",
                    exc_info=result,
                )
                if agent.key not in state.failed_agent_keys:
                    state.failed_agent_keys.append(agent.key)
                await self.recorder.record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=agent,
                    round_number=round_num,
                    msg_type="error",
                    content=(
                        f"> ⚠️ **{agent.name} 의 병렬 발언이 실패했습니다.**\n>\n"
                        f"> - 원인: `{type(result).__name__}: {result}`\n>\n"
                        f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다."
                    ),
                    kind=turns.KIND_FAILURE,
                )

        if control is not None and control.stop_requested:
            return True

        await self.merge_parallel_round(
            db=db,
            state=state,
            orchestrator=orchestrator,
            assignments=assignments,
            round_num=round_num,
            custom_instructions=custom_instructions,
            control=control,
            on_event=on_event,
        )
        return False

    async def dispatch_parallel_tasks(
        self,
        *,
        db: Any,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        candidates: List[Agent],
        round_num: int,
        custom_instructions: str,
        parallel_limit: int,
        on_event: Optional[EventCallback],
    ) -> List[Tuple[Agent, str]]:
        """이번 라운드의 에이전트별 태스크 분배 결과를 저장 및 반환합니다."""
        fallback = [(agent, "") for agent in candidates]

        async def _fall_back(why: str) -> List[Tuple[Agent, str]]:
            await self.recorder.record_note(
                db=db,
                state=state,
                on_event=on_event,
                agent=orchestrator,
                round_number=round_num,
                msg_type="error",
                content=(
                    f"[태스크 분배 실패] {why}\n"
                    f"태스크 없이 전원을 동시에 진행합니다: "
                    f"{', '.join(a.name for a in candidates)}"
                ),
                kind=turns.KIND_ASSIGNMENT,
                data={"tasks": [{"agent": a.key, "task": ""} for a in candidates], "fallback": True},
            )
            return fallback

        if len(candidates) <= 1:
            assignments = fallback
            reason = ""
        else:
            try:
                assignments, reason = await self.ask_orchestrator_for_assignments(
                    orchestrator=orchestrator,
                    candidates=candidates,
                    state=state,
                    round_num=round_num,
                    parallel_limit=parallel_limit,
                    custom_instructions=custom_instructions,
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Task dispatch failed; running everyone without tasks: {exc}")
                return await _fall_back(str(exc))

            if not assignments:
                logger.warning("Orchestrator assigned nobody we know; running everyone without tasks.")
                return await _fall_back(
                    "오케스트레이터의 응답에서 태스크를 맡길 에이전트를 찾지 못했습니다."
                )

        named = [agent for agent, _ in assignments]
        lines = [
            f"- **{agent.name}** ({agent.role}): {task or '(태스크 지정 없음 — 전문 영역에서 자유 기여)'}"
            for agent, task in assignments
        ]
        over_limit = len(assignments) > parallel_limit
        summary = (
            f"[Round {round_num} 병렬 지시] {len(assignments)}명에게 태스크를 나눴습니다"
            + (f" (동시 실행 상한 {parallel_limit} — 나머지는 순차적으로 대기 후 실행됩니다)" if over_limit else " (동시 실행)")
            + "\n" + "\n".join(lines)
        )
        skipped = [a.name for a in candidates if a not in named]
        if skipped:
            summary += f"\n\n(이번 라운드 미지명: {', '.join(skipped)})"
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
            kind=turns.KIND_ASSIGNMENT,
            data={"tasks": [{"agent": agent.key, "task": task} for agent, task in assignments]},
        )
        return assignments

    async def ask_orchestrator_for_assignments(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        parallel_limit: int,
        custom_instructions: str,
    ) -> Tuple[List[Tuple[Agent, str]], str]:
        """오케스트레이터에게 이번 라운드의 에이전트별 태스크 분배를 요청합니다."""
        planner = self.conversation.tool_less(orchestrator)

        roster = format_roster(candidates, with_keys=True)
        recent = [
            f"{m.speaker}: {self.conversation.snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            + self.conversation.routing_record(state, planner) +
            "[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[태스크를 맡길 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 이고, 지목된 에이전트는 "
            f"**동시에 각자의 태스크를 수행합니다**. 서로의 이번 라운드 결과를 볼 수 없으므로 "
            f"태스크가 겹치면 같은 일을 두 번 하게 됩니다.\n\n"
            f"겹치지 않게 태스크를 나누세요. 전원을 부를 필요는 없고, 한 명만 불러도 됩니다. "
            f"각 태스크는 다른 에이전트의 결과를 기다리지 않고 혼자 끝낼 수 있는 것이어야 하며, "
            f"무엇을 만들어 낼지(산출물)까지 한두 문장으로 적으세요. "
            f"동시 실행은 {parallel_limit}명까지이고 그보다 많이 부르면 나머지는 순차적으로 밀립니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            '{"assignments": [{"agent": "에이전트키", "task": "이 라운드에 맡길 구체적 태스크"}], '
            '"reason": "한두 문장으로 분배 사유"}'
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
        return parse_assignments(content, candidates)

    @staticmethod
    def assignment_board(assignments: List[Tuple[Agent, str]]) -> str:
        """동시에 도는 동료들이 무엇을 맡았는지 적은 판."""
        return "\n".join(
            f"- {agent.name}({agent.role}): {task or '(태스크 지정 없음)'}"
            for agent, task in assignments
        )

    @staticmethod
    def parallel_turn_instruction(
        strategy: BaseDebateStrategy, agent: Agent, task: str, board: str
    ) -> str:
        """병렬 라운드에서 한 에이전트에게 붙는 지침 = 내 태스크 + 동시 실행 현황."""
        if task:
            head = f"[병렬 지시] 오케스트레이터가 이번 라운드에 당신에게 맡긴 태스크입니다:\n{task}"
        else:
            head = strategy.turn_instruction(
                agent, [agent], 0, DebateState(session_id="", user_prompt="")
            )

        return (
            f"{head}\n\n"
            f"[동시 진행 중]\n{board}\n\n"
            f"이들은 지금 당신과 **같은 시각에** 답하고 있어 이번 라운드 결과를 볼 수 없습니다. "
            f"남의 태스크를 대신 하지 말고 당신 몫을 끝까지 마치세요. 다른 태스크의 결과가 필요하면 "
            f"추측해 채우지 말고 어떤 가정을 두었는지 명시하세요. 라운드 끝에 오케스트레이터가 "
            f"결과를 합칩니다."
        )

    async def merge_parallel_round(
        self,
        *,
        db: Any,
        state: DebateState,
        orchestrator: Agent,
        assignments: List[Tuple[Agent, str]],
        round_num: int,
        custom_instructions: str,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> DebateMessage:
        """라운드 끝의 취합 발언."""
        state.current_speaker = orchestrator.name
        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "debating",
                "speaker": f"{orchestrator.name} (취합)",
                "round": round_num,
            })

        board = self.assignment_board(assignments)
        instruction = (
            f"[Round {round_num} 취합] 방금 다음 에이전트가 **동시에** 각자의 태스크를 수행했습니다:\n"
            f"{board}\n\n"
            f"이들은 서로의 결과를 보지 못한 채 답했습니다. 수석 오케스트레이터로서 "
            f"이번 라운드의 결과를 하나로 붙이세요:\n"
            f"1. 통합된 현재 결론 (무엇이 정해졌는가)\n"
            f"2. 서로 어긋나는 지점과 그 판정 (누구 말이 맞는지, 아직 판단할 수 없다면 그 이유)\n"
            f"3. 각자가 세운 가정 중 아직 검증되지 않은 것\n"
            f"4. 다음 라운드로 넘길 미해결 과제\n\n"
            f"발언하지 못했거나 실패한 에이전트의 몫을 지어내지 마세요."
        )

        return await self.speech.speak(
            db=db,
            state=state,
            agent=orchestrator,
            prompt_messages=await self.conversation.context_for_speech(
                state, orchestrator, instruction, orchestrator=orchestrator, on_event=on_event,
            ),
            custom_instructions=custom_instructions,
            round_number=round_num,
            msg_type="orchestrator",
            on_event=on_event,
            control=control,
            kind=turns.KIND_MERGE,
        )
