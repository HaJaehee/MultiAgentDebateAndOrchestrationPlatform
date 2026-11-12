"""전략 벤치마크 하네스 테스트 (app/bench.py, v1.5).

1. 시나리오 파일 검증 — 스키마 오류를 취합하여 반환하며 번들 예제 시나리오가 정상 로드된다.
2. 드라이런 검증 — LLM 호출 없이 도구 루프, 이벤트 기록, 계측 로직을 거쳐 결과 JSON 및 보고서를 작성한다.
3. 변형별 모델 스냅샷 고정 및 seed 디렉터리 격리 복제를 검증한다.
4. 비교 화면(/compare)에서 기록된 정량 평가가 보고서 재작성 시 올바르게 반영되는지 검증한다.
5. 품질을 수치로 검증 (ADR-055) — 결정적 검사, 판정 모델의 채점, 모델 행렬, 그래프 변형, 두 실행 비교.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import bench, graph_store
from app.agents.decision import DecisionAnswer, DecisionResponse
from app.agents.pool import get_agent_pool
from app.headless import HeadlessError, create_session
from app.orchestration.graph import parse_graph
from app.database.models import SessionAgentModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.session_fork import rate_turn

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, data: dict, name: str = "s.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_the_shipped_scenarios_load():
    for name in ("design-review.json", "small-impl.json"):
        scenario = bench.load_scenario(ROOT / "bench" / name)
        assert scenario.prompt and len(scenario.variants) == 3
    assert bench.load_scenario(ROOT / "bench" / "small-impl.json").seed.joinpath("sales.csv").is_file()


def test_scenario_problems_are_collected(tmp_path):
    path = _write(tmp_path, {
        "prompt": " ", "seed": "없는-폴더",
        "variants": [
            {"label": "a", "strategy": "graph_debate"},
            {"label": "a", "strategy": "nope"},
            {"label": "b", "colour": "red"},
        ],
    })
    with pytest.raises(ValueError) as exc:
        bench.load_scenario(path)
    message = str(exc.value)
    for expected in ("prompt가 비어", "graph_debate", "nope", "colour", "label이 중복됩니다", "seed 디렉터리"):
        assert expected in message, expected


@pytest.mark.asyncio
async def test_a_dry_run_writes_results_and_a_report(tmp_path, monkeypatch):
    await init_db("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(bench, "get_orchestrator_engine", lambda: OrchestratorEngine())
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    path = _write(tmp_path, {
        "name": "시험", "prompt": "검토해 줘", "seed": "seed",
        "variants": [
            {"label": "순차", "strategy": "sequential_debate", "max_rounds": 3},
            {"label": "병렬 · 모델 바꿈", "strategy": "parallel_dispatch", "max_rounds": 1,
             "agents": ["architect", "critic"], "models": {"critic": "openai/other-model"}},
        ],
    })

    run_dir = await bench.run(path, dry_run=True, out_dir=tmp_path / "out")

    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["dry_run"] and [v["label"] for v in payload["variants"]] == ["순차", "병렬 · 모델 바꿈"]
    first, second = payload["variants"]
    assert first["status"] == "completed" and "드라이런 결론" in first["conclusion"]
    assert first["ended_early"] == {"reason": "converged", "round": 2} and first["rounds"] == 2
    assert first["open_issues"] == 0 and first["tool_problems"] == 0 and first["failed_agents"] == []
    assert first["usage"]["calls"] > 0, "드라이런도 진짜 호출 경로를 지나 호출 수와 시간을 셉니다"
    report_text = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "| 도구 컨텍스트 |" in report_text.splitlines()[5], "도구 불러오기·압축 뒤 다시 읽기 열이 있습니다"
    assert second["agents"] == ["orchestrator", "architect", "critic"]
    for v in payload["variants"]:
        workspace = run_dir / "workspaces" / bench._slug(v["label"])
        assert (workspace / "data.csv").is_file()

    async with get_session_factory()() as db:
        title = (await db.get(SessionModel, first["session_id"])).title
        critic = (await db.execute(select(SessionAgentModel).where(
            SessionAgentModel.session_id == second["session_id"], SessionAgentModel.agent_key == "critic",
        ))).scalar_one()
    assert title == "[bench] 시험 · 순차"
    assert critic.config_snapshot["model"] == "openai/other-model"

    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "# MADO Bench — 시험" in report and "드라이런" in report
    assert "| 순차 | sequential_debate | completed | 2/3 | converged @ 2 |" in report
    assert f"/compare?a={first['session_id']}&b={second['session_id']}" in report

    # 비교 화면에서 매긴 평가가 다시 쓴 보고서에 실립니다.
    async with get_session_factory()() as db:
        await rate_turn(db, second["turn_id"], 1, "더 간결")
    await bench.write_report(run_dir / "result.json")
    assert "👍 더 간결" in (run_dir / "report.md").read_text(encoding="utf-8")


# ================================================================ 5. 품질을 수치로 검증 (ADR-055)

CHECKED = {
    "name": "채점 테스트", "prompt": "요약 스크립트를 만들어 줘",
    "variants": [{"label": "순차", "strategy": "sequential_debate", "max_rounds": 1}],
    "checks": [
        {"name": "스크립트가 있음", "file_exists": "summarize.py"},
        {"name": "README에 합계", "file_contains": {"path": "README.md", "text": "합계"}},
        {"name": "결론에 드라이런", "conclusion_contains": {"regex": "드라이런\\s*결론"}},
        {"name": "스크립트가 정상 실행됨", "command": {"argv": ["{python}", "-c", "print('ok 42')"], "stdout_contains": "42"}},
        {"name": "미해결 쟁점이 남지 않음", "max_open_issues": 0},
    ],
    "rubric": {
        "judge": "critic", "scale": ["미흡", "보통", "충실"],
        "criteria": [
            {"name": "정확성", "question": "요청한 것을 빠짐없이 했는가?"},
            {"name": "위험", "question": "위험 요소를 구체적으로 적었는가?"},
        ],
    },
}


def test_checks_and_a_rubric_are_read_from_the_scenario(tmp_path):
    scenario = bench.load_scenario(_write(tmp_path, CHECKED))
    assert [(c.name, c.kind) for c in scenario.checks] == [
        ("스크립트가 있음", "file_exists"), ("README에 합계", "file_contains"), ("결론에 드라이런", "conclusion_contains"),
        ("스크립트가 정상 실행됨", "command"), ("미해결 쟁점이 남지 않음", "max_open_issues"),
    ]
    assert scenario.rubric.judge == "critic" and scenario.rubric.scale == ("미흡", "보통", "충실")
    assert scenario.rubric.criteria[0] == ("정확성", "요청한 것을 빠짐없이 했는가?")
    plain = bench.load_scenario(_write(tmp_path, {
        "prompt": "p", "variants": [{"label": "a", "strategy": "sequential_debate"}],
    }, "plain.json"))
    assert plain.checks == [] and plain.rubric is None, "적지 않은 시나리오는 예전 그대로입니다"
    # 함께 배포하는 예제는 하나씩 보여 줍니다: 설계 검토는 채점표, 소규모 구현은 실행 검사.
    assert bench.load_scenario(ROOT / "bench" / "design-review.json").rubric.judge == "critic"
    assert "command" in [c.kind for c in bench.load_scenario(ROOT / "bench" / "small-impl.json").checks]


def test_check_and_rubric_problems_are_collected_with_the_rest(tmp_path):
    path = _write(tmp_path, {
        "prompt": "p",
        "variants": [
            {"label": "a", "strategy": "sequential_debate", "graph": "some-graph"},
            {"label": "b", "strategy": "sequential_debate", "matrix": {"critic": "openai/x"}},
            {"label": "c", "strategy": "sequential_debate", "models": {"nobody": "openai/x"}},
        ],
        "checks": [
            {"name": "둘", "file_exists": "a", "max_open_issues": 0},
            {"name": "셸", "command": {"argv": "python x.py"}},
            {"name": "정규식", "conclusion_contains": {"regex": "("}},
            {"name": "음수", "max_open_issues": -1},
            "문자열",
        ],
        "rubric": {"judge": "nobody", "criteria": [{"name": "이름만"}], "scale": ["하나"]},
    })
    with pytest.raises(ValueError) as exc:
        bench.load_scenario(path)
    message = str(exc.value)
    for expected in (
        "graph는 그래프 토론과 적응형 그래프 토론에서만", "matrix는 에이전트 키별로", "설정에 없는 에이전트의 모델을 지정했습니다 (nobody)",
        "중 하나만 지정해야 합니다", "단일 셸 명령 문자열은 지원하지 않습니다", "regex가 올바르지 않습니다", "0 이상의 정수", "객체가 아닙니다",
        "판정자 'nobody'", "name과 question", "criteria가 비어", "두 단계 이상",
    ):
        assert expected in message, expected


def test_a_matrix_becomes_one_variant_per_combination(tmp_path):
    scenario = bench.load_scenario(_write(tmp_path, {
        "prompt": "p",
        "variants": [
            {"label": "순차", "strategy": "sequential_debate", "max_rounds": 2, "models": {"architect": "openai/fixed"},
             "matrix": {"critic": ["openai/qwen3-32b", "openai/gpt-oss-20b"], "coder": ["openai/a", "openai/b"]}},
            {"label": "그대로", "strategy": "sequential_debate"},
        ],
    }))
    labels = [v.label for v in scenario.variants]
    assert labels == [
        "순차 · critic=qwen3-32b, coder=a", "순차 · critic=qwen3-32b, coder=b",
        "순차 · critic=gpt-oss-20b, coder=a", "순차 · critic=gpt-oss-20b, coder=b", "그대로",
    ]
    first = scenario.variants[0]
    assert first.models == {"architect": "openai/fixed", "critic": "openai/qwen3-32b", "coder": "openai/a"}
    assert first.max_rounds == 2 and first.matrix == {}
    assert scenario.variants[-1].models == {}


@pytest.mark.asyncio
async def test_checks_look_only_at_that_variants_workspace_and_conclusion(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "summarize.py").write_text("print('서울 120')\n", encoding="utf-8")
    (workspace / "README.md").write_text("# 결과\n지역별 합계: 서울 120\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")

    def check(kind, spec, name="c"):
        return bench.Check(name=name, kind=kind, spec=spec)

    results = await bench.run_checks([
        check("file_exists", "summarize.py", "있음"),
        check("file_exists", "missing.py", "없음"),
        check("file_exists", "../outside.txt", "밖"),
        check("file_contains", {"path": "README.md", "text": "합계"}, "글"),
        check("file_contains", {"path": "README.md", "regex": "^지역별 합계: \\S+ \\d+$"}, "정규식"),
        check("file_contains", {"path": "README.md", "text": "부산"}, "없는 글"),
        check("conclusion_contains", "Redis", "결론"),
        check("command", {"argv": ["{python}", "summarize.py"], "stdout_contains": "서울"}, "실행"),
        check("command", {"argv": ["{python}", "-c", "import sys; sys.exit(3)"]}, "실패"),
        check("command", {"argv": ["{python}", "-c", "import time; time.sleep(5)"], "timeout": 0.5}, "시간 초과"),
        check("command", {"argv": ["no-such-program-xyz"]}, "없는 프로그램"),
        check("max_open_issues", 1, "쟁점"),
    ], workspace, "캐시는 Redis 로 정했습니다.", open_issues=2)
    verdict = {r["name"]: r["ok"] for r in results}
    assert verdict == {
        "있음": True, "없음": False, "밖": False, "글": True, "정규식": True, "없는 글": False, "결론": True,
        "실행": True, "실패": False, "시간 초과": False, "없는 프로그램": False, "쟁점": False,
    }
    detail = {r["name"]: r["detail"] for r in results}
    assert "작업 공간 밖" in detail["밖"] and "종료 코드 3" in detail["실패"] and "종료되지 않았습니다" in detail["시간 초과"]
    assert "미해결 2건 (상한 1)" in detail["쟁점"]


class _JudgeCaller:
    """판정자 호출을 모방합니다. 전달된 메시지를 모아 두고 정해 둔 응답을 반환합니다."""

    def __init__(self, reply="", decision=None, error=None):
        self.reply, self.error, self.sent = reply, error, []
        self.decision_caller = SimpleNamespace(evaluate=self._evaluate)
        self.decision = decision
        self.decision_questions = None

    async def call_agent(self, agent, messages, custom_instructions=""):
        self.sent.append((agent, messages))
        if self.error:
            raise self.error
        return self.reply, []

    async def _evaluate(self, cfg, state_text, questions, agent=None):
        self.decision_questions = (state_text, questions)
        return self.decision


def _scenario(tmp_path) -> bench.Scenario:
    return bench.load_scenario(_write(tmp_path, CHECKED))


@pytest.mark.asyncio
async def test_the_judge_sees_only_the_request_and_the_conclusion_and_scores_each_criterion(tmp_path):
    scenario = _scenario(tmp_path)
    caller = _JudgeCaller('판정입니다.\n{"scores": {"정확성": 2, "위험": "보통"}}')
    result = await bench.judge(scenario, "## 최종 합의 결론\n요약 스크립트를 만들었습니다.", SimpleNamespace(llm_caller=caller))
    assert result["error"] == "" and result["judge"] == "critic"
    assert result["scores"] == {"정확성": {"step": 2, "label": "충실"}, "위험": {"step": 1, "label": "보통"}}
    assert result["score"] == 0.75, "(2 + 1) / (2기준 × 최고 2)"

    agent, messages = caller.sent[0]
    assert agent.allowed_mcp_servers == [] and not agent.sequential_thinking.enabled, "판정자는 도구 없이, 사고 지침 없이 답만 합니다"
    text = messages[0]["content"]
    assert "요약 스크립트를 만들어 줘" in text and "요약 스크립트를 만들었습니다." in text and "미흡 < 보통 < 충실" in text
    assert "순차" not in text and "sequential_debate" not in text, "변형의 이름과 전략은 판정자에게 가지 않습니다"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, word", [
    ("점수를 매기기 어렵습니다.", "점수를 읽지 못한 기준 정확성, 위험"),
    ('{"scores": {"정확성": 2}}', "점수를 읽지 못한 기준 위험"),
    ('{"scores": {"정확성": 9, "위험": -1}}', "점수를 읽지 못한 기준 정확성, 위험"),
    ('{"scores": "높음"}', "점수를 읽지 못한 기준"),
])
async def test_an_answer_the_judge_did_not_give_is_not_made_up(tmp_path, reply, word):
    result = await bench.judge(_scenario(tmp_path), "결론", SimpleNamespace(llm_caller=_JudgeCaller(reply)))
    assert result["score"] is None and word in result["error"]


@pytest.mark.asyncio
async def test_a_failing_judge_or_an_empty_conclusion_leaves_the_variant_unscored(tmp_path):
    scenario = _scenario(tmp_path)
    failed = await bench.judge(scenario, "결론", SimpleNamespace(llm_caller=_JudgeCaller(error=RuntimeError("down"))))
    assert failed["score"] is None and "판정 실패: RuntimeError" in failed["error"]
    caller = _JudgeCaller('{"scores": {"정확성": 2, "위험": 2}}')
    empty = await bench.judge(scenario, "  ", SimpleNamespace(llm_caller=caller))
    assert empty["score"] is None and "결론이 없어" in empty["error"] and caller.sent == []


@pytest.mark.asyncio
async def test_a_decision_model_judges_with_one_score_question_per_criterion(tmp_path, monkeypatch):
    scenario = _scenario(tmp_path)
    critic = get_agent_pool().get("critic")
    judge_agent = critic.model_copy(update={"decision_llm": critic.decision_llm.model_copy(update={"enabled": True})})
    monkeypatch.setattr(bench, "get_agent_pool", lambda: SimpleNamespace(get=lambda key: judge_agent))
    caller = _JudgeCaller(decision=DecisionResponse(model="jev", answers={
        "c0": DecisionAnswer(type="score", score=1.87), "c1": DecisionAnswer(type="score", score=0.2),
    }))
    result = await bench.judge(scenario, "결론입니다", SimpleNamespace(llm_caller=caller))
    assert result["scores"] == {"정확성": {"step": 2, "label": "충실"}, "위험": {"step": 0, "label": "미흡"}}
    assert result["score"] == 0.5 and caller.sent == [], "생성형 모델은 부르지 않습니다"
    state, questions = caller.decision_questions
    assert "결론입니다" in state and set(questions) == {"c0", "c1"}
    assert questions["c0"].type == "score" and questions["c0"].criteria == ("미흡", "보통", "충실")


@pytest.mark.asyncio
async def test_a_dry_run_runs_the_checks_but_does_not_judge(tmp_path, monkeypatch):
    await init_db("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(bench, "get_orchestrator_engine", lambda: OrchestratorEngine())
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "summarize.py").write_text("print(1)\n", encoding="utf-8")
    run_dir = await bench.run(_write(tmp_path, {**CHECKED, "seed": "seed"}), dry_run=True, out_dir=tmp_path / "out")

    (variant,) = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["variants"]
    assert {c["name"]: c["ok"] for c in variant["checks"]} == {
        "스크립트가 있음": True, "README에 합계": False, "결론에 드라이런": True, "스크립트가 정상 실행됨": True, "미해결 쟁점이 남지 않음": True,
    }
    assert variant["rubric"]["score"] is None and "드라이런" in variant["rubric"]["error"]
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    header, row = report.splitlines()[5], report.splitlines()[7]
    assert "| 검사 | 채점 |" in header and "| 4/5 | 채점 안 함 |" in row
    assert "- ❌ README에 합계 — README.md 파일이 없습니다" in report and "- ✅ 스크립트가 있음 — 있음" in report


def _one_step_graph() -> dict:
    return {
        "id": "one-step", "name": "한 단계",
        "nodes": [{"id": "start", "type": "start"},
                  {"id": "design", "type": "agent", "agent": "architect", "label": "설계", "max_visits": 1},
                  {"id": "end", "type": "end"}],
        "edges": [{"id": "e1", "from": ["start", "out"], "to": ["design", "in"]},
                  {"id": "e2", "from": ["design", "out"], "to": ["end", "in"]}],
    }


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    folder = tmp_path / "graphs"
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: folder)
    graph_store.save_graph(parse_graph(_one_step_graph()))
    return folder


def test_a_graph_variant_names_a_saved_graph(tmp_path, graphs):
    scenario = bench.load_scenario(_write(tmp_path, {
        "prompt": "p", "variants": [
            {"label": "그래프", "strategy": "graph_debate", "graph": "one-step"},
            {"label": "적응형", "strategy": "adaptive_graph", "graph": "one-step"},
        ],
    }))
    assert [v.graph for v in scenario.variants] == ["one-step", "one-step"]
    for bad, word in (
        ({"label": "a", "strategy": "graph_debate"}, "graph에 그래프 ID를 지정해야"),
        ({"label": "a", "strategy": "graph_debate", "graph": "no-such-graph"}, "'no-such-graph'를 읽지 못했습니다"),
    ):
        with pytest.raises(ValueError) as exc:
            bench.load_scenario(_write(tmp_path, {"prompt": "p", "variants": [bad]}))
        assert word in str(exc.value)


@pytest.mark.asyncio
async def test_a_session_can_be_created_on_a_saved_graph_but_not_on_a_missing_one(graphs):
    await init_db("sqlite+aiosqlite:///:memory:")
    created = await create_session(strategy="graph_debate", graph_id="one-step")
    async with get_session_factory()() as db:
        assert (await db.get(SessionModel, created["session_id"])).graph_id == "one-step"
    with pytest.raises(HeadlessError) as missing:
        await create_session(strategy="graph_debate", graph_id="no-such-graph")
    assert missing.value.status == 400 and "no-such-graph" in str(missing.value)
    with pytest.raises(HeadlessError) as none:
        await create_session(strategy="graph_debate")
    assert none.value.status == 400, "그래프를 고르지 않은 그래프 토론은 예전처럼 거절합니다"


@pytest.mark.asyncio
async def test_a_graph_variant_runs_on_its_graph(tmp_path, monkeypatch, graphs):
    await init_db("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(bench, "get_orchestrator_engine", lambda: OrchestratorEngine())
    run_dir = await bench.run(_write(tmp_path, {
        "name": "그래프", "prompt": "설계해 줘",
        "variants": [{"label": "한 단계", "strategy": "graph_debate", "graph": "one-step", "agents": ["architect"]}],
    }), dry_run=True, out_dir=tmp_path / "out")
    (variant,) = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["variants"]
    assert variant["status"] == "completed" and variant["graph"] == "one-step", variant.get("error")
    async with get_session_factory()() as db:
        session = await db.get(SessionModel, variant["session_id"])
    assert session.graph_id == "one-step" and session.strategy == "graph_debate"


def _result(run_id, variants, prompt="같은 요청") -> dict:
    return {"run_id": run_id, "scenario": "시험", "prompt": prompt, "created_at": "2026-11-11T10:00:00", "variants": variants}


def _variant(label, *, status="completed", passed=2, total=2, score=0.8, tokens=1000, seconds=10.0) -> dict:
    return {
        "label": label, "status": status, "seconds": seconds,
        "checks": [{"name": f"c{i}", "ok": i < passed} for i in range(total)],
        "rubric": {"judge": "critic", "score": score, "scores": {}, "error": ""} if score is not None else None,
        "usage": {"prompt_tokens": tokens, "completion_tokens": 0, "calls": 1},
    }


def test_a_diff_names_what_got_worse_and_only_that(tmp_path):
    before = _write(tmp_path, _result("r1", [
        _variant("그대로"), _variant("끝나지 못함"), _variant("검사 줄어듦"), _variant("채점 내려감"),
        _variant("토큰만 늚"), _variant("나아짐", passed=1, score=0.5), _variant("사라짐"),
    ]), "a.json")
    after = _write(tmp_path, _result("r2", [
        _variant("그대로", score=0.7), _variant("끝나지 못함", status="failed"), _variant("검사 줄어듦", passed=1),
        _variant("채점 내려감", score=0.6), _variant("토큰만 늚", tokens=1400), _variant("나아짐", passed=2, score=0.9),
        _variant("새로 생김"),
    ]), "b.json")
    text, regressions = bench.diff_runs(before, after)
    assert regressions == [
        "끝나지 못함: 실행 미완료", "검사 줄어듦: 통과 검사 수 감소", "채점 내려감: 점수 0.20 하락",
    ]
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| ")}
    assert "유지 또는 개선" in rows["그대로"], "0.1 하락은 판정자의 오차 범위로 간주합니다"
    assert "유지 또는 개선 · 토큰 +40%" in rows["토큰만 늚"], "토큰 증가는 참고 정보로만 알립니다"
    assert "1/2 → 2/2" in rows["나아짐"] and "0.50 → 0.90" in rows["나아짐"]
    assert "이후에만 있음" in rows["새로 생김"] and "이전에만 있음" in rows["사라짐"]
    assert "저하된 변형 3개." in text


def test_a_diff_of_different_requests_says_so(tmp_path):
    before = _write(tmp_path, _result("r1", [_variant("a")], prompt="요청 하나"), "a.json")
    after = _write(tmp_path, _result("r2", [_variant("a")], prompt="요청 둘"), "b.json")
    text, regressions = bench.diff_runs(before, after)
    assert regressions == [] and "요청(prompt)이 다릅니다" in text and "저하된 변형이 없습니다." in text


@pytest.mark.asyncio
async def test_the_diff_command_exits_by_what_it_found_and_needs_no_config(tmp_path, capsys, monkeypatch):
    def no_config(*args, **kwargs):
        raise AssertionError("diff는 설정을 읽지 않습니다")

    monkeypatch.setattr(bench, "get_config", no_config)
    good = _write(tmp_path, _result("r1", [_variant("a")]), "a.json")
    worse = _write(tmp_path, _result("r2", [_variant("a", status="failed")]), "b.json")
    assert await bench._main(["diff", str(good), str(good)]) == 0  # noqa: SLF001
    assert "저하된 변형이 없습니다." in capsys.readouterr().out
    assert await bench._main(["diff", str(good), str(worse)]) == 1  # noqa: SLF001
    assert "성능 저하: 실행 미완료" in capsys.readouterr().out
    assert await bench._main(["diff", str(good), str(tmp_path / "없는-파일.json")]) == 2  # noqa: SLF001
    assert "읽지 못했습니다" in capsys.readouterr().err


def test_python_placeholder_runs_the_interpreter_the_bench_runs_on(tmp_path):
    ok, detail = bench._run_command({"argv": ["{python}", "-c", "import sys; print(sys.executable)"],  # noqa: SLF001
                                     "stdout_contains": Path(sys.executable).name}, tmp_path)
    assert ok, detail
