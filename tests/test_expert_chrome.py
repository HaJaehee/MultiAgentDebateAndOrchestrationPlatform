"""전문가 화면의 머리 줄과 정보 창 (app/ui/expert/chrome.py).

기술 스택 배지는 머리 줄이 아니라 정보(ⓘ) 창의 `Tech Stack` 행에 둡니다.
"""

import inspect

from app.ui.expert import chrome

STACK = ["FastAPI + NiceGUI", "MCP Host", "LiteLLM Multi-Model"]


def test_the_tech_stack_badges_sit_in_the_about_dialog():
    assert [name for name, _color in chrome.TECH_STACK_BADGES] == STACK
    dialog = inspect.getsource(chrome.build_about_dialog)
    assert '("Tech Stack", None, "layers")' in dialog
    assert "TECH_STACK_BADGES" in dialog
    # 행 순서: Version 다음, License 앞
    assert dialog.index('("Version"') < dialog.index('("Tech Stack"') < dialog.index('("License"')


def test_the_header_no_longer_carries_the_stack_badges():
    header = inspect.getsource(chrome.build_header)
    for name in STACK:
        assert name not in header, name
    assert "TECH_STACK_BADGES" not in header
    # 버전 배지는 머리 줄에 그대로 둡니다.
    assert "ui.badge(APP_VERSION_LABEL" in header
