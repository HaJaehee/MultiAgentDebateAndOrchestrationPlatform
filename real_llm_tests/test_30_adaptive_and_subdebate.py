"""ADR-034: 오케스트레이터가 그린 실행 그래프와 쟁점별 서브 토론을 실제 LLM 으로 돌립니다.

* `drawn` — 적응형 그래프(`adaptive_graph`). 그래프를 DeepSeek 가 그리고, 서브 토론을 열지도 DeepSeek 가 정합니다.
* `judged` — 순차 토론에 서브 토론을 켜고, 오케스트레이터의 판정만 System 1(Jev)로 돌립니다 (`decision_llm.enabled`).

서브 토론은 모델(또는 Jev)이 열기로 해야 열립니다. 열리지 않은 실행은 실패가 아니라 "이번에는 열지 않았다" 로
기록하고, 열렸을 때만 격리·합류 규칙을 확인합니다.
"""

import re

import pytest

from harness import git

GRAPH_AGENTS = ["orchestrator", "architect", "coder", "critic"]
GRAPH_PROMPT = (
    "사내 서비스의 공개 REST API 에 버전 관리 방식을 도입합니다. URL 경로(/v1/...)와 요청 헤더(Accept-Version) 중 "
    "무엇을 표준으로 할지 정하고, 폐기(deprecation) 공지 기간도 정해 주세요. 클라이언트는 사내 팀 12곳과 외부 파트너 3곳입니다.\n"
    "architect 는 설계안을, coder 는 게이트웨이 라우팅 구현 관점을, critic 은 운영·호환성 위험을 맡습니다. "
    "의견이 갈리면 숨기지 말고 쟁점으로 남기세요.\n"
    "파일을 만들거나 읽을 필요는 없고 도구도 쓰지 마세요. 각 발언은 12줄 이내로 쓰세요."
)
JUDGED_AGENTS = ["orchestrator", "architect", "critic"]
JUDGED_PROMPT = (
    "모바일 앱의 오프라인 동기화 충돌을 어떻게 풀지 정해 주세요. 후보는 '마지막 쓰기 우선(LWW)' 과 "
    "'필드 단위 병합(CRDT 류)' 입니다. architect 는 단순함과 출시 일정을, critic 은 데이터 유실 위험을 우선합니다. "
    "두 사람은 서로 다른 안을 지지하고, 쉽게 양보하지 마세요. 합의가 안 되면 쟁점으로 남기세요.\n"
    "파일을 만들거나 읽을 필요는 없고 도구도 쓰지 마세요. 각 발언은 12줄 이내로 쓰세요."
)


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


async def _use_system1_for_orchestrator(mado, sid: str) -> dict:
    """이 대화의 오케스트레이터만 판정을 System 1 로 돌리게 고정합니다 (다른 시험의 설정은 건드리지 않습니다)."""
    from sqlalchemy import select

    from app.agents.personas import freeze_personas
    from app.agents.pool import get_agent_pool
    from app.database.models import SessionAgentModel, SessionModel

    async with mado.session_factory()() as db:
        session = await db.get(SessionModel, sid)
        await freeze_personas(db, session, get_agent_pool())
        rows = (await db.execute(select(SessionAgentModel).where(SessionAgentModel.session_id == sid))).scalars().all()
        seen = {}
        for row in rows:
            if row.agent_key == "orchestrator" and isinstance(row.config_snapshot, dict):
                snapshot = dict(row.config_snapshot)
                decision = dict(snapshot.get("decision_llm") or {})
                decision["enabled"] = True
                snapshot["decision_llm"] = decision
                row.config_snapshot = snapshot
                seen = decision
        await db.commit()
        return seen


def _answers(call) -> dict:
    return (call.response or {}).get("answers", {}) if isinstance(call.response, dict) else {}


def _noul(answer) -> float:
    return float(answer.get("noul") or 0.0) if isinstance(answer, dict) else 0.0


def _top_conflict(call):
    """1단계(쟁점별 충돌) 호출에서 가장 높은 쟁점의 (질문 키, 확률)."""
    scored = {k: _noul(v) for k, v in _answers(call).items() if k.startswith("conflict_")}
    if not scored:
        return None, 0.0
    key = max(scored, key=lambda k: (scored[k], -int(k.split("_")[1])))
    return key, scored[key]


def _sub_debate_checks(mado, label: str, messages, outcome, issue_numbers_open_before=None) -> bool:
    """서브 토론이 열렸으면 ADR-034 §2 의 규칙을 확인합니다. 열렸는지를 돌려줍니다."""
    opens = [m for m in messages if _kind(m) == "subdebate_open"]
    if not opens:
        asked = [r for r in outcome.requests if "[서브 토론 판단]" in r.last_user()]
        jev = [d for d in outcome.decisions if "conflict_0" in (d.payload.get("questions") or {})]
        mado.note("ADR-034", f"{label}: 서브 토론이 열리지 않음 — 모델에게 물은 횟수 {len(asked)}, Jev 쟁점 판정 {len(jev)}회 · "
                             f"경계별 가장 높은 충돌 확률 {[_top_conflict(d) for d in jev]}")
        return False

    mado.check("ADR-034", f"{label}: 한 턴에 서브 토론은 많아야 2번", len(opens) <= 2, len(opens))
    boundaries = [m.round_number for m in opens]
    mado.check("ADR-034", f"{label}: 한 경계에 많아야 1번", len(boundaries) == len(set(boundaries)), boundaries)
    speeches = [m for m in messages if _kind(m) == "subdebate_speech"]
    conclusions = [m for m in messages if _kind(m) == "subdebate_conclusion"]
    for opened in opens:
        data = opened.turn_meta or {}
        pair = data.get("agents") or []
        mine = [m for m in speeches if m.round_number == opened.round_number]
        mado.check("ADR-034", f"{label}: 경계 {opened.round_number} — 두 전문가를 골라 열었음",
                   len(pair) == 2 and len(set(pair)) == 2 and "orchestrator" not in pair, pair)
        mado.check("ADR-034", f"{label}: 경계 {opened.round_number} — 발언은 그 두 사람만, 설정한 라운드(1) × 2 이내",
                   {m.sender_key for m in mine} <= set(pair) and len(mine) <= 2,
                   [(m.sender_key, m.msg_type) for m in mine])
        closing = [m for m in conclusions if m.round_number == opened.round_number]
        mado.check("ADR-034", f"{label}: 경계 {opened.round_number} — 오케스트레이터의 결론이 하나 있음",
                   len(closing) == 1 and closing[0].sender_key == "orchestrator", [m.sender_key for m in closing])
        issue_no = data.get("issue_no")
        if closing and closing[0].msg_type != "error":
            head = closing[0].content.splitlines()[0]
            expected = f"[서브 토론 결론 · I-{issue_no} · " if issue_no else "[서브 토론 결론 · "
            mado.check("ADR-051", f"{label}: 결론 머리줄에 쟁점 번호가 실림 (`{expected}…`)", head.startswith(expected), head[:160])
        mado.note("ADR-034", f"{label}: 경계 {opened.round_number} 서브 토론 — 쟁점 I-{issue_no} "
                             f"“{str(data.get('issue'))[:80]}” · {pair} · 발언 {len(mine)}건")

    # 격리: 서브 토론 안의 발언은 그 서브 토론 밖의 어떤 요청에도 실리지 않습니다.
    inside_markers = ("[서브 토론 ", "[서브 토론 결론 작성]")
    probes = []
    for message in speeches:
        body = (message.content or "").strip()
        if len(body) > 200:
            probes.append((message.id, body[len(body) // 2: len(body) // 2 + 70]))
    leaked = []
    for request in outcome.requests:
        last = request.last_user()
        if any(marker in last for marker in inside_markers):
            continue
        text = request.text()
        for message_id, probe in probes:
            if probe in text:
                leaked.append((request.index, request.agent, message_id[:8]))
    mado.check("ADR-034", f"{label}: 서브 토론 안의 발언이 본 토론·장부·합성 요청에 새지 않음", not leaked, leaked[:6])

    # 합류: 결론은 그 뒤의 요청에 실립니다.
    joined = 0
    for conclusion in conclusions:
        if conclusion.msg_type == "error":
            continue
        body = (conclusion.content or "").strip()
        probe = body[len(body) // 2: len(body) // 2 + 60]
        joined += any(probe in r.text() for r in outcome.requests if r.at > 0 and "[서브 토론 결론 작성]" not in r.last_user())
    mado.check("ADR-034", f"{label}: 결론은 뒤따르는 요청(다음 발언·장부·합성)에 실림", joined >= 1, joined)

    inside = [r for r in outcome.requests if "[서브 토론 " in r.last_user() and r.agent != "orchestrator"]
    with_peers = [r.index for r in inside if any(name.startswith("peers__") for name in r.tool_names)]
    mado.check("ADR-034", f"{label}: 서브 토론 발언에는 동료 자문 도구를 주지 않음", not with_peers, with_peers)
    return True


# --------------------------------------------------------------------------- 적응형 그래프


@pytest.fixture(scope="module")
def drawn(mado):
    async def go():
        sid = await mado.new_session(
            "adaptive-graph", agents=GRAPH_AGENTS, strategy="adaptive_graph", max_rounds=3,
            converge_early=False, sub_debates=True, sub_debate_rounds=1,
        )
        outcome = await mado.turn(sid, GRAPH_PROMPT, timeout=2400)
        return sid, outcome

    sid, outcome = mado.run(go(), timeout=2700)
    mado.dump("adaptive-graph", outcome.requests, {"events": [e.get("type") for e in outcome.events]})
    if outcome.error is not None:
        pytest.fail(f"턴이 실패했습니다: {type(outcome.error).__name__}: {outcome.error}")
    return {
        "sid": sid, "outcome": outcome, "messages": mado.run(mado.messages(sid)),
        "turns": mado.run(mado.turns(sid)), "session": mado.run(mado.session(sid)),
        "revisions": mado.run(mado.revisions(sid)), "workspace": mado.workspaces / "adaptive-graph",
    }


@pytest.mark.adr(34)
def test_adr034_orchestrator_draws_a_valid_graph_once(mado, drawn):
    from app.orchestration import graph_draft
    from app.orchestration.graph import parse_graph

    messages, outcome = drawn["messages"], drawn["outcome"]
    drafts = [m for m in messages if _kind(m) == "graph_draft"]
    mado.check("ADR-034", "턴이 completed 로 끝남", drawn["turns"][-1].status == "completed", drawn["turns"][-1].error)
    mado.check("ADR-034", "그래프 기록(graph_draft)이 턴에 한 건", len(drafts) == 1, len(drafts))
    if not drafts:
        mado.settle()
        return
    draft = drafts[0]
    meta = draft.turn_meta or {}
    fallback = bool(meta.get("fallback"))
    mado.check("ADR-034", "DeepSeek 가 그린 그래프가 검사를 통과해 그대로 쓰임 (물러서지 않음)", not fallback, draft.content[:400])
    spec = parse_graph(meta["spec"])
    specialists = {"architect", "coder", "critic"}
    mado.check("ADR-034", "그래프의 전문가는 로스터에서 켠 사람뿐", set(spec.agent_keys()) <= specialists, spec.agent_keys())
    working = [n for n in spec.nodes if n.type in ("agent", "merge", "gate")]
    report = graph_draft.draft_report(spec, sorted(specialists), 3)
    mado.check("ADR-034", "작업 노드 12개 이하·최악 호출 40회 이하", len(working) <= 12 and report.max_calls <= 40,
               f"노드 {len(working)} · 최악 호출 {report.max_calls} · 오류 {report.errors}")
    mado.check("ADR-034", "세션의 graph_snapshot 이 그린 그래프로 고정됨",
               drawn["session"].graph_snapshot == meta["spec"], type(drawn["session"].graph_snapshot))

    drafting = [r for r in outcome.requests if graph_draft.DRAFT_PROMPT_MARKER in r.last_user()]
    mado.check("ADR-034", "그리기 요청은 턴 시작에 1~2번뿐 (틀리면 한 번 더)", 1 <= len(drafting) <= 2, len(drafting))
    plan_index = next((i for i, m in enumerate(messages) if _kind(m) == "plan"), -1)
    draft_index = messages.index(draft)
    first_speech = next((i for i, m in enumerate(messages) if m.graph_node_id), len(messages))
    mado.check("ADR-034", "계획 → 그래프 → 발언 순서", plan_index < draft_index < first_speech,
               (plan_index, draft_index, first_speech))

    node_ids = {n.id for n in spec.nodes}
    ran = [m for m in messages if m.graph_node_id]
    mado.check("ADR-034", "돈 노드는 모두 그린 그래프의 노드", {m.graph_node_id for m in ran} <= node_ids,
               sorted({m.graph_node_id for m in ran} - node_ids))
    visits: dict = {}
    for message in ran:
        if _kind(message) in ("speech", "merge", "gate"):
            visits[message.graph_node_id] = visits.get(message.graph_node_id, 0) + 1
    mado.check("ADR-034", "어느 노드도 max_rounds(3)번을 넘겨 돌지 않음", max(visits.values(), default=0) <= 3, visits)
    mado.check("ADR-034", "실제 호출 수가 그래프가 약속한 최악 호출 수 이내", sum(visits.values()) <= report.max_calls,
               f"{sum(visits.values())} / {report.max_calls}")
    gates = [m for m in ran if _kind(m) == "gate"]
    shape = " → ".join(f"{n.type}:{n.agent or n.id}" for n in working)
    mado.note("ADR-034", f"그린 그래프 “{spec.name}” [{shape}] · 선 {len(spec.edges)}개 · 최악 {report.max_calls}회 · "
                         f"실제 방문 {visits} · 판정 {[(m.graph_node_id, m.graph_port) for m in gates]} · "
                         f"그리기 시도 {len(drafting)}회 · 턴 {outcome.seconds:.0f}초 · 요청 {len(outcome.requests)}건")
    mado.settle()


@pytest.mark.adr(43)
def test_adr043_graph_steps_are_snapshotted_and_recorded(mado, drawn):
    turn = drawn["turns"][-1]
    refs = git(drawn["workspace"], "for-each-ref", "--format=%(refname)", f"refs/mado/turns/{turn.id}/")
    names = sorted(ref.rsplit("/", 1)[-1] for ref in refs.splitlines() if ref)
    steps = sorted({m.round_number for m in drawn["messages"] if m.graph_node_id})
    for step in steps:
        mado.check("ADR-043", f"그래프 단계 경계 `step-{step}` 스냅샷이 있음", f"step-{step}" in names, names)
    mado.check("ADR-043", "토론 끝 경계 `debate-end` 가 있음", "debate-end" in names, names)
    kinds = [(r.kind, r.round_number) for r in drawn["revisions"]]
    mado.check("ADR-051", "그래프 단계마다의 장부 개정은 `step`, 마지막은 `final`",
               kinds and kinds[-1][0] == "final" and all(k in ("step", "subdebate", "final") for k, _ in kinds), kinds)
    mado.settle()


@pytest.mark.adr(51)
def test_adr051_no_open_issue_is_lost_to_the_ledger_length_cap(mado, drawn):
    """긴 토론에서 모델이 쓴 장부가 글자 상한을 넘어도 미해결 쟁점은 사라지지 않아야 합니다.

    처음 구현은 뒤(미해결 쟁점·담당)부터 잘라, 잘린 쟁점이 번호 이력에 풀리지 않았는데도 "닫힘" 으로 남았습니다.
    이 시나리오(전문가 3명, 그래프 여러 단계)는 장부가 길어지기 쉬워, 상한에 맞추는 일이 실제로 일어납니다.
    상한을 넘은 장부가 한 건도 없었다면 이 시험은 아무것도 확인하지 못한 것이라 그렇게 적습니다.
    """
    from harness import ledger_clip_losses, ledger_sizes

    sizes = ledger_sizes(drawn["outcome"].requests, drawn["revisions"])
    fitted = [s for s in sizes if s["stored"] < s["written"] - 40]
    mado.note("ADR-051", f"모델이 쓴 장부 → 저장된 장부 (글자): {[(s['kind'], s['round'], s['written'], s['stored']) for s in sizes]} · "
                         f"서버가 상한에 맞춘 개정 {len(fitted)}건")
    losses = ledger_clip_losses(drawn["outcome"].requests, drawn["revisions"])
    mismatch = [loss for loss in losses if "mismatch" in loss]
    if mismatch:
        mado.note("ADR-051", mismatch[0]["mismatch"])
        mado.settle()
        return
    lost_issues = sum(len(loss["lost_open_issues"]) for loss in losses)
    mado.check("ADR-051", "모델이 미해결로 적은 쟁점이 글자 상한에 잘려 장부에서 사라지지 않음", lost_issues == 0,
               [(f"seq {l['seq']} {l['kind']} r{l['round']}", f"{l['written_chars']}→{l['stored_chars']}자", l["lost"],
                 [item[:60] for item in l["lost_open_issues"][:2]]) for l in losses])
    dropped = [(l["seq"], l["sections_dropped"]) for l in losses if l["sections_dropped"]]
    mado.check("ADR-051", "장부의 절이 상한에 잘려 통째로 사라지지 않음", not dropped, dropped)
    trimmed = {}
    for loss in losses:
        for name, count in loss["lost"].items():
            trimmed[name] = trimmed.get(name, 0) + count
    mado.note("ADR-051", f"상한에 맞추며 뺀 항목(칸별 합계): {trimmed or '없음'} · 사라진 미해결 쟁점 합계 {lost_issues}건"
                         + ("" if fitted else " — 이번 실행에서는 상한을 넘은 장부가 없어 줄이는 동작을 확인하지 못했습니다"))
    mado.settle()


@pytest.mark.adr(34)
def test_adr034_sub_debate_in_a_graph_turn(mado, drawn):
    opened = _sub_debate_checks(mado, "적응형 그래프", drawn["messages"], drawn["outcome"])
    if opened:
        kinds = [r.kind for r in drawn["revisions"]]
        mado.check("ADR-051", "서브 토론 결론 뒤에 `subdebate` 개정이 남음", "subdebate" in kinds, kinds)
    mado.settle()


# --------------------------------------------------------------------------- System 1 판정


@pytest.fixture(scope="module")
def judged(mado):
    async def go():
        sid = await mado.new_session(
            "subdebate-jev", agents=JUDGED_AGENTS, max_rounds=3, converge_early=False,
            sub_debates=True, sub_debate_rounds=1,
        )
        decision = await _use_system1_for_orchestrator(mado, sid)
        outcome = await mado.turn(sid, JUDGED_PROMPT, timeout=2400)
        return sid, outcome, decision

    sid, outcome, decision = mado.run(go(), timeout=2700)
    mado.dump("subdebate-jev", outcome.requests, {
        "decisions": [{"payload": d.payload, "response": d.response, "status": d.status} for d in outcome.decisions],
    })
    if outcome.error is not None:
        pytest.fail(f"턴이 실패했습니다: {type(outcome.error).__name__}: {outcome.error}")
    return {
        "sid": sid, "outcome": outcome, "decision": decision, "messages": mado.run(mado.messages(sid)),
        "turns": mado.run(mado.turns(sid)), "revisions": mado.run(mado.revisions(sid)),
    }


@pytest.mark.adr(34)
def test_adr034_jev_decides_whether_to_open_a_sub_debate(mado, judged):
    outcome, messages = judged["outcome"], judged["messages"]
    mado.check("ADR-034", "오케스트레이터의 System 1 판정이 이 대화에서 켜짐", judged["decision"].get("enabled") is True,
               judged["decision"])
    asked_llm = [r for r in outcome.requests if "[서브 토론 판단]" in r.last_user()]
    mado.check("ADR-034", "판정이 System 1 로 가므로 DeepSeek 에게는 서브 토론을 묻지 않음", not asked_llm, len(asked_llm))
    threshold = float(judged["decision"].get("threshold") or 0.5)
    # 판정은 두 단계입니다 (ADR-034, 2026-11-11): 1단계는 쟁점마다 "두 전문가가 서로 다른 안을 고수하는가",
    # 그 확률이 기준을 넘은 경계에서만 2단계로 고른 쟁점을 짚어 "본 토론에서 풀리겠는가" 와 후보별 "맞선 안의 당사자인가" 를 묻습니다.
    firsts = [d for d in outcome.decisions if "conflict_0" in (d.payload.get("questions") or {})]
    seconds = [d for d in outcome.decisions if "settles" in (d.payload.get("questions") or {})]
    # 묻는 것은 라운드를 닫은 장부에 미해결 쟁점이 남았을 때뿐입니다 (쟁점이 없으면 호출 비용이 들지 않아야 합니다).
    boundaries = [r.round_number for r in judged["revisions"] if r.kind == "round" and (r.open_issues or [])]
    mado.check("ADR-034", "미해결 쟁점이 남은 라운드 경계마다 Jev 에게 쟁점 판정을 한 번씩만 물음",
               len(firsts) == len(boundaries) <= 2, f"1단계 호출 {len(firsts)}회 · 쟁점이 남은 경계 {boundaries}")
    escalated = [d for d in firsts if _top_conflict(d)[1] >= threshold]
    mado.check("ADR-034", "2단계(풀림·당사자)는 충돌 확률이 기준을 넘은 경계에서만 물음",
               len(seconds) == len(escalated), f"2단계 {len(seconds)}회 · 기준을 넘은 1단계 {len(escalated)}회")
    for call in [*firsts, *seconds]:
        questions = call.payload.get("questions") or {}
        answers = _answers(call)
        mado.check("ADR-034", "Jev 가 200 으로 답하고 보낸 질문 전부에 답함",
                   call.status == 200 and set(answers) == set(questions), f"{call.status} {sorted(answers)} vs {sorted(questions)}")
    for first in firsts:
        questions = first.payload.get("questions") or {}
        mado.check("ADR-051", "쟁점별 충돌 질문마다 번호 붙은 쟁점을 짚음 (`쟁점 [I-n] '…'`)",
                   all(re.search(r"쟁점 \[I-\d+\] '", str(q.get("instructions"))) for q in questions.values()),
                   [str(q.get("instructions"))[:60] for q in list(questions.values())[:3]])
        per_issue = {re.search(r"\[(I-\d+)\]", str(questions[k].get("instructions"))).group(1): round(_noul(v), 2)
                     for k, v in _answers(first).items() if k in questions and re.search(r"\[(I-\d+)\]", str(questions[k].get("instructions")))}
        mado.note("ADR-034", f"Jev 1단계 {first.seconds:.1f}초: 쟁점별 충돌 {per_issue}")
    for second in seconds:
        questions = second.payload.get("questions") or {}
        parties = sorted(q for q in questions if q.startswith("party_"))
        mado.check("ADR-034", "2단계는 후보 전문가마다 당사자 질문이 감", parties == ["party_architect", "party_critic"], parties)
        tags = {m.group(1) for q in questions.values() for m in [re.search(r"\[(I-\d+)\]", str(q.get("instructions")))] if m}
        mado.check("ADR-034", "2단계 질문은 모두 같은 쟁점 하나를 짚음", len(tags) == 1, sorted(tags))
        answers = _answers(second)
        mado.note("ADR-034", f"Jev 2단계 {second.seconds:.1f}초: 쟁점 {sorted(tags)} · 풀림 {_noul(answers.get('settles')):.2f} · "
                             f"당사자 { {k: round(_noul(v), 2) for k, v in answers.items() if k.startswith('party_')} }")
    opened = _sub_debate_checks(mado, "System 1 판정", messages, outcome)
    if opened:
        note = next(m for m in messages if _kind(m) == "subdebate_open")
        mado.check("ADR-034", "연 이유에 System 1 판정 확률이 적힘", "System 1(JEV/" in note.content, note.content[-300:])
        data = note.turn_meta or {}
        mado.check("ADR-051", "Jev 가 고른 쟁점에서 번호를 떼어 따로 저장 (issue 에는 태그 없음, issue_no 는 정수)",
                   isinstance(data.get("issue_no"), int) and not str(data.get("issue", "")).startswith("[I-"), data)
    usage = judged["turns"][-1].usage or {}
    mado.note("ADR-034", f"턴 {outcome.seconds:.0f}초 · 요청 {len(outcome.requests)}건 · Jev {len(outcome.decisions)}회 · 사용량 {usage}")
    mado.settle()


@pytest.mark.adr(34)
def test_adr034_jev_gate_answers_yes_and_no(mado):
    """그래프 판정 노드의 System 1 경로: 뻔한 예·아니오를 기준(0.5) 양쪽으로 가르는지."""
    from app.agents.pool import get_agent_pool

    orchestrator = get_agent_pool().get("orchestrator")
    decision = orchestrator.decision_llm.model_copy(update={"enabled": True})
    judge = orchestrator.model_copy(update={"decision_llm": decision})
    caller = mado.engine.llm_caller.decision_caller
    state = (
        "[목표]\n로그인 API 에 속도 제한을 붙인다.\n\n[지금까지의 토론]\n"
        "Coder: 구현을 마쳤고 단위 테스트 14개가 모두 통과했습니다.\nCritic: 지적한 세 가지가 모두 반영됐습니다. 남은 이견이 없습니다."
    )

    async def ask():
        yes = await caller.evaluate_gate(judge, state, "남은 이견 없이 구현이 끝났습니까?")
        no = await caller.evaluate_gate(judge, state, "아직 통과하지 못한 테스트가 남아 있습니까?")
        return yes, no

    yes, no = mado.run(ask(), timeout=90)
    mado.check("ADR-034", "끝났다는 질문에 yes", yes[0] == "yes", yes)
    mado.check("ADR-034", "실패가 남았냐는 질문에 no", no[0] == "no", no)
    mado.note("ADR-034", f"판정 사유: {yes[1]} / {no[1]}")
    mado.settle()
