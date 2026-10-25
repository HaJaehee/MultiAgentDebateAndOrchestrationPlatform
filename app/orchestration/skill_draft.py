"""토론 결론 기반 스킬 초안 자동 생성 (v1.5).

토론에서 도출된 핵심 규칙(결정 사항, 제약 조건, 기각 사유)은 결정 장부와 최종 결론에 기록되지만,
별도로 저장하지 않으면 다음 대화 세션에 유지되지 않습니다. 이를 재사용 가능한 스킬(`skills/<이름>/SKILL.md`, ADR-026)로
저장하면 향후 유사 업무를 수행하는 에이전트가 필요할 때 동적으로 참조할 수 있습니다.

* 오케스트레이터 에이전트가 해당 턴의 사용자 요청, 최종 결론, 결정 장부를 종합 분석하여 표준 SKILL.md 규격에 맞게 초안을 작성합니다.
  형식 예시로 기본 탑재 스킬(`mermaid-diagrams`)의 메타데이터 및 구조를 활용합니다.
* 사용자가 스킬 이름, 설명, 지침 본문을 검토 및 수정한 후 저장합니다 (`save_draft`). 생성된 스킬은 안전을 위해
  **비활성화 상태**로 등록되며, 로스터 관리 패널에서 활성화하고 대상 에이전트에 할당할 수 있습니다.
* 기존 스킬을 덮어쓰지 않고 항상 새로운 고유 디렉터리로 저장합니다 (`skills.write_skill`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.personas import frozen_agents
from app.agents.pool import get_agent_pool
from app.agents.reasoning import strip_reasoning_trace
from app.agents.skills import SkillError, parse_skill_md, skills_root, write_skill
from app.config import ORCHESTRATOR_KEY, SKILL_NAME_PATTERN, active_config_path, set_skill_enabled_in_conf_file
from app.database.models import MessageModel, SessionModel, TurnModel
from app.orchestration import turns
from app.orchestration.conversation import ConversationMemory

SKILL_DRAFT_MARKER = "[스킬 초안 작성]"
EXAMPLE_SKILL = "mermaid-diagrams"
EXAMPLE_LINES = 30
MAX_INPUT_CHARS = 12000


@dataclass
class SkillDraft:
    name: str
    description: str
    body: str

    def text(self) -> str:
        description = " ".join(self.description.split())
        return f"---\nname: {self.name}\ndescription: {description}\n---\n\n{self.body.strip()}\n"


def _clip(text: str, limit: int = MAX_INPUT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…(길어서 줄였습니다)"


def _example() -> str:
    path = skills_root() / EXAMPLE_SKILL / "SKILL.md"
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[:EXAMPLE_LINES])
    except OSError:
        return "---\nname: example-skill\ndescription: 언제 쓰는지 한 문장\n---\n\n# 제목\n\n- 지침"


def draft_prompt(request: str, conclusion: str, ledger: str) -> List[dict]:
    content = (
        f"{SKILL_DRAFT_MARKER} 아래 토론에서 정한 규칙을, 다음에 같은 종류의 일을 하는 에이전트가 따를 **스킬**로 "
        "정리합니다. 스킬은 에이전트가 일이 맞을 때 불러 읽는 지침서입니다.\n\n"
        f"[토론의 요청]\n{_clip(request, 3000)}\n\n"
        f"[결정 장부]\n{_clip(ledger) or '(없음)'}\n\n"
        f"[최종 결론]\n{_clip(conclusion)}\n\n"
        f"[스킬 파일 형식의 예]\n{_example()}\n\n"
        "규칙:\n"
        "- 위 예와 같은 SKILL.md 하나만 쓰세요. 머리말(`---` 로 감싼 name, description) 다음에 본문.\n"
        "- name 은 영문 소문자·숫자·하이픈(예: `order-cache-rules`), 64자 이내.\n"
        "- description 은 **언제 이 스킬을 쓰는지** 한 문장. 에이전트는 이 한 줄만 보고 고릅니다.\n"
        "- 본문에는 결정된 규칙과 그 이유, 기각된 대안과 이유, 지켜야 할 제약을 명령문으로. 이번 토론의 날짜·사람·"
        "진행 경과는 빼고, 다음에도 쓸 수 있는 것만. 합의되지 않은 쟁점은 '확인할 것' 으로만 적으세요.\n"
        "- 없는 사실을 지어내지 마세요."
    )
    return [{"role": "user", "content": content}]


def _unfence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", stripped, re.S)
    return match.group(1) if match else stripped


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")[:64]
    return slug if slug and SKILL_NAME_PATTERN.fullmatch(slug) else ""


def parse_draft(content: str) -> SkillDraft:
    """LLM 응답 텍스트로부터 스킬 초안 객체를 파싱합니다. 프론트매터 누락이나 명명 규칙 불일치 시에도 보정하여 반환합니다."""
    text = _unfence(strip_reasoning_trace(content or ""))
    start = text.find("---")
    if start > 0:
        text = text[start:]
    try:
        meta, body = parse_skill_md(text)
    except SkillError:
        meta, body = {}, text
    name = _slug(meta.get("name", "")) or f"debate-rules-{datetime.now():%Y%m%d}"
    return SkillDraft(name=name, description=(meta.get("description") or "").strip(), body=body.strip())


async def _orchestrator(db: Any, session: SessionModel) -> Agent:
    pool = get_agent_pool()
    frozen = {a.key: a for a in await frozen_agents(db, session.id, pool)}
    agent = frozen.get(ORCHESTRATOR_KEY) or pool.get_orchestrator()
    return ConversationMemory.tool_less(agent)


async def draft_skill(db: Any, session_id: str, turn_id: str, *, llm_caller: Optional[LLMCaller] = None) -> SkillDraft:
    """해당 턴의 사용자 요청, 결론, 결정 장부를 분석하여 스킬 초안을 생성합니다."""
    session = await db.get(SessionModel, session_id)
    turn = await db.get(TurnModel, turn_id)
    if session is None or turn is None or turn.session_id != session_id:
        raise ValueError("해당 대화 세션 또는 턴을 찾을 수 없습니다.")
    messages = (await db.execute(
        select(MessageModel).where(MessageModel.turn_id == turn_id).order_by(MessageModel.created_at)
    )).scalars().all()
    synthesis = next((m for m in reversed(messages) if turns.kind_of(m) == turns.KIND_SYNTHESIS), None)
    if synthesis is None or synthesis.msg_type == "error" or not synthesis.content.strip():
        raise ValueError("해당 턴에 생성된 결론이 없어 스킬 초안을 작성할 수 없습니다.")
    request = next((m.content for m in messages if turns.kind_of(m) == turns.KIND_OPENING), "")
    latest = (await db.execute(
        select(TurnModel.id).where(TurnModel.session_id == session_id).order_by(TurnModel.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    ledger = session.decision_ledger if latest == turn_id else (turn.memory or {}).get("decision_ledger", "")
    writer = await _orchestrator(db, session)
    content, _ = await (llm_caller or LLMCaller()).call_agent(writer, draft_prompt(request, synthesis.content, ledger or ""))
    return parse_draft(content)


def save_draft(draft: SkillDraft) -> Path:
    """초안을 스킬 디렉터리에 저장하고 비활성화 상태로 등록합니다. 저장된 `SKILL.md` 경로를 반환합니다."""
    path = write_skill(draft.name, draft.text())
    config_path = active_config_path()
    if config_path is not None:
        set_skill_enabled_in_conf_file(draft.name, False, config_path)
    return path
