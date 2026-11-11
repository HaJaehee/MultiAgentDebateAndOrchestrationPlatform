"""ADR-050: 대화로 실행 그래프와 새 에이전트를 함께 설계하고, 검사를 끝낸 뒤 한 번에 저장합니다.

화면(대화창) 없이 같은 순서를 밟습니다 — 도우미에게 묻고(`ask_workflow_builder`), 답의 ```agents·```graph 블록을
읽어 설계도에 합치고(`split_composite_reply`·`apply_reply`), 문제가 있으면 한 번만 고쳐 달라고 하고
(`correction_message`), 저장합니다(`save_workflow`). 그런 다음 **저장된 그래프와 새 에이전트로 실제 턴을 돌립니다.**

확인하려는 것은 DeepSeek 가 그 형식을 지켜 쓸 수 있는 설계도를 내는지, 저장이 약속대로(키 충돌·실행 중 잠금·
한 번에 쓰기) 되는지, 그리고 새 에이전트가 실제로 **전문가로서** 발언하는지입니다.
"""

import json

import pytest

REQUEST = (
    "릴리스 노트 초안을 만드는 흐름을 설계해 주세요. 새 전문가 두 명이 필요합니다: 변경 사항을 사용자 관점에서 요약하는 "
    "'릴리스 노트 작가'(키는 release_writer)와, 호환성이 깨지는 변경을 찾아내는 '호환성 검토자'(키는 compat_reviewer). "
    "흐름은 작가가 초안을 쓰고 → 검토자가 검토하고 → 오케스트레이터가 '고칠 것이 남았는가' 를 판정해 남았으면 작가에게 "
    "되돌리고(최대 2번), 없으면 끝내는 구조입니다. 두 사람 모두 도구는 필요 없습니다. 질문 없이 바로 설계도를 보내 주세요."
)
TURN_PROMPT = (
    "v2.1 변경 사항: (1) `/orders` 응답의 `total` 필드가 문자열에서 숫자로 바뀜 (2) 로그인 실패 메시지 문구 개선 "
    "(3) 주문 목록 기본 정렬이 최신순으로 바뀜. 이 세 가지로 릴리스 노트 초안을 만들어 주세요. 각 발언은 8줄 이내."
)


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


@pytest.fixture(scope="module")
def built(mado):
    from app.agents.catalog import server_options, skill_options
    from app.config import get_config, read_conf_file
    from app.graph_store import graphs_dir, load_graph
    from app.orchestration.runner import get_debate_runner
    from app.orchestration.workflow_builder import (
        CompositeDraft, SaveRefused, apply_reply, ask_workflow_builder, builder_context, compact_reply,
        correction_message, new_graph_id, request_messages, save_workflow, split_composite_reply,
    )

    def context():
        return builder_context(server_options(get_config(), guest=False), skill_options(), max_visits=3)

    async def go():
        ctx = context()
        draft = CompositeDraft()
        history = [{"role": "user", "content": REQUEST}]
        first_request = len(mado.requests)
        content = await ask_workflow_builder(request_messages(history, draft), ctx, llm=mado.engine.llm_caller)
        reply = split_composite_reply(content)
        draft, problems = apply_reply(draft, reply, ctx)
        first_problems = list(problems)
        corrected = False
        if problems:
            corrected = True
            retry = request_messages(history, draft) + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": correction_message(problems)},
            ]
            content = await ask_workflow_builder(retry, ctx, llm=mado.engine.llm_caller)
            draft, problems = apply_reply(draft, split_composite_reply(content), ctx)
        history.append({"role": "assistant", "content": compact_reply(content)})
        builder_requests = mado.requests[first_request:]

        conf_before = mado.conf_path.read_text(encoding="utf-8")
        # 실행 중인 대화가 있으면 새 에이전트 저장은 거절되어야 합니다 (파일을 건드리기 전에).
        refused = ""
        try:
            save_workflow(draft, context(), graph_id="refused-graph", running=["some-running-session"])
            refused = "저장됨"
        except SaveRefused as exc:
            refused = f"거절: {exc}"
        untouched = mado.conf_path.read_text(encoding="utf-8") == conf_before

        saved, save_error = None, ""
        try:
            saved = save_workflow(draft, context(), graph_id=new_graph_id(),
                                  running=get_debate_runner().running_sessions())
        except SaveRefused as exc:
            save_error = str(exc)
        raw_after = read_conf_file(mado.conf_path)

        # 같은 설계도를 한 번 더 저장하면 키가 겹칩니다 — 새 키를 받고 그래프 노드도 함께 바뀌어야 합니다.
        again, again_error = None, ""
        if saved is not None:
            try:
                again = save_workflow(draft, context(), graph_id=new_graph_id(), running=[])
            except SaveRefused as exc:
                again_error = str(exc)

        outcome, sid = None, None
        if saved is not None and not saved.graph_error:
            from app.database.models import SessionModel

            # 헤드리스 API 는 사람이 고른 그래프가 필요한 그래프 토론을 만들지 않습니다. 화면이 하는 대로
            # 대화를 만든 뒤 전략과 그래프를 고릅니다 (로스터에서 "그래프 토론" + 방금 저장한 그래프 선택).
            sid = await mado.new_session("workflow-builder", agents=["orchestrator", *saved.agent_keys], max_rounds=3)
            async with mado.session_factory()() as db:
                session = await db.get(SessionModel, sid)
                session.strategy = "graph_debate"
                session.graph_id = saved.spec.id
                await db.commit()
            outcome = await mado.turn(sid, TURN_PROMPT, timeout=2400)
        return {
            "draft": draft, "first_problems": first_problems, "problems": problems, "corrected": corrected,
            "builder_requests": builder_requests, "reply_text": reply.text, "refused": refused, "untouched": untouched,
            "saved": saved, "save_error": save_error, "raw_after": raw_after, "again": again, "again_error": again_error,
            "sid": sid, "outcome": outcome, "graphs_dir": graphs_dir(),
            "loaded": load_graph(saved.spec.id) if saved is not None and not saved.graph_error else None,
            "messages": await mado.messages(sid) if sid else [],
            "calls": await mado.tool_calls(sid) if sid else [],
        }

    result = mado.run(go(), timeout=3600)
    mado.dump("workflow-builder", result["builder_requests"], {"draft": json.loads(result["draft"].model_dump_json())})
    if result["outcome"] is not None:
        mado.dump("workflow-builder-turn", result["outcome"].requests)
    return result


@pytest.mark.adr(50)
def test_adr050_deepseek_produces_a_usable_design(mado, built):
    from app.orchestration.workflow_builder import CURRENT_DRAFT_MARKER

    draft = built["draft"]
    keys = sorted(a.key for a in draft.new_agents)
    mado.check("ADR-050", "도우미 답에서 새 에이전트 두 명을 읽어 냄 (요청한 키 그대로)",
               keys == ["compat_reviewer", "release_writer"], keys)
    mado.check("ADR-050", "그래프를 읽어 냄", draft.graph is not None, draft.graph_data)
    mado.check("ADR-050", "한 번의 자체 교정 안에 오류 없는 설계도가 됨", not draft.errors and not built["problems"],
               f"첫 답의 문제 {built['first_problems']} → 남은 문제 {built['problems']} · 오류 {draft.errors}")
    if draft.graph is not None:
        types = [n.type for n in draft.graph.nodes]
        mado.check("ADR-050", "요청한 구조가 들어 있음 — 두 전문가 노드와 판정 노드, 되돌리는 선",
                   set(draft.graph.agent_keys()) == {"release_writer", "compat_reviewer"} and "gate" in types, types)
        mado.check("ADR-050", "적응형 그래프와 같은 상한을 지킴 (작업 노드 12개·최악 40회)",
                   len([t for t in types if t in ("agent", "merge", "gate")]) <= 12 and draft.worst_case_calls <= 40,
                   f"노드 {types} · 최악 {draft.worst_case_calls}회")
    for agent in draft.new_agents:
        mado.check("ADR-050", f"새 에이전트 `{agent.key}` 에 이름·역할·업무 지시서가 채워짐",
                   bool(agent.name and agent.role and agent.system_prompt), agent.model_dump())
    system = built["builder_requests"][0].system() if built["builder_requests"] else ""
    mado.check("ADR-050", "도우미는 도구 없이 불림", built["builder_requests"] and not built["builder_requests"][0].tool_names,
               built["builder_requests"][0].tool_names if built["builder_requests"] else None)
    mado.note("ADR-050", f"자체 교정 {'1회' if built['corrected'] else '없음'} (첫 답의 문제: {built['first_problems'] or '없음'}) · "
                         f"요청 {len(built['builder_requests'])}건 · 경고 {draft.warnings} · 시스템 프롬프트 {len(system)}자 · "
                         f"도우미의 말: {built['reply_text'][:200]!r}")
    mado.settle()


@pytest.mark.adr(50)
def test_adr050_saving_checks_everything_before_writing(mado, built):
    mado.check("ADR-050", "실행 중인 대화가 있으면 새 에이전트 저장을 거절", built["refused"].startswith("거절"), built["refused"])
    mado.check("ADR-050", "거절된 저장은 conf.json 을 건드리지 않음", built["untouched"])
    saved = built["saved"]
    mado.check("ADR-050", "저장됨", saved is not None and not saved.graph_error,
               built["save_error"] or (saved.graph_error if saved else ""))
    if saved is None:
        mado.settle()
        return
    agents = {k: v for k, v in (built["raw_after"].get("agents") or {}).items() if not k.startswith("//")}
    mado.check("ADR-050", "conf.json 에 새 에이전트 두 명이 등록됨", all(key in agents for key in saved.agent_keys),
               sorted(agents))
    for key in saved.agent_keys:
        entry = agents.get(key) or {}
        mado.check("ADR-050", f"`{key}`: 모델·API 키를 적지 않아 llm 기본값을 따름",
                   "model" not in entry and "api_key" not in entry, sorted(entry))
    loaded = built["loaded"]
    mado.check("ADR-050", "그래프 파일이 `ai-<날짜시간>` 이름으로 저장되고 다시 읽힘",
               loaded is not None and saved.spec.id.startswith("ai-") and set(loaded.agent_keys()) == set(saved.agent_keys),
               f"{saved.spec.id} · {loaded.agent_keys() if loaded else None}")
    again = built["again"]
    if again is not None:
        mado.check("ADR-050", "같은 키로 다시 저장하면 새 키를 받고 그래프 노드도 함께 바뀜 (엉뚱한 에이전트가 말하지 않게)",
                   bool(again.renamed) and set(again.spec.agent_keys()) == set(again.agent_keys)
                   and not set(again.agent_keys) & set(saved.agent_keys),
                   f"바뀐 키 {again.renamed} · 그래프 {again.spec.agent_keys()} · 등록 {again.agent_keys}")
    else:
        mado.check("ADR-050", "같은 설계도의 두 번째 저장이 처리됨", False, built["again_error"])
    mado.settle()


@pytest.mark.adr(50)
def test_adr050_the_saved_workflow_runs_with_the_new_agents(mado, built):
    """저장한 그래프를 실제로 돌립니다. 새 에이전트가 System 2(DeepSeek) 전문가로 말하는지가 핵심입니다."""
    from app.agents.pool import get_agent_pool

    outcome = built["outcome"]
    saved = built["saved"]
    if saved is not None:
        agents = built["raw_after"].get("agents") or {}
        written = {key: (agents.get(key) or {}).get("decision_llm") for key in saved.agent_keys}
        mado.check("ADR-050", "conf.json 의 새 에이전트에 `decision_llm.enabled: false` 가 적힘 (전역 System 1 을 상속하지 않음)",
                   all(isinstance(v, dict) and v.get("enabled") is False for v in written.values()), written)
    mado.check("ADR-050", "저장한 그래프로 턴이 끝까지 돎", outcome is not None and outcome.error is None,
               outcome.error if outcome is not None else "턴을 돌리지 못함")
    if outcome is None:
        mado.settle()
        return
    speeches = [m for m in built["messages"] if m.sender_key in saved.agent_keys and _kind(m) == "speech"]
    mado.check("ADR-050", "새 에이전트 둘 다 그래프 노드에서 발언함",
               {m.sender_key for m in speeches} == set(saved.agent_keys), [(m.sender_key, m.graph_node_id) for m in speeches])
    system1 = [m.sender_key for m in speeches if "System 1 결정 모델 평가" in (m.content or "")]
    inherited = {key: get_agent_pool().get(key).decision_llm.enabled for key in saved.agent_keys if get_agent_pool().get(key)}
    mado.check(
        "ADR-050",
        "새 에이전트가 글을 쓰는 전문가(System 2)로 발언함 — 판정 점수표(System 1)가 아님",
        not system1,
        f"System 1 로 답한 발언 {system1} · 새 에이전트의 decision_llm.enabled {inherited} · "
        f"이 conf 의 전역 llm.decision_llm.enabled={mado.inherits_system1}",
    )
    gates = [(m.graph_node_id, m.graph_port) for m in built["messages"] if _kind(m) == "gate"]
    mado.note("ADR-050", f"턴 {outcome.seconds:.0f}초 · 요청 {len(outcome.requests)}건 · Jev {len(outcome.decisions)}회 · 판정 {gates} · "
                         f"새 에이전트 발언 머리: {[(m.sender_key, (m.content or '')[:70]) for m in speeches[:3]]}")

    # ADR-053: 번들 conf 의 오케스트레이터는 filesystem 을 가집니다. 2026-11-11 첫 실행에서는 이 시나리오의 계획 발언이
    # write_file·edit_file 로 파일을 세 번 고쳤습니다. 이제 계획 발언은 읽기만 합니다.
    plans = {m.id for m in built["messages"] if m.sender_key == "orchestrator" and _kind(m) == "plan"}
    plan_calls = [c for c in built["calls"] if c.message_id in plans]
    ran_writes = [(c.tool_name, c.rule) for c in plan_calls
                  if c.status == "success" and c.risk not in ("read", "state", "", None)]
    mado.check("ADR-053", "계획 발언에서 쓰기·실행·네트워크 호출이 실행되지 않음", not ran_writes,
               [(c.tool_name, c.status, c.risk, c.rule) for c in plan_calls])
    mado.note("ADR-053", f"계획 발언의 도구 호출 {len(plan_calls)}건: "
                         f"{[(c.tool_name, c.status, c.rule) for c in plan_calls][:12]}")
    mado.settle()
