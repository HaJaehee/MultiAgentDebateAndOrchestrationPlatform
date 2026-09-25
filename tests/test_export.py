"""대화를 마크다운 문서로 내보내는 규칙.

저장 파일은 나중에 이 대화를 남에게 보여주거나 다른 도구로 넘길 때 쓰는 유일한
사본입니다. 그래서 지키는 것.

* 발언 본문을 그대로 옮긴다. LLM 산출물에는 코드 블록이 들어 있는 일이 흔한데,
  울타리 처리를 잘못하면 문서 뒷부분이 통째로 코드로 보인다.
* 도구 실행 기록을 두 번 보여주지 않는다.
* 파일 이름이 항상 만들어진다 (제목이 비었거나 파일명에 못 쓰는 글자여도).
"""

from datetime import datetime

from app.export import build_session_markdown, safe_filename

SESSION = {
    "title": "분산 캐시 설계",
    # 일부러 옛 이름입니다. 이미 저장된 대화가 이 값을 들고 있습니다.
    "strategy": "sequential_review",
    "max_rounds": 2,
    "active_agents": ["orchestrator", "architect"],
    "custom_instructions": "비동기 파이썬 기준으로 작성",
    "workspace_dir": "",
    "created_at": datetime(2026, 8, 29, 10, 30, 0),
    "updated_at": datetime(2026, 8, 29, 11, 0, 0),
}

MESSAGES = [
    {
        "id": "m1", "sender_key": "user", "sender_name": "User", "sender_role": "Client",
        "content": "캐시 계층을 설계해줘", "round_number": 0, "msg_type": "user",
        "created_at": datetime(2026, 8, 29, 10, 30, 5), "tool_calls": [],
    },
    {
        "id": "m2", "sender_key": "architect", "sender_name": "System Architect",
        "sender_role": "Architecture", "content": "제안입니다.\n\n```python\nx = 1\n```",
        "round_number": 1, "msg_type": "agent",
        "created_at": datetime(2026, 8, 29, 10, 31, 0),
        "tool_calls": [{"tool_name": "read_file", "arguments": {"path": "a.py"},
                        "output": "ok", "status": "success"}],
    },
]


def test_document_carries_the_whole_conversation():
    md = build_session_markdown(SESSION, MESSAGES, artifacts=[], tool_calls=[])

    assert md.startswith("# 분산 캐시 설계")
    # 저장된 키가 아니라 지금 이 대화가 실제로 도는 전략의 이름이 적힙니다.
    # (자유 토론·순차 검증은 순차 토론 하나로 합쳐졌습니다.)
    assert "순차 토론" in md
    assert "sequential_review" not in md
    assert "비동기 파이썬 기준으로 작성" in md
    # 라운드별로 묶입니다.
    assert "### 준비 및 계획" in md
    assert "### Round 1" in md
    # 발언자와 종류가 드러납니다.
    assert "🙋 유저" in md
    assert "System Architect (Architecture)" in md
    # 발언 본문과 도구 실행 기록이 함께 남습니다.
    assert "캐시 계층을 설계해줘" in md
    assert "read_file" in md
    assert "<details>" in md


def test_code_blocks_inside_a_speech_do_not_swallow_the_document():
    """발언 안의 ``` 가 그대로 나가면 그 뒤가 전부 코드로 보입니다."""
    md = build_session_markdown(SESSION, MESSAGES, artifacts=[], tool_calls=[])
    body_fences = md.count("```python\nx = 1\n```")
    assert body_fences == 1, "발언 본문의 코드 블록은 그대로 살아 있어야 합니다"
    # 도구 출력은 더 긴 울타리로 감싸 본문과 섞이지 않습니다.
    assert "````" in md or "```\nok\n```" in md


def test_tool_output_with_backticks_gets_a_longer_fence():
    messages = [{
        "id": "m1", "sender_key": "coder", "sender_name": "Coder", "sender_role": "Impl",
        "content": "실행했습니다", "round_number": 1, "msg_type": "agent",
        "created_at": None,
        "tool_calls": [{"tool_name": "run", "arguments": {},
                        "output": "```\nprint(1)\n```", "status": "error"}],
    }]
    md = build_session_markdown(SESSION, messages)
    assert "````" in md, "출력 안의 울타리보다 긴 울타리로 감싸야 합니다"
    assert "❌" in md, "실패한 도구 호출은 성공과 구분되어야 합니다"


def test_orphan_tool_calls_are_listed_once_at_the_end():
    """어느 발언에서 나왔는지 기록이 없는 도구 호출도 문서에 남습니다."""
    orphan = {"message_id": None, "agent_key": "coder", "tool_name": "git_status",
              "arguments": {}, "output": "clean", "status": "success", "created_at": None}
    attached = {"message_id": "m2", "agent_key": "architect", "tool_name": "read_file",
                "arguments": {}, "output": "ok", "status": "success", "created_at": None}

    md = build_session_markdown(SESSION, MESSAGES, tool_calls=[orphan, attached])

    assert "## 도구 실행 기록" in md
    assert md.count("git_status") == 1
    # 이미 발언에 붙어 나온 것은 뒤에서 다시 보여주지 않습니다.
    assert md.count("read_file") == 1


def test_artifacts_keep_markdown_readable_and_fence_the_rest():
    artifacts = [
        {"artifact_type": "markdown", "title": "보고서", "content": "## 결론\n\n좋습니다", "language": "markdown"},
        {"artifact_type": "mermaid", "title": "다이어그램", "content": "graph TD\n A-->B", "language": "mermaid"},
    ]
    md = build_session_markdown(SESSION, MESSAGES, artifacts=artifacts)

    assert "## 최종 산출물" in md
    # 마크다운 산출물은 울타리로 감싸면 렌더링되지 않습니다.
    assert "## 결론\n\n좋습니다" in md
    # 그 외 형식은 감쌉니다.
    assert "```mermaid\ngraph TD\n A-->B\n```" in md


def test_empty_conversation_still_produces_a_document():
    md = build_session_markdown({"title": ""}, [])
    assert "아직 오간 발언이 없습니다" in md


def test_filename_is_always_usable():
    when = datetime(2026, 8, 29, 14, 5)
    assert safe_filename("분산 캐시 설계", when) == "분산_캐시_설계_20260829-1405.md"
    # 파일명에 쓸 수 없는 글자는 걸러냅니다.
    assert "/" not in safe_filename("a/b:c*d?", when)
    assert "?" not in safe_filename("a/b:c*d?", when)
    # 제목이 비어도 이름이 남습니다.
    assert safe_filename("", when) == "debate_20260829-1405.md"
    assert safe_filename("   ", when).startswith("debate_")
