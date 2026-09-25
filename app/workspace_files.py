"""작업 공간 파일을 @언급으로 가리키고, 화면에서 올린 파일을 작업 공간에 넣습니다.

**내용이 아니라 경로만 전달합니다.** 사용자 메시지는 모든 발언자의 전사와 최종 합성에
라운드마다 복사됩니다. 파일 내용을 거기 붙이면 합성이 코드를 다시 출력하던 때와 같은
컨텍스트 포화가 입력 쪽에서 되살아납니다. 경로를 받은 에이전트가 필요할 때 파일 도구
(filesystem, office 등)로 필요한 만큼만 읽습니다.

그래서 파일 종류를 가리지 않습니다. PDF·오피스 문서도 목록에 나오고 언급할 수 있습니다 —
읽는 것은 그 형식을 다루는 MCP 서버의 몫입니다.

안전장치:

* **작업 공간 밖으로 나가지 않습니다.** 절대 경로, 드라이브 문자, `..` 로 벗어나는 경로,
  바깥을 가리키는 심볼릭 링크는 언급으로도 업로드로도 받지 않습니다.
* **무거운 폴더는 훑지 않습니다.** `.git`, `node_modules`, 가상환경 등과 작업 공간 최상위
  `.gitignore` 의 단순한 규칙을 따르고, 항목 수에 상한을 둡니다.
* **목록 전체를 브라우저로 보내지 않습니다.** 친 글자로 서버에서 걸러 상위 몇 개만 돌려줍니다.
* **목록은 몇 초만 기억합니다.** 토론 중에 에이전트가 파일을 만들고, 사용자가 올리기도
  하므로 오래 기억하면 새 파일이 보이지 않습니다. 업로드 직후에는 바로 비웁니다.
* **올린 파일은 덮어쓰지 않습니다.** 같은 이름이 있으면 `이름 (2).확장자` 로 바꿉니다.
* **코드 블록 안의 `@` 는 언급이 아닙니다.** 붙여 넣은 코드의 `@app.get`, `@Override` 가
  파일이나 에이전트로 해석되면 안 됩니다.
"""

from __future__ import annotations

import fnmatch
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 훑지 않는 폴더. 이름이 같으면 어느 깊이에 있든 건너뜁니다.
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".next", ".nuxt",
    "dist", "build", ".memory-graphs", ".ipynb_checkpoints", "site-packages",
    # 런타임이 쓰는 임시 폴더(app/mcp/pool.py). 사용자가 만든 파일이 아니므로
    # @멘션 목록과 다운로드 창에 보이면 안 됩니다.
    ".mado",
    # PairSlide(발표자료 서버)가 진행 중인 문서를 두는 곳. 작업 공간을 함께 쓰면
    # 여기에 JSON 이 쌓이는데, 사람이 받을 파일은 내보낸 .pptx 쪽입니다.
    ".pairslide",
})

# 한 작업 공간에서 목록에 올리는 최대 항목 수(파일+폴더). 넘으면 거기서 멈추고
# `truncated` 로 알립니다.
MAX_SCAN_ENTRIES = 20_000

# 목록을 기억하는 시간(초).
SCAN_TTL_SECONDS = 5.0

# 언급 창에 보여 줄 최대 후보 수.
MAX_SUGGESTIONS = 30

# 화면에서 올릴 수 있는 파일 하나의 최대 크기.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# 올린 파일이 놓이는 작업 공간 안의 폴더.
UPLOAD_SUBDIR = "uploads"

# 이보다 큰 파일은 참조 블록에 "필요한 부분만 읽으라" 고 붙입니다.
LARGE_FILE_BYTES = 1 * 1024 * 1024

# 참조 블록의 머리. 이 줄부터 끝까지가 앱이 붙인 부분입니다. 긴급 종료로 되돌아온 글을
# 다시 보내면 같은 블록이 두 번 붙지 않도록, 펼치기 전에 이 뒤를 떼어 냅니다.
REFERENCE_MARKER = "[@참조]"


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------


class WorkspacePathError(ValueError):
    """작업 공간 밖을 가리키거나 쓸 수 없는 경로."""


def safe_workspace_path(root: Path, relative: str) -> Path:
    """`relative` 를 작업 공간 안의 절대 경로로 풉니다. 밖이면 `WorkspacePathError`.

    `Path.resolve()` 로 심볼릭 링크까지 따라간 뒤 비교합니다. 이름만 보고 판단하면
    작업 공간 안의 링크가 바깥을 가리키는 경우를 놓칩니다.
    """
    text = (relative or "").strip().replace("\\", "/")
    if not text:
        raise WorkspacePathError("경로가 비어 있습니다")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise WorkspacePathError(f"절대 경로는 쓸 수 없습니다: {relative}")
    if any(part == ".." for part in PurePosixPath(text).parts):
        raise WorkspacePathError(f"작업 공간 밖을 가리킵니다: {relative}")
    root_resolved = Path(root).resolve()
    target = (root_resolved / text).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        raise WorkspacePathError(f"작업 공간 밖을 가리킵니다: {relative}") from None
    return target


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# 목록
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str        # 작업 공간 기준 상대 경로, `/` 구분. 폴더는 끝에 `/` 가 붙지 않습니다.
    is_dir: bool
    size: int = 0
    mtime: float = 0.0   # 파일의 마지막 수정 시각(epoch 초). 다운로드 목록을 최근 것부터 보여 줍니다.

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass
class WorkspaceScan:
    root: Path
    entries: List[WorkspaceEntry] = field(default_factory=list)
    truncated: bool = False
    scanned_at: float = 0.0


def _gitignore_rules(root: Path) -> List[Tuple[str, bool, bool]]:
    """작업 공간 최상위 `.gitignore` 의 **단순한** 규칙: (패턴, 폴더 전용, 경로 기준).

    부정(`!`)은 따르지 않습니다 — 목록에서 빠질 것이 나오는 쪽으로만 틀립니다. 하위
    폴더의 `.gitignore` 도 읽지 않습니다. git 의 규칙을 전부 구현하는 것이 목적이 아니라,
    빌드 산출물 폴더가 목록을 채우지 않게 하려는 것입니다.
    """
    path = root / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rules = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        dir_only = line.endswith("/")
        line = line.strip("/")
        if not line:
            continue
        rules.append((line, dir_only, "/" in line))
    return rules


def _ignored(rel: str, name: str, is_dir: bool, rules: Sequence[Tuple[str, bool, bool]]) -> bool:
    for pattern, dir_only, anchored in rules:
        if dir_only and not is_dir:
            continue
        target = rel if anchored else name
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def scan_workspace(root: Path, max_entries: int = MAX_SCAN_ENTRIES) -> WorkspaceScan:
    """작업 공간의 파일·폴더 목록. 없는 폴더면 빈 목록입니다."""
    root = Path(root)
    scan = WorkspaceScan(root=root, scanned_at=time.monotonic())
    if not root.is_dir():
        return scan
    root_resolved = root.resolve()
    rules = _gitignore_rules(root)

    for current, dirs, files in os.walk(root_resolved, followlinks=False):
        base = Path(current)
        rel_base = base.relative_to(root_resolved).as_posix()
        rel_base = "" if rel_base == "." else rel_base

        kept_dirs = []
        for d in sorted(dirs, key=str.lower):
            rel = f"{rel_base}/{d}" if rel_base else d
            if d in EXCLUDED_DIRS or _ignored(rel, d, True, rules):
                continue
            kept_dirs.append(d)
            scan.entries.append(WorkspaceEntry(rel, True))
            if len(scan.entries) >= max_entries:
                scan.truncated = True
                return scan
        dirs[:] = kept_dirs

        for f in sorted(files, key=str.lower):
            rel = f"{rel_base}/{f}" if rel_base else f
            if _ignored(rel, f, False, rules):
                continue
            try:
                st = (base / f).stat()
            except OSError:
                continue
            scan.entries.append(WorkspaceEntry(rel, False, st.st_size, st.st_mtime))
            if len(scan.entries) >= max_entries:
                scan.truncated = True
                return scan
    return scan


class WorkspaceIndex:
    """작업 공간별 목록을 잠깐 기억합니다. 여러 화면이 함께 씁니다."""

    def __init__(self, ttl: float = SCAN_TTL_SECONDS):
        self.ttl = ttl
        self._scans: Dict[str, WorkspaceScan] = {}
        self._lock = threading.Lock()

    def get(self, root: Path) -> WorkspaceScan:
        key = str(Path(root).resolve())
        with self._lock:
            cached = self._scans.get(key)
        if cached is not None and time.monotonic() - cached.scanned_at < self.ttl:
            return cached
        scan = scan_workspace(Path(root))
        with self._lock:
            self._scans[key] = scan
        return scan

    def invalidate(self, root: Optional[Path] = None) -> None:
        with self._lock:
            if root is None:
                self._scans.clear()
            else:
                self._scans.pop(str(Path(root).resolve()), None)


_index = WorkspaceIndex()


def get_workspace_index() -> WorkspaceIndex:
    return _index


def _score(entry: WorkspaceEntry, query: str) -> Optional[Tuple[int, int, str]]:
    """작을수록 앞. None 이면 맞지 않음."""
    if not query:
        # 아무것도 안 쳤으면 얕은 것부터.
        return (entry.path.count("/"), 0 if entry.is_dir else 1, entry.path.lower())
    q = query.lower()
    name = entry.name.lower()
    path = entry.path.lower()
    if name.startswith(q):
        rank = 0
    elif q in name:
        rank = 1
    elif path.startswith(q):
        rank = 2
    elif q in path:
        rank = 3
    else:
        it = iter(path)
        if not all(ch in it for ch in q):
            return None
        rank = 4
    return (rank, len(entry.path), path)


def search_entries(
    entries: Iterable[WorkspaceEntry], query: str, limit: int = MAX_SUGGESTIONS
) -> List[WorkspaceEntry]:
    scored = []
    for entry in entries:
        s = _score(entry, query.strip().replace("\\", "/"))
        if s is not None:
            scored.append((s, entry))
    scored.sort(key=lambda pair: pair[0])
    return [entry for _, entry in scored[:limit]]


# ---------------------------------------------------------------------------
# 언급
# ---------------------------------------------------------------------------


def mention_token(value: str) -> str:
    """입력창에 넣을 `@...`. 공백이나 따옴표가 있으면 큰따옴표로 감쌉니다."""
    text = value.replace('"', "")
    return f'@"{text}"' if re.search(r"\s", text) else f"@{text}"


@dataclass(frozen=True)
class MentionAgent:
    key: str
    name: str
    role: str
    active: bool = True


@dataclass
class MentionSuggestion:
    kind: str          # "file" | "dir" | "agent"
    label: str
    detail: str
    insert: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "label": self.label, "detail": self.detail, "insert": self.insert}


def suggest_mentions(
    query: str,
    entries: Iterable[WorkspaceEntry],
    agents: Sequence[MentionAgent],
    limit: int = MAX_SUGGESTIONS,
) -> List[MentionSuggestion]:
    """언급 창의 후보. 이번 토론에 참여하는 전문가를 먼저, 그다음 파일·폴더."""
    q = (query or "").strip().lower()
    out: List[MentionSuggestion] = []
    for agent in agents:
        if not agent.active:
            continue
        if q and q not in agent.name.lower() and q not in agent.key.lower() and q not in agent.role.lower():
            continue
        out.append(MentionSuggestion("agent", agent.name, agent.role, mention_token(agent.name)))
    for entry in search_entries(entries, query, limit=max(limit - len(out), 0)):
        if entry.is_dir:
            out.append(MentionSuggestion("dir", entry.path + "/", "폴더", mention_token(entry.path + "/")))
        else:
            out.append(MentionSuggestion("file", entry.path, format_size(entry.size), mention_token(entry.path)))
    return out[:limit]


# `@"따옴표 안"` 또는 `@공백없는조각`. 앞은 글의 처음이거나 공백·여는 괄호여야 합니다 —
# 그래야 `user@example.com` 이 언급이 되지 않습니다.
_MENTION = re.compile(r'(?:^|(?<=[\s(\[{]))@(?:"([^"\n]+)"|([^\s"@]+))', re.M)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TRAILING_PUNCT = ",.;:!?)]}'"
# 코드 밖에서 해석하지 못했을 때 경고할 만큼 경로처럼 보이는가.
_PATHLIKE = re.compile(r"/|\.[A-Za-z0-9]{1,6}$")


@dataclass
class MentionReport:
    files: List[Tuple[str, int]] = field(default_factory=list)       # (경로, 크기)
    dirs: List[str] = field(default_factory=list)
    agents: List[MentionAgent] = field(default_factory=list)
    inactive_agents: List[MentionAgent] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)                  # 경로처럼 보이지만 없음
    rejected: List[str] = field(default_factory=list)                 # 작업 공간 밖

    @property
    def has_references(self) -> bool:
        return bool(self.files or self.dirs or self.agents)

    def warnings(self) -> List[str]:
        out = []
        if self.missing:
            out.append("작업 공간에서 찾지 못해 참조에서 뺐습니다: " + ", ".join(self.missing))
        if self.rejected:
            out.append("작업 공간 밖이라 참조에서 뺐습니다: " + ", ".join(self.rejected))
        if self.inactive_agents:
            out.append(
                "이번 토론에 참여하지 않는 전문가라 지목에서 뺐습니다: "
                + ", ".join(a.name for a in self.inactive_agents)
            )
        return out


def strip_reference_block(text: str) -> str:
    """앱이 붙인 참조 블록을 떼어 냅니다 (없으면 그대로)."""
    idx = text.rfind(REFERENCE_MARKER)
    if idx == -1:
        return text
    return text[:idx].rstrip()


def _code_spans(text: str) -> List[Tuple[int, int]]:
    spans = [m.span() for m in _FENCE.finditer(text)]
    for m in _INLINE_CODE.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return spans


def _match_agent(token: str, agents: Sequence[MentionAgent]) -> Optional[MentionAgent]:
    t = token.strip().lower()
    for agent in agents:
        if t in (agent.key.lower(), agent.name.lower()):
            return agent
    return None


def resolve_mentions(
    text: str, root: Path, agents: Sequence[MentionAgent]
) -> MentionReport:
    """글 속 `@...` 를 전문가·파일·폴더로 해석합니다. 코드 블록 안은 보지 않습니다."""
    report = MentionReport()
    spans = _code_spans(text)
    seen = set()
    root = Path(root)

    for m in _MENTION.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        quoted, bare = m.group(1), m.group(2)
        candidates = [quoted] if quoted is not None else [bare, bare.rstrip(_TRAILING_PUNCT)]
        candidates = [c for c in dict.fromkeys(candidates) if c]
        resolved = False

        for token in candidates:
            agent = _match_agent(token, agents)
            if agent is not None:
                bucket = report.agents if agent.active else report.inactive_agents
                if ("agent", agent.key) not in seen:
                    seen.add(("agent", agent.key))
                    bucket.append(agent)
                resolved = True
                break

            try:
                target = safe_workspace_path(root, token)
            except WorkspacePathError:
                if "/" in token or "\\" in token or ".." in token:
                    if token not in report.rejected:
                        report.rejected.append(token)
                    resolved = True
                    break
                continue
            if not target.exists():
                continue
            rel = target.relative_to(root.resolve()).as_posix()
            if ("path", rel) in seen:
                resolved = True
                break
            seen.add(("path", rel))
            if target.is_dir():
                report.dirs.append(rel)
            else:
                try:
                    size = target.stat().st_size
                except OSError:
                    size = 0
                report.files.append((rel, size))
            resolved = True
            break

        if not resolved:
            token = candidates[-1] if candidates else ""
            if token and _PATHLIKE.search(token) and token not in report.missing:
                report.missing.append(token)
    return report


def build_reference_block(report: MentionReport, root: Path) -> str:
    """메시지 끝에 붙일 참조 블록. 참조가 없으면 빈 문자열."""
    if not report.has_references:
        return ""
    parts = [REFERENCE_MARKER]
    if report.files or report.dirs:
        parts.append(f"참조 파일 — 작업 공간: {Path(root).resolve()}")
        for rel, size in report.files:
            note = " · 큼, 필요한 부분만 읽으세요" if size >= LARGE_FILE_BYTES else ""
            parts.append(f"- {rel} ({format_size(size)}{note})")
        for rel in report.dirs:
            parts.append(f"- {rel}/ (폴더)")
        parts.append(
            "파일 내용은 붙이지 않았습니다. 필요하면 그 형식을 다루는 파일 도구로 이 경로를 "
            "직접 읽으세요."
        )
    if report.agents:
        if len(parts) > 1:
            parts.append("")
        parts.append("지목한 전문가")
        for agent in report.agents:
            parts.append(f"- {agent.name} ({agent.role})")
        parts.append(
            "유저가 이 전문가를 직접 불렀습니다. 요청 중 이 전문가에게 해당하는 부분은 "
            "이 전문가가 맡아 답하게 하세요."
        )
    return "\n".join(parts)


def expand_mentions(
    text: str, root: Path, agents: Sequence[MentionAgent]
) -> Tuple[str, MentionReport]:
    """보낼 글에 참조 블록을 붙입니다. 이미 붙어 있던 블록은 새로 만듭니다."""
    body = strip_reference_block(text)
    report = resolve_mentions(body, root, agents)
    block = build_reference_block(report, root)
    return (f"{body}\n\n{block}" if block else body), report


# ---------------------------------------------------------------------------
# 업로드
# ---------------------------------------------------------------------------

_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)


def sanitize_upload_name(filename: str) -> str:
    """올린 파일 이름에서 경로와 쓸 수 없는 글자를 걷어 냅니다."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME_CHARS.sub("_", name).strip().strip(".")
    if not name or name in {".", ".."}:
        name = "upload"
    if _RESERVED_WINDOWS.match(name):
        name = f"_{name}"
    return name[:200]


def unique_upload_target(directory: Path, name: str) -> Path:
    """같은 이름이 있으면 `이름 (2).확장자` 로 비켜 갑니다. 덮어쓰지 않습니다."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    n = 2
    while True:
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def store_workspace_upload(root: Path, filename: str, content: bytes) -> str:
    """올린 파일을 `<작업 공간>/uploads/` 에 저장하고 작업 공간 기준 경로를 돌려줍니다."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise WorkspacePathError(
            f"파일이 너무 큽니다 ({format_size(len(content))}, 최대 {format_size(MAX_UPLOAD_BYTES)})"
        )
    directory = safe_workspace_path(Path(root), UPLOAD_SUBDIR)
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_upload_target(directory, sanitize_upload_name(filename))
    # 'xb' — 이름을 고른 뒤 다른 업로드가 같은 이름을 먼저 차지했으면 덮어쓰지 않고 실패합니다.
    with open(target, "xb") as fh:
        fh.write(content)
    get_workspace_index().invalidate(Path(root))
    return target.relative_to(Path(root).resolve()).as_posix()


def agents_for_mentions(roster: Iterable[Any], active_keys: Iterable[str]) -> List[MentionAgent]:
    """로스터의 전문가를 언급 후보로. 오케스트레이터는 지목 대상이 아닙니다."""
    active = set(active_keys)
    return [
        MentionAgent(a.key, a.name, a.role, a.key in active)
        for a in roster
        if a.key != "orchestrator"
    ]


# ---------------------------------------------------------------------------
# 다운로드
# ---------------------------------------------------------------------------

# 한 번에 묶을 수 있는 파일 수와 (압축 전) 총 크기. 서버가 거대한 압축을 만들다 디스크와
# 시간을 다 쓰지 않게 합니다. 넘으면 만들지 않고 이유를 알립니다.
MAX_ZIP_FILES = 5_000
MAX_ZIP_BYTES = 1024 * 1024 * 1024

# 압축 파일을 잠시 두는 곳. 작업 공간 밖(앱 소유 `data/`)이라 에이전트 목록에 섞이지 않습니다.
DOWNLOAD_TTL_SECONDS = 60 * 60


class WorkspaceDownloadError(ValueError):
    """다운로드를 만들 수 없는 요청 (빈 선택, 한도 초과, 작업 공간 밖 등)."""


def download_dir() -> Path:
    from app.config import DATA_DIR

    path = DATA_DIR / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def purge_old_downloads(directory: Optional[Path] = None, ttl: float = DOWNLOAD_TTL_SECONDS) -> int:
    """오래된 압축 파일을 지웁니다. 지운 개수. 받는 중인 파일을 지우지 않도록 넉넉히 둡니다."""
    directory = directory or download_dir()
    removed = 0
    now = time.time()
    for item in directory.glob("*.zip"):
        try:
            if now - item.stat().st_mtime > ttl:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@dataclass
class DownloadPlan:
    files: List[Tuple[Path, str]] = field(default_factory=list)   # (실제 경로, 압축 안 이름)
    total_bytes: int = 0
    rejected: List[str] = field(default_factory=list)             # 작업 공간 밖이거나 없는 것


def plan_download(root: Path, rel_paths: Iterable[str]) -> DownloadPlan:
    """고른 경로를 검증해 실제로 내려줄 파일 목록을 만듭니다.

    화면이 보낸 경로를 그대로 믿지 않습니다. 하나하나 작업 공간 안으로 풀고(링크 포함),
    폴더면 목록 규칙(`scan_workspace`)대로 안의 파일을 담습니다. 같은 파일은 한 번만.
    """
    plan = DownloadPlan()
    root = Path(root)
    root_resolved = root.resolve()
    seen = set()
    for rel in rel_paths:
        rel = (rel or "").strip().rstrip("/")
        try:
            target = safe_workspace_path(root, rel)
        except WorkspacePathError:
            plan.rejected.append(rel)
            continue
        if target.is_dir():
            prefix = target.relative_to(root_resolved).as_posix()
            candidates = [
                e.path for e in scan_workspace(root).entries
                if not e.is_dir and (e.path == prefix or e.path.startswith(prefix + "/"))
            ]
        elif target.is_file():
            candidates = [target.relative_to(root_resolved).as_posix()]
        else:
            plan.rejected.append(rel)
            continue
        for candidate in candidates:
            try:
                path = safe_workspace_path(root, candidate)
            except WorkspacePathError:
                plan.rejected.append(candidate)
                continue
            key = str(path)
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            try:
                size = path.stat().st_size
            except OSError:
                plan.rejected.append(candidate)
                continue
            plan.files.append((path, path.relative_to(root_resolved).as_posix()))
            plan.total_bytes += size
    return plan


def build_workspace_zip(
    root: Path,
    rel_paths: Iterable[str],
    *,
    directory: Optional[Path] = None,
    max_files: int = MAX_ZIP_FILES,
    max_bytes: int = MAX_ZIP_BYTES,
) -> Tuple[Path, DownloadPlan, List[str]]:
    """고른 파일을 압축합니다. (압축 파일, 계획, 읽지 못해 뺀 파일).

    압축 안의 이름은 작업 공간 기준 상대 경로라, 풀면 폴더 구조가 그대로 나옵니다. 한글
    이름은 zip 의 UTF-8 표시로 저장됩니다. 쓰는 동안 에이전트가 파일을 바꾸거나 지우면 그
    파일만 빼고 계속합니다.
    """
    import zipfile

    plan = plan_download(root, rel_paths)
    if not plan.files:
        raise WorkspaceDownloadError("내려받을 파일이 없습니다")
    if len(plan.files) > max_files:
        raise WorkspaceDownloadError(f"파일이 너무 많습니다 ({len(plan.files)}개, 최대 {max_files}개)")
    if plan.total_bytes > max_bytes:
        raise WorkspaceDownloadError(
            f"합계가 너무 큽니다 ({format_size(plan.total_bytes)}, 최대 {format_size(max_bytes)})"
        )

    directory = directory or download_dir()
    purge_old_downloads(directory)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = sanitize_upload_name(Path(root).resolve().name or "workspace")
    target = unique_upload_target(directory, f"{name}-{stamp}.zip")
    skipped: List[str] = []
    try:
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for path, arcname in plan.files:
                try:
                    zf.write(path, arcname)
                except OSError:
                    skipped.append(arcname)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target, plan, skipped
