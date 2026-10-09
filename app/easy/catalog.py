"""쉬운 말 카탈로그 — 도구와 스킬을 비엔지니어 친화적 용어로 설명하고, 시연 에이전트·예시 과제·예제 작업 디렉터리를 정의합니다.

설계 도우미(`builder.py`)와 UI 화면이 동일한 카탈로그를 참조합니다. 도우미가 선택할 수 있는 도구 ID와 화면의
체크박스 항목이 일치해야, 도우미가 임의로 없는 도구를 생성하더라도 안전하게 필터링할 수 있습니다.
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

# 체험 방문자에게 허용되는 도구 서버 목록. 방문자 세션은 읽기 전용 모드로 실행되지만, conf.json의
# `tool_security.allow` 허용 규칙이 모드보다 우선 평가됩니다 (`app/mcp/policy.py`의 `evaluate`).
# 따라서 해당 규칙과 무관하게 보안을 보장할 수 있도록 코드 실행 도구(sandbox)는 아예 연결 대상에서 제외합니다.
GUEST_SERVERS: Tuple[str, ...] = ("filesystem",)

# 방문자 1인이 생성 가능한 최대 '내 에이전트' 수 및 단일 작업에 동시 투입 가능한 최대 에이전트 수.
MAX_GUEST_AGENTS = 20
MAX_RUN_AGENTS = 4


@dataclass(frozen=True)
class Option:
    """선택 옵션 항목 — 도구 서버 또는 스킬."""

    id: str
    label: str
    description: str


# 기본 제공 도구 서버들의 직관적인 명칭과 한 줄 설명. 정의되지 않은 서버는 conf.json의 `// <서버>` 설명을 활용합니다.
SERVER_GUIDE: Dict[str, Tuple[str, str]] = {
    "filesystem": ("파일 열어 보기·만들기", "작업 폴더의 문서를 열어 읽고, 새 파일을 만들거나 고칩니다."),
    "sandbox": ("계산·파이썬 실행", "파이썬 코드를 직접 실행해 계산하고 결과를 확인합니다."),
    "memory": ("기억 노트", "알게 된 사실을 노트에 적어 두고, 나중에 다시 찾아봅니다."),
    "git": ("변경 이력 남기기", "파일을 언제 어떻게 바꿨는지 기록해 되돌릴 수 있게 합니다."),
    "fetch": ("웹 페이지 읽기", "인터넷 주소의 내용을 가져와 읽습니다."),
    "sequential_thinking": ("단계별로 생각하기", "어려운 문제를 단계로 나눠 차근차근 생각합니다."),
    "pair_slide": ("발표 자료 만들기", "슬라이드(PPT) 파일을 만듭니다."),
}

# 도구 이름(서버 접두사 제외) → 직관적인 행동 표현 매핑. 화면의 '행동' 단계에 표시됩니다.
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
# 행동의 대상으로 표시할 인자 키 목록. 앞선 순서부터 우선 탐색합니다.
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
    """conf.json의 `mcp_servers` 항목에 정의된 `// <서버>` 주석 설명. 읽지 못하면 빈 딕셔너리를 반환합니다."""
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
    """현재 활성화된 도구 서버 목록. 방문자에게는 `GUEST_SERVERS`만 제공합니다."""
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
    """사용 가능한 스킬 목록 (비활성화되었거나 설정 오류가 있는 스킬은 제외)."""
    try:
        skills = scan_skills()
    except Exception as exc:  # noqa: BLE001 - 스킬이 없어도 에이전트는 만들 수 있습니다
        logger.warning("Could not scan skills for the easy pages: %s", exc)
        return []
    return [Option(s.name, s.title or s.name, s.description) for s in skills if s.usable]


# ---------------------------------------------------------------------------
# 시연 에이전트와 예시 과제
# ---------------------------------------------------------------------------

# 웰컴 화면의 "일하는 모습 보기"에서 사용하는 시연 에이전트. conf.json에는 저장되지 않으며 세션 스냅샷으로만 고정됩니다.
DEMO_AGENT: Dict[str, Any] = {
    "key": "easy_demo",
    "name": "자료 탐색가",
    "role": "작업 공간의 자료를 직접 열어 보고, 확인된 사실에 기반해서만 답변합니다",
    "system_prompt": "\n".join([
        "당신은 '자료 탐색가'입니다. 질문을 받으면 기억이나 추측으로 답하지 않고, 작업 폴더의 파일을 직접 열어 확인한 뒤 답변합니다.",
        "",
        "작업 절차:",
        "1. 먼저 작업 폴더에 어떤 파일이 있는지 살펴봅니다.",
        "2. 질문과 관련 있는 파일을 열어서 내용을 확인합니다.",
        "3. 읽은 내용으로 필요한 계산이나 정리를 수행합니다. 수치는 직접 계산하여 확인합니다.",
        "4. 답변에는 근거(어느 파일의 어떤 내용인지)를 명확히 기재합니다.",
        "",
        "질문의 의도가 두 가지로 해석될 수 있는 경우(예: '가장 많이 팔린' = 수량 기준 또는 매출액 기준) 두 기준 모두 확인하여 안내합니다.",
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

# 모든 사용자가 실행할 수 있는 예시 과제 (예제 디렉터리의 파일을 읽기 전용으로 탐색).
EXAMPLE_MISSIONS: Tuple[str, ...] = (
    DEMO_MISSION,
    "회의 메모(meeting_notes.md)에서 결정 사항과 할 일(담당자·기한)을 표로 정리해 주세요.",
    "고객 문의(customer_inquiries.txt)를 유형별로 분류하고, 가장 접수가 많은 유형과 대응 방안을 알려 주세요.",
)

# 소유자 전용 예시 과제 — 파일 생성/수정이나 코드 실행 등 읽기 전용 모드에서는 제한되는 작업입니다.
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
    """과제를 수행할 작업 디렉터리 경로를 반환합니다. 폴더가 없으면 예제 파일을 복사해 생성합니다.

    원본(`examples/`)은 앱 패키지에 포함되어 있으므로, 에이전트가 원본을 변경하지 않도록 복사본 디렉터리에서 실행합니다.
    소유자와 방문자는 작업 디렉터리를 분리하여 사용합니다 — 소유자 세션에서 생성/수정한 파일이 방문자에게 노출되지 않도록 격리하기 위함입니다.
    """
    target = resolve_workspace_dir() / ("easy-guest" if guest else "easy")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(EXAMPLES_DIR, target)
    return target
