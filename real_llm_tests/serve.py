"""실제 LLM 시험이 남긴 실행 폴더를 화면으로 열어 봅니다.

    dist\\MultiAgentDebateOrchestration_bundle\\python_runtime\\python.exe real_llm_tests\\serve.py <실행 폴더> [--port 8799]

시험이 만든 대화(실제 모델이 쓴 발언·장부·도구 기록)를 그 시험이 쓴 것과 같은 코드·DB·작업 공간으로 띄웁니다.
라운드 묶음, 발언 번호 칩과 인용 링크, 요약 띠, 사용량 차트, 변경 내역 비교처럼 화면에서만 보이는 것을 확인할 때 씁니다.
대상 폴더(번들)의 자료는 건드리지 않습니다 — 실행 폴더의 미러 루트에서 뜹니다.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Harness, default_target  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="실제 LLM 시험의 실행 폴더를 화면으로 엽니다")
    parser.add_argument("run_dir", help="시험이 남긴 실행 폴더 (root/, mado.db 가 있는 곳)")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    harness = Harness(default_target(), run_dir)
    if not (harness.root / "app" / "main.py").is_file() or not harness.conf_path.is_file():
        raise SystemExit(f"실행 폴더가 아닙니다 (root/app 또는 root/conf.json 이 없음): {run_dir}")
    harness._environment()  # noqa: SLF001 - 시험과 같은 환경(대상의 .env, MCP 서버 위치)
    os.chdir(harness.root)
    sys.path.insert(0, str(harness.root))
    sys.argv = ["app.main", "--config", str(harness.conf_path), "--host", "127.0.0.1",
                "--port", str(args.port), "--no-reload"]
    from app.main import start

    print(f"[real-llm] {run_dir} → http://127.0.0.1:{args.port}")
    start()


if __name__ == "__main__":
    main()
