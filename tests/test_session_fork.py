"""대화 세션 분기(Fork) 및 비교 테스트 (app/session_fork.py, v1.5).

1. 특정 턴 시점에서 분기 시 해당 턴까지의 발언, 도구 기록, 아티팩트, 턴 기록이 새 ID로 복제되고 상호 참조 ID가 갱신된다.
2. 해당 턴 완료 시점의 대화 기억(결정 장부)과 작업 공간 디렉터리 스냅샷이 독립적으로 복제된다 (원본 유지).
3. 최종 턴 분기 시 세션 최신 기억을 사용하며, Git 미설치 시 현재 작업 공간 복제 후 안내를 표시한다.
4. 미완료 턴 분기는 차단되며, 분기된 세션에서 후속 턴을 정상 실행할 수 있다.
5. 비교 화면(/compare) 표시용 요약 데이터 및 정량 평가 저장을 검증한다.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database.models import (
    TURN_INTERRUPTED,
    ArtifactModel,
    MessageModel,
    SessionModel,
    ToolCallRecordModel,
    TurnModel,
)
from app.database.session import get_session_factory, init_db
from app.orchestration import context_memory as memory
from app.orchestration.engine import OrchestratorEngine
from app.session_fork import FORK_TITLE_SUFFIX, ForkError, compare_view, fork_session, rate_turn
from tests.fake_llm import FakeLLMCaller

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class Turns(FakeLLMCaller):
    """턴마다 다른 파일 내용과 다른 장부를 남기는 대역 (코더가 쓰고, 장부에 턴 번호가 적힙니다)."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(tool_calls={"coder": [{"tool_name": "filesystem__write_file", "arguments": {"path": "app.py"},
                                                "output": "ok", "status": "success"}]})
        self.workspace = workspace
        self.turn = 0

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        if memory.LEDGER_PROMPT_MARKER in last:
            return (f"## 요구사항·제약\n- 턴 {self.turn} 의 요구\n\n## 결정 사항\n- 결정 {self.turn}\n\n"
                    "## 기각된 대안\n- 없음\n\n## 미해결 쟁점\n- 없음\n\n## 담당·다음 할 일\n- 없음")
        return super()._reply_for(agent, messages)

    async def call_agent(self, agent, messages, *args, **kwargs):
        if agent.key == "coder":
            (self.workspace / "app.py").write_text(f"print({self.turn})\n", encoding="utf-8")
        return await super().call_agent(agent, messages, *args, **kwargs)


async def _two_turns(workspace: Path):
    await init_db("sqlite+aiosqlite:///:memory:")
    factory = get_session_factory()
    sid = f"fork-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(id=sid, title="원본", strategy="sequential_debate", max_rounds=1,
                            active_agents=["orchestrator", "architect", "coder", "critic"],
                            workspace_dir=str(workspace), tool_grants=["write(app.py)"]))
        await db.commit()
    caller = Turns(workspace)
    engine = OrchestratorEngine(llm_caller=caller)
    for n in (1, 2):
        caller.turn = n
        await engine.run_turn(session_id=sid, user_prompt=f"{n}번째 요청")
    async with factory() as db:
        turns = (await db.execute(
            select(TurnModel).where(TurnModel.session_id == sid).order_by(TurnModel.started_at)
        )).scalars().all()
    return factory, sid, turns, engine


@pytest.fixture
def repo(tmp_path, workspace_checkpoints):
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "t@example.com")
    (root / "README.md").write_text("처음\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@needs_git
@pytest.mark.asyncio
async def test_fork_at_an_earlier_turn_takes_that_turn_and_its_workspace(repo):
    factory, sid, turns, engine = await _two_turns(repo)
    first, second = turns
    assert (repo / "app.py").read_text(encoding="utf-8") == "print(2)\n"

    async with factory() as db:
        result = await fork_session(db, sid, first.id)
    assert result.from_snapshot and result.title == "원본" + FORK_TITLE_SUFFIX
    fork_ws = Path(result.workspace)
    assert fork_ws != repo and fork_ws.parent == repo.parent
    # 그 턴이 끝났을 때의 파일, 원본의 커밋 기록. 원본은 그대로입니다.
    assert (fork_ws / "app.py").read_text(encoding="utf-8") == "print(1)\n"
    assert _git(fork_ws, "log", "--oneline") == _git(repo, "log", "--oneline")
    assert _git(fork_ws, "remote") == "", "원본 저장소와의 연결은 끊습니다"
    assert "?? app.py" in _git(fork_ws, "status", "--porcelain")
    assert (repo / "app.py").read_text(encoding="utf-8") == "print(2)\n"

    async with factory() as db:
        new = await db.get(SessionModel, result.session_id)
        source_msgs = (await db.execute(select(MessageModel).where(MessageModel.turn_id == first.id))).scalars().all()
        msgs = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == result.session_id)
        )).scalars().all()
        new_turns = (await db.execute(select(TurnModel).where(TurnModel.session_id == result.session_id))).scalars().all()
        tools = (await db.execute(select(ToolCallRecordModel).where(
            ToolCallRecordModel.session_id == result.session_id))).scalars().all()
        arts = (await db.execute(select(ArtifactModel).where(ArtifactModel.session_id == result.session_id))).scalars().all()

    assert len(msgs) == len(source_msgs) == result.messages
    assert {m.content for m in msgs} == {m.content for m in source_msgs}
    assert not {m.id for m in msgs} & {m.id for m in source_msgs}, "id 는 새로 매깁니다"
    assert not any("2번째 요청" in m.content for m in msgs)
    assert len(new_turns) == 1 and new_turns[0].id != first.id and new_turns[0].checkpoint is None
    assert {m.turn_id for m in msgs} == {new_turns[0].id}
    assert new_turns[0].opening_message_id in {m.id for m in msgs}
    assert tools and all(t.message_id in {m.id for m in msgs} for t in tools)
    assert arts
    # 그 턴이 끝났을 때의 기억, 풀린 잠금, 비운 "이 대화에서 허용", 출처.
    assert "결정 1" in new.decision_ledger and "결정 2" not in new.decision_ledger
    assert new.ledger_through_id in {m.id for m in msgs}
    assert new.personas_locked is False and new.tool_grants == []
    assert new.forked_from == {"session_id": sid, "turn_id": first.id, "title": "원본"}
    assert new.workspace_dir == str(fork_ws)

    # 갈라 만든 대화에서 다음 턴이 돕니다 (원본의 2번째 턴 대신).
    engine.llm_caller.workspace, engine.llm_caller.turn = fork_ws, 3
    state = await engine.run_turn(session_id=result.session_id, user_prompt="다른 길로")
    assert state.status == "completed"
    assert (fork_ws / "app.py").read_text(encoding="utf-8") == "print(3)\n"
    assert (repo / "app.py").read_text(encoding="utf-8") == "print(2)\n"


@needs_git
@pytest.mark.asyncio
async def test_fork_at_the_latest_turn_uses_the_conversation_memory(repo):
    factory, sid, turns, _engine = await _two_turns(repo)
    async with factory() as db:
        result = await fork_session(db, sid, turns[-1].id)
        new = await db.get(SessionModel, result.session_id)
        source = await db.get(SessionModel, sid)
    assert new.decision_ledger == source.decision_ledger and "결정 2" in new.decision_ledger
    assert (Path(result.workspace) / "app.py").read_text(encoding="utf-8") == "print(2)\n"


@pytest.mark.asyncio
async def test_without_git_the_current_folder_is_copied(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    factory, sid, turns, _engine = await _two_turns(workspace)
    async with factory() as db:
        result = await fork_session(db, sid, turns[0].id)
    assert not result.from_snapshot
    copied = Path(result.workspace)
    assert (copied / "app.py").read_text(encoding="utf-8") == "print(2)\n", "스냅샷이 없으면 지금 모습입니다"
    assert not (copied / "node_modules").exists()


@pytest.mark.asyncio
async def test_notes_between_turns_follow_but_revert_notes_do_not(tmp_path):
    factory, sid, turns, _engine = await _two_turns(tmp_path)
    between = turns[1].started_at - (turns[1].started_at - turns[0].started_at) / 10
    async with factory() as db:
        for content, meta in (("사람의 메모", {"kind": "note"}),
                              ("[작업 공간 되돌리기] …", {"kind": "note", "reverted_turn": turns[0].id})):
            db.add(MessageModel(session_id=sid, sender_key="system", sender_name="MADO", content=content,
                                msg_type="system", created_at=between, turn_meta=meta))
        await db.commit()
        result = await fork_session(db, sid, turns[0].id)
        contents = (await db.execute(
            select(MessageModel.content).where(MessageModel.session_id == result.session_id)
        )).scalars().all()
    assert "사람의 메모" in contents and "[작업 공간 되돌리기] …" not in contents


@pytest.mark.asyncio
async def test_unfinished_turns_cannot_be_forked_and_unknown_ids_fail(tmp_path):
    factory, sid, turns, _engine = await _two_turns(tmp_path)
    async with factory() as db:
        turn = await db.get(TurnModel, turns[0].id)
        turn.status = TURN_INTERRUPTED
        await db.commit()
        with pytest.raises(ForkError):
            await fork_session(db, sid, turns[0].id)
        with pytest.raises(ForkError):
            await fork_session(db, sid, "없는-턴")
        with pytest.raises(ForkError):
            await fork_session(db, "없는-대화", turns[0].id)


@pytest.mark.asyncio
async def test_compare_view_and_ratings(tmp_path):
    factory, sid, turns, _engine = await _two_turns(tmp_path)
    async with factory() as db:
        result = await fork_session(db, sid, turns[0].id)
        assert await rate_turn(db, turns[-1].id, 1, "  더 낫다 ")
        views = await compare_view(db, [sid, result.session_id, "없는-대화"])
    assert [v["session_id"] for v in views] == [sid, result.session_id]
    original, fork = views
    assert original["rating"]["score"] == 1 and original["rating"]["note"] == "더 낫다"
    assert original["request"] == "2번째 요청" and fork["request"] == "1번째 요청"
    assert fork["forked_from"]["session_id"] == sid and fork["rating"] is None
    assert "최종 합의 요약" in fork["conclusion"]
    async with factory() as db:
        assert await rate_turn(db, turns[-1].id, 0)
        assert (await db.get(TurnModel, turns[-1].id)).rating is None
        assert not await rate_turn(db, "없는-턴", 1)
