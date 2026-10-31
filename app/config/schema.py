"""conf.json 의 모양 (Pydantic): 앱·LLM·에이전트·MCP 서버·도구 보안·계획 승인·체험·스킬 설정과 그 기본값.
"""

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config.env import join_text_lines, resolve_env_vars
from app.tool_rules import parse_rules


DEFAULT_SEQUENTIAL_THINKING_PROMPT = """[Sequential Thinking Protocol]
최종 답변을 작성하기 전에, 문제를 최대 {max_steps}단계로 나누어 순차적으로 사고하세요.
- `Thought 1..N` 형식으로 각 단계를 명시하고, 각 단계에서는 직전 단계의 결론을 근거로 다음 논리를 전개합니다.
- 중간에 가정이 틀렸다고 판단되면 `Revision of Thought K:` 로 해당 단계를 수정한 뒤 진행하세요.
- 도구(MCP)가 필요한 단계에서는 먼저 도구를 호출해 사실을 검증한 후 다음 단계로 넘어가세요.
- 사고가 끝나면 `---` 구분선 뒤에 `## 최종 결론`을 두고, 그 아래에는 사고 과정이 아닌 결과물만 작성하세요."""

class AppConfig(BaseModel):
    host: str = Field(default="127.0.0.1", description="Application Host")
    port: int = Field(default=8000, description="Application Port")
    db_url: str = Field(
        default="sqlite+aiosqlite:///./multiagent.db",
        description="Async SQLAlchemy database URL",
    )
    debug: bool = Field(default=True, description="Debug mode")
    # 서버 전체에서 LLM 에 동시에 보내는 요청의 상한. 0 이면 제한하지 않습니다.
    #
    # `parallel_limit` 은 대화 하나 안의 상한이라, 대화가 여럿이면 곱으로 늘어납니다.
    # 단일 GPU 런타임(Ollama·vLLM)이나 여러 사람이 함께 쓰는 서버에서는 이 값으로
    # 전체를 묶습니다. 넘는 요청은 버리지 않고 온 순서대로 기다립니다 (`app/agents/llm_gate.py`).
    llm_concurrency: int = Field(default=0, ge=0, description="Max concurrent LLM requests (0 = unlimited)")

    @field_validator("port", mode="before")
    @classmethod
    def _coerce_port(cls, v: Any) -> int:
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v else 8000
        return int(v)

    @field_validator("llm_concurrency", mode="before")
    @classmethod
    def _coerce_concurrency(cls, v: Any) -> int:
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v else 0
        return int(v or 0)

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

def is_blank(value: Any) -> bool:
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

# 에이전트가 파일 내용을 직접 받을 수 있는 입력 형식 (`AgentConfig.allowed_modalities`, ADR-035).
INPUT_MODALITIES = ("image", "audio", "video", "document")

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

def validate_rule_list(value: Any) -> List[str]:
    """규칙 문자열 목록을 검증합니다. 틀린 줄이 있으면 전부 모아 알립니다."""

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
    스냅샷으로 고정됩니다(`session_agents.config_snapshot`). 보안은 고정되면 안 됩니다 —
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
        return validate_rule_list(v)

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
        return validate_rule_list(v)

class PlanApprovalConfig(BaseModel):
    """계획 승인 설정 (`plan_approval`, ADR-028).

    켜면 오케스트레이터의 계획(0라운드)이 나온 뒤 토론을 시작하기 **전에** 사람의 승인을
    받습니다. 사람은 승인 카드에서 전문가별 태스크를 직접 고치거나, 의견을 적어 계획을 다시
    쓰게 하거나, 요청을 되돌릴 수 있습니다. 도구 보안처럼 **지금의** 설정을 읽으므로 진행
    중인 대화에도 다음 턴부터 걸립니다.

    답할 사람이 없는 실행(화면 없이 엔진만 부른 경우)에는 걸리지 않습니다 — 계획 자체는
    아무것도 실행하지 않고, 위험한 도구 호출은 도구 보안이 따로 막습니다.
    """

    enabled: bool = Field(
        default=True,
        description="계획이 나온 뒤 토론을 시작하기 전에 사람의 승인을 받습니다.",
    )
    timeout: float = Field(
        default=600.0, ge=30, le=86400,
        description=(
            "승인 카드에 답이 없으면 이 시간(초)이 지나 턴을 멈춰 둡니다. 아무것도 실행하지 "
            "않으며, '이어서 진행' 을 누르면 같은 계획으로 승인 카드가 다시 열립니다."
        ),
    )

class LLMFallbackConfig(BaseModel):
    """개별 모델의 연결이 실패하면 전역 기본 연결로 물러서는 설정 (`llm.fallback`, ADR-030).

    기본은 꺼져 있습니다. 조용히 다른 모델이 답하는 일이 기본이 되어서는 안 되고, 개별 모델이
    사내 모델이고 전역이 외부 API 인 배치에서는 폴백이 곧 대화 내용이 밖으로 나가는 길이기 때문입니다.
    """

    enabled: bool = Field(default=False, description="Fall back to the global llm connection on a connection failure")
    retry_after: float = Field(
        default=60.0, ge=0,
        description="Seconds a failed connection is skipped before it is tried again (0 = try it every time)",
    )

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
    # 에이전트가 물려받는 값이 아닙니다 (`INHERITABLE_LLM_FIELDS` 에 없습니다). 전역 연결 그 자체에 딸린 설정입니다.
    fallback: LLMFallbackConfig = Field(default_factory=LLMFallbackConfig)

# 동료 자문(peers)에서 자문받는 에이전트가 쓰는 도구 호출 상한의 기본값.
DEFAULT_PEER_TOOL_ITERATIONS = 3

# 서브 토론 한 번의 라운드 수 (ADR-034). 본 토론의 `max_rounds` 와 별개입니다. 한 라운드는 두 전문가가 한 번씩 말하는 것입니다.
DEFAULT_SUB_DEBATE_ROUNDS = 2
MAX_SUB_DEBATE_ROUNDS = 5


def clamp_sub_debate_rounds(value: Any) -> int:
    """서브 토론 라운드 수를 쓸 수 있는 범위로 옮깁니다. 값이 없으면 기본값입니다."""
    if value is None:
        return DEFAULT_SUB_DEBATE_ROUNDS
    return max(1, min(MAX_SUB_DEBATE_ROUNDS, int(value)))


class PeerConsultationConfig(BaseModel):
    """동료 자문(peers, app/agents/consultation.py) 설정. `allowed_mcp_servers` 에 "peers" 가 있을 때만 쓰입니다.

    이 에이전트가 **묻는 쪽**일 때의 설정입니다. 대화가 시작되면 다른 에이전트 설정과 함께 그 대화의
    스냅샷(`session_agents.config_snapshot`)에 고정되므로, 대화마다 다를 수 있습니다.
    """

    model_config = ConfigDict(extra="forbid")

    # 자문할 수 있는 에이전트 키. None 이면 이번 턴에 참여한 다른 전문가 전원입니다. 목록이면 거기
    # 적힌 에이전트만 — 이번 토론에 참여하지 않는 전문가도 자문 상대가 될 수 있습니다.
    targets: Optional[List[str]] = Field(
        default=None, description="Agent keys this agent may consult; null = every other participating specialist",
    )
    # 자문받는 에이전트가 답하는 동안 쓰는 도구 호출 상한. 0 이면 도구 없이 답합니다. 그 에이전트
    # 자신의 `max_tool_iterations` 를 넘지는 않습니다.
    max_tool_iterations: int = Field(
        default=DEFAULT_PEER_TOOL_ITERATIONS, ge=0, le=TOOL_ITERATION_CEILING,
        description="Tool-loop cap for the consulted agent while it answers (0 = no tools)",
    )

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
    # 이 에이전트가 불러 사용할 수 있는 스킬 목록입니다 (`app/agents/skills.py`). 도구 서버 할당과 동일한
    # 에이전트 설정이므로 대화 스냅샷에 함께 보존됩니다. 스킬 내용과 활성화/비활성화 상태는 스냅샷에 고정되지 않습니다.
    allowed_skills: List[str] = Field(
        default_factory=list, description="Skill names (folders under skills.dir) this agent can load"
    )
    # 입력창에서 @언급한 파일과 도구가 돌려준 파일 중 이 에이전트가 내용을 직접 받을 형식 (`app/agents/media.py`).
    # 비어 있으면 글과 파일 경로만 받습니다. 도구 서버 할당처럼 대화 스냅샷에 함께 고정됩니다.
    allowed_modalities: List[Literal["image", "audio", "video", "document"]] = Field(
        default_factory=list,
        description="Input formats delivered to this agent's model as content: image | audio | video | document",
    )
    peer_consultation: PeerConsultationConfig = Field(default_factory=PeerConsultationConfig)
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
    # `llm.fallback` 이 켜져 있어도 이 에이전트만은 전역 기본 모델로 물러서지 않게 합니다. 이 에이전트의
    # 프롬프트는 정해 둔 모델로만 보내야 할 때 씁니다 (사내 모델 전용 자료, 미세조정 모델의 역할 등).
    allow_fallback: bool = Field(
        default=True, description="Allow llm.fallback to answer for this agent when its own connection fails"
    )
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

class TrialConfig(BaseModel):
    """체험 서버 (`trial`). 켜면 원격 방문자가 `/trial` 에서 템플릿으로 토론을 돌려 봅니다.

    주인(루프백 또는 접속 토큰)은 지금처럼 모든 화면을 씁니다. 방문자는 이름과 PIN 으로
    들어와 자기 대화만 보고, 도구 없이 템플릿의 참여자로만 토론합니다 (`app/trial/`).
    """

    enabled: bool = Field(default=False)
    title: str = Field(default="MADO 체험")
    # 공식 템플릿 폴더. 상대 경로면 프로젝트 루트 기준입니다.
    templates_dir: str = Field(default="trial_templates")
    # 모든 체험 화면 아래에 붙는 안내. 운영자가 대화를 볼 수 있다는 사실을 숨기지 않습니다.
    notice: str = Field(default="운영자가 대화 내용을 볼 수 있습니다. 대외비 문서는 올리지 마세요.")
    pin_min_length: int = Field(default=4, ge=4, le=32)
    # 시작 양식 한 칸에 넣을 수 있는 글자 수와 올릴 수 있는 텍스트 파일 크기.
    max_input_chars: int = Field(default=30000, ge=1000)
    max_upload_kb: int = Field(default=200, ge=1, le=5000)
    # 방문자가 고친 템플릿이 가질 수 있는 토론 횟수의 상한.
    max_rounds: int = Field(default=4, ge=1, le=10)

class SkillsConfig(BaseModel):
    """스킬 설정(`skills`)입니다. 스킬 하나는 폴더 하나에 대응합니다 — `<dir>/<이름>/SKILL.md` (`app/agents/skills.py`).

    스킬 목록 자체는 설정 파일에 기록하지 않습니다. 폴더를 추가하거나 삭제하는 것이 곧 설치 및 삭제이며, 설정 파일에는
    스킬 폴더 경로와 비활성화할 스킬 목록만 관리합니다. 두 설정 모두 **진행 중인 대화 세션에도 다음 발언부터**
    즉시 적용됩니다 — MCP 서버 활성화/비활성화처럼 대화 스냅샷에 고정하지 않습니다.
    """

    # 스킬 폴더 경로입니다. 상대 경로인 경우 프로젝트 루트 기준입니다.
    dir: str = Field(default="skills")
    # 비활성화할 스킬의 이름 목록입니다. 목록에 없는 스킬은 활성화 상태로 유지되며, 폴더를 추가하면 즉시 사용할 수 있습니다.
    disabled: List[str] = Field(default_factory=list)

# 거의 모든 발언이 쓰는 도구. 나머지는 한 줄 목록으로만 보입니다. `read_text_file` 은 지침이 `edit_file` 과 함께
# 짚지만, 읽기 전용 모드처럼 고치는 도구가 빠진 발언에서도 있어야 하므로 여기에도 둡니다.
DEFAULT_CORE_TOOLS = (
    "read_text_file", "list_directory", "search_files", "add_observations", "execute_python_code",
    "list_workspace_files", "git_status", "git_diff_unstaged",
)

class ToolLoadingConfig(BaseModel):
    """도구 스키마 지연 로딩 설정(`tool_loading`, `app/agents/tool_loading.py`, ADR-039)입니다.

    MCP 도구의 정의는 요청마다 입력 토큰으로 나갑니다. 켜 두면 자주 쓰는 도구의 정의만 싣고, 나머지는
    `tools__load_tools` 설명의 한 줄 목록으로 보여 줍니다. 진행 중인 대화에도 다음 발언부터 적용합니다.
    """

    enabled: bool = Field(default=True, description="List rarely used MCP tools by one line instead of sending their schemas")
    core_tools: List[str] = Field(
        default_factory=lambda: list(DEFAULT_CORE_TOOLS),
        description="Tools whose schema is always sent: a tool name (git_log), a full name (git__git_log) or a server key (git)",
    )
    min_saving_tokens: int = Field(
        default=1000, ge=0,
        description="Send every schema unless the catalog saves at least this many tokens per request",
    )

class ToolOutputCompactionConfig(BaseModel):
    """도구 루프의 오래된 큰 파일 읽기 결과를 줄이는 설정(`tool_output_compaction`, `app/agents/tool_compaction.py`, ADR-040)입니다.

    모델이 읽는 메시지만 줄고 도구 카드와 DB 의 기록은 원문 그대로입니다. 진행 중인 대화에도 다음 판부터 적용합니다.
    """

    enabled: bool = Field(default=True, description="Replace stale large file reads in the tool loop with a short stub")
    stale_rounds: int = Field(
        default=3, ge=1,
        description="Tool rounds that must follow a file read before it may be compacted",
    )
    min_chars: int = Field(default=8000, ge=1000, description="File reads shorter than this are kept as they are")
    min_usage: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Compact only when the speech uses this share of its context budget, so the prompt cache survives until then",
    )

# 스킬 도구의 접두사입니다 (`skills__load_skill`). MCP 도구와 동일한 `서버__도구` 형태이므로,
# 동일한 이름의 MCP 서버가 등록되면 도구 이름 충돌이 발생합니다.
SKILL_TOOL_PREFIX = "skills"

# 동료 자문 도구의 접두사입니다 (`peers__consult_agent`, app/agents/consultation.py). MCP 서버가
# 아니지만 켜는 법이 `allowed_mcp_servers` 에 이 이름을 넣는 것이라, 같은 이름의 MCP 서버가
# 등록되면 도구 이름도 켜는 스위치도 겹칩니다.
PEERS_TOOL_PREFIX = "peers"

# 사용자 질문 도구 접두사입니다 (`ask__ask_user`, app/agents/ask_user.py). 세션별로 설정 가능한 내장 도구이므로
# MCP 서버로 등록하지 않으나, 동일한 이름의 MCP 서버 등록 시 도구명 충돌을 방지하기 위해 예약어로 지정합니다.
ASK_TOOL_PREFIX = "ask"

# 도구 불러오기의 접두사입니다 (`tools__load_tools`, app/agents/tool_loading.py). MCP 서버가 아니라 앱이
# 직접 처리하는 도구이므로, 동일한 이름의 MCP 서버 등록 시 도구명 충돌을 방지하기 위해 예약어로 지정합니다.
TOOL_LOADING_PREFIX = "tools"

# 필수 에이전트의 키. 토론을 이끌고 합성하는 오케스트레이터는 `agents` 에 이 키로 있어야 합니다.
ORCHESTRATOR_KEY = "orchestrator"

RESERVED_SERVER_NAMES = {
    SKILL_TOOL_PREFIX: f"스킬 도구 이름({SKILL_TOOL_PREFIX}__load_skill)",
    PEERS_TOOL_PREFIX: f"동료 자문 도구 이름({PEERS_TOOL_PREFIX}__consult_agent)",
    ASK_TOOL_PREFIX: f"사용자 질문 도구 이름({ASK_TOOL_PREFIX}__ask_user)",
    TOOL_LOADING_PREFIX: f"도구 불러오기 이름({TOOL_LOADING_PREFIX}__load_tools)",
}

class RootConfig(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=dict)
    agents: Dict[str, AgentConfig] = Field(default_factory=dict)
    tool_security: ToolSecurityConfig = Field(default_factory=ToolSecurityConfig)
    plan_approval: PlanApprovalConfig = Field(default_factory=PlanApprovalConfig)
    trial: TrialConfig = Field(default_factory=TrialConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    tool_loading: ToolLoadingConfig = Field(default_factory=ToolLoadingConfig)
    tool_output_compaction: ToolOutputCompactionConfig = Field(default_factory=ToolOutputCompactionConfig)

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
                if is_blank(llm_raw.get(field)):
                    continue
                group = next((g for g in _ALIAS_GROUPS if field in g), (field,))
                # An agent value that resolved to an empty string (unset env var) still inherits.
                if any(not is_blank(agent_raw.get(alias)) for alias in group):
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
        if ORCHESTRATOR_KEY not in self.agents:
            raise ValueError("Configuration must contain an \"agents.orchestrator\" definition.")
        if not self.agents[ORCHESTRATOR_KEY].enabled:
            raise ValueError("The \"agents.orchestrator\" agent cannot be disabled (enabled: false).")
        return self

    @model_validator(mode="after")
    def validate_reserved_server_names(self) -> "RootConfig":
        if SKILL_TOOL_PREFIX in self.mcp_servers:
            raise ValueError(
                f"\"mcp_servers.{SKILL_TOOL_PREFIX}\" cannot be used: '{SKILL_TOOL_PREFIX}__' is "
                f"the prefix of the built-in skill tools. Rename the MCP server."
            )
        if PEERS_TOOL_PREFIX in self.mcp_servers:
            raise ValueError(
                f"\"mcp_servers.{PEERS_TOOL_PREFIX}\" cannot be used: '{PEERS_TOOL_PREFIX}' is the "
                f"built-in peer consultation tool ({PEERS_TOOL_PREFIX}__consult_agent), switched on by "
                f"listing it in an agent's allowed_mcp_servers. Rename the MCP server."
            )
        if ASK_TOOL_PREFIX in self.mcp_servers:
            raise ValueError(
                f"\"mcp_servers.{ASK_TOOL_PREFIX}\" cannot be used: '{ASK_TOOL_PREFIX}__' is the prefix of the "
                f"built-in ask-the-user tool ({ASK_TOOL_PREFIX}__ask_user). Rename the MCP server."
            )
        if TOOL_LOADING_PREFIX in self.mcp_servers:
            raise ValueError(
                f"\"mcp_servers.{TOOL_LOADING_PREFIX}\" cannot be used: '{TOOL_LOADING_PREFIX}__' is the prefix of the "
                f"built-in tool catalog ({TOOL_LOADING_PREFIX}__load_tools). Rename the MCP server."
            )
        return self

    @property
    def enabled_agents(self) -> Dict[str, AgentConfig]:
        return {k: v for k, v in self.agents.items() if v.enabled}

    @property
    def enabled_mcp_servers(self) -> Dict[str, MCPServerConfig]:
        return {k: v for k, v in self.mcp_servers.items() if v.enabled}

# 에이전트 키와 MCP 서버 이름의 규칙. JSON 은 어떤 문자열이든 키로 받지만, 이
# 이름들은 도구 이름의 접두사로도 쓰이므로 단순해야 합니다.
BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
