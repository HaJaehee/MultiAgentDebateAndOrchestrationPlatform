"""화면에서 에이전트를 추가·비활성화·삭제할 때 conf.json 이 어떻게 바뀌는가.

MCP 서버 편집(`test_mcp_admin.py`)과 같은 규칙을 따릅니다 — `//` 설명이 살아남고,
`${VAR}` 표기가 그대로 남습니다. 여기에 하나가 더 붙습니다.

**미리 채운 기본값은 되쓰지 않는다.** 화면은 새 에이전트 폼을 llm (곧 .env) 의
유효 기본값으로 채워 보여주지만, 사용자가 실제로 바꾼 항목만 파일에 적습니다.
화면이 보는 값은 이미 환경변수가 풀린 값이라, 그대로 되쓰면 해석된 API 키가
conf.json 에 평문으로 박히고 .env 를 바꿔도 따라오지 않게 됩니다.
"""

from pathlib import Path
from typing import List

import pytest

from app.config import (
    LLMConfig,
    add_agent_to_conf_file,
    agent_defaults_from_llm,
    prune_agent_overrides,
    read_conf_file,
    remove_agent_from_conf_file,
    set_agent_debate_order_in_conf_file,
    set_agent_debate_stance_in_conf_file,
    set_agent_enabled_in_conf_file,
    strip_comment_keys,
    write_conf_file,
)
from app.ui.components import roster as roster_module
from app.ui.components.roster import AgentRosterControl

SAMPLE = {
    "app": {"port": 8000},
    "llm": {
        "model": "${LLM_MODEL:-openai/gpt-4o}",
        "api_key": "${LLM_API_KEY}",
        "temperature": 0.4,
        "max_tokens": 4096,
    },
    "mcp_servers": {
        "filesystem": {
            "command": "${NODE_BIN:-node}",
            "args": ["${MCP_NODE_HOME:-./mcp_node}/server.js"],
        }
    },
    "agents": {
        "// orchestrator": "1. 필수 오케스트레이터",
        "orchestrator": {
            "name": "Master Orchestrator",
            "role": "Moderator & Synthesizer",
            "model": "${ORCHESTRATOR_MODEL:-${LLM_MODEL:-openai/gpt-4o}}",
            "allowed_mcp_servers": ["filesystem"],
            "sequential_thinking": {"enabled": True, "max_steps": 6},
        },
        "// critic": "2. 크리티컬 리뷰어. 프롬프트 안의 대괄호 줄은 그냥 글입니다.",
        "critic": {
            "name": "Critic",
            "role": "Review",
            "system_prompt": ["[검토 항목]", "- 보안 취약점"],
            "allowed_mcp_servers": [],
        },
        "// 예시": [
            "이 설명은 아래 예시 블록을 위한 것입니다.",
            '  "example": { "model": "openai/gpt-4o-mini" }',
        ],
    },
}


@pytest.fixture()
def conf(tmp_path: Path) -> Path:
    path = tmp_path / "conf.json"
    write_conf_file(path, SAMPLE)
    return path


def _load(path: Path) -> dict:
    """설정값만 (`//` 설명은 걷어낸 뒤)."""
    return strip_comment_keys(read_conf_file(path))


# --------------------------------------------------------------- 미리 채운 기본값


def test_defaults_come_from_llm_then_fall_back_to_the_model():
    """llm 에 있으면 그것을, 없으면 `AgentConfig` 의 기본값을 씁니다."""
    defaults = agent_defaults_from_llm(
        LLMConfig(model="openai/gpt-4o-mini", temperature=0.4)
    )

    assert defaults["model"] == "openai/gpt-4o-mini"   # llm 에서
    assert defaults["temperature"] == 0.4              # llm 에서
    assert defaults["max_tokens"] == 4096              # AgentConfig 기본값
    assert defaults["drop_params"] is True             # AgentConfig 기본값


def test_untouched_fields_are_not_written_back():
    """폼을 그대로 제출하면 아무 항목도 기록되지 않습니다 → 전부 llm 상속."""
    defaults = agent_defaults_from_llm(
        LLMConfig(model="openai/gpt-4o", api_key="sk-live-secret", temperature=0.4)
    )

    assert prune_agent_overrides(dict(defaults), defaults) == {}


def test_only_the_changed_fields_survive():
    defaults = agent_defaults_from_llm(
        LLMConfig(model="openai/gpt-4o", api_key="sk-live-secret", temperature=0.4)
    )
    submitted = dict(defaults)
    submitted["temperature"] = 0.1
    submitted["max_tokens"] = 8192

    assert prune_agent_overrides(submitted, defaults) == {
        "temperature": 0.1,
        "max_tokens": 8192,
    }, "해석된 API 키는 따라 나오면 안 됩니다"


def test_blank_input_means_inherit_not_empty_string():
    defaults = agent_defaults_from_llm(LLMConfig(model="openai/gpt-4o"))

    assert "api_base" not in prune_agent_overrides({"api_base": "   "}, defaults)


# --------------------------------------------------------------- 추가


def test_added_agent_lands_in_the_agent_group_and_inherits_the_rest(conf: Path):
    add_agent_to_conf_file(
        "data_analyst",
        "Data Analyst",
        "Data & Metrics",
        system_prompt="당신은 데이터 분석가입니다.\n지표의 정의와 표본을 먼저 따집니다.",
        allowed_mcp_servers=["filesystem"],
        overrides={"temperature": 0.15, "max_tokens": 8192},
        config_path=conf,
    )

    agent = _load(conf)["agents"]["data_analyst"]
    assert agent["name"] == "Data Analyst"
    assert agent["role"] == "Data & Metrics"
    assert agent["temperature"] == 0.15
    assert agent["max_tokens"] == 8192
    assert agent["allowed_mcp_servers"] == ["filesystem"]
    # 여러 줄 글은 한 줄이 한 항목인 배열로 적힙니다.
    assert agent["system_prompt"] == [
        "당신은 데이터 분석가입니다.",
        "지표의 정의와 표본을 먼저 따집니다.",
    ]
    # 적지 않은 항목은 파일에도 없어야 llm 을 상속합니다.
    assert "model" not in agent
    assert "api_key" not in agent

    # 에이전트 무리 안에, 마지막 자리에 들어갑니다.
    assert list(_load(conf)["agents"]) == ["orchestrator", "critic", "data_analyst"]
    text = conf.read_text(encoding="utf-8")
    assert text.index('"data_analyst"') > text.index('"critic"')


def test_added_agent_actually_inherits_the_llm_section(conf: Path):
    """파일에 적지 않은 값이 설정을 읽었을 때 llm 값으로 채워지는지."""
    add_agent_to_conf_file("data_analyst", "Data Analyst", "Data", config_path=conf)

    from app.config import load_config

    agent = load_config(conf).agents["data_analyst"]
    assert agent.temperature == 0.4      # llm
    assert agent.max_tokens == 4096      # llm


def test_multiline_prompts_survive_the_round_trip(conf: Path):
    """배열로 적고 문자열로 읽습니다. 그 왕복에서 글이 상하면 안 됩니다."""
    from app.config import load_config

    prompt = "첫 줄\n둘째 줄\n\n넷째 줄"
    add_agent_to_conf_file(
        "writer", "Writer", "Docs", system_prompt=prompt, config_path=conf
    )

    assert load_config(conf).agents["writer"].system_prompt == prompt


def test_integers_are_written_as_integers(conf: Path):
    """`ui.number` 는 무엇을 넣든 float 를 돌려줍니다. 8192.0 이 적히면 안 됩니다."""
    add_agent_to_conf_file(
        "data_analyst", "Data Analyst", "Data",
        overrides={"max_tokens": 8192.0, "num_retries": 3.0},
        config_path=conf,
    )

    text = conf.read_text(encoding="utf-8")
    assert '"max_tokens": 8192' in text and "8192.0" not in text
    assert '"num_retries": 3' in text and "3.0" not in text

    agent = _load(conf)["agents"]["data_analyst"]
    assert isinstance(agent["max_tokens"], int)
    assert agent["max_tokens"] == 8192


def test_sequential_thinking_block_is_optional(conf: Path):
    add_agent_to_conf_file("plain", "Plain", "Role", config_path=conf)
    assert "sequential_thinking" not in _load(conf)["agents"]["plain"]

    add_agent_to_conf_file(
        "thinker", "Thinker", "Role",
        sequential_thinking={"enabled": True, "mode": "prompt", "max_steps": 7},
        config_path=conf,
    )
    thinking = _load(conf)["agents"]["thinker"]["sequential_thinking"]
    assert thinking == {"enabled": True, "mode": "prompt", "max_steps": 7}


def test_comments_and_placeholders_survive_an_add(conf: Path):
    add_agent_to_conf_file("data_analyst", "Data Analyst", "Data", config_path=conf)

    text = conf.read_text(encoding="utf-8")
    assert "1. 필수 오케스트레이터" in text
    assert "${ORCHESTRATOR_MODEL:-${LLM_MODEL:-openai/gpt-4o}}" in text
    assert "${LLM_API_KEY}" in text


def test_add_rejects_duplicates_and_bad_input(conf: Path):
    with pytest.raises(ValueError):
        add_agent_to_conf_file("critic", "Another Critic", "Role", config_path=conf)
    with pytest.raises(ValueError):
        add_agent_to_conf_file("has space", "X", "Role", config_path=conf)
    with pytest.raises(ValueError):
        add_agent_to_conf_file("ok_key", "   ", "Role", config_path=conf)
    with pytest.raises(ValueError):
        add_agent_to_conf_file("ok_key", "Name", "  ", config_path=conf)
    with pytest.raises(ValueError):
        add_agent_to_conf_file("ok_key", "Name", "Role", overrides={"nope": 1}, config_path=conf)


# --------------------------------------------------------------- 비활성화


def test_disable_and_enable_only_touch_the_enabled_flag(conf: Path):
    set_agent_enabled_in_conf_file("critic", False, conf)

    data = _load(conf)
    assert data["agents"]["critic"]["enabled"] is False
    assert data["agents"]["critic"]["name"] == "Critic"
    assert data["agents"]["critic"]["system_prompt"][0] == "[검토 항목]"
    # 하위 블록의 enabled 를 건드리지 않았습니다.
    assert data["agents"]["orchestrator"]["sequential_thinking"]["enabled"] is True

    set_agent_enabled_in_conf_file("critic", True, conf)
    assert _load(conf)["agents"]["critic"]["enabled"] is True


def test_the_orchestrator_cannot_be_switched_off(conf: Path):
    with pytest.raises(ValueError):
        set_agent_enabled_in_conf_file("orchestrator", False, conf)
    assert _load(conf)["agents"]["orchestrator"].get("enabled") is not False


def test_disabling_an_unknown_agent_is_an_error(conf: Path):
    with pytest.raises(KeyError):
        set_agent_enabled_in_conf_file("nope", False, conf)


# --------------------------------------------------------------- 삭제


def test_delete_removes_the_agent_and_its_sub_blocks(conf: Path):
    add_agent_to_conf_file(
        "thinker", "Thinker", "Role",
        sequential_thinking={"enabled": True, "max_steps": 3},
        config_path=conf,
    )
    remove_agent_from_conf_file("thinker", conf)

    text = conf.read_text(encoding="utf-8")
    assert "thinker" not in text
    assert "thinker" not in _load(conf)["agents"]


def test_add_then_delete_leaves_the_file_byte_for_byte(conf: Path):
    """지웠다 다시 추가하기를 반복해도 파일이 늘어지지 않아야 합니다."""
    before = conf.read_text(encoding="utf-8")

    add_agent_to_conf_file(
        "data_analyst", "Data Analyst", "Data",
        system_prompt="여러 줄\n프롬프트",
        allowed_mcp_servers=["filesystem"],
        overrides={"temperature": 0.15},
        sequential_thinking={"enabled": True},
        config_path=conf,
    )
    remove_agent_from_conf_file("data_analyst", conf)

    assert conf.read_text(encoding="utf-8") == before


def test_delete_keeps_the_neighbouring_comments(conf: Path):
    remove_agent_from_conf_file("critic", conf)

    text = conf.read_text(encoding="utf-8")
    assert "critic" not in _load(conf)["agents"]
    # 위에 붙은 설명은 사람이 쓴 글입니다. 지우지 않습니다.
    assert "2. 크리티컬 리뷰어" in text
    # 다음 블록의 설명도 그대로입니다.
    assert "이 설명은 아래 예시 블록을 위한 것입니다." in text


def test_the_orchestrator_cannot_be_deleted(conf: Path):
    with pytest.raises(ValueError):
        remove_agent_from_conf_file("orchestrator", conf)
    assert "orchestrator" in _load(conf)["agents"]


def test_deleting_an_unknown_agent_is_an_error(conf: Path):
    with pytest.raises(KeyError):
        remove_agent_from_conf_file("nope", conf)


# --------------------------------------------------------------- 화면 쪽 잠금 규칙


class _FakeRunner:
    def __init__(self, running: List[str]):
        self.running = list(running)

    def running_sessions(self) -> List[str]:
        return list(self.running)

    def is_running(self, session_id: str) -> bool:
        return session_id in self.running

    def running_elsewhere(self, session_id: str):
        return []


class _FakeManager:
    def __init__(self):
        self.reloads = 0
        self.workspace = Path(".").resolve()

    async def reload_from_config(self):
        self.reloads += 1
        return {}

    def connection_status(self):
        return {}


class _FakePool:
    """런타임을 실제로 띄우지 않는 풀 (이 대화의 폴더에 하나가 떠 있는 상태)."""

    def __init__(self, manager: "_FakeManager"):
        self.manager = manager

    def get(self, workspace=None):
        return self.manager

    async def reload_all(self):
        await self.manager.reload_from_config()
        return {}


@pytest.fixture()
def roster(monkeypatch):
    runner = _FakeRunner([])
    monkeypatch.setattr(roster_module, "get_debate_runner", lambda: runner)
    monkeypatch.setattr(roster_module, "get_runtime_pool", lambda: _FakePool(_FakeManager()))
    monkeypatch.setattr(roster_module.ui, "notify", lambda *a, **k: None)

    control = AgentRosterControl()
    control.build_ui()
    return control, runner


def test_a_fresh_session_may_edit_the_agent_pool(roster):
    control, _ = roster
    assert control._agent_admin_lock_reason() == ""
    assert control.add_agent_btn._props.get("disable") is None


def test_a_started_conversation_freezes_the_agent_pool(roster):
    """첫 발언과 함께 참여 에이전트가 고정됩니다."""
    control, _ = roster
    control.set_personas_locked(True)

    assert "고정" in control._agent_admin_lock_reason()
    assert control.add_agent_btn._props.get("disable") is True


def test_a_running_debate_anywhere_freezes_the_agent_pool(roster):
    """에이전트 풀은 프로세스 전체가 하나를 공유합니다."""
    control, runner = roster
    runner.running = ["another-session"]
    control.refresh_mcp_lock()

    assert "진행 중인 대화" in control._agent_admin_lock_reason()
    assert control.add_agent_btn._props.get("disable") is True


def test_the_add_dialog_does_not_open_while_frozen(roster, monkeypatch):
    control, _ = roster
    opened: List[str] = []
    monkeypatch.setattr(roster_module.ui, "dialog", lambda *a, **k: opened.append("dialog"))

    control.set_personas_locked(True)
    control._open_agent_add_dialog()
    control._open_agent_delete_dialog("critic")

    assert opened == []


@pytest.mark.asyncio
async def test_a_frozen_roster_never_writes_to_the_conf_file(roster, monkeypatch):
    control, _ = roster
    writes: List[tuple] = []
    monkeypatch.setattr(
        roster_module, "set_agent_enabled_in_conf_file",
        lambda *args, **kwargs: writes.append(args),
    )

    control.set_personas_locked(True)
    await control._on_agent_disable("critic")
    await control._on_agent_enable("critic")

    assert writes == []


@pytest.mark.asyncio
async def test_disable_writes_once_the_conversation_is_fresh(roster, monkeypatch):
    control, _ = roster
    writes: List[tuple] = []
    monkeypatch.setattr(
        roster_module, "set_agent_enabled_in_conf_file",
        lambda *args, **kwargs: writes.append(args),
    )

    await control._on_agent_disable("critic")

    assert [args[:2] for args in writes] == [("critic", False)]


# --------------------------------------------------------------- 발언 순서 / 진영


def test_debate_order_is_written_with_gaps(conf: Path):
    """10, 20, 30... 으로 매깁니다. 사이에 자리를 남겨 두어야 한 명을 끼워 넣을 때
    나머지를 다시 쓰지 않습니다."""
    set_agent_debate_order_in_conf_file(["critic", "orchestrator"], conf)

    agents = _load(conf)["agents"]
    assert agents["critic"]["debate_priority"] == 10
    assert agents["orchestrator"]["debate_priority"] == 20


def test_reordering_rewrites_the_existing_value(conf: Path):
    set_agent_debate_order_in_conf_file(["critic", "orchestrator"], conf)
    set_agent_debate_order_in_conf_file(["orchestrator", "critic"], conf)

    agents = _load(conf)["agents"]
    assert agents["orchestrator"]["debate_priority"] == 10
    assert agents["critic"]["debate_priority"] == 20
    # 한 값만 갈아 끼웠습니다. 프롬프트와 설명은 그대로입니다.
    assert agents["critic"]["system_prompt"][0] == "[검토 항목]"
    assert "1. 필수 오케스트레이터" in conf.read_text(encoding="utf-8")


def test_reordering_an_unknown_agent_is_an_error(conf: Path):
    with pytest.raises(KeyError):
        set_agent_debate_order_in_conf_file(["orchestrator", "nope"], conf)
    # 하나라도 없으면 아무것도 쓰지 않습니다.
    assert "debate_priority" not in conf.read_text(encoding="utf-8")


def test_stance_is_written_and_validated(conf: Path):
    set_agent_debate_stance_in_conf_file("critic", "critic", conf)
    assert _load(conf)["agents"]["critic"]["debate_stance"] == "critic"

    set_agent_debate_stance_in_conf_file("critic", "neutral", conf)
    assert _load(conf)["agents"]["critic"]["debate_stance"] == "neutral"

    with pytest.raises(ValueError):
        set_agent_debate_stance_in_conf_file("critic", "referee", conf)
    with pytest.raises(KeyError):
        set_agent_debate_stance_in_conf_file("nope", "critic", conf)


def test_a_new_agent_can_declare_its_stance(conf: Path):
    add_agent_to_conf_file(
        "data_analyst", "Data Analyst", "Data",
        debate_stance="critic", config_path=conf,
    )
    assert _load(conf)["agents"]["data_analyst"]["debate_stance"] == "critic"

    with pytest.raises(ValueError):
        add_agent_to_conf_file("other", "Other", "Role", debate_stance="referee", config_path=conf)


def test_a_neutral_agent_does_not_clutter_the_file(conf: Path):
    """기본값은 적지 않습니다. 설정 파일은 사람이 읽는 문서이기도 합니다."""
    add_agent_to_conf_file("plain", "Plain", "Role", config_path=conf)

    assert "debate_stance" not in conf.read_text(encoding="utf-8")
    assert _load(conf)["agents"]["plain"].get("debate_stance") is None


# --------------------------------------------------------------- 드래그 삽입 위치


def _reorder(keys, source, target, after):
    """`AgentRosterControl._on_drop_on` 의 자리 계산과 같은 규칙."""
    out = list(keys)
    out.remove(source)
    out.insert(out.index(target) + (1 if after else 0), source)
    return out


ROSTER = ["orchestrator", "architect", "coder", "critic"]


def test_dropping_on_the_right_half_lands_behind_the_target():
    """오른쪽 이웃으로 한 칸 옮기기.

    커서 위치를 보지 않고 늘 대상 '앞' 에 넣던 시절에는 이것이 제자리였습니다.
    빼고 나면 그 이웃이 원래 자리로 당겨지기 때문입니다 — 드래그해도 아무 일도
    일어나지 않는 것처럼 보였습니다.
    """
    assert _reorder(ROSTER, "architect", "coder", after=True) == [
        "orchestrator", "coder", "architect", "critic",
    ]
    # 예전 규칙(after=False)은 제자리였습니다.
    assert _reorder(ROSTER, "architect", "coder", after=False) == ROSTER


def test_the_last_position_is_reachable():
    """마지막 카드 '뒤' 에 놓을 수 없으면 맨 끝으로 보낼 방법이 없습니다."""
    assert _reorder(ROSTER, "architect", "critic", after=True) == [
        "orchestrator", "coder", "critic", "architect",
    ]


def test_dropping_on_the_left_half_lands_in_front_of_the_target():
    assert _reorder(ROSTER, "critic", "architect", after=False) == [
        "orchestrator", "critic", "architect", "coder",
    ]


@pytest.mark.parametrize("source", ["architect", "coder", "critic"])
def test_one_drag_can_put_a_card_in_any_position(source):
    """카드 하나를 한 번 끌어서 첫째·둘째·셋째 어느 자리로든 보낼 수 있어야 합니다.

    (배치 전체를 한 번에 뒤집을 수 있다는 뜻은 아닙니다. 세 개를 완전히 역순으로
    만들려면 두 번 끌어야 하고, 그건 드래그의 한계가 아니라 이동 한 번의 한계입니다.)
    """
    specialists = ["architect", "coder", "critic"]
    landed = set()
    for target in specialists:
        if target == source:
            continue
        for after in (False, True):
            order = [k for k in _reorder(ROSTER, source, target, after) if k != "orchestrator"]
            landed.add(order.index(source))

    assert landed == {0, 1, 2}, f"'{source}' 가 닿지 못하는 자리: {{0, 1, 2}} - {landed}"
