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
* 보존 및 가비지 컬렉션 (ADR-044) — 누적된 스냅샷 관리를 위해 유휴 작업 공간에 대해 보존 정책 만료 턴의 Git ref를 삭제하고(`expired_turns`, `forget`)
  `git gc --auto`를 실행합니다(`collect_garbage`). 호출 진입점은 `app/session_ops.maintain_workspace`입니다.

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

# 턴 내부 라운드 경계 스냅샷 (ADR-043): 라운드(`round-N`)·그래프 단계(`step-N`) 개시 시점 및 토론 종료 시점.
# 롤백 및 세션 분기는 불변 턴 스냅샷을 사용하며, 변경 내역을 라운드별로 세분화하여 조회할 때만 참조합니다.
DEBATE_END = "debate-end"
_BOUNDARY = re.compile(r"^(round|step)-(\d+)$")

# 스냅샷 제외 대상 디렉터리 (`dist`, `build`는 사용자 산출물일 수 있으므로 보존)
SNAPSHOT_EXCLUDED_DIRS = sorted(EXCLUDED_DIRS - {"dist", "build", ".git", ".hg", ".svn"})

# Diff 텍스트 최대 글자 수 제한 (대용량 파일 Diff로 인한 UI 프리징 방지)
MAX_DIFF_CHARS = 200_000

# Git 단일 명령어 타임아웃 (초 단위)
GIT_TIMEOUT = 120

# `git gc --auto` 타임아웃 (초 단위). 임계치 도달 시 리팩(repack)이 발생하므로 일반 명령어 대비 여유 있게 설정합니다.
GC_TIMEOUT = 900

# 체크포인트 보존 정책 (작업 공간별). 최근 턴 수 또는 최근 경과일 기준을 충족하는 스냅샷을 보존하며, 두 기준을 모두 초과한 턴만 정리합니다.
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
    """턴 내부의 단일 구간: `start` 스냅샷부터 `end` 스냅샷까지 변경된 파일 목록."""

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
        # 임시 인덱스 생성 시 타임스탬프(mtime)를 보존 복사합니다(`copy2`). Git은 인덱스 타임스탬프보다 늦거나
        # 동일한 시각에 수정된 파일만 재검사하므로, mtime이 현재 시각으로 갱신되면 동일 초/동일 크기 파일 수정을 누락하는 결함을 방지합니다.
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
    """턴을 라운드(적응형 그래프는 단계)별 세부 구간으로 분할합니다. 경계 스냅샷이 없는 턴은 빈 목록을 반환합니다.

    각 라운드는 파일 변경 여부와 관계없이 구간을 유지하며, 토론 이전(계획 단계)과 이후(결론 합성 및 발언 재생성)는 파일 변경이 발생한 경우에만 구간에 포함합니다.
    토론 완료 전 중단된 턴의 경우 마지막 라운드 구간은 턴 종료(`end`) 스냅샷까지로 설정됩니다.
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
    # start/end 외에 라운드 경계 스냅샷도 포함되므로 턴 네임스페이스 하위의 모든 ref를 조회합니다.
    wanted = set(turn_ids)
    listed = _git(git, root, "for-each-ref", "--format=%(refname)", REF_ROOT)
    names = [n for n in _text(listed).splitlines() if n[len(REF_ROOT) + 1:].split("/")[0] in wanted]
    if not names:
        return
    lines = "".join(f"delete {name}\n" for name in names)
    proc = _git(git, root, "update-ref", "--stdin", stdin=lines.encode("utf-8"))
    if proc.returncode != 0:
        # 일괄 삭제 실패 시 개별 ref 단위로 안전하게 삭제를 재시도합니다.
        for name in names:
            _git(git, root, "update-ref", "-d", name)


def expired_turns(
    workspace: str | Path, *, keep_turns: int = KEEP_TURNS, keep_days: float = KEEP_DAYS,
) -> List[str]:
    """보존 정책을 초과하여 만료된 턴 ID 목록을 반환합니다. 최근 `keep_turns` 턴 및 최근 `keep_days` 일 이내의 턴은 제외하며, 오래된 순으로 정렬합니다.

    Git ref 커밋 타임스탬프 중 최신값을 해당 턴의 기준 시각으로 판별합니다.
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
    """`git gc --auto`를 실행하여 저장소의 불필요한 객체를 정리하고 압축합니다. 임계치(기본 느슨한 객체 6,700개) 미만인 경우 즉시 종료됩니다.

    참조가 해제된 고아 객체는 Git의 기본 만료 유예 기간(`gc.pruneExpire`, 기본 2주)이 경과한 후 안전하게 회수됩니다. 사용자 저장소의
    gc 설정을 준수하며, `gc.autoDetach=false`를 통해 백그라운드 분리 없이 동기적으로 완료를 대기합니다.
    출력 파이프를 열지 않는 이유: 프로세스 강제 종료 시 자식 프로세스(`git repack`)가 파이프 핸들을 점유하여 대기가 블로킹되는 현상을 방지합니다.
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
    """실행 중인 `git gc` 서브프로세스를 중단합니다. 백그라운드 gc 작업이 서버 종료 절차를 지연시키지 않도록 방지합니다.

    중단하더라도 Git의 원자적 리팩(atomic rename) 메커니즘에 의해 저장소 무결성은 안전하게 보존됩니다.
    """
    for proc in list(_collecting):
        proc.kill()
