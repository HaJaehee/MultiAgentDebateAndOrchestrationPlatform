"""방문자가 지나갈 수 있는 경로 (`app/security.py` 의 `GuestGate`).

허락하는 것은 체험 화면과, 그 화면이 돌아가는 데 필요한 NiceGUI 자원뿐입니다.

* `/trial`, `/trial/...` — 체험 화면과 로그인.
* `/_nicegui/<버전>/...` — 스크립트·스타일·컴포넌트 (누구에게나 같은 정적 파일).
* `/_nicegui/client/...` — 자기 화면의 업로드 창구 (화면 id 는 추측할 수 없는 uuid).
* `/_nicegui_ws/...` — 화면을 움직이는 웹소켓.
* `/favicon.ico`, `/agent-icon` — 그림.

막는 것 중 눈여겨볼 것은 `/_nicegui/auto/...` 와 `/_mado/download/...` 입니다. 주인 화면이 작업
공간 파일을 내려받게 할 때 잠깐 열리는 주소들이라, 같은 `/_nicegui` 아래 있어도 열면 안 됩니다.
"""

from __future__ import annotations

import nicegui

TRIAL_HOME = "/trial"

_STATIC_PREFIX = f"/_nicegui/{nicegui.__version__}/"
_ALLOWED_PREFIXES = (
    "/trial/",
    _STATIC_PREFIX,
    "/_nicegui/client/",
    "/_nicegui_ws/",
)
_ALLOWED_EXACT = frozenset({TRIAL_HOME, "/favicon.ico", "/agent-icon"})


def guest_path_allowed(path: str) -> bool:
    if not path or "\\" in path or "/../" in path or path.endswith("/.."):
        return False
    return path in _ALLOWED_EXACT or path.startswith(_ALLOWED_PREFIXES)


class TrialGuestGate:
    home_path = TRIAL_HOME

    def enabled(self) -> bool:
        try:
            from app.config import get_config

            return bool(get_config().trial.enabled)
        except Exception:  # noqa: BLE001 - 설정을 못 읽으면 닫힌 쪽으로
            return False

    def allows(self, path: str) -> bool:
        return guest_path_allowed(path)
