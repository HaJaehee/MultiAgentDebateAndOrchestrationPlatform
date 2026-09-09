import asyncio
import json
import logging
import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple
from sqlalchemy import select
from app.agents.base import Agent
from app.agents.llm import (
    LLMCaller,
    LLMUnavailableError,
    context_trim_notice,
    estimate_tokens,
    memory_search_tool,
    strip_reasoning_trace,
)
from app.agents.personas import prepare_agents_for_turn
from app.agents.pool import AgentPool, get_agent_pool
from app.config import TOOL_ITERATION_CEILING, resolve_workspace_dir
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool
from app.mermaid_lint import format_issues, lint_mermaid
from app.database.models import (
    ArtifactModel,
    MessageModel,
    SessionModel,
    ToolCallRecordModel,
    utc_now,
)
from app.database.session import get_session_factory
from app.orchestration.control import TurnControl
from app.orchestration.state import ArtifactItem, DebateMessage, DebateState
from app.orchestration.strategies import (
    BaseDebateStrategy,
    get_strategy,
    resolve_strategy_name,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]

# 닫는 펜스가 없어도 (max_tokens 로 답변이 잘렸을 때) 마지막 블록을 건집니다.
# 예전 정규식은 ``` 짝이 맞을 때만 매칭돼서, 다이어그램 도중에 잘린 답변은
# 아티팩트가 통째로 사라졌습니다.
CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_\-\+]*)[ \t]*\r?\n(.*?)(?:```|\Z)", re.DOTALL)

# 언어 태그 없이 열린 블록이라도 첫 줄이 이 키워드면 Mermaid 로 취급합니다.
MERMAID_HEADERS = (
    "graph", "flowchart", "sequencediagram", "classdiagram", "statediagram",
    "erdiagram", "journey", "gantt", "pie", "gitgraph", "mindmap", "timeline",
    "quadrantchart", "requirementdiagram", "c4context", "sankey-beta",
    "block-beta", "architecture-beta", "xychart-beta",
)

CODE_LANGUAGES = ("python", "py", "typescript", "javascript", "bash", "shell", "json", "toml", "sql")

# 다이어그램 문법이 틀렸을 때 오케스트레이터에게 다시 물어보는 횟수.
#
# 두 번이면 충분합니다. 오류 메시지를 정확히 받은 모델은 대개 한 번에 고치고,
# 두 번째에도 못 고치면 세 번째라고 달라지지 않습니다 — 그때는 사람이 볼 수
# 있도록 원문을 그대로 두고 무엇이 틀렸는지 알리는 편이 낫습니다.
MERMAID_REPAIR_ATTEMPTS = 2

# `A[결제 서비스 (Payment)]` 처럼 대괄호 라벨 안에 괄호가 들어간 형태. LLM 이 가장 자주
# 만드는 Mermaid 파싱 오류이고, 따옴표로 감싸면 그대로 통과합니다.
_PAREN_LABEL_RE = re.compile(r"\[([^\[\]{}\"|]*[()][^\[\]{}\"|]*)\]")
# 여는 문자 -> 닫는 문자. 이 쌍으로 감싸인 것은 라벨이 아니라 노드 모양입니다.
_SHAPE_PAIRS = {"(": ")", "[": "]", "/": "/", "\\": "\\", "{": "}"}


def normalize_mermaid(content: str) -> str:
    """LLM 이 흔히 내는 Mermaid 문법 오류를 최소한만 손봅니다.

    다이어그램을 다시 써 주는 것이 아니라, 렌더러가 통째로 거부해서 화면이 비는
    두 가지 경우만 막습니다: 잘못된 줄바꿈과 따옴표 없는 괄호 라벨.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return text

    # ```mermaid 를 잘라낸 뒤 남은 "mermaid" 머리글
    lines = text.split("\n")
    if lines[0].strip().lower() == "mermaid":
        lines = lines[1:]

    out: List[str] = []
    for line in lines:
        # `[(...)]`(원통), `[[...]]`(서브루틴), `[/.../]`·`[\...\]`(평행사변형) 같은
        # 모양 문법은 라벨이 아니라 노드 종류입니다. 따옴표를 씌우면 안 됩니다.
        def _quote(m: re.Match) -> str:
            inner = m.group(1)
            if not inner:
                return m.group(0)
            if _SHAPE_PAIRS.get(inner[0]) == inner[-1]:
                return m.group(0)
            return f'["{inner.strip()}"]'

        out.append(_PAREN_LABEL_RE.sub(_quote, line))
    return "\n".join(out).strip()


def find_mermaid_blocks(text: str) -> List[Dict[str, Any]]:
    """본문 속 Mermaid 코드 블록을 **위치와 함께** 찾습니다.

    `extract_code_blocks()` 는 코드만 돌려주는데, 고친 다이어그램을 제자리에
    끼워 넣으려면 원문의 어디였는지를 알아야 합니다. 문자열 치환으로 하면
    같은 코드가 두 번 나올 때 엉뚱한 곳을 바꿉니다.
    """
    blocks: List[Dict[str, Any]] = []
    for match in CODE_FENCE_RE.finditer(text or ""):
        lang = match.group(1).strip().lower() or "text"
        code = match.group(2).strip()
        if not code:
            continue
        if lang == "text":
            first = code.split("\n", 1)[0].strip().lower()
            if not any(first.startswith(h) for h in MERMAID_HEADERS):
                continue
        elif lang != "mermaid":
            continue
        # 범위는 **다듬은 코드**의 것이어야 합니다. 원본 그대로의 범위를 쓰면
        # 끝의 줄바꿈까지 포함되고, 그 자리를 줄바꿈 없는 코드로 갈아 끼우는
        # 순간 닫는 ``` 가 마지막 코드 줄에 붙어 펜스가 깨집니다.
        raw = match.group(2)
        start = match.start(2) + (len(raw) - len(raw.lstrip()))
        blocks.append({"code": code, "start": start, "end": start + len(code)})
    return blocks


def extract_code_blocks(text: str) -> List[Dict[str, str]]:
    """Extracts markdown code blocks from text."""
    matches = []
    for match in CODE_FENCE_RE.finditer(text or ""):
        lang = match.group(1).strip().lower() or "text"
        code = match.group(2).strip()
        if not code:
            continue
        if lang == "text":
            first = code.split("\n", 1)[0].strip().lower()
            if any(first.startswith(h) for h in MERMAID_HEADERS):
                lang = "mermaid"
        matches.append({"language": lang, "code": code})
    return matches


class OrchestratorEngine:
    """Multi-Agent Orchestration & Debate Execution Engine."""

    def __init__(self, agent_pool: Optional[AgentPool] = None, llm_caller: Optional[LLMCaller] = None):
        self.agent_pool = agent_pool or get_agent_pool()
        self.llm_caller = llm_caller or LLMCaller()
        self.session_factory = get_session_factory()

    # ------------------------------------------------------------------ 작업 공간

    @staticmethod
    def _mcp_for(state: DebateState) -> Optional[MCPManager]:
        """이 턴이 빌린 MCP 런타임.

        예전에는 전역 매니저 하나를 이 턴의 작업 공간으로 **다시 띄웠습니다**.
        그래서 폴더가 다른 토론이 동시에 돌 수 없었습니다. 지금은 턴이 시작할 때
        그 폴더의 런타임을 빌리고(`run_turn`), 발언마다 그것을 내려보냅니다.

        None 을 돌려주면 `LLMCaller` 가 자기 기본 런타임으로 물러섭니다 — 풀을
        거치지 않고 엔진을 직접 부르는 테스트가 그 경로를 씁니다.
        """
        return get_runtime_pool().get(state.workspace_dir)

    # ------------------------------------------------------------------ 발언

    @staticmethod
    def _unavailable_notice(agent: Agent, exc: LLMUnavailableError, streamed: str) -> str:
        """응답을 받지 못했을 때 화면과 기록에 남는 문구.

        여기서 그럴듯한 대체 발언을 만들어 내면 다음 에이전트의 입력과 최종 합성
        보고서까지 그 거짓말 위에 쌓입니다. 못 받았다고 적는 편이 낫습니다.
        """
        notice = (
            f"> ⚠️ **연결 끊김 — {agent.name} 의 발언을 받지 못했습니다.**\n"
            f">\n"
            f"> - 모델: `{agent.model}`\n"
            f"> - 엔드포인트: `{exc.endpoint}`\n"
            f"> - 원인: `{exc.reason}`\n"
            f">\n"
            f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다. "
            f"엔드포인트를 복구한 뒤 같은 요청을 다시 보내주세요."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 연결이 끊기기 전까지 도착한 부분입니다.)"
            )
        return notice

    @staticmethod
    def _crashed_notice(agent: Agent, exc: BaseException, streamed: str) -> str:
        """발언이 예기치 못한 오류로 끊겼을 때 화면과 기록에 남는 문구.

        `LLMUnavailableError` 는 "엔드포인트에 닿지 못했다" 는 알려진 실패이고,
        이쪽은 우리가 예상하지 못한 실패입니다 — 도구 서버가 이상한 것을 돌려
        주었거나, 프로바이더 응답 모양이 달랐거나, 우리 코드가 틀렸거나.
        어느 쪽이든 **한 에이전트의 사고가 토론 전체를 끝낼 이유는 없습니다.**
        나머지 에이전트는 아직 말할 수 있고, 지금까지의 발언으로 합성도 됩니다.
        """
        notice = (
            f"> ⚠️ **발언 중단 — {agent.name} 의 발언이 오류로 끊겼습니다.**\n"
            f">\n"
            f"> - 모델: `{agent.model}`\n"
            f"> - 오류: `{type(exc).__name__}: {exc}`\n"
            f">\n"
            f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다. "
            f"토론은 나머지 에이전트로 계속됩니다 (자세한 원인은 서버 로그를 보세요)."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 오류가 나기 전까지 도착한 부분입니다.)"
            )
        return notice

    def _make_budget_arbiter(
        self,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, int]]]:
        """도구 호출 상한에 닿았을 때 사람에게 물어보는 통로를 만듭니다.

        `control` 이 없으면 (배치 실행, 테스트) None 을 돌려줍니다. 그러면
        LLM 쪽은 아무도 묻지 않고 곧장 "도구 없이 마무리" 로 갑니다 — 물어볼
        사람이 없는데 3분을 기다릴 이유가 없습니다.

        상한은 `TOOL_ITERATION_CEILING` 을 넘길 수 없습니다. 확장은 폭주를
        늦추는 것이지 푸는 것이 아닙니다.
        """
        if control is None:
            return None

        async def arbiter(info: Dict[str, Any]) -> int:
            limit = int(info.get("limit", 0))
            headroom = max(0, TOOL_ITERATION_CEILING - limit)

            async def _open(request) -> None:
                if on_event:
                    await on_event({"type": "tool_budget_exhausted", **request.describe()})

            request = await control.ask_tool_budget(
                agent_key=str(info.get("agent_key", "")),
                agent_name=str(info.get("agent_name", "")),
                limit=limit,
                used=int(info.get("used", 0)),
                tool_calls=int(info.get("tool_calls", 0)),
                max_extension=headroom,
                on_open=_open,
            )
            if on_event:
                await on_event({
                    "type": "tool_budget_resolved",
                    "id": request.id,
                    "agent_name": request.agent_name,
                    "outcome": request.outcome,
                    "granted": request.granted,
                    "limit": limit + request.granted,
                })
            return request.granted

        return arbiter

    def _make_context_arbiter(
        self,
        state: DebateState,
        agent: Agent,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]]:
        """컨텍스트 창이 넘쳤을 때 사람에게 물어보는 통로를 만듭니다.

        도구 예산 쪽과 두 가지가 다릅니다.

        1. **턴에 한 번만 묻습니다.** 컨텍스트는 라운드가 쌓이면 거의 모든 발언이
           같은 벽에 부딪힙니다. 발언마다 물으면 토론을 진행할 수 없으므로, 첫
           답(`state.context_grant`)을 그 턴의 나머지에 그대로 씁니다.
        2. **모델의 실제 한도를 넘겨 늘리지 않습니다.** `context_headroom()` 이
           조회한 여유가 상한입니다. 넘겨 올리면 깔끔한 트림이 400 으로 바뀝니다.
        """
        if control is None:
            return None

        async def arbiter(info: Dict[str, Any]) -> Dict[str, Any]:
            # 이미 이번 턴에 답을 받았으면 다시 묻지 않습니다.
            if state.context_grant is not None:
                return {"granted": state.context_grant, "wrap_up": False}

            async def _open(request) -> None:
                if on_event:
                    await on_event({"type": "context_window_exhausted", **request.describe()})

            request = await control.ask_context_window(
                agent_key=str(info.get("agent_key", "")),
                agent_name=str(info.get("agent_name", "")),
                window=int(info.get("window", 0)),
                used=int(info.get("used", 0)),
                headroom=info.get("headroom"),
                tool_calls=int(info.get("tool_calls", 0)),
                on_open=_open,
            )
            state.context_grant = request.granted
            if on_event:
                await on_event({
                    "type": "context_window_resolved",
                    "id": request.id,
                    "agent_name": request.agent_name,
                    "outcome": request.outcome,
                    "granted": request.granted,
                    "window": int(info.get("window", 0)) + request.granted,
                })
            # 시간이 지나 답을 못 받았으면 접지 않고 생략하며 진행합니다. 사람이
            # 자리에 없다고 해서 토론을 접을 이유는 없습니다.
            return {
                "granted": request.granted,
                "wrap_up": request.outcome == "wrap_up",
            }

        return arbiter

    async def _speak(
        self,
        *,
        db,
        state: DebateState,
        agent: Agent,
        prompt_messages: List[Dict[str, Any]],
        custom_instructions: str,
        round_number: int,
        msg_type: str,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl] = None,
        db_lock: Optional[asyncio.Lock] = None,
        created_at: Optional[datetime] = None,
        post_process: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
    ) -> DebateMessage:
        """한 에이전트의 발언을 스트리밍하고, DB 에 기록하고, 상태에 반영합니다.

        `db_lock` 은 이 발언이 다른 발언과 **동시에** 진행될 때만 필요합니다
        (병렬 지시 전략). `db` 는 세션 하나를 공유하는데 SQLAlchemy AsyncSession 은
        동시 사용을 허용하지 않습니다 — 두 발언이 같은 순간에 커밋하면
        `IllegalStateChangeError` 로 토론이 통째로 죽습니다. 락은 LLM 호출이 아니라
        기록 구간에만 걸리므로 병렬성은 그대로입니다.

        `created_at` 을 주면 그 시각으로 기록합니다. 병렬 라운드에서는 발언이 끝나는
        순서가 제각각이라, 커밋 시각을 그대로 쓰면 새로고침한 화면의 발언 순서가
        매번 달라집니다 (기록은 `created_at` 으로 정렬해 다시 읽힙니다). 지시받은
        순서를 시각에 박아 두면 실시간 화면과 다시 연 화면이 같은 순서를 보여줍니다.

        LLM 이 응답하지 못하면 실패 사실을 그대로 적은 `msg_type="error"` 발언을
        남기고 토론을 계속합니다 (다른 에이전트는 아직 살아 있을 수 있습니다).

        이 발언 중에 실행된 MCP 도구도 여기서 함께 기록합니다. 어느 발언이 부른
        도구인지는 이 자리에서만 알 수 있습니다 — 밖에서 기록하던 예전 방식은
        `message_id` 를 비워 둘 수밖에 없었고, 그래서 새로고침한 화면과 저장
        파일에서 도구 기록이 발언과 따로 놀았습니다.
        """
        msg_id = str(uuid.uuid4())
        if on_event:
            await on_event({
                "type": "message_stream_start",
                "message": {
                    "id": msg_id,
                    "sender_key": agent.key,
                    "sender_name": agent.name,
                    "sender_role": agent.role,
                    "content": "",
                    "round_number": round_number,
                    "msg_type": msg_type,
                },
            })

        # 이 발언이 실행한 도구. 모아 두었다가 발언 행과 **같은 커밋**에 넣습니다.
        # 도구가 끝나는 즉시 넣으면, 발언 행이 아직 없는 동안 존재하지 않는 발언을
        # 가리키는 행이 남습니다. SQLite 가 외래키를 검사하지 않아 지금은 통과할
        # 뿐이고, 누군가 PRAGMA foreign_keys 를 켜는 날 삽입이 실패합니다.
        executed_tools: List[Dict[str, Any]] = []

        async def _on_tool_call(call_log: Dict[str, Any]) -> None:
            executed_tools.append(call_log)
            if on_event:
                await on_event({
                    "type": "tool_executed",
                    "agent_key": agent.key,
                    "agent_name": agent.name,
                    "tool_call": call_log,
                })

        # 컨텍스트 한도로 무언가 생략됐다는 사실. 예전에는 logger 에만 남아,
        # 토론 기록이 사라지는 것을 보고 있는 사람이 알 방법이 없었습니다.
        def _on_context_trim(dropped: int) -> None:
            state.context_dropped += dropped
            trimmed_here.append(dropped)

        trimmed_here: List[int] = []
        streamed: List[str] = []

        async def _on_chunk(delta: str) -> None:
            streamed.append(delta)
            if on_event:
                await on_event({
                    "type": "message_stream_chunk",
                    "message_id": msg_id,
                    "delta": delta,
                })

        try:
            content, tool_logs = await self.llm_caller.call_agent(
                agent, prompt_messages, custom_instructions,
                on_tool_call=_on_tool_call, on_chunk=_on_chunk,
                session_id=state.session_id,
                budget_arbiter=self._make_budget_arbiter(control, on_event),
                context_arbiter=self._make_context_arbiter(state, agent, control, on_event),
                on_context_trim=_on_context_trim,
                mcp=self._mcp_for(state),
            )
            # 확정본이 비었는데 화면에는 글이 흘러갔다면 그 글을 남깁니다.
            # 여기서 정하는 `content` 가 DB 에 들어가는 값이라, 비워 둔 채로
            # 넘어가면 사람이 방금 읽던 발언이 새로고침 뒤에도 돌아오지 않습니다
            # (프로바이더가 텍스트를 reasoning 쪽으로만 주거나, 스트림 재조립이
            # 본문을 놓친 경우에 실제로 그렇게 됩니다).
            if not content.strip():
                content = "".join(streamed)
            final_type = msg_type
            if post_process is not None:
                # 기록과 화면에 남을 본문을 확정하기 **전에** 손봅니다. 스트리밍
                # 카드는 `message_added` 가 올 때 최종본으로 덮이므로, 여기서
                # 고치면 사람이 보는 것도 고쳐진 쪽입니다.
                try:
                    content = await post_process(content)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - 후처리 실패로 발언을 잃지 않습니다
                    logger.error(
                        f"Post-processing {agent.name}'s message failed: "
                        f"{type(exc).__name__}: {exc}",
                        exc_info=True,
                    )
        except LLMUnavailableError as exc:
            logger.warning(f"Agent '{agent.key}' produced no response: {exc}")
            content = self._unavailable_notice(agent, exc, "".join(streamed))
            tool_logs = []
            final_type = "error"
            if agent.key not in state.failed_agent_keys:
                state.failed_agent_keys.append(agent.key)
        except asyncio.CancelledError:
            # 사용자가 토론을 끊었거나 서버가 내려가는 중입니다. 실패로 적지 않고
            # 그대로 올려 보냅니다 — 여기서 삼키면 취소가 먹지 않습니다.
            raise
        except BaseException as exc:  # noqa: BLE001 - 한 발언의 사고로 토론을 끝내지 않습니다
            logger.error(
                f"Agent '{agent.key}' crashed while speaking: {type(exc).__name__}: {exc}",
                exc_info=True,
            )
            content = self._crashed_notice(agent, exc, "".join(streamed))
            tool_logs = []
            final_type = "error"
            if agent.key not in state.failed_agent_keys:
                state.failed_agent_keys.append(agent.key)

        async with (db_lock or nullcontext()):
            db.add(MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key=agent.key,
                sender_name=agent.name,
                sender_role=agent.role,
                content=content,
                round_number=round_number,
                msg_type=final_type,
                **({"created_at": created_at} if created_at is not None else {}),
            ))
            for call_log in executed_tools:
                db.add(ToolCallRecordModel(
                    id=str(uuid.uuid4()),
                    session_id=state.session_id,
                    message_id=msg_id,
                    agent_key=agent.key,
                    tool_name=call_log.get("tool_name", ""),
                    arguments=call_log.get("arguments", {}),
                    output=call_log.get("output", ""),
                    status=call_log.get("status", "success"),
                ))
            try:
                await db.commit()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                # 기록에 실패했다고 발언을 잃을 수는 없습니다. 화면에는 이미
                # 흘러갔고, 이 세션의 나머지 발언도 계속 기록되어야 합니다.
                # 롤백하지 않으면 세션이 깨진 채로 남아 다음 커밋이 전부 실패합니다.
                logger.error(
                    f"Could not persist {agent.name}'s message: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                try:
                    await db.rollback()
                except BaseException:  # noqa: BLE001
                    logger.debug("Rollback after a failed message commit also failed",
                                 exc_info=True)

        if trimmed_here and on_event:
            await on_event({
                "type": "context_trimmed",
                "agent_key": agent.key,
                "agent_name": agent.name,
                "dropped": sum(trimmed_here),
                "total_dropped": state.context_dropped,
                "where": "speech",
            })

        message = DebateMessage(
            id=msg_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=final_type,
            # 발언이 실패로 끝나면 `call_agent` 는 도구 기록을 돌려주지 못합니다.
            # 그전에 실제로 실행된 것은 남아 있어야 합니다.
            tool_calls=tool_logs or executed_tools,
        )
        state.messages.append(message)

        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    # ------------------------------------------------------------------ 유저 발언

    async def _record_user_message(
        self,
        *,
        db,
        state: DebateState,
        content: str,
        round_number: int,
        on_event: Optional[EventCallback],
    ) -> DebateMessage:
        """유저 발언을 기록에 남기고 화면에 흘립니다.

        턴을 여는 최초 요청과 토론 도중의 개입이 같은 자리에 같은 모양으로
        들어가야, 다음 발언자의 맥락(`_build_context_for_agent`)과 합성 전사가
        둘을 구분 없이 읽습니다.
        """
        msg_id = str(uuid.uuid4())
        db.add(MessageModel(
            id=msg_id,
            session_id=state.session_id,
            sender_key="user",
            sender_name="User",
            sender_role="Client / Requestor",
            content=content,
            round_number=round_number,
            msg_type="user",
        ))
        await db.commit()

        message = DebateMessage(
            id=msg_id,
            sender_key="user",
            sender_name="User",
            sender_role="Client / Requestor",
            content=content,
            round_number=round_number,
            msg_type="user",
        )
        state.messages.append(message)

        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    async def _apply_interjections(
        self,
        *,
        db,
        state: DebateState,
        control: Optional[TurnControl],
        round_number: int,
        on_event: Optional[EventCallback],
    ) -> int:
        """대기 중인 사용자 개입을 지금 시점의 토론 기록에 밀어 넣습니다.

        발언이 진행되는 중간에 끼워 넣으면, 그 발언의 프롬프트는 이미 만들어진
        뒤라 반영되지도 않으면서 기록 순서만 어긋납니다. 그래서 호출 지점은 항상
        발언과 발언 사이입니다. 여기서 들어간 메모는 다음 발언자의 맥락에 그대로
        실립니다.
        """
        if control is None:
            return 0
        notes = control.drain_notes()
        for note in notes:
            await self._record_user_message(
                db=db,
                state=state,
                content=f"[토론 중 사용자 개입]\n{note}",
                round_number=round_number,
                on_event=on_event,
            )
        state.interjection_count += len(notes)
        if notes:
            logger.info(
                f"Applied {len(notes)} user interjection(s) to session {state.session_id} "
                f"at round {round_number}"
            )
        return len(notes)

    # ------------------------------------------------------------------ 턴

    async def _session_workspace(self, session_id: str) -> Path:
        """이 대화가 쓰기로 한 작업 공간. 대화를 찾을 수 없으면 기본값."""
        async with self.session_factory() as db:
            res = await db.execute(
                select(SessionModel.workspace_dir).where(SessionModel.id == session_id)
            )
            return resolve_workspace_dir(res.scalar_one_or_none() or None)

    async def run_turn(
        self,
        session_id: str,
        user_prompt: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
    ) -> DebateState:
        """Executes a full multi-agent collaborative debate and synthesis turn.

        `control` 이 주어지면 발언과 발언 사이마다 사용자의 정지 요청과 개입
        메모를 확인합니다. 정지는 태스크를 죽이는 것이 아니라 남은 라운드를
        건너뛰고 최종 합성으로 넘어가는 것이라, 지금까지의 토론으로도 산출물이
        나옵니다.

        **이 대화의 작업 공간에 해당하는 MCP 런타임을 빌린 채로** 돕니다.
        filesystem 은 허용 경로를 argv 로, sandbox 는 `SANDBOX_WORKSPACE` 를 env 로
        기동 시점에 받으므로 폴더마다 프로세스 묶음이 따로 있어야 합니다. 같은
        폴더를 쓰는 다른 대화가 이미 빌려 놓았으면 그것을 함께 씁니다.

        반납은 턴이 **어떻게 끝나든** 일어나야 합니다 — 정상 종료, 사용자 정지,
        취소, 예외 전부. 반납을 놓치면 그 런타임은 아무도 안 쓰는 채로 종료 때까지
        남고, 그만큼 다음 대화가 쓸 자리가 줄어듭니다.
        """
        pool = get_runtime_pool()
        workspace = await self._session_workspace(session_id)
        await pool.acquire(workspace, holder=session_id)
        try:
            return await self._run_turn(session_id, user_prompt, workspace, on_event, control)
        finally:
            await pool.release(session_id, workspace)

    async def _run_turn(
        self,
        session_id: str,
        user_prompt: str,
        workspace: Path,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
    ) -> DebateState:
        """`run_turn` 의 본문. 런타임은 이미 빌린 상태로 들어옵니다."""
        async with self.session_factory() as db:
            # 1. Load session config from DB
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            res = await db.execute(stmt)
            session_model = res.scalar_one_or_none()
            if not session_model:
                raise ValueError(f"Session with ID '{session_id}' not found.")

            # 옛 이름으로 저장된 대화도 지금 쓰는 전략으로 옮겨 돌립니다.
            strategy_name = resolve_strategy_name(session_model.strategy)
            max_rounds = session_model.max_rounds
            # 병렬 지시 전략에서만 읽힙니다. 0 이나 음수는 "동시에 아무도 못 돈다"
            # 는 뜻이 되어 라운드가 통째로 비므로 최소 1 로 올립니다.
            parallel_limit = max(1, int(session_model.parallel_limit or 3))
            active_keys = session_model.active_agents or ["orchestrator", "architect", "coder", "critic"]
            custom_instructions = session_model.custom_instructions or ""

            # Ensure orchestrator is in active keys
            if "orchestrator" not in active_keys:
                active_keys = ["orchestrator"] + active_keys

            # 세션 페르소나를 적용합니다. 첫 턴이면 이 시점에 기록되고 잠깁니다.
            active_agents = await prepare_agents_for_turn(
                db, session_model, self.agent_pool, active_keys
            )
            orchestrator_agent = next(
                (a for a in active_agents if a.key == "orchestrator"),
                self.agent_pool.get_orchestrator(),
            )

            # 2. Initialize Debate State
            state = DebateState(
                session_id=session_id,
                user_prompt=user_prompt,
                workspace_dir=str(workspace),
                strategy=strategy_name,
                max_rounds=max_rounds,
                current_round=0,
                custom_instructions=custom_instructions,
                active_agent_keys=active_keys,
                status="planning",
            )

            # 이전 턴의 대화 기록을 DB에서 로드하여 대화 맥락을 보존합니다.
            stmt_prev = (
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
            res_prev = await db.execute(stmt_prev)
            prev_db_msgs = res_prev.scalars().all()
            for pm in prev_db_msgs:
                state.messages.append(
                    DebateMessage(
                        id=pm.id,
                        sender_key=pm.sender_key,
                        sender_name=pm.sender_name,
                        sender_role=pm.sender_role,
                        content=pm.content,
                        round_number=pm.round_number,
                        msg_type=pm.msg_type,
                    )
                )

            # 3. Record User Message in DB
            await self._record_user_message(
                db=db,
                state=state,
                content=user_prompt,
                round_number=0,
                on_event=on_event,
            )

            # 4. Phase 1: Master Orchestrator Goal Analysis & Planning
            state.status = "planning"
            state.current_speaker = orchestrator_agent.name
            if on_event:
                await on_event({"type": "status_changed", "status": "planning", "speaker": orchestrator_agent.name})

            if len(state.messages) > 1:
                history_snippets = []
                for m in state.messages[:-1]:
                    if m.msg_type == "error":
                        continue
                    # 자르기 **전에** 사고 과정을 뗍니다. 그러지 않으면 250자가
                    # 통째로 "Thought 1: ..." 머리말로 채워져, 정작 결론은 한 글자도
                    # 안 실립니다.
                    history_snippets.append(
                        f"{m.sender_name}({m.sender_role}): "
                        f"{strip_reasoning_trace(m.content)[:250]}"
                    )
                history_text = "\n".join(history_snippets[-6:])
                orch_plan_prompt = [
                    {"role": "user", "content": (
                        f"[이전 대화 맥락]:\n{history_text}\n\n"
                        f"[신규 User Request]:\n{user_prompt}\n\n"
                        "위의 이전 세션 논의 맥락과 새로운 사용자 요청을 종합 분석하여 이번 토론의 핵심 목표, "
                        "접근 방향, 각 에이전트에게 부여할 발언 지침을 작성하세요."
                    )}
                ]
            else:
                orch_plan_prompt = [
                    {"role": "user", "content": f"[User Request]: {user_prompt}\n\n위 요청을 분석하고 이번 토론의 핵심 목표, 접근 방향, 각 에이전트(Architect, Coder, Critic)에게 부여할 발언 지침을 작성하세요."}
                ]

            await self._speak(
                db=db,
                state=state,
                agent=orchestrator_agent,
                prompt_messages=orch_plan_prompt,
                custom_instructions=custom_instructions,
                round_number=0,
                msg_type="orchestrator",
                on_event=on_event,
                control=control,
            )

            # 5. Phase 2: Multi-Round Specialist Debate Loop
            strategy = get_strategy(strategy_name)
            state.status = "debating"

            # 계획 발언과 첫 라운드 사이도 개입이 반영되는 지점입니다.
            await self._apply_interjections(
                db=db, state=state, control=control, round_number=0, on_event=on_event
            )

            stopped_early = False
            for round_num in range(1, max_rounds + 1):
                if control is not None and control.stop_requested:
                    stopped_early = True
                    break

                state.current_round = round_num
                if on_event:
                    await on_event({
                        "type": "round_started",
                        "round": round_num,
                        "max_rounds": max_rounds,
                    })

                # 병렬 지시 전략은 라운드 전체를 다르게 돕니다 — 과업을 나눠 주고
                # 동시에 띄운 뒤 취합합니다. 발언자를 한 명씩 세우는 아래 루프와
                # 섞을 수 없어 라운드째로 갈라집니다.
                if strategy.orchestrator_dispatches_parallel:
                    stopped_early = await self._run_parallel_round(
                        db=db,
                        state=state,
                        strategy=strategy,
                        orchestrator=orchestrator_agent,
                        active_agents=active_agents,
                        round_num=round_num,
                        custom_instructions=custom_instructions,
                        parallel_limit=parallel_limit,
                        control=control,
                        on_event=on_event,
                    )
                    if stopped_early:
                        break
                    continue

                speakers = await self._select_speakers(
                    db=db,
                    state=state,
                    strategy=strategy,
                    orchestrator=orchestrator_agent,
                    active_agents=active_agents,
                    round_num=round_num,
                    custom_instructions=custom_instructions,
                    on_event=on_event,
                )

                for speaker_index, agent in enumerate(speakers):
                    # 발언과 발언 사이. 사용자의 개입과 정지는 여기서만 반영됩니다.
                    # 진행 중이던 발언을 끊지 않으므로 잘린 기록이 남지 않습니다.
                    await self._apply_interjections(
                        db=db, state=state, control=control,
                        round_number=round_num, on_event=on_event,
                    )
                    if control is not None and control.stop_requested:
                        stopped_early = True
                        break

                    state.current_speaker = agent.name
                    if on_event:
                        await on_event({
                            "type": "status_changed",
                            "status": "debating",
                            "speaker": agent.name,
                            "round": round_num,
                        })

                    await self._speak(
                        db=db,
                        state=state,
                        agent=agent,
                        prompt_messages=self._build_context_for_agent(
                            state,
                            agent,
                            strategy.turn_instruction(agent, speakers, speaker_index, state),
                        ),
                        custom_instructions=custom_instructions,
                        round_number=round_num,
                        msg_type="agent",
                        on_event=on_event,
                        control=control,
                    )

                if stopped_early:
                    break

            # 정지 요청이 마지막 발언 도중에 들어왔더라도, 그때까지 쌓인 개입은
            # 합성 전사에 실어 보냅니다.
            await self._apply_interjections(
                db=db, state=state, control=control,
                round_number=state.current_round, on_event=on_event,
            )
            state.stopped_early = stopped_early
            if stopped_early:
                logger.info(
                    f"Debate for session {session_id} stopped early by the user at "
                    f"round {state.current_round}/{max_rounds}; synthesizing what we have."
                )

            # 6. Phase 3: Final Consensus & Artifact Synthesis
            state.status = "synthesizing"
            state.current_speaker = orchestrator_agent.name
            if on_event:
                await on_event({
                    "type": "status_changed",
                    "status": "synthesizing",
                    "speaker": orchestrator_agent.name,
                })

            async def _fix_diagrams(text: str) -> str:
                return await self._repair_mermaid_blocks(
                    text,
                    agent=orchestrator_agent,
                    custom_instructions=custom_instructions,
                    state=state,
                    on_event=on_event,
                )

            synth_message = await self._speak(
                db=db,
                state=state,
                agent=orchestrator_agent,
                prompt_messages=await self._synthesis_prompt_with_notice(
                    state, orchestrator_agent, on_event
                ),
                custom_instructions=custom_instructions,
                round_number=state.current_round + 1,
                msg_type="orchestrator",
                on_event=on_event,
                post_process=_fix_diagrams,
            )
            synthesis_failed = synth_message.msg_type == "error"

            # 7. Extract and Persist Artifacts
            artifacts = self._extract_artifacts_from_synthesis(
                session_id, synth_message.content, state, synthesis_failed=synthesis_failed
            )
            for art in artifacts:
                art_db = ArtifactModel(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    artifact_type=art.artifact_type,
                    title=art.title,
                    content=art.content,
                    language=art.language,
                )
                db.add(art_db)
                art.id = art_db.id
                state.artifacts.append(art)

            await db.commit()

            # 합성이 시작된 뒤에 도착한 개입은 이번 턴에 실을 자리가 없습니다.
            # 그대로 버리면 화면은 "다음 발언 차례에 반영됩니다" 라고 알린 채 턴이
            # 끝나 버립니다. 기록에 남겨 두면 다음 턴이 맥락으로 읽어 갑니다.
            deferred = await self._apply_interjections(
                db=db, state=state, control=control,
                round_number=state.current_round + 1, on_event=on_event,
            )
            if deferred and on_event:
                await on_event({"type": "interjections_deferred", "count": deferred})

            state.status = "completed"
            # 사용자가 도중에 끊었다면 합의에 이른 것이 아닙니다.
            state.is_consensus_reached = not state.failed_agent_keys and not state.stopped_early
            if state.failed_agent_keys:
                state.error_message = (
                    "다음 에이전트가 LLM 엔드포인트에 닿지 못했습니다: "
                    + ", ".join(state.failed_agent_keys)
                )

            if on_event:
                await on_event({
                    "type": "artifacts_synthesized",
                    "artifacts": [a.model_dump() for a in state.artifacts],
                })
                await on_event({
                    "type": "turn_completed",
                    "status": "completed",
                    "failed_agents": list(state.failed_agent_keys),
                    "error_message": state.error_message,
                    "stopped_early": state.stopped_early,
                    "rounds_completed": state.current_round,
                    "max_rounds": state.max_rounds,
                })

            return state

    # ------------------------------------------------------------ 발언자 선정

    async def _record_note(
        self,
        *,
        db,
        state: DebateState,
        on_event: Optional[EventCallback],
        agent: Agent,
        content: str,
        round_number: int,
        msg_type: str,
    ) -> DebateMessage:
        """LLM 발언이 아닌 기록을 남깁니다 (지명 결과, 지명 실패 안내 등).

        `_speak` 과 같은 자리에 같은 모양으로 들어갑니다. 그래야 새로고침한 화면과
        저장 파일이 이것을 다른 발언과 똑같이 읽습니다.
        """
        msg_id = str(uuid.uuid4())
        db.add(MessageModel(
            id=msg_id,
            session_id=state.session_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=msg_type,
        ))
        await db.commit()

        message = DebateMessage(
            id=msg_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=msg_type,
        )
        state.messages.append(message)
        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    async def _select_speakers(
        self,
        *,
        db,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        active_agents: List[Agent],
        round_num: int,
        custom_instructions: str,
        on_event: Optional[EventCallback],
    ) -> List[Agent]:
        """이번 라운드에 누가 발언할지 정합니다.

        보통은 전략이 결정적으로 정합니다. '오케스트레이터 지명' 전략일 때만
        오케스트레이터에게 물어, 지금 필요한 에이전트만 부릅니다.

        지명에 실패하면 (엔드포인트가 없거나, 응답에서 아는 키를 하나도 못 찾거나)
        전략의 결정적 순서로 물러섭니다. 물러섰다는 사실은 피드에 남깁니다 —
        조용히 다른 순서로 도는 것이 제일 나쁩니다.
        """
        fallback = strategy.get_speakers_for_round(active_agents, round_num, state)
        if not strategy.orchestrator_selects_speakers or len(fallback) <= 1:
            return fallback

        async def _fall_back(why: str) -> List[Agent]:
            await self._record_note(
                db=db, state=state, on_event=on_event, agent=orchestrator,
                round_number=round_num, msg_type="error",
                content=(
                    f"[발언자 지명 실패] {why}\n"
                    f"우선순위 순서로 진행합니다: {', '.join(a.name for a in fallback)}"
                ),
            )
            return fallback

        try:
            picked, reason = await self._ask_orchestrator_for_speakers(
                orchestrator=orchestrator,
                candidates=fallback,
                state=state,
                round_num=round_num,
                custom_instructions=custom_instructions,
            )
        except LLMUnavailableError as exc:
            logger.warning(f"Speaker selection failed; using the deterministic order: {exc}")
            return await _fall_back(str(exc))

        if not picked:
            logger.warning("Orchestrator named no known agent; using the deterministic order.")
            return await _fall_back(
                "오케스트레이터의 응답에서 이번 라운드에 부를 에이전트를 찾지 못했습니다."
            )

        # 누가 왜 불렸는지는 기록에 남아야 합니다. 부르지 않은 에이전트가 있다는
        # 사실도 토론 기록을 읽는 사람에게 보여야 합니다.
        skipped = [a.name for a in fallback if a not in picked]
        summary = f"[Round {round_num} 발언권] {' → '.join(a.name for a in picked)}"
        if skipped:
            summary += f"\n(이번 라운드 미지명: {', '.join(skipped)})"
        if reason:
            summary += f"\n\n{reason}"
        await self._record_note(
            db=db, state=state, on_event=on_event, agent=orchestrator,
            round_number=round_num, msg_type="orchestrator", content=summary,
        )
        return picked

    async def _ask_orchestrator_for_speakers(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        custom_instructions: str,
    ) -> "tuple[List[Agent], str]":
        """오케스트레이터에게 이번 라운드 발언자와 순서를 물어봅니다.

        도구와 단계적 사고를 **끈 사본**으로 부릅니다. 이건 JSON 한 줄을 받는
        라우팅 호출이지 발언이 아닙니다. 도구를 붙이면 지명하려다 파일을 읽기
        시작하고, 단계적 사고 프로토콜이 주입되면 `Thought 1..N` 을 쓰다가 형식을
        놓칩니다.
        """
        selector = orchestrator.model_copy(update={
            "allowed_mcp_servers": [],
            "sequential_thinking": orchestrator.sequential_thinking.model_copy(
                update={"enabled": False}
            ),
        })

        roster = "\n".join(f"- {a.key}: {a.name} ({a.role})" for a in candidates)
        recent = [
            # 자르기 전에 사고 과정을 뗍니다 (계획 프롬프트와 같은 이유).
            f"{m.sender_name}({m.sender_role}): {strip_reasoning_trace(m.content)[:300]}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            f"[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[이번 라운드에 부를 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 입니다. 논의를 진전시키기 위해 "
            f"이번 라운드에 **꼭 필요한 에이전트만** 골라 발언 순서를 정하세요. 전원을 부를 "
            f"필요는 없고, 한 명만 불러도 됩니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            f'{{"speakers": ["에이전트키", ...], "reason": "한두 문장으로 지명 사유"}}'
        )}]

        content, _ = await self.llm_caller.call_agent(
            selector, prompt, custom_instructions, session_id=state.session_id,
            mcp=self._mcp_for(state),
        )
        return self._parse_speaker_selection(content, candidates)

    @staticmethod
    def _parse_speaker_selection(
        content: str, candidates: List[Agent]
    ) -> "tuple[List[Agent], str]":
        """응답에서 지명된 에이전트와 사유를 뽑습니다.

        JSON 이 온전하면 그것을 쓰고, 아니면 본문에서 아는 키를 **등장 순서대로**
        긁습니다. 모델이 설명을 곁들이거나 펜스를 두르는 일은 흔하고, 그때마다
        지명을 포기하면 이 전략은 결국 우선순위 순서와 같아집니다.
        """
        by_key = {a.key: a for a in candidates}
        reason = ""
        keys: List[str] = []

        block = re.search(r"\{.*\}", content or "", re.DOTALL)
        if block:
            try:
                data = json.loads(block.group(0))
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                raw = data.get("speakers")
                if isinstance(raw, list):
                    keys = [str(k).strip() for k in raw]
                reason = str(data.get("reason") or "").strip()

        if not any(k in by_key for k in keys):
            # 본문에서 키를 긁습니다. 등장 순서가 곧 발언 순서입니다.
            found = []
            for key in by_key:
                match = re.search(rf"\b{re.escape(key)}\b", content or "")
                if match:
                    found.append((match.start(), key))
            keys = [key for _, key in sorted(found)]

        picked: List[Agent] = []
        for key in keys:
            agent = by_key.get(key)
            if agent is not None and agent not in picked:
                picked.append(agent)
        return picked, reason

    # ------------------------------------------------------- 병렬 지시 라운드

    async def _run_parallel_round(
        self,
        *,
        db,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        active_agents: List[Agent],
        round_num: int,
        custom_instructions: str,
        parallel_limit: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> bool:
        """한 라운드를 병렬로 돕니다. 정지 요청으로 라운드를 접었으면 True.

        순서: 개입 반영 → 과업 분배 → 동시 실행 → 취합. 사람의 개입과 정지를 보는
        지점이 라운드 경계뿐인 것은 이 전략의 성질입니다 — 다른 전략은 발언과 발언
        사이에서 볼 수 있지만, 여기서는 그 '사이' 에 전원이 이미 달리고 있습니다.
        """
        candidates = strategy.get_speakers_for_round(active_agents, round_num, state)
        if not candidates:
            return False

        # 분배 **전에** 개입을 반영합니다. 이 라운드의 과업을 정하는 근거가
        # 되어야지, 이미 나눠 준 뒤에 들어와서는 다음 라운드까지 놀게 됩니다.
        await self._apply_interjections(
            db=db, state=state, control=control, round_number=round_num, on_event=on_event
        )
        if control is not None and control.stop_requested:
            return True

        assignments = await self._dispatch_parallel_tasks(
            db=db, state=state, strategy=strategy, orchestrator=orchestrator,
            candidates=candidates, round_num=round_num,
            custom_instructions=custom_instructions,
            parallel_limit=parallel_limit, on_event=on_event,
        )
        if not assignments:
            return False

        board = self._assignment_board(assignments)
        # 프롬프트는 **전부 먼저** 만듭니다. 코루틴 안에서 만들면 먼저 끝난 동료의
        # 발언이 늦게 시작한 쪽의 맥락에 섞여 들어가, 같은 라운드인데 누구는 남의
        # 답을 보고 누구는 못 보는 상태가 됩니다. 그건 병렬이 아닙니다.
        prompts = [
            self._build_context_for_agent(
                state, agent, self._parallel_turn_instruction(strategy, agent, task, board)
            )
            for agent, task in assignments
        ]

        # 기록 시각을 지시 순서로 박아 둡니다 (`_speak` 의 `created_at` 주석 참고).
        base_time = utc_now()
        db_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(parallel_limit)
        start_index = len(state.messages)

        async def run_one(index: int) -> DebateMessage:
            agent, _task = assignments[index]
            async with semaphore:
                return await self._speak(
                    db=db,
                    state=state,
                    agent=agent,
                    prompt_messages=prompts[index],
                    custom_instructions=custom_instructions,
                    round_number=round_num,
                    msg_type="agent",
                    on_event=on_event,
                    control=control,
                    db_lock=db_lock,
                    created_at=base_time + timedelta(milliseconds=index),
                )

        if on_event:
            await on_event({
                "type": "status_changed",
                "status": "debating",
                "speaker": " · ".join(a.name for a, _ in assignments),
                "round": round_num,
            })

        results = await asyncio.gather(
            *(run_one(i) for i in range(len(assignments))), return_exceptions=True
        )

        # 완료 순서가 아니라 지시 순서로 정렬합니다. 실시간 화면은 카드가 만들어진
        # 순서(= 지시 순서)로 보여주는데, 기록은 `created_at` 순으로 다시 읽히므로
        # 여기서 맞춰 두지 않으면 새로고침 후 순서가 달라 보입니다.
        spoken = [r for r in results if isinstance(r, DebateMessage)]
        produced = state.messages[start_index:]
        if {m.id for m in spoken} == {m.id for m in produced}:
            state.messages[start_index:] = spoken

        for (agent, _task), result in zip(assignments, results):
            if isinstance(result, BaseException):
                logger.error(
                    f"Parallel turn for '{agent.key}' failed: {type(result).__name__}: {result}",
                    exc_info=result,
                )
                if agent.key not in state.failed_agent_keys:
                    state.failed_agent_keys.append(agent.key)
                await self._record_note(
                    db=db, state=state, on_event=on_event, agent=agent,
                    round_number=round_num, msg_type="error",
                    content=(
                        f"> ⚠️ **{agent.name} 의 병렬 발언이 실패했습니다.**\n>\n"
                        f"> - 원인: `{type(result).__name__}: {result}`\n>\n"
                        f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다."
                    ),
                )

        if control is not None and control.stop_requested:
            # 정지를 원한 사람에게 취합 발언을 한 번 더 기다리게 할 이유가 없습니다.
            # 최종 합성이 곧바로 이어지고, 그것이 이 라운드의 결과도 함께 읽습니다.
            return True

        await self._merge_parallel_round(
            db=db, state=state, orchestrator=orchestrator, assignments=assignments,
            round_num=round_num, custom_instructions=custom_instructions,
            control=control, on_event=on_event,
        )
        return False

    async def _dispatch_parallel_tasks(
        self,
        *,
        db,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        candidates: List[Agent],
        round_num: int,
        custom_instructions: str,
        parallel_limit: int,
        on_event: Optional[EventCallback],
    ) -> List[Tuple[Agent, str]]:
        """이번 라운드의 과업 분배를 받아 기록하고 돌려줍니다.

        분배에 실패하면 전원을 과업 없이 돌리는 것으로 물러섭니다. 지시를 받지
        못했을 뿐 병렬이라는 성질은 남기고, 물러섰다는 사실은 피드에 남깁니다 —
        조용히 다른 방식으로 도는 것이 제일 나쁩니다.
        """
        fallback = [(agent, "") for agent in candidates]

        async def _fall_back(why: str) -> List[Tuple[Agent, str]]:
            await self._record_note(
                db=db, state=state, on_event=on_event, agent=orchestrator,
                round_number=round_num, msg_type="error",
                content=(
                    f"[과업 분배 실패] {why}\n"
                    f"과업 없이 전원을 동시에 진행합니다: "
                    f"{', '.join(a.name for a in candidates)}"
                ),
            )
            return fallback

        if len(candidates) <= 1:
            # 한 명뿐이면 나눌 것이 없습니다. 분배를 물어보는 호출만 낭비됩니다.
            assignments = fallback
            reason = ""
        else:
            try:
                assignments, reason = await self._ask_orchestrator_for_assignments(
                    orchestrator=orchestrator,
                    candidates=candidates,
                    state=state,
                    round_num=round_num,
                    parallel_limit=parallel_limit,
                    custom_instructions=custom_instructions,
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Task dispatch failed; running everyone without tasks: {exc}")
                return await _fall_back(str(exc))

            if not assignments:
                logger.warning("Orchestrator assigned nobody we know; running everyone without tasks.")
                return await _fall_back(
                    "오케스트레이터의 응답에서 과업을 맡길 에이전트를 찾지 못했습니다."
                )

        named = [agent for agent, _ in assignments]
        lines = [
            f"- **{agent.name}** ({agent.role}): {task or '(과업 지정 없음 — 전문 영역에서 자유 기여)'}"
            for agent, task in assignments
        ]
        over_limit = len(assignments) > parallel_limit
        summary = (
            f"[Round {round_num} 병렬 지시] {len(assignments)}명에게 과업을 나눴습니다"
            + (f" (동시 실행 상한 {parallel_limit} — 나머지는 순차적으로 밀립니다)" if over_limit else " (동시 실행)")
            + "\n" + "\n".join(lines)
        )
        skipped = [a.name for a in candidates if a not in named]
        if skipped:
            summary += f"\n\n(이번 라운드 미지명: {', '.join(skipped)})"
        if reason:
            summary += f"\n\n{reason}"
        await self._record_note(
            db=db, state=state, on_event=on_event, agent=orchestrator,
            round_number=round_num, msg_type="orchestrator", content=summary,
        )
        return assignments

    async def _ask_orchestrator_for_assignments(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        parallel_limit: int,
        custom_instructions: str,
    ) -> "Tuple[List[Tuple[Agent, str]], str]":
        """오케스트레이터에게 이번 라운드의 과업 분배를 물어봅니다.

        발언자 지명(`_ask_orchestrator_for_speakers`)과 같은 이유로 도구와 단계적
        사고를 끈 사본으로 부릅니다. 이건 JSON 을 받는 호출이지 발언이 아닙니다.
        """
        planner = orchestrator.model_copy(update={
            "allowed_mcp_servers": [],
            "sequential_thinking": orchestrator.sequential_thinking.model_copy(
                update={"enabled": False}
            ),
        })

        roster = "\n".join(f"- {a.key}: {a.name} ({a.role})" for a in candidates)
        recent = [
            # 자르기 전에 사고 과정을 뗍니다 (계획 프롬프트와 같은 이유).
            f"{m.sender_name}({m.sender_role}): {strip_reasoning_trace(m.content)[:300]}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            f"[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[과업을 맡길 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 이고, 지목된 에이전트는 "
            f"**동시에 각자의 과업을 수행합니다**. 서로의 이번 라운드 결과를 볼 수 없으므로 "
            f"과업이 겹치면 같은 일을 두 번 하게 됩니다.\n\n"
            f"겹치지 않게 과업을 나누세요. 전원을 부를 필요는 없고, 한 명만 불러도 됩니다. "
            f"각 과업은 다른 사람의 결과를 기다리지 않고 혼자 끝낼 수 있는 것이어야 하며, "
            f"무엇을 만들어 낼지(산출물)까지 한두 문장으로 적으세요. "
            f"동시 실행은 {parallel_limit}명까지이고 그보다 많이 부르면 나머지는 순차적으로 밀립니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            '{"assignments": [{"agent": "에이전트키", "task": "이 라운드에 맡길 구체적 과업"}], '
            '"reason": "한두 문장으로 분배 사유"}'
        )}]

        content, _ = await self.llm_caller.call_agent(
            planner, prompt, custom_instructions, session_id=state.session_id,
            mcp=self._mcp_for(state),
        )
        return self._parse_assignments(content, candidates)

    @staticmethod
    def _parse_assignments(
        content: str, candidates: List[Agent]
    ) -> "Tuple[List[Tuple[Agent, str]], str]":
        """응답에서 (에이전트, 과업) 목록과 분배 사유를 뽑습니다.

        과업 문장을 잃더라도 누구를 부를지는 건집니다. `assignments` 가 깨졌으면
        발언자 지명과 같은 방식으로 아는 키를 등장 순서대로 긁고, 과업은 빈
        문자열이 됩니다 — 지시 없는 병렬 라운드가 라운드를 통째로 날리는 것보다
        낫습니다.
        """
        by_key = {a.key: a for a in candidates}
        reason = ""
        pairs: List[Tuple[str, str]] = []

        block = re.search(r"\{.*\}", content or "", re.DOTALL)
        if block:
            try:
                data = json.loads(block.group(0))
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                reason = str(data.get("reason") or "").strip()
                raw = data.get("assignments")
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            key = str(item.get("agent") or item.get("key") or "").strip()
                            task = str(item.get("task") or item.get("instruction") or "").strip()
                            pairs.append((key, task))
                        elif isinstance(item, str):
                            pairs.append((item.strip(), ""))

        if not any(key in by_key for key, _ in pairs):
            # 분배가 깨졌습니다. 최소한 누구를 부르려 했는지는 살립니다.
            picked, scraped = OrchestratorEngine._parse_speaker_selection(content, candidates)
            reason = reason or scraped
            pairs = [(a.key, "") for a in picked]

        assignments: List[Tuple[Agent, str]] = []
        seen = set()
        for key, task in pairs:
            agent = by_key.get(key)
            if agent is None or agent.key in seen:
                continue
            seen.add(agent.key)
            assignments.append((agent, task))
        return assignments, reason

    @staticmethod
    def _assignment_board(assignments: List[Tuple[Agent, str]]) -> str:
        """동시에 도는 동료들이 무엇을 맡았는지 적은 판.

        결과는 못 보여 주지만 **누가 무엇을 하는지**는 알려 줄 수 있습니다. 이것이
        없으면 여럿이 같은 표를 각자 그려 오고, 취합이 중복 제거부터 시작합니다.
        """
        return "\n".join(
            f"- {agent.name}({agent.role}): {task or '(과업 지정 없음)'}"
            for agent, task in assignments
        )

    @staticmethod
    def _parallel_turn_instruction(
        strategy: BaseDebateStrategy, agent: Agent, task: str, board: str
    ) -> str:
        """병렬 라운드에서 한 에이전트에게 붙는 지침 = 내 과업 + 동시 실행 현황."""
        if task:
            head = f"[병렬 지시] 오케스트레이터가 이번 라운드에 당신에게 맡긴 과업입니다:\n{task}"
        else:
            # 분배가 실패한 라운드. 전략이 들고 있는 문구를 그대로 씁니다.
            head = strategy.turn_instruction(
                agent, [agent], 0, DebateState(session_id="", user_prompt="")
            )

        return (
            f"{head}\n\n"
            f"[동시 진행 중]\n{board}\n\n"
            f"이들은 지금 당신과 **같은 시각에** 답하고 있어 이번 라운드 결과를 볼 수 없습니다. "
            f"남의 과업을 대신 하지 말고 당신 몫을 끝까지 마치세요. 다른 과업의 결과가 필요하면 "
            f"추측해 채우지 말고 어떤 가정을 두었는지 명시하세요. 라운드 끝에 오케스트레이터가 "
            f"결과를 합칩니다."
        )

    async def _merge_parallel_round(
        self,
        *,
        db,
        state: DebateState,
        orchestrator: Agent,
        assignments: List[Tuple[Agent, str]],
        round_num: int,
        custom_instructions: str,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> DebateMessage:
        """라운드 끝의 취합 발언. 병렬 결과를 붙이고 충돌과 남은 쟁점을 정리합니다.

        이 발언이 다음 라운드 분배의 입력이 됩니다. 없으면 서로를 못 본 독백들이
        그대로 최종 합성까지 실려 가고, 모순은 거기서 처음 발견됩니다.
        """
        state.current_speaker = orchestrator.name
        if on_event:
            await on_event({
                "type": "status_changed",
                "status": "debating",
                "speaker": f"{orchestrator.name} (취합)",
                "round": round_num,
            })

        board = self._assignment_board(assignments)
        instruction = (
            f"[Round {round_num} 취합] 방금 다음 에이전트가 **동시에** 각자의 과업을 수행했습니다:\n"
            f"{board}\n\n"
            f"이들은 서로의 결과를 보지 못한 채 답했습니다. 수석 오케스트레이터로서 "
            f"이번 라운드의 결과를 하나로 붙이세요:\n"
            f"1. 통합된 현재 결론 (무엇이 정해졌는가)\n"
            f"2. 서로 어긋나는 지점과 그 판정 (누구 말이 맞는지, 아직 판단할 수 없다면 그 이유)\n"
            f"3. 각자가 세운 가정 중 아직 검증되지 않은 것\n"
            f"4. 다음 라운드로 넘길 미해결 과제\n\n"
            f"발언하지 못했거나 실패한 에이전트의 몫을 지어내지 마세요."
        )

        return await self._speak(
            db=db,
            state=state,
            agent=orchestrator,
            prompt_messages=self._build_context_for_agent(state, orchestrator, instruction),
            custom_instructions=custom_instructions,
            round_number=round_num,
            msg_type="orchestrator",
            on_event=on_event,
            control=control,
        )

    def _build_context_for_agent(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str = "",
    ) -> List[Dict[str, Any]]:
        """Prepares discussion transcript for agent turn.

        `turn_instruction` 은 전략이 이 차례에 붙이는 지침입니다. 전략이 순서만
        정하던 시절에는 '자유 토론' 과 '순차 검증' 이 똑같은 프롬프트를 받아,
        발언 순서 말고는 다를 것이 없었습니다. 두 전략의 실제 차이가 여기서
        갈립니다.
        """
        context: List[Dict[str, Any]] = []
        context.append({
            "role": "user",
            "content": f"[User Goal / Current Request]:\n{state.user_prompt}\n\n[Debate Progress]: Round {state.current_round} of {state.max_rounds}."
        })

        for msg in state.messages:
            # 응답을 못 받은 자리는 맥락에 넣지 않습니다. 실패 안내문을 발언인 양
            # 읽히게 하면 다음 에이전트가 그것을 논평하기 시작합니다.
            if msg.msg_type == "error":
                continue
            if msg.sender_key == "user":
                context.append({
                    "role": "user",
                    "content": f"[User]:\n{msg.content}"
                })
            else:
                role_label = f"[{msg.sender_name} ({msg.sender_role})]"
                context.append({
                    "role": "assistant" if msg.sender_key == agent.key else "user",
                    # 사고 과정은 기록과 화면에만 남기고 프롬프트에는 싣지 않습니다.
                    # 자기 발언도 같습니다 — `show_steps` 는 사람이 무엇을 볼지를
                    # 정하는 스위치이지, 모델이 무엇을 읽을지를 정하는 것이 아닙니다.
                    "content": f"{role_label}:\n{strip_reasoning_trace(msg.content)}"
                })

        turn_prompt = (
            f"이제 {agent.name}({agent.role})님의 차례입니다. 앞선 전체 논의 맥락과 직전 "
            f"발언들을 충실히 반영하여 전문적인 의견을 발언하고 필요시 도구를 활용해 주세요."
        )
        if turn_instruction:
            turn_prompt += f"\n\n{turn_instruction}"
        context.append({"role": "user", "content": turn_prompt})
        return context

    async def _synthesis_prompt_with_notice(
        self,
        state: DebateState,
        agent: Optional[Agent],
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """합성 프롬프트를 만들고, 전사가 잘렸으면 화면에 알립니다.

        최종 보고서가 초반 논의를 못 보고 쓰였다는 사실은 사람이 알아야 합니다 —
        예전에는 `logger.warning` 에만 남았습니다.
        """
        before = state.context_dropped
        prompt = self._build_synthesis_prompt(state, agent)
        dropped = state.context_dropped - before
        if dropped and on_event:
            await on_event({
                "type": "context_trimmed",
                "agent_key": agent.key if agent else "orchestrator",
                "agent_name": agent.name if agent else "Orchestrator",
                "dropped": dropped,
                "total_dropped": state.context_dropped,
                "where": "synthesis",
            })
        return prompt

    def _memory_search_tool_for(
        self, agent: Optional[Agent], state: Optional[DebateState] = None,
    ) -> Optional[str]:
        """이 에이전트가 실제로 쓸 수 있는 메모리 검색 도구의 이름. 없으면 None.

        서버 키가 아니라 **주어진 도구 목록**에서 찾습니다. 서버를 껐거나 연결에
        실패했으면 없는 도구를 가리키게 되기 때문입니다. 그 목록은 이 턴이 빌린
        런타임에서 나옵니다 — 대화마다 작업 공간이 다르면 뜬 서버도 다릅니다.
        """
        if agent is None:
            return None
        mcp = (self._mcp_for(state) if state else None) or self.llm_caller.mcp_manager
        try:
            tools = mcp.get_openai_tools_for_servers(
                self.llm_caller.resolve_tool_servers(agent)
            )
        except Exception:  # noqa: BLE001 - 도구 목록을 못 구해도 합성은 진행합니다
            return None
        return memory_search_tool(tools)

    def _build_synthesis_prompt(
        self, state: DebateState, agent: Optional[Agent] = None
    ) -> List[Dict[str, Any]]:
        """Constructs prompt for orchestrator final consensus & artifact generation.

        전사(transcript)를 오케스트레이터의 컨텍스트 한도에 맞춰 자릅니다.
        여기는 한 개의 user 메시지 안에 토론 전체가 통째로 들어가는 자리라,
        `fit_context_window()` 가 손댈 수 있는 것이 없습니다 (메시지 단위로
        덜어내는데 덜어낼 메시지가 없습니다). 라운드가 몇 번만 돌아도 한도를
        넘어 400 이 나므로, 만드는 쪽에서 크기를 정해야 합니다.

        최근 발언부터 채웁니다. 뒤로 갈수록 앞선 논의가 반영된 결론이라,
        잘라야 한다면 앞쪽을 버리는 편이 낫습니다.
        """
        usable = [m for m in state.messages if m.msg_type != "error"]

        def render(msg: DebateMessage) -> str:
            prefix = "### [User]" if msg.sender_key == "user" else f"### {msg.sender_name} ({msg.sender_role})"
            body = msg.content if msg.sender_key == "user" else strip_reasoning_trace(msg.content)
            return f"{prefix}:\n{body}\n"

        if agent is None:
            kept, dropped = [render(m) for m in usable], 0
        else:
            # 응답 분량과 지시문 몫을 빼고 남는 것이 전사의 예산입니다.
            budget = agent.max_context_window - agent.max_tokens - 1024
            kept_rev: List[str] = []
            dropped = 0
            for msg in reversed(usable):
                block = render(msg)
                probe = [{"role": "user", "content": "\n".join([block] + kept_rev)}]
                if kept_rev and budget > 0 and estimate_tokens(agent.model, probe) > budget:
                    dropped = len(usable) - len(kept_rev)
                    break
                kept_rev.insert(0, block)
            kept = kept_rev

        if dropped:
            logger.warning(
                f"Synthesis transcript trimmed: dropped {dropped} of {len(usable)} message(s) "
                f"(max_context_window={agent.max_context_window})"
            )
            # 생략 건수를 상태에 누적합니다. 반환값을 바꾸는 대신 이 자리를 쓰는
            # 것은, 이 메서드가 프롬프트 문자열을 돌려준다는 계약을 호출부와
            # 테스트가 이미 쓰고 있기 때문입니다.
            state.context_dropped += dropped
            memory_tool = self._memory_search_tool_for(agent, state)
            notice = context_trim_notice(dropped, memory_tool)
            if memory_tool:
                notice += (
                    f"\n[생략된 초반 라운드가 결론에 필요하면 `{memory_tool}` 로 "
                    f"이 대화의 그래프를 찾아보세요 — 토론 중에 에이전트들이 "
                    f"핵심을 그곳에 남겼을 수 있습니다.]"
                )
            kept.insert(0, notice + "\n")

        full_transcript = "\n".join(kept)

        early_stop = ""
        if state.stopped_early:
            # 남은 라운드에서 나왔을 반론을 지어내면, 검증되지 않은 결론이 검증된
            # 것처럼 보고서에 올라갑니다.
            early_stop = (
                f"\n[주의] 사용자가 예정된 라운드보다 일찍 토론을 정지시켰습니다 "
                f"(진행: {state.current_round}/{state.max_rounds} 라운드). 남은 라운드에서 "
                f"나왔을 의견을 추측해 채우지 말고, 지금까지 오간 논의만으로 정리하되 "
                f"아직 검토되지 못한 쟁점을 보고서에 명시하세요.\n"
            )

        missing = ""
        if state.failed_agent_keys:
            # 누가 빠졌는지 알려야, 오케스트레이터가 없는 의견을 있는 것처럼 요약하지 않습니다.
            missing = (
                f"\n[주의] 다음 에이전트는 LLM 연결 실패로 이번 토론에서 발언하지 못했습니다: "
                f"{', '.join(state.failed_agent_keys)}. 이들의 의견을 추측해서 채우지 말고, "
                f"보고서에 누락 사실을 명시하세요.\n"
            )

        prompt = (
            f"[User Goal]: {state.user_prompt}\n\n"
            f"[Full Multi-Agent Debate Transcript]:\n{full_transcript}\n"
            f"{early_stop}"
            f"{missing}\n"
            f"수석 오케스트레이터로서 모든 토론과 피드백을 통합하여 최종 합의 보고서를 작성하세요.\n"
            f"반드시 다음 항목들을 포함해야 합니다:\n"
            f"1. **최종 합의 요약 및 아키텍처 결정 사항 (Summary & Architecture)**\n"
            f"2. **Mermaid 다이어그램** (```mermaid 블록. 노드 라벨에 괄호를 쓸 때는 "
            f'A["결제 서비스 (Payment)"] 처럼 반드시 큰따옴표로 감쌀 것)\n'
            f"3. **완전한 실행 가능 소스 코드** (```python 블록)\n"
            f"4. **품질/보안 점검표 및 엣지 케이스 대응 전략**"
        )
        return [{"role": "user", "content": prompt}]

    # ------------------------------------------------- 다이어그램 문법 되돌리기

    async def _repair_mermaid_blocks(
        self,
        text: str,
        *,
        agent: Agent,
        custom_instructions: str,
        state: DebateState,
        on_event: Optional[EventCallback],
    ) -> str:
        """합성 본문의 Mermaid 다이어그램을 검사하고, 틀렸으면 고쳐 받습니다.

        예전에는 문법이 어긋난 다이어그램이 그대로 아티팩트가 되었고, 사람이 탭을
        열었을 때 비로소 "Mermaid 문법 오류" 를 보았습니다. 그때는 토론이 이미
        끝나 고칠 사람이 없습니다 — 다시 물어보려면 새 턴을 돌려야 하고, 그러면
        라운드를 통째로 다시 씁니다.

        여기서는 **쓴 사람에게 그 자리에서 돌려줍니다.** 오류 메시지와 문제가 된
        줄을 그대로 주고 고쳐 달라고 하며, `MERMAID_REPAIR_ATTEMPTS` 번까지
        반복합니다. 끝내 못 고치면 원문을 그대로 둡니다 — 지어낸 다이어그램으로
        바꾸느니, 틀린 채로 두고 무엇이 틀렸는지 알리는 편이 낫습니다.

        검사는 `lint_mermaid` 가 합니다. 확실한 오류만 잡도록 만들어져 있어,
        멀쩡한 그림을 두고 모델을 다시 부르는 일은 없습니다.
        """
        blocks = find_mermaid_blocks(text)
        if not blocks:
            return text

        broken = [
            (i, b, issues)
            for i, b in enumerate(blocks)
            if (issues := lint_mermaid(normalize_mermaid(b["code"])))
        ]
        if not broken:
            return text

        logger.warning(
            f"{len(broken)} of {len(blocks)} mermaid diagram(s) in {agent.name}'s synthesis "
            f"failed the syntax check: "
            + "; ".join(f"#{i + 1} {[x.rule for x in issues]}" for i, _b, issues in broken)
        )

        fixer = agent.model_copy(update={
            "allowed_mcp_servers": [],
            "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
        })

        current = text
        for attempt in range(1, MERMAID_REPAIR_ATTEMPTS + 1):
            if on_event:
                await on_event({
                    "type": "mermaid_repair_started",
                    "agent_name": agent.name,
                    "broken": len(broken),
                    "total": len(blocks),
                    "attempt": attempt,
                    "max_attempts": MERMAID_REPAIR_ATTEMPTS,
                })

            try:
                fixed = await self._ask_for_fixed_diagrams(
                    fixer, broken, state, custom_instructions, attempt
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Mermaid repair call failed: {exc}")
                break

            if not fixed:
                logger.warning("The repair answer contained no mermaid block; giving up on this pass.")
                break

            current = self._splice_blocks(current, broken, fixed)

            blocks = find_mermaid_blocks(current)
            broken = [
                (i, b, issues)
                for i, b in enumerate(blocks)
                if (issues := lint_mermaid(normalize_mermaid(b["code"])))
            ]
            if not broken:
                logger.info(f"Mermaid diagrams fixed on attempt {attempt}.")
                if on_event:
                    await on_event({
                        "type": "mermaid_repair_finished",
                        "agent_name": agent.name,
                        "resolved": True,
                        "attempts": attempt,
                        "remaining": 0,
                    })
                return current

        if on_event:
            await on_event({
                "type": "mermaid_repair_finished",
                "agent_name": agent.name,
                "resolved": False,
                "attempts": MERMAID_REPAIR_ATTEMPTS,
                "remaining": len(broken),
            })
        logger.warning(
            f"{len(broken)} mermaid diagram(s) still fail the syntax check after "
            f"{MERMAID_REPAIR_ATTEMPTS} attempt(s); keeping the original."
        )
        return current

    async def _ask_for_fixed_diagrams(
        self,
        fixer: Agent,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        state: DebateState,
        custom_instructions: str,
        attempt: int,
    ) -> List[str]:
        """틀린 다이어그램만 모아 고쳐 달라고 하고, 받은 블록을 순서대로 돌려줍니다.

        보고서 전체를 다시 쓰게 하지 않습니다. 다시 쓰면 결론이 흔들리고 길이도
        감당할 수 없습니다 — 고쳐야 하는 것은 문법이지 내용이 아닙니다.
        """
        parts = [
            "방금 작성한 보고서의 Mermaid 다이어그램이 문법 오류로 렌더링되지 않습니다.",
            "",
            f"아래 {len(broken)}개를 고쳐서 다시 주세요. **다이어그램만** 주면 됩니다 — "
            "보고서 본문은 다시 쓰지 마세요.",
            "",
        ]
        for order, (_index, block, issues) in enumerate(broken, start=1):
            parts += [
                f"### 다이어그램 {order}",
                "",
                "렌더러가 지적한 곳:",
                format_issues(issues),
                "",
                "원본:",
                "```mermaid",
                block["code"],
                "```",
                "",
            ]
        parts += [
            "---",
            "",
            f"고친 다이어그램 {len(broken)}개를 **같은 순서로**, 각각 ```mermaid 코드 블록"
            " 하나씩으로 출력하세요. 다른 설명은 붙이지 마세요.",
            "",
            "지킬 것:",
            "- 라벨에 괄호·따옴표·특수문자가 있으면 큰따옴표로 감싸세요: `A[\"결제 (PG)\"]`",
            "- `end` 는 예약어입니다. 노드 이름으로 쓰지 마세요 (`END` 는 됩니다).",
            "- `subgraph` 는 반드시 `end` 로 닫으세요.",
            "- 첫 줄은 `graph TD` 처럼 다이어그램 종류 선언이어야 합니다.",
            "- **의미는 바꾸지 마세요.** 노드와 관계는 그대로 두고 문법만 고치세요.",
        ]
        if attempt > 1:
            parts.append(
                f"- 이번이 {attempt}번째 시도입니다. 앞서 고친 것도 같은 이유로 실패했으니, "
                f"의심스러운 라벨은 전부 큰따옴표로 감싸세요."
            )

        content, _ = await self.llm_caller.call_agent(
            fixer, [{"role": "user", "content": "\n".join(parts)}],
            custom_instructions, session_id=state.session_id,
            mcp=self._mcp_for(state),
        )
        return [b["code"] for b in find_mermaid_blocks(content or "")]

    @staticmethod
    def _splice_blocks(
        text: str,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        replacements: List[str],
    ) -> str:
        """고친 다이어그램을 원문의 제자리에 끼워 넣습니다.

        뒤에서부터 바꿉니다. 앞에서부터 바꾸면 길이가 달라지면서 뒤쪽 블록의
        위치가 어긋납니다.

        받은 개수가 모자라면 받은 만큼만 바꿉니다 — 모델이 다이어그램 하나를
        빠뜨렸다고 나머지 고친 것까지 버릴 이유는 없습니다.
        """
        pairs = list(zip(broken, replacements))
        for (_index, block, _issues), fixed in sorted(pairs, key=lambda x: -x[0][1]["start"]):
            text = text[:block["start"]] + fixed + text[block["end"]:]
        return text

    def _extract_artifacts_from_synthesis(
        self,
        session_id: str,
        synth_text: str,
        state: DebateState,
        synthesis_failed: bool = False,
    ) -> List[ArtifactItem]:
        """Extracts markdown, code, and mermaid artifacts from the synthesis text."""
        artifacts: List[ArtifactItem] = []

        # 1. Full Synthesized Markdown Document
        artifacts.append(
            ArtifactItem(
                artifact_type="markdown",
                title=(
                    "합성 실패 (LLM 연결 끊김)" if synthesis_failed
                    else "종합 아키텍처 & 산출물 보고서 (Final Synthesis Report)"
                ),
                content=synth_text,
                language="markdown",
            )
        )

        # 2. Extract code blocks
        code_blocks = extract_code_blocks(synth_text)
        code_idx = 1
        mermaid_idx = 1
        for block in code_blocks:
            lang = block["language"]
            code = block["code"]
            if lang == "mermaid":
                artifacts.append(
                    ArtifactItem(
                        artifact_type="mermaid",
                        title=f"시스템 아키텍처 다이어그램 #{mermaid_idx}",
                        content=normalize_mermaid(code),
                        language="mermaid",
                    )
                )
                mermaid_idx += 1
            elif lang in CODE_LANGUAGES:
                artifacts.append(
                    ArtifactItem(
                        artifact_type="code",
                        title=f"핵심 구현 소스코드 ({lang}) #{code_idx}",
                        content=code,
                        language=lang if lang not in ["py"] else "python",
                    )
                )
                code_idx += 1

        # 2-b. 합성 보고서에 다이어그램이 없으면 토론 본문에서 찾습니다.
        #      아키텍트가 그린 다이어그램이 최종 보고서에 다시 실리지 않는 경우가
        #      잦고 (답변 길이 제한), 그때마다 다이어그램 탭이 통째로 비었습니다.
        if mermaid_idx == 1:
            for msg in reversed(state.messages):
                if msg.msg_type == "error" or msg.sender_key == "user":
                    continue
                found = [b for b in extract_code_blocks(msg.content) if b["language"] == "mermaid"]
                if not found:
                    continue
                for block in found:
                    artifacts.append(
                        ArtifactItem(
                            artifact_type="mermaid",
                            title=f"시스템 아키텍처 다이어그램 #{mermaid_idx} ({msg.sender_name} 제안)",
                            content=normalize_mermaid(block["code"]),
                            language="mermaid",
                        )
                    )
                    mermaid_idx += 1
                break

        # 3. JSON Summary Artifact
        json_summary = {
            "session_id": session_id,
            "goal": state.user_prompt,
            "strategy": state.strategy,
            "total_rounds": state.current_round,
            "participating_agents": state.active_agent_keys,
            "failed_agents": list(state.failed_agent_keys),
            "total_messages": len(state.messages),
            "consensus_reached": not state.failed_agent_keys,
        }
        artifacts.append(
            ArtifactItem(
                artifact_type="json",
                title="세션 메타데이터 & 토론 요약 (JSON)",
                content=json.dumps(json_summary, indent=2, ensure_ascii=False),
                language="json",
            )
        )

        return artifacts


_orchestrator_engine: Optional[OrchestratorEngine] = None


def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
