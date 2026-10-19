from __future__ import annotations

import pytest

from app.agents.base import Agent
from app.agents.consultation import (
    CONSULT_AGENT_TOOL,
    consultation_guidance,
    consultation_scope,
    consultation_tools_for,
    is_consultation_tool,
    run_consultation_tool,
)


def _agent(key: str, name: str, role: str, allow_consult: bool = False) -> Agent:
    return Agent(
        key=key,
        name=name,
        role=role,
        system_prompt=f"You are {name}.",
        model="gpt-4o",
        allow_peer_consultation=allow_consult,
    )


def test_is_consultation_tool():
    assert is_consultation_tool("peers__consult_agent") is True
    assert is_consultation_tool("consult_agent") is True
    assert is_consultation_tool("filesystem__read_file") is False
    assert is_consultation_tool("skills__load_skill") is False


def test_consultation_tools_for_schema():
    orch = _agent("orchestrator", "Orchestrator", "Moderator", allow_consult=True)
    arch = _agent("architect", "Architect", "System Architect", allow_consult=True)
    sec = _agent("security", "Security", "Security Specialist")
    coder = _agent("coder", "Coder", "Software Engineer")

    # Orchestrator does not get consultation tools
    assert consultation_tools_for(orch, peers=[orch, arch, sec, coder]) == []

    # Agent with allow_consult=False does not get tools unless explicit peers/scope provided
    assert consultation_tools_for(sec) == []

    # With peers, architect gets tool with sec and coder in enum (orch and arch excluded)
    tools = consultation_tools_for(arch, peers=[orch, arch, sec, coder])
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert fn["name"] == CONSULT_AGENT_TOOL
    params = fn["parameters"]["properties"]
    assert "agent_key" in params
    assert "question" in params
    assert set(params["agent_key"]["enum"]) == {"security", "coder"}

    # Guidance
    assert consultation_guidance(tools) is not None
    assert consultation_guidance([]) is None


@pytest.mark.asyncio
async def test_run_consultation_tool_validations():
    arch = _agent("architect", "Architect", "System Architect", allow_consult=True)
    sec = _agent("security", "Security", "Security Specialist")

    # Missing arguments
    out, status = await run_consultation_tool(arch, CONSULT_AGENT_TOOL, {})
    assert status == "error"
    assert "agent_key" in out

    out, status = await run_consultation_tool(arch, CONSULT_AGENT_TOOL, {"agent_key": "security"})
    assert status == "error"
    assert "question" in out

    # Self-consultation blocked
    out, status = await run_consultation_tool(
        arch, CONSULT_AGENT_TOOL, {"agent_key": "architect", "question": "Am I right?"}
    )
    assert status == "error"
    assert "자기 자신" in out

    # No scope active
    out, status = await run_consultation_tool(
        arch, CONSULT_AGENT_TOOL, {"agent_key": "security", "question": "Is this safe?"}
    )
    assert status == "error"
    assert "실행 문맥" in out


class DummyLLMCaller:
    def __init__(self, answer: str = "Looks secure."):
        self.answer = answer
        self.calls = []

    async def call_agent(self, agent, messages, custom_instructions="", **kwargs):
        self.calls.append((agent, messages, custom_instructions))
        return self.answer, []


@pytest.mark.asyncio
async def test_run_consultation_tool_success_and_events():
    arch = _agent("architect", "Architect", "System Architect", allow_consult=True)
    sec = _agent("security", "Security", "Security Specialist")
    dummy_caller = DummyLLMCaller("Use argon2id with 3 iterations.")
    events = []

    def on_event(ev):
        events.append(ev)

    with consultation_scope(
        [arch, sec],
        llm_caller=dummy_caller,
        custom_instructions="Be concise",
        session_id="sess-test",
        on_event=on_event,
        max_depth=1,
    ):
        out, status = await run_consultation_tool(
            arch,
            CONSULT_AGENT_TOOL,
            {
                "agent_key": "security",
                "question": "What password hashing algorithm should we use?",
                "context": "User authentication module",
            },
        )

    assert status == "success"
    assert "Security" in out
    assert "argon2id" in out
    assert len(dummy_caller.calls) == 1
    called_agent, called_msgs, instructions = dummy_caller.calls[0]
    assert called_agent.key == "security"
    assert called_agent.max_tool_iterations <= 3
    assert instructions == "Be concise"
    assert "What password hashing algorithm" in called_msgs[0]["content"]

    # Events emitted
    ev_types = [e["type"] for e in events]
    assert "peer_consultation_started" in ev_types
    assert "peer_consultation_finished" in ev_types


@pytest.mark.asyncio
async def test_run_consultation_tool_depth_and_cycle_guard():
    arch = _agent("architect", "Architect", "System Architect", allow_consult=True)
    sec = _agent("security", "Security", "Security Specialist", allow_consult=True)
    dummy_caller = DummyLLMCaller()

    # When depth >= max_depth
    with consultation_scope([arch, sec], llm_caller=dummy_caller, depth=1, max_depth=1):
        out, status = await run_consultation_tool(
            arch, CONSULT_AGENT_TOOL, {"agent_key": "security", "question": "Question?"}
        )
        assert status == "error"
        assert "깊이 상한" in out

    # Cycle guard (arch in history)
    with consultation_scope(
        [arch, sec], llm_caller=dummy_caller, depth=1, max_depth=2, history=("architect",)
    ):
        out, status = await run_consultation_tool(
            sec, CONSULT_AGENT_TOOL, {"agent_key": "architect", "question": "Question back?"}
        )
        assert status == "error"
        assert "순환 자문 호출" in out
