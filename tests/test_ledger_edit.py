"""결정 장부 직접 편집 (ADR-038).

지키려는 것.

1. **형식** — 고쳐도 `## 제목` 과 `- 항목` 이 남아 수렴 판정과 서브 토론이 장부를 그대로 읽습니다.
2. **겹침** — 읽었던 장부가 그 사이 바뀌었으면 저장하지 않습니다. 토론이 돌면 고칠 수 없습니다.
3. **권위** — 고친 내용은 유저 발언 기록에 고정되어 이후 호출과 장부 갱신에 실립니다.
4. **뒤따르는 길** — 대화 목록 순서는 그대로이고, 다음 턴·갈래·발언 다시 실행이 고친 장부를 읽습니다.
5. **화면** — 항목을 고치고 쟁점을 닫아 저장합니다. 도는 토론이 있으면 잠기고 고치던 것은 알리고 닫습니다.
"""

from typing import Any, Dict, List

import pytest
from nicegui import context, ui
from sqlalchemy import select

from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory
from app.orchestration import context_memory as memory
from app.orchestration import ledger_edit, turns
from app.orchestration.ledger_edit import LedgerEditError, LedgerItem, LedgerSection
from app.orchestration.state import DebateMessage, DebateState
from app.session_fork import fork_session
from app.ui.components.roster.ledger_panel import LedgerPanelComponent
from app.ui.components.roster.state import RosterState
from tests.fake_llm import FakeLLMCaller
from tests.test_turn_resume import DB_URL, REQUEST, _crash, _engine, _session

LEDGER = "\n".join([
    "## 요구사항·제약",
    "- Redis 를 쓰지 않는다",
    "",
    "## 결정 사항",
    "- 캐시는 Redis 로 둔다 — 빠르다",
    "- 만료는 10분",
    "",
    "## 기각된 대안",
    "- 없음",
    "",
    "## 미해결 쟁점",
    "- 캐시 키 형식",
    "- 장애 시 동작",
    "",
    "## 담당·다음 할 일",
    "- 없음",
])


# =============================================================== 1. 형식


def test_a_ledger_comes_back_as_it_was_without_edits():
    assert ledger_edit.render_sections(ledger_edit.parse_sections(LEDGER)) == LEDGER
    assert ledger_edit.render_sections(ledger_edit.editable_sections(LEDGER)) == LEDGER


def test_closing_every_issue_leaves_a_readable_empty_section():
    sections = ledger_edit.editable_sections(LEDGER)
    issues = next(s for s in sections if s.title == ledger_edit.OPEN_ISSUES)
    for item in issues.items:
        item.dropped = True

    edited = ledger_edit.render_sections(sections)

    assert "## 미해결 쟁점\n- 없음" in edited
    assert memory.open_issue_count(edited) == 0
    assert memory.ledger_items(edited, "결정 사항") == ["캐시는 Redis 로 둔다 — 빠르다", "만료는 10분"]


def test_sections_the_model_dropped_are_filled_in_their_place():
    sections = ledger_edit.editable_sections("## 결정 사항\n- 하나\n\n## 기타\n- 모델이 만든 칸")
    assert [s.title for s in sections] == [
        "요구사항·제약", "결정 사항", "기각된 대안", "미해결 쟁점", "담당·다음 할 일", "기타",
    ]
    rendered = ledger_edit.render_sections(sections)
    assert memory.ledger_items(rendered, "미해결 쟁점") == []
    assert "## 기타\n- 모델이 만든 칸" in rendered


def test_text_outside_lists_and_continuations_are_not_lost():
    text = "앞말\n\n## 결정 사항\n- 항목\n  근거: 측정\n    - 세부\n### 소제목\n* 별표 항목\n문단"
    sections = ledger_edit.parse_sections(text)
    assert sections[0].title == "" and sections[0].items[0].text == "앞말"
    assert sections[1].items[0].text == "항목\n근거: 측정\n  - 세부"
    assert ledger_edit.render_sections(sections) == text


def test_a_person_typing_the_marker_does_not_double_it():
    section = LedgerSection("결정 사항", [
        LedgerItem("- 표시까지 적음"), LedgerItem("두 줄\n둘째 줄"), LedgerItem("   "), LedgerItem("- "),
    ])
    assert ledger_edit.render_sections([section]) == "## 결정 사항\n- 표시까지 적음\n- 두 줄\n  둘째 줄"


def test_changes_are_listed_per_section_and_layout_alone_is_no_change():
    after = LEDGER.replace("캐시는 Redis 로 둔다 — 빠르다", "캐시는 메모리에 둔다").replace("- 캐시 키 형식\n", "")
    assert ledger_edit.describe_changes(LEDGER, after) == [
        "- 결정 사항에서 뺌: 캐시는 Redis 로 둔다 — 빠르다",
        "- 결정 사항에 넣음: 캐시는 메모리에 둔다",
        "- 미해결 쟁점에서 닫음: 캐시 키 형식",
    ]
    assert ledger_edit.describe_changes(LEDGER, LEDGER.replace("\n\n", "\n")) == []


def test_the_limit_follows_the_smallest_window():
    def agent(window: int) -> Any:
        return AgentPool({"a": AgentConfig(
            name="A", role="r", model="fake/model", api_key="k", max_context_window=window,
        )}).list_all()[0]

    assert ledger_edit.char_limit([agent(200_000), agent(10_000)]) < memory.LEDGER_MAX_CHARS
    assert ledger_edit.char_limit([agent(200_000)]) == memory.LEDGER_MAX_CHARS


# =============================================================== 2. 겹침


async def _finished_turn(max_rounds: int = 1) -> str:
    sid = await _session(max_rounds=max_rounds)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    return sid


async def _set_ledger(sid: str, ledger: str = LEDGER) -> None:
    async with get_session_factory(DB_URL)() as db:
        (await db.get(SessionModel, sid)).decision_ledger = ledger
        await db.commit()


async def _save(sid: str, after: str, expected: str = LEDGER) -> Any:
    async with get_session_factory(DB_URL)() as db:
        return await ledger_edit.save_edit(db, sid, expected, after)


async def _session_row(sid: str) -> SessionModel:
    async with get_session_factory(DB_URL)() as db:
        return await db.get(SessionModel, sid)


async def _rows(sid: str) -> List[MessageModel]:
    async with get_session_factory(DB_URL)() as db:
        return list((await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all())


FIXED = LEDGER.replace("캐시는 Redis 로 둔다 — 빠르다", "캐시는 메모리에 둔다").replace("- 캐시 키 형식\n", "")


@pytest.mark.asyncio
async def test_a_stale_screen_does_not_overwrite_a_newer_ledger():
    sid = await _finished_turn()
    await _set_ledger(sid, LEDGER + "\n- 그 사이 들어온 항목")
    before = len(await _rows(sid))

    with pytest.raises(LedgerEditError) as caught:
        await _save(sid, FIXED)

    assert caught.value.current == LEDGER + "\n- 그 사이 들어온 항목", "화면이 새 장부로 돌아갈 수 있어야 합니다"
    assert (await _session_row(sid)).decision_ledger == caught.value.current
    assert len(await _rows(sid)) == before, "저장하지 못한 수정은 기록에도 남지 않습니다"


@pytest.mark.asyncio
async def test_nothing_is_written_when_no_item_changed():
    sid = await _finished_turn()
    await _set_ledger(sid)
    before = len(await _rows(sid))

    assert await _save(sid, LEDGER.replace("\n\n", "\n")) is None

    assert (await _session_row(sid)).decision_ledger == LEDGER
    assert len(await _rows(sid)) == before


# =============================================================== 3. 권위


class RecordingLLM(FakeLLMCaller):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent: List[Dict[str, Any]] = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.sent.append({
            "key": agent.key, "ledger": kwargs.get("ledger", ""),
            "text": "\n".join(str(m.get("content")) for m in messages),
        })
        return await super().call_agent(agent, messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_a_saved_edit_leaves_a_note_in_the_record():
    sid = await _finished_turn()
    await _set_ledger(sid)

    note = await _save(sid, FIXED)

    assert note["msg_type"] == "system" and note["sender_name"] == "MADO"
    assert "결정 사항에 넣음: 캐시는 메모리에 둔다" in note["content"]
    assert "미해결 쟁점에서 닫음: 캐시 키 형식" in note["content"]
    last = (await _rows(sid))[-1]
    assert last.id == note["id"] and turns.kind_of(last) == turns.KIND_LEDGER_EDIT and last.turn_id is None
    assert (await _session_row(sid)).decision_ledger == FIXED


@pytest.mark.asyncio
async def test_a_huge_edit_does_not_fill_the_pinned_record():
    sid = await _finished_turn()
    await _set_ledger(sid)
    huge = LEDGER + "".join(f"\n- 새 항목 {n} " + "가" * 100 for n in range(40))

    note = await _save(sid, huge)

    assert len(note["content"]) <= ledger_edit.NOTE_MAX_CHARS + 40
    assert note["content"].endswith("지금 장부를 보세요)")


@pytest.mark.asyncio
async def test_the_next_turn_reads_the_edit_with_the_authority_of_a_user_message():
    sid = await _finished_turn()
    await _set_ledger(sid)
    await _save(sid, FIXED)

    llm = RecordingLLM()
    await _engine(llm).run_turn(session_id=sid, user_prompt="이어서 진행해 줘")

    speech = next(s for s in llm.sent if s["key"] == "architect")
    assert "[유저 발언 기록]" in speech["text"] and "결정 장부 직접 수정" in speech["text"]
    assert "결정 사항에 넣음: 캐시는 메모리에 둔다" in speech["text"]
    assert speech["text"].count("결정 사항에 넣음: 캐시는 메모리에 둔다") == 1, "기록 안의 안내는 참조로 바뀝니다"
    assert "캐시는 메모리에 둔다" in speech["ledger"] and "캐시 키 형식" not in speech["ledger"]

    update = next(s for s in llm.sent if memory.LEDGER_PROMPT_MARKER in s["text"])
    assert "결정 사항에 넣음: 캐시는 메모리에 둔다" in update["text"], "장부를 다시 쓰는 호출도 고친 내용을 압니다"
    assert "이번 턴" not in update["text"].split("결정 장부 직접 수정")[0].splitlines()[-1]


def test_a_later_user_message_still_outranks_an_earlier_edit():
    state = DebateState(session_id="s", user_prompt="지금 요청", strategy="sequential_debate", max_rounds=1, current_round=1)
    state.messages = [
        DebateMessage(sender_key="system", sender_name="MADO", sender_role="", content="[결정 장부 직접 수정] 고침", msg_type="system",
                      turn_meta=turns.meta(turns.KIND_LEDGER_EDIT)),
        DebateMessage(sender_key="user", sender_name="User", sender_role="", content="역시 Redis 로 하자", msg_type="user"),
        DebateMessage(sender_key="user", sender_name="User", sender_role="", content="지금 요청", msg_type="user"),
    ]
    state.turn_message_start = 2

    record = memory.build_user_record(state, model="fake/model", token_cap=10_000)

    assert record.refs == {0: 1, 1: 2}
    assert record.text.index("[결정 장부 직접 수정] 고침") < record.text.index("역시 Redis 로 하자")
    assert "이전 턴 결정 장부 직접 수정" in record.text


def test_other_notes_stay_out_of_the_user_record():
    state = DebateState(session_id="s", user_prompt="요청", strategy="sequential_debate", max_rounds=1, current_round=1)
    state.messages = [DebateMessage(
        sender_key="system", sender_name="MADO", sender_role="", content="[작업 공간 되돌리기] ...", msg_type="system",
        turn_meta=turns.meta(turns.KIND_NOTE),
    )]
    assert memory.build_user_record(state, model="fake/model", token_cap=10_000).text == ""


# =============================================================== 4. 뒤따르는 길


@pytest.mark.asyncio
async def test_the_sidebar_order_and_the_folded_position_stay():
    sid = await _finished_turn()
    await _set_ledger(sid)
    before = await _session_row(sid)

    await _save(sid, FIXED)

    after = await _session_row(sid)
    assert after.updated_at == before.updated_at
    assert after.ledger_through_id == before.ledger_through_id


@pytest.mark.asyncio
async def test_only_the_latest_turn_remembers_the_edit():
    sid = await _finished_turn()
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="두 번째 요청")
    current = (await _session_row(sid)).decision_ledger

    async with get_session_factory(DB_URL)() as db:
        first, second = (await db.execute(
            select(TurnModel).where(TurnModel.session_id == sid).order_by(TurnModel.started_at)
        )).scalars().all()
    first_memory = dict(first.memory)

    edited = current + "\n- 사람이 넣은 항목"
    await _save(sid, edited, expected=current)

    async with get_session_factory(DB_URL)() as db:
        assert (await db.get(TurnModel, second.id)).memory["decision_ledger"] == edited
        assert (await db.get(TurnModel, first.id)).memory == first_memory


@pytest.mark.asyncio
async def test_a_fork_at_the_edited_turn_carries_both_the_ledger_and_the_note(tmp_path):
    sid = await _finished_turn()
    async with get_session_factory(DB_URL)() as db:
        (await db.get(SessionModel, sid)).workspace_dir = str(tmp_path / "ws")
        await db.commit()
    await _set_ledger(sid)
    await _save(sid, FIXED)
    async with get_session_factory(DB_URL)() as db:
        turn_id = (await db.execute(select(TurnModel.id).where(TurnModel.session_id == sid))).scalar_one()
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="다음 요청")

    async with get_session_factory(DB_URL)() as db:
        fork = await fork_session(db, sid, turn_id)

    assert (await _session_row(fork.session_id)).decision_ledger == FIXED
    assert [turns.kind_of(m) for m in await _rows(fork.session_id)].count(turns.KIND_LEDGER_EDIT) == 1


@pytest.mark.asyncio
async def test_a_turn_resumed_after_a_crash_reads_the_edit_made_in_between():
    sid = await _finished_turn()
    current = (await _session_row(sid)).decision_ledger
    turn_id = await _crash(sid)

    edited = current + "\n- 끊긴 사이에 사람이 넣은 항목"
    await _save(sid, edited, expected=current)
    async with get_session_factory(DB_URL)() as db:
        assert (await db.get(TurnModel, turn_id)).memory is None, "끝나지 않은 턴에는 기억이 없습니다"

    llm = RecordingLLM()
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    update = next(s for s in llm.sent if memory.LEDGER_PROMPT_MARKER in s["text"])
    previous = update["text"].split("[이전 장부]")[1].split("[새로 나온 발언")[0]
    assert "끊긴 사이에 사람이 넣은 항목" in previous, "재개한 턴은 고친 장부 위에서 갱신합니다"
    speech = next(s for s in llm.sent if s["key"] != "orchestrator")
    assert "담당·다음 할 일에 넣음: 끊긴 사이에 사람이 넣은 항목" in speech["text"]


@pytest.mark.asyncio
async def test_an_edit_alone_does_not_cost_a_ledger_update():
    llm = FakeLLMCaller()
    engine = _engine(llm)
    state = DebateState(session_id="s", user_prompt="요청", strategy="sequential_debate", max_rounds=1, current_round=1)
    state.messages = [
        DebateMessage(sender_key="user", sender_name="User", sender_role="", content="요청", msg_type="user"),
        DebateMessage(sender_key="system", sender_name="MADO", sender_role="", content="[결정 장부 직접 수정] 고침",
                      msg_type="system", turn_meta=turns.meta(turns.KIND_LEDGER_EDIT)),
    ]
    state.decision_ledger = LEDGER

    updated = await engine.conversation.update_ledger(
        state=state, orchestrator=engine.agent_pool.get_orchestrator(), on_event=None, reason="라운드",
    )

    assert updated is False and llm.calls == []


@pytest.mark.asyncio
async def test_retrying_a_speech_of_the_next_turn_starts_from_the_edited_ledger():
    sid = await _finished_turn()
    current = (await _session_row(sid)).decision_ledger
    await _save(sid, current + "\n- 사람이 넣은 항목", expected=current)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="다음 요청")
    target = [
        m for m in await _rows(sid) if m.sender_key == "architect" and turns.kind_of(m) == turns.KIND_SPEECH
    ][-1]

    llm = RecordingLLM()
    await _engine(llm).retry_speech(sid, target.id)

    speech = next(s for s in llm.sent if s["key"] == "architect")
    assert "사람이 넣은 항목" in speech["ledger"]


# =============================================================== 5. 화면


class Service:
    def __init__(self):
        self.running: List[str] = []

    def is_running(self, session_id):
        return session_id in self.running


def _pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=key.title(), role=key, model="fake/model", api_key="k", max_context_window=200_000)
        for key in ("orchestrator", "architect")
    })


@pytest.fixture
def notes(monkeypatch):
    shown: List[Any] = []
    monkeypatch.setattr(ui, "notify", lambda message, **kw: shown.append((str(message), kw.get("type"))))
    return shown


@pytest.fixture
def make_panel(ui_page):
    def make(ledger: str = LEDGER, *, fail: Any = None):
        state = RosterState(agent_pool=_pool(), session_id="s", decision_ledger=ledger)
        service = Service()
        saved: List[Any] = []

        async def on_save(expected, edited):
            if fail is not None:
                raise fail
            saved.append((expected, edited))

        panel = LedgerPanelComponent(state, service, on_save=on_save)
        with ui_page:
            panel.build()
        return panel, service, saved

    return make


def _elements(root, kind) -> List[Any]:
    return [el for el in root.descendants() if isinstance(el, kind)]


def _click(button) -> None:
    for listener_id, listener in list(button._event_listeners.items()):
        if listener.type == "click":
            button._handle_event({"listener_id": listener_id, "args": None})


def _section(panel, title: str) -> LedgerSection:
    return next(s for s in panel.draft if s.title == title)


def test_the_ledger_says_how_long_it_is(make_panel):
    panel, _service, _saved = make_panel("")
    assert panel.expansion._props["caption"] == "비어 있음"
    assert panel.edit_button.visible is False, "고칠 항목이 없으면 버튼도 없습니다"

    panel.set_ledger("가" * 1200)

    assert panel.state.decision_ledger == "가" * 1200
    assert panel.expansion._props["caption"] == "1,200자"
    assert panel.edit_button.visible is True


def test_the_editor_shows_every_item_and_checks_only_the_open_issues(make_panel):
    panel, _service, _saved = make_panel()
    panel.open_editor()

    assert panel.editing and panel.edit_button.visible is False and panel.view.visible is False
    boxes = _elements(panel.editor, ui.textarea)
    assert [b.value for b in boxes] == [
        "Redis 를 쓰지 않는다", "캐시는 Redis 로 둔다 — 빠르다", "만료는 10분", "캐시 키 형식", "장애 시 동작",
    ]
    assert len(_elements(panel.editor, ui.checkbox)) == 2


@pytest.mark.asyncio
async def test_editing_a_decision_and_unchecking_an_issue_saves_the_ledger(notes, make_panel):
    panel, _service, saved = make_panel()
    panel.open_editor()
    box = next(b for b in _elements(panel.editor, ui.textarea) if b.value.startswith("캐시는 Redis"))
    box.set_value("캐시는 메모리에 둔다")
    _elements(panel.editor, ui.checkbox)[0].set_value(False)

    await panel.save()

    assert saved == [(LEDGER, ledger_edit.render_sections(ledger_edit.parse_sections(FIXED)))]
    assert panel.state.decision_ledger == saved[0][1]
    assert panel.editing is False and panel.view.visible is True and panel.edit_button.visible is True
    assert notes[-1][1] == "positive"


@pytest.mark.asyncio
async def test_an_unchecked_issue_can_be_checked_again_before_saving(notes, make_panel):
    panel, _service, saved = make_panel()
    panel.open_editor()
    check = _elements(panel.editor, ui.checkbox)[0]
    check.set_value(False)
    box = next(b for b in _elements(panel.editor, ui.textarea) if b.value == "캐시 키 형식")
    assert _section(panel, "미해결 쟁점").items[0].dropped is True and box.enabled is False

    check.set_value(True)
    assert _section(panel, "미해결 쟁점").items[0].dropped is False and box.enabled is True

    await panel.save()
    assert saved == [] and notes[-1][1] == "info", "돌려놓으면 바뀐 것이 없습니다"


@pytest.mark.asyncio
async def test_items_can_be_added_and_deleted(notes, make_panel):
    panel, _service, saved = make_panel()
    panel.open_editor()
    buttons = _elements(panel.editor, ui.button)

    _click(next(b for b in buttons if b._props.get("icon") == "close"))
    assert "Redis 를 쓰지 않는다" not in [b.value for b in _elements(panel.editor, ui.textarea)]

    _click([b for b in buttons if b.text == "항목 추가"][-1])
    added = _elements(panel.editor, ui.textarea)[-1]
    assert added.value == "" and "autofocus" in added._props
    added.set_value("코더: 구현")

    await panel.save()

    edited = saved[0][1]
    assert memory.ledger_items(edited, "요구사항·제약") == []
    assert memory.ledger_items(edited, "담당·다음 할 일") == ["코더: 구현"]


@pytest.mark.asyncio
async def test_over_the_limit_is_refused_but_shrinking_is_not(notes, make_panel):
    panel, _service, saved = make_panel()
    panel.open_editor()
    _section(panel, "결정 사항").items.append(LedgerItem("가" * (memory.LEDGER_MAX_CHARS + 10)))

    await panel.save()
    assert saved == [] and panel.editing and notes[-1][1] == "warning"

    long_ledger = LEDGER + "\n- " + "나" * (memory.LEDGER_MAX_CHARS + 500)
    panel, _service, saved = make_panel(long_ledger)
    panel.open_editor()
    _section(panel, "담당·다음 할 일").items[-1].text = "나" * (memory.LEDGER_MAX_CHARS + 100)

    await panel.save()
    assert len(saved) == 1, "이미 상한을 넘은 장부라도 줄이는 편집은 저장됩니다"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [None, LedgerEditError("그 사이 장부가 바뀌었습니다", current=LEDGER + "\n- 새 항목")])
async def test_saving_still_speaks_from_the_slot_it_deletes(make_panel, monkeypatch, fail):
    """저장 버튼의 핸들러는 그 버튼이 놓인 슬롯에서 돕니다. 편집칸을 먼저 비우면 슬롯이 사라져 알림이 터집니다."""
    shown: List[str] = []

    def notify(message, **_kw):
        assert context.client is not None
        shown.append(str(message))

    monkeypatch.setattr(ui, "notify", notify)
    panel, _service, _saved = make_panel(fail=fail)
    panel.open_editor()
    _elements(panel.editor, ui.checkbox)[0].set_value(False)
    button = next(b for b in _elements(panel.editor, ui.button) if b.text == "저장")
    slot = button.parent_slot
    del button

    with slot:
        await panel.save()

    assert len(shown) == 1


@pytest.mark.asyncio
async def test_a_debate_found_while_saving_still_speaks_from_the_slot_it_deletes(make_panel, monkeypatch):
    shown: List[str] = []

    def notify(message, **_kw):
        assert context.client is not None
        shown.append(str(message))

    monkeypatch.setattr(ui, "notify", notify)
    panel, service, saved = make_panel()
    panel.open_editor()
    _elements(panel.editor, ui.checkbox)[0].set_value(False)
    button = next(b for b in _elements(panel.editor, ui.button) if b.text == "저장")
    slot = button.parent_slot
    del button
    service.running.append("s")

    with slot:
        await panel.save()

    assert saved == [] and "저장되지 않았습니다" in shown[0]


def test_a_running_debate_locks_the_button(make_panel):
    panel, service, _saved = make_panel()
    assert panel.edit_button.enabled is True

    service.running.append("s")
    panel.sync()

    assert panel.edit_button.enabled is False
    assert "토론이 끝난 뒤" in panel.edit_tooltip.text
    service.running.clear()
    panel.sync()
    assert panel.edit_button.enabled is True


def test_a_debate_started_elsewhere_refuses_to_open_the_editor(notes, make_panel):
    panel, service, _saved = make_panel()
    service.running.append("s")

    panel.open_editor()

    assert panel.editing is False and notes[-1][1] == "warning"


def test_a_debate_starting_closes_the_editor_and_says_so(notes, make_panel):
    panel, service, _saved = make_panel()
    panel.open_editor()
    service.running.append("s")

    panel.sync()

    assert panel.editing is False and panel.view.visible is True
    assert "저장되지 않았습니다" in notes[-1][0]


@pytest.mark.asyncio
async def test_a_debate_started_while_saving_is_not_overwritten(notes, make_panel):
    panel, service, saved = make_panel()
    panel.open_editor()
    _elements(panel.editor, ui.checkbox)[0].set_value(False)
    service.running.append("s")

    await panel.save()

    assert saved == [] and panel.editing is False


def test_a_new_ledger_from_a_round_closes_the_editor(notes, make_panel):
    panel, _service, _saved = make_panel()
    panel.open_editor()

    panel.set_ledger(LEDGER)
    assert panel.editing is True, "같은 장부면 고치던 것을 버리지 않습니다"

    panel.set_ledger(LEDGER + "\n- 새 항목")
    assert panel.editing is False and "저장되지 않았습니다" in notes[-1][0]
    assert panel.state.decision_ledger == LEDGER + "\n- 새 항목"


@pytest.mark.asyncio
async def test_a_conflict_brings_back_the_newer_ledger(notes, make_panel):
    newer = LEDGER + "\n- 다른 탭이 넣은 항목"
    panel, _service, _saved = make_panel(fail=LedgerEditError("그 사이 장부가 바뀌었습니다", current=newer))
    panel.open_editor()
    _elements(panel.editor, ui.checkbox)[0].set_value(False)

    await panel.save()

    assert panel.editing is False and panel.state.decision_ledger == newer
    assert notes[-1] == ("그 사이 장부가 바뀌었습니다", "warning")
