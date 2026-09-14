# 🤖 MADO — Multi-Agent Debate & Orchestration Platform

`v0.8.0` · `LGPL-3.0-or-later` · `Python 3.11+`

> **MCP 도구를 활용하는 반응형 멀티 에이전트 협업 & 토론 웹 애플리케이션**  
> Dynamic Agent Profiling via `conf.json`, MCP Tool Integration, Multi-Model LLM Abstraction (LiteLLM), StateGraph Orchestration, and NiceGUI + FastAPI Reactive Web Interface.

```
Author: Ha, Jaehee, Email: lovesm135@naver.com, Version: v0.8.0
```

같은 내용을 웹 UI 우측 상단의 **ⓘ** 버튼으로도 볼 수 있습니다.

---

## 🌟 주요 특징 (Key Features)

1. **`conf.json` 기반 동적 에이전트 프로파일링**:
   - 시스템 기동 시 `conf.json` 설정 파일로부터 에이전트 풀(Agent Pool)과 MCP 서버를 동적으로 등록.
   - `${OPENAI_API_KEY}`, `${ANTHROPIC_API_KEY}` 등 환경 변수 동적 치환 지원.
2. **Model Context Protocol (MCP) 내장 호스트 & 도구 연동**:
   - Filesystem · Memory(지식 그래프) · Git · Python 코드 실행 샌드박스 MCP 서버와 JSON-RPC stdio 통신.
   - 공식 레퍼런스 서버(Node/Python)와 자체 샌드박스를 **번들로 동봉**하여 폐쇄망에서도 도구 사용 가능.
   - 도구 검색 및 Function Calling 스키마 자동 변환, 실행 결과(Observation) 피드백.
   - **도구 호출 예산 안전가드**: 남은 호출 횟수를 에이전트에게 미리 알리고(한계에 가까울수록 촘촘하게), 상한에 닿으면 발언을 버리는 대신 **유저에게 상한 확장을 묻거나 즉시 마무리하도록** 합니다.
   - **컨텍스트 창 포화 안전가드**: 남은 여유를 띠 단위로 미리 알리고, 메모리 MCP 가 있으면 **잘리기 전에 지식 그래프로 옮겨 두게** 합니다. 창을 넘기면 조용히 버리지 않고 유저에게 상향을 묻습니다.
3. **다양한 LLM 프로바이더 추상화 (LiteLLM)**:
   - OpenAI (`gpt-4o`), Anthropic (`claude-3-5-sonnet`), Google (`gemini-1.5-pro`), Ollama 등 통합 지원.
   - `llm` 전역 설정에서 **API URL(`api_base`), 모델 명, API 버전, provider, timeout/재시도, 커스텀 헤더**를 지정하고 모든 에이전트가 상속.
   - 사내 OpenAI 호환 게이트웨이 · vLLM · LM Studio · Ollama 등 **API 키 없는 로컬 엔드포인트도 그대로 사용 가능**.
   - **Sequential Thinking(단계적 사고)** 을 `prompt` / `native` / `mcp` 3가지 모드로 에이전트별 설정.
   - 엔드포인트에 닿지 못하면 **대체 답변을 지어내지 않고** 해당 발언 자리에 연결 실패 사실을 그대로 기록합니다.
4. **토론은 브라우저와 분리되어 백그라운드에서 진행**:
   - 페이지를 새로고침하거나 페르소나 화면에 다녀와도 진행 중인 토론이 끊기지 않습니다.
   - 돌아오면 그동안 오간 발언과 진행률이 이어서 표시되고, 생성 중이던 발언은 이어서 스트리밍됩니다.
   - 사이드바의 세션 목록과 페르소나 화면에 "진행 중" 표시가 뜹니다.

5. **세션별 페르소나 & 시스템 프롬프트 (첫 대화 시 고정)**:
   - `/personas/{session_id}` 편집 페이지에서 에이전트의 이름·역할·시스템 프롬프트를 세션마다 다르게 지정.
   - 첫 유저 메시지가 기록되는 순간 전 에이전트의 페르소나가 DB 에 스냅샷되고 잠깁니다.
   - 세션을 나중에 다시 열면 그때 저장된 페르소나로 이어서 토론합니다 (`conf.json` 이 바뀌었어도 유지).
   - **카드 색과 아이콘**도 여기서 고릅니다. 아이콘은 머티리얼 아이콘을 고르거나 **그림을 올릴 수 있고**, 올린 그림은 `data/agent_icons/` 로 복사되어 `conf.json` 에는 그 경로만 적힙니다. 그림을 찾지 못하면 기본 아이콘으로 안전하게 물러섭니다.
6. **멀티 에이전트 토론 & 상태 머신 오케스트레이션**:
   - **Master Orchestrator**: 목표 분해, 발언자 선정, 토론 중재, 합의 검증 및 최종 산출물 합성.
   - **4가지 토론 전략**: 순차 토론 (`sequential_debate`), 디베이트 (`adversarial_debate`), 오케스트레이터 지명 (`orchestrator_led`), 병렬 지시 (`parallel_dispatch`).
   - 발언 순서와 진영은 에이전트가 들고 다닙니다 (`debate_priority` · `debate_stance`). 로스터에서 카드를 끌어 순서를 바꿉니다.
   - 무한 루프 방지를 위한 `max_rounds` 및 `is_consensus_reached` 종료 보장.
7. **반응형 모던 Web GUI (FastAPI + NiceGUI)**:
   - **좌측 사이드바**: 세션 히스토리, 신규 생성(`+ New Chat`), 이름 변경, 삭제, 그리고 대화 전체를 마크다운 파일로 저장(💾).
   - **상단 제어 패널**: 에이전트 온/오프 토글, 라운드 제한 슬라이더, 전략 선택, 동시 실행 상한(병렬 지시 전략에서만), 세션별 커스텀 지침, 그리고 앱 재기동 없이 `conf.json` 을 다시 읽어 에이전트 목록을 갱신하는 버튼.
   - **메인 토론 피드**: 에이전트별 색상/아바타 구분 대화창, 접이식(Accordion) MCP 도구 호출 로그. 색을 직접 정한 에이전트는 카드 테두리까지 그 색으로 칠해집니다.
   - **입력창 @언급**: `@` 를 치면 작업 공간의 파일·폴더와 이번 토론의 전문가 목록이 뜹니다. 파일은 **내용이 아니라 경로만** 전달되고(PDF·오피스 문서 포함, 읽기는 해당 MCP 의 몫), 전문가는 지목으로 전달됩니다. 입력창 왼쪽 버튼으로 **작업 공간에 파일을 업로드**하면 바로 목록에 나옵니다.
   - **우측 산출물 뷰어**: 최종 종합 보고서(Markdown), 소스코드(Code), Mermaid 아키텍처 다이어그램 탭 및 원클릭 복사/다운로드.
8. **SQLite 영구 저장소 (SQLAlchemy Async)**:
   - 세션, 메시지, 도구 호출 기록, 최종 아티팩트 영구 보존.

---

## 🏗️ 시스템 아키텍처 (Architecture)

```mermaid
graph TD
    User([Web User / Browser]) <--> UI[NiceGUI + FastAPI Reactive UI]
    UI <--> Engine[Multi-Agent Orchestrator Engine]
    Engine <--> DB[(SQLite Database / SQLAlchemy Async)]
    Engine <--> Pool[Agent Pool Manager]
    
    Pool --> Orch[Master Orchestrator]
    Pool --> Arch[System Architect]
    Pool --> Coder[Senior Python Engineer]
    Pool --> Critic[Security & Quality Critic]
    
    Orch <--> LLM[LiteLLM Provider Layer]
    Arch <--> LLM
    Coder <--> LLM
    Critic <--> LLM
    
    Orch <--> MCP[MCP Client & Host Manager]
    Coder <--> MCP
    MCP <--> MCPServers[External MCP Servers via stdio]
```

---

## 📁 프로젝트 구조 (Directory Structure)

```
MultiAgentOrchestrator/
├── conf.example.json         # 설정 템플릿 (저장소에 커밋되는 원본)
├── setup_mcp.py              # 개발 PC용 MCP 서버 일괄 설치
├── package_offline.py        # 폐쇄망 배포 번들 패키징 (런타임 + MCP 서버 동봉)
├── package_source.py         # 소스·설정만 패키징 (런타임 제외, 갱신 반입용)
├── conf.json                 # 실제 시스템 설정 파일 (로컬 전용, .gitignore 대상)
├── .env.example              # 환경 변수 템플릿
├── requirements.txt          # 파이썬 의존성 패키지
├── README.md                 # 프로젝트 문서
├── LICENSE.md                # 라이선스 (LGPL-3.0 전문 + 제3자 고지)
├── docs/                     # 사용 설명서 (한국어)
│   ├── user_manual/          # 마크다운 원본 (기술 스택 · 핵심 기술 · 워크플로우)
│   ├── user_manual_html/     # HTML 렌더링 산출물 (.gitignore 대상)
│   └── render_user_manual.py # 마크다운 → 정적 HTML 렌더러 (표준 라이브러리만)
├── mcp_node/                 # 공식 Node MCP 서버 (npm install 로 생성, .gitignore 대상)
├── mcp_sandbox/              # AirgappedPySandbox (git clone 으로 생성, .gitignore 대상)
├── workspace/                # 에이전트 공용 작업 공간 (.gitignore 대상)
├── data/                     # 화면에서 올린 파일 (에이전트 아이콘 등, .gitignore 대상)
│   └── agent_icons/          # 업로드한 에이전트 아이콘 이미지
├── app/
│   ├── main.py               # FastAPI + NiceGUI 실행 엔트리포인트
│   ├── about.py              # 앱 이름·버전·저작자 (단일 출처)
│   ├── config.py             # JSON 로더/기록기, 환경변수 치환 및 Pydantic 검증
│   ├── workspace_files.py    # 작업 공간 파일 목록·@언급 해석·업로드 저장 (경로 안전장치)
│   ├── database/             # SQLite & SQLAlchemy 비동기 ORM
│   │   ├── models.py         # Session, Message, ToolCallRecord, Artifact, SessionAgent 모델
│   │   └── session.py        # Async Engine 및 세션 관리
│   ├── mcp/                  # MCP Host & Tool Integration
│   │   ├── client.py         # Stdio MCP Client 프로세스 관리자
│   │   └── manager.py        # 도구 검색 및 Function Calling 디스패치
│   ├── agents/               # 에이전트 및 LLM 계층
│   │   ├── base.py           # Agent 모델, 카드 색·아이콘 해석 및 폴백
│   │   ├── personas.py       # 세션별 페르소나 해석·저장·고정
│   │   ├── llm.py            # LiteLLM 호출기, Tool 루프, LLMUnavailableError
│   │   └── pool.py           # 동적 에이전트 풀 레지스트리
│   ├── orchestration/        # 멀티 에이전트 토론 상태 머신
│   │   ├── state.py          # DebateState, DebateMessage, ArtifactItem
│   │   ├── strategies.py     # 순차 토론, 디베이트, 오케스트레이터 지명, 병렬 지시 전략
│   │   ├── engine.py         # 오케스트레이션 엔진 & 산출물 합성기
│   │   └── runner.py         # 세션별 백그라운드 토론 태스크 & 재접속 스냅샷
│   └── ui/                   # NiceGUI 반응형 웹 UI
│       ├── app.py            # UI 페이지 레이아웃 및 리액티브 바인딩
│       ├── personas_page.py  # /personas/{session_id} 페르소나 편집 페이지
│       ├── theme.py          # Quasar CSS 스타일 & 컬러 팔레트
│       ├── mention_input.py  # 입력창 @언급 창 (브라우저 스크립트)
│       └── components/       # UI 컴포넌트
│           ├── sidebar.py    # 세션 히스토리 사이드바
│           ├── roster.py     # 에이전트 로스터 및 토론 제어판
│           ├── chat_feed.py  # 대화 타임라인 & MCP 도구 아코디언
│           ├── agent_appearance.py # 카드 색·아이콘 편집기 (추가 다이얼로그 / 페르소나 편집 공용)
│           └── artifact_viewer.py # 탭형 산출물 뷰어 (Code, Markdown, Mermaid)
└── tests/                    # 자동화 테스트 스위트
    ├── test_config.py
    ├── test_personas.py       # 페르소나 편집 → 고정 → 재개 수명주기
    ├── test_agent_appearance.py # 카드 색·아이콘 (업로드 → conf.json → 화면 → 스냅샷, 폴백)
    ├── test_agent_admin.py    # 에이전트 추가/비활성화/삭제 → conf.json 편집과 잠금 규칙
    ├── test_session_snapshot.py # 시작한 대화의 구성 스냅샷 (삭제·키 교체에도 자기완결)
    ├── test_speaker_selection.py # 오케스트레이터 지명 전략 (지명·해석·실패 시 물러서기)
    ├── test_parallel_dispatch.py # 병렬 지시 전략 (동시 실행·과업 분배·취합·상한)
    ├── test_abort_turn.py       # 긴급 종료 (그 턴만 지우기, 시작 전으로 되돌리기)
    ├── test_workspace_mentions.py # @언급 (경로만 전달·경로 안전·코드 블록 제외) & 업로드
    ├── test_remote_mcp.py       # 원격(HTTP) MCP 서버 (설정 규칙·전송 방식·토큰 보관)
    ├── test_llm_settings.py   # llm 상속, 엔드포인트/단계적 사고 설정
    ├── test_db.py
    ├── test_mcp.py
    └── test_orchestrator.py
```

---

## 🚀 시작하기 (Getting Started)

### 1. 의존성 설치
```bash
pip install -r requirements.txt
```

### 1-1. MCP 서버 준비
`conf.json` 이 기본으로 켜 두는 MCP 서버를 한 번에 준비합니다 (인터넷 필요, 최초 1회).
```bash
python setup_mcp.py
```
- `./workspace` 생성 및 **git 저장소 초기화** — git MCP 서버는 유효한 git 저장소가 아니면 기동에 실패합니다
- `./mcp_node` 에 공식 Node MCP 서버 설치 (filesystem / memory / sequential-thinking, Node.js 필요)
  및 포크한 memory 서버(대화별 지식 그래프) 사본 배치
- `./mcp_sandbox` 에 [AirgappedPySandbox](https://github.com/HaJaehee/AirgappedPySandbox) 체크아웃

Node 를 쓰지 않거나 샌드박스가 필요 없으면 `--skip-node` / `--skip-sandbox` 를 붙이고,
해당 서버는 `conf.json` 에서 `"enabled": false` 로 꺼두세요.

### 2. 설정 파일 준비
`conf.json` 은 로컬 전용 파일이라 저장소에 포함되지 않습니다. 템플릿을 복사해서 시작하세요.
```bash
cp conf.example.json conf.json
```

### 3. 환경 변수 설정 (선택사항)
`.env.example`을 복사하여 `.env`를 생성하고 엔드포인트와 키를 입력합니다.
```bash
cp .env.example .env
```
```dotenv
LLM_API_BASE=http://localhost:1234/v1     # 사내 게이트웨이 / vLLM / LM Studio / Ollama
LLM_MODEL=openai/qwen2.5-coder-32b        # 모델 명
LLM_API_KEY=                              # 키가 필요 없는 서버라면 비워두세요
```
*(엔드포인트가 없으면 에이전트는 발언하지 못하고, 그 자리에 "연결 끊김" 이 기록됩니다. 대체 답변을 지어내지 않습니다.)*

### 4. 애플리케이션 실행
```bash
python -m app.main
```
접속 주소는 기동 로그의 `Web UI: http://...` 줄에 표시됩니다. 주소는 `conf.json` 의
`app.host` / `app.port` 를 따르며, 이 값은 `.env` 의 `APP_HOST` / `APP_PORT` 로도 덮어쓸 수 있습니다.
실행 스크립트에 주소를 하드코딩하지 않으므로 포트를 바꾸려면 한 곳만 고치면 됩니다.

```json
"app": {
  "host": "${APP_HOST:-127.0.0.1}",
  "port": "${APP_PORT:-8000}"
}
```

---

## 🧪 테스트 실행 (Running Tests)

```bash
pytest -v tests/
```

---

## ℹ️ 정보 (About)

| 항목 | 값 |
|------|-----|
| Author | Ha, Jaehee |
| Email | lovesm135@naver.com |
| Version | **v0.8.0** |
| License | LGPL-3.0-or-later ([LICENSE.md](LICENSE.md)) |

버전 문자열의 정본은 [`app/about.py`](app/about.py) 한 곳입니다. FastAPI 메타데이터,
`GET /api/health`, 웹 UI 헤더 뱃지, 정보 모달이 모두 이 값을 읽습니다 — 올릴 때
한 곳만 고치면 됩니다.

```bash
curl -s localhost:8000/api/health | python -m json.tool
```

```json
{
  "status": "healthy",
  "version": "v0.8.0",
  "author": { "name": "Ha, Jaehee", "email": "lovesm135@naver.com" }
}
```

---

## 📚 문서 (Documentation)

| 위치 | 언어 | 내용 |
|------|------|------|
| [`docs/user_manual/`](docs/user_manual/README.md) | 한국어 | **사용 설명서** — 기술 스택, 핵심 기술(모듈별 원리), 워크플로우, 레퍼런스 |
| [`wiki/`](wiki/README.md) | 영어 | 기술 위키 — 설계 배경과 세부 구현 |
| `CLAUDE.md` | 한국어 | 프로젝트 명세서 |

사용 설명서를 HTML 로 보려면:

```bash
python docs/render_user_manual.py
```

`docs/user_manual_html/index.html` 이 생성됩니다. 사이드바에 문서 트리가 붙고,
문서 사이의 링크가 그대로 이어집니다. 렌더러는 **표준 라이브러리만** 쓰고
산출물에 외부 요청이 하나도 없어, 폐쇄망으로 폴더째 옮겨도 그대로 열립니다.

---

## ⚙️ `conf.json` 커스텀 가이드

### 설정 파일의 문법

설정은 JSON 입니다. 표준 라이브러리 `json` 하나로 읽고 쓰므로 화면에서 고친 값이 그대로 파일에
반영되고, 문법 오류는 **줄 번호와 함께** 보고됩니다. JSON 에 없는 두 가지는 아래 규칙으로 채웁니다.

| 필요한 것 | 규칙 |
|---|---|
| 주석 | 키를 `//` 로 시작하면 설명으로 보고 읽을 때 걷어냅니다. 값은 문자열 또는 문자열 배열(여러 줄) |
| 여러 줄 글 | `system_prompt` / `prompt_template` 은 **문자열 배열**로 적을 수 있고, 읽을 때 줄바꿈으로 이어 붙입니다 |

```json
"// filesystem": [
  "공용 작업 공간 파일 I/O (공식 서버, 도구 14종).",
  "지정한 디렉터리 밖 경로는 서버가 자체적으로 차단합니다."
],
"filesystem": { "command": "${NODE_BIN:-node}", "args": ["..."], "enabled": true }
```

설명은 데이터의 일부라 화면에서 에이전트를 추가·삭제해도 그대로 남습니다.

### 전역 LLM 설정 `llm`

`llm` 의 값은 각 에이전트가 같은 항목을 직접 지정하지 않는 한 **모든 에이전트에 상속**됩니다.
따라서 사내 게이트웨이나 로컬 LLM 서버를 쓸 때는 `llm` 한 곳만 고치면 됩니다.

```json
"llm": {
  "model": "openai/qwen2.5-coder-32b",
  "// api_base": "LLM API URL (api_url / base_url 도 동일)",
  "api_base": "https://llm-gateway.mycorp.com/v1",
  "// api_key": "로컬 모델이면 비워두어도 됩니다",
  "api_key": "${CORP_LLM_TOKEN}",
  "// api_version": "Azure OpenAI 전용",
  "api_version": "2024-10-21",
  "// provider": "LiteLLM provider 강제 지정 (선택)",
  "provider": "openai",
  "temperature": 0.4,
  "max_tokens": 4096,
  "// max_context_window": [
    "엔드포인트의 실제 한도로 맞추세요.",
    "전사가 이 값에 맞춰 잘립니다. 실제보다 크게 잡으면 잘리지 않은 채 나가 400 을 받습니다."
  ],
  "max_context_window": 128000,
  "// timeout": "응답 조각 사이를 기다리는 최대 초. 긴 파일 쓰기 도구 호출은 인자를 다 만들 때까지 조용하니 max_tokens ÷ 초당 생성 토큰보다 크게",
  "timeout": 600,
  "// num_retries": "재시도 횟수",
  "num_retries": 2,
  "// drop_params": "엔드포인트가 모르는 파라미터 자동 제거 (로컬 모델 호환성)",
  "drop_params": true,
  "// max_tool_iterations": "한 턴에서 허용할 MCP 도구 루프 횟수 (1~100)",
  "max_tool_iterations": 30,
  "extra_headers": { "X-Org-Id": "${MY_ORG_ID}" },
  "extra_body": { "user": "multiagent-orchestrator" }
}
```

> **호출 모드 판정**: `api_base` 또는 `api_key` 중 하나라도 설정되어 있으면(또는 모델이 `ollama/`, `ollama_chat/`, `lm_studio/` 로 시작하면) 실제 LLM 을 호출합니다.
> 둘 다 없으면 그 에이전트는 발언 차례에 실패하고, 토론 기록에 연결 실패 사실이 남습니다.
> API 키가 필요 없는 로컬 서버는 `api_base` 만 지정하면 그대로 사용할 수 있습니다.

### Sequential Thinking (단계적 사고)

`llm.sequential_thinking` (또는 `agents.<key>.sequential_thinking`) 에 적습니다.

```json
"sequential_thinking": {
  "enabled": true,
  "// mode": "\"prompt\" | \"native\" | \"mcp\"",
  "mode": "prompt",
  "max_steps": 5,
  "// show_steps": "false 면 최종 결론만 피드에 노출",
  "show_steps": true,
  "// reasoning_effort": "native 모드: minimal | low | medium | high",
  "reasoning_effort": "high",
  "// thinking_budget_tokens": "native 모드: 확장 사고 토큰 예산 (Anthropic 계열)",
  "thinking_budget_tokens": 8192,
  "// mcp_server": "mcp 모드에서 사용할 MCP 서버 키",
  "mcp_server": "sequential_thinking",
  "// prompt_template": "프로토콜 문구 직접 작성. {max_steps} 치환자를 쓸 수 있습니다"
}
```

| mode | 동작 | 적용 대상 |
|------|------|-----------|
| `prompt` | 단계적 사고 프로토콜을 시스템 프롬프트에 주입 | 모든 모델 (로컬 포함) |
| `native` | `reasoning_effort` / `thinking` 파라미터를 실제 요청에 전달 | 추론 지원 모델 |
| `mcp` | `@modelcontextprotocol/server-sequential-thinking` 도구를 강제 사용 | MCP 서버 활성화 필요 |

에이전트의 `agents.<key>.sequential_thinking` 은 전역 값과 **키 단위로 병합**되므로,
바꾸고 싶은 항목만 적으면 나머지는 `llm.sequential_thinking` 값을 그대로 사용합니다.

### 에이전트 페르소나 (세션별 오버라이드)

`conf.json` 의 `agents` 는 **서버 전역 기본값**입니다. 세션마다 다른 인격으로 토론시키려고
서버를 재기동할 필요는 없습니다.

로스터 패널의 **페르소나 편집** 버튼 → `/personas/{session_id}` 에서 에이전트별로
**이름 · 역할 · 시스템 프롬프트**를 고칠 수 있습니다. 모델·엔드포인트·도구 권한은 운영 설정이라
`conf.json` 에 남습니다.

| 시점 | 상태 | 동작 |
|------|------|------|
| 세션 생성 ~ 첫 메시지 직전 | 🟢 편집 가능 | 저장분은 이 세션에만 적용. 손대지 않은 에이전트는 `conf.json` 기본값 |
| 첫 유저 메시지 | 🔒 고정 | 그 시점의 유효값이 **전 에이전트**에 대해 `session_agents` 에 기록되고 `personas_locked = true` |
| 세션 재개 | 🔒 고정 유지 | 저장된 값을 그대로 사용. 그 사이 `conf.json` 이 바뀌어도 세션은 잠글 때의 인격을 유지 |

### 시작한 대화는 자기완결적입니다

첫 메시지가 굳히는 것은 인격만이 아닙니다. `AgentConfig` **전체** — 모델 · 엔드포인트 ·
API 키 · 샘플링 값 · 도구 권한 · 단계적 사고 설정 · 카드 색과 아이콘 — 가
`session_agents.config_snapshot` (JSON) 에 함께 기록됩니다. 그래서 대화는 이 순간부터
`conf.json` 에 의존하지 않습니다.

| `conf.json` 에 한 일 | 시작한 대화 | 아직 시작하지 않은 대화 |
|----------------------|-------------|--------------------------|
| 에이전트 추가 | 영향 없음 (꺼진 채로 보이며, 원하면 직접 켤 수 있음) | 켜진 채로 참여 |
| 에이전트 삭제 · 비활성화 | **영향 없음** — 그 에이전트는 굳은 구성으로 계속 발언하고, 로스터에 `이 대화 전용` 뱃지로 표시 | 풀에서 빠짐 |
| 모델 · 엔드포인트 · 키 변경 | 영향 없음 | 즉시 반영 |
| 카드 색 · 아이콘 변경 | 영향 없음 — 지난 발언 카드도 그때의 색 그대로 | 즉시 반영 |
| 도구(`allowed_mcp_servers`) 변경 | 영향 없음 | 즉시 반영 |
| MCP 서버 on/off · 삭제 | **영향 있음** — 서버 프로세스는 앱 전체가 공유합니다 | 영향 있음 |

마지막 줄이 유일한 예외입니다. 스냅샷은 "이 에이전트가 어떤 서버를 부를 수 있는가" 를
담지만, 그 서버가 실제로 떠 있는지는 프로세스 전체의 사정입니다.

**설정 갱신 (탈출구).** 스냅샷이 정본이 되면 게이트웨이 주소가 바뀌거나 API 키가
만료됐을 때 옛 대화가 죽은 엔드포인트를 계속 두드리게 됩니다. 잠긴 세션의 로스터에
나타나는 **설정 갱신** 버튼이 스냅샷을 지금 `conf.json` 값으로 다시 굳힙니다 — 인격은
건드리지 않으므로 기록의 화자는 그대로입니다. `conf.json` 에 더 이상 없는 에이전트는
손대지 않고 남겨 둡니다.

> **API 키가 DB 에 들어갑니다.** 자기완결성의 대가입니다. `multiagent.db` 는 평문
> SQLite 이므로 배포 시 파일 권한을 확인하세요. `GET /api/sessions/{id}/personas` 는
> 이름 · 역할 · 시스템 프롬프트만 돌려주며 스냅샷을 노출하지 않습니다.

`config_snapshot` 이 `NULL` 인 행은 이 컬럼이 생기기 전에 잠긴 대화입니다. 그런 대화는
예전처럼 살아 있는 `conf.json` 을 따릅니다 (기존 DB 는 기동 시 자동 이관됩니다).

토론 도중 인격이 바뀌면 앞선 발언과 뒤의 발언이 서로 다른 화자에서 나오게 되어 기록을 해석할 수
없습니다. 그래서 첫 메시지 이후로는 UI 가 읽기 전용이 되고, 서버 측에서도 저장 요청을 거부합니다
(`PersonasLockedError`). 다른 페르소나로 토론하려면 새 세션을 시작하세요.

"기본값과 다름" 뱃지는 **행의 존재가 아니라 값 비교**로 판정합니다. 세션을 잠글 때 손대지 않은
에이전트까지 전부 스냅샷되므로, 행이 있다는 것만으로는 유저가 수정했는지 알 수 없기 때문입니다.

현재 페르소나와 잠금 여부는 `GET /api/sessions/{session_id}/personas` 로도 확인할 수 있습니다.

### 발언 순서와 진영 — 누가 언제 말하는가

한 턴은 **오케스트레이터 계획 → N 라운드 전문가 토론 → 오케스트레이터 합성** 입니다.
오케스트레이터는 라운드 밖에 섭니다 — 라운드 안에서 말하지 않습니다.

라운드 안의 순서는 두 값이 정합니다. 둘 다 `conf.json` 의 `agents.<key>` 에 있고,
로스터에서 바꿀 수 있으며, 대화가 시작되면 스냅샷으로 함께 굳습니다.

| 값 | 뜻 | 화면에서 |
|----|-----|----------|
| `debate_priority` | 라운드 안의 발언 순서. 낮을수록 먼저. 같으면 `conf.json` 에 적힌 순서 | 카드를 **끌어서** 배치 |
| `debate_stance` | 디베이트 전략에서의 진영: `proponent` · `critic` · `neutral` | 카드의 **⋮ 메뉴** |

로스터에 놓인 카드 순서가 곧 발언 순서입니다. 카드를 끌어 놓으면 `debate_priority` 가
10, 20, 30... 으로 다시 매겨집니다 (사이에 자리를 남겨, 나중에 한 명을 끼워 넣을 때
나머지를 다시 쓰지 않습니다).

| 전략 | 순서 | 발언 지침 |
|------|------|-----------|
| 순차 토론 (`sequential_debate`) | `debate_priority` 순, 전원 | **직전 발언자를 이름으로 지목**해 그 결론을 이어받아 검증하도록 요구 |
| 디베이트 (`adversarial_debate`) | `proponent` ↔ `critic` 번갈아, 각 진영 안에서는 우선순위 순, `neutral` 은 뒤 | 진영에 맞는 지침 (제안·방어 / 반례·근거) |
| 오케스트레이터 지명 (`orchestrator_led`) | 매 라운드 오케스트레이터가 결정 | 지목 사유를 먼저 처리하도록 요구 |
| 병렬 지시 (`parallel_dispatch`) | 매 라운드 오케스트레이터가 과업을 나눠 주고 **동시 실행** | 개별 과업 + 동시에 도는 동료들의 과업 목록 |

> 한때 '자유 토론' 과 '순차 검증' 이 따로 있었습니다. 순서가 키 하드코딩으로 정해지던
> 시절에는 둘의 발언 순서가 달랐지만, 순서가 `debate_priority` 하나로 정리되면서 두
> 전략은 같은 순서로 같은 사람들을 부르게 되었습니다. 이름과 달리 '자유 토론' 도 결국
> 정해진 순서대로 도는 것이었으므로, 하나로 합치고 하는 일 그대로 **순차 토론** 이라
> 부릅니다. `free_debate` · `sequential_review` 로 저장된 대화는 별칭이 받아 그대로
> 이어집니다 (저장 문서에도 지금 도는 전략 이름이 적힙니다).

**오케스트레이터 지명 (`orchestrator_led`).** 매 라운드 오케스트레이터에게 지금까지의
토론과 참여 에이전트 명단을 주고 "이번 라운드에 꼭 필요한 에이전트만" 고르게 합니다.
전원이 매 라운드 한 번씩 말하는 다른 전략과 달리 **지목된 에이전트만** 발언하며, 한
명만 부를 수도 있습니다. 지명 결과와 사유, 그리고 부르지 않은 에이전트는 피드에
기록으로 남습니다.

- 지명 호출은 **도구와 단계적 사고를 끈 사본**으로 나갑니다. JSON 한 줄 받자고 파일을
  읽거나 `Thought 1..N` 을 쓰기 시작하면 안 되기 때문입니다.
- 응답이 JSON 이 아니어도 본문에서 아는 에이전트 키를 등장 순서대로 긁습니다. 형식
  하나 때문에 지명을 포기하면 이 전략은 결국 우선순위 순서와 같아집니다.
- 엔드포인트가 없거나 아는 키를 하나도 못 찾으면 `debate_priority` 순서로 물러섭니다.
  **물러섰다는 사실은 피드에 남습니다** — 조용히 다른 순서로 도는 것이 제일 나쁩니다.

**병렬 지시 (`parallel_dispatch`).** 오케스트레이터가 라운드마다 **서로 겹치지 않는
과업**을 에이전트별로 써서 나눠 주고, 지목된 에이전트들이 **같은 시각에** 각자의 일을
합니다. 한 명이 끝나야 다음이 시작하는 다른 전략과 달리 라운드 시간은 발언 시간의
합이 아니라 가장 느린 한 명의 시간입니다.

- 각 에이전트는 **자기 과업**과 **누가 무엇을 맡았는지의 목록**을 받지만, 같은 라운드
  동료들의 **답**은 볼 수 없습니다. 이 라운드의 프롬프트는 첫 호출이 나가기 전에 전부
  만들어집니다 — 먼저 끝난 동료의 발언이 늦게 시작한 쪽 맥락에 섞이면 병렬이 아닙니다.
- 라운드 끝에 오케스트레이터가 **취합 발언**을 남깁니다 (통합 결론 · 어긋나는 지점과
  판정 · 검증되지 않은 가정 · 다음 라운드 과제). 아무도 서로를 못 봤으므로 이 접합부가
  없으면 라운드는 독백 묶음이 되고, 모순이 최종 합성까지 그대로 실려 갑니다.
- **동시 실행** 숫자(로스터 패널, 기본 3)가 한 번에 띄우는 수를 제한합니다. 상한을 넘는
  지시는 버려지지 않고 순차적으로 밀립니다 — 로컬 단일 엔드포인트(Ollama 등)에 동시
  요청을 다섯 개 던지면 큐에 쌓이거나 메모리가 터지고, 그 실패는 "에이전트가 응답하지
  못했다" 로 나타납니다.
- 분배가 실패하면 **과업 없이 전원을 동시에** 돌리고, 물러섰다는 사실을 피드에 남깁니다.
  개입과 정지는 **라운드 경계에서만** 반영됩니다 — 발언 사이라는 틈이 이 전략에는
  없습니다 (그 시각에 전원이 이미 달리고 있습니다).

> 예전에는 순서가 `{"architect": 0, "coder": 1, "critic": 2}` 처럼 에이전트 키로 박혀
> 있었습니다. 화면에서 에이전트를 만들 수 있게 되면서 그 방식은 무너졌습니다 — 표에
> 없는 키는 전부 같은 우선순위로 묶여 언제나 맨 뒤로 밀렸고, 디베이트에서는 제안자도
> 비판자도 아닌 `others` 로 빠졌습니다. 지금 전략 코드에 남은 유일한 키는
> `orchestrator` 하나이며, 그건 역할이 아니라 이 시스템의 구조입니다.

### 에이전트 구성 편집 (추가 · 진영 · 순서 · 삭제)

로스터 패널에서 `conf.json` 의 `agents` 를 화면에서 직접 고칩니다. 앱을 다시 띄울
필요는 없습니다.

| 조작 | 어디서 | 저장되는 값 |
|------|--------|-------------|
| 에이전트 추가 | 헤더의 **에이전트 추가** 버튼 | `agents` 에 새 `<key>` 항목 |
| 카드 색 · 아이콘 | **에이전트 추가** 다이얼로그 / **페르소나 편집** 화면 | `card_color` / `icon` |
| 발언 순서 | 카드를 **끌어서** 배치 | `debate_priority` |
| 디베이트 진영 | 카드의 **⋮ 메뉴** | `debate_stance` |
| 비활성화 · 삭제 | 카드의 **⋮ 메뉴** | `enabled` / 섹션 제거 |
| 도구 할당 | 카드의 **도구 N** 버튼 | `allowed_mcp_servers` |

페르소나 편집과는 **적용 범위가 다릅니다.** 페르소나(이름·역할·시스템 프롬프트)는 대화별
오버라이드지만, 위의 것들은 배포 설정이라 `conf.json` 이 정본이고 다음 기동에서도 그대로
살아 있습니다.

위 다섯 가지는 **하나의 잠금**을 함께 씁니다. 하나만 열어 두면 "바꿨는데 왜 이 대화에는
반영되지 않지" 가 됩니다.

| 상태 | 편집 |
|------|------|
| 이 대화가 아직 시작되지 않았고 아무 대화도 토론 중이 아님 | 🟢 전부 가능 |
| 이 대화에 이미 첫 메시지를 보냄 | 🔒 잠김 — 이 대화의 에이전트 구성이 고정되었습니다 |
| 어느 대화든 토론이 돌고 있음 | 🔒 잠김 — 에이전트 풀은 프로세스 전체가 공유합니다 |

**추가.** 폼의 LLM 항목(모델 · API URL · API 키 · provider · 온도 · 컨텍스트 창 · 응답 토큰 ·
타임아웃 · 재시도 · 도구 루프 한도 등)은 `.env` 와 `llm` 에서 온 **현재 유효 기본값**으로 미리
채워집니다. 그대로 둔 항목은 `conf.json` 에 **적지 않습니다** — 화면이 보는 값은 이미 환경변수가
풀린 값이라, 되쓰면 해석된 API 키가 파일에 평문으로 박히고 `.env` 를 바꿔도 따라오지 않게 됩니다.
적지 않은 항목은 계속 `llm` 을 상속하므로 `.env` 를 바꾸면 이 에이전트도 함께 따라갑니다.
페르소나(시스템 프롬프트)와 MCP 도구 할당, 단계적 사고 설정도 같은 창에서 지정합니다.

**비활성화 vs 삭제.** 둘 다 그 에이전트를 풀에서 빼지만, 비활성화(`enabled = false`)는 설정과
프롬프트를 파일에 남겨 두어 '꺼둔 에이전트' 칩에서 언제든 되살릴 수 있습니다. 삭제는 섹션과 하위
블록(`agents.<key>.sequential_thinking`)까지 지웁니다. 오케스트레이터는 토론 진행과 최종
합성을 맡으므로 끄거나 지울 수 없습니다.

**이미 시작한 대화는 건드리지 않습니다.** 첫 메시지와 함께 참여 에이전트와 그 구성 전체가
스냅샷으로 굳으므로([시작한 대화는 자기완결적입니다](#시작한-대화는-자기완결적입니다)),
여기서 지운 에이전트도 그 대화에서는 굳은 구성 그대로 계속 발언합니다. 로스터에는 `이 대화 전용`
뱃지가 붙습니다. 반대로 그 뒤에 추가한 에이전트는 잠긴 대화에서 꺼진 채로 보입니다
(`sessions.known_agents` 로 "사용자가 끈 것" 과 "그 뒤에 생긴 것" 을 구분합니다). 정말
합류시키려면 그 대화에서 직접 체크하세요.

`conf.json` 을 편집기로 직접 고쳤다면 **conf.json 다시 읽기** 버튼으로 같은 결과를 얻습니다.
어느 경로든 주석과 `${VAR}` 표기는 보존됩니다 — 줄 단위로 고치기 때문입니다.

### MCP 도구 구성 `mcp_servers`

기본 구성에는 아래 서버가 등록되어 있습니다. 모든 서버는 진입점을 `node` / `python` 으로
**직접** 실행합니다 — `npx` 는 패키지가 로컬에 없으면 npm 레지스트리에 접속하므로 폐쇄망에서
동작하지 않습니다.

| 서버 | 조달 | 도구 | 기본값 | 역할 |
|------|------|----|--------|------|
| `filesystem` | Node (공식) | 14 | 활성 | 공용 작업 공간 파일 I/O. 지정 디렉터리 밖 경로는 서버가 차단 |
| `memory` | Node (공식 서버 포크) | 9 | 활성 | 합의된 사실을 지식 그래프로 축적 (컨텍스트가 잘려도 보존). 그래프는 **대화별로 분리**됩니다 |
| `git` | Python (공식) | 12 | 활성 | 산출물 버전 관리. 라운드별 변화를 diff 로 추적 |
| `sandbox` | Python ([AirgappedPySandbox](https://github.com/HaJaehee/AirgappedPySandbox)) | 5 | 활성 | Python 코드 실제 실행. 에이전트의 주장을 실행으로 판정 |
| `sequential_thinking` | Node (공식) | 1 | 비활성 | `mode = "mcp"` 를 쓸 때만 필요 |
| `fetch` | Python (공식) | 1 | 비활성 | URL 조회. **폐쇄망에서는 켜지 마세요** |

> `@modelcontextprotocol/server-brave-search` 는 공식 레포에서 `servers-archived` 로 이관되어
> 더 이상 유지보수되지 않으므로 기본 구성에서 제외했습니다. 검색이 필요하면 `conf.example.json`
> 하단의 DuckDuckGo / SearXNG 예시를 참고하세요.

기본 도구 배분은 다음과 같습니다. Critic 에게 `sandbox` 를 주는 것이 핵심으로, 코드를 직접
실행해 반박할 수 있어야 검증 역할이 실질적으로 동작합니다.

| 에이전트 | 도구 |
|----------|------|
| `orchestrator` | `filesystem`, `memory` |
| `architect` | `filesystem`, `memory`, `fetch` |
| `coder` | `filesystem`, `sandbox`, `git` |
| `critic` | `filesystem`, `sandbox`, `git`, `memory` |

#### 도구 실행 실패의 처리

MCP 는 두 종류의 실패를 구분합니다.

| 종류 | 전달 방식 | 이 프로젝트의 처리 |
|------|-----------|--------------------|
| 프로토콜 오류 | JSON-RPC error → SDK 예외 | `status = "error"`, 예외 타입과 메시지를 표시 |
| 도구 실행 오류 | 정상 응답의 `isError: true` | `status = "error"`, **서버 메시지를 그대로 전달** |

후자에서 원문을 보존하는 것이 중요합니다. 도구 실행 오류는 성공 응답과 똑같이 LLM
컨텍스트에 주입되므로, 모델이 "경로가 없다" / "권한이 없다" 를 직접 읽어야 파라미터를
고쳐 다시 시도할 수 있습니다. 래핑해서 원문을 가리면 모델은 같은 실패를 반복합니다.

도구 실행 실패는 세션을 끊지 않습니다. 재연결 대상은 스트림이 닫힌 경우뿐입니다.

> 서버가 실패를 `isError` 로 보고하지 않고 자체 페이로드에 담는 경우도 있습니다.
> 예를 들어 `sandbox` 는 실행한 코드가 예외를 던져도 "도구 호출 자체는 성공했고
> 그 결과가 ERROR" 로 보아 `isError = false` 로 응답하고 `execution_status: ERROR`
> 를 본문에 담습니다. 이는 서버의 설계 선택이며 클라이언트가 교정할 수 없습니다.

#### 도구 호출 예산 (안전가드)

에이전트 하나가 한 번 발언하는 동안 부를 수 있는 도구 호출 횟수는
`max_tool_iterations` (기본 30, 하드 상한 130) 로 정합니다. 이 상한은 두 겹의
안전장치와 함께 동작합니다.

**① 남은 횟수를 미리 알립니다.** 매 호출 직전에 남은 횟수가 프롬프트에 실립니다.
한계에 가까워질수록 촘촘해집니다.

| 남은 횟수 | 고지 |
|---|---|
| 시작 시 | 총량 (`총 30회`) 과 "결론 쓸 여유를 남기라"는 지시 |
| 100 · 80 · 60 · 50 · 40 · 30 · 20 · 10 | 남은 횟수 + 계획 조정 요청 |
| 5 · 4 · 3 · 2 | 남은 횟수 + 마무리 준비 요청 |
| 1 | **마지막 기회입니다** 경고 |

남은 예산을 알아야 모델이 결론을 쓸 자리를 스스로 남겨 둡니다. 사다리가 성기면
"아직 여유가 있다" 고 믿은 채 탐색하다 마지막 판에서 갑자기 끊깁니다.

**② 다 써도 발언을 버리지 않습니다.** 상한에 닿으면 채팅 상단에 안내 줄이 뜹니다.

> 🔧 *Senior Python Engineer 가 도구 호출 상한 30회를 모두 썼습니다 (도구 44건 실행).
> 상한을 늘릴까요, 지금까지의 관측으로 마무리할까요?*  `178초 후 자동 마무리`
> **`+15회 확장`**  `지금 마무리`

| | 확장 | 지금 마무리 | 무응답 (3분) |
|---|---|---|---|
| 진행 중인 발언 | 그 자리에서 이어 돎 | 도구 없이 결론만 | 도구 없이 결론만 |
| 그때까지의 글 · 도구 기록 | 유지 | 유지 | 유지 |
| 상한 | `+15회` (최대 130) | 그대로 | 그대로 |

예전에는 상한을 다 쓰면 그 발언이 통째로 실패로 처리되어, 스트리밍으로 흘러나온
글과 실행된 도구 관측이 전부 사라졌습니다. 원인이 엔드포인트 장애가 아니라 우리가
정한 상한인데도요. 상한은 폭주를 막으라고 있는 것이지 한 일을 버리라고 있는 것이
아닙니다. 지금은 어느 쪽을 고르든 발언과 도구 기록이 남고, 왜 거기서 멈췄는지가
발언 끝에 함께 기록됩니다.

정지 버튼을 누르면 대기 중인 물음은 모두 "확장 없음" 으로 답해집니다. 멈추라고 한
사람에게 "도구를 더 부를까요?" 를 붙잡고 있을 이유가 없습니다.

#### 컨텍스트 창 포화 (안전가드)

라운드가 쌓이면 전사(transcript)가 길어지고, 도구 출력이 더해지면 모델의 컨텍스트
창을 넘깁니다. 넘치면 엔드포인트가 400 을 돌려주고, 예전에는 그 발언이 통째로
사라졌습니다. 도구 예산과 같은 두 겹으로 막습니다.

**① 남은 여유를 미리 알립니다.** 사용률이 띠를 처음 넘을 때만 고지합니다.

| 사용률 | 고지 |
|---|---|
| 50% | 요점 위주로 쓰라고 안내 |
| 70% | 출력이 큰 도구 호출을 자제하라고 안내 |
| 85% | 곧 생략된다고 경고 + **메모리 오프로딩 지시** (아래) |
| 95% | 도구를 더 부르지 말고 지금 결론을 쓰라고 지시 |

**②-a 잘리기 전에 memory 그래프로 옮기게 합니다.** 메모리 MCP 는 대화 하나에
그래프 하나를 전 에이전트가 공유합니다. 85% 에서 그 에이전트가 메모리 쓰기 도구를
갖고 있으면 "지금까지의 핵심 결론·근거·미해결 쟁점을 한 번의 `add_observations`
로 남긴 뒤 계속하라"를 덧붙이고, 생략 안내문은 "필요하면 `search_nodes` 로
그래프에서 찾으세요"로 바뀝니다. 최종 합성에서도 잘린 초반 라운드를 그래프에서
되찾을 수 있습니다.

> 메모리에 쓴다고 컨텍스트가 그 자리에서 줄지는 않습니다. 대화 메시지는 그대로
> 남습니다. 줄어드는 것은 **잘려도 잃지 않게 되는 것**입니다.

메모리가 없는 에이전트(기본 설정의 `coder`)에게는 없는 도구를 시키지 않습니다.
오프로딩도 도구 호출이라 도구 예산이 5회 이하로 남았으면 지시하지 않습니다 —
결론 쓸 호출을 뺏는 셈이 됩니다.

**②-b 버리기 전에 물어봅니다.** 기록을 실제로 잃는 첫 순간에 안내 줄이 뜹니다.

| | 컨텍스트 상향 | 지금 마무리 | 무응답 (3분) |
|---|---|---|---|
| 진행 중인 발언 | 넓힌 창으로 이어 돎 | 도구 떼고 결론만 | 생략하고 **계속 진행** |
| 그때까지의 글·도구 기록 | 유지 | 유지 | 유지 |

무응답이 마무리가 아니라 생략 후 진행인 것이 도구 예산 쪽과 다릅니다 — 사람이
자리에 없다고 토론을 접을 이유는 없습니다. 물음은 **한 턴에 한 번**만 뜹니다.

> 도구 상한과 달리 컨텍스트 창은 **모델의 실제 한도를 넘겨 늘릴 수 없습니다.**
> LiteLLM 에서 실제 한도를 조회해 그 이상은 제안하지 않고, 조회에 실패하는 모델
> (사설 게이트웨이·vLLM·별칭)은 그 사실을 안내 줄에 함께 적습니다.

영구적으로 올리려면 에이전트 로스터의 **컨텍스트 창** 필드(`max_context_window`)를
수정하세요. 안내 줄에서의 상향은 그 턴에만 적용됩니다.

#### 연결 상태 확인

에이전트 로스터 패널에 서버별 연결 상태가 칩으로 표시됩니다. 연결 실패 칩에 마우스를
올리면 실행 명령과 함께 **서버가 stderr 로 남긴 실제 원인**이 보입니다
(`No module named mcp_server_fetch` 처럼). anyio 가 올리는
"unhandled errors in a TaskGroup" 은 원인을 담고 있지 않아, 서버 stderr 를 파이프로
받아 마지막 줄들을 보관합니다.

| 칩 | 뜻 |
|----|-----|
| 🟢 `filesystem 도구 14` | 연결됨 · 등록된 도구 수 |
| 🟠 `연결 끊김` | 세션이 끊김. 다음 도구 호출 시 자동 재연결 |
| 🔴 `연결 실패` | 기동 실패. 툴팁에 원인 표시 |
| ⚪ `비활성` | `conf.json` 에서 `"enabled": false` |

헤더의 새로고침 버튼은 연결되지 않은 서버만 다시 띄웁니다. 같은 정보를 `GET /api/mcp`
로도 조회할 수 있습니다.

#### 폐쇄망 번들의 런타임 의존성

`package_offline.py` 는 wheel 을 `wheels/` 에 모으는 데 그치지 않고 **번들 런타임 안에 설치**한 뒤,
필수 모듈이 실제로 import 되는지 검증하고 하나라도 없으면 패키징을 실패시킵니다. 포터블 런타임은
패키징 머신의 파이썬을 복사한 것이라, 설치 단계가 없으면 그 머신에 없던 패키지가 번들에도 빠진 채
배포됩니다.

버전 정합성은 `constraints.txt` 로 강제합니다. 벤더링한 샌드박스의 `requirements-server.txt` 가
`mcp>=1.2.0` 으로 상한이 없어, 제약이 없으면 pip 가 `mcp` 2.x 를 끌어옵니다. **2.x 는
`mcp.server.fastmcp` 를 제거했으므로**(`MCPServer` 로 개명) 샌드박스 서버가 기동하지 못합니다.
`requirements.txt` 도 같은 이유로 `mcp>=1.29.0,<2` 로 고정되어 있습니다.

이미 만들어진 번들에서 모듈이 빠졌다면 번들 폴더에서 `install_wheels_offline.bat` 을 실행하세요.
번들 런타임을 대상으로 재설치하고 검증까지 수행합니다.

#### 세션 수명주기

MCP 세션은 앱 기동 시 서버당 한 번 열고 **종료할 때까지 유지**합니다. 도구 호출마다
프로세스를 새로 띄우면 서버가 들고 있는 상태가 사라지기 때문입니다 — 특히 `sandbox` 는
네임스페이스별 IPython 커널에 변수·데이터프레임을 담아두므로, 재기동되면 "100MB 엑셀을
한 번만 읽는다"는 전제가 통째로 깨집니다. 기동 비용도 호출마다 다시 치릅니다
(파일 쓰기 5회 기준 1.36초 → 0.02초).

stdio 세션은 서버마다 전용 태스크가 소유합니다. anyio 의 cancel scope 는 태스크에 묶여
있어 컨텍스트를 연 태스크가 아닌 곳에서 닫으면 런타임 에러가 나기 때문입니다.
서버 프로세스가 죽으면 다음 호출에서 **한 번만** 자동 재연결합니다 — 도구 자체가 실패한
경우에는 재시도하지 않습니다. 파일 쓰기 같은 부작용이 두 번 일어날 수 있기 때문입니다.

서버별 연결 상태는 `MCPManager.connection_status()` 로 조회할 수 있습니다.

#### 개발 PC 준비 (최초 1회, 인터넷 필요)

```bash
pip install -r requirements.txt   # mcp-server-git, jupyter_client, ipykernel 포함
python setup_mcp.py               # workspace(git init) + mcp_node + mcp_sandbox
```

`setup_mcp.py` 가 하는 일을 직접 하려면:
```bash
npm install --omit=dev --prefix ./mcp_node \
  @modelcontextprotocol/server-filesystem \
  @modelcontextprotocol/server-memory \
  @modelcontextprotocol/server-sequential-thinking
git clone --depth 1 https://github.com/HaJaehee/AirgappedPySandbox ./mcp_sandbox
git init ./workspace              # git MCP 서버는 유효한 저장소를 요구합니다
```

> **Python MCP 서버는 앱과 같은 인터프리터로 실행됩니다.** `PYTHON_BIN` 을 지정하지 않으면
> `sys.executable` 이 기본값입니다. PATH 의 `python` 을 쓰면 가상환경에서 앱을 돌릴 때
> 의존성이 없는 다른 인터프리터를 가리켜 서버가 기동에 실패합니다.

#### 작업 공간 (모든 도구가 같은 폴더를 보게 하기)

`filesystem` · `git` · `memory` · `sandbox` MCP 는 **하나의 작업 공간**을 공유합니다.
기본값은 프로젝트 루트의 `workspace/` 이고, 앱이 기동할 때 **절대 경로로 정규화**합니다.

절대 경로로 만드는 것이 핵심입니다. 상대 경로(`./workspace`)를 그대로 넘기면 받는 쪽이
각자의 cwd 로 해석하는데, `filesystem` 은 node 프로세스, `sandbox` 는 python 프로세스라
같은 값을 줘도 서로 다른 폴더를 볼 수 있습니다. 실제로 샌드박스는 받은 값을
`Path(v).resolve()` 로 자기 cwd 기준으로 풀어서, `./workspace` 를 주면 **샌드박스 설치
폴더 아래**를 가리켰습니다.

```bash
# 확인
python -c "import os, app.config; print(os.environ['WORKSPACE_DIR'])"
```

#### 대화별 작업 공간 (런타임 변경)

에이전트 설정 패널의 **작업 공간** 칸에 폴더를 적고 `적용` 을 누르면 그 대화에서 쓸
폴더가 바뀝니다. 값은 **세션에 저장되며 `conf.json` 은 바뀌지 않습니다.** 비워 두면
기본값을 씁니다.

MCP 서버는 허용 경로를 기동 시점에 받으므로(`filesystem` 은 argv, `sandbox` 는
`SANDBOX_WORKSPACE`), **서버 묶음은 작업 공간마다 따로 뜹니다.** 값을 바꿔도 지금
떠 있는 서버를 건드리지 않고, 이 대화의 다음 토론이 그 폴더의 묶음을 받습니다.

그래서 **서로 다른 폴더를 쓰는 대화는 동시에 토론할 수 있습니다.** 같은 폴더를 쓰는
대화끼리는 서버를 함께 쓰고(파일을 이미 공유하는 사이입니다), 그 안에서 지식 그래프와
샌드박스 커널은 대화 단위로 갈립니다.

묶음 수에는 상한이 있습니다(`MCP_MAX_RUNTIMES`, 기본 4). 묶음 하나가 MCP 서버
프로세스 전부를 띄우므로 이것은 동시 토론 수가 아니라 메모리 예산입니다. 자리가 다
차면 놀고 있는 묶음부터 정리하고, 전부 쓰는 중이면 어느 대화가 붙잡고 있는지 알려주며
거절합니다. 쓰던 폴더는 마지막 대화가 끝난 뒤에도 5분(`MCP_RUNTIME_IDLE_TTL`) 동안
살아 있어, 연달아 돌릴 때 다시 기다리지 않습니다.

토론 중에는 그 대화의 작업 공간 변경만 막습니다.

#### 파일 언급 (@) 과 업로드

입력창에서 `@` 를 치면 **이 대화의 작업 공간** 파일·폴더와 이번 토론에 참여하는 전문가가
뜹니다. ↑↓ 로 고르고 Enter·Tab 으로 넣으며, Esc 로 닫습니다. 창이 열려 있는 동안의 Enter 는
보내기가 아니라 고르기입니다. 공백이 있는 이름은 `@"docs/요구사항 정의서.pdf"` 처럼 들어갑니다.

보낼 때 앱이 언급을 풀어 메시지 끝에 참조 블록을 붙입니다.

```text
[@참조]
참조 파일 — 작업 공간: D:\work\proj
- docs/요구사항 정의서.pdf (1.2 MB · 큼, 필요한 부분만 읽으세요)
- src/ (폴더)
파일 내용은 붙이지 않았습니다. 필요하면 그 형식을 다루는 파일 도구로 이 경로를 직접 읽으세요.

지목한 전문가
- System Architect (High-Level Architecture & Tech Stack)
```

- **내용이 아니라 경로만.** 사용자 메시지는 모든 발언자의 전사와 합성에 라운드마다
  복사되므로, 내용을 붙이면 컨텍스트가 금방 찹니다. 파일 종류는 가리지 않습니다 — PDF·오피스
  문서는 그 형식을 읽는 MCP 서버(예: office MCP)를 에이전트에 붙여 두세요.
- **안전장치.** 작업 공간 밖(절대 경로·`..`·바깥을 가리키는 링크)은 참조하지 않습니다.
  없는 경로와 참여하지 않는 전문가는 빼고 알림으로 알려 줍니다. 코드 블록·인라인 코드 안의
  `@`(데코레이터 등)와 메일 주소는 언급이 아닙니다. 목록은 `.git`·`node_modules`·가상환경과
  최상위 `.gitignore` 의 단순한 규칙을 건너뛰고, 2만 항목에서 멈추며, 5초만 기억합니다.
- **전문가 지목은 글로 전달됩니다.** 오케스트레이터와 발언자들이 참조 블록을 읽고 반영합니다.
  발언 순서를 강제로 바꾸지는 않습니다.

입력창 왼쪽의 **워크스페이스에 파일 업로드** 버튼은 파일을 `<작업 공간>/uploads/` 에 저장하고
입력창에 `@경로` 를 넣습니다. 목록 기억을 즉시 비우므로 올린 파일이 바로 언급 목록에 나옵니다.
같은 이름은 덮어쓰지 않고 `이름 (2).확장자` 로 저장하며, 파일 하나는 100 MB 까지입니다.

#### 대화별 지식 그래프

`memory` MCP 는 공식 서버가 아니라 `mcp_servers/memory_scoped/` 의 포크입니다.

공식 서버는 프로세스 하나에 그래프 파일 하나(`MEMORY_FILE_PATH`)를 씁니다. 그런데 MCP 서버
프로세스는 모든 대화가 공유하므로, **다음 대화가 이전 대화의 기억을 그대로 읽었습니다.** 이
서버를 둔 이유는 라운드가 길어져 컨텍스트가 잘려도 그 토론의 결정이 남게 하려는 것이지 대화
간에 지식을 쌓으려는 것이 아니었으므로, 동작이 목적과 정반대였습니다.

포크는 격리 기준을 프로세스 수명이 아니라 **요청이 들고 오는 스코프**로 옮깁니다. 그래프는
`workspace/.memory-graphs/<대화 id>.jsonl` 로 나뉘고, 어느 그래프를 열지는 앱이 매 호출의
요청 메타데이터(`_meta`)로 알려줍니다.

스코프를 도구 인자로 두고 모델에게 넘기게 하지 않은 것이 요점입니다. 모델이 한 번 잊으면
조용히 남의 대화에 쓰게 되는데, 그게 바로 고치려던 증상입니다. 어느 대화의 호출인지는 호스트인
앱이 이미 알고 있으니 앱이 말하는 것이 맞습니다. 모델이 `graph_id` 인자로 다른 그래프를
지목해도 `_meta` 가 이깁니다.

스코프가 아예 없는 호출은 공용 그래프가 아니라 `unscoped-<pid>.jsonl` 로 떨어지고 서버가
stderr 에 경고를 남깁니다. 공용으로 떨어뜨리면 주입이 깨졌을 때 예전 증상이 조용히 되살아
납니다.

실행 사본(`mcp_node/memory-scoped.mjs`)은 앱이 기동할 때 자동으로 놓입니다. 포크를 갱신한
뒤 따로 실행할 명령은 없습니다.

#### 무엇을 어디까지 공유하는가

경계는 서버마다 다르고, 그 경계는 호스트가 정합니다.

| 상태 | 경계 | 이유 |
|------|------|------|
| 작업 공간 폴더 | 전부 공유 | 에이전트 사이의 인계 채널. filesystem·git diff·아티팩트 뷰어에 그대로 남습니다 |
| 지식 그래프 | 대화 | 합의된 사실은 그 토론의 참가자 전원이 함께 봐야 합니다 |
| 커널 네임스페이스 | 대화 × 발언자 | 커널 변수는 어디에도 기록되지 않습니다 |

커널을 발언자 단위로 나눈 이유가 핵심입니다. 다음 발언자의 컨텍스트에는 앞 발언의 **본문만**
들어가고 도구 실행 로그는 들어가지 않습니다(`_build_context_for_agent`). 커널을 공유하면
크리틱은 존재조차 모르는 변수를 물려받게 되고, 세 라운드 전의 낡은 `df` 를 현재 것으로 오인해
자신 있게 틀린 리뷰를 씁니다. 게다가 크리틱에게 샌드박스를 준 이유는 "코드를 직접 실행해
반박"인데, 코더가 대화형으로 만들어 둔 객체를 들여다보는 것은 코더의 *결과*를 보는 것이지
코더의 *코드*를 검증하는 것이 아닙니다.

그래서 인계는 파일로 합니다. 코더가 `write_workspace_file` 로 남기고 크리틱이
`run_python_file` 로 다시 실행합니다 — 실패하면 `NameError` 로 시끄럽게 실패하고, 파일을 열면
해결됩니다. 한 에이전트가 라운드를 넘겨가며 자기 코드를 고치는 연속성은 그대로입니다.

샌드박스는 원래 `namespace` 인자로 격리하되 요청 메타데이터가 있으면 그것을 우선하도록
만들어져 있었는데, 앱이 메타데이터를 보내지 않아 그 경로가 한 번도 쓰이지 않았습니다.

필요한 커널 수는 `동시 토론 수 × 샌드박스를 쓰는 에이전트 수` 입니다. 기본 구성은 coder·critic
둘이라 `SANDBOX_MAX_NAMESPACES` 기본값을 16 으로 두었습니다(동시 토론 8건). 넘으면 가장 오래
안 쓴 커널부터 정리되고 그 변수들은 사라집니다.

#### 소스만 갱신해 반입하기

런타임(포터블 파이썬 · node.exe · wheel · MCP 서버)은 한 번 반입하면 버전을 올릴 때까지
그대로 씁니다. 코드만 고쳤을 때 수백 MB 를 다시 넣는 것은 용량 문제이기 이전에 **반입 심사를
매번 처음부터 다시 받는 일**입니다.

```bash
python package_source.py   # dist/MultiAgentOrchestrator_source_YYYYMMDD.zip (약 600KB)
```

담는 것은 **돌아가는 앱을 갱신하는 데 필요한 것뿐**입니다.

| 담기는 것 | 빠지는 것 |
|---|---|
| `app/`, `mcp_servers/` | `python_runtime/`, `node_runtime/`, `wheels/`, `mcp_sandbox/` |
| `mcp_node/memory-scoped.mjs` (포크한 서버의 실행 사본) | `workspace/`, `multiagent.db`, `conf.json` |
| `conf.example.json`, `.env.example`, `requirements.txt` | `tests/`, `wiki/`, `CLAUDE.md` |
| `docs/` (사용 설명서 마크다운 + 렌더러) | `docs/user_manual_html/` (렌더링 산출물) |
| `LICENSE.md` | |
| `setup_mcp.py`, `open_browser.py`, `README.md` | 패키징 스크립트 (`package_*.py|ps1`) |

포함 목록은 제외 목록이 아니라 **허용 목록**입니다. 제외 목록으로 짜면 나중에 생긴 디렉터리가
조용히 딸려 들어가지만, 허용 목록이면 그냥 빠지고 빠진 것은 눈에 띕니다.

스크립트가 거부하는 세 가지:

1. **대상의 `conf.json` 덮어쓰기.** 로컬 `conf.json` 은 아예 담지 않습니다. 배포본의
   `conf.json` 에는 그 망의 실제 엔드포인트가 있고, 새 설정 항목은 `conf.example.json`
   과 비교해 손으로 옮깁니다.
2. **큰 파일.** `--max-file-mb`(기본 2MB)를 넘으면 중단합니다. 소스 패키지에 메가바이트급
   파일이 있다면 런타임 산출물이 새어 들어온 것입니다.
3. **키처럼 보이는 값.** API 키·토큰·개인 키 헤더를 스캔해 중단합니다(`--allow-secrets` 로 강행).
   `conf.json` 은 gitignore 대상이라 누군가 실제 키를 적어 두었을 수 있고, 그것을 반입 심사에서
   발견하는 것은 곤란합니다.

대상 장비에서는 압축을 푼 내용을 설치본 위에 덮어씁니다. `app/` 은 파일 단위로 덮지 말고
**통째로 교체**하세요 — 이번 갱신에서 삭제된 모듈이 대상에 남으면 계속 import 됩니다.
`conf.json` 은 패키지에 없으므로 그대로 살아남습니다. 동봉된 `MANIFEST.txt` 에 파일별
SHA-256 이 있어 반입 심사와 무결성 확인에 씁니다.

```powershell
Get-Content MANIFEST.txt | Where-Object { $_ -notmatch '^#' } | ForEach-Object {
    $sha, $size, $rel = ($_ -split '\s+', 3)
    if ((Get-FileHash $rel -Algorithm SHA256).Hash -ne $sha.ToUpper()) { "다름: $rel" }
}
```

> `requirements.txt` 가 이전 반입본과 다르면 런타임에 없는 패키지가 생긴 것이므로 소스 갱신만으로는
> 실행되지 않습니다. 그때는 `package_offline.py` 로 전체 번들을 다시 만들어야 합니다.

#### 실행 경로 재정의

`command` / `args` 도 환경변수 치환을 지원하므로, 개발 PC와 폐쇄망 배포 번들이 `conf.json`
하나를 그대로 공유합니다. 값을 비워두면 괄호 안의 기본값이 쓰입니다.

| 변수 | 기본값 | 용도 |
|------|--------|------|
| `NODE_BIN` | `node` | Node 실행기 |
| `PYTHON_BIN` | 앱과 동일한 인터프리터 (`sys.executable`) | Python 실행기 |
| `MCP_NODE_HOME` | `./mcp_node` | Node MCP 서버 설치 위치 |
| `MCP_SANDBOX_HOME` | `./mcp_sandbox` | 샌드박스 서버 위치 |
| `WORKSPACE_DIR` | `<프로젝트 루트>/workspace` | 에이전트 공용 작업 공간. 기동 시 절대 경로로 정규화되며 filesystem·git·memory·sandbox 가 모두 이 폴더를 공유합니다 |
| `SANDBOX_KERNEL_PYTHON` | `PYTHON_BIN` | 샌드박스 커널 인터프리터 |

폐쇄망 번들에서는 `run_mado.bat` / `run_mado.ps1` 이 위 값을 자동으로 채웁니다.
두 스크립트는 서버가 응답하기 시작하면 **기본 브라우저로 UI 를 자동으로 엽니다.**
띄우지 않으려면 `MAO_NO_BROWSER=1` 을 설정하고 실행하세요 (기다리는 시간은
`MAO_BROWSER_TIMEOUT`, 기본 90초).

### 로컬 / 사내 엔드포인트 예시

`agents` 안의 해당 에이전트에 아래 항목을 넣습니다.

```json
"agents": {
  "// coder": "Ollama (API 키 불필요)",
  "coder": {
    "model": "ollama_chat/qwen2.5-coder:14b",
    "api_base": "http://localhost:11434"
  },

  "// coder (LM Studio / vLLM, OpenAI 호환)": [
    "\"model\": \"openai/local-model\",",
    "\"api_base\": \"http://localhost:1234/v1\",",
    "\"api_key\": \"lm-studio\""
  ],

  "// orchestrator": "Azure OpenAI",
  "orchestrator": {
    "model": "azure/my-gpt4o-deployment",
    "api_base": "https://my-resource.openai.azure.com",
    "api_version": "2024-10-21",
    "api_key": "${AZURE_OPENAI_API_KEY}"
  }
}
```

환경 변수는 `${VAR}` 외에 `${VAR:-기본값}`, 그리고 중첩(`${A:-${B:-기본값}}`) 형태까지 지원합니다.
빈 값으로 해석된 항목은 "미설정"으로 간주되어 `llm` 전역값을 상속합니다.

### 에이전트 추가

새로운 전문 에이전트를 추가하거나 MCP 서버를 확장하려면 `conf.json` 의 `agents` 에 아래와 같이
추가하기만 하면 자동으로 등록됩니다
(생략한 항목은 `llm` 값을 상속하며, `"enabled": false` 로 잠시 비활성화할 수 있습니다):

```json
"data_scientist": {
  "name": "Data Scientist",
  "role": "Data Analysis & ML Pipeline",
  "model": "openai/gpt-4o",
  "api_key": "${OPENAI_API_KEY}",
  "temperature": 0.2,
  "card_color": "#0097a7",
  "icon": "query_stats",
  "allowed_mcp_servers": ["filesystem"],
  "system_prompt": "데이터 파이프라인 설계 및 머신러닝 모델 아키텍처 검토를 전담합니다.",
  "sequential_thinking": {
    "enabled": true,
    "max_steps": 8
  }
}
```

현재 각 에이전트가 어떤 모델/엔드포인트로 잡혔는지는 `GET /api/agents` 또는 UI 로스터 카드의 툴팁에서 확인할 수 있습니다.

#### 카드 색과 아이콘 (`card_color` · `icon`)

화면에 보이는 겉모습입니다. 둘 다 **적지 않아도 됩니다** — 그러면 예전처럼 에이전트 키에서
자동으로 정해집니다 (`orchestrator`·`architect`·`coder`·`critic` 은 고정색, 그 밖의 키는
키에서 결정되는 팔레트 색이라 언제 어느 PC 에서 열어도 같은 색입니다).

| 항목 | 값 | 쓰이는 곳 |
|---|---|---|
| `card_color` | `"#0097a7"` 또는 Quasar 색 이름(`"teal-8"`) | 아바타 배경, 역할 뱃지, **로스터·채팅 카드 테두리** |
| `icon` | 머티리얼 아이콘 이름(`"query_stats"`) 또는 이미지 경로 | 아바타 |

- 두 값 모두 **에이전트 추가** 다이얼로그와 **페르소나 편집** 화면에서 고를 수 있고, 고른 값이
  이 파일로 되돌아옵니다. 색은 견본 12색 중에서 고르거나 색상 선택기로 직접 지정합니다.
- **이미지 업로드**를 쓰면 그림이 프로젝트 루트의 `data/agent_icons/` 로 복사되고(폴더가 없으면
  생성), `icon` 에는 `data/agent_icons/<키>-<내용해시>.png` 같은 **상대 경로**만 적힙니다.
  폴더째 다른 PC 로 옮겨도 그대로 맞습니다. png·jpg·gif·webp·svg·bmp·ico, 2MB 까지.
- 그림을 찾을 수 없으면(지웠거나, 경로를 잘못 적었거나, 설정만 들고 옮겼거나) **기본 아이콘으로
  조용히 물러섭니다.** 아바타가 깨진 이미지로 남지 않습니다.
- 이 값들도 **대화를 시작하는 순간 함께 고정됩니다.** 나중에 색을 바꿔도 이미 시작한 대화의
  카드는 그때의 색과 아이콘 그대로입니다 (`session_agents.card_color` / `icon_path`).
