# 프로젝트 구조

> 상위: [레퍼런스 개관](README.md) · 이전: [HTTP API](01-http-api.md) · 다음: [테스트](03-testing.md)

---

## 최상위

```text
MultiAgentDebateOrchestration/
├── app/                      애플리케이션 소스
├── tests/                    테스트 (528개)
├── wiki/                     영문 기술 위키
├── docs/
│   ├── user_manual/          이 문서 모음 (마크다운)
│   ├── user_manual_html/     렌더링 산출물 (생성물)
│   └── render_user_manual.py 렌더러
├── mcp_servers/              포크한 MCP 서버 원본
│   └── memory_scoped/        대화별 지식 그래프 (공식 서버의 포크)
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
├── about.py                   22   앱 이름·버전·저작자 (단일 출처)
├── main.py                   303   FastAPI 앱, lifespan, /api/*, /agent-icon, CLI 진입점
├── config.py               1,285   conf.json 로더·기록기, 환경변수 치환, 아이콘 저장소, Pydantic
├── session_ops.py            335   세션 생성·삭제·이어받기
├── export.py                 204   대화 → 마크다운 문서
├── workspace_files.py        546   작업 공간 파일 목록·@언급 해석·업로드 저장 (경로 안전장치)
├── timestamps.py             153   발언 시작·종료·경과, 턴 총 경과, 보고서 완료 시각 (UI·문서·보고서 공용, 상위 무의존)
├── export_mermaid.py       1,618   Mermaid → SVG/PNG 렌더러 (외부 의존 없음)
├── mermaid_lint.py           323   다이어그램 문법 검사·복구
│
├── agents/
│   ├── base.py               207   Agent 모델, 카드 색·아이콘 해석과 폴백
│   ├── pool.py                78   AgentPool 레지스트리
│   ├── llm.py              1,140   LiteLLM 호출, 도구 루프, 컨텍스트 관리
│   ├── skills.py             697   스킬 디렉터리 스캔(머리말 파서)·에이전트별 스킬 도구·지침 로드·스크립트 복사
│   └── personas.py           415   세션별 페르소나·외형 설정, 구성 스냅샷
│
├── mcp/
│   ├── manager.py            563   작업 공간 하나의 서버 묶음, 도구 색인
│   ├── pool.py               378   작업 공간별 런타임 풀 (참조 카운트·유휴 회수·상한)
│   └── client.py             780   stdio 세션, 도구 검색·실행, stderr 갈무리
│
├── orchestration/
│   ├── engine.py           2,004   계획 → 라운드 → 합성 상태 머신
│   ├── runner.py             615   백그라운드 태스크, 이벤트 팬아웃
│   ├── strategies.py         281   토론 전략 4종
│   ├── control.py            332   정지·개입·예산 승인 제어 채널
│   └── state.py               53   DebateState, DebateMessage, ArtifactItem
│
├── database/
│   ├── models.py             144   SQLAlchemy ORM (5개 테이블)
│   └── session.py             85   비동기 엔진·세션 팩토리, init_db, 컬럼 이관
│
└── ui/
    ├── app.py                784   메인 페이지 조립
    ├── personas_page.py      347   /personas/{id}
    ├── theme.py              246   색·아이콘·파비콘
    ├── mermaid_export.py     333   다이어그램 내려받기
    ├── clipboard.py           43   클립보드 복사
    ├── mention_input.py      221   입력창 @언급 창 (브라우저 스크립트)
    └── components/
        ├── roster.py       2,128   에이전트 카드, MCP 칩, 전역 설정 편집
        ├── chat_feed.py    1,157   토론 피드, 발언 카드, 도구 아코디언
        ├── sidebar.py        429   세션 목록, 생성/이름변경/삭제
        ├── artifact_viewer.py 313  산출물 탭, 복사/다운로드
        └── agent_appearance.py 257 카드 색·아이콘 편집기 (추가 / 페르소나 편집 공용)
```

총 16,902줄.

---

## 어디를 고쳐야 하나

| 하고 싶은 것 | 파일 |
| :--- | :--- |
| 설정 항목 추가 | `config.py` (Pydantic 모델 + 기록기) |
| 새 토론 전략 | `orchestration/strategies.py` |
| 토론 흐름 변경 | `orchestration/engine.py` |
| LLM 호출 파라미터 | `agents/llm.py` (`build_completion_kwargs`) |
| MCP 서버 다루는 방식 | `mcp/manager.py` · 작업 공간별 런타임은 `mcp/pool.py` |
| 발언 카드 모양 | `ui/components/chat_feed.py` |
| 에이전트 카드 색·아이콘 | `agents/base.py` (해석·폴백) · `ui/components/agent_appearance.py` (편집기) |
| 로스터 컨트롤 | `ui/components/roster.py` |
| 스킬 (스캔·도구·스크립트 복사) | `agents/skills.py` · 기본 스킬 디렉터리는 루트의 `skills/` |
| 산출물 렌더링 | `ui/components/artifact_viewer.py` |
| 내보내기 형식 | `export.py` |
| 발언·보고서 시각 표기 | `timestamps.py` (화면·저장 문서·보고서가 모두 여기를 거칩니다) |
| API 엔드포인트 | `main.py` |
| 버전·저작자 표기 | `about.py` (여기만 고치면 전부 따라옵니다) |
| DB 스키마 | `database/models.py` |

---

## 큰 파일 두 개

**`roster.py` (2,128줄)** — 로스터 패널은 이 시스템에서 가장 방대한 책임을
담당하는 핵심 컴포넌트입니다. 대화 설정(참여 토글, 전략, 라운드, 작업 공간)과 전역 설정(에이전트
추가·삭제·순서·진영·도구·겉모습, MCP 서버)을 한 화면에서 다루고, 그 둘의 잠금
규칙이 서로 다릅니다.

**`engine.py` (2,004줄)** — 토론 한 턴의 전 과정. 계획, 발언자 선정, 라운드
루프, 도구 실행 기록, 합성, 아티팩트 추출.

---

## 의존 방향

```text
ui/  ──▶  orchestration/  ──▶  agents/  ──▶  config.py
                │                  │
                └──────────────▶  mcp/  ──▶  config.py
                │
                └──────────────▶  database/
```

**역방향 참조가 없습니다.** 특히 `orchestration/` 은 `ui/` 를 import 하지
않습니다 — 토론 실행 태스크가 NiceGUI UI 요소를 직접 참조하지 않아야 브라우저 연결이 끊어지더라도
백그라운드에서 안전하게 완료될 수 있기 때문입니다.

---

## 관련 문서

- [아키텍처](../01-overview/02-architecture.md) — 레이어와 데이터 흐름
- [핵심 기술 개관](../03-core/README.md) — 모듈별 내부 원리
- [테스트](03-testing.md) — 무엇이 검증되고 있는가

---

> 다음: [테스트](03-testing.md)
