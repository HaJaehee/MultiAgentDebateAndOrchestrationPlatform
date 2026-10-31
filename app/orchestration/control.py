"""실행 중인 토론에 사용자가 개입(Interjection)하는 통로.

토론 태스크는 기동 후 완료 시점까지 자율 실행되는 구조로 설계되었습니다. 그래서
"지금까지만 정리하고 멈춰" 나 "그 방향 말고 이쪽으로" 를 전할 방법이 없었고,
할 수 있는 것은 태스크를 통째로 죽이는 것(`DebateRunner.cancel()`)뿐이었습니다.
그러면 진행 중이던 발언은 잘리고 최종 합성 단계는 아예 돌지 않아, 지금까지의
토론으로 산출물을 뽑을 기회가 사라집니다.

`TurnControl`은 그 사이에 위치하는 스레드 안전 메시지 큐입니다. UI 코루틴이
여기에 요청을 넣어 두면, 엔진이 발언과 발언 사이의 안전한 지점에서 꺼내 봅니다.

* **정지 요청** — 태스크를 죽이지 않습니다. 진행 중인 발언은 끝까지 받고,
  남은 라운드를 건너뛰어 곧장 최종 합성으로 넘어갑니다.
* **개입 메모** — 다음 발언자의 맥락에 유저 발언으로 끼어듭니다. 메모에 @전문가 @스킬 지정이
  있었으면 그것도 함께 넘어가, 남은 발언부터 그 전문가에게 스킬이 미리 불립니다.
* **도구 예산 확장 요청** — 여기서는 방향이 반대입니다. 도구 호출 상한을 다 쓴
  에이전트가 사용자에게 결정 요청을 전달하고, 응답(상한 확장 / 즉시 마무리)이 올 때까지
  해당 발언이 대기합니다. 응답이 없으면 타임아웃 후 자체 마무리로 전이합니다.
* **도구 승인 요청** — 도구 보안 판정이 "묻기" 인 호출. 답(이번만 / 이 대화에서 /
  항상 허용, 거부)이 올 때까지 그 발언만 기다리고, 답이 없으면 거부합니다
  (`app/orchestration/tool_gate.py`).
* **계획 승인 요청** — 오케스트레이터의 계획이 나온 뒤, 토론이 시작되기 전 (ADR-028).
  답(승인 / 수정 요청)이 올 때까지 턴 전체가 기다립니다. 답이 없으면 아무것도 실행하지
  않고 턴을 멈춰 둡니다 (`app/orchestration/plan_gate.py`).

경계를 넘어 공유되는 상태는 이 객체 하나뿐이고, 같은 이벤트 루프 안에서만
읽고 쓰므로 락이 필요 없습니다. 엔진은 이 모듈만 알면 되고 UI 도 러너도
모릅니다.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# 도구 예산 소진 시 사용자 응답을 대기하는 타임아웃(초).
#
# 무한정 기다리면 아무도 화면을 보고 있지 않은 토론이 그 자리에서 영영 멈춥니다.
# 시간이 지나면 "확장 없음" 으로 보고, 에이전트에게 지금까지 얻은 것으로 즉시
# 결론을 내라고 합니다 (대화를 버리지는 않습니다).
TOOL_BUDGET_WAIT_SECONDS = 180.0

# 화면의 확장 버튼 한 번이 더해 주는 도구 호출 횟수.
TOOL_BUDGET_EXTENSION_STEP = 15

# 모델의 실제 한도를 조회할 수 없을 때 제안하는 컨텍스트 확장 폭 (현재값 대비).
#
# 사설 게이트웨이·vLLM·별칭 모델은 `litellm.get_model_info` 에 매핑되어 있지
# 않습니다. 그때는 우리가 한도를 모르므로, 지금 값의 절반을 제안하고 판단은
# 사용자에게 맡깁니다 — 자기 엔드포인트는 사용자가 더 잘 압니다.
CONTEXT_WIDEN_FALLBACK_RATIO = 0.5


def asks_human(control: Optional["TurnControl"]) -> bool:
    """해당 턴에서 사용자 입력/응답을 요청할 수 있는지 확인합니다 (UI 대화형 세션 여부)."""
    return control is not None and control.attended


class TurnControl:
    """단일 턴의 정지 요청 및 개입 메모를 관리하는 제어 큐."""

    def __init__(self, *, attended: bool = True) -> None:
        # 사용자가 UI에서 이 턴을 대화형으로 모니터링하며 응답할 수 있는지 여부 (attended). API 및 MCP를 통한 헤드리스 실행은 기본 False입니다 (`app/api.py`).
        # False인 경우 대화형 승인 다이얼로그(계획/도구 승인, 에이전트 질문 등)를 띄우지 않고 무인 실행 규칙을 따릅니다 (단, 외부 중단 및 개입 메모는 수신 가능).
        self.attended = attended
        self._stop_requested = False
        self._notes: List[str] = []
        # 개입 메모와 함께 온 스킬 지정 (전문가 키 → 스킬). 메모와 같은 때에 꺼내 갑니다.
        self._skill_designations: Dict[str, List[str]] = {}
        # 개입 메모에서 언급한 파일 (작업 공간 기준 경로). 메모와 같은 때에 꺼내 갑니다.
        self._attachments: List[str] = []
        # 사용자 응답을 대기 중인 결정 요청 목록 (id -> 요청 객체). 도구 상한과 컨텍스트 윈도우가
        # 동일한 큐 구조를 공유합니다 — 대기 및 응답 수신 방식이 동일합니다.
        #
        # 보통은 한 장입니다. 예산을 다 쓴 에이전트는 답을 받기 전까지 다음
        # 호출로 넘어가지 않으니까요. 하지만 병렬 지시 전략에서는 여러 명이
        # 동시에 달리므로 같은 순간에 두 장이 걸릴 수 있습니다. 그때 한 장만
        # 들고 있으면 나머지는 답할 방법이 없어져(id 가 안 맞아 거절됩니다)
        # 3분을 통째로 기다린 뒤에야 마무리로 갑니다.
        self._decisions: Dict[str, "DecisionRequest"] = {}

    # -------------------------------------------------- 정지

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def request_stop(self) -> None:
        """다음 안전 지점에서 토론을 접고 합성으로 넘어가도록 표시합니다.

        사용자 응답 대기 중인 결정 요청이 존재하면 (도구 상한 또는 컨텍스트 윈도우)
        즉시 "추가 확장 없음"으로 응답합니다. 정지를 요청한 사용자에게 불필요하게 추가 자원 승인을 묻지 않고,
        대기 중인 도구 승인 요청 역시 즉시 거부(Denied) 처리하여 정리합니다.
        """
        self._stop_requested = True
        for request in self.pending_decisions:
            request.resolve(0, "wrap_up")
        for approval in self.pending_approvals:
            approval.answer("deny", reason="유저가 토론 정지를 요청했습니다.")
        # 계획 승인도 닫습니다. 정지는 "지금까지의 것으로 마무리하라" 이므로, 승인을 기다리던
        # 턴은 토론 없이 곧장 합성으로 갑니다 (계획 전에 정지를 누른 턴과 같습니다).
        plan = self.pending_plan_approval
        if plan is not None:
            plan.resolve(0, "wrap_up")

    # -------------------------------------------------- 개입

    @property
    def pending_notes(self) -> List[str]:
        """아직 토론에 반영되지 않고 대기 중인 개입 메모."""
        return list(self._notes)

    def add_note(
        self,
        text: str,
        skill_designations: Optional[Dict[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
    ) -> bool:
        """개입 메모를 대기열에 넣습니다. 빈 문자열은 무시하고 False 를 돌려줍니다."""
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        self._notes.append(cleaned)
        for key, skills in (skill_designations or {}).items():
            bucket = self._skill_designations.setdefault(key, [])
            bucket.extend(s for s in skills if s not in bucket)
        self._attachments.extend(p for p in (attachments or []) if p not in self._attachments)
        return True

    def drain_notes(self) -> List[str]:
        """대기 중인 메모를 전부 꺼내고 대기열을 비웁니다."""
        notes, self._notes = self._notes, []
        return notes

    def drain_skill_designations(self) -> Dict[str, List[str]]:
        """메모와 함께 온 스킬 지정을 전부 꺼내고 비웁니다."""
        designations, self._skill_designations = self._skill_designations, {}
        return designations

    def drain_attachments(self) -> List[str]:
        """메모와 함께 온 첨부 경로를 전부 꺼내고 비웁니다."""
        attachments, self._attachments = self._attachments, []
        return attachments

    # -------------------------------------------------- 결정 요청 (공통)

    @property
    def pending_decision(self) -> Optional["DecisionRequest"]:
        """대기 중인 결정 요청 중 가장 최신 요청을 반환합니다. UI에 기본 표시됩니다."""
        pending = self.pending_decisions
        return pending[-1] if pending else None

    @property
    def pending_decisions(self) -> List["DecisionRequest"]:
        """대기 중인 자원 한도 결정 요청 목록을 반환합니다 (도착 순). 도구 실행 승인 요청은 분리 관리합니다.

        두 요청 유형을 혼합할 경우 요청 ID 없이 전달된 한도 확장이 도구 승인 요청에 잘못 라우팅되는 것을 방지합니다.
        """
        return [
            r for r in self._decisions.values()
            if not r.resolved
            and not isinstance(r, (ToolApprovalRequest, PlanApprovalRequest, UserQuestionRequest))
        ]

    @property
    def pending_approvals(self) -> List["ToolApprovalRequest"]:
        """대기 중인 도구 실행 승인 요청 목록을 반환합니다 (도착 순)."""
        return [
            r for r in self._decisions.values()
            if not r.resolved and isinstance(r, ToolApprovalRequest)
        ]

    @property
    def pending_plan_approval(self) -> Optional["PlanApprovalRequest"]:
        """대기 중인 계획 승인 요청을 반환합니다 (턴당 최대 1건).

        한도 결정 및 도구 승인 요청과 분리 관리하여 상한 확장이나 도구 승인 액션이 계획을 오승인하지 않도록 격리합니다.
        """
        for request in self._decisions.values():
            if not request.resolved and isinstance(request, PlanApprovalRequest):
                return request
        return None

    async def _ask(self, request: "DecisionRequest", timeout: float, on_open) -> "DecisionRequest":
        """결정 요청을 등록하고 사용자 응답을 대기합니다. 요청 종류와 무관하게 동일한 파이프라인을 따릅니다."""
        if self._stop_requested or not request.answerable:
            request.resolve(0, "wrap_up")
            return request

        request.wait_seconds = timeout
        request.opened_at = time.time()
        self._decisions[request.id] = request
        try:
            if on_open is not None:
                await on_open(request)
            await request.wait(timeout)
        finally:
            self._decisions.pop(request.id, None)
        return request

    def resolve_decision(self, extra: int, request_id: Optional[str] = None) -> bool:
        """대기 중인 한도 결정 요청에 응답합니다. `extra`가 0이면 즉시 마무리 지시입니다.

        `request_id` 지정 시 해당 요청에 한정하여 응답합니다. 화면의 지연된 상태로 인해
        타 에이전트의 자원 상한이 잘못 증액되는 오류를 방지합니다. 미지정 시 최신 요청에 응답합니다.
        """
        if request_id is not None:
            request = self._decisions.get(request_id)
        else:
            request = self.pending_decision
        if request is None or isinstance(request, (ToolApprovalRequest, PlanApprovalRequest)):
            return False
        return request.resolve(extra, "extended" if extra > 0 else "wrap_up")

    # -------------------------------------------------- 컨텍스트 창

    async def ask_context_window(
        self,
        *,
        agent_key: str,
        agent_name: str,
        window: int,
        used: int,
        headroom: Optional[int],
        tool_calls: int = 0,
        timeout: float = TOOL_BUDGET_WAIT_SECONDS,
        on_open=None,
    ) -> "DecisionRequest":
        """컨텍스트 창이 넘쳤음을 알리고 답을 기다립니다.

        `headroom` 은 모델의 **실제** 한도까지 남은 여유입니다. 도구 상한과 달리
        컨텍스트는 우리가 정하는 숫자가 아니라서, 실제 한도를 넘겨 올리면 깔끔한
        트림이 엔드포인트 400 으로 바뀝니다. 그래서 조회된 여유를 넘는 확장은
        제안하지 않습니다. 한도를 모르면(`None`) 지금 값의 절반을 제안하고 판단은
        사용자에게 맡깁니다.
        """
        if headroom is None:
            step = max(1, int(window * CONTEXT_WIDEN_FALLBACK_RATIO))
            max_extension = step
        else:
            max_extension = max(0, headroom)
            step = min(max_extension, max(1, int(window * CONTEXT_WIDEN_FALLBACK_RATIO)))

        request = DecisionRequest(
            request_id=uuid.uuid4().hex,
            kind="context_window",
            agent_key=agent_key,
            agent_name=agent_name,
            limit=window,
            used=used,
            tool_calls=tool_calls,
            extension_step=step,
            max_extension=max_extension,
            payload={"headroom_known": headroom is not None},
        )
        return await self._ask(request, timeout, on_open)

    # -------------------------------------------------- 도구 예산

    async def ask_tool_budget(
        self,
        *,
        agent_key: str,
        agent_name: str,
        limit: int,
        used: int,
        tool_calls: int,
        max_extension: int,
        extension_step: int = TOOL_BUDGET_EXTENSION_STEP,
        timeout: float = TOOL_BUDGET_WAIT_SECONDS,
        on_open=None,
    ) -> "ToolBudgetRequest":
        """도구 호출 상한 도달을 사용자에게 알리고 결정을 대기합니다.

        `on_open`은 UI 알림을 표시하는 이벤트 콜백입니다 (엔진이 이벤트로 발행).
        대기 종료 시 요청 객체에는 결정 유형이 기록되어 반환됩니다 —
        확장(`extended`), 즉시 마무리(`wrap_up`), 또는 타임아웃(`timeout`).

        이미 세션 정지가 요청된 상태라면 추가 승인을 묻지 않고 즉시 마무리를 진행합니다.
        """
        request = DecisionRequest(
            request_id=uuid.uuid4().hex,
            kind="tool_budget",
            agent_key=agent_key,
            agent_name=agent_name,
            limit=limit,
            used=used,
            tool_calls=tool_calls,
            extension_step=extension_step,
            max_extension=max_extension,
        )
        return await self._ask(request, timeout, on_open)

    # -------------------------------------------------- 도구 승인

    async def ask_tool_approval(
        self,
        *,
        agent_key: str,
        agent_name: str,
        payload: Dict[str, Any],
        timeout: float = TOOL_BUDGET_WAIT_SECONDS,
        on_open=None,
        validator: Optional[Callable[[List[str], str], Optional[str]]] = None,
    ) -> "ToolApprovalRequest":
        """도구 실행 여부를 사용자에게 확인하고 승인을 대기합니다.

        `payload`는 UI에 렌더링할 상세 정보입니다 (도구명, 액션, 사유, 허용 범위 후보). 대기 종료 시
        요청 객체의 `decision`에 결정 결과가 기록됩니다 — `allow_once`, `allow_session`,
        `allow_always`, `deny`, `timeout`. 정지 요청 상태에서는 확인 없이 즉시 거부 처리합니다.

        `validator`는 사용자가 수정한 허용 범위와 결정 유효성을 검증하며, 부적합 시 사유를 반환합니다.
        응답 처리 시점(`resolve_tool_approval`)에 호출되어 UI에 즉시 피드백을 전달합니다.
        """
        request = ToolApprovalRequest(
            request_id=uuid.uuid4().hex,
            agent_key=agent_key,
            agent_name=agent_name,
            payload=payload,
        )
        request.validator = validator
        await self._ask(request, timeout, on_open)
        return request

    def resolve_tool_approval(
        self,
        request_id: str,
        decision: str,
        *,
        scope: Optional[List[str]] = None,
        reason: str = "",
        approver: str = "",
    ) -> bool:
        """도구 실행 승인 요청에 응답합니다. 이미 처리되었거나 존재하지 않는 요청이면 False를 반환합니다.

        "이 대화에서" 또는 "항상" 범위가 현재 호출을 포괄하지 못하면 ValueError를 발생시킵니다.
        유효 범위를 포괄하지 못하면 방금 승인한 도구가 다음 호출 시 재확인되거나 의도치 않게
        실행되는 오류를 방지하기 위한 검증입니다.
        """
        request = self._decisions.get(request_id)
        if not isinstance(request, ToolApprovalRequest) or request.resolved:
            return False
        if decision in REMEMBERED_DECISIONS:
            cleaned = [str(r).strip() for r in (scope or []) if str(r).strip()]
            if not cleaned:
                raise ValueError(
                    "기억할 범위가 비어 있습니다. 범위를 적거나 '이번만 허용'·'거부' 를 쓰세요."
                )
            if request.validator is not None:
                problem = request.validator(cleaned, decision)
                if problem:
                    raise ValueError(problem)
            scope = cleaned
        return request.answer(decision, scope=scope, reason=reason, approver=approver)

    # -------------------------------------------------- 에이전트의 질문 (v1.5)

    @property
    def pending_questions(self) -> List["UserQuestionRequest"]:
        """사용자 답변을 대기 중인 에이전트 질문 목록을 요청 순서대로 반환합니다."""
        return [r for r in self._decisions.values() if not r.resolved and isinstance(r, UserQuestionRequest)]

    async def ask_user_question(
        self,
        *,
        agent_key: str,
        agent_name: str,
        question: str,
        options: List[str],
        timeout: float,
        on_open=None,
    ) -> "UserQuestionRequest":
        """에이전트의 확인 질문을 사용자 화면에 게시하고 답변을 대기합니다 (`app/agents/ask_user.py`).

        대기가 완료되면 `answer` 필드에 응답이 설정됩니다. 빈 문자열인 경우 답변 부재(시간 초과, 건너뛰기,
        실행 중단 등)를 의미합니다. 이미 중단 요청된 턴에서는 질문을 게시하지 않습니다.
        """
        request = UserQuestionRequest(
            request_id=uuid.uuid4().hex, agent_key=agent_key, agent_name=agent_name,
            payload={"question": question, "options": list(options)},
        )
        await self._ask(request, timeout, on_open)
        return request

    def resolve_user_question(self, request_id: str, answer: str, *, approver: str = "") -> bool:
        """질문에 답합니다. 빈 답은 "건너뛰기" 입니다. 이미 답이 났거나 없는 질문이면 False."""
        request = self._decisions.get(request_id)
        if not isinstance(request, UserQuestionRequest) or request.resolved:
            return False
        return request.reply(answer, outcome="answered" if (answer or "").strip() else "skipped", approver=approver)

    # -------------------------------------------------- 계획 승인

    async def ask_plan_approval(
        self,
        *,
        agent_key: str,
        agent_name: str,
        payload: Dict[str, Any],
        timeout: float,
        on_open=None,
    ) -> "PlanApprovalRequest":
        """턴 실행 계획을 사용자에게 제시하고 승인을 대기합니다 (ADR-028).

        `payload`는 UI에 렌더링할 계획 세부 정보입니다 (대상 계획 ID, 전문가별 할당 태스크). 대기 완료 시
        요청의 `decision` 필드에 결정 유형이 기록됩니다 (`approve`, `revise`, `timeout`, `stopped`).
        이미 정지가 요청된 턴에서는 추가 확인을 생략합니다.
        """
        request = PlanApprovalRequest(
            request_id=uuid.uuid4().hex,
            agent_key=agent_key,
            agent_name=agent_name,
            payload=payload,
        )
        await self._ask(request, timeout, on_open)
        return request

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
        """계획 승인 요청에 응답합니다. 이미 처리되었거나 존재하지 않는 요청이면 False를 반환합니다.

        유효하지 않은 응답인 경우 ValueError를 발생시킵니다 — 피드백 없는 수정 요청은 오케스트레이터가
        수정 사항을 인지할 수 없으며, 피드백이 포함된 승인은 피드백이 누락될 위험이 있습니다.
        UI 버튼 비활성화 외에도 백엔드 검증을 통해 일관된 규칙을 보장합니다.
        """
        request = self._decisions.get(request_id)
        if not isinstance(request, PlanApprovalRequest) or request.resolved:
            return False
        said = bool((comment or "").strip()) or any(
            str(text or "").strip() for text in (task_comments or {}).values()
        )
        if decision == "revise" and not said:
            raise ValueError("수정 요청에는 의견이 필요합니다. 무엇을 고칠지 적어 주십시오.")
        if decision == "approve" and said:
            raise ValueError(
                "의견을 적은 채로는 승인할 수 없습니다. 의견을 반영하려면 '수정 요청' 을, "
                "그대로 진행하려면 의견을 지우고 승인해 주십시오."
            )
        return request.answer(
            decision, tasks=tasks, comment=comment, task_comments=task_comments, approver=approver,
        )


class DecisionRequest:
    """자원 한도에 도달한 에이전트의 사용자 결정 요청 모델.

    응답은 두 가지 유형입니다 — **한도 확장 승인** 또는 **현재까지의 결과로
    마무리 지시**. 어느 쪽이든 진행 중인 발언은 안전하게 유지됩니다. 과거에는
    한도 도달 즉시 예외가 발생하여 진행 중이던 스트리밍 발언과 도구 관측 결과가
    모두 유실되는 문제가 있었습니다.
    """

    def __init__(
        self,
        *,
        request_id: str,
        kind: str = "tool_budget",
        agent_key: str,
        agent_name: str,
        limit: int,
        used: int,
        tool_calls: int,
        extension_step: int = TOOL_BUDGET_EXTENSION_STEP,
        max_extension: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.id = request_id
        # 무엇에 대한 물음인가 — "tool_budget" | "context_window".
        # 화면은 이 값으로 색과 문구를 고릅니다.
        self.kind = kind
        self.payload: Dict[str, Any] = dict(payload or {})
        self.agent_key = agent_key
        self.agent_name = agent_name
        self.limit = limit
        self.used = used
        self.tool_calls = tool_calls
        # 화면이 제안할 확장 폭. 남은 여유(`max_extension`)를 넘지 않습니다.
        self.extension_step = max(0, min(extension_step, max_extension))
        self.max_extension = max(0, max_extension)
        self.granted: int = 0
        self.outcome: str = "pending"  # pending | extended | wrap_up | timeout
        # 화면이 남은 시간을 세려면 "언제 열렸는지" 와 "얼마나 기다리는지" 가
        # 둘 다 필요합니다. 새로고침한 화면도 같은 값으로 같은 초를 셉니다.
        self.opened_at: float = time.time()
        self.wait_seconds: float = TOOL_BUDGET_WAIT_SECONDS
        self._decided = asyncio.Event()

    @property
    def resolved(self) -> bool:
        return self._decided.is_set()

    @property
    def answerable(self) -> bool:
        """사용자에게 제공할 가용 확장 옵션이 존재하는지 확인합니다. 추가 확장 여지가 없으면 확인을 생략하고 종료합니다."""
        return self.max_extension > 0

    def resolve(self, extra: int, outcome: str) -> bool:
        """답을 채워 넣습니다. 이미 답이 있으면 무시하고 False."""
        if self._decided.is_set():
            return False
        self.granted = max(0, min(int(extra), self.max_extension))
        self.outcome = outcome if self.granted else ("wrap_up" if outcome != "timeout" else "timeout")
        self._decided.set()
        return True

    async def wait(self, timeout: float) -> int:
        """답을 기다립니다. 시간이 지나면 확장 없음으로 보고 0 을 돌려줍니다."""
        try:
            await asyncio.wait_for(self._decided.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.resolve(0, "timeout")
        return self.granted

    def describe(self) -> dict:
        """화면과 스냅샷에 실리는 형태."""
        return {
            "id": self.id,
            "kind": self.kind,
            "agent_key": self.agent_key,
            "agent_name": self.agent_name,
            "limit": self.limit,
            "used": self.used,
            "tool_calls": self.tool_calls,
            "extension_step": self.extension_step,
            "max_extension": self.max_extension,
            "opened_at": self.opened_at,
            "wait_seconds": self.wait_seconds,
            **self.payload,
        }


APPROVAL_DECISIONS = (
    "allow_once", "allow_session", "allow_always", "deny", "deny_session", "deny_always",
)
# 범위를 받아 기억하는 답 (이 대화 = 세션 DB, 항상 = conf.json).
REMEMBERED_DECISIONS = ("allow_session", "allow_always", "deny_session", "deny_always")


class ToolApprovalRequest(DecisionRequest):
    """도구 보안 검증 판정이 '확인(ask)'인 단일 호출에 대한 실행 승인 요청 모델.

    한도 결정 요청(`DecisionRequest`)과 동일한 큐와 대기 메커니즘을 사용하되, 응답 형태가 다릅니다 —
    증액 단위가 아닌 **실행 승인 여부 및 허용 범위**입니다. 응답이 없으면 보안상 거부(Denied) 처리됩니다.
    """

    def __init__(self, *, request_id: str, agent_key: str, agent_name: str,
                 payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            request_id=request_id, kind="tool_approval", agent_key=agent_key,
            agent_name=agent_name, limit=0, used=0, tool_calls=0, payload=payload,
        )
        # allow_once | allow_session | allow_always | deny | deny_session | deny_always | timeout
        self.decision: str = "pending"
        self.scope: List[str] = []       # "이 대화에서"·"항상" 기억할 규칙
        self.reason: str = ""            # 거부 사유 (모델에게 전달)
        self.approver: str = ""          # local | remote
        # 사용자가 수정한 허용 범위를 검증하는 함수 (`ask_tool_approval`). 스냅샷에는 미포함.
        self.validator: Optional[Callable[[List[str], str], Optional[str]]] = None

    @property
    def answerable(self) -> bool:
        return True

    @property
    def allowed(self) -> bool:
        return self.decision.startswith("allow_")

    def answer(self, decision: str, *, scope: Optional[List[str]] = None,
               reason: str = "", approver: str = "") -> bool:
        """답을 채웁니다. 이미 답이 있거나 모르는 답이면 False."""
        if self._decided.is_set() or decision not in APPROVAL_DECISIONS + ("timeout",):
            return False
        self.decision = decision
        self.scope = [str(r) for r in (scope or []) if str(r).strip()]
        self.reason = (reason or "").strip()
        self.approver = approver
        self.outcome = decision
        self._decided.set()
        return True

    def resolve(self, extra: int, outcome: str) -> bool:
        """타임아웃 또는 정지로 인한 기본 거부(Denied) 응답을 생성합니다."""
        if outcome == "timeout":
            return self.answer("timeout", reason="정해진 시간 안에 답이 없었습니다.")
        return self.answer("deny", reason="유저가 토론 정지를 요청했습니다.")

    def describe(self) -> dict:
        return {**super().describe(), "decision": self.decision}


class PlanApprovalRequest(DecisionRequest):
    """오케스트레이터의 턴 실행 계획에 대한 사용자 승인 요청 모델 (ADR-028).

    동일한 큐와 대기 메커니즘을 사용하되, 응답 형태가 다릅니다 — **승인**(사용자가 수정한
    태스크 구성 포함) 또는 **수정 요청**(피드백 코멘트 포함)입니다. 무응답 시 자동 승인되지 않으며
    미확인 계획으로 토론이 임의 진행되는 것을 차단합니다.
    """

    def __init__(self, *, request_id: str, agent_key: str, agent_name: str,
                 payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            request_id=request_id, kind="plan_approval", agent_key=agent_key,
            agent_name=agent_name, limit=0, used=0, tool_calls=0, payload=payload,
        )
        # approve | revise | timeout | stopped
        self.decision: str = "pending"
        # 사용자가 카드 UI에서 수정한 전문가별 태스크 및 완료 기준 (승인/수정 요청 공통 수신).
        self.tasks: List[Dict[str, Any]] = []
        self.comment: str = ""                    # 계획 전체에 대한 의견
        self.task_comments: Dict[str, str] = {}   # 전문가 키 → 그 태스크에 대한 의견
        self.approver: str = ""                   # local | remote

    @property
    def answerable(self) -> bool:
        return True

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    def answer(self, decision: str, *, tasks: Optional[List[Dict[str, Any]]] = None,
               comment: str = "", task_comments: Optional[Dict[str, str]] = None,
               approver: str = "") -> bool:
        """답을 채웁니다. 이미 답이 있거나 모르는 답이면 False."""
        if self._decided.is_set() or decision not in ("approve", "revise", "timeout", "stopped"):
            return False
        self.decision = decision
        self.tasks = [dict(t) for t in (tasks or []) if isinstance(t, dict)]
        self.comment = (comment or "").strip()
        self.task_comments = {
            str(k): str(v).strip() for k, v in (task_comments or {}).items() if str(v or "").strip()
        }
        self.approver = approver
        self.outcome = decision
        self._decided.set()
        return True

    def resolve(self, extra: int, outcome: str) -> bool:
        """타임아웃 또는 정지로 인한 기본 미승인 응답을 생성합니다."""
        return self.answer("timeout" if outcome == "timeout" else "stopped")

    def describe(self) -> dict:
        return {**super().describe(), "decision": self.decision}


class UserQuestionRequest(DecisionRequest):
    """에이전트의 단일 사용자 질문 요청 (`ask__ask_user`).

    동일한 의사결정 요청 큐를 사용하며 텍스트 응답을 수신합니다. 답변이 없어도(시간 초과, 건너뛰기, 중단 등)
    발언이 끊기지 않고 가정을 명시한 후 계속 진행됩니다.
    """

    def __init__(self, *, request_id: str, agent_key: str, agent_name: str,
                 payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            request_id=request_id, kind="user_question", agent_key=agent_key,
            agent_name=agent_name, limit=0, used=0, tool_calls=0, payload=payload,
        )
        self.answer: str = ""
        self.approver: str = ""          # local | remote

    @property
    def answerable(self) -> bool:
        return True

    def reply(self, answer: str, *, outcome: str, approver: str = "") -> bool:
        if self._decided.is_set():
            return False
        self.answer = (answer or "").strip()
        self.approver = approver
        self.outcome = outcome  # answered | skipped | timeout | stopped
        self._decided.set()
        return True

    def resolve(self, extra: int, outcome: str) -> bool:
        """결정 요청의 기본 응답 생성 (정지/타임아웃). 무응답 처리됩니다."""
        return self.reply("", outcome="timeout" if outcome == "timeout" else "stopped")

    def describe(self) -> dict:
        return {**super().describe(), "outcome": self.outcome}


# 예전 이름. 도구 예산 전용이던 시절의 import 를 깨뜨리지 않습니다.
ToolBudgetRequest = DecisionRequest
