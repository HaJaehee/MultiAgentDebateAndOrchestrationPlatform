"""한 턴을 처음부터 끝까지 돌리는 엔진: 준비 → 계획 → (승인) → 토론 → 합성.

일은 서비스(`recording`·`speech`·`conversation`·`turn_setup`)와 단계 실행기(`phases/`)가 합니다.
엔진은 그것들을 조립하고, 턴의 수명 — 작업 공간 런타임 대여와 반납, 턴 기록, 끊긴 턴의 재개 —
을 맡습니다.
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select, update

from app import workspace_checkpoint

from app.agents.consultation import consultation_scope
from app.agents.llm import LLMCaller
from app.agents.llm_loop import SPEECH_STATE_VERSION
from app.agents.llm_gate import LLM_HOLDER
from app.agents.llm_usage import Usage, measuring
from app.agents.pool import AgentPool, get_agent_pool
from app.config import resolve_workspace_dir
from app.database.models import (
    TURN_RUNNING,
    TURN_UNFINISHED,
    SessionModel,
    SpeechDraftModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.database.session import get_session_factory
from app.events import EventCallback, EventType
from app.mcp.pool import get_runtime_pool
from app.orchestration import turns
from app.orchestration.context import EngineContext
from app.orchestration.control import TurnControl
from app.orchestration.conversation import ConversationMemory
from app.orchestration.graph import GraphSpec, GraphTurnError, parse_graph
from app.orchestration.phases.debate import DebatePhaseExecutor
from app.orchestration.phases.planning import PlanningPhaseExecutor
from app.orchestration.phases.synthesis import SynthesisPhaseExecutor
from app.orchestration.recording import TurnRecorder
from app.orchestration.speech import SpeechRunner
from app.orchestration.state import DebateState
from app.orchestration.strategies import get_strategy, resolve_strategy_name
from app.orchestration.turn_setup import TurnPreparer, new_state, restore_turn_state

logger = logging.getLogger(__name__)


class OrchestratorEngine:
    """Multi-Agent Orchestration & Debate Execution Engine."""

    def __init__(self, agent_pool: Optional[AgentPool] = None, llm_caller: Optional[LLMCaller] = None):
        self.ctx = EngineContext(
            agent_pool=agent_pool or get_agent_pool(),
            llm_caller=llm_caller or LLMCaller(),
            session_factory=get_session_factory(),
        )
        self.recorder = TurnRecorder(self.ctx)
        self.speech = SpeechRunner(self.ctx, self.recorder)
        self.conversation = ConversationMemory(self.ctx)
        services = (self.ctx, self.recorder, self.speech, self.conversation)
        self.planning_phase = PlanningPhaseExecutor(*services)
        self.debate_phase = DebatePhaseExecutor(*services)
        self.synthesis_phase = SynthesisPhaseExecutor(*services)
        self.preparer = TurnPreparer(self.ctx, graph_for_turn=self.debate_phase.graph.graph_for_turn)

    # 밖(MADOWarGame 구동기, 테스트)에서 쓰는 이름. 실제 값은 서비스들이 함께 보는 `ctx` 에 있습니다.
    @property
    def agent_pool(self) -> AgentPool:
        return self.ctx.agent_pool

    @property
    def llm_caller(self) -> LLMCaller:
        return self.ctx.llm_caller

    @llm_caller.setter
    def llm_caller(self, caller: LLMCaller) -> None:
        self.ctx.llm_caller = caller

    @property
    def session_factory(self):
        return self.ctx.session_factory

    # ------------------------------------------------------------------ 진행 중인 턴의 도구 보안

    async def set_tool_rules(self, session_id: str, grants: List[str], denials: List[str]) -> bool:
        """진행 중인 턴의 "이 대화에서" 규칙을 갈아 끼웁니다. 도는 턴이 없으면 False.

        True 면 저장까지 게이트가 했습니다. 토론 중에는 게이트가 이 목록의 유일한
        기록자여야, 카드에서 방금 더한 규칙을 화면의 옛 목록이 덮지 않습니다.
        """
        gate = self.ctx.tool_gates.get(session_id)
        if gate is None:
            return False
        await gate.replace_rules(grants, denials)
        return True

    def set_tool_mode(self, session_id: str, mode: str) -> bool:
        """진행 중인 턴의 도구 보안 모드를 바꿉니다. 도는 턴이 없으면 False.

        저장은 화면이 합니다 (세션 DB). 여기서는 이미 세워진 문지기에 알려, 수많은 승인
        카드를 보고 모드를 바꾼 사람이 다음 턴까지 기다리지 않게 합니다.
        """
        gate = self.ctx.tool_gates.get(session_id)
        if gate is None:
            return False
        gate.set_mode(mode)
        return True

    async def _session_workspace(self, session_id: str) -> Path:
        """이 대화가 쓰기로 한 작업 공간. 대화를 찾을 수 없으면 기본값."""
        async with self.session_factory() as db:
            res = await db.execute(
                select(SessionModel.workspace_dir).where(SessionModel.id == session_id)
            )
            return resolve_workspace_dir(res.scalar_one_or_none() or None)

    async def run_turn(
        self,
        session_id: str,
        user_prompt: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
        skill_designations: Optional[Dict[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
    ) -> DebateState:
        """Executes a full multi-agent collaborative debate and synthesis turn.

        `skill_designations` 는 입력창에서 `@전문가 @스킬` 로 지정한 것입니다 (전문가 키 → 스킬).
        이 턴에 한해 그 전문가의 `allowed_skills` 밖이어도 주고, 발언마다 미리 불러 둡니다.

        `attachments` 는 입력창에서 @언급한 파일입니다 (작업 공간 기준 경로). 그 형식을 받도록 설정한
        에이전트에게만 이 턴의 발언마다 내용째 실립니다 (`app/agents/media.py`).

        `control` 이 주어지면 발언과 발언 사이마다 사용자의 정지 요청과 개입
        메모를 확인합니다. 정지는 태스크를 죽이는 것이 아니라 남은 라운드를
        건너뛰고 최종 합성으로 넘어가는 것이라, 지금까지의 토론으로도 산출물이
        나옵니다.

        **이 대화의 작업 공간에 해당하는 MCP 런타임을 빌린 채로** 돕니다.
        filesystem 은 허용 경로를 argv 로, sandbox 는 `SANDBOX_WORKSPACE` 를 env 로
        기동 시점에 받으므로 폴더마다 프로세스 묶음이 따로 있어야 합니다. 같은
        폴더를 쓰는 다른 대화가 이미 빌려 놓았으면 그것을 함께 씁니다.

        반납은 턴이 **어떻게 끝나든** 일어나야 합니다 — 정상 종료, 사용자 정지,
        취소, 예외 전부. 반납을 놓치면 그 런타임은 아무도 안 쓰는 채로 종료 때까지
        남고, 그만큼 다음 대화가 쓸 자리가 줄어듭니다.

        턴은 기록으로 남습니다 (`TurnModel`, ADR-024). 예외로 멈추면 "실패" 로 적고, 취소되면
        (서버 종료·긴급 종료) 그대로 둡니다 — 서버 종료였다면 다음 기동이 "끊김" 으로 적고,
        긴급 종료였다면 부른 쪽이 턴째로 지웁니다.
        """
        turn_id = str(uuid.uuid4())
        # 이 턴의 LLM 호출이 어느 대화의 것인지 (서버 전체 동시 요청 상한의 대기열 표시).
        # 병렬 발언 태스크는 만들어질 때 이 값을 물려받습니다.
        holder_token = LLM_HOLDER.set(session_id)
        pool = get_runtime_pool()
        try:
            workspace = await self._session_workspace(session_id)
            await pool.acquire(workspace, holder=session_id)
            try:
                # 해당 턴의 모든 LLM 호출 사용량을 측정합니다 (`llm_usage`). 병렬 발언 비동기 태스크도 이 측정 컨텍스트를 상속받습니다.
                with measuring() as usage:
                    state = await self._run_turn(
                        session_id, user_prompt, workspace, turn_id, on_event, control,
                        skill_designations=skill_designations, attachments=attachments, usage=usage,
                    )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - 적고 그대로 올립니다
                await self.recorder.mark_turn_failed(turn_id, exc)
                await self._close_checkpoint(workspace, turn_id, on_event)
                raise
            else:
                await self._close_checkpoint(workspace, turn_id, on_event, state)
                return state
            finally:
                self.ctx.tool_gates.pop(session_id, None)
                await pool.release(session_id, workspace)
        finally:
            LLM_HOLDER.reset(holder_token)

    async def resume_turn(
        self,
        session_id: str,
        turn_id: str,
        mode: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
    ) -> DebateState:
        """끊긴 턴을 다시 세워 마무리하거나(`finish`) 이어 갑니다(`continue`) (ADR-024).

        * `finish` — 기록된 발언만으로 합성합니다. 정지와 같은 의미라 합의로 적지 않고, 합성
          지시와 보고서에 서버 중단으로 덜 논의되었다고 적습니다.
        * `continue` — 기록에서 어디까지 돌았는지 다시 계산해 남은 발언부터 이어 갑니다.

        참여자·전략·라운드 수·지침·작업 공간은 **그 턴이 시작될 때의 구성**(`TurnModel.config`)을
        따릅니다. 끊긴 사이에 로스터를 바꿨더라도 그 턴은 시작할 때의 모습으로 마칩니다.
        """
        if mode not in ("finish", "continue"):
            raise ValueError(f"알 수 없는 재개 방식입니다: {mode}")
        async with self.session_factory() as db:
            turn = await db.get(TurnModel, turn_id)
            if turn is None or turn.session_id != session_id:
                raise ValueError("이어 갈 턴을 찾지 못했습니다.")
            if turn.status not in TURN_UNFINISHED:
                raise ValueError(f"끊긴 턴이 아닙니다 (상태: {turn.status}).")
            workspace = resolve_workspace_dir((turn.config or {}).get("workspace_dir") or None)

        # 새 턴과 같이, 이어 가는 턴의 LLM 호출도 이 대화의 것으로 대기열에 보입니다.
        holder_token = LLM_HOLDER.set(session_id)
        pool = get_runtime_pool()
        try:
            await pool.acquire(workspace, holder=session_id)
            try:
                with measuring() as usage:
                    state = await self._resume_turn(
                        session_id, turn_id, mode, workspace, on_event, control, usage=usage,
                    )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - 적고 그대로 올립니다
                await self.recorder.mark_turn_failed(turn_id, exc)
                await self._close_checkpoint(workspace, turn_id, on_event)
                raise
            else:
                await self._close_checkpoint(workspace, turn_id, on_event, state)
                return state
            finally:
                self.ctx.tool_gates.pop(session_id, None)
                await pool.release(session_id, workspace)
        finally:
            LLM_HOLDER.reset(holder_token)

    async def _run_turn(
        self,
        session_id: str,
        user_prompt: str,
        workspace: Path,
        turn_id: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
        *,
        skill_designations: Optional[Dict[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
        usage: Optional[Usage] = None,
    ) -> DebateState:
        """`run_turn` 의 본문. 런타임은 이미 빌린 상태로 들어옵니다."""
        async with self.session_factory() as db:
            # 1. Load session config from DB
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            res = await db.execute(stmt)
            session_model = res.scalar_one_or_none()
            if not session_model:
                raise ValueError(f"Session with ID '{session_id}' not found.")

            # 그래프를 검사하고 참여자를 세웁니다. 돌 수 없는 턴이면 사용자 발언을 기록하기
            # **전에** 여기서 멈춥니다 — 답 없는 요청이 다음 턴의 맥락에 끼어들지 않게.
            setup = await self.preparer.setup(
                db, session_model, control=control, on_event=on_event,
                skill_designations=skill_designations, attachments=attachments,
            )

            # 2. Initialize Debate State
            state = new_state(session_id, user_prompt, workspace, setup)
            state.turn_id = turn_id
            if usage is not None:
                state.usage = usage

            # 이전 턴의 대화 기록을 DB에서 로드하여 대화 맥락을 보존합니다.
            state.messages.extend(await self.recorder.load_history(db, session_id))
            # 여기서부터가 이번 턴입니다. 산출물은 이 뒤의 발언에서만 모읍니다.
            state.turn_message_start = len(state.messages)
            self.conversation.load_memory(state, session_model)

            # 끊긴 채로 둔 턴이 있으면 버린 것으로 적습니다. 기록은 남기되 더는 묻지 않습니다.
            try:
                if await turns.abandon_unfinished(db, session_id):
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 적지 못해도 새 턴은 돕니다
                logger.warning(f"Could not mark earlier unfinished turns as abandoned: {exc}")
                await db.rollback()

            # 3. Record User Message in DB
            #
            # 이 요청이 기록된 시각이 이 턴의 시작입니다. 보고서와 저장 문서의 "총 경과"
            # 가 여기서 셉니다 — 기록에 남는 시각이라, 문서를 읽는 사람이 "요청 시각 →
            # 합성 종료" 로 같은 값을 다시 얻을 수 있습니다. 턴 기록은 이 요청과 같은
            # 커밋에 들어갑니다.
            turn_config = setup.config(workspace)
            opening = await self.recorder.record_user_message(
                db=db,
                state=state,
                content=user_prompt,
                round_number=0,
                on_event=on_event,
                kind=turns.KIND_OPENING,
                extra_rows=lambda msg_id, at: [TurnModel(
                    id=turn_id,
                    session_id=session_id,
                    status=TURN_RUNNING,
                    phase="planning",
                    opening_message_id=msg_id,
                    config=turn_config,
                    started_at=at,
                )],
            )
            turn_started_at = opening.started_at
            if on_event:
                await on_event({"type": EventType.TURN_OPENED, "turn_id": turn_id, "resumed": False})

            # 에이전트 작업 시작 전 워크스페이스 스냅샷을 생성합니다 (`app/workspace_checkpoint.py`).
            start = await self._snapshot(workspace, turn_id, workspace_checkpoint.START)
            if start:
                await self.recorder.set_turn(db, state, checkpoint={"start": start})

            # 4. Phase 1: Master Orchestrator Goal Analysis & Planning
            plan_message = await self.planning_phase.plan(db, state, setup, on_event=on_event, control=control)

            # 4-b. 계획 승인 (ADR-028). 사람이 계획을 확인하기 전에는 아무도 발언하지 않습니다.
            # 수정을 요청하면 계획이 다시 쓰이므로, 토론은 여기서 돌려받은 계획에서 시작합니다.
            plan_message = await self.planning_phase.approve_plan(
                db, state, setup, plan_message, on_event=on_event, control=control,
            )

            # 5. Phase 2: Multi-Round Specialist Debate Loop
            await self.recorder.set_turn(db, state, phase="debating")
            with consultation_scope(
                setup.active_agents,
                pool=setup.consult_pool,
                llm_caller=self.llm_caller,
                custom_instructions=setup.custom_instructions,
                session_id=session_id,
                mcp=self.ctx.mcp_for(state),
                tool_gate=self.ctx.gate_for(state),
                on_event=on_event,
            ):
                stopped_early = await self.debate_phase.debate(
                    db, state, setup, control=control, on_event=on_event,
                    start_value=[
                        plan_message.id
                        if plan_message is not None and plan_message.msg_type != "error"
                        else opening.id
                    ],
                )

            # 6. Phase 3: Final Consensus & Artifact Synthesis
            await self.synthesis_phase.conclude(
                db, state, setup, stopped_early=stopped_early, turn_started_at=turn_started_at,
                control=control, on_event=on_event,
            )
            return state

    async def _resume_turn(
        self,
        session_id: str,
        turn_id: str,
        mode: str,
        workspace: Path,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
        *,
        usage: Optional[Usage] = None,
    ) -> DebateState:
        """`resume_turn` 의 본문. 런타임은 이미 빌린 상태로 들어옵니다."""
        async with self.session_factory() as db:
            session_model = await db.get(SessionModel, session_id)
            turn = await db.get(TurnModel, turn_id)
            if session_model is None or turn is None:
                raise ValueError("이어 갈 턴을 찾지 못했습니다.")
            config = dict(turn.config or {})

            # 그래프는 그 턴이 시작될 때 고정해 둔 것입니다. 파일은 그 사이 바뀌었을 수 있습니다.
            # 오케스트레이터가 그린 그래프는 턴의 기록에 있어 토론에 들어갈 때 읽습니다 (`GraphDrafter`).
            graph_spec: Optional[GraphSpec] = None
            if get_strategy(resolve_strategy_name(config.get("strategy"))).uses_graph_file:
                if not session_model.graph_snapshot:
                    raise GraphTurnError("이 턴이 돌던 그래프의 기록이 없어 다시 세울 수 없습니다.")
                graph_spec = parse_graph(session_model.graph_snapshot)
            setup = await self.preparer.setup(
                db, session_model, control=control, on_event=on_event,
                config=config, graph_spec=graph_spec,
            )

            history = await self.recorder.load_history(db, session_id, tools_for_turn=turn_id)
            start = next((i for i, m in enumerate(history) if m.id == turn.opening_message_id), None)
            if start is None:
                raise ValueError("이 턴을 연 요청이 기록에 없어 다시 세울 수 없습니다.")
            opening = history[start]

            state = new_state(session_id, opening.content, workspace, setup)
            state.turn_id = turn_id
            if usage is not None:
                # 중단 전까지 누적된 사용량에 이어서 측정합니다.
                usage.add(Usage.of(turn.usage))
                state.usage = usage
            state.messages = history
            state.turn_message_start = start
            self.conversation.load_memory(state, session_model)
            restore_turn_state(state)

            # 끊긴 채로 있던 시간을 셉니다. 보고서의 총 경과에 "그중 서버 중단" 으로 함께 적습니다.
            orphans = (await db.execute(
                select(ToolCallRecordModel)
                .where(ToolCallRecordModel.turn_id == turn_id, ToolCallRecordModel.message_id.is_(None))
                .order_by(ToolCallRecordModel.created_at)
            )).scalars().all()
            stopped_at = turns.last_activity(turn, state.messages[start:], [o.created_at for o in orphans])
            paused = int((utc_now() - stopped_at).total_seconds()) if stopped_at is not None else 0
            turn.status = TURN_RUNNING
            turn.error = ""
            turn.paused_seconds = int(turn.paused_seconds or 0) + max(0, paused)
            turn.resumed_count = int(turn.resumed_count or 0) + 1
            await db.commit()
            state.paused_seconds = turn.paused_seconds
            state.resumed_count = turn.resumed_count
            if on_event:
                # 끊기기 전의 기록도 싣습니다. 러너의 스냅샷이 턴 전체를 들고 있어야, 이어 가는 중에
                # 새로고침한 화면이 그래프 진행 표시를 처음부터 다시 그립니다.
                await on_event({
                    "type": EventType.TURN_OPENED, "turn_id": turn_id, "resumed": True, "mode": mode,
                    "messages": [m.model_dump() for m in state.messages[start:]],
                })

            # 도구 단위로 이어 갈 발언의 초안 (ADR-025). 이어 가기에서만 씁니다 — 결론 내기는 발언을
            # 더 하지 않으므로, 초안이 들고 있던 도구 기록도 아래의 "끊김" 안내로 돌립니다.
            drafts = (await db.execute(
                select(SpeechDraftModel).where(SpeechDraftModel.turn_id == turn_id)
            )).scalars().all()
            usable = [
                d for d in drafts
                if mode == "continue" and (d.state or {}).get("version") == SPEECH_STATE_VERSION
            ]
            state.resume_drafts = {
                turns.draft_key(d.agent_key, d.kind, d.round_number, d.graph_node_id): {
                    "id": d.id, "kind": d.kind, "started_at": d.started_at, "state": d.state,
                }
                for d in usable
            }
            owned = {rid for d in usable for rid in (d.state or {}).get("tool_record_ids") or []}
            stale = [d.id for d in drafts if d not in usable]
            if stale:
                await db.execute(delete(SpeechDraftModel).where(SpeechDraftModel.id.in_(stale)))
                await db.commit()

            # 끝나지 못한 발언이 실행한 도구는 "끊겼다" 는 안내에 잇습니다. 그 발언의 글은 기록되지
            # 않았으므로 남아 있지 않다는 것도 함께 적습니다. 이어 가면 그 에이전트가 다시 말할 때
            # 이 기록을 관찰로 건넵니다. 초안이 있는 발언은 초안에서 이어 가므로 빼고, 그 도구 기록은
            # 이어 간 발언에 그대로 이어집니다.
            redo = await self.recorder.note_interrupted_speeches(
                db, state, setup, [o for o in orphans if o.id not in owned], on_event=on_event,
            )

            if mode == "finish":
                # 정지와 같은 의미입니다. 기록된 발언만으로 합성하고, 합의로 적지 않습니다.
                state.interrupted = True
                await self.recorder.set_turn(db, state, phase="debating")
                await self.synthesis_phase.conclude(
                    db, state, setup, stopped_early=True, turn_started_at=opening.started_at,
                    control=control, on_event=on_event,
                )
                return state

            if not turns.can_continue(setup.strategy_name, turn.phase):
                raise ValueError("이 전략의 턴은 아직 이어서 진행할 수 없습니다. 지금까지로 결론을 내거나 버려 주십시오.")

            if turn.phase == "synthesizing":
                # 토론은 끝났고 합성만 남았습니다. 정지로 덜 논의된 턴이었는지는 턴 기록이 압니다.
                stopped_early = bool(turn.stopped_early)
            else:
                # 계획이 기록되기 전에 끊겼으면 계획부터 다시 합니다.
                plan_expected = setup.graph_spec is None or setup.graph_spec.start.plan
                if plan_expected and not turns.count_kind(state.messages[start:], turns.KIND_PLAN):
                    await self.planning_phase.plan(db, state, setup, on_event=on_event, control=control)
                # 승인을 받기 전에 끊긴 턴은 (답이 없어 멈춰 둔 턴도) 승인부터 다시 받습니다
                # (ADR-028). 토론에 들어간 뒤에 끊긴 턴은 이미 승인됐거나 승인 없이 시작한 것입니다.
                if (
                    turn.phase in ("planning", "approval")
                    and state.plan_index is not None and state.approval_index is None
                ):
                    await self.planning_phase.approve_plan(
                        db, state, setup, state.messages[state.plan_index],
                        on_event=on_event, control=control,
                    )
                await self.recorder.set_turn(db, state, phase="debating")
                # 끊기기 전의 발언까지 장부에 접어 두고 이어 갑니다 (라운드 중간에 끊겼으면 장부가
                # 그 라운드를 모릅니다). 새 발언이 없으면 부르지 않습니다.
                await self.conversation.update_ledger(
                    state=state, orchestrator=setup.orchestrator, on_event=on_event, reason="재개",
                )
                with consultation_scope(
                    setup.active_agents,
                    pool=setup.consult_pool,
                    llm_caller=self.llm_caller,
                    custom_instructions=setup.custom_instructions,
                    session_id=session_id,
                    mcp=self.ctx.mcp_for(state),
                    tool_gate=self.ctx.gate_for(state),
                    on_event=on_event,
                ):
                    stopped_early = await self.debate_phase.debate(
                        db, state, setup, control=control, on_event=on_event,
                        start_value=[
                            state.messages[state.plan_index].id
                            if state.plan_index is not None else opening.id
                        ],
                        resume=True, redo=redo,
                    )
                # 정지 등으로 다시 불리지 않은 발언의 초안은, 그 도구 기록을 "끊김" 안내로 돌립니다.
                # 합성의 초안은 바로 아래 합성이 씁니다.
                await self.recorder.settle_drafts(db, state, setup, on_event=on_event, keep=(turns.KIND_SYNTHESIS,))
            await self.synthesis_phase.conclude(
                db, state, setup, stopped_early=stopped_early, turn_started_at=opening.started_at,
                control=control, on_event=on_event,
            )
            return state


    # ------------------------------------------------------------ 작업 공간 체크포인트

    @staticmethod
    async def _snapshot(workspace: Path, turn_id: str, which: str) -> Optional[str]:
        """현재 워크스페이스 상태를 해당 턴의 체크포인트 `which`(start/end)로 기록합니다 (실패해도 턴 진행은 유지)."""
        try:
            return await asyncio.to_thread(
                workspace_checkpoint.snapshot, workspace, workspace_checkpoint.turn_ref(turn_id, which),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 체크포인트는 턴의 부산물입니다
            logger.warning(f"Could not checkpoint the workspace {workspace} ({which} of turn {turn_id}): {exc}")
            return None

    async def _close_checkpoint(
        self,
        workspace: Path,
        turn_id: str,
        on_event: Optional[EventCallback],
        state: Optional[DebateState] = None,
    ) -> None:
        """턴 종료 시점의 워크스페이스 스냅샷을 기록하고, 시작 시점과 비교한 변경 파일 수를 턴 레코드에 저장한 뒤 이벤트를 발행합니다.

        실패한 턴의 경우에도 실패 전까지 수정된 파일을 복원할 수 있도록 체크포인트를 기록합니다. 시작 스냅샷이
        없으면(git 미지원 또는 초기 중단) 기록을 생략합니다.
        """
        try:
            start = await asyncio.to_thread(
                workspace_checkpoint.resolve, workspace,
                workspace_checkpoint.turn_ref(turn_id, workspace_checkpoint.START),
            )
            if not start:
                return
            end = await self._snapshot(workspace, turn_id, workspace_checkpoint.END)
            if not end:
                return
            changed = await asyncio.to_thread(workspace_checkpoint.changes, workspace, start, end)
            count = len(changed) if changed is not None else None
            async with self.session_factory() as db:
                await db.execute(
                    update(TurnModel).where(TurnModel.id == turn_id)
                    .values(checkpoint={"start": start, "end": end, "changed": count})
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 체크포인트는 턴의 부산물입니다
            logger.warning(f"Could not close the workspace checkpoint of turn {turn_id}: {exc}")
            return
        if on_event:
            synthesis: Any = None
            if state is not None:
                synthesis = next(
                    (m for m in reversed(state.messages) if turns.kind_of(m) == turns.KIND_SYNTHESIS), None
                )
            await on_event({
                "type": EventType.WORKSPACE_CHECKPOINTED,
                "turn_id": turn_id,
                "changed": count,
                "message_id": synthesis.id if synthesis is not None else None,
            })


_orchestrator_engine: Optional[OrchestratorEngine] = None


def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
