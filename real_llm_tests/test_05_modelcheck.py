"""모델 적합성 점검 (ADR-054) — 대상 conf.json의 연결마다 실제로 점검을 실행해, 검사가 끝까지 진행되고 결과가 저장되는지 확인합니다.

무엇이 통과하고 무엇이 주의인지는 **모델과 서버의 특성에 따라 달라지므로** 실패로 처리하지 않고 관찰 기록으로 남깁니다. 여기서
확인하는 것은 점검 자체의 규칙입니다: 응답하는 모델에서 첫 검사가 실패로 나오지 않고, 검사가 모두 한 줄씩
결과를 내며, 기록이 실행 폴더에 저장되고, 요청 수가 정해진 범위 안에 있는지 검증합니다.

생성형 연결 하나에 요청 8~9건(짧은 응답), 결정 LLM은 1건입니다.
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
            mado.check("ADR-054", f"{profile.model}: 검사 10개가 순서대로 한 줄씩 결과를 생성함",
                       [r.key for r in profile.results] == SYSTEM2_KEYS, [r.key for r in profile.results])
            mado.check("ADR-054", f"{profile.model}: 응답하는 연결의 첫 검사는 실패가 아님",
                       outcomes.get("answer") != mc.FAIL, profile.results[0].summary)
            mado.check("ADR-054", f"{profile.model}: 요청 수는 8~9건 (재시도 없음)", 8 <= sent <= 9, sent)
        else:
            mado.check("ADR-054", f"{profile.model}: 결정 LLM은 요청 1건에 4개 항목 출력",
                       sent == 1 and len(profile.results) == 4, (sent, [r.key for r in profile.results]))
            mado.check("ADR-054", f"{profile.model}: 결정 LLM이 응답함", outcomes.get("systemone") == mc.OK,
                       profile.results[0].summary)

        path = mc.save_profile(profile)
        mado.check("ADR-054", f"{profile.model}: 기록이 실행 폴더의 data/modelcheck에 저장됨",
                   path is not None and path.exists() and str(mado.run_dir) in str(path.resolve()), path)
        reread = mc.load_profile(profile.target_id)
        mado.check("ADR-054", f"{profile.model}: 기록을 다시 읽으면 동일한 결과 반환", reread == profile, reread)
        key = (target.api_key or "").strip()
        if key and path is not None:
            mado.check("ADR-054", f"{profile.model}: 기록에 API 키가 포함되지 않음", key not in path.read_text(encoding="utf-8"), path)
    mado.settle()
