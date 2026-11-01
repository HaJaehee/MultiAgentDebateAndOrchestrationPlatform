"""두 소스 코드의 차이점(Diff)을 파싱하여 UI 렌더링용 라인 자료구조로 변환하는 순수 함수 모듈 (ADR-043).

UI 프레임워크에 종속되지 않는 독립적인 파서 및 비교 연산 함수들로 구성됩니다. 턴 파일 변경 내역은 parse_unified로 git diff 출력을 파싱하고, 산출물 뷰어는 diff_texts로 두 코드 문자열을 직접 비교합니다. Unified(통합) 및 Split(좌우) 뷰는 동일한 정규화 모델(FileDiff)을 다르게 렌더링합니다 (`app/ui/components/diff_viewer.py`).

* 라인은 오직 `\n` 기준으로만 분할합니다. str.splitlines는 폼 피드(`\f`)나 `\u2028` 등에서도 줄바꿈을 수행하여 헝크 라인 수가 불일치할 수 있기 때문입니다.
* 헝크 본문은 헤더에 선언된 라인 수만큼만 정확히 파싱합니다. 삭제된 SQL 주석 라인 `-- x` 등은 diff 출력에서 `--- x` 형태로 나타나므로, 이를 파일 헤더로 오인하여 유실하지 않도록 방지합니다.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

ADD, DEL, SAME = "+", "-", " "

# 변경 라인 전후의 컨텍스트 라인 수 (git diff 기본값인 3과 동일).
CONTEXT_LINES = 3

# 대용량 출력 절삭 시 덧붙여지는 절삭 안내 문구의 접두사 (헝크 본문 라인은 ` `·`+`·`-` 로만 시작).
TRUNCATION_MARK = "…"

# 최대 비교 허용 문자 수. difflib 연산은 순수 파이썬 구현체이므로 대용량 입력 시 CPU 및 GIL 점유율이 급증하여 이벤트 루프를 블로킹할 수 있습니다.
MAX_COMPARE_CHARS = 400_000

# 인라인 단어 강조(Word Diff) 제한 임계치. 단일 라인이 너무 길거나(압축 코드 등) 헝크 크기가 초과되면 성능 보호를 위해 라인 단위 하이라이트만 적용합니다.
MAX_WORD_DIFF_CHARS = 2_000
MAX_ALIGN_LINES = 200

# 삭제 라인과 수정 라인 간 페어링 탐색 윈도우 크기 및 최소 매칭 유사도 임계치.
ALIGN_WINDOW = 8
SIMILAR_RATIO = 0.5

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
_WORD = re.compile(r"\w+")


@dataclass
class DiffLine:
    kind: str  # ADD · DEL · SAME
    text: str
    old_no: Optional[int] = None
    new_no: Optional[int] = None
    # 라인 내부에서 변경된 문자 구간(start, end). 페어링된 유사 라인에만 부여됩니다.
    spans: List[Tuple[int, int]] = field(default_factory=list)
    no_newline: bool = False


# Split(좌우) 뷰의 단일 행: (left_line, right_line). 공백 행은 None이며, 변경 없는 행은 동일 인스턴스입니다.
Row = Tuple[Optional[DiffLine], Optional[DiffLine]]


@dataclass
class DiffHunk:
    header: str
    lines: List[DiffLine] = field(default_factory=list)
    rows: List[Row] = field(default_factory=list)


@dataclass
class FileDiff:
    hunks: List[DiffHunk] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    binary: bool = False
    truncated: bool = False  # 출력이 임계치를 초과하여 전반부만 절삭 수집됨
    too_large: bool = False  # 크기 임계치 초과로 비교 연산이 생략됨 (`MAX_COMPARE_CHARS`)


def parse_unified(text: str) -> FileDiff:
    """Unified diff 형식의 텍스트를 파싱합니다. 파일 헤더(`diff --git` 등)는 건너뜁니다."""
    diff = FileDiff()
    hunk: Optional[DiffHunk] = None
    old_no = new_no = old_left = new_left = 0
    for raw in (text or "").split("\n"):
        if raw.startswith("\\"):  # `\ No newline at end of file`: 직전 라인의 속성
            if hunk is not None and hunk.lines:
                hunk.lines[-1].no_newline = True
            continue
        if hunk is not None and (old_left > 0 or new_left > 0):
            tag = raw[:1]
            if tag == DEL and old_left > 0:
                hunk.lines.append(DiffLine(DEL, raw[1:], old_no=old_no))
                old_no, old_left, diff.removed = old_no + 1, old_left - 1, diff.removed + 1
            elif tag == ADD and new_left > 0:
                hunk.lines.append(DiffLine(ADD, raw[1:], new_no=new_no))
                new_no, new_left, diff.added = new_no + 1, new_left - 1, diff.added + 1
            elif tag == SAME and old_left > 0 and new_left > 0:
                hunk.lines.append(DiffLine(SAME, raw[1:], old_no=old_no, new_no=new_no))
                old_no, new_no, old_left, new_left = old_no + 1, new_no + 1, old_left - 1, new_left - 1
            else:
                # 잔여 라인이 존재하나 헝크 본문 형식이 아닌 경우, 출력이 중간 절삭된 지점으로 간주합니다.
                diff.truncated = True
                old_left = new_left = 0
            continue
        match = _HUNK.match(raw)
        if match:
            hunk = DiffHunk(header=raw)
            diff.hunks.append(hunk)
            old_no, new_no = int(match[1]), int(match[3])
            old_left = int(match[2]) if match[2] is not None else 1
            new_left = int(match[4]) if match[4] is not None else 1
        elif raw.startswith(("Binary files ", "GIT binary patch")):
            diff.binary = True
        elif raw.startswith(TRUNCATION_MARK):
            diff.truncated = True
    for each in diff.hunks:
        _pair_lines(each)
    return diff


def diff_texts(old: str, new: str, context: int = CONTEXT_LINES) -> FileDiff:
    """두 텍스트 간의 Diff를 계산합니다. 문자 수가 임계치를 초과하면 연산을 생략하고 `too_large=True`를 반환합니다."""
    if len(old) + len(new) > MAX_COMPARE_CHARS:
        return FileDiff(too_large=True)
    lines = difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=context)
    return parse_unified("\n".join(lines))


def similarity(old: str, new: str) -> float:
    """두 코드의 어휘(Word Token) 유사도를 계산합니다 (0.0 ~ 1.0). 비교 기준 추천을 위한 통계 지표입니다.

    라인 단위 비교는 단일 인자가 바뀐 명령어도 완전 불일치로 처리되고, 문자 단위 비교는 의미 없는 노이즈가 과대평가되는 한계를 보완합니다.
    """
    matcher = difflib.SequenceMatcher(None, _WORD.findall(old), _WORD.findall(new), autojunk=False)
    return matcher.quick_ratio()


def _pair_lines(hunk: DiffHunk) -> None:
    """Split(좌우) 뷰의 매칭 행 목록을 구성하고, 페어링된 유사 라인의 인라인 변경 단어 위치를 계산합니다."""
    lines, index = hunk.lines, 0
    while index < len(lines):
        if lines[index].kind == SAME:
            _chomp(lines[index])
            hunk.rows.append((lines[index], lines[index]))
            index += 1
            continue
        dels: List[DiffLine] = []
        adds: List[DiffLine] = []
        while index < len(lines) and lines[index].kind == DEL:
            dels.append(lines[index])
            index += 1
        while index < len(lines) and lines[index].kind == ADD:
            adds.append(lines[index])
            index += 1
        hunk.rows.extend(_pair_block(dels, adds))


def _pair_block(dels: Sequence[DiffLine], adds: Sequence[DiffLine]) -> List[Row]:
    """단일 수정 헝크 블록의 행들을 정렬합니다. 매칭 쌍은 동일 행에 배치하고, 단독 추가/삭제 라인은 상단부터 순차 배치합니다."""
    rows: List[Row] = []
    old_at = new_at = 0
    for i, j in [*_align(dels, adds), (len(dels), len(adds))]:
        lone_old, lone_new = dels[old_at:i], adds[new_at:j]
        for line in (*lone_old, *lone_new):
            _chomp(line)
        for k in range(max(len(lone_old), len(lone_new))):
            rows.append((
                lone_old[k] if k < len(lone_old) else None,
                lone_new[k] if k < len(lone_new) else None,
            ))
        if i < len(dels):
            _mark_words(dels[i], adds[j])
            rows.append((dels[i], adds[j]))
        old_at, new_at = i + 1, j + 1
    return rows


def _align(dels: Sequence[DiffLine], adds: Sequence[DiffLine]) -> List[Tuple[int, int]]:
    """유사 라인 간의 순서를 보존하며 페어링합니다. 중간에 라인이 삽입되더라도 후속 라인의 매칭 순서가 왜곡되지 않도록 유지합니다."""
    if not dels or not adds:
        return []
    # 개행 문자(CRLF/LF)만 변경된 경우 파일 전체 헝크를 1:1 매칭으로 직결 페어링합니다.
    if len(dels) == len(adds) and all(d.text.rstrip("\r") == a.text.rstrip("\r") for d, a in zip(dels, adds)):
        return [(i, i) for i in range(len(dels))]
    if len(dels) > MAX_ALIGN_LINES or len(adds) > MAX_ALIGN_LINES:
        return []
    pairs: List[Tuple[int, int]] = []
    start = 0
    for i, line in enumerate(dels):
        for j in range(start, min(start + ALIGN_WINDOW, len(adds))):
            if _similar(line.text, adds[j].text):
                pairs.append((i, j))
                start = j + 1
                break
    return pairs


def _similar(old: str, new: str) -> bool:
    if len(old) > MAX_WORD_DIFF_CHARS or len(new) > MAX_WORD_DIFF_CHARS:
        return False
    if old.strip() == new.strip():
        return True
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return (
        matcher.real_quick_ratio() >= SIMILAR_RATIO
        and matcher.quick_ratio() >= SIMILAR_RATIO
        and matcher.ratio() >= SIMILAR_RATIO
    )


def _mark_words(old: DiffLine, new: DiffLine) -> None:
    """페어링된 두 라인 간의 인라인 단어 Diff 위치를 추출합니다. 끝의 `\\r` 은 한쪽에만 존재하는 경우에 한해 가시화합니다."""
    if old.text.endswith("\r") and new.text.endswith("\r"):
        _chomp(old)
        _chomp(new)
    if len(old.text) > MAX_WORD_DIFF_CHARS or len(new.text) > MAX_WORD_DIFF_CHARS:
        return
    old_tokens, new_tokens = _TOKEN.findall(old.text), _TOKEN.findall(new.text)
    old_at, new_at = _offsets(old_tokens), _offsets(new_tokens)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            old.spans.append((old_at[i1], old_at[i2]))
        if j2 > j1:
            new.spans.append((new_at[j1], new_at[j2]))


def _offsets(tokens: Sequence[str]) -> List[int]:
    offsets = [0]
    for token in tokens:
        offsets.append(offsets[-1] + len(token))
    return offsets


def _chomp(line: DiffLine) -> None:
    if line.text.endswith("\r"):
        line.text = line.text[:-1]
