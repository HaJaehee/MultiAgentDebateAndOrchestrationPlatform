"""대화형 워크플로 빌더 단위 테스트 (app/orchestration/workflow_builder.py, ADR-050).

검증 목표:

1. **응답 파싱** — `agents` 및 `graph` 블록을 함께 추출하고 사용자 노출 본문에서 깔끔히 분리한다. `agent` 태그와 단일 객체 형식도 수용하며 `json` 태그는 무시한다. 업무 지시서 내의 백틱(```)에 의해 파싱이 끊기지 않는다. 파싱 실패 블록은 기존 값을 유지하고 자체 교정을 요청한다.
2. **컨텍스트 최적화** — 현재 설계도는 최신 사용자 메시지에만 첨부하고, 대화 이력에는 블록 대신 요약 안내 문구만 남긴다.
3. **키 충돌 방지** — 새 에이전트 키가 기존 키(비활성화 및 오케스트레이터 포함)와 중복되면 검증 오류로 차단한다. 중복된 채로 저장되어 노드가 기존 에이전트를 가리킴으로써 엉뚱한 에이전트가 발언하는 결함을 예방한다.
4. **일관된 검증** — 그래프 명세는 적응형 그래프와 동일한 `draft_report` 검증을 통과해야 한다 (판정 노드 없는 무한 루프 차단, 노드 수 및 호출 횟수 상한).
5. **자가 교정** — 이번 응답으로 발생한 오류에 대해서만 1회 교정을 재요청하며, 해당 교정 왕복 대화는 이력에 남기지 않는다.
6. **안전한 저장** — 실제 파일 기록 전에 모든 사전 검증을 완료하고 conf.json에 일괄 기록한다. 진행 중인 대화가 있으면 저장을 거부한다. 저장 직전 키 경합 시 노드 참조 키도 함께 치환한다. 그래프 저장이 실패해도 conf.json은 롤백하지 않고 사실을 알린다.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.agents import pool as pool_module
from app.agents.catalog import Option
from app.agents.draft import AgentDraft, SaveRefused
from app.agents.pool import AgentPool
from app.config import (
    AgentConfig,
    add_agent_to_conf_file,
    add_agents_to_conf_file,
    get_config,
    load_config,
    read_conf_file,
    write_conf_file,
)
from app.config import editors as editors_module
from app.config import loader as config_module
from app.graph_store import load_graph
from app.orchestration import graph_draft
from app.orchestration import workflow_builder as wb
from app.orchestration.graph import validate_graph
from tests.fake_llm import FakeLLMCaller

SERVERS = [Option("filesystem", "파일 열어 보기·만들기", "")]

SAMPLE = {
    "llm": {"model": "${LLM_MODEL:-openai/gpt-4o}", "api_key": "${LLM_API_KEY:-sk-test}"},
    "mcp_servers": {"filesystem": {"command": "node", "args": ["server.js"]}},
    "agents": {
        "// orchestrator": "필수 오케스트레이터",
        "orchestrator": {"name": "Master Orchestrator", "role": "Moderator", "allowed_mcp_servers": []},
        "coder": {"name": "Senior Python Engineer", "role": "Implementation", "allowed_mcp_servers": ["filesystem"]},
        "critic": {"name": "Quality Critic", "role": "Review", "allowed_mcp_servers": [], "enabled": False},
    },
}

AUDITOR = {
    "key": "contract_auditor",
    "name": "컨트랙트 보안관",
    "role": "스마트 컨트랙트 취약점 점검",
    "system_prompt": "당신은 Web3 보안 전문가입니다.\n1. 코드를 읽습니다.\n2. 취약점을 적습니다.",
    "allowed_mcp_servers": ["filesystem", "ghost"],
    "allowed_skills": [],
    "card_color": "#e64a19",
    "icon": "security",
}


def audit_graph(**overrides) -> Dict[str, Any]:
    """새 보안관이 점검하고, 치명적 취약점이 남으면 coder 가 고친 뒤 다시 점검하는 그래프."""
    data = {
        "name": "스마트 컨트랙트 보안 감사",
        "nodes": [
            {"id": "audit", "type": "agent", "agent": "contract_auditor", "label": "보안 점검",
             "instruction": "취약점을 점검하세요.", "max_visits": 2},
            {"id": "check", "type": "gate", "question": "치명적인 취약점이 남아 있습니까?", "max_visits": 2},
            {"id": "fix", "type": "agent", "agent": "coder", "label": "취약점 수정",
             "instruction": "지적된 취약점을 고치세요.", "max_visits": 2},
        ],
        "edges": [
            {"from": "start", "to": "audit"},
            {"from": "audit", "to": "check"},
            {"from": "check", "to": "fix", "port": "yes"},
            {"from": "check", "to": "end", "port": "no"},
            {"from": "fix", "to": "audit", "carry": "refs"},
        ],
    }
    data.update(overrides)
    return data


def answer(text: str = "보안관을 새로 만들고 흐름을 그렸습니다.", agents: Any = None, graph: Any = None) -> str:
    parts = [text]
    if agents is not None:
        parts.append(f"```agents\n{json.dumps(agents, ensure_ascii=False)}\n```")
    if graph is not None:
        parts.append(f"```graph\n{json.dumps(graph, ensure_ascii=False)}\n```")
    return "\n\n".join(parts)


def offline_context(**kwargs) -> wb.BuilderContext:
    """conf.json 없이 만든 구성: 켜진 coder 하나, 꺼진 critic, 오케스트레이터."""
    pool = AgentPool({
        "orchestrator": AgentConfig(name="Master Orchestrator", role="Moderator", model="openai/gpt-4o-mini",
                                    api_key="sk-test"),
        "coder": AgentConfig(name="Senior Python Engineer", role="Implementation", api_key="sk-test",
                             allowed_mcp_servers=["filesystem"]),
    })
    return wb.builder_context(SERVERS, [], pool=pool, configured=["orchestrator", "coder", "critic"], **kwargs)


def drafted(ctx: wb.BuilderContext, agents=None, graph=None) -> wb.CompositeDraft:
    draft, _ = wb.apply_reply(
        wb.CompositeDraft(), wb.split_composite_reply(answer(
            agents=[AUDITOR] if agents is None else agents, graph=audit_graph() if graph is None else graph,
        )), ctx,
    )
    return draft


@pytest.fixture()
def live_conf(tmp_path: Path):
    """임시 conf.json 을 지금 설정으로 쓰고, 끝나면 원래 설정과 풀로 되돌립니다."""
    path = tmp_path / "conf.json"
    write_conf_file(path, SAMPLE)
    saved = (config_module._config, config_module._config_path, pool_module._agent_pool)  # noqa: SLF001
    get_config(reload=True, config_path=path)
    pool_module._agent_pool = None  # noqa: SLF001
    yield path
    config_module._config, config_module._config_path, pool_module._agent_pool = saved  # noqa: SLF001


# =============================================================== 1. 답 읽기


def test_both_blocks_are_read_and_removed_from_the_visible_text():
    reply = wb.split_composite_reply(answer(agents=[AUDITOR], graph=audit_graph()))
    assert reply.text == "보안관을 새로 만들고 흐름을 그렸습니다."
    assert reply.agents == [AUDITOR] and reply.graph == audit_graph()
    assert not reply.broken and reply.has_blocks


def test_a_single_agent_object_and_the_agent_tag_are_accepted_but_a_json_tag_is_not():
    text = f"설명\n```agent\n{json.dumps(AUDITOR, ensure_ascii=False)}\n```"
    assert wb.split_composite_reply(text).agents == [AUDITOR]

    ambiguous = wb.split_composite_reply(f"설명\n```json\n{json.dumps(audit_graph())}\n```")
    assert ambiguous.agents is None and ambiguous.graph is None, "블록 유형이 불분명한 json 태그는 파싱 대상에서 제외합니다"


def test_a_fence_inside_a_system_prompt_does_not_cut_the_block():
    agent = dict(AUDITOR, system_prompt="당신은 검토자입니다.\n```python\nprint('hi')\n```\n위처럼 예시를 답니다.")
    reply = wb.split_composite_reply(answer(agents=[agent], graph=audit_graph()))
    assert reply.agents[0]["system_prompt"] == agent["system_prompt"]
    assert reply.graph == audit_graph() and reply.text == "보안관을 새로 만들고 흐름을 그렸습니다."


def test_a_broken_block_keeps_the_current_value_and_asks_for_a_fix():
    ctx = offline_context()
    current = drafted(ctx)
    broken = wb.split_composite_reply("고쳤습니다.\n```graph\n{\"nodes\": [ \n```")
    assert broken.graph is None and broken.broken and broken.text == "고쳤습니다."

    draft, problems = wb.apply_reply(current, broken, ctx)
    assert draft.graph == current.graph, "파싱할 수 없는 응답이 오더라도 마지막으로 정상 파싱된 그래프를 유지합니다"
    assert any("그래프 블록" in p for p in problems)


def test_the_blocks_are_hidden_while_they_stream():
    assert wb.streaming_text("흐름을 그렸습니다.\n```gra") == "흐름을 그렸습니다.\n```gra"
    assert wb.streaming_text("흐름을 그렸습니다.\n```graph\n{\"nodes\"") == "흐름을 그렸습니다."
    assert wb.streaming_text("만들었습니다.\n```agents\n[") == "만들었습니다."


# =============================================================== 2. 맥락 크기


def test_the_current_draft_rides_only_on_the_last_user_message():
    ctx = offline_context()
    draft = drafted(ctx)
    history = [
        {"role": "user", "content": "감사 흐름을 만들어 줘"},
        {"role": "assistant", "content": "만들었습니다."},
        {"role": "user", "content": "수정은 두 번까지만"},
    ]
    messages = wb.request_messages(history, draft)
    assert wb.CURRENT_DRAFT_MARKER not in messages[0]["content"]
    assert messages[2]["content"].startswith("수정은 두 번까지만\n\n" + wb.CURRENT_DRAFT_MARKER)
    payload = json.loads(messages[2]["content"].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert [a["key"] for a in payload["agents"]] == ["contract_auditor"]
    assert '"pos"' not in json.dumps(payload["graph"]), "편집기 좌표 정보는 도우미 에이전트에게 불필요하므로 전송 데이터에서 제외합니다"
    assert history[2]["content"] == "수정은 두 번까지만", "대화 이력 원본은 수정하지 않고 그대로 보존합니다"
    assert wb.request_messages(history, wb.CompositeDraft()) == history, "빈 설계도는 메시지에 첨부하지 않습니다"


def test_the_record_keeps_a_line_instead_of_the_blocks():
    compact = wb.compact_reply(answer(agents=[AUDITOR], graph=audit_graph()))
    assert "```" not in compact and wb.COMPACT_NOTE in compact
    assert wb.compact_reply("무엇을 맡기실까요?") == "무엇을 맡기실까요?"


# =============================================================== 3. 키 충돌 · 4. 같은 검사


@pytest.mark.parametrize("key", ["coder", "critic", "orchestrator"])
def test_a_new_key_that_already_exists_is_an_error(key):
    ctx = offline_context()
    agent = dict(AUDITOR, key=key)
    graph = audit_graph()
    graph["nodes"][0]["agent"] = key
    draft = drafted(ctx, agents=[agent], graph=graph)
    assert any(f"새 에이전트 키 {key} 는 이미 있습니다" in e for e in draft.errors), draft.errors


def test_the_collision_would_otherwise_pass_silently():
    """키 중복 검사가 누락되었을 때의 부작용 검증: 풀에 기존 `coder`가 존재하므로 그래프 검증 자체는 통과해 버립니다."""
    ctx = offline_context()
    graph = audit_graph()
    graph["nodes"][0]["agent"] = "coder"
    draft = drafted(ctx, agents=[dict(AUDITOR, key="coder")], graph=graph)
    report = graph_draft.draft_report(draft.graph, ctx.candidate_keys, ctx.max_visits)
    assert report.ok, "그래프 자체만 보면 유효해 보이지만, 키 충돌을 별도로 검사해야 합니다"
    assert draft.errors, "빌더 유효성 검사에서 키 충돌을 정상 차단합니다"


def test_new_agents_must_be_usable_and_used():
    ctx = offline_context()
    agents = [
        dict(AUDITOR),
        dict(AUDITOR, key="Bad-Key", name="키가 틀린 에이전트"),
        dict(AUDITOR, key="contract_auditor", name="같은 키 두 번"),
        dict(AUDITOR, key="idle_helper", name="쉬는 도우미", system_prompt=""),
    ]
    draft = drafted(ctx, agents=agents)
    joined = "\n".join(draft.errors)
    assert "Bad-Key" in joined
    assert "업무 지시서" in joined and "쉬는 도우미" in joined
    assert any("idle_helper" in w and "쓰이지 않습니다" in w for w in draft.warnings)
    assert len([a for a in draft.new_agents if a.key == "contract_auditor"]) == 1, "동일 키가 중복 전달될 경우 마지막 항목 하나만 반영합니다"


def test_unknown_tools_are_dropped_from_new_agents():
    draft = drafted(offline_context())
    assert draft.new_agents[0].allowed_mcp_servers == ["filesystem"]
    assert draft.new_agents[0].card_color == "#e64a19" and draft.new_agents[0].icon == "security"


def test_the_graph_passes_the_same_checks_as_an_adaptive_graph():
    ctx = offline_context()
    ok = drafted(ctx)
    assert ok.errors == [] and ok.worst_case_calls == 8, (ok.errors, ok.worst_case_calls)

    no_gate = audit_graph(edges=[
        {"from": "start", "to": "audit"}, {"from": "audit", "to": "fix"},
        {"from": "fix", "to": "audit"}, {"from": "fix", "to": "end"},
    ])
    assert any("멈출 조건" in e for e in drafted(ctx, graph=no_gate).errors)

    many = audit_graph(
        nodes=[{"id": f"n{i}", "type": "agent", "agent": "coder"} for i in range(13)],
        edges=[{"from": "start", "to": "n0"}] + [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(12)]
              + [{"from": "n12", "to": "end"}],
    )
    assert any("12개까지만" in e for e in drafted(ctx, agents=[], graph=many).errors)


def test_both_prompts_share_the_graph_format_and_the_builder_lists_what_it_must_not_reuse():
    ctx = offline_context()
    prompt = wb.workflow_builder_prompt(ctx)
    assert graph_draft.GRAPH_FORMAT_GUIDE in prompt
    assert "coder: Senior Python Engineer" in prompt
    taken_line = prompt.split("새 전문가는 이 키를 쓸 수 없습니다\n", 1)[1].split("\n", 1)[0]
    assert {"coder", "critic", "orchestrator"} <= set(taken_line.split(", ")), "비활성화된 에이전트의 키도 중복 사용이 불가능해야 합니다"
    assert "`filesystem`" in prompt and "{tools}" not in prompt and "{colors}" not in prompt

    adaptive = graph_draft.draft_prompt(goal="g", record="", plan="", roster="- coder", max_rounds=3, parallel_limit=2)
    assert graph_draft.GRAPH_FORMAT_GUIDE in adaptive[0]["content"]


@pytest.mark.asyncio
async def test_the_builder_borrows_the_orchestrator_connection_without_any_tools():
    class Recording(FakeLLMCaller):
        def __init__(self):
            super().__init__(replies={"orchestrator": answer(agents=[], graph=audit_graph())})
            self.agents: List[Any] = []

        async def call_agent(self, agent, messages, *args, **kwargs):
            self.agents.append(agent)
            return await super().call_agent(agent, messages, *args, **kwargs)

    ctx = offline_context()
    llm = Recording()
    pool = AgentPool({"orchestrator": AgentConfig(
        name="Master Orchestrator", role="Moderator", model="openai/gpt-4o-mini", api_key="sk-test",
        allowed_mcp_servers=["filesystem"], sequential_thinking={"enabled": True},
    )})
    content = await wb.ask_workflow_builder([{"role": "user", "content": "감사"}], ctx, llm=llm, pool=pool)
    agent = llm.agents[0]
    assert agent.model == "openai/gpt-4o-mini" and agent.allowed_mcp_servers == []
    assert agent.sequential_thinking.enabled is False
    assert "워크플로 설계 도우미" in agent.system_prompt
    assert wb.split_composite_reply(content).graph == audit_graph()


# =============================================================== 합치기


def test_a_reply_merges_agents_by_key_and_the_list_is_the_whole_list():
    ctx = offline_context()
    current = drafted(ctx).model_copy(deep=True)
    current.new_agents[0] = current.new_agents[0].model_copy(update={"name": "보안관 (고침)"})
    helper = dict(AUDITOR, key="report_writer", name="보고서 작성자")

    draft, _ = wb.apply_reply(current, wb.split_composite_reply(answer(
        agents=[{"key": "contract_auditor", "role": "취약점 점검과 등급"}, helper],
    )), ctx)
    auditor = draft.agent("contract_auditor")
    assert auditor.name == "보안관 (고침)", "응답에 누락된 필드는 현재 값(사용자가 화면에서 수정한 값)을 그대로 유지합니다"
    assert auditor.role == "취약점 점검과 등급"
    assert [a.key for a in draft.new_agents] == ["contract_auditor", "report_writer"]

    emptied, _ = wb.apply_reply(draft, wb.split_composite_reply(answer(agents=[])), ctx)
    assert emptied.new_agents == [] and emptied.graph == draft.graph, "agents 블록만 전달된 경우 그래프는 기존 설계를 유지합니다"


def test_problems_are_counted_only_for_what_the_reply_made():
    ctx = offline_context()
    current = wb.remove_new_agent(drafted(ctx), "contract_auditor", ctx)
    assert current.errors, "사용자가 랙에서 제거한 에이전트를 참조하는 노드는 오류로 감지되어야 합니다"
    _, problems = wb.apply_reply(current, wb.split_composite_reply("어떤 점검을 원하시나요?"), ctx)
    assert problems == [], "블록이 없는 일반 응답이나 사용자의 직접 수정으로 발생한 오류로는 재요청을 유발하지 않습니다"


def test_a_removed_agent_is_announced_and_cleared_when_the_assistant_brings_it_back():
    ctx = offline_context()
    removed = wb.remove_new_agent(drafted(ctx), "contract_auditor", ctx)
    assert removed.removed_agents == ["contract_auditor"]
    assert '"removed_agents": ["contract_auditor"]' in wb.with_composite_draft("다시", removed)
    back, _ = wb.apply_reply(removed, wb.split_composite_reply(answer(agents=[AUDITOR])), ctx)
    assert back.removed_agents == [] and back.errors == []


def test_editor_positions_carry_over_only_when_the_nodes_stay():
    ctx = offline_context()
    spec = drafted(ctx).graph
    placed = spec.model_copy(update={"nodes": [n.model_copy(update={"pos": (10.0 * i, 5.0)})
                                               for i, n in enumerate(spec.nodes)]})
    start = wb.draft_from_graph(placed.model_copy(update={"id": "my-graph"}))
    assert start.base_graph_id == "my-graph" and start.graph.id == wb.DRAFT_GRAPH_ID

    same, _ = wb.apply_reply(start, wb.split_composite_reply(answer(graph=audit_graph(name="이름만 바꿈"))), ctx)
    assert [n.pos for n in same.graph.nodes] == [n.pos for n in placed.nodes]

    grown = audit_graph()
    grown["nodes"].append({"id": "notes", "type": "agent", "agent": "coder", "label": "기록"})
    grown["edges"].append({"from": "audit", "to": "notes"})
    grown["edges"].append({"from": "notes", "to": "end"})
    moved, _ = wb.apply_reply(start, wb.split_composite_reply(answer(graph=grown)), ctx)
    assert all(n.pos is None for n in moved.graph.nodes), "새 노드가 추가되면 겹침을 방지하기 위해 전체 노드를 자동 배치합니다"


def test_renaming_a_new_agent_in_the_rack_renames_its_nodes():
    ctx = offline_context()
    draft = drafted(ctx)
    fixed = draft.new_agents[0].model_copy(update={"key": "web3_auditor"})
    renamed = wb.replace_new_agent(draft, "contract_auditor", fixed, ctx)
    assert renamed.graph.node("audit").agent == "web3_auditor"
    assert renamed.errors == [] and renamed.graph_data["nodes"][0]["agent"] == "web3_auditor"


# =============================================================== 5. 자가 교정 (창)


@pytest.mark.asyncio
async def test_the_chat_corrects_a_bad_draft_once_and_keeps_the_exchange_out_of_the_record(monkeypatch):
    from app.ui.components import graph_builder_dialog as dialog_module

    no_gate = audit_graph(edges=[
        {"from": "start", "to": "audit"}, {"from": "audit", "to": "fix"},
        {"from": "fix", "to": "audit"}, {"from": "fix", "to": "end"},
    ])
    replies = [answer(agents=[AUDITOR], graph=no_gate), answer("판정을 넣었습니다.", graph=audit_graph())]
    calls: List[List[Dict[str, str]]] = []

    async def fake_ask(messages, ctx, *, on_chunk=None, **_):
        calls.append(messages)
        return replies[len(calls) - 1]

    monkeypatch.setattr(dialog_module, "ask_workflow_builder", fake_ask)
    chat = dialog_module.GraphBuilderChat(offline_context(), on_finish=None, finish_label="저장")
    chat.history.append({"role": "user", "content": chat.user_entry("감사 흐름을 만들어 줘")})
    restarts: List[str] = []

    content = await chat.ask(lambda _delta: None, restarts.append)
    visible = chat.take_reply(content)

    assert len(calls) == 2 and restarts == [dialog_module.RETRY_NOTE]
    assert calls[1][-1]["content"].startswith(wb.CORRECTION_MARKER) and "멈출 조건" in calls[1][-1]["content"]
    assert chat.composite.errors == [] and chat.composite.agent("contract_auditor") is not None, \
        "고쳐 받은 답에 agents 블록이 없어도 첫 답의 새 에이전트는 남습니다"
    assert visible.startswith(dialog_module.CORRECTED_NOTE) and "판정을 넣었습니다." in visible
    assert chat.history == [
        {"role": "user", "content": "감사 흐름을 만들어 줘"},
        {"role": "assistant", "content": f"판정을 넣었습니다.\n\n{wb.COMPACT_NOTE}"},
    ], "틀린 답과 고쳐 달라는 말은 기록에 남지 않고, 사용자 말에는 설계도를 붙이지 않습니다"


@pytest.mark.asyncio
async def test_a_question_without_blocks_is_not_retried(monkeypatch):
    from app.ui.components import graph_builder_dialog as dialog_module

    calls = []

    async def fake_ask(messages, ctx, *, on_chunk=None, **_):
        calls.append(messages)
        return "누가 검토를 맡으면 좋을까요?"

    monkeypatch.setattr(dialog_module, "ask_workflow_builder", fake_ask)
    chat = dialog_module.GraphBuilderChat(offline_context(), on_finish=None, finish_label="저장")
    chat.history.append({"role": "user", "content": "검토 흐름"})
    visible = chat.take_reply(await chat.ask(lambda _d: None, lambda _n: None))
    assert len(calls) == 1 and visible == "누가 검토를 맡으면 좋을까요?" and chat.composite.graph is None


# =============================================================== 6. 저장


def test_save_registers_every_new_agent_in_one_write_and_saves_the_graph(live_conf, tmp_path, monkeypatch):
    graphs = tmp_path / "graphs"
    writes = []
    real_write = editors_module.write_conf_file
    monkeypatch.setattr(editors_module, "write_conf_file", lambda p, d: (writes.append(p), real_write(p, d)))

    ctx = wb.builder_context(SERVERS, [])
    helper = dict(AUDITOR, key="report_writer", name="보고서 작성자")
    graph = audit_graph()
    graph["nodes"].append({"id": "report", "type": "agent", "agent": "report_writer", "label": "보고서"})
    graph["edges"] = [e for e in graph["edges"] if e.get("port") != "no"] + [
        {"from": "check", "to": "report", "port": "no"}, {"from": "report", "to": "end"},
    ]
    draft = drafted(ctx, agents=[AUDITOR, helper], graph=graph)
    assert draft.errors == [], draft.errors

    saved = wb.save_workflow(draft, wb.builder_context(SERVERS, []), graph_id="ai-test", directory=graphs)

    assert len(writes) == 1, "여러 에이전트를 conf.json에 한 번에 일괄 기록합니다"
    assert saved.agent_keys == ["contract_auditor", "report_writer"] and saved.renamed == {}
    raw = read_conf_file(live_conf)
    assert raw["agents"]["// orchestrator"] == "필수 오케스트레이터", "설정 파일의 주석용 키를 유지합니다"
    block = raw["agents"]["contract_auditor"]
    assert "model" not in block and "api_key" not in block, "모델과 API 키는 llm 기본 설정을 상속받도록 생략합니다"
    assert block["allowed_mcp_servers"] == ["filesystem"]
    assert pool_module.get_agent_pool().get("report_writer") is not None, "서버 재시작 없이도 에이전트 풀에 즉시 반영됩니다"

    stored = load_graph("ai-test", graphs)
    assert stored.agent_keys() == ["contract_auditor", "coder", "report_writer"]
    known = {key: True for key in ("coder", "contract_auditor", "report_writer")}
    assert validate_graph(stored, known).ok


def test_a_key_taken_while_talking_is_renamed_together_with_its_nodes(live_conf, tmp_path):
    draft = drafted(wb.builder_context(SERVERS, []))
    assert draft.errors == []
    # 대화하는 사이 다른 창에서 같은 키의 에이전트가 생겼습니다.
    add_agent_to_conf_file("contract_auditor", "다른 창의 보안관", "다른 역할", config_path=live_conf)
    pool_module.reload_agent_pool()

    saved = wb.save_workflow(draft, wb.builder_context(SERVERS, []), graph_id="ai-race", directory=tmp_path)
    assert saved.renamed == {"contract_auditor": "contract_auditor_2"}
    assert saved.spec.node("audit").agent == "contract_auditor_2", "노드가 다른 에이전트를 가리키지 않고 치환된 새 키를 정확히 참조해야 합니다"
    assert load_config(live_conf).agents["contract_auditor"].name == "다른 창의 보안관"
    assert load_config(live_conf).agents["contract_auditor_2"].name == "컨트랙트 보안관"


def test_save_is_refused_while_any_debate_runs_and_writes_nothing(live_conf, tmp_path):
    draft = drafted(wb.builder_context(SERVERS, []))
    before = live_conf.read_bytes()
    with pytest.raises(SaveRefused, match="진행 중인 대화"):
        wb.save_workflow(draft, wb.builder_context(SERVERS, []), graph_id="ai-run", running=["s1"],
                         directory=tmp_path / "graphs")
    assert live_conf.read_bytes() == before and not (tmp_path / "graphs").exists()


def test_a_graph_without_new_agents_is_saved_even_while_a_debate_runs(live_conf, tmp_path):
    graph = {"name": "한 명", "nodes": [{"id": "work", "type": "agent", "agent": "coder"}],
             "edges": [{"from": "start", "to": "work"}, {"from": "work", "to": "end"}]}
    draft = drafted(wb.builder_context(SERVERS, []), agents=[], graph=graph)
    saved = wb.save_workflow(draft, wb.builder_context(SERVERS, []), graph_id="ai-one", running=["s1"],
                             directory=tmp_path)
    assert saved.agent_keys == [] and load_graph("ai-one", tmp_path).agent_keys() == ["coder"]


def test_a_draft_with_errors_is_refused_before_anything_is_written(live_conf, tmp_path):
    ctx = wb.builder_context(SERVERS, [])
    no_gate = audit_graph(edges=[
        {"from": "start", "to": "audit"}, {"from": "audit", "to": "fix"},
        {"from": "fix", "to": "audit"}, {"from": "fix", "to": "end"},
    ])
    before = live_conf.read_bytes()
    with pytest.raises(SaveRefused, match="멈출 조건"):
        wb.save_workflow(drafted(ctx, graph=no_gate), ctx, graph_id="ai-bad", directory=tmp_path / "graphs")
    with pytest.raises(SaveRefused, match="그래프가 없습니다"):
        wb.save_workflow(wb.CompositeDraft(), ctx, graph_id="ai-none", directory=tmp_path / "graphs")
    assert live_conf.read_bytes() == before and not (tmp_path / "graphs").exists()


def test_a_failed_graph_write_keeps_the_agents_and_says_so(live_conf, tmp_path, monkeypatch):
    def broken_disk(*_args, **_kwargs):
        raise OSError("디스크가 가득 찼습니다")

    monkeypatch.setattr(wb, "save_graph", broken_disk)
    draft = drafted(wb.builder_context(SERVERS, []))
    saved = wb.save_workflow(draft, wb.builder_context(SERVERS, []), graph_id="ai-disk", directory=tmp_path)
    assert "에이전트 1명은 등록했지만" in saved.graph_error and "디스크가 가득 찼습니다" in saved.graph_error
    assert "contract_auditor" in read_conf_file(live_conf)["agents"], "그래프 파일 저장이 실패하더라도 conf.json 변경 사항은 롤백하지 않습니다"


def test_the_editor_path_registers_agents_without_writing_a_graph_file(live_conf, tmp_path):
    draft = drafted(wb.builder_context(SERVERS, []))
    saved = wb.save_workflow(draft, wb.builder_context(SERVERS, []), graph_id="my-graph", write_graph=False,
                             directory=tmp_path / "graphs")
    assert saved.spec.id == "my-graph" and saved.agent_keys == ["contract_auditor"]
    assert not (tmp_path / "graphs").exists(), "편집기 경로에서는 파일 저장을 편집기의 자체 [저장] 기능에 위임합니다"


def test_new_graph_ids_are_not_made_from_korean_names(tmp_path):
    now = datetime(2026, 11, 4, 9, 30)
    assert wb.new_graph_id(tmp_path, now) == "ai-20261104-0930"
    (tmp_path / "ai-20261104-0930.json").write_text("{}", encoding="utf-8")
    assert wb.new_graph_id(tmp_path, now) == "ai-20261104-0930-2"


# =============================================================== conf.json 한 번에 쓰기


def test_adding_several_agents_is_all_or_nothing(tmp_path):
    path = tmp_path / "conf.json"
    write_conf_file(path, SAMPLE)
    before = path.read_bytes()
    one = {"agent_key": "writer", "name": "작성자", "role": "작성"}

    with pytest.raises(ValueError, match="coder"):
        add_agents_to_conf_file([one, {"agent_key": "coder", "name": "또 코더", "role": "구현"}], path)
    with pytest.raises(ValueError, match="두 번"):
        add_agents_to_conf_file([one, dict(one)], path)
    with pytest.raises(ValueError, match="이름"):
        add_agents_to_conf_file([one, {"agent_key": "nameless", "name": "", "role": "역할"}], path)
    assert path.read_bytes() == before, "입력값 중 하나라도 잘못되면 conf.json에 아무것도 기록하지 않아야 합니다"

    assert add_agents_to_conf_file([one, {"agent_key": "reader", "name": "독자", "role": "읽기",
                                          "system_prompt": "한 줄\n두 줄"}], path) == ["writer", "reader"]
    agents = read_conf_file(path)["agents"]
    assert agents["writer"] == {"name": "작성자", "role": "작성", "allowed_mcp_servers": []}
    assert agents["reader"]["system_prompt"] == ["한 줄", "두 줄"]
    assert add_agents_to_conf_file([], path) == []


def test_adding_one_agent_still_behaves_as_before(tmp_path):
    path = tmp_path / "conf.json"
    write_conf_file(path, SAMPLE)
    add_agent_to_conf_file("writer", "작성자", "작성", config_path=path, card_color="#009688")
    assert read_conf_file(path)["agents"]["writer"] == {
        "name": "작성자", "role": "작성", "card_color": "#009688", "allowed_mcp_servers": [],
    }
    with pytest.raises(ValueError, match="이미"):
        add_agent_to_conf_file("writer", "작성자", "작성", config_path=path)


# =============================================================== 화면 연결


def test_the_two_entry_points_finish_the_way_the_design_says():
    """대화 화면은 새 파일로 저장하여 연결하고, 편집기는 파일 저장 없이 캔버스에 즉시 반영합니다."""
    import inspect

    from app.ui import graph_page
    from app.ui.components.roster.graph_row import GraphRowComponent

    finish = inspect.getsource(GraphRowComponent._finish_builder)
    assert "save_workflow(" in finish and "new_graph_id()" in finish and "running_sessions()" in finish
    assert "personas_locked" in finish and "config_changed_now()" in finish
    assert "strategy" not in finish.replace("# 전략은 바꾸지 않습니다", ""), "전략은 바꾸지 않습니다"

    page = inspect.getsource(graph_page.create_graph_page)
    apply = page[page.index("async def apply_builder"):]
    assert "write_graph=False" in apply and '"replaceGraph"' in apply and "save_graph(" not in apply
    assert "agent_palette.refresh()" in apply
