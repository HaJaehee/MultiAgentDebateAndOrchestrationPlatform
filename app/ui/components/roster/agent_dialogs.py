"""로스터 패널 — 카드별 창: 도구 할당, 동료 자문(peers) 상대, 스킬 할당, 입력 모달."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

from app.agents.base import Agent
from app.agents.host_tools import assignable_plugin_tools
from app.agents.media import MAX_ATTACHMENT_BYTES, MODALITY_LABELS, extensions_of
from app.config import (
    DEFAULT_DECISION_THRESHOLD,
    DEFAULT_DECISION_TIMEOUT,
    DEFAULT_PEER_TOOL_ITERATIONS,
    INPUT_MODALITIES,
    ORCHESTRATOR_KEY,
    PEERS_TOOL_PREFIX,
    TOOL_ITERATION_CEILING,
)
from app.orchestration.roster_service import RosterService
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.common import peer_candidates, peer_selection_summary, unknown_tool_servers
from app.ui.components.roster.state import RosterState
from app.ui.components.roster.widgets import peers_choice, plugin_tool_choice, skill_state_badge
from app.workspace_files import format_size


class AgentDialogs:
    """카드별 창: 도구 할당, 동료 자문(peers) 상대, 스킬 할당, 입력 모달."""

    def __init__(self, state: RosterState, service: RosterService, changes: RosterChanges):
        self.state = state
        self.service = service
        self.changes = changes

    def open_tools(self, agent_key: str) -> None:
        """이 에이전트가 호출할 수 있는 MCP 서버(와 내장 도구 peers)를 고릅니다.

        peers 는 conf.json 의 mcp_servers 에 없어도 늘 목록에 있습니다 — 켜는 법이 여기서
        체크하는 것(allowed_mcp_servers 에 "peers")뿐이기 때문입니다.

        도구 할당은 conf.json 에 저장되어 **아직 시작하지 않은 대화**에 걸립니다.
        이미 시작한 대화는 첫 발언 때 도구 권한까지 스냅샷으로 고정되었으므로 여기서
        바꿔도 그 대화에는 닿지 않습니다. 그래서 다른 구성 변경과 같은 잠금을
        씁니다 (`RosterState.admin_lock_reason`) — 닿지 않는 조작을 열어 두면 바꿨다고
        착각하게 됩니다.
        """
        if self.changes.blocked_for_agent_admin():
            return

        agent = self.state.find_agent(agent_key)
        if agent is None:
            ui.notify(f"'{agent_key}' 에이전트를 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        status, configured = self.service.mcp_view(self.state.workspace_dir)

        allowed = list(agent.allowed_mcp_servers or [])
        # conf.json 에서 이미 사라진 서버가 아직 적혀 있을 수 있습니다. 조용히
        # 지우지 않고 보여주고, 사용자가 체크를 풀어 정리하게 합니다. peers 는 빼고요.
        orphans = unknown_tool_servers(allowed, configured)
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[520px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · 도구 할당").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 발언 중 호출할 수 있는 MCP 서버입니다. conf.json 에 저장되어 "
                "아직 시작하지 않은 대화와 앞으로 만드는 대화에 적용됩니다. 이미 시작한 "
                "대화는 그때 고정한 도구 권한을 그대로 씁니다."
            ).classes("text-[12px] text-amber-400 mb-2 leading-snug")

            if not configured and not orphans:
                ui.label("conf.json 에 등록된 MCP 서버가 없습니다.").classes("text-[13px] text-slate-400")

            with ui.column().classes("w-full gap-1 max-h-[46vh] overflow-y-auto"):
                for name, server_cfg in configured.items():
                    info = status.get(name)
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=name in allowed).props("dense dark color=indigo-4")
                        ui.label(name).classes("text-[13px] font-semibold text-slate-200")
                        if not server_cfg.enabled:
                            ui.badge("꺼짐", color="grey-8").props("dense").classes("text-[13px]")
                            ui.label("서버가 꺼져 있어 지금은 도구가 제공되지 않습니다").classes(
                                "text-[11px] text-slate-500 truncate"
                            )
                        elif info and info["connected"]:
                            ui.badge(f"도구 {info['tool_count']}", color="green-8").props("dense").classes("text-[13px]")
                        else:
                            ui.badge("연결 안 됨", color="red-9").props("dense").classes("text-[13px]")

                # peers 자문 대상 및 도구 상한. '에이전트 선택' 다이얼로그에서 설정하고 '저장' 시 함께 영속화.
                peer_state: Dict[str, Any] = {
                    "targets": agent.peer_consultation.targets,
                    "max": agent.peer_consultation.max_tool_iterations,
                }
                peer_original = dict(peer_state)
                summary_holder: Dict[str, Any] = {}

                def refresh_peer_summary() -> None:
                    label = summary_holder.get("label")
                    if label is not None and not label.is_deleted:
                        label.set_text(peer_selection_summary(peer_state["targets"], peer_state["max"]))

                summary_holder["label"] = peers_choice(
                    boxes,
                    checked=PEERS_TOOL_PREFIX in allowed,
                    agent_key=agent.key,
                    on_select=lambda: self.open_peer_targets(agent, peer_state, refresh_peer_summary),
                    summary=peer_selection_summary(peer_state["targets"], peer_state["max"]),
                )

                for provider in assignable_plugin_tools():
                    plugin_tool_choice(boxes, provider, checked=provider.prefix in allowed)

                for name in orphans:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=True).props("dense dark color=amber-6")
                        ui.label(name).classes("text-[13px] font-semibold text-amber-300")
                        ui.badge("설정에 없음", color="amber-9").props("dense").classes("text-[13px]")

            st = agent.sequential_thinking
            if st.enabled and st.mode == "mcp":
                ui.label(
                    f"참고: 이 에이전트는 sequential_thinking 을 mcp 모드로 쓰므로 "
                    f"'{st.mcp_server}' 서버가 여기 선택과 무관하게 자동으로 포함됩니다."
                ).classes("text-[11px] text-slate-500 mt-2 leading-snug")

            async def do_save() -> None:
                servers = [name for name, box in boxes.items() if box.value]
                peers_changed = peer_state != peer_original
                dialog.close()

                def write() -> None:
                    self.service.set_agent_tools(agent_key, servers)
                    if peers_changed:
                        self.service.set_agent_peer_consultation(agent_key, peer_state["targets"], peer_state["max"])

                await self.changes.apply_conf_change(
                    write,
                    f"'{agent.name}' 에이전트의 도구를 {len(servers)}개로 저장하였습니다."
                    if servers else f"'{agent.name}' 에이전트에 도구를 할당하지 않았습니다.",
                    # 서버 구성은 그대로입니다. 다시 띄울 이유가 없습니다.
                    restart_servers=False,
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()

    def open_peer_targets(
        self, agent: Agent, state: Dict[str, Any], on_done: Callable[[], None]
    ) -> None:
        """peers 질의 대상 에이전트와 피자문 전문가의 도구 호출 상한을 설정합니다.

        고른 값은 도구 할당 창의 `state` 에만 담고, 그 창의 '저장' 이 도구 할당과 함께 conf.json 에
        씁니다. 저장된 값은 아직 시작하지 않은 대화에 걸리고, 대화가 시작되면 첫 발언 때의 다른
        에이전트 설정과 함께 그 대화의 스냅샷(DB)에 고정됩니다.
        """
        candidates = peer_candidates(self.state.roster_agents(), agent.key)
        candidate_keys = {a.key for a in candidates}
        chosen = set(state["targets"] or [])
        stale = sorted(chosen - candidate_keys)
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[580px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · peers 자문 상대").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 토론 중 peers 로 질문할 수 있는 상대를 고릅니다. conf.json 에 저장되어 아직 "
                "시작하지 않은 대화와 앞으로 만드는 대화에 적용되고, 대화가 시작되면 그 대화에 고정됩니다."
            ).classes("text-[12px] text-amber-400 mb-1 leading-snug")

            mode = ui.radio(
                {
                    "all": "이번 턴에 참여한 다른 전문가 전원 (기본)",
                    "pick": "아래에서 고른 전문가만 — 토론에 참여하지 않는 전문가도 고를 수 있습니다",
                },
                value="all" if state["targets"] is None else "pick",
            ).props("dense dark color=indigo-4").classes("text-[13px]")

            with ui.column().classes("w-full gap-1 max-h-[40vh] overflow-y-auto pl-6 mt-1"):
                if not candidates:
                    ui.label("고를 수 있는 다른 전문가가 없습니다.").classes("text-[12px] text-slate-400")
                for peer in candidates:
                    participating = self.state.participates(peer.key)
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        box = ui.checkbox(value=peer.key in chosen).props("dense dark color=indigo-4")
                        box.bind_enabled_from(mode, "value", backward=lambda v: v == "pick")
                        boxes[peer.key] = box
                        ui.avatar(peer.avatar, color=peer.color, text_color="white", size="22px").classes(
                            "flex-shrink-0"
                        )
                        with ui.column().classes("gap-0 min-w-0 flex-grow"):
                            ui.label(peer.name).classes("text-[13px] font-semibold text-slate-200 truncate")
                            ui.label(peer.role).classes("text-[11px] text-slate-400 truncate")
                        ui.badge(
                            "토론 참여" if participating else "토론 불참",
                            color="teal-8" if participating else "grey-8",
                        ).props("dense").classes("text-[11px] flex-shrink-0")
            if stale:
                ui.label(
                    f"이 대화의 에이전트가 아닌 상대({', '.join(stale)})는 확인하면 빠집니다."
                ).classes("text-[11px] text-amber-300 mt-1")

            with ui.row().classes("w-full items-center gap-2 mt-3 no-wrap"):
                limit_in = ui.number(
                    "피자문 도구 호출 상한", value=state["max"], min=0,
                    max=TOOL_ITERATION_CEILING, step=1, format="%.0f",
                ).props("outlined dense dark").classes("w-56 text-xs")
                ui.label(
                    "자문 한 번에 상대가 쓸 수 있는 도구 호출 수. 0 이면 도구 없이 답합니다 "
                    "(상대 자신의 상한은 넘지 않습니다)."
                ).classes("text-[11px] text-slate-500 leading-snug")

            def confirm() -> None:
                try:
                    limit = int(limit_in.value if limit_in.value is not None else DEFAULT_PEER_TOOL_ITERATIONS)
                except (TypeError, ValueError):
                    limit = DEFAULT_PEER_TOOL_ITERATIONS
                state["max"] = max(0, min(TOOL_ITERATION_CEILING, limit))
                state["targets"] = (
                    None if mode.value == "all"
                    else [key for key, box in boxes.items() if box.value]
                )
                dialog.close()
                on_done()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("확인", on_click=confirm).props("unelevated color=indigo-6")

        dialog.open()

    def open_skills(self, agent_key: str) -> None:
        """이 에이전트가 불러 쓸 수 있는 스킬을 고릅니다.

        도구 할당과 같습니다 — conf.json 에 저장되어 **아직 시작하지 않은 대화**에 걸리고,
        이미 시작한 대화는 첫 발언 때 고정된 할당을 그대로 씁니다. 그래서 같은 잠금을 씁니다.
        스킬의 내용과 켜기·끄기는 이와 달리 어느 대화에나 바로 걸립니다.
        """
        if self.changes.blocked_for_agent_admin():
            return

        agent = self.state.find_agent(agent_key)
        if agent is None:
            ui.notify(f"'{agent_key}' 에이전트를 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        skills, root = self.service.skills_view()

        allowed = list(agent.allowed_skills or [])
        # 폴더에서 이미 사라진 스킬이 아직 적혀 있을 수 있습니다. 조용히 지우지 않고
        # 보여주고, 사용자가 체크를 풀어 정리하게 합니다.
        known = {s.name for s in skills}
        orphans = [name for name in allowed if name not in known]
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[560px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · 스킬 할당").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 발언 중 불러 사용할 수 있는 스킬입니다. conf.json에 저장되어 아직 "
                "시작되지 않은 세션과 향후 생성할 세션에 적용됩니다. 이미 시작된 세션은 시작 시점의 "
                "할당 구성을 그대로 유지합니다. 스킬의 본문 내용과 활성화/비활성화 상태는 모든 세션에 즉시 적용됩니다."
            ).classes("text-[12px] text-amber-400 mb-2 leading-snug")

            if not skills and not orphans:
                ui.label(
                    f"스킬 폴더({root})에 등록된 스킬이 없습니다. `<이름>/SKILL.md` 폴더를 추가하면 목록에 표시됩니다."
                ).classes("text-[13px] text-slate-400")

            with ui.column().classes("w-full gap-1 max-h-[46vh] overflow-y-auto"):
                for skill in skills:
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        boxes[skill.name] = ui.checkbox(value=skill.name in allowed).props(
                            "dense dark color=sky-4"
                        )
                        with ui.column().classes("gap-0 min-w-0"):
                            with ui.row().classes("items-center gap-1 no-wrap"):
                                ui.label(skill.name).classes("text-[13px] font-semibold text-slate-200")
                                skill_state_badge(skill, size="text-[13px]")
                            ui.label(skill.problem or skill.description).classes(
                                "text-[11px] leading-snug "
                                + ("text-rose-300" if skill.problem else "text-slate-400")
                            )

                for name in orphans:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=True).props("dense dark color=amber-6")
                        ui.label(name).classes("text-[13px] font-semibold text-amber-300")
                        ui.badge("폴더에 없음", color="amber-9").props("dense").classes("text-[13px]")

            async def do_save() -> None:
                chosen = [name for name, box in boxes.items() if box.value]
                dialog.close()
                await self.changes.apply_conf_change(
                    lambda: self.service.set_agent_skills(agent_key, chosen),
                    f"'{agent.name}' 에이전트의 스킬을 {len(chosen)}개로 저장하였습니다."
                    if chosen else f"'{agent.name}' 에이전트에 스킬을 할당하지 않았습니다.",
                    # 서버 구성은 그대로입니다. 다시 띄울 이유가 없습니다.
                    restart_servers=False,
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()

    def open_modalities(self, agent_key: str) -> None:
        """@언급한 파일 중 이 에이전트의 모델에게 내용을 직접 전달할 형식을 고릅니다.

        도구·스킬 할당과 같습니다 — conf.json 에 저장되어 **아직 시작하지 않은 대화**에 걸리고, 이미 시작한
        대화는 첫 발언 때 고정된 값을 그대로 씁니다. 그래서 같은 잠금을 씁니다. 모두 비워 두면 글과 파일 경로만
        받습니다.
        """
        if self.changes.blocked_for_agent_admin():
            return

        agent = self.state.find_agent(agent_key)
        if agent is None:
            ui.notify(f"'{agent_key}' 에이전트를 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        allowed = set(agent.allowed_modalities or [])
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[520px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · 입력 모달").classes("text-lg font-bold")
            ui.label(
                "입력창에서 @언급한 파일 중 체크한 형식은 경로와 함께 내용을 BASE64 로 이 에이전트의 모델에게 직접 "
                "전달합니다. 모두 비워 두면 기본 동작대로 글과 파일 경로만 받습니다. conf.json 에 저장되어 아직 "
                "시작되지 않은 세션과 향후 생성할 세션에 적용되고, 이미 시작된 세션은 시작 시점의 구성을 유지합니다."
            ).classes("text-[12px] text-amber-400 mb-2 leading-snug")

            with ui.column().classes("w-full gap-1"):
                for kind in INPUT_MODALITIES:
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        boxes[kind] = ui.checkbox(value=kind in allowed).props("dense dark color=purple-3")
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label(MODALITY_LABELS[kind]).classes("text-[13px] font-semibold text-slate-200")
                            ui.label(", ".join(extensions_of(kind))).classes(
                                "text-[11px] leading-snug text-slate-400"
                            )

            ui.label(
                f"파일당 최대 {format_size(MAX_ATTACHMENT_BYTES)}이며, 언급한 턴의 발언에만 실립니다. 도구가 돌려준 "
                f"이미지·파일도 같은 체크를 따릅니다. 모델이 받지 못하는 형식이면 요청이 거절되므로, 그때는 내용 없이 "
                f"발언을 이어 가고 발언 앞에 그 사실을 적습니다."
            ).classes("text-[11px] text-slate-500 mt-2 leading-snug")

            async def do_save() -> None:
                chosen = [kind for kind, box in boxes.items() if box.value]
                dialog.close()
                labels = ", ".join(MODALITY_LABELS[kind] for kind in chosen)
                await self.changes.apply_conf_change(
                    lambda: self.service.set_agent_modalities(agent_key, chosen),
                    f"'{agent.name}' 에이전트가 {labels} 파일을 직접 받도록 저장하였습니다."
                    if chosen else f"'{agent.name}' 에이전트는 글과 파일 경로만 받도록 저장하였습니다.",
                    # 서버 구성은 그대로입니다. 다시 띄울 이유가 없습니다.
                    restart_servers=False,
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()

    def open_model_settings(self, agent_key: str) -> None:
        """에이전트 풀에서 System 2 생성형 LLM ↔ System 1 결정 LLM(Jev/Laya)을 전환하고 모델 설정을 저장합니다."""
        if self.changes.blocked_for_agent_admin():
            return

        agent = self.state.find_agent(agent_key)
        if agent is None:
            ui.notify(f"'{agent_key}' 에이전트를 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        raw_entry = self.service.raw_agent_entry(agent_key) or {}
        raw_dl = raw_entry.get("decision_llm")
        raw_dl = dict(raw_dl) if isinstance(raw_dl, dict) else {}
        try:
            cfg = self.service.config()
        except Exception:  # noqa: BLE001
            cfg = None
        global_llm = getattr(cfg, "llm", None)

        is_orchestrator = agent.key == ORCHESTRATOR_KEY
        dcfg = agent.decision_llm

        s2_init_model = str(raw_entry.get("model") or "")
        s2_init_provider = str(raw_entry.get("provider") or raw_entry.get("custom_llm_provider") or "")
        s2_init_base = str(raw_entry.get("api_base") or raw_entry.get("api_url") or raw_entry.get("base_url") or "")
        has_s2_conf_key = bool(str(raw_entry.get("api_key") or "").strip())

        dl_init_model = str(raw_dl.get("model") or "")
        dl_init_base = str(raw_dl.get("api_base") or raw_dl.get("api_url") or raw_dl.get("base_url") or "")
        dl_init_path = str(raw_dl.get("model_path") or "")
        has_dl_conf_key = bool(str(raw_dl.get("api_key") or "").strip())

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[620px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label(f"{agent.name} · 모델 스위칭 & 설정").classes("text-lg font-bold")
                ui.badge(
                    f"현재: System 1 ({dcfg.backend.upper()})" if dcfg.enabled else "현재: System 2 (기본)",
                    color="cyan-8" if dcfg.enabled else "indigo-8",
                ).props("dense").classes("text-xs")
            ui.label(
                "conf.json 에 저장되어 아직 시작하지 않은 세션과 향후 생성할 세션에 적용됩니다. "
                "API 키는 1차적으로 .env(JEV_API_KEY, LAYA_API_KEY 등)에서 읽고, 아래에서 입력한 값만 conf.json 오버라이드로 기록됩니다."
            ).classes("text-[12px] text-amber-400 mb-1 leading-snug")

            with ui.column().classes("w-full gap-2.5 max-h-[64vh] overflow-y-auto pr-1"):
                mode_radio = ui.radio(
                    {
                        "system2": "System 2 — 생성형 LLM (기본값 · 산문 추론 및 도구 루프)",
                        "system1": "System 1 — 결정 LLM (Jev / Laya · 단일 패스 확률·척도·선택 판정)",
                    },
                    value="system1" if dcfg.enabled else "system2",
                ).props("dense dark color=cyan-4").classes("text-[13px] font-semibold")

                ui.label(
                    (
                        "오케스트레이터에서 System 1을 켜면 그래프 게이트 분기·발언자 지명·서브 토론 개시 판단을 "
                        "Jev/Laya 단일 패스로 수행하고, 계획 수립·결정 장부·최종 결론 합성은 아래 System 2 모델을 유지합니다."
                    )
                    if is_orchestrator
                    else (
                        "전문가 에이전트에서 System 1을 켜면 발언 차례에 도구 루프 대신 Jev/Laya(/v1/systemone) "
                        "단일 패스로 타당성 확률(noul)·품질 척도(score)·권고 조치(choice)를 평가합니다."
                    )
                ).classes("text-[11px] text-slate-400 -mt-1 leading-snug")

                with ui.column().classes(
                    "w-full gap-2 p-2.5 rounded-lg bg-slate-800/60 border border-cyan-900/60"
                ) as s1_box:
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("⚡ System 1 결정 LLM 설정 (Jev / Laya)").classes(
                            "text-xs font-bold text-cyan-300"
                        )
                        if has_dl_conf_key:
                            ui.badge("conf.json 키 오버라이드 중", color="amber-8").props("dense").classes("text-[11px]")
                        elif dcfg.has_env_api_key:
                            ui.badge(".env 1차 키 연결됨", color="teal-8").props("dense").classes("text-[11px]")
                        else:
                            ui.badge("API 키 미설정 (.env 권장)", color="grey-8").props("dense").classes("text-[11px]")

                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        dl_backend = ui.select(
                            {
                                "jev": "Jev (TypeSafe AI · 호스팅 API)",
                                "laya": "Laya (Convai · 오픈웨이트 로컬/HTTP)",
                            },
                            value=dcfg.backend,
                            label="결정 모델 백엔드",
                        ).props("outlined dense dark options-dense").classes("w-64 text-xs")
                        dl_model_in = ui.input(
                            "결정 모델 이름 (선택)",
                            value=dl_init_model,
                            placeholder=f"비우면 기본값 ({dcfg.effective_model})",
                        ).props("outlined dense dark").classes("flex-grow min-w-[180px] text-xs")

                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        dl_base_in = ui.input(
                            "결정 API URL 오버라이드 (선택)",
                            value=dl_init_base,
                            placeholder="비우면 .env 또는 백엔드 기본 엔드포인트",
                        ).props("outlined dense dark").classes("flex-grow min-w-[200px] text-xs")
                        dl_path_in = ui.input(
                            "Laya 로컬 모델 경로 (선택)",
                            value=dl_init_path,
                            placeholder="비우면 .env LAYA_MODEL_PATH",
                        ).props("outlined dense dark").classes("flex-grow min-w-[180px] text-xs")

                    with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                        dl_threshold_in = ui.number(
                            "판정 임계치 (threshold)",
                            value=float(dcfg.threshold),
                            min=0.0,
                            max=1.0,
                            step=0.05,
                            format="%.2f",
                        ).props("outlined dense dark").classes("w-44 text-xs")
                        dl_timeout_in = ui.number(
                            "타임아웃(초)",
                            value=float(dcfg.timeout),
                            min=1.0,
                            max=300.0,
                            step=1.0,
                            format="%.0f",
                        ).props("outlined dense dark").classes("w-36 text-xs")

                    dl_key_in = ui.input(
                        "결정 API 키 오버라이드 (conf.json)",
                        value="",
                        placeholder="비워 두면 .env(JEV_API_KEY / LAYA_API_KEY / DECISION_API_KEY)를 그대로 사용",
                    ).props("outlined dense dark type=password").classes("w-full text-xs")
                    clear_dl_key_cb: Optional[ui.checkbox] = None
                    if has_dl_conf_key:
                        clear_dl_key_cb = ui.checkbox(
                            "conf.json 결정 API 키 오버라이드 제거 (.env 1차 키로 복귀)",
                            value=False,
                        ).props("dense dark color=amber-5").classes("text-xs text-amber-300")

                with ui.column().classes(
                    "w-full gap-2 p-2.5 rounded-lg bg-slate-800/40 border border-slate-700/80"
                ):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("🤖 System 2 생성형 LLM 오버라이드").classes(
                            "text-xs font-bold text-indigo-300"
                        )
                        if has_s2_conf_key:
                            ui.badge("conf.json 키 오버라이드 중", color="amber-8").props("dense").classes("text-[11px]")
                        else:
                            ui.badge("전역 llm / .env 상속", color="indigo-9").props("dense").classes("text-[11px]")

                    inherited_model = getattr(global_llm, "model", None) or agent.model
                    inherited_base = getattr(global_llm, "api_base", None) or "Provider 기본 주소"
                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        s2_model_in = ui.input(
                            "System 2 모델 (비우면 전역 llm 상속)",
                            value=s2_init_model,
                            placeholder=f"전역 기본값: {inherited_model}",
                        ).props("outlined dense dark").classes("flex-grow min-w-[200px] text-xs")
                        s2_provider_in = ui.input(
                            "Provider (선택)",
                            value=s2_init_provider,
                            placeholder=getattr(global_llm, "provider", None) or "자동 판별",
                        ).props("outlined dense dark").classes("w-44 text-xs")

                    s2_base_in = ui.input(
                        "System 2 API URL (비우면 전역 llm 상속)",
                        value=s2_init_base,
                        placeholder=f"전역 기본값: {inherited_base}",
                    ).props("outlined dense dark").classes("w-full text-xs")
                    s2_key_in = ui.input(
                        "System 2 API 키 오버라이드 (conf.json)",
                        value="",
                        placeholder="비워 두면 전역 llm / .env 의 API 키를 그대로 상속",
                    ).props("outlined dense dark type=password").classes("w-full text-xs")
                    clear_s2_key_cb: Optional[ui.checkbox] = None
                    if has_s2_conf_key:
                        clear_s2_key_cb = ui.checkbox(
                            "conf.json System 2 API 키 오버라이드 제거 (전역 llm / .env 로 복귀)",
                            value=False,
                        ).props("dense dark color=amber-5").classes("text-xs text-amber-300")

                def _sync_s1_emphasis() -> None:
                    if s1_box.is_deleted:
                        return
                    if mode_radio.value == "system1":
                        s1_box.classes(remove="opacity-70", add="border-cyan-500/70")
                    else:
                        s1_box.classes(remove="border-cyan-500/70", add="opacity-70")

                mode_radio.on_value_change(lambda _: _sync_s1_emphasis())
                _sync_s1_emphasis()

            async def do_save() -> None:
                enable_s1 = mode_radio.value == "system1"
                backend_val = str(dl_backend.value or "jev").strip().lower()
                try:
                    threshold_val = float(
                        dl_threshold_in.value if dl_threshold_in.value is not None else DEFAULT_DECISION_THRESHOLD
                    )
                except (TypeError, ValueError):
                    threshold_val = DEFAULT_DECISION_THRESHOLD
                threshold_val = max(0.0, min(1.0, threshold_val))

                try:
                    timeout_val = float(
                        dl_timeout_in.value if dl_timeout_in.value is not None else DEFAULT_DECISION_TIMEOUT
                    )
                except (TypeError, ValueError):
                    timeout_val = DEFAULT_DECISION_TIMEOUT
                timeout_val = max(1.0, min(300.0, timeout_val))

                dl_payload: Dict[str, Any] = {
                    "enabled": enable_s1,
                    "backend": backend_val,
                    "threshold": threshold_val,
                    "timeout": timeout_val,
                }
                for field_name, widget, init_val in (
                    ("model", dl_model_in, dl_init_model),
                    ("api_base", dl_base_in, dl_init_base),
                    ("model_path", dl_path_in, dl_init_path),
                ):
                    cur_val = (widget.value or "").strip()
                    if cur_val != init_val.strip():
                        dl_payload[field_name] = cur_val

                new_dl_key = (dl_key_in.value or "").strip()
                if new_dl_key:
                    dl_payload["api_key"] = new_dl_key
                    clear_dl_key = False
                else:
                    clear_dl_key = bool(clear_dl_key_cb is not None and clear_dl_key_cb.value)

                s2_overrides: Dict[str, Any] = {}
                s2_clears: List[str] = []
                for field_name, widget, init_val in (
                    ("model", s2_model_in, s2_init_model),
                    ("provider", s2_provider_in, s2_init_provider),
                    ("api_base", s2_base_in, s2_init_base),
                ):
                    cur_val = (widget.value or "").strip()
                    if cur_val != init_val.strip():
                        if cur_val:
                            s2_overrides[field_name] = cur_val
                        elif init_val.strip():
                            s2_clears.append(field_name)

                new_s2_key = (s2_key_in.value or "").strip()
                if new_s2_key:
                    s2_overrides["api_key"] = new_s2_key
                elif clear_s2_key_cb is not None and clear_s2_key_cb.value:
                    s2_clears.append("api_key")

                dialog.close()
                summary_mode = (
                    f"System 1 결정 LLM ({backend_val.upper()})"
                    if enable_s1
                    else f"System 2 생성형 LLM ({s2_overrides.get('model') or agent.model})"
                )
                await self.changes.apply_agent_change(
                    lambda: self.service.set_agent_model_settings(
                        agent_key,
                        dl_payload,
                        clear_decision_api_key=clear_dl_key,
                        system2_overrides=s2_overrides or None,
                        clear_system2_fields=s2_clears or None,
                    ),
                    f"'{agent.name}' 에이전트의 모델 모드를 {summary_mode}(으)로 저장하였습니다.",
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()
