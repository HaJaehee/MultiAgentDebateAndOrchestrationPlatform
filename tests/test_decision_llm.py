"""System 1 결정 LLM (Jev / Laya) 통합 검증 스위트.

검증 범위:
1. 기본값(System 2 생성형 LLM) 유지 및 에이전트별 `decision_llm.enabled` 옵션 플래그 동작
2. `conf.json` 전역(`llm.decision_llm`) 상속 및 에이전트별(`agents.<key>.decision_llm`) 켜기/끄기·주석/`${VAR}` 보존
3. `.env` 1차 API 키(`JEV_API_KEY`, `LAYA_API_KEY`, `DECISION_API_KEY`) 조회 및 `conf.json` 오버라이드 우선순위·비유출 보장
4. `DecisionCaller` HTTP (`/v1/systemone`, Laya `/predict` 폴백) · 로컬 `laya.Router` · 사용량 계측 · 오류 분류
5. 전문가 에이전트 System 1 발언(`LLMCaller`), 전역 폴백(`llm.fallback`), 연결 상태(`model_status`), `/api/agents` 키 비노출
6. 오케스트레이터 System 1 제어면 (`orchestrator_led` 발언자 지명, `graph_debate` 게이트 판정, `sub_debate` 개시 판단)
7. 에이전트 풀 UI 모델 스위칭 (`AgentCardsComponent` 모델 버튼·`S1` 배지, `AgentDialogs.open_model_settings`, 잠금 규칙)
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from nicegui import ui

from app import api as api_module
from app import graph_store
from app.agents import llm_fallback, llm_usage, model_status
from app.agents import pool as pool_module
from app.agents.base import Agent
from app.agents.connection import AUTH, MODEL, OK, RATE_LIMITED, SERVER_ERROR, classify
from app.agents.decision import DecisionCaller, DecisionHTTPStatusError, DecisionQuestion
from app.agents.llm import LLMCaller
from app.agents.pool import AgentPool, get_agent_pool
from app.config import (
    PROJECT_ROOT,
    AgentConfig,
    DecisionLLMConfig,
    RootConfig,
    get_config,
    read_conf_file,
    set_agent_model_settings_in_conf_file,
)
from app.database.models import SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration import context_memory as memory
from app.orchestration import roster_service
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.graph import parse_graph
from app.orchestration.roster_service import RosterService
from app.ui.components.roster.agent_cards import AgentCardsComponent
from app.ui.components.roster.agent_dialogs import AgentDialogs
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.state import RosterState
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def clean_decision_env(monkeypatch):
    for var in (
        "DECISION_BACKEND",
        "DECISION_MODEL",
        "DECISION_API_BASE",
        "DECISION_API_KEY",
        "JEV_API_KEY",
        "JEV_API_BASE",
        "JEV_MODEL",
        "LAYA_API_KEY",
        "LAYA_API_BASE",
        "LAYA_MODEL",
        "LAYA_MODEL_PATH",
    ):
        monkeypatch.delenv(var, raising=False)


# ===================================================================== 1 & 2. 스키마 · 상속 · API 키 우선순위


def test_default_is_system2_and_conf_example_parses(clean_decision_env):
    cfg = AgentConfig(name="Coder", role="Implementation", model="openai/gpt-4o", api_key="sk-s2")
    agent = Agent.from_config("coder", cfg)

    assert agent.decision_llm.enabled is False
    assert agent.uses_decision_llm is False
    assert agent.display_model == "openai/gpt-4o"
    assert agent.is_live is True

    raw_example = read_conf_file(PROJECT_ROOT / "conf.example.json")
    from app.config import resolve_env_vars, strip_comment_keys

    parsed_example = RootConfig.model_validate(resolve_env_vars(strip_comment_keys(raw_example)))
    assert parsed_example.llm.decision_llm.enabled is False
    for ag_cfg in parsed_example.agents.values():
        assert ag_cfg.decision_llm.enabled is False


def test_global_decision_llm_inheritance_and_per_agent_override(clean_decision_env, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "jev-env-secret")
    root = RootConfig.model_validate({
        "llm": {
            "model": "openai/gpt-4o",
            "api_key": "sk-s2",
            "decision_llm": {
                "enabled": True,
                "backend": "jev",
                "threshold": 0.65,
            },
        },
        "agents": {
            "orchestrator": {"name": "Master", "role": "Moderator"},
            "architect": {
                "name": "Architect",
                "role": "Design",
                "decision_llm": {"enabled": False},
            },
            "critic": {
                "name": "Critic",
                "role": "Review",
                "decision_llm": {"backend": "laya", "model": "laya-7b", "api_base": "http://localhost:8010/v1"},
            },
        },
    })

    assert root.agents["orchestrator"].decision_llm.enabled is True
    assert root.agents["orchestrator"].decision_llm.backend == "jev"
    assert root.agents["orchestrator"].decision_llm.threshold == 0.65
    assert root.agents["orchestrator"].decision_llm.effective_api_key == "jev-env-secret"

    assert root.agents["architect"].decision_llm.enabled is False
    assert root.agents["critic"].decision_llm.enabled is True
    assert root.agents["critic"].decision_llm.backend == "laya"
    assert root.agents["critic"].decision_llm.effective_model == "laya-7b"


def test_env_primary_api_key_and_conf_json_override_precedence(clean_decision_env, monkeypatch):
    monkeypatch.setenv("DECISION_API_KEY", "generic-env-key")
    monkeypatch.setenv("JEV_API_KEY", "jev-primary-env-key")
    monkeypatch.setenv("LAYA_API_KEY", "laya-primary-env-key")

    jev_cfg = DecisionLLMConfig(enabled=True, backend="jev")
    assert jev_cfg.has_env_api_key is True
    assert jev_cfg.has_conf_api_key is False
    assert jev_cfg.effective_api_key == "jev-primary-env-key"
    assert jev_cfg.is_live is True

    laya_cfg = DecisionLLMConfig(enabled=True, backend="laya")
    assert laya_cfg.effective_api_key == "laya-primary-env-key"

    overridden = DecisionLLMConfig(enabled=True, backend="jev", api_key="jev-conf-override-key")
    assert overridden.has_conf_api_key is True
    assert overridden.effective_api_key == "jev-conf-override-key"


def test_conf_editor_preserves_comments_placeholders_and_never_leaks_env_key(
    tmp_path: Path, clean_decision_env, monkeypatch,
):
    monkeypatch.setenv("JEV_API_KEY", "jev-super-secret-from-env")
    monkeypatch.setenv("CODER_MODEL", "openai/gpt-4.1")

    conf_path = tmp_path / "conf.json"
    initial = {
        "// top": "최상단 설명",
        "llm": {"model": "openai/gpt-4o", "api_key": "${OPENAI_API_KEY:-sk-test}"},
        "agents": {
            "orchestrator": {"name": "Master", "role": "Mod"},
            "coder": {
                "// note": "코더 주석 보존",
                "name": "Coder",
                "role": "Dev",
                "model": "${CODER_MODEL:-openai/gpt-4o}",
            },
        },
    }
    conf_path.write_text(json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1) System 1 활성화 (API 키는 입력하지 않아 .env 1차 키 사용)
    set_agent_model_settings_in_conf_file(
        "coder",
        decision_llm={"enabled": True, "backend": "jev", "threshold": 0.7},
        config_path=conf_path,
    )
    saved_text = conf_path.read_text(encoding="utf-8")
    assert "jev-super-secret-from-env" not in saved_text
    assert "${CODER_MODEL:-openai/gpt-4o}" in saved_text
    saved = read_conf_file(conf_path)
    assert saved["agents"]["coder"]["// note"] == "코더 주석 보존"
    assert saved["agents"]["coder"]["decision_llm"] == {
        "enabled": True,
        "backend": "jev",
        "threshold": 0.7,
    }

    # 2) conf.json 오버라이드 키 기록 후 다시 제거(.env 복귀)
    set_agent_model_settings_in_conf_file(
        "coder",
        decision_llm={"enabled": True, "backend": "jev", "api_key": "conf-override-key"},
        config_path=conf_path,
    )
    assert read_conf_file(conf_path)["agents"]["coder"]["decision_llm"]["api_key"] == "conf-override-key"

    set_agent_model_settings_in_conf_file(
        "coder",
        clear_decision_api_key=True,
        config_path=conf_path,
    )
    assert "api_key" not in read_conf_file(conf_path)["agents"]["coder"]["decision_llm"]

    # 3) 기본값(System 2)으로 끄고 보조 설정이 기본값이면 블록 자체 정리
    set_agent_model_settings_in_conf_file(
        "coder",
        decision_llm={"enabled": False, "backend": "jev", "threshold": 0.5},
        config_path=conf_path,
    )
    assert "decision_llm" not in read_conf_file(conf_path)["agents"]["coder"]


# ===================================================================== 3. DecisionCaller 전송 계층 (Jev / Laya)


@pytest.mark.asyncio
async def test_decision_caller_jev_http_and_usage_metering(clean_decision_env, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "jev-live-key")
    seen_requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "jev-latest"
        assert "gate" in body["questions"]
        return httpx.Response(
            200,
            json={
                "model": "jev-2026-q4",
                "answers": {"gate": {"type": "noul", "noul": 0.84, "confidence": 0.91}},
                "usage": {"input_tokens": 120, "output_tokens": 6},
            },
        )

    caller = DecisionCaller(http_transport=httpx.MockTransport(handler))
    cfg = DecisionLLMConfig(enabled=True, backend="jev")

    with llm_usage.measuring() as meter:
        resp = await caller.evaluate(
            cfg,
            "현재 설계 상태",
            {"gate": DecisionQuestion(type="noul", instructions="설계가 충분합니까?")},
        )

    assert len(seen_requests) == 1
    assert str(seen_requests[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert seen_requests[0].headers["Authorization"] == "Bearer jev-live-key"
    assert resp.model == "jev-2026-q4"
    assert resp.answers["gate"].noul == pytest.approx(0.84)
    assert meter.calls == 1
    assert meter.prompt_tokens == 120
    assert meter.completion_tokens == 6


@pytest.mark.asyncio
async def test_decision_caller_laya_http_falls_back_to_predict_and_local_router(clean_decision_env, monkeypatch):
    paths_called: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths_called.append(request.url.path)
        if request.url.path.endswith("/v1/systemone"):
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(
            200,
            json={
                "model": "laya-http",
                "answers": {
                    "pick": {
                        "type": "choice",
                        "choice": "A",
                        "probabilities": {"A": 0.75, "B": 0.25},
                    }
                },
            },
        )

    caller = DecisionCaller(http_transport=httpx.MockTransport(handler))
    http_cfg = DecisionLLMConfig(enabled=True, backend="laya", api_base="http://127.0.0.1:8020")
    resp = await caller.evaluate(
        http_cfg,
        "상태",
        {"pick": DecisionQuestion(type="choice", instructions="골라주세요", criteria=("A", "B"))},
    )
    assert paths_called == ["/v1/systemone", "/predict"]
    assert resp.answers["pick"].choice == "A"

    # 로컬 인프로세스 laya.Router 경로 검증
    fake_laya = types.ModuleType("laya")
    init_paths: List[str] = []

    class FakeRouter:
        def __init__(self, path: str = "default"):
            init_paths.append(path)

        def predict(self, state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
            assert "qual" in questions
            return {"answers": {"qual": {"type": "score", "score": 2.6, "confidence": 0.88}}}

    fake_laya.Router = FakeRouter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "laya", fake_laya)

    local_cfg = DecisionLLMConfig(enabled=True, backend="laya", model_path="/models/laya-small")
    assert local_cfg.is_live is True
    local_resp = await caller.evaluate(
        local_cfg,
        "로컬 상태",
        {"qual": DecisionQuestion(type="score", instructions="품질 점수", criteria=("낮음", "보통", "높음", "최상"))},
    )
    assert init_paths == ["/models/laya-small"]
    assert local_resp.answers["qual"].score == pytest.approx(2.6)


def test_decision_http_errors_classify_into_mado_connection_buckets():
    assert classify(DecisionHTTPStatusError(401, "Unauthorized")) == AUTH
    assert classify(DecisionHTTPStatusError(404, "Unknown model")) == MODEL
    assert classify(DecisionHTTPStatusError(429, "Too Many Requests")) == RATE_LIMITED
    assert classify(DecisionHTTPStatusError(503, "Unavailable")) == SERVER_ERROR


# ===================================================================== 4. 전문가 System 1 발언 · 폴백 · 상태 · API


@pytest.mark.asyncio
async def test_specialist_agent_speaks_with_system1_and_supports_fallback(clean_decision_env, monkeypatch):
    llm_fallback.get_fallback_breaker().reset()

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-pro",
                "answers": {
                    "approval": {"type": "noul", "noul": 0.88},
                    "quality": {"type": "score", "score": 2.7, "confidence": 0.9},
                    "action": {
                        "type": "choice",
                        "choice": "승인 및 합의 진행",
                        "probabilities": {"승인 및 합의 진행": 0.82, "구체적 보완 후 진행": 0.18},
                    },
                },
            },
        )

    caller = LLMCaller()
    caller.decision_caller = DecisionCaller(http_transport=httpx.MockTransport(ok_handler))

    critic = Agent.from_config(
        "critic",
        AgentConfig(
            name="Quality Critic",
            role="Security & Quality Review",
            model="openai/gpt-4o",
            api_key="sk-s2",
            decision_llm=DecisionLLMConfig(enabled=True, backend="jev", api_key="jev-key", threshold=0.6),
        ),
    )
    chunks: List[str] = []
    text, logs = await caller.call_agent(
        critic,
        [{"role": "user", "content": "이 설계안을 검토해 주세요."}],
        on_chunk=chunks.append,
    )
    assert logs == []
    assert chunks and "".join(chunks) == text
    assert "충족 (승인)" in text
    assert "승인 및 합의 진행" in text
    assert memory.extract_digest(text), "다음 라운드 압축이 쓸 ## 요지 섹션이 반드시 포함되어야 합니다"

    # Jev 인증 실패(401) 시 전역 System 2 모델로 폴백하는지 검증
    root = RootConfig.model_validate({
        "llm": {
            "model": "openai/gpt-4o-mini",
            "api_key": "sk-global-s2",
            "fallback": {"enabled": True, "retry_after": 60},
        },
        "agents": {
            "orchestrator": {"name": "Master", "role": "Mod"},
            "critic": {
                "name": "Quality Critic",
                "role": "Review",
                "decision_llm": {"enabled": True, "backend": "jev", "api_key": "bad-jev-key"},
            },
        },
    })
    monkeypatch.setattr(llm_fallback, "get_config", lambda: root)

    def auth_fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    caller.decision_caller = DecisionCaller(http_transport=httpx.MockTransport(auth_fail_handler))

    async def fake_s2_loop(agent_arg, *args, **kwargs):
        assert agent_arg.decision_llm.enabled is False
        assert agent_arg.model == "openai/gpt-4o-mini"
        return "전역 System 2 폴백 응답", []

    monkeypatch.setattr(caller, "_run_litellm_loop", fake_s2_loop)
    notices = []
    s1_critic = Agent.from_config("critic", root.agents["critic"])
    with llm_fallback.listening(notices.append):
        reply, _ = await caller.call_agent(s1_critic, [{"role": "user", "content": "검토"}])

    assert reply == "전역 System 2 폴백 응답"
    assert len(notices) == 1
    assert notices[0].intended_model == "jev/jev-latest"
    assert notices[0].model == "openai/gpt-4o-mini"
    llm_fallback.get_fallback_breaker().reset()


@pytest.mark.asyncio
async def test_model_status_and_api_agents_handle_decision_llm_safely(clean_decision_env, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "sk-jev-secret-never-leak")
    root = RootConfig.model_validate({
        "llm": {"model": "openai/gpt-4o", "api_key": "sk-s2"},
        "agents": {
            "orchestrator": {
                "name": "Master",
                "role": "Moderator",
                "decision_llm": {"enabled": True, "backend": "jev"},
            },
            "critic": {
                "name": "Critic",
                "role": "Review",
                "decision_llm": {"enabled": True, "backend": "jev"},
            },
        },
    })

    targets = model_status.collect_targets(root)
    assert len(targets) == 2
    s2_target, s1_target = targets[0], targets[1]
    assert s2_target.model == "openai/gpt-4o"
    assert "Master" in s2_target.users
    assert s1_target.model == "jev/jev-latest"
    assert s1_target.users == ("Master (결정 LLM)", "Critic")
    assert s1_target.decision_cfg is not None

    async def fake_probe(self, cfg, timeout_seconds=15.0):
        return None

    monkeypatch.setattr(DecisionCaller, "probe", fake_probe)
    status = await model_status.probe(s1_target)
    assert status.state == OK

    pool = AgentPool(root.agents)
    monkeypatch.setattr(api_module, "get_agent_pool", lambda: pool)
    payload = await api_module.list_agents()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "sk-jev-secret-never-leak" not in serialized
    critic_row = next(item for item in payload if item["key"] == "critic")
    assert critic_row["decision_llm"]["enabled"] is True
    assert critic_row["decision_llm"]["backend"] == "jev"
    assert critic_row["decision_llm"]["has_api_key"] is True
    assert critic_row["decision_llm"]["has_conf_api_key"] is False


# ===================================================================== 5. 오케스트레이터 System 1 제어면


@pytest.mark.asyncio
async def test_orchestrator_system1_controls_speaker_selection_sub_debate_and_graph_gate(
    tmp_path: Path, clean_decision_env, monkeypatch,
):
    await init_db(DB_URL)
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)

    s1_requests: List[Dict[str, Any]] = []

    def s1_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        s1_requests.append(body)
        qs = body["questions"]
        if "gate" in qs:
            return httpx.Response(200, json={"model": "jev-ctrl", "answers": {"gate": {"noul": 0.91}}})
        if "open" in qs:
            return httpx.Response(
                200,
                json={
                    "model": "jev-ctrl",
                    "answers": {
                        "open": {"noul": 0.85},
                        "cand_architect": {"noul": 0.92},
                        "cand_coder": {"noul": 0.20},
                        "cand_critic": {"noul": 0.88},
                    },
                },
            )
        # 발언자 지명: critic 만 임계치(0.6) 이상
        return httpx.Response(
            200,
            json={
                "model": "jev-ctrl",
                "answers": {
                    "architect": {"noul": 0.25},
                    "coder": {"noul": 0.30},
                    "critic": {"noul": 0.93},
                },
            },
        )

    pool = AgentPool({
        "orchestrator": AgentConfig(
            name="Master",
            role="Moderator",
            model="fake/s2",
            api_key="k",
            decision_llm=DecisionLLMConfig(enabled=True, backend="jev", api_key="jev-k", threshold=0.6),
        ),
        "architect": AgentConfig(name="Architect", role="Design", model="fake/s2", api_key="k"),
        "coder": AgentConfig(name="Coder", role="Impl", model="fake/s2", api_key="k"),
        "critic": AgentConfig(name="Critic", role="Review", model="fake/s2", api_key="k"),
    })

    open_ledger = (
        "## 요구사항·제약\n- 없음\n\n## 결정 사항\n- 없음\n\n## 기각된 대안\n- 없음\n\n"
        "## 미해결 쟁점\n- 캐시 무효화 정책 충돌\n\n## 담당·다음 할 일\n- 없음\n"
    )

    class HybridFakeLLM(FakeLLMCaller):
        def __init__(self):
            super().__init__()
            self.decision_caller = DecisionCaller(http_transport=httpx.MockTransport(s1_handler))

        def _reply_for(self, agent, messages):
            last = messages[-1]["content"] if messages else ""
            if "[결정 장부 갱신]" in last:
                return open_ledger
            return f"{agent.name} System 2 응답\n\n## 요지\n- 핵심 요지"

    llm = HybridFakeLLM()
    engine = OrchestratorEngine(agent_pool=pool, llm_caller=llm)

    # (A) orchestrator_led + sub_debates=True
    sid1 = str(uuid.uuid4())
    async with get_session_factory()() as db:
        db.add(SessionModel(
            id=sid1,
            title="S1 Control",
            strategy="orchestrator_led",
            max_rounds=2,
            active_agents=["orchestrator", "architect", "coder", "critic"],
            sub_debates=True,
            sub_debate_rounds=1,
            converge_early=False,
        ))
        await db.commit()

    state1 = await engine.run_turn(session_id=sid1, user_prompt="캐시 아키텍처를 설계하세요")
    # Round 1·2 모두 System 1이 critic 만 지명했고, 라운드 1 경계에서 architect ↔ critic 서브 토론이 열림
    nomination_notes = [m for m in state1.messages if "[Round " in m.content and "발언권]" in m.content]
    assert len(nomination_notes) == 2
    assert all("Critic" in m.content and "System 1(JEV/jev-ctrl)" in m.content for m in nomination_notes)
    sub_open_notes = [m for m in state1.messages if m.content.startswith("[서브 토론]")]
    assert len(sub_open_notes) == 1
    assert "Architect ↔ Critic" in sub_open_notes[0].content
    assert "System 1(JEV/jev-ctrl)" in sub_open_notes[0].content

    # (B) graph_debate 게이트 판정
    graph_spec = parse_graph({
        "id": "s1-gate-graph",
        "name": "S1 게이트 검증",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "arch", "type": "agent", "agent": "architect", "label": "설계"},
            {"id": "gate", "type": "gate", "label": "통과 판정", "question": "설계가 통과 기준을 넘었는가?", "default": "no"},
            {"id": "fix", "type": "agent", "agent": "coder", "label": "보완"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "from": ["start", "out"], "to": ["arch", "in"]},
            {"id": "e2", "from": ["arch", "out"], "to": ["gate", "in"]},
            {"id": "e3", "from": ["gate", "yes"], "to": ["end", "in"]},
            {"id": "e4", "from": ["gate", "no"], "to": ["fix", "in"]},
            {"id": "e5", "from": ["fix", "out"], "to": ["end", "in"]},
        ],
    })
    graph_store.save_graph(graph_spec)

    sid2 = str(uuid.uuid4())
    async with get_session_factory()() as db:
        db.add(SessionModel(
            id=sid2,
            title="S1 Gate",
            strategy="graph_debate",
            graph_id="s1-gate-graph",
            max_rounds=2,
            active_agents=["orchestrator", "architect", "coder"],
        ))
        await db.commit()

    state2 = await engine.run_turn(session_id=sid2, user_prompt="설계 통과 여부 확인")
    gate_notes = [m for m in state2.messages if m.graph_node_id == "gate"]
    assert len(gate_notes) == 1
    assert "**예**" in gate_notes[0].content
    assert "System 1(JEV/jev-ctrl)" in gate_notes[0].content
    # yes 갈래로 곧장 종료되어 보완(coder) 노드는 실행되지 않음
    assert not any(m.sender_key == "coder" for m in state2.messages)
    assert any("gate" in req["questions"] for req in s1_requests)


# ===================================================================== 6. 에이전트 풀 UI 모델 스위칭


class _IdleRunner:
    running: List[str] = []

    def running_sessions(self) -> List[str]:
        return list(self.running)

    def is_running(self, session_id: str) -> bool:
        return session_id in self.running


class _NoopRuntimes:
    def get(self, workspace=None):
        return None

    async def reload_all(self):
        return {}


@pytest.mark.asyncio
async def test_agent_pool_ui_switches_models_and_respects_lock(
    tmp_path: Path, clean_decision_env, monkeypatch, ui_page,
):
    monkeypatch.setenv("LAYA_API_KEY", "laya-env-primary-secret")
    conf_path = tmp_path / "conf.json"
    conf_path.write_text(
        json.dumps(
            {
                "llm": {"model": "openai/gpt-4o", "api_key": "sk-s2"},
                "agents": {
                    "orchestrator": {"name": "Master", "role": "Moderator"},
                    "critic": {"name": "Critic", "role": "Reviewer"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    from app.config import loader

    monkeypatch.setattr(loader, "_config", None)
    monkeypatch.setattr(loader, "_config_path", None)
    monkeypatch.setattr(pool_module, "_agent_pool", None)
    get_config(reload=True, config_path=conf_path)
    monkeypatch.setattr(roster_service, "get_debate_runner", lambda: _IdleRunner())
    monkeypatch.setattr(roster_service, "get_runtime_pool", lambda: _NoopRuntimes())

    notifications: List[str] = []
    monkeypatch.setattr(ui, "notify", lambda message, **kw: notifications.append(str(message)))

    service = RosterService()
    state = RosterState(agent_pool=service.agent_pool())

    cards_holder: Dict[str, AgentCardsComponent] = {}

    def _on_applied() -> None:
        state.agent_pool = service.agent_pool()
        state.sync_with_pool()
        if "cards" in cards_holder and cards_holder["cards"].built:
            cards_holder["cards"].fill(state.roster_agents())

    changes = RosterChanges(
        state,
        service,
        alive=lambda: True,
        toast_host=lambda: None,
        on_lock_changed=lambda: None,
        on_applied=_on_applied,
        on_write_failed=lambda: None,
    )
    dialogs = AgentDialogs(state, service, changes)
    cards = AgentCardsComponent(
        state,
        service,
        changes,
        open_tools=lambda _: None,
        open_skills=lambda _: None,
        open_modalities=lambda _: None,
        open_delete=lambda _: None,
        disable_agent=lambda _: None,
        on_selection_changed=lambda: None,
        refresh_disabled=lambda: None,
        open_model=dialogs.open_model_settings,
    )
    cards_holder["cards"] = cards

    handlers: Dict[str, Any] = {}
    real_button = ui.button

    def spying_button(text="", *args, on_click=None, **kwargs):
        if text:
            handlers[text] = on_click
        return real_button(text, *args, on_click=on_click, **kwargs)

    monkeypatch.setattr(ui, "button", spying_button)

    try:
        with ui_page:
            cards.build()
            model_buttons = [
                el for el in cards.cards_row.descendants()
                if isinstance(el, real_button) and getattr(el, "text", None) == "모델"
            ]
            assert len(model_buttons) == 2, "오케스트레이터와 전문가 카드 모두 '모델' 스위칭 버튼을 제공합니다"
            assert all(btn.enabled for btn in model_buttons)

            # Critic 모델 설정 창을 열어 System 1 (Laya) 으로 전환
            dialogs.open_model_settings("critic")
            radios = [el for el in ui_page.layout.descendants() if isinstance(el, ui.radio)]
            selects = [el for el in ui_page.layout.descendants() if isinstance(el, ui.select)]
            assert radios and radios[-1].value == "system2"
            radios[-1].value = "system1"
            selects[-1].value = "laya"

            await handlers["저장"]()

            # conf.json 영속화 및 .env 비밀키 비유출 확인
            raw_text = conf_path.read_text(encoding="utf-8")
            assert "laya-env-primary-secret" not in raw_text
            raw_conf = read_conf_file(conf_path)
            assert raw_conf["agents"]["critic"]["decision_llm"]["enabled"] is True
            assert raw_conf["agents"]["critic"]["decision_llm"]["backend"] == "laya"

            # 에이전트 풀 및 카드 배지(S1·LAYA) 즉시 갱신 확인
            reloaded_critic = get_agent_pool().get("critic")
            assert reloaded_critic is not None
            assert reloaded_critic.decision_llm.enabled is True
            assert reloaded_critic.display_model == "laya/laya-latest"
            card_texts = [el.text for el in cards.cards_row.descendants() if getattr(el, "text", None)]
            assert "S1·LAYA" in card_texts
            assert "laya/laya-latest" in card_texts

            # 세션 잠금 시 모델 스위칭 창과 카드 버튼이 함께 잠기는지 확인
            state.personas_locked = True
            cards.fill(state.roster_agents())
            locked_buttons = [
                el for el in cards.cards_row.descendants()
                if isinstance(el, real_button) and getattr(el, "text", None) == "모델"
            ]
            assert locked_buttons and not any(btn.enabled for btn in locked_buttons)
            before_count = len(notifications)
            dialogs.open_model_settings("critic")
            assert len(notifications) > before_count
    finally:
        loader._config = None  # noqa: SLF001
        loader._config_path = None  # noqa: SLF001
        pool_module._agent_pool = None  # noqa: SLF001
