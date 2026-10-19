"""Phase 2: Multi-Round Specialist Debate, Parallel Dispatch, and Graph Execution.

`OrchestratorEngine`에서 라운드별 토론 진행(`_debate`), 발언자 지명, 병렬 라운드 실행,
그래프 기반 토론 실행을 전담하는 `DebatePhaseExecutor` 모듈입니다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

from sqlalchemy import update

from app.agents.base import Agent
from app.agents.llm import LLMUnavailableError, context_budget, strip_reasoning_trace
from app.database.models import SessionModel, utc_now
from app.orchestration import context_memory as memory
from app.orchestration import turns
from app.orchestration.artifacts import format_roster
from app.orchestration.control import TurnControl
from app.orchestration.graph import (
    CARRY_LABELS,
    Activation,
    GraphScheduler,
    GraphSpec,
    validate_graph,
)
from app.orchestration.state import DebateMessage, DebateState
from app.orchestration.strategies import BaseDebateStrategy, get_strategy

if TYPE_CHECKING:
    from app.orchestration.engine import OrchestratorEngine, TurnSetup

logger = logging.getLogger(__name__)

EventCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class GraphTurnError(RuntimeError):
    """그래프 토론을 시작할 수 없을 때 (그래프 없음 · 파일 없음 · 검증 오류). 사람이 읽을 문장입니다."""


def parse_gate_decision(content: str) -> Optional[Tuple[str, str]]:
    """판정 응답에서 (yes|no, 사유). 읽지 못하면 None."""
    text = strip_reasoning_trace(content or "").strip()
    block = re.search(r"\{.*\}", text, re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            raw = str(data.get("decision", "")).strip().lower()
            reason = str(data.get("reason") or "").strip()
            if raw in ("yes", "y", "예", "true"):
                return "yes", reason
            if raw in ("no", "n", "아니오", "아니요", "false"):
                return "no", reason
    first = text.splitlines()[0].strip().strip("*#:. ").lower() if text else ""
    if first in ("yes", "예"):
        return "yes", ""
    if first in ("no", "아니오", "아니요"):
        return "no", ""
    return None


class DebatePhaseExecutor:
    """전문가 토론 라운드(순차·대립·오케스트레이터 지명·병렬 지시·그래프 토론) 실행을 전담합니다."""

    def __init__(self, engine: "OrchestratorEngine") -> None:
        self.engine = engine

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
        """전문가 토론 라운드. 사람이 정지시켰으면 True."""
        redo = dict(redo or {})
        strategy = get_strategy(setup.strategy_name)
        orchestrator_agent = setup.orchestrator
        max_rounds = setup.max_rounds
        state.status = "debating"

        await self.engine._apply_interjections(
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
            return await self.engine._run_graph(
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
                    "type": "round_started",
                    "round": round_num,
                    "max_rounds": max_rounds,
                })

            if strategy.orchestrator_dispatches_parallel:
                stopped_early = await self.engine._run_parallel_round(
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
                    await self.engine._update_ledger(
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
                speakers = await self.engine._select_speakers(
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
                await self.engine._apply_interjections(
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
                        "type": "status_changed",
                        "status": "debating",
                        "speaker": agent.name,
                        "round": round_num,
                    })

                instruction = strategy.turn_instruction(agent, speakers, speaker_index, state)
                if agent.key in redo:
                    instruction = f"{turns.resume_notice(redo.pop(agent.key))}\n\n{instruction}".strip()
                await self.engine._speak(
                    db=db,
                    state=state,
                    agent=agent,
                    prompt_messages=await self.engine._context_for_speech(
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
                await self.engine._update_ledger(
                    state=state,
                    orchestrator=orchestrator_agent,
                    on_event=on_event,
                    reason=f"Round {round_num}",
                )
        return stopped_early

    # ------------------------------------------------------------ 발언자 선정

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
        """이번 라운드에 누가 발언할지 정합니다."""
        fallback = strategy.get_speakers_for_round(active_agents, round_num, state)
        if not strategy.orchestrator_selects_speakers or len(fallback) <= 1:
            return fallback

        async def _fall_back(why: str) -> List[Agent]:
            await self.engine._record_note(
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
            picked, reason = await self.engine._ask_orchestrator_for_speakers(
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
        await self.engine._record_note(
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
        """오케스트레이터에게 이번 라운드 발언자와 순서를 물어봅니다."""
        selector = self.engine._tool_less(orchestrator)

        roster = format_roster(candidates, with_keys=True)
        recent = [
            f"{m.speaker}: {self.engine._snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            + self.engine._routing_record(state, selector) +
            f"[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[이번 라운드에 부를 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 입니다. 논의를 진전시키기 위해 "
            f"이번 라운드에 **꼭 필요한 에이전트만** 골라 발언 순서를 정하세요. 전원을 부를 "
            f"필요는 없고, 한 명만 불러도 됩니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            f'{{"speakers": ["에이전트키", ...], "reason": "한두 문장으로 지명 사유"}}'
        )}]

        content, _ = await self.engine.llm_caller.call_agent(
            selector,
            prompt,
            custom_instructions,
            session_id=state.session_id,
            mcp=self.engine._mcp_for(state),
            tool_gate=self.engine._gate_for(state),
            ledger=state.decision_ledger,
        )
        return self.engine._parse_speaker_selection(content, candidates)

    @staticmethod
    def parse_speaker_selection(
        content: str, candidates: List[Agent]
    ) -> Tuple[List[Agent], str]:
        """응답에서 지명된 에이전트와 사유를 뽑습니다."""
        by_key = {a.key: a for a in candidates}
        reason = ""
        keys: List[str] = []

        block = re.search(r"\{.*\}", content or "", re.DOTALL)
        if block:
            try:
                data = json.loads(block.group(0))
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                raw = data.get("speakers")
                if isinstance(raw, list):
                    keys = [str(k).strip() for k in raw]
                reason = str(data.get("reason") or "").strip()

        if not any(k in by_key for k in keys):
            found = []
            for key in by_key:
                match = re.search(rf"\b{re.escape(key)}\b", content or "")
                if match:
                    found.append((match.start(), key))
            keys = [key for _, key in sorted(found)]

        picked: List[Agent] = []
        for key in keys:
            agent = by_key.get(key)
            if agent is not None and agent not in picked:
                picked.append(agent)
        return picked, reason

    # ------------------------------------------------------- 병렬 지시 라운드

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
        """한 라운드를 병렬로 돕니다. 정지 요청으로 라운드를 접었으면 True."""
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

            await self.engine._apply_interjections(
                db=db, state=state, control=control, round_number=round_num, on_event=on_event
            )
            if control is not None and control.stop_requested:
                return True

            assignments = await self.engine._dispatch_parallel_tasks(
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

        board = self.engine._assignment_board(assignments)
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
            instruction = self.engine._parallel_turn_instruction(strategy, agent, task, board)
            if agent.key in redo:
                instruction = f"{turns.resume_notice(redo.pop(agent.key))}\n\n{instruction}"
            prompts[index] = await self.engine._context_for_speech(
                prompt_state, agent, instruction, orchestrator=orchestrator, on_event=on_event,
            )

        base_time = utc_now()
        db_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(parallel_limit)
        start_index = len(state.messages)

        async def run_one(index: int) -> DebateMessage:
            agent, _task = assignments[index]
            async with semaphore:
                return await self.engine._speak(
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
                "type": "status_changed",
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
                await self.engine._record_note(
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

        await self.engine._merge_parallel_round(
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
        """이번 라운드의 태스크 분배를 받아 기록하고 돌려줍니다."""
        fallback = [(agent, "") for agent in candidates]

        async def _fall_back(why: str) -> List[Tuple[Agent, str]]:
            await self.engine._record_note(
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
                assignments, reason = await self.engine._ask_orchestrator_for_assignments(
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
            + (f" (동시 실행 상한 {parallel_limit} — 나머지는 순차적으로 밀립니다)" if over_limit else " (동시 실행)")
            + "\n" + "\n".join(lines)
        )
        skipped = [a.name for a in candidates if a not in named]
        if skipped:
            summary += f"\n\n(이번 라운드 미지명: {', '.join(skipped)})"
        if reason:
            summary += f"\n\n{reason}"
        await self.engine._record_note(
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
        """오케스트레이터에게 이번 라운드의 태스크 분배를 물어봅니다."""
        planner = self.engine._tool_less(orchestrator)

        roster = format_roster(candidates, with_keys=True)
        recent = [
            f"{m.speaker}: {self.engine._snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            + self.engine._routing_record(state, planner) +
            f"[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
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

        content, _ = await self.engine.llm_caller.call_agent(
            planner,
            prompt,
            custom_instructions,
            session_id=state.session_id,
            mcp=self.engine._mcp_for(state),
            tool_gate=self.engine._gate_for(state),
            ledger=state.decision_ledger,
        )
        return self.engine._parse_assignments(content, candidates)

    @staticmethod
    def parse_assignments(
        content: str, candidates: List[Agent]
    ) -> Tuple[List[Tuple[Agent, str]], str]:
        """응답에서 (에이전트, 태스크) 목록과 분배 사유를 뽑습니다."""
        by_key = {a.key: a for a in candidates}
        reason = ""
        pairs: List[Tuple[str, str]] = []

        block = re.search(r"\{.*\}", content or "", re.DOTALL)
        if block:
            try:
                data = json.loads(block.group(0))
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                reason = str(data.get("reason") or "").strip()
                raw = data.get("assignments")
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            key = str(item.get("agent") or item.get("key") or "").strip()
                            task = str(item.get("task") or item.get("instruction") or "").strip()
                            pairs.append((key, task))
                        elif isinstance(item, str):
                            pairs.append((item.strip(), ""))

        if not any(key in by_key for key, _ in pairs):
            picked, scraped = DebatePhaseExecutor.parse_speaker_selection(content, candidates)
            reason = reason or scraped
            pairs = [(a.key, "") for a in picked]

        assignments: List[Tuple[Agent, str]] = []
        seen = set()
        for key, task in pairs:
            agent = by_key.get(key)
            if agent is None or agent.key in seen:
                continue
            seen.add(agent.key)
            assignments.append((agent, task))
        return assignments, reason

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
                "type": "status_changed",
                "status": "debating",
                "speaker": f"{orchestrator.name} (취합)",
                "round": round_num,
            })

        board = self.engine._assignment_board(assignments)
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

        return await self.engine._speak(
            db=db,
            state=state,
            agent=orchestrator,
            prompt_messages=await self.engine._context_for_speech(
                state, orchestrator, instruction, orchestrator=orchestrator, on_event=on_event,
            ),
            custom_instructions=custom_instructions,
            round_number=round_num,
            msg_type="orchestrator",
            on_event=on_event,
            control=control,
            kind=turns.KIND_MERGE,
        )

    # ------------------------------------------------------------ 그래프 토론

    async def graph_for_turn(
        self, db: Any, session_model: SessionModel, max_rounds: int
    ) -> GraphSpec:
        """이 턴에 쓸 그래프를 읽고 검사하고, 고정해 둡니다. 쓸 수 없으면 `GraphTurnError`."""
        from app.agents.personas import frozen_agents
        from app.config import get_config
        from app.graph_store import load_graph

        graph_id = (session_model.graph_id or "").strip()
        if not graph_id:
            raise GraphTurnError(
                "그래프 토론에 쓸 그래프가 없습니다. 에이전트 패널의 그래프 선택 메뉴에서 선택하거나 "
                "'카드 순서로 만들기'로 생성해 주십시오."
            )
        try:
            spec = load_graph(graph_id)
        except FileNotFoundError:
            raise GraphTurnError(f"그래프 파일을 찾을 수 없습니다: data/graphs/{graph_id}.json")
        except ValueError as exc:
            raise GraphTurnError(f"그래프 {graph_id} 를 읽지 못했습니다: {exc}")

        known: Dict[str, bool] = {}
        try:
            for key, cfg in get_config().agents.items():
                if not getattr(cfg, "enabled", True):
                    known[key] = False
        except Exception:  # noqa: BLE001
            logger.debug("Could not read conf.json agents for graph validation", exc_info=True)
        for agent in self.engine.agent_pool.list_all():
            known[agent.key] = True
        for agent in await frozen_agents(db, session_model.id, self.engine.agent_pool):
            known[agent.key] = True
        report = validate_graph(spec, known, default_max_visits=max_rounds)
        if not report.ok:
            raise GraphTurnError(
                f"그래프 “{spec.name or spec.id}” 에 오류가 있어 시작하지 않습니다: "
                + " · ".join(report.errors[:3])
                + (f" (외 {len(report.errors) - 3}건)" if len(report.errors) > 3 else "")
            )

        try:
            await db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_model.id)
                .values(graph_snapshot=spec.dump(), updated_at=SessionModel.updated_at)
            )
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not store the graph snapshot for {session_model.id}: {exc}")
            await db.rollback()
        return spec

    async def run_graph(
        self,
        *,
        db: Any,
        state: DebateState,
        spec: GraphSpec,
        orchestrator: Agent,
        active_agents: List[Agent],
        parallel_limit: int,
        max_visits: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        start_value: List[str],
        replay: Optional[turns.GraphReplay] = None,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> bool:
        """그래프를 단계별로 돕니다. 사용자가 정지시켰으면 True."""
        agents = {a.key: a for a in active_agents}
        redo = redo if redo is not None else {}
        scheduler = replay.scheduler if replay is not None else GraphScheduler(spec, max_visits)
        max_steps = scheduler.max_steps()
        announced_exhausted = len(scheduler.exhausted)

        if on_event:
            await on_event({
                "type": "graph_started",
                "graph_id": spec.id,
                "name": spec.name or spec.id,
                "nodes": [{"id": n.id, "type": n.type, "label": n.display} for n in spec.nodes],
                "spec": spec.dump(),
                "max_steps": max_steps,
                "history": [
                    {
                        "id": m.id,
                        "graph_node_id": m.graph_node_id,
                        "graph_port": m.graph_port,
                        "round_number": m.round_number,
                        "msg_type": m.msg_type,
                    }
                    for m in state.messages[state.turn_message_start:] if m.graph_node_id
                ] if replay is not None else [],
            })

        step = 0
        if replay is None:
            scheduler.deliver(spec.start.id, "out", start_value)
        else:
            step = replay.step
            state.current_round = step
            if replay.pending:
                await self.engine._finish_graph_step(
                    db=db,
                    state=state,
                    spec=spec,
                    replay=replay,
                    agents=agents,
                    orchestrator=orchestrator,
                    parallel_limit=parallel_limit,
                    max_steps=max_steps,
                    control=control,
                    on_event=on_event,
                    redo=redo,
                )

        while True:
            await self.engine._apply_interjections(
                db=db, state=state, control=control, round_number=step, on_event=on_event
            )
            if control is not None and control.stop_requested:
                await self.engine._graph_finished(on_event, "stopped", step)
                return True

            ready = scheduler.ready()
            for node_id in scheduler.exhausted[announced_exhausted:]:
                node = spec.node(node_id)
                await self.engine._record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=orchestrator,
                    round_number=step,
                    msg_type="orchestrator",
                    graph_node_id=node_id,
                    content=(
                        f"[그래프] “{node.display}” 은(는) 이번 턴에 최대 "
                        f"{scheduler.visits[node_id]}회까지 불려 더 부르지 않습니다."
                    ),
                )
            announced_exhausted = len(scheduler.exhausted)

            if any(n.type == "end" for n in ready):
                await self.engine._graph_finished(on_event, "end", step)
                return False
            if not ready:
                waiting = [spec.node(n).display for n in scheduler.pending()]
                await self.engine._record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=orchestrator,
                    round_number=step,
                    msg_type="orchestrator",
                    content=(
                        "[그래프] 최종 합성 노드에 닿기 전에 더 진행할 노드가 없어, 지금까지의 "
                        "발언으로 합성합니다."
                        + (
                            f" 모든 입력을 기다리다 멈춘 노드: {', '.join('“' + w + '”' for w in waiting)}."
                            if waiting else ""
                        )
                    ),
                )
                await self.engine._graph_finished(on_event, "idle", step)
                return False
            if step >= max_steps:
                await self.engine._record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=orchestrator,
                    round_number=step,
                    msg_type="orchestrator",
                    content=f"[그래프] 단계 상한({max_steps})에 닿아 여기서 멈추고 합성합니다.",
                )
                await self.engine._graph_finished(on_event, "step_cap", step)
                return False

            step += 1
            state.current_round = step
            activations = [scheduler.activate(node) for node in ready]
            if on_event:
                await on_event({"type": "round_started", "round": step, "max_rounds": max_steps})
                await on_event({
                    "type": "graph_step_started",
                    "step": step,
                    "max_steps": max_steps,
                    "nodes": [{"id": a.node.id, "label": a.node.display, "visit": a.visit} for a in activations],
                })
                await on_event({
                    "type": "status_changed",
                    "status": "debating",
                    "speaker": " · ".join(a.node.display for a in activations),
                    "round": step,
                })

            outputs = await self.engine._run_graph_step(
                db=db,
                state=state,
                spec=spec,
                scheduler=scheduler,
                activations=activations,
                agents=agents,
                orchestrator=orchestrator,
                step=step,
                parallel_limit=parallel_limit,
                control=control,
                on_event=on_event,
                redo=redo,
            )
            for activation in activations:
                port, value = outputs[activation.node.id]
                scheduler.deliver(activation.node.id, port, value)
            await self.engine._after_graph_step(state, spec, scheduler, orchestrator, step, control, on_event)

    async def after_graph_step(
        self,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        orchestrator: Agent,
        step: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> None:
        heading_to_end = any(
            scheduler.fresh[n.id] for n in spec.nodes if n.type == "end"
        )
        if not heading_to_end and not (control is not None and control.stop_requested):
            await self.engine._update_ledger(
                state=state, orchestrator=orchestrator, on_event=on_event, reason=f"Step {step}",
            )

    async def finish_graph_step(
        self,
        *,
        db: Any,
        state: DebateState,
        spec: GraphSpec,
        replay: turns.GraphReplay,
        agents: Dict[str, Agent],
        orchestrator: Agent,
        parallel_limit: int,
        max_steps: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        redo: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """끊긴 단계를 마저 돌립니다."""
        step = replay.step
        pending = replay.pending
        if on_event:
            await on_event({"type": "round_started", "round": step, "max_rounds": max_steps})
            await on_event({
                "type": "graph_step_started",
                "step": step,
                "max_steps": max_steps,
                "nodes": [{"id": a.node.id, "label": a.node.display, "visit": a.visit} for a in pending],
            })
            await on_event({
                "type": "status_changed",
                "status": "debating",
                "speaker": " · ".join(a.node.display for a in pending),
                "round": step,
            })
        view = state.model_copy(update={"messages": [
            m for m in state.messages if not (m.graph_node_id and m.round_number == step)
        ]})
        outputs = dict(replay.outputs)
        outputs.update(await self.engine._run_graph_step(
            db=db,
            state=state,
            spec=spec,
            scheduler=replay.scheduler,
            activations=pending,
            agents=agents,
            orchestrator=orchestrator,
            step=step,
            parallel_limit=parallel_limit,
            control=control,
            on_event=on_event,
            view=view,
            redo=redo,
        ))
        for activation in replay.activations:
            port, value = outputs[activation.node.id]
            replay.scheduler.deliver(activation.node.id, port, value)
        await self.engine._after_graph_step(state, spec, replay.scheduler, orchestrator, step, control, on_event)

    @staticmethod
    async def graph_finished(on_event: Optional[EventCallback], reason: str, steps: int) -> None:
        if on_event:
            await on_event({"type": "graph_finished", "reason": reason, "steps": steps})

    async def run_graph_step(
        self,
        *,
        db: Any,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        activations: List[Activation],
        agents: Dict[str, Agent],
        orchestrator: Agent,
        step: int,
        parallel_limit: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        view: Optional[DebateState] = None,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Tuple[str, List[str]]]:
        """한 단계의 노드들을 동시에 돌리고, 노드마다 (출력 핀, 값) 을 돌려줍니다."""
        redo = redo if redo is not None else {}
        prompts: Dict[str, List[Dict[str, Any]]] = {}
        for activation in activations:
            node = activation.node
            if node.type == "gate":
                continue
            speaker = orchestrator if node.type == "merge" else agents[node.agent]
            prompts[node.id] = await self.engine._context_for_node(
                state=view if view is not None else state,
                spec=spec,
                scheduler=scheduler,
                activation=activation,
                speaker=speaker,
                step=step,
                orchestrator=orchestrator,
                on_event=on_event,
                notice=turns.resume_notice(redo.pop(speaker.key)) if speaker.key in redo else "",
            )

        base_time = utc_now()
        db_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(max(1, parallel_limit))
        start_index = len(state.messages)

        async def run_one(index: int, activation: Activation) -> Tuple[str, List[str]]:
            node = activation.node
            created_at = base_time + timedelta(milliseconds=index)
            async with semaphore:
                if node.type == "gate":
                    return await self.engine._run_gate(
                        db=db,
                        state=state,
                        spec=spec,
                        activation=activation,
                        orchestrator=orchestrator,
                        step=step,
                        on_event=on_event,
                        db_lock=db_lock,
                        created_at=created_at,
                    )
                speaker = orchestrator if node.type == "merge" else agents[node.agent]
                message = await self.engine._speak(
                    db=db,
                    state=state,
                    agent=speaker,
                    prompt_messages=prompts[node.id],
                    custom_instructions=state.custom_instructions,
                    round_number=step,
                    msg_type="orchestrator" if node.type == "merge" else "agent",
                    on_event=on_event,
                    control=control,
                    db_lock=db_lock,
                    created_at=created_at,
                    graph_node_id=node.id,
                    graph_port="out",
                    kind=turns.KIND_MERGE if node.type == "merge" else turns.KIND_SPEECH,
                )
                return "out", [message.id]

        results = await asyncio.gather(
            *(run_one(i, a) for i, a in enumerate(activations)), return_exceptions=True
        )

        order = {a.node.id: i for i, a in enumerate(activations)}
        produced = state.messages[start_index:]
        if all(m.graph_node_id in order for m in produced):
            state.messages[start_index:] = sorted(produced, key=lambda m: order[m.graph_node_id])

        outputs: Dict[str, Tuple[str, List[str]]] = {}
        for activation, result in zip(activations, results):
            node = activation.node
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.error(
                    f"Graph node '{node.id}' failed: {type(result).__name__}: {result}",
                    exc_info=result,
                )
                note = await self.engine._record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=orchestrator,
                    round_number=step,
                    msg_type="error",
                    graph_node_id=node.id,
                    graph_port=node.default if node.type == "gate" else "out",
                    content=(
                        f"> ⚠️ **그래프 노드 “{node.display}” 가 실패했습니다.**\n>\n"
                        f"> - 원인: `{type(result).__name__}: {result}`\n>\n"
                        f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다."
                    ),
                    kind=turns.KIND_FAILURE,
                )
                port = node.default if node.type == "gate" else "out"
                outputs[node.id] = (port, [note.id])
            else:
                outputs[node.id] = result
        return outputs

    def render_graph_input(
        self,
        state: DebateState,
        message: DebateMessage,
        carry: str,
        placeholders: Dict[int, str],
        index_of: Dict[str, int],
    ) -> str:
        """선 하나가 싣고 온 발언 하나. 선 종류대로 전문 / 요지 / 참조."""
        if message.msg_type == "error":
            return f"[{message.sender_name}]: (이 발언은 실패해 내용이 없습니다)"
        pinned = placeholders.get(index_of.get(message.id, -1))
        label = "[User]" if message.sender_key == "user" else f"[{message.speaker}]"
        if pinned:
            return f"{label}:\n{pinned}"
        body = strip_reasoning_trace(message.content)
        if carry == "digest" and message.sender_key != "user":
            digest = memory.extract_digest(message.content)
            if digest:
                return f"{label} {memory.DIGEST_HEADING} (전문 {len(body):,}자 중 요지만):\n{digest}"
            return f"{label}:\n{memory.reference_code_blocks(body, message.tool_calls)}"
        if carry == "refs":
            return f"{label}:\n{memory.reference_code_blocks(body, message.tool_calls)}"
        return f"{label}:\n{body}"

    async def context_for_node(
        self,
        *,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        activation: Activation,
        speaker: Agent,
        step: int,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        notice: str = "",
    ) -> List[Dict[str, Any]]:
        """노드 하나의 프롬프트."""
        node = activation.node
        instruction = self.engine._graph_turn_instruction(spec, activation, step)
        if notice:
            instruction = f"{notice}\n\n{instruction}"
        if node.type == "agent" and node.sees == "all":
            return await self.engine._context_for_speech(
                state, speaker, instruction, orchestrator=orchestrator, on_event=on_event,
            )

        budget = self.engine._speech_budget(speaker, state)
        record = memory.build_user_record(
            state, model=speaker.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        plan_pin = memory.build_plan_pin(
            state, model=speaker.model, token_cap=int(budget * memory.PLAN_PIN_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, bool(plan_pin))
        index_of = {m.id: i for i, m in enumerate(state.messages)}
        by_id = {m.id: m for m in state.messages}

        head = [f"[User Goal / Current Request]:\n{state.user_prompt}"]
        head += [part for part in (record.text, plan_pin) if part]
        context: List[Dict[str, Any]] = [{"role": "user", "content": "\n\n".join(head)}]

        previous = [
            m for m in state.messages[state.turn_message_start:]
            if m.graph_node_id == node.id and m.msg_type != "error" and m.sender_key == speaker.key
        ]
        if previous:
            last = previous[-1]
            context.append({
                "role": "assistant",
                "content": (
                    f"[{speaker.name} ({speaker.role}) · 이 노드의 직전 발언]:\n"
                    f"{memory.reference_code_blocks(strip_reasoning_trace(last.content), last.tool_calls)}"
                ),
            })

        back = scheduler.back
        for edge, ids in activation.inputs:
            source = spec.node(edge.source[0])
            parts = [
                self.engine._render_graph_input(state, by_id[mid], edge.carry, placeholders, index_of)
                for mid in ids if mid in by_id and not (previous and mid == previous[-1].id)
            ]
            if not parts:
                continue
            branch = {"yes": " 예 갈래", "no": " 아니오 갈래"}.get(edge.source[1], "")
            header = (
                f"[입력 · “{source.display if source else edge.source[0]}”{branch}"
                f" → {CARRY_LABELS.get(edge.carry, edge.carry)}"
                f"{' · 되돌림' if edge.id in back else ''}]"
            )
            context.append({"role": "user", "content": header + "\n" + "\n\n".join(parts)})

        context.append({"role": "user", "content": instruction})
        return context

    @staticmethod
    def graph_turn_instruction(spec: GraphSpec, activation: Activation, step: int) -> str:
        node = activation.node
        visit = f" ({activation.visit}회차)" if activation.visit > 1 else ""
        lines = [f"[Graph Step]: {step}단계 · 노드 “{node.display}”{visit}"]
        if node.type == "merge":
            lines.append(
                "[취합] 위 [입력]으로 들어온 발언들을 하나로 붙이세요:\n"
                "1. 통합된 현재 결론 (무엇이 정해졌는가)\n"
                "2. 서로 어긋나는 지점과 그 판정\n"
                "3. 아직 검증되지 않은 가정\n"
                "4. 다음으로 넘길 미해결 과제\n"
                "발언하지 못했거나 실패한 쪽의 몫을 지어내지 마세요."
            )
        else:
            lines.append(
                "[그래프 토론] 당신은 그래프의 한 노드입니다. 위 [입력]으로 들어온 발언을 받아 이 노드의 "
                "몫을 하세요. 입력에 없는 다른 에이전트의 결론을 추측해 채우지 말고, 필요하면 가정으로 "
                "명시하세요."
            )
        if activation.visit > 1:
            lines.append(
                "이 노드가 다시 불렸습니다. 되돌아온 이유(판정·취합 의견)를 먼저 처리하고, 직전 발언에서 "
                "바뀐 점을 분명히 하세요."
            )
        if node.instruction.strip():
            lines.append(f"[이 노드의 지시]\n{node.instruction.strip()}")
        lines.append(memory.DIGEST_INSTRUCTION)
        return "\n\n".join(lines)

    async def run_gate(
        self,
        *,
        db: Any,
        state: DebateState,
        spec: GraphSpec,
        activation: Activation,
        orchestrator: Agent,
        step: int,
        on_event: Optional[EventCallback],
        db_lock: asyncio.Lock,
        created_at: datetime,
    ) -> Tuple[str, List[str]]:
        """판정 노드."""
        node = activation.node
        judge = self.engine._tool_less(orchestrator)
        by_id = {m.id: m for m in state.messages}
        index_of = {m.id: i for i, m in enumerate(state.messages)}
        record = memory.build_user_record(
            state, model=judge.model, token_cap=int(context_budget(judge) * memory.ROUTING_RECORD_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, False)
        seen: List[str] = []
        blocks: List[str] = []
        for edge, ids in activation.inputs:
            for mid in ids:
                if mid in by_id and mid not in seen:
                    seen.append(mid)
                    blocks.append(
                        self.engine._render_graph_input(state, by_id[mid], edge.carry, placeholders, index_of)
                    )

        default_label = "예" if node.default == "yes" else "아니오"
        prompt = [{"role": "user", "content": (
            f"[판정] 그래프 토론의 판정 노드 “{node.display}” 입니다.\n\n"
            f"[목표]\n{state.user_prompt}\n\n"
            + (f"{record.text}\n\n" if record.text else "")
            + "[판정할 내용]\n" + ("\n\n".join(blocks) or "(들어온 발언이 없습니다)") + "\n\n"
            f"[질문]\n{node.question.strip()}\n\n"
            f"위 내용만 근거로 질문에 예/아니오로 답하세요. 근거가 부족해 판단할 수 없으면 "
            f"“{default_label}” 로 답하고 그 이유를 적으세요.\n\n"
            '다음 JSON 한 줄로만 답하세요:\n{"decision": "yes" 또는 "no", "reason": "한두 문장"}'
        )}]

        decision, reason, understood = node.default, "", False
        try:
            content, _ = await self.engine.llm_caller.call_agent(
                judge,
                prompt,
                state.custom_instructions,
                session_id=state.session_id,
                mcp=self.engine._mcp_for(state),
                tool_gate=self.engine._gate_for(state),
                ledger=state.decision_ledger,
            )
            parsed = parse_gate_decision(content)
            if parsed is not None:
                decision, reason = parsed
                understood = True
            else:
                reason = "판정 응답을 읽지 못했습니다"
        except LLMUnavailableError as exc:
            reason = f"판정 호출에 실패했습니다 ({exc.reason})"

        verdict = "예" if decision == "yes" else "아니오"
        content_text = f"[판정 · {node.display}] **{verdict}** — {reason or '(사유 없음)'}"
        if not understood:
            content_text += f"\n(기본 갈래 “{default_label}” 로 진행합니다)"
        async with db_lock:
            note = await self.engine._record_note(
                db=db,
                state=state,
                on_event=on_event,
                agent=orchestrator,
                round_number=step,
                msg_type="orchestrator",
                graph_node_id=node.id,
                graph_port=decision,
                content=content_text,
                created_at=created_at,
                kind=turns.KIND_GATE,
            )
        if on_event:
            await on_event({
                "type": "graph_gate_decided",
                "node_id": node.id,
                "label": node.display,
                "decision": decision,
                "reason": reason,
                "fallback": not understood,
            })
        return decision, [note.id] + seen
