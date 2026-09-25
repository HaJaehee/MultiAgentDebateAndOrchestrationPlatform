import asyncio
import json
import logging
import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple
from sqlalchemy import select, update
from app.agents.base import Agent
from app.agents.llm import (
    LLMCaller,
    LLMUnavailableError,
    context_budget,
    context_trim_notice,
    estimate_tokens,
    NATIVE_REASONING_HEADER,
    memory_search_tool,
    strip_reasoning_trace,
)
from app.agents.personas import prepare_agents_for_turn
from app.agents.pool import AgentPool, get_agent_pool
from app.timestamps import report_completed_line, to_local
from app.config import DATA_DIR, TOOL_ITERATION_CEILING, resolve_workspace_dir
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool
from app.mermaid_lint import diagram_kind, format_issues, lint_mermaid
from app.database.models import (
    ArtifactModel,
    MessageModel,
    SessionModel,
    ToolCallRecordModel,
    utc_now,
)
from app.database.session import get_session_factory
from app.orchestration import context_memory as memory
from app.orchestration.graph import (
    CARRY_LABELS,
    Activation,
    GraphNode,
    GraphScheduler,
    GraphSpec,
    back_edges,
    validate_graph,
)
from app.workspace_files import mention_token
from app.orchestration.control import TurnControl
from app.orchestration.tool_gate import ToolGate
from app.orchestration.state import ArtifactItem, DebateMessage, DebateState
from app.orchestration.strategies import (
    BaseDebateStrategy,
    get_strategy,
    resolve_strategy_name,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class GraphTurnError(RuntimeError):
    """그래프 토론을 시작할 수 없을 때 (그래프 없음 · 파일 없음 · 검증 오류). 사람이 읽을 문장입니다."""


def parse_gate_decision(content: str) -> Optional[Tuple[str, str]]:
    """판정 응답에서 (yes|no, 사유). 읽지 못하면 None.

    JSON 이 온전하면 그것을 쓰고, 아니면 첫 줄의 예/아니오·yes/no 를 봅니다. 둘 다 없으면 None —
    본문 어딘가의 "no" 를 긁으면 "no problem" 이 거부가 됩니다.
    """
    text = strip_reasoning_trace(content or "").strip()
    block = re.search(r"\{.*\}", text, re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            raw = str(data.get("decision", "")).strip().lower()
            reason = str(data.get("reason") or "").strip()
            if raw in ("yes", "y", "예", "true"):
                return "yes", reason
            if raw in ("no", "n", "아니오", "아니요", "false"):
                return "no", reason
    first = text.splitlines()[0].strip().strip("*#:. ").lower() if text else ""
    if first in ("yes", "예"):
        return "yes", ""
    if first in ("no", "아니오", "아니요"):
        return "no", ""
    return None

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
#
# 두 번이면 충분합니다. 오류 메시지를 정확히 받은 모델은 대개 한 번에 고치고,
# 두 번째에도 못 고치면 세 번째라고 달라지지 않습니다 — 그때는 사람이 볼 수
# 있도록 원문을 그대로 두고 무엇이 틀렸는지 알리는 편이 낫습니다.
MERMAID_REPAIR_ATTEMPTS = 2

# `A[결제 서비스 (Payment)]` 처럼 대괄호 라벨 안에 괄호가 들어간 형태. LLM 이 가장 자주
# 만드는 Mermaid 파싱 오류이고, 따옴표로 감싸면 그대로 통과합니다.
_PAREN_LABEL_RE = re.compile(r"\[([^\[\]{}\"|]*[()][^\[\]{}\"|]*)\]")
# 여는 문자 -> 닫는 문자. 이 쌍으로 감싸인 것은 라벨이 아니라 노드 모양입니다.
_SHAPE_PAIRS = {"(": ")", "[": "]", "/": "/", "\\": "\\", "{": "}"}


# flowchart 에 섞여 들어온 시퀀스 다이어그램의 노트. `Note right of A: 글`, `Note over A,B: 글`.
_FLOWCHART_NOTE_RE = re.compile(
    r"^(?P<indent>\s*)note\s+(?:right\s+of|left\s+of|over)\s+(?P<targets>[^:]+?)\s*(?::\s*(?P<text>.*))?$",
    re.IGNORECASE,
)
_SIMPLE_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
# 변환한 노트에 입히는 모양. 파서로 확인했습니다 (`classDef` + `class`).
_NOTE_CLASS_DEF = "classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12"


def _convert_flowchart_notes(lines: List[str]) -> List[str]:
    """flowchart 안의 `Note right of A: 글` 을 `A -.- mado_note_1["글"]` 로 바꿉니다.

    모델이 순서도에 시퀀스 다이어그램의 노트를 끼워 넣으면 렌더러가 다이어그램을 통째로
    거부합니다. 노트는 "이 노드에 붙은 설명" 이라 점선으로 붙인 노드와 뜻이 같아, LLM 을
    다시 부르지 않고 기계적으로 바꿀 수 있습니다. `loop`·`alt`·`participant` 는 구조를 바꿔야
    해서 여기서 손대지 않고 수선 요청(`sequence-syntax-in-flowchart`)으로 남깁니다.

    대상 노드 이름이 단순한 식별자(영숫자·밑줄)가 아니면 건드리지 않습니다 — 공백이 든
    이름은 어느 노드를 뜻하는지 확신할 수 없습니다. 글이 없는 노트는 전할 것이 없어 지웁니다.
    """
    out: List[str] = []
    converted: List[str] = []
    joined = "\n".join(lines)
    # 이미 있는 이름과 겹치지 않게 번호를 고릅니다. 수선 결과를 다시 정규화하거나 모델이
    # 앞서 변환된 노드를 베껴 오면, 1번부터 다시 세는 순간 남의 노트에 붙어 버립니다.
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
        # 큰따옴표는 라벨을 닫고, 백틱은 마크다운 문자열로 읽혀 라벨을 깨뜨립니다 (파서 확인).
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
        # 같은 classDef 가 두 번 있어도 파서는 받지만, 이미 있으면 덧붙이지 않습니다.
        if not re.search(r"^\s*classDef\s+madoNote\b", joined, re.M):
            out.append(f"  {_NOTE_CLASS_DEF}")
        out.append(f"  class {','.join(converted)} madoNote")
    return out


# `style`·`classDef`·`linkStyle` 의 `rgb(…)`·`rgba(…)`. 이 문장들은 쉼표로 속성을 나누므로
# 괄호 안의 쉼표 때문에 렌더러가 거부합니다 (파서 확인: `fill:rgb(255,0,0)` 오류,
# `fill:#ff0000`·`#00000080` 정상). 숫자로 된 것만 16진수로 바꿉니다.
_STYLE_LINE_RE = re.compile(r"^\s*(?:style|classDef|linkStyle)\b", re.IGNORECASE)
_RGB_RE = re.compile(
    r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*([\d.]+%?)\s*)?\)",
    re.IGNORECASE,
)
# `subgraph 그룹 (A)` — 따옴표·대괄호 없는 제목에 괄호. `subgraph "그룹 (A)"` 는 정상.
_BARE_SUBGRAPH_PAREN_RE = re.compile(r'^(?P<head>\s*subgraph\s+)(?P<title>[^"\[\]\n]*[()][^"\[\]\n]*?)\s*$', re.IGNORECASE)
# `A>결제 (PG)]` — 비대칭 모양 라벨에 괄호. `A>"결제 (PG)"]` 는 정상. 앞 글자가 단어
# 문자여야 `-->` 같은 화살표의 `>` 와 헷갈리지 않습니다.
_ASYMMETRIC_PAREN_RE = re.compile(r'(?<=[\w])>(?P<inner>[^"\[\]\n>]*[()][^"\[\]\n>]*)\]')
# `A[/입력 (x)/]` 평행사변형·사다리꼴(`/` `\` 조합) 안의 괄호. `[/"입력 (x)"/]` 는 정상 (파서 확인).
_SLANT_PAREN_RE = re.compile(r'\[(?P<o>[/\\])(?P<inner>[^"\[\]/\\\n]*[()][^"\[\]/\\\n]*)(?P<c>[/\\])\]')
# `A((원 (x)))` 이중 원 안의 괄호 한 겹. `(("원 (x)"))` 는 정상 (파서 확인).
_DOUBLE_CIRCLE_PAREN_RE = re.compile(r'(?<=[\w])\(\((?P<inner>[^()"\n]*\([^()"\n]*\)[^()"\n]*)\)\)')
# `subgraph pay "결제"` — 아이디 뒤 따옴표 제목은 거부됩니다. `subgraph pay["결제"]` 는 정상 (파서 확인).
_SUBGRAPH_ID_QUOTED_RE = re.compile(r'^(?P<head>\s*subgraph\s+)(?P<id>[A-Za-z0-9_]+)\s+"(?P<title>[^"\n]*)"\s*$', re.IGNORECASE)


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
        # `[(DB (주))]` 는 거부되고 `[("DB (주)")]` 는 정상입니다 (파서 확인). 다른 모양
        # (`[/…/]`, `[\…\]`)은 확인하지 않았으므로 건드리지 않습니다.
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
    """LLM 이 흔히 내는 Mermaid 문법 오류를 최소한만 손봅니다.

    다이어그램을 다시 써 주는 것이 아니라, 렌더러가 통째로 거부해서 화면이 비는
    경우만 막습니다: 잘못된 줄바꿈, 따옴표 없는 괄호 라벨, flowchart 에 섞인 시퀀스
    다이어그램 노트 (v0.8.1), 스타일의 `rgb()`, 괄호가 든 원통·비대칭 모양과 subgraph 제목.

    **라벨 손질은 flowchart 와 mindmap 에서만 합니다.** 시퀀스·상태·간트·클래스·ER·여정·
    타임라인에서는 `[결제 (PG)]` 가 원래 정상이라(파서 확인), 따옴표를 씌우면 화면에 보이는
    글자에 따옴표가 덧붙을 뿐입니다. 여러 번 적용해도 결과가 같습니다.
    """
    text = (content or "").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return text

    # ```mermaid 를 잘라낸 뒤 남은 "mermaid" 머리글
    lines = text.split("\n")
    if lines[0].strip().lower() == "mermaid":
        lines = lines[1:]

    kind = diagram_kind("\n".join(lines))
    if kind in ("graph", "flowchart"):
        lines = [_normalize_flowchart_line(line) for line in _convert_flowchart_notes(lines)]
    elif kind == "mindmap":
        # `가지[항목 (A)]` 는 거부되고 `가지["항목 (A)"]` 는 정상입니다 (파서 확인).
        lines = [_PAREN_LABEL_RE.sub(_quote_bracket_label, line) for line in lines]
    return "\n".join(lines).strip()


def find_mermaid_blocks(text: str) -> List[Dict[str, Any]]:
    """본문 속 Mermaid 코드 블록을 **위치와 함께** 찾습니다.

    `extract_code_blocks()` 는 코드만 돌려주는데, 고친 다이어그램을 제자리에
    끼워 넣으려면 원문의 어디였는지를 알아야 합니다. 문자열 치환으로 하면
    같은 코드가 두 번 나올 때 엉뚱한 곳을 바꿉니다.
    """
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
        # 범위는 **다듬은 코드**의 것이어야 합니다. 원본 그대로의 범위를 쓰면
        # 끝의 줄바꿈까지 포함되고, 그 자리를 줄바꿈 없는 코드로 갈아 끼우는
        # 순간 닫는 ``` 가 마지막 코드 줄에 붙어 펜스가 깨집니다.
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


# ---------------------------------------------------------------- 기록 실패

# 커밋이 실패했을 때 다시 시도하기 전에 기다리는 시간(초). 시도는 이것보다 한 번 많습니다.
#
# SQLite 자체가 이미 잠금을 `SQLITE_BUSY_TIMEOUT_MS` 까지 기다리므로, 여기까지 왔다면
# 바깥 프로그램이 파일을 오래 잡고 있는 것입니다. 조금 간격을 두고 다시 두드립니다.
PERSIST_RETRY_DELAYS = (2.0, 5.0)

# ---------------------------------------------------------------- 스트리밍 조각

# 스트리밍 조각 이벤트를 모아 보내는 간격(초).
#
# 예전에는 LLM 토큰 하나마다 이벤트를 한 통씩 보냈습니다. 초당 수십 통이 러너를 거쳐
# 모든 화면의 구독 큐로 퍼지고, 화면이 잠깐만 느려도 큐(`MAX_QUEUED_EVENTS`)가 차서
# 그 화면이 조용히 구독에서 빠졌습니다 — 카드가 스트리밍 도중 멈춘 채 남았습니다.
# 0.1초는 사람 눈에 끊겨 보이지 않을 만큼 짧고, 이벤트 수는 10분의 1 아래로 줄입니다.
STREAM_EVENT_INTERVAL = 0.1

# ---------------------------------------------------------------- 합성 결과

# 합성 결론으로 보지 않는 줄 — 응답 한도·빈 답변 등을 알리는 꼬리표(`> ⚠️ ...`).
_NOTICE_LINE = re.compile(r"^\s*>\s*⚠️")

# 이번 턴 전문가 발언에서 모아 산출물로 올릴 코드의 최대 개수. 초안과 수정본이 여러 번
# 오가는 토론에서 탭이 끝없이 늘지 않게 합니다.
MAX_DEBATE_CODE_ARTIFACTS = 12


def format_roster(agents: List[Agent], *, with_keys: bool = False) -> str:
    """오케스트레이터에게 보여 줄 전문가 목록. 계획·발언자 지명·과업 분배가 함께 씁니다.

    이름과 역할, 쓸 수 있는 도구 서버 이름만 적습니다. 시스템 프롬프트는 넣지 않습니다 —
    누구에게 무엇을 맡길지 정하는 데는 이것으로 충분하고, 프롬프트 전문은 호출마다 수천
    토큰입니다. 도구 서버는 "파일 쓰기는 누구에게" 를 가르는 데 필요해 이름만 붙입니다
    (스키마는 넣지 않습니다). 단계적 사고 서버는 일을 하는 도구가 아니라 뺍니다.

    `with_keys` 는 JSON 으로 에이전트 키를 돌려받는 호출(지명·분배)용입니다. 계획 발언은
    전문가들이 전사에서 자기 이름을 찾아 읽으므로 이름으로 부르게 합니다.
    """
    lines = []
    for agent in agents:
        thinking_server = agent.sequential_thinking.mcp_server
        servers = [s for s in agent.allowed_mcp_servers if s != thinking_server]
        head = f"{agent.key}: {agent.name}" if with_keys else agent.name
        tools = f" · 도구: {', '.join(servers)}" if servers else " · 도구: 없음"
        lines.append(f"- {head} ({agent.role}){tools}")
    return "\n".join(lines)


def synthesis_has_content(text: str) -> bool:
    """합성 발언이 실제로 결론을 담았는가.

    오케스트레이터가 빈 답을 내면 예전에는 **빈 보고서가 정상 결론의 제목으로** 저장됐고,
    화면은 그 턴의 산출물로 뷰어를 통째로 바꿔 이전 결론까지 사라져 보였습니다. 꼬리표만
    붙은 답(응답 한도, 답이 사고 안에만 있음 등)도 결론이 아닙니다.
    """
    lines = (text or "").strip().splitlines()
    # native 모드의 사고 인용 블록만 있고 답이 없는 경우. `strip_reasoning_trace` 는 이때
    # 사고라도 넘기려고 원문을 지키므로, 여기서 따로 걷어냅니다.
    if lines and lines[0].startswith(NATIVE_REASONING_HEADER):
        while lines and (lines[0].startswith(">") or not lines[0].strip()):
            lines.pop(0)
    body = "\n".join(line for line in lines if not _NOTICE_LINE.match(line))
    return bool(strip_reasoning_trace(body).strip())


# DB 에 끝내 기록하지 못한 발언·산출물을 남기는 폴더.
#
# 예전에는 기록 실패를 로그 한 줄로 남기고 넘어갔습니다. 화면에는 이미 흘러갔으니
# 괜찮아 보이지만, 새로고침하거나 앱을 다시 켜면 **최종 합성 보고서가 사라졌습니다.**
# 실제로 자리를 비운 사이 `database is locked` 로 그렇게 됐습니다. 대화의 산출물을
# 조용히 잃는 것보다는, DB 가 아닌 곳에라도 남기고 사람에게 알리는 편이 낫습니다.
UNSAVED_DIR = DATA_DIR / "unsaved"


def save_unpersisted(
    *,
    kind: str,
    session_id: str,
    title: str,
    body: str,
    error: BaseException,
    directory: Optional[Path] = None,
) -> Optional[Path]:
    """DB 에 기록하지 못한 내용을 마크다운 파일로 남깁니다. 파일마저 못 쓰면 None.

    파일 이름에 무작위 꼬리를 붙이는 이유: 같은 초에 두 발언이 실패하면 앞의 것을
    덮어씁니다.
    """
    folder = directory or UNSAVED_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{session_id[:8]}-{kind}-{uuid.uuid4().hex[:6]}.md"
    first_line = str(error).splitlines()[0] if str(error) else ""
    header = (
        f"<!-- MADO: DB 에 기록하지 못한 {kind} -->\n"
        f"<!-- session_id: {session_id} -->\n"
        f"<!-- saved_at: {datetime.now().astimezone().isoformat(timespec='seconds')} -->\n"
        f"<!-- error: {type(error).__name__}: {first_line} -->\n\n"
        f"# {title}\n\n"
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(header + body, encoding="utf-8")
        return path
    except OSError as exc:
        logger.error(f"Could not even write the unsaved {kind} to {path}: {exc}")
        return None


class OrchestratorEngine:
    """Multi-Agent Orchestration & Debate Execution Engine."""

    def __init__(self, agent_pool: Optional[AgentPool] = None, llm_caller: Optional[LLMCaller] = None):
        self.agent_pool = agent_pool or get_agent_pool()
        self.llm_caller = llm_caller or LLMCaller()
        self.session_factory = get_session_factory()
        # 진행 중인 턴의 도구 보안 문지기 (세션 id → 게이트). 턴이 열 때 만들고 닫을 때
        # 치웁니다 (`run_turn`). 발언뿐 아니라 지명·계획·요약 같은 보조 호출도 에이전트의
        # 도구를 들고 나가므로, 모든 `call_agent` 가 같은 게이트를 거칩니다.
        self._tool_gates: Dict[str, ToolGate] = {}

    # ------------------------------------------------------------------ 작업 공간

    async def set_tool_rules(self, session_id: str, grants: List[str], denials: List[str]) -> bool:
        """진행 중인 턴의 "이 대화에서" 규칙을 갈아 끼웁니다. 도는 턴이 없으면 False.

        True 면 저장까지 게이트가 했습니다. 토론 중에는 게이트가 이 목록의 유일한
        기록자여야, 카드에서 방금 더한 규칙을 화면의 옛 목록이 덮지 않습니다.
        """
        gate = getattr(self, "_tool_gates", {}).get(session_id)
        if gate is None:
            return False
        await gate.replace_rules(grants, denials)
        return True

    def set_tool_mode(self, session_id: str, mode: str) -> bool:
        """진행 중인 턴의 도구 보안 모드를 바꿉니다. 도는 턴이 없으면 False.

        저장은 화면이 합니다 (세션 DB). 여기서는 이미 세워진 문지기에 알려, 수많은 승인
        카드를 보고 모드를 바꾼 사람이 다음 턴까지 기다리지 않게 합니다.
        """
        gate = getattr(self, "_tool_gates", {}).get(session_id)
        if gate is None:
            return False
        gate.set_mode(mode)
        return True

    def _gate_for(self, state: DebateState) -> Optional[ToolGate]:
        """이 턴의 도구 보안 문지기. 턴 밖에서 부른 발언(테스트)이면 None."""
        return getattr(self, "_tool_gates", {}).get(state.session_id)

    def _rule_saver(
        self, session_id: str,
    ) -> Callable[[List[str], List[str]], Coroutine[Any, Any, None]]:
        """"이 대화에서 허용·거부" 목록을 세션에 저장하는 함수. 다음 턴의 게이트가 읽습니다."""

        async def save(grants: List[str], denials: List[str]) -> None:
            async with self.session_factory() as db:
                row = await db.get(SessionModel, session_id)
                if row is not None:
                    row.tool_grants = list(grants)
                    row.tool_denials = list(denials)
                    await db.commit()

        return save

    @staticmethod
    def _mcp_for(state: DebateState) -> Optional[MCPManager]:
        """이 턴이 빌린 MCP 런타임.

        예전에는 전역 매니저 하나를 이 턴의 작업 공간으로 **다시 띄웠습니다**.
        그래서 폴더가 다른 토론이 동시에 돌 수 없었습니다. 지금은 턴이 시작할 때
        그 폴더의 런타임을 빌리고(`run_turn`), 발언마다 그것을 내려보냅니다.

        None 을 돌려주면 `LLMCaller` 가 자기 기본 런타임으로 물러섭니다 — 풀을
        거치지 않고 엔진을 직접 부르는 테스트가 그 경로를 씁니다.
        """
        return get_runtime_pool().get(state.workspace_dir)

    # ------------------------------------------------------------------ 발언

    @staticmethod
    def _unavailable_notice(agent: Agent, exc: LLMUnavailableError, streamed: str) -> str:
        """응답을 받지 못했을 때 화면과 기록에 남는 문구.

        여기서 그럴듯한 대체 발언을 만들어 내면 다음 에이전트의 입력과 최종 합성
        보고서까지 그 거짓말 위에 쌓입니다. 못 받았다고 적는 편이 낫습니다.
        """
        notice = (
            f"> ⚠️ **연결 끊김 — {agent.name} 의 발언을 받지 못했습니다.**\n"
            f">\n"
            f"> - 모델: `{agent.model}`\n"
            f"> - 엔드포인트: `{exc.endpoint}`\n"
            f"> - 원인: `{exc.reason}`\n"
            f">\n"
            f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다. "
            f"엔드포인트를 복구한 뒤 같은 요청을 다시 보내주세요."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 연결이 끊기기 전까지 도착한 부분입니다.)"
            )
        return notice

    @staticmethod
    def _crashed_notice(agent: Agent, exc: BaseException, streamed: str) -> str:
        """발언이 예기치 못한 오류로 끊겼을 때 화면과 기록에 남는 문구.

        `LLMUnavailableError` 는 "엔드포인트에 닿지 못했다" 는 알려진 실패이고,
        이쪽은 우리가 예상하지 못한 실패입니다 — 도구 서버가 이상한 것을 돌려
        주었거나, 프로바이더 응답 모양이 달랐거나, 우리 코드가 틀렸거나.
        어느 쪽이든 **한 에이전트의 사고가 토론 전체를 끝낼 이유는 없습니다.**
        나머지 에이전트는 아직 말할 수 있고, 지금까지의 발언으로 합성도 됩니다.
        """
        notice = (
            f"> ⚠️ **발언 중단 — {agent.name} 의 발언이 오류로 끊겼습니다.**\n"
            f">\n"
            f"> - 모델: `{agent.model}`\n"
            f"> - 오류: `{type(exc).__name__}: {exc}`\n"
            f">\n"
            f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다. "
            f"토론은 나머지 에이전트로 계속됩니다 (자세한 원인은 서버 로그를 보세요)."
        )
        if streamed.strip():
            return (
                f"{streamed.rstrip()}\n\n---\n\n{notice}\n>\n"
                f"> (위 본문은 오류가 나기 전까지 도착한 부분입니다.)"
            )
        return notice

    def _make_budget_arbiter(
        self,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, int]]]:
        """도구 호출 상한에 닿았을 때 사람에게 물어보는 통로를 만듭니다.

        `control` 이 없으면 (배치 실행, 테스트) None 을 돌려줍니다. 그러면
        LLM 쪽은 아무도 묻지 않고 곧장 "도구 없이 마무리" 로 갑니다 — 물어볼
        사람이 없는데 3분을 기다릴 이유가 없습니다.

        상한은 `TOOL_ITERATION_CEILING` 을 넘길 수 없습니다. 확장은 폭주를
        늦추는 것이지 푸는 것이 아닙니다.
        """
        if control is None:
            return None

        async def arbiter(info: Dict[str, Any]) -> int:
            limit = int(info.get("limit", 0))
            headroom = max(0, TOOL_ITERATION_CEILING - limit)

            async def _open(request) -> None:
                if on_event:
                    await on_event({"type": "tool_budget_exhausted", **request.describe()})

            request = await control.ask_tool_budget(
                agent_key=str(info.get("agent_key", "")),
                agent_name=str(info.get("agent_name", "")),
                limit=limit,
                used=int(info.get("used", 0)),
                tool_calls=int(info.get("tool_calls", 0)),
                max_extension=headroom,
                on_open=_open,
            )
            if on_event:
                await on_event({
                    "type": "tool_budget_resolved",
                    "id": request.id,
                    "agent_name": request.agent_name,
                    "outcome": request.outcome,
                    "granted": request.granted,
                    "limit": limit + request.granted,
                })
            return request.granted

        return arbiter

    def _make_context_arbiter(
        self,
        state: DebateState,
        agent: Agent,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]]:
        """컨텍스트 창이 넘쳤을 때 사람에게 물어보는 통로를 만듭니다.

        도구 예산 쪽과 두 가지가 다릅니다.

        1. **턴에 한 번만 묻습니다.** 컨텍스트는 라운드가 쌓이면 거의 모든 발언이
           같은 벽에 부딪힙니다. 발언마다 물으면 토론을 진행할 수 없으므로, 첫
           답(`state.context_grant`)을 그 턴의 나머지에 그대로 씁니다.
        2. **모델의 실제 한도를 넘겨 늘리지 않습니다.** `context_headroom()` 이
           조회한 여유가 상한입니다. 넘겨 올리면 깔끔한 트림이 400 으로 바뀝니다.
        """
        if control is None:
            return None

        async def arbiter(info: Dict[str, Any]) -> Dict[str, Any]:
            # 이미 이번 턴에 답을 받았으면 다시 묻지 않습니다.
            if state.context_grant is not None:
                return {"granted": state.context_grant, "wrap_up": False}

            async def _open(request) -> None:
                if on_event:
                    await on_event({"type": "context_window_exhausted", **request.describe()})

            request = await control.ask_context_window(
                agent_key=str(info.get("agent_key", "")),
                agent_name=str(info.get("agent_name", "")),
                window=int(info.get("window", 0)),
                used=int(info.get("used", 0)),
                headroom=info.get("headroom"),
                tool_calls=int(info.get("tool_calls", 0)),
                on_open=_open,
            )
            state.context_grant = request.granted
            if on_event:
                await on_event({
                    "type": "context_window_resolved",
                    "id": request.id,
                    "agent_name": request.agent_name,
                    "outcome": request.outcome,
                    "granted": request.granted,
                    "window": int(info.get("window", 0)) + request.granted,
                })
            # 시간이 지나 답을 못 받았으면 접지 않고 생략하며 진행합니다. 사람이
            # 자리에 없다고 해서 토론을 접을 이유는 없습니다.
            return {
                "granted": request.granted,
                "wrap_up": request.outcome == "wrap_up",
            }

        return arbiter

    async def _speak(
        self,
        *,
        db,
        state: DebateState,
        agent: Agent,
        prompt_messages: List[Dict[str, Any]],
        custom_instructions: str,
        round_number: int,
        msg_type: str,
        on_event: Optional[EventCallback],
        control: Optional[TurnControl] = None,
        db_lock: Optional[asyncio.Lock] = None,
        created_at: Optional[datetime] = None,
        post_process: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
        turn_started_at: Optional[datetime] = None,
        graph_node_id: Optional[str] = None,
        graph_port: Optional[str] = None,
    ) -> DebateMessage:
        """한 에이전트의 발언을 스트리밍하고, DB 에 기록하고, 상태에 반영합니다.

        `turn_started_at` 은 이 발언이 턴을 마무리하는 합성 발언일 때만 줍니다.
        그 턴의 총 경과 시간을 기록에서 다시 계산할 수 있게 함께 적습니다.

        `db_lock` 은 이 발언이 다른 발언과 **동시에** 진행될 때만 필요합니다
        (병렬 지시 전략). `db` 는 세션 하나를 공유하는데 SQLAlchemy AsyncSession 은
        동시 사용을 허용하지 않습니다 — 두 발언이 같은 순간에 커밋하면
        `IllegalStateChangeError` 로 토론이 통째로 죽습니다. 락은 LLM 호출이 아니라
        기록 구간에만 걸리므로 병렬성은 그대로입니다.

        `created_at` 을 주면 그 시각으로 기록합니다. 병렬 라운드에서는 발언이 끝나는
        순서가 제각각이라, 커밋 시각을 그대로 쓰면 새로고침한 화면의 발언 순서가
        매번 달라집니다 (기록은 `created_at` 으로 정렬해 다시 읽힙니다). 지시받은
        순서를 시각에 박아 두면 실시간 화면과 다시 연 화면이 같은 순서를 보여줍니다.

        LLM 이 응답하지 못하면 실패 사실을 그대로 적은 `msg_type="error"` 발언을
        남기고 토론을 계속합니다 (다른 에이전트는 아직 살아 있을 수 있습니다).

        이 발언 중에 실행된 MCP 도구도 여기서 함께 기록합니다. 어느 발언이 부른
        도구인지는 이 자리에서만 알 수 있습니다 — 밖에서 기록하던 예전 방식은
        `message_id` 를 비워 둘 수밖에 없었고, 그래서 새로고침한 화면과 저장
        파일에서 도구 기록이 발언과 따로 놀았습니다.
        """
        msg_id = str(uuid.uuid4())
        # 벽시계 시작 시각. `created_at` 과 따로 둡니다 — 그쪽은 발언이 끝난 뒤에
        # 들어가는 정렬 키라서, 병렬 라운드에서는 실제 시각이 아닙니다.
        started_at = utc_now()
        if on_event:
            await on_event({
                "type": "message_stream_start",
                "message": {
                    "id": msg_id,
                    "sender_key": agent.key,
                    "sender_name": agent.name,
                    "sender_role": agent.role,
                    "content": "",
                    "round_number": round_number,
                    "msg_type": msg_type,
                    "started_at": started_at,
                    "graph_node_id": graph_node_id,
                    "graph_port": graph_port,
                },
            })

        # 이 발언이 실행한 도구. 모아 두었다가 발언 행과 **같은 커밋**에 넣습니다.
        # 도구가 끝나는 즉시 넣으면, 발언 행이 아직 없는 동안 존재하지 않는 발언을
        # 가리키는 행이 남습니다. SQLite 가 외래키를 검사하지 않아 지금은 통과할
        # 뿐이고, 누군가 PRAGMA foreign_keys 를 켜는 날 삽입이 실패합니다.
        executed_tools: List[Dict[str, Any]] = []

        async def _on_tool_call(call_log: Dict[str, Any]) -> None:
            executed_tools.append(call_log)
            if on_event:
                await on_event({
                    "type": "tool_executed",
                    "agent_key": agent.key,
                    "agent_name": agent.name,
                    "tool_call": call_log,
                })

        # 컨텍스트 한도로 무언가 생략됐다는 사실. 예전에는 logger 에만 남아,
        # 토론 기록이 사라지는 것을 보고 있는 사람이 알 방법이 없었습니다.
        def _on_context_trim(dropped: int) -> None:
            state.context_dropped += dropped
            trimmed_here.append(dropped)

        trimmed_here: List[int] = []
        streamed: List[str] = []

        # 조각은 모았다가 `STREAM_EVENT_INTERVAL` 마다 한 통으로 보냅니다.
        #
        # "다음 조각이 올 때 간격이 지났으면 보낸다" 로 하면 안 됩니다. 모델이 한 줄
        # 쓰고 30초 걸리는 도구를 부르면, 그 한 줄은 다음 조각이 올 때까지 30초 동안
        # 화면에 나오지 않습니다. 그래서 첫 조각이 쌓이는 순간 짧은 예약을 걸어, 뒤에
        # 무엇이 오든 오지 않든 그 간격 안에 내보냅니다.
        pending_chunks: List[str] = []
        flush_task: Optional[asyncio.Task] = None
        flush_lock = asyncio.Lock()

        async def _flush_chunks() -> None:
            # 순서를 지키려고 한 번에 하나만 내보냅니다. 모은 것을 꺼내는 데에는
            # await 가 없어, 꺼낸 뒤에 들어온 조각은 다음 통에 실립니다.
            async with flush_lock:
                if not pending_chunks or not on_event:
                    return
                delta = "".join(pending_chunks)
                pending_chunks.clear()
                await on_event({
                    "type": "message_stream_chunk",
                    "message_id": msg_id,
                    "delta": delta,
                })

        async def _flush_later() -> None:
            await asyncio.sleep(STREAM_EVENT_INTERVAL)
            await _flush_chunks()

        def _cancel_flush() -> None:
            # 기다리지 않고 취소만 합니다. 예약은 sleep 에서 멈춰 있거나 아직 시작하지
            # 않았으므로, 취소하면 내보내기 전에 끝납니다.
            if flush_task is not None and not flush_task.done():
                flush_task.cancel()

        async def _drain_chunks() -> None:
            """남은 조각을 지금 내보냅니다. 발언을 확정하기 전에 반드시 부릅니다.

            예약을 남겨 두면 `message_added` 로 발언이 확정된 **뒤에** 조각이 한 통 더
            도착하고, 러너의 스냅샷은 그것을 확정본 끝에 한 번 더 붙입니다.
            """
            _cancel_flush()
            await _flush_chunks()

        async def _on_chunk(delta: str) -> None:
            nonlocal flush_task
            streamed.append(delta)
            if not on_event:
                return
            pending_chunks.append(delta)
            if flush_task is None or flush_task.done():
                flush_task = asyncio.create_task(_flush_later())

        try:
            content, tool_logs = await self.llm_caller.call_agent(
                agent, prompt_messages, custom_instructions,
                on_tool_call=_on_tool_call, on_chunk=_on_chunk,
                session_id=state.session_id,
                budget_arbiter=self._make_budget_arbiter(control, on_event),
                context_arbiter=self._make_context_arbiter(state, agent, control, on_event),
                on_context_trim=_on_context_trim,
                mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
                ledger=state.decision_ledger,
            )
            # 다이어그램 교정 같은 후처리는 시간이 걸립니다. 그동안 카드가 마지막 조각
            # 직전에서 멈춰 보이지 않게, 흘러온 글을 먼저 다 내보냅니다.
            await _drain_chunks()
            # 확정본이 비었는데 화면에는 글이 흘러갔다면 그 글을 남깁니다.
            # 여기서 정하는 `content` 가 DB 에 들어가는 값이라, 비워 둔 채로
            # 넘어가면 사람이 방금 읽던 발언이 새로고침 뒤에도 돌아오지 않습니다
            # (프로바이더가 텍스트를 reasoning 쪽으로만 주거나, 스트림 재조립이
            # 본문을 놓친 경우에 실제로 그렇게 됩니다).
            if not content.strip():
                content = "".join(streamed)
            final_type = msg_type
            if post_process is not None:
                # 기록과 화면에 남을 본문을 확정하기 **전에** 손봅니다. 스트리밍
                # 카드는 `message_added` 가 올 때 최종본으로 덮이므로, 여기서
                # 고치면 사람이 보는 것도 고쳐진 쪽입니다.
                try:
                    content = await post_process(content)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - 후처리 실패로 발언을 잃지 않습니다
                    logger.error(
                        f"Post-processing {agent.name}'s message failed: "
                        f"{type(exc).__name__}: {exc}",
                        exc_info=True,
                    )
        except LLMUnavailableError as exc:
            logger.warning(f"Agent '{agent.key}' produced no response: {exc}")
            content = self._unavailable_notice(agent, exc, "".join(streamed))
            tool_logs = []
            final_type = "error"
            if agent.key not in state.failed_agent_keys:
                state.failed_agent_keys.append(agent.key)
        except asyncio.CancelledError:
            # 사용자가 토론을 끊었거나 서버가 내려가는 중입니다. 실패로 적지 않고
            # 그대로 올려 보냅니다 — 여기서 삼키면 취소가 먹지 않습니다.
            _cancel_flush()
            raise
        except BaseException as exc:  # noqa: BLE001 - 한 발언의 사고로 토론을 끝내지 않습니다
            logger.error(
                f"Agent '{agent.key}' crashed while speaking: {type(exc).__name__}: {exc}",
                exc_info=True,
            )
            content = self._crashed_notice(agent, exc, "".join(streamed))
            tool_logs = []
            final_type = "error"
            if agent.key not in state.failed_agent_keys:
                state.failed_agent_keys.append(agent.key)

        # 실패로 끝났어도 예약된 조각이 확정본 뒤에 도착하면 안 됩니다.
        await _drain_chunks()

        # 응답이 끝난 시각. 실패로 끝난 발언도 여기서 잽니다 — 언제 포기했는지는
        # 그 자체로 쓸모 있는 기록입니다 (엔드포인트가 몇 초 만에 끊었는지, 한도까지
        # 버텼는지). 기록 락을 기다린 시간이 섞이지 않도록 락 **밖에서** 잽니다.
        finished_at = utc_now()

        def _message_rows() -> List[Any]:
            rows: List[Any] = [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key=agent.key,
                sender_name=agent.name,
                sender_role=agent.role,
                content=content,
                round_number=round_number,
                msg_type=final_type,
                started_at=started_at,
                finished_at=finished_at,
                turn_started_at=turn_started_at,
                graph_node_id=graph_node_id,
                graph_port=graph_port,
                **({"created_at": created_at} if created_at is not None else {}),
            )]
            for call_log in executed_tools:
                security = call_log.get("security") or {}
                rows.append(ToolCallRecordModel(
                    id=str(uuid.uuid4()),
                    session_id=state.session_id,
                    message_id=msg_id,
                    agent_key=agent.key,
                    tool_name=call_log.get("tool_name", ""),
                    arguments=call_log.get("arguments", {}),
                    output=call_log.get("output", ""),
                    status=call_log.get("status", "success"),
                    # 도구 보안 판정 (`app/orchestration/tool_gate.py`). 판정 없이 실행된
                    # 호출(게이트 밖의 발언)은 비어 있습니다.
                    decision=str(security.get("decision") or ""),
                    risk=str(security.get("risk") or ""),
                    rule=str(security.get("rule") or ""),
                    approver=str(security.get("approver") or ""),
                ))
            return rows

        # 최종 합성이면 파일 이름과 알림에서 그렇다고 밝힙니다. 사람이 먼저 찾는 것이
        # 그 보고서입니다. `turn_started_at` 은 합성 발언에만 주어집니다.
        is_synthesis = turn_started_at is not None
        async with (db_lock or nullcontext()):
            await self._persist(
                db, _message_rows,
                what=("the final synthesis report" if is_synthesis else f"{agent.name}'s message"),
                label=("최종 합성 보고서" if is_synthesis else f"{agent.name} 의 발언"),
                kind=("synthesis" if is_synthesis else f"message-{agent.key}"),
                session_id=state.session_id,
                fallback_title=f"{agent.name} ({agent.role}) — Round {round_number}",
                fallback_body=content,
                on_event=on_event,
            )

        if trimmed_here and on_event:
            await on_event({
                "type": "context_trimmed",
                "agent_key": agent.key,
                "agent_name": agent.name,
                "dropped": sum(trimmed_here),
                "total_dropped": state.context_dropped,
                "where": "speech",
            })

        message = DebateMessage(
            id=msg_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=final_type,
            # 발언이 실패로 끝나면 `call_agent` 는 도구 기록을 돌려주지 못합니다.
            # 그전에 실제로 실행된 것은 남아 있어야 합니다.
            tool_calls=tool_logs or executed_tools,
            started_at=started_at,
            finished_at=finished_at,
            turn_started_at=turn_started_at,
            graph_node_id=graph_node_id,
            graph_port=graph_port,
        )
        state.messages.append(message)

        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    # ------------------------------------------------------------------ 기록

    async def _persist(
        self,
        db,
        build: Callable[[], List[Any]],
        *,
        what: str,
        label: str,
        kind: str,
        session_id: str,
        fallback_title: str,
        fallback_body: str,
        on_event: Optional[EventCallback],
    ) -> bool:
        """행을 기록합니다. 실패하면 간격을 두고 다시 시도하고, 끝내 실패하면 파일로 남깁니다.

        `build` 는 **시도할 때마다 새 행을 만들어** 돌려줘야 합니다. 실패한 커밋을 롤백하면
        그 트랜잭션에 넣었던 객체는 세션에서 떨어져 나가, 같은 객체를 다시 넣는 것보다
        새로 만드는 편이 확실합니다 (id 는 호출하는 쪽이 고정해 두므로 같은 행입니다).

        롤백을 빠뜨리면 세션이 깨진 채로 남아 이 대화의 다음 커밋이 전부 실패합니다.

        예외를 올리지 않습니다. 기록 실패로 토론이 멈추면, 이미 나온 발언 뒤로 이어질
        발언까지 잃습니다. 대신 잃지 않게 파일로 남기고 사람에게 알립니다. 취소만은
        그대로 올립니다.
        """
        attempts = len(PERSIST_RETRY_DELAYS) + 1
        last: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            db.add_all(build())
            try:
                await db.commit()
                if attempt > 1:
                    logger.info(f"Persisted {what} on attempt {attempt}/{attempts}")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 어떤 기록 실패로도 토론을 멈추지 않습니다
                last = exc
                logger.warning(
                    f"Could not persist {what} (attempt {attempt}/{attempts}): "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug("Rollback after a failed commit also failed", exc_info=True)
                if attempt < attempts:
                    await asyncio.sleep(PERSIST_RETRY_DELAYS[attempt - 1])

        assert last is not None
        saved_to = save_unpersisted(
            kind=kind, session_id=session_id, title=fallback_title,
            body=fallback_body, error=last,
        )
        logger.error(
            f"Gave up persisting {what} after {attempts} attempts "
            f"({type(last).__name__}: {last}); "
            + (f"saved to {saved_to}" if saved_to else "could not save it to a file either"),
            exc_info=last,
        )
        if on_event:
            first_line = str(last).splitlines()[0] if str(last) else ""
            try:
                await on_event({
                    "type": "persist_failed",
                    "what": what,
                    "label": label,
                    "error": f"{type(last).__name__}: {first_line}",
                    "saved_to": str(saved_to) if saved_to else None,
                })
            except Exception:  # noqa: BLE001 - 알리다 실패해도 토론은 계속됩니다
                logger.debug("persist_failed notification failed", exc_info=True)
        return False

    # ------------------------------------------------------------------ 유저 발언

    async def _record_user_message(
        self,
        *,
        db,
        state: DebateState,
        content: str,
        round_number: int,
        on_event: Optional[EventCallback],
    ) -> DebateMessage:
        """유저 발언을 기록에 남기고 화면에 흘립니다.

        턴을 여는 최초 요청과 토론 도중의 개입이 같은 자리에 같은 모양으로
        들어가야, 다음 발언자의 맥락(`_build_context_for_agent`)과 합성 전사가
        둘을 구분 없이 읽습니다.
        """
        msg_id = str(uuid.uuid4())
        # 사람 발언에는 걸리는 시간이 없습니다. 시작과 끝을 같은 시각으로 적어,
        # 읽는 쪽이 "시각이 없는 옛 기록" 과 구분할 수 있게 합니다.
        now = utc_now()
        await self._persist(
            db,
            lambda: [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key="user",
                sender_name="User",
                # 역할은 비웁니다 — 유저는 한 명이고 요청하는 쪽도 늘 그 유저입니다.
                sender_role="",
                content=content,
                round_number=round_number,
                msg_type="user",
                started_at=now,
                finished_at=now,
            )],
            what="the user's message",
            label="유저 발언",
            kind="message-user",
            session_id=state.session_id,
            fallback_title=f"User — Round {round_number}",
            fallback_body=content,
            on_event=on_event,
        )

        message = DebateMessage(
            id=msg_id,
            sender_key="user",
            sender_name="User",
            sender_role="",
            content=content,
            round_number=round_number,
            msg_type="user",
            started_at=now,
            finished_at=now,
        )
        state.messages.append(message)

        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    async def _apply_interjections(
        self,
        *,
        db,
        state: DebateState,
        control: Optional[TurnControl],
        round_number: int,
        on_event: Optional[EventCallback],
    ) -> int:
        """대기 중인 사용자 개입을 지금 시점의 토론 기록에 밀어 넣습니다.

        발언이 진행되는 중간에 끼워 넣으면, 그 발언의 프롬프트는 이미 만들어진
        뒤라 반영되지도 않으면서 기록 순서만 어긋납니다. 그래서 호출 지점은 항상
        발언과 발언 사이입니다. 여기서 들어간 메모는 다음 발언자의 맥락에 그대로
        실립니다.
        """
        if control is None:
            return 0
        notes = control.drain_notes()
        for note in notes:
            await self._record_user_message(
                db=db,
                state=state,
                content=f"{memory.INTERJECTION_PREFIX}\n{note}",
                round_number=round_number,
                on_event=on_event,
            )
        state.interjection_count += len(notes)
        if notes:
            logger.info(
                f"Applied {len(notes)} user interjection(s) to session {state.session_id} "
                f"at round {round_number}"
            )
        return len(notes)

    # ------------------------------------------------------------------ 턴

    async def _session_workspace(self, session_id: str) -> Path:
        """이 대화가 쓰기로 한 작업 공간. 대화를 찾을 수 없으면 기본값."""
        async with self.session_factory() as db:
            res = await db.execute(
                select(SessionModel.workspace_dir).where(SessionModel.id == session_id)
            )
            return resolve_workspace_dir(res.scalar_one_or_none() or None)

    async def run_turn(
        self,
        session_id: str,
        user_prompt: str,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
    ) -> DebateState:
        """Executes a full multi-agent collaborative debate and synthesis turn.

        `control` 이 주어지면 발언과 발언 사이마다 사용자의 정지 요청과 개입
        메모를 확인합니다. 정지는 태스크를 죽이는 것이 아니라 남은 라운드를
        건너뛰고 최종 합성으로 넘어가는 것이라, 지금까지의 토론으로도 산출물이
        나옵니다.

        **이 대화의 작업 공간에 해당하는 MCP 런타임을 빌린 채로** 돕니다.
        filesystem 은 허용 경로를 argv 로, sandbox 는 `SANDBOX_WORKSPACE` 를 env 로
        기동 시점에 받으므로 폴더마다 프로세스 묶음이 따로 있어야 합니다. 같은
        폴더를 쓰는 다른 대화가 이미 빌려 놓았으면 그것을 함께 씁니다.

        반납은 턴이 **어떻게 끝나든** 일어나야 합니다 — 정상 종료, 사용자 정지,
        취소, 예외 전부. 반납을 놓치면 그 런타임은 아무도 안 쓰는 채로 종료 때까지
        남고, 그만큼 다음 대화가 쓸 자리가 줄어듭니다.
        """
        pool = get_runtime_pool()
        workspace = await self._session_workspace(session_id)
        await pool.acquire(workspace, holder=session_id)
        try:
            return await self._run_turn(session_id, user_prompt, workspace, on_event, control)
        finally:
            self._tool_gates.pop(session_id, None)
            await pool.release(session_id, workspace)

    async def _run_turn(
        self,
        session_id: str,
        user_prompt: str,
        workspace: Path,
        on_event: Optional[EventCallback] = None,
        control: Optional[TurnControl] = None,
    ) -> DebateState:
        """`run_turn` 의 본문. 런타임은 이미 빌린 상태로 들어옵니다."""
        async with self.session_factory() as db:
            # 1. Load session config from DB
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            res = await db.execute(stmt)
            session_model = res.scalar_one_or_none()
            if not session_model:
                raise ValueError(f"Session with ID '{session_id}' not found.")

            # 옛 이름으로 저장된 대화도 지금 쓰는 전략으로 옮겨 돌립니다.
            strategy_name = resolve_strategy_name(session_model.strategy)
            max_rounds = session_model.max_rounds
            # 병렬 지시 전략에서만 읽힙니다. 0 이나 음수는 "동시에 아무도 못 돈다"
            # 는 뜻이 되어 라운드가 통째로 비므로 최소 1 로 올립니다.
            parallel_limit = max(1, int(session_model.parallel_limit or 3))
            active_keys = session_model.active_agents or ["orchestrator", "architect", "coder", "critic"]
            custom_instructions = session_model.custom_instructions or ""
            # 도구 보안: 이 대화의 모드와 "이 대화에서 허용·거부" 목록으로 문지기를 세웁니다.
            self._tool_gates[session_id] = ToolGate(
                session_id=session_id,
                mode=session_model.tool_mode or "",
                grants=list(session_model.tool_grants or []),
                denials=list(session_model.tool_denials or []),
                control=control,
                on_event=on_event,
                save_rules=self._rule_saver(session_id),
            )

            # Ensure orchestrator is in active keys
            if "orchestrator" not in active_keys:
                active_keys = ["orchestrator"] + active_keys

            # 그래프 토론은 그래프에 놓인 에이전트가 곧 참여자입니다 (로스터 체크박스가 아니라).
            # 사용자 발언을 기록하기 **전에** 검사합니다 — 돌 수 없는 턴의 요청이 기록에 남으면
            # 다음 턴의 맥락에 답 없는 요청으로 끼어듭니다.
            graph_spec: Optional[GraphSpec] = None
            if get_strategy(strategy_name).runs_graph:
                graph_spec = await self._graph_for_turn(db, session_model, max_rounds)
                active_keys = ["orchestrator"] + graph_spec.agent_keys()

            # 세션 페르소나를 적용합니다. 첫 턴이면 이 시점에 기록되고 잠깁니다.
            active_agents = await prepare_agents_for_turn(
                db, session_model, self.agent_pool, active_keys
            )
            if graph_spec is not None:
                present = {a.key for a in active_agents}
                missing = [k for k in graph_spec.agent_keys() if k not in present]
                if missing:
                    raise GraphTurnError(
                        f"그래프의 에이전트를 준비하지 못했습니다: {', '.join(missing)}"
                    )
            orchestrator_agent = next(
                (a for a in active_agents if a.key == "orchestrator"),
                self.agent_pool.get_orchestrator(),
            )
            # 장부와 요약은 모든 참여자의 프롬프트에 실립니다. 상한을 가장 작은 창에 맞춥니다.
            memory_budget = min(
                (context_budget(a) for a in active_agents), default=context_budget(orchestrator_agent)
            )

            # 2. Initialize Debate State
            state = DebateState(
                session_id=session_id,
                user_prompt=user_prompt,
                workspace_dir=str(workspace),
                strategy=strategy_name,
                max_rounds=max_rounds,
                current_round=0,
                custom_instructions=custom_instructions,
                active_agent_keys=active_keys,
                status="planning",
                memory_budget=memory_budget,
            )

            # 이전 턴의 대화 기록을 DB에서 로드하여 대화 맥락을 보존합니다.
            stmt_prev = (
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
            res_prev = await db.execute(stmt_prev)
            prev_db_msgs = res_prev.scalars().all()
            for pm in prev_db_msgs:
                state.messages.append(
                    DebateMessage(
                        id=pm.id,
                        sender_key=pm.sender_key,
                        sender_name=pm.sender_name,
                        sender_role=pm.sender_role,
                        content=pm.content,
                        round_number=pm.round_number,
                        msg_type=pm.msg_type,
                        graph_node_id=pm.graph_node_id,
                        graph_port=pm.graph_port,
                    )
                )
            # 여기서부터가 이번 턴입니다. 산출물은 이 뒤의 발언에서만 모읍니다.
            state.turn_message_start = len(state.messages)
            self._load_memory(state, session_model)

            # 3. Record User Message in DB
            #
            # 이 요청이 기록된 시각이 이 턴의 시작입니다. 보고서와 저장 문서의 "총 경과"
            # 가 여기서 셉니다 — 기록에 남는 시각이라, 문서를 읽는 사람이 "요청 시각 →
            # 합성 종료" 로 같은 값을 다시 얻을 수 있습니다.
            opening = await self._record_user_message(
                db=db,
                state=state,
                content=user_prompt,
                round_number=0,
                on_event=on_event,
            )
            turn_started_at = opening.started_at

            # 4. Phase 1: Master Orchestrator Goal Analysis & Planning
            state.status = "planning"
            state.current_speaker = orchestrator_agent.name
            if on_event:
                await on_event({"type": "status_changed", "status": "planning", "speaker": orchestrator_agent.name})

            # 0라운드에서 업무를 나누려면 누가 있는지 알아야 합니다. 예전에는 첫 턴 프롬프트에
            # "(Architect, Coder, Critic)" 이 박혀 있어 로스터가 다른 세션에서 없는 사람에게
            # 일을 나눴고, 이후 턴에는 목록 자체가 없었습니다.
            specialists = [a for a in active_agents if a.key != "orchestrator"]
            roster_block = (
                f"[이번 토론 참여 전문가]\n{format_roster(specialists)}\n\n"
                if specialists else ""
            )
            assign_rule = (
                "위 전문가 각각에게, 목록의 이름 그대로 불러 이번 턴에 맡을 일과 산출물을 "
                "한두 문장으로 지시하세요. 도구가 필요한 일(파일 쓰기 등)은 그 도구를 가진 "
                "전문가에게 맡기고, 목록에 없는 역할에는 일을 주지 마세요."
                if specialists else ""
            )

            # 이전 턴의 사용자 발언은 250자 요약으로 넘기지 않고 전문을 고정합니다.
            # 계획이 1턴의 제약을 모르면 그 턴 전체가 제약을 어긴 채 시작합니다.
            plan_record = memory.build_user_record(
                state, model=orchestrator_agent.model,
                token_cap=int(context_budget(orchestrator_agent) * memory.USER_RECORD_SHARE),
            ).text
            plan_record_block = f"{plan_record}\n\n" if plan_record else ""

            if len(state.messages) > 1:
                history_snippets = []
                for m in state.messages[:-1]:
                    if m.msg_type == "error":
                        continue
                    # 자르기 **전에** 사고 과정을 뗍니다. 그러지 않으면 250자가
                    # 통째로 "Thought 1: ..." 머리말로 채워져, 정작 결론은 한 글자도
                    # 안 실립니다.
                    history_snippets.append(
                        f"{m.speaker}: {self._snippet(m, 250)}"
                    )
                history_text = "\n".join(history_snippets[-6:])
                orch_plan_prompt = [
                    {"role": "user", "content": (
                        f"[이전 대화 맥락]:\n{history_text}\n\n"
                        f"{plan_record_block}"
                        f"[신규 User Request]:\n{user_prompt}\n\n"
                        f"{roster_block}"
                        "위의 이전 세션 논의 맥락과 새로운 유저 요청을 종합 분석하여 이번 토론의 핵심 목표, "
                        "접근 방향, 각 전문가에게 부여할 발언 지침을 작성하세요. "
                        f"{assign_rule}"
                    )}
                ]
            else:
                orch_plan_prompt = [
                    {"role": "user", "content": (
                        f"[User Request]: {user_prompt}\n\n"
                        f"{roster_block}"
                        "위 요청을 분석하고 이번 토론의 핵심 목표, 접근 방향, 각 전문가에게 부여할 "
                        f"발언 지침을 작성하세요. {assign_rule}"
                    )}
                ]

            # 그래프의 시작 노드에서 "계획 포함" 을 끄면 계획 없이 요청만 흘려보냅니다.
            plan_message: Optional[DebateMessage] = None
            if graph_spec is None or graph_spec.start.plan:
                plan_message = await self._speak(
                    db=db,
                    state=state,
                    agent=orchestrator_agent,
                    prompt_messages=orch_plan_prompt,
                    custom_instructions=custom_instructions,
                    round_number=0,
                    msg_type="orchestrator",
                    on_event=on_event,
                    control=control,
                )
            if plan_message is not None and plan_message.msg_type != "error":
                # 뒤쪽 발언자가 자기 몫을 잊지 않도록 목표 메시지에 고정합니다.
                state.plan_index = len(state.messages) - 1

            # 5. Phase 2: Multi-Round Specialist Debate Loop
            strategy = get_strategy(strategy_name)
            state.status = "debating"

            # 계획 발언과 첫 라운드 사이도 개입이 반영되는 지점입니다.
            await self._apply_interjections(
                db=db, state=state, control=control, round_number=0, on_event=on_event
            )

            stopped_early = False
            if graph_spec is not None:
                # 그래프 토론은 라운드 대신 그래프의 단계로 돕니다. 계획·합성·산출물·장부는
                # 다른 전략과 같은 코드를 씁니다.
                stopped_early = await self._run_graph(
                    db=db,
                    state=state,
                    spec=graph_spec,
                    orchestrator=orchestrator_agent,
                    active_agents=active_agents,
                    parallel_limit=parallel_limit,
                    max_visits=max_rounds,
                    control=control,
                    on_event=on_event,
                    start_value=[
                        plan_message.id
                        if plan_message is not None and plan_message.msg_type != "error"
                        else opening.id
                    ],
                )
            else:
                for round_num in range(1, max_rounds + 1):
                    if control is not None and control.stop_requested:
                        stopped_early = True
                        break

                    state.current_round = round_num
                    if on_event:
                        await on_event({
                            "type": "round_started",
                            "round": round_num,
                            "max_rounds": max_rounds,
                        })

                    # 병렬 지시 전략은 라운드 전체를 다르게 돕니다 — 과업을 나눠 주고
                    # 동시에 띄운 뒤 취합합니다. 발언자를 한 명씩 세우는 아래 루프와
                    # 섞을 수 없어 라운드째로 갈라집니다.
                    if strategy.orchestrator_dispatches_parallel:
                        stopped_early = await self._run_parallel_round(
                            db=db,
                            state=state,
                            strategy=strategy,
                            orchestrator=orchestrator_agent,
                            active_agents=active_agents,
                            round_num=round_num,
                            custom_instructions=custom_instructions,
                            parallel_limit=parallel_limit,
                            control=control,
                            on_event=on_event,
                        )
                        if stopped_early:
                            break
                        if round_num < max_rounds and not (control is not None and control.stop_requested):
                            await self._update_ledger(
                                state=state, orchestrator=orchestrator_agent,
                                on_event=on_event, reason=f"Round {round_num}",
                            )
                        continue

                    speakers = await self._select_speakers(
                        db=db,
                        state=state,
                        strategy=strategy,
                        orchestrator=orchestrator_agent,
                        active_agents=active_agents,
                        round_num=round_num,
                        custom_instructions=custom_instructions,
                        on_event=on_event,
                    )

                    for speaker_index, agent in enumerate(speakers):
                        # 발언과 발언 사이. 사용자의 개입과 정지는 여기서만 반영됩니다.
                        # 진행 중이던 발언을 끊지 않으므로 잘린 기록이 남지 않습니다.
                        await self._apply_interjections(
                            db=db, state=state, control=control,
                            round_number=round_num, on_event=on_event,
                        )
                        if control is not None and control.stop_requested:
                            stopped_early = True
                            break

                        state.current_speaker = agent.name
                        if on_event:
                            await on_event({
                                "type": "status_changed",
                                "status": "debating",
                                "speaker": agent.name,
                                "round": round_num,
                            })

                        await self._speak(
                            db=db,
                            state=state,
                            agent=agent,
                            prompt_messages=await self._context_for_speech(
                                state,
                                agent,
                                strategy.turn_instruction(agent, speakers, speaker_index, state),
                                orchestrator=orchestrator_agent,
                                on_event=on_event,
                            ),
                            custom_instructions=custom_instructions,
                            round_number=round_num,
                            msg_type="agent",
                            on_event=on_event,
                            control=control,
                        )

                    if stopped_early:
                        break

                    # 마지막 라운드 뒤와 정지 요청 뒤에는 건너뜁니다. 곧바로 합성이 전사를 직접
                    # 읽고, 합성 뒤의 갱신이 이 라운드까지 함께 접습니다 — 같은 내용으로 두 번
                    # 부를 이유도, 정지를 원한 사람을 한 번 더 기다리게 할 이유도 없습니다.
                    if round_num < max_rounds and not (control is not None and control.stop_requested):
                        await self._update_ledger(
                            state=state, orchestrator=orchestrator_agent,
                            on_event=on_event, reason=f"Round {round_num}",
                        )

            # 정지 요청이 마지막 발언 도중에 들어왔더라도, 그때까지 쌓인 개입은
            # 합성 전사에 실어 보냅니다.
            await self._apply_interjections(
                db=db, state=state, control=control,
                round_number=state.current_round, on_event=on_event,
            )
            state.stopped_early = stopped_early
            if stopped_early:
                logger.info(
                    f"Debate for session {session_id} stopped early by the user at "
                    f"round {state.current_round}/{max_rounds}; synthesizing what we have."
                )

            # 6. Phase 3: Final Consensus & Artifact Synthesis
            state.status = "synthesizing"
            state.current_speaker = orchestrator_agent.name
            if on_event:
                await on_event({
                    "type": "status_changed",
                    "status": "synthesizing",
                    "speaker": orchestrator_agent.name,
                })

            async def _fix_diagrams(text: str) -> str:
                return await self._repair_mermaid_blocks(
                    text,
                    agent=orchestrator_agent,
                    custom_instructions=custom_instructions,
                    state=state,
                    on_event=on_event,
                )

            synth_message = await self._speak(
                db=db,
                state=state,
                agent=orchestrator_agent,
                prompt_messages=await self._synthesis_prompt_with_notice(
                    state, orchestrator_agent, on_event
                ),
                custom_instructions=custom_instructions,
                round_number=state.current_round + 1,
                msg_type="orchestrator",
                on_event=on_event,
                post_process=_fix_diagrams,
                turn_started_at=turn_started_at,
            )
            synthesis_failed = synth_message.msg_type == "error"
            failure_reason = "LLM 연결 끊김" if synthesis_failed else ""
            # 연결은 됐는데 결론이 비었습니다. 컨텍스트가 가득 찼거나, 답을 사고 안에만
            # 썼거나, 응답 한도를 사고에 다 쓴 경우입니다. 정상 결론으로 저장하면 안 됩니다.
            if not synthesis_failed and not synthesis_has_content(synth_message.content):
                synthesis_failed = True
                failure_reason = "빈 응답"
                logger.error(
                    f"Synthesis for session {session_id} came back empty; "
                    f"keeping each specialist's latest speech as the report instead"
                )

            # 7. Extract and Persist Artifacts
            artifacts = self._extract_artifacts_from_synthesis(
                session_id, synth_message.content, state, synthesis_failed=synthesis_failed,
                failure_reason=failure_reason,
                # 합성 발언이 끝난 시각. 다이어그램 자가 수선(`_fix_diagrams`)까지 마친
                # 뒤에 잰 값이라, 보고서 본문이 확정된 순간입니다.
                completed_at=synth_message.finished_at,
                turn_started_at=turn_started_at,
            )
            # id 를 먼저 정해 둡니다. 다시 시도할 때 같은 산출물이 같은 행이 되어야 합니다.
            for art in artifacts:
                art.id = str(uuid.uuid4())
                state.artifacts.append(art)

            if artifacts:
                fence = "`" * 3
                await self._persist(
                    db,
                    lambda: [
                        ArtifactModel(
                            id=art.id,
                            session_id=session_id,
                            artifact_type=art.artifact_type,
                            title=art.title,
                            content=art.content,
                            language=art.language,
                        )
                        for art in artifacts
                    ],
                    what=f"{len(artifacts)} artifact(s)",
                    label=f"산출물 {len(artifacts)}건",
                    kind="artifacts",
                    session_id=session_id,
                    fallback_title="산출물",
                    fallback_body="\n\n".join(
                        f"## {art.title} ({art.artifact_type})\n\n"
                        f"{fence}{art.language or ''}\n{art.content}\n{fence}"
                        for art in artifacts
                    ),
                    on_event=on_event,
                )

            # 산출물은 저장되는 대로 화면에 보냅니다. 아래 장부 갱신은 LLM 호출 한 번이라,
            # 그 뒤로 미루면 보고서 발언은 떴는데 산출물 탭만 그만큼 늦게 채워집니다.
            if on_event:
                await on_event({
                    "type": "artifacts_synthesized",
                    "artifacts": [a.model_dump() for a in state.artifacts],
                })

            # 이번 턴의 결론까지 장부에 접습니다. 다음 턴은 이 장부를 들고 시작합니다.
            await self._update_ledger(
                state=state, orchestrator=orchestrator_agent,
                on_event=on_event, reason="최종 합성",
            )

            # 합성이 시작된 뒤에 도착한 개입은 이번 턴에 실을 자리가 없습니다.
            # 그대로 버리면 화면은 "다음 발언 차례에 반영됩니다" 라고 알린 채 턴이
            # 끝나 버립니다. 기록에 남겨 두면 다음 턴이 맥락으로 읽어 갑니다.
            deferred = await self._apply_interjections(
                db=db, state=state, control=control,
                round_number=state.current_round + 1, on_event=on_event,
            )
            if deferred and on_event:
                await on_event({"type": "interjections_deferred", "count": deferred})

            # 장부와 요약은 턴이 끝까지 온 경우에만 저장합니다 (모듈 설명의 "저장").
            await self._persist_memory(db, state, on_event)

            state.status = "completed"
            # 사용자가 도중에 끊었다면 합의에 이른 것이 아닙니다.
            state.is_consensus_reached = (
                not state.failed_agent_keys and not state.stopped_early and not synthesis_failed
            )
            if state.failed_agent_keys:
                state.error_message = (
                    "다음 에이전트가 LLM 엔드포인트에 닿지 못했습니다: "
                    + ", ".join(state.failed_agent_keys)
                )
            elif synthesis_failed:
                state.error_message = (
                    "오케스트레이터의 최종 결론이 비어 있었습니다. 전문가별 마지막 발언을 "
                    "산출물로 남겼습니다."
                )

            if on_event:
                await on_event({
                    "type": "turn_completed",
                    "status": "completed",
                    "failed_agents": list(state.failed_agent_keys),
                    "error_message": state.error_message,
                    "stopped_early": state.stopped_early,
                    "rounds_completed": state.current_round,
                    "max_rounds": state.max_rounds,
                })

            return state

    # ------------------------------------------------------------ 발언자 선정

    async def _record_note(
        self,
        *,
        db,
        state: DebateState,
        on_event: Optional[EventCallback],
        agent: Agent,
        content: str,
        round_number: int,
        msg_type: str,
        graph_node_id: Optional[str] = None,
        graph_port: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> DebateMessage:
        """LLM 발언이 아닌 기록을 남깁니다 (지명 결과, 지명 실패 안내 등).

        `_speak` 과 같은 자리에 같은 모양으로 들어갑니다. 그래야 새로고침한 화면과
        저장 파일이 이것을 다른 발언과 똑같이 읽습니다.
        """
        msg_id = str(uuid.uuid4())
        # LLM 을 부르지 않는 기록이라 걸리는 시간이 없습니다 (시작 = 끝).
        now = utc_now()
        await self._persist(
            db,
            lambda: [MessageModel(
                id=msg_id,
                session_id=state.session_id,
                sender_key=agent.key,
                sender_name=agent.name,
                sender_role=agent.role,
                content=content,
                round_number=round_number,
                msg_type=msg_type,
                started_at=now,
                finished_at=now,
                graph_node_id=graph_node_id,
                graph_port=graph_port,
                **({"created_at": created_at} if created_at is not None else {}),
            )],
            what=f"a note from {agent.name}",
            label=f"{agent.name} 의 기록",
            kind=f"note-{agent.key}",
            session_id=state.session_id,
            fallback_title=f"{agent.name} ({agent.role}) — Round {round_number}",
            fallback_body=content,
            on_event=on_event,
        )

        message = DebateMessage(
            id=msg_id,
            sender_key=agent.key,
            sender_name=agent.name,
            sender_role=agent.role,
            content=content,
            round_number=round_number,
            msg_type=msg_type,
            started_at=now,
            finished_at=now,
            graph_node_id=graph_node_id,
            graph_port=graph_port,
        )
        state.messages.append(message)
        if on_event:
            await on_event({"type": "message_added", "message": message.model_dump()})
        return message

    async def _select_speakers(
        self,
        *,
        db,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        active_agents: List[Agent],
        round_num: int,
        custom_instructions: str,
        on_event: Optional[EventCallback],
    ) -> List[Agent]:
        """이번 라운드에 누가 발언할지 정합니다.

        보통은 전략이 결정적으로 정합니다. '오케스트레이터 지명' 전략일 때만
        오케스트레이터에게 물어, 지금 필요한 에이전트만 부릅니다.

        지명에 실패하면 (엔드포인트가 없거나, 응답에서 아는 키를 하나도 못 찾거나)
        전략의 결정적 순서로 물러섭니다. 물러섰다는 사실은 피드에 남깁니다 —
        조용히 다른 순서로 도는 것이 제일 나쁩니다.
        """
        fallback = strategy.get_speakers_for_round(active_agents, round_num, state)
        if not strategy.orchestrator_selects_speakers or len(fallback) <= 1:
            return fallback

        async def _fall_back(why: str) -> List[Agent]:
            await self._record_note(
                db=db, state=state, on_event=on_event, agent=orchestrator,
                round_number=round_num, msg_type="error",
                content=(
                    f"[발언자 지명 실패] {why}\n"
                    f"우선순위 순서로 진행합니다: {', '.join(a.name for a in fallback)}"
                ),
            )
            return fallback

        try:
            picked, reason = await self._ask_orchestrator_for_speakers(
                orchestrator=orchestrator,
                candidates=fallback,
                state=state,
                round_num=round_num,
                custom_instructions=custom_instructions,
            )
        except LLMUnavailableError as exc:
            logger.warning(f"Speaker selection failed; using the deterministic order: {exc}")
            return await _fall_back(str(exc))

        if not picked:
            logger.warning("Orchestrator named no known agent; using the deterministic order.")
            return await _fall_back(
                "오케스트레이터의 응답에서 이번 라운드에 부를 에이전트를 찾지 못했습니다."
            )

        # 누가 왜 불렸는지는 기록에 남아야 합니다. 부르지 않은 에이전트가 있다는
        # 사실도 토론 기록을 읽는 사람에게 보여야 합니다.
        skipped = [a.name for a in fallback if a not in picked]
        summary = f"[Round {round_num} 발언권] {' → '.join(a.name for a in picked)}"
        if skipped:
            summary += f"\n(이번 라운드 미지명: {', '.join(skipped)})"
        if reason:
            summary += f"\n\n{reason}"
        await self._record_note(
            db=db, state=state, on_event=on_event, agent=orchestrator,
            round_number=round_num, msg_type="orchestrator", content=summary,
        )
        return picked

    async def _ask_orchestrator_for_speakers(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        custom_instructions: str,
    ) -> "tuple[List[Agent], str]":
        """오케스트레이터에게 이번 라운드 발언자와 순서를 물어봅니다.

        도구와 단계적 사고를 **끈 사본**으로 부릅니다. 이건 JSON 한 줄을 받는
        라우팅 호출이지 발언이 아닙니다. 도구를 붙이면 지명하려다 파일을 읽기
        시작하고, 단계적 사고 프로토콜이 주입되면 `Thought 1..N` 을 쓰다가 형식을
        놓칩니다.
        """
        selector = self._tool_less(orchestrator)

        roster = format_roster(candidates, with_keys=True)
        recent = [
            f"{m.speaker}: {self._snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            + self._routing_record(state, selector) +
            f"[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[이번 라운드에 부를 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 입니다. 논의를 진전시키기 위해 "
            f"이번 라운드에 **꼭 필요한 에이전트만** 골라 발언 순서를 정하세요. 전원을 부를 "
            f"필요는 없고, 한 명만 불러도 됩니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            f'{{"speakers": ["에이전트키", ...], "reason": "한두 문장으로 지명 사유"}}'
        )}]

        content, _ = await self.llm_caller.call_agent(
            selector, prompt, custom_instructions, session_id=state.session_id,
            mcp=self._mcp_for(state), tool_gate=self._gate_for(state), ledger=state.decision_ledger,
        )
        return self._parse_speaker_selection(content, candidates)

    @staticmethod
    def _parse_speaker_selection(
        content: str, candidates: List[Agent]
    ) -> "tuple[List[Agent], str]":
        """응답에서 지명된 에이전트와 사유를 뽑습니다.

        JSON 이 온전하면 그것을 쓰고, 아니면 본문에서 아는 키를 **등장 순서대로**
        긁습니다. 모델이 설명을 곁들이거나 펜스를 두르는 일은 흔하고, 그때마다
        지명을 포기하면 이 전략은 결국 우선순위 순서와 같아집니다.
        """
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
            # 본문에서 키를 긁습니다. 등장 순서가 곧 발언 순서입니다.
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

    # ------------------------------------------------------- 병렬 지시 라운드

    async def _run_parallel_round(
        self,
        *,
        db,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        active_agents: List[Agent],
        round_num: int,
        custom_instructions: str,
        parallel_limit: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> bool:
        """한 라운드를 병렬로 돕니다. 정지 요청으로 라운드를 접었으면 True.

        순서: 개입 반영 → 과업 분배 → 동시 실행 → 취합. 사람의 개입과 정지를 보는
        지점이 라운드 경계뿐인 것은 이 전략의 성질입니다 — 다른 전략은 발언과 발언
        사이에서 볼 수 있지만, 여기서는 그 '사이' 에 전원이 이미 달리고 있습니다.
        """
        candidates = strategy.get_speakers_for_round(active_agents, round_num, state)
        if not candidates:
            return False

        # 분배 **전에** 개입을 반영합니다. 이 라운드의 과업을 정하는 근거가
        # 되어야지, 이미 나눠 준 뒤에 들어와서는 다음 라운드까지 놀게 됩니다.
        await self._apply_interjections(
            db=db, state=state, control=control, round_number=round_num, on_event=on_event
        )
        if control is not None and control.stop_requested:
            return True

        assignments = await self._dispatch_parallel_tasks(
            db=db, state=state, strategy=strategy, orchestrator=orchestrator,
            candidates=candidates, round_num=round_num,
            custom_instructions=custom_instructions,
            parallel_limit=parallel_limit, on_event=on_event,
        )
        if not assignments:
            return False

        board = self._assignment_board(assignments)
        # 프롬프트는 **전부 먼저** 만듭니다. 코루틴 안에서 만들면 먼저 끝난 동료의
        # 발언이 늦게 시작한 쪽의 맥락에 섞여 들어가, 같은 라운드인데 누구는 남의
        # 답을 보고 누구는 못 보는 상태가 됩니다. 그건 병렬이 아닙니다.
        prompts = [
            await self._context_for_speech(
                state, agent, self._parallel_turn_instruction(strategy, agent, task, board),
                orchestrator=orchestrator, on_event=on_event,
            )
            for agent, task in assignments
        ]

        # 기록 시각을 지시 순서로 박아 둡니다 (`_speak` 의 `created_at` 주석 참고).
        base_time = utc_now()
        db_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(parallel_limit)
        start_index = len(state.messages)

        async def run_one(index: int) -> DebateMessage:
            agent, _task = assignments[index]
            async with semaphore:
                return await self._speak(
                    db=db,
                    state=state,
                    agent=agent,
                    prompt_messages=prompts[index],
                    custom_instructions=custom_instructions,
                    round_number=round_num,
                    msg_type="agent",
                    on_event=on_event,
                    control=control,
                    db_lock=db_lock,
                    created_at=base_time + timedelta(milliseconds=index),
                )

        if on_event:
            await on_event({
                "type": "status_changed",
                "status": "debating",
                "speaker": " · ".join(a.name for a, _ in assignments),
                "round": round_num,
            })

        results = await asyncio.gather(
            *(run_one(i) for i in range(len(assignments))), return_exceptions=True
        )

        # 완료 순서가 아니라 지시 순서로 정렬합니다. 실시간 화면은 카드가 만들어진
        # 순서(= 지시 순서)로 보여주는데, 기록은 `created_at` 순으로 다시 읽히므로
        # 여기서 맞춰 두지 않으면 새로고침 후 순서가 달라 보입니다.
        spoken = [r for r in results if isinstance(r, DebateMessage)]
        produced = state.messages[start_index:]
        if {m.id for m in spoken} == {m.id for m in produced}:
            state.messages[start_index:] = spoken

        for (agent, _task), result in zip(assignments, results):
            if isinstance(result, BaseException):
                logger.error(
                    f"Parallel turn for '{agent.key}' failed: {type(result).__name__}: {result}",
                    exc_info=result,
                )
                if agent.key not in state.failed_agent_keys:
                    state.failed_agent_keys.append(agent.key)
                await self._record_note(
                    db=db, state=state, on_event=on_event, agent=agent,
                    round_number=round_num, msg_type="error",
                    content=(
                        f"> ⚠️ **{agent.name} 의 병렬 발언이 실패했습니다.**\n>\n"
                        f"> - 원인: `{type(result).__name__}: {result}`\n>\n"
                        f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다."
                    ),
                )

        if control is not None and control.stop_requested:
            # 정지를 원한 사람에게 취합 발언을 한 번 더 기다리게 할 이유가 없습니다.
            # 최종 합성이 곧바로 이어지고, 그것이 이 라운드의 결과도 함께 읽습니다.
            return True

        await self._merge_parallel_round(
            db=db, state=state, orchestrator=orchestrator, assignments=assignments,
            round_num=round_num, custom_instructions=custom_instructions,
            control=control, on_event=on_event,
        )
        return False

    async def _dispatch_parallel_tasks(
        self,
        *,
        db,
        state: DebateState,
        strategy: BaseDebateStrategy,
        orchestrator: Agent,
        candidates: List[Agent],
        round_num: int,
        custom_instructions: str,
        parallel_limit: int,
        on_event: Optional[EventCallback],
    ) -> List[Tuple[Agent, str]]:
        """이번 라운드의 과업 분배를 받아 기록하고 돌려줍니다.

        분배에 실패하면 전원을 과업 없이 돌리는 것으로 물러섭니다. 지시를 받지
        못했을 뿐 병렬이라는 성질은 남기고, 물러섰다는 사실은 피드에 남깁니다 —
        조용히 다른 방식으로 도는 것이 제일 나쁩니다.
        """
        fallback = [(agent, "") for agent in candidates]

        async def _fall_back(why: str) -> List[Tuple[Agent, str]]:
            await self._record_note(
                db=db, state=state, on_event=on_event, agent=orchestrator,
                round_number=round_num, msg_type="error",
                content=(
                    f"[과업 분배 실패] {why}\n"
                    f"과업 없이 전원을 동시에 진행합니다: "
                    f"{', '.join(a.name for a in candidates)}"
                ),
            )
            return fallback

        if len(candidates) <= 1:
            # 한 명뿐이면 나눌 것이 없습니다. 분배를 물어보는 호출만 낭비됩니다.
            assignments = fallback
            reason = ""
        else:
            try:
                assignments, reason = await self._ask_orchestrator_for_assignments(
                    orchestrator=orchestrator,
                    candidates=candidates,
                    state=state,
                    round_num=round_num,
                    parallel_limit=parallel_limit,
                    custom_instructions=custom_instructions,
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Task dispatch failed; running everyone without tasks: {exc}")
                return await _fall_back(str(exc))

            if not assignments:
                logger.warning("Orchestrator assigned nobody we know; running everyone without tasks.")
                return await _fall_back(
                    "오케스트레이터의 응답에서 과업을 맡길 에이전트를 찾지 못했습니다."
                )

        named = [agent for agent, _ in assignments]
        lines = [
            f"- **{agent.name}** ({agent.role}): {task or '(과업 지정 없음 — 전문 영역에서 자유 기여)'}"
            for agent, task in assignments
        ]
        over_limit = len(assignments) > parallel_limit
        summary = (
            f"[Round {round_num} 병렬 지시] {len(assignments)}명에게 과업을 나눴습니다"
            + (f" (동시 실행 상한 {parallel_limit} — 나머지는 순차적으로 밀립니다)" if over_limit else " (동시 실행)")
            + "\n" + "\n".join(lines)
        )
        skipped = [a.name for a in candidates if a not in named]
        if skipped:
            summary += f"\n\n(이번 라운드 미지명: {', '.join(skipped)})"
        if reason:
            summary += f"\n\n{reason}"
        await self._record_note(
            db=db, state=state, on_event=on_event, agent=orchestrator,
            round_number=round_num, msg_type="orchestrator", content=summary,
        )
        return assignments

    async def _ask_orchestrator_for_assignments(
        self,
        *,
        orchestrator: Agent,
        candidates: List[Agent],
        state: DebateState,
        round_num: int,
        parallel_limit: int,
        custom_instructions: str,
    ) -> "Tuple[List[Tuple[Agent, str]], str]":
        """오케스트레이터에게 이번 라운드의 과업 분배를 물어봅니다.

        발언자 지명(`_ask_orchestrator_for_speakers`)과 같은 이유로 도구와 단계적
        사고를 끈 사본으로 부릅니다. 이건 JSON 을 받는 호출이지 발언이 아닙니다.
        """
        planner = self._tool_less(orchestrator)

        roster = format_roster(candidates, with_keys=True)
        recent = [
            f"{m.speaker}: {self._snippet(m, 300)}"
            for m in state.messages if m.msg_type != "error"
        ][-8:]

        prompt = [{"role": "user", "content": (
            f"[목표]\n{state.user_prompt}\n\n"
            + self._routing_record(state, planner) +
            f"[지금까지의 토론]\n" + ("\n".join(recent) or "(아직 없음)") + "\n\n"
            f"[과업을 맡길 수 있는 에이전트]\n{roster}\n\n"
            f"지금은 Round {round_num}/{state.max_rounds} 이고, 지목된 에이전트는 "
            f"**동시에 각자의 과업을 수행합니다**. 서로의 이번 라운드 결과를 볼 수 없으므로 "
            f"과업이 겹치면 같은 일을 두 번 하게 됩니다.\n\n"
            f"겹치지 않게 과업을 나누세요. 전원을 부를 필요는 없고, 한 명만 불러도 됩니다. "
            f"각 과업은 다른 에이전트의 결과를 기다리지 않고 혼자 끝낼 수 있는 것이어야 하며, "
            f"무엇을 만들어 낼지(산출물)까지 한두 문장으로 적으세요. "
            f"동시 실행은 {parallel_limit}명까지이고 그보다 많이 부르면 나머지는 순차적으로 밀립니다.\n\n"
            f"다음 JSON 형식으로만 답하세요:\n"
            '{"assignments": [{"agent": "에이전트키", "task": "이 라운드에 맡길 구체적 과업"}], '
            '"reason": "한두 문장으로 분배 사유"}'
        )}]

        content, _ = await self.llm_caller.call_agent(
            planner, prompt, custom_instructions, session_id=state.session_id,
            mcp=self._mcp_for(state), tool_gate=self._gate_for(state), ledger=state.decision_ledger,
        )
        return self._parse_assignments(content, candidates)

    @staticmethod
    def _parse_assignments(
        content: str, candidates: List[Agent]
    ) -> "Tuple[List[Tuple[Agent, str]], str]":
        """응답에서 (에이전트, 과업) 목록과 분배 사유를 뽑습니다.

        과업 문장을 잃더라도 누구를 부를지는 건집니다. `assignments` 가 깨졌으면
        발언자 지명과 같은 방식으로 아는 키를 등장 순서대로 긁고, 과업은 빈
        문자열이 됩니다 — 지시 없는 병렬 라운드가 라운드를 통째로 날리는 것보다
        낫습니다.
        """
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
            # 분배가 깨졌습니다. 최소한 누구를 부르려 했는지는 살립니다.
            picked, scraped = OrchestratorEngine._parse_speaker_selection(content, candidates)
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

    @staticmethod
    def _assignment_board(assignments: List[Tuple[Agent, str]]) -> str:
        """동시에 도는 동료들이 무엇을 맡았는지 적은 판.

        결과는 못 보여 주지만 **누가 무엇을 하는지**는 알려 줄 수 있습니다. 이것이
        없으면 여럿이 같은 표를 각자 그려 오고, 취합이 중복 제거부터 시작합니다.
        """
        return "\n".join(
            f"- {agent.name}({agent.role}): {task or '(과업 지정 없음)'}"
            for agent, task in assignments
        )

    @staticmethod
    def _parallel_turn_instruction(
        strategy: BaseDebateStrategy, agent: Agent, task: str, board: str
    ) -> str:
        """병렬 라운드에서 한 에이전트에게 붙는 지침 = 내 과업 + 동시 실행 현황."""
        if task:
            head = f"[병렬 지시] 오케스트레이터가 이번 라운드에 당신에게 맡긴 과업입니다:\n{task}"
        else:
            # 분배가 실패한 라운드. 전략이 들고 있는 문구를 그대로 씁니다.
            head = strategy.turn_instruction(
                agent, [agent], 0, DebateState(session_id="", user_prompt="")
            )

        return (
            f"{head}\n\n"
            f"[동시 진행 중]\n{board}\n\n"
            f"이들은 지금 당신과 **같은 시각에** 답하고 있어 이번 라운드 결과를 볼 수 없습니다. "
            f"남의 과업을 대신 하지 말고 당신 몫을 끝까지 마치세요. 다른 과업의 결과가 필요하면 "
            f"추측해 채우지 말고 어떤 가정을 두었는지 명시하세요. 라운드 끝에 오케스트레이터가 "
            f"결과를 합칩니다."
        )

    async def _merge_parallel_round(
        self,
        *,
        db,
        state: DebateState,
        orchestrator: Agent,
        assignments: List[Tuple[Agent, str]],
        round_num: int,
        custom_instructions: str,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> DebateMessage:
        """라운드 끝의 취합 발언. 병렬 결과를 붙이고 충돌과 남은 쟁점을 정리합니다.

        이 발언이 다음 라운드 분배의 입력이 됩니다. 없으면 서로를 못 본 독백들이
        그대로 최종 합성까지 실려 가고, 모순은 거기서 처음 발견됩니다.
        """
        state.current_speaker = orchestrator.name
        if on_event:
            await on_event({
                "type": "status_changed",
                "status": "debating",
                "speaker": f"{orchestrator.name} (취합)",
                "round": round_num,
            })

        board = self._assignment_board(assignments)
        instruction = (
            f"[Round {round_num} 취합] 방금 다음 에이전트가 **동시에** 각자의 과업을 수행했습니다:\n"
            f"{board}\n\n"
            f"이들은 서로의 결과를 보지 못한 채 답했습니다. 수석 오케스트레이터로서 "
            f"이번 라운드의 결과를 하나로 붙이세요:\n"
            f"1. 통합된 현재 결론 (무엇이 정해졌는가)\n"
            f"2. 서로 어긋나는 지점과 그 판정 (누구 말이 맞는지, 아직 판단할 수 없다면 그 이유)\n"
            f"3. 각자가 세운 가정 중 아직 검증되지 않은 것\n"
            f"4. 다음 라운드로 넘길 미해결 과제\n\n"
            f"발언하지 못했거나 실패한 에이전트의 몫을 지어내지 마세요."
        )

        return await self._speak(
            db=db,
            state=state,
            agent=orchestrator,
            prompt_messages=await self._context_for_speech(
                state, orchestrator, instruction, orchestrator=orchestrator, on_event=on_event,
            ),
            custom_instructions=custom_instructions,
            round_number=round_num,
            msg_type="orchestrator",
            on_event=on_event,
            control=control,
        )

    # ------------------------------------------------------------ 그래프 토론
    #
    # 무엇을 왜 이렇게 돌리는지는 `app/orchestration/graph.py` 에 있습니다. 여기는 LLM 을
    # 부르는 쪽 — 노드마다 맥락을 만들고, 발언시키고, 판정을 받고, 결과를 선으로 보냅니다.

    async def _graph_for_turn(
        self, db, session_model: SessionModel, max_rounds: int
    ) -> GraphSpec:
        """이 턴에 쓸 그래프를 읽고 검사하고, 굳혀 둡니다. 쓸 수 없으면 `GraphTurnError`."""
        from app.agents.personas import frozen_agents
        from app.config import get_config
        from app.graph_store import load_graph

        graph_id = (session_model.graph_id or "").strip()
        if not graph_id:
            raise GraphTurnError(
                "그래프 토론에 쓸 그래프가 없습니다. 로스터의 그래프 선택에서 고르거나 "
                "'카드 순서로 만들기' 로 만드세요."
            )
        try:
            spec = load_graph(graph_id)
        except FileNotFoundError:
            raise GraphTurnError(f"그래프 파일을 찾을 수 없습니다: data/graphs/{graph_id}.json")
        except ValueError as exc:
            raise GraphTurnError(f"그래프 {graph_id} 를 읽지 못했습니다: {exc}")

        # 있는 에이전트는 이 엔진의 풀과, 이미 시작한 대화가 굳혀 둔 스냅샷입니다(conf.json 에서
        # 사라졌어도 그 대화에서는 발언합니다). 풀에는 켜진 에이전트만 있어 "꺼짐" 과 "없음" 을
        # 가릴 수 없으므로, conf.json 에서 꺼진 것만 따로 표시해 오류 문구를 정확히 합니다.
        known: Dict[str, bool] = {}
        try:
            for key, cfg in get_config().agents.items():
                if not getattr(cfg, "enabled", True):
                    known[key] = False
        except Exception:  # noqa: BLE001 - 설정을 못 읽어도 풀만으로 검사합니다
            logger.debug("Could not read conf.json agents for graph validation", exc_info=True)
        for agent in self.agent_pool.list_all():
            known[agent.key] = True
        for agent in await frozen_agents(db, session_model.id, self.agent_pool):
            known[agent.key] = True
        report = validate_graph(spec, known, default_max_visits=max_rounds)
        if not report.ok:
            raise GraphTurnError(
                f"그래프 “{spec.name or spec.id}” 에 오류가 있어 시작하지 않습니다: "
                + " · ".join(report.errors[:3])
                + (f" (외 {len(report.errors) - 3}건)" if len(report.errors) > 3 else "")
            )

        try:
            await db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_model.id)
                .values(graph_snapshot=spec.dump(), updated_at=SessionModel.updated_at)
            )
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 스냅샷을 못 적어도 이번 턴은 이 그래프로 돕니다
            logger.warning(f"Could not store the graph snapshot for {session_model.id}: {exc}")
            await db.rollback()
        return spec

    async def _run_graph(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        orchestrator: Agent,
        active_agents: List[Agent],
        parallel_limit: int,
        max_visits: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
        start_value: List[str],
    ) -> bool:
        """그래프를 단계별로 돕니다. 사용자가 정지시켰으면 True.

        끝나는 경우는 넷입니다: 최종 합성 노드에 닿음 · 더 돌 노드가 없음 · 단계 상한 · 정지.
        정지가 아닌 셋은 모두 합성으로 넘어가고, 앞의 것이 아니면 왜 멈췄는지 기록에 남깁니다.
        """
        agents = {a.key: a for a in active_agents}
        scheduler = GraphScheduler(spec, max_visits)
        max_steps = scheduler.max_steps()
        announced_exhausted = 0

        if on_event:
            await on_event({
                "type": "graph_started",
                "graph_id": spec.id,
                "name": spec.name or spec.id,
                "nodes": [{"id": n.id, "type": n.type, "label": n.display} for n in spec.nodes],
                # 화면이 이번 턴에 실제로 도는 그림을 그립니다 (파일은 턴 도중에 바뀔 수 있습니다).
                "spec": spec.dump(),
                "max_steps": max_steps,
            })
        scheduler.deliver(spec.start.id, "out", start_value)

        step = 0
        while True:
            await self._apply_interjections(
                db=db, state=state, control=control, round_number=step, on_event=on_event
            )
            if control is not None and control.stop_requested:
                await self._graph_finished(on_event, "stopped", step)
                return True

            ready = scheduler.ready()
            for node_id in scheduler.exhausted[announced_exhausted:]:
                node = spec.node(node_id)
                await self._record_note(
                    db=db, state=state, on_event=on_event, agent=orchestrator,
                    round_number=step, msg_type="orchestrator", graph_node_id=node_id,
                    content=(
                        f"[그래프] “{node.display}” 은(는) 이번 턴에 최대 "
                        f"{scheduler.visits[node_id]}회까지 불려 더 부르지 않습니다."
                    ),
                )
            announced_exhausted = len(scheduler.exhausted)

            if any(n.type == "end" for n in ready):
                await self._graph_finished(on_event, "end", step)
                return False
            if not ready:
                waiting = [spec.node(n).display for n in scheduler.pending()]
                await self._record_note(
                    db=db, state=state, on_event=on_event, agent=orchestrator,
                    round_number=step, msg_type="orchestrator",
                    content=(
                        "[그래프] 최종 합성 노드에 닿기 전에 더 진행할 노드가 없어, 지금까지의 "
                        "발언으로 합성합니다."
                        + (
                            f" 모든 입력을 기다리다 멈춘 노드: {', '.join('“' + w + '”' for w in waiting)}."
                            if waiting else ""
                        )
                    ),
                )
                await self._graph_finished(on_event, "idle", step)
                return False
            if step >= max_steps:
                await self._record_note(
                    db=db, state=state, on_event=on_event, agent=orchestrator,
                    round_number=step, msg_type="orchestrator",
                    content=f"[그래프] 단계 상한({max_steps})에 닿아 여기서 멈추고 합성합니다.",
                )
                await self._graph_finished(on_event, "step_cap", step)
                return False

            step += 1
            state.current_round = step
            activations = [scheduler.activate(node) for node in ready]
            if on_event:
                await on_event({"type": "round_started", "round": step, "max_rounds": max_steps})
                await on_event({
                    "type": "graph_step_started",
                    "step": step,
                    "max_steps": max_steps,
                    "nodes": [{"id": a.node.id, "label": a.node.display, "visit": a.visit} for a in activations],
                })
                await on_event({
                    "type": "status_changed",
                    "status": "debating",
                    "speaker": " · ".join(a.node.display for a in activations),
                    "round": step,
                })

            outputs = await self._run_graph_step(
                db=db, state=state, spec=spec, scheduler=scheduler, activations=activations,
                agents=agents, orchestrator=orchestrator, step=step,
                parallel_limit=parallel_limit, control=control, on_event=on_event,
            )
            for activation in activations:
                port, value = outputs[activation.node.id]
                scheduler.deliver(activation.node.id, port, value)

            # 곧바로 합성으로 가는 단계 뒤에는 장부를 건너뜁니다 (합성 뒤 갱신이 대신합니다).
            heading_to_end = any(
                scheduler.fresh[n.id] for n in spec.nodes if n.type == "end"
            )
            if not heading_to_end and not (control is not None and control.stop_requested):
                await self._update_ledger(
                    state=state, orchestrator=orchestrator, on_event=on_event, reason=f"Step {step}",
                )

    @staticmethod
    async def _graph_finished(on_event: Optional[EventCallback], reason: str, steps: int) -> None:
        if on_event:
            await on_event({"type": "graph_finished", "reason": reason, "steps": steps})

    async def _run_graph_step(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        activations: List[Activation],
        agents: Dict[str, Agent],
        orchestrator: Agent,
        step: int,
        parallel_limit: int,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Dict[str, Tuple[str, List[str]]]:
        """한 단계의 노드들을 동시에 돌리고, 노드마다 (출력 핀, 값) 을 돌려줍니다.

        병렬 지시 라운드와 같은 규칙입니다 — 프롬프트는 전부 먼저 만들고(같은 단계의 결과가
        섞이지 않게), 기록 구간만 잠그고, 기록 시각은 노드 순서로 박습니다.
        """
        prompts: Dict[str, List[Dict[str, Any]]] = {}
        for activation in activations:
            node = activation.node
            if node.type == "gate":
                continue
            speaker = orchestrator if node.type == "merge" else agents[node.agent]
            prompts[node.id] = await self._context_for_node(
                state=state, spec=spec, scheduler=scheduler, activation=activation,
                speaker=speaker, step=step, orchestrator=orchestrator, on_event=on_event,
            )

        base_time = utc_now()
        db_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(max(1, parallel_limit))
        start_index = len(state.messages)

        async def run_one(index: int, activation: Activation) -> Tuple[str, List[str]]:
            node = activation.node
            created_at = base_time + timedelta(milliseconds=index)
            async with semaphore:
                if node.type == "gate":
                    return await self._run_gate(
                        db=db, state=state, spec=spec, activation=activation,
                        orchestrator=orchestrator, step=step, on_event=on_event,
                        db_lock=db_lock, created_at=created_at,
                    )
                speaker = orchestrator if node.type == "merge" else agents[node.agent]
                message = await self._speak(
                    db=db,
                    state=state,
                    agent=speaker,
                    prompt_messages=prompts[node.id],
                    custom_instructions=state.custom_instructions,
                    round_number=step,
                    msg_type="orchestrator" if node.type == "merge" else "agent",
                    on_event=on_event,
                    control=control,
                    db_lock=db_lock,
                    created_at=created_at,
                    graph_node_id=node.id,
                    graph_port="out",
                )
                return "out", [message.id]

        results = await asyncio.gather(
            *(run_one(i, a) for i, a in enumerate(activations)), return_exceptions=True
        )

        # 완료 순서가 아니라 노드 순서로 (`_run_parallel_round` 와 같은 이유).
        order = {a.node.id: i for i, a in enumerate(activations)}
        produced = state.messages[start_index:]
        if all(m.graph_node_id in order for m in produced):
            state.messages[start_index:] = sorted(produced, key=lambda m: order[m.graph_node_id])

        outputs: Dict[str, Tuple[str, List[str]]] = {}
        for activation, result in zip(activations, results):
            node = activation.node
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.error(
                    f"Graph node '{node.id}' failed: {type(result).__name__}: {result}",
                    exc_info=result,
                )
                note = await self._record_note(
                    db=db, state=state, on_event=on_event, agent=orchestrator,
                    round_number=step, msg_type="error", graph_node_id=node.id,
                    graph_port=node.default if node.type == "gate" else "out",
                    content=(
                        f"> ⚠️ **그래프 노드 “{node.display}” 가 실패했습니다.**\n>\n"
                        f"> - 원인: `{type(result).__name__}: {result}`\n>\n"
                        f"> 이 자리에 들어갈 내용을 대신 지어내지 않았습니다."
                    ),
                )
                port = node.default if node.type == "gate" else "out"
                outputs[node.id] = (port, [note.id])
            else:
                outputs[node.id] = result
        return outputs

    def _render_graph_input(
        self,
        state: DebateState,
        message: DebateMessage,
        carry: str,
        placeholders: Dict[int, str],
        index_of: Dict[str, int],
    ) -> str:
        """선 하나가 싣고 온 발언 하나. 선 종류대로 전문 / 요지 / 참조."""
        if message.msg_type == "error":
            return f"[{message.sender_name}]: (이 발언은 실패해 내용이 없습니다)"
        pinned = placeholders.get(index_of.get(message.id, -1))
        label = "[User]" if message.sender_key == "user" else f"[{message.speaker}]"
        if pinned:
            return f"{label}:\n{pinned}"
        body = strip_reasoning_trace(message.content)
        if carry == "digest" and message.sender_key != "user":
            digest = memory.extract_digest(message.content)
            if digest:
                return f"{label} {memory.DIGEST_HEADING} (전문 {len(body):,}자 중 요지만):\n{digest}"
            return f"{label}:\n{memory.reference_code_blocks(body, message.tool_calls)}"
        if carry == "refs":
            return f"{label}:\n{memory.reference_code_blocks(body, message.tool_calls)}"
        return f"{label}:\n{body}"

    async def _context_for_node(
        self,
        *,
        state: DebateState,
        spec: GraphSpec,
        scheduler: GraphScheduler,
        activation: Activation,
        speaker: Agent,
        step: int,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """노드 하나의 프롬프트. 기본은 **들어온 선만** — 전사 전체 대신.

        고정 맥락(요청 · 사용자 발언 기록 · 이번 턴 계획)은 모든 노드에 들어가고, 결정 장부는
        호출기가 마지막 메시지 앞에 붙입니다. 루프로 다시 불린 노드는 자기 직전 발언을 함께 봅니다.
        "전체 기록 보기" 를 켠 에이전트 노드는 기존 발언 맥락(세 층)을 받습니다.
        """
        node = activation.node
        instruction = self._graph_turn_instruction(spec, activation, step)
        if node.type == "agent" and node.sees == "all":
            return await self._context_for_speech(
                state, speaker, instruction, orchestrator=orchestrator, on_event=on_event,
            )

        budget = self._speech_budget(speaker, state)
        record = memory.build_user_record(
            state, model=speaker.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        plan_pin = memory.build_plan_pin(
            state, model=speaker.model, token_cap=int(budget * memory.PLAN_PIN_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, bool(plan_pin))
        index_of = {m.id: i for i, m in enumerate(state.messages)}
        by_id = {m.id: m for m in state.messages}

        head = [f"[User Goal / Current Request]:\n{state.user_prompt}"]
        head += [part for part in (record.text, plan_pin) if part]
        context: List[Dict[str, Any]] = [{"role": "user", "content": "\n\n".join(head)}]

        previous = [
            m for m in state.messages[state.turn_message_start:]
            if m.graph_node_id == node.id and m.msg_type != "error" and m.sender_key == speaker.key
        ]
        if previous:
            last = previous[-1]
            context.append({
                "role": "assistant",
                "content": (
                    f"[{speaker.name} ({speaker.role}) · 이 노드의 직전 발언]:\n"
                    f"{memory.reference_code_blocks(strip_reasoning_trace(last.content), last.tool_calls)}"
                ),
            })

        back = scheduler.back
        for edge, ids in activation.inputs:
            source = spec.node(edge.source[0])
            parts = [
                self._render_graph_input(state, by_id[mid], edge.carry, placeholders, index_of)
                for mid in ids if mid in by_id and not (previous and mid == previous[-1].id)
            ]
            if not parts:
                continue
            branch = {"yes": " 예 갈래", "no": " 아니오 갈래"}.get(edge.source[1], "")
            header = (
                f"[입력 · “{source.display if source else edge.source[0]}”{branch}"
                f" → {CARRY_LABELS.get(edge.carry, edge.carry)}"
                f"{' · 되돌림' if edge.id in back else ''}]"
            )
            context.append({"role": "user", "content": header + "\n" + "\n\n".join(parts)})

        context.append({"role": "user", "content": instruction})
        return context

    @staticmethod
    def _graph_turn_instruction(spec: GraphSpec, activation: Activation, step: int) -> str:
        node = activation.node
        visit = f" ({activation.visit}회차)" if activation.visit > 1 else ""
        lines = [f"[Graph Step]: {step}단계 · 노드 “{node.display}”{visit}"]
        if node.type == "merge":
            lines.append(
                "[취합] 위 [입력]으로 들어온 발언들을 하나로 붙이세요:\n"
                "1. 통합된 현재 결론 (무엇이 정해졌는가)\n"
                "2. 서로 어긋나는 지점과 그 판정\n"
                "3. 아직 검증되지 않은 가정\n"
                "4. 다음으로 넘길 미해결 과제\n"
                "발언하지 못했거나 실패한 쪽의 몫을 지어내지 마세요."
            )
        else:
            lines.append(
                "[그래프 토론] 당신은 그래프의 한 노드입니다. 위 [입력]으로 들어온 발언을 받아 이 노드의 "
                "몫을 하세요. 입력에 없는 다른 에이전트의 결론을 추측해 채우지 말고, 필요하면 가정으로 "
                "명시하세요."
            )
        if activation.visit > 1:
            lines.append(
                "이 노드가 다시 불렸습니다. 되돌아온 이유(판정·취합 의견)를 먼저 처리하고, 직전 발언에서 "
                "바뀐 점을 분명히 하세요."
            )
        if node.instruction.strip():
            lines.append(f"[이 노드의 지시]\n{node.instruction.strip()}")
        lines.append(memory.DIGEST_INSTRUCTION)
        return "\n\n".join(lines)

    async def _run_gate(
        self,
        *,
        db,
        state: DebateState,
        spec: GraphSpec,
        activation: Activation,
        orchestrator: Agent,
        step: int,
        on_event: Optional[EventCallback],
        db_lock: asyncio.Lock,
        created_at: datetime,
    ) -> Tuple[str, List[str]]:
        """판정 노드. 오케스트레이터(도구 없는 사본)에게 예/아니오를 받아 한쪽 갈래로 보냅니다.

        내보내는 값은 **판정 기록 + 판정한 입력**입니다. 되돌려 받은 노드는 무엇 때문에 되돌아왔는지와
        무엇을 고쳐야 하는지를 함께 봐야 합니다. 응답을 읽지 못하면 노드에 정한 기본 갈래로 가고,
        그 사실을 기록에 남깁니다 — 조용히 한쪽으로 흐르는 것이 제일 나쁩니다.
        """
        node = activation.node
        judge = self._tool_less(orchestrator)
        by_id = {m.id: m for m in state.messages}
        index_of = {m.id: i for i, m in enumerate(state.messages)}
        record = memory.build_user_record(
            state, model=judge.model, token_cap=int(context_budget(judge) * memory.ROUTING_RECORD_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, False)
        seen: List[str] = []
        blocks: List[str] = []
        for edge, ids in activation.inputs:
            for mid in ids:
                if mid in by_id and mid not in seen:
                    seen.append(mid)
                    blocks.append(self._render_graph_input(state, by_id[mid], edge.carry, placeholders, index_of))

        default_label = "예" if node.default == "yes" else "아니오"
        prompt = [{"role": "user", "content": (
            f"[판정] 그래프 토론의 판정 노드 “{node.display}” 입니다.\n\n"
            f"[목표]\n{state.user_prompt}\n\n"
            + (f"{record.text}\n\n" if record.text else "")
            + "[판정할 내용]\n" + ("\n\n".join(blocks) or "(들어온 발언이 없습니다)") + "\n\n"
            f"[질문]\n{node.question.strip()}\n\n"
            f"위 내용만 근거로 질문에 예/아니오로 답하세요. 근거가 부족해 판단할 수 없으면 "
            f"“{default_label}” 로 답하고 그 이유를 적으세요.\n\n"
            '다음 JSON 한 줄로만 답하세요:\n{"decision": "yes" 또는 "no", "reason": "한두 문장"}'
        )}]

        decision, reason, understood = node.default, "", False
        try:
            content, _ = await self.llm_caller.call_agent(
                judge, prompt, state.custom_instructions, session_id=state.session_id,
                mcp=self._mcp_for(state), tool_gate=self._gate_for(state), ledger=state.decision_ledger,
            )
            parsed = parse_gate_decision(content)
            if parsed is not None:
                decision, reason = parsed
                understood = True
            else:
                reason = "판정 응답을 읽지 못했습니다"
        except LLMUnavailableError as exc:
            reason = f"판정 호출에 실패했습니다 ({exc.reason})"

        verdict = "예" if decision == "yes" else "아니오"
        content_text = f"[판정 · {node.display}] **{verdict}** — {reason or '(사유 없음)'}"
        if not understood:
            content_text += f"\n(기본 갈래 “{default_label}” 로 진행합니다)"
        async with db_lock:
            note = await self._record_note(
                db=db, state=state, on_event=on_event, agent=orchestrator,
                round_number=step, msg_type="orchestrator", graph_node_id=node.id, graph_port=decision,
                content=content_text, created_at=created_at,
            )
        if on_event:
            await on_event({
                "type": "graph_gate_decided",
                "node_id": node.id,
                "label": node.display,
                "decision": decision,
                "reason": reason,
                "fallback": not understood,
            })
        return decision, [note.id] + seen

    # ------------------------------------------------------------ 대화 기억
    #
    # 컨텍스트 창이 차도 잃으면 안 되는 것 — 사용자 발언, 이번 턴 계획, 결정 장부,
    # 앞선 논의의 요약. 무엇을 왜 지키는지는 `app/orchestration/context_memory.py` 에 있습니다.

    @staticmethod
    def _tool_less(agent: Agent) -> Agent:
        """도구와 단계적 사고를 끈 사본.

        JSON 한 줄(발언자 지명·과업 분배)이나 정해진 형식의 글(장부·요약)을 받는 호출에
        씁니다. 도구를 붙이면 파일을 읽기 시작하고, 단계적 사고 프로토콜이 주입되면
        `Thought 1..N` 을 쓰다가 형식을 놓칩니다.
        """
        return agent.model_copy(update={
            "allowed_mcp_servers": [],
            "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
        })

    def _speech_budget(self, agent: Agent, state: DebateState) -> int:
        """이 발언자의 요청에서 시스템 프롬프트와 대화가 쓸 수 있는 토큰."""
        return context_budget(agent, tools=self._tools_for(agent, state))

    @staticmethod
    def _system_tokens(agent: Agent, state: DebateState) -> int:
        """시스템 프롬프트 몫의 어림값. 페르소나·세션 지침·장부에 고정 지침 몫을 더합니다."""
        text = "\n\n".join(
            part for part in (agent.system_prompt, state.custom_instructions, state.decision_ledger)
            if part
        )
        return memory.text_tokens(agent.model, text) + 512

    @staticmethod
    def _summary_cap(state: DebateState, budget: int) -> int:
        """요약의 글자 상한. 참여자 중 가장 작은 예산을 알면 그것에 맞춥니다."""
        return memory.memory_cap(
            memory.SUMMARY_SHARE, memory.SUMMARY_MAX_CHARS, state.memory_budget or budget
        )

    @staticmethod
    def _snippet(msg: DebateMessage, limit: int) -> str:
        """짧게 옮길 때의 한 조각. 요지가 있으면 요지, 없으면 앞부분.

        앞 N자만 자르면 긴 발언은 서론만 남습니다. 요지는 발언자가 결론을 추려 둔 것입니다.
        자르기 전에 사고 과정을 뗍니다 — 그러지 않으면 N자가 "Thought 1: ..." 로 채워집니다.
        """
        digest = memory.extract_digest(msg.content) if msg.sender_key != "user" else None
        text = digest or strip_reasoning_trace(msg.content)
        return text[:limit]

    def _routing_record(self, state: DebateState, agent: Agent) -> str:
        """발언자 지명·과업 분배 프롬프트에 넣을 사용자 발언 기록 (짧은 몫)."""
        record = memory.build_user_record(
            state, model=agent.model,
            token_cap=int(context_budget(agent) * memory.ROUTING_RECORD_SHARE),
        ).text
        return f"{record}\n\n" if record else ""

    def _load_memory(self, state: DebateState, session_model: SessionModel) -> None:
        """저장된 장부·요약을 이번 턴 상태로 옮깁니다."""
        ids = [m.id for m in state.messages]
        state.decision_ledger = session_model.decision_ledger or ""
        through = memory.through_index(ids, session_model.ledger_through_id)
        # 반영 지점이 기록에서 사라졌으면 지금까지를 반영된 것으로 봅니다. 옛 기록 전체를
        # 장부 갱신 한 번에 다시 밀어 넣지 않습니다.
        state.ledger_through = len(ids) if through is None else through

        summary = session_model.transcript_summary or ""
        summary_through = memory.through_index(ids, session_model.summary_through_id)
        # 요약은 덮는 자리를 모르면 쓸 수 없습니다 (원문과 겹치거나 빠집니다). 버리고,
        # 필요하면 다시 접습니다.
        if summary.strip() and summary_through:
            state.transcript_summary = summary
            state.summary_through = summary_through

    async def _persist_memory(
        self, db, state: DebateState, on_event: Optional[EventCallback]
    ) -> None:
        """장부와 요약을 저장합니다. 턴이 끝까지 왔을 때만 부릅니다.

        긴급 종료(`session_ops.discard_turn`)는 턴 도중의 취소라 여기까지 오지 않습니다.
        그래서 지워진 발언을 반영한 장부가 남지 않습니다.
        """
        values = {
            "decision_ledger": state.decision_ledger,
            "ledger_through_id": memory.through_id(state.messages, state.ledger_through),
            "transcript_summary": state.transcript_summary if state.summary_through else "",
            "summary_through_id": memory.through_id(state.messages, state.summary_through),
            # 기억을 적었다고 대화 목록에서 "방금 바뀐 대화" 로 올라가지 않게 합니다.
            "updated_at": SessionModel.updated_at,
        }
        try:
            await db.execute(
                update(SessionModel).where(SessionModel.id == state.session_id).values(**values)
            )
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 기억을 못 적어도 턴은 끝납니다
            logger.error(
                f"Could not persist the decision ledger for session {state.session_id}: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("Rollback after a failed ledger commit also failed", exc_info=True)
            if on_event:
                await on_event({
                    "type": "persist_failed",
                    "what": "the decision ledger",
                    "label": "결정 장부",
                    "error": f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                    "saved_to": None,
                })

    async def _context_for_speech(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str,
        *,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """발언 맥락. 창을 넘으면 먼저 앞선 기록을 요약으로 접은 뒤 만듭니다."""
        prompt = self._build_context_for_agent(state, agent, turn_instruction)
        total = self._system_tokens(agent, state) + estimate_tokens(agent.model, prompt)
        folded = await self._ensure_room(
            state, model=agent.model, total=total, budget=self._speech_budget(agent, state),
            orchestrator=orchestrator, on_event=on_event, agent_name=agent.name,
        )
        if folded:
            prompt = self._build_context_for_agent(state, agent, turn_instruction)
        return prompt

    async def _ensure_room(
        self,
        state: DebateState,
        *,
        model: str,
        total: int,
        budget: int,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        agent_name: str,
    ) -> bool:
        """`total` 이 `budget` 을 넘으면 앞선 기록을 요약으로 접습니다. 접었으면 True.

        접지 못해도 괜찮습니다 — `fit_context_window` 가 예전처럼 오래된 것부터 생략합니다.
        """
        if budget <= 0 or total <= budget:
            return False
        start = state.summary_through
        costs = [
            0 if m.msg_type == "error" else memory.text_tokens(model, memory.render_message(m))
            for m in state.messages[start:]
        ]
        cut = memory.choose_fold_cut(
            state,
            message_tokens=costs,
            total_tokens=total,
            budget=budget,
            summary_allowance=(
                self._summary_cap(state, budget) - memory.text_tokens(model, state.transcript_summary)
            ),
        )
        if cut <= start:
            return False
        return await self._fold_into_summary(
            state, orchestrator=orchestrator, cut=cut, on_event=on_event, agent_name=agent_name,
            max_chars=self._summary_cap(state, budget),
        )

    async def _fold_into_summary(
        self,
        state: DebateState,
        *,
        orchestrator: Agent,
        cut: int,
        on_event: Optional[EventCallback],
        agent_name: str,
        max_chars: int = memory.SUMMARY_MAX_CHARS,
    ) -> bool:
        """`messages[summary_through:cut]` 를 누적 요약에 접습니다. 조금이라도 접었으면 True.

        묶음마다 상태에 반영합니다. 뒤 묶음이 실패해도 앞에서 접은 것은 남습니다.
        """
        writer = self._tool_less(orchestrator)
        start = state.summary_through
        items = [
            (index, state.messages[index]) for index in range(start, cut)
            if state.messages[index].msg_type != "error"
        ]
        if not items:
            return False
        room = (
            context_budget(writer)
            - memory.PROMPT_OVERHEAD_TOKENS
            - memory.text_tokens(writer.model, state.transcript_summary)
        )
        if room <= 0:
            logger.warning(
                f"No room to summarize earlier messages with {writer.name} "
                f"(max_context_window={writer.max_context_window}); older ones will be dropped instead"
            )
            return False
        batches = memory.batch_blocks(
            writer.model, [memory.render_message(m) for _index, m in items], room
        )[: memory.SUMMARY_MAX_BATCHES]

        if on_event:
            await on_event({
                "type": "context_summarizing",
                "agent_name": agent_name,
                "messages": cut - start,
            })

        consumed = 0
        try:
            for batch in batches:
                content, _ = await self.llm_caller.call_agent(
                    writer,
                    memory.summary_prompt(
                        previous=state.transcript_summary, blocks=batch,
                        covered=state.summary_through, max_chars=max_chars,
                    ),
                    state.custom_instructions,
                    session_id=state.session_id,
                    mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
                )
                summary = memory.parse_summary(content, max_chars)
                if not summary:
                    raise ValueError("요약 응답이 비어 있습니다")
                consumed += len(batch)
                state.transcript_summary = summary
                state.summary_through = cut if consumed == len(items) else items[consumed - 1][0] + 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 요약에 실패하면 예전처럼 생략합니다
            logger.warning(
                f"Could not summarize earlier messages for {agent_name}; older ones will be "
                f"dropped instead: {type(exc).__name__}: {exc}"
            )
            if on_event:
                await on_event({
                    "type": "context_summary_failed",
                    "agent_name": agent_name,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        folded = state.summary_through - start
        if folded and on_event:
            await on_event({
                "type": "context_summarized",
                "agent_name": agent_name,
                "folded": folded,
                "total": state.summary_through,
            })
        return folded > 0

    async def _update_ledger(
        self,
        *,
        state: DebateState,
        orchestrator: Agent,
        on_event: Optional[EventCallback],
        reason: str,
    ) -> bool:
        """결정 장부를 갱신합니다. 갱신했으면 True.

        새 발언이 없거나 실패하면 이전 장부를 그대로 둡니다. 장부는 토론의 부산물이라,
        장부를 못 써서 토론이 멈추면 안 됩니다.
        """
        start, upto = state.ledger_through, len(state.messages)
        fresh = [
            (index, state.messages[index]) for index in range(start, upto)
            if state.messages[index].msg_type != "error"
        ]
        if not any(m.sender_key != "user" for _index, m in fresh):
            return False

        writer = self._tool_less(orchestrator)
        budget = context_budget(writer)
        ledger_chars = memory.memory_cap(
            memory.LEDGER_SHARE, memory.LEDGER_MAX_CHARS, state.memory_budget or budget
        )
        record = memory.build_user_record(
            state, model=writer.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        # 고정된 사용자 발언은 프롬프트에 기록으로 따로 들어가므로 참조로만 둡니다.
        placeholders = {i: memory.user_placeholder(n) for i, n in record.refs.items()}
        opening = memory.opening_index(state)
        if opening is not None:
            placeholders[opening] = "(이번 턴 요청 — 위 [이번 턴 요청]에 전문이 있습니다)"

        room = (
            budget
            - memory.PROMPT_OVERHEAD_TOKENS
            - memory.text_tokens(writer.model, state.decision_ledger)
            - memory.text_tokens(writer.model, record.text)
            - memory.text_tokens(writer.model, state.user_prompt)
        )
        if room <= 0:
            logger.warning(
                f"No room to update the decision ledger with {writer.name} "
                f"(max_context_window={writer.max_context_window})"
            )
            return False

        # 최근 것부터 채웁니다. 못 실은 앞쪽은 이전 장부에 반영돼 있는 경우가 대부분입니다.
        kept: List[str] = []
        used = 0
        for index, msg in reversed(fresh):
            block = memory.render_message(msg, placeholders.get(index))
            cost = memory.text_tokens(writer.model, block)
            if kept and used + cost > room:
                break
            if cost > room:
                block = memory.clip_to_tokens(writer.model, block, room)
                cost = room
            kept.insert(0, block)
            used += cost

        if on_event:
            await on_event({"type": "ledger_update_started", "reason": reason})
        try:
            content, _ = await self.llm_caller.call_agent(
                writer,
                memory.ledger_prompt(
                    previous=state.decision_ledger,
                    user_record=record.text,
                    blocks=kept,
                    skipped=len(fresh) - len(kept),
                    user_prompt=state.user_prompt,
                    max_chars=ledger_chars,
                ),
                state.custom_instructions,
                session_id=state.session_id,
                mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
            )
            ledger = memory.parse_ledger(content, ledger_chars)
            if ledger is None:
                raise ValueError("응답에 장부 형식(`## ` 제목)이 없습니다")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 장부를 못 써도 토론은 계속됩니다
            logger.warning(
                f"Could not update the decision ledger ({reason}) for session {state.session_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            if on_event:
                await on_event({
                    "type": "ledger_update_failed",
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            return False

        state.decision_ledger = ledger
        state.ledger_through = upto
        if on_event:
            await on_event({"type": "ledger_updated", "reason": reason, "ledger": ledger})
        return True

    def _build_context_for_agent(
        self,
        state: DebateState,
        agent: Agent,
        turn_instruction: str = "",
        *,
        use_summary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Prepares discussion transcript for agent turn.

        `turn_instruction` 은 전략이 이 차례에 붙이는 지침입니다. 전략이 순서만
        정하던 시절에는 '자유 토론' 과 '순차 검증' 이 똑같은 프롬프트를 받아,
        발언 순서 말고는 다를 것이 없었습니다. 두 전략의 실제 차이가 여기서
        갈립니다.

        첫 메시지(목표)에는 잘리면 안 되는 것을 고정합니다 — 사용자 발언 기록, 이번 턴
        계획, 앞선 논의 요약. 두 자르기 모두 이 자리를 남깁니다. 기록 안의 같은 발언은
        참조로 바꿔 토큰을 두 번 쓰지 않습니다.

        요약은 원문 전체가 이 발언자의 창에 들어가지 않을 때만 씁니다 (`use_summary`
        를 주지 않으면 여기서 판단). 창이 큰 에이전트는 원문을 그대로 읽습니다.
        """
        budget = self._speech_budget(agent, state)
        record = memory.build_user_record(
            state, model=agent.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        plan_pin = memory.build_plan_pin(
            state, model=agent.model, token_cap=int(budget * memory.PLAN_PIN_SHARE)
        )
        placeholders = memory.placeholders_for(state, record, bool(plan_pin))
        summary = memory.summary_block(state)

        # 라운드 표시는 맨 끝에 둡니다. 목표 메시지에 있으면 라운드마다 두 번째 메시지가 바뀌어,
        # 그 뒤의 기록 전체가 프롬프트 캐시에서 빠집니다 (모듈 설명의 "프롬프트 캐싱").
        turn_prompt = (
            f"[Debate Progress]: Round {state.current_round} of {state.max_rounds}.\n\n"
            f"이제 {agent.name}({agent.role})님의 차례입니다. 앞선 전체 논의 맥락과 직전 "
            f"발언들을 충실히 반영하여 전문적인 의견을 발언하고 필요시 도구를 활용해 주세요."
        )
        if turn_instruction:
            turn_prompt += f"\n\n{turn_instruction}"
        turn_prompt += f"\n\n{memory.DIGEST_INSTRUCTION}"

        # 이 발언자가 마지막으로 말한 뒤에 나온 발언은 원문으로 (모듈 설명의 "발언을 얼마나
        # 넘길까"). 이번 턴에 아직 말하지 않았으면 이번 턴 전체가 새 발언입니다.
        fresh_start = state.turn_message_start
        for index in range(len(state.messages) - 1, -1, -1):
            msg = state.messages[index]
            if msg.sender_key == agent.key and msg.msg_type != "error":
                fresh_start = max(fresh_start, index + 1)
                break
        mentions = {mention_token(agent.name), f"@{agent.key}"}

        def body_for(index: int, msg: DebateMessage) -> str:
            pinned = placeholders.get(index)
            if pinned:
                return pinned
            body = strip_reasoning_trace(msg.content)
            if index >= fresh_start and msg.sender_key != agent.key:
                return body
            if (
                msg.sender_key != agent.key
                and len(body) > memory.DIGEST_MIN_BODY_CHARS
                and not any(token in body for token in mentions)
            ):
                digest = memory.extract_digest(msg.content)
                if digest:
                    return f"{memory.DIGEST_HEADING} (전문 {len(body):,}자 중 요지만 싣습니다)\n{digest}"
            return memory.reference_code_blocks(body, msg.tool_calls)

        def assemble(start: int, with_summary: bool) -> List[Dict[str, Any]]:
            head = [f"[User Goal / Current Request]:\n{state.user_prompt}"]
            head += [part for part in (record.text, plan_pin, summary if with_summary else "") if part]
            context: List[Dict[str, Any]] = [{"role": "user", "content": "\n\n".join(head)}]

            for index in range(start, len(state.messages)):
                msg = state.messages[index]
                # 응답을 못 받은 자리는 맥락에 넣지 않습니다. 실패 안내문을 발언인 양
                # 읽히게 하면 다음 에이전트가 그것을 논평하기 시작합니다.
                if msg.msg_type == "error":
                    continue
                if msg.sender_key == "user":
                    context.append({
                        "role": "user",
                        "content": f"[User]:\n{placeholders.get(index, msg.content)}"
                    })
                else:
                    role_label = f"[{msg.speaker}]"
                    context.append({
                        "role": "assistant" if msg.sender_key == agent.key else "user",
                        # 사고 과정은 기록과 화면에만 남기고 프롬프트에는 싣지 않습니다.
                        # 자기 발언도 같습니다 — `show_steps` 는 사람이 무엇을 볼지를
                        # 정하는 스위치이지, 모델이 무엇을 읽을지를 정하는 것이 아닙니다.
                        "content": f"{role_label}:\n{body_for(index, msg)}",
                    })

            context.append({"role": "user", "content": turn_prompt})
            return context

        full = assemble(0, False)
        if not summary:
            return full
        if use_summary is None:
            use_summary = (
                budget > 0
                and self._system_tokens(agent, state) + estimate_tokens(agent.model, full) > budget
            )
        return assemble(state.summary_through, True) if use_summary else full

    async def _synthesis_prompt_with_notice(
        self,
        state: DebateState,
        agent: Optional[Agent],
        on_event: Optional[EventCallback],
    ) -> List[Dict[str, Any]]:
        """합성 프롬프트를 만들고, 전사가 잘렸으면 화면에 알립니다.

        최종 보고서가 초반 논의를 못 보고 쓰였다는 사실은 사람이 알아야 합니다 —
        예전에는 `logger.warning` 에만 남았습니다.
        """
        if agent is not None:
            await self._ensure_synthesis_room(state, agent, on_event)
        before = state.context_dropped
        prompt = self._build_synthesis_prompt(state, agent)
        dropped = state.context_dropped - before
        if dropped and on_event:
            await on_event({
                "type": "context_trimmed",
                "agent_key": agent.key if agent else "orchestrator",
                "agent_name": agent.name if agent else "Orchestrator",
                "dropped": dropped,
                "total_dropped": state.context_dropped,
                "where": "synthesis",
            })
        return prompt

    async def _ensure_synthesis_room(
        self, state: DebateState, agent: Agent, on_event: Optional[EventCallback]
    ) -> None:
        """합성 전사가 창을 넘으면 앞선 기록을 요약으로 접습니다 (`_ensure_room`)."""
        budget = context_budget(agent, tools=self._tools_for(agent, state)) - 512
        record = memory.build_user_record(
            state, model=agent.model, token_cap=int(budget * memory.USER_RECORD_SHARE)
        )
        summary = memory.summary_block(state)
        start = state.summary_through if summary else 0
        total = (
            memory.text_tokens(agent.model, record.text)
            + memory.text_tokens(agent.model, summary)
            + sum(
                memory.text_tokens(agent.model, memory.render_message(m))
                for m in state.messages[start:] if m.msg_type != "error"
            )
        )
        await self._ensure_room(
            state, model=agent.model, total=total, budget=budget,
            orchestrator=agent, on_event=on_event, agent_name=agent.name,
        )

    def _memory_search_tool_for(
        self, agent: Optional[Agent], state: Optional[DebateState] = None,
    ) -> Optional[str]:
        """이 에이전트가 실제로 쓸 수 있는 메모리 검색 도구의 이름. 없으면 None.

        서버 키가 아니라 **주어진 도구 목록**에서 찾습니다. 서버를 껐거나 연결에
        실패했으면 없는 도구를 가리키게 되기 때문입니다. 그 목록은 이 턴이 빌린
        런타임에서 나옵니다 — 대화마다 작업 공간이 다르면 뜬 서버도 다릅니다.
        """
        if agent is None:
            return None
        return memory_search_tool(self._tools_for(agent, state))

    def _tools_for(
        self, agent: Optional[Agent], state: Optional[DebateState] = None,
    ) -> List[Dict[str, Any]]:
        """이 에이전트의 요청에 실릴 도구 정의. 못 구하면 빈 목록.

        도구 정의는 요청마다 함께 나가는 입력이라, 전사의 크기를 정할 때도 그 몫을 빼야
        합니다 (`context_budget`). 대역 호출기처럼 도구를 모르는 경우에도 합성은 진행합니다.
        """
        if agent is None:
            return []
        try:
            mcp = (self._mcp_for(state) if state else None) or self.llm_caller.mcp_manager
            tools = mcp.get_openai_tools_for_servers(
                self.llm_caller.resolve_tool_servers(agent)
            ) or []
            # 발언이 실제로 들고 나가는 목록과 같아야 합니다 (보안상 빠지는 도구 제외).
            gate = self._gate_for(state) if state else None
            return gate.filter_tools(agent.key, tools, mcp) if gate is not None else tools
        except Exception:  # noqa: BLE001 - 도구 목록을 못 구해도 합성은 진행합니다
            return []

    def _build_synthesis_prompt(
        self, state: DebateState, agent: Optional[Agent] = None
    ) -> List[Dict[str, Any]]:
        """Constructs prompt for orchestrator final consensus & artifact generation.

        전사(transcript)를 오케스트레이터의 컨텍스트 한도에 맞춰 자릅니다.
        여기는 한 개의 user 메시지 안에 토론 전체가 통째로 들어가는 자리라,
        `fit_context_window()` 가 손댈 수 있는 것이 없습니다 (메시지 단위로
        덜어내는데 덜어낼 메시지가 없습니다). 라운드가 몇 번만 돌아도 한도를
        넘어 400 이 나므로, 만드는 쪽에서 크기를 정해야 합니다.

        최근 발언부터 채웁니다. 뒤로 갈수록 앞선 논의가 반영된 결론이라,
        잘라야 한다면 앞쪽을 버리는 편이 낫습니다.

        오케스트레이터에게는 **결론과 종합 다이어그램만** 시킵니다. 예전에는 "완전한 실행
        가능 소스 코드" 까지 요구해, 전문가들이 이미 쓴 코드를 합성에서 통째로 다시
        출력했습니다. 응답 한도를 금방 채웠고, 그 합성 발언이 다음 턴 전사에 코드 덤프로
        다시 들어가 컨텍스트가 턴마다 빠르게 포화됐습니다 — 결국 합성이 비어 돌아오는
        원인이 됐습니다. 코드 산출물은 전문가 발언에서 따로 모읍니다.
        """
        # 사용자 발언은 전사 앞에 고정합니다. 최신부터 채우는 전사에서 초반 요구사항이
        # 빠지면, 보고서가 그 요구사항을 모른 채 결론을 씁니다.
        model = agent.model if agent is not None else "gpt-4o"
        budget = (
            context_budget(agent, tools=self._tools_for(agent, state)) - 512
            if agent is not None else 0
        )
        record = memory.build_user_record(
            state, model=model,
            token_cap=int(budget * memory.USER_RECORD_SHARE) if agent is not None else 10 ** 9,
        )
        placeholders = memory.placeholders_for(state, record, plan_pinned=False)

        def render(index: int, msg: DebateMessage) -> str:
            prefix = "### [User]" if msg.sender_key == "user" else f"### {msg.speaker}"
            body = msg.content if msg.sender_key == "user" else strip_reasoning_trace(msg.content)
            return f"{prefix}:\n{placeholders.get(index) or body}\n"

        # 원문이 창에 들어가지 않고 요약이 있으면, 요약이 덮는 앞쪽은 요약으로 읽습니다.
        summary = memory.summary_block(state) if agent is not None else ""
        start = 0
        if summary:
            full_cost = sum(
                memory.text_tokens(model, render(i, m))
                for i, m in enumerate(state.messages) if m.msg_type != "error"
            )
            if full_cost + memory.text_tokens(model, record.text) > budget:
                start = state.summary_through
            else:
                summary = ""
        usable = [
            (i, m) for i, m in enumerate(state.messages)
            if i >= start and m.msg_type != "error"
        ]

        if agent is None:
            kept, dropped = [render(i, m) for i, m in usable], 0
        else:
            # 응답 분량(사고 예산이 더해진 실제 값), 요청마다 실리는 도구 정의, 지시문 몫을
            # 빼고 남는 것이 전사의 예산입니다. 예전에는 설정값 `max_tokens` 만 뺐습니다 —
            # 도구 정의 수천 토큰과 `native` 모드의 사고 예산이 빠져, 합성 요청이 창을 넘겼습니다.
            # 앞에 고정한 사용자 발언 기록과 요약의 몫도 뺍니다.
            budget -= memory.text_tokens(model, record.text) + memory.text_tokens(model, summary)
            kept_rev: List[str] = []
            dropped = 0
            for index, msg in reversed(usable):
                block = render(index, msg)
                probe = [{"role": "user", "content": "\n".join([block] + kept_rev)}]
                if kept_rev and budget > 0 and estimate_tokens(agent.model, probe) > budget:
                    dropped = len(usable) - len(kept_rev)
                    break
                kept_rev.insert(0, block)
            kept = kept_rev

        if dropped:
            logger.warning(
                f"Synthesis transcript trimmed: dropped {dropped} of {len(usable)} message(s) "
                f"(max_context_window={agent.max_context_window})"
            )
            # 생략 건수를 상태에 누적합니다. 반환값을 바꾸는 대신 이 자리를 쓰는
            # 것은, 이 메서드가 프롬프트 문자열을 돌려준다는 계약을 호출부와
            # 테스트가 이미 쓰고 있기 때문입니다.
            state.context_dropped += dropped
            memory_tool = self._memory_search_tool_for(agent, state)
            notice = context_trim_notice(dropped, memory_tool)
            if memory_tool:
                notice += (
                    f"\n[생략된 초반 라운드가 결론에 필요하면 `{memory_tool}` 로 "
                    f"이 대화의 그래프를 찾아보세요 — 토론 중에 에이전트들이 "
                    f"핵심을 그곳에 남겼을 수 있습니다.]"
                )
            kept.insert(0, notice + "\n")

        full_transcript = "\n".join(kept)

        early_stop = ""
        if state.stopped_early:
            # 남은 라운드에서 나왔을 반론을 지어내면, 검증되지 않은 결론이 검증된
            # 것처럼 보고서에 올라갑니다.
            early_stop = (
                f"\n[주의] 유저가 예정된 라운드보다 일찍 토론을 정지시켰습니다 "
                f"(진행: {state.current_round}/{state.max_rounds} 라운드). 남은 라운드에서 "
                f"나왔을 의견을 추측해 채우지 말고, 지금까지 오간 논의만으로 정리하되 "
                f"아직 검토되지 못한 쟁점을 보고서에 명시하세요.\n"
            )

        missing = ""
        if state.failed_agent_keys:
            # 누가 빠졌는지 알려야, 오케스트레이터가 없는 의견을 있는 것처럼 요약하지 않습니다.
            missing = (
                f"\n[주의] 다음 에이전트는 LLM 연결 실패로 이번 토론에서 발언하지 못했습니다: "
                f"{', '.join(state.failed_agent_keys)}. 이들의 의견을 추측해서 채우지 말고, "
                f"보고서에 누락 사실을 명시하세요.\n"
            )

        pinned = "".join(f"{part}\n\n" for part in (record.text, summary) if part)
        prompt = (
            f"[User Goal]: {state.user_prompt}\n\n"
            f"{pinned}"
            f"[Full Multi-Agent Debate Transcript]:\n{full_transcript}\n"
            f"{early_stop}"
            f"{missing}\n"
            f"수석 오케스트레이터로서 토론을 종합해 최종 합의 보고서를 작성하세요. "
            f"이 보고서가 맡는 것은 **결론과 종합 다이어그램까지**입니다.\n"
            f"다음 두 가지만 쓰세요:\n"
            f"1. **최종 합의 결론** — 결정 사항과 그 근거, 채택하지 않은 대안과 이유, "
            f"남은 쟁점과 위험\n"
            f"2. **종합 Mermaid 다이어그램** 하나 (```mermaid 블록. 노드 라벨에 괄호를 쓸 때는 "
            f'A["결제 서비스 (Payment)"] 처럼 반드시 큰따옴표로 감쌀 것)\n\n'
            f"[하지 말 것] 소스 코드를 다시 쓰거나 붙여 넣지 마세요. 코드는 전문가 발언과 작업 "
            f"공간 파일에 이미 있고, 산출물 탭에도 전문가 발언에서 따로 모읍니다. 코드가 필요한 "
            f"자리는 파일 경로·모듈·함수 이름으로만 가리키세요."
        )
        return [{"role": "user", "content": prompt}]

    # ------------------------------------------------- 다이어그램 문법 되돌리기

    async def _repair_mermaid_blocks(
        self,
        text: str,
        *,
        agent: Agent,
        custom_instructions: str,
        state: DebateState,
        on_event: Optional[EventCallback],
    ) -> str:
        """합성 본문의 Mermaid 다이어그램을 검사하고, 틀렸으면 고쳐 받습니다.

        예전에는 문법이 어긋난 다이어그램이 그대로 아티팩트가 되었고, 사람이 탭을
        열었을 때 비로소 "Mermaid 문법 오류" 를 보았습니다. 그때는 토론이 이미
        끝나 고칠 사람이 없습니다 — 다시 물어보려면 새 턴을 돌려야 하고, 그러면
        라운드를 통째로 다시 씁니다.

        여기서는 **쓴 사람에게 그 자리에서 돌려줍니다.** 오류 메시지와 문제가 된
        줄을 그대로 주고 고쳐 달라고 하며, `MERMAID_REPAIR_ATTEMPTS` 번까지
        반복합니다. 끝내 못 고치면 원문을 그대로 둡니다 — 지어낸 다이어그램으로
        바꾸느니, 틀린 채로 두고 무엇이 틀렸는지 알리는 편이 낫습니다.

        검사는 `lint_mermaid` 가 합니다. 확실한 오류만 잡도록 만들어져 있어,
        멀쩡한 그림을 두고 모델을 다시 부르는 일은 없습니다.
        """
        blocks = find_mermaid_blocks(text)
        if not blocks:
            return text

        broken = [
            (i, b, issues)
            for i, b in enumerate(blocks)
            if (issues := lint_mermaid(normalize_mermaid(b["code"])))
        ]
        if not broken:
            return text

        logger.warning(
            f"{len(broken)} of {len(blocks)} mermaid diagram(s) in {agent.name}'s synthesis "
            f"failed the syntax check: "
            + "; ".join(f"#{i + 1} {[x.rule for x in issues]}" for i, _b, issues in broken)
        )

        fixer = agent.model_copy(update={
            "allowed_mcp_servers": [],
            "sequential_thinking": agent.sequential_thinking.model_copy(update={"enabled": False}),
        })

        current = text
        for attempt in range(1, MERMAID_REPAIR_ATTEMPTS + 1):
            if on_event:
                await on_event({
                    "type": "mermaid_repair_started",
                    "agent_name": agent.name,
                    "broken": len(broken),
                    "total": len(blocks),
                    "attempt": attempt,
                    "max_attempts": MERMAID_REPAIR_ATTEMPTS,
                })

            try:
                fixed = await self._ask_for_fixed_diagrams(
                    fixer, broken, state, custom_instructions, attempt
                )
            except LLMUnavailableError as exc:
                logger.warning(f"Mermaid repair call failed: {exc}")
                break

            if not fixed:
                logger.warning("The repair answer contained no mermaid block; giving up on this pass.")
                break

            current = self._splice_blocks(current, broken, fixed)

            blocks = find_mermaid_blocks(current)
            broken = [
                (i, b, issues)
                for i, b in enumerate(blocks)
                if (issues := lint_mermaid(normalize_mermaid(b["code"])))
            ]
            if not broken:
                logger.info(f"Mermaid diagrams fixed on attempt {attempt}.")
                if on_event:
                    await on_event({
                        "type": "mermaid_repair_finished",
                        "agent_name": agent.name,
                        "resolved": True,
                        "attempts": attempt,
                        "remaining": 0,
                    })
                return current

        if on_event:
            await on_event({
                "type": "mermaid_repair_finished",
                "agent_name": agent.name,
                "resolved": False,
                "attempts": MERMAID_REPAIR_ATTEMPTS,
                "remaining": len(broken),
            })
        logger.warning(
            f"{len(broken)} mermaid diagram(s) still fail the syntax check after "
            f"{MERMAID_REPAIR_ATTEMPTS} attempt(s); keeping the original."
        )
        return current

    async def _ask_for_fixed_diagrams(
        self,
        fixer: Agent,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        state: DebateState,
        custom_instructions: str,
        attempt: int,
    ) -> List[str]:
        """틀린 다이어그램만 모아 고쳐 달라고 하고, 받은 블록을 순서대로 돌려줍니다.

        보고서 전체를 다시 쓰게 하지 않습니다. 다시 쓰면 결론이 흔들리고 길이도
        감당할 수 없습니다 — 고쳐야 하는 것은 문법이지 내용이 아닙니다.
        """
        parts = [
            "방금 작성한 보고서의 Mermaid 다이어그램이 문법 오류로 렌더링되지 않습니다.",
            "",
            f"아래 {len(broken)}개를 고쳐서 다시 주세요. **다이어그램만** 주면 됩니다 — "
            "보고서 본문은 다시 쓰지 마세요.",
            "",
        ]
        for order, (_index, block, issues) in enumerate(broken, start=1):
            parts += [
                f"### 다이어그램 {order}",
                "",
                "렌더러가 지적한 곳:",
                format_issues(issues),
                "",
                "원본:",
                "```mermaid",
                block["code"],
                "```",
                "",
            ]
        parts += [
            "---",
            "",
            f"고친 다이어그램 {len(broken)}개를 **같은 순서로**, 각각 ```mermaid 코드 블록"
            " 하나씩으로 출력하세요. 다른 설명은 붙이지 마세요.",
            "",
            "지킬 것:",
            "- 라벨에 괄호·따옴표·특수문자가 있으면 큰따옴표로 감싸세요: `A[\"결제 (PG)\"]`",
            "- `end` 는 예약어입니다. 노드 이름으로 쓰지 마세요 (`END` 는 됩니다).",
            "- `subgraph` 는 반드시 `end` 로 닫으세요.",
            "- 첫 줄은 `graph TD` 처럼 다이어그램 종류 선언이어야 합니다.",
            "- **의미는 바꾸지 마세요.** 노드와 관계는 그대로 두고 문법만 고치세요.",
        ]
        if attempt > 1:
            parts.append(
                f"- 이번이 {attempt}번째 시도입니다. 앞서 고친 것도 같은 이유로 실패했으니, "
                f"의심스러운 라벨은 전부 큰따옴표로 감싸세요."
            )

        content, _ = await self.llm_caller.call_agent(
            fixer, [{"role": "user", "content": "\n".join(parts)}],
            custom_instructions, session_id=state.session_id,
            mcp=self._mcp_for(state), tool_gate=self._gate_for(state),
        )
        return [b["code"] for b in find_mermaid_blocks(content or "")]

    @staticmethod
    def _splice_blocks(
        text: str,
        broken: List[Tuple[int, Dict[str, Any], List[Any]]],
        replacements: List[str],
    ) -> str:
        """고친 다이어그램을 원문의 제자리에 끼워 넣습니다.

        뒤에서부터 바꿉니다. 앞에서부터 바꾸면 길이가 달라지면서 뒤쪽 블록의
        위치가 어긋납니다.

        받은 개수가 모자라면 받은 만큼만 바꿉니다 — 모델이 다이어그램 하나를
        빠뜨렸다고 나머지 고친 것까지 버릴 이유는 없습니다.
        """
        pairs = list(zip(broken, replacements))
        for (_index, block, _issues), fixed in sorted(pairs, key=lambda x: -x[0][1]["start"]):
            text = text[:block["start"]] + fixed + text[block["end"]:]
        return text

    def _turn_speeches(self, state: DebateState) -> List[DebateMessage]:
        """이번 턴 전문가 발언. 사용자·오케스트레이터(계획·지명·합성)·실패 안내는 뺍니다."""
        return [
            m for m in state.messages[state.turn_message_start:]
            if m.sender_key not in ("user", "orchestrator") and m.msg_type != "error"
        ]

    def _debate_code_artifacts(self, state: DebateState, stamp: str) -> List[ArtifactItem]:
        """이번 턴 전문가 발언에서 코드를 모읍니다. 에이전트마다 **가장 최근에** 코드를 낸 발언.

        오케스트레이터가 합성에서 코드를 다시 쓰지 않으므로, 코드 탭은 여기서 채웁니다.
        이전 턴의 코드는 그 턴의 산출물로 이미 남아 있으니 다시 올리지 않습니다. 사고 과정에
        적은 초안이 섞이지 않게 결론만 봅니다. 같은 코드는 한 번만 올립니다.
        """
        latest: Dict[str, Tuple[DebateMessage, List[Dict[str, str]]]] = {}
        for msg in self._turn_speeches(state):
            blocks = [
                b for b in extract_code_blocks(strip_reasoning_trace(msg.content))
                if b["language"] in CODE_LANGUAGES
            ]
            if blocks:
                latest[msg.sender_key] = (msg, blocks)

        artifacts: List[ArtifactItem] = []
        seen = set()
        for msg, blocks in latest.values():
            for block in blocks:
                key = block["code"].strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                lang = "python" if block["language"] == "py" else block["language"]
                artifacts.append(ArtifactItem(
                    artifact_type="code",
                    title=f"{stamp} {lang} · {msg.sender_name} R{msg.round_number}".strip(),
                    content=block["code"],
                    language=lang,
                ))
                if len(artifacts) >= MAX_DEBATE_CODE_ARTIFACTS:
                    return artifacts
        return artifacts

    def _synthesis_fallback_report(
        self, state: DebateState, failure_reason: str, synth_text: str
    ) -> str:
        """합성이 비었거나 실패했을 때 남기는 보고서 — LLM 없이 모은 전문가별 마지막 발언.

        토론은 끝났는데 결론만 없는 상태입니다. 예전에는 이 턴의 산출물이 빈 보고서였고,
        화면이 그것으로 뷰어를 바꿔 **토론 결과가 증발한 것처럼** 보였습니다. 발언 자체는
        기록에 있지만, 결론을 찾는 사람은 산출물 탭을 봅니다.
        """
        latest: Dict[str, DebateMessage] = {}
        for msg in self._turn_speeches(state):
            latest[msg.sender_key] = msg

        what = (
            "만들지 못했습니다 (LLM 연결 끊김)" if failure_reason == "LLM 연결 끊김"
            else f"비워 두었습니다 ({failure_reason})"
        )
        parts = [
            f"> ⚠️ **오케스트레이터가 이번 턴의 최종 결론을 {what}.**\n"
            f">\n"
            f"> 토론 기록은 그대로 남아 있습니다. 아래는 LLM 없이 모은 **전문가별 마지막 "
            f"발언**입니다. 이전 턴의 결론은 산출물 탭에 그대로 있습니다. 결론이 필요하면 "
            f"같은 대화에서 \"결론만 다시 정리해 줘\" 처럼 요청하세요."
        ]
        notice = (synth_text or "").strip()
        if notice and failure_reason == "LLM 연결 끊김":
            parts.append(notice)
        if latest:
            parts.append("## 전문가별 마지막 발언")
            for msg in latest.values():
                body = strip_reasoning_trace(msg.content).strip() or "(내용 없음)"
                parts.append(
                    f"### {msg.speaker} — Round {msg.round_number}\n\n{body}"
                )
        else:
            parts.append("_이번 턴에는 기록된 전문가 발언이 없습니다._")
        return "\n\n".join(parts) + "\n"

    def _extract_artifacts_from_synthesis(
        self,
        session_id: str,
        synth_text: str,
        state: DebateState,
        synthesis_failed: bool = False,
        completed_at: Optional[datetime] = None,
        turn_started_at: Optional[datetime] = None,
        failure_reason: str = "LLM 연결 끊김",
    ) -> List[ArtifactItem]:
        """이 턴의 산출물: 최종 결론, 종합 다이어그램, 전문가 코드, 요약 JSON.

        제목 앞에 턴이 끝난 시각(`MM-DD HH:MM`)을 붙입니다. 산출물은 턴마다 쌓이고 화면은
        지난 턴 것을 지우지 않으므로, 같은 제목의 탭이 여러 개일 때 어느 턴의 것인지 보여야
        합니다.

        `completed_at` 이 주어지면 보고서 끝에 완료 시각과 총 경과를 적습니다. 합성이
        실패했으면 적지 않습니다 — 실패 안내를 "보고서 완료" 라고 부르면 거짓입니다.
        """
        stamp = to_local(completed_at).strftime("%m-%d %H:%M") if completed_at else ""
        artifacts: List[ArtifactItem] = []

        # 1. 최종 결론 — 실패했으면 전문가별 마지막 발언으로 대신합니다.
        if synthesis_failed:
            artifacts.append(ArtifactItem(
                artifact_type="markdown",
                title=f"{stamp} 합성 실패 ({failure_reason or 'LLM 연결 끊김'})".strip(),
                content=self._synthesis_fallback_report(state, failure_reason, synth_text),
                language="markdown",
            ))
        else:
            completed_line = report_completed_line(completed_at, turn_started_at)
            report = (
                f"{synth_text.rstrip()}\n\n---\n\n{completed_line}\n" if completed_line else synth_text
            )
            artifacts.append(ArtifactItem(
                artifact_type="markdown",
                title=f"{stamp} 최종 결론".strip(),
                content=report,
                language="markdown",
            ))

        # 2. 종합 다이어그램 — 합성 원문에서. 코드는 뽑지 않습니다 (오케스트레이터가 쓰지
        #    않도록 했고, 그래도 썼다면 보고서 본문에 남아 있습니다).
        def diagram_item(title: str, raw_code: str, source: str) -> ArtifactItem:
            # 기계적 수선(`normalize_mermaid`) 뒤에도 검사에 걸리면 제목 앞에 ⚠ 를 붙입니다.
            # 합성 다이어그램은 이미 수선을 거쳤고, 전문가 발언에서 가져온 것은 수선할
            # 사람이 없어 여기서 처음 검사합니다. 탭을 열기 전에 알 수 있어야 합니다.
            code = normalize_mermaid(raw_code)
            issues = lint_mermaid(code)
            if issues:
                logger.warning(
                    f"Diagram '{title}' from {source} still fails the syntax check: "
                    f"{[i.rule for i in issues]}"
                )
                title = f"⚠ {title}"
            return ArtifactItem(artifact_type="mermaid", title=title, content=code, language="mermaid")

        mermaid_idx = 1
        if not synthesis_failed:
            for block in extract_code_blocks(synth_text):
                if block["language"] != "mermaid":
                    continue
                artifacts.append(diagram_item(
                    f"{stamp} 종합 다이어그램" + (f" #{mermaid_idx}" if mermaid_idx > 1 else ""),
                    block["code"], "the synthesis",
                ))
                mermaid_idx += 1

        # 2-b. 합성에 다이어그램이 없으면 이번 턴 토론 본문에서 찾습니다. 합성이 실패한
        #      턴에도 다이어그램 탭이 비지 않게 합니다. 이전 턴 다이어그램은 그 턴의
        #      산출물로 이미 남아 있어 다시 올리지 않습니다. 이 경로는 LLM 수선을 거치지
        #      않으므로 기계적 수선과 ⚠ 표시가 전부입니다.
        if mermaid_idx == 1:
            for msg in reversed(state.messages[state.turn_message_start:]):
                if msg.msg_type == "error" or msg.sender_key == "user":
                    continue
                found = [b for b in extract_code_blocks(msg.content) if b["language"] == "mermaid"]
                if not found:
                    continue
                for block in found:
                    artifacts.append(diagram_item(
                        f"{stamp} 다이어그램 #{mermaid_idx} ({msg.sender_name} 제안)".strip(),
                        block["code"], msg.sender_name,
                    ))
                    mermaid_idx += 1
                break

        # 3. 전문가 코드 — 이번 턴 발언에서.
        artifacts.extend(self._debate_code_artifacts(state, stamp))

        # 4. 요약 JSON
        json_summary = {
            "session_id": session_id,
            "goal": state.user_prompt,
            "strategy": state.strategy,
            "total_rounds": state.current_round,
            "participating_agents": state.active_agent_keys,
            "failed_agents": list(state.failed_agent_keys),
            "synthesis_failed": synthesis_failed,
            "total_messages": len(state.messages),
            "consensus_reached": not state.failed_agent_keys and not synthesis_failed,
        }
        artifacts.append(ArtifactItem(
            artifact_type="json",
            title=f"{stamp} 토론 요약 (JSON)".strip(),
            content=json.dumps(json_summary, indent=2, ensure_ascii=False),
            language="json",
        ))

        return artifacts

_orchestrator_engine: Optional[OrchestratorEngine] = None


def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
