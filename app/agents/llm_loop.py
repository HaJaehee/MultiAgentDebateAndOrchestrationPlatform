"""도구 루프: 한 발언 안에서 LLM 을 부르고, 도구를 실행하고, 관측을 되돌려 다시 부릅니다.

`ToolLoopMixin` 은 `LLMCaller`(app/agents/llm.py) 의 일부입니다. 한 판 호출(`_complete_once`)과 도구 실행
(`llm_tool_calls`), 응답 되살리기·마무리(`llm_recovery`)는 그쪽 것을 씁니다. 끊긴 발언을 이어 가는 초안의
형식(`SPEECH_STATE_VERSION`)도 여기서 정합니다.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.agents.base import Agent
from app.agents.ask_user import is_ask_tool
from app.agents.connection import REJECTED, classify
from app.agents.consultation import is_consultation_tool
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
    fit_tool_loop_context,
    max_tokens_label,
    tool_schema_tokens,
)
from app.agents.llm_recovery import (
    LEAKED_TOOL_CALL_FOOTER,
    LEAKED_TOOL_CALL_PLACEHOLDER,
    MAX_LEAKED_TOOL_CALL_RETRIES,
    find_leaked_tool_call,
    leaked_tool_call_notice,
)
from app.agents.llm_tools import memory_search_tool, memory_write_tool
from app.agents.llm_types import BudgetArbiter, ContextArbiter, SpeechCheckpoint
from app.agents.media import (
    MODALITY_LABELS,
    TOOL_MEDIA_HEADER,
    has_attachments,
    is_attachment,
    modality_of_mime,
    replace_attachments,
    store_tool_media,
)
from app.agents.skills import is_skill_tool
from app.agents.tool_loading import (
    CALLED_BEFORE_LOADING_NOTE,
    ToolShelf,
    is_load_tool,
    load_tools_answer,
    remember_loaded,
)
from app.mcp.manager import MCPManager
from app.workspace_files import format_size

logger = logging.getLogger(__name__)


# 초안의 형식. 필드를 바꾸면 올립니다 — 모르는 형식의 초안은 이어 가지 않고 발언을 처음부터 합니다.
SPEECH_STATE_VERSION = 2


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


# 모델이 첨부가 든 요청을 거절했을 때 발언 앞에 남기는 글. 화면과 기록과 다음 발언자가 함께 읽습니다.
ATTACHMENTS_REJECTED_NOTICE = (
    "> ⚠️ **첨부 전달 실패** — 모델이 첨부가 든 요청을 받지 않아 내용 없이 진행합니다: {names}\n"
    "> (원인: `{reason}`)"
)


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


@dataclass
class ToolLoop:
    """한 발언의 도구 루프가 판을 넘기며 들고 다니는 것.

    앞쪽은 발언 동안 그대로인 설정이고, 뒤쪽은 판마다 바뀌는 진행 상태입니다. 진행 상태는 판을 마칠
    때마다 체크포인트로 남고(`snapshot`), 끊긴 발언을 이어 갈 때 그대로 되살립니다(`restore`, ADR-025).
    """

    # --- 발언 동안 그대로인 것
    agent: Agent
    tools: List[Dict[str, Any]]
    on_chunk: Optional[Callable[[str], Any]] = None
    on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None
    on_context_trim: Optional[Callable[[int], Any]] = None
    session_id: Optional[str] = None
    mcp: Optional[MCPManager] = None
    tool_gate: Optional[Any] = None
    checkpoint: Optional[SpeechCheckpoint] = None
    # 발언을 시작할 때의 마지막 사용자 메시지. 창에 맞추다 버리게 되면 다시 붙입니다 (`fit_tool_loop_context`).
    turn_anchor: str = ""
    # 도구 정의를 나눈 진열대. 도구를 불러오면 `tools` 가 바뀝니다 (`app/agents/tool_loading.py`).
    shelf: Optional[ToolShelf] = None

    # --- 판마다 바뀌는 것 (체크포인트에 남습니다)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    limit: int = 0
    # 컨텍스트 창은 사람이 넓혀 줄 수 있으므로 에이전트 설정을 그대로 쓰지 않고 이 발언 동안의 유효값으로
    # 들고 다닙니다.
    window: int = 0
    used: int = 0
    # 이터레이션마다 나온 본문을 모읍니다.
    #
    # 도구를 부르는 판의 텍스트("먼저 파일을 확인하겠습니다…", 도구 결과에 대한
    # 관측과 판단)도 엄연히 그 에이전트의 발언입니다. 화면에는 `on_chunk` 으로
    # 이미 흘러가 있고요. 그런데 예전에는 마지막 판(도구를 부르지 않은 판)의
    # content 만 돌려주었습니다. 그 값이 화면 카드를 통째로 덮어쓰고 DB 에도
    # 그대로 들어가므로, 도구를 많이 부른 긴 발언일수록 사람이 읽고 있던 글이
    # 발언이 끝나는 순간 사라졌습니다 (짧은 답변은 이터레이션이 한 번이라
    # 마지막 판 == 전체였고, 그래서 이 손실이 오래 눈에 띄지 않았습니다).
    segments: List[str] = field(default_factory=list)
    tool_logs: List[Dict[str, Any]] = field(default_factory=list)
    # 호출 표식이 본문으로 새어 나와 다시 부르게 한 횟수 (`MAX_LEAKED_TOOL_CALL_RETRIES`).
    leaked_retries: int = 0
    context_asked: bool = False
    announced_bands: Set[Any] = field(default_factory=set)
    # 이번 판의 도구들이 돌려준 파일. 판의 도구를 다 돌린 뒤 사용자 메시지로 붙입니다 (`_flush_tool_media`).
    tool_media: List[Dict[str, Any]] = field(default_factory=list)

    # --- 도구 목록에서 한 번 구해 두는 것
    memory_write: Optional[str] = field(init=False)
    memory_search: Optional[str] = field(init=False)
    # 도구 정의는 요청마다 나가는 입력이라 컨텍스트 예산에서 뺍니다 (`context_budget`).
    schema_tokens: int = field(init=False)

    def __post_init__(self) -> None:
        self._read_tools()

    def _read_tools(self) -> None:
        self.memory_write = memory_write_tool(self.tools)
        self.memory_search = memory_search_tool(self.tools)
        self.schema_tokens = tool_schema_tokens(self.agent.model, self.tools)

    def _refresh_shelf(self, loaded: Sequence[str]) -> None:
        """불러온 도구의 정의를 다음 요청부터 싣고, 이 대화의 다음 발언에도 남깁니다."""
        if not loaded or self.shelf is None:
            return
        self.tools = self.shelf.offered()
        self._read_tools()
        remember_loaded(self.session_id, self.agent.key, loaded)
        logger.info(
            f"{self.agent.name} loaded tool schemas ({', '.join(loaded)}): "
            f"tools={len(self.tools)}, schema≈{self.schema_tokens:,} tok"
        )

    def answer_load_tools(self, arguments: Any) -> Tuple[str, str]:
        """모델이 부른 `tools__load_tools` 를 처리합니다."""
        output, status, loaded = load_tools_answer(self.shelf, arguments)
        self._refresh_shelf(loaded)
        return output, status

    def note_direct_call(self, fn_name: str, output: str, status: str) -> str:
        """목록에만 있던 도구를 곧바로 불렀으면 정의를 싣고, 실패했으면 다시 부르라고 덧붙입니다."""
        if self.shelf is None:
            return output
        loaded = self.shelf.load([fn_name])
        self._refresh_shelf(loaded)
        if loaded and status == "error":
            return f"{output}\n\n{CALLED_BEFORE_LOADING_NOTE}"
        return output

    def restore(self, saved: Dict[str, Any]) -> None:
        """끊긴 발언의 초안에서 진행 상태를 되살립니다. 상한은 줄이지 않습니다."""
        self.messages = [dict(m) for m in saved.get("messages") or self.messages]
        self.segments = list(saved.get("segments") or [])
        self.used = int(saved.get("used") or 0)
        self.limit = max(self.limit, int(saved.get("limit") or 0))
        self.window = int(saved.get("window") or self.window)
        self.tool_logs = [dict(log) for log in saved.get("tool_logs") or []]
        self.leaked_retries = int(saved.get("leaked_retries") or 0)
        self.context_asked = bool(saved.get("context_asked"))
        self.announced_bands = set(saved.get("announced_bands") or [])
        if self.shelf is not None:
            # 기억은 프로세스 안에만 있어 재시작하면 비어 있습니다. 불러온 도구는 초안이 압니다.
            self._refresh_shelf(self.shelf.load(saved.get("loaded_tools") or []))

    def snapshot(self) -> Dict[str, Any]:
        """이어 갈 수 있게 넘길 진행 상태 (형식 `SPEECH_STATE_VERSION`)."""
        return {
            "version": SPEECH_STATE_VERSION,
            "messages": self.messages,
            "segments": self.segments,
            "used": self.used,
            "limit": self.limit,
            "window": self.window,
            "tool_logs": self.tool_logs,
            "leaked_retries": self.leaked_retries,
            "context_asked": self.context_asked,
            "announced_bands": sorted(self.announced_bands, key=str),
            "turn_anchor": self.turn_anchor,
            "loaded_tools": self.shelf.loaded_names() if self.shelf is not None else [],
        }

    def answer(self) -> Tuple[str, List[Dict[str, Any]]]:
        """지금까지의 발언 전체와 도구 기록."""
        return "\n\n".join(self.segments), self.tool_logs

    def over_budget(self) -> bool:
        """보낼 메시지가 이 발언의 컨텍스트 예산을 넘는지."""
        budget = context_budget(self.agent, self.window, self.tools)
        return budget > 0 and estimate_tokens(self.agent.model, self.messages) > budget


class ToolLoopMixin:
    """도구 루프. `LLMCaller` 의 일부입니다."""

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
        shelf: Optional[ToolShelf] = None,
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

        한 판은 이 순서입니다: 창에 맞추기(`_fit_loop_context`) → 한도 안내(`_announce_loop_limits`) →
        LLM 한 판 → 도구 호출이 없으면 답으로 마무리(`_settle_answer`), 있으면 도구 실행
        (`_run_tool_round`). 상한에 닿으면 확장을 묻고(`_extend_tool_budget`), 못 받으면 도구 없이 마무리합니다.
        """
        loop = ToolLoop(
            agent=agent, tools=tools, on_chunk=on_chunk, on_tool_call=on_tool_call,
            on_context_trim=on_context_trim, session_id=session_id, mcp=mcp, tool_gate=tool_gate,
            checkpoint=checkpoint, turn_anchor=turn_anchor, shelf=shelf,
            messages=list(messages), limit=max_tool_iterations or agent.max_tool_iterations,
            window=agent.max_context_window,
        )

        # 끊긴 발언을 이어 가면 판 단위로 남긴 상태에서 시작합니다 (ADR-025). 끊긴 순간 실행 중이던
        # 도구에는 "결과 모름" 을 채우고, 이어진다는 사실을 모델에게 알립니다.
        if resume_state is not None:
            loop.restore(resume_state)
            unknown = close_open_tool_calls(loop.messages)
            notice = RESUMED_SPEECH_NOTICE
            if unknown:
                notice += f" 결과를 받지 못한 호출: {', '.join(unknown)}."
            self._append_budget_notice(loop.messages, notice)

        # 유저가 지정한 스킬을 첫 판 전에 불러 둡니다. 이어 가는 발언은 이미 들어 있습니다.
        if resume_state is None and preload_skills:
            if await self._preload_skills(
                agent, preload_skills, loop.messages, loop.tool_logs, tools,
                on_tool_call=on_tool_call, session_id=session_id, mcp=mcp,
            ):
                await self._save_loop_progress(loop)

        while True:
            while loop.used < loop.limit:
                wrapped = await self._fit_loop_context(loop, context_arbiter)
                if wrapped is not None:
                    return wrapped
                self._announce_loop_limits(loop)

                try:
                    message, finish_reason = await self._complete_once(
                        agent, loop.messages, loop.tools, on_chunk
                    )
                except Exception as exc:
                    if await self._drop_rejected_attachments(loop, exc):
                        continue
                    raise
                loop.used += 1

                segment = self._compose_content(agent, message)
                if segment.strip():
                    loop.segments.append(segment.strip())

                tool_calls = getattr(message, "tool_calls", None)
                if not tool_calls:
                    answer = await self._settle_answer(loop, message, finish_reason, segment)
                    if answer is None:
                        continue                    # 새어 나온 도구 호출 — 다시 부릅니다
                    return answer
                await self._run_tool_round(loop, message, tool_calls, finish_reason)

            if not await self._extend_tool_budget(loop, budget_arbiter):
                return await self._wrap_up_without_tools(
                    agent, loop.messages, loop.segments, loop.tool_logs, loop.limit, on_chunk, tools=loop.tools
                )

    async def _save_loop_progress(self, loop: ToolLoop) -> None:
        """지금까지를 이어 갈 수 있게 넘깁니다. 넘기다 실패해도 발언은 계속됩니다."""
        if loop.checkpoint is None:
            return
        try:
            await loop.checkpoint(loop.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not save the progress of {loop.agent.name}'s speech: {exc}")

    async def _drop_rejected_attachments(self, loop: ToolLoop, exc: Exception) -> bool:
        """모델이 첨부가 든 요청을 거절했으면 첨부를 안내 글로 바꿉니다. 다시 부를 수 있게 됐으면 True.

        체크박스는 사람이 고르고 어떤 모델이 무엇을 받는지는 Provider 마다 다릅니다 (Anthropic 은 음성을,
        많은 모델이 동영상을 받지 않습니다). 첫 판이 거절되면 그 발언 전체를 잃고, 대화가 시작되면 모달은
        고정되므로 사람이 바로 고칠 수도 없습니다. 그래서 글로 이어 갑니다. 모델에게는 받지 못했다고 적고,
        사람에게는 발언 앞의 경고로 알립니다.
        """
        if not has_attachments(loop.messages) or classify(exc) != REJECTED:
            return False
        names = replace_attachments(loop.messages, "모델이 이 형식의 첨부를 받지 않습니다.")
        reason = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        logger.warning(f"{loop.agent.name} ({loop.agent.model}) rejected attachments {names}: {reason}")
        notice = ATTACHMENTS_REJECTED_NOTICE.format(names=", ".join(names), reason=reason[:300])
        loop.segments.append(notice)
        if loop.on_chunk:
            await self._emit_chunk(loop.on_chunk, f"{notice}\n\n")
        await self._save_loop_progress(loop)
        return True

    # ------------------------------------------------------------ 판 앞: 창과 한도

    async def _fit_loop_context(
        self, loop: ToolLoop, context_arbiter: Optional[ContextArbiter]
    ) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        """컨텍스트: 넘치면 짝 단위로 덜어내고, 버릴 것이 생기면 물어봅니다.

        사람이 "여기서 접으라" 고 답하면 도구 없이 마무리한 결과를 돌려줍니다. 그 밖에는 None.
        """
        agent = loop.agent
        over = loop.over_budget()

        if over and context_arbiter is not None and not loop.context_asked:
            loop.context_asked = True
            answer = await self._ask_context(
                context_arbiter, agent, loop.window, loop.messages, len(loop.tool_logs), loop.tools
            )
            granted = int(answer.get("granted") or 0)
            if granted > 0:
                loop.window += granted
                logger.info(
                    f"Context window for {agent.name} widened by {granted} to {loop.window} "
                    f"by the user"
                )
                self._append_budget_notice(
                    loop.messages,
                    CONTEXT_WIDENED_INSTRUCTION.format(extra=granted, window=loop.window),
                )
                over = loop.over_budget()
            elif answer.get("wrap_up"):
                # 넓히지 않고 여기서 접겠다는 뜻입니다. 마무리 호출도 넘친
                # 메시지로 나가면 400 이므로 먼저 창 안에 맞춥니다.
                self._trim_loop_context(loop)
                return await self._wrap_up_without_tools(
                    agent, loop.messages, loop.segments, loop.tool_logs, loop.limit, loop.on_chunk,
                    tools=loop.tools,
                )

        if over:
            self._trim_loop_context(loop)
        return None

    @staticmethod
    def _trim_loop_context(loop: ToolLoop) -> None:
        loop.messages, dropped = fit_tool_loop_context(
            loop.agent, loop.messages, loop.window, loop.memory_search, tools=loop.tools,
            keep=loop.turn_anchor,
        )
        if dropped and loop.on_context_trim:
            loop.on_context_trim(dropped)

    def _announce_loop_limits(self, loop: ToolLoop) -> None:
        """남은 도구 호출 수와 컨텍스트 사용률을 판 앞에 알립니다 (도구가 있는 발언만)."""
        if not loop.tools:
            return
        agent = loop.agent
        notice = tool_budget_notice(
            remaining=loop.limit - loop.used, limit=loop.limit, used=loop.used, tool_calls=len(loop.tool_logs)
        )
        if notice:
            self._append_budget_notice(loop.messages, notice)

        pressure = context_pressure_notice(
            used=estimate_tokens(agent.model, loop.messages),
            budget=context_budget(agent, loop.window, loop.tools),
            announced=loop.announced_bands,
            memory_tool=loop.memory_write,
            tool_calls_left=loop.limit - loop.used,
        )
        if pressure:
            self._append_budget_notice(loop.messages, pressure)

    # ------------------------------------------------------------ 도구를 부르지 않은 판

    async def _settle_answer(
        self, loop: ToolLoop, message: Any, finish_reason: str, segment: str
    ) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        """도구를 부르지 않고 끝난 판을 답으로 마무리합니다. 새어 나온 호출을 다시 부를 때만 None."""
        agent, tools = loop.agent, loop.tools
        # 도구 호출이 본문 글자로 새어 나왔는지 먼저 봅니다. 그렇다면 이것은
        # 답변이 아니라 **실행되지 못한 호출**입니다. 평범한 답으로 받으면 도구는
        # 돌지 않고, 잘렸을 때는 이어받기가 호출 표식을 산문처럼 이어 씁니다.
        leak_at = find_leaked_tool_call(segment) if tools else None
        if leak_at is not None:
            return self._handle_leaked_tool_call(loop, message, finish_reason, segment, leak_at)

        answer_text = message_text(message).strip()
        reasoning = getattr(message, "reasoning_content", None) or ""

        # 한도에 닿지 않았는데 본문이 없고 사고에만 글이 있으면, 서버가 답까지
        # 사고로 분류한 것입니다. 그대로 두면 빈 카드가 됩니다.
        if finish_reason != "length" and not answer_text and reasoning.strip():
            await self._answer_left_in_reasoning(
                agent, loop.messages, loop.segments, loop.on_chunk, reasoning, tools,
                reasoning_shown=bool(segment.strip()),
            )
            return loop.answer()

        # 도구를 부르지 않고 끝난 판입니다. 여기서 잘렸다면 이의를
        # 제기해 줄 도구 서버가 없으므로 우리가 표시를 남깁니다.
        if finish_reason == "length":
            logger.warning(
                f"Truncated answer from {agent.name}: finish_reason='length', "
                f"max_tokens={effective_max_tokens(agent)}, "
                + completion_budget_report(
                    message, None, None,
                    estimate_tokens(agent.model, loop.messages), loop.window, loop.schema_tokens,
                )
            )
        await self._finish_truncated_answer(
            agent, loop.messages, loop.segments, finish_reason, loop.on_chunk,
            # 합친 글이 아니라 모델이 준 본문으로 판단합니다 (위 마무리와 같은 이유).
            last_text=answer_text,
            reasoning=reasoning,
            tools=tools,
        )
        return loop.answer()

    def _handle_leaked_tool_call(
        self, loop: ToolLoop, message: Any, finish_reason: str, segment: str, leak_at: int
    ) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        """본문에 새어 나온 도구 호출. 다시 부를 수 있으면 None, 아니면 표식 앞까지로 마무리한 결과."""
        agent = loop.agent
        kept = segment[:leak_at].strip()
        if segment.strip():
            loop.segments.pop()          # 방금 넣은 이 판의 글을 표식 앞까지로 바꿉니다
        if kept:
            loop.segments.append(kept)
        logger.warning(
            f"Tool call leaked into the text of {agent.name}'s response and was "
            f"not executed: finish_reason={finish_reason!r}, "
            f"max_tokens={effective_max_tokens(agent)}, "
            + completion_budget_report(
                message, None, None,
                estimate_tokens(agent.model, loop.messages), loop.window, loop.schema_tokens,
            )
        )
        if loop.leaked_retries < MAX_LEAKED_TOOL_CALL_RETRIES:
            loop.leaked_retries += 1
            loop.messages.append(
                {"role": "assistant", "content": kept or LEAKED_TOOL_CALL_PLACEHOLDER}
            )
            self._append_budget_notice(
                loop.messages,
                leaked_tool_call_notice(max_tokens_label(agent), finish_reason, loop.tools),
            )
            return None
        loop.segments.append(LEAKED_TOOL_CALL_FOOTER)
        return loop.answer()

    # ------------------------------------------------------------ 도구를 부른 판

    async def _run_tool_round(
        self, loop: ToolLoop, message: Any, tool_calls: List[Any], finish_reason: str
    ) -> None:
        """모델이 부른 도구를 모두 실행하고 결과를 대화에 붙입니다."""
        agent = loop.agent
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
                estimate_tokens(agent.model, loop.messages), loop.window, loop.schema_tokens,
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
        loop.messages.append(self._assistant_turn(message, parsed))
        # 도구를 실행하기 전에 남깁니다. 실행 도중 끊기면 무엇을 부르던 중이었는지 압니다.
        await self._save_loop_progress(loop)

        # Execute all requested tool calls
        #
        # 이 구간은 무슨 일이 있어도 예외를 올리지 않습니다. 도구가
        # 실패하면 그 사실을 `role="tool"` 결과로 되돌려 주어야 모델이
        # 읽고 스스로 고칩니다. 여기서 예외를 올리면 발언 전체가
        # LLMUnavailableError 로 덮여, 이미 흘러간 글과 성공한 도구
        # 관측까지 통째로 사라집니다.
        for fn_name, fn_args, call_id, _parsed_ok in parsed:
            await self._run_one_tool(loop, fn_name, fn_args, call_id)
        await self._flush_tool_media(loop)

        # 잘렸다는 사실은 **말로** 알려야 합니다. 도구 서버가 돌려준
        # "Input validation error" 만으로는 모델이 원인을 알 수 없어,
        # 같은 호출을 그대로 다시 시도하다 예산을 태웁니다.
        # 사고가 한도를 크게 먹었으면 "나누어 쓰라" 만으로는 다시 걸립니다.
        # 두 경우 모두 같은 뿌리이므로 같은 한 단락을 덧붙입니다.
        if arguments_cut:
            self._append_budget_notice(
                loop.messages,
                TRUNCATED_TOOL_CALL_NOTICE.format(
                    max_tokens=max_tokens_label(agent),
                    advice=truncation_advice(loop.tools),
                ) + reasoning_heavy_note(message, tool_calls),
            )
        elif limit_reached:
            self._append_budget_notice(
                loop.messages,
                limit_reached_notice(max_tokens_label(agent), parsed[-1][0], loop.tools)
                + reasoning_heavy_note(message, tool_calls),
            )

    async def _run_one_tool(
        self, loop: ToolLoop, fn_name: str, fn_args: Dict[str, Any], call_id: str
    ) -> None:
        """도구 하나: 보안 판정 → 실행 → 기록·알림 → 결과를 대화에 붙이고 남깁니다."""
        agent = loop.agent
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
                if (
                    is_skill_tool(fn_name) or is_consultation_tool(fn_name) or is_ask_tool(fn_name)
                    or is_load_tool(fn_name)
                )
                else await self._check_tool_gate(
                    loop.tool_gate, agent, fn_name, fn_args, loop.mcp or self.mcp_manager,
                )
            )
            if gate is not None and not gate.allowed:
                output, status = gate.output, gate.status or "denied"
            elif is_load_tool(fn_name):
                output, status = loop.answer_load_tools(fn_args)
            else:
                output, status = await self._execute_tool_safely(
                    agent, fn_name, fn_args, loop.session_id, loop.mcp, tools=loop.tools,
                )
                output = await self._take_tool_media(loop, fn_name, output)
                output = loop.note_direct_call(fn_name, output, status)
            security = gate.audit if gate is not None else {}

        call_log = {
            "tool_name": fn_name or "(unknown)",
            "arguments": fn_args,
            "output": output,
            "status": status,
            "security": security,
        }
        loop.tool_logs.append(call_log)
        await self._notify_tool_call(agent, loop.on_tool_call, call_log)

        loop.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": fn_name or "unknown_tool",
            "content": output,
        })
        # 도구 하나가 끝날 때마다 남깁니다. 끊기면 그 다음 도구부터 이어 갑니다.
        await self._save_loop_progress(loop)

    async def _take_tool_media(self, loop: ToolLoop, fn_name: str, output: str) -> str:
        """도구가 돌려준 파일을 처리하고 모델이 읽을 글을 돌려줍니다 (`app/mcp/client.py` 의 `ToolOutput`).

        이 에이전트가 받는 형식이면 파일로 풀어 두었다가 판 끝에 함께 싣고, 아니면 무엇이 왔는지만 적습니다.
        어느 쪽이든 BASE64 는 도구 결과의 글에 넣지 않습니다 — 그 글은 모델이 읽고, 화면에 나오고, DB 에 남습니다.
        """
        media = getattr(output, "media", None)
        if not media:
            return output
        allowed = set(loop.agent.allowed_modalities or ())
        workspace = getattr(loop.mcp or self.mcp_manager, "workspace", None)
        notes: List[str] = []
        for item in media:
            kind = modality_of_mime(item.mime)
            label = MODALITY_LABELS.get(kind, "파일")
            described = f"{label} ({item.mime or '형식 모름'}, {format_size(item.size)})"
            if kind in allowed and workspace:
                try:
                    block = await asyncio.to_thread(store_tool_media, workspace, fn_name, kind, item.mime, item.data)
                except Exception as exc:  # noqa: BLE001 - 파일을 못 풀어도 도구 결과의 글은 남습니다
                    logger.warning(f"Could not store a file returned by {fn_name}: {exc}")
                    notes.append(f"[도구가 {described} 을 돌려주었지만 앱이 받아 두지 못했습니다.]")
                    continue
                loop.tool_media.append(block)
                notes.append(f"[도구가 {described} 을 돌려주었습니다. 바로 다음 메시지에 첨부합니다.]")
            elif kind in MODALITY_LABELS:
                notes.append(
                    f"[도구가 {described} 을 돌려주었습니다. 이 에이전트는 '{label}' 입력을 받도록 설정되어 있지 않아 "
                    f"내용을 볼 수 없습니다. 내용을 본 것처럼 말하지 마세요.]"
                )
            else:
                notes.append(f"[도구가 {described} 을 돌려주었습니다. 모델에게 전달할 수 없는 형식입니다.]")
        return "\n\n".join(part for part in (str(output), *notes) if part)

    async def _flush_tool_media(self, loop: ToolLoop) -> None:
        """이번 판에 받은 파일을 사용자 메시지로 붙입니다.

        `role=tool` 에는 붙이지 않습니다. OpenAI 호환 엔드포인트의 도구 결과는 글만 받습니다. 도구 결과 사이에는
        다른 메시지를 끼울 수 없으므로 (짝이 깨지면 400) 판의 도구를 다 돌린 뒤에 붙입니다.
        """
        if not loop.tool_media:
            return
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": TOOL_MEDIA_HEADER}]
        for block in loop.tool_media:
            if is_attachment(block):
                blocks.append({"type": "text", "text": f"- {block['name']}"})
            blocks.append(block)
        loop.tool_media = []
        loop.messages.append({"role": "user", "content": blocks})
        await self._save_loop_progress(loop)

    # ------------------------------------------------------------ 상한에 닿았을 때

    async def _extend_tool_budget(self, loop: ToolLoop, budget_arbiter: Optional[BudgetArbiter]) -> bool:
        """예산 소진. 사람에게 한 번 묻고, 늘려 받았으면 True — 아니면 도구 없이 마무리하게 됩니다.

        여기서 예외를 올리면 지금까지의 발언이 통째로 사라집니다. 묻다가 실패해도 늘려 받지 못한 것으로 칩니다.
        """
        agent = loop.agent
        granted = 0
        if budget_arbiter is not None:
            try:
                granted = int(await budget_arbiter({
                    "agent_key": agent.key,
                    "agent_name": agent.name,
                    "limit": loop.limit,
                    "used": loop.used,
                    "tool_calls": len(loop.tool_logs),
                }) or 0)
            except Exception as exc:  # noqa: BLE001 - 묻다가 실패해도 발언은 살립니다
                logger.warning(f"Tool budget arbiter failed for {agent.name}: {exc}")
                granted = 0

        if granted <= 0:
            return False
        loop.limit += granted
        logger.info(
            f"Tool budget for {agent.name} extended by {granted} to {loop.limit} by the user"
        )
        self._append_budget_notice(
            loop.messages,
            BUDGET_EXTENDED_INSTRUCTION.format(
                extra=granted, limit=loop.limit, remaining=loop.limit - loop.used
            ),
        )
        # 사람이 늘려 준 상한도 끊겼다 이어 갈 때 남아 있어야 합니다.
        await self._save_loop_progress(loop)
        return True
