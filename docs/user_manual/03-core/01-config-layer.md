# 설정 레이어

> 상위: [핵심 기술 개관](README.md) · 다음: [에이전트 풀과 페르소나](02-agent-pool.md)
>
> 파일: `app/config.py` (1,035줄) · 설정 사용법은 [conf.json 설정](../02-getting-started/02-configuration.md)

이 레이어가 특별한 이유는 **읽기만 하지 않기 때문**입니다. 화면에서 에이전트를
추가하거나 발언 순서를 바꾸면 그 결과가 다시 `conf.json` 에 기록됩니다. 설정
파일이 입력이자 출력입니다.

---

## 읽기 경로

```text
conf.json
   │
   ├─ read_conf_file()      json.loads (utf-8-sig, BOM 허용)
   │                        문법 오류 → 줄·칸이 찍힌 ValueError
   │
   ├─ strip_comment_keys()  "//" 로 시작하는 키 재귀 제거
   │
   ├─ resolve_env_vars()    ${VAR} / ${VAR:-기본값} / 중첩 치환
   │
   └─ RootConfig.model_validate()
          ├─ apply_llm_defaults()        llm → 각 agent 로 상속 병합
          ├─ join_text_lines()           문자열 배열 → 줄바꿈 결합
          └─ validate_orchestrator_exists()
                  │
                  ▼
            RootConfig (앱 전역 싱글턴)
```

### 주석 규칙

JSON 에는 주석 문법이 없습니다. 이 프로젝트는 **키가 `//` 로 시작하면 설명**으로
보고 검증 전에 걷어냅니다.

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

설명이 **데이터의 일부**이기 때문에, 읽고 다시 쓰는 것만으로 보존됩니다.
기록기가 주석을 지키려고 따로 애쓸 필요가 없습니다.

### 환경변수 치환

`_substitute_env()` 는 중첩된 기본값까지 처리하는 직접 구현 파서입니다.
정규식으로는 `${A:-${B:-c}}` 의 괄호 짝을 셀 수 없기 때문입니다.

```text
"${APP_PORT:-${PORT:-8000}}"
   │
   ├─ APP_PORT 있으면 그 값
   ├─ 없으면 ${PORT:-8000} 을 다시 치환
   └─ 그것도 없으면 "8000"
```

빈 값으로 풀린 항목은 **"미설정"** 으로 간주되어 `llm` 에서 상속합니다.
`${CODER_API_BASE}` 가 빈 문자열이 되었다고 전역 `api_base` 를 빈 값으로
덮어쓰면 안 되기 때문입니다 (`_blank_to_none` 검증기).

### 여러 줄 텍스트

`system_prompt` 와 `prompt_template` 은 문자열 또는 문자열 배열을 받습니다.

```python
@field_validator("system_prompt", mode="before")
def _join_prompt_lines(cls, v):
    return join_text_lines(v)   # list → "\n".join(...)
```

---

## 쓰기 경로

모든 기록기가 **같은 네 단계**입니다.

```text
1. 검증        입력을 먼저 전부 확인 (키 패턴, 진영 값, 미지의 필드)
                  ↓ 실패하면 여기서 끝 — 파일에 닿지 않음
2. 원문 읽기    read_conf_file()  ← "//" 주석과 ${VAR} 미해석 상태 그대로
3. 딕셔너리 수정  기존 키에 대입 = 위치 유지 / 새 키 = 해당 객체 끝에 추가
4. 한 번에 쓰기  write_conf_file()  ← 임시 파일 + os.replace (원자적)
```

```python
def write_conf_file(config_path, data):
    path = Path(config_path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)      # 쓰다 죽어도 반쪽짜리 설정이 남지 않음
```

### 왜 원문을 읽는가

화면이 보는 값은 **이미 환경변수가 풀린 값**입니다. 그것을 되쓰면:

- 해석된 API 키가 `conf.json` 에 평문으로 박힙니다
- `.env` 를 바꿔도 따라오지 않게 됩니다
- 다른 기계에서 열면 남의 절대 경로가 들어 있습니다

그래서 기록기는 언제나 `read_conf_file()` 이 돌려준 원문을 손봅니다.

### 미리 채운 기본값은 되쓰지 않는다

새 에이전트 폼은 `llm` 기본값으로 미리 채워져 뜨지만, **사용자가 실제로 바꾼
항목만** 파일에 적힙니다.

```python
defaults = agent_defaults_from_llm()          # llm 값 → 없으면 AgentConfig 기본값
overrides = prune_agent_overrides(submitted, defaults)   # 같은 값은 제거
```

적히지 않은 항목은 계속 `llm` 을 상속하므로, `.env` 를 바꾸면 이 에이전트도
함께 따라갑니다.

---

## 기록기 목록

| 함수 | 하는 일 |
| :--- | :--- |
| `update_agent_persona_in_conf_file()` | name / role / system_prompt 갱신 (없으면 생성) |
| `update_agent_appearance_in_conf_file()` | card_color / icon 갱신 (빈 값이면 항목을 지움) |
| `add_agent_to_conf_file()` | 새 에이전트 추가 |
| `set_agent_enabled_in_conf_file()` | 활성/비활성 (오케스트레이터는 거부) |
| `remove_agent_from_conf_file()` | 에이전트 삭제 (오케스트레이터는 거부) |
| `set_agent_allowed_mcp_servers_in_conf_file()` | 도구 권한 교체 |
| `set_agent_debate_order_in_conf_file()` | 발언 순서를 10, 20, 30… 으로 재부여 |
| `set_agent_debate_stance_in_conf_file()` | 토론 진영 변경 |
| `add_mcp_server_to_conf_file()` | MCP 서버 추가 |
| `set_mcp_server_enabled_in_conf_file()` | MCP 서버 on/off |
| `remove_mcp_server_from_conf_file()` | MCP 서버 삭제 |

공통 규칙 두 가지:

- **삭제해도 `//` 설명은 남깁니다.** 사람이 쓴 글을 지우는 것은 되돌릴 수 없고,
  같은 서버를 다시 추가할 때 그대로 쓸 수 있는 정보입니다
- **추가 후 삭제하면 파일이 바이트 단위로 같아집니다.** 테스트가 이 왕복을 검증합니다

### 발언 순서에 10씩 주는 이유

```python
agents[key]["debate_priority"] = (position + 1) * DEBATE_PRIORITY_STEP  # 10
```

사이에 자리를 남겨 두어야 나중에 한 명을 둘 사이에 끼워 넣을 때 나머지를 다시
쓰지 않습니다.

---

## 왜 TOML 에서 JSON 으로 옮겼는가

파이썬 표준 라이브러리에는 TOML **리더**(`tomllib`)는 있어도 **라이터**가 없습니다.
그래서 되쓰는 코드가 줄 단위 편집이었습니다.

| | TOML 시절 | 지금 (JSON) |
| :--- | :--- | :--- |
| 섹션 찾기 | `_find_toml_section()` — 여러 줄 문자열 상태를 추적하며 줄 범위 계산 | `data["agents"][key]` |
| 값 쓰기 | `_toml_string/_array/_inline_table/_multiline_string/_value` 5종 | `json.dumps` |
| 주석 보존 | 줄을 건드리지 않는 방식으로 우회 | 데이터의 일부라 자동 |
| 위험 | `system_prompt` 의 `[검토 항목]` 줄을 섹션 헤더로 오독 | 없음 |
| 내부 헬퍼 | 12개 | 5개 |

부수적으로 얻은 것: 문법 오류가 **줄 번호와 칸**과 함께 보고됩니다.

```text
ValueError: conf.json 의 JSON 문법이 잘못되었습니다 (줄 4, 칸 3): Expecting property name
```

16KB 짜리 설정 파일에서 이것이 있고 없고는 큰 차이입니다.

---

## 안전장치

### 활성 설정만 다시 읽기

```python
def reload_config_if_active(path) -> bool:
    active = active_config_path()
    if active is not None and Path(path).resolve() == active:
        get_config(reload=True, config_path=path)
        return True
    return False
```

조건 없이 다시 읽으면, 테스트가 임시 파일을 고쳤을 뿐인데 전역 설정이 그
파일로 갈아끼워집니다. 에이전트 풀과 MCP 서버 목록이 통째로 바뀝니다.

### 파이썬 실행기 고정

```python
if not os.environ.get("PYTHON_BIN"):
    os.environ["PYTHON_BIN"] = sys.executable
```

`${PYTHON_BIN:-python}` 이 PATH 의 python 으로 풀리면, 앱이 가상환경에서 돌 때
의존성이 없는 다른 인터프리터로 MCP 서버가 떠서 기동에 실패합니다.

### 작업 공간 절대 경로화

```python
os.environ["WORKSPACE_DIR"] = str(resolve_workspace_dir(os.environ.get("WORKSPACE_DIR")))
```

filesystem(node)과 sandbox(python)는 다른 프로세스이고 각자의 cwd 로 상대 경로를
풉니다. 한 번 절대 경로로 만들어 두면 argv 로 가든 env 로 가든 같은 곳을 봅니다.

---

## 관련 문서

- [conf.json 설정](../02-getting-started/02-configuration.md) — 설정 항목 레퍼런스
- [로스터 편집](../04-workflows/03-roster-editing.md) — 이 기록기들을 부르는 화면
- [에이전트 풀과 페르소나](02-agent-pool.md) — 읽어 들인 설정이 Agent 가 되는 곳

---

> 다음: [에이전트 풀과 페르소나](02-agent-pool.md)
