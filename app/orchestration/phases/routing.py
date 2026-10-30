"""오케스트레이터의 지명·분배 응답 읽기.

`orchestrator_led` 의 발언자 지명과 `parallel_dispatch` 의 태스크 분배는 오케스트레이터에게 JSON 한 줄을
받습니다. 모델이 형식을 어겨도 토론이 멈추지 않도록, 읽을 수 있는 만큼 읽고 못 읽으면 빈 결과를
돌려줍니다 (부른 쪽이 기본 순서로 물러섭니다).
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

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


def parse_sub_debate(
    content: str, candidates: List[Agent]
) -> Optional[Tuple[List[Agent], str, str]]:
    """서브 토론을 열겠다는 답이면 (두 전문가, 쟁점, 사유). 아니면 None.

    지명·분배와 달리 본문에서 에이전트 키를 긁어 오지 않습니다. 형식을 어긴 답에서 추측해 열면 사람이 고른 적
    없는 비용이 나갑니다. 열지 않아도 토론은 그대로 돕니다.
    """
    block = re.search(r"\{.*\}", content or "", re.DOTALL)
    if block is None:
        return None
    try:
        data = json.loads(block.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("open") is not True:
        return None
    by_key = {a.key: a for a in candidates}
    raw = data.get("agents")
    keys = list(dict.fromkeys(str(k).strip() for k in raw)) if isinstance(raw, list) else []
    issue = str(data.get("issue") or "").strip()
    if len(keys) != 2 or not all(k in by_key for k in keys) or not issue:
        return None
    return [by_key[k] for k in keys], issue, str(data.get("reason") or "").strip()
