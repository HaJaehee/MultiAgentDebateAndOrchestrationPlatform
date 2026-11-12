"""모델 적합성 점검 CLI (ADR-054) — 새 모델을 반입한 날 서버를 기동하기 전에 콘솔에서 실행합니다.

    python -m app.modelcheck                    # conf.json의 모든 연결 점검
    python -m app.modelcheck --agent coder      # 지정한 에이전트가 사용하는 연결만 점검(복수 지정 가능)
    python -m app.modelcheck --json             # 기계 판독용 JSON 출력
    python -m app.modelcheck --config other.json

단일 연결당 짧은 요청 약 8건을 순차적으로 전송합니다. 결과는 화면의 모델 연결 상태 창과 동일한 경로
(`data/modelcheck/<연결 id>.json`)에 저장됩니다. 실제 검사 및 판정 로직은 `app/agents/model_check.py`에 있습니다.

종료 코드: 실패한 항목이 없으면 0, 실패가 있으면 1, 인자나 설정이 잘못되었으면 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List

from app.agents import model_check
from app.agents.model_status import collect_targets
from app.config import DEFAULT_CONFIG_PATH, get_config


async def _main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.modelcheck", description="MADO 모델 적합성 점검")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="conf.json 경로")
    parser.add_argument("--agent", action="append", default=[], metavar="KEY",
                        help="지정한 에이전트가 사용하는 연결만 점검합니다 (복수 지정 가능)")
    parser.add_argument("--all", action="store_true", help="모든 연결을 점검합니다 (기본값)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 형식으로 출력합니다")
    parser.add_argument("--timeout", type=float, default=model_check.CHECK_TIMEOUT_SECONDS,
                        help="요청당 대기 제한 시간(초)")
    args = parser.parse_args(argv)

    try:
        config = get_config(reload=True, config_path=args.config)
    except Exception as exc:  # noqa: BLE001 - 설정 로드 실패 원인을 그대로 출력합니다
        print(f"[modelcheck] 설정을 읽지 못했습니다: {exc}", file=sys.stderr)
        return 2

    if args.agent and not args.all:
        try:
            targets = model_check.targets_for_agents(args.agent, config)
        except KeyError as exc:
            print(f"[modelcheck] 설정에 없는 에이전트입니다: {exc.args[0]}", file=sys.stderr)
            return 2
    else:
        targets = collect_targets(config)
    if not targets:
        print("[modelcheck] 점검할 연결이 없습니다.", file=sys.stderr)
        return 2

    profiles = []
    for target in targets:
        if not args.json:
            print(f"[modelcheck] {target.model} 점검 중 …", flush=True)
        profile = await model_check.check_target(target, config, timeout_seconds=args.timeout)
        model_check.save_profile(profile)
        profiles.append(profile)
        if not args.json:
            print(model_check.format_profile(profile), flush=True)
            print()

    if args.json:
        print(json.dumps([p.to_dict(detail=True) for p in profiles], ensure_ascii=False, indent=2))
    else:
        print(f"[modelcheck] 기록: {model_check.modelcheck_dir()}")
    return 1 if any(p.count(model_check.FAIL) for p in profiles) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
