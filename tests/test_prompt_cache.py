"""Claude 프롬프트 캐시 제어 마커 주입 및 캐시 효율 검증 (app/agents/llm_prompt_cache.py, ADR-045).

1. **마커 주입 대상 엔드포인트 판별** — Anthropic·Bedrock·Vertex·Azure 경로의 Claude 모델만 대상. 타 공급자 요청은 페이로드 불변 유지.
2. **후보 위치 및 한도 검증** — 시스템 프롬프트, 도구 정의, 목표 메시지, 마지막 자체 발언, 도구 루프 끝. 단일 요청당 4개 상한 엄수.
3. **실제 전송 페이로드 변환 검증** — LiteLLM 변환을 거친 Anthropic·Bedrock 페이로드 구조, 모의 서버 스트리밍 및 토큰 사용량 계측.
4. **캐시 적중 시뮬레이션 검증** — 실제 오케스트레이션 엔진이 생성한 요청에 대해 Anthropic 캐시 규칙(접두사 일치, 20토큰 역추적 정렬)을 시뮬레이션하여 캐시 히트율 측정.
   프롬프트 조립 로직 변경 시 마커 위치가 흔들리면 해당 테스트에서 회귀가 검출됩니다.
"""

import hashlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig

from app.agents import llm_usage
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_context import fit_tool_loop_context, merge_consecutive_roles
from app.agents.llm_fallback import substitute_for
from app.agents.llm_prompt_cache import (
    MAX_CACHE_MARKS,
    cache_mark,
    mark_goal,
    route_takes_cache_marks,
    with_cache_marks,
)
from app.agents.pool import AgentPool
from app.config import AgentConfig, LLMConfig
from app.config.schema import RootConfig
from app.database.models import SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.context import EngineContext
from app.orchestration.engine import OrchestratorEngine

MODEL = "anthropic/claude-sonnet-4-5"
BEDROCK_MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
MARK = {"type": "ephemeral"}
DB_URL = "sqlite+aiosqlite:///:memory:"


def _agent(**kwargs) -> Agent:
    return Agent(**{**dict(key="coder", name="Coder", role="Impl", model=MODEL, api_key="k"), **kwargs})


def _tool(name: str = "srv__read") -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": f"{name} 도구", "parameters": {"type": "object", "properties": {}},
    }}


def _conversation() -> List[Dict[str, Any]]:
    """오케스트레이션 엔진이 전달하는 메시지 구조: 시스템 프롬프트, 목표 메시지, 타 에이전트 발언 이력, 턴 지시문."""
    return [
        {"role": "system", "content": "페르소나"},
        {"role": "user", "content": "[목표]"},
        {"role": "user", "content": "[Architect]: 발언"},
        {"role": "assistant", "content": "[Coder]: 지난 발언"},
        {"role": "user", "content": "[Critic]: 새 발언"},
        {"role": "user", "content": "[Debate Progress]: Round 2 of 3"},
    ]


def _prepared(agent: Agent) -> List[Dict[str, Any]]:
    return merge_consecutive_roles(mark_goal(agent, _conversation()))


def _marks_in(messages: List[Dict[str, Any]], tools: Any = None) -> int:
    return json.dumps(messages, ensure_ascii=False).count("cache_control") + json.dumps(tools or []).count("cache_control")


# ================================================================ 1. 캐시 마커 적용 대상 엔드포인트 판별


@pytest.mark.parametrize("model, provider, expected", [
    ("anthropic/claude-3-5-sonnet-20241022", None, True),      # 모델 테이블에서 누락된 구형 모델명 (conf.json 기본값)
    ("anthropic/claude-sonnet-4-5", None, True),
    ("claude-opus-5-5", None, True),
    ("my-alias", "anthropic", True),
    ("bedrock/us.anthropic.claude-3-7-sonnet-20250219-v1:0", None, True),
    ("bedrock/converse/us.anthropic.claude-3-7-sonnet-20250219-v1:0", None, True),
    ("bedrock/invoke/anthropic.claude-3-5-sonnet-20241022-v2:0", None, True),
    ("vertex_ai/claude-sonnet-4-5", None, True),
    ("azure_ai/claude-sonnet-4-5", None, True),
    ("bedrock/anthropic.claude-3-opus-20240229-v1:0", None, False),  # 캐싱 미지원 모델
    ("bedrock/meta.llama3-70b-instruct-v1:0", None, False),
    ("bedrock/amazon.nova-pro-v1:0", None, False),
    ("openai/claude-sonnet-4-5", None, False),                 # 사내 게이트웨이 프록시 경로
    ("claude-sonnet-4-5", "openai", False),
    ("openai/gpt-4o", None, False),
    ("gemini/gemini-1.5-pro", None, False),
    ("ollama_chat/qwen2.5-coder:14b", None, False),
    ("fake/model", None, False),
])
def test_only_claude_routes_take_marks(model, provider, expected):
    assert route_takes_cache_marks(model, provider) is expected


def test_the_setting_picks_the_lifetime_or_turns_marks_off():
    assert cache_mark(_agent()) == {"type": "ephemeral"}
    assert cache_mark(_agent(prompt_cache="1h")) == {"type": "ephemeral", "ttl": "1h"}
    assert cache_mark(_agent(prompt_cache="off")) is None


def test_other_routes_keep_the_exact_request_they_had():
    agent = _agent(model="openai/gpt-4o")
    conversation = _conversation()
    tools = [_tool()]

    assert mark_goal(agent, conversation) is conversation
    messages, marked_tools = with_cache_marks(agent, conversation, tools)
    assert messages is conversation and marked_tools is tools

    kwargs = LLMCaller.__new__(LLMCaller).build_completion_kwargs(agent, merge_consecutive_roles(conversation), tools)
    assert all(isinstance(m["content"], str) for m in kwargs["messages"])
    assert _marks_in(kwargs["messages"], kwargs["tools"]) == 0


def test_off_keeps_the_exact_request_on_a_claude_route():
    agent = _agent(prompt_cache="off")
    kwargs = LLMCaller.__new__(LLMCaller).build_completion_kwargs(agent, _prepared(agent), [_tool()])
    assert all(isinstance(m["content"], str) for m in kwargs["messages"])
    assert _marks_in(kwargs["messages"], kwargs["tools"]) == 0


def test_the_setting_is_inherited_and_the_fallback_uses_the_global_one():
    config = RootConfig.model_validate({
        "llm": {"model": MODEL, "api_key": "k", "prompt_cache": "1h"},
        "agents": {
            "orchestrator": {"name": "O", "role": "R"},
            "coder": {"name": "C", "role": "R", "model": "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
                      "prompt_cache": "off"},
        },
    })
    assert config.agents["orchestrator"].prompt_cache == "1h"
    assert config.agents["coder"].prompt_cache == "off"
    pool = AgentPool(config.agents)
    assert pool.get("orchestrator").prompt_cache == "1h"

    substitute = substitute_for(pool.get("coder"), LLMConfig(model=MODEL, api_key="k", prompt_cache="1h"))
    assert substitute is not None and substitute.prompt_cache == "1h"


# ================================================================ 2. 캐시 마커 주입 위치 및 상한 검증


def test_the_goal_stays_its_own_block_when_the_roles_are_merged():
    prepared = _prepared(_agent())

    goal_turn = prepared[1]
    assert [b["text"] for b in goal_turn["content"]] == ["[목표]", "[Architect]: 발언"]
    assert goal_turn["content"][0]["cache_control"] == MARK
    assert "cache_control" not in goal_turn["content"][1]
    assert [m["role"] for m in prepared] == ["system", "user", "assistant", "user"]


def test_a_single_prompt_has_no_goal_to_mark():
    agent = _agent()
    single = [{"role": "system", "content": "페르소나"}, {"role": "user", "content": "장부를 고쳐 쓰세요"}]
    assert mark_goal(agent, single) is single


def test_the_goal_mark_survives_a_tool_loop_trim():
    agent = _agent(max_context_window=3000, max_tokens=500)
    bulk = "관측 " * 900
    messages = merge_consecutive_roles(mark_goal(agent, [
        {"role": "system", "content": "페르소나"},
        {"role": "user", "content": "[목표]"},
        {"role": "user", "content": "이번 차례"},
    ]))
    for i in range(3):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"t{i}", "type": "function", "function": {"name": "srv__read", "arguments": "{}"}},
        ]})
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "name": "srv__read", "content": bulk})

    trimmed, dropped = fit_tool_loop_context(agent, messages, keep="이번 차례")

    assert dropped
    assert trimmed[1]["content"][0] == {"type": "text", "text": "[목표]", "cache_control": MARK}
    assert _marks_in(trimmed) == 1, "생략 안내는 목표 블록 뒤에 따로 붙습니다"


def test_first_call_marks_tools_system_goal_and_last_own_speech_but_not_the_turn():
    agent = _agent()
    kwargs = LLMCaller.__new__(LLMCaller).build_completion_kwargs(agent, _prepared(agent), [_tool("a"), _tool("b")])
    messages, tools = kwargs["messages"], kwargs["tools"]

    assert "cache_control" not in tools[0] and tools[1]["cache_control"] == MARK
    assert messages[0]["content"] == [{"type": "text", "text": "페르소나", "cache_control": MARK}]
    assert messages[1]["content"][0]["cache_control"] == MARK
    assert messages[2]["content"] == [{"type": "text", "text": "[Coder]: 지난 발언", "cache_control": MARK}]
    assert isinstance(messages[3]["content"], str), "라운드 번호가 든 지시에는 달지 않습니다"
    assert _marks_in(messages, tools) == MAX_CACHE_MARKS


def test_the_tool_loop_tail_takes_the_tool_definitions_place():
    agent = _agent()
    conversation = _prepared(agent) + [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "a", "content": "결과"},
    ]
    messages, tools = with_cache_marks(agent, conversation, [_tool("a")])

    assert messages[-1]["cache_control"] == MARK
    assert messages[2]["content"][0]["cache_control"] == MARK, "라운드 경계는 남깁니다"
    assert "cache_control" not in tools[0]
    assert _marks_in(messages, tools) == MAX_CACHE_MARKS


def test_marking_does_not_touch_what_the_loop_keeps():
    agent = _agent()
    conversation = _prepared(agent)
    snapshot = json.dumps(conversation, ensure_ascii=False)
    tools = [_tool()]

    with_cache_marks(agent, conversation, tools)

    assert json.dumps(conversation, ensure_ascii=False) == snapshot
    assert "cache_control" not in tools[0]


def test_empty_text_is_never_marked():
    """빈 텍스트 블록에 마커를 주입하면 Anthropic API에서 400 Bad Request가 발생합니다."""
    agent = _agent()
    messages, _ = with_cache_marks(agent, [
        {"role": "system", "content": ""},
        {"role": "user", "content": "[목표]"},
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": "이번 차례"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "a", "content": ""},
    ])
    assert _marks_in(messages) == 0
    assert messages[0]["content"] == "" and messages[2]["content"] == "   "


def test_the_last_text_block_carries_the_mark_when_attachments_follow_it():
    agent = _agent()
    attachment = {"type": "mado_attachment", "name": "a.png"}
    messages = mark_goal(agent, [
        {"role": "system", "content": "페르소나"},
        {"role": "user", "content": [{"type": "text", "text": "[목표]"}, {"type": "text", "text": "- a.png"}, attachment]},
        {"role": "user", "content": "이번 차례"},
    ])
    assert "cache_control" not in messages[1]["content"][0]
    assert messages[1]["content"][1]["cache_control"] == MARK
    assert messages[1]["content"][2] == attachment


# ================================================================ 3. 실제 아웃바운드 페이로드 변환 검증


def _loop_request() -> Dict[str, Any]:
    agent = _agent()
    conversation = _prepared(agent) + [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "srv__read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "srv__read", "content": "결과"},
    ]
    return LLMCaller.__new__(LLMCaller).build_completion_kwargs(agent, conversation, [_tool()])


def _anthropic_body(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    config = AnthropicConfig()
    optional = config.map_openai_params(
        non_default_params={k: kwargs[k] for k in ("tools", "tool_choice") if k in kwargs},
        optional_params={}, model="claude-sonnet-4-5", drop_params=True,
    )
    return config.transform_request(
        model="claude-sonnet-4-5", messages=[dict(m) for m in kwargs["messages"]], optional_params=optional,
        litellm_params={}, headers={},
    )


def test_anthropic_receives_the_marks_on_its_own_blocks():
    body = _anthropic_body(_loop_request())

    assert body["system"] == [{"type": "text", "text": "페르소나", "cache_control": MARK}]
    assert body["messages"][0]["content"][0] == {"type": "text", "text": "[목표]", "cache_control": MARK}
    assert body["messages"][1]["content"][0]["cache_control"] == MARK
    assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    assert body["messages"][-1]["content"][0]["cache_control"] == MARK
    assert json.dumps(body, ensure_ascii=False).count("cache_control") == MAX_CACHE_MARKS


def test_bedrock_receives_the_marks_as_cache_points():
    kwargs = _loop_request()
    config = AmazonConverseConfig()
    optional = config.map_openai_params(
        non_default_params={"tools": kwargs["tools"], "tool_choice": "auto"},
        optional_params={}, model=BEDROCK_MODEL, drop_params=True,
    )
    request = config.transform_request(
        model=BEDROCK_MODEL, messages=[dict(m) for m in kwargs["messages"]], optional_params=optional,
        litellm_params={}, headers={},
    )

    point = {"cachePoint": {"type": "default"}}
    assert request["system"] == [{"text": "페르소나"}, point]
    assert request["messages"][0]["content"][:2] == [{"text": "[목표]"}, point]
    assert request["messages"][1]["content"][-1] == point
    assert "toolResult" in request["messages"][-1]["content"][0] and request["messages"][-1]["content"][-1] == point
    assert json.dumps(request).count("cachePoint") == MAX_CACHE_MARKS
    assert "cache_control" not in json.dumps(request)


class _NoTools:
    def get_openai_tools_for_servers(self, servers):
        return []


@pytest.fixture
def anthropic_stub():
    """스트리밍 응답을 모의하는 가짜 Anthropic 서버. 수신된 페이로드를 수집합니다."""
    bodies: List[Dict[str, Any]] = []
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 50, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 1000,
                      "output_tokens": 1},
        }}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "답입니다"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for name, data in events:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", bodies
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_a_real_streamed_call_sends_the_marks_and_meters_cache_tokens(anthropic_stub):
    base, bodies = anthropic_stub
    agent = _agent(api_base=base, system_prompt="페르소나")

    with llm_usage.measuring() as meter:
        content, _ = await LLMCaller(mcp_manager=_NoTools()).call_agent(agent, [
            {"role": "user", "content": "[목표]"},
            {"role": "assistant", "content": "[Coder]: 지난 발언"},
            {"role": "user", "content": "이번 차례"},
        ])

    assert content == "답입니다"
    body = bodies[0]
    assert body["system"][0]["cache_control"] == MARK
    assert body["messages"][0]["content"][0]["cache_control"] == MARK
    assert body["messages"][1]["content"][0]["cache_control"] == MARK
    assert "cache_control" not in json.dumps(body["messages"][2], ensure_ascii=False)
    assert (meter.prompt_tokens, meter.cached_tokens, meter.cache_write_tokens) == (1350, 1000, 300)


# ================================================================ 4. 캐시 적중률 및 토큰 절감 검증


class PrefixCache:
    """Anthropic 프롬프트 캐시 동작 규격을 시뮬레이션합니다. 마커 위치까지의 바이트 접두사를 저장하고 최대 20토큰 역추적 정렬을 수행합니다."""

    LOOKBACK = 20

    def __init__(self) -> None:
        self.entries: set = set()

    @staticmethod
    def _blocks(body: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        blocks: List[Tuple[str, Dict[str, Any]]] = [("tool", tool) for tool in body.get("tools") or []]
        blocks += [("system", block) for block in body.get("system") or []]
        for msg in body["messages"]:
            content = msg["content"]
            content = [{"type": "text", "text": content}] if isinstance(content, str) else content
            blocks += [(msg["role"], block) for block in content]
        return blocks

    def serve(self, body: Dict[str, Any]) -> Tuple[int, int, int]:
        """해당 요청의 `(캐시 적중, 캐시 생성, 기본 처리)` 문자 수."""
        digest = hashlib.sha256()
        keys: List[str] = []
        sizes: List[int] = []
        marks: List[int] = []
        for index, (role, block) in enumerate(self._blocks(body)):
            shown = {k: v for k, v in block.items() if k != "cache_control"}
            rendered = json.dumps({"role": role, **shown}, sort_keys=True, ensure_ascii=False)
            digest.update(rendered.encode())
            keys.append(digest.hexdigest())
            sizes.append(len(rendered))
            if "cache_control" in block:
                marks.append(index)
        assert len(marks) <= MAX_CACHE_MARKS

        read_upto = 0
        for mark in marks:
            for index in range(mark, max(mark - self.LOOKBACK, -1), -1):
                if keys[index] in self.entries:
                    read_upto = max(read_upto, index + 1)
                    break
        write_upto = max(read_upto, marks[-1] + 1) if marks else read_upto
        self.entries.update(keys[mark] for mark in marks)
        return sum(sizes[:read_upto]), sum(sizes[read_upto:write_upto]), sum(sizes[write_upto:])


SPEECH = "캐시 계층은 읽기 경로에 둡니다. " * 60 + "\n## 요지\n- 읽기 경로에 캐시를 둡니다\n"


def _debate_endpoint(tool_rounds: int = 0):
    """전문가는 발언마다 `tool_rounds` 회 도구를 호출하고, 나머지는 즉시 응답합니다. 아웃바운드 요청을 수집합니다."""
    sent: List[Dict[str, Any]] = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")   # 비스트리밍 경로로
        sent.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        last_user = max(i for i, m in enumerate(kwargs["messages"]) if m["role"] == "user")
        done = sum(1 for m in kwargs["messages"][last_user:] if m["role"] == "tool")
        persona = json.dumps(kwargs["messages"][0]["content"], ensure_ascii=False)
        if kwargs.get("tools") and "persona:orchestrator " not in persona and done < tool_rounds:
            call = SimpleNamespace(
                id=f"c{uuid.uuid4().hex[:8]}", function=SimpleNamespace(name="srv__read", arguments="{}"),
            )
            message = SimpleNamespace(
                content="", tool_calls=[call], reasoning_content=None,
                model_dump=lambda: {"role": "assistant", "content": ""},
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])
        message = SimpleNamespace(content=SPEECH, tool_calls=None, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    return sent, fake_acompletion


class _SharedTools:
    def __init__(self, tools):
        self.tools = tools

    def get_openai_tools_for_servers(self, servers):
        return self.tools

    async def execute_tool(self, name, args, scope=None, actor=None):
        return "읽은 내용 " * 300, "success"


async def _run_debate(tool_rounds: int = 0, rounds: int = 3) -> List[Tuple[str, Tuple[int, int, int]]]:
    """실제 오케스트레이션 엔진으로 토론을 구동하고 발생한 요청을 캐시 시뮬레이터에 통과시킵니다. `(에이전트, (캐시 읽기, 캐시 쓰기, 일반))`."""
    await init_db(DB_URL)
    sid = f"cache-{uuid.uuid4().hex[:8]}"
    keys = ("orchestrator", "architect", "coder", "critic")
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="캐시", strategy="sequential_debate", max_rounds=rounds, active_agents=list(keys),
            converge_early=False,
        ))
        await db.commit()

    pool = AgentPool({
        key: AgentConfig(
            name=key.title(), role="Role", model=MODEL, api_key="k", system_prompt=f"persona:{key} " + "규칙. " * 400,
            allowed_mcp_servers=["srv"],
        )
        for key in keys
    })
    tools = [_tool(f"srv__{name}") for name in ("read", "write", "list")]
    sent, fake = _debate_endpoint(tool_rounds)
    caller = LLMCaller(mcp_manager=_SharedTools(tools))
    # 턴이 빌린 런타임 대신 호출기의 도구를 쓰게 합니다 (`EngineContext.mcp_for`).
    with patch("litellm.acompletion", side_effect=fake), patch.object(EngineContext, "mcp_for", return_value=None):
        await OrchestratorEngine(agent_pool=pool, llm_caller=caller).run_turn(session_id=sid, user_prompt="캐시 설계")

    cache = PrefixCache()
    served = []
    for request in sent:
        persona = json.dumps(request["messages"][0]["content"], ensure_ascii=False)
        key = next(k for k in keys if f"persona:{k} " in persona)
        served.append((key, cache.serve(_anthropic_body(request))))
    return served


def _cost(served: List[Tuple[int, int, int]]) -> float:
    """캐시 마커 미적용 대비 상대 비용 비율 산출 (읽기 0.1배, 쓰기 1.25배 가중치 적용)."""
    total = sum(read + written + plain for read, written, plain in served)
    return sum(read * 0.1 + written * 1.25 + plain for read, written, plain in served) / total


@pytest.mark.asyncio
async def test_each_round_reads_what_the_round_before_wrote():
    served = await _run_debate()
    shared = next(numbers for k, numbers in served if k == "architect")[0]
    assert shared > 0

    for key in ("architect", "coder", "critic"):
        calls = [numbers for k, numbers in served if k == key]
        assert len(calls) >= 3
        reads = [read for read, _w, _p in calls]
        assert reads[0] == shared, "첫 발언이 읽는 것은 에이전트끼리 나눠 쓰는 도구 정의뿐입니다"
        assert shared < reads[1] < reads[2], f"{key}: 라운드가 쌓일수록 더 많이 읽어야 합니다 ({reads})"

    specialists = [numbers for k, numbers in served if k != "orchestrator"]
    assert _cost(specialists) < 0.8


@pytest.mark.asyncio
async def test_a_tool_loop_reads_its_own_previous_call():
    served = await _run_debate(tool_rounds=4)

    for key in ("architect", "coder", "critic"):
        calls = [numbers for k, numbers in served if k == key]
        first_speech = calls[:5]   # 도구 네 번 + 답 한 번
        reads = [read for read, _w, _p in first_speech]
        assert reads == sorted(reads) and reads[1] > reads[0]
        for (read, _w, _p), (prev_read, prev_written, _pp) in zip(first_speech[2:], first_speech[1:]):
            assert read >= prev_read + prev_written, f"{key}: 앞 판이 쓴 것까지 읽어야 합니다"

    assert _cost([numbers for k, numbers in served if k != "orchestrator"]) < 0.5
