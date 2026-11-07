"""결정 장부의 쟁점 고정 번호와 개정 이력 (ADR-051).

장부는 라운드마다 LLM이 전체를 새로 작성합니다(`context_memory.ledger_prompt`). 이에 따라 기존에는 미해결 쟁점에 고유 식별자(정체성)가
없었습니다. 같은 쟁점이라도 라운드마다 표현이 달라져 서브 토론이나 정체 관찰자가 쟁점을 목록 내 위치(인덱스)나 본문 텍스트로만 식별해야
했습니다. 또한 UI에서도 라운드를 거치며 미해결 쟁점이 어떻게 해소되었는지 변화 추이를 보여줄 근거가 부족했습니다.

## 번호 규칙

* 미해결 쟁점 항목은 `- [I-3] 내용` 형식으로 작성합니다. 번호는 **서버가** 부여합니다. LLM에게는 직전 장부의 `[I-n]` 태그를 그대로
  유지하고, 신규 쟁점은 번호 없이 작성하도록 안내합니다.
* 해결된 쟁점은 목록에서 제거합니다. 목록에서 사라지면 해결(종결)된 것으로 간주하며, 이전 번호가 다시 나타나면 동일한 쟁점이 재개된 것으로 처리합니다.
* 번호를 대조하는 범위는 **세션 전체**입니다(`IssueIndex`). 종결된 쟁점을 번호 없이 동일한 본문으로 다시 작성하더라도 이전 번호가 다시 부여됩니다.
  문구가 변경된 경우에는 장부 프롬프트에 제공되는 '최근 종결된 쟁점' 목록을 바탕으로 번호를 유지합니다.
* 번호는 재사용하지 않습니다. 다음 번호는 개정 이력의 `issue_high`, 현재 장부 본문의 최대 번호, 메모리 내 값 중 최댓값에 1을 더해 결정합니다.
  단, 비정상 종료되거나 폐기된 턴의 번호는 해당 턴의 개정 이력과 함께 삭제되므로 재사용될 수 있습니다.

## 개정 이력

장부가 갱신될 때마다(라운드 종료, 그래프 단계 완료, 서브 토론 결론, 최종 종합, 세션 재개, 발언 재생성, 사용자 직접 편집 등) `ledger_revisions` 테이블에
개정 이력을 기록합니다. 저장에 실패하더라도 토론 진행이 중단되지는 않습니다. 본 저장소의 다른 저장 로직과 마찬가지로 레코드 삽입 즉시 커밋하며, 실패 시 롤백합니다.
오케스트레이션 엔진의 모든 저장은 즉시 커밋을 수행하므로, 롤백 시 해당 개정 이력 행 하나만 취소됩니다.

이 모듈 전반부는 순수 함수로 구성되어 있으며, 데이터베이스와 직접 상호작용하는 함수는 `load_revisions`, `load_index`, `record_revision`뿐입니다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, insert, select

from app.database.models import LedgerRevisionModel, new_id, utc_now
from app.events import EventCallback, EventType
from app.orchestration import context_memory as memory

logger = logging.getLogger(__name__)

OPEN_ISSUES = "미해결 쟁점"
DECIDED = "결정 사항"
REJECTED = "기각된 대안"

# 개정 종류 (`LedgerRevisionModel.kind`).
KIND_ROUND = "round"          # 라운드 종료 시점 (`debate._close_round`)
KIND_STEP = "step"            # 그래프 단계 완료 시점 (`graph.after_graph_step`)
KIND_SUBDEBATE = "subdebate"  # 서브 토론 결론 도출 후
KIND_FINAL = "final"          # 최종 종합 후
KIND_RESUME = "resume"        # 중단된 턴을 재개할 때
KIND_RETRY = "retry"          # 특정 발언을 재생성하거나 되돌린 후 (`retry._correct_ledger`)
KIND_EDIT = "edit"            # 사용자가 장부를 직접 수정한 경우 (`ledger_edit.save_edit`)
REVISION_KINDS = (KIND_ROUND, KIND_STEP, KIND_SUBDEBATE, KIND_FINAL, KIND_RESUME, KIND_RETRY, KIND_EDIT)

# 장부 프롬프트에 포함할 최근 종결된 쟁점 최대 개수 및 한 줄당 글자 수 제한.
RECENTLY_CLOSED_LIMIT = 5
RECENTLY_CLOSED_CHARS = 80

# ---------------------------------------------------------------- 쟁점 태그

ISSUE_TAG = re.compile(r"^\[I-(\d{1,5})\]\s*")
_ANY_ISSUE_TAG = re.compile(r"\[I-\d{1,5}\]\s*")
_WS_RE = re.compile(r"\s+")


def issue_label(no: int) -> str:
    """`I-3`."""
    return f"I-{no}"


def issue_tag(no: int) -> str:
    """`[I-3]`."""
    return f"[{issue_label(no)}]"


def split_issue_tag(text: str) -> Tuple[Optional[int], str]:
    """항목 앞의 `[I-n]` 태그를 분리하여 (번호, 본문 텍스트) 튜플로 반환합니다. 태그가 없으면 (None, 본문 텍스트)를 반환합니다."""
    text = (text or "").strip()
    match = ISSUE_TAG.match(text)
    if match is None:
        return None, text
    return int(match.group(1)), text[match.end():].strip()


def strip_issue_tags(text: str) -> str:
    """문자열 내의 모든 `[I-n]` 태그를 제거합니다."""
    return _WS_RE.sub(" ", _ANY_ISSUE_TAG.sub("", text or "")).strip()


def parse_issue_label(value: Any) -> Optional[int]:
    """`"I-3"`, `"[I-3]"`, `3` 등의 입력을 정수 번호로 파싱합니다. 식별할 수 없으면 None을 반환합니다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    match = re.fullmatch(r"\s*\[?\s*I\s*-\s*(\d{1,5})\s*\]?\s*", str(value or ""), re.IGNORECASE)
    if match is None:
        return None
    no = int(match.group(1))
    return no if no > 0 else None


def normalize_issue(issue: str) -> str:
    """쟁점 텍스트를 비교에 적합하도록 정규화합니다. 태그, 괄호 안 설명, 문장부호를 제거하고 소문자로 변환하여 앞 80자까지 반환합니다.

    동일한 쟁점이 번호 없이 다시 작성된 경우를 식별(`assign_issue_ids`)하거나 정체 관찰자의 상태 서명(`diagnostic_loop`)을 생성할 때 사용합니다.
    """
    text = re.sub(r"\([^)]*\)", "", strip_issue_tags(issue))
    text = re.sub(r"[^\w가-힣./_-]+", " ", text.lower())
    return _WS_RE.sub(" ", text).strip()[:80]


# ---------------------------------------------------------------- 쟁점 색인


@dataclass
class IssueIndex:
    """세션 내에서 지금까지 등장한 쟁점들을 추적하는 색인 구조체.

    `texts`는 쟁점 번호별 최신 본문, `by_text`는 정규화된 텍스트별 가장 최근 번호, `last_open`은 쟁점 번호별 마지막으로 미해결 상태였던
    개정 순번(`seq`)을 매핑합니다. `high`는 지금까지 발급된 가장 높은 쟁점 번호입니다.
    """

    texts: Dict[int, str] = field(default_factory=dict)
    by_text: Dict[str, int] = field(default_factory=dict)
    last_open: Dict[int, int] = field(default_factory=dict)
    high: int = 0
    seq: int = 0

    def _remember(self, no: int, text: str) -> None:
        self.texts[no] = text
        norm = normalize_issue(text)
        if norm:
            self.by_text[norm] = no
        self.last_open[no] = self.seq
        self.high = max(self.high, no)

    def observe(self, open_issues: Iterable[Dict[str, Any]], high: int = 0) -> None:
        """단일 개정 이력의 미해결 쟁점 목록을 색인에 반영합니다 (DB에서 조회한 항목 또는 방금 번호를 부여한 항목)."""
        self.seq += 1
        for item in open_issues or ():
            no = item.get("no") if isinstance(item, dict) else None
            if isinstance(no, int) and no > 0:
                self._remember(no, str(item.get("text") or ""))
        self.high = max(self.high, int(high or 0))

    def absorb(self, ledger: str) -> None:
        """장부 본문에 포함된 태그들을 색인에 반영합니다. 개정 이력 없이 텍스트만 전달된 세션(`continue_session`)이나 저장이 누락된 개정을 보완합니다.

        이미 등록된 번호의 텍스트는 덮어쓰지 않습니다. 개정 이력의 기록이 더 정확하기 때문입니다.
        """
        for no, text in tagged_open_issues(ledger):
            if no not in self.texts:
                self._remember(no, text)
            self.high = max(self.high, no)

    def recently_closed(self, open_now: Iterable[int], limit: int = RECENTLY_CLOSED_LIMIT) -> List[Tuple[int, str]]:
        """현재 미해결 상태가 아닌 종결된 쟁점들을 최근 종결된 순서대로 최대 `limit`개까지 반환합니다."""
        current = set(open_now)
        closed = [no for no in self.texts if no not in current]
        closed.sort(key=lambda no: (self.last_open.get(no, 0), no), reverse=True)
        return [(no, self.texts[no]) for no in closed[:limit]]


def _section_bounds(lines: Sequence[str], title: str) -> Optional[Tuple[int, int]]:
    """`## title` 섹션의 (시작 줄 번호, 종료 줄 번호) 범위를 반환합니다. 섹션이 없으면 None을 반환하며, `ledger_items`와 동일하게 첫 번째로 매칭된 섹션만 탐색합니다."""
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            if start is not None:
                return start, index
            if line[3:].strip() == title:
                start = index + 1
    return (start, len(lines)) if start is not None else None


def _is_item(line: str) -> bool:
    return line.startswith(("- ", "* "))


def _skippable(text: str) -> bool:
    """빈 항목 표시(`- 없음`)나 장부 축약 시 삽입되는 생략 표시 줄은 쟁점 파싱 대상에서 제외합니다."""
    return not text or text.lower() in memory.EMPTY_ITEMS or text == memory.LEDGER_CLIP_NOTE


def tagged_open_issues(ledger: str) -> List[Tuple[int, str]]:
    """미해결 쟁점 중 식별 태그가 부여된 항목들의 (번호, 본문 텍스트) 튜플 목록을 반환합니다."""
    out: List[Tuple[int, str]] = []
    for item in memory.ledger_items(ledger, OPEN_ISSUES) or []:
        no, text = split_issue_tag(item)
        if no is not None:
            out.append((no, text))
    return out


def assign_issue_ids(ledger: str, index: IssueIndex) -> Tuple[str, List[Dict[str, Any]]]:
    """미해결 쟁점에 고유 번호를 부여한 장부 본문과 미해결 쟁점 목록(`[{"no", "text"}]`)을 반환합니다. 전달받은 `index`를 인플레이스로 갱신합니다.

    각 항목의 번호는 다음 순서에 따라 결정됩니다:

    1. 유효한 태그가 존재하고 그 번호가 기존 최대 번호(`high`) 이하이며, 현재 장부에서 아직 사용되지 않았다면 기존 번호를 유지합니다 (재개된 쟁점 포함).
    2. 정규화된 텍스트가 색인 내 기존 쟁점과 일치하면 해당 번호를 재사용합니다 (종결된 쟁점 포함). 단, 현재 장부에서 이미 사용된 번호라면 3번으로 넘어갑니다.
    3. 위 조건에 해당하지 않으면 신규 번호를 순차적으로 발급합니다.

    미해결 쟁점 외 다른 섹션(예: 결정 사항으로 이전된 항목 등)의 항목 머리에 붙은 태그는 제거됩니다. 고유 번호는 미해결 쟁점 섹션에만 유지됩니다.
    """
    lines = (ledger or "").splitlines()
    bounds = _section_bounds(lines, OPEN_ISSUES)
    used: set = set()
    open_issues: List[Dict[str, Any]] = []
    for index_, line in enumerate(lines):
        if not _is_item(line):
            continue
        inside = bounds is not None and bounds[0] <= index_ < bounds[1]
        marker, body = line[:2], line[2:].strip()
        if not inside:
            if ISSUE_TAG.match(body):
                lines[index_] = f"{marker}{split_issue_tag(body)[1]}"
            continue
        if _skippable(body):
            continue
        no, text = split_issue_tag(body)
        if _skippable(text):
            continue
        if no is None or not (1 <= no <= index.high) or no in used:
            no = None
            known = index.by_text.get(normalize_issue(text))
            if known is not None and known not in used:
                no = known
        if no is None:
            index.high += 1
            no = index.high
        used.add(no)
        lines[index_] = f"{marker}{issue_tag(no)} {text}"
        open_issues.append({"no": no, "text": text})
    index.observe(open_issues)
    return "\n".join(lines), open_issues


def summarize(ledger: str) -> Dict[str, Any]:
    """장부의 현황을 집계하여 `{"open": [{"no", "text"}], "decided": n, "rejected": n}` 형태로 반환합니다. 태그가 없는 기존 쟁점은 `no`가 None으로 설정됩니다."""
    open_items = []
    for item in memory.ledger_items(ledger, OPEN_ISSUES) or []:
        no, text = split_issue_tag(item)
        open_items.append({"no": no, "text": text})
    return {
        "open": open_items,
        "decided": len(memory.ledger_items(ledger, DECIDED) or []),
        "rejected": len(memory.ledger_items(ledger, REJECTED) or []),
    }


def recently_closed_block(index: IssueIndex, ledger: str) -> str:
    """장부 작성 프롬프트에 삽입될 '최근 종결된 쟁점' 텍스트 블록을 생성합니다. 대상이 없으면 빈 문자열을 반환합니다."""
    open_now = {no for no, _text in tagged_open_issues(ledger)}
    closed = index.recently_closed(open_now)
    if not closed:
        return ""
    lines = []
    for no, text in closed:
        short = text if len(text) <= RECENTLY_CLOSED_CHARS else text[:RECENTLY_CLOSED_CHARS - 1] + "…"
        lines.append(f"- {issue_tag(no)} {short}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 라운드별 변화량


def _open_nos(revision: Optional[Dict[str, Any]]) -> set:
    if not revision:
        return set()
    return {item.get("no") for item in revision.get("open") or [] if isinstance(item.get("no"), int)}


def revision_delta(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> Dict[str, Any]:
    """두 개정 이력 사이의 상태 변화를 계산합니다. `before`가 없으면 초기 상태(빈 장부)에서 시작한 것으로 간주합니다."""
    old, new = _open_nos(before), _open_nos(after)
    return {
        "decided": int(after.get("decided") or 0) - int((before or {}).get("decided") or 0),
        "rejected": int(after.get("rejected") or 0) - int((before or {}).get("rejected") or 0),
        "closed": sorted(old - new),
        "opened": sorted(new - old),
        "open": len(after.get("open") or []),
        "final": after.get("kind") == KIND_FINAL,
        "revision_id": after.get("id"),
    }


def round_deltas(revisions: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """(턴 ID, 라운드 번호)별로 해당 라운드의 최종 개정과 직전 개정을 비교한 상태 변화량을 산출합니다.

    `revisions`는 시간순으로 정렬된 개정 딕셔너리 목록입니다(`load_revisions` 및 `LEDGER_UPDATED` 이벤트의 `revision`). 발언 재생성(`retry`)이나 턴 외부에서의
    직접 수정(`edit`)은 라운드 단위 작업이 아니므로 별도 묶음 카드를 생성하지 않으나, 장부 변경 사항은 다음 라운드의 비교 기준(baseline)으로 반영됩니다. 동일 라운드 내에
    복수의 개정이 존재하는 경우(라운드 종료 + 서브 토론 결론, 조기 수렴 + 최종 종합 등)에는 가장 마지막 개정을 기준으로 집계합니다.
    """
    base: Dict[Tuple[str, int], Optional[Dict[str, Any]]] = {}
    last: Dict[Tuple[str, int], Dict[str, Any]] = {}
    previous: Optional[Dict[str, Any]] = None
    for revision in revisions:
        kind = revision.get("kind")
        turn_id = revision.get("turn_id")
        round_number = int(revision.get("round") or 0)
        if kind not in (KIND_RETRY, KIND_EDIT) and turn_id and round_number > 0:
            key = (turn_id, round_number)
            if key not in base:
                base[key] = previous
            last[key] = revision
        previous = revision
    return {key: revision_delta(base[key], last[key]) for key in last}


# ---------------------------------------------------------------- DB


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def revision_payload(
    *,
    revision_id: str,
    turn_id: Optional[str],
    round_number: int,
    kind: str,
    reason: str,
    open_issues: Sequence[Dict[str, Any]],
    decided: int,
    rejected: int,
    created_at: Any,
    seq: int = 0,
    opened: Sequence[int] = (),
    closed: Sequence[int] = (),
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """UI 및 이벤트 통신에 사용되는 개정 데이터 딕셔너리를 생성합니다."""
    return {
        "id": revision_id,
        "turn_id": turn_id,
        "round": int(round_number or 0),
        "kind": kind,
        "reason": reason,
        "open": [dict(item) for item in open_issues],
        "opened": list(opened),
        "closed": list(closed),
        "decided": int(decided or 0),
        "rejected": int(rejected or 0),
        "created_at": _iso(created_at),
        "seq": int(seq or 0),
        "message_id": message_id,
    }


def _row_payload(row: LedgerRevisionModel) -> Dict[str, Any]:
    return revision_payload(
        revision_id=row.id, turn_id=row.turn_id, round_number=row.round_number, kind=row.kind,
        reason=row.reason or "", open_issues=row.open_issues or [], decided=row.decided, rejected=row.rejected,
        created_at=row.created_at, seq=row.seq, message_id=row.message_id,
    )


async def load_revisions(db, session_id: str) -> List[Dict[str, Any]]:
    """세션의 전체 개정 이력을 순서대로 조회합니다. `opened` 및 `closed` 필드는 직전 개정과 비교하여 산출합니다."""
    rows = (await db.execute(
        select(LedgerRevisionModel)
        .where(LedgerRevisionModel.session_id == session_id)
        .order_by(LedgerRevisionModel.seq, LedgerRevisionModel.created_at)
    )).scalars().all()
    out: List[Dict[str, Any]] = []
    previous: Optional[Dict[str, Any]] = None
    for row in rows:
        payload = _row_payload(row)
        delta = revision_delta(previous, payload)
        payload["opened"], payload["closed"] = delta["opened"], delta["closed"]
        out.append(payload)
        previous = payload
    return out


async def load_index(db, session_id: str) -> IssueIndex:
    """세션의 기존 개정 이력을 바탕으로 쟁점 색인(`IssueIndex`)을 구축합니다. 조회가 실패할 경우 빈 색인을 반환하며, 번호는 장부 본문에서 이어 부여됩니다.

    조회 완료 후 즉시 트랜잭션을 커밋·종료하여 커넥션을 풀에 반환합니다 (ADR-041). 호출처(`update_ledger`)가 이후 장시간 소요되는 LLM 호출을 대기하기 때문입니다.
    호출 시점에 커밋되지 않은 변경 사항이 없으므로 이 커밋은 실제 쓰기 동작 없이 트랜잭션을 안전하게 마무리합니다.
    """
    index = IssueIndex()
    try:
        rows = (await db.execute(
            select(LedgerRevisionModel.open_issues, LedgerRevisionModel.issue_high)
            .where(LedgerRevisionModel.session_id == session_id)
            .order_by(LedgerRevisionModel.seq, LedgerRevisionModel.created_at)
        )).all()
        await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 색인 조회가 실패하더라도 장부 갱신은 계속 진행합니다
        logger.warning(f"Could not read the ledger revisions of session {session_id}: {type(exc).__name__}: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("Rollback after a failed revision read also failed", exc_info=True)
        return index
    for open_issues, high in rows:
        index.observe(open_issues or [], high or 0)
    return index


async def record_revision(
    db,
    *,
    session_id: str,
    turn_id: Optional[str],
    round_number: int,
    kind: str,
    reason: str,
    ledger: str,
    open_issues: Sequence[Dict[str, Any]],
    issue_high: int,
    previous_ledger: str = "",
    message_id: Optional[str] = None,
    on_event: Optional[EventCallback] = None,
) -> Dict[str, Any]:
    """개정 이력 1건을 데이터베이스에 삽입하고 즉시 커밋합니다. 반환값은 이벤트 브로드캐스트에 사용될 개정 페이로드이며, DB 저장이 실패하더라도 반환됩니다.

    모든 식별자 및 타임스탬프(`id`, `created_at`, `seq`)는 애플리케이션 레벨에서 직접 확정하며, 커밋 후 행을 다시 조회하지 않습니다. 저장 실패로 롤백될 때
    세션 내 ORM 인스턴스가 만료(expire)되더라도, 비동기 컨텍스트에서 만료된 인스턴스를 재조회하려다 발생하는 예외를 방지하기 위함입니다.
    """
    counts = summarize(ledger)
    before = {item["no"] for item in summarize(previous_ledger)["open"] if item["no"] is not None}
    now = {item["no"] for item in open_issues}
    revision_id = new_id()
    created_at = utc_now()
    payload = revision_payload(
        revision_id=revision_id, turn_id=turn_id, round_number=round_number, kind=kind, reason=reason,
        open_issues=open_issues, decided=counts["decided"], rejected=counts["rejected"], created_at=created_at,
        opened=sorted(now - before), closed=sorted(before - now), message_id=message_id,
    )
    try:
        seq = int(await db.scalar(
            select(func.coalesce(func.max(LedgerRevisionModel.seq), 0))
            .where(LedgerRevisionModel.session_id == session_id)
        ) or 0) + 1
        payload["seq"] = seq
        await db.execute(insert(LedgerRevisionModel).values(
            id=revision_id, session_id=session_id, turn_id=turn_id, seq=seq,
            round_number=int(round_number or 0), kind=kind, reason=(reason or "")[:120],
            ledger=ledger, open_issues=[dict(item) for item in open_issues],
            decided=counts["decided"], rejected=counts["rejected"], issue_high=int(issue_high or 0),
            message_id=message_id, created_at=created_at,
        ))
        await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 개정 이력 기록에 실패하더라도 토론 진행은 중단하지 않습니다
        logger.error(
            f"Could not record a ledger revision for session {session_id}: {type(exc).__name__}: {exc}",
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("Rollback after a failed ledger revision also failed", exc_info=True)
        if on_event:
            await on_event({
                "type": EventType.PERSIST_FAILED,
                "what": "a ledger revision",
                "label": "결정 장부 개정 이력",
                "error": f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                "saved_to": None,
            })
    return payload
