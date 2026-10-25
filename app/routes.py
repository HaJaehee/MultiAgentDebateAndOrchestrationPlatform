"""화면 주소. 여러 화면 패키지가 서로를 가리킬 때 이 이름을 씁니다.

체험 화면이 쉬운 화면으로, 전문가 화면이 쉬운 화면으로 가는 버튼이 있습니다. 주소를 각 화면
패키지에 두면 버튼 하나 때문에 패키지끼리 서로를 import 하게 되어, 여기 한 곳에 둡니다.
"""

TRIAL_HOME = "/trial"
TRIAL_LOGIN_PATH = "/trial/login"
TRIAL_LOGOUT_PATH = "/trial/logout"
TRIAL_ADMIN = "/trial/admin"

# 세션 비교 페이지 경로 (v1.5, `app/ui/compare_page.py`). 사이드바 및 분기 알림 링크에서 사용됩니다.
COMPARE_PAGE = "/compare"


def compare_url(a: str, b: str = "") -> str:
    """두 세션을 나란히 비교하는 페이지 URL을 반환합니다. `b` 를 생략하고 `a` 가 분기 세션인 경우 원본 세션이 반대쪽에 자동으로 배치됩니다."""
    return f"{COMPARE_PAGE}?a={a}" + (f"&b={b}" if b else "")

EASY_HOME = "/trial/easy"
EASY_BUILD = f"{EASY_HOME}/build"
EASY_RUN = f"{EASY_HOME}/run"
