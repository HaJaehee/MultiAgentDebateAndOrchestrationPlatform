"""스킬 — 에이전트가 필요할 때 불러 읽는 작업 지침 묶음.

스킬 하나는 폴더 하나입니다::

    skills/
      mermaid-diagrams/
        SKILL.md          ← 머리말(name, description) + 본문 지침
        reference.md      ← 부속 문서 (`skills__read_skill_file` 로 읽음)

**점진적 공개.** 에이전트는 평소에 스킬의 이름과 한 줄 설명만 봅니다 (`skills__load_skill` 도구의
설명). 맡은 일이 그 설명에 맞으면 도구로 본문을 불러 읽고, 본문이 가리키는 부속 문서는 필요할
때만 `skills__read_skill_file` 로 읽습니다. 본문을 처음부터 시스템 프롬프트에 넣지 않는 것은,
스킬이 늘어도 발언마다 드는 토큰이 설명 몇 줄씩만 늘게 하기 위해서입니다.

**MCP 서버가 아니라 호스트 도구입니다.** 에이전트마다 볼 수 있는 스킬이 다르고
(`allowed_skills`), 목록은 스킬 폴더와 켜기·끄기를 따라 발언마다 새로 만들어집니다. 서버
프로세스의 도구 목록은 기동할 때 한 번 받는 것이라 둘 다 담을 수 없습니다. 모델 쪽에서는 MCP
도구와 구분되지 않습니다 — 같은 `서버__도구` 이름 모양이고, 같은 도구 루프·기록·접이식 카드를
탑니다. 스킬 폴더는 MADO 설치 폴더 안에 있어 filesystem 도구로는 닿지 않으므로(고정 보호),
읽기는 이 모듈이 스킬 폴더 안으로만 대신 합니다.

**실시간입니다.** 스킬의 내용, 폴더의 추가·삭제, 켜기·끄기(`skills.disabled`)는 대화 스냅샷에
굳히지 않고 발언마다 다시 읽습니다. 굳는 것은 에이전트 설정의 일부인 `allowed_skills` 뿐입니다
(`allowed_mcp_servers` 와 같습니다).

**스크립트.** 스킬 폴더에 든 Python 스크립트는 에이전트의 도구로 닿지 않는 곳에 있습니다. 그래서
실행 도구(`run_python_file`)를 가진 에이전트가 스크립트가 든 스킬을 불러오면, 폴더를 그 대화의
작업 공간 `.mado/skills/<이름>/` 으로 복사하고 실행할 경로를 알려 줍니다 (`stage_skill`). 실행은
샌드박스 도구라 도구 보안의 코드 검사와 승인을 그대로 거칩니다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import (
    PROJECT_ROOT,
    SKILL_NAME_PATTERN,
    SKILL_TOOL_PREFIX,
    RootConfig,
    get_config,
)
from app.mcp.client import clip_tool_output

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
LOAD_SKILL_TOOL = f"{SKILL_TOOL_PREFIX}__load_skill"
READ_SKILL_FILE_TOOL = f"{SKILL_TOOL_PREFIX}__read_skill_file"
_TOOL_TAILS = {"load_skill", "read_skill_file"}

# SKILL.md 하나의 상한. 본문은 부르면 통째로 모델에게 가므로, 긴 참고 내용은 부속 문서로
# 나누게 합니다.
MAX_SKILL_MD_BYTES = 256 * 1024
# 설명의 상한 (Anthropic 스킬 명세와 같은 값). 넘으면 잘라서 싣습니다.
MAX_DESCRIPTION_CHARS = 1024
# 불러올 때 보여 줄 부속 파일 목록의 상한.
MAX_LISTED_FILES = 200
# `read_skill_file` 로 읽는 파일 하나의 상한.
MAX_READ_BYTES = 256 * 1024

# 스킬 폴더 안에서 훑지 않는 폴더. 점으로 시작하는 폴더와 파일도 건너뜁니다.
SKIPPED_DIRS = frozenset({"__pycache__", "node_modules", ".venv", "venv"})

# 스크립트가 든 스킬을 복사해 둘 자리 (작업 공간 기준). `.mado` 는 런타임 전용 폴더라 @언급
# 목록과 작업 공간 다운로드에 보이지 않습니다 (`app/workspace_files.py` 의 `EXCLUDED_DIRS`).
STAGING_DIR = PurePosixPath(".mado") / "skills"
# 스크립트를 실행하는 샌드박스 도구. 서버 키가 무엇이든 이름 꼬리로 찾습니다.
RUN_TOOL_TAIL = "run_python_file"
# 한 스킬을 복사할 때의 상한. 스킬은 지침과 작은 도구 묶음이지 데이터 저장소가 아닙니다.
MAX_STAGE_FILES = 500
MAX_STAGE_BYTES = 20 * 1024 * 1024


class SkillError(ValueError):
    """스킬을 읽을 수 없는 이유. 화면과 모델에게 그대로 보여 줄 한국어 문장입니다."""


@dataclass(frozen=True)
class Skill:
    """스킬 폴더 하나를 지금 모습대로 읽은 것."""

    name: str                       # 폴더 이름 = 에이전트 설정·도구 인자에 적는 이름
    path: Path                      # 스킬 폴더 (절대 경로)
    title: str = ""                 # 머리말의 name (폴더 이름과 다를 수 있음)
    description: str = ""
    body: str = ""                  # 머리말을 뗀 SKILL.md 본문
    files: Tuple[str, ...] = ()     # SKILL.md 를 뺀 부속 파일 (스킬 폴더 기준 posix 경로)
    truncated: bool = False         # 부속 파일이 `MAX_LISTED_FILES` 를 넘었는가
    enabled: bool = True
    problem: str = ""               # 비어 있지 않으면 깨진 스킬 — 어느 에이전트에게도 주지 않음

    @property
    def usable(self) -> bool:
        return self.enabled and not self.problem

    @property
    def scripts(self) -> Tuple[str, ...]:
        """실행할 수 있는 스크립트 — 부속 파일 중 Python 파일 (샌드박스가 Python 만 돌립니다)."""
        return tuple(f for f in self.files if f.lower().endswith(".py"))


# ---------------------------------------------------------------------------
# SKILL.md 머리말
#
# YAML 전부가 아니라 스킬 머리말에 실제로 쓰이는 모양만 읽습니다 — `key: value`, 따옴표,
# `|`·`>` 블록, 들여 쓴 이어짐 줄. 쓰는 값은 name 과 description 둘뿐이고, 그 밖의 키
# (license, metadata 등)는 읽고 버립니다. 새 의존성을 들이지 않기 위해서입니다 (폐쇄망 반입).
# ---------------------------------------------------------------------------

_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")
_BLOCK_INDICATOR = re.compile(r"^[|>][+-]?[0-9]?$")
_NESTED_START = re.compile(r"^(?:[\w-]+\s*:(?:\s|$)|- )")


def parse_skill_md(text: str) -> Tuple[Dict[str, str], str]:
    """SKILL.md 를 (머리말, 본문) 으로 나눕니다. 머리말이 없거나 읽을 수 없으면 `SkillError`."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillError(
            f"{SKILL_FILE} 맨 앞에 `---` 줄로 둘러싼 머리말(name, description)이 없습니다."
        )
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise SkillError("머리말을 닫는 `---` 줄이 없습니다.")
    meta = _parse_front_matter(lines[1:end])
    body = "\n".join(lines[end + 1:]).strip("\n")
    return meta, body


def _parse_front_matter(lines: Sequence[str]) -> Dict[str, str]:
    fields: Dict[str, Tuple[str, List[str]]] = {}
    current: Optional[str] = None
    for raw in lines:
        if raw[:1] in (" ", "\t") or (not raw.strip() and current is not None):
            if current is not None:
                fields[current][1].append(raw)
            continue
        if not raw.strip() or raw.startswith("#"):
            continue
        match = _KEY_LINE.match(raw)
        if not match:
            raise SkillError(f"머리말의 이 줄을 읽지 못했습니다: {raw.strip()[:80]}")
        current = match.group(1)
        fields[current] = (match.group(2).strip(), [])
    return {key: _scalar(value, rest) for key, (value, rest) in fields.items()}


def _scalar(value: str, rest: List[str]) -> str:
    block = list(rest)
    while block and not block[-1].strip():
        block.pop()

    if _BLOCK_INDICATOR.match(value):
        filled = [line for line in block if line.strip()]
        indent = min((len(line) - len(line.lstrip()) for line in filled), default=0)
        rows = [line[indent:] if line.strip() else "" for line in block]
        return "\n".join(rows) if value[0] == "|" else _fold(rows)

    if value[:1] in ("\"", "'"):
        return _unquote(" ".join([value] + [line.strip() for line in block if line.strip()]))

    if not value and block:
        first = next(line.strip() for line in block if line.strip())
        if _NESTED_START.match(first):
            return ""   # 목록·매핑 값 (metadata 등). 쓰지 않습니다.

    parts = [_strip_comment(value)] + [line.strip() for line in block if line.strip()]
    return " ".join(part for part in parts if part)


def _fold(rows: List[str]) -> str:
    """`>` 블록: 이어진 줄은 한 칸 띄워 붙이고, 빈 줄은 줄바꿈으로 남깁니다."""
    paragraphs: List[str] = []
    current: List[str] = []
    for row in rows:
        if row.strip():
            current.append(row.strip())
        else:
            paragraphs.append(" ".join(current))
            current = []
    paragraphs.append(" ".join(current))
    return "\n".join(p for p in paragraphs if p)


def _unquote(text: str) -> str:
    quote = text[0]
    closing = -1
    i = 1
    while i < len(text):
        if quote == "\"" and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1:i + 2] == "'":
                i += 2
                continue
            closing = i
            break
        i += 1
    if closing < 0:
        raise SkillError("머리말 값의 따옴표가 닫히지 않았습니다.")
    inner = text[1:closing]
    if quote == "'":
        return inner.replace("''", "'")
    escapes = {"n": "\n", "t": "\t", "\"": "\"", "\\": "\\", "/": "/"}
    return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), inner)


def _strip_comment(value: str) -> str:
    """따옴표 없는 값 끝의 ` # 주석` 을 뗍니다 (`C#` 처럼 붙은 # 은 값입니다)."""
    cut = value.find(" #")
    return (value[:cut] if cut >= 0 else value).strip()


# ---------------------------------------------------------------------------
# 스킬 폴더 읽기
# ---------------------------------------------------------------------------

# SKILL.md 경로 → ((mtime_ns, 크기), (title, description, body, problem)). 폴더는 발언마다 훑지만
# 바뀌지 않은 본문은 다시 읽지 않습니다.
_parse_cache: Dict[str, Tuple[Tuple[int, int], Tuple[str, str, str, str]]] = {}
_cache_lock = threading.Lock()


def skills_root(config: Optional[RootConfig] = None) -> Path:
    """스킬 폴더 (`skills.dir`). 상대 경로면 프로젝트 루트 기준입니다."""
    cfg = config or get_config()
    raw = (cfg.skills.dir or "").strip() or "skills"
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def scan_skills(
    root: Optional[Path] = None, disabled: Optional[Iterable[str]] = None,
) -> List[Skill]:
    """스킬 폴더를 지금 모습대로 읽습니다. 이름순이고, 깨진 스킬도 이유와 함께 들어 있습니다.

    발언마다 불러도 될 만큼 가볍습니다 — 폴더 목록과 파일 정보만 보고, 본문은 바뀐 것만
    다시 읽습니다. 설정을 주지 않으면 지금 설정(`skills.dir`, `skills.disabled`)을 씁니다.
    """
    if root is None or disabled is None:
        cfg = get_config()
        root = skills_root(cfg) if root is None else root
        disabled = cfg.skills.disabled if disabled is None else disabled
    off = set(disabled or ())
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []
    except OSError as exc:
        logger.warning(f"Could not list the skills folder {root}: {exc}")
        return []
    skills: List[Skill] = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name in SKIPPED_DIRS or not entry.is_dir():
            continue
        skills.append(_read_skill(entry, enabled=entry.name not in off))
    return skills


def _read_skill(folder: Path, enabled: bool) -> Skill:
    name = folder.name
    problem = ""
    if not SKILL_NAME_PATTERN.fullmatch(name):
        problem = (
            "폴더 이름은 영문·숫자·밑줄·하이픈으로 64자까지 쓰고 영문이나 숫자로 시작해야 합니다."
        )
    title = description = body = ""
    md = folder / SKILL_FILE
    if md.is_file():
        title, description, body, parse_problem = _parse_cached(md)
        problem = problem or parse_problem
    else:
        problem = problem or f"{SKILL_FILE} 이 없습니다."
    files, truncated = _list_files(folder)
    return Skill(
        name=name, path=folder.resolve(), title=title, description=description, body=body,
        files=files, truncated=truncated, enabled=enabled, problem=problem,
    )


def _parse_cached(md: Path) -> Tuple[str, str, str, str]:
    try:
        stat = md.stat()
    except OSError as exc:
        return "", "", "", f"{SKILL_FILE} 을 읽지 못했습니다: {exc}"
    key, stamp = str(md), (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        hit = _parse_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    result = _parse_skill_file(md, stat.st_size)
    with _cache_lock:
        _parse_cache[key] = (stamp, result)
    return result


def _parse_skill_file(md: Path, size: int) -> Tuple[str, str, str, str]:
    if size > MAX_SKILL_MD_BYTES:
        return "", "", "", (
            f"{SKILL_FILE} 이 너무 큽니다 ({size:,}바이트, 상한 {MAX_SKILL_MD_BYTES:,}). "
            f"긴 참고 내용은 부속 문서로 나누세요."
        )
    try:
        text = md.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return "", "", "", f"{SKILL_FILE} 이 UTF-8 로 저장되어 있지 않습니다."
    except OSError as exc:
        return "", "", "", f"{SKILL_FILE} 을 읽지 못했습니다: {exc}"
    try:
        meta, body = parse_skill_md(text)
    except SkillError as exc:
        return "", "", "", str(exc)

    title = " ".join((meta.get("name") or "").split())
    description = " ".join((meta.get("description") or "").split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS - 1] + "…"
    if not description:
        return title, "", body, (
            "머리말에 description 이 없습니다. 언제 이 스킬을 쓰는지 한두 문장으로 적으세요 — "
            "에이전트는 이 설명만 보고 스킬을 부를지 정합니다."
        )
    if not body.strip():
        return title, description, "", "본문(지침)이 비어 있습니다."
    return title, description, body, ""


def _list_files(folder: Path) -> Tuple[Tuple[str, ...], bool]:
    """SKILL.md 를 뺀 부속 파일. 숨김·캐시 폴더와 심볼릭 링크(폴더 밖을 가리킬 수 있음)는 뺍니다."""
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in SKIPPED_DIRS
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            full = Path(dirpath) / filename
            if full.is_symlink():
                continue
            relative = full.relative_to(folder).as_posix()
            if relative == SKILL_FILE:
                continue
            if len(found) >= MAX_LISTED_FILES:
                return tuple(found), True
            found.append(relative)
    return tuple(found), False


# ---------------------------------------------------------------------------
# 에이전트에게 줄 도구
# ---------------------------------------------------------------------------


def visible_skills(agent: Any, skills: Optional[Sequence[Skill]] = None) -> List[Skill]:
    """이 에이전트가 지금 쓸 수 있는 스킬 — 할당되었고(`allowed_skills`), 켜져 있고, 깨지지 않은 것."""
    allowed = set(getattr(agent, "allowed_skills", None) or ())
    if not allowed:
        return []   # 스킬을 받지 않은 에이전트는 폴더도 훑지 않습니다
    pool = scan_skills() if skills is None else skills
    return [s for s in pool if s.name in allowed and s.usable]


def skill_tools(skills: Sequence[Skill]) -> List[Dict[str, Any]]:
    """스킬 도구의 정의. 목록(이름과 설명)은 `load_skill` 의 설명에 싣습니다. 스킬이 없으면 빈 목록."""
    if not skills:
        return []
    catalog = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    tools: List[Dict[str, Any]] = [{
        "type": "function",
        "function": {
            "name": LOAD_SKILL_TOOL,
            "description": (
                f"[{SKILL_TOOL_PREFIX}] 스킬의 지침(SKILL.md)을 불러옵니다. 맡은 일이 아래 스킬의 "
                f"설명에 맞으면, 답을 쓰기 전에 먼저 불러 그 지침을 따르세요. 이번 발언에서 이미 "
                f"불렀다면 다시 부르지 않아도 됩니다.\n\n쓸 수 있는 스킬:\n{catalog}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "enum": [s.name for s in skills],
                        "description": "불러올 스킬 이름",
                    },
                },
                "required": ["skill"],
            },
        },
    }]
    with_files = [s.name for s in skills if s.files]
    if with_files:
        tools.append({
            "type": "function",
            "function": {
                "name": READ_SKILL_FILE_TOOL,
                "description": (
                    f"[{SKILL_TOOL_PREFIX}] 불러온 스킬의 부속 파일(참고 문서·예시·템플릿)을 "
                    f"읽습니다. path 는 `{LOAD_SKILL_TOOL}` 결과의 '부속 파일' 목록에 적힌, "
                    f"스킬 폴더 기준 경로입니다. 글 파일만 읽습니다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "enum": with_files, "description": "스킬 이름"},
                        "path": {"type": "string", "description": "스킬 폴더 기준 파일 경로"},
                    },
                    "required": ["skill", "path"],
                },
            },
        })
    return tools


def skill_tools_for(agent: Any) -> List[Dict[str, Any]]:
    """이 에이전트의 요청에 실을 스킬 도구. 스킬을 못 읽어도 발언은 스킬 없이 진행합니다."""
    if not getattr(agent, "allowed_skills", None):
        return []
    try:
        return skill_tools(visible_skills(agent))
    except Exception as exc:  # noqa: BLE001 - 스킬 때문에 발언을 막지 않습니다
        logger.warning(
            f"Could not read skills for {getattr(agent, 'key', '?')}: {type(exc).__name__}: {exc}"
        )
        return []


def visible_skill_names(agent: Any) -> List[str]:
    """오케스트레이터에게 보여 줄 이 에이전트의 스킬 이름. 못 읽으면 빈 목록."""
    if not getattr(agent, "allowed_skills", None):
        return []
    try:
        return [s.name for s in visible_skills(agent)]
    except Exception:  # noqa: BLE001
        return []


def is_skill_tool(tool_name: str) -> bool:
    """스킬 도구인가. 모델이 앞자리를 떼고 부르는 경우(`load_skill`)도 받습니다."""
    name = (tool_name or "").strip()
    if "__" in name:
        prefix, tail = name.split("__", 1)
        return prefix == SKILL_TOOL_PREFIX and tail in _TOOL_TAILS
    return name in _TOOL_TAILS


SKILL_GUIDANCE_HEAD = "[스킬]"


def skill_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """스킬 도구를 **가진** 에이전트에게만 붙는 상시 지침. 없으면 None.

    목록 자체는 도구 설명에 있습니다. 여기서는 "먼저 불러라" 는 순서만 말합니다 — 도구 설명만
    두면 모델이 스킬을 참고 자료쯤으로 여겨 부르지 않고 자기 방식대로 답하곤 합니다.
    """
    names = {str((t.get("function") or {}).get("name") or "") for t in tools or ()}
    if LOAD_SKILL_TOOL not in names:
        return None
    lines = [
        SKILL_GUIDANCE_HEAD,
        f"- 맡은 일에 맞는 스킬이 `{LOAD_SKILL_TOOL}` 설명의 목록에 있으면, 답을 쓰기 전에 먼저 "
        f"불러 그 지침을 따르세요. 스킬의 지침은 일반적인 방법보다 우선합니다.",
    ]
    if READ_SKILL_FILE_TOOL in names:
        lines.append(
            f"- 스킬 본문이 가리키는 부속 문서는 필요할 때만 `{READ_SKILL_FILE_TOOL}` 로 읽으세요."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


async def run_skill_tool(
    agent: Any,
    tool_name: str,
    arguments: Any,
    *,
    workspace: Optional[Path] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    """스킬 도구 하나를 실행하고 (결과, 상태) 를 돌려줍니다. 실패도 결과로 돌려줍니다.

    쓸 수 있는지는 **부르는 순간** 다시 봅니다. 목록을 받은 뒤에 스킬이 꺼졌거나 지워졌으면
    그 사실을 결과로 알립니다 — 켜기·끄기가 진행 중인 발언에도 곧바로 걸리게 하기 위해서입니다.

    `workspace` 는 이 발언의 작업 공간, `tools` 는 이 발언이 실제로 든 도구입니다. 스크립트가 든
    스킬을 불러올 때 복사할 곳과, 실행 도구를 가졌는지를 여기서 봅니다.
    """
    args = arguments if isinstance(arguments, dict) else {}
    requested = str(args.get("skill") or args.get("name") or "").strip()
    if not requested:
        return "불러올 스킬 이름(skill)을 지정하세요.", "error"

    skills = await asyncio.to_thread(visible_skills, agent)
    skill = next((s for s in skills if s.name == requested), None)
    if skill is None:
        return _unavailable_text(requested, skills), "error"

    if tool_name.rsplit("__", 1)[-1] == "load_skill":
        text = render_skill(skill)
        if skill.scripts:
            text += "\n\n" + await _scripts_section(skill, workspace, tools)
        return clip_tool_output(text), "success"
    return await asyncio.to_thread(read_skill_file, skill, str(args.get("path") or ""))


def _unavailable_text(requested: str, skills: Sequence[Skill]) -> str:
    head = (
        f"'{requested}' 스킬은 지금 이 에이전트가 쓸 수 없습니다 (없는 이름이거나, 꺼졌거나, "
        f"이 에이전트에게 할당되지 않았습니다)."
    )
    if skills:
        return f"{head} 쓸 수 있는 스킬: {', '.join(s.name for s in skills)}"
    return f"{head} 지금 쓸 수 있는 스킬이 없습니다. 스킬 없이 진행하세요."


def render_skill(skill: Skill) -> str:
    """`load_skill` 의 결과 — 본문과, 필요하면 읽을 부속 파일 목록."""
    lines = [f"# 스킬: {skill.name}", "", skill.body.strip()]
    if skill.files:
        lines += [
            "",
            "---",
            f"부속 파일 (필요할 때 `{READ_SKILL_FILE_TOOL}` 로 읽습니다. 스킬 폴더 기준 경로):",
        ]
        lines += [f"- {f}" for f in skill.files]
        if skill.truncated:
            lines.append(f"- … (목록은 {MAX_LISTED_FILES}개까지만 보여 줍니다)")
    return "\n".join(lines)


def read_skill_file(skill: Skill, raw_path: str) -> Tuple[str, str]:
    """스킬 폴더 **안의** 글 파일 하나를 읽습니다. 밖을 가리키는 경로는 받지 않습니다."""
    text = (raw_path or "").strip().replace("\\", "/")
    if not text:
        return "읽을 파일 경로(path)를 지정하세요.", "error"
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return f"'{raw_path}' — 스킬 폴더 기준 상대 경로로 지정하세요 (예: reference.md).", "error"
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if not parts or ".." in parts:
        return f"'{raw_path}' — 스킬 폴더 밖은 읽을 수 없습니다.", "error"
    if any(p.startswith(".") or p in SKIPPED_DIRS for p in parts):
        return f"'{raw_path}' — 숨김 파일과 캐시 폴더는 읽지 않습니다.", "error"

    root = skill.path.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        return f"'{raw_path}' — 스킬 폴더 밖을 가리키는 경로입니다.", "error"
    if not target.is_file():
        listed = ", ".join(skill.files[:20]) or "없음"
        return f"'{text}' 파일이 '{skill.name}' 스킬에 없습니다. 부속 파일: {listed}", "error"

    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(MAX_READ_BYTES)
    except OSError as exc:
        return f"'{text}' 을 읽지 못했습니다: {exc}", "error"
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if size > MAX_READ_BYTES:
            # 상한에서 자른 자리가 글자 가운데일 수 있습니다.
            content = data.decode("utf-8-sig", errors="ignore")
        else:
            return (
                f"'{text}' 은 글 파일이 아니어서 읽을 수 없습니다 (이 도구는 UTF-8 글 문서만 읽습니다).",
                "error",
            )
    if "\x00" in content:
        return f"'{text}' 은 글 파일이 아니어서 읽을 수 없습니다.", "error"
    if size > MAX_READ_BYTES:
        content += f"\n\n... [파일이 커서 앞 {MAX_READ_BYTES:,}바이트만 읽었습니다 (전체 {size:,}바이트)]"
    return clip_tool_output(content), "success"


# ---------------------------------------------------------------------------
# 스크립트 — 작업 공간에 복사해 샌드박스로 실행
# ---------------------------------------------------------------------------


def _tool_by_tail(tools: Optional[List[Dict[str, Any]]], tail: str) -> Optional[str]:
    """이 발언이 든 도구 중 이름이 `tail` 로 끝나는 것 (`sandbox__run_python_file` 등)."""
    for tool in tools or ():
        name = str((tool.get("function") or {}).get("name") or "")
        if name == tail or name.endswith(f"__{tail}"):
            return name
    return None


async def _scripts_section(
    skill: Skill, workspace: Optional[Path], tools: Optional[List[Dict[str, Any]]],
) -> str:
    """스크립트가 든 스킬을 불러올 때 본문 뒤에 붙는 안내. 필요하면 먼저 작업 공간에 복사합니다.

    실행 도구가 없는 에이전트에게는 복사하지 않습니다. 복사하는 까닭이 실행이고, 쓰지도 않을
    파일로 작업 공간을 어지럽히지 않기 위해서입니다. 복사에 실패해도 본문은 그대로 돌려줍니다 —
    지침만으로도 쓸모가 있습니다.
    """
    run_tool = _tool_by_tail(tools, RUN_TOOL_TAIL)
    if run_tool is None:
        return (
            f"---\n이 스킬에는 스크립트가 있지만, 이 에이전트에게는 스크립트를 실행할 도구"
            f"(`{RUN_TOOL_TAIL}`)가 없어 작업 공간에 복사하지 않았습니다. 지침과 부속 문서만 쓰거나, "
            f"실행 도구를 가진 에이전트에게 실행을 맡기세요."
        )
    if workspace is None:
        return "---\n이 발언의 작업 공간을 알 수 없어 스크립트를 복사하지 못했습니다. 지침과 부속 문서만 쓰세요."
    try:
        staged = await asyncio.to_thread(stage_skill, skill, Path(workspace))
    except (SkillError, OSError) as exc:
        logger.warning(f"Could not stage skill '{skill.name}' into {workspace}: {exc}")
        return f"---\n스크립트를 작업 공간에 복사하지 못했습니다 ({exc}). 지침과 부속 문서만 쓰세요."

    paths = [(staged / script).as_posix() for script in skill.scripts]
    lines = [
        "---",
        f"스크립트 — 작업 공간의 `{staged.as_posix()}/` 에 복사했습니다. SKILL.md 가 정한 것을 실행하세요:",
    ]
    lines += [f"- `{path}`" for path in paths]
    lines += [
        "",
        f"실행: `{run_tool}` 에 `file_path` 로 위 경로를 넘깁니다 (예: `{paths[0]}`). 스크립트는 작업 "
        f"공간을 작업 폴더(cwd)로 삼아 **인자 없이** 돕니다 — 입력과 출력은 SKILL.md 가 정한 대로 작업 "
        f"공간의 파일로 주고받습니다. 복사본은 고치지 마세요. 스킬을 부를 때마다 원본으로 되돌아갑니다.",
    ]
    return "\n".join(lines)


# 복사 대상 폴더 → 잠금. 같은 작업 공간을 쓰는 발언이 동시에 같은 스킬을 불러도 복사가 섞이지
# 않게 합니다. 복사는 스레드에서 돌므로 threading 잠금입니다.
_stage_locks: Dict[str, threading.Lock] = {}
_stage_locks_guard = threading.Lock()


def _stage_lock(target: Path) -> threading.Lock:
    key = os.path.normcase(str(target))
    with _stage_locks_guard:
        return _stage_locks.setdefault(key, threading.Lock())


def _stage_sources(folder: Path) -> List[Tuple[str, Path, os.stat_result]]:
    """복사할 파일 (SKILL.md 포함). 부속 파일 목록과 같은 것을 뺍니다. 상한을 넘으면 `SkillError`."""
    found: List[Tuple[str, Path, os.stat_result]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in SKIPPED_DIRS
        )
        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            if filename.startswith(".") or full.is_symlink():
                continue
            stat = full.stat()
            total += stat.st_size
            found.append((full.relative_to(folder).as_posix(), full, stat))
            if len(found) > MAX_STAGE_FILES:
                raise SkillError(f"스킬의 파일이 너무 많습니다 (상한 {MAX_STAGE_FILES}개)")
            if total > MAX_STAGE_BYTES:
                raise SkillError(f"스킬이 너무 큽니다 (상한 {MAX_STAGE_BYTES // (1024 * 1024)}MB)")
    return found


def stage_skill(skill: Skill, workspace: Path) -> PurePosixPath:
    """스킬 폴더를 작업 공간의 `.mado/skills/<이름>/` 으로 복사하고, 그 자리를 작업 공간 기준
    상대 경로로 돌려줍니다.

    **바뀐 파일만** 다시 복사합니다 (크기와 수정 시각이 원본과 다를 때). 그래서 스킬을 고치면 다음에
    불러올 때 새 스크립트가 놓이고, 에이전트가 복사본을 고쳤더라도 원본으로 되돌아갑니다. 원본에
    없는 파일은 지우지 않습니다 — 스크립트가 자기 폴더에 남긴 결과물일 수 있기 때문입니다.
    """
    relative = STAGING_DIR / skill.name
    target = Path(workspace).joinpath(*relative.parts)
    sources = _stage_sources(skill.path)
    with _stage_lock(target):
        for rel, source, stat in sources:
            dest = target.joinpath(*rel.split("/"))
            try:
                current = dest.stat()
                if current.st_size == stat.st_size and current.st_mtime_ns == stat.st_mtime_ns:
                    continue
            except FileNotFoundError:
                pass
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    return relative
