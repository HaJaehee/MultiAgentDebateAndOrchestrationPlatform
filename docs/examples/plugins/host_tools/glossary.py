"""예제 호스트 도구: 사내 용어집에서 용어의 정의를 조회합니다.

위험 등급(risk="read")을 명시하여 읽기 전용 보안 모드에서도 사용자 확인 절차 없이 자동 실행됩니다.
이 파일을 프로젝트 루트의 `host_tools/` 디렉터리에 복사하고 서버를 재시작한 뒤,
에이전트 카드의 `도구` 패널에서 `glossary`를 활성화합니다.
참조 가이드: docs/user_manual/03-core/11-plugins.md
"""

from typing import Any, Dict, List, Optional, Tuple

from app.agents.host_tools import HostToolCall, HostToolProvider

GLOSSARY = {
    "ADR": "아키텍처 결정 기록. 왜 그렇게 정했는지를 남기는 문서입니다.",
    "MADO": "Multi-Agent Debate & Orchestration Platform. 여러 에이전트의 토론을 중재하는 플랫폼입니다.",
}


class GlossaryTools(HostToolProvider):
    prefix = "glossary"                         # 도구 식별자 접두사. MCP 서버 이름으로 등록되지 않도록 예약됩니다.
    tails = ("lookup",)                         # 전체 도구 이름은 glossary__lookup 입니다.
    label = "사내 용어집 도구"                    # 동일한 이름의 MCP 서버 충돌 등록 시 안내 메시지에 표시됩니다.
    summary = "사내 용어집에서 용어의 뜻을 찾습니다"  # 에이전트 도구 설정 패널에 표시되는 요약 설명입니다.
    risk = "read"                               # 파일이나 네트워크 변경 없이 인메모리 값만 안전하게 조회합니다.

    def tools_for(self, agent: Any) -> List[Dict[str, Any]]:
        """이번 발언에 제공할 도구 스키마 정의. `allowed_mcp_servers`에 `glossary`가 할당된 에이전트에게만 제공됩니다."""
        return [self.definition("lookup", "사내 용어집에서 용어의 뜻을 찾습니다.", {
            "type": "object",
            "properties": {"term": {"type": "string", "description": "찾을 용어"}},
            "required": ["term"],
        })]

    def guidance(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """시스템 프롬프트에 주입될 지침. 본 도구가 활성화된 발언 차례에만 주입되도록 `tools` 목록을 검사합니다."""
        names = {(tool.get("function") or {}).get("name") for tool in tools or ()}
        if "glossary__lookup" not in names:
            return None
        return "[용어집] 뜻이 분명하지 않은 사내 용어는 추측하지 말고 `glossary__lookup` 으로 찾으세요."

    async def run(self, call: HostToolCall) -> Tuple[str, str]:
        """도구 호출 요청을 처리합니다. 예외를 직접 발생시키지 않고 `(안내, "error")` 튜플로 반환하여 모델이 자가 교정하도록 합니다."""
        term = str(call.arguments.get("term") or "").strip()
        if not term:
            return "찾을 용어(`term`)를 적어 주세요.", "error"
        meaning = GLOSSARY.get(term.upper())
        if meaning is None:
            return f"'{term}' 은 용어집에 없습니다. 있는 용어: {', '.join(sorted(GLOSSARY))}", "error"
        return f"{term.upper()}: {meaning}", "success"
