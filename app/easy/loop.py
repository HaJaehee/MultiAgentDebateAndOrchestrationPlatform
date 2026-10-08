"""토론 이벤트를 생각 → 행동 → 관찰 단계로 나눕니다. 화면을 모르는 순수 로직입니다.

엔진은 발언 글을 조각(`message_stream_chunk`)으로, 실행한 도구를 그 즉시(`tool_executed`)
보냅니다. 두 이벤트가 오는 순서가 곧 에이전트가 일한 순서입니다 — 도구를 부르기 전까지의 글이
생각, 도구 호출이 행동, 그 결과가 관찰, 그 뒤의 글이 다음 생각입니다. 이 모듈은 그 순서대로
단계를 쌓습니다. 엔진은 고치지 않습니다.

두 가지를 알고 씁니다.

* **조각 묶음.** 엔진은 글 조각을 짧게(`STREAM_EVENT_INTERVAL`, 0.1초) 모았다가 보내고, 도구 결과는
  바로 보냅니다. 보통은 도구 인자가 흘러나오고 도구가 도는 사이에 앞 글이 먼저 도착합니다. 도구가 그보다
  빨리 끝나면 앞 생각의 마지막 몇 글자가 다음 생각으로 넘어갈 수 있는데, 그것은 감수합니다. 시간으로
  추측해 붙이면 다음 생각을 빨리 쓰는 LLM 에서 다음 생각 전체가 앞 생각에 붙습니다 (실제로 확인했습니다).
* **다시 연 기록.** DB 에는 발언 본문과 그 발언의 도구 기록만 있고 둘 사이의 순서는 없습니다. 다시
  불러온 발언은 행동을 먼저 모으고 본문을 결론으로 보이며, `restored` 로 그 사실을 알립니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

THOUGHT, ACTION = "thought", "action"
# 발언의 종류. 화면이 모양을 고릅니다.
USER, PLAN, SYNTHESIS, AGENT, ERROR, NOTE = "user", "plan", "synthesis", "agent", "error", "note"
APPROVAL = "approval"  # 사람이 계획을 승인한 기록 (ADR-028). 사회자 이름으로 남지만 발언이 아닙니다.

# 발언이 아니라 엔진이 남긴 기록 (`app/orchestration/turns.py` 의 KIND_*). 사회자 이름으로 남아도 계획이 아닙니다.
_NOTE_KINDS = {"note", "nomination", "assignment", "failure", "interrupted"}


@dataclass
class Step:
    kind: str
    text: str = ""
    tool: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Speech:
    message_id: str
    kind: str
    agent_key: str = ""
    agent_name: str = ""
    agent_role: str = ""
    steps: List[Step] = field(default_factory=list)
    content: str = ""
    done: bool = False
    restored: bool = False

    @property
    def actions(self) -> List[Step]:
        return [s for s in self.steps if s.kind == ACTION]

    @property
    def text(self) -> str:
        """화면에 보일 본문. 끝난 발언은 기록된 본문, 진행 중이면 지금까지의 생각."""
        if self.done and self.content:
            return self.content
        return "\n\n".join(s.text for s in self.steps if s.kind == THOUGHT and s.text)


@dataclass
class Change:
    """방금 바뀐 발언과, 마지막 생각에 글만 붙었는지 (화면은 그때 글만 고칩니다)."""

    speech: Speech
    tail_only: bool = False


def _kind_of(msg: Dict[str, Any], phase: str) -> str:
    msg_type = msg.get("msg_type") or ""
    meta = msg.get("turn_meta")
    record = meta.get("kind") if isinstance(meta, dict) else None
    if record == "plan_approval":
        return APPROVAL
    if record in _NOTE_KINDS:
        return NOTE
    if msg.get("sender_key") == "user" or msg_type == "user":
        return USER
    if msg_type == "error":
        return ERROR
    if msg_type == "system":
        return NOTE
    if msg_type == "orchestrator":
        # 합성 발언은 턴 시작 시각을 들고 기록됩니다. 흐르는 중에는 엔진이 알린 단계로 압니다.
        return SYNTHESIS if msg.get("turn_started_at") or phase == "synthesizing" else PLAN
    return AGENT


class LoopTimeline:
    def __init__(self) -> None:
        self.speeches: List[Speech] = []
        self._by_id: Dict[str, Speech] = {}
        # 지금 말하고 있는 발언 (에이전트 키 → 발언). 도구 결과에는 발언 id 가 없어 이것으로 찾습니다.
        self._live: Dict[str, Speech] = {}
        self.phase = ""

    # ------------------------------------------------------------ 기록에서

    @classmethod
    def from_messages(
        cls, messages: Iterable[Dict[str, Any]], streaming_ids: Optional[Set[str]] = None,
    ) -> "LoopTimeline":
        timeline = cls()
        streaming = streaming_ids or set()
        for msg in messages:
            speech = timeline._add(msg, timeline.phase)
            tools = msg.get("tool_calls") or []
            speech.steps = [Step(ACTION, tool=dict(tc)) for tc in tools]
            speech.restored = bool(tools)
            if msg.get("id") in streaming:
                # 진행 중인 발언에 다시 붙었습니다. 지금까지의 글은 생각 하나로 둡니다.
                speech.steps.append(Step(THOUGHT, text=msg.get("content") or ""))
                speech.restored = True
                timeline._live[speech.agent_key] = speech
            else:
                speech.content = msg.get("content") or ""
                speech.done = True
        return timeline

    def _add(self, msg: Dict[str, Any], phase: str) -> Speech:
        speech = Speech(
            message_id=str(msg.get("id") or ""),
            kind=_kind_of(msg, phase),
            agent_key=msg.get("sender_key") or "",
            agent_name=msg.get("sender_name") or "",
            agent_role=msg.get("sender_role") or "",
        )
        self.speeches.append(speech)
        if speech.message_id:
            self._by_id[speech.message_id] = speech
        return speech

    # ------------------------------------------------------------ 이벤트에서

    def apply(self, event: Dict[str, Any]) -> Optional[Change]:
        etype = event.get("type")
        if etype == "status_changed":
            self.phase = event.get("status") or self.phase
            return None
        if etype == "message_stream_start":
            msg = event.get("message") or {}
            speech = self._by_id.get(str(msg.get("id") or "")) or self._add(msg, self.phase)
            if msg.get("content"):
                # 끊겼다가 이어 가는 발언은 앞서 흐른 글을 들고 시작합니다.
                speech.steps.append(Step(THOUGHT, text=msg["content"]))
            self._live[speech.agent_key] = speech
            return Change(speech)
        if etype == "message_stream_chunk":
            speech = self._by_id.get(str(event.get("message_id") or ""))
            if speech is None or speech.done:
                return None
            return Change(speech, tail_only=self._add_text(speech, event.get("delta") or ""))
        if etype == "tool_executed":
            speech = self._live.get(event.get("agent_key") or "")
            if speech is None or speech.done:
                return None
            speech.steps.append(Step(ACTION, tool=dict(event.get("tool_call") or {})))
            return Change(speech)
        if etype == "message_added":
            msg = event.get("message") or {}
            speech = self._by_id.get(str(msg.get("id") or ""))
            if speech is None:
                speech = self._add(msg, self.phase)
            if speech.kind == PLAN and msg.get("turn_started_at"):
                speech.kind = SYNTHESIS
            speech.content = msg.get("content") or ""
            speech.done = True
            if self._live.get(speech.agent_key) is speech:
                del self._live[speech.agent_key]
            return Change(speech)
        return None

    @staticmethod
    def _add_text(speech: Speech, delta: str) -> bool:
        """글 조각을 붙입니다. 마지막 생각 끝에 붙었으면 True, 도구 뒤의 새 생각을 열었으면 False."""
        if speech.steps and speech.steps[-1].kind == THOUGHT:
            speech.steps[-1].text += delta
            return True
        speech.steps.append(Step(THOUGHT, text=delta))
        return False

    # ------------------------------------------------------------ 세기

    def counts(self) -> Dict[str, int]:
        thoughts = actions = 0
        for speech in self.speeches:
            if speech.kind != AGENT:
                continue
            actions += len(speech.actions)
            thoughts += sum(1 for s in speech.steps if s.kind == THOUGHT and s.text.strip())
            if speech.restored and speech.done and speech.content.strip():
                thoughts += 1
        # 관찰은 행동마다 하나입니다 — 실패나 거부도 에이전트가 읽는 결과입니다.
        return {"thought": thoughts, "action": actions, "observation": actions}
