"""실행 기반 진단(Diagnostician) 에이전트 · 시도 이력 누적 · 외부 정체 관찰자 검증 (ADR-049).

검증 항목:
1. 기본 설정(`conf.example.json`)과 도구 보안 정책:
   - `diagnostician` 에이전트(`debate_priority=50`, `debate_stance="diagnostician"`, `temperature=0.1`) 로드 검증
   - `read`·`sandbox` 실행은 허용하되 `write(**)`·파일 수정·git 변이 도구 및 샌드박스 내부 파일 쓰기는 차단
2. 순차·대립 전략 배치 및 바통 패스 지침 (`ADR-012` 에이전트 키 독립성):
   - 순차 토론: `architect(10) -> coder(20) -> critic(30) -> diagnostician(50)` 순서 및 Round 2+ 직전 라운드 마지막 발언자 바통 인계
   - 대립 토론: `proponent -> critic -> diagnostician -> neutral` 배치 및 실행 증거 지침
3. `SpeechRunner` 실행 증거 게이트:
   - 실행 도구가 제공되었는데 호출 없이 의견만 내면 1회 `[실행 기반 진단 필수]` 재시도 수행
   - 재시도에서 실행 도구를 호출하면 `verified=True`, 끝내 호출하지 않거나 실행 도구가 없으면 `verified=False` + `[실행 검증 미수행]` 배너 부착
4. 구조화된 `[시도·검증 이력]` 누적 및 프롬프트 캐시 보존 (`ADR-045`):
   - 앞선 발언 본문이 `## 요지` 로 압축되더라도 수정 파일 경로·샌드박스 실패 트레이스백·원인 위치·진단 요약이 후속 라운드 프롬프트 말미에 보존됨
   - 증상 패치로 줄 번호가 밀려도(`line 42` -> `line 49`) 동일 실패 시그니처로 정규화됨
   - 수정·실행·진단 활동이 없는 순수 개념 토론에서는 빈 문자열을 반환해 프롬프트를 건드리지 않음
5. 외부 정체 관찰자 & 편향 격리형 오케스트레이터 재설계 에스컬레이션 + 중단 재개 멱등성 (`ADR-024`):
   - 2회 연속 동일 실패 시그니처 감지 시 `KIND_STAGNATION` 기록, `STAGNATION_DETECTED` 이벤트 발행, 참여자 대화 원문을 배제한 오케스트레이터 재설계(`KIND_PLAN`) 수행 및 `state.plan_index` 교체
   - Round 2 재설계 직후 중단되었다가 `resume_turn` 으로 재개해도 중복 에스컬레이션 없이 Round 3을 새 계획으로 이어감
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app.agents.base import AGENT_STYLE_MAP, Agent
from app.agents.pool import AgentPool
from app.agents.tool_loading import LOAD_TOOLS_TOOL, ToolShelf
from app.config import AgentConfig, load_config, read_conf_file
from app.config.schema import DEBATE_STANCES
from app.database.models import MessageModel, SessionModel, ToolCallRecordModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.events import EventType
from app.mcp.policy import ALLOW, ASK, DENY, Policy, ToolMeta, evaluate, profile_call, tool_always_denied
from app.orchestration import context_memory as memory
from app.orchestration import diagnostic_loop, turns
from app.orchestration.context import EngineContext
from app.orchestration.conversation import ConversationMemory
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateMessage, DebateState
from app.orchestration.strategies import (
    AdversarialDebateStrategy,
    SequentialDebateStrategy,
    instruction_for_turn,
    speakers_for_round,
)
from app.tool_rules import parse_rules
from tests.fake_llm import LEDGER_REPLY, SYNTHESIS_REPLY, FakeLLMCaller

CONF_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "conf.example.json"
EXEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox__execute_python_code",
        "description": "Execute Python code in sandbox",
        "parameters": {"type": "object", "properties": {}},
    },
}
WS = Path(os.path.abspath("mado-diag-workspace"))


def _sandbox_failure_output(*, line_no: int = 42, exc: str = "KeyError: 'stale_token'") -> str:
    return (
        "execution_status: RAN_WITH_ERRORS\n"
        "active_namespace: session:diagnostician\n"
        "next_action: inspect stderr below before proceeding.\n\n"
        "--- stderr ---\n"
        "Traceback (most recent call last):\n"
        '  File "<sandbox>", line 4, in <module>\n'
        f'  File "/workspace/src/cache.py", line {line_no}, in get_item\n'
        f"    return store[key]\n"
        f"{exc}\n\n"
        "--- stdout ---\n"
        "(nothing was printed to stdout; assign or print() values you want to observe)"
    )


def _sandbox_pass_output(msg: str = "PASS: generation token invalidated cleanly") -> str:
    return (
        "execution_status: RAN_UNVERIFIED\n"
        "active_namespace: session:diagnostician\n"
        "next_action: inspect stdout and write an explicit assertion if verifying correctness.\n\n"
        "--- stdout ---\n"
        f"{msg}"
    )


def _open_ledger(issue: str = "캐시 세대 토큰 무효화 불일치") -> str:
    return (
        "## 요구사항·제약\n- 멀티 워커 캐시 일관성 보장\n\n"
        "## 결정 사항\n- FastAPI 서비스 계층 유지\n\n"
        "## 기각된 대안\n- 없음\n\n"
        f"## 미해결 쟁점\n- {issue}\n\n"
        "## 담당·다음 할 일\n- 코더: 근본 원인 수정"
    )


def _make_pool(*, custom_diag_key: str = "diagnostician") -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(
            name="Master Orchestrator",
            role="Moderator",
            model="gpt-test",
            api_key="k",
            system_prompt="조율자",
        ),
        "architect": AgentConfig(
            name="System Architect",
            role="Architecture",
            model="gpt-test",
            api_key="k",
            system_prompt="설계자",
            debate_priority=10,
            debate_stance="proponent",
        ),
        "coder": AgentConfig(
            name="Senior Engineer",
            role="Implementation",
            model="gpt-test",
            api_key="k",
            system_prompt="구현자",
            debate_priority=20,
            debate_stance="proponent",
            allowed_mcp_servers=["filesystem", "sandbox"],
        ),
        "critic": AgentConfig(
            name="Quality Critic",
            role="Review",
            model="gpt-test",
            api_key="k",
            system_prompt="비평가",
            debate_priority=30,
            debate_stance="critic",
        ),
        custom_diag_key: AgentConfig(
            name="Root-Cause Diagnostician",
            role="Reproduction & Root-Cause Verification",
            model="diag-model-distinct",
            api_key="k",
            system_prompt="실행 기반 진단가",
            temperature=0.1,
            debate_priority=50,
            debate_stance="diagnostician",
            allowed_mcp_servers=["filesystem", "sandbox", "git"],
        ),
    })


class _StubMCP:
    """실행 도구 스키마를 제공하는 경량 MCP 매니저 대역."""

    def __init__(self, tool_schemas: List[Dict[str, Any]]) -> None:
        self._schemas = list(tool_schemas)

    def get_openai_tools_for_servers(self, server_keys: List[str]) -> List[Dict[str, Any]]:
        if not server_keys:
            return []
        return list(self._schemas)


# ===========================================================================
# 1. 설정 스키마 · conf.example.json · 읽기/실행 전용 보안 정책
# ===========================================================================


def test_conf_example_registers_diagnostician_and_enforces_read_exec_only_security():
    assert "diagnostician" in DEBATE_STANCES
    assert "diagnostician" in AGENT_STYLE_MAP

    raw = read_conf_file(str(CONF_EXAMPLE_PATH))
    assert raw["agents"]["diagnostician"]["model"].startswith("${DIAGNOSTICIAN_MODEL")

    cfg = load_config(str(CONF_EXAMPLE_PATH))
    assert "diagnostician" in cfg.agents
    diag_cfg = cfg.agents["diagnostician"]
    assert diag_cfg.debate_stance == "diagnostician"
    assert diag_cfg.debate_priority == 50
    assert diag_cfg.temperature == pytest.approx(0.1)
    assert set(diag_cfg.allowed_mcp_servers) == {"filesystem", "sandbox", "git"}

    # critic(30) 뒤에서 비평을 실행 증거로 전환하도록 우선순위가 배치되어야 합니다.
    assert cfg.agents["critic"].debate_priority < diag_cfg.debate_priority

    sec = cfg.tool_security
    agent_sec = sec.agents["diagnostician"]
    deny_rules = parse_rules(list(sec.deny) + list(agent_sec.deny))
    ask_rules = parse_rules(list(sec.ask) + list(agent_sec.ask))
    allow_rules = parse_rules(list(sec.allow))
    policy = Policy(mode="default", deny=deny_rules, ask=ask_rules, allow=allow_rules)

    # 1) 소스 읽기 허용
    read_profile = profile_call(
        ToolMeta("filesystem", "read_text_file"),
        {"path": str(WS / "src/cache.py")},
        WS,
        read_file=lambda _p: None,
    )
    assert evaluate(read_profile, policy).effect == ALLOW

    # 2) 샌드박스에서 재현 스크립트 실행은 DENY 되지 않음 (기본 모드: ASK 또는 ALLOW)
    exec_profile = profile_call(
        ToolMeta("sandbox", "execute_python_code"),
        {"code": "from src.cache import get_item\nassert get_item('x') is not None\n"},
        WS,
        read_file=lambda _p: None,
    )
    assert evaluate(exec_profile, policy).effect in (ALLOW, ASK)

    # 3) 샌드박스 내부에서 프로덕션 파일을 몰래 수정하려는 시도는 write(**) 규칙으로 즉시 DENY!
    stealth_write_profile = profile_call(
        ToolMeta("sandbox", "execute_python_code"),
        {"code": "open('src/cache.py', 'w').write('patched = True')\n"},
        WS,
        read_file=lambda _p: None,
    )
    assert evaluate(stealth_write_profile, policy).effect == DENY

    # 4) 파일 쓰기·편집·git 커밋 도구는 스키마 단계에서부터 항상 거부(숨김) 대상
    for server, tool_name in (
        ("filesystem", "write_file"),
        ("filesystem", "edit_file"),
        ("sandbox", "write_workspace_file"),
        ("git", "git_commit"),
    ):
        assert bool(tool_always_denied(ToolMeta(server, tool_name), policy)) is True
    assert bool(tool_always_denied(ToolMeta("sandbox", "execute_python_code"), policy)) is False


# ===========================================================================
# 2. 전략 배치 순서 및 라운드 간 바통 패스 (ADR-012 준수)
# ===========================================================================


def test_sequential_and_adversarial_strategies_route_diagnostician_without_hardcoded_keys():
    # 에이전트 키를 기본값이 아닌 임의 키(`repro_hunter`)로 두어도 stance 와 priority 만으로 동작해야 합니다 (ADR-012).
    pool = _make_pool(custom_diag_key="repro_hunter")
    active = pool.list_all()
    state = DebateState(session_id="s1", user_prompt="버그 수정", strategy="sequential_debate", max_rounds=3)

    seq = SequentialDebateStrategy()
    r1_speakers, problem = speakers_for_round(seq, active, 1, state)
    assert not problem
    assert [a.key for a in r1_speakers] == ["architect", "coder", "critic", "repro_hunter"]

    # Round 1 마지막 순서인 진단 에이전트는 실행 증거 산출 지침을 받습니다.
    state.current_round = 1
    diag_r1_inst = instruction_for_turn(seq, r1_speakers[3], r1_speakers, 3, state)
    assert "[진단·재현 지침]" in diag_r1_inst
    assert "Quality Critic" in diag_r1_inst
    assert "프로덕션 소스 파일은 직접 수정하지 마세요" in diag_r1_inst

    # Round 1 종료 시 마지막 발언자(Root-Cause Diagnostician)가 기록되면,
    # Round 2 첫 발언자(architect)에게 라운드 경계를 넘어 바통이 인계됩니다.
    state.messages.append(DebateMessage(
        sender_key="repro_hunter",
        sender_name="Root-Cause Diagnostician",
        sender_role="Reproduction",
        content="재현 결과 KeyError 확인",
        round_number=1,
        turn_meta=turns.meta(turns.KIND_SPEECH, stance="diagnostician"),
    ))
    state.current_round = 2
    first_r2_inst = instruction_for_turn(seq, r1_speakers[0], r1_speakers, 0, state)
    assert "Root-Cause Diagnostician(Reproduction)" in first_r2_inst
    assert "근본 원인을 수정한 뒤 실행으로 검증하세요" in first_r2_inst

    coder_r2_inst = instruction_for_turn(seq, r1_speakers[1], r1_speakers, 1, state)
    assert "증상만 우회하지 말고 지목된 근본 원인을 수정한 뒤 실행으로 검증하세요" in coder_r2_inst

    # 대립(Adversarial) 전략에서도 공방(proponent <-> critic 교차) 직후 diagnostician 이 실행 검증을 수행합니다.
    adv = AdversarialDebateStrategy()
    adv_speakers, adv_problem = speakers_for_round(adv, active, 1, state)
    assert not adv_problem
    assert [a.key for a in adv_speakers] == ["architect", "critic", "coder", "repro_hunter"]
    adv_diag_inst = instruction_for_turn(adv, adv_speakers[3], adv_speakers, 3, state)
    assert "[디베이트·진단]" in adv_diag_inst


# ===========================================================================
# 3. SpeechRunner 실행 증거 검증 게이트 (재시도 및 미검증 경고 스탬프)
# ===========================================================================


@pytest.fixture
def caller_tools(monkeypatch):
    """턴이 빌린 런타임 대신 호출기의 도구(`_StubMCP`)를 쓰게 합니다 (`EngineContext.mcp_for`)."""
    monkeypatch.setattr(EngineContext, "mcp_for", staticmethod(lambda state: None))


class _DiagGateCaller(FakeLLMCaller):
    """진단 에이전트의 1차·재시도 도구 호출 여부를 제어하는 테스트 호출기."""

    def __init__(
        self,
        *,
        mcp_schemas: List[Dict[str, Any]],
        run_exec_on_attempt: Optional[int],
    ) -> None:
        super().__init__()
        self.mcp_manager = _StubMCP(mcp_schemas)
        self.run_exec_on_attempt = run_exec_on_attempt
        self.diag_attempts = 0
        self.diag_prompts: List[str] = []

    def resolve_tool_servers(self, agent: Agent) -> List[str]:
        return list(agent.allowed_mcp_servers or [])

    async def call_agent(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        last = messages[-1]["content"] if messages else ""
        if agent.debate_stance == "diagnostician" and "[결정 장부 갱신]" not in last:
            self.diag_attempts += 1
            self.diag_prompts.append(last)
            if self.run_exec_on_attempt == self.diag_attempts:
                call_log = {
                    "tool_name": "sandbox__execute_python_code",
                    "arguments": {"code": "from src.cache import get_item; get_item('stale_token')"},
                    "status": "success",
                    "output": _sandbox_failure_output(line_no=42),
                }
                if on_tool_call is not None:
                    res = on_tool_call(call_log)
                    if asyncio.iscoroutine(res):
                        await res
                body = (
                    "샌드박스 재현 결과 `src/cache.py:42` (`get_item`)에서 `KeyError: 'stale_token'` 발생을 확인했습니다.\n\n"
                    "## 요지\n- `src/cache.py:42` 에서 만료 키 조회 시 `KeyError` 재현됨"
                )
                return body, [call_log]
            return "코드상 아마 캐시 만료 처리에서 문제가 나는 것으로 추측됩니다.", []
        return await super().call_agent(
            agent, messages, custom_instructions, on_tool_call=on_tool_call, on_chunk=on_chunk, **kwargs
        )


@pytest.mark.asyncio
async def test_diagnostician_opinion_triggers_one_execution_retry_and_verifies(caller_tools):
    caller = _DiagGateCaller(mcp_schemas=[EXEC_SCHEMA], run_exec_on_attempt=2)
    pool = _make_pool()

    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"diag-retry-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid,
            title="진단 재시도",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "coder", "critic", "diagnostician"],
        ))
        await db.commit()

    await OrchestratorEngine(agent_pool=pool, llm_caller=caller).run_turn(
        session_id=sid, user_prompt="캐시 오류 원인을 찾아 줘",
    )

    assert caller.diag_attempts == 2
    assert "[실행 기반 진단 필수]" in caller.diag_prompts[1]

    async with get_session_factory(db_url)() as db:
        msgs = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        tool_rows = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid)
        )).scalars().all()

    diag_msg = next(m for m in msgs if m.sender_key == "diagnostician")
    assert diag_msg.turn_meta["stance"] == "diagnostician"
    # 검증 여부는 행에 적지 않고 실행 기록에서 셉니다 (다시 실행하면 행의 `turn_meta` 는 그대로입니다).
    assert "verified" not in diag_msg.turn_meta
    assert diagnostic_loop.UNVERIFIED_DIAGNOSTIC_TAG not in diag_msg.content
    assert "src/cache.py:42" in diag_msg.content
    assert any(t.message_id == diag_msg.id and t.tool_name == "sandbox__execute_python_code" for t in tool_rows)


@pytest.mark.asyncio
async def test_diagnostician_stamps_warning_when_unverified_or_without_exec_tools(caller_tools):
    # 1) 실행 도구가 있는데 재시도에서도 끝내 부르지 않은 경우 -> 재시도 1회 후 미검증 배너 부착
    stubborn_caller = _DiagGateCaller(mcp_schemas=[EXEC_SCHEMA], run_exec_on_attempt=None)
    pool = _make_pool()
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid_retry = f"diag-unverified-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid_retry,
            title="미검증 경고",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "critic", "diagnostician"],
        ))
        await db.commit()

    await OrchestratorEngine(agent_pool=pool, llm_caller=stubborn_caller).run_turn(
        session_id=sid_retry, user_prompt="진단해 줘",
    )
    assert stubborn_caller.diag_attempts == 2

    # 2) 실행 도구가 아예 제공되지 않은 환경 -> 불필요한 재시도 없이 즉시 미검증 배너 부착
    no_tool_caller = _DiagGateCaller(mcp_schemas=[], run_exec_on_attempt=None)
    sid_no_tool = f"diag-notool-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid_no_tool,
            title="도구 없음",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "critic", "diagnostician"],
        ))
        await db.commit()

    await OrchestratorEngine(agent_pool=pool, llm_caller=no_tool_caller).run_turn(
        session_id=sid_no_tool, user_prompt="진단해 줘",
    )
    assert no_tool_caller.diag_attempts == 1

    async with get_session_factory(db_url)() as db:
        msg_retry = (await db.execute(
            select(MessageModel).where(
                MessageModel.session_id == sid_retry, MessageModel.sender_key == "diagnostician"
            )
        )).scalar_one()
        msg_no_tool = (await db.execute(
            select(MessageModel).where(
                MessageModel.session_id == sid_no_tool, MessageModel.sender_key == "diagnostician"
            )
        )).scalar_one()

    assert diagnostic_loop.UNVERIFIED_DIAGNOSTIC_TAG in msg_retry.content
    assert diagnostic_loop.UNVERIFIED_DIAGNOSTIC_TAG in msg_no_tool.content


@pytest.mark.asyncio
async def test_retried_diagnostician_speech_goes_through_the_gate_and_history_follows_its_tools(caller_tools):
    # Round 1 진단은 첫 시도에 실행해 검증됐습니다.
    caller = _DiagGateCaller(mcp_schemas=[EXEC_SCHEMA], run_exec_on_attempt=1)
    pool = _make_pool()
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"diag-revision-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid,
            title="진단 다시 실행",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "critic", "diagnostician"],
        ))
        await db.commit()
    engine = OrchestratorEngine(agent_pool=pool, llm_caller=caller)
    await engine.run_turn(session_id=sid, user_prompt="진단해 줘")
    assert caller.diag_attempts == 1

    async with get_session_factory(db_url)() as db:
        target = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid, MessageModel.sender_key == "diagnostician")
        )).scalar_one()
    assert diagnostic_loop.UNVERIFIED_DIAGNOSTIC_TAG not in target.content

    # 다시 실행한 버전은 실행 없이 의견만 냅니다 -> 같은 게이트로 1회 보완 요구 후 미검증 배너가 붙어야 합니다.
    await engine.retry_speech(sid, target.id)
    assert caller.diag_attempts == 3
    assert "[실행 기반 진단 필수]" in caller.diag_prompts[2]
    async with get_session_factory(db_url)() as db:
        revised = await db.get(MessageModel, target.id)
    assert diagnostic_loop.UNVERIFIED_DIAGNOSTIC_TAG in revised.content

    # 행의 `turn_meta` 에 옛 값이 남아 있어도 시도 이력은 지금 버전의 실행 기록만 봅니다.
    stale = DebateMessage(
        sender_key="diagnostician",
        sender_name="Root-Cause Diagnostician",
        sender_role="Reproduction",
        content="아마 캐시 만료 처리 문제로 보입니다.\n\n## 요지\n- 만료 처리 의심",
        round_number=1,
        tool_calls=[],
        turn_meta=turns.meta(turns.KIND_SPEECH, stance="diagnostician", verified=True),
    )
    [attempt] = diagnostic_loop.collect_round_attempts([stale])
    assert [verified for _, verified, _ in attempt.diagnostic_notes] == [False]


def test_execution_tools_waiting_behind_the_loader_count_but_catalog_words_do_not():
    shelf = ToolShelf(waiting={"sandbox__execute_python_code": EXEC_SCHEMA})
    # 요청에 실리는 것은 불러오기 도구뿐이지만, 불러오면 부를 수 있는 실행 도구도 셉니다.
    assert diagnostic_loop.available_execution_tools(shelf.offered()) == []
    names = diagnostic_loop.available_execution_tools(shelf.reachable())
    assert names == ["sandbox__execute_python_code"]
    prompt = diagnostic_loop.diagnostic_retry_prompt(names, to_load=list(shelf.waiting))
    assert f"`{LOAD_TOOLS_TOOL}` 로 먼저 불러오세요" in prompt
    assert "먼저 불러오세요" not in diagnostic_loop.diagnostic_retry_prompt(names)

    # 카탈로그 설명에 `lint`·`compile` 같은 낱말이 있어도 그 도구가 실행 도구가 되지는 않습니다.
    docs = {
        "type": "function",
        "function": {"name": "docs__search", "description": "Search lint and compile guides", "parameters": {}},
    }
    assert diagnostic_loop.available_execution_tools(ToolShelf(waiting={"docs__search": docs}).reachable()) == []


# ===========================================================================
# 4. 구조화된 [시도·검증 이력] 추출 · 요지 압축 생존성 · 시그니처 정규화
# ===========================================================================


def test_signature_normalization_survives_line_shifts_and_attempt_history_survives_digest_folding():
    obs_r1 = diagnostic_loop.parse_execution_call({
        "tool_name": "sandbox__execute_python_code",
        "status": "success",
        "output": _sandbox_failure_output(line_no=42),
    })
    obs_r2 = diagnostic_loop.parse_execution_call({
        "tool_name": "sandbox__execute_python_code",
        "status": "success",
        "output": _sandbox_failure_output(line_no=49),
    })
    assert obs_r1.failed is True and obs_r2.failed is True
    assert obs_r1.locations == ("src/cache.py:42 (get_item)",)
    assert obs_r2.locations == ("src/cache.py:49 (get_item)",)
    # 증상 패치로 줄 번호가 42 -> 49 로 바뀌어도 동일 실패 시그니처로 매칭되어야 합니다.
    assert obs_r1.signature == obs_r2.signature == "exec:keyerror: 'stale_token'@cache.py"

    pool = _make_pool()
    ctx = EngineContext(agent_pool=pool, llm_caller=FakeLLMCaller(), session_factory=lambda: None)
    conv = ConversationMemory(ctx)

    long_coder_prose = ("구현 세부 설명입니다. " * 90) + "\n\n## 요지\n- 캐시 조회 함수 초안 작성"
    long_diag_prose = ("재현 분석 상세 로그입니다. " * 90) + "\n\n## 요지\n- 만료 토큰 조회 시 KeyError 확인"

    state = DebateState(
        session_id="hist-1",
        user_prompt="캐시 무효화 버그 수정",
        strategy="sequential_debate",
        max_rounds=3,
        current_round=3,
        turn_message_start=0,
        decision_ledger=_open_ledger(),
        messages=[
            DebateMessage(
                sender_key="user",
                sender_name="User",
                sender_role="User",
                content="캐시 무효화 버그 수정",
                round_number=0,
                msg_type="user",
                turn_meta=turns.meta(turns.KIND_OPENING),
            ),
            # Round 1 발언들 (길이가 길어 Round 3 에서는 본문이 `## 요지` 로 접힘)
            DebateMessage(
                sender_key="coder",
                sender_name="Senior Engineer",
                sender_role="Implementation",
                content=long_coder_prose,
                round_number=1,
                tool_calls=[{
                    "tool_name": "filesystem__write_file",
                    "arguments": {"path": "src/cache.py", "content": "x = 1"},
                    "status": "success",
                    "output": "written",
                }],
                turn_meta=turns.meta(turns.KIND_SPEECH, stance="proponent"),
            ),
            DebateMessage(
                sender_key="diagnostician",
                sender_name="Root-Cause Diagnostician",
                sender_role="Reproduction",
                content=long_diag_prose,
                round_number=1,
                tool_calls=[{
                    "tool_name": "sandbox__execute_python_code",
                    "arguments": {"code": "get_item('stale_token')"},
                    "status": "success",
                    "output": _sandbox_failure_output(line_no=42),
                }],
                turn_meta=turns.meta(turns.KIND_SPEECH, stance="diagnostician"),
            ),
            # coder 가 Round 2 에 발언한 기록이 있어 Round 1 기록은 fresh_start 이전으로 밀려 `## 요지` 로 압축됨
            DebateMessage(
                sender_key="coder",
                sender_name="Senior Engineer",
                sender_role="Implementation",
                content="2라운드 임시 방어 코드 추가\n\n## 요지\n- 키 접두사 보정",
                round_number=2,
                tool_calls=[{
                    "tool_name": "filesystem__edit_file",
                    "arguments": {"path": "src/cache.py"},
                    "status": "success",
                    "output": "edited",
                }],
                turn_meta=turns.meta(turns.KIND_SPEECH, stance="proponent"),
            ),
        ],
    )

    coder = pool.get("coder")
    assert coder is not None
    prompt = conv.build_context_for_agent(state, coder, "이번 차례 지침")
    transcript_text = "\n".join(m["content"] for m in prompt[:-1])
    last_turn_prompt = prompt[-1]["content"]

    # Round 1 진단 발언 원문은 `## 요지` 로 압축되었더라도,
    assert "전문" in transcript_text and "요지만 싣습니다" in transcript_text
    # 마지막 사용자 메시지의 `[시도·검증 이력]` 에는 수정 파일과 정확한 실행 실패 위치가 살아 있어야 합니다!
    assert diagnostic_loop.ATTEMPT_HISTORY_HEADING in last_turn_prompt
    assert "`src/cache.py` (Senior Engineer)" in last_turn_prompt
    assert "src/cache.py:42 (get_item)" in last_turn_prompt
    assert "KeyError: 'stale_token'" in last_turn_prompt
    # 첫 번째 목표 메시지(프롬프트 캐시 고정 영역)에는 라운드별 시도 이력이 섞이지 않아야 합니다 (ADR-045).
    assert diagnostic_loop.ATTEMPT_HISTORY_HEADING not in prompt[0]["content"]


def test_pure_conceptual_debate_without_tools_leaves_attempt_history_and_stagnation_empty():
    msgs = [
        DebateMessage(
            sender_key="architect",
            sender_name="Architect",
            sender_role="Arch",
            content="이벤트 소싱이 좋겠습니다.",
            round_number=1,
            turn_meta=turns.meta(turns.KIND_SPEECH, stance="proponent"),
        ),
        DebateMessage(
            sender_key="critic",
            sender_name="Critic",
            sender_role="Review",
            content="운영 복잡도가 높습니다.",
            round_number=1,
            turn_meta=turns.meta(turns.KIND_SPEECH, stance="critic"),
        ),
        DebateMessage(
            sender_key="architect",
            sender_name="Architect",
            sender_role="Arch",
            content="아웃박스 패턴으로 단순화합시다.",
            round_number=2,
            turn_meta=turns.meta(turns.KIND_SPEECH, stance="proponent"),
        ),
        DebateMessage(
            sender_key="critic",
            sender_name="Critic",
            sender_role="Review",
            content="지연 시간 기준을 더 정해야 합니다.",
            round_number=2,
            turn_meta=turns.meta(turns.KIND_SPEECH, stance="critic"),
        ),
    ]
    state = DebateState(
        session_id="conceptual",
        user_prompt="아키텍처 토론",
        current_round=3,
        messages=msgs,
        decision_ledger=_open_ledger("지연 시간 목표치 미확정"),
    )
    assert diagnostic_loop.build_attempt_history(state) == ""
    assert (
        diagnostic_loop.observe_stagnation(
            msgs,
            round_num=2,
            ledger_by_round={1: _open_ledger("지연 시간 목표치 미확정"), 2: _open_ledger("지연 시간 목표치 미확정")},
        )
        is None
    )


# ===========================================================================
# 5. 외부 정체 관찰자 E2E 재설계 에스컬레이션 & 중단 재개(ADR-024) 멱등성
# ===========================================================================


PARTICIPANT_BIAS_PHRASE = "캐시 키 접두사만 살짝 바꾸면 완벽히 해결됩니다"
INITIAL_PLAN_TEXT = "초기 계획: 기존 인메모리 딕셔너리의 키 문자열만 부분 보정한다."
REDESIGNED_PLAN_TEXT = (
    "재설계 계획: 인메모리 부분 패치를 폐기하고, 세대 번호(generation counter) 기반 원자적 무효화 계층으로 교체한다."
)


class _StagnationScenarioCaller(FakeLLMCaller):
    """Round 1·2 연속 동일 실행 실패 -> Round 2 종료 시 재설계 -> Round 3 해결 시나리오 대역."""

    def __init__(self, *, crash_on_round3_start: bool = False, crash_on_redesign: bool = False) -> None:
        super().__init__()
        self.mcp_manager = _StubMCP([{
            "type": "function",
            "function": {
                "name": "sandbox__execute_python_code",
                "description": "Run code in sandbox",
                "parameters": {"type": "object", "properties": {}},
            },
        }])
        self.crash_on_round3_start = crash_on_round3_start
        self.crash_on_redesign = crash_on_redesign
        self.redesign_prompts: List[str] = []
        self.round3_coder_messages: List[List[Dict[str, Any]]] = []
        self.diag_round_calls = 0
        self.coder_round_calls = 0

    def resolve_tool_servers(self, agent: Agent) -> List[str]:
        return list(agent.allowed_mcp_servers or [])

    async def call_agent(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        custom_instructions: str = "",
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        last = messages[-1]["content"] if messages else ""

        if memory.LEDGER_PROMPT_MARKER in last:
            if "Round 3" in last or "세대 번호" in last:
                return LEDGER_REPLY, []
            return _open_ledger(), []

        if diagnostic_loop.REDESIGN_PROMPT_MARKER in last:
            self.redesign_prompts.append(last)
            if self.crash_on_redesign:
                raise asyncio.CancelledError()
            return REDESIGNED_PLAN_TEXT, []

        if "최종 합의 보고서" in last:
            return SYNTHESIS_REPLY, []

        if agent.key == "orchestrator":
            return INITIAL_PLAN_TEXT, []

        if self.crash_on_round3_start and "Round 3 of 3" in last:
            raise asyncio.CancelledError()

        if agent.key == "coder":
            self.coder_round_calls += 1
            if "Round 3 of 3" in last:
                self.round3_coder_messages.append(messages)
            write_log = {
                "tool_name": "filesystem__write_file",
                "arguments": {"path": "src/cache.py", "content": f"# patch {self.coder_round_calls}\n"},
                "status": "success",
                "output": "ok",
            }
            if on_tool_call is not None:
                res = on_tool_call(write_log)
                if asyncio.iscoroutine(res):
                    await res
            return (
                f"{PARTICIPANT_BIAS_PHRASE} (패치 #{self.coder_round_calls})\n\n"
                "## 요지\n- `src/cache.py` 패치 적용",
                [write_log],
            )

        if agent.key == "diagnostician":
            self.diag_round_calls += 1
            is_round_3 = "Round 3 of 3" in last
            output = (
                _sandbox_pass_output()
                if is_round_3
                else _sandbox_failure_output(line_no=40 + self.diag_round_calls * 5)
            )
            exec_log = {
                "tool_name": "sandbox__execute_python_code",
                "arguments": {"code": "from src.cache import get_item; get_item('stale_token')"},
                "status": "success",
                "output": output,
            }
            if on_tool_call is not None:
                res = on_tool_call(exec_log)
                if asyncio.iscoroutine(res):
                    await res
            verdict_text = (
                "재설계된 세대 번호 캐시 검증 통과!\n\n## 요지\n- 샌드박스 재현 테스트 통과"
                if is_round_3
                else "재현 테스트 실행 결과 여전히 `KeyError: 'stale_token'` 이 발생합니다.\n\n## 요지\n- 동일 KeyError 재현"
            )
            return verdict_text, [exec_log]

        return f"{PARTICIPANT_BIAS_PHRASE}\n\n## 요지\n- 검토 의견", []


@pytest.mark.asyncio
async def test_external_observer_escalates_two_consecutive_failures_to_isolated_redesign():
    pool = _make_pool()
    caller = _StagnationScenarioCaller()

    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"stagnation-e2e-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid,
            title="정체 에스컬레이션 E2E",
            strategy="sequential_debate",
            max_rounds=3,
            active_agents=["orchestrator", "architect", "coder", "critic", "diagnostician"],
        ))
        await db.commit()

    events: List[Dict[str, Any]] = []

    async def on_event(ev: Dict[str, Any]) -> None:
        events.append(ev)

    state = await OrchestratorEngine(agent_pool=pool, llm_caller=caller).run_turn(
        session_id=sid,
        user_prompt="멀티 워커 캐시 만료 시 KeyError 버그를 근본적으로 해결해 줘",
        on_event=on_event,
    )

    # 1) 외부 정체 관찰자가 정확히 Round 2 종료 시점에만 1회 재설계를 발동해야 합니다.
    assert len(caller.redesign_prompts) == 1
    redesign_input = caller.redesign_prompts[0]
    assert INITIAL_PLAN_TEXT in redesign_input
    assert "KeyError: 'stale_token'" in redesign_input
    assert diagnostic_loop.ATTEMPT_HISTORY_HEADING in redesign_input
    # 핵심 불변식: 참여 에이전트들이 주고받은 편향된 자기 합리화 문장은 재설계 프롬프트에 섞이면 안 됩니다!
    assert PARTICIPANT_BIAS_PHRASE not in redesign_input

    # 2) 이벤트 및 DB 메시지 검증 (`KIND_STAGNATION` + 새 `KIND_PLAN`)
    stag_events = [e for e in events if e["type"] == EventType.STAGNATION_DETECTED]
    assert len(stag_events) == 1
    assert stag_events[0]["round"] == 2
    assert stag_events[0]["streak"] == 2
    assert stag_events[0]["rounds"] == [1, 2]
    assert stag_events[0]["modified_files"] == ["src/cache.py"]

    async with get_session_factory(db_url)() as db:
        rows = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()

    stag_notes = [m for m in rows if turns.kind_of(m) == turns.KIND_STAGNATION]
    assert len(stag_notes) == 1
    assert "[정체 감지 · 재설계 에스컬레이션]" in stag_notes[0].content

    plans = [m for m in rows if turns.kind_of(m) == turns.KIND_PLAN]
    assert len(plans) == 2
    assert plans[0].round_number == 0 and INITIAL_PLAN_TEXT in plans[0].content
    assert plans[1].round_number == 2 and REDESIGNED_PLAN_TEXT in plans[1].content

    # 3) Round 3 에 들어간 구현 에이전트는 상단 고정 계획으로 새 재설계 계획을 받고,
    #    실패한 초기 계획은 대체 플레이스홀더로 접혀야 합니다.
    assert len(caller.round3_coder_messages) == 1
    r3_msgs = caller.round3_coder_messages[0]
    assert REDESIGNED_PLAN_TEXT in r3_msgs[0]["content"]
    r3_transcript = "\n".join(m["content"] for m in r3_msgs[1:-1])
    assert memory.SUPERSEDED_PLAN_PLACEHOLDER in r3_transcript
    assert state.messages[state.plan_index].content == REDESIGNED_PLAN_TEXT


@pytest.mark.asyncio
async def test_resuming_turn_after_stagnation_escalation_keeps_redesigned_plan_without_duplicate_escalation():
    pool = _make_pool()
    crashing_caller = _StagnationScenarioCaller(crash_on_round3_start=True)

    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"stagnation-resume-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid,
            title="재설계 후 재개",
            strategy="sequential_debate",
            max_rounds=3,
            active_agents=["orchestrator", "coder", "critic", "diagnostician"],
        ))
        await db.commit()

    # Round 2 종료 직후 정체 에스컬레이션(`KIND_STAGNATION` + 새 `KIND_PLAN`)까지 기록된 뒤 Round 3 첫 발언에서 중단됨
    with pytest.raises(asyncio.CancelledError):
        await OrchestratorEngine(agent_pool=pool, llm_caller=crashing_caller).run_turn(
            session_id=sid,
            user_prompt="멀티 워커 캐시 만료 시 KeyError 버그 수정",
        )
    assert len(crashing_caller.redesign_prompts) == 1

    async with get_session_factory(db_url)() as db:
        turn = (await db.execute(
            select(TurnModel).where(TurnModel.session_id == sid)
        )).scalar_one()
        await turns.mark_interrupted_turns(db)
        turn_id = turn.id

    # 재개 시 이미 기록된 Round 2 에스컬레이션을 중복 실행하지 않고, 새 계획을 고정한 채 Round 3을 마무리해야 합니다.
    resuming_caller = _StagnationScenarioCaller(crash_on_round3_start=False)
    state = await OrchestratorEngine(agent_pool=pool, llm_caller=resuming_caller).resume_turn(
        session_id=sid,
        turn_id=turn_id,
        mode="continue",
    )

    assert len(resuming_caller.redesign_prompts) == 0
    assert len(resuming_caller.round3_coder_messages) == 1
    assert REDESIGNED_PLAN_TEXT in resuming_caller.round3_coder_messages[0][0]["content"]
    assert state.messages[state.plan_index].content == REDESIGNED_PLAN_TEXT

    async with get_session_factory(db_url)() as db:
        rows = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
    assert sum(1 for m in rows if turns.kind_of(m) == turns.KIND_STAGNATION) == 1
    assert sum(1 for m in rows if turns.kind_of(m) == turns.KIND_PLAN) == 2
    assert any(turns.kind_of(m) == turns.KIND_SYNTHESIS for m in rows)


@pytest.mark.asyncio
async def test_resuming_a_turn_cut_while_writing_the_redesign_writes_only_the_plan():
    pool = _make_pool()
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"stagnation-cut-redesign-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid,
            title="재설계 도중 중단",
            strategy="sequential_debate",
            max_rounds=3,
            active_agents=["orchestrator", "coder", "critic", "diagnostician"],
        ))
        await db.commit()

    # Round 2 를 닫으며 정체 안내까지 남긴 뒤, 재설계 계획을 쓰는 도중에 끊깁니다.
    cut_caller = _StagnationScenarioCaller(crash_on_redesign=True)
    with pytest.raises(asyncio.CancelledError):
        await OrchestratorEngine(agent_pool=pool, llm_caller=cut_caller).run_turn(
            session_id=sid, user_prompt="멀티 워커 캐시 만료 시 KeyError 버그 수정",
        )
    async with get_session_factory(db_url)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        await turns.mark_interrupted_turns(db)
        turn_id = turn.id
        rows = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().all()
    assert sum(1 for m in rows if turns.kind_of(m) == turns.KIND_STAGNATION) == 1
    assert sum(1 for m in rows if turns.kind_of(m) == turns.KIND_PLAN) == 1

    # 이어 가면 안내는 다시 남기지 않고 재설계 계획만 다시 써서, Round 3 을 새 계획으로 돕니다.
    resuming_caller = _StagnationScenarioCaller()
    events: List[Dict[str, Any]] = []

    async def on_event(ev: Dict[str, Any]) -> None:
        events.append(ev)

    state = await OrchestratorEngine(agent_pool=pool, llm_caller=resuming_caller).resume_turn(
        session_id=sid, turn_id=turn_id, mode="continue", on_event=on_event,
    )
    assert len(resuming_caller.redesign_prompts) == 1
    assert not [e for e in events if e["type"] == EventType.STAGNATION_DETECTED]
    assert state.messages[state.plan_index].content == REDESIGNED_PLAN_TEXT
    assert len(resuming_caller.round3_coder_messages) == 1
    assert REDESIGNED_PLAN_TEXT in resuming_caller.round3_coder_messages[0][0]["content"]

    async with get_session_factory(db_url)() as db:
        rows = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().all()
    assert sum(1 for m in rows if turns.kind_of(m) == turns.KIND_STAGNATION) == 1
    assert sum(1 for m in rows if turns.kind_of(m) == turns.KIND_PLAN) == 2
    assert any(turns.kind_of(m) == turns.KIND_SYNTHESIS for m in rows)


def test_blocked_execution_call_does_not_count_as_verified_and_post_escalation_window_resets():
    # 보안 게이트에서 차단(denied)된 실행 도구 호출은 검증 증거로 인정하면 안 됩니다.
    blocked_call = {
        "tool_name": "sandbox__execute_python_code",
        "arguments": {"code": "open('src/cache.py', 'w').write('x')"},
        "status": "error",
        "security": {"decision": "denied", "risk": "write", "rule": "write(**)"},
        "output": "[보안 거부] write(**)",
    }
    assert diagnostic_loop.executed_verification_calls([blocked_call]) == []

    # Round 2 에스컬레이션 직후에는 최소 N(2) 라운드가 새로 쌓이기 전(Round 3 단독 실패)에 재에스컬레이션하지 않으며,
    # Round 3·4 두 라운드가 다시 연속 동일 실패할 때에만 2차 에스컬레이션을 발동합니다.
    def _diag_fail(r: int) -> DebateMessage:
        return DebateMessage(
            sender_key="diagnostician",
            sender_name="Diag",
            sender_role="Diag",
            content="여전히 재현됨",
            round_number=r,
            tool_calls=[{
                "tool_name": "sandbox__execute_python_code",
                "status": "success",
                "output": _sandbox_failure_output(line_no=10 + r),
            }],
            turn_meta=turns.meta(turns.KIND_SPEECH, stance="diagnostician"),
        )

    msgs = [
        _diag_fail(1),
        _diag_fail(2),
        DebateMessage(
            sender_key="orchestrator",
            sender_name="Orch",
            sender_role="Mod",
            content="[정체 감지]",
            round_number=2,
            msg_type="orchestrator",
            turn_meta=turns.meta(turns.KIND_STAGNATION, round=2, streak=2),
        ),
        _diag_fail(3),
    ]
    assert diagnostic_loop.observe_stagnation(msgs, round_num=3) is None

    msgs.append(_diag_fail(4))
    second = diagnostic_loop.observe_stagnation(msgs, round_num=4)
    assert second is not None
    assert second.round_number == 4 and second.rounds == (3, 4) and second.streak == 2


def test_roster_preview_and_stance_labels_include_diagnostician():
    from app.ui.components.roster.common import STANCE_COLORS, STANCE_LABELS, STANCE_PREVIEW_CLASSES
    from app.ui.components.roster.state import RosterState

    assert STANCE_LABELS["diagnostician"] == "진단자"
    assert "diagnostician" in STANCE_COLORS
    assert "diagnostician" in STANCE_PREVIEW_CLASSES

    roster_seq = RosterState(agent_pool=_make_pool(), strategy_name="sequential_debate")
    assert [a.key for a in roster_seq.speaking_order()] == ["architect", "coder", "critic", "diagnostician"]

    roster_adv = RosterState(agent_pool=_make_pool(), strategy_name="adversarial_debate")
    assert [a.key for a in roster_adv.speaking_order()] == ["architect", "critic", "coder", "diagnostician"]
