"""두 코드의 차이를 화면에 그리기 좋은 줄 목록으로 만듭니다 (ADR-043).

화면을 모르는 순수 함수만 둡니다. 턴 변경 창은 `git diff` 결과를 `parse_unified` 로, 산출물 뷰어는 두 코드를
`diff_texts` 로 읽습니다. 통합·좌우 보기는 같은 결과를 다르게 그릴 뿐입니다 (`app/ui/components/diff_viewer.py`).

* 줄은 `\\n` 으로만 나눕니다. `str.splitlines` 는 폼 피드(`\\f`)나 `\\u2028` 에서도 끊어, 헝크의 줄 수가 어긋납니다.
* 헝크는 머리글의 줄 수만큼만 읽습니다. 지운 줄 `-- x` 는 diff 에 `--- x` 로 나오는데, 파일 머리글로 오인하면
  그 줄이 사라집니다.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

ADD, DEL, SAME = "+", "-", " "

# 바뀐 줄 앞뒤로 함께 보여 줄 줄 수 (`git diff` 기본값과 같습니다).
CONTEXT_LINES = 3

# `file_diff` 가 긴 출력을 자르고 덧붙이는 안내의 첫 글자. 헝크 본문의 줄은 ` `·`+`·`-` 로만 시작합니다.
TRUNCATION_MARK = "…"

# 이보다 길면 비교하지 않습니다. difflib 은 파이썬 코드라 입력이 커지면 이벤트 루프와 GIL 을 오래 다툽니다.
MAX_COMPARE_CHARS = 400_000

# 줄 안 강조의 한도. 한 줄이 이보다 길거나(압축된 코드) 한 묶음이 이보다 크면 줄 단위 색만 칠합니다.
MAX_WORD_DIFF_CHARS = 2_000
MAX_ALIGN_LINES = 200

# 지운 줄의 짝을 찾을 때 살펴볼 추가 줄 수와, 짝으로 볼 최소 유사도.
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
    # 줄 안에서 바뀐 구간 (글자 위치). 비슷한 짝이 있는 줄에만 있습니다.
    spans: List[Tuple[int, int]] = field(default_factory=list)
    no_newline: bool = False


# 좌우 보기의 한 행: (왼쪽 줄, 오른쪽 줄). 없는 쪽은 None, 같은 줄은 양쪽에 같은 객체입니다.
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
    truncated: bool = False  # 출력이 길어 앞부분만 있습니다
    too_large: bool = False  # 비교를 시도하지 않았습니다 (`MAX_COMPARE_CHARS`)


def parse_unified(text: str) -> FileDiff:
    """unified diff 글을 읽습니다. 머리글(`diff --git` 등)은 건너뜁니다."""
    diff = FileDiff()
    hunk: Optional[DiffHunk] = None
    old_no = new_no = old_left = new_left = 0
    for raw in (text or "").split("\n"):
        if raw.startswith("\\"):  # `\ No newline at end of file`: 바로 앞 줄의 속성입니다
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
                # 남은 줄이 있는데 본문이 아닙니다. 출력이 헝크 중간에서 잘린 자리입니다.
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
    """두 글의 차이. 너무 길면 시도하지 않고 `too_large` 로 알립니다."""
    if len(old) + len(new) > MAX_COMPARE_CHARS:
        return FileDiff(too_large=True)
    lines = difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=context)
    return parse_unified("\n".join(lines))


def similarity(old: str, new: str) -> float:
    """두 코드가 얼마나 같은 낱말로 이루어졌는가 (0~1). 비교 기준을 고르는 근사값입니다.

    줄로 비교하면 숫자 하나 바뀐 한 줄짜리 명령이 전혀 다른 코드가 되고, 글자로 비교하면 아무 코드나 비슷해집니다.
    """
    matcher = difflib.SequenceMatcher(None, _WORD.findall(old), _WORD.findall(new), autojunk=False)
    return matcher.quick_ratio()


def _pair_lines(hunk: DiffHunk) -> None:
    """좌우 보기의 행을 만들고, 비슷한 짝의 줄 안 차이를 표시합니다."""
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
    """바뀐 묶음 하나의 행. 짝은 같은 행에, 짝이 없는 줄은 그 사이에 위에서부터 나란히 둡니다."""
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
    """비슷한 줄끼리 순서를 지켜 짝짓습니다. 위에 줄 하나가 끼어들어도 아래 줄들이 어긋나지 않습니다."""
    if not dels or not adds:
        return []
    # 줄바꿈 문자만 바뀐 파일은 묶음이 파일 전체라도 그대로 짝입니다. 그러지 않으면 똑같아 보이는 줄만 늘어섭니다.
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
    """짝지은 두 줄에서 실제로 바뀐 낱말의 위치를 적습니다. 끝의 `\\r` 은 한쪽에만 있을 때만 남겨 보여 줍니다."""
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
