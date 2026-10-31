"""설정: conf.json 을 읽고, 검증하고, 화면에서 고친 값을 되씁니다.

conf.json 은 JSON 이라 표준 라이브러리 `json` 만으로 읽고 씁니다. JSON 에 없는 두 가지는 규칙으로 채웁니다 —
`//` 로 시작하는 키는 설명이라 읽을 때 걷어 내고, `system_prompt` 같은 여러 줄 글은 문자열 배열로 적으면
줄바꿈으로 잇습니다. `${VAR}` · `${VAR:-기본값}` 은 환경 변수로 풉니다.

- `env` — `.env` 읽기와 환경 변수 치환 (이 패키지를 부르면 가장 먼저 돕니다)
- `paths` — 프로젝트 루트, 데이터 폴더, 작업 공간, 에이전트 아이콘
- `schema` — 설정의 모양 (Pydantic)
- `loader` — 파일 읽기·쓰기와 설정 싱글턴 `get_config`
- `editors` — 에이전트·MCP 서버·스킬·도구 보안 규칙을 conf.json 에 되쓰기

여기서 내보내는 이름이 이 패키지의 공개 API 입니다. 앱 안의 모듈은 이 이름으로 가져다 씁니다.
"""

from app.config.env import join_text_lines, resolve_env_vars
from app.config.paths import (
    DATA_DIR,
    DEFAULT_CONFIG_PATH,
    ICON_EXTENSIONS,
    MAX_ICON_BYTES,
    PROJECT_ROOT,
    resolve_agent_icon,
    resolve_workspace_dir,
    store_agent_icon,
    strip_extended_path_prefix,
)
from app.config.schema import (
    BARE_KEY_PATTERN,
    DEBATE_STANCES,
    DEFAULT_DEBATE_PRIORITY,
    DEFAULT_PEER_TOOL_ITERATIONS,
    DEFAULT_SUB_DEBATE_ROUNDS,
    MAX_SUB_DEBATE_ROUNDS,
    ORCHESTRATOR_KEY,
    PEERS_TOOL_PREFIX,
    ASK_TOOL_PREFIX,
    SKILL_TOOL_PREFIX,
    TOOL_ITERATION_CEILING,
    AgentConfig,
    AppConfig,
    LLMConfig,
    MCPServerConfig,
    PeerConsultationConfig,
    PlanApprovalConfig,
    RootConfig,
    SequentialThinkingConfig,
    TrialConfig,
    clamp_sub_debate_rounds,
)
from app.config.loader import (
    active_config_path,
    get_config,
    load_config,
    read_conf_file,
    strip_comment_keys,
    write_conf_file,
)
from app.config.editors import (
    SKILL_NAME_PATTERN,
    add_agent_to_conf_file,
    add_mcp_server_to_conf_file,
    add_tool_security_rules_to_conf_file,
    agent_defaults_from_llm,
    prune_agent_overrides,
    remove_agent_from_conf_file,
    remove_mcp_server_from_conf_file,
    set_agent_allowed_mcp_servers_in_conf_file,
    set_agent_allowed_skills_in_conf_file,
    set_agent_debate_order_in_conf_file,
    set_agent_debate_stance_in_conf_file,
    set_agent_enabled_in_conf_file,
    set_agent_peer_consultation_in_conf_file,
    set_mcp_server_enabled_in_conf_file,
    set_skill_enabled_in_conf_file,
    update_agent_appearance_in_conf_file,
    update_agent_persona_in_conf_file,
)

__all__ = [
    "BARE_KEY_PATTERN",
    "DATA_DIR",
    "DEBATE_STANCES",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DEBATE_PRIORITY",
    "DEFAULT_PEER_TOOL_ITERATIONS",
    "DEFAULT_SUB_DEBATE_ROUNDS",
    "ICON_EXTENSIONS",
    "MAX_ICON_BYTES",
    "MAX_SUB_DEBATE_ROUNDS",
    "clamp_sub_debate_rounds",
    "ORCHESTRATOR_KEY",
    "PEERS_TOOL_PREFIX",
    "ASK_TOOL_PREFIX",
    "PROJECT_ROOT",
    "SKILL_NAME_PATTERN",
    "SKILL_TOOL_PREFIX",
    "TOOL_ITERATION_CEILING",
    "AgentConfig",
    "AppConfig",
    "LLMConfig",
    "MCPServerConfig",
    "PeerConsultationConfig",
    "PlanApprovalConfig",
    "RootConfig",
    "SequentialThinkingConfig",
    "TrialConfig",
    "active_config_path",
    "add_agent_to_conf_file",
    "add_mcp_server_to_conf_file",
    "add_tool_security_rules_to_conf_file",
    "agent_defaults_from_llm",
    "get_config",
    "join_text_lines",
    "load_config",
    "prune_agent_overrides",
    "read_conf_file",
    "remove_agent_from_conf_file",
    "remove_mcp_server_from_conf_file",
    "resolve_agent_icon",
    "resolve_env_vars",
    "resolve_workspace_dir",
    "set_agent_allowed_mcp_servers_in_conf_file",
    "set_agent_allowed_skills_in_conf_file",
    "set_agent_debate_order_in_conf_file",
    "set_agent_debate_stance_in_conf_file",
    "set_agent_enabled_in_conf_file",
    "set_agent_peer_consultation_in_conf_file",
    "set_mcp_server_enabled_in_conf_file",
    "set_skill_enabled_in_conf_file",
    "store_agent_icon",
    "strip_comment_keys",
    "strip_extended_path_prefix",
    "update_agent_appearance_in_conf_file",
    "update_agent_persona_in_conf_file",
    "write_conf_file",
]
