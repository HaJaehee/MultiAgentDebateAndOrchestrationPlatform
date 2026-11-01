"""대화 안에 나온 코드의 버전들: 산출물을 앞선 라운드·턴의 코드와 견주는 재료 (ADR-043).

코드 산출물에는 파일 이름도 턴 id 도 없고, 턴마다 전문가별 마지막 발언의 코드만 올라옵니다. 그래서 라운드별
버전은 발언 기록에서 다시 모읍니다. 어느 코드가 어느 코드의 이전 모습인지 표시가 없으므로 유사도로 비교 기준을
제안하고, 화면에서 사람이 바꿀 수 있게 합니다.
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
    """발언 기록(시간순)의 코드 블록을 버전으로 모읍니다. 기준은 산출물 수집(`debate_code_artifacts`)과 같습니다."""
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
    """뷰어에 있는 코드 산출물을 버전으로. 발언 기록을 읽지 못해도 턴끼리는 견줄 수 있게 합니다."""
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
    """`content` 와 견줄 앞선 버전과 유사도. 최근 것부터, 같은 언어만, 같은 코드는 한 번만.

    `created_at` 은 산출물이 만들어진 시각입니다. 그 뒤의 코드는 이전 버전이 아니고, 같은 턴이라도 그 코드가
    나온 발언 뒤의 것은 이전 버전이 아닙니다. 그 발언을 찾지 못하면(다시 실행한 발언) 시각으로만 거릅니다.
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
