# ADR-032. 로스터와 피드를 mixin 대신 컴포넌트로 조립하고, conf.json 규칙과 설계도를 화면 밖으로 내립니다

> [ADR 목록](README.md) · 이전: [ADR-031](ADR-031-assemble-streams-off-the-loop.md)

## 상태 (Status)

채택됨 · 2026-10-30 · 관련: [ADR-029](ADR-029-beginner-pages.md), [ADR-008](ADR-008-background-debate-runner.md)

## 맥락 (Context)

- v1.4.1 구조 정리에서 `AgentRosterControl`(3,659줄, 8개 파일)과 `ChatFeed`(2,478줄, 5개 파일)를 관심사별 mixin 파일로 나눴습니다. 한 객체로 남겨 두었기에 동작이 바뀔 수 없었지만, 파일만 나뉘었을 뿐 모든 메서드가 다른 파일이 세운 `self.*` 를 씁니다. 파일 하나를 읽어서는 무엇에 기대는지 알 수 없고, 하나만 시험할 수도 없습니다.
- 같은 이름(`get_config`, `get_runtime_pool` …)을 여러 mixin 파일이 각자 import 하므로, 테스트는 `tests/roster_patch.py` 로 패키지의 모든 모듈에 같은 이름을 한꺼번에 끼웠습니다. 로스터 시험은 대부분 `ui.notify` 를 바꿔 끼우고 패널 전체를 세워야 했습니다.
- `conf.json` 을 고치는 규칙과 토론 중 잠금 규칙은 화면이 아니지만 화면 클래스의 메서드였습니다.
- 로스터(전문가 화면)가 `easy.builder`·`easy.catalog`·`easy.pages.build` 를 가져와, 화면 패키지끼리 기대지 않는다는 원칙의 예외로 남아 있었습니다. 이 둘(P4·P5의 나머지)은 2026-10-24 에 하지 않기로 했던 일입니다. 위의 비용이 계속 들어 그 결정을 뒤집습니다.

## 결정 (Decision)

**두 클래스를 생성자로 필요한 것만 받는 하위 컴포넌트의 조립으로 바꾸고, 화면이 아닌 것은 화면 밖으로 내립니다.**

- **`RosterService`(`app/orchestration/roster_service.py`)** 가 로스터의 유일한 바깥 창구입니다. 이름 있는 `conf.json` 쓰기, 돌고 있는 앱에 반영, 다시 읽기, 잠금 규칙을 맡고, 결과는 `Notice` 로 돌려줍니다. NiceGUI 를 모릅니다. 러너·MCP 풀·에이전트 풀·설정을 모두 볼 수 있으면서 의존 방향 테스트가 NiceGUI 를 막아 주는 가장 낮은 층이라 `orchestration/` 에 둡니다.
- **`RosterState`(`roster/state.py`)** 가 이 대화의 값과 그 위의 규칙(누가 로스터에 있는가, 선택, 발언 순서)을 들고, 역시 NiceGUI 를 모릅니다. 컴포넌트는 서로의 속성을 읽지 않고 이 객체를 함께 받습니다.
- **`RosterChanges`(`roster/changes.py`)** 가 화면 쪽 절차(잠금 확인 → 쓰기 → 반영 → 알림 → 다시 그리기)를 맡습니다. 무엇을 다시 그릴지는 생성자 콜백으로 받습니다.
- **`control.py`·`feed.py` 는 조립만 합니다.** 무엇이 무엇을 다시 그리는지가 이 두 파일의 콜백으로 읽힙니다. 컴포넌트끼리는 서로를 import 하지 않습니다.
- **`ChatFeed` 의 공개 메서드는 그대로 둡니다.** 세 화면이 쓰는 면이라 안쪽이 바뀌어도 화면이 바뀌지 않게, 부품에 넘기는 메서드로 남깁니다. 로스터는 전문가 화면 하나만 쓰므로 값은 `roster_control.state` 에서 직접 읽습니다.
- **`AgentDraft` 와 도구·스킬 카탈로그는 `app/agents/` 로** 내립니다 (`draft.py`, `catalog.py`). 도우미 대화는 공용 부품 `ui/components/agent_builder_chat.py` 로, `FormBuilderChat` 은 로스터로 옮깁니다. 쉬운 화면만의 것(시연 에이전트, 예시 과제, 방문자 저장)은 `app/easy/` 에 남깁니다. 이제 전문가 화면은 `app.easy` 를 import 할 수 없습니다.
- **`tests/test_layering.py` 가 이 모양을 지킵니다.** 컴포넌트끼리의 import, `ui/components/` 의 mixin 과 다중 상속, 상태 객체의 NiceGUI import 를 실패로 알립니다.

**다음은 하지 않습니다.**

- `LLMCaller` 의 mixin 은 그대로 둡니다. MADOWarGame 구동기가 상속해 `_complete_unthrottled` 를 재정의하는 공개 표면입니다.
- 서비스에 의존성 주입 컨테이너를 만들지 않습니다. 서비스는 인자 없이 만들어지고 `get_*()` 를 부릅니다 ([모듈 경계 §6.3](../../wiki/architecture/module-boundaries.md)). 시험은 컴포넌트에 대역 서비스를 주거나 `roster_service` 한 모듈의 이름을 바꿉니다.
- 옛 이름을 다시 내보내지 않습니다. MADO 는 아직 배포 전입니다.

## 결과 (Consequences)

**장점**

- 컴포넌트와 서비스를 혼자 만들어 시험합니다 (`tests/test_roster_service.py`, `tests/test_roster_components.py`, `tests/test_feed_components.py`). `tests/roster_patch.py` 는 지웠습니다.
- `conf.json` 규칙을 임시 파일과 돌려받은 `Notice` 만으로 시험합니다. `ui.notify` 를 바꿔 끼우지 않습니다.
- 화면 사이 결합이 사라져, 의존 방향 표의 예외가 하나 줄었습니다.
- 동작이 바뀌지 않았음을 브라우저가 받는 요소 트리 수준으로 확인했습니다 (시나리오 50개, 287단계, [모듈 경계 §6.9](../../wiki/architecture/module-boundaries.md)).

**고려할 트레이드오프**

- 코드가 조금 늘었습니다 (로스터 3,659 → 3,917줄, 피드 2,478 → 2,658줄). 생성자와 콜백, `ChatFeed` 의 넘기는 메서드 몫입니다.
- 한 객체일 때는 어느 메서드든 무엇이든 부를 수 있었습니다. 이제 다른 컴포넌트에 닿으려면 `control.py` 에 콜백을 더해야 합니다. 번거로움이 곧 의존이 보인다는 뜻이라 받아들입니다.
- `build_ui` 의 배치가 여러 파일의 `build` 로 흩어졌습니다. 화면 순서는 `control.py`·`feed.py` 에서 읽고, 칸의 안쪽은 각 컴포넌트에서 읽습니다.
