import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional
from dotenv import load_dotenv
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Load .env file if present
load_dotenv()

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-(.*?))?\}")

# 설정 파일. JSON 이므로 표준 라이브러리 `json` 만으로 읽고 쓸 수 있습니다 —
# 화면에서 고친 값을 되쓸 때 문자열을 손으로 조립하지 않습니다.
CONFIG_FILENAME = "conf.json"
EXAMPLE_CONFIG_FILENAME = "conf.example.json"
DEFAULT_CONFIG_PATH = CONFIG_FILENAME

DEFAULT_SEQUENTIAL_THINKING_PROMPT = """[Sequential Thinking Protocol]
최종 답변을 작성하기 전에, 문제를 최대 {max_steps}단계로 나누어 순차적으로 사고하세요.
- `Thought 1..N` 형식으로 각 단계를 명시하고, 각 단계에서는 직전 단계의 결론을 근거로 다음 논리를 전개합니다.
- 중간에 가정이 틀렸다고 판단되면 `Revision of Thought K:` 로 해당 단계를 수정한 뒤 진행하세요.
- 도구(MCP)가 필요한 단계에서는 먼저 도구를 호출해 사실을 검증한 후 다음 단계로 넘어가세요.
- 사고가 끝나면 `---` 구분선 뒤에 `## 최종 결론`을 두고, 그 아래에는 사고 과정이 아닌 결과물만 작성하세요."""


VAR_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")

# Python 으로 구동되는 MCP 서버는 기본적으로 **앱과 같은 인터프리터**로 띄웁니다.
# conf.json 의 `${PYTHON_BIN:-python}` 이 PATH 의 python 으로 풀리면, 앱이 가상환경
# 에서 돌 때 의존성이 없는 다른 인터프리터를 가리켜 서버가 기동에 실패합니다.
# 사용자가 PYTHON_BIN 을 직접 지정했다면 그 값을 존중합니다.
if not os.environ.get("PYTHON_BIN"):
    os.environ["PYTHON_BIN"] = sys.executable

# 프로젝트 루트 = app/ 의 부모. cwd 가 아니라 이 값을 기준으로 삼습니다.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 작업 공간은 **절대 경로**로 고정합니다.
#
# conf.json 이 `${WORKSPACE_DIR:-./workspace}` 로 상대 경로를 넘기면, 그 값을
# 받는 쪽이 각자의 cwd 로 resolve 합니다. filesystem MCP(node)와 sandbox MCP
# (python)는 서로 다른 프로세스이고, 특히 샌드박스는 받은 값을
# `Path(v).resolve()` 로 자기 cwd 기준으로 풉니다. 그래서 "같은 ./workspace 를
# 줬는데 두 서버가 다른 폴더를 본다" 가 됩니다.
#
# 여기서 한 번 절대 경로로 만들어 두면 argv 로 가든 env 로 가든 같은 곳을
# 가리킵니다.

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


os.environ["WORKSPACE_DIR"] = str(resolve_workspace_dir(os.environ.get("WORKSPACE_DIR")))


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


class AppConfig(BaseModel):
    host: str = Field(default="127.0.0.1", description="Application Host")
    port: int = Field(default=8000, description="Application Port")
    db_url: str = Field(
        default="sqlite+aiosqlite:///./multiagent.db",
        description="Async SQLAlchemy database URL",
    )
    debug: bool = Field(default=True, description="Debug mode")

    @field_validator("port", mode="before")
    @classmethod
    def _coerce_port(cls, v: Any) -> int:
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v else 8000
        return int(v)


class MCPServerConfig(BaseModel):
    """MCP 서버 하나. 로컬 프로세스(stdio)이거나 원격 주소(HTTP)입니다.

    둘은 서로 배타적입니다. `command` 가 있으면 이 앱이 프로세스를 띄워 표준
    입출력으로 이야기하고, `url` 이 있으면 이미 떠 있는 서버에 HTTP 로 붙습니다.
    둘 다 적거나 둘 다 비우면 무엇을 하려는 것인지 알 수 없으므로 거절합니다 —
    조용히 하나를 고르면, 고치려는 사람이 왜 다른 쪽이 무시되는지 알 수 없습니다.
    """

    command: str = Field(default="", description="Command to execute a local MCP server, e.g. npx or python")
    args: List[str] = Field(default_factory=list, description="CLI arguments (stdio only)")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables for the server (stdio only)")

    # --- 원격 서버 ---------------------------------------------------------
    url: str = Field(default="", description="Remote MCP endpoint, e.g. https://host/mcp (http/sse transport)")
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers sent with every request, e.g. Authorization (remote only)",
    )
    transport: Literal["auto", "stdio", "http", "sse"] = Field(
        default="auto",
        description=(
            "auto: command -> stdio, url -> http (falling back to sse) | "
            "stdio: local process | http: Streamable HTTP | sse: legacy HTTP+SSE"
        ),
    )
    timeout: float = Field(
        default=30.0, gt=0, description="Remote only: seconds to wait for a single HTTP request"
    )

    enabled: bool = Field(default=True, description="Whether this MCP server is started at startup")

    @model_validator(mode="after")
    def _exactly_one_endpoint(self) -> "MCPServerConfig":
        has_command = bool((self.command or "").strip())
        has_url = bool((self.url or "").strip())
        if has_command and has_url:
            raise ValueError(
                "MCP 서버에는 command 와 url 중 하나만 적을 수 있습니다 "
                "(로컬 프로세스인지 원격 주소인지)."
            )
        if not has_command and not has_url:
            raise ValueError("MCP 서버에는 command(로컬) 또는 url(원격) 중 하나가 필요합니다.")
        if self.transport == "stdio" and not has_command:
            raise ValueError("transport 가 stdio 이면 command 가 있어야 합니다.")
        if self.transport in ("http", "sse") and not has_url:
            raise ValueError(f"transport 가 {self.transport} 이면 url 이 있어야 합니다.")
        return self

    @property
    def is_remote(self) -> bool:
        return bool((self.url or "").strip())

    @property
    def resolved_transport(self) -> str:
        """`auto` 를 실제 전송 방식으로 풉니다.

        원격의 기본값이 `http`(Streamable HTTP)인 것은 그것이 지금의 표준이기
        때문입니다. 옛 SSE 서버는 붙을 때 한 번 물러서서 다시 시도합니다
        (`MCPClientConnection`). 처음부터 `sse` 로 적어 두면 그 왕복을 건너뜁니다.
        """
        if self.transport != "auto":
            return self.transport
        return "http" if self.is_remote else "stdio"

    @property
    def endpoint_label(self) -> str:
        """화면에 보여줄 한 줄 (어디에 붙는 서버인가)."""
        if self.is_remote:
            return self.url
        return " ".join([self.command, *self.args]).strip()


class SequentialThinkingConfig(BaseModel):
    """Step-by-step (Sequential Thinking) reasoning settings for an agent."""

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False, description="Enable sequential/step-by-step reasoning")
    mode: Literal["prompt", "native", "mcp"] = Field(
        default="prompt",
        description=(
            "prompt: inject a step-by-step protocol into the system prompt | "
            "native: use the provider's own reasoning parameters (reasoning_effort / thinking budget) | "
            "mcp: force usage of a sequential-thinking MCP server"
        ),
    )
    max_steps: int = Field(default=5, ge=1, le=50, description="Maximum number of reasoning steps")
    reasoning_effort: Optional[Literal["minimal", "low", "medium", "high"]] = Field(
        default=None, description="native mode: provider reasoning effort level"
    )
    thinking_budget_tokens: Optional[int] = Field(
        default=None, ge=0, description="native mode: extended-thinking token budget (Anthropic style)"
    )
    mcp_server: str = Field(
        default="sequential_thinking",
        description="mcp mode: MCP server key providing the sequentialthinking tool",
    )
    prompt_template: str = Field(
        default=DEFAULT_SEQUENTIAL_THINKING_PROMPT,
        description="prompt/mcp mode: text appended to the system prompt ({max_steps} placeholder supported)",
    )
    show_steps: bool = Field(
        default=True,
        description="Show reasoning steps in the debate feed (False keeps only the final conclusion)",
    )

    @field_validator("prompt_template", mode="before")
    @classmethod
    def _join_prompt_lines(cls, v: Any) -> Any:
        return join_text_lines(v)

    def render_prompt(self) -> str:
        """Renders the protocol text injected into the system prompt."""
        try:
            return self.prompt_template.format(max_steps=self.max_steps)
        except (KeyError, IndexError, ValueError):
            return self.prompt_template


# Fields an agent inherits from the global "llm" object when it does not set them itself.
INHERITABLE_LLM_FIELDS = (
    "model",
    "api_key",
    "api_base",
    "api_url",
    "base_url",
    "api_version",
    "provider",
    "custom_llm_provider",
    "temperature",
    "top_p",
    "max_tokens",
    "max_context_window",
    "timeout",
    "num_retries",
    "drop_params",
    "extra_headers",
    "extra_body",
    "max_tool_iterations",
    "max_continuations",
)

# Aliases that refer to the same underlying field; setting any one blocks inheritance of the group.
_ALIAS_GROUPS = (
    ("api_base", "api_url", "base_url"),
    ("provider", "custom_llm_provider"),
)


def _is_blank(value: Any) -> bool:
    """None, empty string, or whitespace-only string (e.g. an unresolved env var)."""
    return value is None or (isinstance(value, str) and not value.strip())


# 한 턴에서 허용하는 MCP 도구 루프의 하드 상한.
#
# 실제 횟수는 에이전트마다 `max_tool_iterations` 로 정합니다 (기본 30). 이 값은
# 그 설정이 넘어설 수 없는 선입니다 — 루프 한 번이 LLM 호출 한 번이라, 오타로
# 큰 수가 들어가면 한 발언이 그만큼의 요금과 시간을 씁니다.
#
# 50 이었는데, 파일을 훑거나 저장소를 뒤지는 작업은 그 안에 못 끝내고
# `max_tool_iterations` 소진으로 발언이 통째로 실패하는 일이 있었습니다
# (그때까지 실행된 도구와 관측은 남지만 답변은 나오지 않습니다). 100 이면
# 그런 작업이 들어가고, 폭주를 막는 선으로서의 역할도 그대로입니다.
TOOL_ITERATION_CEILING = 130

# 한 발언에서 "이어받기"를 허용하는 하드 상한.
#
# 응답이 `max_tokens` 에 걸려 잘리면 이어서 한 판 더 받아 붙입니다. 실제 횟수는
# 에이전트마다 `max_continuations` 로 정합니다 (기본 2). 도구 루프와 같은 이유로
# 상한을 둡니다 — 이어받기 한 번도 LLM 호출 한 번이고, 계속 잘리는 응답을 끝없이
# 이어받으면 한 발언이 컨텍스트와 요금을 통째로 씁니다.
#
# 기본 2 는 `max_tokens` 의 3배까지 받을 수 있다는 뜻입니다 (원본 + 2회). 그보다
# 긴 산출물이 필요하면 `max_tokens` 자체를 올리는 편이 낫습니다 — 이어받기는
# 이음매가 생기고, 모델이 앞부분을 다시 읽어야 하므로 공짜가 아닙니다.
CONTINUATION_CEILING = 10

# 순서를 지정하지 않은 에이전트의 발언 우선순위. 전부 같은 값이라 정렬이 안정적으로
# 유지되어 conf.json 에 적힌 순서가 그대로 나옵니다. 화면에서 순서를 바꾸면 그때
# 10, 20, 30... 이 실제로 적힙니다 (사이에 끼워 넣을 자리를 남겨 둡니다).
DEFAULT_DEBATE_PRIORITY = 100
DEBATE_PRIORITY_STEP = 10
DEBATE_STANCES = ("proponent", "critic", "neutral")


# ---------------------------------------------------------------------------
# 도구 보안 (`app/mcp/policy.py`)
# ---------------------------------------------------------------------------

TOOL_SECURITY_MODES = ("read_only", "default", "review", "auto")

# 기본 거부 규칙. conf.json 에 `deny` 를 적으면 **이 목록을 대신합니다** (합치지 않습니다) —
# 그래야 필요 없는 항목을 지울 수 있습니다.
#
# 비밀 파일은 어느 도구로 읽든(sandbox 코드 안의 경로 포함) 같은 규칙에 걸립니다.
# 유출용으로 널리 쓰이는 수신 서비스는 네트워크가 "묻기" 여도 사람이 무심코 허락하기
# 쉬워 아예 막습니다 (Antigravity 의 기본 허용 목록에 webhook.site 가 들어 있어 그대로
# 유출 경로가 된 사례). 목록은 완전하지 않습니다 — 네트워크의 기본값이 묻기인 이유입니다.
DEFAULT_TOOL_DENY_RULES = (
    "read(**/.env)",
    "read(**/.env.*)",
    "read(**/*.pem)",
    "read(**/*.key)",
    "read(**/*.pfx)",
    "read(**/*.p12)",
    "read(**/id_rsa*)",
    "read(**/id_ed25519*)",
    "read(**/id_ecdsa*)",
    "read(**/.ssh/**)",
    "net(webhook.site)",
    "net(requestbin.net)",
    "net(pipedream.net)",
    "net(beeceptor.com)",
    "net(ngrok.io)",
    "net(ngrok-free.app)",
    "net(interact.sh)",
    "net(oast.fun)",
    "net(burpcollaborator.net)",
)

# 사람의 답을 기다리는 시간(초)의 기본값. 도구 상한·컨텍스트 창과 같습니다.
DEFAULT_APPROVAL_TIMEOUT = 180.0


def _validate_rule_list(value: Any) -> List[str]:
    """규칙 문자열 목록을 검증합니다. 틀린 줄이 있으면 전부 모아 알립니다."""
    from app.mcp.policy import parse_rules  # app.mcp 가 app.config 를 가져오므로 늦게

    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    rules = [str(v).strip() for v in value if str(v or "").strip()]
    parse_rules(rules)
    return rules


class AgentToolSecurity(BaseModel):
    """에이전트별 덮어쓰기 (`tool_security.agents.<key>`). **조이기만** 합니다.

    모드는 대화 모드와 비교해 더 엄격한 쪽이 쓰이고, 규칙은 전역 규칙에 더해집니다.
    허용(allow) 규칙은 받지 않습니다 — 에이전트 하나를 위해 전역 거부를 풀 길이
    생기면 안 되기 때문입니다.

    `agents.<key>` 안이 아니라 여기 두는 이유: 에이전트 설정은 대화가 시작될 때
    스냅샷으로 굳습니다(`session_agents.config_snapshot`). 보안은 굳으면 안 됩니다 —
    나중에 조인 규칙이 지난 대화에도 바로 걸려야 합니다. 보안 설정이 한 곳에 모여
    있어야 무엇이 풀려 있는지 한눈에 보이기도 합니다.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Optional[Literal["read_only", "default", "review", "auto"]] = None
    deny: List[str] = Field(default_factory=list)
    ask: List[str] = Field(default_factory=list)

    @field_validator("mode", mode="before")
    @classmethod
    def _blank_mode(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("deny", "ask", mode="before")
    @classmethod
    def _rules(cls, v: Any) -> List[str]:
        return _validate_rule_list(v)


class ToolSecurityConfig(BaseModel):
    """전역 도구 보안 설정 (`tool_security`)."""

    mode: Literal["read_only", "default", "review", "auto"] = Field(
        default="default",
        description="새 대화의 기본 모드. 대화마다 로스터 패널에서 바꿀 수 있습니다.",
    )
    approval_timeout: float = Field(
        default=DEFAULT_APPROVAL_TIMEOUT, ge=10, le=3600,
        description="승인 카드에 답이 없으면 이 시간(초)이 지나 거부합니다.",
    )
    deny: List[str] = Field(default_factory=lambda: list(DEFAULT_TOOL_DENY_RULES))
    ask: List[str] = Field(default_factory=list)
    allow: List[str] = Field(default_factory=list)
    # 도구 이름표·annotations 를 믿을 서버. 비워 두면(None) 로컬 프로세스 서버와 이 PC
    # 안의 원격 서버(127.0.0.1 등)를 믿고, 다른 호스트의 원격 서버는 믿지 않습니다.
    trusted_servers: Optional[List[str]] = Field(default=None)
    # 에이전트별로 더 조이는 설정 (키 = 에이전트 키).
    agents: Dict[str, AgentToolSecurity] = Field(default_factory=dict)

    @field_validator("deny", "ask", "allow", mode="before")
    @classmethod
    def _rules(cls, v: Any) -> List[str]:
        return _validate_rule_list(v)


class LLMConfig(BaseModel):
    """Global LLM defaults (the "llm" object). Every agent inherits these unless it overrides them."""

    model_config = ConfigDict(populate_by_name=True)

    model: Optional[str] = Field(default=None, description="Default model name, e.g. 'openai/gpt-4o'")
    api_key: Optional[str] = Field(default=None, description="Default API key")
    api_base: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("api_base", "api_url", "base_url"),
        description="Default API endpoint URL (OpenAI-compatible gateway, Ollama, vLLM, LM Studio...)",
    )
    api_version: Optional[str] = Field(default=None, description="Default API version (Azure OpenAI)")
    provider: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("provider", "custom_llm_provider"),
        description="Force the LiteLLM provider, e.g. 'openai', 'azure', 'ollama', 'vertex_ai'",
    )
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    max_context_window: Optional[int] = Field(default=None, gt=0)
    timeout: Optional[float] = Field(default=None, gt=0, description="Request timeout in seconds")
    num_retries: Optional[int] = Field(default=None, ge=0, description="LiteLLM retry count")
    drop_params: Optional[bool] = Field(
        default=None, description="Silently drop parameters the endpoint does not support"
    )
    extra_headers: Optional[Dict[str, str]] = Field(default=None, description="Extra HTTP headers")
    extra_body: Optional[Dict[str, Any]] = Field(default=None, description="Extra JSON body fields")
    max_tool_iterations: Optional[int] = Field(default=None, ge=1, le=TOOL_ITERATION_CEILING)
    max_continuations: Optional[int] = Field(default=None, ge=0, le=CONTINUATION_CEILING)
    sequential_thinking: SequentialThinkingConfig = Field(default_factory=SequentialThinkingConfig)

class AgentConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="Agent display name")
    role: str = Field(description="Agent role or title")
    enabled: bool = Field(default=True, description="Register this agent in the pool")
    model: str = Field(default="openai/gpt-4o", description="LLM model identifier")
    api_key: Optional[str] = Field(default="", description="API key or env var reference")
    api_base: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("api_base", "api_url", "base_url"),
        description="Custom API base URL (accepts api_base / api_url / base_url)",
    )
    api_version: Optional[str] = Field(default=None, description="API version (Azure OpenAI)")
    provider: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("provider", "custom_llm_provider"),
        description="Explicit LiteLLM provider override",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, gt=0)
    max_context_window: int = Field(default=128000, gt=0)
    timeout: Optional[float] = Field(default=None, gt=0, description="Request timeout in seconds")
    num_retries: int = Field(default=0, ge=0, description="LiteLLM retry count")
    drop_params: bool = Field(
        default=True, description="Silently drop parameters the endpoint does not support"
    )
    extra_headers: Dict[str, str] = Field(default_factory=dict, description="Extra HTTP headers")
    extra_body: Dict[str, Any] = Field(default_factory=dict, description="Extra JSON body fields")
    max_tool_iterations: int = Field(
        default=30, ge=1, le=TOOL_ITERATION_CEILING,
        description="Max MCP tool-loop iterations per turn",
    )
    max_continuations: int = Field(
        default=2, ge=0, le=CONTINUATION_CEILING,
        description="How many times a turn cut off at max_tokens may be continued (0 = never)",
    )
    allowed_mcp_servers: List[str] = Field(
        default_factory=list, description="List of MCP server keys this agent can access"
    )
    # 토론에서의 자리. 예전에는 전략 코드가 'architect' 다음 'coder' 하는 식으로
    # 키를 문자열로 박아 두었는데, 화면에서 에이전트를 만들 수 있게 되면서 그
    # 방식으로는 새 에이전트가 언제나 맨 뒤로 밀렸습니다. 이제 순서와 진영은
    # 에이전트 자신이 들고 다닙니다.
    debate_priority: int = Field(
        default=DEFAULT_DEBATE_PRIORITY,
        ge=0,
        le=999,
        description="Speaking order within a round; lower speaks earlier. Ties keep conf.json order.",
    )
    debate_stance: Literal["proponent", "critic", "neutral"] = Field(
        default="neutral",
        description=(
            "Role in the adversarial-debate strategy. "
            "proponent: proposes and defends | critic: challenges | neutral: neither side"
        ),
    )
    sequential_thinking: SequentialThinkingConfig = Field(default_factory=SequentialThinkingConfig)
    system_prompt: str = Field(default="", description="Base system instructions")
    # 화면에서 고르는 겉모습. 비어 있으면 에이전트 키에서 색과 아이콘을 정하던
    # 예전 규칙 (`style_for_agent`) 이 그대로 적용됩니다.
    card_color: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("card_color", "color"),
        description="Agent card accent colour: '#rrggbb' or a Quasar palette name",
    )
    # 머티리얼 아이콘 이름 ('psychology') 이거나, 올린 그림의 경로
    # ('data/agent_icons/coder-1a2b3c4d5e.png'). 경로는 프로젝트 루트 기준입니다.
    icon: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("icon", "avatar", "icon_path"),
        description="Material icon name, or path to an uploaded icon image",
    )

    @field_validator("system_prompt", mode="before")
    @classmethod
    def _join_prompt_lines(cls, v: Any) -> Any:
        return join_text_lines(v)

    @field_validator("api_key", "api_base", "api_version", "provider", "card_color", "icon",
                     mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        """Unresolved env vars ('${LLM_API_BASE}' with no value) resolve to '' -> treat as unset."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("api_base")
    @classmethod
    def _strip_trailing_slash(cls, v: Optional[str]) -> Optional[str]:
        return v.rstrip("/") if isinstance(v, str) else v

    @property
    def is_live(self) -> bool:
        """True when the agent has enough connection info to reach a real LLM endpoint."""
        if self.api_base:
            return True
        if self.api_key and self.api_key.strip():
            return True
        # Local runtimes that need neither an API key nor an explicit URL
        return self.model.split("/", 1)[0] in {"ollama", "ollama_chat", "lm_studio"}


class RootConfig(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=dict)
    agents: Dict[str, AgentConfig] = Field(default_factory=dict)
    tool_security: ToolSecurityConfig = Field(default_factory=ToolSecurityConfig)

    # 치환 **전** 의 "mcp_servers" 원문. 작업 공간을 런타임마다 다르게 풀 때
    # 이걸 그 작업 공간으로 다시 풉니다. 이미 치환된 경로 문자열을 찾아 바꾸는
    # 것보다 정확합니다 — 같은 치환기를 그대로 한 번 더 돌리는 것이니까요.
    raw_mcp_servers: Dict[str, Any] = Field(default_factory=dict, exclude=True)

    def mcp_servers_for_workspace(
        self, workspace: Path, extra_env: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, MCPServerConfig]:
        """"mcp_servers" 를 이 작업 공간 기준으로 해석합니다.

        **`os.environ` 은 건드리지 않습니다.** 예전에는 여기서 전역
        `WORKSPACE_DIR` 을 바꿔 자식 프로세스에게 물려줬지만, 작업 공간이 다른
        런타임이 둘 이상 살아 있으면 마지막에 부른 쪽이 나머지 전부의 경로를
        덮어씁니다. 대신 치환기에 이 작업 공간만 얹어 넘기고, 결과로 나온 각
        서버의 `env` 에 `WORKSPACE_DIR` 을 **명시적으로** 적습니다. 자식은
        부모의 낡은 전역이 아니라 그 값을 봅니다.

        `extra_env` 는 런타임이 자기 프로세스에만 걸고 싶은 환경변수입니다
        (TMP, 샌드박스 네임스페이스 상한 등). 치환에도 쓰이고 결과 `env` 에도
        얹히므로, conf.json 이 `${...}` 로 참조하는 값도 런타임마다 다르게
        줄 수 있습니다.
        """
        overrides: Dict[str, str] = {"WORKSPACE_DIR": str(workspace)}
        overrides.update({k: str(v) for k, v in (extra_env or {}).items()})

        if self.raw_mcp_servers:
            resolved = resolve_env_vars(self.raw_mcp_servers, overrides)
            servers = {name: MCPServerConfig.model_validate(cfg) for name, cfg in resolved.items()}
        else:
            # 원문이 없는 경우(테스트에서 손으로 만든 설정 등). 이미 풀린 값을
            # 복사해 씁니다 — 원본을 고치면 다른 런타임까지 따라 바뀝니다.
            servers = {name: cfg.model_copy(deep=True) for name, cfg in self.mcp_servers.items()}

        # 매니저는 켜져 있는 서버만 띄웁니다 (`enabled_mcp_servers` 와 같은 기준).
        servers = {name: cfg for name, cfg in servers.items() if cfg.enabled}
        for cfg in servers.values():
            # 원격 서버는 우리가 띄우는 프로세스가 아니라 환경을 줄 자리가 없습니다.
            if cfg.command:
                cfg.env = {**overrides, **cfg.env}
        return servers

    @model_validator(mode="before")
    @classmethod
    def apply_llm_defaults(cls, data: Any) -> Any:
        """Merges the global "llm" object into every agent that does not override a field."""
        if not isinstance(data, dict):
            return data

        llm_raw = data.get("llm") or {}
        agents_raw = data.get("agents") or {}
        if not isinstance(llm_raw, dict) or not llm_raw or not isinstance(agents_raw, dict):
            return data

        data = copy.deepcopy(data)
        llm_raw = data["llm"]
        agents_raw = data["agents"]

        for agent_raw in agents_raw.values():
            if not isinstance(agent_raw, dict):
                continue

            for field in INHERITABLE_LLM_FIELDS:
                if _is_blank(llm_raw.get(field)):
                    continue
                group = next((g for g in _ALIAS_GROUPS if field in g), (field,))
                # An agent value that resolved to an empty string (unset env var) still inherits.
                if any(not _is_blank(agent_raw.get(alias)) for alias in group):
                    continue
                agent_raw[field] = llm_raw[field]

            st_default = llm_raw.get("sequential_thinking")
            if isinstance(st_default, dict):
                merged = dict(st_default)
                st_agent = agent_raw.get("sequential_thinking")
                if isinstance(st_agent, dict):
                    merged.update(st_agent)
                agent_raw["sequential_thinking"] = merged

        return data

    @model_validator(mode="after")
    def validate_orchestrator_exists(self) -> "RootConfig":
        if "orchestrator" not in self.agents:
            raise ValueError("Configuration must contain an \"agents.orchestrator\" definition.")
        if not self.agents["orchestrator"].enabled:
            raise ValueError("The \"agents.orchestrator\" agent cannot be disabled (enabled: false).")
        return self

    @property
    def enabled_agents(self) -> Dict[str, AgentConfig]:
        return {k: v for k, v in self.agents.items() if v.enabled}

    @property
    def enabled_mcp_servers(self) -> Dict[str, MCPServerConfig]:
        return {k: v for k, v in self.mcp_servers.items() if v.enabled}


# ---------------------------------------------------------------------------
# 설정 파일 읽기 / 쓰기
#
# 설정은 JSON 입니다. 표준 라이브러리의 `json` 하나로 읽고 쓸 수 있어서, 화면에서
# 고친 값을 파일에 반영할 때 문자열을 직접 조립할 필요가 없습니다. 예전 TOML 시절
# 에는 표준 라이브러리에 기록기가 없어 줄 단위로 파일을 고쳤고, 그 코드가 여러 줄
# 문자열·주석·섹션 경계를 전부 손으로 다뤄야 했습니다.
#
# 주석은 `//` 로 시작하는 키로 적습니다. JSON 자체에는 주석 문법이 없지만, 이
# 규칙이면 설명이 데이터의 일부로 남아 읽고 다시 쓰는 것만으로 그대로 보존됩니다.
# 값은 문자열 하나 또는 문자열 배열(여러 줄)입니다.
#
#     "// filesystem": ["공용 작업 공간 파일 I/O", "지정한 디렉터리 밖은 차단됩니다"],
#     "filesystem": { "command": "${NODE_BIN:-node}", ... }
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 설정 파일 편집 공통
#
# 에이전트와 MCP 서버 구성은 **배포 설정**입니다. 대화마다 갈리는 값(페르소나,
# 작업 공간)과 달리 이 파일이 정본이고, 화면에서 바꾼 것도 여기 남아야 다음
# 기동에서 살아납니다.
#
# 모든 편집이 같은 모양입니다 — 입력을 먼저 전부 검증하고, 원문을 읽어, 딕셔너리를
# 고치고, 한 번에 다시 씁니다. 검증이 앞에 있으므로 잘못된 입력은 파일에 닿지
# 않습니다.
# ---------------------------------------------------------------------------

# 에이전트 키와 MCP 서버 이름의 규칙. JSON 은 어떤 문자열이든 키로 받지만, 이
# 이름들은 도구 이름의 접두사로도 쓰이므로 단순해야 합니다.
BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

# `ui.number` 는 무엇을 넣든 float 를 돌려줍니다. 이 항목들은 정수로 적어야
# `"max_tokens": 4096.0` 같은 값이 파일에 남지 않습니다.
INT_VALUE_FIELDS = frozenset(
    {
        "max_tokens",
        "max_context_window",
        "num_retries",
        "max_tool_iterations",
        "max_continuations",
        "max_steps",
        "thinking_budget_tokens",
        "debate_priority",
        "port",
    }
)


def _require_key(value: str, label: str) -> str:
    name = (value or "").strip()
    if not BARE_KEY_PATTERN.fullmatch(name):
        raise ValueError(
            f"{label} '{value}' 을 쓸 수 없습니다. "
            f"영문/숫자/밑줄/하이픈만 쓰고 숫자나 하이픈으로 시작하지 마세요."
        )
    return name


def _section(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    """최상위 섹션을 꺼냅니다. 없으면 빈 것으로 만들어 끼웁니다."""
    node = data.get(name)
    if not isinstance(node, dict):
        node = {}
        data[name] = node
    return node


def _entry(data: Dict[str, Any], section: str, key: str, path: Path) -> Dict[str, Any]:
    """`section.key` 의 설정 블록. 없으면 KeyError."""
    node = data.get(section)
    entry = node.get(key) if isinstance(node, dict) else None
    if not isinstance(entry, dict):
        raise KeyError(f"{path.name} 에 {section}.{key} 가 없습니다.")
    return entry


def _text_value(value: Any) -> Any:
    """여러 줄 글은 문자열 배열로 적습니다.

    JSON 문자열 안의 줄바꿈 이스케이프는 사람이 읽기 어렵습니다. 배열로 적어 두면
    한 줄이 한 항목이 되어 편집기에서 그대로 읽히고, 읽는 쪽(`join_text_lines`)이
    다시 이어 붙입니다.
    """
    text = str(value if value is not None else "").strip()
    return text.split("\n") if "\n" in text else text


def _scalar_value(field: str, value: Any) -> Any:
    """설정 파일에 적을 값으로 정규화합니다."""
    # bool 은 int 의 하위형이라 반드시 먼저 봅니다.
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) if field in INT_VALUE_FIELDS else value
    if isinstance(value, str):
        return _text_value(value)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    raise ValueError(f"설정 파일에 적을 수 없는 값입니다: {value!r}")


# ---------------------------------------------------------------------------
# agents.* 페르소나 (이름 · 역할 · 시스템 프롬프트)
# ---------------------------------------------------------------------------


def update_agent_persona_in_conf_file(
    agent_key: str,
    name: str,
    role: str,
    system_prompt: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<agent_key>` 의 name / role / system_prompt 를 갱신합니다.

    파일에 없는 에이전트면 새로 만듭니다 (이 대화에만 있던 에이전트를 전역 설정에
    저장하는 경로).
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path.resolve()}")

    data = read_conf_file(path)
    agents = _section(data, "agents")

    agent = agents.get(agent_key)
    if not isinstance(agent, dict):
        agent = {}
        agents[agent_key] = agent

    agent["name"] = (name or "").strip()
    agent["role"] = (role or "").strip()
    agent["system_prompt"] = _text_value(system_prompt)

    write_conf_file(path, data)
    reload_config_if_active(path)


def update_agent_appearance_in_conf_file(
    agent_key: str,
    card_color: Optional[str],
    icon: Optional[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<agent_key>` 의 card_color / icon 을 갱신합니다.

    빈 값은 항목을 지웁니다 — "기본값으로 되돌린다" 는 뜻이고, 그러면 다시
    에이전트 키에서 색과 아이콘이 정해집니다 (`style_for_agent`).

    파일에 없는 에이전트면 새로 만들지 않습니다. 겉모습만으로 에이전트를
    만들어 두면 이름도 역할도 없는 껍데기가 `agents` 에 남습니다.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path.resolve()}")

    data = read_conf_file(path)
    agents = _section(data, "agents")

    agent = agents.get(agent_key)
    if not isinstance(agent, dict):
        raise KeyError(f"{path.name} 에 agents.{agent_key} 가 없습니다.")

    for field, value in (("card_color", card_color), ("icon", icon)):
        text = (value or "").strip()
        if text:
            agent[field] = text
        else:
            agent.pop(field, None)
            # 옛 표기(`color` / `avatar`)로 적혀 있던 값도 함께 걷어냅니다.
            # 남겨 두면 지웠는데도 그 값이 계속 읽힙니다.
            agent.pop({"card_color": "color", "icon": "avatar"}[field], None)

    write_conf_file(path, data)
    reload_config_if_active(path)


# ---------------------------------------------------------------------------
# mcp_servers.* 편집
# ---------------------------------------------------------------------------


def set_mcp_server_enabled_in_conf_file(
    server_name: str,
    enabled: bool,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`mcp_servers.<name>.enabled` 를 바꿉니다."""
    name = _require_key(server_name, "MCP 서버 이름")
    path = Path(config_path)

    data = read_conf_file(path)
    server = _entry(data, "mcp_servers", name, path)
    server["enabled"] = bool(enabled)

    write_conf_file(path, data)
    reload_config_if_active(path)


def add_mcp_server_to_conf_file(
    server_name: str,
    command: str = "",
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    enabled: bool = True,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    url: str = "",
    headers: Optional[Dict[str, str]] = None,
    transport: str = "auto",
) -> None:
    """`mcp_servers` 에 새 서버를 추가합니다 (로컬 프로세스 또는 원격 주소).

    `command` 와 `url` 중 하나만 받습니다. 검증은 모델과 같은 규칙입니다 —
    화면에서 들어오든 파일을 직접 고치든 같은 말을 들어야 합니다.
    """
    name = _require_key(server_name, "MCP 서버 이름")
    path = Path(config_path)

    command = (command or "").strip()
    url = (url or "").strip()
    if command and url:
        raise ValueError("실행 명령(command)과 주소(url)는 함께 쓸 수 없습니다.")
    if not command and not url:
        raise ValueError("실행 명령(command) 또는 주소(url) 중 하나는 있어야 합니다.")
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("원격 서버 주소는 http:// 또는 https:// 로 시작해야 합니다.")

    args = [a for a in (args or []) if a.strip() != ""]
    env = env or {}
    for key in env:
        if not BARE_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"환경변수 이름 '{key}' 을 쓸 수 없습니다.")
    headers = headers or {}
    for key in headers:
        if not key.strip() or any(c in key for c in " \t\r\n:"):
            raise ValueError(f"HTTP 헤더 이름 '{key}' 을 쓸 수 없습니다.")

    data = read_conf_file(path)
    servers = _section(data, "mcp_servers")
    if name in servers:
        raise ValueError(f"'{name}' 서버가 이미 {path.name} 에 있습니다.")

    block: Dict[str, Any] = {}
    if url:
        block["url"] = url
        if headers:
            block["headers"] = dict(headers)
        if transport and transport != "auto":
            block["transport"] = transport
    else:
        block["command"] = command
        block["args"] = list(args)
        if env:
            block["env"] = dict(env)
    block["enabled"] = bool(enabled)
    servers[name] = block

    write_conf_file(path, data)
    reload_config_if_active(path)


def remove_mcp_server_from_conf_file(
    server_name: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`mcp_servers.<name>` 을 지웁니다.

    그 위에 붙은 `//` 설명은 남깁니다. 사람이 쓴 글을 지우는 것은 되돌릴 수
    없고, 같은 서버를 다시 추가할 때 그대로 쓸 수 있는 정보이기 때문입니다.
    """
    name = _require_key(server_name, "MCP 서버 이름")
    path = Path(config_path)

    data = read_conf_file(path)
    _entry(data, "mcp_servers", name, path)   # 없으면 KeyError
    del data["mcp_servers"][name]

    write_conf_file(path, data)
    reload_config_if_active(path)


def add_tool_security_rules_to_conf_file(
    effect: str,
    rules: List[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> List[str]:
    """`tool_security.<effect>` 에 규칙을 더합니다. 실제로 더해진 규칙을 돌려줍니다.

    승인 카드의 "항상 허용"·"항상 거부" 가 부릅니다. 이미 있는 규칙은 다시 적지 않습니다.
    `tool_security` 가 아직 없는 설정이면 그 칸만 만들고, 나머지는 적지 않아 코드의
    기본값이 그대로 걸립니다.

    `deny` 만은 다릅니다. 파일에 `deny` 를 적으면 기본 거부 목록(비밀 파일, 유출용 수신
    서비스)을 **대신**하므로, 규칙 한 줄만 적으면 기본 보호가 통째로 사라집니다. 그래서
    `deny` 가 아직 없으면 기본 목록을 먼저 옮겨 적고 그 뒤에 더합니다.
    """
    if effect not in ("deny", "ask", "allow"):
        raise ValueError(f"'{effect}' 는 규칙 종류가 아닙니다 (deny · ask · allow).")
    wanted = _validate_rule_list(rules)
    if not wanted:
        return []
    path = Path(config_path)

    data = read_conf_file(path)
    section = _section(data, "tool_security")
    current = section.get(effect)
    if current is None:
        current = list(DEFAULT_TOOL_DENY_RULES) if effect == "deny" else []
    elif not isinstance(current, list):
        raise ValueError(f"{path.name} 의 tool_security.{effect} 가 목록이 아닙니다.")
    added = [r for r in wanted if r not in current]
    if not added:
        return []
    section[effect] = list(current) + added

    write_conf_file(path, data)
    reload_config_if_active(path)
    return added


def set_agent_allowed_mcp_servers_in_conf_file(
    agent_key: str,
    servers: List[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.allowed_mcp_servers` 를 통째로 갈아 끼웁니다.

    어떤 에이전트가 어떤 도구를 쓰는지는 배포 설정이라 이 파일이 정본입니다.
    다만 **아직 시작하지 않은 대화**에만 걸립니다. 대화는 첫 발언과 함께 도구
    권한까지 `session_agents.config_snapshot` 으로 굳으므로, 이미 시작한 대화는
    여기서 무엇을 바꾸든 그때의 권한을 그대로 씁니다.
    """
    key = _require_key(agent_key, "에이전트 키")
    names = [_require_key(s, "MCP 서버 이름") for s in servers]
    path = Path(config_path)

    data = read_conf_file(path)
    agent = _entry(data, "agents", key, path)
    agent["allowed_mcp_servers"] = names

    write_conf_file(path, data)
    reload_config_if_active(path)


# ---------------------------------------------------------------------------
# agents.* 추가 / 끄기 / 삭제
#
# 화면은 새 에이전트를 만들 때 llm 기본값을 미리 채워 보여주지만, **사용자가
# 실제로 바꾼 값만** 파일에 적습니다. 화면이 보는 값은 이미 환경변수가 풀린
# 값이라, 그대로 되쓰면 해석된 API 키가 설정 파일에 평문으로 박히고 .env 를
# 바꿔도 따라오지 않게 됩니다. 손대지 않은 항목은 아예 적지 않아 llm 을 그대로
# 상속합니다.
# ---------------------------------------------------------------------------

# 화면에서 채울 수 있고 llm 에서 상속되는 항목. 여기 적힌 순서가 파일에 적히는
# 순서입니다.
AGENT_OVERRIDE_FIELDS = (
    "model",
    "api_base",
    "api_key",
    "api_version",
    "provider",
    "temperature",
    "top_p",
    "max_tokens",
    "max_context_window",
    "timeout",
    "num_retries",
    "drop_params",
    "max_tool_iterations",
    "max_continuations",
)

SEQUENTIAL_THINKING_FIELDS = ("enabled", "mode", "max_steps", "show_steps")

# 겉모습은 llm 에서 상속되지 않습니다 (에이전트마다 달라야 의미가 있습니다).
# 그래서 `AGENT_OVERRIDE_FIELDS` 와 섞지 않고 따로 둡니다.
AGENT_APPEARANCE_FIELDS = ("card_color", "icon")


def agent_defaults_from_llm(llm: Optional[LLMConfig] = None) -> Dict[str, Any]:
    """새 에이전트 폼에 미리 채울 값.

    llm 에 값이 있으면 그것을, 없으면 `AgentConfig` 의 기본값을 씁니다. 곧
    "아무것도 적지 않은 에이전트가 실제로 갖게 될 값" 입니다. 이 값과 같은 입력은
    `prune_agent_overrides()` 가 걷어내어 파일에 적지 않습니다.
    """
    source = llm if llm is not None else get_config().llm
    defaults: Dict[str, Any] = {}
    for field in AGENT_OVERRIDE_FIELDS:
        value = getattr(source, field, None)
        if _is_blank(value):
            value = AgentConfig.model_fields[field].get_default(call_default_factory=True)
        defaults[field] = value
    return defaults


def _same_value(a: Any, b: Any) -> bool:
    """화면 입력값과 기본값이 같은 값인지. 폼은 숫자도 문자열로 돌려줍니다."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) is bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a).strip() == str(b).strip()


def prune_agent_overrides(
    values: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """기본값과 같거나 비어 있는 항목을 걷어냅니다.

    남은 것만 `agents.<key>` 에 적히고, 걷어낸 항목은 llm 에서 상속됩니다.
    """
    defaults = defaults if defaults is not None else agent_defaults_from_llm()
    pruned: Dict[str, Any] = {}
    for field in AGENT_OVERRIDE_FIELDS:
        if field not in values:
            continue
        value = values[field]
        if _is_blank(value):
            continue  # 빈 칸은 "지정하지 않음" 입니다
        default = defaults.get(field)
        if not _is_blank(default) and _same_value(value, default):
            continue
        pruned[field] = value
    return pruned


def add_agent_to_conf_file(
    agent_key: str,
    name: str,
    role: str,
    system_prompt: str = "",
    allowed_mcp_servers: Optional[List[str]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    sequential_thinking: Optional[Dict[str, Any]] = None,
    debate_stance: str = "neutral",
    card_color: Optional[str] = None,
    icon: Optional[str] = None,
    enabled: bool = True,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents` 에 새 에이전트를 추가합니다.

    `overrides` 에는 `prune_agent_overrides()` 를 지난 값만 넘기세요. 여기 없는
    항목은 파일에 적히지 않고 llm 에서 상속됩니다.
    """
    key = _require_key(agent_key, "에이전트 키")
    path = Path(config_path)

    name = (name or "").strip()
    role = (role or "").strip()
    if not name:
        raise ValueError("에이전트 이름은 비워 둘 수 없습니다.")
    if not role:
        raise ValueError("에이전트 역할은 비워 둘 수 없습니다.")

    servers = [
        _require_key(s, "MCP 서버 이름")
        for s in (allowed_mcp_servers or [])
        if s and s.strip()
    ]

    overrides = dict(overrides or {})
    unknown = [k for k in overrides if k not in AGENT_OVERRIDE_FIELDS]
    if unknown:
        raise ValueError(f"모르는 설정 항목입니다: {', '.join(sorted(unknown))}")

    thinking = dict(sequential_thinking or {})
    unknown_st = [k for k in thinking if k not in SEQUENTIAL_THINKING_FIELDS]
    if unknown_st:
        raise ValueError(f"모르는 단계적 사고 항목입니다: {', '.join(sorted(unknown_st))}")

    if debate_stance not in DEBATE_STANCES:
        raise ValueError(
            f"'{debate_stance}' 는 쓸 수 없는 진영입니다. "
            f"{' | '.join(DEBATE_STANCES)} 중에서 고르세요."
        )

    data = read_conf_file(path)
    agents = _section(data, "agents")
    if key in agents:
        raise ValueError(f"'{key}' 에이전트가 이미 {path.name} 에 있습니다.")

    block: Dict[str, Any] = {"name": name, "role": role}
    if not enabled:
        block["enabled"] = False
    for field, value in (("card_color", card_color), ("icon", icon)):
        text = (value or "").strip()
        if text:
            block[field] = text
    for field in AGENT_OVERRIDE_FIELDS:
        if field in overrides:
            block[field] = _scalar_value(field, overrides[field])
    if debate_stance != "neutral":
        block["debate_stance"] = debate_stance
    block["allowed_mcp_servers"] = servers
    if (system_prompt or "").strip():
        block["system_prompt"] = _text_value(system_prompt)
    if thinking:
        block["sequential_thinking"] = {
            field: _scalar_value(field, thinking[field])
            for field in SEQUENTIAL_THINKING_FIELDS
            if field in thinking
        }

    agents[key] = block

    write_conf_file(path, data)
    reload_config_if_active(path)


def set_agent_enabled_in_conf_file(
    agent_key: str,
    enabled: bool,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.enabled` 를 바꿉니다.

    끈 에이전트는 풀에 등록되지 않아 어떤 대화에도 참여하지 않습니다. 삭제와 다른
    점은 설정과 프롬프트가 파일에 그대로 남아 언제든 되살릴 수 있다는 것입니다.
    """
    if agent_key == "orchestrator" and not enabled:
        raise ValueError(
            "오케스트레이터는 끌 수 없습니다. 토론 진행과 최종 합성을 맡습니다."
        )
    key = _require_key(agent_key, "에이전트 키")
    path = Path(config_path)

    data = read_conf_file(path)
    agent = _entry(data, "agents", key, path)
    agent["enabled"] = bool(enabled)

    write_conf_file(path, data)
    reload_config_if_active(path)


def remove_agent_from_conf_file(
    agent_key: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>` 를 통째로 지웁니다 (하위 sequential_thinking 블록 포함).

    그 위에 붙은 `//` 설명은 남깁니다. 사람이 쓴 글을 지우는 것은 되돌릴 수 없기
    때문입니다 (MCP 서버 삭제와 같은 규칙).
    """
    if agent_key == "orchestrator":
        raise ValueError(
            "오케스트레이터는 삭제할 수 없습니다. 토론 진행과 최종 합성을 맡습니다."
        )
    key = _require_key(agent_key, "에이전트 키")
    path = Path(config_path)

    data = read_conf_file(path)
    _entry(data, "agents", key, path)   # 없으면 KeyError
    del data["agents"][key]

    write_conf_file(path, data)
    reload_config_if_active(path)


def set_agent_debate_order_in_conf_file(
    order: List[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """화면에서 정한 발언 순서를 각 `agents.<key>.debate_priority` 에 적습니다.

    `order` 에 담긴 차례대로 10, 20, 30... 을 매깁니다. 사이에 자리를 남기는 것은
    나중에 한 명을 둘 사이에 끼워 넣을 때 나머지를 다시 쓰지 않기 위해서입니다.

    `order` 에 없는 에이전트는 건드리지 않습니다. 로스터가 늘 전원을 넘기지만,
    이 함수만 놓고 보면 "모르는 에이전트의 값을 지우지 않는다" 가 맞습니다.
    """
    keys = [_require_key(key, "에이전트 키") for key in order]
    path = Path(config_path)

    data = read_conf_file(path)
    agents = data.get("agents")
    agents = agents if isinstance(agents, dict) else {}
    missing = [key for key in keys if not isinstance(agents.get(key), dict)]
    if missing:
        raise KeyError(f"{path.name} 에 없는 에이전트입니다: {', '.join(missing)}")

    for position, key in enumerate(keys):
        agents[key]["debate_priority"] = (position + 1) * DEBATE_PRIORITY_STEP

    write_conf_file(path, data)
    reload_config_if_active(path)


def set_agent_debate_stance_in_conf_file(
    agent_key: str,
    stance: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.debate_stance` 를 바꿉니다 (디베이트 전략의 진영)."""
    if stance not in DEBATE_STANCES:
        raise ValueError(
            f"'{stance}' 는 쓸 수 없는 진영입니다. {' | '.join(DEBATE_STANCES)} 중에서 고르세요."
        )
    key = _require_key(agent_key, "에이전트 키")
    path = Path(config_path)

    data = read_conf_file(path)
    agent = _entry(data, "agents", key, path)
    agent["debate_stance"] = stance

    write_conf_file(path, data)
    reload_config_if_active(path)
