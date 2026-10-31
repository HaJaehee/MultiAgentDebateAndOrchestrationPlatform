"""오래된 큰 파일 읽기 결과 압축 (ADR-040): 무엇을 줄이고, 언제 줄이고, 무엇이 남는가."""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from pydantic import ValidationError

from app.agents import llm as llm_module
from app.agents import tool_compaction
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_context import context_budget, estimate_tokens
from app.agents.tool_compaction import (
    COMPACTED_OUTPUT_HEAD,
    EXCERPT_HEAD_LINES,
    EXCERPT_LINE_CHARS,
    EXCERPT_TAIL_LINES,
    compact_loop_messages,
    compact_stale_tool_outputs,
    compacted_output,
)
from app.config import RootConfig, ToolOutputCompactionConfig

READ = "filesystem__read_text_file"
BIG = "\n".join(f"line {i:04d} of the file" for i in range(1, 1001))  # 약 2만 자


def _opening() -> List[Dict[str, Any]]:
    return [{"role": "system", "content": "시스템"}, {"role": "user", "content": "조사하세요"}]


def _round(call_id: str, name: str, args: Dict[str, Any], output: str) -> List[Dict[str, Any]]:
    """도구를 한 번 부른 판 (`_assistant_turn` 이 만드는 모양)."""
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }]},
        {"role": "tool", "tool_call_id": call_id, "name": name, "content": output},
    ]


def _reads(count: int, output: str = BIG) -> List[Dict[str, Any]]:
    messages = _opening()
    for i in range(count):
        messages += _round(f"c{i}", READ, {"path": f"f{i}.py"}, output)
    return messages


def _outputs(messages: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    return {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}


def _compacted_ids(messages: Sequence[Dict[str, Any]]) -> List[str]:
    return [k for k, v in _outputs(messages).items() if v.startswith(COMPACTED_OUTPUT_HEAD)]


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """개발 PC 의 conf.json 을 보지 않습니다."""
    settings = ToolOutputCompactionConfig()
    monkeypatch.setattr(tool_compaction, "get_config", lambda: SimpleNamespace(tool_output_compaction=settings))
    return settings


# =============================================================== 1. 무엇을 줄이는가


def test_only_reads_at_least_stale_rounds_old_are_compacted():
    """가장 최근 판의 나이가 0 입니다. 네 판을 읽으면 첫 판만 세 판이 지났습니다."""
    out, count = compact_stale_tool_outputs(_reads(4), stale_rounds=3, min_chars=8000)
    assert count == 1
    assert _compacted_ids(out) == ["c0"]
    assert _outputs(out)["c1"] == BIG


def test_small_reads_stay():
    out, count = compact_stale_tool_outputs(_reads(6, "짧은 파일"), stale_rounds=1, min_chars=8000)
    assert count == 0
    assert _compacted_ids(out) == []


def test_results_that_cannot_be_got_back_stay():
    """다시 돌리면 달라지거나 대가가 있는 도구의 결과는 커도 건드리지 않습니다."""
    messages = _opening()
    for i, name in enumerate(("sandbox__execute_python_code", "brave_search__brave_web_search", "git__git_diff")):
        messages += _round(f"x{i}", name, {}, BIG)
    messages += _reads(3)[2:]
    out, count = compact_stale_tool_outputs(messages, stale_rounds=1, min_chars=8000)
    assert count == 2                     # 파일 읽기 3개 중 최근 판을 뺀 2개
    assert [_outputs(out)[f"x{i}"] for i in range(3)] == [BIG, BIG, BIG]


@pytest.mark.parametrize(
    "name, args",
    [
        ("filesystem__read_text_file", {"path": "a.py"}),
        ("filesystem__read_file", {"path": "a.py"}),
        ("read_file", {"path": "a.py"}),                          # 서버 키가 무엇이든 이름 꼬리로 봅니다
        ("files__read_multiple_files", {"paths": ["a.py", "b.py"]}),
    ],
)
def test_every_rereadable_read_counts(name, args):
    messages = _opening() + _round("c0", name, args, BIG) + _round("c1", READ, {"path": "z.py"}, "짧음")
    out, count = compact_stale_tool_outputs(messages, stale_rounds=1, min_chars=8000)
    assert count == 1
    stub = _outputs(out)["c0"]
    assert name in stub
    assert json.dumps(args, ensure_ascii=False) in stub   # 같은 호출을 다시 만들 수 있게


def test_the_conversation_keeps_its_shape_and_nothing_is_mutated():
    """호출과 결과의 짝, 메시지 수, 순서가 그대로여야 합니다. 받은 목록과 메시지도 고치지 않습니다 (첫 요청이 공유합니다)."""
    messages = _reads(5)
    copy = json.loads(json.dumps(messages))
    out, count = compact_stale_tool_outputs(messages, stale_rounds=2, min_chars=8000)
    assert count == 3
    assert messages == copy
    assert [(m["role"], m.get("tool_call_id")) for m in out] == [(m["role"], m.get("tool_call_id")) for m in copy]
    assert out[2] is messages[2]          # 건드리지 않은 메시지는 그대로


def test_compacting_again_changes_nothing():
    """끊긴 발언을 이어 가면 스텁이 든 메시지로 다시 돕니다. 스텁을 또 줄이면 안 됩니다."""
    once, count = compact_stale_tool_outputs(_reads(5), stale_rounds=1, min_chars=100)
    assert count == 4
    again, count = compact_stale_tool_outputs(once, stale_rounds=1, min_chars=100)
    assert count == 0
    assert again is once


def test_with_nothing_to_compact_the_same_list_comes_back():
    messages = _reads(2)
    out, count = compact_stale_tool_outputs(messages, stale_rounds=3, min_chars=8000)
    assert out is messages and count == 0


# =============================================================== 2. 스텁에 무엇이 남는가


def test_the_stub_says_what_was_read_and_how_to_get_it_back():
    stub = compacted_output(READ, '{"path": "app/x.py"}', BIG)
    lines = BIG.splitlines()
    assert stub.startswith(COMPACTED_OUTPUT_HEAD)
    assert READ in stub and "app/x.py" in stub
    assert f"{len(lines):,}줄" in stub and f"{len(BIG):,}자" in stub
    assert "다시 읽으세요" in stub and "지어내지" in stub
    assert lines[0] in stub and lines[EXCERPT_HEAD_LINES - 1] in stub
    assert lines[EXCERPT_HEAD_LINES] not in stub
    assert lines[-1] in stub and lines[-EXCERPT_TAIL_LINES] in stub
    assert lines[-EXCERPT_TAIL_LINES - 1] not in stub
    assert len(stub) < 1500


def test_a_file_with_no_line_breaks_stays_small():
    """압축된 JSON 처럼 줄바꿈이 없으면 줄 수로는 줄지 않습니다."""
    blob = "x" * 50_000
    stub = compacted_output(READ, '{"path": "a.json"}', blob)
    assert len(stub) < 1000
    assert "x" * EXCERPT_LINE_CHARS in stub
    assert "--- 끝" not in stub           # 앞에 다 나온 줄을 끝에 또 적지 않습니다


def test_a_short_file_does_not_repeat_lines():
    text = "\n".join(f"l{i}" for i in range(EXCERPT_HEAD_LINES + 1))
    stub = compacted_output(READ, "{}", text)
    assert stub.count(f"l{EXCERPT_HEAD_LINES}") == 1


def test_a_stub_longer_than_the_output_is_not_used():
    """짧은 출력을 작은 `min_chars` 로 줄이면 오히려 길어질 수 있습니다."""
    out, count = compact_stale_tool_outputs(_reads(3, "한 줄"), stale_rounds=1, min_chars=1)
    assert count == 0
    assert _outputs(out)["c0"] == "한 줄"


# =============================================================== 3. 언제 줄이는가


def test_with_room_left_nothing_is_touched(_settings):
    """앞쪽을 고치면 프롬프트 캐시가 깨집니다. 창에 여유가 있으면 오래됐어도 그대로 둡니다."""
    messages = _reads(6)
    used = estimate_tokens("fake/model", messages)
    out, count = compact_loop_messages("fake/model", messages, budget=int(used / _settings.min_usage) + 1000)
    assert count == 0 and out is messages


def test_past_the_usage_share_stale_reads_go_together(_settings):
    messages = _reads(6)
    used = estimate_tokens("fake/model", messages)
    out, count = compact_loop_messages("fake/model", messages, budget=used + 1000)
    assert count == 3                     # 나이 3 이상인 판 전부를 한 번에
    assert _compacted_ids(out) == ["c0", "c1", "c2"]


def test_over_budget_only_the_latest_round_stays(_settings):
    """다음 차례는 덩어리째 버리기입니다. 스텁은 무엇을 읽었는지라도 남깁니다."""
    messages = _reads(4)
    used = estimate_tokens("fake/model", messages)
    out, count = compact_loop_messages("fake/model", messages, budget=used - 1)
    assert count == 3
    assert _compacted_ids(out) == ["c0", "c1", "c2"]


def test_turned_off_nothing_is_touched(_settings):
    _settings.enabled = False
    messages = _reads(6)
    out, count = compact_loop_messages("fake/model", messages, budget=10)
    assert count == 0 and out is messages


def test_with_nothing_to_compact_tokens_are_not_counted(monkeypatch):
    """세는 동안 이벤트 루프가 멈춥니다. 파일을 읽지 않는 발언이 그 값을 치르지 않습니다."""
    def counted(*_args):
        raise AssertionError("줄일 것이 없으면 세지 않습니다")

    monkeypatch.setattr(tool_compaction, "estimate_tokens", counted)
    messages = _opening() + _round("c0", READ, {"path": "a.py"}, "짧음") + _round("c1", READ, {"path": "b.py"}, BIG)
    out, count = compact_loop_messages("fake/model", messages, budget=10)
    assert count == 0 and out is messages     # 큰 결과는 가장 최근 판의 것뿐입니다


def test_a_zero_share_compacts_by_age_alone(_settings):
    _settings.min_usage = 0.0
    out, count = compact_loop_messages("fake/model", _reads(4), budget=1_000_000)
    assert count == 1


def test_a_failure_leaves_the_speech_going(monkeypatch):
    def broken():
        raise RuntimeError("설정을 읽지 못함")

    monkeypatch.setattr(tool_compaction, "get_config", broken)
    messages = _reads(4)
    out, count = compact_loop_messages("fake/model", messages, budget=10)
    assert count == 0 and out is messages


def test_settings_are_bounded():
    assert RootConfig.model_fields["tool_output_compaction"].default_factory is ToolOutputCompactionConfig
    assert ToolOutputCompactionConfig().enabled is True
    for bad in ({"stale_rounds": 0}, {"min_chars": 10}, {"min_usage": 1.5}):
        with pytest.raises(ValidationError):
            ToolOutputCompactionConfig(**bad)


# =============================================================== 4. 실제 도구 루프에서


class _MCP:
    """읽으면 큰 파일을 돌려주는 MCP. 파일마다 표식을 넣어 어느 요청에 원문이 실렸는지 봅니다."""

    def __init__(self) -> None:
        self.tools = [{"type": "function", "function": {
            "name": READ, "description": "read", "parameters": {"type": "object", "properties": {}},
        }}]

    def get_openai_tools_for_servers(self, servers):
        return self.tools

    async def execute_tool(self, name, args, scope=None, actor=None):
        return f"MARK-{args['path']}\n{BIG}", "success"


class _Message(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


def _install(monkeypatch, reads: int) -> List[List[Dict[str, Any]]]:
    """파일을 `reads` 판 읽고 답하는 가짜 엔드포인트. 요청마다 받은 메시지를 모아 돌려줍니다."""
    sent: List[List[Dict[str, Any]]] = []
    current: Dict[str, Optional[Tuple[str, Dict[str, Any]]]] = {"call": None}

    async def fake_acompletion(**kwargs):
        sent.append([dict(m) for m in kwargs["messages"]])
        turn = len(sent)
        current["call"] = (READ, {"path": f"f{turn}.py"}) if turn <= reads else None

        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=""))])

        return stream()

    def fake_builder(chunks, messages=None):
        call = current["call"]
        tool_calls = [SimpleNamespace(
            id=f"call_{len(sent)}", function=SimpleNamespace(name=call[0], arguments=json.dumps(call[1])),
        )] if call else None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=_Message(content="" if call else "끝", tool_calls=tool_calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)
    return sent


def _agent(window: int) -> Agent:
    return Agent(
        key="coder", name="Coder", role="Impl", api_key="k", model="fake/model",
        max_context_window=window, max_tokens=1000, max_tool_iterations=20,
    )


async def _speak(agent: Agent, **kwargs: Any):
    return await LLMCaller(mcp_manager=_MCP()).call_agent(
        agent, [{"role": "user", "content": "조사하세요 TURN-4412"}], **kwargs,
    )


def _orphans(messages: Sequence[Dict[str, Any]]) -> List[str]:
    open_ids: set = set()
    orphans: List[str] = []
    for m in messages:
        if m.get("role") == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m.get("role") == "tool" and m["tool_call_id"] not in open_ids:
            orphans.append(m["tool_call_id"])
    return orphans


@pytest.mark.asyncio
async def test_a_small_window_survives_big_reads_without_dropping_or_asking(monkeypatch, _settings):
    """전제부터 봅니다: 압축을 끄면 같은 발언이 묻고 잘립니다. 켜면 어느 쪽도 일어나지 않습니다."""
    window = 30_000

    async def never(_request):
        raise AssertionError("줄여서 맞으면 묻지 않습니다")

    # 끄면 잘립니다.
    _settings.enabled = False
    _install(monkeypatch, reads=8)
    trims: List[int] = []
    await _speak(_agent(window), on_context_trim=trims.append)
    assert trims, "이 테스트의 전제: 압축이 없으면 이 창은 넘칩니다"

    # 켜면 잘리지도 묻지도 않습니다.
    _settings.enabled = True
    sent = _install(monkeypatch, reads=8)
    trims = []
    content, logs = await _speak(_agent(window), on_context_trim=trims.append, context_arbiter=never)
    assert content.endswith("끝")
    assert trims == []
    assert len(logs) == 8

    agent = _agent(window)
    budget = context_budget(agent, window, _MCP().tools)
    last = sent[-1]
    assert estimate_tokens(agent.model, last) <= budget
    assert _orphans(last) == []
    joined = "\n".join(str(m.get("content")) for m in last)
    assert "TURN-4412" in joined
    # 모델의 호출 기록과 어떤 파일을 읽었는지는 모두 남습니다.
    for i in range(1, 9):
        assert f"f{i}.py" in joined


@pytest.mark.asyncio
async def test_the_tool_log_keeps_the_original(monkeypatch):
    """도구 카드와 DB 는 원문을 봅니다. 줄어드는 것은 모델이 읽는 메시지뿐입니다."""
    sent = _install(monkeypatch, reads=8)
    seen: List[Dict[str, Any]] = []

    async def on_tool_call(log):
        seen.append(log)

    _content, logs = await _speak(_agent(30_000), on_tool_call=on_tool_call)
    assert all(BIG in log["output"] for log in logs)
    assert all(BIG in log["output"] for log in seen)
    assert any(
        str(m.get("content")).startswith(COMPACTED_OUTPUT_HEAD) for m in sent[-1] if m.get("role") == "tool"
    )


@pytest.mark.asyncio
async def test_a_large_window_sends_the_same_prefix_every_time(monkeypatch):
    """창에 여유가 있으면 앞서 보낸 메시지가 한 글자도 바뀌지 않습니다. 접두사 캐시가 살아 있어야 합니다."""
    sent = _install(monkeypatch, reads=6)
    await _speak(_agent(1_000_000))
    for earlier, later in zip(sent, sent[1:]):
        assert later[: len(earlier)] == earlier
    assert not any(COMPACTED_OUTPUT_HEAD in str(m.get("content")) for m in sent[-1])


@pytest.mark.asyncio
async def test_a_resumed_speech_keeps_the_stubs_and_does_not_redo_them(monkeypatch):
    """스텁은 초안에 그대로 남습니다. 이어 가도 원문이 되살아나거나 스텁을 또 줄이지 않습니다."""
    _install(monkeypatch, reads=8)
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    await _speak(_agent(30_000), checkpoint=checkpoint)
    state = saved[-1]
    stubs = [m["content"] for m in state["messages"] if str(m.get("content")).startswith(COMPACTED_OUTPUT_HEAD)]
    assert stubs

    sent = _install(monkeypatch, reads=0)
    await _speak(_agent(30_000), resume_state=state)
    resumed = [m["content"] for m in sent[0] if str(m.get("content")).startswith(COMPACTED_OUTPUT_HEAD)]
    assert all(stub in resumed for stub in stubs)
    assert all(content.count(COMPACTED_OUTPUT_HEAD) == 1 for content in resumed)
