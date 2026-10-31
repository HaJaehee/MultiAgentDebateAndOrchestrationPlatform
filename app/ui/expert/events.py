"""전문가 화면이 토론 이벤트를 그리는 방법.

이벤트 하나에 메서드 하나입니다 (`@_handles`). 상태 줄·알림·카드·로스터 표시를 무엇으로 바꾸는지가
이벤트별로 모여 있어, 새 이벤트를 화면에 보이게 하려면 여기에 메서드 하나를 더합니다. 스냅샷에
남겨야 하는 것(새로고침한 화면이 다시 그릴 것)은 `app/orchestration/turn_run.py` 쪽입니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Optional

from nicegui import ui

from app.agents.llm_usage import usage_line
from app.events import TURN_END_EVENTS, Event, EventType
from app.orchestration.graph_run import GraphRunTracker, node_labels
from app.ui.components.sidebar import event_changes_session_list

if TYPE_CHECKING:
    from app.ui.expert.page import ExpertPage

Handler = Callable[["ExpertEventView", Event], Awaitable[None]]
_HANDLERS: Dict[str, Handler] = {}


def _handles(event_type: EventType) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _HANDLERS[event_type] = fn
        return fn
    return register


class ExpertEventView:
    """백그라운드 토론 이벤트를 전문가 화면에 반영합니다."""

    def __init__(self, page: "ExpertPage") -> None:
        self.page = page
        # 그래프 토론의 실행 표시. 이벤트와 기록 둘 다 이것 하나로 모읍니다 (graph_run.py).
        self.graph_tracker: Optional[GraphRunTracker] = None

    async def apply(self, event: Event) -> None:
        etype = event.get("type")

        # 그래프 실행 표시. 카드가 그려지기 전에 노드 이름을 알아야 배지가 붙습니다.
        if etype == EventType.GRAPH_STARTED:
            self.graph_tracker = GraphRunTracker()
            self.page.chat_feed.set_graph_labels(node_labels(event.get("spec")))
        if self.graph_tracker is not None:
            changed = self.graph_tracker.observe(event)
            if etype in TURN_END_EVENTS:
                self.page.roster_control.set_graph_run(self.graph_tracker, active=False)
            elif changed:
                self.page.roster_control.set_graph_run(self.graph_tracker, active=True)

        handler = _HANDLERS.get(etype)
        if handler is not None:
            await handler(self, event)

        # 목록에 보이는 것이 바뀌는 이벤트에서만 사이드바를 다시 그립니다.
        # 판단은 한 곳(`event_changes_session_list`)에서 합니다 — 갈래마다
        # 흩어 두면 새 이벤트를 더할 때마다 빠뜨립니다. 실제로 첫 사용자
        # 발언이 이 호출을 얻지 못해, 카드가 토론 내내 '시작 전' 이었습니다.
        if event_changes_session_list(event):
            await self.page.sidebar.refresh_list()

    @_handles(EventType.STATUS_CHANGED)
    async def _on_status_changed(self, event: Event) -> None:
        speaker = event.get("speaker", "")
        status = event.get("status", "")
        round_num = event.get("round", "")
        label = f"[{speaker}] 발언 및 분석 중..." if speaker else f"상태: {status}"
        round_info = f"Round {round_num}" if round_num else "Debating"
        self.page.chat_feed.set_busy(True, label, round_info)

    @_handles(EventType.ROUND_STARTED)
    async def _on_round_started(self, event: Event) -> None:
        r = event.get("round", 1)
        mr = event.get("max_rounds", 3)
        self.page.chat_feed.set_busy(True, f"Round {r}/{mr} 전문가 토론 진행 중...", f"Round {r}/{mr}")

    @_handles(EventType.DEBATE_ENDED_EARLY)
    async def _on_debate_ended_early(self, event: Event) -> None:
        r, mr = event.get("round", 0), event.get("max_rounds", 0)
        if event.get("reason") == "budget":
            ui.notify(
                f"턴 토큰 예산 한도에 도달하여 Round {r}/{mr}에서 토론을 조기 종료하고 결론을 합성합니다.",
                type="warning", position="bottom-right",
            )
        else:
            ui.notify(
                f"Round {r}/{mr}에서 상호 합의에 도달하여 남은 라운드를 건너뛰고 결론을 합성합니다.",
                type="positive", position="bottom-right",
            )

    @_handles(EventType.MESSAGE_STREAM_START)
    async def _on_message_stream_start(self, event: Event) -> None:
        self.page.chat_feed.start_streaming_message(event.get("message", {}))

    @_handles(EventType.MESSAGE_STREAM_CHUNK)
    async def _on_message_stream_chunk(self, event: Event) -> None:
        self.page.chat_feed.append_stream_chunk(event.get("message_id", ""), event.get("delta", ""))

    @_handles(EventType.LLM_FALLBACK)
    async def _on_llm_fallback(self, event: Event) -> None:
        self.page.chat_feed.mark_fallback(event.get("message_id", ""), event.get("llm_meta"))

    @_handles(EventType.MESSAGE_ADDED)
    async def _on_message_added(self, event: Event) -> None:
        self.page.chat_feed.append_message(event.get("message", {}))

    @_handles(EventType.MERMAID_REPAIR_STARTED)
    async def _on_mermaid_repair_started(self, event: Event) -> None:
        self.page.chat_feed.set_busy(
            True,
            f"[{event.get('agent_name', '')}] 다이어그램 {event.get('broken', 0)}개의 "
            f"문법 오류를 발견하여 다시 작성하는 중입니다 "
            f"({event.get('attempt', 1)}/{event.get('max_attempts', 1)}회차)...",
            "Fixing diagram",
        )

    @_handles(EventType.MERMAID_REPAIR_FINISHED)
    async def _on_mermaid_repair_finished(self, event: Event) -> None:
        if event.get("resolved"):
            ui.notify(
                f"다이어그램 문법 오류를 {event.get('attempts', 1)}회 만에 수정하였습니다.",
                type="positive", position="bottom-right",
            )
        else:
            # 고치지 못한 것을 조용히 넘기면, 사람은 아티팩트 탭에서
            # 빈 화면을 보고 나서야 알게 됩니다.
            ui.notify(
                f"다이어그램 {event.get('remaining', 0)}개에 문법 오류가 남아 있습니다. "
                f"원본을 보존하였으니 산출물 탭에서 확인하십시오.",
                type="warning", position="bottom-right", close_button="확인",
            )

    @_handles(EventType.ARTIFACTS_SYNTHESIZED)
    async def _on_artifacts_synthesized(self, event: Event) -> None:
        # 교체가 아니라 덧붙이기입니다. 이 이벤트에는 이번 턴 산출물만 옵니다.
        self.page.artifact_viewer.add_artifacts(event.get("artifacts", []))

    @_handles(EventType.STOP_REQUESTED)
    async def _on_stop_requested(self, event: Event) -> None:
        # 정지를 누른 화면뿐 아니라 같은 세션을 보고 있는 모든 화면이
        # 같은 상태를 보아야 합니다.
        self.page.chat_feed.set_busy(
            True,
            "정지 요청됨 — 진행 중인 발언을 마친 후 지금까지의 토론 내용을 바탕으로 합성합니다.",
            "Stopping",
        )
        self.page.chat_feed.set_stop_pending(True)
        ui.notify(
            "정지를 요청하였습니다. 진행 중인 발언을 마치는 대로 최종 합성 단계로 진행합니다.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.INTERJECTION_QUEUED)
    async def _on_interjection_queued(self, event: Event) -> None:
        pending = event.get("pending", 0)
        if event.get("deferred"):
            # 합성이 시작된 뒤에는 이번 턴에 실을 자리가 없습니다.
            # "다음 발언 차례에 반영" 이라고 알려 놓고 조용히 버리면 안 됩니다.
            ui.notify(
                "최종 합성이 이미 시작되어 이번 턴에는 반영되지 않습니다. "
                "해당 내용은 기록에 보존되며 다음 요청부터 반영됩니다.",
                type="warning",
                position="bottom-right",
            )
        else:
            ui.notify(
                f"개입 메시지를 전달하였습니다 (대기 {pending}건). 다음 발언 순서에 반영됩니다.",
                type="info",
                position="bottom-right",
            )

    @_handles(EventType.INTERJECTIONS_DEFERRED)
    async def _on_interjections_deferred(self, event: Event) -> None:
        count = event.get("count", 0)
        ui.notify(
            f"개입 메시지 {count}건이 합성 단계 이후에 도착하여 이번 턴에 반영되지 않았습니다. "
            f"기록에 저장되었으므로 다음 요청에서 이어집니다.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.TOOL_BUDGET_EXHAUSTED)
    async def _on_tool_budget_exhausted(self, event: Event) -> None:
        # 에이전트는 지금 이 답을 기다리며 멈춰 있습니다. 알림은 지나가지만
        # 안내 줄은 답할 때까지 남습니다.
        self.page.chat_feed.set_budget_request({k: v for k, v in event.items() if k != "type"})
        self.page.chat_feed.set_busy(
            True,
            f"[{event.get('agent_name', '')}] 도구 호출 상한({event.get('limit', 0)}회)에 도달하였습니다 — "
            f"상한을 늘릴지 마무리할지 선택해 주십시오.",
            "Tool limit",
        )
        ui.notify(
            f"{event.get('agent_name', '에이전트')} 에이전트가 도구 호출 상한에 도달하였습니다. "
            f"상한을 늘리거나 지금까지의 결과로 마무리하도록 선택해 주십시오.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.TOOL_BUDGET_RESOLVED)
    async def _on_tool_budget_resolved(self, event: Event) -> None:
        self.page.chat_feed.clear_budget_request(event.get("id"))
        granted = event.get("granted", 0)
        if granted:
            ui.notify(
                f"도구 호출 상한을 {granted}회 늘렸습니다 (총 {event.get('limit', 0)}회). "
                f"토론을 계속 진행합니다.",
                type="info",
                position="bottom-right",
            )
        elif event.get("outcome") == "timeout":
            ui.notify(
                "도구 상한 확장에 응답이 없어, 지금까지의 관측 결과만으로 마무리합니다. "
                "발언 및 도구 기록은 그대로 보존됩니다.",
                type="warning",
                position="bottom-right",
            )
        else:
            ui.notify(
                "도구를 추가로 사용하지 않고 지금까지의 관측 결과로 마무리합니다.",
                type="info",
                position="bottom-right",
            )

    @_handles(EventType.CONTEXT_WINDOW_EXHAUSTED)
    async def _on_context_window_exhausted(self, event: Event) -> None:
        self.page.chat_feed.set_decision_request({k: v for k, v in event.items() if k != "type"})
        self.page.chat_feed.set_busy(
            True,
            f"[{event.get('agent_name', '')}] 컨텍스트 창 용량 초과 — "
            f"한도를 확장할지 이전 내용을 생략하고 진행할지 선택해 주십시오.",
            "Context full",
        )
        ui.notify(
            f"{event.get('agent_name', '에이전트')} 에이전트의 컨텍스트 창이 가득 찼습니다. "
            f"한도를 확장하거나, 이전 기록을 생략하고 진행하도록 선택해 주십시오.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.CONTEXT_WINDOW_RESOLVED)
    async def _on_context_window_resolved(self, event: Event) -> None:
        self.page.chat_feed.clear_budget_request(event.get("id"))
        granted = event.get("granted", 0)
        if granted:
            ui.notify(
                f"컨텍스트 한도를 {granted:,} 토큰 확장하였습니다 "
                f"(총 {event.get('window', 0):,}). 이전 기록은 그대로 유지됩니다.",
                type="info",
                position="bottom-right",
            )
        elif event.get("outcome") == "timeout":
            ui.notify(
                "컨텍스트 확장에 응답이 없어, 이전 기록부터 생략하여 진행합니다.",
                type="warning",
                position="bottom-right",
            )
        else:
            ui.notify(
                "컨텍스트를 확장하지 않고 지금까지의 기록으로 마무리합니다.",
                type="info",
                position="bottom-right",
            )

    @_handles(EventType.TOOL_APPROVAL_REQUESTED)
    async def _on_tool_approval_requested(self, event: Event) -> None:
        # 에이전트는 이 답을 기다리며 멈춰 있습니다. 카드는 답하거나 시간이
        # 지나 거부될 때까지 남습니다.
        self.page.chat_feed.add_approval({k: v for k, v in event.items() if k != "type"})
        self.page.chat_feed.set_busy(
            True,
            f"[{event.get('agent_name', '')}] `{event.get('tool_name', '')}` 실행 승인 대기 — "
            f"{event.get('risk_label', '')}",
            "Approval",
        )
        ui.notify(
            f"{event.get('agent_name', '에이전트')} 에이전트가 `{event.get('tool_name', '')}` 실행 "
            f"승인을 기다리고 있습니다 ({event.get('risk_label', '')}).",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.TOOL_APPROVAL_RESOLVED)
    async def _on_tool_approval_resolved(self, event: Event) -> None:
        self.page.chat_feed.remove_approval(event.get("id"))
        # "이 대화에서 허용·거부" 가 더해졌을 수 있습니다. 규칙 창의 숫자와 목록을 맞춥니다.
        self.page.roster_control.set_tool_rules(event.get("session_grants"), event.get("session_denials"))
        if event.get("note"):
            ui.notify(str(event["note"]), type="warning", position="bottom-right")
        elif event.get("persisted"):
            ui.notify(
                f"conf.json의 tool_security.{event.get('persisted_as', 'allow')}에 "
                f"저장하였습니다: " + ", ".join(event["persisted"]),
                type="positive",
                position="bottom-right",
            )
            self.page.roster_control.refresh_global_tool_rules()
        elif event.get("decision") == "timeout":
            ui.notify(
                f"`{event.get('tool_name', '')}` 승인에 응답이 없어 실행하지 않았습니다.",
                type="warning",
                position="bottom-right",
            )

    @_handles(EventType.PLAN_TASKS_STARTED)
    async def _on_plan_tasks_started(self, event: Event) -> None:
        self.page.chat_feed.set_busy(
            True,
            f"[{event.get('agent_name', '')}] 승인받을 태스크 분담표를 정리하는 중입니다...",
            "Plan",
        )

    @_handles(EventType.PLAN_APPROVAL_REQUESTED)
    async def _on_plan_approval_requested(self, event: Event) -> None:
        # 턴 전체가 이 답을 기다리며 멈춰 있습니다 (ADR-028). 카드는 답하거나 대기
        # 시간이 끝날 때까지 남습니다.
        self.page.chat_feed.set_plan_approval({k: v for k, v in event.items() if k != "type"})
        self.page.chat_feed.set_busy(
            True,
            "계획 승인 대기 — 태스크를 확인하고 승인하시거나, 의견을 적어 수정을 요청해 주십시오.",
            "Plan approval",
        )
        ui.notify(
            f"{event.get('agent_name', '오케스트레이터')}의 계획이 승인을 기다리고 있습니다. "
            f"승인하시기 전에는 토론이 시작되지 않습니다.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.PLAN_APPROVAL_RESOLVED)
    async def _on_plan_approval_resolved(self, event: Event) -> None:
        self.page.chat_feed.clear_plan_approval(event.get("id"))
        decision = event.get("decision")
        if decision == "approve":
            self.page.chat_feed.set_busy(True, "계획을 승인하였습니다. 토론을 시작합니다.", "Debating")
        elif decision == "revise":
            self.page.chat_feed.set_busy(
                True, "계획 수정을 요청하였습니다. 오케스트레이터가 계획을 다시 작성합니다.", "Planning"
            )

    @_handles(EventType.GRAPH_GATE_DECIDED)
    async def _on_graph_gate_decided(self, event: Event) -> None:
        verdict = "예" if event.get("decision") == "yes" else "아니오"
        ui.notify(
            f"판정 “{event.get('label', '')}”: {verdict}"
            + (" (응답을 해석하지 못해 기본 분기로 진행)" if event.get("fallback") else "")
            + (f" — {event.get('reason')}" if event.get("reason") else ""),
            type="warning" if event.get("fallback") else "info",
            position="bottom-right",
        )

    @_handles(EventType.GRAPH_FINISHED)
    async def _on_graph_finished(self, event: Event) -> None:
        if event.get("reason") in ("idle", "step_cap"):
            ui.notify(
                "그래프가 최종 합성 노드에 도달하기 전에 종료되어, 지금까지의 발언 내용으로 합성합니다."
                if event.get("reason") == "idle" else
                "그래프가 최대 단계 상한에 도달하여 지금까지의 발언 내용으로 합성합니다.",
                type="warning", position="bottom-right",
            )

    @_handles(EventType.LEDGER_UPDATE_STARTED)
    async def _on_ledger_update_started(self, event: Event) -> None:
        self.page.chat_feed.set_busy(
            True, f"결정 장부 정리 중 ({event.get('reason', '')})...", "Ledger"
        )

    @_handles(EventType.LEDGER_UPDATED)
    async def _on_ledger_updated(self, event: Event) -> None:
        self.page.roster_control.set_decision_ledger(event.get("ledger", ""))

    @_handles(EventType.LEDGER_UPDATE_FAILED)
    async def _on_ledger_update_failed(self, event: Event) -> None:
        ui.notify(
            f"결정 장부를 갱신하지 못하여 기존 장부를 유지합니다 ({event.get('reason', '')}): "
            f"{event.get('error', '')}",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.CONTEXT_SUMMARIZING)
    async def _on_context_summarizing(self, event: Event) -> None:
        self.page.chat_feed.set_busy(
            True,
            f"[{event.get('agent_name', '')}] 컨텍스트 용량 초과로 이전 기록 "
            f"{event.get('messages', 0)}건을 요약하는 중입니다...",
            "Summarizing",
        )

    @_handles(EventType.CONTEXT_SUMMARIZED)
    async def _on_context_summarized(self, event: Event) -> None:
        ui.notify(
            f"컨텍스트 용량 초과로 이전 기록 {event.get('folded', 0)}건을 요약 압축하였습니다 "
            f"(누적 {event.get('total', 0)}건). 사용자 발언과 결정 장부는 원문 그대로 유지됩니다.",
            type="info",
            position="bottom-right",
        )

    @_handles(EventType.CONTEXT_SUMMARY_FAILED)
    async def _on_context_summary_failed(self, event: Event) -> None:
        ui.notify(
            "이전 기록을 요약하지 못하여, 컨텍스트 한도를 초과하는 오래된 기록은 생략하고 진행합니다.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.CONTEXT_TRIMMED)
    async def _on_context_trimmed(self, event: Event) -> None:
        # 답이 필요한 물음이 아니라 사후 통지입니다. 버튼 없는 알림으로만
        # 띄웁니다 — 배너는 사람이 골라야 하는 자리에만 씁니다.
        where = "최종 합성 전사" if event.get("where") == "synthesis" else "발언 맥락"
        ui.notify(
            f"{where}에서 기록 {event.get('dropped', 0)}건이 컨텍스트 한도로 "
            f"생략되었습니다 (누적 {event.get('total_dropped', 0)}건). "
            f"로스터 설정에서 해당 에이전트의 '컨텍스트 창' 크기를 늘리시면 생략을 방지할 수 있습니다.",
            type="warning",
            position="bottom-right",
        )

    @_handles(EventType.PERSIST_FAILED)
    async def _on_persist_failed(self, event: Event) -> None:
        # 사람이 자리를 비운 사이에 일어나기 쉬운 일이라(백신 검사·백업이 파일을
        # 잡는 때), 저절로 사라지는 알림으로는 돌아와서 볼 수가 없습니다. 닫을
        # 때까지 남깁니다.
        saved = event.get("saved_to")
        where = (
            f"내용은 파일로 저장하였습니다: {saved}"
            if saved else "파일로도 저장하지 못했습니다. 콘솔 로그를 확인해 주십시오."
        )
        ui.notify(
            f"{event.get('label') or '기록'}을(를) DB 에 저장하지 못했습니다 "
            f"({event.get('error', '')}). {where}",
            type="negative",
            position="top",
            close_button="확인",
            timeout=0,
            multi_line=True,
        )

    @_handles(EventType.TURN_COMPLETED)
    async def _on_turn_completed(self, event: Event) -> None:
        # 이 턴의 토큰·LLM 시간. 끝난 사유를 적는 상태 줄 끝에 붙입니다.
        used = usage_line(event.get("usage"))
        used = f" · {used}" if used else ""
        failed = event.get("failed_agents") or []
        fallback = event.get("fallback_agents") or []
        if fallback:
            # 실패가 아니라 사후 통지입니다. 발언은 받았지만, 정해 둔 모델이 쓴 것이 아닙니다.
            ui.notify(
                f"연결에 실패한 모델 대신 기본 모델이 답한 에이전트가 있습니다: {', '.join(fallback)}. "
                "발언 카드의 '기본 모델로 응답' 배지와 상단의 '모델 연결 상태' 를 확인해 주십시오.",
                type="warning",
                position="bottom-right",
                multi_line=True,
            )
        if failed:
            self.page.chat_feed.set_busy(
                False, f"토론 완료 — 응답하지 못한 에이전트: {', '.join(failed)}{used}", "Incomplete",
            )
            ui.notify(
                f"일부 에이전트가 LLM 엔드포인트에 연결하지 못했습니다: {', '.join(failed)}",
                type="warning",
                position="bottom-right",
            )
        elif event.get("interrupted"):
            self.page.chat_feed.set_busy(
                False,
                f"끊긴 턴을 지금까지의 발언으로 마무리하였습니다 (서버 중단으로 덜 논의됨).{used}",
                "Resumed",
            )
            ui.notify(
                "서버 중단으로 끊긴 턴을 기록된 발언만으로 합성하였습니다. 보고서에 중단 사실을 적었습니다.",
                type="info",
                position="bottom-right",
            )
        elif event.get("stopped_early"):
            rounds = event.get("rounds_completed", 0)
            max_rounds = event.get("max_rounds", 0)
            self.page.chat_feed.set_busy(
                False,
                f"사용자 요청으로 정지됨 — {rounds}/{max_rounds} 라운드까지의 토론 내용으로 합성하였습니다.{used}",
                "Stopped",
            )
            ui.notify(
                "정지 요청에 따라 지금까지의 토론 내용만으로 최종 산출물을 생성하였습니다.",
                type="info",
                position="bottom-right",
            )
        elif event.get("converged_round"):
            self.page.chat_feed.set_busy(
                False, f"Round {event['converged_round']}에서 합의 수렴으로 조기 종료 · 최종 결론 합성 완료{used}", "Done",
            )
        else:
            self.page.chat_feed.set_busy(False, f"토론 완료 및 최종 산출물 합성 완료{used}", "Done")
        # 결론 카드에 갈라 보기 단추를 붙입니다 (바뀐 파일 칩은 체크포인트 알림이 붙입니다).
        self.page.chat_feed.set_turn_actions(event.get("synthesis_id"), event.get("turn_id") or "")
        # 첫 턴에서 페르소나가 고정되었으므로 편집 버튼을 잠금 상태로 바꿉니다.
        self.page.roster_control.set_personas_locked(True)

    @_handles(EventType.USER_QUESTION_REQUESTED)
    async def _on_user_question_requested(self, event: Event) -> None:
        self.page.chat_feed.add_question({k: v for k, v in event.items() if k != "type"})
        self.page.chat_feed.set_busy(True, f"[{event.get('agent_name', '')}]의 질문에 대한 사용자 응답을 대기 중입니다.", "Question")

    @_handles(EventType.USER_QUESTION_RESOLVED)
    async def _on_user_question_resolved(self, event: Event) -> None:
        # 다른 화면에서 답변했거나 시간이 만료된 경우 해당 질문 카드를 닫습니다.
        self.page.chat_feed.remove_approval(event.get("id"))

    @_handles(EventType.WORKSPACE_CHECKPOINTED)
    async def _on_workspace_checkpointed(self, event: Event) -> None:
        self.page.chat_feed.set_turn_actions(event.get("message_id"), event.get("turn_id", ""), event.get("changed"))

    @_handles(EventType.RUN_FINISHED)
    async def _on_run_finished(self, event: Event) -> None:
        # 토론이 끝났으므로 MCP 서버 구성을 다시 만질 수 있습니다. 상태가
        # 확정되는 시점이 여기라, 잠금 해제도 여기서 알립니다.
        self.page.roster_control.refresh_mcp_lock()
        status = event.get("status")
        if event.get("parked"):
            # 계획 승인에 답이 없어 턴을 멈춰 둔 것입니다 (ADR-028). 오류가 아니므로
            # 오류로 알리지 않고, 이어 갈 수 있다는 안내 줄을 띄웁니다.
            self.page.chat_feed.set_busy(False, str(event.get("error") or ""), "Waiting")
            ui.notify(
                "계획 승인에 응답이 없어 턴을 멈춰 두었습니다. 아무것도 실행되지 않았습니다. "
                "'이어서 진행'을 누르시면 같은 계획으로 승인 카드가 다시 열립니다.",
                type="warning", position="bottom-right", close_button="확인", timeout=0,
            )
            await self.page.refresh_unfinished_turn()
        elif status == "failed":
            error = event.get("error") or "알 수 없는 오류"
            self.page.chat_feed.set_busy(False, f"오류로 인하여 중단되었습니다: {error}", "Error")
            ui.notify(f"토론 실행 중 오류가 발생하였습니다: {error}", type="negative")
            # 멈춘 턴은 "실패" 로 기록됩니다. 이어 가거나 마무리하거나 버릴 수 있게
            # 안내 줄을 다시 띄웁니다.
            await self.page.refresh_unfinished_turn()
        elif status == "cancelled":
            self.page.chat_feed.set_busy(False, "토론이 취소되었습니다.", "Cancelled")
        else:
            self.page.chat_feed.set_busy(False)
