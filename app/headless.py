"""헤드리스(Headless) 토론 실행 코어: 세션 생성, 턴 시작·중단, 상태·결과 조회, 이벤트 스트리밍 (v1.5).

REST API(`app/api.py`의 `/api/sessions/...`)와 MADO MCP 서버(`app/mcp_server.py`)에서 이 모듈의 함수를 공통 사용합니다.
외부 스크립트, CI/CD, 타 AI 에이전트(Claude Code, Cursor 등)가 MADO를 '다중 에이전트 토론 팀' 도구로 활용할 수 있도록 지원합니다.

* 턴은 웹 화면에서 시작된 턴과 동일하게 `DebateRunner`가 백그라운드에서 비동기 실행합니다. 동일 세션을 웹 화면에서 열면
  진행 중인 토론이 실시간 표시되며 화면에서 수동 중단할 수도 있습니다.
* **비대면(Unattended) 턴 실행**: 기본적으로 `attended=False` 모드로 실행됩니다. 계획 승인 단계를 건너뛰고 자동 진행하며,
  도구 보안 정책상 "사용자 확인(Ask)" 대상 호출은 자동 차단(`unattended`)되고, 예산 한도 도달 시 별도 확인 없이 즉시 마무리합니다.
  사용자가 웹 UI에서 직접 개입하고자 할 때만 `attended=True`로 실행합니다.
* 결과 데이터는 데이터베이스로부터 조회하므로 서버 재기동이나 세션 재접속 후에도 동일한 상태가 일관되게 유지됩니다.
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

# SSE 스트림 유휴 시 연결 유지를 위한 하트비트(빈 줄) 전송 간격 (프록시/클라이언트 타임아웃 방지).
KEEPALIVE_SECONDS = 15.0
MAX_ROUNDS_LIMIT = 10


class HeadlessError(ValueError):
    """헤드리스 API 요청 오류 (잘못된 요청: 400, 대상 없음: 404, 충돌/진행 중: 409). `status`는 HTTP 상태 코드."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _clean_agents(keys: Optional[List[str]]) -> List[str]:
    """참여 에이전트 목록을 검증합니다. 미지정 시 활성화된 전체 에이전트가 배정되며 오케스트레이터는 자동 포함됩니다."""
    pool = get_agent_pool()
    enabled = [a.key for a in pool.list_all() if getattr(a, "enabled", True)]
    if not keys:
        chosen = enabled
    else:
        unknown = [k for k in keys if pool.get(k) is None]
        if unknown:
            raise HeadlessError(400, f"존재하지 않거나 비활성화된 에이전트입니다: {', '.join(unknown)} (사용 가능: {', '.join(enabled)})")
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
    """새 대화 세션을 생성합니다. 그래프 토론은 전용 편집기를 통해 생성해야 하므로 지원되지 않습니다."""
    # 신규 생성이므로 레거시 별칭(`LEGACY_STRATEGY_ALIASES`)은 허용하지 않으며 정확한 전략 식별자를 요구합니다.
    name = (strategy or "").strip()
    chosen = STRATEGY_MAP.get(name)
    if chosen is None:
        raise HeadlessError(400, f"지원되지 않는 토론 전략입니다: {strategy} (지원 전략: {', '.join(STRATEGY_MAP)})")
    if chosen.runs_graph:
        raise HeadlessError(400, "그래프 기반 토론은 웹 UI에서 그래프를 선택하여 생성해야 합니다.")
    if not 1 <= int(max_rounds) <= MAX_ROUNDS_LIMIT:
        raise HeadlessError(400, f"max_rounds 값은 1~{MAX_ROUNDS_LIMIT} 사이여야 합니다.")
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
        raise HeadlessError(404, f"대화 세션을 찾을 수 없습니다: {session_id}")
    return session


async def start_turn(session_id: str, prompt: str, *, attended: bool = False) -> Dict[str, Any]:
    """백그라운드에서 토론 턴을 시작하고 즉시 응답합니다. 이미 실행 중인 경우 409 반환."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HeadlessError(400, "사용자 요청 프롬프트(prompt)가 비어 있습니다.")
    runner = get_debate_runner()
    async with get_session_factory()() as db:
        session = await _session_or_404(db, session_id)
        workspace = session.workspace_dir or None
    if runner.is_running(session_id):
        raise HeadlessError(409, "해당 대화 세션에서 이미 토론이 진행 중입니다. 완료 후 다시 시도해 주세요.")
    run = runner.start(session_id, prompt, workspace, attended=attended)
    return {"session_id": session_id, "status": run.status, "attended": attended}


def stop_turn(session_id: str) -> bool:
    """진행 중인 턴의 중단을 요청합니다 — 잔여 라운드를 건너뛰고 현재까지의 논의를 바탕으로 결론을 합성합니다."""
    return get_debate_runner().request_stop(session_id)


async def turn_status(session_id: str) -> Dict[str, Any]:
    """세션의 현재 상태를 조회합니다. 진행 중인 경우 실시간 진행 상태를, 유휴 상태인 경우 최종 턴 기록을 반환합니다."""
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
    """최종 턴의 실행 결과를 조회합니다: 최종 결론, 생성된 아티팩트, 결정 장부, 토큰 사용량, 조기 종료 여부, 작업 공간 변경 파일 통계."""
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
    """진행 중인 턴이 완료될 때까지 대기합니다. 완료 시 True, 제한 시간 초과 시 False 반환."""
    run = get_debate_runner().get(session_id)
    if run is None or run.status != "running" or run.task is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(run.task), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    except Exception:  # noqa: BLE001 - 예외 발생 시에도 실행 종료로 간주하며 상세 사유는 결과 조회에서 확인합니다
        return True
    return True


def _jsonable(event: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(event)
    for key, value in list(out.items()):
        if isinstance(value, set):
            out[key] = sorted(value)
    return out


async def events(session_id: str) -> AsyncIterator[Dict[str, Any]]:
    """진행 중인 턴의 이벤트를 실시간 스트리밍합니다. 초기 연결 시 `snapshot`, 완료 시 `run_finished`를 전송합니다.

    진행 중인 턴이 없으면 `idle` 이벤트를 전송하고 종료합니다. 유휴 구간에서는 하트비트 유지를 위해 `None`을 yield합니다.
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
