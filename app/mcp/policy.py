"""도구 호출을 **허용 · 묻기 · 거부** 로 판정합니다.

Claude Code · Zed · Antigravity 의 도구 권한에서 가져온 것들입니다.

* **판정 순서** — 고정 보호 → deny → ask → allow → 모드 기본값. 먼저 걸린 쪽이
  이깁니다. 구체적인 allow 규칙도 넓은 deny 규칙을 뚫지 못합니다 (세 제품 공통).
* **도구가 아니라 행위에 규칙을 겁니다** (Antigravity 의 `action(target)`). 호출은
  `read(경로)` · `write(경로)` · `delete(경로)` · `exec(코드)` · `net(호스트)` 로 바뀐 뒤
  판정됩니다. 그래야 filesystem 에서 막은 `.env` 를 sandbox 의 `open('.env')` 로 읽는
  우회가 같은 규칙 한 줄에 걸립니다 (Antigravity 가 `cat .env` 로 뚫린 사례).
* **모드** — 위험 등급마다 기본 판정이 정해진 묶음 (`MODE_DEFAULTS`).
* **"이 대화에서 허용"·"항상 허용"** 은 allow 규칙을 더하는 것입니다. 그래서 ask 규칙과
  deny 규칙은 그 뒤에도 그대로 이깁니다 (Claude Code 와 같은 뜻).

세 제품 모두 MCP 도구는 도구 단위로만 규칙을 겁니다. MADO 는 도구가 전부 MCP 이고
기본 서버(filesystem · git · memory · sandbox · fetch · PairSlide)의 인자 모양을 알므로,
그 서버들은 인자 단위까지 봅니다 (`KNOWN_TOOLS`). 모르는 서버는 도구 단위입니다.

이 모듈은 순수합니다 — 판정에 필요한 것은 전부 인자로 받습니다. 사람에게 묻고
기록하는 쪽은 `app/orchestration/tool_gate.py` 입니다.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from app.mcp.exec_scan import describe as describe_scan
from app.mcp.exec_scan import iter_mentions, scan_python, url_host
from app.mcp.guards import binary_write_refusal

# ---------------------------------------------------------------------------
# 모드와 위험 등급
# ---------------------------------------------------------------------------

ALLOW, ASK, DENY = "allow", "ask", "deny"
EFFECT_RANK = {ALLOW: 0, ASK: 1, DENY: 2}

MODES = ("read_only", "default", "review", "auto")
DEFAULT_MODE = "default"
MODE_LABELS = {
    "read_only": "읽기 전용",
    "default": "기본",
    "review": "검토",
    "auto": "자동",
}
MODE_DESCRIPTIONS = {
    "read_only": "읽기만 합니다. 쓰기·실행·네트워크는 묻지 않고 거부합니다.",
    "default": "작업 공간 안의 쓰기와 검사를 통과한 코드 실행은 묻지 않습니다. "
               "삭제·작업 공간 밖·네트워크·검사에 걸린 코드는 묻습니다.",
    "review": "읽기 말고는 전부 묻습니다.",
    "auto": "고정 보호와 거부 규칙 말고는 묻지 않고 실행합니다. 격리된 환경에서만 쓰세요.",
}
# 둘 중 더 엄격한 모드를 고를 때의 순서. 에이전트 설정은 세션 모드를 조이기만 합니다.
_STRICTNESS = {"auto": 0, "default": 1, "review": 2, "read_only": 3}

READ, STATE, WRITE, OUTSIDE, DELETE, EXEC, EXEC_FLAGGED, NET, UNKNOWN = (
    "read", "state", "write", "outside", "delete", "exec", "exec_flagged", "net", "unknown",
)
RISK_LABELS = {
    READ: "읽기",
    STATE: "대화 상태",
    WRITE: "작업 공간 쓰기",
    OUTSIDE: "작업 공간 밖 접근",
    DELETE: "삭제",
    EXEC: "코드 실행",
    EXEC_FLAGGED: "검사에 걸린 코드 실행",
    NET: "네트워크",
    UNKNOWN: "등급을 모르는 도구",
}
# 판정 카드에 "가장 위험한 것" 하나를 보여줄 때의 순서.
_RISK_RANK = {
    READ: 0, STATE: 0, WRITE: 1, EXEC: 2, OUTSIDE: 3, DELETE: 3,
    EXEC_FLAGGED: 4, NET: 4, UNKNOWN: 4,
}

MODE_DEFAULTS: Dict[str, Dict[str, str]] = {
    "read_only": {
        READ: ALLOW, STATE: ALLOW, WRITE: DENY, OUTSIDE: DENY, DELETE: DENY,
        EXEC: DENY, EXEC_FLAGGED: DENY, NET: DENY, UNKNOWN: DENY,
    },
    # 작업 공간 쓰기를 묻지 않는 이유: 토론 중에 쓰기마다 멈추면 협업이 되지
    # 않습니다. 작업 공간은 앱이 git 저장소로 만들어 두므로 되돌릴 길이 있습니다.
    # Antigravity 의 Default 프리셋도 작업 공간 파일은 묻지 않습니다.
    "default": {
        READ: ALLOW, STATE: ALLOW, WRITE: ALLOW, OUTSIDE: ASK, DELETE: ASK,
        EXEC: ALLOW, EXEC_FLAGGED: ASK, NET: ASK, UNKNOWN: ASK,
    },
    "review": {
        READ: ALLOW, STATE: ALLOW, WRITE: ASK, OUTSIDE: ASK, DELETE: ASK,
        EXEC: ASK, EXEC_FLAGGED: ASK, NET: ASK, UNKNOWN: ASK,
    },
    "auto": {risk: ALLOW for risk in RISK_LABELS},
}


def normalize_mode(mode: Optional[str], fallback: str = DEFAULT_MODE) -> str:
    """모르는 값은 `fallback` 으로. 빈 값은 "지정 안 함" 입니다."""
    value = (mode or "").strip()
    return value if value in MODES else fallback


def stricter_mode(*modes: Optional[str]) -> str:
    """주어진 모드 중 가장 엄격한 것. 비어 있는 값은 건너뜁니다."""
    present = [m for m in modes if m in MODES]
    if not present:
        return DEFAULT_MODE
    return max(present, key=lambda m: _STRICTNESS[m])


# ---------------------------------------------------------------------------
# 규칙
# ---------------------------------------------------------------------------

PATH_KINDS = ("read", "write", "delete")
RULE_KINDS = PATH_KINDS + ("exec", "net", "mcp")
# 경로 행위의 세기. 제한(deny·ask)은 위로, 허용은 아래로 번집니다 — `read(.env)` 를
# 막으면 `.env` 쓰기도 막히고, `write(src/**)` 를 허용하면 그 읽기도 허용됩니다.
_PATH_RANK = {"read": 0, "write": 1, "delete": 2}

_RULE_RE = re.compile(r"^\s*([a-z]+)\s*(?:\((.*)\))?\s*$", re.S)
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:/")


def _glob_regex(glob: str) -> "re.Pattern[str]":
    """경로 glob → 정규식. `**` 는 폴더를 넘나들고, `*` 와 `?` 는 한 폴더 안입니다.

    대소문자를 가리지 않습니다. 윈도우 파일 시스템이 가리지 않으므로, 가리면
    `read(**/.ENV)` 가 `.env` 를 놓칩니다 (거부 규칙에서는 그 놓침이 곧 구멍입니다).
    """
    text = glob.replace("\\", "/")
    out: List[str] = []
    i = 0
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    """`kind(pattern)` 한 줄. 원문(`text`)이 곧 화면과 conf.json 에 적히는 모양입니다."""

    text: str
    kind: str
    pattern: str = ""
    regex: Optional["re.Pattern[str]"] = None
    absolute: bool = False

    @property
    def matches_all(self) -> bool:
        return self.pattern in ("", "*", "**")


def parse_rule(text: str) -> Rule:
    """규칙 한 줄을 읽습니다. 틀리면 무엇이 틀렸는지 적은 ValueError."""
    raw = str(text or "").strip()
    match = _RULE_RE.match(raw)
    if not match:
        raise ValueError(
            f"규칙 '{raw}' 을 읽을 수 없습니다. `read(경로)`, `write(경로)`, `delete(경로)`, "
            f"`exec`, `net(호스트)`, `mcp(서버/도구)` 모양으로 적으세요."
        )
    kind, pattern = match.group(1), (match.group(2) or "").strip()
    if kind not in RULE_KINDS:
        raise ValueError(f"규칙 '{raw}' 의 '{kind}' 는 없는 행위입니다 ({', '.join(RULE_KINDS)} 중 하나).")

    regex: Optional["re.Pattern[str]"] = None
    absolute = False
    if pattern.startswith("regex:"):
        if kind == "mcp":
            raise ValueError(f"규칙 '{raw}': mcp 규칙은 regex: 대신 `서버/도구` glob 을 씁니다.")
        try:
            regex = re.compile(pattern[len("regex:"):], re.IGNORECASE | re.S)
        except re.error as exc:
            raise ValueError(f"규칙 '{raw}' 의 정규식이 틀렸습니다: {exc}") from exc
    elif kind == "exec" and pattern not in ("", "*"):
        raise ValueError(
            f"규칙 '{raw}': exec 는 `exec` 또는 `exec(regex:정규식)` 으로만 씁니다 "
            f"(코드는 glob 으로 가를 수 없습니다)."
        )
    elif kind in PATH_KINDS and pattern not in ("", "*", "**"):
        normalized = os.path.expanduser(pattern).replace("\\", "/")
        absolute = normalized.startswith("/") or bool(_WINDOWS_ABS_RE.match(normalized))
        if normalized.startswith("./"):
            normalized = normalized[2:]
        regex = _glob_regex(normalized)
    elif kind == "mcp" and pattern and "/" not in pattern:
        pattern = f"{pattern}/*"
    return Rule(text=raw, kind=kind, pattern=pattern, regex=regex, absolute=absolute)


def parse_rules(texts: Iterable[str]) -> List[Rule]:
    """여러 줄을 읽습니다. 하나라도 틀리면 ValueError (틀린 줄을 전부 적습니다)."""
    rules: List[Rule] = []
    problems: List[str] = []
    for text in texts or ():
        if not str(text or "").strip():
            continue
        try:
            rules.append(parse_rule(text))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError("\n".join(problems))
    return rules


# ---------------------------------------------------------------------------
# 호출을 행위로
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """호출 하나가 하는 일 하나.

    `target` 은 규칙과 화면이 보는 값입니다 — 경로면 작업 공간 기준 상대 경로(밖이면
    절대 경로), 네트워크면 호스트, 실행이면 코드. 대상을 모르면 빈 문자열입니다.
    """

    kind: str          # read | write | delete | exec | net | tool
    target: str
    risk: str
    absolute: str = ""  # 경로 행위의 절대 경로 ('/' 구분)
    # "arg" (인자) | "code" (코드에서 찾음) | "server" (원격 서버) | "tool" (도구 자체)
    origin: str = ""
    display: str = ""   # 화면용 이름을 따로 줄 때 (도구 자체를 대표하는 행위)

    def label(self) -> str:
        """카드에 적는 한 줄."""
        if self.display:
            return self.display
        noun = {
            "read": "읽기", "write": "쓰기", "delete": "삭제", "exec": "코드 실행",
            "net": "네트워크", "tool": "도구",
        }.get(self.kind, self.kind)
        if self.kind == "exec":
            return noun
        target = self.target or ("모든 호스트" if self.kind == "net" else "대상을 알 수 없음")
        where = " (코드에서)" if self.origin == "code" else ""
        return f"{noun} {target}{where}"


@dataclass
class CallProfile:
    """호출 하나를 판정에 필요한 모양으로 바꾼 것."""

    server: str
    tool: str
    risk: str
    actions: List[Action] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"{self.server}/{self.tool}"

    @property
    def max_risk(self) -> str:
        risks = [a.risk for a in self.actions] or [self.risk]
        return max(risks, key=lambda r: _RISK_RANK.get(r, 4))


@dataclass(frozen=True)
class ToolKind:
    """알려진 도구 하나의 모양.

    `paths` 는 (인자 이름, 행위). `code_arg`/`code_file_arg` 는 실행할 코드(또는 그
    파일)가 든 인자, `url_arg` 는 나갈 주소가 든 인자입니다. `relative_to` 가 있으면
    경로 인자를 그 인자(저장소 경로 등) 기준으로 풉니다.
    """

    risk: str
    paths: Tuple[Tuple[str, str], ...] = ()
    code_arg: str = ""
    code_file_arg: str = ""
    url_arg: str = ""
    relative_to: str = ""


def _fs(risk: str, action: str, *args: str) -> ToolKind:
    return ToolKind(risk, tuple((a, action) for a in args))


# 기본 서버들의 도구. 이름 꼬리로 찾습니다 — 서버 키를 무엇으로 등록했든 같습니다.
# 다만 **신뢰하는 서버**에만 씁니다 (`profile_call`). 원격 서버가 자기 도구 이름을
# `read_file` 로 지어 읽기로 통과하는 일을 막기 위해서입니다.
KNOWN_TOOLS: Dict[str, ToolKind] = {
    # filesystem (@modelcontextprotocol/server-filesystem)
    "read_file": _fs(READ, "read", "path"),
    "read_text_file": _fs(READ, "read", "path"),
    "read_media_file": _fs(READ, "read", "path"),
    "read_multiple_files": _fs(READ, "read", "paths"),
    "list_directory": _fs(READ, "read", "path"),
    "list_directory_with_sizes": _fs(READ, "read", "path"),
    "directory_tree": _fs(READ, "read", "path"),
    "search_files": _fs(READ, "read", "path"),
    "get_file_info": _fs(READ, "read", "path"),
    "list_allowed_directories": ToolKind(READ),
    "write_file": _fs(WRITE, "write", "path"),
    "edit_file": _fs(WRITE, "write", "path"),
    "create_directory": _fs(WRITE, "write", "path"),
    # 대상이 이미 있으면 서버가 거절하므로 덮어쓰지 않습니다 — 양쪽 다 쓰기입니다.
    "move_file": _fs(WRITE, "write", "source", "destination"),
    # git (mcp-server-git). `git_reset` 은 스테이징만 풉니다 (작업 트리를 지우지 않음).
    "git_status": _fs(READ, "read", "repo_path"),
    "git_diff_unstaged": _fs(READ, "read", "repo_path"),
    "git_diff_staged": _fs(READ, "read", "repo_path"),
    "git_diff": _fs(READ, "read", "repo_path"),
    "git_log": _fs(READ, "read", "repo_path"),
    "git_show": _fs(READ, "read", "repo_path"),
    "git_branch": _fs(READ, "read", "repo_path"),
    "git_add": ToolKind(WRITE, (("repo_path", "write"), ("files", "write")), relative_to="repo_path"),
    "git_commit": _fs(WRITE, "write", "repo_path"),
    "git_reset": _fs(WRITE, "write", "repo_path"),
    "git_create_branch": _fs(WRITE, "write", "repo_path"),
    "git_checkout": _fs(WRITE, "write", "repo_path"),
    "git_init": _fs(WRITE, "write", "repo_path"),
    # memory (포크한 memory-scoped). 대화마다 격리된 그래프만 건드립니다.
    "read_graph": ToolKind(STATE),
    "search_nodes": ToolKind(STATE),
    "open_nodes": ToolKind(STATE),
    "create_entities": ToolKind(STATE),
    "create_relations": ToolKind(STATE),
    "add_observations": ToolKind(STATE),
    "delete_entities": ToolKind(STATE),
    "delete_observations": ToolKind(STATE),
    "delete_relations": ToolKind(STATE),
    # sandbox (AirgappedPySandbox)
    "execute_python_code": ToolKind(EXEC, code_arg="code"),
    "run_python_file": ToolKind(EXEC, code_file_arg="file_path"),
    "write_workspace_file": _fs(WRITE, "write", "filename"),
    "append_workspace_file": _fs(WRITE, "write", "filename"),
    "list_workspace_files": ToolKind(READ),
    "reset_kernel_state": ToolKind(STATE),
    # 패키지 설치는 밖의 코드를 들여오는 일입니다 (사내 미러라도).
    "install_python_packages": ToolKind(EXEC_FLAGGED),
    # sequential thinking
    "sequentialthinking": ToolKind(STATE),
    # fetch (mcp-server-fetch)
    "fetch": ToolKind(NET, url_arg="url"),
    # PairSlide (pair_slide). 문서는 PairSlide 쪽 저장소에 있고, 경로 인자는 그쪽
    # 기준이라 작업 공간 경로로 풀지 않습니다.
    "slide_read": ToolKind(READ),
    "slide_comments": ToolKind(READ),
    "slide_history": ToolKind(READ),
    "slide_list": ToolKind(READ),
    "slide_open": ToolKind(WRITE),
    "slide_set": ToolKind(WRITE),
    "slide_add": ToolKind(WRITE),
    "slide_delete": ToolKind(WRITE),
    "slide_save": ToolKind(WRITE),
    "slide_load": ToolKind(WRITE),
    "slide_export": ToolKind(WRITE),
    "slide_export_html": ToolKind(WRITE),
    "slide_resolve_comment": ToolKind(WRITE),
    "slide_restore": ToolKind(WRITE),
    "sheet_write_table": ToolKind(WRITE),
}


def risk_from_annotations(annotations: Optional[Mapping[str, Any]]) -> str:
    """MCP 도구 annotations 로 등급을 정합니다. 없으면 `UNKNOWN`.

    명세의 기본값을 그대로 따릅니다 — `destructiveHint` 와 `openWorldHint` 는 적지
    않으면 True 입니다. 그래서 힌트를 대충 단 도구는 대개 묻는 쪽으로 갑니다.
    """
    if not annotations:
        return UNKNOWN
    read_only = bool(annotations.get("readOnlyHint", False))
    destructive = bool(annotations.get("destructiveHint", True))
    open_world = bool(annotations.get("openWorldHint", True))
    if open_world:
        return NET
    if read_only:
        return READ
    return DELETE if destructive else WRITE


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip("[]").lower()
    if value in ("localhost", ""):
        return value == "localhost"
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ToolMeta:
    """판정에 필요한 도구 정보. `MCPManager.tool_meta` 가 채웁니다."""

    server: str
    tool: str
    trusted: bool = True
    annotations: Optional[Mapping[str, Any]] = None
    remote_host: str = ""   # 원격(URL) 서버면 호스트, 로컬 프로세스면 빈 문자열


def _as_posix(path: str) -> str:
    return path.replace("\\", "/")


def resolve_path(raw: str, workspace: Path, base: Optional[str] = None) -> Tuple[str, str, bool]:
    """(규칙·화면용 경로, 절대 경로, 작업 공간 안인가).

    상대 경로는 작업 공간 기준으로 풉니다. `./workspace/x` 처럼 작업 공간 이름을
    머리에 붙인 경로도 받습니다 (sandbox 가 그렇게 받고, 모델도 그렇게 씁니다).
    파일 시스템을 건드리지 않고 글자로만 정규화합니다 — 판정은 호출보다 먼저라
    대상이 아직 없을 수 있습니다.
    """
    text = str(raw or "").strip().strip("\"'")
    if text.lower().startswith("file://"):
        text = urlparse(text).path
        if re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
    text = os.path.expanduser(text)
    ws = Path(os.path.normpath(str(workspace)))
    ws_posix = _as_posix(str(ws))

    candidate = Path(text)
    if not candidate.is_absolute() and not _WINDOWS_ABS_RE.match(_as_posix(text)):
        rel = _as_posix(text)
        while rel.startswith("./"):
            rel = rel[2:]
        prefix = f"{ws.name}/"
        if ws.name and rel.lower().startswith(prefix.lower()):
            rel = rel[len(prefix):]
        root = Path(base) if base else ws
        if base and not Path(base).is_absolute():
            root = ws / base
        candidate = root / rel
    absolute = _as_posix(os.path.normpath(str(candidate)))

    inside = absolute.lower() == ws_posix.lower() or absolute.lower().startswith(
        ws_posix.lower().rstrip("/") + "/"
    )
    if inside:
        rel_path = absolute[len(ws_posix.rstrip("/")) + 1:] if absolute.lower() != ws_posix.lower() else "."
        return rel_path or ".", absolute, True
    return absolute, absolute, False


def _path_action(kind: str, raw: str, workspace: Path, origin: str,
                 base: Optional[str] = None) -> Action:
    if not str(raw or "").strip():
        risk = {"read": READ, "write": WRITE, "delete": DELETE}[kind]
        return Action(kind, "", risk, "", origin)
    shown, absolute, inside = resolve_path(raw, workspace, base)
    if not inside:
        risk = OUTSIDE
    else:
        risk = {"read": READ, "write": WRITE, "delete": DELETE}[kind]
    return Action(kind, shown, risk, absolute, origin)


def _values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


def _net_action(host: str, origin: str) -> Action:
    return Action("net", (host or "").lower(), NET, "", origin)


def _default_file_reader(path: Path) -> Optional[str]:
    try:
        if path.is_file() and path.stat().st_size <= 512 * 1024:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return None


def profile_call(
    meta: ToolMeta,
    arguments: Mapping[str, Any],
    workspace: Path,
    read_file: Callable[[Path], Optional[str]] = _default_file_reader,
) -> CallProfile:
    """호출 하나를 행위 목록으로 바꿉니다."""
    args = arguments if isinstance(arguments, Mapping) else {}
    kind = KNOWN_TOOLS.get(meta.tool) if meta.trusted else None
    notes: List[str] = []
    actions: List[Action] = []

    if kind is not None:
        risk = kind.risk
        base = None
        if kind.relative_to:
            base_values = _values(args.get(kind.relative_to))
            base = base_values[0] if base_values else None
        for arg_name, action in kind.paths:
            path_base = base if arg_name != kind.relative_to else None
            for value in _values(args.get(arg_name)):
                actions.append(_path_action(action, value, workspace, "arg", path_base))

        code: Optional[str] = None
        if kind.code_arg:
            code = str(args.get(kind.code_arg) or "")
        elif kind.code_file_arg:
            raw_file = next(iter(_values(args.get(kind.code_file_arg))), "")
            if raw_file:
                actions.append(_path_action("read", raw_file, workspace, "arg"))
                _shown, absolute, inside = resolve_path(raw_file, workspace)
                code = read_file(Path(absolute)) if inside else None
                if code is None:
                    notes.append("실행할 파일을 읽지 못해 내용을 검사하지 못했습니다")
            else:
                notes.append("실행할 파일이 지정되지 않았습니다")
        if kind.code_arg or kind.code_file_arg:
            scan = scan_python(code) if code is not None else None
            if scan is None:
                exec_risk = EXEC_FLAGGED
            else:
                notes.extend(describe_scan(scan))
                exec_risk = EXEC_FLAGGED if scan.uncertain else EXEC
                for action, path in iter_mentions(scan):
                    actions.append(_path_action(action, path, workspace, "code"))
                for host in scan.hosts:
                    actions.append(_net_action(host, "code"))
                if scan.network and not scan.hosts:
                    actions.append(_net_action("", "code"))
            actions.insert(0, Action("exec", code or "", exec_risk, "", "arg"))
            risk = exec_risk

        if kind.url_arg:
            url = next(iter(_values(args.get(kind.url_arg))), "")
            actions.append(_net_action(url_host(url) or _bare_host(url), "arg"))
    else:
        risk = risk_from_annotations(meta.annotations) if meta.trusted else UNKNOWN

    # 원격 서버를 부르는 것 자체가 인자를 그 호스트로 보내는 일입니다.
    if meta.remote_host and not is_loopback_host(meta.remote_host):
        actions.append(_net_action(meta.remote_host, "server"))

    # 도구 자체의 등급을 대표하는 행위가 없으면 하나 세웁니다 (memory, 패키지 설치,
    # 모르는 도구). 규칙은 `mcp(서버/도구)` 나 행위 이름만으로 이것을 가리킵니다.
    if not any(a.origin == "arg" or a.kind == "exec" for a in actions):
        actions.insert(0, Action(
            _kind_for_risk(risk), "", risk, "", "tool",
            display=f"도구 {meta.server}/{meta.tool}",
        ))

    return CallProfile(server=meta.server, tool=meta.tool, risk=risk, actions=actions, notes=notes)


def _bare_host(text: str) -> str:
    """스킴 없이 온 주소에서 호스트만 ("example.com/path" → "example.com")."""
    value = (text or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"http://{value}")
    return (parsed.hostname or "").lower()


def _kind_for_risk(risk: str) -> str:
    return {
        READ: "read", STATE: "tool", WRITE: "write", OUTSIDE: "write", DELETE: "delete",
        EXEC: "exec", EXEC_FLAGGED: "exec", NET: "net", UNKNOWN: "tool",
    }.get(risk, "tool")


# ---------------------------------------------------------------------------
# 규칙 맞추기
# ---------------------------------------------------------------------------


def _path_matches(rule: Rule, action: Action) -> bool:
    if rule.matches_all and rule.regex is None:
        return True
    if not action.target:
        return False
    if rule.regex is None:
        return False
    if rule.absolute:
        return bool(rule.regex.match(action.absolute or action.target))
    candidates = [action.target]
    # `**/` 로 시작하는 규칙과 정규식은 작업 공간 밖의 같은 이름도 가리킵니다 (`~/.env` 등).
    if rule.pattern.startswith(("**", "regex:")) and action.absolute:
        candidates.append(action.absolute)
    if rule.pattern.startswith("regex:"):
        return any(rule.regex.search(c) for c in candidates)
    return any(rule.regex.match(c) for c in candidates)


def _host_matches(rule: Rule, host: str) -> bool:
    if rule.matches_all and rule.regex is None:
        return True
    if not host:
        return False
    if rule.regex is not None:
        return bool(rule.regex.search(host))
    pattern = rule.pattern.lower()
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(host, pattern)
    return host == pattern or host.endswith("." + pattern)


def rule_matches_action(rule: Rule, action: Action, restrictive: bool) -> bool:
    """이 규칙이 이 행위를 가리키는가. `restrictive` 면 deny·ask 로서 봅니다."""
    if rule.kind in PATH_KINDS:
        if action.kind not in PATH_KINDS:
            return False
        rule_rank, action_rank = _PATH_RANK[rule.kind], _PATH_RANK[action.kind]
        if restrictive and action_rank < rule_rank:
            return False
        if not restrictive and action_rank > rule_rank:
            return False
        return _path_matches(rule, action)
    if rule.kind == "exec":
        if action.kind != "exec":
            return False
        if rule.regex is None:
            return True
        return bool(action.target) and bool(rule.regex.search(action.target))
    if rule.kind == "net":
        return action.kind == "net" and _host_matches(rule, action.target)
    return False


def rule_matches_identity(rule: Rule, profile: CallProfile) -> bool:
    if rule.kind != "mcp":
        return False
    pattern = rule.pattern or "*/*"
    return fnmatch.fnmatchcase(profile.identity, pattern)


def _first_hit(rules: Sequence[Rule], profile: CallProfile, restrictive: bool) -> Optional[Tuple[Rule, str]]:
    """먼저 걸린 규칙과, 무엇에 걸렸는지 한 줄."""
    for rule in rules:
        if rule_matches_identity(rule, profile):
            return rule, f"도구 {profile.identity}"
        for action in profile.actions:
            if rule_matches_action(rule, action, restrictive):
                return rule, action.label()
    return None


# ---------------------------------------------------------------------------
# 고정 보호 — 어떤 모드·규칙으로도 풀 수 없습니다
# ---------------------------------------------------------------------------

MEMORY_GRAPH_DIRNAME = ".memory-graphs"


def _within(path: str, root: str) -> bool:
    p, r = path.lower().rstrip("/"), root.lower().rstrip("/")
    return p == r or p.startswith(r + "/")


def hard_block(
    profile: CallProfile,
    workspace: Path,
    install_root: Optional[Path] = None,
    protected_files: Sequence[Path] = (),
) -> Optional[str]:
    """설정으로 끌 수 없는 보호. 걸리면 이유 한 줄, 아니면 None.

    1. **MADO 설치 폴더** — 작업 공간을 뺀 전부. `conf.json`(권한 규칙이 든 파일),
       `.env`(원격 접속 토큰과 API 키), `multiagent.db`(다른 대화 전부), 앱 코드가 여기
       있습니다. 세션 작업 공간을 설치 폴더나 그 위로 잡아도 이 파일들은 도구 범위에
       들어가지 않습니다 — 에이전트가 자기 권한 규칙을 고칠 수 없어야 합니다.
    2. **`.memory-graphs`** — 대화마다 격리된 지식 그래프입니다. memory 서버는 호스트가
       준 `_meta` 로 대화를 가르는데, 파일을 직접 열면 그 경계를 건너 다른 대화의
       그래프를 읽습니다. memory 도구만 이 폴더를 씁니다.
    3. **`.git` 안 쓰기** — `hooks/` 는 다음 git 명령 때 실행되는 코드입니다. 저장소는
       git 도구로만 바꿉니다 (git 도구의 행위는 저장소 경로라 여기 걸리지 않습니다).
    """
    ws = _as_posix(os.path.normpath(str(workspace)))
    root = _as_posix(os.path.normpath(str(install_root))) if install_root else ""
    # 작업 공간이 설치 폴더 **안쪽**에 있을 때만 그 작업 공간이 보호에서 빠집니다.
    # 작업 공간이 설치 폴더이거나 그 위라면 설치 폴더 전체가 그대로 보호됩니다.
    workspace_carved = bool(root) and _within(ws, root) and ws.lower() != root.lower()
    protected = [_as_posix(os.path.normpath(str(p))) for p in protected_files if p]

    for action in profile.actions:
        if action.kind not in PATH_KINDS or not action.absolute:
            continue
        parts = [p.lower() for p in PurePosixPath(action.absolute).parts]
        if MEMORY_GRAPH_DIRNAME in parts:
            return (
                f"`{action.target}` 은 대화별 지식 그래프 폴더({MEMORY_GRAPH_DIRNAME})입니다. "
                f"다른 대화의 기억이 섞이지 않도록 memory 도구로만 다룹니다."
            )
        if action.kind != "read" and ".git" in parts:
            return (
                f"`{action.target}` 은 git 저장소 내부(.git)입니다. hooks 가 코드 실행 경로라 "
                f"파일로 직접 쓰지 않고 git 도구로만 바꿉니다."
            )
        if any(_within(action.absolute, p) for p in protected):
            return f"`{action.target}` 은 MADO 의 설정·비밀·기록 파일이라 도구로 다룰 수 없습니다."
        if root and _within(action.absolute, root):
            if workspace_carved and _within(action.absolute, ws):
                continue
            return (
                f"`{action.target}` 은 MADO 설치 폴더 안입니다. 설정(conf.json)·비밀(.env)·"
                f"대화 기록·앱 코드가 있는 곳이라 작업 공간 밖으로 보고 막습니다."
            )
    return None


def hard_refusal(
    profile: CallProfile,
    tool_name: str,
    arguments: Mapping[str, Any],
    available_tools: Iterable[str],
    workspace: Path,
    install_root: Optional[Path] = None,
    protected_files: Sequence[Path] = (),
) -> Optional[Tuple[str, str]]:
    """설정으로 끌 수 없는 거부 — (모델에게 돌려줄 문구, 상태) 또는 None.

    바이너리 문서 쓰기 거부(`guards.binary_write_refusal`)도 여기서 함께 봅니다. 보안이
    아니라 형식 문제라 상태는 `error` 이고(모델이 읽고 다른 도구로 고칠 실패), 보안
    거부는 `denied` 입니다. 둘 다 어떤 설정으로도 풀면 안 되고, 여기서 먼저 걸러야
    결국 실패할 호출에 승인 카드를 띄우지 않습니다.
    """
    binary = binary_write_refusal(tool_name, arguments, available_tools)
    if binary:
        return binary, "error"
    reason = hard_block(profile, workspace, install_root, protected_files)
    if reason:
        return refusal_text(tool_name, f"고정 보호 — {reason}"), "denied"
    return None


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------


@dataclass
class Policy:
    """한 발언에 걸리는 규칙 묶음. 전역·에이전트·대화 허용을 합친 결과입니다."""

    mode: str = DEFAULT_MODE
    deny: List[Rule] = field(default_factory=list)
    ask: List[Rule] = field(default_factory=list)
    allow: List[Rule] = field(default_factory=list)
    grants: List[Rule] = field(default_factory=list)   # "이 대화에서 허용"
    # "이 대화에서 거부". deny 와 같은 무게이지만 따로 둡니다 — 모델에게 "설정이 막았다" 가
    # 아니라 "사용자가 이 대화에서 거부했다" 고 알려야 같은 시도를 되풀이하지 않습니다.
    denials: List[Rule] = field(default_factory=list)


@dataclass
class Verdict:
    effect: str
    risk: str
    source: str               # hard | rule | grant | mode
    rule: str = ""
    headline: str = ""        # 왜 이렇게 판정했는지 한 줄
    reasons: List[str] = field(default_factory=list)      # 걸린 행위들과 코드 검사 소견
    suggestions: List[str] = field(default_factory=list)  # "다음부터 묻지 않기" 범위 후보

    def explain(self) -> str:
        """모델에게 돌려줄 한 덩어리."""
        lines = [self.headline] + [f"- {r}" for r in self.reasons]
        return "\n".join(line for line in lines if line)


def _suggestion(profile: CallProfile, action: Action) -> str:
    """이 행위를 다음부터 묻지 않게 할 가장 좁은 규칙."""
    if action.kind in PATH_KINDS and action.target:
        return f"{action.kind}({action.target})"
    if action.kind == "net" and action.target:
        return f"net({action.target})"
    # 코드는 매번 다르고, 대상을 모르는 행위는 좁힐 수 없습니다 — 도구 단위가 가장
    # 좁습니다. 화면이 "이 도구의 모든 호출" 이라고 밝힙니다.
    return f"mcp({profile.identity})"


def evaluate(profile: CallProfile, policy: Policy) -> Verdict:
    """호출 하나를 판정합니다 (고정 보호는 `hard_refusal` 이 먼저 봅니다)."""
    mode = normalize_mode(policy.mode)
    top = profile.max_risk

    hit = _first_hit(policy.deny, profile, restrictive=True)
    if hit:
        rule, what = hit
        return Verdict(DENY, top, "rule", rule.text, f"거부 규칙 `{rule.text}` 에 걸렸습니다.", [what])

    hit = _first_hit(policy.denials, profile, restrictive=True)
    if hit:
        rule, what = hit
        return Verdict(
            DENY, top, "denial", rule.text,
            f"유저가 이 대화에서 `{rule.text}` 를 거부했습니다. 이 대화 동안 같은 범위의 호출은 "
            f"실행되지 않습니다.",
            [what],
        )

    hit = _first_hit(policy.ask, profile, restrictive=True)
    if hit:
        rule, what = hit
        # ask 규칙은 허용 규칙보다 앞서므로 "다음부터 묻지 않기" 는 효과가 없습니다.
        # 그래서 제안하지 않습니다 — 없는 선택지를 보여주지 않습니다.
        return Verdict(ASK, top, "rule", rule.text, f"묻기 규칙 `{rule.text}` 에 걸렸습니다.",
                       [what] + profile.notes)

    for source, rules in (("rule", policy.allow), ("grant", policy.grants)):
        for rule in rules:
            if rule_matches_identity(rule, profile):
                return Verdict(ALLOW, top, source, rule.text)

    pending: List[Tuple[str, Action]] = []
    used: Optional[Tuple[str, Rule]] = None
    for action in profile.actions:
        effect = MODE_DEFAULTS[mode].get(action.risk, ASK)
        if effect == ALLOW:
            continue
        covering = next(
            ((s, r) for s, rules in (("rule", policy.allow), ("grant", policy.grants))
             for r in rules if rule_matches_action(r, action, restrictive=False)),
            None,
        )
        if covering is not None:
            used = used or covering
            continue
        pending.append((effect, action))

    if not pending:
        if used is not None:
            return Verdict(ALLOW, top, used[0], used[1].text)
        return Verdict(ALLOW, top, "mode")

    effect = max((e for e, _a in pending), key=lambda e: EFFECT_RANK[e])
    deciding = [a for e, a in pending if e == effect]
    risk = max((a.risk for a in deciding), key=lambda r: _RISK_RANK.get(r, 4))
    headline = (
        f"{MODE_LABELS[mode]} 모드에서 확인이 필요한 호출입니다."
        if effect == ASK else
        f"{MODE_LABELS[mode]} 모드에서는 하지 않는 호출입니다."
    )
    reasons = [f"[{RISK_LABELS.get(a.risk, a.risk)}] {a.label()}" for a in deciding]
    reasons.extend(profile.notes)
    suggestions: List[str] = []
    if effect == ASK:
        for action in deciding:
            text = _suggestion(profile, action)
            if text not in suggestions:
                suggestions.append(text)
    return Verdict(effect, risk, "mode", "", headline, reasons, suggestions)


def narrow_rules(profile: CallProfile) -> List[str]:
    """이 호출을 가리키는 가장 좁은 규칙들 — "이 대화에서 거부" 범위의 기본값.

    허용 제안(`Verdict.suggestions`)은 **묻게 만든** 행위만 덮으면 되지만, 거부는 묻기
    규칙에 걸린 호출에도 쓸 수 있어야 해서 따로 만듭니다. 인자와 원격 서버에서 나온
    행위만 씁니다 — 코드 안에서 찾은 경로까지 거부하면 그 파일을 다른 방법으로 읽는
    정당한 호출까지 막힙니다.
    """
    rules: List[str] = []
    for action in profile.actions:
        if action.origin not in ("arg", "server") or action.kind == "exec":
            continue
        text = _suggestion(profile, action)
        if text not in rules:
            rules.append(text)
    return rules or [f"mcp({profile.identity})"]


def denial_covers(rule_texts: Sequence[str], profile: CallProfile) -> bool:
    """사람이 고친 거부 범위가 지금 이 호출을 막는가. 막지 못하는 범위는 저장해도 소용없습니다."""
    try:
        rules = parse_rules(rule_texts)
    except ValueError:
        return False
    if not rules:
        return False
    return evaluate(profile, Policy(mode="auto", denials=rules)).effect == DENY


def grant_covers(rule_texts: Sequence[str], profile: CallProfile, mode: str) -> bool:
    """사람이 고친 허용 범위가 지금 이 호출을 덮는가 (Antigravity 의 범위 검증).

    덮지 못하는 범위로 "이 대화에서 허용" 을 누르면, 방금 허락한 호출이 다음 판에
    다시 물어집니다. 사람은 허락했다고 생각하므로 그 전에 알려야 합니다.
    """
    try:
        rules = parse_rules(rule_texts)
    except ValueError:
        return False
    if not rules:
        return False
    return evaluate(profile, Policy(mode=mode, allow=rules)).effect == ALLOW


def tool_always_denied(meta: ToolMeta, policy: Policy) -> Optional[str]:
    """인자와 무관하게 늘 거부되는 도구면 그 이유, 아니면 None.

    이런 도구는 목록에서 뺍니다 (Claude Code 가 도구 이름 단위 deny 를 모델의 문맥에서
    지우는 것과 같습니다). 모델이 시도조차 하지 않아 토큰과 헛걸음이 줄어듭니다.
    """
    probe = CallProfile(server=meta.server, tool=meta.tool, risk=UNKNOWN)
    for rule in policy.deny + policy.denials:
        if rule.kind == "mcp" and rule_matches_identity(rule, probe):
            return f"거부 규칙 `{rule.text}`"
    mode = normalize_mode(policy.mode)
    kind = KNOWN_TOOLS.get(meta.tool) if meta.trusted else None
    risk = kind.risk if kind is not None else (
        risk_from_annotations(meta.annotations) if meta.trusted else UNKNOWN
    )
    if meta.remote_host and not is_loopback_host(meta.remote_host):
        risk = NET if _RISK_RANK[risk] < _RISK_RANK[NET] else risk
    if MODE_DEFAULTS[mode].get(risk) == DENY:
        allowed = any(rule_matches_identity(r, probe) for r in policy.allow + policy.grants)
        if not allowed:
            return f"{MODE_LABELS[mode]} 모드는 {RISK_LABELS[risk]} 도구를 쓰지 않습니다"
    return None


# ---------------------------------------------------------------------------
# 도구 기록을 사람에게 보여줄 말 — 채팅 피드와 세션 저장 파일이 같은 말을 씁니다
# ---------------------------------------------------------------------------
#
# 한 줄에 두 가지만 답합니다. **결과**(도구가 돌았는가, 어떻게 끝났는가)는 제목 줄의
# 배지 하나로, **판정**(누가 · 무슨 근거로 허락하거나 막았는가)은 본문 첫 줄 하나로.
# 예전에는 제목 앞말(Tool Call/Blocked), 상태 배지(SUCCESS/DENIED), 보안 배지(사용자 거부)
# 가 같은 사실을 서로 다른 말로 되풀이했습니다.
#
# 감사 기록의 `rule` 칸 모양 (`tool_gate` 가 적습니다):
#   mode:<모드>        모드 기본값으로 판정
#   session:<규칙…>    이 대화의 허용·거부 규칙 (카드에서 등록했거나, 등록된 것에 걸렸거나)
#   always:<규칙…>     카드에서 conf.json 에 등록
#   once · repeat · unattended · gate-error   표식
#   그 밖              conf.json 의 규칙 원문 (고정 보호면 그 종류)

OUTCOME_LABELS = {"success": "성공", "error": "실패", "blocked": "차단"}
_BLOCKING_DECISIONS = frozenset({"deny", "hard", "rejected", "timeout"})
_APPROVER_LABELS = {"local": "서버 PC", "remote": "원격"}


def tool_outcome(status: str, security: Optional[Mapping[str, Any]] = None) -> str:
    """도구가 돌았는가와 그 결과 — `success` · `error` · `blocked`.

    `blocked` 는 실행하지 않은 호출입니다. 보안 판정뿐 아니라 문서 형식 거부(텍스트 도구로
    .pptx)도 여기 듭니다 — 사람에게 중요한 것은 "도구가 돌았는가" 입니다.
    """
    decision = str((security or {}).get("decision") or "")
    if status == "denied" or decision in _BLOCKING_DECISIONS:
        return "blocked"
    return "success" if status == "success" else "error"


def _split_rule(rule: str) -> Tuple[str, str]:
    for origin in ("mode", "session", "always"):
        if rule.startswith(f"{origin}:"):
            return origin, rule[len(origin) + 1:]
    return "", rule


def _registered(origin: str, body: str) -> str:
    if origin == "session":
        return f"이 대화 규칙으로 등록 {body}"
    if origin == "always":
        return f"conf.json 규칙으로 등록 {body}"
    return "이번만"


def describe_verdict(security: Optional[Mapping[str, Any]]) -> str:
    """판정 한 줄 — "누가 · 근거 · 답한 곳". 판정 없이 실행된 호출이면 빈 문자열.

    "누가" 자리의 말은 정해져 있습니다: 자동 허용 · 유저 승인 · 규칙 차단 · 모드 차단 ·
    고정 보호 · 유저 거부 · 응답 없음 (그리고 판정 오류). 승인 카드 버튼의 허용·거부와
    같은 말입니다.
    """
    info = security or {}
    decision = str(info.get("decision") or "")
    if not decision:
        return ""
    rule = str(info.get("rule") or "")
    approver = str(info.get("approver") or "")
    origin, body = _split_rule(rule)

    if decision == "allow":
        if origin == "mode":
            parts = ["자동 허용", f"{MODE_LABELS.get(body, body)} 모드"]
        elif origin == "session":
            parts = ["자동 허용", f"이 대화 규칙 {body}"]
        else:
            parts = ["자동 허용", f"규칙 {body}" if body else ""]
    elif decision == "approved":
        parts = ["유저 승인", _registered(origin, body)]
    elif decision == "deny":
        if origin == "mode":
            parts = ["모드 차단", f"{MODE_LABELS.get(body, body)} 모드"]
        elif rule == "unattended":
            parts = ["응답 없음", "물어볼 유저가 없었음"]
        elif rule == "gate-error":
            parts = ["판정 오류", "안전을 위해 실행하지 않음"]
        else:
            parts = ["규칙 차단", body]
    elif decision == "hard":
        parts = ["고정 보호", "" if body in ("", "고정 보호") else body]
    elif decision == "rejected":
        if rule == "repeat":
            parts = ["유저 거부", "이번 턴에 이미 거부한 호출"]
        elif origin == "session" and not approver:
            parts = ["유저 거부", f"이 대화 규칙 {body}"]
        else:
            parts = ["유저 거부", _registered(origin, body)]
    elif decision == "timeout":
        parts = ["응답 없음", "정해진 시간 안에 답이 없었음"]
    else:
        parts = [decision, rule]
    parts.append(_APPROVER_LABELS.get(approver, ""))
    return " · ".join(part for part in parts if part)


def refusal_text(tool_name: str, reason: str, next_action: str = "") -> str:
    """모델에게 돌려줄 거부 관측. `guards.binary_write_refusal` 과 같은 모양입니다."""
    action = next_action or (
        "같은 호출을 다시 하지 마십시오. 이 도구 없이 진행하거나, 꼭 필요하다면 "
        "왜 필요한지 발언에 적어 유저가 판단하게 하세요."
    )
    return (
        f"REFUSED - 도구가 실행되지 않았습니다 (도구 보안 정책).\n"
        f"도구: `{tool_name}`\n"
        f"이유: {reason}\n\n"
        f"next_action: {action}"
    )


# ---------------------------------------------------------------------------
# 비밀 환경변수
# ---------------------------------------------------------------------------

# 이름에 이것이 든 환경변수는 MCP 서버 프로세스에 물려주지 않습니다.
SECRET_ENV_PATTERN = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
    r"_KEY$|^KEY$|AUTH)",
    re.IGNORECASE,
)


def server_environment(parent: Mapping[str, str], explicit: Mapping[str, str]) -> Dict[str, str]:
    """MCP 서버 프로세스에 줄 환경변수. 부모의 비밀은 빼고, 서버에 명시한 값은 넣습니다.

    MADO 는 `.env` 를 읽어 자기 환경에 올립니다 — 원격 접속 토큰과 LLM API 키가 거기
    있습니다. 예전에는 그 환경을 통째로 서버에 물려줬고, sandbox 커널은 서버의 환경을
    다시 물려받습니다. 그러면 에이전트 코드 한 줄(`os.environ`)이면 토큰과 키가 읽혔습니다.

    서버가 정말 비밀이 필요하면 conf.json 의 그 서버 `env` 블록에 적습니다
    (`"env": {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}`). 명시한 값은 거르지 않습니다 —
    누가 무엇을 받는지가 설정에 드러나기 때문입니다.
    """
    env = {k: v for k, v in parent.items() if not SECRET_ENV_PATTERN.search(k)}
    env.update(explicit or {})
    return env
