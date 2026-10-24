"""한 에이전트의 발언: 스트리밍, 도구 기록, 한도 쪽지, 확정 기록.

LLM 이 응답하지 못하거나 발언이 오류로 끊기면, 대신 지어내지 않고 그 사실을 적은 실패 발언을
남긴 뒤 토론을 이어 갑니다.
"""

import asyncio
import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from sqlalchemy import delete, update

from app.agents import llm_fallback
from app.agents.base import Agent
from app.agents.llm_types import LLMUnavailableError
from app.agents.skills import with_designated_skills
from app.config import TOOL_ITERATION_CEILING
from app.database.models import MessageModel, SpeechDraftModel, ToolCallRecordModel, utc_now
from app.events import EventCallback, EventType
from app.orchestration import turns
from app.orchestration.context import EngineContext
from app.orchestration.control import TurnControl
from app.orchestration.recording import TurnRecorder
from app.orchestration.state import DebateMessage, DebateState

logger = logging.getLogger(__name__)


# 스트리밍 조각 이벤트를 모아 보내는 간격(초).
STREAM_EVENT_INTERVAL = 0.1


class _ChunkStream:
    """흘러나오는 조각을 모았다가 `STREAM_EVENT_INTERVAL` 마다 한 통으로 보냅니다.

    "다음 조각이 올 때 간격이 지났으면 보낸다" 로 하면 안 됩니다. 모델이 한 줄
    쓰고 30초 걸리는 도구를 부르면, 그 한 줄은 다음 조각이 올 때까지 30초 동안
    화면에 나오지 않습니다. 그래서 첫 조각이 쌓이는 순간 짧은 예약을 걸어, 뒤에
    무엇이 오든 오지 않든 그 간격 안에 내보냅니다.
    """

    def __init__(self, msg_id: str, on_event: Optional[EventCallback], prior_text: str) -> None:
        self.msg_id = msg_id
        self.on_event = on_event
        # 화면에 흘러간 글 전부 (끊기기 전에 흘렀던 글 포함). 확정본이 비었거나 발언이 실패하면 이것을 남깁니다.
        self.streamed: List[str] = [f"{prior_text}\n\n"] if prior_text else []
        self._pending: List[str] = []
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def text(self) -> str:
        return "".join(self.streamed)

    async def on_chunk(self, delta: str) -> None:
        self.streamed.append(delta)
        if not self.on_event:
            return
        self._pending.append(delta)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_later())

    async def _flush(self) -> None:
        # 순서를 지키려고 한 번에 하나만 내보냅니다. 모은 것을 꺼내는 데에는
        # await 가 없어, 꺼낸 뒤에 들어온 조각은 다음 통에 실립니다.
        async with self._lock:
            if not self._pending or not self.on_event:
                return
            delta = "".join(self._pending)
            self._pending.clear()
            await self.on_event({
                "type": EventType.MESSAGE_STREAM_CHUNK,
                "message_id": self.msg_id,
                "delta": delta,
            })

    async def _flush_later(self) -> None:
        await asyncio.sleep(STREAM_EVENT_INTERVAL)
        await self._flush()

    def cancel(self) -> None:
        # 기다리지 않고 취소만 합니다. 예약은 sleep 에서 멈춰 있거나 아직 시작하지
        # 않았으므로, 취소하면 내보내기 전에 끝납니다.
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def drain(self) -> None:
        """남은 조각을 지금 내보냅니다. 발언을 확정하기 전에 반드시 부릅니다.

        예약을 남겨 두면 `message_added` 로 발언이 확정된 **뒤에** 조각이 한 통 더
        도착하고, 러너의 스냅샷은 그것을 확정본 끝에 한 번 더 붙입니다.
        """
        self.cancel()
        await self._flush()


class _Speech:
    """발언 하나가 진행되는 동안의 상태와, 도구 루프에 넘기는 콜백들 (`SpeechRunner.speak`)."""

    def __init__(
        self,
        recorder: TurnRecorder,
        *,
        db,
        db_lock: Optional[asyncio.Lock],
        state: DebateState,
        agent: Agent,
        kind: str,
        round_number: int,
        msg_type: str,
        graph_node_id: Optional[str],
        graph_port: Optional[str],
        on_event: Optional[EventCallback],
        draft: Optional[Dict[str, Any]],
    ) -> None:
        self.recorder = recorder
        self.db, self.db_lock, self.state, self.agent = db, db_lock, state, agent
        self.kind, self.round_number, self.msg_type = kind, round_number, msg_type
        self.graph_node_id, self.graph_port = graph_node_id, graph_port
        self.on_event = on_event
        self.draft_state: Optional[Dict[str, Any]] = draft["state"] if draft else None
        self.id = draft["id"] if draft else str(uuid.uuid4())
        # 벽시계 시작 시각. `created_at` 과 따로 둡니다 — 그쪽은 발언이 끝난 뒤에
        # 들어가는 정렬 키라서, 병렬 라운드에서는 실제 시각이 아닙니다.
        self.started_at = (draft.get("started_at") if draft else None) or utc_now()
        # 끊기기 전에 이미 화면에 흘렀던 글과, 이미 기록된 도구 호출.
        self.prior_text = "\n\n".join((self.draft_state or {}).get("segments") or [])
        self.prior_record_ids: List[str] = list((self.draft_state or {}).get("tool_record_ids") or [])
        self.stream = _ChunkStream(self.id, on_event, self.prior_text)

        # 이 발언이 실행한 도구. 실행하는 **즉시** 발언 id 를 비운 채 기록하고
        # (`TurnRecorder.record_tool_call`), 발언이 기록될 때 같은 커밋에서 이 발언에 잇습니다.
        # 예전에는 모아 두었다가 발언 행과 함께 넣었는데, 발언이 끝나기 전에 서버가
        # 내려가면 이미 실행된 도구(승인받은 파일 쓰기 포함)의 기록이 통째로 사라졌습니다
        # (ADR-024). 발언 id 를 비워 두는 것은, 아직 없는 발언을 가리키는 행을 만들지
        # 않기 위해서입니다 (SQLite 가 외래키를 검사하지 않아 통과할 뿐입니다).
        self.executed_tools: List[Dict[str, Any]] = []
        # 즉시 기록에 성공한 호출 (`id(call_log)` -> 기록 id). 실패한 것은 예전처럼 발언과 함께 넣습니다.
        self.recorded_tools: Dict[int, str] = {}
        # 이 발언에서 컨텍스트 한도로 생략된 수. 예전에는 logger 에만 남아,
        # 토론 기록이 사라지는 것을 보고 있는 사람이 알 방법이 없었습니다.
        self.trimmed: List[int] = []
        # 이 에이전트의 연결이 실패해 전역 기본 모델이 대신 답하게 됐다는 알림 (`llm.fallback`, ADR-030).
        self.fallback_notices: List[Any] = []
        # 도구 루프가 한 판을 마칠 때마다 이어 갈 수 있는 상태를 초안으로 남깁니다 (ADR-025).
        # 도구를 부르지 않는 발언은 한 번도 불리지 않으므로 초안이 생기지 않습니다.
        self.draft_saved = draft is not None

    def start_event(self) -> Dict[str, Any]:
        return {
            "type": EventType.MESSAGE_STREAM_START,
            "message": {
                "id": self.id,
                "sender_key": self.agent.key,
                "sender_name": self.agent.name,
                "sender_role": self.agent.role,
                "content": f"{self.prior_text}\n\n" if self.prior_text else "",
                "round_number": self.round_number,
                "msg_type": self.msg_type,
                "started_at": self.started_at,
                "graph_node_id": self.graph_node_id,
                "graph_port": self.graph_port,
            },
        }

    # ------------------------------------------------------------ 도구 루프에 넘기는 콜백

    async def on_tool_call(self, call_log: Dict[str, Any]) -> None:
        self.executed_tools.append(call_log)
        record_id = await self.recorder.record_tool_call(self.db, self.state, self.agent, call_log, self.db_lock)
        if record_id:
            self.recorded_tools[id(call_log)] = record_id
        if self.on_event:
            await self.on_event({
                "type": EventType.TOOL_EXECUTED,
                "agent_key": self.agent.key,
                "agent_name": self.agent.name,
                "tool_call": call_log,
            })

    def on_context_trim(self, dropped: int) -> None:
        self.state.context_dropped += dropped
        self.trimmed.append(dropped)

    async def on_fallback(self, notice: Any) -> None:
        # 기본 모델을 부르기 전에 오므로, 흘러나오는 카드에 "누가 답하고 있는지" 를 먼저 붙입니다.
        self.fallback_notices.append(notice)
        if self.on_event:
            await self.on_event({
                "type": EventType.LLM_FALLBACK,
                "message_id": self.id,
                "agent_key": self.agent.key,
                "agent_name": self.agent.name,
                "llm_meta": notice.as_meta(),
            })

    async def checkpoint(self, loop_state: Dict[str, Any]) -> None:
        payload = json.loads(json.dumps(
            {**loop_state, "tool_record_ids": self.prior_record_ids + list(self.recorded_tools.values())},
            ensure_ascii=False, default=str,
        ))
        db = self.db
        async with (self.db_lock or nullcontext()):
            try:
                if self.draft_saved:
                    await db.execute(
                        update(SpeechDraftModel)
                        .where(SpeechDraftModel.id == self.id)
                        .values(state=payload, updated_at=utc_now())
                    )
                else:
                    db.add(SpeechDraftModel(
                        id=self.id, session_id=self.state.session_id, turn_id=self.state.turn_id,
                        agent_key=self.agent.key, kind=self.kind, round_number=self.round_number,
                        graph_node_id=self.graph_node_id, started_at=self.started_at, state=payload,
                    ))
                await db.commit()
                self.draft_saved = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 초안을 못 남겨도 발언은 계속됩니다
                logger.warning(f"Could not save the draft of {self.agent.name}'s speech: {exc}")
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug("Rollback after a failed draft save also failed", exc_info=True)

    # ------------------------------------------------------------ 기록

    def mark_failed(self) -> None:
        if self.agent.key not in self.state.failed_agent_keys:
            self.state.failed_agent_keys.append(self.agent.key)

    def fallback_meta(self, final_type: str) -> Optional[Dict[str, Any]]:
        """기본 모델이 대신 답한 발언이면 그 사실을 기록에 남깁니다.

        기본 모델까지 실패한 발언에는 남기지 않습니다 — 답한 모델이 없고, 두 이유는 실패 안내 본문에
        적혀 있습니다.
        """
        if not self.fallback_notices or final_type == "error":
            return None
        if self.agent.key not in self.state.fallback_agent_keys:
            self.state.fallback_agent_keys.append(self.agent.key)
        return self.fallback_notices[-1].as_meta()

    def message_rows(
        self,
        *,
        content: str,
        final_type: str,
        finished_at: datetime,
        created_at: Optional[datetime],
        turn_started_at: Optional[datetime],
        llm_meta: Optional[Dict[str, Any]],
    ) -> List[Any]:
        """발언 행과, 즉시 기록하지 못한 도구 행. 기록을 다시 시도할 때마다 새로 만듭니다."""
        rows: List[Any] = [MessageModel(
            id=self.id,
            session_id=self.state.session_id,
            sender_key=self.agent.key,
            sender_name=self.agent.name,
            sender_role=self.agent.role,
            content=content,
            round_number=self.round_number,
            msg_type=final_type,
            started_at=self.started_at,
            finished_at=finished_at,
            turn_started_at=turn_started_at,
            graph_node_id=self.graph_node_id,
            graph_port=self.graph_port,
            turn_id=self.state.turn_id,
            turn_meta=turns.meta(self.kind),
            llm_meta=llm_meta,
            **({"created_at": created_at} if created_at is not None else {}),
        )]
        for call_log in self.executed_tools:
            if id(call_log) not in self.recorded_tools:
                rows.append(self.recorder.tool_row(self.state, self.agent, call_log, message_id=self.id))
        return rows

    def link_tools(self) -> Callable[[], Coroutine[Any, Any, None]]:
        """발언 행과 같은 커밋에서 할 일: 먼저 기록해 둔 도구를 이 발언에 잇고, 초안을 지웁니다."""
        linked = self.prior_record_ids + list(self.recorded_tools.values())

        async def run() -> None:
            if linked:
                await self.db.execute(
                    update(ToolCallRecordModel)
                    .where(ToolCallRecordModel.id.in_(linked))
                    .values(message_id=self.id)
                )
            # 발언이 기록되면 초안은 할 일을 마쳤습니다. 같은 커밋에서 지웁니다.
            if self.draft_saved:
                await self.db.execute(delete(SpeechDraftModel).where(SpeechDraftModel.id == self.id))

        return run


class SpeechRunner:
    """한 에이전트의 발언을 스트리밍하고, 도구 호출을 기록하고, 확정본을 기록합니다.

    한도(도구 호출 상한·컨텍스트 창)에 닿으면 사람에게 묻는 통로도 여기서 만듭니다.
    """

    def __init__(self, ctx: EngineContext, recorder: TurnRecorder) -> None:
        self.ctx = ctx
        self.recorder = recorder

    @staticmethod
    def unavailable_notice(agent: Agent, exc: LLMUnavailableError, streamed: str) -> str:
        """응답을 받지 못했을 때 화면과 기록에 남는 문구.

        여기서 그럴듯한 대체 발언을 만들어 내면 다음 에이전트의 입력과 최종 합성
        보고서까지 그 거짓말 위에 쌓입니다. 못 받았다고 적는 편이 낫습니다.
        """
        notice = (
            f"> ⚠️ **연결 끊김 — {agent.name} 의 발언을 받지 못했습니다.**\n"
            f">\n"
            f"> - 모델: `{agent.model}`\n"
            f"> - 엔드포인트: `{exc.endpoint}`\n"
            f"> - 원인: `{exc.reason}`\n"
            f">\n"
            f"> 이 자리에 들어갈 내용을 임의로 생성하지 않았습니다. "
            f"엔드포인트를 복구하신 후 같은 요청을 다시 전달해 주십시오."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 연결이 끊기기 전까지 수신된 내용입니다.)"
            )
        return notice

    @staticmethod
    def crashed_notice(agent: Agent, exc: BaseException, streamed: str) -> str:
        """발언이 예기치 못한 오류로 끊겼을 때 화면과 기록에 남는 문구.

        `LLMUnavailableError` 는 "엔드포인트에 닿지 못했다" 는 알려진 실패이고,
        이쪽은 우리가 예상하지 못한 실패입니다 — 도구 서버가 이상한 것을 돌려
        주었거나, 프로바이더 응답 모양이 달랐거나, 우리 코드가 틀렸거나.
        어느 쪽이든 **한 에이전트의 사고가 토론 전체를 끝낼 이유는 없습니다.**
        나머지 에이전트는 아직 말할 수 있고, 지금까지의 발언으로 합성도 됩니다.
        """
        notice = (
            f"> ⚠️ **발언 중단 — {agent.name} 의 발언이 오류로 끊겼습니다.**\n"
            f">\n"
            f"> - 모델: `{agent.model}`\n"
            f"> - 오류: `{type(exc).__name__}: {exc}`\n"
            f">\n"
            f"> 이 자리에 들어갈 내용을 임의로 생성하지 않았습니다. "
            f"토론은 나머지 에이전트로 계속 진행됩니다 (자세한 원인은 서버 로그를 확인해 주십시오)."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 오류가 발생하기 전까지 수신된 내용입니다.)"
            )
        return notice

    def make_budget_arbiter(
        self,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, int]]]:
        """도구 호출 상한에 닿았을 때 사람에게 물어보는 통로를 만듭니다.

        `control` 이 없으면 (배치 실행, 테스트) None 을 돌려줍니다. 그러면
        LLM 쪽은 아무도 묻지 않고 곧장 "도구 없이 마무리" 로 갑니다 — 물어볼
        사람이 없는데 3분을 기다릴 이유가 없습니다.

        상한은 `TOOL_ITERATION_CEILING` 을 넘길 수 없습니다. 확장은 폭주를
        늦추는 것이지 푸는 것이 아닙니다.
        """
        if control is None:
            return None

        async def arbiter(info: Dict[str, Any]) -> int:
            limit = int(info.get("limit", 0))
            headroom = max(0, TOOL_ITERATION_CEILING - limit)

            async def _open(request) -> None:
                if on_event:
                    await on_event({"type": EventType.TOOL_BUDGET_EXHAUSTED, **request.describe()})

            request = await control.ask_tool_budget(
                agent_key=str(info.get("agent_key", "")),
                agent_name=str(info.get("agent_name", "")),
                limit=limit,
                used=int(info.get("used", 0)),
                tool_calls=int(info.get("tool_calls", 0)),
                max_extension=headroom,
                on_open=_open,
            )
            if on_event:
                await on_event({
                    "type": EventType.TOOL_BUDGET_RESOLVED,
                    "id": request.id,
                    "agent_name": request.agent_name,
                    "outcome": request.outcome,
                    "granted": request.granted,
                    "limit": limit + request.granted,
                })
            return request.granted

        return arbiter

    def make_context_arbiter(
        self,
        state: DebateState,
        agent: Agent,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]]:
        """컨텍스트 창이 넘쳤을 때 사람에게 물어보는 통로를 만듭니다.

        도구 예산 쪽과 두 가지가 다릅니다.

        1. **턴에 한 번만 묻습니다.** 컨텍스트는 라운드가 쌓이면 거의 모든 발언이
           같은 벽에 부딪힙니다. 발언마다 물으면 토론을 진행할 수 없으므로, 첫
           답(`state.context_grant`)을 그 턴의 나머지에 그대로 씁니다.
        2. **모델의 실제 한도를 넘겨 늘리지 않습니다.** `context_headroom()` 이
           조회한 여유가 상한입니다. 넘겨 올리면 깔끔한 트림이 400 으로 바뀝니다.
        """
        if control is None:
            return None

        async def arbiter(info: Dict[str, Any]) -> Dict[str, Any]:
            # 이미 이번 턴에 답을 받았으면 다시 묻지 않습니다.
            if state.context_grant is not None:
                return {"granted": state.context_grant, "wrap_up": False}

            async def _open(request) -> None:
                if on_event:
                    await on_event({"type": EventType.CONTEXT_WINDOW_EXHAUSTED, **request.describe()})

            request = await control.ask_context_window(
                agent_key=str(info.get("agent_key", "")),
                agent_name=str(info.get("agent_name", "")),
                window=int(info.get("window", 0)),
                used=int(info.get("used", 0)),
                headroom=info.get("headroom"),
                tool_calls=int(info.get("tool_calls", 0)),
                on_open=_open,
            )
            state.context_grant = request.granted
            if on_event:
                await on_event({
                    "type": EventType.CONTEXT_WINDOW_RESOLVED,
                    "id": request.id,
                    "agent_name": request.agent_name,
                    "outcome": request.outcome,
                    "granted": request.granted,
                    "window": int(info.get("window", 0)) + request.granted,
                })
            # 시간이 지나 답을 못 받았으면 접지 않고 생략하며 진행합니다. 사람이
            # 자리에 없다고 해서 토론을 접을 이유는 없습니다.
            return {
                "granted": request.granted,
                "wrap_up": request.outcome == "wrap_up",
            }

        return arbiter

    async def speak(
        self,
        *,
        db,
        state: DebateState,
        agent: Agent,
        prompt_messages: List[Dict[str, Any]],
        custom_instructions: str,
        round_number: int,
        msg_type: str,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl] = None,
        db_lock: Optional[asyncio.Lock] = None,
        created_at: Optional[datetime] = None,
        post_process: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
        turn_started_at: Optional[datetime] = None,
        graph_node_id: Optional[str] = None,
        graph_port: Optional[str] = None,
        kind: str = turns.KIND_SPEECH,
    ) -> DebateMessage:
        """한 에이전트의 발언을 스트리밍하고, DB 에 기록하고, 상태에 반영합니다.

        `kind` 는 이 발언이 턴의 흐름에서 맡은 자리입니다 (`turns.KIND_*`). 끊긴 턴을 다시
        세울 때 기록을 이것으로 읽습니다.

        `turn_started_at` 은 이 발언이 턴을 마무리하는 합성 발언일 때만 줍니다.
        그 턴의 총 경과 시간을 기록에서 다시 계산할 수 있게 함께 적습니다.

        `db_lock` 은 이 발언이 다른 발언과 **동시에** 진행될 때만 필요합니다
        (병렬 지시 전략). `db` 는 세션 하나를 공유하는데 SQLAlchemy AsyncSession 은
        동시 사용을 허용하지 않습니다 — 두 발언이 같은 순간에 커밋하면
        `IllegalStateChangeError` 로 토론이 통째로 죽습니다. 락은 LLM 호출이 아니라
        기록 구간에만 걸리므로 병렬성은 그대로입니다.

        `created_at` 을 주면 그 시각으로 기록합니다. 병렬 라운드에서는 발언이 끝나는
        순서가 제각각이라, 커밋 시각을 그대로 쓰면 새로고침한 화면의 발언 순서가
        매번 달라집니다 (기록은 `created_at` 으로 정렬해 다시 읽힙니다). 지시받은
        순서를 시각에 박아 두면 실시간 화면과 다시 연 화면이 같은 순서를 보여줍니다.

        LLM 이 응답하지 못하면 실패 사실을 그대로 적은 `msg_type="error"` 발언을
        남기고 토론을 계속합니다 (다른 에이전트는 아직 살아 있을 수 있습니다).

        이 발언 중에 실행된 MCP 도구도 여기서 함께 기록합니다. 어느 발언이 부른
        도구인지는 이 자리에서만 알 수 있습니다 — 밖에서 기록하던 예전 방식은
        `message_id` 를 비워 둘 수밖에 없었고, 그래서 새로고침한 화면과 저장
        파일에서 도구 기록이 발언과 따로 놀았습니다.

        순서: 시작 알림 → LLM 에서 발언 받기(`_receive_speech`) → 남은 조각 내보내기 → 기록
        (`_record_speech`) → 생략 알림 → 상태에 반영하고 확정 알림(`_publish_speech`).
        """
        # 끊긴 턴을 이어 가는 중이고 이 발언의 초안이 남아 있으면, 처음부터가 아니라 초안의 마지막
        # 판 다음부터 이어 갑니다 (ADR-025). 발언 id 와 시작 시각은 초안의 것을 그대로 씁니다 —
        # 끊기기 전에 실행된 도구 기록이 이 발언에 이어지고, 카드는 처음 시작한 시각부터 셉니다.
        draft = state.resume_drafts.pop(
            turns.draft_key(agent.key, kind, round_number, graph_node_id), None
        )
        # 유저가 이 전문가에게 지정한 스킬 (입력창의 `@전문가 @스킬`). 이 발언에서만 `allowed_skills`
        # 에 더하고, 첫 판 전에 불러 둡니다. 대화에 고정된 에이전트 설정은 그대로입니다.
        designated = list(state.skill_designations.get(agent.key) or [])
        agent = with_designated_skills(agent, designated)
        speech = _Speech(
            self.recorder, db=db, db_lock=db_lock, state=state, agent=agent, kind=kind,
            round_number=round_number, msg_type=msg_type, graph_node_id=graph_node_id,
            graph_port=graph_port, on_event=on_event, draft=draft,
        )
        if on_event:
            await on_event(speech.start_event())

        content, tool_logs, final_type = await self._receive_speech(
            speech, prompt_messages, custom_instructions, designated, control, post_process,
        )
        # 실패로 끝났어도 예약된 조각이 확정본 뒤에 도착하면 안 됩니다.
        await speech.stream.drain()

        llm_meta = speech.fallback_meta(final_type)
        # 응답이 끝난 시각. 실패로 끝난 발언도 여기서 잽니다 — 언제 포기했는지는
        # 그 자체로 쓸모 있는 기록입니다 (엔드포인트가 몇 초 만에 끊었는지, 한도까지
        # 버텼는지). 기록 락을 기다린 시간이 섞이지 않도록 락 **밖에서** 잽니다.
        finished_at = utc_now()
        await self._record_speech(
            speech, content=content, final_type=final_type, finished_at=finished_at,
            created_at=created_at, turn_started_at=turn_started_at, llm_meta=llm_meta,
        )
        if speech.trimmed and on_event:
            await on_event({
                "type": EventType.CONTEXT_TRIMMED,
                "agent_key": agent.key,
                "agent_name": agent.name,
                "dropped": sum(speech.trimmed),
                "total_dropped": state.context_dropped,
                "where": "speech",
            })
        return await self._publish_speech(
            speech, content=content, final_type=final_type, tool_logs=tool_logs,
            finished_at=finished_at, turn_started_at=turn_started_at, llm_meta=llm_meta,
        )

    async def _receive_speech(
        self,
        speech: _Speech,
        prompt_messages: List[Dict[str, Any]],
        custom_instructions: str,
        designated: List[str],
        control: Optional[TurnControl],
        post_process: Optional[Callable[[str], Coroutine[Any, Any, str]]],
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        """LLM 에서 발언을 받습니다. `(본문, 도구 기록, msg_type)`.

        응답을 받지 못했거나 발언이 오류로 끊기면 실패 안내를 본문으로 돌려줍니다 (`"error"`).
        취소만은 그대로 올립니다.
        """
        state, agent, on_event = speech.state, speech.agent, speech.on_event
        try:
            with llm_fallback.listening(speech.on_fallback):
                content, tool_logs = await self.ctx.llm_caller.call_agent(
                    agent, prompt_messages, custom_instructions,
                    on_tool_call=speech.on_tool_call, on_chunk=speech.stream.on_chunk,
                    session_id=state.session_id,
                    budget_arbiter=self.make_budget_arbiter(control, on_event),
                    context_arbiter=self.make_context_arbiter(state, agent, control, on_event),
                    on_context_trim=speech.on_context_trim,
                    mcp=self.ctx.mcp_for(state), tool_gate=self.ctx.gate_for(state),
                    ledger=state.decision_ledger,
                    checkpoint=speech.checkpoint if state.turn_id else None,
                    resume_state=speech.draft_state,
                    preload_skills=designated,
                )
            # 다이어그램 교정 같은 후처리는 시간이 걸립니다. 그동안 카드가 마지막 조각
            # 직전에서 멈춰 보이지 않게, 흘러온 글을 먼저 다 내보냅니다.
            await speech.stream.drain()
            # 확정본이 비었는데 화면에는 글이 흘러갔다면 그 글을 남깁니다.
            # 여기서 정하는 `content` 가 DB 에 들어가는 값이라, 비워 둔 채로
            # 넘어가면 사람이 방금 읽던 발언이 새로고침 뒤에도 돌아오지 않습니다
            # (프로바이더가 텍스트를 reasoning 쪽으로만 주거나, 스트림 재조립이
            # 본문을 놓친 경우에 실제로 그렇게 됩니다).
            if not content.strip():
                content = speech.stream.text
            final_type = speech.msg_type
            if post_process is not None:
                # 기록과 화면에 남을 본문을 확정하기 **전에** 손봅니다. 스트리밍
                # 카드는 `message_added` 가 올 때 최종본으로 덮이므로, 여기서
                # 고치면 사람이 보는 것도 고쳐진 쪽입니다.
                try:
                    content = await post_process(content)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - 후처리 실패로 발언을 잃지 않습니다
                    logger.error(
                        f"Post-processing {agent.name}'s message failed: "
                        f"{type(exc).__name__}: {exc}",
                        exc_info=True,
                    )
        except LLMUnavailableError as exc:
            logger.warning(f"Agent '{agent.key}' produced no response: {exc}")
            content = self.unavailable_notice(agent, exc, speech.stream.text)
            tool_logs = []
            final_type = "error"
            speech.mark_failed()
        except asyncio.CancelledError:
            # 사용자가 토론을 끊었거나 서버가 내려가는 중입니다. 실패로 적지 않고
            # 그대로 올려 보냅니다 — 여기서 삼키면 취소가 먹지 않습니다.
            speech.stream.cancel()
            raise
        except BaseException as exc:  # noqa: BLE001 - 한 발언의 사고로 토론을 끝내지 않습니다
            logger.error(
                f"Agent '{agent.key}' crashed while speaking: {type(exc).__name__}: {exc}",
                exc_info=True,
            )
            content = self.crashed_notice(agent, exc, speech.stream.text)
            tool_logs = []
            final_type = "error"
            speech.mark_failed()
        return content, tool_logs, final_type

    async def _record_speech(
        self,
        speech: _Speech,
        *,
        content: str,
        final_type: str,
        finished_at: datetime,
        created_at: Optional[datetime],
        turn_started_at: Optional[datetime],
        llm_meta: Optional[Dict[str, Any]],
    ) -> None:
        """발언 행을 기록하고, 같은 커밋에서 먼저 기록해 둔 도구를 잇고 초안을 지웁니다."""
        agent = speech.agent
        link_tools = speech.link_tools()
        # 최종 합성이면 파일 이름과 알림에서 그렇다고 밝힙니다. 사람이 먼저 찾는 것이
        # 그 보고서입니다. `turn_started_at` 은 합성 발언에만 주어집니다.
        is_synthesis = turn_started_at is not None
        async with (speech.db_lock or nullcontext()):
            await self.recorder.persist(
                speech.db,
                lambda: speech.message_rows(
                    content=content, final_type=final_type, finished_at=finished_at,
                    created_at=created_at, turn_started_at=turn_started_at, llm_meta=llm_meta,
                ),
                what=("the final synthesis report" if is_synthesis else f"{agent.name}'s message"),
                label=("최종 합성 보고서" if is_synthesis else f"{agent.name} 의 발언"),
                kind=("synthesis" if is_synthesis else f"message-{agent.key}"),
                session_id=speech.state.session_id,
                fallback_title=f"{agent.name} ({agent.role}) — Round {speech.round_number}",
                fallback_body=content,
                on_event=speech.on_event,
                before_commit=link_tools,
            )

    @staticmethod
    async def _publish_speech(
        speech: _Speech,
        *,
        content: str,
        final_type: str,
        tool_logs: List[Dict[str, Any]],
        finished_at: datetime,
        turn_started_at: Optional[datetime],
        llm_meta: Optional[Dict[str, Any]],
    ) -> DebateMessage:
        """확정된 발언을 토론 상태에 더하고 화면에 알립니다."""
        agent = speech.agent
        message = DebateMessage(
            id=speech.id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=speech.round_number,
            msg_type=final_type,
            # 발언이 실패로 끝나면 `call_agent` 는 도구 기록을 돌려주지 못합니다.
            # 그전에 실제로 실행된 것은 남아 있어야 합니다.
            tool_calls=tool_logs or speech.executed_tools,
            started_at=speech.started_at,
            finished_at=finished_at,
            turn_started_at=turn_started_at,
            graph_node_id=speech.graph_node_id,
            graph_port=speech.graph_port,
            turn_meta=turns.meta(speech.kind),
            llm_meta=llm_meta,
        )
        speech.state.messages.append(message)

        if speech.on_event:
            await speech.on_event({"type": EventType.MESSAGE_ADDED, "message": message.model_dump()})
        return message
