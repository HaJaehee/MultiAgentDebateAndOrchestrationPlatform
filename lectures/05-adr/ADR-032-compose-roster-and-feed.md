# ADR-032. 로스터와 피드를 믹스인(mixin) 대신 컴포넌트 합성 구조로 리팩터링하고, conf.json 처리 규칙 및 스키마 검증을 UI 레이어 외부로 분리한다

> [ADR 목록](README.md) · 이전: [ADR-031](ADR-031-assemble-streams-off-the-loop.md)

## 상태 (Status)

채택됨 · 2026-10-30 · 관련: [ADR-029](ADR-029-beginner-pages.md), [ADR-008](ADR-008-background-debate-runner.md)

## 맥락 (Context)

- v1.4.1 모듈화 작업에서 `AgentRosterControl`(3,659줄, 8개 파일)과 `ChatFeed`(2,478줄, 5개 파일)를 관심사별 믹스인 파일로 분할했습니다. 단일 객체 인터페이스를 유지하여 기존 동작은 보존되었으나, 파일만 물리적으로 분리되었을 뿐 모든 메서드가 타 파일에서 동적으로 바인딩한 `self.*` 속성을 참조하는 구조였습니다. 그 결과 개별 파일만으로는 컴포넌트 간 의존 관계를 파악하기 어려웠고, 독립적인 단위 테스트 작성 또한 불가능했습니다.
- 동일한 팩토리 함수(`get_config`, `get_runtime_pool` 등)를 여러 믹스인 파일이 제각기 import하였기 때문에, 테스트 코드에서는 `tests/roster_patch.py`를 통해 패키지 내 모든 모듈에 모의 객체를 일괄 패치해야 했습니다. 로스터 테스트의 대부분은 `ui.notify`를 모킹하고 패널 전체 UI 트리를 인스턴스화해야만 검증할 수 있었습니다.
- `conf.json` 수정 검증 규칙과 토론 진행 중 잠금(Lock) 규칙은 UI 표현과 무관한 도메인 로직임에도 화면 제어 클래스의 메서드로 포함되어 있었습니다.
- 로스터(전문가 화면)가 `easy.builder`, `easy.catalog`, `easy.pages.build`를 직접 참조하여, UI 패키지 간 상호 참조를 금지하는 모듈 아키텍처 원칙에 예외로 남아 있었습니다. 이는 유지보수 비용을 지속적으로 증가시켰으므로 구조를 전면 개선하기로 결정했습니다.

## 결정 (Decision)

**두 클래스를 생성자 주입 기반의 하위 컴포넌트 합성(Composition) 구조로 전환하고, UI 비종속 로직을 화면 외부 계층으로 분리합니다.**

- **`RosterService`(`app/orchestration/roster_service.py`)**: 로스터의 단일 비즈니스 로직 진입점입니다. 명명된 `conf.json` 쓰기, 실행 중인 애플리케이션 반영, 설정 재로드, 잠금 규칙 검증을 전담하며, 처리 결과를 `Notice` 객체로 반환합니다. NiceGUI 의존성이 전혀 없습니다. 러너, MCP 풀, 에이전트 풀, 설정을 모두 참조하면서 레이어링 테스트로 NiceGUI 참조를 차단할 수 있는 최하위 계층이므로 `orchestration/`에 배치합니다.
- **`RosterState`(`roster/state.py`)**: 현재 대화의 상태 값과 규칙(로스터 등록 여부, 선택 상태, 발언 순서 등)을 관리하며, 마찬가지로 NiceGUI 의존성을 배제합니다. 하위 컴포넌트들은 상호 속성을 직접 조회하지 않고 이 상태 객체를 공유받습니다.
- **`RosterChanges`(`roster/changes.py`)**: UI 레이어의 트랜잭션 절차(잠금 검증 → 디스크 쓰기 → 런타임 반영 → 사용자 알림 → 화면 재렌더링)를 전담합니다. 재렌더링 대상은 생성자 콜백으로 주입받습니다.
- **`control.py` 및 `feed.py`**: 하위 컴포넌트 조립만을 담당합니다. 어떤 이벤트가 어느 컴포넌트를 재렌더링하는지는 이 두 파일의 콜백 연결부를 통해 명확히 드러납니다. 하위 컴포넌트 상호 간의 import는 엄격히 금지됩니다.
- **`ChatFeed`의 공개 메서드 유지**: 세 화면(전문가, 초급자, 체험)이 공통으로 사용하는 인터페이스이므로 내부 구조 변경이 외부 화면에 영향을 주지 않도록 위임(Delegation) 메서드를 유지합니다. 로스터는 전문가 화면 전용이므로 상태 값은 `roster_control.state`에서 직접 참조합니다.
- **`AgentDraft` 및 도구/스킬 카탈로그 분리**: 도메인 객체들을 `app/agents/` 계층(`draft.py`, `catalog.py`)으로 분리합니다. 빌더 대화 UI는 공용 컴포넌트인 `ui/components/agent_builder_chat.py`로, `FormBuilderChat`은 로스터 패키지로 이관합니다. 초급자 화면 전용 요소(시연 에이전트, 예제 과제, 게스트 저장소)는 `app/easy/`에 둡니다. 이제 전문가 화면은 `app.easy`를 import할 수 없습니다.
- **`tests/test_layering.py` 아키텍처 규칙 검증**: 컴포넌트 간 상호 import, `ui/components/` 내 믹스인 및 다중 상속, 상태 객체의 NiceGUI import를 정적 분석하여 위반 시 테스트 실패로 감지합니다.

**다음 사항은 구현하지 않습니다.**

- `LLMCaller`의 믹스인 구조는 유지합니다. MADOWarGame 실행기가 상속하여 `_complete_unthrottled`를 오버라이드하는 공개 인터페이스이기 때문입니다.
- 서비스 계층에 무거운 DI(의존성 주입) 컨테이너를 도입하지 않습니다. 서비스는 기본 인자 없이 인스턴스화되며 내부에서 `get_*()`를 호출합니다 ([모듈 경계 §6.3](../../wiki/architecture/module-boundaries.md)). 테스트 시에는 컴포넌트에 Mock 서비스를 전달하거나 `roster_service` 모듈 레벨의 참조를 교체합니다.
- 레거시 심볼에 대한 별칭(Alias) 재익스포트는 제공하지 않습니다.

## 결과 (Consequences)

**이점**

- 개별 컴포넌트와 서비스를 격리하여 독립적으로 단위 테스트를 수행할 수 있습니다 (`tests/test_roster_service.py`, `tests/test_roster_components.py`, `tests/test_feed_components.py`). 임시 패치 유틸리티였던 `tests/roster_patch.py`를 완전히 제거했습니다.
- `conf.json` 수정 검증 규칙을 UI 인스턴스화 없이 임시 파일과 `Notice` 반환값만으로 신속하게 테스트할 수 있습니다.
- 화면 컴포넌트 간 결합도가 제거되어 아키텍처 의존 방향 표의 예외가 해소되었습니다.
- 브라우저 DOM 트리 수준의 회귀 검증을 통해 기존 기능과 UI 동작이 100% 동일하게 보존됨을 확인했습니다 (시나리오 50개, 287단계 검증 완료, [모듈 경계 §6.9](../../wiki/architecture/module-boundaries.md)).

**트레이드오프**

- 생성자 주입 및 콜백 연결, `ChatFeed`의 위임 메서드 추가로 인해 코드 라인 수가 소폭 증가했습니다 (로스터 3,659 → 3,917줄, 피드 2,478 → 2,658줄).
- 단일 객체 구조에서는 임의의 메서드를 자유롭게 호출할 수 있었으나, 컴포넌트 분리 후에는 타 컴포넌트와 통신하기 위해 `control.py`에 명시적인 콜백을 등록해야 합니다. 이는 의존성을 투명하게 드러내기 위한 의도된 설계입니다.
- 기존 단일 `build_ui` 구성이 여러 컴포넌트의 `build` 메서드로 분산되었습니다. 화면 전체 배치 순서는 `control.py`와 `feed.py`에서 관리하고, 세부 입력 패널 배치는 각 컴포넌트 내부에서 캡슐화합니다.
