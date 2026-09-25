# 설정 레이어

> 상위: [핵심 기술 개관](README.md) · 다음: [에이전트 풀과 페르소나](02-agent-pool.md)
>
> 관련 소스: `app/config.py` (1,035줄) · 설정 작성법은 [conf.json 설정](../02-getting-started/02-configuration.md) 참고

이 레이어가 특별한 이유는 **단순히 설정을 읽기만 하지 않기 때문**입니다. UI 화면에서 에이전트를 추가하거나 발언 순서를 변경하면 그 결과가 즉시 `conf.json` 파일에 반영되어 기록됩니다. 즉, 설정 파일이 입력원이자 동시에 출력 대상이 됩니다.

---

## 읽기 파이프라인 (Read Path)

```text
conf.json
   │
   ├─ read_conf_file()      json.loads (utf-8-sig 인코딩, BOM 허용)
   │                        문법 오류 발생 시 → 줄·열 번호가 포함된 ValueError 발생
   │
   ├─ strip_comment_keys()  "//"로 시작하는 주석용 키 재귀적 제거
   │
   ├─ resolve_env_vars()    ${VAR} / ${VAR:-기본값} / 중첩 환경변수 치환
   │
   └─ RootConfig.model_validate()
          ├─ apply_llm_defaults()        llm 전역 설정을 각 agent로 상속 및 병합
          ├─ join_text_lines()           문자열 배열 목록 → 줄바꿈(\n) 결합
          └─ validate_orchestrator_exists() 오케스트레이터 필수 존재 검증
                  │
                  ▼
            RootConfig (애플리케이션 전역 싱글턴 인스턴스)
```

### 주석 처리 규칙

표준 JSON 규격에는 주석 문법이 존재하지 않습니다. MADO는 **키 이름이 `//`로 시작하는 항목을 설명용 주석**으로 간주하여 Pydantic 검증 전에 재귀적으로 제거합니다.

```python
def is_comment_key(key):
    return isinstance(key, str) and key.lstrip().startswith("//")

def strip_comment_keys(value):
    if isinstance(value, dict):
        return {k: strip_comment_keys(v) for k, v in value.items() if not is_comment_key(k)}
    if isinstance(value, list):
        return [strip_comment_keys(item) for item in value]
    return value
```

설명용 주석이 별도의 문법 요소가 아니라 **JSON 데이터 구조의 일부(Key-Value)**로 존재하므로, 파일을 읽고 다시 쓰는 과정에서도 자연스럽게 보존됩니다. 따라서 기록기(Writer)가 주석을 보존하기 위해 복잡한 파싱 로직을 유지할 필요가 없습니다.

### 환경변수 치환 메커니즘

`_substitute_env()`는 중첩된 기본값까지 정확하게 처리할 수 있도록 자체 구현된 파서입니다. 단순 정규식(Regex)으로는 `${A:-${B:-c}}`와 같이 중첩된 괄호 쌍을 완벽하게 계산하기 어렵기 때문입니다.

```text
"${APP_PORT:-${PORT:-8000}}"
   │
   ├─ APP_PORT 환경변수가 있으면 해당 값 사용
   ├─ 없으면 ${PORT:-8000} 표현식을 재귀적으로 치환
   └─ PORT 환경변수도 없으면 기본값 "8000" 사용
```

환경변수 치환 결과가 빈 문자열(`""`)이 된 항목은 **"미설정(None)"**으로 간주되어 전역 `llm` 설정값을 정상 상속받습니다. 만약 `${CODER_API_BASE}`가 설정되지 않아 빈 문자열이 되었을 때, 전역 `api_base`를 빈 값으로 덮어쓰는 오동작을 방지하기 위함입니다 (`_blank_to_none` 검증기).

### 여러 줄 텍스트 처리

`system_prompt`와 `prompt_template`은 단일 문자열뿐만 아니라 가독성을 위한 문자열 배열(문장 목록) 형태로도 입력받을 수 있습니다. 검증 단계에서 줄바꿈(`\n`)으로 자동 결합됩니다.

```python
@field_validator("system_prompt", mode="before")
def _join_prompt_lines(cls, v):
    return join_text_lines(v)   # list → "\n".join(...)
```

---

## 쓰기 파이프라인 (Write Path)

모든 설정 기록기(Writer) 함수는 **동일한 4단계 흐름**을 엄격히 준수합니다.

```text
1. 사전 검증      입력 데이터 전체 검증 (키 패턴, 진영 값, 미지의 필드 확인)
                    ↓ 검증 실패 시 즉시 중단 — 원본 파일은 변경되지 않음
2. 원문 읽기      read_conf_file() 호출 — "//" 주석과 ${VAR} 미해석 상태 원본 유지
3. 딕셔너리 수정  기존 키 대입 시 원래 위치 유지 / 새 키 추가 시 객체 끝에 배치
4. 원자적 기록    write_conf_file() 호출 — 임시 파일 생성 후 os.replace로 원자적 대체
```

```python
def write_conf_file(config_path, data):
    path = Path(config_path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)      # 기록 도중 중단되어도 손상된 불완전 설정이 남지 않음
```

### 원본 JSON을 그대로 읽어 수정하는 이유

현재 실행 중인 앱 메모리나 프론트엔드가 참조하는 값은 **이미 환경변수 치환이 완료된 상태**입니다. 만약 이 값을 그대로 파일에 다시 기록하면 다음과 같은 심각한 문제가 발생합니다:

- 이미 해석된 실제 API 키가 `conf.json` 파일에 평문(Plaintext)으로 영구 기록됩니다.
- 추후 `.env` 파일의 설정을 변경하더라도 `conf.json`에 박힌 고정값 때문에 설정이 반영되지 않습니다.
- 다른 개발 장비나 서버 환경으로 설정을 이전할 때 타인의 로컬 절대 경로가 그대로 유지되어 오류가 발생합니다.

따라서 기록기는 항상 `read_conf_file()`을 통해 환경변수(`${VAR}`) 표기가 보존된 원본 데이터를 직접 읽어 수정한 뒤 저장합니다.

### 기본값과 동일한 항목은 파일에 중복 기록하지 않습니다

새 에이전트 생성 폼 UI에서는 전역 `llm` 기본값이 미리 채워진 상태로 표시되지만, **유저가 기본값과 다르게 수정한 항목만** 파일에 기록됩니다.

```python
defaults = agent_defaults_from_llm()                     # 전역 llm 값 → 미지정 시 AgentConfig 기본값
overrides = prune_agent_overrides(submitted, defaults)   # 기본값과 동일한 항목은 사전에서 제거
```

명시적으로 기록되지 않은 설정 항목은 전역 `llm` 설정을 지속적으로 상속하므로, 추후 `.env`나 전역 설정을 변경했을 때 해당 에이전트의 동작도 자연스럽게 함께 갱신됩니다.

---

## 설정 기록기 함수 목록

| 함수명 | 역할 및 주요 동작 |
| :--- | :--- |
| `update_agent_persona_in_conf_file()` | `name`, `role`, `system_prompt`를 갱신합니다 (해당 에이전트가 없으면 신규 생성). |
| `update_agent_appearance_in_conf_file()` | `card_color`, `icon`을 갱신합니다 (빈 문자열 전달 시 항목을 삭제). |
| `add_agent_to_conf_file()` | 신규 에이전트를 설정에 추가합니다. |
| `set_agent_enabled_in_conf_file()` | 에이전트 활성/비활성 여부를 전환합니다 (오케스트레이터 비활성화는 거부). |
| `remove_agent_from_conf_file()` | 에이전트를 설정에서 삭제합니다 (오케스트레이터 삭제는 거부). |
| `set_agent_allowed_mcp_servers_in_conf_file()` | 에이전트에 허용된 MCP 서버 권한 목록을 갱신합니다. |
| `set_agent_debate_order_in_conf_file()` | 에이전트 발언 순서를 10, 20, 30 단위로 순차 재부여합니다. |
| `set_agent_debate_stance_in_conf_file()` | 에이전트의 토론 진영(`affirmative`/`negative`/`neutral` 등)을 변경합니다. |
| `add_mcp_server_to_conf_file()` | 신규 MCP 서버 설정을 추가합니다. |
| `set_mcp_server_enabled_in_conf_file()` | MCP 서버 활성 여부를 켜거나 끕니다. |
| `remove_mcp_server_from_conf_file()` | MCP 서버 설정을 삭제합니다. |

설정 변경 시 지켜지는 두 가지 핵심 공통 원칙은 다음과 같습니다:

- **항목을 삭제해도 `//` 설명 주석은 보존합니다.** 유저가 작성한 설명 문서를 임의로 삭제하지 않으며, 차후 동일한 서버나 설정을 다시 추가할 때 유용한 참고 자료가 됩니다.
- **추가 후 삭제를 거치면 파일 내용이 바이트 단위로 정확히 복원됩니다.** 회귀 테스트를 통해 이러한 가역성(Round-trip)을 철저히 검증하고 있습니다.

### 발언 순서(Priority)를 10 단위로 부여하는 이유

```python
agents[key]["debate_priority"] = (position + 1) * DEBATE_PRIORITY_STEP  # 10 단위 증분
```

발언 우선순위 간격을 10 단위로 여유 있게 배치해야, 향후 두 에이전트 사이에 새로운 에이전트를 삽입할 때 전체 목록의 우선순위를 매번 재계산하여 다시 작성하는 비용을 줄일 수 있기 때문입니다.

---

## 설정 형식을 TOML에서 JSON으로 전환한 이유

Python 표준 라이브러리에는 TOML 파서(`tomllib`)만 내장되어 있을 뿐, **TOML 라이터(Writer)**가 기본 제공되지 않습니다. 이로 인해 과거에는 파일 재기록을 위해 복잡한 줄 단위 텍스트 편집 로직에 의존해야 했습니다.

| 비교 항목 | 기존 방식 (TOML) | 현재 방식 (JSON) |
| :--- | :--- | :--- |
| 섹션 탐색 | `_find_toml_section()`을 사용하여 여러 줄 문자열 상태를 추적하며 줄 범위 계산 | `data["agents"][key]` 딕셔너리 직접 접근 |
| 값 기록 | `_toml_string/_array/_inline_table/_multiline_string/_value` 등 5종 전용 헬퍼 함수 필요 | 표준 라이브러리 `json.dumps` 직접 사용 |
| 주석 보존 | 줄 단위 텍스트를 건드리지 않는 우회 방식 사용 | 데이터 구조의 일부(`//`)로 자연스럽게 자동 보존 |
| 잠재적 위험 | `system_prompt` 내부의 `[검토 항목]` 같은 대괄호 줄을 섹션 헤더로 오독 | 구조화된 파싱으로 파싱 오류 원천 차단 |
| 내부 헬퍼 수 | 12개 | 5개 |

JSON 전환으로 얻은 또 다른 이점은 문법 오류 발생 시 **정확한 줄 번호(Line)와 열 번호(Column)**가 상세히 보고된다는 점입니다.

```text
ValueError: conf.json 의 JSON 문법이 잘못되었습니다 (줄 4, 칸 3): Expecting property name
```

수백 줄에 달하는 설정 파일에서 오류 위치를 즉시 파악할 수 있는 것은 개발 및 운영 편의성에 큰 차이를 만듭니다.

---

## 핵심 안전장치

### 현재 활성화된 설정 파일만 선별적으로 재로드합니다

```python
def reload_config_if_active(path) -> bool:
    active = active_config_path()
    if active is not None and Path(path).resolve() == active:
        get_config(reload=True, config_path=path)
        return True
    return False
```

조건 없이 무조건 재로드를 허용하면, 단위 테스트가 임시 파일을 조작했을 뿐인데 전역 싱글턴 설정이 임시 파일 내용으로 교체되는 치명적인 부작용이 발생할 수 있습니다. 이로 인해 에이전트 풀과 MCP 서버 목록 전체가 왜곡되는 현상을 방지합니다.

### Python 실행 경로 고정

```python
if not os.environ.get("PYTHON_BIN"):
    os.environ["PYTHON_BIN"] = sys.executable
```

`${PYTHON_BIN:-python}` 설정이 시스템 `PATH` 상의 기본 Python으로 치환되면, 가상환경(venv) 내에서 애플리케이션을 구동할 때 필요한 라이브러리가 없는 시스템 인터프리터로 MCP 서버가 실행되어 기동에 실패할 수 있습니다. 이를 방지하기 위해 현재 프로세스의 `sys.executable` 경로를 기본값으로 고정합니다.

### 작업 공간(Workspace) 경로의 절대 경로 변환

```python
os.environ["WORKSPACE_DIR"] = str(resolve_workspace_dir(os.environ.get("WORKSPACE_DIR")))
```

MCP의 파일시스템 서버(Node.js)와 샌드박스 서버(Python)는 각각 독립된 프로세스이므로, 상대 경로를 지정하면 각자의 작업 디렉터리(`cwd`) 기준으로 경로를 다르게 해석하는 위험이 있습니다. 초기에 절대 경로(`Path.resolve()`)로 표준화해 두면 명령행 인자나 환경변수 전달 방식에 상관없이 항상 일관된 동일 디렉터리를 참조하게 됩니다.

---

## 관련 문서

- [conf.json 설정](../02-getting-started/02-configuration.md) — 설정 항목 상세 레퍼런스
- [로스터 편집](../04-workflows/03-roster-editing.md) — 설정 기록기를 호출하는 UI 인터페이스
- [에이전트 풀과 페르소나](02-agent-pool.md) — 로드된 설정이 에이전트 인스턴스로 변환되는 과정

---

> 다음: [에이전트 풀과 페르소나](02-agent-pool.md)
