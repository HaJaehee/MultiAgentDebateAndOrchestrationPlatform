"""Bench 의 자동 채점 (ADR-055) — 작은 구현 시나리오 하나를 실제 모델로 돌려, 검사와 채점이 끝까지 가는지 봅니다.

* 결정적 검사: 모델이 만든 스크립트가 있는가, 실제로 돌아 맞는 합계를 내는가 (작업 공간에서 직접 실행).
* 채점: 생성형 판정자가 JSON 한 줄로 답하는가, 판정자에게 변형의 이름이 가지 않는가. 결정 LLM 이 있으면 같은 결론을
  `score` 질문으로도 채점해 봅니다.
* 같은 실행을 자기 자신과 견주면 나빠진 것이 없어야 합니다.

모델이 스크립트를 실제로 만드는지, 판정자가 몇 점을 주는지는 **모델의 선택**이라 관찰로 남깁니다. 확인하는 것은 Bench 의
규칙입니다: 검사마다 결과가 남고, 못 읽은 점수는 지어내지 않고, 보고서에 열이 있는가.
"""

import json

import pytest

pytestmark = pytest.mark.adr(55)

LABEL = "순차 1라운드 XQZ"   # 판정자에게 새면 눈에 띄는 이름
CSV = "region,month,amount\n서울,1,100\n서울,2,250\n부산,1,80\n부산,2,120\n"


@pytest.fixture(scope="module")
def ran(mado):
    from app import bench

    folder = mado.run_dir / "bench-scenario"
    (folder / "seed").mkdir(parents=True, exist_ok=True)
    (folder / "seed" / "sales.csv").write_text(CSV, encoding="utf-8")
    scenario = {
        "name": "실제 채점", "seed": "seed",
        "prompt": (
            "작업 공간의 sales.csv 를 읽어 지역(region)별 amount 합계를 한 줄에 하나씩 `지역 합계` 형식으로 출력하는 "
            "파이썬 스크립트 summarize.py 를 작업 공간에 작성하세요. 표준 라이브러리만 쓰고, 작성한 뒤 한 번 실행해 "
            "결과를 확인하세요. 다른 파일은 만들지 마세요."
        ),
        "variants": [{"label": LABEL, "strategy": "sequential_debate", "max_rounds": 1,
                      "agents": ["orchestrator", "rt_researcher"]}],
        "checks": [
            {"name": "스크립트가 있음", "file_exists": "summarize.py"},
            {"name": "스크립트가 돌아 서울 합계 350 을 냄", "command": {"argv": ["{python}", "summarize.py"], "stdout_contains": "350"}},
            {"name": "결론이 합계를 말함", "conclusion_contains": {"regex": "350|200"}},
        ],
        "rubric": {
            "judge": "rt_reader",
            "criteria": [
                {"name": "요청 충족", "question": "요청한 스크립트를 작성하고 실행 결과를 확인했는가?"},
                {"name": "간결함", "question": "요청하지 않은 일을 하지 않고 간결하게 끝냈는가?"},
            ],
        },
    }
    path = folder / "scenario.json"
    path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    before = len(mado.requests)
    run_dir = mado.run(bench.run(path, out_dir=mado.run_dir / "bench-out"), timeout=2400)
    requests = mado.requests[before:]
    mado.dump("bench-scoring", requests)
    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    return {"run_dir": run_dir, "payload": payload, "variant": payload["variants"][0], "requests": requests,
            "scenario": bench.load_scenario(path)}


def test_adr055_every_check_leaves_a_verdict_and_the_report_shows_them(mado, ran):
    variant = ran["variant"]
    mado.check("ADR-055", "변형이 끝남", variant["status"] == "completed", variant.get("error"))
    checks = variant.get("checks") or []
    mado.check("ADR-055", "검사 3개가 모두 결과(참/거짓)와 사유를 남김",
               len(checks) == 3 and all(isinstance(c.get("ok"), bool) and c.get("detail") for c in checks), checks)
    workspace = next((ran["run_dir"] / "workspaces").iterdir())
    exists = (workspace / "summarize.py").is_file()
    by_name = {c["name"]: c for c in checks}
    mado.check("ADR-055", "'스크립트가 있음' 의 판정이 실제 파일 유무와 같음", by_name["스크립트가 있음"]["ok"] == exists, exists)
    report = (ran["run_dir"] / "report.md").read_text(encoding="utf-8")
    mado.check("ADR-055", "보고서에 검사·채점 열과 검사별 줄이 있음",
               "| 검사 | 채점 |" in report and "스크립트가 있음 —" in report, report[:600])
    mado.note("ADR-055", f"{mado.s2}: 검사 {[(c['name'], c['ok'], c['detail'][:80]) for c in checks]} · "
                         f"도구 문제 {variant.get('tool_problems')} · 변경 파일 {variant.get('files_changed')} · {variant.get('seconds')}초")
    mado.settle()


def test_adr055_the_generative_judge_is_blind_and_never_gets_a_made_up_score(mado, ran):
    rubric = ran["variant"].get("rubric") or {}
    judge_requests = [r for r in ran["requests"] if "당신은 결과물을 채점하는 판정자입니다" in r.text()]
    # 요청 수는 모델에 달렸습니다. 추론이 응답 한도를 다 쓰면 본문이 비어, 앱이 답을 한 번 더 청합니다
    # (`_recover_empty_answer`). 2026-11-11 deepseek-v4-flash 에서 그렇게 두 번 나갔습니다.
    mado.check("ADR-055", "판정 요청이 나감", len(judge_requests) >= 1, len(judge_requests))
    leaked = [r.index for r in judge_requests if LABEL in r.text() or "sequential_debate" in r.text()]
    mado.check("ADR-055", "판정자에게 변형의 이름·전략이 가지 않음", not leaked, leaked)
    with_tools = [(r.index, r.tool_names) for r in judge_requests if r.tool_names]
    mado.check("ADR-055", "판정자는 도구 없이 답만 함", not with_tools, with_tools)
    mado.note("ADR-055", f"판정 요청 {len(judge_requests)}건 (본문이 비어 다시 청한 것 포함)")
    score = rubric.get("score")
    if score is None:
        mado.check("ADR-055", "점수를 읽지 못했으면 사유를 남기고 점수는 비움", bool(rubric.get("error")), rubric)
        mado.note("ADR-055", f"{mado.s2} 판정자가 읽을 수 있는 답을 내지 않음: {rubric.get('error')} · "
                             f"원문 {(judge_requests[0].response_text or '')[:200] if judge_requests else ''}")
    else:
        mado.check("ADR-055", "점수는 0~1 이고 기준 둘의 단계가 모두 있음",
                   0.0 <= score <= 1.0 and set(rubric.get("scores") or {}) == {"요청 충족", "간결함"}, rubric)
        mado.note("ADR-055", f"{mado.s2} 판정: {score:.2f} · {rubric.get('scores')}")
    mado.settle()


def test_adr055_a_decision_model_can_judge_the_same_conclusion(mado, ran):
    """결정 LLM 이 있으면 같은 결론을 `score` 질문으로 채점합니다 (요청 한 번)."""
    from dataclasses import replace

    from app import bench
    from app.config import get_config

    cfg = get_config().llm.decision_llm
    if not cfg.is_live:
        pytest.skip("결정 LLM 연결 정보가 없습니다")
    conclusion = ran["variant"].get("conclusion") or ""
    if not conclusion.strip():
        pytest.skip("채점할 결론이 없습니다")
    judge_key = next((a.key for a in __import__("app.agents.pool", fromlist=["get_agent_pool"]).get_agent_pool().list_all()
                      if a.decision_llm.enabled and a.decision_llm.is_live and a.key != "orchestrator"), None)
    if judge_key is None:
        pytest.skip("결정 LLM 을 쓰는 전문가 에이전트가 없습니다")
    scenario = replace(ran["scenario"], rubric=replace(ran["scenario"].rubric, judge=judge_key))
    before = len(mado.decisions)
    result = mado.run(bench.judge(scenario, conclusion, mado.engine), timeout=120)
    calls = mado.decisions[before:]
    mado.check("ADR-055", "결정 LLM 판정은 요청 한 번", len(calls) == 1, len(calls))
    if calls:
        kinds = {q.get("type") for q in (calls[0].payload.get("questions") or {}).values()}
        mado.check("ADR-055", "기준마다 score 질문 하나", kinds == {"score"} and len(calls[0].payload["questions"]) == 2,
                   calls[0].payload.get("questions"))
    if result["score"] is None:
        mado.check("ADR-055", "점수를 읽지 못했으면 사유를 남김", bool(result["error"]), result)
    else:
        mado.check("ADR-055", "점수는 0~1", 0.0 <= result["score"] <= 1.0, result)
    mado.note("ADR-055", f"결정 LLM 판정자({judge_key}): {result['score']} · {result['scores']} · {result['error']}")
    mado.settle()


def test_adr055_a_run_compared_with_itself_has_nothing_worse(mado, ran):
    from app import bench

    path = ran["run_dir"] / "result.json"
    text, regressions = bench.diff_runs(path, path)
    mado.check("ADR-055", "같은 실행끼리는 나빠진 변형이 없음", regressions == [] and "나빠진 변형이 없습니다." in text, text[-300:])
    mado.settle()
