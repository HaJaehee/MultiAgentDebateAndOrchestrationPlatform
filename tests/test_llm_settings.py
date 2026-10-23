import pytest
from app.agents.base import Agent
from app.agents.llm_request import LOCAL_API_KEY_PLACEHOLDER
from app.agents.llm import LLMCaller
from app.config import RootConfig, resolve_env_vars


def _root(**overrides):
    data = {
        "llm": {
            "model": "openai/local-model",
            "api_base": "http://localhost:1234/v1/",
            "temperature": 0.4,
            "timeout": 30,
            "num_retries": 2,
            "extra_headers": {"X-Team": "platform"},
            "sequential_thinking": {"enabled": True, "mode": "prompt", "max_steps": 7},
        },
        "agents": {
            "orchestrator": {"name": "Orch", "role": "Lead"},
            "coder": {
                "name": "Coder",
                "role": "Dev",
                "api_url": "http://gpu-box:8000/v1",
                "temperature": 0.1,
                "sequential_thinking": {"mode": "native", "reasoning_effort": "high"},
            },
        },
    }
    data.update(overrides)
    return RootConfig.model_validate(data)


def test_nested_env_var_defaults(monkeypatch):
    monkeypatch.delenv("A_VAR", raising=False)
    monkeypatch.setenv("B_VAR", "http://gateway/v1")
    assert resolve_env_vars("${A_VAR:-${B_VAR}}") == "http://gateway/v1"
    assert resolve_env_vars("${A_VAR:-${C_VAR:-fallback}}") == "fallback"
    monkeypatch.setenv("A_VAR", "primary")
    assert resolve_env_vars("${A_VAR:-${B_VAR}}") == "primary"


def test_agents_inherit_global_llm_defaults():
    cfg = _root()
    orch = cfg.agents["orchestrator"]

    assert orch.model == "openai/local-model"
    assert orch.api_base == "http://localhost:1234/v1"  # trailing slash trimmed
    assert orch.temperature == 0.4
    assert orch.timeout == 30
    assert orch.num_retries == 2
    assert orch.extra_headers == {"X-Team": "platform"}


def test_agent_overrides_win_over_globals():
    coder = _root().agents["coder"]

    assert coder.api_base == "http://gpu-box:8000/v1"  # api_url alias honoured
    assert coder.temperature == 0.1
    assert coder.model == "openai/local-model"  # not overridden -> inherited


def test_sequential_thinking_deep_merge():
    cfg = _root()
    orch_st = cfg.agents["orchestrator"].sequential_thinking
    coder_st = cfg.agents["coder"].sequential_thinking

    assert (orch_st.enabled, orch_st.mode, orch_st.max_steps) == (True, "prompt", 7)
    # Agent block overrides mode but still inherits enabled/max_steps from [llm]
    assert (coder_st.enabled, coder_st.mode, coder_st.max_steps) == (True, "native", 7)
    assert coder_st.reasoning_effort == "high"


def test_blank_env_value_falls_back_to_global():
    cfg = RootConfig.model_validate({
        "llm": {"api_base": "http://gateway/v1", "api_key": "global-key"},
        "agents": {"orchestrator": {"name": "O", "role": "L", "api_base": "", "api_key": "  "}},
    })
    orch = cfg.agents["orchestrator"]
    assert orch.api_base == "http://gateway/v1"
    assert orch.api_key == "global-key"


def test_is_live_detection():
    assert Agent(key="a", name="A", role="R", api_base="http://localhost:11434").is_live
    assert Agent(key="a", name="A", role="R", api_key="sk-123").is_live
    assert Agent(key="a", name="A", role="R", model="ollama_chat/qwen2.5").is_live
    assert not Agent(key="a", name="A", role="R", model="openai/gpt-4o").is_live


def test_completion_kwargs_mapping():
    caller = LLMCaller.__new__(LLMCaller)  # no MCP manager needed for this mapping
    agent = Agent(
        key="coder",
        name="Coder",
        role="Dev",
        model="openai/local-model",
        api_base="http://localhost:1234/v1",
        api_version="2024-10-21",
        provider="openai",
        top_p=0.9,
        timeout=30,
        num_retries=2,
        extra_headers={"X-Team": "platform"},
        extra_body={"user": "multiagent"},
    )
    kwargs = caller.build_completion_kwargs(agent, [{"role": "user", "content": "hi"}], tools=[])

    assert kwargs["api_base"] == "http://localhost:1234/v1"
    assert kwargs["api_key"] == LOCAL_API_KEY_PLACEHOLDER  # keyless local endpoint
    assert kwargs["api_version"] == "2024-10-21"
    assert kwargs["custom_llm_provider"] == "openai"
    assert kwargs["top_p"] == 0.9
    assert kwargs["timeout"] == 30
    assert kwargs["num_retries"] == 2
    assert kwargs["drop_params"] is True
    assert kwargs["extra_headers"] == {"X-Team": "platform"}
    assert kwargs["extra_body"] == {"user": "multiagent"}
    assert "tools" not in kwargs


def test_native_sequential_thinking_kwargs():
    caller = LLMCaller.__new__(LLMCaller)
    agent = Agent(
        key="critic",
        name="Critic",
        role="Review",
        model="anthropic/claude-3-5-sonnet-20241022",
        api_key="sk-test",
        temperature=0.3,
        max_tokens=4096,
        sequential_thinking={
            "enabled": True,
            "mode": "native",
            "reasoning_effort": "high",
            "thinking_budget_tokens": 8192,
        },
    )
    kwargs = caller.build_completion_kwargs(agent, [{"role": "user", "content": "hi"}])

    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert kwargs["temperature"] == 1.0  # required by Anthropic extended thinking
    assert kwargs["max_tokens"] > 8192


def test_prompt_mode_injects_protocol_and_mcp_server():
    caller = LLMCaller.__new__(LLMCaller)
    agent = Agent(
        key="orchestrator",
        name="Orch",
        role="Lead",
        system_prompt="You lead the debate.",
        allowed_mcp_servers=["filesystem"],
        sequential_thinking={"enabled": True, "mode": "mcp", "max_steps": 4},
    )

    prompt = caller.build_system_prompt(agent, custom_instructions="Use Pydantic v2.")
    assert "You lead the debate." in prompt
    assert "4단계" in prompt
    assert "sequentialthinking" in prompt
    assert "Use Pydantic v2." in prompt

    assert caller.resolve_tool_servers(agent) == ["filesystem", "sequential_thinking"]


def test_show_steps_false_keeps_only_conclusion():
    caller = LLMCaller.__new__(LLMCaller)
    agent = Agent(
        key="coder",
        name="Coder",
        role="Dev",
        sequential_thinking={"enabled": True, "mode": "prompt", "show_steps": False},
    )
    text = "Thought 1: ...\nThought 2: ...\n---\n## 최종 결론\n결과물입니다."
    assert caller._apply_show_steps(agent, text).startswith("## 최종 결론")


def test_disabled_agent_and_orchestrator_guard():
    cfg = RootConfig.model_validate({
        "agents": {
            "orchestrator": {"name": "O", "role": "L"},
            "critic": {"name": "C", "role": "R", "enabled": False},
        }
    })
    assert "critic" in cfg.agents
    assert list(cfg.enabled_agents) == ["orchestrator"]

    with pytest.raises(ValueError, match="cannot be disabled"):
        RootConfig.model_validate({"agents": {"orchestrator": {"name": "O", "role": "L", "enabled": False}}})
