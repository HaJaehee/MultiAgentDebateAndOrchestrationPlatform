"""실패한 요청이 어떤 모양이었는지 남기는 지문.

엔드포인트가 이유를 알려 주지 않는 일이 실제로 있습니다. 게이트웨이가 vLLM 의
400 본문을 버리고 자기 500 으로 감싸 보내면 우리에게 남는 것은

    Error code: 500 - {'error': "client error: 400, message='Bad Request'"}

뿐이고, 우리가 무엇을 보냈는지조차 알 수 없습니다. 상대가 말해 주지 않으면 우리
쪽 기록으로 좁혀야 합니다.
"""

import random
from types import SimpleNamespace

import pytest

from app.agents.base import Agent
from app.agents.llm import LLMCaller, request_fingerprint


def _agent(**kwargs) -> Agent:
    base = dict(
        key="coder", name="Senior Python Engineer", role="Implementation",
        model="fake/model", api_key="k", max_context_window=32768, max_tokens=4096,
    )
    base.update(kwargs)
    return Agent(**base)


def _blob(n_chars: int) -> str:
    """실제 소스 파일처럼 토큰이 촘촘한 글자열.

    `"x" * 68000` 같은 반복 문자는 토크나이저가 통째로 뭉쳐 버려서, 20만 자가
    2만 5천 토큰으로 세어집니다. 도구가 읽어 오는 것은 소스 코드이고 그쪽은
    대략 4자에 1토큰입니다.
    """
    rng = random.Random(20260909)
    out = []
    size = 0
    while size < n_chars:
        word = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz_") for _ in range(rng.randint(3, 9)))
        out.append(word)
        size += len(word) + 1
    return " ".join(out)[:n_chars]


def _tool_loop_messages(read_chars: int = 68_000, reads: int = 3):
    """도구로 파일을 읽어 온 뒤의 대화. 실제로 실패한 발언의 모양입니다."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "구현해줘"},
    ]
    for i in range(reads):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"call_{i}",
                "function": {"name": "filesystem__read_text_file",
                             "arguments": '{"path": "app/main.py"}'},
            }],
        })
        messages.append({
            "role": "tool", "tool_call_id": f"call_{i}",
            "name": "filesystem__read_text_file", "content": _blob(read_chars),
        })
    return messages


# ------------------------------------------------------------------ 모양


def test_roles_are_run_length_encoded():
    """role 을 하나씩 나열하면 도구를 많이 쓴 요청에서 줄이 화면을 넘깁니다."""
    text = request_fingerprint(_agent(), _tool_loop_messages(read_chars=10, reads=3))
    assert "roles=system,user,assistant,tool,assistant,tool,assistant,tool" in text


def test_two_user_turns_in_a_row_are_visible_in_the_shape():
    """role 교대 400 은 이 한 줄로 판별됩니다."""
    text = request_fingerprint(_agent(), [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "목표"},
        {"role": "user", "content": "[앞선 기록 4건은 컨텍스트 한도로 생략되었습니다]"},
    ])
    assert "roles=system,user*2" in text


# ------------------------------------------------------------------ 분량


def test_an_oversized_request_says_so():
    """컨텍스트 초과 400 의 지문. 예산은 window - max_tokens - 512 입니다."""
    text = request_fingerprint(_agent(), _tool_loop_messages())
    assert "OVER BUDGET" in text
    assert "budget 28,160" in text, "32768 - 4096 - 512"


def test_a_request_that_fits_is_not_flagged():
    text = request_fingerprint(_agent(), _tool_loop_messages(read_chars=200, reads=2))
    assert "OVER BUDGET" not in text


def test_the_biggest_messages_are_named_with_their_tool():
    """어느 도구 결과가 요청을 부풀렸는지가 진단의 절반입니다."""
    text = request_fingerprint(_agent(), _tool_loop_messages(read_chars=68_000, reads=3))
    assert "filesystem__read_text_file" in text
    assert "68,000 chars" in text


def test_tool_call_arguments_count_toward_the_size():
    big = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "write_file", "arguments": _blob(5_000)}},
    ]}
    text = request_fingerprint(_agent(), [{"role": "user", "content": "hi"}, big])
    assert "assistant(tool_calls*1) 5,000 chars" in text


# ------------------------------------------------------------------ 도구


def test_tools_and_choice_are_recorded():
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    text = request_fingerprint(_agent(), [{"role": "user", "content": "hi"}], tools, "none")
    assert "tools=1 tool_choice=none" in text


def test_a_request_without_tools_says_none_sent():
    """도구 이력이 있는데 목록을 안 실었다면 그 자체가 단서입니다."""
    text = request_fingerprint(_agent(), _tool_loop_messages(read_chars=10, reads=1), None)
    assert "tools=0 tool_choice=(none sent)" in text


# ------------------------------------------------------------------ 안전


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "user"}],                                    # content 없음
    [{"role": "assistant", "content": None}],
    [{"role": "user", "content": [{"type": "text", "text": "블록"}]}],
    [{"role": "assistant", "content": "", "tool_calls": [SimpleNamespace(
        id="c", function=SimpleNamespace(name="f", arguments="{}"))]}],
])
def test_a_broken_message_list_never_breaks_the_fingerprint(messages):
    """지문을 못 만든다고 실패를 덮어서는 안 됩니다."""
    text = request_fingerprint(_agent(), messages)
    assert "Senior Python Engineer" in text


# ------------------------------------------------------------------ 실제 호출


@pytest.mark.asyncio
async def test_a_failed_call_leaves_a_fingerprint_in_the_log(caplog):
    """스트리밍도 비스트리밍도 안 되면, 우리가 보낸 것을 남깁니다."""
    from unittest.mock import patch

    async def always_400(**_kwargs):
        raise RuntimeError("client error: 400, message='Bad Request'")

    caller = LLMCaller()
    caller.mcp_manager = SimpleNamespace(
        get_openai_tools_for_servers=lambda servers: [],
        execute_tool=None,
    )

    with caplog.at_level("ERROR"), patch("litellm.acompletion", side_effect=always_400):
        with pytest.raises(RuntimeError):
            await caller._complete_once(_agent(), _tool_loop_messages(), None)

    assert any("Request fingerprint for Senior Python Engineer" in r.message
               for r in caplog.records)
    assert any("OVER BUDGET" in r.message for r in caplog.records)
