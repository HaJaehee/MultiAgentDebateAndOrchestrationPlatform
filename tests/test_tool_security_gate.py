"""도구 보안 — 판정을 토론에 붙이는 쪽 (`tool_gate` · `control` · 도구 루프 · 매니저 · 러너).

지키려는 것.

1. "묻기" 는 사람의 답을 기다리고, 답이 없거나 물을 사람이 없으면 **거부**합니다.
2. "이 대화에서 허용" 은 그 뒤의 같은 호출을 묻지 않게 하고 세션에 저장됩니다. 범위가
   지금 호출을 덮지 못하면 받지 않습니다.
3. "항상 허용" 은 서버 PC 에서만 conf.json 을 고칩니다. 원격이면 이 대화로 좁힙니다.
4. 거부된 호출은 실행되지 않고, 그 이유가 곧 도구 결과가 됩니다. 같은 호출을 되풀이해도
   카드를 다시 띄우지 않습니다.
5. 게이트가 스스로 터지면 거부합니다 (fail closed).
6. 매니저의 고정 보호는 게이트 밖에서 들어온 호출에도 걸립니다.
"""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import app.agents.llm as llm_module
import app.orchestration.tool_gate as gate_module
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.config import RootConfig, add_tool_security_rules_to_conf_file, read_conf_file
from app.database.models import SessionModel
from app.database.session import get_session_factory, init_db
from app.mcp import manager as manager_module
from app.mcp.manager import MCPManager
from app.orchestration.control import ToolApprovalRequest, TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import TurnRun
from app.orchestration.tool_gate import ToolGate
from tests.fake_llm import FakeLLMCaller


# ---------------------------------------------------------------------------
# 대역
# ---------------------------------------------------------------------------


def _config(**security: Any) -> RootConfig:
    return RootConfig.model_validate({
        "agents": {"orchestrator": {"name": "O", "role": "r"}},
        "tool_security": security,
    })


@pytest.fixture
def security(monkeypatch):
    """게이트와 매니저가 읽는 설정을 테스트가 정합니다 (개발 PC 의 conf.json 과 무관)."""
    holder = {"config": _config()}

    def use(**values: Any) -> RootConfig:
        holder["config"] = _config(**values)
        return holder["config"]

    monkeypatch.setattr(gate_module, "get_config", lambda *a, **k: holder["config"])
    monkeypatch.setattr(manager_module, "get_config", lambda *a, **k: holder["config"])
    return use


class _RecordingClient:
    """부르면 기록만 남기는 서버 연결."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        self.tools: List[Any] = []
        self.is_connected = True
        self.calls: List[Any] = []

    async def execute_tool(self, tool_name, arguments, scope=None):
        self.calls.append((tool_name, arguments))
        return f"ran {tool_name}"


def _manager(tmp_path: Path) -> MCPManager:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = MCPManager({}, workspace=workspace)
    for server, tools in {
        "filesystem": ("read_file", "write_file"),
        "sandbox": ("execute_python_code",),
        "fetch": ("fetch",),
    }.items():
        client = _RecordingClient(server)
        manager.clients[server] = client  # type: ignore[assignment]
        for name in tools:
            manager._tool_lookup[f"{server}__{name}"] = (client, name)  # noqa: SLF001
    return manager


def _agent(key: str = "coder") -> Agent:
    return Agent(key=key, name=key.title(), role="r", api_key="sk-test")


async def _answer_when_asked(control: TurnControl, decision: str, **kw: Any) -> None:
    """승인 카드가 뜨면 곧바로 답합니다 (사람 대역)."""
    for _ in range(200):
        pending = control.pending_approvals
        if pending:
            control.resolve_tool_approval(pending[0].id, decision, **kw)
            return
        await asyncio.sleep(0.005)
    raise AssertionError("승인 카드가 뜨지 않았습니다")


# ---------------------------------------------------------------------------
# 승인 통로 (TurnControl)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unanswered_approval_is_a_denial():
    control = TurnControl()
    request = await control.ask_tool_approval(
        agent_key="coder", agent_name="Coder", payload={}, timeout=0.05,
    )
    assert request.decision == "timeout" and not request.allowed


@pytest.mark.asyncio
async def test_stopping_the_debate_denies_pending_approvals():
    control = TurnControl()
    task = asyncio.create_task(control.ask_tool_approval(
        agent_key="coder", agent_name="Coder", payload={}, timeout=5,
    ))
    await asyncio.sleep(0.01)
    control.request_stop()
    request = await task
    assert request.decision == "deny"


@pytest.mark.asyncio
async def test_budget_answers_never_reach_an_approval():
    """id 없이 누른 "상한 확장" 이 도구 실행을 허락하는 일은 없어야 합니다."""
    control = TurnControl()
    task = asyncio.create_task(control.ask_tool_approval(
        agent_key="coder", agent_name="Coder", payload={}, timeout=5,
    ))
    await asyncio.sleep(0.01)
    assert control.pending_decisions == []
    assert control.resolve_decision(15) is False
    assert len(control.pending_approvals) == 1
    control.resolve_tool_approval(control.pending_approvals[0].id, "deny")
    await task


@pytest.mark.asyncio
async def test_a_scope_that_misses_the_call_is_refused_before_answering():
    control = TurnControl()
    task = asyncio.create_task(control.ask_tool_approval(
        agent_key="coder", agent_name="Coder", payload={}, timeout=5,
        validator=lambda scope, decision: None if scope == ["net(ok.com)"] else "덮지 못함",
    ))
    await asyncio.sleep(0.01)
    request_id = control.pending_approvals[0].id
    with pytest.raises(ValueError):
        control.resolve_tool_approval(request_id, "allow_session", scope=["net(bad.com)"])
    with pytest.raises(ValueError):
        control.resolve_tool_approval(request_id, "allow_session", scope=[])
    assert control.resolve_tool_approval(request_id, "allow_session", scope=["net(ok.com)"])
    request = await task
    assert request.decision == "allow_session" and request.scope == ["net(ok.com)"]
    assert isinstance(request, ToolApprovalRequest)


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_workspace_writes_run_without_asking(tmp_path, security):
    gate = ToolGate(session_id="s", control=TurnControl())
    result = await gate.check(_agent(), "filesystem__write_file", {"path": "a.md"}, _manager(tmp_path))
    assert result.allowed
    assert result.audit["decision"] == "allow" and result.audit["rule"] == "mode:default"


@pytest.mark.asyncio
async def test_asks_are_denied_when_nobody_can_answer(tmp_path, security):
    gate = ToolGate(session_id="s", control=None)
    result = await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org"}, _manager(tmp_path))
    assert not result.allowed and result.status == "denied"
    assert result.audit["rule"] == "unattended"
    assert "REFUSED" in result.output


@pytest.mark.asyncio
async def test_session_grants_stop_the_next_prompt_and_are_saved(tmp_path, security):
    control = TurnControl()
    saved: List[List[str]] = []

    async def save(grants: List[str], denials: List[str]) -> None:
        saved.append(list(grants))
        assert denials == []

    events: List[Dict[str, Any]] = []

    async def on_event(event: Dict[str, Any]) -> None:
        events.append(event)

    gate = ToolGate(session_id="s", control=control, on_event=on_event, save_rules=save)
    mcp = _manager(tmp_path)
    answer = asyncio.create_task(_answer_when_asked(
        control, "allow_session", scope=["net(python.org)"], approver="remote",
    ))
    first = await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org/3/"}, mcp)
    await answer
    assert first.allowed and first.audit["decision"] == "approved"
    assert first.audit["approver"] == "remote"
    assert saved == [["net(python.org)"]]
    assert [e["type"] for e in events] == ["tool_approval_requested", "tool_approval_resolved"]
    requested = events[0]
    assert requested["tool_name"] == "fetch__fetch" and requested["suggestions"] == ["net(docs.python.org)"]

    second = await gate.check(_agent("critic"), "fetch__fetch", {"url": "https://peps.python.org"}, mcp)
    assert second.allowed and second.audit["rule"] == "session:net(python.org)"
    assert control.pending_approvals == []


@pytest.mark.asyncio
async def test_a_rejected_call_is_not_asked_again(tmp_path, security):
    control = TurnControl()
    gate = ToolGate(session_id="s", control=control)
    mcp = _manager(tmp_path)
    args = {"url": "https://example.com"}
    answer = asyncio.create_task(_answer_when_asked(control, "deny", reason="외부 접속 금지"))
    first = await gate.check(_agent(), "fetch__fetch", args, mcp)
    await answer
    assert not first.allowed and "외부 접속 금지" in first.output
    assert first.audit["decision"] == "rejected"

    again = await gate.check(_agent(), "fetch__fetch", args, mcp)
    assert not again.allowed and "다시 묻지 않았습니다" in again.output
    assert control.pending_approvals == []


@pytest.mark.asyncio
async def test_always_allow_from_the_server_pc_writes_conf_json(tmp_path, security, monkeypatch):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"agents": {"orchestrator": {"name": "O", "role": "r"}}}), encoding="utf-8")
    monkeypatch.setattr(gate_module, "active_config_path", lambda: conf)
    control = TurnControl()
    gate = ToolGate(session_id="s", control=control)
    answer = asyncio.create_task(_answer_when_asked(
        control, "allow_always", scope=["net(docs.python.org)"], approver="local",
    ))
    result = await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org"}, _manager(tmp_path))
    await answer
    assert result.allowed
    assert read_conf_file(conf)["tool_security"]["allow"] == ["net(docs.python.org)"]


@pytest.mark.asyncio
async def test_always_allow_from_a_remote_viewer_stays_in_the_conversation(tmp_path, security, monkeypatch):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    monkeypatch.setattr(gate_module, "active_config_path", lambda: conf)
    control = TurnControl()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    gate = ToolGate(session_id="s", control=control, on_event=on_event)
    answer = asyncio.create_task(_answer_when_asked(
        control, "allow_always", scope=["net(docs.python.org)"], approver="remote",
    ))
    result = await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org"}, _manager(tmp_path))
    await answer
    assert result.allowed
    assert "tool_security" not in read_conf_file(conf)
    assert gate.grants == ["net(docs.python.org)"]
    assert events[-1]["decision"] == "allow_session" and events[-1]["note"]


@pytest.mark.asyncio
async def test_agent_overrides_only_tighten(tmp_path, security):
    security(agents={"critic": {"mode": "read_only"}})
    gate = ToolGate(session_id="s", mode="auto", control=None)
    mcp = _manager(tmp_path)
    assert (await gate.check(_agent("coder"), "filesystem__write_file", {"path": "a"}, mcp)).allowed
    blocked = await gate.check(_agent("critic"), "filesystem__write_file", {"path": "a"}, mcp)
    assert not blocked.allowed and blocked.audit["decision"] == "deny"


@pytest.mark.asyncio
async def test_live_mode_change_applies_to_the_next_call(tmp_path, security):
    gate = ToolGate(session_id="s", control=None)
    mcp = _manager(tmp_path)
    assert (await gate.check(_agent(), "filesystem__write_file", {"path": "a"}, mcp)).allowed
    gate.set_mode("read_only")
    assert not (await gate.check(_agent(), "filesystem__write_file", {"path": "a"}, mcp)).allowed


def test_filter_tools_drops_tools_that_can_never_run(tmp_path, security):
    gate = ToolGate(session_id="s", mode="read_only")
    tools = [{"type": "function", "function": {"name": n}} for n in
             ("filesystem__read_file", "filesystem__write_file", "sandbox__execute_python_code")]
    kept = gate.filter_tools("coder", tools, _manager(tmp_path))
    assert [t["function"]["name"] for t in kept] == ["filesystem__read_file"]


@pytest.mark.asyncio
async def test_a_crashing_gate_denies(tmp_path, security, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("broken")

    monkeypatch.setattr(gate_module, "evaluate", boom)
    gate = ToolGate(session_id="s", control=None)
    result = await gate.check(_agent(), "filesystem__read_file", {"path": "a"}, _manager(tmp_path))
    assert not result.allowed and result.audit["rule"] == "gate-error"


@pytest.mark.asyncio
async def test_hard_protection_needs_no_card(tmp_path, security):
    control = TurnControl()
    gate = ToolGate(session_id="s", mode="auto", control=control)
    mcp = _manager(tmp_path)
    result = await gate.check(_agent(), "filesystem__read_file", {"path": ".memory-graphs/x.jsonl"}, mcp)
    assert not result.allowed and result.audit["decision"] == "hard"
    assert control.pending_approvals == []


# ---------------------------------------------------------------------------
# 매니저 — 게이트 밖에서 들어온 호출에도 고정 보호
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_refuses_protected_paths_without_calling_the_server(tmp_path, security):
    mcp = _manager(tmp_path)
    output, status = await mcp.execute_tool("filesystem__read_file", {"path": ".memory-graphs/x.jsonl"})
    assert status == "denied" and "REFUSED" in output
    assert mcp.clients["filesystem"].calls == []  # type: ignore[attr-defined]
    output, status = await mcp.execute_tool("filesystem__read_file", {"path": "notes.md"})
    assert status == "success"


def test_remote_servers_on_other_hosts_are_not_trusted_by_default(security):
    assert manager_module.server_is_trusted("filesystem", "")
    assert manager_module.server_is_trusted("pair_slide", "127.0.0.1")
    assert not manager_module.server_is_trusted("jira", "jira.corp.example")
    security(trusted_servers=["jira"])
    assert manager_module.server_is_trusted("jira", "jira.corp.example")
    assert not manager_module.server_is_trusted("filesystem", "")


# ---------------------------------------------------------------------------
# 도구 루프 — 거부된 호출은 실행되지 않고 이유가 결과가 됩니다
# ---------------------------------------------------------------------------


class _FakeMessage(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


def _install_one_tool_call(monkeypatch, name: str, arguments: Dict[str, Any]) -> None:
    turns = [True, False]

    async def fake_acompletion(**kwargs):
        fake_acompletion.calls_tool = turns.pop(0)

        async def _stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="."))])

        return _stream()

    def fake_builder(chunks, messages=None):
        tool_calls = [SimpleNamespace(
            id="call_1", function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )] if fake_acompletion.calls_tool else None
        return SimpleNamespace(choices=[SimpleNamespace(message=_FakeMessage(content=".", tool_calls=tool_calls))])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)


@pytest.mark.asyncio
async def test_a_denied_call_never_reaches_the_server(tmp_path, security, monkeypatch):
    security(deny=["read(**/.env)"])
    _install_one_tool_call(monkeypatch, "filesystem__read_file", {"path": ".env"})
    mcp = _manager(tmp_path)
    mcp.get_openai_tools_for_servers = lambda servers: [  # type: ignore[assignment]
        {"type": "function", "function": {"name": "filesystem__read_file", "parameters": {}}}
    ]
    gate = ToolGate(session_id="s", control=None)
    caller = LLMCaller(mcp_manager=mcp)
    _content, logs = await caller.call_agent(
        _agent(), [{"role": "user", "content": "읽어"}], mcp=mcp, tool_gate=gate,
    )
    assert mcp.clients["filesystem"].calls == []  # type: ignore[attr-defined]
    assert logs[0]["status"] == "denied"
    assert logs[0]["security"]["decision"] == "deny"
    assert "read(**/.env)" in logs[0]["output"]


# ---------------------------------------------------------------------------
# 러너 스냅샷 · 설정 기록
# ---------------------------------------------------------------------------


def test_the_snapshot_carries_every_pending_card():
    run = TurnRun("s", "p")
    run.apply({"type": "tool_approval_requested", "id": "a", "agent_name": "A", "tool_name": "t"})
    run.apply({"type": "tool_approval_requested", "id": "b", "agent_name": "B", "tool_name": "t"})
    assert [c["id"] for c in run.snapshot()["tool_approvals"]] == ["a", "b"]
    run.apply({"type": "tool_approval_resolved", "id": "a", "decision": "deny"})
    assert [c["id"] for c in run.snapshot()["tool_approvals"]] == ["b"]
    run.apply({"type": "turn_completed"})
    assert run.snapshot()["tool_approvals"] == []


def test_adding_rules_to_conf_json_keeps_comments_and_skips_duplicates(tmp_path):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({
        "// tool_security": "설명",
        "tool_security": {"allow": ["net(a.com)"]},
    }), encoding="utf-8")
    assert add_tool_security_rules_to_conf_file("allow", ["net(a.com)", "net(b.com)"], conf) == ["net(b.com)"]
    data = read_conf_file(conf)
    assert data["tool_security"]["allow"] == ["net(a.com)", "net(b.com)"]
    assert data["// tool_security"] == "설명"
    with pytest.raises(ValueError):
        add_tool_security_rules_to_conf_file("allow", ["nonsense("], conf)


def test_agent_overrides_cannot_loosen():
    with pytest.raises(ValueError):
        _config(agents={"critic": {"allow": ["net(*)"]}})


# ---------------------------------------------------------------------------
# 엔진 — 턴마다 문지기 하나, 모든 LLM 호출에
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_llm_call_of_a_turn_gets_the_same_gate():
    """발언뿐 아니라 계획·합성 같은 호출도 에이전트의 도구를 들고 나갑니다."""
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"tool-gate-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="gate", strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator", "architect"], tool_mode="review",
            tool_grants=["net(python.org)"], tool_denials=["mcp(fetch)"],
        ))
        await db.commit()

    caller = FakeLLMCaller()
    engine = OrchestratorEngine(llm_caller=caller)
    await engine.run_turn(session_id=sid, user_prompt="설계해 줘")

    gates = {id(g) for g in caller.tool_gates}
    assert len(gates) == 1 and caller.tool_gates[0] is not None
    gate = caller.tool_gates[0]
    assert gate._session_mode == "review"  # noqa: SLF001
    assert gate.grants == ["net(python.org)"]
    assert gate.denials == ["mcp(fetch)"]
    assert engine._tool_gates == {}, "턴이 끝나면 문지기를 치웁니다"  # noqa: SLF001
    assert engine.set_tool_mode(sid, "auto") is False


# ---------------------------------------------------------------------------
# 이 대화에서 거부 · 규칙 교체
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_session_denial_is_saved_and_blocks_the_next_turn_without_asking(tmp_path, security):
    control = TurnControl()
    saved: List[Any] = []

    async def save(grants: List[str], denials: List[str]) -> None:
        saved.append((list(grants), list(denials)))

    gate = ToolGate(session_id="s", control=control, save_rules=save)
    mcp = _manager(tmp_path)
    answer = asyncio.create_task(_answer_when_asked(
        control, "deny_session", scope=["net(python.org)"], reason="외부 문서 금지",
    ))
    first = await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org"}, mcp)
    await answer
    assert not first.allowed and first.audit["decision"] == "rejected"
    assert first.audit["rule"] == "session:net(python.org)"
    assert "외부 문서 금지" in first.output and "net(python.org)" in first.output
    assert saved == [([], ["net(python.org)"])]

    # 다음 턴 — 새 게이트가 세션에 저장된 목록으로 시작합니다. 다른 에이전트, 다른 주소여도
    # 범위 안이면 묻지 않고 거부합니다.
    next_turn = ToolGate(session_id="s", control=TurnControl(), denials=saved[-1][1])
    again = await next_turn.check(_agent("critic"), "fetch__fetch", {"url": "https://peps.python.org"}, mcp)
    assert not again.allowed and again.audit["decision"] == "rejected"
    assert "유저가 이 대화에서" in again.output
    assert next_turn.control.pending_approvals == []


@pytest.mark.asyncio
async def test_a_denial_scope_that_does_not_block_the_call_is_refused(tmp_path, security):
    control = TurnControl()
    gate = ToolGate(session_id="s", control=control)
    task = asyncio.create_task(gate.check(
        _agent(), "fetch__fetch", {"url": "https://docs.python.org"}, _manager(tmp_path),
    ))
    for _ in range(200):
        if control.pending_approvals:
            break
        await asyncio.sleep(0.005)
    request_id = control.pending_approvals[0].id
    with pytest.raises(ValueError):
        control.resolve_tool_approval(request_id, "deny_session", scope=["net(evil.com)"])
    assert control.resolve_tool_approval(request_id, "deny_session", scope=["mcp(fetch)"])
    result = await task
    assert not result.allowed and gate.denials == ["mcp(fetch)"]


@pytest.mark.asyncio
async def test_always_deny_keeps_the_built_in_deny_list(tmp_path, security, monkeypatch):
    """`deny` 를 처음 쓰면서 기본 거부 목록(비밀 파일 등)이 사라지면 안 됩니다."""
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    monkeypatch.setattr(gate_module, "active_config_path", lambda: conf)
    control = TurnControl()
    gate = ToolGate(session_id="s", control=control)
    answer = asyncio.create_task(_answer_when_asked(
        control, "deny_always", scope=["net(docs.python.org)"], approver="local",
    ))
    result = await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org"}, _manager(tmp_path))
    await answer
    assert not result.allowed
    deny = read_conf_file(conf)["tool_security"]["deny"]
    assert deny[-1] == "net(docs.python.org)"
    assert "read(**/.env)" in deny and "net(webhook.site)" in deny


@pytest.mark.asyncio
async def test_always_deny_from_a_remote_viewer_stays_in_the_conversation(tmp_path, security, monkeypatch):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    monkeypatch.setattr(gate_module, "active_config_path", lambda: conf)
    control = TurnControl()
    gate = ToolGate(session_id="s", control=control)
    answer = asyncio.create_task(_answer_when_asked(
        control, "deny_always", scope=["net(docs.python.org)"], approver="remote",
    ))
    await gate.check(_agent(), "fetch__fetch", {"url": "https://docs.python.org"}, _manager(tmp_path))
    await answer
    assert "tool_security" not in read_conf_file(conf)
    assert gate.denials == ["net(docs.python.org)"]


@pytest.mark.asyncio
async def test_ask_rule_cards_offer_denial_but_not_allowance(tmp_path, security):
    security(ask=["mcp(filesystem/write_file)"])
    control = TurnControl()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    gate = ToolGate(session_id="s", control=control, on_event=on_event)
    answer = asyncio.create_task(_answer_when_asked(control, "deny"))
    await gate.check(_agent(), "filesystem__write_file", {"path": "a.md"}, _manager(tmp_path))
    await answer
    card = events[0]
    assert card["can_remember"] is False
    assert card["deny_suggestions"] == ["write(a.md)"]


@pytest.mark.asyncio
async def test_replacing_rules_mid_turn_takes_effect_and_is_saved(tmp_path, security):
    saved: List[Any] = []

    async def save(grants, denials):
        saved.append((list(grants), list(denials)))

    gate = ToolGate(session_id="s", mode="auto", control=None, denials=["write(a.md)"], save_rules=save)
    mcp = _manager(tmp_path)
    assert not (await gate.check(_agent(), "filesystem__write_file", {"path": "a.md"}, mcp)).allowed
    await gate.replace_rules([], [])
    assert (await gate.check(_agent(), "filesystem__write_file", {"path": "a.md"}, mcp)).allowed
    assert saved == [([], [])]


def test_writing_deny_rules_seeds_the_defaults_once(tmp_path):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    assert add_tool_security_rules_to_conf_file("deny", ["read(**/.env)"], conf) == []
    assert "tool_security" not in read_conf_file(conf), "기본 목록에 이미 있으면 적지 않습니다"
    add_tool_security_rules_to_conf_file("deny", ["net(x.com)"], conf)
    add_tool_security_rules_to_conf_file("deny", ["net(y.com)"], conf)
    deny = read_conf_file(conf)["tool_security"]["deny"]
    assert deny.count("read(**/.env)") == 1 and deny[-2:] == ["net(x.com)", "net(y.com)"]


@pytest.mark.asyncio
async def test_rules_go_through_the_running_gate_only():
    engine = OrchestratorEngine(llm_caller=FakeLLMCaller())
    assert await engine.set_tool_rules("nobody", ["a"], ["b"]) is False
    saved: List[Any] = []

    async def save(grants, denials):
        saved.append((grants, denials))

    engine._tool_gates["s"] = ToolGate(session_id="s", save_rules=save)  # noqa: SLF001
    assert await engine.set_tool_rules("s", ["net(a.com)"], ["net(b.com)"]) is True
    assert saved == [(["net(a.com)"], ["net(b.com)"])]
