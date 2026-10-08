"""쉬운 말 카탈로그 — 도구와 스킬을 비엔지니어의 말로 옮기고, 시연 에이전트·예시 과제·예제 폴더를 둡니다.

도우미(`builder.py`)와 화면이 같은 카탈로그를 봅니다. 도우미가 고를 수 있는 도구 id 와 화면의
체크박스가 같아야, 도우미가 없는 도구를 지어내도 거를 수 있습니다.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.agents.skills import scan_skills
from app.config import (
    DEFAULT_CONFIG_PATH,
    RootConfig,
    active_config_path,
    join_text_lines,
    read_conf_file,
    resolve_workspace_dir,
)

logger = logging.getLogger(__name__)

# 체험 방문자에게 붙일 수 있는 도구 서버. 방문자 대화는 읽기 전용 모드로 돌지만, conf.json 의
# `tool_security.allow` 규칙은 모드보다 먼저 판정됩니다 (`app/mcp/policy.py` `evaluate`).
# 실행 도구(샌드박스)를 아예 붙이지 않는 것이 그 규칙과 상관없이 지켜지는 경계입니다.
GUEST_SERVERS: Tuple[str, ...] = ("filesystem",)

# 방문자 한 사람이 둘 수 있는 '내 에이전트' 수와, 한 번에 일을 맡길 수 있는 에이전트 수.
MAX_GUEST_AGENTS = 20
MAX_RUN_AGENTS = 4


@dataclass(frozen=True)
class Option:
    """체크박스 하나 — 도구 서버 또는 스킬."""

    id: str
    label: str
    description: str


# 기본으로 싣는 서버들의 쉬운 이름과 한 줄 설명. 모르는 서버는 conf.json 의 `// <서버>` 설명을 씁니다.
SERVER_GUIDE: Dict[str, Tuple[str, str]] = {
    "filesystem": ("파일 열어 보기·만들기", "작업 폴더의 문서를 열어 읽고, 새 파일을 만들거나 고칩니다."),
    "sandbox": ("계산·파이썬 실행", "파이썬 코드를 직접 실행해 계산하고 결과를 확인합니다."),
    "memory": ("기억 노트", "알게 된 사실을 노트에 적어 두고, 나중에 다시 찾아봅니다."),
    "git": ("변경 이력 남기기", "파일을 언제 어떻게 바꿨는지 기록해 되돌릴 수 있게 합니다."),
    "fetch": ("웹 페이지 읽기", "인터넷 주소의 내용을 가져와 읽습니다."),
    "sequential_thinking": ("단계별로 생각하기", "어려운 문제를 단계로 나눠 차근차근 생각합니다."),
    "pair_slide": ("발표 자료 만들기", "슬라이드(PPT) 파일을 만듭니다."),
}

# 도구 이름(서버 접두사를 뗀 것) → 사람이 읽을 행동. 화면의 '행동' 단계에 씁니다.
_ACTIONS: Dict[str, str] = {
    "read_text_file": "파일 읽기",
    "read_file": "파일 읽기",
    "read_multiple_files": "여러 파일 읽기",
    "read_media_file": "그림·미디어 파일 읽기",
    "list_directory": "폴더 살펴보기",
    "list_directory_with_sizes": "폴더 살펴보기",
    "directory_tree": "폴더 구조 살펴보기",
    "search_files": "파일 찾기",
    "get_file_info": "파일 정보 확인",
    "list_allowed_directories": "쓸 수 있는 폴더 확인",
    "write_file": "파일 쓰기",
    "write_text_file": "파일 쓰기",
    "write_workspace_file": "파일 쓰기",
    "append_workspace_file": "파일 뒤에 덧붙이기",
    "edit_file": "파일 고치기",
    "create_directory": "폴더 만들기",
    "move_file": "파일 옮기기",
    "execute_python_code": "파이썬 코드 실행",
    "run_python_file": "파이썬 파일 실행",
    "create_entities": "기억 노트에 적기",
    "add_observations": "기억 노트에 적기",
    "create_relations": "기억 노트에 연결 적기",
    "search_nodes": "기억 노트 찾아보기",
    "open_nodes": "기억 노트 펼쳐 보기",
    "read_graph": "기억 노트 전체 보기",
    "fetch": "웹 페이지 읽기",
    "sequentialthinking": "단계별로 생각하기",
    "load_skill": "업무 매뉴얼(스킬) 펼쳐 보기",
    "read_skill_file": "업무 매뉴얼 부록 읽기",
}
# 행동의 대상으로 보여 줄 인자. 앞의 것부터 찾습니다.
_TARGET_ARGS = ("path", "file_path", "url", "name", "query", "paths")


def tool_label(tool_name: str, arguments: Any = None) -> str:
    """`filesystem__read_text_file` + `{"path": "a.csv"}` → `파일 읽기: a.csv`."""
    tail = (tool_name or "").split("__", 1)[-1]
    if tail in _ACTIONS:
        action = _ACTIONS[tail]
    elif tail.startswith("git_"):
        action = "변경 이력 다루기"
    else:
        action = f"도구 사용: {tail or '알 수 없는 도구'}"
    target = ""
    if isinstance(arguments, Mapping):
        for key in _TARGET_ARGS:
            value = arguments.get(key)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value[:3])
            if isinstance(value, str) and value.strip():
                target = value.strip()
                break
    if len(target) > 60:
        target = target[:57] + "..."
    return f"{action}: {target}" if target else action


# ---------------------------------------------------------------------------
# 고를 수 있는 도구와 스킬
# ---------------------------------------------------------------------------


def _server_comments() -> Dict[str, str]:
    """conf.json `mcp_servers` 의 `// <서버>` 설명. 읽지 못하면 비웁니다."""
    path = active_config_path() or Path(DEFAULT_CONFIG_PATH)
    try:
        section = read_conf_file(path).get("mcp_servers") or {}
    except Exception:  # noqa: BLE001 - 설명은 없어도 됩니다
        return {}
    comments: Dict[str, str] = {}
    for key, value in section.items():
        if isinstance(key, str) and key.startswith("//"):
            text = join_text_lines(value)
            if isinstance(text, str):
                comments[key[2:].strip()] = text.strip().splitlines()[0] if text.strip() else ""
    return comments


def server_options(cfg: RootConfig, *, guest: bool) -> List[Option]:
    """지금 켜져 있는 도구 서버. 방문자에게는 `GUEST_SERVERS` 만."""
    comments: Optional[Dict[str, str]] = None
    options: List[Option] = []
    for name, server in cfg.mcp_servers.items():
        if not server.enabled or (guest and name not in GUEST_SERVERS):
            continue
        if name in SERVER_GUIDE:
            label, description = SERVER_GUIDE[name]
        else:
            if comments is None:
                comments = _server_comments()
            label, description = name, comments.get(name, "")
        options.append(Option(name, label, description))
    return options


def skill_options() -> List[Option]:
    """쓸 수 있는 스킬 (꺼졌거나 깨진 것은 뺍니다)."""
    try:
        skills = scan_skills()
    except Exception as exc:  # noqa: BLE001 - 스킬이 없어도 에이전트는 만들 수 있습니다
        logger.warning("Could not scan skills for the easy pages: %s", exc)
        return []
    return [Option(s.name, s.title or s.name, s.description) for s in skills if s.usable]


# ---------------------------------------------------------------------------
# 시연 에이전트와 예시 과제
# ---------------------------------------------------------------------------

# 웰컴 화면의 "일하는 모습 보기" 가 쓰는 에이전트. conf.json 에는 없고, 대화에만 고정됩니다.
DEMO_AGENT: Dict[str, Any] = {
    "key": "easy_demo",
    "name": "자료 탐색가",
    "role": "작업 폴더의 자료를 직접 열어 보고, 확인한 사실로만 답합니다",
    "system_prompt": "\n".join([
        "당신은 '자료 탐색가'입니다. 질문을 받으면 기억이나 추측으로 답하지 않고, 작업 폴더의 파일을 직접 열어 확인한 뒤 답합니다.",
        "",
        "일하는 순서:",
        "1. 먼저 작업 폴더에 어떤 파일이 있는지 살펴봅니다.",
        "2. 질문과 관련 있는 파일을 열어 읽습니다.",
        "3. 읽은 내용으로 필요한 계산이나 정리를 합니다. 숫자는 직접 더해 확인합니다.",
        "4. 답에는 근거(어느 파일의 어떤 내용인지)를 함께 적습니다.",
        "",
        "질문의 뜻이 두 가지로 읽히면(예: '많이 팔린' = 수량 또는 매출액) 둘 다 확인해 알려 줍니다.",
    ]),
    "allowed_mcp_servers": ["filesystem"],
    "allowed_skills": [],
    "card_color": "#009688",
    "icon": "travel_explore",
}

DEMO_MISSION = (
    "작업 폴더의 판매 기록(sales_2026q3.csv)을 직접 열어 보고, "
    "이번 분기에 가장 많이 팔린 제품이 무엇인지 근거와 함께 알려 주세요."
)

# 누구나 쓸 수 있는 예시 과제 (예제 폴더의 파일을 읽기만 합니다).
EXAMPLE_MISSIONS: Tuple[str, ...] = (
    DEMO_MISSION,
    "회의 메모(meeting_notes.md)에서 결정된 것과 할 일(담당·기한)만 표로 추려 주세요.",
    "고객 문의(customer_inquiries.txt)를 유형별로 나누고, 가장 많은 유형과 대응 방안을 알려 주세요.",
)

# 주인만 보는 예시 과제 — 파일 쓰기나 코드 실행처럼 읽기 전용에서는 거부되는 일입니다.
OWNER_MISSIONS: Tuple[str, ...] = (
    "판매 기록을 파이썬으로 집계해 제품별 수량·매출 합계 표를 만들고 report.md 파일로 저장해 주세요.",
)


# ---------------------------------------------------------------------------
# 예제 작업 폴더
# ---------------------------------------------------------------------------

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"


def example_files() -> List[str]:
    return sorted(p.name for p in EXAMPLES_DIR.iterdir() if p.is_file()) if EXAMPLES_DIR.is_dir() else []


def easy_workspace(guest: bool) -> Path:
    """일을 맡기는 대화의 작업 폴더. 없으면 예제 파일을 복사해 만듭니다.

    원본(`examples/`)은 앱 소스와 함께 다니므로 에이전트가 직접 쓰지 않게 복사본에서 돌립니다.
    주인과 방문자는 폴더를 따로 씁니다 — 주인 대화가 쓴 파일이 방문자에게 보이지 않게 하려는 것입니다.
    """
    target = resolve_workspace_dir() / ("easy-guest" if guest else "easy")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(EXAMPLES_DIR, target)
    return target
