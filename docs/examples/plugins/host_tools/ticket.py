"""예제 호스트 도구: 현재 대화 세션의 작업 공간에 있는 `tickets.md` 파일에 티켓 항목을 추가합니다.

위험 등급을 명시하지 않아 '미분류 도구'로 취급됩니다. 기본 모드에서는 호출 시 승인 카드가 표시되며,
읽기 전용 모드에서는 도구 목록에서 제외되고, 비대면(헤드리스) 실행 환경에서는 호출이 거부됩니다.
보안 정책 규칙은 `mcp(ticket/create)` 형태로 지정합니다.

도구 게이트 판정 엔진은 플러그인 내부 구현을 검사할 수 없습니다. 따라서 파일 쓰기 대상을 LLM의 인자로
받지 않고, 시스템이 주입한 작업 공간 내부의 고정된 파일 하나로 제한하여 안전성을 확보합니다.
참조 가이드: docs/user_manual/03-core/11-plugins.md
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
        # 줄바꿈 문자를 정제하여 모델이 단일 호출로 여러 줄을 임의 삽입하지 못하도록 방지합니다.
        title = " ".join(str(call.arguments.get("title") or "").split())
        if not title:
            return "티켓 제목(`title`)을 적어 주세요.", "error"
        if call.workspace is None:
            return "작업 공간을 알 수 없어 티켓을 적지 못했습니다.", "error"
        line = f"- [ ] {title} ({getattr(call.agent, 'name', '')})"
        # `run` 메서드는 메인 이벤트 루프에서 실행됩니다. 파일 I/O는 백그라운드 스레드로 오프로드하여 UI 블로킹을 방지합니다.
        await asyncio.to_thread(_append_line, Path(call.workspace) / TICKET_FILE, line)
        return f"`{TICKET_FILE}` 에 적었습니다: {title}", "success"
