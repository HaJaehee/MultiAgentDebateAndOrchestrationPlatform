"""컨텍스트 창이 가득 찼을 때 무엇을 잃고 무엇을 지키는가.

도구 예산 가드([`test_tool_budget.py`](test_tool_budget.py))와 같은 두 겹입니다.

1. **미리 고지** — 남은 여유를 띠 단위로 알리고, 85% 에서는 메모리 그래프가 있으면
   "잘리기 전에 옮겨 두라" 고 시킵니다.
2. **버리기 전에 물음** — 기록을 실제로 잃는 첫 순간에 사람에게 넓힐지 묻습니다.
   답이 없으면 오래된 것부터 생략하며 진행합니다 (토론을 멈추지는 않습니다).

특히 중요한 것은 **짝 단위 트림**입니다. 도구 루프에서 `tool` 메시지를 앞선
assistant 없이 남기면 로컬에서는 멀쩡하고 실제 엔드포인트에서만 400 이 납니다.
"""

import asyncio
import uuid

import pytest

from app.agents.base import Agent
from app.agents.llm import (
    CONTEXT_PRESSURE_BANDS,
    context_headroom,
    context_pressure_notice,
    context_trim_notice,
    fit_tool_loop_context,
    memory_search_tool,
    memory_write_tool,
)
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from tests.fake_llm import FakeLLMCaller


MEMORY_TOOLS = [
    {"type": "function", "function": {"name": "memory__add_observations", "parameters": {}}},
    {"type": "function", "function": {"name": "memory__search_nodes", "parameters": {}}},
]
FILE_TOOLS = [
    {"type": "function", "function": {"name": "filesystem__read_text_file", "parameters": {}}},
]


# --------------------------------------------------------------- 1. 미리 고지


def test_each_band_is_announced_once():
    """띠를 넘을 때만 알립니다. 매 판 떠들면 프롬프트만 늘어납니다 (그것도 컨텍스트입니다)."""
    announced: set = set()
    said = []
    for used in range(0, 101):
        notice = context_pressure_notice(used=used, budget=100, announced=announced)
        if notice:
            said.append(used)

    # 띠마다 정확히 한 번.
    assert len(said) == len(CONTEXT_PRESSURE_BANDS)
    assert said == [int(b * 100) for b in CONTEXT_PRESSURE_BANDS]


def test_nothing_is_said_while_there_is_room():
    assert context_pressure_notice(used=10, budget=100, announced=set()) is None


def test_the_last_band_tells_the_agent_to_stop_calling_tools():
    notice = context_pressure_notice(used=96, budget=100, announced=set())
    assert "여유가 거의 없습니다" in notice
    assert "도구를 더 부르지 말고" in notice


def test_a_zero_budget_never_divides_by_zero():
    """`max_context_window` 가 `max_tokens` 보다 작게 설정될 수 있습니다."""
    assert context_pressure_notice(used=10, budget=0, announced=set()) is None
    assert context_pressure_notice(used=10, budget=-500, announced=set()) is None


# --------------------------------------------------------------- 2. 메모리 오프로딩


def test_the_offload_instruction_names_the_real_tool():
    """모델이 실제로 부를 수 있는 이름을 그대로 적어 줘야 합니다."""
    notice = context_pressure_notice(
        used=86, budget=100, announced=set(),
        memory_tool="memory__add_observations", tool_calls_left=20,
    )
    assert "memory__add_observations" in notice
    assert "호출은 한 번만" in notice, "쪼개 부르면 예산과 컨텍스트를 함께 태웁니다"


def test_an_agent_without_memory_is_not_told_to_use_it():
    """conf.json 기준 `coder` 에게는 memory 가 없습니다. 없는 도구를 시키면 안 됩니다."""
    notice = context_pressure_notice(
        used=86, budget=100, announced=set(), memory_tool=None, tool_calls_left=20,
    )
    assert "곧 앞선 발언" in notice, "경고 자체는 그대로 갑니다"
    assert "add_observations" not in notice


def test_offloading_is_not_asked_for_when_the_tool_budget_is_nearly_gone():
    """오프로딩도 도구 호출입니다. 결론 쓸 호출을 뺏으면 안 됩니다."""
    notice = context_pressure_notice(
        used=86, budget=100, announced=set(),
        memory_tool="memory__add_observations", tool_calls_left=3,
    )
    assert "add_observations" not in notice


def test_offloading_is_not_asked_for_at_the_very_last_band():
    """95% 에서 메시지를 더 얹는 것은 역효과입니다."""
    notice = context_pressure_notice(
        used=96, budget=100, announced=set(),
        memory_tool="memory__add_observations", tool_calls_left=50,
    )
    assert "add_observations" not in notice


def test_the_trim_notice_points_at_the_graph_when_there_is_one():
    assert "memory__search_nodes" in context_trim_notice(3, "memory__search_nodes")
    assert "search_nodes" not in context_trim_notice(3, None)


def test_memory_tools_are_found_by_name_not_by_server_key():
    """서버를 껐거나 키 이름을 바꿨을 때 없는 도구를 가리키지 않기 위해서입니다."""
    assert memory_write_tool(MEMORY_TOOLS) == "memory__add_observations"
    assert memory_search_tool(MEMORY_TOOLS) == "memory__search_nodes"
    assert memory_write_tool(FILE_TOOLS) is None
    assert memory_write_tool([]) is None
    assert memory_write_tool(None) is None

    # 키 이름을 바꿔도 찾아냅니다.
    renamed = [{"type": "function", "function": {"name": "kg__add_observations"}}]
    assert memory_write_tool(renamed) == "kg__add_observations"


# --------------------------------------------------------------- 3. 짝 단위 트림


def _agent(**kwargs) -> Agent:
    base = dict(key="coder", name="Coder", role="Engineer", model="fake/model",
                max_context_window=2000, max_tokens=500)
    base.update(kwargs)
    return Agent(**base)


def _tool_loop_messages(rounds: int, filler: int = 400):
    """`_run_litellm_loop` 이 실제로 쌓는 모양 그대로 만듭니다."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "목표"},
    ]
    for i in range(rounds):
        messages.append({
            "role": "assistant",
            "content": f"{i}번째 확인",
            "tool_calls": [{"id": f"call_{i}", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        })
        messages.append({
            "role": "tool", "tool_call_id": f"call_{i}", "name": "read_file",
            "content": f"결과 {i} " + "가" * filler,
        })
    return messages


def _orphan_tool_messages(messages):
    """앞선 assistant(tool_calls) 없이 남은 `tool` 메시지."""
    orphans = []
    open_calls = False
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            open_calls = True
        elif msg.get("role") == "tool":
            if not open_calls:
                orphans.append(msg)
        else:
            open_calls = False
    return orphans


def test_trimming_never_orphans_a_tool_result():
    """이것이 400 의 직접 원인입니다.

    "messages with role 'tool' must be a response to a preceding message with
    'tool_calls'" — 로컬 테스트는 통과하고 실제 엔드포인트에서만 터집니다.
    """
    agent = _agent()
    messages = _tool_loop_messages(rounds=12)

    fitted, dropped = fit_tool_loop_context(agent, messages)

    assert dropped > 0, "한도를 넘겼으니 무언가는 덜어냈어야 합니다"
    assert _orphan_tool_messages(fitted) == []


def test_trimming_keeps_the_head_and_the_latest_block():
    agent = _agent()
    messages = _tool_loop_messages(rounds=12)

    fitted, dropped = fit_tool_loop_context(agent, messages)

    assert fitted[0]["content"] == "sys"
    # 생략 안내는 바로 앞 user(목표) 에 합쳐집니다 — 따로 끼우면 user 가 연달아
    # 두 번이 되고, OpenAI 호환 셔임은 그것을 400 으로 거절합니다.
    assert fitted[1]["content"].startswith("목표")
    assert "컨텍스트 한도로 생략" in fitted[1]["content"]
    # 가장 최근 관측은 남아야 합니다 — 그것이 지금 판단의 근거입니다.
    assert "결과 11" in fitted[-1]["content"]


def test_trimming_never_leaves_two_user_turns_in_a_row():
    """`merge_consecutive_roles` 가 막아 주던 400 이 루프 안쪽에서 되살아났었습니다.

    "roles must alternate between user and assistant" — Anthropic·Gemini 와
    상당수 OpenAI 호환 셔임이 이것으로 요청을 거절합니다. 발언 시작 전 경로는
    자르기 뒤에 합치기를 두어 지켜졌는데, 도구 루프의 트림만 빠져 있었습니다.
    """
    agent = _agent()
    fitted, dropped = fit_tool_loop_context(agent, _tool_loop_messages(rounds=12))

    assert dropped > 0
    roles = [m["role"] for m in fitted]
    repeats = [
        (a, b) for a, b in zip(roles, roles[1:])
        if a == b and a in ("user", "assistant")
    ]
    assert repeats == [], f"같은 role 이 연달아 있습니다: {repeats}"


def test_a_conversation_that_fits_is_untouched():
    agent = _agent(max_context_window=128000, max_tokens=4096)
    messages = _tool_loop_messages(rounds=2, filler=10)
    assert fit_tool_loop_context(agent, messages) == (messages, 0)


def test_the_trim_notice_carries_the_memory_hint():
    agent = _agent()
    fitted, dropped = fit_tool_loop_context(
        agent, _tool_loop_messages(rounds=12), None, "memory__search_nodes"
    )
    assert dropped > 0
    assert "memory__search_nodes" in fitted[1]["content"]


# --------------------------------------------------------------- 4. 실제 한도 조회


def test_headroom_comes_from_the_provider_when_the_model_is_known():
    """컨텍스트는 우리가 정하는 숫자가 아닙니다. 실제 한도를 넘겨 올리면 400 입니다."""
    agent = Agent(key="a", name="A", role="R", model="gpt-4o", max_context_window=64000)
    headroom = context_headroom(agent)
    assert headroom is not None and headroom > 0


def test_headroom_is_unknown_for_private_gateways():
    """사설 게이트웨이·별칭 모델은 매핑되어 있지 않습니다. 그때는 사용자가 압니다."""
    agent = Agent(key="a", name="A", role="R", model="our-internal/llm-v3")
    assert context_headroom(agent) is None


def test_headroom_never_goes_negative():
    """설정이 이미 실제 한도보다 크면 더 넓힐 여지가 없습니다."""
    agent = Agent(key="a", name="A", role="R", model="gpt-4o", max_context_window=500_000)
    assert context_headroom(agent) == 0


# --------------------------------------------------------------- 5. 사람에게 묻기


@pytest.mark.asyncio
async def test_an_unknown_limit_still_offers_a_widening():
    """한도를 몰라도 물어는 봅니다 — 자기 엔드포인트는 사용자가 더 잘 압니다."""
    control = TurnControl()

    async def _answer(request):
        assert request.kind == "context_window"
        assert request.payload["headroom_known"] is False
        assert request.extension_step > 0
        control.resolve_context_window(request.extension_step, request.id)

    request = await control.ask_context_window(
        agent_key="a", agent_name="A", window=8000, used=9000,
        headroom=None, timeout=5.0, on_open=_answer,
    )
    assert request.granted == 4000, "한도를 모르면 지금 값의 절반을 제안합니다"


@pytest.mark.asyncio
async def test_a_widening_never_passes_the_real_limit():
    control = TurnControl()

    async def _greedy(request):
        control.resolve_context_window(999_999, request.id)

    request = await control.ask_context_window(
        agent_key="a", agent_name="A", window=64000, used=70000,
        headroom=1000, timeout=5.0, on_open=_greedy,
    )
    assert request.granted == 1000


@pytest.mark.asyncio
async def test_no_room_left_means_no_question():
    """실제 한도까지 다 쓴 경우엔 물어볼 것이 없습니다."""
    opened = []

    async def _open(request):
        opened.append(request)

    control = TurnControl()
    request = await control.ask_context_window(
        agent_key="a", agent_name="A", window=128000, used=130000,
        headroom=0, timeout=5.0, on_open=_open,
    )
    assert opened == []
    assert request.granted == 0 and request.outcome == "wrap_up"


@pytest.mark.asyncio
async def test_stopping_answers_a_pending_context_question_too():
    """정지는 두 종류의 물음을 모두 닫습니다."""
    control = TurnControl()

    async def _stop(request):
        control.request_stop()

    request = await control.ask_context_window(
        agent_key="a", agent_name="A", window=8000, used=9000,
        headroom=100000, timeout=5.0, on_open=_stop,
    )
    assert request.granted == 0 and request.outcome == "wrap_up"


@pytest.mark.asyncio
async def test_both_kinds_share_one_mailbox():
    """도구 상한과 컨텍스트가 같은 우편함을 쓰되, 서로를 덮지 않아야 합니다."""
    control = TurnControl()
    seen = []

    async def _collect(request):
        seen.append(request)

    async def _ask_budget():
        return await control.ask_tool_budget(
            agent_key="a", agent_name="A", limit=30, used=30, tool_calls=44,
            max_extension=50, timeout=5.0, on_open=_collect,
        )

    async def _ask_context():
        return await control.ask_context_window(
            agent_key="b", agent_name="B", window=8000, used=9000,
            headroom=100000, timeout=5.0, on_open=_collect,
        )

    async def _answer_both():
        while len(seen) < 2:
            await asyncio.sleep(0)
        kinds = {r.kind: r.id for r in seen}
        assert set(kinds) == {"tool_budget", "context_window"}
        control.resolve_decision(7, kinds["tool_budget"])
        control.resolve_decision(0, kinds["context_window"])

    budget, context, _ = await asyncio.gather(_ask_budget(), _ask_context(), _answer_both())
    assert budget.granted == 7
    assert context.granted == 0 and context.outcome == "wrap_up"


# --------------------------------------------------------------- 6. 화면까지의 통로


def _fixed_pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=name, role=role, model="fake/model", api_key="test-key",
                         max_context_window=window)
        for key, name, role, window in (
            ("orchestrator", "Master Orchestrator", "Moderator", 128000),
            ("coder", "Senior Engineer", "Implementation", 128000),
        )
    })


async def _make_session() -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    session_factory = get_session_factory("sqlite+aiosqlite:///:memory:")
    sid = f"ctx-{uuid.uuid4().hex[:8]}"
    async with session_factory() as db:
        db.add(SessionModel(
            id=sid, title="Context", strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator", "coder"],
        ))
        await db.commit()
    return sid


@pytest.mark.asyncio
async def test_the_turn_only_asks_once_however_many_speeches_hit_the_wall():
    """라운드가 쌓이면 거의 모든 발언이 같은 벽에 부딪힙니다.

    발언마다 물으면 토론을 진행할 수 없으므로, 첫 답을 그 턴의 나머지에 씁니다.
    """
    sid = await _make_session()
    engine = OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller())
    control = TurnControl()

    from app.orchestration.state import DebateState

    debate = DebateState(session_id=sid, user_prompt="구현해줘")
    asked = []

    async def _on_event(event):
        if event.get("type") == "context_window_exhausted":
            asked.append(event)
            control.resolve_context_window(5000, event["id"])

    agent = _fixed_pool().get("coder")
    arbiter = engine._make_context_arbiter(debate, agent, control, _on_event)

    info = {"agent_key": "coder", "agent_name": "Coder", "window": 8000,
            "used": 9000, "headroom": 100000, "tool_calls": 3}
    first = await arbiter(dict(info))
    second = await arbiter(dict(info))
    third = await arbiter(dict(info))

    assert len(asked) == 1, "한 턴에 한 번만 물어야 합니다"
    assert first["granted"] == 5000
    assert second["granted"] == 5000 and third["granted"] == 5000, (
        "같은 턴의 나머지 발언은 첫 답을 그대로 씁니다"
    )
    assert debate.context_grant == 5000


@pytest.mark.asyncio
async def test_no_answer_keeps_the_debate_going_instead_of_ending_it():
    """사람이 자리에 없다고 해서 토론을 접을 이유는 없습니다."""
    from app.orchestration.state import DebateState

    engine = OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=FakeLLMCaller())
    control = TurnControl()
    debate = DebateState(session_id="s1", user_prompt="p")

    async def _ignore(event):
        return None

    arbiter = engine._make_context_arbiter(
        debate, _fixed_pool().get("coder"), control, _ignore
    )

    # 실제 대기 시간을 기다리지 않도록 곧바로 시간 초과로 답이 나게 합니다.
    original = control.ask_context_window

    async def _fast(**kwargs):
        kwargs["timeout"] = 0.05
        return await original(**kwargs)

    control.ask_context_window = _fast  # type: ignore[method-assign]

    answer = await arbiter({"agent_key": "coder", "agent_name": "Coder", "window": 8000,
                            "used": 9000, "headroom": 100000, "tool_calls": 0})

    assert answer["granted"] == 0
    assert answer["wrap_up"] is False, "무응답은 마무리가 아니라 '생략하고 진행' 입니다"


@pytest.mark.asyncio
async def test_the_runner_labels_the_answer_with_the_right_kind():
    """화면의 버튼 하나가 두 종류의 물음에 답합니다.

    어느 쪽에 답한 것인지는 떠 있는 쪽지의 `kind` 가 정하고, 그 값이 그대로
    이벤트 이름이 됩니다 (`context_window_resolved` / `tool_budget_resolved`).
    엉뚱한 이름으로 나가면 화면이 알림 문구를 잘못 고릅니다.
    """
    from app.orchestration.runner import TurnRun

    run = TurnRun("s1", "prompt")
    events = []
    run._fanout = lambda event: events.append(event)  # noqa: SLF001

    async def _pending():
        return await run.control.ask_context_window(
            agent_key="coder", agent_name="Coder", window=8000, used=9000,
            headroom=100000, timeout=5.0,
            on_open=lambda request: _show(request),
        )

    async def _show(request):
        run.apply({"type": "context_window_exhausted", **request.describe()})

    task = asyncio.create_task(_pending())
    while run.decision_request is None:
        await asyncio.sleep(0)

    assert run.decision_request["kind"] == "context_window"
    assert run.resolve_decision(4000, run.decision_request["id"]) is True

    request = await task
    assert request.granted == 4000
    assert [e["type"] for e in events] == ["context_window_resolved"], (
        "쪽지의 종류가 이벤트 이름을 정합니다"
    )
    assert events[0]["window"] == 12000


@pytest.mark.asyncio
async def test_the_tool_loop_survives_a_context_overflow():
    """실제 손실 경로입니다.

    `fit_context_window` 는 발언 시작 전 한 번만 돕니다. 도구 출력이 창을 넘기면
    엔드포인트가 400 을 돌려주고, 예전에는 그 발언이 통째로 사라졌습니다.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.agents.llm import LLMCaller

    sent = []
    calls = {"n": 0}

    def _message(with_tools: bool):
        tc = SimpleNamespace(
            id=f"call_{calls['n']}",
            function=SimpleNamespace(name="read_file", arguments="{}"),
        )
        return SimpleNamespace(
            content="확인 중입니다." if with_tools else "여기까지의 관측으로 정리합니다.",
            tool_calls=[tc] if with_tools else None,
            model_dump=lambda: {
                "role": "assistant",
                "content": "확인 중입니다.",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            },
        )

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        calls["n"] += 1
        sent.append([dict(m) for m in kwargs["messages"]])
        # 6판까지는 도구를 부르고, 그 뒤에는 결론을 씁니다.
        return SimpleNamespace(
            choices=[SimpleNamespace(message=_message(calls["n"] <= 6 and bool(kwargs.get("tools"))))]
        )

    # 창이 좁아서 도구 결과 두어 개면 넘칩니다.
    agent = Agent(key="coder", name="Coder", role="Engineer", model="fake/model",
                  api_key="k", max_context_window=3000, max_tokens=500,
                  max_tool_iterations=20)

    caller = LLMCaller()
    caller.mcp_manager = SimpleNamespace(
        get_openai_tools_for_servers=lambda servers: [
            {"type": "function", "function": {"name": "read_file", "parameters": {}}}
        ],
        execute_tool=AsyncMock(return_value=("파일 내용 " + "가" * 800, "success")),
    )

    trims = []
    with patch("litellm.acompletion", side_effect=fake_acompletion):
        content, logs = await caller.call_agent(
            agent, [{"role": "user", "content": "읽어줘"}],
            on_context_trim=trims.append,
        )

    assert trims, "넘쳤으면 생략이 일어나고, 그 사실이 밖으로 나가야 합니다"
    assert "여기까지의 관측으로 정리합니다." in content, "발언이 살아남아야 합니다"
    assert len(logs) >= 3, "실행된 도구 기록은 그대로 남습니다"

    # 매 호출이 고아 `tool` 메시지 없이 나갔는지 — 이것이 400 의 직접 원인입니다.
    for messages in sent:
        assert _orphan_tool_messages(messages) == []


@pytest.mark.asyncio
async def test_the_loop_asks_before_it_starts_losing_observations():
    """기록을 실제로 버리는 첫 순간에 사람에게 묻습니다."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.agents.llm import LLMCaller

    asked = []

    def _message(with_tools: bool):
        tc = SimpleNamespace(id="call_x",
                             function=SimpleNamespace(name="read_file", arguments="{}"))
        return SimpleNamespace(
            content="확인 중",
            tool_calls=[tc] if with_tools else None,
            model_dump=lambda: {"role": "assistant", "content": "확인 중",
                                "tool_calls": [{"id": "call_x", "type": "function",
                                                "function": {"name": "read_file",
                                                             "arguments": "{}"}}]},
        )

    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        calls["n"] += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=_message(calls["n"] <= 5 and bool(kwargs.get("tools"))))]
        )

    async def arbiter(info):
        asked.append(dict(info))
        return {"granted": 0, "wrap_up": False}   # 넓히지 않고 생략하며 진행

    agent = Agent(key="coder", name="Coder", role="Engineer", model="fake/model",
                  api_key="k", max_context_window=3000, max_tokens=500,
                  max_tool_iterations=20)

    caller = LLMCaller()
    caller.mcp_manager = SimpleNamespace(
        get_openai_tools_for_servers=lambda servers: [
            {"type": "function", "function": {"name": "read_file", "parameters": {}}}
        ],
        execute_tool=AsyncMock(return_value=("결과 " + "나" * 800, "success")),
    )

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        content, _logs = await caller.call_agent(
            agent, [{"role": "user", "content": "읽어줘"}], context_arbiter=arbiter,
        )

    assert len(asked) == 1, "한 발언에서 두 번 묻지 않습니다"
    assert asked[0]["agent_name"] == "Coder"
    assert asked[0]["window"] == 3000
    assert content.strip(), "물어본 뒤에도 발언은 살아남습니다"


@pytest.mark.asyncio
async def test_trimming_is_reported_to_the_screen():
    """예전에는 logger 에만 남아, 기록이 사라지는 것을 사람이 알 수 없었습니다."""
    from app.orchestration.runner import TurnRun

    run = TurnRun("s1", "prompt")
    run.apply({
        "type": "context_trimmed", "agent_key": "coder", "agent_name": "Coder",
        "dropped": 3, "total_dropped": 5, "where": "speech",
    })
    assert run.context_dropped == 5
    assert "3건" in run.status_text and "누적 5건" in run.status_text
    assert run.snapshot()["context_dropped"] == 5

    run.apply({
        "type": "context_trimmed", "agent_key": "orchestrator", "agent_name": "O",
        "dropped": 2, "total_dropped": 7, "where": "synthesis",
    })
    assert "최종 합성 전사" in run.status_text


def test_the_banner_does_not_offer_a_widening_that_is_not_available():
    """확장 버튼이 사라졌는데 문구가 "늘릴까요?" 로 남으면 사라진 버튼을 찾게 됩니다."""
    from app.ui.components.chat_feed import ChatFeed

    async def _noop(*args, **kwargs):
        return None

    feed = ChatFeed(_noop, on_decision=_noop)

    feed.set_decision_request({
        "id": "c1", "kind": "context_window", "agent_name": "A",
        "limit": 128000, "extension_step": 0, "max_extension": 0,
    })
    # 화면이 없으므로 상태만 확인합니다 (`alive` 가 False 라 그리기는 건너뜁니다).
    assert feed._extension_step() == 0

    feed.set_decision_request({
        "id": "c2", "kind": "context_window", "agent_name": "A",
        "limit": 32000, "extension_step": 16000, "max_extension": 16000,
    })
    assert feed._extension_step() == 16000
