"""모델 연결 상태 (app/agents/model_status.py, app/ui/components/model_status.py).

1. 같은 연결을 쓰는 에이전트는 한 줄로 묶이고, 모델이나 주소가 다른 에이전트는 따로 한 줄이 된다.
2. 확인 요청은 실제 호출과 같은 연결 정보로, 가장 가볍게 나간다.
3. 실패는 사람이 고칠 수 있는 갈래로 나뉜다 — 진짜 LiteLLM 이 로컬 가짜 서버를 부르게 해서 본다.
4. 서버 전체의 동시 요청 상한을 지킨다. 자리가 없으면 끼어들지 않고, 쥔 자리는 반드시 돌려준다.
5. 확인은 한 판만 돌고, 결과는 잠깐 다시 쓰이며, '다시 확인' 도 연달아 보내지 않는다.
6. 방문자에게 가는 자료에는 주소와 오류 원문이 없고, 키는 누구에게도 가지 않는다.
7. 세 화면(전문가·체험·쉬운)의 머리에 단추가 달려 있다.
"""

import asyncio
import inspect
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.agents import connection as conn, llm as llm_module
from app.agents import model_status as ms
from app.agents.llm_gate import LLMGate
from app.config import RootConfig

ROOT = Path(__file__).resolve().parents[1]
SHARED_KEY = "sk-shared-secret-0001"


def make_config(agents=None, llm=None) -> RootConfig:
    return RootConfig.model_validate({
        "llm": {"model": "openai/shared", "api_base": "http://llm.local/v1", "api_key": SHARED_KEY} if llm is None else llm,
        "agents": agents or {
            "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
            "architect": {"name": "Architect", "role": "Design"},
            "coder": {"name": "Coder", "role": "Code", "model": "openai/coder-only"},
            "critic": {"name": "Critic", "role": "Review", "api_base": "http://other.local/v1"},
            "off": {"name": "Disabled", "role": "x", "enabled": False, "model": "openai/never"},
        },
    })


def make_target(**overrides) -> ms.ModelTarget:
    values = dict(
        id="t1", model="openai/test", api_base="http://llm.local/v1", api_key=SHARED_KEY, api_version=None,
        provider=None, extra_headers=(), extra_body="{}", users=("전역 기본값",), is_live=True,
    )
    values.update(overrides)
    return ms.ModelTarget(**values)


@pytest.fixture
def gate(monkeypatch) -> LLMGate:
    """이 시험만의 관문 (상한 없음). 상한을 걸려면 `gate.configure(n)`."""
    gate = LLMGate(0)
    monkeypatch.setattr(ms, "get_llm_gate", lambda: gate)
    return gate


# ---------------------------------------------------------------- 1. 연결 묶기


def test_agents_sharing_a_connection_become_one_row():
    targets = ms.collect_targets(make_config())
    by_users = {t.users: t for t in targets}
    assert list(by_users) == [("전역 기본값", "Orchestrator", "Architect"), ("Coder",), ("Critic",)]
    assert by_users[("Coder",)].model == "openai/coder-only"
    assert by_users[("Critic",)].api_base == "http://other.local/v1"
    assert by_users[("Critic",)].model == "openai/shared", "모델은 같아도 주소가 다르면 다른 연결입니다"
    assert all(t.is_live for t in targets)
    assert len({t.id for t in targets}) == len(targets)
    assert not any("Disabled" in t.users or t.model == "openai/never" for t in targets), "꺼진 에이전트는 보지 않습니다"


def test_a_different_key_or_header_is_a_different_connection():
    config = make_config(agents={
        "orchestrator": {"name": "O", "role": "M"},
        "a": {"name": "A", "role": "x", "api_key": "sk-another-secret-0002"},
        "b": {"name": "B", "role": "x", "extra_headers": {"x-team": "blue"}},
    })
    assert [t.users for t in ms.collect_targets(config)] == [("전역 기본값", "O"), ("A",), ("B",)]


def test_targets_never_carry_the_key_in_what_gets_printed():
    for target in ms.collect_targets(make_config()):
        assert SHARED_KEY not in repr(target) and SHARED_KEY not in target.id
        assert target.api_key == SHARED_KEY, "확인 요청에는 써야 하므로 값은 들고 있습니다"


def test_without_any_connection_info_there_is_one_row_that_is_not_live():
    config = make_config(agents={"orchestrator": {"name": "O", "role": "M"}}, llm={})
    (target,) = ms.collect_targets(config)
    assert target.users == ("전역 기본값", "O") and not target.is_live
    assert target.endpoint == ms.DEFAULT_ENDPOINT


def test_keyless_local_runtimes_count_as_live():
    config = make_config(agents={"orchestrator": {"name": "O", "role": "M"}}, llm={"model": "ollama/llama3"})
    assert ms.collect_targets(config)[0].is_live


# ---------------------------------------------------------------- 2. 확인 요청의 모양


def test_probe_request_is_as_light_as_it_can_be():
    target = make_target(
        api_version="2024-06-01", provider="azure",
        extra_headers=(("x-team", "blue"),), extra_body=json.dumps({"priority": "low"}),
    )
    kwargs = ms.probe_kwargs(target, 12.0)
    assert kwargs == {
        "model": "openai/test", "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": ms.PROBE_MAX_TOKENS, "timeout": 12.0, "num_retries": 0, "drop_params": True,
        "api_key": SHARED_KEY, "api_base": "http://llm.local/v1", "api_version": "2024-06-01",
        "custom_llm_provider": "azure", "extra_headers": {"x-team": "blue"}, "extra_body": {"priority": "low"},
    }
    assert ms.PROBE_MAX_TOKENS <= 32
    # 연결과 무관한 이유로 거절되지 않게, 모델마다 받는 값이 다른 것은 보내지 않습니다.
    assert not {"temperature", "tools", "stream", "reasoning_effort", "thinking"} & set(kwargs)


def test_keyless_endpoints_get_the_same_placeholder_key_as_real_calls():
    kwargs = ms.probe_kwargs(make_target(api_key=None))
    assert kwargs["api_key"] == llm_module.LOCAL_API_KEY_PLACEHOLDER
    assert "api_key" not in ms.probe_kwargs(make_target(api_key=None, api_base=None, model="ollama/llama3"))


# ---------------------------------------------------------------- 3. 진짜 LiteLLM 으로 갈래 확인


class _FakeLLM(BaseHTTPRequestHandler):
    """모델 이름으로 응답을 정하는 OpenAI 호환 서버."""

    replies = {
        "ok": (200, 0.0, ""), "bad-key": (401, 0.0, f"Incorrect API key provided: {SHARED_KEY}"),
        "forbidden": (403, 0.0, "forbidden"), "missing": (404, 0.0, "The model `missing` does not exist"),
        "limited": (429, 0.0, "rate limit"), "bad-request": (400, 0.0, "unsupported value"),
        "broken": (500, 0.0, "boom"), "slow": (200, 3.0, ""),
    }
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        type(self).seen.append(body)
        status, delay, message = self.replies[body["model"]]
        time.sleep(delay)
        if status == 200:
            payload = {
                "id": "x", "object": "chat.completion", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        else:
            payload = {"error": {"message": message, "type": "x", "code": status}}
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # 기다리다 끊은 손님


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def fake_llm():
    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), _FakeLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    thread.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("model, expected", [
    ("ok", conn.OK), ("bad-key", conn.AUTH), ("forbidden", conn.AUTH), ("missing", conn.MODEL),
    ("limited", conn.RATE_LIMITED), ("bad-request", conn.REJECTED), ("broken", conn.SERVER_ERROR),
])
async def test_real_litellm_failures_land_in_the_right_bucket(fake_llm, gate, model, expected):
    _FakeLLM.seen.clear()
    result = await ms.probe(make_target(model=f"openai/{model}", api_base=fake_llm), timeout_seconds=10)
    assert result.state == expected, result.detail
    assert len(_FakeLLM.seen) == 1, "다시 시도하지 않습니다"
    assert _FakeLLM.seen[0]["max_tokens"] == ms.PROBE_MAX_TOKENS and "tools" not in _FakeLLM.seen[0]
    assert (result.latency_ms is not None) == (expected == conn.OK)
    assert SHARED_KEY not in result.detail, "Provider 가 되돌려 보낸 키도 가립니다"
    assert gate.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_a_server_that_is_down_is_unreachable_not_a_server_error(gate):
    result = await ms.probe(make_target(api_base=f"http://127.0.0.1:{_free_port()}/v1"), timeout_seconds=8)
    assert result.state == conn.UNREACHABLE, result.detail


@pytest.mark.asyncio
async def test_a_server_that_does_not_answer_in_time_is_a_timeout(fake_llm, gate):
    result = await ms.probe(make_target(model="openai/slow", api_base=fake_llm), timeout_seconds=0.5)
    assert result.state == conn.TIMEOUT, result.detail
    assert gate.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_an_unknown_provider_prefix_points_at_the_model_name(gate):
    result = await ms.probe(make_target(model="google/gemini-1.5-pro", api_base=None), timeout_seconds=5)
    assert result.state == conn.MODEL, result.detail


def test_classification_falls_back_to_status_codes_and_words():
    class Coded(Exception):
        def __init__(self, message, status_code=None):
            super().__init__(message)
            self.status_code = status_code

    assert conn.classify(Coded("nope", 401)) == conn.AUTH
    assert conn.classify(Coded("nope", 404)) == conn.MODEL
    assert conn.classify(Coded("nope", 429)) == conn.RATE_LIMITED
    assert conn.classify(Coded("nope", 502)) == conn.SERVER_ERROR
    assert conn.classify(Coded("API key not valid. Please pass a valid API key.", 400)) == conn.AUTH
    assert conn.classify(Coded("model 'x' not found", 400)) == conn.MODEL
    assert conn.classify(Coded("All connection attempts failed", 500)) == conn.UNREACHABLE
    assert conn.classify(asyncio.TimeoutError()) == conn.TIMEOUT
    assert conn.classify(RuntimeError("something else")) == conn.ERROR


def test_error_text_is_masked_and_short():
    target = make_target(extra_headers=(("x-gateway-token", "gw-token-abcdef123456"),))
    text = ms.safe_detail(RuntimeError(
        f"bad key {SHARED_KEY} (also sk-proj-abcd****wxyz and AIzaSyA1234567890abcdef) header gw-token-abcdef123456\n" + "x" * 900
    ), target)
    for secret in (SHARED_KEY, "sk-proj-abcd", "AIzaSyA1234567890abcdef", "gw-token-abcdef123456"):
        assert secret not in text
    assert "\n" not in text and len(text) <= 310


# ---------------------------------------------------------------- 4. 동시 요청 상한


@pytest.mark.asyncio
async def test_probe_holds_a_slot_while_it_calls_and_returns_it(gate, monkeypatch):
    seen = {}

    async def fake_completion(**kwargs):
        seen["snapshot"] = gate.snapshot()
        seen["kwargs"] = kwargs

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    result = await ms.probe(make_target())
    assert result.state == conn.OK
    assert seen["snapshot"]["active_holders"] == [ms.GATE_HOLDER]
    assert seen["kwargs"]["api_key"] == SHARED_KEY
    assert gate.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_probe_does_not_cut_in_when_the_gate_is_full(gate, monkeypatch):
    called = []

    async def fake_completion(**kwargs):
        called.append(kwargs)

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    gate.configure(1)
    await gate.acquire("debate-1")
    result = await ms.probe(make_target(), gate_wait_seconds=0.05)
    assert result.state == conn.BUSY and not called
    snap = gate.snapshot()
    assert snap["active_holders"] == ["debate-1"] and snap["waiting"] == 0, "줄에 흔적을 남기지 않습니다"
    gate.release("debate-1")
    assert (await ms.probe(make_target(), gate_wait_seconds=0.05)).state == conn.OK


@pytest.mark.asyncio
async def test_a_connection_without_info_is_not_called(gate, monkeypatch):
    async def fake_completion(**kwargs):
        raise AssertionError("부르면 안 됩니다")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    result = await ms.probe(make_target(is_live=False, api_base=None, api_key=None))
    assert result.state == conn.NOT_CONFIGURED


# ---------------------------------------------------------------- 5. 결과판


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_board(gate, *, hold: asyncio.Event | None = None, states=None):
    calls = []
    running = {"now": 0, "peak": 0}
    clock = Clock()

    async def prober(target: ms.ModelTarget) -> ms.ModelStatus:
        calls.append(target.model)
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        try:
            if hold is not None:
                await hold.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            running["now"] -= 1
        return ms.ModelStatus(target.id, (states or {}).get(target.model, conn.OK), latency_ms=120)

    return ms.ModelStatusBoard(prober=prober, clock=clock), calls, running, clock


@pytest.mark.asyncio
async def test_only_one_round_runs_and_everyone_shares_it(gate):
    hold = asyncio.Event()
    board, calls, _, _ = make_board(gate, hold=hold)
    config = make_config()
    assert board.start(config) is True
    assert board.start(config) is False and board.start(config, force=True) is False, "도는 판에 올라탑니다"
    await asyncio.sleep(0)
    assert board.running and {row.state for row in board.rows(config)} == {conn.CHECKING}
    hold.set()
    await board.wait()
    assert not board.running and sorted(calls) == ["openai/coder-only", "openai/shared", "openai/shared"]
    assert [row.state for row in board.rows(config)] == [conn.OK, conn.OK, conn.OK]


@pytest.mark.asyncio
async def test_results_are_reused_for_a_while(gate):
    board, calls, _, clock = make_board(gate)
    config = make_config()
    board.start(config)
    await board.wait()
    first = len(calls)

    clock.now += ms.CACHE_SECONDS - 1
    assert board.start(config) is False and len(calls) == first
    row = board.rows(config)[0]
    assert row.age_seconds == pytest.approx(ms.CACHE_SECONDS - 1) and row.latency_ms == 120

    clock.now += 2
    assert board.start(config) is True
    await board.wait()
    assert len(calls) == first * 2


@pytest.mark.asyncio
async def test_recheck_is_throttled_but_not_blocked(gate):
    board, calls, _, clock = make_board(gate)
    config = make_config()
    board.start(config)
    await board.wait()
    first = len(calls)

    clock.now += ms.MIN_INTERVAL_SECONDS - 1
    assert board.start(config, force=True) is False and len(calls) == first, "연달아 눌러도 새로 보내지 않습니다"
    clock.now += 2
    assert board.start(config, force=True) is True, "캐시 시간 안이어도 다시 확인은 됩니다"
    await board.wait()
    assert len(calls) == first * 2


@pytest.mark.asyncio
async def test_a_changed_config_is_checked_right_away_and_old_rows_are_dropped(gate):
    board, calls, _, _ = make_board(gate)
    board.start(make_config())
    await board.wait()
    changed = make_config(agents={
        "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
        "coder": {"name": "Coder", "role": "Code", "model": "openai/brand-new"},
    })
    assert [row.state for row in board.rows(changed)] == [conn.OK, conn.UNKNOWN], "처음 보는 연결은 '확인 전' 입니다"
    assert board.start(changed) is True, "캐시 시간 안이어도 처음 보는 연결이 있으면 확인합니다"
    await board.wait()
    assert calls[-2:] == ["openai/shared", "openai/brand-new"] or sorted(calls[-2:]) == ["openai/brand-new", "openai/shared"]
    assert [row.target.model for row in board.rows(changed)] == ["openai/shared", "openai/brand-new"]
    assert len(board._results) == 2  # noqa: SLF001 - 사라진 연결의 결과가 남지 않았는지


@pytest.mark.asyncio
async def test_rows_show_what_each_connection_came_back_with(gate):
    board, _, _, _ = make_board(gate, states={"openai/coder-only": conn.AUTH})
    config = make_config(agents={
        "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
        "coder": {"name": "Coder", "role": "Code", "model": "openai/coder-only"},
    })
    assert [row.state for row in board.rows(config)] == [conn.UNKNOWN, conn.UNKNOWN]
    board.start(config)
    await board.wait()
    rows = board.rows(config)
    assert [row.state for row in rows] == [conn.OK, conn.AUTH]
    assert ms.summarize(rows) == "연결 2개 중 1개 응답"


@pytest.mark.asyncio
async def test_with_a_limit_probes_go_one_at_a_time(gate):
    board, calls, running, _ = make_board(gate)
    gate.configure(2)
    board.start(make_config())
    await board.wait()
    assert len(calls) == 3 and running["peak"] == 1, "상한이 있으면 우리끼리 자리를 다투지 않습니다"


@pytest.mark.asyncio
async def test_without_a_limit_probes_go_together(gate):
    board, calls, running, _ = make_board(gate)
    board.start(make_config())
    await board.wait()
    assert len(calls) == 3 and running["peak"] == 3


@pytest.mark.asyncio
async def test_one_crashing_probe_does_not_stop_the_rest(gate):
    async def prober(target):
        if target.model == "openai/coder-only":
            raise RuntimeError(f"exploded with {SHARED_KEY}")
        return ms.ModelStatus(target.id, conn.OK)

    board = ms.ModelStatusBoard(prober=prober)
    config = make_config()
    board.start(config)
    await board.wait()
    rows = board.rows(config)
    assert [row.state for row in rows] == [conn.OK, conn.ERROR, conn.OK]
    assert SHARED_KEY not in rows[1].detail
    assert not board.running and board.start(config) is False


def test_a_connection_without_info_reads_as_such_before_any_check():
    config = make_config(agents={"orchestrator": {"name": "O", "role": "M"}}, llm={})
    (row,) = ms.ModelStatusBoard().rows(config)
    assert row.state == conn.NOT_CONFIGURED


# ---------------------------------------------------------------- 6. 화면으로 가는 자료


def _row(state=conn.AUTH, detail="litellm.AuthenticationError: bad key [키 가림]") -> ms.ModelRow:
    target = make_target(users=("전역 기본값", "Coder"), api_base="http://gateway.internal:8443/v1")
    return ms.ModelRow(target, state, latency_ms=None, detail=detail, age_seconds=12.0)


def test_visitors_get_no_address_and_no_raw_error():
    view = ms.describe_row(_row(), detail=False)
    assert view["label"] == "인증 실패" and view["tone"] == "bad" and view["hint"]
    assert view["model"] == "openai/test" and view["users"] == ["전역 기본값", "Coder"]
    assert not {"endpoint", "has_key", "error"} & set(view)
    blob = json.dumps(view, ensure_ascii=False)
    assert "gateway.internal" not in blob and "AuthenticationError" not in blob and SHARED_KEY not in blob


def test_the_owner_gets_the_address_and_the_error_but_never_the_key():
    view = ms.describe_row(_row(), detail=True)
    assert view["endpoint"] == "http://gateway.internal:8443/v1" and view["has_key"] is True
    assert "AuthenticationError" in view["error"]
    assert SHARED_KEY not in json.dumps(view, ensure_ascii=False)


def test_every_state_has_a_label_a_tone_and_something_to_do():
    states = [getattr(conn, name) for name in (
        "OK", "NOT_CONFIGURED", "BUSY", "AUTH", "MODEL", "RATE_LIMITED", "UNREACHABLE", "TIMEOUT",
        "REJECTED", "SERVER_ERROR", "ERROR", "CHECKING", "UNKNOWN",
    )]
    assert set(states) == set(conn.STATE_INFO)
    for state in states:
        label, tone, hint = conn.STATE_INFO[state]
        assert label and hint and tone in ("good", "warn", "bad", "idle")
    assert conn.STATE_INFO[conn.OK][1] == "good"
    # 연결은 되는데 지금 못 쓰는 것은 '실패' 가 아니라 '주의' 입니다.
    assert {conn.STATE_INFO[s][1] for s in (conn.RATE_LIMITED, conn.BUSY)} == {"warn"}


def test_timing_text_is_readable():
    fast = ms.describe_row(ms.ModelRow(make_target(), conn.OK, latency_ms=484, age_seconds=2.0), detail=False)
    slow = ms.describe_row(ms.ModelRow(make_target(), conn.OK, latency_ms=2340, age_seconds=75.0), detail=False)
    assert (fast["latency"], fast["age"]) == ("484ms", "방금 확인")
    assert (slow["latency"], slow["age"]) == ("2.3초", "1분 전 확인")
    checking = [ms.ModelRow(make_target(), conn.CHECKING), ms.ModelRow(make_target(id="t2"), conn.OK)]
    assert ms.summarize(checking) == "확인 중 — 2개 중 1개 끝남"


# ---------------------------------------------------------------- 7. 세 화면의 머리


def test_the_button_is_in_all_three_headers_with_the_right_level_of_detail():
    from app.easy.pages import common as easy_common
    from app.trial.pages import common as trial_common
    from app.ui.components import model_status as component

    assert component.BUTTON_LABEL == "모델 연결 상태"
    # 전문가 화면은 주인만 엽니다. 체험·쉬운 화면은 방문자도 여므로 주인일 때만 자세히 보여 줍니다.
    expert = (ROOT / "app" / "ui" / "expert" / "chrome.py").read_text(encoding="utf-8")
    assert "build_model_status_button(detail=True)" in expert
    assert "build_model_status_button(detail=owner)" in inspect.getsource(trial_common.header)
    assert "build_model_status_button(detail=viewer.owner)" in inspect.getsource(easy_common.easy_header)


def test_the_dialog_only_renders_what_describe_row_hands_it():
    """창은 `describe_row` 가 준 것만 그립니다. 연결 정보를 직접 꺼내 쓰면 방문자에게 샐 수 있습니다."""
    from app.ui.components import model_status as component

    source = inspect.getsource(component)
    assert "describe_row(row, detail=detail)" in source
    for leak in (".api_key", ".api_base", ".endpoint", ".detail"):
        assert leak not in source, f"화면 코드가 {leak} 를 직접 읽습니다"
