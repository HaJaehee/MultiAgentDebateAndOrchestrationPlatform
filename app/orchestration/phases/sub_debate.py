"""서브 토론: 쟁점 하나를 전문가 두 명이 따로 다루고, 결론만 본 토론에 합류시킵니다 (ADR-034).

동료 자문(`peers__consult_agent`)은 한 사람이 묻고 다른 사람이 답하는 한 번의 왕복이고, 답은 물은 사람의 도구
결과로만 남습니다. 서로 어긋나는 두 전문가가 주고받아야 풀리는 쟁점은 라운드를 통째로 더 돌리는 수밖에
없었습니다. 서브 토론은 그 사이에 있습니다.

* **언제** — 라운드(그래프는 단계) 경계에서, 결정 장부에 미해결 쟁점이 있을 때만 오케스트레이터에게 묻습니다.
  쟁점이 없으면 LLM 을 부르지 않습니다. 대화 설정으로 켜야 돌고(`sessions.sub_debates`), 한 턴에
  `MAX_SUB_DEBATES_PER_TURN` 번까지이며, 토큰 예산을 다 썼으면 열지 않습니다.
* **어떻게** — 두 사람이 번갈아 `SUB_DEBATE_SPEECHES` 번 말하고 오케스트레이터가 결론을 씁니다. 도구는 쓸 수
  있지만 상한을 줄이고, 자문은 뺍니다 — 서브 토론 안에서 다시 가지를 치지 않습니다.
* **합류** — 안의 발언은 화면과 저장 문서에만 남고(`turns.in_main_record`), 다른 발언자·장부·합성에는 결론만
  갑니다. 결론이 나오면 장부를 바로 갱신합니다 — 다음 발언자가 장부에서 이미 풀린 쟁점을 다시 읽지 않게.
* **재개** — 경계마다 많아야 하나이고 모든 단계가 기록에 값으로 남아, 끊기면 남은 발언부터 이어 갑니다.

정지 요청이 오면 결론을 쓰지 않고 멈춥니다. 쟁점은 장부에 미해결로 남고, 합성이 그렇게 적습니다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.agents.reasoning import strip_reasoning_trace
from app.config import DEFAULT_PEER_TOOL_ITERATIONS, PEERS_TOOL_PREFIX
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory
from app.orchestration import turns
from app.orchestration.artifacts import format_roster
from app.orchestration.control import TurnControl
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.phases.routing import parse_sub_debate
from app.orchestration.state import DebateMessage, DebateState
from app.orchestration.strategies import specialists_of

if TYPE_CHECKING:
    from app.orchestration.turn_setup import TurnSetup

logger = logging.getLogger(__name__)

# 두 사람이 번갈아 하는 발언 수. 입장 → 반박 → 답변이면 쟁점이 드러납니다. 더 길면 라운드와 다르지 않습니다.
SUB_DEBATE_SPEECHES = 3
MAX_SUB_DEBATES_PER_TURN = 2
SUB_DEBATE_TOOL_ITERATIONS = DEFAULT_PEER_TOOL_ITERATIONS
SUB_DEBATE_PROMPT_MARKER = "[서브 토론 판단]"
SUB_DEBATE_CONCLUSION_MARKER = "[서브 토론 결론 작성]"
# 쟁점이 나온 직전 논의로 함께 주는 발언 수와 한 발언의 길이.
RECENT_SPEECHES = 6
RECENT_CHARS = 600


class SubDebateRunner(PhaseExecutor):
    """라운드·단계 경계에서 서브 토론을 열지 정하고, 열었으면 결론까지 돌립니다."""

    async def run_if_needed(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        boundary: int,
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        """`boundary` 경계의 서브 토론. 이미 닫혔으면 아무것도 하지 않고, 끊겼으면 이어 갑니다."""
        if not setup.sub_debates:
            return
        record = turns.sub_debate_at(state.messages[state.turn_message_start:], boundary)
        if record is None:
            record = await self.open_if_needed(db, state, setup, boundary, control=control, on_event=on_event)
        if record is None or record.concluded:
            return
        await self.run(db, state, setup, boundary, record, control=control, on_event=on_event, redo=redo)

    async def open_if_needed(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        boundary: int,
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[turns.SubDebateRecord]:
        """열 이유가 있으면 오케스트레이터에게 묻고, 연다고 하면 기록합니다. 안 열면 None."""
        if control is not None and control.stop_requested:
            return None
        candidates = specialists_of(setup.active_agents)
        turn_messages = state.messages[state.turn_message_start:]
        if (
            len(candidates) < 2
            or turns.count_kind(turn_messages, turns.KIND_SUBDEBATE_OPEN) >= MAX_SUB_DEBATES_PER_TURN
            or not memory.open_issue_count(state.decision_ledger)
            or (setup.token_budget and state.usage.total_tokens >= setup.token_budget)
        ):
            return None

        try:
            content = await self.ask_orchestrator(state, setup, candidates)
        except LLMUnavailableError as exc:
            # 묻지 못하면 그냥 다음 라운드로 갑니다. 토론의 필수 단계가 아닙니다.
            logger.warning(f"Could not ask whether to open a sub-debate: {exc}")
            return None
        decision = parse_sub_debate(content, candidates)
        if decision is None:
            return None
        pair, issue, reason = decision

        await self.recorder.record_note(
            db=db,
            state=state,
            on_event=on_event,
            agent=setup.orchestrator,
            round_number=boundary,
            msg_type="orchestrator",
            content=(
                f"[서브 토론] **쟁점:** {issue}\n\n"
                f"{pair[0].name} ↔ {pair[1].name} · {SUB_DEBATE_SPEECHES}번 주고받은 뒤 오케스트레이터가 결론을 씁니다."
                + (f"\n\n{reason}" if reason else "")
            ),
            kind=turns.KIND_SUBDEBATE_OPEN,
            data={"issue": issue, "agents": [a.key for a in pair]},
        )
        return turns.SubDebateRecord(issue=issue, agents=[a.key for a in pair])

    async def ask_orchestrator(
        self, state: DebateState, setup: "TurnSetup", candidates: List[Agent],
    ) -> str:
        """도구 없는 오케스트레이터에게 서브 토론을 열지 묻습니다."""
        selector = self.conversation.tool_less(setup.orchestrator)
        issues = memory.ledger_items(state.decision_ledger, "미해결 쟁점") or []
        recent = [
            f"{m.speaker}: {self.conversation.snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error" and turns.in_main_record(m)
        ][-8:]
        prompt = [{"role": "user", "content": (
            f"{SUB_DEBATE_PROMPT_MARKER} 결정 장부에 미해결 쟁점이 있습니다. 그대로 다음 차례로 넘길지, 전문가 "
            f"두 명을 따로 붙여 결론을 낼지 정합니다.\n\n"
            f"[목표]\n{state.user_prompt}\n\n"
            + self.conversation.routing_record(state, selector) +
            "[미해결 쟁점]\n" + "\n".join(f"- {item}" for item in issues) + "\n\n"
            "[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[부를 수 있는 전문가]\n{format_roster(candidates, with_keys=True)}\n\n"
            "두 전문가의 의견이 실제로 부딪히거나, 두 분야를 함께 봐야 풀리는 **구체적인 쟁점 하나**가 있을 때만 "
            "여세요. 다음 차례에 자연히 다룰 일, 한 사람이 혼자 답할 수 있는 일, 유저에게 물어야 풀리는 일에는 "
            "열지 마세요. 확신이 없으면 열지 마세요.\n\n"
            "다음 JSON 형식으로만 답하세요:\n"
            '{"open": true, "issue": "쟁점 한 문장", "agents": ["에이전트키", "에이전트키"], '
            '"reason": "한두 문장으로 연 이유"} 또는 {"open": false}'
        )}]
        content, _ = await self.ctx.llm_caller.call_agent(
            selector,
            prompt,
            setup.custom_instructions,
            session_id=state.session_id,
            mcp=self.ctx.mcp_for(state),
            tool_gate=self.ctx.gate_for(state),
            ledger=state.decision_ledger,
        )
        return content

    async def run(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        boundary: int,
        record: turns.SubDebateRecord,
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        """남은 발언을 돌리고 결론을 씁니다. 한 사람이 응답하지 못하면 거기까지로 결론을 냅니다."""
        redo = redo if redo is not None else {}
        by_key = {a.key: a for a in setup.active_agents}
        pair = [by_key[key] for key in record.agents if key in by_key]
        exchange: List[DebateMessage] = list(record.speeches)
        failed = any(m.msg_type == "error" for m in exchange)

        if len(pair) == 2 and not failed:
            for index in range(len(exchange), SUB_DEBATE_SPEECHES):
                await self.recorder.apply_interjections(
                    db=db, state=state, control=control, round_number=boundary, on_event=on_event,
                )
                if control is not None and control.stop_requested:
                    return
                speaker, other = pair[index % 2], pair[(index + 1) % 2]
                state.current_speaker = speaker.name
                if on_event:
                    await on_event({
                        "type": EventType.STATUS_CHANGED,
                        "status": "debating",
                        "speaker": f"{speaker.name} (서브 토론)",
                        "round": boundary,
                    })
                instruction = self.speech_instruction(speaker, other, record.issue, index)
                if speaker.key in redo:
                    instruction = f"{turns.resume_notice(redo.pop(speaker.key))}\n\n{instruction}"
                message = await self.speech.speak(
                    db=db,
                    state=state,
                    agent=self.narrowed(speaker),
                    prompt_messages=self.speech_prompt(state, speaker, exchange, instruction),
                    custom_instructions=setup.custom_instructions,
                    round_number=boundary,
                    msg_type="agent",
                    on_event=on_event,
                    control=control,
                    kind=turns.KIND_SUBDEBATE_SPEECH,
                )
                exchange.append(message)
                if message.msg_type == "error":
                    break

        if control is not None and control.stop_requested:
            return
        await self.conclude(db, state, setup, boundary, record.issue, exchange, control=control, on_event=on_event)

    @staticmethod
    def narrowed(agent: Agent) -> Agent:
        """서브 토론에서 쓸 사본. 도구 상한을 줄이고 자문을 뺍니다 — 여기서 다시 가지를 치지 않습니다."""
        return agent.model_copy(update={
            "max_tool_iterations": min(agent.max_tool_iterations, SUB_DEBATE_TOOL_ITERATIONS),
            "allowed_mcp_servers": [s for s in (agent.allowed_mcp_servers or []) if s != PEERS_TOOL_PREFIX],
            "allow_peer_consultation": False,
        })

    @staticmethod
    def speech_instruction(speaker: Agent, other: Agent, issue: str, index: int) -> str:
        head = (
            f"[서브 토론 {index + 1}/{SUB_DEBATE_SPEECHES}] 오케스트레이터가 이 쟁점을 따로 떼어 당신과 "
            f"{other.name}({other.role}) 두 사람에게 맡겼습니다. 다른 전문가는 이 대화를 읽지 않고 "
            f"오케스트레이터의 결론만 읽습니다.\n쟁점: {issue}\n\n"
        )
        if index == 0:
            body = "쟁점에 대한 당신의 입장과 근거를 먼저 밝히세요."
        elif index == SUB_DEBATE_SPEECHES - 1:
            body = "상대의 반박에 답하고, 합의된 것과 남은 이견을 구분해 마무리하세요."
        else:
            body = (
                "직전 발언의 근거를 당신의 전문 영역에서 검증하세요. 동의하면 어디까지인지, 반대하면 막연한 "
                "우려 대신 재현 가능한 반례나 근거를 대세요."
            )
        return head + body + " 쟁점 밖의 일은 다루지 말고, 도구는 쟁점을 검증하는 데 꼭 필요할 때만 쓰세요."

    def speech_prompt(
        self, state: DebateState, speaker: Agent, exchange: List[DebateMessage], instruction: str,
    ) -> List[Dict[str, Any]]:
        """서브 토론 발언의 맥락: 목표, 직전 논의의 요지, 이 서브 토론에서 오간 말, 이번 지시."""
        budget = self.conversation.speech_budget(speaker, state)
        record = memory.build_user_record(
            state, model=speaker.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        plan_pin = memory.build_plan_pin(
            state, model=speaker.model, token_cap=int(budget * memory.PLAN_PIN_SHARE)
        )
        head = [f"[User Goal / Current Request]:\n{state.user_prompt}"]
        head += [part for part in (record.text, plan_pin) if part]
        context: List[Dict[str, Any]] = [{"role": "user", "content": "\n\n".join(head)}]

        recent = [
            f"{m.speaker}: {self.conversation.snippet(m, RECENT_CHARS)}"
            for m in state.messages[state.turn_message_start:]
            if m.msg_type != "error" and m.sender_key != "user" and turns.in_main_record(m)
            and turns.kind_of(m) != turns.KIND_PLAN
        ][-RECENT_SPEECHES:]
        if recent:
            context.append({"role": "user", "content": "[직전 논의]\n" + "\n\n".join(recent)})

        for message in exchange:
            if message.msg_type == "error":
                continue
            context.append({
                "role": "assistant" if message.sender_key == speaker.key else "user",
                "content": f"[{message.speaker}]:\n{strip_reasoning_trace(message.content)}",
            })
        context.append({"role": "user", "content": instruction})
        return context

    async def conclude(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        boundary: int,
        issue: str,
        exchange: List[DebateMessage],
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> None:
        """오케스트레이터가 결론을 쓰고 장부에 반영합니다. 받은 발언이 없으면 결론 없이 닫습니다."""
        spoken = [m for m in exchange if m.msg_type != "error"]
        if not spoken:
            await self.recorder.record_note(
                db=db,
                state=state,
                on_event=on_event,
                agent=setup.orchestrator,
                round_number=boundary,
                msg_type="error",
                content=f"[서브 토론] 두 전문가의 응답을 받지 못해 결론 없이 닫습니다. 쟁점은 그대로 남습니다: {issue}",
                kind=turns.KIND_SUBDEBATE_CONCLUSION,
            )
            return

        judge = self.conversation.tool_less(setup.orchestrator)
        state.current_speaker = judge.name
        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "debating",
                "speaker": f"{judge.name} (서브 토론 결론)",
                "round": boundary,
            })
        record = memory.build_user_record(
            state, model=judge.model, token_cap=int(self.conversation.speech_budget(judge, state) * memory.ROUTING_RECORD_SHARE)
        )
        body = "\n\n".join(
            f"[{m.speaker}]:\n{memory.reference_code_blocks(strip_reasoning_trace(m.content), m.tool_calls)}"
            for m in spoken
        )
        prompt = [{"role": "user", "content": (
            f"{SUB_DEBATE_CONCLUSION_MARKER} 두 전문가가 쟁점 하나를 따로 다뤘습니다. 다른 전문가는 이 대화를 읽지 "
            f"않고 지금 쓰는 결론만 읽습니다.\n\n"
            f"[목표]\n{state.user_prompt}\n\n"
            + (f"{record.text}\n\n" if record.text else "")
            + f"[쟁점]\n{issue}\n\n[두 전문가의 발언]\n{body}\n\n"
            "수석 오케스트레이터로서 이 쟁점을 닫으세요:\n"
            "1. 합의된 것\n"
            "2. 남은 이견과 그 판정 (아직 판단할 수 없다면 그 이유)\n"
            "3. 다른 전문가가 반영해야 할 것\n\n"
            "10줄 이내로 쓰세요. 말하지 못했거나 실패한 쪽의 몫을 지어내지 마세요."
        )}]

        async def _label(text: str) -> str:
            return f"[서브 토론 결론 · {issue}]\n\n{text.strip()}"

        message = await self.speech.speak(
            db=db,
            state=state,
            agent=judge,
            prompt_messages=prompt,
            custom_instructions=setup.custom_instructions,
            round_number=boundary,
            msg_type="orchestrator",
            on_event=on_event,
            control=control,
            post_process=_label,
            kind=turns.KIND_SUBDEBATE_CONCLUSION,
        )
        if message.msg_type != "error":
            await self.conversation.update_ledger(
                state=state, orchestrator=setup.orchestrator, on_event=on_event, reason=f"서브 토론 {boundary}",
            )
