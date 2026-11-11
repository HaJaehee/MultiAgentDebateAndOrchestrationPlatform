"""도구가 컨텍스트를 차지하는 문제를 다룬 두 결정을 실제 MCP 서버와 실제 LLM 으로 확인합니다.

* ADR-039 도구 스키마 지연 로딩 — 번들의 실제 서버(filesystem·sandbox·git) 도구가 나뉘어 실리는지, 모델이 기다리는 도구를
  실제로 불러 쓰는지, 불러 둔 도구가 그 대화의 그 에이전트에게 남는지.
* ADR-040 지난 파일 읽기 결과 압축 — 창이 작은 에이전트가 큰 파일을 연달아 읽을 때 오래된 결과가 스텁으로 바뀌는지,
  도구 기록에는 원문이 남는지, 그리고 **모델이 스텁 뒤에서 내용을 지어내지 않는지**.
"""

import json
import os
import random
import subprocess

import pytest

SECRET_COMMIT = "fix: rotate signing key (ticket ZEPHYR-31)"

pytestmark = pytest.mark.acceptance


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


def _git_repo_with_history(path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@localhost",
           "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@localhost"}

    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "chore: first commit")
    (path / "keys.txt").write_text("rotated\n", encoding="utf-8")
    git("add", "keys.txt")
    git("commit", "-q", "-m", SECRET_COMMIT)


# --------------------------------------------------------------------------- ADR-039


@pytest.fixture(scope="module")
def lazy(mado):
    from app.agents.tool_loading import remembered_loaded

    async def go():
        workspace = mado.workspace("lazy-tools")
        _git_repo_with_history(workspace)
        sid = await mado.new_session("lazy-tools", agents=["orchestrator", "coder"], max_rounds=1,
                                     orchestrator_hands_off=True)
        first = await mado.turn(
            sid,
            "이 작업 공간 git 저장소의 **가장 최근 커밋 메시지**를 git 도구로 확인해, 그 안에 적힌 티켓 번호를 보고하세요. "
            "추측하지 말고 반드시 도구로 확인하세요. 파일을 고치지 마세요.",
        )
        remembered = list(remembered_loaded(sid, "coder"))
        second = await mado.turn(sid, "방금 확인한 커밋의 바로 앞 커밋 메시지도 git 도구로 확인해 보고하세요. 파일을 고치지 마세요.")
        return sid, first, second, remembered

    sid, first, second, remembered = mado.run(go(), timeout=2400)
    mado.dump("lazy-tools-1", first.requests)
    mado.dump("lazy-tools-2", second.requests)
    return {"sid": sid, "first": first, "second": second, "remembered": remembered,
            "messages": mado.run(mado.messages(sid)), "calls": mado.run(mado.tool_calls(sid))}


@pytest.mark.adr(39)
def test_adr039_only_core_tools_ride_along_and_the_rest_wait(mado, lazy):
    import litellm

    from app.agents.pool import get_agent_pool
    from app.agents.tool_loading import LOAD_TOOLS_TOOL
    from app.mcp.pool import get_runtime_pool

    first = lazy["first"]
    mado.check("ADR-039", "턴이 끝남", first.error is None, first.error)
    speech = [r for r in first.requests_by("coder") if "[결정 장부 갱신]" not in r.last_user()]
    mado.check("ADR-039", "coder 의 발언 요청이 있음", bool(speech), len(first.requests))
    if not speech:
        mado.settle()
        return
    opening = speech[0]
    names = set(opening.tool_names)
    mado.check("ADR-039", "첫 요청에 `tools__load_tools` 가 실림", LOAD_TOOLS_TOOL in names, sorted(names))
    for core in ("filesystem__read_text_file", "sandbox__execute_python_code", "git__git_status"):
        mado.check("ADR-039", f"핵심 도구 `{core}` 는 처음부터 실림", core in names, sorted(names))
    for guided in ("filesystem__write_file", "filesystem__edit_file"):
        mado.check("ADR-039", f"상시 지침이 이름을 짚는 `{guided}` 는 처음부터 실림", guided in names, sorted(names))
    mado.check("ADR-039", "기다리는 도구 `git__git_log` 는 첫 요청에 스키마가 없음", "git__git_log" not in names, sorted(names))
    catalog = next((t for t in opening.tools if (t.get("function") or {}).get("name") == LOAD_TOOLS_TOOL), {})
    description = (catalog.get("function") or {}).get("description", "")
    mado.check("ADR-039", "불러오기 도구의 설명에 기다리는 도구 목록이 한 줄씩 있음", "git_log" in description, description[:400])

    # 실제 서버의 전체 도구와 비교한 절감량.
    workspace = mado.workspaces / "lazy-tools"
    coder = get_agent_pool().get("coder")
    manager = get_runtime_pool().get(workspace)
    everything = manager.get_openai_tools_for_servers(coder.allowed_mcp_servers) if manager is not None else []
    if everything:
        def tokens(tools):
            return litellm.token_counter(model="gpt-4o", messages=[{"role": "user", "content": "x"}], tools=tools)

        full, offered = tokens(everything), tokens(opening.tools)
        mado.check("ADR-039", "실은 도구 수가 전체보다 적고, 아낀 스키마 토큰이 기준(1,000)을 넘음",
                   len(opening.tools) < len(everything) and full - offered >= 1000,
                   f"도구 {len(opening.tools)}/{len(everything)}개 · 스키마 {offered}/{full} 토큰")
        mado.note("ADR-039", f"coder(filesystem+sandbox+git): 도구 {len(everything)}개 중 {len(opening.tools)}개만 실음 · "
                             f"스키마 토큰 {full} → {offered} ({(full - offered) / full:.0%} 절감, gpt-4o 토크나이저)")
    mado.settle()


@pytest.mark.adr(39)
def test_adr039_the_model_reaches_a_waiting_tool_and_keeps_it(mado, lazy):
    from app.agents.tool_loading import LOAD_TOOLS_TOOL

    first, second = lazy["first"], lazy["second"]
    turn1 = [c for c in lazy["calls"] if c.agent_key == "coder"]
    log_calls = [c for c in turn1 if c.tool_name == "git__git_log"]
    loads = [c for c in turn1 if c.tool_name == LOAD_TOOLS_TOOL]
    mado.check("ADR-039", "모델이 기다리던 `git__git_log` 를 실제로 실행함",
               any(c.status == "success" for c in log_calls), [(c.tool_name, c.status) for c in turn1])
    speech = [r for r in first.requests_by("coder") if "[결정 장부 갱신]" not in r.last_user()]
    after = [r for r in speech if "git__git_log" in r.tool_names]
    mado.check("ADR-039", "불러온 뒤의 요청에는 `git__git_log` 스키마가 tools 에 실림", bool(after),
               [len(r.tool_names) for r in speech])
    answer = next((m for m in lazy["messages"] if m.sender_key == "coder" and _kind(m) == "speech"), None)
    mado.check("ADR-039", "도구로 확인한 값을 답함 (ZEPHYR-31)", answer is not None and "ZEPHYR-31" in answer.content,
               answer.content[:300] if answer else None)
    path = "load_tools 로 먼저 불러옴" if loads else "목록만 보고 바로 호출함 (ADR-039 §5 경로)"
    mado.note("ADR-039", f"{mado.s2} 의 경로: {path} · 호출 순서 {[(c.tool_name, c.status) for c in turn1]}")

    mado.check("ADR-039", "불러 둔 도구가 그 대화의 그 에이전트에게 남음", "git__git_log" in lazy["remembered"], lazy["remembered"])
    second_speech = [r for r in second.requests_by("coder") if "[결정 장부 갱신]" not in r.last_user()]
    mado.check("ADR-039", "다음 턴 첫 요청부터 `git__git_log` 가 실려 있어 다시 불러오지 않음",
               bool(second_speech) and "git__git_log" in second_speech[0].tool_names,
               second_speech[0].tool_names if second_speech else None)
    # 이미 실려 있는 도구를 모델이 또 불러 달라고 하는 것은 모델의 버릇이라 막지 않습니다. 그 요청이 오류 없이 답해지는지만 봅니다.
    reloads = [c for c in lazy["calls"] if c.tool_name == LOAD_TOOLS_TOOL and c.agent_key == "coder"
               and c.created_at > turn1[-1].created_at]
    mado.check("ADR-039", "이미 실린 도구를 다시 불러 달라는 요청도 오류 없이 답함", all(c.status == "success" for c in reloads),
               [(c.arguments, c.status, (c.output or "")[:160]) for c in reloads])
    mado.note("ADR-039", f"두 번째 턴의 불러오기 요청: {[(c.arguments, (c.output or '')[:200]) for c in reloads] or '없음'}")
    failed = [(c.tool_name, json.dumps(c.arguments, ensure_ascii=False)[:90], (c.output or "")[:110]) for c in lazy["calls"]
              if c.status == "error"]
    mado.note("tools", f"이 대화에서 실패한 도구 호출 {len(failed)}건 (경로 혼동 등): {failed[:6]}")

    # 불러오기가 치른 값의 실측 (ADR-039 가 "재지 못했다" 고 적어 둔 것). 사용량 기록에 함께 실립니다.
    from app.agents.llm_usage import Usage, tool_context_line

    measured = Usage()
    for turn in mado.run(mado.turns(lazy["sid"])):
        measured.add(Usage.of(turn.usage))
    mado.check("ADR-039", "도구를 불러온 횟수가 턴의 사용량 기록에 실림", measured.tool_loads >= 1, measured.as_meta())
    mado.note("ADR-039", f"실측(두 턴 · {mado.s2}): {tool_context_line(measured)} · LLM 호출 {measured.calls}회")
    mado.settle()


# --------------------------------------------------------------------------- ADR-040

FILES = 8


def _log_file(index: int, code: str) -> str:
    """앞·뒤는 평범하고 가운데(스텁에서 잘려 나가는 자리)에 확인 코드가 있는 약 1만 자 파일."""
    rng = random.Random(index)
    words = ["cache", "worker", "queue", "retry", "shard", "probe", "flush", "merge", "index", "commit"]

    def line(n: int) -> str:
        return f"{n:04d} " + " ".join(rng.choice(words) for _ in range(9)) + f" ok seq={rng.randint(1000, 9999)}"

    body = [f"# service log part {index}"] + [line(n) for n in range(1, 81)]
    body.append(f"AUDIT-CODE part{index} = {code}")
    body += [line(n) for n in range(82, 161)]
    return "\n".join(body) + "\n"


@pytest.fixture(scope="module")
def compaction(mado):
    codes = {i: f"{w}-{100 + i * 7}" for i, w in enumerate(
        ["AMBER", "BASIL", "CEDAR", "DELTA", "EMBER", "FJORD", "GROVE", "HAZEL"], start=1)}
    files = {f"logs/part{i}.txt": _log_file(i, code) for i, code in codes.items()}

    async def go():
        sid = await mado.new_session("compaction", agents=["orchestrator", "rt_reader"], max_rounds=1, files=files,
                                     orchestrator_hands_off=True)
        outcome = await mado.turn(
            sid,
            f"작업 공간의 logs/part1.txt 부터 logs/part{FILES}.txt 까지를 read_text_file 로 **한 번에 한 파일씩 순서대로** "
            "전부 읽으세요 (read_multiple_files 를 쓰지 말고, head/tail 로 일부만 읽지도 마세요). "
            "각 파일 한가운데에 `AUDIT-CODE partN = 값` 줄이 하나씩 있습니다. 여덟 파일의 값을 모두 찾아 "
            "마지막에 `partN: 값` 형식의 목록으로 보고하세요. 값을 확인하지 못한 파일은 추측하지 말고 다시 읽으세요.",
            timeout=2400,
        )
        return sid, outcome

    sid, outcome = mado.run(go(), timeout=2700)
    mado.dump("compaction", outcome.requests, {"events": [e.get("type") for e in outcome.events]})
    return {"sid": sid, "outcome": outcome, "codes": codes, "sizes": {k: len(v) for k, v in files.items()},
            "messages": mado.run(mado.messages(sid)), "calls": mado.run(mado.tool_calls(sid))}


@pytest.mark.adr(40)
def test_adr040_stale_file_reads_become_stubs_but_logs_keep_the_text(mado, compaction):
    from app.agents.tool_compaction import COMPACTED_OUTPUT_HEAD

    outcome = compaction["outcome"]
    mado.check("ADR-040", "턴이 끝남", outcome.error is None, outcome.error)
    reader = [r for r in outcome.requests_by("rt_reader") if "[결정 장부 갱신]" not in r.last_user()]
    stubbed = []
    for request in reader:
        stubs = [m for m in request.messages if m.get("role") == "tool"
                 and str(m.get("content") or "").startswith(COMPACTED_OUTPUT_HEAD)]
        if stubs:
            stubbed.append((request, stubs))
    mado.check("ADR-040", "오래된 파일 읽기 결과가 스텁으로 바뀐 요청이 있음", bool(stubbed),
               f"발언 요청 {len(reader)}건 · 마지막 요청의 입력 토큰 {reader[-1].usage.get('prompt_tokens') if reader else None}")
    if stubbed:
        request, stubs = stubbed[-1]
        sample = str(stubs[0]["content"])
        mado.check("ADR-040", "스텁에 호출 인자(경로)와 전체 크기가 적혀 다시 읽을 수 있음",
                   "part" in sample.split("\n")[0] and "전체" in sample and "다시" in sample, sample[:400])
        mado.check("ADR-040", "스텁 하나가 대략 1,000자 남짓으로 줄어듦", len(sample) < 2600, len(sample))
        mado.check("ADR-040", "스텁이 도구가 아니라 앱이 나중에 줄였다고 밝힘 (설계 4)", "앱이 나중에" in sample, sample[:300])
        tools = [m for m in request.messages if m.get("role") == "tool"]
        newest = str(tools[-1].get("content") or "")
        mado.check("ADR-040", "가장 최근 읽기 결과는 원문 그대로", not newest.startswith(COMPACTED_OUTPUT_HEAD), newest[:80])
        first_stubbed = stubbed[0][0]
        usage_ratio = (first_stubbed.usage.get("prompt_tokens") or 0) / 24000
        mado.note("ADR-040", f"압축이 처음 보인 요청: {reader.index(first_stubbed) + 1}번째/{len(reader)}건 · 그 요청의 입력 토큰 "
                             f"{first_stubbed.usage.get('prompt_tokens')} (창 24,000 의 {usage_ratio:.0%}) · 스텁 {len(stubbed[0][1])}개 → "
                             f"마지막 요청 스텁 {len(stubs)}개 / 도구 결과 {len(tools)}개")

    notices = sorted({line for r in reader for m in r.messages if m.get("role") == "user"
                      for line in str(m.get("content") or "").splitlines() if line.startswith("[컨텍스트]")})
    if notices:
        mado.check("ADR-040", "사용률 고지가 줄일 수 있는 지난 파일 읽기 몫을 함께 알림 (설계 4)",
                   any("지난 파일 읽기를 줄이면" in n for n in notices), [n[:200] for n in notices])
    mado.note("ADR-040", f"컨텍스트 고지 {len(notices)}종: {[n[:220] for n in notices]}")

    reads = [c for c in compaction["calls"] if c.tool_name == "filesystem__read_text_file" and c.status == "success"]
    full = [c for c in reads if len(c.output or "") >= 8000]
    mado.check("ADR-040", "도구 기록(tool_calls)에는 읽은 원문이 그대로 남음", len(full) >= min(FILES, len(reads)) > 0,
               [(c.arguments, len(c.output or "")) for c in reads][:10])
    trimmed = [e for e in outcome.events if e.get("type") in ("context_trimmed", "context_window_exhausted")]
    mado.check("ADR-040", "압축 덕에 기록을 통째로 버리거나 창 확장을 묻는 일이 없었음", not trimmed,
               [e.get("type") for e in trimmed])
    prompts = [r.usage.get("prompt_tokens") or 0 for r in reader]
    mado.note("ADR-040", f"파일 {FILES}개 × 약 {max(compaction['sizes'].values())}자 · 요청별 입력 토큰 {prompts} · "
                         f"읽기 호출 {len(reads)}회 (다시 읽기 {max(0, len(reads) - FILES)}회)")

    # 압축이 치른 값의 실측 (ADR-040 이 "재지 못했다" 고 적어 둔 것): 줄인 건수와, 줄인 뒤 같은 파일을 다시 읽은 횟수.
    from app.agents.llm_usage import Usage, tool_context_line

    measured = Usage.of(mado.run(mado.turns(compaction["sid"]))[-1].usage)
    if stubbed:
        mado.check("ADR-040", "줄인 건수가 턴의 사용량 기록에 실림", measured.compacted_reads >= 1, measured.as_meta())
    mado.check("ADR-040", "압축 뒤 다시 읽기는 읽기 호출 가운데 처음이 아닌 것보다 많을 수 없음",
               measured.rereads_after_compaction <= max(0, len(reads) - 1), (measured.as_meta(), len(reads)))
    mado.note("ADR-040", f"실측({mado.s2} · 창 24,000): {tool_context_line(measured) or '압축 없음'} · 읽기 호출 {len(reads)}회 · "
                         f"LLM 호출 {measured.calls}회")
    mado.settle()


@pytest.mark.adr(40)
def test_adr040_the_model_does_not_invent_what_the_stub_dropped(mado, compaction):
    answer = next((m for m in compaction["messages"] if m.sender_key == "rt_reader" and _kind(m) == "speech"), None)
    mado.check("ADR-040", "rt_reader 의 발언이 기록됨", answer is not None and answer.msg_type != "error",
               answer.content[:200] if answer else None)
    if answer is None:
        mado.settle()
        return
    right = [i for i, code in compaction["codes"].items() if code in answer.content]
    import re

    claimed = dict(re.findall(r"part\s*(\d)\s*[:=]\s*`?([A-Z]+-\d+)", answer.content))
    wrong = {n: v for n, v in claimed.items() if compaction["codes"].get(int(n)) != v}
    # 기준은 "지어내지 않는다" 입니다. 전부 읽었는지는 모델의 선택이라 관찰로 남깁니다 — 컨텍스트 사용률 고지를 보고
    # 일찍 멈춘 실행이 있었습니다. 2026-11-11 부터 고지가 "지난 파일 읽기를 줄이면 약 N 토큰" 을 함께 알리고, 스텁은 도구가
    # 아니라 앱이 줄였다고 밝힙니다 (설계 4). 그 뒤로 몇 개를 읽고 멈췄는지, 줄어든 파일을 다시 읽었는지를 함께 남깁니다.
    mado.check("ADR-040", "스텁으로 줄어든 파일의 값을 지어내지 않음 (보고한 값은 모두 정답)", bool(right) and not wrong,
               f"맞음 {right} · 틀림 {wrong}")
    stopped_early = len(right) < FILES
    mado.note("ADR-040", f"보고한 값 {claimed} · 정답 일치 {len(right)}/{FILES} · 일찍 멈춤: {stopped_early}"
                         + (f" — 모델의 사유: {answer.content[answer.content.find('사유'):][:260]!r}" if stopped_early else ""))
    mado.settle()
