"""에이전트가 사람에게 묻기 (`ask__ask_user`, app/agents/ask_user.py, v1.5).

1. 도구는 묻는 범위가 있을 때, 전문가에게만, 남은 질문이 있을 때만 주어진다. 자문 답변 중에는 없다.
2. 실제 도구 루프가 이 도구를 보안 판정 없이 실행하고, 사람의 답(또는 답 없음)을 도구 결과로 돌려준다.
3. 우편함: 답·건너뛰기·시간 초과. 한도 쪽지와 섞이지 않는다.
4. 러너로 돈 턴에서 질문 카드가 스냅샷에 실리고, 사람이 답하면 그 답이 개입 메모로 대화에 남는다.
   대화가 끄면, 또는 사람이 지켜보지 않는 턴이면 도구가 없다.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.agents import ask_user as au
from app.agents.base import Agent
from app.agents.consultation import consultation_scope
from app.agents.llm import LLMCaller
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.control import TurnControl, UserQuestionRequest
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from tests.fake_llm import FakeLLMCaller

ARCH = Agent(key="architect", name="System Architect", role="Arch", model="openai/gpt-4o", api_key="k",
             max_tokens=1024, max_context_window=32768, max_tool_iterations=3)
ORCH = Agent(key="orchestrator", name="Orch", role="Lead", model="openai/gpt-4o", api_key="k")


async def _answer(agent, question, options):
    return "PostgreSQL"


def test_the_tool_is_offered_only_inside_a_scope_to_specialists_with_questions_left():
    assert au.ask_tools_for(ARCH) == []
    left = [1]
    with au.asking(_answer, left):
        tool = au.ask_tools_for(ARCH)
        assert [t["function"]["name"] for t in tool] == ["ask__ask_user"]
        assert "남은 질문: 1번" in tool[0]["function"]["description"]
        assert au.ask_tools_for(ORCH) == []
        with consultation_scope([ARCH], depth=1):
            assert au.ask_tools_for(ARCH) == [], "자문에 답하는 중에는 묻지 않습니다"
        left[0] = 0
        assert au.ask_tools_for(ARCH) == []
    assert au.ask_guidance(tool) and au.ask_guidance([]) is None


@pytest.mark.asyncio
async def test_run_ask_tool_answers_counts_and_falls_back():
    left = [2]

    async def silent(agent, question, options):
        return None

    with au.asking(_answer, left):
        assert await au.run_ask_tool(ARCH, {"question": "DB 는?", "options": ["A", "A", " "]}) == (
            "유저의 답: PostgreSQL", "success")
        assert left == [1]
        assert (await au.run_ask_tool(ARCH, {"question": " "}))[1] == "error"
        assert left == [1], "빈 질문은 세지 않습니다"
    with au.asking(silent, left):
        output, status = await au.run_ask_tool(ARCH, {"question": "DB 는?"})
        assert status == "success" and output.startswith(au.NO_ANSWER) and "가정" in output
        assert (await au.run_ask_tool(ARCH, {"question": "또?"}))[0].startswith("이번 턴에 물을 수 있는 횟수")
    assert (await au.run_ask_tool(ARCH, {"question": "DB 는?"}))[1] == "error"  # 범위 밖


def _msg(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None,
                           model_dump=lambda: {"role": "assistant", "content": content})


class _NoTools:
    def get_openai_tools_for_servers(self, servers):
        return []


@pytest.mark.asyncio
async def test_the_real_tool_loop_asks_without_a_security_gate():
    sent = []
    script = [
        _msg("", [SimpleNamespace(id="c1", function=SimpleNamespace(
            name="ask__ask_user", arguments='{"question": "DB 는 무엇으로 할까요?", "options": ["PostgreSQL", "MySQL"]}'))]),
        _msg("PostgreSQL 로 설계합니다."),
    ]

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=script[len(sent) - 1], finish_reason="stop")])

    class Gate:
        def filter_tools(self, agent_key, tools, mcp):
            return tools

        async def check(self, *args, **kwargs):  # pragma: no cover - 불리면 안 됩니다
            raise AssertionError("묻기 도구는 보안 판정을 거치지 않습니다")

    asked = []

    async def asker(agent, question, options):
        asked.append((agent.key, question, options))
        return "PostgreSQL"

    logs = []
    with patch("litellm.acompletion", side_effect=fake_acompletion), au.asking(asker, [3]):
        content, _ = await LLMCaller(mcp_manager=_NoTools()).call_agent(
            ARCH, [{"role": "user", "content": "설계"}], on_tool_call=logs.append, tool_gate=Gate(),
        )
    assert content == "PostgreSQL 로 설계합니다."
    assert asked == [("architect", "DB 는 무엇으로 할까요?", ["PostgreSQL", "MySQL"])]
    assert "ask__ask_user" in {t["function"]["name"] for t in sent[0]["tools"]}
    assert "[사람에게 묻기]" in sent[0]["messages"][0]["content"]
    assert logs[0]["tool_name"] == "ask__ask_user" and "유저의 답: PostgreSQL" in logs[0]["output"]


@pytest.mark.asyncio
async def test_the_mailbox_keeps_questions_apart():
    control = TurnControl()
    opened = []

    async def on_open(request):
        opened.append(request.describe())

    task = asyncio.create_task(control.ask_user_question(
        agent_key="architect", agent_name="A", question="Q?", options=["x"], timeout=5, on_open=on_open,
    ))
    await asyncio.sleep(0.05)
    assert opened[0]["kind"] == "user_question" and opened[0]["question"] == "Q?" and opened[0]["options"] == ["x"]
    assert control.pending_decision is None and len(control.pending_questions) == 1
    assert control.resolve_user_question(opened[0]["id"], "  예  ")
    request = await task
    assert (request.answer, request.outcome) == ("예", "answered")
    assert not control.resolve_user_question(opened[0]["id"], "또"), "답은 한 번뿐입니다"

    skipped = asyncio.create_task(control.ask_user_question(
        agent_key="a", agent_name="A", question="Q?", options=[], timeout=5,
    ))
    await asyncio.sleep(0.05)
    control.resolve_user_question(control.pending_questions[0].id, "")
    assert (await skipped).outcome == "skipped"

    timed_out = await control.ask_user_question(agent_key="a", agent_name="A", question="Q?", options=[], timeout=0.05)
    assert isinstance(timed_out, UserQuestionRequest) and timed_out.outcome == "timeout" and timed_out.answer == ""


class Asking(FakeLLMCaller):
    """아키텍트가 발언할 때 실제 도구 루프처럼 `ask__ask_user` 를 한 번 실행합니다."""

    def __init__(self):
        super().__init__()
        self.offered = []
        self.results = []
        self.prompts = {}

    async def call_agent(self, agent, messages, *args, **kwargs):
        tools = au.ask_tools_for(agent)
        self.offered.append((agent.key, bool(tools)))
        self.prompts.setdefault(agent.key, []).append("\n".join(str(m.get("content")) for m in messages))
        if agent.key == "architect" and tools:
            self.results.append(await au.run_ask_tool(agent, {"question": "DB 는?", "options": ["PostgreSQL"]}))
        return await super().call_agent(agent, messages, *args, **kwargs)


async def _session(ask_user: bool) -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    sid = f"ask-{uuid.uuid4().hex[:8]}"
    async with get_session_factory()() as db:
        db.add(SessionModel(id=sid, title="질문", strategy="sequential_debate", max_rounds=1, ask_user=ask_user,
                            active_agents=["orchestrator", "architect", "coder", "critic"]))
        await db.commit()
    return sid


@pytest.mark.asyncio
async def test_a_watched_turn_shows_the_card_and_shares_the_answer():
    sid = await _session(ask_user=True)
    caller = Asking()
    runner = DebateRunner(engine=OrchestratorEngine(llm_caller=caller))
    run = runner.start(sid, "설계해 줘")
    for _ in range(200):
        if run.snapshot()["user_questions"]:
            break
        await asyncio.sleep(0.02)
    card = run.snapshot()["user_questions"][0]
    assert card["agent_name"] == "System Architect" and card["question"] == "DB 는?"
    assert runner.resolve_user_question(sid, card["id"], "PostgreSQL 로 해 주세요", approver="local")
    await run.task

    assert caller.results == [("유저의 답: PostgreSQL 로 해 주세요", "success")]
    assert run.snapshot()["user_questions"] == []
    # 오케스트레이터는 도구를 받지 않습니다. 전문가는 받습니다.
    assert ("orchestrator", True) not in caller.offered and ("architect", True) in caller.offered
    # 답은 개입 메모로 남아 다음 발언자의 맥락에 실립니다.
    async with get_session_factory()() as db:
        users = (await db.execute(select(MessageModel.content).where(
            MessageModel.session_id == sid, MessageModel.sender_key == "user"))).scalars().all()
    assert any("[System Architect 의 질문에 대한 답]" in u and "PostgreSQL 로 해 주세요" in u for u in users)
    assert any("PostgreSQL 로 해 주세요" in p for p in caller.prompts["coder"])
    await runner.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("ask_user, attended", [(False, True), (True, False)])
async def test_no_tool_when_the_conversation_or_the_turn_has_nobody_to_ask(ask_user, attended):
    sid = await _session(ask_user=ask_user)
    caller = Asking()
    runner = DebateRunner(engine=OrchestratorEngine(llm_caller=caller))
    run = runner.start(sid, "설계해 줘", attended=attended)
    await run.task
    assert run.status == "completed"
    assert not any(offered for _key, offered in caller.offered) and caller.results == []
    await runner.shutdown()
