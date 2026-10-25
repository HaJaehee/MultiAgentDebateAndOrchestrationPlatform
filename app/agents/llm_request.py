"""요청 만들기: 시스템 프롬프트(페르소나 + 세션 지침 + 상시 지침), 쓸 도구 서버, LiteLLM 인자.

`RequestBuilderMixin` 은 `LLMCaller`(app/agents/llm.py) 의 일부입니다.
"""

import logging
from typing import Any, Dict, List, Optional

from app.agents.base import Agent
from app.agents.ask_user import ask_guidance
from app.agents.consultation import PEERS_TOOL_PREFIX, consultation_guidance
from app.agents.llm_context import effective_max_tokens
from app.agents.llm_tools import binary_file_guidance, file_writing_guidance
from app.agents.reasoning import strip_reasoning_trace
from app.agents.skills import skill_guidance


logger = logging.getLogger(__name__)

# Placeholder key for endpoints that require no authentication (Ollama, vLLM, LM Studio...)
LOCAL_API_KEY_PLACEHOLDER = "sk-no-key-required"


class RequestBuilderMixin:
    """시스템 프롬프트와 LiteLLM 요청 인자를 짓습니다."""

    def build_system_prompt(
        self,
        agent: Agent,
        custom_instructions: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """System prompt = persona + sequential thinking + file/skill rules + session instructions.

        세션 지침을 가장 마지막에 배치하는 이유는 해당 지시가 가장 구체적이기 때문입니다. 파일 작성 및 스킬 관련
        지침을 그 앞쪽에 배치하여, 사용자가 세션 지침으로 다른 방식을 지정하면 해당 지시가 우선하도록 구성합니다.

        결정 장부는 여기 넣지 않습니다. 라운드마다 바뀌는 글이 시스템 프롬프트에 있으면
        그 뒤 전체가 프롬프트 캐시에서 빠집니다 (`place_ledger_last`).
        """
        parts = [agent.system_prompt]

        st = agent.sequential_thinking
        if st.enabled and st.mode in ("prompt", "mcp"):
            parts.append(st.render_prompt())
            if st.mode == "mcp":
                parts.append(
                    f"각 사고 단계는 반드시 '{st.mcp_server}' MCP 서버의 sequentialthinking 도구를 호출해 기록한 뒤 진행하세요."
                )

        guidance = file_writing_guidance(tools)
        if guidance:
            parts.append(guidance)

        binary = binary_file_guidance(tools)
        if binary:
            parts.append(binary)

        skills = skill_guidance(tools)
        if skills:
            parts.append(skills)

        consultation = consultation_guidance(tools)
        if consultation:
            parts.append(consultation)

        asking = ask_guidance(tools)
        if asking:
            parts.append(asking)

        if custom_instructions:
            parts.append(f"[Session Custom Instructions]:\n{custom_instructions}")

        return "\n\n".join(p for p in parts if p)

    def resolve_tool_servers(self, agent: Agent) -> List[str]:
        """MCP servers this agent may use, including the sequential-thinking server when required."""
        servers = [s for s in agent.allowed_mcp_servers if s != PEERS_TOOL_PREFIX]
        st = agent.sequential_thinking
        if st.enabled and st.mode == "mcp" and st.mcp_server not in servers:
            servers.append(st.mcp_server)
        return servers

    def _apply_show_steps(self, agent: Agent, content: str) -> str:
        """Strips the reasoning steps when sequential_thinking.show_steps is disabled.

        `show_steps = true` 여도 사고 과정은 **다음 발언자의 프롬프트에는** 실리지
        않습니다 (`strip_reasoning_trace`). 여기서 정하는 것은 기록과 화면에
        무엇을 남길지입니다.
        """
        st = agent.sequential_thinking
        if not content or not st.enabled or st.show_steps:
            return content
        return strip_reasoning_trace(content)

    def build_completion_kwargs(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        """Maps the agent configuration onto LiteLLM completion parameters.

        `tool_choice` 는 도구를 넘기되 **부르지는 못하게** 해야 하는 자리를 위해
        열어 둡니다 (`"none"`). 자세한 사연은 `_wrap_up_without_tools` 에 있습니다.
        """
        kwargs: Dict[str, Any] = {
            "model": agent.model,
            "messages": messages,
            "temperature": agent.temperature,
            "max_tokens": effective_max_tokens(agent),
        }

        # Endpoint / credentials
        api_key = (agent.api_key or "").strip()
        if not api_key and agent.api_base:
            # Keyless local servers still need a non-empty value for OpenAI-compatible clients
            api_key = LOCAL_API_KEY_PLACEHOLDER
        if api_key:
            kwargs["api_key"] = api_key
        if agent.api_base:
            kwargs["api_base"] = agent.api_base
        if agent.api_version:
            kwargs["api_version"] = agent.api_version
        if agent.provider:
            kwargs["custom_llm_provider"] = agent.provider

        # Sampling & transport options
        if agent.top_p is not None:
            kwargs["top_p"] = agent.top_p
        if agent.timeout:
            kwargs["timeout"] = agent.timeout
        if agent.num_retries:
            kwargs["num_retries"] = agent.num_retries
        if agent.drop_params:
            kwargs["drop_params"] = True
        if agent.extra_headers:
            kwargs["extra_headers"] = dict(agent.extra_headers)
        if agent.extra_body:
            kwargs["extra_body"] = dict(agent.extra_body)

        # Native (provider-side) sequential thinking
        st = agent.sequential_thinking
        if st.enabled and st.mode == "native":
            if st.reasoning_effort:
                kwargs["reasoning_effort"] = st.reasoning_effort
            if st.thinking_budget_tokens:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": st.thinking_budget_tokens}
                # Anthropic extended thinking requires temperature = 1
                if "claude" in agent.model.lower() or "anthropic" in agent.model.lower():
                    kwargs["temperature"] = 1.0

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        return kwargs

    def _compose_content(self, agent: Agent, message: Any) -> str:
        """Merges provider-side reasoning traces (native thinking) with the answer text."""
        content = getattr(message, "content", None) or ""
        if isinstance(content, list):  # some providers return content blocks
            content = "\n".join(
                block.get("text", "") if isinstance(block, dict) else str(block) for block in content
            )

        st = agent.sequential_thinking
        reasoning = getattr(message, "reasoning_content", None) or ""
        if st.enabled and st.mode == "native" and st.show_steps and reasoning:
            content = f"> **[Sequential Thinking]**\n>\n> {reasoning.strip().replace(chr(10), chr(10) + '> ')}\n\n{content}"

        return content
