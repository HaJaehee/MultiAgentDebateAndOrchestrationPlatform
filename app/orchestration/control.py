"""진행 중인 토론에 사람이 끼어드는 통로.

토론 태스크는 한번 뜨면 끝까지 혼자 도는 것이 원래 설계였습니다. 그래서
"지금까지만 정리하고 멈춰" 나 "그 방향 말고 이쪽으로" 를 전할 방법이 없었고,
할 수 있는 것은 태스크를 통째로 죽이는 것(`DebateRunner.cancel()`)뿐이었습니다.
그러면 진행 중이던 발언은 잘리고 최종 합성 단계는 아예 돌지 않아, 지금까지의
토론으로 산출물을 뽑을 기회가 사라집니다.

`TurnControl` 은 그 사이에 놓이는 아주 작은 우편함입니다. 화면 쪽 코루틴이
여기에 요청을 넣어 두면, 엔진이 발언과 발언 사이의 안전한 지점에서 꺼내 봅니다.

* **정지 요청** — 태스크를 죽이지 않습니다. 진행 중인 발언은 끝까지 받고,
  남은 라운드를 건너뛰어 곧장 최종 합성으로 넘어갑니다.
* **개입 메모** — 다음 발언자의 맥락에 유저 발언으로 끼어듭니다.
* **도구 예산 확장 요청** — 여기서는 방향이 반대입니다. 도구 호출 상한을 다 쓴
  에이전트가 사람에게 쪽지를 내밀고, 답(상한 확장 / 즉시 마무리)이 올 때까지
  그 발언만 기다립니다. 답이 없으면 시간이 지나 스스로 마무리로 갑니다.

경계를 넘어 공유되는 상태는 이 객체 하나뿐이고, 같은 이벤트 루프 안에서만
읽고 쓰므로 락이 필요 없습니다. 엔진은 이 모듈만 알면 되고 UI 도 러너도
모릅니다.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 도구 예산이 바닥났을 때 사람의 답을 기다리는 시간(초).
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


class TurnControl:
    """한 턴에 대한 정지 요청과 개입 메모를 담아 두는 우편함."""

    def __init__(self) -> None:
        self._stop_requested = False
        self._notes: List[str] = []
        # 사람의 답을 기다리는 쪽지들 (id -> 쪽지). 도구 상한과 컨텍스트 창이
        # 같은 우편함을 씁니다 — 기다리는 방식도, 답을 받는 방식도 같습니다.
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

        사람의 답을 기다리는 쪽지가 있었다면 (도구 상한이든 컨텍스트든) 그 자리에서
        "늘리지 않음" 으로 답합니다. 멈추라고 한 사람에게 "더 쓸까요?" 를 계속 물어
        놓는 것은 답이 이미 나온 질문을 붙잡고 있는 것입니다.
        """
        self._stop_requested = True
        for request in self.pending_decisions:
            request.resolve(0, "wrap_up")

    # -------------------------------------------------- 개입

    @property
    def pending_notes(self) -> List[str]:
        """아직 토론에 반영되지 않고 대기 중인 개입 메모."""
        return list(self._notes)

    def add_note(self, text: str) -> bool:
        """개입 메모를 대기열에 넣습니다. 빈 문자열은 무시하고 False 를 돌려줍니다."""
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        self._notes.append(cleaned)
        return True

    def drain_notes(self) -> List[str]:
        """대기 중인 메모를 전부 꺼내고 대기열을 비웁니다."""
        notes, self._notes = self._notes, []
        return notes

    # -------------------------------------------------- 결정 요청 (공통)

    @property
    def pending_decision(self) -> Optional["DecisionRequest"]:
        """답을 기다리는 쪽지 중 가장 최근 것. 화면은 이걸 보여줍니다."""
        pending = self.pending_decisions
        return pending[-1] if pending else None

    @property
    def pending_decisions(self) -> List["DecisionRequest"]:
        """답을 기다리는 쪽지 전부 (들어온 순서)."""
        return [r for r in self._decisions.values() if not r.resolved]

    # 예전 이름. 러너와 테스트가 쓰고 있어 그대로 둡니다.
    @property
    def pending_budget_request(self) -> Optional["DecisionRequest"]:
        return self.pending_decision

    @property
    def pending_budget_requests(self) -> List["DecisionRequest"]:
        return self.pending_decisions

    async def _ask(self, request: "DecisionRequest", timeout: float, on_open) -> "DecisionRequest":
        """쪽지를 걸어 두고 답을 기다립니다. 종류가 무엇이든 흐름은 같습니다."""
        if self._stop_requested or request.max_extension <= 0:
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
        """대기 중인 쪽지에 답합니다. `extra` 가 0 이면 "지금 마무리하라" 입니다.

        `request_id` 를 주면 그 쪽지에만 답합니다. 화면이 오래된 쪽지를 들고
        있을 수 있고 (다음 발언자가 새 쪽지를 낸 뒤에 누른 버튼), 그 답이 엉뚱한
        에이전트의 상한을 늘려서는 안 됩니다. 주지 않으면 가장 최근 쪽지에
        답합니다.
        """
        if request_id is not None:
            request = self._decisions.get(request_id)
        else:
            request = self.pending_decision
        if request is None:
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

    def resolve_context_window(self, extra: int, request_id: Optional[str] = None) -> bool:
        """컨텍스트 확장 요청에 답합니다 (`resolve_decision` 과 같습니다)."""
        return self.resolve_decision(extra, request_id)

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
        """도구 상한에 닿았음을 사람에게 알리고 답을 기다립니다.

        `on_open` 은 쪽지를 화면에 띄우는 콜백입니다 (엔진이 이벤트로 보냅니다).
        기다림이 끝나면 요청은 어떤 형태로든 답이 채워진 채 돌아옵니다 —
        확장(`extended`), 즉시 마무리(`wrap_up`), 또는 시간 초과(`timeout`).

        이미 정지를 요청한 사람에게 "더 부를까요?" 를 묻지 않습니다. 그 답은
        이미 나와 있습니다.
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

    def resolve_tool_budget(self, extra: int, request_id: Optional[str] = None) -> bool:
        """도구 상한 요청에 답합니다 (`resolve_decision` 과 같습니다)."""
        return self.resolve_decision(extra, request_id)


class DecisionRequest:
    """한도에 닿은 에이전트가 사람에게 내미는 쪽지.

    답은 둘 중 하나입니다 — **상한을 늘려 주거나**, **지금까지 얻은 것으로
    끝내라고 하거나**. 어느 쪽이든 진행 중인 발언은 살아남습니다. 예전에는
    상한에 닿는 순간 `LLMUnavailableError` 가 올라와, 그때까지 흘러나온 글과
    도구 관측이 통째로 실패 안내로 덮였습니다.
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


# 예전 이름. 도구 예산 전용이던 시절의 import 를 깨뜨리지 않습니다.
ToolBudgetRequest = DecisionRequest
