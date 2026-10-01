"""체험 템플릿 — 누가 어떤 방식으로 토론하고, 사람에게 무엇을 묻는지.

템플릿 하나는 **시작하기 전의 대화 한 벌**입니다. 참여자(인격과 기반 에이전트), 토론 방식,
토론 횟수, 커스텀 지침, 그리고 시작 양식(입력 칸과 그 값을 요청문에 끼우는 틀)을 담습니다.

## 어디에 있는가

* **공식 템플릿**은 `trial_templates/*.json` 파일입니다 (`trial.templates_dir`). 운영자가 고치고,
  DB 를 비워도 남습니다. 파일마다 따로 검사해서, 하나가 깨져도 나머지는 갤러리에 나옵니다.
* **사본**은 방문자가 템플릿을 복사해 고친 것으로, DB(`trial_template_copies`)에 템플릿 전체를
  JSON 으로 둡니다. 공식 템플릿이 바뀌어도 사본은 복사한 때 그대로입니다.

설정 파일과 같은 규칙으로 읽습니다 — `//` 로 시작하는 키는 설명이고, 여러 줄 글은 문자열
배열로 적을 수 있습니다.

## 대화로 바꾸는 법

참여자마다 `base` 에이전트(conf.json)의 운영 설정 — 모델·엔드포인트·키·샘플링 — 을 빌리고,
인격만 템플릿의 것으로 바꿉니다. 그리고 **모든 도구 할당을 해제합니다** (`allowed_mcp_servers = []`,
스킬 역시 `allowed_skills = []`).
그 구성을 대화의 `session_agents.config_snapshot` 에 미리 고정해 대화를 잠근 상태로 만듭니다.
엔진은 잠긴 대화를 스냅샷 그대로 돌리므로, conf.json 에 없는 참여자 키도 발언합니다
(`app/agents/personas.py`). 코어 엔진은 체험 서버를 모릅니다.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.personas import config_snapshot_of
from app.agents.pool import AgentPool
from app.config import PROJECT_ROOT, AgentConfig, join_text_lines, read_conf_file, strip_comment_keys
from app.database.models import SessionAgentModel, SessionModel
from app.trial.models import TrialSessionModel

ORCHESTRATOR = "orchestrator"

# 방문자가 고를 수 있는 토론 방식. 그래프 토론은 편집기가 필요해 체험에서는 뺍니다.
STRATEGY_CHOICES: Dict[str, Tuple[str, str]] = {
    "sequential_debate": ("차례로 검토", "참여자가 정해진 순서로 앞사람의 결론을 이어받아 검토합니다."),
    "adversarial_debate": ("찬반 대결", "찬성과 반대가 번갈아 맞서고, 사회자가 정리합니다."),
    "orchestrator_led": ("사회자가 지휘", "사회자가 매번 지금 필요한 사람만 골라 발언을 시킵니다."),
    "parallel_dispatch": ("동시에 의견 내기", "사회자가 일을 나눠 주고 참여자가 한꺼번에 답합니다."),
}

_KEY_RE = r"^[a-z][a-z0-9_]{0,39}$"
_INPUT_RE = r"^[a-z][a-z0-9_]{0,31}$"
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")

TEXT_UPLOAD_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".log")


class TemplateError(ValueError):
    """템플릿을 대화로 바꿀 수 없습니다 (사람에게 보여 줄 문장)."""


def _join(value: Any) -> Any:
    return join_text_lines(value) if isinstance(value, (list, tuple)) else value


class TemplateInput(BaseModel):
    id: str = Field(pattern=_INPUT_RE)
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["text", "long_text"] = "long_text"
    required: bool = True
    placeholder: str = ""
    help: str = ""
    # 텍스트 파일(.txt·.md)을 올려 이 칸을 채울 수 있게 합니다.
    allow_file: bool = False

    @field_validator("placeholder", "help", mode="before")
    @classmethod
    def _join_text(cls, value: Any) -> Any:
        return _join(value)


class TemplateParticipant(BaseModel):
    key: str = Field(pattern=_KEY_RE)
    # 모델·엔드포인트를 빌려 올 conf.json 에이전트. 비우면 같은 키, 없으면 오케스트레이터.
    base: str = ""
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(default="", max_length=120)
    system_prompt: str = ""
    stance: Optional[Literal["proponent", "critic", "neutral"]] = None
    color: str = Field(default="", max_length=40)
    # Material 아이콘 이름만 받습니다 (파일 경로는 주인 화면에서만).
    icon: str = Field(default="", pattern=r"^[a-z0-9_]{0,40}$")

    @field_validator("system_prompt", mode="before")
    @classmethod
    def _join_text(cls, value: Any) -> Any:
        return _join(value)


class TrialTemplate(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=200)
    category: str = Field(default="기타", max_length=20)
    # 갤러리 순서. 작을수록 앞입니다 (같으면 id 순).
    order: int = Field(default=100, ge=0, le=10000)
    estimated_minutes: int = Field(default=3, ge=1, le=120)
    strategy: str = "sequential_debate"
    max_rounds: int = Field(default=2, ge=1, le=10)
    result_hint: str = Field(default="", max_length=200)
    custom_instructions: str = ""
    participants: List[TemplateParticipant]
    inputs: List[TemplateInput]
    prompt: str = Field(min_length=1)
    example: Dict[str, str] = Field(default_factory=dict)
    followups: List[str] = Field(default_factory=list)

    @field_validator("summary", "result_hint", "custom_instructions", "prompt", mode="before")
    @classmethod
    def _join_text(cls, value: Any) -> Any:
        return _join(value)

    @field_validator("example", mode="before")
    @classmethod
    def _join_examples(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _join(v) for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _check(self) -> "TrialTemplate":
        if self.strategy not in STRATEGY_CHOICES:
            raise ValueError(f"토론 방식 '{self.strategy}' 은 체험에서 쓸 수 없습니다 ({', '.join(STRATEGY_CHOICES)}).")
        keys = [p.key for p in self.participants]
        if len(set(keys)) != len(keys):
            raise ValueError("참여자 키가 겹칩니다.")
        if ORCHESTRATOR not in keys:
            raise ValueError("사회자(key: orchestrator)가 있어야 합니다.")
        if len(keys) < 2:
            raise ValueError("사회자 말고 참여자가 한 명 이상 있어야 합니다.")
        # 사회자를 맨 앞에 둡니다. 나머지 순서가 곧 발언 순서입니다.
        self.participants.sort(key=lambda p: p.key != ORCHESTRATOR)
        ids = [i.id for i in self.inputs]
        if not ids:
            raise ValueError("입력 칸이 하나 이상 있어야 합니다.")
        if len(set(ids)) != len(ids):
            raise ValueError("입력 칸 id 가 겹칩니다.")
        unknown = sorted(set(_PLACEHOLDER_RE.findall(self.prompt)) - set(ids))
        if unknown:
            raise ValueError(f"요청문 틀이 없는 입력 칸을 가리킵니다: {', '.join(unknown)}")
        return self

    @property
    def specialists(self) -> List[TemplateParticipant]:
        return [p for p in self.participants if p.key != ORCHESTRATOR]


def parse_template(data: Any, fallback_id: str = "") -> TrialTemplate:
    """JSON 객체 하나를 템플릿으로 읽습니다. 틀리면 `TemplateError`."""
    if not isinstance(data, dict):
        raise TemplateError("템플릿의 최상위는 JSON 객체여야 합니다.")
    data = strip_comment_keys(data)
    if fallback_id and not data.get("id"):
        data = {**data, "id": fallback_id}
    try:
        return TrialTemplate.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        message = first.get("msg", "").removeprefix("Value error, ")
        raise TemplateError(f"{where}: {message}" if where else message) from exc


# ---------------------------------------------------------------------------
# 기반 에이전트
# ---------------------------------------------------------------------------


def base_key_for(participant: TemplateParticipant, pool: AgentPool) -> str:
    if participant.base:
        return participant.base
    return participant.key if pool.get(participant.key) is not None else ORCHESTRATOR


def pool_problems(template: TrialTemplate, pool: AgentPool) -> List[str]:
    """이 서버의 conf.json 으로는 돌릴 수 없는 이유."""
    problems = []
    for participant in template.participants:
        base = base_key_for(participant, pool)
        if pool.get(base) is None:
            problems.append(f"참여자 '{participant.name}' 의 기반 에이전트 '{base}' 가 conf.json 에 없거나 꺼져 있습니다.")
    return problems


# ---------------------------------------------------------------------------
# 공식 템플릿 폴더
# ---------------------------------------------------------------------------


@dataclass
class TemplateLoad:
    templates: Dict[str, TrialTemplate] = field(default_factory=dict)
    # 파일 이름 → 문제. 운영자 화면에만 보입니다.
    errors: Dict[str, str] = field(default_factory=dict)


def templates_dir(configured: str) -> Path:
    path = Path(configured or "trial_templates")
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_official_templates(directory: Path, pool: Optional[AgentPool] = None) -> TemplateLoad:
    """폴더의 `*.json` 을 파일 이름 순으로 읽습니다. 깨진 파일은 건너뛰고 이유를 남깁니다."""
    load = TemplateLoad()
    if not directory.is_dir():
        load.errors[str(directory)] = "템플릿 폴더가 없습니다."
        return load
    for path in sorted(directory.glob("*.json")):
        try:
            template = parse_template(read_conf_file(path), fallback_id=path.stem)
            if template.id in load.templates:
                raise TemplateError(f"id '{template.id}' 가 다른 파일과 겹칩니다.")
            if pool is not None:
                problems = pool_problems(template, pool)
                if problems:
                    raise TemplateError(problems[0])
        except (TemplateError, ValueError, OSError) as exc:
            load.errors[path.name] = str(exc)
            continue
        load.templates[template.id] = template
    load.templates = dict(sorted(load.templates.items(), key=lambda kv: (kv[1].order, kv[0])))
    return load


def role_library(templates: List[TrialTemplate]) -> List[TemplateParticipant]:
    """사본에 더할 수 있는 역할 — 공식 템플릿에 나오는 참여자 전부 (사회자 제외, 이름으로 중복 제거)."""
    seen = set()
    roles = []
    for template in templates:
        for participant in template.specialists:
            if participant.name in seen:
                continue
            seen.add(participant.name)
            roles.append(participant)
    return roles


# ---------------------------------------------------------------------------
# 템플릿 참조
# ---------------------------------------------------------------------------


def official_ref(template_id: str) -> str:
    return f"t:{template_id}"


def copy_ref(copy_id: str) -> str:
    return f"c:{copy_id}"


def split_ref(ref: str) -> Tuple[str, str]:
    """`t:<id>` → ("t", id). 모르는 모양이면 ("", "")."""
    kind, _, value = (ref or "").partition(":")
    if kind in ("t", "c") and value:
        return kind, value
    return "", ""


# ---------------------------------------------------------------------------
# 시작 양식
# ---------------------------------------------------------------------------


def input_problems(template: TrialTemplate, values: Dict[str, str], max_chars: int) -> Dict[str, str]:
    """칸 id → 사람에게 보여 줄 문제. 비어 있으면 시작해도 됩니다."""
    problems = {}
    for item in template.inputs:
        value = (values.get(item.id) or "").strip()
        if item.required and not value:
            problems[item.id] = f"{item.label}을(를) 채워 주세요."
        elif len(value) > max_chars:
            problems[item.id] = f"{max_chars:,}자까지 넣을 수 있습니다 (지금 {len(value):,}자)."
    return problems


def render_prompt(template: TrialTemplate, values: Dict[str, str]) -> str:
    """요청문 틀의 `{칸 id}` 를 채웁니다. 비운 선택 칸은 "(없음)" 입니다.

    값 안의 중괄호는 건드리지 않습니다 — 틀만 한 번 훑어 바꿉니다.
    """
    known = {item.id for item in template.inputs}

    def fill(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in known:
            return match.group(0)
        return (values.get(key) or "").strip() or "(없음)"

    return _PLACEHOLDER_RE.sub(fill, template.prompt).strip()


def session_title(template: TrialTemplate, values: Dict[str, str]) -> str:
    first = next((values.get(i.id, "") for i in template.inputs if (values.get(i.id) or "").strip()), "")
    snippet = re.sub(r"\s+", " ", first).strip()[:30]
    return f"{template.title} · {snippet}" if snippet else template.title


def decode_text_upload(filename: str, content: bytes, max_bytes: int) -> str:
    """올린 텍스트 파일을 글로 바꿉니다. 사내 PC 의 메모장 파일(CP949)도 읽습니다."""
    name = (filename or "").lower()
    if not name.endswith(TEXT_UPLOAD_SUFFIXES):
        raise TemplateError("텍스트 파일(.txt, .md, .csv)만 올릴 수 있습니다. 문서는 내용을 복사해 붙여 넣어 주세요.")
    if len(content) > max_bytes:
        raise TemplateError(f"파일이 너무 큽니다 ({len(content) // 1024:,}KB). {max_bytes // 1024:,}KB 까지 올릴 수 있습니다.")
    if b"\x00" in content[:4096]:
        raise TemplateError("텍스트 파일이 아닌 것 같습니다.")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return content.decode(encoding).replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
    raise TemplateError("글자 인코딩을 알 수 없습니다. UTF-8 로 저장해 다시 올려 주세요.")


# ---------------------------------------------------------------------------
# 대화 만들기
# ---------------------------------------------------------------------------


def participant_snapshot(participant: TemplateParticipant, pool: AgentPool, priority: int) -> Dict[str, Any]:
    """기반 에이전트의 운영 설정과 템플릿의 페르소나를 결합합니다. 외부 도구 및 스킬 할당은 모두 제거합니다."""
    base_key = base_key_for(participant, pool)
    base = pool.get(base_key)
    if base is None:
        raise TemplateError(f"참여자 '{participant.name}' 의 기반 에이전트 '{base_key}' 가 없습니다.")
    snapshot = config_snapshot_of(base)
    snapshot.update(
        name=participant.name,
        role=participant.role or base.role,
        system_prompt=participant.system_prompt or base.system_prompt or "",
        allowed_mcp_servers=[],
        allowed_skills=[],
        debate_priority=priority,
        card_color=participant.color or None,
        icon=participant.icon or None,
        enabled=True,
    )
    if participant.stance:
        snapshot["debate_stance"] = participant.stance
    # 스냅샷은 엔진이 `AgentConfig` 로 되읽습니다. 여기서 한 번 검사해 두면 깨진 대화가 생기지 않습니다.
    return AgentConfig.model_validate(snapshot).model_dump(mode="json")


async def create_trial_session(
    db: AsyncSession,
    *,
    user_id: str,
    template: TrialTemplate,
    template_ref: str,
    values: Dict[str, str],
    pool: AgentPool,
) -> str:
    """템플릿으로 잠긴 대화를 만들고 방문자에게 붙입니다. 대화 id 를 돌려줍니다."""
    snapshots = {
        p.key: participant_snapshot(p, pool, priority=(index + 1) * 10)
        for index, p in enumerate(template.participants)
    }
    keys = [p.key for p in template.participants]
    sid = str(uuid.uuid4())
    db.add(SessionModel(
        id=sid,
        title=session_title(template, values)[:255],
        strategy=template.strategy,
        max_rounds=template.max_rounds,
        parallel_limit=max(1, min(3, len(keys) - 1)),
        active_agents=keys,
        known_agents=keys,
        custom_instructions=template.custom_instructions,
        # 참여자에게 도구가 없으니 쓸 일도 없지만, 혹시 붙더라도 읽기만 하게 둡니다.
        tool_mode="read_only",
        personas_locked=True,
    ))
    for participant in template.participants:
        snapshot = snapshots[participant.key]
        db.add(SessionAgentModel(
            session_id=sid,
            agent_key=participant.key,
            name=snapshot["name"],
            role=snapshot["role"],
            system_prompt=snapshot.get("system_prompt") or "",
            card_color=snapshot.get("card_color") or "",
            icon_path=snapshot.get("icon") or "",
            config_snapshot=snapshot,
        ))
    db.add(TrialSessionModel(
        session_id=sid, user_id=user_id, template_ref=template_ref, template_title=template.title,
    ))
    await db.commit()
    return sid
