"""`max_tokens` 와 컨텍스트 창을 둘러싼 다섯 가지 (v0.7.0).

`finish_reason='length'` 가 `read_file` 에 찍힌 일을 따라가다 드러난 것들입니다. 각각은
따로 보면 사소한데, 모두 같은 증상 — 응답이 이유 없이 한도에 걸리거나 사라짐 — 으로
나타나서 로그만 보고는 어느 것인지 가릴 수 없었습니다.

1. 도구 정의가 컨텍스트 예산에 안 들어갔습니다. 요청마다 수천 토큰이 실려 나가는데
   출력 여유는 `max_tokens + 512` 뿐이라, 대화가 차면 서버가 남은 창만큼만 출력하게 했습니다.
2. 추론 모델이 한도를 사고에 다 쓰면 본문이 비고, 발언은 꼬리표 하나로 끝났습니다.
3. `native` 모드에서 요청에 실린 `max_tokens` 와 예산·안내·로그의 숫자가 달랐습니다.
4. 토큰 계산이 실패할 때의 대체 계산이 한글을 절반으로, 도구 호출 인자를 0 으로 셌습니다.
5. 서버가 해석하지 못한 도구 호출이 본문 글자로 새어 나오면, 답변으로 받아 이어 썼습니다.

그리고 2번의 다른 얼굴: 한도에 닿지 않았는데도 답이 **사고 안에만** 있고 본문이 빈 채
정상 종료하는 응답. reasoning parser 가 사고 종료 표식을 못 찾으면 이렇게 되고, 로그 한 줄
없이 빈 카드로 남았습니다.

덤으로, 도구를 쓴 발언의 이어받기가 도구 정의 없이 나가 Anthropic 에서 400 이 났습니다.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from app.agents.base import Agent
from app.agents.llm import (
    ANSWER_AFTER_REASONING_INSTRUCTION,
    ANSWER_ONLY_IN_REASONING_FOOTER,
    NATIVE_REASONING_HEADER,
    CONTINUE_ANSWER_INSTRUCTION,
    LEAKED_TOOL_CALL_FOOTER,
    MAX_LEAKED_TOOL_CALL_RETRIES,
    REASONING_CARRY_CHARS,
    LLMCaller,
    context_budget,
    effective_max_tokens,
    estimate_tokens,
    find_leaked_tool_call,
    fit_context_window,
    fit_tool_loop_context,
    max_tokens_label,
    tool_schema_tokens,
)
from app.config import SequentialThinkingConfig

MODEL = "openai/gpt-4o"


def _agent(**kwargs) -> Agent:
    base = dict(key="coder", name="Coder", role="Impl", model=MODEL, api_key="k",
                max_tokens=4096, max_context_window=32768, max_tool_iterations=6,
                max_continuations=2)
    base.update(kwargs)
    return Agent(**base)


def _tools(count: int, tag: str = "") -> list:
    """설명이 붙은 도구 정의 `count` 개. 실제 MCP 서버의 도구와 비슷한 무게입니다."""
    return [{
        "type": "function",
        "function": {
            "name": f"srv__tool_{tag}{i}",
            "description": f"[srv] 도구 {i} 입니다. 파일을 읽고 쓰며 경로는 허용된 디렉터리 안이어야 합니다. " * 3,
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "대상 경로"},
                "content": {"type": "string", "description": "쓸 내용"},
            }, "required": ["path"]},
        },
    } for i in range(count)]


# ================================================================ 1. 도구 정의도 예산에


def test_the_budget_leaves_room_for_the_tool_definitions():
    agent = _agent()
    tools = _tools(35)

    schema = tool_schema_tokens(MODEL, tools)
    assert schema > 1000, "35개 도구 정의는 수천 토큰입니다"
    assert context_budget(agent) - context_budget(agent, tools=tools) == schema
    assert tool_schema_tokens(MODEL, None) == 0 and tool_schema_tokens(MODEL, []) == 0


def _history(turns: int, chars: int = 1200) -> list:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "목표"}] + [
        {"role": "assistant" if i % 2 else "user", "content": "가" * chars} for i in range(turns)
    ] + [{"role": "user", "content": "이번 차례"}]


def test_a_transcript_that_fits_alone_is_trimmed_once_tools_are_counted():
    """메시지만 보면 들어가는데, 도구 정의까지 실으면 창을 넘는 대화."""
    agent = _agent(max_context_window=12000, max_tokens=1000)
    tools = _tools(35)
    messages = _history(6)
    assert estimate_tokens(MODEL, messages) <= context_budget(agent), "전제: 메시지만으로는 들어갑니다"
    assert estimate_tokens(MODEL, messages) > context_budget(agent, tools=tools), "전제: 도구까지면 넘습니다"

    assert fit_context_window(agent, messages)[1] == 0
    fitted, dropped = fit_context_window(agent, messages, tools=tools)
    assert dropped > 0
    assert estimate_tokens(MODEL, fitted) <= context_budget(agent, tools=tools)


def test_the_tool_loop_trim_counts_tool_definitions_too():
    agent = _agent(max_context_window=12000, max_tokens=1000)
    tools = _tools(35)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "목표"}]
    for i in range(6):
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "fs__read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "fs__read_file", "content": "나" * 1200},
        ]
    assert estimate_tokens(MODEL, messages) <= context_budget(agent)

    assert fit_tool_loop_context(agent, messages)[1] == 0
    assert fit_tool_loop_context(agent, messages, tools=tools)[1] > 0


class _Tools:
    def __init__(self, tools):
        self.tools = tools

    def get_openai_tools_for_servers(self, servers):
        return self.tools

    async def execute_tool(self, name, args, scope=None, actor=None):
        return "결과", "success"


def _msg(content="", tool_calls=None, reasoning=None):
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=reasoning,
        model_dump=lambda: {"role": "assistant", "content": content},
    )


def _script(turns):
    """`turns` 를 차례로 돌려주는 가짜 엔드포인트. 나간 요청(kwargs)을 모읍니다."""
    sent = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")   # 비스트리밍 경로로
        # 루프는 넘긴 메시지 목록에 계속 덧붙이므로, 그 순간의 모양을 복사해 둡니다.
        sent.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        message, finish = turns[min(len(sent) - 1, len(turns) - 1)]
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])

    return sent, fake_acompletion


def _read_call(call_id="c1", args='{"path": "a.py"}'):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name="srv__tool_0", arguments=args))


@pytest.mark.asyncio
async def test_the_request_actually_sent_fits_with_its_tool_definitions():
    agent = _agent(max_context_window=12000, max_tokens=1000)
    tools = _tools(35)
    sent, fake = _script([(_msg("답입니다"), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        await LLMCaller(mcp_manager=_Tools(tools)).call_agent(agent, _history(6)[1:])

    request = sent[0]
    total = estimate_tokens(MODEL, request["messages"]) + tool_schema_tokens(MODEL, request["tools"])
    assert total <= agent.max_context_window - effective_max_tokens(agent), \
        "도구 정의까지 실은 요청이 출력 몫을 남겨야 합니다"


@pytest.mark.asyncio
async def test_the_synthesis_transcript_leaves_room_for_tool_definitions(monkeypatch):
    """합성은 전사 전체가 user 메시지 하나라, 만드는 쪽에서 크기를 정합니다."""
    from tests.fake_llm import FakeLLMCaller
    from tests.test_resilience import _engine, _make_session

    sid = await _make_session(max_rounds=3)
    llm = FakeLLMCaller(replies={k: "가" * 3000 for k in ("architect", "coder", "critic")})
    engine = _engine(llm_caller=llm)
    state = await engine.run_turn(session_id=sid, user_prompt="설계해줘")

    orchestrator = Agent(key="orchestrator", name="O", role="R", model="fake/model",
                         max_context_window=12000, max_tokens=1000)
    llm.resolve_tool_servers = lambda agent: ["srv"]
    # 턴이 끝나도 풀에는 그 폴더의 런타임(도구 없음)이 유휴로 남아 먼저 잡힙니다.
    # 여기서는 도구 목록만 바꿔 보려는 것이라 호출기의 매니저를 쓰게 합니다.
    monkeypatch.setattr(engine, "_mcp_for", lambda state: None)
    llm.mcp_manager = _Tools([])
    without = engine._build_synthesis_prompt(state, orchestrator)[0]["content"]
    llm.mcp_manager = _Tools(_tools(35))
    with_tools = engine._build_synthesis_prompt(state, orchestrator)[0]["content"]

    assert len(with_tools) < len(without), "도구 정의 몫만큼 전사가 줄어야 합니다"


# ================================================================ 2. 사고만 하다 한도에 닿음


@pytest.mark.asyncio
async def test_an_answer_lost_to_reasoning_is_asked_for_again():
    reasoning = "먼저 요구를 정리한다. " * 400
    sent, fake = _script([
        (_msg("", reasoning=reasoning), "length"),
        (_msg("## 결론\n2단 캐시로 갑니다."), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(), [{"role": "user", "content": "설계해줘"}])

    assert content == "## 결론\n2단 캐시로 갑니다."
    assert "잘렸습니다" not in content and "사고(reasoning)" not in content
    retry = sent[1]["messages"][-1]["content"]
    assert "[답변 없음]" in retry
    assert "4,096" in retry, "어느 한도였는지 알려야 합니다"
    carried = retry.split("[직전 사고의 끝부분")[1]
    assert "요구를 정리한다" in carried
    assert len(carried) <= REASONING_CARRY_CHARS + 60, "사고를 통째로 넘기면 그것이 다시 한도를 먹습니다"


@pytest.mark.asyncio
async def test_when_every_retry_is_spent_thinking_the_footer_says_why():
    sent, fake = _script([(_msg("", reasoning="생각" * 500), "length")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(max_continuations=2), [{"role": "user", "content": "설계해줘"}])

    assert len(sent) == 1 + 2, "처음 한 번과 max_continuations 번까지만 부릅니다"
    assert "사고(reasoning)에 응답 한도" in content
    assert "여기서 잘렸습니다" not in content, "잘린 것이 아니라 본문이 아예 없었습니다"
    assert "[다시 요청]" in sent[2]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_after_tool_use_an_empty_final_answer_is_not_glued_to_earlier_text():
    """예전에는 `segments[-1]` 을 이어받았는데, 그것은 도구를 부르기 전 판의 글이었습니다."""
    sent, fake = _script([
        (_msg("파일을 봅니다.", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("", reasoning="생각" * 300), "length"),
        (_msg("결론입니다."), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(), [{"role": "user", "content": "봐줘"}])

    assert content == "파일을 봅니다.\n\n결론입니다."
    told = json.dumps([m for r in sent for m in r["messages"]], ensure_ascii=False)
    assert CONTINUE_ANSWER_INSTRUCTION[:12] not in told, "이어받을 글이 없는데 이어받기를 시키면 안 됩니다"


@pytest.mark.asyncio
async def test_a_continuation_that_only_thinks_is_asked_once_more():
    sent, fake = _script([
        (_msg("앞부분"), "length"),
        (_msg("", reasoning="생각" * 300), "length"),
        (_msg("뒷부분"), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(max_continuations=3), [{"role": "user", "content": "써줘"}])

    assert content == "앞부분뒷부분"
    assert "사고 없이 곧바로 이어 쓰세요" in sent[2]["messages"][-1]["content"]
    # 이어받기 요청은 매번 새로 조립합니다 — 지금까지의 글이 한 벌만 실립니다.
    assert sum(1 for m in sent[2]["messages"] if m.get("content") == "앞부분") == 1


# ================================================================ 2-b. 한도가 아닌데 답이 사고 안에만

THINKING_WITH_CONCLUSION = "Thought 1: 로컬 LRU 와 Redis 를 비교한다.\n---\n## 최종 결론\n2단 캐시로 갑니다."


def _thinking(show_steps=True, mode="prompt"):
    return SequentialThinkingConfig(enabled=True, mode=mode, show_steps=show_steps)


@pytest.mark.asyncio
async def test_an_answer_written_inside_reasoning_is_moved_to_the_body():
    """서버가 분류를 틀렸을 뿐 결론까지 다 쓰였습니다. 다시 부를 필요가 없습니다."""
    sent, fake = _script([(_msg("", reasoning=THINKING_WITH_CONCLUSION), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(sequential_thinking=_thinking()), [{"role": "user", "content": "설계해줘"}])

    assert len(sent) == 1
    assert content == THINKING_WITH_CONCLUSION


@pytest.mark.asyncio
async def test_show_steps_still_applies_to_an_answer_moved_out_of_reasoning():
    """원래 본문에 왔어야 할 글이므로, 사고 과정을 숨기는 설정도 평소처럼 걸립니다."""
    sent, fake = _script([(_msg("", reasoning=THINKING_WITH_CONCLUSION), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(sequential_thinking=_thinking(show_steps=False)),
            [{"role": "user", "content": "설계해줘"}])

    assert content == "## 최종 결론\n2단 캐시로 갑니다."


@pytest.mark.asyncio
async def test_reasoning_without_a_conclusion_asks_for_the_answer():
    sent, fake = _script([
        (_msg("", reasoning="요구를 정리한다. 무효화 비용을 따진다."), "stop"),
        (_msg("## 결론\n2단 캐시로 갑니다."), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(), [{"role": "user", "content": "설계해줘"}])

    assert content == "## 결론\n2단 캐시로 갑니다."
    retry = sent[1]["messages"][-1]["content"]
    assert "[답변 없음]" in retry and "사고(reasoning) 안에만" in retry
    assert "응답 한도" not in retry, "한도에 걸린 것이 아니므로 한도 탓을 하면 안 됩니다"
    assert "무효화 비용을 따진다" in retry, "직전 사고의 끝을 건네야 처음부터 다시 생각하지 않습니다"


@pytest.mark.asyncio
async def test_when_the_answer_never_leaves_reasoning_the_card_is_not_blank():
    """끝내 본문이 없으면 이유를 적고, 받은 사고라도 보여 줍니다 — 빈 카드보다 낫습니다."""
    sent, fake = _script([(_msg("", reasoning="요구를 정리한다."), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(max_continuations=2), [{"role": "user", "content": "설계해줘"}])

    assert len(sent) == 1 + 2
    assert content.strip(), "빈 카드로 남으면 안 됩니다"
    assert content.index(NATIVE_REASONING_HEADER) < content.index(ANSWER_ONLY_IN_REASONING_FOOTER)
    assert "요구를 정리한다." in content
    assert "사고(reasoning)에 응답 한도" not in content, "한도 탓이 아닙니다"


@pytest.mark.asyncio
async def test_hidden_steps_mean_the_reasoning_is_not_shown_even_then():
    sent, fake = _script([(_msg("", reasoning="요구를 정리한다."), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(max_continuations=1, sequential_thinking=_thinking(show_steps=False)),
            [{"role": "user", "content": "설계해줘"}])

    assert ANSWER_ONLY_IN_REASONING_FOOTER in content
    assert "요구를 정리한다." not in content


@pytest.mark.asyncio
async def test_native_mode_does_not_show_the_reasoning_twice():
    """`native` 모드는 사고를 인용 블록으로 이미 본문 앞에 붙였습니다."""
    sent, fake = _script([
        (_msg("", reasoning="요구를 정리한다."), "stop"),
        (_msg("결론입니다."), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(sequential_thinking=_thinking(mode="native")),
            [{"role": "user", "content": "설계해줘"}])

    assert len(sent) == 2, "인용 블록만 있는 발언을 답이 있는 것으로 보면 안 됩니다"
    assert content.count(NATIVE_REASONING_HEADER) == 1
    assert content.endswith("결론입니다.")


@pytest.mark.asyncio
async def test_a_native_answer_cut_while_thinking_is_recovered_not_continued():
    """합친 글(인용 블록)로 판단하면 빈 본문을 놓치고 인용 블록을 이어 씁니다."""
    sent, fake = _script([
        (_msg("", reasoning="생각" * 200), "length"),
        (_msg("결론입니다."), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(sequential_thinking=_thinking(mode="native")),
            [{"role": "user", "content": "설계해줘"}])

    told = json.dumps([m for r in sent for m in r["messages"]], ensure_ascii=False)
    assert CONTINUE_ANSWER_INSTRUCTION[:12] not in told
    assert "[답변 없음]" in told
    assert content.endswith("결론입니다.")


@pytest.mark.asyncio
async def test_after_tool_use_an_answer_left_in_reasoning_is_kept_as_its_own_paragraph():
    sent, fake = _script([
        (_msg("파일을 봅니다.", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("", reasoning=THINKING_WITH_CONCLUSION), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(), [{"role": "user", "content": "봐줘"}])

    assert content == "파일을 봅니다.\n\n" + THINKING_WITH_CONCLUSION


@pytest.mark.asyncio
async def test_a_wrap_up_answer_left_in_reasoning_is_moved_to_the_body_too():
    sent, fake = _script([
        (_msg("", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("", reasoning=THINKING_WITH_CONCLUSION), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(max_tool_iterations=1), [{"role": "user", "content": "봐줘"}])

    assert THINKING_WITH_CONCLUSION in content


@pytest.mark.asyncio
async def test_a_normal_answer_with_reasoning_is_left_alone():
    """추론 모델의 평범한 성공 — 본문도 있고 사고도 있음 — 에는 손대지 않습니다."""
    sent, fake = _script([(_msg("답입니다.", reasoning="생각했다."), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(), [{"role": "user", "content": "설계해줘"}])

    assert content == "답입니다." and len(sent) == 1


# ================================================================ 덤: 도구를 쓴 발언의 이어받기


@pytest.mark.asyncio
async def test_a_continuation_after_tool_use_sends_tools_but_forbids_calling_them():
    """Anthropic 은 tool_use/tool_result 가 든 대화를 tools 없이 보내면 400 입니다."""
    sent, fake = _script([
        (_msg("", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("앞부분"), "length"),
        (_msg("뒷부분"), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(), [{"role": "user", "content": "써줘"}])

    assert content.endswith("앞부분뒷부분")
    continuation = sent[2]
    assert continuation.get("tools"), "도구 정의가 빠지면 Anthropic 이 거절합니다"
    assert continuation.get("tool_choice") == "none", "이어 쓰는 자리에서 도구를 부르면 안 됩니다"


@pytest.mark.asyncio
async def test_an_answer_recovery_after_tool_use_also_keeps_tools_defined():
    sent, fake = _script([
        (_msg("", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("", reasoning="생각" * 100), "length"),
        (_msg("결론"), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(), [{"role": "user", "content": "써줘"}])

    assert sent[2].get("tools") and sent[2].get("tool_choice") == "none"


# ================================================================ 3. native 모드의 실제 max_tokens


def _native(max_tokens=4096, budget=4096):
    return _agent(max_tokens=max_tokens, sequential_thinking=SequentialThinkingConfig(
        enabled=True, mode="native", thinking_budget_tokens=budget))


def test_the_request_and_the_budget_agree_on_max_tokens():
    agent = _native()
    kwargs = LLMCaller(mcp_manager=_Tools([])).build_completion_kwargs(
        agent, [{"role": "user", "content": "hi"}])

    assert effective_max_tokens(agent) == 8192
    assert kwargs["max_tokens"] == 8192
    assert context_budget(agent) == agent.max_context_window - 8192 - 512, \
        "요청이 8,192 를 쓰는데 예산이 4,096 만 떼면 창을 넘깁니다"


def test_the_label_shows_where_the_number_came_from():
    label = max_tokens_label(_native())
    assert "8,192" in label and "설정 4,096" in label and "사고 예산 4,096" in label
    assert max_tokens_label(_agent()) == "4,096"


def test_a_budget_inside_max_tokens_does_not_raise_it():
    """사고 예산이 max_tokens 보다 작으면 원래 값 안에서 생각하고 답합니다."""
    assert effective_max_tokens(_native(max_tokens=8000, budget=4096)) == 8000
    prompt_mode = _agent(sequential_thinking=SequentialThinkingConfig(
        enabled=True, mode="prompt", thinking_budget_tokens=4096))
    assert effective_max_tokens(prompt_mode) == 4096


@pytest.mark.asyncio
async def test_the_truncation_footer_reports_the_real_limit():
    sent, fake = _script([(_msg("앞부분"), "length")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _native().model_copy(update={"max_continuations": 0}),
            [{"role": "user", "content": "써줘"}])

    assert "8,192 (설정 4,096 + 사고 예산 4,096)" in content


# ================================================================ 4. 대체 토큰 계산


@pytest.fixture
def no_tokenizer(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("unmapped model")
    monkeypatch.setattr(litellm, "token_counter", boom)


def test_korean_is_not_counted_at_half(no_tokenizer):
    korean = "가" * 1000
    assert estimate_tokens(MODEL, [{"role": "user", "content": korean}]) >= 1000


def test_ascii_is_not_wildly_overcounted(no_tokenizer):
    english = "cache " * 500       # 3,000자, 실제로는 500~750 토큰
    tokens = estimate_tokens(MODEL, [{"role": "user", "content": english}])
    assert 700 <= tokens <= 1100


def test_tool_call_arguments_are_counted(no_tokenizer):
    """큰 파일을 쓴 턴은 content 가 비고 무게가 전부 인자에 있습니다. 예전에는 4토큰이었습니다."""
    big = json.dumps({"path": "a.py", "content": "x = 1\n" * 3000})
    turn = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "fs__write_file", "arguments": big}}]}
    assert estimate_tokens(MODEL, [turn]) >= len(big) // 3


def test_tool_definitions_are_counted_without_a_tokenizer(no_tokenizer):
    assert tool_schema_tokens(MODEL, _tools(10, tag="fallback")) > 500


# ================================================================ 5. 본문으로 새어 나온 도구 호출


@pytest.mark.parametrize("text, kept", [
    ('파일을 봅니다.\n<tool_call>\n{"name": "fs__read_file", "arguments": {}}', "파일을 봅니다.\n"),
    ('좋습니다. [TOOL_CALLS] [{"name": "x"}]', "좋습니다. "),
    ('<|python_tag|>{"name": "x"}', ""),
    ('앞말 <function=read_file>{"path": "a"}</function>', "앞말 "),
    ("앞말<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function", "앞말"),
    ("분석합니다.\n<|start|>assistant<|channel|>commentary to=functions.read_file <|message|>{}",
     "분석합니다.\n"),
])
def test_leaked_markers_are_found_at_their_start(text, kept):
    at = find_leaked_tool_call(text)
    assert at is not None and text[:at] == kept


@pytest.mark.parametrize("text", [
    '형식 예시:\n```\n<tool_call>\n{"name": "x"}\n```\n끝.',     # 설명하는 코드 블록
    "Hermes 형식에서는 tool_call 태그를 씁니다.",                    # 산문
    "",
])
def test_explaining_the_format_is_not_a_leak(text):
    assert find_leaked_tool_call(text) is None


LEAK = '생각을 정리했습니다.\n<tool_call>\n{"name": "srv__tool_0", "arguments": {"path": "a.py", "content": "def f('


@pytest.mark.asyncio
async def test_a_leaked_call_is_not_taken_as_the_answer():
    sent, fake = _script([
        (_msg(LEAK), "length"),
        (_msg("", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("결론입니다."), "stop"),
    ])
    mcp = _Tools(_tools(2))
    mcp.execute_tool = AsyncMock(return_value=("결과", "success"))

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=mcp).call_agent(
            _agent(), [{"role": "user", "content": "써줘"}])

    mcp.execute_tool.assert_awaited_once()
    assert "<tool_call>" not in content, "새어 나온 표식이 발언에 남으면 안 됩니다"
    assert content == "생각을 정리했습니다.\n\n결론입니다."

    retry = sent[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "생각을 정리했습니다."}
    told = retry[-1]["content"]
    assert "[도구 호출 실패]" in told and "실행되지 않았습니다" in told
    assert "잘린 탓" in told, "한도에 걸려 새어 나왔으면 그 사실을 알려야 합니다"
    all_told = json.dumps([m for r in sent for m in r["messages"]], ensure_ascii=False)
    assert CONTINUE_ANSWER_INSTRUCTION[:12] not in all_told, "호출 표식을 산문처럼 이어 쓰게 하면 안 됩니다"


@pytest.mark.asyncio
async def test_repeated_leaks_stop_with_a_footer():
    sent, fake = _script([(_msg(LEAK), "length")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(), [{"role": "user", "content": "써줘"}])

    assert len(sent) == 1 + MAX_LEAKED_TOOL_CALL_RETRIES
    assert "<tool_call>" not in content
    assert LEAKED_TOOL_CALL_FOOTER in content


@pytest.mark.asyncio
async def test_without_tools_there_is_nothing_to_leak():
    """부를 도구가 없는 발언에서 이 모양은 그냥 글입니다."""
    sent, fake = _script([(_msg(LEAK), "stop")])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools([])).call_agent(
            _agent(), [{"role": "user", "content": "형식을 보여줘"}])

    assert content == LEAK and len(sent) == 1


@pytest.mark.asyncio
async def test_a_leak_in_the_wrap_up_answer_is_removed_and_explained():
    """예산을 다 쓴 뒤의 마무리에서는 다시 부르게 할 수 없으니 지우고 알립니다."""
    sent, fake = _script([
        (_msg("", tool_calls=[_read_call()]), "tool_calls"),
        (_msg("마무리합니다.\n" + LEAK.split("\n", 1)[1]), "stop"),
    ])

    with patch("litellm.acompletion", side_effect=fake):
        content, _ = await LLMCaller(mcp_manager=_Tools(_tools(2))).call_agent(
            _agent(max_tool_iterations=1), [{"role": "user", "content": "써줘"}])

    assert "<tool_call>" not in content
    assert "마무리합니다." in content and LEAKED_TOOL_CALL_FOOTER in content
