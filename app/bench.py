"""전략 비교 하네스 (MADO Bench, v1.5): 같은 요청을 여러 조합으로 돌려 표로 비교합니다.

"이 일에는 어떤 전략이 맞나" 를 감이 아니라 기록으로 고르려는 도구입니다. 시나리오 파일(`bench/*.json`) 하나에
요청과 비교할 변형(전략·라운드·참여자·모델)을 적고 돌리면, 변형마다 대화를 하나씩 만들어 끝까지 돌리고
결과를 `data/bench/<실행 id>/` 에 남깁니다.

    python -m app.bench run bench/design-review.json            # conf.json 의 모델로
    python -m app.bench run bench/design-review.json --dry-run  # LLM 없이 흐름만 확인
    python -m app.bench report data/bench/<실행 id>/result.json  # 비교 화면의 평가를 반영해 보고서 다시 쓰기

* 대화는 앱의 DB 에 `[bench]` 로 시작하는 제목으로 남습니다. 앱에서 열어 보고, 보고서의 비교 링크(`/compare`)로
  나란히 놓고 👍/👎 를 매긴 뒤 `report` 를 다시 돌리면 평가가 표에 실립니다.
* 변형마다 작업 공간을 따로 만듭니다 (`seed` 폴더가 있으면 그 사본). 서로의 파일을 보지 않습니다.
* 턴은 답할 사람이 없는 턴으로 돕니다 — 계획 승인과 도구 확인을 묻지 않습니다 (`app/headless.py`).
* 지표: 상태, 라운드, 일찍 끝났는지, 토큰·LLM 시간, 응답 못 한 에이전트, 도구 실패·차단 수, 마지막 장부의 미해결
  쟁점 수, 바뀐 파일 수, 걸린 시간, 사람의 평가.
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
    # 에이전트 키 → 이 변형에서 쓸 모델 (conf.json 의 연결 정보는 그대로, 모델 이름만 바꿉니다)
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
    """시나리오 파일을 읽고 확인합니다. 틀린 곳이 있으면 모두 모아 ValueError 로 알립니다."""
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
            problems.append(f"variants[{index}] '{variant.label}': 쓸 수 없는 전략 {variant.strategy}")
        variants.append(variant)
    if not variants:
        problems.append("variants 가 비어 있습니다")
    if len({v.label for v in variants}) != len(variants):
        problems.append("variants 의 label 이 겹칩니다")
    seed = None
    if data.get("seed"):
        seed = (source.parent / str(data["seed"])).resolve()
        if not seed.is_dir():
            problems.append(f"seed 폴더가 없습니다: {seed}")
    if problems:
        raise ValueError(f"{source}: " + "; ".join(problems))
    return Scenario(name=str(data.get("name") or source.stem), prompt=prompt, variants=variants, seed=seed)


# ---------------------------------------------------------------- LLM 없이 돌리기


class DryRunCaller(LLMCaller):
    """LLM 없이 하네스의 흐름을 확인하는 대본 호출기 (`--dry-run`).

    `_complete_unthrottled` 만 바꿔 끼우므로 도구 루프·기록·계측은 진짜 경로를 지납니다. 답은 프롬프트의 모양만
    보고 정합니다 — 장부 갱신이면 장부, 결론 요청이면 결론, 나머지는 짧은 의견.
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
    """이 변형의 모델로 에이전트 구성을 미리 고정합니다 (잠긴 대화 — 첫 턴이 conf.json 을 다시 읽지 않습니다)."""
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
    """`result.json` 으로 `report.md` 를 (다시) 씁니다. 그 사이 비교 화면에서 매긴 평가를 DB 에서 읽어 넣습니다."""
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
        "| 변형 | 전략 | 상태 | 라운드 | 일찍 끝남 | 입력 토큰 | 출력 토큰 | LLM 시간 | 걸린 시간 | 미해결 쟁점 | 도구 실패·차단 | 응답 없음 | 바뀐 파일 | 평가 |",
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
    lines += ["", "## 나란히 보기", ""]
    for v in variants[1:]:
        lines.append(f"- {variants[0]['label']} ↔ {v['label']}: `{compare_url(first, v['session_id'])}`")
    lines += ["", "평가는 비교 화면에서 매긴 뒤 `python -m app.bench report " + str(result_path) + "` 로 다시 반영합니다.", ""]
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
