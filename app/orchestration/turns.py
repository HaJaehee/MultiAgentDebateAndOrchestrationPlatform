"""턴 기록 — 끊긴 턴을 알아보고 다시 세우는 근거 (ADR-024).

## 무엇이 문제였나

토론의 각 턴은 사용자 요청으로 시작하여 합성으로 종료되나, 이전에는 명시적 경계 마커가 없었습니다.
턴 완료 표시는 합성 발언의 `turn_started_at`뿐이어서, 서버가 실행 도중 중단되면:

* 진행 중이던 발언은 사라지고(취소는 기록하지 않고 올려 보냅니다), 끝난 발언만 남습니다.
* 남은 발언은 "끊겼다" 는 표시 없이 피드에 있다가, 다음 요청의 맥락에 조용히 섞입니다.
* 그 발언이 이미 실행한 도구는 결과(파일 쓰기 등)만 남고 기록이 없습니다 — 도구 기록은 발언
  행과 같은 커밋에 들어갔기 때문입니다.

## 여기서 하는 일

1. **기록의 자리** — 발언마다 턴 흐름상 역할(`turn_meta`)을 명시합니다 (하단 KIND_* 참조).
   중단된 턴을 복원할 때 자연어 본문 대신 이 메타데이터를 파싱합니다.
2. **감지** — 서버 기동 시 "실행 중" 상태로 잔류된 턴을 "중단됨" 상태로 전환합니다
   (`mark_interrupted_turns`). 단일 프로세스 아키텍처(ADR-001)상 기동 시점의 "실행 중" 턴은
   비정상 종료된 턴입니다.
3. **안내** — 중단된 턴이 존재하는 세션 진입 시 잔여 상태를 요약합니다 (`unfinished_turn`).
   사용자가 재개(이어가기) / 현 상태로 결론 / 폐기 중 선택합니다. 사용자 승인 없이
   불필요한 LLM 과금이나 도구 실행이 발생하는 것을 방지하기 위해 자동 재개하지 않습니다.
4. **재구성** — 기록에서 실패 에이전트, 계획 위치, 개입 횟수 등을 재산출합니다. 순수 함수 구조로
   설계되어 테스트가 DB 기록만으로 무결성을 검증합니다.

실제로 이어 가는 일은 `OrchestratorEngine.resume_turn` 이 합니다.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import delete, func, or_, select, update

from app.database.models import (
    TURN_ABANDONED,
    TURN_COMPLETED,
    TURN_INTERRUPTED,
    TURN_RUNNING,
    TURN_UNFINISHED,
    MessageModel,
    SpeechDraftModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.agents.llm_loop import SPEECH_STATE_VERSION
from app.agents.llm_usage import Usage
from app.mcp.manager import compose_scope
from app.mcp.policy import EARLIER_RESULTS_SOURCE, wrap_untrusted
from app.orchestration.graph import Activation, GraphScheduler, GraphSpec
from app.orchestration.graph_run import OUTPUT_PORTS
from app.timestamps import _as_datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 기록의 자리

KIND_OPENING = "opening"            # 턴을 시작한 사용자 요청
KIND_INTERJECTION = "interjection"  # 토론 진행 중 사용자 개입
KIND_PLAN = "plan"                  # 오케스트레이터 계획
KIND_SPEECH = "speech"              # 전문가 발언 (라운드·병렬·그래프 노드)
KIND_MERGE = "merge"                # 오케스트레이터 취합 (병렬 라운드 끝, 그래프 취합 노드)
KIND_GATE = "gate"                  # 그래프 판정
KIND_NOMINATION = "nomination"      # 이번 라운드 발언자 지명. 값: speakers
KIND_ASSIGNMENT = "assignment"      # 이번 라운드 병렬 태스크 분배. 값: tasks
KIND_NOTE = "note"                  # 그 밖의 안내 (지명 실패, 그래프 상한 등)
KIND_FAILURE = "failure"            # 발언이 예외로 끝났다는 안내 (병렬·그래프)
KIND_INTERRUPTED = "interrupted"    # 서버 중단으로 발언이 끊겼다는 안내 (재개할 때 남김)
KIND_SYNTHESIS = "synthesis"        # 턴을 마무리한 합성
# 토론 조기 종료 및 결론 합성 전환 안내 메시지 유형 (v1.5). 값: reason (`converged` · `budget`), round
KIND_EARLY_END = "early_end"
# 외부 정체 관찰자가 N회 연속 동일 실패를 감지해 재설계로 에스컬레이션한 안내 (ADR-049). 값: round, streak, signatures
KIND_STAGNATION = "stagnation"
# 계획 승인 (ADR-028). 계획 발언 뒤, 토론이 시작되기 전에만 나옵니다.
KIND_PLAN_REVISION = "plan_revision"  # 사용자의 계획 수정 요청. 후속 계획이 이전 계획을 대체합니다
KIND_APPROVAL = "plan_approval"       # 사용자의 계획 승인. 값: plan_id, tasks, changes, approver
# 오케스트레이터가 그린 이번 턴의 실행 그래프 (ADR-034). 값: spec, fallback
KIND_GRAPH_DRAFT = "graph_draft"
# 서브 토론 (ADR-034). 라운드(그래프는 단계) 경계에서 전문가 두 명이 쟁점 하나를 따로 다룹니다.
KIND_SUBDEBATE_OPEN = "subdebate_open"              # 값: issue, issue_no, agents
KIND_SUBDEBATE_SPEECH = "subdebate_speech"          # 그 안의 발언. 본 기록의 맥락에는 싣지 않습니다
KIND_SUBDEBATE_CONCLUSION = "subdebate_conclusion"  # 오케스트레이터의 결론. 이것이 본 토론에 합류합니다
# 사용자가 결정 장부를 직접 편집한 기록 (ADR-038). 유저 발언 기록에 함께 고정됩니다 (`build_user_record`).
KIND_LEDGER_EDIT = "ledger_edit"
# 끊긴 턴을 이어 가기 전에 커널 셀을 다시 실행한 기록 (ADR-046). 값: kernels. 다시 실행한 도구 기록이 이것에 이어집니다.
KIND_KERNEL_REPLAY = "kernel_replay"

# 실패로 끝나면 "그 에이전트가 이번 턴에 말하지 못했다" 는 뜻이 되는 자리.
_SPEAKING_KINDS = (
    KIND_PLAN, KIND_SPEECH, KIND_MERGE, KIND_SYNTHESIS, KIND_SUBDEBATE_SPEECH, KIND_SUBDEBATE_CONCLUSION,
)

# 발언 카드와 내보내기에 붙이는 이름.
SUB_DEBATE_LABELS = {
    KIND_SUBDEBATE_OPEN: "서브 토론 시작",
    KIND_SUBDEBATE_SPEECH: "서브 토론",
    KIND_SUBDEBATE_CONCLUSION: "서브 토론 결론",
}


# ---------------------------------------------------------------- 발언 번호 (ADR-052)

# 발언 초안의 상태(`SpeechDraftModel.state`)에 적어 두는 예약 번호의 열쇠. 초안에서 이어 가는 발언은 이 번호를 그대로 씁니다.
DRAFT_SPEECH_NO = "speech_no"


async def speech_no_floor(db, session_id: str, turn_id: Optional[str]) -> int:
    """이 대화에서 이미 쓴 가장 큰 발언 번호. 기록된 발언과 이 턴의 발언 초안에 예약해 둔 번호 중 큰 것.

    초안에서 이어 가는 발언은 초안의 번호를 그대로 쓰므로, 새 번호는 그보다 커야 겹치지 않습니다. 예약만 하고 저장되지
    않은 번호(초안도 없이 끊긴 발언)는 다시 쓰여도 됩니다. 그 번호는 어떤 프롬프트에도 실린 적이 없습니다.
    """
    recorded = await db.scalar(
        select(func.max(MessageModel.speech_no)).where(MessageModel.session_id == session_id)
    )
    reserved = 0
    if turn_id:
        for draft_state in (await db.execute(
            select(SpeechDraftModel.state).where(SpeechDraftModel.turn_id == turn_id)
        )).scalars():
            number = draft_state.get(DRAFT_SPEECH_NO) if isinstance(draft_state, Mapping) else None
            if isinstance(number, int) and not isinstance(number, bool):
                reserved = max(reserved, number)
    return max(int(recorded or 0), reserved)


async def ensure_speech_counter(db, state: Any) -> None:
    """턴에서 처음 번호를 낼 때 계수기를 DB 에 맞춥니다 (새 턴·재개·재실행 모두 이 길). 읽지 못하면 메모리의 값으로 이어 갑니다."""
    if state.speech_counter_ready:
        return
    try:
        floor = await speech_no_floor(db, state.session_id, state.turn_id)
        # 읽은 트랜잭션을 닫아 연결을 돌려줍니다 (ADR-041). 바로 뒤에 LLM 응답을 기다리는 호출(그래프 판정 등)이 옵니다.
        await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 번호를 못 맞춰도 발언은 합니다
        logger.warning(f"Could not read the speech numbers of session {state.session_id}: {type(exc).__name__}: {exc}")
        floor = 0
    state.speech_counter = max(int(state.speech_counter or 0), floor)
    state.speech_counter_ready = True


def reserve_speech_no(state: Any) -> int:
    """다음 발언 번호를 냅니다. 한 세션에서는 턴이 하나씩만 돌므로 경쟁은 없습니다."""
    state.speech_counter = int(state.speech_counter or 0) + 1
    return state.speech_counter


def draft_key(agent_key: str, kind: str, round_number: int, graph_node_id: Optional[str]) -> str:
    """발언 초안(ADR-025)이 어느 발언의 것인지 맞추는 열쇠.

    이어 가는 턴에서 같은 자리의 발언이 다시 불릴 때 이 열쇠로 초안을 찾습니다 — 같은 에이전트,
    같은 종류, 같은 라운드(그래프는 단계), 같은 노드. 계획과 합성은 한 번에 하나만 쓰이므로
    라운드를 보지 않습니다 (합성의 라운드 번호는 합성 직전의 라운드에서 셉니다). 수정 요청으로
    계획을 다시 쓰는 턴에도 끝나지 못한 계획 발언은 많아야 하나입니다 (ADR-028).
    """
    if kind in (KIND_PLAN, KIND_SYNTHESIS):
        return f"{agent_key}|{kind}"
    return f"{agent_key}|{kind}|{int(round_number or 0)}|{graph_node_id or ''}"


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


def in_main_record(msg: Any) -> bool:
    """본 토론의 기록인가. 서브 토론 안의 발언은 화면과 저장 문서에만 남고, 맥락·장부·합성에는 결론만 갑니다."""
    return kind_of(msg) != KIND_SUBDEBATE_SPEECH


def sub_debate_label(msg: Any) -> Optional[str]:
    """서브 토론의 기록이면 그 이름. 아니면 None."""
    return SUB_DEBATE_LABELS.get(kind_of(msg) or "")


def fallback_agents(messages: Iterable[Any]) -> List[str]:
    """이번 턴에 전역 기본 모델로 발언한 에이전트 (기록 순서, `MessageModel.llm_meta`)."""
    keys: List[str] = []
    for msg in messages:
        meta = _get(msg, "llm_meta")
        key = _get(msg, "sender_key")
        if isinstance(meta, dict) and meta.get("fallback") and key and key not in keys:
            keys.append(key)
    return keys


def failed_agents(messages: Iterable[Any]) -> List[str]:
    """이번 턴에 응답을 받지 못한 에이전트 (기록 순서).

    엔진 실행 중 `DebateState.failed_agent_keys`에 누적되는 기준과 동일합니다 — 발언이
    실패로 끝났거나(`msg_type="error"`), 병렬/그래프 발언이 예외로 끝났다는 안내가 남은 경우.
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
    """이번 턴 계획 발언의 자리. 계획이 없거나 실패했으면 None.

    사용자가 수정을 요청하여 계획이 재생성된 경우 (ADR-028) 복수의 계획 발언이 존재할 수 있습니다. 그때는
    **마지막으로 성공한** 계획을 채택합니다 — 재생성 실패 시 직전 유효 계획이 유지됩니다.
    """
    found: Optional[int] = None
    for index in range(start, len(messages)):
        msg = messages[index]
        if kind_of(msg) == KIND_PLAN and _get(msg, "msg_type") != "error":
            found = index
    return found


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

    실패로 종료된 발언도 발언 완료로 계산합니다 (예외 안내 포함). 실행 중이던
    엔진도 실패한 발언자를 재호출하지 않고 다음 에이전트로 전환했습니다. 아직 라운드가
    시작되지 않은 경우 (0, 빈 집합).

    순서는 표시 위치가 아닌 **에이전트 키**를 기준으로 판정합니다. 중단 중 카드 순서가 변경되었더라도,
    해당 라운드에서 아직 발언하지 않은 에이전트만 현재 순서대로 이어서 발언합니다.
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
    # 기록된 병렬 분배 [(에이전트 키, 태스크)]. 분배 전에 끊겼으면 None.
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
    게이트 판정 노드의 값은 실행 중 상태와 동일하게 "판정 기록 + 판정 대상 입력"입니다.

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
                # 실행 중 게이트 노드가 출력한 값과 동일하게 유지 — 판정 기록 + 판정 대상 입력 (`GraphRunner.run_gate`).
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


def drawn_graph(messages: Iterable[Any]) -> Optional[Dict[str, Any]]:
    """이 턴에 오케스트레이터가 그려 기록해 둔 그래프 (`GraphSpec.dump()` 모양). 아직 없으면 None."""
    found: Optional[Dict[str, Any]] = None
    for msg in messages:
        if kind_of(msg) == KIND_GRAPH_DRAFT:
            spec = (_get(msg, "turn_meta") or {}).get("spec")
            if isinstance(spec, Mapping):
                found = dict(spec)
    return found


@dataclass
class SubDebateRecord:
    """한 경계에서 열린 서브 토론이 어디까지 갔나 (`sub_debate_at`). 끊긴 턴은 이것으로 이어 갑니다."""

    issue: str
    agents: List[str]
    speeches: List[Any] = field(default_factory=list)
    concluded: bool = False
    # 쟁점의 고정 번호 (ADR-051). 번호 없는 쟁점이나 이 칸이 생기기 전의 기록은 None.
    issue_no: Optional[int] = None


def sub_debate_at(messages: Iterable[Any], boundary: int) -> Optional[SubDebateRecord]:
    """`boundary` 라운드(그래프는 단계) 끝에 열린 서브 토론. 경계마다 많아야 하나입니다."""
    record: Optional[SubDebateRecord] = None
    for msg in messages:
        if int(_get(msg, "round_number") or 0) != boundary:
            continue
        kind = kind_of(msg)
        if kind == KIND_SUBDEBATE_OPEN:
            data = _get(msg, "turn_meta") or {}
            issue_no = data.get("issue_no")
            record = SubDebateRecord(
                issue=str(data.get("issue") or ""), agents=[str(k) for k in data.get("agents") or []],
                issue_no=issue_no if isinstance(issue_no, int) and not isinstance(issue_no, bool) else None,
            )
        elif record is not None and kind == KIND_SUBDEBATE_SPEECH:
            record.speeches.append(msg)
        elif record is not None and kind == KIND_SUBDEBATE_CONCLUSION:
            record.concluded = True
    return record


def tool_call_lines(calls: Sequence[Mapping[str, Any]], output_chars: int = 300) -> str:
    """이전에 실행된 도구 호출 및 결과 목록을 프롬프트 인용 문자열로 포맷합니다.

    개별 호출의 외부 콘텐츠 여부는 DB에 별도 기록되지 않으므로 전체를 보안 래퍼로 감쌉니다. 도구 실행 결과는 모두 모델의 참고 자료에 해당합니다.
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
    return wrap_untrusted("\n".join(lines), EARLIER_RESULTS_SOURCE)


def resume_notice(calls: Sequence[Mapping[str, Any]], output_chars: int = 300) -> str:
    """끊긴 발언을 다시 하는 에이전트에게 붙이는 안내 — 이미 실행한 도구와 결과.

    발언의 글은 남지 않았지만 도구는 실행됐습니다. 모르고 같은 파일 쓰기를 되풀이하지 않도록,
    실행된 것을 관찰 결과로 건넵니다 (ADR-014: 도구의 결과는 관찰입니다).
    """
    return (
        "[재개 안내] 서버가 다시 시작되어 당신의 직전 발언이 끝나지 못했고, 그 글은 남아 있지 "
        "않습니다. 끊기기 전에 당신이 이미 실행한 도구와 결과입니다:\n"
        + tool_call_lines(calls, output_chars)
        + "\n같은 작업(특히 파일 쓰기)을 모르고 되풀이하지 말고, 필요하면 결과를 먼저 확인한 뒤 "
        "발언을 처음부터 다시 작성하세요."
    )


# ---------------------------------------------------------------- 커널 셀 (ADR-046)

# 커널 내 코드 실행 도구 및 커널 초기화 도구 접미사 목록.
KERNEL_RUN_TOOLS = ("execute_python_code", "run_python_file")
KERNEL_RESET_TOOL = "reset_kernel_state"
# 코드 정상 실행 결과 접두사. 내장 샌드박스는 예외/타임아웃이 아닐 경우 `RAN_` 접두사로 판정을 반환합니다.
_CELL_RAN_PREFIX = "execution_status: RAN_"
# 실행 시간 초과로 인한 커널 프로세스 재시작 판별 문구 (기존 변수 소멸).
_KERNEL_LOST_MARKS = ("The kernel had to be restarted", "Kernel was unresponsive")


def cell_ran(status: Any, output: Any) -> bool:
    """해당 도구 호출의 코드가 정상 완료되었는지 판별합니다. 샌드박스 서버는 코드 예외 발생 시에도 도구 호출 자체는 성공으로 반환합니다."""
    return status == "success" and str(output or "").startswith(_CELL_RAN_PREFIX)


def kernel_namespace(tool_name: str, session_id: str, agent_key: str) -> str:
    """해당 도구 호출이 바인딩되는 커널 네임스페이스를 반환합니다 (`compose_scope` 반환값과 동일)."""
    server = tool_name.split("__", 1)[0] if "__" in tool_name else ""
    return compose_scope(server, session_id, agent_key) or ""


def kernel_cells(
    rows: Iterable[Any], session_id: str, replay_notes: Iterable[str] = (),
) -> Dict[str, List[Any]]:
    """커널 변수 복구를 위해 선별된 재실행 대상 코드 셀 맵 {네임스페이스: [도구 기록 목록]}을 반환합니다 (`rows`는 시간순 정렬).

    정상 완료된 호출만 선별하며, 커널 초기화 이후의 호출만 포함합니다. 이전 재실행 기록
    (`replay_notes` 연결 행)은 중복 방지를 위해 제외합니다.
    """
    skipped = set(replay_notes)
    cells: Dict[str, List[Any]] = {}
    for row in rows:
        if _get(row, "message_id") in skipped:
            continue
        name = str(_get(row, "tool_name") or "")
        tail = name.split("__", 1)[-1]
        status, output = _get(row, "status"), str(_get(row, "output") or "")
        namespace = kernel_namespace(name, session_id, str(_get(row, "agent_key") or ""))
        if tail == KERNEL_RESET_TOOL:
            if status == "success" and output.startswith('Namespace "'):
                cells[namespace] = []
        elif tail in KERNEL_RUN_TOOLS:
            if any(mark in output for mark in _KERNEL_LOST_MARKS):
                cells[namespace] = []
            elif cell_ran(status, output):
                cells.setdefault(namespace, []).append(row)
    return {namespace: found for namespace, found in cells.items() if found}


async def turn_kernel_cells(db, session_id: str, turn_id: str, messages: Iterable[Any]) -> Dict[str, List[Any]]:
    """해당 턴의 영속화된 도구 실행 이력에서 재실행 대상 코드 셀들을 조회합니다 (`kernel_cells`). 도구는 실행 즉시 영속화되므로 중단 시점의 호출도 누락되지 않습니다."""
    tails = (*KERNEL_RUN_TOOLS, KERNEL_RESET_TOOL)
    rows = (await db.execute(
        select(ToolCallRecordModel)
        .where(
            ToolCallRecordModel.turn_id == turn_id,
            or_(*(ToolCallRecordModel.tool_name.like(f"%{tail}") for tail in tails)),
        )
        .order_by(ToolCallRecordModel.created_at)
    )).scalars().all()
    notes = [_get(m, "id") for m in messages if kind_of(m) == KIND_KERNEL_REPLAY]
    return kernel_cells(rows, session_id, notes)


# ---------------------------------------------------------------- 감지

async def mark_interrupted_turns(db) -> Dict[str, int]:
    """서버 기동 시 미완료("실행 중") 상태로 잔류된 턴을 복구/정리합니다. {"interrupted": n, "completed": m}.

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
            # 끝난 턴에는 이어 갈 발언이 없습니다.
            await db.execute(delete(SpeechDraftModel).where(SpeechDraftModel.turn_id == turn.id))
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
    """이 대화의 끊긴 턴을 "버려짐" 으로 적습니다. 새 요청이 들어올 때 부릅니다. 커밋은 부르는 쪽이.

    버린 턴의 발언 초안(ADR-025)은 더 이어 갈 일이 없으므로 함께 지웁니다.
    """
    await db.execute(delete(SpeechDraftModel).where(SpeechDraftModel.turn_id.in_(
        select(TurnModel.id).where(
            TurnModel.session_id == session_id,
            TurnModel.status.in_(TURN_UNFINISHED + (TURN_RUNNING,)),
        )
    )))
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
# 내기·버리기만 고를 수 있습니다. 지금은 여섯 전략 모두입니다 — 지명·분배·그린 그래프처럼 LLM 이 정한
# 경로도 기록에 값으로 남으므로(`round_record`, `drawn_graph`) 다시 묻지 않고 그대로 이어 갑니다.
CONTINUABLE_STRATEGIES = frozenset({
    "sequential_debate", "adversarial_debate", "orchestrator_led", "parallel_dispatch", "graph_debate",
    "adaptive_graph",
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
    # 전문가가 남긴 발언 수, 끝나지 못한 발언이 실행한 도구 수, 도구 단위로 이어 갈 수 있는 발언 수.
    speeches: int
    orphan_tools: int
    resumable_speeches: int
    can_continue: bool
    can_finish: bool
    # 세션 재개 전 선별 재실행 가능한 커널 코드 셀 수 (ADR-046). 0일 경우 재실행 버튼이 비활성화됩니다.
    replayable_cells: int = 0

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
    drafts = (await db.execute(
        select(SpeechDraftModel.state).where(SpeechDraftModel.turn_id == turn.id)
    )).scalars().all()
    resumable = sum(1 for s in drafts if (s or {}).get("version") == SPEECH_STATE_VERSION)
    cells = await turn_kernel_cells(db, session_id, turn.id, messages)
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
        resumable_speeches=resumable,
        can_continue=can_continue(str(config.get("strategy") or ""), turn.phase),
        can_finish=speeches > 0,
        replayable_cells=sum(len(found) for found in cells.values()),
    )


async def usage_by_session(db, session_id: Optional[str] = None) -> Dict[str, Usage]:
    """세션별 전체 턴의 소모 토큰 및 LLM 소요 시간 누적 합(`TurnModel.usage`)을 반환합니다. `session_id` 를 지정하면 해당 세션만 조회합니다.

    해당 컬럼 도입 이전의 레거시 턴은 집계에서 제외됩니다 (데이터가 없는 세션은 결과 딕셔너리에 포함되지 않음).
    """
    stmt = select(TurnModel.session_id, TurnModel.usage).where(TurnModel.usage.is_not(None))
    if session_id is not None:
        stmt = stmt.where(TurnModel.session_id == session_id)
    totals: Dict[str, Usage] = {}
    for sid, raw in (await db.execute(stmt)).all():
        usage = Usage.of(raw)
        if usage.empty:
            continue
        totals.setdefault(sid, Usage()).add(usage)
    return totals


EARLY_END_CONVERGED = "converged"
EARLY_END_BUDGET = "budget"


def early_end(messages: Iterable[Any]) -> Optional[Tuple[str, int]]:
    """해당 턴에 조기 종료 이벤트가 발생했는지 확인하여 (`converged`·`budget`, 해당 라운드 번호) 튜플 또는 None을 반환합니다."""
    for msg in messages:
        if kind_of(msg) != KIND_EARLY_END:
            continue
        meta = _get(msg, "turn_meta") or {}
        reason = meta.get("reason")
        if reason in (EARLY_END_CONVERGED, EARLY_END_BUDGET):
            return reason, int(meta.get("round") or _get(msg, "round_number") or 0)
    return None
