"""전역 도구 규칙 편집과 보안 감사.

1. 규칙 편집은 파일의 지금 내용에서 한 줄만 고치고, 주석과 나머지 기본 보호를 잃지 않는다.
2. 고친 규칙은 진행 중인 게이트에 다음 호출부터 걸린다.
3. 감사는 규칙·모드·고정 보호가 막은 호출만 모으고, 갈라 낸 대화의 복사본을 두 번 세지 않는다.
4. 고치는 것은 서버 PC 앞의 사람만 한다. 원격은 보기만 하고, 함수가 불려도 파일이 그대로다.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List

import pytest
import pytest_asyncio
from nicegui import ui
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import (
    DEFAULT_TOOL_DENY_RULES,
    add_tool_security_rules_to_conf_file,
    get_config,
    read_conf_file,
    remove_tool_security_rule_from_conf_file,
    replace_tool_security_rule_in_conf_file,
)
from app.config import loader
from app.database.models import Base, SessionModel, ToolCallRecordModel
from app.orchestration import roster_service
from app.orchestration.recording import TurnRecorder
from app.orchestration.roster_service import RosterService
from app.orchestration.tool_audit import ARGUMENTS_TEXT_LIMIT, blocked_tool_calls
from app.orchestration.tool_gate import ToolGate
from app.ui.components.roster.state import RosterState
from app.ui.components.roster.tool_security_panel import ToolSecurityDialog
from tests.test_tool_security_gate import _agent, _manager


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "conf.json"
    path.write_text(json.dumps({
        "llm": {"model": "openai/gpt-4o", "api_key": "sk-test"},
        "agents": {"orchestrator": {"name": "M", "role": "r"}},
        "// tool_security": "설명",
        "tool_security": {"ask": ["mcp(git/git_checkout)"], "allow": ["net(a.com)", "net(b.com)"]},
    }, ensure_ascii=False), encoding="utf-8")
    get_config(reload=True, config_path=path)
    yield path
    # 다른 시험이 이 임시 파일을 읽지 않도록 전역 설정을 비웁니다.
    monkeypatch.setattr(loader, "_config", None)


@pytest_asyncio.fixture
async def factory():
    """이 파일만의 빈 DB. 감사는 대화를 가로질러 읽으므로 다른 시험의 기록이 섞이면 안 됩니다."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def notes(monkeypatch):
    shown: List[Any] = []
    monkeypatch.setattr(ui, "notify", lambda message, **kw: shown.append((str(message), kw.get("type"))))
    return shown


def _rules(path, effect):
    return read_conf_file(path)["tool_security"][effect]


async def _session(factory, sid: str, title: str, created_at: datetime = None) -> None:
    async with factory() as db:
        fields = {"created_at": created_at} if created_at else {}
        db.add(SessionModel(id=sid, title=title, strategy="sequential_debate", max_rounds=1, active_agents=[], **fields))
        await db.commit()


async def _call(factory, sid: str, decision: str, *, status="denied", rule="", tool="filesystem__read_file",
                agent="coder", arguments=None, at: datetime = None, output="REFUSED") -> None:
    async with factory() as db:
        db.add(ToolCallRecordModel(
            session_id=sid, agent_key=agent, tool_name=tool, arguments=arguments or {"path": ".env"},
            output=output, status=status, decision=decision, rule=rule,
            created_at=at or datetime.now(timezone.utc),
        ))
        await db.commit()


# ------------------------------------------------------------------ conf.json 되쓰기


def test_removing_a_rule_leaves_the_rest_and_the_comments(conf):
    assert remove_tool_security_rule_from_conf_file("allow", "net(a.com)", conf) is True
    data = read_conf_file(conf)
    assert data["tool_security"]["allow"] == ["net(b.com)"]
    assert data["// tool_security"] == "설명"
    assert get_config().tool_security.allow == ["net(b.com)"], "돌고 있는 설정도 함께 바뀝니다"


def test_removing_a_rule_that_is_gone_writes_nothing(conf):
    before = conf.read_bytes()
    assert remove_tool_security_rule_from_conf_file("allow", "net(gone.com)", conf) is False
    assert conf.read_bytes() == before


def test_removing_a_default_deny_rule_keeps_the_other_defaults(conf):
    """`deny` 를 파일에 적으면 기본 목록을 대신합니다. 한 줄 지운다고 나머지 보호가 사라지면 안 됩니다."""
    assert "deny" not in read_conf_file(conf)["tool_security"]
    assert remove_tool_security_rule_from_conf_file("deny", "net(ngrok.io)", conf) is True
    assert _rules(conf, "deny") == [r for r in DEFAULT_TOOL_DENY_RULES if r != "net(ngrok.io)"]


def test_removing_every_deny_rule_stays_empty_after_reload(conf):
    """빈 목록도 사람이 고른 값입니다. 다시 읽을 때 기본 목록으로 되살아나면 지운 것이 무효가 됩니다."""
    for rule in DEFAULT_TOOL_DENY_RULES:
        remove_tool_security_rule_from_conf_file("deny", rule, conf)
    assert _rules(conf, "deny") == []
    assert get_config(reload=True, config_path=conf).tool_security.deny == []


def test_replacing_a_rule_keeps_its_place(conf):
    assert replace_tool_security_rule_in_conf_file("allow", "net(a.com)", "net(c.com)", conf) is True
    assert _rules(conf, "allow") == ["net(c.com)", "net(b.com)"]


def test_replacing_with_a_rule_already_listed_leaves_one_line(conf):
    assert replace_tool_security_rule_in_conf_file("allow", "net(a.com)", "net(b.com)", conf) is True
    assert _rules(conf, "allow") == ["net(b.com)"]


def test_replacing_a_rule_that_is_gone_writes_nothing(conf):
    before = conf.read_bytes()
    assert replace_tool_security_rule_in_conf_file("allow", "net(gone.com)", "net(c.com)", conf) is False
    assert conf.read_bytes() == before


@pytest.mark.parametrize("bad", ["nonsense(", "", "   "])
def test_a_bad_rule_never_reaches_the_file(conf, bad):
    before = conf.read_bytes()
    with pytest.raises(ValueError):
        replace_tool_security_rule_in_conf_file("allow", "net(a.com)", bad, conf)
    assert conf.read_bytes() == before


def test_a_rule_list_written_as_one_string_can_be_edited(tmp_path):
    """설정은 문자열 하나도 목록으로 읽습니다. 고칠 때도 같은 말을 들어야 합니다."""
    path = tmp_path / "conf.json"
    path.write_text(json.dumps({"tool_security": {"allow": "  net(a.com) "}}), encoding="utf-8")
    assert remove_tool_security_rule_from_conf_file("allow", "net(a.com)", path) is True
    assert _rules(path, "allow") == []
    with pytest.raises(ValueError):
        remove_tool_security_rule_from_conf_file("mode", "x", path)


def test_restoring_defaults_keeps_rules_added_by_hand(conf):
    service = RosterService()
    add_tool_security_rules_to_conf_file("deny", ["net(x.com)"], conf)
    remove_tool_security_rule_from_conf_file("deny", "net(ngrok.io)", conf)
    assert service.restore_default_deny_rules() == ["net(ngrok.io)"]
    deny = _rules(conf, "deny")
    assert "net(x.com)" in deny and set(DEFAULT_TOOL_DENY_RULES) <= set(deny)


@pytest.mark.asyncio
async def test_a_running_gate_follows_the_file_from_the_next_call(conf, tmp_path):
    """보안은 대화에 고정되지 않습니다. 진행 중인 게이트가 다음 호출부터 고친 규칙을 씁니다."""
    service = RosterService()
    manager = _manager(tmp_path)
    gate = ToolGate(session_id="s", control=None)
    args = {"path": "notes.txt"}

    assert (await gate.check(_agent(), "filesystem__read_file", args, manager)).allowed
    assert service.add_tool_rule("deny", "read(notes.txt)") is True
    blocked = await gate.check(_agent(), "filesystem__read_file", args, manager)
    assert not blocked.allowed and blocked.audit["rule"] == "read(notes.txt)"
    assert service.remove_tool_rule("deny", "read(notes.txt)") is True
    assert (await gate.check(_agent(), "filesystem__read_file", args, manager)).allowed


# ------------------------------------------------------------------ 감사 조회


@pytest.mark.asyncio
async def test_only_calls_blocked_by_settings_are_listed(factory):
    await _session(factory, "s1", "대화")
    await _call(factory, "s1", "deny", rule="read(**/.env)")
    await _call(factory, "s1", "hard", rule="고정 보호")
    for decision, status in (("allow", "success"), ("approved", "success"), ("rejected", "denied"), ("timeout", "denied"),
                             ("", "success"), ("hard", "error")):
        await _call(factory, "s1", decision, status=status, rule="문서 형식" if status == "error" else "")

    calls, total = await blocked_tool_calls(factory)

    assert total == 2
    assert sorted(c.decision for c in calls) == ["deny", "hard"]
    assert {c.verdict for c in calls} == {"규칙 차단 · read(**/.env)", "고정 보호"}
    assert calls[0].session_title == "대화"


@pytest.mark.asyncio
async def test_real_gate_refusals_are_the_ones_listed(conf, factory, tmp_path):
    """게이트가 실제로 남기는 값으로 기록해 봅니다. 칸 모양이 어긋나면 감사에 아무것도 안 뜹니다."""
    await _session(factory, "s1", "대화")
    manager = _manager(tmp_path)
    gate = ToolGate(session_id="s1", control=None)
    agent = _agent()
    cases = [
        ("filesystem__read_file", {"path": ".env"}),                              # 기본 거부 규칙
        ("filesystem__read_file", {"path": ".memory-graphs/other.jsonl"}),        # 고정 보호
        ("filesystem__write_file", {"path": "report.docx", "content": "x"}),      # 문서 형식 (보안 아님)
        ("fetch__fetch", {"url": "https://example.com"}),                         # 물어볼 사람이 없음
        ("filesystem__read_file", {"path": "notes.txt"}),                         # 허용
    ]
    async with factory() as db:
        for tool, arguments in cases:
            result = await gate.check(agent, tool, arguments, manager)
            db.add(TurnRecorder.tool_row(
                SimpleNamespace(session_id="s1", turn_id=None), agent,
                {"tool_name": tool, "arguments": arguments, "output": result.output,
                 "status": result.status or "success", "security": result.audit},
                message_id=None,
            ))
        await db.commit()

    calls, total = await blocked_tool_calls(factory)

    assert total == 3
    assert {c.verdict for c in calls} == {
        "규칙 차단 · read(**/.env)", "고정 보호", "응답 없음 · 물어볼 유저가 없었음",
    }


@pytest.mark.asyncio
async def test_filters_and_limit(factory):
    await _session(factory, "s1", "하나")
    await _session(factory, "s2", "둘")
    now = datetime.now(timezone.utc)
    await _call(factory, "s1", "deny", at=now - timedelta(minutes=3), arguments={"path": "oldest"})
    await _call(factory, "s1", "hard", at=now - timedelta(minutes=2), arguments={"path": "middle"})
    await _call(factory, "s2", "deny", at=now - timedelta(minutes=1), arguments={"path": "newest"})

    calls, total = await blocked_tool_calls(factory, limit=2)
    assert total == 3 and [c.session_id for c in calls] == ["s2", "s1"], "최근 것부터, 건수는 자르기 전 값"
    assert "newest" in calls[0].arguments_text

    assert [c.decision for c in (await blocked_tool_calls(factory, session_id="s1"))[0]] == ["hard", "deny"]
    assert (await blocked_tool_calls(factory, decision="hard"))[1] == 1
    with pytest.raises(ValueError):
        await blocked_tool_calls(factory, decision="allow")


@pytest.mark.asyncio
async def test_a_forked_conversations_copies_are_counted_once(factory):
    """갈라 낸 대화는 기록을 시각까지 복사합니다. 전체 보기에서는 원본 하나로, 그 대화만 볼 때는 그 대화의 것으로."""
    now = datetime.now(timezone.utc)
    await _session(factory, "orig", "원본", created_at=now - timedelta(days=1))
    await _session(factory, "fork", "원본 (분기)", created_at=now)
    await _call(factory, "orig", "deny", at=now)
    await _call(factory, "fork", "deny", at=now)

    calls, total = await blocked_tool_calls(factory)
    assert total == 1 and calls[0].session_id == "orig"

    calls, total = await blocked_tool_calls(factory, session_id="fork")
    assert total == 1 and calls[0].session_id == "fork"


@pytest.mark.asyncio
async def test_calls_in_the_same_clock_tick_are_not_mistaken_for_copies(factory):
    """시각 해상도가 낮으면 한 대화의 연속 호출도 시각이 같습니다. 복사본이 아니므로 그대로 셉니다."""
    now = datetime.now(timezone.utc)
    await _session(factory, "s1", "대화")
    await _call(factory, "s1", "deny", at=now)
    await _call(factory, "s1", "deny", at=now)
    assert (await blocked_tool_calls(factory))[1] == 2


@pytest.mark.asyncio
async def test_long_arguments_are_clipped(factory):
    """`write_file` 의 인자에는 파일 내용이 통째로 들어 있습니다."""
    await _session(factory, "s1", "대화")
    await _call(factory, "s1", "deny", arguments={"content": "가" * (ARGUMENTS_TEXT_LIMIT * 2)})
    text = (await blocked_tool_calls(factory))[0][0].arguments_text
    assert len(text) < ARGUMENTS_TEXT_LIMIT + 40 and "총" in text


# ------------------------------------------------------------------ 창: 전역 규칙


def _panel(local: bool = True, opened: List[Any] = None) -> ToolSecurityDialog:
    async def on_open_call(session_id, message_id):
        if opened is not None:
            opened.append((session_id, message_id))

    state = RosterState(agent_pool=SimpleNamespace(list_all=lambda: []))
    state.session_id = "s1"
    return ToolSecurityDialog(state, RosterService(), viewer_is_local=lambda: local, on_open_call=on_open_call)


def _walk(element):
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def _texts(element) -> List[str]:
    return [str(e.text) for e in _walk(element) if getattr(e, "text", "")]


def _buttons(element) -> List[str]:
    return [str(e.props.get("icon") or e.text) for e in _walk(element) if isinstance(e, ui.button)]


@pytest.fixture
def audit_db(factory, monkeypatch):
    monkeypatch.setattr(roster_service, "get_session_factory", lambda: factory)
    return factory


@pytest.mark.asyncio
async def test_the_panel_lists_rules_with_their_defaults_marked(conf, audit_db, ui_page):
    panel = _panel()
    with ui_page:
        await panel.open()
    texts = _texts(panel._rules_body)  # noqa: SLF001
    assert f"거부 {len(DEFAULT_TOOL_DENY_RULES)}" in texts and "묻기 1" in texts and "허용 2" in texts
    assert "read(**/.env)" in texts and "net(a.com)" in texts and "기본" in texts
    assert any("기본 목록까지" in t for t in texts), "deny 를 처음 고치면 무슨 일이 일어나는지 미리 밝힙니다"


@pytest.mark.asyncio
async def test_a_remote_viewer_only_reads(conf, audit_db, ui_page, notes):
    panel = _panel(local=False)
    with ui_page:
        await panel.open()
    body = panel._rules_body  # noqa: SLF001
    assert any("서버 PC 에서만" in t for t in _texts(body)) and "net(a.com)" in _texts(body)
    assert _buttons(body) == [] and not [e for e in _walk(body) if isinstance(e, ui.input)]


@pytest.mark.asyncio
async def test_a_remote_viewer_cannot_write_even_if_the_handler_fires(conf, audit_db, ui_page, notes):
    """화면을 안 그리는 것이 전부면 낡은 화면이나 만든 이벤트가 그대로 파일에 닿습니다."""
    panel = _panel(local=False)
    before = conf.read_bytes()
    with ui_page:
        await panel.open()
        panel.add_rule("allow", "net(c.com)")
        panel.save_edit("allow", "net(a.com)", "net(c.com)")
        panel.remove_rule("allow", "net(a.com)")
        panel.restore_defaults()
    assert conf.read_bytes() == before
    assert len(notes) == 4 and all(kind == "warning" for _, kind in notes)


@pytest.mark.asyncio
async def test_adding_a_rule_from_the_panel(conf, audit_db, ui_page, notes):
    panel = _panel()
    with ui_page:
        await panel.open()
        panel.add_rule("allow", " net(c.com) ")
    assert _rules(conf, "allow") == ["net(a.com)", "net(b.com)", "net(c.com)"]
    assert "net(c.com)" in _texts(panel._rules_body) and "허용 3" in _texts(panel._rules_body)  # noqa: SLF001
    assert notes[-1][1] == "positive"

    with ui_page:
        panel.add_rule("allow", "net(c.com)")
    assert notes[-1] == ("이미 있는 규칙입니다: net(c.com)", "info")


@pytest.mark.asyncio
async def test_a_bad_rule_is_explained_and_the_file_stays(conf, audit_db, ui_page, notes):
    panel = _panel()
    before = conf.read_bytes()
    with ui_page:
        await panel.open()
        field = next(e for e in _walk(panel._rules_body) if isinstance(e, ui.input))  # noqa: SLF001
        field.value = "nonsense("
        panel.add_rule("deny", field.value)
    assert conf.read_bytes() == before
    assert "읽을 수 없습니다" in notes[-1][0] and notes[-1][1] == "warning"
    assert not field.is_deleted and field.value == "nonsense(", "입력칸을 남겨 고칠 수 있게 합니다"


@pytest.mark.asyncio
async def test_a_rule_shadowed_by_a_stronger_list_is_called_out(conf, audit_db, ui_page, notes):
    panel = _panel()
    with ui_page:
        await panel.open()
        panel.add_rule("allow", "mcp(git/git_checkout)")
    assert any("묻기 쪽에 있어" in text for text, _ in notes)


@pytest.mark.asyncio
async def test_removing_a_restriction_asks_first_but_removing_an_allowance_does_not(conf, audit_db, ui_page, notes):
    panel = _panel()
    with ui_page:
        await panel.open()
        panel.ask_remove("allow", "net(a.com)")
        assert _rules(conf, "allow") == ["net(b.com)"], "허용을 지우면 조여질 뿐이라 묻지 않습니다"

        panel.ask_remove("ask", "mcp(git/git_checkout)")
        assert _rules(conf, "ask") == ["mcp(git/git_checkout)"]
        assert any("제한이 풀립니다" in t for t in _texts(panel._rules_body))  # noqa: SLF001

        panel.cancel()
        assert not any("제한이 풀립니다" in t for t in _texts(panel._rules_body))  # noqa: SLF001

        panel.ask_remove("ask", "mcp(git/git_checkout)")
        panel.remove_rule("ask", "mcp(git/git_checkout)")
    assert _rules(conf, "ask") == []
    assert not any("제한이 풀립니다" in t for t in _texts(panel._rules_body))  # noqa: SLF001


@pytest.mark.asyncio
async def test_editing_a_rule_in_place(conf, audit_db, ui_page, notes):
    panel = _panel()
    with ui_page:
        await panel.open()
        panel.start_edit("allow", "net(a.com)")
        editor = next(e for e in _walk(panel._rules_body) if isinstance(e, ui.input) and e.value == "net(a.com)")  # noqa: SLF001
        assert editor is not None

        panel.save_edit("allow", "net(a.com)", "nonsense(")
        assert _rules(conf, "allow") == ["net(a.com)", "net(b.com)"]
        assert not editor.is_deleted, "틀리면 고치던 칸을 그대로 둡니다"

        panel.save_edit("allow", "net(a.com)", "net(c.com)")
    assert _rules(conf, "allow") == ["net(c.com)", "net(b.com)"]
    assert "net(c.com)" in _texts(panel._rules_body)  # noqa: SLF001


@pytest.mark.asyncio
async def test_restoring_defaults_is_offered_only_when_something_is_missing(conf, audit_db, ui_page, notes):
    panel = _panel()
    with ui_page:
        await panel.open()
        assert "restore" not in _buttons(panel._rules_body)  # noqa: SLF001
        panel.ask_remove("deny", "net(ngrok.io)")
        panel.remove_rule("deny", "net(ngrok.io)")
        assert "restore" in _buttons(panel._rules_body)  # noqa: SLF001
        panel.restore_defaults()
        assert "restore" not in _buttons(panel._rules_body)  # noqa: SLF001
    assert "net(ngrok.io)" in _rules(conf, "deny")


@pytest.mark.asyncio
async def test_an_unreadable_config_does_not_break_the_panel(conf, audit_db, ui_page, monkeypatch):
    panel = _panel()
    monkeypatch.setattr(panel.service, "tool_security", lambda: None)
    with ui_page:
        await panel.open()
    assert any("읽지 못해" in t for t in _texts(panel._rules_body))  # noqa: SLF001


# ------------------------------------------------------------------ 창: 보안 감사


@pytest.mark.asyncio
async def test_the_audit_tab_lists_filters_and_opens_a_call(conf, audit_db, ui_page, notes):
    await _session(audit_db, "s1", "이 대화")
    await _session(audit_db, "s2", "다른 대화")
    now = datetime.now(timezone.utc)
    await _call(audit_db, "s1", "deny", rule="read(**/.env)", at=now - timedelta(minutes=2), output="REFUSED 이유")
    await _call(audit_db, "s2", "hard", rule="고정 보호", at=now - timedelta(minutes=1))
    opened: List[Any] = []
    panel = _panel(opened=opened)

    with ui_page:
        await panel.open()
        table = panel._table  # noqa: SLF001
        assert [r["session"] for r in table.rows] == ["다른 대화", "이 대화"]
        assert panel._count.text == "차단 2건"  # noqa: SLF001

        await panel._on_scope_change(SimpleNamespace(value=True))  # noqa: SLF001
        assert [r["session"] for r in table.rows] == ["이 대화"]

        await panel._on_scope_change(SimpleNamespace(value=False))  # noqa: SLF001
        await panel._on_decision_change(SimpleNamespace(value="hard"))  # noqa: SLF001
        assert [r["session"] for r in table.rows] == ["다른 대화"]

        await panel._on_decision_change(SimpleNamespace(value=""))  # noqa: SLF001
        row = next(r for r in table.rows if r["session"] == "이 대화")
        panel._on_row_click(SimpleNamespace(args=[{}, row, 1]))  # noqa: SLF001
        detail = _texts(panel._detail)  # noqa: SLF001
        assert "판정: 규칙 차단 · read(**/.env)" in detail and "REFUSED 이유" in detail

        await panel._open_call(panel._calls[row["id"]])  # noqa: SLF001
    assert opened == [("s1", None)]
    assert panel._dialog.value is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_the_audit_tab_loads_more_on_request(conf, audit_db, ui_page, notes, monkeypatch):
    monkeypatch.setattr("app.ui.components.roster.tool_security_panel.AUDIT_PAGE_SIZE", 2)
    await _session(audit_db, "s1", "대화")
    now = datetime.now(timezone.utc)
    for minutes in range(3):
        await _call(audit_db, "s1", "deny", at=now - timedelta(minutes=minutes), arguments={"path": f"{minutes}"})
    panel = _panel()
    panel._limit = 2  # noqa: SLF001

    with ui_page:
        await panel.open()
        assert len(panel._table.rows) == 2 and panel._count.text == "차단 3건 중 최근 2건"  # noqa: SLF001
        assert panel._more.visible is True  # noqa: SLF001
        await panel._load_more()  # noqa: SLF001
        assert len(panel._table.rows) == 3 and panel._count.text == "차단 3건"  # noqa: SLF001
        assert panel._more.visible is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_refresh_keeps_a_selection_only_while_the_call_is_listed(conf, audit_db, ui_page, notes):
    await _session(audit_db, "s1", "대화")
    await _call(audit_db, "s1", "deny", rule="read(**/.env)")
    panel = _panel()
    with ui_page:
        await panel.open()
        row = panel._table.rows[0]  # noqa: SLF001
        panel._on_row_click(SimpleNamespace(args=[{}, row, 0]))  # noqa: SLF001
        await panel.load_audit()
        assert _texts(panel._detail), "다시 읽어도 펼친 내용은 남습니다"  # noqa: SLF001

        await panel._on_decision_change(SimpleNamespace(value="hard"))  # noqa: SLF001
        assert not _texts(panel._detail), "목록에서 사라진 호출을 펼쳐 두지 않습니다"  # noqa: SLF001
