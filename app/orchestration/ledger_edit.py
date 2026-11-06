"""결정 장부(Decision Ledger) 직접 편집 서비스 (ADR-038).

결정 장부는 오케스트레이터가 매 라운드 갱신하는 핵심 산출물입니다. 잘못 기록된 결정을 채팅으로만 정정할 경우 불필요한 토론 왜곡이 발생하므로,
사용자가 UI에서 장부를 직접 수정할 수 있도록 지원하며 다음 3대 원칙을 보장합니다:

* **동시성 제어**: 턴 실행 중 덮어쓰기 유실을 방지하기 위해 낙관적 락을 적용하며, 실행 중이 아닐 때만 저장을 허용합니다.
* **맥락 영속성**: LLM이 다음 라운드에서 사용자의 수정을 덮어쓰지 않도록 수정 사실을 유저 발언 기록(`build_user_record`)에 고정 주입합니다.
* **마크다운 스키마 정합성**: 수렴 판정 및 서브 토론 파서와의 호환성을 유지하기 위해 표준 마크다운 구조(`## 섹션`, `- 항목`)를 엄격히 준수합니다.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select, update

from app.agents.base import Agent
from app.agents.llm_context import context_budget
from app.database.models import MessageModel, SessionModel, TurnModel, utc_now
from app.events import EventCallback
from app.orchestration import context_memory as memory
from app.orchestration import ledger_history, turns

OPEN_ISSUES = "미해결 쟁점"
_EMPTY_SECTION = "- 없음"
# 안내의 글자 상한. 안내는 유저 발언 기록에 고정되므로 길면 진짜 유저 발언의 자리를 뺏습니다. 전문은 장부에 있습니다.
NOTE_MAX_CHARS = 1500
NOTE_HEADER = (
    "[결정 장부 직접 수정] 유저가 결정 장부를 직접 고쳤고, 지금 장부에 반영되어 있습니다. 앞선 발언이나 이전 장부와 "
    "어긋나면 이 수정을 따르세요. 유저가 닫은 쟁점은 유저가 다시 꺼내지 않는 한 되살리지 마세요."
)


class LedgerEditError(ValueError):
    """저장하지 못했습니다. `current` 는 지금 저장된 장부입니다."""

    def __init__(self, message: str, current: str = ""):
        super().__init__(message)
        self.current = current


@dataclass
class LedgerItem:
    text: str
    # 목록 표시. 비어 있으면 모델이 목록 밖에 쓴 줄(문단, 소제목)이고, 표시 없이 그대로 되돌립니다.
    marker: str = "- "
    # 저장할 때 뺍니다 — 화면에서 쟁점의 체크를 풀었거나 항목을 지웠습니다.
    dropped: bool = False
    # 미해결 쟁점의 고정 번호 (ADR-051). `text` 에는 태그를 넣지 않습니다. 편집기에서 새로 넣은 항목은 None 이고, 저장할 때
    # 번호를 받습니다(`save_edit`).
    issue_no: Optional[int] = None


@dataclass
class SavedEdit:
    """직접 수정을 저장한 결과. `note` 는 피드에 올릴 안내, `ledger` 는 번호까지 붙여 저장한 장부, `revision` 은 개정 이력."""

    note: Dict[str, Any]
    ledger: str
    revision: Dict[str, Any]


@dataclass
class LedgerSection:
    title: str
    items: List[LedgerItem] = field(default_factory=list)


def parse_sections(ledger: str) -> List[LedgerSection]:
    """장부를 제목과 항목으로 나눕니다. 비어 있다는 표시(`- 없음`)는 항목이 아닙니다.

    `ledger_items` 와 읽는 규칙이 같습니다. 들여쓴 줄은 앞 항목의 설명으로 그 항목에 붙고, 글을 잃지 않도록 제목 앞의 글과
    목록 밖의 줄도 남깁니다.
    """
    sections: List[LedgerSection] = []
    for line in (ledger or "").splitlines():
        if line.startswith("## "):
            sections.append(LedgerSection(line[3:].strip()))
            continue
        if not line.strip():
            continue
        if not sections:
            sections.append(LedgerSection(""))
        items = sections[-1].items
        issue_no: Optional[int] = None
        if line.startswith(("- ", "* ")):
            text, marker = line[2:].strip(), line[:2]
            if sections[-1].title == OPEN_ISSUES:
                issue_no, text = ledger_history.split_issue_tag(text)
        elif line[0].isspace() and items:
            items[-1].text += "\n" + (line[2:] if line.startswith("  ") else line.lstrip()).rstrip()
            continue
        else:
            text, marker = line.strip(), ""
        if text.lower() not in memory.EMPTY_ITEMS:
            items.append(LedgerItem(text, marker, issue_no=issue_no))
    return sections


def editable_sections(ledger: str) -> List[LedgerSection]:
    """편집 화면에 놓을 칸. 모델이 빠뜨린 정해진 칸은 제자리에 채웁니다. 칸이 없으면 수렴 판정이 장부를 못 읽습니다."""
    sections = parse_sections(ledger)
    at = 1 if sections and not sections[0].title else 0
    for title in memory.LEDGER_SECTIONS:
        found = next((i for i, section in enumerate(sections) if section.title == title), None)
        if found is None:
            sections.insert(at, LedgerSection(title))
            at += 1
        else:
            at = found + 1
    return sections


def _render_item(item: LedgerItem) -> str:
    lines = [line.rstrip() for line in item.text.strip().splitlines() if line.strip()]
    if item.dropped or not lines:
        return ""
    # 사람이 칸 안에 `- ` 까지 적었어도 목록 표시는 하나입니다.
    first = re.sub(r"^[-*](\s+|$)", "", lines[0]).strip() if item.marker else lines[0]
    if not first:
        return ""
    if item.marker and item.issue_no is not None:
        first = f"{ledger_history.issue_tag(item.issue_no)} {first}"
    return "\n".join([f"{item.marker}{first}", *(f"  {line}" for line in lines[1:])])


def render_sections(sections: Iterable[LedgerSection]) -> str:
    """칸들을 장부 글로 되돌립니다. 항목이 하나도 남지 않은 칸은 `- 없음` 으로 둡니다 (`ledger_prompt` 의 약속)."""
    blocks: List[str] = []
    for section in sections:
        body = [line for line in (_render_item(item) for item in section.items) if line]
        if section.title:
            blocks.append("\n".join([f"## {section.title}", *(body or [_EMPTY_SECTION])]))
        elif body:
            blocks.append("\n".join(body))
    return "\n\n".join(blocks)


def _items_by_section(ledger: str) -> Dict[str, List[Tuple[Optional[int], str]]]:
    out: Dict[str, List[Tuple[Optional[int], str]]] = {}
    for section in parse_sections(ledger):
        out.setdefault(section.title, []).extend(
            (item.issue_no, " ".join(item.text.split())) for item in section.items
        )
    return out


def _issue_text(no: Optional[int], text: str) -> str:
    return f"{ledger_history.issue_tag(no)} {text}" if no is not None else text


def _issue_changes(
    old: List[Tuple[Optional[int], str]], new: List[Tuple[Optional[int], str]],
) -> Tuple[List[str], List[Tuple[Optional[int], str]], List[Tuple[Optional[int], str]]]:
    """미해결 쟁점을 번호로 비교합니다 (ADR-051). (고친 줄, 닫은 쟁점, 넣은 쟁점).

    번호가 같으면 같은 쟁점이고 글이 달라졌으면 고친 것입니다. 한쪽에만 번호가 있는 쟁점(옛 장부, 저장하며 번호를 받은 새
    항목)은 글로 맞춰 봅니다. 글이 같으면 번호만 붙은 것이라 바뀐 것이 없습니다.
    """
    old_by_no = {no: text for no, text in old if no is not None}
    new_by_no = {no: text for no, text in new if no is not None}
    edited = [
        f"- {OPEN_ISSUES} {ledger_history.issue_tag(no)} 고침: {new_by_no[no]}"
        for no in old_by_no if no in new_by_no and old_by_no[no] != new_by_no[no]
    ]
    gone = [(no, text) for no, text in old if no is None or no not in new_by_no]
    came = [(no, text) for no, text in new if no is None or no not in old_by_no]
    came_texts = [text for _no, text in came]
    gone_texts = [text for _no, text in gone]
    return (
        edited,
        [(no, text) for no, text in gone if text not in came_texts],
        [(no, text) for no, text in came if text not in gone_texts],
    )


def describe_changes(before: str, after: str) -> List[str]:
    """바뀐 항목을 한 줄씩. 글자 배치만 달라졌으면 빈 목록입니다. 미해결 쟁점은 고정 번호로 비교합니다."""
    old, new = _items_by_section(before), _items_by_section(after)
    lines: List[str] = []
    for title in [*new, *(t for t in old if t not in new)]:
        label = title or "장부"
        if title == OPEN_ISSUES:
            edited, gone, came = _issue_changes(old.get(title, []), new.get(title, []))
            lines += [f"- {label}에서 닫음: {_issue_text(no, text)}" for no, text in gone]
            lines += edited
            lines += [f"- {label}에 넣음: {_issue_text(no, text)}" for no, text in came]
            continue
        old_texts = [text for _no, text in old.get(title, [])]
        new_texts = [text for _no, text in new.get(title, [])]
        lines += [f"- {label}에서 뺌: {text}" for text in old_texts if text not in new_texts]
        lines += [f"- {label}에 넣음: {text}" for text in new_texts if text not in old_texts]
    return lines


def char_limit(agents: Iterable[Agent]) -> int:
    """이 대화 장부의 글자 상한. 턴이 LLM 이 쓴 장부에 거는 것과 같은 값입니다 (`update_ledger`)."""
    budget = min((context_budget(agent) for agent in agents), default=0)
    return memory.memory_cap(memory.LEDGER_SHARE, memory.LEDGER_MAX_CHARS, budget)


async def save_edit(
    db, session_id: str, expected: str, ledger: str, *, on_event: Optional[EventCallback] = None,
) -> Optional[SavedEdit]:
    """장부를 사람이 고친 값으로 바꾸고 안내를 남깁니다. 바뀐 항목이 없으면 None.

    `expected` 는 편집을 시작할 때 읽은 장부입니다. 그 사이 다른 탭이나 턴이 장부를 바꿨으면 저장하지 않습니다.

    장부에 반영된 지점(`ledger_through_id`)은 그대로 둡니다. 안내는 그 뒤에 놓이므로 다음 갱신이 함께 읽습니다. 마지막 턴의
    기억(`TurnModel.memory`)도 맞춥니다. 다음 턴은 고친 장부로 시작하는데, 그 턴에서 발언을 다시 실행하거나 이 턴에서
    갈라지면 그 기억을 읽고, 안내도 그 자리에 함께 있습니다.

    편집기에서 새로 넣은 쟁점은 세션의 쟁점 색인으로 번호를 받습니다(ADR-051). 옛 쟁점의 글을 그대로 다시 넣으면 옛 번호를
    받습니다. 저장한 뒤 개정 이력(`kind=edit`)을 안내 id 와 함께 남깁니다.
    """
    index = await ledger_history.load_index(db, session_id)
    index.absorb(expected)
    ledger, open_issues = ledger_history.assign_issue_ids(ledger, index)
    changes = describe_changes(expected, ledger)
    if not changes:
        return None
    saved = await db.execute(
        update(SessionModel)
        .where(SessionModel.id == session_id, func.coalesce(SessionModel.decision_ledger, "") == expected)
        # 기억을 적었다고 대화 목록에서 "방금 바뀐 대화" 로 올라가지 않게 합니다 (`persist_memory` 와 같습니다).
        .values(decision_ledger=ledger, updated_at=SessionModel.updated_at)
    )
    if not saved.rowcount:
        await db.rollback()
        session = await db.get(SessionModel, session_id)
        raise LedgerEditError(
            "그 사이 장부가 바뀌어 저장하지 않았습니다. 새 장부를 불러왔으니 다시 고쳐 주십시오.",
            current=(session.decision_ledger or "") if session is not None else expected,
        )

    latest = (await db.execute(
        select(TurnModel).where(TurnModel.session_id == session_id).order_by(TurnModel.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    if latest is not None and isinstance(latest.memory, dict):
        await db.execute(update(TurnModel).where(TurnModel.id == latest.id).values(
            memory={**latest.memory, "decision_ledger": ledger}, updated_at=TurnModel.updated_at,
        ))

    now = utc_now()
    note = MessageModel(
        session_id=session_id, sender_key="system", sender_name="MADO", sender_role="",
        msg_type="system", round_number=0, started_at=now, finished_at=now,
        turn_meta=turns.meta(turns.KIND_LEDGER_EDIT),
        content=memory.clip_chars(
            "\n".join([NOTE_HEADER, *changes]), NOTE_MAX_CHARS, "- …(더 있습니다. 지금 장부를 보세요)",
        ),
    )
    db.add(note)
    await db.commit()
    shown = {
        "id": note.id, "sender_key": note.sender_key, "sender_name": note.sender_name,
        "sender_role": note.sender_role, "content": note.content, "round_number": note.round_number,
        "msg_type": note.msg_type, "created_at": note.created_at, "started_at": note.started_at,
        "finished_at": note.finished_at, "turn_meta": note.turn_meta, "tool_calls": [],
    }
    revision = await ledger_history.record_revision(
        db,
        session_id=session_id,
        turn_id=None,
        round_number=0,
        kind=ledger_history.KIND_EDIT,
        reason="직접 수정",
        ledger=ledger,
        open_issues=open_issues,
        issue_high=index.high,
        previous_ledger=expected,
        message_id=shown["id"],
        on_event=on_event,
    )
    return SavedEdit(note=shown, ledger=ledger, revision=revision)
