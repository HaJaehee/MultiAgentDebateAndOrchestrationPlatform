"""중단된 파일 쓰기 작업의 디스크 반영 여부를 SHA-256 해시로 검증합니다 (ADR-046).

파일 쓰기 도구 실행 도중 서버가 비정상 종료되면 해당 호출의 응답을 수신할 수 없습니다. MCP 도구 프로토콜에는
멱등성 키가 없어 서버에 직접 상태를 질의할 수도 없습니다. 모델에게 단순 [결과 모름]만 전달하면 덧붙이기(`append_workspace_file`)를
재호출하여 동일 내용이 중복 추가되거나, 이미 작성 완료된 파일을 불필요하게 다시 쓰느라 토큰이 낭비됩니다. 반면 로컬 파일은 호스트 애플리케이션이 직접 검증할 수 있습니다.

따라서 쓰기 도구를 실행하기 직전에 대상 파일의 기존 SHA-256과 호출 성공 시 예상되는 파일 내용의 SHA-256을
발언 초안(ADR-025)에 기록하고, 세션 재개 시 현재 디스크 파일과 대조합니다. 호출 완료 후의 해시도 기록하여 해당 발언이
마지막으로 쓰기를 완료한 이후 외부에서 파일 내용이 변경되었는지도 모델에게 알립니다.

검증 대상은 파라미터 규격을 인식할 수 있는 기본 서버의 4개 도구(`WRITE_MODES`)로 한정합니다. 원격 서버의 경로는 로컬 호스트의
파일이 아니며, 작업 공간 외부 경로는 보안 판정 대상이 아니므로 검증하지 않습니다. 이러한 호출은 기존과 동일하게
[결과 모름]으로 유지됩니다. 파일 읽기에 실패한 경우에도 마찬가지입니다.

본 모듈의 함수는 디스크 I/O를 수행하는 동기 함수이므로 이벤트 루프 외부(`asyncio.to_thread`)에서 실행해야 합니다.
"""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.mcp.policy import KNOWN_TOOLS, ToolMeta, is_loopback_host, resolve_path

REPLACE, APPEND, EDIT = "replace", "append", "edit"

# 호출 성공 시 파일 변경 모드 정의. `edit_file`은 결과를 사전에 예측할 수 없으므로 내용 변경 여부만 확인합니다.
WRITE_MODES = {
    "write_file": REPLACE,
    "write_workspace_file": REPLACE,
    "append_workspace_file": APPEND,
    "edit_file": EDIT,
}

# 이 크기를 초과하는 대용량 파일은 해시 검증을 생략합니다. 쓰기 호출 시 디스크 전체를 순회하느라 응답이 지연되는 현상을 방지합니다.
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
    """파일의 해시 객체를 생성합니다. 파일이 없으면 None. 일반 파일이 아니거나 용량 초과 시 OSError.

    경로는 모델이 전달한 파라미터 값입니다. 디바이스 파일이나 파이프를 열 경우 무한 대기에 빠질 수 있으므로 일반 파일만 조회합니다.
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
    """파일의 SHA-256 해시 문자열을 반환합니다. 파일이 없으면 None. 읽기 실패 시 OSError."""
    digest = _file_digest(absolute)
    return digest.hexdigest() if digest is not None else None


def _expected_hash(mode: str, before: Optional[Any], content: Any) -> Optional[str]:
    """도구 호출 성공 시 기대되는 파일의 SHA-256 해시를 산출합니다. 사전 예측이 불가능한 경우 None."""
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
    """해당 호출이 중단되었을 때 비교할 해시 검증 계획을 생성합니다. 검증 불가능한 호출이면 None."""
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
    """중단된 도구 호출의 결과로 전달할 안내 문구를 생성합니다. 파일 조회가 불가능하면 None([결과 모름] 유지)."""
    try:
        now = sha256_of(plan["absolute"])
    except OSError:
        return None
    path, before, expected = plan["path"], plan.get("before"), plan.get("expected")
    # 예상 결과를 우선 대조합니다. 파일이 이미 예상 내용과 일치한다면 실제 완료 여부와 무관하게 목적이 달성된 상태입니다.
    if expected is not None and now == expected:
        return APPLIED_RESULT.format(path=path)
    if now == before:
        absent = " (호출 전에도 지금도 없는 파일입니다)" if now is None else ""
        return NOT_APPLIED_RESULT.format(path=path, absent=absent)
    return (OTHER_RESULT if expected is not None else CHANGED_RESULT).format(path=path)


def reconcile(
    open_writes: Mapping[str, Mapping[str, Any]], known_files: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, str], List[str]]:
    """세션 재개 시점의 디스크 파일 상태를 초안 기록과 동기화합니다. (호출 ID -> 도구 결과 딕셔너리, 마지막 쓰기 이후 변경된 파일 목록).

    안내 완료된 파일은 현재 해시로 갱신하여, 다시 중단되더라도 동일 파일에 대해 중복 알림이 발생하지 않도록 방지합니다.
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
