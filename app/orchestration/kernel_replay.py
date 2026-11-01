"""끊긴 턴을 이어 가기 전에 샌드박스 커널의 변수를 되살립니다 (ADR-046).

서버가 내려가면 커널 프로세스도 함께 사라져 변수와 임포트가 없어집니다. 그런데 초안의 대화에는 그 변수를
만든 기록이 남아 있어, 모델은 있다고 믿고 코드를 씁니다. 커널의 메모리는 저장되지 않지만 실행한 코드는
도구 기록에 있으므로(`turns.kernel_cells`), 사람이 고르면 같은 코드를 순서대로 다시 실행합니다.

코드를 다시 돌리는 일이라 세 가지를 지킵니다.

1. **사람이 고릅니다.** "이어서 진행" 은 코드를 다시 돌리지 않습니다. 사람 없이 도구 실행이 시작되면 안
   됩니다 (ADR-024). 다시 실행하는 버튼은 따로 있습니다.
2. **지금의 보안 설정을 다시 받습니다.** 셀마다 도구 보안 판정을 거칩니다. 끊긴 사이 조인 규칙을 옛 기록으로
   건너뛰면 안 됩니다. 막히면 거기서 멈춥니다.
3. **작업 공간의 파일은 그대로 둡니다.** 옛 셀에는 그 뒤에 고친 파일을 옛 내용으로 덮어쓰는 코드가 들어
   있을 수 있습니다. 변수만 되살리려는 일이 파일을 되돌리면 안 되므로, 다시 실행하기 전의 스냅샷으로
   파일을 돌립니다. 스냅샷을 못 만들거나 같은 작업 공간에서 다른 턴이 도는 중이면 실행하지 않습니다.
   네트워크 호출처럼 작업 공간 밖으로 나간 일은 되돌릴 수 없습니다.

되살린 상태는 근사치입니다. 난수·시각·외부 상태에 달린 값은 달라질 수 있고, 실패한 셀이 도중까지 만든 변수나
이전 턴에서 만든 변수는 없습니다. 그래서 첫 실패에서 멈추고, 어디까지 되살렸는지 모델과 사람에게 그대로 알립니다.
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

# 실패 사유로 보여 주는 글자 수.
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
    """커널 하나를 다시 실행한 결과."""

    namespace: str
    agents: List[str]
    planned: int
    ran: int = 0
    # 끝까지 못 돌았으면 그 이유.
    stopped: str = ""
    # 실행한 호출 (에이전트, 도구 기록). 보안 판정에 막힌 호출도 감사 기록에 남도록 함께 둡니다.
    calls: List[Tuple[Agent, Dict[str, Any]]] = field(default_factory=list)


def agents_of(cells: Dict[str, List[Any]]) -> List[str]:
    """셀을 실행한 에이전트 (처음 나온 순서)."""
    return list(dict.fromkeys(row.agent_key for rows in cells.values() for row in rows))


def _failure_reason(output: str) -> str:
    """실패한 셀의 사유 한 줄. stderr 의 마지막 줄이 `NameError: …` 입니다."""
    found = re.search(r"--- stderr ---\n(.*?)(?:\n--- stdout ---|\Z)", output, re.S)
    lines = [line.strip() for line in (found.group(1) if found else output).splitlines() if line.strip()]
    return (lines[-1] if lines else "결과를 읽지 못했습니다")[:REASON_CHARS]


async def _call(
    mcp: Any, gate: Any, session_id: str, agent: Agent, tool_name: str, arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """보안 판정을 받고 도구를 부릅니다. 도구 기록 모양으로 돌려줍니다."""
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
    """커널 셀을 다시 실행하고, 그 사실을 기록에 남깁니다."""

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
        """이 턴의 셀을 다시 실행합니다. 돌려주는 값은 {에이전트 키: 모델에게 알릴 말} 입니다."""
        turn_messages = state.messages[state.turn_message_start:]
        cells = await turns.turn_kernel_cells(db, state.session_id, state.turn_id, turn_messages)
        if not cells:
            return {}
        # 셀은 몇 분씩 걸릴 수 있습니다. 읽은 트랜잭션을 닫아 그동안 연결을 쥐고 있지 않습니다 (ADR-041).
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
        """다시 실행하면 안 되는 이유. 없으면 빈 문자열."""
        if self._runtime(state) is None:
            return "도구 서버가 떠 있지 않습니다"
        others = get_runtime_pool().shared_with(workspace, state.session_id)
        if others:
            # 끝난 뒤 파일을 스냅샷으로 돌리면, 그 사이 다른 턴이 쓴 파일까지 함께 돌아갑니다.
            return "같은 작업 공간에서 다른 대화가 진행 중입니다"
        return ""

    def _runtime(self, state: DebateState) -> Any:
        """이 턴의 MCP 런타임. 풀에 없으면 `LLMCaller` 처럼 기본 런타임으로 물러섭니다."""
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
        """커널을 비우고 셀을 순서대로 실행합니다. 처음 막히거나 실패한 자리에서 멈춥니다."""
        mcp = self._runtime(state)
        emptied = False
        for number, row in enumerate(rows, start=1):
            if control is not None and control.stop_requested:
                kernel.stopped = "유저가 정지를 요청했습니다"
                return
            agent = self._agent(setup, row.agent_key)
            meta = mcp.tool_meta(row.tool_name)
            # 끊긴 사이 에이전트의 도구 권한이나 서버가 빠졌으면 그때의 호출을 되풀이하지 않습니다.
            if agent is None or meta is None or meta.server not in agent.allowed_mcp_servers:
                kernel.stopped = f"{number}번째 셀의 도구를 지금은 쓸 수 없습니다 ({row.tool_name})"
                return
            if not emptied:
                # 커널이 살아 있을 수도 있습니다 (엔진 오류로 멈춘 턴). 비우고 시작해야 코드가 두 번 쌓이지 않습니다.
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
        """다시 실행이 바꾼 파일을 스냅샷으로 돌립니다. 기록에 적을 한 줄을 돌려줍니다."""
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
        """무엇을 다시 실행했는지 피드에 남기고, 실행한 도구 기록을 그 안내에 잇습니다."""
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
                "> 🔁 **커널 복원** — 유저의 선택으로 이번 턴에서 끝까지 실행된 코드 셀을 다시 실행했습니다.\n>\n"
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
        """초안에서 이어 가는 발언에게 알릴 말. 다시 실행하지 못한 커널은 비어 있다고 적습니다."""
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
