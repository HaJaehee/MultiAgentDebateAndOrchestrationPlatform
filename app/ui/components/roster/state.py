"""로스터 패널의 상태. 이 대화의 설정과 선택 규칙이고, NiceGUI 를 모릅니다.

하위 컴포넌트들은 서로의 속성을 읽지 않습니다. 함께 봐야 하는 값은 전부 여기 있고, 생성자로 같은 객체를
받습니다. 화면 없이 만들어 시험할 수 있습니다 (`tests/test_order_preview.py`, `tests/test_roster_selection.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.agents.base import Agent
from app.agents.pool import AgentPool
from app.config import DEFAULT_SUB_DEBATE_ROUNDS, ORCHESTRATOR_KEY, clamp_sub_debate_rounds
from app.orchestration.roster_service import agent_admin_lock_reason
from app.orchestration.state import DebateState
from app.orchestration.strategies import (
    DEFAULT_STRATEGY,
    STRATEGY_MAP,
    order_by_priority,
    resolve_strategy_name,
    specialists_of,
)

logger = logging.getLogger(__name__)


def is_selected(
    key: str,
    active_keys: List[str],
    known_keys: Optional[List[str]],
    personas_locked: bool = False,
) -> bool:
    """이 대화에서 이 에이전트를 켜 둘지.

    `active_keys` 는 켜 둔 것만 담는 허용 목록이라, 목록에 없다는 사실만으로는
    "사용자가 끈 것" 과 "그 대화를 설정할 때는 없던 것" 을 구분할 수 없습니다.
    구분하지 않으면 둘 중 하나가 반드시 틀립니다 — conf.json 에 에이전트를
    추가했더니 기존 대화에서 전부 꺼진 것으로 보이거나(지금까지의 증상),
    반대로 사용자가 끈 에이전트가 새로고침할 때마다 되살아납니다.

    `known_keys` 는 그 대화의 로스터를 저장할 때 존재하던 에이전트 전부입니다.
    여기에 없는 키는 그 뒤에 추가된 것이므로, 기본값(켜짐)으로 둡니다.

    단 **이미 토론이 시작된 대화**는 예외입니다. 첫 발언과 함께 참여 에이전트와
    페르소나가 고정되므로, 그 뒤에 conf.json 에 추가된 에이전트가 저절로
    끼어들면 앞선 발언과 뒤의 발언이 서로 다른 구성에서 나오게 됩니다. 그런
    키는 꺼 둔 채로 보여주고, 정말 합류시킬지는 사용자가 정합니다.
    """
    if not active_keys:
        # 로스터가 아예 비어 있는 대화 (예전 기록이나 손상된 행). 켤지 끌지를
        # 말해 주는 정보가 없으므로 기본값인 "전부 켜짐" 으로 둡니다.
        return True
    if key in active_keys:
        return True
    if known_keys:
        if key in known_keys:
            return False                      # 사용자가 끈 에이전트
        return not personas_locked            # 저장 이후에 생긴 에이전트
    # 이 컬럼이 생기기 전에 만들어진 대화입니다. 그때 무엇이 있었는지 알 수 없어
    # 둘 중 하나는 틀리는데, 지금 있는 에이전트를 켜는 쪽을 고릅니다. 새 에이전트가
    # 모든 옛 대화에서 꺼져 보이는 것보다, 예전에 꺼 둔 에이전트가 한 번 되살아나고
    # 그 대화를 열어 보는 순간 기록이 남아 다시 정확해지는 편이 낫습니다.
    return True


def order_preview_note(strategy: Any, speakers: List[Agent], parallel_limit: int) -> str:
    """순서 옆에 붙는 한 줄. 이 순서를 얼마나 믿어도 되는지 말합니다."""
    if strategy.orchestrator_dispatches_parallel:
        over = len(speakers) > parallel_limit
        note = f"동시 실행 (한 번에 최대 {parallel_limit}명"
        note += ", 나머지는 순차로 밀림)" if over else ")"
        return note + " · 태스크는 매 라운드 오케스트레이터가 나눕니다"
    if strategy.orchestrator_selects_speakers:
        return "매 라운드 오케스트레이터가 지명합니다 · 아래는 지명 실패 시의 순서"
    if strategy.name == "adversarial_debate":
        stances = {a.debate_stance for a in speakers}
        if not ({"proponent", "critic"} <= stances):
            return "한쪽 진영이 비어 대립이 성립하지 않습니다 · 우선순위 순으로 진행"
        return "제안 ↔ 비판 교차 · 중립은 맨 뒤 (카드 순서와 다를 수 있습니다)"
    return "카드 순서 그대로 진행 · 각 에이전트가 이전 발언자의 결론을 이어받습니다"


@dataclass


class RosterState:
    """이 화면의 로스터가 들고 있는 값. 저장은 화면(`ExpertPage.on_config_changed`)이 합니다."""

    agent_pool: AgentPool
    # 이 대화의 참여 여부 (에이전트 키 → 켜짐). 비워 두면 풀의 에이전트를 전부 켭니다.
    selected_agents: Dict[str, bool] = field(default_factory=dict)
    strategy_name: str = DEFAULT_STRATEGY
    max_rounds: int = 3
    # 결정 장부가 수렴하면 남은 라운드를 건너뛸지, 턴 토큰 예산(0 = 없음) (v1.5).
    converge_early: bool = True
    token_budget: int = 0
    # 전문가가 발언 도중 사용자에게 질문할 수 있는지 (`ask__ask_user`, v1.5). 새 대화 세션은 기본 활성화합니다.
    ask_user: bool = True
    # 미해결 쟁점을 전문가 두 명이 따로 다루는 서브 토론을 허용하는지 (ADR-034). 호출이 늘어나므로 켜야 돕니다.
    sub_debates: bool = False
    # 서브 토론 한 번의 최대 라운드 수. 본 토론의 `max_rounds` 와 별개입니다.
    sub_debate_rounds: int = DEFAULT_SUB_DEBATE_ROUNDS
    # 병렬 지시 전략에서 한 라운드에 동시에 띄울 에이전트 수의 상한.
    # 다른 전략에서는 쓰이지 않으므로 그 전략일 때만 화면에 나옵니다.
    parallel_limit: int = 3
    # 이 대화의 도구 보안 모드. 비어 있으면 conf.json 의 `tool_security.mode` 를 따릅니다.
    tool_mode: str = ""
    # 승인 카드의 "이 대화에서 허용·거부" 로 쌓인 규칙 (세션 DB). 규칙 창에서 보고 지웁니다.
    tool_grants: List[str] = field(default_factory=list)
    tool_denials: List[str] = field(default_factory=list)
    # 그래프 토론이 쓸 그래프 파일 id (`data/graphs/<id>.json`). 다른 전략에서는 읽히지 않습니다.
    graph_id: str = ""
    # 그래프 전략일 때 참여자 = 그래프에 놓인 에이전트. 카드 체크박스가 이 값을 따라갑니다.
    graph_agent_keys: Optional[Set[str]] = None
    custom_instructions: str = ""
    # 이 대화의 결정 장부. 오케스트레이터가 쓰고, 토론이 돌지 않을 때는 사람이 고칠 수 있습니다 (ADR-038).
    decision_ledger: str = ""
    # 이 대화의 작업 공간. 빈 문자열이면 conf.json 기본값을 씁니다.
    workspace_dir: str = ""
    session_id: Optional[str] = None
    personas_locked: bool = False
    # 잠긴 대화가 잠글 때 고정한 에이전트. 살아 있는 풀 대신 이것을 그립니다.
    session_agents: Optional[List[Agent]] = None
    current_personas: Optional[Dict[str, Any]] = None
    # MCP 구성은 프로세스 전체가 공유합니다. 토론이 하나라도 돌고 있으면
    # 그 도구가 지금 쓰이는 중이므로 잠급니다.
    mcp_locked: bool = False
    mcp_lock_reason: str = ""
    # 드래그로 순서를 바꾸는 중에 집어 든 에이전트 키.
    dragging_key: Optional[str] = None
    # 카드 보기 방식 (이 화면에만 걸림, conf.json 과 무관). 요약 보기는 아이콘과 이름만,
    # 비활성 숨기기는 참여에서 뺀 카드와 꺼 둔 에이전트 줄을 감춥니다. 어느 쪽이든
    # 보이는 카드끼리는 드래그로 순서를 바꿀 수 있습니다.
    compact_cards: bool = False
    hide_inactive: bool = False

    def __post_init__(self) -> None:
        if not self.selected_agents:
            self.selected_agents = {agent.key: True for agent in self.agent_pool.list_all()}

    # ------------------------------------------------------------ 누가 있는가

    def roster_agents(self) -> List[Agent]:
        """이 대화의 로스터에 보일 에이전트 (발언 순서대로).

        잠긴 대화는 잠글 때 고정된 구성을, 아직 시작하지 않은 대화는 살아 있는 풀을
        씁니다. 이것을 쓰지 않으면 conf.json 에서 지운 에이전트가 카드도 없이
        발언하는 대화가 생깁니다 — 실행은 스냅샷을 보는데 화면만 풀을 보기 때문에.
        """
        agents = (
            list(self.session_agents)
            if (self.personas_locked and self.session_agents)
            else self.agent_pool.list_all()
        )
        # 카드가 놓인 순서가 곧 발언 순서입니다. 그래야 드래그로 순서를 바꾼다는
        # 말이 성립합니다. 오케스트레이터는 라운드 밖에서 계획·합성을 맡으므로
        # 순서와 무관하게 늘 맨 앞에 둡니다.
        ordered = order_by_priority(agents)
        return (
            [a for a in ordered if a.key == ORCHESTRATOR_KEY]
            + [a for a in ordered if a.key != ORCHESTRATOR_KEY]
        )

    def find_agent(self, agent_key: str) -> Optional[Agent]:
        return next((a for a in self.roster_agents() if a.key == agent_key), None)

    def set_session_agents(self, agents: Optional[List[Agent]]) -> None:
        """이 대화가 실제로 쓰는 에이전트 목록을 갈아 끼웁니다 (잠긴 대화의 스냅샷)."""
        self.session_agents = list(agents) if agents else None

    def known_agent_keys(self) -> List[str]:
        """이 대화가 알고 있는 에이전트 전부 (선택 여부와 무관)."""
        return [agent.key for agent in self.roster_agents()]

    def active_agent_keys(self) -> List[str]:
        keys = [k for k, v in self.selected_agents.items() if v]
        if ORCHESTRATOR_KEY not in keys:
            keys = [ORCHESTRATOR_KEY] + keys
        return keys

    def participates(self, agent_key: str) -> bool:
        """이 대화의 로스터에서 그 에이전트가 토론에 참여하는지 (그래프 토론이면 그래프에 놓였는지)."""
        if self.graph_agent_keys is not None:
            return agent_key in self.graph_agent_keys
        return self.selected_agents.get(agent_key, True)

    def participants(self) -> List[Agent]:
        """참여로 체크한 에이전트. 오케스트레이터는 늘 들어갑니다."""
        return [
            a for a in self.roster_agents()
            if a.key == ORCHESTRATOR_KEY or self.selected_agents.get(a.key, True)
        ]

    def selected_specialists(self) -> List[str]:
        """참여로 체크한 전문가의 키 (카드 순서대로)."""
        return [a.key for a in order_by_priority(specialists_of(self.participants()))]

    @property
    def active_count(self) -> int:
        if self.graph_agent_keys is not None:
            # 그래프 토론은 체크박스가 아니라 그래프에 놓인 에이전트가 참여합니다. 세션 목록
            # 카드의 'N Agents' 와 같은 수여야 합니다 (`sidebar.active_agent_count`).
            return len({ORCHESTRATOR_KEY, *self.graph_agent_keys})
        return len([k for k, v in self.selected_agents.items() if v])

    # ------------------------------------------------------------ 선택

    @property
    def selection_locked(self) -> bool:
        """참여 체크박스가 잠기는 경우 (그래프 토론, 이미 시작된 대화)."""
        return self.graph_agent_keys is not None or self.personas_locked

    def select_all_specialists(self, value: bool) -> None:
        """오케스트레이터는 늘 참여하므로 건드리지 않습니다."""
        for agent in self.roster_agents():
            if agent.key != ORCHESTRATOR_KEY:
                self.selected_agents[agent.key] = value

    def sync_with_pool(self) -> None:
        """선택 상태를 지금 풀에 있는 에이전트에 맞춥니다.

        새로 생긴 에이전트는 **아직 시작하지 않은 대화에서만** 켠 채로 둡니다.
        이미 토론이 시작된 대화는 그때의 구성으로 고정된 것이므로, 나중에 추가된
        에이전트가 슬쩍 끼어들면 안 됩니다 (`is_selected` 와 같은 규칙).

        설정에서 사라진 에이전트는 선택 목록에서도 지웁니다. 남겨 두면 그 키가
        대화의 `active_agents` 에 계속 저장되어, 있지도 않은 에이전트가 기록에
        쌓입니다.
        """
        live_keys = [agent.key for agent in self.roster_agents()]
        default = not self.personas_locked
        self.selected_agents = {
            key: self.selected_agents.get(key, default) for key in live_keys
        }

    # ------------------------------------------------------------ 잠금

    def admin_lock_reason(self) -> str:
        """지금 에이전트 구성을 바꾸면 안 되는 이유 (`agent_admin_lock_reason`)."""
        return agent_admin_lock_reason(
            self.mcp_lock_reason if self.mcp_locked else "", self.personas_locked
        )

    # ------------------------------------------------------------ 발언 순서

    def speaking_order(self) -> List[Agent]:
        """지금 설정으로 한 라운드를 돌면 나올 발언 순서.

        엔진과 **같은 함수**를 부릅니다 (`strategy.get_speakers_for_round`).
        여기서 순서를 다시 구현하면 화면과 실제가 언젠가 갈라지고, 그때
        사람이 믿는 쪽은 화면입니다.

        오케스트레이터를 목록에 넣어 넘기는 것도 엔진과 같습니다 — 전략이
        `specialists_of()` 로 직접 빼냅니다.
        """
        strategy = STRATEGY_MAP.get(resolve_strategy_name(self.strategy_name))
        if strategy is None:
            return []
        probe = DebateState(
            session_id=self.session_id or "preview",
            user_prompt="",
            strategy=strategy.name,
            max_rounds=self.max_rounds,
            current_round=1,
        )
        try:
            return strategy.get_speakers_for_round(self.participants(), 1, probe)
        except Exception:  # noqa: BLE001 - 미리보기가 로스터를 깨뜨리면 안 됩니다
            logger.warning("Could not compute the speaking-order preview", exc_info=True)
            return []

    # ------------------------------------------------------------ 도구 규칙

    def set_tool_rules(self, grants: Optional[List[str]], denials: Optional[List[str]]) -> None:
        self.tool_grants = [str(r) for r in grants or [] if str(r).strip()]
        self.tool_denials = [str(r) for r in denials or [] if str(r).strip()]

    def tool_rules_label(self) -> str:
        count = len(self.tool_grants) + len(self.tool_denials)
        return f"대화 규칙 {count}" if count else "대화 규칙"

    # ------------------------------------------------------------ 대화 열기

    def apply_session(
        self,
        agent_pool: AgentPool,
        active_keys: List[str],
        strategy: str,
        max_rounds: int,
        instructions: str,
        parallel_limit: int,
        converge_early: bool,
        token_budget: int,
        ask_user: bool,
        session_id: Optional[str],
        workspace_dir: str,
        personas_locked: bool,
        personas: Optional[Dict[str, Any]],
        known_keys: Optional[List[str]],
        session_agents: Optional[List[Agent]],
        tool_mode: str,
        sub_debates: bool = False,
        sub_debate_rounds: int = DEFAULT_SUB_DEBATE_ROUNDS,
    ) -> None:
        self.agent_pool = agent_pool
        # 잠금 여부와 스냅샷이 선택 규칙과 카드 목록을 함께 정하므로 먼저 반영합니다.
        self.personas_locked = personas_locked
        self.set_session_agents(session_agents)
        self.selected_agents = {
            agent.key: is_selected(agent.key, active_keys, known_keys, personas_locked)
            for agent in self.roster_agents()
        }
        self.strategy_name = resolve_strategy_name(strategy)
        self.max_rounds = max_rounds
        self.parallel_limit = max(1, int(parallel_limit or 3))
        self.converge_early = bool(converge_early)
        self.token_budget = max(0, int(token_budget or 0))
        self.ask_user = bool(ask_user)
        self.sub_debates = bool(sub_debates)
        self.sub_debate_rounds = clamp_sub_debate_rounds(sub_debate_rounds)
        self.tool_mode = tool_mode or ""
        self.custom_instructions = instructions
        self.workspace_dir = workspace_dir or ""
        self.session_id = session_id
        if personas is not None:
            self.current_personas = personas
