import asyncio
import json
import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional, Set, Tuple
from sqlalchemy import delete, select, update
from app.agents.base import Agent
from app.agents.llm import (
    SPEECH_STATE_VERSION,
    LLMCaller,
    LLMUnavailableError,
    context_budget,
    estimate_tokens,
    memory_search_tool,
    strip_reasoning_trace,
)
from app.agents import llm_fallback
from app.agents.llm_gate import LLM_HOLDER
from app.agents.personas import frozen_agents, prepare_agents_for_turn
from app.agents.skills import skill_tools_for, with_designated_skills
from app.agents.consultation import consultation_scope, consultation_tools_for
from app.agents.pool import AgentPool, get_agent_pool
from app.config import (
    DATA_DIR,
    SKILL_NAME_PATTERN,
    TOOL_ITERATION_CEILING,
    PlanApprovalConfig,
    get_config,
    resolve_workspace_dir,
)
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool
from app.database.models import (
    TURN_FAILED,
    TURN_RUNNING,
    TURN_UNFINISHED,
    MessageModel,
    SessionModel,
    SpeechDraftModel,
    ToolCallRecordModel,
    TurnModel,
    utc_now,
)
from app.database.session import get_session_factory
from app.orchestration import context_memory as memory
from app.orchestration import plan_gate, turns
from app.orchestration.graph import Activation, GraphScheduler, GraphSpec, parse_graph
from app.workspace_files import mention_token
from app.orchestration.control import TurnControl
from app.orchestration.tool_gate import ToolGate
from app.orchestration.state import ArtifactItem, DebateMessage, DebateState
from app.orchestration.strategies import (
    BaseDebateStrategy,
    get_strategy,
    resolve_strategy_name,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


def plan_approval_settings() -> PlanApprovalConfig:
    """계획 승인 설정 (ADR-028). 도구 보안처럼 **지금의** conf.json 에서 읽습니다.

    대화가 시작된 뒤에 켜거나 끈 것도 다음 계획부터 걸려야 합니다 (에이전트 설정처럼 대화에
    고정되지 않습니다). 설정을 읽지 못하면 켠 것으로 봅니다 — 승인을 건너뛰는 쪽으로 틀리면
    확인받지 않은 계획이 그대로 돕니다.
    """
    try:
        return get_config().plan_approval
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not read plan_approval settings; keeping the gate on: {exc}")
        return PlanApprovalConfig()


@dataclass
class TurnSetup:
    """한 턴의 구성과 참여자 (`OrchestratorEngine._setup_turn`)."""

    strategy_name: str
    max_rounds: int
    parallel_limit: int
    active_keys: List[str]
    custom_instructions: str
    active_agents: List[Agent]
    orchestrator: Agent
    graph_spec: Optional[GraphSpec]
    memory_budget: int
    # 입력창에서 @전문가 @스킬 로 지정한 것 (전문가 키 → 스킬). 이 턴에만 겁니다.
    skill_designations: Dict[str, List[str]] = field(default_factory=dict)
    # 동료 자문(peers)의 상대가 될 수 있는 이 대화의 에이전트 전부. 참여자는 이번 턴의 사본이고,
    # 참여하지 않는 에이전트는 첫 턴에 고정한 스냅샷입니다 — 자문 상대를 고른 에이전트는 이번
    # 토론에 참여하지 않는 전문가에게도 물을 수 있습니다 (`peer_consultation.targets`).
    consult_pool: List[Agent] = field(default_factory=list)

    def config(self, workspace: Path) -> Dict[str, Any]:
        """턴 기록에 고정해 둘 구성 (`TurnModel.config`). 끊긴 턴은 이 구성으로 마칩니다."""
        return {
            "strategy": self.strategy_name,
            "max_rounds": self.max_rounds,
            "parallel_limit": self.parallel_limit,
            "active_agents": list(self.active_keys),
            "custom_instructions": self.custom_instructions,
            "workspace_dir": str(workspace),
            "skill_designations": {k: list(v) for k, v in self.skill_designations.items()},
        }


def clean_skill_designations(
    designations: Optional[Dict[str, Any]], active_keys: List[str],
) -> Dict[str, List[str]]:
    """스킬 지정에서 이 턴에 쓸 수 있는 것만 남깁니다.

    참여하지 않는 전문가와 오케스트레이터(지목 대상이 아닙니다), 스킬 이름 규칙에 맞지 않는
    이름은 뺍니다. 지정은 화면이 보낸 값이고, 이름은 그대로 `allowed_skills` 에 더해지므로
    폴더 이름 규칙으로 거릅니다. 스킬이 실제로 있는지·켜져 있는지는 발언할 때 봅니다 (실시간).
    """
    active = set(active_keys) - {"orchestrator"}
    out: Dict[str, List[str]] = {}
    for key, names in (designations or {}).items():
        if key not in active or not isinstance(names, (list, tuple)):
            continue
        kept = [
            n for n in dict.fromkeys(str(x) for x in names)
            if SKILL_NAME_PATTERN.fullmatch(n)
        ]
        if kept:
            out[key] = kept
    return out


from app.orchestration.phases import (
    DebatePhaseExecutor,
    GraphTurnError,
    PlanningPhaseExecutor,
    SynthesisPhaseExecutor,
)

# 커밋이 실패했을 때 다시 시도하기 전에 기다리는 시간(초). 시도는 이것보다 한 번 많습니다.
PERSIST_RETRY_DELAYS = (2.0, 5.0)

# 스트리밍 조각 이벤트를 모아 보내는 간격(초).
STREAM_EVENT_INTERVAL = 0.1



# DB 에 끝내 기록하지 못한 발언·산출물을 남기는 폴더.
#
# 예전에는 기록 실패를 로그 한 줄로 남기고 넘어갔습니다. 화면에는 이미 흘러갔으니
# 괜찮아 보이지만, 새로고침하거나 앱을 다시 켜면 **최종 합성 보고서가 사라졌습니다.**
# 실제로 자리를 비운 사이 `database is locked` 로 그렇게 됐습니다. 대화의 산출물을
# 조용히 잃는 것보다는, DB 가 아닌 곳에라도 남기고 사람에게 알리는 편이 낫습니다.
UNSAVED_DIR = DATA_DIR / "unsaved"


def save_unpersisted(
    *,
    kind: str,
    session_id: str,
    title: str,
    body: str,
    error: BaseException,
    directory: Optional[Path] = None,
) -> Optional[Path]:
    """DB 에 기록하지 못한 내용을 마크다운 파일로 남깁니다. 파일마저 못 쓰면 None.

    파일 이름에 무작위 꼬리를 붙이는 이유: 같은 초에 두 발언이 실패하면 앞의 것을
    덮어씁니다.
    """
    folder = directory or UNSAVED_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{session_id[:8]}-{kind}-{uuid.uuid4().hex[:6]}.md"
    first_line = str(error).splitlines()[0] if str(error) else ""
    header = (
        f"<!-- MADO: DB 에 기록하지 못한 {kind} -->\n"
        f"<!-- session_id: {session_id} -->\n"
        f"<!-- saved_at: {datetime.now().astimezone().isoformat(timespec='seconds')} -->\n"
        f"<!-- error: {type(error).__name__}: {first_line} -->\n\n"
        f"# {title}\n\n"
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(header + body, encoding="utf-8")
        return path
    except OSError as exc:
        logger.error(f"Could not even write the unsaved {kind} to {path}: {exc}")
        return None


class OrchestratorEngine:
    """Multi-Agent Orchestration & Debate Execution Engine."""

    def __init__(self, agent_pool: Optional[AgentPool] = None, llm_caller: Optional[LLMCaller] = None):
        self.agent_pool = agent_pool or get_agent_pool()
        self.llm_caller = llm_caller or LLMCaller()
        self.session_factory = get_session_factory()
        # 진행 중인 턴의 도구 보안 문지기 (세션 id → 게이트). 턴이 열 때 만들고 닫을 때
        # 치웁니다 (`run_turn`). 발언뿐 아니라 지명·계획·요약 같은 보조 호출도 에이전트의
        # 도구를 들고 나가므로, 모든 `call_agent` 가 같은 게이트를 거칩니다.
        self._tool_gates: Dict[str, ToolGate] = {}
        self._planning_phase = PlanningPhaseExecutor(self)
        self._debate_phase = DebatePhaseExecutor(self)
        self._synthesis_phase = SynthesisPhaseExecutor(self)

    @property
    def planning_phase(self) -> PlanningPhaseExecutor:
        phase = getattr(self, "_planning_phase", None)
        if phase is None:
            phase = PlanningPhaseExecutor(self)
            self._planning_phase = phase
        return phase

    @property
    def debate_phase(self) -> DebatePhaseExecutor:
        phase = getattr(self, "_debate_phase", None)
        if phase is None:
            phase = DebatePhaseExecutor(self)
            self._debate_phase = phase
        return phase

    @property
    def synthesis_phase(self) -> SynthesisPhaseExecutor:
        phase = getattr(self, "_synthesis_phase", None)
        if phase is None:
            phase = SynthesisPhaseExecutor(self)
            self._synthesis_phase = phase
        return phase


    # ------------------------------------------------------------------ 작업 공간

    async def set_tool_rules(self, session_id: str, grants: List[str], denials: List[str]) -> bool:
        """진행 중인 턴의 "이 대화에서" 규칙을 갈아 끼웁니다. 도는 턴이 없으면 False.

        True 면 저장까지 게이트가 했습니다. 토론 중에는 게이트가 이 목록의 유일한
        기록자여야, 카드에서 방금 더한 규칙을 화면의 옛 목록이 덮지 않습니다.
        """
        gate = getattr(self, "_tool_gates", {}).get(session_id)
        if gate is None:
            return False
        await gate.replace_rules(grants, denials)
        return True

    def set_tool_mode(self, session_id: str, mode: str) -> bool:
        """진행 중인 턴의 도구 보안 모드를 바꿉니다. 도는 턴이 없으면 False.

        저장은 화면이 합니다 (세션 DB). 여기서는 이미 세워진 문지기에 알려, 수많은 승인
        카드를 보고 모드를 바꾼 사람이 다음 턴까지 기다리지 않게 합니다.
        """
        gate = getattr(self, "_tool_gates", {}).get(session_id)
        if gate is None:
            return False
        gate.set_mode(mode)
        return True

    def _gate_for(self, state: DebateState) -> Optional[ToolGate]:
        """이 턴의 도구 보안 문지기. 턴 밖에서 부른 발언(테스트)이면 None."""
        return getattr(self, "_tool_gates", {}).get(state.session_id)

    def _rule_saver(
        self, session_id: str,
    ) -> Callable[[List[str], List[str]], Coroutine[Any, Any, None]]:
        """"이 대화에서 허용·거부" 목록을 세션에 저장하는 함수. 다음 턴의 게이트가 읽습니다."""

        async def save(grants: List[str], denials: List[str]) -> None:
            async with self.session_factory() as db:
                row = await db.get(SessionModel, session_id)
                if row is not None:
                    row.tool_grants = list(grants)
                    row.tool_denials = list(denials)
                    await db.commit()

        return save

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
            f"> 이 자리에 들어갈 내용을 임의로 생성하지 않았습니다. "
            f"엔드포인트를 복구하신 후 같은 요청을 다시 전달해 주십시오."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 연결이 끊기기 전까지 수신된 내용입니다.)"
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
            f"> 이 자리에 들어갈 내용을 임의로 생성하지 않았습니다. "
            f"토론은 나머지 에이전트로 계속 진행됩니다 (자세한 원인은 서버 로그를 확인해 주십시오)."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 오류가 발생하기 전까지 수신된 내용입니다.)"
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
        turn_started_at: Optional[datetime] = None,
        graph_node_id: Optional[str] = None,
        graph_port: Optional[str] = None,
        kind: str = turns.KIND_SPEECH,
    ) -> DebateMessage:
        """한 에이전트의 발언을 스트리밍하고, DB 에 기록하고, 상태에 반영합니다.

        `kind` 는 이 발언이 턴의 흐름에서 맡은 자리입니다 (`turns.KIND_*`). 끊긴 턴을 다시
        세울 때 기록을 이것으로 읽습니다.

        `turn_started_at` 은 이 발언이 턴을 마무리하는 합성 발언일 때만 줍니다.
        그 턴의 총 경과 시간을 기록에서 다시 계산할 수 있게 함께 적습니다.

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
        # 끊긴 턴을 이어 가는 중이고 이 발언의 초안이 남아 있으면, 처음부터가 아니라 초안의 마지막
        # 판 다음부터 이어 갑니다 (ADR-025). 발언 id 와 시작 시각은 초안의 것을 그대로 씁니다 —
        # 끊기기 전에 실행된 도구 기록이 이 발언에 이어지고, 카드는 처음 시작한 시각부터 셉니다.
        draft = state.resume_drafts.pop(
            turns.draft_key(agent.key, kind, round_number, graph_node_id), None
        )
        # 유저가 이 전문가에게 지정한 스킬 (입력창의 `@전문가 @스킬`). 이 발언에서만 `allowed_skills`
        # 에 더하고, 첫 판 전에 불러 둡니다. 대화에 고정된 에이전트 설정은 그대로입니다.
        designated = list(state.skill_designations.get(agent.key) or [])
        agent = with_designated_skills(agent, designated)
        draft_state: Optional[Dict[str, Any]] = draft["state"] if draft else None
        msg_id = draft["id"] if draft else str(uuid.uuid4())
        # 벽시계 시작 시각. `created_at` 과 따로 둡니다 — 그쪽은 발언이 끝난 뒤에
        # 들어가는 정렬 키라서, 병렬 라운드에서는 실제 시각이 아닙니다.
        started_at = (draft.get("started_at") if draft else None) or utc_now()
        # 끊기기 전에 이미 화면에 흘렀던 글과, 이미 기록된 도구 호출.
        prior_text = "\n\n".join((draft_state or {}).get("segments") or [])
        prior_record_ids: List[str] = list((draft_state or {}).get("tool_record_ids") or [])
        if on_event:
            await on_event({
                "type": "message_stream_start",
                "message": {
                    "id": msg_id,
                    "sender_key": agent.key,
                    "sender_name": agent.name,
                    "sender_role": agent.role,
                    "content": f"{prior_text}\n\n" if prior_text else "",
                    "round_number": round_number,
                    "msg_type": msg_type,
                    "started_at": started_at,
                    "graph_node_id": graph_node_id,
                    "graph_port": graph_port,
                },
            })

        # 이 발언이 실행한 도구. 실행하는 **즉시** 발언 id 를 비운 채 기록하고
        # (`_record_tool_call`), 발언이 기록될 때 같은 커밋에서 이 발언에 잇습니다.
        # 예전에는 모아 두었다가 발언 행과 함께 넣었는데, 발언이 끝나기 전에 서버가
        # 내려가면 이미 실행된 도구(승인받은 파일 쓰기 포함)의 기록이 통째로 사라졌습니다
        # (ADR-024). 발언 id 를 비워 두는 것은, 아직 없는 발언을 가리키는 행을 만들지
        # 않기 위해서입니다 (SQLite 가 외래키를 검사하지 않아 통과할 뿐입니다).
        executed_tools: List[Dict[str, Any]] = []
        # 즉시 기록에 성공한 호출 (`id(call_log)` -> 기록 id). 실패한 것은 예전처럼 발언과 함께 넣습니다.
        recorded_tools: Dict[int, str] = {}

        async def _on_tool_call(call_log: Dict[str, Any]) -> None:
            executed_tools.append(call_log)
            record_id = await self._record_tool_call(db, state, agent, call_log, db_lock)
            if record_id:
                recorded_tools[id(call_log)] = record_id
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

        # 이 에이전트의 연결이 실패해 전역 기본 모델이 대신 답하게 됐다는 알림 (`llm.fallback`, ADR-030).
        # 기본 모델을 부르기 전에 오므로, 흘러나오는 카드에 "누가 답하고 있는지" 를 먼저 붙입니다.
        fallback_notices: List[Any] = []

        async def _on_fallback(notice: Any) -> None:
            fallback_notices.append(notice)
            if on_event:
                await on_event({
                    "type": "llm_fallback",
                    "message_id": msg_id,
                    "agent_key": agent.key,
                    "agent_name": agent.name,
                    "llm_meta": notice.as_meta(),
                })
        streamed: List[str] = [f"{prior_text}\n\n"] if prior_text else []

        # 도구 루프가 한 판을 마칠 때마다 이어 갈 수 있는 상태를 초안으로 남깁니다 (ADR-025).
        # 도구를 부르지 않는 발언은 한 번도 불리지 않으므로 초안이 생기지 않습니다.
        draft_saved = draft is not None

        async def _checkpoint(loop_state: Dict[str, Any]) -> None:
            nonlocal draft_saved
            payload = json.loads(json.dumps(
                {**loop_state, "tool_record_ids": prior_record_ids + list(recorded_tools.values())},
                ensure_ascii=False, default=str,
            ))
            async with (db_lock or nullcontext()):
                try:
                    if draft_saved:
                        await db.execute(
                            update(SpeechDraftModel)
                            .where(SpeechDraftModel.id == msg_id)
                            .values(state=payload, updated_at=utc_now())
                        )
                    else:
                        db.add(SpeechDraftModel(
                            id=msg_id, session_id=state.session_id, turn_id=state.turn_id,
                            agent_key=agent.key, kind=kind, round_number=round_number,
                            graph_node_id=graph_node_id, started_at=started_at, state=payload,
                        ))
                    await db.commit()
                    draft_saved = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 초안을 못 남겨도 발언은 계속됩니다
                    logger.warning(f"Could not save the draft of {agent.name}'s speech: {exc}")
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        logger.debug("Rollback after a failed draft save also failed", exc_info=True)

        # 조각은 모았다가 `STREAM_EVENT_INTERVAL` 마다 한 통으로 보냅니다.
        #
        # "다음 조각이 올 때 간격이 지났으면 보낸다" 로 하면 안 됩니다. 모델이 한 줄
        # 쓰고 30초 걸리는 도구를 부르면, 그 한 줄은 다음 조각이 올 때까지 30초 동안
        # 화면에 나오지 않습니다. 그래서 첫 조각이 쌓이는 순간 짧은 예약을 걸어, 뒤에
        # 무엇이 오든 오지 않든 그 간격 안에 내보냅니다.
        pending_chunks: List[str] = []
        flush_task: Optional[asyncio.Task] = None
        flush_lock = asyncio.Lock()

        async def _flush_chunks() -> None:
            # 순서를 지키려고 한 번에 하나만 내보냅니다. 모은 것을 꺼내는 데에는
            # await 가 없어, 꺼낸 뒤에 들어온 조각은 다음 통에 실립니다.
            async with flush_lock:
                if not pending_chunks or not on_event:
                    return
                delta = "".join(pending_chunks)
                pending_chunks.clear()
                await on_event({
                    "type": "message_stream_chunk",
                    "message_id": msg_id,
                    "delta": delta,
                })

        async def _flush_later() -> None:
            await asyncio.sleep(STREAM_EVENT_INTERVAL)
            await _flush_chunks()

        def _cancel_flush() -> None:
            # 기다리지 않고 취소만 합니다. 예약은 sleep 에서 멈춰 있거나 아직 시작하지
            # 않았으므로, 취소하면 내보내기 전에 끝납니다.
            if flush_task is not None and not flush_task.done():
                flush_task.cancel()

        async def _drain_chunks() -> None:
            """남은 조각을 지금 내보냅니다. 발언을 확정하기 전에 반드시 부릅니다.

            예약을 남겨 두면 `message_added` 로 발언이 확정된 **뒤에** 조각이 한 통 더
            도착하고, 러너의 스냅샷은 그것을 확정본 끝에 한 번 더 붙입니다.
            """
            _cancel_flush()
            await _flush_chunks()

        async def _on_chunk(delta: str) -> None:
            nonlocal flush_task
            streamed.append(delta)
            if not on_event:
                return
            pending_chunks.append(delta)
            if flush_task is None or flush_task.done():
                flush_task = asyncio.create_task(_flush_later())

        try:
            with llm_fallback.listening(_on_fallback):
                content, tool_logs = await self.llm_caller.call_agent(
                    agent, prompt_messages, custom_instructions,
                    on_tool_call=_on_tool_call, on_chunk=_on_chunk,
                    session_id=state.session_id,
                    budget_arbiter=self._make_budget_arbiter(control, on_event),
                    context_arbiter=self._make_context_arbiter(state, agent, control, on_event),
                    on_context_trim=_on_context_trim,
                    mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
                    ledger=state.decision_ledger,
                    checkpoint=_checkpoint if state.turn_id else None,
                    resume_state=draft_state,
                    preload_skills=designated,
                )
            # 다이어그램 교정 같은 후처리는 시간이 걸립니다. 그동안 카드가 마지막 조각
            # 직전에서 멈춰 보이지 않게, 흘러온 글을 먼저 다 내보냅니다.
            await _drain_chunks()
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
            _cancel_flush()
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

        # 실패로 끝났어도 예약된 조각이 확정본 뒤에 도착하면 안 됩니다.
        await _drain_chunks()

        # 기본 모델이 대신 답한 발언이면 그 사실을 기록에 남깁니다. 기본 모델까지 실패한 발언에는
        # 남기지 않습니다 — 답한 모델이 없고, 두 이유는 실패 안내 본문에 적혀 있습니다.
        llm_meta: Optional[Dict[str, Any]] = None
        if fallback_notices and final_type != "error":
            llm_meta = fallback_notices[-1].as_meta()
            if agent.key not in state.fallback_agent_keys:
                state.fallback_agent_keys.append(agent.key)

        # 응답이 끝난 시각. 실패로 끝난 발언도 여기서 잽니다 — 언제 포기했는지는
        # 그 자체로 쓸모 있는 기록입니다 (엔드포인트가 몇 초 만에 끊었는지, 한도까지
        # 버텼는지). 기록 락을 기다린 시간이 섞이지 않도록 락 **밖에서** 잽니다.
        finished_at = utc_now()

        def _message_rows() -> List[Any]:
            rows: List[Any] = [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key=agent.key,
                sender_name=agent.name,
                sender_role=agent.role,
                content=content,
                round_number=round_number,
                msg_type=final_type,
                started_at=started_at,
                finished_at=finished_at,
                turn_started_at=turn_started_at,
                graph_node_id=graph_node_id,
                graph_port=graph_port,
                turn_id=state.turn_id,
                turn_meta=turns.meta(kind),
                llm_meta=llm_meta,
                **({"created_at": created_at} if created_at is not None else {}),
            )]
            for call_log in executed_tools:
                if id(call_log) not in recorded_tools:
                    rows.append(self._tool_row(state, agent, call_log, message_id=msg_id))
            return rows

        linked = prior_record_ids + list(recorded_tools.values())

        async def _link_tools() -> None:
            if linked:
                await db.execute(
                    update(ToolCallRecordModel)
                    .where(ToolCallRecordModel.id.in_(linked))
                    .values(message_id=msg_id)
                )
            # 발언이 기록되면 초안은 할 일을 마쳤습니다. 같은 커밋에서 지웁니다.
            if draft_saved:
                await db.execute(delete(SpeechDraftModel).where(SpeechDraftModel.id == msg_id))

        # 최종 합성이면 파일 이름과 알림에서 그렇다고 밝힙니다. 사람이 먼저 찾는 것이
        # 그 보고서입니다. `turn_started_at` 은 합성 발언에만 주어집니다.
        is_synthesis = turn_started_at is not None
        async with (db_lock or nullcontext()):
            await self._persist(
                db, _message_rows,
                what=("the final synthesis report" if is_synthesis else f"{agent.name}'s message"),
                label=("최종 합성 보고서" if is_synthesis else f"{agent.name} 의 발언"),
                kind=("synthesis" if is_synthesis else f"message-{agent.key}"),
                session_id=state.session_id,
                fallback_title=f"{agent.name} ({agent.role}) — Round {round_number}",
                fallback_body=content,
                on_event=on_event,
                before_commit=_link_tools,
            )

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
            started_at=started_at,
            finished_at=finished_at,
            turn_started_at=turn_started_at,
            graph_node_id=graph_node_id,
            graph_port=graph_port,
            turn_meta=turns.meta(kind),
            llm_meta=llm_meta,
        )
        state.messages.append(message)

        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    # ------------------------------------------------------------------ 기록

    @staticmethod
    def _tool_row(
        state: DebateState,
        agent: Agent,
        call_log: Dict[str, Any],
        *,
        message_id: Optional[str],
        record_id: Optional[str] = None,
    ) -> ToolCallRecordModel:
        security = call_log.get("security") or {}
        return ToolCallRecordModel(
            id=record_id or str(uuid.uuid4()),
            session_id=state.session_id,
            message_id=message_id,
            turn_id=state.turn_id,
            agent_key=agent.key,
            tool_name=call_log.get("tool_name", ""),
            arguments=call_log.get("arguments", {}),
            output=call_log.get("output", ""),
            status=call_log.get("status", "success"),
            # 도구 보안 판정 (`app/orchestration/tool_gate.py`). 판정 없이 실행된
            # 호출(게이트 밖의 발언)은 비어 있습니다.
            decision=str(security.get("decision") or ""),
            risk=str(security.get("risk") or ""),
            rule=str(security.get("rule") or ""),
            approver=str(security.get("approver") or ""),
        )

    async def _record_tool_call(
        self,
        db,
        state: DebateState,
        agent: Agent,
        call_log: Dict[str, Any],
        db_lock: Optional[asyncio.Lock],
    ) -> Optional[str]:
        """도구 호출 하나를 실행 즉시 기록합니다. 기록 id, 못 했으면 None.

        발언 id 는 비워 둡니다 — 발언이 기록될 때 같은 커밋에서 잇습니다 (`_speak`). 여기서
        실패해도 다시 시도하지 않습니다. 도구 루프를 붙잡지 않고, 그 호출은 예전처럼 발언과
        함께 기록됩니다.
        """
        record_id = str(uuid.uuid4())
        async with (db_lock or nullcontext()):
            db.add(self._tool_row(state, agent, call_log, message_id=None, record_id=record_id))
            try:
                await db.commit()
                return record_id
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 즉시 기록을 못 해도 발언과 함께 기록됩니다
                logger.warning(
                    f"Could not record {agent.name}'s tool call right away "
                    f"({call_log.get('tool_name')}); it will be saved with the message: "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug("Rollback after a failed tool-call commit also failed", exc_info=True)
                return None

    async def _persist(
        self,
        db,
        build: Callable[[], List[Any]],
        *,
        what: str,
        label: str,
        kind: str,
        session_id: str,
        fallback_title: str,
        fallback_body: str,
        on_event: Optional[EventCallback],
        before_commit: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> bool:
        """행을 기록합니다. 실패하면 간격을 두고 다시 시도하고, 끝내 실패하면 파일로 남깁니다.

        `before_commit` 은 행을 넣은 뒤 같은 트랜잭션에서 돌릴 문장입니다 (이미 기록된 도구 호출을
        이 발언에 잇는 UPDATE). 다시 시도할 때마다 다시 돕니다.

        `build` 는 **시도할 때마다 새 행을 만들어** 돌려줘야 합니다. 실패한 커밋을 롤백하면
        그 트랜잭션에 넣었던 객체는 세션에서 떨어져 나가, 같은 객체를 다시 넣는 것보다
        새로 만드는 편이 확실합니다 (id 는 호출하는 쪽이 고정해 두므로 같은 행입니다).

        롤백을 빠뜨리면 세션이 깨진 채로 남아 이 대화의 다음 커밋이 전부 실패합니다.

        예외를 올리지 않습니다. 기록 실패로 토론이 멈추면, 이미 나온 발언 뒤로 이어질
        발언까지 잃습니다. 대신 잃지 않게 파일로 남기고 사람에게 알립니다. 취소만은
        그대로 올립니다.
        """
        attempts = len(PERSIST_RETRY_DELAYS) + 1
        last: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            db.add_all(build())
            try:
                if before_commit is not None:
                    await before_commit()
                await db.commit()
                if attempt > 1:
                    logger.info(f"Persisted {what} on attempt {attempt}/{attempts}")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 어떤 기록 실패로도 토론을 멈추지 않습니다
                last = exc
                logger.warning(
                    f"Could not persist {what} (attempt {attempt}/{attempts}): "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug("Rollback after a failed commit also failed", exc_info=True)
                if attempt < attempts:
                    await asyncio.sleep(PERSIST_RETRY_DELAYS[attempt - 1])

        assert last is not None
        saved_to = save_unpersisted(
            kind=kind, session_id=session_id, title=fallback_title,
            body=fallback_body, error=last,
        )
        logger.error(
            f"Gave up persisting {what} after {attempts} attempts "
            f"({type(last).__name__}: {last}); "
            + (f"saved to {saved_to}" if saved_to else "could not save it to a file either"),
            exc_info=last,
        )
        if on_event:
            first_line = str(last).splitlines()[0] if str(last) else ""
            try:
                await on_event({
                    "type": "persist_failed",
                    "what": what,
                    "label": label,
                    "error": f"{type(last).__name__}: {first_line}",
                    "saved_to": str(saved_to) if saved_to else None,
                })
            except Exception:  # noqa: BLE001 - 알리다 실패해도 토론은 계속됩니다
                logger.debug("persist_failed notification failed", exc_info=True)
        return False

    # ------------------------------------------------------------------ 유저 발언

    async def _record_user_message(
        self,
        *,
        db,
        state: DebateState,
        content: str,
        round_number: int,
        on_event: Optional[EventCallback],
        kind: str = turns.KIND_INTERJECTION,
        extra_rows: Optional[Callable[[str, datetime], List[Any]]] = None,
    ) -> DebateMessage:
        """유저 발언을 기록에 남기고 화면에 흘립니다.

        턴을 여는 최초 요청과 토론 도중의 개입이 같은 자리에 같은 모양으로
        들어가야, 다음 발언자의 맥락(`_build_context_for_agent`)과 합성 전사가
        둘을 구분 없이 읽습니다. 기록의 자리(`kind`)만 다릅니다.

        `extra_rows(발언 id, 기록 시각)` 는 이 발언과 **같은 커밋**에 넣을 행입니다 — 턴을 여는
        요청은 턴 기록과 함께 들어가야, 요청만 있고 턴이 없는(또는 그 반대) 기록이 생기지 않습니다.
        """
        msg_id = str(uuid.uuid4())
        # 사람 발언에는 걸리는 시간이 없습니다. 시작과 끝을 같은 시각으로 적어,
        # 읽는 쪽이 "시각이 없는 옛 기록" 과 구분할 수 있게 합니다.
        now = utc_now()
        await self._persist(
            db,
            lambda: (extra_rows(msg_id, now) if extra_rows else []) + [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key="user",
                sender_name="User",
                # 역할은 비웁니다 — 유저는 한 명이고 요청하는 쪽도 늘 그 유저입니다.
                sender_role="",
                content=content,
                round_number=round_number,
                msg_type="user",
                started_at=now,
                finished_at=now,
                turn_id=state.turn_id,
                turn_meta=turns.meta(kind),
            )],
            what="the user's message",
            label="유저 발언",
            kind="message-user",
            session_id=state.session_id,
            fallback_title=f"User — Round {round_number}",
            fallback_body=content,
            on_event=on_event,
        )

        message = DebateMessage(
            id=msg_id,
            sender_key="user",
            sender_name="User",
            sender_role="",
            content=content,
            round_number=round_number,
            msg_type="user",
            started_at=now,
            finished_at=now,
            turn_meta=turns.meta(kind),
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
        await self._add_skill_designations(db, state, control.drain_skill_designations())
        for note in notes:
            await self._record_user_message(
                db=db,
                state=state,
                content=f"{memory.INTERJECTION_PREFIX}\n{note}",
                round_number=round_number,
                on_event=on_event,
                kind=turns.KIND_INTERJECTION,
            )
        state.interjection_count += len(notes)
        if notes:
            logger.info(
                f"Applied {len(notes)} user interjection(s) to session {state.session_id} "
                f"at round {round_number}"
            )
        return len(notes)

    async def _add_skill_designations(
        self, db, state: DebateState, designations: Dict[str, List[str]],
    ) -> None:
        """개입 메모와 함께 온 스킬 지정을 남은 발언에 겁니다. 끊겼다 이어 가도 남도록 턴 기록에도 적습니다."""
        added = False
        for key, names in clean_skill_designations(designations, state.active_agent_keys).items():
            bucket = state.skill_designations.setdefault(key, [])
            for name in names:
                if name not in bucket:
                    bucket.append(name)
                    added = True
        if not added or not state.turn_id:
            return
        try:
            turn = await db.get(TurnModel, state.turn_id)
            config = dict(turn.config or {}) if turn is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 적지 못해도 이번 턴에는 걸립니다
            logger.warning(f"Could not read turn {state.turn_id} to save skill designations: {exc}")
            return
        if config is not None:
            config["skill_designations"] = {k: list(v) for k, v in state.skill_designations.items()}
            await self._set_turn(db, state, config=config)

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
        skill_designations: Optional[Dict[str, List[str]]] = None,
    ) -> DebateState:
        """Executes a full multi-agent collaborative debate and synthesis turn.

        `skill_designations` 는 입력창에서 `@전문가 @스킬` 로 지정한 것입니다 (전문가 키 → 스킬).
        이 턴에 한해 그 전문가의 `allowed_skills` 밖이어도 주고, 발언마다 미리 불러 둡니다.

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

        턴은 기록으로 남습니다 (`TurnModel`, ADR-024). 예외로 멈추면 "실패" 로 적고, 취소되면
        (서버 종료·긴급 종료) 그대로 둡니다 — 서버 종료였다면 다음 기동이 "끊김" 으로 적고,
        긴급 종료였다면 부른 쪽이 턴째로 지웁니다.
        """
        turn_id = str(uuid.uuid4())
        # 이 턴의 LLM 호출이 어느 대화의 것인지 (서버 전체 동시 요청 상한의 대기열 표시).
        # 병렬 발언 태스크는 만들어질 때 이 값을 물려받습니다.
        holder_token = LLM_HOLDER.set(session_id)
        pool = get_runtime_pool()
        try:
            workspace = await self._session_workspace(session_id)
            await pool.acquire(workspace, holder=session_id)
            try:
                return await self._run_turn(
                    session_id, user_prompt, workspace, turn_id, on_event, control,
                    skill_designations=skill_designations,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - 적고 그대로 올립니다
                await self._mark_turn_failed(turn_id, exc)
                raise
            finally:
                self._tool_gates.pop(session_id, None)
                await pool.release(session_id, workspace)
        finally:
            LLM_HOLDER.reset(holder_token)

    async def resume_turn(
        self,
        session_id: str,
        turn_id: str,
        mode: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
    ) -> DebateState:
        """끊긴 턴을 다시 세워 마무리하거나(`finish`) 이어 갑니다(`continue`) (ADR-024).

        * `finish` — 기록된 발언만으로 합성합니다. 정지와 같은 의미라 합의로 적지 않고, 합성
          지시와 보고서에 서버 중단으로 덜 논의되었다고 적습니다.
        * `continue` — 기록에서 어디까지 돌았는지 다시 계산해 남은 발언부터 이어 갑니다.

        참여자·전략·라운드 수·지침·작업 공간은 **그 턴이 시작될 때의 구성**(`TurnModel.config`)을
        따릅니다. 끊긴 사이에 로스터를 바꿨더라도 그 턴은 시작할 때의 모습으로 마칩니다.
        """
        if mode not in ("finish", "continue"):
            raise ValueError(f"알 수 없는 재개 방식입니다: {mode}")
        async with self.session_factory() as db:
            turn = await db.get(TurnModel, turn_id)
            if turn is None or turn.session_id != session_id:
                raise ValueError("이어 갈 턴을 찾지 못했습니다.")
            if turn.status not in TURN_UNFINISHED:
                raise ValueError(f"끊긴 턴이 아닙니다 (상태: {turn.status}).")
            workspace = resolve_workspace_dir((turn.config or {}).get("workspace_dir") or None)

        # 새 턴과 같이, 이어 가는 턴의 LLM 호출도 이 대화의 것으로 대기열에 보입니다.
        holder_token = LLM_HOLDER.set(session_id)
        pool = get_runtime_pool()
        try:
            await pool.acquire(workspace, holder=session_id)
            try:
                return await self._resume_turn(session_id, turn_id, mode, workspace, on_event, control)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - 적고 그대로 올립니다
                await self._mark_turn_failed(turn_id, exc)
                raise
            finally:
                self._tool_gates.pop(session_id, None)
                await pool.release(session_id, workspace)
        finally:
            LLM_HOLDER.reset(holder_token)

    async def _run_turn(
        self,
        session_id: str,
        user_prompt: str,
        workspace: Path,
        turn_id: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
        *,
        skill_designations: Optional[Dict[str, List[str]]] = None,
    ) -> DebateState:
        """`run_turn` 의 본문. 런타임은 이미 빌린 상태로 들어옵니다."""
        async with self.session_factory() as db:
            # 1. Load session config from DB
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            res = await db.execute(stmt)
            session_model = res.scalar_one_or_none()
            if not session_model:
                raise ValueError(f"Session with ID '{session_id}' not found.")

            # 그래프를 검사하고 참여자를 세웁니다. 돌 수 없는 턴이면 사용자 발언을 기록하기
            # **전에** 여기서 멈춥니다 — 답 없는 요청이 다음 턴의 맥락에 끼어들지 않게.
            setup = await self._setup_turn(
                db, session_model, control=control, on_event=on_event,
                skill_designations=skill_designations,
            )

            # 2. Initialize Debate State
            state = self._new_state(session_id, user_prompt, workspace, setup)
            state.turn_id = turn_id

            # 이전 턴의 대화 기록을 DB에서 로드하여 대화 맥락을 보존합니다.
            state.messages.extend(await self._load_history(db, session_id))
            # 여기서부터가 이번 턴입니다. 산출물은 이 뒤의 발언에서만 모읍니다.
            state.turn_message_start = len(state.messages)
            self._load_memory(state, session_model)

            # 끊긴 채로 둔 턴이 있으면 버린 것으로 적습니다. 기록은 남기되 더는 묻지 않습니다.
            try:
                if await turns.abandon_unfinished(db, session_id):
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 적지 못해도 새 턴은 돕니다
                logger.warning(f"Could not mark earlier unfinished turns as abandoned: {exc}")
                await db.rollback()

            # 3. Record User Message in DB
            #
            # 이 요청이 기록된 시각이 이 턴의 시작입니다. 보고서와 저장 문서의 "총 경과"
            # 가 여기서 셉니다 — 기록에 남는 시각이라, 문서를 읽는 사람이 "요청 시각 →
            # 합성 종료" 로 같은 값을 다시 얻을 수 있습니다. 턴 기록은 이 요청과 같은
            # 커밋에 들어갑니다.
            turn_config = setup.config(workspace)
            opening = await self._record_user_message(
                db=db,
                state=state,
                content=user_prompt,
                round_number=0,
                on_event=on_event,
                kind=turns.KIND_OPENING,
                extra_rows=lambda msg_id, at: [TurnModel(
                    id=turn_id,
                    session_id=session_id,
                    status=TURN_RUNNING,
                    phase="planning",
                    opening_message_id=msg_id,
                    config=turn_config,
                    started_at=at,
                )],
            )
            turn_started_at = opening.started_at
            if on_event:
                await on_event({"type": "turn_opened", "turn_id": turn_id, "resumed": False})

            # 4. Phase 1: Master Orchestrator Goal Analysis & Planning
            plan_message = await self._plan(db, state, setup, on_event=on_event, control=control)

            # 4-b. 계획 승인 (ADR-028). 사람이 계획을 확인하기 전에는 아무도 발언하지 않습니다.
            # 수정을 요청하면 계획이 다시 쓰이므로, 토론은 여기서 돌려받은 계획에서 시작합니다.
            plan_message = await self._approve_plan(
                db, state, setup, plan_message, on_event=on_event, control=control,
            )

            # 5. Phase 2: Multi-Round Specialist Debate Loop
            await self._set_turn(db, state, phase="debating")
            with consultation_scope(
                setup.active_agents,
                pool=setup.consult_pool,
                llm_caller=self.llm_caller,
                custom_instructions=setup.custom_instructions,
                session_id=session_id,
                mcp=self._mcp_for(state),
                tool_gate=self._gate_for(state),
                on_event=on_event,
            ):
                stopped_early = await self._debate(
                    db, state, setup, control=control, on_event=on_event,
                    start_value=[
                        plan_message.id
                        if plan_message is not None and plan_message.msg_type != "error"
                        else opening.id
                    ],
                )

            # 6. Phase 3: Final Consensus & Artifact Synthesis
            await self._conclude(
                db, state, setup, stopped_early=stopped_early, turn_started_at=turn_started_at,
                control=control, on_event=on_event,
            )
            return state

    async def _resume_turn(
        self,
        session_id: str,
        turn_id: str,
        mode: str,
        workspace: Path,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
    ) -> DebateState:
        """`resume_turn` 의 본문. 런타임은 이미 빌린 상태로 들어옵니다."""
        async with self.session_factory() as db:
            session_model = await db.get(SessionModel, session_id)
            turn = await db.get(TurnModel, turn_id)
            if session_model is None or turn is None:
                raise ValueError("이어 갈 턴을 찾지 못했습니다.")
            config = dict(turn.config or {})

            # 그래프는 그 턴이 시작될 때 고정해 둔 것입니다. 파일은 그 사이 바뀌었을 수 있습니다.
            graph_spec: Optional[GraphSpec] = None
            if get_strategy(resolve_strategy_name(config.get("strategy"))).runs_graph:
                if not session_model.graph_snapshot:
                    raise GraphTurnError("이 턴이 돌던 그래프의 기록이 없어 다시 세울 수 없습니다.")
                graph_spec = parse_graph(session_model.graph_snapshot)
            setup = await self._setup_turn(
                db, session_model, control=control, on_event=on_event,
                config=config, graph_spec=graph_spec,
            )

            history = await self._load_history(db, session_id, tools_for_turn=turn_id)
            start = next((i for i, m in enumerate(history) if m.id == turn.opening_message_id), None)
            if start is None:
                raise ValueError("이 턴을 연 요청이 기록에 없어 다시 세울 수 없습니다.")
            opening = history[start]

            state = self._new_state(session_id, opening.content, workspace, setup)
            state.turn_id = turn_id
            state.messages = history
            state.turn_message_start = start
            self._load_memory(state, session_model)
            self._restore_turn_state(state)

            # 끊긴 채로 있던 시간을 셉니다. 보고서의 총 경과에 "그중 서버 중단" 으로 함께 적습니다.
            orphans = (await db.execute(
                select(ToolCallRecordModel)
                .where(ToolCallRecordModel.turn_id == turn_id, ToolCallRecordModel.message_id.is_(None))
                .order_by(ToolCallRecordModel.created_at)
            )).scalars().all()
            stopped_at = turns.last_activity(turn, state.messages[start:], [o.created_at for o in orphans])
            paused = int((utc_now() - stopped_at).total_seconds()) if stopped_at is not None else 0
            turn.status = TURN_RUNNING
            turn.error = ""
            turn.paused_seconds = int(turn.paused_seconds or 0) + max(0, paused)
            turn.resumed_count = int(turn.resumed_count or 0) + 1
            await db.commit()
            state.paused_seconds = turn.paused_seconds
            state.resumed_count = turn.resumed_count
            if on_event:
                # 끊기기 전의 기록도 싣습니다. 러너의 스냅샷이 턴 전체를 들고 있어야, 이어 가는 중에
                # 새로고침한 화면이 그래프 진행 표시를 처음부터 다시 그립니다.
                await on_event({
                    "type": "turn_opened", "turn_id": turn_id, "resumed": True, "mode": mode,
                    "messages": [m.model_dump() for m in state.messages[start:]],
                })

            # 도구 단위로 이어 갈 발언의 초안 (ADR-025). 이어 가기에서만 씁니다 — 결론 내기는 발언을
            # 더 하지 않으므로, 초안이 들고 있던 도구 기록도 아래의 "끊김" 안내로 돌립니다.
            drafts = (await db.execute(
                select(SpeechDraftModel).where(SpeechDraftModel.turn_id == turn_id)
            )).scalars().all()
            usable = [
                d for d in drafts
                if mode == "continue" and (d.state or {}).get("version") == SPEECH_STATE_VERSION
            ]
            state.resume_drafts = {
                turns.draft_key(d.agent_key, d.kind, d.round_number, d.graph_node_id): {
                    "id": d.id, "kind": d.kind, "started_at": d.started_at, "state": d.state,
                }
                for d in usable
            }
            owned = {rid for d in usable for rid in (d.state or {}).get("tool_record_ids") or []}
            stale = [d.id for d in drafts if d not in usable]
            if stale:
                await db.execute(delete(SpeechDraftModel).where(SpeechDraftModel.id.in_(stale)))
                await db.commit()

            # 끝나지 못한 발언이 실행한 도구는 "끊겼다" 는 안내에 잇습니다. 그 발언의 글은 기록되지
            # 않았으므로 남아 있지 않다는 것도 함께 적습니다. 이어 가면 그 에이전트가 다시 말할 때
            # 이 기록을 관찰로 건넵니다. 초안이 있는 발언은 초안에서 이어 가므로 빼고, 그 도구 기록은
            # 이어 간 발언에 그대로 이어집니다.
            redo = await self._note_interrupted_speeches(
                db, state, setup, [o for o in orphans if o.id not in owned], on_event=on_event,
            )

            if mode == "finish":
                # 정지와 같은 의미입니다. 기록된 발언만으로 합성하고, 합의로 적지 않습니다.
                state.interrupted = True
                await self._set_turn(db, state, phase="debating")
                await self._conclude(
                    db, state, setup, stopped_early=True, turn_started_at=opening.started_at,
                    control=control, on_event=on_event,
                )
                return state

            if not turns.can_continue(setup.strategy_name, turn.phase):
                raise ValueError("이 전략의 턴은 아직 이어서 진행할 수 없습니다. 지금까지로 결론을 내거나 버려 주십시오.")

            if turn.phase == "synthesizing":
                # 토론은 끝났고 합성만 남았습니다. 정지로 덜 논의된 턴이었는지는 턴 기록이 압니다.
                stopped_early = bool(turn.stopped_early)
            else:
                # 계획이 기록되기 전에 끊겼으면 계획부터 다시 합니다.
                plan_expected = setup.graph_spec is None or setup.graph_spec.start.plan
                if plan_expected and not turns.count_kind(state.messages[start:], turns.KIND_PLAN):
                    await self._plan(db, state, setup, on_event=on_event, control=control)
                # 승인을 받기 전에 끊긴 턴은 (답이 없어 멈춰 둔 턴도) 승인부터 다시 받습니다
                # (ADR-028). 토론에 들어간 뒤에 끊긴 턴은 이미 승인됐거나 승인 없이 시작한 것입니다.
                if (
                    turn.phase in ("planning", "approval")
                    and state.plan_index is not None and state.approval_index is None
                ):
                    await self._approve_plan(
                        db, state, setup, state.messages[state.plan_index],
                        on_event=on_event, control=control,
                    )
                await self._set_turn(db, state, phase="debating")
                # 끊기기 전의 발언까지 장부에 접어 두고 이어 갑니다 (라운드 중간에 끊겼으면 장부가
                # 그 라운드를 모릅니다). 새 발언이 없으면 부르지 않습니다.
                await self._update_ledger(
                    state=state, orchestrator=setup.orchestrator, on_event=on_event, reason="재개",
                )
                with consultation_scope(
                    setup.active_agents,
                    pool=setup.consult_pool,
                    llm_caller=self.llm_caller,
                    custom_instructions=setup.custom_instructions,
                    session_id=session_id,
                    mcp=self._mcp_for(state),
                    tool_gate=self._gate_for(state),
                    on_event=on_event,
                ):
                    stopped_early = await self._debate(
                        db, state, setup, control=control, on_event=on_event,
                        start_value=[
                            state.messages[state.plan_index].id
                            if state.plan_index is not None else opening.id
                        ],
                        resume=True, redo=redo,
                    )
                # 정지 등으로 다시 불리지 않은 발언의 초안은, 그 도구 기록을 "끊김" 안내로 돌립니다.
                # 합성의 초안은 바로 아래 합성이 씁니다.
                await self._settle_drafts(db, state, setup, on_event=on_event, keep=(turns.KIND_SYNTHESIS,))
            await self._conclude(
                db, state, setup, stopped_early=stopped_early, turn_started_at=opening.started_at,
                control=control, on_event=on_event,
            )
            return state

    # ------------------------------------------------------------ 턴의 단계

    async def _setup_turn(
        self,
        db,
        session_model: SessionModel,
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        config: Optional[Dict[str, Any]] = None,
        graph_spec: Optional[GraphSpec] = None,
        skill_designations: Optional[Dict[str, List[str]]] = None,
    ) -> "TurnSetup":
        """턴의 구성과 참여자를 세웁니다. 도구 보안 문지기도 여기서 섭니다.

        `config` 가 없으면 지금 대화 설정으로(새 턴), 있으면 그 구성으로(끊긴 턴) 세웁니다.
        스킬 지정도 같습니다 — 새 턴은 `skill_designations` 를, 끊긴 턴은 고정해 둔 것을 씁니다.
        도구 보안(모드·허용·거부)은 어느 쪽이든 **지금** 대화 설정을 따릅니다 — 사람이 끊긴
        사이에 조인 보안을 이어 가는 턴이 우회하면 안 됩니다.
        """
        session_id = session_model.id
        if config is None:
            # 옛 이름으로 저장된 대화도 지금 쓰는 전략으로 옮겨 돌립니다.
            strategy_name = resolve_strategy_name(session_model.strategy)
            max_rounds = session_model.max_rounds
            # 병렬 지시 전략에서만 읽힙니다. 0 이나 음수는 "동시에 아무도 못 돈다"
            # 는 뜻이 되어 라운드가 통째로 비므로 최소 1 로 올립니다.
            parallel_limit = max(1, int(session_model.parallel_limit or 3))
            active_keys = session_model.active_agents or ["orchestrator", "architect", "coder", "critic"]
            custom_instructions = session_model.custom_instructions or ""
            designations = skill_designations
        else:
            strategy_name = resolve_strategy_name(config.get("strategy"))
            max_rounds = int(config.get("max_rounds") or session_model.max_rounds or 3)
            parallel_limit = max(1, int(config.get("parallel_limit") or 3))
            active_keys = list(config.get("active_agents") or session_model.active_agents or [])
            custom_instructions = str(config.get("custom_instructions") or "")
            designations = config.get("skill_designations")

        # 도구 보안: 이 대화의 모드와 "이 대화에서 허용·거부" 목록으로 문지기를 세웁니다.
        self._tool_gates[session_id] = ToolGate(
            session_id=session_id,
            mode=session_model.tool_mode or "",
            grants=list(session_model.tool_grants or []),
            denials=list(session_model.tool_denials or []),
            control=control,
            on_event=on_event,
            save_rules=self._rule_saver(session_id),
        )

        # Ensure orchestrator is in active keys
        if "orchestrator" not in active_keys:
            active_keys = ["orchestrator"] + list(active_keys)

        # 그래프 토론은 그래프에 놓인 에이전트가 곧 참여자입니다 (로스터 체크박스가 아니라).
        if get_strategy(strategy_name).runs_graph:
            if graph_spec is None:
                graph_spec = await self._graph_for_turn(db, session_model, max_rounds)
            active_keys = ["orchestrator"] + graph_spec.agent_keys()
        else:
            graph_spec = None

        # 세션 페르소나를 적용합니다. 첫 턴이면 이 시점에 기록되고 잠깁니다.
        active_agents = await prepare_agents_for_turn(
            db, session_model, self.agent_pool, active_keys
        )
        if graph_spec is not None:
            present = {a.key for a in active_agents}
            missing = [k for k in graph_spec.agent_keys() if k not in present]
            if missing:
                raise GraphTurnError(
                    f"그래프의 에이전트를 준비하지 못했습니다: {', '.join(missing)}"
                )
        orchestrator_agent = next(
            (a for a in active_agents if a.key == "orchestrator"),
            self.agent_pool.get_orchestrator(),
        )
        present_keys = {a.key for a in active_agents}
        consult_pool = list(active_agents) + [
            a for a in await frozen_agents(db, session_id, self.agent_pool) if a.key not in present_keys
        ]
        # 장부와 요약은 모든 참여자의 프롬프트에 실립니다. 상한을 가장 작은 창에 맞춥니다.
        memory_budget = min(
            (context_budget(a) for a in active_agents), default=context_budget(orchestrator_agent)
        )
        return TurnSetup(
            strategy_name=strategy_name,
            max_rounds=max_rounds,
            parallel_limit=parallel_limit,
            active_keys=list(active_keys),
            custom_instructions=custom_instructions,
            active_agents=active_agents,
            orchestrator=orchestrator_agent,
            graph_spec=graph_spec,
            memory_budget=memory_budget,
            skill_designations=clean_skill_designations(designations, active_keys),
            consult_pool=consult_pool,
        )

    @staticmethod
    def _new_state(session_id: str, user_prompt: str, workspace: Path, setup: "TurnSetup") -> DebateState:
        return DebateState(
            session_id=session_id,
            user_prompt=user_prompt,
            workspace_dir=str(workspace),
            strategy=setup.strategy_name,
            max_rounds=setup.max_rounds,
            current_round=0,
            custom_instructions=setup.custom_instructions,
            active_agent_keys=setup.active_keys,
            status="planning",
            memory_budget=setup.memory_budget,
            skill_designations={k: list(v) for k, v in setup.skill_designations.items()},
        )

    async def _load_history(
        self, db, session_id: str, *, tools_for_turn: Optional[str] = None,
    ) -> List[DebateMessage]:
        """이 대화의 기록 전부를 기록 순서대로.

        `tools_for_turn` 턴의 발언에는 도구 기록도 붙입니다 — 끊긴 턴을 다시 세울 때 그 턴의
        발언은 도는 중이던 상태와 같아야 합니다 (코드 블록을 파일 참조로 바꾸는 규칙이 쓴
        파일 목록을 읽습니다). 지난 턴의 발언에는 예전처럼 붙이지 않습니다.
        """
        rows = (await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.created_at)
        )).scalars().all()
        history: List[DebateMessage] = []
        for pm in rows:
            tool_calls: List[Dict[str, Any]] = []
            if tools_for_turn is not None and pm.turn_id == tools_for_turn:
                tool_calls = [
                    {
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                        "output": tc.output,
                        "status": tc.status,
                    }
                    for tc in sorted(pm.tool_calls or [], key=lambda tc: tc.created_at)
                ]
            history.append(DebateMessage(
                id=pm.id,
                sender_key=pm.sender_key,
                sender_name=pm.sender_name,
                sender_role=pm.sender_role,
                content=pm.content,
                round_number=pm.round_number,
                msg_type=pm.msg_type,
                tool_calls=tool_calls,
                started_at=pm.started_at,
                finished_at=pm.finished_at,
                turn_started_at=pm.turn_started_at,
                graph_node_id=pm.graph_node_id,
                graph_port=pm.graph_port,
                turn_meta=pm.turn_meta if isinstance(pm.turn_meta, dict) else None,
                llm_meta=pm.llm_meta if isinstance(pm.llm_meta, dict) else None,
            ))
        return history

    @staticmethod
    def _restore_turn_state(state: DebateState) -> None:
        """끊긴 턴의 기록에서, 도는 동안 상태가 들고 있던 값을 다시 셉니다."""
        turn_messages = state.messages[state.turn_message_start:]
        state.plan_index = turns.plan_position(state.messages, state.turn_message_start)
        # 승인은 그것이 가리키는 계획에만 유효합니다 (ADR-028). 승인 뒤에 계획이 바뀐 기록이면
        # 승인받지 않은 것으로 봅니다.
        state.plan_tasks, state.approval_index = [], None
        approval = plan_gate.approval_record(state.messages, state.turn_message_start)
        if approval is not None and state.plan_index is not None:
            index, meta = approval
            if meta.get("plan_id") == state.messages[state.plan_index].id:
                state.plan_tasks = plan_gate.clean_tasks(meta.get("tasks"))
                state.approval_index = index
        state.failed_agent_keys = turns.failed_agents(turn_messages)
        state.fallback_agent_keys = turns.fallback_agents(turn_messages)
        state.interjection_count = turns.count_kind(turn_messages, turns.KIND_INTERJECTION)
        state.current_round = max(
            (m.round_number for m in turn_messages if m.sender_key != "user"), default=0
        )

    async def _note_interrupted_speeches(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        orphans: List[ToolCallRecordModel],
        *,
        on_event: Optional[EventCallback],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """끝나지 못한 발언이 실행한 도구를 "발언이 끊겼다" 는 안내에 잇습니다.

        그 발언의 글은 기록되지 않았습니다(취소는 기록하지 않고 올립니다). 도구 기록만 남아
        있어, 에이전트마다 안내 하나를 남기고 그 기록을 거기에 붙입니다 — 화면과 저장 문서가
        그 호출을 발언 카드처럼 보여 줍니다. 돌려주는 값은 {에이전트 키: 그 호출들} 입니다.
        """
        by_agent: Dict[str, List[ToolCallRecordModel]] = {}
        for row in orphans:
            by_agent.setdefault(row.agent_key, []).append(row)
        agents = {a.key: a for a in setup.active_agents}
        executed: Dict[str, List[Dict[str, Any]]] = {}
        for key, rows in by_agent.items():
            agent = agents.get(key) or self.agent_pool.get(key) or setup.orchestrator
            calls = [
                {
                    "tool_name": r.tool_name,
                    "arguments": r.arguments,
                    "output": r.output,
                    "status": r.status,
                    "security": {
                        "decision": r.decision or "", "risk": r.risk or "",
                        "rule": r.rule or "", "approver": r.approver or "",
                    },
                }
                for r in rows
            ]
            executed[key] = calls
            ids = [r.id for r in rows]

            async def _link(note_id: str, ids: List[str] = ids) -> None:
                await db.execute(
                    update(ToolCallRecordModel)
                    .where(ToolCallRecordModel.id.in_(ids))
                    .values(message_id=note_id)
                )

            names = ", ".join(dict.fromkeys(r.tool_name for r in rows))
            await self._record_note(
                db=db, state=state, on_event=on_event, agent=agent,
                round_number=state.current_round, msg_type="error",
                content=(
                    f"> ⚠️ **서버가 다시 시작되어 {agent.name} 의 발언이 끝나지 못했습니다.**\n>\n"
                    f"> 발언이 끊기기 전에 실행된 도구 {len(rows)}건의 기록을 이 자리에 남깁니다"
                    f" ({names}).\n>\n"
                    f"> 끊긴 발언의 글은 기록되지 않아 남아 있지 않습니다."
                ),
                kind=turns.KIND_INTERRUPTED,
                tool_calls=calls,
                before_commit=_link,
            )
        return executed

    async def _settle_drafts(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        *,
        on_event: Optional[EventCallback],
        keep: Tuple[str, ...] = (),
    ) -> None:
        """이어 가지 않을 초안을 정리합니다 — 그 도구 기록은 "끊김" 안내로, 초안 행은 지웁니다.

        `keep` 에 든 종류의 초안은 남깁니다 (이어서 합성이 쓸 합성 초안). 턴이 끝날 때는 이 턴의
        초안을 모두 지웁니다 — 이어 가기가 아닌 턴(새 턴, 결론 내기)의 초안도 여기서 사라집니다.
        """
        leftover = {k: d for k, d in state.resume_drafts.items() if d.get("kind") not in keep}
        ids = {rid for d in leftover.values() for rid in (d.get("state") or {}).get("tool_record_ids") or []}
        if ids:
            rows = (await db.execute(
                select(ToolCallRecordModel)
                .where(ToolCallRecordModel.id.in_(ids), ToolCallRecordModel.message_id.is_(None))
                .order_by(ToolCallRecordModel.created_at)
            )).scalars().all()
            await self._note_interrupted_speeches(db, state, setup, rows, on_event=on_event)
        for key in leftover:
            state.resume_drafts.pop(key, None)
        if not state.turn_id:
            return
        remove = delete(SpeechDraftModel).where(SpeechDraftModel.turn_id == state.turn_id)
        if keep:
            remove = remove.where(SpeechDraftModel.kind.not_in(keep))
        try:
            await db.execute(remove)
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 초안 정리를 못 해도 턴은 끝납니다
            logger.warning(f"Could not clear the speech drafts of turn {state.turn_id}: {exc}")
            await db.rollback()

    async def _set_turn(self, db, state: DebateState, **values: Any) -> None:
        """턴 기록의 단계·상태를 고칩니다. 못 적어도 턴은 계속됩니다 — 기록의 부산물이라."""
        if not state.turn_id:
            return
        try:
            await db.execute(update(TurnModel).where(TurnModel.id == state.turn_id).values(**values))
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not update turn {state.turn_id} ({values}): {type(exc).__name__}: {exc}")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("Rollback after a failed turn update also failed", exc_info=True)

    async def _mark_turn_failed(self, turn_id: str, exc: BaseException) -> None:
        """예외로 멈춘 턴을 "실패" 로 적습니다. 새 DB 세션을 씁니다 — 멈춘 쪽 세션은 깨졌을 수 있습니다."""
        # 계획 승인을 기다리다 멈춘 턴은 오류가 아닙니다 (ADR-028). 사람이 읽을 문장만 적습니다.
        reason = (
            str(exc) if isinstance(exc, plan_gate.PlanApprovalExpired)
            else f"{type(exc).__name__}: {exc}"
        ).strip()
        try:
            async with self.session_factory() as db:
                await db.execute(
                    update(TurnModel)
                    .where(TurnModel.id == turn_id, TurnModel.status == TURN_RUNNING)
                    .values(status=TURN_FAILED, error=reason[:2000])
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 적지 못해도 원래 예외가 올라갑니다
            logger.warning(f"Could not mark turn {turn_id} as failed", exc_info=True)

    async def _plan(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        *,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
        revision: Optional[str] = None,
    ) -> Optional[DebateMessage]:
        """오케스트레이터가 이번 턴의 목표와 분담을 정합니다. (`PlanningPhaseExecutor.plan` 위임)"""
        return await self.planning_phase.plan(
            db, state, setup, on_event=on_event, control=control, revision=revision
        )

    async def _approve_plan(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        plan_message: Optional[DebateMessage],
        *,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl],
    ) -> Optional[DebateMessage]:
        """계획을 사람에게 보이고 승인을 받습니다. (`PlanningPhaseExecutor.approve_plan` 위임)"""
        return await self.planning_phase.approve_plan(
            db, state, setup, plan_message, on_event=on_event, control=control
        )

    async def _turn_config(self, db, state: DebateState) -> Optional[Dict[str, Any]]:
        """턴 기록에 고정해 둔 구성의 지금 값. (`PlanningPhaseExecutor.turn_config` 위임)"""
        return await self.planning_phase.turn_config(db, state)

    async def _plan_proposal(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        plan_message: DebateMessage,
        specialists: List[Agent],
        *,
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """이 계획의 분담표 (전문가별 태스크). (`PlanningPhaseExecutor.plan_proposal` 위임)"""
        return await self.planning_phase.plan_proposal(
            db, state, setup, plan_message, specialists, on_event=on_event
        )

    async def _ask_orchestrator_for_plan_tasks(
        self,
        *,
        orchestrator: Agent,
        specialists: List[Agent],
        state: DebateState,
        plan_message: DebateMessage,
        custom_instructions: str,
    ) -> List[Dict[str, Any]]:
        """계획 본문을 전문가별 태스크로 옮겨 받습니다. (`PlanningPhaseExecutor.ask_orchestrator_for_plan_tasks` 위임)"""
        return await self.planning_phase.ask_orchestrator_for_plan_tasks(
            orchestrator=orchestrator,
            specialists=specialists,
            state=state,
            plan_message=plan_message,
            custom_instructions=custom_instructions,
        )

    async def _debate(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        start_value: List[str],
        resume: bool = False,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> bool:
        """전문가 토론 라운드. (`DebatePhaseExecutor.debate` 위임)"""
        return await self.debate_phase.debate(
            db,
            state,
            setup,
            control=control,
            on_event=on_event,
            start_value=start_value,
            resume=resume,
            redo=redo,
        )

    async def _conclude(
        self,
        db,
        state: DebateState,
        setup: "TurnSetup",
        *,
        stopped_early: bool,
        turn_started_at: Optional[datetime],
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> None:
        """최종 합성 → 산출물 → 장부 → 턴 완료. (`SynthesisPhaseExecutor.conclude` 위임)"""
        await self.synthesis_phase.conclude(
            db,
            state,
            setup,
            stopped_early=stopped_early,
            turn_started_at=turn_started_at,
            control=control,
            on_event=on_event,
        )


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
        graph_node_id: Optional[str] = None,
        graph_port: Optional[str] = None,
        created_at: Optional[datetime] = None,
        kind: str = turns.KIND_NOTE,
        data: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        before_commit: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> DebateMessage:
        """LLM 발언이 아닌 기록을 남깁니다 (지명 결과, 지명 실패 안내 등).

        `_speak` 과 같은 자리에 같은 모양으로 들어갑니다. 그래야 새로고침한 화면과
        저장 파일이 이것을 다른 발언과 똑같이 읽습니다.

        `kind` 와 `data` 는 기록의 자리입니다 (`turns.KIND_*`). 지명·분배는 누가 불렸고 무엇을
        맡았는지를 `data` 에 값으로 적어, 끊긴 턴을 다시 세울 때 문장을 읽지 않게 합니다.

        `before_commit(기록 id)` 는 같은 커밋에서 돌릴 문장입니다 — 끊긴 발언이 남긴 도구 기록을
        이 안내에 잇습니다. 그때 `tool_calls` 는 화면에 함께 보일 그 기록입니다.
        """
        msg_id = str(uuid.uuid4())
        # LLM 을 부르지 않는 기록이라 걸리는 시간이 없습니다 (시작 = 끝).
        now = utc_now()
        turn_meta = turns.meta(kind, **(data or {}))
        await self._persist(
            db,
            lambda: [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key=agent.key,
                sender_name=agent.name,
                sender_role=agent.role,
                content=content,
                round_number=round_number,
                msg_type=msg_type,
                started_at=now,
                finished_at=now,
                graph_node_id=graph_node_id,
                graph_port=graph_port,
                turn_id=state.turn_id,
                turn_meta=turn_meta,
                **({"created_at": created_at} if created_at is not None else {}),
            )],
            what=f"a note from {agent.name}",
            label=f"{agent.name} 의 기록",
            kind=f"note-{agent.key}",
            session_id=state.session_id,
            fallback_title=f"{agent.name} ({agent.role}) — Round {round_number}",
            fallback_body=content,
            on_event=on_event,
            before_commit=(lambda: before_commit(msg_id)) if before_commit is not None else None,
        )

        message = DebateMessage(
            id=msg_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=msg_type,
            tool_calls=list(tool_calls or []),
            started_at=now,
            finished_at=now,
            graph_node_id=graph_node_id,
            graph_port=graph_port,
            turn_meta=turn_meta,
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
        """이번 라운드에 누가 발언할지 정합니다. (`DebatePhaseExecutor.select_speakers` 위임)"""
        return await self.debate_phase.select_speakers(
            db=db,
            state=state,
            strategy=strategy,
            orchestrator=orchestrator,
            active_agents=active_agents,
            round_num=round_num,
            custom_instructions=custom_instructions,
            on_event=on_event,
        )

    async def _ask_orchestrator_for_speakers(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        custom_instructions: str,
    ) -> "tuple[List[Agent], str]":
        """오케스트레이터에게 이번 라운드 발언자와 순서를 물어봅니다. (`DebatePhaseExecutor.ask_orchestrator_for_speakers` 위임)"""
        return await self.debate_phase.ask_orchestrator_for_speakers(
            orchestrator=orchestrator,
            candidates=candidates,
            state=state,
            round_num=round_num,
            custom_instructions=custom_instructions,
        )

    @staticmethod
    def _parse_speaker_selection(
        content: str, candidates: List[Agent]
    ) -> "tuple[List[Agent], str]":
        """응답에서 지명된 에이전트와 사유를 뽑습니다. (`DebatePhaseExecutor.parse_speaker_selection` 위임)"""
        return DebatePhaseExecutor.parse_speaker_selection(content, candidates)

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
        resume: Optional["turns.RoundRecord"] = None,
        done: Optional[Set[str]] = None,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> bool:
        """한 라운드를 병렬로 돕니다. (`DebatePhaseExecutor.run_parallel_round` 위임)"""
        return await self.debate_phase.run_parallel_round(
            db=db,
            state=state,
            strategy=strategy,
            orchestrator=orchestrator,
            active_agents=active_agents,
            round_num=round_num,
            custom_instructions=custom_instructions,
            parallel_limit=parallel_limit,
            control=control,
            on_event=on_event,
            resume=resume,
            done=done,
            redo=redo,
        )

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
        """이번 라운드의 태스크 분배를 받아 기록하고 돌려줍니다. (`DebatePhaseExecutor.dispatch_parallel_tasks` 위임)"""
        return await self.debate_phase.dispatch_parallel_tasks(
            db=db,
            state=state,
            strategy=strategy,
            orchestrator=orchestrator,
            candidates=candidates,
            round_num=round_num,
            custom_instructions=custom_instructions,
            parallel_limit=parallel_limit,
            on_event=on_event,
        )

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
        """오케스트레이터에게 이번 라운드의 태스크 분배를 물어봅니다. (`DebatePhaseExecutor.ask_orchestrator_for_assignments` 위임)"""
        return await self.debate_phase.ask_orchestrator_for_assignments(
            orchestrator=orchestrator,
            candidates=candidates,
            state=state,
            round_num=round_num,
            parallel_limit=parallel_limit,
            custom_instructions=custom_instructions,
        )

    @staticmethod
    def _parse_assignments(
        content: str, candidates: List[Agent]
    ) -> "Tuple[List[Tuple[Agent, str]], str]":
        """응답에서 (에이전트, 태스크) 목록과 분배 사유를 뽑습니다. (`DebatePhaseExecutor.parse_assignments` 위임)"""
        return DebatePhaseExecutor.parse_assignments(content, candidates)

    @staticmethod
    def _assignment_board(assignments: List[Tuple[Agent, str]]) -> str:
        """동시에 도는 동료들이 무엇을 맡았는지 적은 판. (`DebatePhaseExecutor.assignment_board` 위임)"""
        return DebatePhaseExecutor.assignment_board(assignments)

    @staticmethod
    def _parallel_turn_instruction(
        strategy: BaseDebateStrategy, agent: Agent, task: str, board: str
    ) -> str:
        """병렬 라운드에서 한 에이전트에게 붙는 지침. (`DebatePhaseExecutor.parallel_turn_instruction` 위임)"""
        return DebatePhaseExecutor.parallel_turn_instruction(strategy, agent, task, board)

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
        """라운드 끝의 취합 발언. (`DebatePhaseExecutor.merge_parallel_round` 위임)"""
        return await self.debate_phase.merge_parallel_round(
            db=db,
            state=state,
            orchestrator=orchestrator,
            assignments=assignments,
            round_num=round_num,
            custom_instructions=custom_instructions,
            control=control,
            on_event=on_event,
        )

    # ------------------------------------------------------------ 그래프 토론

    async def _graph_for_turn(
        self, db, session_model: SessionModel, max_rounds: int
    ) -> GraphSpec:
        """이 턴에 쓸 그래프를 읽고 검사하고, 고정해 둡니다. (`DebatePhaseExecutor.graph_for_turn` 위임)"""
        return await self.debate_phase.graph_for_turn(db, session_model, max_rounds)

    async def _run_graph(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        orchestrator: Agent,
        active_agents: List[Agent],
        parallel_limit: int,
        max_visits: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        start_value: List[str],
        replay: Optional["turns.GraphReplay"] = None,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> bool:
        """그래프를 단계별로 돕니다. (`DebatePhaseExecutor.run_graph` 위임)"""
        return await self.debate_phase.run_graph(
            db=db,
            state=state,
            spec=spec,
            orchestrator=orchestrator,
            active_agents=active_agents,
            parallel_limit=parallel_limit,
            max_visits=max_visits,
            control=control,
            on_event=on_event,
            start_value=start_value,
            replay=replay,
            redo=redo,
        )

    async def _after_graph_step(
        self,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        orchestrator: Agent,
        step: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> None:
        await self.debate_phase.after_graph_step(
            state, spec, scheduler, orchestrator, step, control, on_event
        )

    async def _finish_graph_step(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        replay: "turns.GraphReplay",
        agents: Dict[str, Agent],
        orchestrator: Agent,
        parallel_limit: int,
        max_steps: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        redo: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """끊긴 단계를 마저 돌립니다. (`DebatePhaseExecutor.finish_graph_step` 위임)"""
        await self.debate_phase.finish_graph_step(
            db=db,
            state=state,
            spec=spec,
            replay=replay,
            agents=agents,
            orchestrator=orchestrator,
            parallel_limit=parallel_limit,
            max_steps=max_steps,
            control=control,
            on_event=on_event,
            redo=redo,
        )

    @staticmethod
    async def _graph_finished(on_event: Optional[EventCallback], reason: str, steps: int) -> None:
        await DebatePhaseExecutor.graph_finished(on_event, reason, steps)

    async def _run_graph_step(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        activations: List[Activation],
        agents: Dict[str, Agent],
        orchestrator: Agent,
        step: int,
        parallel_limit: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        view: Optional[DebateState] = None,
        redo: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Tuple[str, List[str]]]:
        """한 단계의 노드들을 동시에 돌립니다. (`DebatePhaseExecutor.run_graph_step` 위임)"""
        return await self.debate_phase.run_graph_step(
            db=db,
            state=state,
            spec=spec,
            scheduler=scheduler,
            activations=activations,
            agents=agents,
            orchestrator=orchestrator,
            step=step,
            parallel_limit=parallel_limit,
            control=control,
            on_event=on_event,
            view=view,
            redo=redo,
        )

    def _render_graph_input(
        self,
        state: DebateState,
        message: DebateMessage,
        carry: str,
        placeholders: Dict[int, str],
        index_of: Dict[str, int],
    ) -> str:
        """선 하나가 싣고 온 발언 하나. (`DebatePhaseExecutor.render_graph_input` 위임)"""
        return self.debate_phase.render_graph_input(
            state, message, carry, placeholders, index_of
        )

    async def _context_for_node(
        self,
        *,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        activation: Activation,
        speaker: Agent,
        step: int,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        notice: str = "",
    ) -> List[Dict[str, Any]]:
        """노드 하나의 프롬프트. (`DebatePhaseExecutor.context_for_node` 위임)"""
        return await self.debate_phase.context_for_node(
            state=state,
            spec=spec,
            scheduler=scheduler,
            activation=activation,
            speaker=speaker,
            step=step,
            orchestrator=orchestrator,
            on_event=on_event,
            notice=notice,
        )

    @staticmethod
    def _graph_turn_instruction(spec: GraphSpec, activation: Activation, step: int) -> str:
        return DebatePhaseExecutor.graph_turn_instruction(spec, activation, step)

    async def _run_gate(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        activation: Activation,
        orchestrator: Agent,
        step: int,
        on_event: Optional[EventCallback],
        db_lock: asyncio.Lock,
        created_at: datetime,
    ) -> Tuple[str, List[str]]:
        """판정 노드. (`DebatePhaseExecutor.run_gate` 위임)"""
        return await self.debate_phase.run_gate(
            db=db,
            state=state,
            spec=spec,
            activation=activation,
            orchestrator=orchestrator,
            step=step,
            on_event=on_event,
            db_lock=db_lock,
            created_at=created_at,
        )


    # ------------------------------------------------------------ 대화 기억
    #
    # 컨텍스트 창이 차도 잃으면 안 되는 것 — 사용자 발언, 이번 턴 계획, 결정 장부,
    # 앞선 논의의 요약. 무엇을 왜 지키는지는 `app/orchestration/context_memory.py` 에 있습니다.

    @staticmethod
    def _tool_less(agent: Agent) -> Agent:
        """도구와 단계적 사고를 끈 사본.

        JSON 한 줄(발언자 지명·태스크 분배)이나 정해진 형식의 글(장부·요약)을 받는 호출에
        씁니다. 도구를 붙이면 파일을 읽기 시작하고, 단계적 사고 프로토콜이 주입되면
        `Thought 1..N` 을 쓰다가 형식을 놓칩니다.
        """
        return agent.model_copy(update={
            "allowed_mcp_servers": [],
            "allowed_skills": [],
            "allow_peer_consultation": False,
            "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
        })

    def _speech_budget(self, agent: Agent, state: DebateState) -> int:
        """이 발언자의 요청에서 시스템 프롬프트와 대화가 쓸 수 있는 토큰."""
        return context_budget(agent, tools=self._tools_for(agent, state))

    @staticmethod
    def _system_tokens(agent: Agent, state: DebateState) -> int:
        """시스템 프롬프트 몫의 어림값. 페르소나·세션 지침·장부에 고정 지침 몫을 더합니다."""
        text = "\n\n".join(
            part for part in (agent.system_prompt, state.custom_instructions, state.decision_ledger)
            if part
        )
        return memory.text_tokens(agent.model, text) + 512

    @staticmethod
    def _summary_cap(state: DebateState, budget: int) -> int:
        """요약의 글자 상한. 참여자 중 가장 작은 예산을 알면 그것에 맞춥니다."""
        return memory.memory_cap(
            memory.SUMMARY_SHARE, memory.SUMMARY_MAX_CHARS, state.memory_budget or budget
        )

    @staticmethod
    def _snippet(msg: DebateMessage, limit: int) -> str:
        """짧게 옮길 때의 한 조각. 요지가 있으면 요지, 없으면 앞부분.

        앞 N자만 자르면 긴 발언은 서론만 남습니다. 요지는 발언자가 결론을 추려 둔 것입니다.
        자르기 전에 사고 과정을 뗍니다 — 그러지 않으면 N자가 "Thought 1: ..." 로 채워집니다.
        """
        digest = memory.extract_digest(msg.content) if msg.sender_key != "user" else None
        text = digest or strip_reasoning_trace(msg.content)
        return text[:limit]

    def _routing_record(self, state: DebateState, agent: Agent) -> str:
        """발언자 지명·태스크 분배 프롬프트에 넣을 사용자 발언 기록 (짧은 몫).

        유저가 승인한 태스크 분담이 있으면 (ADR-028) 함께 싣습니다. 지명과 라운드별 분배는
        오케스트레이터가 그때그때 정하는데, 승인된 분담을 모르고 정하면 사람이 확인한 것과
        다른 일을 시키게 됩니다.
        """
        record = memory.build_user_record(
            state, model=agent.model,
            token_cap=int(context_budget(agent) * memory.ROUTING_RECORD_SHARE),
        ).text
        parts = [record] if record else []
        if state.plan_tasks:
            parts.append(
                f"{plan_gate.ROUTING_TASKS_HEADING}\n{plan_gate.tasks_block(state.plan_tasks)}"
            )
        return "".join(f"{part}\n\n" for part in parts)

    def _load_memory(self, state: DebateState, session_model: SessionModel) -> None:
        """저장된 장부·요약을 이번 턴 상태로 옮깁니다."""
        ids = [m.id for m in state.messages]
        state.decision_ledger = session_model.decision_ledger or ""
        through = memory.through_index(ids, session_model.ledger_through_id)
        # 반영 지점이 기록에서 사라졌으면 지금까지를 반영된 것으로 봅니다. 옛 기록 전체를
        # 장부 갱신 한 번에 다시 밀어 넣지 않습니다.
        state.ledger_through = len(ids) if through is None else through

        summary = session_model.transcript_summary or ""
        summary_through = memory.through_index(ids, session_model.summary_through_id)
        # 요약은 덮는 자리를 모르면 쓸 수 없습니다 (원문과 겹치거나 빠집니다). 버리고,
        # 필요하면 다시 접습니다.
        if summary.strip() and summary_through:
            state.transcript_summary = summary
            state.summary_through = summary_through

    async def _persist_memory(
        self, db, state: DebateState, on_event: Optional[EventCallback]
    ) -> None:
        """장부와 요약을 저장합니다. 턴이 끝까지 왔을 때만 부릅니다.

        긴급 종료(`session_ops.discard_turn`)는 턴 도중의 취소라 여기까지 오지 않습니다.
        그래서 지워진 발언을 반영한 장부가 남지 않습니다.
        """
        values = {
            "decision_ledger": state.decision_ledger,
            "ledger_through_id": memory.through_id(state.messages, state.ledger_through),
            "transcript_summary": state.transcript_summary if state.summary_through else "",
            "summary_through_id": memory.through_id(state.messages, state.summary_through),
            # 기억을 적었다고 대화 목록에서 "방금 바뀐 대화" 로 올라가지 않게 합니다.
            "updated_at": SessionModel.updated_at,
        }
        try:
            await db.execute(
                update(SessionModel).where(SessionModel.id == state.session_id).values(**values)
            )
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 기억을 못 적어도 턴은 끝납니다
            logger.error(
                f"Could not persist the decision ledger for session {state.session_id}: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("Rollback after a failed ledger commit also failed", exc_info=True)
            if on_event:
                await on_event({
                    "type": "persist_failed",
                    "what": "the decision ledger",
                    "label": "결정 장부",
                    "error": f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                    "saved_to": None,
                })

    async def _context_for_speech(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str,
        *,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """발언 맥락. 창을 넘으면 먼저 앞선 기록을 요약으로 접은 뒤 만듭니다."""
        prompt = self._build_context_for_agent(state, agent, turn_instruction)
        total = self._system_tokens(agent, state) + estimate_tokens(agent.model, prompt)
        folded = await self._ensure_room(
            state, model=agent.model, total=total, budget=self._speech_budget(agent, state),
            orchestrator=orchestrator, on_event=on_event, agent_name=agent.name,
        )
        if folded:
            prompt = self._build_context_for_agent(state, agent, turn_instruction)
        return prompt

    async def _ensure_room(
        self,
        state: DebateState,
        *,
        model: str,
        total: int,
        budget: int,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        agent_name: str,
    ) -> bool:
        """`total` 이 `budget` 을 넘으면 앞선 기록을 요약으로 접습니다. 접었으면 True.

        접지 못해도 괜찮습니다 — `fit_context_window` 가 예전처럼 오래된 것부터 생략합니다.
        """
        if budget <= 0 or total <= budget:
            return False
        start = state.summary_through
        costs = [
            0 if m.msg_type == "error" else memory.text_tokens(model, memory.render_message(m))
            for m in state.messages[start:]
        ]
        cut = memory.choose_fold_cut(
            state,
            message_tokens=costs,
            total_tokens=total,
            budget=budget,
            summary_allowance=(
                self._summary_cap(state, budget) - memory.text_tokens(model, state.transcript_summary)
            ),
        )
        if cut <= start:
            return False
        return await self._fold_into_summary(
            state, orchestrator=orchestrator, cut=cut, on_event=on_event, agent_name=agent_name,
            max_chars=self._summary_cap(state, budget),
        )

    async def _fold_into_summary(
        self,
        state: DebateState,
        *,
        orchestrator: Agent,
        cut: int,
        on_event: Optional[EventCallback],
        agent_name: str,
        max_chars: int = memory.SUMMARY_MAX_CHARS,
    ) -> bool:
        """`messages[summary_through:cut]` 를 누적 요약에 접습니다. 조금이라도 접었으면 True.

        묶음마다 상태에 반영합니다. 뒤 묶음이 실패해도 앞에서 접은 것은 남습니다.
        """
        writer = self._tool_less(orchestrator)
        start = state.summary_through
        items = [
            (index, state.messages[index]) for index in range(start, cut)
            if state.messages[index].msg_type != "error"
        ]
        if not items:
            return False
        room = (
            context_budget(writer)
            - memory.PROMPT_OVERHEAD_TOKENS
            - memory.text_tokens(writer.model, state.transcript_summary)
        )
        if room <= 0:
            logger.warning(
                f"No room to summarize earlier messages with {writer.name} "
                f"(max_context_window={writer.max_context_window}); older ones will be dropped instead"
            )
            return False
        batches = memory.batch_blocks(
            writer.model, [memory.render_message(m) for _index, m in items], room
        )[: memory.SUMMARY_MAX_BATCHES]

        if on_event:
            await on_event({
                "type": "context_summarizing",
                "agent_name": agent_name,
                "messages": cut - start,
            })

        consumed = 0
        try:
            for batch in batches:
                content, _ = await self.llm_caller.call_agent(
                    writer,
                    memory.summary_prompt(
                        previous=state.transcript_summary, blocks=batch,
                        covered=state.summary_through, max_chars=max_chars,
                    ),
                    state.custom_instructions,
                    session_id=state.session_id,
                    mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
                )
                summary = memory.parse_summary(content, max_chars)
                if not summary:
                    raise ValueError("요약 응답이 비어 있습니다")
                consumed += len(batch)
                state.transcript_summary = summary
                state.summary_through = cut if consumed == len(items) else items[consumed - 1][0] + 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 요약에 실패하면 예전처럼 생략합니다
            logger.warning(
                f"Could not summarize earlier messages for {agent_name}; older ones will be "
                f"dropped instead: {type(exc).__name__}: {exc}"
            )
            if on_event:
                await on_event({
                    "type": "context_summary_failed",
                    "agent_name": agent_name,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        folded = state.summary_through - start
        if folded and on_event:
            await on_event({
                "type": "context_summarized",
                "agent_name": agent_name,
                "folded": folded,
                "total": state.summary_through,
            })
        return folded > 0

    async def _update_ledger(
        self,
        *,
        state: DebateState,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        reason: str,
    ) -> bool:
        """결정 장부를 갱신합니다. 갱신했으면 True.

        새 발언이 없거나 실패하면 이전 장부를 그대로 둡니다. 장부는 토론의 부산물이라,
        장부를 못 써서 토론이 멈추면 안 됩니다.
        """
        start, upto = state.ledger_through, len(state.messages)
        fresh = [
            (index, state.messages[index]) for index in range(start, upto)
            if state.messages[index].msg_type != "error"
        ]
        if not any(m.sender_key != "user" for _index, m in fresh):
            return False

        writer = self._tool_less(orchestrator)
        budget = context_budget(writer)
        ledger_chars = memory.memory_cap(
            memory.LEDGER_SHARE, memory.LEDGER_MAX_CHARS, state.memory_budget or budget
        )
        record = memory.build_user_record(
            state, model=writer.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        # 고정된 사용자 발언은 프롬프트에 기록으로 따로 들어가므로 참조로만 둡니다.
        placeholders = {i: memory.user_placeholder(n) for i, n in record.refs.items()}
        opening = memory.opening_index(state)
        if opening is not None:
            placeholders[opening] = "(이번 턴 요청 — 위 [이번 턴 요청]에 전문이 있습니다)"

        room = (
            budget
            - memory.PROMPT_OVERHEAD_TOKENS
            - memory.text_tokens(writer.model, state.decision_ledger)
            - memory.text_tokens(writer.model, record.text)
            - memory.text_tokens(writer.model, state.user_prompt)
        )
        if room <= 0:
            logger.warning(
                f"No room to update the decision ledger with {writer.name} "
                f"(max_context_window={writer.max_context_window})"
            )
            return False

        # 최근 것부터 채웁니다. 못 실은 앞쪽은 이전 장부에 반영돼 있는 경우가 대부분입니다.
        kept: List[str] = []
        used = 0
        for index, msg in reversed(fresh):
            block = memory.render_message(msg, placeholders.get(index))
            cost = memory.text_tokens(writer.model, block)
            if kept and used + cost > room:
                break
            if cost > room:
                block = memory.clip_to_tokens(writer.model, block, room)
                cost = room
            kept.insert(0, block)
            used += cost

        if on_event:
            await on_event({"type": "ledger_update_started", "reason": reason})
        try:
            content, _ = await self.llm_caller.call_agent(
                writer,
                memory.ledger_prompt(
                    previous=state.decision_ledger,
                    user_record=record.text,
                    blocks=kept,
                    skipped=len(fresh) - len(kept),
                    user_prompt=state.user_prompt,
                    max_chars=ledger_chars,
                ),
                state.custom_instructions,
                session_id=state.session_id,
                mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
            )
            ledger = memory.parse_ledger(content, ledger_chars)
            if ledger is None:
                raise ValueError("응답에 장부 형식(`## ` 제목)이 없습니다")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 장부를 못 써도 토론은 계속됩니다
            logger.warning(
                f"Could not update the decision ledger ({reason}) for session {state.session_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            if on_event:
                await on_event({
                    "type": "ledger_update_failed",
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            return False

        state.decision_ledger = ledger
        state.ledger_through = upto
        if on_event:
            await on_event({"type": "ledger_updated", "reason": reason, "ledger": ledger})
        return True

    def _build_context_for_agent(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str = "",
        *,
        use_summary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Prepares discussion transcript for agent turn.

        `turn_instruction` 은 전략이 이 차례에 붙이는 지침입니다. 전략이 순서만
        정하던 시절에는 '자유 토론' 과 '순차 검증' 이 똑같은 프롬프트를 받아,
        발언 순서 말고는 다를 것이 없었습니다. 두 전략의 실제 차이가 여기서
        갈립니다.

        첫 메시지(목표)에는 잘리면 안 되는 것을 고정합니다 — 사용자 발언 기록, 이번 턴
        계획, 앞선 논의 요약. 두 자르기 모두 이 자리를 남깁니다. 기록 안의 같은 발언은
        참조로 바꿔 토큰을 두 번 쓰지 않습니다.

        요약은 원문 전체가 이 발언자의 창에 들어가지 않을 때만 씁니다 (`use_summary`
        를 주지 않으면 여기서 판단). 창이 큰 에이전트는 원문을 그대로 읽습니다.
        """
        budget = self._speech_budget(agent, state)
        record = memory.build_user_record(
            state, model=agent.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        plan_pin = memory.build_plan_pin(
            state, model=agent.model, token_cap=int(budget * memory.PLAN_PIN_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, bool(plan_pin))
        summary = memory.summary_block(state)

        # 라운드 표시는 맨 끝에 둡니다. 목표 메시지에 있으면 라운드마다 두 번째 메시지가 바뀌어,
        # 그 뒤의 기록 전체가 프롬프트 캐시에서 빠집니다 (모듈 설명의 "프롬프트 캐싱").
        turn_prompt = (
            f"[Debate Progress]: Round {state.current_round} of {state.max_rounds}.\n\n"
            f"이제 {agent.name}({agent.role})님의 차례입니다. 앞선 전체 논의 맥락과 직전 "
            f"발언들을 충실히 반영하여 전문적인 의견을 발언하고 필요시 도구를 활용해 주세요."
        )
        if turn_instruction:
            turn_prompt += f"\n\n{turn_instruction}"
        # 유저가 승인한 이 전문가의 태스크 (ADR-028). 고정문에도 있지만, 마지막 메시지에 한 번 더
        # 둡니다 — 긴 토론에서 자기 몫을 가장 덜 잊는 자리입니다.
        own_task = plan_gate.own_task(state.plan_tasks, agent.key)
        if own_task:
            turn_prompt += f"\n\n{own_task}"
        turn_prompt += f"\n\n{memory.DIGEST_INSTRUCTION}"

        # 이 발언자가 마지막으로 말한 뒤에 나온 발언은 원문으로 (모듈 설명의 "발언을 얼마나
        # 넘길까"). 이번 턴에 아직 말하지 않았으면 이번 턴 전체가 새 발언입니다.
        fresh_start = state.turn_message_start
        for index in range(len(state.messages) - 1, -1, -1):
            msg = state.messages[index]
            if msg.sender_key == agent.key and msg.msg_type != "error":
                fresh_start = max(fresh_start, index + 1)
                break
        mentions = {mention_token(agent.name), f"@{agent.key}"}

        def body_for(index: int, msg: DebateMessage) -> str:
            pinned = placeholders.get(index)
            if pinned:
                return pinned
            body = strip_reasoning_trace(msg.content)
            if index >= fresh_start and msg.sender_key != agent.key:
                return body
            if (
                msg.sender_key != agent.key
                and len(body) > memory.DIGEST_MIN_BODY_CHARS
                and not any(token in body for token in mentions)
            ):
                digest = memory.extract_digest(msg.content)
                if digest:
                    return f"{memory.DIGEST_HEADING} (전문 {len(body):,}자 중 요지만 싣습니다)\n{digest}"
            return memory.reference_code_blocks(body, msg.tool_calls)

        def assemble(start: int, with_summary: bool) -> List[Dict[str, Any]]:
            head = [f"[User Goal / Current Request]:\n{state.user_prompt}"]
            head += [part for part in (record.text, plan_pin, summary if with_summary else "") if part]
            context: List[Dict[str, Any]] = [{"role": "user", "content": "\n\n".join(head)}]

            for index in range(start, len(state.messages)):
                msg = state.messages[index]
                # 응답을 못 받은 자리는 맥락에 넣지 않습니다. 실패 안내문을 발언인 양
                # 읽히게 하면 다음 에이전트가 그것을 논평하기 시작합니다.
                if msg.msg_type == "error":
                    continue
                if msg.sender_key == "user":
                    context.append({
                        "role": "user",
                        "content": f"[User]:\n{placeholders.get(index, msg.content)}"
                    })
                else:
                    role_label = f"[{msg.speaker}]"
                    context.append({
                        "role": "assistant" if msg.sender_key == agent.key else "user",
                        # 사고 과정은 기록과 화면에만 남기고 프롬프트에는 싣지 않습니다.
                        # 자기 발언도 같습니다 — `show_steps` 는 사람이 무엇을 볼지를
                        # 정하는 스위치이지, 모델이 무엇을 읽을지를 정하는 것이 아닙니다.
                        "content": f"{role_label}:\n{body_for(index, msg)}",
                    })

            context.append({"role": "user", "content": turn_prompt})
            return context

        full = assemble(0, False)
        if not summary:
            return full
        if use_summary is None:
            use_summary = (
                budget > 0
                and self._system_tokens(agent, state) + estimate_tokens(agent.model, full) > budget
            )
        return assemble(state.summary_through, True) if use_summary else full

    async def _synthesis_prompt_with_notice(
        self,
        state: DebateState,
        agent: Optional[Agent],
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """합성 프롬프트를 만들고, 전사가 잘렸으면 화면에 알립니다."""
        return await self.synthesis_phase.synthesis_prompt_with_notice(state, agent, on_event)

    async def _ensure_synthesis_room(
        self, state: DebateState, agent: Agent, on_event: Optional[EventCallback]
    ) -> None:
        """합성 전사가 창을 넘으면 앞선 기록을 요약으로 접습니다 (`_ensure_room`)."""
        await self.synthesis_phase.ensure_synthesis_room(state, agent, on_event)

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
        return memory_search_tool(self._tools_for(agent, state))

    def _tools_for(
        self, agent: Optional[Agent], state: Optional[DebateState] = None,
    ) -> List[Dict[str, Any]]:
        """이 에이전트의 요청에 실릴 도구 정의. 못 구하면 빈 목록.

        도구 정의는 요청마다 함께 나가는 입력이라, 전사의 크기를 정할 때도 그 몫을 빼야
        합니다 (`context_budget`). 대역 호출기처럼 도구를 모르는 경우에도 합성은 진행합니다.
        """
        if agent is None:
            return []
        if state is not None:
            agent = with_designated_skills(agent, state.skill_designations.get(agent.key))
        try:
            mcp = (self._mcp_for(state) if state else None) or self.llm_caller.mcp_manager
            tools = mcp.get_openai_tools_for_servers(
                self.llm_caller.resolve_tool_servers(agent)
            ) or []
            # 발언 시 실제로 전달되는 도구 목록과 일치해야 합니다 (보안상 제외된 도구는 제외하고 스킬 도구는 포함합니다).
            gate = self._gate_for(state) if state else None
            if gate is not None:
                tools = gate.filter_tools(agent.key, tools, mcp)
            return tools + skill_tools_for(agent) + consultation_tools_for(agent)
        except Exception:  # noqa: BLE001 - 도구 목록을 못 구해도 합성은 진행합니다
            return []

    def _build_synthesis_prompt(
        self, state: DebateState, agent: Optional[Agent] = None
    ) -> List[Dict[str, Any]]:
        """Constructs prompt for orchestrator final consensus & artifact generation."""
        return self.synthesis_phase.build_synthesis_prompt(state, agent)

    # ------------------------------------------------- 다이어그램 문법 자가 수선

    async def _repair_mermaid_blocks(
        self,
        text: str,
        *,
        agent: Agent,
        custom_instructions: str,
        state: DebateState,
        on_event: Optional[EventCallback],
    ) -> str:
        """합성 본문의 Mermaid 다이어그램 문법을 검증하고 오류 발생 시 수정을 요청합니다."""
        return await self.synthesis_phase.repair_mermaid_blocks(
            text, agent=agent, custom_instructions=custom_instructions, state=state, on_event=on_event
        )

    async def _ask_for_fixed_diagrams(
        self,
        fixer: Agent,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        state: DebateState,
        custom_instructions: str,
        attempt: int,
    ) -> List[str]:
        """문법 오류가 발생한 다이어그램을 취합해 수정을 요청하고 보정된 다이어그램 목록을 반환합니다."""
        return await self.synthesis_phase.ask_for_fixed_diagrams(
            fixer, broken, state, custom_instructions, attempt
        )

    @staticmethod
    def _splice_blocks(
        text: str,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        replacements: List[str],
    ) -> str:
        """수정된 다이어그램을 원문의 본래 위치에 치환하여 삽입합니다."""
        return SynthesisPhaseExecutor.splice_blocks(text, broken, replacements)

    def _turn_speeches(self, state: DebateState) -> List[DebateMessage]:
        """이번 턴의 전문가 발언 목록을 반환합니다 (사용자 입력, 오케스트레이터 메시지, 실패 안내 제외)."""
        return self.synthesis_phase.turn_speeches(state)

    def _debate_code_artifacts(self, state: DebateState, stamp: str) -> List[ArtifactItem]:
        """이번 턴 전문가 발언에서 코드 산출물을 추출합니다 (에이전트별 최신 코드 발언 기준)."""
        return self.synthesis_phase.debate_code_artifacts(state, stamp)

    def _synthesis_fallback_report(
        self, state: DebateState, failure_reason: str, synth_text: str
    ) -> str:
        """합성 발언이 비어 있거나 실패했을 때의 대체 보고서를 작성합니다 (전문가별 최종 발언 취합)."""
        return self.synthesis_phase.synthesis_fallback_report(state, failure_reason, synth_text)

    def _extract_artifacts_from_synthesis(
        self,
        session_id: str,
        synth_text: str,
        state: DebateState,
        synthesis_failed: bool = False,
        completed_at: Optional[datetime] = None,
        turn_started_at: Optional[datetime] = None,
        failure_reason: str = "LLM 연결 끊김",
    ) -> List[ArtifactItem]:
        """이 턴의 산출물: 최종 결론, 종합 다이어그램, 전문가 코드, 요약 JSON."""
        return self.synthesis_phase.extract_artifacts_from_synthesis(
            session_id,
            synth_text,
            state,
            synthesis_failed=synthesis_failed,
            completed_at=completed_at,
            turn_started_at=turn_started_at,
            failure_reason=failure_reason,
        )

_orchestrator_engine: Optional[OrchestratorEngine] = None


def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
