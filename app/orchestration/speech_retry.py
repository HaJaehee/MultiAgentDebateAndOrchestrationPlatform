"""이미 끝난 발언 하나만 다시 실행하기 (ADR-036).

전문가 한 명의 발언이 빗나갔을 때 할 수 있는 것이 턴 전체를 다시 돌리는 것뿐이면, 멀쩡한 발언까지 비용을 내고 다시
받게 됩니다. 여기서는 그 발언의 자리에 새 버전을 넣습니다.

* **자리를 지킵니다.** 발언 행의 id·정렬 시각·라운드·노드는 그대로이고 값만 바뀝니다. 장부의 반영 지점, 계획 승인,
  그래프 재구성, 검색 색인이 모두 그 id 를 보므로 어느 것도 고칠 필요가 없습니다.
* **옛 버전을 지우지 않습니다.** `MessageModel.revisions` 로 옮겨 카드에서 넘겨 보고 되돌릴 수 있습니다. 그 버전이 실행한
  도구 기록은 발언에서 떼어 버전에 적어 둡니다 (감사 기록은 그대로 남습니다).
* **실패한 시도는 지금 발언을 바꾸지 않습니다.** 멀쩡한 발언이 연결 오류 안내로 덮이면 안 됩니다. 실패도 한 버전으로
  남아, 그 사이 실행된 도구와 원인을 볼 수 있습니다.
* **그 시점의 기록으로 답합니다.** 뒤에 나온 발언과 결론을 보여 주지 않습니다. 작업 공간만은 되돌릴 수 없어 지금 상태라는
  것을 프롬프트에 적습니다.
* **뒤의 발언은 다시 돌지 않습니다.** 그것이 이 기능의 목적이지만, 어긋날 수 있다는 사실은 숨기지 않습니다 — 대화 끝에
  안내를 남겨 다음 턴의 에이전트가 읽고, 결정 장부도 새 버전에 맞춰 고칩니다.
"""

from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import select, update

from app.agents.base import Agent
from app.agents.connection import CONNECTION_FIELDS, connection_id, global_entry
from app.agents.llm_fallback import substitute_for
from app.agents.reasoning import strip_reasoning_trace
from app.config import LLMConfig
from app.database.models import (
    TURN_RUNNING,
    TURN_UNFINISHED,
    MessageModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.orchestration import turns

# 이 발언을 받을 때의 마지막 프롬프트 (`turn_meta` 의 키). 다시 실행할 때 같은 차례 지시로 답하도록 남겨 둡니다.
PROMPT_TAIL = "prompt_tail"

# 프롬프트에 실을 이전 버전의 상한(글자). 고칠 대상을 보여 주려는 것이지 전사를 늘리려는 것이 아닙니다.
PREVIOUS_VERSION_CHARS = 8000

# 모델 고르기에서 "이 에이전트의 모델 그대로".
OWN_CONNECTION = ""


class SpeechRetryError(ValueError):
    """이 발언을 다시 실행할 수 없습니다. 문구는 화면에 그대로 나갑니다."""


def retryable(msg: Any) -> bool:
    """다시 실행할 수 있는 발언인가 — 전문가 발언(실패로 끝난 것 포함)만.

    계획·취합·판정·합성은 그 뒤의 흐름을 정한 기록이라 글만 바꾸면 기록과 흐름이 어긋납니다. 서브 토론 안의 발언은
    두 전문가가 주고받은 맥락에 묶여 있습니다.
    """
    return turns.kind_of(msg) == turns.KIND_SPEECH and turns._get(msg, "msg_type") in ("agent", "error")


async def load_target(db, session_id: str, message_id: str) -> tuple:
    """다시 실행할 발언과 그 턴. 쓸 수 없으면 `SpeechRetryError`."""
    row = await db.get(MessageModel, message_id)
    if row is None or row.session_id != session_id:
        raise SpeechRetryError("다시 실행할 발언을 찾지 못했습니다.")
    if not retryable(row):
        raise SpeechRetryError("전문가의 발언만 다시 실행할 수 있습니다.")
    turn = await db.get(TurnModel, row.turn_id) if row.turn_id else None
    if turn is None:
        raise SpeechRetryError("이 발언은 턴 기록이 없어 다시 실행할 수 없습니다.")
    if turn.status == TURN_RUNNING or turn.status in TURN_UNFINISHED:
        raise SpeechRetryError("끊긴 턴의 발언입니다. 먼저 턴을 이어 가거나 마무리해 주십시오.")
    return row, turn


# ---------------------------------------------------------------- 버전


def _stamp(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else value


def _when(value: Any) -> Optional[datetime]:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else value


def live_number(revisions: Optional[Sequence[Mapping[str, Any]]]) -> int:
    """지금 보이는 버전의 번호. 지난 버전이 쓰지 않는 가장 작은 번호입니다."""
    taken = {int(entry.get("n") or 0) for entry in revisions or ()}
    return next(n for n in range(1, len(taken) + 2) if n not in taken)


def version_entry(
    *, n: int, content: str, msg_type: str, llm_meta: Any, started_at: Any, finished_at: Any,
    tool_record_ids: Iterable[str],
) -> Dict[str, Any]:
    return {
        "n": n, "content": content, "msg_type": msg_type, "llm_meta": llm_meta,
        "started_at": _stamp(started_at), "finished_at": _stamp(finished_at),
        "tool_record_ids": list(tool_record_ids),
    }


async def _tool_ids_of(db, message_id: str) -> List[str]:
    return list((await db.execute(
        select(ToolCallRecordModel.id).where(ToolCallRecordModel.message_id == message_id)
    )).scalars().all())


async def _make_live(
    db,
    message_id: str,
    *,
    retiring: Dict[str, Any],
    incoming: Dict[str, Any],
    others: Sequence[Dict[str, Any]],
) -> None:
    """`incoming` 을 발언 행의 값으로, `retiring` 을 지난 버전으로. 도구 기록도 버전을 따라갑니다."""
    if retiring["tool_record_ids"]:
        await db.execute(
            update(ToolCallRecordModel)
            .where(ToolCallRecordModel.id.in_(retiring["tool_record_ids"]))
            .values(message_id=None)
        )
    if incoming["tool_record_ids"]:
        await db.execute(
            update(ToolCallRecordModel)
            .where(ToolCallRecordModel.id.in_(incoming["tool_record_ids"]))
            .values(message_id=message_id)
        )
    await db.execute(update(MessageModel).where(MessageModel.id == message_id).values(
        content=incoming["content"],
        msg_type=incoming["msg_type"],
        llm_meta=incoming["llm_meta"],
        started_at=_when(incoming["started_at"]),
        finished_at=_when(incoming["finished_at"]),
        revisions=sorted([*others, retiring], key=lambda entry: entry["n"]),
    ))


def _revision_note(session_id: str, message_id: str, content: str) -> MessageModel:
    """대화 끝에 남기는 안내. 다음 턴의 에이전트가 읽습니다 (`session_ops.revert_turn_files` 와 같은 자리)."""
    now = utc_now()
    return MessageModel(
        session_id=session_id, sender_key="system", sender_name="MADO", sender_role="",
        msg_type="system", round_number=0, started_at=now, finished_at=now,
        turn_meta={"kind": turns.KIND_NOTE, "revised_message": message_id},
        content=content,
    )


def position_of(row_or_msg: Any) -> str:
    """화면과 안내에 적는 그 발언의 자리 — "Round 2" (그래프 토론은 "Step 2")."""
    word = "Step" if turns._get(row_or_msg, "graph_node_id") else "Round"
    return f"{word} {int(turns._get(row_or_msg, 'round_number') or 0)}"


@dataclass
class SpeechRevision:
    """다시 실행하는 발언의 자리. `SpeechRunner.speak` 이 새 행을 넣는 대신 이것으로 행을 고칩니다."""

    session_id: str
    message_id: str
    # 지금 보이는 버전 (`version_entry`)과 지난 버전들.
    live: Dict[str, Any]
    past: List[Dict[str, Any]]
    # 기록에 남길 이번 시도의 조건 (`llm_meta.retry`): model, instruction.
    condition: Dict[str, Any]
    # 그대로 두는 `turn_meta`.
    turn_meta: Dict[str, Any]
    # 안내에 적을 이름과 자리.
    agent_name: str
    position: str
    # 이 발언에 달린, 아직 전달되지 않은 코멘트. 이번 시도에 함께 실립니다.
    comments: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    async def of(
        cls, db, row: MessageModel, *, condition: Dict[str, Any], comments: List[Dict[str, Any]],
    ) -> "SpeechRevision":
        past = [dict(entry) for entry in row.revisions or []]
        return cls(
            session_id=row.session_id,
            message_id=row.id,
            live=version_entry(
                n=live_number(past), content=row.content, msg_type=row.msg_type, llm_meta=row.llm_meta,
                started_at=row.started_at, finished_at=row.finished_at,
                tool_record_ids=await _tool_ids_of(db, row.id),
            ),
            past=past,
            condition=condition,
            turn_meta=dict(row.turn_meta or {}),
            agent_name=row.sender_name,
            position=position_of(row),
            comments=comments,
        )

    async def apply(
        self,
        db,
        *,
        content: str,
        msg_type: str,
        started_at: Any,
        finished_at: Any,
        llm_meta: Optional[Dict[str, Any]],
        tool_ids: Iterable[str],
    ) -> None:
        """이번 시도의 결과를 기록합니다 (발언 기록의 커밋 안에서). 커밋이 실패하면 다시 불립니다."""
        attempt = version_entry(
            n=len(self.past) + 2, content=content, msg_type=msg_type,
            llm_meta={**(llm_meta or {}), "retry": self.condition},
            started_at=started_at, finished_at=finished_at, tool_record_ids=tool_ids,
        )
        if msg_type == "error":
            await db.execute(update(MessageModel).where(MessageModel.id == self.message_id).values(
                revisions=sorted([*self.past, attempt], key=lambda entry: entry["n"]),
            ))
            return
        await _make_live(db, self.message_id, retiring=self.live, incoming=attempt, others=self.past)
        instruction = str(self.condition.get("instruction") or "").strip()
        db.add(_revision_note(self.session_id, self.message_id, (
            f"[발언 다시 실행] 유저가 {self.agent_name} 의 {self.position} 발언을 다시 실행해 새 버전으로 바꿨습니다"
            f" (모델 `{self.condition.get('model')}`"
            + (f", 지침: {instruction}" if instruction else "")
            + "). 기록의 그 자리에는 새 버전이 있습니다. 그 뒤의 발언과 결론은 이전 버전을 보고 쓰인 것이니, "
            "어긋나는 부분은 새 버전을 기준으로 삼으세요."
        )))


async def restore_version(db, row: MessageModel, n: int) -> Dict[str, Any]:
    """지난 버전 `n` 을 다시 지금 버전으로 합니다. 커밋은 부르는 쪽이. 되돌린 버전을 돌려줍니다."""
    past = [dict(entry) for entry in row.revisions or []]
    incoming = next((entry for entry in past if int(entry.get("n") or 0) == n), None)
    if incoming is None:
        raise SpeechRetryError("되돌릴 버전을 찾지 못했습니다.")
    if incoming.get("msg_type") == "error":
        raise SpeechRetryError("실패한 시도로는 되돌릴 수 없습니다.")
    retiring = version_entry(
        n=live_number(past), content=row.content, msg_type=row.msg_type, llm_meta=row.llm_meta,
        started_at=row.started_at, finished_at=row.finished_at,
        tool_record_ids=await _tool_ids_of(db, row.id),
    )
    await _make_live(
        db, row.id, retiring=retiring, incoming=incoming,
        others=[entry for entry in past if entry is not incoming],
    )
    db.add(_revision_note(row.session_id, row.id, (
        f"[발언 되돌리기] 유저가 {row.sender_name} 의 {position_of(row)} 발언을 버전 {n} 으로 되돌렸습니다. "
        "기록의 그 자리에는 이 버전이 있습니다. 다른 버전을 기준으로 쓰인 부분과 어긋나면 이 버전을 기준으로 삼으세요."
    )))
    return incoming


async def revisions_with_tools(db, rows: Iterable[MessageModel]) -> Dict[str, List[Dict[str, Any]]]:
    """다시 실행한 발언들의 지난 버전 {발언 id: 버전들}. 버전마다 그 도구 기록을 붙입니다 (카드가 그립니다)."""
    revised = {row.id: [dict(entry) for entry in row.revisions] for row in rows if row.revisions}
    ids = [rid for entries in revised.values() for entry in entries for rid in entry.get("tool_record_ids") or []]
    calls: Dict[str, ToolCallRecordModel] = {}
    if ids:
        found = (await db.execute(select(ToolCallRecordModel).where(ToolCallRecordModel.id.in_(ids)))).scalars().all()
        calls = {tc.id: tc for tc in found}
    for entries in revised.values():
        for entry in entries:
            entry["tool_calls"] = [
                {
                    "tool_name": tc.tool_name, "arguments": tc.arguments, "output": tc.output, "status": tc.status,
                    "security": {
                        "decision": tc.decision or "", "risk": tc.risk or "",
                        "rule": tc.rule or "", "approver": tc.approver or "",
                    },
                }
                for tc in (calls.get(rid) for rid in entry.get("tool_record_ids") or []) if tc is not None
            ]
    return revised


# ---------------------------------------------------------------- 모델 고르기


def connection_choices(agents: Iterable[Agent], llm: Optional[LLMConfig]) -> Dict[str, str]:
    """다시 실행 창의 모델 목록 {연결 이름표: 화면 문구}. 이 대화의 에이전트들과 전역 기본 모델이 쓰는 연결입니다.

    화면에는 이름표만 갑니다. 키와 주소는 엔진이 이름표로 다시 찾습니다.
    """
    named: Dict[str, Dict[str, Any]] = {}
    for agent in agents:
        slot = named.setdefault(connection_id(agent), {"model": agent.model, "users": []})
        slot["users"].append(agent.name)
    choices = {
        key: f"{slot['model']} ({', '.join(slot['users'])})" for key, slot in named.items()
    }
    if llm is not None:
        target = global_entry(llm)
        key = connection_id(target)
        if target.is_live and key not in choices:
            choices[key] = f"{target.model} (전역 기본 모델)"
    return choices


def with_connection(
    agent: Agent, connection: str, candidates: Iterable[Agent], llm: Optional[LLMConfig],
) -> Agent:
    """고른 연결로 바꾼 에이전트 사본. 페르소나와 도구는 그대로이고, 그 모델의 용량 값은 함께 옮깁니다."""
    if not connection or connection == connection_id(agent):
        return agent
    for source in candidates:
        if connection_id(source) == connection:
            update_fields = {name: getattr(source, name) for name in (*CONNECTION_FIELDS, "max_tokens", "max_context_window")}
            # Provider 고유의 사고 옵션은 그 모델에 묶인 값입니다 (`llm_fallback.substitute_for` 와 같습니다).
            if agent.sequential_thinking.enabled and agent.sequential_thinking.mode == "native":
                update_fields["sequential_thinking"] = source.sequential_thinking.model_copy(deep=True)
            return agent.model_copy(update=update_fields)
    if llm is not None and connection_id(global_entry(llm)) == connection:
        substitute = substitute_for(agent, llm)
        if substitute is not None:
            return substitute
    raise SpeechRetryError("고른 모델을 이 대화에서 찾지 못했습니다. 창을 다시 열어 주십시오.")


# ---------------------------------------------------------------- 프롬프트


def retry_notice(
    *,
    previous: str = "",
    instruction: str = "",
    tools: Sequence[Mapping[str, Any]] = (),
) -> str:
    """다시 쓰는 차례의 프롬프트 끝에 붙이는 안내."""
    parts = [
        "[발언 다시 실행] 유저가 당신의 이번 차례 발언을 다시 쓰도록 요청했습니다. 위 기록은 이 차례 직전까지이고, "
        "이 차례 뒤에 나온 발언은 보여 드리지 않습니다. 작업 공간의 파일만은 그때가 아니라 지금 상태이니, 파일을 "
        "근거로 삼으려면 읽어서 확인하세요."
    ]
    body = strip_reasoning_trace(previous or "").strip()
    if body:
        if len(body) > PREVIOUS_VERSION_CHARS:
            body = body[:PREVIOUS_VERSION_CHARS] + f"\n… (이하 {len(body) - PREVIOUS_VERSION_CHARS:,}자 생략)"
        parts.append(f"[이전 버전]\n{body}")
    if (instruction or "").strip():
        parts.append(f"[유저의 추가 지침]\n{instruction.strip()}")
    if tools:
        parts.append(
            "[이전 버전이 이미 실행한 도구]\n" + turns.tool_call_lines(tools)
            + "\n같은 작업(특히 파일 쓰기)을 모르고 되풀이하지 말고, 필요하면 결과를 먼저 확인하세요."
        )
    parts.append("앞선 버전을 언급하지 말고, 이 차례의 발언 자체로 완결되게 쓰세요.")
    return "\n\n".join(parts)
