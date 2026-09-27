"""턴 기록 — 끊긴 턴을 알아보고 다시 세우는 근거 (ADR-024).

## 무엇이 문제였나

토론 한 턴은 사람의 요청으로 시작해 합성으로 끝나는데, 그 경계를 따로 적지 않았습니다.
턴이 끝났다는 표시는 합성 발언의 `turn_started_at` 뿐이라, 서버가 턴 도중에 내려가면:

* 진행 중이던 발언은 사라지고(취소는 기록하지 않고 올려 보냅니다), 끝난 발언만 남습니다.
* 남은 발언은 "끊겼다" 는 표시 없이 피드에 있다가, 다음 요청의 맥락에 조용히 섞입니다.
* 그 발언이 이미 실행한 도구는 결과(파일 쓰기 등)만 남고 기록이 없습니다 — 도구 기록은 발언
  행과 같은 커밋에 들어갔기 때문입니다.

## 여기서 하는 일

1. **기록의 자리** — 발언마다 턴의 흐름에서 맡은 자리(`turn_meta`)를 적습니다 (아래 KIND_*).
   끊긴 턴을 다시 세울 때 사람이 읽는 문장이 아니라 이것을 읽습니다.
2. **감지** — 서버가 뜰 때 "도는 중" 으로 남은 턴을 "끊김" 으로 바꿉니다
   (`mark_interrupted_turns`). 프로세스가 하나뿐이라(ADR-001) 기동 시점의 "도는 중" 은 곧
   죽은 턴입니다.
3. **안내** — 끊긴 턴이 있는 대화를 열면 무엇이 남았는지 요약합니다 (`unfinished_turn`).
   사람이 이어 가기 · 지금까지로 결론 내기 · 버리기를 고릅니다. 자동으로 재개하지 않습니다 —
   사람 없이 LLM 비용이 나가고 도구가 실행되면 안 됩니다.
4. **재구성** — 기록에서 실패한 에이전트, 계획의 자리, 개입 수 등을 다시 셉니다. LLM 도, 엔진도
   모르는 순수 함수라 테스트가 기록만 가지고 확인합니다.

실제로 이어 가는 일은 `OrchestratorEngine.resume_turn` 이 합니다.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import func, select, update

from app.database.models import (
    TURN_ABANDONED,
    TURN_COMPLETED,
    TURN_INTERRUPTED,
    TURN_RUNNING,
    TURN_UNFINISHED,
    MessageModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.orchestration.graph import Activation, GraphScheduler, GraphSpec
from app.orchestration.graph_run import OUTPUT_PORTS
from app.timestamps import _as_datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 기록의 자리

KIND_OPENING = "opening"            # 턴을 연 사람 요청
KIND_INTERJECTION = "interjection"  # 토론 도중 사람의 개입
KIND_PLAN = "plan"                  # 오케스트레이터 계획
KIND_SPEECH = "speech"              # 전문가 발언 (라운드·병렬·그래프 노드)
KIND_MERGE = "merge"                # 오케스트레이터 취합 (병렬 라운드 끝, 그래프 취합 노드)
KIND_GATE = "gate"                  # 그래프 판정
KIND_NOMINATION = "nomination"      # 이번 라운드 발언자 지명. 값: speakers
KIND_ASSIGNMENT = "assignment"      # 이번 라운드 병렬 과업 분배. 값: tasks
KIND_NOTE = "note"                  # 그 밖의 안내 (지명 실패, 그래프 상한 등)
KIND_FAILURE = "failure"            # 발언이 예외로 끝났다는 안내 (병렬·그래프)
KIND_INTERRUPTED = "interrupted"    # 서버 중단으로 발언이 끊겼다는 안내 (재개할 때 남김)
KIND_SYNTHESIS = "synthesis"        # 턴을 마무리한 합성

# 실패로 끝나면 "그 에이전트가 이번 턴에 말하지 못했다" 는 뜻이 되는 자리.
_SPEAKING_KINDS = (KIND_PLAN, KIND_SPEECH, KIND_MERGE, KIND_SYNTHESIS)


def meta(kind: str, **data: Any) -> Dict[str, Any]:
    """`turn_meta` 에 적을 값."""
    return {"kind": kind, **data}


def _get(msg: Any, key: str, default: Any = None) -> Any:
    if isinstance(msg, Mapping):
        return msg.get(key, default)
    return getattr(msg, key, default)


def kind_of(msg: Any) -> Optional[str]:
    value = _get(msg, "turn_meta")
    return value.get("kind") if isinstance(value, Mapping) else None


def failed_agents(messages: Iterable[Any]) -> List[str]:
    """이번 턴에 응답을 받지 못한 에이전트 (기록 순서).

    엔진이 도는 동안 `DebateState.failed_agent_keys` 에 쌓는 것과 같은 기준입니다 — 발언이
    실패로 끝났거나(`msg_type="error"`), 병렬·그래프 발언이 예외로 끝났다는 안내가 남은 경우.
    지명·분배 실패 안내처럼 에이전트가 아니라 경로가 실패한 기록은 세지 않습니다.
    """
    keys: List[str] = []
    for msg in messages:
        kind = kind_of(msg)
        failed = (
            (_get(msg, "msg_type") == "error" and kind in _SPEAKING_KINDS)
            or (kind == KIND_FAILURE and _get(msg, "sender_key") != "orchestrator")
        )
        key = _get(msg, "sender_key")
        if failed and key and key not in keys:
            keys.append(key)
    return keys


def plan_position(messages: Sequence[Any], start: int = 0) -> Optional[int]:
    """이번 턴 계획 발언의 자리. 계획이 없거나 실패했으면 None."""
    for index in range(start, len(messages)):
        msg = messages[index]
        if kind_of(msg) == KIND_PLAN:
            return None if _get(msg, "msg_type") == "error" else index
    return None


def count_kind(messages: Iterable[Any], kind: str) -> int:
    return sum(1 for m in messages if kind_of(m) == kind)


def has_synthesis(messages: Iterable[Any]) -> bool:
    return any(kind_of(m) == KIND_SYNTHESIS or _get(m, "turn_started_at") is not None for m in messages)


def specialist_speeches(messages: Iterable[Any]) -> int:
    """전문가가 실제로 남긴 발언 수. 0 이면 "지금까지로 결론 내기" 가 의미 없습니다."""
    return sum(
        1 for m in messages
        if kind_of(m) == KIND_SPEECH and _get(m, "msg_type") != "error"
    )


def last_activity(turn: Any, messages: Iterable[Any], tool_times: Iterable[Any] = ()) -> Optional[datetime]:
    """턴이 마지막으로 움직인 시각 — 끊긴 채로 있던 시간을 재는 기준.

    끝난 발언의 종료 시각, 즉시 기록된 도구 호출 시각, 턴 행이 마지막으로 바뀐 시각 중 가장
    늦은 것입니다. 진행 중이던 발언의 시작 시각은 기록에 없으므로 실제로 멈춘 순간보다 조금
    이를 수 있습니다 (그만큼 중단 시간이 길게 잡힙니다 — 짧게 잡는 것보다 정직합니다).
    """
    stamps = [_as_datetime(_get(turn, "updated_at")), _as_datetime(_get(turn, "started_at"))]
    for msg in messages:
        stamps.append(_as_datetime(_get(msg, "finished_at")) or _as_datetime(_get(msg, "created_at")))
    stamps.extend(_as_datetime(t) for t in tool_times)
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


# ---------------------------------------------------------------- 커서

# 라운드 안에서 남는 기록. 이 중 가장 늦은 라운드가 끊긴 라운드입니다 — 지명·분배만 기록되고
# 아무도 발언하지 못한 채 끊긴 라운드도 있습니다.
_ROUND_KINDS = (KIND_SPEECH, KIND_NOMINATION, KIND_ASSIGNMENT, KIND_MERGE, KIND_FAILURE)


def round_progress(messages: Iterable[Any]) -> Tuple[int, Set[str]]:
    """라운드 전략이 어디까지 돌았나 — (마지막 라운드, 그 라운드에 이미 발언한 에이전트).

    실패로 끝난 발언도 "발언한 것" 으로 셉니다 (병렬 발언이 예외로 끝난 안내 포함). 도는 중이던
    엔진도 실패한 발언자를 다시 부르지 않고 다음 사람으로 넘어갔습니다. 아직 어떤 라운드도
    시작되지 않았으면 (0, 빈 집합).

    순서는 자리가 아니라 **에이전트 키**로 맞춥니다. 끊긴 사이에 카드 순서를 바꿨어도, 그
    라운드에 아직 말하지 않은 사람만 지금의 순서대로 이어서 말합니다.
    """
    rows = [m for m in messages if kind_of(m) in _ROUND_KINDS]
    if not rows:
        return 0, set()
    last = max(int(_get(m, "round_number") or 0) for m in rows)
    return last, {
        _get(m, "sender_key") for m in rows
        if int(_get(m, "round_number") or 0) == last
        and kind_of(m) in (KIND_SPEECH, KIND_FAILURE)
        and _get(m, "sender_key") != "orchestrator"
    }


@dataclass
class RoundRecord:
    """한 라운드에서 오케스트레이터가 정한 것 — 끊긴 라운드를 이어 갈 때 다시 묻지 않습니다."""

    # 기록된 지명 (에이전트 키, 발언 순서). 지명 전에 끊겼으면 None.
    nominated: Optional[List[str]] = None
    # 기록된 병렬 분배 [(에이전트 키, 과업)]. 분배 전에 끊겼으면 None.
    tasks: Optional[List[Tuple[str, str]]] = None
    # 병렬 라운드의 취합까지 기록됐는지.
    merged: bool = False


def round_record(messages: Iterable[Any], round_num: int) -> RoundRecord:
    """`round_num` 라운드의 지명·분배·취합 기록. 문장이 아니라 `turn_meta` 의 값을 읽습니다."""
    record = RoundRecord()
    for msg in messages:
        if int(_get(msg, "round_number") or 0) != round_num:
            continue
        kind = kind_of(msg)
        data = _get(msg, "turn_meta") or {}
        if kind == KIND_NOMINATION and isinstance(data.get("speakers"), list):
            record.nominated = [str(key) for key in data["speakers"]]
        elif kind == KIND_ASSIGNMENT and isinstance(data.get("tasks"), list):
            record.tasks = [
                (str(item.get("agent") or ""), str(item.get("task") or ""))
                for item in data["tasks"] if isinstance(item, Mapping)
            ]
        elif kind == KIND_MERGE:
            record.merged = True
    return record


@dataclass
class GraphReplay:
    """기록을 따라 다시 돌린 그래프 스케줄러 (`replay_graph`)."""

    scheduler: GraphScheduler
    # 기록에 남은 마지막 단계. 0 이면 아직 어떤 노드도 돌지 않았습니다.
    step: int
    # 그 단계에 돈 노드 전부와, 그중 출력을 내지 못한 노드. 끊긴 단계가 아니면 둘 다 비어 있습니다.
    activations: List[Activation] = field(default_factory=list)
    pending: List[Activation] = field(default_factory=list)
    # 끊긴 단계에서 이미 나온 출력 {노드 id: (핀, 값)}.
    outputs: Dict[str, Tuple[str, List[str]]] = field(default_factory=dict)


def replay_graph(
    spec: GraphSpec, max_visits: int, start_value: List[str], messages: Sequence[Any],
) -> GraphReplay:
    """턴의 기록을 따라 그래프 스케줄러를 다시 돌립니다. LLM 을 부르지 않습니다.

    스케줄러는 결정적입니다 — 같은 출력을 같은 순서로 넣으면 같은 노드를 같은 단계에 깨웁니다.
    노드가 낸 발언에는 단계(`round_number`)·노드·나간 핀이 적혀 있으므로, 단계마다 `ready()` 로
    깨운 노드의 출력을 기록에서 찾아 넣으면 끊기기 직전의 받은편지함과 방문 수가 그대로 섭니다.
    판정 노드의 값은 도는 중과 같이 "판정 기록 + 판정한 입력" 입니다.

    마지막 단계에서 출력이 없는 노드가 있으면 그 단계가 끊긴 것이고, 그 노드들이 `pending` 입니다.
    그보다 앞 단계에서 출력이 빠져 있으면 기록이 이 그래프와 맞지 않는 것이라 ValueError.
    """
    scheduler = GraphScheduler(spec, max_visits)
    scheduler.deliver(spec.start.id, "out", start_value)
    present = {_get(m, "id") for m in messages}
    by_step: Dict[int, Dict[str, Any]] = {}
    for msg in messages:
        node_id = _get(msg, "graph_node_id")
        if node_id and _get(msg, "graph_port") in OUTPUT_PORTS:
            by_step.setdefault(int(_get(msg, "round_number") or 0), {})[node_id] = msg
    last_step = max(by_step, default=0)

    step = 0
    while step < last_step:
        ready = scheduler.ready()
        if not ready or any(n.type == "end" for n in ready):
            raise ValueError(f"기록에는 {last_step}단계까지 있는데 그래프가 {step}단계에서 멈춥니다.")
        step += 1
        activations = [scheduler.activate(node) for node in ready]
        recorded = by_step.get(step, {})
        outputs: Dict[str, Tuple[str, List[str]]] = {}
        for activation in activations:
            msg = recorded.get(activation.node.id)
            if msg is None:
                continue
            value = [_get(msg, "id")]
            if activation.node.type == "gate":
                # 도는 중의 판정 노드가 내보낸 값과 같게 — 판정 기록 + 판정한 입력 (`_run_gate`).
                seen: List[str] = []
                for _edge, ids in activation.inputs:
                    for mid in ids:
                        if mid in present and mid not in seen:
                            seen.append(mid)
                value += seen
            outputs[activation.node.id] = (_get(msg, "graph_port"), value)
        pending = [a for a in activations if a.node.id not in outputs]
        if pending:
            if step < last_step:
                raise ValueError(
                    f"{step}단계의 노드 출력이 기록에 없습니다: {', '.join(a.node.id for a in pending)}"
                )
            return GraphReplay(scheduler, step, activations, pending, outputs)
        for activation in activations:
            port, value = outputs[activation.node.id]
            scheduler.deliver(activation.node.id, port, value)
    return GraphReplay(scheduler, step)


def resume_notice(calls: Sequence[Mapping[str, Any]], output_chars: int = 300) -> str:
    """끊긴 발언을 다시 하는 에이전트에게 붙이는 안내 — 이미 실행한 도구와 결과.

    발언의 글은 남지 않았지만 도구는 실행됐습니다. 모르고 같은 파일 쓰기를 되풀이하지 않도록,
    실행된 것을 관찰 결과로 건넵니다 (ADR-014: 도구의 결과는 관찰입니다).
    """
    lines = []
    for call in calls:
        try:
            args = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(call.get("arguments"))
        output = str(call.get("output") or "").strip().replace("\n", " ")
        if len(output) > output_chars:
            output = output[:output_chars] + "…"
        lines.append(f"- {call.get('tool_name')}({args[:200]}) → {call.get('status') or 'success'}: {output}")
    return (
        "[재개 안내] 서버가 다시 시작되어 당신의 직전 발언이 끝나지 못했고, 그 글은 남아 있지 "
        "않습니다. 끊기기 전에 당신이 이미 실행한 도구와 결과입니다:\n"
        + "\n".join(lines)
        + "\n같은 작업(특히 파일 쓰기)을 모르고 되풀이하지 말고, 필요하면 결과를 먼저 확인한 뒤 "
        "발언을 처음부터 다시 작성하세요."
    )


# ---------------------------------------------------------------- 감지

async def mark_interrupted_turns(db) -> Dict[str, int]:
    """기동할 때 "도는 중" 으로 남은 턴을 정리합니다. {"interrupted": n, "completed": m}.

    합성 발언까지 기록된 턴은 사실상 끝난 턴입니다 — 합성 뒤에는 산출물 저장(밀리초)과 장부
    갱신이 남는데, 장부는 반영 지점이 발언 id 로 적혀 있어 다음 턴이 이어 접습니다(ADR-020).
    그런 턴은 "완료" 로 적고, 나머지는 "끊김" 으로 적습니다.
    """
    rows = (await db.execute(select(TurnModel).where(TurnModel.status == TURN_RUNNING))).scalars().all()
    counts = {"interrupted": 0, "completed": 0}
    for turn in rows:
        closed = await db.scalar(
            select(func.count()).select_from(MessageModel).where(
                MessageModel.turn_id == turn.id, MessageModel.turn_started_at.is_not(None),
            )
        )
        if closed:
            turn.status = TURN_COMPLETED
            turn.phase = "completed"
            turn.finished_at = turn.finished_at or utc_now()
            counts["completed"] += 1
        else:
            turn.status = TURN_INTERRUPTED
            counts["interrupted"] += 1
    if rows:
        await db.commit()
        logger.warning(
            "Marked %d debate turn(s) as interrupted and %d as completed after a restart",
            counts["interrupted"], counts["completed"],
        )
    return counts


async def abandon_unfinished(db, session_id: str) -> int:
    """이 대화의 끊긴 턴을 "버려짐" 으로 적습니다. 새 요청이 들어올 때 부릅니다. 커밋은 부르는 쪽이."""
    result = await db.execute(
        update(TurnModel)
        .where(
            TurnModel.session_id == session_id,
            TurnModel.status.in_(TURN_UNFINISHED + (TURN_RUNNING,)),
        )
        .values(status=TURN_ABANDONED, updated_at=TurnModel.updated_at)
    )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------- 안내

# 이어 가기를 지원하는 전략. 여기 없는 전략은 합성 중에 끊긴 턴만 이어 갈 수 있고, 그 밖에는 결론
# 내기·버리기만 고를 수 있습니다. 지금은 다섯 전략 모두입니다 — 지명·분배처럼 LLM 이 정한 경로도
# 기록에 값으로 남으므로(`round_record`) 다시 묻지 않고 그대로 이어 갑니다.
CONTINUABLE_STRATEGIES = frozenset({
    "sequential_debate", "adversarial_debate", "orchestrator_led", "parallel_dispatch", "graph_debate",
})


def can_continue(strategy: str, phase: str) -> bool:
    return strategy in CONTINUABLE_STRATEGIES or phase == "synthesizing"


@dataclass
class UnfinishedTurn:
    """끊긴 턴의 요약 — 화면의 안내 줄이 그립니다."""

    turn_id: str
    session_id: str
    status: str
    phase: str
    error: str
    prompt: str
    strategy: str
    workspace_dir: str
    started_at: Optional[datetime]
    stopped_at: Optional[datetime]
    # 전문가가 남긴 발언 수, 끝나지 못한 발언이 실행한 도구 수.
    speeches: int
    orphan_tools: int
    can_continue: bool
    can_finish: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


async def unfinished_turn(db, session_id: str) -> Optional[UnfinishedTurn]:
    """이 대화의 끊긴 턴. 없으면 None. 여럿이면 가장 최근 것 (나머지는 새 턴이 이미 버렸습니다)."""
    turn = (await db.execute(
        select(TurnModel)
        .where(TurnModel.session_id == session_id, TurnModel.status.in_(TURN_UNFINISHED))
        .order_by(TurnModel.started_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if turn is None:
        return None
    messages = (await db.execute(
        select(MessageModel).where(MessageModel.turn_id == turn.id).order_by(MessageModel.created_at)
    )).scalars().all()
    orphans = (await db.execute(
        select(ToolCallRecordModel.created_at).where(
            ToolCallRecordModel.turn_id == turn.id, ToolCallRecordModel.message_id.is_(None),
        )
    )).scalars().all()
    opening = next((m for m in messages if m.id == turn.opening_message_id), None)
    config = turn.config or {}
    speeches = specialist_speeches(messages)
    return UnfinishedTurn(
        turn_id=turn.id,
        session_id=session_id,
        status=turn.status,
        phase=turn.phase,
        error=turn.error or "",
        prompt=opening.content if opening is not None else "",
        strategy=str(config.get("strategy") or ""),
        workspace_dir=str(config.get("workspace_dir") or ""),
        started_at=turn.started_at,
        stopped_at=last_activity(turn, messages, orphans),
        speeches=speeches,
        orphan_tools=len(orphans),
        can_continue=can_continue(str(config.get("strategy") or ""), turn.phase),
        can_finish=speeches > 0,
    )
