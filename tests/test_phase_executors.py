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
from app.orchestration.phases import (
    DebatePhaseExecutor,
    GraphTurnError,
    PlanningPhaseExecutor,
    SynthesisPhaseExecutor,
    parse_gate_decision,
)
from app.orchestration.state import DebateMessage, DebateState


def _dummy_agent(key: str = "architect", name: str = "Architect") -> Agent:
    return Agent(
        key=key,
        name=name,
        role="System Architect",
        system_prompt="Design scalable systems.",
        model="gpt-4o",
    )


def test_engine_lazy_initializes_phase_executors():
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    assert isinstance(engine.planning_phase, PlanningPhaseExecutor)
    assert isinstance(engine.debate_phase, DebatePhaseExecutor)
    assert isinstance(engine.synthesis_phase, SynthesisPhaseExecutor)
    assert engine.planning_phase.engine is engine
    assert engine.debate_phase.engine is engine
    assert engine.synthesis_phase.engine is engine


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
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    candidates = [_dummy_agent("arch", "Architect"), _dummy_agent("sec", "Security")]

    selected, reason = engine.debate_phase.parse_speaker_selection(
        '{"speakers": ["sec"], "stop": false, "reason": "Need security review"}',
        candidates,
    )
    assert [a.key for a in selected] == ["sec"]
    assert reason == "Need security review"

    stopped_selected, stopped_reason = engine.debate_phase.parse_speaker_selection(
        '{"speakers": [], "stop": true, "reason": "All agreed"}',
        candidates,
    )
    assert stopped_selected == []
    assert stopped_reason == "All agreed"


def test_synthesis_phase_extract_artifacts():
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
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


def test_parse_gate_decision_reexport():
    decision = parse_gate_decision('{"decision": "yes", "reason": "Looks good"}')
    assert decision == ("yes", "Looks good")
    assert issubclass(GraphTurnError, Exception)
