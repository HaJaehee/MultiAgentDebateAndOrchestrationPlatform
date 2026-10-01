"""스킬 — 에이전트가 필요할 때 동적으로 참조하는 작업 지침 패키지입니다.

스킬 하나는 개별 디렉터리로 구성됩니다::

    skills/
      mermaid-diagrams/
        SKILL.md          ← 헤더(name, description) 및 본문 지침
        reference.md      ← 부속 문서 (`skills__read_skill_file` 도구로 조회)

**점진적 공개.** 에이전트는 평상시 스킬의 명칭과 한 줄 요약 설명만 인지합니다 (`skills__load_skill` 도구의
설명). 수행할 작업이 해당 설명과 부합할 때 도구를 호출하여 본문 지침을 불러오며, 본문이 안내하는 부속 문서는
필요한 경우에만 `skills__read_skill_file` 도구로 조회합니다. 본문 전체를 시스템 프롬프트에 상시 적재하지 않는 이유는,
스킬 개수가 늘어나더라도 발언마다 소비되는 기본 토큰이 요약 설명 몇 줄 수준으로 최소화되기 때문입니다.

**MCP 서버가 아닌 호스트 도구로 동작합니다.** 에이전트마다 접근 가능한 스킬 목록이 상이하며
(`allowed_skills`), 이 목록은 스킬 디렉터리의 실제 상태와 활성화 설정을 반영하여 발언 시점마다 동적으로 구성됩니다.
외부 서버 프로세스의 도구 스키마는 기동 시 1회만 전달받으므로 이러한 동적 제어를 처리할 수 없습니다. 모델 입장에서는
일반 MCP 도구와 구분되지 않으며, 동일한 `서버__도구` 형태의 명칭 체계와 호출 루프·기록·접이식 카드를
그대로 사용합니다. 스킬 디렉터리는 MADO 설치 폴더 내에 위치하여 일반 파일시스템 도구로는 접근할 수 없으므로(고정 보호),
지침 파일 읽기는 본 모듈이 스킬 디렉터리 내부로 한정하여 안전하게 대행합니다.

**실시간으로 반영됩니다.** 스킬의 본문 내용, 디렉터리 추가·삭제, 활성화/비활성화(`skills.disabled`) 설정은 대화 스냅샷에
고정되지 않고 발언 시점마다 새로 조회됩니다. 대화 시작 시점에 고정되는 것은 에이전트별 접근 권한인 `allowed_skills` 설정뿐입니다
(`allowed_mcp_servers` 도구 권한과 동일합니다).

**실행 스크립트를 지원합니다.** 스킬 디렉터리에 포함된 Python 스크립트는 설치 디렉터리 보호 규칙에 의해 에이전트가 직접 실행할 수 없습니다.
이에 따라 실행 도구(`run_python_file`)를 보유한 에이전트가 스크립트가 포함된 스킬을 호출하면, 해당 스킬 디렉터리를 대화 세션의
작업 공간(`.mado/skills/<이름>/`)으로 자동 복사하고 실행 가능한 경로를 함께 안내합니다 (`stage_skill`). 실제 실행은
샌드박스 도구를 거치므로 도구 보안의 코드 정적 분석과 승인 절차를 동일하게 준수합니다.
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
    """스킬을 정상적으로 로드할 수 없는 원인을 나타냅니다. 사용자 화면 및 에이전트 모델에 직접 전달되는 한국어 메시지입니다."""


@dataclass(frozen=True)
class Skill:
    """스킬 디렉터리의 현재 상태를 반영하는 데이터 모델입니다."""

    name: str                       # 폴더 이름 = 에이전트 설정·도구 인자에 사용하는 식별자
    path: Path                      # 스킬 폴더 (절대 경로)
    title: str = ""                 # 머리말의 name (폴더 이름과 다를 수 있습니다)
    description: str = ""
    body: str = ""                  # 머리말을 제외한 SKILL.md 본문 지침
    files: Tuple[str, ...] = ()     # SKILL.md 를 제외한 부속 파일 목록 (스킬 폴더 기준 posix 경로)
    truncated: bool = False         # 부속 파일 목록이 `MAX_LISTED_FILES` 한도를 초과했는지 여부
    enabled: bool = True
    problem: str = ""               # 비어 있지 않으면 오류가 발생한 스킬 — 어떤 에이전트에도 할당하지 않습니다

    @property
    def usable(self) -> bool:
        return self.enabled and not self.problem

    @property
    def scripts(self) -> Tuple[str, ...]:
        """실행 가능한 스크립트 목록입니다 (부속 파일 중 Python 파일 대상)."""
        return tuple(f for f in self.files if f.lower().endswith(".py"))


# ---------------------------------------------------------------------------
# SKILL.md 머리말
#
# YAML 전체가 아닌 스킬 머리말에 실제로 사용되는 구문만 파싱합니다 — `key: value`, 따옴표,
# `|`·`>` 블록, 들여쓴 연속 행. 실제 사용하는 필드는 name과 description 2종이며, 그 외의 키
# (license, metadata 등)는 읽고 무시합니다. 외부 라이브러리 의존성을 배제하기 위함입니다 (폐쇄망 환경 지원).
# ---------------------------------------------------------------------------

_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")
_BLOCK_INDICATOR = re.compile(r"^[|>][+-]?[0-9]?$")
_NESTED_START = re.compile(r"^(?:[\w-]+\s*:(?:\s|$)|- )")


def parse_skill_md(text: str) -> Tuple[Dict[str, str], str]:
    """SKILL.md 파일을 (머리말, 본문) 튜플로 분리합니다. 머리말이 누락되었거나 파싱할 수 없으면 `SkillError`를 발생시킵니다."""
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
            raise SkillError(f"머리말의 해당 행을 읽지 못했습니다: {raw.strip()[:80]}")
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
    """`>` 블록을 처리합니다: 연결된 행은 공백으로 병합하고, 빈 행은 단락 구분(개행)으로 유지합니다."""
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
        raise SkillError("머리말 값의 따옴표가 올바르게 닫히지 않았습니다.")
    inner = text[1:closing]
    if quote == "'":
        return inner.replace("''", "'")
    escapes = {"n": "\n", "t": "\t", "\"": "\"", "\\": "\\", "/": "/"}
    return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), inner)


def _strip_comment(value: str) -> str:
    """따옴표가 없는 값 끝에 위치한 ` # 주석`을 제거합니다 (`C#`과 같이 문자열에 포함된 #은 유지합니다)."""
    cut = value.find(" #")
    return (value[:cut] if cut >= 0 else value).strip()


# ---------------------------------------------------------------------------
# 스킬 폴더 읽기
# ---------------------------------------------------------------------------

# SKILL.md 경로 → ((mtime_ns, 크기), (title, description, body, problem)). 폴더는 발언마다 스캔하지만
# 변경되지 않은 본문은 캐시를 활용하여 다시 파싱하지 않습니다.
_parse_cache: Dict[str, Tuple[Tuple[int, int], Tuple[str, str, str, str]]] = {}
_cache_lock = threading.Lock()


def skills_root(config: Optional[RootConfig] = None) -> Path:
    """스킬 폴더 경로(`skills.dir`)를 반환합니다. 상대 경로인 경우 프로젝트 루트 기준입니다."""
    cfg = config or get_config()
    raw = (cfg.skills.dir or "").strip() or "skills"
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def scan_skills(
    root: Optional[Path] = None, disabled: Optional[Iterable[str]] = None,
) -> List[Skill]:
    """스킬 폴더의 현재 상태를 스캔합니다. 이름순으로 정렬되며, 유효하지 않은 스킬도 원인과 함께 포함됩니다.

    매 발언마다 호출해도 될 정도로 가볍습니다. 폴더 목록과 파일 정보만 확인하며, 본문은 변경된 경우에만
    다시 읽습니다. 설정을 지정하지 않으면 현재 설정(`skills.dir`, `skills.disabled`)을 사용합니다.
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
            "폴더 이름은 영문, 숫자, 밑줄(_), 하이픈(-)으로 64자 이내로 구성해야 하며 영문이나 숫자로 시작해야 합니다."
        )
    title = description = body = ""
    md = folder / SKILL_FILE
    if md.is_file():
        title, description, body, parse_problem = _parse_cached(md)
        problem = problem or parse_problem
    else:
        problem = problem or f"{SKILL_FILE} 파일이 없습니다."
    files, truncated = _list_files(folder)
    return Skill(
        name=name, path=folder.resolve(), title=title, description=description, body=body,
        files=files, truncated=truncated, enabled=enabled, problem=problem,
    )


def _parse_cached(md: Path) -> Tuple[str, str, str, str]:
    try:
        stat = md.stat()
    except OSError as exc:
        return "", "", "", f"{SKILL_FILE} 파일을 읽지 못했습니다: {exc}"
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
            f"{SKILL_FILE} 이 너무 큽니다 ({size:,}바이트, 상한 {MAX_SKILL_MD_BYTES:,}바이트). "
            f"긴 참고 내용은 부속 문서로 분리하십시오."
        )
    try:
        text = md.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return "", "", "", f"{SKILL_FILE} 파일이 UTF-8로 저장되어 있지 않습니다."
    except OSError as exc:
        return "", "", "", f"{SKILL_FILE} 파일을 읽지 못했습니다: {exc}"
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
            "머리말에 description이 없습니다. 언제 이 스킬을 사용하는지 한두 문장으로 작성해 주십시오. "
            "에이전트는 이 설명만 보고 스킬을 호출할지 결정합니다."
        )
    if not body.strip():
        return title, description, "", "본문(지침)이 비어 있습니다."
    return title, description, body, ""


def _list_files(folder: Path) -> Tuple[Tuple[str, ...], bool]:
    """SKILL.md를 제외한 부속 파일 목록입니다. 숨김·캐시 폴더와 심볼릭 링크(폴더 외부를 가리킬 수 있음)는 제외합니다."""
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
# 에이전트에 제공할 도구
# ---------------------------------------------------------------------------


def visible_skills(agent: Any, skills: Optional[Sequence[Skill]] = None) -> List[Skill]:
    """이 에이전트가 현재 사용할 수 있는 스킬 목록입니다 — 할당되었고(`allowed_skills`), 활성화되어 있으며, 오류가 없는 스킬입니다."""
    allowed = set(getattr(agent, "allowed_skills", None) or ())
    if not allowed:
        return []   # 스킬을 할당받지 않은 에이전트는 폴더도 조회하지 않습니다.
    pool = scan_skills() if skills is None else skills
    return [s for s in pool if s.name in allowed and s.usable]


def skill_tools(skills: Sequence[Skill]) -> List[Dict[str, Any]]:
    """스킬 도구의 정의입니다. 목록(이름과 설명)은 `load_skill`의 설명에 포함됩니다. 스킬이 없으면 빈 목록을 반환합니다."""
    if not skills:
        return []
    catalog = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    tools: List[Dict[str, Any]] = [{
        "type": "function",
        "function": {
            "name": LOAD_SKILL_TOOL,
            "description": (
                f"[{SKILL_TOOL_PREFIX}] 스킬의 지침(SKILL.md)을 불러옵니다. 수행할 작업이 아래 스킬의 "
                f"설명에 부합하면, 답변을 작성하기 전에 먼저 호출하여 해당 지침을 따르십시오. 이번 발언에서 이미 "
                f"호출했다면 다시 호출하지 않아도 됩니다.\n\n사용 가능한 스킬:\n{catalog}"
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
                    f"읽습니다. path는 `{LOAD_SKILL_TOOL}` 결과의 '부속 파일' 목록에 기재된, "
                    f"스킬 폴더 기준 상대 경로입니다. 텍스트 파일만 읽습니다."
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
    """이 에이전트의 요청에 전달할 스킬 도구 목록입니다. 스킬을 읽지 못해도 발언은 스킬 없이 계속 진행합니다."""
    if not getattr(agent, "allowed_skills", None):
        return []
    try:
        return skill_tools(visible_skills(agent))
    except Exception as exc:  # noqa: BLE001 - 스킬 오류로 인해 발언이 차단되지 않도록 합니다.
        logger.warning(
            f"Could not read skills for {getattr(agent, 'key', '?')}: {type(exc).__name__}: {exc}"
        )
        return []


def visible_skill_names(agent: Any) -> List[str]:
    """오케스트레이터에게 표시할 이 에이전트의 스킬 이름 목록입니다. 읽지 못하면 빈 목록을 반환합니다."""
    if not getattr(agent, "allowed_skills", None):
        return []
    try:
        return [s.name for s in visible_skills(agent)]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 유저 지정 — 입력창의 `@전문가 @스킬`
#
# 지정은 그 턴에만 겁니다. 대화에 굳은 `allowed_skills` 는 바꾸지 않고, 그 턴의 발언에 쓸
# 에이전트 사본에 스킬을 더합니다 (`with_designated_skills`). 그리고 발언 첫머리에 호스트가
# `load_skill` 을 대신 불러 결과를 넣어 둡니다 (`LLMCaller._preload_skills`). 모델이
# 스킬 목록을 참고만 하고 넘어가는 일이 없도록, 지정한 스킬은 반드시 읽고 시작하게 합니다.
#
# 켜기·끄기는 지정보다 앞섭니다. 꺼진 스킬은 `visible_skills` 에서 빠지므로 지정해도 주지 않습니다.
# ---------------------------------------------------------------------------

DESIGNATED_SKILL_NOTE = (
    "[유저 지정] 유저가 이번 턴에 당신에게 이 스킬을 쓰도록 지정해, 앱이 미리 불러 두었습니다. "
    "아래 지침을 따라 답하십시오."
)


def with_designated_skills(agent: Any, names: Optional[Iterable[str]]) -> Any:
    """`names` 를 `allowed_skills` 에 더한 에이전트 사본. 더할 것이 없으면 그대로 돌려줍니다."""
    current = list(getattr(agent, "allowed_skills", None) or ())
    extra = [n for n in dict.fromkeys(names or ()) if n and n not in current]
    if not extra:
        return agent
    return agent.model_copy(update={"allowed_skills": current + extra})


def offered_skills(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    """이번 발언의 `load_skill` 도구가 받는 스킬 이름 (도구 정의의 enum). 도구가 없으면 빈 목록."""
    for tool in tools or ():
        function = tool.get("function") or {}
        if function.get("name") == LOAD_SKILL_TOOL:
            prop = ((function.get("parameters") or {}).get("properties") or {}).get("skill") or {}
            return [str(name) for name in prop.get("enum") or ()]
    return []


def is_skill_tool(tool_name: str) -> bool:
    """스킬 도구인지 여부를 확인합니다. 모델이 접두사를 생략하고 호출한 경우(`load_skill`)도 지원합니다."""
    name = (tool_name or "").strip()
    if "__" in name:
        prefix, tail = name.split("__", 1)
        return prefix == SKILL_TOOL_PREFIX and tail in _TOOL_TAILS
    return name in _TOOL_TAILS


SKILL_GUIDANCE_HEAD = "[스킬]"


def skill_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """스킬 도구를 **보유한** 에이전트에게만 추가되는 상시 지침입니다. 도구가 없으면 None을 반환합니다.

    스킬 목록 자체는 도구 설명에 기재되어 있습니다. 여기서는 '먼저 호출하라'는 실행 순서만 안내합니다.
    도구 설명에만 두면 모델이 스킬을 단순 참고 자료로 여겨 호출하지 않고 임의의 방식으로 답변하는 경향이 있습니다.
    """
    names = {str((t.get("function") or {}).get("name") or "") for t in tools or ()}
    if LOAD_SKILL_TOOL not in names:
        return None
    lines = [
        SKILL_GUIDANCE_HEAD,
        f"- 수행할 작업에 적합한 스킬이 `{LOAD_SKILL_TOOL}` 설명의 목록에 포함되어 있다면, 답변을 작성하기 전에 먼저 "
        f"호출하여 해당 지침을 따르십시오. 스킬의 지침은 일반적인 처리 방식보다 우선합니다.",
    ]
    if READ_SKILL_FILE_TOOL in names:
        lines.append(
            f"- 스킬 본문에서 참조하는 부속 문서는 필요한 경우에만 `{READ_SKILL_FILE_TOOL}`로 읽으십시오."
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
    """스킬 도구 하나를 실행하고 (결과, 상태) 튜플을 반환합니다. 실행 실패 시에도 오류 결과를 반환합니다.

    사용 가능 여부는 **호출 시점**에 다시 검증합니다. 스킬 목록을 전달받은 이후 스킬이 비활성화되었거나 삭제된 경우
    해당 사실을 결과로 알립니다. 이는 활성화/비활성화 변경 사항이 진행 중인 발언에도 즉시 반영되도록 하기 위함입니다.

    `workspace`는 이번 발언의 작업 공간이며, `tools`는 이번 발언에 실제로 전달된 도구 목록입니다. 스크립트가 포함된
    스킬을 불러올 때 복사할 대상 경로와, 실행 도구의 보유 여부를 여기서 확인합니다.
    """
    args = arguments if isinstance(arguments, dict) else {}
    requested = str(args.get("skill") or args.get("name") or "").strip()
    if not requested:
        return "불러올 스킬 이름(skill)을 지정해 주십시오.", "error"

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
        f"'{requested}' 스킬은 지금 이 에이전트가 쓸 수 없습니다 (존재하지 않는 이름이거나, 비활성화되었거나, "
        f"이 에이전트에게 할당되지 않았습니다)."
    )
    if skills:
        return f"{head} 쓸 수 있는 스킬: {', '.join(s.name for s in skills)}"
    return f"{head} 지금 쓸 수 있는 스킬이 없습니다. 스킬 없이 진행하십시오."


def render_skill(skill: Skill) -> str:
    """`load_skill`의 실행 결과입니다 — 본문과, 필요한 경우 참조할 부속 파일 목록을 반환합니다."""
    lines = [f"# 스킬: {skill.name}", "", skill.body.strip()]
    if skill.files:
        lines += [
            "",
            "---",
            f"부속 파일 (필요 시 `{READ_SKILL_FILE_TOOL}` 도구로 조회하십시오. 스킬 폴더 기준 상대 경로):",
        ]
        lines += [f"- {f}" for f in skill.files]
        if skill.truncated:
            lines.append(f"- … (목록은 최대 {MAX_LISTED_FILES}개까지만 표시됩니다)")
    return "\n".join(lines)


def read_skill_file(skill: Skill, raw_path: str) -> Tuple[str, str]:
    """스킬 폴더 **내부의** 텍스트 파일 하나를 읽습니다. 폴더 외부를 가리키는 경로는 허용되지 않습니다."""
    text = (raw_path or "").strip().replace("\\", "/")
    if not text:
        return "조회할 파일 경로(path)를 지정해 주십시오.", "error"
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return f"'{raw_path}' — 스킬 폴더 기준 상대 경로로 지정해 주십시오 (예: reference.md).", "error"
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if not parts or ".." in parts:
        return f"'{raw_path}' — 스킬 폴더 외부는 조회할 수 없습니다.", "error"
    if any(p.startswith(".") or p in SKIPPED_DIRS for p in parts):
        return f"'{raw_path}' — 숨김 파일과 캐시 폴더는 읽지 않습니다.", "error"

    root = skill.path.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        return f"'{raw_path}' — 스킬 폴더 외부를 가리키는 경로입니다.", "error"
    if not target.is_file():
        listed = ", ".join(skill.files[:20]) or "없음"
        return f"'{text}' 파일이 '{skill.name}' 스킬에 없습니다. 부속 파일: {listed}", "error"

    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(MAX_READ_BYTES)
    except OSError as exc:
        return f"'{text}' 파일을 읽지 못했습니다: {exc}", "error"
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if size > MAX_READ_BYTES:
            # 상한에서 잘린 지점이 멀티바이트 문자 중간일 수 있습니다.
            content = data.decode("utf-8-sig", errors="ignore")
        else:
            return (
                f"'{text}' 은 글 파일이 아니어서 읽을 수 없습니다 (이 도구는 UTF-8 글 문서만 읽습니다).",
                "error",
            )
    if "\x00" in content:
        return f"'{text}' 은 글 파일이 아니어서 읽을 수 없습니다.", "error"
    if size > MAX_READ_BYTES:
        content += f"\n\n... [파일 용량이 초과되어 앞 {MAX_READ_BYTES:,}바이트만 읽었습니다 (전체 {size:,}바이트)]"
    return clip_tool_output(content), "success"


# ---------------------------------------------------------------------------
# 스크립트 — 작업 공간에 복사해 샌드박스로 실행
# ---------------------------------------------------------------------------


def _tool_by_tail(tools: Optional[List[Dict[str, Any]]], tail: str) -> Optional[str]:
    """이번 발언에 제공된 도구 중 이름이 `tail`로 끝나는 도구(`sandbox__run_python_file` 등)를 찾습니다."""
    for tool in tools or ():
        name = str((tool.get("function") or {}).get("name") or "")
        if name == tail or name.endswith(f"__{tail}"):
            return name
    return None


async def _scripts_section(
    skill: Skill, workspace: Optional[Path], tools: Optional[List[Dict[str, Any]]],
) -> str:
    """스크립트가 포함된 스킬을 불러올 때 본문 뒤에 추가되는 안내문입니다. 필요한 경우 작업 공간에 먼저 복사합니다.

    실행 도구가 없는 에이전트에게는 복사하지 않습니다. 복사하는 주된 목적이 실행이며, 사용하지도 않을
    파일로 작업 공간을 어지럽히지 않기 위함입니다. 복사에 실패하더라도 본문 지침은 그대로 반환합니다 —
    지침 내용만으로도 충분히 유용하기 때문입니다.
    """
    run_tool = _tool_by_tail(tools, RUN_TOOL_TAIL)
    if run_tool is None:
        return (
            f"---\n이 스킬에는 스크립트가 포함되어 있으나, 이 에이전트에게는 스크립트를 실행할 도구"
            f"(`{RUN_TOOL_TAIL}`)가 없어 작업 공간에 복사하지 않았습니다. 지침과 부속 문서만 참조하시거나, "
            f"실행 도구를 보유한 에이전트에게 실행을 위임하십시오."
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
        f"공간의 파일로 주고받습니다. 복사본은 고치지 마십시오. 스킬을 불러올 때마다 원본으로 되돌아갑니다.",
    ]
    return "\n".join(lines)


# 복사 대상 폴더 → 잠금 매핑입니다. 동일한 작업 공간을 사용하는 여러 발언이 동시에 같은 스킬을 불러와도
# 파일 복사가 충돌하지 않도록 방지합니다. 복사는 별도 스레드에서 실행되므로 threading.Lock을 사용합니다.
_stage_locks: Dict[str, threading.Lock] = {}
_stage_locks_guard = threading.Lock()


def _stage_lock(target: Path) -> threading.Lock:
    key = os.path.normcase(str(target))
    with _stage_locks_guard:
        return _stage_locks.setdefault(key, threading.Lock())


def _stage_sources(folder: Path) -> List[Tuple[str, Path, os.stat_result]]:
    """복사할 파일 목록(SKILL.md 포함)을 반환합니다. 부속 파일 목록과 동일한 규칙으로 제외하며, 상한 초과 시 `SkillError`를 발생시킵니다."""
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
    """스킬 폴더를 작업 공간의 `.mado/skills/<이름>/` 디렉터리로 복사하고, 해당 위치를 작업 공간 기준
    상대 경로로 반환합니다.

    **변경된 파일만** 다시 복사합니다 (크기와 수정 시각이 원본과 다른 경우). 따라서 스킬 원본을 수정하면 다음
    호출 시 새 스크립트가 배치되며, 에이전트가 복사본을 임의로 수정하더라도 원본 내용으로 복원됩니다. 원본에
    없는 파일은 삭제하지 않습니다 — 스크립트 실행 과정에서 자체 생성된 결과물 파일일 수 있기 때문입니다.
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
