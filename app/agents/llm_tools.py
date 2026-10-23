"""발언이 가진 도구를 이름 꼬리로 찾기와, 파일 쓰기 도구를 가진 에이전트에게 붙는 상시 지침.

도구는 서버 키가 아니라 이름 꼬리(`write_file`, `append_workspace_file` …)로 찾습니다. 지침은 해당
도구가 있을 때만 붙어, 파일을 만지지 않는 에이전트의 프롬프트는 늘지 않습니다.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# 메모리 MCP 의 도구 이름. 서버 키(`memory`)가 아니라 **도구 이름 꼬리**로 찾습니다.
# 서버를 껐거나, 연결에 실패했거나, conf.json 에서 키 이름을 바꿨을 때 없는 도구를
# 부르라고 시키지 않기 위해서입니다 (`memory__add_observations` 의 `__` 뒤).
MEMORY_WRITE_TOOLS = ("add_observations", "create_entities")

MEMORY_SEARCH_TOOLS = ("search_nodes", "open_nodes")

# 파일 **뒤에 덧붙일 수 있는** 도구와, **통째로 덮어쓰는** 도구.
#
# 둘을 가르는 이유는 잘린 도구 호출을 어떻게 만회하라고 말할지가 달라지기
# 때문입니다. 공식 filesystem 서버의 `write_file` 은 "completely overwrite" 라,
# 그걸로 이어쓰려 하면 앞부분을 매번 통째로 다시 써야 합니다 — 나눌수록 호출이
# 커지고 같은 자리에서 또 잘립니다. 나누기가 뜻을 가지려면 덧붙이는 도구가
# 있어야 하고, 없으면 아예 다른 조언을 해야 합니다.
#
# **순서가 우선순위입니다** (`append_tool`). 맨 앞의 `append_workspace_file`(샌드박스 v0.8.0)만
# 진짜 덧붙이기입니다. `edit_file` 은 찾아 바꾸기라, 덧붙이기로 쓰면 반복되는 줄(`);`,
# `</div>`)의 첫 일치 위치에 끼어들고 찾을 문자열이 비면 파일 맨 앞에 들어갑니다 — 실제로
# TSX 가 그렇게 깨졌습니다. 그래서 다른 길이 없을 때만 짚습니다.
APPEND_TOOLS = (
    "append_workspace_file", "append_file", "edit_file", "edit_text_file", "patch_file", "str_replace",
)

# 결과에 파일 끝 줄을 돌려주는 샌드박스의 덧붙이기 도구 (`file_writing_guidance` 가 한 줄을 더합니다).
TRUE_APPEND_TOOL = "append_workspace_file"

# `write_workspace_file` 은 샌드박스 서버의 쓰기 도구입니다. 빠져 있던 동안 `sandbox` 만
# 가진 에이전트는 분할 쓰기 지침(`file_writing_guidance`)을 한 줄도 받지 못했습니다.
FILE_WRITE_TOOLS = ("write_file", "write_text_file", "create_file", "write_workspace_file")

def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    """OpenAI 도구 스키마 목록에서 함수 이름만 뽑습니다."""
    names = []
    for tool in tools or []:
        name = (tool.get("function") or {}).get("name")
        if name:
            names.append(str(name))
    return names

def _find_tool(tools: Optional[List[Dict[str, Any]]], suffixes: Tuple[str, ...]) -> Optional[str]:
    """이름이 `suffixes` 중 하나로 끝나는 도구의 **전체 이름**을 돌려줍니다.

    전체 이름이어야 지시문에 모델이 실제로 부를 이름을 그대로 적어 줄 수 있습니다.
    """
    for name in _tool_names(tools):
        tail = name.split("__", 1)[-1]
        if tail in suffixes:
            return name
    return None

def memory_write_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """이 발언이 쓸 수 있는 메모리 기록 도구의 이름. 없으면 None."""
    return _find_tool(tools, MEMORY_WRITE_TOOLS)

def memory_search_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """이 발언이 쓸 수 있는 메모리 검색 도구의 이름. 없으면 None."""
    return _find_tool(tools, MEMORY_SEARCH_TOOLS)

def is_file_writing_call(name: str) -> bool:
    """이 호출이 파일에 내용을 쓰는 도구인가 (덮어쓰기든 덧붙이기든)."""
    tail = (name or "").split("__", 1)[-1]
    return tail in FILE_WRITE_TOOLS or tail in APPEND_TOOLS

def append_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """파일 뒤에 덧붙일 수 있는 도구의 이름. 없으면 None.

    `_find_tool` 과 달리 도구 목록의 순서가 아니라 `APPEND_TOOLS` 의 순서로 고릅니다 —
    진짜 덧붙이기 도구가 있으면 `edit_file` 보다 그것을 짚어야 합니다.
    """
    names = _tool_names(tools)
    for suffix in APPEND_TOOLS:
        for name in names:
            if name.split("__", 1)[-1] == suffix:
                return name
    return None

def file_write_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """파일을 통째로 쓰는(덮어쓰는) 도구의 이름. 없으면 None."""
    return _find_tool(tools, FILE_WRITE_TOOLS)

def file_tools(tools: Optional[List[Dict[str, Any]]]) -> Tuple[Optional[str], Optional[str]]:
    """나누어 쓰기 지침에 짚을 (덧붙이기 도구, 쓰기 도구).

    덧붙이기 도구와 **같은 서버**에 쓰기 도구가 있으면 그것을 짝으로 고릅니다. 서버마다 경로를
    읽는 방식이 달라(filesystem 은 허용 폴더 기준, 샌드박스는 작업 공간 기준 상대 경로) 첫 부분과
    이어지는 부분을 다른 서버로 쓰면 같은 파일에 들어간다는 보장이 없습니다.
    """
    append, write = append_tool(tools), file_write_tool(tools)
    if append and "__" in append:
        server = append.split("__", 1)[0]
        paired = next(
            (
                name for name in _tool_names(tools)
                if name.split("__", 1)[0] == server and name.split("__", 1)[-1] in FILE_WRITE_TOOLS
            ),
            None,
        )
        write = paired or write
    return append, write

# 파일 쓰기 도구를 가진 에이전트의 시스템 프롬프트에 상시로 붙는 두어 줄.
#
# `truncation_advice` 는 **잘린 뒤에** 하는 말이고 이쪽은 **잘리기 전에** 하는 말입니다.
# 사후 수습만으로는 부족했습니다 — 잘린 호출 한 번은 실패한 도구 실행, 낭비된 도구
# 예산, 되돌려 보낼 수 없는 인자, 그리고 다시 쓰는 한 판을 뜻합니다.
#
# 예전에는 "인자를 짧게 쓰라" 는 상시 지시를 일부러 넣지 않았습니다. 모델은 자기
# 출력이 몇 토큰인지 셀 수 없어서 지켜지지 않고, 지키려다 내용만 부실해지기
# 때문입니다. 그런데 여기 적는 것은 **크기가 아니라 전략**입니다 — 어느 도구를
# 고르고 어떤 단위로 나눌 것인가. 토큰을 셀 필요가 없으므로 모델이 실제로 따를 수
# 있고, 요즘 코딩 에이전트들이 전체 쓰기 대신 편집 도구를 주력으로 두는 것과 같은
# 방향입니다.
FILE_WRITING_HEAD = (
    "[파일 쓰기] 파일을 한 번의 도구 호출로 다 쓰려 하지 마세요. **도구 호출의 인자도 "
    "응답 한도(max_tokens)를 함께 씁니다.** 내용이 길면 인자가 중간에서 잘리고 그 호출은 "
    "실패합니다."
)

BINARY_DOC_HEAD = (
    "[바이너리 문서(.pptx / .xlsx / .docx / .pdf)]\n"
    ".pptx 와 .xlsx 는 XML 을 담은 ZIP 컨테이너입니다. 텍스트가 아니므로 파일 쓰기 "
    "도구로는 만들 수 없습니다 — 그렇게 쓴 파일은 PowerPoint / Excel 이 열지 못합니다."
)

def binary_file_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """파일 쓰기 도구를 가진 에이전트에게, 바이너리 문서는 만들 수 없다고 알립니다. 없으면 None.

    `file_writing_guidance` 와 같은 규율입니다 — 도구를 이름 꼬리로 찾고, 실제로 가진
    도구의 **전체 이름**을 짚어 주며, 쓰기 도구가 없으면 None 이라 프롬프트가 한 글자도
    늘지 않습니다. 이 지침이 없으면 모델은 `.pptx` 를 텍스트로 써 놓고 만들었다고
    보고합니다. 쓰기 자체는 `app/mcp/guards.py` 가 막습니다.
    """
    writer = file_write_tool(tools)
    if not writer:
        return None
    return (
        f"{BINARY_DOC_HEAD}\n"
        f"- 이 환경에는 그런 파일을 만들 수 있는 도구가 없습니다. `{writer}` 로 "
        f"마크다운(.md)이나 CSV 로 쓰고, 요청한 형식의 파일은 만들지 못했다고 "
        f"유저에게 그대로 알리세요. 만들었다고 말하지 마세요."
    )

def file_writing_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """파일 쓰기 도구를 **가진** 에이전트에게만 붙는 상시 지침. 없으면 None.

    도구를 이름 꼬리로 찾는 것은 `truncation_advice` 와 같은 규칙이고, 갈래도 같습니다 —
    덧붙일 수 있으면 나누어 덧붙이라고, 덮어쓰기뿐이면 파일을 쪼개라고 합니다.
    도구가 없으면 None 이라, 파일을 만지지 않는 에이전트(비평가 등)의 프롬프트는
    한 글자도 늘지 않습니다.

    덧붙이기 도구가 진짜 덧붙이기(`append_workspace_file`)면, 결과에 실리는 파일 끝 줄을 보고
    이어 쓰라는 한 줄을 더합니다. 조각 경계에서 같은 내용을 다시 보내거나 닫는 괄호를 빠뜨리는
    실수를 모델이 스스로 잡게 하려는 것입니다.
    """
    append, write = file_tools(tools)
    if not append and not write:
        return None

    if append and write:
        body = (
            f"- 먼저 `{write}` 로 첫 부분(문서라면 도입·첫 절, 코드라면 import 와 앞쪽 정의)만 "
            f"만들고, 이어지는 부분은 `{append}` 로 **뒤에 덧붙이세요.** 절이나 챕터 단위(코드라면 "
            f"함수·컴포넌트 단위)로 나누면 됩니다.\n"
            f"- 한 번의 호출에는 한 덩어리만 담으세요. `{write}` 로 파일 전체를 다시 쓰는 "
            f"방식으로 이어붙이면 호출이 점점 커져 결국 잘립니다."
        )
    elif append:
        body = (
            f"- 긴 내용은 `{append}` 로 절이나 챕터 단위로 **나누어 덧붙이세요.** "
            f"한 번의 호출에는 한 덩어리만 담습니다."
        )
    else:
        body = (
            f"- `{write}` 는 덮어쓰기라 이어붙일 수 없습니다. 내용이 길면 **파일을 여러 개로 "
            f"나누어**(예: `01-개요.md`, `02-설계.md`) 각각 한 번에 쓰세요."
        )
    if append and append.split("__", 1)[-1] == TRUE_APPEND_TOOL:
        body += (
            f"\n- `{append}` 의 결과에는 파일 끝 몇 줄이 줄 번호와 함께 나옵니다. 그것을 보고 다음 "
            f"조각을 이어 쓰고, 이미 쓴 부분은 다시 보내지 마세요."
        )
    return f"{FILE_WRITING_HEAD}\n{body}"
