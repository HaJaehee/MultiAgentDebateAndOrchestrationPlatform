"""끊긴 파일 쓰기가 디스크에 반영됐는지 해시로 확인합니다 (ADR-046).

쓰기 도구가 도는 도중에 서버가 내려가면 그 호출의 응답은 오지 않습니다. MCP 도구에는 멱등 키가 없어
서버에 물어볼 수도 없습니다. 모델에게 "결과 모름" 이라고만 하면 덧붙이기를 다시 불러 같은 조각이 두 번
들어가거나, 이미 쓴 파일을 다시 쓰느라 토큰을 씁니다. 그런데 파일은 앱이 직접 읽을 수 있습니다.

그래서 쓰기 도구를 부르기 **직전**에 대상 파일의 SHA-256 과, 호출이 성공하면 되어야 할 내용의 SHA-256 을
초안(ADR-025)에 남기고, 이어 갈 때 지금 파일과 비교합니다. 호출이 끝나면 그 뒤의 해시도 남겨, 이 발언이
마지막으로 쓴 뒤에 파일이 달라졌는지도 알립니다.

다루는 도구는 인자 모양을 아는 기본 서버의 네 개뿐입니다 (`WRITE_MODES`). 원격 서버의 경로는 이 PC 의
파일이 아니고, 작업 공간 밖의 경로는 보안 판정을 받은 대상이 아니므로 읽지 않습니다. 그런 호출은 예전처럼
"결과 모름" 입니다. 파일을 읽지 못해도 마찬가지입니다.

본 모듈의 함수는 디스크를 읽는 동기 함수라 이벤트 루프 밖(`asyncio.to_thread`)에서 부릅니다.
"""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.mcp.policy import KNOWN_TOOLS, ToolMeta, is_loopback_host, resolve_path

REPLACE, APPEND, EDIT = "replace", "append", "edit"

# 호출이 성공하면 파일이 어떻게 되는지. `edit_file` 은 결과를 앱이 미리 알 수 없어, 바뀌었는지만 봅니다.
WRITE_MODES = {
    "write_file": REPLACE,
    "write_workspace_file": REPLACE,
    "append_workspace_file": APPEND,
    "edit_file": EDIT,
}

# 이보다 큰 파일은 해시하지 않습니다. 쓰기 호출마다 디스크를 통째로 읽게 되어 발언이 멈춘 듯 보입니다.
MAX_HASHED_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024

_CHECKED = "[디스크 확인] 서버가 다시 시작되어 이 호출의 응답은 받지 못했지만, 앱이 파일을 직접 확인했습니다. "
APPLIED_RESULT = _CHECKED + (
    "`{path}` 는 이 호출이 쓰려던 내용과 바이트까지 같습니다. 이미 반영되었으니 같은 호출을 되풀이하지 마세요."
)
NOT_APPLIED_RESULT = _CHECKED + (
    "`{path}` 는 호출 전과 같습니다{absent}. 이 호출의 변경은 디스크에 없으니, 필요하면 다시 호출하세요."
)
CHANGED_RESULT = _CHECKED + (
    "`{path}` 는 호출 전과 달라졌습니다. 이 호출이 고친 것으로 보이지만 어떤 내용이 되어야 하는지 앱은 알 수 "
    "없습니다. 파일을 읽어 확인한 뒤 다시 호출할지 정하세요."
)
OTHER_RESULT = _CHECKED + (
    "`{path}` 는 호출 전 내용도, 이 호출이 쓰려던 내용도 아닙니다. 일부만 쓰였거나 그 사이 다른 쪽이 고쳤을 수 "
    "있습니다. 파일을 읽어 확인한 뒤 어떻게 할지 정하세요."
)


def _file_digest(absolute: str) -> Optional[Any]:
    """파일의 해시 객체. 없는 파일이면 None. 일반 파일이 아니거나 너무 크면 OSError.

    경로는 모델이 정한 값입니다. 장치 파일이나 파이프를 열면 끝나지 않으므로 일반 파일만 읽습니다.
    """
    try:
        info = os.stat(absolute)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {absolute}")
    if info.st_size > MAX_HASHED_BYTES:
        raise OSError(f"Too large to hash ({info.st_size:,} bytes): {absolute}")
    digest = hashlib.sha256()
    with open(absolute, "rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest


def sha256_of(absolute: str) -> Optional[str]:
    """파일의 SHA-256. 없는 파일이면 None. 읽지 못하면 OSError."""
    digest = _file_digest(absolute)
    return digest.hexdigest() if digest is not None else None


def _expected_hash(mode: str, before: Optional[Any], content: Any) -> Optional[str]:
    """호출이 성공했을 때 파일의 SHA-256. 미리 알 수 없으면 None."""
    if mode == EDIT or not isinstance(content, str):
        return None
    try:
        data = content.encode("utf-8")
    except UnicodeError:
        return None
    whole = before.copy() if mode == APPEND and before is not None else hashlib.sha256()
    whole.update(data)
    return whole.hexdigest()


def plan_write(meta: ToolMeta, arguments: Mapping[str, Any], workspace: Path) -> Optional[Dict[str, Any]]:
    """이 호출이 끊겼을 때 비교할 값. 확인할 수 없는 호출이면 None."""
    mode = WRITE_MODES.get(meta.tool)
    if mode is None or not meta.trusted:
        return None
    if meta.remote_host and not is_loopback_host(meta.remote_host):
        return None
    raw = arguments.get(KNOWN_TOOLS[meta.tool].paths[0][0])
    if not isinstance(raw, str) or not raw.strip():
        return None
    shown, absolute, inside = resolve_path(raw, workspace)
    if not inside:
        return None
    try:
        before = _file_digest(absolute)
    except OSError:
        return None
    return {
        "path": shown,
        "absolute": absolute,
        "before": before.hexdigest() if before is not None else None,
        "expected": _expected_hash(mode, before, arguments.get("content")),
    }


def checked_result(plan: Mapping[str, Any]) -> Optional[str]:
    """끊긴 호출의 도구 결과로 넣을 글. 파일을 읽지 못하면 None ("결과 모름" 으로 남습니다)."""
    try:
        now = sha256_of(plan["absolute"])
    except OSError:
        return None
    path, before, expected = plan["path"], plan.get("before"), plan.get("expected")
    # 쓰려던 내용을 먼저 봅니다. 파일이 이미 그 내용이었으면 실행 여부와 상관없이 할 일이 끝난 것입니다.
    if expected is not None and now == expected:
        return APPLIED_RESULT.format(path=path)
    if now == before:
        absent = " (호출 전에도 지금도 없는 파일입니다)" if now is None else ""
        return NOT_APPLIED_RESULT.format(path=path, absent=absent)
    return (OTHER_RESULT if expected is not None else CHANGED_RESULT).format(path=path)


def reconcile(
    open_writes: Mapping[str, Mapping[str, Any]], known_files: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, str], List[str]]:
    """이어 가는 순간의 디스크를 초안과 맞춥니다. (호출 id → 도구 결과, 마지막으로 쓴 뒤 달라진 파일).

    알린 파일은 지금 해시로 바꿔 둡니다. 다시 끊겨도 같은 파일을 또 알리지 않습니다.
    """
    results: Dict[str, str] = {}
    for call_id, plan in open_writes.items():
        text = checked_result(plan)
        if text is not None:
            results[call_id] = text
        known_files.pop(plan["absolute"], None)
    drifted: List[str] = []
    for absolute, entry in known_files.items():
        try:
            now = sha256_of(absolute)
        except OSError:
            continue
        if now != entry.get("sha256"):
            drifted.append(entry["path"])
            entry["sha256"] = now
    return results, drifted
