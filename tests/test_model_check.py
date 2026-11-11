"""모델 적합성 점검 (app/agents/model_check.py, app/modelcheck.py, ADR-054).

진짜 LiteLLM 이 로컬 가짜 서버를 부르게 해서 봅니다. 가짜 서버는 모델 이름으로 "어떤 서버인지" 를 정합니다.

1. 잘 맞는 서버는 전부 통과하고, 요청은 정해진 수만 나가며 재시도하지 않는다.
2. 서버마다 다른 어긋남(도구 호출 누출, 추론에 갇힌 답, 잘림을 알리지 않음 …)이 그 검사 하나에만 드러난다.
3. 컨텍스트 창은 서버가 말한 값과 에이전트 설정을 견준다.
4. 결정 LLM 은 한 번의 요청으로 세 가지 답의 모양을 본다.
5. 결과는 파일로 남고 다시 읽히며, 방문자용 자료에는 주소와 오류 원문이 없다.
6. 결과판은 한 번에 한 연결만 점검한다.
7. 명령줄과 화면.
"""

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.agents import model_check as mc
from app.agents import model_status as ms
from app.agents.llm_gate import LLMGate
from app.agents.tool_loading import LOAD_TOOLS_TOOL
from app.config import DecisionLLMConfig, RootConfig

SHARED_KEY = "sk-shared-secret-0001"
BIG_WINDOW = 131072


# ---------------------------------------------------------------- 가짜 서버


def _reply(content="", *, finish="stop", tool_calls=None, reasoning=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
        finish = "tool_calls"
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return message, finish


def _call(name, arguments):
    return [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}]


class _FakeServer(BaseHTTPRequestHandler):
    """모델 이름으로 성격을 정하는 OpenAI 호환 서버."""

    seen: list = []

    def log_message(self, *args):
        pass

    # --- 요청의 종류
    @staticmethod
    def _kind(body) -> str:
        messages = body.get("messages") or []
        if body.get("stream"):
            return "stream"
        if body.get("tools"):
            if messages and messages[-1].get("role") == "tool":
                return "tool_result"
            if body["tools"][0]["function"]["name"] == LOAD_TOOLS_TOOL:
                return "catalog"
            return "tools"
        if body.get("max_tokens") == mc.LENGTH_PROBE_TOKENS:
            return "length"
        if messages and isinstance(messages[-1].get("content"), list):
            return "image"
        if len([m for m in messages if m.get("role") == "user"]) >= 2:
            return "roles"
        return "answer"

    def _decide(self, model, kind, body):
        """`(상태 코드, (message, finish) 또는 오류 문장)`."""
        refuse = {
            "down": {"answer"}, "strict-roles": {"roles"}, "no-tools": {"tools", "tool_result", "catalog"},
            "no-history": {"tool_result"}, "blind": {"image"}, "no-stream": {"stream"},
        }
        if kind in refuse.get(model, ()):
            return 400 if model != "down" else 500, f"refused {kind} (key {SHARED_KEY})"
        if model == "no-none" and kind == "tool_result" and body.get("tool_choice") == "none":
            return 400, "tool_choice none is not supported"

        if kind == "answer":
            if model == "thinker":
                return 200, _reply("", finish="length", reasoning="17 더하기 25 를 따져 보면 " * 20)
            if model == "trapped":
                return 200, _reply("", reasoning="생각 끝에 답은 42 입니다.")
            if model == "inline-think":
                # 여는 표식이 챗 템플릿에 미리 들어가 닫는 표식만 오는 모양. 짝이 맞는 <think>…</think> 는
                # LiteLLM 이 스스로 reasoning_content 로 갈라 주기도 합니다 (1.98 에서 확인).
                return 200, _reply("17 과 25 를 더하면</think>42")
            if model == "splitter":
                return 200, _reply("42", reasoning="17 + 25 = 42")
            return 200, _reply("42")
        if kind == "length":
            return 200, _reply("1\n2\n3", finish="stop" if model == "quiet-cut" else "length")
        if kind == "roles":
            return 200, _reply("바다")
        if kind == "tools":
            if model == "leaky":
                return 200, _reply('<tool_call>{"name": "probe__lookup_code", "arguments": {"code": "A7"}}</tool_call>')
            if model == "talker":
                return 200, _reply("A7 은 아마 좌표일 것입니다.")
            if model == "thinks-then-calls":
                return 200, _reply(tool_calls=_call(mc.PROBE_TOOL_NAME, json.dumps({"code": mc.PROBE_CODE})),
                                   reasoning="코드를 찾아야 한다")
            if model == "broken-args":
                return 200, _reply(tool_calls=_call(mc.PROBE_TOOL_NAME, '{"code": "A7'))
            return 200, _reply(tool_calls=_call(mc.PROBE_TOOL_NAME, json.dumps({"code": mc.PROBE_CODE})))
        if kind == "tool_result":
            if model == "ignores-none":
                return 200, _reply(tool_calls=_call(mc.PROBE_TOOL_NAME, json.dumps({"code": mc.PROBE_CODE})))
            return 200, _reply(f"{mc.PROBE_CODE} 의 뜻은 {mc.PROBE_MEANING} 입니다.")
        if kind == "catalog":
            if model == "no-catalog":
                return 200, _reply("그런 도구는 없습니다.")
            if model == "direct-caller":
                return 200, _reply(tool_calls=_call(mc.PROBE_TOOL_NAME, json.dumps({"code": mc.PROBE_CODE})))
            return 200, _reply(tool_calls=_call(LOAD_TOOLS_TOOL, json.dumps({"names": [mc.PROBE_TOOL_NAME]})))
        if kind == "image":
            return 200, _reply("I cannot see any image." if model == "blind-quiet" else "Red")
        raise AssertionError(kind)

    def _send(self, status, payload, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append({"path": self.path})
        entries = [
            {"id": "small-window", "object": "model", "max_model_len": 32768},
            {"id": "no-window", "object": "model"},
        ]
        entries += [{"id": name, "object": "model", "max_model_len": BIG_WINDOW} for name in ("good", "leaky")]
        self._send(200, {"object": "list", "data": entries})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        model, kind = body["model"], self._kind(body)
        type(self).seen.append({"kind": kind, "body": body})
        if kind == "stream" and model != "no-stream":
            lines = []
            for piece in ("하나 ", "둘 ", "셋"):
                lines.append({"id": "x", "object": "chat.completion.chunk", "created": 1, "model": model,
                              "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            last = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            lines.append(last)
            data = "".join(f"data: {json.dumps(line)}\n\n" for line in lines) + "data: [DONE]\n\n"
            return self._send(200, data.encode(), "text/event-stream")
        status, outcome = self._decide(model, kind, body)
        if status != 200:
            return self._send(status, {"error": {"message": outcome, "type": "x", "code": status}})
        message, finish = outcome
        return self._send(200, {
            "id": "x", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        })


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), _FakeServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def gate(monkeypatch) -> LLMGate:
    gate = LLMGate(0)
    monkeypatch.setattr(mc, "get_llm_gate", lambda: gate)
    return gate


@pytest.fixture(autouse=True)
def _scratch_records(tmp_path, monkeypatch):
    """점검 기록이 실제 `data/modelcheck` 에 쌓이지 않게 합니다."""
    monkeypatch.setattr(mc, "modelcheck_dir", lambda: tmp_path / "modelcheck")
    _FakeServer.seen.clear()


def make_config(server, model="good", agents=None) -> RootConfig:
    return RootConfig.model_validate({
        "llm": {"model": f"openai/{model}", "api_base": server, "api_key": SHARED_KEY},
        "agents": agents or {
            "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
            "coder": {"name": "Coder", "role": "Code"},
        },
    })


async def run(server, model="good", agents=None, **kwargs) -> mc.ModelProfile:
    config = make_config(server, model, agents)
    (target,) = ms.collect_targets(config)
    return await mc.check_target(target, config, timeout_seconds=10, **kwargs)


def by_key(profile: mc.ModelProfile) -> dict:
    return {result.key: result for result in profile.results}


# ---------------------------------------------------------------- 1. 잘 맞는 서버


@pytest.mark.asyncio
async def test_a_well_behaved_server_passes_everything(server, gate):
    profile = await run(server)
    results = by_key(profile)
    assert list(results) == [
        "answer", "reasoning", "stream", "length", "roles", "tools", "tool_result", "catalog", "image", "window",
    ]
    assert {key: r.outcome for key, r in results.items() if r.outcome != mc.OK} == {}
    assert profile.worst == mc.OK and not profile.suggestions()
    assert "사용량 보고됨" in results["answer"].summary and "3조각" in results["stream"].summary
    assert profile.kind == mc.SYSTEM2 and profile.users == ("전역 기본값", "Orchestrator", "Coder")
    assert gate.snapshot()["active"] == 0, "쥔 자리는 모두 돌려줍니다"


@pytest.mark.asyncio
async def test_requests_are_few_small_and_never_retried(server, gate):
    await run(server)
    posts = [entry for entry in _FakeServer.seen if "kind" in entry]
    assert [entry["kind"] for entry in posts] == [
        "answer", "stream", "length", "roles", "tools", "tool_result", "catalog", "image",
    ]
    assert all(entry["body"]["max_tokens"] <= mc.ANSWER_TOKENS for entry in posts)
    assert not any("temperature" in entry["body"] for entry in posts), "모델마다 받는 값이 다른 것은 보내지 않습니다"
    assert [entry["path"] for entry in _FakeServer.seen if "path" in entry] == ["/v1/models"]


@pytest.mark.asyncio
async def test_the_wrap_up_request_has_the_shape_the_real_loop_sends(server, gate):
    await run(server)
    body = next(entry["body"] for entry in _FakeServer.seen if entry.get("kind") == "tool_result")
    assert body["tool_choice"] == "none" and body["tools"], "도구는 싣되 부르지 못하게 — `_wrap_up_without_tools` 와 같습니다"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool"]
    assert body["messages"][1]["tool_calls"][0]["id"] == body["messages"][2]["tool_call_id"] == "call_1", (
        "지어낸 턴이 아니라 방금 모델이 낸 호출을 되돌려 넣습니다"
    )


@pytest.mark.asyncio
async def test_the_assistant_turn_carries_back_what_the_model_sent_with_its_call(server, gate):
    """도구를 부른 발언의 `reasoning_content` 를 돌려받아야 하는 모델이 있습니다 (DeepSeek thinking mode).

    손으로 만든 assistant 턴은 그런 모델에서 400 으로 거절되어, 멀쩡한 모델이 '실패' 로 나왔습니다 (2026-11-11 실제 시험).
    """
    await run(server, "thinks-then-calls")
    body = next(entry["body"] for entry in _FakeServer.seen if entry.get("kind") == "tool_result")
    assert body["messages"][1].get("reasoning_content") == "코드를 찾아야 한다"


@pytest.mark.asyncio
async def test_without_a_call_from_the_model_the_wrap_up_is_not_asked(server, gate):
    results = by_key(await run(server, "talker"))
    assert results["tool_result"].outcome == mc.SKIP and results["catalog"].outcome == mc.OK
    assert "tool_result" not in [entry.get("kind") for entry in _FakeServer.seen]


@pytest.mark.asyncio
async def test_the_catalog_request_uses_the_real_catalog_tool_and_guidance(server, gate):
    await run(server)
    body = next(entry["body"] for entry in _FakeServer.seen if entry.get("kind") == "catalog")
    (tool,) = body["tools"]
    assert tool["function"]["name"] == LOAD_TOOLS_TOOL and mc.PROBE_TOOL_NAME in tool["function"]["description"]
    assert body["messages"][0]["role"] == "system" and LOAD_TOOLS_TOOL in body["messages"][0]["content"]


# ---------------------------------------------------------------- 2. 어긋나는 서버


@pytest.mark.asyncio
@pytest.mark.parametrize("model, key, outcome, word", [
    ("leaky", "tools", mc.FAIL, "본문 글자로"),
    ("talker", "tools", mc.WARN, "부르지 않고"),
    ("broken-args", "tools", mc.WARN, "JSON 이 아닙니다"),
    ("thinker", "reasoning", mc.WARN, "한도"),
    ("trapped", "reasoning", mc.WARN, "추론(reasoning_content"),
    ("inline-think", "reasoning", mc.WARN, "<think>"),
    ("quiet-cut", "length", mc.WARN, "finish_reason='stop'"),
    ("strict-roles", "roles", mc.INFO, "합쳐서 보내므로"),
    ("no-none", "tool_result", mc.WARN, "tool_choice='none' 을 받지 않습니다"),
    ("no-history", "tool_result", mc.FAIL, "도구 결과가 담긴 대화를 받지 않습니다"),
    ("ignores-none", "tool_result", mc.WARN, "다시 불렀습니다"),
    ("no-catalog", "catalog", mc.WARN, "불러오지 않았습니다"),
    ("blind", "image", mc.INFO, "받지 않습니다"),
    ("blind-quiet", "image", mc.INFO, "읽지 못했습니다"),
    ("no-stream", "stream", mc.WARN, "스트리밍 요청이 실패"),
])
async def test_each_quirk_shows_up_in_its_own_check(server, gate, model, key, outcome, word):
    profile = await run(server, model)
    results = by_key(profile)
    assert results[key].outcome == outcome, results[key].summary
    assert word in results[key].summary
    # 그 검사 말고는 잘 맞는 서버와 같습니다 (창 크기는 /models 에 없는 모델이라 건너뜁니다).
    others = {k: r.outcome for k, r in results.items() if k not in (key, "window", "stream", "answer")}
    # 모델이 낸 도구 호출이 없으면 그 뒤의 도구 검사는 묻지 않고 건너뜁니다.
    allowed = {mc.OK, mc.SKIP} if key == "tools" else {mc.OK}
    assert set(others.values()) <= allowed, others


@pytest.mark.asyncio
async def test_reasoning_that_arrives_apart_from_the_answer_is_fine(server, gate):
    result = by_key(await run(server, "splitter"))["reasoning"]
    assert result.outcome == mc.OK and "갈라져" in result.summary


@pytest.mark.asyncio
async def test_calling_a_listed_tool_directly_also_counts(server, gate):
    result = by_key(await run(server, "direct-caller"))["catalog"]
    assert result.outcome == mc.OK and "곧바로" in result.summary


@pytest.mark.asyncio
async def test_suggestions_name_the_setting_to_change(server, gate):
    thinker = by_key(await run(server, "thinker"))["reasoning"].suggestions[0]
    assert (thinker.setting, thinker.value) == (mc.SET_MAX_TOKENS, mc.RECOMMENDED_REASONING_MAX_TOKENS)
    catalog = by_key(await run(server, "no-catalog"))["catalog"].suggestions[0]
    assert (catalog.setting, catalog.value) == (mc.SET_TOOL_LOADING, False)
    leaky = by_key(await run(server, "leaky"))["tools"].suggestions[0]
    assert leaky.setting == "" and "tool-call-parser" in leaky.text, "서버 쪽 설정은 conf 로 고칠 수 없습니다"


@pytest.mark.asyncio
async def test_without_tool_calls_the_tool_follow_ups_are_skipped(server, gate):
    results = by_key(await run(server, "no-tools"))
    assert results["tools"].outcome == mc.FAIL
    assert results["tool_result"].outcome == results["catalog"].outcome == mc.SKIP
    assert "tool_result" not in [entry.get("kind") for entry in _FakeServer.seen]


@pytest.mark.asyncio
async def test_an_agent_that_takes_images_turns_a_blind_model_into_a_warning(server, gate):
    agents = {
        "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
        "coder": {"name": "Coder", "role": "Code", "allowed_modalities": ["image"]},
    }
    result = by_key(await run(server, "blind", agents))["image"]
    assert result.outcome == mc.WARN
    assert (result.suggestions[0].setting, result.suggestions[0].value) == (mc.SET_DROP_IMAGE, False)
    assert "Coder" in result.suggestions[0].text


@pytest.mark.asyncio
async def test_a_server_that_does_not_answer_stops_after_one_request(server, gate):
    profile = await run(server, "down")
    (result,) = profile.results
    assert result.key == "answer" and result.outcome == mc.FAIL and "서버 오류" in result.summary
    assert SHARED_KEY not in result.detail, "Provider 가 되돌려 보낸 키도 가립니다"
    assert len([entry for entry in _FakeServer.seen if "kind" in entry]) == 1


@pytest.mark.asyncio
async def test_a_connection_without_info_is_not_called(gate, monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("부르면 안 됩니다")

    monkeypatch.setattr(mc.llm_module.litellm, "acompletion", boom)
    config = RootConfig.model_validate({"llm": {}, "agents": {"orchestrator": {"name": "O", "role": "M"}}})
    (target,) = ms.collect_targets(config)
    profile = await mc.check_target(target, config)
    assert [r.outcome for r in profile.results] == [mc.SKIP]


@pytest.mark.asyncio
async def test_a_crashing_check_does_not_stop_the_rest(server, gate, monkeypatch):
    async def explode(asker):
        raise RuntimeError("bug in the check")

    monkeypatch.setattr(mc, "_check_roles", explode)
    results = (await run(server)).results
    assert [r.key for r in results].count("error") == 1 and results[-1].key == "window"


# ---------------------------------------------------------------- 3. 컨텍스트 창


@pytest.mark.asyncio
async def test_a_window_smaller_than_the_setting_fails_and_says_who(server, gate):
    agents = {
        "orchestrator": {"name": "Orchestrator", "role": "Moderator", "max_context_window": 32000},
        "coder": {"name": "Coder", "role": "Code", "max_context_window": 128000},
    }
    result = by_key(await run(server, "small-window", agents))["window"]
    assert result.outcome == mc.FAIL and "32,768" in result.summary
    assert "Coder(128,000)" in result.summary and "Orchestrator" not in result.summary
    assert (result.suggestions[0].setting, result.suggestions[0].value) == (mc.SET_MAX_CONTEXT_WINDOW, 32768)


@pytest.mark.asyncio
async def test_a_server_that_does_not_say_its_window_is_skipped_with_advice(server, gate):
    result = by_key(await run(server, "no-window"))["window"]
    assert result.outcome == mc.SKIP and "max_context_window" in result.summary


@pytest.mark.asyncio
async def test_settings_inside_the_window_pass(server, gate):
    result = by_key(await run(server, "good"))["window"]
    assert result.outcome == mc.OK and f"{BIG_WINDOW:,}" in result.summary


def test_only_enabled_agents_on_that_connection_count_as_users(server):
    config = make_config(server, agents={
        "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
        "coder": {"name": "Coder", "role": "Code", "model": "openai/other"},
        "off": {"name": "Off", "role": "x", "enabled": False},
    })
    shared, other = ms.collect_targets(config)
    assert [use.key for use in mc.uses_of(shared, config)] == ["orchestrator"]
    assert [use.key for use in mc.uses_of(other, config)] == ["coder"]
    assert mc.targets_for_agents(["coder"], config) == [other]
    with pytest.raises(KeyError):
        mc.targets_for_agents(["nobody"], config)


# ---------------------------------------------------------------- 4. 결정 LLM


def _s1_target(**overrides) -> ms.ModelTarget:
    cfg = DecisionLLMConfig(enabled=True, backend="jev", model="jev-test", api_base="https://s1.local", api_key="jev-key-000111")
    values = dict(
        id="s1", model="jev/jev-test", api_base=cfg.api_base, api_key=cfg.api_key, api_version=None, provider="jev",
        extra_headers=(), extra_body="{}", users=("Critic",), is_live=True, decision_cfg=cfg,
    )
    values.update(overrides)
    return ms.ModelTarget(**values)


def _s1_transport(answers, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope jev-key-000111"}})
        return httpx.Response(200, json={"model": "jev-test", "answers": answers})

    return httpx.MockTransport(handler)


GOOD_S1 = {
    "noul": {"type": "noul", "noul": 0.91},
    "choice": {"type": "choice", "choice": "go", "probabilities": {"go": 0.8, "stop": 0.2}},
    "score": {"type": "score", "score": 2.4},
}


@pytest.mark.asyncio
async def test_a_decision_model_is_checked_with_one_request(gate):
    seen = []
    profile = await mc.check_target(_s1_target(), http_transport=_s1_transport(GOOD_S1, seen=seen))
    assert profile.kind == mc.SYSTEM1 and [r.outcome for r in profile.results] == [mc.OK] * 4
    assert len(seen) == 1 and set(seen[0]["questions"]) == {"noul", "choice", "score"}
    assert isinstance(seen[0]["questions"]["choice"]["criteria"], dict), "Jev 는 choice 의 criteria 를 사전으로만 받습니다"


@pytest.mark.asyncio
async def test_missing_or_odd_decision_answers_fail_on_their_own_line(gate):
    answers = {"noul": {"type": "noul"}, "choice": {"type": "choice", "choice": "maybe"}}
    results = by_key(await mc.check_target(_s1_target(), http_transport=_s1_transport(answers)))
    assert results["systemone"].outcome == mc.OK
    assert results["s1_choice"].outcome == results["s1_score"].outcome == mc.FAIL
    # 답에 확률이 없으면 0 으로 읽히므로 형식은 통과합니다 — 값이 없다는 것은 'noul' 이 아예 빠졌을 때 드러납니다.
    partial = by_key(await mc.check_target(_s1_target(), http_transport=_s1_transport({"score": {"score": 1}})))
    assert partial["s1_noul"].outcome == mc.FAIL and partial["s1_score"].outcome == mc.OK


@pytest.mark.asyncio
async def test_a_choice_without_probabilities_is_a_warning(gate):
    answers = {**GOOD_S1, "choice": {"type": "choice", "choice": "go"}}
    assert by_key(await mc.check_target(_s1_target(), http_transport=_s1_transport(answers)))["s1_choice"].outcome == mc.WARN


@pytest.mark.asyncio
async def test_a_decision_model_that_refuses_is_one_failure_and_three_skips(gate):
    profile = await mc.check_target(_s1_target(), http_transport=_s1_transport({}, status=401))
    assert [r.outcome for r in profile.results] == [mc.FAIL, mc.SKIP, mc.SKIP, mc.SKIP]
    assert "인증 실패" in profile.results[0].summary and "jev-key-000111" not in profile.results[0].detail


# ---------------------------------------------------------------- 5. 기록과 내보내기


@pytest.mark.asyncio
async def test_profiles_are_saved_and_read_back(server, gate):
    profile = await run(server, "leaky")
    path = mc.save_profile(profile)
    assert path is not None and path.name == f"{profile.target_id}.json"
    assert SHARED_KEY not in path.read_text(encoding="utf-8")
    assert mc.load_profile(profile.target_id) == profile
    assert mc.load_profile("no-such-connection") is None


@pytest.mark.asyncio
async def test_visitors_get_no_address_and_no_raw_error(server, gate):
    profile = await run(server, "down")
    owner, visitor = profile.to_dict(detail=True), profile.to_dict(detail=False)
    assert owner["endpoint"] == server and owner["results"][0]["detail"]
    assert "endpoint" not in visitor and "detail" not in visitor["results"][0]
    assert server not in json.dumps(visitor, ensure_ascii=False)


@pytest.mark.asyncio
async def test_the_console_text_lists_every_check_and_its_advice(server, gate):
    profile = await run(server, "no-catalog")
    text = mc.format_profile(profile)
    assert "openai/no-catalog" in text and "[주의] 도구 목록에서 불러오기" in text
    assert "→" in text and "tool_loading.enabled" in text
    assert mc.summarize(profile) in text


# ---------------------------------------------------------------- 6. 결과판


@pytest.mark.asyncio
async def test_the_board_checks_one_connection_at_a_time(server, gate):
    hold = asyncio.Event()
    calls = []

    async def checker(target, config=None):
        calls.append(target.model)
        await hold.wait()
        return mc.ModelProfile(target.id, target.model, mc.SYSTEM2, target.users,
                               (mc.CheckResult("answer", "기본 응답", mc.OK, "응답했습니다"),), checked_at=1.0)

    config = make_config(server, agents={
        "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
        "coder": {"name": "Coder", "role": "Code", "model": "openai/other"},
    })
    first, second = ms.collect_targets(config)
    board = mc.ModelCheckBoard(checker=checker)
    assert board.profile(first.id) is None
    assert board.start(first.id, config) is True
    assert board.start(second.id, config) is False, "도는 동안에는 다른 연결을 시작하지 않습니다"
    await asyncio.sleep(0)
    assert board.running and board.running_id == first.id
    hold.set()
    await board.wait()
    assert not board.running and board.running_id is None and calls == ["openai/good"]
    assert board.profile(first.id).worst == mc.OK
    assert mc.load_profile(first.id) is not None, "끝나면 파일로도 남깁니다"
    assert board.start("unknown-id", config) is False


@pytest.mark.asyncio
async def test_a_new_board_reads_the_last_result_from_disk(server, gate):
    profile = await run(server)
    mc.save_profile(profile)
    assert mc.ModelCheckBoard().profile(profile.target_id) == profile


@pytest.mark.asyncio
async def test_a_crashing_checker_leaves_the_board_usable(server, gate):
    async def checker(target, config=None):
        raise RuntimeError("boom")

    config = make_config(server)
    (target,) = ms.collect_targets(config)
    board = mc.ModelCheckBoard(checker=checker)
    board.start(target.id, config)
    await board.wait()
    assert not board.running and board.profile(target.id) is None
    assert board.start(target.id, config) is True
    await board.wait()


# ---------------------------------------------------------------- 7. 명령줄과 화면


def _write_conf(tmp_path, server, model, agents=None):
    path = tmp_path / "conf.json"
    path.write_text(json.dumps({
        "llm": {"model": f"openai/{model}", "api_base": server, "api_key": SHARED_KEY},
        "agents": agents or {"orchestrator": {"name": "Orchestrator", "role": "Moderator"}},
    }), encoding="utf-8")
    return str(path)


@pytest.fixture
def restore_config():
    """명령줄은 전역 설정을 다시 읽습니다. 시험이 끝나면 원래 설정으로 되돌립니다."""
    from app.config import get_config

    yield
    get_config(reload=True)


@pytest.mark.asyncio
async def test_the_command_line_prints_a_report_and_exits_by_the_worst_result(
    server, gate, tmp_path, capsys, restore_config,
):
    from app import modelcheck

    assert await modelcheck._main(["--config", _write_conf(tmp_path, server, "good")]) == 0  # noqa: SLF001
    out = capsys.readouterr().out
    assert "openai/good" in out and "[통과] 도구 호출" in out and SHARED_KEY not in out
    assert list((tmp_path / "modelcheck").glob("*.json")), "화면이 읽는 자리에 기록을 남깁니다"

    assert await modelcheck._main(["--config", _write_conf(tmp_path, server, "leaky"), "--json"]) == 1  # noqa: SLF001
    (profile,) = json.loads(capsys.readouterr().out)
    assert profile["worst"] == mc.FAIL and profile["model"] == "openai/leaky"


@pytest.mark.asyncio
async def test_the_command_line_can_pick_agents_and_rejects_unknown_ones(server, gate, tmp_path, capsys, restore_config):
    from app import modelcheck

    conf = _write_conf(tmp_path, server, "good", agents={
        "orchestrator": {"name": "Orchestrator", "role": "Moderator"},
        "coder": {"name": "Coder", "role": "Code", "model": "openai/talker"},
    })
    assert await modelcheck._main(["--config", conf, "--agent", "coder", "--json"]) == 0  # noqa: SLF001
    assert [p["model"] for p in json.loads(capsys.readouterr().out)] == ["openai/talker"]
    assert await modelcheck._main(["--config", conf, "--agent", "nobody"]) == 2  # noqa: SLF001
    assert "nobody" in capsys.readouterr().err


def _texts(element) -> list:
    found = []
    for child in element.descendants():
        text = getattr(child, "text", None)
        if isinstance(text, str) and text:
            found.append(text)
    return found


@pytest.mark.asyncio
async def test_the_owner_row_shows_the_check_button_and_the_results(server, gate, ui_page):
    from nicegui import ui

    from app.ui.components import model_status as component

    profile = await run(server, "no-catalog")
    view = {"id": profile.target_id, "state": "ok", "label": "연결됨", "tone": "good", "hint": "", "model": profile.model,
            "users": ["Coder"], "latency": "", "age": ""}
    pressed = []
    with ui_page:
        with ui.column() as owner:
            component._row(view, profile=profile.to_dict(detail=True), can_check=True, on_check=pressed.append)  # noqa: SLF001
        with ui.column() as visitor:
            component._row(view)  # noqa: SLF001
        with ui.column() as busy:
            component._row(view, checking=True, on_check=pressed.append)  # noqa: SLF001

    owner_text = "\n".join(_texts(owner))
    assert "다시 점검" in owner_text and "적합성 점검: " in owner_text
    assert "도구 목록에서 불러오기" in owner_text and "tool_loading.enabled" in owner_text
    assert "점검" not in "\n".join(_texts(visitor)), "방문자의 줄에는 단추도 결과도 없습니다"
    assert "적합성 점검 중" in "\n".join(_texts(busy)) and "다시 점검" not in "\n".join(_texts(busy))

    button = next(child for child in owner.descendants() if getattr(child, "text", None) == "다시 점검")
    for listener in button._event_listeners.values():  # noqa: SLF001
        if listener.type == "click":
            listener.handler(None)
    assert pressed == [profile.target_id]


# ---------------------------------------------------------------- 8. 권고를 conf.json 에 적기 (ADR-056)


def _conf_file(tmp_path, server, agents, tool_loading=None):
    path = tmp_path / "conf.json"
    data = {
        "// llm": "전역 연결",
        "llm": {"model": "openai/small-window", "api_base": server, "api_key": "${TEST_LLM_KEY:-sk-from-env}"},
        "agents": agents,
    }
    if tool_loading is not None:
        data["tool_loading"] = tool_loading
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _reload(path) -> RootConfig:
    from app.config import get_config

    return get_config(reload=True, config_path=str(path))


def test_applying_a_window_suggestion_lowers_only_the_agents_that_are_over(server, tmp_path, restore_config):
    path = _conf_file(tmp_path, server, {
        "orchestrator": {"name": "Orchestrator", "role": "M", "max_context_window": 32000},
        "coder": {"name": "Coder", "role": "C"},
        "other": {"name": "Other", "role": "x", "model": "openai/elsewhere", "max_context_window": 200000},
    })
    config = _reload(path)
    target = ms.collect_targets(config)[0]
    changed = mc.apply_suggestion(target.id, mc.SET_MAX_CONTEXT_WINDOW, 32768, config, config_path=path)
    assert changed == ["Coder: max_context_window 128,000 → 32,768"]

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["agents"]["coder"]["max_context_window"] == 32768
    assert raw["agents"]["orchestrator"]["max_context_window"] == 32000, "이미 그 아래인 것은 건드리지 않습니다"
    assert raw["agents"]["other"]["max_context_window"] == 200000, "다른 연결의 에이전트는 건드리지 않습니다"
    assert raw["llm"]["api_key"] == "${TEST_LLM_KEY:-sk-from-env}" and raw["// llm"] == "전역 연결", "참조와 주석은 그대로"
    assert _reload(path).agents["coder"].max_context_window == 32768
    assert mc.apply_suggestion(target.id, mc.SET_MAX_CONTEXT_WINDOW, 32768, _reload(path), config_path=path) == []


def test_applying_the_other_suggestions(server, tmp_path, restore_config):
    path = _conf_file(tmp_path, server, {
        "orchestrator": {"name": "Orchestrator", "role": "M", "max_tokens": 16000},
        "coder": {"name": "Coder", "role": "C", "max_tokens": 2000, "allowed_modalities": ["image", "document"]},
    })
    config = _reload(path)
    target = ms.collect_targets(config)[0]

    assert mc.apply_suggestion(target.id, mc.SET_MAX_TOKENS, 8192, config, config_path=path) == [
        "Coder: max_tokens 2,000 → 8,192",
    ], "이미 넉넉한 에이전트는 낮추지 않습니다"
    config = _reload(path)
    target = ms.collect_targets(config)[0]
    assert mc.apply_suggestion(target.id, mc.SET_DROP_IMAGE, False, config, config_path=path) == ["Coder: 모달에서 이미지를 뺌"]
    config = _reload(path)
    assert config.agents["coder"].allowed_modalities == ["document"] and config.agents["coder"].max_tokens == 8192

    assert config.tool_loading.enabled is True
    assert mc.apply_suggestion("any", mc.SET_TOOL_LOADING, False, config, config_path=path) == [
        "tool_loading.enabled → false (전역: 모든 에이전트)",
    ]
    assert _reload(path).tool_loading.enabled is False
    assert mc.apply_suggestion("any", mc.SET_TOOL_LOADING, False, _reload(path), config_path=path) == []


def test_applying_refuses_what_it_does_not_know(server, tmp_path, restore_config):
    path = _conf_file(tmp_path, server, {"orchestrator": {"name": "Orchestrator", "role": "M"}})
    config = _reload(path)
    with pytest.raises(ValueError, match="적용할 수 없는 설정"):
        mc.apply_suggestion("x", "api_key", "sk-evil", config, config_path=path)
    with pytest.raises(ValueError, match="그 연결을 찾지 못했습니다"):
        mc.apply_suggestion("no-such-connection", mc.SET_MAX_TOKENS, 8192, config, config_path=path)
    assert "sk-evil" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_apply_button_is_only_on_suggestions_that_name_a_setting_and_only_when_allowed(server, gate, ui_page):
    from nicegui import ui

    from app.ui.components import model_status as component

    catalog = (await run(server, "no-catalog")).to_dict(detail=True)     # 설정 이름이 붙은 권고
    leaky = (await run(server, "leaky")).to_dict(detail=True)            # 서버 쪽에서 고칠 것 (글만)
    pressed = []
    with ui_page:
        with ui.column() as allowed:
            component._check_results(catalog, lambda target_id, suggestion: pressed.append((target_id, suggestion)))  # noqa: SLF001
        with ui.column() as server_side:
            component._check_results(leaky, lambda target_id, suggestion: pressed.append((target_id, suggestion)))  # noqa: SLF001
        with ui.column() as remote:
            component._check_results(catalog)  # noqa: SLF001

    def buttons(element):
        return [child for child in element.descendants() if getattr(child, "text", None) == component.APPLY_LABEL]

    assert len(buttons(allowed)) == 1 and buttons(server_side) == [] and buttons(remote) == []
    for listener in buttons(allowed)[0]._event_listeners.values():  # noqa: SLF001
        if listener.type == "click":
            listener.handler(None)
    assert pressed == [(catalog["target_id"], {"text": pressed[0][1]["text"], "setting": mc.SET_TOOL_LOADING, "value": False})]
