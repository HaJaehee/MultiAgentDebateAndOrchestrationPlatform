# HTTP API

> 상위: [레퍼런스 개관](README.md) · 다음: [프로젝트 구조](02-project-layout.md)
>
> 파일: `app/main.py`

FastAPI 엔드포인트는 **조회 전용**입니다. 토론 진행과 설정 변경은 NiceGUI
화면(WebSocket)을 통합니다. 이 API 는 상태 점검과 외부 연동용입니다.

---

## `GET /api/health`

서버·설정·등록된 에이전트.

```json
{
  "status": "healthy",
  "version": "v1.5",
  "author": { "name": "Ha, Jaehee", "email": "lovesm135@naver.com" },
  "app": { "host": "127.0.0.1", "port": 8000, "debug": true },
  "registered_agents": ["orchestrator", "architect", "coder", "critic"],
  "event_loop": {"stall_threshold_seconds": 1.0, "stalls": 0, "longest_seconds": 0.0, "last": null}
}
```

`event_loop` 은 서버 이벤트 루프가 1초 넘게 붙잡혔던 기록의 요약입니다 (횟수, 가장 긴 시간, 마지막 위치).
자세한 스택은 `data/diagnostics/stalls.log` 에 있습니다. `MADO_DIAGNOSTICS=0` 으로 감시를 끄면 `null` 입니다.

`version` 과 `author` 는 [`app/about.py`](../../../app/about.py) 한 곳에서 옵니다.
FastAPI 메타데이터, 화면 헤더 배지, 정보 모달(우측 상단 **ⓘ**)이 같은 값을
읽으므로 버전을 올릴 때 한 곳만 고치면 됩니다.

---

## `GET /api/agents`

에이전트별 유효 구성. **API 키 값 자체는 나가지 않습니다** — 있는지 여부만.

```json
[
  {
    "key": "architect",
    "name": "System Architect",
    "role": "High-Level Architecture & Tech Stack",
    "model": "anthropic/claude-3-5-sonnet-20241022",
    "api_base": null,
    "api_version": null,
    "provider": null,
    "has_api_key": true,
    "mode": "live",
    "temperature": 0.5,
    "max_tokens": 4096,
    "sequential_thinking": { "enabled": true, "mode": "prompt", "max_steps": 5, "show_steps": true },
    "allowed_mcp_servers": ["filesystem", "memory", "fetch"],
    "allowed_skills": ["mermaid-diagrams"],
    "card_color": "#009688",
    "icon": "account_tree"
  }
]
```

| 필드 | 설명 |
| :--- | :--- |
| `mode` | `live`: 실제 LLM API 호출 가능 / `unconfigured`: API 키 미설정 등으로 발언 불가 |
| `has_api_key` | 키가 설정되어 있는지 (값은 아님) |
| `sequential_thinking` | `prompt_template` 본문은 응답 축소를 위해 제외 |
| `card_color` · `icon` | 카드 색상 및 아이콘 식별자. `null`이면 키 해시 기반으로 자동 배정 |

**설정 적용 여부를 터미널에서 신속하게 검증할 수 있는 엔드포인트**입니다.

```bash
curl -s localhost:8000/api/agents | python -m json.tool | grep -E '"key"|"mode"|"model"'
```

---

## `GET /agent-icon?src=<경로>`

에이전트 아이콘 이미지. `src` 는 `conf.json` 의 `agents.<키>.icon` 에 적힌
경로입니다 (보통 `data/agent_icons/<키>-<해시>.png`).

- 보안을 위해 **애플리케이션 디렉터리 내부의 유효한 이미지 파일**만 반환합니다. 상위 디렉터리 탐색(`../..`), 절대 경로, 또는 이미지 형식이 아닌 파일에 대한 접근은 안전하게 차단됩니다.
- 요청한 아이콘 파일을 찾을 수 없더라도 **404 에러 대신 기본 로봇 SVG 아이콘을 HTTP 200으로 반환**합니다. 브라우저 화면이 렌더링된 후 404가 발생하여 아바타 이미지가 깨진 상자로 노출되는 문제를 방지하기 위함입니다.

```bash
curl -si 'localhost:8000/agent-icon?src=data/agent_icons/critic-0491a7c46f.png' | head -3
```

---

## `GET /api/mcp`

MCP 서버별 연결 상태. `"enabled": false` 로 꺼 둔 서버도 함께 보고합니다.

서버 프로세스는 **세션 작업 공간별로 격리되어 독립** 구동됩니다. `servers` 는 기본 작업 공간의 상태이고,
지금 살아 있는 런타임 전부는 `runtimes` 에 담깁니다.

```json
{
  "servers": [
    {
      "name": "filesystem",
      "enabled": true,
      "command": "node",
      "connected": true,
      "available": true,
      "tool_count": 14,
      "error": null
    },
    {
      "name": "memory",
      "enabled": true,
      "command": "node",
      "connected": false,
      "available": false,
      "tool_count": 0,
      "error": "Cannot find package '@modelcontextprotocol/sdk' imported from ..."
    }
  ],
  "runtimes": {
    "D:\MultiAgentDebateOrchestration\workspace": {
      "workspace": "D:\MultiAgentDebateOrchestration\workspace",
      "holders": ["3f1c...세션 id"],
      "idle_seconds": 0.0,
      "initialized": true,
      "servers": { "filesystem": { "connected": true, "tool_count": 14, "...": "..." } }
    }
  },
  "max_runtimes": 4,
  "idle_ttl_seconds": 300.0
}
```

`error` 필드에는 연결 실패 시 자식 프로세스의 표준 에러(stderr) 로그가 캡처되어 포함됩니다. `holders`는 해당 런타임을 대여 중인 세션 ID 목록이며, 대여자가 없으면 유휴 상태로 전환되어 `idle_ttl_seconds` 경과 후 자동으로 프로세스가 정리됩니다.
→ [MCP 호스트](../03-core/04-mcp-host.md#연결-상태)

---

## `GET /api/skills`

스킬 디렉터리의 현재 상태를 조회합니다. 비활성화된 스킬과 유효하지 않은 스킬도 원인과 함께 반환합니다. 호출할 때마다 디렉터리 상태를 최신으로 스캔합니다.

```json
{
  "dir": "D:\\MultiAgentDebateOrchestration\\skills",
  "skills": [
    {
      "name": "mermaid-diagrams",
      "title": "mermaid-diagrams",
      "description": "Mermaid 다이어그램(순서도·시퀀스·클래스·상태·ER)을 작성할 때 사용합니다. ...",
      "enabled": true,
      "usable": true,
      "problem": null,
      "files": ["reference.md"],
      "scripts": []
    }
  ]
}
```

| 필드 | 설명 |
| :--- | :--- |
| `name` | 스킬 폴더명이며 `allowed_skills`에 지정하는 고유 식별자입니다. |
| `title` | YAML 머리말의 `name` 값이며 웹 UI 화면 표시용 타이틀입니다. |
| `enabled` | `skills.disabled` 목록에 포함되어 있지 않으면 `true`입니다. |
| `usable` | 활성화되어 있고 오류가 없는지 여부입니다. 에이전트에게는 이 값이 `true`인 스킬만 제공됩니다. |
| `problem` | 오류 원인(머리말 누락, 설명 누락 등)입니다. 정상적인 경우 `null`입니다. |
| `files` | SKILL.md를 제외한 부속 파일 목록입니다 (스킬 폴더 기준 상대 경로). |
| `scripts` | 부속 파일 중 파이썬 스크립트(`*.py`) 목록입니다. 실행 도구를 보유한 에이전트가 호출하면 작업 공간의 `.mado/skills/<이름>/` 디렉터리로 복사됩니다. |

→ [스킬](../03-core/09-skills.md)

---

## `GET /api/sessions/{session_id}/personas`

세션에서 실제로 쓰이는 페르소나와 잠금 여부.

```json
{
  "session_id": "a1b2c3d4-...",
  "personas_locked": true,
  "agents": [
    {
      "agent_key": "architect",
      "name": "System Architect",
      "role": "High-Level Architecture & Tech Stack",
      "system_prompt": "당신은 수석 소프트웨어 아키텍트입니다...",
      "is_customized": false
    }
  ]
}
```

| 필드 | 설명 |
| :--- | :--- |
| `personas_locked` | `true`: 첫 사용자 메시지가 전송되어 페르소나가 영구 고정된 세션 (편집 불가) |
| `is_customized` | `conf.json` 기본값에서 사용자 정의 프롬프트로 커스터마이징됨 |

세션이 없으면 `404`.

---

## `POST /api/diagnostics/client`

메인 화면의 스크립트가 보내는 진단 보고입니다. 긴 작업, 연결 끊김과 그 사유, 재접속을 받아
`data/diagnostics/client.log` 에 한 줄씩 적고 `204` 로 답합니다. 연결이 끊긴 순간에도 보낼 수 있도록
웹소켓이 아니라 HTTP 를 씁니다. 8KB 를 넘는 본문은 `413`, JSON 이 아니면 `400` 이며, 한 주소에서 1분에
60건까지 받습니다. 접근 제어는 다른 `/api/*` 와 같습니다.

---

## 헤드리스(Headless) 토론 실행 — `/api/sessions/...` (v1.5)

외부 스크립트, CI/CD 파이프라인, 타 에이전트 환경에서 웹 UI 없이도 토론을 시작하고 결과를 수신할 수 있는 REST API입니다.
동일한 세션을 웹 브라우저에서 열면 실시간으로 진행 상황을 함께 모니터링할 수 있습니다. 자세한 내용은 위키 `wiki/operations/headless-api.md`를 참고하세요.

| 엔드포인트 | 설명 |
| :--- | :--- |
| `GET /api/sessions?limit=50` | 최근 대화 세션 목록 조회 (세션 ID, 제목, 토론 전략, 참여 에이전트 목록, 진행 여부) |
| `POST /api/sessions` | 새 대화 세션을 생성합니다. 요청 본문: `title`, `strategy`, `max_rounds`(1~10), `agents`(생략 시 활성화된 전체 에이전트), `custom_instructions`, `workspace_dir`, `converge_early`, `token_budget`. 상태 코드 `201` 반환 |
| `POST /api/sessions/{id}/turns` | 토론 턴 시작 요청: `{"prompt": "...", "attended": false}`. 접수 시 `202`, 이미 진행 중인 경우 `409` |
| `POST /api/sessions/{id}/stop` | 현재까지의 발언 내용을 바탕으로 최종 결론을 도출하도록 토론 중단(Stop) 요청 |
| `GET /api/sessions/{id}/status` | 세션 실행 상태 조회: 진행 중인 경우 현재 페이즈, 상태 메시지, 사용자 응답 대기 여부를 반환하며, 유휴 상태인 경우 마지막 턴의 상태 반환 |
| `GET /api/sessions/{id}/result?wait=0` | 마지막 턴 결과 조회: 최종 결론, 생성된 아티팩트 목록, 결정 장부, 토큰 사용량, 조기 종료 여부, 무응답 에이전트 목록, 변경된 파일 통계 반환. `wait`(초 단위, 최대 600초) 파라미터로 턴 완료까지 롱 폴링 대기 가능 |
| `GET /api/sessions/{id}/events` | Server-Sent Events(SSE) 스트림: 연결 시 초기 `snapshot` 이벤트, 턴 완료 시 `run_finished` 이벤트 수신. 진행 중인 턴이 없으면 `idle` 이벤트 후 대기 |

오류 발생 시에는 `{"error": "..."}` 형태의 JSON과 함께 적절한 HTTP 상태 코드(`400`: 유효하지 않은 전략/에이전트/빈 요청/그래프 토론, `404`: 세션 없음, `409`: 턴 충돌)가 반환됩니다.

**비대면(Unattended) 턴 실행.** API를 통해 시작된 턴은 기본적으로 사용자가 대화창에 참여하지 않는 `attended=false` 모드로 동작합니다.
계획 승인 카드를 띄우지 않고 자동 승인하며, 도구 보안 정책상 "사용자 확인(Ask)"으로 분류된 호출은 안전을 위해 자동 거부되고,
예산 한도 도달 시에도 별도 확인 없이 즉시 발언을 마무리합니다. 사용자가 웹 UI에서 승인 및 질문 카드에 직접 응답하고자 하는 경우 `"attended": true`로 지정합니다.

```bash
curl -s -X POST http://127.0.0.1:8000/api/sessions -H "Content-Type: application/json" -d "{"max_rounds": 2}"
curl -s -X POST http://127.0.0.1:8000/api/sessions/<id>/turns -H "Content-Type: application/json" -d "{"prompt": "..."}"
curl -s "http://127.0.0.1:8000/api/sessions/<id>/result?wait=300"
```

---

## MADO를 외부 MCP 서버로 제공 — `/mcp` (v1.5)

MADO의 토론 오케스트레이션 기능을 Model Context Protocol(MCP Streamable HTTP) 도구로 외부에 제공합니다.
제공 도구: `list_agents`, `list_debates`, `start_debate`, `get_debate_status`, `get_debate_result`(완료 대기 중 진행 알림 지원), `stop_debate`.
`start_debate` 호출 시 기존 `session_id`를 전달하면 해당 대화 세션의 맥락을 이어서 추가 질문을 진행할 수 있습니다.

```bash
claude mcp add --transport http mado http://127.0.0.1:8000/mcp
```

---

## 웹 화면 경로

| 경로 | 설명 |
| :--- | :--- |
| `/` | 메인 전문가 화면 (사이드바 + 로스터 패널 + 토론 피드 + 산출물 뷰어) |
| `/personas/{session_id}` | 세션별 페르소나 편집 |
| `/graphs/{graph_id}` | 그래프 토론 편집기 |
| `/compare?a=<id>&b=<id>` | 두 대화 세션 나란히 비교 및 결론 평가 (v1.5) |

---

## 인증 및 접근 제어

* **로컬 루프백(`127.0.0.1` / `localhost`)**에서 유입되는 요청은 마스터 권한으로 간주하여 별도 인증 없이 즉시 허용합니다.
* **외부 원격 접속 시**에는 원격 접속 토큰(`.env`의 `MADO_ACCESS_TOKEN`)이 필요합니다. 웹 브라우저는 `/login` 페이지에서
  토큰으로 로그인하여 쿠키(7일간 유지)로 인증하며, REST API(`/api/*`) 및 MCP 서버(`/mcp`) 엔드포인트는 `Authorization: Bearer <토큰>`
  헤더를 통해 인증합니다 (v1.5). 잘못된 토큰을 반복 입력할 경우 계정 잠금 정책이 동일하게 적용됩니다 (잠금 상태 시 `429 Too Many Requests`).
* 토큰이 설정되지 않은 경우 외부 원격 접속은 기본 차단됩니다. 체험 모드가 활성화된 경우 일반 방문자는 `/trial` 하위 페이지만 이용 가능하며 `/api/*` 엔드포인트는 접근할 수 없습니다.
* 타 사이트로부터의 무단 접근을 방지하기 위해 CSRF 및 교차 출처(Cross-Origin) 요청은 출처와 관계없이 엄격히 거부합니다.

## 관련 문서

- [설치와 첫 실행](../02-getting-started/01-installation.md#6-확인) — 기동 확인 절차
- [MCP 호스트](../03-core/04-mcp-host.md) — 서버 상태의 의미
- [에이전트 풀과 페르소나](../03-core/02-agent-pool.md) — `is_live` 판정

---

> 다음: [프로젝트 구조](02-project-layout.md)
