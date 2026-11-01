"""작업 공간 체크포인트: 턴 시작 및 종료 시 작업 공간 상태를 Git 스냅샷으로 기록 (v1.5).

에이전트는 도구를 사용하여 작업 공간 내 파일을 생성하고 수정합니다. 각 턴에서 변경된 파일 내역을 확인하고,
필요 시 해당 턴의 변경 사항만 안전하게 롤백할 수 있도록 Git 기반 스냅샷 기능을 제공합니다. 작업 공간은
기본적으로 Git 저장소로 관리됩니다 (`app/mcp/manager.ensure_workspace`).

**사용자 및 에이전트의 기존 Git 작업 히스토리를 전혀 침범하지 않습니다.** 체크포인트 스냅샷 커밋은 일반 브랜치나
HEAD에 등록되지 않으며, `refs/mado/turns/<턴 id>/start` 및 `end`라는 독립된 Git ref 네임스페이스로만 관리됩니다.
스냅샷 생성 시 별도의 임시 인덱스(`GIT_INDEX_FILE`)를 격리 사용하므로, 에이전트가 `git add`한 기존 스테이징 내역이
온전히 보존되며 `git log`나 `git status`에도 노출되지 않습니다.

* 대상 파일 필터링 — `.gitignore`를 준수하며, 외부 의존성 및 캐시 디렉터리(`node_modules`, `.venv`, `__pycache__` 등)와
  런타임 임시 디렉터리(`.mado`)는 제외합니다. (사용자가 빌드한 산출물일 수 있는 `dist`, `build` 디렉터리는 포함)
* 롤백(되돌리기) — 해당 턴에서 변경된 파일들만 턴 시작 전 상태로 복원합니다. 생성된 파일은 삭제하고, 수정·삭제된
  파일은 원상 복구합니다. 턴 완료 후 외부나 후속 턴에서 동일 파일이 추가로 수정된 경우, 변경 사항 유실을 방지하기 위해
  롤백을 안전하게 중단하고 충돌 파일 목록을 안내합니다.
* 보존과 압축 (ADR-044) — 턴마다 스냅샷이 쌓이므로, 작업 공간이 쉴 때 오래된 턴의 이름을 지우고(`expired_turns`, `forget`)
  `git gc --auto` 를 돌립니다(`collect_garbage`). 부르는 쪽은 `app/session_ops.maintain_workspace` 입니다.

Git 미설치 환경이거나 작업 공간이 Git 저장소가 아닌 경우 조용히 무시(None)합니다.
본 모듈의 모든 함수는 비동기 이벤트 루프 외부(`asyncio.to_thread`)에서 안전하게 실행되도록 설계된 동기 함수입니다.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.code_diff import TRUNCATION_MARK
from app.mcp.client import positive_float_env
from app.mcp.manager import find_git_executable
from app.workspace_files import EXCLUDED_DIRS

logger = logging.getLogger(__name__)

REF_ROOT = "refs/mado/turns"
START, END = "start", "end"

# 턴 안의 경계 스냅샷 (ADR-043): 라운드(`round-N`)·그래프 단계(`step-N`)가 시작할 때와 토론이 끝났을 때.
# 되돌리기와 대화 분기는 쓰지 않고, 변경 내역을 라운드별로 나눠 볼 때만 읽습니다.
DEBATE_END = "debate-end"
_BOUNDARY = re.compile(r"^(round|step)-(\d+)$")

# 스냅샷 제외 대상 디렉터리 (`dist`, `build`는 사용자 산출물일 수 있으므로 보존)
SNAPSHOT_EXCLUDED_DIRS = sorted(EXCLUDED_DIRS - {"dist", "build", ".git", ".hg", ".svn"})

# Diff 텍스트 최대 글자 수 제한 (대용량 파일 Diff로 인한 UI 프리징 방지)
MAX_DIFF_CHARS = 200_000

# Git 단일 명령어 타임아웃 (초 단위)
GIT_TIMEOUT = 120

# `git gc --auto` 타임아웃 (초 단위). 기준에 닿으면 저장소 전체를 다시 묶으므로 일반 명령어보다 깁니다.
GC_TIMEOUT = 900

# 체크포인트 보존 기준 (작업 공간별). 최근 턴과 최근 기간을 함께 남기고, 둘 다 넘긴 턴만 지웁니다.
KEEP_TURNS = int(positive_float_env("CHECKPOINT_KEEP_TURNS", 200))
KEEP_DAYS = positive_float_env("CHECKPOINT_KEEP_DAYS", 30.0)

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


@dataclass(frozen=True)
class Segment:
    """턴 안의 한 구간: `start` 스냅샷에서 `end` 스냅샷까지 바뀐 파일."""

    label: str
    start: str
    end: str
    files: List[FileChange]


@dataclass
class RevertResult:
    ok: bool
    reverted: List[FileChange] = field(default_factory=list)
    # 턴 완료 후 외부에서 추가 수정되어 롤백이 보류된 충돌 파일 목록
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
    """현재 작업 공간 상태를 독립된 Git 커밋으로 스냅샷하고, `ref`가 지정된 경우 해당 참조를 생성합니다."""
    found = _repo(workspace)
    if found is None:
        return None
    git, root = found
    with tempfile.TemporaryDirectory(prefix="mado-ckpt-") as tmp:
        index = Path(tmp) / "index"
        real = root / ".git" / "index"
        # 저장소의 인덱스를 복사해 시작합니다. 파일마다 남아 있는 크기·시각 기록 덕에 바뀌지 않은
        # 파일을 다시 해시하지 않습니다. 복사본이 깨졌으면(에이전트가 쓰는 중) 빈 인덱스로 다시 합니다.
        # 인덱스의 시각도 함께 복사합니다(`copy2`). git 은 인덱스보다 늦거나 같은 시각에 고친 파일의 내용을
        # 다시 읽는데, 복사본 시각이 지금이 되면 이 검사가 꺼져 같은 초에 같은 크기로 고친 파일을 놓칩니다.
        if real.is_file():
            try:
                shutil.copy2(real, index)
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
    proc = _git(git, root, "diff", "--no-renames", "--no-color", "--no-ext-diff", start, end, "--", path)
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n{TRUNCATION_MARK} (내용이 길어 최대 {MAX_DIFF_CHARS:,}자까지만 표시합니다)\n"
    return text


def turn_segments(workspace: str | Path, turn_id: str, start: str, end: str) -> List[Segment]:
    """턴을 라운드(그래프는 단계)별 구간으로 나눕니다. 경계 스냅샷이 없는 턴이면 빈 목록입니다.

    라운드는 파일이 바뀌지 않았어도 구간으로 둡니다. 토론 앞(계획)과 뒤(합성·발언 다시 실행)는 바뀐 파일이 있을 때만 둡니다.
    토론이 끝나기 전에 턴이 멈췄으면 마지막 라운드는 턴 끝 스냅샷까지입니다.
    """
    found = _repo(workspace)
    if found is None:
        return []
    git, root = found
    proc = _git(git, root, "for-each-ref", "--format=%(refname) %(objectname)", f"{REF_ROOT}/{turn_id}")
    if proc.returncode != 0:
        return []
    marks: List[Tuple[int, str, str]] = []
    debate_end: Optional[str] = None
    for line in _text(proc).splitlines():
        name, _, sha = line.rpartition(" ")
        short = name.rsplit("/", 1)[-1]
        boundary = _BOUNDARY.match(short)
        if boundary:
            marks.append((int(boundary[2]), boundary[1], sha))
        elif short == DEBATE_END:
            debate_end = sha
    if not marks:
        return []
    marks.sort()

    def segment(label: str, a: str, b: str, *, keep_empty: bool) -> List[Segment]:
        files = changes(root, a, b)
        return [Segment(label, a, b, files)] if files is not None and (files or keep_empty) else []

    out = segment("계획 단계", start, marks[0][2], keep_empty=False)
    for index, (number, kind, sha) in enumerate(marks):
        following = marks[index + 1][2] if index + 1 < len(marks) else (debate_end or end)
        label = f"라운드 {number}" if kind == "round" else f"{number}단계"
        out += segment(label, sha, following, keep_empty=True)
    if debate_end:
        out += segment("토론 이후", debate_end, end, keep_empty=False)
    return out


def revert(workspace: str | Path, start: str, end: str) -> RevertResult:
    """`start`→`end` 사이에 바뀐 파일을 `start` 의 모습으로 돌립니다.

    그 파일이 `end` 뒤에 또 바뀌었으면 아무것도 하지 않고 그 목록을 돌려줍니다 — 나중의 변경을 말없이
    덮어쓰면 되돌리기가 새 사고가 됩니다.
    """
    found = _repo(workspace)
    if found is None:
        return RevertResult(ok=False, error="작업 공간이 Git 저장소가 아니거나 Git 명령어를 찾을 수 없습니다.")
    git, root = found
    turn_changes = changes(root, start, end)
    if turn_changes is None:
        return RevertResult(ok=False, error="해당 턴의 체크포인트 스냅샷을 읽을 수 없습니다.")
    if not turn_changes:
        return RevertResult(ok=True)

    now = snapshot(root, None)
    if now is None:
        return RevertResult(ok=False, error="현재 작업 공간의 상태를 읽을 수 없습니다.")
    later = changes(root, end, now)
    if later is None:
        return RevertResult(ok=False, error="턴 종료 이후의 파일 변경 내역을 확인하지 못했습니다.")
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
            return RevertResult(ok=False, error=f"파일 복원에 실패했습니다: {_error(proc)}")
    for change in turn_changes:
        if change.status != "A":
            continue
        target = (root / change.path).resolve()
        if root not in target.parents:
            continue  # 저장소 밖을 가리키는 이름은 건드리지 않습니다
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            return RevertResult(ok=False, reverted=[], error=f"`{change.path}` 파일 삭제에 실패했습니다: {exc}")
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
    # 시작·끝 말고 라운드 경계도 있으므로 턴 아래의 이름을 저장소에서 읽어 옵니다.
    wanted = set(turn_ids)
    listed = _git(git, root, "for-each-ref", "--format=%(refname)", REF_ROOT)
    names = [n for n in _text(listed).splitlines() if n[len(REF_ROOT) + 1:].split("/")[0] in wanted]
    if not names:
        return
    lines = "".join(f"delete {name}\n" for name in names)
    proc = _git(git, root, "update-ref", "--stdin", stdin=lines.encode("utf-8"))
    if proc.returncode != 0:
        # 그 사이 사라진 이름이 하나라도 있으면 통째로 실패합니다. 하나씩 지웁니다.
        for name in names:
            _git(git, root, "update-ref", "-d", name)


def expired_turns(
    workspace: str | Path, *, keep_turns: int = KEEP_TURNS, keep_days: float = KEEP_DAYS,
) -> List[str]:
    """보존 기준을 넘긴 턴의 id. 최근 `keep_turns` 턴과 `keep_days` 일 안의 턴은 빼고, 오래된 턴부터 돌려줍니다.

    저장소의 이름만 읽습니다. 턴의 시각은 스냅샷 커밋의 시각 중 가장 늦은 것입니다.
    """
    found = _repo(workspace)
    if found is None:
        return []
    git, root = found
    proc = _git(git, root, "for-each-ref", "--format=%(committerdate:unix) %(refname)", REF_ROOT)
    if proc.returncode != 0:
        logger.warning(f"Could not list workspace checkpoints in {root}: {_error(proc)}")
        return []
    latest: Dict[str, int] = {}
    for line in _text(proc).splitlines():
        stamp, _, name = line.partition(" ")
        turn_id = name[len(REF_ROOT) + 1:].split("/")[0]
        if stamp.isdigit() and turn_id:
            latest[turn_id] = max(latest.get(turn_id, 0), int(stamp))
    cutoff = time.time() - keep_days * 86400
    newest_first = sorted(latest, key=lambda turn_id: latest[turn_id], reverse=True)
    return [turn_id for turn_id in reversed(newest_first[keep_turns:]) if latest[turn_id] < cutoff]


# 돌고 있는 `git gc`. 서버가 끝날 때 끊을 수 있게 들고 있습니다 (`stop_collecting`).
_collecting: Set[subprocess.Popen] = set()


def collect_garbage(workspace: str | Path) -> None:
    """`git gc --auto` 를 돌립니다. 쌓인 것이 기준(`gc.auto`, 기본 6,700개) 미만이면 바로 끝납니다.

    이름을 잃은 스냅샷의 객체는 git 의 유예 기간(`gc.pruneExpire`, 기본 2주)이 지난 뒤에 지워집니다. 사용자의
    gc 설정을 그대로 따르고, 스스로 백그라운드로 나가지 않게만 합니다. 나가면 끝을 기다릴 수 없고 실패가 로그에 남지 않습니다.
    출력을 파이프로 받지 않는 이유: gc 를 끊어도 자식 프로세스(`git repack`)가 파이프를 쥐고 있어 기다림이 풀리지 않습니다.
    """
    found = _repo(workspace)
    if found is None:
        return
    git, root = found
    proc = subprocess.Popen(
        [git, "-C", str(root), "-c", "gc.autoDetach=false", "gc", "--auto", "--quiet"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _collecting.add(proc)
    try:
        code = proc.wait(timeout=GC_TIMEOUT)
        if code != 0:
            logger.warning(f"git gc --auto exited with {code} in {root}")
    except subprocess.TimeoutExpired:
        logger.warning(f"git gc --auto did not finish in {GC_TIMEOUT}s in {root}; stopping it")
        proc.kill()
        proc.wait()
    finally:
        _collecting.discard(proc)


def stop_collecting() -> None:
    """돌고 있는 `git gc` 를 끊습니다. 스레드에서 도는 gc 가 서버 종료를 붙잡지 않게 합니다.

    끊어도 저장소는 망가지지 않습니다. git 은 새 묶음을 다 쓴 뒤에 이름을 바꿔 넣습니다.
    """
    for proc in list(_collecting):
        proc.kill()
