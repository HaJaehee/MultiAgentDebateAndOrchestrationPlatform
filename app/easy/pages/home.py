"""웰컴(홈) 화면 — AI 에이전트와 챗봇의 차이점, 에이전트 핵심 구성 요소 및 시작 경로 안내."""

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

# 색상. 생각·행동·관찰은 실행 화면의 단계 막대와 동일한 색상입니다 (common.py `.easy-step-*`).
THOUGHT, ACTION, OBSERVE = "#6366f1", "#f59e0b", "#10b981"
CHATBOT, AGENT = "#64748b", "#14b8a6"


def _tint(color: str) -> str:
    """색 카드에 줄 인라인 스타일 (`.easy-tint` · `.easy-badge` · `.easy-pill` 이 읽습니다)."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"--c:{color};--rgb:{r},{g},{b}"


# 다이어그램은 어두운 카드 위에 그려집니다. Mermaid 기본 선 색(진회색)은 그 위에서 보이지 않으므로
# 선·화살촉(`lineColor`)은 밝게, 선 위 글자("목표 달성")는 어두운 바탕에 밝은 글씨로 칠합니다.
# 노드는 연한 바탕에 진한 테두리라 어두운 카드에서도 읽힙니다.
LOOP_DIAGRAM = """%%{init: {"themeVariables": {"lineColor": "#cbd5e1", "edgeLabelBackground": "#334155", "textColor": "#e2e8f0"}}}%%
flowchart LR
    goal(["🎯 목표"]) --> think["💭 생각<br/>무엇을 해야 하지?"]
    think --> act["🛠 행동<br/>도구를 쓴다"]
    act --> obs["👀 관찰<br/>결과를 본다"]
    obs --> think
    think -->|목표 달성| done(["✅ 완료"])
    classDef cGoal fill:#ede9fe,stroke:#8b5cf6,stroke-width:2px,color:#3b0764
    classDef cThink fill:#e0e7ff,stroke:#6366f1,stroke-width:2px,color:#1e1b4b
    classDef cAct fill:#fef3c7,stroke:#f59e0b,stroke-width:2px,color:#451a03
    classDef cObs fill:#d1fae5,stroke:#10b981,stroke-width:2px,color:#022c22
    classDef cDone fill:#dcfce7,stroke:#22c55e,stroke-width:2px,color:#052e16
    class goal cGoal
    class think cThink
    class act cAct
    class obs cObs
    class done cDone
"""

# 같은 질문에 대한 두 모습. 에이전트 쪽은 예제 폴더의 판매 기록(examples/sales_2026q3.csv)으로
# 실제로 도출되는 숫자입니다 — 수량은 보조 배터리, 매출액은 무선 이어폰이 1위입니다.
_CHATBOT_STEPS = [
    ("💬", "질문을 수신합니다.", "text-slate-300"),
    ("📚", "사전 학습된 지식에만 의존하여 답변합니다. 실제 사내 판매 데이터 파일은 조회할 수 없습니다.", "text-slate-300"),
]
_CHATBOT_ANSWER = "“실제 판매 데이터를 확인할 수 없어 정확하지는 않지만, 일반적인 시장 통계상 무선 이어폰이 많이 판매됩니다.”"
# 글자색은 단계 종류를 따릅니다 (생각 인디고 · 행동 호박 · 관찰 에메랄드).
_AGENT_STEPS = [
    ("💭", "생각 — 판매 기록 파일이 존재하는지 먼저 확인해 봐야겠다.", "text-indigo-200"),
    ("🛠", "행동 — 작업 디렉터리 파일 목록을 살펴본다.", "text-amber-200"),
    ("👀", "관찰 — sales_2026q3.csv 파일이 있음을 확인했다.", "text-emerald-200"),
    ("🛠", "행동 — 해당 CSV 파일을 열어서 읽는다.", "text-amber-200"),
    ("👀", "관찰 — 총 60행의 제품별 판매 데이터(수량, 단가)를 확인했다.", "text-emerald-200"),
    ("💭", "생각 — '가장 많이 팔린'의 기준이 수량인지 매출액인지 둘 다 집계해 보자.", "text-indigo-200"),
]
_AGENT_ANSWER = "“수량으로는 보조 배터리(668개), 매출액으로는 무선 이어폰(약 5,705만 원)이 1위입니다. 근거: sales_2026q3.csv.”"

_INGREDIENTS = [
    ("#8b5cf6", "badge", "페르소나", "기본 정체성", "이름과 한 줄 역할 정의입니다. 오케스트레이터가 이를 참고하여 누구에게 어떤 업무를 분담할지 결정합니다.",
     "name · role"),
    ("#0ea5e9", "description", "업무 지시서", "행동 지침", "목표, 작업 절차, 결과물 양식, 금지 사항을 명시한 지침입니다. "
     "에이전트는 작업을 시작할 때마다 이 지침을 최우선으로 확인합니다.", "system_prompt"),
    # 도구는 행동과 동일한 색상입니다 — 도구를 사용하는 것이 곧 행동입니다.
    (ACTION, "construction", "도구", "손과 발", "파일 열기, 계산, 웹 검색처럼 현실의 작업을 실제로 수행하는 기능입니다. "
     "도구가 없으면 텍스트 대화만 나눌 수 있습니다.", "allowed_mcp_servers (MCP)"),
    ("#ec4899", "menu_book", "스킬", "업무 매뉴얼", "특정 업무를 전문적으로 수행하는 절차와 노하우를 담은 매뉴얼입니다. 필요한 상황에 참조하여 단계별로 수행합니다.",
     "allowed_skills"),
]


def _hero() -> None:
    with ui.column().classes("easy-hero w-full p-6 gap-2"):
        ui.label("AI 에이전트는 대답만 하는 챗봇이 아닙니다").classes("text-2xl font-semibold")
        ui.label(
            "챗봇은 질문을 받으면 이미 알고 있는 지식 내에서만 답변합니다. 반면 AI 에이전트는 목표가 주어지면 스스로 계획을 세우고, 도구를 활용해 "
            "자료를 직접 열어 보고, 계산하고, 검증하면서 목표를 완료할 때까지 주도적으로 실행합니다."
        ).classes("text-slate-200 leading-relaxed")
        with ui.row().classes("items-center gap-2 mt-1"):
            for index, (label, color) in enumerate((("💭 생각", THOUGHT), ("🛠 행동", ACTION), ("👀 관찰", OBSERVE))):
                if index:
                    ui.icon("arrow_forward", size="xs").classes("text-slate-300")
                ui.label(label).classes("easy-pill").style(_tint(color))
            ui.label("— 목표를 달성할 때까지 이 3단계를 반복합니다").classes("text-sm text-slate-200")


def _compare() -> None:
    ui.label("동일한 질문에 대한 챗봇과 에이전트의 차이").classes("text-lg font-semibold")
    with ui.row().classes("items-center gap-2 text-sm text-slate-300"):
        ui.icon("help_outline", size="xs").classes("text-indigo-300")
        ui.label("“이번 분기에 가장 많이 팔린 제품은 무엇입니까?”")
    with ui.grid().classes("w-full gap-3 grid-cols-1 md:grid-cols-2"):
        for color, icon_name, title, subtitle, steps, answer, tone in (
            (CHATBOT, "chat_bubble_outline", "챗봇", "기존 학습 지식으로만 답변", _CHATBOT_STEPS, _CHATBOT_ANSWER,
             "text-slate-400"),
            (AGENT, "smart_toy", "에이전트", "실제 데이터를 확인하고 답변", _AGENT_STEPS, _AGENT_ANSWER, "text-teal-200"),
        ):
            with ui.card().classes("trial-card easy-tint p-4 gap-2 w-full").style(_tint(color)):
                with ui.row().classes("items-center gap-2"):
                    with ui.element("div").classes("easy-badge"):
                        ui.icon(icon_name, size="sm")
                    ui.label(title).classes("text-base font-semibold")
                    ui.label(subtitle).classes("text-xs text-slate-400")
                for icon, text, step_tone in steps:
                    with ui.row().classes("items-start gap-2 flex-nowrap"):
                        ui.label(icon).classes("flex-shrink-0")
                        ui.label(text).classes(f"text-sm {step_tone} leading-snug")
                ui.label(answer).classes(f"text-sm {tone} mt-1 leading-snug font-medium")


def _loop() -> None:
    ui.label("에이전트 실행 루프: 생각 → 행동 → 관찰").classes("text-lg font-semibold")
    with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row md:flex-nowrap"):
        with ui.card().classes("trial-card p-4 w-full md:w-1/2"):
            ui.mermaid(LOOP_DIAGRAM).classes("w-full")
        with ui.column().classes("gap-2 w-full md:w-1/2 text-sm text-slate-300 leading-relaxed"):
            ui.label("에이전트는 이 루프를 여러 차례 반복합니다. 루프를 돌 때마다 새롭게 획득한 정보(관찰)를 바탕으로 다음 행동을 보정하므로, "
                     "초기 계획이나 가설에 오류가 있더라도 스스로 바로잡을 수 있습니다.")
            ui.label("도구 실행이 정책에 의해 차단되거나 실패하더라도 그 결과 역시 유의미한 관찰 데이터가 됩니다. 에이전트는 이를 바탕으로 다른 대안을 모색합니다.")
            ui.label("이 플랫폼에서는 여러 전문 에이전트가 하나의 팀으로 협업합니다. 오케스트레이터(사회자)가 먼저 전체 계획을 세워 역할을 분담하고, "
                     "각 전문가 에이전트가 순차적으로 작업을 완수한 후, 오케스트레이터가 최종 결과를 종합 보고서로 정리합니다.")


def _ingredients() -> None:
    ui.label("에이전트의 4대 핵심 구성 요소").classes("text-lg font-semibold")
    with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-4"):
        for color, icon, name, short, body, tech in _INGREDIENTS:
            with ui.card().classes("trial-card easy-tint p-4 gap-1 w-full").style(_tint(color)):
                with ui.row().classes("items-center gap-2"):
                    with ui.element("div").classes("easy-badge"):
                        ui.icon(icon, size="sm")
                    ui.label(name).classes("font-semibold")
                    ui.label(short).classes("text-xs text-slate-400")
                ui.label(body).classes("text-sm text-slate-300 leading-snug")
                ui.label(f"설정 필드명: {tech}").classes("easy-mono mt-1")
    ui.label("에이전트를 만들 때 이 네 가지 요소를 도우미와 대화하며 쉽게 설정할 수 있습니다. 복잡한 설정 파일(conf.json)을 직접 다루지 않아도 됩니다.").classes(
        "text-sm text-slate-400"
    )


def _start_cards(viewer: Viewer) -> None:
    ui.label("시작하기").classes("text-lg font-semibold")
    cards = [
        (OBSERVE, "play_circle", "일하는 모습 보기",
         "샘플 판매 데이터를 바탕으로 '자료 탐색가' 에이전트가 생각·행동·관찰을 수행하는 과정을 실시간으로 확인합니다 (약 1~3분 소요).",
         f"{EASY_RUN}?demo=1"),
        ("#8b5cf6", "chat", "나만의 에이전트 만들기",
         "원하는 업무를 설명하면 대화형 도우미가 필요한 설정을 맞춤형으로 작성해 줍니다.", EASY_BUILD),
        ("#0ea5e9", "assignment", "내 에이전트에게 일 맡기기",
         "원하는 에이전트를 선택하여 과제를 부여하고, 수행 과정과 최종 산출물을 확인합니다.", EASY_RUN),
    ]
    with ui.grid().classes("w-full gap-3 grid-cols-1 md:grid-cols-3"):
        for color, icon, title, body, target in cards:
            with ui.card().classes("trial-card easy-tint easy-tint-link p-4 gap-2 w-full").style(_tint(color)).on(
                "click", lambda t=target: ui.navigate.to(t)
            ):
                with ui.row().classes("items-center gap-2"):
                    with ui.element("div").classes("easy-badge"):
                        ui.icon(icon, size="sm")
                    ui.label(title).classes("font-semibold")
                ui.label(body).classes("text-sm text-slate-300 leading-snug")
    if viewer.owner:
        note = "생성한 에이전트는 conf.json에 영구 저장되어 전문가 화면 및 다른 세션에서도 바로 활용할 수 있습니다."
    else:
        note = ("생성한 에이전트는 본인 계정의 '내 에이전트' 목록에 저장됩니다. 체험 환경에서는 작업 디렉터리가 읽기 전용으로 제한되어 "
                "파일 생성/수정이나 코드 실행은 차단됩니다 — 차단된 결과 역시 에이전트가 관찰하여 보고합니다.")
    ui.label(note).classes("text-xs text-slate-500")


async def _records(viewer: Viewer) -> None:
    @ui.refreshable
    async def records() -> None:
        async with get_session_factory()() as db:
            sessions = await list_easy_sessions(db, viewer.user_id)
            agents = [] if viewer.owner else await list_guest_agents(db, viewer.user_id)
        with ui.grid().classes("w-full gap-4 grid-cols-1 md:grid-cols-2"):
            with ui.column().classes("gap-1 w-full"):
                ui.label("내 실행 기록").classes("text-sm font-semibold text-slate-300")
                if not sessions:
                    ui.label("아직 실행한 과제가 없습니다.").classes("text-sm text-slate-500")
                for row in sessions:
                    with ui.row().classes(
                        "w-full items-center justify-between gap-2 px-3 py-2 rounded-lg hover:bg-slate-900 "
                        "cursor-pointer flex-nowrap"
                    ).on("click", lambda sid=row.session_id: ui.navigate.to(f"{EASY_HOME}/s/{sid}")):
                        ui.label(row.title).classes("text-sm text-slate-200 truncate min-w-0")
                        ui.label(local_time(row.created_at)).classes("text-xs text-slate-500 flex-shrink-0")
            with ui.column().classes("gap-1 w-full"):
                ui.label("내 에이전트 목록").classes("text-sm font-semibold text-slate-300")
                if viewer.owner:
                    count = sum(1 for a in get_agent_pool().list_all() if a.key != "orchestrator")
                    ui.label(f"conf.json에 등록된 전문가 에이전트가 총 {count}명 있습니다. 에이전트 수정 및 삭제는 상단 '전문가 화면'의 "
                             "에이전트 구성(로스터)에서 진행할 수 있습니다.").classes("text-sm text-slate-500")
                elif not agents:
                    ui.label("아직 생성한 맞춤 에이전트가 없습니다.").classes("text-sm text-slate-500")
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
        ui.notify("에이전트를 삭제했습니다.")
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
