"""토론을 브라우저 세션에서 떼어 내 백그라운드로 굴리는 실행기.

예전에는 `engine.run_turn()` 이 채팅 입력 핸들러 안에서 그대로 await 되었습니다.
그래서 페이지를 새로고침하거나 페르소나 화면에 다녀오면:

* NiceGUI 가 그 클라이언트를 지우고, 코루틴이 붙잡고 있던 슬롯의 부모 엘리먼트가
  사라집니다. 이어지는 UI 갱신은 `The parent element this slot belongs to has been
  deleted.` 로 터졌고, 그 예외가 토론 자체를 중단시켰습니다.
* 살아남더라도 진행 상황을 다시 볼 방법이 없었습니다.

여기서는 토론을 세션 단위 `asyncio.Task` 로 띄우고, 이벤트를 두 갈래로 보냅니다.

1. **정본(canonical) 스냅샷** — 지금까지 나온 발언과 진행 상태를 `TurnRun` 이 들고
   있습니다. 새로 붙는 화면은 이걸 그대로 그리면 됩니다.
2. **구독 큐** — 붙어 있는 화면마다 큐 하나. 화면이 죽으면 큐만 버리고 토론은
   계속됩니다.

UI 쪽 코드는 이 모듈을 import 하지만, 이 모듈은 UI 를 전혀 모릅니다. 엔진 태스크가
NiceGUI 엘리먼트를 건드릴 일이 없으니 클라이언트가 사라져도 터질 곳이 없습니다.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from app.config import resolve_workspace_dir
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine, get_orchestrator_engine
from app.orchestration.graph_run import GraphRunTracker

logger = logging.getLogger(__name__)

# 구독 큐 상한. 브라우저 하나가 느려도 토론을 붙잡지 않도록 넉넉히 잡되,
# 무한히 쌓이지는 않게 합니다. 넘치면 그 화면은 스냅샷으로 다시 맞춥니다.
MAX_QUEUED_EVENTS = 2000

# 취소를 요청한 뒤 태스크가 실제로 멈추기를 기다리는 한도(초).
#
# 무한정 기다리면 안 됩니다. 도구 호출 하나가 취소에 반응하지 않는 순간
# 서버 종료가 통째로 멈추고, uvicorn 이 강제로 내려가면서 "백엔드가 죽었다" 로
# 보입니다. 정리하지 못한 태스크는 데몬처럼 남겨 두고 넘어갑니다.
CANCEL_TIMEOUT = 20.0


# 예전에는 여기에 `WorkspaceConflictError` 가 있었습니다. MCP 서버가 프로세스
# 전체에 한 벌뿐이라 작업 공간이 다른 토론을 동시에 돌릴 수 없었고, 조용히 남의
# 폴더를 쓰느니 두 번째 토론을 거절했습니다.
#
# 지금은 작업 공간마다 서버 묶음이 따로 뜹니다(`app/mcp/pool.py`). 그래서 거절할
# 이유가 없어졌습니다. 자리가 모자랄 때의 거절은 풀이 `RuntimeCapacityError` 로
# 합니다 — 이유가 "동시에 두 폴더를 쓸 수 없어서" 가 아니라 "지금 그만큼 띄울
# 예산이 없어서" 로 바뀌었습니다.


class TurnRun:
    """진행 중이거나 방금 끝난 토론 한 턴."""

    def __init__(self, session_id: str, user_prompt: str, workspace: Optional[str] = None):
        self.session_id = session_id
        self.user_prompt = user_prompt
        self.workspace = resolve_workspace_dir(workspace or None)
        self.status: str = "running"  # running | completed | failed | cancelled
        self.error: Optional[str] = None
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

        # 컨텍스트 한도로 생략된 기록의 누적 건수. 화면이 "얼마나 잃었는지" 를
        # 계속 보여주기 위해 스냅샷에 남깁니다.
        self.context_dropped: int = 0

        # 이번 턴에 갱신된 결정 장부. None 이면 아직 갱신되지 않은 것이고, 화면은 DB 의
        # 값을 그대로 씁니다 (장부는 턴이 끝날 때 저장됩니다).
        self.decision_ledger: Optional[str] = None

        # 그래프 토론이면 어느 노드가 돌고 있는지. 새로고침한 화면이 실행 표시를 다시 그립니다.
        # 방문 횟수와 판정 갈래는 발언 기록에서 세므로, 여기에는 기록으로 알 수 없는 것만 있습니다.
        self.graph: Optional[GraphRunTracker] = None

    @property
    def budget_request(self) -> Optional[Dict[str, Any]]:
        """예전 이름. 도구 예산 전용이던 시절의 호출부를 깨뜨리지 않습니다."""
        return self.decision_request

    @budget_request.setter
    def budget_request(self, value: Optional[Dict[str, Any]]) -> None:
        self.decision_request = value

    # -------------------------------------------------- 구독

    def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _fanout(self, event: Dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 화면이 따라오지 못하고 있습니다. 큐를 버리면 그 화면은 다음
                # 스냅샷에서 복구됩니다. 토론은 여기서 멈추지 않습니다.
                logger.warning("Dropping a slow debate subscriber for session %s", self.session_id)
                self._subscribers.discard(queue)

    # -------------------------------------------------- 사람의 개입

    def request_stop(self) -> bool:
        """남은 라운드를 접고 지금까지의 토론으로 합성하도록 요청합니다.

        `DebateRunner.cancel()` 과 다릅니다. 태스크를 죽이지 않으므로 진행 중인
        발언이 중간에 잘리지 않고, 최종 합성과 아티팩트도 그대로 나옵니다.
        이미 요청했거나 끝난 토론이면 아무것도 하지 않고 False 를 돌려줍니다.
        """
        if self.status != "running" or self.control.stop_requested:
            return False
        self.control.request_stop()
        self._emit({"type": "stop_requested"})
        return True

    def interject(self, text: str) -> bool:
        """토론 중인 에이전트들에게 사용자 메시지를 끼워 넣습니다.

        곧바로 반영되지는 않습니다. 지금 발언 중인 에이전트의 프롬프트는 이미
        만들어져 나갔으므로, 엔진이 다음 발언자로 넘어가는 지점에서 꺼내 갑니다.
        """
        if self.status != "running":
            return False
        if not self.control.add_note(text):
            return False
        self._emit({
            "type": "interjection_queued",
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
            "type": f"{kind}_resolved",
            "id": request_id or shown.get("id"),
            "agent_name": shown.get("agent_name", ""),
            "outcome": "extended" if extra > 0 else "wrap_up",
            "granted": max(0, int(extra)),
            "window": int(shown.get("limit", 0)) + max(0, int(extra)),
            "by_user": True,
        })
        return True

    def resolve_tool_budget(self, extra: int, request_id: Optional[str] = None) -> bool:
        """예전 이름. `resolve_decision` 과 같습니다."""
        return self.resolve_decision(extra, request_id)

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

    # -------------------------------------------------- 상태 적용

    def _emit(self, event: Dict[str, Any]) -> None:
        """엔진이 아니라 사람이 만든 이벤트를 스냅샷과 구독자에게 함께 보냅니다."""
        self.apply(event)
        self._fanout(event)

    def _pending_prefix(self, text: str) -> str:
        """정지를 기다리는 중이라는 표시. 다음 상태 문구가 덮어써도 계속 붙습니다."""
        return f"(정지 대기) {text}" if self.control.stop_requested else text

    def apply(self, event: Dict[str, Any]) -> None:
        """이벤트를 정본 스냅샷에 반영합니다. 새로 붙는 화면이 이걸 그립니다."""
        etype = event.get("type")
        if etype == "graph_started":
            self.graph = GraphRunTracker()
        if self.graph is not None:
            self.graph.observe(event)

        if etype == "status_changed":
            speaker = event.get("speaker", "")
            status = event.get("status", "")
            round_num = event.get("round", "")
            if status:
                self.phase = status
            self.busy = True
            label = f"[{speaker}] 발언 및 분석 중..." if speaker else f"상태: {status}"
            self.status_text = self._pending_prefix(label)
            self.round_info = f"Round {round_num}" if round_num else "Debating"

        elif etype == "round_started":
            r = event.get("round", 1)
            mr = event.get("max_rounds", 3)
            self.busy = True
            self.status_text = self._pending_prefix(f"Round {r}/{mr} 전문가 토론 진행 중...")
            self.round_info = f"Round {r}/{mr}"

        elif etype == "message_stream_start":
            msg = dict(event.get("message", {}))
            msg_id = msg.get("id", "")
            if msg_id:
                self._upsert(msg)
                self.streaming_ids.add(msg_id)

        elif etype == "message_stream_chunk":
            msg_id = event.get("message_id", "")
            idx = self._message_index.get(msg_id)
            if idx is not None:
                self.messages[idx]["content"] += event.get("delta", "")

        elif etype == "message_added":
            msg = dict(event.get("message", {}))
            msg_id = msg.get("id", "")
            if msg_id:
                self._upsert(msg)
                self.streaming_ids.discard(msg_id)

        elif etype == "stop_requested":
            self.busy = True
            self.status_text = "정지 요청됨 — 진행 중인 발언을 마친 뒤 지금까지의 토론으로 합성합니다."
            self.round_info = "Stopping"

        elif etype == "interjection_queued":
            pending = event.get("pending", 0)
            if event.get("deferred"):
                self.status_text = self._pending_prefix(
                    f"유저 개입 {pending}건 — 최종 합성 중이라 다음 요청부터 반영됩니다."
                )
            else:
                self.status_text = self._pending_prefix(
                    f"유저 개입 {pending}건 대기 — 다음 발언 차례에 반영됩니다."
                )

        elif etype == "interjections_deferred":
            count = event.get("count", 0)
            self.status_text = (
                f"개입 {count}건은 합성 이후에 도착해 기록에만 남았습니다 (다음 요청에 반영)."
            )

        elif etype == "tool_budget_exhausted":
            self.decision_request = {k: v for k, v in event.items() if k != "type"}
            self.busy = True
            self.status_text = (
                f"[{event.get('agent_name', '')}] 도구 호출 상한 {event.get('limit', 0)}회를 "
                f"모두 썼습니다 — 상한을 늘릴지, 지금까지의 관측으로 마무리할지 골라 주세요."
            )
            self.round_info = "Tool limit"

        elif etype == "tool_budget_resolved":
            # 답이 난 쪽지만 걷습니다. 병렬 라운드에서는 그 사이 다른 에이전트가
            # 낸 새 쪽지가 화면에 올라와 있을 수 있고, 그걸 같이 지우면 답할
            # 곳이 사라집니다.
            shown_id = (self.decision_request or {}).get("id")
            if not event.get("id") or event.get("id") == shown_id:
                self.decision_request = None
            granted = event.get("granted", 0)
            outcome = event.get("outcome")
            if granted:
                self.status_text = self._pending_prefix(
                    f"도구 호출 상한을 {granted}회 늘렸습니다 (총 {event.get('limit', 0)}회). 토론을 이어갑니다."
                )
            elif outcome == "timeout":
                self.status_text = self._pending_prefix(
                    "도구 상한 확장 요청에 답이 없어, 지금까지의 관측으로 마무리하도록 했습니다."
                )
            else:
                self.status_text = self._pending_prefix(
                    "도구를 더 쓰지 않고 지금까지의 관측으로 마무리하도록 했습니다."
                )
            self.round_info = "Debating"

        elif etype == "tool_approval_requested":
            card = {k: v for k, v in event.items() if k != "type"}
            self.approvals = [a for a in self.approvals if a.get("id") != card.get("id")] + [card]
            self.busy = True
            self.status_text = (
                f"[{event.get('agent_name', '')}] `{event.get('tool_name', '')}` 실행 승인을 "
                f"기다립니다 — {event.get('risk_label', '')}."
            )
            self.round_info = "Approval"

        elif etype == "tool_approval_resolved":
            self.approvals = [a for a in self.approvals if a.get("id") != event.get("id")]
            decision = event.get("decision")
            who, tool = event.get("agent_name", ""), event.get("tool_name", "")
            if str(decision).startswith("allow"):
                text = f"[{who}] `{tool}` 실행을 허락했습니다."
            elif decision == "timeout":
                text = f"[{who}] `{tool}` 승인에 답이 없어 실행하지 않았습니다."
            else:
                text = f"[{who}] `{tool}` 실행을 거부했습니다."
            self.status_text = self._pending_prefix(text)
            if not self.approvals:
                self.round_info = "Debating"

        elif etype == "context_window_exhausted":
            self.decision_request = {k: v for k, v in event.items() if k != "type"}
            self.busy = True
            self.status_text = (
                f"[{event.get('agent_name', '')}] 컨텍스트 창이 가득 찼습니다 — "
                f"한도를 넓힐지, 오래된 기록을 생략하고 진행할지 골라 주세요."
            )
            self.round_info = "Context full"

        elif etype == "context_window_resolved":
            shown_id = (self.decision_request or {}).get("id")
            if not event.get("id") or event.get("id") == shown_id:
                self.decision_request = None
            granted = event.get("granted", 0)
            if granted:
                self.status_text = self._pending_prefix(
                    f"컨텍스트 한도를 {granted:,} 토큰 넓혔습니다 "
                    f"(총 {event.get('window', 0):,}). 앞선 기록이 그대로 남습니다."
                )
            elif event.get("outcome") == "timeout":
                self.status_text = self._pending_prefix(
                    "컨텍스트 확장 요청에 답이 없어, 오래된 기록부터 생략하며 진행합니다."
                )
            else:
                self.status_text = self._pending_prefix(
                    "컨텍스트를 넓히지 않고 지금까지의 기록으로 마무리하도록 했습니다."
                )
            self.round_info = "Debating"

        elif etype == "mermaid_repair_started":
            self.busy = True
            self.status_text = (
                f"[{event.get('agent_name', '')}] 다이어그램 {event.get('broken', 0)}개의 "
                f"문법 오류를 발견해 다시 그리는 중입니다 "
                f"({event.get('attempt', 1)}/{event.get('max_attempts', 1)}회차)."
            )
            self.round_info = "Fixing diagram"

        elif etype == "mermaid_repair_finished":
            if event.get("resolved"):
                self.status_text = (
                    f"다이어그램 문법 오류를 {event.get('attempts', 1)}회 만에 고쳤습니다."
                )
            else:
                self.status_text = (
                    f"다이어그램 {event.get('remaining', 0)}개는 {event.get('attempts', 0)}번 "
                    f"고쳐 봐도 문법 오류가 남았습니다. 원본을 그대로 두었으니 "
                    f"아티팩트 탭에서 직접 확인하세요."
                )
            self.round_info = "Synthesizing"

        elif etype == "context_trimmed":
            self.context_dropped = int(event.get("total_dropped") or 0)
            where = "최종 합성 전사" if event.get("where") == "synthesis" else "발언 맥락"
            self.status_text = self._pending_prefix(
                f"{where}에서 기록 {event.get('dropped', 0)}건이 컨텍스트 한도로 "
                f"생략됐습니다 (누적 {self.context_dropped}건)."
            )

        elif etype == "graph_step_started":
            self.busy = True
            nodes = " · ".join(n.get("label", "") for n in event.get("nodes", []))
            self.status_text = self._pending_prefix(
                f"그래프 {event.get('step', 0)}단계 — {nodes} 발언 중..."
            )
            self.round_info = f"Step {event.get('step', 0)}/{event.get('max_steps', 0)}"

        elif etype == "graph_gate_decided":
            verdict = "예" if event.get("decision") == "yes" else "아니오"
            self.status_text = self._pending_prefix(
                f"판정 “{event.get('label', '')}”: {verdict}"
                + (" (응답을 읽지 못해 기본 갈래)" if event.get("fallback") else "")
            )

        elif etype == "ledger_update_started":
            self.busy = True
            self.status_text = self._pending_prefix(
                f"결정 장부 정리 중 ({event.get('reason', '')})..."
            )

        elif etype == "ledger_updated":
            self.decision_ledger = str(event.get("ledger") or "")
            self.status_text = self._pending_prefix(
                f"결정 장부를 갱신했습니다 ({event.get('reason', '')})."
            )

        elif etype == "ledger_update_failed":
            self.status_text = self._pending_prefix(
                f"결정 장부를 갱신하지 못해 이전 장부를 유지합니다 ({event.get('reason', '')})."
            )

        elif etype == "context_summarizing":
            self.busy = True
            self.status_text = self._pending_prefix(
                f"[{event.get('agent_name', '')}] 컨텍스트가 차서 앞선 기록 "
                f"{event.get('messages', 0)}건을 요약으로 접는 중..."
            )

        elif etype == "context_summarized":
            self.status_text = self._pending_prefix(
                f"앞선 기록 {event.get('folded', 0)}건을 요약으로 접었습니다 "
                f"(요약이 덮는 기록 누적 {event.get('total', 0)}건)."
            )

        elif etype == "context_summary_failed":
            self.status_text = self._pending_prefix(
                "앞선 기록을 요약하지 못해, 컨텍스트 한도를 넘는 오래된 기록은 생략합니다."
            )

        elif etype == "artifacts_synthesized":
            self.artifacts = list(event.get("artifacts", []))

        elif etype == "turn_completed":
            self.busy = False
            self.streaming_ids.clear()
            self.decision_request = None
            self.approvals = []
            failed = event.get("failed_agents") or []
            if failed:
                self.status_text = f"토론 완료 — 응답하지 못한 에이전트: {', '.join(failed)}"
                self.round_info = "Incomplete"
            elif event.get("stopped_early"):
                rounds = event.get("rounds_completed", 0)
                max_rounds = event.get("max_rounds", 0)
                self.status_text = (
                    f"유저 요청으로 정지 — {rounds}/{max_rounds} 라운드까지의 토론으로 "
                    f"합성을 마쳤습니다."
                )
                self.round_info = "Stopped"
            else:
                self.status_text = "토론 완료 및 최종 아티팩트 합성 완료"
                self.round_info = "Done"

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
            "status": self.status,
            "error": self.error,
            "busy": self.busy,
            "status_text": self.status_text,
            "round_info": self.round_info,
            "messages": [dict(m) for m in self.messages],
            "streaming_ids": set(self.streaming_ids),
            "artifacts": [dict(a) for a in self.artifacts],
            "stop_requested": self.control.stop_requested,
            "pending_notes": len(self.control.pending_notes),
            "decision_request": dict(self.decision_request) if self.decision_request else None,
            # 예전 이름. 스냅샷을 읽는 오래된 코드가 있어도 깨지지 않게 둡니다.
            "budget_request": dict(self.decision_request) if self.decision_request else None,
            "tool_approvals": [dict(a) for a in self.approvals],
            "context_dropped": self.context_dropped,
            "decision_ledger": self.decision_ledger,
            "graph": self.graph.to_state() if self.graph is not None else None,
        }


class DebateRunner:
    """세션별 토론 태스크의 소유자. 프로세스 전체에서 하나만 씁니다."""

    def __init__(self, engine: Optional[OrchestratorEngine] = None):
        self._engine = engine
        self._runs: Dict[str, TurnRun] = {}

    @property
    def engine(self) -> OrchestratorEngine:
        if self._engine is None:
            self._engine = get_orchestrator_engine()
        return self._engine

    def get(self, session_id: str) -> Optional[TurnRun]:
        return self._runs.get(session_id)

    def is_running(self, session_id: str) -> bool:
        run = self._runs.get(session_id)
        return run is not None and run.status == "running"

    def running_sessions(self) -> List[str]:
        """지금 토론이 돌고 있는 대화의 id.

        conf.json 처럼 **모든 런타임의 정본**인 설정을 화면에서 잠글지 판단할 때
        씁니다. 서버 프로세스는 이제 작업 공간마다 나뉘지만, 그것들이 무엇을 어떻게
        띄울지는 여전히 파일 하나가 정합니다.
        """
        return [sid for sid, run in self._runs.items() if run.status == "running"]

    def running_elsewhere(self, session_id: str) -> List[TurnRun]:
        """이 세션이 아닌 다른 세션에서 돌고 있는 토론."""
        return [r for sid, r in self._runs.items()
                if sid != session_id and r.status == "running"]

    def start(self, session_id: str, user_prompt: str,
              workspace: Optional[str] = None) -> TurnRun:
        """토론을 백그라운드에서 시작합니다. 이미 돌고 있으면 그 실행을 돌려줍니다."""
        existing = self._runs.get(session_id)
        if existing is not None and existing.status == "running":
            return existing

        run = TurnRun(session_id, user_prompt, workspace)
        self._runs[session_id] = run

        async def on_event(event: Dict[str, Any]) -> None:
            run.apply(event)
            run._fanout(event)  # noqa: SLF001 - 같은 모듈 안의 협력 객체입니다

        async def driver() -> None:
            try:
                await self.engine.run_turn(
                    session_id=session_id, user_prompt=user_prompt, on_event=on_event,
                    control=run.control,
                )
                run.status = "completed"
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.error = "토론이 취소되었습니다."
                raise
            except BaseException as exc:  # noqa: BLE001 - 어떤 실패든 화면에 알려야 합니다
                # `Exception` 이 아니라 `BaseException` 입니다. anyio 로 도구
                # 서버를 다루는 경로는 `BaseExceptionGroup` 을 올리는데, 그건
                # Exception 이 아니라서 예전에는 여기를 그냥 지나갔습니다. 그러면
                # 태스크가 아무 흔적 없이 죽고 화면은 "토론 중..." 에 영원히
                # 멈춰 있었습니다.
                logger.error(
                    f"Debate turn failed for session {session_id}: "
                    f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                run.status = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
            finally:
                run.busy = False
                run.streaming_ids.clear()
                if run.status == "failed":
                    run.status_text = f"오류로 중단됨: {run.error}"
                    run.round_info = "Error"
                elif run.status == "cancelled":
                    run.status_text = "토론이 취소되었습니다."
                    run.round_info = "Cancelled"
                try:
                    run._fanout({  # noqa: SLF001
                        "type": "run_finished",
                        "status": run.status,
                        "error": run.error,
                    })
                except BaseException:  # noqa: BLE001 - 알리다 실패해도 태스크는 조용히 끝냅니다
                    logger.warning(
                        "Could not announce the end of the debate for session %s",
                        session_id, exc_info=True,
                    )

        # asyncio.create_task 로 띄운 태스크는 NiceGUI 슬롯 스택을 물려받지 않습니다.
        # 즉 이 안에서는 UI 엘리먼트를 만들 수 없고, 만들 일도 없습니다.
        run.task = asyncio.create_task(driver(), name=f"debate-{session_id}")
        return run

    def request_stop(self, session_id: str) -> bool:
        """진행 중인 토론을 지금까지의 내용으로 마무리하도록 요청합니다."""
        run = self._runs.get(session_id)
        return run.request_stop() if run is not None else False

    async def abort(self, session_id: str) -> Optional[Dict[str, Any]]:
        """토론을 즉시 끊고, 이 턴이 남긴 것의 목록을 돌려줍니다.

        `request_stop()` 과 정반대입니다. 정지는 "여기까지의 논의로 결론을
        내라" 는 뜻이라 진행 중인 발언을 끝까지 받고 합성까지 갑니다. 여기서는
        요청 **자체가 틀렸을** 때를 다룹니다 — 그 답을 기다릴 이유가 없으므로
        발언 도중이라도 끊고, 지운 자리를 사람이 다시 쓰게 합니다.

        지우는 일은 하지 않습니다. 무엇을 지워야 하는지만 알려 줍니다 (기록은
        DB 를 아는 쪽의 몫입니다). 돌려주는 값:

            {"prompt": 사람이 보냈던 글, "message_ids": [...], "artifact_ids": [...]}

        진행 중인 토론이 없으면 None.
        """
        run = self._runs.get(session_id)
        if run is None or run.status != "running":
            return None

        # 태스크를 죽이기 전에 목록을 뜹니다. 취소 뒤에도 스냅샷은 남지만,
        # 순서를 지켜야 "무엇이 있었는지" 를 놓치지 않습니다.
        produced = {
            "prompt": run.user_prompt,
            "message_ids": [m.get("id") for m in run.messages if m.get("id")],
            "artifact_ids": [a.get("id") for a in run.artifacts if a.get("id")],
        }
        await self.cancel(session_id)
        logger.info(
            f"Debate for session {session_id} was aborted by the user; "
            f"{len(produced['message_ids'])} message(s) will be discarded."
        )
        return produced

    def interject(self, session_id: str, text: str) -> bool:
        """진행 중인 토론에 사용자 메시지를 끼워 넣습니다."""
        run = self._runs.get(session_id)
        return run.interject(text) if run is not None else False

    def resolve_decision(self, session_id: str, extra: int,
                         request_id: Optional[str] = None) -> bool:
        """한도에 닿은 에이전트에게 확장 여부를 알려 줍니다 (도구 상한·컨텍스트 공통)."""
        run = self._runs.get(session_id)
        return run.resolve_decision(extra, request_id) if run is not None else False

    def resolve_tool_budget(self, session_id: str, extra: int,
                            request_id: Optional[str] = None) -> bool:
        """예전 이름. `resolve_decision` 과 같습니다."""
        return self.resolve_decision(session_id, extra, request_id)

    async def set_tool_rules(self, session_id: str, grants: List[str], denials: List[str]) -> bool:
        """진행 중인 토론의 "이 대화에서" 규칙을 바꿉니다. 도는 토론이 없으면 False.

        False 면 저장은 부른 쪽(화면)이 합니다. 도는 토론이 있으면 그 게이트가 저장합니다
        (`OrchestratorEngine.set_tool_rules`).
        """
        run = self._runs.get(session_id)
        if run is None or run.status != "running":
            return False
        return await self.engine.set_tool_rules(session_id, grants, denials)

    def set_tool_mode(self, session_id: str, mode: str) -> bool:
        """진행 중인 토론의 도구 보안 모드를 바꿉니다 (`OrchestratorEngine.set_tool_mode`)."""
        if session_id not in self._runs or self._runs[session_id].status != "running":
            return False
        return self.engine.set_tool_mode(session_id, mode)

    def resolve_tool_approval(self, session_id: str, request_id: str, decision: str, *,
                              scope: Optional[List[str]] = None, reason: str = "",
                              approver: str = "") -> bool:
        """도구 승인 카드에 답합니다 (`TurnRun.resolve_tool_approval`)."""
        run = self._runs.get(session_id)
        if run is None:
            return False
        return run.resolve_tool_approval(
            request_id, decision, scope=scope, reason=reason, approver=approver,
        )

    async def cancel(self, session_id: str) -> bool:
        run = self._runs.get(session_id)
        if run is None or run.task is None or run.task.done():
            return False
        task = run.task
        task.cancel()
        # `await task` 가 아니라 `asyncio.wait` 입니다. 전자는 취소된 태스크의
        # CancelledError 를 이 코루틴 쪽으로 다시 올려서, 정작 취소되지 않은
        # 호출자(서버 종료 경로)까지 취소된 것처럼 보이게 만듭니다. 후자는
        # 무엇으로 끝났든 예외를 올리지 않습니다.
        done, _pending = await asyncio.wait({task}, timeout=CANCEL_TIMEOUT)
        if not done:
            # 취소를 흡수하고 놓아주지 않는 코드가 어딘가 있다는 뜻입니다.
            # 여기서 계속 기다리면 서버 종료(lifespan)가 그 자리에서 멈추고,
            # uvicorn 이 강제로 내려가면서 "백엔드가 죽었다" 로 보입니다.
            logger.warning(
                "Debate task for session %s did not stop within %.0fs; leaving it behind",
                session_id, CANCEL_TIMEOUT,
            )
            return True
        if not task.cancelled():
            # 예외를 한 번은 읽어 두어야 "Task exception was never retrieved" 가
            # 로그를 어지럽히지 않습니다.
            exc = task.exception()
            if exc is not None:
                logger.debug(
                    "Cancelled debate task for session %s ended with %s",
                    session_id, type(exc).__name__,
                )
        return True

    async def shutdown(self) -> None:
        """서버 종료 시 남은 토론 태스크를 정리합니다."""
        for session_id in list(self._runs):
            await self.cancel(session_id)

    def forget(self, session_id: str) -> None:
        """세션이 삭제됐을 때 스냅샷까지 버립니다."""
        run = self._runs.pop(session_id, None)
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()


_runner: Optional[DebateRunner] = None


def get_debate_runner() -> DebateRunner:
    global _runner
    if _runner is None:
        _runner = DebateRunner()
    return _runner
