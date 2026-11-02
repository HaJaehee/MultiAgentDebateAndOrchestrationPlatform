"""대화·턴이 소모한 토큰과 소요 시간을 에이전트별·라운드별로 집계합니다 (ADR-048).

별도로 새로 측정하는 값 없이, 발언 기록의 `llm_meta.usage` · 시작/종료 시각과 턴 기록의 `turns.usage`를 바탕으로 집계합니다.

* **토큰** — 각 발언의 토큰 사용량을 합산합니다. 재실행된 발언(ADR-036)의 이전 버전 역시 해당 턴에서 실제로 소모된 토큰이므로
  합산에 포함합니다. 턴 합계에서 발언들의 합을 뺀 나머지는 **보조 호출**로 분류됩니다: 의사결정 장부 기록·중간 요약·라우팅처럼
  발언에 속하지 않는 호출과, 중단되어 기록되지 못한 발언의 호출이 여기에 해당합니다.
* **시간** — LLM 소요 시간(`llm_seconds`)은 모델의 순수 응답 시간만을 측정합니다. 발언의 전체 경과 시간에는 도구 실행,
  LLM 게이트 대기 순번, 사용자 승인 대기 시간 등이 추가로 포함되므로, 둘의 차이가 곧 "도구·대기" 시간이 됩니다. 병렬 라운드는
  발언 시간대가 겹치므로 전체 경과는 단순 합산이 아닌 **시간 구간들의 합집합**으로 계산합니다. 따라서 라운드별 경과 시간 막대에서는
  LLM 소요 시간을 분할하여 표시하지 않습니다 (병렬 실행 시 LLM 시간의 단순 합계가 실제 경과 시간보다 길 수 있기 때문입니다).
* **중단 후 재개된 발언** — 중단되었던 발언은 중단 전의 시작 시각을 그대로 유지합니다. 이를 그대로 소요 시간 계산에 사용하면
  서버가 꺼져 있던 유휴 시간까지 병목 시간으로 잘못 표시되므로, 실제 재개된 시각(`llm_meta.resumed_at`)부터 측정합니다.
  토큰 역시 재개 이후의 호출분만 반영되므로 시간과 토큰의 집계 범위가 일치합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import select

from app.agents.llm_usage import Usage, format_tokens
from app.database.models import MessageModel, TurnModel
from app.orchestration.turns import (
    KIND_PLAN,
    KIND_SUBDEBATE_CONCLUSION,
    KIND_SUBDEBATE_SPEECH,
    KIND_SYNTHESIS,
    kind_of,
)
from app.timestamps import _as_datetime, format_duration, to_local

OTHER_LABEL = "보조 호출"
OTHER_KEY = "__other__"

_Span = Tuple[datetime, datetime]
# 라운드 표 표시 순서: 계획 → 라운드(서브 토론은 해당 라운드 바로 뒤) → 결론 합성.
_FIRST_SLOT = (0, 0)
_LAST_SLOT = (10 ** 9, 0)


def token_parts(usage: Usage) -> Tuple[int, int, int, int]:
    """중복되지 않는 4가지 세부 구성: 캐시 제외 입력, 캐시, 추론 제외 출력, 추론 토큰. 4가지의 합은 항상 `total_tokens`와 같습니다."""
    cached = min(usage.cached_tokens, usage.prompt_tokens)
    reasoning = min(usage.reasoning_tokens, usage.completion_tokens)
    return usage.prompt_tokens - cached, cached, usage.completion_tokens - reasoning, reasoning


@dataclass
class UsageRow:
    """차트의 한 행 (개별 에이전트, 라운드, 또는 턴)."""

    key: str
    label: str
    usage: Usage = field(default_factory=Usage)
    speeches: int = 0
    # 초 단위. 발언 구간들의 합집합 총 시간입니다. 보조 호출은 시간 구간이 없으므로 LLM 소요 시간을 그대로 사용합니다.
    elapsed: float = 0.0
    other: bool = False

    @property
    def rest_seconds(self) -> float:
        """전체 경과 시간 중 LLM 응답 시간을 제외한 나머지 시간 (도구 실행, 순번 대기, 승인 대기 등)."""
        return max(0.0, self.elapsed - self.usage.llm_seconds)

    @property
    def per_call_seconds(self) -> float:
        return self.usage.llm_seconds / self.usage.calls if self.usage.calls else 0.0


@dataclass(frozen=True)
class TurnChoice:
    """조회 범위로 선택 가능한 개별 턴."""

    id: str
    label: str


@dataclass
class UsageReport:
    total: Usage
    elapsed: float
    by_agent: List[UsageRow]
    # 단일 턴 조회 시에는 라운드별, 복수 턴 대화 조회 시에는 턴별로 집계됩니다 (`step_word`). 여러 턴에 걸쳐
    # 동일 라운드 번호를 단순 합산하는 것은 의미가 없기 때문입니다. 턴별 조회 중 세부 라운드를 확인하려면 범위를 특정 턴으로 지정합니다.
    by_step: List[UsageRow]
    step_word: str
    # 사용량이 기록된 해당 대화의 모든 턴 목록입니다. 현재 선택된 조회 범위와 무관하게 전체를 포함합니다.
    turns: List[TurnChoice]

    @property
    def empty(self) -> bool:
        return self.total.empty


@dataclass(frozen=True)
class _Attempt:
    """개별 발언(또는 특정 수정 버전)의 사용량입니다. 재실행된 적 없는 발언은 1개, 재실행된 발언은 시도 횟수마다 1개씩 생성됩니다."""

    agent_key: str
    agent_name: str
    slot: Tuple[int, int]
    slot_label: str
    usage: Usage
    span: Optional[_Span]


def _step_of(kind: Optional[str], round_number: int, graph_node_id: Optional[str]) -> Tuple[Tuple[int, int], str]:
    if kind == KIND_SYNTHESIS:
        return _LAST_SLOT, "결론 합성"
    if kind == KIND_PLAN or round_number <= 0:
        return _FIRST_SLOT, "계획"
    if kind in (KIND_SUBDEBATE_SPEECH, KIND_SUBDEBATE_CONCLUSION):
        return (round_number, 1), f"서브 토론 {round_number}"
    # 그래프 토론에서의 라운드는 단계를 의미합니다 (발언 카드의 "Step N"과 동일).
    return (round_number, 0), f"단계 {round_number}" if graph_node_id else f"라운드 {round_number}"


def _attempts(row: Any) -> Iterator[_Attempt]:
    slot, slot_label = _step_of(kind_of(row), int(row.round_number or 0), row.graph_node_id)
    live = {"llm_meta": row.llm_meta, "started_at": row.started_at, "finished_at": row.finished_at}
    past = row.revisions if isinstance(row.revisions, list) else []
    for version in (live, *past):
        meta = version.get("llm_meta")
        usage = Usage.of(meta.get("usage")) if isinstance(meta, Mapping) else Usage()
        if usage.empty:
            continue
        started = _as_datetime(meta.get("resumed_at")) or _as_datetime(version.get("started_at"))
        finished = _as_datetime(version.get("finished_at"))
        span = (started, finished) if started and finished and finished > started else None
        yield _Attempt(row.sender_key, row.sender_name, slot, slot_label, usage, span)


def _union_seconds(spans: Iterable[_Span]) -> float:
    """여러 시간 구간의 합집합 총 시간입니다. 겹치는 구간은 한 번만 계산합니다."""
    total = 0.0
    reached: Optional[datetime] = None
    for start, finish in sorted(spans):
        if reached is None or start >= reached:
            total += (finish - start).total_seconds()
            reached = finish
        elif finish > reached:
            total += (finish - reached).total_seconds()
            reached = finish
    return total


def _row_of(key: str, label: str, attempts: Sequence[_Attempt]) -> UsageRow:
    row = UsageRow(key=key, label=label, speeches=len(attempts))
    for attempt in attempts:
        row.usage.add(attempt.usage)
    row.elapsed = _union_seconds(a.span for a in attempts if a.span)
    return row


def _group(attempts: Iterable[_Attempt], key: Callable[[_Attempt], Any]) -> Dict[Any, List[_Attempt]]:
    groups: Dict[Any, List[_Attempt]] = {}
    for attempt in attempts:
        groups.setdefault(key(attempt), []).append(attempt)
    return groups


def _other_row(usage: Usage) -> UsageRow:
    return UsageRow(key=OTHER_KEY, label=OTHER_LABEL, usage=usage, elapsed=usage.llm_seconds, other=True)


def build_report(turns: Sequence[Any], messages: Iterable[Any], turn_id: Optional[str] = None) -> UsageReport:
    """`turns`는 시작 순서대로 정렬된 턴 목록(`id`, `usage`, `started_at`)이며, `messages`는 해당 대화의 메시지 기록입니다."""
    numbers = {turn.id: number for number, turn in enumerate(turns, 1)}
    attempts_by_turn: Dict[str, List[_Attempt]] = {}
    for message in messages:
        if message.turn_id in numbers:
            attempts_by_turn.setdefault(message.turn_id, []).extend(_attempts(message))

    # 턴 사용량 합계는 단계가 전환될 때 갱신되므로, 현재 진행 중인 턴은 턴 합계가 개별 발언들의 합보다 작을 수 있습니다.
    # 따라서 턴 기록의 합계를 그대로 쓰지 않고 '발언 합계 + 나머지(보조 호출)' 방식으로 집계합니다. 차감 시 음수가 되지 않도록 보정하므로 항상 정확합니다.
    others: Dict[str, Usage] = {}
    for turn in turns:
        spoken = Usage()
        for attempt in attempts_by_turn.get(turn.id, ()):
            spoken.add(attempt.usage)
        others[turn.id] = Usage.of(turn.usage).minus(spoken)

    recorded = [turn for turn in turns if attempts_by_turn.get(turn.id) or not others[turn.id].empty]
    picked = [turn for turn in recorded if turn_id in (None, turn.id)]

    attempts = [attempt for turn in picked for attempt in attempts_by_turn.get(turn.id, ())]
    other_total = Usage()
    for turn in picked:
        if not others[turn.id].empty:
            other_total.add(others[turn.id])
    other_rows = [_other_row(other_total)] if not other_total.empty else []

    by_agent = [
        _row_of(key, group[-1].agent_name, group)
        for key, group in _group(attempts, lambda a: a.agent_key).items()
    ]
    # 병렬 호출이 발생한 발언(예: 동료 자문)은 LLM 소요 시간 합계가 실제 구간보다 길 수 있습니다. 막대 그래프가 전체 LLM 시간을 수용할 수 있도록 맞춥니다.
    for row in by_agent:
        row.elapsed = max(row.elapsed, row.usage.llm_seconds)
    by_agent.sort(key=lambda r: (-r.elapsed, -r.usage.total_tokens, r.label))

    turn_rows: List[UsageRow] = []
    for turn in picked:
        row = _row_of(turn.id, f"턴 {numbers[turn.id]}", attempts_by_turn.get(turn.id, ()))
        row.elapsed = (row.elapsed or row.usage.llm_seconds) + others[turn.id].llm_seconds
        row.usage.add(others[turn.id])
        turn_rows.append(row)

    if len(picked) > 1:
        by_step, step_word = turn_rows, "턴별"
    else:
        groups = _group(attempts, lambda a: (a.slot, a.slot_label))
        by_step = []
        for (_slot, label), group in sorted(groups.items(), key=lambda item: item[0][0]):
            row = _row_of(label, label, group)
            row.elapsed = row.elapsed or row.usage.llm_seconds
            by_step.append(row)
        by_step.extend(other_rows)
        step_word = "라운드별"

    total = Usage()
    for row in turn_rows:
        total.add(row.usage)
    return UsageReport(
        total=total,
        elapsed=sum(row.elapsed for row in turn_rows),
        by_agent=by_agent + other_rows,
        by_step=by_step,
        step_word=step_word,
        turns=[
            TurnChoice(turn.id, f"턴 {numbers[turn.id]} · {to_local(turn.started_at).strftime('%m-%d %H:%M')}")
            for turn in recorded
        ],
    )


async def load_usage_report(db: Any, session_id: str, turn_id: Optional[str] = None) -> UsageReport:
    """해당 대화(`turn_id` 지정 시 특정 턴)의 사용량 보고서를 조회합니다. 메시지 본문은 조회하지 않습니다."""
    turns = (await db.execute(
        select(TurnModel.id, TurnModel.usage, TurnModel.started_at)
        .where(TurnModel.session_id == session_id)
        .order_by(TurnModel.started_at)
    )).all()
    messages = (await db.execute(
        select(
            MessageModel.turn_id, MessageModel.sender_key, MessageModel.sender_name, MessageModel.round_number,
            MessageModel.graph_node_id, MessageModel.turn_meta, MessageModel.llm_meta, MessageModel.revisions,
            MessageModel.started_at, MessageModel.finished_at,
        )
        .where(MessageModel.session_id == session_id, MessageModel.turn_id.is_not(None))
        .order_by(MessageModel.created_at)
    )).all()
    return build_report(turns, messages, turn_id)


def _leader(rows: Sequence[UsageRow], value: Callable[[UsageRow], float]) -> Optional[Tuple[UsageRow, float]]:
    """가장 큰 값을 가진 행과 전체 대비 비율을 계산합니다. 비교 대상 행이 2개 이상일 때만 산출하며, 보조 호출은 제외합니다."""
    named = [row for row in rows if not row.other]
    whole = sum(value(row) for row in rows)
    if len(named) < 2 or whole <= 0:
        return None
    top = max(named, key=value)
    return (top, value(top) / whole) if value(top) > 0 else None


def bottleneck_lines(rows: Sequence[UsageRow]) -> List[str]:
    """'시간 병목: Coder 1분 12초 (61%)', '토큰 최다: Architect 13.4k (48%)' 등의 병목 요약 문구를 생성합니다. 비교 대상이 부족하면 해당 항목을 생략합니다."""
    lines: List[str] = []
    slowest = _leader(rows, lambda row: row.elapsed)
    if slowest:
        lines.append(f"시간 병목: {slowest[0].label} {format_duration(slowest[0].elapsed)} ({slowest[1]:.0%})")
    largest = _leader(rows, lambda row: row.usage.total_tokens)
    if largest:
        lines.append(f"토큰 최다: {largest[0].label} {format_tokens(largest[0].usage.total_tokens)} ({largest[1]:.0%})")
    return lines
