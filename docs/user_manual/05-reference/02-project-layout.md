# 프로젝트 구조

> 상위: [레퍼런스 개관](README.md) · 이전: [HTTP API](01-http-api.md) · 다음: [테스트](03-testing.md)

---

## 최상위

```text
MultiAgentDebateOrchestration/
├── app/                      애플리케이션 소스
├── tests/                    테스트 (2,147개)
├── wiki/                     영문 기술 위키
├── docs/
│   ├── user_manual/          이 문서 모음 (마크다운)
│   ├── user_manual_html/     렌더링 산출물 (생성물)
│   └── render_user_manual.py 렌더러
├── mcp_servers/              포크한 MCP 서버 원본
│   └── memory_scoped/        대화별 지식 그래프 (공식 서버의 포크)
│
├── bench/                    전략 비교 시나리오 (v1.5, python -m app.bench)
│
├── conf.example.json         설정 템플릿 (저장소에 커밋)
├── conf.json                 실제 설정 (gitignore)
├── .env.example / .env       환경변수
├── requirements.txt
│
├── setup_mcp.py              개발 PC MCP 서버 준비
├── open_browser.py           서버 응답 시 브라우저 열기
├── package_offline.py        폐쇄망 전체 번들
├── package_source.py         소스 갱신 패키지
│
├── README.md                 저장소 안내
├── LICENSE.md                라이선스 (LGPL-3.0 전문 + 제3자 고지)
└── CLAUDE.md                 프로젝트 명세서
```

생성물 (gitignore): `workspace/`, `data/`(올린 에이전트 아이콘, `data/unsaved/`),
`multiagent.db`(과 WAL 파일 `multiagent.db-wal`·`multiagent.db-shm`),
`mcp_node/`, `mcp_sandbox/`, `dist/`, `docs/user_manual_html/`

`data/unsaved/` 에는 **DB에 정상 기록되지 못한 발언·최종 보고서·산출물**이 비상 마크다운 파일로
보존됩니다. 백신 소프트웨어나 백업 유틸리티가 DB 파일에 파일 잠금(Lock)을 장시간 유지하는 예외적인 상황에
대비한 긴급 안전망이며, 해당 상황 발생 시 UI 상단에 영구 알림 배너가 표시됩니다. 평소에는 항상 빈 상태를 유지합니다.

데이터베이스 파일은 반드시 **로컬 디스크**에 배치하십시오. 네트워크 드라이브(NFS, SMB 등)에서는 동시 기록에 강한
SQLite WAL 모드가 정상 동작하지 않아(데이터 파일 손상 위험) 잠금 충돌에 취약해집니다. 장시간 무인 운영 시
기록 실패를 방지하려면 DB 저장 폴더를 백신 실시간 감시 및 클라우드 동기화·백업 대상에서 제외하는 것을 권장합니다.

---

## `app/` 상세

```text
app/
├── about.py                          22   앱 이름·버전·저작자 (단일 출처)
├── main.py                          248   조립: FastAPI 서버, lifespan(시작·종료 순서), 화면·라우터·접근 제어, CLI
├── api.py                           280   HTTP 엔드포인트 /api/* · /agent-icon
├── routes.py                         22   화면 주소 (체험·쉬운 화면)
├── events.py                         90   토론 이벤트 이름표 EventType
├── tool_rules.py                    125   도구 보안 규칙 문법 (read(경로) · net(호스트) …)
├── timestamps.py                    176   발언·턴·보고서 시각 표기 (화면·문서·보고서 공용)
├── session_ops.py                   489   세션 되돌리기·이어받기, 피드 기록 읽기
├── export.py                        229   대화 → 마크다운 문서
├── export_mermaid.py              1,618   Mermaid → StarUML·HTML 변환 (외부 의존 없음)
├── mermaid_lint.py                  491   다이어그램 문법 검사·복구
├── workspace_files.py               797   작업 공간 파일 목록·@언급·업로드·다운로드 (경로 안전장치)
├── diagnostics.py                   394   이벤트 루프 정체 스택·브라우저 끊김 보고
├── security.py                      782   원격 접속 토큰, 접근 제어 미들웨어
├── headless.py                      250   헤드리스 토론 실행 코어 (REST·MCP 공용) (v1.5)
├── mcp_server.py                    179   내장 MCP 서버 엔드포인트 /mcp (v1.5)
├── workspace_checkpoint.py          323   작업 공간 턴 체크포인트·롤백·분기 내보내기 (v1.5)
├── session_fork.py                  275   대화 세션 분기(Fork), 비교 데이터 구성 및 결론 평가 (v1.5)
├── bench.py                         336   전략 벤치마크 하네스 python -m app.bench (v1.5)
├── graph_store.py                   105   그래프 토론의 그래프 파일 data/graphs/
│
├── config/
│   ├── __init__.py                  131   공개 API (앱은 app.config 로 가져다 씀)
│   ├── env.py                        96   .env 읽기, ${VAR} 치환
│   ├── paths.py                     124   프로젝트 루트·데이터 폴더·작업 공간·아이콘 저장소
│   ├── schema.py                    730   Pydantic 모델과 기본값
│   ├── loader.py                    101   conf.json 읽기·쓰기, get_config 싱글턴
│   └── editors.py                   680   화면에서 고친 값을 conf.json 에 되쓰기
│
├── agents/
│   ├── base.py                      215   Agent 모델, 카드 색·아이콘
│   ├── pool.py                       78   AgentPool 레지스트리
│   ├── personas.py                  415   세션별 페르소나·외형, 구성 스냅샷
│   ├── skills.py                    762   스킬 스캔·도구·지침·스크립트 복사
│   ├── ask_user.py                  129   에이전트 사용자 질문 ask__ask_user (v1.5)
│   ├── consultation.py              458   동료 자문 peers__consult_agent
│   ├── llm.py                       469   LLMCaller: 호출, 폴백 판단, 한 판 호출
│   ├── llm_loop.py                  630   도구 루프 (ToolLoop 상태 + 단계별 함수)
│   ├── llm_request.py               173   시스템 프롬프트·LiteLLM 인자
│   ├── llm_tool_calls.py            281   도구 호출 한 번: 읽기·판정·실행·기록
│   ├── llm_recovery.py              568   잘린 답·빈 답·새어 나온 도구 호출 되살리기
│   ├── llm_context.py               464   토큰 어림, 컨텍스트 창 맞춤
│   ├── llm_budget.py                265   도구 상한·응답 한도 안내
│   ├── llm_tools.py                 195   도구 이름 찾기, 파일 쓰기 지침
│   ├── llm_fingerprint.py           101   거절 시 요청 지문
│   ├── llm_usage.py                 169   토큰 및 LLM 실행 시간 계측 (v1.5)
│   ├── llm_types.py                  51   LLMUnavailableError, 콜백 형식
│   ├── reasoning.py                  86   사고 과정 떼기·결론 찾기
│   ├── llm_gate.py                  154   서버 전체 LLM 동시 요청 상한
│   ├── llm_fallback.py              242   전역 기본 모델로 폴백 (ADR-030)
│   ├── connection.py                174   모델 연결 ID, 실패 분류
│   └── model_status.py              394   모델 연결 상태 확인
│
├── mcp/
│   ├── client.py                    746   stdio·HTTP 세션 하나, 도구 검색·실행
│   ├── manager.py                   736   작업 공간 하나의 서버 묶음, 도구 색인
│   ├── pool.py                      371   작업 공간별 런타임 풀
│   ├── policy.py                    952   도구 보안 판정 허용·묻기·거부
│   ├── guards.py                    102   바이너리 문서 쓰기 거부
│   └── exec_scan.py                 343   실행 전 코드 검사
│
├── orchestration/
│   ├── engine.py                    591   조립과 턴 수명 (run_turn · resume_turn)
│   ├── context.py                    51   EngineContext — 서비스가 함께 쓰는 것
│   ├── turn_setup.py                262   턴 구성·참여자·도구 게이트
│   ├── recording.py                 633   TurnRecorder — 기록, 재시도, data/unsaved/
│   ├── speech.py                    756   SpeechRunner — 발언 받기·기록·확정 (조각 묶음·초안은 _ChunkStream·_Speech)
│   ├── conversation.py              563   ConversationMemory — 맥락·요약·장부
│   ├── context_memory.py            640   대화 기억 규칙 (LLM 을 부르지 않음)
│   ├── phases/base.py                27   단계 실행기 바탕
│   ├── phases/planning.py           348   1단계: 계획과 계획 승인
│   ├── phases/debate.py             397   2단계: 라운드, 발언자 지명
│   ├── phases/parallel.py           388   병렬 지시 라운드
│   ├── phases/graph.py              695   그래프 토론
│   ├── phases/routing.py             92   지명·분배 응답 읽기
│   ├── phases/synthesis.py          766   3단계: 합성, 다이어그램 수선, 산출물
│   ├── artifacts.py                 289   코드 블록·Mermaid 정규화, 로스터 글
│   ├── strategies.py                307   토론 전략
│   ├── graph.py                     541   그래프 모델·검증·스케줄러
│   ├── graph_run.py                 304   그래프 실행 표시
│   ├── skill_draft.py               143   결론 기반 스킬 초안 생성 (v1.5)
│   ├── plan_gate.py                 430   계획 승인 규칙 (ADR-028)
│   ├── tool_gate.py                 453   도구 보안 판정을 토론에 붙임
│   ├── control.py                   693   정지·개입·한도·승인 제어 채널
│   ├── turns.py                     517   턴 기록, 끊긴 턴 (ADR-024)
│   ├── state.py                     148   DebateState · DebateMessage · ArtifactItem
│   ├── runner.py                    388   DebateRunner — 백그라운드 턴 시작·끝
│   ├── turn_run.py                  658   TurnRun — 턴 스냅샷 (이벤트별 리듀서)
│   ├── event_bus.py                 114   구독자 큐 이벤트 버스
│   └── worker.py                    168   태스크 워커 풀
│
├── database/
│   ├── search.py                    176   대화 세션 및 발언 검색 (FTS5 trigram + LIKE) (v1.5)
│   ├── models.py                    342   SQLAlchemy ORM
│   └── session.py                   258   비동기 엔진·세션 팩토리, init_db, 컬럼 이관
│
├── easy/                              비엔지니어 화면 /trial/easy (ADR-029)
├── trial/                             체험 서버 /trial
│
└── ui/
    ├── run_binding.py                85   화면이 토론에 붙는 구독 (세 화면 공용)
    ├── expert/page.py               842   전문가 화면 ExpertPage
    ├── expert/events.py             544   이벤트별 화면 반영
    ├── expert/chrome.py             104   머리 줄, 정보 창
    ├── shared/page.py               119   체험·쉬운 화면 공용 틀
    ├── personas_page.py             347   /personas/{id}
    ├── compare_page.py              162   /compare 대화 세션 비교 및 평가 (v1.5)
    ├── graph_page.py                430   /graphs/{id} 그래프 편집
    ├── theme.py                     494   색·아이콘·파비콘
    ├── mermaid_export.py            407   다이어그램 내려받기 스크립트
    ├── clipboard.py                  43   클립보드 복사
    ├── mention_input.py             221   입력창 @언급 창 (브라우저 스크립트)
    ├── diagnostics_script.py        120   브라우저 진단 보고 스크립트
    ├── math_markdown.py             266   LaTeX → MathML 마크다운
    ├── components/roster/         3,659   로스터 패널 (control · session_settings · agent_cards · mcp_panel · agent_admin · agent_dialogs · skills_panel · graph_row · common)
    ├── components/chat_feed/      2,478   토론 피드 (feed · cards · approvals · decisions · mentions · common)
    ├── components/sidebar.py        829   세션 목록
    ├── components/artifact_viewer.py   377   산출물 탭, 복사·다운로드
    ├── components/agent_appearance.py   257   카드 색·아이콘 편집기
    ├── components/model_status.py   143   모델 연결 상태 단추와 창
    ├── components/access_token.py   352   원격 접속 토큰 버튼
    ├── components/workspace_download.py   224   작업 공간 파일 다운로드 창
    ├── components/turn_changes.py   155   턴 파일 변경 내역(Diff) 및 롤백 창 (v1.5)
    ├── components/skill_draft_dialog.py   97   스킬 초안 생성 및 편집 창 (v1.5)
    ├── components/graph_canvas.py   217   그래프 편집 캔버스
    └── components/quiet_splitter.py    98   드래그가 가벼운 스플리터
```

총 49,738줄.

---

## 어디를 고쳐야 하나

| 하고 싶은 것 | 파일 |
| :--- | :--- |
| 설정 항목 추가 | `config/schema.py` (Pydantic 모델) · 화면에서 고치려면 `config/editors.py` |
| 새 토론 전략 | `orchestration/strategies.py` |
| 토론 흐름 변경 | `orchestration/phases/` (계획·토론·합성) · 턴 수명은 `orchestration/engine.py` |
| 발언 한 번의 처리 | `orchestration/speech.py` |
| 발언 맥락·요약·결정 장부 | `orchestration/conversation.py` (규칙은 `context_memory.py`) |
| 새 토론 이벤트 | `events.py` 에 이름 → 스냅샷은 `orchestration/turn_run.py`, 전문가 화면은 `ui/expert/events.py` |
| LLM 호출 파라미터 | `agents/llm_request.py` (`build_completion_kwargs`) |
| 도구 루프 | `agents/llm_loop.py` (`_run_litellm_loop` 와 단계 함수) · 도구 호출 한 번은 `agents/llm_tool_calls.py` |
| MCP 서버 다루는 방식 | `mcp/manager.py` · 작업 공간별 런타임은 `mcp/pool.py` |
| 도구 보안 규칙 | 문법은 `tool_rules.py`, 판정은 `mcp/policy.py` |
| 발언 카드 모양 | `ui/components/chat_feed/cards.py` |
| 에이전트 카드 색·아이콘 | `agents/base.py` (해석·폴백) · `ui/components/agent_appearance.py` (편집기) |
| 로스터 컨트롤 | `ui/components/roster/` (대화 설정은 `session_settings.py`, conf.json 편집은 `agent_admin.py`) |
| 전문가 화면 배치·동작 | `ui/expert/page.py` |
| 비엔지니어 화면 (홈·대화형 빌더·생각/행동/관찰 단계) | `easy/` — 도구 레이블/안내 문구는 `easy/catalog.py` |
| 체험·쉬운 화면 공용 틀 | `ui/shared/page.py` · 화면 주소는 `routes.py` |
| 스킬 (스캔·도구·스크립트 복사) | `agents/skills.py` · 기본 스킬 디렉터리는 루트의 `skills/` |
| 산출물 렌더링 | `ui/components/artifact_viewer.py` |
| 내보내기 형식 | `export.py` |
| 발언·보고서 시각 표기 | `timestamps.py` (화면·저장 문서·보고서가 모두 여기를 거칩니다) |
| API 엔드포인트 | `api.py` · 헤드리스 토론 실행은 `headless.py`, 내장 MCP 서버 도구는 `mcp_server.py` |
| 토큰 및 LLM 실행 시간 계측 | `agents/llm_usage.py` |
| 합의 판정 및 조기 종료 | 판정은 `orchestration/context_memory.py` (`ledger_converged`), 라운드 마감은 `orchestration/phases/debate.py` (`_close_round`) |
| 턴 체크포인트 및 롤백 | `workspace_checkpoint.py` · 화면은 `ui/components/turn_changes.py` |
| 대화 세션 및 발언 검색 | `database/search.py` · 화면은 `ui/components/sidebar.py` |
| 세션 분기, 비교 및 결론 평가 | `session_fork.py` · 화면은 `ui/compare_page.py` |
| 에이전트 사용자 질문 | `agents/ask_user.py` · 응답 대기는 `orchestration/control.py`, UI 카드는 `ui/components/chat_feed/approvals.py` |
| 전략 비교 벤치마크 | `bench.py` · 테스트 시나리오는 루트 디렉터리의 `bench/` |
| 결론 기반 스킬 초안 생성 | `orchestration/skill_draft.py` · 화면은 `ui/components/skill_draft_dialog.py` |
| 버전·저작자 표기 | `about.py` (여기만 고치면 전부 따라옵니다) |
| DB 스키마 | `database/models.py` |

---

## 큰 파일

1,000줄이 넘는 파일은 둘입니다.

**`export_mermaid.py` (1,618줄)** — 외부 의존 없이 Mermaid 를 StarUML·HTML 로 옮기는 독립 변환기. 다른
모듈과 얽히지 않아 나누지 않았습니다.

**`mcp/policy.py` (952줄)** — 도구 호출 판정 한 가지 일을 합니다. 규칙 문법만 `tool_rules.py` 로 내렸습니다.

나머지 큰 클래스는 관심사별 파일로 나눠 두었습니다. 로스터 패널(`AgentRosterControl`)과 토론 피드(`ChatFeed`)는
한 클래스를 mixin 파일로 나눈 것이라, 메서드를 찾을 때는 패키지 안의 파일 이름을 보시면 됩니다. 어떻게 나눴는지는
`docs/refactoring-plan.md` 에 있습니다.

---

## 의존 방향

```text
ui/ · trial/ · easy/  ──▶  orchestration/  ──▶  agents/  ──▶  mcp/  ──▶  config/  ──▶  tool_rules.py
                               │                   │
                               └──────────────▶  database/
```

**역방향 참조가 없고, 테스트가 그것을 지킵니다** (`tests/test_layering.py`, 위키 `architecture/module-boundaries.md`).
특히 `orchestration/` 은 화면도 NiceGUI 도 import 하지 않습니다 — 토론 실행 태스크가 화면 요소를 직접 참조하지
않아야 브라우저 연결이 끊어지더라도 백그라운드에서 안전하게 완료될 수 있기 때문입니다. 단계 실행기와 서비스는
엔진을 되부르지 않고, 화면 패키지끼리는 공용 부품(`ui/shared/`, `ui/components/`)만 함께 씁니다.

---

## 관련 문서

- [아키텍처](../01-overview/02-architecture.md) — 레이어와 데이터 흐름
- [핵심 기술 개관](../03-core/README.md) — 모듈별 내부 원리
- [테스트](03-testing.md) — 무엇이 검증되고 있는가

---

> 다음: [테스트](03-testing.md)
