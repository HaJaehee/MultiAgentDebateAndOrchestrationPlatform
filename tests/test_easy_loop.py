"""생각 → 행동 → 관찰 나누기 (app/easy/loop.py) 와 도구 이름의 쉬운 말 (app/easy/catalog.py).

지키려는 것:

1. 글 조각과 도구 결과가 온 순서대로 생각 · 행동 · 다음 생각이 쌓인다. 도구 바로 뒤에 온 글도
   다음 생각이다 — 시간으로 추측해 앞 생각에 붙이지 않는다 (빠른 LLM 에서 다음 생각 전체가 붙었습니다).
2. 다시 연 기록은 순서를 모른다는 표시(`restored`)와 함께 행동을 먼저, 본문을 결론으로 둔다.
3. 사회자의 발언은 단계에 따라 계획과 최종 정리로 나뉘고, 계획 승인 기록은 계획이 아니다.
"""

from app.easy.catalog import tool_label
from app.easy.loop import ACTION, AGENT, APPROVAL, PLAN, SYNTHESIS, THOUGHT, USER, LoopTimeline

READ = {"tool_name": "filesystem__read_text_file", "arguments": {"path": "sales.csv"}, "output": "날짜,제품", "status": "success"}
WRITE = {"tool_name": "filesystem__write_file", "arguments": {"path": "r.md"}, "output": "거부", "status": "denied",
         "security": {"decision": "deny"}}


def _start(timeline, mid="m1", key="easy_demo", msg_type="agent"):
    return timeline.apply({"type": "message_stream_start", "message": {
        "id": mid, "sender_key": key, "sender_name": "자료 탐색가", "sender_role": "탐색", "msg_type": msg_type,
    }})


def _chunk(timeline, text, mid="m1"):
    return timeline.apply({"type": "message_stream_chunk", "message_id": mid, "delta": text})


def _tool(timeline, call, key="easy_demo"):
    return timeline.apply({"type": "tool_executed", "agent_key": key, "tool_call": call})


def test_text_and_tools_become_thought_action_thought_in_arrival_order():
    timeline = LoopTimeline()
    _start(timeline)
    assert _chunk(timeline, "먼저 판매 파일을 ").tail_only is False
    assert _chunk(timeline, "열어 보겠습니다.").tail_only is True, "마지막 생각에 붙은 글은 글만 고칩니다"
    _tool(timeline, READ)
    assert _chunk(timeline, "제품별로 더해 보니 ").tail_only is False, "도구 뒤의 글은 새 생각입니다"
    _chunk(timeline, "보조 배터리가 1위입니다.")
    timeline.apply({"type": "message_added", "message": {"id": "m1", "sender_key": "easy_demo", "msg_type": "agent",
                                                          "content": "전문"}})

    speech = timeline.speeches[0]
    assert speech.kind == AGENT and speech.done
    assert [s.kind for s in speech.steps] == [THOUGHT, ACTION, THOUGHT]
    assert speech.steps[0].text == "먼저 판매 파일을 열어 보겠습니다."
    assert speech.steps[1].tool["tool_name"] == "filesystem__read_text_file"
    assert timeline.counts() == {"thought": 2, "action": 1, "observation": 1}


def test_several_tools_in_a_row_are_separate_actions_between_two_thoughts():
    timeline = LoopTimeline()
    _start(timeline)
    _chunk(timeline, "폴더를 살펴보겠습니다.")
    _tool(timeline, READ)
    _tool(timeline, WRITE)
    _chunk(timeline, "쓰기는 거부되었습니다.")

    steps = timeline.speeches[0].steps
    assert [s.kind for s in steps] == [THOUGHT, ACTION, ACTION, THOUGHT]
    assert steps[0].text == "폴더를 살펴보겠습니다."
    assert steps[3].text == "쓰기는 거부되었습니다."


def test_a_tool_result_without_a_live_speech_is_ignored():
    timeline = LoopTimeline()
    assert _tool(timeline, READ) is None
    _start(timeline)
    assert _tool(timeline, READ, key="someone_else") is None


def test_reloaded_speeches_keep_actions_first_and_say_the_order_is_unknown():
    messages = [
        {"id": "u", "sender_key": "user", "msg_type": "user", "content": "가장 많이 팔린 제품은?"},
        {"id": "p", "sender_key": "orchestrator", "msg_type": "orchestrator", "content": "계획"},
        {"id": "a", "sender_key": "easy_demo", "msg_type": "agent", "content": "보조 배터리입니다.",
         "tool_calls": [READ, WRITE]},
        {"id": "s", "sender_key": "orchestrator", "msg_type": "orchestrator", "content": "정리",
         "turn_started_at": "2026-10-08T00:00:00Z"},
    ]
    timeline = LoopTimeline.from_messages(messages)
    kinds = [s.kind for s in timeline.speeches]
    assert kinds == [USER, PLAN, AGENT, SYNTHESIS]
    agent = timeline.speeches[2]
    assert agent.restored and agent.done
    assert [s.kind for s in agent.steps] == [ACTION, ACTION]
    assert agent.text == "보조 배터리입니다."
    assert timeline.counts() == {"thought": 1, "action": 2, "observation": 2}


def test_an_orchestrator_speech_streamed_while_synthesizing_is_the_final_summary():
    timeline = LoopTimeline()
    timeline.apply({"type": "status_changed", "status": "planning"})
    _start(timeline, mid="p", key="orchestrator", msg_type="orchestrator")
    # 계획 승인 기록은 사회자 이름으로 흐르지 않고 바로 기록됩니다 (`_record_note`).
    timeline.apply({"type": "message_added", "message": {
        "id": "ok", "sender_key": "orchestrator", "msg_type": "orchestrator", "content": "[계획 승인] ...",
        "turn_meta": {"kind": "plan_approval"},
    }})
    timeline.apply({"type": "status_changed", "status": "synthesizing"})
    _start(timeline, mid="s", key="orchestrator", msg_type="orchestrator")
    assert [s.kind for s in timeline.speeches] == [PLAN, APPROVAL, SYNTHESIS]


def test_reattaching_to_a_streaming_speech_keeps_it_live():
    messages = [{"id": "a", "sender_key": "easy_demo", "msg_type": "agent", "content": "지금까지 쓴 글"}]
    timeline = LoopTimeline.from_messages(messages, streaming_ids={"a"})
    speech = timeline.speeches[0]
    assert not speech.done
    _tool(timeline, READ)
    assert speech.steps[-1].kind == ACTION


def test_tool_names_become_plain_words_with_their_target():
    assert tool_label("filesystem__read_text_file", {"path": "sales.csv"}) == "파일 읽기: sales.csv"
    assert tool_label("filesystem__list_directory", {"path": "."}) == "폴더 살펴보기: ."
    assert tool_label("sandbox__execute_python_code", {"code": "print(1)"}) == "파이썬 코드 실행"
    assert tool_label("git__git_status", {}) == "변경 이력 다루기"
    assert tool_label("skills__load_skill", {"name": "csv-profile"}) == "업무 매뉴얼(스킬) 펼쳐 보기: csv-profile"
    assert tool_label("custom__frobnicate", None) == "도구 사용: frobnicate"
    assert tool_label("filesystem__read_multiple_files", {"paths": ["a", "b"]}) == "여러 파일 읽기: a, b"
