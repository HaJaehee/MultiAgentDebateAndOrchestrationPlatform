"""LLM 호출기: 에이전트 한 명의 발언을 LiteLLM 으로 받아 옵니다.

시스템 프롬프트를 짓고, 도구 루프를 돌리고(도구 호출 → 보안 판정 → 실행 → 관측), 한도에 닿으면 사람에게
묻거나 마무리합니다. 엔드포인트에 닿지 못하면 `LLMUnavailableError` 를 올리고, 대신 답을 지어내지 않습니다.

곁가지는 옆 모듈에 있습니다: 사고 과정(`reasoning`), 컨텍스트 창 맞춤(`llm_context`), 한도 안내
(`llm_budget`), 도구 이름·쓰기 지침(`llm_tools`), 요청 지문(`llm_fingerprint`), 응답 되살리기
(`llm_recovery`), 도구 호출 처리(`llm_tool_calls`), 예외와 콜백 형식(`llm_types`), 전역 모델 폴백
(`llm_fallback`), 서버 전체 동시 요청 상한(`llm_gate`).
"""

import asyncio
import contextvars
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import litellm

from app.agents import llm_fallback
from app.agents.base import Agent
from app.agents.connection import SERVER_ERROR, classify
from app.agents.consultation import (
    PEERS_TOOL_PREFIX,
    consultation_guidance,
    consultation_tools_for,
    is_consultation_tool,
)
from app.agents.llm_budget import (
    BUDGET_EXTENDED_INSTRUCTION,
    completion_budget_report,
    limit_reached_notice,
    message_text,
    reasoning_heavy_note,
    tool_budget_notice,
    truncation_advice,
)
from app.agents.llm_context import (
    context_budget,
    context_pressure_notice,
    effective_max_tokens,
    estimate_tokens,
    fit_context_window,
    fit_tool_loop_context,
    max_tokens_label,
    merge_consecutive_roles,
    place_ledger_last,
    tool_schema_tokens,
)
from app.agents.llm_fingerprint import request_fingerprint
from app.agents.llm_gate import get_llm_gate
from app.agents.llm_recovery import (
    LEAKED_TOOL_CALL_FOOTER,
    LEAKED_TOOL_CALL_PLACEHOLDER,
    MAX_LEAKED_TOOL_CALL_RETRIES,
    AnswerRecoveryMixin,
    find_leaked_tool_call,
    leaked_tool_call_notice,
)
from app.agents.llm_tool_calls import ToolCallMixin
from app.agents.llm_tools import (
    binary_file_guidance,
    file_writing_guidance,
    memory_search_tool,
    memory_write_tool,
)
from app.agents.llm_types import BudgetArbiter, ContextArbiter, LLMUnavailableError, SpeechCheckpoint
from app.agents.reasoning import strip_reasoning_trace
from app.agents.skills import is_skill_tool, skill_guidance, skill_tools_for
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool

logger = logging.getLogger(__name__)

# Suppress litellm verbose logging
litellm.suppress_debug_info = True

# Placeholder key for endpoints that require no authentication (Ollama, vLLM, LM Studio...)
LOCAL_API_KEY_PLACEHOLDER = "sk-no-key-required"


# ---------------------------------------------------------------- 도구 예산 고지


# 초안의 형식. 필드를 바꾸면 올립니다 — 모르는 형식의 초안은 이어 가지 않고 발언을 처음부터 합니다.
SPEECH_STATE_VERSION = 1

# 도구 단위로 이어 가는 발언의 첫 판 앞에 붙는 안내.
RESUMED_SPEECH_NOTICE = (
    "[재개] 서버가 다시 시작되어 이 발언이 잠시 끊겼다가 이어집니다. 위 대화는 끊기기 전까지의 "
    "진행 그대로입니다 — 앞서 쓴 글을 되풀이하거나 이미 결과를 받은 도구를 다시 실행하지 말고, "
    "멈춘 자리에서 이어 가세요."
)

# 끊긴 순간 실행 중이던 도구 호출에 채우는 결과. 짝 없는 tool_call 을 남기면 다음 요청이 400 입니다.
UNKNOWN_TOOL_RESULT = (
    "[결과 모름] 서버가 다시 시작되어 이 도구 호출의 결과를 받지 못했습니다. 실행되었는지 알 수 "
    "없습니다. 결과가 필요하면(특히 파일 쓰기·명령 실행) 먼저 지금 상태를 확인한 뒤 다시 호출할지 "
    "정하세요."
)


def close_open_tool_calls(messages: List[Dict[str, Any]]) -> List[str]:
    """마지막 assistant 발언이 부른 도구 중 결과가 없는 것에 "결과 모름" 을 채웁니다. 채운 도구 이름.

    도구를 부른 발언과 그 결과는 짝이 맞아야 합니다 (`_assistant_turn`). 서버가 도구를 실행하는
    도중에 내려가면 부른 기록만 있고 결과가 없는 호출이 남습니다. 그 자리를 비워 두면 요청이
    거절되고, 지어낸 결과를 넣으면 모델이 그것을 사실로 읽습니다. 모른다고 적습니다.
    """
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls") or []
        if not calls:
            return []
        answered = {
            m.get("tool_call_id") for m in messages[index + 1:] if m.get("role") == "tool"
        }
        missing: List[str] = []
        for call in calls:
            call_id = call.get("id")
            if call_id in answered:
                continue
            name = ((call.get("function") or {}).get("name")) or "unknown_tool"
            messages.append({
                "role": "tool", "tool_call_id": call_id, "name": name, "content": UNKNOWN_TOOL_RESULT,
            })
            missing.append(name)
        return missing
    return []


# 예산이 바닥나 도구 없이 마무리했다는 사실을 발언 끝에 남깁니다. 화면과 기록
# 모두에서 "왜 여기서 멈췄는가" 가 보여야, 사람이 상한을 올릴지 판단할 수 있습니다.
# ------------------------------------------------------------ 컨텍스트 창 포화


CONTEXT_WIDENED_INSTRUCTION = (
    "[컨텍스트 확장] 유저가 이 발언의 컨텍스트 한도를 {extra:,} 토큰 늘려 "
    "총 {window:,} 토큰이 되었습니다. 앞선 기록이 생략되지 않고 그대로 남았습니다."
)

TRUNCATED_TOOL_CALL_NOTICE = (
    "[출력 잘림] 직전 도구 호출은 응답 한도(max_tokens={max_tokens})에 걸려 "
    "**인자를 끝까지 쓰지 못한 채 잘렸습니다.** 그래서 그 호출은 의도한 대로 "
    "실행되지 않았습니다 (도구 서버가 인자를 읽지 못했다고 답했을 것입니다).\n"
    "같은 내용을 그대로 다시 보내지 마세요 — 또 같은 자리에서 잘립니다.{advice}"
)

# ---------------------------------------------------------------- 사고만 하다 한도에 닿음
#
# 추론 모델(reasoning_content 를 따로 주는 모델)은 속으로 생각하는 토큰도 `max_tokens` 에
# 넣어 셉니다. 생각이 길면 본문을 한 글자도 못 쓰고 `length` 로 끝납니다. 예전에는 그
# 발언에 "잘렸습니다" 꼬리표만 남았습니다 — 이어받을 글이 없었으니까요.


# ---------------------------------------------------------------- 실패 지문


@dataclass
class _SpeechProgress:
    """이번 `call_agent` 에서 모델이 무엇을 내놓았는지.

    전역 기본 모델로 물러서는 것(`llm.fallback`)은 모델이 **아무것도 내놓기 전**에만 합니다. 그 판단에
    쓰는 표시이고, 도구 루프 곳곳에 인자를 늘리지 않으려고 컨텍스트 변수로 둡니다 (`LLM_HOLDER` 와 같은
    방식). 병렬 발언은 태스크마다 컨텍스트가 따로라 서로 섞이지 않습니다.
    """

    rounds: int = 0            # 끝까지 받은 LLM 판 수 — 0 이 아니면 모델이 만든 도구 호출이 기록에 있습니다
    streamed: bool = False     # 화면으로 흘려보낸 글이 있는지
    llm_failed: bool = False   # 실패가 LLM 호출에서 났는지 (도구 루프의 다른 곳에서 난 예외와 가릅니다)
    can_switch: bool = True    # 실패하면 물러설 수 있는 발언인지 (끊긴 발언을 이어 가는 중이면 아닙니다)


_SPEECH_PROGRESS: "contextvars.ContextVar[Optional[_SpeechProgress]]" = contextvars.ContextVar(
    "mado_speech_progress", default=None
)


class LLMCaller(AnswerRecoveryMixin, ToolCallMixin):
    """Executes LLM completions with an MCP tool-calling loop.

    호출이 실패하면 `LLMUnavailableError` 를 올립니다. 대체 응답을 만들어 내지 않습니다.
    `llm.fallback` 을 켠 배치에서는 개별 모델의 연결 실패에 한해 전역 기본 모델이 대신 답하고,
    그 사실을 걸어 둔 곳에 알립니다 (`app/agents/llm_fallback.py`).
    """

    def __init__(self, mcp_manager: Optional[MCPManager] = None):
        # 기본 런타임. 실제 토론은 발언마다 **그 대화의 작업 공간에 해당하는**
        # 런타임을 `call_agent(mcp=...)` 로 받아 씁니다. 이 값은 그것이 주어지지
        # 않았을 때의 폴백입니다.
        self.mcp_manager = mcp_manager or get_runtime_pool().default_runtime()

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

    async def call_agent(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[BudgetArbiter] = None,
        context_arbiter: Optional[ContextArbiter] = None,
        on_context_trim: Optional[Callable[[int], Any]] = None,
        mcp: Optional[MCPManager] = None,
        ledger: str = "",
        tool_gate: Optional[Any] = None,
        checkpoint: Optional[SpeechCheckpoint] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        preload_skills: Sequence[str] = (),
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Executes a turn for the given agent.
        Returns (response_text, tool_call_logs).

        `preload_skills` 는 유저가 이 에이전트에게 지정한 스킬입니다 (입력창의 `@전문가 @스킬`).
        첫 LLM 호출 전에 호스트가 `load_skill` 을 대신 불러 결과를 넣어 둡니다 (`_preload_skills`).
        이어 가는 발언에서는 이미 메시지에 들어 있으므로 다시 부르지 않습니다.

        `checkpoint` 는 도구 루프가 한 판을 마칠 때마다(도구를 부른 직후, 도구 결과를 받을 때마다)
        이어 갈 수 있는 상태를 넘겨받는 콜백입니다 (ADR-025). `resume_state` 는 그렇게 남긴 상태로,
        주면 프롬프트를 새로 만들지 않고 그 상태의 메시지에서 다음 LLM 호출부터 이어 갑니다.

        `mcp` 는 이 발언이 쓸 MCP 런타임입니다. 대화마다 작업 공간이 다를 수
        있고 런타임은 작업 공간마다 따로 뜨므로, 어느 것을 쓸지는 턴을 여는
        쪽(`OrchestratorEngine.run_turn`)이 정해 내려보냅니다. 주지 않으면
        기본 런타임을 씁니다.

        `session_id` 는 MCP 도구 호출의 스코프로 함께 보내집니다. 이걸 빠뜨리면
        서버가 대화를 구분할 수 없어 다른 대화의 상태(지식 그래프 등)를 봅니다.
        발언자(`agent.key`)도 함께 실려서, 커널처럼 에이전트 단위로 나뉘어야 하는
        상태를 서버가 구분할 수 있습니다 (`MCPManager.compose_scope`).

        `budget_arbiter` 는 도구 호출 상한에 닿았을 때 사람에게 확장을 물어보는
        통로입니다. 주지 않으면 확장 없이, 도구를 떼고 결론만 받아 마무리합니다
        (`_run_litellm_loop`). 어느 쪽이든 발언이 통째로 버려지지는 않습니다.

        `context_arbiter` 는 컨텍스트 창이 넘쳐 기록을 버려야 할 때의 같은 통로이고,
        `on_context_trim` 은 실제로 생략이 일어났음을 화면에 알리는 콜백입니다.

        `ledger` 는 이 대화의 결정 장부입니다. 마지막 사용자 메시지 앞에 붙습니다
        (`place_ledger_last`) — 시스템 프롬프트에 두면 갱신될 때마다 캐시가 깨집니다.

        `llm.fallback` 이 켜져 있으면, 이 에이전트의 연결이 실패했을 때 전역 기본 모델이 대신 답합니다
        (ADR-030). 물러서는 것은 모델이 아무것도 내놓기 전(첫 판이 실패했을 때)뿐이고, 끊긴 발언을 이어
        가는 중(`resume_state`)에는 물러서지 않습니다. 전환은 `llm_fallback.listening(...)` 으로 걸어 둔
        곳에 `FallbackNotice` 로 알립니다. 기본 모델을 부르기 **전에** 알리므로, 화면은 흘러나오는 글 위에
        "누가 답하고 있는지" 를 먼저 붙일 수 있습니다.

        `tool_gate` 는 도구 보안 문지기입니다 (`app/orchestration/tool_gate.py`). 늘 거부될
        도구를 목록에서 빼고, 호출마다 허용·묻기·거부를 판정합니다. 주지 않으면 판정 없이
        실행하고, 매니저의 고정 보호만 걸립니다.
        """
        # 순환 import: 폴백 → 모델 연결 상태 → 이 모듈.
        async def run(
            target: Agent,
            tool_call_callback: Optional[Callable[[Dict[str, Any]], Any]],
            trim_callback: Optional[Callable[[int], Any]],
        ) -> Tuple[str, List[Dict[str, Any]]]:
            return await self._call_agent_once(
                target, messages, custom_instructions,
                on_tool_call=tool_call_callback, on_chunk=on_chunk, session_id=session_id,
                budget_arbiter=budget_arbiter, context_arbiter=context_arbiter,
                on_context_trim=trim_callback, mcp=mcp, ledger=ledger, tool_gate=tool_gate,
                checkpoint=checkpoint, resume_state=resume_state, preload_skills=preload_skills,
            )

        route = llm_fallback.route_for(agent)
        if route is None:
            return await run(agent, on_tool_call, on_context_trim)

        # 끊긴 발언을 이어 가는 중이면 앞서 모델이 만든 도구 호출이 메시지에 들어 있습니다. 그것을 다른
        # 모델에게 넘기지 않습니다 (Provider 에 따라 거절합니다).
        can_switch = resume_state is None
        if can_switch:
            known = route.known_failure()
            if known is not None:
                # 조금 전에 실패한 연결입니다. 발언마다 타임아웃을 다시 치르지 않고 곧바로 기본 모델로 갑니다.
                return await self._answer_on_fallback(
                    route, run, on_tool_call, on_context_trim, state=known, skipped=True,
                )

        # 첫 판이 실패하면 처음부터 다시 부릅니다. 그때 두 번 알리지 않도록, 지정 스킬을 미리 부른 기록은
        # 몇 건을 알렸는지 세어 두고, 컨텍스트 생략은 이 시도가 끝까지 갔을 때만 알립니다.
        announced = 0
        trims: List[int] = []

        async def counting_tool_call(call_log: Dict[str, Any]) -> None:
            nonlocal announced
            announced += 1
            await self._notify_tool_call(agent, on_tool_call, call_log)

        progress = _SpeechProgress(can_switch=can_switch)
        token = _SPEECH_PROGRESS.set(progress)
        try:
            result = await run(agent, counting_tool_call, trims.append)
        except LLMUnavailableError as exc:
            # LLM 호출이 연결 문제로 실패한 경우만 봅니다. 도구 루프의 다른 곳에서 난 예외나, 상한에 닿아
            # 올린 `LLMUnavailableError` 는 이 연결의 문제가 아닙니다.
            cause = exc.__cause__ if progress.llm_failed else None
            state = classify(cause) if cause is not None else None
            if state not in llm_fallback.FALLBACK_STATES:
                raise
            # 물러서지 못하는 판이어도, 이 연결이 안 된다는 것은 다음 발언이 알아야 합니다.
            route.trip(state)
            if not can_switch or progress.rounds or progress.streamed:
                raise
            primary_error = exc
        else:
            if on_context_trim:
                for dropped in trims:
                    on_context_trim(dropped)
            return result
        finally:
            _SPEECH_PROGRESS.reset(token)

        return await self._answer_on_fallback(
            route, run, on_tool_call, on_context_trim,
            state=state, skipped=False, primary_error=primary_error, already_announced=announced,
        )

    async def _answer_on_fallback(
        self,
        route: Any,
        run: Callable[..., Awaitable[Tuple[str, List[Dict[str, Any]]]]],
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]],
        on_context_trim: Optional[Callable[[int], Any]],
        *,
        state: str,
        skipped: bool,
        primary_error: Optional[LLMUnavailableError] = None,
        already_announced: int = 0,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """전역 기본 모델로 같은 발언을 처음부터 받습니다. 그것도 실패하면 두 이유를 함께 올립니다."""
        agent, substitute = route.primary, route.substitute
        notice = route.notice(state, skipped=skipped)
        logger.warning(
            f"Falling back for {agent.name}: {agent.model} is unavailable ({state}"
            f"{', skipped without calling' if skipped else ''}); answering with the global model {substitute.model}"
        )
        on_fallback = llm_fallback.current_listener()
        if on_fallback is not None:
            try:
                outcome = on_fallback(notice)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 알리다 실패해도 발언은 받습니다
                logger.warning(f"Fallback notification failed for {agent.name}: {type(exc).__name__}: {exc}")

        remaining = already_announced

        async def tool_call_once(call_log: Dict[str, Any]) -> None:
            # 앞선 시도가 이미 알린 것(지정 스킬을 미리 부른 기록)은 다시 알리지 않습니다.
            nonlocal remaining
            if remaining > 0:
                remaining -= 1
                return
            await self._notify_tool_call(agent, on_tool_call, call_log)

        try:
            return await run(substitute, tool_call_once, on_context_trim)
        except LLMUnavailableError as exc:
            first = primary_error.reason if primary_error is not None else (
                f"{notice.label} (조금 전에 실패한 연결이라 다시 부르지 않았습니다)"
            )
            raise LLMUnavailableError(
                agent,
                f"{first} — 기본 모델 `{substitute.model}` 로도 응답을 받지 못했습니다: {exc.reason}",
            ) from exc

    async def _call_agent_once(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        session_id: Optional[str] = None,
        budget_arbiter: Optional[BudgetArbiter] = None,
        context_arbiter: Optional[ContextArbiter] = None,
        on_context_trim: Optional[Callable[[int], Any]] = None,
        mcp: Optional[MCPManager] = None,
        ledger: str = "",
        tool_gate: Optional[Any] = None,
        checkpoint: Optional[SpeechCheckpoint] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        preload_skills: Sequence[str] = (),
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """`agent` 의 연결 그대로 한 번 부릅니다 (`call_agent` 가 물러설지 말지를 정합니다)."""
        # Retrieve available tools for this agent
        #
        # 시스템 프롬프트보다 먼저 구합니다. 파일 쓰기 지침이 이 에이전트가 실제로
        # 가진 도구의 이름을 짚기 때문입니다 (`file_writing_guidance`).
        #
        # 자르기보다도 먼저입니다. 무엇이 잘렸는지 알리는 문구가 "메모리 그래프에서
        # 찾아보라" 로 바뀌려면, 이 에이전트가 그 도구를 실제로 갖고 있는지 알아야
        # 합니다 (서버를 껐거나 연결에 실패했으면 없는 도구를 가리키게 됩니다).
        mcp = mcp or self.mcp_manager
        tools = mcp.get_openai_tools_for_servers(self.resolve_tool_servers(agent))
        if tool_gate is not None:
            tools = tool_gate.filter_tools(agent.key, tools, mcp)
        # 스킬 도구는 발언이 진행될 때마다 스킬 디렉터리와 활성화 여부를 새로 조회하여 동적으로 생성합니다 (`app/agents/skills.py`).
        tools = tools + skill_tools_for(agent) + consultation_tools_for(agent)

        if resume_state is not None:
            # 끊긴 발언을 이어 갑니다. 모델이 보던 메시지를 그대로 씁니다 — 프롬프트를 다시 만들면
            # 그 사이 바뀐 장부·요약이 섞여, 모델이 보던 것과 다른 대화가 됩니다.
            formatted_messages = [dict(m) for m in resume_state.get("messages") or []]
            turn_anchor = str(resume_state.get("turn_anchor") or "")
        else:
            formatted_messages = [
                {"role": "system",
                 "content": self.build_system_prompt(agent, custom_instructions, tools)}
            ]
            prompt = place_ledger_last(messages, ledger)
            formatted_messages.extend(prompt)
            # 발언을 시작할 때의 마지막 사용자 메시지(장부 + 이번 차례 지시). 도구 루프가 넘쳐
            # 이것을 버리게 되면 다시 붙입니다 (`fit_tool_loop_context` 의 `keep`).
            turn_anchor = (
                prompt[-1]["content"]
                if prompt and prompt[-1].get("role") == "user" and isinstance(prompt[-1].get("content"), str)
                else ""
            )

            # 순서 주의: 먼저 한도에 맞춰 자르고, 그 다음 role 을 합칩니다. 생략 안내가
            # user 로 들어가므로 합치기를 나중에 해야 교대가 보장됩니다.
            formatted_messages, trimmed = fit_context_window(
                agent, formatted_messages, memory_search_tool(tools), tools=tools,
            )
            if trimmed and on_context_trim:
                on_context_trim(trimmed)
            formatted_messages = merge_consecutive_roles(formatted_messages)

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
                context_arbiter=context_arbiter, on_context_trim=on_context_trim,
                mcp=mcp, turn_anchor=turn_anchor, tool_gate=tool_gate,
                checkpoint=checkpoint, resume_state=resume_state, preload_skills=preload_skills,
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
        tool_choice: str = "auto",
    ) -> Tuple[Any, str]:
        """LLM 한 판. `(message, finish_reason)`.

        `finish_reason` 을 함께 돌려주는 이유: `"length"` 는 **모델이 하던 말을
        끝내지 못하고 잘렸다**는 뜻입니다. 그 판이 도구 호출이었다면 인자 JSON 이
        중간에서 끊기고, 도구는 인자를 읽지 못해 실패합니다. 그 사실을 모르면
        모델은 같은 호출을 그대로 다시 시도하다 예산만 태웁니다.

        스트리밍이 안 되는 엔드포인트면 한 번만 비스트리밍으로 되묻습니다.

        서버 전체의 동시 요청 상한(`app.llm_concurrency`)이 있으면 자리가 날 때까지
        여기서 기다립니다. 스트리밍이 끝날 때까지 자리를 쥡니다 (`app/agents/llm_gate.py`).
        """
        progress = _SPEECH_PROGRESS.get()
        try:
            async with get_llm_gate().slot():
                result = await self._complete_unthrottled(agent, messages, tools, on_chunk, tool_choice)
        except Exception:
            if progress is not None:
                progress.llm_failed = True
            raise
        if progress is not None:
            progress.rounds += 1
        return result

    async def _complete_unthrottled(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        on_chunk: Optional[Callable[[str], Any]],
        tool_choice: str,
    ) -> Tuple[Any, str]:
        kwargs = self.build_completion_kwargs(agent, messages, tools, tool_choice)
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
                    progress = _SPEECH_PROGRESS.get()
                    if progress is not None:
                        progress.streamed = True
                    await self._emit_chunk(on_chunk, content_delta)
            complete_response = litellm.stream_chunk_builder(chunks, messages=messages)
            return complete_response.choices[0].message, self._finish_reason(complete_response)
        except Exception as exc:
            # 이미 화면에 흘려보낸 조각이 있으면 비스트리밍으로 다시 부르지 않습니다.
            # 같은 답변이 두 번 붙어 버리고, 무엇보다 이 실패는 삼킬 것이 아니라
            # 발언자에게 그대로 전달되어야 합니다 (LLMUnavailableError).
            if streamed_any:
                logger.error(request_fingerprint(agent, messages, tools, tool_choice))
                raise
            if self._will_fall_back(exc):
                # 연결 자체가 안 됩니다. 같은 연결로 스트리밍 없이 다시 물어도 같은 답이고, 그만큼
                # 기본 모델로 넘어가는 것만 늦어집니다 (`call_agent`).
                raise
            logger.warning(
                f"Streaming completion failed or not supported for {agent.name} ({exc}); "
                f"retrying without stream"
            )
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception:
                # 스트리밍도 비스트리밍도 안 됩니다. 이건 엔드포인트가 요청 자체를
                # 거절한 것이므로, 우리가 무엇을 보냈는지 남깁니다 — 상대가 이유를
                # 알려 주지 않을 때 유일하게 남는 단서입니다.
                logger.error(request_fingerprint(agent, messages, tools, tool_choice))
                raise
            message = response.choices[0].message
            if message.content and on_chunk:
                await self._emit_chunk(on_chunk, message.content)
            return message, self._finish_reason(response)

    @staticmethod
    def _will_fall_back(exc: BaseException) -> bool:
        """이 실패로 `call_agent` 가 곧 전역 기본 모델로 물러설 것인지 (첫 판이고, 연결이 안 될 때).

        서버 오류는 여기서 빼 둡니다. 스트리밍을 받지 않는 엔드포인트가 5xx 로 답하는 경우가 있어,
        그때는 지금처럼 스트리밍 없이 한 번 더 물어본 뒤에 판단합니다.
        """
        progress = _SPEECH_PROGRESS.get()
        if progress is None or not progress.can_switch or progress.rounds or progress.streamed:
            return False

        state = classify(exc)
        return state in llm_fallback.FALLBACK_STATES and state != SERVER_ERROR


    # ------------------------------------------------------------ 도구 예산


    # ------------------------------------------------------------ 도구 실행 방어


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
        context_arbiter: Optional[ContextArbiter] = None,
        on_context_trim: Optional[Callable[[int], Any]] = None,
        mcp: Optional[MCPManager] = None,
        turn_anchor: str = "",
        tool_gate: Optional[Any] = None,
        checkpoint: Optional[SpeechCheckpoint] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        preload_skills: Sequence[str] = (),
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """도구 루프. 상한은 두 겹의 안전장치와 함께 돕니다.

        1. **미리 고지** — 매 판이 시작되기 전에 남은 호출 횟수를 에이전트에게
           알립니다. 한계에 가까워질수록 촘촘해집니다 (`tool_budget_notice`).
           남은 예산을 알아야 모델이 결론을 쓸 자리를 스스로 남겨 둡니다.
        2. **소진해도 버리지 않음** — 다 쓰면 `budget_arbiter` 로 사람에게 상한
           확장을 묻고, 확장을 받으면 이어서 돕니다. 못 받으면 도구를 떼고
           "지금까지 얻은 것으로 즉시 결론을 내라" 고 한 판 더 부릅니다. 어느
           쪽이든 그때까지의 발언과 도구 기록은 그대로 살아남습니다.

        컨텍스트 창도 같은 두 겹으로 지킵니다. 매 판 직전에 `fit_tool_loop_context`
        로 창 안에 맞추고(짝 단위로 덜어냅니다 — 고아 `tool` 메시지는 400 입니다),
        사용률이 띠를 넘으면 `context_pressure_notice` 로 알립니다. 기록을 실제로
        버려야 하는 첫 순간에는 `context_arbiter` 로 사람에게 묻습니다.
        """
        tool_logs: List[Dict[str, Any]] = []
        current_messages = list(messages)
        limit = max_tool_iterations or agent.max_tool_iterations
        used = 0

        # 컨텍스트 쪽 상태. `window` 는 사람이 넓혀 줄 수 있으므로 에이전트 설정을
        # 그대로 쓰지 않고 이 발언 동안의 유효값으로 들고 다닙니다.
        window = agent.max_context_window
        announced_bands: set = set()
        memory_write = memory_write_tool(tools)
        memory_search = memory_search_tool(tools)
        context_asked = False
        # 도구 정의는 요청마다 나가는 입력이라 컨텍스트 예산에서 뺍니다 (`context_budget`).
        schema_tokens = tool_schema_tokens(agent.model, tools)
        # 호출 표식이 본문으로 새어 나와 다시 부르게 한 횟수 (`MAX_LEAKED_TOOL_CALL_RETRIES`).
        leaked_retries = 0

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

        # 끊긴 발언을 이어 가면 판 단위로 남긴 상태에서 시작합니다 (ADR-025). 끊긴 순간 실행 중이던
        # 도구에는 "결과 모름" 을 채우고, 이어진다는 사실을 모델에게 알립니다.
        if resume_state is not None:
            current_messages = [dict(m) for m in resume_state.get("messages") or current_messages]
            segments = list(resume_state.get("segments") or [])
            used = int(resume_state.get("used") or 0)
            limit = max(limit, int(resume_state.get("limit") or 0))
            window = int(resume_state.get("window") or window)
            tool_logs = [dict(log) for log in resume_state.get("tool_logs") or []]
            leaked_retries = int(resume_state.get("leaked_retries") or 0)
            context_asked = bool(resume_state.get("context_asked"))
            announced_bands = set(resume_state.get("announced_bands") or [])
            unknown = close_open_tool_calls(current_messages)
            notice = RESUMED_SPEECH_NOTICE
            if unknown:
                notice += f" 결과를 받지 못한 호출: {', '.join(unknown)}."
            self._append_budget_notice(current_messages, notice)

        async def save_progress() -> None:
            """지금까지를 이어 갈 수 있게 넘깁니다. 넘기다 실패해도 발언은 계속됩니다."""
            if checkpoint is None:
                return
            try:
                await checkpoint({
                    "version": SPEECH_STATE_VERSION,
                    "messages": current_messages,
                    "segments": segments,
                    "used": used,
                    "limit": limit,
                    "window": window,
                    "tool_logs": tool_logs,
                    "leaked_retries": leaked_retries,
                    "context_asked": context_asked,
                    "announced_bands": sorted(announced_bands, key=str),
                    "turn_anchor": turn_anchor,
                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Could not save the progress of {agent.name}'s speech: {exc}")

        # 유저가 지정한 스킬을 첫 판 전에 불러 둡니다. 이어 가는 발언은 이미 들어 있습니다.
        if resume_state is None and preload_skills:
            if await self._preload_skills(
                agent, preload_skills, current_messages, tool_logs, tools,
                on_tool_call=on_tool_call, session_id=session_id, mcp=mcp,
            ):
                await save_progress()

        while True:
            while used < limit:
                # --- 컨텍스트: 넘치면 짝 단위로 덜어내고, 버릴 것이 생기면 물어봅니다.
                budget = context_budget(agent, window, tools)
                over = budget > 0 and estimate_tokens(agent.model, current_messages) > budget

                if over and context_arbiter is not None and not context_asked:
                    context_asked = True
                    answer = await self._ask_context(
                        context_arbiter, agent, window, current_messages, len(tool_logs), tools
                    )
                    granted = int(answer.get("granted") or 0)
                    if granted > 0:
                        window += granted
                        logger.info(
                            f"Context window for {agent.name} widened by {granted} to {window} "
                            f"by the user"
                        )
                        self._append_budget_notice(
                            current_messages,
                            CONTEXT_WIDENED_INSTRUCTION.format(extra=granted, window=window),
                        )
                        budget = context_budget(agent, window, tools)
                        over = budget > 0 and estimate_tokens(agent.model, current_messages) > budget
                    elif answer.get("wrap_up"):
                        # 넓히지 않고 여기서 접겠다는 뜻입니다. 마무리 호출도 넘친
                        # 메시지로 나가면 400 이므로 먼저 창 안에 맞춥니다.
                        current_messages, dropped = fit_tool_loop_context(
                            agent, current_messages, window, memory_search, tools=tools,
                            keep=turn_anchor,
                        )
                        if dropped and on_context_trim:
                            on_context_trim(dropped)
                        return await self._wrap_up_without_tools(
                            agent, current_messages, segments, tool_logs, limit, on_chunk,
                            tools=tools,
                        )

                if over:
                    current_messages, dropped = fit_tool_loop_context(
                        agent, current_messages, window, memory_search, tools=tools,
                        keep=turn_anchor,
                    )
                    if dropped and on_context_trim:
                        on_context_trim(dropped)

                if tools:
                    notice = tool_budget_notice(
                        remaining=limit - used, limit=limit, used=used, tool_calls=len(tool_logs)
                    )
                    if notice:
                        self._append_budget_notice(current_messages, notice)

                    pressure = context_pressure_notice(
                        used=estimate_tokens(agent.model, current_messages),
                        budget=context_budget(agent, window, tools),
                        announced=announced_bands,
                        memory_tool=memory_write,
                        tool_calls_left=limit - used,
                    )
                    if pressure:
                        self._append_budget_notice(current_messages, pressure)

                message, finish_reason = await self._complete_once(
                    agent, current_messages, tools, on_chunk
                )
                used += 1

                segment = self._compose_content(agent, message)
                if segment.strip():
                    segments.append(segment.strip())

                # Check for tool calls
                tool_calls = getattr(message, "tool_calls", None)
                if not tool_calls:
                    # 도구 호출이 본문 글자로 새어 나왔는지 먼저 봅니다. 그렇다면 이것은
                    # 답변이 아니라 **실행되지 못한 호출**입니다. 평범한 답으로 받으면 도구는
                    # 돌지 않고, 잘렸을 때는 이어받기가 호출 표식을 산문처럼 이어 씁니다.
                    leak_at = find_leaked_tool_call(segment) if tools else None
                    if leak_at is not None:
                        kept = segment[:leak_at].strip()
                        if segment.strip():
                            segments.pop()          # 방금 넣은 이 판의 글을 표식 앞까지로 바꿉니다
                        if kept:
                            segments.append(kept)
                        logger.warning(
                            f"Tool call leaked into the text of {agent.name}'s response and was "
                            f"not executed: finish_reason={finish_reason!r}, "
                            f"max_tokens={effective_max_tokens(agent)}, "
                            + completion_budget_report(
                                message, None, None,
                                estimate_tokens(agent.model, current_messages), window, schema_tokens,
                            )
                        )
                        if leaked_retries < MAX_LEAKED_TOOL_CALL_RETRIES:
                            leaked_retries += 1
                            current_messages.append(
                                {"role": "assistant", "content": kept or LEAKED_TOOL_CALL_PLACEHOLDER}
                            )
                            self._append_budget_notice(
                                current_messages,
                                leaked_tool_call_notice(max_tokens_label(agent), finish_reason, tools),
                            )
                            continue
                        segments.append(LEAKED_TOOL_CALL_FOOTER)
                        return "\n\n".join(segments), tool_logs

                    answer_text = message_text(message).strip()
                    reasoning = getattr(message, "reasoning_content", None) or ""

                    # 한도에 닿지 않았는데 본문이 없고 사고에만 글이 있으면, 서버가 답까지
                    # 사고로 분류한 것입니다. 그대로 두면 빈 카드가 됩니다.
                    if finish_reason != "length" and not answer_text and reasoning.strip():
                        await self._answer_left_in_reasoning(
                            agent, current_messages, segments, on_chunk, reasoning, tools,
                            reasoning_shown=bool(segment.strip()),
                        )
                        return "\n\n".join(segments), tool_logs

                    # 도구를 부르지 않고 끝난 판입니다. 여기서 잘렸다면 이의를
                    # 제기해 줄 도구 서버가 없으므로 우리가 표시를 남깁니다.
                    if finish_reason == "length":
                        logger.warning(
                            f"Truncated answer from {agent.name}: finish_reason='length', "
                            f"max_tokens={effective_max_tokens(agent)}, "
                            + completion_budget_report(
                                message, None, None,
                                estimate_tokens(agent.model, current_messages), window, schema_tokens,
                            )
                        )
                    await self._finish_truncated_answer(
                        agent, current_messages, segments, finish_reason, on_chunk,
                        # 합친 글이 아니라 모델이 준 본문으로 판단합니다 (위 마무리와 같은 이유).
                        last_text=answer_text,
                        reasoning=reasoning,
                        tools=tools,
                    )
                    return "\n\n".join(segments), tool_logs

                # 실행하기 전에 먼저 다 풀어 둡니다. 되돌려 보낼 발언이 **실제로
                # 실행한 인자와 같은 모양**이어야 하기 때문입니다 (`_assistant_turn`).
                parsed = [self._parse_tool_call(tc) for tc in tool_calls]
                # 두 가지를 구분합니다. 예전에는 둘을 하나로 묶어, 인자가 멀쩡히 읽혀
                # 실행까지 된 호출에도 "잘려서 실행되지 않았다" 고 알렸습니다.
                #
                # * 인자를 못 읽었다 — 호출이 실제로 잘렸고 실행되지 않았습니다.
                # * 인자는 읽혔는데 응답이 한도에 닿았다 — 호출은 실행됩니다. 다만
                #   한도에 닿은 자리가 마지막 호출의 끝이라, 그 끝이 잘렸을 수는 있습니다.
                arguments_cut = any(not parsed_ok for _n, _a, _i, parsed_ok in parsed)
                limit_reached = finish_reason == "length" and not arguments_cut
                if arguments_cut or limit_reached:
                    spent = completion_budget_report(
                        message, tool_calls, parsed,
                        estimate_tokens(agent.model, current_messages), window, schema_tokens,
                    )
                    if arguments_cut:
                        logger.warning(
                            f"Truncated tool call from {agent.name}: "
                            f"finish_reason={finish_reason!r}, "
                            f"max_tokens={effective_max_tokens(agent)}, {spent}"
                        )
                    else:
                        logger.warning(
                            f"Response from {agent.name} reached max_tokens after readable "
                            f"tool call(s); they were executed (the last one may be cut): "
                            f"finish_reason={finish_reason!r}, "
                            f"max_tokens={effective_max_tokens(agent)}, {spent}"
                        )

                # Append assistant message with tool calls to context
                current_messages.append(self._assistant_turn(message, parsed))
                # 도구를 실행하기 전에 남깁니다. 실행 도중 끊기면 무엇을 부르던 중이었는지 압니다.
                await save_progress()

                # Execute all requested tool calls
                #
                # 이 구간은 무슨 일이 있어도 예외를 올리지 않습니다. 도구가
                # 실패하면 그 사실을 `role="tool"` 결과로 되돌려 주어야 모델이
                # 읽고 스스로 고칩니다. 여기서 예외를 올리면 발언 전체가
                # LLMUnavailableError 로 덮여, 이미 흘러간 글과 성공한 도구
                # 관측까지 통째로 사라집니다.
                for fn_name, fn_args, call_id, _parsed_ok in parsed:
                    if not fn_name:
                        # 도구 이름조차 못 읽었습니다. 모델에게 그대로 알리고
                        # 다음 판으로 넘깁니다 (고아 tool_call 을 남기면 다음
                        # 요청이 400 으로 거절됩니다).
                        output, status = (
                            "이 도구 호출은 이름을 읽을 수 없어 실행하지 못했습니다. "
                            "도구 이름과 인자를 다시 정확히 지정해 호출하세요.",
                            "error",
                        )
                        security: Dict[str, Any] = {}
                    else:
                        # 도구 보안: 허용·묻기·거부. 거부되면 실행하지 않고, 그 이유가 곧
                        # 도구 결과가 됩니다 — 모델이 읽고 다른 길을 찾습니다.
                        #
                        # 스킬 도구는 별도의 게이트 보안 검사를 거치지 않습니다. 호스트 프로세스가
                        # 스킬 디렉터리 내의 지침 파일만을 참조하며 작업 공간이나 외부 네트워크에 접근하지 않고,
                        # 사용 권한은 `allowed_skills` 및 활성화 설정을 통해 사전에 통제되기 때문입니다.
                        # (스킬 내부의 스크립트를 실제로 **실행**하는 단계는 샌드박스 도구를 통해 수행되므로,
                        # 해당 도구 호출 시 정상적으로 보안 판정을 받게 됩니다.)
                        gate = (
                            None
                            if (is_skill_tool(fn_name) or is_consultation_tool(fn_name))
                            else await self._check_tool_gate(
                                tool_gate, agent, fn_name, fn_args, mcp or self.mcp_manager,
                            )
                        )
                        if gate is not None and not gate.allowed:
                            output, status = gate.output, gate.status or "denied"
                        else:
                            output, status = await self._execute_tool_safely(
                                agent, fn_name, fn_args, session_id, mcp, tools=tools,
                            )
                        security = gate.audit if gate is not None else {}

                    call_log = {
                        "tool_name": fn_name or "(unknown)",
                        "arguments": fn_args,
                        "output": output,
                        "status": status,
                        "security": security,
                    }
                    tool_logs.append(call_log)
                    await self._notify_tool_call(agent, on_tool_call, call_log)

                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": fn_name or "unknown_tool",
                        "content": output,
                    })
                    # 도구 하나가 끝날 때마다 남깁니다. 끊기면 그 다음 도구부터 이어 갑니다.
                    await save_progress()

                # 잘렸다는 사실은 **말로** 알려야 합니다. 도구 서버가 돌려준
                # "Input validation error" 만으로는 모델이 원인을 알 수 없어,
                # 같은 호출을 그대로 다시 시도하다 예산을 태웁니다.
                # 사고가 한도를 크게 먹었으면 "나누어 쓰라" 만으로는 다시 걸립니다.
                # 두 경우 모두 같은 뿌리이므로 같은 한 단락을 덧붙입니다.
                if arguments_cut:
                    self._append_budget_notice(
                        current_messages,
                        TRUNCATED_TOOL_CALL_NOTICE.format(
                            max_tokens=max_tokens_label(agent),
                            advice=truncation_advice(tools),
                        ) + reasoning_heavy_note(message, tool_calls),
                    )
                elif limit_reached:
                    self._append_budget_notice(
                        current_messages,
                        limit_reached_notice(max_tokens_label(agent), parsed[-1][0], tools)
                        + reasoning_heavy_note(message, tool_calls),
                    )

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
                # 사람이 늘려 준 상한도 끊겼다 이어 갈 때 남아 있어야 합니다.
                await save_progress()
                continue

            return await self._wrap_up_without_tools(
                agent, current_messages, segments, tool_logs, limit, on_chunk, tools=tools
            )
