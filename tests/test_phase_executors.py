from __future__ import annotations

from app.agents.base import Agent
from app.orchestration.artifacts import (
    CODE_LANGUAGES,
    extract_code_blocks,
    find_mermaid_blocks,
    format_roster,
    normalize_mermaid,
    synthesis_has_content,
)
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.phases.debate import DebatePhaseExecutor
from app.orchestration.phases.graph import parse_gate_decision
from app.orchestration.graph import GraphTurnError
from app.orchestration.phases.planning import PlanningPhaseExecutor
from app.orchestration.phases.routing import parse_speaker_selection
from app.orchestration.phases.synthesis import SynthesisPhaseExecutor
from app.orchestration.state import DebateMessage, DebateState


def _dummy_agent(key: str = "architect", name: str = "Architect") -> Agent:
    return Agent(
        key=key,
        name=name,
        role="System Architect",
        system_prompt="Design scalable systems.",
        model="gpt-4o",
    )


def test_engine_wires_phases_to_shared_services():
    """단계 실행기는 엔진을 되부르지 않고, 엔진이 만든 서비스를 함께 씁니다."""
    engine = OrchestratorEngine()
    assert isinstance(engine.planning_phase, PlanningPhaseExecutor)
    assert isinstance(engine.debate_phase, DebatePhaseExecutor)
    assert isinstance(engine.synthesis_phase, SynthesisPhaseExecutor)
    for phase in (engine.planning_phase, engine.debate_phase, engine.synthesis_phase):
        assert phase.ctx is engine.ctx
        assert phase.recorder is engine.recorder
        assert phase.speech is engine.speech
        assert phase.conversation is engine.conversation
    # 실행 중에 갈아 끼운 호출기(MADOWarGame 의 대역)를 모든 단계가 봅니다.
    caller = object()
    engine.llm_caller = caller
    assert engine.ctx.llm_caller is caller


def test_artifacts_module_functions():
    roster_text = format_roster([_dummy_agent("arch", "Architect")])
    assert "Architect" in roster_text
    assert "System Architect" in roster_text

    md = "Here is code:\n```python\nprint('hi')\n```\nand diagram:\n```mermaid\ngraph TD\n  A[Start (1)] --> B\n```"
    blocks = extract_code_blocks(md)
    assert len(blocks) == 2
    assert blocks[0]["language"] == "python"
    assert blocks[1]["language"] == "mermaid"

    m_blocks = find_mermaid_blocks(md)
    assert len(m_blocks) == 1
    normalized = normalize_mermaid(m_blocks[0]["code"])
    assert '"Start (1)"' in normalized
    assert "python" in CODE_LANGUAGES
    assert synthesis_has_content("Valid conclusion report") is True
    assert synthesis_has_content("   ") is False


def test_debate_phase_parse_speaker_selection():
    engine = OrchestratorEngine()
    candidates = [_dummy_agent("arch", "Architect"), _dummy_agent("sec", "Security")]

    selected, reason = parse_speaker_selection(
        '{"speakers": ["sec"], "stop": false, "reason": "Need security review"}',
        candidates,
    )
    assert [a.key for a in selected] == ["sec"]
    assert reason == "Need security review"

    stopped_selected, stopped_reason = parse_speaker_selection(
        '{"speakers": [], "stop": true, "reason": "All agreed"}',
        candidates,
    )
    assert stopped_selected == []
    assert stopped_reason == "All agreed"


def test_synthesis_phase_extract_artifacts():
    engine = OrchestratorEngine()
    state = DebateState(
        session_id="sess-1",
        user_prompt="Build API",
        strategy="dynamic",
        max_rounds=2,
        current_round=1,
        active_agent_keys=["arch"],
    )
    state.messages.append(
        DebateMessage(
            speaker="Architect (System Architect)",
            sender_key="arch",
            sender_name="Architect",
            sender_role="System Architect",
            content="Here is implementation:\n```python\ndef api():\n    return 200\n```",
            round_number=1,
        )
    )
    synth_md = "## Conclusion\nAgreed.\n```mermaid\ngraph TD\n  A --> B\n```"
    artifacts = engine.synthesis_phase.extract_artifacts_from_synthesis(
        "sess-1", synth_md, state, synthesis_failed=False
    )
    types = [a.artifact_type for a in artifacts]
    assert "markdown" in types
    assert "mermaid" in types
    assert "code" in types
    assert "json" in types


def test_parse_gate_decision():
    decision = parse_gate_decision('{"decision": "yes", "reason": "Looks good"}')
    assert decision == ("yes", "Looks good")
    assert issubclass(GraphTurnError, Exception)
