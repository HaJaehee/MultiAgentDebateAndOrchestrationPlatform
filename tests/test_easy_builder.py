"""대화로 에이전트 만들기 (app/easy/builder.py).

지키려는 것:

1. 도우미 답의 설계도 블록을 떼어 읽고, 사람에게 보일 글에서는 지운다. 깨진 블록은 무시한다.
2. 없는 도구·스킬, 팔레트 밖의 색·아이콘은 걸러지고, 쓸 수 없는 키는 쓸 수 있는 키로 바뀐다.
3. 주인이 저장하면 conf.json 에 블록이 더해지고 (`//` 설명은 그대로), 다시 읽어도 검증을 통과하며,
   돌고 있는 풀에 바로 들어간다. 모델·키는 적지 않아 llm 을 물려받는다.
4. 진행 중인 대화가 있으면 conf.json 을 건드리지 않고 거절한다.
5. 방문자가 저장하면 conf.json 은 한 바이트도 바뀌지 않고, 도구는 방문자 허용분만 남는다.
6. 도우미는 오케스트레이터의 연결로 돌되 도구·스킬·단계적 사고가 없다.
"""

from pathlib import Path

import pytest

from app import config as config_module
from app.agents import pool as pool_module
from app.config import AgentConfig, get_config, load_config, read_conf_file, write_conf_file
from app.database.session import get_session_factory, init_db
from app.easy import models as easy_models  # noqa: F401 - 테이블 등록
from app.easy.builder import (
    AgentDraft,
    SaveRefused,
    agent_key_for,
    ask_builder,
    builder_prompt,
    conf_block,
    merge_draft,
    require_complete,
    sanitize_draft,
    save_guest_agent,
    save_owner_agent,
    split_reply,
    streaming_text,
    with_draft,
)
from app.easy.catalog import MAX_GUEST_AGENTS, Option
from app.agents.pool import AgentPool
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"

SAMPLE = {
    "llm": {"model": "${LLM_MODEL:-openai/gpt-4o}", "api_key": "${LLM_API_KEY:-sk-test}", "temperature": 0.4},
    "mcp_servers": {
        "// filesystem": "파일 서버",
        "filesystem": {"command": "node", "args": ["server.js"]},
    },
    "agents": {
        "// orchestrator": "1. 필수 오케스트레이터",
        "orchestrator": {"name": "Master Orchestrator", "role": "Moderator", "allowed_mcp_servers": ["filesystem"]},
    },
}

DRAFT = AgentDraft(
    key="meeting_secretary",
    name="회의록 비서",
    role="회의 메모에서 결정과 할 일을 추립니다",
    system_prompt="당신은 회의록 비서입니다.\n1. 메모를 읽습니다.\n2. 결정과 할 일을 표로 씁니다.",
    allowed_mcp_servers=["filesystem"],
    card_color="#009688",
    icon="fact_check",
)


@pytest.fixture()
def live_conf(tmp_path: Path):
    """임시 conf.json 을 지금 앱이 쓰는 설정으로 둡니다. 끝나면 원래 전역 설정과 풀로 되돌립니다."""
    path = tmp_path / "conf.json"
    write_conf_file(path, SAMPLE)
    saved = (config_module._config, config_module._config_path, pool_module._agent_pool)  # noqa: SLF001
    get_config(reload=True, config_path=path)
    pool_module._agent_pool = None  # noqa: SLF001
    yield path
    config_module._config, config_module._config_path, pool_module._agent_pool = saved  # noqa: SLF001


# ------------------------------------------------------------------ 1. 설계도 블록


def test_the_blueprint_block_is_read_and_removed_from_the_visible_text():
    content = (
        "회의 메모를 다루시니 '파일 열어 보기' 도구를 드립니다.\n\n"
        "```agent\n"
        '{"name": "회의록 비서", "system_prompt": ["당신은 {고객명} 을 부릅니다.", "둘째 줄"], '
        '"allowed_mcp_servers": ["filesystem"]}\n'
        "```"
    )
    visible, data = split_reply(content)
    assert visible == "회의 메모를 다루시니 '파일 열어 보기' 도구를 드립니다."
    assert data["name"] == "회의록 비서"
    assert merge_draft(AgentDraft(), data).system_prompt == "당신은 {고객명} 을 부릅니다.\n둘째 줄"


def test_a_broken_block_is_ignored_and_a_missing_block_means_no_change():
    visible, data = split_reply("설명입니다.\n```agent\n{\"name\": 회의록}\n```")
    assert visible == "설명입니다." and data is None
    visible, data = split_reply("질문이 두 가지 있습니다.")
    assert visible == "질문이 두 가지 있습니다." and data is None
    assert merge_draft(DRAFT, None) == DRAFT


def test_the_block_is_hidden_while_it_streams():
    assert streaming_text("알겠습니다.\n\n```agent\n{\"name\": \"회") == "알겠습니다."
    assert streaming_text("아직 블록 전") == "아직 블록 전"


def test_unreadable_fields_keep_the_current_value():
    merged = merge_draft(DRAFT, {"allowed_mcp_servers": "filesystem", "unknown": 1, "name": None})
    assert merged.allowed_mcp_servers == ["filesystem"]
    assert merged.name == ""  # 비운 칸은 비운 것입니다


# ------------------------------------------------------------------ 2. 거르기와 키


def test_unknown_tools_skills_colours_and_icons_are_dropped():
    draft = DRAFT.model_copy(update={
        "allowed_mcp_servers": ["filesystem", "sandbox", "filesystem"],
        "allowed_skills": ["csv-profile", "made-up"],
        "card_color": "#123456",
        "icon": "not_an_icon",
    })
    clean = sanitize_draft(draft, ["filesystem"], ["csv-profile"])
    assert clean.allowed_mcp_servers == ["filesystem"]
    assert clean.allowed_skills == ["csv-profile"]
    assert clean.card_color == "" and clean.icon == ""
    assert sanitize_draft(DRAFT, ["filesystem"], []).card_color == "#009688"


def test_keys_are_made_usable_and_unique():
    assert agent_key_for(AgentDraft(key="Meeting-Bot"), []) == "meeting_bot"
    assert agent_key_for(AgentDraft(key="회의록"), []) == "agent"
    assert agent_key_for(AgentDraft(key="agent"), ["agent", "agent_2"]) == "agent_3"
    assert agent_key_for(AgentDraft(key="orchestrator"), []) == "orchestrator_2"


def test_an_incomplete_blueprint_cannot_be_saved():
    with pytest.raises(SaveRefused, match="업무 지시서"):
        require_complete(DRAFT.model_copy(update={"system_prompt": ""}))


def test_the_prompt_lists_only_the_given_tools_and_warns_guests():
    servers = [Option("filesystem", "파일 열어 보기·만들기", "작업 폴더의 문서")]
    owner = builder_prompt(servers, [], guest=False)
    guest = builder_prompt(servers, [], guest=True)
    assert "`filesystem`" in owner and "sandbox" not in owner
    assert "읽기 전용" in guest and "읽기 전용" not in owner
    assert "```agent" in owner and "{tools}" not in owner


def test_the_card_travels_with_the_users_words_once_it_has_content():
    assert with_draft("안녕하세요", AgentDraft()) == "안녕하세요"
    assert "[지금 설계도]" in with_draft("이름을 바꿨어요", DRAFT)


# ------------------------------------------------------------------ 3·4. 주인 저장


def test_an_owner_save_adds_the_agent_to_conf_json_and_the_live_pool(live_conf: Path):
    key = save_owner_agent(DRAFT)
    assert key == "meeting_secretary"

    raw = read_conf_file(live_conf)
    assert raw["agents"]["// orchestrator"] == "1. 필수 오케스트레이터", "설명 키가 살아남아야 합니다"
    assert raw["mcp_servers"]["// filesystem"] == "파일 서버"
    block = raw["agents"][key]
    assert block == conf_block(DRAFT), "화면의 미리보기와 실제로 적힌 것이 같아야 합니다"
    assert "model" not in block and "api_key" not in block

    reread = load_config(live_conf).agents[key]
    assert reread.model == "openai/gpt-4o" and reread.temperature == 0.4, "llm 을 물려받습니다"
    assert pool_module.get_agent_pool().get(key) is not None, "재시작 없이 풀에 들어갑니다"

    assert save_owner_agent(DRAFT) == "meeting_secretary_2", "같은 키는 번호를 붙여 따로 둡니다"


def test_an_owner_save_is_refused_while_any_debate_runs(live_conf: Path):
    before = live_conf.read_bytes()
    with pytest.raises(SaveRefused, match="진행 중인 대화"):
        save_owner_agent(DRAFT, running=["some-session"])
    assert live_conf.read_bytes() == before


# ------------------------------------------------------------------ 5. 방문자 저장


@pytest.mark.asyncio
async def test_a_guest_save_stays_in_the_database_and_keeps_only_guest_tools(live_conf: Path):
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    before = live_conf.read_bytes()
    draft = DRAFT.model_copy(update={"allowed_mcp_servers": ["filesystem", "sandbox"]})
    async with factory() as db:
        row = await save_guest_agent(db, "guest-1", draft)
    assert row.allowed_mcp_servers == ["filesystem"]
    assert live_conf.read_bytes() == before, "방문자는 conf.json 을 바꾸지 않습니다"
    assert "meeting_secretary" not in get_config().agents


@pytest.mark.asyncio
async def test_a_guest_can_keep_only_so_many_agents():
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    async with factory() as db:
        for _ in range(MAX_GUEST_AGENTS):
            await save_guest_agent(db, "guest-cap", DRAFT)
        with pytest.raises(SaveRefused, match=str(MAX_GUEST_AGENTS)):
            await save_guest_agent(db, "guest-cap", DRAFT)


# ------------------------------------------------------------------ 6. 도우미


class RecordingLLM(FakeLLMCaller):
    def __init__(self):
        super().__init__(replies={"orchestrator": "무엇을 맡기실까요?\n```agent\n{\"name\": \"비서\"}\n```"})
        self.agents = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.agents.append(agent)
        return await super().call_agent(agent, messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_the_builder_borrows_the_orchestrator_connection_without_any_tools():
    pool = AgentPool({"orchestrator": AgentConfig(
        name="Master Orchestrator", role="Moderator", model="openai/gpt-4o-mini", api_key="sk-test",
        allowed_mcp_servers=["filesystem"], allowed_skills=["mermaid-diagrams"],
        sequential_thinking={"enabled": True},
    )})
    llm = RecordingLLM()
    chunks = []
    content = await ask_builder(
        [{"role": "user", "content": "회의록 정리"}], servers=[], skills=[], guest=False,
        on_chunk=chunks.append, llm=llm, pool=pool,
    )
    agent = llm.agents[0]
    assert agent.model == "openai/gpt-4o-mini"
    assert agent.allowed_mcp_servers == [] and agent.allowed_skills == []
    assert agent.sequential_thinking.enabled is False
    assert "에이전트 설계 도우미" in agent.system_prompt
    assert split_reply(content)[1] == {"name": "비서"}
    assert "".join(chunks) == content
