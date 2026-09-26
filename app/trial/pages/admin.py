"""운영자 사용량 화면 — 주인(루프백·접속 토큰)만 봅니다.

템플릿별로 몇 번 돌았고, 몇 번 결론까지 갔고, 얼마나 걸렸고, 사람들이 어떻게 평가했는지를
봅니다. 방문자의 PIN 초기화와 잠금 해제도 여기서 합니다.
"""

from __future__ import annotations

from typing import List

from fastapi import Request
from nicegui import ui

from app.agents.llm_gate import get_llm_gate
from app.config import get_config
from app.database.session import get_session_factory
from app.orchestration.runner import get_debate_runner
from app.trial.catalog import official_templates
from app.trial.pages.common import duration, footer_notice, header, local_time, page_setup
from app.trial.stats import TemplateUsage, UserUsage, usage_report
from app.trial.store import aware, reset_pin, unlock_user
from app.trial.web import is_owner, owner_only_response


def _metric(label: str, value: str) -> None:
    with ui.column().classes("trial-card p-4 gap-0 min-w-[140px] flex-1"):
        ui.label(label).classes("text-xs text-slate-400")
        ui.label(value).classes("text-2xl font-semibold")


def _template_rows(rows: List[TemplateUsage]) -> None:
    columns = [
        {"name": "title", "label": "템플릿", "field": "title", "align": "left"},
        {"name": "sessions", "label": "대화", "field": "sessions"},
        {"name": "completed", "label": "완료", "field": "completed"},
        {"name": "unfinished", "label": "미완료", "field": "unfinished"},
        {"name": "avg", "label": "평균 시간", "field": "avg"},
        {"name": "up", "label": "좋아요", "field": "up"},
        {"name": "down", "label": "아쉬워요", "field": "down"},
    ]
    data = [
        {
            "title": r.title or r.ref, "sessions": r.sessions, "completed": r.completed,
            "unfinished": r.unfinished, "avg": duration(r.average_seconds) or "-", "up": r.up, "down": r.down,
        }
        for r in rows
    ]
    ui.table(columns=columns, rows=data, row_key="title").props("dense flat dark").classes("w-full trial-card")


def build_admin() -> None:
    @ui.page("/trial/admin")
    async def trial_admin(request: Request):
        if not is_owner(request):
            return owner_only_response()
        cfg = get_config().trial
        page_setup("운영자")
        header(None, owner=True)

        with ui.column().classes("trial-page px-4 gap-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("체험 서버 운영").classes("text-2xl font-semibold")
                with ui.row().classes("gap-3 text-sm"):
                    ui.link("주인 화면", "/").classes("text-indigo-300")
                    ui.link("체험 첫 화면", "/trial").classes("text-indigo-300")
            if not cfg.enabled:
                with ui.row().classes("trial-box-warn p-3 w-full"):
                    ui.label("체험 서버가 꺼져 있습니다 (conf.json 의 trial.enabled). 방문자는 들어올 수 없습니다.").classes(
                        "text-sm text-amber-200")

            gate_label = ui.label("").classes("text-sm text-slate-300")

            def tick() -> None:
                snap = get_llm_gate().snapshot()
                limit = snap["limit"] or "제한 없음"
                running = len(get_debate_runner().running_sessions())
                gate_label.set_text(
                    f"LLM 동시 요청 {snap['active']} / {limit} · 대기 {snap['waiting']}건 · 진행 중인 토론 {running}건"
                )

            tick()
            ui.timer(2.0, tick)

            @ui.refreshable
            async def body() -> None:
                async with get_session_factory()() as db:
                    report = await usage_report(db)
                with ui.row().classes("w-full gap-3"):
                    _metric("방문자", f"{report.users}")
                    _metric("대화", f"{report.sessions}")
                    _metric("결론까지 간 대화", f"{report.completed}")
                    ratio = f"{report.completed * 100 // report.sessions}%" if report.sessions else "-"
                    _metric("완료 비율", ratio)

                ui.label("템플릿별").classes("text-sm font-semibold text-slate-300 mt-2")
                if report.templates:
                    _template_rows(report.templates)
                else:
                    ui.label("아직 체험 대화가 없습니다.").classes("text-sm text-slate-500")

                ui.label("최근 의견").classes("text-sm font-semibold text-slate-300 mt-2")
                if not report.feedback:
                    ui.label("아직 남긴 의견이 없습니다.").classes("text-sm text-slate-500")
                for note in report.feedback:
                    mark = "좋아요" if note.rating > 0 else "아쉬워요" if note.rating < 0 else "의견"
                    with ui.column().classes("trial-card w-full p-3 gap-1"):
                        ui.label(f"{local_time(note.when)} · {note.user} · {note.template} · {mark}").classes(
                            "text-xs text-slate-500")
                        ui.label(note.comment).classes("text-sm text-slate-200 whitespace-pre-line")

                ui.label("방문자").classes("text-sm font-semibold text-slate-300 mt-2")
                for person in report.people:
                    _person_row(person, body)

            await body()
            with ui.row().classes("w-full justify-end"):
                ui.button("새로 고침", icon="refresh", on_click=body.refresh).props("flat no-caps color=grey-4")

            load = official_templates()
            ui.label("공식 템플릿 파일").classes("text-sm font-semibold text-slate-300 mt-2")
            ui.label(f"{len(load.templates)}개를 읽었습니다: " + ", ".join(t.title for t in load.templates.values())).classes(
                "text-sm text-slate-400")
            for name, problem in load.errors.items():
                with ui.row().classes("trial-box-warn p-3 w-full"):
                    ui.label(f"{name}: {problem}").classes("text-sm text-amber-200")

        footer_notice()


def _person_row(person: UserUsage, body) -> None:
    from datetime import datetime, timezone

    locked = person.locked_until is not None and aware(person.locked_until) > datetime.now(timezone.utc)
    with ui.row().classes("w-full items-center justify-between trial-card px-3 py-2"):
        with ui.column().classes("gap-0"):
            ui.label(person.name).classes("text-sm")
            status = []
            if locked:
                status.append("잠김")
            if not person.has_pin:
                status.append("PIN 초기화됨")
            ui.label(
                f"대화 {person.sessions}개 · 마지막 로그인 {local_time(person.last_login_at) or '-'}"
                + (f" · {' · '.join(status)}" if status else "")
            ).classes("text-xs text-slate-500")
        with ui.row().classes("gap-1"):
            if locked:
                async def do_unlock(uid=person.user_id) -> None:
                    async with get_session_factory()() as db:
                        await unlock_user(db, uid)
                    ui.notify("잠금을 풀었습니다.")
                    body.refresh()
                ui.button("잠금 해제", on_click=do_unlock).props("flat dense no-caps color=indigo-3")

            async def do_reset(uid=person.user_id, name=person.name) -> None:
                with ui.dialog() as dialog, ui.card().classes("bg-slate-900 text-slate-100 p-4 gap-3"):
                    ui.label(f"{name} 의 PIN 을 초기화할까요?").classes("font-semibold")
                    ui.label("지금 로그인이 끊기고, 다음에 들어올 때 새 PIN 을 정합니다. 대화는 그대로 남습니다.").classes(
                        "text-xs text-slate-400")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("취소", on_click=dialog.close).props("flat no-caps color=grey-4")
                        ui.button("초기화", on_click=lambda: dialog.submit(True)).props("unelevated no-caps color=red-7")
                if not await dialog:
                    return
                async with get_session_factory()() as db:
                    await reset_pin(db, uid)
                ui.notify("PIN 을 초기화했습니다.")
                body.refresh()

            ui.button("PIN 초기화", on_click=do_reset).props("flat dense no-caps color=grey-5")
