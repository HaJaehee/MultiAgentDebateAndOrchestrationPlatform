"""환경 변수: `.env` 읽기, `${VAR}` · `${VAR:-기본값}` · 중첩 치환, 여러 줄 글 잇기.

이 모듈을 불러오는 순간 `.env` 를 읽고 `PYTHON_BIN` 기본값을 정합니다 (`app.config` 를 부르면 가장 먼저 불립니다).
"""

import os
import re
import sys
from typing import Any, List, Mapping, Optional

from dotenv import load_dotenv

# Load .env file if present


# .env 가 있으면 읽습니다.
load_dotenv()

# Python 으로 구동되는 MCP 서버는 기본적으로 **앱과 같은 인터프리터**로 띄웁니다.
# conf.json 의 `${PYTHON_BIN:-python}` 이 PATH 의 python 으로 풀리면, 앱이 가상환경
# 에서 돌 때 의존성이 없는 다른 인터프리터를 가리켜 서버가 기동에 실패합니다.
# 사용자가 PYTHON_BIN 을 직접 지정했다면 그 값을 존중합니다.
if not os.environ.get("PYTHON_BIN"):
    os.environ["PYTHON_BIN"] = sys.executable

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-(.*?))?\}")

VAR_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")

def _substitute_env(text: str, overrides: Optional[Mapping[str, str]] = None) -> str:
    """Expands ${VAR} / ${VAR:-default}, where the default may itself contain ${...}.

    `overrides` 가 있으면 `os.environ` 보다 먼저 봅니다. 작업 공간처럼
    **런타임마다 달라야 하는** 값을 전역 환경변수를 건드리지 않고 넘기는
    통로입니다. 전역을 바꾸면 마지막에 부른 쪽이 나머지 전부의 경로를 덮습니다.
    """
    out: List[str] = []
    i, n = 0, len(text)

    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "{":
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            if depth:  # unbalanced braces: emit the remainder verbatim
                out.append(text[i:])
                return "".join(out)

            expr = text[i + 2 : j - 1]
            var_name, sep, default_expr = expr.partition(":-")
            var_name = var_name.strip()
            if VAR_NAME_PATTERN.fullmatch(var_name):
                if overrides is not None and var_name in overrides:
                    resolved = overrides[var_name]
                else:
                    resolved = os.environ.get(var_name, "")
                if not resolved and sep:
                    resolved = _substitute_env(default_expr, overrides)
                out.append(resolved)
            else:
                out.append(text[i:j])
            i = j
        else:
            out.append(text[i])
            i += 1

    return "".join(out)

def join_text_lines(value: Any) -> Any:
    """여러 줄 글을 문자열 배열로도 받습니다.

    JSON 문자열 안의 줄바꿈 이스케이프는 사람이 읽기 어렵습니다. 설정 파일에서는
    한 줄을 한 항목으로 적을 수 있게 하고, 여기서 다시 이어 붙입니다.

        "system_prompt": ["당신은 오케스트레이터입니다.", "요구사항을 분해하세요."]
    """
    if isinstance(value, (list, tuple)):
        return "\n".join("" if item is None else str(item) for item in value)
    return value

def resolve_env_vars(value: Any, overrides: Optional[Mapping[str, str]] = None) -> Any:
    """Recursively resolves ${VAR_NAME} or ${VAR_NAME:-default} strings using os.environ.

    `overrides` 는 `os.environ` 보다 우선합니다 (`_substitute_env` 참고).
    """
    if isinstance(value, str):
        return _substitute_env(value, overrides)
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v, overrides) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item, overrides) for item in value]
    return value
