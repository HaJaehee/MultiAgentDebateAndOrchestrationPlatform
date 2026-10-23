"""conf.json 읽기·쓰기와 설정 싱글턴 (`get_config`). `//` 로 시작하는 키는 설명이라 읽을 때 걷어 냅니다.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.config.env import resolve_env_vars
from app.config.paths import DEFAULT_CONFIG_PATH
from app.config.schema import RootConfig


COMMENT_KEY_PREFIX = "//"

def is_comment_key(key: Any) -> bool:
    """`//` 로 시작하는 키는 사람이 읽는 설명입니다 (설정값이 아닙니다)."""
    return isinstance(key, str) and key.lstrip().startswith(COMMENT_KEY_PREFIX)

def strip_comment_keys(value: Any) -> Any:
    """검증에 넘기기 전에 주석 키를 걷어냅니다."""
    if isinstance(value, dict):
        return {k: strip_comment_keys(v) for k, v in value.items() if not is_comment_key(k)}
    if isinstance(value, list):
        return [strip_comment_keys(item) for item in value]
    return value

def read_conf_file(config_path: str | Path) -> Dict[str, Any]:
    """설정 파일을 **주석 키까지 그대로** 읽습니다 (다시 쓸 때 보존하기 위해).

    환경변수 치환도 하지 않습니다. 화면이 보는 값은 이미 풀린 값이라, 그대로
    되쓰면 해석된 API 키가 파일에 평문으로 박히고 .env 를 바꿔도 따라오지 않게
    됩니다. 파일을 고치는 쪽은 늘 이 함수가 돌려준 원문을 손봅니다.
    """
    path = Path(config_path)
    # utf-8-sig: 윈도우 편집기가 붙이는 BOM 을 조용히 걷어냅니다.
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} 의 JSON 문법이 잘못되었습니다 "
            f"(줄 {exc.lineno}, 칸 {exc.colno}): {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 의 최상위는 JSON 객체여야 합니다.")
    return data

def write_conf_file(config_path: str | Path, data: Dict[str, Any]) -> None:
    """설정 파일을 통째로 다시 씁니다.

    임시 파일에 먼저 쓰고 갈아 끼웁니다. 쓰는 도중에 프로세스가 죽어도 반쪽짜리
    설정 파일이 남지 않습니다 — 그 상태가 되면 앱이 아예 기동하지 못합니다.
    """
    path = Path(config_path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> RootConfig:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found at: {path.resolve()}")

    raw_dict = strip_comment_keys(read_conf_file(path))

    resolved_dict = resolve_env_vars(raw_dict)
    config = RootConfig.model_validate(resolved_dict)
    config.raw_mcp_servers = raw_dict.get("mcp_servers") or {}
    return config

# Global singleton instance
_config: Optional[RootConfig] = None

# 그 싱글턴을 어느 파일에서 읽었는지. 아래 persona 갱신이 "지금 쓰는 설정"인지
# 판단하는 데 씁니다.
_config_path: Optional[Path] = None

def get_config(reload: bool = False, config_path: str | Path = DEFAULT_CONFIG_PATH) -> RootConfig:
    global _config, _config_path
    if _config is None or reload:
        _config = load_config(config_path)
        _config_path = Path(config_path).resolve()
    return _config

def active_config_path() -> Optional[Path]:
    """현재 전역 설정을 읽어 온 파일 경로 (아직 안 읽었으면 None)."""
    return _config_path

def reload_config_if_active(path: Path) -> bool:
    """방금 쓴 파일이 **지금 앱이 쓰고 있는 설정일 때만** 다시 읽습니다.

    조건 없이 다시 읽으면, 다른 파일을 고쳤을 뿐인데 전역 설정이 그 파일로
    갈아끼워집니다. 그 뒤의 에이전트 풀·MCP 서버 목록이 통째로 바뀝니다.
    """
    active = active_config_path()
    if active is not None and Path(path).resolve() == active:
        get_config(reload=True, config_path=path)
        return True
    return False
