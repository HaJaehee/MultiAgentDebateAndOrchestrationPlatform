"""예제 호스트 도구: 이번 대화의 작업 공간에 있는 `tickets.md` 에 티켓을 한 줄 적습니다.

위험 등급을 밝히지 않아 '등급을 모르는 도구' 로 판정됩니다. 기본 모드에서는 부를 때 승인 카드가 뜨고, 읽기 전용
모드에서는 목록에서 빠지며, 물을 사람이 없는 헤드리스 실행에서는 거부됩니다. 규칙은 `mcp(ticket/create)` 로 겁니다.

판정은 플러그인 안쪽을 보지 못합니다. 그래서 쓰는 곳을 모델의 인자로 받지 않고, 앱이 알려 준 작업 공간의 고정된
파일 하나로 제한합니다. 설명: docs/user_manual/03-core/11-plugins.md
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.agents.host_tools import HostToolCall, HostToolProvider

TICKET_FILE = "tickets.md"


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class TicketTools(HostToolProvider):
    prefix = "ticket"
    tails = ("create",)
    label = "티켓 도구"
    summary = "작업 공간의 tickets.md 에 티켓을 적습니다 (등급을 밝히지 않아 승인 카드가 뜹니다)"

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        return [self.definition("create", "작업 공간의 tickets.md 에 작업 티켓을 한 줄 적습니다.", {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "티켓 제목"}},
            "required": ["title"],
        })]

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        # 줄바꿈을 지워 모델이 파일에 여러 줄을 끼워 넣지 못하게 합니다.
        title = " ".join(str(call.arguments.get("title") or "").split())
        if not title:
            return "티켓 제목(`title`)을 적어 주세요.", "error"
        if call.workspace is None:
            return "작업 공간을 알 수 없어 티켓을 적지 못했습니다.", "error"
        line = f"- [ ] {title} ({getattr(call.agent, 'name', '')})"
        # `run` 은 이벤트 루프에서 돕니다. 파일을 다루는 일은 다른 스레드로 넘겨 토론과 화면을 막지 않습니다.
        await asyncio.to_thread(_append_line, Path(call.workspace) / TICKET_FILE, line)
        return f"`{TICKET_FILE}` 에 적었습니다: {title}", "success"
