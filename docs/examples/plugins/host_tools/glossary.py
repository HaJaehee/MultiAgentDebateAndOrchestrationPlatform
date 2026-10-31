"""예제 호스트 도구: 사내 용어집에서 뜻을 찾습니다. 위험 등급을 밝혀, 읽기 전용 모드에서도 묻지 않고 실행됩니다.

이 파일을 프로젝트 루트의 `host_tools/` 폴더에 복사하고 서버를 다시 시작한 뒤, 에이전트 카드의 `도구` 창에서
`glossary` 를 켭니다. 설명: docs/user_manual/03-core/11-plugins.md
"""

from typing import Any, Dict, List, Optional, Tuple

from app.agents.host_tools import HostToolCall, HostToolProvider

GLOSSARY = {
    "ADR": "아키텍처 결정 기록. 왜 그렇게 정했는지를 남기는 문서입니다.",
    "MADO": "Multi-Agent Debate & Orchestration Platform. 여러 에이전트의 토론을 중재하는 플랫폼입니다.",
}


class GlossaryTools(HostToolProvider):
    prefix = "glossary"                         # 도구 이름의 앞부분. MCP 서버 이름으로 쓸 수 없게 예약됩니다.
    tails = ("lookup",)                         # 전체 이름은 glossary__lookup 입니다.
    label = "사내 용어집 도구"                    # 같은 이름의 MCP 서버를 등록하려 할 때 안내에 나옵니다.
    summary = "사내 용어집에서 용어의 뜻을 찾습니다"  # 도구 할당 창에 나옵니다.
    risk = "read"                               # 파일도 네트워크도 건드리지 않고 값만 읽습니다.

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        """이번 발언에 줄 도구 정의. `allowed_mcp_servers` 에 `glossary` 가 있는 에이전트에게만 불립니다."""
        return [self.definition("lookup", "사내 용어집에서 용어의 뜻을 찾습니다.", {
            "type": "object",
            "properties": {"term": {"type": "string", "description": "찾을 용어"}},
            "required": ["term"],
        })]

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """시스템 프롬프트에 붙는 글. 이 도구를 가진 발언에만 붙도록 `tools` 를 확인합니다."""
        names = {(tool.get("function") or {}).get("name") for tool in tools or ()}
        if "glossary__lookup" not in names:
            return None
        return "[용어집] 뜻이 분명하지 않은 사내 용어는 추측하지 말고 `glossary__lookup` 으로 찾으세요."

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        """호출 하나를 처리합니다. 실패도 예외 대신 `(설명, "error")` 로 돌려주면 모델이 읽고 고칩니다."""
        term = str(call.arguments.get("term") or "").strip()
        if not term:
            return "찾을 용어(`term`)를 적어 주세요.", "error"
        meaning = GLOSSARY.get(term.upper())
        if meaning is None:
            return f"'{term}' 은 용어집에 없습니다. 있는 용어: {', '.join(sorted(GLOSSARY))}", "error"
        return f"{term.upper()}: {meaning}", "success"
