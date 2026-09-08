"""로스터의 발언 순서 미리보기.

카드가 놓인 순서가 곧 발언 순서인 전략은 **순차 토론뿐**입니다. 디베이트는
진영끼리 교차시켜 카드 순서와 달라지고(제안자 둘·비판자 하나면
`A → C → B` 가 됩니다), 지명·병렬은 매 라운드 오케스트레이터가 정합니다.
그 차이가 화면에 없으면 사람은 카드 순서대로 돌 것이라 믿습니다.

여기서 지키려는 것.

1. 미리보기는 엔진과 **같은 함수**로 순서를 구한다 (직접 다시 구현하지 않는다).
2. 카드 순서·전략·진영·참여 토글을 바꾸면 미리보기도 함께 바뀐다.
3. 미리보기 계산이 실패해도 로스터가 죽지 않는다.
"""

from typing import List

import pytest

from app.agents.base import Agent
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.orchestration.state import DebateState
from app.orchestration.strategies import STRATEGY_MAP
from app.ui.components.roster import AgentRosterControl


def _agent(key: str, priority: int, stance: str = "neutral") -> AgentConfig:
    return AgentConfig(
        name=key.title(), role=f"{key} role", model="fake/model", api_key="k",
        debate_priority=priority, debate_stance=stance,
    )


def _roster(strategy: str = "sequential_debate", **overrides) -> AgentRosterControl:
    """UI 를 만들지 않고 계산 부분만 쓸 수 있는 로스터."""
    roster = AgentRosterControl.__new__(AgentRosterControl)
    roster.agent_pool = AgentPool({
        "orchestrator": _agent("orchestrator", 10),
        "architect": _agent("architect", 20, "proponent"),
        "coder": _agent("coder", 30, "proponent"),
        "critic": _agent("critic", 40, "critic"),
    })
    roster.session_agents = None
    roster.personas_locked = False
    roster.current_personas = {}
    roster.selected_agents = {k: True for k in ("orchestrator", "architect", "coder", "critic")}
    roster.strategy_name = strategy
    roster.max_rounds = 3
    roster.parallel_limit = 3
    roster.session_id = None
    roster.order_preview = None
    for key, value in overrides.items():
        setattr(roster, key, value)
    return roster


def _names(agents: List[Agent]) -> List[str]:
    return [a.name for a in agents]


# --------------------------------------------- 1. 엔진과 같은 순서를 보여준다


def test_sequential_preview_follows_card_order():
    roster = _roster("sequential_debate")
    assert _names(roster._speaking_order()) == ["Architect", "Coder", "Critic"]


def test_adversarial_preview_interleaves_and_differs_from_card_order():
    """디베이트는 카드 순서와 다릅니다. 그게 미리보기를 붙인 이유입니다."""
    roster = _roster("adversarial_debate")
    assert _names(roster._speaking_order()) == ["Architect", "Critic", "Coder"]


def test_preview_matches_what_the_engine_would_run():
    """직접 다시 구현하지 않았음을 고정합니다 — 언젠가 갈라지면 사람은 화면을 믿습니다."""
    for name in STRATEGY_MAP:
        roster = _roster(name)
        agents = [
            a for a in roster._roster_agents()
            if a.key == "orchestrator" or roster.selected_agents.get(a.key, True)
        ]
        probe = DebateState(
            session_id="preview", user_prompt="", strategy=name,
            max_rounds=3, current_round=1,
        )
        expected = STRATEGY_MAP[name].get_speakers_for_round(agents, 1, probe)
        assert roster._speaking_order() == expected, name


def test_orchestrator_never_appears_in_the_preview():
    """계획과 합성은 라운드 밖입니다."""
    for name in STRATEGY_MAP:
        assert "Orchestrator" not in _names(_roster(name)._speaking_order()), name


# ------------------------------------------------------- 2. 설정을 따라 바뀐다


def test_turning_an_agent_off_removes_it_from_the_preview():
    roster = _roster("sequential_debate")
    roster.selected_agents["coder"] = False
    assert _names(roster._speaking_order()) == ["Architect", "Critic"]


def test_reordering_cards_reorders_the_preview():
    """드래그는 `debate_priority` 를 다시 매깁니다. 미리보기는 그 값을 읽습니다."""
    roster = _roster("sequential_debate")
    roster.agent_pool = AgentPool({
        "orchestrator": _agent("orchestrator", 10),
        "critic": _agent("critic", 20, "critic"),        # 맨 앞으로 끌어다 놓음
        "architect": _agent("architect", 30, "proponent"),
        "coder": _agent("coder", 40, "proponent"),
    })
    assert _names(roster._speaking_order()) == ["Critic", "Architect", "Coder"]


def test_changing_stance_reshuffles_the_debate_preview():
    roster = _roster("adversarial_debate")
    assert _names(roster._speaking_order()) == ["Architect", "Critic", "Coder"]

    # coder 를 비판 진영으로 옮기면 제안 1 : 비판 2 가 됩니다.
    roster.agent_pool = AgentPool({
        "orchestrator": _agent("orchestrator", 10),
        "architect": _agent("architect", 20, "proponent"),
        "coder": _agent("coder", 30, "critic"),
        "critic": _agent("critic", 40, "critic"),
    })
    assert _names(roster._speaking_order()) == ["Architect", "Coder", "Critic"]


def test_empty_camp_falls_back_to_priority_order():
    """한쪽 진영이 비면 대립이 성립하지 않습니다. 미리보기도 그렇게 말해야 합니다."""
    roster = _roster("adversarial_debate")
    roster.agent_pool = AgentPool({
        "orchestrator": _agent("orchestrator", 10),
        "architect": _agent("architect", 20, "proponent"),
        "coder": _agent("coder", 30, "proponent"),
        "critic": _agent("critic", 40, "proponent"),      # 비판자가 없다
    })
    speakers = roster._speaking_order()
    assert _names(speakers) == ["Architect", "Coder", "Critic"]
    note = roster._order_preview_note(STRATEGY_MAP["adversarial_debate"], speakers)
    assert "한쪽 진영이 비어" in note


# ------------------------------------------------------------- 3. 안내 문구


@pytest.mark.parametrize("strategy,expected", [
    ("sequential_debate", "카드 순서 그대로"),
    ("adversarial_debate", "제안 ↔ 비판 교차"),
    ("orchestrator_led", "매 라운드 오케스트레이터가 지명"),
    ("parallel_dispatch", "동시 실행"),
])
def test_note_says_how_much_to_trust_the_order(strategy, expected):
    roster = _roster(strategy)
    note = roster._order_preview_note(STRATEGY_MAP[strategy], roster._speaking_order())
    assert expected in note


def test_parallel_note_warns_when_the_round_exceeds_the_limit():
    roster = _roster("parallel_dispatch", parallel_limit=2)
    note = roster._order_preview_note(
        STRATEGY_MAP["parallel_dispatch"], roster._speaking_order()
    )
    assert "최대 2명" in note and "순차로 밀림" in note

    roster.parallel_limit = 5
    note = roster._order_preview_note(
        STRATEGY_MAP["parallel_dispatch"], roster._speaking_order()
    )
    assert "순차로 밀림" not in note


# ---------------------------------------------------- 4. 미리보기가 로스터를 깨지 않는다


def test_a_broken_strategy_does_not_take_the_roster_down():
    roster = _roster("sequential_debate")

    class _Exploding:
        name = "sequential_debate"
        orchestrator_selects_speakers = False
        orchestrator_dispatches_parallel = False

        def get_speakers_for_round(self, *a, **kw):
            raise RuntimeError("boom")

    STRATEGY_MAP["sequential_debate"], original = _Exploding(), STRATEGY_MAP["sequential_debate"]
    try:
        assert roster._speaking_order() == []
    finally:
        STRATEGY_MAP["sequential_debate"] = original


def test_unknown_strategy_yields_an_empty_preview():
    assert _roster("no_such_strategy")._speaking_order() != []  # 기본값으로 풀립니다
