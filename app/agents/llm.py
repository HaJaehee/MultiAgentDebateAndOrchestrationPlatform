"""LLM 호출기: 에이전트 한 명의 발언을 LiteLLM 으로 받아 옵니다.

시스템 프롬프트를 짓고, 도구 루프를 돌리고(도구 호출 → 보안 판정 → 실행 → 관측), 한도에 닿으면 사람에게
묻거나 마무리합니다. 엔드포인트에 닿지 못하면 `LLMUnavailableError` 를 올리고, 대신 답을 지어내지 않습니다.

곁가지는 옆 모듈에 있습니다: 도구 루프(`llm_loop`), 요청 만들기(`llm_request`), 사고 과정(`reasoning`), 컨텍스트 창 맞춤(`llm_context`), 한도 안내
(`llm_budget`), 도구 이름·쓰기 지침(`llm_tools`), 요청 지문(`llm_fingerprint`), 응답 되살리기
(`llm_recovery`), 도구 호출 처리(`llm_tool_calls`), 예외와 콜백 형식(`llm_types`), 전역 모델 폴백
(`llm_fallback`), 서버 전체 동시 요청 상한(`llm_gate`), 토큰·시간 계측(`llm_usage`).
"""

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import litellm

from app.agents import llm_fallback, llm_usage
from app.agents.base import Agent
from app.agents.connection import SERVER_ERROR, classify
from app.agents.ask_user import ask_tools_for
from app.agents.consultation import consultation_tools_for
from app.agents.llm_context import fit_context_window, merge_consecutive_roles, place_ledger_last
from app.agents.llm_fingerprint import request_fingerprint
from app.agents.llm_gate import get_llm_gate
from app.agents.llm_loop import ToolLoopMixin
from app.agents.llm_recovery import AnswerRecoveryMixin
from app.agents.llm_request import RequestBuilderMixin
from app.agents.llm_tool_calls import ToolCallMixin
from app.agents.llm_tools import memory_search_tool
from app.agents.llm_types import BudgetArbiter, ContextArbiter, LLMUnavailableError, SpeechCheckpoint
from app.agents.skills import skill_tools_for
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool

logger = logging.getLogger(__name__)

# Suppress litellm verbose logging
litellm.suppress_debug_info = True


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


class LLMCaller(RequestBuilderMixin, AnswerRecoveryMixin, ToolCallMixin, ToolLoopMixin):
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
        tools = tools + skill_tools_for(agent) + consultation_tools_for(agent) + ask_tools_for(agent)

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
                # 게이트 대기 시간은 제외하고, 모델 응답 소요 시간만 측정합니다 (`llm_usage`).
                began = time.monotonic()
                try:
                    result = await self._complete_unthrottled(agent, messages, tools, on_chunk, tool_choice)
                finally:
                    llm_usage.note_call(time.monotonic() - began)
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
            # 조립은 동기 함수라 루프 위에서 돌면 끝날 때까지 다른 발언 스트림·대화·화면이 모두 멈춥니다.
            # 스레드로 보내도 GIL 때문에 빨라지지는 않지만 루프가 그동안 차례를 받습니다. 목록은 복사해 넘깁니다.
            complete_response = await asyncio.to_thread(
                litellm.stream_chunk_builder, chunks, messages=list(messages),
            )
            llm_usage.note_response(complete_response)
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
            llm_usage.note_response(response)
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
