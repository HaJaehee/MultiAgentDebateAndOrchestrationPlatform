"""결정 장부 요약 띠 — 상태 영역 하단 고정 단일 행 컴포넌트 (전문가 화면, docs/feature_ui_upgrade.md P2).

기존 결정 장부는 로스터 하단 접이식 패널에만 위치하여 토론 진행 중 실시간 확인이 어려웠고, 라운드를 거치며 미해결 쟁점이
어떻게 해소되었는지 변화 추이를 확인할 수 없었습니다. 본 요약 띠는 피드 스크롤과 무관하게 상단 상태 영역에 항상 노출되는 단일 행 바 형태로 제공됩니다.

    결정 5 · 기각 2 · 미해결 2  I-3 I-5        추이 3 → R1 3 → R2 2 → 합성 0 ✓

* **단일 행 고정 높이:** 쟁점 추가나 종결에 따라 높이가 변하면 실시간 스트리밍 중 피드 스크롤 위치가 흔들리는 현상이 발생합니다.
  공간이 협소할 경우 [요약 통계(항상 노출)] → [쟁점 칩(최대 4개)] → [추이] 순으로 표시 공간을 확보하며, 추이 영역은 띠 너비 기준의
  CSS Container Query로 자동 숨김 처리합니다 (`theme.py`의 `.ledger-strip`). 브라우저 창 전체 너비가 아니라 스플리터 조절에 따른 피드 너비를
  동적으로 반영하기 위함입니다. 숨겨진 추이는 통계 레이블 툴팁에서 확인할 수 있습니다.
* **가장 최근 턴 기준 추이:** 시작값은 직전 턴의 최종 개정 상태입니다. 마지막 라운드는 장부를 별도 갱신하지 않고 (`debate._close_round`)
  최종 종합 단계에서 일괄 갱신하므로 마지막 지점은 '합성'입니다. 종합 진행 중에는 반투명한 `합성 …` 칩을 표시하며, 최종 개정이 완료되면 실제 수치로
  갱신되고, 미완료 종료 시에는 자동으로 제거됩니다.
* 개정 이력이 없는 기존 레거시 세션의 경우 장부 본문에서 직접 집계한 개수만 표시합니다.

비즈니스 로직 연산은 순수 함수(`strip_counts`, `trend_points`, `issue_opened_label`)로 구성되며, UI 컴포넌트는 해당 결과를 렌더링하는 역할에 집중합니다.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from nicegui import ui

from app.orchestration import ledger_history

# 쟁점 칩 최대 표시 개수. 초과 시 '외 n'으로 축약하고 툴팁에 상세 목록을 표시합니다.
MAX_ISSUE_CHIPS = 4
# 추이에 표시할 최대 지점 수. 초과 시 앞부분을 '…'으로 축약합니다.
MAX_TREND_POINTS = 5

STRIP_CLASSES = (
    "ledger-strip w-full h-7 items-center gap-2 px-3 rounded-lg text-[11px] flex-nowrap overflow-hidden "
    "min-w-0 whitespace-nowrap bg-slate-900/70 border border-slate-800"
)
TREND_CLASSES = "ledger-strip-trend items-center gap-1 flex-nowrap min-w-0 overflow-hidden ml-auto text-slate-400"
CHIP_CLASSES = "px-1.5 rounded bg-amber-900/40 text-amber-200 font-mono flex-shrink-0 cursor-pointer"
POINT_CLASSES = "px-1 rounded bg-slate-800 text-slate-300 flex-shrink-0"
PENDING_POINT_CLASSES = "px-1 rounded bg-slate-800 text-slate-500 opacity-60 flex-shrink-0"

# 단일 턴 내 라운드 단위를 구성하는 개정 종류 (재생성 및 사용자 직접 편집은 라운드 단위 개정이 아님).
_ROUND_KINDS = (
    ledger_history.KIND_ROUND, ledger_history.KIND_STEP, ledger_history.KIND_SUBDEBATE, ledger_history.KIND_RESUME,
)
_TURN_KINDS = _ROUND_KINDS + (ledger_history.KIND_FINAL,)

# 결정 장부 패널 열기 콜백. 로스터 컴포넌트를 제어하는 상위 페이지 레벨에서 주입합니다.
OpenLedger = Callable[[], None]
# 라운드 칩 클릭 콜백 (턴 ID, 라운드 번호). 해당 라운드 묶음 위치로 스크롤 이동합니다.
RevealRound = Callable[[str, int], Any]


@dataclass
class TrendPoint:
    """추이 데이터 포인트. `label`이 비어 있으면 시작값(직전 턴의 최종 개정 상태)이며, `value`가 None이면 종합 진행 중 상태입니다."""

    label: str
    value: Optional[int]
    turn_id: Optional[str] = None
    round_number: int = 0

    @property
    def pending(self) -> bool:
        return self.value is None

    @property
    def text(self) -> str:
        shown = "…" if self.value is None else str(self.value)
        return f"{self.label} {shown}" if self.label else shown


def _open_count(revision: Dict[str, Any]) -> int:
    return len(revision.get("open") or [])


def strip_counts(revisions: Sequence[Dict[str, Any]], ledger: str) -> Dict[str, Any]:
    """요약 띠에 표시할 카운트와 미해결 쟁점 목록을 추출합니다. 개정 이력이 존재하면 최신 개정을 사용하고, 없으면 장부 본문에서 직접 파싱합니다 (레거시 대화)."""
    if revisions:
        last = revisions[-1]
        return {
            "decided": int(last.get("decided") or 0), "rejected": int(last.get("rejected") or 0),
            "open": [dict(item) for item in last.get("open") or []],
        }
    return ledger_history.summarize(ledger or "")


def latest_turn(revisions: Sequence[Dict[str, Any]], synthesizing: Optional[str] = None) -> Optional[str]:
    """추이를 표시할 대상 턴 ID를 결정합니다. 종합 진행 중인 턴이 있다면 해당 턴을 우선하고, 그렇지 않으면 라운드 또는 최종 종합 개정 이력이 존재하는 가장 최근 턴을 반환합니다."""
    if synthesizing:
        return synthesizing
    for revision in reversed(revisions):
        if revision.get("kind") in _TURN_KINDS and revision.get("turn_id"):
            return revision["turn_id"]
    return None


def trend_points(
    revisions: Sequence[Dict[str, Any]], synthesizing: Optional[str] = None,
) -> Tuple[Optional[str], List[TrendPoint]]:
    """(대상 턴 ID, 추이 포인트 목록)을 산출합니다. [시작값] → [라운드별 최종 개정] → [최종 종합] 순으로 구성됩니다. 동일 라운드 내 복수의 개정이 존재할 경우 가장 마지막 개정을 기준으로 채택합니다."""
    turn = latest_turn(revisions, synthesizing)
    if turn is None:
        return None, []
    first = next(
        (i for i, r in enumerate(revisions) if r.get("turn_id") == turn and r.get("kind") in _TURN_KINDS),
        len(revisions),
    )
    points: List[TrendPoint] = []
    if first > 0:
        points.append(TrendPoint("", _open_count(revisions[first - 1])))

    rounds: Dict[int, Dict[str, Any]] = {}
    labels: Dict[int, str] = {}
    final: Optional[Dict[str, Any]] = None
    for revision in revisions[first:]:
        if revision.get("turn_id") != turn:
            continue
        kind = revision.get("kind")
        number = int(revision.get("round") or 0)
        if kind == ledger_history.KIND_FINAL:
            final = revision
        elif kind in _ROUND_KINDS and number > 0:
            rounds[number] = revision
            if kind == ledger_history.KIND_STEP:
                labels[number] = f"단계 {number}"
            labels.setdefault(number, f"R{number}")
    for number, revision in rounds.items():
        points.append(TrendPoint(labels[number], _open_count(revision), turn, number))
    if final is not None:
        points.append(TrendPoint("합성", _open_count(final), turn, int(final.get("round") or 0)))
    elif synthesizing == turn:
        points.append(TrendPoint("합성", None, turn))
    return turn, points


def trend_text(points: Sequence[TrendPoint], converged: Optional[int] = None) -> str:
    """추이 전체 경로를 단일 문자열로 서식화합니다 (툴팁 표시용이며 생략 없이 전체를 출력합니다)."""
    if not points:
        return ""
    text = "추이 " + " → ".join(point.text for point in points)
    if not points[-1].pending and points[-1].value == 0:
        text += " ✓"
    if converged:
        text += f" 수렴(R{converged})"
    return text


def issue_opened_label(revisions: Sequence[Dict[str, Any]], no: int, current_turn: Optional[str]) -> str:
    """현재 미해결 상태인 쟁점이 최초로 제기되었거나 재개된 시점의 개정 라벨을 반환합니다 (예: "Round 2 (이번 턴)"). 식별 불가 시 빈 문자열을 반환합니다."""
    opened: Optional[Dict[str, Any]] = None
    for revision in reversed(revisions):
        if no not in {item.get("no") for item in revision.get("open") or []}:
            break
        opened = revision
    if opened is None:
        return ""
    if opened.get("kind") == ledger_history.KIND_EDIT:
        return "직접 수정"
    reason = opened.get("reason") or ""
    if opened.get("turn_id") and current_turn:
        reason += " (이번 턴)" if opened["turn_id"] == current_turn else " (이전 턴)"
    return reason.strip()


def _tip(element: ui.element, text: str) -> None:
    """줄바꿈이 보존되는 멀티라인 툴팁을 추가합니다 (`element.tooltip()`은 줄바꿈을 보존하지 않음)."""
    with element:
        ui.tooltip(text).classes("whitespace-pre-line")


class LedgerStrip:
    """상태 영역에 마운트되는 결정 장부 요약 띠 컴포넌트입니다. 개정 이력(`ledger_history`)을 바탕으로 실시간 렌더링됩니다."""

    def __init__(
        self,
        *,
        on_open_ledger: Optional[OpenLedger] = None,
        on_reveal_round: Optional[RevealRound] = None,
    ):
        self.on_open_ledger = on_open_ledger
        self.on_reveal_round = on_reveal_round
        self.row: Optional[ui.row] = None
        self.revisions: List[Dict[str, Any]] = []
        # 개정 이력이 없을 때 직접 파싱할 장부 본문 (레거시 대화).
        self.ledger = ""
        # 현재 종합이 진행 중인 턴 ID. 최종 개정이 수신되면 None으로 리셋됩니다.
        self.synthesizing: Optional[str] = None
        # 턴별 조기 수렴 종료 라운드 번호 매핑.
        self.converged: Dict[str, int] = {}
        # 현재 렌더링된 쟁점 칩 매핑 (쟁점 번호 → UI 레이블 인스턴스). 본문의 `[I-3]` 인용 링크 클릭 시 해당 칩을 강조 표시합니다 (ADR-052).
        self._chips: Dict[int, ui.label] = {}

    def build(self) -> None:
        self.row = ui.row().classes(STRIP_CLASSES)
        self.render()

    # ------------------------------------------------------------ 상태

    def set_revisions(
        self, revisions: Sequence[Dict[str, Any]], ledger: str = "", converged: Optional[Dict[str, int]] = None,
    ) -> None:
        """세션 로드 시 전체 상태를 초기화합니다. 지금까지 누적된 개정 이력, 현재 장부 본문, 턴별 조기 수렴 정보를 설정합니다."""
        self.revisions = [dict(r) for r in revisions]
        self.ledger = ledger or ""
        self.converged = dict(converged or {})
        self.synthesizing = None
        self.render()

    def add_revision(self, revision: Optional[Dict[str, Any]]) -> None:
        """장부 갱신 이벤트를 반영합니다 (`ledger_updated`, 직접 수정 등). 중복 수신된 동일 개정 ID는 무시합니다."""
        if not revision:
            return
        if not any(r.get("id") == revision.get("id") for r in self.revisions):
            self.revisions.append(dict(revision))
        if revision.get("kind") == ledger_history.KIND_FINAL and revision.get("turn_id") == self.synthesizing:
            self.synthesizing = None
        self.render()

    def set_synthesizing(self, turn_id: Optional[str]) -> None:
        """종합 단계가 시작된 턴 ID를 설정합니다. None 전달 시 종합 중 표시 칩을 제거합니다 (최종 갱신 실패 또는 종합 생략 종료)."""
        if turn_id == self.synthesizing:
            return
        self.synthesizing = turn_id
        self.render()

    def set_converged(self, turn_id: Optional[str], round_number: int) -> None:
        if not turn_id or not round_number:
            return
        self.converged[turn_id] = int(round_number)
        self.render()

    def clear(self) -> None:
        self.revisions, self.ledger, self.synthesizing, self.converged = [], "", None, {}
        self.render()

    # ------------------------------------------------------------ 그리기

    @property
    def visible(self) -> bool:
        """신규 대화와 같이 표시할 장부 데이터가 전혀 없는 경우 띠를 숨깁니다."""
        return bool(self.revisions) or bool(self.ledger.strip()) or self.synthesizing is not None

    def render(self) -> None:
        # 피드의 `alive` 속성은 타임라인이 초기화된 후에 True가 되며, 요약 띠는 그보다 먼저 빌드될 수 있습니다. 소멸된 컴포넌트는 `is_deleted` 속성으로 방어합니다.
        if self.row is None or self.row.is_deleted:
            return
        self.row.clear()
        self._chips = {}
        self.row.set_visibility(self.visible)
        if not self.visible:
            return
        counts = strip_counts(self.revisions, self.ledger)
        turn, points = trend_points(self.revisions, self.synthesizing)
        converged = self.converged.get(turn) if turn else None
        trend = trend_text(points, converged)
        numbered = [item for item in counts["open"] if isinstance(item.get("no"), int)]

        with self.row:
            summary = ui.label(
                f"결정 {counts['decided']} · 기각 {counts['rejected']} · 미해결 {len(counts['open'])}"
            ).classes("text-slate-300 font-semibold flex-shrink-0 cursor-pointer")
            _tip(summary, "\n".join(part for part in (trend, "누르면 결정 장부를 엽니다") if part))
            summary.on("click", self._open_ledger)

            for item in numbered[:MAX_ISSUE_CHIPS]:
                self._issue_chip(item, turn)
            rest = numbered[MAX_ISSUE_CHIPS:]
            if rest:
                more = ui.label(f"외 {len(rest)}").classes("text-slate-400 flex-shrink-0 cursor-pointer")
                _tip(more, "\n".join(f"{ledger_history.issue_tag(item['no'])} {item.get('text', '')}" for item in rest))
                more.on("click", self._open_ledger)

            if points:
                self._trend(points, converged)

    def _issue_chip(self, item: Dict[str, Any], turn: Optional[str]) -> None:
        no = item["no"]
        chip = ui.label(ledger_history.issue_label(no)).classes(CHIP_CLASSES)
        chip.props(f'data-mado-issue="{no}"')
        self._chips[no] = chip
        opened = issue_opened_label(self.revisions, no, turn)
        tip = f"{ledger_history.issue_tag(no)} {item.get('text', '')}"
        if opened:
            tip += f"\n열린 시점: {opened}"
        _tip(chip, tip + "\n누르면 결정 장부를 엽니다")
        chip.on("click", self._open_ledger)

    def _trend(self, points: List[TrendPoint], converged: Optional[int]) -> None:
        shown = points[-MAX_TREND_POINTS:]
        with ui.row().classes(TREND_CLASSES):
            ui.label("추이").classes("flex-shrink-0")
            if len(points) > len(shown):
                ui.label("…").classes("flex-shrink-0")
            for index, point in enumerate(shown):
                if index or len(points) > len(shown):
                    ui.label("→").classes("text-slate-600 flex-shrink-0")
                chip = ui.label(point.text).classes(PENDING_POINT_CLASSES if point.pending else POINT_CLASSES)
                if point.pending:
                    chip.tooltip("최종 합성 뒤 장부를 정리하는 중입니다")
                elif not point.label:
                    chip.tooltip("직전 턴이 끝났을 때의 미해결 쟁점 수")
                elif point.round_number and point.turn_id and point.label != "합성" and self.on_reveal_round:
                    chip.classes(add="cursor-pointer hover:bg-slate-700")
                    chip.tooltip(f"{point.label} 묶음으로 이동")
                    chip.on("click", lambda _e, p=point: self._reveal_round(p))
            if not points[-1].pending and points[-1].value == 0:
                ui.label("✓").classes("text-emerald-400 flex-shrink-0").tooltip("미해결 쟁점이 없습니다")
            if converged:
                ui.label(f"수렴(R{converged})").classes("text-emerald-300 flex-shrink-0").tooltip(
                    f"Round {converged} 에서 장부가 수렴해 남은 라운드를 건너뛰었습니다"
                )

    def highlight_issue(self, no: int) -> bool:
        """쟁점 칩을 일시적으로 강조 표시합니다 (본문의 `[I-3]` 링크 클릭 시 호출). 대상 칩이 화면에 없으면(종결된 쟁점 또는 '외 n'에 포함된 경우) False를 반환합니다."""
        chip = self._chips.get(int(no))
        if chip is None or chip.is_deleted:
            return False
        chip.classes(add="ring-2 ring-amber-400")
        ui.timer(2.5, lambda: chip.is_deleted or chip.classes(remove="ring-2 ring-amber-400"), once=True)
        return True

    def _open_ledger(self, _event: Any = None) -> None:
        if self.on_open_ledger is not None:
            self.on_open_ledger()

    def _reveal_round(self, point: TrendPoint) -> None:
        if self.on_reveal_round is not None and point.turn_id:
            self.on_reveal_round(point.turn_id, point.round_number)
