"""화면에서 고친 값을 conf.json 에 되쓰기: 에이전트 추가·삭제·켜기·순서·진영·도구·스킬, MCP 서버, 도구 보안 규칙.

검증을 디스크에 쓰기 **전에** 하므로, 틀린 입력이 반쯤 쓰인 파일을 남기지 않습니다. 주석 키(`//`)와
`${VAR}` 는 그대로 보존합니다.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config.loader import get_config, read_conf_file, reload_config_if_active, write_conf_file
from app.config.paths import DEFAULT_CONFIG_PATH
from app.config.schema import (
    BARE_KEY_PATTERN,
    DEBATE_PRIORITY_STEP,
    DEBATE_STANCES,
    INPUT_MODALITIES,
    DEFAULT_PEER_TOOL_ITERATIONS,
    DEFAULT_TOOL_DENY_RULES,
    ORCHESTRATOR_KEY,
    RESERVED_SERVER_NAMES,
    TOOL_ITERATION_CEILING,
    AgentConfig,
    LLMConfig,
    is_blank,
    validate_rule_list,
)


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
    if name in RESERVED_SERVER_NAMES:
        raise ValueError(
            f"'{name}'는 {RESERVED_SERVER_NAMES[name]}으로 예약되어 있으므로 "
            f"MCP 서버 이름으로 사용할 수 없습니다."
        )
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
    wanted = validate_rule_list(rules)
    if not wanted:
        return []
    path = Path(config_path)

    data = read_conf_file(path)
    current = _tool_rule_list(data, effect, path)
    added = [r for r in wanted if r not in current]
    if not added:
        return []
    _section(data, "tool_security")[effect] = current + added

    write_conf_file(path, data)
    reload_config_if_active(path)
    return added

def _tool_rule_list(data: Dict[str, Any], effect: str, path: Path) -> List[str]:
    """파일의 `tool_security.<effect>` 를 설정이 읽는 모양 그대로 꺼냅니다.

    `deny` 가 파일에 없으면 기본 목록이 걸려 있는 것이므로 그 목록을 돌려줍니다. 더하든 지우든
    그 위에서 고쳐 적어야 나머지 기본 보호가 사라지지 않습니다.
    """
    if effect not in ("deny", "ask", "allow"):
        raise ValueError(f"'{effect}' 는 규칙 종류가 아닙니다 (deny · ask · allow).")
    section = data.get("tool_security")
    section = section if isinstance(section, dict) else {}
    if effect not in section:
        return list(DEFAULT_TOOL_DENY_RULES) if effect == "deny" else []
    current = section[effect]
    if current is not None and not isinstance(current, (list, str)):
        raise ValueError(f"{path.name} 의 tool_security.{effect} 가 목록이 아닙니다.")
    return validate_rule_list(current)

def remove_tool_security_rule_from_conf_file(
    effect: str,
    rule: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> bool:
    """`tool_security.<effect>` 에서 규칙 하나를 지웁니다. 지웠으면 True.

    화면이 들고 있는 목록을 통째로 되쓰지 않고 지금의 파일에서 한 줄만 고칩니다. 그 사이 승인
    카드의 "항상" 이나 손으로 고친 내용이 낡은 목록에 덮이지 않습니다. 이미 없으면 쓰지 않습니다.
    """
    path = Path(config_path)
    target = str(rule or "").strip()

    data = read_conf_file(path)
    current = _tool_rule_list(data, effect, path)
    if target not in current:
        return False
    _section(data, "tool_security")[effect] = [r for r in current if r != target]

    write_conf_file(path, data)
    reload_config_if_active(path)
    return True

def replace_tool_security_rule_in_conf_file(
    effect: str,
    old_rule: str,
    new_rule: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> bool:
    """`tool_security.<effect>` 의 규칙 하나를 제자리에서 고칩니다. 고쳤으면 True.

    새 규칙이 틀리면 ValueError 이고 파일은 그대로입니다. 고칠 규칙이 이미 없으면 쓰지 않고
    False 입니다. 새 규칙이 목록에 이미 있으면 옛 줄만 지워 같은 줄이 둘 남지 않게 합니다.
    """
    wanted = validate_rule_list([new_rule])
    if not wanted:
        raise ValueError("규칙을 비워 둘 수 없습니다. 지우려면 삭제 버튼을 쓰세요.")
    path = Path(config_path)
    target = str(old_rule or "").strip()

    data = read_conf_file(path)
    current = _tool_rule_list(data, effect, path)
    if target not in current:
        return False
    replaced = [wanted[0] if r == target else r for r in current]
    _section(data, "tool_security")[effect] = list(dict.fromkeys(replaced))

    write_conf_file(path, data)
    reload_config_if_active(path)
    return True

def set_agent_allowed_mcp_servers_in_conf_file(
    agent_key: str,
    servers: List[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.allowed_mcp_servers` 를 통째로 갈아 끼웁니다.

    어떤 에이전트가 어떤 도구를 쓰는지는 배포 설정이라 이 파일이 정본입니다.
    다만 **아직 시작하지 않은 대화**에만 걸립니다. 대화는 첫 발언과 함께 도구
    권한까지 `session_agents.config_snapshot` 으로 고정되므로, 이미 시작한 대화는
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

def set_agent_peer_consultation_in_conf_file(
    agent_key: str,
    targets: Optional[List[str]],
    max_tool_iterations: int,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.peer_consultation` 을 갈아 끼웁니다 (동료 자문 상대와 도구 상한).

    `targets` 가 None 이면 이번 턴에 참여한 다른 전문가 전원입니다 — 그때는 항목을 적지 않습니다.
    둘 다 기본값이면 블록을 통째로 지워 파일을 처음 모양으로 둡니다. 도구 할당과 같이 **아직 시작하지
    않은 대화**에 걸리고, 시작한 대화는 첫 발언 때 고정한 값을 그대로 씁니다.
    """
    key = _require_key(agent_key, "에이전트 키")
    if targets is not None:
        targets = list(dict.fromkeys(_require_key(t, "자문 대상 에이전트 키") for t in targets))
        if key in targets:
            raise ValueError("자기 자신은 자문 대상이 될 수 없습니다.")
    limit = int(max_tool_iterations)
    if not 0 <= limit <= TOOL_ITERATION_CEILING:
        raise ValueError(f"자문 도구 상한은 0~{TOOL_ITERATION_CEILING} 사이여야 합니다.")
    path = Path(config_path)

    data = read_conf_file(path)
    agent = _entry(data, "agents", key, path)
    block: Dict[str, Any] = {}
    if targets is not None:
        block["targets"] = targets
    if limit != DEFAULT_PEER_TOOL_ITERATIONS:
        block["max_tool_iterations"] = limit
    if block:
        agent["peer_consultation"] = block
    else:
        agent.pop("peer_consultation", None)

    write_conf_file(path, data)
    reload_config_if_active(path)

# 스킬 이름 규칙입니다 (스킬 폴더 이름과 일치). 에이전트 설정과 도구 인자에 그대로 전달되므로 간결해야 합니다.
SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

def require_skill_name(value: str) -> str:
    name = (value or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"스킬 이름 '{value}'은 사용할 수 없습니다. 영문, 숫자, 밑줄(_), 하이픈(-)으로 64자 이내로 구성해야 하며 "
            f"영문이나 숫자로 시작해야 합니다."
        )
    return name

def set_skill_enabled_in_conf_file(
    skill_name: str,
    enabled: bool,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`skills.disabled` 목록에서 해당 스킬을 제거(활성화)하거나 추가(비활성화)합니다.

    진행 중인 대화 세션에도 다음 발언부터 즉시 적용됩니다. 스킬 목록은 매 발언마다 이 설정과 스킬 폴더를
    다시 읽어서 구성하기 때문입니다 (`app/agents/skills.py`).
    """
    name = require_skill_name(skill_name)
    path = Path(config_path)

    data = read_conf_file(path)
    section = _section(data, "skills")
    current = section.get("disabled")
    if current is None:
        current = []
    elif not isinstance(current, list):
        raise ValueError(f"{path.name} 파일의 skills.disabled 항목이 목록 형태가 아닙니다.")
    names = [n for n in current if n != name]
    if not enabled:
        names.append(name)
    section["disabled"] = names

    write_conf_file(path, data)
    reload_config_if_active(path)

def set_agent_allowed_skills_in_conf_file(
    agent_key: str,
    skills: List[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.allowed_skills` 목록을 새로운 스킬 목록으로 교체합니다.

    `allowed_mcp_servers`와 마찬가지로 **아직 시작되지 않은 새 대화 세션**에만 적용됩니다 (이미 시작된 대화는
    첫 발언 시점에 에이전트 설정 스냅샷으로 고정됩니다). 빈 목록을 전달하면 해당 필드를 삭제합니다.
    """
    key = _require_key(agent_key, "에이전트 키")
    names = list(dict.fromkeys(require_skill_name(s) for s in skills))
    path = Path(config_path)

    data = read_conf_file(path)
    agent = _entry(data, "agents", key, path)
    if names:
        agent["allowed_skills"] = names
    else:
        agent.pop("allowed_skills", None)

    write_conf_file(path, data)
    reload_config_if_active(path)

def set_agent_allowed_modalities_in_conf_file(
    agent_key: str,
    modalities: List[str],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> None:
    """`agents.<key>.allowed_modalities` 목록을 교체합니다. 빈 목록이면 항목을 지웁니다.

    `allowed_skills` 와 같이 **아직 시작되지 않은 대화 세션**에만 적용됩니다. 순서는
    `INPUT_MODALITIES` 를 따르므로 어떤 순서로 체크해도 파일에 같게 적힙니다.
    """
    key = _require_key(agent_key, "에이전트 키")
    unknown = [m for m in modalities if m not in INPUT_MODALITIES]
    if unknown:
        raise ValueError(
            f"쓸 수 없는 입력 형식입니다: {', '.join(map(str, unknown))}. "
            f"{' | '.join(INPUT_MODALITIES)} 중에서 고르세요."
        )
    names = [m for m in INPUT_MODALITIES if m in modalities]
    path = Path(config_path)

    data = read_conf_file(path)
    agent = _entry(data, "agents", key, path)
    if names:
        agent["allowed_modalities"] = names
    else:
        agent.pop("allowed_modalities", None)

    write_conf_file(path, data)
    reload_config_if_active(path)

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
        if is_blank(value):
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
        if is_blank(value):
            continue  # 빈 칸은 "지정하지 않음" 입니다
        default = defaults.get(field)
        if not is_blank(default) and _same_value(value, default):
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
    allowed_skills: Optional[List[str]] = None,
) -> None:
    """`agents`에 새 에이전트를 추가합니다.

    `overrides`에는 `prune_agent_overrides()`를 거친 값만 전달하십시오. 여기에 포함되지 않은
    항목은 파일에 기록되지 않고 llm 기본 설정에서 상속됩니다. 스킬은 선택한 항목이 있을 때만 기록합니다.
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
    skills = list(dict.fromkeys(
        require_skill_name(s) for s in (allowed_skills or []) if s and s.strip()
    ))

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
    if skills:
        block["allowed_skills"] = skills
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
    if agent_key == ORCHESTRATOR_KEY and not enabled:
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
    if agent_key == ORCHESTRATOR_KEY:
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
