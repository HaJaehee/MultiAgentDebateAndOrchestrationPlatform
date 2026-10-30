"""`file_editing_guidance` 가 모델에게 하는 말이 실제 filesystem 서버의 동작과 맞는지.

지침은 서버의 동작을 옮겨 적은 글입니다. 서버는 이 저장소의 코드가 아니고 버전 범위(`^2026.8.31`)도
열려 있어, 새 설치본에서 동작이 바뀌면 지침이 조용히 틀린 말이 됩니다. 여기가 깨지면 서버가 달라진
것이니, 시험을 맞추기 전에 지침(`app/agents/llm_tools.py`)과 [LLM Integration §2.5] 부터 다시 읽으세요.

MADO 가 서버를 붙이는 그대로(`MCPManager`, Roots 포함) 부릅니다.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import pytest

from app.config import MCPServerConfig
from app.mcp.manager import MCPManager
from tests.test_mcp import _node_bin, _node_mcp_home


def _entry() -> Optional[Path]:
    home = _node_mcp_home()
    if home is None:
        return None
    entry = home / "node_modules" / "@modelcontextprotocol" / "server-filesystem" / "dist" / "index.js"
    return entry if entry.is_file() else None


pytestmark = pytest.mark.skipif(
    _entry() is None or _node_bin() is None,
    reason="Node 런타임 또는 filesystem 서버가 없습니다 (python setup_mcp.py)",
)


@asynccontextmanager
async def _filesystem(workspace: Path):
    server = MCPServerConfig(command=_node_bin(), args=[str(_entry()), str(workspace)])
    manager = MCPManager({"filesystem": server}, workspace=workspace)
    await manager.initialize()
    try:
        assert manager.clients["filesystem"].is_connected, manager.clients["filesystem"].connect_error
        yield manager
    finally:
        await manager.shutdown()


async def _edit(manager: MCPManager, path: str, *edits: tuple) -> tuple:
    return await manager.execute_tool("filesystem__edit_file", {
        "path": path,
        "edits": [{"oldText": old, "newText": new} for old, new in edits],
    })


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


@pytest.mark.asyncio
async def test_the_arguments_the_rule_names_are_the_ones_the_model_sees(tmp_path):
    """지침이 적은 인자 이름이 도구 스키마와 같고, 줄 번호·줄 범위 인자는 없습니다."""
    async with _filesystem(tmp_path) as manager:
        tools = manager.get_openai_tools_for_servers(["filesystem"])
    by_name = {tool["function"]["name"]: tool["function"] for tool in tools}

    params = by_name["filesystem__edit_file"]["parameters"]["properties"]
    assert set(params) == {"path", "edits", "dryRun"}
    assert set(params["edits"]["items"]["properties"]) == {"oldText", "newText"}
    assert "filesystem__read_text_file" in by_name, "지침이 먼저 읽으라고 짚는 도구입니다"


@pytest.mark.asyncio
async def test_only_the_first_match_changes_and_nobody_asks_if_it_is_unique(tmp_path):
    """v1.1.2 에서 긴 TSX 를 깨뜨린 원인입니다. 아직 그대로라 지침이 앞뒤 줄을 함께 적으라고 합니다."""
    target = _write(tmp_path / "a.tsx", "<div>\n  a\n</div>\n<div>\n  b\n</div>\n")
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(manager, "a.tsx", ("</div>", "</div>\n<p>tail</p>"))
    assert status == "success", output
    assert _read(target) == "<div>\n  a\n</div>\n<p>tail</p>\n<div>\n  b\n</div>\n"


@pytest.mark.asyncio
async def test_an_empty_old_text_lands_at_the_top_of_the_file(tmp_path):
    target = _write(tmp_path / "a.md", "first\nsecond\n")
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(manager, "a.md", ("", "added\n"))
    assert status == "success", output
    assert _read(target) == "added\nfirst\nsecond\n"


@pytest.mark.asyncio
async def test_a_loose_match_finds_the_lines_but_can_break_the_indentation(tmp_path):
    """공백만 다른 `oldText` 도 찾아 주고 성공을 보고합니다 — 그래서 들여쓰기까지 그대로 옮기라고 합니다."""
    target = _write(tmp_path / "a.py", "def f():\n    if x:\n        return 1\n    return 2\n")
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(
            manager, "a.py", ("if x:\n    return 1", "if x:\n    if y:\n        return 1"))
    assert status == "success", output
    assert _read(target) == "def f():\n    if x:\n    if y:\n        return 1\n    return 2\n"
    assert "+    if y:" in output, "깨진 모양은 결과의 diff 에서 보입니다"


@pytest.mark.asyncio
async def test_a_failed_edit_writes_nothing_at_all(tmp_path):
    """앞 수정이 맞았어도 하나라도 못 찾으면 그 호출 전체가 없던 일입니다 (지침: 하나도 적용되지 않음)."""
    target = _write(tmp_path / "a.txt", "alpha\nbeta\n")
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(manager, "a.txt", ("alpha", "ALPHA"), ("no such line", "x"))
    assert status == "error"
    assert "Could not find exact match" in output
    assert _read(target) == "alpha\nbeta\n"


@pytest.mark.asyncio
async def test_edits_apply_in_the_order_written(tmp_path):
    target = _write(tmp_path / "a.txt", "alpha\n")
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(manager, "a.txt", ("alpha", "beta"), ("beta", "gamma"))
    assert status == "success", output
    assert _read(target) == "gamma\n"


@pytest.mark.asyncio
async def test_paths_are_relative_to_the_workspace_and_the_prefix_is_not_taken_off(tmp_path):
    """샌드박스와 달리 `./workspace/` 를 떼어 주지 않습니다. 샌드박스로 쓴 경로를 그대로 넘기면 없는 파일입니다."""
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    target = _write(workspace / "src" / "a.txt", "old\n")
    async with _filesystem(workspace) as manager:
        output, status = await _edit(manager, "./workspace/src/a.txt", ("old", "new"))
        assert status == "error" and "ENOENT" in output
        assert _read(target) == "old\n"

        output, status = await _edit(manager, "src/a.txt", ("old", "new"))
        assert status == "success", output
    assert _read(target) == "new\n"


@pytest.mark.asyncio
async def test_a_small_edit_of_a_big_file_answers_with_a_short_diff(tmp_path):
    """이 지침의 이유입니다. 큰 파일에서 한 줄을 고쳐도 모델이 주고받는 글은 몇 줄뿐입니다."""
    original = "".join(f"line {i}\n" for i in range(20000))
    target = _write(tmp_path / "big.txt", original)
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(manager, "big.txt", ("line 12345\n", "LINE 12345\n"))
    assert status == "success", output
    assert _read(target) == original.replace("line 12345\n", "LINE 12345\n")
    assert "-line 12345" in output and "+LINE 12345" in output
    assert len(output) < 2000 < len(original) // 100


@pytest.mark.asyncio
async def test_a_crlf_file_comes_back_all_lf_and_the_diff_does_not_show_it(tmp_path):
    """알려진 한계입니다 (지침으로 피할 수 없음). 사람이 윈도우에서 만든 파일을 고치면 줄끝이 전부 바뀝니다."""
    target = tmp_path / "a.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    async with _filesystem(tmp_path) as manager:
        output, status = await _edit(manager, "a.txt", ("two", "TWO"))
    assert status == "success", output
    assert target.read_bytes() == b"one\nTWO\nthree\n"
    assert "-one" not in output, "diff 는 바꾼 줄만 보여 줍니다"
