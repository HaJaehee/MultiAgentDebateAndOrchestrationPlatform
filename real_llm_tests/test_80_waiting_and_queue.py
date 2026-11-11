"""사람과 자리(런타임 슬롯)를 기다리는 두 결정을, 화면이 쓰는 것과 같은 실행기(`DebateRunner`)로 실제 턴을 돌려 확인합니다.

* ADR-037 답을 기다리는 물음을 다른 탭·다른 대화에 알리기 — 정본은 실행기의 카드 목록, 전역 채널에는 대화 id 만,
  방문자의 턴은 섞지 않기. 실제 계획(DeepSeek 가 쓴)과 실제 도구 승인 요청으로 확인합니다.
* ADR-044 런타임 자리가 모자랄 때 줄 세우기·라운드 경계 양보·줄에서 기다리다 정지·체크포인트 보존 정리.
  자리 상한을 1 로 낮추고 서로 다른 작업 공간의 두 대화를 동시에 돌립니다.
"""

import asyncio
import dataclasses
import time

import pytest

from harness import git

FLAGGED_CODE = "import subprocess, sys; print(subprocess.run([sys.executable, '--version'], capture_output=True, text=True).stdout)"


async def _until(predicate, timeout: float, step: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    return None


def _drain(queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def _finish(run, timeout: float) -> None:
    if run.task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(run.task), timeout=timeout)
        except Exception:  # noqa: BLE001 - 결과는 DB 와 run.status 로 봅니다
            pass


# --------------------------------------------------------------------------- ADR-037


@pytest.fixture(scope="module")
def waiting(mado):
    from app.events import WAITING_CHANNEL
    from app.orchestration.runner import get_debate_runner

    async def go():
        runner = get_debate_runner()
        signals = runner.event_bus.subscribe(WAITING_CHANNEL)
        owner = await mado.new_session("waiting-owner", agents=["orchestrator", "coder"], max_rounds=1)
        guest = await mado.new_session("waiting-guest", agents=["orchestrator", "architect"], max_rounds=1)
        log = {}

        # ---- 주인의 턴: 계획 승인 → 도구 승인
        run = runner.start(
            owner,
            "coder 는 sandbox 의 execute_python_code 로 아래 코드를 **글자 그대로 한 번** 실행하고 출력된 파이썬 버전을 보고하세요. "
            f"다른 방법으로 바꾸지 마세요.\n```python\n{FLAGGED_CODE}\n```",
            str(mado.workspaces / "waiting-owner"), attended=True,
        )
        plan_wait = await _until(lambda: [w for w in runner.waiting_by_session().get(owner, []) if w.kind == "plan"], 300)
        log["plan_wait"] = plan_wait
        log["plan_card"] = dict(run.plan_approval or {})
        log["signals_at_plan"] = _drain(signals)

        # ---- 방문자의 턴: 같은 서버에서 돌지만 주인에게 알리지 않음
        guest_run = runner.start(
            guest, "서비스 헬스 체크 주기를 10초와 30초 중 무엇으로 할지 한 문단으로 정해 주세요. 도구는 쓰지 마세요.",
            str(mado.workspaces / "waiting-guest"), attended=True, announce_waiting=False,
        )
        guest_card = await _until(lambda: guest_run.plan_approval, 300)
        log["guest_card"] = bool(guest_card)
        log["guest_own_view"] = [dataclasses.asdict(w) for w in guest_run.waiting_requests()]
        log["by_session_with_guest_pending"] = {k: [dataclasses.asdict(w) for w in v]
                                                for k, v in runner.waiting_by_session().items()}
        log["signals_for_guest"] = [s for s in _drain(signals) if s.get("session_id") == guest]

        # ---- 주인이 계획을 승인
        log["approved"] = runner.resolve_plan_approval(owner, plan_wait[0].id, "approve", approver="local") if plan_wait else False
        tool_wait = await _until(lambda: [w for w in runner.waiting_by_session().get(owner, []) if w.kind == "tool"], 600)
        log["tool_wait"] = tool_wait
        log["tool_card"] = dict(run.approvals[0]) if run.approvals else {}
        log["signals_at_tool"] = _drain(signals)
        if tool_wait:
            log["tool_allowed"] = runner.resolve_tool_approval(owner, tool_wait[0].id, "allow_once", approver="local")
        if guest_card:
            runner.resolve_plan_approval(guest, guest_card["id"], "approve", approver="guest")
        await _finish(run, 900)
        await _finish(guest_run, 900)
        log["after"] = {k: [dataclasses.asdict(w) for w in v] for k, v in runner.waiting_by_session().items()}
        log["signals_at_end"] = _drain(signals)
        log["statuses"] = (run.status, guest_run.status)
        runner.event_bus.unsubscribe(WAITING_CHANNEL, signals)
        return {"owner": owner, "guest": guest, "log": log,
                "calls": await mado.tool_calls(owner), "turns": await mado.turns(owner),
                "guest_turns": await mado.turns(guest)}

    return mado.run(go(), timeout=2400)


@pytest.mark.adr(37)
def test_adr037_a_pending_plan_is_announced_with_only_the_session_id(mado, waiting):
    log, owner = waiting["log"], waiting["owner"]
    plan_wait = log["plan_wait"] or []
    mado.check("ADR-037", "계획 승인을 기다리는 동안 `waiting_by_session()` 에 그 대화가 kind=plan 으로 뜸", len(plan_wait) == 1,
               plan_wait)
    signals = log["signals_at_plan"]
    mado.check("ADR-037", "전역 채널에는 대화 id 만 실린 신호가 옴", bool(signals) and all(s == {"session_id": owner} for s in signals),
               signals[:3])
    if plan_wait:
        fields = dataclasses.asdict(plan_wait[0])
        mado.check("ADR-037", "알림에 쓰는 값은 id·종류·에이전트 이름·도구 이름뿐 (계획 본문 없음)",
                   set(fields) == {"id", "kind", "agent_name", "tool_name"} and fields["id"] == log["plan_card"].get("id"),
                   fields)
        body = str(log["plan_card"].get("content") or log["plan_card"].get("plan") or "")[:40]
        mado.check("ADR-037", "정본은 실행기의 카드 — 카드에는 계획이 있고 알림 값에는 없음",
                   not any(body and body in str(v) for v in fields.values()), sorted(log["plan_card"].keys()))
    mado.check("ADR-037", "승인하면 물음이 걷힘", log["approved"] is True, log["approved"])
    mado.settle()


@pytest.mark.adr(37)
def test_adr037_a_tool_approval_announces_the_tool_name_but_not_its_arguments(mado, waiting):
    log = waiting["log"]
    tool_wait = log["tool_wait"] or []
    mado.check("ADR-037", "검사에 걸린 코드 실행이 도구 승인 물음으로 뜸 (kind=tool)", len(tool_wait) >= 1,
               f"{tool_wait} · 도구 기록 {[(c.tool_name, c.status, c.decision, c.rule) for c in waiting['calls']]}")
    if tool_wait:
        fields = dataclasses.asdict(tool_wait[0])
        mado.check("ADR-037", "알림 값에 도구 이름까지만 있음", fields["tool_name"] == "sandbox__execute_python_code", fields)
        mado.check("ADR-037", "알림 값에 코드(도구 인자)가 실리지 않음", not any("subprocess" in str(v) for v in fields.values()), fields)
        mado.check("ADR-037", "카드(정본)에는 인자가 있음 — 화면은 거기서 읽음", "subprocess" in str(log["tool_card"]),
                   sorted(log["tool_card"].keys()))
        mado.check("ADR-037", "도구 승인 요청 때도 전역 채널에 신호가 옴", bool(log["signals_at_tool"]), log["signals_at_tool"][:3])
        runs = [c for c in waiting["calls"] if c.tool_name == "sandbox__execute_python_code"]
        mado.check("ADR-037", "'이번만 허용' 뒤 그 호출이 실제로 실행됨", any(c.status == "success" and c.approver for c in runs),
                   [(c.status, c.decision, c.rule, c.approver, (c.output or "")[:80]) for c in runs])
    mado.check("ADR-037", "턴이 끝나면 기다리는 물음이 없음", not log["after"], log["after"])
    mado.check("ADR-037", "두 턴 모두 끝남", log["statuses"] == ("completed", "completed"), log["statuses"])
    mado.settle()


@pytest.mark.adr(37)
def test_adr037_a_guest_turn_does_not_reach_the_owners_alerts(mado, waiting):
    log, guest = waiting["log"], waiting["guest"]
    mado.check("ADR-037", "방문자 턴도 계획 승인을 기다림 (그 방문자의 화면에는 보임)",
               log["guest_card"] and len(log["guest_own_view"]) == 1, log["guest_own_view"])
    mado.check("ADR-037", "주인의 `waiting_by_session()` 집계에는 방문자 대화가 없음",
               guest not in log["by_session_with_guest_pending"], sorted(log["by_session_with_guest_pending"]))
    mado.check("ADR-037", "전역 채널에 방문자 대화의 신호가 한 번도 나가지 않음",
               not log["signals_for_guest"] and guest not in {s.get("session_id") for s in log["signals_at_end"]},
               log["signals_for_guest"])
    mado.settle()


# --------------------------------------------------------------------------- ADR-044


@pytest.fixture(scope="module")
def queued(mado):
    from app.mcp.pool import get_runtime_pool
    from app.orchestration.runner import get_debate_runner

    async def go():
        runner, pool = get_debate_runner(), get_runtime_pool()
        # 앞선 시나리오가 남긴 유휴 런타임을 내리고 시작합니다 (상한을 낮추기 전의 것이 표본에 섞이지 않게).
        await pool.shutdown_all()
        saved = (pool.max_runtimes, pool.yield_after)
        pool.max_runtimes, pool.yield_after = 1, 20.0
        first = await mado.new_session("queue-first", agents=["orchestrator", "architect", "critic"], max_rounds=2,
                                       converge_early=False)
        second = await mado.new_session("queue-second", agents=["orchestrator", "architect"], max_rounds=1)
        third = await mado.new_session("queue-stopped", agents=["orchestrator", "architect"], max_rounds=1)
        log = {"events": {first: [], second: [], third: []}, "live": [], "waiting_seen": [], "over": []}
        try:
            run1 = runner.start(first, "API 응답에 ETag 를 붙일지 정해 주세요. 도구는 쓰지 말고 각 발언은 6줄 이내로.",
                                str(mado.workspaces / "queue-first"), attended=False)
            await _until(lambda: pool.status(), 120)
            run2 = runner.start(second, "로그 보존 기간을 30일과 90일 중 무엇으로 할지 정해 주세요. 도구는 쓰지 말고 6줄 이내로.",
                                str(mado.workspaces / "queue-second"), attended=False)
            run3 = runner.start(third, "이 요청은 줄에서 기다리다 정지됩니다.", str(mado.workspaces / "queue-stopped"),
                                attended=False)
            queues = {sid: run.subscribe() for sid, run in ((first, run1), (second, run2), (third, run3))}

            async def watch():
                while True:
                    status = pool.status()
                    # 프로세스가 실제로 떠 있는 런타임만 셉니다. 기본 작업 공간의 빈 매니저(`default_runtime`)는 프로세스가 없습니다.
                    started = {key: info for key, info in status.items() if info.get("initialized")}
                    log["live"].append(len(started))
                    if len(status) > 1 and len(log["over"]) < 12:
                        log["over"].append([(key.replace(chr(92), "/").rsplit("/", 1)[-1], info.get("initialized"),
                                             len(info.get("holders") or [])) for key, info in status.items()])
                    waiters = pool.waiting()
                    if waiters:
                        log["waiting_seen"].append([(w["holder"], w["waited_seconds"]) for w in waiters])
                    for sid, queue in queues.items():
                        for event in _drain(queue):
                            if event.get("type") in ("runtime_queued", "turn_opened", "round_started", "turn_completed", "run_finished"):
                                log["events"][sid].append((event.get("type"), event.get("ahead"), event.get("round")))
                    await asyncio.sleep(0.3)

            watcher = asyncio.ensure_future(watch())
            await _until(lambda: any(w["holder"] == third for w in pool.waiting()), 60)
            log["stop_requested"] = runner.request_stop(third)
            await _finish(run3, 120)
            log["third_status"] = (run3.status, run3.status_text)
            await _finish(run2, 1500)
            await _finish(run1, 1500)
            await asyncio.sleep(1.0)
            watcher.cancel()
            log["statuses"] = (run1.status, run2.status)
        finally:
            pool.max_runtimes, pool.yield_after = saved
        return {"first": first, "second": second, "third": third, "log": log,
                "turns": {sid: await mado.turns(sid) for sid in (first, second, third)},
                "messages": {sid: await mado.messages(sid) for sid in (first, second, third)}}

    return mado.run(go(), timeout=3600)


@pytest.mark.adr(44)
def test_adr044_a_second_workspace_waits_in_line_instead_of_failing(mado, queued):
    log, first, second = queued["log"], queued["first"], queued["second"]
    mado.check("ADR-044", "자리가 하나뿐이어도 두 대화의 턴이 모두 끝남", log["statuses"] == ("completed", "completed"),
               f"{log['statuses']} · 턴 {[(t.status, t.error) for sid in (first, second) for t in queued['turns'][sid]]}")
    queued_events = [e for e in log["events"][second] if e[0] == "runtime_queued"]
    mado.check("ADR-044", "두 번째 대화가 실패하지 않고 대기 순번 이벤트(runtime_queued)를 받음", bool(queued_events),
               log["events"][second][:6])
    mado.check("ADR-044", "기다리는 동안 `pool.waiting()` (= /api/mcp 의 waiting)에 그 대화가 보임",
               any(any(holder == second for holder, _ in sample) for sample in log["waiting_seen"]),
               log["waiting_seen"][:3])
    mado.check("ADR-044", "프로세스가 떠 있는 런타임이 상한(1)을 넘은 적이 없음", max(log["live"], default=0) <= 1,
               f"최대 {max(log['live'], default=0)} · 표본 {len(log['live'])}개 · 목록이 둘 이상이던 순간 {log['over'][:6]}")
    mado.note("ADR-044", f"풀 목록에 항목이 둘 이상이던 순간 (작업 공간, 떠 있음, 빌린 수): {log['over'][:6] or '없음'}")
    mado.settle()


@pytest.mark.adr(44)
def test_adr044_the_running_turn_yields_at_a_round_boundary(mado, queued):
    log, first, second = queued["log"], queued["first"], queued["second"]
    events = log["events"][first]
    opened = next((i for i, e in enumerate(events) if e[0] == "turn_opened"), None)
    yielded = [e for e in events[(opened or 0) + 1:] if e[0] == "runtime_queued"] if opened is not None else []
    order = [(sid == first and "first" or "second", m.created_at) for sid in (first, second)
             for m in queued["messages"][sid] if (m.turn_meta or {}).get("kind") == "synthesis"]
    order.sort(key=lambda item: item[1])
    mado.check("ADR-044", "먼저 돌던 턴이 라운드 경계에서 자리를 내주고 다시 줄을 섬 (턴 도중 runtime_queued)", bool(yielded), events)
    mado.check("ADR-044", "양보 덕에 늦게 온 짧은 턴이 먼저 온 긴 턴보다 먼저 끝남", [name for name, _ in order] == ["second", "first"],
               [(name, at.strftime("%H:%M:%S")) for name, at in order])
    refs = git(mado.workspaces / "queue-first", "for-each-ref", "--format=%(refname)",
               f"refs/mado/turns/{queued['turns'][first][-1].id}/")
    names = sorted(ref.rsplit("/", 1)[-1] for ref in refs.splitlines() if ref)
    mado.check("ADR-043", "양보했다 돌아온 턴도 라운드 경계 스냅샷이 빠짐없이 남음",
               all(name in names for name in ("round-1", "round-2", "debate-end")), names)
    mado.note("ADR-044", f"먼저 온 턴의 이벤트 {events} · 대기 표본 {log['waiting_seen'][:2]}…{log['waiting_seen'][-1:] }")
    mado.settle()


@pytest.mark.adr(44)
def test_adr044_stopping_while_in_line_leaves_no_turn_behind(mado, queued):
    log, third = queued["log"], queued["third"]
    mado.check("ADR-044", "줄에서 기다리는 턴에 정지를 요청할 수 있음", log.get("stop_requested") is True, log.get("stop_requested"))
    mado.check("ADR-044", "그 턴은 시작되지 않고 턴 기록·발언을 남기지 않음 (입력을 되돌려 줄 수 있게)",
               not queued["turns"][third] and not queued["messages"][third],
               f"턴 {len(queued['turns'][third])} · 발언 {len(queued['messages'][third])} · 실행 상태 {log.get('third_status')}")
    mado.settle()


@pytest.mark.adr(44)
def test_adr044_expired_checkpoints_are_dropped_db_first(mado, queued):
    """보존 기준을 0 으로 좁혀 방금 끝난 턴을 "만료" 로 만들고, 유휴 훅이 부르는 것과 같은 정리를 돌립니다."""
    from app import session_ops, workspace_checkpoint

    first = queued["first"]
    workspace = mado.workspaces / "queue-first"
    turn_id = queued["turns"][first][-1].id
    before = git(workspace, "for-each-ref", "--format=%(refname)", f"refs/mado/turns/{turn_id}/").splitlines()
    kept_default = workspace_checkpoint.expired_turns(workspace)
    mado.check("ADR-044", "기본 보존 기준(200턴·30일)에서는 방금 끝난 턴이 만료되지 않음", turn_id not in kept_default, kept_default)
    expired = workspace_checkpoint.expired_turns(workspace, keep_turns=0, keep_days=0)
    mado.check("ADR-044", "두 기준을 모두 넘긴 턴만 만료로 골라짐", turn_id in expired, expired)

    async def drop():
        async with mado.session_factory()() as db:
            await session_ops.expire_checkpoints(db, [turn_id])
        await asyncio.to_thread(workspace_checkpoint.forget, workspace, [turn_id])
        await asyncio.to_thread(workspace_checkpoint.collect_garbage, workspace)
        return (await mado.turns(first))[-1]

    turn = mado.run(drop(), timeout=300)
    after = git(workspace, "for-each-ref", "--format=%(refname)", f"refs/mado/turns/{turn_id}/").splitlines()
    checkpoint = turn.checkpoint or {}
    mado.check("ADR-044", "턴 기록에서 start·end 해시가 지워지고 변경 파일 수는 남음",
               "start" not in checkpoint and "end" not in checkpoint and "changed" in checkpoint, checkpoint)
    mado.check("ADR-044", "그 턴의 스냅샷 ref (라운드 경계 포함)가 모두 지워짐", bool(before) and not after,
               f"{len(before)} → {len(after)}")
    mado.check("ADR-044", "정리된 턴은 변경 내역을 볼 수 없다고 답함", session_ops.reviewable_changes(checkpoint) is None, checkpoint)
    mado.settle()
