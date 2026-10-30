"""발언이 가진 도구를 이름 꼬리로 찾기와, 파일 도구를 가진 에이전트에게 붙는 상시 지침 (쓰기·고치기).

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
# TSX 가 그렇게 깨졌습니다. 그래서 다른 길이 없을 때만 짚습니다. 이것은 **덧붙이기**로 쓸 때의
# 이야기이고, 파일의 일부를 고칠 때는 `edit_file` 이 맞는 도구입니다 (`file_editing_guidance`).
APPEND_TOOLS = (
    "append_workspace_file", "append_file", "edit_file", "edit_text_file", "patch_file", "str_replace",
)

# 결과에 파일 끝 줄을 돌려주는 샌드박스의 덧붙이기 도구 (`file_writing_guidance` 가 한 줄을 더합니다).
TRUE_APPEND_TOOL = "append_workspace_file"

# `write_workspace_file` 은 샌드박스 서버의 쓰기 도구입니다. 빠져 있던 동안 `sandbox` 만
# 가진 에이전트는 분할 쓰기 지침(`file_writing_guidance`)을 한 줄도 받지 못했습니다.
FILE_WRITE_TOOLS = ("write_file", "write_text_file", "create_file", "write_workspace_file")

# 파일의 일부만 고치는 도구 (공식 filesystem 서버의 찾아 바꾸기). 지침에 인자 이름(`oldText`)까지
# 적으므로, 인자 모양을 모르는 비슷한 이름의 도구(`patch_file` 등)는 넣지 않습니다.
EDIT_TOOL = "edit_file"

# 고치기 전에 읽으라고 짚을 도구. 순서가 우선순위입니다 (공식 서버에서 `read_file` 은 옛 이름입니다).
FILE_READ_TOOLS = ("read_text_file", "read_file")

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

def edit_tool(tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """파일의 일부만 고칠 수 있는 도구의 이름. 없으면 None."""
    return _find_tool(tools, (EDIT_TOOL,))

def _on_same_server(
    tools: Optional[List[Dict[str, Any]]], anchor: str, suffixes: Tuple[str, ...],
) -> Optional[str]:
    """`anchor` 와 같은 서버의 도구를 `suffixes` 순서대로 찾습니다. 없으면 None."""
    server = anchor.split("__", 1)[0]
    names = [name for name in _tool_names(tools) if name.split("__", 1)[0] == server]
    for suffix in suffixes:
        for name in names:
            if name.split("__", 1)[-1] == suffix:
                return name
    return None

def file_tools(tools: Optional[List[Dict[str, Any]]]) -> Tuple[Optional[str], Optional[str]]:
    """나누어 쓰기 지침에 짚을 (덧붙이기 도구, 쓰기 도구).

    덧붙이기 도구와 **같은 서버**에 쓰기 도구가 있으면 그것을 짝으로 고릅니다. 서버마다 경로를
    읽는 방식이 달라(filesystem 은 허용 폴더 기준, 샌드박스는 작업 공간 기준 상대 경로) 첫 부분과
    이어지는 부분을 다른 서버로 쓰면 같은 파일에 들어간다는 보장이 없습니다.
    """
    append, write = append_tool(tools), file_write_tool(tools)
    if append and "__" in append:
        write = _on_same_server(tools, append, FILE_WRITE_TOOLS) or write
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
    if append and append.split("__", 1)[-1] == EDIT_TOOL:
        body += (
            f"\n- `{append}` 는 찾아 바꾸기입니다. 덧붙일 때는 파일의 마지막 여러 줄을 `oldText` 에, "
            f"그 줄들과 이어질 내용을 `newText` 에 넣으세요. 마지막 한 줄만 적으면 같은 줄이 앞에 또 "
            f"있을 때 그 자리에 끼어듭니다."
        )
    return f"{FILE_WRITING_HEAD}\n{body}"

def file_editing_guidance(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """`edit_file` 을 **가진** 에이전트에게만 붙는, 기존 파일을 고치는 상시 지침. 없으면 None.

    `file_writing_guidance` 가 새 파일과 긴 파일을 어떻게 나누어 쓸지를 말한다면, 이쪽은 이미 있는
    파일을 다시 쓰지 말라고 말합니다. 큰 파일의 몇 줄을 고치려고 전체를 다시 출력하면 그 시간을
    고스란히 기다리게 되고, 파일이 크면 응답 한도에 걸려 잘립니다.

    조심시키는 것들은 전부 서버(`applyFileEdits`)의 실제 동작이고, `tests/test_edit_file_contract.py`
    가 실제 서버로 지킵니다. 처음 일치한 한 곳만 바꾸고 유일한지 묻지 않으며, 빈 `oldText` 는 파일 맨
    앞에 들어갑니다 — v1.1.2 에서 긴 TSX 를 깨뜨린 원인이 그대로 남아 있습니다. 공백만 다른 `oldText`
    도 찾아 주는 대신 새 내용의 들여쓰기를 어림으로 맞춰서, 파이썬이라면 조용히 깨질 수 있습니다.
    줄 번호나 줄 범위를 받는 인자는 없으므로 고칠 자리는 문맥으로만 짚습니다.
    """
    edit = edit_tool(tools)
    if not edit:
        return None
    reader = _on_same_server(tools, edit, FILE_READ_TOOLS)
    read = f"먼저 `{reader}` 로 파일을 읽고" if reader else "먼저 파일의 실제 내용을 확인하고"
    return (
        f"[기존 파일 고치기] 이미 있는 파일의 일부만 바꿀 때는 파일을 다시 쓰지 말고 `{edit}` 로 "
        f"**바꿀 부분만** 보내세요. 바꾸지 않는 부분까지 다시 출력하면 그만큼 오래 걸리고 응답 한도에도 "
        f"걸립니다. 파일 대부분을 바꿀 때만 다시 쓰세요.\n"
        f"- {read}, `oldText` 에는 바꿀 줄을 들여쓰기까지 그대로 옮기세요 (줄 번호는 빼고). "
        f"들여쓰기가 다르면 찾기는 되어도 새 내용의 들여쓰기가 틀어질 수 있습니다.\n"
        f"- `oldText` 는 **처음 일치하는 한 곳만** 바뀝니다. `);` 나 `</div>` 처럼 되풀이되는 줄만 "
        f"적으면 다른 자리가 바뀌니, 앞뒤 줄을 함께 적어 파일에서 한 곳에만 맞게 하세요. `oldText` 를 "
        f"비우면 파일 맨 앞에 들어갑니다.\n"
        f"- 여러 곳을 고칠 때는 `edits` 한 번에 함께 담으세요. 적은 순서대로 적용됩니다.\n"
        f"- 결과로 나오는 diff 에서 의도한 줄이 바뀌었는지 확인하세요. 못 찾았다는 오류가 나면 그 호출의 "
        f"수정은 하나도 적용되지 않은 것이니, 파일을 다시 읽어 `oldText` 를 실제 내용에 맞게 고쳐 다시 "
        f"부르세요.\n"
        f"- `path` 는 작업 공간 기준 상대 경로로 적고 `./workspace/` 는 붙이지 마세요. 이 도구는 샌드박스 "
        f"도구와 달리 그 접두사를 떼어 주지 않아 없는 파일을 찾게 됩니다."
    )
