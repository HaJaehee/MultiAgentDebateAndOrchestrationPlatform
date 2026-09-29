# 설치와 첫 실행

> 상위: [시작하기](README.md) · 다음: [conf.json 설정](02-configuration.md)

---

## 사전 요구사항

| 항목 | 필요 여부 | 비고 |
| :--- | :--- | :--- |
| Python 3.11+ | **필수** | `tomllib` 내장, 비동기 TaskGroup 및 예외 그룹(ExceptionGroup) 지원 |
| Node.js 18+ | 선택 | 공식 Node MCP 도구 서버 3종을 구동할 때만 요구되며, 초기 설치 시점에만 필요합니다. |
| Git | 권장 | Git MCP 서버 구동 및 `workspace` 디렉터리 초기화에 필요합니다. |
| 인터넷 연결 | 최초 1회 | 파이썬 의존성 패키지 및 MCP 도구 서버 다운로드에 필요합니다. |

---

## 1. 파이썬 의존성

```bash
pip install -r requirements.txt
```

파이썬 가상환경(`venv`) 사용을 강력히 권장합니다. MCP 도구 서버 중 파이썬 기반으로 동작하는 프로세스들(`git`, `sandbox`, `fetch`)이 **메인 애플리케이션과 동일한 파이썬 인터프리터**를 통해 서브프로세스로 기동되기 때문입니다. `app/config.py`가 내부적으로 `PYTHON_BIN` 환경변수를 `sys.executable` 경로로 자동 바인딩하므로, 가상환경이 활성화된 상태에서 애플리케이션을 구동하면 모든 파이썬 MCP 도구 서버 역시 해당 가상환경에 설치된 패키지를 그대로 활용하게 됩니다.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
```

---

## 2. MCP 서버 준비

```bash
python setup_mcp.py
```

스크립트의 주요 수행 작업:

| 대상 | 수행 내용 |
| :--- | :--- |
| `./workspace` | 대상 디렉터리를 생성하고 **Git 저장소로 초기화(`git init`)**합니다. Git MCP 서버는 대상 경로가 유효한 Git 저장소가 아닐 경우 기동에 실패합니다. |
| `./mcp_node` | 공식 Node.js MCP 도구 서버 3종(filesystem / memory / sequential-thinking)을 로컬에 설치합니다. |
| `./mcp_node/memory-scoped.mjs` | MADO 아키텍처에 맞게 포크된 세션 격리 메모리 서버 실행 사본을 배치합니다 (대화 세션별 독립 지식 그래프 유지). |
| `./mcp_sandbox` | 안전한 코드 실행을 지원하는 [AirgappedPySandbox](https://github.com/HaJaehee/AirgappedPySandbox) 저장소를 체크아웃합니다. |

선택 옵션:

```bash
python setup_mcp.py --skip-node      # Node 서버 건너뛰기
python setup_mcp.py --skip-sandbox   # 코드 실행 샌드박스 건너뛰기
```

설치에서 제외한 서버는 `conf.json` 설정 파일에서 반드시 비활성화해 주시기 바랍니다. 활성화된 상태로 방치할 경우 애플리케이션 기동 시마다 연결 실패 경고 로그가 누적됩니다.

```json
"sandbox": { "command": "...", "args": ["..."], "enabled": false }
```

> `npx` 명령어는 대상 패키지가 로컬에 존재하지 않을 경우 외부 npm 공식 레지스트리에 무조건 접속을 시도하므로, 인터넷이 차단된 오프라인 폐쇄망에서는 정상 동작하지 않습니다. 따라서 `conf.json` 설정 파일은 진입점 번들 파일(`dist/index.js`)을 로컬 `node` 바이너리로 직접 구동하도록 선언되어 있습니다.

---

## 3. 설정 파일

```bash
cp conf.example.json conf.json
```

`conf.json` 파일은 버전 관리 제외(`.gitignore`) 대상입니다. 세부 설정 구조는 [conf.json 설정](02-configuration.md) 문서를 참고하시기 바랍니다.

---

## 4. 환경 변수

```bash
cp .env.example .env
```

`conf.json` 파일 내의 모든 문자열 설정값은 `${VAR}` 및 `${VAR:-기본값}` 환경변수 동적 치환을 완벽히 지원합니다. 따라서 실제 엔드포인트 URL과 API 키는 로컬 `.env` 파일에 안전하게 보관하고, 공용 설정 파일은 원형 그대로 팀원들과 공유하는 방식이 표준 사용법입니다.

```dotenv
# 전역 LLM (모든 에이전트가 상속)
LLM_API_BASE=http://localhost:1234/v1
LLM_MODEL=openai/qwen2.5-coder-32b
LLM_API_KEY=

# 에이전트별로 다른 모델을 쓰고 싶다면
ORCHESTRATOR_MODEL=openai/gpt-4o
ARCHITECT_MODEL=anthropic/claude-3-5-sonnet-20241022
CODER_MODEL=openai/gpt-4o
CRITIC_MODEL=google/gemini-1.5-pro

# 서버 기동 (conf.json 의 app 이 참조)
APP_HOST=127.0.0.1
APP_PORT=8000
```

---

## 5. 실행

```bash
python -m app.main
```

애플리케이션 기동 콘솔 로그가 순차적으로 출력됩니다.

```text
Loaded configuration for host=127.0.0.1:8000, db=sqlite+aiosqlite:///./multiagent.db
Web UI: http://127.0.0.1:8000
SQLite database tables initialized.
MCP session established for 'filesystem'
Discovered 14 tools from MCP server 'filesystem'
...
MCPManager initialized. Connected: ['filesystem', 'memory', 'git', 'sandbox'] | Total registered tools: 39
Agent 'orchestrator' -> model=openai/gpt-4o, endpoint=http://localhost:1234/v1, sequential_thinking=on:prompt
AgentPool loaded 4 agents: ['orchestrator', 'architect', 'coder', 'critic']
Application startup complete.
```

만약 `endpoint=no endpoint configured`라는 안내 문구가 표시된다면 해당 에이전트는 API 호출이 불가능한 상태입니다. 로컬 `.env` 파일의 설정을 다시 확인해 주시기 바랍니다.

### 실행 옵션

| 옵션 | 설명 |
| :--- | :--- |
| `--host HOST` | 바인딩 호스트 네트워크 주소입니다 (`conf.json`과 `.env` 설정을 덮어씁니다). |
| `--port PORT` | 바인딩 포트 번호입니다. |
| `--config PATH` | 적용할 설정 파일 경로입니다 (기본값: `conf.json`). |
| `--reload` / `--no-reload` | 코드 변경 감지 자동 재시작 모드입니다 (기본값: `conf.json`의 `app.debug`). |

설정 파라미터의 적용 우선순위는 **명령행 인자(CLI) > 환경변수(`.env`) > `conf.json` 기본값** 순으로 엄격히 결정됩니다.

```bash
python -m app.main --host 0.0.0.0 --port 9000 --config custom_conf.json
```

---

## 6. 확인

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/agents
curl http://127.0.0.1:8000/api/mcp
```

`/api/agents` 응답의 각 에이전트 항목에 `"mode": "live"` 속성이 명시되어 있다면 실제 언어 모델 호출이 가능한 정상 상태임을 의미합니다. 반면 `"unconfigured"`로 표기된다면 엔드포인트 URL 또는 API 자격 증명이 누락된 상태입니다. → [HTTP API](../05-reference/01-http-api.md)

---

## 테스트 실행

```bash
pytest -q
```

세부 테스트 수행 방법은 [테스트](../05-reference/03-testing.md) 문서를 참고하시기 바랍니다.

---

## 문제 해결

| 장애 증상 | 원인 분석 및 해결 조치 |
| :--- | :--- |
| `endpoint=no endpoint configured` 경고 출력 | `.env` 파일에 `LLM_API_BASE` 또는 `LLM_API_KEY` 설정이 누락되어 있습니다. 환경변수 값을 확인하십시오. |
| `Cannot find package '@modelcontextprotocol/sdk'` 오류 | `python setup_mcp.py` 스크립트가 실행되지 않았습니다. 스크립트를 실행하여 Node 도구 서버를 설치하십시오. |
| `can't open file '.../mcp_sandbox/server.py'` 오류 | 파이썬 샌드박스 서버가 설치되지 않았습니다. `setup_mcp.py`를 실행하거나 `conf.json`에서 `"enabled": false`로 비활성화하십시오. |
| Git MCP 도구 서버 기동 실패 | `workspace` 디렉터리가 Git 저장소가 아닙니다. `git init workspace` 명령을 수행하십시오. |
| 설정 파일 문법 파싱 오류 | 콘솔 오류 메시지에 **행 번호와 열 번호**가 명확히 보고됩니다. 해당 위치의 JSON 문법(쉼표, 따옴표 등)을 점검하십시오. |
| 다른 세션이 진행 중이라며 세션 시작이 거부됨 | 서로 다른 작업 공간을 사용하는 다중 토론 세션의 동시 실행 슬롯이 한도에 도달했습니다. [아키텍처](../01-overview/02-architecture.md#동시성-모델)를 참조하십시오. |

---

> 다음: [conf.json 설정](02-configuration.md)
