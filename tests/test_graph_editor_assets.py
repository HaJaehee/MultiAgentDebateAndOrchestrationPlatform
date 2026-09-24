"""그래프 편집기 라이브러리 묶음 (app/ui/static/graph_editor/).

폐쇄망에서는 CDN 을 쓸 수 없어 Vue Flow 를 하나의 ES 모듈로 묶어 저장소에 싣습니다. 지키려는 것.

1. 묶음이 `vue` 외에는 바깥에서 아무것도 가져오지 않는다 — 가져오면 인터넷 없는 망에서 편집기가 뜨지 않는다.
   `vue` 는 NiceGUI 가 importmap 에 올린 것을 함께 쓴다.
2. 라이선스 고지가 함께 다닌다.
3. 소스 갱신 패키지(`package_source.py`)가 이 파일들을 실제로 담는다 — 이름 규칙에 걸려 조용히 빠지지 않는다.
"""

import re
from pathlib import Path

import package_source

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "ui" / "static" / "graph_editor"


def test_the_bundle_imports_nothing_but_vue():
    code = (ASSETS / "index.js").read_text(encoding="utf-8")
    # 압축된 import 는 `from"vue"` · `import("x")` 처럼 공백이 없습니다. 공백을 허용하면 Vue Flow 의
    # 경고 문구("Please import '@vue-flow/core/dist/style.css'")까지 import 로 잡습니다.
    specifiers = set(re.findall(r"""(?<![\w$])(?:from|import)\(?["']([^"']+)["']""", code))
    assert specifiers == {"vue"}, specifiers
    assert "http://" not in code.replace("http://www.w3.org", "") and "https://" not in code


def test_every_bundled_license_is_noticed():
    notices = (ASSETS / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    for name in ("@vue-flow/core 1.48.2", "@vueuse/core", "vue-demi", "d3-zoom", "d3-ease 3.0.1 — BSD-3-Clause"):
        assert name in notices


def test_the_source_package_carries_the_editor_assets():
    packed = {rel for _src, rel in package_source.collect_dir(ROOT / "app", "app")}
    editor_paths = {p for p in package_source.REQUIRED_PACKAGE_PATHS
                    if p.startswith("app/ui/static/graph_editor/")}
    assert editor_paths, "필수 목록에서 편집기 자산이 사라졌습니다"
    assert editor_paths <= packed
    assert all((ROOT / path).is_file() for path in editor_paths)
    assert "vendor" in package_source.FORBIDDEN_NAMES, "이 이름을 피해 graph_editor/ 에 둔 이유"
