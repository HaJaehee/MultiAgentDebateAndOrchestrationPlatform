"""웰컴 화면 — 에이전트가 챗봇과 무엇이 다른지, 무엇으로 이루어지는지, 그리고 어디서 시작하는지."""

from __future__ import annotations

from fastapi import Request
from nicegui import ui

from app.agents.pool import get_agent_pool
from app.database.session import get_session_factory
from app.easy.pages.common import (
    EASY_BUILD,
    EASY_HOME,
    EASY_RUN,
    Viewer,
    agent_avatar,
    easy_header,
    easy_setup,
    resolve_viewer,
)
from app.easy.sessions import delete_guest_agent, list_easy_sessions, list_guest_agents
from app.trial.pages.common import disabled_page, footer_notice, local_time

LOOP_DIAGRAM = """flowchart LR
    goal(["🎯 목표"]) --> think["💭 생각<br/>무엇을 해야 하지?"]
    think --> act["🛠 행동<br/>도구를 쓴다"]
    act --> obs["👀 관찰<br/>결과를 본다"]
    obs --> think
    think -->|목표 달성| done(["✅ 완료"])
"""

# 같은 질문에 대한 두 모습. 에이전트 쪽은 예제 폴더의 판매 기록(examples/sales_2026q3.csv)으로
# 실제로 나오는 숫자입니다 — 수량은 보조 배터리, 매출액은 무선 이어폰이 1위입니다.
_CHATBOT_STEPS = [
    ("💬", "질문을 받습니다."),
    ("📚", "학습해 둔 지식만으로 답을 만듭니다. 회사의 판매 파일은 볼 수 없습니다."),
]
_CHATBOT_ANSWER = "“판매 자료를 볼 수 없어 정확히는 알 수 없지만, 보통은 무선 이어폰이 많이 팔립니다.”"
_AGENT_STEPS = [
    ("💭", "생각 — 판매 기록 파일이 있는지 먼저 봐야겠다."),
    ("🛠", "행동 — 작업 폴더를 살펴본다."),
    ("👀", "관찰 — sales_2026q3.csv 가 있다."),
    ("🛠", "행동 — 파일을 열어 읽는다."),
    ("👀", "관찰 — 제품별 판매 60줄. 수량과 단가가 있다."),
    ("💭", "생각 — '많이 팔린' 은 수량일 수도, 매출액일 수도 있다. 둘 다 더해 보자."),
]
_AGENT_ANSWER = "“수량으로는 보조 배터리(668개), 매출액으로는 무선 이어폰(약 5,705만 원)이 1위입니다. 근거: sales_2026q3.csv.”"

_INGREDIENTS = [
    ("badge", "페르소나", "누구인가", "이름과 한 줄 역할입니다. 사회자는 이 줄을 보고 누구에게 어떤 일을 맡길지 정합니다.",
     "name · role"),
    ("description", "업무 지시서", "어떻게 일하나", "목표, 일하는 순서, 결과물의 모양, 하지 말아야 할 것을 적은 글입니다. "
     "에이전트는 일할 때마다 이 글을 맨 먼저 읽습니다.", "system_prompt"),
    ("construction", "도구", "손과 발", "파일 열기, 계산, 웹 페이지 읽기처럼 실제로 무언가를 하는 능력입니다. "
     "도구가 없으면 말만 할 수 있습니다.", "allowed_mcp_servers (MCP)"),
    ("menu_book", "스킬", "업무 매뉴얼", "특정한 일을 잘하는 요령을 적어 둔 문서입니다. 필요할 때 펼쳐 보고 그대로 따라 합니다.",
     "allowed_skills"),
]


def _hero() -> None:
    with ui.column().classes("easy-hero w-full p-6 gap-2"):
        ui.label("AI 에이전트는 대답만 하는 챗봇이 아닙니다").classes("text-2xl font-semibold")
        ui.label(
            "챗봇은 묻는 말에 아는 만큼 대답합니다. 에이전트는 목표를 받으면 스스로 계획을 세우고, 도구를 써서 "
            "자료를 직접 열어 보고, 계산하고, 확인하면서 일이 끝날 때까지 움직입니다."
        ).classes("text-slate-300 leading-relaxed")


def _compare() -> None:
    ui.label("같은 질문, 다른 일하는 방식").classes("text-lg font-semibold")
    with ui.row().classes("items-center gap-2 text-sm text-slate-300"):
        ui.icon("help_outline", size="xs").classes("text-indigo-300")
        ui.label("“이번 분기에 가장 많이 팔린 제품은 무엇입니까?”")
    with ui.grid().classes("w-full gap-3 grid-cols-1 md:grid-cols-2"):
        for title, subtitle, steps, answer, tone in (
            ("챗봇", "아는 것으로 바로 답합니다", _CHATBOT_STEPS, _CHATBOT_ANSWER, "text-slate-400"),
            ("에이전트", "확인하고 나서 답합니다", _AGENT_STEPS, _AGENT_ANSWER, "text-emerald-200"),
        ):
            with ui.card().classes("trial-card p-4 gap-2 w-full"):
                with ui.row().classes("items-baseline gap-2"):
                    ui.label(title).classes("text-base font-semibold")
                    ui.label(subtitle).classes("text-xs text-slate-500")
                for icon, text in steps:
                    with ui.row().classes("items-start gap-2 flex-nowrap"):
                        ui.label(icon).classes("flex-shrink-0")
                        ui.label(text).classes("text-sm text-slate-300 leading-snug")
                ui.label(answer).classes(f"text-sm {tone} mt-1 leading-snug")


def _loop() -> None:
    ui.label("에이전트가 일하는 고리: 생각 → 행동 → 관찰").classes("text-lg font-semibold")
    with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row md:flex-nowrap"):
        with ui.card().classes("trial-card p-4 w-full md:w-1/2"):
            ui.mermaid(LOOP_DIAGRAM).classes("w-full")
        with ui.column().classes("gap-2 w-full md:w-1/2 text-sm text-slate-300 leading-relaxed"):
            ui.label("에이전트는 이 고리를 여러 번 돕니다. 한 번 돌 때마다 새로 알게 된 것(관찰)으로 다음 행동을 고칩니다. "
                     "그래서 처음 생각이 틀려도 스스로 바로잡을 수 있습니다.")
            ui.label("도구가 거부되거나 실패해도 그것 역시 관찰입니다. 에이전트는 그 결과를 읽고 다른 방법을 찾습니다.")
            ui.label("이 앱에서는 여러 에이전트가 한 팀으로 일합니다. 사회자(오케스트레이터)가 먼저 계획을 세워 일을 나누고, "
                     "전문가 에이전트들이 차례로 일한 뒤, 사회자가 결과를 정리합니다.")


def _ingredients() -> None:
    ui.label("에이전트를 이루는 네 가지").classes("text-lg font-semibold")
    with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-4"):
        for icon, name, short, body, tech in _INGREDIENTS:
            with ui.card().classes("trial-card p-4 gap-1 w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(icon, size="sm").classes("text-indigo-300")
                    ui.label(name).classes("font-semibold")
                    ui.label(short).classes("text-xs text-slate-500")
                ui.label(body).classes("text-sm text-slate-300 leading-snug")
                ui.label(f"설정 이름: {tech}").classes("easy-mono mt-1")
    ui.label("직접 만들 때는 이 네 가지를 도우미와 대화하며 정합니다. 설정 파일을 몰라도 됩니다.").classes(
        "text-sm text-slate-400"
    )


def _start_cards(viewer: Viewer) -> None:
    ui.label("시작하기").classes("text-lg font-semibold")
    cards = [
        ("play_circle", "일하는 모습 보기",
         "예제 판매 기록으로 ‘자료 탐색가’ 에이전트가 생각·행동·관찰을 되풀이하는 모습을 실시간으로 봅니다 (1~3분).",
         f"{EASY_RUN}?demo=1"),
        ("chat", "나만의 에이전트 만들기",
         "맡기고 싶은 일을 말하면 도우미가 몇 가지를 묻고, 에이전트의 설정을 대신 써 줍니다.", EASY_BUILD),
        ("assignment", "내 에이전트에게 일 맡기기",
         "에이전트를 골라 과제를 주고, 일하는 과정과 결과를 받습니다.", EASY_RUN),
    ]
    with ui.grid().classes("w-full gap-3 grid-cols-1 md:grid-cols-3"):
        for icon, title, body, target in cards:
            with ui.card().classes("trial-card trial-card-link p-4 gap-2 w-full").on(
                "click", lambda t=target: ui.navigate.to(t)
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(icon, size="sm").classes("text-indigo-300")
                    ui.label(title).classes("font-semibold")
                ui.label(body).classes("text-sm text-slate-400 leading-snug")
    if viewer.owner:
        note = "만든 에이전트는 conf.json 에 저장되어 전문가 화면에서도 그대로 쓸 수 있습니다."
    else:
        note = ("만든 에이전트는 본인만 쓰는 ‘내 에이전트’로 저장됩니다. 체험에서는 작업 폴더를 읽기만 할 수 있어, "
                "파일 쓰기나 코드 실행은 거부됩니다 — 거부되는 장면도 에이전트가 관찰하는 결과입니다.")
    ui.label(note).classes("text-xs text-slate-500")


async def _records(viewer: Viewer) -> None:
    @ui.refreshable
    async def records() -> None:
        async with get_session_factory()() as db:
            sessions = await list_easy_sessions(db, viewer.user_id)
            agents = [] if viewer.owner else await list_guest_agents(db, viewer.user_id)
        with ui.grid().classes("w-full gap-4 grid-cols-1 md:grid-cols-2"):
            with ui.column().classes("gap-1 w-full"):
                ui.label("내 기록").classes("text-sm font-semibold text-slate-300")
                if not sessions:
                    ui.label("아직 맡긴 일이 없습니다.").classes("text-sm text-slate-500")
                for row in sessions:
                    with ui.row().classes(
                        "w-full items-center justify-between gap-2 px-3 py-2 rounded-lg hover:bg-slate-900 "
                        "cursor-pointer flex-nowrap"
                    ).on("click", lambda sid=row.session_id: ui.navigate.to(f"{EASY_HOME}/s/{sid}")):
                        ui.label(row.title).classes("text-sm text-slate-200 truncate min-w-0")
                        ui.label(local_time(row.created_at)).classes("text-xs text-slate-500 flex-shrink-0")
            with ui.column().classes("gap-1 w-full"):
                ui.label("내 에이전트").classes("text-sm font-semibold text-slate-300")
                if viewer.owner:
                    count = sum(1 for a in get_agent_pool().list_all() if a.key != "orchestrator")
                    ui.label(f"conf.json 에 전문가 에이전트 {count}명이 있습니다. 고치거나 지우는 것은 전문가 화면의 "
                             "에이전트 구성에서 합니다.").classes("text-sm text-slate-500")
                elif not agents:
                    ui.label("아직 만든 에이전트가 없습니다.").classes("text-sm text-slate-500")
                for agent in agents:
                    with ui.row().classes("w-full items-center gap-2 px-3 py-2 rounded-lg hover:bg-slate-900 flex-nowrap"):
                        agent_avatar(f"my_{agent.id[:8]}", agent.name, agent.card_color, agent.icon, size="sm")
                        with ui.column().classes("gap-0 min-w-0 flex-grow cursor-pointer").on(
                            "click", lambda aid=agent.id: ui.navigate.to(f"{EASY_RUN}?agent=my:{aid}")
                        ):
                            ui.label(agent.name).classes("text-sm text-slate-200 truncate w-full")
                            ui.label(agent.role).classes("text-xs text-slate-500 truncate w-full")
                        ui.button(icon="delete_outline", on_click=lambda aid=agent.id: remove(aid)).props(
                            "flat dense round size=12px color=grey-6"
                        )

    async def remove(agent_id: str) -> None:
        async with get_session_factory()() as db:
            await delete_guest_agent(db, viewer.user_id, agent_id)
        ui.notify("에이전트를 지웠습니다.")
        records.refresh()

    await records()


def build_home() -> None:
    @ui.page(EASY_HOME)
    async def easy_home(request: Request):
        viewer, redirect = await resolve_viewer(request, EASY_HOME)
        if redirect is not None:
            return redirect
        if viewer is None:
            disabled_page(False)
            return

        easy_setup()
        easy_header(viewer)
        with ui.column().classes("trial-page px-4 pb-6 gap-5"):
            _hero()
            _compare()
            _loop()
            _ingredients()
            _start_cards(viewer)
            await _records(viewer)
        if not viewer.owner:
            footer_notice()
