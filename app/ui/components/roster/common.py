"""로스터 패널의 상수와 순수 함수 (드래그 스크립트, 진영 이름, 내장 도구 peers 표시 규칙)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agents.base import Agent
from app.agents.consultation import PEERS_TOOL_TOOLTIP
from app.config import ORCHESTRATOR_KEY, PEERS_TOOL_PREFIX


# 미리보기 칩의 색. 디베이트에서만 진영별로 갈라 칠합니다 — 다른 전략에서는
# 진영이 읽히지 않으므로 색을 주면 없는 규칙을 있다고 말하는 셈입니다.
STANCE_PREVIEW_CLASSES = {
    "proponent": "bg-emerald-500/15 text-emerald-300",
    "critic": "bg-rose-500/15 text-rose-300",
    "neutral": "bg-slate-700/60 text-slate-300",
}

# 진행 토스트가 스스로 사라지는 시간(초). 서버 재기동은 보통 2~6초면 끝나므로
# 그 뒤에는 결과 토스트가 대신 말해 줍니다. 사라지는 것을 보장하는 것은 이 값이고,
# 작업이 끝났을 때 부르는 dismiss() 는 더 일찍 치우기 위한 것입니다.
PROGRESS_TOAST_TIMEOUT = 8

# 설정 패널의 MCP 서버 칩 공통 모양. 서버 칩의 높이는 켜기 스위치·삭제 버튼이 정하는데(34px),
# 그 둘이 없는 내장 도구 peers 칩은 낮아져 줄이 들쭉날쭉했습니다. 최소 높이를 같게 둡니다.
MCP_CHIP_CLASSES = "flex items-center gap-1 py-1 rounded-md bg-slate-800/70 border min-h-[34px]"

# 새 에이전트 폼에서 정수로 적어야 하는 항목. `ui.number` 는 무엇을 넣든 float 를
# 돌려주므로, 그대로 쓰면 conf.json 에 `max_tokens = 4096.0` 이 적힙니다.
INT_AGENT_FIELDS = ("max_tokens", "max_context_window", "num_retries", "max_tool_iterations")

# 카드 드래그는 **브라우저 안에서** 처리합니다.
#
# 예전에는 `dragover` 에 파이썬 핸들러를 달았습니다. NiceGUI 는 파이썬 핸들러가
# 있으면 그 이벤트를 서버로 보내는데, `dragover` 는 커서가 움직이는 내내 초당 수십
# 번 발생합니다. 카드 위를 지나가기만 해도 웹소켓 메시지가 쏟아져 드래그가 끊겼습니다.
# `js_handler` 만 주면 서버로 아무것도 보내지 않습니다.
#
# 놓을 자리도 여기서 정합니다. 커서가 카드의 왼쪽 절반이면 그 앞, 오른쪽 절반이면
# 그 뒤입니다. 이걸 커서로 정하지 않으면 "오른쪽으로 한 칸" 과 "맨 뒤로" 를 표현할
# 방법이 없습니다.
_DRAG_CLASSES = "'agent-dragging', 'agent-drop-before', 'agent-drop-after'"

JS_DRAG_START = (
    "(e) => { e.dataTransfer.effectAllowed = 'move';"
    " e.currentTarget.classList.add('agent-dragging'); emit(); }"
)
JS_DRAG_OVER = (
    "(e) => { e.preventDefault();"
    " const el = e.currentTarget;"
    " if (el.classList.contains('agent-dragging')) return;"
    " const r = el.getBoundingClientRect();"
    " const after = e.clientX > r.left + r.width / 2;"
    " el.classList.toggle('agent-drop-before', !after);"
    " el.classList.toggle('agent-drop-after', after); }"
)
JS_DRAG_LEAVE = (
    "(e) => { e.currentTarget.classList.remove('agent-drop-before', 'agent-drop-after'); }"
)
JS_DROP = (
    "(e) => { e.preventDefault();"
    " const el = e.currentTarget;"
    " el.classList.remove('agent-drop-before', 'agent-drop-after');"
    " const r = el.getBoundingClientRect();"
    " emit({after: e.clientX > r.left + r.width / 2}); }"
)
JS_DRAG_END = (
    "(e) => { document.querySelectorAll("
    "'.agent-dragging, .agent-drop-before, .agent-drop-after')"
    f".forEach(n => n.classList.remove({_DRAG_CLASSES})); emit(); }}"
)


def dropped_after(event: Any) -> bool:
    """드롭 지점이 대상 카드의 오른쪽 절반이었는지 (`JS_DROP` 이 실어 보낸 값)."""
    args = getattr(event, "args", None)
    if isinstance(args, (list, tuple)):
        args = args[0] if args else None
    return bool(args.get("after")) if isinstance(args, dict) else False


# 디베이트 전략의 진영. conf.json 에는 영문 키가 들어가고 화면에는 이 이름이 뜹니다.
STANCE_LABELS = {"proponent": "제안자", "critic": "비판자", "neutral": "중립"}
STANCE_COLORS = {"proponent": "teal-8", "critic": "amber-9", "neutral": "slate-7"}
FLOAT_AGENT_FIELDS = ("temperature", "top_p", "timeout")


# --- 내장 도구 peers (동료 전문가 자문, app/agents/consultation.py) -------------------
#
# MCP 서버가 아니지만 켜는 법이 `allowed_mcp_servers` 에 "peers" 를 넣는 것이라, 화면에서도
# MCP 서버 칸과 도구 할당에 서버처럼 함께 보입니다. conf.json 의 mcp_servers 에는 없으므로,
# 거기 없는 이름을 "설정에 없음" 으로 표시하는 규칙에서 빼야 합니다.


def peers_users(agents: List[Agent]) -> List[str]:
    """peers 를 켠 전문가의 이름. 오케스트레이터는 켜 두어도 쓰지 않으므로 뺍니다."""
    return [
        a.name for a in agents
        if a.key != ORCHESTRATOR_KEY and PEERS_TOOL_PREFIX in (a.allowed_mcp_servers or [])
    ]


def peers_chip_tooltip(agents: List[Agent]) -> str:
    """MCP 서버 칸의 peers 칩에 다는 설명."""
    users = peers_users(agents)
    return f"{PEERS_TOOL_TOOLTIP}\n이 대화에서 켠 전문가: {', '.join(users) if users else '없음'}"


def unknown_tool_servers(allowed: List[str], configured: Dict[str, Any]) -> List[str]:
    """도구 할당 창에서 "설정에 없음" 으로 보일 이름. 내장 도구(peers)는 늘 있는 항목이라 빠집니다."""
    return [name for name in allowed if name not in configured and name != PEERS_TOOL_PREFIX]


def peer_candidates(agents: List[Agent], asker_key: str) -> List[Agent]:
    """peers 로 물을 상대로 고를 수 있는 에이전트 — 토론 참여 여부와 상관없이 자기와 오케스트레이터만 뺍니다.

    conf.json 에서 꺼 둔 에이전트는 풀에 없어 여기에도 없습니다 (켜면 나타납니다).
    """
    return [a for a in agents if a.key not in (asker_key, ORCHESTRATOR_KEY)]


def peer_selection_summary(targets: Optional[List[str]], tool_limit: int) -> str:
    """도구 할당 창의 peers 줄에 붙는 한 줄 요약."""
    if targets is None:
        who = "참여 전문가 전원"
    elif targets:
        who = f"고른 {len(targets)}명"
    else:
        who = "고른 상대 없음 (도구가 주어지지 않음)"
    return f"{who} · 도구 {tool_limit}회" if tool_limit else f"{who} · 도구 없이 답함"


