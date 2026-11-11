"""모델 적합성 점검 (ADR-054) — 대상 conf 의 연결마다 실제로 점검을 돌려, 검사가 끝까지 가고 결과가 남는지 봅니다.

무엇이 통과하고 무엇이 주의인지는 **모델과 서버에 달린 것**이라 실패로 만들지 않고 관찰로 남깁니다. 여기서
확인하는 것은 점검 자체의 규칙입니다: 연결이 되는 모델에서 첫 요청이 실패로 나오지 않고, 검사가 모두 한 줄씩
결과를 내며, 기록이 실행 폴더에 남고, 요청 수가 정해진 범위 안인가.

생성형 연결 하나에 요청 8~9건(짧은 답), 결정 LLM 은 1건입니다.
"""

import pytest

pytestmark = [pytest.mark.adr(54), pytest.mark.acceptance]

SYSTEM2_KEYS = ["answer", "reasoning", "stream", "length", "roles", "tools", "tool_result", "catalog", "image", "window"]


def _targets():
    from app.agents.model_status import collect_targets
    from app.config import get_config

    config = get_config()
    return config, [t for t in collect_targets(config) if t.is_live]


def test_every_live_connection_gets_a_full_profile(mado):
    from app.agents import model_check as mc

    config, targets = _targets()
    mado.check("ADR-054", "점검할 연결이 하나 이상 있음", bool(targets), [t.model for t in targets])
    for target in targets:
        before_llm, before_s1 = len(mado.requests), len(mado.decisions)
        profile = mado.run(mc.check_target(target, config), timeout=900)
        sent = (len(mado.requests) - before_llm) + (len(mado.decisions) - before_s1)
        outcomes = {r.key: r.outcome for r in profile.results}
        mado.note("ADR-054", f"{profile.model} [{profile.kind}] {mc.summarize(profile)} · 요청 {sent}건 · {outcomes}")
        for result in profile.results:
            if result.outcome in (mc.WARN, mc.FAIL, mc.SKIP) or result.suggestions:
                mado.note("ADR-054", f"{profile.model} · {result.title}: {result.summary}"
                                     f"{' → ' + ' / '.join(s.text for s in result.suggestions) if result.suggestions else ''}")

        if profile.kind == mc.SYSTEM2:
            mado.check("ADR-054", f"{profile.model}: 검사 10개가 차례대로 한 줄씩 결과를 냄",
                       [r.key for r in profile.results] == SYSTEM2_KEYS, [r.key for r in profile.results])
            mado.check("ADR-054", f"{profile.model}: 응답하는 연결의 첫 검사는 실패가 아님",
                       outcomes.get("answer") != mc.FAIL, profile.results[0].summary)
            mado.check("ADR-054", f"{profile.model}: 요청은 8~9건 (재시도 없음)", 8 <= sent <= 9, sent)
        else:
            mado.check("ADR-054", f"{profile.model}: 결정 LLM 은 요청 한 번에 네 줄",
                       sent == 1 and len(profile.results) == 4, (sent, [r.key for r in profile.results]))
            mado.check("ADR-054", f"{profile.model}: 결정 LLM 이 답함", outcomes.get("systemone") == mc.OK,
                       profile.results[0].summary)

        path = mc.save_profile(profile)
        mado.check("ADR-054", f"{profile.model}: 기록이 실행 폴더의 data/modelcheck 에 남음",
                   path is not None and path.exists() and str(mado.run_dir) in str(path.resolve()), path)
        reread = mc.load_profile(profile.target_id)
        mado.check("ADR-054", f"{profile.model}: 기록을 다시 읽으면 같은 결과", reread == profile, reread)
        key = (target.api_key or "").strip()
        if key and path is not None:
            mado.check("ADR-054", f"{profile.model}: 기록에 키가 없음", key not in path.read_text(encoding="utf-8"), path)
    mado.settle()
