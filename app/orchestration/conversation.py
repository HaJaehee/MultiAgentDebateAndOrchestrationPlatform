"""대화 기억을 쓰는 쪽: 발언 맥락 조립, 요약 접기, 결정 장부 갱신.

규칙은 `app/orchestration/context_memory.py` 에 있습니다 (그 모듈은 LLM 을 부르지 않습니다).
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import update

from app.agents.base import Agent
from app.agents.consultation import consultation_tools_for
from app.agents.llm_context import context_budget, estimate_tokens
from app.agents.llm_tools import memory_search_tool
from app.agents.reasoning import strip_reasoning_trace
from app.agents.skills import skill_tools_for, with_designated_skills
from app.agents.tool_loading import plan_shelf
from app.database.models import SessionModel, TurnModel
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory, plan_gate, turns
from app.orchestration.context import EngineContext
from app.orchestration.state import DebateMessage, DebateState
from app.workspace_files import mention_token

logger = logging.getLogger(__name__)


class ConversationMemory:
    """발언 맥락을 만들고, 창이 차면 앞선 기록을 요약으로 접고, 결정 장부를 갱신합니다.

    무엇을 왜 지키는지(고정되는 사용자 기록·계획, 요약·장부의 몫)는 `context_memory` 에 있고,
    여기는 그 규칙으로 LLM 을 부르고 턴 상태를 바꾸는 쪽입니다.
    """

    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx

    @staticmethod
    def tool_less(agent: Agent) -> Agent:
        """도구와 단계적 사고를 끈 사본.

        JSON 한 줄(발언자 지명·태스크 분배)이나 정해진 형식의 글(장부·요약)을 받는 호출에
        씁니다. 도구를 붙이면 파일을 읽기 시작하고, 단계적 사고 프로토콜이 주입되면
        `Thought 1..N` 을 쓰다가 형식을 놓칩니다.
        """
        return agent.model_copy(update={
            "allowed_mcp_servers": [],
            "allowed_skills": [],
            "allow_peer_consultation": False,
            "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
        })

    def speech_budget(self, agent: Agent, state: DebateState) -> int:
        """이 발언자의 요청에서 시스템 프롬프트와 대화가 쓸 수 있는 토큰."""
        return context_budget(agent, tools=self.tools_for(agent, state))

    @staticmethod
    def system_tokens(agent: Agent, state: DebateState) -> int:
        """시스템 프롬프트 몫의 어림값. 페르소나·세션 지침·장부에 고정 지침 몫을 더합니다."""
        text = "\n\n".join(
            part for part in (agent.system_prompt, state.custom_instructions, state.decision_ledger)
            if part
        )
        return memory.text_tokens(agent.model, text) + 512

    @staticmethod
    def summary_cap(state: DebateState, budget: int) -> int:
        """요약의 글자 상한. 참여자 중 가장 작은 예산을 알면 그것에 맞춥니다."""
        return memory.memory_cap(
            memory.SUMMARY_SHARE, memory.SUMMARY_MAX_CHARS, state.memory_budget or budget
        )

    @staticmethod
    def snippet(msg: DebateMessage, limit: int) -> str:
        """짧게 옮길 때의 한 조각. 요지가 있으면 요지, 없으면 앞부분.

        앞 N자만 자르면 긴 발언은 서론만 남습니다. 요지는 발언자가 결론을 추려 둔 것입니다.
        자르기 전에 사고 과정을 뗍니다 — 그러지 않으면 N자가 "Thought 1: ..." 로 채워집니다.
        """
        digest = memory.extract_digest(msg.content) if msg.sender_key != "user" else None
        text = digest or strip_reasoning_trace(msg.content)
        return text[:limit]

    def routing_record(self, state: DebateState, agent: Agent) -> str:
        """발언자 지명·태스크 분배 프롬프트에 넣을 사용자 발언 기록 (짧은 몫).

        유저가 승인한 태스크 분담이 있으면 (ADR-028) 함께 싣습니다. 지명과 라운드별 분배는
        오케스트레이터가 그때그때 정하는데, 승인된 분담을 모르고 정하면 사람이 확인한 것과
        다른 일을 시키게 됩니다.
        """
        record = memory.build_user_record(
            state, model=agent.model,
            token_cap=int(context_budget(agent) * memory.ROUTING_RECORD_SHARE),
        ).text
        parts = [record] if record else []
        if state.plan_tasks:
            parts.append(
                f"{plan_gate.ROUTING_TASKS_HEADING}\n{plan_gate.tasks_block(state.plan_tasks)}"
            )
        return "".join(f"{part}\n\n" for part in parts)

    def load_memory(self, state: DebateState, session_model: SessionModel) -> None:
        """저장된 장부·요약을 이번 턴 상태로 옮깁니다."""
        ids = [m.id for m in state.messages]
        state.decision_ledger = session_model.decision_ledger or ""
        through = memory.through_index(ids, session_model.ledger_through_id)
        # 반영 지점이 기록에서 사라졌으면 지금까지를 반영된 것으로 봅니다. 옛 기록 전체를
        # 장부 갱신 한 번에 다시 밀어 넣지 않습니다.
        state.ledger_through = len(ids) if through is None else through

        summary = session_model.transcript_summary or ""
        summary_through = memory.through_index(ids, session_model.summary_through_id)
        # 요약은 덮는 자리를 모르면 쓸 수 없습니다 (원문과 겹치거나 빠집니다). 버리고,
        # 필요하면 다시 접습니다.
        if summary.strip() and summary_through:
            state.transcript_summary = summary
            state.summary_through = summary_through

    async def persist_memory(
        self, db, state: DebateState, on_event: Optional[EventCallback]
    ) -> None:
        """장부와 요약을 저장합니다. 턴이 끝까지 왔을 때만 부릅니다.

        긴급 종료(`session_ops.discard_turn`)는 턴 도중의 취소라 여기까지 오지 않습니다.
        그래서 지워진 발언을 반영한 장부가 남지 않습니다.
        """
        remembered = {
            "decision_ledger": state.decision_ledger,
            "ledger_through_id": memory.through_id(state.messages, state.ledger_through),
            "transcript_summary": state.transcript_summary if state.summary_through else "",
            "summary_through_id": memory.through_id(state.messages, state.summary_through),
        }
        # 기억을 적었다고 대화 목록에서 "방금 바뀐 대화" 로 올라가지 않게 합니다.
        values = {**remembered, "updated_at": SessionModel.updated_at}
        try:
            await db.execute(
                update(SessionModel).where(SessionModel.id == state.session_id).values(**values)
            )
            # 해당 턴 완료 시점의 대화 기억을 턴 레코드에도 보관합니다 (세션 분기 시 과거 기억 복원용, `app/session_fork.py`).
            if state.turn_id:
                await db.execute(update(TurnModel).where(TurnModel.id == state.turn_id).values(memory=remembered))
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 기억을 못 적어도 턴은 끝납니다
            logger.error(
                f"Could not persist the decision ledger for session {state.session_id}: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("Rollback after a failed ledger commit also failed", exc_info=True)
            if on_event:
                await on_event({
                    "type": EventType.PERSIST_FAILED,
                    "what": "the decision ledger",
                    "label": "결정 장부",
                    "error": f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                    "saved_to": None,
                })

    async def context_for_speech(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str,
        *,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """발언 맥락. 창을 넘으면 먼저 앞선 기록을 요약으로 접은 뒤 만듭니다."""
        prompt = self.build_context_for_agent(state, agent, turn_instruction)
        total = self.system_tokens(agent, state) + estimate_tokens(agent.model, prompt)
        folded = await self.ensure_room(
            state, model=agent.model, total=total, budget=self.speech_budget(agent, state),
            orchestrator=orchestrator, on_event=on_event, agent_name=agent.name,
        )
        if folded:
            prompt = self.build_context_for_agent(state, agent, turn_instruction)
        return prompt

    async def ensure_room(
        self,
        state: DebateState,
        *,
        model: str,
        total: int,
        budget: int,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        agent_name: str,
    ) -> bool:
        """`total` 이 `budget` 을 넘으면 앞선 기록을 요약으로 접습니다. 접었으면 True.

        접지 못해도 괜찮습니다 — `fit_context_window` 가 예전처럼 오래된 것부터 생략합니다.
        """
        if budget <= 0 or total <= budget:
            return False
        start = state.summary_through
        costs = [
            0 if m.msg_type == "error" or not turns.in_main_record(m)
            else memory.text_tokens(model, memory.render_message(m))
            for m in state.messages[start:]
        ]
        cut = memory.choose_fold_cut(
            state,
            message_tokens=costs,
            total_tokens=total,
            budget=budget,
            summary_allowance=(
                self.summary_cap(state, budget) - memory.text_tokens(model, state.transcript_summary)
            ),
        )
        if cut <= start:
            return False
        return await self.fold_into_summary(
            state, orchestrator=orchestrator, cut=cut, on_event=on_event, agent_name=agent_name,
            max_chars=self.summary_cap(state, budget),
        )

    async def fold_into_summary(
        self,
        state: DebateState,
        *,
        orchestrator: Agent,
        cut: int,
        on_event: Optional[EventCallback],
        agent_name: str,
        max_chars: int = memory.SUMMARY_MAX_CHARS,
    ) -> bool:
        """`messages[summary_through:cut]` 를 누적 요약에 접습니다. 조금이라도 접었으면 True.

        묶음마다 상태에 반영합니다. 뒤 묶음이 실패해도 앞에서 접은 것은 남습니다.
        """
        writer = self.tool_less(orchestrator)
        start = state.summary_through
        items = [
            (index, state.messages[index]) for index in range(start, cut)
            if state.messages[index].msg_type != "error" and turns.in_main_record(state.messages[index])
        ]
        if not items:
            return False
        room = (
            context_budget(writer)
            - memory.PROMPT_OVERHEAD_TOKENS
            - memory.text_tokens(writer.model, state.transcript_summary)
        )
        if room <= 0:
            logger.warning(
                f"No room to summarize earlier messages with {writer.name} "
                f"(max_context_window={writer.max_context_window}); older ones will be dropped instead"
            )
            return False
        batches = memory.batch_blocks(
            writer.model, [memory.render_message(m) for _index, m in items], room
        )[: memory.SUMMARY_MAX_BATCHES]

        if on_event:
            await on_event({
                "type": EventType.CONTEXT_SUMMARIZING,
                "agent_name": agent_name,
                "messages": cut - start,
            })

        consumed = 0
        try:
            for batch in batches:
                content, _ = await self.ctx.llm_caller.call_agent(
                    writer,
                    memory.summary_prompt(
                        previous=state.transcript_summary, blocks=batch,
                        covered=state.summary_through, max_chars=max_chars,
                    ),
                    state.custom_instructions,
                    session_id=state.session_id,
                    mcp=self.ctx.mcp_for(state), tool_gate=self.ctx.gate_for(state),
                )
                summary = memory.parse_summary(content, max_chars)
                if not summary:
                    raise ValueError("요약 응답이 비어 있습니다")
                consumed += len(batch)
                state.transcript_summary = summary
                state.summary_through = cut if consumed == len(items) else items[consumed - 1][0] + 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 요약에 실패하면 예전처럼 생략합니다
            logger.warning(
                f"Could not summarize earlier messages for {agent_name}; older ones will be "
                f"dropped instead: {type(exc).__name__}: {exc}"
            )
            if on_event:
                await on_event({
                    "type": EventType.CONTEXT_SUMMARY_FAILED,
                    "agent_name": agent_name,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        folded = state.summary_through - start
        if folded and on_event:
            await on_event({
                "type": EventType.CONTEXT_SUMMARIZED,
                "agent_name": agent_name,
                "folded": folded,
                "total": state.summary_through,
            })
        return folded > 0

    async def update_ledger(
        self,
        *,
        state: DebateState,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        reason: str,
    ) -> bool:
        """결정 장부를 갱신합니다. 갱신했으면 True.

        새 발언이 없거나 실패하면 이전 장부를 그대로 둡니다. 장부는 토론의 부산물이라,
        장부를 못 써서 토론이 멈추면 안 됩니다.
        """
        start, upto = state.ledger_through, len(state.messages)
        fresh = [
            (index, state.messages[index]) for index in range(start, upto)
            if state.messages[index].msg_type != "error" and turns.in_main_record(state.messages[index])
        ]
        # 사람의 말과 사람이 장부를 고친 기록만으로는 다시 쓸 것이 없습니다. 고친 값은 이미 장부에 있습니다.
        if not any(m.sender_key != "user" and turns.kind_of(m) != turns.KIND_LEDGER_EDIT for _index, m in fresh):
            return False

        writer = self.tool_less(orchestrator)
        budget = context_budget(writer)
        ledger_chars = memory.memory_cap(
            memory.LEDGER_SHARE, memory.LEDGER_MAX_CHARS, state.memory_budget or budget
        )
        record = memory.build_user_record(
            state, model=writer.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        # 고정된 사용자 발언은 프롬프트에 기록으로 따로 들어가므로 참조로만 둡니다.
        placeholders = {i: memory.user_placeholder(n) for i, n in record.refs.items()}
        opening = memory.opening_index(state)
        if opening is not None:
            placeholders[opening] = "(이번 턴 요청 — 위 [이번 턴 요청]에 전문이 있습니다)"

        room = (
            budget
            - memory.PROMPT_OVERHEAD_TOKENS
            - memory.text_tokens(writer.model, state.decision_ledger)
            - memory.text_tokens(writer.model, record.text)
            - memory.text_tokens(writer.model, state.user_prompt)
        )
        if room <= 0:
            logger.warning(
                f"No room to update the decision ledger with {writer.name} "
                f"(max_context_window={writer.max_context_window})"
            )
            return False

        # 최근 것부터 채웁니다. 못 실은 앞쪽은 이전 장부에 반영돼 있는 경우가 대부분입니다.
        kept: List[str] = []
        used = 0
        for index, msg in reversed(fresh):
            block = memory.render_message(msg, placeholders.get(index))
            cost = memory.text_tokens(writer.model, block)
            if kept and used + cost > room:
                break
            if cost > room:
                block = memory.clip_to_tokens(writer.model, block, room)
                cost = room
            kept.insert(0, block)
            used += cost

        if on_event:
            await on_event({"type": EventType.LEDGER_UPDATE_STARTED, "reason": reason})
        try:
            content, _ = await self.ctx.llm_caller.call_agent(
                writer,
                memory.ledger_prompt(
                    previous=state.decision_ledger,
                    user_record=record.text,
                    blocks=kept,
                    skipped=len(fresh) - len(kept),
                    user_prompt=state.user_prompt,
                    max_chars=ledger_chars,
                ),
                state.custom_instructions,
                session_id=state.session_id,
                mcp=self.ctx.mcp_for(state), tool_gate=self.ctx.gate_for(state),
            )
            ledger = memory.parse_ledger(content, ledger_chars)
            if ledger is None:
                raise ValueError("응답에 장부 형식(`## ` 제목)이 없습니다")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 장부를 못 써도 토론은 계속됩니다
            logger.warning(
                f"Could not update the decision ledger ({reason}) for session {state.session_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            if on_event:
                await on_event({
                    "type": EventType.LEDGER_UPDATE_FAILED,
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            return False

        state.decision_ledger = ledger
        state.ledger_through = upto
        if on_event:
            await on_event({"type": EventType.LEDGER_UPDATED, "reason": reason, "ledger": ledger})
        return True

    def build_context_for_agent(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str = "",
        *,
        use_summary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Prepares discussion transcript for agent turn.

        `turn_instruction` 은 전략이 이 차례에 붙이는 지침입니다. 전략이 순서만
        정하던 시절에는 '자유 토론' 과 '순차 검증' 이 똑같은 프롬프트를 받아,
        발언 순서 말고는 다를 것이 없었습니다. 두 전략의 실제 차이가 여기서
        갈립니다.

        첫 메시지(목표)에는 잘리면 안 되는 것을 고정합니다 — 사용자 발언 기록, 이번 턴
        계획, 앞선 논의 요약. 두 자르기 모두 이 자리를 남깁니다. 기록 안의 같은 발언은
        참조로 바꿔 토큰을 두 번 쓰지 않습니다.

        요약은 원문 전체가 이 발언자의 창에 들어가지 않을 때만 씁니다 (`use_summary`
        를 주지 않으면 여기서 판단). 창이 큰 에이전트는 원문을 그대로 읽습니다.
        """
        budget = self.speech_budget(agent, state)
        record = memory.build_user_record(
            state, model=agent.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        plan_pin = memory.build_plan_pin(
            state, model=agent.model, token_cap=int(budget * memory.PLAN_PIN_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, bool(plan_pin))
        summary = memory.summary_block(state)

        # 라운드 표시는 맨 끝에 둡니다. 목표 메시지에 있으면 라운드마다 두 번째 메시지가 바뀌어,
        # 그 뒤의 기록 전체가 프롬프트 캐시에서 빠집니다 (모듈 설명의 "프롬프트 캐싱").
        turn_prompt = (
            f"[Debate Progress]: Round {state.current_round} of {state.max_rounds}.\n\n"
            f"이제 {agent.name}({agent.role})님의 차례입니다. 앞선 전체 논의 맥락과 직전 "
            f"발언들을 충실히 반영하여 전문적인 의견을 발언하고 필요시 도구를 활용해 주세요."
        )
        if turn_instruction:
            turn_prompt += f"\n\n{turn_instruction}"
        # 유저가 승인한 이 전문가의 태스크 (ADR-028). 고정문에도 있지만, 마지막 메시지에 한 번 더
        # 둡니다 — 긴 토론에서 자기 몫을 가장 덜 잊는 자리입니다.
        own_task = plan_gate.own_task(state.plan_tasks, agent.key)
        if own_task:
            turn_prompt += f"\n\n{own_task}"
        turn_prompt += f"\n\n{memory.DIGEST_INSTRUCTION}"

        # 이 발언자가 마지막으로 말한 뒤에 나온 발언은 원문으로 (모듈 설명의 "발언을 얼마나
        # 넘길까"). 이번 턴에 아직 말하지 않았으면 이번 턴 전체가 새 발언입니다.
        fresh_start = state.turn_message_start
        for index in range(len(state.messages) - 1, -1, -1):
            msg = state.messages[index]
            # 서브 토론에서 한 말은 본 토론의 응답이 아닙니다. 그 앞의 발언은 아직 답하지 않은 것입니다.
            if msg.sender_key == agent.key and msg.msg_type != "error" and turns.in_main_record(msg):
                fresh_start = max(fresh_start, index + 1)
                break
        mentions = {mention_token(agent.name), f"@{agent.key}"}

        def body_for(index: int, msg: DebateMessage) -> str:
            pinned = placeholders.get(index)
            if pinned:
                return pinned
            body = strip_reasoning_trace(msg.content)
            if index >= fresh_start and msg.sender_key != agent.key:
                return body
            if (
                msg.sender_key != agent.key
                and len(body) > memory.DIGEST_MIN_BODY_CHARS
                and not any(token in body for token in mentions)
            ):
                digest = memory.extract_digest(msg.content)
                if digest:
                    return f"{memory.DIGEST_HEADING} (전문 {len(body):,}자 중 요지만 싣습니다)\n{digest}"
            return memory.reference_code_blocks(body, msg.tool_calls)

        def assemble(start: int, with_summary: bool) -> List[Dict[str, Any]]:
            head = [f"[User Goal / Current Request]:\n{state.user_prompt}"]
            head += [part for part in (record.text, plan_pin, summary if with_summary else "") if part]
            context: List[Dict[str, Any]] = [{"role": "user", "content": "\n\n".join(head)}]

            for index in range(start, len(state.messages)):
                msg = state.messages[index]
                # 응답을 못 받은 자리는 맥락에 넣지 않습니다. 실패 안내문을 발언인 양
                # 읽히게 하면 다음 에이전트가 그것을 논평하기 시작합니다. 서브 토론 안의 발언도
                # 넣지 않습니다 — 합류하는 것은 결론입니다.
                if msg.msg_type == "error" or not turns.in_main_record(msg):
                    continue
                if msg.sender_key == "user":
                    context.append({
                        "role": "user",
                        "content": f"[User]:\n{placeholders.get(index, msg.content)}"
                    })
                else:
                    role_label = f"[{msg.speaker}]"
                    context.append({
                        "role": "assistant" if msg.sender_key == agent.key else "user",
                        # 사고 과정은 기록과 화면에만 남기고 프롬프트에는 싣지 않습니다.
                        # 자기 발언도 같습니다 — `show_steps` 는 사람이 무엇을 볼지를
                        # 정하는 스위치이지, 모델이 무엇을 읽을지를 정하는 것이 아닙니다.
                        "content": f"{role_label}:\n{body_for(index, msg)}",
                    })

            context.append({"role": "user", "content": turn_prompt})
            return context

        full = assemble(0, False)
        if not summary:
            return full
        if use_summary is None:
            use_summary = (
                budget > 0
                and self.system_tokens(agent, state) + estimate_tokens(agent.model, full) > budget
            )
        return assemble(state.summary_through, True) if use_summary else full

    def memory_search_tool_for(
        self, agent: Optional[Agent], state: Optional[DebateState] = None,
    ) -> Optional[str]:
        """이 에이전트가 실제로 쓸 수 있는 메모리 검색 도구의 이름. 없으면 None.

        서버 키가 아니라 **주어진 도구 목록**에서 찾습니다. 서버를 껐거나 연결에
        실패했으면 없는 도구를 가리키게 되기 때문입니다. 그 목록은 이 턴이 빌린
        런타임에서 나옵니다 — 대화마다 작업 공간이 다르면 뜬 서버도 다릅니다.
        """
        if agent is None:
            return None
        return memory_search_tool(self.tools_for(agent, state))

    def tools_for(
        self, agent: Optional[Agent], state: Optional[DebateState] = None,
    ) -> List[Dict[str, Any]]:
        """이 에이전트의 요청에 실릴 도구 정의. 못 구하면 빈 목록.

        도구 정의는 요청마다 함께 나가는 입력이라, 전사의 크기를 정할 때도 그 몫을 빼야
        합니다 (`context_budget`). 대역 호출기처럼 도구를 모르는 경우에도 합성은 진행합니다.
        """
        if agent is None:
            return []
        if state is not None:
            agent = with_designated_skills(agent, state.skill_designations.get(agent.key))
        try:
            mcp = (self.ctx.mcp_for(state) if state else None) or self.ctx.llm_caller.mcp_manager
            tools = mcp.get_openai_tools_for_servers(
                self.ctx.llm_caller.resolve_tool_servers(agent)
            ) or []
            # 발언 시 실제로 전달되는 도구 목록과 일치해야 합니다 (보안상 제외된 도구는 제외하고 스킬 도구는 포함합니다).
            gate = self.ctx.gate_for(state) if state else None
            if gate is not None:
                tools = gate.filter_tools(agent.key, tools, mcp)
            return plan_shelf(
                agent, tools, skill_tools_for(agent) + consultation_tools_for(agent),
                session_id=state.session_id if state else None,
            ).offered()
        except Exception:  # noqa: BLE001 - 도구 목록을 못 구해도 합성은 진행합니다
            return []
