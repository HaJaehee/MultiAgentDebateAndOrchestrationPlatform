"""중단된 턴의 세션 재개 전 샌드박스 커널 메모리의 변수 상태를 복원합니다 (ADR-046).

서버 프로세스가 비정상 종료되면 커널 프로세스도 함께 소멸하여 인메모리 변수 및 모듈 임포트 상태가 유실됩니다.
반면 발언 초안의 대화 기록에는 해당 변수를 생성한 실행 이력이 보존되어 있어, 모델은 변수가 메모리에 존재한다고
가정하고 후속 코드를 작성하게 됩니다. 커널 메모리는 디스크에 영속화되지 않으나 실행된 코드 셀은 도구 호출
이력에 기록되어 있으므로(`turns.kernel_cells`), 사용자가 승인할 경우 동일한 코드를 순서대로 재실행합니다.

코드 재실행 시 준수하는 핵심 설계 원칙:

1. **사용자의 명시적 승인에 의해서만 실행됩니다.** 기본 '이어서 진행'은 코드를 재실행하지 않습니다.
   인간의 명시적 승인 없이 백그라운드에서 임의로 도구가 실행되어서는 안 되기 때문입니다 (ADR-024).
2. **현재의 도구 보안 정책을 엄격히 적용합니다.** 셀 단위로 최신 보안 판정을 거치며, 중단 시점의 이전 승인 기록을
   근거로 보안 검사를 우회하지 않습니다. 보안 정책에 의해 차단될 경우 즉시 실행을 중단합니다.
3. **작업 공간 파일의 무결성을 보장합니다.** 과거 셀에 파일 쓰기 코드가 포함되어 있을 경우 그 이후 수정된 최신 파일 내용이
   과거 내용으로 덮어써질 위험이 있습니다. 변수 복원을 위해 작업 결과물이 유실되는 부작용을 방지하고자, 재실행 직전의
   스냅샷으로 작업 공간 파일을 자동 롤백합니다. 스냅샷 생성이 불가능하거나 동일 작업 공간에서 타 턴이 실행 중인 경우에는
   안전을 위해 재실행을 거부합니다. 외부 네트워크 호출 등 작업 공간 외부 부수 효과는 롤백할 수 없습니다.

복원된 변수 상태는 근사치(Approximation)입니다. 난수, 타임스탬프, 외부 환경에 의존하는 값은 이전과 달라질 수 있으며,
실패한 셀이 생성하려 했던 변수나 이전 턴에서 선언된 전역 변수는 복구되지 않습니다. 따라서 선행 셀 실패 시 후속 실행을 즉시 중단하고
복구 범위를 모델과 사용자에게 투명하게 고지합니다.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app import workspace_checkpoint
from app.agents.base import Agent
from app.events import EventCallback
from app.mcp.pool import get_runtime_pool
from app.orchestration import turns
from app.orchestration.context import EngineContext
from app.orchestration.control import TurnControl
from app.orchestration.recording import TurnRecorder
from app.orchestration.state import DebateState
from app.orchestration.turn_setup import TurnSetup

logger = logging.getLogger(__name__)

# 오류 사유 표시 최대 문자 수.
REASON_CHARS = 200

# 초안에서 이어 가는 발언에게 커널 상태를 알리는 말 (`ToolLoopMixin._close_cut_speech`).
KERNEL_LOST_NOTE = (
    "[커널] 서버가 다시 시작되어 샌드박스 커널의 변수와 임포트가 사라졌습니다. 앞서 만든 변수가 필요하면 다시 "
    "만드세요. 작업 공간의 파일은 그대로입니다."
)
KERNEL_REPLAYED_NOTE = (
    "[커널] 커널을 비우고 이번 턴에서 끝까지 실행된 셀 {ran}개를 순서대로 다시 실행해 변수를 되살렸습니다. 코드를 "
    "다시 돌린 것이라 난수·시각·외부 상태에 달린 값은 그때와 다를 수 있고, 이전 턴에서 만든 변수는 없습니다."
)
KERNEL_PARTIAL_NOTE = (
    "[커널] 커널을 비우고 셀 {planned}개 중 {ran}개까지만 다시 실행했습니다 ({reason}). 나머지 셀이 만든 변수는 "
    "없으니 필요하면 다시 만드세요."
)
KERNEL_NOT_REPLAYED_NOTE = (
    "[커널] 셀을 다시 실행하지 못했습니다 ({reason}). 커널의 변수가 비어 있을 수 있으니, 앞서 만든 변수를 쓰기 전에 "
    "있는지 확인하고 없으면 다시 만드세요."
)


@dataclass
class _Kernel:
    """단일 커널 네임스페이스에 대한 재실행 결과 데이터 클래스."""

    namespace: str
    agents: List[str]
    planned: int
    ran: int = 0
    # 실행 중단 사유.
    stopped: str = ""
    # 재실행된 도구 호출 목록 (에이전트 인스턴스, 도구 기록). 보안 차단 호출도 감사 로그를 위해 보존합니다.
    calls: List[Tuple[Agent, Dict[str, Any]]] = field(default_factory=list)


def agents_of(cells: Dict[str, List[Any]]) -> List[str]:
    """코드 셀을 실행한 에이전트 목록 반환 (최초 등장 순서 보존)."""
    return list(dict.fromkeys(row.agent_key for rows in cells.values() for row in rows))


def _failure_reason(output: str) -> str:
    """실패한 코드 셀의 표준 오류(stderr) 요약 문자열 반환."""
    found = re.search(r"--- stderr ---\n(.*?)(?:\n--- stdout ---|\Z)", output, re.S)
    lines = [line.strip() for line in (found.group(1) if found else output).splitlines() if line.strip()]
    return (lines[-1] if lines else "결과를 읽지 못했습니다")[:REASON_CHARS]


async def _call(
    mcp: Any, gate: Any, session_id: str, agent: Agent, tool_name: str, arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """도구 보안 정책 판정을 거친 후 도구를 실행하고 도구 기록 규격으로 반환합니다."""
    verdict = await gate.check(agent, tool_name, arguments, mcp) if gate is not None else None
    if verdict is not None and not verdict.allowed:
        output, status = verdict.output, verdict.status or "denied"
    else:
        output, status = await mcp.execute_tool(tool_name, arguments, scope=session_id, actor=agent.key)
    return {
        "tool_name": tool_name, "arguments": arguments, "output": str(output), "status": status,
        "security": verdict.audit if verdict is not None else {},
    }


class KernelReplayer:
    """커널 코드 셀을 선별 재실행하고 감사 이력 및 피드 노트를 기록하는 실행기."""

    def __init__(self, ctx: EngineContext, recorder: TurnRecorder) -> None:
        self.ctx = ctx
        self.recorder = recorder

    async def replay(
        self,
        db,
        state: DebateState,
        setup: TurnSetup,
        workspace: Path,
        *,
        control: Optional[TurnControl],
        on_event: Optional[EventCallback],
    ) -> Dict[str, str]:
        """해당 턴의 코드 셀들을 재실행합니다. 반환값은 {에이전트 키: 모델 안내 문구} 맵입니다."""
        turn_messages = state.messages[state.turn_message_start:]
        cells = await turns.turn_kernel_cells(db, state.session_id, state.turn_id, turn_messages)
        if not cells:
            return {}
        # 코드 셀 재실행은 수 분 이상 소요될 수 있으므로, 데이터베이스 트랜잭션을 커밋 및 종료하여 커넥션 점유를 방지합니다 (ADR-041).
        await db.commit()

        kernels = [
            _Kernel(namespace, list(dict.fromkeys(row.agent_key for row in rows)), len(rows))
            for namespace, rows in cells.items()
        ]
        refusal = self._refusal(state, workspace)
        before = None
        if not refusal:
            before = await asyncio.to_thread(workspace_checkpoint.snapshot, workspace, None)
            if before is None:
                refusal = "작업 공간 스냅샷을 만들지 못해 파일을 지킬 수 없습니다"

        restored = ""
        if refusal:
            for kernel in kernels:
                kernel.stopped = refusal
        else:
            try:
                for kernel in kernels:
                    await self._run_kernel(kernel, cells[kernel.namespace], state, setup, control)
            finally:
                restored = await self._restore_files(workspace, before)

        await self._record(db, state, setup, kernels, restored, on_event)
        return self._notes(kernels)

    def _refusal(self, state: DebateState, workspace: Path) -> str:
        """코드 재실행 거부 사유 판별. 거부 사유가 없으면 빈 문자열 반환."""
        if self._runtime(state) is None:
            return "도구 서버가 떠 있지 않습니다"
        others = get_runtime_pool().shared_with(workspace, state.session_id)
        if others:
            # 재실행 완료 후 작업 공간을 스냅샷 시점으로 롤백할 때, 타 세션이 작업 중인 파일까지 함께 손상되는 문제를 방지합니다.
            return "같은 작업 공간에서 다른 대화가 진행 중입니다"
        return ""

    def _runtime(self, state: DebateState) -> Any:
        """해당 턴의 MCP 런타임 반환. 풀에 미등록된 경우 기본 런타임으로 폴백합니다."""
        return self.ctx.mcp_for(state) or getattr(self.ctx.llm_caller, "mcp_manager", None)

    def _agent(self, setup: TurnSetup, key: str) -> Optional[Agent]:
        return next((a for a in setup.active_agents if a.key == key), None) or self.ctx.agent_pool.get(key)

    async def _run_kernel(
        self,
        kernel: _Kernel,
        rows: List[Any],
        state: DebateState,
        setup: TurnSetup,
        control: Optional[TurnControl],
    ) -> None:
        """커널 인스턴스를 초기화한 후 코드 셀들을 순서대로 재실행합니다. 최초 실패 또는 보안 차단 발생 시 즉시 중단합니다."""
        mcp = self._runtime(state)
        emptied = False
        for number, row in enumerate(rows, start=1):
            if control is not None and control.stop_requested:
                kernel.stopped = "유저가 정지를 요청했습니다"
                return
            agent = self._agent(setup, row.agent_key)
            meta = mcp.tool_meta(row.tool_name)
            # 중단된 공백 기간 동안 에이전트 도구 권한이나 서버 설정이 제거된 경우 해당 호출을 건너뜁니다.
            if agent is None or meta is None or meta.server not in agent.allowed_mcp_servers:
                kernel.stopped = f"{number}번째 셀의 도구를 지금은 쓸 수 없습니다 ({row.tool_name})"
                return
            if not emptied:
                # 런타임 예외로 중단된 턴의 경우 커널 프로세스가 살아있을 수 있으므로, 초기화 후 실행하여 상태 왜곡을 방지합니다.
                reset = await _call(
                    mcp, self.ctx.gate_for(state), state.session_id, agent,
                    f"{meta.server}__{turns.KERNEL_RESET_TOOL}", {"namespace": kernel.namespace},
                )
                if reset["status"] != "success":
                    kernel.stopped = f"커널을 비우지 못했습니다: {_failure_reason(reset['output'])}"
                    return
                emptied = True
            call = await _call(
                mcp, self.ctx.gate_for(state), state.session_id, agent, row.tool_name, dict(row.arguments or {}),
            )
            kernel.calls.append((agent, call))
            if not turns.cell_ran(call["status"], call["output"]):
                what = "보안 판정에 막혔습니다" if call["status"] == "denied" else "실패했습니다"
                kernel.stopped = f"{number}번째 셀이 {what}: {_failure_reason(call['output'])}"
                return
            kernel.ran = number

    @staticmethod
    async def _restore_files(workspace: Path, before: Optional[str]) -> str:
        """코드 재실행으로 인해 변경된 작업 공간 파일을 스냅샷 상태로 롤백합니다."""
        after = await asyncio.to_thread(workspace_checkpoint.snapshot, workspace, None)
        if after is None or before is None:
            return "⚠️ 재실행 뒤의 작업 공간을 읽지 못해 파일을 되돌리지 못했습니다. 변경 내역을 확인해 주십시오."
        result = await asyncio.to_thread(workspace_checkpoint.revert, workspace, before, after)
        if not result.ok:
            logger.warning(f"Could not restore {workspace} after a kernel replay: {result.error or result.drifted}")
            return f"⚠️ 재실행이 바꾼 파일을 되돌리지 못했습니다: {result.error or ', '.join(result.drifted)}"
        if not result.reverted:
            return "재실행이 바꾼 작업 공간 파일은 없습니다."
        names = ", ".join(f"`{change.path}`" for change in result.reverted[:10])
        more = f" 외 {len(result.reverted) - 10}개" if len(result.reverted) > 10 else ""
        return f"재실행이 바꾼 작업 공간 파일 {len(result.reverted)}개를 되돌렸습니다: {names}{more}."

    async def _record(
        self,
        db,
        state: DebateState,
        setup: TurnSetup,
        kernels: List[_Kernel],
        restored: str,
        on_event: Optional[EventCallback],
    ) -> None:
        """코드 재실행 결과를 피드 이벤트 카드로 기록하고 재실행된 도구 이력을 연결합니다."""
        calls = [item for kernel in kernels for item in kernel.calls]

        async def _link(note_id: str) -> None:
            db.add_all(
                self.recorder.tool_row(state, agent, call, message_id=note_id) for agent, call in calls
            )

        lines = []
        for kernel in kernels:
            names = ", ".join(
                (self._agent(setup, key).name if self._agent(setup, key) else key) for key in kernel.agents
            )
            line = f"> - {names}: 셀 {kernel.planned}개 중 {kernel.ran}개 다시 실행"
            lines.append(f"{line} — {kernel.stopped}" if kernel.stopped else line)
        if restored:
            lines.append(f">\n> {restored}")
        await self.recorder.record_note(
            db=db, state=state, on_event=on_event, agent=setup.orchestrator,
            round_number=state.current_round, msg_type="orchestrator",
            content=(
                "> 🔁 **커널 복원** — 사용자의 선택으로 이번 턴에서 정상 완료된 코드 셀을 재실행했습니다.\n>\n"
                + "\n".join(lines)
            ),
            kind=turns.KIND_KERNEL_REPLAY,
            data={"kernels": [
                {"agents": k.agents, "planned": k.planned, "ran": k.ran, "stopped": k.stopped} for k in kernels
            ]},
            tool_calls=[call for _agent, call in calls],
            before_commit=_link,
        )

    @staticmethod
    def _notes(kernels: List[_Kernel]) -> Dict[str, str]:
        """초안을 기반으로 재개되는 발언 프롬프트에 주입할 커널 복구 상태 안내문을 생성합니다."""
        notes: Dict[str, str] = {}
        for kernel in kernels:
            if not kernel.stopped:
                note = KERNEL_REPLAYED_NOTE.format(ran=kernel.ran)
            elif kernel.ran or kernel.calls:
                note = KERNEL_PARTIAL_NOTE.format(planned=kernel.planned, ran=kernel.ran, reason=kernel.stopped)
            else:
                note = KERNEL_NOT_REPLAYED_NOTE.format(reason=kernel.stopped)
            for key in kernel.agents:
                notes[key] = note
        return notes
