"""템플릿 참조(`t:<id>` · `c:<사본 id>`)를 실제 템플릿으로 바꿉니다.

공식 템플릿은 부를 때마다 폴더에서 다시 읽습니다. 파일 몇 개라 싸고, 운영자가 파일을 고치면
앱을 다시 띄우지 않아도 다음 화면부터 반영됩니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pool import get_agent_pool
from app.config import get_config
from app.trial.models import TrialTemplateCopyModel
from app.trial.store import copy_template, get_copy
from app.trial.templates import (
    TemplateError,
    TemplateLoad,
    TrialTemplate,
    load_official_templates,
    pool_problems,
    split_ref,
    templates_dir,
)


def official_templates() -> TemplateLoad:
    return load_official_templates(templates_dir(get_config().trial.templates_dir), get_agent_pool())


@dataclass
class ResolvedTemplate:
    ref: str
    template: TrialTemplate
    copy: Optional[TrialTemplateCopyModel] = None
    # 사본인데 지금 conf.json 으로는 돌릴 수 없는 이유 (기반 에이전트가 사라진 경우 등).
    problem: str = ""

    @property
    def is_copy(self) -> bool:
        return self.copy is not None


async def resolve_template(db: AsyncSession, visitor_id: str, ref: str) -> Optional[ResolvedTemplate]:
    kind, value = split_ref(ref)
    if kind == "t":
        template = official_templates().templates.get(value)
        return ResolvedTemplate(ref, template) if template is not None else None
    if kind == "c":
        copy = await get_copy(db, visitor_id, value)
        if copy is None:
            return None
        try:
            template = copy_template(copy)
        except TemplateError:
            return None
        problems = pool_problems(template, get_agent_pool())
        return ResolvedTemplate(ref, template, copy, problems[0] if problems else "")
    return None
