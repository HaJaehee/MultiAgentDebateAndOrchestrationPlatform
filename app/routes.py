"""화면 주소. 여러 화면 패키지가 서로를 가리킬 때 이 이름을 씁니다.

체험 화면이 쉬운 화면으로, 전문가 화면이 쉬운 화면으로 가는 버튼이 있습니다. 주소를 각 화면
패키지에 두면 버튼 하나 때문에 패키지끼리 서로를 import 하게 되어, 여기 한 곳에 둡니다.
"""

TRIAL_HOME = "/trial"
TRIAL_LOGIN_PATH = "/trial/login"
TRIAL_LOGOUT_PATH = "/trial/logout"
TRIAL_ADMIN = "/trial/admin"

# 대화 비교 (v1.5, `app/ui/compare_page.py`). 사이드바와 갈래 알림이 이 주소로 갑니다.
COMPARE_PAGE = "/compare"


def compare_url(a: str, b: str = "") -> str:
    """두 대화를 나란히 보는 주소. `b` 를 비우면 `a` 가 갈래일 때 원본이 반대쪽에 섭니다."""
    return f"{COMPARE_PAGE}?a={a}" + (f"&b={b}" if b else "")

EASY_HOME = "/trial/easy"
EASY_BUILD = f"{EASY_HOME}/build"
EASY_RUN = f"{EASY_HOME}/run"
