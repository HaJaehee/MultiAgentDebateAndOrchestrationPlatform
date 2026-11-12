"""소스 갱신 패키지(`package_source.py`)가 무엇을 담는가.

폐쇄망 설치본은 이 패키지 하나로 갱신됩니다. 여기서 빠진 것은 대상 장비에
**없는 것**이고, 그 실패는 조용합니다 — `conf.json` 은 서버를 가리키는데 파일이
없어 기동만 못 하고, 화면에는 "연결 안 됨" 한 줄로 보입니다.
"""

import sys
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


def test_acceptance_tests_travel_only_when_asked_for():
    """실제 모델을 호출하는 인수 테스트는 기본 패키지에 포함하지 않습니다. 폐쇄망에서 새 모델을 검증할 때만 옵션을 지정해 포함합니다."""
    assert package_source.ACCEPTANCE_DIR not in package_source.SOURCE_DIRS
    assert not any(rel.startswith("real_llm_tests/") for rel in _packed())

    carried = {rel for _src, rel in package_source.collect_acceptance()}
    for needed in ("conftest.py", "harness.py", "README.md", "test_00_connectivity.py", "test_05_modelcheck.py",
                   "test_10_debate_core.py", "test_40_tool_context.py", "fixtures/webdocs_server.py"):
        assert f"real_llm_tests/{needed}" in carried
    assert not any("__pycache__" in rel or rel.endswith(".pyc") for rel in carried)


def test_acceptance_tests_pass_the_same_checks_as_the_rest_of_the_package():
    """함께 패키징하는 파일도 키 검사와 크기 상한을 그대로 거칩니다."""
    items = package_source.collect_acceptance()
    assert package_source.scan_for_secrets(items) == []
    assert all(src.stat().st_size <= 2 * 1024 * 1024 for src, _rel in items)


def _load_harness():
    """`real_llm_tests/harness.py`를 모듈로 불러옵니다 (표준 라이브러리만 import하므로 앱 코드를 건드리지 않습니다)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mado_real_llm_harness", ROOT / "real_llm_tests" / "harness.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass가 자기 모듈을 찾습니다
    spec.loader.exec_module(module)
    return module


def test_unpacked_into_an_installation_the_harness_uses_that_installation(tmp_path, monkeypatch):
    """대상을 지정하지 않았을 때: 개발 저장소에서는 번들, 번들이 없으면(=설치본에 풀어 놓았으면) 그 폴더 자체를 사용합니다."""
    harness = _load_harness()
    monkeypatch.delenv("MADO_REAL_LLM_TARGET", raising=False)
    monkeypatch.setattr(harness, "DEFAULT_TARGET", tmp_path / "no-bundle-here")
    assert harness.default_target() == harness.REPO.resolve() == ROOT
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(harness, "DEFAULT_TARGET", bundle)
    assert harness.default_target() == bundle.resolve()
    monkeypatch.setenv("MADO_REAL_LLM_TARGET", str(tmp_path))
    assert harness.default_target() == tmp_path.resolve(), "지정해 주면 그 경로가 우선입니다"


def test_standard_agents_missing_from_an_operators_conf_are_filled_from_the_example(tmp_path):
    """시나리오가 이름으로 부르는 에이전트가 운영 `conf.json`에 없어도 테스트가 동작합니다. 기존 항목은 건드리지 않습니다."""
    harness = _load_harness()
    h = harness.Harness(tmp_path, tmp_path / "run", ROOT)
    agents = {
        "orchestrator": {"name": "O"},
        "critic": {"name": "우리 비평가", "model": "openai/in-house", "enabled": False},
    }
    h._supplement_standard_agents(agents)  # noqa: SLF001
    assert agents["critic"]["name"] == "우리 비평가" and agents["critic"]["model"] == "openai/in-house"
    assert agents["critic"]["enabled"] is True, "비활성화된 항목은 테스트용 사본에서만 활성화합니다"
    for key in ("architect", "coder"):
        assert agents[key]["name"] and agents[key]["system_prompt"]
        assert not {"model", "api_key", "api_base"} & set(agents[key]), "연결 정보는 전역 llm 설정을 물려받습니다"
        assert not any(name.startswith("//") for name in agents[key])
    assert h.supplemented_agents == ["architect", "coder", "critic(켬)"]


def test_the_acceptance_set_is_marked():
    for name in ("test_00_connectivity.py", "test_05_modelcheck.py", "test_10_debate_core.py", "test_40_tool_context.py"):
        assert "pytest.mark.acceptance" in (ROOT / "real_llm_tests" / name).read_text(encoding="utf-8"), name
    for name in ("test_20_revise.py", "test_50_resume.py"):
        assert "pytest.mark.acceptance" not in (ROOT / "real_llm_tests" / name).read_text(encoding="utf-8"), name
