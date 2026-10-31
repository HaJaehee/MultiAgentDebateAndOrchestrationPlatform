"""호스트 도구 레지스트리 (ADR-042): `skills`·`peers`·`ask`·`tools` 와 플러그인이 같은 파이프라인을 탄다.

1. 내장 네 묶음은 고치기 전과 똑같이 나간다 — 도구 순서, 지침 순서, 판정을 건너뛰는 것까지.
2. 플러그인은 `allowed_mcp_servers` 로 켜고, 받지 않은 에이전트는 이름을 불러도 실행되지 않는다.
3. 플러그인은 도구 보안 판정을 받는다. 등급을 밝히지 않으면 모르는 도구이고, 규칙으로 막을 수 있다.
4. 플러그인이 터지거나 남의 이름으로 도구를 내놓아도 발언은 계속되고, 정의와 실행이 갈라지지 않는다.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

from app.agents import host_tools as host_tools_module
from app.agents import llm as llm_module
from app.agents.ask_user import ASK_USER_TOOL, asking
from app.agents.base import Agent
from app.agents.consultation import CONSULT_AGENT_TOOL, consultation_scope
from app.agents.host_tools import (
    HostToolCall,
    HostToolProvider,
    assignable_plugin_tools,
    find_host_tool,
    host_tool_guidance,
    host_tool_meta,
    host_tool_providers,
    host_tool_switches,
    host_tools_for,
    register_host_tool,
    run_host_tool,
    unregister_host_tool,
)
from app.agents.llm import LLMCaller
from app.agents.skills import LOAD_SKILL_TOOL
from app.agents.tool_loading import LOAD_TOOLS_TOOL
from app.config import RESERVED_SERVER_NAMES, RootConfig
from app.mcp.client import clip_tool_output
from app.mcp.policy import ToolMeta, UNKNOWN, profile_call
from app.orchestration.control import TurnControl
from app.orchestration.tool_gate import ToolGate

BASE = {"agents": {"orchestrator": {"name": "O", "role": "R", "model": "openai/gpt-4o"}}}


class Glossary(HostToolProvider):
    prefix = "glossary"
    tails = ("lookup", "define")
    label = "사내 용어집 도구"
    summary = "사내 용어를 찾습니다"
    risk = "read"

    def __init__(self) -> None:
        self.calls: List[HostToolCall] = []

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return [self.definition("lookup", "용어를 찾습니다.", {
            "type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"],
        })]

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        names = {(t.get("function") or {}).get("name") for t in tools or ()}
        return "[용어집] 사내 용어는 먼저 찾으세요." if "glossary__lookup" in names else None

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        self.calls.append(call)
        return f"{call.arguments.get('term')}: 뜻", "success"


class Unrated(Glossary):
    """등급을 밝히지 않은 플러그인."""

    prefix = "unrated"
    label = ""
    risk = UNKNOWN

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return [self.definition("lookup", "무언가를 합니다.")]

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        return None


@pytest.fixture()
def glossary() -> Iterator[Glossary]:
    provider = Glossary()
    register_host_tool(provider)
    yield provider
    unregister_host_tool(provider.prefix)


@pytest.fixture()
def unrated() -> Iterator[Unrated]:
    provider = Unrated()
    register_host_tool(provider)
    yield provider
    unregister_host_tool(provider.prefix)


def _agent(*servers: str, key: str = "coder", skills: Sequence[str] = ()) -> Agent:
    return Agent(
        key=key, name=key.title(), role="r", model="openai/gpt-4o", api_key="k",
        allowed_mcp_servers=list(servers), allowed_skills=list(skills),
        max_tokens=1024, max_context_window=32768, max_tool_iterations=4,
    )


def _names(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    return [t["function"]["name"] for t in tools or ()]


# --------------------------------------------------------------- 1. 내장 도구


def test_the_builtins_are_one_table_with_the_reserved_names():
    assert [p.prefix for p in host_tool_providers()] == ["skills", "peers", "ask", "tools"]
    assert set(RESERVED_SERVER_NAMES) == {p.prefix for p in host_tool_providers()}
    assert host_tool_switches() == {"peers"}
    assert assignable_plugin_tools() == []


@pytest.mark.parametrize("prefix", ["skills", "peers", "ask", "tools"])
def test_a_config_cannot_use_any_reserved_name(prefix):
    with pytest.raises(ValueError, match=f"mcp_servers.{prefix}.+Rename"):
        RootConfig.model_validate({**BASE, "mcp_servers": {prefix: {"command": "node"}}})


@pytest.mark.parametrize("name, prefix", [
    (LOAD_SKILL_TOOL, "skills"), ("load_skill", "skills"), ("skills__read_skill_file", "skills"),
    (CONSULT_AGENT_TOOL, "peers"), ("consult_agent", "peers"),
    (ASK_USER_TOOL, "ask"), ("ask_user", "ask"),
    (LOAD_TOOLS_TOOL, "tools"), ("load_tools", "tools"),
])
def test_builtin_names_with_or_without_the_prefix_find_their_provider(name, prefix):
    assert find_host_tool(name).prefix == prefix


@pytest.mark.parametrize("name", ["filesystem__read_file", "skills__delete_everything", "other__load_skill", "", "read_file"])
def test_everything_else_is_an_mcp_tool(name):
    assert find_host_tool(name) is None


def test_builtins_are_not_gated():
    for name in (LOAD_SKILL_TOOL, CONSULT_AGENT_TOOL, ASK_USER_TOOL, LOAD_TOOLS_TOOL):
        assert host_tool_meta(name) is None and find_host_tool(name).gated is False


def test_tools_come_in_the_same_order_as_before():
    """접두사 순서가 바뀌면 프롬프트 캐시가 깨집니다."""
    arch = _agent("peers", key="architect")
    peer = _agent(key="critic")
    remaining = [1]

    async def _answer(agent, question, options):
        return "답"

    assert host_tools_for(_agent()) == []
    with consultation_scope([arch, peer]), asking(_answer, remaining):
        assert _names(host_tools_for(arch)) == [CONSULT_AGENT_TOOL, ASK_USER_TOOL]


def test_guidance_comes_in_the_same_order_and_only_for_tools_the_speech_has():
    tools = [{"function": {"name": n}} for n in (LOAD_TOOLS_TOOL, ASK_USER_TOOL, CONSULT_AGENT_TOOL, LOAD_SKILL_TOOL)]

    heads = [text.split("\n")[0] for text in host_tool_guidance(tools)]

    assert heads == ["[스킬]", "[동료 전문가 자문]", "[사람에게 묻기]", "[도구 불러오기]"]
    assert host_tool_guidance([]) == [] and host_tool_guidance(None) == []


@pytest.mark.asyncio
async def test_load_tools_without_a_loop_is_an_observation_not_a_crash():
    output, status = await run_host_tool(
        find_host_tool(LOAD_TOOLS_TOOL), HostToolCall(agent=_agent(), name=LOAD_TOOLS_TOOL),
    )
    assert status == "error" and "불러올 수 없습니다" in output


# --------------------------------------------------------------- 2. 플러그인: 등록과 켜기


@pytest.mark.parametrize("prefix", ["", "Bad", "two__parts", "tail_", "한글", "1st", "a b"])
def test_a_bad_prefix_is_refused(prefix):
    class _Bad(Glossary):
        pass

    _Bad.prefix = prefix
    with pytest.raises(ValueError, match="접두사"):
        register_host_tool(_Bad())
    assert prefix not in RESERVED_SERVER_NAMES


@pytest.mark.parametrize("prefix", ["skills", "peers", "ask", "tools"])
def test_a_plugin_cannot_take_over_a_builtin(prefix):
    class _Taker(Glossary):
        pass

    _Taker.prefix = prefix
    before = find_host_tool(f"{prefix}__x")
    with pytest.raises(ValueError, match="이미"):
        register_host_tool(_Taker())
    assert find_host_tool(f"{prefix}__x") is before
    assert not isinstance(next(p for p in host_tool_providers() if p.prefix == prefix), Glossary)


@pytest.mark.parametrize("tails", [(), ("bad name",), (1,)])
def test_tool_names_must_be_declared_and_valid(tails):
    class _NoTails(Glossary):
        prefix = "no_tails"

    _NoTails.tails = tails
    with pytest.raises(ValueError, match="tails"):
        register_host_tool(_NoTails())


def test_two_plugins_cannot_share_a_prefix(glossary):
    with pytest.raises(ValueError, match="사내 용어집 도구"):
        register_host_tool(Glossary())


def test_a_plugin_is_switched_on_through_allowed_mcp_servers(glossary):
    assert host_tools_for(_agent()) == []
    assert _names(host_tools_for(_agent("glossary"))) == ["glossary__lookup"]
    assert host_tool_switches() == {"peers", "glossary"}
    assert assignable_plugin_tools() == [glossary]


def test_the_roster_row_writes_the_prefix_into_the_tool_list(glossary, ui_page):
    from app.ui.components.roster.widgets import plugin_tool_choice, plugin_tool_tooltip

    boxes: Dict[str, Any] = {}
    with ui_page:
        plugin_tool_choice(boxes, glossary, checked=True)
        plugin_tool_choice(boxes, Unrated(), checked=False, compact=True)

    assert [name for name, box in boxes.items() if box.value] == ["glossary"]
    assert "mcp(glossary/도구이름)" in plugin_tool_tooltip(glossary)


def test_the_prefix_is_not_sent_to_the_mcp_manager(glossary):
    assert LLMCaller().resolve_tool_servers(_agent("filesystem", "glossary", "peers")) == ["filesystem"]


def test_a_full_name_is_required_so_a_bare_name_cannot_steal_an_mcp_tool(glossary):
    assert find_host_tool("glossary__lookup") is glossary
    assert find_host_tool("glossary__define") is glossary
    assert find_host_tool("lookup") is None and find_host_tool("glossary__other") is None


@pytest.mark.asyncio
async def test_an_agent_without_it_cannot_call_it_by_guessing_the_name(glossary):
    output, status = await run_host_tool(
        glossary, HostToolCall(agent=_agent(), name="glossary__lookup", arguments={"term": "x"}),
    )
    assert status == "error" and "할당되지 않은" in output and glossary.calls == []


@pytest.mark.asyncio
async def test_huge_results_are_clipped_like_any_other_tool(glossary, monkeypatch):
    async def huge(call):
        return "가" * 10_000_000, "success"

    monkeypatch.setattr(glossary, "run", huge)
    output, status = await run_host_tool(glossary, HostToolCall(agent=_agent("glossary"), name="glossary__lookup"))
    assert status == "success" and len(output) < 10_000_000


@pytest.mark.asyncio
async def test_a_builtin_result_is_not_clipped_twice(monkeypatch):
    """내장 도구가 줄인 글을 또 줄이면 생략 안내가 틀린 숫자로 바뀝니다."""
    clipped = clip_tool_output("가" * 10_000_000)

    async def already_clipped(*args, **kwargs):
        return clipped, "success"

    monkeypatch.setattr(host_tools_module, "run_skill_tool", already_clipped)
    output, _status = await run_host_tool(
        find_host_tool(LOAD_SKILL_TOOL), HostToolCall(agent=_agent(), name=LOAD_SKILL_TOOL),
    )
    assert output == clipped


# --------------------------------------------------------------- 3. 플러그인: 도구 보안


def test_the_gate_sees_a_plugin_as_an_untrusted_tool_with_its_declared_risk(glossary, unrated):
    meta = host_tool_meta("glossary__lookup")
    assert meta == ToolMeta(server="glossary", tool="lookup", trusted=False, declared_risk="read")
    assert host_tool_meta("unrated__lookup").declared_risk == UNKNOWN


def test_a_plugin_tool_named_like_a_known_tool_is_not_treated_as_one():
    """믿는 서버의 `read_file` 은 경로까지 풀어 판정합니다. 플러그인이 그 이름을 쓴다고 그렇게 되면 안 됩니다."""
    meta = ToolMeta(server="x", tool="read_file", trusted=False, declared_risk="write")
    profile = profile_call(meta, {"path": "a.txt"}, Path("."))
    assert profile.risk == "write" and all(a.origin == "tool" for a in profile.actions)


def test_an_unusable_risk_means_unknown(glossary, monkeypatch):
    monkeypatch.setattr(glossary, "risk", "whatever")
    assert host_tool_meta("glossary__lookup").declared_risk == UNKNOWN

    def boom(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(glossary, "risk_of", boom)
    assert host_tool_meta("glossary__lookup").declared_risk == UNKNOWN


class _MCP:
    """도구가 없는 런타임. 매니저가 모르는 플러그인을 게이트가 어떻게 보는지 확인합니다."""

    workspace = Path(".")

    def __init__(self) -> None:
        self.executed: List[str] = []

    def get_openai_tools_for_servers(self, servers: List[str]) -> List[Dict[str, Any]]:
        return []

    def tool_meta(self, name: str) -> Optional[ToolMeta]:
        return None

    def hard_refusal(self, name: str, arguments: Any) -> None:
        return None

    async def execute_tool(self, name: str, arguments: Any, scope: Any = None, actor: Any = None) -> Tuple[str, str]:
        self.executed.append(name)
        return "MCP 결과", "success"


def _gate(mode: str = "default") -> ToolGate:
    return ToolGate(session_id="s", mode=mode, control=TurnControl(attended=False))


@pytest.mark.asyncio
async def test_an_unrated_plugin_cannot_run_without_a_person_to_ask(unrated):
    result = await _gate(mode="default").check(_agent("unrated"), "unrated__lookup", {}, _MCP())
    assert result.allowed is False and result.status == "denied"
    assert "물어볼 유저가 없어" in result.output


@pytest.mark.asyncio
async def test_a_read_rated_plugin_runs_and_is_recorded(glossary):
    result = await _gate(mode="read_only").check(_agent("glossary"), "glossary__lookup", {"term": "x"}, _MCP())
    assert result.allowed is True and result.audit["risk"] == "read"


@pytest.mark.asyncio
async def test_read_only_mode_denies_a_plugin_that_writes(glossary, monkeypatch):
    monkeypatch.setattr(glossary, "risk", "write")
    result = await _gate(mode="read_only").check(_agent("glossary"), "glossary__lookup", {}, _MCP())
    assert result.allowed is False and result.status == "denied"


def _config_with_rules(**security: Any) -> RootConfig:
    return RootConfig.model_validate({**BASE, "tool_security": security})


@pytest.mark.asyncio
async def test_a_deny_rule_blocks_the_call_and_takes_it_off_the_list(glossary, monkeypatch):
    from app.orchestration import tool_gate as tool_gate_module

    monkeypatch.setattr(tool_gate_module, "get_config", lambda: _config_with_rules(deny=["mcp(glossary/*)"]))
    gate, mcp = _gate(), _MCP()

    result = await gate.check(_agent("glossary"), "glossary__lookup", {}, mcp)
    assert result.allowed is False and "mcp(glossary/*)" in result.output
    assert gate.filter_tools("coder", host_tools_for(_agent("glossary")), mcp) == []


def test_read_only_takes_an_unrated_plugin_off_the_list_but_keeps_builtins(unrated):
    gate, mcp = _gate(mode="read_only"), _MCP()
    builtin = {"type": "function", "function": {"name": LOAD_SKILL_TOOL}}

    kept = gate.filter_tools("coder", [*host_tools_for(_agent("unrated")), builtin], mcp)

    assert _names(kept) == [LOAD_SKILL_TOOL]


# --------------------------------------------------------------- 4. 플러그인: 실패와 도구 루프


def test_a_provider_that_fails_while_listing_does_not_block_the_others(glossary, monkeypatch):
    def boom(agent):
        raise RuntimeError("boom")

    monkeypatch.setattr(glossary, "tools_for", boom)
    assert host_tools_for(_agent("glossary")) == []


def test_a_foreign_tool_name_is_dropped_so_definition_and_execution_cannot_part(glossary, monkeypatch):
    monkeypatch.setattr(glossary, "tools_for", lambda agent: [
        {"type": "function", "function": {"name": "filesystem__read_file"}},
        {"type": "function", "function": {"name": LOAD_SKILL_TOOL}},
        glossary.definition("lookup", "찾습니다"),
    ])
    assert _names(host_tools_for(_agent("glossary"))) == ["glossary__lookup"]


def test_a_failing_guidance_does_not_block_the_prompt(glossary, monkeypatch):
    def boom(tools):
        raise RuntimeError("boom")

    monkeypatch.setattr(glossary, "guidance", boom)
    assert host_tool_guidance([{"function": {"name": ASK_USER_TOOL}}])[0].startswith("[사람에게 묻기]")


def test_a_failing_owns_does_not_block_other_tool_calls(glossary, monkeypatch):
    def boom(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(glossary, "owns", boom)
    assert find_host_tool("filesystem__read_file") is None
    assert find_host_tool(ASK_USER_TOOL).prefix == "ask"


class _Message:
    def __init__(self, content: str, tool_calls: Any = None) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None

    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


def _install(monkeypatch, replies: List[Tuple[str, Sequence[Tuple[str, Dict[str, Any]]]]]) -> List[Dict[str, Any]]:
    remaining = list(replies)
    requests: List[Dict[str, Any]] = []

    async def fake_acompletion(**kwargs):
        requests.append(kwargs)
        fake_acompletion.current = remaining.pop(0)

        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=fake_acompletion.current[0]))])

        return stream()

    def fake_builder(chunks, messages=None):
        content, calls = fake_acompletion.current
        tool_calls = [
            SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for i, (name, args) in enumerate(calls)
        ] or None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=_Message(content, tool_calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)
    return requests


async def _speak(mcp: _MCP, agent: Agent, **kwargs: Any) -> Tuple[str, List[Dict[str, Any]]]:
    return await LLMCaller(mcp_manager=mcp).call_agent(
        agent, [{"role": "user", "content": "조사하세요"}], session_id="s1", **kwargs,
    )


@pytest.mark.asyncio
async def test_a_plugin_tool_runs_in_the_real_tool_loop(glossary, monkeypatch):
    requests = _install(monkeypatch, [
        ("찾아봅니다", [("glossary__lookup", {"term": "MADO"})]),
        ("끝", []),
    ])
    mcp = _MCP()

    content, logs = await _speak(mcp, _agent("glossary"), tool_gate=_gate(mode="read_only"))

    assert "glossary__lookup" in _names(requests[0]["tools"])
    assert "[용어집] 사내 용어는 먼저 찾으세요." in requests[0]["messages"][0]["content"]
    assert [(log["tool_name"], log["status"], log["output"]) for log in logs] == [
        ("glossary__lookup", "success", "MADO: 뜻"),
    ]
    assert logs[0]["security"]["decision"] == "allow"
    assert mcp.executed == [] and content == "찾아봅니다\n\n끝"
    call = glossary.calls[0]
    assert call.session_id == "s1" and call.workspace == mcp.workspace and call.loop is not None
    assert "glossary__lookup" in _names(call.tools)


@pytest.mark.asyncio
async def test_an_agent_without_it_never_sees_it_and_a_guess_goes_nowhere(glossary, monkeypatch):
    requests = _install(monkeypatch, [
        ("", [("glossary__lookup", {"term": "x"})]),
        ("끝", []),
    ])

    _content, logs = await _speak(_MCP(), _agent())

    assert "tools" not in requests[0] or "glossary__lookup" not in _names(requests[0].get("tools"))
    assert logs[0]["status"] == "error" and "할당되지 않은" in logs[0]["output"] and glossary.calls == []


@pytest.mark.asyncio
async def test_a_plugin_that_raises_is_an_observation_and_the_speech_goes_on(glossary, monkeypatch):
    _install(monkeypatch, [
        ("", [("glossary__lookup", {"term": "x"})]),
        ("끝", []),
    ])

    async def boom(call):
        raise RuntimeError("플러그인 오류")

    monkeypatch.setattr(glossary, "run", boom)

    content, logs = await _speak(_MCP(), _agent("glossary"))

    assert logs[0]["status"] == "error" and "플러그인 오류" in logs[0]["output"] and content == "끝"


@pytest.mark.asyncio
async def test_a_denied_plugin_call_does_not_run_and_the_model_reads_why(unrated, monkeypatch):
    _install(monkeypatch, [
        ("", [("unrated__lookup", {})]),
        ("끝", []),
    ])

    _content, logs = await _speak(_MCP(), _agent("unrated"), tool_gate=_gate(mode="default"))

    assert logs[0]["status"] == "denied" and unrated.calls == []
    assert logs[0]["security"]["decision"] == "deny"
