"""ADR-046: 끊긴 턴을 이어 갈 때 (1) 끊기던 파일 쓰기가 디스크에 닿았는지 앱이 확인해 알려 주는지,
(2) 사라진 커널 변수를 유저가 고른 때만 셀을 다시 돌려 되살리는지를 실제 샌드박스와 실제 LLM 으로 확인합니다.

"서버가 죽었다" 는 하네스가 흉내 냅니다: 도구 실행 앞이나 뒤에서 턴의 태스크를 취소하고(`crash_turn`), MCP 서버
프로세스(커널 포함)를 모두 내린 뒤 돌던 턴을 "끊김" 으로 적습니다(`restart_server`, 서버 기동 때 하는 일과 같습니다).
그 뒤의 이어 가기는 진짜 `resume_turn` 입니다.

모델이 실제로 보는 것은 "[디스크 확인] 이미 반영되었으니 되풀이하지 마세요" 같은 한 줄입니다. 그 말을 듣고
**DeepSeek 가 같은 덧붙이기를 또 하지 않는지**가 이 결정의 실제 효과입니다.
"""

import pytest

APPEND_LINE = "APPENDED-BY-AGENT-ONCE"


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


async def _drafts(mado, sid):
    from app.database.models import SpeechDraftModel

    return await mado.rows(SpeechDraftModel, SpeechDraftModel.session_id == sid)


# --------------------------------------------------------------------------- 끊긴 쓰기


APPEND_PROMPT = (
    "sandbox 의 append_workspace_file 도구로 notes.txt 끝에 정확히 `" + APPEND_LINE + "` 한 줄을 덧붙이세요 "
    "(내용 끝에 줄바꿈 포함). 그런 다음 filesystem 의 read_text_file 로 notes.txt 를 읽어 그 줄이 몇 번 들어 있는지 세어 보고하세요. "
    "다른 파일은 만들지 마세요."
)


async def _cut_append(mado, name: str, when: str):
    """덧붙이기 호출의 `when`(before=실행 전, after=실행 후 응답을 받기 전)에서 끊고 이어 갑니다."""
    sid = await mado.new_session(name, agents=["orchestrator", "coder"], max_rounds=1,
                                 files={"notes.txt": "first line\n"}, orchestrator_hands_off=True)

    def fault(moment, agent, tool, args):
        return moment == when and tool == "sandbox__append_workspace_file"

    crashed = await mado.crash_turn(sid, APPEND_PROMPT, fault)
    workspace = mado.workspaces / name
    text_at_crash = (workspace / "notes.txt").read_text(encoding="utf-8")
    drafts = await _drafts(mado, sid)
    marked = await mado.restart_server()
    turn = (await mado.turns(sid))[-1]
    resumed = await mado.resume(sid, turn.id, "continue")
    return {
        "sid": sid, "crashed": crashed, "resumed": resumed, "workspace": workspace, "marked": marked,
        "text_at_crash": text_at_crash, "drafts": [dict(d.state or {}) for d in drafts],
        "turn_before": turn, "turns": await mado.turns(sid), "messages": await mado.messages(sid),
        "calls": await mado.tool_calls(sid),
    }


@pytest.fixture(scope="module")
def cut_after(mado):
    result = mado.run(_cut_append(mado, "resume-write-applied", "after"), timeout=2400)
    mado.dump("resume-write-applied", result["crashed"].requests + result["resumed"].requests)
    return result


@pytest.fixture(scope="module")
def cut_before(mado):
    result = mado.run(_cut_append(mado, "resume-write-missing", "before"), timeout=2400)
    mado.dump("resume-write-missing", result["crashed"].requests + result["resumed"].requests)
    return result


def _probe_checks(mado, story, label: str, applied: bool):
    from app.agents import write_probe

    mado.check("ADR-046", f"{label}: 덧붙이기 호출에서 턴이 끊김", story["crashed"].crashed, story["crashed"].error)
    mado.check("ADR-046", f"{label}: 재시작 뒤 그 턴이 '끊김' 으로 적힘", story["turn_before"].status == "interrupted",
               f"{story['turn_before'].status} · {story['marked']}")
    count_at_crash = story["text_at_crash"].count(APPEND_LINE)
    mado.check("ADR-046", f"{label}: 끊긴 순간 디스크 상태가 시나리오대로 ({'쓰임' if applied else '안 쓰임'})",
               count_at_crash == (1 if applied else 0), repr(story["text_at_crash"]))
    plans = [plan for state in story["drafts"] for plan in (state.get("open_writes") or {}).values()]
    mado.check("ADR-046", f"{label}: 초안에 실행 직전의 해시와 '성공하면 될 해시' 가 남아 있음",
               len(plans) == 1 and plans[0].get("before") and plans[0].get("expected"),
               [{k: (str(v)[:16] if isinstance(v, str) else v) for k, v in plan.items()} for plan in plans])
    versions = {state.get("version") for state in story["drafts"]}
    mado.note("ADR-046", f"{label}: 초안 상태 버전 {versions} · 계획 {[(p.get('path'), p.get('mode')) for p in plans]}")

    resumed = story["resumed"]
    mado.check("ADR-046", f"{label}: 이어 가기가 끝까지 감", resumed.error is None, resumed.error)
    coder = [r for r in resumed.requests_by("coder") if "[결정 장부 갱신]" not in r.last_user()]
    expected = write_probe.APPLIED_RESULT if applied else write_probe.NOT_APPLIED_RESULT
    # 문구에서 `{path}` 바로 뒤, 다음 자리표시(`{absent}`) 앞까지가 두 결과를 가르는 말입니다.
    marker = expected.split("`{path}`")[1].split("{")[0][:22]
    told = [str(m.get("content") or "") for r in coder for m in r.messages if m.get("role") == "tool"
            and "[디스크 확인]" in str(m.get("content") or "")]
    mado.check("ADR-046", f"{label}: 모델이 받은 도구 결과가 `[결과 모름]` 이 아니라 디스크 확인 결과",
               bool(told) and all(marker in content for content in told),
               told[0][:300] if told else [str(m.get("content"))[:120] for r in coder[:1] for m in r.messages if m.get("role") == "tool"])

    final = (story["workspace"] / "notes.txt").read_text(encoding="utf-8")
    appends = [c for c in story["calls"] if c.tool_name == "sandbox__append_workspace_file"]
    mado.check("ADR-046", f"{label}: 턴이 끝난 뒤 그 줄은 파일에 정확히 한 번", final.count(APPEND_LINE) == 1, repr(final))
    after_resume = [c for c in appends if c.created_at > story["turn_before"].updated_at and c.status == "success"]
    if applied:
        mado.check("ADR-046", f"{label}: DeepSeek 가 '이미 반영됨' 을 듣고 같은 덧붙이기를 다시 하지 않음",
                   not after_resume, [(c.arguments, c.status) for c in after_resume])
    else:
        mado.check("ADR-046", f"{label}: DeepSeek 가 '디스크에 없음' 을 듣고 한 번 다시 호출함",
                   len(after_resume) == 1, [(c.arguments, c.status) for c in appends])
    turn = story["turns"][-1]
    mado.check("ADR-046", f"{label}: 턴이 completed 로 닫히고 재개 횟수가 1", turn.status == "completed" and turn.resumed_count == 1,
               f"{turn.status} · resumed {turn.resumed_count} · paused {turn.paused_seconds}s")
    speech = next((m for m in story["messages"] if m.sender_key == "coder" and _kind(m) == "speech"), None)
    resumed_at = ((speech.llm_meta or {}).get("resumed_at") if speech is not None else None)
    mado.check("ADR-048", f"{label}: 이어 간 발언에 `llm_meta.resumed_at` 이 남음 (집계가 재개 시점부터 세도록)",
               bool(resumed_at), (speech.llm_meta if speech is not None else None))
    mado.note("ADR-046", f"{label}: 덧붙이기 호출 기록 {[(c.status, c.created_at.strftime('%H:%M:%S')) for c in appends]} · "
                         f"최종 파일 {final!r} · 모델의 보고: {(speech.content[-200:] if speech else None)!r}")


@pytest.mark.adr(46)
def test_adr046_a_write_that_reached_the_disk_is_not_repeated(mado, cut_after):
    _probe_checks(mado, cut_after, "쓰인 뒤 끊김", applied=True)
    mado.settle()


@pytest.mark.adr(46)
def test_adr046_a_write_that_never_ran_is_reported_as_missing(mado, cut_before):
    _probe_checks(mado, cut_before, "쓰이기 전 끊김", applied=False)
    mado.settle()


# --------------------------------------------------------------------------- 커널 셀 다시 실행


KERNEL_PROMPT = (
    "sandbox 의 execute_python_code 를 **세 번에 나눠** 순서대로 실행하세요 (같은 namespace 로). 한 번에 합치지 마세요.\n"
    "1) `secret = 7919 * 13`\n"
    "2) `count = 5` 를 정의하고 `open('out.txt', 'w').write('written-by-cell')` 로 파일도 하나 씁니다\n"
    "3) `print(secret + count)`\n"
    "세 번째 실행이 출력한 숫자를 보고하세요. 변수가 없다는 오류가 나면 무엇이 없었는지와 어떻게 처리했는지도 적으세요."
)


async def _cut_kernel(mado, name: str, mode: str):
    """세 번째 셀 실행 직전에 끊고, 그 사이 유저가 out.txt 를 고친 뒤 `mode` 로 이어 갑니다."""
    sid = await mado.new_session(name, agents=["orchestrator", "coder"], max_rounds=1, orchestrator_hands_off=True)
    seen = {"runs": 0}

    def fault(moment, agent, tool, args):
        if tool != "sandbox__execute_python_code" or moment != "before":
            return False
        seen["runs"] += 1
        return seen["runs"] == 3

    crashed = await mado.crash_turn(sid, KERNEL_PROMPT, fault)
    workspace = mado.workspaces / name
    written = (workspace / "out.txt").read_text(encoding="utf-8") if (workspace / "out.txt").exists() else None
    await mado.restart_server()
    # 끊긴 사이 유저가 그 파일을 고쳤습니다. 다시 실행된 셀이 이 내용을 덮으면 안 됩니다 (§4).
    (workspace / "out.txt").write_text("edited-by-user-after-crash", encoding="utf-8")
    turn = (await mado.turns(sid))[-1]
    cells = await _kernel_cells(mado, sid, turn.id)
    resumed = await mado.resume(sid, turn.id, mode)
    return {
        "sid": sid, "crashed": crashed, "resumed": resumed, "workspace": workspace, "written": written,
        "cells": cells, "turn_before": turn, "runs_before_crash": seen["runs"],
        "turns": await mado.turns(sid), "messages": await mado.messages(sid), "calls": await mado.tool_calls(sid),
    }


async def _kernel_cells(mado, sid, turn_id):
    from app.orchestration import turns

    messages = [m for m in await mado.messages(sid) if m.turn_id == turn_id]
    async with mado.session_factory()() as db:
        return await turns.turn_kernel_cells(db, sid, turn_id, messages)


@pytest.fixture(scope="module")
def replayed(mado):
    result = mado.run(_cut_kernel(mado, "resume-kernel-replay", "replay"), timeout=2400)
    mado.dump("resume-kernel-replay", result["crashed"].requests + result["resumed"].requests)
    return result


@pytest.fixture(scope="module")
def continued(mado):
    result = mado.run(_cut_kernel(mado, "resume-kernel-continue", "continue"), timeout=2400)
    mado.dump("resume-kernel-continue", result["crashed"].requests + result["resumed"].requests)
    return result


def _kernel_preconditions(mado, story, label):
    mado.check("ADR-046", f"{label}: 세 번째 셀 직전에 끊김", story["crashed"].crashed and story["runs_before_crash"] == 3,
               f"crashed={story['crashed'].crashed} runs={story['runs_before_crash']} err={story['crashed'].error}")
    mado.check("ADR-046", f"{label}: 끊기기 전 두 번째 셀이 파일을 썼음", story["written"] == "written-by-cell", story["written"])
    cells = story["cells"]
    count = sum(len(v) for v in cells.values())
    mado.check("ADR-046", f"{label}: 다시 돌릴 셀을 기존 도구 기록(tool_calls)에서 골라냄 — 끝까지 실행된 두 셀",
               count == 2, {k: len(v) for k, v in cells.items()})


@pytest.mark.adr(46)
def test_adr046_replay_restores_variables_and_rolls_files_back(mado, replayed):
    from app.orchestration import kernel_replay

    _kernel_preconditions(mado, replayed, "셀 다시 실행")
    resumed = replayed["resumed"]
    mado.check("ADR-046", "셀 다시 실행: 이어 가기가 끝까지 감", resumed.error is None, resumed.error)
    cards = [m for m in replayed["messages"] if _kind(m) == "kernel_replay"]
    mado.check("ADR-046", "셀 다시 실행: 피드에 다시 실행 기록(kernel_replay)이 한 건 남음", len(cards) == 1,
               [(m.sender_key, m.content[:200]) for m in cards])
    out = (replayed["workspace"] / "out.txt").read_text(encoding="utf-8")
    mado.check("ADR-046", "셀 다시 실행: 다시 돈 셀이 덮은 파일이 유저가 고친 내용으로 되돌아옴",
               out == "edited-by-user-after-crash", out)
    coder = [r for r in resumed.requests_by("coder") if "[결정 장부 갱신]" not in r.last_user()]
    head = kernel_replay.KERNEL_REPLAYED_NOTE.split("{ran}")[0][:30]
    mado.check("ADR-046", "셀 다시 실행: 이어 가는 발언에게 '셀 N개를 다시 실행해 변수를 되살렸다' 고 알림",
               bool(coder) and head in coder[0].text(), coder[0].text()[-600:] if coder else None)
    after = [c for c in replayed["calls"] if c.tool_name == "sandbox__execute_python_code"
             and c.created_at > replayed["turn_before"].updated_at]
    printed = [c for c in after if "102952" in (c.output or "")]
    name_errors = [c for c in after if "NameError" in (c.output or "")]
    mado.check("ADR-046", "셀 다시 실행: 되살아난 변수로 세 번째 계산이 NameError 없이 끝남 (7919*13+5=102952)",
               bool(printed) and not name_errors,
               [(c.status, (c.output or "")[:160]) for c in after])
    speech = next((m for m in replayed["messages"] if m.sender_key == "coder" and _kind(m) == "speech"), None)
    mado.check("ADR-046", "셀 다시 실행: 최종 보고에 맞는 값이 실림", speech is not None and "102952" in speech.content.replace(",", ""),
               speech.content[-300:] if speech else None)
    replays = [c for c in after if c.message_id and cards and c.message_id == cards[0].id]
    mado.note("ADR-046", f"다시 실행 기록: {cards[0].content[:300] if cards else None!r} · 그 카드에 묶인 도구 기록 {len(replays)}건 · "
                         f"재개 뒤 실행 {[(c.status, (c.output or '')[:50]) for c in after]}")
    mado.settle()


@pytest.mark.adr(46)
def test_adr046_continue_tells_the_model_the_kernel_is_empty(mado, continued):
    from app.orchestration import kernel_replay

    _kernel_preconditions(mado, continued, "그대로 이어 가기")
    resumed = continued["resumed"]
    mado.check("ADR-046", "그대로 이어 가기: 이어 가기가 끝까지 감", resumed.error is None, resumed.error)
    mado.check("ADR-046", "그대로 이어 가기: 셀을 다시 돌리지 않음 (다시 실행 기록 없음)",
               not [m for m in continued["messages"] if _kind(m) == "kernel_replay"])
    out = (continued["workspace"] / "out.txt").read_text(encoding="utf-8")
    coder = [r for r in resumed.requests_by("coder") if "[결정 장부 갱신]" not in r.last_user()]
    mado.check("ADR-046", "그대로 이어 가기: 발언에게 '커널의 변수가 사라졌다' 고 미리 알림",
               bool(coder) and kernel_replay.KERNEL_LOST_NOTE[:30] in coder[0].text(), coder[0].text()[-500:] if coder else None)
    after = [c for c in continued["calls"] if c.tool_name == "sandbox__execute_python_code"
             and c.created_at > continued["turn_before"].updated_at]
    name_errors = [c for c in after if "NameError" in (c.output or "")]
    speech = next((m for m in continued["messages"] if m.sender_key == "coder" and _kind(m) == "speech"), None)
    correct = speech is not None and "102952" in speech.content.replace(",", "")
    mado.check("ADR-046", "그대로 이어 가기: 알림 덕에 모델이 변수를 다시 만들어 맞는 값을 냄", correct,
               speech.content[-300:] if speech else None)
    mado.note("ADR-046", f"그대로 이어 가기: NameError 를 겪은 실행 {len(name_errors)}회 · 재개 뒤 실행 {len(after)}회 · "
                         f"out.txt 최종 {out!r} (모델이 2번 셀을 다시 돌리면 유저 수정이 덮입니다)")
    mado.settle()
