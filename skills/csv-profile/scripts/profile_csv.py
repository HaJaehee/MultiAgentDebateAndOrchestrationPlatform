"""작업 공간의 CSV 파일을 훑어, 열마다 종류·빈 값·범위·많이 나온 값을 마크다운 표로 요약합니다.

샌드박스의 `run_python_file` 로 **인자 없이** 실행합니다. 작업 폴더(cwd)는 작업 공간입니다.

- 작업 공간에 `csv-profile.targets.txt` 가 있으면 거기 적힌 파일만 봅니다 (한 줄에 하나, 작업 공간
  기준 경로, `#` 으로 시작하는 줄은 무시). 없으면 작업 공간 아래의 `*.csv` 를 모두 봅니다.
- 결과는 표준 출력으로 쓰고, 같은 내용을 작업 공간의 `csv-profile.md` 로도 저장합니다.

표준 라이브러리만 씁니다 (폐쇄망).
"""

import csv
import os
import re
import statistics
from collections import Counter
from pathlib import Path

TARGETS_FILE = "csv-profile.targets.txt"
REPORT_FILE = "csv-profile.md"
MAX_FILES = 20
MAX_ROWS = 200_000
ENCODINGS = ("utf-8-sig", "cp949")
# 점으로 시작하는 폴더(.git, .mado 등)는 따로 적지 않아도 건너뜁니다. 그 이름들을 여기 글자로
# 적으면 도구 보안의 코드 검사가 "그 폴더를 읽는다" 로 보고, 대화별 지식 그래프 폴더는 고정
# 보호에 걸려 실행 자체가 거부됩니다.
SKIP_DIRS = {"node_modules", "__pycache__", "venv"}
NUMBER = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$|^[+-]?\.\d+$")


def find_targets(root):
    listed = root / TARGETS_FILE
    if listed.is_file():
        names = [
            line.strip() for line in listed.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return [root / name for name in names[:MAX_FILES]]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.lower().endswith(".csv"):
                found.append(Path(dirpath) / filename)
                if len(found) >= MAX_FILES:
                    return found
    return found


def guess_delimiter(text):
    """구분자 추정. 행마다 칸 수가 들쭉날쭉해 추정이 실패하면 머리글에 가장 많이 나온 것을 씁니다."""
    candidates = ",;\t|"
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=candidates).delimiter
    except csv.Error:
        header = text.split("\n", 1)[0]
        best = max(candidates, key=header.count)
        return best if header.count(best) else ","


def read_table(path):
    """(인코딩, 구분자, 머리글, 행) — 읽을 수 없으면 ValueError."""
    raw = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"인코딩을 알 수 없습니다 ({', '.join(ENCODINGS)} 로 읽히지 않음)")
    delimiter = guess_delimiter(text)
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    header = next(reader, None)
    if not header:
        raise ValueError("비어 있는 파일입니다")
    rows = []
    for row in reader:
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return encoding, delimiter, [h.strip() for h in header], rows


def as_number(value):
    text = value.strip()
    if not NUMBER.match(text):
        return None
    return float(text.replace(",", ""))


def fmt(number):
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.3f}".rstrip("0").rstrip(".")


def profile_column(values):
    filled = [v.strip() for v in values if v.strip()]
    missing = len(values) - len(filled)
    if not filled:
        return "비어 있음", missing, "-"
    numbers = [as_number(v) for v in filled]
    if all(n is not None for n in numbers):
        summary = (
            f"최소 {fmt(min(numbers))} · 최대 {fmt(max(numbers))} · "
            f"평균 {fmt(statistics.fmean(numbers))} · 중앙값 {fmt(statistics.median(numbers))}"
        )
        return "숫자", missing, summary
    counts = Counter(filled)
    top = ", ".join(f"{value[:30]}({count:,})" for value, count in counts.most_common(3))
    mixed = sum(n is not None for n in numbers)
    note = f" · 숫자로 읽히는 값 {mixed:,}개 섞임" if mixed else ""
    return "글", missing, f"고유 {len(counts):,}개 · {top}{note}"


def profile_file(root, path):
    shown = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    lines = [f"## {shown}", ""]
    try:
        encoding, delimiter, header, rows = read_table(path)
    except (OSError, ValueError) as exc:
        return lines + [f"읽지 못했습니다: {exc}", ""]
    width = len(header)
    ragged = sum(1 for row in rows if len(row) != width)
    cut = " (앞부분만 읽음)" if len(rows) >= MAX_ROWS else ""
    lines.append(
        f"- 행 {len(rows):,}개{cut} · 열 {width}개 · 인코딩 `{encoding}` · 구분자 `{delimiter}`"
        + (f" · 열 수가 머리글과 다른 행 {ragged:,}개" if ragged else "")
    )
    lines += ["", "| 열 | 종류 | 빈 값 | 요약 |", "| :--- | :--- | ---: | :--- |"]
    for index, name in enumerate(header):
        values = [row[index] if index < len(row) else "" for row in rows]
        kind, missing, summary = profile_column(values)
        label = (name or f"(이름 없음 {index + 1})").replace("|", "/")
        lines.append(f"| {label} | {kind} | {missing:,} | {summary.replace('|', '/')} |")
    return lines + [""]


def main():
    root = Path.cwd()
    targets = find_targets(root)
    if not targets:
        report = "# CSV 요약\n\n작업 공간에서 CSV 파일을 찾지 못했습니다.\n"
    else:
        lines = ["# CSV 요약", ""]
        for path in targets:
            lines += profile_file(root, path)
        report = "\n".join(lines)
    Path(REPORT_FILE).write_text(report, encoding="utf-8")
    print(report)
    print(f"\n(같은 내용을 작업 공간의 {REPORT_FILE} 에 저장했습니다.)")


main()
