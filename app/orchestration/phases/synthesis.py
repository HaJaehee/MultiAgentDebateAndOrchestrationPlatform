"""3단계: 합성.

오케스트레이터가 토론을 종합해 최종 보고서를 쓰고(`conclude`), 다이어그램 문법을 스스로 고치게
한 뒤(`repair_mermaid_blocks`), 이번 턴의 산출물을 뽑아 기록합니다(`extract_artifacts_from_synthesis`).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.agents.llm_context import context_budget, context_trim_notice, estimate_tokens
from app.agents.reasoning import strip_reasoning_trace
from app.database.models import TURN_COMPLETED, ArtifactModel, utc_now
from app.events import EventCallback, EventType
from app.mermaid_lint import format_issues, lint_mermaid
from app.orchestration import context_memory as memory
from app.orchestration import plan_gate, turns
from app.orchestration.artifacts import (
    CODE_LANGUAGES,
    MAX_DEBATE_CODE_ARTIFACTS,
    MERMAID_REPAIR_ATTEMPTS,
    extract_code_blocks,
    find_mermaid_blocks,
    normalize_mermaid,
    synthesis_has_content,
)
from app.orchestration.control import TurnControl
from app.orchestration.phases.base import PhaseExecutor
from app.orchestration.state import ArtifactItem, DebateMessage, DebateState
from app.timestamps import report_completed_line, to_local

if TYPE_CHECKING:
    from app.orchestration.turn_setup import TurnSetup

logger = logging.getLogger(__name__)



class SynthesisPhaseExecutor(PhaseExecutor):
    """최종 합의 보고서 작성, Mermaid 다이어그램 자가 수선, 산출물 추출 및 턴 마무리를 전담합니다."""

    async def conclude(
        self,
        db: Any,
        state: DebateState,
        setup: "TurnSetup",
        *,
        stopped_early: bool,
        turn_started_at: Optional[datetime],
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> None:
        """최종 합성 보고서 작성, 산출물 추출, 결정 장부 갱신 및 턴 완료 처리를 수행합니다."""
        session_id = state.session_id
        orchestrator_agent = setup.orchestrator
        custom_instructions = setup.custom_instructions

        await self.recorder.apply_interjections(
            db=db,
            state=state,
            control=control,
            round_number=state.current_round,
            on_event=on_event,
        )
        state.stopped_early = stopped_early
        if stopped_early:
            logger.info(
                f"Debate for session {session_id} "
                + ("was interrupted by a restart" if state.interrupted else "stopped early by the user")
                + f" at round {state.current_round}/{setup.max_rounds}; synthesizing what we have."
            )

        state.status = "synthesizing"
        state.current_speaker = orchestrator_agent.name
        await self.recorder.set_turn(db, state, phase="synthesizing", stopped_early=stopped_early)
        if on_event:
            await on_event({
                "type": EventType.STATUS_CHANGED,
                "status": "synthesizing",
                "speaker": orchestrator_agent.name,
            })

        async def _fix_diagrams(text: str) -> str:
            return await self.repair_mermaid_blocks(
                text,
                agent=orchestrator_agent,
                custom_instructions=custom_instructions,
                state=state,
                on_event=on_event,
            )

        synth_message = await self.speech.speak(
            db=db,
            state=state,
            agent=orchestrator_agent,
            prompt_messages=await self.synthesis_prompt_with_notice(
                state, orchestrator_agent, on_event
            ),
            custom_instructions=custom_instructions,
            round_number=state.current_round + 1,
            msg_type="orchestrator",
            on_event=on_event,
            post_process=_fix_diagrams,
            turn_started_at=turn_started_at,
            kind=turns.KIND_SYNTHESIS,
        )
        synthesis_failed = synth_message.msg_type == "error"
        failure_reason = "LLM 연결 끊김" if synthesis_failed else ""
        if not synthesis_failed and not synthesis_has_content(synth_message.content):
            synthesis_failed = True
            failure_reason = "빈 응답"
            logger.error(
                f"Synthesis for session {session_id} came back empty; "
                f"keeping each specialist's latest speech as the report instead"
            )

        artifacts = self.extract_artifacts_from_synthesis(
            session_id,
            synth_message.content,
            state,
            synthesis_failed=synthesis_failed,
            failure_reason=failure_reason,
            completed_at=synth_message.finished_at,
            turn_started_at=turn_started_at,
        )
        for art in artifacts:
            art.id = str(uuid.uuid4())
            state.artifacts.append(art)

        if artifacts:
            fence = "`" * 3
            await self.recorder.persist(
                db,
                lambda: [
                    ArtifactModel(
                        id=art.id,
                        session_id=session_id,
                        artifact_type=art.artifact_type,
                        title=art.title,
                        content=art.content,
                        language=art.language,
                    )
                    for art in artifacts
                ],
                what=f"{len(artifacts)} artifact(s)",
                label=f"산출물 {len(artifacts)}건",
                kind="artifacts",
                session_id=session_id,
                fallback_title="산출물",
                fallback_body="\n\n".join(
                    f"## {art.title} ({art.artifact_type})\n\n"
                    f"{fence}{art.language or ''}\n{art.content}\n{fence}"
                    for art in artifacts
                ),
                on_event=on_event,
            )

        if on_event:
            await on_event({
                "type": EventType.ARTIFACTS_SYNTHESIZED,
                "artifacts": [a.model_dump() for a in state.artifacts],
            })

        await self.conversation.update_ledger(
            state=state,
            orchestrator=orchestrator_agent,
            on_event=on_event,
            reason="최종 합성",
        )

        deferred = await self.recorder.apply_interjections(
            db=db,
            state=state,
            control=control,
            round_number=state.current_round + 1,
            on_event=on_event,
        )
        if deferred and on_event:
            await on_event({"type": EventType.INTERJECTIONS_DEFERRED, "count": deferred})

        await self.conversation.persist_memory(db, state, on_event)

        state.status = "completed"
        await self.recorder.settle_drafts(db, state, setup, on_event=on_event)
        await self.recorder.set_turn(
            db, state, status=TURN_COMPLETED, phase="completed", finished_at=utc_now(),
        )
        # 합의: 모두가 발언했고, 덜 논의되지 않았고, 결론이 나왔고, 마지막 장부에 미해결 쟁점이 없습니다.
        # 장부를 읽지 못하면(형식이 아니면) 합의로 적지 않습니다.
        state.is_consensus_reached = (
            not state.failed_agent_keys and not state.stopped_early and not synthesis_failed
            and memory.open_issue_count(state.decision_ledger) == 0
        )
        if state.failed_agent_keys:
            state.error_message = (
                "다음 에이전트가 LLM 엔드포인트에 닿지 못했습니다: "
                + ", ".join(state.failed_agent_keys)
            )
        elif synthesis_failed:
            state.error_message = (
                "오케스트레이터의 최종 결론이 비어 있었습니다. 전문가별 마지막 발언을 "
                "산출물로 남겼습니다."
            )

        if on_event:
            await on_event({
                "type": EventType.TURN_COMPLETED,
                "status": "completed",
                "turn_id": state.turn_id,
                "failed_agents": list(state.failed_agent_keys),
                "fallback_agents": list(state.fallback_agent_keys),
                "error_message": state.error_message,
                "stopped_early": state.stopped_early,
                "interrupted": state.interrupted,
                "rounds_completed": state.current_round,
                "max_rounds": state.max_rounds,
                "usage": state.usage.as_meta(),
                "converged_round": state.converged_round,
                "synthesis_id": next(
                    (m.id for m in reversed(state.messages) if turns.kind_of(m) == turns.KIND_SYNTHESIS), None
                ),
                "budget_stopped": state.budget_stopped,
                "consensus": state.is_consensus_reached,
            })

    async def synthesis_prompt_with_notice(
        self,
        state: DebateState,
        agent: Optional[Agent],
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """합성 프롬프트를 만들고, 전사가 잘렸으면 화면에 알립니다."""
        if agent is not None:
            await self.ensure_synthesis_room(state, agent, on_event)
        before = state.context_dropped
        prompt = self.build_synthesis_prompt(state, agent)
        dropped = state.context_dropped - before
        if dropped and on_event:
            await on_event({
                "type": EventType.CONTEXT_TRIMMED,
                "agent_key": agent.key if agent else "orchestrator",
                "agent_name": agent.name if agent else "Orchestrator",
                "dropped": dropped,
                "total_dropped": state.context_dropped,
                "where": "synthesis",
            })
        return prompt

    async def ensure_synthesis_room(
        self, state: DebateState, agent: Agent, on_event: Optional[EventCallback]
    ) -> None:
        """합성 대화 기록이 컨텍스트 창 한도를 초과하면 이전 라운드 기록을 요약본으로 압축합니다."""
        budget = context_budget(agent, tools=self.conversation.tools_for(agent, state)) - 512
        record = memory.build_user_record(
            state, model=agent.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        summary = memory.summary_block(state)
        start = state.summary_through if summary else 0
        total = (
            memory.text_tokens(agent.model, record.text)
            + memory.text_tokens(agent.model, summary)
            + sum(
                memory.text_tokens(agent.model, memory.render_message(m))
                for m in state.messages[start:] if m.msg_type != "error"
            )
        )
        await self.conversation.ensure_room(
            state,
            model=agent.model,
            total=total,
            budget=budget,
            orchestrator=agent,
            on_event=on_event,
            agent_name=agent.name,
        )

    def build_synthesis_prompt(
        self, state: DebateState, agent: Optional[Agent] = None
    ) -> List[Dict[str, Any]]:
        """Constructs prompt for orchestrator final consensus & artifact generation."""
        model = agent.model if agent is not None else "gpt-4o"
        budget = (
            context_budget(agent, tools=self.conversation.tools_for(agent, state)) - 512
            if agent is not None else 0
        )
        record = memory.build_user_record(
            state,
            model=model,
            token_cap=int(budget * memory.USER_RECORD_SHARE) if agent is not None else 10 ** 9,
        )
        placeholders = memory.placeholders_for(state, record, plan_pinned=False)

        def render(index: int, msg: DebateMessage) -> str:
            prefix = "### [User]" if msg.sender_key == "user" else f"### {msg.speaker}"
            body = msg.content if msg.sender_key == "user" else strip_reasoning_trace(msg.content)
            return f"{prefix}:\n{placeholders.get(index) or body}\n"

        summary = memory.summary_block(state) if agent is not None else ""
        start = 0
        if summary:
            full_cost = sum(
                memory.text_tokens(model, render(i, m))
                for i, m in enumerate(state.messages) if m.msg_type != "error"
            )
            if full_cost + memory.text_tokens(model, record.text) > budget:
                start = state.summary_through
            else:
                summary = ""
        usable = [
            (i, m) for i, m in enumerate(state.messages)
            if i >= start and m.msg_type != "error"
        ]

        if agent is None:
            kept, dropped = [render(i, m) for i, m in usable], 0
        else:
            budget -= memory.text_tokens(model, record.text) + memory.text_tokens(model, summary)
            kept_rev: List[str] = []
            dropped = 0
            for index, msg in reversed(usable):
                block = render(index, msg)
                probe = [{"role": "user", "content": "\n".join([block] + kept_rev)}]
                if kept_rev and budget > 0 and estimate_tokens(agent.model, probe) > budget:
                    dropped = len(usable) - len(kept_rev)
                    break
                kept_rev.insert(0, block)
            kept = kept_rev

        if dropped:
            logger.warning(
                f"Synthesis transcript trimmed: dropped {dropped} of {len(usable)} message(s) "
                f"(max_context_window={agent.max_context_window})"
            )
            state.context_dropped += dropped
            memory_tool = self.conversation.memory_search_tool_for(agent, state)
            notice = context_trim_notice(dropped, memory_tool)
            if memory_tool:
                notice += (
                    f"\n[생략된 초반 라운드가 결론에 필요하면 `{memory_tool}` 로 "
                    f"이 대화의 그래프를 찾아보세요 — 토론 중에 에이전트들이 "
                    f"핵심을 그곳에 남겼을 수 있습니다.]"
                )
            kept.insert(0, notice + "\n")

        full_transcript = "\n".join(kept)

        early_stop = ""
        if state.stopped_early:
            if state.interrupted:
                why = (
                    "서버가 다시 시작되어 토론이 예정보다 일찍 끊겼고, 유저가 지금까지의 논의로 "
                    "결론을 내도록 골랐습니다"
                )
            elif state.budget_stopped:
                why = "이번 턴의 토큰 예산을 다 써서 예정된 라운드보다 일찍 토론을 마쳤습니다"
            else:
                why = "유저가 예정된 라운드보다 일찍 토론을 정지시켰습니다"
            early_stop = (
                f"\n[주의] {why} "
                f"(진행: {state.current_round}/{state.max_rounds} 라운드). 남은 라운드에서 "
                f"나왔을 의견을 추측해 채우지 말고, 지금까지 오간 논의만으로 정리하되 "
                f"아직 검토되지 못한 쟁점을 보고서에 명시하세요.\n"
            )
        elif state.converged_round is not None:
            early_stop = (
                f"\n[참고] Round {state.converged_round} 에서 결정 장부가 수렴해(미해결 쟁점 없음, 새 결정 없음) "
                f"남은 라운드를 건너뛰었습니다 (진행: {state.current_round}/{state.max_rounds} 라운드). "
                f"덜 논의된 것이 아니라 합의로 마친 것이니 그렇게 정리하세요.\n"
            )

        missing = ""
        if state.failed_agent_keys:
            missing = (
                f"\n[주의] 다음 에이전트는 LLM 연결 실패로 이번 토론에서 발언하지 못했습니다: "
                f"{', '.join(state.failed_agent_keys)}. 이들의 의견을 추측해서 채우지 말고, "
                f"보고서에 누락 사실을 명시하세요.\n"
            )

        completion = ""
        if state.plan_tasks:
            speeches: Dict[str, int] = {}
            for m in state.messages[state.turn_message_start:]:
                if turns.kind_of(m) == turns.KIND_SPEECH and m.msg_type != "error":
                    speeches[m.sender_key] = speeches.get(m.sender_key, 0) + 1
            completion = "\n" + plan_gate.completion_block(
                state.plan_tasks, speeches, state.failed_agent_keys
            ) + "\n"

        pinned = "".join(f"{part}\n\n" for part in (record.text, summary) if part)
        prompt = (
            f"[User Goal]: {state.user_prompt}\n\n"
            f"{pinned}"
            f"[Full Multi-Agent Debate Transcript]:\n{full_transcript}\n"
            f"{early_stop}"
            f"{missing}"
            f"{completion}\n"
            f"수석 오케스트레이터로서 토론을 종합해 최종 합의 보고서를 작성하세요. "
            f"이 보고서가 맡는 것은 **결론과 종합 다이어그램까지**입니다.\n"
            f"다음 {'세' if completion else '두'} 가지만 쓰세요:\n"
            f"1. **최종 합의 결론** — 결정 사항과 그 근거, 채택하지 않은 대안과 이유, "
            f"남은 쟁점과 위험\n"
            f"2. **종합 Mermaid 다이어그램** 하나 (```mermaid 블록. 노드 라벨에 괄호를 쓸 때는 "
            f'A["결제 서비스 (Payment)"] 처럼 반드시 큰따옴표로 감쌀 것)\n'
            + (f"3. {plan_gate.COMPLETION_INSTRUCTION}\n" if completion else "") +
            "\n"
            "[하지 말 것] 소스 코드를 다시 쓰거나 붙여 넣지 마세요. 코드는 전문가 발언과 작업 "
            "공간 파일에 이미 있고, 산출물 탭에도 전문가 발언에서 따로 모읍니다. 코드가 필요한 "
            "자리는 파일 경로·모듈·함수 이름으로만 가리키세요."
        )
        return [{"role": "user", "content": prompt}]

    # ------------------------------------------------- 다이어그램 문법 자가 수선

    async def repair_mermaid_blocks(
        self,
        text: str,
        *,
        agent: Agent,
        custom_instructions: str,
        state: DebateState,
        on_event: Optional[EventCallback],
    ) -> str:
        """합성 본문의 Mermaid 다이어그램 문법을 검증하고, 오류가 발견되면 오케스트레이터에게 수정을 요청합니다."""
        max_attempts = MERMAID_REPAIR_ATTEMPTS
        blocks = find_mermaid_blocks(text)
        if not blocks:
            return text

        broken = [
            (i, b, issues)
            for i, b in enumerate(blocks)
            if (issues := lint_mermaid(normalize_mermaid(b["code"])))
        ]
        if not broken:
            return text

        logger.warning(
            f"{len(broken)} of {len(blocks)} mermaid diagram(s) in {agent.name}'s synthesis "
            f"failed the syntax check: "
            + "; ".join(f"#{i + 1} {[x.rule for x in issues]}" for i, _b, issues in broken)
        )

        fixer = agent.model_copy(update={
            "allowed_mcp_servers": [],
            "allowed_skills": [],
            "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
        })

        current = text
        for attempt in range(1, max_attempts + 1):
            if on_event:
                await on_event({
                    "type": EventType.MERMAID_REPAIR_STARTED,
                    "agent_name": agent.name,
                    "broken": len(broken),
                    "total": len(blocks),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                })

            try:
                fixed = await self.ask_for_fixed_diagrams(
                    fixer, broken, state, custom_instructions, attempt
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Mermaid repair call failed: {exc}")
                break

            if not fixed:
                logger.warning("The repair answer contained no mermaid block; giving up on this pass.")
                break

            current = self.splice_blocks(current, broken, fixed)

            blocks = find_mermaid_blocks(current)
            broken = [
                (i, b, issues)
                for i, b in enumerate(blocks)
                if (issues := lint_mermaid(normalize_mermaid(b["code"])))
            ]
            if not broken:
                logger.info(f"Mermaid diagrams fixed on attempt {attempt}.")
                if on_event:
                    await on_event({
                        "type": EventType.MERMAID_REPAIR_FINISHED,
                        "agent_name": agent.name,
                        "resolved": True,
                        "attempts": attempt,
                        "remaining": 0,
                    })
                return current

        if on_event:
            await on_event({
                "type": EventType.MERMAID_REPAIR_FINISHED,
                "agent_name": agent.name,
                "resolved": False,
                "attempts": max_attempts,
                "remaining": len(broken),
            })
        logger.warning(
            f"{len(broken)} mermaid diagram(s) still fail the syntax check after "
            f"{max_attempts} attempt(s); keeping the original."
        )
        return current

    async def ask_for_fixed_diagrams(
        self,
        fixer: Agent,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        state: DebateState,
        custom_instructions: str,
        attempt: int,
    ) -> List[str]:
        """문법 오류가 발생한 다이어그램만 취합하여 수정을 요청하고, 보정된 다이어그램 블록을 순서대로 반환합니다."""
        parts = [
            "방금 작성한 보고서의 Mermaid 다이어그램이 문법 오류로 렌더링되지 않습니다.",
            "",
            f"아래 {len(broken)}개를 고쳐서 다시 주세요. **다이어그램만** 주면 됩니다 — "
            "보고서 본문은 다시 쓰지 마세요.",
            "",
        ]
        for order, (_index, block, issues) in enumerate(broken, start=1):
            parts += [
                f"### 다이어그램 {order}",
                "",
                "렌더러가 지적한 곳:",
                format_issues(issues),
                "",
                "원본:",
                "```mermaid",
                block["code"],
                "```",
                "",
            ]
        parts += [
            "---",
            "",
            f"고친 다이어그램 {len(broken)}개를 **같은 순서로**, 각각 ```mermaid 코드 블록"
            " 하나씩으로 출력하세요. 다른 설명은 붙이지 마세요.",
            "",
            "지킬 것:",
            '- 라벨에 괄호·따옴표·특수문자가 있으면 큰따옴표로 감싸세요: `A["결제 (PG)"]`',
            "- `end` 는 예약어입니다. 노드 이름으로 쓰지 마세요 (`END` 는 됩니다).",
            "- `subgraph` 는 반드시 `end` 로 닫으세요.",
            "- 첫 줄은 `graph TD` 처럼 다이어그램 종류 선언이어야 합니다.",
            "- **의미는 바꾸지 마세요.** 노드와 관계는 그대로 두고 문법만 고치세요.",
        ]
        if attempt > 1:
            parts.append(
                f"- 이번이 {attempt}번째 시도입니다. 앞서 고친 것도 같은 이유로 실패했으니, "
                f"의심스러운 라벨은 전부 큰따옴표로 감싸세요."
            )

        content, _ = await self.ctx.llm_caller.call_agent(
            fixer,
            [{"role": "user", "content": "\n".join(parts)}],
            custom_instructions,
            session_id=state.session_id,
            mcp=self.ctx.mcp_for(state),
            tool_gate=self.ctx.gate_for(state),
        )
        return [b["code"] for b in find_mermaid_blocks(content or "")]

    @staticmethod
    def splice_blocks(
        text: str,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        replacements: List[str],
    ) -> str:
        """수정된 다이어그램을 원문의 본래 위치에 치환하여 삽입합니다."""
        pairs = list(zip(broken, replacements))
        for (_index, block, _issues), fixed in sorted(pairs, key=lambda x: -x[0][1]["start"]):
            text = text[:block["start"]] + fixed + text[block["end"]:]
        return text

    @staticmethod
    def turn_speeches(state: DebateState) -> List[DebateMessage]:
        """이번 턴의 전문가 발언 목록을 반환합니다. 사용자 입력, 오케스트레이터 메시지, 실패 안내 등은 제외합니다."""
        return [
            m for m in state.messages[state.turn_message_start:]
            if m.sender_key not in ("user", "orchestrator") and m.msg_type != "error"
        ]

    def debate_code_artifacts(self, state: DebateState, stamp: str) -> List[ArtifactItem]:
        """이번 턴의 전문가 발언에서 코드 블록 산출물을 추출합니다."""
        latest: Dict[str, Tuple[DebateMessage, List[Dict[str, str]]]] = {}
        for msg in self.turn_speeches(state):
            blocks = [
                b for b in extract_code_blocks(strip_reasoning_trace(msg.content))
                if b["language"] in CODE_LANGUAGES
            ]
            if blocks:
                latest[msg.sender_key] = (msg, blocks)

        artifacts: List[ArtifactItem] = []
        seen = set()
        for msg, blocks in latest.values():
            for block in blocks:
                key = block["code"].strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                lang = "python" if block["language"] == "py" else block["language"]
                artifacts.append(ArtifactItem(
                    artifact_type="code",
                    title=f"{stamp} {lang} · {msg.sender_name} R{msg.round_number}".strip(),
                    content=block["code"],
                    language=lang,
                ))
                if len(artifacts) >= MAX_DEBATE_CODE_ARTIFACTS:
                    return artifacts
        return artifacts

    def synthesis_fallback_report(
        self, state: DebateState, failure_reason: str, synth_text: str
    ) -> str:
        """합성 응답이 비어 있거나 실패했을 때 대체 보고서를 생성합니다 (전문가별 최종 발언 취합)."""
        latest: Dict[str, DebateMessage] = {}
        for msg in self.turn_speeches(state):
            latest[msg.sender_key] = msg

        what = (
            "만들지 못했습니다 (LLM 연결 끊김)" if failure_reason == "LLM 연결 끊김"
            else f"비워 두었습니다 ({failure_reason})"
        )
        parts = [
            f"> ⚠️ **오케스트레이터가 이번 턴의 최종 결론을 {what}.**\n"
            f">\n"
            f"> 토론 기록은 그대로 남아 있습니다. 아래는 LLM 없이 모은 **전문가별 마지막 "
            f"발언**입니다. 이전 턴의 결론은 산출물 탭에 그대로 있습니다. 결론이 필요하면 "
            f"같은 대화에서 \"결론만 다시 정리해 줘\" 처럼 요청하세요."
        ]
        notice = (synth_text or "").strip()
        if notice and failure_reason == "LLM 연결 끊김":
            parts.append(notice)
        if latest:
            parts.append("## 전문가별 마지막 발언")
            for msg in latest.values():
                body = strip_reasoning_trace(msg.content).strip() or "(내용 없음)"
                parts.append(
                    f"### {msg.speaker} — Round {msg.round_number}\n\n{body}"
                )
        else:
            parts.append("_이번 턴에는 기록된 전문가 발언이 없습니다._")
        return "\n\n".join(parts) + "\n"

    def extract_artifacts_from_synthesis(
        self,
        session_id: str,
        synth_text: str,
        state: DebateState,
        synthesis_failed: bool = False,
        completed_at: Optional[datetime] = None,
        turn_started_at: Optional[datetime] = None,
        failure_reason: str = "LLM 연결 끊김",
    ) -> List[ArtifactItem]:
        """이 턴의 산출물: 최종 결론, 종합 다이어그램, 전문가 코드, 요약 JSON."""
        stamp = to_local(completed_at).strftime("%m-%d %H:%M") if completed_at else ""
        artifacts: List[ArtifactItem] = []

        if synthesis_failed:
            artifacts.append(ArtifactItem(
                artifact_type="markdown",
                title=f"{stamp} 합성 실패 ({failure_reason or 'LLM 연결 끊김'})".strip(),
                content=self.synthesis_fallback_report(state, failure_reason, synth_text),
                language="markdown",
            ))
        else:
            completed_line = report_completed_line(
                completed_at,
                turn_started_at,
                paused_seconds=state.paused_seconds,
                resumed_count=state.resumed_count,
            )
            report = (
                f"{synth_text.rstrip()}\n\n---\n\n{completed_line}\n" if completed_line else synth_text
            )
            artifacts.append(ArtifactItem(
                artifact_type="markdown",
                title=f"{stamp} 최종 결론".strip(),
                content=report,
                language="markdown",
            ))

        def diagram_item(title: str, raw_code: str, source: str) -> ArtifactItem:
            code = normalize_mermaid(raw_code)
            issues = lint_mermaid(code)
            if issues:
                logger.warning(
                    f"Diagram '{title}' from {source} still fails the syntax check: "
                    f"{[i.rule for i in issues]}"
                )
                title = f"⚠ {title}"
            return ArtifactItem(artifact_type="mermaid", title=title, content=code, language="mermaid")

        mermaid_idx = 1
        if not synthesis_failed:
            for block in extract_code_blocks(synth_text):
                if block["language"] != "mermaid":
                    continue
                artifacts.append(diagram_item(
                    f"{stamp} 종합 다이어그램" + (f" #{mermaid_idx}" if mermaid_idx > 1 else ""),
                    block["code"],
                    "the synthesis",
                ))
                mermaid_idx += 1

        if mermaid_idx == 1:
            for msg in reversed(state.messages[state.turn_message_start:]):
                if msg.msg_type == "error" or msg.sender_key == "user":
                    continue
                found = [b for b in extract_code_blocks(msg.content) if b["language"] == "mermaid"]
                if not found:
                    continue
                for block in found:
                    artifacts.append(diagram_item(
                        f"{stamp} 다이어그램 #{mermaid_idx} ({msg.sender_name} 제안)".strip(),
                        block["code"],
                        msg.sender_name,
                    ))
                    mermaid_idx += 1
                break

        artifacts.extend(self.debate_code_artifacts(state, stamp))

        # 4. 요약 JSON
        json_summary = {
            "session_id": session_id,
            "goal": state.user_prompt,
            "strategy": state.strategy,
            "total_rounds": state.current_round,
            "participating_agents": state.active_agent_keys,
            "failed_agents": list(state.failed_agent_keys),
            "fallback_agents": list(state.fallback_agent_keys),
            "synthesis_failed": synthesis_failed,
            "total_messages": len(state.messages),
            # 모두 발언했고, 덜 논의되지 않았고, 결론이 나왔는가. 장부의 합의 판정(`turn_completed`
            # 의 `consensus`)은 이 요약을 만든 뒤에 마지막 장부로 정해지므로 여기에는 없습니다.
            "completed_cleanly": (
                not state.failed_agent_keys and not synthesis_failed and not state.stopped_early
            ),
        }
        if state.converged_round is not None:
            json_summary["converged_round"] = state.converged_round
        if state.budget_stopped:
            json_summary["budget_stopped"] = True
        if state.resumed_count:
            json_summary["resumed_count"] = state.resumed_count
            json_summary["paused_seconds"] = state.paused_seconds
            json_summary["interrupted"] = state.interrupted
        artifacts.append(ArtifactItem(
            artifact_type="json",
            title=f"{stamp} 토론 요약 (JSON)".strip(),
            content=json.dumps(json_summary, indent=2, ensure_ascii=False),
            language="json",
        ))

        return artifacts

