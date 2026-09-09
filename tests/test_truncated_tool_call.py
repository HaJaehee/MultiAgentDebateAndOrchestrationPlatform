"""응답 한도에 걸려 잘린 도구 호출.

실제로 겪은 일입니다. System Architect 가 `filesystem__write_file` 로 파일을
통째로 쓰려다 `max_tokens=4096` 에 걸려 인자 JSON 이 `... if path:` 에서 끊겼고,

* `_parse_tool_call` 이 못 읽어 `{"raw": <잘린 원문>}` 으로 넘겼고,
* 도구 서버는 `MCP error -32602: Input validation error` 로 거절했고,
* 그런데 우리는 **그 읽지 못한 JSON 을 그대로 다음 요청에 다시 실어** 보냈습니다.

vLLM 은 그것을 채팅 템플릿에 렌더링하다 400 으로 거절했고, 앞의 게이트웨이가 그
400 을 자기 500 으로 감싸는 바람에 이유는 로그에도 남지 않았습니다.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base import Agent
from app.agents.llm import LLMCaller, truncation_advice

# 실제로 끊긴 자리를 그대로 옮긴 인자입니다. 닫히지 않은 문자열이라 파싱되지 않습니다.
TRUNCATED = '{"path": "app/util.py", "content": "def load(path):\n    if path:'

GOOD = '{"path": "a.py", "content": "print(1)"}'


def _agent(**kwargs) -> Agent:
    base = dict(key="architect", name="System Architect", role="Architecture",
                model="fake/model", api_key="k", max_tokens=4096, max_tool_iterations=3)
    base.update(kwargs)
    return Agent(**base)


def _caller() -> LLMCaller:
    caller = LLMCaller()
    caller.mcp_manager = SimpleNamespace(
        get_openai_tools_for_servers=lambda servers: [
            {"type": "function", "function": {"name": "filesystem__write_file", "parameters": {}}}
        ],
        # 인자를 읽지 못한 호출에 도구 서버가 실제로 돌려준 답입니다.
        execute_tool=AsyncMock(return_value=("MCP error -32602: Input validation error", "error")),
    )
    return caller


def _tool_call(arguments: str, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="filesystem__write_file", arguments=arguments),
    )


async def _one_tool_turn(arguments: str, finish_reason: str, call_id: str = "call_1"):
    """도구를 한 번 부르고 다음 판에서 끝내는 발언. 나간 요청들을 돌려줍니다.

    두 번째 요청이 관심사입니다 — 거기에 우리가 되돌려 보낸 발언이 들어 있고,
    실제 400 은 그 요청에서 났습니다.
    """
    sent = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")   # 비스트리밍 경로로 떨어뜨립니다
        sent.append(kwargs)
        first = len(sent) == 1
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="파일을 쓰겠습니다." if first else "다시 정리하겠습니다.",
                tool_calls=[_tool_call(arguments, call_id)] if first else None,
                # 프로바이더가 돌려주는 원본. 예전에는 이것이 그대로 다시 나갔습니다.
                model_dump=lambda: {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": call_id, "type": "function",
                                    "function": {"name": "filesystem__write_file",
                                                 "arguments": arguments}}],
                },
            ),
            finish_reason=finish_reason if first else "stop",
        )])

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await _caller().call_agent(_agent(), [{"role": "user", "content": "파일 만들어줘"}])
    return sent


def _assistant_tool_calls(messages):
    return [c for m in messages if m.get("tool_calls") for c in m["tool_calls"]]


# ------------------------------------------------------------------ 되돌려 보내는 모양


@pytest.mark.asyncio
async def test_unreadable_arguments_are_never_sent_back():
    """이것이 400 의 직접 원인이었습니다 — 우리가 못 읽은 JSON 을 상대에게 떠넘겼습니다."""
    sent = await _one_tool_turn(TRUNCATED, "length")

    calls = _assistant_tool_calls(sent[-1]["messages"])
    assert calls, "도구를 부른 발언은 맥락에 남아야 합니다"
    for call in calls:
        arguments = call["function"]["arguments"]
        json.loads(arguments)          # 나가는 인자는 언제나 온전한 JSON
        assert "if path:" not in arguments, "잘린 원문을 되돌려 보내면 안 됩니다"
        assert "_unreadable" in arguments


@pytest.mark.asyncio
async def test_readable_arguments_survive_the_round_trip():
    """정상 호출까지 뭉개면 모델이 자기가 무엇을 했는지 잃습니다."""
    sent = await _one_tool_turn(GOOD, "tool_calls")

    call = _assistant_tool_calls(sent[-1]["messages"])[0]
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py", "content": "print(1)"}
    assert call["function"]["name"] == "filesystem__write_file"


@pytest.mark.asyncio
async def test_the_tool_result_id_matches_the_assistant_turn():
    """id 가 어긋난 tool 결과는 그 자체로 400 입니다.

    `_parse_tool_call` 은 id 가 비면 하나 지어냅니다. 예전에는 `tool` 결과만 그
    지어낸 id 를 쓰고 assistant 쪽은 빈 id 그대로여서 짝이 어긋났습니다.
    """
    sent = await _one_tool_turn(GOOD, "tool_calls", call_id="")

    messages = sent[-1]["messages"]
    offered = {c["id"] for c in _assistant_tool_calls(messages)}
    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert answered, "도구 결과가 있어야 합니다"
    assert answered <= offered, f"고아 tool 결과: {answered - offered}"
    assert "" not in offered, "빈 id 를 그대로 내보내면 안 됩니다"


# ------------------------------------------------------------------ 모델에게 알리기


@pytest.mark.asyncio
async def test_the_agent_is_told_that_it_was_cut_off():
    """도구 서버의 -32602 만으로는 원인을 알 수 없어 같은 호출을 되풀이합니다."""
    sent = await _one_tool_turn(TRUNCATED, "length")

    told = "\n".join(str(m.get("content")) for m in sent[-1]["messages"])
    assert "출력 잘림" in told
    assert "4,096" in told, "어느 한도에 걸렸는지 알려야 사람이 조정할 수 있습니다"
    assert "나누어" in told, "다음에 무엇을 하라는 말이 있어야 합니다"


@pytest.mark.asyncio
async def test_finish_reason_length_alone_is_enough_to_warn():
    """인자가 우연히 닫혔어도 잘린 것은 잘린 것입니다 (내용이 중간에서 끝납니다)."""
    sent = await _one_tool_turn(GOOD, "length")

    told = "\n".join(str(m.get("content")) for m in sent[-1]["messages"])
    assert "출력 잘림" in told


@pytest.mark.asyncio
async def test_a_clean_tool_call_gets_no_truncation_notice():
    sent = await _one_tool_turn(GOOD, "tool_calls")

    told = "\n".join(str(m.get("content")) for m in sent[-1]["messages"])
    assert "출력 잘림" not in told


# ------------------------------------------------------------------ 무엇을 하라고 할지


def _tools(*names):
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


def test_with_an_append_tool_it_says_to_append_by_name():
    """`edit_file` 이 있으면 그것을 이름으로 짚어 줍니다."""
    advice = truncation_advice(_tools(
        "filesystem__read_text_file", "filesystem__write_file", "filesystem__edit_file"))
    assert "`filesystem__edit_file` 로 **뒤에 덧붙이세요**" in advice
    # 쓰지 말아야 할 쪽도 이름으로 짚습니다 — 모델이 손에 잡히는 것을 다시 잡습니다.
    assert "`filesystem__write_file` 로 이어쓰려 하면" in advice


def test_with_only_an_overwriting_tool_it_does_not_say_to_split_the_write():
    """이것이 이번 구체화의 핵심입니다.

    공식 filesystem 서버의 `write_file` 은 덮어쓰기입니다. 그걸로 "나누어 쓰라"
    고 하면 앞부분을 매번 다시 써야 해서 호출이 제곱으로 커지고, 나눈 보람도
    없이 같은 한도에 다시 걸립니다.
    """
    advice = truncation_advice(_tools("filesystem__write_file"))
    assert "덮어쓰기" in advice
    assert "여러 파일로 쪼개" in advice
    assert "덧붙이세요" not in advice, "덧붙일 도구가 없는데 덧붙이라고 하면 안 됩니다"


def test_without_file_tools_it_just_says_to_shorten():
    advice = truncation_advice(_tools("memory__search_nodes"))
    assert "짧게" in advice
    assert "파일" not in advice, "가지지도 않은 도구 이야기를 할 이유가 없습니다"


def test_no_tools_at_all_is_not_an_error():
    assert truncation_advice(None)
    assert truncation_advice([])


def test_the_tool_is_found_by_its_name_tail_not_the_server_key():
    """서버 키를 바꿔도 찾아야 합니다 (`memory_write_tool` 과 같은 규칙)."""
    advice = truncation_advice(_tools("myfs__edit_file"))
    assert "`myfs__edit_file`" in advice


@pytest.mark.asyncio
async def test_the_notice_carries_the_advice_for_the_agents_own_tools():
    """고지문에 붙는 조언은 그 발언이 실제로 가진 도구에서 나와야 합니다."""
    sent = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append(kwargs)
        first = len(sent) == 1
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="", tool_calls=[_tool_call(TRUNCATED)] if first else None,
                model_dump=lambda: {"role": "assistant", "content": "", "tool_calls": []},
            ),
            finish_reason="length" if first else "stop",
        )])

    caller = _caller()
    caller.mcp_manager.get_openai_tools_for_servers = lambda servers: _tools(
        "filesystem__write_file", "filesystem__edit_file")

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await caller.call_agent(_agent(), [{"role": "user", "content": "써줘"}])

    told = "\n".join(str(m.get("content")) for m in sent[-1]["messages"])
    assert "filesystem__edit_file" in told


# ------------------------------------------------------------------ 사람에게 알리기


async def _plain_turn(finish_reason: str, tools=None, max_continuations: int = 0):
    """도구를 부르지 않는 발언 한 번. 돌아온 본문을 돌려줍니다.

    이어받기는 기본으로 끕니다 — 여기서 보려는 것은 "사람에게 남는 표시" 입니다.
    """
    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="## 아키텍처 제안\n\n세 계층으로 나눕니다. 첫째로 게이트웨이가",
                tool_calls=None,
                model_dump=lambda: {"role": "assistant", "content": ""},
            ),
            finish_reason=finish_reason,
        )])

    caller = _caller()
    caller.mcp_manager.get_openai_tools_for_servers = lambda servers: tools or []
    agent = _agent(max_continuations=max_continuations)
    with patch("litellm.acompletion", side_effect=fake_acompletion):
        content, _logs = await caller.call_agent(
            agent, [{"role": "user", "content": "설계해줘"}])
    return content


@pytest.mark.asyncio
async def test_a_truncated_answer_is_marked_for_the_reader():
    """도구 호출이 잘리면 도구 서버가 거절해 주지만, 그냥 긴 글은 아무도 이의를
    제기하지 않습니다. 문장 중간에서 끝난 발언이 그대로 저장되고, 읽는 사람은
    그것이 잘린 것인지 원래 그렇게 끝난 것인지 알 수 없습니다."""
    content = await _plain_turn("length")

    assert "첫째로 게이트웨이가" in content, "받은 만큼은 그대로 남아야 합니다"
    assert "응답 한도" in content and "4,096" in content
    assert "max_tokens" in content, "어느 손잡이를 올려야 하는지 적혀 있어야 합니다"


@pytest.mark.asyncio
async def test_a_complete_answer_gets_no_footer():
    content = await _plain_turn("stop")
    assert "응답 한도" not in content


@pytest.mark.asyncio
async def test_the_footer_does_not_need_tools_to_appear():
    """도구가 아예 없는 에이전트(critic 처럼)도 잘립니다."""
    assert "응답 한도" in await _plain_turn("length", tools=[])


# ------------------------------------------------------------------ 파서


def test_parse_tool_call_reports_whether_it_could_read_the_arguments():
    name, args, call_id, ok = LLMCaller._parse_tool_call(_tool_call(GOOD))
    assert (name, ok) == ("filesystem__write_file", True)
    assert args == {"path": "a.py", "content": "print(1)"}
    assert call_id == "call_1"

    _name, args, _id, ok = LLMCaller._parse_tool_call(_tool_call(TRUNCATED))
    assert ok is False
    assert args == {"raw": TRUNCATED}, "원문은 도구 실행 쪽에 그대로 넘깁니다"


def test_a_missing_id_is_replaced_not_left_empty():
    _name, _args, call_id, ok = LLMCaller._parse_tool_call(_tool_call(GOOD, call_id=""))
    assert ok is True and call_id.startswith("call_")


# ------------------------------------------------------------------ 이어받기


async def _continuing_turn(pieces, finish_reasons, max_continuations=2):
    """조각을 차례로 돌려주는 엔드포인트. `(본문, 나간 요청들)`."""
    sent = []
    turns = list(zip(pieces, finish_reasons))

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append(kwargs)
        piece, finish = turns[min(len(sent) - 1, len(turns) - 1)]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=piece, tool_calls=None,
                model_dump=lambda: {"role": "assistant", "content": piece},
            ),
            finish_reason=finish,
        )])

    caller = _caller()
    caller.mcp_manager.get_openai_tools_for_servers = lambda servers: []
    with patch("litellm.acompletion", side_effect=fake_acompletion):
        content, _logs = await caller.call_agent(
            _agent(max_continuations=max_continuations),
            [{"role": "user", "content": "보고서 써줘"}])
    return content, sent


@pytest.mark.asyncio
async def test_a_truncated_report_is_continued_and_joined_without_a_seam():
    """최종 보고서가 문장 중간에서 끊기면 표시만으로는 부족합니다 — 그것이 산출물입니다."""
    content, sent = await _continuing_turn(
        ["세 계층으로 나눕니다. 첫째로 게이트", "웨이가 요청을 받습니다. 끝."],
        ["length", "stop"],
    )

    assert len(sent) == 2, "잘렸으니 한 판 더 불러야 합니다"
    # 빈 줄이 끼면 한 문장이 두 문단으로 갈립니다.
    assert "첫째로 게이트웨이가 요청을 받습니다. 끝." in content
    assert "응답 한도" not in content, "끝까지 받았으면 표시를 붙일 이유가 없습니다"


@pytest.mark.asyncio
async def test_the_continuation_request_shows_the_model_its_own_partial_text():
    content, sent = await _continuing_turn(["앞부분", "뒷부분"], ["length", "stop"])

    second = sent[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"] == "앞부분"
    assert second[-1]["role"] == "user" and "이어쓰기" in second[-1]["content"]
    assert "tools" not in sent[1], "마저 쓰는 자리이지 새로 확인할 자리가 아닙니다"


@pytest.mark.asyncio
async def test_running_out_of_continuations_says_how_many_were_used():
    """`max_tokens` 를 올릴지 `max_continuations` 를 올릴지 사람이 판단해야 합니다."""
    content, sent = await _continuing_turn(
        ["1", "2", "3"], ["length", "length", "length"], max_continuations=2)

    assert len(sent) == 3, "원본 + 이어받기 2회"
    assert "이어받기 2회" in content
    assert "123" in content, "받은 것은 전부 남습니다"


@pytest.mark.asyncio
async def test_continuation_can_be_turned_off():
    content, sent = await _continuing_turn(["앞부분"], ["length"], max_continuations=0)

    assert len(sent) == 1
    assert "응답 한도" in content and "이어받기" not in content


@pytest.mark.asyncio
async def test_a_failed_continuation_keeps_what_was_already_written():
    """이어받기는 발언을 낫게 하려는 것이지, 실패하면 앞의 것까지 버리라는 것이 아닙니다."""
    sent = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append(kwargs)
        if len(sent) > 1:
            raise RuntimeError("client error: 400, message='Bad Request'")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="여기까지 썼습니다", tool_calls=None,
                model_dump=lambda: {"role": "assistant", "content": ""},
            ),
            finish_reason="length",
        )])

    caller = _caller()
    caller.mcp_manager.get_openai_tools_for_servers = lambda servers: []
    with patch("litellm.acompletion", side_effect=fake_acompletion):
        content, _logs = await caller.call_agent(
            _agent(max_continuations=2), [{"role": "user", "content": "써줘"}])

    assert "여기까지 썼습니다" in content
    assert "응답 한도" in content


@pytest.mark.asyncio
async def test_an_empty_continuation_stops_instead_of_looping():
    content, sent = await _continuing_turn(["앞부분", "   "], ["length", "length"])

    assert len(sent) == 2, "빈 답을 받으면 더 조르지 않습니다"
    assert "앞부분" in content
