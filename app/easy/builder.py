"""대화로 에이전트 만들기 — 도우미 프롬프트, 초안 읽기·거르기, 저장.

도우미는 오케스트레이터의 연결 설정을 빌린 **도구 없는 사본**입니다 (엔진이 발언자 지명이나 요약을
받을 때 쓰는 `_tool_less` 와 같은 방식). 답마다 끝에 ```` ```agent {json}``` ```` 설계도 블록을 붙이게
하고, 화면은 그 블록을 떼어 설계도 카드에 옮깁니다. 사람은 카드에서 직접 고칠 수 있으므로 도우미가
틀려도 저장 전에 바로잡힙니다.

저장은 둘로 갈립니다.

* 주인 — conf.json 의 `agents` 에 바로 추가합니다 (`add_agent_to_conf_file`). 모델·키는 적지 않아
  `llm` 에서 상속됩니다. 로스터와 같은 이유로 진행 중인 대화가 있으면 거절합니다.
* 방문자 — `easy_agents` 테이블에만 남깁니다. conf.json 은 서버 전체가 읽는 설정이라 방문자가 쓰면
  다른 사람의 대화와 도구 권한이 바뀝니다.
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

# 설계도 블록. 업무 지시서 안에 `{고객명}` 같은 중괄호가 있어도 블록 끝(```)까지 읽습니다.
_BLOCK_RE = re.compile(r"```(?:agent|json)\s*(\{.*?\})\s*```", re.S)
_BLOCK_START_RE = re.compile(r"```(?:agent|json)")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_COLORS = {c["hex"].lower() for c in CARD_COLOR_CHOICES}

MAX_NAME = 40
MAX_ROLE = 120


class SaveRefused(ValueError):
    """저장하지 못한 이유. 화면에 그대로 보입니다."""


class AgentDraft(BaseModel):
    """설계도 — conf.json 의 에이전트 항목과 같은 이름을 씁니다 (화면이 그 대응을 보여 줍니다)."""

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

_PROMPT = """당신은 '에이전트 설계 도우미'입니다. 상대는 개발자가 아닌 직장인입니다. 대화로 그 사람이 맡기고 싶은 일을 알아내고, 그 일을 해낼 AI 에이전트의 설정을 대신 써 줍니다.

## 대화 방식
- 쉬운 한국어로 짧게 말합니다. 전문 용어를 쓰면 바로 뒤 괄호에 쉬운 말을 붙입니다.
- 한 번에 질문은 두 개까지만 합니다. 이미 들은 것은 다시 묻지 않습니다.
- 알아낼 것: ① 맡길 일과 목표 ② 다루는 자료(파일·문서·웹 등) ③ 결과물의 모양(표, 요약, 메일 초안 등) ④ 꼭 지킬 것이나 하지 말 것.
- 두세 번 오가면 충분합니다. 정보가 모자라도 그럴듯한 기본값으로 설계도를 먼저 채우고, 바꿀 점이 있는지 묻습니다.
- 설계도를 고칠 때마다 무엇을 왜 정했는지 한두 문장으로 설명합니다. 특히 도구를 고른 이유를 말합니다 (예: "파일을 읽어야 하므로 '파일 열어 보기' 도구를 줍니다").

## 에이전트의 네 가지 요소
- 페르소나: name(이름), role(한 줄 역할)
- 업무 지시서: system_prompt — 에이전트가 일할 때마다 맨 먼저 읽는 글
- 도구: allowed_mcp_servers — 실제로 무언가를 하는 손과 발. 도구가 없으면 말만 할 수 있습니다
- 스킬: allowed_skills — 필요할 때 펼쳐 보는 업무 매뉴얼

## 고를 수 있는 도구 (id 만 씁니다. 목록에 없는 것은 쓰지 않습니다)
{tools}
{guest_note}
## 고를 수 있는 스킬 (id 만 씁니다)
{skills}

## system_prompt 쓰는 법
"당신은 ..." 으로 시작해 2인칭으로 씁니다. 목표, 일하는 순서(번호 목록), 결과물의 형식, 도구를 언제 쓰는지, 하지 말아야 할 것을 담습니다. 확인할 수 있는 것은 추측하지 말고 도구로 확인하라는 문장을 넣습니다.

## 설계도 블록 — 모든 답의 맨 끝에 반드시 붙입니다
지금까지 정한 내용을 아래 형식의 블록 하나로 답 끝에 붙입니다. 아직 모르는 칸은 빈 문자열로 둡니다. 블록 밖에는 JSON 을 쓰지 않습니다.
```agent
{"key": "영문 소문자와 밑줄로 된 식별자", "name": "이름", "role": "한 줄 역할", "system_prompt": "업무 지시서", "allowed_mcp_servers": ["도구 id"], "allowed_skills": ["스킬 id"], "card_color": "#rrggbb", "icon": "아이콘 이름"}
```
- card_color 는 다음 중 하나입니다: {colors}
- icon 은 다음 중 하나입니다: {icons}

사용자 메시지 끝에 [지금 설계도] 가 붙어 오면, 사용자가 화면에서 직접 고친 값입니다. 그 값을 존중하며 이어서 고칩니다."""

_GUEST_NOTE = (
    "\n체험 방문자와 대화하고 있습니다. 체험에서는 도구가 읽기 전용으로 돕니다 — 파일을 쓰거나 코드를 "
    "실행하는 일은 거부됩니다. 대화 중에 이 점을 한 번 알려 주세요.\n"
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
    """오케스트레이터의 연결 설정으로 도는 도구 없는 도우미."""
    orchestrator = pool.get_orchestrator()
    return orchestrator.model_copy(update={
        "system_prompt": prompt,
        "allowed_mcp_servers": [],
        "allowed_skills": [],
        "sequential_thinking": orchestrator.sequential_thinking.model_copy(update={"enabled": False}),
    })


def with_draft(text: str, draft: AgentDraft) -> str:
    """사용자의 말에 화면의 설계도를 붙입니다. 사람이 직접 고친 값을 도우미가 덮어쓰지 않게 합니다."""
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
    """도우미에게 대화를 넘기고 답 전문을 받습니다. 연결이 없으면 `LLMUnavailableError`."""
    agent = builder_agent(pool or get_agent_pool(), builder_prompt(servers, skills, guest=guest))
    content, _ = await (llm or LLMCaller()).call_agent(agent, history, on_chunk=on_chunk)
    return content


# ---------------------------------------------------------------------------
# 초안 읽기·거르기
# ---------------------------------------------------------------------------


def split_reply(content: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """도우미의 답을 (사람에게 보일 글, 설계도 dict) 로 나눕니다. 블록이 없거나 깨졌으면 설계도는 None."""
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
    """스트리밍 중인 답에서 설계도 블록이 시작된 뒤를 감춥니다."""
    match = _BLOCK_START_RE.search(text or "")
    return (text[:match.start()] if match else text or "").strip()


def merge_draft(current: AgentDraft, data: Optional[Dict[str, Any]]) -> AgentDraft:
    """도우미가 준 설계도로 고칩니다. 읽을 수 없는 값이면 지금 것을 그대로 둡니다."""
    if not data:
        return current
    fields = {k: v for k, v in data.items() if k in AgentDraft.model_fields}
    try:
        return AgentDraft.model_validate({**current.model_dump(), **fields})
    except ValidationError:
        return current


def sanitize_draft(draft: AgentDraft, servers: Iterable[str], skills: Iterable[str]) -> AgentDraft:
    """없는 도구·스킬, 팔레트 밖의 색과 아이콘을 걸러 냅니다."""
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
    """conf.json 에 쓸 키. 도우미가 준 키가 쓸 수 없거나 이미 있으면 뒤에 번호를 붙입니다."""
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
    """conf.json 에 적힐 모양 (`add_agent_to_conf_file` 이 쓰는 순서). 화면의 미리보기에 씁니다."""
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
    """저장할 수 있는 설계도인지. 아니면 `SaveRefused`."""
    missing = [label for value, label in (
        (draft.name, "이름"), (draft.role, "한 줄 역할"), (draft.system_prompt, "업무 지시서"),
    ) if not value]
    if missing:
        raise SaveRefused(f"{', '.join(missing)} 칸을 채워 주십시오.")
    try:
        AgentConfig.model_validate(conf_block(draft))
    except ValidationError as exc:
        raise SaveRefused(f"설계도를 설정으로 읽을 수 없습니다: {exc.errors()[0].get('msg', exc)}") from exc


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------


def save_owner_agent(
    draft: AgentDraft,
    *,
    running: Sequence[str] = (),
    config_path: Optional[str | Path] = None,
) -> str:
    """conf.json 에 에이전트를 추가하고 풀을 다시 채웁니다. 쓴 키를 돌려줍니다.

    `running` 은 지금 토론 중인 대화들입니다. 로스터(`_agent_admin_lock_reason`)와 같은 규칙으로,
    에이전트 풀은 프로세스 전체가 하나를 쓰므로 어느 대화든 돌고 있으면 바꾸지 않습니다.
    """
    if running:
        raise SaveRefused(
            "진행 중인 대화가 있어 지금은 conf.json 을 바꿀 수 없습니다. "
            "모든 대화가 끝난 뒤 다시 저장해 주십시오."
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
    """방문자의 '내 에이전트' 로 저장합니다. 도구는 방문자가 쓸 수 있는 것만 남깁니다."""
    require_complete(draft)
    count = (await db.execute(
        select(func.count()).select_from(EasyAgentModel).where(EasyAgentModel.user_id == user_id)
    )).scalar_one()
    if count >= MAX_GUEST_AGENTS:
        raise SaveRefused(f"내 에이전트는 {MAX_GUEST_AGENTS}개까지 만들 수 있습니다. 쓰지 않는 것을 지운 뒤 저장해 주십시오.")
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
