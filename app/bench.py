"""전략 벤치마크 하네스 (MADO Bench, v1.5): 동일한 프롬프트 요구사항에 대해 여러 전략과 설정을 실행하고 비교합니다.

요구사항에 최적화된 토론 전략을 정량적 지표로 평가하기 위한 도구입니다. 시나리오 파일(`bench/*.json`)에
프롬프트와 비교 대상 변형(전략, 라운드 수, 참여 에이전트, 모델)을 정의하면, 각 변형별로 독립된 세션을 생성하여 실행하고
결과를 `data/bench/<실행 id>/` 디렉터리에 저장합니다.

    python -m app.bench run bench/design-review.json            # conf.json 설정 모델로 실행
    python -m app.bench run bench/design-review.json --dry-run  # LLM 호출 없이 파이프라인 흐름만 검증
    python -m app.bench report data/bench/<실행 id>/result.json  # 비교 화면의 정량 평가를 반영해 보고서 재작성

* 실행된 세션은 데이터베이스에 `[bench]` 접두사가 붙은 제목으로 저장됩니다. 웹 브라우저에서 직접 검토하거나 보고서의 비교 링크(`/compare`)를
  통해 좌우로 나란히 비교하며 평가(👍/👎)를 기록할 수 있으며, `report` 명령으로 평가 결과를 보고서에 재반영할 수 있습니다.
* 변형마다 격리된 작업 공간 디렉터리를 생성합니다 (`seed` 디렉터리 지정 시 복제본 사용). 변형 간 파일 간섭이 배제됩니다.
* 비대면(unattended) 모드로 실행되므로 계획 승인 및 도구 확인 절차를 자동으로 통과합니다 (`app/headless.py`).
* 측정 지표: 실행 상태, 진행 라운드, 조기 종료 여부, 토큰 소모량, LLM 응답 시간, 소요 시간, 미해결 쟁점 수,
  도구 오류/차단 횟수, 무응답 에이전트, 변경 파일 수, 정량 평가.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from app.agents.llm import LLMCaller
from app.agents.llm_usage import Usage, format_tokens
from app.agents.personas import freeze_personas
from app.agents.pool import get_agent_pool
from app.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, get_config
from app.database.models import SessionAgentModel, SessionModel, ToolCallRecordModel, TurnModel
from app.database.session import get_engine, get_session_factory, init_db
from app.headless import HeadlessError, create_session, turn_result
from app.mcp.pool import get_runtime_pool
from app.orchestration import context_memory as memory
from app.orchestration.engine import get_orchestrator_engine
from app.orchestration.strategies import STRATEGY_MAP
from app.routes import compare_url

BENCH_DIR = PROJECT_ROOT / "data" / "bench"
TITLE_PREFIX = "[bench]"


# ---------------------------------------------------------------- 시나리오


@dataclass
class Variant:
    label: str
    strategy: str = "sequential_debate"
    max_rounds: int = 3
    agents: Optional[List[str]] = None
    # 에이전트 키 → 해당 변형에서 사용할 모델 (conf.json 의 연결 정보는 유지하고 모델명만 재정의)
    models: Dict[str, str] = field(default_factory=dict)
    converge_early: bool = True
    token_budget: int = 0
    custom_instructions: str = ""


@dataclass
class Scenario:
    name: str
    prompt: str
    variants: List[Variant]
    seed: Optional[Path] = None


def load_scenario(path: str | Path) -> Scenario:
    """시나리오 파일을 로드하고 유효성을 검증합니다. 오류 발견 시 목록을 취합하여 ValueError를 발생시킵니다."""
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    problems: List[str] = []
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        problems.append("prompt 가 비어 있습니다")
    variants: List[Variant] = []
    for index, raw in enumerate(data.get("variants") or []):
        try:
            variant = Variant(**{k: v for k, v in raw.items() if not k.startswith("//")})
        except TypeError as exc:
            problems.append(f"variants[{index}]: {exc}")
            continue
        strategy = STRATEGY_MAP.get(variant.strategy)
        if strategy is None or strategy.runs_graph:
            problems.append(f"variants[{index}] '{variant.label}': 지원되지 않는 전략 {variant.strategy}")
        variants.append(variant)
    if not variants:
        problems.append("variants 가 비어 있습니다")
    if len({v.label for v in variants}) != len(variants):
        problems.append("variants 의 label 이 겹칩니다")
    seed = None
    if data.get("seed"):
        seed = (source.parent / str(data["seed"])).resolve()
        if not seed.is_dir():
            problems.append(f"seed 폴더가 존재하지 않습니다: {seed}")
    if problems:
        raise ValueError(f"{source}: " + "; ".join(problems))
    return Scenario(name=str(data.get("name") or source.stem), prompt=prompt, variants=variants, seed=seed)


# ---------------------------------------------------------------- LLM 없이 돌리기


class DryRunCaller(LLMCaller):
    """LLM 호출 없이 하네스 파이프라인의 정상 동작을 검증하기 위한 모의(Mock) 호출기 (`--dry-run`).

    `_complete_unthrottled`만 모킹하므로 도구 루프, 이벤트 기록, 계측 로직은 실제 실행 경로를 거칩니다.
    프롬프트 유형에 따라 결정 장부, 최종 결론, 간략 의견 등의 모의 응답을 반환합니다.
    """

    async def _complete_unthrottled(self, agent, messages, tools, on_chunk, tool_choice):
        last = str(messages[-1].get("content") or "") if messages else ""
        if memory.LEDGER_PROMPT_MARKER in last:
            text = ("## 요구사항·제약\n- 요청을 따른다\n\n## 결정 사항\n- 드라이런 결정\n\n## 기각된 대안\n- 없음\n\n"
                    "## 미해결 쟁점\n- 없음\n\n## 담당·다음 할 일\n- 없음")
        elif "최종 합의 보고서" in last:
            text = "## 최종 합의 결론\n\n드라이런 결론입니다.\n\n```mermaid\nflowchart LR\n  A[요청] --> B[결론]\n```"
        else:
            text = f"### {agent.name} 의견\n\n드라이런 발언입니다."
        if on_chunk:
            await self._emit_chunk(on_chunk, text)
        message = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None,
                                  model_dump=lambda: {"role": "assistant", "content": text})
        return message, "stop"


# ---------------------------------------------------------------- 실행


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "-", text).strip("-")[:40] or "variant"


async def _freeze_models(session_id: str, models: Dict[str, str]) -> None:
    """해당 변형의 모델 설정으로 세션 에이전트 구성을 스냅샷 고정합니다 (첫 턴 실행 시 재동기화 방지)."""
    if not models:
        return
    async with get_session_factory()() as db:
        session = await db.get(SessionModel, session_id)
        await freeze_personas(db, session, get_agent_pool())
        rows = (await db.execute(
            select(SessionAgentModel).where(SessionAgentModel.session_id == session_id)
        )).scalars().all()
        for row in rows:
            if row.agent_key in models and isinstance(row.config_snapshot, dict):
                row.config_snapshot = {**row.config_snapshot, "model": models[row.agent_key]}
        await db.commit()


async def _tool_problems(turn_id: Optional[str]) -> int:
    if not turn_id:
        return 0
    async with get_session_factory()() as db:
        return int(await db.scalar(
            select(func.count()).select_from(ToolCallRecordModel).where(
                ToolCallRecordModel.turn_id == turn_id, ToolCallRecordModel.status.in_(("error", "denied")),
            )
        ) or 0)


async def run_variant(scenario: Scenario, variant: Variant, run_dir: Path, engine: Any) -> Dict[str, Any]:
    workspace = run_dir / "workspaces" / _slug(variant.label)
    if scenario.seed is not None:
        shutil.copytree(scenario.seed, workspace)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
    created = await create_session(
        title=f"{TITLE_PREFIX} {scenario.name} · {variant.label}", strategy=variant.strategy,
        max_rounds=variant.max_rounds, agents=variant.agents, workspace_dir=str(workspace),
        converge_early=variant.converge_early, token_budget=variant.token_budget,
        custom_instructions=variant.custom_instructions,
    )
    sid = created["session_id"]
    await _freeze_models(sid, variant.models)
    began = time.monotonic()
    error = ""
    try:
        state = await engine.run_turn(session_id=sid, user_prompt=scenario.prompt)
        rounds = state.current_round
    except Exception as exc:  # noqa: BLE001 - 한 변형의 실패로 나머지를 멈추지 않습니다
        error, rounds = f"{type(exc).__name__}: {exc}", 0
    seconds = round(time.monotonic() - began, 1)
    result = await turn_result(sid)
    return {
        "label": variant.label,
        "strategy": variant.strategy,
        "max_rounds": variant.max_rounds,
        "agents": created["agents"],
        "models": variant.models,
        "session_id": sid,
        "turn_id": result.get("turn_id"),
        "status": result.get("status") if not error else "failed",
        "error": error or result.get("error"),
        "rounds": rounds,
        "ended_early": result.get("ended_early"),
        "usage": result.get("usage"),
        "failed_agents": result.get("failed_agents") or [],
        "tool_problems": await _tool_problems(result.get("turn_id")),
        "open_issues": memory.open_issue_count(result.get("decision_ledger") or ""),
        "files_changed": result.get("files_changed"),
        "seconds": seconds,
        "conclusion": result.get("conclusion") or "",
    }


async def run(scenario_path: str | Path, *, dry_run: bool = False, out_dir: Path = BENCH_DIR) -> Path:
    """시나리오를 돌리고 `result.json`·`report.md` 를 쓴 폴더를 돌려줍니다."""
    scenario = load_scenario(scenario_path)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    run_dir = Path(out_dir) / run_id
    run_dir.mkdir(parents=True)
    engine = get_orchestrator_engine()
    if dry_run:
        engine.llm_caller = DryRunCaller()
    results = []
    for variant in scenario.variants:
        print(f"[bench] {variant.label} …", flush=True)
        results.append(await run_variant(scenario, variant, run_dir, engine))
    payload = {
        "run_id": run_id, "scenario": scenario.name, "prompt": scenario.prompt, "dry_run": dry_run,
        "source": str(Path(scenario_path).resolve()), "created_at": datetime.now().isoformat(timespec="seconds"),
        "variants": results,
    }
    (run_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    await write_report(run_dir / "result.json")
    return run_dir


# ---------------------------------------------------------------- 보고서


async def _ratings(turn_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    ids = [t for t in turn_ids if t]
    if not ids:
        return {}
    async with get_session_factory()() as db:
        rows = (await db.execute(select(TurnModel.id, TurnModel.rating).where(TurnModel.id.in_(ids)))).all()
    return {turn_id: rating for turn_id, rating in rows if rating}


def _cell(value: Any) -> str:
    return "-" if value in (None, "", []) else str(value).replace("|", "\\|")


async def write_report(result_path: str | Path) -> Path:
    """`result.json`을 기반으로 `report.md` 보고서를 생성(갱신)합니다. 비교 페이지(/compare)에서 기록된 평가를 DB에서 조회하여 반영합니다."""
    result_path = Path(result_path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    variants = payload["variants"]
    ratings = await _ratings([v.get("turn_id") for v in variants])
    lines = [
        f"# MADO Bench — {payload['scenario']}",
        "",
        f"- 실행: `{payload['run_id']}` ({payload['created_at']}){' · 드라이런 (LLM 없음)' if payload.get('dry_run') else ''}",
        f"- 요청: {payload['prompt']}",
        "",
        "| 변형 | 전략 | 상태 | 라운드 | 조기 종료 | 입력 토큰 | 출력 토큰 | LLM 시간 | 소요 시간 | 미해결 쟁점 | 도구 실패·차단 | 무응답 에이전트 | 변경 파일 | 평가 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for v in variants:
        usage = Usage.of(v.get("usage"))
        early = v.get("ended_early") or {}
        rating = ratings.get(v.get("turn_id") or "") or {}
        score = {1: "👍", -1: "👎"}.get(rating.get("score"), "")
        if rating.get("note"):
            score += f" {rating['note']}"
        lines.append("| " + " | ".join(_cell(x) for x in (
            v["label"], v["strategy"], v["status"], f"{v['rounds']}/{v['max_rounds']}",
            f"{early.get('reason')} @ {early.get('round')}" if early else "",
            format_tokens(usage.prompt_tokens) if usage.prompt_tokens else "",
            format_tokens(usage.completion_tokens) if usage.completion_tokens else "",
            f"{usage.llm_seconds:.1f}초" if usage.llm_seconds else "",
            f"{v['seconds']}초", v.get("open_issues"), v.get("tool_problems"),
            ", ".join(v.get("failed_agents") or []), v.get("files_changed"), score,
        )) + " |")
    first = variants[0]["session_id"] if variants else ""
    lines += ["", "## 세션 비교 (Compare)", ""]
    for v in variants[1:]:
        lines.append(f"- {variants[0]['label']} ↔ {v['label']}: `{compare_url(first, v['session_id'])}`")
    lines += ["", "평가는 비교 화면(/compare)에서 기록한 후 `python -m app.bench report " + str(result_path) + "` 명령으로 보고서에 반영할 수 있습니다.", ""]
    for v in variants:
        lines += [f"## {v['label']}", ""]
        if v.get("error"):
            lines += [f"> 오류: {v['error']}", ""]
        lines += [(v.get("conclusion") or "_결론 없음_").strip(), ""]
    report = result_path.with_name("report.md")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


# ---------------------------------------------------------------- 명령줄


async def _main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.bench", description="MADO 전략 비교 하네스")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="conf.json 경로")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="시나리오를 돌립니다")
    run_cmd.add_argument("scenario")
    run_cmd.add_argument("--dry-run", action="store_true", help="LLM 없이 흐름만 확인합니다")
    run_cmd.add_argument("--out", default=str(BENCH_DIR))
    report_cmd = sub.add_parser("report", help="평가를 반영해 보고서를 다시 씁니다")
    report_cmd.add_argument("result")
    args = parser.parse_args(argv)

    # DB 엔진은 처음 부른 쪽의 주소로 고정됩니다. 설정의 주소로 먼저 묶습니다.
    cfg = get_config(reload=True, config_path=args.config)
    get_engine(cfg.app.db_url)
    await init_db(cfg.app.db_url)
    try:
        if args.command == "run":
            run_dir = await run(args.scenario, dry_run=args.dry_run, out_dir=Path(args.out))
            print(f"[bench] 결과: {run_dir / 'report.md'}")
        else:
            print(f"[bench] 보고서: {await write_report(args.result)}")
    except (ValueError, HeadlessError) as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2
    finally:
        await get_runtime_pool().shutdown_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
