"""결정 장부 요약 띠 — 상태 줄 아래 한 줄 (전문가 화면, docs/feature_ui_upgrade.md P2).

결정 장부는 로스터 맨 아래의 접힌 칸에만 있어 토론을 지켜보는 동안 보이지 않았고, 미해결 쟁점이 라운드마다 어떻게
줄었는지는 어디에도 없었습니다. 이 띠는 스크롤과 상관없이 늘 보이는 상태 칸에 한 줄로 놓입니다.

    결정 5 · 기각 2 · 미해결 2  I-3 I-5        추이 3 → R1 3 → R2 2 → 합성 0 ✓

* **한 줄, 고정 높이.** 쟁점이 생기고 닫힐 때마다 높이가 바뀌면 스트리밍 중에 피드 스크롤 영역이 라운드마다
  들썩입니다. 좁으면 개수(항상) → 쟁점 칩(최대 4개) → 추이 순으로 남기고, 추이는 띠 폭에 대한 CSS 컨테이너 질의로
  숨깁니다(`theme.py` 의 `.ledger-strip`). 화면 폭이 아니라 스플리터로 좁아진 피드 폭을 따라야 하기 때문입니다.
  숨긴 추이는 개수 쪽 툴팁에 있습니다.
* **추이는 가장 최근 턴만.** 시작값은 직전 턴의 마지막 개정입니다. 마지막 라운드는 장부를 따로 갱신하지 않으므로
  (`debate._close_round`) 마지막 값은 "합성" 입니다. 합성 중에는 흐린 `합성 …` 을 두었다가 최종 개정이 오면 값으로
  바꾸고, 그 값 없이 끝나면 거둡니다.
* 개정 이력이 없는 옛 대화는 장부 글에서 센 개수만 보여 줍니다.

계산은 순수 함수(`strip_counts`, `trend_points`, `issue_opened_label`)이고, 화면은 그 결과를 그리기만 합니다.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from nicegui import ui

from app.orchestration import ledger_history

# 쟁점 칩의 최대 개수. 넘치면 "외 n" 으로 줄이고 툴팁에 나머지를 적습니다.
MAX_ISSUE_CHIPS = 4
# 추이에 보이는 값의 최대 개수. 앞은 "…" 으로 줄입니다.
MAX_TREND_POINTS = 5

STRIP_CLASSES = (
    "ledger-strip w-full h-7 items-center gap-2 px-3 rounded-lg text-[11px] flex-nowrap overflow-hidden "
    "min-w-0 whitespace-nowrap bg-slate-900/70 border border-slate-800"
)
TREND_CLASSES = "ledger-strip-trend items-center gap-1 flex-nowrap min-w-0 overflow-hidden ml-auto text-slate-400"
CHIP_CLASSES = "px-1.5 rounded bg-amber-900/40 text-amber-200 font-mono flex-shrink-0 cursor-pointer"
POINT_CLASSES = "px-1 rounded bg-slate-800 text-slate-300 flex-shrink-0"
PENDING_POINT_CLASSES = "px-1 rounded bg-slate-800 text-slate-500 opacity-60 flex-shrink-0"

# 한 턴의 라운드를 이루는 개정 (재실행·직접 수정은 라운드가 아닙니다).
_ROUND_KINDS = (
    ledger_history.KIND_ROUND, ledger_history.KIND_STEP, ledger_history.KIND_SUBDEBATE, ledger_history.KIND_RESUME,
)
_TURN_KINDS = _ROUND_KINDS + (ledger_history.KIND_FINAL,)

# 장부 열기 콜백. 로스터를 아는 페이지가 넘깁니다 (chat_feed 는 roster 를 모릅니다).
OpenLedger = Callable[[], None]
# 라운드 칩을 눌렀을 때 (턴 id, 라운드). 라운드 묶음(P3)이 그 묶음으로 이동합니다.
RevealRound = Callable[[str, int], Any]


@dataclass
class TrendPoint:
    """추이의 값 하나. `label` 이 비면 시작값(직전 턴의 마지막 개정)이고, `value` 가 None 이면 합성 중입니다."""

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
    """띠의 개수와 열린 쟁점. 개정이 있으면 마지막 개정, 없으면 장부 글에서 셉니다 (옛 대화)."""
    if revisions:
        last = revisions[-1]
        return {
            "decided": int(last.get("decided") or 0), "rejected": int(last.get("rejected") or 0),
            "open": [dict(item) for item in last.get("open") or []],
        }
    return ledger_history.summarize(ledger or "")


def latest_turn(revisions: Sequence[Dict[str, Any]], synthesizing: Optional[str] = None) -> Optional[str]:
    """추이를 보여 줄 턴. 합성 중인 턴이 있으면 그 턴, 아니면 라운드·합성 개정이 있는 가장 최근 턴."""
    if synthesizing:
        return synthesizing
    for revision in reversed(revisions):
        if revision.get("kind") in _TURN_KINDS and revision.get("turn_id"):
            return revision["turn_id"]
    return None


def trend_points(
    revisions: Sequence[Dict[str, Any]], synthesizing: Optional[str] = None,
) -> Tuple[Optional[str], List[TrendPoint]]:
    """(그 턴, 추이). 시작값 → 라운드마다 마지막 개정 → 합성. 같은 라운드의 개정이 여럿이면 마지막 것을 씁니다."""
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
    """추이를 한 줄 글로 (툴팁용, 줄이지 않습니다)."""
    if not points:
        return ""
    text = "추이 " + " → ".join(point.text for point in points)
    if not points[-1].pending and points[-1].value == 0:
        text += " ✓"
    if converged:
        text += f" 수렴(R{converged})"
    return text


def issue_opened_label(revisions: Sequence[Dict[str, Any]], no: int, current_turn: Optional[str]) -> str:
    """쟁점이 지금 열려 있는 구간이 시작된 개정. 예: "Round 2 (이번 턴)". 모르면 빈 문자열."""
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
    """여러 줄 툴팁. `element.tooltip()` 은 줄바꿈을 지키지 않습니다."""
    with element:
        ui.tooltip(text).classes("whitespace-pre-line")


class LedgerStrip:
    """상태 칸의 결정 장부 요약 한 줄. 개정 이력(`ledger_history`)으로 그립니다."""

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
        # 개정 이력이 없을 때 셀 장부 글 (옛 대화).
        self.ledger = ""
        # 합성 중인 턴. 그 턴의 최종 개정이 오면 비웁니다.
        self.synthesizing: Optional[str] = None
        # 턴마다 합의 수렴으로 조기 종료한 라운드.
        self.converged: Dict[str, int] = {}
        # 지금 그려 둔 쟁점 칩 (번호 → 칩). 본문의 `[I-3]` 링크가 칩을 강조합니다 (ADR-052).
        self._chips: Dict[int, ui.label] = {}

    def build(self) -> None:
        self.row = ui.row().classes(STRIP_CLASSES)
        self.render()

    # ------------------------------------------------------------ 상태

    def set_revisions(
        self, revisions: Sequence[Dict[str, Any]], ledger: str = "", converged: Optional[Dict[str, int]] = None,
    ) -> None:
        """대화를 열 때. 지금까지의 개정 이력, 지금 장부, 턴별 수렴 라운드."""
        self.revisions = [dict(r) for r in revisions]
        self.ledger = ledger or ""
        self.converged = dict(converged or {})
        self.synthesizing = None
        self.render()

    def add_revision(self, revision: Optional[Dict[str, Any]]) -> None:
        """장부가 바뀌었습니다 (`ledger_updated`, 직접 수정). 같은 개정이 두 번 와도 한 번만 셉니다."""
        if not revision:
            return
        if not any(r.get("id") == revision.get("id") for r in self.revisions):
            self.revisions.append(dict(revision))
        if revision.get("kind") == ledger_history.KIND_FINAL and revision.get("turn_id") == self.synthesizing:
            self.synthesizing = None
        self.render()

    def set_synthesizing(self, turn_id: Optional[str]) -> None:
        """합성을 시작한 턴. None 이면 합성 중 칩을 거둡니다 (최종 갱신 실패, 그 값 없이 끝난 턴)."""
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
        """새 대화처럼 장부가 비어 있으면 숨깁니다."""
        return bool(self.revisions) or bool(self.ledger.strip()) or self.synthesizing is not None

    def render(self) -> None:
        # 피드의 `alive` 는 타임라인을 그린 뒤에야 참이고 띠는 그보다 먼저 그립니다. 버려진 화면은 `is_deleted` 로 압니다.
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
        """쟁점 칩을 잠깐 강조합니다 (본문의 `[I-3]` 링크). 칩이 없으면(닫힌 쟁점, '외 n' 에 든 쟁점) False."""
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
