"""라운드가 쌓여도 토론 결과가 사라지지 않는지, 오케스트레이터가 결론과 다이어그램까지만 쓰는지.

실제로 겪은 일: 턴이 여러 번 쌓인 세션에서 산출물 뷰어에 최근 턴 것만 남았습니다. 턴이
끝날 때 화면이 그 턴의 산출물로 뷰어를 통째로 바꿨기 때문입니다. 오케스트레이터가 빈 답을
낸 턴이면 남은 것이 빈 보고서 하나라, 토론 결과가 증발한 것처럼 보였습니다.

그 빈 답의 배경에는 합성 프롬프트가 "완전한 실행 가능 소스 코드" 까지 요구한 것이
있었습니다. 전문가들이 이미 쓴 코드를 합성에서 다시 출력해 응답 한도를 채웠고, 그 합성이
다음 턴 전사에 코드 덤프로 다시 들어가 컨텍스트가 빠르게 찼습니다.
"""

import pytest
from sqlalchemy import select

from app.database.models import ArtifactModel
from app.database.session import get_session_factory
from app.orchestration.engine import OrchestratorEngine, synthesis_has_content
from app.orchestration.state import DebateMessage, DebateState
from app.ui.components.artifact_viewer import default_tab_index, merge_artifacts
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _engine, _make_session

CODER_REPLY = """### 구현

```python
def cache_get(key: str) -> str:
    return store[key]
```
"""


class SynthesisCaller(FakeLLMCaller):
    """합성 답만 바꿉니다. 계획·지명 같은 오케스트레이터의 다른 발언은 그대로 둡니다."""

    def __init__(self, synthesis: str, **kwargs):
        super().__init__(**kwargs)
        self.synthesis = synthesis

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        if "최종 합의 보고서" in last:
            return self.synthesis
        return super()._reply_for(agent, messages)


async def _artifacts(sid):
    async with get_session_factory("sqlite+aiosqlite:///:memory:")() as db:
        rows = await db.execute(
            select(ArtifactModel).where(ArtifactModel.session_id == sid).order_by(ArtifactModel.created_at)
        )
        return rows.scalars().all()


# ------------------------------------------------------------------ 합성 프롬프트


def test_the_synthesis_prompt_asks_for_conclusion_and_diagram_only():
    state = DebateState(session_id="s", user_prompt="캐시 설계", active_agent_keys=["architect"])
    state.messages.append(DebateMessage(
        sender_key="coder", sender_name="Coder", sender_role="Impl", content=CODER_REPLY, round_number=1,
    ))
    prompt = OrchestratorEngine(agent_pool=_engine().agent_pool)._build_synthesis_prompt(state)[0]["content"]

    assert "최종 합의 보고서" in prompt, "가짜 LLM 과 기존 흐름이 합성 요청을 이 표시로 알아봅니다"
    assert "종합 Mermaid 다이어그램" in prompt
    assert "소스 코드를 다시 쓰거나 붙여 넣지 마세요" in prompt
    assert "완전한 실행 가능 소스 코드" not in prompt
    assert "```python" not in prompt.split("[Full Multi-Agent Debate Transcript]")[0]


def test_the_default_orchestrator_persona_no_longer_asks_for_code():
    import io
    from pathlib import Path

    text = io.open(Path(__file__).resolve().parents[1] / "conf.example.json", encoding="utf-8").read()
    assert "완전한 실행 가능 코드" not in text
    assert "소스 코드는 다시 쓰지 마세요" in text


# ------------------------------------------------------------------ 빈 합성


@pytest.mark.parametrize("text", [
    "",
    "   \n",
    "> ⚠️ 응답 한도(max_tokens=16,000)에 닿아 잘렸습니다.",
    "> **[Sequential Thinking]**\n>\n> 결론은 이렇다\n\n> ⚠️ 답이 사고 안에만 있었습니다.",
])
def test_notice_only_or_empty_synthesis_has_no_content(text):
    assert synthesis_has_content(text) is False


def test_a_real_conclusion_has_content():
    assert synthesis_has_content("## 결론\n\n캐시는 Redis 로 둡니다.\n\n> ⚠️ 한도에 닿았습니다.")
    assert synthesis_has_content("> **[Sequential Thinking]**\n>\n> 생각\n\n## 결론\n\nRedis 로 둡니다.")
    # 인용문으로 시작하는 진짜 답은 결론입니다.
    assert synthesis_has_content("> 요구사항: 캐시\n\nRedis 로 둡니다.")


@pytest.mark.asyncio
async def test_an_empty_synthesis_is_a_failure_that_keeps_the_specialists_conclusions():
    sid = await _make_session()
    engine = _engine(llm_caller=SynthesisCaller("", replies={"coder": CODER_REPLY}))

    state = await engine.run_turn(session_id=sid, user_prompt="캐시를 설계해 주세요.")

    report = next(a for a in state.artifacts if a.artifact_type == "markdown")
    assert "합성 실패 (빈 응답)" in report.title
    assert "최종 결론" not in report.title, "빈 보고서를 정상 결론의 제목으로 저장하면 안 됩니다"
    assert "## 전문가별 마지막 발언" in report.content
    assert "def cache_get" in report.content
    assert "System Architect" in report.content
    assert "보고서 완료" not in report.content
    assert state.is_consensus_reached is False
    assert state.error_message
    summary = next(a for a in state.artifacts if a.artifact_type == "json")
    assert '"synthesis_failed": true' in summary.content
    assert '"consensus_reached": false' in summary.content


# ------------------------------------------------------------------ 코드 산출물


@pytest.mark.asyncio
async def test_code_artifacts_come_from_this_turns_specialists_not_the_synthesis():
    sid = await _make_session()
    synthesis = "## 결론\n\n캐시를 둡니다.\n\n```python\nprint('합성이 쓴 코드')\n```\n"
    caller = SynthesisCaller(synthesis, replies={"coder": CODER_REPLY})
    engine = _engine(llm_caller=caller)

    state = await engine.run_turn(session_id=sid, user_prompt="캐시를 설계해 주세요.")
    codes = [a for a in state.artifacts if a.artifact_type == "code"]
    assert [c.content.strip() for c in codes] == ["def cache_get(key: str) -> str:\n    return store[key]"]
    assert "Senior Engineer" in codes[0].title

    # 다음 턴에 코더가 코드 없이 답하면, 이전 턴의 코드를 다시 올리지 않습니다.
    caller.replies = {}
    state2 = await engine.run_turn(session_id=sid, user_prompt="보안 관점을 보완해 주세요.")
    assert not [a for a in state2.artifacts if a.artifact_type == "code"]


# ------------------------------------------------------------------ 쌓이는 산출물


@pytest.mark.asyncio
async def test_every_turns_report_survives_including_an_empty_one():
    sid = await _make_session()
    caller = SynthesisCaller("## 결론 1\n\n첫 번째 결론입니다.")
    engine = _engine(llm_caller=caller)

    await engine.run_turn(session_id=sid, user_prompt="첫 요청")
    caller.synthesis = ""
    await engine.run_turn(session_id=sid, user_prompt="둘째 요청")

    reports = [a for a in await _artifacts(sid) if a.artifact_type == "markdown"]
    assert len(reports) == 2
    assert "첫 번째 결론입니다." in reports[0].content
    assert "합성 실패 (빈 응답)" in reports[1].title


def test_merge_appends_new_artifacts_and_skips_known_ids():
    old = [{"id": "a", "artifact_type": "markdown"}, {"id": "b", "artifact_type": "code"}]
    new = [{"id": "b", "artifact_type": "code"}, {"id": "c", "artifact_type": "markdown"}]
    merged = merge_artifacts(old, new)
    assert [a["id"] for a in merged] == ["a", "b", "c"]
    assert old == [{"id": "a", "artifact_type": "markdown"}, {"id": "b", "artifact_type": "code"}]


def test_the_latest_report_opens_first():
    arts = [
        {"artifact_type": "markdown"}, {"artifact_type": "code"},
        {"artifact_type": "markdown"}, {"artifact_type": "mermaid"}, {"artifact_type": "json"},
    ]
    assert default_tab_index(arts) == 2
    assert default_tab_index([{"artifact_type": "code"}, {"artifact_type": "json"}]) == 1
    assert default_tab_index([]) == 0


def test_the_screen_appends_instead_of_replacing():
    import io
    from pathlib import Path

    source = io.open(Path(__file__).resolve().parents[1] / "app" / "ui" / "app.py", encoding="utf-8").read()
    assert 'artifact_viewer.add_artifacts(event.get("artifacts", []))' in source
    assert 'formatted_arts = snapshot["artifacts"]' not in source
    assert 'merge_artifacts(formatted_arts, snapshot["artifacts"])' in source


# ------------------------------------------------------------------ v0.8.1 전문가 다이어그램 검사


@pytest.mark.asyncio
async def test_a_specialist_diagram_gets_the_mechanical_fix_without_repair():
    """합성에 다이어그램이 없으면 전문가 발언의 것을 씁니다. 이 경로는 LLM 수선을 거치지 않습니다."""
    sid = await _make_session()
    architect = (
        "### 설계\n\n```mermaid\nflowchart TD\n  Validator -->|실패 시| LLM\n"
        "  Note right of Validator: 최대 2회 재시도\n```\n"
    )
    engine = _engine(llm_caller=SynthesisCaller("## 결론\n\n다이어그램 없이 결론만.", replies={"architect": architect}))

    state = await engine.run_turn(session_id=sid, user_prompt="검증기를 설계해 주세요.")

    diagram = next(a for a in state.artifacts if a.artifact_type == "mermaid")
    assert "System Architect 제안" in diagram.title
    assert not diagram.title.startswith("⚠"), "기계적 수선으로 고쳐졌으니 표시하지 않습니다"
    assert "Note right of" not in diagram.content
    assert 'Validator -.- mado_note_1["최대 2회 재시도"]' in diagram.content


@pytest.mark.asyncio
async def test_a_specialist_diagram_that_still_fails_is_marked():
    sid = await _make_session()
    architect = "```mermaid\nflowchart LR\n  A --> B\n  loop 재시도\n  A --> C\n  end\n```"
    engine = _engine(llm_caller=SynthesisCaller("## 결론\n\n결론만.", replies={"architect": architect}))

    state = await engine.run_turn(session_id=sid, user_prompt="설계해 주세요.")

    diagram = next(a for a in state.artifacts if a.artifact_type == "mermaid")
    assert diagram.title.startswith("⚠ "), "탭을 열기 전에 문법 오류가 있다는 것을 보여야 합니다"
    assert "loop 재시도" in diagram.content, "지어낸 다이어그램으로 바꾸지 않고 원문을 둡니다"
