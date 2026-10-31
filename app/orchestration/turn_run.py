"""진행 중이거나 방금 끝난 토론 한 턴의 정본 스냅샷.

엔진이 보내는 이벤트를 `TurnRun.apply()` 가 스냅샷에 반영하고, `publish()` 가 붙어 있는 화면들의
구독 큐로 나눠 줍니다. 새로 붙는 화면(새로고침, 다른 브라우저)은 `snapshot()` 을 그대로 그리면 되고,
화면이 죽으면 그 큐만 버려집니다. 이 모듈은 화면을 모릅니다.

이벤트마다 스냅샷을 어떻게 바꾸는지는 `_reduces(...)` 로 등록한 메서드 하나씩입니다. 새 이벤트가
스냅샷에 남아야 하면 여기에 하나를 더합니다 (이름표는 `app.events.EventType`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Set

from app.agents.llm_usage import usage_line
from app.config import resolve_workspace_dir
from app.events import DECISION_RESOLVED, Event, EventType
from app.orchestration.control import TurnControl
from app.orchestration.event_bus import BaseEventBus, get_event_bus
from app.orchestration.graph_run import GraphRunTracker

logger = logging.getLogger(__name__)

# 구독 큐 상한. 브라우저 하나가 느려도 토론을 붙잡지 않도록 넉넉히 잡되,
# 무한히 쌓이지는 않게 합니다. 넘치면 그 화면은 스냅샷으로 다시 맞춥니다.
MAX_QUEUED_EVENTS = 2000

Reducer = Callable[["TurnRun", Event], None]
_REDUCERS: Dict[str, Reducer] = {}


def _reduces(*types: EventType) -> Callable[[Reducer], Reducer]:
    """이 메서드가 어떤 이벤트를 스냅샷에 반영하는지 등록합니다."""
    def register(fn: Reducer) -> Reducer:
        for event_type in types:
            _REDUCERS[event_type] = fn
        return fn
    return register


def _payload(event: Event) -> Dict[str, Any]:
    """`type` 을 뺀 나머지. 카드·쪽지는 이것을 그대로 들고 있습니다."""
    return {k: v for k, v in event.items() if k != "type"}


class TurnRun:
    """진행 중이거나 방금 끝난 토론 한 턴."""

    def __init__(
        self,
        session_id: str,
        user_prompt: str,
        workspace: Optional[str] = None,
        *,
        turn_id: Optional[str] = None,
        resume_mode: Optional[str] = None,
        retry_target: Optional[str] = None,
        event_bus: Optional[BaseEventBus] = None,
    ):
        self.session_id = session_id
        self.user_prompt = user_prompt
        self.workspace = resolve_workspace_dir(workspace or None)
        self.event_bus = event_bus or get_event_bus()
        self.channel = f"session:{self.session_id}"
        self.status: str = "running"  # running | completed | failed | cancelled
        self.error: Optional[str] = None
        # 이 실행이 돌리는 턴 (`TurnModel`). 새 턴이면 엔진이 요청을 기록하는 순간
        # (`turn_opened`) 정해지고, 끊긴 턴을 이어 가는 실행이면 처음부터 압니다.
        self.turn_id: Optional[str] = turn_id
        # 끊긴 턴을 다시 세운 실행이면 "finish" · "continue" (ADR-024).
        self.resume_mode: Optional[str] = resume_mode
        # 끝난 발언 하나를 다시 실행하거나 되돌리는 실행이면 그 발언 id (ADR-036). 다음 발언자도 합성도 없으므로
        # 정지·개입을 받지 않고, 긴급 종료는 기록을 지우지 않습니다.
        self.retry_target: Optional[str] = retry_target
        self.busy: bool = True
        self.status_text: str = "토론 준비 중..."
        self.round_info: str = "Debating"

        # 이번 턴에 나온 발언 (스트리밍 중인 것 포함). id 로 중복 제거합니다.
        self.messages: List[Dict[str, Any]] = []
        self._message_index: Dict[str, int] = {}
        self.streaming_ids: Set[str] = set()
        self.artifacts: List[Dict[str, Any]] = []

        self.task: Optional[asyncio.Task] = None
        self._subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = set()

        # 사람이 이 턴에 끼어드는 통로. 엔진이 발언 사이마다 꺼내 봅니다.
        self.control = TurnControl()
        # 지금 어느 단계인지. 최종 합성에 들어간 뒤로는 개입을 실을 자리가 없어,
        # 화면에 "이번 턴에는 반영되지 않는다" 고 정확히 알려야 합니다.
        self.phase: str = "planning"

        # 한도에 닿아 사람의 답을 기다리는 쪽지 (도구 상한 또는 컨텍스트 창).
        # 스냅샷에 들어가므로 새로고침하거나 나중에 붙은 화면에서도 같은 물음이
        # 보입니다 (기다리는 쪽은 엔진이고, 답할 수 있는 창이 3분뿐입니다).
        self.decision_request: Optional[Dict[str, Any]] = None

        # 도구 승인 카드 (도구 보안 판정이 "묻기" 인 호출). 한도 쪽지와 달리 여러 장이
        # 동시에 떠 있을 수 있습니다 — 병렬 라운드의 발언자마다 하나씩. 스냅샷에 들어가
        # 새로고침한 화면과 원격 화면에서도 같은 카드가 보입니다.
        self.approvals: List[Dict[str, Any]] = []

        # 계획 승인 카드 (ADR-028). 한 턴에 많아야 하나입니다. 답할 수 있는 시간이 길어
        # (기본 10분) 그 사이 새로고침하거나 다른 화면에서 붙는 일이 흔하므로, 스냅샷에 둡니다.
        self.plan_approval: Optional[Dict[str, Any]] = None
        # 답변을 대기 중인 에이전트 질문 카드 목록 (`ask__ask_user`, v1.5). 페이지 새로고침 시에도 동일하게 복원됩니다.
        self.questions: List[Dict[str, Any]] = []
        # 승인을 기다리다 답이 없어 멈춰 둔 실행인지. 오류로 멈춘 것과 구분해 알립니다.
        self.parked: bool = False

        # 컨텍스트 한도로 생략된 기록의 누적 건수. 화면이 "얼마나 잃었는지" 를
        # 계속 보여주기 위해 스냅샷에 남깁니다.
        self.context_dropped: int = 0

        # 이번 턴에 갱신된 결정 장부. None 이면 아직 갱신되지 않은 것이고, 화면은 DB 의
        # 값을 그대로 씁니다 (장부는 턴이 끝날 때 저장됩니다).
        self.decision_ledger: Optional[str] = None

        # 그래프 토론이면 어느 노드가 돌고 있는지. 새로고침한 화면이 실행 표시를 다시 그립니다.
        # 방문 횟수와 판정 갈래는 발언 기록에서 세므로, 여기에는 기록으로 알 수 없는 것만 있습니다.
        self.graph: Optional[GraphRunTracker] = None

    # -------------------------------------------------- 구독

    def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        queue = self.event_bus.subscribe(self.channel, maxsize=MAX_QUEUED_EVENTS)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        self.event_bus.unsubscribe(self.channel, queue)
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: Event) -> None:
        """붙어 있는 화면들에 이벤트를 나눠 줍니다. 큐가 넘친 화면은 떼어 냅니다."""
        self.event_bus.publish_nowait(self.channel, event)
        for queue in list(self._subscribers):
            if queue.full():
                logger.warning("Dropping a slow debate subscriber for session %s", self.session_id)
                self._subscribers.discard(queue)

    # -------------------------------------------------- 사람의 개입

    def request_stop(self) -> bool:
        """남은 라운드를 접고 지금까지의 토론으로 합성하도록 요청합니다.

        `DebateRunner.cancel()` 과 다릅니다. 태스크를 죽이지 않으므로 진행 중인
        발언이 중간에 잘리지 않고, 최종 합성과 아티팩트도 그대로 나옵니다.
        이미 요청했거나 끝난 토론이면 아무것도 하지 않고 False 를 돌려줍니다.
        """
        if self.status != "running" or self.control.stop_requested or self.retry_target:
            return False
        self.control.request_stop()
        self._emit({"type": EventType.STOP_REQUESTED})
        return True

    def interject(
        self,
        text: str,
        skill_designations: Optional[Dict[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
    ) -> bool:
        """토론 중인 에이전트들에게 사용자 메시지를 끼워 넣습니다.

        곧바로 반영되지는 않습니다. 지금 발언 중인 에이전트의 프롬프트는 이미
        만들어져 나갔으므로, 엔진이 다음 발언자로 넘어가는 지점에서 꺼내 갑니다.
        `skill_designations` (전문가 키 → 스킬) 와 `attachments` (언급한 파일) 도 그때 함께 반영됩니다.
        """
        if self.status != "running" or self.retry_target:
            return False
        if not self.control.add_note(text, skill_designations, attachments):
            return False
        self._emit({
            "type": EventType.INTERJECTION_QUEUED,
            "text": text.strip(),
            "pending": len(self.control.pending_notes),
            # 합성 중에 들어온 것은 이번 턴의 발언에 실리지 못하고, 기록에만 남아
            # 다음 요청의 맥락이 됩니다.
            "deferred": self.phase == "synthesizing",
        })
        return True

    def resolve_decision(self, extra: int, request_id: Optional[str] = None) -> bool:
        """대기 중인 물음에 답합니다. `extra` 가 0 이면 "지금 마무리하라".

        도구 상한이든 컨텍스트 창이든 흐름은 같습니다 — 답을 받은 에이전트는 그
        자리에서 이어서 돌거나(확장), 지금까지의 것으로 마무리합니다. 어느 쪽이든
        지금까지의 발언은 그대로 남습니다. 어느 물음에 답하는지는 화면에 떠 있는
        쪽지의 `kind` 가 정합니다.
        """
        if self.status != "running":
            return False
        shown = self.decision_request or {}
        kind = shown.get("kind", "tool_budget")
        if not self.control.resolve_decision(extra, request_id):
            return False
        self._emit({
            "type": DECISION_RESOLVED[kind],
            "id": request_id or shown.get("id"),
            "agent_name": shown.get("agent_name", ""),
            "outcome": "extended" if extra > 0 else "wrap_up",
            "granted": max(0, int(extra)),
            "window": int(shown.get("limit", 0)) + max(0, int(extra)),
            "by_user": True,
        })
        return True

    def resolve_tool_approval(
        self,
        request_id: str,
        decision: str,
        *,
        scope: Optional[List[str]] = None,
        reason: str = "",
        approver: str = "",
    ) -> bool:
        """도구 승인 카드에 답합니다. 범위가 쓸 수 없으면 ValueError (화면이 알립니다).

        카드는 여기서 걷지 않습니다. 기다리던 게이트가 깨어나 `tool_approval_resolved`
        를 보내면 그때 걷힙니다 — 저장 실패 같은 사정도 그 이벤트에 실려 옵니다.
        """
        if self.status != "running":
            return False
        return self.control.resolve_tool_approval(
            request_id, decision, scope=scope, reason=reason, approver=approver,
        )

    def resolve_plan_approval(
        self,
        request_id: str,
        decision: str,
        *,
        tasks: Optional[List[Dict[str, Any]]] = None,
        comment: str = "",
        task_comments: Optional[Dict[str, str]] = None,
        approver: str = "",
    ) -> bool:
        """계획 승인 카드에 답합니다 (ADR-028). 쓸 수 없는 답이면 ValueError (화면이 알립니다).

        카드는 여기서 걷지 않습니다. 기다리던 엔진이 깨어나 `plan_approval_resolved` 를
        보내면 그때 걷힙니다 — 같은 대화를 보는 다른 화면도 그 이벤트로 함께 걷습니다.
        """
        if self.status != "running":
            return False
        return self.control.resolve_plan_approval(
            request_id, decision, tasks=tasks, comment=comment,
            task_comments=task_comments, approver=approver,
        )

    # -------------------------------------------------- 상태 적용

    def _emit(self, event: Event) -> None:
        """엔진이 아니라 사람이 만든 이벤트를 스냅샷과 구독자에게 함께 보냅니다."""
        self.apply(event)
        self.publish(event)

    def _pending_prefix(self, text: str) -> str:
        """정지를 기다리는 중이라는 표시. 다음 상태 문구가 덮어써도 계속 붙습니다."""
        return f"(정지 대기) {text}" if self.control.stop_requested else text

    def apply(self, event: Event) -> None:
        """이벤트를 정본 스냅샷에 반영합니다. 새로 붙는 화면이 이걸 그립니다."""
        etype = event.get("type")
        if etype == EventType.GRAPH_STARTED:
            self.graph = GraphRunTracker()
        if self.graph is not None:
            self.graph.observe(event)
        reducer = _REDUCERS.get(etype)
        if reducer is not None:
            reducer(self, event)

    @_reduces(EventType.TURN_OPENED)
    def _on_turn_opened(self, event: Event) -> None:
        if not event.get("turn_id"):
            return
        self.turn_id = event["turn_id"]
        # 끊긴 턴을 이어 가는 실행은 그 턴의 앞선 기록도 들고 시작합니다. 새로 붙는 화면의
        # 그래프 진행 표시가 스냅샷의 발언에서 다시 그려지기 때문입니다.
        for msg in event.get("messages") or []:
            if msg.get("id"):
                self._upsert(dict(msg))

    @_reduces(EventType.STATUS_CHANGED)
    def _on_status_changed(self, event: Event) -> None:
        speaker = event.get("speaker", "")
        status = event.get("status", "")
        round_num = event.get("round", "")
        if status:
            self.phase = status
        self.busy = True
        label = f"[{speaker}] 발언 및 분석 중..." if speaker else f"상태: {status}"
        self.status_text = self._pending_prefix(label)
        self.round_info = f"Round {round_num}" if round_num else "Debating"

    @_reduces(EventType.ROUND_STARTED)
    def _on_round_started(self, event: Event) -> None:
        r = event.get("round", 1)
        mr = event.get("max_rounds", 3)
        self.busy = True
        self.status_text = self._pending_prefix(f"Round {r}/{mr} 전문가 토론 진행 중...")
        self.round_info = f"Round {r}/{mr}"

    @_reduces(EventType.MESSAGE_STREAM_START)
    def _on_stream_start(self, event: Event) -> None:
        msg = dict(event.get("message", {}))
        msg_id = msg.get("id", "")
        if msg_id:
            self._upsert(msg)
            self.streaming_ids.add(msg_id)

    @_reduces(EventType.MESSAGE_STREAM_CHUNK)
    def _on_stream_chunk(self, event: Event) -> None:
        idx = self._message_index.get(event.get("message_id", ""))
        if idx is not None:
            self.messages[idx]["content"] += event.get("delta", "")

    @_reduces(EventType.LLM_FALLBACK)
    def _on_llm_fallback(self, event: Event) -> None:
        # 흘러나오는 발언을 전역 기본 모델이 대신 쓰고 있습니다 (ADR-030). 도중에 붙는 화면도 알게 합니다.
        idx = self._message_index.get(event.get("message_id", ""))
        if idx is not None:
            self.messages[idx]["llm_meta"] = event.get("llm_meta")

    @_reduces(EventType.MESSAGE_ADDED)
    def _on_message_added(self, event: Event) -> None:
        msg = dict(event.get("message", {}))
        msg_id = msg.get("id", "")
        if msg_id:
            self._upsert(msg)
            self.streaming_ids.discard(msg_id)

    @_reduces(EventType.STOP_REQUESTED)
    def _on_stop_requested(self, event: Event) -> None:
        self.busy = True
        self.status_text = "정지가 요청되었습니다 — 진행 중인 발언을 마친 후 지금까지의 토론 내용을 바탕으로 합성합니다."
        self.round_info = "Stopping"

    @_reduces(EventType.INTERJECTION_QUEUED)
    def _on_interjection_queued(self, event: Event) -> None:
        pending = event.get("pending", 0)
        if event.get("deferred"):
            self.status_text = self._pending_prefix(
                f"사용자 개입 {pending}건 — 최종 합성 중이므로 다음 요청부터 반영됩니다."
            )
        else:
            self.status_text = self._pending_prefix(
                f"사용자 개입 {pending}건 대기 — 다음 발언 순서에 반영됩니다."
            )

    @_reduces(EventType.INTERJECTIONS_DEFERRED)
    def _on_interjections_deferred(self, event: Event) -> None:
        count = event.get("count", 0)
        self.status_text = (
            f"개입 {count}건은 합성 이후에 도착하여 기록에만 반영되었습니다 (다음 요청에 반영됩니다)."
        )

    @_reduces(EventType.TOOL_BUDGET_EXHAUSTED)
    def _on_tool_budget_exhausted(self, event: Event) -> None:
        self.decision_request = _payload(event)
        self.busy = True
        self.status_text = (
            f"[{event.get('agent_name', '')}] 도구 호출 상한 {event.get('limit', 0)}회를 "
            f"모두 소진했습니다 — 상한을 늘리실지, 지금까지의 관측 결과로 마무리하실지 선택해 주십시오."
        )
        self.round_info = "Tool limit"

    def _clear_answered_decision(self, event: Event) -> None:
        # 답이 난 쪽지만 걷습니다. 병렬 라운드에서는 그 사이 다른 에이전트가
        # 낸 새 쪽지가 화면에 올라와 있을 수 있고, 그걸 같이 지우면 답할
        # 곳이 사라집니다.
        shown_id = (self.decision_request or {}).get("id")
        if not event.get("id") or event.get("id") == shown_id:
            self.decision_request = None

    @_reduces(EventType.TOOL_BUDGET_RESOLVED)
    def _on_tool_budget_resolved(self, event: Event) -> None:
        self._clear_answered_decision(event)
        granted = event.get("granted", 0)
        if granted:
            self.status_text = self._pending_prefix(
                f"도구 호출 상한을 {granted}회 늘렸습니다 (총 {event.get('limit', 0)}회). 토론을 이어갑니다."
            )
        elif event.get("outcome") == "timeout":
            self.status_text = self._pending_prefix(
                "도구 상한 확장 요청에 응답이 없어, 지금까지의 관측 결과로 마무리하도록 처리했습니다."
            )
        else:
            self.status_text = self._pending_prefix(
                "도구를 추가로 사용하지 않고 지금까지의 관측 결과로 마무리하도록 처리했습니다."
            )
        self.round_info = "Debating"

    @_reduces(EventType.TOOL_APPROVAL_REQUESTED)
    def _on_tool_approval_requested(self, event: Event) -> None:
        card = _payload(event)
        self.approvals = [a for a in self.approvals if a.get("id") != card.get("id")] + [card]
        self.busy = True
        self.status_text = (
            f"[{event.get('agent_name', '')}] `{event.get('tool_name', '')}` 실행 승인을 "
            f"기다립니다 — {event.get('risk_label', '')}."
        )
        self.round_info = "Approval"

    @_reduces(EventType.USER_QUESTION_REQUESTED)
    def _on_user_question_requested(self, event: Event) -> None:
        card = _payload(event)
        self.questions = [q for q in self.questions if q.get("id") != card.get("id")] + [card]
        self.status_text = f"[{event.get('agent_name', '')}] 질문에 대한 답변을 기다리고 있습니다."
        self.round_info = "Question"

    @_reduces(EventType.USER_QUESTION_RESOLVED)
    def _on_user_question_resolved(self, event: Event) -> None:
        self.questions = [q for q in self.questions if q.get("id") != event.get("id")]
        who = event.get("agent_name", "")
        text = (f"[{who}] 질문에 답변이 등록되었습니다." if event.get("answer")
                else f"[{who}] 질문에 답변이 없어 자체 가정을 명시하고 진행합니다.")
        self.status_text = self._pending_prefix(text)
        if not self.questions:
            self.round_info = "Debating"

    @_reduces(EventType.TOOL_APPROVAL_RESOLVED)
    def _on_tool_approval_resolved(self, event: Event) -> None:
        self.approvals = [a for a in self.approvals if a.get("id") != event.get("id")]
        decision = event.get("decision")
        who, tool = event.get("agent_name", ""), event.get("tool_name", "")
        if str(decision).startswith("allow"):
            text = f"[{who}] `{tool}` 실행을 승인했습니다."
        elif decision == "timeout":
            text = f"[{who}] `{tool}` 승인 응답이 없어 실행하지 않았습니다."
        else:
            text = f"[{who}] `{tool}` 실행을 거부했습니다."
        self.status_text = self._pending_prefix(text)
        if not self.approvals:
            self.round_info = "Debating"

    @_reduces(EventType.PLAN_TASKS_STARTED)
    def _on_plan_tasks_started(self, event: Event) -> None:
        self.busy = True
        self.status_text = self._pending_prefix(
            f"[{event.get('agent_name', '')}] 승인받을 태스크 분담표를 정리하는 중..."
        )
        self.round_info = "Plan"

    @_reduces(EventType.PLAN_APPROVAL_REQUESTED)
    def _on_plan_approval_requested(self, event: Event) -> None:
        self.plan_approval = _payload(event)
        self.busy = True
        self.status_text = (
            "계획 승인을 기다립니다 — 태스크를 확인하고 승인하시거나, 의견을 적어 수정을 요청해 주십시오."
        )
        self.round_info = "Plan approval"

    @_reduces(EventType.PLAN_APPROVAL_RESOLVED)
    def _on_plan_approval_resolved(self, event: Event) -> None:
        shown_id = (self.plan_approval or {}).get("id")
        if not event.get("id") or event.get("id") == shown_id:
            self.plan_approval = None
        decision = event.get("decision")
        if decision == "approve":
            text = "계획을 승인했습니다. 토론을 시작합니다."
        elif decision == "revise":
            text = "계획 수정을 요청했습니다. 오케스트레이터가 계획을 다시 씁니다."
        elif decision == "timeout":
            text = "계획 승인에 응답이 없어 턴을 멈춰 둡니다. 아무것도 실행되지 않았습니다."
        else:
            text = "계획 승인을 기다리던 중 정지가 요청되었습니다."
        self.status_text = self._pending_prefix(text)
        self.round_info = "Planning" if decision == "revise" else "Debating"

    @_reduces(EventType.CONTEXT_WINDOW_EXHAUSTED)
    def _on_context_window_exhausted(self, event: Event) -> None:
        self.decision_request = _payload(event)
        self.busy = True
        self.status_text = (
            f"[{event.get('agent_name', '')}] 컨텍스트 창이 가득 찼습니다 — "
            f"한도를 확장하실지, 이전 기록을 생략하고 진행하실지 선택해 주십시오."
        )
        self.round_info = "Context full"

    @_reduces(EventType.CONTEXT_WINDOW_RESOLVED)
    def _on_context_window_resolved(self, event: Event) -> None:
        self._clear_answered_decision(event)
        granted = event.get("granted", 0)
        if granted:
            self.status_text = self._pending_prefix(
                f"컨텍스트 한도를 {granted:,} 토큰 확장했습니다 "
                f"(총 {event.get('window', 0):,}). 이전 기록이 그대로 유지됩니다."
            )
        elif event.get("outcome") == "timeout":
            self.status_text = self._pending_prefix(
                "컨텍스트 확장 요청에 응답이 없어, 이전 기록부터 생략하며 진행합니다."
            )
        else:
            self.status_text = self._pending_prefix(
                "컨텍스트를 확장하지 않고 지금까지의 기록으로 마무리하도록 처리했습니다."
            )
        self.round_info = "Debating"

    @_reduces(EventType.MERMAID_REPAIR_STARTED)
    def _on_mermaid_repair_started(self, event: Event) -> None:
        self.busy = True
        self.status_text = (
            f"[{event.get('agent_name', '')}] 다이어그램 {event.get('broken', 0)}개의 "
            f"문법 오류를 발견하여 재생성하는 중입니다 "
            f"({event.get('attempt', 1)}/{event.get('max_attempts', 1)}회차)."
        )
        self.round_info = "Fixing diagram"

    @_reduces(EventType.MERMAID_REPAIR_FINISHED)
    def _on_mermaid_repair_finished(self, event: Event) -> None:
        if event.get("resolved"):
            self.status_text = (
                f"다이어그램 문법 오류를 {event.get('attempts', 1)}회 만에 수정했습니다."
            )
        else:
            self.status_text = (
                f"다이어그램 {event.get('remaining', 0)}개는 {event.get('attempts', 0)}회 "
                f"수정을 시도했으나 문법 오류가 남아 있습니다. 원본을 보존하였으니 "
                f"산출물 탭에서 직접 확인해 주십시오."
            )
        self.round_info = "Synthesizing"

    @_reduces(EventType.CONTEXT_TRIMMED)
    def _on_context_trimmed(self, event: Event) -> None:
        self.context_dropped = int(event.get("total_dropped") or 0)
        where = "최종 합성 전사" if event.get("where") == "synthesis" else "발언 맥락"
        self.status_text = self._pending_prefix(
            f"{where}에서 기록 {event.get('dropped', 0)}건이 컨텍스트 한도로 "
            f"생략되었습니다 (누적 {self.context_dropped}건)."
        )

    @_reduces(EventType.GRAPH_STEP_STARTED)
    def _on_graph_step_started(self, event: Event) -> None:
        self.busy = True
        nodes = " · ".join(n.get("label", "") for n in event.get("nodes", []))
        self.status_text = self._pending_prefix(
            f"그래프 {event.get('step', 0)}단계 — {nodes} 발언 중..."
        )
        self.round_info = f"Step {event.get('step', 0)}/{event.get('max_steps', 0)}"

    @_reduces(EventType.GRAPH_GATE_DECIDED)
    def _on_graph_gate_decided(self, event: Event) -> None:
        verdict = "예" if event.get("decision") == "yes" else "아니오"
        self.status_text = self._pending_prefix(
            f"판정 “{event.get('label', '')}”: {verdict}"
            + (" (응답을 수신하지 못하여 기본 갈래로 진행)" if event.get("fallback") else "")
        )

    @_reduces(EventType.LEDGER_UPDATE_STARTED)
    def _on_ledger_update_started(self, event: Event) -> None:
        self.busy = True
        self.status_text = self._pending_prefix(
            f"결정 장부 정리 중 ({event.get('reason', '')})..."
        )

    @_reduces(EventType.LEDGER_UPDATED)
    def _on_ledger_updated(self, event: Event) -> None:
        self.decision_ledger = str(event.get("ledger") or "")
        self.status_text = self._pending_prefix(
            f"결정 장부를 갱신했습니다 ({event.get('reason', '')})."
        )

    @_reduces(EventType.LEDGER_UPDATE_FAILED)
    def _on_ledger_update_failed(self, event: Event) -> None:
        self.status_text = self._pending_prefix(
            f"결정 장부를 갱신하지 못하여 이전 장부를 유지합니다 ({event.get('reason', '')})."
        )

    @_reduces(EventType.CONTEXT_SUMMARIZING)
    def _on_context_summarizing(self, event: Event) -> None:
        self.busy = True
        self.status_text = self._pending_prefix(
            f"[{event.get('agent_name', '')}] 컨텍스트 용량이 초과되어 이전 기록 "
            f"{event.get('messages', 0)}건을 요약하는 중..."
        )

    @_reduces(EventType.CONTEXT_SUMMARIZED)
    def _on_context_summarized(self, event: Event) -> None:
        self.status_text = self._pending_prefix(
            f"이전 기록 {event.get('folded', 0)}건을 요약으로 정리했습니다 "
            f"(요약 대상 누적 {event.get('total', 0)}건)."
        )

    @_reduces(EventType.CONTEXT_SUMMARY_FAILED)
    def _on_context_summary_failed(self, event: Event) -> None:
        self.status_text = self._pending_prefix(
            "이전 기록을 요약하지 못하여, 컨텍스트 한도를 초과하는 이전 기록은 생략합니다."
        )

    @_reduces(EventType.ARTIFACTS_SYNTHESIZED)
    def _on_artifacts_synthesized(self, event: Event) -> None:
        self.artifacts = list(event.get("artifacts", []))

    @_reduces(EventType.TURN_COMPLETED)
    def _on_turn_completed(self, event: Event) -> None:
        self.busy = False
        self.streaming_ids.clear()
        self.decision_request = None
        self.approvals = []
        self.plan_approval = None
        self.questions = []
        failed = event.get("failed_agents") or []
        fallback = event.get("fallback_agents") or []
        if failed:
            self.status_text = f"토론 완료 — 응답하지 못한 에이전트: {', '.join(failed)}"
            self.round_info = "Incomplete"
        elif fallback and not event.get("stopped_early"):
            self.status_text = f"토론 완료 — 기본 모델이 대신 답한 에이전트: {', '.join(fallback)}"
            self.round_info = "Done"
        elif event.get("stopped_early"):
            rounds = event.get("rounds_completed", 0)
            max_rounds = event.get("max_rounds", 0)
            self.status_text = (
                f"사용자 요청으로 정지 — {rounds}/{max_rounds} 라운드까지의 토론으로 "
                f"최종 보고서 합성을 완료했습니다."
            )
            self.round_info = "Stopped"
        elif event.get("converged_round"):
            self.status_text = f"Round {event['converged_round']}에서 조기 합의되어 결론 합성을 완료했습니다."
            self.round_info = "Done"
        else:
            self.status_text = "토론 및 최종 산출물 합성을 완료했습니다."
            self.round_info = "Done"
        # 해당 턴의 소모 토큰 및 LLM 소요 시간 (`usage`). 새로고침된 화면에서도 동일한 상태 라인을 렌더링합니다.
        used = usage_line(event.get("usage"))
        if used:
            self.status_text += f" · {used}"

    def _upsert(self, msg: Dict[str, Any]) -> None:
        msg_id = msg["id"]
        idx = self._message_index.get(msg_id)
        if idx is None:
            self._message_index[msg_id] = len(self.messages)
            self.messages.append(msg)
        else:
            self.messages[idx] = msg

    def snapshot(self) -> Dict[str, Any]:
        """지금 화면을 그리는 데 필요한 전부."""
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "error": self.error,
            "retry_target": self.retry_target,
            "busy": self.busy,
            "status_text": self.status_text,
            "round_info": self.round_info,
            "messages": [dict(m) for m in self.messages],
            "streaming_ids": set(self.streaming_ids),
            "artifacts": [dict(a) for a in self.artifacts],
            "stop_requested": self.control.stop_requested,
            "pending_notes": len(self.control.pending_notes),
            "decision_request": dict(self.decision_request) if self.decision_request else None,
            "tool_approvals": [dict(a) for a in self.approvals],
            "plan_approval": dict(self.plan_approval) if self.plan_approval else None,
            "user_questions": [dict(q) for q in self.questions],
            "context_dropped": self.context_dropped,
            "decision_ledger": self.decision_ledger,
            "graph": self.graph.to_state() if self.graph is not None else None,
        }
