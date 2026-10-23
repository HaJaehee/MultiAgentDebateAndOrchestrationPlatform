"""세션 카드의 'N Agents' — 해당 대화에 실제 참여하는 에이전트 수 검증.

카드는 `sessions.active_agents` 의 길이를 그대로 찍었습니다. 두 군데에서 틀렸습니다.

* 로스터에서 참여 여부를 변경해도 세션 목록이 다시 렌더링되지 않아, 세션 생성 시의 초기값(전원 활성화)이
  첫 발언 전까지 잔존하던 문제 (`app/ui/expert/page.py` 의 `on_config_changed`).
* 그래프 토론은 체크박스가 아닌 그래프 노드에 배치된 에이전트가 실제 참여자임에도 불구하고 체크된
  전체 인원이 표기되던 문제.

여기서는 세는 규칙을 고정합니다. 로스터 상단의 'N Agents Active' 뱃지도 동일한 수여야 합니다.
"""

import pytest

from app import graph_store
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import SessionModel
from app.orchestration.graph import graph_from_card_order
from app.ui.components import sidebar
from tests.roster_patch import patch_roster
from app.ui.components.roster.control import AgentRosterControl
from app.ui.components.sidebar import active_agent_count

FIVE = ["orchestrator", "architect", "coder", "critic", "researcher"]


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    """architect → coder 두 명만 놓인 그래프 `pair` 가 있는 그래프 폴더."""
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    graph_store.save_graph(graph_from_card_order("pair", "둘이서", ["architect", "coder"]))
    return tmp_path


def graph_session(graph_id: str, active=FIVE) -> SessionModel:
    return SessionModel(strategy="graph_debate", graph_id=graph_id, active_agents=list(active))


# ------------------------------------------------------------------ 체크박스로 정하는 전략

def test_card_counts_the_agents_left_on():
    session = SessionModel(strategy="sequential_debate", active_agents=["orchestrator", "coder"])
    assert active_agent_count(session) == 2


def test_card_of_a_session_with_no_roster_shows_zero():
    assert active_agent_count(SessionModel(active_agents=[])) == 0
    assert active_agent_count(SessionModel(active_agents=None)) == 0


def test_a_graph_picked_earlier_does_not_count_once_the_strategy_is_not_graph(graphs):
    """전략을 되돌리면 `graph_id` 는 남지만 참여자는 다시 체크박스가 정합니다."""
    session = SessionModel(strategy="sequential_debate", graph_id="pair", active_agents=FIVE)
    assert active_agent_count(session) == 5


# ------------------------------------------------------------------ 그래프 토론

def test_graph_debate_counts_the_agents_on_the_graph_not_the_checkboxes(graphs):
    """다섯을 체크해 두었어도 그래프에 둘만 놓았으면 오케스트레이터까지 셋입니다."""
    assert active_agent_count(graph_session("pair")) == 3
    assert active_agent_count(graph_session("pair", active=["orchestrator"])) == 3


@pytest.mark.parametrize("graph_id", ["", "missing", "broken"])
def test_graph_debate_without_a_usable_graph_leaves_only_the_orchestrator(graphs, graph_id):
    """고르지 않았거나 읽지 못하는 그래프로는 아무도 참여하지 못합니다. 목록은 그래도 그려져야 합니다."""
    (graphs / "broken.json").write_text("{ not json", encoding="utf-8")
    assert active_agent_count(graph_session(graph_id)) == 1


def test_sessions_sharing_a_graph_read_the_file_once(graphs, monkeypatch):
    """목록은 자주 다시 그려집니다. 같은 그래프를 쓰는 대화마다 파일을 읽지 않습니다."""
    reads = []

    def counting(graph_id):
        reads.append(graph_id)
        return graph_store.graph_agent_keys(graph_id)

    monkeypatch.setattr(sidebar, "graph_agent_keys", counting)
    graph_keys = {}
    counts = [active_agent_count(graph_session("pair"), graph_keys) for _ in range(4)]

    assert counts == [3, 3, 3, 3]
    assert reads == ["pair"]


# ------------------------------------------------------------------ 로스터 뱃지

class FakeBadge:
    text = ""

    def set_text(self, text: str) -> None:
        self.text = text


def roster_control(monkeypatch) -> AgentRosterControl:
    """conf.json 없이도 돌도록 풀을 직접 넣습니다."""
    pool = AgentPool({
        key: AgentConfig(name=key.title(), role=key, model="openai/gpt-4o-mini", api_key="sk-test")
        for key in FIVE
    })
    patch_roster(monkeypatch, "get_agent_pool", lambda: pool)
    control = AgentRosterControl()
    control.summary_badge = FakeBadge()
    return control


def test_roster_badge_shows_the_same_number_as_the_card(graphs, monkeypatch):
    control = roster_control(monkeypatch)
    control.selected_agents["critic"] = False

    control._update_summary_badge()
    checked = [key for key in FIVE if key != "critic"]
    assert control.summary_badge.text == "4 Agents Active"
    assert active_agent_count(SessionModel(active_agents=checked)) == 4

    # 그래프 토론으로 바꾸면 둘 다 그래프를 봅니다. 꺼 둔 critic 은 수와 무관합니다.
    control._graph_agent_keys = set(graph_store.graph_agent_keys("pair"))
    control._update_summary_badge()
    assert control.summary_badge.text == "3 Agents Active"
    assert active_agent_count(graph_session("pair", active=checked)) == 3
