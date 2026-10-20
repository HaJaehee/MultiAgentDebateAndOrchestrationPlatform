"""산출물 추출, Mermaid 정규화 및 로스터 포맷 유틸리티.

`OrchestratorEngine`에서 분리된 순수 텍스트·코드블록·다이어그램 처리 모듈입니다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.agents.base import Agent
from app.agents.llm import NATIVE_REASONING_HEADER, strip_reasoning_trace
from app.agents.skills import visible_skill_names
from app.mermaid_lint import diagram_kind

# 코드 펜스를 여는 줄. 줄 머리(들여쓰기 허용)의 ``` 또는 ~~~ 세 개 이상, 언어, 그리고
# 뒤따르는 정보 문자열(```` ```mermaid title="흐름" ````). 백틱 펜스의 정보에는 백틱이
# 올 수 없습니다 — 그래야 본문 속 "``` 가" 같은 인라인 표기를 여는 펜스로 잘못 읽지 않습니다.
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<lang>[A-Za-z0-9_+\-]*)(?P<info>[^\n]*)$")
# 줄 중간에서 여는 펜스 ("다음과 같습니다: ```mermaid"). 예전 정규식이 받던 모양이라, 언어가
# 붙고 줄이 거기서 끝날 때만 받습니다.
_FENCE_MIDLINE_OPEN_RE = re.compile(r"(?P<fence>```)(?P<lang>[A-Za-z0-9_+\-]+)[ \t]*$")


def _iter_code_fences(text: str) -> List[Dict[str, Any]]:
    """본문의 코드 블록을 줄 단위로 찾습니다: [{lang, start, end}] (`text[start:end]` 가 코드).

    예전에는 정규식 한 줄(```` ```lang\\n(.*?)``` ````)이었고, 세 가지를 놓쳤습니다.

    * 정보 문자열이 붙은 펜스(```` ```mermaid title="흐름" ````) — 여는 펜스로 못 읽고, 그 블록의
      **닫는** 펜스를 여는 펜스로 읽어 짝이 한 칸씩 밀렸습니다. 뒤따르던 python 블록까지 사라졌습니다.
    * `~~~` 펜스.
    * 바깥 펜스가 더 긴 경우(```` ```` ```` 안의 ```` ``` ````) — 안쪽 줄에서 닫혔습니다.

    닫는 펜스가 없으면(응답 한도로 잘린 답변) 끝까지를 코드로 봅니다. 닫는 줄 뒤에 글이
    붙어 있거나(```` ``` 끝 ````) 코드 줄 끝에 펜스가 붙은 경우(```` A-->B``` ````)도 닫힌 것으로
    봅니다 — 모델이 흔히 그렇게 씁니다.
    """
    text = text or ""
    blocks: List[Dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    i = 0
    while i < len(lines):
        body = lines[i].rstrip("\r\n")
        m = _FENCE_OPEN_RE.match(body)
        if m and not (m.group("fence")[0] == "`" and "`" in m.group("info")):
            fence, lang = m.group("fence"), m.group("lang")
        else:
            mid = _FENCE_MIDLINE_OPEN_RE.search(body)
            if not mid:
                i += 1
                continue
            fence, lang = mid.group("fence"), mid.group("lang")

        start = offsets[i] + len(lines[i])
        end = len(text)
        j = i + 1
        next_i = len(lines)
        while j < len(lines):
            line_body = lines[j].rstrip("\r\n")
            stripped = line_body.lstrip(" \t")
            run = len(stripped) - len(stripped.lstrip(fence[0]))
            if run >= len(fence):
                end = offsets[j]
                next_i = j + 1
                break
            tail = line_body.rstrip()
            if fence[0] == "`" and tail.endswith(fence) and not tail.endswith(fence[0] * (len(fence) + 1)):
                end = offsets[j] + len(tail) - len(fence)
                next_i = j + 1
                break
            j += 1
        blocks.append({"lang": lang, "start": start, "end": end})
        i = next_i
    return blocks


def _is_untagged_mermaid(code: str) -> bool:
    """언어 태그 없이 열린 블록이 Mermaid 인가. 선언을 `diagram_kind` 로 읽습니다.

    YAML 머리말·`%%{init}%%`·주석 뒤의 선언도 읽고, `kanban`·`packet-beta` 같은 새 종류도 압니다.
    """
    return diagram_kind(code) is not None


CODE_LANGUAGES = ("python", "py", "typescript", "javascript", "bash", "shell", "json", "toml", "sql")

# 다이어그램 문법이 틀렸을 때 오케스트레이터에게 다시 물어보는 횟수.
MERMAID_REPAIR_ATTEMPTS = 2

# `A[결제 서비스 (Payment)]` 처럼 대괄호 라벨 안에 괄호가 들어간 형태.
_PAREN_LABEL_RE = re.compile(r"\[([^\[\]{}\"|]*[()][^\[\]{}\"|]*)\]")
# 여는 문자 -> 닫는 문자. 이 쌍으로 감싸인 것은 라벨이 아니라 노드 모양입니다.
_SHAPE_PAIRS = {"(": ")", "[": "]", "/": "/", "\\": "\\", "{": "}"}

# flowchart 에 섞여 들어온 시퀀스 다이어그램의 노트. `Note right of A: 글`, `Note over A,B: 글`.
_FLOWCHART_NOTE_RE = re.compile(
    r"^(?P<indent>\s*)note\s+(?:right\s+of|left\s+of|over)\s+(?P<targets>[^:]+?)\s*(?::\s*(?P<text>.*))?$",
    re.IGNORECASE,
)
_SIMPLE_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_NOTE_CLASS_DEF = "classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12"


def _convert_flowchart_notes(lines: List[str]) -> List[str]:
    """flowchart 안의 `Note right of A: 글` 을 `A -.- mado_note_1["글"]` 로 바꿉니다."""
    out: List[str] = []
    converted: List[str] = []
    joined = "\n".join(lines)
    next_index = 1
    for line in lines:
        m = _FLOWCHART_NOTE_RE.match(line)
        if not m:
            out.append(line)
            continue
        targets = [t.strip() for t in m.group("targets").split(",") if t.strip()]
        if not targets or not all(_SIMPLE_NODE_ID_RE.match(t) for t in targets):
            out.append(line)
            continue
        text = (m.group("text") or "").strip().replace('"', "'").replace("`", "'")
        if not text:
            continue
        while re.search(rf"\bmado_note_{next_index}\b", joined):
            next_index += 1
        node = f"mado_note_{next_index}"
        next_index += 1
        converted.append(node)
        indent = m.group("indent")
        out.append(f'{indent}{targets[0]} -.- {node}["{text}"]')
        for extra in targets[1:]:
            out.append(f"{indent}{extra} -.- {node}")
    if converted:
        if not re.search(r"^\s*classDef\s+madoNote\b", joined, re.M):
            out.append(f"  {_NOTE_CLASS_DEF}")
        out.append(f"  class {','.join(converted)} madoNote")
    return out


_STYLE_LINE_RE = re.compile(r"^\s*(?:style|classDef|linkStyle)\b", re.IGNORECASE)
_RGB_RE = re.compile(
    r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*([\d.]+%?)\s*)?\)",
    re.IGNORECASE,
)
_BARE_SUBGRAPH_PAREN_RE = re.compile(
    r'^(?P<head>\s*subgraph\s+)(?P<title>[^"\[\]\n]*[()][^"\[\]\n]*?)\s*$', re.IGNORECASE
)
_ASYMMETRIC_PAREN_RE = re.compile(r'(?<=[\w])>(?P<inner>[^"\[\]\n>]*[()][^"\[\]\n>]*)\]')
_SLANT_PAREN_RE = re.compile(
    r'\[(?P<o>[/\\])(?P<inner>[^"\[\]/\\\n]*[()][^"\[\]/\\\n]*)(?P<c>[/\\])\]'
)
_DOUBLE_CIRCLE_PAREN_RE = re.compile(r'(?<=[\w])\(\((?P<inner>[^()"\n]*\([^()"\n]*\)[^()"\n]*)\)\)')
_SUBGRAPH_ID_QUOTED_RE = re.compile(
    r'^(?P<head>\s*subgraph\s+)(?P<id>[A-Za-z0-9_]+)\s+"(?P<title>[^"\n]*)"\s*$', re.IGNORECASE
)


def _rgb_to_hex(m: re.Match) -> str:
    r, g, b, alpha = m.group(1), m.group(2), m.group(3), m.group(4)
    channels = [int(r), int(g), int(b)]
    if any(c > 255 for c in channels):
        return m.group(0)
    hex_value = "#" + "".join(f"{c:02x}" for c in channels)
    if alpha is not None:
        try:
            a = float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha)
        except ValueError:
            return m.group(0)
        if not 0 <= a <= 1:
            return m.group(0)
        if a < 1:
            hex_value += f"{round(a * 255):02x}"
    return hex_value


def _quote_bracket_label(m: re.Match) -> str:
    """`[라벨 (괄호)]` → `["라벨 (괄호)"]`. 모양 감싸개는 그대로 두되, 원통 `[(…)]` 안의 괄호는 감쌉니다."""
    inner = m.group(1)
    if not inner:
        return m.group(0)
    if _SHAPE_PAIRS.get(inner[0]) == inner[-1]:
        body = inner[1:-1]
        if inner[0] == "(" and ("(" in body or ")" in body) and body.strip():
            return f'[("{body.strip()}")]'
        return m.group(0)
    return f'["{inner.strip()}"]'


def _normalize_flowchart_line(line: str) -> str:
    if _STYLE_LINE_RE.match(line):
        return _RGB_RE.sub(_rgb_to_hex, line)
    sub = _SUBGRAPH_ID_QUOTED_RE.match(line)
    if sub:
        return f'{sub.group("head")}{sub.group("id")}["{sub.group("title")}"]'
    sub = _BARE_SUBGRAPH_PAREN_RE.match(line)
    if sub:
        return f'{sub.group("head")}"{sub.group("title").strip()}"'
    line = _ASYMMETRIC_PAREN_RE.sub(lambda m: f'>"{m.group("inner").strip()}"]', line)
    line = _SLANT_PAREN_RE.sub(lambda m: f'[{m.group("o")}"{m.group("inner").strip()}"{m.group("c")}]', line)
    line = _DOUBLE_CIRCLE_PAREN_RE.sub(lambda m: f'(("{m.group("inner").strip()}"))', line)
    return _PAREN_LABEL_RE.sub(_quote_bracket_label, line)


def normalize_mermaid(content: str) -> str:
    """LLM 이 빈번히 생성하는 Mermaid 문법 오류를 최소한으로 보정합니다."""
    text = (content or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return text

    lines = text.split("\n")
    if lines[0].strip().lower() == "mermaid":
        lines = lines[1:]

    kind = diagram_kind("\n".join(lines))
    if kind in ("graph", "flowchart"):
        lines = [_normalize_flowchart_line(line) for line in _convert_flowchart_notes(lines)]
    elif kind == "mindmap":
        lines = [_PAREN_LABEL_RE.sub(_quote_bracket_label, line) for line in lines]
    return "\n".join(lines).strip()


def find_mermaid_blocks(text: str) -> List[Dict[str, Any]]:
    """본문 속 Mermaid 코드 블록을 **위치와 함께** 찾습니다."""
    blocks: List[Dict[str, Any]] = []
    source = text or ""
    for fence in _iter_code_fences(source):
        lang = fence["lang"].strip().lower() or "text"
        raw = source[fence["start"]:fence["end"]]
        code = raw.strip()
        if not code:
            continue
        if lang == "text":
            if not _is_untagged_mermaid(code):
                continue
        elif lang != "mermaid":
            continue
        start = fence["start"] + (len(raw) - len(raw.lstrip()))
        blocks.append({"code": code, "start": start, "end": start + len(code)})
    return blocks


def extract_code_blocks(text: str) -> List[Dict[str, str]]:
    """Extracts markdown code blocks from text."""
    matches = []
    source = text or ""
    for fence in _iter_code_fences(source):
        lang = fence["lang"].strip().lower() or "text"
        code = source[fence["start"]:fence["end"]].strip()
        if not code:
            continue
        if lang == "text" and _is_untagged_mermaid(code):
            lang = "mermaid"
        matches.append({"language": lang, "code": code})
    return matches


# 합성 결론으로 보지 않는 줄 — 응답 한도·빈 답변 등을 알리는 꼬리표(`> ⚠️ ...`).
_NOTICE_LINE = re.compile(r"^\s*>\s*⚠️")

# 이번 턴 전문가 발언에서 모아 산출물로 올릴 코드의 최대 개수.
MAX_DEBATE_CODE_ARTIFACTS = 12


def format_roster(agents: List[Agent], *, with_keys: bool = False) -> str:
    """오케스트레이터에게 제공할 전문가 목록입니다."""
    lines = []
    for agent in agents:
        thinking_server = agent.sequential_thinking.mcp_server
        servers = [s for s in agent.allowed_mcp_servers if s != thinking_server]
        head = f"{agent.key}: {agent.name}" if with_keys else agent.name
        tools = f" · 도구: {', '.join(servers)}" if servers else " · 도구: 없음"
        skills = visible_skill_names(agent)
        if skills:
            tools += f" · 스킬: {', '.join(skills)}"
        lines.append(f"- {head} ({agent.role}){tools}")
    return "\n".join(lines)


def synthesis_has_content(text: str) -> bool:
    """합성 발언 메시지가 실질적인 결론 내용을 포함하고 있는지 확인합니다."""
    lines = (text or "").strip().splitlines()
    if lines and lines[0].startswith(NATIVE_REASONING_HEADER):
        while lines and (lines[0].startswith(">") or not lines[0].strip()):
            lines.pop(0)
    body = "\n".join(line for line in lines if not _NOTICE_LINE.match(line))
    return bool(strip_reasoning_trace(body).strip())
