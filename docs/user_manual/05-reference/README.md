# 레퍼런스 개관

> 상위: [MADO 사용 설명서](../README.md) · 다음: [HTTP API](01-http-api.md)

이 섹션은 시스템 전반의 명세와 기술 사양을 필요할 때 즉시 조회할 수 있는 빠른 참조(Reference) 문서로 구성되어 있습니다.

---

## 참조 문서 목록

| 문서 | 주요 활용 목적 |
| :--- | :--- |
| [HTTP API](01-http-api.md) | 외부 시스템에서 세션 상태를 조회하거나 자동화 연동을 수행할 때 |
| [프로젝트 구조](02-project-layout.md) | 소스 코드를 분석하거나 신규 기능을 추가/수정하기 위해 파일 위치를 찾을 때 |
| [테스트](03-testing.md) | 코드 변경 후 회귀 결함을 검증하거나 테스트 스위트를 실행할 때 |

---

## 저장소 내 관련 문서 안내

본 사용자 설명서 외에도 프로젝트 저장소 내에 다음과 같은 기술 문서들이 제공됩니다:

| 문서 위치 | 언어 | 문서 성격 | 배포 번들 포함 여부 |
| :--- | :--- | :--- | :--- |
| `docs/user_manual/` | 한국어 | **본 사용자 설명서** (전체 기능 및 워크플로우 가이드) | 포함됨 |
| [README.md](../../../README.md) | 한국어 | 저장소 최상위 안내 (설치, 핵심 기능 요약, 설정 가이드) | 포함됨 |
| `wiki/` | 영어 | 기술 위키 (심층 설계 배경, 아키텍처 및 내부 알고리즘) | 개발 저장소 전용 |
| `CLAUDE.md` | 한국어 | 프로젝트 명세서 및 개발 규칙 (요구사항 원문) | 개발 저장소 전용 |

기술 위키와 본 설명서는 상호 보완적인 관계입니다. 영문 기술 위키(`wiki/`)가 "아키텍처 설계 의도와 내부 알고리즘의 배경"을 심도 있게 다룬다면, 본 사용자 설명서(`docs/user_manual/`)는 "실제 시스템의 사용법과 안전장치의 동작 원리"를 체계적으로 해설합니다.

---

## 주요 기본값 및 설정 요약

### 시스템 기본값

| 설정 항목 | 시스템 기본값 | 설정 파일 위치 / 소스 상수 |
| :--- | :--- | :--- |
| 웹 서버 HTTP 포트 | `8000` | `app.port` |
| 데이터베이스 URL | `sqlite+aiosqlite:///./multiagent.db` | `app.db_url` |
| 세션 최대 라운드 수 | `3` | `sessions.max_rounds` |
| 기본 토론 전략 | `sequential_debate` | `sessions.strategy` |
| 샘플링 온도(Temperature) | `0.4` (전역 llm) / `0.7` (AgentConfig 기본) | `llm.temperature` |
| 최대 응답 토큰 수 | `4096` | `max_tokens` |
| 모델 컨텍스트 창 크기 | `128000` | `max_context_window` |
| 도구 루프 호출 상한 | `30`회 | `max_tool_iterations` |
| 스트리밍 타임아웃 | `600`초 (청크 간 공백 허용 시간) | `timeout` |
| 네트워크 재시도 횟수 | `2`회 | `num_retries` |
| 발언 우선순위 기본값 | `100` (미지정 시) | `debate_priority` |
| 우선순위 재할당 증분 | `10` 단위 | 드래그 앤 드롭 재정렬 시 부여 |
| 실시간 구독 큐 상한 | `2000`개 | `MAX_QUEUED_EVENTS` |
| 샌드박스 커널 네임스페이스 상한 | `16`개 | `SANDBOX_MAX_NAMESPACES` |

### 주요 환경변수

| 환경변수명 | 주요 용도 |
| :--- | :--- |
| `APP_HOST` / `APP_PORT` | MADO 웹 애플리케이션 바인딩 주소 및 포트 |
| `APP_DB_URL` | 데이터베이스 연결 문자열 (SQLite / PostgreSQL) |
| `LLM_MODEL` / `LLM_API_BASE` / `LLM_API_KEY` | 전역 LLM 기본 모델, 엔드포인트 URL, 인증 API 키 |
| `LLM_API_VERSION` / `LLM_PROVIDER` | Azure OpenAI API 버전 지정 / LiteLLM 프로바이더 강제 |
| `ORCHESTRATOR_MODEL`, `ARCHITECT_MODEL` 등 | 에이전트별 특화 LLM 모델 식별자 지정 |
| `NODE_BIN` / `PYTHON_BIN` | MCP 도구 서버 실행을 위한 Node.js / Python 인터프리터 경로 |
| `MCP_NODE_HOME` / `MCP_SANDBOX_HOME` | 번들링된 Node MCP 서버 / Python 샌드박스 홈 디렉터리 경로 |
| `WORKSPACE_DIR` | 기본 작업 공간(Workspace) 루트 디렉터리 절대 경로 |
| `SANDBOX_EXEC_TIMEOUT` / `SANDBOX_MAX_NAMESPACES` | 샌드박스 코드 실행 제한 시간(초) / 최대 활성 커널 수 |
| `MAO_NO_BROWSER` / `MAO_BROWSER_TIMEOUT` | 기동 시 브라우저 자동 실행 비활성화 / 브라우저 열기 타임아웃 |

### 주요 상태 열거형 (Enum)

| 열거형 구분 | 정의된 상태 값 목록 |
| :--- | :--- |
| **토론 턴 상태** | `idle` `planning` `debating` `synthesizing` `completed` `error` |
| **메시지 유형** | `user` `orchestrator` `agent` `system` `error` |
| **산출물 유형** | `code` `markdown` `mermaid` `json` |
| **도구 실행 결과** | `success` `error` |
| **토론 진영** | `proponent` (제안) `critic` (비판) `neutral` (중립) |
| **토론 전략** | `sequential_debate` `adversarial_debate` `orchestrator_led` `parallel_dispatch` |
| **추론 모드** | `prompt` `native` `mcp` |

---

> 다음: [HTTP API](01-http-api.md)
