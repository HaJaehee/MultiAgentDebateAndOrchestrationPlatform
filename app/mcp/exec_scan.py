"""sandbox 에 넘길 Python 코드를 실행 **전에** 읽어, 코드가 무엇을 건드리는지 추립니다.

도구 보안 판정(`app/mcp/policy.py`)은 도구가 아니라 **행위**에 규칙을 겁니다. filesystem
도구에서 `.env` 읽기를 막아 두어도, sandbox 에 `open('.env').read()` 한 줄을 보내면
같은 파일이 읽힙니다 — Antigravity 가 파일 도구로는 막힌 `.env` 를 터미널 `cat` 으로
읽어 낸 것과 같은 우회로입니다. 그래서 코드 안의 경로·네트워크·프로세스 실행을
여기서 행위로 바꿔 같은 규칙에 태웁니다.

**이것은 보안 경계가 아닙니다.** Python 은 문자열을 조립하거나 `getattr` 로 이름을
감추면 무엇이든 숨길 수 있습니다. 여기서 거르는 것은 모델의 실수와 흔한 주입 문구가
만들어 내는 **평범한** 코드입니다. 그래서 판정할 수 없는 모양(구문 분석 실패, `eval`,
프로세스 실행)을 만나면 "안전하다" 고 하지 않고 소견(`flags`)을 남겨 사람에게 묻게
합니다. 진짜 경계는 커널을 OS 수준에서 격리하는 것이고, 그건 이 모듈의 몫이 아닙니다.

순수 함수입니다 — 파일도 네트워크도 건드리지 않습니다.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple

# 가져오기만 해도 네트워크를 쓰겠다는 뜻인 모듈 (첫 이름 기준).
NET_MODULES = frozenset({
    "socket", "ssl", "urllib", "urllib2", "urllib3", "http", "httplib", "requests",
    "httpx", "aiohttp", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
    "paramiko", "asyncssh", "websocket", "websockets", "xmlrpc", "pycurl", "grpc",
})

# 프로세스를 띄우거나 네이티브 코드를 부르는 모듈. 커널 밖으로 나가는 문이라, 무엇을
# 하는지 여기서는 알 수 없습니다.
PROCESS_MODULES = frozenset({
    "subprocess", "pty", "ctypes", "cffi", "winreg", "_winapi", "msvcrt",
    "win32api", "win32process", "win32com",
})

# 코드를 문자열로 실행하는 호출. 그 문자열 안은 읽을 수 없습니다.
DYNAMIC_CALLS = frozenset({
    "eval", "exec", "compile", "__import__",
    "importlib.import_module", "importlib.__import__", "builtins.eval", "builtins.exec",
})

# 프로세스를 띄우는 호출 (모듈을 가져오지 않고도 `os` 로 할 수 있습니다).
PROCESS_CALLS = frozenset({
    "os.system", "os.popen", "os.startfile", "os.fork", "os.forkpty", "os.kill",
    "os.posix_spawn", "os.posix_spawnp", "pty.spawn",
})
PROCESS_CALL_PREFIXES = ("os.exec", "os.spawn", "subprocess.")

# 첫 인자가 지울 대상인 호출.
DELETE_CALLS = frozenset({
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree",
    "send2trash.send2trash",
})
# `Path(...).unlink()` 처럼 받는 쪽이 대상인 메서드.
DELETE_METHODS = frozenset({"unlink", "rmdir", "rmtree"})

# (원본, 대상) 을 받는 호출. 원본을 옮기는 것은 쓰기, 복사하는 것은 읽기입니다.
MOVE_CALLS = frozenset({"shutil.move", "os.rename", "os.replace", "os.renames"})
COPY_CALLS = frozenset({
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
    "os.link", "os.symlink",
})

# `Path` 의 메서드 — 받는 쪽이 경로입니다 (`Path("a.md").write_text(내용)`).
RECEIVER_WRITE_METHODS = frozenset({"write_text", "write_bytes", "mkdir", "touch"})
RECEIVER_READ_METHODS = frozenset({"read_text", "read_bytes", "iterdir", "glob", "rglob"})
# 첫 인자가 경로인 메서드 — pandas·matplotlib·openpyxl·numpy·os 의 저장과 읽기.
ARG_WRITE_METHODS = frozenset({
    "to_csv", "to_excel", "to_json", "to_parquet", "to_pickle", "to_html", "to_markdown",
    "to_feather", "savefig", "save", "write_html", "write_image", "makedirs",
})
ARG_READ_METHODS = frozenset({
    "read_csv", "read_excel", "read_json", "read_parquet", "read_pickle", "read_table",
    "read_html", "read_feather", "load", "loadtxt", "genfromtxt", "load_workbook",
    "listdir", "scandir", "walk",
})

_URL_RE = re.compile(r"^(?:https?|ftp|wss?)://(?:[^@/\s]*@)?(\[[^\]]+\]|[^/\s:?#]+)", re.I)
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_FILE_NAME_RE = re.compile(r"^[\w.-]+\.[A-Za-z0-9]{1,8}$")

# 한 번의 검사에서 모을 경로의 상한. 거대한 데이터 리터럴이 판정 카드를 뒤덮지 않게 합니다.
MAX_MENTIONS = 64


@dataclass
class ScanResult:
    """코드가 건드리는 것들.

    `reads`/`writes`/`deletes` 는 코드에 **글자로 적힌** 경로입니다. 대상이 변수라 알 수
    없는 삭제는 빈 문자열 하나로 남습니다 — 무엇을 지우는지 모르는 삭제도 삭제입니다.
    `hosts` 는 코드에 적힌 URL 의 호스트이고, `network` 는 네트워크 모듈을 쓰는지입니다.
    `flags` 는 판정할 수 없어 사람이 봐야 하는 소견입니다.
    """

    parsed: bool = True
    reads: List[str] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    deletes: List[str] = field(default_factory=list)
    hosts: List[str] = field(default_factory=list)
    network: bool = False
    flags: List[str] = field(default_factory=list)

    @property
    def uncertain(self) -> bool:
        return bool(self.flags)


def looks_like_path(text: str) -> bool:
    """문자열이 파일 경로처럼 생겼는지. 판정에 쓸 후보만 고릅니다.

    `"/".join(...)` 의 `"/"` 나 f-문자열 조각까지 경로로 보면 평범한 코드가 매번
    작업 공간 밖 접근으로 걸립니다. 그래서 너무 짧거나 공백이 섞인 문장은 뺍니다.
    """
    if not text or len(text) > 260 or "\n" in text or "\r" in text or "\x00" in text:
        return False
    if _URL_RE.match(text):
        return False
    if not re.search(r"[\w.]", text) or len(text) < 2:
        return False
    if "/" in text or "\\" in text:
        if not re.search(r"\s", text):
            return True
        # 공백이 든 경로는 절대 경로일 때만 받습니다 ("C:/Program Files/...").
        return bool(_WINDOWS_ABS_RE.match(text) or text.startswith(("/", "~")))
    if text.startswith(".") and not re.search(r"\s", text):
        return True  # ".env", ".ssh"
    return bool(_FILE_NAME_RE.match(text))


def url_host(text: str) -> Optional[str]:
    """URL 이면 호스트(소문자), 아니면 None."""
    match = _URL_RE.match(text or "")
    if not match:
        return None
    return match.group(1).strip("[]").lower()


def _dotted(node: ast.AST) -> str:
    """`os.path.join` 같은 이름을 점 이름으로. 모르는 모양이면 빈 문자열."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _literal(node: Optional[ast.AST]) -> Optional[str]:
    """글자 그대로의 문자열이면 그 값. `Path("x")` · `"a" / "b"` 도 풉니다."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] in {
        "Path", "PurePath", "PosixPath", "WindowsPath", "open",
    } and node.args:
        return _literal(node.args[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return f"{left.rstrip('/')}/{right.lstrip('/')}"
    return None


def _open_mode(call: ast.Call) -> str:
    """`open(path, mode)` 의 모드. 모르면 읽기로 봅니다."""
    mode_node: Optional[ast.AST] = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    mode = _literal(mode_node) if mode_node is not None else "r"
    return mode or "r"


class _Scanner:
    """AST 를 세 번 훑습니다 — 가져오기·호출을 먼저, 떠도는 글자를 나중에.

    순서가 반대면 `open(".env", "w")` 의 ".env" 가 먼저 "읽기 언급" 으로 잡혀, 쓰기로
    연 경우도 읽기로 남습니다.
    """

    def __init__(self) -> None:
        self.result = ScanResult()
        self._seen: Set[Tuple[str, str]] = set()
        # 판정에 쓰지 않을 문자열 노드 (f-문자열 조각, 독스트링).
        self._skip: Set[int] = set()

    def run(self, tree: ast.AST) -> ScanResult:
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                self._skip.add(id(node.value))  # 독스트링과 떠도는 문자열은 코드가 아닙니다
            elif isinstance(node, ast.JoinedStr):
                # f"{base}/out.csv" 의 "/out.csv" 는 절대 경로가 아니라 조각입니다.
                self._skip.update(id(part) for part in node.values)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._module(alias.name)
            elif isinstance(node, ast.ImportFrom):
                self._module(node.module or "")
            elif isinstance(node, ast.Call):
                self._call(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                self._constant(node)
        return self.result

    # -------------------------------------------------- 기록

    def _add(self, bucket: str, value: str) -> None:
        key = (bucket, value)
        if key in self._seen:
            return
        total = len(self.result.reads) + len(self.result.writes) + len(self.result.deletes)
        if total >= MAX_MENTIONS and value:
            return
        self._seen.add(key)
        getattr(self.result, bucket).append(value)

    def _flag(self, text: str) -> None:
        if text not in self.result.flags:
            self.result.flags.append(text)

    def _path_arg(self, node: Optional[ast.AST], bucket: str) -> bool:
        value = _literal(node)
        if value is not None and value.strip():
            self._add(bucket, value.strip())
            return True
        return False

    # -------------------------------------------------- 가져오기

    def _module(self, name: str) -> None:
        head = (name or "").split(".")[0]
        if head in NET_MODULES:
            self.result.network = True
        if head in PROCESS_MODULES:
            self._flag(f"프로세스·네이티브 모듈 `{head}` 사용")
        if head == "importlib":
            self._flag("`importlib` 로 모듈을 동적으로 불러옴")

    # -------------------------------------------------- 호출

    def _call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        tail = name.split(".")[-1] if name else (
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        )
        first = node.args[0] if node.args else None
        second = node.args[1] if len(node.args) > 1 else None
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None

        if name in DYNAMIC_CALLS:
            self._flag(f"`{name}` 로 문자열 코드를 실행함")
        if name in PROCESS_CALLS or name.startswith(PROCESS_CALL_PREFIXES):
            self._flag(f"`{name}` 로 프로세스를 실행함")

        if name in {"open", "io.open", "codecs.open"}:
            mode = _open_mode(node)
            self._path_arg(first, "writes" if any(c in mode for c in "wax+") else "reads")
        elif name in DELETE_CALLS:
            if not self._path_arg(first, "deletes"):
                self._add("deletes", "")
        elif tail in DELETE_METHODS and receiver is not None:
            if not self._path_arg(receiver, "deletes"):
                self._add("deletes", "")
        elif name in MOVE_CALLS:
            self._path_arg(first, "writes")
            self._path_arg(second, "writes")
        elif name in COPY_CALLS:
            self._path_arg(first, "reads")
            self._path_arg(second, "writes")
        elif tail in RECEIVER_WRITE_METHODS and receiver is not None:
            self._path_arg(receiver, "writes")
        elif tail in RECEIVER_READ_METHODS and receiver is not None:
            self._path_arg(receiver, "reads")
        elif tail in ARG_WRITE_METHODS:
            self._path_arg(first, "writes")
        elif tail in ARG_READ_METHODS:
            self._path_arg(first, "reads")

    # -------------------------------------------------- 글자

    def _constant(self, node: ast.Constant) -> None:
        if id(node) in self._skip or not isinstance(node.value, str):
            return
        value = node.value.strip()
        host = url_host(value)
        if host:
            if host not in self.result.hosts:
                self.result.hosts.append(host)
            return
        if not looks_like_path(value):
            return
        if any((bucket, value) in self._seen for bucket in ("reads", "writes", "deletes")):
            return
        # 어디에 쓰이는지 모르는 경로는 "읽기" 로 봅니다. 규칙과 고정 보호가 이 경로를
        # 보게 하는 것이 목적입니다 — 변수에 담았다가 여는 경우도 여기서 잡힙니다.
        self._add("reads", value)


def scan_python(code: str) -> ScanResult:
    """코드를 읽어 건드리는 것들을 추립니다. 실행하지 않습니다."""
    text = code or ""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        # IPython 매직(`%pip`)이나 셸 탈출(`!ls`)이 섞였거나, 코드가 깨졌습니다.
        # 어느 쪽이든 무엇을 할지 읽을 수 없습니다.
        result = ScanResult(parsed=False)
        result.flags.append(
            f"구문 분석 실패 — 셸 명령이나 IPython 매직이 섞였을 수 있음 ({type(exc).__name__})"
        )
        for token in re.findall(r"[^\s'\"()\[\],;=]+", text):
            host = url_host(token)
            if host and host not in result.hosts:
                result.hosts.append(host)
        return result
    return _Scanner().run(tree)


def describe(result: ScanResult) -> List[str]:
    """판정 카드에 적을 소견 목록."""
    notes = list(result.flags)
    if result.network and not result.hosts:
        notes.append("네트워크 모듈 사용")
    return notes


def iter_mentions(result: ScanResult) -> Iterable[Tuple[str, str]]:
    """(행위, 경로) 쌍. 행위는 read | write | delete."""
    for path in result.reads:
        yield "read", path
    for path in result.writes:
        yield "write", path
    for path in result.deletes:
        yield "delete", path
