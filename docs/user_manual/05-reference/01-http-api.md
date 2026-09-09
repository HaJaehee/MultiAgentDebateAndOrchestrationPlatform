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
  "version": "v0.6.0",
  "author": { "name": "Ha, Jaehee", "email": "lovesm135@naver.com" },
  "app": { "host": "127.0.0.1", "port": 8000, "debug": true },
  "registered_agents": ["orchestrator", "architect", "coder", "critic"]
}
```

`version` 과 `author` 는 [`app/about.py`](../../../app/about.py) 한 곳에서 옵니다.
FastAPI 메타데이터, 화면 헤더 뱃지, 정보 모달(우측 상단 **ⓘ**)이 같은 값을
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
    "card_color": "#009688",
    "icon": "account_tree"
  }
]
```

| 필드 | 뜻 |
| :--- | :--- |
| `mode` | `live` = 실제 호출 가능 / `unconfigured` = 발언 차례에 실패 |
| `has_api_key` | 키가 설정되어 있는지 (값은 아님) |
| `sequential_thinking` | `prompt_template` 은 제외 (길어서) |
| `card_color` · `icon` | 화면 카드의 색과 아이콘. `null` 이면 에이전트 키에서 자동 |

**설정이 제대로 먹었는지 확인하는 가장 빠른 방법**입니다.

```bash
curl -s localhost:8000/api/agents | python -m json.tool | grep -E '"key"|"mode"|"model"'
```

---

## `GET /agent-icon?src=<경로>`

에이전트 아이콘 이미지. `src` 는 `conf.json` 의 `agents.<키>.icon` 에 적힌
경로입니다 (보통 `data/agent_icons/<키>-<해시>.png`).

- 내려주는 것은 **프로젝트 폴더 안의 이미지 파일** 뿐입니다. 경로가 설정에서
  오는 값이라, 그 밖(`../..`, 절대 경로)이나 이미지가 아닌 파일을 가리키면
  파일을 읽어 주지 않습니다.
- 못 찾아도 **404 가 아니라 기본 로봇 SVG 를 200 으로** 돌려줍니다. 화면은 이미
  그려진 뒤이고, 여기서 실패하면 아바타 자리가 깨진 이미지로 남습니다.

```bash
curl -si 'localhost:8000/agent-icon?src=data/agent_icons/critic-0491a7c46f.png' | head -3
```

---

## `GET /api/mcp`

MCP 서버별 연결 상태. `"enabled": false` 로 꺼 둔 서버도 함께 보고합니다.

서버 프로세스는 **작업 공간마다 따로** 뜹니다. `servers` 는 기본 작업 공간의 상태이고,
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
    "D:\MultiAgentOrchestrator\workspace": {
      "workspace": "D:\MultiAgentOrchestrator\workspace",
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

`error` 에 자식 프로세스의 stderr 갈무리가 들어갑니다. `holders` 는 그 런타임을 지금
쓰고 있는 대화의 id 이고, 비어 있으면 유휴 상태로 `idle_ttl_seconds` 뒤에 정리됩니다.
→ [MCP 호스트](../03-core/04-mcp-host.md#연결-상태)

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

| 필드 | 뜻 |
| :--- | :--- |
| `personas_locked` | `true` 면 첫 메시지를 보낸 대화 (편집 불가) |
| `is_customized` | `conf.json` 기본값과 다른 값이 적용 중 |

세션이 없으면 `404`.

---

## 화면 경로

| 경로 | 내용 |
| :--- | :--- |
| `/` | 메인 (사이드바 + 로스터 + 토론 피드 + 산출물 뷰어) |
| `/personas/{session_id}` | 세션별 페르소나 편집 |

---

## 인증

**없습니다.** 기본 바인딩이 `127.0.0.1` 인 이유입니다. 사내망에 노출한다면
리버스 프록시에서 인증을 붙이세요.

```json
"app": { "host": "0.0.0.0" }
```

이렇게 바꾸면 모든 인터페이스에 열립니다. 폐쇄망 안이라도 접근 통제를 앞단에
두는 것을 권합니다.

---

## 관련 문서

- [설치와 첫 실행](../02-getting-started/01-installation.md#6-확인) — 기동 확인 절차
- [MCP 호스트](../03-core/04-mcp-host.md) — 서버 상태의 의미
- [에이전트 풀과 페르소나](../03-core/02-agent-pool.md) — `is_live` 판정

---

> 다음: [프로젝트 구조](02-project-layout.md)
