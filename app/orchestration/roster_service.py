"""로스터의 conf.json 조작과 토론 중 잠금 규칙. NiceGUI 를 모릅니다.

화면이 하던 일 가운데 화면이 아닌 것을 모았습니다 — 어떤 값을 어디에 쓰는지, 쓴 뒤 돌고 있는 앱에 어떻게
반영하는지, 지금 바꿔도 되는지. 무엇이 일어났는지는 `Notice` 로 돌려주고, 어떻게 보여 줄지는 화면이 정합니다.
그래서 `ui.notify` 를 바꿔 끼우지 않고 임시 conf.json 하나로 시험할 수 있습니다 (`tests/test_roster_service.py`).

이 패키지에 두는 것은 러너·MCP 풀·에이전트 풀·설정을 모두 볼 수 있는 가장 낮은 층이기 때문입니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agents.pool import AgentPool, get_agent_pool, reload_agent_pool
from app.agents.skills import Skill, scan_skills, skills_root
from app.config import (
    DEFAULT_CONFIG_PATH,
    RootConfig,
    active_config_path,
    add_agent_to_conf_file,
    add_mcp_server_to_conf_file,
    get_config,
    remove_agent_from_conf_file,
    remove_mcp_server_from_conf_file,
    set_agent_allowed_mcp_servers_in_conf_file,
    set_agent_allowed_modalities_in_conf_file,
    set_agent_allowed_skills_in_conf_file,
    set_agent_debate_order_in_conf_file,
    set_agent_debate_stance_in_conf_file,
    set_agent_enabled_in_conf_file,
    set_agent_peer_consultation_in_conf_file,
    set_mcp_server_enabled_in_conf_file,
    set_skill_enabled_in_conf_file,
)
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool
from app.orchestration.runner import get_debate_runner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notice:
    """사람에게 알릴 일 하나. `level` 은 positive·negative·warning·info 입니다."""

    message: str
    level: str = "positive"
    multi_line: bool = False


@dataclass(frozen=True)
class ReloadResult:
    """conf.json 다시 읽기의 결과. 읽지 못했으면 `pool` 이 None 이고 지금 구성이 그대로 남습니다."""

    pool: Optional[AgentPool]
    notices: Tuple[Notice, ...]


def agent_admin_lock_reason(running_reason: str, personas_locked: bool) -> str:
    """지금 에이전트 구성을 바꾸면 안 되는 이유. 바꿔도 되면 빈 문자열.

    이 하나가 conf.json 을 고치는 조작 전부를 막습니다 — 추가·삭제·비활성화,
    디베이트 진영 배정, 발언 순서(드래그), 도구 할당. 그래서 문구도 그 전부를
    말해야 합니다. '추가·삭제' 만 말하면 순서를 바꾸려다 막힌 사용자가 엉뚱한
    안내를 읽습니다.

    두 가지를 봅니다.

    하나는 **진행 중인 토론**(`running_reason`). 에이전트 풀은 프로세스 전체가 하나를 공유하므로,
    어느 대화든 돌고 있으면 그 토론의 발언자 목록이 도중에 바뀝니다. MCP 서버와
    같은 이유라 같은 잠금을 그대로 씁니다.

    다른 하나는 **이 대화가 이미 시작되었는지**. 첫 발언과 함께 구성이 고정되어
    여기서 바꿔도 이 대화에는 반영되지 않으므로, 열어 두면 헷갈리기만 합니다.
    """
    if running_reason:
        return running_reason
    if personas_locked:
        return (
            "본 세션은 이미 토론이 시작되어 에이전트 구성이 고정되었습니다. "
            "에이전트 추가·삭제, 진영, 발언 순서, 도구를 변경하시려면 새 세션을 "
            "시작하십시오."
        )
    return ""


class RosterService:
    """로스터가 바깥 세계(conf.json, 에이전트 풀, MCP 런타임, 러너)를 만나는 유일한 창구."""

    # ------------------------------------------------------------ 읽기

    @staticmethod
    def conf_path() -> str:
        """지금 앱이 읽고 있는 설정 파일. 없으면 기본값."""
        active = active_config_path()
        return str(active) if active is not None else DEFAULT_CONFIG_PATH

    @staticmethod
    def agent_pool() -> AgentPool:
        return get_agent_pool()

    @staticmethod
    def config() -> RootConfig:
        return get_config()

    @staticmethod
    def configured_agents() -> Dict[str, Any]:
        """conf.json 의 에이전트 전부 (꺼 둔 것 포함). 읽지 못하면 비어 있습니다."""
        try:
            return dict(get_config().agents)
        except Exception as e:  # noqa: BLE001 - 화면은 설정 오류로 죽지 않아야 합니다
            logger.warning(f"Could not read agents from conf.json: {e}")
            return {}

    @staticmethod
    def default_tool_mode() -> str:
        """conf.json 의 도구 보안 모드. 읽지 못해도 목록은 그려야 하므로 기본값을 돌려줍니다."""
        try:
            return get_config().tool_security.mode
        except Exception:  # noqa: BLE001
            return "default"

    @staticmethod
    def runtime(workspace: Any) -> Optional[MCPManager]:
        """이 대화의 작업 공간에 해당하는 MCP 런타임. 아직 안 떴으면 None.

        **떠 있는 것만 찾습니다** (`pool.get`). 화면을 그리는 것만으로 서버가 뜨면
        사이드바를 클릭할 때마다 프로세스 묶음이 하나씩 늘어납니다.

        없을 때 기본 런타임으로 물러서지 **않습니다.** 그러면 다른 폴더를 쓰는
        대화에 남의 작업 공간에서 뜬 서버의 상태가 "도구 14" 처럼 그려집니다 —
        이 대화의 서버는 하나도 안 떠 있는데 말입니다. 없으면 없다고 그리는 것이
        맞고, 그게 "첫 토론 때 기동됩니다" 라는 사실과도 맞습니다.
        """
        return get_runtime_pool().get(workspace)

    def mcp_view(self, workspace: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """(이 대화 런타임의 서버별 상태, conf.json 의 서버들). 읽지 못하면 둘 다 비어 있습니다."""
        try:
            runtime = self.runtime(workspace)
            status = runtime.connection_status() if runtime is not None else {}
            return status, get_config().mcp_servers
        except Exception as e:  # noqa: BLE001 - 화면은 설정 오류로 죽지 않아야 합니다
            logger.warning(f"Could not read MCP status: {e}")
            return {}, {}

    def runtime_status(self, workspace: Any) -> Optional[Dict[str, Any]]:
        """이 대화 런타임의 서버별 상태. 읽지 못하면 None (주기적인 비교에서 건너뜁니다)."""
        try:
            runtime = self.runtime(workspace)
            return runtime.connection_status() if runtime is not None else {}
        except Exception as e:  # noqa: BLE001 - 화면 갱신이 설정 오류로 죽지 않게
            logger.debug(f"Could not read MCP status for the refresh check: {e}")
            return None

    @staticmethod
    def skills_view() -> Tuple[List[Skill], Optional[Path]]:
        """(스킬 목록, 스킬 폴더). 읽지 못하면 ([], None)."""
        try:
            return scan_skills(), skills_root()
        except Exception as e:  # noqa: BLE001 - 화면은 설정 오류로 죽지 않아야 합니다
            logger.warning(f"Could not read skills: {e}")
            return [], None

    @staticmethod
    def mcp_fingerprint() -> Dict[str, Any]:
        """지금 떠 있어야 할 MCP 서버 구성. 값이 달라지면 다시 띄워야 합니다."""
        try:
            servers = get_config().enabled_mcp_servers
        except Exception:  # noqa: BLE001
            return {}
        return {
            name: (cfg.command, tuple(cfg.args), tuple(sorted(cfg.env.items())))
            for name, cfg in servers.items()
        }

    # ------------------------------------------------------------ 잠금

    @staticmethod
    def running_debate_lock_reason() -> str:
        """지금 MCP 구성을 바꾸면 안 되는 이유. 바꿔도 되면 빈 문자열.

        서버 프로세스는 이제 작업 공간마다 나뉘지만, **무엇을 어떻게 띄울지는
        여전히 conf.json 하나가 정합니다.** 여기서 서버를 고치면 살아 있는 런타임
        전부를 다시 띄우게 되고(`pool.reload_all`), 진행 중인 토론은 그 도중에
        도구를 잃거나 다른 구성으로 응답하는 서버를 만납니다. 그래서 이 대화든
        다른 대화든 하나라도 돌고 있으면 잠급니다.
        """
        running = get_debate_runner().running_sessions()
        if not running:
            return ""
        return (
            f"토론이 진행 중인 대화가 {len(running)}개 있습니다. MCP 서버 구성은 "
            f"conf.json 파일로 전체 작업 공간을 일괄 설정하므로, 모든 토론이 "
            f"종료되거나 정지되어 최종 산출물이 생성된 후에 변경할 수 있습니다."
        )

    @staticmethod
    def is_running(session_id: str) -> bool:
        return get_debate_runner().is_running(session_id)

    # ------------------------------------------------------------ conf.json 쓰기 (실패는 예외)

    def set_agent_enabled(self, agent_key: str, enabled: bool) -> None:
        set_agent_enabled_in_conf_file(agent_key, enabled, self.conf_path())

    def remove_agent(self, agent_key: str) -> None:
        remove_agent_from_conf_file(agent_key, self.conf_path())

    def add_agent(self, key: str, name: str, role: str, **fields: Any) -> None:
        add_agent_to_conf_file(key, name, role, config_path=self.conf_path(), **fields)

    def set_debate_order(self, keys: List[str]) -> None:
        set_agent_debate_order_in_conf_file(keys, self.conf_path())

    def set_debate_stance(self, agent_key: str, stance: str) -> None:
        set_agent_debate_stance_in_conf_file(agent_key, stance, self.conf_path())

    def set_agent_tools(self, agent_key: str, servers: List[str]) -> None:
        set_agent_allowed_mcp_servers_in_conf_file(agent_key, servers, self.conf_path())

    def set_agent_peer_consultation(self, agent_key: str, targets: Optional[List[str]], max_iterations: int) -> None:
        set_agent_peer_consultation_in_conf_file(agent_key, targets, max_iterations, self.conf_path())

    def set_agent_skills(self, agent_key: str, skills: List[str]) -> None:
        set_agent_allowed_skills_in_conf_file(agent_key, skills, self.conf_path())

    def set_agent_modalities(self, agent_key: str, modalities: List[str]) -> None:
        set_agent_allowed_modalities_in_conf_file(agent_key, modalities, self.conf_path())

    def set_mcp_enabled(self, server_name: str, enabled: bool) -> None:
        set_mcp_server_enabled_in_conf_file(server_name, enabled, self.conf_path())

    def add_local_mcp_server(
        self, name: str, command: str, args: List[str], env: Dict[str, str], enabled: bool
    ) -> None:
        add_mcp_server_to_conf_file(name, command, args, env, enabled, self.conf_path())

    def add_remote_mcp_server(
        self, name: str, url: str, headers: Dict[str, str], transport: str, enabled: bool
    ) -> None:
        add_mcp_server_to_conf_file(
            name, enabled=enabled, config_path=self.conf_path(), url=url, headers=headers, transport=transport,
        )

    def remove_mcp_server(self, server_name: str) -> None:
        remove_mcp_server_from_conf_file(server_name, self.conf_path())

    def set_skill_enabled(self, skill_name: str, enabled: bool) -> None:
        set_skill_enabled_in_conf_file(skill_name, enabled, self.conf_path())

    # ------------------------------------------------------------ 돌고 있는 앱에 반영

    @staticmethod
    def write_conf(write: Callable[[], None]) -> Optional[Notice]:
        """conf.json 을 고칩니다. 실패하면 알릴 내용을, 성공하면 None 을 돌려줍니다."""
        try:
            write()
        except Exception as e:  # noqa: BLE001 - 설정 오류는 화면에 그대로 알립니다
            logger.error(f"Could not update MCP servers in conf.json: {e}", exc_info=True)
            return Notice(f"conf.json 을 고치지 못했습니다: {e}", "negative")
        return None

    @staticmethod
    async def apply_to_running_app(done_message: str, restart_servers: bool = True) -> Notice:
        """고친 conf.json 을 돌고 있는 앱에 그대로 반영합니다.

        파일만 고치고 끝내면 화면과 실제로 떠 있는 서버·에이전트가 어긋납니다.
        다음 기동 때까지 그 사실을 아무도 모르므로 여기서 바로 맞춥니다.

        `restart_servers` 는 서버 프로세스를 다시 띄울지입니다. 도구 할당처럼
        서버 구성이 그대로인 변경까지 전부 다시 띄우면 몇 초씩 걸리기만 하고
        얻는 것이 없습니다.
        """
        try:
            if restart_servers:
                # 살아 있는 런타임 전부. conf.json 은 폴더가 달라도 정본 하나입니다.
                await get_runtime_pool().reload_all()
            # 설정을 다시 읽었으므로 에이전트 풀도 그 구성으로 맞춥니다.
            reload_agent_pool()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Applying the new configuration failed: {e}", exc_info=True)
            return Notice(f"conf.json 은 저장했지만 실행 중인 앱에 반영하지 못했습니다: {e}", "negative")
        return Notice(done_message, "positive")

    async def reload_conf(self, current_pool: AgentPool) -> ReloadResult:
        """conf.json 을 다시 읽어 에이전트 풀(과 필요하면 MCP 서버)을 갱신합니다.

        앱을 다시 띄우지 않고 에이전트를 추가·수정하기 위한 것입니다. 설정
        파일이 깨져 있으면 아무것도 바꾸지 않고 돌아옵니다 — `get_config()` 는
        새 설정을 다 읽은 뒤에야 전역 값을 갈아 끼우므로, 실패하면 지금 돌고
        있는 구성이 그대로 남습니다.
        """
        before_agents = {agent.key for agent in current_pool.list_all()}
        before_mcp = self.mcp_fingerprint()

        try:
            get_config(reload=True, config_path=self.conf_path())
        except Exception as e:  # noqa: BLE001 - 깨진 설정으로 앱이 죽으면 안 됩니다
            logger.error(f"Could not reload conf.json: {e}", exc_info=True)
            return ReloadResult(None, (Notice(
                f"conf.json 을 읽지 못했습니다. 실행 중인 설정은 그대로입니다: {e}", "negative", multi_line=True,
            ),))

        pool = reload_agent_pool()
        after_agents = {agent.key for agent in pool.list_all()}
        added = sorted(after_agents - before_agents)
        removed = sorted(before_agents - after_agents)
        notices: List[Notice] = []

        # MCP 구성까지 바뀌었다면 서버도 맞춰야 합니다. 그러지 않으면 화면은
        # 새 설정을, 도구는 옛 서버를 가리키게 됩니다.
        if self.mcp_fingerprint() != before_mcp:
            try:
                await get_runtime_pool().reload_all()
                notices.append(Notice("MCP 서버 구성이 변경되어 함께 재시작하였습니다.", "info"))
            except Exception as e:  # noqa: BLE001
                logger.error(f"MCP reload after conf reload failed: {e}", exc_info=True)
                notices.append(Notice(f"에이전트는 갱신하였으나 MCP 서버를 재시작하지 못했습니다: {e}", "negative"))

        parts = [f"에이전트 {len(after_agents)}개"]
        if added:
            parts.append(f"추가: {', '.join(added)}")
        if removed:
            parts.append(f"제거: {', '.join(removed)}")
        if not added and not removed:
            parts.append("목록 변경 없음 (모델·프롬프트 등 설정 갱신됨)")
        notices.append(Notice("conf.json을 다시 불러왔습니다 — " + " · ".join(parts), "positive", multi_line=True))
        return ReloadResult(pool, tuple(notices))
