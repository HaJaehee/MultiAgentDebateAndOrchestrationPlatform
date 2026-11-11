"""실제 LLM 시험의 pytest 설정.

이 폴더의 시험은 **실제 LLM(대상 conf 의 모델 — 개발 PC 에서는 DeepSeek 와 Jev)을 호출해 돈과 시간이 듭니다.**
그래서 켜야만 돕니다.

    set MADO_REAL_LLM=1
    dist\\MultiAgentDebateOrchestration_bundle\\python_runtime\\python.exe -m pytest real_llm_tests -x -q

`MADO_REAL_LLM` 이 없으면 아무 것도 수집하지 않습니다 (저장소 루트에서 `pytest -q` 를 돌려도 단위 테스트만 돕니다).
자세한 내용은 `README.md`.
"""

import os
import sys
from pathlib import Path

import pytest

ENABLED = os.environ.get("MADO_REAL_LLM", "").strip() not in ("", "0", "false", "no")

if not ENABLED:
    collect_ignore_glob = ["test_*.py"]

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HARNESS = None


def pytest_configure(config):
    config.addinivalue_line("markers", "adr(number): 이 시험이 확인하는 ADR 번호")
    config.addinivalue_line(
        "markers", "acceptance: 새 모델을 들였을 때 먼저 돌리는 최소 묶음 (`-m acceptance`, 연결·적합성·토론 한 턴·도구)",
    )


@pytest.fixture(scope="session")
def mado():
    """대상(번들) 코드를 실제 연결로 세운 하네스. 세션에 하나입니다."""
    global _HARNESS
    from harness import Harness, default_code, default_run_dir, default_target

    target = default_target()
    harness = Harness(target, default_run_dir(), default_code(target)).bootstrap()
    _HARNESS = harness
    identity = harness.code_identity
    print(f"\n[real-llm] 대상(환경): {harness.target}\n[real-llm] 코드: {harness.code}"
          f"{'' if identity.get('code_is_target_code') else ' (대상의 app/ 과 다름)'}\n"
          f"[real-llm] 실행 폴더: {harness.run_dir}\n"
          f"[real-llm] 모델: {identity.get('model')} · 미러 일치: {identity.get('mirror_matches_code')}"
          + (f"\n[real-llm] 예제 설정에서 채운 에이전트: {', '.join(identity['supplemented_agents'])}"
             if identity.get("supplemented_agents") else ""))
    yield harness
    harness.shutdown()
    print(f"\n[real-llm] 보고서: {harness.run_dir / 'report.json'}")


@pytest.fixture(autouse=True)
def _per_test(request):
    """시험 이름을 확인 기록에 남기고, 끝날 때마다 보고서를 다시 씁니다 (중간에 멈춰도 남게)."""
    if _HARNESS is not None:
        _HARNESS.current_test = request.node.nodeid
    yield
    if _HARNESS is not None:
        _HARNESS.write_report()
