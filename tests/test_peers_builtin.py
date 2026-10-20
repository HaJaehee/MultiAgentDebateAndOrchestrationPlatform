"""내장 도구 peers (동료 전문가 자문) 를 설정과 화면이 어떻게 다루는가.

peers 는 MCP 서버가 아니지만, 켜는 법이 에이전트의 `allowed_mcp_servers` 에 "peers" 를 넣는
것입니다 (app/agents/consultation.py). 여기서 지키려는 것.

1. conf.json 의 mcp_servers 에 없어도 도구 할당에서 늘 고를 수 있고, "설정에 없음" 으로 보이지 않는다.
2. MCP 서버 칸의 peers 칩 설명은 실제 제약과 누가 켰는지를 말한다 (오케스트레이터는 쓰지 않는다).
3. 같은 이름의 MCP 서버는 등록할 수 없다 — 도구 이름과 켜는 스위치가 겹친다.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.consultation import (
    DEFAULT_MAX_CONSULTATION_DEPTH,
    MAX_CONSULTATION_TOOL_ITERATIONS,
    PEERS_TOOL_TOOLTIP,
)
from app.config import (
    PEERS_TOOL_PREFIX,
    RootConfig,
    add_mcp_server_to_conf_file,
    load_config,
    read_conf_file,
    set_agent_allowed_mcp_servers_in_conf_file,
    write_conf_file,
)
from app.ui.components.roster import peers_chip_tooltip, peers_users, unknown_tool_servers

ROOT = Path(__file__).resolve().parents[1]

SAMPLE = {
    "mcp_servers": {"filesystem": {"command": "node", "args": ["server.js"]}},
    "agents": {
        "orchestrator": {"name": "Master Orchestrator", "role": "Moderator"},
        "critic": {"name": "Critic", "role": "Review", "allowed_mcp_servers": ["filesystem"]},
    },
}


@pytest.fixture()
def conf(tmp_path: Path) -> Path:
    path = tmp_path / "conf.json"
    write_conf_file(path, SAMPLE)
    return path


def _agent(key: str, name: str, servers):
    return SimpleNamespace(key=key, name=name, allowed_mcp_servers=list(servers))


# --------------------------------------------------------------- 도구 할당


def test_peers_is_never_reported_as_missing_from_the_config():
    configured = {"filesystem": object()}

    assert unknown_tool_servers(["peers", "filesystem", "gone"], configured) == ["gone"]


def test_peers_can_be_assigned_without_a_conf_entry(conf: Path):
    set_agent_allowed_mcp_servers_in_conf_file("critic", ["filesystem", PEERS_TOOL_PREFIX], conf)

    data = read_conf_file(conf)
    assert data["agents"]["critic"]["allowed_mcp_servers"] == ["filesystem", "peers"]
    assert PEERS_TOOL_PREFIX not in data["mcp_servers"]


# --------------------------------------------------------------- MCP 서버 칸의 설명


def test_chip_tooltip_names_the_specialists_who_switched_it_on():
    agents = [
        _agent("orchestrator", "Master Orchestrator", ["peers"]),  # 켜 두어도 쓰지 않습니다
        _agent("architect", "System Architect", ["peers", "filesystem"]),
        _agent("critic", "Critic", ["filesystem"]),
    ]

    assert peers_users(agents) == ["System Architect"]
    assert peers_chip_tooltip(agents).endswith("이 대화에서 켠 전문가: System Architect")
    assert peers_chip_tooltip(agents[2:]).endswith("이 대화에서 켠 전문가: 없음")


def test_tooltip_states_the_limits_the_code_enforces():
    """설명이 코드와 어긋나면 사람은 설명을 믿습니다 — 수치는 상수에서 가져와야 합니다."""
    assert f"{DEFAULT_MAX_CONSULTATION_DEPTH}단계" in PEERS_TOOL_TOOLTIP
    assert f"{MAX_CONSULTATION_TOOL_ITERATIONS}회" in PEERS_TOOL_TOOLTIP
    assert "오케스트레이터는 쓰지 않습니다" in PEERS_TOOL_TOOLTIP
    assert "MCP 서버가 아닙니다" in PEERS_TOOL_TOOLTIP


# --------------------------------------------------------------- 예약된 이름


def test_an_mcp_server_cannot_be_named_peers(conf: Path):
    with pytest.raises(ValueError, match="동료 자문 도구"):
        add_mcp_server_to_conf_file(PEERS_TOOL_PREFIX, "node", [], {}, True, conf)
    with pytest.raises(ValueError, match="스킬 도구"):
        add_mcp_server_to_conf_file("skills", "node", [], {}, True, conf)


def test_a_config_that_declares_a_peers_server_is_rejected():
    with pytest.raises(ValueError, match="peer consultation"):
        RootConfig.model_validate({
            "agents": {"orchestrator": {"name": "O", "role": "R"}},
            "mcp_servers": {PEERS_TOOL_PREFIX: {"command": "node"}},
        })


# --------------------------------------------------------------- 예시 설정


def test_example_config_no_longer_ships_pair_slide():
    """PairSlide 는 별도 프로젝트라 예시 설정에 싣지 않습니다 (연동 코드는 그대로입니다)."""
    cfg = load_config(ROOT / "conf.example.json")

    assert "pair_slide" not in cfg.mcp_servers
    assert "slide_writer" not in cfg.agents
    text = (ROOT / "conf.example.json").read_text(encoding="utf-8")
    assert "pair_slide" not in text and "PairSlide" not in text
