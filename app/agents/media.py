"""멀티모달 입력: @언급한 파일과 도구가 돌려준 파일을 내용째 모델에게 전달합니다 (ADR-035).

에이전트 카드의 `모달` 체크박스(`allowed_modalities`)가 이 에이전트가 직접 받을 형식을 정합니다.
모두 비어 있으면 이 모듈은 아무 일도 하지 않고, 에이전트는 예전처럼 글과 파일 경로만 받습니다.

**메시지에는 파일을 가리키는 블록만 듭니다.** BASE64 는 요청을 보내기 직전에 만들고
(`materialize_attachments`) 요청이 끝나면 버립니다. 메시지 목록은 판마다 초안으로 DB 에 남고
(ADR-025), 토큰을 세고, 로그의 지문이 되는데, 거기 수십 MB 의 글자가 끼면 그 모두가 차례로 느려집니다.
이어 가는 발언은 같은 블록에서 파일을 다시 읽습니다.

**첨부는 턴 단위입니다.** 언급한 턴의 발언마다 목표 메시지에 실립니다 — 자르기가 남기는 자리이고,
매번 같은 자리라 프롬프트 캐시도 깨지 않습니다 (`with_attachments`). 다음 턴에는 실리지 않습니다.
글에 남은 `[@참조]` 경로는 그대로이니, 다시 보여 주려면 다시 언급합니다.
"""

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import INPUT_MODALITIES
from app.mcp.client import MAX_TOOL_OUTPUT_CHARS, clip_tool_output
from app.workspace_files import WorkspacePathError, format_size, safe_workspace_path

logger = logging.getLogger(__name__)


IMAGE, AUDIO, VIDEO, DOCUMENT = INPUT_MODALITIES

# 카드와 창에 쓰는 이름.
MODALITY_LABELS: Dict[str, str] = {IMAGE: "이미지", AUDIO: "음성", VIDEO: "동영상", DOCUMENT: "문서"}

_MIME_BY_EXTENSION: Dict[str, Dict[str, str]] = {
    IMAGE: {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    },
    AUDIO: {
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
        ".flac": "audio/flac", ".aac": "audio/aac",
    },
    VIDEO: {
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".mpeg": "video/mpeg", ".mpg": "video/mpeg", ".avi": "video/x-msvideo",
    },
    DOCUMENT: {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".hwp": "application/x-hwp",
        ".hwpx": "application/hwp+zip",
        ".rtf": "application/rtf",
        # 글자 문서 (`TEXT_DOCUMENT_EXTENSIONS`).
        ".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
        ".tsv": "text/tab-separated-values", ".html": "text/html", ".htm": "text/html",
        ".json": "application/json", ".xml": "application/xml",
    },
}

_MIME_ALIASES = {"image/jpg": "image/jpeg", "audio/mp3": "audio/mpeg", "audio/x-wav": "audio/wav"}

# 글자 문서는 BASE64 가 아니라 글자 그대로 싣습니다. 글자를 BASE64 문서로 받지 않는 Provider 가 많은데
# (Anthropic 은 PDF 만 받습니다), 글은 어느 모델이나 읽으니 인코딩을 거칠 이유가 없습니다.
TEXT_DOCUMENT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".html", ".htm", ".json", ".xml",
})

# 파일 하나와 한 발언 전체의 상한. Provider 들의 요청 크기 한도(수십 MB)에 맞춘 값입니다. 넘으면 보내지
# 않고 경로로만 전달한다고 모델에게 적습니다 — 보내서 거절당하면 왜 안 되는지 알 수 없습니다.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_SPEECH_ATTACHMENT_BYTES = 50 * 1024 * 1024

# 첨부가 컨텍스트 예산에서 차지할 수 있는 몫. 넘치는 것은 경로로만 전달합니다.
ATTACHMENT_CONTEXT_SHARE = 0.5

# 토큰 어림. 실제 값은 모델이 정하고 미리 알 방법이 없어 위쪽으로 잡습니다. 이미지는 해상도와 무관하게
# 비슷하고(Provider 가 줄입니다), 나머지는 크기에 비례합니다.
_IMAGE_TOKENS = 1_600
_MIN_ATTACHMENT_TOKENS = 1_000
_BYTES_PER_TOKEN = {AUDIO: 500, VIDEO: 500, DOCUMENT: 100}
_BYTES_PER_TOKEN_TEXT = 3

# 도구가 돌려준 파일을 두는 곳 (작업 공간 기준). `.mado` 는 런타임 전용 폴더라 @언급 목록에 나오지 않습니다.
TOOL_MEDIA_DIR = Path(".mado") / "tool_media"
TOOL_MEDIA_TTL_SECONDS = 7 * 24 * 60 * 60

# 메시지 안의 첨부 블록 형식. 요청을 보낼 때 이 블록이 실제 내용으로 바뀝니다.
ATTACHMENT_BLOCK = "mado_attachment"

ATTACHMENT_HEADER = (
    "[첨부 파일] 유저가 이번 턴에 언급한 파일 중 당신이 직접 받을 수 있는 형식을 앱이 여기 실었습니다. "
    "내용을 이미 볼 수 있으니 같은 파일을 도구로 다시 읽지 마세요."
)

TOOL_MEDIA_HEADER = "[도구 결과 첨부] 바로 앞 도구 호출이 돌려준 파일입니다."

# Gemini·Bedrock 은 음성·동영상도 `file` 블록(`file_data` + `format`)으로 받습니다. 나머지(OpenAI, vLLM 등
# OpenAI 호환)는 음성은 `input_audio`, 동영상은 `video_url` 입니다.
_FILE_BLOCK_PROVIDERS = frozenset({"gemini", "vertex_ai", "vertex_ai_beta", "bedrock", "bedrock_converse"})

_AUDIO_FORMATS = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a",
    "audio/ogg": "ogg", "audio/flac": "flac", "audio/aac": "aac",
}


# ---------------------------------------------------------------------------
# 형식
# ---------------------------------------------------------------------------


def modality_of(name: str) -> Optional[Tuple[str, str]]:
    """파일 이름의 `(형식, MIME)`. 네 형식 어디에도 들지 않으면 None — 경로로만 전달합니다."""
    suffix = Path(name).suffix.lower()
    for kind, table in _MIME_BY_EXTENSION.items():
        if suffix in table:
            return kind, table[suffix]
    return None


def modality_of_mime(mime: str) -> Optional[str]:
    """도구가 알려 준 MIME 의 형식. 표에 없으면 None — SVG·BMP 처럼 모델이 받지 못하는 그림을 실으면
    함께 실린 첨부까지 요청째 거절당합니다."""
    value = (mime or "").split(";", 1)[0].strip().lower()
    value = _MIME_ALIASES.get(value, value)
    for kind, table in _MIME_BY_EXTENSION.items():
        if value in table.values():
            return kind
    return None


def extensions_of(kind: str) -> List[str]:
    """이 형식으로 보는 확장자 (점 없이). 체크박스 옆에 보여 줍니다."""
    return [suffix.lstrip(".") for suffix in _MIME_BY_EXTENSION.get(kind, {})]


def _extension_of_mime(mime: str) -> str:
    value = (mime or "").split(";", 1)[0].strip().lower()
    value = _MIME_ALIASES.get(value, value)
    for table in _MIME_BY_EXTENSION.values():
        for suffix, known in table.items():
            if known == value:
                return suffix
    return ".bin"


# ---------------------------------------------------------------------------
# 메시지의 첨부 블록
# ---------------------------------------------------------------------------


def is_attachment(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == ATTACHMENT_BLOCK


def has_attachments(messages: Iterable[Dict[str, Any]]) -> bool:
    return any(
        isinstance(m.get("content"), list) and any(is_attachment(b) for b in m["content"])
        for m in messages
    )


def attachment_block(path: Path, name: str, kind: str, mime: str, size: int, *, as_text: bool = False) -> Dict[str, Any]:
    """파일을 가리키는 블록. JSON 으로 그대로 저장되므로 끊긴 발언을 이어 갈 때도 쓸 수 있습니다."""
    block: Dict[str, Any] = {
        "type": ATTACHMENT_BLOCK, "path": str(path), "name": name, "kind": kind, "mime": mime, "size": int(size),
    }
    if as_text:
        block["as_text"] = True
    return block


def attachment_tokens(block: Dict[str, Any]) -> int:
    """이 첨부가 요청에서 차지할 토큰의 어림값."""
    kind, size = block.get("kind"), int(block.get("size") or 0)
    if block.get("as_text"):
        return max(_MIN_ATTACHMENT_TOKENS, size // _BYTES_PER_TOKEN_TEXT)
    if kind == IMAGE:
        return _IMAGE_TOKENS
    return max(_MIN_ATTACHMENT_TOKENS, size // _BYTES_PER_TOKEN.get(kind, 100))


def without_attachments(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """첨부 블록을 뺀 메시지와, 뺀 블록의 토큰 어림값. 첨부가 없으면 같은 목록을 돌려줍니다.

    토크나이저는 모르는 블록에서 예외를 올립니다. 첨부는 어차피 글자 수로 셀 수 없습니다.
    """
    if not has_attachments(messages):
        return messages, 0
    plain: List[Dict[str, Any]] = []
    tokens = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(is_attachment(b) for b in content):
            tokens += sum(attachment_tokens(b) for b in content if is_attachment(b))
            msg = {**msg, "content": [b for b in content if not is_attachment(b)] or ""}
        plain.append(msg)
    return plain, tokens


def _note_block(text: str) -> Dict[str, str]:
    return {"type": "text", "text": text}


def _caption(block: Dict[str, Any]) -> str:
    label = MODALITY_LABELS.get(block.get("kind"), "파일")
    return f"- {block.get('name')} ({label}, {format_size(int(block.get('size') or 0))})"


def _not_delivered(name: str, reason: str) -> Dict[str, str]:
    return _note_block(
        f"- {name}: {reason} 그래서 내용을 싣지 못했습니다. 내용을 본 것처럼 말하지 말고, 필요하면 파일 도구로 "
        f"직접 읽거나 읽지 못했다고 밝히세요."
    )


# ---------------------------------------------------------------------------
# 발언에 싣기
# ---------------------------------------------------------------------------


def attachments_for(agent: Any, rel_paths: Sequence[str], workspace: Optional[str]) -> List[Dict[str, Any]]:
    """이번 턴에 언급된 파일 중 이 에이전트가 받을 것. 모달이 비어 있으면 빈 목록입니다.

    경로는 화면이 보낸 값이고 끊긴 턴이면 DB 에서 읽은 값이라, 작업 공간 안인지 다시 확인합니다. 그사이
    지워진 파일은 조용히 넘어가지 않고 안내로 남깁니다.
    """
    allowed = set(getattr(agent, "allowed_modalities", None) or ())
    if not allowed or not rel_paths or not workspace:
        return []
    out: List[Dict[str, Any]] = []
    for rel in dict.fromkeys(rel_paths):
        found = modality_of(rel)
        if found is None or found[0] not in allowed:
            continue
        kind, mime = found
        try:
            target = safe_workspace_path(Path(workspace), rel)
            size = target.stat().st_size
        except (WorkspacePathError, OSError) as exc:
            logger.warning(f"Attachment '{rel}' is unavailable for {agent.name}: {exc}")
            out.append(_not_delivered(rel, "이 파일을 읽을 수 없습니다."))
            continue
        out.append(attachment_block(
            target, rel, kind, mime, size, as_text=Path(rel).suffix.lower() in TEXT_DOCUMENT_EXTENSIONS,
        ))
    return out


def fit_attachments(blocks: List[Dict[str, Any]], token_budget: int) -> List[Dict[str, Any]]:
    """파일·발언 크기 상한과 컨텍스트 몫에 맞춥니다. 넘치는 것은 안내로 바꿉니다 (앞에 언급한 것부터 남습니다)."""
    token_cap = int(token_budget * ATTACHMENT_CONTEXT_SHARE) if token_budget > 0 else 0
    out: List[Dict[str, Any]] = []
    used_bytes = used_tokens = 0
    for block in blocks:
        if not is_attachment(block):
            out.append(block)
            continue
        name, size, tokens = block["name"], int(block["size"]), attachment_tokens(block)
        if size > MAX_ATTACHMENT_BYTES:
            out.append(_not_delivered(
                name, f"파일이 {format_size(size)} 로 직접 전달 한도 {format_size(MAX_ATTACHMENT_BYTES)} 를 넘습니다.",
            ))
        elif used_bytes + size > MAX_SPEECH_ATTACHMENT_BYTES:
            out.append(_not_delivered(
                name, f"이 발언의 첨부 합계가 {format_size(MAX_SPEECH_ATTACHMENT_BYTES)} 를 넘습니다.",
            ))
        elif token_cap and used_tokens + tokens > token_cap:
            out.append(_not_delivered(name, "컨텍스트 창에 다 들어가지 않습니다."))
        else:
            used_bytes += size
            used_tokens += tokens
            out.append(block)
    return out


def with_attachments(prompt: List[Dict[str, Any]], blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """첫 사용자 메시지(목표)에 첨부를 붙입니다. 첨부가 없으면 같은 목록을 돌려줍니다.

    두 자르기(`fit_context_window`, `fit_tool_loop_context`) 모두 이 자리를 남기므로, 긴 토론에서도 끝까지
    보입니다. 라운드마다 같은 자리이니 프롬프트 캐시도 그대로 탑니다.
    """
    if not blocks:
        return prompt
    body: List[Dict[str, Any]] = [_note_block(ATTACHMENT_HEADER)]
    for block in blocks:
        if is_attachment(block):
            body.append(_note_block(_caption(block)))
        body.append(block)
    out = [dict(m) for m in prompt]
    for index, msg in enumerate(out):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            out[index] = {**msg, "content": [_note_block(msg["content"]), *body]}
            return out
    out.append({"role": "user", "content": body})
    return out


def replace_attachments(messages: List[Dict[str, Any]], reason: str) -> List[str]:
    """모든 첨부를 "받지 못했다" 는 안내로 바꿉니다. 바꾼 파일 이름 목록.

    모델이 첨부가 든 요청을 거절했을 때 씁니다 (`ToolLoopMixin._drop_rejected_attachments`).
    """
    names: List[str] = []
    for index, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list) or not any(is_attachment(b) for b in content):
            continue
        replaced = []
        for block in content:
            if is_attachment(block):
                names.append(block["name"])
                replaced.append(_not_delivered(block["name"], reason))
            else:
                replaced.append(block)
        messages[index] = {**msg, "content": replaced}
    return names


# ---------------------------------------------------------------------------
# 요청 직전: 파일 → 내용
# ---------------------------------------------------------------------------


def _uses_file_blocks(agent: Any) -> bool:
    provider = (getattr(agent, "provider", None) or str(agent.model).split("/", 1)[0]).lower()
    return provider in _FILE_BLOCK_PROVIDERS


def _decoded(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _wire_block(agent: Any, block: Dict[str, Any], data: bytes) -> Dict[str, Any]:
    """파일 내용을 Provider 가 받는 블록으로. LiteLLM 이 OpenAI 형식을 각 Provider 형식으로 옮깁니다."""
    name, kind, mime = block["name"], block["kind"], block["mime"]
    if block.get("as_text"):
        text = clip_tool_output(_decoded(data), MAX_TOOL_OUTPUT_CHARS)
        return _note_block(f"<첨부 문서 이름=\"{name}\">\n{text}\n</첨부 문서>")
    encoded = base64.b64encode(data).decode("ascii")
    url = f"data:{mime};base64,{encoded}"
    if kind == IMAGE:
        return {"type": "image_url", "image_url": {"url": url}}
    if _uses_file_blocks(agent):
        return {"type": "file", "file": {"file_data": url, "format": mime}}
    if kind == AUDIO:
        return {"type": "input_audio", "input_audio": {"data": encoded, "format": _AUDIO_FORMATS.get(mime, "wav")}}
    if kind == VIDEO:
        return {"type": "video_url", "video_url": {"url": url}}
    return {"type": "file", "file": {"filename": Path(name).name, "file_data": url}}


def _read_block(agent: Any, block: Dict[str, Any]) -> Dict[str, Any]:
    name = block["name"]
    try:
        path = Path(block["path"])
        if path.stat().st_size > MAX_ATTACHMENT_BYTES:
            return _not_delivered(name, f"파일이 직접 전달 한도 {format_size(MAX_ATTACHMENT_BYTES)} 를 넘습니다.")
        return _wire_block(agent, block, path.read_bytes())
    except OSError as exc:
        logger.warning(f"Could not read attachment '{name}' for {agent.name}: {exc}")
        return _not_delivered(name, "이 파일을 읽을 수 없습니다.")


def _materialize(agent: Any, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(is_attachment(b) for b in content):
            msg = {**msg, "content": [_read_block(agent, b) if is_attachment(b) else b for b in content]}
        out.append(msg)
    return out


async def materialize_attachments(agent: Any, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """보낼 메시지. 첨부 블록을 파일 내용으로 바꿉니다. 첨부가 없으면 같은 목록을 돌려줍니다.

    파일을 읽고 인코딩하는 동안 다른 발언과 화면이 멈추지 않게 이벤트 루프 밖에서 합니다.
    """
    if not has_attachments(messages):
        return messages
    return await asyncio.to_thread(_materialize, agent, messages)


# ---------------------------------------------------------------------------
# 도구가 돌려준 파일
# ---------------------------------------------------------------------------


def _purge_old_tool_media(directory: Path) -> None:
    now = time.time()
    for item in directory.iterdir():
        try:
            if item.is_file() and now - item.stat().st_mtime > TOOL_MEDIA_TTL_SECONDS:
                item.unlink()
        except OSError:
            continue


def store_tool_media(workspace: Any, tool_name: str, kind: str, mime: str, data: str) -> Dict[str, Any]:
    """도구가 BASE64 로 돌려준 파일을 작업 공간의 `.mado/tool_media/` 에 풀고 첨부 블록을 돌려줍니다.

    메시지에는 파일을 가리키는 블록만 두는 것은 @언급과 같습니다. 파일이 있어야 끊긴 발언을 이어 갈 때도
    다시 실을 수 있습니다. 며칠 지난 것은 저장할 때 함께 지웁니다.
    """
    directory = Path(workspace) / TOOL_MEDIA_DIR
    directory.mkdir(parents=True, exist_ok=True)
    _purge_old_tool_media(directory)
    content = base64.b64decode(data, validate=False)
    target = directory / f"{uuid.uuid4().hex[:16]}{_extension_of_mime(mime)}"
    target.write_bytes(content)
    return attachment_block(target, f"{tool_name} 결과", kind, mime, len(content))


# ---------------------------------------------------------------------------
# 보낼 때 화면 안내
# ---------------------------------------------------------------------------


def attachment_notices(files: Sequence[Tuple[str, int]], agents: Sequence[Any]) -> Tuple[List[str], List[str]]:
    """이번에 언급한 파일이 누구에게 직접 가는지. `(안내, 경고)`.

    글에는 경로만 남아 무엇이 실제로 실렸는지 화면에서 알 수 없으므로, 보내는 순간 한 번 알립니다.
    """
    delivered: Dict[str, List[str]] = {}
    warnings: List[str] = []
    for rel, size in files:
        found = modality_of(rel)
        if found is None:
            continue
        kind = found[0]
        receivers = [a.name for a in agents if kind in (getattr(a, "allowed_modalities", None) or ())]
        if not receivers:
            continue
        if size > MAX_ATTACHMENT_BYTES:
            warnings.append(
                f"{rel}: {format_size(size)} 로 직접 전달 한도 {format_size(MAX_ATTACHMENT_BYTES)} 를 넘어 경로로만 전달합니다."
            )
            continue
        delivered[rel] = receivers
    notices = [f"{rel} → {', '.join(names)} 에게 내용을 직접 전달합니다." for rel, names in delivered.items()]
    return notices, warnings
