"""conf.json 에 에이전트를 추가했을 때 기존 대화의 로스터가 어떻게 보이는가.

`sessions.active_agents` 는 켜 둔 것만 담는 허용 목록입니다. 목록에 없다는 사실
하나로는 "사용자가 끈 에이전트" 와 "그 대화를 설정할 때는 없던 에이전트" 를
구분할 수 없고, 구분하지 않으면 둘 중 하나가 반드시 틀립니다.

* 전부 꺼진 것으로 보면 → 새로 추가한 에이전트가 모든 기존 대화에서 흐리게 나온다.
* 전부 켜진 것으로 보면 → 사용자가 끈 에이전트가 새로고침할 때마다 되살아난다.

그래서 그 대화를 저장할 때 존재하던 에이전트(`known_agents`)를 함께 적고,
여기서 세 경우를 갈라 놓습니다.
"""

import pytest

from app.ui.components.roster import AgentRosterControl

FOUR = ["orchestrator", "architect", "coder", "critic"]


def selected(key, active, known, locked=False):
    return AgentRosterControl._is_selected(key, active, known, locked)


def test_agent_kept_on_stays_on():
    assert selected("architect", FOUR, FOUR) is True


def test_agent_the_user_turned_off_stays_off():
    """끈 상태가 새로고침을 넘겨 살아남아야 합니다. 엔진도 같은 목록을 읽습니다."""
    active = ["orchestrator", "coder", "critic"]      # architect 를 껐다
    assert selected("architect", active, FOUR) is False


def test_agent_added_to_conf_after_the_session_shows_up_enabled():
    """이번에 고친 증상. 새 에이전트는 기존 대화에서도 켜진 채로 보입니다."""
    assert selected("researcher", FOUR, FOUR) is True


def test_new_agent_and_disabled_agent_are_told_apart():
    active = ["orchestrator", "coder", "critic"]      # architect 를 껐다
    known = FOUR                                      # 그때는 넷뿐이었다
    assert selected("architect", active, known) is False, "끈 것은 꺼진 채로"
    assert selected("researcher", active, known) is True, "새로 생긴 것은 켜진 채로"


@pytest.mark.parametrize("key", FOUR + ["researcher"])
def test_sessions_older_than_the_record_light_everything_up(key):
    """`known_agents` 가 생기기 전 대화는 그때 무엇이 있었는지 알 수 없습니다.

    새 에이전트가 옛 대화 전부에서 꺼져 보이는 것보다, 한 번 켜진 채로 보이고
    그 대화를 열어 보는 순간 기록이 남아 정확해지는 편이 낫습니다.
    """
    assert selected(key, ["orchestrator", "coder"], []) is True


def test_a_session_with_no_roster_at_all_enables_everything():
    assert selected("architect", [], []) is True
    assert selected("architect", [], FOUR) is True


# ------------------------------------------------------------- 이미 시작한 대화

def test_a_started_conversation_does_not_pick_up_new_agents():
    """첫 발언과 함께 참여 에이전트가 고정됩니다.

    그 뒤 conf.json 에 추가된 에이전트가 저절로 끼어들면, 앞선 발언과 뒤의 발언이
    서로 다른 구성에서 나오게 되어 기록을 해석할 수 없습니다. 꺼 둔 채로 보여주고
    정말 합류시킬지는 사용자가 정합니다.
    """
    assert selected("researcher", FOUR, FOUR, locked=True) is False


def test_a_started_conversation_keeps_the_agents_it_already_had():
    active = ["orchestrator", "coder", "critic"]      # architect 를 껐다
    assert selected("coder", active, FOUR, locked=True) is True
    assert selected("architect", active, FOUR, locked=True) is False


def test_the_lock_does_not_change_sessions_older_than_the_record():
    """`known_agents` 가 없으면 그때 무엇이 있었는지 알 수 없습니다. 규칙은 그대로."""
    assert selected("researcher", ["orchestrator", "coder"], [], locked=True) is True


def test_on_agent_toggle_updates_card_classes():
    """체크박스를 토글하면 전달받은 카드의 CSS 클래스가 즉시 밝음/어두움으로 교체되어야 합니다."""
    control = AgentRosterControl()

    class FakeCard:
        def __init__(self):
            self.is_deleted = False
            self.calls = []

        def classes(self, add=None, remove=None, replace=None):
            self.calls.append({"add": add, "remove": remove, "replace": replace})

    card = FakeCard()

    # 1. 켜기 (True)
    control._on_agent_toggle("architect", True, card=card)
    assert control.selected_agents["architect"] is True
    assert card.calls[-1] == {
        "remove": "bg-slate-900/60 border-slate-800 opacity-50",
        "add": "bg-slate-800/90 border-indigo-500/60",
        "replace": None,
    }

    # 2. 끄기 (False)
    control._on_agent_toggle("architect", False, card=card)
    assert control.selected_agents["architect"] is False
    assert card.calls[-1] == {
        "remove": "bg-slate-800/90 border-indigo-500/60",
        "add": "bg-slate-900/60 border-slate-800 opacity-50",
        "replace": None,
    }



def _control_with_four_agents(monkeypatch) -> AgentRosterControl:
    """conf.json 없이도 돌도록 풀을 직접 넣습니다."""
    from app.agents.pool import AgentPool
    from app.config import AgentConfig
    from app.ui.components import roster

    pool = AgentPool({
        key: AgentConfig(name=key.title(), role=key, model="openai/gpt-4o-mini", api_key="sk-test") for key in FOUR
    })
    monkeypatch.setattr(roster, "get_agent_pool", lambda: pool)
    return AgentRosterControl()


def test_select_none_turns_every_specialist_off_but_keeps_the_orchestrator(monkeypatch):
    control = _control_with_four_agents(monkeypatch)
    control._on_select_all(False)
    assert {k: v for k, v in control.selected_agents.items() if k != "orchestrator"} == {
        "architect": False, "coder": False, "critic": False,
    }
    assert control.selected_agents["orchestrator"] is True
    assert control.get_active_agent_keys() == ["orchestrator"]

    control._on_select_all(True)
    assert all(control.selected_agents[k] for k in FOUR)


@pytest.mark.parametrize("lock", ["started", "graph"])
def test_select_all_does_nothing_where_the_checkboxes_are_locked(lock, monkeypatch):
    """체크박스가 잠기는 경우(이미 시작된 대화, 그래프 토론)에는 일괄 선택도 바꾸지 않습니다."""
    control = _control_with_four_agents(monkeypatch)
    control.selected_agents["coder"] = False
    if lock == "started":
        control.personas_locked = True
    else:
        control._graph_agent_keys = set()
    control._on_select_all(True)
    assert control.selected_agents["coder"] is False
    control._on_select_all(False)
    assert control.selected_agents["architect"] is True
