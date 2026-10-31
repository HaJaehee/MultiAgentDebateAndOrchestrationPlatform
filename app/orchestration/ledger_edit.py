"""결정 장부 직접 편집 (ADR-038).

장부는 오케스트레이터가 라운드마다 통째로 다시 쓰는 파생물입니다. 고칠 길이 채팅뿐이면 잘못 적힌 결정이 다음 갱신까지
모든 프롬프트에 실립니다. 여기서는 사람이 항목을 바로 고치되, 세 가지를 지킵니다.

* **도는 턴과 겹치지 않습니다.** 턴은 장부를 메모리에 들고 있다가 끝에서 통째로 저장합니다(`persist_memory`). 그 사이에
  고친 값은 말없이 덮입니다. 부르는 쪽이 실행 여부를 확인하고, 저장은 읽었던 장부가 그대로일 때만 합니다.
* **다음 갱신에도 남습니다.** LLM 은 장부 전체를 다시 쓰므로, 사람이 고친 항목인 줄 모르면 되돌릴 수 있습니다. 고친 내용을
  안내로 남기고 유저 발언 기록에 함께 고정합니다(`build_user_record`). 채팅 정정과 같은 권위입니다.
* **형식이 깨지지 않습니다.** 수렴 판정과 서브 토론은 `## 제목` 과 `- 항목` 을 읽습니다(`ledger_items`). 화면은 항목 단위로
  고치고, 글은 여기서 다시 씁니다.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func, select, update

from app.agents.base import Agent
from app.agents.llm_context import context_budget
from app.database.models import MessageModel, SessionModel, TurnModel, utc_now
from app.orchestration import context_memory as memory
from app.orchestration import turns

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
        if line.startswith(("- ", "* ")):
            text, marker = line[2:].strip(), line[:2]
        elif line[0].isspace() and items:
            items[-1].text += "\n" + (line[2:] if line.startswith("  ") else line.lstrip()).rstrip()
            continue
        else:
            text, marker = line.strip(), ""
        if text.lower() not in memory.EMPTY_ITEMS:
            items.append(LedgerItem(text, marker))
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


def _items_by_section(ledger: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for section in parse_sections(ledger):
        out.setdefault(section.title, []).extend(" ".join(item.text.split()) for item in section.items)
    return out


def describe_changes(before: str, after: str) -> List[str]:
    """바뀐 항목을 한 줄씩. 글자 배치만 달라졌으면 빈 목록입니다."""
    old, new = _items_by_section(before), _items_by_section(after)
    lines: List[str] = []
    for title in [*new, *(t for t in old if t not in new)]:
        gone = [text for text in old.get(title, []) if text not in new.get(title, [])]
        came = [text for text in new.get(title, []) if text not in old.get(title, [])]
        label = title or "장부"
        verb = "닫음" if title == OPEN_ISSUES else "뺌"
        lines += [f"- {label}에서 {verb}: {text}" for text in gone]
        lines += [f"- {label}에 넣음: {text}" for text in came]
    return lines


def char_limit(agents: Iterable[Agent]) -> int:
    """이 대화 장부의 글자 상한. 턴이 LLM 이 쓴 장부에 거는 것과 같은 값입니다 (`update_ledger`)."""
    budget = min((context_budget(agent) for agent in agents), default=0)
    return memory.memory_cap(memory.LEDGER_SHARE, memory.LEDGER_MAX_CHARS, budget)


async def save_edit(db, session_id: str, expected: str, ledger: str) -> Optional[Dict[str, Any]]:
    """장부를 사람이 고친 값으로 바꾸고 안내를 남깁니다. 돌려주는 값은 피드에 올릴 안내이고, 바뀐 항목이 없으면 None.

    `expected` 는 편집을 시작할 때 읽은 장부입니다. 그 사이 다른 탭이나 턴이 장부를 바꿨으면 저장하지 않습니다.

    장부에 반영된 지점(`ledger_through_id`)은 그대로 둡니다. 안내는 그 뒤에 놓이므로 다음 갱신이 함께 읽습니다. 마지막 턴의
    기억(`TurnModel.memory`)도 맞춥니다. 다음 턴은 고친 장부로 시작하는데, 그 턴에서 발언을 다시 실행하거나 이 턴에서
    갈라지면 그 기억을 읽고, 안내도 그 자리에 함께 있습니다.
    """
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
    return {
        "id": note.id, "sender_key": note.sender_key, "sender_name": note.sender_name,
        "sender_role": note.sender_role, "content": note.content, "round_number": note.round_number,
        "msg_type": note.msg_type, "created_at": note.created_at, "started_at": note.started_at,
        "finished_at": note.finished_at, "turn_meta": note.turn_meta, "tool_calls": [],
    }
