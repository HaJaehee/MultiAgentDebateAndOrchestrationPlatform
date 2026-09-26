"""체험 서버 — 사내 방문자가 URL 만으로 멀티 에이전트 토론을 써 보는 화면.

주인 화면(`/`)과 같은 프로세스·같은 DB 에서 돌지만, 코어는 체험 서버를 모릅니다. 코어와 닿는
곳은 네 군데뿐입니다.

* `app/config.py` — `trial` 설정과 `app.llm_concurrency`.
* `app/agents/llm_gate.py` — 서버 전체의 LLM 동시 요청 상한과 대기열.
* `app/security.py` — 토큰 없는 원격 접속을 방문자로 받는 통로(`GuestGate`).
* `app/main.py` — 이 패키지를 붙이는 자리.

나머지는 모두 여기 있습니다. 템플릿으로 **잠긴 대화**를 미리 만들어 엔진에 넘기므로, 엔진은
평소처럼 대화를 돌릴 뿐입니다 (`templates.py`).

| 모듈 | 하는 일 |
|---|---|
| `models.py` | 방문자, 대화 주인, 내 템플릿, 평가 테이블 |
| `auth.py` | 이름 + PIN, 서명 쿠키, 잠금 |
| `gate.py` | 방문자가 지나갈 수 있는 경로 |
| `web.py` | 로그인·로그아웃 폼, 화면이 쓰는 방문자 확인 |
| `templates.py` | 템플릿 형식, 공식 템플릿 폴더, 대화 만들기 |
| `catalog.py` | 템플릿 참조(`t:`·`c:`)를 템플릿으로 |
| `store.py` | 내 대화·사본·평가·결과 |
| `stats.py` | 운영자 사용량 |
| `pages/` | 화면 |
"""

from fastapi import FastAPI

from app.trial import models  # noqa: F401 - init_db 의 create_all 전에 테이블을 등록합니다
from app.trial.gate import TrialGuestGate


def setup_trial(server: FastAPI) -> TrialGuestGate:
    """경로와 화면을 붙이고, 접속 미들웨어에 넘길 문지기를 돌려줍니다.

    `ui.run_with()` **전에** 불러야 합니다. 로그인 폼 경로는 FastAPI 경로라, NiceGUI 를 `/` 에
    붙인 뒤에 더하면 그 아래에 가려집니다.
    """
    from app.trial.pages import create_trial_pages
    from app.trial.web import register_trial_routes

    register_trial_routes(server)
    create_trial_pages()
    return TrialGuestGate()
