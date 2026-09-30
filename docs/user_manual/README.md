# MADO 사용 설명서

**MADO: Multi-Agent Debate & Orchestration Platform** — `conf.json` 설정 파일 하나로 정의한 여러 LLM 에이전트가 MCP 도구를 활용하여 토론하고, 그 결과를 실행 가능한 산출물로 종합하는 파이썬 웹 애플리케이션입니다.

이 설명서 문서는 **기술 스택 · 핵심 기술 · 워크플로우**의 세 가지 축으로 체계화되어 있습니다. MADO를 처음 접하신다면 [시스템 개요](01-overview/README.md)부터 순서대로 살펴보시기 바랍니다.

---

## 문서 트리

```text
docs/user_manual/
├── README.md ............................ 이 문서 (전체 지도)
│
├── 01-overview/ ......................... 무엇을 하는 시스템인가
│   ├── README.md ........................ 시스템 개요
│   ├── 01-tech-stack.md ................. 기술 스택과 선택 이유
│   └── 02-architecture.md ............... 레이어 구조와 데이터 흐름
│
├── 02-getting-started/ .................. 설치 및 실행
│   ├── README.md ........................ 시작하기
│   ├── 01-installation.md ............... 설치와 첫 실행
│   └── 02-configuration.md .............. conf.json 설정
│
├── 03-core/ ............................. 핵심 기술 (모듈별 원리)
│   ├── README.md ........................ 핵심 기술 개관
│   ├── 01-config-layer.md ............... 설정 레이어 (JSON + Pydantic)
│   ├── 02-agent-pool.md ................. 에이전트 풀과 페르소나
│   ├── 03-llm-integration.md ............ LiteLLM 추상화와 도구 루프
│   ├── 04-mcp-host.md ................... MCP 호스트와 클라이언트
│   ├── 05-orchestration-engine.md ....... 오케스트레이션 엔진
│   ├── 06-debate-strategies.md .......... 토론 전략 3종
│   ├── 07-persistence.md ................ 데이터베이스와 세션 스냅샷
│   ├── 08-tool-security.md .............. 도구 보안 (허용 · 묻기 · 거부)
│   └── 09-skills.md ..................... 스킬 (필요 시 참조하는 작업 지침 및 스크립트)
│
├── 04-workflows/ ........................ 핵심 워크플로우 (실제 동작 흐름)
│   ├── README.md ........................ 워크플로우 개관
│   ├── 01-debate-turn.md ................ 토론 한 턴의 생애주기
│   ├── 02-session-lifecycle.md .......... 세션 생성 → 잠금 → 재개
│   ├── 03-roster-editing.md ............. 로스터 편집
│   ├── 04-artifact-and-export.md ........ 산출물 생성과 내보내기
│   └── 05-airgap-deployment.md .......... 폐쇄망 배포
│
└── 05-reference/ ........................ 레퍼런스 및 참조 자료
    ├── README.md ........................ 레퍼런스 개관
    ├── 01-http-api.md ................... HTTP API
    ├── 02-project-layout.md ............. 프로젝트 구조
    └── 03-testing.md .................... 테스트
```

---

## 목적별 길잡이

| 수행하려는 작업 | 권장 문서 |
| :--- | :--- |
| 시스템의 핵심 개념을 5분 안에 파악하기 | [시스템 개요](01-overview/README.md) |
| 도입된 기술 스택과 아키텍처 선정 이유 확인하기 | [기술 스택](01-overview/01-tech-stack.md) |
| 로컬 개발 PC에서 직접 기동해 보기 | [설치와 첫 실행](02-getting-started/01-installation.md) |
| 사내 LLM 게이트웨이 및 원격 API 연동하기 | [conf.json 설정](02-getting-started/02-configuration.md) |
| 신규 에이전트 추가 및 역할 수정하기 | [로스터 편집](04-workflows/03-roster-editing.md) |
| 외부 MCP 도구 서버 연동하기 | [MCP 호스트](03-core/04-mcp-host.md) |
| 에이전트의 도구 실행 권한 제어하기 | [도구 보안](03-core/08-tool-security.md) |
| 표준 작업 방식(보고서 양식·다이어그램 규칙 등)을 에이전트에 부여하기 | [스킬](03-core/09-skills.md) |
| 다자간 토론의 실행 순서와 생애주기 이해하기 | [토론 한 턴의 생애주기](04-workflows/01-debate-turn.md) |
| 오프라인 폐쇄망 환경에 패키징 및 배포하기 | [폐쇄망 배포](04-workflows/05-airgap-deployment.md) |
| 코드 수정 전 전체 아키텍처 및 디렉터리 파악하기 | [아키텍처](01-overview/02-architecture.md), [프로젝트 구조](05-reference/02-project-layout.md) |

---

## 이 문서를 HTML로 보기

```bash
python docs/render_user_manual.py
```

`docs/user_manual_html/` 디렉터리에 사이드바 탐색 트리가 포함된 반응형 정적 웹사이트가 생성됩니다. 생성 후 `docs/user_manual_html/index.html` 파일을 웹 브라우저로 열어 확인하시면 됩니다.

렌더러 스크립트는 **파이썬 표준 라이브러리만으로** 동작합니다. 생성되는 산출물 역시 외부 네트워크 요청이 전혀 발생하지 않는 완전한 독립적(Self-contained) HTML 문서이므로, 인터넷이 단절된 폐쇄망 환경으로 디렉터리째 복사하더라도 레이아웃 깨짐 없이 그대로 열람할 수 있습니다.

| 옵션 | 설명 |
| :--- | :--- |
| `--src DIR` | 입력 마크다운 디렉터리 경로입니다 (기본값: `docs/user_manual`). |
| `--out DIR` | 출력 HTML 디렉터리 경로입니다 (기본값: `docs/user_manual_html`). |
| `--clean` | 빌드 전에 출력 디렉터리를 먼저 초기화하여 비웁니다. |

---

## 관련 문서

- [README.md](../../README.md) — 저장소 최상위 안내 문서입니다 (빠른 설치 및 주요 기능 요약). 배포 패키지 내부에도 함께 동봉됩니다.
- `wiki/` — 심층 영문 기술 위키입니다 (설계 배경 및 세부 구현 원리). **개발 저장소에만** 제공됩니다.
- `CLAUDE.md` — 프로젝트 개발 명세서입니다. **개발 저장소에만** 제공됩니다.

본 사용 설명서는 배포 패키지에 항상 함께 포함되므로, 폐쇄망 설치 환경에서도 오프라인 상태로 자유롭게 열람하실 수 있습니다. → [폐쇄망 배포](04-workflows/05-airgap-deployment.md)
