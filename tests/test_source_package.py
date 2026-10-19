"""소스 갱신 패키지(`package_source.py`)가 무엇을 담는가.

폐쇄망 설치본은 이 패키지 하나로 갱신됩니다. 여기서 빠진 것은 대상 장비에
**없는 것**이고, 그 실패는 조용합니다 — `conf.json` 은 서버를 가리키는데 파일이
없어 기동만 못 하고, 화면에는 "연결 안 됨" 한 줄로 보입니다.
"""

import json
import sys
import zipfile
from pathlib import Path

import pytest

import package_source

ROOT = Path(__file__).resolve().parents[1]


def _packed() -> set:
    """`SOURCE_DIRS` 를 실제로 훑어서 담기는 상대경로 집합을 만듭니다."""
    packed = set()
    for name in package_source.SOURCE_DIRS:
        src = ROOT / name
        if src.is_dir():
            packed |= {rel for _src, rel in package_source.collect_dir(src, name)}
    packed |= {dest for _src, dest in package_source.PACKAGE_FILES}
    return packed


def test_every_required_path_is_actually_collected():
    """무시·금지 규칙이 바뀌어 필수 파일이 걸러지면 여기서 먼저 드러납니다."""
    missing = sorted(set(package_source.REQUIRED_PACKAGE_PATHS) - _packed())
    assert missing == [], f"규칙에 걸려 빠집니다: {missing}"


def test_every_required_path_exists_in_the_tree():
    absent = [p for p in package_source.REQUIRED_PACKAGE_PATHS if not (ROOT / p).is_file()]
    assert absent == [], f"저장소에 없는 파일을 필수로 적어 두었습니다: {absent}"


def test_conf_example_is_carried_but_the_live_conf_is_not():
    """conf.json 에는 그 망의 실제 엔드포인트와 키가 들어 있어 반입 대상이 아닙니다."""
    packed = _packed()
    assert "conf.example.json" in packed
    assert "conf.json" not in packed


def test_runtime_directories_stay_out():
    for name in ("python_runtime", "node_runtime", "mcp_sandbox", "workspace", "node_modules"):
        assert name in package_source.FORBIDDEN_NAMES


def test_wheels_never_arrive_by_accident():
    """wheel 은 이름으로 지정한 것(git MCP 서버, `--with-wheels`)만 담깁니다.

    디렉터리를 훑다가 런타임이 딸려 들어가는 길은 그대로 막혀 있어야 합니다 —
    그러면 이 패키지가 사실상 오프라인 번들이 되어 존재 이유가 없어집니다.
    """
    assert "wheels" in package_source.FORBIDDEN_NAMES
    assert "wheels" not in package_source.SOURCE_DIRS
    assert not any(dest.startswith("wheels/") for _src, dest in package_source.PACKAGE_FILES)


# --------------------------------------------------------------- git MCP 서버


def test_git_mcp_package_is_the_one_conf_example_runs():
    """conf.example.json 의 git 서버가 실행하는 모듈과 담는 패키지·확인하는 wheel 이 같아야 합니다."""
    conf = json.loads((ROOT / "conf.example.json").read_text(encoding="utf-8"))
    args = conf["mcp_servers"]["git"]["args"]
    module = args[args.index("-m") + 1]

    assert module == "mcp_server_git"
    assert package_source.GIT_MCP_PACKAGES == ["mcp-server-git"]
    assert package_source.GIT_MCP_WHEEL_PATTERN == f"wheels/{module}-*.whl"


def _run_main(monkeypatch, tmp_path, *flags, wheel_name="mcp_server_git-2026.1.14-py3-none-any.whl"):
    """패키징을 실제로 돌리되 wheel 내려받기만 대역으로 바꿉니다 (네트워크 없이)."""
    requested = []

    def fake_collect_wheels(packages, build_dir):
        requested.append(list(packages))
        build_dir.mkdir(parents=True, exist_ok=True)
        wheel = build_dir / wheel_name
        wheel.write_bytes(b"wheel")
        return [(wheel, f"wheels/{wheel_name}")]

    monkeypatch.setattr(package_source, "collect_wheels", fake_collect_wheels)
    monkeypatch.setattr(sys, "argv", ["package_source.py", "--skip-manual-html",
                                      "--out-dir", str(tmp_path), *flags])
    package_source.main()
    return requested


def _zip_names(tmp_path) -> set:
    (archive,) = tmp_path.glob(f"{package_source.PACKAGE_NAME}_*.zip")
    with zipfile.ZipFile(archive) as zf:
        return set(zf.namelist())


def test_git_mcp_wheel_is_packed_by_default(monkeypatch, tmp_path):
    requested = _run_main(monkeypatch, tmp_path)

    assert requested == [["mcp-server-git"]]
    assert f"{package_source.PACKAGE_NAME}/wheels/mcp_server_git-2026.1.14-py3-none-any.whl" in _zip_names(tmp_path)


def test_packaging_stops_when_the_git_wheel_is_missing(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, wheel_name="gitpython-3.1.45-py3-none-any.whl")

    assert package_source.GIT_MCP_WHEEL_PATTERN in capsys.readouterr().out
    assert not list(tmp_path.glob("*.zip"))


def test_skip_git_mcp_packs_source_only(monkeypatch, tmp_path):
    requested = _run_main(monkeypatch, tmp_path, "--skip-git-mcp")

    assert requested == []
    assert not any("/wheels/" in name for name in _zip_names(tmp_path))
