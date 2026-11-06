"""결정 장부의 쟁점 고정 번호와 개정 이력 (ADR-051).

장부는 LLM 이 라운드마다 통째로 다시 씁니다(`context_memory.ledger_prompt`). 그래서 예전에는 미해결 쟁점에 정체성이
없었습니다. 같은 쟁점이 라운드마다 다른 문장으로 적히고, 서브 토론과 정체 관찰자는 쟁점을 목록 위치나 글로 찾아야
했습니다. 화면도 "미해결 쟁점이 라운드마다 어떻게 줄었는지" 를 보여 줄 근거가 없었습니다.

## 번호 규칙

* 미해결 쟁점 항목은 `- [I-3] 내용` 으로 씁니다. 번호는 **서버가** 붙입니다. LLM 에게는 이전 장부의 `[I-n]` 을 그대로
  두고 새 쟁점은 번호 없이 쓰라고만 지시합니다.
* 해결된 쟁점은 목록에서 지웁니다. 사라지면 닫힌 것이고, 옛 번호를 다시 쓰면 같은 쟁점이 다시 열린 것입니다.
* 번호를 대조하는 범위는 **세션 전체**입니다(`IssueIndex`). 닫힌 쟁점을 번호 없이 같은 글로 다시 쓰면 옛 번호를 받습니다.
  글을 바꿔 쓴 경우는 장부 프롬프트에 보여 주는 "최근에 닫힌 쟁점" 으로만 잡힙니다.
* 번호는 재사용하지 않습니다. 다음 번호는 개정 이력의 `issue_high`, 지금 장부 글의 최대 번호, 메모리의 값 중 가장 큰
  것에 1 을 더한 값입니다. 긴급 종료나 버린 턴의 번호는 그 턴의 개정 이력과 함께 지워지므로 다시 쓰여도 됩니다.

## 개정 이력

장부를 바꿀 때마다(라운드·그래프 단계·서브 토론·최종 합성·재개·발언 교체·직접 수정) `ledger_revisions` 에 한 행을
남깁니다. 저장에 실패해도 토론은 멈추지 않습니다. 이 저장소의 다른 쓰기와 같이 넣은 즉시 커밋하고, 실패하면 롤백합니다.
엔진의 모든 쓰기가 그 자리에서 커밋하므로 롤백은 이 행 하나만 버립니다.

이 모듈의 앞부분은 순수 함수이고, DB 를 만지는 것은 `load_revisions`·`load_index`·`record_revision` 뿐입니다.
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
KIND_ROUND = "round"          # 라운드 끝 (`debate._close_round`)
KIND_STEP = "step"            # 그래프 단계 끝 (`graph.after_graph_step`)
KIND_SUBDEBATE = "subdebate"  # 서브 토론 결론 뒤
KIND_FINAL = "final"          # 최종 합성 뒤
KIND_RESUME = "resume"        # 끊긴 턴을 이어 갈 때
KIND_RETRY = "retry"          # 발언 하나를 다시 실행하거나 되돌린 뒤 (`retry._correct_ledger`)
KIND_EDIT = "edit"            # 사람이 장부를 직접 고침 (`ledger_edit.save_edit`)
REVISION_KINDS = (KIND_ROUND, KIND_STEP, KIND_SUBDEBATE, KIND_FINAL, KIND_RESUME, KIND_RETRY, KIND_EDIT)

# 장부 프롬프트에 보여 줄 최근에 닫힌 쟁점의 수와 한 줄의 글자 상한.
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
    """항목 앞의 `[I-n]` 을 떼어 (번호, 나머지 글). 태그가 없으면 (None, 글)."""
    text = (text or "").strip()
    match = ISSUE_TAG.match(text)
    if match is None:
        return None, text
    return int(match.group(1)), text[match.end():].strip()


def strip_issue_tags(text: str) -> str:
    """글 안의 `[I-n]` 태그를 모두 뗍니다."""
    return _WS_RE.sub(" ", _ANY_ISSUE_TAG.sub("", text or "")).strip()


def parse_issue_label(value: Any) -> Optional[int]:
    """`"I-3"`·`"[I-3]"`·`3` 을 번호로. 알아볼 수 없으면 None."""
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
    """쟁점 글을 비교용으로 다듬습니다. 태그·괄호 속 설명·문장부호를 빼고 소문자로, 앞 80자.

    같은 쟁점을 번호 없이 다시 쓴 경우를 알아보는 데(`assign_issue_ids`)와 정체 관찰자의 서명(`diagnostic_loop`)에 씁니다.
    """
    text = re.sub(r"\([^)]*\)", "", strip_issue_tags(issue))
    text = re.sub(r"[^\w가-힣./_-]+", " ", text.lower())
    return _WS_RE.sub(" ", text).strip()[:80]


# ---------------------------------------------------------------- 쟁점 색인


@dataclass
class IssueIndex:
    """세션에서 지금까지 나온 쟁점의 색인.

    `texts` 는 번호마다 마지막 글, `by_text` 는 정규화한 글마다 가장 최근 번호, `last_open` 은 쟁점마다 마지막으로 열려 있던
    개정의 순번(`seq`)입니다. `high` 는 지금까지 쓴 가장 큰 번호입니다.
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
        """개정 하나의 열린 쟁점을 더합니다 (DB 에서 읽은 것이든 방금 번호를 붙인 것이든)."""
        self.seq += 1
        for item in open_issues or ():
            no = item.get("no") if isinstance(item, dict) else None
            if isinstance(no, int) and no > 0:
                self._remember(no, str(item.get("text") or ""))
        self.high = max(self.high, int(high or 0))

    def absorb(self, ledger: str) -> None:
        """장부 글에 적힌 태그를 더합니다. 개정 이력 없이 글만 넘어온 장부(`continue_session`)나 저장에 실패한 개정을 덮습니다.

        이미 아는 번호의 글은 바꾸지 않습니다. 개정 이력이 더 정확합니다.
        """
        for no, text in tagged_open_issues(ledger):
            if no not in self.texts:
                self._remember(no, text)
            self.high = max(self.high, no)

    def recently_closed(self, open_now: Iterable[int], limit: int = RECENTLY_CLOSED_LIMIT) -> List[Tuple[int, str]]:
        """지금 열려 있지 않은 쟁점을 최근에 열려 있던 순으로 `limit` 개까지."""
        current = set(open_now)
        closed = [no for no in self.texts if no not in current]
        closed.sort(key=lambda no: (self.last_open.get(no, 0), no), reverse=True)
        return [(no, self.texts[no]) for no in closed[:limit]]


def _section_bounds(lines: Sequence[str], title: str) -> Optional[Tuple[int, int]]:
    """`## title` 칸의 (첫 줄, 끝 줄 다음) 자리. 없으면 None. `ledger_items` 와 같이 처음 나온 칸만 봅니다."""
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
    """빈 표시(`- 없음`)와 장부를 자를 때 붙는 생략 표시 줄은 쟁점이 아닙니다."""
    return not text or text.lower() in memory.EMPTY_ITEMS or text == memory.LEDGER_CLIP_NOTE


def tagged_open_issues(ledger: str) -> List[Tuple[int, str]]:
    """미해결 쟁점 중 태그가 붙은 것의 (번호, 글)."""
    out: List[Tuple[int, str]] = []
    for item in memory.ledger_items(ledger, OPEN_ISSUES) or []:
        no, text = split_issue_tag(item)
        if no is not None:
            out.append((no, text))
    return out


def assign_issue_ids(ledger: str, index: IssueIndex) -> Tuple[str, List[Dict[str, Any]]]:
    """미해결 쟁점에 번호를 붙인 장부와 열린 쟁점 목록 `[{"no", "text"}]`. `index` 를 그 자리에서 갱신합니다.

    항목마다 다음 순서로 번호를 정합니다.

    1. 태그가 있고 그 번호가 `high` 이하이며 이번 장부에서 아직 쓰이지 않았으면 그대로 둡니다(다시 연 쟁점도 이 경우).
    2. 정규화한 글이 색인의 쟁점과 같으면 그 번호를 다시 씁니다. 닫힌 쟁점도 대상입니다. 이미 이번 장부에서 쓰였으면 3.
    3. 둘 다 아니면 새 번호를 줍니다.

    다른 칸의 항목 앞에 붙은 태그는 뗍니다. 번호는 미해결 쟁점에만 붙습니다(결정 사항으로 옮겨 간 쟁점 등).
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
    """`{"open": [{"no", "text"}], "decided": n, "rejected": n}`. 태그가 없는 옛 쟁점은 `no` 가 None 입니다."""
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
    """장부 프롬프트의 "최근에 닫힌 쟁점" 칸. 없으면 빈 문자열."""
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
    """두 개정 사이의 변화. `before` 가 없으면 빈 장부에서 시작한 것으로 봅니다."""
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
    """(턴, 라운드)마다 그 라운드의 마지막 개정을 라운드 직전의 개정과 비교한 변화량.

    `revisions` 는 순서대로 놓인 개정 dict 입니다(`load_revisions`·`LEDGER_UPDATED` 의 `revision`). 발언 교체(`retry`)와 턴 밖의
    직접 수정(`edit`)은 라운드가 아니므로 묶음을 만들지 않지만, 장부를 바꿨으므로 다음 라운드의 기준은 됩니다. 같은 라운드에
    개정이 여럿이면(라운드 끝 + 서브 토론, 조기 수렴 + 최종 합성) 마지막 것을 씁니다.
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
    """화면과 이벤트가 쓰는 개정 하나의 모양."""
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
    """세션의 개정 이력을 순서대로. `opened`·`closed` 는 바로 앞 개정과 비교해 채웁니다."""
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
    """세션의 개정 이력으로 쟁점 색인을 세웁니다. 읽지 못하면 빈 색인(번호는 장부 글에서 이어 셉니다).

    읽은 뒤 트랜잭션을 닫아 연결을 풀로 돌려줍니다 (ADR-041). 부르는 쪽(`update_ledger`)이 곧 LLM 응답을 몇 분씩 기다리기
    때문입니다. 부르는 시점에는 커밋 안 된 변경이 없으므로 이 커밋은 아무것도 쓰지 않습니다.
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
    except Exception as exc:  # noqa: BLE001 - 색인을 못 읽어도 장부는 갱신합니다
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
    """개정 한 행을 넣고 바로 커밋합니다. 돌려주는 값은 이벤트에 실을 개정이고, 저장에 실패해도 돌려줍니다.

    값은 모두 여기서 정하고(`id`·`created_at`·`seq`), 커밋 뒤에 행을 다시 읽지 않습니다. 실패해 롤백하면 세션의 ORM 객체가
    만료되는데, 그 뒤에 만료된 객체를 비동기 문맥에서 읽는 일이 없게 하려는 것입니다.
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
    except Exception as exc:  # noqa: BLE001 - 이력을 못 적어도 토론은 계속됩니다
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
