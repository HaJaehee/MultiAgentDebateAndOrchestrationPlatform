# 에이전트 풀과 페르소나

> 상위: [핵심 기술 개관](README.md) · 이전: [설정 레이어](01-config-layer.md) · 다음: [LLM 통합](03-llm-integration.md)
>
> 관련 소스: `app/agents/base.py` · `pool.py` · `personas.py`

---

## Agent 모델

`AgentConfig`(설정 모델)에 UI 표현 속성을 결합한 것이 런타임에서 동작하는 `Agent` 인스턴스입니다.

```python
Agent.from_config(key, cfg)   # AgentConfig + 외형 스타일 → Agent 인스턴스 생성
```

| 분류 | 주요 필드 목록 |
| :--- | :--- |
| 정체성 (Identity) | `key`, `name`, `role`, `system_prompt` |
| LLM 연결 | `model`, `api_key`, `api_base`, `api_version`, `provider` |
| 샘플링 파라미터 | `temperature`, `top_p`, `max_tokens`, `max_context_window` |
| 네트워크 제어 | `timeout`, `num_retries`, `drop_params`, `extra_headers`, `extra_body` |
| 도구 권한 | `allowed_mcp_servers`, `max_tool_iterations` |
| 토론 제어 | `debate_priority`, `debate_stance` |
| 추론 확장 | `sequential_thinking` |
| 외형 (설정 원본) | `card_color`, `icon` |
| 외형 (해석된 속성) | `avatar`, `color`, `badge_color` |

### 핵심 파생 속성

```python
@property
def is_live(self) -> bool:
    if self.api_base:                       # 엔드포인트 URL이 지정된 경우
        return True
    if self.api_key and self.api_key.strip():   # API 키가 존재하는 경우
        return True
    return self.model.split("/", 1)[0] in {"ollama", "ollama_chat", "lm_studio"}
```

`is_live` 속성이 `False`인 에이전트는 자신의 발언 차례가 되었을 때 즉시 `LLMUnavailableError` 예외를 발생시킵니다. **임의의 모의(Mock) 대체 응답을 생성하지 않습니다.** 한편 `endpoint_label`은 UI 화면에 표시되는 엔드포인트 요약 정보이며, 설정되지 않았을 경우 `"no endpoint configured"`를 반환합니다.

### 카드 색상 및 아이콘 결정 규칙

`style_for_agent(key, card_color, icon)` 함수는 화면 표시에 필요한 세 가지 속성(`avatar`, `color`, `badge_color`)을 결정합니다. 처리 순서는 다음과 같습니다:

1. **에이전트 키(Key)로부터 기본 스타일을 결정합니다.** 기본 4종 에이전트(`orchestrator`, `architect`, `coder`, `critic`)는 고정된 사전 정의 스타일 표를 따르며, 사용자가 추가한 커스텀 에이전트는 색상 팔레트에서 해시값 기반으로 자동 배정됩니다.

   ```python
   CUSTOM_STYLE_PALETTE[zlib.crc32(key.encode("utf-8")) % len(CUSTOM_STYLE_PALETTE)]
   ```

   **`crc32`를 사용하는 이유**: Python 내장 `hash()` 함수는 프로세스 실행마다 무작위 시드(Hash Randomization)가 적용되어 결과가 달라집니다. 반면 에이전트 키가 동일하다면 어떤 프로세스나 실행 시점에서도 항상 동일한 색상이 유지되어야 합니다. 화면에서 추가한 에이전트들이 일괄적으로 동일한 회색 아이콘으로 표시되면 대화 피드에서 발언자를 식별하기 어려우므로, 별도의 색상을 지정하지 않더라도 키 기반으로 고유 색상이 자동 부여됩니다.

2. **`conf.json`에 명시된 사용자 정의 값이 기본값을 덮어씁니다.** `card_color`는 `#rrggbb` 형태의 HEX 코드이거나 Quasar 색상명이며, `icon`은 Material Icon 이름 또는 로컬 이미지 경로입니다. `badge_color`는 이를 일반 CSS 스타일에서 직접 사용할 수 있는 실제 색상 코드로 변환한 값이며, 테두리 등 Quasar CSS 클래스를 직접 적용하기 어려운 UI 요소에 사용됩니다.

### 이미지 아이콘 해석 및 폴백 메커니즘

`icon` 값이 이미지 경로인 경우 `resolve_agent_icon()`이 프로젝트 루트 기준으로 경로를 해석하며, 해당 파일이 실제로 디스크에 존재할 때만 `avatar` 속성에 `"img:/agent-icon?src=..."` 형식의 URL이 지정됩니다. 그 외의 모든 비정상 상황(파일 부재, 비이미지 확장자, 프로젝트 외부 디렉터리 참조 등)에서는 **1단계에서 결정된 기본 벡터 아이콘으로 안전하게 폴백(Fallback)**되며 경고 로그를 기록합니다.

이러한 폴백 로직을 `_avatar_value` 한곳으로 일원화한 이유는, UI 상에서 아바타가 빈 공간이나 깨진 이미지 엑스박스로 노출되는 것이 단순 설정 오류보다 사용자 경험을 크게 저해하기 때문입니다. 백엔드의 `/agent-icon` 엔드포인트 역시 동일한 검증을 재수행하며, 이미지 조회 실패 시 404 에러 대신 기본 로봇 SVG 아이콘을 HTTP 200으로 반환합니다. 이는 프론트엔드 화면이 이미 렌더링된 이후 파일이 디스크에서 삭제되는 예외 상황까지 대비하기 위함입니다.

---

## AgentPool

애플리케이션 프로세스 전체에서 공유되는 싱글턴(Singleton) 레지스트리입니다.

```python
pool = get_agent_pool()
pool.get("architect")            # 특정 키의 단일 에이전트 조회
pool.get_orchestrator()          # 오케스트레이터 조회 (미존재 시 RuntimeError 발생)
pool.list_all()                  # 등록된 전체 에이전트 목록 반환
pool.get_active(["coder"])       # 오케스트레이터를 항상 맨 앞에 포함하여 반환
```

`enabled: false`로 설정된 비활성 에이전트는 풀에 등록되지 않습니다.

### 제자리 갱신 (In-place Reload)

```python
def reload_agent_pool() -> AgentPool:
    pool = get_agent_pool()
    pool.agent_configs = get_config().agents
    pool.reload()          # 인스턴스를 새로 생성하지 않고 내부 내용만 교체
    return pool
```

새 `AgentPool` 객체로 전체를 교체하지 않습니다. 오케스트레이션 엔진과 백엔드 라우터들이 기존 풀 인스턴스를 메모리 참조 형태로 유지하고 있기 때문에, 객체 자체를 새로 생성하면 기존 참조를 유지하고 있던 모듈들이 갱신 이전의 구성을 계속 참조하는 결함이 발생합니다.

---

## 세션별 페르소나

`conf.json`의 에이전트 정의는 **서버 전역 기본값**에 해당합니다. 특정 대화 세션마다 서로 다른 인격과 프롬프트를 부여하기 위해 전체 서버를 재기동할 필요는 없습니다.

```text
세션 생성 ─── 첫 메시지 전 ────┬──── 첫 메시지 ────── 그 뒤 ───▶
             🟢 편집 가능       │      🔒 잠김
             (초안 저장)        │      (스냅샷 고정)
                                │
                    이 순간 모든 에이전트의
                    AgentConfig 전체가 DB로 영구 저장
```

### 편집 가능한 속성과 불가능한 속성

| 편집 가능 (세션 레벨 오버라이드) | 편집 불가 (서버 배포 설정) |
| :--- | :--- |
| `name` | `model`, `api_base`, `api_key` |
| `role` | `allowed_mcp_servers` |
| `system_prompt` | `temperature`, `max_tokens` 등 샘플링 값 |
| `card_color`, `icon` | |

운영 및 인프라 설정은 `conf.json`이 유일한 원본(Single Source of Truth)입니다. 대화의 성격을 바꾸는 핵심은 페르소나 인격이지 인프라 엔드포인트가 아니기 때문입니다.

외형 스타일은 페르소나 영역에 포함됩니다. 대화 피드에서 발언자를 시각적으로 구분하는 속성이므로 세션별로 유연하게 조정할 수 있어야 합니다. 다만 저장 시 `conf.json`에도 함께 반영됩니다 (이름, 역할, 시스템 프롬프트와 동일).

### 3단계 생애주기

**1단계 — 초안 상태 (첫 메시지 전송 전)**

페르소나 설정 페이지(`/personas/{session_id}`)에서 수정한 내용이 `session_agents` 테이블에 초안(Draft)으로 저장됩니다. 별도로 수정하지 않은 에이전트는 `conf.json`의 기본값을 그대로 참조합니다.

**2단계 — 잠금 상태 (첫 메시지 전송 시점)**

`prepare_agents_for_turn()` 함수가 **모든 활성 에이전트**에 대해 해당 시점의 유효 구성을 `session_agents`에 영구 기록하고 `sessions.personas_locked = True`로 잠금 플래그를 설정합니다. 이때 저장되는 데이터는 단순 페르소나 텍스트뿐만 아니라 모델명, 엔드포인트, API 키, 샘플링 파라미터, MCP 도구 권한까지 포함하는 `AgentConfig` 전체의 완전한 `config_snapshot`입니다.

**3단계 — 세션 재개**

이후 해당 세션을 다시 열어 토론을 이어갈 때는 데이터베이스에 저장된 스냅샷 구성을 그대로 사용합니다. 그 사이 서버의 `conf.json` 파일이 어떻게 변경되었든 기존 세션은 영향을 받지 않습니다.

### 잠금 이후 `conf.json`이 변경되었을 때의 동작

| `conf.json` 변경 내역 | 이미 시작된 대화 세션 | 아직 시작되지 않은 신규 대화 세션 |
| :--- | :--- | :--- |
| 에이전트 삭제 | 영향 없음 (스냅샷으로 계속 토론 참여) | 목록에서 즉시 제거됨 |
| 에이전트 비활성화 | 영향 없음 | 토론 참여에서 제외됨 |
| 모델·엔드포인트 변경 | 영향 없음 | 신규 설정값 적용 |
| 도구 권한 변경 | 영향 없음 | 신규 설정값 적용 |
| 신규 에이전트 추가 | 참여하지 않음 | 토론 참여 가능 |

이러한 격리 보장이 바로 [세션 스냅샷](07-persistence.md) 아키텍처가 존재하는 이유입니다.

### 예외적 구제 수단 — 설정 갱신 (Refresh Config)

스냅샷이 완전히 고정되어 있으면 난감한 상황이 발생할 수 있습니다. 예를 들어 토론 도중 외부 LLM 엔드포인트 주소가 변경되었거나 API 키가 만료된 대화 세션입니다.
로스터 UI의 **설정 갱신(Refresh Config)** 버튼을 누르면, 현재 `conf.json`의 최신 인프라 설정값으로 스냅샷을 다시 덮어씁니다. 이때 **사용자가 조율한 페르소나(이름, 역할, 프롬프트)는 전혀 건드리지 않으므로** 토론 기록 상의 발언자 정체성은 완벽히 유지됩니다. 또한 이미 전역 `conf.json`에서 삭제되었으나 해당 세션에만 남아있는 에이전트는 강제로 삭제되지 않고 보존됩니다.

### 발언자 정렬 우선순위

```text
1. 오케스트레이터 (항상 최우선 배치)
2. conf.json에 정의된 우선순위 순서
3. 해당 대화 세션에만 스냅샷으로 남아 있는 에이전트 (전역 설정에서 삭제된 항목들)
```

---

## 관련 문서

- [데이터베이스와 세션 스냅샷](07-persistence.md) — `session_agents` 테이블 스키마 상세
- [세션 생애주기](../04-workflows/02-session-lifecycle.md) — 세션 잠금 시점의 전체 라이프사이클 흐름
- [로스터 편집](../04-workflows/03-roster-editing.md) — UI에서 에이전트 풀 자체를 수정하는 방법
- [토론 전략](06-debate-strategies.md) — `debate_priority` 및 `debate_stance` 활용 방식

---

> 다음: [LLM 통합](03-llm-integration.md)
