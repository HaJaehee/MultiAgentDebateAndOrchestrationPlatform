"""화면 없이 토론을 돌리는 길: 대화 만들기, 턴 시작·정지, 상태·결과 읽기, 이벤트 받기 (v1.5).

REST(`app/api.py` 의 `/api/sessions/...`)와 MCP 서버(`app/mcp_server.py`)가 이 함수들을 함께 씁니다.
스크립트·CI·다른 에이전트(Claude Code, IDE)가 MADO 를 "토론 팀" 하나로 부를 수 있게 하는 것이 목적입니다.

* 턴은 화면에서 시작한 턴과 똑같이 `DebateRunner` 가 백그라운드에서 돌립니다. 같은 대화를 화면으로 열면
  진행 중인 토론이 그대로 보이고, 화면에서 정지할 수도 있습니다.
* **답할 사람이 없는 턴**: 기본으로 `attended=False` 로 시작합니다. 계획 승인 카드를 띄우지 않고, 도구 보안이
  "묻기" 로 판정한 호출은 실행하지 않으며(`unattended`), 한도에 닿으면 묻지 않고 마무리합니다. 사람이 화면에서
  답하겠다면 `attended=True` 로 시작합니다.
* 결과는 기록(DB)에서 읽습니다. 그래서 서버가 다시 떠도, 다른 화면에서 시작한 턴이어도 같은 답입니다.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy import desc, select

from app.agents.llm_usage import Usage
from app.agents.pool import get_agent_pool
from app.config import ORCHESTRATOR_KEY, resolve_workspace_dir
from app.database.models import ArtifactModel, MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory
from app.events import EventType
from app.orchestration import turns
from app.orchestration.runner import get_debate_runner
from app.orchestration.strategies import DEFAULT_STRATEGY, STRATEGY_MAP

# 이벤트 흐름에서 이만큼 조용하면 연결이 살아 있다는 표시(빈 줄)를 보냅니다 (SSE 의 프록시·클라이언트 시간 초과).
KEEPALIVE_SECONDS = 15.0
MAX_ROUNDS_LIMIT = 10


class HeadlessError(ValueError):
    """요청이 틀렸거나(400) 대상이 없거나(404) 지금은 할 수 없습니다(409). `status` 가 HTTP 상태입니다."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _clean_agents(keys: Optional[List[str]]) -> List[str]:
    """참여할 에이전트. 주지 않으면 켜져 있는 모든 에이전트. 없는 키는 거절합니다. 오케스트레이터는 늘 들어갑니다."""
    pool = get_agent_pool()
    enabled = [a.key for a in pool.list_all() if getattr(a, "enabled", True)]
    if not keys:
        chosen = enabled
    else:
        unknown = [k for k in keys if pool.get(k) is None]
        if unknown:
            raise HeadlessError(400, f"없는 에이전트입니다: {', '.join(unknown)} (있는 것: {', '.join(enabled)})")
        chosen = list(dict.fromkeys(keys))
    if ORCHESTRATOR_KEY not in chosen:
        chosen = [ORCHESTRATOR_KEY] + chosen
    return chosen


async def create_session(
    *,
    title: str = "",
    strategy: str = DEFAULT_STRATEGY,
    max_rounds: int = 3,
    agents: Optional[List[str]] = None,
    custom_instructions: str = "",
    workspace_dir: str = "",
    converge_early: bool = True,
    token_budget: int = 0,
) -> Dict[str, Any]:
    """대화를 만듭니다. 그래프 토론은 화면에서 그래프를 골라야 하므로 여기서는 만들 수 없습니다."""
    # 새로 만드는 길이라 옛 이름(`LEGACY_STRATEGY_ALIASES`)은 받지 않고, 모르는 이름을 기본값으로 바꾸지도 않습니다.
    name = (strategy or "").strip()
    chosen = STRATEGY_MAP.get(name)
    if chosen is None:
        raise HeadlessError(400, f"없는 전략입니다: {strategy} (있는 것: {', '.join(STRATEGY_MAP)})")
    if chosen.runs_graph:
        raise HeadlessError(400, "그래프 토론은 화면에서 그래프를 골라 만드십시오.")
    if not 1 <= int(max_rounds) <= MAX_ROUNDS_LIMIT:
        raise HeadlessError(400, f"max_rounds 는 1~{MAX_ROUNDS_LIMIT} 이어야 합니다.")
    keys = _clean_agents(agents)
    sid = str(uuid.uuid4())
    async with get_session_factory()() as db:
        db.add(SessionModel(
            id=sid,
            title=(title or "").strip()[:200] or "API Debate",
            strategy=name,
            max_rounds=int(max_rounds),
            active_agents=keys,
            known_agents=[a.key for a in get_agent_pool().list_all()],
            custom_instructions=custom_instructions or "",
            workspace_dir=(workspace_dir or "").strip(),
            converge_early=bool(converge_early),
            token_budget=max(0, int(token_budget or 0)),
        ))
        await db.commit()
    return {"session_id": sid, "strategy": name, "agents": keys,
            "workspace_dir": str(resolve_workspace_dir(workspace_dir or None))}


async def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    runner = get_debate_runner()
    async with get_session_factory()() as db:
        rows = (await db.execute(
            select(SessionModel).order_by(desc(SessionModel.updated_at)).limit(max(1, min(limit, 500)))
        )).scalars().all()
    return [
        {"session_id": s.id, "title": s.title, "strategy": s.strategy, "agents": list(s.active_agents or []),
         "updated_at": _iso(s.updated_at), "running": runner.is_running(s.id)}
        for s in rows
    ]


async def _session_or_404(db, session_id: str) -> SessionModel:
    session = await db.get(SessionModel, session_id)
    if session is None:
        raise HeadlessError(404, f"없는 대화입니다: {session_id}")
    return session


async def start_turn(session_id: str, prompt: str, *, attended: bool = False) -> Dict[str, Any]:
    """턴을 백그라운드에서 시작하고 곧바로 돌아옵니다. 이미 돌고 있으면 409."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HeadlessError(400, "요청(prompt)이 비어 있습니다.")
    runner = get_debate_runner()
    async with get_session_factory()() as db:
        session = await _session_or_404(db, session_id)
        workspace = session.workspace_dir or None
    if runner.is_running(session_id):
        raise HeadlessError(409, "이 대화는 토론이 진행 중입니다. 끝난 뒤에 다시 시작하십시오.")
    run = runner.start(session_id, prompt, workspace, attended=attended)
    return {"session_id": session_id, "status": run.status, "attended": attended}


def stop_turn(session_id: str) -> bool:
    """진행 중인 턴에 정지를 요청합니다 — 남은 라운드를 건너뛰고 지금까지로 결론을 냅니다."""
    return get_debate_runner().request_stop(session_id)


async def turn_status(session_id: str) -> Dict[str, Any]:
    """지금 상태. 돌고 있으면 러너의 상태 줄을, 아니면 마지막 턴의 기록을 돌려줍니다."""
    async with get_session_factory()() as db:
        await _session_or_404(db, session_id)
    run = get_debate_runner().get(session_id)
    if run is not None and run.status == "running":
        return {
            "session_id": session_id, "running": True, "turn_id": run.turn_id, "phase": run.phase,
            "status_text": run.status_text, "round_info": run.round_info,
            "waiting_for_human": bool(run.plan_approval or run.approvals or run.decision_request),
        }
    result = await turn_result(session_id)
    return {"session_id": session_id, "running": False, **{k: result.get(k) for k in ("turn_id", "status", "error")}}


async def turn_result(session_id: str) -> Dict[str, Any]:
    """마지막 턴의 결과: 결론, 산출물, 결정 장부, 사용량, 일찍 끝났는지, 작업 공간에서 바뀐 파일 수."""
    async with get_session_factory()() as db:
        session = await _session_or_404(db, session_id)
        turn = (await db.execute(
            select(TurnModel).where(TurnModel.session_id == session_id)
            .order_by(desc(TurnModel.started_at)).limit(1)
        )).scalar_one_or_none()
        if turn is None:
            return {"session_id": session_id, "turn_id": None, "status": "none",
                    "conclusion": "", "artifacts": [], "decision_ledger": session.decision_ledger or ""}
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.turn_id == turn.id).order_by(MessageModel.created_at)
        )).scalars().all()
        artifacts = (await db.execute(
            select(ArtifactModel)
            .where(ArtifactModel.session_id == session_id, ArtifactModel.created_at >= turn.started_at)
            .order_by(ArtifactModel.created_at)
        )).scalars().all()
    synthesis = next((m for m in reversed(messages) if turns.kind_of(m) == turns.KIND_SYNTHESIS), None)
    early = turns.early_end(messages)
    checkpoint = turn.checkpoint if isinstance(turn.checkpoint, dict) else {}
    return {
        "session_id": session_id,
        "turn_id": turn.id,
        "status": turn.status,
        "error": turn.error or None,
        "request": next((m.content for m in messages if turns.kind_of(m) == turns.KIND_OPENING), ""),
        "conclusion": synthesis.content if synthesis is not None else "",
        "artifacts": [
            {"type": a.artifact_type, "title": a.title, "language": a.language, "content": a.content}
            for a in artifacts
        ],
        "decision_ledger": session.decision_ledger or "",
        "failed_agents": turns.failed_agents(messages),
        "ended_early": {"reason": early[0], "round": early[1]} if early else None,
        "usage": Usage.of(turn.usage).as_meta() if turn.usage else None,
        "files_changed": checkpoint.get("changed"),
        "started_at": _iso(turn.started_at),
        "finished_at": _iso(turn.finished_at),
    }


async def wait_for_turn(session_id: str, timeout: float) -> bool:
    """진행 중인 턴이 끝날 때까지 기다립니다. 끝났으면 True, 시간 안에 안 끝났으면 False."""
    run = get_debate_runner().get(session_id)
    if run is None or run.status != "running" or run.task is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(run.task), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    except Exception:  # noqa: BLE001 - 실패도 "끝남" 입니다. 이유는 결과에 있습니다
        return True
    return True


def _jsonable(event: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(event)
    for key, value in list(out.items()):
        if isinstance(value, set):
            out[key] = sorted(value)
    return out


async def events(session_id: str) -> AsyncIterator[Dict[str, Any]]:
    """진행 중인 턴의 이벤트. 처음에 지금까지의 스냅샷(`snapshot`)을, 끝에 `run_finished` 를 보냅니다.

    돌고 있는 턴이 없으면 `idle` 하나를 보내고 끝납니다. 조용한 동안에는 `None` 을 내어 보내는 쪽이 연결
    유지 표시를 보내게 합니다.
    """
    run = get_debate_runner().get(session_id)
    if run is None or run.status != "running":
        yield {"type": "idle", "session_id": session_id}
        return
    queue = run.subscribe()
    try:
        yield {"type": "snapshot", **_jsonable(run.snapshot())}
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield None  # type: ignore[misc]
                continue
            yield _jsonable(event)
            if event.get("type") == EventType.RUN_FINISHED:
                return
    finally:
        run.unsubscribe(queue)
