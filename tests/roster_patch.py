"""로스터 패널(`app.ui.components.roster`)의 바깥 의존을 테스트에서 바꿔 끼우는 도우미.

로스터는 한 클래스를 관심사별 파일(mixin)로 나눠 두었습니다. 같은 이름(`get_config`, `get_runtime_pool` …)을
여러 파일이 각자 import 하므로, 한 파일에만 끼우면 다른 파일의 메서드는 진짜를 부릅니다. 이름을 가진
파일 전부에 끼웁니다 — 테스트가 내부 파일 구성을 알 필요가 없게.
"""

import importlib
import pkgutil
from typing import Any

import app.ui.components.roster as roster_package


def roster_modules():
    for info in pkgutil.iter_modules(roster_package.__path__):
        yield importlib.import_module(f"{roster_package.__name__}.{info.name}")


def patch_roster(monkeypatch, name: str, value: Any) -> None:
    hit = False
    for module in roster_modules():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            hit = True
    assert hit, f"로스터 패키지에 {name} 를 쓰는 곳이 없습니다"
