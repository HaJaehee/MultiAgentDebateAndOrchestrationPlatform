"""로스터의 conf.json 조작과 잠금 규칙 — 화면 없이.

`RosterService` 는 NiceGUI 를 모릅니다. 그래서 `ui.notify` 를 바꿔 끼우지 않고, 임시 conf.json 을 놓고 돌려 준
`Notice` 와 파일 내용만 봅니다. 화면이 그 결과를 어떻게 쓰는지는 `test_roster_lock.py`·`test_agent_admin.py` 가 봅니다.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from app.agents import pool as pool_module
from app.config import get_config, read_conf_file
from app.orchestration import roster_service
from app.orchestration.roster_service import Notice, RosterService, agent_admin_lock_reason

ROOT = Path(__file__).resolve().parents[1]

SAMPLE = {
    "llm": {"model": "openai/gpt-4o", "api_key": "sk-test"},
    "mcp_servers": {
        "// filesystem": "파일 서버",
        "filesystem": {"command": "node", "args": ["server.js"]},
        "git": {"command": "python", "args": ["-m", "git"], "enabled": False},
    },
    "agents": {
        "// orchestrator": "1. 필수 오케스트레이터",
        "orchestrator": {"name": "Master", "role": "Moderator"},
        "critic": {"name": "Critic", "role": "Review", "debate_priority": 20},
    },
}


class FakeRunner:
    def __init__(self):
        self.running: List[str] = []

    def running_sessions(self):
        return list(self.running)

    def is_running(self, session_id):
        return session_id in self.running


class FakeRuntimes:
    """`reload_all` 만 세는 MCP 풀."""

    def __init__(self):
        self.reloads = 0
        self.fail = False

    def get(self, workspace=None):
        return None

    async def reload_all(self):
        self.reloads += 1
        if self.fail:
            raise RuntimeError("서버를 띄우지 못했습니다")
        return {}


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "conf.json"
    path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    get_config(reload=True, config_path=path)
    monkeypatch.setattr(pool_module, "_agent_pool", None)
    yield path
    # 다른 시험이 이 임시 파일을 읽지 않도록 전역 설정을 비웁니다.
    from app.config import loader

    monkeypatch.setattr(loader, "_config", None)


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(roster_service, "get_debate_runner", lambda: fake)
    return fake


@pytest.fixture
def runtimes(monkeypatch):
    fake = FakeRuntimes()
    monkeypatch.setattr(roster_service, "get_runtime_pool", lambda: fake)
    return fake


# ------------------------------------------------------------------ 화면을 모른다


@pytest.mark.parametrize("module", [
    "app.orchestration.roster_service", "app.ui.components.roster.state", "app.agents.draft", "app.agents.catalog",
])
def test_loading_it_does_not_load_the_screen_library(module):
    """이 시험 프로세스에는 이미 NiceGUI 가 올라와 있으므로 새 프로세스에서 봅니다."""
    code = f"import sys, importlib; importlib.import_module({module!r}); sys.exit('nicegui' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


# ------------------------------------------------------------------ 잠금 규칙


def test_nothing_running_means_no_reason(runner):
    assert RosterService().running_debate_lock_reason() == ""


def test_a_running_debate_is_the_reason_and_counts_the_conversations(runner):
    runner.running = ["a", "b"]
    assert "2개" in RosterService().running_debate_lock_reason()


def test_admin_lock_prefers_the_running_debate_over_the_started_conversation():
    assert agent_admin_lock_reason("", False) == ""
    assert "고정" in agent_admin_lock_reason("", True)
    assert agent_admin_lock_reason("토론 중입니다", True) == "토론 중입니다"


# ------------------------------------------------------------------ 이름 있는 쓰기


def test_each_write_reaches_the_active_conf_file(conf):
    service = RosterService()
    assert service.conf_path() == str(conf)

    service.set_agent_enabled("critic", False)
    service.set_debate_stance("critic", "critic")
    service.set_debate_order(["critic", "orchestrator"])
    service.set_agent_tools("critic", ["filesystem"])
    service.set_agent_skills("critic", ["alpha"])
    service.set_agent_peer_consultation("critic", ["orchestrator"], 2)
    service.add_agent("data_analyst", "분석가", "지표", system_prompt="당신은 분석가입니다.", allowed_mcp_servers=["git"])
    service.set_mcp_enabled("git", True)
    service.add_local_mcp_server("everything", "npx", ["-y", "x"], {"A": "1"}, True)
    service.add_remote_mcp_server("remote", "https://x/mcp", {"H": "v"}, "auto", False)
    service.set_skill_enabled("alpha", False)

    data = read_conf_file(conf)
    critic = data["agents"]["critic"]
    assert critic["enabled"] is False and critic["debate_stance"] == "critic"
    assert critic["debate_priority"] == 10 and data["agents"]["orchestrator"]["debate_priority"] == 20
    assert critic["allowed_mcp_servers"] == ["filesystem"] and critic["allowed_skills"] == ["alpha"]
    assert critic["peer_consultation"]["targets"] == ["orchestrator"]
    assert data["agents"]["data_analyst"]["name"] == "분석가"
    assert data["mcp_servers"]["git"]["enabled"] is True
    assert data["mcp_servers"]["everything"]["command"] == "npx"
    assert data["mcp_servers"]["remote"]["url"] == "https://x/mcp" and data["mcp_servers"]["remote"]["enabled"] is False
    assert "alpha" in data["skills"]["disabled"]

    service.remove_agent("data_analyst")
    service.remove_mcp_server("everything")
    data = read_conf_file(conf)
    assert "data_analyst" not in data["agents"] and "everything" not in data["mcp_servers"]


def test_a_failed_write_comes_back_as_a_notice_not_an_exception():
    def broken():
        raise ValueError("JSON 문법 오류")

    notice = RosterService.write_conf(broken)
    assert notice == Notice("conf.json 을 고치지 못했습니다: JSON 문법 오류", "negative")
    assert RosterService.write_conf(lambda: None) is None


# ------------------------------------------------------------------ 돌고 있는 앱에 반영


@pytest.mark.asyncio
async def test_applying_restarts_servers_only_when_asked(conf, runtimes):
    notice = await RosterService.apply_to_running_app("저장했습니다.", restart_servers=False)
    assert notice == Notice("저장했습니다.", "positive")
    assert runtimes.reloads == 0

    await RosterService.apply_to_running_app("저장했습니다.", restart_servers=True)
    assert runtimes.reloads == 1


@pytest.mark.asyncio
async def test_a_failed_apply_says_the_file_was_still_saved(conf, runtimes):
    runtimes.fail = True
    notice = await RosterService.apply_to_running_app("저장했습니다.")
    assert notice.level == "negative" and "conf.json 은 저장했지만" in notice.message


@pytest.mark.asyncio
async def test_applying_refills_the_pool_in_place(conf, runtimes):
    """엔진과 화면이 풀을 붙잡고 있으므로 새 객체로 갈아 끼우지 않습니다."""
    service = RosterService()
    pool = service.agent_pool()
    service.set_agent_enabled("critic", False)

    await service.apply_to_running_app("끝", restart_servers=False)

    assert service.agent_pool() is pool
    assert "critic" not in {a.key for a in pool.list_all()}


# ------------------------------------------------------------------ conf.json 다시 읽기


def _rewrite(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_broken_file_changes_nothing(conf, runtimes):
    service = RosterService()
    pool = service.agent_pool()
    conf.write_text("{ 깨진 파일", encoding="utf-8")

    result = await service.reload_conf(pool)

    assert result.pool is None
    assert [n.level for n in result.notices] == ["negative"] and result.notices[0].multi_line
    assert "실행 중인 설정은 그대로입니다" in result.notices[0].message
    assert runtimes.reloads == 0


@pytest.mark.asyncio
async def test_the_notice_says_who_came_and_went(conf, runtimes):
    service = RosterService()
    pool = service.agent_pool()

    def change(data):
        del data["agents"]["critic"]
        data["agents"]["researcher"] = {"name": "Researcher", "role": "Research"}

    _rewrite(conf, change)
    result = await service.reload_conf(pool)

    assert result.pool is pool
    assert runtimes.reloads == 0, "MCP 구성이 그대로면 서버를 다시 띄우지 않습니다"
    assert len(result.notices) == 1
    assert "추가: researcher" in result.notices[0].message and "제거: critic" in result.notices[0].message


@pytest.mark.asyncio
async def test_a_changed_server_config_restarts_the_servers_first(conf, runtimes):
    service = RosterService()
    _rewrite(conf, lambda data: data["mcp_servers"]["filesystem"].update(args=["other.js"]))

    result = await service.reload_conf(service.agent_pool())

    assert runtimes.reloads == 1
    assert [n.level for n in result.notices] == ["info", "positive"]
    assert "목록 변경 없음" in result.notices[1].message


@pytest.mark.asyncio
async def test_a_failed_restart_is_reported_but_the_agents_are_still_updated(conf, runtimes):
    service = RosterService()
    runtimes.fail = True
    _rewrite(conf, lambda data: data["mcp_servers"]["filesystem"].update(args=["other.js"]))

    result = await service.reload_conf(service.agent_pool())

    assert result.pool is not None
    assert [n.level for n in result.notices] == ["negative", "positive"]


# ------------------------------------------------------------------ 읽기는 화면을 죽이지 않는다


def _broken_config(*_a, **_k):
    raise ValueError("읽지 못합니다")


def test_reads_fall_back_when_the_config_cannot_be_read(monkeypatch, runtimes):
    service = RosterService()
    monkeypatch.setattr(roster_service, "get_config", _broken_config)

    assert service.configured_agents() == {}
    assert service.default_tool_mode() == "default"
    assert service.mcp_view("") == ({}, {})
    assert service.mcp_fingerprint() == {}


def test_a_broken_runtime_is_skipped_by_the_periodic_check(monkeypatch):
    class Broken:
        def get(self, workspace=None):
            raise RuntimeError("풀이 없습니다")

    monkeypatch.setattr(roster_service, "get_runtime_pool", lambda: Broken())
    assert RosterService().runtime_status("") is None


def test_broken_skill_folder_is_an_empty_list(monkeypatch):
    def broken():
        raise OSError("읽지 못합니다")

    monkeypatch.setattr(roster_service, "scan_skills", broken)
    assert RosterService.skills_view() == ([], None)
