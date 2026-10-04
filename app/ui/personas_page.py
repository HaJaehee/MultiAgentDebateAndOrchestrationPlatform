"""세션별 에이전트 페르소나 편집 페이지.

토론이 시작되기 전에만 편집할 수 있습니다. 첫 유저 메시지가 기록되면 그 시점의
페르소나가 DB 에 고정되고, 이 페이지는 읽기 전용이 됩니다.
"""

import logging
from typing import Any, Dict, Optional

from nicegui import ui
from sqlalchemy import func, select

from app.agents.base import style_for_agent
from app.agents.personas import (
    AgentPersona,
    PersonasLockedError,
    default_personas,
    effective_personas,
    persona_from_agent,
    reset_persona,
    save_persona,
    session_roster_agents,
)
from app.agents.pool import get_agent_pool
from app.config import (
    update_agent_appearance_in_conf_file,
    update_agent_persona_in_conf_file,
)
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory
from app.orchestration.runner import get_debate_runner
from app.ui.components.agent_appearance import AgentAppearanceEditor
from app.ui.theme import CUSTOM_CSS, FAVICON_SVG

logger = logging.getLogger(__name__)


def create_personas_page() -> None:
    """`/personas/{session_id}` 페이지를 등록합니다."""

    @ui.page("/personas/{session_id}", title="에이전트 페르소나 편집", favicon=FAVICON_SVG)
    async def personas_page(session_id: str):
        ui.dark_mode(True)
        ui.add_head_html(f"<style>{CUSTOM_CSS}</style>")

        session_factory = get_session_factory()
        pool = get_agent_pool()

        async with session_factory() as db:
            result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
            session_model = result.scalar_one_or_none()
            if session_model is None:
                _render_missing_session(session_id)
                return

            personas = await effective_personas(db, session_id, pool)
            # 잠긴 대화는 잠글 때 고정된 에이전트를 보여줍니다. 그 사이 conf.json 에서
            # 지워진 에이전트도 이 대화에서는 계속 발언하므로 카드가 있어야 합니다.
            agents = await session_roster_agents(db, session_model, pool)
            locked = session_model.personas_locked
            session_title = session_model.title
            msg_count = await db.scalar(
                select(func.count(MessageModel.id)).where(MessageModel.session_id == session_id)
            )

        defaults = default_personas(pool)
        inputs: Dict[str, Dict[str, Any]] = {}

        # ---------------- 헤더 ----------------
        with ui.header().classes(
            "bg-slate-900 border-b border-slate-800 px-4 py-2 items-center justify-between"
        ):
            with ui.row().classes("items-center gap-2.5"):
                ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
                    "flat dense round color=grey-4"
                ).tooltip("토론 화면으로 돌아가기")
                ui.icon("badge", size="sm").classes("text-indigo-400")
                with ui.column().classes("gap-0"):
                    ui.label("에이전트 페르소나 & 시스템 프롬프트").classes(
                        "text-base font-bold text-white tracking-wide"
                    )
                    ui.label(session_title).classes("text-[11px] text-slate-400")
            if locked:
                ui.badge("고정됨 (토론 진행 중)", color="amber-8").props("dense")
            else:
                ui.badge("편집 가능 (토론 시작 전)", color="green-7").props("dense")

        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
            # ---------------- 진행 중 안내 ----------------
            if get_debate_runner().is_running(session_id):
                with ui.card().classes(
                    "w-full bg-indigo-950/40 border border-indigo-700/60 p-3 rounded-xl"
                ):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.spinner("dots", size="sm", color="indigo-4")
                        with ui.column().classes("gap-0.5"):
                            ui.label("이 세션의 토론이 백그라운드에서 진행 중입니다.").classes(
                                "text-sm font-bold text-indigo-200"
                            )
                            ui.label(
                                "해당 화면으로 이동하셔도 토론은 중단되지 않습니다. "
                                "토론 화면으로 돌아가시면 그동안 진행된 발언이 이어서 표시됩니다."
                            ).classes("text-[11px] text-indigo-300/80 leading-relaxed")

            # ---------------- 안내 배너 ----------------
            if locked:
                with ui.card().classes(
                    "w-full bg-amber-950/40 border border-amber-800/60 p-3 rounded-xl"
                ):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon("lock", size="sm").classes("text-amber-400")
                        with ui.column().classes("gap-0.5"):
                            ui.label("이 세션의 페르소나는 고정되었습니다.").classes(
                                "text-sm font-bold text-amber-200"
                            )
                            ui.label(
                                f"메시지 {msg_count}건이 기록되어 있습니다. 토론 도중에 페르소나가 변경되면 "
                                "발언 화자의 일관성이 훼손되므로 수정할 수 없습니다. "
                                "다른 페르소나로 진행하시려면 새 세션을 시작하십시오."
                            ).classes("text-[11px] text-amber-300/80 leading-relaxed")
                    ui.button(
                        "토론 화면으로 돌아가기",
                        icon="arrow_back",
                        on_click=lambda: ui.navigate.to("/"),
                    ).props("unelevated color=amber-8 dense").classes("mt-2 text-xs").tooltip(
                        "사이드바의 '+ 새 토론 세션'으로 새 세션을 생성하시면 다시 편집하실 수 있습니다."
                    )
            else:
                with ui.card().classes(
                    "w-full bg-slate-900/70 border border-slate-800 p-3 rounded-xl"
                ):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon("edit_note", size="sm").classes("text-indigo-400")
                        with ui.column().classes("gap-0.5"):
                            ui.label("첫 메시지를 전송하기 전까지만 편집하실 수 있습니다.").classes(
                                "text-sm font-bold text-slate-100"
                            )
                            ui.label(
                                "저장된 값은 conf.json 및 현재 세션에 즉시 반영됩니다. "
                                "토론이 시작되면 해당 시점의 설정이 DB에 영구 기록되어 세션을 다시 열어도 그대로 유지됩니다."
                            ).classes("text-[11px] text-slate-400 leading-relaxed")

            # ---------------- 에이전트 카드 ----------------
            for agent in agents:
                # 이 대화에만 남은 에이전트는 conf.json 기본값이 없습니다.
                # 그때는 이 대화가 고정해 둔 값이 곧 기본값입니다.
                fallback = persona_from_agent(agent)
                persona = personas.get(agent.key) or fallback
                inputs[agent.key] = _build_agent_card(
                    agent_key=agent.key,
                    persona=persona,
                    default_persona=defaults.get(agent.key, fallback),
                    model_label=agent.model,
                    endpoint_label=agent.endpoint_label,
                    locked=locked,
                    on_save=lambda k=agent.key: _handle_save(k),
                    on_reset=lambda k=agent.key: _handle_reset(k),
                )

        # ---------------- 동작 ----------------
        async def _load_session(db):
            result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
            return result.scalar_one_or_none()

        async def _handle_save(agent_key: str) -> None:
            fields = inputs[agent_key]
            name = (fields["name"].value or "").strip()
            role = (fields["role"].value or "").strip()
            prompt = (fields["system_prompt"].value or "").strip()
            look: AgentAppearanceEditor = fields["appearance"]

            if not name:
                ui.notify("이름은 필수 입력 항목입니다.", type="warning", position="bottom-right")
                return

            async with session_factory() as db:
                current = await _load_session(db)
                if current is None:
                    ui.notify("세션을 찾을 수 없습니다.", type="negative", position="bottom-right")
                    return
                try:
                    await save_persona(
                        db, current, agent_key, name, role, prompt,
                        card_color=look.card_color, icon=look.icon,
                    )
                except PersonasLockedError:
                    ui.notify(
                        "토론이 이미 시작되어 저장할 수 없습니다. 페이지를 새로고침해 주십시오.",
                        type="negative",
                        position="bottom-right",
                    )
                    return

            try:
                # 인격을 먼저 적습니다. 이 대화에만 있던 에이전트라면 그때
                # `agents.<키>` 블록이 만들어지고, 겉모습은 그 위에 얹힙니다.
                update_agent_persona_in_conf_file(agent_key, name, role, prompt)
                update_agent_appearance_in_conf_file(agent_key, look.card_color, look.icon)
                pool.reload()
            except Exception as exc:
                logger.warning(f"conf.json 업데이트 실패: {exc}")

            fields["badge"].set_text("기본값과 다름")
            fields["badge"].set_visibility(True)
            ui.notify(f"'{name}' 페르소나를 저장하고 conf.json에 반영하였습니다.", type="positive", position="bottom-right")

        async def _handle_reset(agent_key: str) -> None:
            async with session_factory() as db:
                current = await _load_session(db)
                if current is None:
                    return
                try:
                    await reset_persona(db, current, agent_key)
                except PersonasLockedError:
                    ui.notify(
                        "토론이 이미 시작되어 되돌릴 수 없습니다.",
                        type="negative",
                        position="bottom-right",
                    )
                    return

            fields = inputs[agent_key]
            base = defaults[agent_key]
            fields["name"].value = base.name
            fields["role"].value = base.role
            fields["system_prompt"].value = base.system_prompt
            fields["appearance"].set_values(base.card_color, base.icon)
            fields["badge"].set_visibility(False)
            ui.notify("conf.json 기본값으로 초기화하였습니다.", type="info", position="bottom-right")


def _build_agent_card(
    agent_key: str,
    persona: AgentPersona,
    default_persona: AgentPersona,
    model_label: str,
    endpoint_label: str,
    locked: bool,
    on_save,
    on_reset,
) -> Dict[str, Any]:
    style = style_for_agent(agent_key, persona.card_color, persona.icon)
    fields: Dict[str, Any] = {}

    card = ui.card().classes(
        "w-full bg-slate-900 border border-slate-800 p-4 rounded-xl shadow-md gap-3"
    )
    # 목록에서 어느 카드가 어느 에이전트인지 색으로 바로 읽히게, 사람이 색을
    # 정한 에이전트는 테두리도 그 색으로 칠합니다.
    if (persona.card_color or "").strip():
        card.style(f"border-color: {style['badge_color']}")

    with card:
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-center gap-2.5 min-w-0"):
                header_avatar = ui.element("div").classes("flex-shrink-0")
                with header_avatar:
                    ui.avatar(style["avatar"], color=style["color"], text_color="white", size="sm")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(agent_key).classes("text-xs font-mono text-slate-500")
                    ui.label(model_label).classes("text-[12px] text-slate-500 truncate")
            with ui.row().classes("items-center gap-1.5"):
                badge = ui.badge("기본값과 다름", color="indigo-7").props("dense")
                badge.tooltip("conf.json의 전역 기본값과 다른 설정이 현재 세션에 적용되어 있습니다.")
                badge.set_visibility(persona.is_customized)
                fields["badge"] = badge
                if agent_key == "orchestrator":
                    ui.badge("필수", color="indigo-9").props("dense")

        common = "outlined dark dense" + (" readonly" if locked else "")

        with ui.row().classes("w-full gap-3 no-wrap"):
            fields["name"] = (
                ui.input(label="이름", value=persona.name)
                .props(common)
                .classes("flex-grow text-sm")
            )
            fields["role"] = (
                ui.input(label="역할", value=persona.role)
                .props(common)
                .classes("flex-grow text-sm")
            )

        fields["system_prompt"] = (
            ui.textarea(label="시스템 프롬프트", value=persona.system_prompt)
            .props(common + " autogrow rows=5")
            .classes("w-full text-xs font-mono")
        )

        with ui.row().classes("w-full items-center gap-1.5 mt-1"):
            ui.icon("palette", size="xs").classes("text-slate-500")
            ui.label("카드 색 & 아이콘").classes(
                "text-[11px] font-semibold text-slate-400"
            )
        appearance = AgentAppearanceEditor(
            agent_key,
            card_color=persona.card_color,
            icon=persona.icon,
            enabled=not locked,
            # 머리글의 아바타와 카드 테두리도 고르는 즉시 따라갑니다. 저장하기
            # 전에 결과를 볼 수 없으면 색을 고르는 일이 시행착오가 됩니다.
            on_change=lambda: _repaint_card_header(card, header_avatar, agent_key, appearance),
        )
        fields["appearance"] = appearance

        with ui.row().classes("w-full items-center justify-between"):
            ui.label(f"엔드포인트: {endpoint_label}").classes("text-[12px] text-slate-500 truncate")
            if not locked:
                with ui.row().classes("gap-2"):
                    ui.button("기본값으로", icon="restart_alt", on_click=on_reset).props(
                        "flat dense color=slate-4"
                    ).classes("text-xs").tooltip(
                        f"conf.json 기본 설정값으로 되돌립니다 ({default_persona.name})."
                    )
                    ui.button("저장", icon="save", on_click=on_save).props(
                        "unelevated dense color=indigo-6"
                    ).classes("text-xs px-3")

    return fields


def _repaint_card_header(
    card: ui.card,
    avatar_slot: ui.element,
    agent_key: str,
    appearance: "AgentAppearanceEditor",
) -> None:
    """편집기에서 고른 값으로 카드 머리글의 아바타와 테두리를 다시 칠합니다."""
    style = style_for_agent(agent_key, appearance.card_color, appearance.icon)
    if not avatar_slot.is_deleted:
        avatar_slot.clear()
        with avatar_slot:
            ui.avatar(style["avatar"], color=style["color"], text_color="white", size="sm")
    if not card.is_deleted:
        card.style(
            replace=f"border-color: {style['badge_color']}" if appearance.card_color else ""
        )


def _render_missing_session(session_id: str) -> None:
    with ui.column().classes("w-full h-screen items-center justify-center gap-3"):
        ui.icon("search_off", size="xl").classes("text-slate-600")
        ui.label("세션을 찾을 수 없습니다.").classes("text-lg font-bold text-slate-300")
        ui.label(session_id).classes("text-xs font-mono text-slate-600")
        ui.button("토론 화면으로", icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
            "unelevated color=indigo-6"
        )
