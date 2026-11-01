"""작업 공간 체크포인트 및 턴 롤백 테스트 (app/workspace_checkpoint.py, v1.5).

1. 스냅샷 커밋은 기존 HEAD, 브랜치, 인덱스를 변경하지 않고 격리된 ref 네임스페이스(`refs/mado/turns/…`)로만 관리된다.
2. 두 스냅샷 간의 추가/수정/삭제 변경 내역과 단일 파일 Diff 조회를 검증한다.
3. 롤백 기능 검증: 신규 생성 파일 삭제, 수정/삭제 파일 복원 및 턴 완료 후 외부 추가 변경 감지 시 중단 검증.
4. 엔진 연동 검증: 턴 시작/종료 체크포인트 자동 생성 및 이벤트 전달, 롤백 수행 시 시스템 메모 기록.
5. 턴 폐기 시 관련 Git ref 정리 검증.
6. 보존과 압축 검증: 기준을 모두 넘긴 턴만 지우고, 기록과 저장소를 함께 맞추며, `git gc --auto` 가 스냅샷을 보존한다.
"""

import hashlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List

import pytest
from sqlalchemy import select

from app import workspace_checkpoint as wc
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.session_ops import discard_turn, maintain_workspace, reviewable_changes, revert_turn_files, turn_changes
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


def test_a_same_size_edit_right_after_the_index_was_written_is_not_missed(repo):
    """복사한 인덱스의 시각이 지금이 되면 git 의 재확인이 꺼져, 크기가 같은 수정을 변경 없음으로 봅니다."""
    (repo / "same.txt").write_text("v = 0\n", encoding="utf-8")
    _git(repo, "add", "same.txt")
    start = wc.snapshot(repo, None)
    (repo / "same.txt").write_text("v = 1\n", encoding="utf-8")
    time.sleep(1.1)                                                           # 복사 시각이 수정보다 뒤의 초가 되도록
    end = wc.snapshot(repo, None)
    assert [(c.status, c.path) for c in wc.changes(repo, start, end)] == [("M", "same.txt")]


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


# ------------------------------------------------------------------ 보존과 압축 (ADR-044)


def _turn_from(repo: Path, monkeypatch, turn_id: str, days_ago: float, *which: str) -> Dict[str, str]:
    """`days_ago` 일 전에 찍은 것처럼 스냅샷을 남깁니다."""
    monkeypatch.setenv("GIT_COMMITTER_DATE", f"{int(time.time() - days_ago * 86400)} +0000")
    shas = {name: wc.snapshot(repo, wc.turn_ref(turn_id, name)) for name in which or (wc.START, wc.END)}
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    return shas


def test_only_turns_past_both_limits_expire(repo, monkeypatch):
    _turn_from(repo, monkeypatch, "oldest", 90)
    _turn_from(repo, monkeypatch, "old", 60)
    _turn_from(repo, monkeypatch, "recent", 1)

    assert wc.expired_turns(repo, keep_turns=1, keep_days=30) == ["oldest", "old"]
    assert wc.expired_turns(repo, keep_turns=2, keep_days=30) == ["oldest"], "최근 턴 수 안이면 남깁니다"
    assert wc.expired_turns(repo, keep_turns=1, keep_days=75) == ["oldest"], "최근 기간 안이면 남깁니다"
    assert wc.expired_turns(repo, keep_turns=3, keep_days=30) == []


def test_a_turn_is_as_old_as_its_latest_snapshot(repo, monkeypatch):
    """오래전에 시작했어도 최근에 이어 간 턴은 최근 턴입니다."""
    _turn_from(repo, monkeypatch, "resumed", 90, wc.START)
    _turn_from(repo, monkeypatch, "resumed", 1, wc.END)
    _turn_from(repo, monkeypatch, "newest", 0.5)

    assert wc.expired_turns(repo, keep_turns=1, keep_days=30) == []


def test_expiry_reads_nothing_outside_a_repository(tmp_path, workspace_checkpoints):
    assert wc.expired_turns(tmp_path, keep_turns=0, keep_days=0.001) == []
    wc.collect_garbage(tmp_path)


def _blobs_in_objects_17(count: int) -> List[str]:
    """`objects/17/` 에 들어갈 내용. `gc --auto` 는 이 폴더만 세어 전체 수를 짐작합니다."""
    found: List[str] = []
    number = 0
    while len(found) < count:
        content = f"blob-{number}"
        number += 1
        if hashlib.sha1(f"blob {len(content)}\0{content}".encode()).hexdigest().startswith("17"):
            found.append(content)
    return found


def _count_objects(repo: Path) -> Dict[str, int]:
    pairs = (line.split(": ") for line in _git(repo, "count-objects", "-v").splitlines())
    return {name: int(value) for name, value in pairs if value.isdigit()}


def test_collecting_garbage_packs_objects_and_keeps_snapshots(repo):
    _git(repo, "config", "gc.auto", "1")
    for index, content in enumerate(_blobs_in_objects_17(3)):
        (repo / f"blob{index}.txt").write_text(content, encoding="utf-8")
    sha = wc.snapshot(repo, wc.turn_ref("t", wc.START))
    assert _count_objects(repo)["count"] > 0

    wc.collect_garbage(repo)

    assert _count_objects(repo)["count"] == 0 and _count_objects(repo)["in-pack"] > 0
    assert wc.resolve(repo, wc.turn_ref("t", wc.START)) == sha
    assert wc.changes(repo, sha, sha) == []


def test_collecting_garbage_follows_the_users_gc_settings(repo):
    _git(repo, "config", "gc.auto", "0")
    for index, content in enumerate(_blobs_in_objects_17(3)):
        (repo / f"blob{index}.txt").write_text(content, encoding="utf-8")
    wc.snapshot(repo, wc.turn_ref("t", wc.START))
    before = _count_objects(repo)["count"]

    wc.collect_garbage(repo)

    assert _count_objects(repo)["count"] == before


def test_stopping_kills_running_collectors(monkeypatch):
    killed = []

    class Running:
        def kill(self):
            killed.append(self)

    running = Running()
    monkeypatch.setattr(wc, "_collecting", {running})

    wc.stop_collecting()

    assert killed == [running]


@pytest.mark.asyncio
async def test_maintenance_drops_old_checkpoints_from_the_repository_and_the_record(repo, monkeypatch):
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"keep-{uuid.uuid4().hex[:8]}"
    old_id, recent_id = str(uuid.uuid4()), str(uuid.uuid4())
    head = _git(repo, "rev-parse", "HEAD")
    old = _turn_from(repo, monkeypatch, old_id, 90)
    recent = _turn_from(repo, monkeypatch, recent_id, 1)
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="보존", strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator"], workspace_dir=str(repo),
        ))
        for turn_id, shas in ((old_id, old), (recent_id, recent)):
            db.add(TurnModel(
                id=turn_id, session_id=sid, status="completed", phase="done", config={"workspace_dir": str(repo)},
                checkpoint={"start": shas[wc.START], "end": shas[wc.END], "changed": 3},
            ))
        await db.commit()
    expired = wc.expired_turns
    monkeypatch.setattr(wc, "expired_turns", lambda workspace: expired(workspace, keep_turns=1, keep_days=30))

    await maintain_workspace(factory, repo)

    names = _git(repo, "for-each-ref", "--format=%(refname)", "refs/mado").splitlines()
    assert names and all(name.startswith(f"refs/mado/turns/{recent_id}/") for name in names)
    assert _git(repo, "rev-parse", "HEAD") == head
    async with factory() as db:
        kept = await db.get(TurnModel, recent_id)
        gone = await db.get(TurnModel, old_id)
        assert kept.checkpoint["end"] == recent[wc.END] and reviewable_changes(kept.checkpoint) == 3
        # 바뀐 파일 수는 턴의 기록으로 남고, 스냅샷이 없으니 변경 칩과 되돌리기는 내놓지 않습니다.
        assert gone.checkpoint == {"changed": 3} and reviewable_changes(gone.checkpoint) is None
        assert await turn_changes(db, sid, old_id) is None
        assert await turn_changes(db, sid, recent_id) is not None
        reverted = await revert_turn_files(db, sid, old_id)
    assert not reverted.ok and "체크포인트" in reverted.error


@pytest.mark.asyncio
async def test_a_failure_to_update_the_record_keeps_the_names(repo, monkeypatch):
    """이름을 먼저 지우면 스냅샷이 사라진 턴의 기록에 되돌리기가 남습니다."""
    _turn_from(repo, monkeypatch, "old", 90)
    expired = wc.expired_turns
    monkeypatch.setattr(wc, "expired_turns", lambda workspace: expired(workspace, keep_turns=0, keep_days=30))

    def broken():
        raise RuntimeError("DB 없음")

    with pytest.raises(RuntimeError):
        await maintain_workspace(broken, repo)

    assert wc.resolve(repo, wc.turn_ref("old", wc.END))
