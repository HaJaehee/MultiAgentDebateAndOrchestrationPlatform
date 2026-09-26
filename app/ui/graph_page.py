"""그래프 토론 편집 페이지 — `/graphs/{graph_id}`.

왼쪽 팔레트에서 노드를 놓고, 가운데 캔버스에서 핀을 이어 선을 만들고, 오른쪽 속성 창에서 노드·선을
고칩니다. 편집 상태는 브라우저가 들고 있고(`GraphCanvas`), **저장** 을 누를 때 한 번 서버로 가져와
그래프 파일(`data/graphs/<id>.json`)에 씁니다.

그래프는 대화에 묶이지 않습니다. 여러 대화가 같은 그래프를 고를 수 있고, 토론이 도는 중에 고쳐도
도는 턴은 턴 시작 때 굳힌 스냅샷을 씁니다. 그래서 이 페이지에는 잠금이 없습니다.

검증은 엔진과 같은 함수(`validate_graph`)입니다. 오류가 있어도 저장은 합니다 — 그리다 만 그래프를
날리지 않기 위해서입니다. 오류가 있는 그래프로는 토론이 시작되지 않고, 로스터와 이 페이지가 같은
이유를 보여 줍니다.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from nicegui import ui

from app.agents.pool import get_agent_pool
from app.config import get_config
from app.graph_store import free_graph_id, load_graph, save_graph
from app.orchestration.graph import (
    CARRY_LABELS,
    GraphReport,
    graph_from_card_order,
    validate_graph,
)
from app.orchestration.strategies import order_by_priority, specialists_of
from app.ui.components.graph_canvas import (
    GraphCanvas,
    agent_infos,
    canvas_to_spec,
    spec_to_canvas,
)
from app.ui.theme import CUSTOM_CSS, FAVICON_SVG

logger = logging.getLogger(__name__)

# 검증 때 노드에 상한을 적지 않았으면 이 값으로 셉니다. 실제 토론에서는 그 대화의 "최대 라운드" 입니다.
PREVIEW_MAX_VISITS = 3

CARRY_HELP = {
    "full": "발언 원문을 그대로 전달합니다.",
    "digest": "발언 끝의 ## 요지만 전달합니다 (없을 경우 긴 코드만 참조로 치환한 원문).",
    "refs": "원문 형태로 전달하되 긴 코드 블록은 한 줄 참조로 치환합니다.",
}
NODE_HELP = {
    "start": "이번 턴의 요청이 시작되는 노드입니다. 계획을 활성화하면 오케스트레이터 계획도 함께 전달됩니다.",
    "agent": "에이전트 1인의 발언 노드입니다. 연결된 입력선(또는 전체 기록)을 참조하여 발언합니다.",
    "merge": "오케스트레이터가 전달받은 발언들을 하나로 취합하고 상충되는 내용을 정리합니다.",
    "gate": "오케스트레이터가 질문에 대해 예/아니오로 판정하여 분기합니다. 반복 루프는 반드시 판정을 거쳐야 합니다.",
    "end": "해당 노드에 도달하면 그래프 실행을 종료하고 최종 합성 단계로 진행합니다.",
}


def known_agents() -> Dict[str, bool]:
    """검증에 쓸 {키: 켜짐}. 엔진·로스터와 같은 기준입니다."""
    known: Dict[str, bool] = {}
    try:
        for key, cfg in get_config().agents.items():
            if not getattr(cfg, "enabled", True):
                known[key] = False
    except Exception:  # noqa: BLE001 - 설정을 못 읽어도 풀만으로 검사합니다
        logger.debug("Could not read conf.json agents for graph validation", exc_info=True)
    for agent in get_agent_pool().list_all():
        known[agent.key] = True
    return known


def create_graph_page() -> None:
    """`/graphs/{graph_id}` 페이지를 등록합니다."""

    @ui.page("/graphs/{graph_id}", title="그래프 편집", favicon=FAVICON_SVG)
    async def graph_page(graph_id: str):
        ui.dark_mode(True)
        ui.add_head_html(f"<style>{CUSTOM_CSS}</style>")
        GraphCanvas.add_styles()

        try:
            spec = load_graph(graph_id)
        except FileNotFoundError:
            _render_problem(f"그래프 파일이 없습니다: data/graphs/{graph_id}.json")
            return
        except ValueError as exc:
            _render_problem(f"그래프를 읽지 못했습니다: {exc}")
            return

        agents = agent_infos(get_agent_pool().list_all())
        state: Dict[str, Any] = {"dirty": False, "selected": None}

        # ---------------- 헤더 ----------------
        with ui.header().classes(
            "bg-slate-900 border-b border-slate-800 px-4 py-2 items-center justify-between gap-3"
        ):
            with ui.row().classes("items-center gap-2.5 no-wrap min-w-0"):
                ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
                    "flat dense round color=grey-4"
                ).tooltip("토론 화면으로 돌아갑니다 (저장하지 않은 변경사항이 있으면 확인창이 표시됩니다).")
                ui.icon("hub", size="sm").classes("text-teal-400")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label("그래프 편집").classes("text-base font-bold text-white tracking-wide")
                    ui.label(f"data/graphs/{spec.id}.json").classes("text-[11px] text-slate-400 font-mono")
                name_input = ui.input("그래프 이름", value=spec.name).props(
                    "outlined dense dark debounce=300"
                ).classes("w-80 text-sm")
            with ui.row().classes("items-center gap-2 no-wrap"):
                dirty_badge = ui.badge("저장 안 됨", color="amber-8").props("dense")
                dirty_badge.set_visibility(False)
                ui.button("검증", icon="rule", on_click=lambda: validate()).props(
                    "flat dense no-caps color=grey-3"
                )
                ui.button("다른 이름으로 저장", icon="content_copy", on_click=lambda: save_as_dialog.open()).props(
                    "flat dense no-caps color=grey-3"
                )
                ui.button("저장", icon="save", on_click=lambda: save()).props(
                    "unelevated dense no-caps color=indigo-6"
                )

        def mark_dirty(*_args) -> None:
            if not state["dirty"]:
                state["dirty"] = True
                dirty_badge.set_visibility(True)

        name_input.on_value_change(mark_dirty)

        with ui.row().classes("w-full no-wrap gap-0 items-stretch").style("height: calc(100vh - 64px)"):
            # ---------------- 팔레트 ----------------
            with ui.column().classes(
                "w-60 flex-shrink-0 bg-slate-900 border-r border-slate-800 p-3 gap-2 overflow-auto"
            ):
                ui.label("흐름").classes("text-[11px] font-semibold text-slate-400 tracking-wider")
                for kind, label, icon in (
                    ("start", "시작", "play_arrow"),
                    ("merge", "취합", "call_merge"),
                    ("gate", "판정 (예/아니오)", "alt_route"),
                    ("end", "최종 합성", "flag"),
                ):
                    ui.button(label, icon=icon, on_click=lambda k=kind: add_node({"type": k})).props(
                        "flat dense no-caps align=left color=grey-3"
                    ).classes("w-full text-xs").tooltip(NODE_HELP[kind])

                ui.separator().classes("bg-slate-800 my-1")
                ui.label("에이전트").classes("text-[11px] font-semibold text-slate-400 tracking-wider")
                if not agents:
                    ui.label("활성화된 전문가 에이전트가 없습니다.").classes("text-xs text-amber-400")
                for key, (name, role, color) in agents.items():
                    with ui.button(
                        on_click=lambda k=key: add_node({
                            "type": "agent", "agent": k, "agentName": agents[k][0],
                            "agentRole": agents[k][1], "color": agents[k][2],
                        })
                    ).props("flat dense no-caps align=left").classes("w-full"):
                        with ui.row().classes("items-center gap-2 no-wrap w-full"):
                            ui.element("span").classes("inline-block w-2.5 h-2.5 rounded-full flex-shrink-0").style(
                                f"background: {color or '#6366f1'}"
                            )
                            with ui.column().classes("gap-0 items-start min-w-0"):
                                ui.label(name).classes("text-xs text-slate-200 truncate")
                                ui.label(key).classes("text-[10px] text-slate-500 font-mono")

                ui.separator().classes("bg-slate-800 my-1")
                ui.button("카드 순서로 다시 채우기", icon="account_tree", on_click=lambda: refill_dialog.open()).props(
                    "flat dense no-caps align=left color=teal-4"
                ).classes("w-full text-xs")
                ui.button("화면 맞춤", icon="fit_screen", on_click=lambda: canvas.run_method("fit")).props(
                    "flat dense no-caps align=left color=grey-4"
                ).classes("w-full text-xs")
                ui.label(
                    "출력 핀을 드래그하여 다른 노드의 입력 핀에 연결하면 선이 생성됩니다. 노드나 선을 클릭하면 우측에서 "
                    "속성을 수정할 수 있으며, Delete 키로 삭제합니다. 빈 영역을 드래그하면 화면이 이동하고 휠로 확대/축소합니다."
                ).classes("text-[10px] text-slate-500 leading-snug mt-2")

            # ---------------- 캔버스 ----------------
            with ui.element("div").classes("flex-grow min-w-0 h-full"):
                canvas = GraphCanvas(spec_to_canvas(spec, agents)).classes("w-full h-full")

            # ---------------- 속성 창 ----------------
            with ui.column().classes(
                "w-80 flex-shrink-0 bg-slate-900 border-l border-slate-800 p-3 gap-3 overflow-auto"
            ):
                inspector = ui.column().classes("w-full gap-2")
                ui.separator().classes("bg-slate-800")
                report_box = ui.column().classes("w-full gap-1")

        canvas.on("dirty", mark_dirty)

        # ---------------- 속성 창 내용 ----------------
        def update_node(node_id: str, **patch: Any) -> None:
            canvas.run_method("updateNode", node_id, patch)
            mark_dirty()

        def show_empty() -> None:
            inspector.clear()
            with inspector:
                ui.label("선택된 요소 없음").classes("text-xs font-semibold text-slate-400")
                ui.label(
                    "노드나 선을 클릭하면 이곳에서 속성을 수정할 수 있습니다. 선 색상은 전달 방식을 나타냅니다 — "
                    "보라: 전문 · 청록: 요지 · 주황: 참조. 판정 노드의 '아니오' 선은 점선으로 표시됩니다."
                ).classes("text-[11px] text-slate-500 leading-snug")

        def field_label(text: str) -> None:
            ui.label(text).classes("text-[11px] text-slate-400 -mb-1")

        def show_node(node_id: str, data: Dict[str, Any]) -> None:
            kind = data.get("type", "agent")
            inspector.clear()
            with inspector:
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    ui.label(f"노드 · {node_id}").classes("text-xs font-semibold text-slate-300 font-mono")
                    ui.button(icon="delete", on_click=lambda: canvas.run_method("removeElement", "node", node_id)).props(
                        "flat dense round size=sm color=red-4"
                    ).tooltip("해당 노드 및 연결된 선을 삭제합니다.")
                ui.label(NODE_HELP.get(kind, "")).classes("text-[11px] text-slate-500 leading-snug")

                ui.input("이름 (비워둘 시 기본값 적용)", value=data.get("label") or "",
                         on_change=lambda e: update_node(node_id, label=e.value or "")).props(
                    "outlined dense dark debounce=300").classes("w-full text-xs")

                if kind == "start":
                    ui.switch("오케스트레이터 계획 포함", value=data.get("plan", True) is not False,
                              on_change=lambda e: update_node(node_id, plan=bool(e.value))).props("dense dark color=teal-4")

                if kind == "agent":
                    options = {k: f"{v[0]} ({k})" for k, v in agents.items()}
                    current = data.get("agent") or None
                    if current and current not in options:
                        options[current] = f"(없거나 비활성화됨) {current}"

                    def choose_agent(e, nid=node_id):
                        key = e.value or ""
                        name, role, color = agents.get(key, ("", "", ""))
                        update_node(nid, agent=key, agentName=name, agentRole=role, color=color)

                    ui.select(options, value=current, label="에이전트", on_change=choose_agent).props(
                        "outlined dense dark options-dense").classes("w-full text-xs")

                if kind == "gate":
                    ui.textarea("판정 질문", value=data.get("question") or "",
                                placeholder="예: 치명적 결함이 없는가?",
                                on_change=lambda e: update_node(node_id, question=e.value or "")).props(
                        "outlined dense dark autogrow debounce=300").classes("w-full text-xs")
                    ui.select({"yes": "예", "no": "아니오"}, value=data.get("default") or "yes",
                              label="판정 실패 시 기본 분기",
                              on_change=lambda e: update_node(node_id, default=e.value)).props(
                        "outlined dense dark options-dense").classes("w-full text-xs")

                if kind in ("agent", "merge"):
                    ui.textarea("노드 개별 지시사항", value=data.get("instruction") or "",
                                on_change=lambda e: update_node(node_id, instruction=e.value or "")).props(
                        "outlined dense dark autogrow debounce=300").classes("w-full text-xs")

                if kind == "agent":
                    ui.select({"inputs": "연결된 선만 참조", "all": "전체 기록 참조"},
                              value=data.get("sees") or "inputs", label="참조 맥락",
                              on_change=lambda e: update_node(node_id, sees=e.value)).props(
                        "outlined dense dark options-dense").classes("w-full text-xs")

                if kind in ("agent", "merge", "gate", "end"):
                    ui.select({"any": "하나라도 도착하면 실행", "all": "모두 도착하면 실행 (첫 활성화 시)"},
                              value=data.get("wait") or "any", label="다중 입력 대기 조건",
                              on_change=lambda e: update_node(node_id, wait=e.value)).props(
                        "outlined dense dark options-dense").classes("w-full text-xs")

                if kind in ("agent", "merge", "gate"):
                    ui.number("최대 방문 횟수 (비워둘 시 세션 최대 라운드 적용)", value=data.get("max_visits"),
                              min=1, max=20, step=1, format="%.0f",
                              on_change=lambda e: update_node(
                                  node_id, max_visits=int(e.value) if e.value else None)).props(
                        "outlined dense dark").classes("w-full text-xs")

        def show_edge(edge_id: str, data: Dict[str, Any]) -> None:
            inspector.clear()
            with inspector:
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    ui.label(f"선 · {edge_id}").classes("text-xs font-semibold text-slate-300 font-mono")
                    ui.button(icon="delete", on_click=lambda: canvas.run_method("removeElement", "edge", edge_id)).props(
                        "flat dense round size=sm color=red-4"
                    ).tooltip("해당 선을 삭제합니다.")
                source, target = data.get("from") or ["?", "?"], data.get("to") or ["?", "?"]
                branch = {"yes": " (예)", "no": " (아니오)"}.get(source[1], "")
                ui.label(f"{source[0]}{branch} → {target[0]}").classes("text-[11px] text-slate-400 font-mono")
                carry = data.get("carry") or "full"
                help_label = ui.label(CARRY_HELP[carry]).classes("text-[11px] text-slate-500 leading-snug")

                def choose_carry(e, eid=edge_id):
                    canvas.run_method("updateEdge", eid, {"carry": e.value})
                    help_label.set_text(CARRY_HELP.get(e.value, ""))
                    mark_dirty()

                ui.select({k: v for k, v in CARRY_LABELS.items()}, value=carry, label="전달 방식",
                          on_change=choose_carry).props("outlined dense dark options-dense").classes("w-full text-xs")

        def on_select(e) -> None:
            payload = e.args or {}
            kind, element_id, data = payload.get("kind"), payload.get("id"), payload.get("data") or {}
            if kind == "node":
                show_node(element_id, data)
            elif kind == "edge":
                show_edge(element_id, data)
            else:
                show_empty()

        canvas.on("select", on_select)
        show_empty()

        # ---------------- 검증 · 저장 ----------------
        def show_report(report: GraphReport) -> None:
            report_box.clear()
            with report_box:
                tone = "text-red-400" if report.errors else ("text-amber-400" if report.warnings else "text-teal-300")
                ui.label(report.summary()).classes(f"text-xs font-semibold {tone}")
                for error in report.errors:
                    ui.label(f"오류 · {error}").classes("text-[11px] text-red-300 leading-snug")
                for warning in report.warnings:
                    ui.label(f"경고 · {warning}").classes("text-[11px] text-amber-300 leading-snug")
                ui.label(
                    f"호출 횟수는 상한이 지정되지 않은 노드를 {PREVIEW_MAX_VISITS}회로 계산하였습니다. 실제 토론 시에는 해당 세션의 최대 라운드가 적용됩니다."
                ).classes("text-[10px] text-slate-500 leading-snug")

        async def current_spec(graph_id_: str, name: str):
            raw = await canvas.run_method("getGraph", timeout=10)
            return canvas_to_spec(graph_id_, name, raw or {})

        async def validate() -> Optional[GraphReport]:
            try:
                candidate = await current_spec(spec.id, name_input.value or spec.id)
            except ValueError as exc:
                ui.notify(str(exc), type="negative", position="bottom-right")
                return None
            report = validate_graph(candidate, known_agents(), default_max_visits=PREVIEW_MAX_VISITS)
            show_report(report)
            return report

        async def save(target_id: Optional[str] = None, name: Optional[str] = None) -> bool:
            try:
                candidate = await current_spec(target_id or spec.id, name if name is not None else (name_input.value or spec.id))
            except ValueError as exc:
                ui.notify(str(exc), type="negative", position="bottom-right")
                return False
            report = validate_graph(candidate, known_agents(), default_max_visits=PREVIEW_MAX_VISITS)
            try:
                save_graph(candidate)
            except OSError as exc:
                ui.notify(f"저장하지 못하였습니다: {exc}", type="negative", position="bottom-right")
                return False
            show_report(report)
            if target_id is None:
                canvas.run_method("markClean")
                state["dirty"] = False
                dirty_badge.set_visibility(False)
            ui.notify(
                f"저장하였습니다 — {report.summary()}"
                + ("  (오류가 존재할 경우 해당 그래프로는 토론을 시작할 수 없습니다)" if report.errors else ""),
                type="warning" if report.errors else "positive", position="bottom-right",
            )
            return True

        # ---------------- 대화 상자 ----------------
        with ui.dialog() as save_as_dialog, ui.card().classes("bg-slate-900 text-white p-4 w-96 gap-3"):
            ui.label("다른 이름으로 저장").classes("text-sm font-bold")
            copy_name = ui.input("새 그래프 이름", value=f"{spec.name or spec.id} 사본").props(
                "outlined dense dark").classes("w-full")
            ui.label("새 파일로 저장한 후 해당 그래프를 엽니다. 현재 파일은 변경되지 않습니다.").classes(
                "text-[11px] text-slate-400")

            async def do_save_as():
                new_id = free_graph_id(spec.id)
                if await save(target_id=new_id, name=copy_name.value or new_id):
                    save_as_dialog.close()
                    canvas.run_method("markClean")
                    ui.navigate.to(f"/graphs/{new_id}")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("취소", on_click=save_as_dialog.close).props("flat dense no-caps color=grey-4")
                ui.button("저장하고 열기", on_click=do_save_as).props("unelevated dense no-caps color=indigo-6")

        with ui.dialog() as refill_dialog, ui.card().classes("bg-slate-900 text-white p-4 w-96 gap-3"):
            ui.label("카드 순서로 다시 채우기").classes("text-sm font-bold")
            ui.label(
                "캔버스의 모든 노드와 연결선을 초기화하고, 활성화된 전문가 에이전트를 로스터 순서대로 "
                "순차 연결한 그래프로 재구성합니다. 저장하기 전까지는 파일에 반영되지 않습니다."
            ).classes("text-[11px] text-slate-400 leading-snug")

            def do_refill():
                keys = [a.key for a in order_by_priority(specialists_of(get_agent_pool().list_all()))]
                if not keys:
                    ui.notify("활성화된 전문가 에이전트가 없습니다.", type="warning", position="bottom-right")
                    return
                chain = graph_from_card_order(spec.id, name_input.value or spec.id, keys)
                canvas.run_method("replaceGraph", spec_to_canvas(chain, agents))
                mark_dirty()
                refill_dialog.close()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("취소", on_click=refill_dialog.close).props("flat dense no-caps color=grey-4")
                ui.button("바꾸기", on_click=do_refill).props("unelevated dense no-caps color=teal-7")

        def add_node(data: Dict[str, Any]) -> None:
            canvas.run_method("addNode", data)
            mark_dirty()

        # 첫 화면에서 지금 그래프의 검증 결과를 보여 줍니다.
        show_report(validate_graph(spec, known_agents(), default_max_visits=PREVIEW_MAX_VISITS))


def new_blank_graph(name: Optional[str] = None):
    """시작 → 최종 합성 두 노드만 있는 새 그래프를 만들어 저장하고 돌려줍니다."""
    from app.orchestration.graph import GraphEdge, GraphNode, GraphSpec

    spec = GraphSpec(
        id=free_graph_id("graph"),
        name=name or f"새 그래프 ({datetime.now().strftime('%m-%d %H:%M')})",
        nodes=[
            GraphNode(id="start", type="start", label="시작", pos=(40, 160)),
            GraphNode(id="end", type="end", label="최종 합성", pos=(520, 160)),
        ],
        edges=[GraphEdge(id="e1", source=("start", "out"), target=("end", "in"))],
    )
    save_graph(spec)
    return spec


def _render_problem(message: str) -> None:
    with ui.column().classes("w-full max-w-xl mx-auto p-8 gap-4 items-start"):
        ui.label(message).classes("text-base text-red-300")
        ui.button("토론 화면으로", icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
            "unelevated color=indigo-7"
        )
