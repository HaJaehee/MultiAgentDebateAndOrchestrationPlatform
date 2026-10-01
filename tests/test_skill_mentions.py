"""입력창의 `@전문가 @스킬` — 그 전문가가 그 스킬을 쓰도록 지정합니다.

* 스킬 언급은 **같은 줄에서 바로 앞에 언급한 전문가**에게 갑니다. 앞에 없으면 그 줄의 뒤 첫째,
  그 줄에 아무도 없으면 앞 줄들에서 가장 가까운 전문가. 그래도 없으면 지정하지 않고 알립니다.
* 지정은 **이번 턴에만** 겁니다. 그 전문가의 `allowed_skills` 밖이어도 그 턴의 발언에서는 주고,
  대화에 고정된 설정은 바꾸지 않습니다. 꺼진 스킬은 지정해도 주지 않습니다 (켜기·끄기가 앞섭니다).
* 지정한 스킬은 모델에게 맡기지 않고 **발언 첫머리에 호스트가 불러 둡니다** — 도구 카드로 남습니다.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents import skills as skills_module
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.skills import (
    DESIGNATED_SKILL_NOTE,
    LOAD_SKILL_TOOL,
    offered_skills,
    skill_tools_for,
    with_designated_skills,
)
from app.config import RootConfig
from app.database.models import TurnModel
from app.database.session import get_session_factory
from app.orchestration.control import TurnControl
from app.orchestration.engine import clean_skill_designations
from app.workspace_files import (
    REFERENCE_MARKER,
    MentionAgent,
    MentionSkill,
    expand_mentions,
    scan_workspace,
    skills_for_mentions,
    suggest_mentions,
)
from tests.fake_llm import FakeLLMCaller
from tests.test_interaction import HookedCaller
from tests.test_resilience import _engine, _make_session

AGENTS = [
    MentionAgent("architect", "System Architect", "Architecture", True),
    MentionAgent("coder", "Senior Python Engineer", "Implementation", True),
    MentionAgent("critic", "Quality Critic", "Review", False),
]
SKILLS = [
    MentionSkill("mermaid-diagrams", "Mermaid 다이어그램을 그릴 때 씁니다.", True),
    MentionSkill("csv-profile", "CSV 를 요약할 때 씁니다.", True),
    MentionSkill("report-writing", "보고서를 쓸 때 씁니다.", False),
]


def _expand(text: str, ws: Path):
    return expand_mentions(text, ws, AGENTS, SKILLS)


# ================================================================ 짝짓기


def test_a_skill_goes_to_the_specialist_just_before_it_on_the_same_line(tmp_path: Path):
    _, report = _expand(
        "@architect 는 @mermaid-diagrams 로 그리고, @coder 는 @csv-profile 로 분석해 주세요", tmp_path
    )
    assert report.skill_designations == {"architect": ["mermaid-diagrams"], "coder": ["csv-profile"]}
    assert not report.warnings()


def test_a_skill_written_first_goes_to_the_next_specialist_on_the_line(tmp_path: Path):
    _, report = _expand('@mermaid-diagrams 로 @"System Architect" 가 그려 주세요', tmp_path)
    assert report.skill_designations == {"architect": ["mermaid-diagrams"]}


def test_a_skill_on_its_own_line_goes_to_the_nearest_specialist_above(tmp_path: Path):
    text = "@coder 는 구현을 맡고\n@architect 는 구조를 맡아 주세요.\n- @mermaid-diagrams 로 그릴 것"
    _, report = _expand(text, tmp_path)
    assert report.skill_designations == {"architect": ["mermaid-diagrams"]}


def test_a_skill_without_any_specialist_is_not_designated_and_says_so(tmp_path: Path):
    out, report = _expand("@mermaid-diagrams 로 그려 주세요", tmp_path)
    assert report.skill_designations == {}
    assert REFERENCE_MARKER not in out
    assert report.unbound_skills == ["mermaid-diagrams"]
    assert any("함께 언급하지 않아" in w for w in report.warnings())


def test_a_disabled_skill_is_refused_with_a_warning(tmp_path: Path):
    _, report = _expand("@architect @report-writing 으로 보고서", tmp_path)
    assert report.skill_designations == {}
    assert report.unusable_skills == ["report-writing"]
    assert any("쓸 수 없는 스킬" in w for w in report.warnings())


def test_a_skill_for_a_specialist_outside_the_debate_is_dropped_with_that_specialist(tmp_path: Path):
    _, report = _expand('@"Quality Critic" 는 @csv-profile 로', tmp_path)
    assert report.skill_designations == {}
    assert [a.key for a in report.inactive_agents] == ["critic"]
    assert not report.unbound_skills, "전문가가 있었으므로 '함께 언급하라' 가 아닙니다"


def test_skill_mentions_inside_code_are_not_designations(tmp_path: Path):
    _, report = _expand("@architect 참고: `@mermaid-diagrams`\n```\n@csv-profile\n```", tmp_path)
    assert report.skill_designations == {}
    assert not report.unbound_skills


def test_duplicates_collapse_and_two_skills_can_go_to_one_specialist(tmp_path: Path):
    _, report = _expand("@architect @mermaid-diagrams @csv-profile @mermaid-diagrams.", tmp_path)
    assert report.skill_designations == {"architect": ["mermaid-diagrams", "csv-profile"]}


def test_a_specialist_name_wins_over_a_skill_with_the_same_name(tmp_path: Path):
    skills = [MentionSkill("coder", "같은 이름", True)]
    _, report = expand_mentions("@coder 봐 주세요", tmp_path, AGENTS, skills)
    assert [a.key for a in report.agents] == ["coder"] and report.skill_designations == {}


def test_the_reference_block_tells_the_orchestrator_who_uses_which_skill(tmp_path: Path):
    text = "@architect 는 @mermaid-diagrams 로 그려 주세요"
    out, _ = _expand(text, tmp_path)
    block = out[len(text):]
    assert "지정한 스킬" in block
    assert "- System Architect → mermaid-diagrams" in block
    assert "- System Architect (Architecture)" in block, "지목한 전문가로도 실립니다"
    twice, _ = _expand(out, tmp_path)
    assert twice == out, "다시 보내도 블록이 겹치지 않습니다"


# ================================================================ 후보


def test_usable_skills_are_suggested_after_specialists_and_before_files(tmp_path: Path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    items = suggest_mentions("", scan_workspace(tmp_path).entries, AGENTS, skills=SKILLS)
    kinds = [i.kind for i in items]
    assert kinds[:4] == ["agent", "agent", "skill", "skill"]
    assert kinds.index("file") > kinds.index("skill")
    assert "report-writing" not in [i.label for i in items], "꺼진 스킬은 후보에 없습니다"
    skill = next(i for i in items if i.label == "mermaid-diagrams")
    assert skill.insert == "@mermaid-diagrams" and "Mermaid" in skill.detail


def test_skills_match_by_name_or_description(tmp_path: Path):
    labels = [i.label for i in suggest_mentions("CSV", [], AGENTS, skills=SKILLS)]
    assert labels == ["csv-profile"]


def test_scanned_skills_become_candidates_with_their_state():
    scanned = [SimpleNamespace(name="a", description="d", usable=True),
               SimpleNamespace(name="b", description="", usable=False)]
    assert skills_for_mentions(scanned) == [MentionSkill("a", "d", True), MentionSkill("b", "", False)]


def test_the_popup_shows_a_skill_icon():
    from app.ui.mention_input import MENTION_JS
    from app.ui.theme import CUSTOM_CSS

    assert "skill: 'menu_book'" in MENTION_JS
    assert ".mado-mention-icon.mado-mention-skill" in CUSTOM_CSS


def test_the_main_screen_passes_designations_with_new_turns_and_interjections():
    root = Path(__file__).resolve().parents[1]
    src = (root / "app" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "skill_designations=skill_designations" in src
    assert src.count("skill_designations=skill_designations") == 2, "새 턴과 개입 모두"


# ================================================================ 이번 턴에만 주는 사본


def _agent(*skills: str, key: str = "architect") -> Agent:
    return Agent(key=key, name="A", role="R", model="openai/gpt-4o", api_key="k",
                 allowed_skills=list(skills))


def test_designation_adds_skills_to_a_copy_only():
    agent = _agent("a")
    copy = with_designated_skills(agent, ["b", "a", "b"])
    assert copy.allowed_skills == ["a", "b"]
    assert agent.allowed_skills == ["a"], "대화에 고정된 설정은 그대로입니다"
    assert with_designated_skills(agent, ["a"]) is agent
    assert with_designated_skills(agent, None) is agent


def test_only_participating_specialists_and_valid_names_are_kept():
    cleaned = clean_skill_designations(
        {"architect": ["mermaid-diagrams", "../x", "mermaid-diagrams"], "orchestrator": ["a"],
         "critic": ["a"], "coder": "csv-profile"},
        ["orchestrator", "architect", "coder"],
    )
    assert cleaned == {"architect": ["mermaid-diagrams"]}


def test_interjection_designations_wait_in_the_mailbox_with_the_note():
    control = TurnControl()
    assert control.add_note("이번엔 그림으로", {"architect": ["a"]})
    assert control.add_note("표도", {"architect": ["a", "b"], "coder": ["c"]})
    assert not control.add_note("   ", {"coder": ["x"]}), "빈 메모는 지정도 싣지 않습니다"
    assert control.drain_skill_designations() == {"architect": ["a", "b"], "coder": ["c"]}
    assert control.drain_skill_designations() == {}


# ================================================================ 발언 첫머리에 불러 두기


def _write_skill(root: Path, name: str, body: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 할 때 씁니다.\n---\n\n{body}\n", encoding="utf-8"
    )


@pytest.fixture()
def skill_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    state = {"disabled": []}

    def config():
        return RootConfig.model_validate({
            "agents": {"orchestrator": {"name": "O", "role": "R"}},
            "skills": {"dir": str(root), "disabled": list(state["disabled"])},
        })

    monkeypatch.setattr(skills_module, "get_config", config)
    return SimpleNamespace(root=root, state=state)


class _NoTools:
    workspace = None

    def get_openai_tools_for_servers(self, servers):
        return []

    async def execute_tool(self, *args, **kwargs):
        raise AssertionError("스킬 도구는 MCP 로 가지 않습니다")


def _msg(content="", tool_calls=None):
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=None,
        model_dump=lambda: {"role": "assistant", "content": content},
    )


async def _speak(agent: Agent, preload):
    sent = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return SimpleNamespace(choices=[SimpleNamespace(message=_msg("지침대로 답합니다."), finish_reason="stop")])

    logs = []
    with patch("litellm.acompletion", side_effect=fake_acompletion):
        text, tool_logs = await LLMCaller(mcp_manager=_NoTools()).call_agent(
            agent, [{"role": "user", "content": "구조도를 그려 주세요"}],
            on_tool_call=logs.append, preload_skills=preload,
        )
    return text, tool_logs, logs, sent


@pytest.mark.asyncio
async def test_a_designated_skill_is_loaded_before_the_first_request(skill_root):
    _write_skill(skill_root.root, "mermaid-diagrams", "subgraph 제목은 대괄호로 씁니다.")
    # allowed_skills 에 없던 스킬 — 지정이 이번 발언의 사본에만 더합니다.
    agent = with_designated_skills(_agent(), ["mermaid-diagrams"])

    text, tool_logs, logs, sent = await _speak(agent, ["mermaid-diagrams"])

    assert text == "지침대로 답합니다."
    assert len(sent) == 1, "불러 두기는 LLM 호출이 아닙니다"
    messages = sent[0]["messages"]
    at = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
    assert [m["role"] for m in messages[:at]] == ["system", "user"], "요청 바로 뒤, 첫 판 전에 들어갑니다"
    call, result = messages[at], messages[at + 1]
    assert call["role"] == "assistant" and call["tool_calls"][0]["function"]["name"] == LOAD_SKILL_TOOL
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {"skill": "mermaid-diagrams"}
    assert result["role"] == "tool" and result["tool_call_id"] == call["tool_calls"][0]["id"]
    assert result["content"].startswith(DESIGNATED_SKILL_NOTE)
    assert "subgraph 제목은 대괄호로 씁니다." in result["content"]
    assert offered_skills(sent[0]["tools"]) == ["mermaid-diagrams"], "모델도 다시 부를 수 있습니다"
    assert tool_logs[0]["tool_name"] == LOAD_SKILL_TOOL and tool_logs[0]["status"] == "success"
    assert logs == tool_logs, "화면에 도구 카드로 남습니다"


@pytest.mark.asyncio
async def test_a_skill_turned_off_after_sending_is_not_loaded(skill_root):
    _write_skill(skill_root.root, "mermaid-diagrams", "본문")
    _write_skill(skill_root.root, "csv-profile", "본문")
    skill_root.state["disabled"] = ["mermaid-diagrams"]
    agent = with_designated_skills(_agent(), ["mermaid-diagrams", "csv-profile"])

    _, tool_logs, _, sent = await _speak(agent, ["mermaid-diagrams", "csv-profile"])

    assert [log["arguments"] for log in tool_logs] == [{"skill": "csv-profile"}]
    assert offered_skills(skill_tools_for(agent)) == ["csv-profile"]
    assert not any(
        "mermaid-diagrams" in json.dumps(m.get("tool_calls") or [], ensure_ascii=False)
        for m in sent[0]["messages"]
    ), "내놓지 않은 도구를 부른 기록은 만들지 않습니다"


@pytest.mark.asyncio
async def test_without_designation_nothing_is_preloaded(skill_root):
    _write_skill(skill_root.root, "mermaid-diagrams", "본문")
    _, tool_logs, _, sent = await _speak(_agent("mermaid-diagrams"), [])
    assert tool_logs == []
    assert sent[0]["messages"][-1]["role"] == "user"


# ================================================================ 엔진: 이번 턴, 그 전문가에게만


def _preloads_of(caller: FakeLLMCaller, key: str):
    return [(skills, allowed) for k, skills, allowed in caller.preloads if k == key]


@pytest.mark.asyncio
async def test_a_turn_preloads_the_skill_for_that_specialist_only_and_records_it():
    sid = await _make_session(max_rounds=2)
    caller = FakeLLMCaller()
    engine = _engine(llm_caller=caller)

    state = await engine.run_turn(
        session_id=sid, user_prompt="구조도를 그려 주세요",
        skill_designations={"architect": ["mermaid-diagrams"], "orchestrator": ["x"]},
    )

    architect = _preloads_of(caller, "architect")
    assert len(architect) == 2, "라운드마다 발언할 때마다 불러 둡니다"
    assert all(skills == ["mermaid-diagrams"] and "mermaid-diagrams" in allowed for skills, allowed in architect)
    assert all(skills == [] for skills, _ in _preloads_of(caller, "coder") + _preloads_of(caller, "orchestrator"))

    async with get_session_factory()() as db:
        turn = await db.get(TurnModel, state.turn_id)
    assert turn.config["skill_designations"] == {"architect": ["mermaid-diagrams"]}, "끊겨도 이어 가도록"

    # 다음 턴에는 걸리지 않습니다.
    caller.preloads.clear()
    await engine.run_turn(session_id=sid, user_prompt="이어서 설명해 주세요")
    assert all(skills == [] for _, skills, _ in caller.preloads)
    assert all("mermaid-diagrams" not in allowed for _, _, allowed in caller.preloads)


@pytest.mark.asyncio
async def test_an_interjection_designates_a_skill_for_the_remaining_speeches():
    sid = await _make_session(max_rounds=1)
    control = TurnControl()
    caller = HookedCaller(
        after="architect",
        hook=lambda: control.add_note("@coder 는 @csv-profile 로", {"coder": ["csv-profile"]}),
    )
    state = await _engine(llm_caller=caller).run_turn(
        session_id=sid, user_prompt="데이터를 봐 주세요", control=control,
    )

    assert [skills for skills, _ in _preloads_of(caller, "architect")] == [[]], "이미 끝난 발언에는 없습니다"
    assert [skills for skills, _ in _preloads_of(caller, "coder")] == [["csv-profile"]]
    async with get_session_factory()() as db:
        turn = await db.get(TurnModel, state.turn_id)
    assert turn.config["skill_designations"] == {"coder": ["csv-profile"]}


@pytest.mark.asyncio
async def test_continuing_an_interrupted_turn_keeps_its_designations():
    import asyncio

    from sqlalchemy import select

    from app.orchestration import turns
    from tests.test_turn_resume import DB_URL, REQUEST, CrashingLLM, _session
    from tests.test_turn_resume import _engine as resume_engine

    sid = await _session()
    with pytest.raises(asyncio.CancelledError):
        await resume_engine(CrashingLLM(crash_at=("coder", 1))).run_turn(
            session_id=sid, user_prompt=REQUEST, skill_designations={"coder": ["csv-profile"]},
        )
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalars().one()
        await turns.mark_interrupted_turns(db)

    llm = CrashingLLM()
    await resume_engine(llm).resume_turn(session_id=sid, turn_id=turn.id, mode="continue")

    coder = _preloads_of(llm, "coder")
    assert coder and all(skills == ["csv-profile"] for skills, _ in coder), "턴 기록에서 다시 읽습니다"
    assert all(skills == [] for skills, _ in _preloads_of(llm, "architect"))
