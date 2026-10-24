"""도구 호출 한 번의 처리: 응답에서 호출 읽기, 도구 보안 판정, 실행(실패도 관측으로), 기록 알림, 지정 스킬 미리 불러오기.

`ToolCallMixin` 은 `LLMCaller`(app/agents/llm.py) 의 일부입니다.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.agents.base import Agent
from app.agents.consultation import is_consultation_tool, run_consultation_tool
from app.agents.skills import (
    DESIGNATED_SKILL_NOTE,
    LOAD_SKILL_TOOL,
    is_skill_tool,
    offered_skills,
    run_skill_tool,
)
from app.mcp.manager import MCPManager


logger = logging.getLogger(__name__)


class ToolCallMixin:
    """도구 호출 처리. 도구 루프 자체는 `llm_loop.ToolLoopMixin` 의 것입니다."""

    @staticmethod
    def _parse_tool_call(tc: Any) -> Tuple[str, Dict[str, Any], str, bool]:
        """모델이 돌려준 tool_call 하나를 `(이름, 인자, id, 읽었는가)` 로 풉니다.

        프로바이더마다 모양이 다르고(객체/딕셔너리), 인자는 모델이 만든
        문자열이라 깨져 있을 수 있습니다. 여기서 터지면 발언 전체가 날아가므로
        어떤 모양이 와도 읽을 수 있는 만큼만 읽고 나머지는 비웁니다.

        네 번째 값은 **인자를 온전히 읽었는가**입니다. 못 읽었다는 사실은 도구
        실행뿐 아니라 그 발언을 되돌려 보낼 때도 필요합니다 — 우리가 읽지 못한
        JSON 을 엔드포인트라고 읽을 수 있는 것은 아니기 때문입니다
        (`_assistant_turn` 참고).
        """
        def _get(obj: Any, key: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        try:
            function = _get(tc, "function") or {}
            fn_name = _get(function, "name") or ""
            fn_args_raw = _get(function, "arguments")
            call_id = _get(tc, "id") or ""
        except Exception:  # noqa: BLE001 - 알 수 없는 모양이면 통째로 포기합니다
            return "", {}, "", False

        parsed_ok = True
        if isinstance(fn_args_raw, dict):
            fn_args: Dict[str, Any] = fn_args_raw
        elif isinstance(fn_args_raw, str):
            try:
                parsed = json.loads(fn_args_raw or "{}")
                fn_args = parsed if isinstance(parsed, dict) else {"input": parsed}
            except Exception:  # noqa: BLE001 - 모델이 만든 JSON 은 자주 깨집니다
                fn_args = {"raw": fn_args_raw}
                parsed_ok = False
        elif fn_args_raw is None:
            fn_args = {}
        else:
            fn_args = {"input": fn_args_raw}

        # tool_call_id 가 비면 OpenAI 호환 엔드포인트가 다음 요청을 거절합니다.
        return (
            str(fn_name),
            fn_args,
            str(call_id) or f"call_{abs(hash((fn_name, id(tc)))):x}",
            parsed_ok,
        )

    @staticmethod
    def _assistant_turn(message: Any, parsed: List[Tuple[str, Dict[str, Any], str, bool]]) -> Dict[str, Any]:
        """도구를 부른 발언을 **우리가 실제로 실행한 모양으로** 정규화해 되돌립니다.

        예전에는 `message.model_dump()` 를 그대로 다시 넣었습니다. 그러면 모델이
        만든 인자 문자열이 손대지 않은 채 다음 요청마다 계속 실려 나갑니다.
        `max_tokens` 에 걸려 JSON 이 중간에서 잘린 경우 — 실제로 겪었습니다,
        `filesystem__write_file` 이 `... if path:` 에서 끊겼습니다 — **우리가
        읽지 못했다고 인정한 그 JSON 을 엔드포인트에 다시 보내는 셈**이고,
        vLLM 은 그것을 채팅 템플릿에 렌더링하다 400 으로 거절했습니다.

        두 가지를 바로잡습니다.

        1. 인자는 항상 `json.dumps` 로 다시 씁니다. 나가는 요청에는 언제나 온전한
           JSON 만 실립니다. 덤으로 **id 도 맞춰집니다** — `_parse_tool_call` 은
           id 가 비면 하나 지어내는데, 예전에는 `tool` 결과만 그 지어낸 id 를 쓰고
           assistant 쪽은 빈 id 그대로여서 짝이 어긋났습니다 (그 자체로 400 입니다).
        2. 못 읽은 인자는 짧은 표시로 바꿉니다. 잘린 14KB 를 되돌려 보내 봐야
           모델에게는 아무 정보도 아니고, 토큰만 먹고, 같은 생성을 다시 유도합니다.
           무슨 일이 있었는지는 `TRUNCATED_TOOL_CALL_NOTICE` 가 말로 설명합니다.
        """
        turn = message.model_dump() if hasattr(message, "model_dump") else dict(message)
        calls = []
        for fn_name, fn_args, call_id, parsed_ok in parsed:
            if parsed_ok:
                arguments = json.dumps(fn_args, ensure_ascii=False)
            else:
                dropped = len(str(fn_args.get("raw", "")))
                arguments = json.dumps(
                    {"_unreadable": f"인자 {dropped:,}자를 읽지 못해 생략했습니다"},
                    ensure_ascii=False,
                )
            calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": fn_name or "unknown_tool", "arguments": arguments},
            })
        turn["tool_calls"] = calls
        return turn

    @staticmethod
    async def _check_tool_gate(
        tool_gate: Optional[Any],
        agent: Agent,
        fn_name: str,
        fn_args: Dict[str, Any],
        mcp: Any,
    ) -> Optional[Any]:
        """도구 보안 판정. 문지기가 없으면 None (판정 없이 실행)."""
        if tool_gate is None:
            return None
        return await tool_gate.check(agent, fn_name, fn_args, mcp)

    async def _execute_tool_safely(
        self,
        agent: Agent,
        fn_name: str,
        fn_args: Dict[str, Any],
        session_id: Optional[str],
        mcp: Optional[MCPManager] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str]:
        """MCP 도구(또는 스킬 도구)를 호출하고, 실행 결과를 항상 (문자열, 상태) 튜플 형태로 반환합니다.

        `MCPManager.execute_tool`에서 예외를 대부분 처리하지만, 매니저 객체가 교체되거나
        도구 명칭 조회 과정에서 예외가 발생할 가능성이 있습니다. 개별 도구 호출 실패가
        전체 발언의 중단으로 이어져서는 안 된다는 원칙을 본 단계에서 재차 보장합니다.
        단, 작업 취소 예외는 상위로 그대로 전달합니다.

        스킬 도구는 외부 MCP 서버가 아닌 애플리케이션 프로세스 내부에서 직접 실행합니다 (`app/agents/skills.py`).
        스크립트가 복사될 작업 공간은 해당 발언이 사용하는 런타임의 작업 공간이며, 실행 가능 여부는 해당 발언에
        실제로 부여된 도구 목록(`tools`)을 기준으로 판정합니다.
        """
        runtime = mcp or self.mcp_manager
        try:
            if is_skill_tool(fn_name):
                output, status = await run_skill_tool(
                    agent, fn_name, fn_args,
                    workspace=getattr(runtime, "workspace", None), tools=tools,
                )
            elif is_consultation_tool(fn_name):
                output, status = await run_consultation_tool(
                    agent, fn_name, fn_args,
                    llm_caller=self,
                )
            else:
                output, status = await runtime.execute_tool(
                    fn_name, fn_args, scope=session_id, actor=agent.key
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 도구 실패로 발언을 죽이지 않습니다
            logger.error(
                f"Tool '{fn_name}' raised out of the MCP manager for {agent.name}: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return (
                f"도구 '{fn_name}' 실행이 실패했습니다 ({type(exc).__name__}: {exc}). "
                f"다른 방법을 쓰거나, 이미 확보한 정보만으로 결론을 내세요.",
                "error",
            )

        if not isinstance(output, str):
            output = str(output)
        return output, (status or "error")

    async def _preload_skills(
        self,
        agent: Agent,
        names: Sequence[str],
        messages: List[Dict[str, Any]],
        tool_logs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]],
        session_id: Optional[str],
        mcp: Optional[MCPManager],
    ) -> bool:
        """지정한 스킬의 `load_skill` 을 모델 대신 불러, 부른 기록과 결과를 `messages` 끝에 붙입니다.

        모델이 직접 부른 것과 같은 모양입니다 — 도구를 부른 assistant 발언 하나와 그 결과들. 그래서
        화면에도 도구 카드로 남고, 이어 가기와 컨텍스트 자르기도 다른 호출과 똑같이 다룹니다. 이번
        발언의 도구 목록에 없는 스킬(그사이 꺼졌거나 지워진 것)은 건너뜁니다 — 내놓지 않은 도구를
        부른 기록을 만들면 요청이 거절될 수 있습니다. 하나라도 불렀으면 True.
        """
        offered = set(offered_skills(tools))
        wanted = [name for name in dict.fromkeys(names) if name in offered]
        skipped = [name for name in dict.fromkeys(names) if name not in offered]
        if skipped:
            logger.info(f"Designated skill(s) not available to {agent.name} now: {', '.join(skipped)}")
        if not wanted:
            return False

        calls = [
            (name, {"skill": name}, f"call_{uuid.uuid4().hex[:12]}")
            for name in wanted
        ]
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": LOAD_SKILL_TOOL, "arguments": json.dumps(args, ensure_ascii=False)},
                }
                for _name, args, call_id in calls
            ],
        })
        for _name, args, call_id in calls:
            output, status = await self._execute_tool_safely(
                agent, LOAD_SKILL_TOOL, args, session_id, mcp, tools=tools,
            )
            if status == "success":
                output = f"{DESIGNATED_SKILL_NOTE}\n\n{output}"
            call_log = {
                "tool_name": LOAD_SKILL_TOOL,
                "arguments": args,
                "output": output,
                "status": status,
                "security": {},
            }
            tool_logs.append(call_log)
            await self._notify_tool_call(agent, on_tool_call, call_log)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": LOAD_SKILL_TOOL,
                "content": output,
            })
        return True

    @staticmethod
    async def _notify_tool_call(
        agent: Agent,
        on_tool_call: Optional[Callable[[Dict[str, Any]], Any]],
        call_log: Dict[str, Any],
    ) -> None:
        """도구 실행을 화면·기록 쪽에 알립니다. 알리다 실패해도 발언은 계속됩니다.

        콜백은 UI 이벤트를 타고 나갑니다. 브라우저가 닫혔거나 큐가 막혔을 때
        여기서 올라온 예외가 발언을 끝내면, 실제로 실행된 도구의 관측을 잃습니다.
        """
        if not on_tool_call:
            return
        try:
            if asyncio.iscoroutinefunction(on_tool_call):
                await on_tool_call(call_log)
            else:
                result = on_tool_call(call_log)
                if asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.warning(
                f"Tool-call notification failed for {agent.name} "
                f"({call_log.get('tool_name')}): {type(exc).__name__}: {exc}"
            )
