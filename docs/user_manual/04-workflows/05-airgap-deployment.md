# 폐쇄망 배포

> 상위: [워크플로우 개관](README.md) · 이전: [산출물 생성과 내보내기](04-artifact-and-export.md)
>
> 관련 소스: `package_offline.py` (전체 번들 빌드) · `package_source.py` (소스 갱신 패키지 빌드)

MADO는 인터넷 연결이 완전히 차단된 폐쇄망(Airgap) 환경으로의 반입 배포를 기본 전제로 설계되었습니다. 패키징 스크립트가 2종류로 분리되어 있는 이유는 **최초 구축 시의 대규모 반입**과 **운영 중 소스 코드 갱신**의 성격 및 보안 심사 절차가 근본적으로 다르기 때문입니다.

---

## 2가지 패키징 방식 비교

| 비교 항목 | 전체 오프라인 번들 (`package_offline.py`) | 소스 갱신 전용 패키지 (`package_source.py`) |
| :--- | :--- | :--- |
| **적용 시점** | 최초 시스템 반입 설치, Python/Node 런타임 버전 업그레이드 | 애플리케이션 소스 코드 및 프롬프트 수정 시 |
| **패키지 용량** | 수백 MB (런타임 바이너리 및 전체 wheel 포함) | 수백 KB (순수 텍스트 소스 및 설명서) |
| **포함 내용** | CPython/Node 포터블 런타임 + wheel + 소스 + 오프라인 설명서 | 애플리케이션 소스 + 템플릿 + 오프라인 설명서 |
| **보안 심사 범위** | 바이너리를 포함한 전체 패키지 신규 심사 | 변경된 소스 코드 diff 및 텍스트 파일만 심사 |

Python 및 Node.js 런타임 환경은 한 번 성공적으로 반입해 두면 메이저 버전 업그레이드가 없는 한 그대로 유지하여 사용합니다. 소스 코드 몇 줄만 수정했을 때 매번 수백 MB에 달하는 런타임 전체를 다시 반입하는 것은 네트워크 대역폭 낭비일 뿐만 아니라, **보안 반입 심사를 매번 기초부터 다시 받아야 하는 비효율**을 초래하기 때문입니다.

---

## 최초 배포용 전체 오프라인 번들 생성

```bash
python package_offline.py [--skip-node] [--skip-sandbox]
                          [--node-version 22.22.2] [--sandbox-src <경로>]
```

빌드 작업은 Windows 환경에서 실행하는 것을 원칙으로 합니다. 포터블 CPython 런타임과 오프라인 pip wheel 파일들이 대상 운영체제 플랫폼을 기준으로 다운로드 및 수집되기 때문입니다. **패키징을 빌드하는 외부 개발 PC에서는 인터넷 연결이 필수적이며, 생성된 최종 번들을 실행하는 폐쇄망 서버에서는 일체의 외부 네트워크 연결을 필요로 하지 않습니다.**

```text
MultiAgentOrchestrator_bundle/
├── app/                    애플리케이션 전체 소스 코드
├── conf.json               기본 설정 파일 (부재 시 conf.example.json에서 자동 복사)
├── LICENSE.md              오픈소스 라이선스 고지문 (LGPL-3.0 전문 및 제3자 라이선스)
├── wheels/                 오프라인 설치용 pip wheel 패키지 아카이브
├── python_runtime/         Windows용 독립 포터블 CPython 환경
├── node_runtime/           독립 실행형 node.exe (npm 제외)
├── mcp_node/               Node 기반 MCP 서버 번들 (filesystem, memory, sequential-thinking)
├── mcp_servers/            자체 포크한 MCP 서버 원본 소스
├── mcp_sandbox/            Python 코드 실행용 AirgappedPySandbox
├── docs/                   사용 설명서 (마크다운 원본 + 오프라인 렌더링된 HTML)
├── workspace/              기본 작업 공간 디렉터리 (git 저장소 초기화 완료)
└── run_mado.bat | ps1      원클릭 실행 런처 스크립트
```

### 원클릭 실행 스크립트의 동작 원리

`run_mado.bat` / `.ps1` 파일은 빌드 과정에서 자동 생성되는 진입점 스크립트입니다:

1. 번들 내 상대 경로를 기반으로 MCP 핵심 환경변수를 자동 주입합니다 (`NODE_BIN`, `PYTHON_BIN`, `MCP_NODE_HOME`, `MCP_SANDBOX_HOME`, `WORKSPACE_DIR`).
2. MADO 메인 서버 프로세스를 백그라운드로 안전하게 기동합니다.
3. 보조 스크립트 `open_browser.py`를 실행하여 서버 포트가 정상 응답하는 즉시 기본 웹 브라우저를 자동 실행합니다.

**접속 URL을 스크립트에 하드코딩하지 않습니다.**

```bat
for /f "usebackq delims=" %%i in (`"%PYTHON_BIN%" -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"`) do set "APP_URL=%%i"
```

`conf.json`의 `app` 설정 블록에서 호스트와 포트를 직접 조회하여 브라우저를 엽니다. 두 곳에 동일한 포트 번호를 중복 기재하여 설정이 불일치하는 버그를 원천 차단합니다.

### 번들 패키징 시 제외되는 운영 잔재 파일

빌드 스테이징 디렉터리(`dist/MultiAgentOrchestrator_bundle/`)는 빌드 간에 유지될 수 있습니다. 만약 로컬에서 `run_mado.bat`로 테스트 실행을 했다면 데이터베이스 파일이나 테스트 산출물이 스테이징 폴더에 남아 있게 되는데, 과거에는 압축 시 폴더를 통째로 담아 불필요한 운영 데이터가 반입물에 포함되는 사고가 있었습니다.

| 제외 대상 패턴 | 제외 사유 |
| :--- | :--- |
| 최상위 디렉터리의 `*.db`, `*.db-wal`, `*.db-shm`, `*.sqlite*` | 과거 대화 이력 데이터베이스. 폐쇄망 첫 실행 시 클린 상태로 자동 생성되어야 함 |
| 최상위 디렉터리의 `.env` | 개발자 로컬 PC의 실제 API 자격증명 및 비밀키 누출 방지 |
| 최상위 디렉터리의 `*.log`, `MANIFEST.txt` | 이전 패키징 빌드 및 테스트 실행 잔재 |
| `workspace/` 내부의 작업 파일들 (`.gitkeep`, `.git/` 제외) | 로컬 테스트에서 에이전트가 생성한 임시 파일이므로 배포 번들에서 배제 |

**운영 잔재 필터링은 프로젝트 최상위 루트 디렉터리에만 적용됩니다.** 벤더링된 외부 패키지 트리(`python_runtime/`, `wheels/`, `mcp_sandbox/`, `node_runtime/`) 내부에 존재하는 `__pycache__`나 패키지 매니페스트 파일 등은 해당 런타임 패키지의 정상적인 구성품이므로 임의로 삭제하지 않고 원본 그대로 포함합니다.

단, `workspace/.git/` 디렉터리는 온전히 보존합니다. MCP git 서버는 유효한 git 저장소가 초기화되어 있지 않으면 기동 시 오류를 발생시키기 때문입니다.

어떤 파일들이 운영 잔재로 제외되었는지는 숨기지 않고 빌드 콘솔 로그에 상세히 출력됩니다:

```text
      운영 잔재 5개를 제외했습니다:
        - MANIFEST.txt
        - multiagent.db
        - workspace/.memory-graphs
        - workspace/handoff.py
        - workspace/workspace
```

---

### 오프라인 런타임 의존성 사전 검증

패키징 완료 단계에서 폐쇄망 구동에 필요한 필수 Python 모듈들의 `import` 가능 여부를 자동으로 검증하며, 단 하나라도 임포트에 실패하면 **빌드를 즉시 실패 처리**합니다.

```python
REQUIRED_IMPORTS = [
    ("nicegui", "웹 UI 프레임워크"),
    ("litellm", "LLM 호출 파이프라인"),
    ("mcp", "MCP 클라이언트"),
    ("mcp.server.fastmcp", "샌드박스 MCP 서버 (mcp 2.x에서는 제거됨 → mcp<2 필요)"),
    ("mcp_server_git", "git MCP 서버"),
    ("jupyter_client", "샌드박스 커널 통신"),
    ("ipykernel", "샌드박스 커널 실행 엔진"),
]
```

이 사전 검증 체계가 없다면, 수백 MB의 번들을 보안 구역 폐쇄망에 반입하여 실행한 뒤에야 핵심 모듈 누락 사실을 발견하고 재작업해야 하는 낭비가 발생합니다.

또한 `PIP_CONSTRAINTS` 환경을 통해 `mcp>=1.29.0,<2` 버전을 엄격히 강제합니다. 벤더링된 샌드박스의 `requirements-server.txt`에 버전 상한이 없어 그대로 둘 경우 최신 pip가 mcp 2.x를 다운로드하게 됩니다. 그런데 mcp 2.x에서는 `mcp.server.fastmcp` 모듈이 제거되어 샌드박스 서버가 기동하지 못하므로, 안전한 1.x 상한선 제약이 필수적입니다.

---

## 유지보수용 소스 갱신 패키지 (`package_source.py`)

```bash
python package_source.py [--out-dir dist] [--max-file-mb 2] [--allow-secrets]
                         [--skip-manual-html]
```

```text
MultiAgentOrchestrator_source/
├── app/                          애플리케이션 전체 소스 코드 (현장 app/ 디렉터리와 통째로 교체)
├── mcp_servers/                  포크한 MCP 서버 원본 소스
├── mcp_node/memory-scoped.mjs    메모리 서버 실행 JavaScript 사본
├── docs/                         사용 설명서 (마크다운 원본 + 파이썬 렌더러)
├── conf.example.json             신규 설정 템플릿 참조본
├── .env.example
├── requirements.txt
├── setup_mcp.py
├── open_browser.py
├── README.md
├── LICENSE.md                    오픈소스 라이선스 고지문 (LGPL-3.0 전문 및 제3자 라이선스)
└── MANIFEST.txt                  패키지 내 전 파일의 SHA-256 무결성 검증 목록
```

### 사용자 설명서(docs) 번들링 규칙

두 패키지 모두 `docs/` 디렉터리를 빠짐없이 포함합니다. 폐쇄망 환경에서는 GitHub 저장소나 온라인 위키에 접속할 수 없으므로, 모든 사용 설명서가 오프라인 설치본과 항상 함께 제공되어야 합니다.

| 패키지 종류 | 마크다운 원본 (.md) | 정적 HTML 렌더링 결과 |
| :--- | :--- | :--- |
| **전체 오프라인 번들** | 포함됨 | **빌드 시점에 사전 렌더링되어 포함됨** |
| **소스 갱신 패키지** | 포함됨 | **빌드 시점에 사전 렌더링되어 포함됨** |

폐쇄망 장비에 별도의 뷰어가 설치되어 있지 않더라도 기본 웹 브라우저로 문서를 즉시 열람할 수 있도록 `docs/user_manual_html/index.html`을 패키징 시점에 사전 렌더링하여 동봉합니다. 동시에 원본 마크다운 파일과 `render_user_manual.py` 파이썬 렌더러도 함께 포함되므로 현장에서 문서를 다시 렌더링하는 것도 가능합니다.

**개발 작업 트리에 이미 존재하는 `docs/user_manual_html/` 폴더를 그대로 긁어 담지 않습니다.** 해당 폴더가 언제 생성되었는지 검증할 수 없으므로, 마크다운 원문을 수정한 뒤 렌더러를 실행하지 않았다면 단일 패키지 내에서 마크다운 원본과 HTML 결과물의 내용이 서로 불일치하는 문제가 생길 수 있기 때문입니다. 패키징 스크립트는 항상 독립된 임시 폴더에 최신 HTML을 새로 렌더링하여 패키지에 담으며, 번들링되는 HTML 역시 다른 소스 파일과 동일하게 크기 검사 및 보안 비밀값 검사를 거쳐 `MANIFEST.txt`에 SHA-256 해시가 기록됩니다.

HTML 렌더링을 생략하려면 `--skip-manual-html` 플래그를 사용하십시오 (마크다운 원본은 정상 포함됩니다).

```bash
python docs/render_user_manual.py
```

HTML 렌더링에 예기치 못한 실패가 발생하더라도 번들 생성 자체를 중단시키지는 않습니다. 설명서는 앱 실행의 필수 바이너리가 아니며, 마크다운 원본이 이미 안전하게 포함되어 있기 때문입니다.

자체 개발된 렌더러는 오직 Python 표준 라이브러리만을 사용하며, 생성된 HTML 산출물 역시 외부 CDN이나 외부 폰트에 대한 네트워크 요청이 단 1건도 발생하지 않습니다. 오프라인 폐쇄망 환경에서의 완전한 자립 구동을 철저히 보장합니다.

---

### 허용 목록(Allow-list) 기반 수집 정책

포함 대상 파일 목록은 철저히 **허용 목록(Allow-list)** 방식으로 통제됩니다. 제외 목록(Deny-list) 방식으로 관리하면 개발 도중 신규 디렉터리나 임시 폴더가 생겼을 때 의도치 않게 패키지에 포함되는 보안 사고가 발생하기 때문입니다. 허용 목록 정책에서는 등록되지 않은 신규 폴더가 자동으로 누락되므로 개발자가 변경 사항을 즉시 인지할 수 있습니다.

### 세 가지 엄격한 빌드 거부 조건

| 거부 조건 | 사유 및 배경 |
| :--- | :--- |
| `conf.json` 파일을 패키지에 포함하지 않음 | 폐쇄망 현장 서버의 `conf.json`에는 사내망 전용 엔드포인트 URL이 기재되어 있습니다. 갱신 패키지가 이를 덮어쓰면 현장의 모든 에이전트 연결이 즉시 단절됩니다. |
| 2MB 초과 파일 발견 시 빌드 중단 | 소스 갱신 패키지에 메가바이트급 파일이 포함되어 있다면 빌드 부산물이나 대용량 바이너리가 잘못 유입된 것입니다. |
| API 키 패턴 발견 시 빌드 중단 | `conf.json`이 로컬 gitignore 대상이므로 개발자가 테스트용 실제 키를 기재해 두었을 위험을 차단합니다. |

3번째 보안 비밀값 감지는 개발 단계에서 `--allow-secrets` 플래그로 무시할 수 있으나, **보안 반입 심사 단계는 유출된 API 키가 발견되기에 가장 치명적인 자리**이므로 절대 실서버 반입 패키지에 사용해서는 안 됩니다.

자동 감지되는 패턴: OpenAI 계열(`sk-`), Anthropic(`sk-ant-`), Google Cloud(`AIza`), GitHub(`ghp_`), Slack(`xox*-`), 개인 키(Private Key) 헤더 등.

### 폐쇄망 서버에서의 소스 패키지 적용 절차

```text
1. app/ 디렉터리 통째 교체  ← 파일 단위로 덮어쓰면 이번 버전에서 삭제된 레거시 모듈이
                               남아 런타임 충돌을 유발할 수 있습니다.
2. 나머지 루트 파일 덮어쓰기
3. conf.json은 건드리지 않음 ← 패키지에 없으므로 현장 고유 설정이 안전하게 유지됩니다.
4. MANIFEST.txt로 파일 무결성 최종 검증
```

새로 추가된 설정 항목이 있다면 `conf.example.json`을 참고하여 현장의 `conf.json`에 수동으로 반영합니다.

```powershell
Get-Content MANIFEST.txt | Where-Object { $_ -notmatch '^#' } | ForEach-Object { ... }
```

---

## 단일 설정 파일로 개발/폐쇄망 2대 환경 지원

개발 PC와 폐쇄망 프로덕션 번들은 **동일한 `conf.json` 구조를 100% 공유**합니다. 모든 시스템 경로가 유연한 환경변수 치환 문법(`${VAR:-기본값}`)으로 구성되어 있기 때문입니다.

| 핵심 환경변수 | 개발 환경 기본값 | 폐쇄망 번들 환경 자동 주입값 |
| :--- | :--- | :--- |
| `NODE_BIN` | `node` | `node_runtime\node.exe` |
| `PYTHON_BIN` | `sys.executable` | `python_runtime\python.exe` |
| `MCP_NODE_HOME` | `./mcp_node` | 번들 내부의 `mcp_node` 상대 경로 |
| `MCP_SANDBOX_HOME` | `./mcp_sandbox` | 번들 내부의 `mcp_sandbox` 상대 경로 |
| `WORKSPACE_DIR` | 개발 프로젝트의 `workspace` | 번들 내부의 `workspace` 디렉터리 |

```json
"command": "${NODE_BIN:-node}",
"args": ["${MCP_NODE_HOME:-./mcp_node}/node_modules/.../dist/index.js"]
```

원클릭 실행 스크립트가 위 환경변수들을 번들 내부의 상대 경로로 자동 계산하여 프로세스에 주입하므로, **폐쇄망에 반입한 후 사용자가 설정 파일의 경로를 수동으로 수정할 필요가 전혀 없습니다.**

---

## 폐쇄망 운영 시 주의사항

| 주의 항목 | 운영 조치 및 권장 사항 |
| :--- | :--- |
| `npx` 명령어 사용 금지 | 패키지가 로컬 캐시에 없으면 공용 npm 레지스트리로 접속을 시도합니다. 진입점 JavaScript를 `node` 명령어로 직접 실행하십시오. |
| `fetch` MCP 서버 비활성화 | 외부 인터넷 아웃바운드가 열려 있는 망에서만 동작합니다. 기본 설정에서 비활성화되어 있는 이유입니다. |
| 웹 검색 MCP 서버 배제 | 외부 검색 엔진 연동 도구는 기본 구성에서 제외되어 있습니다. 필요 시 사내 구축형 SearXNG 인스턴스를 연동하십시오. |
| LLM 엔드포인트 주소 | 폐쇄망 내부의 사내 AI 게이트웨이 또는 vLLM/Ollama 로컬 서버 주소로 지정되어야 합니다. |

---

## 실행 스크립트만 재생성하는 방법

실행 스크립트는 패키징 시 생성되는 부산물이므로, 번들 전체를 재빌드할 필요 없이 스크립트만 신속히 재생성할 수 있습니다:

```bash
python package_offline.py --launchers-only <설치본 경로>
```

---

## 오픈소스 라이선스 고지 준수

`LICENSE.md` 문서는 **두 패키지 모두에 필수 포함**됩니다. MADO는 완전한 오프라인 구동을 위해 런타임 의존성 전체(포터블 CPython, `node.exe`, 오프라인 pip wheel, 커스텀 MCP 서버)를 통째로 재배포하므로, 각 오픈소스 의존성의 라이선스 고지 의무를 엄격히 준수해야 합니다:

- MADO 프로젝트 본체는 **LGPL-3.0** 라이선스를 따릅니다. 전문이 `LICENSE.md`에 포함되어 있습니다.
- 배포 번들 내에서 가장 엄격한 라이선스는 `pyzmq` 휠에 포함된 `libzmq`(LGPL-3.0) 라이브러리입니다 (`jupyter_client` 및 `ipykernel`이 샌드박스 커널 통신을 위해 사용).
- 제3자(Third-party) 의존성 라이브러리들의 원본 라이선스 문서는 각 wheel 파일의 `.dist-info/` 메타데이터 내에 온전히 동봉되어 함께 배포됩니다.

---

## 관련 문서

- [설치와 첫 실행](../02-getting-started/01-installation.md) — 개발 PC 환경 구축 가이드
- [conf.json 설정](../02-getting-started/02-configuration.md#환경변수-치환) — 환경변수 치환 규칙 및 설정 작성법
- [MCP 호스트](../03-core/04-mcp-host.md) — MCP 도구 서버 아키텍처 및 런타임 격리
- [기술 스택](../01-overview/01-tech-stack.md) — Python 단일 프로세스 아키텍처 채택 배경

---

> 다음 섹션: [레퍼런스 개관](../05-reference/README.md)
