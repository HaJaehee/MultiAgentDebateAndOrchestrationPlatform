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
from app.agents.llm import LLMCaller

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
