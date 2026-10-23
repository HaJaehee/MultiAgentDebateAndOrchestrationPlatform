"""경로: 프로젝트 루트, conf.json 기본 위치, 데이터 폴더, 작업 공간(절대 경로로 고정), 에이전트 아이콘 저장소.
"""

import hashlib
import re
from pathlib import Path
from typing import Optional

# Load .env file if present


# 설정 파일. JSON 이므로 표준 라이브러리 `json` 만으로 읽고 쓸 수 있습니다 —
# 화면에서 고친 값을 되쓸 때 문자열을 손으로 조립하지 않습니다.
CONFIG_FILENAME = "conf.json"

EXAMPLE_CONFIG_FILENAME = "conf.example.json"

DEFAULT_CONFIG_PATH = CONFIG_FILENAME

# 프로젝트 루트 = app/ 의 부모. cwd 가 아니라 이 값을 기준으로 삼습니다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 윈도우의 확장 길이 경로 접두사. `Path.resolve()` 가 이걸 붙인 채로 돌려주는
# 경우가 있습니다 (특히 아직 만들어지지 않은 폴더). 그대로 두면 `as_uri()` 가
# `file://?/C:/...` 라는 UNC 모양을 만들고, MCP Roots 응답이 URL 검증에 걸려
# "Failed to request initial roots" 로 조용히 실패합니다 — filesystem 서버는
# 허용 디렉터리를 못 받은 채 뜹니다.
_EXTENDED_PREFIX = '\\\\?\\'

_EXTENDED_UNC_PREFIX = '\\\\?\\UNC\\'

def strip_extended_path_prefix(path: Path) -> Path:
    """윈도우의 확장 길이 경로 접두사를 걷어냅니다 (다른 OS 에서는 그대로)."""
    text = str(path)
    if text.startswith(_EXTENDED_UNC_PREFIX):
        return Path('\\\\' + text[len(_EXTENDED_UNC_PREFIX):])
    if text.startswith(_EXTENDED_PREFIX):
        return Path(text[len(_EXTENDED_PREFIX):])
    return path

def resolve_workspace_dir(value: Optional[str] = None) -> Path:
    """작업 공간 경로를 절대 경로로 정규화합니다. 비어 있으면 `<루트>/workspace`."""
    raw = (value or "").strip()
    if not raw:
        return PROJECT_ROOT / "workspace"
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return strip_extended_path_prefix(resolved)

# ---------------------------------------------------------------------------
# data/ — 화면에서 올린 파일이 놓이는 곳
# ---------------------------------------------------------------------------
#
# 작업 공간과 달리 이 폴더는 앱이 소유합니다. 에이전트가 만든 산출물이 아니라,
# 사람이 설정으로 올린 것(지금은 에이전트 아이콘)만 들어갑니다. cwd 가 아니라
# run_mado 가 있는 프로젝트 루트를 기준으로 삼아, 어디서 띄우든 같은 곳입니다.
DATA_DIR = PROJECT_ROOT / "data"

AGENT_ICON_DIR = DATA_DIR / "agent_icons"

# 올릴 수 있는 아이콘. 브라우저가 아바타에 그릴 수 있는 것만 받습니다.
ICON_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}

MAX_ICON_BYTES = 2 * 1024 * 1024

# 파일 이름은 우리가 짓습니다 (`<키>-<내용해시>.<확장자>`). 올린 쪽 이름을 그대로
# 쓰면 경로 조작과 이름 충돌을 각각 따로 막아야 하는데, 지어 주면 둘 다 없습니다.
SAFE_ICON_NAME = re.compile(r"[A-Za-z0-9._-]+")

def agent_icon_dir(create: bool = True) -> Path:
    """올린 에이전트 아이콘이 놓이는 폴더. 없으면 만듭니다."""
    if create:
        AGENT_ICON_DIR.mkdir(parents=True, exist_ok=True)
    return AGENT_ICON_DIR

def store_agent_icon(agent_key: str, filename: str, content: bytes) -> str:
    """올린 이미지를 `data/agent_icons/` 에 복사하고 conf.json 에 적을 경로를 돌려줍니다.

    돌려주는 값은 **프로젝트 루트 기준 상대 경로**입니다. 폴더를 통째로 옮기거나
    다른 PC 에 풀어도 설정이 그대로 맞습니다.

    이름은 내용 해시로 짓습니다. 같은 그림을 두 번 올리면 같은 파일이 되고, 다른
    그림이 같은 이름으로 덮어써지는 일도 없습니다.
    """
    if not content:
        raise ValueError("빈 파일입니다.")
    if len(content) > MAX_ICON_BYTES:
        raise ValueError(
            f"아이콘은 {MAX_ICON_BYTES // (1024 * 1024)}MB 까지만 올릴 수 있습니다 "
            f"(올린 파일: {len(content) / (1024 * 1024):.1f}MB)."
        )

    suffix = Path(filename or "").suffix.lower()
    if suffix not in ICON_EXTENSIONS:
        raise ValueError(
            f"'{suffix or filename}' 은 쓸 수 없는 형식입니다. "
            f"{', '.join(sorted(ICON_EXTENSIONS))} 중 하나를 올리세요."
        )

    stem = "".join(SAFE_ICON_NAME.findall(agent_key or "")) or "agent"
    digest = hashlib.sha1(content).hexdigest()[:10]
    target = agent_icon_dir() / f"{stem}-{digest}{suffix}"
    target.write_bytes(content)
    return target.relative_to(PROJECT_ROOT).as_posix()

def resolve_agent_icon(value: Optional[str]) -> Optional[Path]:
    """conf.json 에 적힌 아이콘 경로를 실제 파일로 풉니다. 없으면 None.

    None 이 곧 폴백 신호입니다 — 파일이 지워졌거나 경로가 틀렸거나 애초에 그림이
    아닌 (머티리얼 아이콘 이름) 값이면, 부르는 쪽이 기본 아이콘으로 물러섭니다.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        path = path.resolve()
    except OSError:
        return None
    if path.suffix.lower() not in ICON_EXTENSIONS:
        return None
    return path if path.is_file() else None
