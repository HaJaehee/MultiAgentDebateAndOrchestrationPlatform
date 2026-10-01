"""세션 이어받기 — 컨텍스트만 비우고 나머지는 물려받는다.

라운드가 쌓여 컨텍스트가 가득 차면 에이전트들이 헛돌기 시작합니다. 그때
할 수 있는 것은 새 대화를 여는 것뿐이었는데, 그러면 작업 공간을 다시 지정해야
하고 지식 그래프는 통째로 사라졌습니다.

`memory` 서버는 대화마다 파일 하나(`<GRAPH_DIR>/<대화 id>.jsonl`)를 쓰고, 어느
그래프를 열지는 호스트가 `_meta.conversationId` 로 정합니다. 서버는 메타를
인자보다 **우선**하므로(모델이 `graph_id` 에 아무 값이나 적어 남의 그래프를
여는 것을 막으려는 설계), "이전 대화의 그래프를 참고해라" 는 지시는 닿지
않습니다. 경계를 정하는 것이 호스트이니 옮기는 것도 호스트가 해야 합니다.

여기서 지키려는 것.

1. 작업 공간·전략·에이전트 구성·이전 결론이 따라온다.
2. 지식 그래프 파일이 새 대화의 것으로 복사된다 (원본은 그대로).
3. 발언 기록은 따라오지 않는다 — 그게 비우려던 것이다.
4. 못 옮겼으면 쪽지가 그렇게 말한다. 조용히 빈손으로 시작하지 않는다.
"""

import uuid
from pathlib import Path

import pytest

from app.database.models import (
    ArtifactModel,
    MessageModel,
    SessionAgentModel,
    SessionModel,
)
from app.database.session import get_session_factory, init_db
from app.mcp.manager import carry_over_memory_graph
from app.session_ops import (
    HANDOFF_TITLE_SUFFIX,
    build_handoff_note,
    continue_session,
)

DB_URL = "sqlite+aiosqlite:///:memory:"

SYNTHESIS = "## 최종 합의\n\nRedis 를 세션 캐시로 채택합니다. TTL 은 30분."


async def _seed_session(workspace: str = "") -> str:
    """토론을 한 바퀴 돈 대화 하나."""
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    sid = f"src-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="이커머스 아키텍처 토론",
            strategy="adversarial_debate", max_rounds=4, parallel_limit=2,
            active_agents=["orchestrator", "architect", "critic"],
            known_agents=["orchestrator", "architect", "coder", "critic"],
            custom_instructions="Pydantic v2 기준으로 작성",
            workspace_dir=workspace,
            personas_locked=True,
        ))
        db.add(SessionAgentModel(
            id=str(uuid.uuid4()), session_id=sid, agent_key="architect",
            name="고정된 아키텍트", role="Design", system_prompt="너는 설계를 맡는다",
            config_snapshot={"name": "고정된 아키텍트", "model": "fake/frozen"},
        ))
        db.add(MessageModel(
            id=str(uuid.uuid4()), session_id=sid, sender_key="user",
            sender_name="User", sender_role="Client", content="캐시를 설계하라",
            round_number=0, msg_type="user",
        ))
        db.add(MessageModel(
            id=str(uuid.uuid4()), session_id=sid, sender_key="orchestrator",
            sender_name="Master Orchestrator", sender_role="Moderator",
            content=SYNTHESIS, round_number=2, msg_type="orchestrator",
        ))
        db.add(ArtifactModel(
            id=str(uuid.uuid4()), session_id=sid, artifact_type="mermaid",
            title="시스템 아키텍처 다이어그램", content="graph TD\n A-->B", language="mermaid",
        ))
        await db.commit()
    return sid


# ------------------------------------------------------- 1. 무엇이 따라오는가


@pytest.mark.asyncio
async def test_settings_and_agents_are_inherited():
    sid = await _seed_session(workspace="")
    factory = get_session_factory(DB_URL)

    async with factory() as db:
        result = await continue_session(db, sid)
    assert result is not None

    async with factory() as db:
        new = await db.get(SessionModel, result["session_id"])
        rows = (await db.execute(
            SessionAgentModel.__table__.select().where(
                SessionAgentModel.session_id == result["session_id"]
            )
        )).fetchall()

    assert new.strategy == "adversarial_debate"
    assert new.max_rounds == 4
    assert new.parallel_limit == 2
    assert new.active_agents == ["orchestrator", "architect", "critic"]
    assert new.known_agents == ["orchestrator", "architect", "coder", "critic"]
    assert new.custom_instructions == "Pydantic v2 기준으로 작성"
    assert new.title.endswith(HANDOFF_TITLE_SUFFIX)

    # conf.json 에서 사라진 에이전트도 계속 발언할 수 있어야 합니다.
    assert len(rows) == 1
    assert rows[0].name == "고정된 아키텍트"
    assert rows[0].config_snapshot == {"name": "고정된 아키텍트", "model": "fake/frozen"}


@pytest.mark.asyncio
async def test_the_new_session_is_not_locked():
    """아직 시작하지 않은 대화입니다. 컨텍스트가 터진 원인이 모델 선택일 수도 있습니다."""
    sid = await _seed_session()
    factory = get_session_factory(DB_URL)
    async with factory() as db:
        result = await continue_session(db, sid)
    async with factory() as db:
        assert (await db.get(SessionModel, result["session_id"])).personas_locked is False


@pytest.mark.asyncio
async def test_transcript_does_not_follow_but_the_conclusion_does():
    """비우려던 것은 발언 기록입니다. 결론은 쪽지 한 장으로 옮겨집니다."""
    sid = await _seed_session()
    factory = get_session_factory(DB_URL)

    async with factory() as db:
        result = await continue_session(db, sid)

    async with factory() as db:
        rows = (await db.execute(
            MessageModel.__table__.select()
            .where(MessageModel.session_id == result["session_id"])
        )).fetchall()

    assert len(rows) == 1, "인수인계 쪽지 한 장뿐이어야 합니다"
    note = rows[0]
    assert note.msg_type == "orchestrator"
    assert note.sender_key == "orchestrator", "사용자가 쓴 글이 아닙니다"
    assert "이전 세션 인수인계" in note.content
    assert "Redis 를 세션 캐시로 채택합니다" in note.content
    assert "캐시를 설계하라" not in note.content, "이전 발언 기록이 따라오면 안 됩니다"


@pytest.mark.asyncio
async def test_artifacts_are_listed_but_not_copied():
    """산출물은 원본 대화에 그대로 둡니다. 쪽지는 무엇이 있는지만 알려 줍니다."""
    sid = await _seed_session()
    factory = get_session_factory(DB_URL)

    async with factory() as db:
        result = await continue_session(db, sid)

    async with factory() as db:
        arts = (await db.execute(
            ArtifactModel.__table__.select()
            .where(ArtifactModel.session_id == result["session_id"])
        )).fetchall()
        note = (await db.execute(
            MessageModel.__table__.select()
            .where(MessageModel.session_id == result["session_id"])
        )).fetchall()[0]

    assert arts == []
    assert "시스템 아키텍처 다이어그램" in note.content


@pytest.mark.asyncio
async def test_conclusion_falls_back_to_the_markdown_artifact():
    """합성 발언이 없어도 최종 보고서 아티팩트가 있으면 그것을 넘깁니다.

    합성 호출은 실패했는데 아티팩트는 뽑힌 대화, 그리고 밖에서 가져다 넣은
    기록이 여기에 해당합니다. 결론을 못 넘기면 이어받기의 값어치가 절반입니다.
    """
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    sid = f"noorch-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(id=sid, title="합성 발언이 없는 대화",
                            active_agents=["orchestrator"]))
        db.add(MessageModel(
            id=str(uuid.uuid4()), session_id=sid, sender_key="architect",
            sender_name="A", sender_role="R", content="설계 발언",
            round_number=1, msg_type="agent",
        ))
        db.add(ArtifactModel(
            id=str(uuid.uuid4()), session_id=sid, artifact_type="markdown",
            title="최종 종합 보고서",
            content="## 결론\n\nKafka 를 이벤트 브로커로 씁니다.",
            language="markdown",
        ))
        await db.commit()

    async with factory() as db:
        result = await continue_session(db, sid)

    async with factory() as db:
        note = (await db.execute(
            MessageModel.__table__.select()
            .where(MessageModel.session_id == result["session_id"])
        )).fetchall()[0]

    assert "Kafka 를 이벤트 브로커로 씁니다" in note.content
    assert "최종 합성까지 가지 못한" not in note.content


@pytest.mark.asyncio
async def test_missing_source_returns_none():
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    async with factory() as db:
        assert await continue_session(db, "no-such-session") is None


@pytest.mark.asyncio
async def test_title_suffix_is_not_stacked():
    """이어받은 것을 또 이어받아도 '(이어서) (이어서)' 가 되지 않습니다."""
    sid = await _seed_session()
    factory = get_session_factory(DB_URL)
    async with factory() as db:
        first = await continue_session(db, sid)
    async with factory() as db:
        second = await continue_session(db, first["session_id"])
    assert second["title"].count(HANDOFF_TITLE_SUFFIX) == 1


# ------------------------------------------------------- 2. 지식 그래프 복사


@pytest.mark.asyncio
async def test_knowledge_graph_is_copied_not_moved(tmp_path, monkeypatch):
    """복사입니다. 원본 대화를 다시 열어도 그때 쌓은 사실이 그대로 있어야 합니다."""
    import app.mcp.manager as manager

    graph_dir = tmp_path / ".memory-graphs"
    graph_dir.mkdir()
    monkeypatch.setattr(manager, "memory_graph_dir", lambda workspace: graph_dir)

    src, dst = "session-a", "session-b"
    payload = '{"type":"entity","name":"Redis","observations":["세션 캐시로 채택"]}\n'
    (graph_dir / f"{src}.jsonl").write_text(payload, encoding="utf-8")

    assert carry_over_memory_graph(src, dst, str(tmp_path)) is True
    assert (graph_dir / f"{dst}.jsonl").read_text(encoding="utf-8") == payload
    assert (graph_dir / f"{src}.jsonl").exists(), "원본은 남아 있어야 합니다"


@pytest.mark.asyncio
async def test_carry_over_is_honest_when_there_is_nothing_to_carry(tmp_path, monkeypatch):
    import app.mcp.manager as manager

    graph_dir = tmp_path / ".memory-graphs"
    graph_dir.mkdir()
    monkeypatch.setattr(manager, "memory_graph_dir", lambda workspace: graph_dir)

    # 그래프 파일이 아예 없는 경우
    assert carry_over_memory_graph("empty-src", "dst", str(tmp_path)) is False
    # 있지만 비어 있는 경우
    (graph_dir / "blank.jsonl").write_text("", encoding="utf-8")
    assert carry_over_memory_graph("blank", "dst2", str(tmp_path)) is False


@pytest.mark.asyncio
async def test_existing_target_graph_is_never_overwritten(tmp_path, monkeypatch):
    import app.mcp.manager as manager

    graph_dir = tmp_path / ".memory-graphs"
    graph_dir.mkdir()
    monkeypatch.setattr(manager, "memory_graph_dir", lambda workspace: graph_dir)

    (graph_dir / "src.jsonl").write_text("from source\n", encoding="utf-8")
    (graph_dir / "dst.jsonl").write_text("already here\n", encoding="utf-8")

    assert carry_over_memory_graph("src", "dst", str(tmp_path)) is False
    assert (graph_dir / "dst.jsonl").read_text(encoding="utf-8") == "already here\n"


def test_no_memory_server_configured_is_not_an_error(monkeypatch):
    import app.mcp.manager as manager

    monkeypatch.setattr(manager, "memory_graph_dir", lambda workspace: None)
    assert carry_over_memory_graph("a", "b", "") is False


def test_carrying_onto_itself_is_refused():
    assert carry_over_memory_graph("same", "same", "") is False


@pytest.mark.asyncio
async def test_a_failed_graph_copy_does_not_stop_the_handoff(monkeypatch):
    """그래프를 못 옮겼다고 새 대화를 못 만들 이유는 없습니다."""
    import app.session_ops as ops

    def _explode(*a, **kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(ops, "carry_over_memory_graph", _explode)
    sid = await _seed_session()
    factory = get_session_factory(DB_URL)
    async with factory() as db:
        result = await continue_session(db, sid)

    assert result is not None, "그래프를 못 옮겨도 새 대화는 만들어져야 합니다"
    assert result["memory_carried"] is False

    async with factory() as db:
        note = (await db.execute(
            MessageModel.__table__.select()
            .where(MessageModel.session_id == result["session_id"])
        )).fetchall()[0]
    assert "이어받지 못했습니다" in note.content, "숨기지 말고 쪽지에 적혀야 합니다"


# ------------------------------------------------------------- 3. 쪽지 문구


def test_note_says_the_graph_came_along():
    note = build_handoff_note(
        source_title="T", synthesis=SYNTHESIS, artifact_titles=["다이어그램"],
        workspace="/w", memory_carried=True, message_count=12,
    )
    assert "이어받았습니다" in note
    assert "다시 논쟁하지 말고" in note


def test_note_says_plainly_when_the_graph_did_not_come_along():
    """반쪽 성공을 숨기면 에이전트가 없는 기억을 있다고 가정합니다."""
    note = build_handoff_note(
        source_title="T", synthesis=SYNTHESIS, artifact_titles=[],
        workspace="", memory_carried=False, message_count=3,
    )
    assert "이어받지 못했습니다" in note
    assert "없는 기억을 있다고 가정하지 마세요" in note


def test_note_handles_a_debate_that_never_reached_synthesis():
    note = build_handoff_note(
        source_title="T", synthesis="", artifact_titles=[],
        workspace="", memory_carried=False, message_count=1,
    )
    assert "최종 합성까지 가지 못한 대화입니다" in note


def test_long_conclusions_are_trimmed():
    """새 대화를 여는 이유가 컨텍스트 포화입니다. 첫 발언부터 채우면 안 됩니다."""
    huge = "가" * 40_000
    note = build_handoff_note(
        source_title="T", synthesis=huge, artifact_titles=[],
        workspace="", memory_carried=True, message_count=99,
    )
    assert len(note) < 12_000
    assert "자를 줄였습니다" in note
