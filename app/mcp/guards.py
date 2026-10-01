"""도구 호출이 서버로 나가기 전에 거르는 검사들.

여기 있는 것은 전부 순수 함수입니다 — I/O 도, 상태도, MCP 의존성도 없습니다.
`MCPManager.execute_tool` 이 실제 서버로 넘기기 직전에 부르고, 문자열을 돌려받으면
그 문자열이 곧 도구 실패 관측이 되어 모델에게 되먹여집니다.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Optional, Tuple

# 텍스트로는 만들 수 없는 문서 형식. OOXML(.pptx/.xlsx/.docx) 은 XML 을 담은 ZIP
# 컨테이너이고, PDF 와 구형 OLE 형식(.ppt/.xls/.doc) 도 마찬가지로 바이너리입니다.
BINARY_DOC_EXTENSIONS = frozenset({
    ".pptx", ".potx", ".ppt", ".ppsx",
    ".xlsx", ".xlsm", ".xltx", ".xls",
    ".docx", ".dotx", ".doc",
    ".pdf", ".hwp", ".hwpx",
})

# 텍스트를 파일에 쓰는 도구들의 이름 꼬리(`server__tool` 의 `__` 뒤).
# `llm.FILE_WRITE_TOOLS` / `llm.APPEND_TOOLS` 와 같은 집합입니다 (샌드박스의
# `write_workspace_file` · `append_workspace_file` 포함).
TEXT_WRITE_TOOL_TAILS = frozenset({
    "write_file", "write_text_file", "create_file", "write_workspace_file",
    "append_workspace_file", "edit_file", "edit_text_file", "append_file", "patch_file", "str_replace",
})

# 인자 이름이 이 중 하나면 "파일 경로" 로 봅니다.
PATH_ARGUMENT_KEYS = (
    "path", "file_path", "filename", "file", "filepath",
    "target", "target_file", "uri", "dest", "destination", "output_path",
)

# 거부 문구에서 대안으로 지목할 발표자료 · 스프레드시트 도구들 (conf 키 `pair_slide`).
_SLIDE_ENTRY_TOOLS = ("slide_open", "slide_add", "slide_export")
_SHEET_ENTRY_TOOLS = ("sheet_write_table",)

_PRESENTATION_EXTENSIONS = frozenset({".pptx", ".potx", ".ppt", ".ppsx"})
_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".xltx", ".xls"})


def _tool_tail(tool_name: str) -> str:
    """`filesystem__write_file` → `write_file`. 서버 키가 무엇이든 같게 봅니다."""
    return str(tool_name or "").split("__", 1)[-1]


def _binary_target(arguments: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """인자에서 바이너리 문서를 가리키는 경로를 찾습니다. (경로, 확장자) 또는 None."""
    if not isinstance(arguments, Mapping):
        return None
    for key in PATH_ARGUMENT_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        ext = os.path.splitext(value.strip())[1].lower()
        if ext in BINARY_DOC_EXTENSIONS:
            return value.strip(), ext
    return None


def _find_available(available_tools: Iterable[str], tails: Iterable[str]) -> list:
    """주어진 이름 꼬리를 가진 도구의 **전체 이름** 목록. 런타임에 실제로 있는 것만."""
    wanted = tuple(tails)
    found = []
    for name in available_tools or ():
        tail = _tool_tail(str(name))
        if tail in wanted and str(name) not in found:
            found.append(str(name))
    # `_tool_lookup` 은 정규화된 이름(`pair_slide__slide_open`)과 맨이름을 둘 다
    # 들고 있습니다. 모델에게는 서버가 붙은 쪽을 보여 줍니다 — 모호하지 않으니까요.
    qualified = [n for n in found if "__" in n]
    return qualified or found


def binary_write_refusal(
    tool_name: str,
    arguments: Any,
    available_tools: Iterable[str] = (),
) -> Optional[str]:
    """텍스트 쓰기 도구로 바이너리 문서를 만들려는 호출을 막습니다.

    막는 이유는 단순합니다. `.pptx` 는 XML 을 담은 ZIP 컨테이너인데 `write_file` 은
    UTF-8 텍스트를 씁니다. 텍스트를 `.pptx` 이름으로 저장하면 PowerPoint 가 열지
    못하는 파일이 **반드시** 나옵니다. 모델 크기의 문제가 아니라 형식의 문제라,
    다시 시도해도 결과는 같습니다.

    이 호출을 그냥 흘려보내면 실패가 **조용합니다** — 도구는 성공을 보고하고,
    모델은 파일을 만들었다고 말하고, 사용자는 열리지 않는 파일을 받습니다.
    여기서 끊어야 모델이 읽고 고칠 수 있는 관측이 됩니다.

    발동 조건은 둘뿐입니다: 도구 이름 꼬리가 텍스트 쓰기 도구이고, 경로 인자가
    바이너리 문서 확장자로 끝날 것. 내용 인자까지 보지 않는 것은 의도한 선택입니다 —
    빈 내용으로 만든 `.pptx` 도 똑같이 깨진 파일이고, 인자 이름이 서버마다 달라
    내용 검사는 놓침(false negative)만 만들기 때문입니다. 읽기 도구는 꼬리 목록에
    없으므로 업로드된 덱을 `read_file` 로 들여다보는 정당한 작업은 그대로 통과합니다.

    돌려주는 문자열은 그대로 모델이 읽는 지시문이 되므로, 무엇이 일어났고(아무것도
    쓰이지 않음) 다음에 무엇을 해야 하는지를 명시합니다.
    """
    if _tool_tail(tool_name) not in TEXT_WRITE_TOOL_TAILS:
        return None

    target = _binary_target(arguments if isinstance(arguments, Mapping) else {})
    if target is None:
        return None
    path, ext = target

    slide_tools = _find_available(available_tools, _SLIDE_ENTRY_TOOLS)
    sheet_tools = _find_available(available_tools, _SHEET_ENTRY_TOOLS)
    wants_deck = ext in _PRESENTATION_EXTENSIONS
    wants_sheet = ext in _SPREADSHEET_EXTENSIONS
    usable = slide_tools if wants_deck else (sheet_tools if wants_sheet else [])

    head = (
        f"REFUSED - 아무것도 쓰이지 않았습니다.\n"
        f"'{path}' 는 {ext} 파일입니다. .pptx / .xlsx / .docx / .pdf 는 텍스트가 아니라 "
        f"XML 을 담은 ZIP 컨테이너(또는 바이너리)입니다. `{tool_name}` 으로 쓰면 "
        f"PowerPoint / Excel 이 열지 못하는 파일이 됩니다. 같은 방법으로 다시 써도 "
        f"결과는 같습니다."
    )

    if usable:
        if wants_deck:
            plan = (
                f"대신 이렇게 하세요: {', '.join(f'`{n}`' for n in usable)}.\n"
                f"next_action: `{usable[0]}` 을 파일명과 제목으로 부른 뒤, 슬라이드를 "
                f"한 호출에 한 장씩 추가하고 마지막에 저장하세요. "
                f"이 파일에 대해 `{tool_name}` 을 다시 부르지 마십시오."
            )
        else:
            plan = (
                f"대신 이렇게 하세요: {', '.join(f'`{n}`' for n in usable)}.\n"
                f"next_action: `{usable[0]}` 에 열 이름과 행 데이터를 넘겨 표를 쓰세요. "
                f"이 파일에 대해 `{tool_name}` 을 다시 부르지 마십시오."
            )
        return f"{head}\n\n{plan}"

    # 대안 도구가 이 런타임에 없을 때. 여기서 정직한 실패를 지시하지 않으면,
    # 모델은 만들지 못한 파일을 만들었다고 보고합니다.
    alt = "마크다운(.md)" if wants_deck else "CSV(.csv)"
    return (
        f"{head}\n\n"
        f"이 환경에는 {ext} 파일을 만들 수 있는 도구가 없습니다.\n"
        f"next_action: 내용을 {alt} 로 쓰고, 요청한 형식의 파일은 만들지 못했다고 "
        f"유저에게 그대로 알리세요. 만들었다고 말하지 마십시오."
    )
