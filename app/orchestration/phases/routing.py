"""오케스트레이터의 지명·분배 응답 읽기.

`orchestrator_led` 의 발언자 지명과 `parallel_dispatch` 의 태스크 분배는 오케스트레이터에게 JSON 한 줄을
받습니다. 모델이 형식을 어겨도 토론이 멈추지 않도록, 읽을 수 있는 만큼 읽고 못 읽으면 빈 결과를
돌려줍니다 (부른 쪽이 기본 순서로 물러섭니다).
"""

from __future__ import annotations

import json
import re
from typing import List, Tuple

from app.agents.base import Agent


def parse_speaker_selection(
    content: str, candidates: List[Agent]
) -> Tuple[List[Agent], str]:
    """오케스트레이터 응답에서 지명된 에이전트 목록과 지명 사유를 추출합니다."""
    by_key = {a.key: a for a in candidates}
    reason = ""
    keys: List[str] = []

    block = re.search(r"\{.*\}", content or "", re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            raw = data.get("speakers")
            if isinstance(raw, list):
                keys = [str(k).strip() for k in raw]
            reason = str(data.get("reason") or "").strip()

    if not any(k in by_key for k in keys):
        found = []
        for key in by_key:
            match = re.search(rf"\b{re.escape(key)}\b", content or "")
            if match:
                found.append((match.start(), key))
        keys = [key for _, key in sorted(found)]

    picked: List[Agent] = []
    for key in keys:
        agent = by_key.get(key)
        if agent is not None and agent not in picked:
            picked.append(agent)
    return picked, reason


def parse_assignments(
    content: str, candidates: List[Agent]
) -> Tuple[List[Tuple[Agent, str]], str]:
    """응답에서 (에이전트, 태스크) 목록과 분배 사유를 뽑습니다."""
    by_key = {a.key: a for a in candidates}
    reason = ""
    pairs: List[Tuple[str, str]] = []

    block = re.search(r"\{.*\}", content or "", re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            reason = str(data.get("reason") or "").strip()
            raw = data.get("assignments")
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        key = str(item.get("agent") or item.get("key") or "").strip()
                        task = str(item.get("task") or item.get("instruction") or "").strip()
                        pairs.append((key, task))
                    elif isinstance(item, str):
                        pairs.append((item.strip(), ""))

    if not any(key in by_key for key, _ in pairs):
        picked, scraped = parse_speaker_selection(content, candidates)
        reason = reason or scraped
        pairs = [(a.key, "") for a in picked]

    assignments: List[Tuple[Agent, str]] = []
    seen = set()
    for key, task in pairs:
        agent = by_key.get(key)
        if agent is None or agent.key in seen:
            continue
        seen.add(agent.key)
        assignments.append((agent, task))
    return assignments, reason
