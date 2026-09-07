# LLM 통합

> 상위: [핵심 기술 개관](README.md) · 이전: [에이전트 풀과 페르소나](02-agent-pool.md) · 다음: [MCP 호스트](04-mcp-host.md)
>
> 파일: `app/agents/llm.py` (401줄)

`LLMCaller` 는 에이전트 하나의 발언 차례를 처리합니다. LiteLLM 을 감싸고,
시스템 프롬프트를 조립하고, 도구 호출 루프를 돌리고, 실패를 정직하게 올립니다.

---

## 한 번의 발언

```text
call_agent(agent, messages, custom_instructions, session_id)
   │
   ├─ 1. 시스템 프롬프트 조립
   │      페르소나 + 단계적 사고 프로토콜 + 세션 커스텀 지침
   │
   ├─ 2. fit_context_window()      한도를 넘으면 가운데부터 덜어냄
   ├─ 3. merge_consecutive_roles() 같은 role 이 연속되면 합침
   │
   ├─ 4. 도구 목록 조회             allowed_mcp_servers → OpenAI tool 스펙
   │
   ├─ 5. is_live 확인 ─────────────▶ False 면 LLMUnavailableError
   │
   ├─ 6. _run_litellm_loop()       도구 루프 (최대 max_tool_iterations)
   │
   └─ 7. _apply_show_steps()       show_steps=false 면 사고 과정을 잘라냄
          │
          ▼
     (응답 텍스트, 도구 호출 기록)
```

---

## 시스템 프롬프트 조립

```python
def build_system_prompt(self, agent, custom_instructions=""):
    parts = [agent.system_prompt]

    st = agent.sequential_thinking
    if st.enabled and st.mode in ("prompt", "mcp"):
        parts.append(st.render_prompt())          # {max_steps} 치환
        if st.mode == "mcp":
            parts.append(f"각 사고 단계는 반드시 '{st.mcp_server}' MCP 서버의 "
                         "sequentialthinking 도구를 호출해 기록한 뒤 진행하세요.")

    if custom_instructions:
        parts.append(f"[Session Custom Instructions]:\n{custom_instructions}")

    return "\n\n".join(p for p in parts if p)
```

세 층이 쌓입니다: **에이전트 페르소나** → **사고 프로토콜** → **이 대화의 지침**.
여기에 엔진이 발언 차례마다 전략 지침(`turn_instruction`)을 덧붙입니다.

---

## 컨텍스트 창 관리

라운드가 쌓이면 전사가 그대로 길어져 한도를 넘고, 엔드포인트는
`maximum context length ... however you requested ...` 로 400 을 돌려줍니다.

```python
budget = agent.max_context_window - agent.max_tokens - 512   # 응답분 + 여유

head, tail = messages[:2], messages[-1:]   # system+목표 / 이번 차례 지시
middle = messages[2:-1]
while middle and estimate_tokens(...) > budget:
    middle.pop(0)                          # 오래된 것부터
```

**맨 앞(목표)과 맨 뒤(이번 차례 지시)는 절대 덜어내지 않습니다.** 그 사이를
오래된 것부터 버리고, 무엇이 빠졌는지 모델에게 알립니다.

```text
[앞선 발언 7건은 컨텍스트 한도로 생략되었습니다.
 남은 기록만으로 판단하고, 생략된 내용을 지어내지 마세요.]
```

이 안내가 없으면 모델이 빈 곳을 상상으로 메웁니다.

토큰 계산은 `litellm.token_counter()` 를 먼저 쓰고, 모델을 몰라 실패하면
글자 수로 어림잡습니다 (한글은 토크나이저에 따라 글자당 1~1.5 토큰이라 넉넉히).
**토큰 계산 실패가 호출 자체를 막아서는 안 됩니다.**

### 순서가 중요합니다

```python
formatted = fit_context_window(agent, formatted)      # 1. 자르고
formatted = merge_consecutive_roles(formatted)        # 2. 합친다
```

생략 안내가 `user` role 로 들어가므로, 합치기를 나중에 해야 role 교대가
보장됩니다. Anthropic 계열은 `user`/`assistant` 가 번갈아 오지 않으면 거부합니다.

---

## 도구 루프

```text
┌─────────────────────────────────────────────┐
│ iteration 1..max_tool_iterations (기본 30)   │
│                                             │
│   [예산 고지] 남은 호출 N회를 프롬프트에 주입 │
│   litellm.acompletion(messages, tools)      │
│              │                              │
│              ├─ tool_calls 없음 → 종료 ─────┼──▶ 최종 텍스트
│              │                              │
│              └─ tool_calls 있음             │
│                   │                         │
│                   ├─ MCPManager.execute_tool()  (병렬)
│                   ├─ 결과를 messages 에 추가    │
│                   └─ on_tool_call 콜백 → UI    │
└─────────────────────────────────────────────┘
              │
              ▼ 한도 소진
     budget_arbiter → 사람에게 확장 여부를 물음
              │
              ├─ 확장 승인 → 상한을 늘려 그 자리에서 계속
              │
              └─ 거절 / 무응답(3분) → tools 없이 한 판 더
                                      "지금까지 얻은 것으로 결론을 써라"
```

### 안전장치 1 — 남은 횟수를 미리 알립니다

매 판이 시작되기 전에 남은 호출 횟수를 프롬프트에 실어 줍니다
(`tool_budget_notice`). 한계에 가까워질수록 촘촘해집니다.

| 남은 횟수 | 고지 |
| :--- | :--- |
| 시작 시 | 총량 (`총 30회`) |
| 100 / 80 / 60 / 50 / 40 / 30 / 20 / 10 | 남은 횟수 + 계획 조정 요청 |
| 5 / 4 / 3 / 2 | 남은 횟수 + 마무리 준비 요청 |
| 1 | **마지막 기회** 경고 |

남은 예산을 알아야 모델이 결론을 쓸 자리를 스스로 남겨 둡니다. 사다리를 성기게
두면 "아직 여유가 있다" 고 믿은 채 탐색하다 마지막 판에서 갑자기 끊깁니다.

### 안전장치 2 — 소진해도 발언을 버리지 않습니다

예전에는 여기서 `LLMUnavailableError` 를 올렸습니다. 그러면 그때까지 스트리밍으로
흘러나온 글과 실행된 도구 관측이 **전부 실패 안내로 덮여 사라졌습니다** — 도구를
많이 쓴 긴 발언일수록 잃는 것이 컸고, 정작 원인은 엔드포인트 장애가 아니라 우리가
정한 상한이었습니다. 상한은 폭주를 막으라고 있는 것이지, 한 일을 버리라고 있는
것이 아닙니다.

지금은 `budget_arbiter` 콜백으로 사람에게 묻습니다 (엔진이 `TurnControl` 을 통해
화면까지 연결합니다 → [오케스트레이션 엔진](05-orchestration-engine.md)).

- **확장 승인** — 상한을 늘리고 같은 발언을 그 자리에서 이어 돕니다.
  하드 상한 `TOOL_ITERATION_CEILING` (130) 은 넘지 못합니다.
- **거절 / 3분 무응답** — `tools` 를 떼고 한 판 더 부릅니다. "새로 도구를 부르지
  말고 지금까지의 관측만으로 결론을 쓰라"는 지시와 함께입니다. 도구 목록을 같이
  주면 모델은 또 부르려 하고 그 호출은 버려집니다.

어느 쪽이든 지금까지의 발언과 도구 기록은 그대로 남고, 발언 끝에 왜 여기서
멈췄는지가 함께 기록됩니다. `budget_arbiter` 를 주지 않으면 (배치 실행, 테스트)
묻지 않고 곧장 마무리로 갑니다 — 물어볼 사람이 없는데 3분을 기다릴 이유가 없습니다.

---

## 컨텍스트 창 관리

포화는 **세 군데**에서 일어납니다. 셋 다 예전에는 `logger.warning` 만 남겨,
토론 기록이 사라지는 것을 보고 있는 사람이 알 방법이 없었습니다.

| 지점 | 함수 | 자르는 단위 |
| :--- | :--- | :--- |
| 발언 시작 전 | `fit_context_window()` | 메시지 (가운데부터) |
| 도구 루프 안 | `fit_tool_loop_context()` | **assistant + 딸린 tool 결과 덩어리** |
| 최종 합성 전사 | `_build_synthesis_prompt()` | 발언 (오래된 것부터) |

세 번째까지는 원래 있었지만 **두 번째가 없었습니다.** `fit_context_window()` 는
발언 시작 전 한 번만 돌고, 루프는 매 판 assistant + tool 메시지를 쌓습니다. 도구
출력은 수십 KB 가 되기도 하므로 긴 루프는 창을 넘겼고, 엔드포인트가 400 을
돌려주면 그 발언이 통째로 사라졌습니다.

### 왜 짝을 맞춰 잘라야 하는가

`tool` 메시지를 앞선 assistant(`tool_calls`) 없이 남기면 OpenAI 호환 엔드포인트가
거절합니다.

```text
messages with role 'tool' must be a response to
a preceding message with 'tool_calls'
```

메시지 하나씩 덜어내면 이 상태가 만들어집니다. **로컬 테스트는 통과하고 실제
엔드포인트에서만 400 이 납니다.** `fit_tool_loop_context()` 는 도구 호출을 여는
assistant 와 거기 딸린 tool 결과들을 한 덩어리로 묶어 오래된 것부터 덜어냅니다.
맨 앞(system + 목표)과 가장 최근 덩어리는 남깁니다 — 최근 관측이 지금 판단의
근거입니다.

### 안전장치 1 — 남은 여유를 미리 알립니다

사용률이 띠를 **처음 넘을 때만** 고지합니다 (`context_pressure_notice`).
매 판 떠들면 프롬프트만 늘어나고, 그것도 컨텍스트입니다.

| 사용률 | 고지 |
| :--- | :--- |
| 50% | 요점 위주로 쓰라고 안내 |
| 70% | 출력이 큰 도구 호출을 자제하라고 안내 |
| 85% | 곧 생략된다고 경고 + **메모리 오프로딩 지시** |
| 95% | 도구를 더 부르지 말고 지금 결론을 쓰라고 지시 |

### 안전장치 1-b — 잘리기 전에 memory 그래프로 옮기게 합니다

메모리 MCP 는 **세션 단위 스코프**입니다 (`AGENT_SCOPED_SERVERS` 에는 `sandbox`
만 있습니다). 한 대화에 그래프 하나를 전 에이전트가 공유하므로, 아키텍트가 남긴
관측을 다음 라운드의 크리틱과 최종 합성이 그대로 읽습니다.

85% 띠에서 그 에이전트가 메모리 쓰기 도구를 갖고 있으면, 고지에 "지금까지의 핵심
결론·근거·미해결 쟁점을 한 번의 `add_observations` 로 남긴 뒤 계속하라"를
덧붙입니다. 생략 안내문도 바뀝니다 — "생략되었습니다" 가 아니라 "생략되었습니다.
필요하면 `search_nodes` 로 그래프에서 찾으세요".

> **메모리에 쓴다고 컨텍스트가 그 자리에서 줄지는 않습니다.** 대화 메시지는 그대로
> 남습니다. 줄어드는 것은 **잘려도 잃지 않게 되는 것**이고, 문구도 "확보한다" 가
> 아니라 "옮겨 둔다" 로 씁니다.

감지는 서버 키(`memory`)가 아니라 **그 발언에 실제로 주어진 도구 목록**에서
이름이 `add_observations` / `create_entities` 로 끝나는 것을 찾습니다
(`memory_write_tool`). 서버를 껐거나, 연결에 실패했거나, conf.json 에서 키 이름을
바꿨을 때 없는 도구를 부르라고 시키지 않기 위해서입니다.

오프로딩도 **도구 호출이라 도구 예산을 씁니다.** 그래서 남은 호출이
`TOOL_BUDGET_FINAL_COUNTDOWN`(5회) 이하이거나 이미 95% 띠이면 지시하지 않습니다 —
결론 쓸 호출을 뺏거나, 자리가 없는데 메시지를 더 얹는 셈이 됩니다.

### 안전장치 2 — 버리기 전에 사람에게 묻습니다

기록을 실제로 잃는 첫 순간에 `context_arbiter` 로 물어봅니다.

| | 컨텍스트 상향 | 지금 마무리 | 무응답 (3분) |
| :--- | :--- | :--- | :--- |
| 진행 중인 발언 | 넓힌 창으로 이어 돎 | 도구 떼고 결론만 | 생략하고 **계속 진행** |
| 그때까지의 글·도구 기록 | 유지 | 유지 | 유지 |

무응답이 마무리가 아니라 **생략 후 진행**인 것이 도구 예산 쪽과 다릅니다. 사람이
자리에 없다고 해서 토론을 접을 이유는 없습니다.

**턴에 한 번만 묻습니다.** 라운드가 쌓이면 거의 모든 발언이 같은 벽에 부딪히므로,
첫 답(`DebateState.context_grant`)을 그 턴의 나머지에 그대로 씁니다.

> 도구 상한과 달리 컨텍스트 창은 **모델의 실제 한도를 넘겨 늘릴 수 없습니다.**
> `context_headroom()` 이 `litellm.get_model_info()` 로 실제 한도를 조회해 남은
> 여유를 구하고, 그 이상은 제안하지 않습니다. 넘겨 올리면 깔끔한 트림이
> 엔드포인트 400 으로 바뀝니다. 매핑되지 않은 모델(사설 게이트웨이, vLLM, 별칭)은
> 조회가 실패하고, 그때는 지금 값의 절반을 제안하며 **한도를 확인하지 못했다는
> 사실을 안내 줄에 함께 적습니다.**

---

도구 호출에는 `session_id` 와 발언자 키가 스코프로 함께 실립니다. 이것을
빠뜨리면 서버가 대화를 구분하지 못해 **다른 대화의 지식 그래프를 읽습니다**.
→ [MCP 호스트](04-mcp-host.md#스코프)

---

## 요청 파라미터 조립

`build_completion_kwargs()` 가 `Agent` 필드를 LiteLLM 인자로 옮깁니다.

| Agent 필드 | LiteLLM 인자 | 비고 |
| :--- | :--- | :--- |
| `model` | `model` | `<provider>/<model>` |
| `api_base` | `api_base` | 없으면 프로바이더 기본 |
| `api_key` | `api_key` | 없고 로컬이면 자리표시자 주입 |
| `provider` | `custom_llm_provider` | 강제 지정 |
| `temperature`, `top_p`, `max_tokens` | 동일 | |
| `timeout`, `num_retries` | 동일 | |
| `drop_params` | `drop_params` | 미지원 파라미터 무시 |
| `extra_headers`, `extra_body` | 동일 | 게이트웨이 인증·라우팅 |
| `sequential_thinking` (native) | `reasoning_effort` / `thinking` | 모드에 따라 |

**키 없는 로컬 엔드포인트**: vLLM 이나 LM Studio 처럼 키가 필요 없는 서버라도
LiteLLM 은 비어 있지 않은 키 인자를 요구합니다. 그래서 자리표시자
(`sk-no-key-required`)를 자동으로 넣습니다.

---

## 단계적 사고 (Sequential Thinking)

| mode | 구현 | 대상 |
| :--- | :--- | :--- |
| `prompt` | 프로토콜 문구를 시스템 프롬프트에 주입 | 모든 모델 |
| `native` | `reasoning_effort` / `thinking` 파라미터 전달 | 추론 지원 모델 |
| `mcp` | sequential-thinking MCP 도구 사용을 강제 | 해당 서버 필요 |

`show_steps: false` 면 응답에서 결론 표지(`## 최종 결론` 등)를 찾아 그 앞을
잘라냅니다. 사고 과정은 모델이 하되 피드에는 결론만 남습니다.

```python
def _apply_show_steps(self, agent, content):
    st = agent.sequential_thinking
    if not content or not st.enabled or st.show_steps:
        return content
    for marker in CONCLUSION_MARKERS:
        idx = content.find(marker)
        if idx != -1:
            return content[idx:].strip()
    return content
```

표지를 못 찾으면 원문을 그대로 둡니다 — 잘못 자르느니 다 보여줍니다.

---

## 실패 처리

`LLMUnavailableError` 하나로 모읍니다.

```python
if not agent.is_live:
    raise LLMUnavailableError(agent,
        "api_base 도 api_key 도 설정되어 있지 않습니다. "
        f"conf.json 의 llm 또는 agents.{agent.key} 에 엔드포인트를 지정하세요.")
```

발생 조건:

- `is_live` 가 False (엔드포인트도 키도 없음)
- LiteLLM 호출이 예외로 끝남 (타임아웃, 인증, 네트워크)
- 도구 루프 한도를 다 쓴 **뒤의 마무리 호출까지** 실패했고, 그때까지 나온 글도
  없음 (한도 소진 자체는 더 이상 실패가 아닙니다 → [도구 루프](#도구-루프))

엔진은 이것을 잡아 **연결 끊김 알림을 발언 자리에 기록**하고, 그 에이전트 키를
`state.failed_agent_keys` 에 넣습니다. 이 목록이 두 곳에 쓰입니다.

1. 최종 합성 프롬프트 — "이 에이전트는 발언하지 못했다" 를 알고 씀
2. `is_consensus_reached` 판정 — 실패자가 있으면 합의로 치지 않음

로그에는 진단에 필요한 것이 전부 남습니다.

```text
LLM call failed for System Architect
(model=anthropic/claude-3-5-sonnet-20241022, api_base=provider default): APIConnectionError: ...
```

---

## 관련 문서

- [MCP 호스트](04-mcp-host.md) — 도구가 실제로 실행되는 곳
- [오케스트레이션 엔진](05-orchestration-engine.md) — `call_agent` 를 부르는 쪽
- [conf.json 설정](../02-getting-started/02-configuration.md#llm--전역-llm-기본값) — 파라미터 레퍼런스

---

> 다음: [MCP 호스트](04-mcp-host.md)
