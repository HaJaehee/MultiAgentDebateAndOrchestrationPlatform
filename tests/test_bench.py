"""전략 벤치마크 하네스 테스트 (app/bench.py, v1.5).

1. 시나리오 파일 검증 — 스키마 오류를 취합하여 반환하며 번들 예제 시나리오가 정상 로드된다.
2. 드라이런 검증 — LLM 호출 없이 도구 루프, 이벤트 기록, 계측 로직을 거쳐 결과 JSON 및 보고서를 작성한다.
3. 변형별 모델 스냅샷 고정 및 seed 디렉터리 격리 복제를 검증한다.
4. 비교 화면(/compare)에서 기록된 정량 평가가 보고서 재작성 시 올바르게 반영되는지 검증한다.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import bench
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
    for expected in ("prompt 가 비어", "graph_debate", "nope", "colour", "label 이 겹칩니다", "seed 폴더"):
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
