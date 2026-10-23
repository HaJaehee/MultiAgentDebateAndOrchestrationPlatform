import logging
from typing import Dict, List, Optional
from app.agents.base import Agent
from app.config import ORCHESTRATOR_KEY, AgentConfig, get_config

logger = logging.getLogger(__name__)


class AgentPool:
    """Registry and factory of configured Agents."""

    def __init__(self, agent_configs: Optional[Dict[str, AgentConfig]] = None):
        self.agent_configs = agent_configs or {}
        self._agents: Dict[str, Agent] = {}
        self.reload()

    def reload(self) -> None:
        """Instantiates Agent objects from configuration."""
        self._agents.clear()
        for key, cfg in self.agent_configs.items():
            if not getattr(cfg, "enabled", True):
                logger.info(f"Agent '{key}' is disabled in conf.json; skipping registration.")
                continue
            agent = Agent.from_config(key, cfg)
            self._agents[key] = agent
            logger.info(
                f"Agent '{key}' -> model={agent.model}, endpoint={agent.endpoint_label}, "
                f"sequential_thinking={'on:' + agent.sequential_thinking.mode if agent.sequential_thinking.enabled else 'off'}"
            )
        logger.info(f"AgentPool loaded {len(self._agents)} agents: {list(self._agents.keys())}")

    def get(self, key: str) -> Optional[Agent]:
        return self._agents.get(key)

    def get_orchestrator(self) -> Agent:
        orch = self.get(ORCHESTRATOR_KEY)
        if not orch:
            raise RuntimeError("Orchestrator agent is not registered in AgentPool.")
        return orch

    def list_all(self) -> List[Agent]:
        return list(self._agents.values())

    def get_active(self, keys: List[str]) -> List[Agent]:
        """Returns list of agents for given keys. Always ensures orchestrator is included."""
        active = []
        # Ensure orchestrator is always first
        if ORCHESTRATOR_KEY not in keys:
            keys = [ORCHESTRATOR_KEY] + [k for k in keys if k != ORCHESTRATOR_KEY]

        for k in keys:
            ag = self.get(k)
            if ag and ag not in active:
                active.append(ag)
        return active


_agent_pool: Optional[AgentPool] = None


def get_agent_pool() -> AgentPool:
    global _agent_pool
    if _agent_pool is None:
        cfg = get_config()
        _agent_pool = AgentPool(cfg.agents)
    return _agent_pool


def reload_agent_pool() -> AgentPool:
    """다시 읽어 들인 conf.json 으로 전역 풀을 갱신합니다.

    새 객체를 만들지 않고 제자리에서 다시 채웁니다. 엔진과 화면이 이 풀을 각자
    붙잡고 있어서, 갈아 끼우면 이미 들고 있던 쪽은 예전 구성을 계속 보게 됩니다.
    """
    pool = get_agent_pool()
    pool.agent_configs = get_config().agents
    pool.reload()
    return pool
