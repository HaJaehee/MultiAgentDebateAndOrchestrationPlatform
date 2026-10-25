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

스냅샷과 구독 큐는 `app/orchestration/turn_run.py` 의 `TurnRun` 이 들고, 여기의 `DebateRunner` 는
세션마다 그 실행을 띄우고 끝내는 일을 맡습니다.

UI 쪽 코드는 이 모듈을 import 하지만, 이 모듈은 UI 를 전혀 모릅니다. 엔진 태스크가
NiceGUI 엘리먼트를 건드릴 일이 없으니 클라이언트가 사라져도 터질 곳이 없습니다.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.events import EventType
from app.orchestration.engine import OrchestratorEngine, get_orchestrator_engine
from app.orchestration.event_bus import BaseEventBus, get_event_bus
from app.orchestration.plan_gate import PlanApprovalExpired
from app.orchestration.turn_run import TurnRun
from app.orchestration.worker import TaskWorkerPool, get_task_worker_pool

logger = logging.getLogger(__name__)

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


class DebateRunner:
    """세션별 토론 태스크의 소유자. 프로세스 전체에서 하나만 씁니다."""

    def __init__(
        self,
        engine: Optional[OrchestratorEngine] = None,
        event_bus: Optional[BaseEventBus] = None,
        worker_pool: Optional[TaskWorkerPool] = None,
    ):
        self._engine = engine
        self._runs: Dict[str, TurnRun] = {}
        self.event_bus = event_bus or get_event_bus()
        self.worker_pool = worker_pool or get_task_worker_pool()

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
        """지금 토론이 돌고 있는 대화의 id."""
        return [sid for sid, run in self._runs.items() if run.status == "running"]

    def running_elsewhere(self, session_id: str) -> List[TurnRun]:
        """이 세션이 아닌 다른 세션에서 돌고 있는 토론."""
        return [r for sid, r in self._runs.items()
                if sid != session_id and r.status == "running"]

    def start(self, session_id: str, user_prompt: str,
              workspace: Optional[str] = None,
              skill_designations: Optional[Dict[str, List[str]]] = None,
              *, attended: bool = True) -> TurnRun:
        """토론을 백그라운드에서 시작합니다. 이미 돌고 있으면 그 실행을 돌려줍니다.

        `attended=False`는 대화형 사용자가 없는 무인 모드 턴입니다 (API/MCP 헤드리스 실행, `app/headless.py`).
        대화형 승인 다이얼로그를 띄우지 않습니다 (`control.asks_human`).
        """
        existing = self._runs.get(session_id)
        if existing is not None and existing.status == "running":
            return existing

        run = TurnRun(session_id, user_prompt, workspace, event_bus=self.event_bus)
        run.control.attended = attended
        self._runs[session_id] = run

        async def body(on_event) -> None:
            await self.engine.run_turn(
                session_id=session_id, user_prompt=user_prompt, on_event=on_event,
                control=run.control, skill_designations=skill_designations,
            )

        return self._launch(run, body)

    def resume(self, session_id: str, turn_id: str, mode: str, user_prompt: str = "",
               workspace: Optional[str] = None) -> TurnRun:
        """끊긴 턴을 백그라운드에서 마무리하거나(`finish`) 이어 갑니다(`continue`)."""
        existing = self._runs.get(session_id)
        if existing is not None and existing.status == "running":
            return existing

        run = TurnRun(
            session_id, user_prompt, workspace, turn_id=turn_id, resume_mode=mode, event_bus=self.event_bus
        )
        run.status_text = (
            "끊긴 턴을 지금까지의 발언으로 마무리하는 중..." if mode == "finish"
            else "끊긴 턴을 이어 가는 중..."
        )
        self._runs[session_id] = run

        async def body(on_event) -> None:
            await self.engine.resume_turn(
                session_id=session_id, turn_id=turn_id, mode=mode, on_event=on_event,
                control=run.control,
            )

        return self._launch(run, body)

    def _launch(
        self,
        run: TurnRun,
        body: Callable[[Callable[[Dict[str, Any]], Awaitable[None]]], Awaitable[None]],
    ) -> TurnRun:
        """실행을 태스크로 띄웁니다. 끝나는 방식(완료·취소·실패)을 스냅샷과 화면에 알립니다."""
        session_id = run.session_id

        started = False

        async def on_event(event: Dict[str, Any]) -> None:
            run.apply(event)
            run.publish(event)

        def announce_end() -> None:
            run.busy = False
            run.streaming_ids.clear()
            run.plan_approval = None
            if run.parked:
                run.status_text = str(run.error or "")
                run.round_info = "Waiting"
            elif run.status == "failed":
                run.status_text = f"오류로 중단됨: {run.error}"
                run.round_info = "Error"
            elif run.status == "cancelled":
                run.status_text = "토론이 취소되었습니다."
                run.round_info = "Cancelled"
            try:
                run.publish({
                    "type": EventType.RUN_FINISHED,
                    "status": run.status,
                    "error": run.error,
                    "parked": run.parked,
                })
            except BaseException:  # noqa: BLE001
                logger.warning(
                    "Could not announce the end of the debate for session %s",
                    session_id, exc_info=True,
                )

        async def driver() -> None:
            nonlocal started
            started = True
            try:
                await body(on_event)
                run.status = "completed"
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.error = "토론이 취소되었습니다."
                raise
            except PlanApprovalExpired as exc:
                logger.info(f"Debate turn for session {session_id} is parked: {exc}")
                run.status = "failed"
                run.parked = True
                run.error = str(exc)
            except BaseException as exc:  # noqa: BLE001
                logger.error(
                    f"Debate turn failed for session {session_id}: "
                    f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                run.status = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
            finally:
                announce_end()

        def settle_unstarted(task: "asyncio.Task[Any]") -> None:
            # 드라이버가 실행을 시작하지 못하고 종료된 경우 상태를 정리합니다. 워커 풀 슬롯 대기 중이거나
            # 태스크 생성 직후 취소되면 코루틴 내부의 `except`·`finally` 블록이 실행되지 않습니다.
            # 이를 방치하면 실행 상태가 "running" 으로 남아 UI 에서 종료를 감지하지 못하고, 이후 `start` 호출 시
            # 종료된 인스턴스를 반환하여 서버를 재기동하기 전까지 해당 세션을 재사용할 수 없게 됩니다.
            if started:
                return
            if task.cancelled():
                run.status = "cancelled"
                run.error = "토론이 취소되었습니다."
            else:
                exc = task.exception()
                run.status = "failed"
                run.error = f"{type(exc).__name__}: {exc}" if exc else "토론을 시작하지 못했습니다."
            announce_end()

        job = self.worker_pool.submit(f"debate:{session_id}", driver)
        run.task = job.task
        run.task.add_done_callback(settle_unstarted)
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

            {"prompt": 사람이 보냈던 글, "message_ids": [...], "artifact_ids": [...],
             "turn_id": 턴 기록 id 또는 None}

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
            # 턴을 아는 쪽이 턴째로 지웁니다 — 끊겼다 이어 간 턴이면 앞선 실행이 남긴 발언과,
            # 끝나지 못한 발언의 도구 기록까지 이 턴의 것입니다.
            "turn_id": run.turn_id,
        }
        await self.cancel(session_id)
        logger.info(
            f"Debate for session {session_id} was aborted by the user; "
            f"{len(produced['message_ids'])} message(s) will be discarded."
        )
        return produced

    def interject(self, session_id: str, text: str,
                  skill_designations: Optional[Dict[str, List[str]]] = None) -> bool:
        """진행 중인 토론에 사용자 메시지를 끼워 넣습니다."""
        run = self._runs.get(session_id)
        return run.interject(text, skill_designations) if run is not None else False

    def resolve_decision(self, session_id: str, extra: int,
                         request_id: Optional[str] = None) -> bool:
        """한도에 닿은 에이전트에게 확장 여부를 알려 줍니다 (도구 상한·컨텍스트 공통)."""
        run = self._runs.get(session_id)
        return run.resolve_decision(extra, request_id) if run is not None else False

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

    def resolve_user_question(self, session_id: str, request_id: str, answer: str, *, approver: str = "") -> bool:
        """에이전트의 확인 질문에 대한 사용자 답변을 전달합니다 (빈 문자열 전달 시 건너뛰기 처리)."""
        run = self._runs.get(session_id)
        if run is None or run.status != "running":
            return False
        return run.control.resolve_user_question(request_id, answer, approver=approver)

    def resolve_plan_approval(self, session_id: str, request_id: str, decision: str, *,
                              tasks: Optional[List[Dict[str, Any]]] = None, comment: str = "",
                              task_comments: Optional[Dict[str, str]] = None,
                              approver: str = "") -> bool:
        """계획 승인 카드에 답합니다 (`TurnRun.resolve_plan_approval`)."""
        run = self._runs.get(session_id)
        if run is None:
            return False
        return run.resolve_plan_approval(
            request_id, decision, tasks=tasks, comment=comment,
            task_comments=task_comments, approver=approver,
        )

    async def cancel(self, session_id: str) -> bool:
        self.worker_pool.cancel(f"debate:{session_id}")
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
        """서버 종료 시 남은 토론 태스크를 정리합니다.

        작업 완료를 무한정 대기하지 않고 즉시 취소합니다 (`worker_pool.drain` 을 먼저 호출하지 않습니다).
        토론 턴은 수 분간 진행되거나 사용자의 승인을 대기할 수 있어 자연 종료를 기다리면 서버 종료가 과도하게 지연되고,
        그사이 불필요한 LLM 호출 및 도구 실행이 지속되기 때문입니다. 취소 요청에 응답하지 않는 태스크는
        `cancel` 의 `CANCEL_TIMEOUT` 경과 후 격리 처리하고 반환합니다.
        """
        for session_id in list(self._runs):
            await self.cancel(session_id)

    def forget(self, session_id: str) -> None:
        """세션이 삭제됐을 때 스냅샷까지 버립니다."""
        self.worker_pool.cancel(f"debate:{session_id}")
        run = self._runs.pop(session_id, None)
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()


_runner: Optional[DebateRunner] = None


def get_debate_runner() -> DebateRunner:
    global _runner
    if _runner is None:
        _runner = DebateRunner()
    return _runner
