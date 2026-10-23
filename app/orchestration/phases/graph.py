"""그래프 토론: 그려 둔 그래프를 단계별로 돌립니다 (노드 발언, 판정 노드, 선이 싣는 발언)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import update

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.agents.llm_context import context_budget
from app.agents.reasoning import strip_reasoning_trace
from app.database.models import SessionModel, utc_now
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory, turns
from app.orchestration.control import TurnControl
from app.orchestration.graph import (
    CARRY_LABELS,
    Activation,
    GraphScheduler,
    GraphSpec,
    GraphTurnError,
    validate_graph,
)
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.state import DebateMessage, DebateState

logger = logging.getLogger(__name__)


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


class GraphRunner(PhaseExecutor):
    """`graph_debate` 전략의 턴 — 그래프를 고정하고, 단계마다 활성 노드를 돌립니다."""

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
        for agent in self.ctx.agent_pool.list_all():
            known[agent.key] = True
        for agent in await frozen_agents(db, session_model.id, self.ctx.agent_pool):
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
        """그래프를 단계별로 실행합니다. 사용자가 정지 요청을 한 경우 True를 반환합니다."""
        agents = {a.key: a for a in active_agents}
        redo = redo if redo is not None else {}
        scheduler = replay.scheduler if replay is not None else GraphScheduler(spec, max_visits)
        max_steps = scheduler.max_steps()
        announced_exhausted = len(scheduler.exhausted)

        if on_event:
            await on_event({
                "type": EventType.GRAPH_STARTED,
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
                await self.finish_graph_step(
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
            await self.recorder.apply_interjections(
                db=db, state=state, control=control, round_number=step, on_event=on_event
            )
            if control is not None and control.stop_requested:
                await self.graph_finished(on_event, "stopped", step)
                return True

            ready = scheduler.ready()
            for node_id in scheduler.exhausted[announced_exhausted:]:
                node = spec.node(node_id)
                await self.recorder.record_note(
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
                await self.graph_finished(on_event, "end", step)
                return False
            if not ready:
                waiting = [spec.node(n).display for n in scheduler.pending()]
                await self.recorder.record_note(
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
                await self.graph_finished(on_event, "idle", step)
                return False
            if step >= max_steps:
                await self.recorder.record_note(
                    db=db,
                    state=state,
                    on_event=on_event,
                    agent=orchestrator,
                    round_number=step,
                    msg_type="orchestrator",
                    content=f"[그래프] 단계 상한({max_steps})에 닿아 여기서 멈추고 합성합니다.",
                )
                await self.graph_finished(on_event, "step_cap", step)
                return False

            step += 1
            state.current_round = step
            activations = [scheduler.activate(node) for node in ready]
            if on_event:
                await on_event({"type": EventType.ROUND_STARTED, "round": step, "max_rounds": max_steps})
                await on_event({
                    "type": EventType.GRAPH_STEP_STARTED,
                    "step": step,
                    "max_steps": max_steps,
                    "nodes": [{"id": a.node.id, "label": a.node.display, "visit": a.visit} for a in activations],
                })
                await on_event({
                    "type": EventType.STATUS_CHANGED,
                    "status": "debating",
                    "speaker": " · ".join(a.node.display for a in activations),
                    "round": step,
                })

            outputs = await self.run_graph_step(
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
            await self.after_graph_step(state, spec, scheduler, orchestrator, step, control, on_event)

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
            await self.conversation.update_ledger(
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
        """중단된 그래프 단계를 이어서 실행합니다."""
        step = replay.step
        pending = replay.pending
        if on_event:
            await on_event({"type": EventType.ROUND_STARTED, "round": step, "max_rounds": max_steps})
            await on_event({
                "type": EventType.GRAPH_STEP_STARTED,
                "step": step,
                "max_steps": max_steps,
                "nodes": [{"id": a.node.id, "label": a.node.display, "visit": a.visit} for a in pending],
            })
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "debating",
                "speaker": " · ".join(a.node.display for a in pending),
                "round": step,
            })
        view = state.model_copy(update={"messages": [
            m for m in state.messages if not (m.graph_node_id and m.round_number == step)
        ]})
        outputs = dict(replay.outputs)
        outputs.update(await self.run_graph_step(
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
        await self.after_graph_step(state, spec, replay.scheduler, orchestrator, step, control, on_event)

    @staticmethod
    async def graph_finished(on_event: Optional[EventCallback], reason: str, steps: int) -> None:
        if on_event:
            await on_event({"type": EventType.GRAPH_FINISHED, "reason": reason, "steps": steps})

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
        """현재 단계의 노드들을 동시 실행하고, 노드별 (출력 핀, 결과값) 튜플 목록을 반환합니다."""
        redo = redo if redo is not None else {}
        prompts: Dict[str, List[Dict[str, Any]]] = {}
        for activation in activations:
            node = activation.node
            if node.type == "gate":
                continue
            speaker = orchestrator if node.type == "merge" else agents[node.agent]
            prompts[node.id] = await self.context_for_node(
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
                    return await self.run_gate(
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
                message = await self.speech.speak(
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
                note = await self.recorder.record_note(
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
        """엣지(선)를 통해 전달된 발언 메시지 내용입니다 (전문, 요지, 참조 유형별 변환)."""
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
        """단일 노드 실행을 위한 프롬프트를 구성합니다."""
        node = activation.node
        instruction = self.graph_turn_instruction(spec, activation, step)
        if notice:
            instruction = f"{notice}\n\n{instruction}"
        if node.type == "agent" and node.sees == "all":
            return await self.conversation.context_for_speech(
                state, speaker, instruction, orchestrator=orchestrator, on_event=on_event,
            )

        budget = self.conversation.speech_budget(speaker, state)
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
                self.render_graph_input(state, by_id[mid], edge.carry, placeholders, index_of)
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
        """조건 분기를 위한 판정 노드를 실행합니다."""
        node = activation.node
        judge = self.conversation.tool_less(orchestrator)
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
                        self.render_graph_input(state, by_id[mid], edge.carry, placeholders, index_of)
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
            content, _ = await self.ctx.llm_caller.call_agent(
                judge,
                prompt,
                state.custom_instructions,
                session_id=state.session_id,
                mcp=self.ctx.mcp_for(state),
                tool_gate=self.ctx.gate_for(state),
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
            note = await self.recorder.record_note(
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
                "type": EventType.GRAPH_GATE_DECIDED,
                "node_id": node.id,
                "label": node.display,
                "decision": decision,
                "reason": reason,
                "fallback": not understood,
            })
        return decision, [note.id] + seen
