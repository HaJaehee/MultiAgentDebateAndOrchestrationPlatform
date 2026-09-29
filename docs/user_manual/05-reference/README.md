# 레퍼런스 개관

> 상위: [MADO 사용 설명서](../README.md) · 다음: [HTTP API](01-http-api.md)

참조용 문서 모음입니다. 처음부터 순차적으로 읽지 않고, 특정 기능이나 설정값을 확인할 때 찾아보는 빠른 참고서(Quick Reference)입니다.

---

## 이 섹션의 문서

| 문서 | 언제 보는가 |
| :--- | :--- |
| [HTTP API](01-http-api.md) | 외부 시스템에서 상태를 조회하거나 연동할 때 |
| [프로젝트 구조](02-project-layout.md) | 소스 코드를 분석하거나 수정하기 전 파일 위치를 찾을 때 |
| [테스트](03-testing.md) | 코드 변경 사항이 기존 기능에 미치는 영향을 검증할 때 |

---

## 다른 곳의 문서

이 사용 설명서 외에도 저장소에 문서가 있습니다.

| 위치 | 언어 | 성격 | 배포 패키지 |
| :--- | :--- | :--- | :--- |
| `docs/user_manual/` | 한국어 | **이 문서 모음** | 포함 |
| [README.md](../../../README.md) | 한국어 | 저장소 최상위 안내. 설치, 기능 요약, 설정 가이드 | 포함 |
| `wiki/` | 영어 | 기술 위키. 설계 배경과 세부 구현 | 개발 저장소만 |
| `CLAUDE.md` | 한국어 | 프로젝트 명세서 (요구사항 원문) | 개발 저장소만 |

위키와 이 설명서는 겹치는 부분이 있습니다. 위키가 "왜 이렇게 설계했는가" 를
영어로 깊게 다룬다면, 이 설명서는 "무엇을 어떻게 쓰는가" 를 한국어로 정리합니다.

---

## 자주 찾는 값

### 기본값

| 항목 | 기본값 | 위치 |
| :--- | :--- | :--- |
| 포트 | `8000` | `app.port` |
| DB | `sqlite+aiosqlite:///./multiagent.db` | `app.db_url` |
| 최대 라운드 | `3` | `sessions.max_rounds` |
| 토론 전략 | `sequential_debate` | `sessions.strategy` |
| 온도 | `0.4` (llm) / `0.7` (AgentConfig) | |
| 응답 토큰 | `4096` | `max_tokens` |
| 컨텍스트 창 | `128000` | `max_context_window` |
| 도구 루프 한도 | `30` | `max_tool_iterations` |
| 타임아웃 | `600`초 (스트리밍 청크 수신 간격) | `timeout` |
| 재시도 | `2` | `num_retries` |
| 발언 우선순위 | `100` (미지정 시) | `debate_priority` |
| 우선순위 간격 | `10` | 드래그 시 재부여 |
| 구독 큐 상한 | `2000` | `MAX_QUEUED_EVENTS` |
| 샌드박스 커널 상한 | `16` | `SANDBOX_MAX_NAMESPACES` |

### 환경변수

| 변수 | 용도 |
| :--- | :--- |
| `APP_HOST` / `APP_PORT` | 서버 기동 주소 |
| `APP_DB_URL` | 데이터베이스 |
| `LLM_MODEL` / `LLM_API_BASE` / `LLM_API_KEY` | 전역 LLM |
| `LLM_API_VERSION` / `LLM_PROVIDER` | Azure / provider 강제 |
| `ORCHESTRATOR_MODEL`, `ARCHITECT_MODEL`, `CODER_MODEL`, `CRITIC_MODEL` | 에이전트별 모델 |
| `NODE_BIN` / `PYTHON_BIN` | MCP 서버 실행기 |
| `MCP_NODE_HOME` / `MCP_SANDBOX_HOME` | MCP 서버 위치 |
| `WORKSPACE_DIR` | 공용 작업 공간 |
| `SANDBOX_KERNEL_PYTHON` / `SANDBOX_EXEC_TIMEOUT` / `SANDBOX_MAX_NAMESPACES` | 샌드박스 |
| `MADO_NO_BROWSER` / `MADO_BROWSER_TIMEOUT` | 자동 브라우저 열기 |

### 상태 값

| 열거 | 값 |
| :--- | :--- |
| 토론 상태 | `idle` `planning` `debating` `synthesizing` `completed` `error` |
| 메시지 유형 | `user` `orchestrator` `agent` `system` `error` |
| 산출물 유형 | `code` `markdown` `mermaid` `json` |
| 도구 상태 | `success` `error` |
| 토론 진영 | `proponent` `critic` `neutral` |
| 전략 | `sequential_debate` `adversarial_debate` `orchestrator_led` |
| 사고 모드 | `prompt` `native` `mcp` |

---

> 다음: [HTTP API](01-http-api.md)
