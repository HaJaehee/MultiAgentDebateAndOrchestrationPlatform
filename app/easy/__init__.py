"""비엔지니어 화면 — AI 에이전트가 무엇인지 보여 주고, 대화로 만들고, 일을 맡겨 보는 곳 (ADR-029).

체험 서버의 주소 아래(`/trial/easy`)에 둡니다. 방문자가 지나갈 수 있는 경로(`app/trial/gate.py`)와
이름 + PIN 로그인을 그대로 쓰기 위해서입니다. 체험 코드와 코어는 이 패키지를 모릅니다. 닿는 곳은
`app/main.py` 의 `setup_easy()` 한 줄뿐이고, 대화는 체험처럼 **잠긴 대화**를 미리 만들어 엔진에
넘깁니다 (`sessions.py`).

| 모듈 | 하는 일 |
|---|---|
| `models.py` | 대화 주인, 방문자가 만든 에이전트 테이블 |
| `catalog.py` | 도구·스킬을 쉬운 말로, 시연 에이전트·예시 과제, 예제 작업 폴더 |
| `builder.py` | 대화로 에이전트 만들기 — 도우미 프롬프트, 초안 읽기·거르기, 저장 |
| `sessions.py` | 일을 맡기는 대화 만들기, 내 기록·내 에이전트 |
| `loop.py` | 토론 이벤트를 생각 → 행동 → 관찰 단계로 나누기 |
| `pages/` | 화면 |

누가 무엇을 할 수 있는지:

* **주인** — 만든 에이전트는 conf.json 에 바로 들어가고, 일을 맡길 때 도구 보안 설정을 그대로 따릅니다.
* **체험 방문자** — 만든 에이전트는 자기만 쓰는 '내 에이전트'(DB)에 남습니다. 일을 맡기면 예제 파일을
  복사해 둔 폴더에서 **읽기 전용**으로 돌고, 도구는 `filesystem` 하나만 붙습니다.
"""

from app.easy import models  # noqa: F401 - init_db 의 create_all 전에 테이블을 등록합니다


def setup_easy() -> None:
    """화면을 붙입니다. `ui.run_with()` **전에** 불러야 합니다 (체험 화면과 같습니다)."""
    from app.easy.pages import create_easy_pages

    create_easy_pages()
