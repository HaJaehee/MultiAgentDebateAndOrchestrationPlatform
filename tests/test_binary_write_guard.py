"""텍스트 도구로 바이너리 문서를 쓰려는 호출을 매니저가 막는지.

고치는 고장: 에이전트가 `filesystem__write_file` 로 `.pptx` 를 쓰면 도구는 성공을
보고하고, 모델은 파일을 만들었다고 말하고, 사용자는 PowerPoint 가 열지 못하는
파일을 받습니다. 실패가 조용하다는 것이 문제의 핵심이라, 호출이 서버에 닿기
**전에** 끊어 모델이 읽고 고칠 관측으로 바꿉니다.
"""

from typing import Any, List

import pytest

from app.agents.llm import binary_file_guidance
from app.mcp.guards import binary_write_refusal
from app.mcp.manager import MCPManager


class _RecordingClient:
    """부르면 기록만 남기는 서버 연결. 실제로 불렸는지를 봅니다."""

    server_name = "filesystem"
    tools: List[Any] = []
    is_connected = True

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def execute_tool(self, tool_name, arguments, scope=None):
        self.calls.append((tool_name, arguments))
        return "wrote it"


def _manager(with_slides: bool = True) -> tuple:
    manager = MCPManager({})
    client = _RecordingClient()
    for name in ("write_file", "read_file"):
        manager._tool_lookup[f"filesystem__{name}"] = (client, name)  # noqa: SLF001
        manager._tool_lookup[name] = (client, name)  # noqa: SLF001
    if with_slides:
        for name in ("slide_open", "slide_add", "slide_export", "sheet_write_table"):
            manager._tool_lookup[f"pair_slide__{name}"] = (client, name)  # noqa: SLF001
    manager.clients["filesystem"] = client  # type: ignore[assignment]
    return manager, client


@pytest.mark.asyncio
async def test_write_file_with_pptx_path_is_refused():
    """거부는 서버에 닿기 전에 일어나야 합니다 — 파일이 이미 생겼으면 소용없습니다."""
    manager, client = _manager()

    output, status = await manager.execute_tool(
        "filesystem__write_file", {"path": "workspace/slides.pptx", "content": "<xml/>"}
    )

    assert status == "error"
    assert client.calls == [], "서버까지 갔습니다 — 깨진 파일이 이미 만들어졌습니다"
    assert "pair_slide__slide_open" in output, "대안 도구를 지목하지 않으면 모델이 같은 짓을 반복합니다"
    assert "REFUSED" in output


@pytest.mark.asyncio
async def test_xlsx_refusal_points_at_the_sheet_tool():
    manager, _ = _manager()
    output, status = await manager.execute_tool(
        "sandbox__write_workspace_file", {"filename": "report.xlsx", "content": "a,b"}
    )
    assert status == "error"
    assert "pair_slide__sheet_write_table" in output
    assert "slide_open" not in output, "스프레드시트인데 발표자료 도구를 권하고 있습니다"


@pytest.mark.asyncio
async def test_refusal_without_slide_tools_tells_it_to_write_markdown():
    """pair_slide 를 꺼 둔 배포에서 모델이 '만들었다' 고 거짓 보고하는 것을 막습니다."""
    manager, _ = _manager(with_slides=False)

    output, status = await manager.execute_tool(
        "filesystem__write_file", {"path": "deck.pptx", "content": "x"}
    )

    assert status == "error"
    assert "마크다운" in output
    assert "만들었다고 말하지 마십시오" in output
    assert "pair_slide__" not in output, "없는 도구를 부르라고 시키고 있습니다"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["notes.md", "data.csv", "main.py", "conf.json", "a.txt"])
async def test_text_files_pass_through(path):
    """오탐이 나면 정상 작업이 막힙니다. 조건을 좁게 잡은 이유입니다."""
    manager, client = _manager()
    output, status = await manager.execute_tool(
        "filesystem__write_file", {"path": path, "content": "hello"}
    )
    assert status == "success", output
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_reading_a_pptx_is_allowed():
    """올라온 덱을 들여다보는 것은 정당한 작업입니다."""
    manager, client = _manager()
    _, status = await manager.execute_tool("filesystem__read_file", {"path": "given.pptx"})
    assert status == "success"
    assert client.calls


def test_guard_is_case_insensitive_about_the_extension():
    assert binary_write_refusal("filesystem__write_file", {"path": "A.PPTX", "content": "x"})


def test_guard_ignores_arguments_without_a_path():
    assert binary_write_refusal("filesystem__write_file", {"content": "x.pptx"}) is None


# --- 프롬프트에 붙는 지침 -----------------------------------------------------
# 막는 것만으로는 반쪽입니다. 못 하게 한 다음 **무엇을 하라**고 알려 주는 쪽도
# 같이 지켜야 하는데, 이 지침은 조용히 빠져도 아무 테스트도 깨지지 않았습니다.
def _tools(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def test_the_guidance_tells_the_agent_to_read_what_people_wrote():
    """코멘트는 사람 → 에이전트 통로입니다.

    읽으라고 시키지 않으면 편지함만 달아 놓고 수신자에게 알려 주지 않은 꼴이
    되어, 사람은 대답 없는 곳에 계속 적게 됩니다.
    """
    text = binary_file_guidance(_tools(
        "pair_slide__slide_open",
        "pair_slide__slide_comments",
        "pair_slide__slide_resolve_comment",
    ))

    assert "pair_slide__slide_comments" in text
    assert "pair_slide__slide_resolve_comment" in text, "닫는 법까지 알려 줘야 합니다"


def test_without_the_comment_tool_the_prompt_does_not_grow():
    """도구가 없으면 한 글자도 늘리지 않습니다 — 이 파일의 다른 지침들과 같은 규율."""
    text = binary_file_guidance(_tools("pair_slide__slide_open"))

    assert "slide_open" in text
    assert "부탁" not in text


def test_with_no_deck_tool_at_all_it_says_it_cannot():
    """도구가 없을 때 '만들었다' 고 말하는 것이 원래의 버그였습니다."""
    text = binary_file_guidance(_tools("filesystem__write_file"))

    assert "만들지 못했다고" in text
