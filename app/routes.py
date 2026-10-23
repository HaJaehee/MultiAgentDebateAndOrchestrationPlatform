"""화면 주소. 여러 화면 패키지가 서로를 가리킬 때 이 이름을 씁니다.

체험 화면이 쉬운 화면으로, 전문가 화면이 쉬운 화면으로 가는 버튼이 있습니다. 주소를 각 화면
패키지에 두면 버튼 하나 때문에 패키지끼리 서로를 import 하게 되어, 여기 한 곳에 둡니다.
"""

TRIAL_HOME = "/trial"
TRIAL_LOGIN_PATH = "/trial/login"
TRIAL_LOGOUT_PATH = "/trial/logout"
TRIAL_ADMIN = "/trial/admin"

EASY_HOME = "/trial/easy"
EASY_BUILD = f"{EASY_HOME}/build"
EASY_RUN = f"{EASY_HOME}/run"
