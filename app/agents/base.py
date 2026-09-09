import logging
import zlib
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote
from pydantic import BaseModel, Field
from app.config import (
    DEFAULT_DEBATE_PRIORITY,
    PROJECT_ROOT,
    AgentConfig,
    SequentialThinkingConfig,
    resolve_agent_icon,
)

logger = logging.getLogger(__name__)

# Color and avatar mappings for UI styling
AGENT_STYLE_MAP: Dict[str, Dict[str, str]] = {
    "orchestrator": {"avatar": "forum", "color": "indigo-8", "badge_color": "#3f51b5"},
    "architect": {"avatar": "account_tree", "color": "teal-8", "badge_color": "#009688"},
    "coder": {"avatar": "code", "color": "deep-purple-8", "badge_color": "#673ab7"},
    "critic": {"avatar": "security", "color": "amber-9", "badge_color": "#ff8f00"},
    "user": {"avatar": "chat_bubble", "color": "blue-grey-8", "badge_color": "#607d8b"},
}

DEFAULT_STYLE = {"avatar": "smart_toy", "color": "primary", "badge_color": "#1976d2"}

# 화면에서 추가한 에이전트는 이 표에 없습니다. 전부 같은 회색 로봇으로 나오면
# 토론 피드에서 누가 말하는지 색으로 구분할 수 없으므로, 키에서 색을 하나
# 골라 줍니다. 키가 같으면 언제 어느 프로세스에서 보든 같은 색이어야 해서
# (파이썬의 문자열 hash 는 실행마다 달라집니다) crc32 를 씁니다.
CUSTOM_STYLE_PALETTE: List[Dict[str, str]] = [
    {"avatar": "psychology", "color": "cyan-8", "badge_color": "#0097a7"},
    {"avatar": "insights", "color": "pink-8", "badge_color": "#c2185b"},
    {"avatar": "science", "color": "light-green-8", "badge_color": "#689f38"},
    {"avatar": "travel_explore", "color": "orange-9", "badge_color": "#ef6c00"},
    {"avatar": "gavel", "color": "brown-7", "badge_color": "#6d4c41"},
    {"avatar": "diversity_3", "color": "deep-orange-8", "badge_color": "#e64a19"},
]


# 화면에서 고를 수 있는 카드 색. 값은 그대로 conf.json 의 `card_color` 가 되고,
# NiceGUI 는 '#' 으로 시작하는 값을 CSS 색으로 그대로 씁니다 — 그래서 팔레트를
# 벗어난 색을 직접 골라도 같은 경로로 흐릅니다.
CARD_COLOR_CHOICES: List[Dict[str, str]] = [
    {"label": "인디고", "hex": "#3f51b5"},
    {"label": "청록", "hex": "#009688"},
    {"label": "보라", "hex": "#673ab7"},
    {"label": "호박", "hex": "#ff8f00"},
    {"label": "시안", "hex": "#0097a7"},
    {"label": "자홍", "hex": "#c2185b"},
    {"label": "연두", "hex": "#689f38"},
    {"label": "주황", "hex": "#ef6c00"},
    {"label": "갈색", "hex": "#6d4c41"},
    {"label": "진홍", "hex": "#e64a19"},
    {"label": "청회색", "hex": "#607d8b"},
    {"label": "파랑", "hex": "#1976d2"},
]

# 화면에서 고를 수 있는 머티리얼 아이콘. 그림을 올리지 않는 경우의 선택지입니다.
ICON_CHOICES: List[str] = [
    "forum", "account_tree", "code", "security", "psychology", "insights",
    "science", "travel_explore", "gavel", "diversity_3", "smart_toy", "biotech",
    "calculate", "design_services", "engineering", "fact_check", "hub",
    "lightbulb", "manage_search", "policy", "query_stats", "rocket_launch",
    "school", "support_agent", "terminal", "verified",
]

# 표에 적힌 Quasar 색 이름 -> 실제 색. 테두리처럼 CSS 로 직접 칠해야 하는 자리에
# 씁니다 (Quasar 이름은 클래스라서 인라인 스타일에 넣을 수 없습니다).
QUASAR_HEX: Dict[str, str] = {
    style["color"]: style["badge_color"]
    for style in list(AGENT_STYLE_MAP.values()) + CUSTOM_STYLE_PALETTE + [DEFAULT_STYLE]
}

# 올린 그림을 화면에 넘기는 길. 파일을 못 찾으면 이 라우트가 기본 아이콘을
# 대신 내려주므로, 아바타가 깨진 이미지로 남지 않습니다.
ICON_ROUTE = "/agent-icon"

def icon_url(path_value: str) -> str:
    """conf.json 에 적힌 아이콘 경로를 브라우저가 받을 수 있는 주소로 바꿉니다."""
    return f"{ICON_ROUTE}?src={quote(str(path_value), safe='')}"


def _avatar_value(key: str, icon: Optional[str], fallback: str) -> str:
    """`ui.avatar()` 에 넘길 값. 그림이면 'img:...', 아니면 머티리얼 아이콘 이름.

    폴백이 여기 한 자리에 모여 있습니다. 그림을 지정했는데 파일이 없으면 —
    지워졌든, 경로를 잘못 적었든, 설정만 들고 다른 PC 로 옮겼든 — 키에서 정해지는
    원래 아이콘으로 조용히 물러섭니다. 화면이 깨지는 것보다 낫습니다.
    """
    raw = (icon or "").strip()
    if not raw:
        return fallback

    resolved = resolve_agent_icon(raw)
    if resolved is not None:
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError:
            # 프로젝트 밖의 파일은 내려주지 않습니다 (아이콘 경로가 임의의 파일을
            # 읽는 통로가 되지 않게). 기본 아이콘으로 물러섭니다.
            logger.warning(
                "Agent '%s' icon '%s' is outside the project folder; using the default icon.",
                key, raw,
            )
            return fallback
        return f"img:{icon_url(raw)}"

    if "/" in raw or "\\" in raw or "." in raw:
        # 그림을 가리키려던 값인데 풀리지 않았습니다. 머티리얼 아이콘 이름으로는
        # 쓸 수 없으므로 (아바타가 빈 칸이 됩니다) 기본 아이콘을 씁니다.
        logger.warning("Agent '%s' icon '%s' could not be found; using the default icon.", key, raw)
        return fallback

    return raw  # 머티리얼 아이콘 이름


def style_for_agent(
    key: str, card_color: Optional[str] = None, icon: Optional[str] = None
) -> Dict[str, str]:
    """에이전트 카드에 쓰이는 아바타/색.

    `card_color` 와 `icon` 은 conf.json 에서 사람이 정한 값입니다. 주지 않으면
    예전과 같이 키에서 정해집니다 — 표에 있는 키는 표에서, 나머지는 팔레트에서.
    """
    base = AGENT_STYLE_MAP.get(key)
    if base is None:
        base = (
            DEFAULT_STYLE if not key
            else CUSTOM_STYLE_PALETTE[zlib.crc32(key.encode("utf-8")) % len(CUSTOM_STYLE_PALETTE)]
        )

    style = dict(base)
    style["avatar"] = _avatar_value(key, icon, base["avatar"])

    chosen = (card_color or "").strip()
    if chosen:
        style["color"] = chosen
        style["badge_color"] = QUASAR_HEX.get(chosen, chosen)
    return style


class Agent(BaseModel):
    key: str
    name: str
    role: str
    enabled: bool = True
    model: str = "openai/gpt-4o"
    api_key: Optional[str] = ""
    api_base: Optional[str] = None
    api_version: Optional[str] = None
    provider: Optional[str] = None
    temperature: float = 0.7
    top_p: Optional[float] = None
    max_tokens: int = 4096
    max_context_window: int = 128000
    timeout: Optional[float] = None
    num_retries: int = 0
    drop_params: bool = True
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    max_tool_iterations: int = 30
    max_continuations: int = 2
    allowed_mcp_servers: List[str] = Field(default_factory=list)
    # 토론에서의 자리. 전략이 이 값으로 순서와 진영을 정합니다 (에이전트 키를
    # 문자열로 박아 두던 방식을 대신합니다).
    debate_priority: int = DEFAULT_DEBATE_PRIORITY
    debate_stance: Literal["proponent", "critic", "neutral"] = "neutral"
    sequential_thinking: SequentialThinkingConfig = Field(default_factory=SequentialThinkingConfig)
    system_prompt: str = ""
    # conf.json 에 적힌 그대로의 겉모습. 되쓰거나 스냅샷에 담을 때 이 값을 씁니다.
    card_color: Optional[str] = None
    icon: Optional[str] = None
    # 위 두 값을 화면이 바로 쓸 수 있게 푼 것. `avatar` 는 머티리얼 아이콘 이름
    # 이거나 'img:...' 이고, `badge_color` 는 테두리처럼 CSS 로 칠하는 자리용
    # 실제 색입니다.
    avatar: str = "forum"
    color: str = "primary"
    badge_color: str = "#1976d2"

    @property
    def has_custom_appearance(self) -> bool:
        """사람이 색이나 아이콘을 직접 정했는지. 테두리를 칠할지 가릅니다."""
        return bool((self.card_color or "").strip() or (self.icon or "").strip())

    @property
    def is_live(self) -> bool:
        """True when the agent has enough connection info to reach a real LLM endpoint.

        False 면 발언 차례에 `LLMUnavailableError` 가 올라옵니다. 대체 응답은 없습니다.
        """
        if self.api_base:
            return True
        if self.api_key and self.api_key.strip():
            return True
        return self.model.split("/", 1)[0] in {"ollama", "ollama_chat", "lm_studio"}

    @property
    def endpoint_label(self) -> str:
        """Short human-readable endpoint description for the UI."""
        if self.api_base:
            return self.api_base
        return "provider default endpoint" if self.is_live else "no endpoint configured"

    @classmethod
    def from_config(cls, key: str, cfg: AgentConfig) -> "Agent":
        style = style_for_agent(key, cfg.card_color, cfg.icon)
        return cls(key=key, **cfg.model_dump(), **style)
