"""턴의 기록: 발언·유저 발언·기록용 안내·도구 호출·턴 상태를 DB 에 남깁니다.

기록 실패는 다시 시도하고, 끝내 실패하면 `data/unsaved/` 에 마크다운으로 남긴 뒤 화면에 알립니다
(`persist_failed`). 기록 때문에 토론이 멈추지는 않습니다.
"""

import asyncio
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import delete, select, update

from app.agents.base import Agent
from app.config import DATA_DIR
from app.database.models import (
    TURN_FAILED,
    TURN_RUNNING,
    MessageModel,
    SpeechDraftModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory, plan_gate, turns
from app.orchestration.context import EngineContext
from app.orchestration.control import TurnControl
from app.orchestration.state import DebateMessage, DebateState
from app.orchestration.turn_setup import TurnSetup, clean_skill_designations

logger = logging.getLogger(__name__)


# 커밋이 실패했을 때 다시 시도하기 전에 기다리는 시간(초). 시도는 이것보다 한 번 많습니다.
PERSIST_RETRY_DELAYS = (2.0, 5.0)

# DB 에 끝내 기록하지 못한 발언·산출물을 남기는 폴더.
#
# 예전에는 기록 실패를 로그 한 줄로 남기고 넘어갔습니다. 화면에는 이미 흘러갔으니
# 괜찮아 보이지만, 새로고침하거나 앱을 다시 켜면 **최종 합성 보고서가 사라졌습니다.**
# 실제로 자리를 비운 사이 `database is locked` 로 그렇게 됐습니다. 대화의 산출물을
# 조용히 잃는 것보다는, DB 가 아닌 곳에라도 남기고 사람에게 알리는 편이 낫습니다.
UNSAVED_DIR = DATA_DIR / "unsaved"


def save_unpersisted(
    *,
    kind: str,
    session_id: str,
    title: str,
    body: str,
    error: BaseException,
    directory: Optional[Path] = None,
) -> Optional[Path]:
    """DB 에 기록하지 못한 내용을 마크다운 파일로 남깁니다. 파일마저 못 쓰면 None.

    파일 이름에 무작위 꼬리를 붙이는 이유: 같은 초에 두 발언이 실패하면 앞의 것을
    덮어씁니다.
    """
    folder = directory or UNSAVED_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{session_id[:8]}-{kind}-{uuid.uuid4().hex[:6]}.md"
    first_line = str(error).splitlines()[0] if str(error) else ""
    header = (
        f"<!-- MADO: DB 에 기록하지 못한 {kind} -->\n"
        f"<!-- session_id: {session_id} -->\n"
        f"<!-- saved_at: {datetime.now().astimezone().isoformat(timespec='seconds')} -->\n"
        f"<!-- error: {type(error).__name__}: {first_line} -->\n\n"
        f"# {title}\n\n"
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(header + body, encoding="utf-8")
        return path
    except OSError as exc:
        logger.error(f"Could not even write the unsaved {kind} to {path}: {exc}")
        return None


class TurnRecorder:
    """턴의 기록을 DB 에 남깁니다: 발언 행, 유저 발언·개입, 기록용 안내, 도구 호출, 턴 상태.

    어떤 기록 실패로도 토론을 멈추지 않습니다 (`persist`). 끝내 못 남긴 것은 파일로 남기고 알립니다.
    """

    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx

    @staticmethod
    def tool_row(
        state: DebateState,
        agent: Agent,
        call_log: Dict[str, Any],
        *,
        message_id: Optional[str],
        record_id: Optional[str] = None,
    ) -> ToolCallRecordModel:
        security = call_log.get("security") or {}
        return ToolCallRecordModel(
            id=record_id or str(uuid.uuid4()),
            session_id=state.session_id,
            message_id=message_id,
            turn_id=state.turn_id,
            agent_key=agent.key,
            tool_name=call_log.get("tool_name", ""),
            arguments=call_log.get("arguments", {}),
            output=call_log.get("output", ""),
            status=call_log.get("status", "success"),
            # 도구 보안 판정 (`app/orchestration/tool_gate.py`). 판정 없이 실행된
            # 호출(게이트 밖의 발언)은 비어 있습니다.
            decision=str(security.get("decision") or ""),
            risk=str(security.get("risk") or ""),
            rule=str(security.get("rule") or ""),
            approver=str(security.get("approver") or ""),
        )

    async def record_tool_call(
        self,
        db,
        state: DebateState,
        agent: Agent,
        call_log: Dict[str, Any],
        db_lock: Optional[asyncio.Lock],
    ) -> Optional[str]:
        """도구 호출 하나를 실행 즉시 기록합니다. 기록 id, 못 했으면 None.

        발언 id 는 비워 둡니다 — 발언이 기록될 때 같은 커밋에서 잇습니다 (`SpeechRunner.speak`). 여기서
        실패해도 다시 시도하지 않습니다. 도구 루프를 붙잡지 않고, 그 호출은 예전처럼 발언과
        함께 기록됩니다.
        """
        record_id = str(uuid.uuid4())
        async with (db_lock or nullcontext()):
            db.add(self.tool_row(state, agent, call_log, message_id=None, record_id=record_id))
            try:
                await db.commit()
                return record_id
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 즉시 기록을 못 해도 발언과 함께 기록됩니다
                logger.warning(
                    f"Could not record {agent.name}'s tool call right away "
                    f"({call_log.get('tool_name')}); it will be saved with the message: "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug("Rollback after a failed tool-call commit also failed", exc_info=True)
                return None

    async def persist(
        self,
        db,
        build: Callable[[], List[Any]],
        *,
        what: str,
        label: str,
        kind: str,
        session_id: str,
        fallback_title: str,
        fallback_body: str,
        on_event: Optional[EventCallback],
        before_commit: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> bool:
        """행을 기록합니다. 실패하면 간격을 두고 다시 시도하고, 끝내 실패하면 파일로 남깁니다.

        `before_commit` 은 행을 넣은 뒤 같은 트랜잭션에서 돌릴 문장입니다 (이미 기록된 도구 호출을
        이 발언에 잇는 UPDATE). 다시 시도할 때마다 다시 돕니다.

        `build` 는 **시도할 때마다 새 행을 만들어** 돌려줘야 합니다. 실패한 커밋을 롤백하면
        그 트랜잭션에 넣었던 객체는 세션에서 떨어져 나가, 같은 객체를 다시 넣는 것보다
        새로 만드는 편이 확실합니다 (id 는 호출하는 쪽이 고정해 두므로 같은 행입니다).

        롤백을 빠뜨리면 세션이 깨진 채로 남아 이 대화의 다음 커밋이 전부 실패합니다.

        예외를 올리지 않습니다. 기록 실패로 토론이 멈추면, 이미 나온 발언 뒤로 이어질
        발언까지 잃습니다. 대신 잃지 않게 파일로 남기고 사람에게 알립니다. 취소만은
        그대로 올립니다.
        """
        attempts = len(PERSIST_RETRY_DELAYS) + 1
        last: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            db.add_all(build())
            try:
                if before_commit is not None:
                    await before_commit()
                await db.commit()
                if attempt > 1:
                    logger.info(f"Persisted {what} on attempt {attempt}/{attempts}")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 어떤 기록 실패로도 토론을 멈추지 않습니다
                last = exc
                logger.warning(
                    f"Could not persist {what} (attempt {attempt}/{attempts}): "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug("Rollback after a failed commit also failed", exc_info=True)
                if attempt < attempts:
                    await asyncio.sleep(PERSIST_RETRY_DELAYS[attempt - 1])

        assert last is not None
        saved_to = save_unpersisted(
            kind=kind, session_id=session_id, title=fallback_title,
            body=fallback_body, error=last,
        )
        logger.error(
            f"Gave up persisting {what} after {attempts} attempts "
            f"({type(last).__name__}: {last}); "
            + (f"saved to {saved_to}" if saved_to else "could not save it to a file either"),
            exc_info=last,
        )
        if on_event:
            first_line = str(last).splitlines()[0] if str(last) else ""
            try:
                await on_event({
                    "type": EventType.PERSIST_FAILED,
                    "what": what,
                    "label": label,
                    "error": f"{type(last).__name__}: {first_line}",
                    "saved_to": str(saved_to) if saved_to else None,
                })
            except Exception:  # noqa: BLE001 - 알리다 실패해도 토론은 계속됩니다
                logger.debug("persist_failed notification failed", exc_info=True)
        return False

    async def record_user_message(
        self,
        *,
        db,
        state: DebateState,
        content: str,
        round_number: int,
        on_event: Optional[EventCallback],
        kind: str = turns.KIND_INTERJECTION,
        extra_rows: Optional[Callable[[str, datetime], List[Any]]] = None,
    ) -> DebateMessage:
        """유저 발언을 기록에 남기고 화면에 흘립니다.

        턴을 여는 최초 요청과 토론 도중의 개입이 같은 자리에 같은 모양으로
        들어가야, 다음 발언자의 맥락(`ConversationMemory.build_context_for_agent`)과 합성 전사가
        둘을 구분 없이 읽습니다. 기록의 자리(`kind`)만 다릅니다.

        `extra_rows(발언 id, 기록 시각)` 는 이 발언과 **같은 커밋**에 넣을 행입니다 — 턴을 여는
        요청은 턴 기록과 함께 들어가야, 요청만 있고 턴이 없는(또는 그 반대) 기록이 생기지 않습니다.
        """
        msg_id = str(uuid.uuid4())
        # 사람 발언에는 걸리는 시간이 없습니다. 시작과 끝을 같은 시각으로 적어,
        # 읽는 쪽이 "시각이 없는 옛 기록" 과 구분할 수 있게 합니다.
        now = utc_now()
        await self.persist(
            db,
            lambda: (extra_rows(msg_id, now) if extra_rows else []) + [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key="user",
                sender_name="User",
                # 역할은 비웁니다 — 유저는 한 명이고 요청하는 쪽도 늘 그 유저입니다.
                sender_role="",
                content=content,
                round_number=round_number,
                msg_type="user",
                started_at=now,
                finished_at=now,
                turn_id=state.turn_id,
                turn_meta=turns.meta(kind),
            )],
            what="the user's message",
            label="유저 발언",
            kind="message-user",
            session_id=state.session_id,
            fallback_title=f"User — Round {round_number}",
            fallback_body=content,
            on_event=on_event,
        )

        message = DebateMessage(
            id=msg_id,
            sender_key="user",
            sender_name="User",
            sender_role="",
            content=content,
            round_number=round_number,
            msg_type="user",
            started_at=now,
            finished_at=now,
            turn_meta=turns.meta(kind),
        )
        state.messages.append(message)

        if on_event:
            await on_event({"type": EventType.MESSAGE_ADDED, "message": message.model_dump()})
        return message

    async def record_note(
        self,
        *,
        db,
        state: DebateState,
        on_event: Optional[EventCallback],
        agent: Agent,
        content: str,
        round_number: int,
        msg_type: str,
        graph_node_id: Optional[str] = None,
        graph_port: Optional[str] = None,
        created_at: Optional[datetime] = None,
        kind: str = turns.KIND_NOTE,
        data: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        before_commit: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> DebateMessage:
        """LLM 발언이 아닌 기록을 남깁니다 (지명 결과, 지명 실패 안내 등).

        발언(`SpeechRunner.speak`)과 같은 자리에 같은 모양으로 들어갑니다. 그래야 새로고침한 화면과
        저장 파일이 이것을 다른 발언과 똑같이 읽습니다.

        `kind` 와 `data` 는 기록의 자리입니다 (`turns.KIND_*`). 지명·분배는 누가 불렸고 무엇을
        맡았는지를 `data` 에 값으로 적어, 끊긴 턴을 다시 세울 때 문장을 읽지 않게 합니다.

        `before_commit(기록 id)` 는 같은 커밋에서 돌릴 문장입니다 — 끊긴 발언이 남긴 도구 기록을
        이 안내에 잇습니다. 그때 `tool_calls` 는 화면에 함께 보일 그 기록입니다.
        """
        msg_id = str(uuid.uuid4())
        # LLM 을 부르지 않는 기록이라 걸리는 시간이 없습니다 (시작 = 끝).
        now = utc_now()
        turn_meta = turns.meta(kind, **(data or {}))
        await self.persist(
            db,
            lambda: [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key=agent.key,
                sender_name=agent.name,
                sender_role=agent.role,
                content=content,
                round_number=round_number,
                msg_type=msg_type,
                started_at=now,
                finished_at=now,
                graph_node_id=graph_node_id,
                graph_port=graph_port,
                turn_id=state.turn_id,
                turn_meta=turn_meta,
                **({"created_at": created_at} if created_at is not None else {}),
            )],
            what=f"a note from {agent.name}",
            label=f"{agent.name} 의 기록",
            kind=f"note-{agent.key}",
            session_id=state.session_id,
            fallback_title=f"{agent.name} ({agent.role}) — Round {round_number}",
            fallback_body=content,
            on_event=on_event,
            before_commit=(lambda: before_commit(msg_id)) if before_commit is not None else None,
        )

        message = DebateMessage(
            id=msg_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=msg_type,
            tool_calls=list(tool_calls or []),
            started_at=now,
            finished_at=now,
            graph_node_id=graph_node_id,
            graph_port=graph_port,
            turn_meta=turn_meta,
        )
        state.messages.append(message)
        if on_event:
            await on_event({"type": EventType.MESSAGE_ADDED, "message": message.model_dump()})
        return message

    async def apply_interjections(
        self,
        *,
        db,
        state: DebateState,
        control: Optional[TurnControl],
        round_number: int,
        on_event: Optional[EventCallback],
    ) -> int:
        """대기 중인 사용자 개입을 지금 시점의 토론 기록에 밀어 넣습니다.

        발언이 진행되는 중간에 끼워 넣으면, 그 발언의 프롬프트는 이미 만들어진
        뒤라 반영되지도 않으면서 기록 순서만 어긋납니다. 그래서 호출 지점은 항상
        발언과 발언 사이입니다. 여기서 들어간 메모는 다음 발언자의 맥락에 그대로
        실립니다.
        """
        if control is None:
            return 0
        notes = control.drain_notes()
        await self.add_skill_designations(db, state, control.drain_skill_designations())
        for note in notes:
            await self.record_user_message(
                db=db,
                state=state,
                content=f"{memory.INTERJECTION_PREFIX}\n{note}",
                round_number=round_number,
                on_event=on_event,
                kind=turns.KIND_INTERJECTION,
            )
        state.interjection_count += len(notes)
        if notes:
            logger.info(
                f"Applied {len(notes)} user interjection(s) to session {state.session_id} "
                f"at round {round_number}"
            )
        return len(notes)

    async def add_skill_designations(
        self, db, state: DebateState, designations: Dict[str, List[str]],
    ) -> None:
        """개입 메모와 함께 온 스킬 지정을 남은 발언에 겁니다. 끊겼다 이어 가도 남도록 턴 기록에도 적습니다."""
        added = False
        for key, names in clean_skill_designations(designations, state.active_agent_keys).items():
            bucket = state.skill_designations.setdefault(key, [])
            for name in names:
                if name not in bucket:
                    bucket.append(name)
                    added = True
        if not added or not state.turn_id:
            return
        try:
            turn = await db.get(TurnModel, state.turn_id)
            config = dict(turn.config or {}) if turn is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 적지 못해도 이번 턴에는 걸립니다
            logger.warning(f"Could not read turn {state.turn_id} to save skill designations: {exc}")
            return
        if config is not None:
            config["skill_designations"] = {k: list(v) for k, v in state.skill_designations.items()}
            await self.set_turn(db, state, config=config)

    async def load_history(
        self, db, session_id: str, *, tools_for_turn: Optional[str] = None,
    ) -> List[DebateMessage]:
        """이 대화의 기록 전부를 기록 순서대로.

        `tools_for_turn` 턴의 발언에는 도구 기록도 붙입니다 — 끊긴 턴을 다시 세울 때 그 턴의
        발언은 도는 중이던 상태와 같아야 합니다 (코드 블록을 파일 참조로 바꾸는 규칙이 쓴
        파일 목록을 읽습니다). 지난 턴의 발언에는 예전처럼 붙이지 않습니다.
        """
        rows = (await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.created_at)
        )).scalars().all()
        history: List[DebateMessage] = []
        for pm in rows:
            tool_calls: List[Dict[str, Any]] = []
            if tools_for_turn is not None and pm.turn_id == tools_for_turn:
                tool_calls = [
                    {
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                        "output": tc.output,
                        "status": tc.status,
                    }
                    for tc in sorted(pm.tool_calls or [], key=lambda tc: tc.created_at)
                ]
            history.append(DebateMessage(
                id=pm.id,
                sender_key=pm.sender_key,
                sender_name=pm.sender_name,
                sender_role=pm.sender_role,
                content=pm.content,
                round_number=pm.round_number,
                msg_type=pm.msg_type,
                tool_calls=tool_calls,
                started_at=pm.started_at,
                finished_at=pm.finished_at,
                turn_started_at=pm.turn_started_at,
                graph_node_id=pm.graph_node_id,
                graph_port=pm.graph_port,
                turn_meta=pm.turn_meta if isinstance(pm.turn_meta, dict) else None,
                llm_meta=pm.llm_meta if isinstance(pm.llm_meta, dict) else None,
            ))
        return history

    async def note_interrupted_speeches(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        orphans: List[ToolCallRecordModel],
        *,
        on_event: Optional[EventCallback],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """끝나지 못한 발언이 실행한 도구를 "발언이 끊겼다" 는 안내에 잇습니다.

        그 발언의 글은 기록되지 않았습니다(취소는 기록하지 않고 올립니다). 도구 기록만 남아
        있어, 에이전트마다 안내 하나를 남기고 그 기록을 거기에 붙입니다 — 화면과 저장 문서가
        그 호출을 발언 카드처럼 보여 줍니다. 돌려주는 값은 {에이전트 키: 그 호출들} 입니다.
        """
        by_agent: Dict[str, List[ToolCallRecordModel]] = {}
        for row in orphans:
            by_agent.setdefault(row.agent_key, []).append(row)
        agents = {a.key: a for a in setup.active_agents}
        executed: Dict[str, List[Dict[str, Any]]] = {}
        for key, rows in by_agent.items():
            agent = agents.get(key) or self.ctx.agent_pool.get(key) or setup.orchestrator
            calls = [
                {
                    "tool_name": r.tool_name,
                    "arguments": r.arguments,
                    "output": r.output,
                    "status": r.status,
                    "security": {
                        "decision": r.decision or "", "risk": r.risk or "",
                        "rule": r.rule or "", "approver": r.approver or "",
                    },
                }
                for r in rows
            ]
            executed[key] = calls
            ids = [r.id for r in rows]

            async def _link(note_id: str, ids: List[str] = ids) -> None:
                await db.execute(
                    update(ToolCallRecordModel)
                    .where(ToolCallRecordModel.id.in_(ids))
                    .values(message_id=note_id)
                )

            names = ", ".join(dict.fromkeys(r.tool_name for r in rows))
            await self.record_note(
                db=db, state=state, on_event=on_event, agent=agent,
                round_number=state.current_round, msg_type="error",
                content=(
                    f"> ⚠️ **서버가 다시 시작되어 {agent.name} 의 발언이 끝나지 못했습니다.**\n>\n"
                    f"> 발언이 끊기기 전에 실행된 도구 {len(rows)}건의 기록을 이 자리에 남깁니다"
                    f" ({names}).\n>\n"
                    f"> 끊긴 발언의 글은 기록되지 않아 남아 있지 않습니다."
                ),
                kind=turns.KIND_INTERRUPTED,
                tool_calls=calls,
                before_commit=_link,
            )
        return executed

    async def settle_drafts(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        *,
        on_event: Optional[EventCallback],
        keep: Tuple[str, ...] = (),
    ) -> None:
        """이어 가지 않을 초안을 정리합니다 — 그 도구 기록은 "끊김" 안내로, 초안 행은 지웁니다.

        `keep` 에 든 종류의 초안은 남깁니다 (이어서 합성이 쓸 합성 초안). 턴이 끝날 때는 이 턴의
        초안을 모두 지웁니다 — 이어 가기가 아닌 턴(새 턴, 결론 내기)의 초안도 여기서 사라집니다.
        """
        leftover = {k: d for k, d in state.resume_drafts.items() if d.get("kind") not in keep}
        ids = {rid for d in leftover.values() for rid in (d.get("state") or {}).get("tool_record_ids") or []}
        if ids:
            rows = (await db.execute(
                select(ToolCallRecordModel)
                .where(ToolCallRecordModel.id.in_(ids), ToolCallRecordModel.message_id.is_(None))
                .order_by(ToolCallRecordModel.created_at)
            )).scalars().all()
            await self.note_interrupted_speeches(db, state, setup, rows, on_event=on_event)
        for key in leftover:
            state.resume_drafts.pop(key, None)
        if not state.turn_id:
            return
        remove = delete(SpeechDraftModel).where(SpeechDraftModel.turn_id == state.turn_id)
        if keep:
            remove = remove.where(SpeechDraftModel.kind.not_in(keep))
        try:
            await db.execute(remove)
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 초안 정리를 못 해도 턴은 끝납니다
            logger.warning(f"Could not clear the speech drafts of turn {state.turn_id}: {exc}")
            await db.rollback()

    async def set_turn(self, db, state: DebateState, **values: Any) -> None:
        """턴 기록의 단계·상태를 고칩니다. 못 적어도 턴은 계속됩니다 — 기록의 부산물이라."""
        if not state.turn_id:
            return
        try:
            await db.execute(update(TurnModel).where(TurnModel.id == state.turn_id).values(**values))
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not update turn {state.turn_id} ({values}): {type(exc).__name__}: {exc}")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("Rollback after a failed turn update also failed", exc_info=True)

    async def mark_turn_failed(self, turn_id: str, exc: BaseException) -> None:
        """예외로 멈춘 턴을 "실패" 로 적습니다. 새 DB 세션을 씁니다 — 멈춘 쪽 세션은 깨졌을 수 있습니다."""
        # 계획 승인을 기다리다 멈춘 턴은 오류가 아닙니다 (ADR-028). 사람이 읽을 문장만 적습니다.
        reason = (
            str(exc) if isinstance(exc, plan_gate.PlanApprovalExpired)
            else f"{type(exc).__name__}: {exc}"
        ).strip()
        try:
            async with self.ctx.session_factory() as db:
                await db.execute(
                    update(TurnModel)
                    .where(TurnModel.id == turn_id, TurnModel.status == TURN_RUNNING)
                    .values(status=TURN_FAILED, error=reason[:2000])
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 적지 못해도 원래 예외가 올라갑니다
            logger.warning(f"Could not mark turn {turn_id} as failed", exc_info=True)
