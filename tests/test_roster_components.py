"""로스터의 하위 컴포넌트를 하나씩 — `AgentRosterControl` 없이.

컴포넌트는 `RosterState`, 서비스, 콜백을 생성자로 받습니다. 그래서 필요한 것만 흉내 낸 서비스를 주고 혼자 그려
보면 됩니다. 패널을 통째로 세우거나 여러 모듈의 이름을 동시에 바꿔 끼울 일이 없습니다.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from nicegui import ui

from app import graph_store
from app.agents.pool import AgentPool
from app.agents.skills import Skill
from app.config import AgentConfig
from app.orchestration.graph import graph_from_card_order
from app.orchestration.graph_run import GraphRunTracker
from app.orchestration.roster_service import Notice, ReloadResult
from app.ui.components.roster.agent_admin import AgentAdminComponent
from app.ui.components.roster.agent_cards import AgentCardsComponent
from app.ui.components.roster.agent_dialogs import AgentDialogs
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.graph_row import GraphRowComponent
from app.ui.components.roster.mcp_panel import McpPanelComponent
from app.ui.components.roster.session_settings import SessionSettingsComponent, ToolRulesDialog
from app.ui.components.roster.skills_panel import SkillsPanelComponent
from app.ui.components.roster.state import RosterState
from app.ui.components.roster.workspace_row import WorkspaceRowComponent


def _pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=key.title(), role=key, model="openai/gpt-4o-mini", api_key="sk-test", debate_priority=i * 10)
        for i, key in enumerate(("orchestrator", "architect", "coder", "critic"))
    })


class StubService:
    """컴포넌트가 묻는 것만 답하는 서비스. 쓰기는 기록만 합니다."""

    def __init__(self):
        self.writes: List[Any] = []
        self.lock_reason = ""
        self.status: Dict[str, Any] = {}
        self.servers: Dict[str, Any] = {}
        self.skills: List[Skill] = []
        self.runtime_object = None
        self.running: List[str] = []
        self.write_failure = None
        self.apply_notice = Notice("반영했습니다", "positive")
        self.reload_result = ReloadResult(None, (Notice("읽지 못했습니다", "negative", multi_line=True),))

    def running_debate_lock_reason(self):
        return self.lock_reason

    def is_running(self, session_id):
        return session_id in self.running

    def mcp_view(self, workspace):
        return self.status, self.servers

    def runtime_status(self, workspace):
        return self.status

    def runtime(self, workspace):
        return self.runtime_object

    def skills_view(self):
        return self.skills, Path("skills")

    def default_tool_mode(self):
        return "default"

    def configured_agents(self):
        return {}

    def write_conf(self, write):
        if self.write_failure is not None:
            return self.write_failure
        write()
        return None

    async def apply_to_running_app(self, done_message, restart_servers=True):
        self.writes.append(("apply", done_message, restart_servers))
        return self.apply_notice

    async def reload_conf(self, pool):
        return self.reload_result

    def __getattr__(self, name):
        # set_mcp_enabled, set_debate_order … 이름 있는 쓰기는 전부 기록합니다.
        def record(*args, **kwargs):
            self.writes.append((name, *args))
        return record


@pytest.fixture
def notes(monkeypatch):
    shown: List[Any] = []
    monkeypatch.setattr(ui, "notify", lambda message, **kw: shown.append((str(message), kw.get("type"))))
    return shown


@pytest.fixture
def state() -> RosterState:
    return RosterState(agent_pool=_pool())


def _changes(state, service, **overrides) -> RosterChanges:
    wiring = dict(
        alive=lambda: True, toast_host=lambda: None, on_lock_changed=lambda: None, on_applied=lambda: None,
        on_write_failed=lambda: None,
    )
    wiring.update(overrides)
    return RosterChanges(state, service, **wiring)


# ------------------------------------------------------------------ RosterChanges: 변경 절차


@pytest.mark.asyncio
async def test_a_change_writes_then_tells_then_redraws(state, notes):
    service = StubService()
    order: List[str] = []
    changes = _changes(state, service, on_applied=lambda: order.append("redraw"))

    await changes.apply_conf_change(lambda: order.append("write"), "저장", restart_servers=False)

    assert order == ["write", "redraw"]
    assert service.writes == [("apply", "저장", False)]
    assert notes == [("반영했습니다", "positive")]


@pytest.mark.asyncio
async def test_a_failed_write_stops_there(state, notes):
    service = StubService()
    service.write_failure = Notice("못 고쳤습니다", "negative")
    failed, applied = [], []
    changes = _changes(state, service, on_write_failed=lambda: failed.append(1), on_applied=lambda: applied.append(1))

    await changes.apply_conf_change(lambda: None, "저장")

    assert notes == [("못 고쳤습니다", "negative")]
    assert failed == [1] and applied == [], "쓰지 못했으면 앱에 반영할 것도 없습니다"
    assert service.writes == []


@pytest.mark.asyncio
async def test_an_agent_change_also_saves_this_conversation(state, notes):
    saved: List[int] = []

    async def on_config_changed():
        saved.append(1)

    changes = _changes(state, StubService(), on_config_changed=on_config_changed)
    await changes.apply_agent_change(lambda: None, "저장")
    assert saved == [1]


def test_the_lock_is_redrawn_only_when_it_changes(state):
    service = StubService()
    redraws: List[int] = []
    changes = _changes(state, service, on_lock_changed=lambda: redraws.append(1))

    changes.sync_lock()
    assert redraws == [] and state.mcp_locked is False

    service.lock_reason = "토론 중"
    changes.sync_lock()
    changes.sync_lock()
    assert redraws == [1] and state.mcp_locked is True and state.mcp_lock_reason == "토론 중"


def test_a_dead_page_does_not_touch_the_lock(state):
    service = StubService()
    service.lock_reason = "토론 중"
    redraws: List[int] = []
    changes = _changes(state, service, alive=lambda: False, on_lock_changed=lambda: redraws.append(1))

    changes.sync_lock()

    assert redraws == [] and state.mcp_locked is False


def test_blocked_checks_look_at_the_lock_again_and_say_why(state, notes):
    service = StubService()
    changes = _changes(state, service)
    assert changes.blocked_by_running_debate() is False
    assert changes.blocked_for_agent_admin() is False

    state.personas_locked = True
    assert changes.blocked_by_running_debate() is False, "시작된 대화는 MCP 구성을 막지 않습니다"
    assert changes.blocked_for_agent_admin() is True
    assert "고정" in notes[-1][0]

    service.lock_reason = "토론 중"
    assert changes.blocked_by_running_debate() is True
    assert notes[-1] == ("토론 중", "warning")


# ------------------------------------------------------------------ 스킬 칸 (서비스만 받습니다)


def _skill(name: str, *, enabled: bool = True, problem: str = "") -> Skill:
    return SimpleNamespace(
        name=name, enabled=enabled, problem=problem, title=name, description="설명", files=(), scripts=(),
        path=Path(name), usable=enabled and not problem,
    )


def test_skills_redraw_only_when_the_folder_changed():
    service = StubService()
    service.skills = [_skill("alpha")]
    panel = SkillsPanelComponent(service)
    panel.build()
    assert panel.badge.text == "1/1 켜짐"
    seen = panel.seen

    panel.refresh()
    assert panel.seen is seen, "바뀐 것이 없으면 다시 그리지 않습니다"

    service.skills = [_skill("alpha"), _skill("beta", enabled=False)]
    panel.refresh()
    assert panel.seen != seen and panel.badge.text == "1/2 켜짐"


@pytest.mark.asyncio
async def test_a_skill_toggle_goes_through_the_service(notes):
    service = StubService()
    panel = SkillsPanelComponent(service)
    await panel.on_toggle("alpha", False)
    assert service.writes == [("set_skill_enabled", "alpha", False)]
    assert "비활성화" in notes[0][0]


# ------------------------------------------------------------------ MCP 칸


def _mcp(state, service, **changes_overrides):
    changes = _changes(state, service, **changes_overrides)
    panel = McpPanelComponent(state, service, changes, alive=lambda: True)
    panel.build()
    return panel, changes


def test_the_chips_follow_this_conversations_runtime(state):
    service = StubService()
    service.servers = {"filesystem": SimpleNamespace(enabled=True)}
    panel, _ = _mcp(state, service)
    assert panel.status_seen == ()
    assert panel.badge.text == "0/1 연결됨"

    service.status = {"filesystem": {"connected": True, "available": True, "tool_count": 14, "command": "node"}}
    panel.sync_runtime()

    assert panel.status_seen == (("filesystem", True, 14),)
    assert panel.badge.text == "1/1 연결됨"


def test_the_panel_shows_the_lock_it_is_told_about(state):
    service = StubService()
    panel, changes = _mcp(state, service, on_lock_changed=lambda: panel.apply_lock())
    assert panel.lock_hint.visible is False

    service.lock_reason = "토론 중"
    changes.sync_lock()

    assert panel.lock_badge.visible is True and panel.lock_hint.visible is True
    assert panel.add_btn._props.get("disable") is True


@pytest.mark.asyncio
async def test_a_toggle_is_written_once_nothing_is_running(state, notes, ui_page):
    service = StubService()
    with ui_page:
        panel, _ = _mcp(state, service)

    await panel.on_toggle("git", False)

    assert ("set_mcp_enabled", "git", False) in service.writes
    assert ("apply", "'git' 서버를 비활성화하였습니다.", True) in service.writes


@pytest.mark.asyncio
async def test_a_toggle_is_refused_while_a_debate_runs(state, notes, ui_page):
    service = StubService()
    with ui_page:
        panel, _ = _mcp(state, service)
    service.lock_reason = "토론 중"

    await panel.on_toggle("git", False)

    assert service.writes == []
    assert notes[-1] == ("토론 중", "warning")


@pytest.mark.asyncio
async def test_reconnect_needs_a_runtime(state, notes, ui_page):
    with ui_page:
        panel, _ = _mcp(state, StubService())
    await panel.on_reconnect()
    assert "첫 토론을 시작하면" in notes[0][0]


# ------------------------------------------------------------------ 카드 줄 (메뉴는 콜백입니다)


def _cards(state, service=None, **callbacks):
    calls: Dict[str, List[Any]] = {name: [] for name in ("tools", "skills", "delete", "disable", "selection", "disabled")}
    service = service or StubService()
    wiring = dict(
        open_tools=lambda k: calls["tools"].append(k),
        open_skills=lambda k: calls["skills"].append(k),
        open_delete=lambda k: calls["delete"].append(k),
        disable_agent=lambda k: calls["disable"].append(k),
        on_selection_changed=lambda: calls["selection"].append(1),
        refresh_disabled=lambda: calls["disabled"].append(1),
    )
    wiring.update(callbacks)
    cards = AgentCardsComponent(state, service, _changes(state, service), **wiring)
    cards.build()
    return cards, calls


def test_cards_are_drawn_from_the_state_alone(state):
    cards, _ = _cards(state)
    assert cards.built
    names = [e.text for e in cards.cards_row.descendants() if getattr(e, "text", None) in ("Architect", "Coder", "Critic")]
    assert names == ["Architect", "Coder", "Critic"]


def test_a_toggle_updates_the_state_and_tells_the_panel(state):
    cards, calls = _cards(state)
    cards.on_toggle("coder", False)
    assert state.selected_agents["coder"] is False
    assert calls["selection"] == [1]
    assert "coder" not in [a.key for a in state.speaking_order()]


def test_select_all_is_refused_where_the_checkboxes_are_locked(state):
    cards, calls = _cards(state)
    state.personas_locked = True
    cards.on_select_all(False)
    assert all(state.selected_agents.values())
    assert calls["selection"] == []


def test_the_view_option_redraws_the_disabled_row_too(state):
    cards, calls = _cards(state)
    cards.set_view_option("hide_inactive", True)
    assert state.hide_inactive is True and calls["disabled"] == [1]


@pytest.mark.asyncio
async def test_a_drop_writes_the_new_order(state, notes, ui_page):
    service = StubService()
    with ui_page:
        cards, _ = _cards(state, service)
    cards.on_drag_start("critic")

    await cards.on_drop_on("architect", after=False)

    assert ("set_debate_order", ["orchestrator", "critic", "architect", "coder"]) in service.writes
    assert state.dragging_key is None


@pytest.mark.asyncio
async def test_a_drop_is_refused_once_the_conversation_started(state, notes, ui_page):
    service = StubService()
    with ui_page:
        cards, _ = _cards(state, service)
    state.personas_locked = True
    cards.on_drag_start("critic")

    await cards.on_drop_on("architect")

    assert service.writes == []


# ------------------------------------------------------------------ 카드별 창과 관리


def test_dialogs_stay_shut_while_the_roster_is_locked(state, notes, monkeypatch):
    opened: List[str] = []
    monkeypatch.setattr(ui, "dialog", lambda *a, **k: opened.append("dialog"))
    service = StubService()
    state.personas_locked = True
    dialogs = AgentDialogs(state, service, _changes(state, service))

    dialogs.open_tools("coder")
    dialogs.open_skills("coder")

    assert opened == []


def test_an_unknown_agent_is_reported(state, notes):
    service = StubService()
    dialogs = AgentDialogs(state, service, _changes(state, service))
    dialogs.open_tools("nope")
    assert "'nope'" in notes[0][0]


@pytest.mark.asyncio
async def test_a_broken_reload_leaves_the_pool_and_redraws_anyway(state, notes):
    service = StubService()
    redraws: List[int] = []
    admin = AgentAdminComponent(
        state, service, _changes(state, service), alive=lambda: True, on_reloaded=lambda: redraws.append(1),
    )
    pool = state.agent_pool

    await admin.on_reload_conf()

    assert state.agent_pool is pool
    assert notes == [("읽지 못했습니다", "negative")]
    assert redraws == [1]


@pytest.mark.asyncio
async def test_a_reload_syncs_the_selection_with_the_new_pool(state, notes):
    service = StubService()
    new_pool = AgentPool({
        key: AgentConfig(name=key.title(), role=key, model="openai/gpt-4o-mini", api_key="sk-test")
        for key in ("orchestrator", "researcher")
    })
    service.reload_result = ReloadResult(new_pool, (Notice("다시 읽었습니다", "positive", multi_line=True),))
    admin = AgentAdminComponent(state, service, _changes(state, service), alive=lambda: True, on_reloaded=lambda: None)

    await admin.on_reload_conf()

    assert state.agent_pool is new_pool
    assert state.selected_agents == {"orchestrator": True, "researcher": True}


@pytest.mark.asyncio
async def test_disable_goes_through_the_lock(state, notes):
    service = StubService()
    admin = AgentAdminComponent(state, service, _changes(state, service), alive=lambda: True, on_reloaded=lambda: None)

    await admin.disable_agent("critic")
    assert ("set_agent_enabled", "critic", False) in service.writes

    service.writes.clear()
    state.personas_locked = True
    await admin.disable_agent("critic")
    assert service.writes == []


# ------------------------------------------------------------------ 대화 설정


def _settings(state, service=None, **callbacks):
    service = service or StubService()

    async def on_rules(_grants, _denials):
        return None

    rules = ToolRulesDialog(state, on_changed=on_rules)
    wiring = dict(on_strategy_changed=lambda: None, on_parallel_changed=lambda: None)
    wiring.update(callbacks)
    settings = SessionSettingsComponent(state, service, _changes(state, service), tool_rules=rules, **wiring)
    settings.build_controls()
    settings.build_notes()
    return settings, rules


def test_a_new_strategy_shows_the_parallel_limit_and_tells_the_panel(state, monkeypatch):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    told: List[int] = []
    settings, _ = _settings(state, on_strategy_changed=lambda: told.append(1))
    assert settings.parallel_row.visible is False

    settings.strategy_select.set_value("parallel_dispatch")

    assert state.strategy_name == "parallel_dispatch"
    assert settings.parallel_row.visible is True
    assert told == [1]


def test_settings_keep_their_values_in_the_state(state, monkeypatch):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    settings, _ = _settings(state)

    settings.rounds_slider.set_value(5)
    settings.budget_input.set_value(1000)
    settings.custom_instr_input.set_value("새 지침")

    assert (state.max_rounds, state.token_budget, state.custom_instructions) == (5, 1000, "새 지침")
    assert settings.rounds_label.text == "5"


def test_opening_a_session_moves_the_values_to_the_handles(state, monkeypatch):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    settings, _ = _settings(state)
    state.max_rounds, state.custom_instructions = 4, "지침"

    settings.sync_from_state()

    assert settings.rounds_slider.value == 4 and settings.custom_instr_input.value == "지침"
    assert settings.rounds_label.text == "4"


def test_the_sub_debate_switch_is_off_until_turned_on_and_follows_the_session(state, monkeypatch):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    settings, _ = _settings(state)
    assert state.sub_debates is False and settings.sub_debate_switch.value is False, "호출이 늘어나므로 켜야 돕니다"

    settings.sub_debate_switch.set_value(True)
    assert state.sub_debates is True

    state.sub_debates = False
    settings.sync_from_state()
    assert settings.sub_debate_switch.value is False


def test_the_adaptive_strategy_shows_the_parallel_limit_too(state, monkeypatch):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    settings, _ = _settings(state)
    settings.strategy_select.set_value("adaptive_graph")
    assert settings.parallel_row.visible is True, "그린 그래프도 병렬로 돕니다"


# ------------------------------------------------------------------ 그래프 줄


def _graph_row(state, service=None) -> GraphRowComponent:
    service = service or StubService()
    row = GraphRowComponent(
        state, service, _changes(state, service), refresh_cards=lambda: None, update_summary=lambda: None,
    )
    row.build()
    return row


def test_the_adaptive_strategy_needs_no_graph_and_keeps_the_checkboxes(state, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    state.strategy_name = "adaptive_graph"
    row = _graph_row(state)

    assert row.row.visible is True
    assert state.graph_agent_keys is None and state.selection_locked is False, "참여자는 체크한 전문가입니다"
    assert row.select_label.text == "기준 그래프:"
    assert "오케스트레이터가 턴마다" in row.status.text and "참여자는 체크한 전문가" in row.status.text
    assert "text-amber-400" not in row.status.classes and "text-red-400" not in row.status.classes, "그래프가 없어도 정상입니다"


def test_a_base_graph_is_named_and_a_missing_one_is_only_a_warning(state, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    graph_store.save_graph(graph_from_card_order("base", "기준 줄", ["coder"]))
    state.strategy_name = "adaptive_graph"
    state.graph_id = "base"
    row = _graph_row(state)
    assert "“기준 줄” 을(를) 기준으로" in row.status.text
    assert state.graph_agent_keys is None, "기준 그래프가 있어도 체크박스가 정합니다"

    state.graph_id = "nope"
    row.sync()
    assert "처음부터 그립니다" in row.status.text and "text-amber-400" in row.status.classes


def test_the_hand_drawn_strategy_still_locks_to_the_graph(state, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    graph_store.save_graph(graph_from_card_order("base", "기준 줄", ["coder"]))
    state.strategy_name = "graph_debate"
    state.graph_id = "base"
    row = _graph_row(state)
    assert row.select_label.text == "그래프:"
    assert state.graph_agent_keys == {"coder"} and state.selection_locked is True


def test_a_finished_drawn_graph_stays_on_screen_without_a_file(state, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    state.strategy_name = "adaptive_graph"
    row = _graph_row(state)
    assert row._preview_source()[0] is None, "아직 그린 것이 없습니다"

    spec = graph_from_card_order("drawn-1", "그린 그래프", ["coder"]).dump()
    tracker = GraphRunTracker(spec)
    tracker.observe_message({"id": "m1", "graph_node_id": "n1", "graph_port": "out", "round_number": 1})
    row.set_run(tracker, active=False)

    drawn, view, summary = row._preview_source()
    assert drawn == spec and view["nodes"]["n1"]["state"] == "done"
    assert summary.startswith("지난 턴")


def test_the_ledger_says_how_long_it_is(state):
    settings, _ = _settings(state)
    settings.set_decision_ledger("가" * 1200)
    assert state.decision_ledger == "가" * 1200
    assert settings.ledger_expansion._props["caption"] == "1,200자"
    settings.set_decision_ledger("")
    assert settings.ledger_expansion._props["caption"] == "비어 있음"


def test_rules_are_counted_on_the_button(state, monkeypatch):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    _, rules = _settings(state)
    rules.set_rules(["a", " "], ["b"])
    assert state.tool_grants == ["a"] and state.tool_denials == ["b"]
    assert rules.button.text == "대화 규칙 2"
    rules.set_rules(None, None)
    assert rules.button.text == "대화 규칙"


@pytest.mark.asyncio
async def test_removing_a_rule_is_saved_before_it_is_redrawn(state, notes):
    saved: List[Any] = []

    async def on_changed(grants, denials):
        saved.append((grants, denials))

    rules = ToolRulesDialog(state, on_changed=on_changed)
    rules.set_rules(["a", "b"], ["c"])

    await rules.remove("grants", "a")

    assert saved == [(["b"], ["c"])]
    assert state.tool_grants == ["b"]
    assert "a" in notes[0][0]


# ------------------------------------------------------------------ 작업 공간 줄


@pytest.mark.asyncio
async def test_applying_a_workspace_is_this_conversations_setting(state, notes, tmp_path, monkeypatch, ui_page):
    monkeypatch.setattr(ui, "timer", lambda *a, **k: None)
    service = StubService()
    changed, saved = [], []

    async def on_config_changed():
        saved.append(state.workspace_dir)

    async def on_download():
        return None

    row = WorkspaceRowComponent(
        state, service, _changes(state, service, on_config_changed=on_config_changed),
        on_changed=lambda: changed.append(1), on_open_download=on_download,
    )
    with ui_page:
        row.build()

    row.input.set_value(str(tmp_path))
    await row.on_apply()

    assert state.workspace_dir == str(tmp_path)
    assert changed == [1] and saved == [str(tmp_path)]
    assert service.writes == [], "conf.json 은 건드리지 않습니다"


@pytest.mark.asyncio
async def test_the_workspace_stays_while_this_conversation_runs(state, notes, tmp_path, ui_page):
    service = StubService()
    state.session_id = "s1"
    service.running = ["s1"]

    async def on_download():
        return None

    row = WorkspaceRowComponent(state, service, _changes(state, service), on_changed=lambda: None, on_open_download=on_download)
    with ui_page:
        row.build()
    row.input.set_value(str(tmp_path))

    await row.on_apply()

    assert state.workspace_dir == ""
    assert "진행 중" in notes[0][0]
