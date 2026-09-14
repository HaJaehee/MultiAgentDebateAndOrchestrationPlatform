"""다이어그램 문법이 틀리면 오케스트레이터가 그 자리에서 고쳐 낸다.

증상: 오케스트레이터가 라운드 끝에 그리는 Mermaid 다이어그램이 자주 문법 오류로
렌더링되지 않았습니다. 그 다이어그램은 그대로 아티팩트가 되고, 사람은 탭을
열었을 때 비로소 오류를 봅니다 — 그때는 토론이 이미 끝나 고칠 사람이 없습니다.

이제 합성 직후에 검사하고, 틀렸으면 오류 메시지와 문제가 된 줄을 **쓴 사람에게
그대로 돌려주어** 고쳐 받습니다.

## 린터의 판정 기준은 실제 렌더러로 맞췄습니다

`app/mermaid_lint.py` 의 규칙은 브라우저의 진짜 `mermaid.parse()` 를 오라클로
삼아 골랐습니다. 아래 `RENDERS` / `FAILS` 는 그 판정 결과를 그대로 옮긴 것입니다.

핵심 규칙: **놓치는 것보다 잘못 잡는 것이 나쁘다.**

* 놓친 오류 = 지금과 같음 (사람이 화면에서 봅니다).
* 잘못 잡은 오류 = 멀쩡한 그림을 두고 LLM 을 다시 부릅니다. 토큰과 시간이
  나가고, 고칠 것이 없는 모델이 멀쩡한 그림을 망칩니다.

그래서 `RENDERS` 에 대해서는 **단 하나도 지적하면 안 됩니다.**
"""

from typing import Any, Dict, List

import pytest

from app.agents.base import Agent
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.mermaid_lint import format_issues, lint_mermaid
from app.orchestration.engine import (
    MERMAID_REPAIR_ATTEMPTS,
    OrchestratorEngine,
    find_mermaid_blocks,
    normalize_mermaid,
)
from app.orchestration.state import DebateState


# --------------------------------------------------------------------- 오라클
#
# 실제 mermaid@10 의 `mermaid.parse()` 가 통과시킨 것들.

RENDERS = {
    "flowchart-basic": "graph TD\n    A[Start] --> B[End]",
    "edge-label": 'graph TD\n    A -->|"HTTPS / WSS"| B',
    "edge-label-unquoted": "graph TD\n    A -->|HTTPS / WSS| B",
    "shapes": "graph TD\n    A[(DB)] --> B[[Sub]]\n    C{Dec} --> D((Circle))\n    E[/Para/] --> F[\\Rev\\]",
    "subgraph": "graph TD\n  subgraph S[그룹]\n    A --> B\n  end\n  B --> C",
    "quoted-parens": 'graph TD\n    A["결제 (PG)"] --> B',
    "br-tag": 'graph TD\n    A["줄1<br/>줄2"] --> B',
    "class-generics": "classDiagram\n    class Order {\n        +List~Item~ items\n        +total() Decimal\n    }\n    Order --> Item",
    "sequence": "sequenceDiagram\n    actor C as 고객\n    participant S as 서버\n    C->>S: 주문\n    S-->>C: 확인",
    "sequence-alt": "sequenceDiagram\n    A->>B: x\n    alt 성공\n        B-->>A: ok\n    else 실패\n        B-->>A: no\n    end",
    "sequence-paren-alias": "sequenceDiagram\n    actor C as 고객 (앱)\n    C->>S: x",
    "state": "stateDiagram-v2\n    [*] --> Idle\n    Idle --> Run\n    Run --> [*]",
    "er-brace": "erDiagram\n    ORDER ||--o{ ITEM : contains\n    ITEM }o--|| PRODUCT : refs",
    "comment": "graph TD\n    %% 주석입니다\n    A --> B",
    "semicolons": "graph TD;\n    A-->B;\n    B-->C;",
    "emoji-label": "graph TD\n    A[📦 주문 서비스] --> B[💳 결제]",
    "pie": 'pie title 비율\n    "A" : 40\n    "B" : 60',
    "header-only": "graph TD",
    "markdown-bold": "graph TD\n    **A** --> B",
    "triple-dash-label": "graph TD\n    A ---|text| B",
    "dash-text-arrow": "graph TD\n    A -- text --> B",
    "uppercase-END": "graph TD\n    start --> END\n    END --> done",
    "hexagon": "graph TD\n    A{{육각}} --> B",
    "class-brackets": "classDiagram\n    class A {\n        +int[] xs\n    }",
    "multiline-label": 'graph TD\n    A["첫줄\n두번째"] --> B',
    "journey": "journey\n    title 여정\n    section 시작\n      로그인: 5: 사용자",
    "gantt": "gantt\n    title 일정\n    section A\n    작업1 :a1, 2024-01-01, 30d",
    "amp-label": "graph TD\n    A[인증 & 세션] --> B",
    "colon-label": "graph TD\n    A[주의: 여기] --> B",
    "cylinder-korean": "graph TD\n    PG[(🏦 외부 결제 PG사 API)] --> B",
    # v0.8.1 — flowchart 에서 시퀀스 키워드가 **노드 이름**으로 쓰인 경우는 정상.
    # (NiceGUI 동봉 mermaid 의 parse() 로 확인)
    "kw-note-as-node": "flowchart LR\n  Note --> B\n  Note[메모] --> C",
    "kw-note-alone": "flowchart LR\n  Note\n  Note --> B",
    "kw-participant-arrow": "flowchart LR\n  participant --> B",
    "kw-loop-arrow": "flowchart LR\n  A --> B\n  loop --> C",
    "kw-opt-label": "flowchart LR\n  A --> B\n  opt[옵션] --> C",
    "kw-par-amp": "flowchart LR\n  A --> B\n  par & A --> C",
    "kw-alt-alone": "flowchart LR\n  A --> B\n  alt",
    "kw-rect-alone": "flowchart LR\n  A --> B\n  rect",
    "kw-note-dot": "flowchart LR\n  A --> B\n  note.x --> B",
    "kw-activate-semicolon": "flowchart LR\n  A --> B\n  activate;",
    "kw-autonumber": "flowchart LR\n  A --> B\n  autonumber",
    "flow-cross-edge": "flowchart LR\n  A --x B",
    "state-note": "stateDiagram-v2\n  A --> B\n  note right of A: x",
    "class-note": 'classDiagram\n  class A\n  note for A "x"',
    "sequence-note": "sequenceDiagram\n  participant V as Validator\n  V->>L: 호출\n  Note right of V: 최대 2회",
    # 기계적 수선이 만든 모양 — 그 자체로 통과해야 합니다.
    "converted-note": (
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["설명: A와 B"]\n  B -.- mado_note_1\n'
        "  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote"
    ),
}

# 실제 `mermaid.parse()` 가 거부한 것들 중, 린터가 잡기로 한 것.
FAILS = {
    "unbalanced-bracket": ("graph TD\n    A[결제 서비스 --> B[주문]", "bracket-balance"),
    "brace-unbalanced": ("graph TD\n    A{결정 --> B", "bracket-balance"),
    "unclosed-subgraph": ("graph TD\n  subgraph S\n    A --> B", "subgraph-unclosed"),
    "stray-end": ("graph TD\n    A --> B\n    end", "stray-end"),
    "no-header": ("    A --> B\n    B --> C", "no-header"),
    "empty": ("", "empty"),
    "end-as-node": ("graph TD\n    start --> end\n    end --> done", "end-as-node"),
    "class-unclosed": ("classDiagram\n    class Order {\n        +String id", "block-unclosed"),
    "sequence-bad-arrow": ("sequenceDiagram\n    A ==> B: 주문", "bad-sequence-arrow"),
    "paren-in-edge-label": ("graph TD\n    A -->|결제 (PG)| B", "paren-in-label"),
    "paren-in-round-node": ("graph TD\n    A(결제 (PG)) --> B", "paren-in-label"),
    "paren-in-brace-node": ("graph TD\n    A{결제 (PG)} --> B", "paren-in-label"),
    "nested-quotes": ('graph TD\n    A["그는 "안녕" 이라 했다"] --> B', "nested-quotes"),
    # v0.8.1 — flowchart 에 섞인 시퀀스 다이어그램 문법. 실제 오류 보고:
    # "Parse error on line 46: 실패 시| LLM Note right of Validator: Expecting 'SEMI', ... got 'NODE_STRING'"
    # 노트는 기계적 수선이 먼저 고치므로, 여기에는 수선이 손대지 못하는 모양만 둡니다.
    "seq-note-spaced-target": ("flowchart LR\n  A --> B\n  Note over 사용자 서비스: x", "sequence-syntax-in-flowchart"),
    "seq-participant": ("flowchart LR\n  participant A\n  A --> B", "sequence-syntax-in-flowchart"),
    "seq-participant-as": ("flowchart LR\n  A --> B\n  participant V as Validator", "sequence-syntax-in-flowchart"),
    "seq-actor": ("flowchart LR\n  actor User\n  User --> B", "sequence-syntax-in-flowchart"),
    "seq-activate": ("flowchart LR\n  A --> B\n  activate A", "sequence-syntax-in-flowchart"),
    "seq-deactivate": ("flowchart LR\n  A --> B\n  deactivate A", "sequence-syntax-in-flowchart"),
    "seq-loop": ("flowchart LR\n  A --> B\n  loop 재시도\n  A --> C\n  end", "sequence-syntax-in-flowchart"),
    "seq-loop-english": ("flowchart LR\n  A --> B\n  LOOP retry", "sequence-syntax-in-flowchart"),
    "seq-loop-node-arrow": ("flowchart LR\n  A --> B\n  loop A --> B", "sequence-syntax-in-flowchart"),
    "seq-alt": ("flowchart LR\n  alt 성공\n  A --> B\n  end", "sequence-syntax-in-flowchart"),
    "seq-opt": ("flowchart LR\n  A --> B\n  opt 캐시 있음", "sequence-syntax-in-flowchart"),
    "seq-opt-colon": ("flowchart LR\n  A --> B\n  opt: x", "sequence-syntax-in-flowchart"),
    "seq-par": ("flowchart LR\n  A --> B\n  par 병렬", "sequence-syntax-in-flowchart"),
    "seq-critical": ("flowchart LR\n  A --> B\n  critical 중요", "sequence-syntax-in-flowchart"),
    "seq-break": ("flowchart LR\n  A --> B\n  break 실패", "sequence-syntax-in-flowchart"),
    "seq-rect": ("flowchart LR\n  A --> B\n  rect rgb(0,0,0)", "sequence-syntax-in-flowchart"),
    "seq-else": ("flowchart LR\n  A --> B\n  else 다른 경우", "sequence-syntax-in-flowchart"),
    "seq-and": ("flowchart LR\n  A --> B\n  and B --> C", "sequence-syntax-in-flowchart"),
    "seq-arrow": ("flowchart LR\n  A->>B: 호출", "sequence-syntax-in-flowchart"),
    "seq-arrow-dashed": ("flowchart LR\n  A -->> B", "sequence-syntax-in-flowchart"),
    "seq-arrow-x": ("flowchart LR\n  A -x B", "sequence-syntax-in-flowchart"),
    "seq-arrow-paren": ("flowchart LR\n  A -) B", "sequence-syntax-in-flowchart"),
}


# --------------------------------------------------------------------- v0.8.1 노트 기계적 수선


def test_the_reported_note_in_a_flowchart_is_fixed_without_an_llm():
    raw = (
        "flowchart TD\n  Validator{검증} -->|성공| Out[결과]\n  Validator -->|실패 시| LLM\n"
        "  Note right of Validator: 최대 2회 재시도 (백오프)"
    )
    assert lint_mermaid(raw), "원문은 검사에 걸려야 합니다 (실제 렌더러가 거부)"
    fixed = normalize_mermaid(raw)
    assert 'Validator -.- mado_note_1["최대 2회 재시도 (백오프)"]' in fixed
    assert "Note right of" not in fixed
    assert "class mado_note_1 madoNote" in fixed
    assert lint_mermaid(fixed) == []


def test_note_over_several_nodes_links_each_and_keeps_indentation():
    raw = 'graph LR\n  A --> B\n  subgraph S\n    B --> C\n    note over B,C: "묶음" 설명\n  end'
    fixed = normalize_mermaid(raw)
    assert "    B -.- mado_note_1[\"'묶음' 설명\"]" in fixed, "안쪽 따옴표는 겹치지 않게 바꿉니다"
    assert "    C -.- mado_note_1" in fixed
    assert lint_mermaid(fixed) == []


def test_a_note_without_text_is_dropped_and_unclear_targets_are_left_for_repair():
    assert "Note" not in normalize_mermaid("flowchart LR\n  A --> B\n  Note left of A")
    spaced = normalize_mermaid("flowchart LR\n  A --> B\n  Note over 사용자 서비스: x")
    assert "Note over 사용자 서비스: x" in spaced


def test_notes_are_only_converted_in_flowcharts():
    seq = "sequenceDiagram\n  A->>B: hi\n  Note right of A: x"
    state = "stateDiagram-v2\n  A --> B\n  note right of A: x"
    assert normalize_mermaid(seq) == seq
    assert normalize_mermaid(state) == state


def test_the_repair_message_suggests_a_dotted_node_for_notes():
    issues = lint_mermaid("flowchart LR\n  A --> B\n  Note over 사용자 서비스: x")
    assert any("-.-" in i.message for i in issues)


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_never_flags_a_diagram_that_actually_renders(name):
    """거짓 양성 0. 이 목록은 실제 mermaid.parse() 가 통과시킨 것들입니다."""
    issues = lint_mermaid(normalize_mermaid(RENDERS[name]))
    assert issues == [], f"{name}: 멀쩡한 다이어그램을 지적했습니다 -> {format_issues(issues)}"


@pytest.mark.parametrize("name", sorted(FAILS))
def test_catches_the_errors_it_claims_to_catch(name):
    code, expected_rule = FAILS[name]
    rules = {i.rule for i in lint_mermaid(normalize_mermaid(code))}
    assert expected_rule in rules, f"{name}: {expected_rule} 을 놓쳤습니다 (잡은 것: {rules})"


def test_normalize_already_fixes_the_most_common_error():
    """`A[결제 (PG)]` 는 린터에 오기 전에 `normalize_mermaid` 가 고칩니다."""
    raw = "graph TD\n    A[결제 (PG) 서비스] --> B[주문]"
    assert lint_mermaid(raw), "고치기 전에는 오류여야 합니다"
    assert lint_mermaid(normalize_mermaid(raw)) == [], "정규화 후에는 통과해야 합니다"


def test_issue_message_tells_the_model_where_and_what():
    """모델이 고치려면 위치와 이유가 있어야 합니다."""
    issues = lint_mermaid("graph TD\n  subgraph S\n    A --> B")
    text = format_issues(issues)
    assert "subgraph" in text and "end" in text


# ------------------------------------------------------------ 블록 찾기·끼워넣기


REPORT = """# 보고서

본문입니다.

```mermaid
graph TD
    A[결제 (PG] --> B
```

가운데 설명.

```python
print("코드는 건드리지 않습니다")
```

```mermaid
sequenceDiagram
    A ==> B: 주문
```
"""


def test_finds_mermaid_blocks_with_positions():
    blocks = find_mermaid_blocks(REPORT)
    assert len(blocks) == 2
    assert blocks[0]["code"].startswith("graph TD")
    assert blocks[1]["code"].startswith("sequenceDiagram")
    # 위치가 맞아야 제자리에 끼워 넣을 수 있습니다.
    for b in blocks:
        assert REPORT[b["start"]:b["end"]] == b["code"]


def test_python_blocks_are_left_alone():
    assert all("print(" not in b["code"] for b in find_mermaid_blocks(REPORT))


def test_splice_replaces_in_place_and_keeps_the_rest():
    blocks = find_mermaid_blocks(REPORT)
    broken = [(i, b, []) for i, b in enumerate(blocks)]
    out = OrchestratorEngine._splice_blocks(
        REPORT, broken, ["graph TD\n    A --> B", "sequenceDiagram\n    A->>B: 주문"]
    )
    assert "본문입니다." in out and "가운데 설명." in out
    assert 'print("코드는 건드리지 않습니다")' in out
    codes = [b["code"] for b in find_mermaid_blocks(out)]
    assert codes == ["graph TD\n    A --> B", "sequenceDiagram\n    A->>B: 주문"]


def test_splice_with_fewer_replacements_keeps_what_it_got():
    """모델이 하나를 빠뜨렸다고 고친 나머지까지 버릴 이유는 없습니다."""
    blocks = find_mermaid_blocks(REPORT)
    broken = [(i, b, []) for i, b in enumerate(blocks)]
    out = OrchestratorEngine._splice_blocks(REPORT, broken, ["graph TD\n    A --> B"])
    codes = [b["code"] for b in find_mermaid_blocks(out)]
    assert codes[0] == "graph TD\n    A --> B"
    assert codes[1].startswith("sequenceDiagram")


# ------------------------------------------------------------------ 수리 루프


def _engine(replies: List[str]) -> OrchestratorEngine:
    """`replies` 를 순서대로 돌려주는 수리 담당 LLM 을 단 엔진."""
    pool = AgentPool({
        key: AgentConfig(name=key.title(), role="R", model="fake/model", api_key="k")
        for key in ("orchestrator", "architect")
    })
    engine = OrchestratorEngine(agent_pool=pool)

    class _Fixer:
        def __init__(self):
            self.calls: List[str] = []
            self.remaining = list(replies)

        async def call_agent(self, agent, messages, custom_instructions="", **kwargs):
            self.calls.append(messages[-1]["content"])
            return (self.remaining.pop(0) if self.remaining else ""), []

    engine.llm_caller = _Fixer()
    return engine


def _state() -> DebateState:
    return DebateState(session_id="s", user_prompt="p", strategy="sequential_debate",
                       max_rounds=1, current_round=1)


async def _repair(engine: OrchestratorEngine, text: str, events: List[Dict[str, Any]]):
    async def on_event(e):
        events.append(e)

    return await engine._repair_mermaid_blocks(
        text,
        agent=engine.agent_pool.get_orchestrator(),
        custom_instructions="",
        state=_state(),
        on_event=on_event,
    )


@pytest.mark.asyncio
async def test_a_clean_report_is_never_sent_back_for_repair():
    """멀쩡하면 LLM 을 부르지 않습니다. 거짓 양성의 대가가 여기서 나옵니다."""
    engine = _engine([])
    text = "# 보고서\n\n```mermaid\ngraph TD\n    A --> B\n```\n"
    out = await _repair(engine, text, [])
    assert out == text
    assert engine.llm_caller.calls == []


@pytest.mark.asyncio
async def test_a_report_without_diagrams_is_untouched():
    engine = _engine([])
    text = "# 보고서\n\n```python\nprint(1)\n```\n"
    assert await _repair(engine, text, []) == text
    assert engine.llm_caller.calls == []


@pytest.mark.asyncio
async def test_a_broken_diagram_is_fixed_and_spliced_back():
    engine = _engine(["```mermaid\ngraph TD\n    A --> B\n```"])
    events: List[Dict[str, Any]] = []
    text = "# 보고서\n\n앞말\n\n```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```\n\n뒷말\n"

    out = await _repair(engine, text, events)

    assert lint_mermaid(normalize_mermaid(find_mermaid_blocks(out)[0]["code"])) == []
    assert "앞말" in out and "뒷말" in out, "보고서 본문은 그대로여야 합니다"
    assert [e["type"] for e in events] == ["mermaid_repair_started", "mermaid_repair_finished"]
    assert events[-1]["resolved"] is True


@pytest.mark.asyncio
async def test_the_repair_prompt_carries_the_error_and_the_original():
    """모델이 디버깅하려면 무엇이 왜 틀렸는지 알아야 합니다."""
    engine = _engine(["```mermaid\ngraph TD\n    A --> B\n```"])
    await _repair(engine, "```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```", [])

    prompt = engine.llm_caller.calls[0]
    assert "subgraph" in prompt, "원본이 들어가야 합니다"
    assert "닫히지 않았습니다" in prompt, "렌더러의 지적이 들어가야 합니다"
    assert "보고서 본문은 다시 쓰지 마세요" in prompt, "전체를 다시 쓰게 하면 결론이 흔들립니다"


@pytest.mark.asyncio
async def test_it_retries_and_gives_up_without_destroying_the_original():
    """끝내 못 고치면 원문을 그대로 둡니다. 지어낸 그림으로 바꾸지 않습니다."""
    broken = "```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```"
    # 두 번 다 여전히 깨진 답을 돌려줍니다.
    engine = _engine([broken, broken])
    events: List[Dict[str, Any]] = []

    out = await _repair(engine, broken, events)

    assert len(engine.llm_caller.calls) == MERMAID_REPAIR_ATTEMPTS
    assert lint_mermaid(normalize_mermaid(find_mermaid_blocks(out)[0]["code"])), "여전히 깨진 채"
    finished = [e for e in events if e["type"] == "mermaid_repair_finished"][0]
    assert finished["resolved"] is False
    assert finished["remaining"] == 1


@pytest.mark.asyncio
async def test_the_second_attempt_says_it_is_the_second_attempt():
    broken = "```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```"
    engine = _engine([broken, "```mermaid\ngraph TD\n    A --> B\n```"])
    await _repair(engine, broken, [])
    assert "2번째 시도" in engine.llm_caller.calls[1]


@pytest.mark.asyncio
async def test_an_empty_repair_answer_stops_the_loop():
    """도구도 못 쓰는 수리 호출이 빈 답을 주면 더 물어봐야 소용없습니다."""
    broken = "```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```"
    engine = _engine(["설명만 하고 다이어그램은 안 줌"])
    out = await _repair(engine, broken, [])
    assert out == broken
    assert len(engine.llm_caller.calls) == 1


@pytest.mark.asyncio
async def test_only_the_broken_diagram_is_replaced():
    """멀쩡한 다이어그램은 손대지 않습니다."""
    good = "graph TD\n    OK1 --> OK2"
    text = (
        f"```mermaid\n{good}\n```\n\n"
        "```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```\n"
    )
    engine = _engine(["```mermaid\ngraph TD\n    FIXED --> B\n```"])
    out = await _repair(engine, text, [])

    codes = [b["code"] for b in find_mermaid_blocks(out)]
    assert codes[0] == good, "멀쩡한 것은 그대로"
    assert "FIXED" in codes[1]
    assert "고쳐서 다시 주세요" in engine.llm_caller.calls[0]
    assert "OK1" not in engine.llm_caller.calls[0], "멀쩡한 것은 프롬프트에도 넣지 않습니다"


@pytest.mark.asyncio
async def test_repair_uses_a_copy_with_tools_and_thinking_off():
    """문법을 고치는 기계적인 호출입니다. 도구를 붙이면 파일을 읽기 시작합니다."""
    captured: List[Agent] = []
    engine = _engine(["```mermaid\ngraph TD\n    A --> B\n```"])

    original = engine.llm_caller.call_agent

    async def spy(agent, messages, custom_instructions="", **kwargs):
        captured.append(agent)
        return await original(agent, messages, custom_instructions, **kwargs)

    engine.llm_caller.call_agent = spy
    await _repair(engine, "```mermaid\ngraph TD\n  subgraph S\n    A --> B\n```", [])

    assert captured[0].allowed_mcp_servers == []
    assert captured[0].sequential_thinking.enabled is False
