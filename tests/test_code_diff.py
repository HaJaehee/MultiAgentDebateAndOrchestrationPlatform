"""코드 변경점 뷰어 시험 (ADR-043).

1. unified diff 해석: 줄 번호, `--- x` 처럼 머리글을 닮은 줄, 폼 피드, 파일 끝 줄바꿈, 바이너리, 잘린 출력.
2. 줄 안 강조와 좌우 정렬: 바뀐 낱말만 표시하고, 끼어든 줄이 아래 짝을 밀지 않으며, 큰 묶음은 시도하지 않습니다.
3. HTML: 모델과 작업 공간에서 온 글은 어디서든 이스케이프되고, 보기를 바꿔도 다시 계산하지 않습니다.
4. 코드 버전: 앞선 것만, 같은 언어만, 같은 코드는 한 번만, 만들어진 뒤의 코드는 빼고 고릅니다.
5. 라운드 경계 스냅샷: 구간 나누기, 이어 가는 턴, 이름 지우기, 실제 턴.
6. 화면 부품: 늦게 온 답 버리기, 비교 기준 제안, 구간 고르기.
"""

import asyncio
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import graph_store
from app import workspace_checkpoint as wc
from app.code_diff import ADD, DEL, MAX_ALIGN_LINES, MAX_COMPARE_CHARS, SAME, FileDiff, diff_texts, parse_unified, similarity
from app.code_versions import CodeVersion, artifact_versions, earlier_versions, speech_code_versions
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.graph import parse_graph
from app.orchestration.turns import KIND_SUBDEBATE_SPEECH
from app.session_ops import TurnChanges, session_code_versions, turn_changes, turn_file_diff
from app.ui.components.code_compare import CodeCompare
from app.ui.components.diff_viewer import SPLIT, UNIFIED, DiffViewer, empty_message, render_diff
from app.ui.components.turn_changes import _ChangesView
from tests.fake_llm import FakeLLMCaller
from tests.test_graph_debate import GraphLLM
from tests.test_graph_debate import _pool as graph_pool
from tests.test_graph_debate import example as graph_example

needs_git =pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")


GIT_DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,4 @@ def main():
 first
-total = price * 2
+total = price * 3
 third
 fourth
@@ -10,2 +10,3 @@ def other():
 ten
+eleven
 twelve
"""


def _rows(diff: FileDiff):
    return [row for hunk in diff.hunks for row in hunk.rows]


# ------------------------------------------------------------ 해석


def test_line_numbers_and_counts_follow_the_hunk_header():
    diff = parse_unified(GIT_DIFF)
    assert (diff.added, diff.removed) == (2, 1)
    first, second = diff.hunks
    assert first.header == "@@ -1,4 +1,4 @@ def main():"
    assert [(l.kind, l.old_no, l.new_no) for l in first.lines] == [
        (SAME, 1, 1), (DEL, 2, None), (ADD, None, 2), (SAME, 3, 3), (SAME, 4, 4),
    ]
    assert [(l.kind, l.old_no, l.new_no) for l in second.lines] == [(SAME, 10, 10), (ADD, None, 11), (SAME, 11, 12)]


def test_a_deleted_line_that_looks_like_a_file_header_stays_a_line():
    text = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n--- 지울 주석\n keep\n"
    diff = parse_unified(text)
    assert [(l.kind, l.text) for l in diff.hunks[0].lines] == [(DEL, "-- 지울 주석"), (SAME, "keep")]
    assert diff.removed == 1


def test_a_form_feed_does_not_split_a_line():
    diff = parse_unified("@@ -1,2 +1,2 @@\n a\x0cb\n-old\n+new\n")
    assert [l.text for l in diff.hunks[0].lines] == ["a\x0cb", "old", "new"]
    assert not diff.truncated


def test_a_hunk_without_counts_means_one_line():
    diff = parse_unified("@@ -3 +3 @@\n-a\n+b\n")
    assert [(l.old_no, l.new_no) for l in diff.hunks[0].lines] == [(3, None), (None, 3)]


def test_missing_final_newline_is_an_attribute_of_the_line_before():
    diff = parse_unified("@@ -1 +1 @@\n-same\n\\ No newline at end of file\n+same\n")
    old, new = diff.hunks[0].lines
    assert old.no_newline and not new.no_newline
    assert "줄바꿈 없음" in render_diff(diff, UNIFIED)


def test_binary_and_empty_diffs_explain_themselves():
    assert parse_unified("Binary files a/x.png and b/x.png differ\n").binary
    assert "바이너리" in empty_message(parse_unified("Binary files a/x.png and b/x.png differ\n"))
    assert "내용 변경이 없습니다" in empty_message(parse_unified("old mode 100644\nnew mode 100755\n"))
    assert empty_message(parse_unified("")) and empty_message(parse_unified(GIT_DIFF)) is None


@pytest.mark.parametrize("cut", [
    "@@ -1,5 +1,5 @@\n one\n two\n\n… (내용이 길어 최대 200,000자까지만 표시합니다)\n",   # 줄 사이에서 잘림
    "@@ -1,5 +1,5 @@\n one\n tw\n… (내용이 길어 최대 200,000자까지만 표시합니다)\n",       # 줄 중간에서 잘림
    "@@ -1,1 +1,1 @@\n-one\n+two\n… (내용이 길어 최대 200,000자까지만 표시합니다)\n",     # 헝크 끝에서 잘림
])
def test_a_cut_off_diff_keeps_what_it_has_and_says_so(cut):
    diff = parse_unified(cut)
    assert diff.truncated and diff.hunks[0].lines
    assert not any("…" in l.text for l in diff.hunks[0].lines)


def test_diff_texts_reads_the_same_way_and_refuses_huge_input():
    diff = diff_texts("a\nb\nc\n", "a\nB\nc\n")
    assert (diff.added, diff.removed) == (1, 1)
    assert [l.kind for l in diff.hunks[0].lines].count(SAME) >= 2
    assert not diff_texts("같음", "같음").hunks
    huge = diff_texts("x" * MAX_COMPARE_CHARS, "y")
    assert huge.too_large and "너무 길어" in empty_message(huge)


def test_similarity_ignores_indentation_and_blank_lines():
    assert similarity("a\nb\nc", "    a\n    b\n    c") == 1
    assert similarity("a\nb", "c\nd") == 0
    assert similarity("a\nb\n\n", "c\nd\n\n") == 0, "빈 줄만 같은 코드는 비슷한 코드가 아닙니다"
    assert similarity("python run.py --version 1", "python run.py --version 2") >= 0.75, "한 줄짜리 명령도 비슷합니다"


# ------------------------------------------------------------ 줄 안 강조와 좌우 정렬


def test_only_the_changed_word_is_marked():
    old, new = parse_unified(GIT_DIFF).hunks[0].lines[1:3]
    assert [old.text[a:b] for a, b in old.spans] == ["2"]
    assert [new.text[a:b] for a, b in new.spans] == ["3"]


def test_unrelated_lines_are_not_marked_but_share_a_row():
    diff = parse_unified("@@ -1 +1 @@\n-alpha beta\n+1234567890\n")
    assert _rows(diff) == [(diff.hunks[0].lines[0], diff.hunks[0].lines[1])]
    assert not diff.hunks[0].lines[0].spans and not diff.hunks[0].lines[1].spans


def test_an_inserted_line_does_not_push_the_pairs_below_out_of_line():
    diff = parse_unified(
        "@@ -1,2 +1,3 @@\n"
        "-value = compute(first)\n"
        "-return value + 1\n"
        "+import brand_new_thing\n"
        "+value = compute(second)\n"
        "+return value + 2\n"
    )
    rows = [(o.text if o else None, n.text if n else None) for o, n in _rows(diff)]
    assert rows == [
        (None, "import brand_new_thing"),
        ("value = compute(first)", "value = compute(second)"),
        ("return value + 1", "return value + 2"),
    ]


def test_a_huge_block_is_not_compared_line_by_line():
    count = MAX_ALIGN_LINES + 1
    body = "".join(f"-old line number {i}\n" for i in range(count)) + "".join(f"+new line number {i}\n" for i in range(count))
    started = time.perf_counter()
    diff = parse_unified(f"@@ -1,{count} +1,{count} @@\n{body}")
    assert time.perf_counter() - started < 1
    assert len(_rows(diff)) == count and not any(l.spans for l in diff.hunks[0].lines)


def test_long_minified_lines_are_not_compared_word_by_word():
    long_old, long_new = "a" * 5_000, "a" * 4_999 + "b"
    diff = parse_unified(f"@@ -1 +1 @@\n-{long_old}\n+{long_new}\n")
    assert not any(l.spans for l in diff.hunks[0].lines)


def test_crlf_is_hidden_unless_only_the_line_ending_changed():
    both = parse_unified("@@ -1,2 +1,2 @@\n ctx\r\n-old\r\n+new\r\n")
    assert [l.text for l in both.hunks[0].lines] == ["ctx", "old", "new"]

    # 파일 전체가 CRLF → LF. 묶음이 커도 짝을 지어, 무엇이 바뀌었는지 보이게 합니다.
    count = MAX_ALIGN_LINES + 1
    body = "".join(f"-line {i}\r\n" for i in range(count)) + "".join(f"+line {i}\n" for i in range(count))
    converted = parse_unified(f"@@ -1,{count} +1,{count} @@\n{body}")
    assert len(_rows(converted)) == count
    old, new = _rows(converted)[0]
    assert old.spans == [(6, 7)] and not new.spans
    assert "<u>␍</u>" in render_diff(converted, SPLIT)


def test_unpaired_lines_lose_their_carriage_return():
    diff = parse_unified("@@ -1 +1,2 @@\n ctx\r\n+brand new\r\n")
    assert [l.text for l in diff.hunks[0].lines] == ["ctx", "brand new"]


# ------------------------------------------------------------ HTML


@pytest.mark.parametrize("mode", [UNIFIED, SPLIT])
def test_everything_from_the_model_is_escaped(mode):
    evil = "<script>alert(1)</script>"
    diff = parse_unified(f"@@ -1,2 +1,2 @@ {evil}\n {evil}\n-{evil} old\n+{evil} new\n")
    rendered = render_diff(diff, mode, old_label=evil, new_label=evil)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_a_marked_word_is_escaped_too():
    diff = parse_unified("@@ -1 +1 @@\n-if a<b:\n+if a>b:\n")
    rendered = render_diff(diff, UNIFIED)
    assert "<mark>&lt;</mark>" in rendered and "<mark>&gt;</mark>" in rendered


def test_the_two_views_show_the_same_lines_differently():
    diff = parse_unified("@@ -1,2 +1,2 @@\n keep\n-gone\n")
    unified = render_diff(diff, UNIFIED, "이전", "이후")
    split = render_diff(diff, SPLIT, "이전", "이후")
    assert '<div class="u d">' in unified and "이전 → 이후" in unified
    assert '<div class="l e"></div>' in split and "<div>이전</div><div>이후</div>" in split


def test_long_diffs_are_chunked_for_the_browser():
    count = 400
    body = "".join(f" line {i}\n" for i in range(count))
    rendered = render_diff(parse_unified(f"@@ -1,{count} +1,{count} @@\n{body}"), UNIFIED)
    assert rendered.count('class="mado-diff-chunk"') == 3


# ------------------------------------------------------------ 코드 버전


def _version(content, language="python", label="v", created_at=None):
    return CodeVersion(content=content, language=language, label=label, created_at=created_at)


def test_only_earlier_code_in_the_same_language_is_offered():
    versions = [
        _version("print(1)", label="R1"),
        _version("echo hi", language="bash", label="bash"),
        _version("print(2)", label="R2"),
        _version("print(3)", label="대상"),
        _version("print(4)", label="나중"),
    ]
    found = earlier_versions("print(3)", "python", versions)
    assert [v.label for v, _ in found] == ["R2", "R1"]


def test_py_and_python_are_the_same_language():
    assert earlier_versions("b", "py", [_version("a"), _version("b")])[0][0].content == "a"


def test_the_same_code_is_offered_once_and_never_against_itself():
    versions = [_version("a", label="처음"), _version("a", label="다시"), _version("b", label="b"), _version("c", label="대상")]
    assert [v.label for v, _ in earlier_versions("c", "python", versions)] == ["b", "다시"]
    versions = [_version("c", label="같은 것"), _version("a", label="a"), _version("c", label="대상")]
    assert [v.label for v, _ in earlier_versions("c", "python", versions)] == ["a"]


def test_code_written_after_the_artifact_is_not_an_earlier_version():
    at = datetime(2026, 11, 1, 12, 0)
    versions = [
        _version("first", label="앞", created_at=at - timedelta(minutes=2)),
        _version("target", label="대상", created_at=at - timedelta(minutes=1)),
        _version("target", label="다음 턴에 그대로 옮겨 적음", created_at=at + timedelta(minutes=5)),
        _version("next", label="뒤", created_at=at + timedelta(minutes=6)),
    ]
    assert [v.label for v, _ in earlier_versions("target", "python", versions, at)] == ["앞"]


def test_an_artifact_missing_from_the_record_compares_with_everything_before_it():
    at = datetime(2026, 11, 1, 12, 0)
    versions = [_version("a", label="앞", created_at=at - timedelta(minutes=1)), _version("b", label="뒤", created_at=at + timedelta(minutes=1))]
    assert [v.label for v, _ in earlier_versions("다시 실행 전 코드", "python", versions, at)] == ["앞"]


def _speech(content, *, round_number=1, turn_id="t1", name="Coder", graph_node_id=None, turn_meta=None):
    return SimpleNamespace(
        content=content, round_number=round_number, turn_id=turn_id, sender_name=name, graph_node_id=graph_node_id,
        turn_meta=turn_meta, created_at=datetime(2026, 11, 1, 3, 4),
    )


def test_labels_name_the_turn_the_round_and_the_speaker():
    speeches = [
        _speech("```python\na = 1\n```", round_number=2),
        _speech("```py\nb = 1\n```\n```python\nc = 1\n```", turn_id="t2"),
        _speech("```python\nd = 1\n```", round_number=3, graph_node_id="n1"),
        _speech("```text\n설명\n```"),
        _speech("코드 없음"),
        _speech("```python\ne = 1\n```", turn_meta={"kind": KIND_SUBDEBATE_SPEECH}),
    ]
    versions = speech_code_versions(speeches, {"t1": 1, "t2": 2})
    assert [v.content for v in versions] == ["a = 1", "b = 1", "c = 1", "d = 1"]
    assert versions[0].label.startswith("턴 1 · R2 · Coder · ")
    assert versions[1].label.startswith("턴 2 · R1 · Coder · 코드 1/2 · ")
    assert versions[1].language == "python"
    assert versions[3].label.startswith("턴 1 · 3단계 · Coder · ")


def test_artifacts_are_versions_when_the_record_cannot_be_read():
    versions = artifact_versions([
        {"artifact_type": "markdown", "content": "보고서", "title": "결론"},
        {"artifact_type": "code", "content": "x", "language": "py", "title": "코드"},
    ])
    assert [(v.content, v.language, v.label) for v in versions] == [("x", "python", "코드")]


# ------------------------------------------------------------ 라운드 경계 스냅샷


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, workspace_checkpoints):
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "t@example.com")
    (root / "app.py").write_text("v = 0\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


def _mark(repo: Path, name: str) -> str:
    return wc.snapshot(repo, wc.turn_ref("t", name))


@needs_git
def test_a_turn_is_split_into_rounds(repo):
    start = _mark(repo, wc.START)
    _mark(repo, "round-1")
    _write(repo, "app.py", "v = 1\n")
    _mark(repo, "round-2")
    _mark(repo, "round-3")                 # 라운드 2 는 아무것도 바꾸지 않았습니다
    _write(repo, "app.py", "v = 3\n")
    _write(repo, "new.py", "n = 1\n")
    _mark(repo, wc.DEBATE_END)
    end = _mark(repo, wc.END)

    segments = wc.turn_segments(repo, "t", start, end)
    assert [(s.label, [c.path for c in s.files]) for s in segments] == [
        ("라운드 1", ["app.py"]),
        ("라운드 2", []),
        ("라운드 3", ["app.py", "new.py"]),
    ]
    assert "+v = 1" in wc.file_diff(repo, segments[0].start, segments[0].end, "app.py")
    assert "+v = 3" in wc.file_diff(repo, segments[2].start, segments[2].end, "app.py")
    assert "-v = 1" in wc.file_diff(repo, segments[2].start, segments[2].end, "app.py")


@needs_git
def test_changes_outside_the_debate_get_their_own_segment_only_when_there_are_some(repo):
    start = _mark(repo, wc.START)
    _write(repo, "plan.md", "계획\n")
    _mark(repo, "round-1")
    _mark(repo, wc.DEBATE_END)
    _write(repo, "report.md", "결론\n")
    end = _mark(repo, wc.END)
    assert [(s.label, [c.path for c in s.files]) for s in wc.turn_segments(repo, "t", start, end)] == [
        ("계획 단계", ["plan.md"]), ("라운드 1", []), ("토론 이후", ["report.md"]),
    ]


@needs_git
def test_a_turn_that_stopped_mid_debate_runs_its_last_round_to_the_end(repo):
    start = _mark(repo, wc.START)
    _mark(repo, "round-1")
    _write(repo, "app.py", "v = 1\n")
    end = _mark(repo, wc.END)
    segments = wc.turn_segments(repo, "t", start, end)
    assert [s.label for s in segments] == ["라운드 1"] and segments[0].end == end


@needs_git
def test_rounds_sort_as_numbers_and_graph_steps_are_named_as_steps(repo):
    start = _mark(repo, wc.START)
    for number in (10, 2, 1):
        _mark(repo, f"step-{number}")
    end = _mark(repo, wc.END)
    assert [s.label for s in wc.turn_segments(repo, "t", start, end)] == ["1단계", "2단계", "10단계"]


@needs_git
def test_a_turn_without_boundaries_has_no_segments(repo):
    start = _mark(repo, wc.START)
    end = _mark(repo, wc.END)
    assert wc.turn_segments(repo, "t", start, end) == []
    assert wc.turn_segments(repo.parent, "t", start, end) == [], "저장소가 아니어도 조용히 빈 목록입니다"


@needs_git
def test_forget_removes_boundaries_and_leaves_other_turns(repo):
    wc.snapshot(repo, wc.turn_ref("t1", wc.START))
    wc.snapshot(repo, wc.turn_ref("t1", "round-1"))
    wc.snapshot(repo, wc.turn_ref("t1", wc.DEBATE_END))
    wc.snapshot(repo, wc.turn_ref("t2", "round-1"))
    wc.forget(repo, ["t1", "없는-턴"])
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/mado") == "refs/mado/turns/t2/round-1"


@needs_git
@pytest.mark.asyncio
async def test_a_resumed_turn_keeps_the_boundaries_it_already_passed(repo):
    engine = OrchestratorEngine(llm_caller=FakeLLMCaller())
    await engine._mark_boundary(repo, "t", "round-1")
    first = wc.resolve(repo, wc.turn_ref("t", "round-1"))
    _write(repo, "app.py", "v = 1\n")
    await engine._mark_boundary(repo, "t", "round-1")
    assert wc.resolve(repo, wc.turn_ref("t", "round-1")) == first


class RoundWriter(FakeLLMCaller):
    """코더가 발언할 때마다 파일을 고치는 대역. 라운드마다 다른 내용이 됩니다."""

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self.spoken = 0

    async def call_agent(self, agent, messages, *args, **kwargs):
        if agent.key == "coder":
            self.spoken += 1
            _write(self.workspace, "app.py", f"v = {self.spoken}\n")
        return await super().call_agent(agent, messages, *args, **kwargs)


async def _run_turn(repo: Path, max_rounds: int = 2):
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"diff-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="라운드", strategy="sequential_debate", max_rounds=max_rounds,
            active_agents=["orchestrator", "architect", "coder", "critic"], workspace_dir=str(repo),
        ))
        await db.commit()
    await OrchestratorEngine(llm_caller=RoundWriter(repo)).run_turn(session_id=sid, user_prompt="고쳐 줘")
    return factory, sid


@needs_git
@pytest.mark.asyncio
async def test_a_real_turn_can_be_read_round_by_round(repo):
    factory, sid = await _run_turn(repo)
    async with factory() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        changes = await turn_changes(db, sid, turn.id)

    assert [c.path for c in changes.files] == ["app.py"]
    assert [s.label for s in changes.segments][:2] == ["라운드 1", "라운드 2"]
    assert all("토론" not in s.label for s in changes.segments), "합성은 파일을 고치지 않았습니다"

    whole = await turn_file_diff(changes, "app.py")
    assert "-v = 0" in whole and "+v = 2" in whole
    round_two = await turn_file_diff(changes, "app.py", changes.segments[1])
    assert "-v = 1" in round_two and "+v = 2" in round_two


class GraphWriter(GraphLLM):
    """구현 노드가 발언할 때 파일을 고치는 그래프 토론 대역."""

    def __init__(self, workspace: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.workspace = workspace

    async def call_agent(self, agent, messages, *args, **kwargs):
        if agent.key == "coder":
            _write(self.workspace, "app.py", "v = 1\n")
        return await super().call_agent(agent, messages, *args, **kwargs)


@needs_git
@pytest.mark.asyncio
async def test_a_graph_turn_is_read_step_by_step(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    graph_store.save_graph(parse_graph(graph_example()))
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"diff-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="그래프", strategy="graph_debate", graph_id="review-loop", max_rounds=3,
            active_agents=["orchestrator", "architect"], workspace_dir=str(repo),
        ))
        await db.commit()
    llm = GraphWriter(repo, gate_answers=['{"decision": "yes", "reason": "해결됨"}'])
    await OrchestratorEngine(agent_pool=graph_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="만들어 줘")

    async with factory() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        changes = await turn_changes(db, sid, turn.id)
    # 설계 → 구현·보안 검토 → 취합 → 판정. 파일은 두 번째 단계에서 바뀝니다.
    assert [(s.label, [c.path for c in s.files]) for s in changes.segments] == [
        ("1단계", []), ("2단계", ["app.py"]), ("3단계", []), ("4단계", []),
    ]


@needs_git
@pytest.mark.asyncio
async def test_a_failed_snapshot_does_not_stop_the_turn(repo, monkeypatch):
    calls = []

    def fail_on_boundaries(workspace, ref):
        calls.append(ref)
        if "round-" in ref or wc.DEBATE_END in ref:
            raise OSError("디스크 가득 참")
        return real(workspace, ref)

    real = wc.snapshot
    monkeypatch.setattr(wc, "snapshot", fail_on_boundaries)
    factory, sid = await _run_turn(repo, max_rounds=1)
    assert any("round-1" in ref for ref in calls)
    async with factory() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        assert turn.status == "completed"
        changes = await turn_changes(db, sid, turn.id)
    assert changes.files and changes.segments == []


@pytest.mark.asyncio
async def test_session_code_versions_reads_specialist_speeches_in_order():
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"code-{uuid.uuid4().hex[:8]}"
    base = datetime(2026, 11, 1, 9, 0)
    async with factory() as db:
        db.add(SessionModel(id=sid, title="코드"))
        db.add(TurnModel(id="turn-b", session_id=sid, started_at=base + timedelta(hours=1)))
        db.add(TurnModel(id="turn-a", session_id=sid, started_at=base))
        rows = [
            ("coder", "agent", "```python\nfirst\n```", "turn-a", 1),
            ("orchestrator", "orchestrator", "```python\n계획 속 코드\n```", "turn-a", 1),
            ("critic", "error", "```python\n실패 안내\n```", "turn-a", 1),
            ("coder", "agent", "```python\nsecond\n```", "turn-b", 2),
        ]
        for index, (key, msg_type, content, turn_id, round_number) in enumerate(rows):
            db.add(MessageModel(
                session_id=sid, sender_key=key, sender_name=key, sender_role="", msg_type=msg_type, content=content,
                turn_id=turn_id, round_number=round_number, created_at=base + timedelta(minutes=index),
            ))
        await db.commit()
    async with factory() as db:
        versions = await session_code_versions(db, sid)
    assert [v.content for v in versions] == ["first", "second"]
    assert versions[0].label.startswith("턴 1 · R1 · coder")
    assert versions[1].label.startswith("턴 2 · R2 · coder")


# ------------------------------------------------------------ 화면 부품


def _html(viewer: DiffViewer) -> str:
    return viewer.content.content


@pytest.mark.asyncio
async def test_switching_views_redraws_without_recomputing(ui_page, monkeypatch):
    with ui_page:
        modes = []
        viewer = DiffViewer(mode=SPLIT, on_mode_change=modes.append)
        viewer.build()
    await viewer.show(parse_unified(GIT_DIFF), "이전", "이후")
    assert 'class="s"' in _html(viewer) and viewer.stats.text == "+2 −1"

    await viewer._set_mode(UNIFIED)
    assert 'class="u' in _html(viewer) and modes == [UNIFIED]

    # 한 번 그린 보기는 다시 만들지 않습니다.
    monkeypatch.setattr("app.ui.components.diff_viewer.render_diff", lambda *a: pytest.fail("다시 그렸습니다"))
    await viewer._set_mode(SPLIT)
    assert 'class="s"' in _html(viewer)


@pytest.mark.asyncio
async def test_a_late_answer_does_not_replace_the_file_picked_after_it(ui_page):
    with ui_page:
        viewer = DiffViewer()
        viewer.build()
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return parse_unified("@@ -1 +1 @@\n-늦은 파일\n+늦은 파일 수정\n")

    async def fast():
        return parse_unified("@@ -1 +1 @@\n-나중 파일\n+나중 파일 수정\n")

    pending = asyncio.create_task(viewer.load(slow))
    await asyncio.sleep(0)
    await viewer.load(fast)
    release.set()
    await pending
    assert "나중 파일" in _html(viewer) and "늦은 파일" not in _html(viewer)


@pytest.mark.asyncio
async def test_a_message_replaces_the_diff_and_clear_frees_it(ui_page):
    with ui_page:
        viewer = DiffViewer()
        viewer.build()
    await viewer.show(parse_unified("Binary files a/x and b/x differ\n"))
    assert "바이너리" in _html(viewer) and viewer.diff is None

    await viewer.show(parse_unified(GIT_DIFF))
    viewer.clear()
    assert "total" not in _html(viewer) and viewer.diff is None and viewer.stats.text == ""


def _compare(art, versions):
    async def load():
        return versions

    return CodeCompare(art, artifacts=lambda: [art], load_versions=load)


@pytest.mark.asyncio
async def test_compare_opens_with_the_most_similar_earlier_code(ui_page):
    art = {"artifact_type": "code", "title": "이 코드", "language": "python", "content": "a = 1\nb = 2\nc = 3\nd = 5\n"}
    versions = [
        _version("a = 1\nb = 2\nc = 3\nd = 4\n", label="R1"),
        _version("전혀 다른 코드\n", label="R2"),
        _version(art["content"], label="이 발언"),
    ]
    with ui_page:
        compare = _compare(art, versions)
        compare.build_button()
        compare.build_panel(compare.button)  # 코드 칸 자리를 차지하는 요소면 충분합니다
    await compare.toggle()

    assert compare.active and compare.button.text == "코드 보기"
    assert [v.label for v, _ in compare.candidates] == ["R2", "R1"]
    assert compare.viewer.labels == ("R1", "이 코드")
    assert "<mark>5</mark>" in _html(compare.viewer)
    assert not compare.warning.visible

    await compare._compare(0)   # 비슷하지 않은 기준을 고르면 알립니다
    assert compare.warning.visible and "비슷한 코드를 찾지 못했습니다" in compare.warning.text
    assert compare.viewer.labels == ("R2", "이 코드")

    await compare.toggle()
    assert not compare.active and compare.button.text == "변경점"


@pytest.mark.asyncio
async def test_compare_says_so_when_there_is_nothing_earlier(ui_page):
    art = {"artifact_type": "code", "title": "첫 코드", "language": "python", "content": "x = 1\n"}
    with ui_page:
        compare = _compare(art, [_version(art["content"])])
        compare.build_button()
        compare.build_panel(compare.button)
    await compare.toggle()
    assert compare.viewer is None and not compare.candidates


@pytest.mark.asyncio
async def test_compare_falls_back_to_artifacts_when_the_record_cannot_be_read(ui_page):
    older = {"artifact_type": "code", "title": "앞 턴", "language": "python", "content": "x = 1\n"}
    art = {"artifact_type": "code", "title": "이 턴", "language": "python", "content": "x = 2\n"}

    async def broken():
        raise RuntimeError("DB 끊김")

    with ui_page:
        compare = CodeCompare(art, artifacts=lambda: [older, art], load_versions=broken)
        compare.build_button()
        compare.build_panel(compare.button)
    await compare.toggle()
    assert [v.label for v, _ in compare.candidates] == ["앞 턴"]


@needs_git
@pytest.mark.asyncio
async def test_the_dialog_view_moves_between_the_whole_turn_and_a_round(repo, ui_page):
    start = _mark(repo, wc.START)
    _mark(repo, "round-1")
    _write(repo, "app.py", "v = 1\n")
    _mark(repo, "round-2")
    _write(repo, "other.py", "o = 1\n")
    _mark(repo, "round-3")
    end = _mark(repo, wc.END)
    changes = TurnChanges(
        turn_id="t", workspace=repo, start=start, end=end, files=wc.changes(repo, start, end),
        segments=wc.turn_segments(repo, "t", start, end),
    )
    with ui_page:
        view = _ChangesView(changes, mode=UNIFIED, on_mode_change=lambda mode: None)
        view.build()

    await view.open_first_file()
    assert view.selected == "app.py" and "턴 이전 → 턴 이후" in _html(view.viewer)

    await view._on_pick_segment(SimpleNamespace(value=2))     # 라운드 2: app.py 는 없습니다
    assert view.selected == "other.py" and list(view.rows) == ["other.py"]
    assert "라운드 2 이전 → 라운드 2 이후" in _html(view.viewer)

    await view._on_pick_segment(SimpleNamespace(value=3))     # 라운드 3: 바뀐 파일 없음
    assert view.selected is None and "이 구간에 바뀐 파일이 없습니다" in _html(view.viewer)

    await view._on_pick_segment(SimpleNamespace(value=0))
    assert view.selected == "app.py"
    assert "bg-slate-800" in view.rows["app.py"]._classes


@needs_git
@pytest.mark.asyncio
async def test_the_dialog_view_has_no_segment_picker_for_an_old_turn(repo, ui_page):
    start = _mark(repo, wc.START)
    _write(repo, "app.py", "v = 1\n")
    end = _mark(repo, wc.END)
    changes = TurnChanges(turn_id="t", workspace=repo, start=start, end=end, files=wc.changes(repo, start, end))
    with ui_page:
        view = _ChangesView(changes, mode=SPLIT, on_mode_change=lambda mode: None)
        view.build()
    assert len(view.segments) == 1
    await view.open_first_file()
    assert 'class="s"' in _html(view.viewer)
