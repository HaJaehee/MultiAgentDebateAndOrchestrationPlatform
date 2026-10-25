"""작업 공간 체크포인트 및 턴 롤백 테스트 (app/workspace_checkpoint.py, v1.5).

1. 스냅샷 커밋은 기존 HEAD, 브랜치, 인덱스를 변경하지 않고 격리된 ref 네임스페이스(`refs/mado/turns/…`)로만 관리된다.
2. 두 스냅샷 간의 추가/수정/삭제 변경 내역과 단일 파일 Diff 조회를 검증한다.
3. 롤백 기능 검증: 신규 생성 파일 삭제, 수정/삭제 파일 복원 및 턴 완료 후 외부 추가 변경 감지 시 중단 검증.
4. 엔진 연동 검증: 턴 시작/종료 체크포인트 자동 생성 및 이벤트 전달, 롤백 수행 시 시스템 메모 기록.
5. 턴 폐기 시 관련 Git ref 정리 검증.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app import workspace_checkpoint as wc
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.session_ops import discard_turn, revert_turn_files, turn_changes
from tests.fake_llm import FakeLLMCaller

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, workspace_checkpoints):
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "t@example.com")
    (root / "keep.txt").write_text("처음\n", encoding="utf-8")
    (root / "gone.txt").write_text("지워질 파일\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    (root / "staged.txt").write_text("에이전트가 add 만 해 둔 파일\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    return root


def test_a_snapshot_leaves_head_branch_and_index_alone(repo):
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (repo / "untracked.md").write_text("새 문서", encoding="utf-8")

    sha = wc.snapshot(repo, wc.turn_ref("t1", wc.START))

    assert sha and wc.resolve(repo, wc.turn_ref("t1", wc.START)) == sha
    assert _git(repo, "rev-parse", "HEAD") == head
    # 인덱스 그대로: add 해 둔 파일은 여전히 add 된 채, 새 파일은 여전히 추적되지 않은 채입니다.
    assert _git(repo, "diff", "--cached", "--name-only") == "staged.txt"
    assert "?? untracked.md" in _git(repo, "status", "--porcelain")
    files = _git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert {"keep.txt", "gone.txt", "staged.txt", "untracked.md"} <= set(files)
    assert not [f for f in files if f.startswith("node_modules/")]
    assert "refs/mado/turns/t1/start" in _git(repo, "for-each-ref", "--format=%(refname)")
    assert _git(repo, "log", "--oneline").count("\n") == 0                    # 기록에 커밋이 늘지 않음


def _make_turn_edits(repo: Path) -> None:
    (repo / "keep.txt").write_text("에이전트가 고침\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "new.py").write_text("print('new')\n", encoding="utf-8")


def test_changes_diff_and_revert(repo):
    start = wc.snapshot(repo, wc.turn_ref("t2", wc.START))
    _make_turn_edits(repo)
    end = wc.snapshot(repo, wc.turn_ref("t2", wc.END))

    changes = wc.changes(repo, start, end)
    assert {(c.status, c.path) for c in changes} == {("M", "keep.txt"), ("D", "gone.txt"), ("A", "src/pkg/new.py")}
    assert [c.label for c in changes if c.path == "keep.txt"] == ["수정"]
    diff = wc.file_diff(repo, start, end, "keep.txt")
    assert "-처음" in diff and "+에이전트가 고침" in diff

    result = wc.revert(repo, start, end)
    assert result.ok and len(result.reverted) == 3
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "처음\n"
    assert (repo / "gone.txt").read_text(encoding="utf-8") == "지워질 파일\n"
    assert not (repo / "src").exists(), "턴이 만든 파일과 그 빈 폴더는 사라집니다"
    assert "staged.txt" in _git(repo, "diff", "--cached", "--name-only")      # 인덱스는 여전히 그대로


def test_revert_refuses_when_a_file_changed_again_after_the_turn(repo):
    start = wc.snapshot(repo, wc.turn_ref("t3", wc.START))
    _make_turn_edits(repo)
    end = wc.snapshot(repo, wc.turn_ref("t3", wc.END))
    (repo / "keep.txt").write_text("다음 턴이 또 고침\n", encoding="utf-8")   # 턴이 끝난 뒤의 변경
    (repo / "other.txt").write_text("상관없는 파일\n", encoding="utf-8")

    result = wc.revert(repo, start, end)

    assert not result.ok and result.drifted == ["keep.txt"]
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "다음 턴이 또 고침\n"
    assert (repo / "src" / "pkg" / "new.py").exists(), "거부하면 아무것도 바꾸지 않습니다"


def test_forget_removes_the_names(repo):
    wc.snapshot(repo, wc.turn_ref("t4", wc.START))
    wc.snapshot(repo, wc.turn_ref("t4", wc.END))
    wc.snapshot(repo, wc.turn_ref("t5", wc.START))
    wc.forget(repo, ["t4", "t5", "없는-턴"])
    assert _git(repo, "for-each-ref", "refs/mado") == ""


def test_export_makes_an_independent_copy_of_a_snapshot(repo, tmp_path):
    _make_turn_edits(repo)                                   # gone.txt 지움, keep.txt 고침, 새 파일
    wc.snapshot(repo, wc.turn_ref("t6", wc.END))
    target = tmp_path / "copy"
    assert wc.export(repo, wc.turn_ref("t6", wc.END), target)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "에이전트가 고침\n"
    assert (target / "src" / "pkg" / "new.py").is_file() and not (target / "gone.txt").exists()
    status = _git(target, "status", "--porcelain")
    lines = {line.strip() for line in status.splitlines()}
    assert {"D gone.txt", "M keep.txt", "?? src/"} <= lines
    assert _git(target, "remote") == "" and _git(target, "for-each-ref", "refs/mado") == ""
    assert not wc.export(repo, wc.turn_ref("t6", wc.END), target), "있는 폴더에는 쓰지 않습니다"
    assert not wc.export(repo, wc.turn_ref("없는-턴", wc.END), tmp_path / "none")


def test_export_of_an_empty_snapshot_in_a_repository_without_commits(tmp_path, workspace_checkpoints):
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("x", encoding="utf-8")   # 스냅샷에 담지 않는 폴더뿐
    assert wc.snapshot(root, wc.turn_ref("t7", wc.END))
    assert wc.export(root, wc.turn_ref("t7", wc.END), tmp_path / "copy")
    assert [p.name for p in (tmp_path / "copy").iterdir()] == [".git"]


def test_nothing_happens_outside_a_repository(tmp_path, workspace_checkpoints):
    assert wc.snapshot(tmp_path, wc.turn_ref("t", wc.START)) is None
    assert wc.changes(tmp_path, "a", "b") is None
    assert not wc.revert(tmp_path, "a", "b").ok


class Writer(FakeLLMCaller):
    """코더가 발언할 때 작업 공간에 파일을 하나 만드는 대역 (도구 실행 흉내)."""

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.workspace = workspace

    async def call_agent(self, agent, messages, *args, **kwargs):
        if agent.key == "coder":
            (self.workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
        return await super().call_agent(agent, messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_a_turn_is_checkpointed_and_can_be_reverted(repo):
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"ckpt-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="체크포인트", strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator", "architect", "coder", "critic"], workspace_dir=str(repo),
        ))
        await db.commit()
    events = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(llm_caller=Writer(repo)).run_turn(
        session_id=sid, user_prompt="만들어 줘", on_event=on_event,
    )

    async with factory() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        assert turn.checkpoint["changed"] == 1 and turn.checkpoint["start"] and turn.checkpoint["end"]
        found = await turn_changes(db, sid, turn.id)
    assert [(c.status, c.path) for c in found.files] == [("A", "app.py")]

    checkpointed = next(e for e in events if e["type"] == "workspace_checkpointed")
    synthesis = state.messages[-1]
    assert checkpointed == {
        "type": "workspace_checkpointed", "turn_id": turn.id, "changed": 1, "message_id": synthesis.id,
    }
    # 턴이 다 끝난 뒤에 옵니다 — 결론 카드가 이미 그려져 있어야 칩을 붙일 수 있습니다.
    assert [e["type"] for e in events].index("turn_completed") < events.index(checkpointed)

    async with factory() as db:
        result = await revert_turn_files(db, sid, turn.id)
    assert result.ok and not (repo / "app.py").exists()

    async with factory() as db:
        note = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid, MessageModel.sender_key == "system")
        )).scalar_one()
        turn = await db.get(TurnModel, turn.id)
        assert "[작업 공간 되돌리기]" in note.content and "`app.py`(추가)" in note.content
        assert note.turn_meta == {"kind": "note", "reverted_turn": turn.id}
        assert turn.checkpoint["reverted_at"]

        # 턴을 버리면 저장소의 이름도 지웁니다.
        await discard_turn(db, sid, [], turn_id=turn.id)
        await db.commit()
    assert _git(repo, "for-each-ref", "refs/mado") == ""
