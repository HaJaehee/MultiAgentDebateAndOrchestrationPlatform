# 폐쇄망 배포

> 상위: [워크플로우 개관](README.md) · 이전: [산출물 생성과 내보내기](04-artifact-and-export.md)
>
> 파일: `package_offline.py` (전체 번들) · `package_source.py` (소스 갱신)

인터넷 없는 망에 반입하는 것을 전제로 만들어졌습니다. 패키징 스크립트가 둘인
이유는 **처음 반입**과 **이후 갱신**의 성격이 다르기 때문입니다.

---

## 두 개의 패키지

| | `package_offline.py` | `package_source.py` |
| :--- | :--- | :--- |
| 언제 | 최초 반입, 런타임 버전 업 | 코드만 고쳤을 때 |
| 크기 | 수백 MB | 수백 KB |
| 포함 | 런타임 전부 + 소스 + 설명서 | 소스 + 설명서 |
| 심사 | 전체를 처음부터 | 바뀐 것만 |

런타임은 한 번 반입하면 버전을 올릴 때까지 그대로 씁니다. 코드만 고쳤을 때
그것을 매번 다시 반입하는 것은 용량도 용량이지만, **반입 심사를 매번 처음부터
다시 받는 일**입니다.

---

## 전체 번들 만들기

```bash
python package_offline.py [--skip-node] [--skip-sandbox]
                          [--node-version 22.22.2] [--sandbox-src <경로>]
```

Windows 에서 실행하는 것을 전제로 합니다 — 포터블 런타임과 wheel 이 실행
플랫폼 기준으로 수집되기 때문입니다. **패키징 시점에는 인터넷이 필요하고,
산출물 실행 시점에는 필요하지 않습니다.**

```text
MultiAgentOrchestrator_bundle/
├── app/                    애플리케이션 소스
├── conf.json               설정 (없으면 conf.example.json 에서 복사)
├── LICENSE.md              라이선스 (LGPL-3.0 전문 + 제3자 고지)
├── wheels/                 오프라인 pip wheel
├── python_runtime/         포터블 CPython
├── node_runtime/           node.exe (npm 미포함)
├── mcp_node/               filesystem / memory / sequential-thinking
├── mcp_servers/            포크한 MCP 서버 원본
├── mcp_sandbox/            AirgappedPySandbox
├── docs/                   사용 설명서 (마크다운 + 렌더링된 HTML)
├── workspace/              작업 공간 (git 초기화됨)
└── run_mado.bat|ps1     실행 스크립트
```

### 실행 스크립트가 하는 일

`run_mado.bat` / `.ps1` 은 생성되는 산출물입니다.

1. MCP 경로 환경변수를 자동 주입 (`NODE_BIN`, `PYTHON_BIN`, `MCP_NODE_HOME`,
   `MCP_SANDBOX_HOME`, `WORKSPACE_DIR`)
2. 서버 기동
3. `open_browser.py` 를 백그라운드로 띄워, 포트가 응답하면 브라우저를 엽니다

**접속 주소를 하드코딩하지 않습니다.**

```bat
for /f "usebackq delims=" %%i in (`"%PYTHON_BIN%" -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"`) do set "APP_URL=%%i"
```

`conf.json` 의 `app` 을 그대로 읽습니다. 두 곳에 같은 값을 적어 두면 반드시
어긋납니다.

### 압축에서 빠지는 것

스테이징 폴더(`dist/MultiAgentOrchestrator_bundle/`)는 빌드 사이에 남아 있습니다.
거기서 `run_mado.bat` 로 앱을 한 번 띄우면 대화 DB 와 에이전트가 만든 파일이
그 안에 생기고, 압축이 폴더를 통째로 담던 시절에는 그것이 그대로 반입 대상이
되었습니다.

| 빠지는 것 | 왜 |
| :--- | :--- |
| 최상위의 `*.db` / `*.db-wal` / `*.db-shm` / `*.sqlite*` | 대화 기록. 받는 쪽에서 첫 기동 때 새로 만들어집니다 |
| 최상위의 `.env` | 이 기계의 실제 자격증명 |
| 최상위의 `*.log`, `MANIFEST.txt` | 실행·패키징 잔재 |
| `workspace/` 의 내용물 (`.gitkeep`, `.git/` 제외) | 여기서 돌려 본 흔적이지 배포물이 아닙니다 |

**최상위에서만** 봅니다. 벤더링한 트리(`python_runtime/`, `wheels/`,
`mcp_sandbox/`, `node_runtime/`)는 받은 그대로 나갑니다 — 그 안의 `__pycache__`
나 `MANIFEST.txt` 는 그 배포판의 일부이지 이 앱이 만든 잔재가 아닙니다.

`workspace/.git/` 은 남깁니다. git MCP 서버는 유효한 저장소가 아니면 기동에
실패합니다.

무엇이 빠졌는지는 조용히 넘기지 않고 빌드 로그에 찍습니다.

```text
      운영 잔재 5개를 제외했습니다:
        - MANIFEST.txt
        - multiagent.db
        - workspace/.memory-graphs
        - workspace/handoff.py
        - workspace/workspace
```

---

### 런타임 검증

패키징 단계에서 필수 모듈 import 를 확인하고, 하나라도 없으면 **실패로
처리합니다.**

```python
REQUIRED_IMPORTS = [
    ("nicegui", "웹 UI"),
    ("litellm", "LLM 호출"),
    ("mcp", "MCP 클라이언트"),
    ("mcp.server.fastmcp", "샌드박스 MCP 서버 (mcp 2.x 에서는 제거됨 → mcp<2 필요)"),
    ("mcp_server_git", "git MCP 서버"),
    ("jupyter_client", "샌드박스 커널"),
    ("ipykernel", "샌드박스 커널"),
]
```

이것이 없으면 폐쇄망에서 앱을 띄운 뒤에야 기능이 빠진 것을 알게 됩니다.

`PIP_CONSTRAINTS` 로 `mcp>=1.29.0,<2` 를 강제합니다. 벤더링한 샌드박스의
`requirements-server.txt` 는 `mcp>=1.2.0` 으로 상한이 없어, 그대로 두면 pip 가
mcp 2.x 를 끌어옵니다. mcp 2.x 는 `mcp.server.fastmcp` 를 제거했으므로 샌드박스
서버가 기동하지 못합니다.

---

## 소스 갱신 패키지

```bash
python package_source.py [--out-dir dist] [--max-file-mb 2] [--allow-secrets]
                         [--skip-manual-html]
```

```text
MultiAgentOrchestrator_source/
├── app/                          애플리케이션 소스 (통째로 교체)
├── mcp_servers/                  포크한 MCP 서버 원본
├── mcp_node/memory-scoped.mjs    그 실행 사본
├── docs/                         사용 설명서 (마크다운 원본 + 렌더러)
├── conf.example.json             설정 템플릿
├── .env.example
├── requirements.txt
├── setup_mcp.py
├── open_browser.py
├── README.md
├── LICENSE.md                    라이선스 (LGPL-3.0 전문 + 제3자 고지)
└── MANIFEST.txt                  파일별 SHA-256
```

### 설명서는 어떻게 담기는가

두 패키지 모두 `docs/` 를 담습니다. 폐쇄망에서는 저장소도 위키도 열 수 없으므로
설명서가 설치본과 같이 다녀야 합니다.

| | 마크다운 원본 | 렌더링된 HTML |
| :--- | :--- | :--- |
| 전체 번들 | 담김 | **미리 렌더링해서 담김** |
| 소스 갱신 패키지 | 담김 | **미리 렌더링해서 담김** |

두 패키지 모두 받는 쪽이 아무것도 실행하지 않아도 읽을 수 있어야 하므로
`docs/user_manual_html/index.html` 을 패키징 시점에 만들어 둡니다. 렌더러와
마크다운 원본도 함께 담기므로 대상에서 다시 돌릴 수도 있습니다.

**작업 트리의 `docs/user_manual_html/` 을 긁어 담지는 않습니다.** 그 폴더는 언제
만들어진 것인지 알 수 없어서, 마크다운을 고치고 렌더러를 돌리지 않았다면 한
패키지 안에서 원본과 산출물이 서로 다른 말을 하게 됩니다. 매번 임시 폴더에 새로
렌더링해 담으므로 둘이 어긋날 수가 없고, 담긴 HTML 도 다른 파일과 똑같이 크기
검사·비밀값 검사를 거쳐 `MANIFEST.txt` 에 해시가 남습니다.

렌더링을 건너뛰려면 `--skip-manual-html` 을 씁니다 (마크다운 원본은 그대로 담깁니다).

```bash
python docs/render_user_manual.py
```

**렌더링에 실패해도 번들 생성을 막지 않습니다.** 설명서는 앱이 도는 데 필요한
것이 아니고, 원본 마크다운은 이미 담겼습니다.

렌더러는 표준 라이브러리만 쓰고 산출물에 외부 요청이 하나도 없습니다 — 문서를
보려고 의존성을 들이거나 CDN 을 부를 수 없는 환경이 이 프로젝트의 전제입니다.

---

### 허용 목록으로 짜는 이유

포함 목록은 **allow-list** 입니다. 제외 목록으로 짜면 새 디렉터리가 생겼을 때
조용히 딸려 들어갑니다. 허용 목록에서는 새 디렉터리가 그냥 빠지고, **빠진 것은
눈에 띕니다.**

### 세 가지 거부 조건

| 조건 | 이유 |
| :--- | :--- |
| `conf.json` 을 담지 않음 | 배포본의 것에 그 망의 실제 엔드포인트가 있음. 덮어쓰면 모든 에이전트가 죽음 |
| 2MB 초과 파일이 있으면 중단 | 소스 패키지에 메가바이트급 파일이 있다면 런타임 산출물이 샌 것 |
| 키처럼 보이는 값이 있으면 중단 | `conf.json` 은 gitignore 대상이라 누군가 실제 키를 적어 두었을 수 있음 |

세 번째는 `--allow-secrets` 로 무시할 수 있지만, **반입 심사는 그것을 발견하기에
가장 나쁜 자리**입니다.

감지 패턴: OpenAI 계열(`sk-`), Anthropic(`sk-ant-`), Google(`AIza`),
GitHub(`ghp_`), Slack(`xox*-`), 개인 키 헤더.

### 대상에서 적용

```text
1. app/ 을 통째로 교체       ← 파일 단위로 덮으면 이번 갱신에서 삭제된 모듈이
                               남아 계속 import 됩니다
2. 나머지 파일 덮어쓰기
3. conf.json 은 손대지 않음  ← 패키지에 없으므로 자동으로 살아남음
4. MANIFEST.txt 로 무결성 확인
```

새 설정 항목은 `conf.example.json` 을 보고 손으로 옮깁니다.

```powershell
Get-Content MANIFEST.txt | Where-Object { $_ -notmatch '^#' } | ForEach-Object { ... }
```

---

## 설정 하나로 두 환경

개발 PC 와 폐쇄망 번들이 **같은 `conf.json` 을 공유**합니다. 경로가 전부
환경변수 치환이기 때문입니다.

| 변수 | 개발 PC | 번들 |
| :--- | :--- | :--- |
| `NODE_BIN` | `node` | `node_runtime\node.exe` |
| `PYTHON_BIN` | `sys.executable` | `python_runtime\python.exe` |
| `MCP_NODE_HOME` | `./mcp_node` | 번들 안의 경로 |
| `MCP_SANDBOX_HOME` | `./mcp_sandbox` | 번들 안의 경로 |
| `WORKSPACE_DIR` | 프로젝트의 `workspace` | 번들 안의 `workspace` |

```json
"command": "${NODE_BIN:-node}",
"args": ["${MCP_NODE_HOME:-./mcp_node}/node_modules/.../dist/index.js"]
```

실행 스크립트가 위 값을 자동으로 채웁니다. **설정 파일에 손댈 일이 없습니다.**

---

## 폐쇄망에서 주의할 것

| 항목 | 조치 |
| :--- | :--- |
| `npx` | 쓰지 마세요. 패키지가 없으면 npm 레지스트리에 접속합니다. 진입점을 `node` 로 직접 실행 |
| `fetch` MCP 서버 | 외부 네트워크가 열린 환경에서만. 기본값이 `"enabled": false` 인 이유 |
| 웹 검색 MCP | 기본 구성에서 제외됨. 사내 SearXNG 등을 감싸 쓰세요 |
| LLM 엔드포인트 | 사내 게이트웨이나 로컬 서버여야 합니다 |

---

## 실행 스크립트만 다시 만들기

실행 스크립트는 생성 산출물이지 소스가 아닙니다. 번들 전체를 다시 만들지 않고
갱신할 수 있습니다.

```bash
python package_offline.py --launchers-only <설치본 경로>
```

---

## 라이선스 고지

`LICENSE.md` 는 **두 패키지 모두**에 담깁니다. 이 프로젝트는 의존성을 통째로
재배포하므로(포터블 CPython, `node.exe`, 오프라인 wheel, 벤더링한 MCP 서버),
각 의존성의 라이선스 의무를 직접 이행해야 합니다.

- 이 프로젝트는 **LGPL-3.0** 입니다. 전문이 `LICENSE.md` 에 실려 있습니다
- 배포물에서 가장 엄격한 라이선스는 `pyzmq` wheel 에 동봉된 `libzmq`(LGPL-3.0)
  입니다. `jupyter_client` / `ipykernel` 이 커널 통신에 끌어옵니다
- 제3자 의존성의 라이선스 원문은 각 wheel 의 `.dist-info/` 안에 함께 실려
  번들로 따라옵니다 — 별도 수집 단계가 필요 없습니다

---

## 관련 문서

- [설치와 첫 실행](../02-getting-started/01-installation.md) — 개발 PC 준비
- [conf.json 설정](../02-getting-started/02-configuration.md#환경변수-치환) — 치환 규칙
- [MCP 호스트](../03-core/04-mcp-host.md) — 서버 구성
- [기술 스택](../01-overview/01-tech-stack.md) — 왜 파이썬 단일 프로세스인가

---

> 다음 섹션: [레퍼런스 개관](../05-reference/README.md)
