"""작업 공간 체크포인트: 턴이 시작하고 끝날 때 작업 공간의 모습을 git 에 남깁니다 (v1.5).

에이전트는 도구로 작업 공간의 파일을 만들고 고칩니다. 한 턴이 무엇을 바꿨는지 보고, 마음에 들지 않으면
그 턴의 변경만 되돌릴 수 있어야 `auto` 도구 모드를 마음 놓고 씁니다. 작업 공간은 이미 git 저장소이므로
(`app/mcp/manager.ensure_workspace`) 그 저장소에 스냅샷을 남깁니다.

**에이전트의 git 기록을 건드리지 않습니다.** 스냅샷은 HEAD 에도 브랜치에도 올라가지 않는 커밋이고,
`refs/mado/turns/<턴 id>/start` · `end` 라는 따로 된 이름으로만 가리킵니다. 만들 때는 저장소의 인덱스를
복사한 임시 인덱스(`GIT_INDEX_FILE`)를 써서, 에이전트가 `git add` 해 둔 것도 그대로 둡니다. 그래서
`git log`·`git status` 에는 아무것도 보이지 않습니다.

* 무엇을 담나 — `.gitignore` 를 따르고, 의존성·캐시 폴더(`node_modules`, `.venv`, `__pycache__` 등)와
  런타임 임시 폴더(`.mado`)는 뺍니다. 다운로드 창이 훑지 않는 폴더와 같은 목록입니다 (`dist`·`build` 는
  사용자가 만든 결과물일 수 있어 담습니다).
* 되돌리기 — 그 턴에서 바뀐 파일만 턴 시작 때의 모습으로 돌립니다. 턴에서 생긴 파일은 지우고, 고치거나
  지운 파일은 되살립니다. 턴이 끝난 뒤 같은 파일이 **또** 바뀌었으면(다음 턴, 같은 폴더를 쓰는 다른 대화,
  사람의 손) 그 변경을 덮어쓰지 않도록 거부하고 그 파일을 알려 줍니다.

git 이 없거나 작업 공간이 저장소가 아니면 아무것도 하지 않습니다(None). 모두 이벤트 루프 밖에서 부르는
동기 함수입니다 (`asyncio.to_thread`).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.mcp.manager import find_git_executable
from app.workspace_files import EXCLUDED_DIRS

logger = logging.getLogger(__name__)

REF_ROOT = "refs/mado/turns"
START, END = "start", "end"

# 스냅샷에서 빼는 폴더. 사용자가 만든 결과물일 수 있는 `dist`·`build` 는 담습니다.
SNAPSHOT_EXCLUDED_DIRS = sorted(EXCLUDED_DIRS - {"dist", "build", ".git", ".hg", ".svn"})

# 화면에 보여 줄 diff 한 건의 상한(글자). 큰 생성 파일 하나가 대화상자를 멈추지 않게 합니다.
MAX_DIFF_CHARS = 200_000

# git 명령 하나의 시간 상한(초). 큰 작업 공간의 첫 스냅샷은 파일을 모두 해시하므로 넉넉히 둡니다.
GIT_TIMEOUT = 120

_IDENTITY = {
    "GIT_AUTHOR_NAME": "MADO", "GIT_AUTHOR_EMAIL": "mado@localhost",
    "GIT_COMMITTER_NAME": "MADO", "GIT_COMMITTER_EMAIL": "mado@localhost",
}

STATUS_LABELS = {"A": "추가", "M": "수정", "D": "삭제", "T": "형식 변경"}


@dataclass(frozen=True)
class FileChange:
    status: str  # A 추가 · M 수정 · D 삭제 · T 형식 변경 (`git diff --name-status --no-renames`)
    path: str    # 작업 공간 기준, `/` 구분

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass
class RevertResult:
    ok: bool
    reverted: List[FileChange] = field(default_factory=list)
    # 턴이 끝난 뒤 또 바뀌어서 되돌리지 않은 파일. 있으면 `ok` 는 False 입니다.
    drifted: List[str] = field(default_factory=list)
    error: str = ""


def turn_ref(turn_id: str, which: str) -> str:
    return f"{REF_ROOT}/{turn_id}/{which}"


def _repo(workspace: str | Path) -> Optional[Tuple[str, Path]]:
    git = find_git_executable()
    root = Path(workspace).expanduser().resolve()
    if not git or not (root / ".git").exists():
        return None
    return git, root


def _git(
    git: str, root: Path, *args: str, env: Optional[Dict[str, str]] = None, stdin: Optional[bytes] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=True, timeout=GIT_TIMEOUT, check=False, env=env, input=stdin,
    )


def _text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or b"").decode("utf-8", errors="replace").strip()


def _error(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or b"").decode("utf-8", errors="replace").strip()


def snapshot(workspace: str | Path, ref: Optional[str]) -> Optional[str]:
    """작업 공간의 지금 모습을 커밋으로 만들고, `ref` 를 주면 그 이름을 답니다. 커밋 id, 못 하면 None."""
    found = _repo(workspace)
    if found is None:
        return None
    git, root = found
    with tempfile.TemporaryDirectory(prefix="mado-ckpt-") as tmp:
        index = Path(tmp) / "index"
        real = root / ".git" / "index"
        # 저장소의 인덱스를 복사해 시작합니다. 파일마다 남아 있는 크기·시각 기록 덕에 바뀌지 않은
        # 파일을 다시 해시하지 않습니다. 복사본이 깨졌으면(에이전트가 쓰는 중) 빈 인덱스로 다시 합니다.
        if real.is_file():
            try:
                shutil.copyfile(real, index)
            except OSError:
                pass
        env = {**os.environ, **_IDENTITY, "GIT_INDEX_FILE": str(index)}
        excludes = [f":(exclude,glob)**/{name}/**" for name in SNAPSHOT_EXCLUDED_DIRS]
        add = _git(git, root, "add", "-A", "--", ".", *excludes, env=env)
        if add.returncode != 0 and index.exists():
            index.unlink()
            add = _git(git, root, "add", "-A", "--", ".", *excludes, env=env)
        if add.returncode != 0:
            logger.warning(f"Workspace checkpoint skipped for {root}: git add failed: {_error(add)}")
            return None
        tree = _git(git, root, "write-tree", env=env)
        if tree.returncode != 0:
            logger.warning(f"Workspace checkpoint skipped for {root}: {_error(tree)}")
            return None
        commit = _git(git, root, "commit-tree", _text(tree), "-m", f"MADO checkpoint {ref or '(temporary)'}", env=env)
        if commit.returncode != 0:
            logger.warning(f"Workspace checkpoint skipped for {root}: {_error(commit)}")
            return None
    sha = _text(commit)
    if ref:
        update = _git(git, root, "update-ref", ref, sha)
        if update.returncode != 0:
            logger.warning(f"Could not name the workspace checkpoint {ref} in {root}: {_error(update)}")
            return None
    return sha


def resolve(workspace: str | Path, ref: str) -> Optional[str]:
    """`ref` 가 가리키는 커밋 id. 없으면 None."""
    found = _repo(workspace)
    if found is None:
        return None
    git, root = found
    proc = _git(git, root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return _text(proc) if proc.returncode == 0 else None


def changes(workspace: str | Path, start: str, end: str) -> Optional[List[FileChange]]:
    """두 스냅샷 사이에 바뀐 파일. 이름 바꾸기는 지우기와 추가로 셉니다. 읽지 못하면 None."""
    found = _repo(workspace)
    if found is None:
        return None
    git, root = found
    proc = _git(git, root, "diff", "--name-status", "--no-renames", "-z", start, end)
    if proc.returncode != 0:
        logger.warning(f"Could not list checkpoint changes in {root}: {_error(proc)}")
        return None
    parts = (proc.stdout or b"").decode("utf-8", errors="replace").split("\0")
    out: List[FileChange] = []
    for status, path in zip(parts[0::2], parts[1::2]):
        if status and path:
            out.append(FileChange(status=status[0], path=path))
    return out


def file_diff(workspace: str | Path, start: str, end: str, path: str) -> str:
    """한 파일의 diff (unified). 너무 길면 자르고 그렇다고 적습니다."""
    found = _repo(workspace)
    if found is None:
        return ""
    git, root = found
    proc = _git(git, root, "diff", "--no-renames", "--no-color", start, end, "--", path)
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n… (diff 가 길어 {MAX_DIFF_CHARS:,}자에서 잘랐습니다)\n"
    return text


def revert(workspace: str | Path, start: str, end: str) -> RevertResult:
    """`start`→`end` 사이에 바뀐 파일을 `start` 의 모습으로 돌립니다.

    그 파일이 `end` 뒤에 또 바뀌었으면 아무것도 하지 않고 그 목록을 돌려줍니다 — 나중의 변경을 말없이
    덮어쓰면 되돌리기가 새 사고가 됩니다.
    """
    found = _repo(workspace)
    if found is None:
        return RevertResult(ok=False, error="작업 공간이 git 저장소가 아니거나 git 을 찾지 못했습니다.")
    git, root = found
    turn_changes = changes(root, start, end)
    if turn_changes is None:
        return RevertResult(ok=False, error="이 턴의 스냅샷을 읽지 못했습니다.")
    if not turn_changes:
        return RevertResult(ok=True)

    now = snapshot(root, None)
    if now is None:
        return RevertResult(ok=False, error="지금 작업 공간의 모습을 읽지 못했습니다.")
    later = changes(root, end, now)
    if later is None:
        return RevertResult(ok=False, error="턴이 끝난 뒤의 변경을 확인하지 못했습니다.")
    touched = {c.path for c in turn_changes}
    drifted = sorted(c.path for c in later if c.path in touched)
    if drifted:
        return RevertResult(ok=False, drifted=drifted)

    restore = [c.path for c in turn_changes if c.status != "A"]
    if restore:
        proc = _git(
            git, root, "restore", f"--source={start}", "--worktree",
            "--pathspec-from-file=-", "--pathspec-file-nul",
            stdin="\0".join(restore).encode("utf-8"),
        )
        if proc.returncode != 0:
            return RevertResult(ok=False, error=f"파일을 되살리지 못했습니다: {_error(proc)}")
    for change in turn_changes:
        if change.status != "A":
            continue
        target = (root / change.path).resolve()
        if root not in target.parents:
            continue  # 저장소 밖을 가리키는 이름은 건드리지 않습니다
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            return RevertResult(ok=False, reverted=[], error=f"`{change.path}` 를 지우지 못했습니다: {exc}")
        _remove_empty_parents(target.parent, root)
    return RevertResult(ok=True, reverted=list(turn_changes))


def _remove_empty_parents(folder: Path, root: Path) -> None:
    """턴이 만든 파일을 지우고 남은 빈 폴더를 작업 공간 최상위 직전까지 정리합니다."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def export(workspace: str | Path, ref: str, target: str | Path) -> bool:
    """`ref` 스냅샷의 모습으로 `target` 에 독립된 작업 공간을 만듭니다 (대화 분기, `app/session_fork.py`).

    원본을 복제해 git 기록(에이전트의 커밋)은 그대로 가져오고, 작업 트리만 스냅샷의 모습으로 맞춥니다.
    스냅샷에 있던 것 중 커밋되지 않은 변경은 복제본에서도 커밋되지 않은 변경입니다. 원본과의 연결(origin)은
    끊어, 갈라진 대화가 원본 저장소를 건드릴 길을 남기지 않습니다. 못 하면 False — 반쯤 만든 폴더는 지웁니다.
    """
    found = _repo(workspace)
    destination = Path(target).expanduser().resolve()
    if found is None or destination.exists():
        return False
    git, root = found
    sha = resolve(root, ref)
    if not sha:
        return False
    clone = subprocess.run(
        [git, "clone", "-q", "--no-hardlinks", str(root), str(destination)],
        capture_output=True, timeout=GIT_TIMEOUT, check=False,
    )
    if clone.returncode != 0:
        logger.warning(f"Could not clone {root} for a fork: {_error(clone)}")
        shutil.rmtree(destination, ignore_errors=True)
        return False
    try:
        _git(git, destination, "remote", "remove", "origin")
        fetch = _git(git, destination, "fetch", "-q", str(root), f"{ref}:refs/mado/fork-base")
        if fetch.returncode != 0:
            raise RuntimeError(_error(fetch))
        # 작업 트리를 스냅샷과 똑같이: 스냅샷의 파일을 쓰고, HEAD 에는 있지만 스냅샷에는 없는 파일(턴 끝에
        # 지워져 있던 것)은 지웁니다. 빈 스냅샷(빼는 폴더만 바뀐 턴)도 그대로 됩니다.
        read = _git(git, destination, "read-tree", "-u", "--reset", sha)
        if read.returncode != 0:
            raise RuntimeError(_error(read))
        # 인덱스는 HEAD 로 되돌려, 스냅샷에서 커밋되지 않았던 변경이 여기서도 커밋되지 않은 변경으로 보이게 합니다.
        if _git(git, destination, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
            _git(git, destination, "reset", "-q")
        else:
            _git(git, destination, "read-tree", "--empty")
        _git(git, destination, "update-ref", "-d", "refs/mado/fork-base")
    except Exception as exc:  # noqa: BLE001 - 반쯤 만든 폴더를 남기지 않습니다
        logger.warning(f"Could not export {ref} of {root} to {destination}: {exc}")
        shutil.rmtree(destination, ignore_errors=True)
        return False
    return True


def copy_folder(workspace: str | Path, target: str | Path) -> bool:
    """스냅샷이 없을 때: 작업 공간의 지금 모습을 복사합니다 (의존성·캐시 폴더 제외). 못 하면 False."""
    source = Path(workspace).expanduser().resolve()
    destination = Path(target).expanduser().resolve()
    if not source.is_dir() or destination.exists():
        return False
    try:
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*SNAPSHOT_EXCLUDED_DIRS))
    except OSError as exc:
        logger.warning(f"Could not copy {source} to {destination}: {exc}")
        shutil.rmtree(destination, ignore_errors=True)
        return False
    return True


def forget(workspace: str | Path, turn_ids: Sequence[str]) -> None:
    """턴들의 스냅샷 이름을 지웁니다 (턴을 버리거나 대화를 지울 때). 커밋 자체는 git 이 나중에 치웁니다."""
    found = _repo(workspace)
    if found is None or not turn_ids:
        return
    git, root = found
    lines = "".join(f"delete {turn_ref(t, which)}\n" for t in turn_ids for which in (START, END))
    proc = _git(git, root, "update-ref", "--stdin", stdin=lines.encode("utf-8"))
    if proc.returncode != 0:
        # 한 줄이라도 없는 이름이면 통째로 실패합니다. 하나씩 지웁니다.
        for turn_id in turn_ids:
            for which in (START, END):
                _git(git, root, "update-ref", "-d", turn_ref(turn_id, which))
