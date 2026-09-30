"""체험 템플릿 (app/trial/templates.py).

템플릿은 "시작하기 전의 대화 한 벌" 입니다. 여기서 지키려는 것:

1. 저장소에 싣는 공식 템플릿이 예시 설정으로 전부 읽힌다.
2. 깨진 템플릿은 이유와 함께 거절된다 (사회자 없음, 키 중복, 없는 칸을 가리키는 틀, 그래프 전략).
3. 요청문 틀은 칸 값만 끼우고, 값 안의 중괄호는 건드리지 않는다.
4. 참여자 스냅샷은 기반 에이전트의 모델을 빌리되 **도구를 전부 뗀다.**
5. 템플릿으로 만든 잠긴 대화를 엔진이 그대로 끝까지 돌린다 — conf.json 에 없는 참여자 키로도.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.pool import AgentPool
from app.config import AgentConfig, load_config
from app.database.models import MessageModel, SessionAgentModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.trial import models as trial_models  # noqa: F401 - 테이블 등록
from app.trial.models import TrialSessionModel
from app.trial.templates import (
    TemplateError,
    create_trial_session,
    decode_text_upload,
    input_problems,
    load_official_templates,
    parse_template,
    participant_snapshot,
    render_prompt,
    role_library,
    session_title,
)
from tests.fake_llm import FakeLLMCaller

ROOT = Path(__file__).resolve().parents[1]
DB_URL = "sqlite+aiosqlite:///:memory:"


def _pool() -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(
            name="Master Orchestrator", role="Moderator", model="openai/gpt-4o", api_key="sk-test",
            temperature=0.2, allowed_mcp_servers=["filesystem", "sandbox"], system_prompt="중재하세요.",
            allowed_skills=["mermaid-diagrams"],
        ),
        "critic": AgentConfig(
            name="Critic", role="Reviewer", model="openai/gpt-4o-mini", api_key="sk-test",
            allowed_mcp_servers=["sandbox", "git"], system_prompt="비판하세요.",
            allowed_skills=["mermaid-diagrams"],
        ),
    })


def _template(**overrides):
    data = {
        "id": "demo",
        "title": "데모 검토",
        "strategy": "sequential_debate",
        "max_rounds": 1,
        "participants": [
            {"key": "reader", "name": "바쁜 독자", "role": "첫 쪽만", "system_prompt": ["짧게", "봅니다"]},
            {"key": "orchestrator", "name": "사회자", "system_prompt": "정리합니다"},
            {"key": "critic", "name": "논리 검토자", "stance": "critic"},
        ],
        "inputs": [
            {"id": "document", "label": "문서", "allow_file": True},
            {"id": "focus", "label": "볼 점", "kind": "text", "required": False},
        ],
        "prompt": ["검토해 주세요.", "[볼 점] {focus}", "[문서]", "{document}"],
        "example": {"document": ["첫 줄", "둘째 줄"]},
    }
    data.update(overrides)
    return parse_template(data)


# ------------------------------------------------------------------ 1. 공식 템플릿


def test_every_shipped_template_loads_with_the_example_config():
    cfg = load_config(ROOT / "conf.example.json")
    load = load_official_templates(ROOT / "trial_templates", AgentPool(cfg.agents))
    assert load.errors == {}
    assert len(load.templates) >= 5
    for template in load.templates.values():
        assert template.participants[0].key == "orchestrator"
        # 예시는 시작 양식을 그대로 통과해야 합니다 ("예시로 채우기" 뒤 바로 시작).
        assert input_problems(template, template.example, 30000) == {}, template.id


def test_the_trial_is_off_in_the_example_config():
    assert load_config(ROOT / "conf.example.json").trial.enabled is False


def test_a_broken_file_is_skipped_with_its_reason(tmp_path):
    (tmp_path / "a-good.json").write_text(
        (ROOT / "trial_templates" / "meeting-notes.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "b-bad.json").write_text('{"title": "x"', encoding="utf-8")
    load = load_official_templates(tmp_path, _pool())
    assert list(load.templates) == ["meeting-notes"]
    assert "b-bad.json" in load.errors


def test_a_template_whose_base_agent_is_missing_is_refused(tmp_path):
    (tmp_path / "t.json").write_text(
        '{"title": "x", "participants": [{"key": "orchestrator", "name": "O"},'
        ' {"key": "a", "base": "coder", "name": "A"}],'
        ' "inputs": [{"id": "q", "label": "Q"}], "prompt": "{q}"}',
        encoding="utf-8",
    )
    load = load_official_templates(tmp_path, _pool())
    assert load.templates == {}
    assert "coder" in load.errors["t.json"]


# ------------------------------------------------------------------ 2. 검사


@pytest.mark.parametrize("overrides, needle", [
    ({"participants": [{"key": "a", "name": "A"}, {"key": "b", "name": "B"}]}, "orchestrator"),
    ({"participants": [{"key": "orchestrator", "name": "O"}]}, "한 명 이상"),
    ({"participants": [{"key": "orchestrator", "name": "O"}, {"key": "a", "name": "A"},
                       {"key": "a", "name": "A2"}]}, "겹칩니다"),
    ({"prompt": "{document} {missing}"}, "missing"),
    ({"strategy": "graph_debate"}, "graph_debate"),
    ({"inputs": []}, "입력 칸"),
])
def test_broken_templates_are_refused_with_a_reason(overrides, needle):
    with pytest.raises(TemplateError) as exc:
        _template(**overrides)
    assert needle in str(exc.value)


def test_the_orchestrator_is_moved_first_and_multiline_fields_are_joined():
    template = _template()
    assert [p.key for p in template.participants] == ["orchestrator", "reader", "critic"]
    assert template.participants[1].system_prompt == "짧게\n봅니다"
    assert template.example["document"] == "첫 줄\n둘째 줄"


# ------------------------------------------------------------------ 3. 시작 양식


def test_the_prompt_fills_inputs_and_marks_empty_optional_fields():
    template = _template()
    text = render_prompt(template, {"document": "본문 {x} 그대로", "focus": "  "})
    assert "[볼 점] (없음)" in text
    assert "본문 {x} 그대로" in text, "값 안의 중괄호는 틀이 아닙니다"


def test_required_and_too_long_inputs_are_reported_per_field():
    template = _template()
    problems = input_problems(template, {"document": "", "focus": "x" * 11}, max_chars=10)
    assert set(problems) == {"document", "focus"}
    assert input_problems(template, {"document": "짧은 글"}, max_chars=10) == {}


def test_the_session_title_uses_the_first_filled_input():
    template = _template()
    assert session_title(template, {"document": "  회의실\n예약 개편  "}) == "데모 검토 · 회의실 예약 개편"
    assert session_title(template, {}) == "데모 검토"


def test_text_uploads_accept_utf8_and_cp949_and_refuse_the_rest():
    assert decode_text_upload("a.txt", "가나다\r\n".encode("utf-8-sig"), 1000) == "가나다\n"
    assert decode_text_upload("메모.MD", "회의록".encode("cp949"), 1000) == "회의록"
    with pytest.raises(TemplateError):
        decode_text_upload("report.docx", b"PK\x03\x04", 1000)
    with pytest.raises(TemplateError):
        decode_text_upload("a.txt", b"x" * 2000, 1000)
    with pytest.raises(TemplateError):
        decode_text_upload("a.txt", b"abc\x00def", 1000)


def test_the_role_library_lists_specialists_once():
    template = _template()
    names = [p.name for p in role_library([template, template])]
    assert names == ["바쁜 독자", "논리 검토자"]


# ------------------------------------------------------------------ 4. 스냅샷


def test_a_participant_borrows_the_base_model_but_loses_every_tool():
    template = _template()
    pool = _pool()
    reader, critic = template.participants[1], template.participants[2]

    snap = participant_snapshot(reader, pool, priority=20)
    # conf.json 에 없는 키는 오케스트레이터의 운영 설정을 빌립니다.
    assert snap["model"] == "openai/gpt-4o"
    assert snap["temperature"] == 0.2
    assert snap["name"] == "바쁜 독자" and snap["role"] == "첫 쪽만"
    assert snap["system_prompt"] == "짧게\n봅니다"
    assert snap["allowed_mcp_servers"] == []
    assert snap["allowed_skills"] == [], "스킬도 도구처럼 뗍니다"
    assert snap["debate_priority"] == 20

    snap = participant_snapshot(critic, pool, priority=30)
    assert snap["model"] == "openai/gpt-4o-mini", "같은 키가 conf.json 에 있으면 그 에이전트를 빌립니다"
    assert snap["allowed_mcp_servers"] == []
    assert snap["allowed_skills"] == []
    assert snap["debate_stance"] == "critic"
    # 인격을 비워 두면 기반 에이전트의 것을 씁니다.
    assert snap["system_prompt"] == "비판하세요."


# ------------------------------------------------------------------ 5. 엔진


class RecordingLLM(FakeLLMCaller):
    def __init__(self):
        super().__init__()
        self.agents = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.agents.append(agent)
        return await super().call_agent(agent, messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_a_template_session_runs_to_the_end_with_only_its_participants_and_no_tools():
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    template = _template(max_rounds=1)
    pool = _pool()
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(trial_models.TrialUserModel(id=user_id, name="홍길동", name_key=f"홍길동-{user_id}", pin_hash="x"))
        await db.commit()
        sid = await create_trial_session(
            db, user_id=user_id, template=template, template_ref="t:demo",
            values={"document": "검토할 글"}, pool=pool,
        )

    async with factory() as db:
        session = await db.get(SessionModel, sid)
        assert session.personas_locked is True
        assert session.tool_mode == "read_only"
        assert session.active_agents == ["orchestrator", "reader", "critic"]
        rows = (await db.execute(select(SessionAgentModel).where(SessionAgentModel.session_id == sid))).scalars().all()
        assert {r.agent_key for r in rows} == {"orchestrator", "reader", "critic"}
        owner = await db.get(TrialSessionModel, sid)
        assert owner.user_id == user_id and owner.template_ref == "t:demo"

    llm = RecordingLLM()
    engine = OrchestratorEngine(agent_pool=pool, llm_caller=llm)
    state = await engine.run_turn(session_id=sid, user_prompt=render_prompt(template, {"document": "검토할 글"}))

    assert state.status == "completed"
    spoken = {a.key for a in llm.agents}
    assert spoken <= {"orchestrator", "reader", "critic"}
    assert {"reader", "critic"} <= spoken, "conf.json 에 없는 참여자 키도 발언해야 합니다"
    assert all(a.allowed_mcp_servers == [] for a in llm.agents), "체험 참여자에게 도구가 붙으면 안 됩니다"
    names = {a.key: a.name for a in llm.agents}
    assert names["orchestrator"] == "사회자" and names["reader"] == "바쁜 독자"

    async with factory() as db:
        final = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid, MessageModel.turn_started_at.is_not(None))
        )).scalars().all()
    assert len(final) == 1, "합성 발언이 하나 기록되어야 결과 화면이 그것을 보여 줍니다"
