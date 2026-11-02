"""대화·턴이 쓴 토큰과 시간을 에이전트별·라운드별로 나눕니다 (ADR-048).

새로 재는 것은 없습니다. 재료는 발언 기록의 `llm_meta.usage` · 시작·종료 시각과 턴 기록의 `turns.usage` 입니다.

* **토큰** — 발언 하나의 값을 그대로 더합니다. 다시 실행한 발언(ADR-036)의 지난 버전도 그 턴이 실제로 쓴 토큰이므로
  함께 셉니다. 턴 합계에서 발언 합을 뺀 나머지가 **보조 호출**입니다: 결정 장부·중간 요약·라우팅처럼 발언이 아닌
  호출과, 끊겨서 기록되지 못한 발언의 호출입니다.
* **시간** — LLM 시간(`llm_seconds`)은 모델 응답만 잽니다. 발언의 경과 시간에는 도구 실행, LLM 게이트 순번, 사람의 승인
  대기가 더 들어 있어, 둘의 차이가 곧 "도구·대기" 입니다. 병렬 라운드는 발언 구간이 겹치므로 경과는 합이 아니라
  **구간의 합집합**입니다. 그래서 라운드의 경과에는 LLM 합계를 나눠 그리지 않습니다 (경과보다 길 수 있습니다).
* **이어 간 발언** — 끊긴 발언은 시작 시각을 끊기기 전 그대로 이어받습니다. 그대로 재면 서버가 꺼져 있던 시간이 병목으로
  보이므로 이어 간 시각(`llm_meta.resumed_at`)부터 잽니다. 토큰도 이어 간 뒤의 호출만 재므로 둘의 범위가 같습니다.
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
# 라운드 표의 줄 순서: 계획 → 라운드(서브 토론은 그 라운드 바로 뒤) → 합성.
_FIRST_SLOT = (0, 0)
_LAST_SLOT = (10 ** 9, 0)


def token_parts(usage: Usage) -> Tuple[int, int, int, int]:
    """겹치지 않는 네 조각: 캐시를 뺀 입력, 캐시, 추론을 뺀 출력, 추론. 합은 늘 `total_tokens` 입니다."""
    cached = min(usage.cached_tokens, usage.prompt_tokens)
    reasoning = min(usage.reasoning_tokens, usage.completion_tokens)
    return usage.prompt_tokens - cached, cached, usage.completion_tokens - reasoning, reasoning


@dataclass
class UsageRow:
    """차트의 한 줄 (에이전트 하나, 라운드 하나, 턴 하나)."""

    key: str
    label: str
    usage: Usage = field(default_factory=Usage)
    speeches: int = 0
    # 초. 발언 구간의 합집합 길이입니다. 보조 호출은 구간이 없어 LLM 시간을 그대로 씁니다.
    elapsed: float = 0.0
    other: bool = False

    @property
    def rest_seconds(self) -> float:
        """경과 중 LLM 응답을 기다린 시간이 아닌 나머지 (도구 실행, 순번, 승인 대기)."""
        return max(0.0, self.elapsed - self.usage.llm_seconds)

    @property
    def per_call_seconds(self) -> float:
        return self.usage.llm_seconds / self.usage.calls if self.usage.calls else 0.0


@dataclass(frozen=True)
class TurnChoice:
    """보기 범위로 고를 수 있는 턴 하나."""

    id: str
    label: str


@dataclass
class UsageReport:
    total: Usage
    elapsed: float
    by_agent: List[UsageRow]
    # 턴 하나를 보면 라운드별, 턴이 여럿인 대화를 보면 턴별입니다 (`step_word`). 턴을 넘나들며 같은 번호의 라운드를
    # 더한 값은 뜻이 없습니다. 턴별에서 라운드를 보려면 범위를 그 턴으로 좁힙니다.
    by_step: List[UsageRow]
    step_word: str
    # 사용량이 기록된 이 대화의 모든 턴. 지금 범위와 상관없이 전부 담습니다.
    turns: List[TurnChoice]

    @property
    def empty(self) -> bool:
        return self.total.empty


@dataclass(frozen=True)
class _Attempt:
    """발언의 한 버전이 쓴 양. 다시 실행한 적 없는 발언은 하나, 다시 실행한 발언은 시도마다 하나입니다."""

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
    # 그래프 토론의 라운드는 단계입니다 (발언 카드의 "Step N" 과 같은 수).
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
    """구간들이 차지한 시간. 겹친 부분은 한 번만 셉니다."""
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
    """`turns` 는 시작 순서대로 (`id`, `usage`, `started_at`), `messages` 는 그 대화의 기록입니다."""
    numbers = {turn.id: number for number, turn in enumerate(turns, 1)}
    attempts_by_turn: Dict[str, List[_Attempt]] = {}
    for message in messages:
        if message.turn_id in numbers:
            attempts_by_turn.setdefault(message.turn_id, []).extend(_attempts(message))

    # 턴 합계는 단계가 바뀔 때만 저장되므로 진행 중인 턴은 발언 합보다 작을 수 있습니다. 그래서 합계를 턴 기록에서
    # 그대로 가져오지 않고 "발언 합 + 나머지" 로 셉니다. 빼는 쪽이 0 에서 멈추므로 늘 맞습니다.
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
    # 호출이 겹친 발언(동료 자문)은 LLM 합계가 구간보다 길 수 있습니다. 막대가 합계를 담도록 맞춥니다.
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
    """이 대화(`turn_id` 를 주면 그 턴)의 사용량 분석. 본문은 읽지 않습니다."""
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
    """값이 가장 큰 줄과 전체에서 차지하는 비중. 비교할 줄이 둘 이상일 때만. 보조 호출은 뽑지 않습니다."""
    named = [row for row in rows if not row.other]
    whole = sum(value(row) for row in rows)
    if len(named) < 2 or whole <= 0:
        return None
    top = max(named, key=value)
    return (top, value(top) / whole) if value(top) > 0 else None


def bottleneck_lines(rows: Sequence[UsageRow]) -> List[str]:
    """`시간 병목: Coder 1분 12초 (61%)` 와 `토큰 최다: Architect 13.4k (48%)`. 비교할 것이 없으면 그 줄은 없습니다."""
    lines: List[str] = []
    slowest = _leader(rows, lambda row: row.elapsed)
    if slowest:
        lines.append(f"시간 병목: {slowest[0].label} {format_duration(slowest[0].elapsed)} ({slowest[1]:.0%})")
    largest = _leader(rows, lambda row: row.usage.total_tokens)
    if largest:
        lines.append(f"토큰 최다: {largest[0].label} {format_tokens(largest[0].usage.total_tokens)} ({largest[1]:.0%})")
    return lines
