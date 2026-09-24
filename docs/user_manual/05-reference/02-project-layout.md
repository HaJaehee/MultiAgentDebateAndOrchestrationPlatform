# 프로젝트 구조

> 상위: [레퍼런스 개관](README.md) · 이전: [HTTP API](01-http-api.md) · 다음: [테스트](03-testing.md)

---

## 최상위 디렉터리 구조

```text
MultiAgentOrchestrator/
├── app/                      애플리케이션 핵심 소스 코드
├── tests/                    단위 및 통합 테스트 스위트 (528개)
├── wiki/                     영문 기술 위키 (아키텍처 및 심층 설계 문서)
├── docs/
│   ├── user_manual/          본 사용자 설명서 (마크다운 원본)
│   ├── user_manual_html/     오프라인 HTML 렌더링 산출물 (빌드 부산물)
│   └── render_user_manual.py 표준 라이브러리 기반 정적 HTML 렌더러
├── mcp_servers/              자체 포크한 MCP 서버 원본 소스
│   └── memory_scoped/        세션 격리형 지식 그래프 서버 (공식 서버 자체 포크)
│
├── conf.example.json         배포 설정 템플릿 (저장소 형상 관리 대상)
├── conf.json                 실제 런타임 배포 설정 파일 (gitignore 대상)
├── .env.example / .env       환경변수 템플릿 및 실제 자격증명 파일
├── requirements.txt          Python 패키지 의존성 정의
│
├── setup_mcp.py              개발 장비용 MCP 서버 자동 준비 스크립트
├── open_browser.py           서버 정상 기동 감지 후 브라우저 자동 실행 스크립트
├── package_offline.py        폐쇄망 반입용 전체 오프라인 번들 빌더
├── package_source.py         유지보수용 소스 갱신 패키지 빌더
│
├── README.md                 저장소 최상위 안내 문서
├── LICENSE.md                오픈소스 라이선스 고지문 (LGPL-3.0 전문 및 제3자 라이선스)
└── CLAUDE.md                 프로젝트 상세 명세서 및 코딩 표준
```

**런타임 생성 파일 (gitignore 대상)**:
`workspace/`, `data/`(업로드된 에이전트 아이콘 및 `data/unsaved/`), `multiagent.db`(및 SQLite 저널 파일 `multiagent.db-wal`, `multiagent.db-shm`), `mcp_node/`, `mcp_sandbox/`, `dist/`, `docs/user_manual_html/`

`data/unsaved/` 디렉터리에는 예기치 못한 DB 파일 잠금 등으로 인해 **데이터베이스에 끝내 기록하지 못한 에이전트 발언, 최종 보고서, 산출물 데이터**가 비상용 마크다운 파일로 긴급 보존됩니다. 백신 실시간 검사기나 디스크 백업 에이전트가 SQLite 데이터베이스 파일을 독점 점유하여 잠금이 발생하는 드문 상황을 대비한 안전망이며, 이러한 긴급 저장이 발생하면 웹 UI 화면에 사용자가 직접 닫을 때까지 유지되는 고정 경고 알림이 노출됩니다. 정상적인 운영 환경에서는 항상 비어 있습니다.

데이터베이스 파일은 반드시 **서버 로컬 고속 디스크(SSD 등)**에 배치하십시오. NFS나 SMB 같은 네트워크 공유 드라이브 환경에서는 동시 읽기/쓰기 성능을 극대화하는 SQLite WAL(Write-Ahead Logging) 모드가 정상 작동하지 않아 파일 손상 및 빈번한 락 충돌을 유발할 수 있습니다. 장시간 무인 운영 중 간헐적인 DB 기록 실패가 발생한다면, 데이터베이스가 위치한 디렉터리를 안티바이러스 백신의 실시간 검사 예외 및 클라우드 동기화/백업 대상에서 제외하는 것을 적극 권장합니다.

---

## `app/` 상세 구조

```text
app/
├── about.py                   22줄   앱 메타데이터 (이름, 버전, 저작자 정보 단일 관리)
├── main.py                   303줄   FastAPI 앱 인스턴스, 수명주기 핸들러, /api/*, /agent-icon, CLI
├── config.py               1,285줄   conf.json 로더/기록기, 환경변수 치환, Pydantic 모델 검증
├── session_ops.py            335줄   세션 생성, 삭제, 복원 및 롤백 제어
├── export.py                 204줄   대화 기록 → 마크다운 종합 보고서 내보내기 엔진
├── workspace_files.py        546줄   작업 공간 파일 스캔, @언급 해석, 안전 파일 업로드
├── timestamps.py             153줄   발언 및 턴 단위 타임스탬프 계산 및 로컬 시간대 변환
├── export_mermaid.py       1,618줄   Mermaid 다이어그램 → SVG/PNG 렌더러
├── mermaid_lint.py           323줄   Mermaid 다이어그램 문법 검사 및 자동 보정
│
├── agents/
│   ├── base.py               207줄   Agent 런타임 모델, 외형 색상 및 아이콘 결정 규칙
│   ├── pool.py                78줄   AgentPool 싱글턴 레지스트리 및 제자리 갱신
│   ├── llm.py              1,140줄   LiteLLM 파이프라인, 도구 루프, 컨텍스트 축소 제어
│   └── personas.py           415줄   세션별 페르소나 조율 및 config_snapshot 잠금
│
├── mcp/
│   ├── manager.py            563줄   단일 작업 공간용 MCP 서버 관리자 및 도구 네임스페이스 색인
│   ├── pool.py               378줄   작업 공간별 격리 런타임 풀 (참조 카운팅, 유휴 회수)
│   └── client.py             780줄   stdio 통신 세션, 도구 검색/실행, stderr 버퍼 갈무리
│
├── orchestration/
│   ├── engine.py           2,004줄   계획 → 라운드 → 합성 상태 머신 핵심 엔진
│   ├── runner.py             615줄   asyncio 백그라운드 태스크 제어 및 이벤트 브로드캐스트
│   ├── strategies.py         281줄   토론 전략 4종 (순차 토론, 디베이트, 지명, 병렬 지시)
│   ├── control.py            332줄   TurnControl (정지, 개입, 도구/컨텍스트 예산 중재 우편함)
│   └── state.py               53줄   DebateState, DebateMessage, ArtifactItem 상태 모델
│
├── database/
│   ├── models.py             144줄   SQLAlchemy 비동기 ORM 테이블 모델 (5개 엔티티)
│   └── session.py             85줄   비동기 DB 엔진, 세션 팩토리, init_db 및 무중단 마이그레이션
│
└── ui/
    ├── app.py                784줄   메인 웹 UI 페이지 조립 및 라우팅
    ├── personas_page.py      347줄   /personas/{session_id} 페르소나 전용 편집 화면
    ├── theme.py              246줄   테마 색상 팔레트, 아이콘 매핑, 파비콘 설정
    ├── mermaid_export.py     333줄   다이어그램 이미지 다운로드 컴포넌트
    ├── clipboard.py           43줄   브라우저 클립보드 복사 헬퍼
    ├── mention_input.py      221줄   입력창 @언급 자동완성 팝업 (클라이언트 JavaScript 스크립트)
    └── components/
        ├── roster.py       2,128줄   에이전트 카드 목록, 발언 순서 드래그, MCP 상태 칩, 설정 모달
        ├── chat_feed.py    1,157줄   실시간 토론 피드, 발언 카드, 도구 실행 아코디언
        ├── sidebar.py        429줄   세션 히스토리 목록, 신규 생성, 이름 변경, 세션 삭제
        ├── artifact_viewer.py 313줄  산출물 탭 뷰어, 마크다운 렌더링, 코드/다이어그램 다운로드
        └── agent_appearance.py 257줄 에이전트 외형(색상/아이콘) 편집 대화상자 컴포넌트
```

총 16,902줄의 간결하고 응집도 높은 코드베이스로 구성되어 있습니다.

---

## 기능별 수정 위치 가이드

| 구현하고자 하는 작업 | 관련 핵심 소스 파일 |
| :--- | :--- |
| 전역 설정 항목 추가 및 검증 로직 작성 | `app/config.py` (Pydantic 모델 및 기록기 함수) |
| 신규 토론 전략 알고리즘 추가 | `app/orchestration/strategies.py` |
| 토론 라운드 실행 흐름 및 상태 머신 제어 | `app/orchestration/engine.py` |
| LLM 호출 파라미터 매핑 및 컨텍스트 제어 | `app/agents/llm.py` (`build_completion_kwargs`) |
| MCP 서버 관리 방식 및 작업 공간 풀링 | `app/mcp/manager.py`, `app/mcp/pool.py` |
| 대화 피드 내 발언 카드 디자인 및 인터랙션 | `app/ui/components/chat_feed.py` |
| 에이전트 외형(카드 색상, 아이콘) 처리 | `app/agents/base.py` (해석), `app/ui/components/agent_appearance.py` (UI) |
| 로스터 패널 UI 및 전역 설정 조작 | `app/ui/components/roster.py` |
| 최종 산출물 탭 렌더링 및 다운로드 | `app/ui/components/artifact_viewer.py` |
| 마크다운 내보내기 문서 포맷 변경 | `app/export.py` |
| 발언 및 보고서의 타임스탬프 계산 | `app/timestamps.py` |
| REST API 엔드포인트 추가 | `app/main.py` |
| 애플리케이션 버전 및 저작자 정보 갱신 | `app/about.py` (단일 수정으로 전체 적용) |
| 데이터베이스 스키마 모델 변경 | `app/database/models.py` |

---

## 핵심 복합 모듈 분석

**`roster.py` (2,128줄)**:
로스터 패널 컴포넌트는 시스템에서 가장 풍부한 기능을 담당합니다. 세션별 로컬 설정(참여 토글, 토론 전략, 라운드 수, 작업 공간)과 전역 배포 설정(에이전트 추가/삭제/우선순위/진영/도구 매핑/외형, MCP 서버 관리)을 단일 패널에서 유기적으로 처리하며, 서로 다른 잠금 정책을 정확히 제어합니다.

**`engine.py` (2,004줄)**:
토론 한 턴의 전 과정을 총괄하는 상태 머신입니다. 목표 계획 수립, 동적 발언자 지명, 라운드 루프 실행, 도구 호출 이력 수집, 최종 합성 및 아티팩트 파싱까지 모든 오케스트레이션 로직이 응집되어 있습니다.

---

## 모듈 간 계층별 의존 방향

```text
ui/  ──▶  orchestration/  ──▶  agents/  ──▶  config.py
                │                  │
                └──────────────▶  mcp/  ──▶  config.py
                │
                └──────────────▶  database/
```

**하위 계층에서 상위 계층으로의 역방향 참조(Circular Reference)가 원천 배제되어 있습니다.** 특히 핵심 제어를 담당하는 `orchestration/` 계층은 프레젠테이션 계층인 `ui/`를 절대 import하지 않습니다. 토론 백그라운드 태스크가 NiceGUI UI 엘리먼트를 직접 참조하지 않아야, 브라우저 연결이 끊어지거나 새로고침되더라도 토론 프로세스가 중단 없이 안전하게 완결되기 때문입니다.

---

## 관련 문서

- [아키텍처 개요](../01-overview/02-architecture.md) — 레이어별 상세 아키텍처 및 데이터 흐름
- [핵심 기술 개관](../03-core/README.md) — 모듈별 내부 동작 원리 상세
- [테스트](03-testing.md) — 시스템 무결성을 보장하는 테스트 스위트 구조

---

> 다음: [테스트](03-testing.md)
