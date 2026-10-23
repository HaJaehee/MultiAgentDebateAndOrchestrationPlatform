"""응답이 온전히 오지 않았을 때 되살리기: 응답 한도에서 잘린 답 이어 받기, 빈 답·사고 과정에만 남은 답,
본문에 새어 나온 도구 호출, 도구 상한에 닿은 뒤 도구 없이 마무리하기.

`AnswerRecoveryMixin` 은 `LLMCaller`(app/agents/llm.py) 의 일부입니다.
"""

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agents.base import Agent
from app.agents.llm_budget import (
    BUDGET_EXHAUSTED_INSTRUCTION,
    completion_budget_report,
    message_text,
    truncation_advice,
)
from app.agents.llm_context import (
    context_budget,
    context_headroom,
    effective_max_tokens,
    estimate_tokens,
    max_tokens_label,
    tool_schema_tokens,
)
from app.agents.llm_types import ContextArbiter, LLMUnavailableError
from app.agents.reasoning import conclusion_from_reasoning, reasoning_quote

logger = logging.getLogger(__name__)


# 답을 다시 요청할 때. 사고를 처음부터 다시 하면 또 같은 자리에서 끝나므로, 직전 사고의
# 끝부분을 건네고 거기서 결론만 쓰라고 합니다.
ANSWER_AFTER_REASONING_INSTRUCTION = (
    "[답변 없음] 직전 응답은 사고(reasoning)에 응답 한도(max_tokens={max_tokens})를 모두 써서 "
    "**본문을 한 글자도 쓰지 못했습니다.** 사고는 이미 충분히 했습니다. 처음부터 다시 "
    "생각하지 말고, 곧바로 최종 답변 본문을 쓰세요. 사고는 최소한으로 줄이세요."
)

# 한도와 무관하게, 답이 사고 안에만 있고 본문이 빈 채로 **정상 종료**했을 때.
#
# vLLM 의 reasoning parser(Qwen3·DeepSeek-R1 등)는 모델이 사고 종료 표식을 내지 않거나 답을
# 사고 블록 안에 쓰면 출력 전체를 `reasoning_content` 로 분류합니다. `finish_reason` 은
# `stop` 이라 v0.7.0 의 한도 복구에도 걸리지 않았고, `prompt` 모드는 사고를 버리므로 발언이
# 로그 한 줄 없이 **빈 카드**로 남았습니다.
ANSWER_ONLY_IN_REASONING_INSTRUCTION = (
    "[답변 없음] 직전 응답은 끝까지 쓰였지만 **답이 사고(reasoning) 안에만 있고 본문이 "
    "비어** 있었습니다. 유저에게는 본문만 보입니다. 사고를 다시 하지 말고, 최종 답변을 "
    "본문으로 쓰세요."
)

ANSWER_ONLY_IN_REASONING_FOOTER = (
    "> ⚠️ **모델이 답을 사고(reasoning) 안에만 쓰고 본문을 비워 두었습니다.** 답을 본문으로 "
    "다시 요청해도 오지 않았습니다. 서버의 reasoning parser 가 이 모델의 사고 종료 표식과 맞지 "
    "않을 수 있습니다."
)

REASONING_CARRY_NOTE = "\n\n[직전 사고의 끝부분 — 여기서 결론만 이어 쓰세요]\n{reasoning}"

# 건네는 사고의 최대 길이(글자). 통째로 넣으면 그것이 다시 한도를 먹습니다.
REASONING_CARRY_CHARS = 2000

ANSWER_NOW_AGAIN = "\n\n[다시 요청] 이번에도 본문이 비었습니다. 사고 없이 결론부터 짧게 쓰세요."

# 이어받기 호출마저 사고에 한도를 다 썼을 때 한 번 더 부탁하는 말.
CONTINUE_WITHOUT_REASONING = (
    "\n\n[다시 요청] 직전 이어받기는 사고에 한도를 다 써서 글이 오지 않았습니다. "
    "사고 없이 곧바로 이어 쓰세요."
)

# ---------------------------------------------------------------- 본문으로 새어 나온 도구 호출
#
# 서버의 도구 파서가 모델이 만든 호출을 해석하지 못하면, 호출 표식이 `tool_calls` 가
# 아니라 **본문 글자로** 흘러나옵니다 (호출이 한도에 걸려 잘렸거나, 파서가 그 모델의
# 형식과 맞지 않을 때). 예전에는 그것을 평범한 답변으로 받았습니다. 도구는 실행되지 않았고,
# 잘렸으면 이어받기가 **호출 표식을 산문처럼 이어 썼고**, 카드에는 표식이 그대로 남았습니다.
#
# 표식은 모델 계열마다 다릅니다. 여기 있는 것은 널리 쓰이는 형식이고, 새 형식은 이 목록에
# 더하면 됩니다. 산문에서 우연히 나올 수 있는 모양은 넣지 않습니다.
LEAKED_TOOL_CALL_MARKERS = (
    re.compile(r"<tool_call>\s*[\[{]"),             # Hermes · Qwen 2.5/3 계열
    re.compile(r"\[TOOL_CALLS\]"),                   # Mistral
    re.compile(r"<\|python_tag\|>"),                 # Llama 3.1 내장 도구
    re.compile(r"<function=[\w.\-]+>\s*\{"),         # Llama 3.x 사용자 정의 함수 형식
    re.compile(r"<｜tool▁calls▁begin｜>"),            # DeepSeek V3 · R1
    re.compile(r"to=functions\.[\w.\-]+"),           # gpt-oss (harmony)
)

_FENCED_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)

# 새어 나온 호출을 모델에게 알리고 다시 부르게 하는 횟수. 같은 일이 되풀이되면 서버
# 파서와 모델 형식이 맞지 않는 것이라, 무한히 다시 시켜도 소용이 없습니다.
MAX_LEAKED_TOOL_CALL_RETRIES = 2

LEAKED_TOOL_CALL_NOTICE = (
    "[도구 호출 실패] 직전 응답에서 도구 호출이 **본문 글자로** 나왔습니다. 서버가 그것을 "
    "도구 호출로 해석하지 못해 **실행되지 않았습니다.**{cause}\n"
    "도구가 필요하면 정식 도구 호출로 다시 부르세요. 호출 표식을 본문에 직접 쓰지 마세요."
)

# 새어 나온 호출만 있던 판을 대화에 되돌려 넣을 때의 자리표시. 빈 assistant 본문은
# 몇몇 엔드포인트가 거절합니다.
LEAKED_TOOL_CALL_PLACEHOLDER = "(도구를 호출하려 했지만 호출 표식이 본문으로 나와 실행되지 않았습니다.)"

LEAKED_TOOL_CALL_FOOTER = (
    "> ⚠️ **도구 호출이 본문 글자로 새어 나와 실행하지 못했습니다.** 서버가 모델의 호출 표식을 "
    "도구 호출로 해석하지 못했습니다 — 응답 한도에 걸려 호출이 잘렸거나, 서버의 도구 파서가 이 "
    "모델의 형식과 맞지 않을 수 있습니다. 새어 나온 표식은 이 발언에서 지웠습니다."
)

REASONING_EXHAUSTED_FOOTER = (
    "> ⚠️ **이 발언은 사고(reasoning)에 응답 한도(max_tokens={max_tokens})를 모두 써서 본문을 "
    "받지 못했습니다.** 답을 다시 요청해도 본문이 오지 않았습니다. `max_tokens` 를 올리거나, "
    "추론 강도를 낮추거나, 요구 범위를 좁혀 다시 물어보세요."
)

def find_leaked_tool_call(text: str) -> Optional[int]:
    """본문에 새어 나온 도구 호출 표식이 시작하는 위치. 없으면 None.

    코드 블록 안은 보지 않습니다 — 도구 호출 형식을 **설명하는** 답변이 그것을 예시로
    적는 것은 정상입니다. 표식 앞에 같은 줄의 특수 토큰(`<|start|>...` 따위)이 붙어
    있으면 그 자리부터로 잡아, 남기는 본문에 부스러기가 섞이지 않게 합니다.
    """
    if not text:
        return None
    fences = [(m.start(), m.end()) for m in _FENCED_BLOCK.finditer(text)]
    earliest: Optional[int] = None
    for pattern in LEAKED_TOOL_CALL_MARKERS:
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in fences):
                continue
            if earliest is None or match.start() < earliest:
                earliest = match.start()
            break
    if earliest is None:
        return None
    line_start = text.rfind("\n", 0, earliest) + 1
    special = text.find("<|", line_start, earliest)
    return special if special != -1 else earliest

def leaked_tool_call_notice(
    max_tokens: str, finish_reason: str, tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """새어 나온 호출을 모델에게 알리는 말. 한도에 걸린 것이면 그 사실과 대처를 붙입니다."""
    cause = ""
    if finish_reason == "length":
        cause = (
            f" 응답 한도(max_tokens={max_tokens})에 걸려 호출이 중간에 잘린 탓일 가능성이 "
            f"큽니다 — 같은 호출을 그대로 다시 보내면 또 잘립니다." + truncation_advice(tools)
        )
    return LEAKED_TOOL_CALL_NOTICE.format(cause=cause)

# 발언이 응답 한도에서 끊겼음을 기록과 화면에 남깁니다.
#
# `TRUNCATED_TOOL_CALL_NOTICE` 는 **모델에게** 하는 말이고(다음 판에서 만회하라),
# 이쪽은 **사람에게** 하는 말입니다. 도구 호출이 잘리면 도구 서버가 거절해 주지만,
# 그냥 긴 글이 잘리면 아무도 이의를 제기하지 않습니다 — 문장 중간에서 끝난 발언이
# 그대로 저장되고, 읽는 사람은 그것이 잘린 것인지 원래 그렇게 끝난 것인지 알 수
# 없습니다. 다음 발언자와 최종 합성도 마찬가지고요.
TRUNCATED_ANSWER_FOOTER = (
    "> ⚠️ **응답 한도(max_tokens={max_tokens})에 걸려 이 발언은 여기서 잘렸습니다.** "
    "끝맺지 못한 문장이나 닫히지 않은 코드 블록이 있을 수 있습니다. "
    "이어서 받으려면 남은 부분을 다시 요청하거나, 에이전트 설정의 `max_tokens` 를 올리세요."
)

# 이어받기를 다 쓰고도 끝나지 않았을 때. 위와 달리 "이미 N번 이어받았다" 를 밝힙니다 —
# 그래야 사람이 `max_continuations` 를 더 올릴지, `max_tokens` 를 올릴지, 아니면 애초에
# 요구 범위를 좁힐지 판단할 수 있습니다.
CONTINUED_BUT_STILL_TRUNCATED_FOOTER = (
    "> ⚠️ **이어받기 {used}회를 모두 쓰고도 끝나지 않아 여기서 멈췄습니다** "
    "(응답 한도 max_tokens={max_tokens}). 끝맺지 못한 문장이나 닫히지 않은 코드 블록이 "
    "있을 수 있습니다. `max_tokens` 를 올리거나, 요구 범위를 좁혀 다시 물어보세요."
)

# 이어받기를 요청하는 지시문.
#
# **이어붙일 것**이라는 사실을 분명히 해야 합니다. 그러지 않으면 모델은 새 답변을
# 시작하는 것으로 알고 "네, 이어서 설명드리겠습니다" 같은 서두를 붙이거나 앞부분을
# 요약해 되풀이합니다. 둘 다 이음매에 그대로 남습니다.
CONTINUE_ANSWER_INSTRUCTION = (
    "[이어쓰기] 바로 위 당신의 글은 응답 한도에 걸려 **문장 중간에서 잘렸습니다.**\n"
    "그 마지막 글자 **바로 다음부터** 이어서 계속 쓰세요.\n"
    "- 이 답변은 앞 글에 **그대로 이어 붙습니다.** 인사말·머리말·'이어서 쓰겠습니다' 같은 "
    "말을 앞에 넣지 마세요.\n"
    "- 앞에서 이미 쓴 내용을 요약하거나 되풀이하지 마세요. 잘린 지점부터 새 내용만 쓰세요.\n"
    "- 코드 블록이나 표 안에서 잘렸다면 그 안에서 계속 쓰고, 끝나면 제대로 닫으세요.\n"
    "- 문서를 끝까지 마무리하세요."
)

BUDGET_WRAP_UP_FOOTER = (
    "> ⚠️ **도구 호출 상한({limit}회)에 도달**해 도구 없이 마무리한 발언입니다 "
    "(도구 {tool_calls}건 실행). 더 확인이 필요하면 에이전트 설정의 "
    "`max_tool_iterations` 를 올리거나, 다음 요청에서 범위를 좁혀 다시 물어보세요."
)


class AnswerRecoveryMixin:
    """응답 되살리기. 모델 호출 자체(`_complete_once`)는 `LLMCaller` 의 것입니다."""

    @staticmethod
    def _finish_reason(response: Any) -> str:
        """응답이 왜 끝났는지. 모르면 빈 문자열 — 판단을 막지는 않습니다."""
        try:
            return str(getattr(response.choices[0], "finish_reason", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _finish_truncated_answer(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        segments: List[str],
        finish_reason: str,
        on_chunk: Optional[Callable[[str], Any]] = None,
        *,
        last_text: Optional[str] = None,
        reasoning: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """`max_tokens` 에 걸려 잘린 발언을 이어받아 `segments` 를 채웁니다.

        잘린 발언에 표시만 남기는 것으로 충분하지 않은 자리가 있습니다. **최종 합성
        보고서**가 그렇습니다 — 그것이 이 대화의 산출물인데, 문장 중간에서 끊긴 채로
        저장되면 표시가 붙어 있어도 결국 처음부터 다시 돌려야 합니다.

        그래서 잘렸으면 한 판 더 부릅니다. 지금까지 쓴 글을 assistant 발언으로 넣고
        "그 마지막 글자 바로 다음부터 이어서" 라고 지시한 뒤, 돌아온 조각을 **구분자
        없이 그대로 이어 붙입니다.** `segments` 의 다른 조각들은 빈 줄로 이어지지만
        여기는 한 문장이 반으로 갈린 자리라, 빈 줄을 넣으면 그 자리가 문단 경계로
        보이게 됩니다.

        멈추는 조건이 셋입니다. 끝까지 받았거나(`finish_reason` 이 `length` 가 아님),
        `max_continuations` 를 다 썼거나, 이어받기 호출 자체가 실패했거나. 어느
        쪽이든 **그때까지 받은 글은 남깁니다** — 이어받기는 발언을 더 낫게 하려는
        것이지, 실패하면 앞의 것까지 버리라는 것이 아닙니다.

        도구는 넘기지 않습니다. 지금은 쓰던 글을 마저 쓰는 자리이지 새로 무언가를
        확인할 자리가 아닙니다.

        `last_text` 는 **한도에 닿은 그 응답**의 본문입니다. 비어 있으면 이어받을 글이
        없습니다 — 추론 모델이 속으로 생각하는 데 한도를 다 쓴 경우입니다. 예전에는 그때도
        `segments[-1]` 을 이어받았는데, 도구를 부른 발언이면 그것은 **앞선 판의 글**이라
        엉뚱한 자리에 이어 붙였고, 도구를 안 부른 발언이면 목록이 비어 꼬리표만 남았습니다.
        그 경우는 `_recover_empty_answer` 가 답을 다시 받아 옵니다.

        `tools` 는 넘기되 **부르지 못하게** 합니다 (`tool_choice="none"`). 이어받기는 도구를
        쓴 뒤의 대화에서도 일어나는데, Anthropic 은 tool_use/tool_result 가 든 대화를
        `tools` 없이 보내면 400 으로 거절합니다 — `_wrap_up_without_tools` 가 겪은 것과
        같습니다. 예전에는 도구 없이 불러, claude 계열에서 도구를 쓴 발언의 이어받기가
        조용히 실패했습니다.
        """
        if finish_reason != "length":
            return
        label = max_tokens_label(agent)
        if last_text is not None and not last_text.strip():
            await self._recover_empty_answer(agent, messages, segments, on_chunk, reasoning, tools)
            return
        if not segments or agent.max_continuations <= 0:
            segments.append(TRUNCATED_ANSWER_FOOTER.format(max_tokens=label))
            return

        used = 0
        nudged = False
        for _ in range(agent.max_continuations):
            # 매번 새로 조립합니다. 예전에는 대화에 이어 붙여, 부를 때마다 지금까지의 글
            # 전체가 assistant 턴으로 한 벌씩 더 쌓였습니다.
            convo = list(messages) + [
                {"role": "assistant", "content": segments[-1]},
                {"role": "user", "content": CONTINUE_ANSWER_INSTRUCTION
                    + (CONTINUE_WITHOUT_REASONING if nudged else "")},
            ]
            try:
                message, finish_reason = await self._complete_once(
                    agent, convo, tools, on_chunk, tool_choice="none" if tools else "auto",
                )
            except Exception as exc:  # noqa: BLE001 - 이어받기 실패로 앞의 글을 버리지 않습니다
                logger.warning(f"Continuation failed for {agent.name}: {type(exc).__name__}: {exc}")
                break

            piece = self._compose_content(agent, message)
            if not piece.strip():
                # 이어받기 호출도 사고에 한도를 다 썼을 수 있습니다. 한 번은 사고 없이
                # 이어 쓰라고 다시 부탁합니다 (남은 횟수 안에서).
                if getattr(message, "reasoning_content", None) and not nudged:
                    nudged = True
                    logger.warning(
                        f"Continuation for {agent.name} spent max_tokens on reasoning; "
                        f"asking once more without it"
                    )
                    continue
                logger.warning(f"Continuation returned nothing for {agent.name}; stopping")
                break

            used += 1
            segments[-1] = segments[-1] + piece      # 한 문장이 갈린 자리입니다
            if finish_reason != "length":
                logger.info(f"Continued a truncated answer for {agent.name}: {used} time(s)")
                return

        if used:
            segments.append(CONTINUED_BUT_STILL_TRUNCATED_FOOTER.format(used=used, max_tokens=label))
        else:
            segments.append(TRUNCATED_ANSWER_FOOTER.format(max_tokens=label))

    async def _recover_empty_answer(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        segments: List[str],
        on_chunk: Optional[Callable[[str], Any]],
        reasoning: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        cause: str = "length",
    ) -> bool:
        """본문이 빈 응답에서 답을 다시 받아 옵니다. 받았으면 True.

        원인이 둘이고, 모델에게 하는 말과 사람에게 남기는 꼬리표가 다릅니다.

        * `cause="length"` — 사고만 하다 응답 한도에 닿았습니다. 추론 모델은 속으로 생각한
          토큰도 `max_tokens` 에 넣어 셉니다.
        * `cause="stop"` — 끝까지 썼는데 답이 사고 안에만 있습니다 (`_answer_left_in_reasoning`).

        다시 부를 때 그냥 같은 질문을 던지면 같은 길이로 다시 생각하다 같은 자리에서
        끝납니다. 그래서 (1) 본문이 비었다는 사실과 원인을 알리고, (2) 직전 사고의 끝부분을
        건네 "여기서 결론만 쓰라" 고 하고, (3) 그래도 비면 사고 없이 결론부터 쓰라고 한 번
        더 부탁합니다. `max_continuations` 번까지만 부릅니다. 끝내 본문이 없으면 원인을
        밝히는 꼬리표를 남깁니다.

        "받았다" 는 **본문**을 받았다는 뜻입니다. `native` 모드는 사고를 인용 블록으로 본문
        앞에 붙이므로, 합친 글이 아니라 모델이 준 본문 자체를 봐야 합니다.
        """
        label = max_tokens_label(agent)
        if cause == "length":
            logger.warning(
                f"{agent.name} reached max_tokens with no answer text "
                f"(reasoning={len(reasoning or ''):,} chars, max_tokens={effective_max_tokens(agent)}); "
                f"asking for the answer"
            )
            instruction = ANSWER_AFTER_REASONING_INSTRUCTION.format(max_tokens=label)
            footer = REASONING_EXHAUSTED_FOOTER.format(max_tokens=label)
        else:
            logger.warning(
                f"{agent.name} finished with no answer text but {len(reasoning or ''):,} chars of "
                f"reasoning and no conclusion marker in it; asking for the answer"
            )
            instruction = ANSWER_ONLY_IN_REASONING_INSTRUCTION
            footer = ANSWER_ONLY_IN_REASONING_FOOTER

        if agent.max_continuations <= 0:
            segments.append(footer)
            return False

        tail = (reasoning or "").strip()[-REASONING_CARRY_CHARS:]
        if tail:
            instruction += REASONING_CARRY_NOTE.format(reasoning=tail)

        for attempt in range(agent.max_continuations):
            convo = list(messages)
            self._append_budget_notice(convo, instruction + (ANSWER_NOW_AGAIN if attempt else ""))
            try:
                message, finish_reason = await self._complete_once(
                    agent, convo, tools, on_chunk, tool_choice="none" if tools else "auto",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Answer recovery failed for {agent.name}: {type(exc).__name__}: {exc}")
                break

            answer = message_text(message).strip()
            if answer:
                segments.append(self._compose_content(agent, message).strip())
                logger.info(
                    f"Recovered an answer for {agent.name} after an empty response "
                    f"({cause}, {attempt + 1} call(s))"
                )
                if finish_reason == "length":
                    # 되받은 답도 잘렸으면 평소처럼 이어받습니다.
                    await self._finish_truncated_answer(
                        agent, convo, segments, finish_reason, on_chunk,
                        last_text=answer, tools=tools,
                    )
                return True

        segments.append(footer)
        return False

    async def _answer_left_in_reasoning(
        self,
        agent: Agent,
        messages: List[Dict[str, Any]],
        segments: List[str],
        on_chunk: Optional[Callable[[str], Any]],
        reasoning: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        reasoning_shown: bool = False,
    ) -> None:
        """본문 없이 **정상 종료**했는데 사고(reasoning)에만 글이 있는 응답을 수습합니다.

        v0.7.0 은 본문이 빈 채 **한도에 닿은** 경우만 복구했습니다. 그런데 한도와 무관하게,
        서버의 reasoning parser 가 사고 종료 표식을 못 찾으면 출력 전체가 사고로 분류되고
        `finish_reason` 은 `stop` 입니다. `prompt` 모드는 사고를 버리므로 발언이 로그 한 줄
        없이 빈 카드로 남았습니다 — 사고 안에 `## 최종 결론` 이 멀쩡히 들어 있어도요.

        1. 사고에 결론 마커가 있으면, 서버가 분류를 틀렸을 뿐 답은 다 쓰인 것입니다. 그대로
           본문으로 옮깁니다 (추가 호출 없음). 원래 본문에 왔어야 할 글이므로 `show_steps`
           도 평소처럼 적용됩니다.
        2. 없으면 사고만 한 것이라 답을 다시 요청합니다 (`_recover_empty_answer`).
        3. 그래도 없으면 꼬리표로 알리고, `show_steps` 가 켜져 있으면 받은 사고를 인용 블록으로
           함께 남깁니다 — 빈 카드보다는 모델이 무엇을 생각했는지라도 보이는 편이 낫습니다.
           이미 `native` 모드가 사고를 본문 앞에 붙였다면(`reasoning_shown`) 다시 붙이지 않습니다.
        """
        if conclusion_from_reasoning(reasoning):
            logger.warning(
                f"{agent.name} returned its whole answer inside reasoning ({len(reasoning):,} chars) "
                f"with no answer text; the conclusion was found there and used as the answer"
            )
            segments.append(reasoning.strip())
            return

        recovered = await self._recover_empty_answer(
            agent, messages, segments, on_chunk, reasoning, tools, cause="stop",
        )
        if not recovered and not reasoning_shown and agent.sequential_thinking.show_steps:
            segments.insert(len(segments) - 1, reasoning_quote(reasoning))

    @staticmethod
    def _append_budget_notice(messages: List[Dict[str, Any]], text: str) -> None:
        """예산 고지를 대화 끝에 붙입니다.

        마지막 메시지가 user 면 그 안에 이어 붙입니다. 새 메시지로 넣으면 user 가
        연달아 두 번이 되어 Anthropic·Gemini 와 여러 OpenAI 호환 셔임이 400 으로
        거절합니다 (`merge_consecutive_roles` 의 사연과 같습니다). 도구 결과 뒤라면
        role 이 tool 이므로 그냥 새로 붙입니다.
        """
        last = messages[-1] if messages else None
        if last is not None and last.get("role") == "user" and isinstance(last.get("content"), str):
            messages[-1] = {**last, "content": f"{last['content']}\n\n{text}"}
            return
        messages.append({"role": "user", "content": text})

    async def _ask_context(
        self,
        arbiter: "ContextArbiter",
        agent: Agent,
        window: int,
        messages: List[Dict[str, Any]],
        tool_calls: int,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """컨텍스트가 넘쳤음을 사람에게 알리고 답을 받아 옵니다.

        묻다가 실패해도 발언은 살립니다 — 답을 못 받은 것으로 보고 트림으로
        넘어갑니다 (도구 예산 쪽 중재자와 같은 태도입니다).
        """
        try:
            answer = await arbiter({
                "agent_key": agent.key,
                "agent_name": agent.name,
                "window": window,
                "used": estimate_tokens(agent.model, messages),
                "budget": context_budget(agent, window, tools),
                "headroom": context_headroom(agent),
                "tool_calls": tool_calls,
            })
            return answer or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Context arbiter failed for {agent.name}: {exc}")
            return {}

    async def _wrap_up_without_tools(
        self,
        agent: Agent,
        current_messages: List[Dict[str, Any]],
        segments: List[str],
        tool_logs: List[Dict[str, Any]],
        limit: int,
        on_chunk: Optional[Callable[[str], Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """예산이 바닥난 자리에서 도구 없이 결론만 받아 냅니다.

        예전에는 여기서 `LLMUnavailableError` 를 올렸습니다. 그러면 그때까지
        스트리밍으로 흘러나온 글과 실행된 도구 관측이 전부 실패 안내로 덮여
        사라졌습니다 — 도구를 많이 쓴 긴 발언일수록 잃는 것이 컸고, 정작 원인은
        엔드포인트 장애가 아니라 우리가 정한 상한이었습니다. 상한은 폭주를
        막으라고 있는 것이지, 한 일을 버리라고 있는 것이 아닙니다.
        """
        logger.warning(
            f"Tool budget exhausted for {agent.name}: {limit} call(s), "
            f"{len(tool_logs)} tool execution(s). Asking for a final answer without tools."
        )
        self._append_budget_notice(
            current_messages,
            BUDGET_EXHAUSTED_INSTRUCTION.format(limit=limit, tool_calls=len(tool_logs)),
        )
        try:
            # 도구 목록은 **넘기되 부르지 못하게** 합니다 (`tool_choice="none"`).
            #
            # 예전에는 `tools` 를 아예 빼고 불렀습니다. 의도는 옳았습니다 — 상한을
            # 알려 주고도 목록을 함께 주면 모델은 또 부르려 하고, 그 호출은 실행되지
            # 않은 채 버려지니까요. 그런데 이 시점의 `current_messages` 에는 이미
            # 실행한 도구의 `tool_calls` assistant 메시지와 `tool` 결과가 쌓여
            # 있습니다. **Anthropic 은 tool_use/tool_result 가 든 대화를 `tools`
            # 없이 보내면 400 으로 거절합니다** ("Requests which include `tool_use`
            # or `tool_result` blocks must define tools"). OpenAI 는 받아주므로,
            # 도구를 많이 쓴 긴 발언에서 claude 계열 에이전트만 이유 없이 죽는
            # 모양으로 나타났습니다.
            #
            # `tool_choice="none"` 은 두 요구를 모두 만족시킵니다 — 도구는 정의되어
            # 있고, 모델은 그것을 부를 수 없습니다.
            message, finish_reason = await self._complete_once(
                agent, current_messages, tools, on_chunk,
                tool_choice="none" if tools else "auto",
            )
            final = self._compose_content(agent, message).strip()
            # 도구를 못 부르게 했는데도 호출 표식을 본문으로 쓰는 모델이 있습니다. 여기서는
            # 다시 부르게 할 수 없으니, 표식을 지우고 그 사실만 남깁니다.
            leak_at = find_leaked_tool_call(final) if tools else None
            if leak_at is not None:
                logger.warning(
                    f"Tool call leaked into the wrap-up answer of {agent.name}; it cannot run here"
                )
                final = final[:leak_at].rstrip()
            if final:
                segments.append(final)
            # 한도가 아닌데 본문이 없고 사고에만 글이 있으면, 답이 사고로 분류된 것입니다.
            answer_text = message_text(message).strip()
            final_reasoning = getattr(message, "reasoning_content", None) or ""
            if (leak_at is None and finish_reason != "length"
                    and not answer_text and final_reasoning.strip()):
                await self._answer_left_in_reasoning(
                    agent, current_messages, segments, on_chunk, final_reasoning, tools,
                    reasoning_shown=bool(final),
                )
            # 마무리 발언도 한도에 걸릴 수 있습니다. 예산 소진 꼬리표와 둘 다 붙는
            # 것이 맞습니다 — 서로 다른 두 한도에 걸린 것이고, 사람이 올릴 손잡이도
            # 각각 다릅니다 (`max_tool_iterations` 와 `max_tokens`).
            if finish_reason == "length" and leak_at is None:
                logger.warning(
                    f"Truncated wrap-up answer from {agent.name}: "
                    f"max_tokens={effective_max_tokens(agent)}, "
                    + completion_budget_report(
                        message, None, None, estimate_tokens(agent.model, current_messages),
                        agent.max_context_window, tool_schema_tokens(agent.model, tools),
                    )
                )
                await self._finish_truncated_answer(
                    agent, current_messages, segments, finish_reason, on_chunk,
                    # 합친 글이 아니라 모델이 준 본문으로 판단합니다. `native` 모드는 사고를
                    # 인용 블록으로 앞에 붙이므로, 합친 글로 보면 빈 본문을 놓칩니다.
                    last_text=answer_text,
                    reasoning=final_reasoning,
                    tools=tools,
                )
            if leak_at is not None:
                segments.append(LEAKED_TOOL_CALL_FOOTER)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Final tool-free answer failed for {agent.name}: {exc}")
            if not segments:
                raise LLMUnavailableError(
                    agent,
                    f"도구 호출 상한 {limit}회를 모두 쓴 뒤의 마무리 호출도 실패했습니다: "
                    f"{type(exc).__name__}: {exc}",
                ) from exc

        segments.append(BUDGET_WRAP_UP_FOOTER.format(limit=limit, tool_calls=len(tool_logs)))
        return "\n\n".join(segments), tool_logs
