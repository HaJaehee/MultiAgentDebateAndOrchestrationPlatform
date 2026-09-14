"""작업 공간 파일·전문가 @언급과 작업 공간 업로드 (v0.8.0).

언급은 **경로만** 전달합니다. 사용자 메시지는 모든 발언자의 전사와 합성에 라운드마다
복사되므로, 파일 내용을 붙이면 컨텍스트가 입력 쪽에서 포화됩니다.
"""

import io
import os
from pathlib import Path

import pytest

from app.ui.mention_input import MENTION_INPUT_CLASS, MENTION_JS, MENTION_QUERY_EVENT
from app.workspace_files import (
    LARGE_FILE_BYTES,
    MAX_UPLOAD_BYTES,
    REFERENCE_MARKER,
    MentionAgent,
    WorkspaceIndex,
    WorkspacePathError,
    agents_for_mentions,
    expand_mentions,
    get_workspace_index,
    mention_token,
    safe_workspace_path,
    sanitize_upload_name,
    scan_workspace,
    store_workspace_upload,
    strip_reference_block,
    suggest_mentions,
)

ROOT = Path(__file__).resolve().parents[1]

AGENTS = [
    MentionAgent("architect", "System Architect", "Architecture", True),
    MentionAgent("coder", "Senior Python Engineer", "Implementation", True),
    MentionAgent("critic", "Quality Critic", "Review", False),
]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text("# spec", encoding="utf-8")
    (tmp_path / "docs" / "요구사항 정의서.pdf").write_bytes(b"%PDF-1.7 binary")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cache.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "bundle.js").write_text("", encoding="utf-8")
    (tmp_path / "debug.log").write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("out/\n*.log\n# comment\n!keep.log\n", encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ 목록


def test_scan_skips_heavy_and_ignored_folders_but_keeps_binaries(ws):
    paths = {e.path for e in scan_workspace(ws).entries}
    assert {"docs", "docs/spec.md", "docs/요구사항 정의서.pdf", "src", "src/cache.py"} <= paths
    assert not any(p.startswith(("node_modules", ".git/", "out")) for p in paths)
    assert "debug.log" not in paths, ".gitignore 의 *.log"


def test_scan_stops_at_the_entry_cap(ws):
    scan = scan_workspace(ws, max_entries=2)
    assert len(scan.entries) == 2 and scan.truncated


def test_scan_of_a_missing_folder_is_empty(tmp_path):
    assert scan_workspace(tmp_path / "nope").entries == []


def test_index_remembers_briefly_and_forgets_on_invalidate(ws):
    index = WorkspaceIndex(ttl=60)
    first = index.get(ws)
    (ws / "new.txt").write_text("", encoding="utf-8")
    assert index.get(ws) is first, "TTL 안에서는 다시 훑지 않습니다"
    index.invalidate(ws)
    assert "new.txt" in {e.path for e in index.get(ws).entries}


def test_suggestions_put_active_specialists_first_and_hide_inactive(ws):
    items = suggest_mentions("", scan_workspace(ws).entries, AGENTS)
    kinds = [i.kind for i in items]
    assert kinds[:2] == ["agent", "agent"]
    assert "Quality Critic" not in [i.label for i in items]
    assert {"file", "dir"} <= set(kinds)


def test_suggestions_match_by_name_first(ws):
    items = suggest_mentions("spec", scan_workspace(ws).entries, AGENTS)
    assert items[0].label == "docs/spec.md"
    assert items[0].insert == "@docs/spec.md"


def test_paths_with_spaces_are_quoted():
    assert mention_token("docs/요구사항 정의서.pdf") == '@"docs/요구사항 정의서.pdf"'
    assert mention_token("System Architect") == '@"System Architect"'
    assert mention_token("src/") == "@src/"


# ------------------------------------------------------------------ 경로 안전


@pytest.mark.parametrize("bad", ["../secret.txt", "docs/../../x", "/etc/passwd", "C:/Windows/win.ini", "C:\\x", ""])
def test_paths_outside_the_workspace_are_refused(ws, bad):
    with pytest.raises(WorkspacePathError):
        safe_workspace_path(ws, bad)


@pytest.mark.skipif(os.name == "nt", reason="Windows 에서는 심볼릭 링크 권한이 필요합니다")
def test_a_symlink_pointing_outside_is_refused(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (ws / "link").symlink_to(outside)
    with pytest.raises(WorkspacePathError):
        safe_workspace_path(ws, "link")


# ------------------------------------------------------------------ 언급 해석


def test_mentions_become_a_path_only_reference_block(ws):
    text = '@docs/spec.md 과 @"docs/요구사항 정의서.pdf", @src/ 를 보고 @"System Architect" 가 답해 줘'
    out, report = expand_mentions(text, ws, AGENTS)

    assert out.startswith(text), "사람이 쓴 글은 그대로 둡니다"
    block = out[len(text):]
    assert REFERENCE_MARKER in block
    assert str(ws.resolve()) in block, "에이전트가 도구에 넘길 수 있게 작업 공간 위치를 적습니다"
    assert "- docs/spec.md (6 B)" in block
    assert "- docs/요구사항 정의서.pdf" in block
    assert "- src/ (폴더)" in block
    assert "- System Architect (Architecture)" in block
    assert "# spec" not in out and "x = 1" not in out, "내용은 붙이지 않습니다"
    assert not report.warnings()


def test_trailing_punctuation_and_duplicates(ws):
    _, report = expand_mentions("@src/cache.py, 그리고 다시 @src/cache.py.", ws, AGENTS)
    assert report.files == [("src/cache.py", 5)]


def test_agents_match_by_key_or_name_and_inactive_ones_are_reported(ws):
    _, report = expand_mentions("@coder 와 @\"Quality Critic\"", ws, AGENTS)
    assert [a.key for a in report.agents] == ["coder"]
    assert [a.key for a in report.inactive_agents] == ["critic"]
    assert any("참여하지 않는" in w for w in report.warnings())


def test_code_blocks_emails_and_decorators_are_not_mentions(ws):
    text = (
        "메일은 me@example.com 으로.\n"
        "```python\n@app.get('/x')\ndef f(): ...\n```\n"
        "인라인 `@docs/spec.md` 도 코드입니다."
    )
    out, report = expand_mentions(text, ws, AGENTS)
    assert out == text
    assert not report.has_references and not report.warnings()


def test_missing_and_outside_paths_are_dropped_with_a_warning(ws):
    out, report = expand_mentions("@docs/nope.md 와 @../secret.txt 를 봐", ws, AGENTS)
    assert REFERENCE_MARKER not in out
    assert report.missing == ["docs/nope.md"]
    assert report.rejected == ["../secret.txt"]
    assert len(report.warnings()) == 2


def test_plain_words_after_at_are_left_alone(ws):
    _, report = expand_mentions("@everyone 확인 부탁", ws, AGENTS)
    assert not report.has_references and not report.warnings()


def test_large_files_ask_for_partial_reads(ws):
    (ws / "big.csv").write_bytes(b"x" * LARGE_FILE_BYTES)
    out, _ = expand_mentions("@big.csv", ws, AGENTS)
    assert "필요한 부분만 읽으세요" in out


def test_expanding_twice_does_not_duplicate_the_block(ws):
    """긴급 종료로 되돌아온 글을 그대로 다시 보내는 경우."""
    once, _ = expand_mentions("@docs/spec.md 봐", ws, AGENTS)
    twice, _ = expand_mentions(once, ws, AGENTS)
    assert twice == once
    assert strip_reference_block(once) == "@docs/spec.md 봐"


def test_the_orchestrator_is_not_a_mention_target():
    class A:
        def __init__(self, key):
            self.key, self.name, self.role = key, key.title(), "r"

    agents = agents_for_mentions([A("orchestrator"), A("coder"), A("critic")], ["orchestrator", "coder"])
    assert [(a.key, a.active) for a in agents] == [("coder", True), ("critic", False)]


# ------------------------------------------------------------------ 업로드


def test_upload_lands_in_uploads_and_never_overwrites(ws):
    first = store_workspace_upload(ws, "report.pdf", b"one")
    second = store_workspace_upload(ws, "report.pdf", b"two")
    assert first == "uploads/report.pdf"
    assert second == "uploads/report (2).pdf"
    assert (ws / "uploads" / "report.pdf").read_bytes() == b"one"


def test_upload_names_cannot_escape_or_be_reserved(ws):
    assert sanitize_upload_name("../../evil.txt") == "evil.txt"
    assert sanitize_upload_name("C:\\Users\\me\\a.txt") == "a.txt"
    assert sanitize_upload_name("con.txt") == "_con.txt"
    assert sanitize_upload_name("..") == "upload"
    assert store_workspace_upload(ws, "../../evil.txt", b"x") == "uploads/evil.txt"
    assert not (ws.parent / "evil.txt").exists()


def test_uploaded_file_shows_up_in_mentions_immediately(ws):
    index = get_workspace_index()
    index.get(ws)                                   # 목록을 기억해 둔 상태에서
    rel = store_workspace_upload(ws, "새 문서.docx", b"PK")
    labels = [i.label for i in suggest_mentions("새 문서", index.get(ws).entries, [])]
    assert rel in labels, "업로드는 기억한 목록을 비워야 합니다"


def test_upload_size_is_capped(ws):
    with pytest.raises(WorkspacePathError):
        store_workspace_upload(ws, "huge.bin", b"\0" * (MAX_UPLOAD_BYTES + 1))


# ------------------------------------------------------------------ 화면 연결


def test_the_popup_intercepts_enter_before_the_send_handler():
    """입력창의 Enter 는 보내기에 묶여 있습니다. 창이 열려 있으면 고르기여야 합니다."""
    assert "document.addEventListener('keydown'" in MENTION_JS
    keydown = MENTION_JS[MENTION_JS.index("document.addEventListener('keydown'"):]
    assert "}, true);" in keydown[:1500], "캡처 단계여야 입력 요소의 핸들러보다 먼저 옵니다"
    assert "stopImmediatePropagation" in keydown[:1500]
    assert "isComposing" in keydown[:600], "한글 조합 중 Enter 는 건드리지 않습니다"


def test_stale_answers_are_dropped():
    assert "seq !== state.seq" in MENTION_JS


def test_the_main_screen_wires_mentions_and_upload():
    app_src = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
    feed_src = io.open(ROOT / "app" / "ui" / "components" / "chat_feed.py", encoding="utf-8").read()
    assert "MENTION_JS" in app_src
    assert "mention_provider=mention_provider" in app_src and "on_upload_file=on_upload_file" in app_src
    assert app_src.count("await with_references(") == 2, "새 턴과 개입 모두"
    assert "MENTION_INPUT_CLASS" in feed_src and "MENTION_QUERY_EVENT" in feed_src
    assert MENTION_QUERY_EVENT in MENTION_JS and MENTION_INPUT_CLASS in MENTION_JS
