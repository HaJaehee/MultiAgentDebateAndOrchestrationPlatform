# conf.json 설정

> 상위: [시작하기](README.md) · 이전: [설치와 첫 실행](01-installation.md)

`conf.json` 은 이 시스템의 **배포 설정 정본**입니다. 에이전트, 모델, 엔드포인트,
자격증명, 도구 권한, 발언 순서, MCP 서버가 전부 여기 있습니다.

구현 원리는 [설정 레이어](../03-core/01-config-layer.md)를 보세요. 이 문서는
"무엇을 어떻게 적는가" 입니다.

---

## 전체 구조

```json
{
  "app":         { },
  "llm":         { "sequential_thinking": { } },
  "mcp_servers": { "<서버이름>": { } },
  "agents":      { "<에이전트키>": { "sequential_thinking": { } } }
}
```

---

## JSON 에 없는 두 가지 규칙

### 주석 — `//` 로 시작하는 키

JSON 에는 주석 문법이 없습니다. 이 프로젝트는 **키가 `//` 로 시작하면 설명으로
보고 읽을 때 걷어냅니다.** 값은 문자열 하나 또는 문자열 배열(여러 줄)입니다.

```json
"// filesystem": [
  "공용 작업 공간 파일 I/O (공식 서버, 도구 14종).",
  "지정한 디렉터리 밖 경로는 서버가 자체적으로 차단합니다."
],
"filesystem": { "command": "${NODE_BIN:-node}", "enabled": true }
```

설명이 데이터의 일부이므로, 화면에서 에이전트를 추가·삭제해도 그대로 남습니다.

### 여러 줄 글 — 문자열 배열

`system_prompt` 와 `prompt_template` 은 문자열 배열로 적을 수 있고, 읽을 때
줄바꿈으로 이어 붙입니다. `\n` 이스케이프 한 줄로 뭉개지면 사람이 못 읽습니다.

```json
"system_prompt": [
  "당신은 수석 소프트웨어 아키텍트입니다.",
  "확장성과 유지보수성을 고려하여 구조를 제안하세요."
]
```

---

## `app` — 서버 기동

```json
"app": {
  "host": "${APP_HOST:-${HOST:-127.0.0.1}}",
  "port": "${APP_PORT:-${PORT:-8000}}",
  "db_url": "${APP_DB_URL:-sqlite+aiosqlite:///./multiagent.db}",
  "debug": true
}
```

| 키 | 타입 | 기본값 | 설명 |
| :--- | :--- | :--- | :--- |
| `host` | str | `127.0.0.1` | Uvicorn 바인딩 주소 |
| `port` | int | `8000` | HTTP 포트 (문자열로 적어도 정수로 변환) |
| `db_url` | str | `sqlite+aiosqlite:///./multiagent.db` | 비동기 SQLAlchemy URL |
| `debug` | bool | `true` | 자동 재시작 + 상세 DB 로깅 |

### 원격 접속 토큰 (다른 PC 에서 쓰기)

MADO 는 기본적으로 `127.0.0.1` 에만 열립니다. 같은 망의 다른 PC 에서 쓰려면 `APP_HOST=0.0.0.0` 으로
열고, **서버의 주인만 토큰으로 로그인**하게 합니다.

| 접속 | 규칙 |
| :--- | :--- |
| 서버 PC 자신(`127.0.0.1`, `::1`) | 토큰 없이 사용. 오른쪽 위 정보 버튼 옆에 **열쇠 버튼**이 보입니다 |
| 다른 PC | `/login` 에서 토큰 입력 → **7일** 유지. 정보 버튼 옆에 로그아웃 버튼 |
| 토큰이 없거나 형식이 틀림 | 다른 PC 의 접속은 **전부 거부** (토큰을 깜빡하고 외부에 열어도 문이 열리지 않음) |

**하위 호환 — 외부에 열린 서버인데 `.env` 에 토큰이 없을 때.** 토큰 기능 전부터 `0.0.0.0` 으로 쓰던 서버는
업데이트 직후 토큰이 없습니다. 서버 PC 에서 **첫 화면을 여는 순간** 새 토큰을 만들어 `.env` 에 저장·적용하고
팝업으로 알립니다: "외부 유저 인증 토큰이 없어 새 토큰(`토큰`)으로 서버를 시작했습니다. `.env`에 저장하였습니다."
그 전까지 다른 PC 는 막혀 있습니다. 다음 경우에는 만들지 않습니다: `127.0.0.1` 에만 열린 서버, 이미 쓸 수 있는
토큰이 있음, `.env` 에 **적혀 있지만 형식이 틀린** 값(주인이 쓴 값은 덮어쓰지 않음 — 키가 없거나 비어 있을 때만),
운영체제 환경변수로 준 값이 있음. 여러 화면이 동시에 열어도 한 번만 만들고, 저장에 실패하면 적용하지 않고 알립니다.

- **토큰**: `.env` 의 `MADO_ACCESS_TOKEN`. 영문 대소문자·숫자 **정확히 24자**. 길이와 글자 종류만
  검사합니다(`0` 24개도 받습니다). `.env` 는 git 과 폐쇄망 번들에서 빠집니다.
- **열쇠 버튼(서버 PC 에서만)**: ① `.env` 의 토큰 적용 — 손으로 고친 값을 재기동 없이 반영(형식이
  틀리면 적용하지 않고 지금 토큰 유지) ② 새 토큰 생성 후 `.env` 에 저장 — 보안 난수로 만들어 저장에
  성공한 뒤에만 적용하고, 그 창에서만 한 번 보여 줍니다. 어느 쪽이든 **모든 원격 로그인이 끊기고**
  열려 있던 원격 화면은 로그인 페이지로 돌아갑니다.
- **잠긴 IP 해제(서버 PC 에서만)**: 같은 열쇠 창 아래에 로그인 실패로 잠긴 IP 와 남은 시간이 보이고, `해제`
  (여럿이면 `모두 해제`)로 15분을 기다리지 않고 풉니다. 실패 횟수도 비워, 한 번 더 틀렸다고 곧바로 다시
  잠기지 않습니다.
- **막는 방식**: 페이지·웹소켓·`/api/*`·다운로드 모두를 지나는 맨 바깥 계층 하나(`app/security.py`).
  토큰은 POST 본문으로만 받고(URL 에 싣지 않음), 쿠키에는 토큰이 아니라 토큰에서 파생한 키로 서명한
  발급 시각이 들어갑니다(`HttpOnly`, `SameSite=Strict`). 같은 IP 에서 5번 틀리면 15분 잠급니다.
  서버 PC 에서 연 악성 웹페이지가 루프백 권한을 빌려 쓰지 못하게 **루프백에도 Host·Origin** 을 확인합니다.

> ⚠️ **HTTPS 가 없습니다.** 같은 망에서 트래픽을 볼 수 있는 사람은 로그인 순간의 토큰과 쿠키를 가로챌
> 수 있습니다. 암호화가 필요하면 `ssh -L 8000:127.0.0.1:8000 서버` 로 붙으세요 — 앱은 `127.0.0.1` 에 둔 채
> 터널로 들어온 접속은 루프백이 됩니다.
>
> ⚠️ **리버스 프록시 뒤에 두지 마세요.** 모든 접속이 프록시의 루프백 주소로 보여 누구나 주인이 됩니다.
> 토큰 하나는 주인 한 명입니다 — 토큰을 나눠 주면 모든 대화를 나눠 주는 것입니다.

---

## `llm` — 전역 LLM 기본값

여기 적은 값은 각 에이전트가 같은 키를 **직접 지정하지 않는 한** 전부에게
상속됩니다. 사내 게이트웨이나 로컬 서버를 쓸 때는 이 한 곳만 고치면 됩니다.

| 키 | 타입 | 기본값 | 설명 |
| :--- | :--- | :--- | :--- |
| `model` | str | `openai/gpt-4o` | LiteLLM 형식 `<provider>/<model>` |
| `api_base` | str | – | 엔드포인트 URL. `api_url` / `base_url` 로 적어도 동일 |
| `api_key` | str | – | 로컬 모델이면 비워도 됩니다 |
| `api_version` | str | – | Azure OpenAI 전용 |
| `provider` | str | – | LiteLLM provider 강제 지정 |
| `temperature` | float | `0.4` | 0.0 ~ 2.0 |
| `top_p` | float | – | 뉴클리어스 샘플링 |
| `max_tokens` | int | `4096` | 응답 토큰 상한 |
| `max_context_window` | int | `128000` | **엔드포인트의 실제 한도로 맞추세요** |
| `timeout` | float | `600` | 응답 조각 사이를 기다리는 최대 초 — 전체 응답 시간이 아닙니다. 긴 파일을 쓰는 도구 호출은 서버가 인자를 다 만들 때까지 아무것도 보내지 않을 수 있어 `max_tokens ÷ 초당 생성 토큰`보다 크게 잡으세요 |
| `num_retries` | int | `2` | 재시도 횟수 |
| `drop_params` | bool | `true` | 엔드포인트가 모르는 파라미터 자동 제거 |
| `max_tool_iterations` | int | `30` | 한 턴의 MCP 도구 루프 상한 (1~100) |
| `extra_headers` | dict | `{}` | 커스텀 HTTP 헤더 |
| `extra_body` | dict | `{}` | 커스텀 JSON 바디 필드 |

> **`max_context_window` 주의.** 전사(대화 기록)가 이 값에 맞춰 잘립니다.
> 실제보다 크게 잡으면 잘리지 않은 채 나가 엔드포인트가 400 을 돌려줍니다.

### 상속 규칙

- 에이전트가 값을 비워 두거나(`${VAR}` 가 빈 문자열로 풀린 경우) 아예 적지 않으면 `llm` 에서 상속합니다
- 별칭 그룹 중 하나만 적어도 그룹 전체를 덮어씁니다: `api_base`/`api_url`/`base_url`, `provider`/`custom_llm_provider`

---

## `llm.sequential_thinking` — 단계적 사고

```json
"sequential_thinking": {
  "enabled": true,
  "mode": "prompt",
  "max_steps": 5,
  "show_steps": true
}
```

| mode | 동작 | 적용 대상 |
| :--- | :--- | :--- |
| `prompt` | 단계적 사고 프로토콜을 시스템 프롬프트에 주입 | 모든 모델 (로컬 포함) |
| `native` | `reasoning_effort` / `thinking` 파라미터를 실제 요청에 전달 | 추론 지원 모델 |
| `mcp` | sequential-thinking MCP 서버 도구를 강제 사용 | 해당 서버 활성화 필요 |

`show_steps: false` 면 최종 결론만 피드에 노출하고 사고 과정은 접어둡니다.
`native` 모드 추가 항목은 `reasoning_effort`(`minimal`|`low`|`medium`|`high`)와
`thinking_budget_tokens` 입니다.

에이전트의 `sequential_thinking` 은 전역 값과 **키 단위로 병합**되므로, 바꾸고
싶은 항목만 적으면 됩니다.

---

## `mcp_servers` — 도구 서버

```json
"filesystem": {
  "command": "${NODE_BIN:-node}",
  "args": [
    "${MCP_NODE_HOME:-./mcp_node}/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js",
    "${WORKSPACE_DIR:-./workspace}"
  ],
  "enabled": true
}
```

| 키 | 타입 | 기본값 | 설명 |
| :--- | :--- | :--- | :--- |
| `command` | str | 필수 | 실행 명령 (`node`, `python` 등) |
| `args` | list[str] | `[]` | 인자 |
| `env` | dict[str,str] | `{}` | 자식 프로세스 환경변수 |
| `enabled` | bool | `true` | `false` 면 기동하지 않습니다 |

기본 구성에 들어 있는 서버는 [MCP 호스트](../03-core/04-mcp-host.md#기본-구성-서버)를 보세요.

---

## `agents` — 에이전트 정의

```json
"architect": {
  "name": "System Architect",
  "role": "High-Level Architecture & Tech Stack",
  "model": "${ARCHITECT_MODEL:-${LLM_MODEL:-anthropic/claude-3-5-sonnet-20241022}}",
  "api_base": "${ARCHITECT_API_BASE}",
  "api_key": "${ANTHROPIC_API_KEY}",
  "temperature": 0.5,
  "debate_priority": 20,
  "debate_stance": "proponent",
  "allowed_mcp_servers": ["filesystem", "memory", "fetch"],
  "system_prompt": [
    "당신은 수석 소프트웨어 아키텍트입니다.",
    "시스템 아키텍처 설계, 모듈 분리, 기술 스택 선정을 전담합니다."
  ]
}
```

| 키 | 타입 | 기본값 | 설명 |
| :--- | :--- | :--- | :--- |
| `name` | str | 필수 | 화면에 뜨는 이름 |
| `role` | str | 필수 | 역할 |
| `enabled` | bool | `true` | `false` 면 풀에 등록되지 않습니다 |
| `allowed_mcp_servers` | list[str] | `[]` | 이 에이전트가 호출할 수 있는 MCP 서버 |
| `debate_priority` | int | `100` | 라운드 안의 발언 순서. 낮을수록 먼저. 같으면 파일 순서 |
| `debate_stance` | str | `neutral` | `proponent` / `critic` / `neutral`. 디베이트 전략에서만 사용 |
| `system_prompt` | str \| list[str] | `""` | 기본 페르소나 |
| 그 외 LLM 항목 | – | `llm` 상속 | `model`, `api_base`, `temperature` … |

**`orchestrator` 키는 필수입니다.** 없으면 설정 검증에서 거부되고, 끄거나 지울
수도 없습니다 — 토론 진행과 최종 합성을 맡기 때문입니다.

---

## 환경변수 치환

모든 문자열 값에 적용됩니다.

| 문법 | 동작 |
| :--- | :--- |
| `${VAR}` | 없으면 빈 문자열 → "미설정" 으로 간주되어 상속 |
| `${VAR:-기본값}` | 없으면 기본값 |
| `${A:-${B:-기본값}}` | 중첩 가능 |

특별 취급되는 변수:

| 변수 | 용도 |
| :--- | :--- |
| `PYTHON_BIN` | 파이썬 MCP 서버 실행기. 미지정 시 **앱과 같은 인터프리터**(`sys.executable`)로 자동 설정 |
| `NODE_BIN` | Node 실행기 (기본 `node`) |
| `MCP_NODE_HOME` | Node MCP 서버 위치 (기본 `./mcp_node`) |
| `MCP_SANDBOX_HOME` | 샌드박스 위치 (기본 `./mcp_sandbox`) |
| `WORKSPACE_DIR` | 공용 작업 공간. **기동 시 절대 경로로 정규화**됩니다 |

`WORKSPACE_DIR` 을 절대 경로로 고정하는 이유: filesystem(node)과 sandbox(python)는
서로 다른 프로세스이고 각자의 cwd 로 상대 경로를 풉니다. 그대로 두면 "같은
`./workspace` 를 줬는데 두 서버가 다른 폴더를 본다" 가 됩니다.

---

## 엔드포인트 설정 예시

### 사내 OpenAI 호환 게이트웨이

```json
"llm": {
  "model": "openai/qwen2.5-coder-32b",
  "api_base": "https://llm-gateway.mycorp.com/v1",
  "api_key": "${CORP_LLM_TOKEN}",
  "extra_headers": { "X-Team": "platform" }
}
```

### Ollama (API 키 불필요)

```json
"model": "ollama_chat/qwen2.5-coder:14b",
"api_base": "http://localhost:11434"
```

### LM Studio / vLLM

```json
"model": "openai/local-model",
"api_base": "http://localhost:1234/v1",
"api_key": "lm-studio"
```

### Azure OpenAI

```json
"model": "azure/my-gpt4o-deployment",
"api_base": "https://my-resource.openai.azure.com",
"api_version": "2024-10-21",
"api_key": "${AZURE_OPENAI_API_KEY}"
```

---

## 호출 모드 판정

에이전트가 실제 LLM 을 부르는지(`is_live`)는 이렇게 정해집니다.

1. `api_base` 가 있으면 → live
2. `api_key` 가 있으면 → live
3. 모델이 `ollama/`, `ollama_chat/`, `lm_studio/` 로 시작하면 → live
4. 그 외 → **unconfigured**. 발언 차례에 실패하고 기록에 남습니다

---

## 편집 시 주의

- 화면(로스터 패널)에서 고친 값도 이 파일에 기록됩니다. 앱이 도는 중에 편집기로
  직접 고쳤다면 **conf.json 다시 읽기** 버튼을 누르세요
- 진행 중인 토론이 있으면 화면에서의 편집이 잠깁니다
- 문법 오류는 **줄 번호와 칸**이 찍힌 메시지로 보고됩니다
- 첫 화면 편집 시 템플릿의 빈 줄이 정규화되어 사라집니다 (값과 설명은 그대로)

---

## 관련 문서

- [설정 레이어](../03-core/01-config-layer.md) — 로더/기록기 구현 원리
- [로스터 편집](../04-workflows/03-roster-editing.md) — 화면에서 설정 고치기
- [LLM 통합](../03-core/03-llm-integration.md) — 이 값들이 실제 호출에 쓰이는 방식
- [MCP 호스트](../03-core/04-mcp-host.md) — MCP 서버 구성

---

> 다음 섹션: [핵심 기술 개관](../03-core/README.md)
