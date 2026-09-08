"""Mermaid 문법 검사 — 렌더러가 거부할 다이어그램을 **발언이 끝나기 전에** 잡습니다.

오케스트레이터가 라운드 끝에 그리는 다이어그램은 자주 문법이 어긋납니다.
예전에는 그것이 그대로 아티팩트가 되어, 사람이 탭을 열었을 때 비로소 "Mermaid
문법 오류" 를 보았습니다. 그때는 토론이 이미 끝나 고칠 사람이 없습니다.

여기서는 합성 직후에 검사하고, 틀렸으면 **오케스트레이터에게 오류를 그대로
돌려주어 스스로 고치게** 합니다 (`OrchestratorEngine._repair_mermaid_blocks`).

## 설계 원칙: 놓치는 것보다 잘못 잡는 것이 나쁘다

이 모듈은 Mermaid 파서가 **아닙니다.** 파서를 파이썬으로 다시 쓰는 것은 지는
싸움이고, 폐쇄망 번들에 Node 렌더러를 넣을 수도 없습니다. 대신 규칙 하나하나를
실제 `mermaid.parse()` 로 확인해 가며 골랐습니다.

* **놓친 오류**(false negative)의 대가 = 지금과 같음. 사람이 화면에서 봅니다.
* **잘못 잡은 오류**(false positive)의 대가 = 멀쩡한 다이어그램을 두고 LLM 을
  다시 부릅니다. 토큰과 시간이 나가고, 고칠 것이 없는 모델이 멀쩡한 그림을
  망칩니다.

그래서 확신이 없는 규칙은 넣지 않았습니다. 실제로 뺀 것들:

* `A --|라벨| B` (화살표 없는 파이프) — 틀린 문법이 맞지만, 대시가 하나 더 붙은
  `A ---|라벨| B` 는 **정상**입니다. 부분 문자열로는 둘을 가를 수 없습니다.
* 괄호가 든 라벨 — flowchart 에서만 오류입니다. `sequenceDiagram` 의
  `actor C as 고객 (앱)` 은 정상이라, 다이어그램 종류를 보고 나서만 봅니다.
* `end` 를 노드 이름으로 쓰기 — 소문자 `end` 만 오류이고 `END` 는 정상입니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

# 첫 줄에 올 수 있는 다이어그램 선언. 없으면 렌더러가
# "No diagram type detected" 로 거부합니다.
DIAGRAM_HEADERS = (
    "graph", "flowchart", "sequencediagram", "classdiagram", "statediagram",
    "statediagram-v2", "erdiagram", "journey", "gantt", "pie", "gitgraph",
    "mindmap", "timeline", "quadrantchart", "requirementdiagram", "c4context",
    "c4container", "c4component", "c4dynamic", "sankey-beta", "block-beta",
    "architecture-beta", "xychart-beta", "packet-beta", "kanban", "zenuml",
)

# 노드 모양·라벨 검사를 적용할 종류. 다른 종류는 괄호와 중괄호를 다르게 씁니다
# (erDiagram 의 `||--o{`, classDiagram 의 여러 줄 `class X { }`).
_FLOWCHART_KINDS = ("graph", "flowchart")

_COMMENT_RE = re.compile(r"^\s*%%")
# 따옴표 안의 내용. 괄호·대괄호를 세기 전에 지웁니다 — 따옴표로 감싼 것은
# 이미 안전하고, 그 안의 기호까지 세면 멀쩡한 라벨을 오류로 봅니다.
_QUOTED_RE = re.compile(r'"[^"]*"')
# `A --> end` 처럼 소문자 end 를 노드 이름으로 쓴 자리.
_END_AS_NODE_RE = re.compile(r"(?:^|[\s>|)\]}])end(?:$|[\s<(\[{-])")
# flowchart 의 라벨 자리: [..] {..} |..|. `(..)` 는 중첩이 있어 따로 봅니다.
_LABEL_SPANS = (("[", "]"), ("{", "}"), ("|", "|"))

# 여는 문자 -> 닫는 문자. `A[(DB)]`(원통) `B[[Sub]]`(서브루틴) `E[/Para/]`(평행사변형)
# 처럼 **모양을 나타내는 감싸개**입니다. 안쪽 괄호는 라벨의 일부가 아니므로
# 지적하면 안 됩니다 — `normalize_mermaid` 가 따옴표를 씌우지 않는 것과 같은 이유.
_SHAPE_WRAPPERS = {"(": ")", "[": "]", "/": "/", "\\": "\\", "{": "}"}


@dataclass(frozen=True)
class MermaidIssue:
    """검사에 걸린 자리 하나."""

    line: int          # 1부터. 0 이면 다이어그램 전체에 대한 지적입니다.
    rule: str
    message: str
    snippet: str = ""

    def describe(self) -> str:
        where = f"{self.line}번째 줄" if self.line else "다이어그램 전체"
        text = f"- {where}: {self.message}"
        if self.snippet:
            text += f"\n    {self.snippet.strip()}"
        return text


def diagram_kind(code: str) -> Optional[str]:
    """첫 의미 있는 줄에서 다이어그램 종류를 읽습니다. 못 읽으면 None."""
    for line in (code or "").splitlines():
        stripped = line.strip()
        if not stripped or _COMMENT_RE.match(stripped):
            continue
        head = stripped.lower()
        for keyword in sorted(DIAGRAM_HEADERS, key=len, reverse=True):
            if head == keyword or head.startswith(keyword + " ") or head.startswith(keyword + ";"):
                return keyword
            # `graph TD;` `flowchart LR` 처럼 방향이 붙는 형태
            if head.startswith(keyword) and len(head) > len(keyword) and not head[len(keyword)].isalnum():
                return keyword
        return None
    return None


def _strip_quoted(line: str) -> str:
    return _QUOTED_RE.sub('""', line)


def _meaningful_lines(code: str) -> List[tuple]:
    """(1부터 센 줄번호, 원문) 중 빈 줄과 주석을 뺀 것."""
    return [
        (i, raw)
        for i, raw in enumerate((code or "").splitlines(), start=1)
        if raw.strip() and not _COMMENT_RE.match(raw)
    ]


def _balance_issues(line_no: int, line: str, pairs: str) -> List[MermaidIssue]:
    """한 줄 안에서 짝이 맞지 않는 괄호를 지적합니다."""
    text = _strip_quoted(line)
    issues: List[MermaidIssue] = []
    for opener, closer in zip(pairs[0::2], pairs[1::2]):
        if text.count(opener) != text.count(closer):
            issues.append(MermaidIssue(
                line=line_no,
                rule="bracket-balance",
                message=f"`{opener}` 와 `{closer}` 의 개수가 맞지 않습니다.",
                snippet=line,
            ))
    return issues


def _is_shape_wrapper(inner: str) -> bool:
    """`[(...)]`, `[[...]]`, `[/.../]` 처럼 모양을 뜻하는 감싸개인지."""
    inner = inner.strip()
    return len(inner) >= 2 and _SHAPE_WRAPPERS.get(inner[0]) == inner[-1]


def _round_node_paren_issue(line_no: int, line: str) -> Optional[MermaidIssue]:
    """`A(결제 (PG))` — 둥근 노드 라벨 안의 괄호.

    `[..]` 와 달리 여는 문자와 닫는 문자가 라벨 안에도 나올 수 있어, 정규식이
    아니라 짝을 세면서 훑습니다. `A((원))`(이중 원)과 `A(("x"))` 는 모양
    문법이므로 건드리지 않습니다.
    """
    text = _strip_quoted(line)
    for match in re.finditer(r"[A-Za-z0-9_\-]\(", text):
        start = match.end() - 1
        depth, i = 0, start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            return None  # 짝이 안 맞는 것은 bracket-balance 가 봅니다.
        inner = text[start + 1:i]
        if _is_shape_wrapper(inner):
            continue
        if "(" in inner or ")" in inner:
            return MermaidIssue(
                line=line_no,
                rule="paren-in-label",
                message=(
                    f"둥근 노드 라벨 `({inner.strip()})` 안에 괄호가 따옴표 없이 들어 있습니다. "
                    f'`("{inner.strip()}")` 처럼 큰따옴표로 감싸세요.'
                ),
                snippet=line,
            )
    return None


def _paren_in_label_issues(line_no: int, line: str) -> List[MermaidIssue]:
    """flowchart 라벨 안의 따옴표 없는 괄호. 렌더러가 거부합니다."""
    issues: List[MermaidIssue] = []
    scan = _strip_quoted(line)
    for opener, closer in _LABEL_SPANS:
        pattern = re.escape(opener) + r"([^" + re.escape(opener + closer) + r'"]*)' + re.escape(closer)
        for match in re.finditer(pattern, scan):
            inner = match.group(1)
            if _is_shape_wrapper(inner):
                continue
            if "(" in inner or ")" in inner:
                issues.append(MermaidIssue(
                    line=line_no,
                    rule="paren-in-label",
                    message=(
                        f"라벨 `{opener}{inner.strip()}{closer}` 안에 괄호가 따옴표 없이 들어 있습니다. "
                        f'`{opener}"{inner.strip()}"{closer}` 처럼 큰따옴표로 감싸세요.'
                    ),
                    snippet=line,
                ))
                break
    round_issue = _round_node_paren_issue(line_no, line)
    if round_issue is not None:
        issues.append(round_issue)
    return issues


def lint_mermaid(code: str) -> List[MermaidIssue]:
    """렌더링에 실패할 것이 **확실한** 자리만 돌려줍니다. 통과하면 빈 목록.

    빈 목록이 "문법이 완벽하다" 는 뜻은 아닙니다. "우리가 아는 확실한 오류는
    없다" 는 뜻입니다 (모듈 docstring 의 설계 원칙 참고).
    """
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = _meaningful_lines(text)

    if not lines:
        return [MermaidIssue(0, "empty", "다이어그램이 비어 있습니다.")]

    kind = diagram_kind(text)
    if kind is None:
        return [MermaidIssue(
            line=lines[0][0],
            rule="no-header",
            message=(
                "첫 줄에 다이어그램 종류 선언이 없습니다 "
                "(`graph TD`, `flowchart LR`, `sequenceDiagram`, `classDiagram` 등). "
                "```mermaid 펜스나 `mermaid` 라는 머리글은 본문에 넣지 마세요."
            ),
            snippet=lines[0][1],
        )]

    issues: List[MermaidIssue] = []
    is_flowchart = kind in _FLOWCHART_KINDS

    if is_flowchart:
        depth = 0
        for line_no, raw in lines:
            body = _strip_quoted(raw).strip()
            lowered = body.lower()

            # 따옴표가 이 줄에서 닫히지 않으면 여러 줄 라벨입니다
            # (`A["첫줄` / `두번째"] --> B`). 정상 문법이므로 괄호를 세지
            # 않습니다 — 세면 반드시 짝이 안 맞는 것으로 보입니다.
            if raw.count('"') % 2 == 0:
                issues.extend(_balance_issues(line_no, raw, "[]()"))
                # `{}` 는 flowchart 에서만 한 줄 안에서 닫힙니다. erDiagram 의
                # `||--o{` 와 classDiagram 의 여러 줄 블록은 여기 오지 않습니다.
                issues.extend(_balance_issues(line_no, raw, "{}"))
                issues.extend(_paren_in_label_issues(line_no, raw))

            if lowered.startswith("subgraph"):
                depth += 1
            elif lowered == "end":
                depth -= 1
                if depth < 0:
                    issues.append(MermaidIssue(
                        line=line_no,
                        rule="stray-end",
                        message="짝이 없는 `end` 입니다. 열려 있는 `subgraph` 가 없습니다.",
                        snippet=raw,
                    ))
                    depth = 0
            elif _END_AS_NODE_RE.search(body):
                # 소문자 `end` 는 예약어입니다. `END` 나 `endNode` 로 바꾸면 통과합니다.
                issues.append(MermaidIssue(
                    line=line_no,
                    rule="end-as-node",
                    message=(
                        "`end` 는 예약어라 노드 이름으로 쓸 수 없습니다 "
                        "(`END` 나 `endNode` 처럼 바꾸세요)."
                    ),
                    snippet=raw,
                ))

        if depth > 0:
            issues.append(MermaidIssue(
                line=lines[-1][0],
                rule="subgraph-unclosed",
                message=f"`subgraph` {depth}개가 `end` 로 닫히지 않았습니다.",
            ))

    if kind in ("classdiagram", "statediagram", "statediagram-v2"):
        opened = sum(_strip_quoted(raw).count("{") for _, raw in lines)
        closed = sum(_strip_quoted(raw).count("}") for _, raw in lines)
        if opened != closed:
            issues.append(MermaidIssue(
                line=lines[-1][0],
                rule="block-unclosed",
                message=f"`{{` {opened}개와 `}}` {closed}개의 짝이 맞지 않습니다.",
            ))

    if kind == "sequencediagram":
        for line_no, raw in lines:
            body = _strip_quoted(raw)
            # flowchart 의 `==>` 는 굵은 화살표지만 시퀀스에는 없습니다.
            if re.search(r"[^-]=+>", body):
                issues.append(MermaidIssue(
                    line=line_no,
                    rule="bad-sequence-arrow",
                    message=(
                        "시퀀스 다이어그램에는 `==>` 화살표가 없습니다 "
                        "(`->>`, `-->>`, `->`, `-->`, `-x`, `-)` 중에서 쓰세요)."
                    ),
                    snippet=raw,
                ))

    # 라벨 안에 따옴표가 겹친 자리. `A["그는 "안녕" 이라 했다"]` 는 거부됩니다.
    for line_no, raw in lines:
        for match in re.finditer(r"\[([^\[\]]*)\]", raw):
            if match.group(1).count('"') > 2:
                issues.append(MermaidIssue(
                    line=line_no,
                    rule="nested-quotes",
                    message="라벨 안에 큰따옴표가 겹쳐 있습니다. 안쪽 따옴표는 지우거나 `&quot;` 로 바꾸세요.",
                    snippet=raw,
                ))
                break

    return issues


def format_issues(issues: List[MermaidIssue]) -> str:
    """모델에게 그대로 보여 줄 오류 목록."""
    return "\n".join(issue.describe() for issue in issues)


def lint_blocks(blocks: Dict[int, str]) -> Dict[int, List[MermaidIssue]]:
    """{블록 번호: 코드} 를 한 번에 검사합니다. 문제 있는 것만 돌려줍니다."""
    return {
        index: issues
        for index, code in blocks.items()
        if (issues := lint_mermaid(code))
    }
