"""ADR-042: 번들에 들어 있는 예제 플러그인을 플러그인 폴더에 놓고(하네스가 시작 전에 복사) 실제 턴을 돌립니다.

* 토론 전략 플러그인 `consensus_voting` — 라운드마다 발언 순서와 지시가 플러그인이 정한 대로 가는지
* 호스트 도구 플러그인 `glossary`(등급 read) — 켠 에이전트에게만 실리고, 도구 보안 판정을 거쳐 그냥 실행되는지
* 호스트 도구 플러그인 `ticket`(등급을 밝히지 않음) — 무인 턴에서 거부되는지
"""

import pytest


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


@pytest.fixture(scope="module")
def plugged(mado):
    async def go():
        sid = await mado.new_session(
            "plugins", agents=["orchestrator", "rt_plugin_user", "critic"], strategy="consensus_voting",
            max_rounds=2, converge_early=False,
        )
        outcome = await mado.turn(
            sid,
            "사내 문서에서 'MADO' 라는 용어를 어떻게 풀어 쓸지 정합니다. Plugin User 는 glossary 도구로 MADO 의 뜻을 확인해 "
            "그 뜻을 그대로 인용하고, ticket 도구로 '용어집 보강' 이라는 제목의 티켓도 하나 만들어 보세요 "
            "(만들지 못하면 못 했다고 적으세요). critic 은 도구 없이 표기안을 검토하세요. 각 발언은 8줄 이내.",
        )
        return sid, outcome

    sid, outcome = mado.run(go(), timeout=2400)
    mado.dump("plugins", outcome.requests)
    return {"sid": sid, "outcome": outcome, "workspace": mado.workspaces / "plugins",
            "messages": mado.run(mado.messages(sid)), "calls": mado.run(mado.tool_calls(sid))}


@pytest.mark.adr(42)
def test_adr042_a_strategy_plugin_drives_the_rounds(mado, plugged):
    outcome, messages = plugged["outcome"], plugged["messages"]
    mado.check("ADR-042", "플러그인 전략으로 턴이 끝남", outcome.error is None, outcome.error)
    speeches = [m for m in messages if _kind(m) == "speech"]
    first = [m.sender_key for m in speeches if m.round_number == 1]
    second = [m.sender_key for m in speeches if m.round_number == 2]
    mado.check("ADR-042", "2라운드의 발언 순서가 1라운드의 역순 (플러그인이 정한 규칙)",
               len(first) == 2 and second == list(reversed(first)), f"1R {first} · 2R {second}")
    requests = [r for r in outcome.requests if r.agent in ("rt_plugin_user", "critic") and "[결정 장부 갱신]" not in r.last_user()]
    proposals = [r for r in requests if "[제안]" in r.last_user()]
    votes = [r for r in requests if "[투표]" in r.last_user()]
    mado.check("ADR-042", "1라운드에는 플러그인의 [제안] 지시가, 2라운드에는 [투표] 지시가 실림",
               bool(proposals) and bool(votes), f"제안 {len(proposals)} · 투표 {len(votes)}")
    mado.settle()


@pytest.mark.adr(42)
def test_adr042_host_tool_plugins_are_offered_only_where_enabled_and_gated(mado, plugged):
    outcome = plugged["outcome"]
    user = [r for r in outcome.requests_by("rt_plugin_user") if "[결정 장부 갱신]" not in r.last_user()]
    critic = [r for r in outcome.requests_by("critic") if "[결정 장부 갱신]" not in r.last_user()]
    mado.check("ADR-042", "켠 에이전트의 요청에 `glossary__lookup` 이 전체 이름으로 실림",
               bool(user) and "glossary__lookup" in user[0].tool_names, user[0].tool_names if user else None)
    mado.check("ADR-042", "켜지 않은 에이전트(critic)의 요청에는 플러그인 도구가 없음",
               bool(critic) and not [n for r in critic for n in r.tool_names if n.startswith(("glossary", "ticket"))],
               critic[0].tool_names if critic else None)
    mado.check("ADR-042", "플러그인의 지침이 그 도구를 가진 발언의 시스템 프롬프트에 실림",
               bool(user) and "[용어집]" in user[0].system(), user[0].system()[-300:] if user else None)
    mado.check("ADR-042", "지침은 도구가 없는 발언에는 실리지 않음", bool(critic) and "[용어집]" not in critic[0].system())

    calls = plugged["calls"]
    lookups = [c for c in calls if c.tool_name == "glossary__lookup"]
    mado.check("ADR-042", "등급을 read 로 밝힌 플러그인 도구는 확인 없이 실행됨",
               any(c.status == "success" and "Multi-Agent Debate" in (c.output or "") for c in lookups),
               [(c.tool_name, c.status, c.decision, c.risk, (c.output or "")[:80]) for c in calls])
    mado.check("ADR-042", "그 호출도 도구 보안 판정을 거쳐 기록됨 (decision·risk 가 비어 있지 않음)",
               bool(lookups) and all(c.decision and c.risk for c in lookups),
               [(c.decision, c.risk, c.rule) for c in lookups])
    tickets = [c for c in calls if c.tool_name == "ticket__create"]
    if tickets:
        mado.check("ADR-042", "등급을 밝히지 않은 플러그인 도구는 무인 턴에서 거부됨",
                   all(c.status == "denied" for c in tickets), [(c.status, c.decision, c.risk, c.rule, c.approver) for c in tickets])
        mado.check("ADR-042", "거부된 호출은 파일을 만들지 않음", not (plugged["workspace"] / "tickets.md").exists())
    offered = bool(user) and "ticket__create" in user[0].tool_names
    mado.note("ADR-042", f"ticket__create 가 무인 턴의 도구 목록에 실렸는가: {offered} · 호출 "
                         f"{[(c.status, c.risk, c.rule, c.approver) for c in tickets] or '모델이 부르지 않음'}")
    answer = next((m for m in plugged["messages"] if m.sender_key == "rt_plugin_user" and _kind(m) == "speech"), None)
    if answer is not None and tickets:
        mado.check("ADR-014", "만들지 못한 티켓을 만들었다고 적지 않음",
                   not any(phrase in answer.content for phrase in ("티켓을 만들었습니다", "티켓 생성 완료", "티켓을 생성했습니다")),
                   answer.content[:400])
    mado.settle()
