"""개발 PC용 MCP 서버 일괄 설치 스크립트.

`conf.json` 이 기본으로 켜 두는 MCP 서버들이 바로 붙도록 준비합니다.
폐쇄망 배포본은 `package_offline.py` 가 같은 구성을 번들에 넣으므로 이 스크립트가
필요 없습니다.

    python setup_mcp.py [--skip-node] [--skip-sandbox]

수행 항목:
  1. ./workspace 생성 및 git 저장소 초기화
     (git MCP 서버는 유효한 git 저장소가 아니면 기동에 실패합니다)
  2. ./mcp_node 에 공식 Node MCP 서버 설치 (filesystem / memory / sequential-thinking)
     + 포크한 memory 서버(대화별 지식 그래프) 사본 배치
  3. ./mcp_sandbox 에 AirgappedPySandbox 체크아웃 (코드 실행 샌드박스)

파이썬 MCP 서버(mcp-server-git 등)는 requirements.txt 에 포함되어 있으므로
`pip install -r requirements.txt` 로 이미 설치됩니다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

# 공식 Node MCP 서버와 버전 범위. 전체 번들(`package_offline.py`)도 이 목록을 가져다 씁니다.
#
# 2.0 계열은 받지 않습니다. 2026-10-19 에 나온 2.0.0 은 SDK 를 v2(`@modelcontextprotocol/server`)로
# 바꿔 `@modelcontextprotocol/sdk` 를 깔지 않으므로, sdk 1.x 를 node_modules 에서 찾는 memory
# 포크(mcp_servers/memory_scoped)가 기동하지 못합니다. 이 패키지들은 2025 년부터 날짜형 버전을
# 써서 2026.8.31 이 semver 로는 2.0.0 보다 큽니다 — `<2.0.0` 이라고 적으면 npm 은 1.0.0 을 고릅니다.
# `^2026.8.31` 은 2026.8.31 이상 2027 미만입니다. 올릴 때는 포크와 함께 확인하세요.
NODE_MCP_PACKAGES = {
    "@modelcontextprotocol/server-filesystem": "^2026.8.31",
    "@modelcontextprotocol/server-memory": "^2026.8.31",
    "@modelcontextprotocol/server-sequential-thinking": "^2026.8.31",
}


def node_packages_label() -> str:
    return ", ".join(f"{name.rsplit('/', 1)[-1]}@{spec}" for name, spec in NODE_MCP_PACKAGES.items())


def write_node_manifest(prefix: Path) -> None:
    """설치할 서버와 버전 범위를 `prefix` 의 package.json 에 적습니다.

    범위를 명령줄 인자(`이름@^2026.8.31`)로 넘기지 않습니다. 윈도우의 npm 은 npm.cmd 라
    cmd.exe 를 거치는데, 거기서 `^` 는 이스케이프 문자라 지워지고(범위가 정확한 버전이 됩니다)
    `<`·`>` 는 리다이렉션입니다. package.json 에 적고 인자 없이 `npm install` 하면 셸을
    거치지 않습니다.
    """
    prefix.mkdir(parents=True, exist_ok=True)
    path = prefix / "package.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, dict):
        dependencies = manifest["dependencies"] = {}
    dependencies.update(NODE_MCP_PACKAGES)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def node_servers_installed(prefix: Path) -> bool:
    """`prefix` 디렉터리에 정의된 Node MCP 서버 목록 및 버전 범위가 올바르게 설치되어 있는지 확인합니다.

    `node_modules` 존재 여부만 검사하면 버전 범위를 고정하기 전에 설치된 버전(2.0.0 등)을 그대로 유지하는
    문제가 발생합니다. `package.json` 의 버전 범위가 현재 요구 목록과 완전히 일치할 때만 설치를 건너뜁니다.
    """
    try:
        manifest = json.loads((prefix / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    dependencies = manifest.get("dependencies") if isinstance(manifest, dict) else None
    if not isinstance(dependencies, dict):
        return False
    return (prefix / "node_modules" / "@modelcontextprotocol").is_dir() and all(
        dependencies.get(name) == spec for name, spec in NODE_MCP_PACKAGES.items()
    )
SANDBOX_REPO_URL = "https://github.com/HaJaehee/AirgappedPySandbox"


def _run(cmd: list[str], **kwargs) -> bool:
    try:
        subprocess.run(cmd, check=True, **kwargs)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"      [실패] {' '.join(cmd[:3])}...: {exc}")
        return False


def setup_workspace() -> bool:
    """에이전트 공용 작업 공간을 만들고 git 저장소로 초기화합니다."""
    workspace = ROOT_DIR / "workspace"
    workspace.mkdir(exist_ok=True)

    if (workspace / ".git").exists():
        print("      이미 git 저장소입니다.")
        return True

    if shutil.which("git") is None:
        print("      [경고] git 을 찾을 수 없습니다. git MCP 서버는 기동에 실패합니다.")
        return False

    if not _run(["git", "init", "-q", str(workspace)]):
        return False

    # 빈 저장소에서도 git_log 등이 동작하도록 최초 커밋을 남깁니다.
    keep = workspace / ".gitkeep"
    keep.touch()
    _run(["git", "-C", str(workspace), "add", ".gitkeep"])
    _run(
        ["git", "-C", str(workspace), "-c", "user.name=multiagent",
         "-c", "user.email=multiagent@localhost", "commit", "-q", "-m", "workspace 초기화"],
    )
    print(f"      git 저장소로 초기화했습니다: {workspace}")
    return True


def setup_node_servers() -> bool:
    """공식 Node MCP 서버를 ./mcp_node 에 설치합니다."""
    target = ROOT_DIR / "mcp_node"
    if node_servers_installed(target):
        print("      이미 설치되어 있습니다.")
        return True

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        print("      [경고] npm 을 찾을 수 없습니다. Node.js LTS 설치 후 다시 실행하세요.")
        print("             filesystem / memory MCP 서버 없이 동작하게 됩니다.")
        return False

    write_node_manifest(target)
    print(f"      설치 중: {node_packages_label()}")
    ok = _run([npm, "install", "--omit=dev", "--no-audit", "--no-fund", "--prefix", str(target)])
    if ok:
        bin_dir = target / "node_modules" / ".bin"
        if bin_dir.exists():
            shutil.rmtree(bin_dir, ignore_errors=True)
    return ok


def install_forked_servers() -> bool:
    """포크한 memory MCP 서버를 ./mcp_node 안에 놓습니다.

    실행 사본이 `node_modules` 옆에 있어야 @modelcontextprotocol/sdk 와 zod 를
    찾습니다. 앱도 기동할 때 같은 일을 하므로 여기서 실패해도 치명적이지는
    않지만, 설치 직후 상태를 완결시켜 둡니다.

    공식 memory 서버 대신 포크를 쓰는 이유는 mcp_servers/memory_scoped/index.mjs
    의 머리말에 적혀 있습니다 — 요약하면, 공식 서버는 프로세스 하나에 그래프
    하나여서 다음 대화가 이전 대화의 기억을 읽습니다.
    """
    source = ROOT_DIR / "mcp_servers" / "memory_scoped" / "index.mjs"
    if not source.is_file():
        print(f"      [경고] 포크 원본을 찾을 수 없습니다: {source}")
        return False

    target = ROOT_DIR / "mcp_node" / "memory-scoped.mjs"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"      배치 완료: {target.relative_to(ROOT_DIR)}")
    return True


def setup_sandbox() -> bool:
    """코드 실행 샌드박스를 ./mcp_sandbox 에 준비합니다."""
    target = ROOT_DIR / "mcp_sandbox"
    if (target / "server.py").exists():
        print("      이미 준비되어 있습니다.")
        return True

    if shutil.which("git") is None:
        print("      [경고] git 을 찾을 수 없어 샌드박스를 받을 수 없습니다.")
        return False

    print(f"      클론 중: {SANDBOX_REPO_URL}")
    if not _run(["git", "clone", "--depth", "1", SANDBOX_REPO_URL, str(target)]):
        print("             수동으로 받으려면:")
        print(f"               git clone --depth 1 {SANDBOX_REPO_URL} ./mcp_sandbox")
        return False

    (target / "workspace").mkdir(exist_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="개발 PC용 MCP 서버 설치")
    parser.add_argument("--skip-node", action="store_true", help="Node MCP 서버 설치 건너뛰기")
    parser.add_argument("--skip-sandbox", action="store_true", help="코드 실행 샌드박스 건너뛰기")
    args = parser.parse_args()

    print("=" * 60)
    print("  MCP 서버 설치 (개발 PC용)")
    print("=" * 60)

    results: dict[str, bool] = {}

    print("[1/3] 작업 공간(./workspace) 준비 중...")
    results["workspace"] = setup_workspace()

    if args.skip_node:
        print("[2/3] Node MCP 서버 — 건너뜁니다.")
    else:
        print("[2/3] 공식 Node MCP 서버(./mcp_node) 설치 중...")
        results["mcp_node"] = setup_node_servers()
        if results["mcp_node"]:
            print("      포크한 memory 서버(대화별 지식 그래프) 배치 중...")
            results["memory_scoped"] = install_forked_servers()

    if args.skip_sandbox:
        print("[3/3] 코드 실행 샌드박스 — 건너뜁니다.")
    else:
        print("[3/3] 코드 실행 샌드박스(./mcp_sandbox) 준비 중...")
        results["mcp_sandbox"] = setup_sandbox()

    print("=" * 60)
    for name, ok in results.items():
        print(f"  {'OK  ' if ok else '실패'}  {name}")
    print()
    print("  파이썬 MCP 서버(mcp-server-git 등)는 requirements.txt 에 있습니다:")
    print("      pip install -r requirements.txt")
    print()
    print("  서버를 띄운 뒤 로스터 패널의 'MCP 서버' 칩에서 연결 상태를 확인하세요.")
    print("=" * 60)

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
