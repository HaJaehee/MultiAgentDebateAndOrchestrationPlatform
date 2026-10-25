"""토론이 화면에 알리는 이벤트의 이름표.

엔진·단계 실행기·도구 게이트·LLM 호출부가 `{"type": ..., ...}` 사전을 `on_event` 로 보내고,
실행기(`TurnRun`)가 스냅샷에 반영한 뒤 붙어 있는 화면들에 나눠 줍니다. 받는 쪽은 전문가 화면,
체험·쉬운 화면, 세션 목록, 그래프 진행 표시 등 여럿이라, 이름을 문자열로 흩어 두면 한쪽만 바뀌어도
아무 오류 없이 그 이벤트가 무시됩니다. 이름은 여기 한 곳에만 둡니다.

`EventType` 은 `str` 이므로 `event["type"] == EventType.MESSAGE_ADDED` 와 `== "message_added"` 가 같고,
JSON 으로도 그대로 나갑니다. 페이로드의 모양은 각 이벤트를 내보내는 자리의 docstring 과
`wiki/orchestration/engine-lifecycle.md` 의 표를 따릅니다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Awaitable, Callable, Dict


class EventType(StrEnum):
    # 턴의 시작과 끝
    TURN_OPENED = "turn_opened"
    STATUS_CHANGED = "status_changed"
    ROUND_STARTED = "round_started"
    DEBATE_ENDED_EARLY = "debate_ended_early"  # 장부 수렴·토큰 예산으로 남은 라운드를 건너뜀 (v1.5)
    TURN_COMPLETED = "turn_completed"
    WORKSPACE_CHECKPOINTED = "workspace_checkpointed"  # 턴이 바꾼 파일을 셌음 (v1.5, 턴이 끝난 뒤)
    RUN_FINISHED = "run_finished"  # 엔진이 아니라 DebateRunner 가 보냅니다
    PERSIST_FAILED = "persist_failed"

    # 발언
    MESSAGE_STREAM_START = "message_stream_start"
    MESSAGE_STREAM_CHUNK = "message_stream_chunk"
    MESSAGE_ADDED = "message_added"
    LLM_FALLBACK = "llm_fallback"
    TOOL_EXECUTED = "tool_executed"
    PEER_CONSULTATION_STARTED = "peer_consultation_started"
    PEER_CONSULTATION_FINISHED = "peer_consultation_finished"

    # 사람의 개입
    STOP_REQUESTED = "stop_requested"
    INTERJECTION_QUEUED = "interjection_queued"
    INTERJECTIONS_DEFERRED = "interjections_deferred"

    # 한도에 닿아 사람에게 묻는 쪽지 (`<kind>_exhausted` / `<kind>_resolved`)
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    TOOL_BUDGET_RESOLVED = "tool_budget_resolved"
    CONTEXT_WINDOW_EXHAUSTED = "context_window_exhausted"
    CONTEXT_WINDOW_RESOLVED = "context_window_resolved"

    # 승인 카드
    TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
    TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
    PLAN_TASKS_STARTED = "plan_tasks_started"
    PLAN_APPROVAL_REQUESTED = "plan_approval_requested"
    PLAN_APPROVAL_RESOLVED = "plan_approval_resolved"
    USER_QUESTION_REQUESTED = "user_question_requested"  # 에이전트가 사람에게 묻는 질문 (v1.5)
    USER_QUESTION_RESOLVED = "user_question_resolved"

    # 대화 기억
    CONTEXT_TRIMMED = "context_trimmed"
    CONTEXT_SUMMARIZING = "context_summarizing"
    CONTEXT_SUMMARIZED = "context_summarized"
    CONTEXT_SUMMARY_FAILED = "context_summary_failed"
    LEDGER_UPDATE_STARTED = "ledger_update_started"
    LEDGER_UPDATED = "ledger_updated"
    LEDGER_UPDATE_FAILED = "ledger_update_failed"

    # 그래프 토론
    GRAPH_STARTED = "graph_started"
    GRAPH_STEP_STARTED = "graph_step_started"
    GRAPH_GATE_DECIDED = "graph_gate_decided"
    GRAPH_FINISHED = "graph_finished"

    # 합성
    MERMAID_REPAIR_STARTED = "mermaid_repair_started"
    MERMAID_REPAIR_FINISHED = "mermaid_repair_finished"
    ARTIFACTS_SYNTHESIZED = "artifacts_synthesized"


# 한도 쪽지의 종류(`DecisionRequest.kind`) → 답이 났을 때 보내는 이벤트.
DECISION_RESOLVED = {
    "tool_budget": EventType.TOOL_BUDGET_RESOLVED,
    "context_window": EventType.CONTEXT_WINDOW_RESOLVED,
}

# 턴이 끝났다는 신호 둘. 화면은 둘 중 먼저 오는 것으로 입력을 다시 엽니다.
TURN_END_EVENTS = frozenset({EventType.TURN_COMPLETED, EventType.RUN_FINISHED})

Event = Dict[str, Any]
EventCallback = Callable[[Event], Awaitable[None]]
