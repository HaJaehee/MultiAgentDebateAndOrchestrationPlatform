"""쉬운 화면의 내용 — 시연 에이전트·예시 과제·예제 작업 디렉터리와 방문자 한도.

도구·스킬의 쉬운 말 설명은 전문가 화면도 쓰므로 `app/agents/catalog.py` 에 있습니다.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.config import resolve_workspace_dir

# 방문자 1인이 생성 가능한 최대 '내 에이전트' 수 및 단일 작업에 동시 투입 가능한 최대 에이전트 수.
MAX_GUEST_AGENTS = 20
MAX_RUN_AGENTS = 4


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
