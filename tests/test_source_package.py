"""소스 갱신 패키지(`package_source.py`)가 무엇을 담는가.

폐쇄망 설치본은 이 패키지 하나로 갱신됩니다. 여기서 빠진 것은 대상 장비에
**없는 것**이고, 그 실패는 조용합니다 — `conf.json` 은 서버를 가리키는데 파일이
없어 기동만 못 하고, 화면에는 "연결 안 됨" 한 줄로 보입니다.
"""

from pathlib import Path

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
    """`--with-wheels` 로 이름을 적었을 때만 wheel 이 담깁니다.

    디렉터리를 훑다가 런타임이 딸려 들어가는 길은 그대로 막혀 있어야 합니다 —
    그러면 이 패키지가 사실상 오프라인 번들이 되어 존재 이유가 없어집니다.
    """
    assert "wheels" in package_source.FORBIDDEN_NAMES
    assert "wheels" not in package_source.SOURCE_DIRS
    assert not any(dest.startswith("wheels/") for _src, dest in package_source.PACKAGE_FILES)


def test_the_offline_bundle_carries_the_plugin_manual_and_its_examples(tmp_path, monkeypatch):
    """전체 번들은 `docs/` 를 통째로 담지 않고 고른 것만 옮깁니다. 설명서가 가리키는 예제가 빠지면 링크가 끊깁니다."""
    import package_offline

    monkeypatch.setattr(package_offline, "STAGING_DIR", tmp_path)
    package_offline.stage_docs()

    page = tmp_path / "docs" / "user_manual_html" / "03-core" / "11-plugins.html"
    assert (page.parent / "../../examples/plugins/host_tools/glossary.py").resolve().is_file()
    assert 'href="../../examples/plugins/host_tools/glossary.py"' in page.read_text(encoding="utf-8")
    assert list(tmp_path.rglob("__pycache__")) == []

    # 설정 문서의 해당 절로 바로 갑니다. 원본의 앵커는 GitHub 방식이라 렌더러가 맞춰 줍니다.
    anchor = "plugins-토론-전략과-호스트-도구-플러그인"
    assert f'02-configuration.html#{anchor}"' in page.read_text(encoding="utf-8")
    config = tmp_path / "docs" / "user_manual_html" / "02-getting-started" / "02-configuration.html"
    assert f'id="{anchor}"' in config.read_text(encoding="utf-8")
