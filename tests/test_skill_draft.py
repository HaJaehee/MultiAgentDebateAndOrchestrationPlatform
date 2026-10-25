"""토론 결과 기반 스킬 초안 자동 생성 테스트 (app/orchestration/skill_draft.py, v1.5).

1. 스킬 생성 검증: 중복 생성 방지, 명명 규칙, 프론트매터 및 본문 유효성 검증.
2. 초안 파싱 검증: 마크다운 코드블록 제거, 헤더 추출 및 식별자 보정.
3. 초안 작성 검증: 오케스트레이터의 턴 컨텍스트 분석 및 결론 부재 턴에 대한 예외 처리.
4. 스킬 저장 시 기본 비활성화(conf.json의 skills.disabled) 상태 등록 검증.
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.agents import skills as skills_module
from app.agents.skills import SkillError, write_skill
from app.database.models import SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.orchestration import skill_draft
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.skill_draft import SKILL_DRAFT_MARKER, SkillDraft, draft_skill, parse_draft, save_draft
from tests.fake_llm import FakeLLMCaller

DRAFT = """```markdown
초안입니다:
---
name: order-cache-rules
description: 주문 조회 캐시를 설계하거나 고칠 때 씁니다.
---

# 주문 조회 캐시 규칙

- 캐시는 Redis 에 둔다 (여러 인스턴스가 같은 값을 봐야 하므로).
- 확인할 것: 장애 시 TTL.
```"""


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills_module, "skills_root", lambda config=None: root)
    return root


def test_write_skill_creates_a_new_folder_only(skills_dir):
    text = SkillDraft("team-rules", "언제 쓰는지", "- 규칙").text()
    path = write_skill("team-rules", text)
    assert path == skills_dir / "team-rules" / "SKILL.md" and path.read_text(encoding="utf-8").endswith("- 규칙\n")
    with pytest.raises(SkillError, match="이미 있습니다"):
        write_skill("team-rules", text)
    for name, body in (("bad name", text), ("other", text),
                       ("no-desc", SkillDraft("no-desc", " ", "- 규칙").text()),
                       ("no-body", SkillDraft("no-body", "설명", " ").text())):
        with pytest.raises(SkillError):
            write_skill(name, body)
    assert sorted(p.name for p in skills_dir.iterdir()) == ["team-rules"], "실패한 것은 폴더를 남기지 않습니다"


def test_parse_draft_cleans_the_answer():
    draft = parse_draft(DRAFT)
    assert draft.name == "order-cache-rules" and draft.description.startswith("주문 조회 캐시를")
    assert draft.body.startswith("# 주문 조회 캐시 규칙") and "확인할 것" in draft.body
    odd = parse_draft("---\nname: 한글 이름\ndescription: d\n---\n\n본문")
    assert odd.name.startswith("debate-rules-") and odd.body == "본문"
    bare = parse_draft("머리말 없는 본문")
    assert bare.body == "머리말 없는 본문" and bare.description == ""
    assert SkillDraft("x", "여러\n줄  설명", "b").text().splitlines()[2] == "description: 여러 줄 설명"


class Drafting(FakeLLMCaller):
    def __init__(self):
        super().__init__()
        self.draft_prompts = []

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        if SKILL_DRAFT_MARKER in last:
            self.draft_prompts.append((agent, last))
            return DRAFT
        return super()._reply_for(agent, messages)


@pytest.mark.asyncio
async def test_a_turn_is_drafted_into_a_skill_and_saved_switched_off(skills_dir, tmp_path, monkeypatch):
    await init_db("sqlite+aiosqlite:///:memory:")
    sid = f"skill-{uuid.uuid4().hex[:8]}"
    async with get_session_factory()() as db:
        db.add(SessionModel(id=sid, title="스킬", strategy="sequential_debate", max_rounds=1,
                            active_agents=["orchestrator", "architect", "critic"]))
        await db.commit()
    caller = Drafting()
    await OrchestratorEngine(llm_caller=caller).run_turn(session_id=sid, user_prompt="캐시 설계를 정해 줘")
    async with get_session_factory()() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        draft = await draft_skill(db, sid, turn.id, llm_caller=caller)
        with pytest.raises(ValueError):
            await draft_skill(db, sid, "없는-턴", llm_caller=caller)

    writer, prompt = caller.draft_prompts[0]
    assert writer.key == "orchestrator" and writer.allowed_mcp_servers == [] and not writer.sequential_thinking.enabled
    assert "캐시 설계를 정해 줘" in prompt and "최종 합의 요약" in prompt and "## 결정 사항" in prompt
    assert "name: mermaid-diagrams" in prompt, "형식 예시로 기본 스킬을 보여 줍니다"
    assert draft.name == "order-cache-rules"

    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"skills": {"disabled": ["old"]}}), encoding="utf-8")
    monkeypatch.setattr(skill_draft, "active_config_path", lambda: conf)
    path = save_draft(draft)
    assert path.parent == skills_dir / "order-cache-rules"
    assert json.loads(conf.read_text(encoding="utf-8"))["skills"]["disabled"] == ["old", "order-cache-rules"]
