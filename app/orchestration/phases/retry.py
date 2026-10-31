"""끝난 턴의 발언 하나만 다시 실행하거나 지난 버전으로 되돌립니다 (ADR-036).

턴 전체를 다시 돌리지 않고, 그 발언이 나오던 시점의 맥락을 기록에서 다시 세워 그 자리의 발언만 다시 받습니다.
버전을 어떻게 남기는지는 `app/orchestration/speech_retry.py` 에 있습니다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update

from app.agents.consultation import consultation_scope
from app.agents.llm_usage import Usage
from app.config import get_config
from app.database.models import MessageModel, SessionModel, TurnModel
from app.events import EventCallback, EventType
from app.orchestration import speech_comments, speech_retry, turns
from app.orchestration.control import TurnControl
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.state import DebateMessage, DebateState
from app.orchestration.strategies import get_strategy, resolve_strategy_name
from app.orchestration.turn_setup import TurnPreparer, TurnSetup, new_state, restore_turn_state

logger = logging.getLogger(__name__)


# 참여자를 세울 때만 쓰는 전략. 그래프 전략은 지금의 그래프 파일을 다시 읽고 참여자를 거기서 정하는데, 다시 실행은 그
# 턴에 기록된 참여자(`TurnModel.config`)로 충분합니다. 그 사이 파일이 지워졌어도 멈출 이유가 없습니다.
_SETUP_STRATEGY = "sequential_debate"


class SpeechRevisionExecutor(PhaseExecutor):
    """발언 하나의 새 버전을 받거나 지난 버전을 되살립니다."""

    def __init__(self, *services, preparer: TurnPreparer) -> None:
        super().__init__(*services)
        self.preparer = preparer

    async def _setup(
        self, db, session_model: SessionModel, turn: TurnModel, *,
        control: Optional[TurnControl], on_event: Optional[EventCallback],
    ) -> TurnSetup:
        return await self.preparer.setup(
            db, session_model, control=control, on_event=on_event,
            config={**(turn.config or {}), "strategy": _SETUP_STRATEGY},
        )

    async def _state_before(
        self, db, session_model: SessionModel, turn: TurnModel, target_id: str, setup: TurnSetup, workspace: Path,
    ) -> Tuple[DebateState, DebateMessage]:
        """그 발언이 나오기 직전의 턴 상태와 그 발언.

        기억은 **그 턴이 시작될 때의 것**을 씁니다. 지금의 장부에는 뒤에 나온 결정이 들어 있고, 그 턴 안에서 앞선
        라운드가 반영한 것은 원문이 기록에 그대로 있으므로 잃는 것이 없습니다.
        """
        history = await self.recorder.load_history(db, session_model.id, tools_for_turn=turn.id)
        at = next((i for i, m in enumerate(history) if m.id == target_id), None)
        start = next((i for i, m in enumerate(history) if m.id == turn.opening_message_id), None)
        if at is None or start is None:
            raise speech_retry.SpeechRetryError("이 발언의 자리를 기록에서 찾지 못했습니다.")
        target = history[at]
        before = history[:at]
        # 동시에 돈 발언은 서로의 결과를 보지 못했습니다. 정렬 순서로 앞에 있어도 맥락에 넣지 않습니다.
        strategy = get_strategy(resolve_strategy_name((turn.config or {}).get("strategy")))
        if strategy.orchestrator_dispatches_parallel or strategy.runs_graph:
            before = [
                m for m in before
                if not (
                    m.round_number == target.round_number
                    and turns.kind_of(m) in (turns.KIND_SPEECH, turns.KIND_FAILURE)
                )
            ]

        state = new_state(session_model.id, history[start].content, workspace, setup)
        state.strategy = strategy.name
        state.turn_id = turn.id
        state.messages = before
        state.turn_message_start = start
        remembered = await self._memory_before(db, session_model.id, turn)
        self.conversation.load_memory(state, SimpleNamespace(
            decision_ledger=remembered.get("decision_ledger") or "",
            ledger_through_id=remembered.get("ledger_through_id"),
            transcript_summary=remembered.get("transcript_summary") or "",
            summary_through_id=remembered.get("summary_through_id"),
        ))
        restore_turn_state(state)
        state.current_round = target.round_number
        return state, target

    @staticmethod
    async def _memory_before(db, session_id: str, turn: TurnModel) -> Dict[str, Any]:
        """이 턴 앞에서 가장 가까운, 기억을 남긴 턴의 기억. 없으면 빈 기억."""
        rows = (await db.execute(
            select(TurnModel.memory)
            .where(TurnModel.session_id == session_id, TurnModel.started_at < turn.started_at)
            .order_by(TurnModel.started_at.desc())
        )).scalars().all()
        return next((dict(memory) for memory in rows if isinstance(memory, dict)), {})

    async def retry(
        self,
        db,
        session_model: SessionModel,
        turn: TurnModel,
        row: MessageModel,
        workspace: Path,
        *,
        connection: str,
        instruction: str,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        usage: Optional[Usage],
    ) -> DebateMessage:
        """이 발언의 새 버전을 받습니다. 실패로 끝나도 예외를 올리지 않고 실패한 발언을 돌려줍니다."""
        setup = await self._setup(db, session_model, turn, control=control, on_event=on_event)
        state, target = await self._state_before(db, session_model, turn, row.id, setup, workspace)
        if usage is not None:
            # 턴의 누적 사용량에 더합니다. 다시 실행도 그 턴이 쓴 토큰입니다.
            usage.add(Usage.of(turn.usage))
            state.usage = usage

        pool = setup.consult_pool
        agent = next((a for a in pool if a.key == target.sender_key), None)
        if agent is None:
            raise speech_retry.SpeechRetryError(f"{target.sender_name} 에이전트를 이 대화에서 찾지 못했습니다.")
        agent = speech_retry.with_connection(agent, connection, pool, get_config().llm)
        instruction = (instruction or "").strip()
        comments = await speech_comments.pending_for(db, state.session_id, agent.key, message_id=target.id)

        # 고칠 말이 있을 때만 이전 버전을 보여 줍니다. 조건 없이 다시 받는 것은 이전 글에 끌려가지 않은 새 답을 보려는 것입니다.
        wants_previous = bool(instruction or comments) and target.msg_type != "error"
        notice = speech_retry.retry_notice(
            previous=target.content if wants_previous else "", instruction=instruction, tools=target.tool_calls,
        )
        prompt = await self.conversation.context_for_speech(
            state, agent, notice, orchestrator=setup.orchestrator, on_event=on_event,
        )
        tail = (target.turn_meta or {}).get(speech_retry.PROMPT_TAIL)
        if tail:
            prompt[-1] = {**prompt[-1], "content": f"{tail}\n\n{notice}"}

        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED, "status": "debating",
                "speaker": agent.name, "round": target.round_number,
            })
        revision = await speech_retry.SpeechRevision.of(
            db, row, condition={"model": agent.model, "instruction": instruction}, comments=comments,
        )
        with consultation_scope(
            setup.active_agents,
            pool=pool,
            llm_caller=self.ctx.llm_caller,
            custom_instructions=setup.custom_instructions,
            session_id=state.session_id,
            mcp=self.ctx.mcp_for(state),
            tool_gate=self.ctx.gate_for(state),
            on_event=on_event,
        ):
            message = await self.speech.speak(
                db=db,
                state=state,
                agent=agent,
                prompt_messages=prompt,
                custom_instructions=setup.custom_instructions,
                round_number=target.round_number,
                msg_type="agent",
                on_event=on_event,
                control=control,
                graph_node_id=target.graph_node_id,
                graph_port=target.graph_port,
                revision=revision,
            )

        succeeded = message.msg_type != "error"
        if succeeded:
            await self._correct_ledger(db, state, setup, message, on_event=on_event)
        await self.recorder.set_turn(db, state)
        if on_event:
            await on_event({
                "type": EventType.SPEECH_REVISED, "message_id": message.id, "agent_name": agent.name,
                "ok": succeeded, "restored": False,
            })
        return message

    async def restore(
        self,
        db,
        session_model: SessionModel,
        turn: TurnModel,
        row: MessageModel,
        workspace: Path,
        version: int,
        *,
        on_event: Optional[EventCallback],
    ) -> None:
        """지난 버전을 다시 지금 버전으로 합니다. 발언을 새로 받지 않고, 장부만 맞춥니다."""
        setup = await self._setup(db, session_model, turn, control=None, on_event=on_event)
        state, _target = await self._state_before(db, session_model, turn, row.id, setup, workspace)
        restored = await speech_retry.restore_version(db, row, version)
        await db.commit()
        message = DebateMessage(
            id=row.id, sender_key=row.sender_key, sender_name=row.sender_name, sender_role=row.sender_role,
            content=restored["content"], round_number=row.round_number, msg_type=restored["msg_type"],
            turn_meta=row.turn_meta,
        )
        await self._correct_ledger(db, state, setup, message, on_event=on_event)
        if on_event:
            await on_event({
                "type": EventType.SPEECH_REVISED, "message_id": row.id, "agent_name": row.sender_name,
                "ok": True, "restored": True,
            })

    async def _correct_ledger(
        self, db, state: DebateState, setup: TurnSetup, message: DebateMessage, *,
        on_event: Optional[EventCallback],
    ) -> None:
        """지금의 결정 장부를 바뀐 발언에 맞춥니다. 장부는 모든 에이전트의 시스템 프롬프트에 고정되므로, 옛 버전의 결정이
        남아 있으면 새 버전을 계속 누릅니다. 못 고쳐도 대화 끝의 안내가 남습니다.

        장부에 반영된 지점(`ledger_through_id`)은 그대로 둡니다. 다음 턴이 접을 범위는 달라지지 않습니다.
        """
        session = await db.get(SessionModel, state.session_id)
        current = (session.decision_ledger or "") if session is not None else ""
        if not current.strip():
            return
        corrected = message.model_copy(update={"content": (
            "[유저가 이 발언을 다른 버전으로 바꿨습니다. 이전 장부에서 옛 버전에 근거한 항목은 이 내용에 맞게 고치세요.]\n"
            f"{message.content}"
        )})
        view = state.model_copy(update={
            "messages": [*state.messages[:_cut(state, message)], corrected],
            "decision_ledger": current,
            "ledger_through": _cut(state, message),
        })
        if await self.conversation.update_ledger(
            state=view, orchestrator=setup.orchestrator, on_event=on_event, reason="발언 교체",
        ):
            await db.execute(
                update(SessionModel).where(SessionModel.id == state.session_id).values(
                    decision_ledger=view.decision_ledger, updated_at=SessionModel.updated_at,
                )
            )
            await db.commit()


def _cut(state: DebateState, message: DebateMessage) -> int:
    """`state.messages` 에서 이 발언 앞까지의 길이 (`speak` 이 끝에 덧붙인 경우를 뺍니다)."""
    return next((i for i, m in enumerate(state.messages) if m.id == message.id), len(state.messages))
