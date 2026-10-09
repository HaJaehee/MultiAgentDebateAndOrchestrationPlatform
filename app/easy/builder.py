"""대화형 에이전트 빌더 — 도우미 프롬프트, 설계도 파싱·필터링 및 저장 로직.

설계 도우미는 오케스트레이터의 연결 설정을 공유하는 **도구 없는 사본(tool-less copy)**입니다 (엔진이 발언자 지명이나 요약을
수행할 때 사용하는 `_tool_less`와 동일한 방식). 매 응답 끝에 ```` ```agent {json}``` ```` 형태의 설계도 블록을 첨부하도록 유도하며,
화면에서는 해당 블록을 파싱하여 우측 설계도 카드에 반영합니다. 사용자가 카드에서 내용을 직접 수정할 수 있으므로 도우미의 초안에 오류가 있더라도
저장 전에 바로잡을 수 있습니다.

저장 방식은 사용자 권한에 따라 구분됩니다:

* 소유자 — `conf.json`의 `agents` 섹션에 직접 추가합니다 (`add_agent_to_conf_file`). 모델 및 API 키는 별도로 지정하지 않아 기본
  `llm` 설정을 상속받습니다. 로스터 편집 규칙과 동일하게 진행 중인 토론 세션이 있으면 저장이 거부됩니다.
* 체험 방문자 — `easy_agents` 테이블에만 저장합니다. `conf.json`은 서버 전체가 공유하는 핵심 설정이므로 방문자가 임의로 변경할 경우
  다른 사용자의 대화 세션 및 도구 권한에 영향을 미칠 수 있기 때문입니다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import CARD_COLOR_CHOICES, ICON_CHOICES, Agent
from app.agents.llm import LLMCaller, strip_reasoning_trace
from app.agents.pool import AgentPool, get_agent_pool, reload_agent_pool
from app.config import (
    DEFAULT_CONFIG_PATH,
    AgentConfig,
    active_config_path,
    add_agent_to_conf_file,
    get_config,
    join_text_lines,
)
from app.easy.catalog import GUEST_SERVERS, MAX_GUEST_AGENTS, Option
from app.easy.models import EasyAgentModel

# 설계도 블록 정규식. 업무 지시서 내부에 '{고객명}' 같은 중괄호가 포함되어 있어도 블록 끝(```)까지 안전하게 추출합니다.
_BLOCK_RE = re.compile(r"```(?:agent|json)\s*(\{.*?\})\s*```", re.S)
_BLOCK_START_RE = re.compile(r"```(?:agent|json)")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_COLORS = {c["hex"].lower() for c in CARD_COLOR_CHOICES}

MAX_NAME = 40
MAX_ROLE = 120


class SaveRefused(ValueError):
    """에이전트 저장 실패 예외 (화면에 오류 메시지로 직접 표시됩니다)."""


class AgentDraft(BaseModel):
    """에이전트 설계도 데이터 모델 — conf.json의 에이전트 설정 필드명과 일치시켜 매핑을 직관적으로 보여줍니다."""

    key: str = ""
    name: str = ""
    role: str = ""
    system_prompt: str = ""
    allowed_mcp_servers: List[str] = Field(default_factory=list)
    allowed_skills: List[str] = Field(default_factory=list)
    card_color: str = ""
    icon: str = ""

    @field_validator("key", "name", "role", "card_color", "icon", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return str(v if v is not None else "").strip()

    @field_validator("system_prompt", mode="before")
    @classmethod
    def _prompt(cls, v: Any) -> str:
        return str(join_text_lines(v) if v is not None else "").strip()

    @field_validator("allowed_mcp_servers", "allowed_skills", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            return []
        return [str(x).strip() for x in v if str(x).strip()]


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------

_PROMPT = """당신은 '에이전트 설계 도우미'입니다. 대화 상대는 개발자가 아닌 일반 직장인입니다. 대화를 통해 사용자가 맡기고자 하는 업무를 파악하고, 그 업무를 수행할 AI 에이전트의 설정을 맞춤형으로 작성해 줍니다.

## 대화 방식
- 알기 쉬운 한국어로 간결하게 대화합니다. 전문 용어를 쓸 때는 바로 뒤 괄호 안에 알기 쉬운 설명을 덧붙입니다.
- 질문은 한 번에 최대 두 개까지만 합니다. 이미 확인한 내용은 다시 묻지 않습니다.
- 파악할 핵심 항목: ① 맡길 업무와 목표 ② 활용할 자료(파일, 문서, 웹 등) ③ 결과물 형태(표, 요약문, 이메일 초안 등) ④ 반드시 준수할 사항이나 금지 사항.
- 두세 번의 대화로 신속하게 초안을 완성합니다. 정보가 일부 부족하더라도 합리적인 기본값으로 설계도를 먼저 채운 뒤 수정하고 싶은 부분이 있는지 질문합니다.
- 설계도를 업데이트할 때마다 무엇을 왜 변경했는지 한두 문장으로 설명합니다. 특히 도구를 선택한 이유를 명확히 안내합니다 (예: "문서를 읽어야 하므로 '파일 열어 보기' 도구를 추가했습니다").

## 에이전트의 네 가지 요소
- 페르소나: name(이름), role(한 줄 역할)
- 업무 지시서: system_prompt — 에이전트가 작업을 시작할 때 가장 먼저 읽고 따르는 행동 지침
- 도구: allowed_mcp_servers — 실제로 작업을 수행하는 손과 발. 도구가 없으면 텍스트 대화만 가능합니다
- 스킬: allowed_skills — 특정 작업이 필요할 때 참고하는 업무 매뉴얼

## 고를 수 있는 도구 (id 만 씁니다. 목록에 없는 것은 쓰지 않습니다)
{tools}
{guest_note}
## 고를 수 있는 스킬 (id 만 씁니다)
{skills}

## system_prompt 쓰는 법
"당신은 ..."으로 시작하는 2인칭 문체로 작성합니다. 목표, 작업 절차(번호 목록), 결과물 양식, 도구 활용 시점, 주의 및 금지 사항을 명시합니다. 직접 확인할 수 있는 정보는 추측하지 말고 도구를 활용해 검증하도록 지시합니다.

## 설계도 블록 — 모든 답의 맨 끝에 반드시 붙입니다
지금까지 정의한 내용을 아래 형식의 단일 블록으로 응답 끝에 반드시 포함합니다. 아직 미정인 항목은 빈 문자열로 둡니다. 블록 외부에는 JSON 코드를 작성하지 않습니다.
```agent
{"key": "영문 소문자와 밑줄로 된 식별자", "name": "이름", "role": "한 줄 역할", "system_prompt": "업무 지시서", "allowed_mcp_servers": ["도구 id"], "allowed_skills": ["스킬 id"], "card_color": "#rrggbb", "icon": "아이콘 이름"}
```
- card_color 는 다음 중 하나입니다: {colors}
- icon 은 다음 중 하나입니다: {icons}

사용자 메시지 끝에 [지금 설계도]가 첨부되어 있으면, 사용자가 UI에서 직접 수정한 최신 값입니다. 사용자가 수정한 내용을 최우선으로 반영하여 설계를 이어갑니다."""

_GUEST_NOTE = (
    "\n현재 체험 방문자와 대화 중입니다. 체험 환경에서는 도구가 읽기 전용으로 동작하여 파일 생성/수정이나 코드 "
    "실행 작업은 제한됩니다. 대화 중에 이 점을 자연스럽게 안내해 주세요.\n"
)


def _option_lines(options: Sequence[Option], empty: str) -> str:
    lines = [f"- `{o.id}`: {o.label}" + (f" — {o.description}" if o.description else "") for o in options]
    return "\n".join(lines) or empty


def builder_prompt(servers: Sequence[Option], skills: Sequence[Option], *, guest: bool) -> str:
    return (
        _PROMPT.replace("{tools}", _option_lines(servers, "- (지금 쓸 수 있는 도구가 없습니다. 빈 목록으로 두세요)"))
        .replace("{guest_note}", _GUEST_NOTE if guest else "")
        .replace("{skills}", _option_lines(skills, "- (등록된 스킬이 없습니다. 빈 목록으로 두세요)"))
        .replace("{colors}", ", ".join(c["hex"] for c in CARD_COLOR_CHOICES))
        .replace("{icons}", ", ".join(ICON_CHOICES))
    )


def builder_agent(pool: AgentPool, prompt: str) -> Agent:
    """오케스트레이터의 연결 설정을 기반으로 동작하는 도구 없는 설계 도우미 에이전트를 생성합니다."""
    orchestrator = pool.get_orchestrator()
    return orchestrator.model_copy(update={
        "system_prompt": prompt,
        "allowed_mcp_servers": [],
        "allowed_skills": [],
        "sequential_thinking": orchestrator.sequential_thinking.model_copy(update={"enabled": False}),
    })


def with_draft(text: str, draft: AgentDraft) -> str:
    """사용자 메시지에 현재 UI의 설계도 상태를 첨부합니다. 사용자가 직접 수정한 값을 도우미가 임의로 덮어쓰지 않도록 보장합니다."""
    if not (draft.name or draft.role or draft.system_prompt):
        return text
    return f"{text}\n\n[지금 설계도]\n```json\n{json.dumps(draft.model_dump(), ensure_ascii=False)}\n```"


async def ask_builder(
    history: List[Dict[str, str]],
    *,
    servers: Sequence[Option],
    skills: Sequence[Option],
    guest: bool,
    on_chunk: Optional[Callable[[str], Any]] = None,
    llm: Optional[Any] = None,
    pool: Optional[AgentPool] = None,
) -> str:
    """도우미 에이전트와 대화를 수행하고 전체 응답 텍스트를 반환합니다. LLM 연결이 불가능하면 `LLMUnavailableError`가 발생합니다."""
    agent = builder_agent(pool or get_agent_pool(), builder_prompt(servers, skills, guest=guest))
    content, _ = await (llm or LLMCaller()).call_agent(agent, history, on_chunk=on_chunk)
    return content


# ---------------------------------------------------------------------------
# 초안 읽기·거르기
# ---------------------------------------------------------------------------


def split_reply(content: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """도우미의 응답을 (사용자에게 표시할 대화 텍스트, 파싱된 설계도 dict) 튜플로 분리합니다. 블록이 없거나 JSON 파싱에 실패하면 설계도는 None을 반환합니다."""
    text = strip_reasoning_trace(content or "")
    matches = list(_BLOCK_RE.finditer(text))
    if not matches:
        return text.strip(), None
    last = matches[-1]
    visible = (text[:last.start()] + text[last.end():]).strip()
    try:
        data = json.loads(last.group(1))
    except ValueError:
        return visible, None
    return visible, data if isinstance(data, dict) else None


def streaming_text(text: str) -> str:
    """스트리밍 응답 도중 설계도 코드 블록이 시작되는 지점 이후의 내용을 화면에서 가립니다."""
    match = _BLOCK_START_RE.search(text or "")
    return (text[:match.start()] if match else text or "").strip()


def merge_draft(current: AgentDraft, data: Optional[Dict[str, Any]]) -> AgentDraft:
    """도우미가 제안한 설계도 데이터로 기존 초안을 갱신합니다. 파싱할 수 없는 필드는 기존 값을 그대로 유지합니다."""
    if not data:
        return current
    fields = {k: v for k, v in data.items() if k in AgentDraft.model_fields}
    try:
        return AgentDraft.model_validate({**current.model_dump(), **fields})
    except ValidationError:
        return current


def sanitize_draft(draft: AgentDraft, servers: Iterable[str], skills: Iterable[str]) -> AgentDraft:
    """유효하지 않은 도구·스킬, 지원하지 않는 카드 색상 및 아이콘을 검증하여 필터링합니다."""
    server_ids, skill_ids = set(servers), set(skills)
    color = draft.card_color.lower()
    return draft.model_copy(update={
        "name": draft.name[:MAX_NAME],
        "role": draft.role[:MAX_ROLE],
        "allowed_mcp_servers": [s for s in dict.fromkeys(draft.allowed_mcp_servers) if s in server_ids],
        "allowed_skills": [s for s in dict.fromkeys(draft.allowed_skills) if s in skill_ids],
        "card_color": color if color in _COLORS else "",
        "icon": draft.icon if draft.icon in ICON_CHOICES else "",
    })


def agent_key_for(draft: AgentDraft, taken: Iterable[str]) -> str:
    """conf.json에 등록할 에이전트 고유 키를 생성합니다. 제안된 키가 유효하지 않거나 이미 존재하면 뒤에 숫자를 붙여 중복을 피합니다."""
    used = set(taken) | {"orchestrator"}
    candidate = draft.key.lower().replace("-", "_").replace(" ", "_")
    base = candidate if _KEY_RE.fullmatch(candidate) else "agent"
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def conf_block(draft: AgentDraft) -> Dict[str, Any]:
    """conf.json에 저장될 딕셔너리 구조를 생성합니다 (`add_agent_to_conf_file` 저장 포맷). UI 미리보기에 활용됩니다."""
    block: Dict[str, Any] = {"name": draft.name, "role": draft.role}
    if draft.card_color:
        block["card_color"] = draft.card_color
    if draft.icon:
        block["icon"] = draft.icon
    block["allowed_mcp_servers"] = list(draft.allowed_mcp_servers)
    if draft.allowed_skills:
        block["allowed_skills"] = list(draft.allowed_skills)
    if draft.system_prompt:
        block["system_prompt"] = draft.system_prompt.split("\n") if "\n" in draft.system_prompt else draft.system_prompt
    return block


def require_complete(draft: AgentDraft) -> None:
    """설계도의 필수 필드가 모두 채워져 있고 유효한지 검증합니다. 누락이나 오류가 있으면 `SaveRefused`를 발생시킵니다."""
    missing = [label for value, label in (
        (draft.name, "이름"), (draft.role, "한 줄 역할"), (draft.system_prompt, "업무 지시서"),
    ) if not value]
    if missing:
        raise SaveRefused(f"{', '.join(missing)} 항목을 입력해 주세요.")
    try:
        AgentConfig.model_validate(conf_block(draft))
    except ValidationError as exc:
        raise SaveRefused(f"설계도 설정 형식이 올바르지 않습니다: {exc.errors()[0].get('msg', exc)}") from exc


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------


def save_owner_agent(
    draft: AgentDraft,
    *,
    running: Sequence[str] = (),
    config_path: Optional[str | Path] = None,
) -> str:
    """conf.json에 에이전트를 영구 추가하고 실행 풀을 즉시 갱신합니다. 저장된 고유 키를 반환합니다.

    `running`은 현재 실행 중인 토론 세션 목록입니다. 로스터 관리 규칙(`_agent_admin_lock_reason`)과 동일하게,
    프로세스 전역에서 단일 에이전트 풀을 공유하므로 실행 중인 세션이 하나라도 있으면 설정을 변경하지 않습니다.
    """
    if running:
        raise SaveRefused(
            "진행 중인 대화가 있어 지금은 conf.json 설정을 변경할 수 없습니다. "
            "모든 대화가 종료된 후 다시 시도해 주세요."
        )
    require_complete(draft)
    key = agent_key_for(draft, get_config().agents.keys())
    path = config_path or active_config_path() or DEFAULT_CONFIG_PATH
    add_agent_to_conf_file(
        key,
        draft.name,
        draft.role,
        system_prompt=draft.system_prompt,
        allowed_mcp_servers=list(draft.allowed_mcp_servers),
        allowed_skills=list(draft.allowed_skills),
        card_color=draft.card_color or None,
        icon=draft.icon or None,
        config_path=path,
    )
    reload_agent_pool()
    return key


async def save_guest_agent(db: AsyncSession, user_id: str, draft: AgentDraft) -> EasyAgentModel:
    """방문자 전용 '내 에이전트'로 데이터베이스에 저장합니다. 도구는 방문자에게 허용된 것만 보관합니다."""
    require_complete(draft)
    count = (await db.execute(
        select(func.count()).select_from(EasyAgentModel).where(EasyAgentModel.user_id == user_id)
    )).scalar_one()
    if count >= MAX_GUEST_AGENTS:
        raise SaveRefused(f"내 에이전트는 최대 {MAX_GUEST_AGENTS}개까지 생성할 수 있습니다. 사용하지 않는 에이전트를 삭제한 후 다시 시도해 주세요.")
    row = EasyAgentModel(
        user_id=user_id,
        name=draft.name,
        role=draft.role,
        system_prompt=draft.system_prompt,
        allowed_mcp_servers=[s for s in draft.allowed_mcp_servers if s in GUEST_SERVERS],
        allowed_skills=list(draft.allowed_skills),
        card_color=draft.card_color,
        icon=draft.icon,
    )
    db.add(row)
    await db.commit()
    return row
