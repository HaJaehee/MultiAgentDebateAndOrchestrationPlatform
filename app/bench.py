"""전략 벤치마크 하네스 (MADO Bench, v1.5): 동일한 프롬프트 요구사항에 대해 여러 전략과 설정을 실행하고 비교합니다.

요구사항에 최적화된 토론 전략을 정량적 지표로 평가하기 위한 도구입니다. 시나리오 파일(`bench/*.json`)에
프롬프트와 비교 대상 변형(전략, 라운드 수, 참여 에이전트, 모델)을 정의하면, 각 변형별로 독립된 세션을 생성하여 실행하고
결과를 `data/bench/<실행 id>/` 디렉터리에 저장합니다.

    python -m app.bench run bench/design-review.json            # conf.json 설정 모델로 실행
    python -m app.bench run bench/design-review.json --dry-run  # LLM 호출 없이 파이프라인 흐름만 검증
    python -m app.bench report data/bench/<실행 id>/result.json  # 비교 화면의 정량 평가를 반영해 보고서 재작성
    python -m app.bench diff <이전 result.json> <새 result.json>   # 두 실행을 변형별로 견주고 나빠진 것을 짚음

* 실행된 세션은 데이터베이스에 `[bench]` 접두사가 붙은 제목으로 저장됩니다. 웹 브라우저에서 직접 검토하거나 보고서의 비교 링크(`/compare`)를
  통해 좌우로 나란히 비교하며 평가(👍/👎)를 기록할 수 있으며, `report` 명령으로 평가 결과를 보고서에 재반영할 수 있습니다.
* 변형마다 격리된 작업 공간 디렉터리를 생성합니다 (`seed` 디렉터리 지정 시 복제본 사용). 변형 간 파일 간섭이 배제됩니다.
* 비대면(unattended) 모드로 실행되므로 계획 승인 및 도구 확인 절차를 자동으로 통과합니다 (`app/headless.py`).
* 측정 지표: 실행 상태, 진행 라운드, 조기 종료 여부, 토큰 소모량, LLM 응답 시간, 소요 시간, 미해결 쟁점 수,
  도구 오류/차단 횟수, 도구 컨텍스트(불러오기·읽기 압축·압축 뒤 다시 읽기), 무응답 에이전트, 변경 파일 수, 정량 평가.

## 품질을 숫자로 (ADR-055)

토큰과 시간만으로는 "어느 조합이 일을 잘했는가" 를 고를 수 없습니다. 사람이 👍/👎 를 매기는 길은 그대로 두고,
사람 없이도 남는 두 가지를 더합니다.

* **`checks` — 결정적 검사.** 변형이 끝난 뒤 그 변형의 작업 공간과 결론을 봅니다: 파일이 있는가(`file_exists`), 파일에
  그 글이 있는가(`file_contains`), 결론에 그 글이 있는가(`conclusion_contains`), 명령이 0 으로 끝나는가(`command` —
  만든 스크립트를 돌리거나 pytest 를 돌립니다), 미해결 쟁점이 그 수 이하인가(`max_open_issues`). 같은 결과물에는 늘 같은
  답이 나옵니다.
* **`rubric` — 판정 모델의 채점.** conf 의 에이전트 하나를 판정자로 지정해, 기준마다 척도 한 단계를 고르게 합니다.
  판정자가 결정 LLM(System 1)을 쓰는 에이전트면 `score` 질문으로, 아니면 생성형 모델에게 JSON 한 줄을 받습니다.
  판정자는 변형의 이름·전략·모델을 보지 못하고 요청과 결론만 봅니다. 답을 읽지 못하면 점수를 지어내지 않고 "판정 실패"
  로 남깁니다. 모델이 매긴 점수이므로 절대값이 아니라 **같은 판정자가 매긴 변형끼리의 차이**로 읽습니다.

`matrix` 는 한 변형을 에이전트별 모델 목록의 곱으로 펼칩니다 (폐쇄망의 모델 여럿을 역할마다 바꿔 끼워 보기).
`graph` 는 편집기로 저장해 둔 그래프의 id 로, 그래프 토론 변형을 돌립니다.

`diff` 는 두 실행을 변형 이름으로 맞춰 상태·검사·채점·토큰·시간을 견줍니다. 끝난 변형이 실패로 바뀌었거나, 통과한
검사가 줄었거나, 채점이 눈에 띄게 내려갔으면 **나빠짐** 으로 짚고 끝남 값 1 을 돌려줍니다 (모델이나 프롬프트를 바꾼 뒤의 회귀 확인).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select

from app.agents.decision import DecisionQuestion
from app.agents.llm import LLMCaller
from app.agents.llm_usage import Usage, format_tokens, tool_context_line
from app.agents.personas import freeze_personas
from app.agents.pool import get_agent_pool
from app.config import DEFAULT_CONFIG_PATH, DEFAULT_SUB_DEBATE_ROUNDS, PROJECT_ROOT, get_config
from app.database.models import SessionAgentModel, SessionModel, ToolCallRecordModel, TurnModel
from app.database.session import get_engine, get_session_factory, init_db
from app.graph_store import load_graph
from app.headless import HeadlessError, create_session, turn_result
from app.mcp.pool import get_runtime_pool
from app.orchestration import context_memory as memory
from app.orchestration.engine import get_orchestrator_engine
from app.orchestration.strategies import STRATEGY_MAP
from app.plugins import load_plugins
from app.routes import compare_url

BENCH_DIR = PROJECT_ROOT / "data" / "bench"
TITLE_PREFIX = "[bench]"

CHECK_KINDS = ("file_exists", "file_contains", "conclusion_contains", "command", "max_open_issues")
COMMAND_TIMEOUT_SECONDS = 120.0
# 검사 결과에 남기는 명령 출력의 상한.
COMMAND_OUTPUT_CHARS = 400
DEFAULT_SCALE = ("미흡", "보통", "충실")
# 판정자에게 주는 결론의 상한 (글자). 넘으면 앞부분과 끝부분을 남깁니다.
JUDGE_CONCLUSION_CHARS = 24_000
# `diff` 가 채점이 내려갔다고 보는 폭 (0~1 척도).
SCORE_DROP = 0.15
# `diff` 가 토큰이 늘었다고 알리는 비율. 나빠짐으로 치지는 않습니다.
TOKEN_RISE = 0.25
GRAPH_BASE_STRATEGY = "adaptive_graph"


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
    sub_debates: bool = False
    sub_debate_rounds: int = DEFAULT_SUB_DEBATE_ROUNDS
    custom_instructions: str = ""
    # 그래프 토론이 쓸 그래프의 id (`data/graphs/<id>.json`). 적응형 그래프 토론에서는 바탕 그래프입니다.
    graph: str = ""
    # 에이전트 키 → 모델 목록. 목록들의 곱만큼 변형으로 펼쳐집니다 (`_expand`). 펼친 뒤에는 비어 있습니다.
    matrix: Dict[str, List[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    """결정적 검사 하나. `kind` 는 `CHECK_KINDS` 가운데 하나이고 `spec` 은 그 종류의 값입니다."""

    name: str
    kind: str
    spec: Any


@dataclass(frozen=True)
class Rubric:
    """판정 모델의 채점표. `criteria` 는 `(이름, 물음)` 이고 `scale` 은 낮은 단계부터입니다."""

    judge: str
    criteria: Tuple[Tuple[str, str], ...]
    scale: Tuple[str, ...] = DEFAULT_SCALE


@dataclass
class Scenario:
    name: str
    prompt: str
    variants: List[Variant]
    seed: Optional[Path] = None
    checks: List[Check] = field(default_factory=list)
    rubric: Optional[Rubric] = None


def _model_tail(model: str) -> str:
    return str(model).split("/", 1)[-1]


def _expand(variant: Variant) -> List[Variant]:
    """`matrix` 를 곱으로 펼칩니다. 없으면 그 변형 하나입니다."""
    if not variant.matrix:
        return [variant]
    keys = list(variant.matrix)
    expanded: List[Variant] = []
    for combo in itertools.product(*(variant.matrix[key] for key in keys)):
        picked = dict(zip(keys, combo))
        label = f"{variant.label} · " + ", ".join(f"{key}={_model_tail(model)}" for key, model in picked.items())
        expanded.append(Variant(**{
            **variant.__dict__, "label": label, "models": {**variant.models, **picked}, "matrix": {},
        }))
    return expanded


def _parse_checks(raw: Any, problems: List[str]) -> List[Check]:
    checks: List[Check] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, Mapping):
            problems.append(f"checks[{index}]: 객체가 아닙니다")
            continue
        kinds = [kind for kind in CHECK_KINDS if kind in item]
        if len(kinds) != 1:
            problems.append(f"checks[{index}]: 검사 종류는 {', '.join(CHECK_KINDS)} 가운데 하나만 적습니다")
            continue
        kind, spec = kinds[0], item[kinds[0]]
        name = str(item.get("name") or f"{kind} {index + 1}")
        if kind in ("file_exists",) and not (isinstance(spec, str) and spec.strip()):
            problems.append(f"checks[{index}] '{name}': 경로를 적습니다")
        elif kind == "file_contains" and not (
            isinstance(spec, Mapping) and spec.get("path") and (spec.get("text") or spec.get("regex"))
        ):
            problems.append(f"checks[{index}] '{name}': path 와 text(또는 regex)를 적습니다")
        elif kind == "conclusion_contains" and not (
            (isinstance(spec, str) and spec) or (isinstance(spec, Mapping) and (spec.get("text") or spec.get("regex")))
        ):
            problems.append(f"checks[{index}] '{name}': 찾을 글이나 regex 를 적습니다")
        elif kind == "command" and not (
            isinstance(spec, Mapping) and isinstance(spec.get("argv"), list) and spec["argv"]
            and all(isinstance(part, str) for part in spec["argv"])
        ):
            problems.append(f"checks[{index}] '{name}': argv 를 문자열 목록으로 적습니다 (셸 한 줄은 받지 않습니다)")
        elif kind == "max_open_issues" and not (isinstance(spec, int) and not isinstance(spec, bool) and spec >= 0):
            problems.append(f"checks[{index}] '{name}': 0 이상의 정수를 적습니다")
        else:
            for pattern in _regexes(spec):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"checks[{index}] '{name}': regex 가 틀렸습니다 ({exc})")
            checks.append(Check(name=name, kind=kind, spec=spec))
    if len({check.name for check in checks}) != len(checks):
        problems.append("checks 의 name 이 겹칩니다")
    return checks


def _regexes(spec: Any) -> List[str]:
    return [str(spec["regex"])] if isinstance(spec, Mapping) and spec.get("regex") else []


def _parse_rubric(raw: Any, problems: List[str]) -> Optional[Rubric]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        problems.append("rubric: 객체가 아닙니다")
        return None
    judge = str(raw.get("judge") or "").strip()
    if not judge:
        problems.append("rubric: 판정자 에이전트 키(judge)가 없습니다")
    elif get_agent_pool().get(judge) is None:
        problems.append(f"rubric: 판정자 '{judge}' 는 설정에 없는 에이전트입니다")
    criteria: List[Tuple[str, str]] = []
    for index, item in enumerate(raw.get("criteria") or []):
        name = str((item or {}).get("name") or "").strip() if isinstance(item, Mapping) else ""
        question = str((item or {}).get("question") or "").strip() if isinstance(item, Mapping) else ""
        if not name or not question:
            problems.append(f"rubric.criteria[{index}]: name 과 question 을 적습니다")
            continue
        criteria.append((name, question))
    if not criteria:
        problems.append("rubric: criteria 가 비어 있습니다")
    if len({name for name, _ in criteria}) != len(criteria):
        problems.append("rubric.criteria 의 name 이 겹칩니다")
    scale = raw.get("scale") or list(DEFAULT_SCALE)
    if not (isinstance(scale, list) and len(scale) >= 2 and all(isinstance(step, str) and step for step in scale)):
        problems.append("rubric.scale: 낮은 단계부터 두 단계 이상을 문자열로 적습니다")
        scale = list(DEFAULT_SCALE)
    return Rubric(judge=judge, criteria=tuple(criteria), scale=tuple(scale))


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
        where = f"variants[{index}] '{variant.label}'"
        strategy = STRATEGY_MAP.get(variant.strategy)
        if strategy is None:
            problems.append(f"{where}: 지원되지 않는 전략 {variant.strategy}")
        elif strategy.uses_graph_file and not variant.graph:
            problems.append(f"{where}: {variant.strategy} 는 graph 에 그래프 id 를 적어야 합니다")
        elif variant.graph and not (strategy.uses_graph_file or variant.strategy == GRAPH_BASE_STRATEGY):
            problems.append(f"{where}: graph 는 그래프 토론과 적응형 그래프 토론에서만 씁니다")
        elif variant.graph:
            try:
                load_graph(variant.graph)
            except (OSError, ValueError) as exc:
                problems.append(f"{where}: 그래프 '{variant.graph}' 를 읽지 못했습니다 ({exc})")
        if not isinstance(variant.matrix, dict) or not all(
            isinstance(models, list) and models and all(isinstance(m, str) and m for m in models)
            for models in variant.matrix.values()
        ):
            problems.append(f"{where}: matrix 는 에이전트 키마다 모델 이름 목록을 적습니다")
            variant.matrix = {}
        unknown = [key for key in (*variant.models, *variant.matrix) if get_agent_pool().get(key) is None]
        if unknown:
            problems.append(f"{where}: 설정에 없는 에이전트의 모델을 바꾸려 합니다 ({', '.join(unknown)})")
        variants.extend(_expand(variant))
    if not variants:
        problems.append("variants 가 비어 있습니다")
    if len({v.label for v in variants}) != len(variants):
        problems.append("variants 의 label 이 겹칩니다")
    checks = _parse_checks(data.get("checks"), problems)
    rubric = _parse_rubric(data.get("rubric"), problems)
    seed = None
    if data.get("seed"):
        seed = (source.parent / str(data["seed"])).resolve()
        if not seed.is_dir():
            problems.append(f"seed 폴더가 존재하지 않습니다: {seed}")
    if problems:
        raise ValueError(f"{source}: " + "; ".join(problems))
    return Scenario(
        name=str(data.get("name") or source.stem), prompt=prompt, variants=variants, seed=seed,
        checks=checks, rubric=rubric,
    )


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


# ---------------------------------------------------------------- 결정적 검사


def _inside(workspace: Path, relative: str) -> Optional[Path]:
    """작업 공간 안의 경로. 밖을 가리키면 None (검사는 그 변형의 작업 공간만 봅니다)."""
    root = workspace.resolve()
    target = (root / relative).resolve()
    return target if target == root or root in target.parents else None


def _found(haystack: str, spec: Any) -> bool:
    if isinstance(spec, str):
        return spec in haystack
    if spec.get("regex"):
        return re.search(str(spec["regex"]), haystack, re.MULTILINE) is not None
    return str(spec.get("text") or "") in haystack


def _run_command(spec: Mapping[str, Any], workspace: Path) -> Tuple[bool, str]:
    argv = [sys.executable if part == "{python}" else part for part in spec["argv"]]
    timeout = float(spec.get("timeout") or COMMAND_TIMEOUT_SECONDS)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        done = subprocess.run(
            argv, cwd=str(workspace), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"{timeout:.0f}초 안에 끝나지 않았습니다"
    except OSError as exc:
        return False, f"실행하지 못했습니다 ({type(exc).__name__}: {exc})"
    expected = int(spec.get("expect_exit", 0))
    tail = (done.stdout or done.stderr or "").strip()[-COMMAND_OUTPUT_CHARS:]
    if done.returncode != expected:
        return False, f"끝남 값 {done.returncode} (기대 {expected}) · {tail}"
    wanted = spec.get("stdout_contains")
    if wanted and str(wanted) not in (done.stdout or ""):
        return False, f"출력에 '{wanted}' 가 없습니다 · {tail}"
    return True, f"끝남 값 {done.returncode}"


def _check_one(check: Check, workspace: Path, conclusion: str, open_issues: int) -> Tuple[bool, str]:
    spec = check.spec
    if check.kind == "file_exists":
        path = _inside(workspace, spec)
        if path is None:
            return False, "작업 공간 밖의 경로입니다"
        return (True, "있음") if path.is_file() else (False, f"{spec} 이 없습니다")
    if check.kind == "file_contains":
        path = _inside(workspace, str(spec["path"]))
        if path is None:
            return False, "작업 공간 밖의 경로입니다"
        if not path.is_file():
            return False, f"{spec['path']} 이 없습니다"
        found = _found(path.read_text(encoding="utf-8", errors="replace"), spec)
        return (True, "있음") if found else (False, f"{spec['path']} 에 찾는 글이 없습니다")
    if check.kind == "conclusion_contains":
        return (True, "있음") if _found(conclusion, spec) else (False, "결론에 찾는 글이 없습니다")
    if check.kind == "command":
        return _run_command(spec, workspace)
    return (True, f"미해결 {open_issues}건") if open_issues <= spec else (False, f"미해결 {open_issues}건 (상한 {spec})")


async def run_checks(checks: Sequence[Check], workspace: Path, conclusion: str, open_issues: int) -> List[Dict[str, Any]]:
    """변형 하나의 결과물에 검사를 돌립니다. 검사 하나가 터져도 나머지는 계속합니다."""
    results: List[Dict[str, Any]] = []
    for check in checks:
        try:
            ok, detail = await asyncio.to_thread(_check_one, check, workspace, conclusion, open_issues)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"검사 중 오류 ({type(exc).__name__}: {exc})"
        results.append({"name": check.name, "kind": check.kind, "ok": bool(ok), "detail": detail})
    return results


# ---------------------------------------------------------------- 판정 모델의 채점

JUDGE_INSTRUCTION = (
    "당신은 결과물을 채점하는 판정자입니다. 아래 [요청] 에 대한 [결론] 을 기준마다 평가하세요.\n"
    "척도는 낮은 단계부터 {scale} 이고, 단계 번호는 0 부터 {top} 까지입니다.\n"
    "설명 없이 JSON 한 줄만 답하세요: {{\"scores\": {{\"<기준 이름>\": <단계 번호>, ...}}}}\n\n"
    "[기준]\n{criteria}\n\n[요청]\n{prompt}\n\n[결론]\n{conclusion}"
)


def _clip_conclusion(text: str) -> str:
    if len(text) <= JUDGE_CONCLUSION_CHARS:
        return text
    half = JUDGE_CONCLUSION_CHARS // 2
    return f"{text[:half]}\n\n…(가운데 {len(text) - 2 * half:,}자 생략)…\n\n{text[-half:]}"


def _parse_scores(content: str, rubric: Rubric) -> Dict[str, int]:
    """판정자의 답에서 기준별 단계 번호를 읽습니다. 못 읽은 기준은 빠집니다 (지어내지 않습니다)."""
    for match in re.finditer(r"\{.*\}", content or "", re.DOTALL):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        raw = data.get("scores") if isinstance(data, Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        scores: Dict[str, int] = {}
        for name, _question in rubric.criteria:
            value = raw.get(name)
            if isinstance(value, str) and value in rubric.scale:
                value = rubric.scale.index(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= round(value) < len(rubric.scale):
                scores[name] = int(round(value))
        return scores
    return {}


async def judge(scenario: Scenario, conclusion: str, engine: Any) -> Dict[str, Any]:
    """판정자에게 결론을 채점시킵니다. `{"judge", "scores", "score", "error"}` — 못 읽으면 `score` 는 None 입니다."""
    rubric = scenario.rubric
    result: Dict[str, Any] = {"judge": rubric.judge, "scores": {}, "score": None, "error": ""}
    agent = get_agent_pool().get(rubric.judge)
    if agent is None:
        result["error"] = f"판정자 '{rubric.judge}' 가 설정에 없습니다"
        return result
    if not (conclusion or "").strip():
        result["error"] = "결론이 없어 채점하지 않았습니다"
        return result
    top = len(rubric.scale) - 1
    scores: Dict[str, int] = {}
    try:
        if agent.decision_llm.enabled:
            # 결정 LLM: 기준마다 `score` 질문 하나. 한 번의 요청으로 모두 판정합니다.
            questions = {
                f"c{index}": DecisionQuestion(type="score", instructions=question, criteria=rubric.scale)
                for index, (_name, question) in enumerate(rubric.criteria)
            }
            state = f"[요청]\n{scenario.prompt}\n\n[결론]\n{_clip_conclusion(conclusion)}"
            response = await engine.llm_caller.decision_caller.evaluate(agent.decision_llm, state, questions, agent=agent)
            for index, (name, _question) in enumerate(rubric.criteria):
                answer = response.answers.get(f"c{index}")
                if answer is not None and answer.score is not None and 0 <= round(answer.score) <= top:
                    scores[name] = int(round(answer.score))
        else:
            plain = agent.model_copy(update={
                "allowed_mcp_servers": [], "allowed_skills": [],
                "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
            })
            message = JUDGE_INSTRUCTION.format(
                scale=" < ".join(rubric.scale), top=top,
                criteria="\n".join(f"- {name}: {question}" for name, question in rubric.criteria),
                prompt=scenario.prompt, conclusion=_clip_conclusion(conclusion),
            )
            content, _logs = await engine.llm_caller.call_agent(plain, [{"role": "user", "content": message}], "")
            scores = _parse_scores(content, rubric)
    except Exception as exc:  # noqa: BLE001 - 채점 실패가 변형의 결과를 버리게 하지 않습니다
        result["error"] = f"판정 실패: {type(exc).__name__}: {exc}"[:300]
        return result
    missing = [name for name, _question in rubric.criteria if name not in scores]
    if missing:
        result["error"] = f"판정 실패: 점수를 읽지 못한 기준 {', '.join(missing)}"
    result["scores"] = {name: {"step": step, "label": rubric.scale[step]} for name, step in scores.items()}
    if scores and not missing:
        result["score"] = round(sum(scores.values()) / (len(scores) * top), 3)
    return result


async def run_variant(
    scenario: Scenario, variant: Variant, run_dir: Path, engine: Any, *, dry_run: bool = False,
) -> Dict[str, Any]:
    workspace = run_dir / "workspaces" / _slug(variant.label)
    if scenario.seed is not None:
        shutil.copytree(scenario.seed, workspace)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
    created = await create_session(
        title=f"{TITLE_PREFIX} {scenario.name} · {variant.label}", strategy=variant.strategy,
        max_rounds=variant.max_rounds, agents=variant.agents, workspace_dir=str(workspace),
        converge_early=variant.converge_early, token_budget=variant.token_budget,
        sub_debates=variant.sub_debates, sub_debate_rounds=variant.sub_debate_rounds,
        custom_instructions=variant.custom_instructions, graph_id=variant.graph,
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
    conclusion = result.get("conclusion") or ""
    open_issues = memory.open_issue_count(result.get("decision_ledger") or "")
    checks = await run_checks(scenario.checks, workspace, conclusion, open_issues)
    if scenario.rubric is None:
        rubric = None
    elif dry_run:
        # 드라이런의 결론은 대본이라 채점할 것이 없습니다.
        rubric = {"judge": scenario.rubric.judge, "scores": {}, "score": None, "error": "드라이런 — 채점하지 않았습니다"}
    else:
        rubric = await judge(scenario, conclusion, engine)
    return {
        "label": variant.label,
        "strategy": variant.strategy,
        "graph": variant.graph,
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
        "open_issues": open_issues,
        "files_changed": result.get("files_changed"),
        "seconds": seconds,
        "checks": checks,
        "rubric": rubric,
        "conclusion": conclusion,
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
        results.append(await run_variant(scenario, variant, run_dir, engine, dry_run=dry_run))
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


def checks_text(variant: Mapping[str, Any]) -> str:
    """`3/4`. 검사가 없으면 빈 문자열."""
    checks = variant.get("checks") or []
    return f"{sum(1 for c in checks if c.get('ok'))}/{len(checks)}" if checks else ""


def rubric_text(variant: Mapping[str, Any]) -> str:
    """`0.75`. 채점하지 않았으면 빈 문자열, 못 읽었으면 `판정 실패`."""
    rubric = variant.get("rubric")
    if not rubric:
        return ""
    if rubric.get("score") is None:
        return "채점 안 함" if "드라이런" in str(rubric.get("error")) else "판정 실패"
    return f"{rubric['score']:.2f}"


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
        "| 변형 | 전략 | 상태 | 라운드 | 조기 종료 | 검사 | 채점 | 입력 토큰 | 출력 토큰 | LLM 시간 | 소요 시간 | 미해결 쟁점 | 도구 실패·차단 | 도구 컨텍스트 | 무응답 에이전트 | 변경 파일 | 평가 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
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
            checks_text(v), rubric_text(v),
            format_tokens(usage.prompt_tokens) if usage.prompt_tokens else "",
            format_tokens(usage.completion_tokens) if usage.completion_tokens else "",
            f"{usage.llm_seconds:.1f}초" if usage.llm_seconds else "",
            f"{v['seconds']}초", v.get("open_issues"), v.get("tool_problems"), tool_context_line(usage),
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
        if v.get("models"):
            lines += ["모델: " + ", ".join(f"{key}={model}" for key, model in v["models"].items()), ""]
        for check in v.get("checks") or []:
            lines.append(f"- {'✅' if check.get('ok') else '❌'} {check.get('name')} — {check.get('detail')}")
        rubric = v.get("rubric")
        if rubric:
            marks = ", ".join(f"{name}: {mark.get('label')}" for name, mark in (rubric.get("scores") or {}).items())
            lines.append(f"- 채점({rubric.get('judge')}): {rubric_text(v)}" + (f" — {marks}" if marks else "")
                         + (f" · {rubric['error']}" if rubric.get("error") else ""))
        if v.get("checks") or rubric:
            lines.append("")
        lines += [(v.get("conclusion") or "_결론 없음_").strip(), ""]
    report = result_path.with_name("report.md")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


# ---------------------------------------------------------------- 두 실행 견주기


def _passed(variant: Mapping[str, Any]) -> Optional[int]:
    checks = variant.get("checks") or []
    return sum(1 for c in checks if c.get("ok")) if checks else None


def _score(variant: Mapping[str, Any]) -> Optional[float]:
    rubric = variant.get("rubric") or {}
    return rubric.get("score")


def _arrow(before: Any, after: Any) -> str:
    return _cell(after) if before == after else f"{_cell(before)} → {_cell(after)}"


def diff_runs(before_path: str | Path, after_path: str | Path) -> Tuple[str, List[str]]:
    """두 `result.json` 을 변형 이름으로 맞춰 견줍니다. `(글, 나빠진 것들)` 을 돌려줍니다.

    나빠짐으로 치는 것: 끝났던 변형이 끝나지 못함, 통과한 검사 수가 줄어듦, 채점이 `SCORE_DROP` 이상 내려감.
    토큰이 `TOKEN_RISE` 넘게 늘면 표에 알리기만 합니다 — 품질이 오른 값일 수 있습니다.
    """
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    old = {v["label"]: v for v in before["variants"]}
    new = {v["label"]: v for v in after["variants"]}
    regressions: List[str] = []
    lines = [
        f"# MADO Bench 비교 — {after.get('scenario')}",
        "",
        f"- 이전: `{before.get('run_id')}` ({before.get('created_at')})",
        f"- 이후: `{after.get('run_id')}` ({after.get('created_at')})",
        "",
        "| 변형 | 상태 | 검사 | 채점 | 토큰 | 소요 시간 | 판정 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if before.get("prompt") != after.get("prompt"):
        lines.insert(4, "- ⚠️ 두 실행의 요청(prompt)이 다릅니다. 같은 일을 시킨 비교가 아닙니다.")
    for label in [*new, *(name for name in old if name not in new)]:
        a, b = old.get(label), new.get(label)
        if a is None or b is None:
            lines.append(f"| {_cell(label)} | {'이후에만 있음' if a is None else '이전에만 있음'} | - | - | - | - | - |")
            continue
        notes: List[str] = []
        if a.get("status") == "completed" and b.get("status") != "completed":
            notes.append("끝나지 못함")
        passed_a, passed_b = _passed(a), _passed(b)
        if passed_a is not None and passed_b is not None and passed_b < passed_a:
            notes.append("통과한 검사가 줄어듦")
        score_a, score_b = _score(a), _score(b)
        if score_a is not None and score_b is not None and score_a - score_b >= SCORE_DROP:
            notes.append(f"채점 {score_a - score_b:.2f} 내려감")
        tokens_a = Usage.of(a.get("usage")).total_tokens
        tokens_b = Usage.of(b.get("usage")).total_tokens
        verdict = "나빠짐: " + ", ".join(notes) if notes else "같거나 나아짐"
        if tokens_a and tokens_b > tokens_a * (1 + TOKEN_RISE):
            verdict += f" · 토큰 {tokens_b / tokens_a - 1:+.0%}"
        if notes:
            regressions.append(f"{label}: {', '.join(notes)}")
        lines.append("| " + " | ".join((
            _cell(label), _arrow(a.get("status"), b.get("status")),
            _arrow(checks_text(a), checks_text(b)), _arrow(rubric_text(a), rubric_text(b)),
            _arrow(format_tokens(tokens_a) if tokens_a else "", format_tokens(tokens_b) if tokens_b else ""),
            _arrow(f"{a.get('seconds')}초", f"{b.get('seconds')}초"), verdict,
        )) + " |")
    lines += ["", f"나빠진 변형 {len(regressions)}개." if regressions else "나빠진 변형이 없습니다.", ""]
    return "\n".join(lines), regressions


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
    diff_cmd = sub.add_parser("diff", help="두 실행을 견주고 나빠진 변형이 있으면 끝남 값 1 을 돌려줍니다")
    diff_cmd.add_argument("before", help="이전 실행의 result.json")
    diff_cmd.add_argument("after", help="새 실행의 result.json")
    args = parser.parse_args(argv)

    if args.command == "diff":
        # 파일 둘만 읽습니다. 설정도 DB 도 필요 없습니다.
        try:
            text, regressions = diff_runs(args.before, args.after)
        except (OSError, ValueError, KeyError) as exc:
            print(f"[bench] 결과 파일을 읽지 못했습니다: {exc}", file=sys.stderr)
            return 2
        print(text)
        return 1 if regressions else 0

    # DB 엔진은 처음 부른 쪽의 주소로 고정됩니다. 설정의 주소로 먼저 묶습니다.
    cfg = get_config(reload=True, config_path=args.config)
    get_engine(cfg.app.db_url)
    await init_db(cfg.app.db_url)
    load_plugins()
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
