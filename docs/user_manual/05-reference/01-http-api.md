# HTTP API

> 상위: [레퍼런스 개관](README.md) · 다음: [프로젝트 구조](02-project-layout.md)
>
> 관련 소스: `app/main.py`

FastAPI 기반의 HTTP REST API 엔드포인트는 **시스템 상태 모니터링 및 조회 전용**으로 제공됩니다. 실시간 토론 제어와 설정 변경은 NiceGUI 웹소켓(WebSocket) 파이프라인을 통해 처리됩니다. 본 HTTP API는 헬스체크, 런타임 상태 진단 및 외부 시스템 연동에 활용할 수 있습니다.

---

## `GET /api/health`

서버 상태, 애플리케이션 버전, 설정된 바인딩 정보 및 등록된 에이전트 목록을 반환합니다.

```json
{
  "status": "healthy",
  "version": "v0.9.1",
  "author": { "name": "Ha, Jaehee", "email": "lovesm135@naver.com" },
  "app": { "host": "127.0.0.1", "port": 8000, "debug": true },
  "registered_agents": ["orchestrator", "architect", "coder", "critic"]
}
```

`version`과 `author` 메타데이터는 [`app/about.py`](../../../app/about.py) 단일 소스에서 중앙 관리됩니다. FastAPI OpenAPI 문서 메타데이터, 웹 UI 상단 헤더의 버전 뱃지, 정보 모달(우측 상단 **ⓘ** 아이콘)이 모두 이 값을 공통 참조하므로, 버전 갱신 시 한곳만 수정하면 시스템 전체에 일관되게 반영됩니다.

---

## `GET /api/agents`

현재 활성화된 에이전트별 상세 런타임 구성을 반환합니다. **보안을 위해 실제 API 키 문자열은 응답에 절대 포함되지 않으며**, 키 설정 여부(`has_api_key`)만 불리언으로 반환합니다.

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
    "card_color": "#009688",
    "icon": "account_tree"
  }
]
```

| 응답 필드 | 의미 및 설명 |
| :--- | :--- |
| `mode` | `live` = 실제 엔드포인트 호출 준비 완료 / `unconfigured` = 발언 차례 시 오류 발생 |
| `has_api_key` | API 키의 설정 여부 (실제 키 값은 은닉됨) |
| `sequential_thinking` | 단계적 사고 설정 상태 (`prompt_template` 본문은 응답 간소화를 위해 제외) |
| `card_color`, `icon` | UI 카드에 표시되는 색상 및 아이콘 (`null`인 경우 에이전트 키 기반 자동 배정) |

**새롭게 변경한 LLM 설정이 런타임에 올바르게 적용되었는지 점검하는 가장 신속하고 정확한 방법**입니다:

```bash
curl -s localhost:8000/api/agents | python -m json.tool | grep -E '"key"|"mode"|"model"'
```

---

## `GET /agent-icon?src=<경로>`

에이전트의 커스텀 아바타 이미지 파일을 반환합니다. `src` 파라미터에는 `conf.json`의 `agents.<키>.icon`에 지정된 상대 경로(예: `data/agent_icons/<키>-<해시>.png`)를 전달합니다.

- 보안을 위해 **프로젝트 루트 디렉터리 내부의 유효한 이미지 파일만** 서빙합니다. 상위 디렉터리 탐색(`..`), 시스템 절대 경로, 비이미지 확장자 접근 요청은 즉시 차단됩니다.
- 디스크에서 해당 이미지 파일을 찾지 못하더라도 **HTTP 404 에러를 반환하지 않고 기본 로봇 SVG 아이콘을 HTTP 200으로 안전하게 반환**합니다. 브라우저 화면이 이미 렌더링된 상태에서 404가 발생하면 아바타 영역이 엑스박스(깨진 이미지)로 노출되어 사용자 경험을 훼손하기 때문입니다.

```bash
curl -si 'localhost:8000/agent-icon?src=data/agent_icons/critic-0491a7c46f.png' | head -3
```

---

## `GET /api/mcp`

현재 시스템에 등록된 MCP 서버별 실시간 연결 상태 및 도구 개수를 반환합니다. `"enabled": false`로 비활성화된 서버의 상태도 함께 포함됩니다.

MCP 서버 프로세스는 **작업 공간(Workspace)별로 독립 격리 구동**됩니다. 응답 본문의 `servers`는 기본 작업 공간에 바인딩된 상태이며, 현재 프로세스 메모리에 활성화되어 있는 모든 런타임 묶음의 상세 상태는 `runtimes` 객체에 상세히 담겨 반환됩니다.

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
    "D:\\MultiAgentOrchestrator\\workspace": {
      "workspace": "D:\\MultiAgentOrchestrator\\workspace",
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

`error` 필드에는 연결 실패 시 자식 프로세스가 출력한 표준 에러(stderr) 스트림의 앞뒤 버퍼 내용이 포함됩니다. `holders`는 해당 런타임 묶음을 현재 점유하여 토론 중인 세션 ID 목록이며, 점유 세션이 비어 있는 유휴 런타임은 `idle_ttl_seconds`(기본 300초) 대기 후 백그라운드에서 자동 회수됩니다. 자세한 내용은 [MCP 호스트](../03-core/04-mcp-host.md#실시간-연결-상태-모니터링) 문서를 참고하십시오.

---

## `GET /api/sessions/{session_id}/personas`

특정 대화 세션에서 실제로 사용되는 페르소나 설정과 잠금 여부를 조회합니다.

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

| 응답 필드 | 의미 및 설명 |
| :--- | :--- |
| `personas_locked` | `true`인 경우 첫 메시지가 전송되어 인격 및 인프라 스냅샷이 잠긴 세션을 의미합니다 (편집 불가). |
| `is_customized` | `conf.json`의 전역 기본값과 다른 세션 맞춤형 페르소나가 적용되었는지 여부. |

해당 `session_id`가 존재하지 않을 경우 HTTP 404 오류를 반환합니다.

---

## 웹 UI 페이지 경로

| URL 경로 | 페이지 설명 |
| :--- | :--- |
| `/` | MADO 메인 화면 (세션 사이드바 + 로스터 패널 + 토론 피드 + 산출물 뷰어) |
| `/personas/{session_id}` | 특정 세션 전용 페르소나 및 프롬프트 편집 화면 |

---

## 보안 및 인증 정책

기본 배포 상태에서는 **별도의 HTTP 기본 인증(Basic Auth)이 적용되지 않습니다.** 이것이 서버의 기본 바인딩 주소가 로컬 루프백(`127.0.0.1`)으로 제한되어 있는 이유입니다.

사내 네트워크 전체에 웹 UI를 공개하고자 할 경우 `conf.json`의 호스트 설정을 다음과 같이 변경할 수 있습니다:

```json
"app": { "host": "0.0.0.0" }
```

이렇게 설정하면 장비의 모든 네트워크 인터페이스로부터의 접속을 허용하게 됩니다. 비록 안전한 사내 폐쇄망 환경이라 하더라도, 외부에 개방할 때는 Nginx 등 리버스 프록시(Reverse Proxy)를 앞단에 두고 접근 제어(IP 화이트리스트 또는 인증)를 적용하는 것을 적극 권장합니다.

---

## 관련 문서

- [설치와 첫 실행](../02-getting-started/01-installation.md#6-정상-기동-확인-절차) — 기동 후 엔드포인트 점검 절차
- [MCP 호스트](../03-core/04-mcp-host.md) — MCP 런타임 풀 및 상태 진단 세부
- [에이전트 풀과 페르소나](../03-core/02-agent-pool.md) — `is_live` 상태 판별 기준

---

> 다음: [프로젝트 구조](02-project-layout.md)
