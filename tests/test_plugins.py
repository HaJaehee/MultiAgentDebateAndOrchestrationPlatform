"""외부 플러그인 아키텍처 검증 스위트 (ADR-042).

1. `strategies/`, `host_tools/` 디렉터리 및 파이썬 Entry Point를 통한 동적 탐색 및 레지스트리 등록.
2. 문법 오류 파일, 식별자 충돌, 내장 전략 오버라이드 시도 격리 및 정상 플러그인 등록 보장.
3. 보안 격리: 호스트 프로세스 권한으로 동작하므로 작업 공간 내부 플러그인 탐색 원천 차단 및 도구 조작 방지.
4. 내결함성: 플러그인 런타임 예외 발생 시 안전한 폴백(우선순위 순차 토론) 실행 보장.
"""

import re
import textwrap
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app import plugins as plugins_module
from app.agents.base import Agent
from app.agents.host_tools import (
    HostToolCall,
    find_host_tool,
    host_tool_guidance,
    host_tool_meta,
    host_tools_for,
    register_host_tool,
    run_host_tool,
    unregister_host_tool,
)
from app.agents.pool import AgentPool
from app.config import (
    PROJECT_ROOT,
    RESERVED_SERVER_NAMES,
    AgentConfig,
    PluginsConfig,
    RootConfig,
    add_mcp_server_to_conf_file,
)
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.mcp import manager as manager_module
from app.mcp.policy import ALLOW, ASK, DENY, MODE_DEFAULTS, RISK_LABELS, CallProfile, Action, hard_block
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateState
from app.orchestration.tool_gate import ToolGate
from app.orchestration.strategies import (
    STRATEGY_MAP,
    SequentialDebateStrategy,
    get_strategy,
    instruction_for_turn,
    register_strategy,
    resolve_strategy_name,
    speakers_for_round,
)
from app.plugins import load_plugins, plugin_problems, plugin_records
from app.ui.components.roster.common import unknown_tool_servers
from app.ui.components.roster.state import RosterState
from tests.fake_llm import FakeLLMCaller

BASE = {"agents": {"orchestrator": {"name": "O", "role": "R", "model": "openai/gpt-4o"}}}

VOTING = """
    from app.orchestration.strategies import BaseDebateStrategy, order_by_priority, specialists_of

    class VotingStrategy(BaseDebateStrategy):
        name = "voting"
        display_name = "합의 투표"

        def get_speakers_for_round(self, active_agents, round_num, state):
            return list(reversed(order_by_priority(specialists_of(active_agents))))

        def turn_instruction(self, agent, speakers, index, state):
            return "[투표] 앞선 제안 중 하나를 고르세요."
"""

GLOSSARY = """
    from app.agents.host_tools import HostToolProvider

    class GlossaryTools(HostToolProvider):
        prefix = "glossary"
        tails = ("lookup",)
        label = "사내 용어집 도구"
        risk = "read"

        def tools_for(self, agent):
            return [self.definition("lookup", "용어를 찾습니다.")]

        async def run(self, call):
            return "답", "success"
"""


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    """플러그인이 더한 것을 시험마다 걷어 냅니다. 다른 시험이 내장 전략만 있다고 믿습니다."""
    strategies = dict(STRATEGY_MAP)
    reserved = dict(RESERVED_SERVER_NAMES)
    records = list(plugins_module._records)  # noqa: SLF001
    yield
    STRATEGY_MAP.clear()
    STRATEGY_MAP.update(strategies)
    for prefix in set(RESERVED_SERVER_NAMES) - set(reserved):
        unregister_host_tool(prefix)
    RESERVED_SERVER_NAMES.clear()
    RESERVED_SERVER_NAMES.update(reserved)
    plugins_module._records[:] = records  # noqa: SLF001


def _write(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config(tmp_path: Path, **plugins: Any) -> RootConfig:
    settings = {
        "strategies_dir": str(tmp_path / "strategies"),
        "host_tools_dir": str(tmp_path / "host_tools"),
        **plugins,
    }
    return RootConfig.model_validate({**BASE, "plugins": settings})


def _load(tmp_path: Path, **plugins: Any) -> List[plugins_module.PluginRecord]:
    return load_plugins(_config(tmp_path, **plugins), reload=True)


@pytest.fixture()
def no_entry_points(monkeypatch):
    monkeypatch.setattr(plugins_module.importlib.metadata, "entry_points", lambda group: [])


# --------------------------------------------------------------- 1. 폴더


def test_a_strategy_file_is_registered_without_touching_the_code(tmp_path, no_entry_points):
    path = _write(tmp_path / "strategies", "voting.py", VOTING)

    records = _load(tmp_path)

    assert [(r.kind, r.name, r.source, r.problem) for r in records] == [("strategy", "voting", str(path), "")]
    assert get_strategy("voting").display_name == "합의 투표"
    assert resolve_strategy_name("voting") == "voting"


def test_a_package_folder_is_one_plugin_and_can_use_relative_imports(tmp_path, no_entry_points):
    folder = tmp_path / "strategies" / "voting_pack"
    _write(folder, "core.py", VOTING)
    _write(folder, "__init__.py", "from .core import VotingStrategy\n")

    records = _load(tmp_path)

    assert [r.name for r in records] == ["voting"] and not plugin_problems()


def test_helpers_and_private_files_are_not_plugins(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "_shared.py", "raise RuntimeError('읽으면 안 됩니다')\n")
    _write(tmp_path / "strategies", ".hidden.py", "raise RuntimeError('읽으면 안 됩니다')\n")
    _write(tmp_path / "strategies", "notes.txt", "글")
    (tmp_path / "strategies" / "data").mkdir()

    assert _load(tmp_path) == []


def test_a_host_tool_file_is_registered_and_reserves_its_prefix(tmp_path, no_entry_points):
    _write(tmp_path / "host_tools", "glossary.py", GLOSSARY)

    records = _load(tmp_path)

    assert [(r.kind, r.name) for r in records] == [("host_tool", "glossary")]
    assert find_host_tool("glossary__lookup").label == "사내 용어집 도구"
    with pytest.raises(ValueError, match="사내 용어집 도구"):
        RootConfig.model_validate({**BASE, "mcp_servers": {"glossary": {"command": "node"}}})


def test_the_prefix_is_refused_when_adding_a_server_from_the_screen(tmp_path, no_entry_points):
    from app.config import write_conf_file

    _write(tmp_path / "host_tools", "glossary.py", GLOSSARY)
    _load(tmp_path)
    conf = tmp_path / "conf.json"
    write_conf_file(conf, BASE)

    with pytest.raises(ValueError, match="사내 용어집 도구"):
        add_mcp_server_to_conf_file("glossary", "node", [], {}, True, conf)


def test_a_missing_or_switched_off_folder_is_not_a_problem(tmp_path, no_entry_points):
    assert _load(tmp_path) == []
    _write(tmp_path / "strategies", "voting.py", VOTING)
    assert _load(tmp_path, strategies_dir="") == [] and "voting" not in STRATEGY_MAP


def test_loading_twice_reads_once(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "voting.py", VOTING)
    first = _load(tmp_path)

    assert load_plugins(_config(tmp_path)) == first and not plugin_problems()


def test_disabled_plugins_are_not_even_imported(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "voting.py", "raise RuntimeError('import 하면 안 됩니다')\n")

    assert _load(tmp_path, disabled=["voting"]) == []


# --------------------------------------------------------------- 1-b. entry point


def _entry(name: str, loaded: Any) -> SimpleNamespace:
    return SimpleNamespace(name=name, load=lambda: loaded)


class _ListedStrategy(SequentialDebateStrategy):
    name = "listed"
    display_name = "패키지 전략"


def _entry_points(monkeypatch, **groups: List[SimpleNamespace]) -> None:
    table = {plugins_module.STRATEGY_GROUP: groups.get("strategies", []),
             plugins_module.HOST_TOOL_GROUP: groups.get("host_tools", [])}
    monkeypatch.setattr(plugins_module.importlib.metadata, "entry_points", lambda group: table[group])


def test_an_entry_point_may_name_a_class_an_instance_or_a_module(tmp_path, monkeypatch):
    class _Instance(_ListedStrategy):
        name = "instance"

    module = ModuleType("listed_pack")
    class _InModule(_ListedStrategy):
        name = "in_module"
    _InModule.__module__ = "listed_pack"
    module.InModule = _InModule

    _entry_points(monkeypatch, strategies=[
        _entry("a", _ListedStrategy), _entry("b", _Instance()), _entry("c", module),
    ])

    records = _load(tmp_path)

    assert [(r.source, r.name) for r in records] == [
        ("mado.strategies:a", "listed"), ("mado.strategies:b", "instance"), ("mado.strategies:c", "in_module"),
    ]


def test_an_entry_point_can_be_switched_off_and_a_broken_one_is_reported(tmp_path, monkeypatch):
    def explode():
        raise ImportError("의존성 없음")

    _entry_points(monkeypatch, strategies=[
        _entry("off", _ListedStrategy),
        SimpleNamespace(name="broken", load=explode),
        _entry("wrong", object()),
    ])

    records = _load(tmp_path, disabled=["off"])

    assert "listed" not in STRATEGY_MAP
    assert [(r.source, "의존성 없음" in r.problem) for r in records][0] == ("mado.strategies:broken", True)
    assert "구체 클래스" in records[1].problem


# --------------------------------------------------------------- 2. 깨진 플러그인


@pytest.mark.parametrize("body, expected", [
    ("def broken(:\n", "SyntaxError"),
    ("raise RuntimeError('import 실패')\n", "import 실패"),
    ("import sys\nsys.exit(3)\n", "SystemExit"),
    ("x = 1\n", "구체 클래스"),
])
def test_a_broken_file_is_reported_and_the_others_still_load(tmp_path, no_entry_points, body, expected):
    _write(tmp_path / "strategies", "a_broken.py", body)
    _write(tmp_path / "strategies", "voting.py", VOTING)

    records = _load(tmp_path)

    assert expected in records[0].problem and records[0].name == ""
    assert records[1].name == "voting" and [r.source for r in plugin_problems()] == [records[0].source]
    assert plugin_records() == records


@pytest.mark.parametrize("name", ["sequential_debate", "free_debate", "두 단어", ""])
def test_a_plugin_cannot_take_over_a_builtin_or_a_stored_name(name):
    class _Taker(SequentialDebateStrategy):
        pass

    _Taker.name = name
    original = STRATEGY_MAP.get("sequential_debate")
    with pytest.raises(ValueError):
        register_strategy(_Taker())
    assert STRATEGY_MAP["sequential_debate"] is original


def test_a_strategy_must_have_a_name_on_the_screen_and_the_right_type():
    class _NoLabel(SequentialDebateStrategy):
        name = "no_label"
        display_name = " "

    with pytest.raises(ValueError, match="display_name"):
        register_strategy(_NoLabel())
    with pytest.raises(ValueError, match="BaseDebateStrategy"):
        register_strategy(object())


def test_two_plugins_cannot_share_a_name(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "a.py", VOTING)
    _write(tmp_path / "strategies", "b.py", VOTING)

    records = _load(tmp_path)

    assert records[0].name == "voting" and "이미 쓰입니다" in records[1].problem


def test_a_class_with_an_abstract_method_left_is_not_a_plugin(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "half.py", """
        from app.orchestration.strategies import BaseDebateStrategy

        class Half(BaseDebateStrategy):
            name = "half"
            display_name = "반쪽"
    """)

    assert "구체 클래스" in _load(tmp_path)[0].problem


# --------------------------------------------------------------- 3. 앱 권한으로 도는 코드


def test_a_folder_inside_the_default_workspace_is_not_read(tmp_path, monkeypatch, no_entry_points):
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(plugins_module, "resolve_workspace_dir", lambda: workspace)
    _write(workspace / "strategies", "voting.py", "raise RuntimeError('import 하면 안 됩니다')\n")

    records = _load(tmp_path, strategies_dir=str(workspace / "strategies"))

    assert "기본 작업 공간" in records[0].problem and "voting" not in STRATEGY_MAP


def test_tools_cannot_reach_a_plugin_folder_even_inside_a_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    folder = workspace / "plugins"
    monkeypatch.setattr(manager_module, "get_config", lambda: _config(tmp_path, strategies_dir=str(folder)))

    protected = manager_module.protected_files()
    assert folder in protected

    write = CallProfile("filesystem", "write_file", "write", [
        Action("write", "plugins/evil.py", "write", str(folder / "evil.py").replace("\\", "/"), "arg"),
    ])
    assert "플러그인" in hard_block(write, workspace, None, protected)


def test_relative_folders_resolve_against_the_project_root():
    from app.config import PROJECT_ROOT

    assert PluginsConfig().folders == {
        "strategy": PROJECT_ROOT / "strategies", "host_tool": PROJECT_ROOT / "host_tools",
    }
    assert PluginsConfig(strategies_dir=" ", host_tools_dir="").folders == {}


def test_a_config_with_a_plugin_server_name_is_refused_when_loading_the_plugin(tmp_path, monkeypatch, no_entry_points):
    """설정이 먼저 읽히므로 시작할 때는 플러그인 쪽이 물러섭니다. 서버는 그대로 돕니다."""
    _write(tmp_path / "host_tools", "glossary.py", GLOSSARY)
    cfg = RootConfig.model_validate({
        **BASE,
        "mcp_servers": {"glossary": {"command": "node"}},
        "plugins": {"host_tools_dir": str(tmp_path / "host_tools"), "strategies_dir": ""},
    })
    from app.agents import host_tools as host_tools_module
    monkeypatch.setattr(host_tools_module, "get_config", lambda: cfg)

    records = load_plugins(cfg, reload=True)

    assert "MCP 서버 이름과 겹칩니다" in records[0].problem and find_host_tool("glossary__lookup") is None


# --------------------------------------------------------------- 4. 전략의 실패


class _Exploding(SequentialDebateStrategy):
    name = "exploding"
    display_name = "터지는 전략"

    def get_speakers_for_round(self, active_agents, round_num, state):
        raise RuntimeError("boom")

    def turn_instruction(self, agent, speakers, index, state):
        raise RuntimeError("boom")


class _Silent(SequentialDebateStrategy):
    name = "silent"
    display_name = "아무도 안 부르는 전략"

    def get_speakers_for_round(self, active_agents, round_num, state):
        return []


class _Strangers(SequentialDebateStrategy):
    name = "strangers"
    display_name = "없는 사람을 부르는 전략"

    def get_speakers_for_round(self, active_agents, round_num, state):
        return [Agent(key="ghost", name="Ghost", role="-"), active_agents[-1]]


class _WrongShape(SequentialDebateStrategy):
    name = "wrong_shape"
    display_name = "목록이 아닌 답"

    def get_speakers_for_round(self, active_agents, round_num, state):
        return 42

    def turn_instruction(self, agent, speakers, index, state):
        return 42


def _agents() -> List[Agent]:
    return [
        Agent(key="orchestrator", name="Orch", role="Lead", debate_priority=10),
        Agent(key="critic", name="Critic", role="Review", debate_priority=40),
        Agent(key="architect", name="Arch", role="Architecture", debate_priority=20),
    ]


STATE = DebateState(session_id="s", user_prompt="")


@pytest.mark.parametrize("strategy, words", [
    (_Exploding(), "RuntimeError: boom"),
    (_Silent(), "한 명도"),
    (_WrongShape(), "TypeError"),
])
def test_a_failing_strategy_falls_back_to_the_priority_order_and_says_why(strategy, words):
    speakers, problem = speakers_for_round(strategy, _agents(), 1, STATE)

    assert [a.key for a in speakers] == ["architect", "critic"]
    assert words in problem and strategy.name in problem


def test_only_agents_of_this_debate_can_be_called():
    speakers, problem = speakers_for_round(_Strangers(), _agents(), 1, STATE)

    assert [a.key for a in speakers] == ["architect"] and problem == ""


def test_a_debate_with_nobody_has_nothing_to_report():
    assert speakers_for_round(_Exploding(), [], 1, STATE) == ([], "")


def test_every_builtin_strategy_answers_the_same_through_the_guard():
    agents = _agents()
    for name, strategy in STRATEGY_MAP.items():
        assert speakers_for_round(strategy, agents, 1, STATE) == (
            strategy.get_speakers_for_round(agents, 1, STATE), "",
        ), name


def test_a_failing_instruction_means_no_instruction():
    agent = _agents()[1]

    assert instruction_for_turn(_Exploding(), agent, [agent], 0, STATE) == ""
    assert instruction_for_turn(_WrongShape(), agent, [agent], 0, STATE) == ""


def test_an_unknown_strategy_falls_back_and_is_logged_once(caplog):
    name = f"gone_{uuid.uuid4().hex[:6]}"

    assert resolve_strategy_name(name) == "sequential_debate"
    assert resolve_strategy_name(name) == "sequential_debate"
    assert sum(name in r.getMessage() for r in caplog.records) == 1


def _pool() -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(name="Orch", role="Lead", api_key="k", debate_priority=10),
        "architect": AgentConfig(name="Arch", role="Architecture", api_key="k", debate_priority=20),
        "critic": AgentConfig(name="Critic", role="Review", api_key="k", debate_priority=40),
    })


async def _run(strategy_name: str, llm: Optional[FakeLLMCaller] = None, max_rounds: int = 1) -> List[MessageModel]:
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"plugin-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="플러그인", strategy=strategy_name, max_rounds=max_rounds, converge_early=False,
            active_agents=["orchestrator", "architect", "critic"],
        ))
        await db.commit()
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm or FakeLLMCaller()).run_turn(sid, "설계해 주세요.")
    async with factory() as db:
        return (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()


class _Recording(FakeLLMCaller):
    """누가 어떤 지침을 받았는지 모읍니다."""

    def __init__(self) -> None:
        super().__init__()
        self.turns: Dict[str, str] = {}
        self.spoken: List[Tuple[str, str]] = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        last = messages[-1].get("content") if messages else ""
        self.turns.setdefault(agent.key, str(last))
        self.spoken.append((agent.key, str(last)))
        return await super().call_agent(agent, messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_a_plugin_strategy_really_decides_the_order_and_the_instruction(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "voting.py", VOTING)
    _load(tmp_path)
    llm = _Recording()

    messages = await _run("voting", llm)

    speeches = [m.sender_key for m in messages if m.round_number == 1 and m.msg_type == "agent"]
    assert speeches == ["critic", "architect"], "우선순위의 반대 순서입니다"
    assert "[투표]" in llm.turns["critic"] and "[투표]" in llm.turns["architect"]


@pytest.mark.asyncio
async def test_a_debate_survives_a_broken_strategy_and_the_record_says_so():
    register_strategy(_Exploding())

    messages = await _run("exploding")

    speeches = [m.sender_key for m in messages if m.round_number == 1 and m.msg_type == "agent"]
    notes = [m.content for m in messages if m.msg_type == "error"]
    assert speeches == ["architect", "critic"]
    assert any("발언자 지명 실패" in n and "RuntimeError: boom" in n for n in notes)


class _ExplodingParallel(_Exploding):
    name = "exploding_parallel"
    display_name = "터지는 병렬"
    orchestrator_dispatches_parallel = True


@pytest.mark.asyncio
async def test_a_parallel_round_survives_a_broken_strategy_and_the_record_says_so():
    register_strategy(_ExplodingParallel())

    messages = await _run("exploding_parallel")

    assert {m.sender_key for m in messages if m.round_number == 1 and m.msg_type == "agent"} == {"architect", "critic"}
    assert any("발언자 지명 실패" in m.content for m in messages if m.msg_type == "error")


# --------------------------------------------------------------- 5. 화면과 나머지 진입점


def test_the_roster_preview_and_the_choices_see_a_plugin_strategy(tmp_path, no_entry_points):
    _write(tmp_path / "strategies", "voting.py", VOTING)
    _load(tmp_path)

    roster = RosterState(agent_pool=_pool(), strategy_name="voting")

    assert [a.key for a in roster.speaking_order()] == ["critic", "architect"]
    assert {k: v.display_name for k, v in STRATEGY_MAP.items()}["voting"] == "합의 투표"


def test_a_plugin_prefix_in_an_agent_is_not_reported_as_a_missing_server(tmp_path, no_entry_points):
    _write(tmp_path / "host_tools", "glossary.py", GLOSSARY)
    assert unknown_tool_servers(["glossary", "gone"], {}) == ["glossary", "gone"]

    _load(tmp_path)

    assert unknown_tool_servers(["glossary", "gone"], {}) == ["gone"]


@pytest.mark.asyncio
async def test_the_api_lists_results_and_problems(tmp_path, monkeypatch, no_entry_points):
    from app import api as api_module

    _write(tmp_path / "strategies", "voting.py", VOTING)
    _write(tmp_path / "strategies", "zz_broken.py", "raise RuntimeError('import 실패')\n")
    cfg = _config(tmp_path)
    load_plugins(cfg, reload=True)
    monkeypatch.setattr(api_module, "get_config", lambda: cfg)

    body = await api_module.list_plugins()

    assert body["folders"]["strategy"] == str(tmp_path / "strategies")
    assert [p["name"] for p in body["plugins"]] == ["voting", None]
    assert "import 실패" in body["plugins"][1]["problem"]


def test_the_headless_api_accepts_a_plugin_strategy_by_name(tmp_path, no_entry_points):
    """헤드리스 API 와 MCP 서버의 전략 목록은 `STRATEGY_MAP` 을 그대로 봅니다."""
    from app.mcp_server import _strategies

    assert "voting" not in _strategies()
    _write(tmp_path / "strategies", "voting.py", VOTING)
    _load(tmp_path)
    assert "voting" in _strategies()


# --------------------------------------------------------------- 6. 설명서의 예제

EXAMPLES = PROJECT_ROOT / "docs" / "examples" / "plugins"
MANUAL = PROJECT_ROOT / "docs" / "user_manual" / "03-core" / "11-plugins.md"


def _load_examples() -> List[plugins_module.PluginRecord]:
    return load_plugins(RootConfig.model_validate({**BASE, "plugins": {
        "strategies_dir": str(EXAMPLES / "strategies"), "host_tools_dir": str(EXAMPLES / "host_tools"),
    }}), reload=True)


def test_the_examples_load_without_a_problem(no_entry_points):
    assert [(r.kind, r.name, r.problem) for r in _load_examples()] == [
        ("strategy", "consensus_voting", ""), ("host_tool", "glossary", ""), ("host_tool", "ticket", ""),
    ]


@pytest.mark.asyncio
async def test_the_example_strategy_proposes_then_votes_in_reverse(no_entry_points):
    _load_examples()
    llm = _Recording()

    messages = await _run("consensus_voting", llm, max_rounds=2)

    def speakers(round_number: int) -> List[str]:
        return [m.sender_key for m in messages if m.round_number == round_number and m.msg_type == "agent"]

    assert speakers(1) == ["architect", "critic"] and speakers(2) == ["critic", "architect"]
    instructions = [text for key, text in llm.spoken if key == "architect"]
    assert "[제안]" in instructions[0] and "[투표]" in instructions[1]
    assert [a.key for a in RosterState(agent_pool=_pool(), strategy_name="consensus_voting").speaking_order()] == [
        "architect", "critic",
    ]


@pytest.mark.asyncio
async def test_the_example_glossary_answers_and_explains_what_it_does_not_know(no_entry_points):
    _load_examples()
    provider = find_host_tool("glossary__lookup")
    agent = Agent(key="a", name="A", role="r", allowed_mcp_servers=["glossary"])

    async def lookup(arguments: Dict[str, Any]) -> Tuple[str, str]:
        return await run_host_tool(provider, HostToolCall(agent=agent, name="glossary__lookup", arguments=arguments))

    assert await lookup({"term": "mado"}) == (
        "MADO: Multi-Agent Debate & Orchestration Platform. 여러 에이전트의 토론을 중재하는 플랫폼입니다.", "success",
    )
    output, status = await lookup({"term": "없는말"})
    assert status == "error" and "ADR, MADO" in output
    assert (await lookup({}))[1] == "error"

    tools = host_tools_for(agent)
    assert [t["function"]["name"] for t in tools] == ["glossary__lookup"]
    assert any(text.startswith("[용어집]") for text in host_tool_guidance(tools))
    assert not any(text.startswith("[용어집]") for text in host_tool_guidance([]))


@pytest.mark.asyncio
async def test_the_example_ticket_writes_one_line_to_its_own_file(tmp_path, no_entry_points):
    _load_examples()
    provider = find_host_tool("ticket__create")
    agent = Agent(key="a", name="A", role="r", allowed_mcp_servers=["ticket"])

    async def create(arguments: Dict[str, Any], workspace: Optional[Path] = tmp_path) -> Tuple[str, str]:
        return await run_host_tool(provider, HostToolCall(
            agent=agent, name="ticket__create", arguments=arguments, workspace=workspace,
        ))

    assert (await create({"title": "첫 줄\n\n- [x] 끼워 넣기"}))[1] == "success"
    assert (await create({"title": "둘째"}))[1] == "success"
    assert (tmp_path / "tickets.md").read_text(encoding="utf-8") == "- [ ] 첫 줄 - [x] 끼워 넣기 (A)\n- [ ] 둘째 (A)\n"
    assert (await create({}))[1] == "error"
    assert (await create({"title": "x"}, workspace=None))[1] == "error"


def test_the_example_tools_are_judged_the_way_the_manual_says(no_entry_points):
    _load_examples()
    agent = Agent(key="a", name="A", role="r", allowed_mcp_servers=["glossary", "ticket"])
    mcp = SimpleNamespace(tool_meta=lambda name: None)

    def offered(mode: str) -> List[str]:
        gate = ToolGate(session_id="s", mode=mode, control=TurnControl(attended=False))
        return [t["function"]["name"] for t in gate.filter_tools("a", host_tools_for(agent), mcp)]

    assert offered("read_only") == ["glossary__lookup"]
    assert offered("default") == ["glossary__lookup", "ticket__create"]


def test_the_risk_table_in_the_manual_matches_the_policy():
    """설명이 코드와 어긋나면 사람은 설명을 믿습니다."""
    labels = {ALLOW: "실행", ASK: "묻기", DENY: "거부"}
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in MANUAL.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `") and line.count("|") == 6 and not line.startswith("| `risk`")
    ]
    covered = set()
    for name_cell, *cells in rows:
        for risk in re.findall(r"`(\w+)`", name_cell):
            covered.add(risk)
            assert cells == [labels[MODE_DEFAULTS[mode][risk]] for mode in ("read_only", "default", "review", "auto")], risk
    assert covered == set(RISK_LABELS)


def test_the_host_tool_in_the_manual_really_works():
    source = re.search(r"```python\n(.*?)```", MANUAL.read_text(encoding="utf-8"), re.S).group(1)
    namespace: Dict[str, Any] = {}
    exec(source, namespace)  # noqa: S102 - 저장소의 문서입니다

    provider = namespace["WeatherTools"]()
    register_host_tool(provider)

    agent = Agent(key="a", name="A", role="r", allowed_mcp_servers=["weather"])
    assert [t["function"]["name"] for t in host_tools_for(agent)] == ["weather__today"]
    assert host_tool_meta("weather__today").declared_risk == "read"


@pytest.mark.parametrize("document", [MANUAL, EXAMPLES / "README.md"])
def test_every_relative_link_points_at_a_real_file(document):
    for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        path = target.split("#")[0]
        if path and not target.startswith("http"):
            assert (document.parent / path).resolve().exists(), target
