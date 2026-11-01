"""대화 세션 내 발언 이력에서 코드 버전을 추출하여 이전 라운드 및 이전 턴과의 Diff 비교 대상을 제공하는 모듈 (ADR-043).

코드 산출물 메타데이터에는 파일 경로와 턴 ID가 누적되지 않으며, 매 턴마다 각 전문가의 최종 발언 코드 블록만 수집됩니다.
따라서 라운드별 점진적 코드 변화는 전체 발언 이력에서 재수집합니다. 코드 간의 명시적 계통 정보가 없으므로 어휘 유사도를
기반으로 비교 기준을 자동 추천하며, 사용자가 UI에서 유연하게 변경할 수 있도록 지원합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.agents.reasoning import strip_reasoning_trace
from app.code_diff import similarity
from app.orchestration.artifacts import CODE_LANGUAGES, extract_code_blocks
from app.orchestration.turns import in_main_record
from app.timestamps import to_local


@dataclass(frozen=True)
class CodeVersion:
    content: str
    language: str
    label: str
    created_at: Any = None


def _language(name: Optional[str]) -> str:
    return "python" if name == "py" else (name or "")


def speech_code_versions(speeches: Iterable[Any], turn_numbers: Mapping[str, int]) -> List[CodeVersion]:
    """발언 이력(시간순)에서 코드 블록을 버전 단위로 추출합니다. 추출 기준은 최종 산출물 수집(`debate_code_artifacts`)과 동일합니다."""
    versions: List[CodeVersion] = []
    for speech in speeches:
        if not in_main_record(speech):
            continue
        blocks = [
            b for b in extract_code_blocks(strip_reasoning_trace(speech.content))
            if b["language"] in CODE_LANGUAGES
        ]
        for index, block in enumerate(blocks, start=1):
            parts: List[str] = []
            turn = turn_numbers.get(speech.turn_id)
            if turn:
                parts.append(f"턴 {turn}")
            if speech.round_number:
                parts.append(f"{speech.round_number}단계" if speech.graph_node_id else f"R{speech.round_number}")
            parts.append(speech.sender_name)
            if len(blocks) > 1:
                parts.append(f"코드 {index}/{len(blocks)}")
            if speech.created_at:
                parts.append(to_local(speech.created_at).strftime("%m-%d %H:%M"))
            versions.append(CodeVersion(
                content=block["code"], language=_language(block["language"]),
                label=" · ".join(parts), created_at=speech.created_at,
            ))
    return versions


def artifact_versions(artifacts: Iterable[Dict[str, Any]]) -> List[CodeVersion]:
    """산출물 뷰어의 코드 산출물 목록을 버전으로 변환합니다. 발언 이력을 조회할 수 없는 경우에도 턴 간 코드 비교가 가능하도록 지원합니다."""
    return [
        CodeVersion(
            content=art.get("content", ""), language=_language(art.get("language")),
            label=art.get("title", ""), created_at=art.get("created_at"),
        )
        for art in artifacts if art.get("artifact_type") == "code"
    ]


def earlier_versions(
    content: str, language: Optional[str], versions: Sequence[CodeVersion], created_at: Any = None,
) -> List[Tuple[CodeVersion, float]]:
    """대상 코드(`content`)와 비교 가능한 이전 버전 목록 및 유사도를 반환합니다. 최신순 정렬, 동일 언어 한정, 중복 코드는 단일 항목만 포함합니다.

    `created_at`은 해당 산출물이 생성된 시점입니다. 해당 시점 이후에 작성된 코드는 이전 버전이 아니며, 같은 턴 내에서도 해당 코드가
    포함된 발언 이후의 코드는 이전 버전에서 제외됩니다. 발언을 특정할 수 없는 경우(재생성된 발언 등) 생성 시각(timestamp)을 기준으로 필터링합니다.
    """
    target = content.strip()
    lang = _language(language)
    pool = [
        v for v in versions
        if v.language == lang and (created_at is None or v.created_at is None or v.created_at <= created_at)
    ]
    own = next((i for i in range(len(pool) - 1, -1, -1) if pool[i].content.strip() == target), None)
    if own is not None:
        pool = pool[:own]
    seen = {target}
    found: List[Tuple[CodeVersion, float]] = []
    for version in reversed(pool):
        key = version.content.strip()
        if key not in seen:
            seen.add(key)
            found.append((version, similarity(version.content, content)))
    return found
