# app/ 리팩터링 계획 — 모듈화와 코드 정제

작성 2026-10-23 · 브랜치 `refactor/app-modularization` (develop 에서 분기)

유지보수성을 위해 `app/` 의 큰 파일과 얽힌 의존을 나눕니다. **동작은 바꾸지 않는 것이 원칙**이고,
동작을 바꾸는 것은 아래 "선행 정리" 두 건뿐입니다. 단계마다 커밋을 나누고, 매 커밋에서 전체 테스트가
통과해야 합니다.

---

## 1. 출발점 (2026-10-23 측정)

- `app/` 45,490줄. 1,000줄 넘는 파일 9개가 18,815줄(41%).
- 테스트: 2,005 통과 · 2 건너뜀 · 약 62초.
- 문서(`docs/user_manual/05-reference/02-project-layout.md`)는 16,902줄 시절 구조를 적고 있어 실제와 다릅니다.

| 파일 | 줄 | 문제 |
| :--- | ---: | :--- |
| `ui/components/roster.py` | 3,416 | `AgentRosterControl` 한 클래스 3,241줄 — 대화 설정 + 전역 conf 편집 + 대화상자 7종 |
| `agents/llm.py` | 3,064 | 모듈 함수 60여 개 + `LLMCaller` 1,568줄, `_run_litellm_loop` 400줄 |
| `orchestration/engine.py` | 3,033 | v1.4 분해 뒤에도 클래스 2,797줄, `_speak` 383줄 |
| `ui/components/chat_feed.py` | 2,099 | `ChatFeed` 1,797줄 — 카드 · 스트리밍 · 승인 카드 3종 |
| `config.py` | 1,788 | 스키마 · 로더 · conf 편집기(약 680줄) · 경로/아이콘 |
| `export_mermaid.py` | 1,618 | 외부 의존 없는 독립 렌더러 — **나누지 않음** |
| `orchestration/phases/debate.py` | 1,436 | 라운드 · 병렬 · 그래프 토론이 한 파일 |
| `ui/app.py` | 1,285 | `create_ui()` 함수 하나가 1,224줄 (클로저 40여 개, 이벤트 if/elif 약 400줄) |
| `mcp/policy.py` | 1,076 | 응집도 높음 — 규칙 문법만 분리 |

## 2. 구조 문제

1. **엔진 ↔ 단계 실행기 핑퐁.** `phases/` 가 `self.engine.*` 를 111곳에서 부르고, 엔진은 다시 실행기에
   위임합니다. 엔진은 `artifacts` 의 비공개 함수를 다시 내보내고, `context_memory` 는 그것을 엔진에서
   지연 import 합니다(순환).
2. **문자열 이벤트 프로토콜.** 이벤트 약 38종을 9개 파일이 내보내고 7개 파일이 받습니다
   (`TurnRun.apply` 305줄, `ui/app.py` if/elif, 체험·쉬운 세션 화면, `easy/loop`, sidebar).
   `EventCallback` 정의가 5벌입니다.
3. **화면 패키지 경계.** easy → `trial.pages` 9곳(공용 화면 틀이 trial 안에 있음), trial → easy,
   ui(roster) → `easy.builder`·`easy.catalog`·`easy.pages.build`. 전문가·체험·쉬운 화면이 runner
   시작·재개·구독·스냅샷 병합을 각자 구현합니다.
4. **지연 import 로 가린 순환.** config → `mcp.policy.parse_rules`, llm ↔ llm_fallback ↔ model_status
   (6곳). `orchestration/__init__` 이 engine·runner 를 즉시 import 해 `state.py` 만 써도 엔진 전체가 뜹니다.
5. **잔여물.** export.py 의 timestamps 재내보내기, 엔진의 artifacts 재내보내기, `get_mcp_manager()`
   ("옛 호출자를 위한 자리"), 중복 상수·함수(`ORCHESTRATOR_KEY`, `_STYLE_LINE_RE`, `_uuid`,
   `_CONNECTION_FIELDS`, `_positive_float_env`, `_aware`), 같은 이름 다른 뜻 `clip_tool_output`.

## 3. 선행 정리 (동작이 바뀌는 두 건, 사용자 결정 2026-10-23)

- **PairSlide 연동 폐기.** PairSlide 는 폐기한 프로젝트입니다. 발표자료·스프레드시트 도구 이름(`slide_*`,
  `sheet_write_table`), 그 지침과 거부 문구의 대안 안내, 정책의 `slide_*` 분류, 쉬운 화면 카탈로그 항목,
  `.pairslide` 무시 목록을 지웁니다. **텍스트 쓰기 도구로 바이너리 문서(.pptx 등)를 쓰는 호출을 막는
  가드는 남깁니다** — PairSlide 와 무관하게 "깨진 파일을 만들고 만들었다고 보고하는" 결함을 막습니다.
- **MCP 가짜 도구 폴백 제거.** 서버가 뜨지 못하면 filesystem·search 용 가짜 도구를 등록하고, 실행하면
  `[Simulated File Write …] (Successfully wrote N bytes)` 같은 지어낸 성공을 돌려주던 경로입니다.
  끊긴 서버의 도구는 제공하지 않고, 실행 요청은 오류 관측으로 돌려줍니다.

## 4. 지켜야 할 제약

- **외부 사용처(MADOWarGame)의 공개 표면.** 아래는 이름과 시그니처를 유지합니다(`test_public_surface.py` 가 지킴).
  - `app.config.get_config`, `AgentConfig` · `app.agents.pool.get_agent_pool` · `app.agents.personas.config_snapshot_of`
  - `app.agents.llm.LLMCaller` — 무인자 생성, 재정의 지점 `_complete_unthrottled(agent, messages, tools, on_chunk, tool_choice)`, `_emit_chunk`
  - `app.orchestration.engine.OrchestratorEngine`·`get_orchestrator_engine` — `session_factory`, 교체 가능한 `llm_caller`, `run_turn(session_id=, user_prompt=, on_event=)`
  - `app.orchestration.runner.get_debate_runner` — `start(session_id, prompt, workspace)` · `app.orchestration.turns.failed_agents`
  - `app.orchestration.context_memory.LEDGER_PROMPT_MARKER`·`SUMMARY_PROMPT_MARKER` · `app.mcp.pool.get_runtime_pool`
  - `app.graph_store.graphs_dir` · `app.database.models.SessionModel`·`SessionAgentModel` · `app.database.session.init_db` · `app.main.server`
- **하위호환 장치 금지(배포 전).** 옮긴 뒤 옛 경로에서 다시 내보내지 않고, 테스트의 import·patch 대상을 같은 커밋에서 옮깁니다.
  위 공개 표면은 호환 장치가 아니라 공개 API 이므로 유지합니다.
- **테스트의 patch 지점.** `roster_module.get_config` 등 모듈 전역 patch 가 많습니다. 파일을 나누면 patch 지점도 옮기고,
  가능하면 주입으로 바꿔 patch 자체를 줄입니다.
- 화면 문구·주석에 '굳-' 대신 '고정-'. 프로젝트 이름은 MADO.

## 5. 단계

| 단계 | 내용 |
| :--- | :--- |
| **P0 안전망** | `tests/test_public_surface.py` (공개 표면 import·시그니처), `tests/test_layering.py` (AST 로 의존 방향 검사 — 남은 위반은 허용 목록, 단계마다 줄임) |
| **P1 잔여물** | 엔진·export 재내보내기 삭제, 중복 상수·함수 통합, `get_mcp_manager()` 삭제, 패키지 `__init__` 즉시 import 축소, `EventCallback` 한 벌 |
| **P2 이벤트 프로토콜** | `orchestration/events.py` — 이벤트 이름 상수 + `EventCallback`. 내보내는 쪽은 상수, 받는 쪽은 `{type: handler}` 표. `TurnRun.apply` 를 이벤트별 처리로 |
| **P3 엔진 2차 분해** | 실행기가 엔진을 되부르지 않도록 `speech.py`(발언 실행), `persistence.py`(기록), `context_builder.py`(맥락·요약·장부), `lifecycle.py`(턴 시작·재개) 로 나누고, `debate.py` 는 `rounds`·`parallel`·`graph` 로. 엔진은 조립과 `run_turn`·`resume_turn` 만 |
| **P4 화면 공용층** | `ui/shared/` (화면 틀·마크다운 확장·피드 메시지 로드 등) 로 trial·easy 상호 import 제거. 에이전트 초안·카탈로그 같은 도메인 조각은 화면 밖으로. 세 화면 공용 실행 바인딩. `create_ui` 를 `ui/expert/` 로 |
| **P5 큰 컴포넌트** | `roster.py` → `ui/components/roster/` (control · agent_card · graph_row · dialogs/), conf 적용·잠금 규칙은 NiceGUI 없는 서비스로. `chat_feed.py` → `ui/components/chat_feed/` (feed · cards · approvals …) |
| **P6 LLM 계층** | `agents/llm.py` → `agents/llm/` 패키지 (reasoning · tokens · notices · guidance · fingerprint · errors · caller · recovery). 재정의 지점 유지 |
| **P7 설정 계층** | `config.py` → `config/` 패키지 (schema · loader · editors · paths). 도구 규칙 문법을 의존 없는 모듈로 내려 config ↔ mcp 순환 해소 |
| **P8 조립·문서** | 전역 싱글턴을 `AppServices` 로 모으고(getter 이름 유지), `/api/*` 를 APIRouter 로. 구조 문서·테스트 문서·위키 갱신 |

손대지 않는 파일: `export_mermaid.py`, `mermaid_lint.py`, `security.py`, `mcp/policy.py`(규칙 문법 분리만).

## 6. 진행 방식

- 한 브랜치(`refactor/app-modularization`)에서 단계마다 커밋합니다. 단계가 앞 단계 결과에 기대므로 브랜치를 나누지 않았습니다.
- "옮기기만" 하는 커밋과 "고치는" 커밋을 나눠, 리뷰에서 diff 가 이동으로 보이게 합니다.
- 매 커밋: 전체 pytest + 공개 표면·의존 방향 테스트. 화면을 바꾼 단계(P4·P5)는 가짜 LLM 으로 띄워 확인합니다.
- 목표: 1,000줄 넘는 파일 9개 → 2개(`export_mermaid.py`, `mcp/policy.py`).

## 7. 진행 상황

| 단계 | 상태 | 비고 |
| :--- | :--- | :--- |
| 선행 정리 | 완료 | PairSlide 연동 제거, MCP 가짜 도구 폴백 제거 |
| P0 | 완료 | `test_public_surface.py`, `test_layering.py` |
| P1 | 완료 | 재내보내기·중복·미사용 import 정리, `__init__` 경량화 |
| P2 | 완료 | `app/events.py`, `orchestration/turn_run.py` (리듀서 표). 이벤트 이름표는 계획한 `orchestration/` 이 아니라 `app/` 바로 아래 — `agents/consultation.py` 도 이벤트를 보내기 때문 |
| P3 | 완료 | engine 3,033 → 495줄, context·recording·speech·conversation·turn_setup, phases 분할. 계획의 persistence·context_builder·lifecycle 는 각각 recording·conversation·turn_setup 이라는 이름으로 |
| P4 | 일부 | `app/routes.py`, `ui/shared/page.py`, `ui/run_binding.py`, `ui/expert/`. **하지 않음**: 에이전트 초안·카탈로그를 화면 밖으로 — 로스터가 아직 `easy.builder`·`easy.catalog`·`easy.pages.build` 를 씀 |
| P5 | 일부 | `roster/`·`chat_feed/` 패키지 (한 클래스를 mixin 파일로). **하지 않음**: conf 적용·잠금 규칙을 NiceGUI 없는 서비스로 — `test_agent_admin.py`·`test_roster_lock.py` 가 아직 `ui.notify` 를 바꿔 끼움 |
| P6 | 완료 | llm.py 3,064 → 974줄, `connection.py` 로 순환 해소. 계획한 `agents/llm/` 패키지가 아니라 `llm_*` 형제 모듈로 나눔 (그렇게 고른 이유는 기록 없음) |
| P7 | 완료 | `app/config/` (env · paths · schema · loader · editors), `app/tool_rules.py`. 남은 의존 방향 위반 0건 |
| P8 | 완료 | `app/api.py` 라우터, main 은 조립만. 구조·테스트 문서, 위키 개요·모듈 경계·릴리스 노트 갱신 |

결정과 이유, 하지 않은 항목은 위키 `wiki/architecture/module-boundaries.md` §6 에 정리했습니다.

**AppServices 는 하지 않았습니다.** 전역 `get_*()` 는 이미 각 모듈이 처음 불릴 때 만들어 들고 있고, 테스트와
MADOWarGame 구동기가 그 이름에 기대고 있습니다. 컨테이너를 하나 더 두면 소유자가 둘이 되어 오히려 읽기 어렵습니다.
대신 시작 순서를 `app/main.py` 의 `lifespan` 한 곳에서 정하고, 모듈 설명에 적어 두었습니다 (DB 엔진이 처음
부른 쪽의 주소로 고정된다는 주의 포함).

## 8. 결과

- 1,000줄이 넘는 파일: 9개 → 1개 (`export_mermaid.py` 1,618줄). 목표는 2개였는데, `mcp/policy.py` 가 규칙 문법을 내보내며 952줄이 됐습니다.
- `app/` 전체: 45,490 → 45,612줄. 줄이 아니라 경계를 나눈 작업이라 총량은 거의 그대로입니다.
- 의존 방향 위반(테스트로 검사): 11건 → 0건.
- 테스트: 2,005 → 2,020 통과 (안전망·이벤트 테스트 추가, 가짜 도구를 기대하던 테스트는 반대 동작으로).
- MADOWarGame 의 MADO 구동 테스트 19건 통과 (P3·P6 이후 확인).
