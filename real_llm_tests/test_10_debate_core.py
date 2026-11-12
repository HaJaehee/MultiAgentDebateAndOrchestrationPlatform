"""순차 토론 한 턴을 실제 LLM 으로 돌려, 토론 한 번에 걸쳐 있는 결정들을 함께 확인합니다.

* ADR-031 스트림 조립이 루프 밖에서 돎 · ADR-041 §4 LLM 을 기다리는 동안 DB 연결을 쥐지 않음
* ADR-043 라운드 경계 스냅샷 · ADR-045 Claude 가 아닌 연결의 요청은 그대로, 앞부분은 라운드가 바뀌어도 같음
* ADR-048 사용량 집계 · ADR-051 쟁점 번호와 장부 개정 이력 · ADR-052 발언 번호와 인용

턴은 모듈에서 한 번만 돌리고(`debate`), 시험마다 그 기록을 다른 각도에서 봅니다.
"""

import json
import re

import pytest

from harness import git

AGENTS = ["orchestrator", "architect", "critic"]
PROMPT = (
    "사내 위키 검색 API 에 읽기 캐시를 붙이려 합니다. 캐시 무효화를 TTL 로 할지, 쓰기 시점 무효화로 할지 정해 주세요.\n"
    "조건: 문서 수정은 하루 수십 건, 검색은 초당 수백 건, 수정 후 5초 안에 검색 결과에 반영되어야 합니다.\n"
    "캐시 저장소(프로세스 내 dict / Redis)는 아직 정하지 않았고, 이번 턴에서 결론이 안 나면 미해결 쟁점으로 남겨도 됩니다.\n"
    "파일을 만들거나 읽을 필요는 없습니다. 각 발언은 15줄 이내로 쓰세요."
)

SPEECH_KINDS = ("plan", "speech", "synthesis", "merge", "subdebate_speech", "subdebate_conclusion")

pytestmark = pytest.mark.acceptance


@pytest.fixture(scope="module")
def debate(mado):
    async def go():
        sid = await mado.new_session("core-debate", agents=AGENTS, max_rounds=2, converge_early=False)
        outcome = await mado.turn(sid, PROMPT)
        return sid, outcome

    sid, outcome = mado.run(go(), timeout=1800)
    mado.dump("core-debate", outcome.requests, {"events": [e.get("type") for e in outcome.events]})
    if outcome.error is not None:
        pytest.fail(f"턴이 실패했습니다: {type(outcome.error).__name__}: {outcome.error}")
    return {
        "sid": sid, "outcome": outcome,
        "messages": mado.run(mado.messages(sid)),
        "turns": mado.run(mado.turns(sid)),
        "revisions": mado.run(mado.revisions(sid)),
        "session": mado.run(mado.session(sid)),
        "workspace": mado.workspaces / "core-debate",
    }


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


def test_turn_completes_with_both_rounds(mado, debate):
    turn = debate["turns"][-1]
    speeches = [m for m in debate["messages"] if _kind(m) == "speech"]
    mado.check("core", "턴이 completed 로 끝남", turn.status == "completed", f"{turn.status} {turn.error}")
    mado.check("core", "전문가 2명 × 2라운드 = 발언 4건",
               len(speeches) == 4 and {m.round_number for m in speeches} == {1, 2},
               [(m.sender_key, m.round_number, m.msg_type) for m in speeches])
    mado.check("core", "오류로 끝난 발언 없음", not [m for m in debate["messages"] if m.msg_type == "error"],
               [(m.sender_key, m.content[:120]) for m in debate["messages"] if m.msg_type == "error"])
    mado.check("core", "합성 발언이 있음", any(_kind(m) == "synthesis" for m in debate["messages"]))
    outcome = debate["outcome"]
    mado.note("core", f"턴 {outcome.seconds:.0f}초 · LLM 요청 {len(outcome.requests)}건 · 스트림 조각 {outcome.stream_chunks}개 · "
                      f"Jev 호출 {len(outcome.decisions)}건")
    mado.settle()


@pytest.mark.adr(31)
def test_adr031_every_stream_is_assembled_off_the_loop(mado, debate):
    outcome = debate["outcome"]
    streamed = [r for r in outcome.requests if r.stream and not r.error]
    mado.check("ADR-031", "스트리밍 요청마다 조립 기록이 있음", len(outcome.assemblies) >= len(streamed) > 0,
               f"조립 {len(outcome.assemblies)} / 스트림 요청 {len(streamed)}")
    on_main = [a for a in outcome.assemblies if a.on_main_thread]
    mado.check("ADR-031", "조립이 한 번도 이벤트 루프 스레드에서 돌지 않음", not on_main, on_main[:2])
    mado.check("ADR-031", "조립된 응답에 사용량이 실려 있음 (루프로 돌아와 기록)",
               all(a.usage.get("total_tokens") for a in outcome.assemblies),
               [a.usage for a in outcome.assemblies if not a.usage.get("total_tokens")][:2])
    lag = outcome.lag
    mado.check("ADR-031", "턴 동안 이벤트 루프가 1초 넘게 붙잡힌 적 없음", lag.worst < 1.0, f"최악 {lag.worst * 1000:.0f}ms")
    mado.note("ADR-031", f"루프 지연 최악 {lag.worst * 1000:.0f}ms · 250ms 초과 {lag.over(0.25)}회 / 표본 {len(lag.samples)}개 · "
                         f"조립 최장 {max(a.seconds for a in outcome.assemblies) * 1000:.0f}ms")
    mado.settle()


@pytest.mark.adr(41)
def test_adr041_no_db_connection_is_held_while_waiting_for_the_llm(mado, debate):
    requests = debate["outcome"].requests
    measured = [r for r in requests if r.db_checked_out is not None]
    if not measured:
        pytest.skip("이 DB 풀은 빌려 간 연결 수를 알려 주지 않습니다")
    holding = [(r.index, r.agent, r.db_checked_out) for r in measured if r.db_checked_out]
    mado.check("ADR-041", "LLM 요청을 보내는 순간 풀에서 빌려 간 DB 연결이 0", not holding,
               f"{len(holding)}/{len(measured)}건이 연결을 쥔 채 요청: {holding[:8]}")
    mado.settle()


@pytest.mark.adr(43)
def test_adr043_round_boundaries_are_snapshotted(mado, debate):
    turn = debate["turns"][-1]
    refs = git(debate["workspace"], "for-each-ref", "--format=%(refname)", f"refs/mado/turns/{turn.id}/")
    names = sorted(ref.rsplit("/", 1)[-1] for ref in refs.splitlines() if ref)
    for expected in ("start", "round-1", "round-2", "debate-end", "end"):
        mado.check("ADR-043", f"경계 스냅샷 `{expected}` 이 턴의 ref 로 남음", expected in names, names)
    head = git(debate["workspace"], "log", "--oneline", "-5")
    mado.check("ADR-043", "스냅샷이 작업 공간의 일반 히스토리(HEAD)에 섞이지 않음",
               "mado" not in head.lower() and "snapshot" not in head.lower(), head or "(커밋 없음)")
    checkpoint = turn.checkpoint or {}
    mado.check("ADR-043", "턴 기록에 시작·끝 체크포인트와 변경 파일 수가 있음",
               bool(checkpoint.get("start")) and bool(checkpoint.get("end")) and "changed" in checkpoint, checkpoint)
    mado.settle()


@pytest.mark.adr(45)
def test_adr045_non_claude_requests_are_untouched_and_prefix_is_stable(mado, debate):
    from app.agents.llm_prompt_cache import cache_mark
    from app.agents.pool import get_agent_pool

    outcome = debate["outcome"]
    marked = [r.index for r in outcome.requests if "cache_control" in json.dumps([r.messages, r.tools])]
    blocks = [r.index for r in outcome.requests if any(isinstance(m.get("content"), list) for m in r.messages)]
    claude = [key for key in AGENTS if cache_mark(get_agent_pool().get(key)) is not None]
    if claude:
        # Claude 연결에서는 마커가 붙는 것이 정상입니다 (해당 검증은 `tests/test_prompt_cache.py`에서 수행합니다).
        mado.note("ADR-045", f"Claude 연결({claude})이므로 마커가 붙습니다 — 마커가 붙은 요청 {len(marked)}건")
    else:
        mado.check("ADR-045", f"{mado.s2} 요청에는 cache_control 마커가 없음", not marked, marked)
        mado.check("ADR-045", f"{mado.s2} 요청의 메시지는 블록이 아니라 일반 텍스트임", not blocks, blocks)

    for agent in ("architect", "critic"):
        own = [r for r in outcome.requests_by(agent) if "[결정 장부 갱신]" not in r.last_user()]
        firsts = _first_request_per_round(own)
        if len(firsts) < 2:
            mado.check("ADR-045", f"{agent}: 두 라운드의 발언 요청을 찾음", False, [r.index for r in own])
            continue
        one, two = (json.dumps(r.messages, ensure_ascii=False) for r in firsts[:2])
        shared = _common_prefix(one, two)
        system_len = len(json.dumps(firsts[0].messages[:1], ensure_ascii=False))
        goal_end = one.find(PROMPT.splitlines()[-1][:20])
        mado.check("ADR-045", f"{agent}: 라운드가 바뀌어도 시스템 프롬프트와 목표까지 요청 앞부분이 글자 그대로 같음",
                   shared >= system_len and (goal_end < 0 or shared > goal_end),
                   f"공통 앞부분 {shared}자 / 1라운드 {len(one)}자 · 시스템 {system_len}자 · 목표 끝 {goal_end}")
        cached = [r.usage.get("prompt_tokens_details", {}).get("cached_tokens") or 0 for r in firsts[:2]]
        prompts = [r.usage.get("prompt_tokens") or 0 for r in firsts[:2]]
        mado.note("ADR-045", f"{agent}: 공통 앞부분 {shared}자 (1R 요청 {len(one)}자, 2R 요청 {len(two)}자) · "
                             f"{mado.s2}가 보고한 캐시 적중 1R {cached[0]}/{prompts[0]} · 2R {cached[1]}/{prompts[1]} 토큰")
    turn_usage = debate["turns"][-1].usage or {}
    mado.check("ADR-045", "사용량 기록에 cache_write_tokens 칸이 있음", "cache_write_tokens" in turn_usage, turn_usage)
    mado.settle()


@pytest.mark.adr(48)
def test_adr048_usage_report_adds_up_to_what_the_provider_reported(mado, debate):
    from app.agents.llm_usage import Usage
    from app.orchestration.usage_report import OTHER_KEY, load_usage_report, token_parts

    async def load():
        async with mado.session_factory()() as db:
            return await load_usage_report(db, debate["sid"])

    report = mado.run(load())
    turn = debate["turns"][-1]
    recorded = Usage.of(turn.usage)
    outcome = debate["outcome"]

    provider_prompt = sum(r.usage.get("prompt_tokens") or 0 for r in outcome.requests)
    provider_completion = sum(r.usage.get("completion_tokens") or 0 for r in outcome.requests)
    jev_in = sum((d.response or {}).get("usage", {}).get("input_tokens", 0) for d in outcome.decisions
                 if isinstance(d.response, dict))
    jev_out = sum((d.response or {}).get("usage", {}).get("output_tokens", 0) for d in outcome.decisions
                  if isinstance(d.response, dict))
    mado.check("ADR-048", "턴 기록의 입력 토큰 = 제공자가 요청마다 보고한 값의 합",
               recorded.prompt_tokens == provider_prompt + jev_in,
               f"턴 {recorded.prompt_tokens} · {mado.s2} {provider_prompt} + 결정 LLM {jev_in}")
    mado.check("ADR-048", "턴 기록의 출력 토큰 = 제공자가 요청마다 보고한 값의 합",
               recorded.completion_tokens == provider_completion + jev_out,
               f"턴 {recorded.completion_tokens} · {mado.s2} {provider_completion} + 결정 LLM {jev_out}")
    mado.check("ADR-048", "턴 기록의 호출 수 = 실제 보낸 요청 수",
               recorded.calls == len(outcome.requests) + len(outcome.decisions),
               f"턴 {recorded.calls} · 요청 {len(outcome.requests)} + Jev {len(outcome.decisions)}")

    mado.check("ADR-048", "보고서 합계 = 턴 기록 합계", report.total.total_tokens == recorded.total_tokens,
               f"보고서 {report.total.total_tokens} · 턴 {recorded.total_tokens}")
    for row in report.by_agent + report.by_step:
        parts = token_parts(row.usage)
        mado.check("ADR-048", f"`{row.label}` 행: 네 조각의 합 = 그 행의 전체 토큰",
                   sum(parts) == row.usage.total_tokens and min(parts) >= 0, f"{parts} vs {row.usage.total_tokens}")
    agent_sum = sum(row.usage.total_tokens for row in report.by_agent)
    step_sum = sum(row.usage.total_tokens for row in report.by_step)
    mado.check("ADR-048", "에이전트별 합 = 라운드별 합 = 전체", agent_sum == step_sum == report.total.total_tokens,
               f"에이전트 {agent_sum} · 라운드 {step_sum} · 전체 {report.total.total_tokens}")
    named = [row for row in report.by_agent if row.key != OTHER_KEY]
    mado.check("ADR-048", "에이전트 행의 경과 시간 ≥ LLM 시간",
               all(row.elapsed + 0.01 >= row.usage.llm_seconds for row in named),
               [(row.label, round(row.elapsed, 1), round(row.usage.llm_seconds, 1)) for row in named])
    labels = [row.label for row in report.by_step]
    mado.check("ADR-048", "한 턴짜리 대화는 라운드별로 보임 (계획 → 라운드 1 → 라운드 2 → 결론 합성 → 보조 호출)",
               report.step_word == "라운드별" and labels[:1] == ["계획"] and "라운드 2" in labels and "결론 합성" in labels,
               labels)
    other = next((row for row in report.by_agent if row.key == OTHER_KEY), None)
    mado.note("ADR-048", "행: " + " | ".join(
        f"{row.label} {row.usage.total_tokens}tok {row.elapsed:.0f}s(LLM {row.usage.llm_seconds:.0f}s)" for row in report.by_agent
    ) + f" · 추론 토큰 {report.total.reasoning_tokens} · 캐시 적중 {report.total.cached_tokens}"
      + (f" · 보조 호출 {other.usage.calls}건" if other else ""))
    mado.settle()


@pytest.mark.adr(51)
def test_adr051_issue_ids_and_ledger_revisions(mado, debate):
    from app.orchestration import context_memory as memory
    from app.orchestration.ledger_history import ISSUE_TAG

    ledger = debate["session"].decision_ledger or ""
    revisions = debate["revisions"]
    for title in memory.LEDGER_SECTIONS:
        mado.check("ADR-051", f"장부에 `## {title}` 절이 있음", f"## {title}" in ledger, ledger[:300])
    open_items = memory.ledger_items(ledger, "미해결 쟁점") or []
    real_items = [item for item in open_items if item.strip() not in ("없음", "")]
    untagged = [item for item in real_items if not ISSUE_TAG.match(item.strip())]
    mado.check("ADR-051", "미해결 쟁점 항목마다 서버가 붙인 [I-n] 번호가 있음", not untagged, untagged)
    numbers = [int(ISSUE_TAG.match(item.strip()).group(1)) for item in real_items if ISSUE_TAG.match(item.strip())]
    mado.check("ADR-051", "한 장부 안에서 쟁점 번호가 겹치지 않음", len(numbers) == len(set(numbers)), numbers)
    for title in memory.LEDGER_SECTIONS:
        if title == "미해결 쟁점":
            continue
        leaked = [item for item in (memory.ledger_items(ledger, title) or []) if ISSUE_TAG.match(item.strip())]
        mado.check("ADR-051", f"`{title}` 절의 항목 머리에는 쟁점 번호가 남지 않음", not leaked, leaked)

    kinds = [(r.kind, r.round_number) for r in revisions]
    # 마지막 라운드는 따로 닫지 않고 합성 뒤의 `final` 개정이 그 라운드의 몫을 맡습니다 (`debate()` 의 `round_num < max_rounds`).
    mado.check("ADR-051", "장부가 바뀔 때마다 개정 이력이 남음 (1라운드 끝 `round` + 마지막 라운드 번호의 `final`)",
               kinds == [("round", 1), ("final", 2)], kinds)
    seqs = [r.seq for r in revisions]
    mado.check("ADR-051", "개정 순번 seq 가 1부터 빈틈없이 늘어남", seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs)
    highs = [r.issue_high for r in revisions]
    mado.check("ADR-051", "issue_high 가 줄지 않음 (번호를 다시 쓰지 않음)", highs == sorted(highs), highs)
    mado.check("ADR-051", "마지막 개정의 장부 = 세션의 장부", revisions and revisions[-1].ledger == ledger,
               f"{len(revisions[-1].ledger) if revisions else 0} vs {len(ledger)}")
    last_open = [f"I-{i.get('no')}" for i in (revisions[-1].open_issues or [])] if revisions else []
    mado.check("ADR-051", "개정 행의 open_issues 가 장부의 미해결 쟁점과 같은 번호",
               sorted(last_open) == sorted(f"I-{n}" for n in numbers), f"{last_open} vs {numbers}")
    seen: dict = {}
    reused = []
    for revision in revisions:
        for issue in revision.open_issues or []:
            text = str(issue.get("text") or "")
            if issue.get("no") in seen and _different_issue(seen[issue["no"]], text):
                reused.append((issue["no"], seen[issue["no"]][:60], text[:60]))
            seen.setdefault(issue.get("no"), text)
    mado.note("ADR-051", f"개정 {kinds} · 쟁점 번호 추이 {[[i.get('no') for i in (r.open_issues or [])] for r in revisions]} · "
                         f"번호가 같은데 글이 크게 다른 쟁점(사람이 봐야 함): {reused or '없음'}")
    mado.settle()


@pytest.mark.adr(52)
def test_adr052_speech_numbers_headers_and_citations(mado, debate):
    from app.orchestration import context_memory as memory

    messages = debate["messages"]
    numbered = [m for m in messages if m.speech_no]
    mado.check("ADR-052", "전문가·오케스트레이터 발언(계획·발언·합성)에 번호가 있음",
               all(m.speech_no for m in messages if _kind(m) in ("plan", "speech", "synthesis") and m.msg_type != "error"),
               [(m.sender_key, _kind(m), m.speech_no) for m in messages if _kind(m) in SPEECH_KINDS])
    mado.check("ADR-052", "사용자 발언과 시스템 메모에는 번호가 없음",
               not [m for m in numbered if m.sender_key == "user" or _kind(m) in ("opening", "note", "nomination")],
               [(m.sender_key, _kind(m), m.speech_no) for m in numbered])
    numbers = [m.speech_no for m in numbered]
    mado.check("ADR-052", "번호가 겹치지 않고 기록 순서대로 늘어남", numbers == sorted(set(numbers)), numbers)

    outcome = debate["outcome"]
    speech_requests = [r for r in outcome.requests if r.agent in ("architect", "critic")
                       and "[결정 장부 갱신]" not in r.last_user()]
    cited = [r for r in speech_requests if memory.CITE_INSTRUCTION in r.text()]
    mado.check("ADR-052", "전문가 발언 요청마다 인용 지시가 들어 있음", len(cited) == len(speech_requests) > 0,
               f"{len(cited)}/{len(speech_requests)}")
    by_no = {m.speech_no: m for m in numbered}
    later = [r for r in speech_requests if re.search(r"· S\d+\]", r.text())]
    mado.check("ADR-052", "앞선 발언이 `[이름 (역할) · S<n>]:` 머리줄로 실려 감", bool(later), len(later))
    wrong_header = []
    for request in later:
        for name, number in re.findall(r"\[([^\[\]\n]{3,80}?) · S(\d+)\]", request.text()):
            if name == "이름 (역할)":  # 인용 지시문 안의 예시
                continue
            message = by_no.get(int(number))
            if message is None or not name.startswith(message.sender_name):
                wrong_header.append((request.index, name, number))
    mado.check("ADR-052", "머리줄의 번호가 DB 의 그 발언자·그 번호와 맞음", not wrong_header, wrong_header[:5])

    valid = invalid = self_header = 0
    samples = []
    for message in numbered:
        for number in re.findall(r"\[S(\d+)\]", message.content or ""):
            if int(number) in by_no and int(number) < message.speech_no:
                valid += 1
            else:
                invalid += 1
                samples.append((message.sender_key, message.speech_no, f"S{number}"))
        if re.match(r"\s*\[[^\[\]\n]{3,80} · S\d+\]:", message.content or ""):
            self_header += 1
    mado.check("ADR-052", "모델이 발언 머리줄을 스스로 적지 않음", self_header == 0, self_header)
    mado.note("ADR-052", f"모델이 쓴 [S n] 인용 {valid + invalid}건 중 유효 {valid} · 없는/미래 번호 {invalid} {samples[:5]} · "
                         f"[I-n] 인용 {sum(len(re.findall(r'\\[I-\\d+\\]', m.content or '')) for m in numbered)}건")
    mado.settle()


# --------------------------------------------------------------------------- 도우미


def _common_prefix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    for index in range(limit):
        if a[index] != b[index]:
            return index
    return limit


def _first_request_per_round(requests):
    """한 에이전트의 발언 요청에서 라운드마다 첫 요청 (도구 루프의 뒤 요청은 뺍니다)."""
    firsts = []
    for request in requests:
        roles = [m.get("role") for m in request.messages]
        if "tool" in roles:
            continue
        firsts.append(request)
    return firsts


def _different_issue(a: str, b: str) -> bool:
    tokens_a, tokens_b = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    if not tokens_a or not tokens_b:
        return False
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b)) < 0.2
