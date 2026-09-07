import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import litellm
from app.agents.base import Agent
from app.mcp.manager import MCPManager, get_mcp_manager

logger = logging.getLogger(__name__)

# Suppress litellm verbose logging
litellm.suppress_debug_info = True

# Placeholder key for endpoints that require no authentication (Ollama, vLLM, LM Studio...)
LOCAL_API_KEY_PLACEHOLDER = "sk-no-key-required"

# Marker emitted by the sequential-thinking protocol, used to hide steps when show_steps = false
CONCLUSION_MARKERS = ("## 최종 결론", "## Final Conclusion", "## 최종결론")


class LLMUnavailableError(RuntimeError):
    """LLM 엔드포인트에 닿지 못했을 때 올라옵니다.

    예전에는 여기서 내장 시뮬레이터가 그럴듯한 페르소나 답변을 지어냈습니다.
    엔드포인트가 500 을 돌려준 뒤에도 토론은 멀쩡히 굴러가는 것처럼 보였고,
    그 지어낸 발언이 다음 에이전트의 입력과 최종 합성 보고서까지 오염시켰습니다.
    모르는 것은 모른다고 말하는 편이 낫습니다.
    """

    def __init__(self, agent: Agent, reason: str):
        self.agent_key = agent.key
        self.agent_name = agent.name
        self.model = agent.model
        self.endpoint = agent.endpoint_label
        self.reason = reason.strip() or "원인을 확인할 수 없습니다"
        super().__init__(f"{agent.name} ({agent.model} @ {self.endpoint}): {self.reason}")


def merge_consecutive_roles(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 role 이 연달아 오면 하나로 합칩니다.

    토론 기록은 본질적으로 다자 대화인데 OpenAI 형식에는 "다른 에이전트" role 이
    없습니다. 그래서 남의 발언은 전부 user, 자기 발언만 assistant 로 넣게 되고,
    발언자가 셋이면 user 가 연달아 3~5개씩 나갑니다.

    OpenAI 는 이것을 받아주지만 **Anthropic·Gemini 와 상당수 OpenAI 호환 셔임
    (llama.cpp server, 일부 vLLM 챗 템플릿)은 400 Bad Request 로 거절합니다**
    ("roles must alternate between user and assistant"). 라운드가 늘수록 연속
    구간이 길어지므로, 이런 엔드포인트에서는 오케스트레이터만 응답하고 전문가
    에이전트는 전부 실패합니다.

    발언마다 `[이름 (역할)]:` 머리표가 이미 붙어 있어, 합쳐도 누가 말했는지는
    그대로 남습니다. tool 호출이 얽힌 메시지는 건드리지 않습니다.
    """
    merged: List[Dict[str, Any]] = []
    for msg in messages:
        prev = merged[-1] if merged else None
        mergeable = (
            prev is not None
            and prev.get("role") == msg.get("role")
            and msg.get("role") in ("user", "assistant")
            and not prev.get("tool_calls") and not msg.get("tool_calls")
            and isinstance(prev.get("content"), str) and isinstance(msg.get("content"), str)
        )
        if mergeable:
            prev["content"] = f"{prev['content']}\n\n{msg['content']}"
        else:
            merged.append(dict(msg))
    return merged


def estimate_tokens(model: str, messages: List[Dict[str, Any]]) -> int:
    """메시지 목록의 토큰 수. 모델을 모르면 글자 수로 어림잡습니다."""
    try:
        return int(litellm.token_counter(model=model, messages=messages))
    except Exception:  # noqa: BLE001 - 토큰 계산 실패가 호출을 막아서는 안 됩니다
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        # 한글은 토크나이저에 따라 글자당 1~1.5 토큰입니다. 넉넉히 잡습니다.
        return chars // 2 + len(messages) * 4


def fit_context_window(agent: Agent, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`max_context_window` 안에 들어가도록 가운데 발언부터 덜어냅니다.

    라운드가 쌓이면 전사(transcript)가 그대로 길어져 컨텍스트 한도를 넘고,
    엔드포인트는 400 ("maximum context length ... however you requested ...") 을
    돌려줍니다. 지금까지 이 설정은 conf.json 에 선언만 되어 있고 아무데서도
    읽히지 않았습니다.

    맨 앞(목표)과 맨 뒤(이번 차례 지시)는 남깁니다. 그 사이를 오래된 것부터
    덜어내고, 무엇이 빠졌는지 모델에게 알려 줍니다.
    """
    budget = agent.max_context_window - agent.max_tokens - 512  # 응답분 + 여유
    if budget <= 0 or len(messages) <= 3:
        return messages
    if estimate_tokens(agent.model, messages) <= budget:
        return messages

    head, tail = messages[:2], messages[-1:]      # system + 목표, 이번 차례 지시
    middle = messages[2:-1]
    dropped = 0
    while middle and estimate_tokens(agent.model, head + middle + tail) > budget:
        middle.pop(0)
        dropped += 1

    if dropped:
        notice = {
            "role": "user",
            "content": f"[앞선 발언 {dropped}건은 컨텍스트 한도로 생략되었습니다. "
                       f"남은 기록만으로 판단하고, 생략된 내용을 지어내지 마세요.]",
        }
        logger.warning(
            f"Context window trim for {agent.name}: dropped {dropped} message(s) "
            f"(max_context_window={agent.max_context_window})"
        )
        return head + [notice] + middle + tail
    return head + middle + tail


class ToolCallLog(dict):
    """Dictionary representing a single tool call and its execution result."""
    pass


# 도구 예산이 바닥났을 때 "얼마나 더 허용할지" 를 정해 주는 콜백.
#
# 0 을 돌려주면 확장 없음 — 에이전트는 도구를 떼고 지금까지 얻은 것으로 결론을
# 씁니다. 이 자리에 사람이 있습니다 (엔진이 화면에 물어보는 통로를 끼웁니다).
BudgetArbiter = Callable[[Dict[str, Any]], Awaitable[int]]


# ---------------------------------------------------------------- 도구 예산 고지

# 도구를 부를 수 있는 횟수가 이 값들에 닿을 때마다 에이전트에게 알립니다.
# 상한보다 큰 값은 그냥 지나갑니다 (상한 30 인 에이전트에게 "100회 남음" 을
# 알릴 일은 없습니다).
TOOL_BUDGET_MILESTONES = (100, 80, 60, 50, 40, 30, 20, 10)

# 여기서부터는 매 호출마다 알립니다. 5 → 4 → 3 → 2 → 1 → 불가.
#
# 사다리를 촘촘하게 만드는 이유는, 모델이 "아직 여유가 있다" 고 믿은 채로
# 탐색을 이어가다 마지막 판에서 갑자기 끊기는 것을 막기 위해서입니다. 남은
# 횟수를 알면 결론을 쓸 자리를 스스로 남겨 둡니다.
TOOL_BUDGET_FINAL_COUNTDOWN = 5


def tool_budget_notice(*, remaining: int, limit: int, used: int, tool_calls: int) -> Optional[str]:
    """이번 판에 에이전트에게 붙일 예산 고지. 알릴 시점이 아니면 None.

    첫 판에는 총량을, 그 뒤로는 사다리(`TOOL_BUDGET_MILESTONES`)에 닿을 때와
    마지막 5회부터 매번 남은 횟수를 알립니다.
    """
    if remaining <= 0:
        return None

    if used == 0:
        return (
            f"[도구 호출 예산] 이번 발언에서 도구를 부를 수 있는 횟수는 **총 {limit}회**입니다. "
            f"한 번에 여러 도구를 함께 불러도 그 묶음이 1회입니다. "
            f"예산을 다 쓰면 도구 없이 결론만 써야 하므로, 꼭 필요한 호출부터 하고 "
            f"결론을 쓸 여유를 남겨 두세요."
        )

    announce = remaining <= TOOL_BUDGET_FINAL_COUNTDOWN or (
        remaining in TOOL_BUDGET_MILESTONES and remaining < limit
    )
    if not announce:
        return None

    head = (
        f"[도구 호출 예산] 남은 호출 **{remaining}회** "
        f"(총 {limit}회 중 {used}회 사용, 도구 {tool_calls}건 실행)."
    )
    if remaining == 1:
        return (
            f"{head} **마지막 기회입니다.** 이번 한 번을 쓰고 나면 도구를 전혀 쓸 수 없습니다. "
            f"정말 결론에 필요한 것이 아니면 지금 바로 답을 작성하세요."
        )
    if remaining <= TOOL_BUDGET_FINAL_COUNTDOWN:
        return (
            f"{head} 마무리를 준비하세요 — 남은 호출은 결론에 반드시 필요한 것에만 쓰고, "
            f"확인하지 못한 것은 확인하지 못했다고 적으세요."
        )
    return f"{head} 남은 횟수 안에서 결론까지 낼 수 있도록 계획을 조정하세요."


BUDGET_EXHAUSTED_INSTRUCTION = (
    "[도구 호출 예산 소진] 도구 호출 상한 {limit}회를 모두 썼습니다. "
    "지금부터 도구는 사용할 수 없습니다.\n"
    "새로 도구를 부르려 하지 말고, 지금까지 실행한 {tool_calls}건의 관측만으로 "
    "이번 발언의 결론을 **지금 작성하세요**.\n"
    "확인하지 못한 것은 확인하지 못했다고 적고, 무엇이 남았는지 다음 발언자에게 넘기세요. "
    "없는 사실을 지어내지 마세요."
)

BUDGET_EXTENDED_INSTRUCTION = (
    "[도구 호출 예산 확장] 사용자가 상한을 {extra}회 늘려 총 {limit}회가 되었습니다 "
    "(남은 호출 {remaining}회). 늘어난 몫은 결론을 내는 데 꼭 필요한 확인에만 쓰고, "
    "같은 탐색을 반복하지 마세요."
)

# 예산이 바닥나 도구 없이 마무리했다는 사실을 발언 끝에 남깁니다. 화면과 기록
# 모두에서 "왜 여기서 멈췄는가" 가 보여야, 사람이 상한을 올릴지 판단할 수 있습니다.
BUDGET_WRAP_UP_FOOTER = (
    "> ⚠️ **도구 호출 상한({limit}회)에 도달**해 도구 없이 마무리한 발언입니다 "
    "(도구 {tool_calls}건 실행). 더 확인이 필요하면 에이전트 설정의 "
    "`max_tool_iterations` 를 올리거나, 다음 요청에서 범위를 좁혀 다시 물어보세요."
)


class LLMCaller:
    """Executes LLM completions with an MCP tool-calling loop.

    호출이 실패하면 `LLMUnavailableError` 를 올립니다. 대체 응답을 만들어 내지 않습니다.
    """

    def __init__(self, mcp_manager: Optional[MCPManager] = None):
        self.mcp_manager = mcp_manager or get_mcp_manager()

    def build_system_prompt(self, agent: Agent, custom_instructions: str = "") -> str:
        """System prompt = persona + sequential thinking protocol + session instructions."""
        parts = [agent.system_prompt]

        st = agent.sequential_thinking
        if st.enabled and st.mode in ("prompt", "mcp"):
            parts.append(st.render_prompt())
            if st.mode == "mcp":
                parts.append(
                    f"각 사고 단계는 반드시 '{st.mcp_server}' MCP 서버의 sequentialthinking 도구를 호출해 기록한 뒤 진행하세요."
                )

        if custom_instructions:
            parts.append(f"[Session Custom Instructions]:\n{custom_instructions}")

        return "\n\n".join(p for p in parts if p)

    def resolve_tool_servers(self, agent: Agent) -> List[str]:
        """MCP servers this agent may use, including the sequential-thinking server when required."""
        servers = list(agent.allowed_mcp_servers)
        st = agent.sequential_thinking
        if st.enabled and st.mode == "mcp" and st.mcp_server not in servers:
            servers.append(st.mcp_server)
        return servers

    async def call_agent(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[BudgetArbiter] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Executes a turn for the given agent.
        Returns (response_text, tool_call_logs).

        `session_id` 는 MCP 도구 호출의 스코프로 함께 보내집니다. 이걸 빠뜨리면
        서버가 대화를 구분할 수 없어 다른 대화의 상태(지식 그래프 등)를 봅니다.
        발언자(`agent.key`)도 함께 실려서, 커널처럼 에이전트 단위로 나뉘어야 하는
        상태를 서버가 구분할 수 있습니다 (`MCPManager.compose_scope`).

        `budget_arbiter` 는 도구 호출 상한에 닿았을 때 사람에게 확장을 물어보는
        통로입니다. 주지 않으면 확장 없이, 도구를 떼고 결론만 받아 마무리합니다
        (`_run_litellm_loop`). 어느 쪽이든 발언이 통째로 버려지지는 않습니다.
        """
        formatted_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(agent, custom_instructions)}
        ]
        formatted_messages.extend(messages)

        # 순서 주의: 먼저 한도에 맞춰 자르고, 그 다음 role 을 합칩니다. 생략 안내가
        # user 로 들어가므로 합치기를 나중에 해야 교대가 보장됩니다.
        formatted_messages = fit_context_window(agent, formatted_messages)
        formatted_messages = merge_consecutive_roles(formatted_messages)

        # Retrieve available tools for this agent
        tools = self.mcp_manager.get_openai_tools_for_servers(self.resolve_tool_servers(agent))

        # Real endpoint if an API URL, an API key, or a keyless local runtime is configured
        if not agent.is_live:
            raise LLMUnavailableError(
                agent,
                "api_base 도 api_key 도 설정되어 있지 않습니다. conf.json 의 llm 또는 "
                f"agents.{agent.key} 에 엔드포인트를 지정하세요.",
            )

        try:
            content, logs = await self._run_litellm_loop(
                agent, formatted_messages, tools, on_tool_call, on_chunk=on_chunk,
                session_id=session_id, budget_arbiter=budget_arbiter,
            )
        except LLMUnavailableError:
            raise
        except Exception as e:
            logger.error(
                f"LLM call failed for {agent.name} "
                f"(model={agent.model}, api_base={agent.api_base or 'provider default'}): {e}"
            )
            raise LLMUnavailableError(agent, f"{type(e).__name__}: {e}") from e

        return self._apply_show_steps(agent, content), logs

    def _apply_show_steps(self, agent: Agent, content: str) -> str:
        """Strips the reasoning steps when sequential_thinking.show_steps is disabled."""
        st = agent.sequential_thinking
        if not content or not st.enabled or st.show_steps:
            return content
        for marker in CONCLUSION_MARKERS:
            idx = content.find(marker)
            if idx != -1:
                return content[idx:].strip()
        return content

    def build_completion_kwargs(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Maps the agent configuration onto LiteLLM completion parameters."""
        kwargs: Dict[str, Any] = {
            "model": agent.model,
            "messages": messages,
            "temperature": agent.temperature,
            "max_tokens": agent.max_tokens,
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
                if agent.max_tokens <= st.thinking_budget_tokens:
                    kwargs["max_tokens"] = st.thinking_budget_tokens + agent.max_tokens

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

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

    # ------------------------------------------------------------ 한 판 호출

    @staticmethod
    async def _emit_chunk(on_chunk: Callable[[str], Any], text: str) -> None:
        if asyncio.iscoroutinefunction(on_chunk):
            await on_chunk(text)
        else:
            on_chunk(text)

    async def _complete_once(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> Any:
        """LLM 한 판. 스트리밍이 안 되는 엔드포인트면 한 번만 비스트리밍으로 되묻습니다."""
        kwargs = self.build_completion_kwargs(agent, messages, tools)
        streamed_any = False
        try:
            response = await litellm.acompletion(**kwargs, stream=True)
            chunks = []
            async for chunk in response:
                chunks.append(chunk)
                content_delta = (
                    chunk.choices[0].delta.content
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content
                    else None
                )
                if content_delta and on_chunk:
                    streamed_any = True
                    await self._emit_chunk(on_chunk, content_delta)
            complete_response = litellm.stream_chunk_builder(chunks, messages=messages)
            return complete_response.choices[0].message
        except Exception as exc:
            # 이미 화면에 흘려보낸 조각이 있으면 비스트리밍으로 다시 부르지 않습니다.
            # 같은 답변이 두 번 붙어 버리고, 무엇보다 이 실패는 삼킬 것이 아니라
            # 발언자에게 그대로 전달되어야 합니다 (LLMUnavailableError).
            if streamed_any:
                raise
            logger.warning(
                f"Streaming completion failed or not supported for {agent.name} ({exc}); "
                f"retrying without stream"
            )
            response = await litellm.acompletion(**kwargs)
            message = response.choices[0].message
            if message.content and on_chunk:
                await self._emit_chunk(on_chunk, message.content)
            return message

    # ------------------------------------------------------------ 도구 예산

    @staticmethod
    def _append_budget_notice(messages: List[Dict[str, Any]], text: str) -> None:
        """예산 고지를 대화 끝에 붙입니다.

        마지막 메시지가 user 면 그 안에 이어 붙입니다. 새 메시지로 넣으면 user 가
        연달아 두 번이 되어 Anthropic·Gemini 와 여러 OpenAI 호환 셔임이 400 으로
        거절합니다 (`merge_consecutive_roles` 의 사연과 같습니다). 도구 결과 뒤라면
        role 이 tool 이므로 그냥 새로 붙입니다.
        """
        last = messages[-1] if messages else None
        if last is not None and last.get("role") == "user" and isinstance(last.get("content"), str):
            messages[-1] = {**last, "content": f"{last['content']}\n\n{text}"}
            return
        messages.append({"role": "user", "content": text})

    async def _wrap_up_without_tools(
        self,
        agent: Agent,
        current_messages: List[Dict[str, Any]],
        segments: List[str],
        tool_logs: List[Dict[str, Any]],
        limit: int,
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """예산이 바닥난 자리에서 도구 없이 결론만 받아 냅니다.

        예전에는 여기서 `LLMUnavailableError` 를 올렸습니다. 그러면 그때까지
        스트리밍으로 흘러나온 글과 실행된 도구 관측이 전부 실패 안내로 덮여
        사라졌습니다 — 도구를 많이 쓴 긴 발언일수록 잃는 것이 컸고, 정작 원인은
        엔드포인트 장애가 아니라 우리가 정한 상한이었습니다. 상한은 폭주를
        막으라고 있는 것이지, 한 일을 버리라고 있는 것이 아닙니다.
        """
        logger.warning(
            f"Tool budget exhausted for {agent.name}: {limit} call(s), "
            f"{len(tool_logs)} tool execution(s). Asking for a final answer without tools."
        )
        self._append_budget_notice(
            current_messages,
            BUDGET_EXHAUSTED_INSTRUCTION.format(limit=limit, tool_calls=len(tool_logs)),
        )
        try:
            # tools 를 아예 넘기지 않습니다. 상한을 알려 주고도 도구 목록을 함께
            # 주면 모델은 또 부르려 하고, 그 호출은 실행되지 않은 채 버려집니다.
            message = await self._complete_once(agent, current_messages, None, on_chunk)
            final = self._compose_content(agent, message).strip()
            if final:
                segments.append(final)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Final tool-free answer failed for {agent.name}: {exc}")
            if not segments:
                raise LLMUnavailableError(
                    agent,
                    f"도구 호출 상한 {limit}회를 모두 쓴 뒤의 마무리 호출도 실패했습니다: "
                    f"{type(exc).__name__}: {exc}",
                ) from exc

        segments.append(BUDGET_WRAP_UP_FOOTER.format(limit=limit, tool_calls=len(tool_logs)))
        return "\n\n".join(segments), tool_logs

    async def _run_litellm_loop(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        max_tool_iterations: Optional[int] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[BudgetArbiter] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """도구 루프. 상한은 두 겹의 안전장치와 함께 돕니다.

        1. **미리 고지** — 매 판이 시작되기 전에 남은 호출 횟수를 에이전트에게
           알립니다. 한계에 가까워질수록 촘촘해집니다 (`tool_budget_notice`).
           남은 예산을 알아야 모델이 결론을 쓸 자리를 스스로 남겨 둡니다.
        2. **소진해도 버리지 않음** — 다 쓰면 `budget_arbiter` 로 사람에게 상한
           확장을 묻고, 확장을 받으면 이어서 돕니다. 못 받으면 도구를 떼고
           "지금까지 얻은 것으로 즉시 결론을 내라" 고 한 판 더 부릅니다. 어느
           쪽이든 그때까지의 발언과 도구 기록은 그대로 살아남습니다.
        """
        tool_logs: List[Dict[str, Any]] = []
        current_messages = list(messages)
        limit = max_tool_iterations or agent.max_tool_iterations
        used = 0

        # 이터레이션마다 나온 본문을 모읍니다.
        #
        # 도구를 부르는 판의 텍스트("먼저 파일을 확인하겠습니다…", 도구 결과에 대한
        # 관측과 판단)도 엄연히 그 에이전트의 발언입니다. 화면에는 `on_chunk` 으로
        # 이미 흘러가 있고요. 그런데 예전에는 마지막 판(도구를 부르지 않은 판)의
        # content 만 돌려주었습니다. 그 값이 화면 카드를 통째로 덮어쓰고 DB 에도
        # 그대로 들어가므로, 도구를 많이 부른 긴 발언일수록 사람이 읽고 있던 글이
        # 발언이 끝나는 순간 사라졌습니다 (짧은 답변은 이터레이션이 한 번이라
        # 마지막 판 == 전체였고, 그래서 이 손실이 오래 눈에 띄지 않았습니다).
        segments: List[str] = []

        while True:
            while used < limit:
                if tools:
                    notice = tool_budget_notice(
                        remaining=limit - used, limit=limit, used=used, tool_calls=len(tool_logs)
                    )
                    if notice:
                        self._append_budget_notice(current_messages, notice)

                message = await self._complete_once(agent, current_messages, tools, on_chunk)
                used += 1

                segment = self._compose_content(agent, message)
                if segment.strip():
                    segments.append(segment.strip())

                # Check for tool calls
                tool_calls = getattr(message, "tool_calls", None)
                if not tool_calls:
                    return "\n\n".join(segments), tool_logs

                # Append assistant message with tool calls to context
                current_messages.append(
                    message.model_dump() if hasattr(message, "model_dump") else dict(message)
                )

                # Execute all requested tool calls
                for tc in tool_calls:
                    fn_name = tc.function.name
                    fn_args_raw = tc.function.arguments
                    try:
                        fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                    except Exception:
                        fn_args = {"raw": fn_args_raw}

                    output, status = await self.mcp_manager.execute_tool(
                        fn_name, fn_args, scope=session_id, actor=agent.key
                    )
                    call_log = {
                        "tool_name": fn_name,
                        "arguments": fn_args,
                        "output": output,
                        "status": status,
                    }
                    tool_logs.append(call_log)
                    if on_tool_call:
                        if asyncio.iscoroutinefunction(on_tool_call):
                            await on_tool_call(call_log)
                        else:
                            on_tool_call(call_log)

                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": output,
                    })

            # 예산 소진. 여기서 예외를 올리면 지금까지의 발언이 통째로 사라집니다.
            # 사람에게 한 번 묻고, 답이 없으면 에이전트에게 즉시 끝내라고 합니다.
            granted = 0
            if budget_arbiter is not None:
                try:
                    granted = int(await budget_arbiter({
                        "agent_key": agent.key,
                        "agent_name": agent.name,
                        "limit": limit,
                        "used": used,
                        "tool_calls": len(tool_logs),
                    }) or 0)
                except Exception as exc:  # noqa: BLE001 - 묻다가 실패해도 발언은 살립니다
                    logger.warning(f"Tool budget arbiter failed for {agent.name}: {exc}")
                    granted = 0

            if granted > 0:
                limit += granted
                logger.info(
                    f"Tool budget for {agent.name} extended by {granted} to {limit} by the user"
                )
                self._append_budget_notice(
                    current_messages,
                    BUDGET_EXTENDED_INSTRUCTION.format(
                        extra=granted, limit=limit, remaining=limit - used
                    ),
                )
                continue

            return await self._wrap_up_without_tools(
                agent, current_messages, segments, tool_logs, limit, on_chunk
            )
