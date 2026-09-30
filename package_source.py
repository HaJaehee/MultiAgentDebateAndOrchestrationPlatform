"""소스·설정만 담는 갱신용 패키지 스크립트.

`package_offline.py` 가 만드는 전체 번들은 포터블 파이썬, node.exe, wheel,
MCP 서버 설치본까지 들고 있어 수백 MB 입니다. 그 런타임은 한 번 반입하면
버전을 올릴 때까지 그대로 쓰면 됩니다. 코드만 고쳤을 때 그걸 매번 다시
반입하는 것은 용량도 용량이지만, 반입 심사를 매번 처음부터 다시 받는 일입니다.

이 스크립트는 **런타임을 제외한** 소스와 설정만 묶습니다.

    dist/MultiAgentDebateOrchestration_source_YYYYMMDD.zip
    └── MultiAgentDebateOrchestration_source/
        ├── app/                      애플리케이션 소스 (통째로 교체)
        ├── mcp_servers/              이 저장소가 직접 들고 있는 MCP 서버
        │   └── memory_scoped/        공식 memory 서버 포크 (대화별 지식 그래프)
        ├── mcp_node/memory-scoped.mjs  그 실행 사본 (설치본의 것을 바로 갈아끼움)
        ├── trial_templates/          체험 서버의 공식 템플릿 (trial.templates_dir)
        ├── skills/                   기본 스킬 디렉터리 (skills.dir)
        ├── wheels/                   --with-wheels 로 지정했을 때만 (아래 참고)
        ├── docs/
        │   ├── user_manual/          사용 설명서 마크다운 원본
        │   ├── user_manual_html/     그것을 패키징 시점에 렌더링한 HTML (바로 열람)
        │   └── render_user_manual.py 렌더러 (대상에서 다시 돌릴 수 있게)
        ├── conf.example.json         설정 템플릿
        ├── .env.example
        ├── requirements.txt
        ├── setup_mcp.py
        ├── open_browser.py
        ├── README.md
        ├── LICENSE.md               라이선스 (LGPL-3.0 전문 + 제3자 고지)
        └── MANIFEST.txt              파일별 SHA-256 (반입 심사·무결성 확인용)

사용법:
    python package_source.py [--out-dir dist] [--max-file-mb 2] [--allow-secrets]
                             [--skip-manual-html] [--with-wheels PKG [PKG ...]]

포함 목록은 **허용 목록(allow-list)** 입니다. 제외 목록으로 짜면 새 디렉터리가
생겼을 때 조용히 딸려 들어갑니다. 여기서는 새 디렉터리가 생기면 그냥 빠지고,
빠진 것은 눈에 띕니다.

## 새 pip 의존성이 생겼을 때 (`--with-wheels`)

이 패키지는 소스만 담으므로 `requirements.txt` 를 고쳐 보내도 대상 장비에는
**그 패키지가 설치되지 않습니다.** 폐쇄망이라 `pip install` 이 레지스트리에 닿지
못하기 때문입니다. 그 결과가 특히 고약한 쪽은 MCP 서버입니다 — `conf.json` 은
서버를 가리키는데 import 가 실패해 기동만 안 되고, 화면에는 "연결 안 됨" 한 줄로만
보입니다. 기능이 조용히 빠집니다.

그래서 이번 갱신에서 **새로 추가된 패키지만** 골라 wheel 로 함께 실을 수 있게
했습니다. 런타임 전체를 다시 싣는 것과는 다릅니다 (그건 `package_offline.py`).

    python package_source.py --with-wheels mcp-server-fetch

대상 장비에서:

    python_runtime\\python.exe -m pip install --no-index --find-links wheels mcp-server-fetch
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from datetime import datetime
from pathlib import Path

# 콘솔/파이프의 인코딩이 UTF-8 이 아니면(윈도우 기본 cp949) 아래 로그에 쓰인
# em-dash 나 이모지가 UnicodeEncodeError 로 스크립트를 끝냅니다. 산출물을 다
# 만들어 놓고 마지막 안내 문구에서 죽으므로, 성공한 실행이 실패로 보입니다.
# 출력 스트림을 UTF-8 로 돌리고, 그래도 못 찍는 문자는 대체 문자로 흘립니다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass  # 리다이렉트된 스트림이 reconfigure 를 지원하지 않는 경우


ROOT_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = "MultiAgentDebateOrchestration_source"

# --- 무엇을 담는가 (허용 목록) -------------------------------------------------

# mcp_servers/ 는 포크한 MCP 서버 원본입니다 (실행 사본은 앱이 mcp_node 에 놓습니다).
#
# docs/ 는 사용 설명서입니다. 폐쇄망에서는 위키나 저장소를 열 수 없으므로 설명서가
# 설치본과 같이 다녀야 하고, 받는 쪽이 아무것도 실행하지 않아도 읽을 수 있어야
# 합니다. 그래서 마크다운 원본·렌더러와 함께 **패키징 시점에 렌더링한 HTML** 을
# 담습니다 (`render_manual_html`). 전체 번들(`package_offline.py`)이 이미 그렇게
# 하고 있고, 소스 갱신 패키지만 예외였습니다.
#
# trial_templates/ 는 체험 서버의 공식 템플릿입니다. 앱 코드처럼 통째로 갈아끼우므로, 운영자가
# 고친 템플릿을 갱신에서 지키려면 conf.json 의 `trial.templates_dir` 를 다른 폴더로 두면 됩니다.
#
# skills/ 디렉터리는 기본 제공 스킬을 포함합니다. 소스 업데이트 패키지 적용 시 템플릿과 마찬가지로
# 전체가 교체되므로, 직접 작성하신 스킬을 보존하시려면 conf.json 의 `skills.dir` 를 다른 폴더로 지정하시기 바랍니다.
SOURCE_DIRS = ["app", "mcp_servers", "docs", "trial_templates", "skills"]

# (원본 경로, 패키지 안에서의 경로). 대부분 루트 파일이지만, 설치본의 같은
# 자리에 바로 놓여야 하는 파일은 하위 경로로 넣습니다.
#
# 여기 없는 것들: 이 패키지는 **돌아가는 앱을 갱신하고 쓰는 데 필요한 것만** 담습니다.
# 테스트·위키·CLAUDE.md 는 개발 저장소에 있고, 패키징 스크립트는 만드는 쪽 도구이며,
# conf.json 은 그 망의 실제 엔드포인트가 들어 있어 반입 대상이 아닙니다.
PACKAGE_FILES: list[tuple[str, str]] = [
    ("requirements.txt", "requirements.txt"),
    ("conf.example.json", "conf.example.json"),
    (".env.example", ".env.example"),
    ("setup_mcp.py", "setup_mcp.py"),
    # 실행 스크립트가 백그라운드로 띄우는 브라우저 대기 스크립트.
    ("open_browser.py", "open_browser.py"),
    ("README.md", "README.md"),
    # 라이선스는 반드시 배포물과 함께 다녀야 합니다 (LGPL-3.0 제4조 고지 의무).
    ("LICENSE.md", "LICENSE.md"),
    # 포크한 memory 서버의 실행 사본. 원본(mcp_servers/)만 넣어도 앱이 기동할 때
    # 다시 복사하지만, 설치본의 파일을 바로 갈아끼울 수 있도록 함께 담습니다.
    ("mcp_node/memory-scoped.mjs", "mcp_node/memory-scoped.mjs"),
]

# 반드시 패키지에 들어 있어야 하는 파일. 모두 SOURCE_DIRS(app/) 안에 있어 따로 담지는 않지만
# (PACKAGE_FILES 에 또 적으면 두 번 들어갑니다), 빠지면 폐쇄망에서 기능이 조용히 죽는 것들이라
# 패키징할 때 확인합니다. 아래 무시·금지 규칙이 바뀌어 이 파일들이 걸러지면 패키징을 멈춥니다.
#
# graph_editor/ 는 그래프 토론 편집기가 쓰는 Vue Flow 묶음입니다. CDN 을 쓸 수 없는 망이라
# 저장소에 싣습니다 (다시 만드는 법: app/ui/static/graph_editor/BUILD.md). 라이선스 고지는
# 배포물과 함께 다녀야 합니다 (MIT · ISC · BSD-3-Clause). 폴더 이름을 `vendor` 로 하면 아래
# FORBIDDEN_NAMES 에 걸립니다.
#
# mcp_servers/ 아래의 서버들은 `conf.json` 이 **경로나 주소로** 가리킵니다. 파일이
# 빠지면 설정은 그대로인데 서버만 기동하지 못하고, 화면에는 "연결 안 됨" 한 줄로
# 보입니다. 기능이 조용히 빠지는 모양이라 여기서 확인합니다.
REQUIRED_PACKAGE_PATHS: list[str] = [
    "app/ui/static/graph_editor/index.js",
    "app/ui/static/graph_editor/vue-flow.css",
    "app/ui/static/graph_editor/THIRD_PARTY_NOTICES.txt",
    "app/ui/static/graph_editor/BUILD.md",
    "mcp_servers/memory_scoped/index.mjs",
    "trial_templates/report-review.json",
    "skills/mermaid-diagrams/SKILL.md",
    "skills/csv-profile/scripts/profile_csv.py",
]

# 디렉터리를 복사할 때 건너뛸 것들. 소스 트리 안에 런타임 부스러기가 섞이는 것을 막습니다.
IGNORE_PATTERNS = [
    "__pycache__", "*.pyc", "*.pyo", "*.egg-info",
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    "*.log", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".DS_Store", "Thumbs.db",
]

# 절대 들어가면 안 되는 것. 실수로 SOURCE_DIRS 에 추가되더라도 여기서 걸립니다.
FORBIDDEN_NAMES = {
    "python_runtime", "node_runtime", "wheels", "wheelhouse", "vendor",
    "mcp_node", "mcp_sandbox", "workspace", "dist", "build",
    ".git", ".venv", "venv", "env", "node_modules",
    # **작업 트리의** docs/user_manual_html/ 입니다. 패키지에 담기는 HTML 은 여기서
    # 긁어 오지 않고 `render_manual_html` 이 매번 새로 만듭니다 — 이 폴더는 언제
    # 만들어진 것인지 알 수 없어서, 마크다운을 고치고 렌더러를 돌리지 않았으면 낡은
    # HTML 이 원본과 어긋난 채로 반입됩니다. 매번 새로 만들면 어긋날 수가 없습니다.
    "user_manual_html",
}

# 반입 심사 전에 걸러야 할 것들. conf.json 은 gitignore 대상이라
# 누군가 실제 키를 적어 두었을 수 있습니다.
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "OpenAI 계열 API 키"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API 키"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{30,}"), "Google API 키"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub 토큰"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "Slack 토큰"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "개인 키"),
]

# 렌더링한 설명서(.html)도 봅니다. 마크다운과 같은 글이 들어 있으므로 원본만
# 검사하고 넘어가면 같은 값이 HTML 로 새어 나갑니다.
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".ps1", ".bat", ".json", ".yml", ".yaml", ".example", ".svg",
                 ".js", ".mjs", ".html"}


def log(message: str) -> None:
    print(message, flush=True)


def is_ignored(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pat) for pat in IGNORE_PATTERNS)


def collect_dir(src: Path, rel_root: str) -> list[tuple[Path, str]]:
    """디렉터리 하나에서 담을 파일을 모읍니다. (원본 경로, 패키지 내 상대경로)"""
    collected: list[tuple[Path, str]] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(src).parts
        if any(p in FORBIDDEN_NAMES for p in parts) or any(is_ignored(Path(p)) for p in parts):
            continue
        if is_ignored(path):
            continue
        collected.append((path, f"{rel_root}/{path.relative_to(src).as_posix()}"))
    return collected


def render_manual_html(build_dir: Path) -> list[tuple[Path, str]]:
    """설명서를 `build_dir` 에 **새로** 렌더링하고 담을 파일 목록을 돌려줍니다.

    작업 트리의 `docs/user_manual_html/` 을 그대로 쓰지 않습니다. 그 폴더는 언제
    만들어진 것인지 알 수 없습니다 — 마크다운을 고치고 렌더러를 돌리지 않았다면
    낡은 HTML 이 그대로 들어가고, **한 패키지 안에서 원본과 산출물이 서로 다른 말을
    합니다.** 받는 쪽은 어느 쪽이 정본인지 알 방법이 없습니다. 매번 새로 만들면
    그런 일이 생길 수 없습니다.

    렌더링에 실패해도 패키지 생성을 막지는 않습니다. 설명서는 앱이 도는 데 필요한
    것이 아니고 마크다운 원본은 이미 담겨 있으므로, 대상에서 렌더러를 한 번 돌리면
    됩니다 (`package_offline.py` 와 같은 태도입니다). 다만 조용히 빠지면 안 되므로
    경고는 남깁니다.
    """
    src_manual = ROOT_DIR / "docs" / "user_manual"
    renderer = ROOT_DIR / "docs" / "render_user_manual.py"
    if not src_manual.is_dir() or not renderer.is_file():
        log("  [건너뜀] 설명서 원본이나 렌더러가 없어 HTML 을 만들지 않습니다.")
        return []

    if build_dir.exists():
        shutil.rmtree(build_dir)
    try:
        subprocess.run(
            [sys.executable, str(renderer),
             "--src", str(src_manual), "--out", str(build_dir), "--clean"],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        log(f"  [경고] 설명서 HTML 렌더링 실패 (마크다운 원본은 담습니다): {exc}")
        if detail:
            log(f"          {detail.decode('utf-8', 'replace').strip().splitlines()[-1]}")
        return []

    found = collect_dir(build_dir, "docs/user_manual_html")
    log(f"  docs/user_manual_html/  {len(found)}개 (이번에 렌더링)")
    return found


def collect_wheels(packages: list[str], build_dir: Path) -> list[tuple[Path, str]]:
    """이번 갱신이 새로 요구하는 패키지만 wheel 로 받아 담습니다.

    `FORBIDDEN_NAMES` 에 `wheels` 가 있는 것과 모순처럼 보이지만 그렇지 않습니다.
    그 규칙은 "소스 트리를 훑다가 런타임이 **실수로** 딸려 들어가는 것" 을 막습니다.
    여기 들어오는 wheel 은 명령줄에 이름을 적어야만 생기는, 의도한 예외입니다.
    그래서 `collect_dir` 을 거치지 않고 따로 모읍니다.

    담는 범위를 `requirements.txt` 전체가 아니라 **지정한 패키지** 로 좁힌 것도
    같은 이유입니다. 전체를 담으면 이 패키지가 사실상 오프라인 번들이 되어,
    "런타임은 한 번만 반입한다" 는 이 스크립트의 존재 이유가 없어집니다.
    """
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    requirements = ROOT_DIR / "requirements.txt"
    command = [sys.executable, "-m", "pip", "download",
               "--only-binary=:all:", "-d", str(build_dir)]
    if requirements.is_file():
        # 버전을 requirements.txt 와 맞춥니다. 대상 장비에 이미 있는 패키지와
        # 어긋난 버전을 실어 보내면 설치 단계에서 해석이 꼬입니다.
        command += ["-c", str(requirements)]
    command += packages

    log(f"  wheel 내려받는 중: {', '.join(packages)}")
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = (getattr(exc, "stderr", b"") or b"").decode("utf-8", "replace").strip()
        log(f"\n[중단] wheel 을 받지 못했습니다: {exc}")
        if detail:
            log(f"  {detail.splitlines()[-1]}")
        sys.exit(
            "인터넷이 되는 PC 에서 실행해야 합니다. wheel 없이 소스만 보내려면 "
            "--with-wheels 를 빼고 다시 실행하세요."
        )

    if not any(build_dir.glob("*.whl")):
        sys.exit("wheel 이 하나도 받아지지 않았습니다. 패키지 이름을 확인하세요.")

    (build_dir / "INSTALL.txt").write_text(
        "이 폴더의 wheel 은 이번 갱신이 새로 요구하는 패키지입니다.\n"
        "설치본 루트에서 아래를 실행한 뒤 앱을 다시 띄우세요.\n"
        "\n"
        f"  python_runtime\\python.exe -m pip install --no-index --find-links wheels "
        f"{' '.join(packages)}\n"
        "\n"
        "포터블 런타임을 쓰지 않는 설치본이라면 python.exe 자리에 그 환경의\n"
        "인터프리터를 넣으세요. --no-index 가 있어 네트워크에 닿지 않습니다.\n"
        "\n"
        f"이 wheel 은 {sys.implementation.name} {sys.version_info.major}."
        f"{sys.version_info.minor} / {sysconfig.get_platform()} 기준으로 받았습니다.\n"
        "설치본의 인터프리터가 다른 버전이면 순수 파이썬이 아닌 wheel(pyzmq 등)이\n"
        "설치되지 않습니다. 그때는 같은 버전의 PC 에서 다시 묶으세요.\n",
        encoding="utf-8",
    )

    found = sorted(p for p in build_dir.glob("*") if p.is_file())
    total = sum(p.stat().st_size for p in found)
    log(f"  wheels/  {len(found)}개 ({total / 1024 / 1024:.1f} MB)")
    return [(p, f"wheels/{p.name}") for p in found]


def scan_for_secrets(items: list[tuple[Path, str]]) -> list[str]:
    """반입 전에 걸러야 할 값이 섞여 있는지 봅니다."""
    findings: list[str] = []
    for src, rel in items:
        if src.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{rel}:{line}  {label} 로 보이는 값")
    return findings


def build_manifest(items: list[tuple[Path, str]]) -> str:
    """파일별 SHA-256. 반입 심사 기록과 반입 후 무결성 확인에 씁니다."""
    lines = [
        f"# {PACKAGE_NAME}",
        f"# generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"# files: {len(items)}",
        f"# bytes: {sum(src.stat().st_size for src, _ in items)}",
        "#",
        "# sha256                                                            size  path",
    ]
    for src, rel in sorted(items, key=lambda it: it[1]):
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        lines.append(f"{digest}  {src.stat().st_size:>9}  {rel}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="런타임을 뺀 소스·설정만 dist/ 에 압축합니다."
    )
    parser.add_argument("--out-dir", default="dist", help="산출물 위치 (기본: dist)")
    parser.add_argument("--max-file-mb", type=float, default=2.0,
                        help="이보다 큰 파일이 있으면 중단 (런타임 혼입 방지, 기본: 2MB)")
    parser.add_argument("--allow-secrets", action="store_true",
                        help="키처럼 보이는 값이 있어도 강행")
    parser.add_argument("--skip-manual-html", action="store_true",
                        help="설명서 HTML 렌더링을 건너뜁니다 (마크다운 원본은 그대로 담깁니다)")
    parser.add_argument("--with-wheels", nargs="+", metavar="PKG", default=[],
                        help="이번 갱신이 새로 요구하는 pip 패키지를 wheel 로 함께 담습니다 "
                             "(예: --with-wheels mcp-server-fetch). 인터넷 필요")
    args = parser.parse_args()

    dist_dir = (ROOT_DIR / args.out_dir).resolve() if not Path(args.out_dir).is_absolute() \
        else Path(args.out_dir)
    staging = dist_dir / PACKAGE_NAME
    zip_path = dist_dir / f"{PACKAGE_NAME}_{datetime.now():%Y%m%d}.zip"

    # --- 1. 담을 것 모으기 ---------------------------------------------------
    items: list[tuple[Path, str]] = []
    for name in SOURCE_DIRS:
        src = ROOT_DIR / name
        if not src.is_dir():
            log(f"  [건너뜀] {name}/ 이 없습니다.")
            continue
        found = collect_dir(src, name)
        items.extend(found)
        log(f"  {name}/  {len(found)}개")

    single_files = 0
    for src_name, dest_name in PACKAGE_FILES:
        src = ROOT_DIR / src_name
        if not src.is_file():
            log(f"  [건너뜀] {src_name} 이 없습니다.")
            continue
        items.append((src, dest_name))
        single_files += 1
    log(f"  개별 파일  {single_files}개")

    # 설명서 HTML 은 여기서 만들어 함께 담습니다. 나머지와 같은 목록에 넣어야
    # 크기 검사·비밀값 검사·MANIFEST 를 똑같이 거칩니다 — 반입 심사용 목록에
    # 빠진 파일이 패키지 안에 들어 있으면 안 됩니다.
    manual_build = dist_dir / f".{PACKAGE_NAME}_manual_html"
    if args.skip_manual_html:
        log("  [건너뜀] --skip-manual-html")
    else:
        dist_dir.mkdir(parents=True, exist_ok=True)
        items.extend(render_manual_html(manual_build))

    # wheel 은 맨 마지막에 붙입니다. 아래의 필수 파일 확인·비밀값 검사·MANIFEST 는
    # 그대로 거치지만, 크기 상한만 면제됩니다 (2MB 를 넘는 것이 정상입니다).
    wheel_build = dist_dir / f".{PACKAGE_NAME}_wheels"
    wheel_items: list[tuple[Path, str]] = []
    if args.with_wheels:
        dist_dir.mkdir(parents=True, exist_ok=True)
        wheel_items = collect_wheels(list(args.with_wheels), wheel_build)
        items.extend(wheel_items)

    if not items:
        sys.exit("담을 파일이 없습니다. 프로젝트 루트에서 실행하고 있습니까?")

    packed = {rel for _src, rel in items}
    missing = [path for path in REQUIRED_PACKAGE_PATHS if path not in packed]
    if missing:
        log("\n[중단] 반드시 담겨야 할 파일이 빠졌습니다:")
        for path in missing:
            log(f"  {path}")
        sys.exit("파일이 있는지, 무시·금지 규칙(IGNORE_PATTERNS · FORBIDDEN_NAMES)에 걸리지 않는지 확인하세요.")

    # --- 2. 검사 -------------------------------------------------------------
    limit = int(args.max_file_mb * 1024 * 1024)
    exempt = {rel for _src, rel in wheel_items}  # wheel 은 크지만 의도한 것입니다
    oversized = [(rel, src.stat().st_size) for src, rel in items
                 if rel not in exempt and src.stat().st_size > limit]
    if oversized:
        log(f"\n[중단] 상한({args.max_file_mb} MB)을 넘는 파일이 있습니다:")
        for rel, size in sorted(oversized, key=lambda it: -it[1])[:20]:
            unit = f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.0f} KB"
            log(f"  {rel}  ({unit})")
        if len(oversized) > 20:
            log(f"  ... 외 {len(oversized) - 20}개")
        sys.exit("런타임 파일이 섞였는지 확인하거나 --max-file-mb 를 올리세요.")

    findings = scan_for_secrets(items)
    if findings:
        log("\n[경고] 키로 보이는 값이 있습니다. 폐쇄망 반입 심사에서 문제가 됩니다:")
        for f in findings:
            log(f"  {f}")
        if not args.allow_secrets:
            sys.exit("해당 값을 ${ENV_VAR} 로 바꾸거나, 알고 있다면 --allow-secrets 로 실행하세요.")
        log("  --allow-secrets 로 강행합니다.")

    # --- 3. 스테이징 ---------------------------------------------------------
    if staging.exists():
        shutil.rmtree(staging)
    for src, rel in items:
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    (staging / "MANIFEST.txt").write_text(build_manifest(items), encoding="utf-8")

    # --- 4. 압축 -------------------------------------------------------------
    dist_dir.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, f"{PACKAGE_NAME}/{path.relative_to(staging).as_posix()}")

    total = sum(src.stat().st_size for src, _ in items)
    if manual_build.exists():
        shutil.rmtree(manual_build, ignore_errors=True)
    if wheel_build.exists():
        shutil.rmtree(wheel_build, ignore_errors=True)

    log("")
    log(f"  스테이징 : {staging}")
    log(f"  압축     : {zip_path}")
    log(f"  파일     : {len(items) + 1}개 (원본 {len(items)} + MANIFEST.txt)")
    log(f"  원본 크기: {total / 1024:.0f} KB  ->  압축 {zip_path.stat().st_size / 1024:.0f} KB")
    log("")
    if wheel_items:
        log(f"  wheel     : {len(wheel_items)}개 — 대상에서 wheels/INSTALL.txt 를 먼저 보세요.")
        log("")
    log("  런타임(python_runtime, node_runtime, mcp_sandbox)과")
    log("  운영 데이터(workspace, multiagent.db, conf.json)는 들어 있지 않습니다.")
    if not wheel_items:
        log("  requirements.txt 에 패키지를 새로 넣었다면 이 패키지만으로는 설치되지")
        log("  않습니다 (폐쇄망은 pip 가 레지스트리에 닿지 못합니다).")
        log("  --with-wheels <패키지…> 로 다시 묶으면 wheel 을 함께 담습니다.")
    log("  설명서는 docs/user_manual_html/index.html 을 브라우저로 열면 됩니다")
    log("  (이번 패키징 시점에 마크다운 원본에서 새로 렌더링했습니다).")
    log("  conf.json 은 담기지 않습니다 — conf.example.json 에 이번에 새 항목이")
    log("  생겼다면(MCP 서버·에이전트 등) 대상의 conf.json 에 직접 옮기세요.")
    log("  대상 장비에서는 압축을 푼 내용을 설치본 위에 덮어쓰세요.")
    log("  app/ 은 파일 단위로 덮지 말고 통째로 교체해야 합니다 —")
    log("  이번 갱신에서 삭제된 모듈이 남아 계속 import 됩니다.")


if __name__ == "__main__":
    main()
