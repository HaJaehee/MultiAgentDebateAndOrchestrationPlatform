"""개별 모델의 연결 실패 → 전역 기본 모델 (app/agents/llm_fallback.py, ADR-030).

1. 기본은 꺼져 있고, 켜도 물러설 곳이 있는 에이전트만 물러선다. 에이전트마다 막을 수 있다.
2. 바뀌는 것은 연결과 그 모델의 용량뿐이다. 페르소나·도구·스킬은 그대로다.
3. 연결의 문제일 때만 물러선다. 한도 초과·요청 거절에서는 물러서지 않는다.
4. 모델이 아무것도 내놓기 전에만 물러선다. 글이 흘렀거나 도구를 부른 뒤, 이어 가는 발언에서는 아니다.
5. 실패한 연결은 잠깐 건너뛰고, 시간이 지나면 다시 불러 본다 (차단기).
6. 물러설 때마다 알리고, 누가 답했는지 발언 기록·화면·저장 문서에 남는다.
7. 기본 모델도 실패하면 지어내지 않고 두 이유를 함께 올린다.
"""

import asyncio
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest

from app.agents import connection as conn, llm as llm_module
from app.agents import llm_fallback as fb
from app.agents import model_status as ms
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_types import LLMUnavailableError
from app.agents.llm_gate import LLMGate
from app.agents.pool import AgentPool
from app.config import AgentConfig, RootConfig, SequentialThinkingConfig
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.export import build_session_markdown
from app.orchestration import turns
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.turn_run import TurnRun
from app.orchestration.state import FALLBACK_BADGE, fallback_sentence
from tests.fake_llm import FakeLLMCaller

GLOBAL = {"model": "openai/shared", "api_base": "http://shared.local/v1", "api_key": "sk-shared-secret-0001"}


def make_config(*, enabled: bool = True, retry_after: float = 60, llm: Optional[dict] = None,
                agents: Optional[dict] = None) -> RootConfig:
    return RootConfig.model_validate({
        "llm": {**(GLOBAL if llm is None else llm), "fallback": {"enabled": enabled, "retry_after": retry_after}},
        "agents": agents or {
            "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
            "critic": {"name": "Critic", "role": "Review", "model": "openai/critic-only",
                       "api_key": "sk-critic-secret-0002", "max_context_window": 200000, "max_tokens": 8000},
        },
    })


def agent_of(config: RootConfig, key: str) -> Agent:
    return Agent.from_config(key, config.agents[key])


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(monkeypatch):
    """폴백을 켠 설정, 이 시험만의 차단기와 관문."""
    state: Dict[str, Any] = {"config": make_config(), "clock": Clock()}
    breaker = fb.FallbackBreaker(clock=state["clock"])
    state["breaker"] = breaker
    monkeypatch.setattr(fb, "get_config", lambda: state["config"])
    monkeypatch.setattr(fb, "get_fallback_breaker", lambda: breaker)
    gate = LLMGate(0)
    monkeypatch.setattr(llm_module, "get_llm_gate", lambda: gate)
    monkeypatch.setattr(ms, "get_llm_gate", lambda: gate)
    return state


def connection_error(message: str = "Connection error.") -> Exception:
    class APIConnectionError(Exception):
        pass

    return APIConnectionError(message)


def coded_error(status: int, message: str = "nope") -> Exception:
    error = RuntimeError(message)
    error.status_code = status  # type: ignore[attr-defined]
    return error


# ---------------------------------------------------------------- 1·2. 누가, 무엇으로 물러서는가


def test_fallback_is_off_unless_asked_for():
    assert RootConfig.model_validate({"agents": {"orchestrator": {"name": "O", "role": "M"}}}).llm.fallback.enabled is False
    config = make_config(enabled=False)
    assert fb.route_for(agent_of(config, "critic"), config) is None


def test_only_an_agent_with_its_own_connection_has_somewhere_to_go():
    config = make_config()
    assert fb.route_for(agent_of(config, "orchestrator"), config) is None, "이미 전역 연결을 씁니다"
    route = fb.route_for(agent_of(config, "critic"), config)
    assert route is not None and route.substitute.model == "openai/shared"
    assert route.connection == conn.connection_id(config.agents["critic"]), "연결 상태 창의 줄과 같은 이름표"

    no_global = make_config(llm={"model": "openai/shared"})  # 전역에 주소도 키도 없음
    assert fb.route_for(agent_of(no_global, "critic"), no_global) is None


def test_an_agent_can_opt_out():
    config = make_config(agents={
        "orchestrator": {"name": "O", "role": "M"},
        "critic": {"name": "C", "role": "R", "model": "openai/in-house", "api_key": "k", "allow_fallback": False},
    })
    assert config.agents["critic"].allow_fallback is False
    assert fb.route_for(agent_of(config, "critic"), config) is None


def test_the_substitute_changes_the_connection_and_capacity_only():
    config = make_config(
        llm={**GLOBAL, "max_context_window": 32000, "max_tokens": 2000, "timeout": 45,
             "extra_body": {"priority": "low"}, "extra_headers": {"x-team": "shared"}},
        agents={
            "orchestrator": {"name": "O", "role": "M"},
            "critic": {
                "name": "Critic", "role": "Review", "model": "anthropic/claude-x", "api_key": "sk-critic-secret-0002",
                "api_base": "http://critic.local/v1", "max_context_window": 200000, "max_tokens": 8000,
                "timeout": 300, "temperature": 0.1, "system_prompt": "비판적으로 검토합니다.",
                "allowed_mcp_servers": ["filesystem"], "allowed_skills": ["review"], "max_tool_iterations": 7,
                "extra_body": {"top_k": 5},
            },
        },
    )
    critic = agent_of(config, "critic")
    substitute = fb.substitute_for(critic, config.llm)
    assert substitute is not None
    # 연결과 그 모델의 용량·전송 설정은 전역 것.
    assert (substitute.model, substitute.api_base, substitute.api_key) == (
        "openai/shared", "http://shared.local/v1", "sk-shared-secret-0001")
    assert (substitute.max_context_window, substitute.max_tokens, substitute.timeout) == (32000, 2000, 45)
    assert substitute.extra_body == {"priority": "low"} and substitute.extra_headers == {"x-team": "shared"}
    # 사람과 일은 그대로.
    assert (substitute.key, substitute.name, substitute.role) == ("critic", "Critic", "Review")
    assert substitute.system_prompt == "비판적으로 검토합니다." and substitute.temperature == 0.1
    assert substitute.allowed_mcp_servers == ["filesystem"] and substitute.allowed_skills == ["review"]
    assert substitute.max_tool_iterations == 7
    assert critic.model == "anthropic/claude-x", "원본은 건드리지 않습니다"


def test_provider_bound_thinking_options_do_not_follow_the_agent():
    config = make_config(agents={
        "orchestrator": {"name": "O", "role": "M"},
        "critic": {"name": "C", "role": "R", "model": "anthropic/claude-x", "api_key": "k",
                   "sequential_thinking": {"enabled": True, "mode": "native", "thinking_budget_tokens": 4000}},
        "coder": {"name": "D", "role": "R", "model": "openai/coder", "api_key": "k",
                  "sequential_thinking": {"enabled": True, "mode": "prompt", "max_steps": 9}},
    })
    native = fb.substitute_for(agent_of(config, "critic"), config.llm)
    assert native.sequential_thinking == SequentialThinkingConfig(), "전역의 설정을 따릅니다"
    prompt = fb.substitute_for(agent_of(config, "coder"), config.llm)
    assert prompt.sequential_thinking.max_steps == 9, "프롬프트 방식은 모델을 가리지 않습니다"


# ---------------------------------------------------------------- 3. 물러서는 실패


def test_only_connection_failures_qualify():
    assert fb.FALLBACK_STATES == {conn.AUTH, conn.MODEL, conn.UNREACHABLE, conn.TIMEOUT, conn.SERVER_ERROR}
    assert conn.RATE_LIMITED not in fb.FALLBACK_STATES, "한도 초과는 잠깐 뒤에 풀립니다"
    assert conn.REJECTED not in fb.FALLBACK_STATES, "요청의 문제는 기본 모델도 같은 이유로 실패합니다"


# ---------------------------------------------------------------- 5. 차단기


def test_a_failed_connection_is_skipped_for_a_while_then_tried_again():
    clock = Clock()
    breaker = fb.FallbackBreaker(clock=clock)
    assert breaker.state("c1", 60) is None
    breaker.trip("c1", conn.AUTH)
    assert breaker.state("c1", 60) == conn.AUTH and breaker.remaining("c1", 60) == pytest.approx(60)
    assert breaker.state("other", 60) is None
    clock.now += 59
    assert breaker.state("c1", 60) == conn.AUTH and breaker.remaining("c1", 60) == pytest.approx(1)
    clock.now += 1
    assert breaker.state("c1", 60) is None, "다시 불러 볼 때가 됐습니다"
    assert breaker.close("c1") is False, "시간이 다 된 것은 잊습니다"


def test_closing_and_zero_retry():
    breaker = fb.FallbackBreaker(clock=Clock())
    breaker.trip("c1", conn.TIMEOUT)
    assert breaker.close("c1") is True and breaker.state("c1", 60) is None
    breaker.trip("c1", conn.TIMEOUT)
    assert breaker.state("c1", 0) is None, "retry_after 가 0 이면 매번 원래 모델부터 부릅니다"


# ---------------------------------------------------------------- 4·5·6·7. call_agent 의 판단


class Scripted(LLMCaller):
    """`_call_agent_once` 만 대본으로 바꾼 호출기. 물러설지 말지의 판단은 진짜 코드가 합니다."""

    def __init__(self, script: Dict[str, Any]):
        super().__init__(mcp_manager=object())
        self.script = script
        self.calls: List[str] = []
        # False 로 두면 "LLM 호출이 아닌 곳에서 난 예외" 를 흉내 냅니다.
        self.llm_call_failed = True

    async def _call_agent_once(self, agent, messages, custom_instructions="", **kwargs):
        self.calls.append(agent.model)
        step = self.script[agent.model]
        if callable(step):
            step = step(kwargs)
            if asyncio.iscoroutine(step):
                step = await step
        if isinstance(step, BaseException):
            if isinstance(step, LLMUnavailableError):
                raise step
            # 진짜 루프에서는 `_complete_once` 가 세우는 표시: 실패가 LLM 호출에서 났습니다.
            progress = llm_module._SPEECH_PROGRESS.get()  # noqa: SLF001
            if progress is not None and self.llm_call_failed:
                progress.llm_failed = True
            raise LLMUnavailableError(agent, f"{type(step).__name__}: {step}") from step
        return step, []


async def call(caller: LLMCaller, agent: Agent, **kwargs):
    notices: List[fb.FallbackNotice] = []
    with fb.listening(notices.append):
        try:
            result = await caller.call_agent(agent, [{"role": "user", "content": "검토해 주세요"}], **kwargs)
        except LLMUnavailableError as exc:
            return exc, notices
    return result, notices


@pytest.mark.asyncio
async def test_a_dead_model_is_answered_by_the_global_one_and_everyone_is_told(setup):
    critic = agent_of(setup["config"], "critic")
    order: List[str] = []
    caller = Scripted({
        "openai/critic-only": connection_error(),
        "openai/shared": lambda kwargs: order.append("substitute called") or "기본 모델의 검토 의견",
    })
    notices: List[fb.FallbackNotice] = []

    def listener(notice):
        order.append("told")
        notices.append(notice)

    with fb.listening(listener):
        content, _ = await caller.call_agent(critic, [{"role": "user", "content": "검토"}])

    assert content == "기본 모델의 검토 의견"
    assert caller.calls == ["openai/critic-only", "openai/shared"]
    assert order == ["told", "substitute called"], "기본 모델을 부르기 전에 알립니다"
    (notice,) = notices
    assert (notice.agent_key, notice.intended_model, notice.model) == ("critic", "openai/critic-only", "openai/shared")
    assert notice.state == conn.UNREACHABLE and notice.label == "연결 실패" and notice.skipped is False
    meta = notice.as_meta()
    assert meta == {"fallback": True, "model": "openai/shared", "intended_model": "openai/critic-only",
                    "reason": conn.UNREACHABLE, "reason_label": "연결 실패"}
    assert "secret" not in json.dumps(meta) and "local" not in json.dumps(meta), "키와 주소는 기록에 담지 않습니다"


@pytest.mark.asyncio
@pytest.mark.parametrize("error, state", [
    (coded_error(401), conn.AUTH), (coded_error(404), conn.MODEL), (coded_error(503), conn.SERVER_ERROR),
    (asyncio.TimeoutError(), conn.TIMEOUT), (connection_error(), conn.UNREACHABLE),
])
async def test_every_connection_failure_falls_back(setup, error, state):
    caller = Scripted({"openai/critic-only": error, "openai/shared": "ok"})
    result, notices = await call(caller, agent_of(setup["config"], "critic"))
    assert result == ("ok", []) and [n.state for n in notices] == [state]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [coded_error(429, "rate limit"), coded_error(400, "unsupported value")])
async def test_rate_limits_and_rejected_requests_do_not_fall_back(setup, error):
    caller = Scripted({"openai/critic-only": error, "openai/shared": "ok"})
    critic = agent_of(setup["config"], "critic")
    result, notices = await call(caller, critic)
    assert isinstance(result, LLMUnavailableError) and not notices
    assert caller.calls == ["openai/critic-only"]
    assert setup["breaker"].state(conn.connection_id(critic), 60) is None, "건너뛸 이유도 아닙니다"


@pytest.mark.asyncio
async def test_a_failure_that_is_not_about_the_endpoint_is_left_alone(setup):
    critic = agent_of(setup["config"], "critic")
    caller = Scripted({"openai/critic-only": LLMUnavailableError(critic, "도구 호출 상한에 닿았습니다"),
                       "openai/shared": "ok"})
    result, notices = await call(caller, critic)
    assert isinstance(result, LLMUnavailableError) and result.reason == "도구 호출 상한에 닿았습니다"
    assert not notices and caller.calls == ["openai/critic-only"]


@pytest.mark.asyncio
async def test_an_error_from_elsewhere_in_the_tool_loop_is_not_a_dead_connection(setup):
    """연결 오류처럼 생겼더라도 LLM 호출에서 난 것이 아니면, 물러서지도 그 연결을 건너뛰지도 않습니다."""
    critic = agent_of(setup["config"], "critic")
    caller = Scripted({"openai/critic-only": connection_error(), "openai/shared": "ok"})
    caller.llm_call_failed = False
    result, notices = await call(caller, critic)
    assert isinstance(result, LLMUnavailableError) and not notices
    assert caller.calls == ["openai/critic-only"]
    assert setup["breaker"].state(conn.connection_id(critic), 60) is None


@pytest.mark.asyncio
async def test_no_fallback_once_text_has_reached_the_screen(setup):
    critic = agent_of(setup["config"], "critic")

    def streamed_then_cut(kwargs):
        llm_module._SPEECH_PROGRESS.get().streamed = True  # noqa: SLF001 - 진짜 루프가 세우는 표시
        return connection_error()

    caller = Scripted({"openai/critic-only": streamed_then_cut, "openai/shared": "ok"})
    result, notices = await call(caller, critic)
    assert isinstance(result, LLMUnavailableError) and not notices
    assert caller.calls == ["openai/critic-only"], "같은 답이 두 번 붙지 않게 합니다"
    assert setup["breaker"].state(conn.connection_id(critic), 60) == conn.UNREACHABLE, "다음 발언은 곧바로 기본 모델로"


@pytest.mark.asyncio
async def test_no_fallback_once_the_model_has_called_tools(setup):
    critic = agent_of(setup["config"], "critic")

    def a_round_then_cut(kwargs):
        llm_module._SPEECH_PROGRESS.get().rounds = 1  # noqa: SLF001
        return coded_error(503)

    caller = Scripted({"openai/critic-only": a_round_then_cut, "openai/shared": "ok"})
    result, notices = await call(caller, critic)
    assert isinstance(result, LLMUnavailableError) and not notices
    assert caller.calls == ["openai/critic-only"], "한 모델이 만든 도구 호출 기록을 다른 모델에게 넘기지 않습니다"


@pytest.mark.asyncio
async def test_a_resumed_speech_stays_on_its_own_model_even_when_it_is_known_to_be_down(setup):
    critic = agent_of(setup["config"], "critic")
    setup["breaker"].trip(conn.connection_id(critic), conn.AUTH)
    caller = Scripted({"openai/critic-only": coded_error(401), "openai/shared": "ok"})
    result, notices = await call(caller, critic, resume_state={"messages": [], "turn_anchor": ""})
    assert isinstance(result, LLMUnavailableError) and not notices
    assert caller.calls == ["openai/critic-only"]


@pytest.mark.asyncio
async def test_a_known_dead_connection_is_not_called_again_until_it_is_time(setup):
    critic = agent_of(setup["config"], "critic")
    caller = Scripted({"openai/critic-only": coded_error(401), "openai/shared": "ok"})
    await call(caller, critic)
    assert caller.calls == ["openai/critic-only", "openai/shared"]

    setup["clock"].now += 30
    result, notices = await call(caller, critic)
    assert result == ("ok", []) and caller.calls[2:] == ["openai/shared"], "타임아웃을 다시 치르지 않습니다"
    assert notices[0].skipped is True and notices[0].state == conn.AUTH

    setup["clock"].now += 31
    caller.script["openai/critic-only"] = "원래 모델이 돌아왔습니다"
    result, notices = await call(caller, critic)
    assert result == ("원래 모델이 돌아왔습니다", []) and not notices
    assert caller.calls[3:] == ["openai/critic-only"]


@pytest.mark.asyncio
async def test_when_the_global_model_fails_too_nothing_is_made_up(setup):
    critic = agent_of(setup["config"], "critic")
    caller = Scripted({"openai/critic-only": coded_error(401, "bad key"), "openai/shared": coded_error(503, "down")})
    error, notices = await call(caller, critic)
    assert isinstance(error, LLMUnavailableError)
    assert error.agent_key == "critic" and error.model == "openai/critic-only", "실패한 것은 이 에이전트의 발언입니다"
    assert "bad key" in error.reason and "openai/shared" in error.reason and "down" in error.reason
    assert len(notices) == 1, "기본 모델을 부르기 전에 알렸습니다 — 기록할지는 결과를 본 쪽이 정합니다"

    # 건너뛰는 중에 기본 모델이 실패해도 마찬가지입니다.
    error, _ = await call(caller, critic)
    assert isinstance(error, LLMUnavailableError) and "다시 부르지 않았습니다" in error.reason
    assert caller.calls == ["openai/critic-only", "openai/shared", "openai/shared"]


@pytest.mark.asyncio
async def test_with_fallback_off_the_call_goes_straight_through(setup):
    setup["config"] = make_config(enabled=False)
    critic = agent_of(setup["config"], "critic")
    seen: Dict[str, Any] = {}

    def remember(kwargs):
        seen.update(kwargs)
        return connection_error()

    caller = Scripted({"openai/critic-only": remember, "openai/shared": "ok"})
    on_tool_call, on_trim = (lambda log: None), (lambda dropped: None)
    result, notices = await call(caller, critic, on_tool_call=on_tool_call, on_context_trim=on_trim)
    assert isinstance(result, LLMUnavailableError) and not notices and caller.calls == ["openai/critic-only"]
    assert seen["on_tool_call"] is on_tool_call and seen["on_context_trim"] is on_trim, "콜백을 감싸지도 않습니다"


@pytest.mark.asyncio
async def test_what_the_first_attempt_already_announced_is_not_announced_twice(setup):
    """지정 스킬을 미리 부른 기록과 컨텍스트 생략 알림은, 처음부터 다시 부를 때 한 번만 나갑니다."""
    critic = agent_of(setup["config"], "critic")
    preload = {"tool_name": "skills__load_skill", "arguments": {"skill": "review"}, "output": "…", "status": "success"}
    after = {"tool_name": "filesystem__read_file", "arguments": {}, "output": "…", "status": "success"}

    async def primary(kwargs):
        await kwargs["on_tool_call"](preload)
        kwargs["on_context_trim"](3)
        return connection_error()

    async def substitute(kwargs):
        await kwargs["on_tool_call"](dict(preload))
        kwargs["on_context_trim"](5)
        await kwargs["on_tool_call"](after)
        return "ok"

    caller = Scripted({"openai/critic-only": primary, "openai/shared": substitute})
    announced: List[str] = []
    trims: List[int] = []
    result, _ = await call(caller, critic, on_tool_call=lambda log: announced.append(log["tool_name"]),
                           on_context_trim=trims.append)
    assert result == ("ok", [])
    assert announced == ["skills__load_skill", "filesystem__read_file"]
    assert trims == [5], "실패한 시도가 자른 것은 실제로 나간 요청이 아닙니다"

    # 원래 모델이 끝까지 간 경우에는 그 시도의 알림이 그대로 나갑니다.
    setup["breaker"].reset()

    async def works(kwargs):
        await kwargs["on_tool_call"](preload)
        kwargs["on_context_trim"](2)
        return "primary ok"

    announced.clear()
    trims.clear()
    caller.script["openai/critic-only"] = works
    result, notices = await call(caller, critic, on_tool_call=lambda log: announced.append(log["tool_name"]),
                                 on_context_trim=trims.append)
    assert result == ("primary ok", []) and not notices
    assert announced == ["skills__load_skill"] and trims == [2]


@pytest.mark.asyncio
async def test_a_listener_that_breaks_does_not_cost_the_speech(setup):
    caller = Scripted({"openai/critic-only": coded_error(401), "openai/shared": "ok"})

    def broken(notice):
        raise RuntimeError("화면이 닫혔습니다")

    with fb.listening(broken):
        content, _ = await caller.call_agent(agent_of(setup["config"], "critic"), [{"role": "user", "content": "x"}])
    assert content == "ok"
    assert fb.current_listener() is None, "블록을 나오면 알릴 곳도 사라집니다"


# ---------------------------------------------------------------- 진짜 LiteLLM · 로컬 가짜 서버


class _FakeLLM(BaseHTTPRequestHandler):
    """모델 이름으로 응답을 정하는 OpenAI 호환 서버 (스트리밍 포함)."""

    # 스트림을 chunked 로 보냅니다. 그래야 말하다 끊긴 것이 '정상 종료' 가 아니라 오류로 보입니다.
    protocol_version = "HTTP/1.1"
    seen: List[Dict[str, Any]] = []

    def _chunk(self, text: str) -> None:
        data = text.encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        type(self).seen.append({"model": body["model"], "stream": bool(body.get("stream"))})
        model = body["model"]
        errors = {"bad-key": (401, "Incorrect API key"), "limited": (429, "rate limit"), "broken": (500, "boom")}
        try:
            if model in errors:
                status, message = errors[model]
                payload = json.dumps({"error": {"message": message, "type": "x", "code": status}}).encode()
                return self._send(status, payload, "application/json")
            text = f"{model} 의 답변입니다"
            if not body.get("stream"):
                payload = json.dumps({
                    "id": "x", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                }).encode()
                return self._send(200, payload, "application/json")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            pieces = [text[: len(text) // 2], text[len(text) // 2:]]
            for index, piece in enumerate(pieces):
                chunk = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}]}
                self._chunk(f"data: {json.dumps(chunk)}\n\n")
                if model == "cut" and index == 0:
                    time.sleep(0.2)
                    self.close_connection = True  # 끝맺는 조각 없이 끊습니다 — 말하다 끊긴 것
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return None
            done = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self._chunk(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass
        return None


@pytest.fixture(scope="module")
def fake_llm():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()
    thread.join(timeout=5)


class _NoTools:
    """도구가 하나도 없는 MCP 런타임."""

    def get_openai_tools_for_servers(self, servers):
        return []


def live_config(base: str, critic_model: str) -> RootConfig:
    return make_config(
        llm={"model": "openai/shared", "api_base": base, "api_key": "sk-shared-secret-0001", "timeout": 10},
        agents={
            "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
            "critic": {"name": "Critic", "role": "Review", "model": f"openai/{critic_model}", "num_retries": 0},
        },
    )


@pytest.mark.asyncio
async def test_real_call_a_rejected_key_is_answered_by_the_global_model(setup, fake_llm):
    setup["config"] = live_config(fake_llm, "bad-key")
    _FakeLLM.seen.clear()
    chunks: List[str] = []
    notices: List[fb.FallbackNotice] = []
    with fb.listening(notices.append):
        content, logs = await LLMCaller(mcp_manager=_NoTools()).call_agent(
            agent_of(setup["config"], "critic"), [{"role": "user", "content": "검토해 주세요"}],
            on_chunk=chunks.append,
        )
    assert content == "shared 의 답변입니다" and "".join(chunks) == content and logs == []
    assert [n.state for n in notices] == [conn.AUTH]
    # 죽은 연결에 스트리밍 없이 다시 묻느라 시간을 쓰지 않았습니다.
    assert _FakeLLM.seen == [{"model": "bad-key", "stream": True}, {"model": "shared", "stream": True}]


@pytest.mark.asyncio
async def test_real_call_a_server_error_is_asked_once_more_without_streaming_first(setup, fake_llm):
    """스트리밍을 받지 않는 엔드포인트가 5xx 로 답하는 경우가 있어, 서버 오류는 한 번 더 물어본 뒤에 물러섭니다."""
    setup["config"] = live_config(fake_llm, "broken")
    _FakeLLM.seen.clear()
    notices: List[fb.FallbackNotice] = []
    with fb.listening(notices.append):
        content, _ = await LLMCaller(mcp_manager=_NoTools()).call_agent(
            agent_of(setup["config"], "critic"), [{"role": "user", "content": "검토해 주세요"}],
            on_chunk=lambda text: None,
        )
    assert content == "shared 의 답변입니다" and [n.state for n in notices] == [conn.SERVER_ERROR]
    # 5xx 는 클라이언트가 스스로 몇 번 더 보내기도 하므로 횟수가 아니라 순서를 봅니다.
    before, last = _FakeLLM.seen[:-1], _FakeLLM.seen[-1]
    assert last == {"model": "shared", "stream": True} and {s["model"] for s in before} == {"broken"}
    assert before[0]["stream"] is True and any(not s["stream"] for s in before), "스트리밍 없이도 물어본 뒤에 물러섰습니다"


@pytest.mark.asyncio
async def test_real_call_a_stream_cut_midway_is_not_restarted_on_another_model(setup, fake_llm):
    setup["config"] = live_config(fake_llm, "cut")
    _FakeLLM.seen.clear()
    chunks: List[str] = []
    notices: List[fb.FallbackNotice] = []
    with fb.listening(notices.append):
        with pytest.raises(LLMUnavailableError):
            await LLMCaller(mcp_manager=_NoTools()).call_agent(
                agent_of(setup["config"], "critic"), [{"role": "user", "content": "검토해 주세요"}],
                on_chunk=chunks.append,
            )
    assert chunks, "끊기기 전까지의 글은 화면에 흘렀습니다"
    assert not notices and [s["model"] for s in _FakeLLM.seen] == ["cut"]


@pytest.mark.asyncio
async def test_real_call_a_rate_limit_stays_a_failure(setup, fake_llm):
    setup["config"] = live_config(fake_llm, "limited")
    _FakeLLM.seen.clear()
    with pytest.raises(LLMUnavailableError) as raised:
        await LLMCaller(mcp_manager=_NoTools()).call_agent(
            agent_of(setup["config"], "critic"), [{"role": "user", "content": "검토해 주세요"}],
        )
    assert "RateLimit" in raised.value.reason
    assert "shared" not in {s["model"] for s in _FakeLLM.seen}


# ---------------------------------------------------------------- 6. 기록·화면·저장 문서


class FallingBack(FakeLLMCaller):
    """`critic` 의 발언만 전역 기본 모델이 대신 답한 것으로 치는 호출기."""

    def __init__(self, *, then_fail: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.then_fail = then_fail

    async def call_agent(self, agent, messages, *args, **kwargs):
        if agent.key == "critic" and "최종 합의 보고서" not in (messages[-1]["content"] if messages else ""):
            listener = fb.current_listener()
            if listener is not None:
                outcome = listener(fb.FallbackNotice(
                    agent_key=agent.key, agent_name=agent.name, intended_model="anthropic/claude-x",
                    model="openai/shared", state=conn.AUTH,
                ))
                if asyncio.iscoroutine(outcome):
                    await outcome
            if self.then_fail:
                raise LLMUnavailableError(agent, "기본 모델로도 응답을 받지 못했습니다")
        return await super().call_agent(agent, messages, *args, **kwargs)


def _pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=name, role=role, model="fake/model", api_key="test-key")
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("architect", "System Architect", "Architecture"),
            ("critic", "Quality Critic", "Review"),
        )
    })


async def _session() -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    sid = f"fallback-{uuid.uuid4().hex[:8]}"
    async with get_session_factory("sqlite+aiosqlite:///:memory:")() as db:
        db.add(SessionModel(id=sid, title="Fallback", strategy="sequential_debate", max_rounds=1,
                            active_agents=["orchestrator", "architect", "critic"]))
        await db.commit()
    return sid


@pytest.mark.asyncio
async def test_the_speech_record_says_who_actually_answered():
    sid = await _session()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=FallingBack())
    state = await engine.run_turn(session_id=sid, user_prompt="설계를 검토해 주세요", on_event=on_event)

    critic = next(m for m in state.messages if m.sender_key == "critic")
    assert critic.msg_type != "error"
    assert critic.llm_meta == {"fallback": True, "model": "openai/shared", "intended_model": "anthropic/claude-x",
                               "reason": conn.AUTH, "reason_label": "인증 실패"}
    assert all(m.llm_meta is None for m in state.messages if m.sender_key != "critic")
    assert state.fallback_agent_keys == ["critic"] and state.failed_agent_keys == [], "실패가 아닙니다"

    # 화면: 흘러나오는 카드에 먼저 알리고, 확정본에도 같은 기록이 실립니다.
    kinds = [(e["type"], (e.get("message") or {}).get("id") or e.get("message_id")) for e in events]
    told = kinds.index(("llm_fallback", critic.id))
    assert kinds.index(("message_stream_start", critic.id)) < told < kinds.index(("message_added", critic.id))
    added = next(e for e in events if e["type"] == "message_added" and e["message"]["id"] == critic.id)
    assert added["message"]["llm_meta"]["model"] == "openai/shared"
    done = next(e for e in events if e["type"] == "turn_completed")
    assert done["fallback_agents"] == ["critic"] and done["failed_agents"] == []

    # 기록: 새로 연 화면과 이어 가는 턴이 같은 사실을 읽습니다.
    async with get_session_factory("sqlite+aiosqlite:///:memory:")() as db:
        row = await db.get(MessageModel, critic.id)
        assert row.llm_meta["intended_model"] == "anthropic/claude-x"
    assert turns.fallback_agents(state.messages) == ["critic"]
    assert turns.fallback_agents([{"sender_key": "critic", "llm_meta": None}]) == []


@pytest.mark.asyncio
async def test_a_speech_nobody_answered_is_not_recorded_as_a_fallback():
    sid = await _session()
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=FallingBack(then_fail=True))
    state = await engine.run_turn(session_id=sid, user_prompt="설계를 검토해 주세요")
    critic = next(m for m in state.messages if m.sender_key == "critic")
    assert critic.msg_type == "error" and critic.llm_meta is None
    assert state.fallback_agent_keys == [] and state.failed_agent_keys == ["critic"]


def test_the_sentence_names_both_models_and_the_reason():
    meta = {"fallback": True, "model": "openai/shared", "intended_model": "anthropic/claude-x",
            "reason": "auth", "reason_label": "인증 실패"}
    sentence = fallback_sentence(meta)
    assert "anthropic/claude-x" in sentence and "openai/shared" in sentence and "인증 실패" in sentence
    assert fallback_sentence(None) == "" and fallback_sentence({"fallback": False}) == ""
    assert fallback_sentence("not a dict") == ""


def test_the_saved_document_says_so_above_the_speech():
    meta = {"fallback": True, "model": "openai/shared", "intended_model": "anthropic/claude-x",
            "reason": "auth", "reason_label": "인증 실패"}
    messages = [
        {"id": "m1", "sender_key": "architect", "sender_name": "Architect", "sender_role": "Design",
         "content": "설계안입니다", "round_number": 1, "msg_type": "agent"},
        {"id": "m2", "sender_key": "critic", "sender_name": "Critic", "sender_role": "Review",
         "content": "검토 의견입니다", "round_number": 1, "msg_type": "agent", "llm_meta": meta},
    ]
    document = build_session_markdown({"title": "T"}, messages)
    assert document.count(FALLBACK_BADGE) == 1
    note = document.index(FALLBACK_BADGE)
    assert document.index("Critic") < note < document.index("검토 의견입니다")
    assert "anthropic/claude-x" in document[note:document.index("검토 의견입니다")]


def test_a_screen_that_joins_midway_learns_about_it_too():
    run = TurnRun("s1", "prompt")
    run.apply({"type": "message_stream_start", "message": {"id": "m1", "sender_key": "critic", "content": ""}})
    meta = {"fallback": True, "model": "openai/shared", "intended_model": "anthropic/claude-x"}
    run.apply({"type": "llm_fallback", "message_id": "m1", "llm_meta": meta})
    assert run.messages[0]["llm_meta"] == meta
    run.apply({"type": "llm_fallback", "message_id": "unknown", "llm_meta": meta})  # 모르는 발언은 무시합니다

    run.apply({"type": "turn_completed", "failed_agents": [], "fallback_agents": ["critic"]})
    assert "기본 모델이 대신 답한 에이전트: critic" in run.status_text
    run.apply({"type": "turn_completed", "failed_agents": ["coder"], "fallback_agents": ["critic"]})
    assert "응답하지 못한 에이전트: coder" in run.status_text, "응답하지 못한 쪽이 더 급한 소식입니다"


def test_the_card_shows_a_badge_from_the_record_while_streaming_and_after():
    import inspect

    from app.ui.components.chat_feed.cards import CardTimeline
    from app.ui.components.chat_feed.feed import ChatFeed

    render = inspect.getsource(CardTimeline._render_card)
    assert "FALLBACK_BADGE" in render and 'self._set_fallback(info, msg.get("llm_meta"))' in render
    assert 'self._set_fallback(info, msg.get("llm_meta"))' in inspect.getsource(CardTimeline._finalize_streaming_message)
    assert callable(getattr(ChatFeed, "mark_fallback", None))
    # 세 화면이 흘러나오는 카드에 붙입니다.
    for path in ("app/ui/expert/events.py", "app/trial/pages/session.py", "app/easy/pages/session.py"):
        text = (ms_root() / path).read_text(encoding="utf-8")
        assert 'mark_fallback(event.get("message_id", ""), event.get("llm_meta"))' in text, path


def ms_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 연결 상태 창과 닿는 곳


@pytest.mark.asyncio
async def test_the_status_dialog_shows_the_fallback_and_a_good_probe_ends_it(setup):
    config = setup["config"]
    critic_id = conn.connection_id(config.agents["critic"])
    setup["breaker"].trip(critic_id, conn.AUTH)
    setup["clock"].now += 18

    states = {"openai/critic-only": conn.AUTH}

    async def prober(target):
        return ms.ModelStatus(target.id, states.get(target.model, conn.OK))

    board = ms.ModelStatusBoard(prober=prober)
    rows = {row.target.model: row for row in board.rows(config)}
    assert rows["openai/critic-only"].skip_seconds == pytest.approx(42)
    assert rows["openai/shared"].skip_seconds is None
    view = ms.describe_row(rows["openai/critic-only"], detail=False)
    assert "기본 모델이 대신 답하고 있습니다" in view["fallback"] and "42초" in view["fallback"]
    assert "fallback" not in ms.describe_row(rows["openai/shared"], detail=False)

    board.start(config)
    await board.wait()
    assert setup["breaker"].state(critic_id, 60) == conn.AUTH, "여전히 안 되면 그대로 건너뜁니다"

    # 키를 고친 뒤 다시 확인하면 (새 결과판 = 캐시 없이 바로 확인)
    states.clear()
    fresh = ms.ModelStatusBoard(prober=prober)
    fresh.start(config)
    await fresh.wait()
    assert setup["breaker"].state(critic_id, 60) is None, "응답하는 것을 확인했으면 원래 모델로 돌아갑니다"
    assert all("fallback" not in ms.describe_row(row, detail=False) for row in fresh.rows(config))


def test_with_fallback_off_the_dialog_shows_nothing_about_it(setup):
    setup["config"] = make_config(enabled=False)
    setup["breaker"].trip(conn.connection_id(setup["config"].agents["critic"]), conn.AUTH)
    assert all(row.skip_seconds is None for row in ms.ModelStatusBoard().rows(setup["config"]))
