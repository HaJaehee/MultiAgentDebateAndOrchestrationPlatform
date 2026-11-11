"""ADR-049: 실행 기반 진단 에이전트, 결정적 `[시도·검증 이력]`, 루프 밖 정체 관찰자를 실제 LLM·실제 샌드박스로 돌립니다.

작업 공간에 "고칠 수 없게 묶인" 실패를 심습니다. `check.py` 는 `settings.json` 에 없는 키 때문에 매번 같은
`KeyError: 'stale_token'` 으로 실패하는데, 유저는 coder 에게 `cache.py` 만 고치라고 하고 나머지 두 파일은
건드리지 못하게 합니다. 증상만 보면 캐시 문제 같지만 근본 원인은 손댈 수 없는 곳에 있어, 라운드를 거듭해도
같은 실패가 남습니다 — 정체 관찰자가 2라운드 연속 같은 실패를 보고 재설계로 올려야 하는 상황입니다.

모델이 제약을 우회해 통과시켜 버리면(예: cache.py 에서 설정을 덧씌움) 정체는 생기지 않습니다. 그 경우는 실패가
아니라 "이번에는 정체가 생기지 않았다" 로 기록합니다.
"""

import json

import pytest

FILES = {
    "cache.py": (
        "class TokenCache:\n"
        "    def __init__(self):\n"
        "        self.store = {}\n\n"
        "    def put(self, key, value):\n"
        "        self.store[key] = value\n\n"
        "    def get(self, key):\n"
        "        return self.store[key]\n"
    ),
    "check.py": (
        "import json\n"
        "from cache import TokenCache\n\n"
        "settings = json.load(open('settings.json', encoding='utf-8'))\n"
        "cache = TokenCache()\n"
        "cache.put('fresh_token', 1)\n"
        "assert cache.get('fresh_token') == 1\n"
        "quota = settings['quotas']['stale_token']\n"
        "print('CHECK PASSED', quota)\n"
    ),
    "settings.json": json.dumps({"quotas": {"fresh_token": 10}}, indent=2) + "\n",
}
PROMPT = (
    "작업 공간의 check.py 를 실행하면 `KeyError: 'stale_token'` 으로 실패합니다. 토큰 캐시(cache.py) 문제로 보입니다.\n"
    "coder 는 **cache.py 만** 고쳐서 check.py 가 통과하게 하세요. check.py 와 settings.json 은 운영팀 소유라 "
    "**절대 수정·삭제·덮어쓰기 금지**이고, cache.py 안에서 그 파일들을 바꾸거나 json 로딩을 가로채는 우회도 금지입니다.\n"
    "diagnostician 은 매 라운드 sandbox 로 check.py 를 **실제로 실행**해 통과 여부와 트레이스백을 보고하세요.\n"
    "각 발언은 12줄 이내로 쓰세요."
)
PROTECTED = ("check.py", "settings.json")


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


@pytest.fixture(scope="module")
def stuck(mado):
    async def go():
        sid = await mado.new_session(
            "diagnostician", agents=["orchestrator", "coder", "diagnostician"], max_rounds=3,
            converge_early=False, files=FILES, orchestrator_hands_off=True,
        )
        outcome = await mado.turn(sid, PROMPT, timeout=2700)
        return sid, outcome

    sid, outcome = mado.run(go(), timeout=3000)
    mado.dump("diagnostician", outcome.requests, {"events": [e.get("type") for e in outcome.events]})
    workspace = mado.workspaces / "diagnostician"
    return {"sid": sid, "outcome": outcome, "workspace": workspace,
            "messages": mado.run(mado.messages(sid)), "calls": mado.run(mado.tool_calls(sid)),
            "turns": mado.run(mado.turns(sid))}


def _speech_requests(outcome, agent):
    return [r for r in outcome.requests_by(agent) if "[결정 장부 갱신]" not in r.last_user()]


@pytest.mark.adr(49)
def test_adr049_the_diagnostician_executes_and_cannot_write(mado, stuck):
    from app.orchestration import diagnostic_loop

    outcome = stuck["outcome"]
    mado.check("ADR-049", "턴이 끝남", outcome.error is None, outcome.error)
    speeches = [m for m in stuck["messages"] if m.sender_key == "diagnostician" and _kind(m) == "speech"]
    mado.check("ADR-049", "진단 에이전트가 라운드마다 발언함", len(speeches) >= 2,
               [(m.round_number, m.msg_type) for m in speeches])
    by_message = {}
    for call in stuck["calls"]:
        if call.agent_key == "diagnostician":
            by_message.setdefault(call.message_id, []).append(call)
    for speech in speeches:
        calls = by_message.get(speech.id, [])
        executed = [c for c in calls if diagnostic_loop.is_execution_tool_name(c.tool_name) and c.status != "denied"]
        stamped = diagnostic_loop.UNVERIFIED_DIAGNOSTIC_TAG in (speech.content or "")
        mado.check("ADR-049", f"{speech.round_number}라운드 진단: 실행 도구를 실제로 불렀거나, 아니면 `[실행 검증 미수행]` 이 붙음",
                   bool(executed) != stamped or (bool(executed) and not stamped),
                   f"실행 {len(executed)}건 · 표시 {stamped} · 호출 {[(c.tool_name, c.status) for c in calls]}")
    retries = [r for r in _speech_requests(outcome, "diagnostician") if "[실행 기반 진단 필수]" in r.text()]
    mado.note("ADR-049", f"실행 없이 의견만 내 보완을 요구받은 횟수: {len(retries)} · "
                         f"진단 발언별 실행 {[len([c for c in by_message.get(s.id, []) if diagnostic_loop.is_execution_tool_name(c.tool_name)]) for s in speeches]}")

    offered = {name for r in _speech_requests(outcome, "diagnostician") for name in r.tool_names}
    writers = sorted(n for n in offered if n in (
        "filesystem__write_file", "filesystem__edit_file", "filesystem__move_file", "filesystem__create_directory",
        "sandbox__write_workspace_file", "sandbox__append_workspace_file", "git__git_commit", "git__git_add",
    ))
    mado.check("ADR-049", "진단 에이전트에게는 쓰기 도구가 아예 실리지 않음 (늘 거부될 도구는 목록에서 뺌)", not writers, writers)
    wrote = [(c.tool_name, c.status, c.rule) for c in stuck["calls"] if c.agent_key == "diagnostician"
             and c.status == "success" and any(w in c.tool_name for w in ("write", "edit_file", "append", "move_file"))]
    mado.check("ADR-049", "진단 에이전트가 쓰기 도구로 파일을 고친 기록이 없음", not wrote, wrote)
    denied = [(c.tool_name, c.rule) for c in stuck["calls"] if c.agent_key == "diagnostician" and c.status == "denied"]
    mado.note("ADR-049", f"진단 에이전트의 거부된 호출: {denied or '없음'}")

    # 쓰기 금지(`write(**)`)는 샌드박스 코드 안의 쓰기에도 걸려야 합니다 (ADR-049 §1). 처음 구현의 코드 정적 검사는
    # 경로가 글자 그대로 적힌 쓰기만 알아봐, `(root / "x").write_text(...)` 처럼 만든 경로로 쓰면 검사를 지나갔습니다.
    from app.mcp.exec_scan import scan_python

    skipped = {".git", ".mado", ".memory-graphs", "__pycache__"}
    present = {p.relative_to(stuck["workspace"]).as_posix() for p in stuck["workspace"].rglob("*")
               if p.is_file() and not skipped & set(p.relative_to(stuck["workspace"]).parts)}
    code_calls = ("sandbox__execute_python_code", "sandbox__run_python_file")
    runs = [c for c in stuck["calls"] if c.agent_key == "diagnostician" and c.status == "success"
            and c.tool_name in code_calls]
    # coder 는 쓰기가 허용됩니다. 다른 에이전트의 호출 인자(파일 이름·코드)에 나오는 파일은 그쪽이 만든 것으로 봅니다.
    others = json.dumps([c.arguments for c in stuck["calls"] if c.agent_key != "diagnostician"], ensure_ascii=False)
    made = sorted(
        path for path in present - set(FILES)
        if path.split("/")[0] not in others
        and any(path.split("/")[0] in str((c.arguments or {}).get("code") or "") for c in runs)
    )
    mado.check("ADR-049", "쓰기가 금지된 진단 에이전트의 코드 실행이 작업 공간에 파일을 만들지 못함", not made,
               f"{len(made)}개: {made[:8]}")
    slipped = []
    for call in runs:
        scan = scan_python(str((call.arguments or {}).get("code") or ""))
        if scan.writes or scan.deletes:
            slipped.append((call.tool_name, call.rule, scan.writes[:3], scan.deletes[:3]))
    mado.check("ADR-049", "허용된 진단 실행 중에 쓰기·삭제가 든 코드가 없음 (경로를 모르는 쓰기 포함)", not slipped, slipped[:4])
    blocked = [(c.rule, scan_python(str((c.arguments or {}).get("code") or "")).writes[:3]) for c in stuck["calls"]
               if c.agent_key == "diagnostician" and c.status == "denied" and c.tool_name in code_calls
               and c.rule == "write(**)"]
    mado.note("ADR-049", f"`write(**)` 에 막힌 진단 실행 {len(blocked)}건 (쓰기 대상, 빈 값은 경로를 모르는 쓰기): {blocked[:4]}")
    mado.settle()


@pytest.mark.adr(49)
def test_adr049_attempt_history_rides_only_on_the_last_user_message(mado, stuck):
    from app.orchestration.diagnostic_loop import ATTEMPT_HISTORY_HEADING

    outcome = stuck["outcome"]
    speech_ids = {m.id: m for m in stuck["messages"] if _kind(m) == "speech"}
    carried, misplaced, early = 0, [], []
    for agent in ("coder", "diagnostician"):
        requests = [r for r in _speech_requests(outcome, agent) if not any(m.get("role") == "tool" for m in r.messages)]
        for position, request in enumerate(requests):
            users = [i for i, m in enumerate(request.messages) if m.get("role") == "user"]
            holders = [i for i, m in enumerate(request.messages)
                       if ATTEMPT_HISTORY_HEADING in (m.get("content") if isinstance(m.get("content"), str) else "")]
            if position == 0 and holders:
                early.append((agent, request.index))
            if holders:
                carried += 1
                if holders != [users[-1]]:
                    misplaced.append((agent, request.index, holders, users[-1]))
    mado.check("ADR-049", "1라운드 요청에는 `[시도·검증 이력]` 이 없음 (아직 끝난 라운드가 없음)", not early, early)
    mado.check("ADR-049", "2라운드부터 `[시도·검증 이력]` 이 실림", carried >= 1, carried)
    mado.check("ADR-049", "이력은 마지막 유저 메시지에만 붙음 (앞부분은 그대로라 캐시가 유지됨)", not misplaced, misplaced)
    sample = next((r.last_user() for agent in ("coder", "diagnostician") for r in _speech_requests(outcome, agent)
                   if ATTEMPT_HISTORY_HEADING in r.last_user()), "")
    block = sample[sample.find(ATTEMPT_HISTORY_HEADING):][:900] if sample else ""
    mado.check("ADR-049", "이력에 실행 실패의 예외와 위치가 결정적으로 뽑혀 있음 (KeyError · check.py)",
               "KeyError" in block and "check.py" in block, block)
    mado.note("ADR-049", f"이력 블록 예: {block!r}")
    mado.settle()


@pytest.mark.adr(49)
def test_adr049_two_rounds_of_the_same_failure_escalate_to_a_redesign(mado, stuck):
    from app.orchestration import context_memory as memory
    from app.orchestration import diagnostic_loop

    outcome, messages = stuck["outcome"], stuck["messages"]
    protected_changed = {name: (stuck["workspace"] / name).read_text(encoding="utf-8") != FILES[name]
                         for name in PROTECTED if (stuck["workspace"] / name).exists()}
    missing = [name for name in PROTECTED if not (stuck["workspace"] / name).exists()]
    mado.note("ADR-049", f"유저가 금지한 파일을 에이전트가 바꿨는가: {protected_changed} · 지워짐 {missing} · "
                         f"cache.py 최종: {(stuck['workspace'] / 'cache.py').read_text(encoding='utf-8')[:400]!r}")

    notes = [m for m in messages if _kind(m) == "stagnation"]
    events = outcome.events_of("stagnation_detected")
    failures = [(c.agent_key, (c.output or "")[:60].replace("\n", " ")) for c in stuck["calls"]
                if "KeyError" in (c.output or "")]
    if not notes:
        mado.note("ADR-049", f"이번 실행에서는 정체가 감지되지 않았습니다 — KeyError 를 본 실행 {len(failures)}건 {failures[:4]}. "
                             f"라운드별로 같은 실패가 2번 이어지지 않았거나, 모델이 우회해 통과시켰습니다.")
        mado.settle()
        return
    note = notes[0]
    meta = note.turn_meta or {}
    mado.check("ADR-049", "정체 안내가 2라운드 끝에 한 번 기록되고 이벤트도 나감",
               len(notes) == 1 and note.round_number == 2 and len(events) == 1,
               f"안내 {[(n.round_number) for n in notes]} · 이벤트 {len(events)}")
    signatures = meta.get("signatures") or []
    mado.check("ADR-049", "실패 서명이 줄 번호 없이 예외·파일로 정규화됨 (`exec:keyerror…@check.py`)",
               any("keyerror" in str(s).lower() and "check.py" in str(s) and ":8" not in str(s) for s in signatures),
               signatures)
    redesign = [r for r in outcome.requests if diagnostic_loop.REDESIGN_PROMPT_MARKER in r.last_user()]
    mado.check("ADR-049", "오케스트레이터에게 재설계 요청이 한 번 감", len(redesign) == 1, len(redesign))
    if redesign:
        text = redesign[0].text()
        leaked = []
        for message in messages:
            if _kind(message) == "speech" and message.round_number <= 2:
                body = (message.content or "").strip()
                probe = body[len(body) // 2: len(body) // 2 + 60]
                if len(probe) >= 40 and probe in text:
                    leaked.append((message.sender_key, message.round_number))
        mado.check("ADR-049", "재설계 요청에는 참여자들의 발언 원문이 들어가지 않음 (편향 격리)", not leaked, leaked)
        mado.check("ADR-049", "재설계 요청에 원래 요청·시도 이력·실패 증거가 실림",
                   "check.py" in text and diagnostic_loop.ATTEMPT_HISTORY_HEADING in text and "KeyError" in text,
                   text[-900:])
    plans = [m for m in messages if _kind(m) == "plan"]
    after = [m for m in plans if m.created_at > note.created_at]
    mado.check("ADR-049", "정체 안내 뒤에 새 계획이 한 건 기록됨", len(plans) == 2 and len(after) == 1,
               [(m.round_number, m.created_at.strftime('%H:%M:%S')) for m in plans])
    approvals = [m for m in messages if _kind(m) == "plan_approval" and m.created_at > note.created_at]
    mado.check("ADR-049", "재설계 계획은 승인을 다시 묻지 않고 바로 적용됨", not approvals, len(approvals))
    third = [r for agent in ("coder", "diagnostician") for r in _speech_requests(outcome, agent)
             if r.at > 0 and after and after[0].content[:60] in r.text()]
    superseded = [r for agent in ("coder", "diagnostician") for r in _speech_requests(outcome, agent)
                  if memory.SUPERSEDED_PLAN_PLACEHOLDER[:30] in r.text()]
    mado.check("ADR-049", "남은 라운드의 발언은 새 계획을 보고, 실패한 옛 계획은 자리 표시로 접힘",
               bool(third) and bool(superseded), f"새 계획을 실은 요청 {len(third)} · 옛 계획 접힘 {len(superseded)}")
    mado.note("ADR-049", f"정체 서명 {signatures} · 연속 {meta.get('streak')}회 · 재설계 계획 머리: {after[0].content[:300] if after else None!r}")
    mado.settle()
