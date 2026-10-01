"""에이전트 카드 색과 아이콘: 고르기 → conf.json → 화면 → 대화 스냅샷.

여기서 지키려는 것은 세 가지입니다.

1. 사람이 정한 색·아이콘이 conf.json 에 적히고 다시 읽힌다.
2. 아이콘 그림을 못 찾아도 화면이 깨지지 않는다 (기본 아이콘으로 물러선다).
3. 대화를 잠그면 그때의 겉모습이 함께 고정되어, 나중에 conf.json 을 바꿔도
   지난 기록의 카드는 그대로다.
"""

import json
import shutil

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.base import Agent, style_for_agent
from app.agents.personas import (
    agent_from_snapshot,
    config_snapshot_of,
    effective_personas,
    freeze_personas,
    frozen_agents,
    save_persona,
)
from app.agents.pool import AgentPool
from app.config import (
    AgentConfig,
    add_agent_to_conf_file,
    load_config,
    read_conf_file,
    resolve_agent_icon,
    store_agent_icon,
    update_agent_appearance_in_conf_file,
)
from app.database.models import Base, SessionAgentModel, SessionModel

# 최소한의 PNG 머리. 내용은 읽지 않고 확장자와 크기만 봅니다.
PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


@pytest.fixture
def conf_path(tmp_path):
    """conf.example.json 을 임시 폴더에 복사한 편집용 설정 파일."""
    path = tmp_path / "conf.json"
    shutil.copy("conf.example.json", path)
    return path


@pytest.fixture
def stored_icon():
    """`data/agent_icons/` 에 실제로 복사된 아이콘. 테스트가 끝나면 지웁니다."""
    rel = store_agent_icon("appearance_test", "logo.PNG", PNG_BYTES)
    yield rel
    path = resolve_agent_icon(rel)
    if path is not None:
        path.unlink()


@pytest_asyncio.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _pool(**overrides) -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(name="Master", role="Moderator"),
        "critic": AgentConfig(name="Critic", role="Reviewer", **overrides),
    })


async def _new_session(factory) -> SessionModel:
    async with factory() as db:
        s = SessionModel(title="겉모습 테스트", active_agents=["orchestrator", "critic"])
        db.add(s)
        await db.commit()
        return s


# --------------------------------------------------------------- 파일 저장


def test_uploaded_icon_lands_in_the_data_folder(stored_icon):
    """올린 그림은 루트의 data/agent_icons/ 로 복사되고, 상대 경로가 돌아온다."""
    assert stored_icon.startswith("data/agent_icons/")
    path = resolve_agent_icon(stored_icon)
    assert path is not None and path.is_file()
    assert path.read_bytes() == PNG_BYTES


def test_same_image_twice_is_one_file(stored_icon):
    """이름을 내용 해시로 짓기 때문에 같은 그림은 같은 파일이 된다."""
    assert store_agent_icon("appearance_test", "other-name.png", PNG_BYTES) == stored_icon


@pytest.mark.parametrize("name", ["evil.exe", "script.js", "noext"])
def test_only_image_files_are_accepted(name):
    with pytest.raises(ValueError):
        store_agent_icon("x", name, PNG_BYTES)


def test_oversized_icon_is_rejected():
    from app.config import MAX_ICON_BYTES

    with pytest.raises(ValueError):
        store_agent_icon("x", "big.png", b"\x00" * (MAX_ICON_BYTES + 1))


@pytest.mark.parametrize(
    "value",
    ["", None, "../../../etc/passwd", "data/agent_icons/does-not-exist.png", "psychology"],
)
def test_unusable_icon_paths_resolve_to_nothing(value):
    """폴백 신호. 이 값들은 전부 '그림 없음' 으로 읽혀야 한다."""
    assert resolve_agent_icon(value) is None


# --------------------------------------------------------------- conf.json


def test_new_agent_keeps_its_colour_and_icon(conf_path, stored_icon):
    add_agent_to_conf_file(
        "analyst", "Data Analyst", "Metrics",
        card_color="#ff8f00", icon=stored_icon, config_path=conf_path,
    )
    block = read_conf_file(conf_path)["agents"]["analyst"]
    assert block["card_color"] == "#ff8f00"
    assert block["icon"] == stored_icon

    agent = load_config(conf_path).agents["analyst"]
    assert agent.card_color == "#ff8f00"
    assert agent.icon == stored_icon


def test_appearance_can_be_changed_and_cleared(conf_path):
    update_agent_appearance_in_conf_file(
        "critic", "#009688", "science", config_path=conf_path
    )
    assert load_config(conf_path).agents["critic"].card_color == "#009688"

    update_agent_appearance_in_conf_file("critic", "", "", config_path=conf_path)
    block = read_conf_file(conf_path)["agents"]["critic"]
    assert "card_color" not in block and "icon" not in block
    assert load_config(conf_path).agents["critic"].card_color is None


def test_clearing_also_drops_the_old_spelling(conf_path):
    """`color` / `avatar` 로 적혀 있던 값도 함께 걷힌다.

    남겨 두면 화면에서 지웠는데도 그 값이 계속 읽혀, 지워지지 않는 색이 됩니다.
    """
    data = read_conf_file(conf_path)
    data["agents"]["critic"].pop("card_color", None)
    data["agents"]["critic"].pop("icon", None)
    data["agents"]["critic"]["color"] = "teal-8"
    data["agents"]["critic"]["avatar"] = "science"
    conf_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    assert load_config(conf_path).agents["critic"].card_color == "teal-8"

    update_agent_appearance_in_conf_file("critic", "", "", config_path=conf_path)
    assert load_config(conf_path).agents["critic"].card_color is None
    assert load_config(conf_path).agents["critic"].icon is None


def test_unknown_agent_is_not_created_by_appearance_alone(conf_path):
    with pytest.raises(KeyError):
        update_agent_appearance_in_conf_file("ghost", "#ffffff", "", config_path=conf_path)


# --------------------------------------------------------------- 화면용 값


def test_key_decides_the_style_when_nothing_is_chosen():
    """예전 규칙 그대로. 표에 있는 키는 표에서, 나머지는 키의 crc32 로."""
    assert style_for_agent("orchestrator")["avatar"] == "forum"
    assert style_for_agent("brand_new") == style_for_agent("brand_new")
    assert style_for_agent("brand_new")["color"] != "primary"


def test_chosen_colour_and_icon_win():
    style = style_for_agent("coder", "#ff0000", "psychology")
    assert style["avatar"] == "psychology"
    assert style["color"] == "#ff0000"
    # 테두리처럼 CSS 로 칠하는 자리에는 실제 색이 필요합니다.
    assert style["badge_color"] == "#ff0000"


def test_quasar_colour_names_still_resolve_to_a_real_colour():
    assert style_for_agent("coder", "teal-8")["badge_color"] == "#009688"


def test_uploaded_image_becomes_an_avatar_image(stored_icon):
    style = style_for_agent("coder", None, stored_icon)
    assert style["avatar"].startswith("img:/agent-icon?src=")


def test_missing_image_falls_back_to_the_key_icon():
    """그림을 못 찾으면 원래 아이콘으로 물러선다 — 빈 아바타를 남기지 않는다."""
    style = style_for_agent("coder", None, "data/agent_icons/deleted.png")
    assert style["avatar"] == style_for_agent("coder")["avatar"] == "code"


def test_icon_outside_the_project_is_refused(tmp_path):
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG_BYTES)
    style = style_for_agent("coder", None, str(outside))
    assert style["avatar"] == "code"


def test_has_custom_appearance_flags_only_explicit_choices():
    plain = Agent.from_config("critic", AgentConfig(name="C", role="R"))
    painted = Agent.from_config("critic", AgentConfig(name="C", role="R", card_color="#123456"))
    assert not plain.has_custom_appearance
    assert painted.has_custom_appearance


# --------------------------------------------------------------- 대화 스냅샷


def test_snapshot_carries_the_appearance(stored_icon):
    agent = Agent.from_config(
        "critic", AgentConfig(name="C", role="R", card_color="#c2185b", icon=stored_icon)
    )
    snapshot = config_snapshot_of(agent)
    assert snapshot["card_color"] == "#c2185b"
    assert snapshot["icon"] == stored_icon

    restored = agent_from_snapshot("critic", snapshot)
    assert restored is not None
    assert (restored.avatar, restored.color) == (agent.avatar, agent.color)


@pytest.mark.asyncio
async def test_appearance_is_saved_with_the_persona(db_factory):
    session = await _new_session(db_factory)
    async with db_factory() as db:
        await save_persona(
            db, session, "critic", "Critic", "Reviewer", "검토하세요.",
            card_color="#689f38", icon="science",
        )
        row = (await db.execute(
            select(SessionAgentModel).where(SessionAgentModel.agent_key == "critic")
        )).scalar_one()
        assert (row.card_color, row.icon_path) == ("#689f38", "science")

        personas = await effective_personas(db, session.id, _pool())
    assert personas["critic"].card_color == "#689f38"
    assert personas["critic"].icon == "science"
    # 색만 바꾼 것도 "기본값과 다름" 입니다. 그렇지 않으면 화면이 바뀐 사실을
    # 알려 주지 못합니다.
    assert personas["critic"].is_customized


@pytest.mark.asyncio
async def test_freezing_pins_the_appearance_of_untouched_agents(db_factory):
    """손대지 않은 에이전트도 잠글 때의 conf.json 겉모습으로 고정된다."""
    session = await _new_session(db_factory)
    async with db_factory() as db:
        await freeze_personas(db, session, _pool(card_color="#0097a7", icon="insights"))
        row = (await db.execute(
            select(SessionAgentModel).where(SessionAgentModel.agent_key == "critic")
        )).scalar_one()
    assert (row.card_color, row.icon_path) == ("#0097a7", "insights")
    assert row.config_snapshot["card_color"] == "#0097a7"


@pytest.mark.asyncio
async def test_locked_conversation_keeps_its_colours(db_factory):
    """잠근 뒤 conf.json 의 색을 바꿔도 그 대화의 카드는 그대로다."""
    session = await _new_session(db_factory)
    async with db_factory() as db:
        await freeze_personas(db, session, _pool(card_color="#0097a7"))

    later = _pool(card_color="#e64a19")  # conf.json 을 나중에 고친 상황
    async with db_factory() as db:
        agents = {a.key: a for a in await frozen_agents(db, session.id, later)}
    assert agents["critic"].card_color == "#0097a7"
    assert agents["critic"].badge_color == "#0097a7"


# --------------------------------------------------------------- 채팅 피드


def _feed():
    """UI 를 짓지 않고 스타일 판정만 보는 최소 인스턴스."""
    from app.ui.components.chat_feed import ChatFeed

    feed = ChatFeed.__new__(ChatFeed)
    feed._agent_styles = {}
    return feed


def test_chat_falls_back_to_the_key_when_the_session_is_unknown():
    assert _feed()._style_for("orchestrator")["avatar"] == "forum"
    assert _feed()._style_for("orchestrator")["custom"] is False


def test_chat_uses_the_session_agents_appearance():
    """발언 카드는 이 대화의 에이전트에서 색을 가져온다 (conf.json 이 아니라)."""
    feed = _feed()
    feed.set_agent_styles([
        Agent.from_config("critic", AgentConfig(name="C", role="R", card_color="#c2185b")),
        Agent.from_config("coder", AgentConfig(name="K", role="R")),
    ])
    assert feed._style_for("critic")["badge_color"] == "#c2185b"
    assert feed._style_for("critic")["custom"] is True
    # 색을 정하지 않은 에이전트는 테두리를 칠하지 않습니다 — 예전 모습 그대로.
    assert feed._style_for("coder")["custom"] is False
