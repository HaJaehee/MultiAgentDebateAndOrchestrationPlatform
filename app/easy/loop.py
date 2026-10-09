"""토론 스트리밍 이벤트를 '생각 → 행동 → 관찰' 단계로 구조화하는 모듈. UI와 독립적인 순수 도메인 로직입니다.

엔진은 발언 텍스트를 청크 단위(`message_stream_chunk`)로, 실행된 도구 결과를 즉시(`tool_executed`)
발행합니다. 두 이벤트의 수신 순서는 곧 에이전트의 실제 작업 순서와 같습니다. 즉, 도구를 호출하기 전까지 생성된 텍스트는
'생각', 도구 호출은 '행동', 도구 실행 결과는 '관찰', 그 뒤에 이어지는 텍스트는 '다음 생각'이 됩니다.
이 모듈은 엔진 코드를 변경하지 않고 이벤트 수신 순서대로 타임라인 단계를 구성합니다.

동작 원리 및 주의 사항:

* **청크 버퍼링(Chunk Batching)**: 엔진은 텍스트 청크를 짧은 주기(`STREAM_EVENT_INTERVAL`, 0.1초)로 버퍼링하여 전송하고, 도구 결과는
  즉시 전송합니다. 일반적으로 도구 인자 스트리밍 및 실행 시간 덕분에 이전 텍스트가 먼저 도착합니다. 만약 도구 실행이 그보다
  빠르게 완료되면 이전 생각의 마지막 몇 글자가 다음 생각 단계로 넘어갈 수 있으나, 이는 정상적인 동작으로 처리합니다. 시간차 추정
  방식으로 결합할 경우 빠른 모델에서 다음 생각 전체가 이전 생각에 잘못 병합되는 현상이 발생하기 때문입니다.
* **저장된 세션 복원(Restored Session)**: 데이터베이스에는 발언 본문과 도구 호출 기록만 저장되며 이벤트 순서는 별도로 보존되지 않습니다.
  따라서 다시 불러온 발언은 도구 행동을 먼저 나열하고 발언 본문을 최종 결론으로 표시하며, `restored` 플래그로 복원된 상태임을 안내합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

THOUGHT, ACTION = "thought", "action"
# 발언 종류. UI에서 표시 형태를 결정합니다.
USER, PLAN, SYNTHESIS, AGENT, ERROR, NOTE = "user", "plan", "synthesis", "agent", "error", "note"
APPROVAL = "approval"  # 사용자 계획 승인 기록 (ADR-028). 오케스트레이터 명의로 저장되지만 일반 발언이 아닙니다.

# 발언이 아닌 엔진 시스템 기록 (`app/orchestration/turns.py`의 KIND_*). 오케스트레이터 명의로 남아도 계획 발언이 아닙니다.
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
        """화면에 표시할 발언 본문. 완료된 발언은 최종 본문, 진행 중인 발언은 현재까지 누적된 생각 텍스트를 반환합니다."""
        if self.done and self.content:
            return self.content
        return "\n\n".join(s.text for s in self.steps if s.kind == THOUGHT and s.text)


@dataclass
class Change:
    """타임라인 변경 상태. 변경된 발언 객체와 마지막 생각 텍스트의 단순 갱신 여부를 담습니다."""

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
        # 최종 종합 발언은 턴 시작 시각 필드를 포함합니다. 스트리밍 중에는 엔진의 현재 상태(phase)로 판별합니다.
        return SYNTHESIS if msg.get("turn_started_at") or phase == "synthesizing" else PLAN
    return AGENT


class LoopTimeline:
    def __init__(self) -> None:
        self.speeches: List[Speech] = []
        self._by_id: Dict[str, Speech] = {}
        # 현재 발언 중인 상태 (에이전트 키 → 발언 매핑). 도구 실행 결과에는 발언 ID가 포함되지 않으므로 키로 발언을 매핑합니다.
        self._live: Dict[str, Speech] = {}
        self.phase = ""

    # ------------------------------------------------------------ DB 기록 복원

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
                # 진행 중인 발언을 재연결했습니다. 현재까지 수신된 텍스트를 단일 생각 단계로 설정합니다.
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

    # ------------------------------------------------------------ 실시간 이벤트 처리

    def apply(self, event: Dict[str, Any]) -> Optional[Change]:
        etype = event.get("type")
        if etype == "status_changed":
            self.phase = event.get("status") or self.phase
            return None
        if etype == "message_stream_start":
            msg = event.get("message") or {}
            speech = self._by_id.get(str(msg.get("id") or "")) or self._add(msg, self.phase)
            if msg.get("content"):
                # 중단 후 재개된 발언은 이전에 수신된 텍스트를 포함하여 시작합니다.
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
        """텍스트 청크를 추가합니다. 기존 생각 단계 끝에 추가되었으면 True, 도구 실행 후 새로운 생각 단계를 시작했으면 False를 반환합니다."""
        if speech.steps and speech.steps[-1].kind == THOUGHT:
            speech.steps[-1].text += delta
            return True
        speech.steps.append(Step(THOUGHT, text=delta))
        return False

    # ------------------------------------------------------------ 단계별 통계 집계

    def counts(self) -> Dict[str, int]:
        thoughts = actions = 0
        for speech in self.speeches:
            if speech.kind != AGENT:
                continue
            actions += len(speech.actions)
            thoughts += sum(1 for s in speech.steps if s.kind == THOUGHT and s.text.strip())
            if speech.restored and speech.done and speech.content.strip():
                thoughts += 1
        # 관찰 횟수는 행동 횟수와 동일합니다 — 도구 실패나 실행 거부 역시 에이전트가 확인하는 관찰 결과입니다.
        return {"thought": thoughts, "action": actions, "observation": actions}
