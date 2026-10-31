# 플러그인 (토론 전략 · 호스트 도구)

> 상위: [핵심 기술 개관](README.md) · 이전: [동료 자문 (peers)](10-peer-consultation.md)
>
> 관련 소스: `app/plugins.py` · `app/agents/host_tools.py` · `app/orchestration/strategies.py` · 예제: `docs/examples/plugins/`

---

앱 코드를 고치지 않고 토론 전략과 호스트 도구를 더합니다. 호스트 도구는 MCP 서버처럼 다른 프로세스에서 돌지 않고 앱 안에서 직접 실행되는 도구입니다. 내장된 `skills`, `peers`, `ask`, `tools` 도 같은 방식으로 동작합니다.

도구가 다른 프로세스에서 돌아도 된다면 [MCP 서버](04-mcp-host.md)로 만드는 편이 낫습니다. 프로세스가 분리되어 있어 앱에 영향을 덜 줍니다. 호스트 도구는 이번 발언의 에이전트와 작업 공간을 직접 봐야 할 때 씁니다.

> **플러그인은 앱과 같은 권한으로 도는 파이썬 코드입니다.** 폴더에 파일을 넣는 일은 패키지를 설치하는 일과 같은 무게입니다. 내용을 읽고 신뢰하는 코드만 넣으십시오.

---

## 예제 넣어 보기

[`docs/examples/plugins/`](../../examples/plugins/README.md) 에 복사해서 바로 쓸 수 있는 예제가 있습니다.

| 예제 | 보여 주는 것 |
| :--- | :--- |
| [`strategies/consensus_voting.py`](../../examples/plugins/strategies/consensus_voting.py) | 첫 라운드는 제안, 다음 라운드부터는 순서를 뒤집어 투표하는 전략 |
| [`host_tools/glossary.py`](../../examples/plugins/host_tools/glossary.py) | 위험 등급을 밝혀 묻지 않고 실행되는 도구, 시스템 프롬프트 지침 |
| [`host_tools/ticket.py`](../../examples/plugins/host_tools/ticket.py) | 등급을 밝히지 않아 승인 카드가 뜨는 도구, 작업 공간에 쓰기 |

**1. 파일을 복사합니다.** 프로젝트 루트에 폴더를 만들고 넣습니다.

```text
MultiAgentDebateOrchestration/
├── strategies/
│   └── consensus_voting.py
└── host_tools/
    ├── glossary.py
    └── ticket.py
```

**2. 서버를 다시 시작합니다.** 플러그인은 시작할 때 한 번만 읽습니다.

**3. 읽혔는지 확인합니다.** 로스터의 MCP 서버 칸에 `glossary` 와 `ticket` 칩이 생기고, 토론 전략 선택 상자에 `합의 투표` 가 나타납니다.

**4. 에이전트에게 도구를 줍니다.** 에이전트 카드의 `도구` 창에서 `glossary` 와 `ticket` 을 켭니다. 이 설정은 아직 시작하지 않은 대화와 앞으로 만드는 대화에 적용되므로, **새 대화를 만들어** 확인합니다.

**5. 토론을 돌립니다.** 전략을 `합의 투표` 로 고르고 최대 라운드를 2 이상으로 둡니다.

- 라운드 1은 우선순위 순서로 제안하고, 라운드 2는 반대 순서로 투표합니다.
- 모델이 `glossary__lookup` 을 부르면 묻지 않고 실행되고, 도구 카드가 남습니다.
- 모델이 `ticket__create` 를 부르면 승인 카드가 뜨고, 허용하면 작업 공간에 `tickets.md` 가 생깁니다.

도구를 실제로 부를지는 모델이 정합니다. 요청에 "용어집에서 MADO 의 뜻을 찾고 티켓을 하나 만들어 주세요" 처럼 적으면 확인하기 쉽습니다.

---

## 어디서 읽는가

| 종류 | 폴더 (기본값) | entry point 그룹 | 기반 클래스 |
| :--- | :--- | :--- | :--- |
| 토론 전략 | `plugins.strategies_dir` (`strategies/`) | `mado.strategies` | `BaseDebateStrategy` |
| 호스트 도구 | `plugins.host_tools_dir` (`host_tools/`) | `mado.host_tools` | `HostToolProvider` |

- **폴더**: `.py` 파일 하나나 패키지 폴더(`이름/__init__.py`) 하나가 플러그인 하나입니다. 그 안의 구체 클래스를 전부 등록합니다.
- **도우미 파일**: `_` 나 `.` 으로 시작하는 이름은 건너뜁니다. 여러 플러그인이 함께 쓰는 코드를 넣을 수 있습니다.
- **끄기**: 지우지 않고 끄려면 `plugins.disabled` 에 파일·폴더 이름(`.py` 제외)이나 entry point 이름을 적습니다. import 자체를 하지 않습니다.
- **반영 시점**: 스킬과 달리 실시간으로 반영하지 않습니다. 고치면 서버를 다시 시작해야 합니다.

설정 항목은 [conf.json 설정](../02-getting-started/02-configuration.md#plugins--토론-전략과-호스트-도구-플러그인)을 참조하십시오.

### 패키지로 배포하기

여러 서버에 배포할 때는 패키지의 entry point 로 등록합니다. 클래스, 객체, 모듈을 가리킬 수 있습니다. 모듈을 가리키면 그 안의 구체 클래스를 전부 등록합니다.

```toml
[project.entry-points."mado.strategies"]
consensus_voting = "my_package.voting:ConsensusVotingStrategy"

[project.entry-points."mado.host_tools"]
glossary = "my_package.glossary:GlossaryTools"
```

폐쇄망에서는 휠을 함께 반입해 설치합니다. 폴더 방식이면 반입 절차 없이 파일만 복사하면 됩니다.

### 업데이트할 때

소스 갱신 패키지는 `strategies/` 와 `host_tools/` 를 건드리지 않습니다. 폐쇄망 전체 번들을 새로 만들면 이 폴더는 담기지 않으므로 직접 옮겨야 합니다.

---

## 토론 전략 만들기

`BaseDebateStrategy` 를 상속합니다. [예제](../../examples/plugins/strategies/consensus_voting.py)를 고쳐 쓰는 것이 가장 빠릅니다.

| 항목 | 필수 | 설명 |
| :--- | :--- | :--- |
| `name` | 예 | 대화에 저장되는 이름입니다. 영문·숫자·`_`·`-` 만 씁니다. 내장 전략과 옛 전략의 이름은 쓸 수 없습니다. |
| `display_name` | 예 | 선택 상자에 보이는 이름입니다. |
| `get_speakers_for_round(active_agents, round_num, state)` | 예 | 이번 라운드의 발언자를 순서대로 돌려줍니다. |
| `turn_instruction(agent, speakers, index, state)` | 아니요 | 이 발언 차례에 덧붙일 지침입니다. |
| `orchestrator_selects_speakers` | 아니요 | `True` 면 오케스트레이터가 매 라운드 후보 중에서 발언자를 고릅니다. |
| `orchestrator_dispatches_parallel` | 아니요 | `True` 면 태스크를 나눠 주고 동시에 돌린 뒤 취합합니다. |

지켜야 할 규칙은 [토론 전략 구현 가이드](06-debate-strategies.md#신규-토론-전략-구현-가이드)에 있습니다. 전략 안에서 LLM 을 부르거나 파일을 읽지 마십시오. 라운드마다 이벤트 루프에서 불리므로 토론과 화면이 함께 멈춥니다.

---

## 호스트 도구 만들기

`HostToolProvider` 를 상속합니다. 꼭 채울 것은 `prefix`, `tails`, `tools_for`, `run` 입니다.

```python
from app.agents.host_tools import HostToolProvider


class WeatherTools(HostToolProvider):
    prefix = "weather"          # 도구 이름의 앞부분
    tails = ("today",)          # 전체 이름은 weather__today
    risk = "read"               # 도구 보안 판정에 쓰는 등급

    def tools_for(self, agent):
        return [self.definition("today", "오늘 날씨를 알려 줍니다.", {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        })]

    async def run(self, call):
        return f"{call.arguments.get('city')}: 맑음", "success"
```

### 속성과 메서드

| 항목 | 기본값 | 설명 |
| :--- | :--- | :--- |
| `prefix` | 없음 (필수) | 도구 이름의 앞부분이자 켜는 스위치입니다. 소문자로 시작하고 소문자·숫자·`-`·`_` 만 쓰며 `__` 는 넣을 수 없습니다. 내장 도구, 다른 플러그인, 등록된 MCP 서버와 겹칠 수 없습니다. |
| `tails` | 없음 (필수) | 접두사 뒤의 도구 이름들입니다. |
| `label` | 접두사로 지음 | 같은 이름의 MCP 서버를 등록하려 할 때 안내에 나옵니다. |
| `summary` | 없음 | 도구 할당 창과 칩의 설명입니다. |
| `risk` | `unknown` | 판정에 쓰는 등급입니다. 아래 [도구 보안](#도구-보안)을 참조하십시오. |
| `gated` | `True` | `False` 면 판정을 받지 않습니다. 판정 없이 실행해도 안전한 이유가 있을 때만 씁니다. |
| `assignable` | `True` | `allowed_mcp_servers` 로 켜는 도구인지 정합니다. `False` 면 할당 창에 나오지 않으므로 `enabled_for` 도 함께 고쳐 씁니다. |
| `tools_for(agent)` | 필수 | 이번 발언에 줄 도구 정의입니다. 발언마다 불리므로 가벼워야 합니다. |
| `run(call)` | 필수 | 호출 하나를 실행하고 `(결과 글, 상태)` 를 돌려줍니다. 상태는 `success` 또는 `error` 입니다. |
| `guidance(tools)` | `None` | 시스템 프롬프트에 붙일 글입니다. `tools` 에 이 도구가 없으면 `None` 을 돌려줘야 합니다. |
| `definition(tail, description, parameters)` | 제공 | 도구 정의를 짓는 도우미입니다. |
| `enabled_for(agent)` | 스위치 확인 | 이 에이전트에게 주는 도구인지 정합니다. |
| `risk_of(tool_name)` | `risk` | 도구마다 등급이 다르면 고쳐 씁니다. |

`run` 이 받는 `call` 에는 다음 값이 있습니다.

| 값 | 설명 |
| :--- | :--- |
| `call.agent` | 부른 에이전트 |
| `call.name` | 모델이 부른 도구 이름 |
| `call.arguments` | 인자 (딕셔너리). 모델이 만든 값이므로 믿지 말고 확인합니다. |
| `call.session_id` | 이번 대화의 ID |
| `call.workspace` | 이번 발언의 작업 공간. 없을 수 있습니다. |
| `call.tools` | 이번 발언에 실린 도구 정의 |

### 지켜야 할 규칙

- **객체 하나를 모든 대화가 함께 씁니다.** 대화별 상태를 속성에 두지 말고 `call.session_id` 로 구분하십시오.
- **`run` 은 이벤트 루프에서 돕니다.** 파일, 네트워크처럼 오래 걸리는 일은 `await asyncio.to_thread(...)` 로 넘기십시오. 막으면 토론과 화면이 함께 멈춥니다.
- **실패는 글로 돌려줍니다.** `("이유", "error")` 를 돌려주면 모델이 읽고 고칩니다. 예외가 나도 같은 방식으로 모델에게 전달되어 발언은 계속됩니다.
- **결과가 길면 줄어듭니다.** 다른 도구와 같은 상한으로 앞뒤만 남습니다.
- **접두사가 다른 이름의 도구는 내놓을 수 없습니다.** 버려지고 로그에 경고가 남습니다.

---

## 도구 보안

플러그인 호출은 MCP 도구처럼 [도구 보안](08-tool-security.md) 판정을 받습니다. 플러그인이 밝힌 등급으로 판정합니다.

| `risk` | 읽기 전용 | 기본 | 검토 | 자동 |
| :--- | :--- | :--- | :--- | :--- |
| `read`, `state` | 실행 | 실행 | 실행 | 실행 |
| `write`, `exec` | 거부 | 실행 | 묻기 | 실행 |
| `outside`, `delete`, `exec_flagged`, `net` | 거부 | 묻기 | 묻기 | 실행 |
| `unknown` (밝히지 않음) | 거부 | 묻기 | 묻기 | 실행 |

물을 사람이 없는 실행(헤드리스 API, 내장 MCP 서버)에서는 '묻기' 가 거부가 됩니다. 항상 거부되는 도구는 목록에서 빠져 모델에게 보이지 않습니다.

규칙은 `mcp(접두사/도구이름)` 으로 겁니다.

```json
"tool_security": {
  "deny": ["mcp(ticket/*)"],
  "allow": ["mcp(glossary/lookup)"]
}
```

**판정이 보지 못하는 것이 있습니다.** 판정은 호출을 실행할지만 정하고, 플러그인 안에서 무엇을 하는지는 보지 못합니다. `read(**/.env)` 같은 인자 규칙과 고정 보호가 걸리지 않습니다. 경로를 받는 도구를 만든다면 플러그인이 직접 막아야 합니다. [예제](../../examples/plugins/host_tools/ticket.py)처럼 경로를 인자로 받지 않는 것이 가장 안전합니다.

### 폴더 보호

- 에이전트의 파일 도구는 플러그인 폴더를 읽거나 쓸 수 없습니다. 설정으로 다른 곳을 가리켜도 같습니다.
- 기본 작업 공간 안의 폴더는 읽지 않습니다. 에이전트가 코드를 실행해 파일을 쓸 수 있는 곳이라, 그 파일이 다음 시작 때 앱 권한을 얻게 되기 때문입니다.
- 대화마다 고르는 작업 공간은 시작할 때 알 수 없어 검사하지 못합니다. 설치 폴더 밖을 가리킨다면 작업 공간으로 쓰지 않는 곳을 고르십시오.

---

## 문제 해결

읽지 못한 플러그인은 앱을 막지 않습니다. 이유는 토론 전략 선택 상자 옆의 주황색 아이콘, [`GET /api/plugins`](../05-reference/01-http-api.md), 서버 로그에서 볼 수 있습니다.

| 증상 | 원인과 해결 |
| :--- | :--- |
| 파일을 넣었는데 나타나지 않습니다 | 서버를 다시 시작했는지 확인하십시오. 이름이 `_` 로 시작하거나 `plugins.disabled` 에 있으면 읽지 않습니다. |
| `구체 클래스를 찾지 못했습니다` | 기반 클래스를 상속하지 않았거나, 필수 메서드나 `name`, `display_name` 이 빠졌습니다. |
| `이미 쓰입니다` | 내장 전략, 옛 전략 이름, 다른 플러그인과 이름이 겹칩니다. |
| `MCP 서버 이름과 겹칩니다` | `conf.json` 의 `mcp_servers` 에 같은 이름이 있습니다. 둘 중 하나를 바꾸십시오. |
| `기본 작업 공간 안의 폴더는 읽지 않습니다` | 플러그인 폴더를 작업 공간 밖으로 옮기십시오. |
| 도구를 켰는데 모델이 받지 못합니다 | 이미 시작한 대화는 첫 발언 때의 구성으로 고정됩니다. 새 대화를 만드십시오. 보안 모드에서 항상 거부되는 도구도 목록에서 빠집니다. |
| 타임라인에 `[발언자 지명 실패]` 가 나옵니다 | 전략이 예외를 냈거나 아무도 부르지 않았습니다. 그 라운드는 우선순위 순서로 진행되었습니다. |
| 플러그인을 지운 뒤 대화의 전략이 바뀌었습니다 | 모르는 전략은 기본 전략으로 돕니다. 대화는 이름을 그대로 들고 있어 플러그인을 다시 넣으면 돌아옵니다. |

---

## 관련 문서

- [토론 전략](06-debate-strategies.md) · [도구 보안](08-tool-security.md) · [스킬](09-skills.md) — 내장 호스트 도구
- [conf.json 설정](../02-getting-started/02-configuration.md#plugins--토론-전략과-호스트-도구-플러그인) · [HTTP API](../05-reference/01-http-api.md)
- 설계 근거: [ADR-042](../../../lectures/05-adr/ADR-042-plugins.md)
- 위키 (영문): [Host Tools and Plugins](../../../wiki/agents/host-tools.md)
