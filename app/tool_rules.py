"""도구 보안 규칙의 문법: `read(경로)`, `write(경로)`, `delete(경로)`, `exec`, `net(호스트)`, `mcp(서버/도구)`.

규칙 한 줄을 읽어 `Rule` 로 만듭니다. 설정(`app.config`)이 conf.json 을 검증할 때와 도구 보안 판정
(`app.mcp.policy`)이 호출을 맞춰 볼 때 같은 문법을 쓰므로, 둘 다 기댈 수 있게 의존 없이 여기 둡니다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

PATH_KINDS = ("read", "write", "delete")


RULE_KINDS = PATH_KINDS + ("exec", "net", "mcp")


# 경로 행위의 세기. 제한(deny·ask)은 위로, 허용은 아래로 번집니다 — `read(.env)` 를
# 막으면 `.env` 쓰기도 막히고, `write(src/**)` 를 허용하면 그 읽기도 허용됩니다.
PATH_RANK = {"read": 0, "write": 1, "delete": 2}


RULE_RE = re.compile(r"^\s*([a-z]+)\s*(?:\((.*)\))?\s*$", re.S)


WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:/")


def glob_regex(glob: str) -> "re.Pattern[str]":
    """경로 glob → 정규식. `**` 는 폴더를 넘나들고, `*` 와 `?` 는 한 폴더 안입니다.

    대소문자를 가리지 않습니다. 윈도우 파일 시스템이 가리지 않으므로, 가리면
    `read(**/.ENV)` 가 `.env` 를 놓칩니다 (거부 규칙에서는 그 놓침이 곧 구멍입니다).
    """
    text = glob.replace("\\", "/")
    out: List[str] = []
    i = 0
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    """`kind(pattern)` 한 줄. 원문(`text`)이 곧 화면과 conf.json 에 적히는 모양입니다."""

    text: str
    kind: str
    pattern: str = ""
    regex: Optional["re.Pattern[str]"] = None
    absolute: bool = False

    @property
    def matches_all(self) -> bool:
        return self.pattern in ("", "*", "**")


def parse_rule(text: str) -> Rule:
    """규칙 한 줄을 읽습니다. 틀리면 무엇이 틀렸는지 적은 ValueError."""
    raw = str(text or "").strip()
    match = RULE_RE.match(raw)
    if not match:
        raise ValueError(
            f"규칙 '{raw}' 을 읽을 수 없습니다. `read(경로)`, `write(경로)`, `delete(경로)`, "
            f"`exec`, `net(호스트)`, `mcp(서버/도구)` 모양으로 적으세요."
        )
    kind, pattern = match.group(1), (match.group(2) or "").strip()
    if kind not in RULE_KINDS:
        raise ValueError(f"규칙 '{raw}' 의 '{kind}' 는 없는 행위입니다 ({', '.join(RULE_KINDS)} 중 하나).")

    regex: Optional["re.Pattern[str]"] = None
    absolute = False
    if pattern.startswith("regex:"):
        if kind == "mcp":
            raise ValueError(f"규칙 '{raw}': mcp 규칙은 regex: 대신 `서버/도구` glob 을 씁니다.")
        try:
            regex = re.compile(pattern[len("regex:"):], re.IGNORECASE | re.S)
        except re.error as exc:
            raise ValueError(f"규칙 '{raw}' 의 정규식이 틀렸습니다: {exc}") from exc
    elif kind == "exec" and pattern not in ("", "*"):
        raise ValueError(
            f"규칙 '{raw}': exec 는 `exec` 또는 `exec(regex:정규식)` 으로만 씁니다 "
            f"(코드는 glob 으로 가를 수 없습니다)."
        )
    elif kind in PATH_KINDS and pattern not in ("", "*", "**"):
        normalized = os.path.expanduser(pattern).replace("\\", "/")
        absolute = normalized.startswith("/") or bool(WINDOWS_ABS_RE.match(normalized))
        if normalized.startswith("./"):
            normalized = normalized[2:]
        regex = glob_regex(normalized)
    elif kind == "mcp" and pattern and "/" not in pattern:
        pattern = f"{pattern}/*"
    return Rule(text=raw, kind=kind, pattern=pattern, regex=regex, absolute=absolute)


def parse_rules(texts: Iterable[str]) -> List[Rule]:
    """여러 줄을 읽습니다. 하나라도 틀리면 ValueError (틀린 줄을 전부 적습니다)."""
    rules: List[Rule] = []
    problems: List[str] = []
    for text in texts or ():
        if not str(text or "").strip():
            continue
        try:
            rules.append(parse_rule(text))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError("\n".join(problems))
    return rules
