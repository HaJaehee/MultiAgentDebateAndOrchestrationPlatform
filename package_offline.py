"""폐쇄망(에어갭) 배포용 번들 패키징 스크립트.

생성되는 번들에는 다음이 모두 포함되어 인터넷 없이 즉시 구동됩니다.

    MultiAgentDebateOrchestration_bundle/
    ├── app/                 애플리케이션 소스
    ├── conf.json            설정 (없으면 conf.example.json 에서 복사)
    ├── LICENSE.md           라이선스 (LGPL-3.0 전문 + 제3자 고지)
    ├── wheels/              오프라인 pip wheel (앱 + MCP 서버 + 샌드박스 서버)
    ├── python_runtime/      포터블 CPython
    ├── node_runtime/        node.exe (MCP 공식 Node 서버 구동용, npm 미포함)
    ├── mcp_node/            filesystem / memory / sequential-thinking MCP 서버
    ├── mcp_servers/         포크한 MCP 서버 원본 (memory: 대화별 지식 그래프)
    ├── mcp_sandbox/         AirgappedPySandbox (Python 코드 실행 MCP 서버)
    ├── trial_templates/     체험 서버의 공식 템플릿 (trial.templates_dir)
    ├── skills/              기본 스킬 디렉터리 (skills.dir)
    ├── docs/                사용 설명서 (마크다운 원본 + 렌더링된 HTML)
    ├── workspace/           에이전트 공용 작업 공간
    └── run_mado.bat|ps1  실행 스크립트 (MCP 경로 환경변수 자동 주입)

사용법:
    python package_offline.py [--skip-node] [--node-version 22.22.2]
                              [--sandbox-src <경로>] [--skip-sandbox]

이 스크립트는 **Windows 에서 실행**하는 것을 전제로 합니다. 포터블 런타임과
pip wheel 이 실행 플랫폼 기준으로 수집되기 때문입니다. 패키징 시점에는 인터넷이
필요하며(pip download / npm install / node 내려받기), 산출물 실행 시점에는
필요하지 않습니다.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

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
DIST_DIR = ROOT_DIR / "dist"
STAGING_DIR = DIST_DIR / "MultiAgentDebateOrchestration_bundle"
ZIP_FILE = DIST_DIR / "MultiAgentDebateOrchestration_offline.zip"

# 번들에 포함할 Node 런타임. 공식 MCP 서버들은 순수 JS 라 node.exe 하나면 돌아가며
# npm / npx 는 런타임에 전혀 필요하지 않습니다.
DEFAULT_NODE_VERSION = "22.22.2"
NODE_DIST_URL = "https://nodejs.org/dist/v{ver}/node-v{ver}-win-x64.zip"

# 공식 레퍼런스 MCP 서버 (TypeScript 구현). 네이티브 애드온이 없어 어느 OS 에서
# 설치해도 그대로 재배치됩니다.
NODE_MCP_PACKAGES = [
    "@modelcontextprotocol/server-filesystem",
    "@modelcontextprotocol/server-memory",
    "@modelcontextprotocol/server-sequential-thinking",
]

# 공식 레퍼런스 MCP 서버 중 Python 구현. 기존 wheel 파이프라인에 그대로 얹힙니다.
PYTHON_MCP_PACKAGES = [
    "mcp-server-fetch",
    "mcp-server-git",
]

# Python 코드 실행 샌드박스 MCP 서버.
SANDBOX_REPO_URL = "https://github.com/HaJaehee/AirgappedPySandbox"

# 번들 전체에 적용할 버전 제약. 벤더링한 샌드박스의 requirements-server.txt 는
# `mcp>=1.2.0` 으로 상한이 없어, 그대로 두면 pip 가 mcp 2.x 를 끌어옵니다.
# mcp 2.x 는 mcp.server.fastmcp 를 제거했으므로 샌드박스 서버가 기동하지 못합니다.
PIP_CONSTRAINTS = [
    "mcp>=1.29.0,<2",
]

# 번들 런타임에서 반드시 import 되어야 하는 모듈. 하나라도 없으면 실행 시점에
# 조용히 기능이 빠지므로, 패키징 단계에서 실패로 처리합니다.
REQUIRED_IMPORTS = [
    ("nicegui", "웹 UI"),
    ("litellm", "LLM 호출"),
    ("mcp", "MCP 클라이언트"),
    ("mcp.server.fastmcp", "샌드박스 MCP 서버 (mcp 2.x 에서는 제거됨 → mcp<2 필요)"),
    ("mcp_server_git", "git MCP 서버"),
    ("jupyter_client", "샌드박스 커널"),
    ("ipykernel", "샌드박스 커널"),
]
SANDBOX_EXCLUDE = shutil.ignore_patterns(
    # `.devvenv` 는 샌드박스 레포의 개발용 가상환경입니다. 경로가 개발 PC 를 가리켜
    # 번들에서는 쓰지도 못하면서, 심사받을 서드파티 코드만 수백 MB 늘립니다.
    ".git", ".github", "__pycache__", "*.pyc", "dist", "build", "workspace", ".venv", ".devvenv"
)

STEPS = 10

# 번들에 담지 않을 것.
#
# 스테이징 폴더(dist/MultiAgentDebateOrchestration_bundle)는 실행 사이에 남아 있고,
# 거기서 run_mado.bat 로 앱을 한 번 띄우면 대화 DB 와 에이전트가 만든 파일이
# 그 안에 생깁니다. 예전에는 압축이 폴더를 통째로 담아서 그것들이 그대로
# 반입 대상이 되었습니다 — 실제로 workspace/handoff.py 같은 테스트 산출물이
# 배포 ZIP 에 들어가 있었습니다.
#
# **최상위에서만** 봅니다. 벤더링한 트리(python_runtime, wheels, mcp_sandbox,
# node_runtime)는 받은 그대로 나가야 합니다 — 그 안의 __pycache__ 나 MANIFEST.txt
# 는 그 배포판의 일부이지 이 앱이 만든 잔재가 아닙니다.
BUNDLE_EXCLUDE_ROOT_PATTERNS = [
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",   # 대화 기록
    "*.log",
    ".env",                                                     # 실제 자격증명
    "MANIFEST.txt",                                             # 소스 패키지 산출물
]

# 작업 공간은 **빈 상태로** 나가야 합니다. 여기 남아 있는 파일은 전부 이 기계에서
# 돌려 본 흔적이지 배포물이 아닙니다. git 저장소 자체는 남깁니다 — git MCP 서버가
# 유효한 저장소가 아니면 기동하지 않습니다.
WORKSPACE_KEEP = {".gitkeep"}


def log(step: int, message: str) -> None:
    print(f"[{step}/{STEPS}] {message}")


# ---------------------------------------------------------------------------
# 1. 소스 및 설정
# ---------------------------------------------------------------------------
def stage_sources() -> None:
    staging_app = STAGING_DIR / "app"
    if staging_app.exists():
        shutil.rmtree(staging_app)
    shutil.copytree(ROOT_DIR / "app", staging_app, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 포크한 MCP 서버 원본. 실행 사본(mcp_node/memory-scoped.mjs)은 앱이 기동할 때
    # 여기서 복사해 놓습니다.
    staging_forks = STAGING_DIR / "mcp_servers"
    if (ROOT_DIR / "mcp_servers").is_dir():
        if staging_forks.exists():
            shutil.rmtree(staging_forks)
        shutil.copytree(ROOT_DIR / "mcp_servers", staging_forks,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 체험 서버의 공식 템플릿. conf.json 의 trial.templates_dir 가 기본으로 가리키는 폴더입니다.
    staging_templates = STAGING_DIR / "trial_templates"
    if (ROOT_DIR / "trial_templates").is_dir():
        if staging_templates.exists():
            shutil.rmtree(staging_templates)
        shutil.copytree(ROOT_DIR / "trial_templates", staging_templates)

    # 기본 스킬 디렉터리입니다. conf.json 의 skills.dir 가 기본값으로 참조하는 폴더입니다.
    staging_skills = STAGING_DIR / "skills"
    if (ROOT_DIR / "skills").is_dir():
        if staging_skills.exists():
            shutil.rmtree(staging_skills)
        shutil.copytree(ROOT_DIR / "skills", staging_skills,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # open_browser.py 는 실행 스크립트가 백그라운드로 띄웁니다 (서버가 응답하면 브라우저 열기).
    # LICENSE.md 는 배포물과 반드시 함께 다녀야 합니다 (LGPL-3.0 제4조 고지 의무).
    for fname in [".env.example", "requirements.txt", "README.md", "LICENSE.md",
                  "open_browser.py"]:
        src = ROOT_DIR / fname
        if src.exists():
            shutil.copy2(src, STAGING_DIR / fname)

    # conf.json 은 gitignore 대상이라 신규 클론에는 없습니다. 템플릿으로 대체합니다.
    conf_src = ROOT_DIR / "conf.json"
    if not conf_src.exists():
        conf_src = ROOT_DIR / "conf.example.json"
        print("      conf.json 이 없어 conf.example.json 을 번들 설정으로 사용합니다.")
    if not conf_src.exists():
        raise FileNotFoundError("conf.json / conf.example.json 을 찾을 수 없습니다.")
    shutil.copy2(conf_src, STAGING_DIR / "conf.json")

    stage_docs()

    # 에이전트 공용 작업 공간 (filesystem / git / sandbox MCP 가 공유)
    workspace = STAGING_DIR / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".gitkeep").touch()
    try:
        git_bin = shutil.which("git")
        if git_bin:
            subprocess.run([git_bin, "init", "-q", str(workspace)], check=True, capture_output=True)
            subprocess.run([git_bin, "-C", str(workspace), "config", "user.name", "multiagent"], check=False, capture_output=True)
            subprocess.run([git_bin, "-C", str(workspace), "config", "user.email", "multiagent@localhost"], check=False, capture_output=True)
            subprocess.run([git_bin, "-C", str(workspace), "add", ".gitkeep"], check=True, capture_output=True)
            subprocess.run(
                [git_bin, "-C", str(workspace), "-c", "user.name=multiagent",
                 "-c", "user.email=multiagent@localhost", "commit", "-q", "-m", "workspace 초기화"],
                check=False, capture_output=True,
            )
            print("      workspace 를 git 저장소로 초기화 완료.")
    except Exception as exc:
        print(f"      [경고] workspace git 초기화 실패: {exc}")


def stage_docs() -> None:
    """사용 설명서를 담고, HTML 로 미리 렌더링해 둡니다.

    폐쇄망에서는 저장소도 위키도 열 수 없습니다. 설명서가 설치본과 같이 다녀야
    하고, 받는 쪽이 아무것도 실행하지 않아도 읽을 수 있어야 합니다. 그래서
    마크다운 원본과 함께 `docs/user_manual_html/index.html` 을 미리 만들어
    둡니다 (원본이 있으므로 대상에서 다시 렌더링할 수도 있습니다).

    렌더링에 실패해도 번들 생성을 막지 않습니다. 설명서는 앱이 도는 데 필요한
    것이 아니고, 원본 마크다운은 이미 담겼습니다.
    """
    src_docs = ROOT_DIR / "docs"
    if not (src_docs / "user_manual").is_dir():
        print("      docs/user_manual 이 없어 설명서를 건너뜁니다.")
        return

    target = STAGING_DIR / "docs"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    shutil.copytree(src_docs / "user_manual", target / "user_manual",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    renderer = src_docs / "render_user_manual.py"
    if renderer.is_file():
        shutil.copy2(renderer, target / renderer.name)

    if not renderer.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(renderer),
             "--src", str(target / "user_manual"),
             "--out", str(target / "user_manual_html"), "--clean"],
            check=True, capture_output=True,
        )
        print("      사용 설명서를 HTML 로 렌더링했습니다: docs/user_manual_html/index.html")
    except (subprocess.CalledProcessError, OSError) as exc:
        # 설명서는 앱 구동에 필요하지 않습니다. 원본은 이미 담겼습니다.
        print(f"      [경고] 설명서 HTML 렌더링 실패 (원본은 담았습니다): {exc}")


# ---------------------------------------------------------------------------
# 2. Python 코드 실행 샌드박스 MCP 서버
# ---------------------------------------------------------------------------
def stage_sandbox(sandbox_src: str | None) -> Path | None:
    """AirgappedPySandbox 를 mcp_sandbox/ 로 벤더링하고 소스 경로를 반환합니다."""
    target = STAGING_DIR / "mcp_sandbox"

    candidates = []
    if sandbox_src:
        candidates.append(Path(sandbox_src).expanduser())
    if os.environ.get("MCP_SANDBOX_SRC"):
        candidates.append(Path(os.environ["MCP_SANDBOX_SRC"]).expanduser())
    candidates += [
        ROOT_DIR.parent / "AirgappedPySandbox",
        ROOT_DIR.parent / "airgappedpysandbox",
        ROOT_DIR / "dist" / "MultiAgentDebateOrchestration_bundle" / "mcp_sandbox",
    ]

    source = next((c for c in candidates if (c / "server.py").exists()), None)

    if source is None:
        # 로컬에 없으면 패키징 시점의 인터넷으로 얕은 클론을 시도합니다.
        checkout = DIST_DIR / "_airgapped_sandbox_src"
        if not (checkout / "server.py").exists():
            print(f"      로컬에서 찾지 못해 클론을 시도합니다: {SANDBOX_REPO_URL}")
            if checkout.exists():
                shutil.rmtree(checkout)
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", SANDBOX_REPO_URL, str(checkout)],
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                print(f"      [경고] 샌드박스 서버를 준비하지 못했습니다: {exc}")
                print("             --sandbox-src <경로> 로 로컬 체크아웃을 지정하거나")
                print("             --skip-sandbox 로 건너뛸 수 있습니다.")
                return None
        source = checkout

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=SANDBOX_EXCLUDE)
    (target / "workspace").mkdir(exist_ok=True)
    print(f"      샌드박스 서버 원본: {source}")
    return source


# ---------------------------------------------------------------------------
# 3. pip wheel 수집
# ---------------------------------------------------------------------------
def _requirements_fingerprint(sandbox_source: Path | None) -> str:
    """요구사항 목록의 지문. 바뀌면 wheel 을 다시 받습니다."""
    parts = [(ROOT_DIR / "requirements.txt").read_text(encoding="utf-8")]
    parts.extend(PYTHON_MCP_PACKAGES)
    parts.extend(PIP_CONSTRAINTS)
    if sandbox_source is not None:
        req = sandbox_source / "requirements-server.txt"
        if req.exists():
            parts.append(req.read_text(encoding="utf-8"))
        req_kernel = sandbox_source / "requirements-kernel.txt"
        if req_kernel.exists():
            parts.append(req_kernel.read_text(encoding="utf-8"))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def write_constraints() -> Path:
    """pip download / install 양쪽에 쓸 제약 파일을 만듭니다."""
    path = STAGING_DIR / "constraints.txt"
    path.write_text(
        "# package_offline.py 가 생성합니다. 번들 전체의 버전 정합성을 강제합니다.\n"
        + "\n".join(PIP_CONSTRAINTS)
        + "\n",
        encoding="utf-8",
    )
    return path


def staged_python() -> Path | None:
    """번들에 들어간 포터블 인터프리터 경로."""
    runtime = STAGING_DIR / "python_runtime"
    for rel in ("python.exe", "bin/python3", "bin/python"):
        candidate = runtime / rel
        if candidate.exists():
            return candidate
    return None



def collect_wheels(sandbox_source: Path | None) -> None:
    staging_wheels = STAGING_DIR / "wheels"
    staging_wheels.mkdir(parents=True, exist_ok=True)

    # 요구사항이 바뀌면 수집분을 자동으로 무효화합니다. 예전에는 마커가 존재하기만
    # 하면 재사용해서, requirements.txt 를 고쳐도 낡은 wheel 이 그대로 실렸습니다.
    fingerprint = _requirements_fingerprint(sandbox_source)
    marker = staging_wheels / ".collected"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == fingerprint:
        count = len(list(staging_wheels.glob("*.whl")))
        print(f"      기존 수집분({count}개) 재사용. 다시 받으려면 wheels/.collected 를 지우세요.")
        return
    if marker.exists():
        print("      요구사항이 변경되어 wheel 을 다시 수집합니다.")

    constraints = write_constraints()

    def pip_download(args: list[str], label: str) -> None:
        print(f"      - {label}")
        subprocess.run(
            [sys.executable, "-m", "pip", "download",
             "-c", str(constraints), *args, "-d", str(staging_wheels)],
            check=True,
        )

    pip_download(["-r", str(ROOT_DIR / "requirements.txt")], "애플리케이션 의존성")
    pip_download(PYTHON_MCP_PACKAGES, "공식 Python MCP 서버 (fetch / git)")

    if sandbox_source is not None:
        req = sandbox_source / "requirements-server.txt"
        if req.exists():
            pip_download(["-r", str(req)], "샌드박스 MCP 서버 의존성 (jupyter_client / ipykernel)")
        req_kernel = sandbox_source / "requirements-kernel.txt"
        if req_kernel.exists():
            pip_download(["-r", str(req_kernel)], "샌드박스 커널 필수 의존성 (requirements-kernel.txt)")

    marker.write_text(fingerprint, encoding="utf-8")
    print(f"      총 {len(list(staging_wheels.glob('*.whl')))}개 wheel 수집 완료.")


# ---------------------------------------------------------------------------
# 4. 포터블 Python 런타임
# ---------------------------------------------------------------------------
def stage_python_runtime() -> None:
    staging_runtime = STAGING_DIR / "python_runtime"
    staging_runtime.mkdir(parents=True, exist_ok=True)

    if (staging_runtime / "python.exe").exists():
        print("      기존 번들 확인 완료.")
        return

    py_home = Path(sys.executable).parent
    for item in ["python.exe", "pythonw.exe", "DLLs", "Lib", "Scripts"]:
        src = py_home / item
        dst = staging_runtime / item
        if not src.exists():
            continue
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)

    for dll in py_home.glob("*.dll"):
        shutil.copy2(dll, staging_runtime / dll.name)


# ---------------------------------------------------------------------------
# 5. Node 런타임 (node.exe 단일 파일)
# ---------------------------------------------------------------------------
def stage_node_runtime(node_version: str) -> bool:
    staging_node = STAGING_DIR / "node_runtime"
    staging_node.mkdir(parents=True, exist_ok=True)
    node_exe = staging_node / "node.exe"

    if node_exe.exists():
        print("      기존 번들 확인 완료.")
        return True

    url = NODE_DIST_URL.format(ver=node_version)
    archive = DIST_DIR / f"node-v{node_version}-win-x64.zip"

    if not archive.exists():
        print(f"      내려받는 중: {url}")
        try:
            urllib.request.urlretrieve(url, archive)
        except Exception as exc:  # noqa: BLE001 - 네트워크/프록시 오류 전부 안내로 전환
            print(f"      [경고] Node 런타임 다운로드 실패: {exc}")
            local = shutil.which("node")
            if local and sys.platform == "win32":
                shutil.copy2(local, node_exe)
                print(f"      대신 로컬 node 를 복사했습니다: {local}")
                return True
            print(f"             {url} 를 수동으로 받아 {archive} 에 두고 다시 실행하세요.")
            return False

    # 압축 전체를 풀지 않고 node.exe 만 꺼냅니다 (npm/npx 는 런타임에 불필요).
    with zipfile.ZipFile(archive) as zf:
        member = next((n for n in zf.namelist() if n.endswith("/node.exe")), None)
        if member is None:
            print("      [경고] 아카이브에서 node.exe 를 찾지 못했습니다.")
            return False
        with zf.open(member) as src, open(node_exe, "wb") as dst:
            shutil.copyfileobj(src, dst)

    size_mb = round(node_exe.stat().st_size / (1024 * 1024), 1)
    print(f"      node.exe 추출 완료 (v{node_version}, {size_mb} MB)")
    return True


# ---------------------------------------------------------------------------
# 런타임에 의존성 설치 & 검증
# ---------------------------------------------------------------------------
def install_into_runtime(sandbox_source: Path | None) -> bool:
    """수집한 wheel 을 **번들 런타임 안에** 설치합니다.

    포터블 런타임은 패키징 머신의 파이썬을 복사한 것이라, 그 머신에 설치되지 않은
    패키지는 번들에도 없습니다. 예전에는 wheel 을 wheels/ 에 넣어두기만 해서,
    패키징 머신 상태에 따라 mcp-server-git 이 빠지거나 mcp 버전이 어긋난 번들이
    그대로 배포되었습니다.
    """
    python = staged_python()
    if python is None:
        print("      [경고] 번들 런타임을 찾을 수 없어 건너뜁니다.")
        print("             Windows 에서 실행하면 python_runtime/python.exe 가 만들어집니다.")
        return False

    wheels = STAGING_DIR / "wheels"
    if not any(wheels.glob("*.whl")):
        print("      [경고] wheels/ 가 비어 있어 설치할 것이 없습니다.")
        return False

    constraints = write_constraints()
    targets = [["-r", str(STAGING_DIR / "requirements.txt")], list(PYTHON_MCP_PACKAGES)]
    if sandbox_source is not None:
        req = sandbox_source / "requirements-server.txt"
        if req.exists():
            targets.append(["-r", str(req)])
        req_kernel = sandbox_source / "requirements-kernel.txt"
        if req_kernel.exists():
            targets.append(["-r", str(req_kernel)])

    for spec in targets:
        label = spec[1] if spec[0] == "-r" else ", ".join(spec)
        print(f"      - {Path(label).name if spec[0] == '-r' else label}")
        try:
            subprocess.run(
                [str(python), "-m", "pip", "install", "--no-index",
                 f"--find-links={wheels}", "-c", str(constraints),
                 "--upgrade", "--no-warn-script-location", *spec],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"      [경고] 설치 실패: {exc}")
            return False
    return True


def verify_runtime() -> list[str]:
    """번들 런타임에서 필수 모듈이 실제로 import 되는지 확인합니다.

    없는 모듈 이름 목록을 돌려줍니다. 실행 시점에 조용히 기능이 빠지는 대신
    패키징 단계에서 드러나게 하는 것이 목적입니다.
    """
    python = staged_python()
    if python is None:
        print("      [경고] 번들 런타임이 없어 검증을 건너뜁니다.")
        return []

    missing: list[str] = []
    for module, purpose in REQUIRED_IMPORTS:
        result = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True, text=True, cwd=str(STAGING_DIR),
        )
        if result.returncode == 0:
            print(f"      [OK  ] {module}")
        else:
            missing.append(module)
            print(f"      [실패] {module}  ({purpose})")
            tail = (result.stderr or "").strip().splitlines()
            if tail:
                print(f"             {tail[-1][:120]}")
    return missing


# ---------------------------------------------------------------------------
# 6. 공식 Node MCP 서버
# ---------------------------------------------------------------------------
def stage_node_mcp_servers() -> bool:
    staging_mcp = STAGING_DIR / "mcp_node"
    staging_mcp.mkdir(parents=True, exist_ok=True)

    if (staging_mcp / "node_modules" / "@modelcontextprotocol").exists():
        print("      기존 설치 확인 완료.")
        return True

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        print("      [경고] npm 을 찾을 수 없어 Node MCP 서버를 건너뜁니다.")
        print("             filesystem / memory MCP 없이 번들이 만들어집니다.")
        return False

    print(f"      설치 중: {', '.join(p.rsplit('/', 1)[-1] for p in NODE_MCP_PACKAGES)}")
    try:
        subprocess.run(
            [npm, "install", "--omit=dev", "--no-audit", "--no-fund",
             "--prefix", str(staging_mcp), *NODE_MCP_PACKAGES],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"      [경고] npm install 실패: {exc}")
        return False

    # .bin 셰이딩은 OS 별 심볼릭 링크/셰이더라 재배치에 방해만 됩니다.
    # 서버는 dist/index.js 를 node 로 직접 실행하므로 필요 없습니다.
    bin_dir = staging_mcp / "node_modules" / ".bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir, ignore_errors=True)

    count = sum(1 for _ in (staging_mcp / "node_modules").iterdir())
    print(f"      node_modules 준비 완료 ({count}개 패키지).")
    return True


# ---------------------------------------------------------------------------
# 7. 실행 스크립트 & 문서
# ---------------------------------------------------------------------------
def write_launchers(has_node: bool, has_sandbox: bool, target_dir: Optional[Path] = None) -> None:
    """번들 안에 실행 스크립트를 씁니다.

    `target_dir` 를 주면 그 폴더에 씁니다. 이미 배포한 설치본의 실행 스크립트만
    새로 만들 때 씁니다 (`--launchers-only`).
    """
    out_dir = Path(target_dir) if target_dir is not None else STAGING_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    def write(name: str, content: str) -> None:
        with open(out_dir / name, "w", encoding="utf-8-sig", newline="") as f:
            f.write(content)

    # --- run_mado.bat ---
    bat = (
        "@echo off\r\n"
        "chcp 65001 > nul\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n\r\n"
        "echo ==========================================================\r\n"
        "echo   MADO: Multi-Agent Debate ^& Orchestration Platform (오프라인 / 폐쇄망 모드)\r\n"
        "echo ==========================================================\r\n\r\n"
        "set \"PYTHONIOENCODING=utf-8\"\r\n"
        "set \"PYTHONUTF8=1\"\r\n\r\n"
        "rem --- 실행 런타임 설정 (conf.json 의 ${PYTHON_BIN} / ${NODE_BIN} 치환에도 사용됩니다) ---\r\n"
        "rem     폐쇄망 배포 번들에는 포터블 런타임(python_runtime, node_runtime)이 동봉되어 있습니다.\r\n"
        "rem     개발 환경 등 내장 런타임이 없는 경우에는 PATH 에 등록된 런타임으로 대체하여 실행합니다.\r\n"
        "rem     존재하지 않는 폴더를 PYTHONHOME 으로 지정하면 시스템 Python 조차 기동되지 않으므로,\r\n"
        "rem     내장 런타임이 존재할 때에만 PYTHONHOME 을 설정합니다.\r\n"
        "set \"PYTHON_BIN=\"\r\n"
        "set \"RUNTIME_LABEL=내장 포터블 Python 런타임\"\r\n"
        "if exist \"%~dp0python_runtime\\python.exe\" goto :bundled_python\r\n"
        "for /f \"delims=\" %%p in ('where python 2^>nul') do if not defined PYTHON_BIN set \"PYTHON_BIN=%%p\"\r\n"
        "if not defined PYTHON_BIN (\r\n"
        "    echo [X] python_runtime\\python.exe 및 PATH 의 python 을 찾을 수 없습니다.\r\n"
        "    pause\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "echo [!] python_runtime 이 존재하지 않아 PATH 에 등록된 Python 을 사용합니다: %PYTHON_BIN%\r\n"
        "echo     requirements.txt 에 명시된 패키지가 미리 설치되어 있어야 합니다.\r\n"
        "set \"PYTHONPATH=%~dp0;%PYTHONPATH%\"\r\n"
        "set \"RUNTIME_LABEL=PATH 의 Python\"\r\n"
        "goto :python_ready\r\n"
        ":bundled_python\r\n"
        "set \"PYTHONHOME=%~dp0python_runtime\"\r\n"
        "set \"PYTHONPATH=%~dp0;%~dp0python_runtime\\Lib;%~dp0python_runtime\\Lib\\site-packages\"\r\n"
        "set \"PATH=%~dp0python_runtime;%~dp0python_runtime\\Scripts;%PATH%\"\r\n"
        "set \"PYTHON_BIN=%~dp0python_runtime\\python.exe\"\r\n"
        ":python_ready\r\n\r\n"
        "set \"NODE_BIN=\"\r\n"
        "if exist \"%~dp0node_runtime\\node.exe\" goto :bundled_node\r\n"
        "for /f \"delims=\" %%n in ('where node 2^>nul') do if not defined NODE_BIN set \"NODE_BIN=%%n\"\r\n"
        "if defined NODE_BIN (\r\n"
        "    echo [!] node_runtime 이 존재하지 않아 PATH 에 등록된 Node 를 사용합니다: %NODE_BIN%\r\n"
        ") else (\r\n"
        "    echo [!] node_runtime\\node.exe 및 PATH 의 node 를 찾을 수 없습니다. filesystem 및 memory MCP 서버가 비활성화됩니다.\r\n"
        ")\r\n"
        "goto :node_ready\r\n"
        ":bundled_node\r\n"
        "set \"NODE_BIN=%~dp0node_runtime\\node.exe\"\r\n"
        "set \"PATH=%~dp0node_runtime;%PATH%\"\r\n"
        ":node_ready\r\n\r\n"
        "rem --- MCP 서버 위치 (conf.json 의 ${VAR:-기본값} 치환에 사용됩니다) ---\r\n"
        "set \"MCP_NODE_HOME=%~dp0mcp_node\"\r\n"
        "set \"MCP_SANDBOX_HOME=%~dp0mcp_sandbox\"\r\n"
        "set \"WORKSPACE_DIR=%~dp0workspace\"\r\n"
        "if not defined SANDBOX_KERNEL_PYTHON set \"SANDBOX_KERNEL_PYTHON=%PYTHON_BIN%\"\r\n\r\n"
        "rem --- 접속 주소는 conf.json 의 app 설정을 그대로 참조합니다 (하드코딩 금지) ---\r\n"
        "rem     앞의 call 명령을 제거하지 마십시오. 명령이 큰따옴표로 시작하면 cmd.exe 가\r\n"
        "rem     양 끝 따옴표를 임의로 제거하여 명령 구문이 손상될 수 있습니다.\r\n"
        "set \"APP_URL=\"\r\n"
        "for /f \"usebackq delims=\" %%i in (`call \"%PYTHON_BIN%\" -c \"from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')\"`) do set \"APP_URL=%%i\"\r\n"
        "if not defined APP_URL set \"APP_URL=conf.json 의 app 설정 참조\"\r\n\r\n"
        "rem --- 서버가 응답하면 브라우저를 엽니다. 서버는 콘솔을 점유하므로\r\n"
        "rem     대기 및 실행 작업은 별도 프로세스가 수행합니다 (MADO_NO_BROWSER=1 인 경우 생략됩니다).\r\n"
        "rem     동일한 인수를 그대로 전달해야 --port 로 포트를 변경하더라도 올바른 주소로 접속합니다.\r\n"
        "if exist \"%~dp0open_browser.py\" start \"\" /b \"%PYTHON_BIN%\" open_browser.py %*\r\n\r\n"
        "echo [*] %RUNTIME_LABEL% 환경에서 서버를 시작합니다 (%APP_URL%)...\r\n"
        "\"%PYTHON_BIN%\" -m app.main %*\r\n\r\n"
        "pause\r\n"
    )
    write("run_mado.bat", bat)

    # --- run_mado.ps1 ---
    ps1 = (
        "# MADO: Multi-Agent Debate & Orchestration Platform - Offline Launch Script\r\n"
        "$ErrorActionPreference = \"Stop\"\r\n\r\n"
        "# 콘솔 입출력 인코딩 UTF-8 설정 (한글 깨짐 방지)\r\n"
        "try {\r\n"
        "    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "    [Console]::InputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "    $OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "} catch {}\r\n\r\n"
        "$RootDir = $PSScriptRoot\r\n"
        "if (-not $RootDir) { $RootDir = (Get-Location).Path }\r\n\r\n"
        "Write-Host \"==========================================================\" -ForegroundColor Cyan\r\n"
        "Write-Host \"  MADO: Multi-Agent Debate & Orchestration Platform (오프라인 / 폐쇄망 모드)\" -ForegroundColor Cyan\r\n"
        "Write-Host \"==========================================================\" -ForegroundColor Cyan\r\n\r\n"
        "$env:PYTHONIOENCODING = \"utf-8\"\r\n"
        "$env:PYTHONUTF8 = \"1\"\r\n\r\n"
        "# --- 실행 런타임 설정 (conf.json 의 ${PYTHON_BIN} / ${NODE_BIN} 치환에도 사용됩니다) ---\r\n"
        "# 폐쇄망 배포 번들에는 포터블 런타임(python_runtime, node_runtime)이 동봉되어 있습니다.\r\n"
        "# 개발 환경 등 내장 런타임이 없는 경우에는 PATH 에 등록된 런타임으로 대체하여 실행합니다.\r\n"
        "# 존재하지 않는 폴더를 PYTHONHOME 으로 지정하면 시스템 Python 조차 기동되지 않으므로,\r\n"
        "# 내장 런타임이 존재할 때에만 PYTHONHOME 을 설정합니다.\r\n"
        "$PythonRuntime = Join-Path $RootDir \"python_runtime\"\r\n"
        "$NodeRuntime = Join-Path $RootDir \"node_runtime\"\r\n\r\n"
        "if (Test-Path (Join-Path $PythonRuntime \"python.exe\")) {\r\n"
        "    $env:PYTHONHOME = $PythonRuntime\r\n"
        "    $env:PYTHONPATH = \"$RootDir;$(Join-Path $PythonRuntime 'Lib');$(Join-Path $PythonRuntime 'Lib\\site-packages')\"\r\n"
        "    $env:PATH = \"$PythonRuntime;$(Join-Path $PythonRuntime 'Scripts');\" + $env:PATH\r\n"
        "    $env:PYTHON_BIN = Join-Path $PythonRuntime \"python.exe\"\r\n"
        "    $RuntimeLabel = \"내장 포터블 Python 런타임\"\r\n"
        "} else {\r\n"
        "    $SystemPython = Get-Command python -ErrorAction SilentlyContinue\r\n"
        "    if (-not $SystemPython) {\r\n"
        "        Write-Host \"[X] python_runtime\\python.exe 및 PATH 의 python 을 찾을 수 없습니다.\" -ForegroundColor Red\r\n"
        "        exit 1\r\n"
        "    }\r\n"
        "    $env:PYTHON_BIN = $SystemPython.Source\r\n"
        "    $env:PYTHONPATH = if ($env:PYTHONPATH) { \"$RootDir;$env:PYTHONPATH\" } else { $RootDir }\r\n"
        "    Write-Warning \"python_runtime 이 존재하지 않아 PATH 에 등록된 Python($($env:PYTHON_BIN))을 사용합니다. requirements.txt 에 명시된 패키지가 미리 설치되어 있어야 합니다.\"\r\n"
        "    $RuntimeLabel = \"PATH 의 Python\"\r\n"
        "}\r\n\r\n"
        "if (Test-Path (Join-Path $NodeRuntime \"node.exe\")) {\r\n"
        "    $env:PATH = \"$NodeRuntime;\" + $env:PATH\r\n"
        "    $env:NODE_BIN = Join-Path $NodeRuntime \"node.exe\"\r\n"
        "} else {\r\n"
        "    $SystemNode = Get-Command node -ErrorAction SilentlyContinue\r\n"
        "    if ($SystemNode) {\r\n"
        "        $env:NODE_BIN = $SystemNode.Source\r\n"
        "        Write-Warning \"node_runtime 이 존재하지 않아 PATH 에 등록된 Node($($env:NODE_BIN))를 사용합니다.\"\r\n"
        "    } else {\r\n"
        "        Write-Warning \"node_runtime\\node.exe 및 PATH 의 node 를 찾을 수 없습니다. filesystem 및 memory MCP 서버가 비활성화됩니다.\"\r\n"
        "    }\r\n"
        "}\r\n\r\n"
        "# --- MCP 서버 위치 (conf.json 의 ${VAR:-기본값} 치환에 사용됩니다) ---\r\n"
        "$env:MCP_NODE_HOME = Join-Path $RootDir \"mcp_node\"\r\n"
        "$env:MCP_SANDBOX_HOME = Join-Path $RootDir \"mcp_sandbox\"\r\n"
        "$env:WORKSPACE_DIR = Join-Path $RootDir \"workspace\"\r\n"
        "if (-not $env:SANDBOX_KERNEL_PYTHON) { $env:SANDBOX_KERNEL_PYTHON = $env:PYTHON_BIN }\r\n\r\n"
        "# 접속 주소는 conf.json 의 app 설정을 그대로 참조합니다 (하드코딩 금지)\r\n"
        "$AppUrl = try {\r\n"
        "    & $env:PYTHON_BIN -c \"from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')\"\r\n"
        "} catch { \"conf.json 의 app 설정 참조\" }\r\n\r\n"
        "# 서버가 응답하면 브라우저를 엽니다. 서버는 콘솔을 점유하므로 대기 및\r\n"
        "# 실행 작업은 별도 프로세스가 수행합니다 (MADO_NO_BROWSER=1 인 경우 생략됩니다). 동일한\r\n"
        "# 인수를 그대로 전달해야 --port 로 포트를 변경하더라도 올바른 주소로 접속합니다.\r\n"
        "if (Test-Path (Join-Path $RootDir \"open_browser.py\")) {\r\n"
        "    Start-Process -FilePath $env:PYTHON_BIN -ArgumentList (@(\"open_browser.py\") + $args) -WorkingDirectory $RootDir -WindowStyle Hidden | Out-Null\r\n"
        "}\r\n\r\n"
        "Write-Host \"[*] ${RuntimeLabel} 환경에서 서버를 시작합니다 ($AppUrl)...\" -ForegroundColor Green\r\n"
        "& $env:PYTHON_BIN -m app.main $args\r\n"
    )
    write("run_mado.ps1", ps1)

    # --- install_wheels_offline.bat ---
    install_bat = (
        "@echo off\r\n"
        "chcp 65001 > nul\r\n"
        "cd /d \"%~dp0\"\r\n\r\n"
        "rem 번들 런타임이 있으면 그쪽에, 없으면 PATH 의 pip 로 설치합니다.\r\n"
        "set \"TARGET_PY=%~dp0python_runtime\\python.exe\"\r\n"
        "if not exist \"%TARGET_PY%\" set \"TARGET_PY=python\"\r\n"
        "echo [*] 설치 대상 인터프리터: %TARGET_PY%\r\n\r\n"
        "rem constraints.txt 가 mcp 를 1.x 로 묶습니다. 빠뜨리면 2.x 가 들어와\r\n"
        "rem mcp.server.fastmcp 가 사라지고 샌드박스 서버가 기동하지 못합니다.\r\n"
        "set \"PIP_C=\"\r\n"
        "if exist \"%~dp0constraints.txt\" set \"PIP_C=-c .\\constraints.txt\"\r\n\r\n"
        "echo [*] wheels 디렉토리의 오프라인 패키지를 설치합니다...\r\n"
        "\"%TARGET_PY%\" -m pip install --no-index --find-links=.\\wheels %PIP_C% "
        "--upgrade --no-warn-script-location -r requirements.txt\r\n"
        "echo [*] MCP 서버 패키지(fetch / git / 샌드박스 커널)를 설치합니다...\r\n"
        "\"%TARGET_PY%\" -m pip install --no-index --find-links=.\\wheels %PIP_C% "
        "--upgrade --no-warn-script-location "
        "mcp-server-fetch mcp-server-git jupyter_client ipykernel\r\n"
        "if exist \"%~dp0mcp_sandbox\\requirements-kernel.txt\" (\r\n"
        "    echo [*] 샌드박스 커널 필수 라이브러리를 설치합니다...\r\n"
        "    \"%TARGET_PY%\" -m pip install --no-index --find-links=.\\wheels %PIP_C% "
        "--upgrade --no-warn-script-location -r \"%~dp0mcp_sandbox\\requirements-kernel.txt\"\r\n"
        ")\r\n\r\n"
        "echo [*] 필수 모듈 검증...\r\n"
        "\"%TARGET_PY%\" -c \"import nicegui, litellm, mcp.server.fastmcp, mcp_server_git, "
        "jupyter_client, ipykernel; print('  전부 정상')\"\r\n"
        "if errorlevel 1 echo [!] 위에 표시된 모듈이 빠져 있습니다. 해당 MCP 서버가 동작하지 않습니다.\r\n"
        "echo.\r\n"
        "echo [!] 이 방식으로 실행할 때는 conf.json 이 참조하는 아래 환경변수를 직접 지정하거나\r\n"
        "echo     PATH 에 node 를 등록해야 filesystem / memory MCP 가 동작합니다.\r\n"
        "echo       NODE_BIN / PYTHON_BIN / MCP_NODE_HOME / MCP_SANDBOX_HOME / WORKSPACE_DIR\r\n"
        "pause\r\n"
    )
    write("install_wheels_offline.bat", install_bat)

    # --- README_OFFLINE.md ---
    node_line = (
        "- `node_runtime/` + `mcp_node/` : filesystem · memory · sequential-thinking MCP 서버 (Node 순수 JS)\r\n"
        if has_node
        else "- [!] Node MCP 서버가 번들에 포함되지 않았습니다 (패키징 시 npm 또는 node 다운로드 실패).\r\n"
    )
    sandbox_line = (
        "- `mcp_sandbox/` : AirgappedPySandbox — 상태 유지형 Python 코드 실행 MCP 서버\r\n"
        if has_sandbox
        else "- [!] 코드 실행 샌드박스가 번들에 포함되지 않았습니다.\r\n"
    )
    readme_offline = (
        "# 📦 MADO: Multi-Agent Debate & Orchestration Platform - 폐쇄망(오프라인) 배포 가이드\r\n\r\n"
        "이 패키지는 외부 인터넷 연결이 불가능한 폐쇄망 환경에서도 즉시 구동될 수 있도록 "
        "파이썬 런타임, Node 런타임, MCP 서버, 의존성 라이브러리, 오프라인 Wheel 파일 및 "
        "소스코드를 모두 포함하고 있습니다.\r\n\r\n"
        "## 🚀 빠른 실행 방법\r\n\r\n"
        "### 방법 1. 내장 포터블 런타임으로 즉시 실행 (가장 추천)\r\n"
        "추가 설치나 환경 설정 없이 압축을 해제한 폴더에서 바로 실행할 수 있습니다.\r\n"
        "- Windows 탐색기에서 **`run_mado.bat`** 더블 클릭  \r\n"
        "  또는 PowerShell에서:\r\n"
        "  ```powershell\r\n"
        "  .\\run_mado.ps1\r\n"
        "  ```\r\n"
        "- 서버가 뜨면 **기본 브라우저가 자동으로 열립니다.** 열리지 않으면 콘솔에 표시되는 "
        "주소로 접속하세요. 주소는 `conf.json` 의 `app.host` / `app.port` "
        "(또는 `.env` 의 `APP_HOST` / `APP_PORT`) 를 그대로 따릅니다.\r\n"
        "- 브라우저를 띄우고 싶지 않으면 `MADO_NO_BROWSER=1` 을 설정한 뒤 실행하세요. "
        "서버를 기다리는 시간은 기본 90초이고 `MADO_BROWSER_TIMEOUT` 으로 바꿉니다.\r\n\r\n"
        "실행 스크립트가 MCP 서버 경로 환경변수(`NODE_BIN`, `PYTHON_BIN`, `MCP_NODE_HOME`, "
        "`MCP_SANDBOX_HOME`, `WORKSPACE_DIR`)를 자동으로 채워주므로 별도 설정이 필요 없습니다.\r\n\r\n"
        "---\r\n\r\n"
        "### 방법 2. 폐쇄망 내 기존 Python 환경에 Wheel 설치하여 실행\r\n"
        "이미 폐쇄망 PC에 별도 Python이 설치되어 있는 경우:\r\n"
        "1. `install_wheels_offline.bat` 실행 (인터넷 없이 `.\\wheels\\` 폴더에서 오프라인 설치)\r\n"
        "2. 위 환경변수를 직접 지정 (특히 `NODE_BIN`, `MCP_NODE_HOME`)\r\n"
        "3. `python -m app.main` 실행\r\n\r\n"
        "---\r\n\r\n"
        "## 🧰 포함된 MCP 도구\r\n\r\n"
        + node_line
        + sandbox_line
        + "- `wheels/` 에 포함된 `mcp-server-git` : 산출물 버전 관리 (기본 활성)\r\n"
        "- `wheels/` 에 포함된 `mcp-server-fetch` : URL 조회 (기본 **비활성** — 폐쇄망에서는 켜지 마세요)\r\n\r\n"
        "### 런타임에 모듈이 빠졌을 때\r\n"
        "`No module named mcp_server_git` / `No module named 'mcp.server.fastmcp'` 처럼 모듈이 없다는 "
        "로그가 보이면, 번들 런타임에 의존성이 설치되지 않은 것입니다. 아래를 실행하세요.\r\n"
        "```\r\n"
        "install_wheels_offline.bat\r\n"
        "```\r\n"
        "이 스크립트는 번들 런타임(`python_runtime\\python.exe`)을 대상으로 `wheels\\` 의 오프라인 "
        "패키지를 설치하고, 끝에 필수 모듈 import 를 검증합니다.\r\n\r\n"
        "> `mcp` 는 반드시 1.x 여야 합니다. 2.x 는 `mcp.server.fastmcp` 를 제거해(MCPServer 로 개명) "
        "샌드박스 서버가 기동하지 못합니다.\r\n\r\n"
        "MCP 서버는 `conf.json` 의 `mcp_servers` 에서 `\"enabled\": false` 로 개별 비활성화할 수 있습니다.\r\n"
        "모든 서버는 `node`/`python` 진입점을 **직접** 실행합니다. `npx` 는 패키지가 로컬에 없으면 "
        "npm 레지스트리에 접속하므로 폐쇄망에서 사용하지 않습니다.\r\n\r\n"
        "### 코드 실행 샌드박스 커널\r\n"
        "`mcp_sandbox` 는 기본적으로 번들 내장 파이썬으로 커널을 띄웁니다. pandas·numpy·matplotlib 등을 "
        "쓰는 코드까지 검증하려면 해당 라이브러리가 포함된 포터블 파이썬 경로를 "
        "`SANDBOX_KERNEL_PYTHON` 환경변수로 지정하세요.\r\n\r\n"
        "---\r\n\r\n"
        "## ⚙️ 설정 커스텀\r\n"
        "- `conf.json`: 에이전트 목록, 프롬프트, 라운드 수, MCP 서버 설정.\r\n"
        "\r\n"
        "## 📖 사용 설명서\r\n\r\n"
        "`docs/user_manual_html/index.html` 을 브라우저로 열면 됩니다 (인터넷 불필요). "
        "기술 스택, 핵심 기술, 워크플로우, 레퍼런스가 정리되어 있습니다.\r\n\r\n"
        "마크다운 원본은 `docs/user_manual/` 에 있고, 고친 뒤 아래로 다시 렌더링할 수 있습니다.\r\n"
        "```\r\n"
        "python docs\\render_user_manual.py\r\n"
        "```\r\n"
        "- `.env`: LLM API 키(폐쇄망 로컬 LLM / Ollama / vLLM 주소 등) 설정.\r\n"
        "- `workspace/`: 에이전트가 파일을 읽고 쓰는 공용 작업 공간. filesystem · git · sandbox MCP 가 "
        "이 디렉터리를 공유합니다.\r\n"
    )
    write("README_OFFLINE.md", readme_offline)


# ---------------------------------------------------------------------------
def _is_excluded(rel: Path) -> bool:
    """번들에서 빼야 할 경로인지.

    걸러내는 것은 **이 기계에서 앱을 돌려 본 흔적**뿐입니다. 벤더링한 런타임과
    wheel 은 받은 그대로 내보냅니다.
    """
    parts = rel.parts
    if not parts:
        return False

    # 1) 최상위에 떨어진 운영 파일 (multiagent.db, .env, *.log ...)
    if len(parts) == 1 and any(
        fnmatch.fnmatch(parts[0], pat) for pat in BUNDLE_EXCLUDE_ROOT_PATTERNS
    ):
        return True

    # 2) 작업 공간 안은 .git 과 .gitkeep 만 남깁니다.
    if parts[0] == "workspace" and len(parts) > 1:
        if parts[1] == ".git":
            return False
        return not (len(parts) == 2 and parts[1] in WORKSPACE_KEEP)

    return False


def write_bundle_zip(staging: Path, archive: Path) -> list[str]:
    """스테이징 폴더를 ZIP 으로 묶되 운영 잔재는 뺍니다.

    제외한 경로 목록을 돌려줍니다 — 조용히 빼면 무엇이 빠졌는지 아무도 모릅니다.
    """
    skipped: list[str] = []
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging.rglob("*")):
            rel = path.relative_to(staging)
            if _is_excluded(rel):
                if path.is_file() or not any(path.iterdir()):
                    skipped.append(rel.as_posix())
                continue
            if path.is_file():
                zf.write(path, rel.as_posix())
            elif path.is_dir() and not any(path.iterdir()):
                # 빈 폴더도 구조로서 의미가 있으면 남깁니다 (workspace 등).
                zf.writestr(rel.as_posix() + "/", "")
    return skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="폐쇄망 배포용 번들 패키징")
    parser.add_argument("--skip-node", action="store_true", help="Node 런타임 및 Node MCP 서버 제외")
    parser.add_argument("--skip-sandbox", action="store_true", help="코드 실행 샌드박스 MCP 서버 제외")
    parser.add_argument("--node-version", default=DEFAULT_NODE_VERSION, help="번들할 Node 버전")
    parser.add_argument("--sandbox-src", default=None, help="AirgappedPySandbox 로컬 체크아웃 경로")
    parser.add_argument(
        "--launchers-only", metavar="DIR", nargs="?", const=str(STAGING_DIR), default=None,
        help="번들 전체를 다시 만들지 않고 실행 스크립트와 open_browser.py 만 갱신 "
             "(기본 대상: dist/MultiAgentDebateOrchestration_bundle)",
    )
    args = parser.parse_args()

    if args.launchers_only:
        # 이미 배포한 설치본을 통째로 다시 만들지 않고 실행 스크립트만 바꿉니다.
        # 런타임과 wheels 를 다시 내려받을 이유가 없습니다.
        target = Path(args.launchers_only).resolve()
        if not target.is_dir():
            raise SystemExit(f"[!] 대상 폴더가 없습니다: {target}")
        has_node = (target / "node_runtime" / "node.exe").exists()
        has_sandbox = (target / "mcp_sandbox").is_dir()
        write_launchers(has_node=has_node, has_sandbox=has_sandbox, target_dir=target)
        shutil.copy2(ROOT_DIR / "open_browser.py", target / "open_browser.py")
        print(f"[완료] 실행 스크립트를 갱신했습니다: {target}")
        return

    print("=" * 60)
    print("  [폐쇄망 배포용] MADO 패키징 시작")
    print("=" * 60)

    if sys.platform != "win32":
        print("[!] 경고: Windows 가 아닌 환경입니다.")
        print("    포터블 런타임과 pip wheel 이 현재 플랫폼 기준으로 수집되므로")
        print("    Windows 대상 번들을 만들려면 Windows 에서 실행하세요.")
        print("    (Node MCP 서버는 순수 JS 라 어느 OS 에서 설치해도 동일합니다.)")
        print()

    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    log(1, "소스코드 및 설정 파일 복사 중...")
    stage_sources()

    log(2, "코드 실행 샌드박스 MCP 서버(AirgappedPySandbox) 준비 중...")
    sandbox_source = None if args.skip_sandbox else stage_sandbox(args.sandbox_src)
    if args.skip_sandbox:
        print("      --skip-sandbox 지정으로 건너뜁니다.")

    log(3, "오프라인 pip Wheel 패키지 수집 중...")
    collect_wheels(sandbox_source)

    log(4, "포터블 파이썬 런타임 복제 중...")
    stage_python_runtime()

    log(5, "번들 런타임에 의존성 설치 중 (오프라인 wheel)...")
    installed = install_into_runtime(sandbox_source)

    log(6, "번들 런타임 필수 모듈 검증 중...")
    missing = verify_runtime()

    has_node = False
    if args.skip_node:
        log(7, "Node 런타임 — --skip-node 지정으로 건너뜁니다.")
        log(8, "Node MCP 서버 — --skip-node 지정으로 건너뜁니다.")
    else:
        log(7, "Node 런타임(node.exe) 준비 중...")
        node_ok = stage_node_runtime(args.node_version)
        log(8, "공식 Node MCP 서버 설치 중 (filesystem / memory / sequential-thinking)...")
        has_node = stage_node_mcp_servers() and node_ok

    log(9, "폐쇄망 전용 실행 스크립트 및 문서 작성 중 (UTF-8 BOM)...")
    write_launchers(has_node=has_node, has_sandbox=sandbox_source is not None)

    log(10, f"최종 ZIP 아카이브 생성 중 ({ZIP_FILE})...")
    if ZIP_FILE.exists():
        ZIP_FILE.unlink()
    skipped = write_bundle_zip(STAGING_DIR, ZIP_FILE)
    if skipped:
        print(f"      운영 잔재 {len(skipped)}개를 제외했습니다:")
        for rel in skipped[:12]:
            print(f"        - {rel}")
        if len(skipped) > 12:
            print(f"        ... 외 {len(skipped) - 12}개")

    zip_size_mb = round(ZIP_FILE.stat().st_size / (1024 * 1024), 2)

    print("=" * 60)
    print(" [완료] 폐쇄망 패키징이 성공적으로 생성되었습니다!")
    print(f"  - 아카이브 경로: {ZIP_FILE}")
    print(f"  - 파일 크기: {zip_size_mb} MB")
    print("  - 포함 구성: 소스코드, conf.json, 포터블 Python 런타임, 오프라인 Wheels,")
    print(f"               Node 런타임 + Node MCP 서버({'포함' if has_node else '미포함'}),")
    print(f"               코드 실행 샌드박스({'포함' if sandbox_source is not None else '미포함'}),")
    print("               실행 스크립트")
    print("=" * 60)

    if missing:
        print()
        print("!" * 60)
        print(" [경고] 번들 런타임에서 다음 모듈을 import 할 수 없습니다:")
        for module in missing:
            print(f"   - {module}")
        print()
        print(" 이 상태로 배포하면 해당 MCP 서버가 실행 시점에 조용히 빠집니다.")
        if not installed:
            print(" 원인: 런타임에 의존성을 설치하지 못했습니다. 위 로그를 확인하세요.")
        print(" 조치: wheels/.collected 를 지우고 다시 패키징하거나, 아래를 직접 실행하세요.")
        print(r"   python_runtime\python.exe -m pip install --no-index "
              r"--find-links=.\wheels -r requirements.txt")
        print("!" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
