"""도구 스키마 지연 로딩 (ADR-039): 무엇을 싣고, 어떻게 불러오고, 무엇이 남는가."""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from app.agents import llm as llm_module
from app.agents import tool_loading
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_budget import truncation_advice
from app.agents.llm_context import context_budget
from app.agents.llm_tools import (
    binary_file_guidance,
    file_editing_guidance,
    file_writing_guidance,
    memory_search_tool,
    memory_write_tool,
)
from app.agents.tool_loading import (
    CALLED_BEFORE_LOADING_NOTE,
    CATALOG_LINE_CHARS,
    LOAD_TOOLS_TOOL,
    TOOL_LOADING_GUIDANCE_HEAD,
    ToolShelf,
    catalog_line,
    load_tools_answer,
    plan_shelf,
    remember_loaded,
    remembered_loaded,
    tool_loading_guidance,
)
from app.config import RootConfig, ToolLoadingConfig
from app.orchestration.conversation import ConversationMemory

PADDING = "This parameter takes a value described at some length, as real MCP schemas do. " * 6


def _tool(name: str, description: str = "") -> Dict[str, Any]:
    server, tail = name.split("__", 1)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"[{server}] {description or f'Does {tail}. And more about it.'}",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": PADDING}},
                "required": ["path"],
            },
        },
    }


FILESYSTEM = [
    "read_file", "read_text_file", "read_media_file", "read_multiple_files", "write_file", "edit_file",
    "create_directory", "list_directory", "list_directory_with_sizes", "directory_tree", "move_file",
    "search_files", "get_file_info", "list_allowed_directories",
]
MEMORY = [
    "create_entities", "create_relations", "add_observations", "delete_entities", "delete_observations",
    "delete_relations", "read_graph", "search_nodes", "open_nodes",
]
GIT = [
    "git_status", "git_diff_unstaged", "git_diff_staged", "git_diff", "git_commit", "git_add", "git_reset",
    "git_log", "git_create_branch", "git_checkout", "git_show", "git_branch",
]
SANDBOX = [
    "execute_python_code", "run_python_file", "write_workspace_file", "append_workspace_file",
    "list_workspace_files", "install_python_packages", "reset_kernel_state",
]


def _server(key: str, tails: Sequence[str]) -> List[Dict[str, Any]]:
    return [_tool(f"{key}__{tail}") for tail in tails]


def _three_servers() -> List[Dict[str, Any]]:
    return _server("filesystem", FILESYSTEM) + _server("memory", MEMORY) + _server("git", GIT)


def _names(tools: Sequence[Dict[str, Any]]) -> List[str]:
    return [tool["function"]["name"] for tool in tools]


def _coder(**extra: Any) -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", api_key="sk-test", model="openai/gpt-4o", **extra)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """설정은 기본값으로 시작합니다. 개발 PC 의 conf.json 을 보지 않고, 기억도 비웁니다."""
    settings = ToolLoadingConfig()
    monkeypatch.setattr(tool_loading, "get_config", lambda: SimpleNamespace(tool_loading=settings))
    tool_loading._remembered.clear()
    yield settings
    tool_loading._remembered.clear()


# =============================================================== 1. 무엇을 싣는가


def test_tools_the_guidance_names_and_core_tools_keep_their_schema_and_the_rest_wait():
    shelf = plan_shelf(_coder(), _three_servers())
    names = _names(shelf.offered())

    for kept in (
        "filesystem__read_text_file", "filesystem__write_file", "filesystem__edit_file",  # 지침이 짚습니다
        "memory__create_entities", "memory__search_nodes",                                # 안내가 짚습니다
        "filesystem__list_directory", "filesystem__search_files", "memory__add_observations",
        "git__git_status", "git__git_diff_unstaged",                                       # core_tools 기본값
    ):
        assert kept in names
    for waiting in ("git__git_log", "memory__delete_entities", "filesystem__directory_tree", "filesystem__read_file"):
        assert waiting not in names and waiting in shelf.waiting
    assert names[-1] == LOAD_TOOLS_TOOL


def test_the_catalog_saves_more_than_half_of_the_schema_tokens():
    tools = _three_servers()
    agent = _coder(max_tokens=1024, max_context_window=128_000)
    shelf = plan_shelf(agent, tools)
    full, lazy = context_budget(agent, tools=tools), context_budget(agent, tools=shelf.offered())
    assert lazy - full > (128_000 - 1024 - 512 - full) / 2


@pytest.mark.parametrize("servers", [
    _three_servers(),
    _three_servers() + _server("sandbox", SANDBOX),
    _server("sandbox", SANDBOX) + _server("memory", MEMORY) + _server("git", GIT),
], ids=["no-sandbox", "with-sandbox", "sandbox-only-files"])
def test_every_standing_rule_reads_the_same_with_the_split_list(servers, _settings):
    _settings.min_saving_tokens = 0
    offered = plan_shelf(_coder(), servers).offered()
    assert len(offered) < len(servers)

    for rule in (file_writing_guidance, file_editing_guidance, binary_file_guidance, truncation_advice):
        assert rule(offered) == rule(servers), rule.__name__
    assert memory_write_tool(offered) == memory_write_tool(servers)
    assert memory_search_tool(offered) == memory_search_tool(servers)


def test_reading_stays_sent_when_read_only_mode_removed_the_tools_that_change_files():
    """`edit_file` 이 없으면 지침은 읽기 도구를 짚지 않습니다. 그래도 읽기는 목록으로 밀리면 안 됩니다."""
    removed = {"write_file", "edit_file", "move_file", "create_directory"}
    tools = _server("filesystem", [t for t in FILESYSTEM if t not in removed]) + _server("memory", MEMORY) + _server("git", GIT)
    assert "filesystem__read_text_file" in _names(plan_shelf(_coder(), tools).offered())


def test_the_tool_that_runs_skill_scripts_is_always_sent(_settings):
    _settings.min_saving_tokens = 0
    _settings.core_tools = []
    assert "sandbox__run_python_file" in _names(plan_shelf(_coder(), _three_servers() + _server("sandbox", SANDBOX)).offered())


def test_the_sequential_thinking_tool_the_prompt_calls_for_is_always_sent(_settings):
    _settings.min_saving_tokens = 0
    tools = _three_servers() + _server("thinking", ["sequentialthinking", "a", "b", "c"])
    assert "thinking__sequentialthinking" in _names(plan_shelf(_coder(), tools).offered())


def test_a_small_server_stays_whole(_settings):
    _settings.min_saving_tokens = 0
    tools = _three_servers() + _server("kb", ["search", "get", "stats"])
    assert {"kb__search", "kb__get", "kb__stats"} <= set(_names(plan_shelf(_coder(), tools).offered()))


@pytest.mark.parametrize("entry,kept", [
    ("git_log", ["git__git_log"]),
    ("git__git_log", ["git__git_log"]),
    ("git", ["git__git_log", "git__git_commit"]),
])
def test_core_tools_takes_a_tool_name_a_full_name_or_a_server_key(_settings, entry, kept):
    _settings.core_tools = [entry]
    names = _names(plan_shelf(_coder(), _three_servers()).offered())
    assert set(kept) <= set(names)


def test_nothing_is_split_when_the_catalog_would_save_too_little(_settings):
    _settings.min_saving_tokens = 10_000_000
    tools = _three_servers()
    shelf = plan_shelf(_coder(), tools)
    assert _names(shelf.offered()) == _names(tools) and not shelf.waiting


def test_turning_it_off_sends_everything(_settings):
    _settings.enabled = False
    tools = _three_servers()
    assert _names(plan_shelf(_coder(), tools).offered()) == _names(tools)


def test_a_failure_while_splitting_still_sends_everything(monkeypatch):
    def broken():
        raise RuntimeError("설정을 읽지 못했습니다")

    monkeypatch.setattr(tool_loading, "get_config", broken)
    tools = _three_servers()
    assert _names(plan_shelf(_coder(), tools).offered()) == _names(tools)


def test_host_tools_are_always_sent_and_no_tools_means_no_catalog():
    host = [{"type": "function", "function": {"name": "skills__load_skill", "parameters": {}}}]
    assert _names(plan_shelf(_coder(), [], host).offered()) == ["skills__load_skill"]
    assert "skills__load_skill" in _names(plan_shelf(_coder(), _three_servers(), host).offered())


# =============================================================== 2. 목록


def test_a_catalog_line_is_the_name_and_the_first_sentence():
    tool = _tool("git__git_log", "Shows the commit logs. Supports many options.\nMore lines.")
    assert catalog_line(tool) == "- git__git_log: Shows the commit logs"
    assert catalog_line(_tool("kb__x", "한국어 설명입니다. 다음 문장")) == "- kb__x: 한국어 설명입니다"
    assert catalog_line({"function": {"name": "bare"}}) == "- bare"


def test_a_long_first_sentence_is_cut():
    line = catalog_line(_tool("git__x", "word " * 100))
    assert line.endswith("…") and len(line) <= len("- git__x: ") + CATALOG_LINE_CHARS


def test_the_guidance_comes_only_with_the_catalog():
    assert tool_loading_guidance(plan_shelf(_coder(), _three_servers()).offered()).startswith(TOOL_LOADING_GUIDANCE_HEAD)
    assert tool_loading_guidance(_server("git", GIT)) is None
    assert tool_loading_guidance(None) is None


# =============================================================== 3. 불러오기


def _shelf() -> ToolShelf:
    return plan_shelf(_coder(), _three_servers())


def test_loading_moves_the_schema_and_shrinks_the_catalog():
    shelf = _shelf()
    output, status, loaded = load_tools_answer(shelf, {"names": ["git__git_log", "git__git_show"]})
    names = _names(shelf.offered())
    catalog = shelf.offered()[-1]["function"]["description"]

    assert status == "success" and loaded == ["git__git_log", "git__git_show"] and "git__git_log" in output
    assert "git__git_log" in names and "git__git_show" in names
    assert "git__git_log" not in catalog and "git__git_commit" in catalog


def test_loading_takes_a_tail_or_a_whole_server():
    shelf = _shelf()
    assert load_tools_answer(shelf, {"names": ["git_log"]})[2] == ["git__git_log"]
    loaded = load_tools_answer(shelf, {"names": ["memory"]})[2]
    assert "memory__delete_entities" in loaded and "memory__read_graph" in loaded


def test_a_name_it_cannot_find_is_answered_with_what_can_be_loaded():
    shelf = _shelf()
    output, status, loaded = load_tools_answer(shelf, {"names": ["nope__nothing"]})
    assert status == "error" and not loaded and "nope__nothing" in output and "git__git_log" in output


def test_a_tool_already_sent_is_not_an_error():
    output, status, loaded = load_tools_answer(_shelf(), {"names": ["filesystem__write_file"]})
    assert status == "success" and not loaded and "이미" in output


@pytest.mark.parametrize("arguments", [{}, {"names": []}, {"names": 3}, "broken", None])
def test_broken_arguments_are_answered_not_raised(arguments):
    output, status, loaded = load_tools_answer(_shelf(), arguments)
    assert status == "error" and not loaded and "git__git_log" in output


def test_a_single_name_as_a_string_still_works():
    assert load_tools_answer(_shelf(), {"name": "git__git_log"})[2] == ["git__git_log"]
    assert load_tools_answer(_shelf(), {"names": "git__git_show"})[2] == ["git__git_show"]


def test_loading_everything_removes_the_catalog():
    shelf = _shelf()
    load_tools_answer(shelf, {"names": list(shelf.waiting)})
    assert not shelf.waiting and LOAD_TOOLS_TOOL not in _names(shelf.offered())


# =============================================================== 4. 도구 루프


class _MCP:
    def __init__(self, tools: Optional[List[Dict[str, Any]]] = None, status: str = "success"):
        self.tools = _three_servers() if tools is None else tools
        self.status = status
        self.calls: List[str] = []

    def get_openai_tools_for_servers(self, servers):
        return self.tools

    async def execute_tool(self, name, args, scope=None, actor=None):
        self.calls.append(name)
        return f"[{name} 결과]", self.status


class _Gate:
    """늘 거부될 도구를 빼고, 호출마다 판정한 이름을 남깁니다."""

    def __init__(self, deny: Sequence[str] = (), always_deny: Sequence[str] = ()):
        self.deny, self.always_deny = set(deny), set(always_deny)
        self.checked: List[str] = []

    def filter_tools(self, agent_key, tools, mcp):
        return [tool for tool in tools if tool["function"]["name"] not in self.always_deny]

    async def check(self, agent, tool_name, arguments, mcp):
        self.checked.append(tool_name)
        if tool_name in self.deny:
            return SimpleNamespace(allowed=False, output="거부", status="denied", audit={"effect": "deny"})
        return SimpleNamespace(allowed=True, output="", status="", audit={"effect": "allow"})


class _Message(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


Reply = Tuple[str, Sequence[Tuple[str, Dict[str, Any]]]]


def _install(monkeypatch, replies: List[Reply]) -> List[Dict[str, Any]]:
    """`replies` 를 차례로 돌려주는 가짜 엔드포인트. 각 요청의 인자를 모아 돌려줍니다."""
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
            message=_Message(content=content, tool_calls=tool_calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)
    return requests


async def _speak(mcp: _MCP, agent: Optional[Agent] = None, session_id: Optional[str] = "s1", **kwargs: Any):
    return await LLMCaller(mcp_manager=mcp).call_agent(
        agent or _coder(), [{"role": "user", "content": "조사하세요"}], session_id=session_id, **kwargs,
    )


@pytest.mark.asyncio
async def test_a_loaded_schema_goes_out_from_the_next_request(monkeypatch):
    requests = _install(monkeypatch, [
        ("이력을 봅니다", [(LOAD_TOOLS_TOOL, {"names": ["git__git_log"]})]),
        ("", [("git__git_log", {"path": "."})]),
        ("끝", []),
    ])
    gate, mcp = _Gate(), _MCP()
    content, logs = await _speak(mcp, tool_gate=gate)

    first, second, third = (_names(request["tools"]) for request in requests)
    assert "git__git_log" not in first and LOAD_TOOLS_TOOL in first
    assert "git__git_log" in second and "git__git_log" in third
    git_log = next(tool for tool in requests[1]["tools"] if tool["function"]["name"] == "git__git_log")
    assert git_log["function"]["parameters"]["required"] == ["path"], "목록이 아니라 정의를 싣습니다"

    assert TOOL_LOADING_GUIDANCE_HEAD in requests[0]["messages"][0]["content"]
    assert [log["tool_name"] for log in logs] == [LOAD_TOOLS_TOOL, "git__git_log"]
    assert logs[0]["status"] == "success" and logs[0]["security"] == {}
    assert gate.checked == ["git__git_log"], "불러오기는 판정하지 않고, 불러온 도구의 호출은 판정합니다"
    assert mcp.calls == ["git__git_log"] and content == "이력을 봅니다\n\n끝"


@pytest.mark.asyncio
async def test_the_next_speech_of_that_agent_starts_with_what_it_loaded(monkeypatch):
    requests = _install(monkeypatch, [
        ("", [(LOAD_TOOLS_TOOL, {"names": ["git__git_log"]})]),
        ("끝", []),
        ("다음 발언", []),
        ("다른 에이전트", []),
        ("다른 대화", []),
    ])
    await _speak(_MCP())
    await _speak(_MCP())
    await _speak(_MCP(), Agent(key="critic", name="Critic", role="R", api_key="sk-test", model="openai/gpt-4o"))
    await _speak(_MCP(), session_id="s2")

    assert "git__git_log" in _names(requests[2]["tools"]), "같은 대화의 같은 에이전트"
    assert "git__git_log" not in _names(requests[3]["tools"]), "다른 에이전트"
    assert "git__git_log" not in _names(requests[4]["tools"]), "다른 대화"
    assert _names(requests[1]["tools"]) == _names(requests[2]["tools"]), "요청의 앞부분이 그대로여야 캐시에 남습니다"


@pytest.mark.asyncio
async def test_calling_a_listed_tool_directly_runs_it_and_loads_it(monkeypatch):
    requests = _install(monkeypatch, [("", [("git__git_log", {"path": "."})]), ("끝", [])])
    gate, mcp = _Gate(), _MCP()
    _content, logs = await _speak(mcp, tool_gate=gate)

    assert mcp.calls == ["git__git_log"] and gate.checked == ["git__git_log"]
    assert "git__git_log" in _names(requests[1]["tools"])
    assert CALLED_BEFORE_LOADING_NOTE not in logs[0]["output"]


@pytest.mark.asyncio
async def test_a_direct_call_that_failed_is_told_to_read_the_schema(monkeypatch):
    requests = _install(monkeypatch, [("", [("git__git_log", {"wrong": 1})]), ("끝", [])])
    _content, logs = await _speak(_MCP(status="error"))
    assert logs[0]["output"].endswith(CALLED_BEFORE_LOADING_NOTE)
    assert "git__git_log" in _names(requests[1]["tools"])


@pytest.mark.asyncio
async def test_a_refused_call_does_not_load_anything(monkeypatch):
    requests = _install(monkeypatch, [("", [("git__git_commit", {"path": "."})]), ("끝", [])])
    mcp = _MCP()
    await _speak(mcp, tool_gate=_Gate(deny=["git__git_commit"]))
    assert not mcp.calls and "git__git_commit" not in _names(requests[1]["tools"])


@pytest.mark.asyncio
async def test_a_tool_the_gate_always_denies_is_not_in_the_catalog(monkeypatch):
    requests = _install(monkeypatch, [("끝", [])])
    await _speak(_MCP(), tool_gate=_Gate(always_deny=["git__git_commit"]))
    catalog = requests[0]["tools"][-1]["function"]["description"]
    assert "git__git_commit" not in catalog and "git__git_log" in catalog


@pytest.mark.asyncio
async def test_a_hallucinated_name_loads_nothing(monkeypatch):
    requests = _install(monkeypatch, [("", [("git", {})]), ("끝", [])])
    await _speak(_MCP())
    assert "git__git_log" not in _names(requests[1]["tools"]), "도구 이름이 서버 키와 같아도 서버를 통째로 싣지 않습니다"


@pytest.mark.asyncio
async def test_the_wrap_up_call_keeps_what_was_loaded(monkeypatch):
    requests = _install(monkeypatch, [
        ("", [(LOAD_TOOLS_TOOL, {"names": ["git__git_log"]})]),
        ("지금까지로 답합니다", []),
    ])
    await _speak(_MCP(), _coder(max_tool_iterations=1))
    assert requests[1]["tool_choice"] == "none" and "git__git_log" in _names(requests[1]["tools"])


@pytest.mark.asyncio
async def test_a_resumed_speech_restores_what_it_loaded_after_a_restart(monkeypatch):
    _install(monkeypatch, [("", [(LOAD_TOOLS_TOOL, {"names": ["git__git_log"]})]), ("끝", [])])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    await _speak(_MCP(), checkpoint=checkpoint)
    assert saved[-1]["loaded_tools"] == ["git__git_log"]

    tool_loading._remembered.clear()    # 재시작하면 기억은 비어 있습니다
    requests = _install(monkeypatch, [("이어서 끝", [])])
    await _speak(_MCP(), resume_state=saved[-1])
    assert "git__git_log" in _names(requests[0]["tools"])
    assert remembered_loaded("s1", "coder") == ["git__git_log"]


@pytest.mark.asyncio
async def test_with_no_session_nothing_is_remembered(monkeypatch):
    _install(monkeypatch, [("", [(LOAD_TOOLS_TOOL, {"names": ["git__git_log"]})]), ("끝", [])])
    await _speak(_MCP(), session_id=None)
    assert not tool_loading._remembered


@pytest.mark.asyncio
async def test_calling_the_load_tool_when_nothing_is_listed_is_answered(monkeypatch):
    few = _server("git", ["git_status", "git_log"])
    requests = _install(monkeypatch, [("", [(LOAD_TOOLS_TOOL, {"names": ["x"]})]), ("끝", [])])
    _content, logs = await _speak(_MCP(tools=few))
    assert LOAD_TOOLS_TOOL not in _names(requests[0]["tools"])
    assert logs[0]["status"] == "error" and "x" in logs[0]["output"]


# =============================================================== 5. 기억과 나머지


def test_the_memory_keeps_the_order_and_forgets_the_oldest(monkeypatch):
    remember_loaded("s", "a", ["x", "y"])
    remember_loaded("s", "a", ["y", "z"])
    assert remembered_loaded("s", "a") == ["x", "y", "z"]

    monkeypatch.setattr(tool_loading, "MAX_REMEMBERED", 2)
    remember_loaded("s", "b", ["x"])
    remember_loaded("s", "c", ["x"])
    assert remembered_loaded("s", "a") == [] and remembered_loaded("s", "c") == ["x"]


def test_the_budget_for_the_transcript_counts_what_the_speech_will_send():
    """전사 크기를 정하는 쪽(`tools_for`)과 발언이 보내는 쪽이 같은 목록을 봅니다."""
    mcp = _MCP()
    caller = SimpleNamespace(mcp_manager=mcp, resolve_tool_servers=lambda agent: [])
    context = SimpleNamespace(llm_caller=caller, mcp_for=lambda state: mcp, gate_for=lambda state: None)
    memory = ConversationMemory.__new__(ConversationMemory)
    memory.ctx = context
    state = SimpleNamespace(session_id="s1", skill_designations={})

    remember_loaded("s1", "coder", ["git__git_log"])
    expected = _names(plan_shelf(_coder(), mcp.tools, session_id="s1").offered())
    assert _names(memory.tools_for(_coder(), state)) == expected
    assert "git__git_log" in expected and LOAD_TOOLS_TOOL in expected


def test_the_prefix_is_reserved():
    base = {"agents": {"orchestrator": {"name": "O", "role": "R", "model": "openai/gpt-4o"}}}
    with pytest.raises(ValueError, match="tools__load_tools"):
        RootConfig.model_validate({**base, "mcp_servers": {"tools": {"command": "node"}}})


def test_the_reserved_prefix_is_refused_when_adding_a_server(tmp_path):
    from app.config import add_mcp_server_to_conf_file

    conf = tmp_path / "conf.json"
    conf.write_text(
        json.dumps({"agents": {"orchestrator": {"name": "O", "role": "R", "model": "openai/gpt-4o"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="예약"):
        add_mcp_server_to_conf_file("tools", "node", [], {}, True, conf)


def test_the_screen_label_names_the_tools():
    from app.agents.catalog import tool_label

    assert tool_label(LOAD_TOOLS_TOOL, {"names": ["git__git_log"]}) == "도구 꺼내 오기: git__git_log"
