import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Sequence
from nicegui import background_tasks, ui
from sqlalchemy import desc, func, select, delete
from app.agents.pool import get_agent_pool
from app.database.models import ArtifactModel, MessageModel, SessionModel, ToolCallRecordModel
from app.database.session import get_session_factory
from app.export import build_session_markdown, safe_filename, to_local
from app.orchestration.runner import get_debate_runner
from app.session_ops import continue_session

logger = logging.getLogger(__name__)


async def first_user_message_times(db) -> Dict[str, datetime]:
    """대화별로 **사용자가 처음 말한 시각**.

    카드에 찍히던 것은 세션 행이 만들어진 시각이었습니다. 새 세션을 열어 두고
    나중에 말을 걸면 그 둘이 크게 벌어지고, 목록에서 "이 대화 언제 했더라" 를
    찾을 때 쓸모가 없습니다. 사람이 기억하는 것은 말을 건 시각입니다.

    발언 하나씩 훑지 않고 한 번의 집계로 가져옵니다. 목록은 자주 다시 그려지고,
    세션 수만큼 질의가 늘어나면 사이드바가 먼저 느려집니다.
    """
    stmt = (
        select(MessageModel.session_id, func.min(MessageModel.created_at))
        .where(MessageModel.sender_key == "user")
        .group_by(MessageModel.session_id)
    )
    result = await db.execute(stmt)
    return {session_id: started for session_id, started in result.all() if started}


async def last_completion_times(db) -> Dict[str, datetime]:
    """대화별로 **가장 최근 턴이 끝난 시각** — 그 턴을 마무리한 합성 발언의 `finished_at`.

    합성 발언에만 `turn_started_at` 이 채워지므로 그것이 "턴을 끝낸 발언" 의 표시입니다
    (`MessageModel.turn_started_at`). 발언 행의 `created_at` 은 정렬 키라 끝난 시각이
    아니고, 토론 중 개입이나 합성 뒤에 도착한 개입도 발언이라 "마지막 발언 시각" 으로는
    완료를 알 수 없습니다.

    시각 컬럼이 생기기 전의 대화에는 그 표시가 없습니다. 그런 대화는 마지막 오케스트레이터
    발언의 `created_at` 을 씁니다 — 그때의 턴은 합성으로 끝났고, 합성이 마지막
    오케스트레이터 발언입니다. 합성으로 끝난 턴이 하나도 없으면 목록에 없습니다 ("완료 전").
    """
    finished = await db.execute(
        select(MessageModel.session_id, func.max(MessageModel.finished_at))
        .where(MessageModel.turn_started_at.is_not(None))
        .group_by(MessageModel.session_id)
    )
    times = {sid: when for sid, when in finished.all() if when}

    legacy = await db.execute(
        select(MessageModel.session_id, func.max(MessageModel.created_at))
        .where(MessageModel.msg_type == "orchestrator", MessageModel.started_at.is_(None))
        .group_by(MessageModel.session_id)
    )
    for sid, when in legacy.all():
        if when and sid not in times:
            times[sid] = when
    return times


# ---------------------------------------------------------------------------- 정렬

# (키, 표시 이름, 기본 방향이 내림차순인가)
#
# 기본 방향은 사람이 그 기준으로 찾을 때 먼저 보고 싶은 쪽입니다. 이름은 가나다순,
# 시각은 최근 것부터.
SORT_KEYS = (
    ("updated", "최근 변경", True),
    ("title", "이름순", False),
    ("started", "시작 시간순", True),
    ("completed", "완료 시간순", True),
)
SORT_LABELS = {key: label for key, label, _desc in SORT_KEYS}
SORT_DEFAULT_DESC = {key: descending for key, _label, descending in SORT_KEYS}
# 정렬 선택은 보는 사람의 취향이라 DB 가 아니라 그 브라우저에 둡니다.
SORT_STORAGE_KEY = "mado.sessionSort"


@dataclass(frozen=True)
class SessionSort:
    key: str = "updated"
    descending: bool = True

    @classmethod
    def default_for(cls, key: str) -> "SessionSort":
        return cls(key, SORT_DEFAULT_DESC.get(key, True))

    def to_json(self) -> str:
        return json.dumps({"key": self.key, "desc": self.descending})

    @classmethod
    def from_json(cls, raw: Any) -> Optional["SessionSort"]:
        """브라우저에 남긴 값. 모양이 틀렸거나 모르는 키면 None (기본값을 씁니다)."""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") not in SORT_LABELS:
            return None
        return cls(str(data["key"]), bool(data.get("desc", SORT_DEFAULT_DESC[data["key"]])))


# 제목 앞의 이모지·기호. 이름순에서는 건너뜁니다 — "🛒 이커머스 …" 를 찾는 사람은 "이" 에서 찾습니다.
_LEADING_SYMBOLS = re.compile(r"^[\W_]+")


def title_sort_key(title: Optional[str]) -> str:
    """이름순 정렬 키. 대소문자를 가리지 않고, 앞쪽의 이모지·기호는 무시합니다."""
    text = (title or "Untitled Debate").strip()
    return (_LEADING_SYMBOLS.sub("", text) or text).casefold()


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite 는 시간대를 떼고 돌려주기도 합니다. 섞여서 비교가 터지지 않게 UTC 로 맞춥니다."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def sort_sessions(
    sessions: Sequence[Any],
    sort: SessionSort,
    *,
    started: Dict[str, datetime],
    completed: Dict[str, datetime],
) -> List[Any]:
    """세션을 고른 기준으로 늘어놓습니다.

    기준 값이 없는 세션(시작 전·완료 전)은 **방향과 상관없이 맨 뒤**에 둡니다. 오름차순으로
    바꿨다고 "시작 전" 세션들이 목록 맨 위를 채우면 찾던 것이 밀려납니다. 값이 같으면
    최근에 바뀐 것이 먼저입니다.
    """
    def value(s: Any) -> Any:
        if sort.key == "title":
            return title_sort_key(s.title)
        if sort.key == "started":
            return _aware(started.get(s.id))
        if sort.key == "completed":
            return _aware(completed.get(s.id))
        return _aware(s.updated_at)

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    # 동점 정리를 먼저 하고(안정 정렬), 그 위에 기준으로 한 번 더 정렬합니다.
    by_recent = sorted(sessions, key=lambda s: _aware(s.updated_at) or epoch, reverse=True)
    present = [s for s in by_recent if value(s) is not None]
    missing = [s for s in by_recent if value(s) is None]
    return sorted(present, key=value, reverse=sort.descending) + missing


# `to_local()` 은 저장 문서와 같은 규칙을 써야 해서 app.export 에 둡니다.


def event_changes_session_list(event: Dict[str, Any]) -> bool:
    """이 토론 이벤트가 세션 목록에 보이는 것을 바꾸는가.

    목록에 보이는 것은 세 가지입니다: **첫 사용자 발언 시각**, 진행 중 표시(스피너),
    그리고 제목·에이전트 수. 이 중 토론이 도는 동안 바뀌는 것은 앞의 둘입니다.

    예전에는 `turn_completed` 에서만 목록을 다시 그렸습니다. 그런데 카드의 시각은
    세션 행이 아니라 **사용자 발언 행**에서 오고, 그 행은 토론이 시작된 뒤에
    생깁니다. 그래서 첫 요청을 보내도 카드는 토론이 끝날 때까지 '시작 전' 이었고,
    새로고침해야 시각이 나타났습니다. 실패로 끝난 토론은 `turn_completed` 자체가
    나오지 않아 스피너가 계속 돌았습니다.
    """
    etype = event.get("type")
    if etype == "message_added":
        # 사용자 발언이 방금 기록되었습니다. 이 대화의 '시작 시각' 이 생기는
        # 순간입니다 (개입 발언이면 이미 있는 값이라 결과는 그대로입니다).
        return ((event.get("message") or {}).get("sender_key")) == "user"
    # 토론이 끝났습니다 — 진행 중 표시를 내려야 합니다. 오류·취소로 끝난 경우
    # `turn_completed` 는 오지 않으므로 `run_finished` 도 함께 봅니다.
    return etype in ("turn_completed", "run_finished")


class SessionSidebar:
    """Manages the session list sidebar, session creation, switching, renaming, and deletion."""

    def __init__(
        self,
        on_session_selected: Callable[[str], Coroutine[None, None, None]],
        on_new_session: Callable[[], Coroutine[None, None, None]],
    ):
        self.on_session_selected = on_session_selected
        self.on_new_session = on_new_session
        self.current_session_id: Optional[str] = None
        self.container: Optional[ui.column] = None
        self.drawer: Optional[ui.left_drawer] = None
        self.session_factory = get_session_factory()
        self.sort = SessionSort()
        self.sort_select: Optional[ui.select] = None
        self.sort_direction_btn: Optional[ui.button] = None
        self.sort_direction_tip: Optional[ui.tooltip] = None

    # ------------------------------------------------------------------ 수명

    @property
    def alive(self) -> bool:
        """이 사이드바가 아직 살아 있는 페이지에 붙어 있는지.

        `ChatFeed.alive` 와 같은 뜻입니다. 여기 이 프로퍼티가 없던 동안 사이드바는
        네 컴포넌트 중 유일하게 생존을 확인하지 않는 곳이었습니다 — 존재
        (`if not self.container`)만 보고 삭제 여부는 보지 않았습니다.

        이 클래스의 화면 작업은 거의 전부 `await` **뒤에** 옵니다 (DB 조회, 세션
        이어받기, 내보내기). 그 사이에 사람이 새로고침하거나 탭을 닫으면
        NiceGUI 가 클라이언트를 지우고, 깨어난 코드는 죽은 클라이언트에 그리게
        됩니다. 엘리먼트 *갱신* 은 NiceGUI 가 조용히 넘기지만 **새 엘리먼트 생성,
        `clear()`, `ui.notify`, `ui.download` 는** "Client has been deleted but is
        still being used" 경고를 남깁니다.
        """
        return self.container is not None and not self.container.is_deleted

    def _notify(self, *args, **kwargs) -> None:
        """페이지가 아직 있을 때만 알립니다. 없으면 조용히 버립니다."""
        if self.alive:
            ui.notify(*args, **kwargs)

    def build_ui(self) -> ui.left_drawer:
        self.drawer = ui.left_drawer(value=True, elevated=True).classes(
            "bg-slate-900 text-slate-100 p-3.5 border-r border-slate-800 flex flex-col justify-between"
        ).props("width=360")

        with self.drawer:
            with ui.column().classes("w-full flex-grow overflow-hidden"):
                # 새 세션을 만드는 길은 아래 버튼 하나로 충분합니다. 머리에 있던
                # 작은 + 버튼은 같은 일을 하면서 자리만 차지했습니다.
                with ui.row().classes("w-full items-center gap-2 mb-3 px-1"):
                    ui.icon("forum", size="md").classes("text-indigo-400")
                    ui.label("토론 세션").classes("text-base font-bold tracking-wide")

                ui.button(
                    "+ 새 토론 세션",
                    icon="chat",
                    on_click=self._handle_create_new,
                ).props("unelevated color=indigo-6 no-caps").classes("w-full mb-3 font-semibold shadow-md text-xs py-2")

                ui.separator().classes("bg-slate-800 mb-2.5")

                with ui.row().classes("w-full items-center justify-between no-wrap mb-2 px-1 gap-1"):
                    ui.label("세션 목록").classes("text-xs font-semibold text-slate-400 tracking-wider")
                    with ui.row().classes("items-center gap-0.5 no-wrap"):
                        self.sort_select = ui.select(
                            {key: label for key, label, _desc in SORT_KEYS},
                            value=self.sort.key,
                            on_change=self._on_sort_key_change,
                        ).props("dense borderless dark options-dense").classes(
                            "text-[11px] text-slate-300 min-w-[92px]"
                        )
                        self.sort_select.tooltip("세션 목록 정렬 기준")
                        self.sort_direction_btn = ui.button(
                            on_click=lambda: self._set_sort(
                                SessionSort(self.sort.key, not self.sort.descending)
                            ),
                        ).props("flat round dense size=sm color=grey-4")
                        # 툴팁은 한 번만 만들고 글자만 바꿉니다. `.tooltip()` 은 부를 때마다
                        # 새 요소를 만들어 쌓고, 페이지가 사라진 뒤에 부르면 예외가 납니다.
                        with self.sort_direction_btn:
                            self.sort_direction_tip = ui.tooltip("")
                        self._sync_sort_controls()

                # Scrollable session list with adequate right padding so borders never clip
                # session-list: Quasar 스크롤 영역의 내용 상자가 카드를 서랍보다
                # 넓게 그리는 것을 막습니다 (theme.py 참고).
                with ui.scroll_area().classes(
                    "session-list w-full flex-grow h-[calc(100vh-220px)] pr-2 py-1"
                ):
                    self.container = ui.column().classes("w-full gap-2.5 p-0.5")

        self._restore_sort_later()
        return self.drawer

    # ------------------------------------------------------------------ 정렬

    def _sync_sort_controls(self) -> None:
        if self.sort_select is not None and self.sort_select.value != self.sort.key:
            self.sort_select.set_value(self.sort.key)
        if self.sort.key == "title":
            label = "가나다 역순 (Z→A)" if self.sort.descending else "가나다순 (A→Z)"
        else:
            label = "최근 것부터" if self.sort.descending else "오래된 것부터"
        if self.sort_direction_btn is not None:
            self.sort_direction_btn.props(
                f"icon={'arrow_downward' if self.sort.descending else 'arrow_upward'}"
            )
        if self.sort_direction_tip is not None:
            self.sort_direction_tip.set_text(f"정렬 방향: {label} — 클릭하여 정렬 순서 전환")

    async def _on_sort_key_change(self, e) -> None:
        """기준을 바꾸면 그 기준의 기본 방향으로. 코드가 값을 맞춘 경우(되살리기)는 무시합니다."""
        if e.value == self.sort.key:
            return
        await self._set_sort(SessionSort.default_for(e.value))

    async def _set_sort(self, sort: SessionSort) -> None:
        if sort == self.sort:
            return
        self.sort = sort
        self._sync_sort_controls()
        if self.alive:
            try:
                ui.run_javascript(
                    f"localStorage.setItem({json.dumps(SORT_STORAGE_KEY)}, {json.dumps(sort.to_json())})"
                )
            except Exception:  # noqa: BLE001 - 기억하지 못해도 정렬은 됩니다
                logger.debug("Could not remember the session sort", exc_info=True)
        await self.refresh_list()

    def _restore_sort_later(self) -> None:
        """이 브라우저가 기억한 정렬로 되돌립니다. 페이지가 연결된 뒤에야 물어볼 수 있습니다."""
        client = ui.context.client

        async def restore() -> None:
            try:
                await client.connected(timeout=15)
                raw = await client.run_javascript(
                    f"localStorage.getItem({json.dumps(SORT_STORAGE_KEY)})", timeout=5
                )
            except Exception:  # noqa: BLE001 - 못 읽으면 기본 정렬로 둡니다
                return
            saved = SessionSort.from_json(raw)
            if saved is None or saved == self.sort or not self.alive:
                return
            self.sort = saved
            self._sync_sort_controls()
            await self.refresh_list()

        background_tasks.create(restore(), name="sidebar-restore-sort")

    async def _handle_create_new(self) -> None:
        await self.on_new_session()
        await self.refresh_list()

    async def refresh_list(self) -> None:
        """Reloads session items from database and renders them.

        순서 주의: **먼저 읽고, 그 다음에 그립니다.** 예전에는 `clear()` 를 조회보다
        앞에 두었는데, 그 사이의 `await` 동안 페이지가 사라지면 깨어난 코드가 죽은
        클라이언트에 세션 카드 수십 개를 만들었습니다. 이 메서드는 백그라운드
        토론의 이벤트 소비자가 목록이 바뀌는 이벤트마다 부르므로, 토론 중
        새로고침이면 언제든 걸릴 수 있는 자리입니다.

        조회를 먼저 하면 목록이 비었다 채워지는 깜빡임도 사라집니다.
        """
        if not self.alive:
            return

        async with self.session_factory() as db:
            stmt = select(SessionModel).order_by(desc(SessionModel.updated_at))
            res = await db.execute(stmt)
            sessions = res.scalars().all()
            started_times = await first_user_message_times(db)
            completed_times = (
                await last_completion_times(db) if self.sort.key == "completed" else {}
            )
        sessions = sort_sessions(
            sessions, self.sort, started=started_times, completed=completed_times,
        )

        # 읽는 동안 페이지가 사라졌을 수 있습니다.
        if not self.alive:
            return

        self.container.clear()

        if not sessions:
            with self.container:
                ui.label("생성된 세션이 없습니다.").classes("text-sm text-slate-500 italic p-2")
            return

        runner = get_debate_runner()

        with self.container:
            for s in sessions:
                is_active = (s.id == self.current_session_id)
                is_running = runner.is_running(s.id)
                card_classes = (
                    "w-full p-2.5 rounded-lg transition-all box-border "
                    + ("bg-indigo-950/90 border-2 border-indigo-400 text-white shadow-lg" if is_active else "bg-slate-800/70 hover:bg-slate-800 text-slate-300 border border-slate-700/60")
                )

                # 제목이 첫 줄을 통째로 씁니다. 버튼을 같은 줄에 두면 이름이 그만큼
                # 잘리는데, 목록에서 대화를 찾는 단서는 이름뿐입니다.
                with ui.card().classes(card_classes):
                    # Clickable session selection area
                    with ui.column().classes("w-full cursor-pointer gap-0 min-w-0").on(
                        "click", lambda _, sid=s.id: self._select_session(sid)
                    ):
                        with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                            if is_running:
                                # 다른 화면에 있어도 토론은 계속됩니다. 어느 세션이
                                # 돌고 있는지 목록에서 바로 보이게 합니다.
                                ui.spinner("dots", size="xs", color="indigo-4")
                            # 이름 변경은 연필 버튼으로만 합니다. 제목 더블클릭도 달아
                            # 봤지만, 첫 클릭이 세션 선택으로 이어져 목록이 다시
                            # 그려지면서 라벨이 사라지는 탓에 두 번째 클릭이 갈 곳이
                            # 없었습니다. 긴 이름은 손을 올리면 전체가 보입니다.
                            title_text = s.title or "Untitled Debate"
                            ui.label(title_text).classes(
                                "text-xs font-semibold truncate min-w-0"
                            ).tooltip(title_text)

                    with ui.row().classes(
                        "w-full items-center justify-between no-wrap mt-1 text-xs text-slate-400"
                    ):
                        with ui.row().classes("items-center gap-1.5 min-w-0 cursor-pointer").on(
                            "click", lambda _, sid=s.id: self._select_session(sid)
                        ):
                            if self.sort.key == "completed":
                                # 완료 시간으로 늘어놓았으면 카드에도 그 시각을 적습니다.
                                # 시작 시각이 찍혀 있으면 순서가 틀려 보입니다.
                                completed = completed_times.get(s.id)
                                date_str = (
                                    f"완료 {to_local(completed).strftime('%m-%d %H:%M')}"
                                    if completed else "완료 전"
                                )
                            else:
                                started = started_times.get(s.id)
                                date_str = (
                                    to_local(started).strftime("%m-%d %H:%M") if started
                                    else "시작 전"
                                )
                            ui.label(date_str).classes("text-[10px]")

                            agents_count = len(s.active_agents) if s.active_agents else 0
                            ui.badge(f"{agents_count} Agents", color="slate-700").props("dense text-[9px] text-color=grey-3")

                        # Action Buttons (Edit / Save / Delete) - Isolated from card click
                        with ui.row().classes("items-center gap-0.5 flex-shrink-0"):
                            ui.button(
                                icon="edit",
                                on_click=lambda _, s_obj=s: self._show_rename_dialog(s_obj),
                            ).props("flat round dense size=xs color=grey-4").tooltip("이름 변경")

                            ui.button(
                                icon="save",
                                on_click=lambda _, sid=s.id: self._save_session_markdown(sid),
                            ).props("flat round dense size=xs color=teal-4").tooltip(
                                "세션 전체 대화 내역을 마크다운 파일로 저장합니다."
                            )

                            continue_btn = ui.button(
                                icon="fork_right",
                                on_click=lambda _, sid=s.id: self._continue_session(sid),
                            ).props("flat round dense size=xs color=amber-4")
                            if is_running:
                                # 돌고 있는 토론의 결론은 아직 없습니다. 지금
                                # 이어받으면 인수인계 쪽지가 반쪽짜리가 됩니다.
                                continue_btn.disable()
                                continue_btn.tooltip(
                                    "토론이 진행 중입니다. 토론이 종료된 후 이어받으십시오."
                                )
                            else:
                                continue_btn.tooltip(
                                    "이어서 새 세션 시작 — 작업 공간, 지식 그래프, 에이전트 구성 및 "
                                    "이전 결론을 인계받고 대화 컨텍스트를 초기화합니다."
                                )

                            ui.button(
                                icon="delete",
                                on_click=lambda _, sid=s.id: self._show_delete_dialog(sid),
                            ).props("flat round dense size=xs color=red-4").tooltip("삭제")

    async def _continue_session(self, session_id: str) -> None:
        """컨텍스트만 비운 새 대화로 이어갑니다.

        라운드가 쌓여 에이전트들이 헛돌기 시작할 때 쓰는 길입니다. 작업 공간과
        지식 그래프, 에이전트 구성, 이전 결론은 따라오고 발언 기록만 새로
        시작합니다.

        지식 그래프를 못 옮긴 경우를 **반드시 알립니다.** 조용히 빈 그래프로
        시작하면 에이전트는 없는 기억을 조회하다 빈손으로 돌아와 지어내기
        시작합니다.
        """
        try:
            orchestrator = get_agent_pool().get_orchestrator()
            async with self.session_factory() as db:
                result = await continue_session(
                    db, session_id,
                    orchestrator_name=orchestrator.name,
                    orchestrator_role=orchestrator.role,
                )
        except Exception as e:  # noqa: BLE001 - 실패해도 사이드바는 살아야 합니다
            logger.error(f"Could not continue session {session_id}: {e}", exc_info=True)
            self._notify(f"세션을 이어받지 못했습니다: {e}", type="negative", position="bottom-right")
            return

        if result is None:
            self._notify("세션을 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        await self._select_session(result["session_id"])

        if result["memory_carried"]:
            self._notify(
                f"'{result['title']}' 세션으로 이어갑니다. 작업 공간과 지식 그래프를 인계받았습니다.",
                type="positive", position="top", close_button="확인",
            )
        else:
            self._notify(
                f"'{result['title']}' 세션으로 이어갑니다. 작업 공간은 인계받았으나 "
                f"지식 그래프는 가져오지 못했습니다 (이전 대화 기록이 없거나 "
                f"memory 서버가 비활성화되어 있습니다).",
                type="warning", position="top", close_button="확인",
            )

    async def _select_session(self, session_id: str) -> None:
        self.current_session_id = session_id
        await self.on_session_selected(session_id)
        await self.refresh_list()

    async def _save_session_markdown(self, session_id: str) -> None:
        """이 대화의 발언·도구 실행·산출물을 마크다운 한 장으로 내려받습니다.

        진행 중인 토론도 그대로 저장할 수 있습니다. 발언은 하나 끝날 때마다 DB 에
        기록되므로, 그 시점까지의 기록이 담깁니다.
        """
        try:
            async with self.session_factory() as db:
                res = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
                session_obj = res.scalar_one_or_none()
                if session_obj is None:
                    self._notify("세션을 찾을 수 없습니다.", type="warning", position="bottom-right")
                    return

                session_data = {
                    "title": session_obj.title,
                    "strategy": session_obj.strategy,
                    "max_rounds": session_obj.max_rounds,
                    "parallel_limit": session_obj.parallel_limit,
                    "active_agents": session_obj.active_agents or [],
                    "custom_instructions": session_obj.custom_instructions or "",
                    "workspace_dir": session_obj.workspace_dir or "",
                    "graph_snapshot": session_obj.graph_snapshot,
                    "created_at": session_obj.created_at,
                    "updated_at": session_obj.updated_at,
                }

                res_m = await db.execute(
                    select(MessageModel)
                    .where(MessageModel.session_id == session_id)
                    .order_by(MessageModel.created_at)
                )
                messages = [
                    {
                        "id": m.id,
                        "sender_key": m.sender_key,
                        "sender_name": m.sender_name,
                        "sender_role": m.sender_role,
                        "content": m.content,
                        "round_number": m.round_number,
                        "msg_type": m.msg_type,
                        "created_at": m.created_at,
                        "started_at": m.started_at,
                        "finished_at": m.finished_at,
                        "turn_started_at": m.turn_started_at,
                        "graph_node_id": m.graph_node_id,
                        "graph_port": m.graph_port,
                        "tool_calls": [
                            {
                                "tool_name": tc.tool_name,
                                "arguments": tc.arguments,
                                "output": tc.output,
                                "status": tc.status,
                                "security": {
                                    "decision": tc.decision or "", "risk": tc.risk or "",
                                    "rule": tc.rule or "", "approver": tc.approver or "",
                                },
                                "created_at": tc.created_at,
                            }
                            for tc in (m.tool_calls or [])
                        ],
                    }
                    for m in res_m.scalars().all()
                ]

                res_t = await db.execute(
                    select(ToolCallRecordModel)
                    .where(ToolCallRecordModel.session_id == session_id)
                    .order_by(ToolCallRecordModel.created_at)
                )
                tool_calls = [
                    {
                        "message_id": tc.message_id,
                        "agent_key": tc.agent_key,
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                        "output": tc.output,
                        "status": tc.status,
                        "security": {
                            "decision": tc.decision or "", "risk": tc.risk or "",
                            "rule": tc.rule or "", "approver": tc.approver or "",
                        },
                        "created_at": tc.created_at,
                    }
                    for tc in res_t.scalars().all()
                ]

                res_a = await db.execute(
                    select(ArtifactModel)
                    .where(ArtifactModel.session_id == session_id)
                    .order_by(ArtifactModel.created_at)
                )
                artifacts = [
                    {
                        "artifact_type": a.artifact_type,
                        "title": a.title,
                        "content": a.content,
                        "language": a.language,
                    }
                    for a in res_a.scalars().all()
                ]
        except Exception as e:  # noqa: BLE001 - 저장 실패가 화면을 죽이면 안 됩니다
            logger.error(f"Could not export session {session_id}: {e}", exc_info=True)
            self._notify(f"대화를 불러오지 못했습니다: {e}", type="negative", position="bottom-right")
            return

        # 브라우저가 받아 갈 파일이라, 페이지가 사라졌으면 보낼 곳이 없습니다.
        if not self.alive:
            logger.debug(f"Skipped the export download for {session_id}: the page is gone")
            return

        markdown = build_session_markdown(session_data, messages, artifacts, tool_calls)
        created = session_data.get("created_at")
        filename = safe_filename(session_data["title"], to_local(created) if created else None)
        ui.download(markdown.encode("utf-8"), filename)
        self._notify(
            f"'{filename}' 파일 다운로드를 시작하였습니다 (발언 {len(messages)}건).",
            type="positive", position="bottom-right",
        )

    def _show_rename_dialog(self, session_obj: SessionModel) -> None:
        with ui.dialog() as dialog, ui.card().classes("p-4 w-96 bg-slate-900 text-white border border-slate-700"):
            ui.label("세션 이름 변경").classes("text-lg font-bold mb-2")
            name_input = ui.input(value=session_obj.title or "").props("outlined dense dark").classes("w-full mb-4")

            async def do_rename():
                new_title = name_input.value.strip()
                if new_title:
                    async with self.session_factory() as db:
                        stmt = select(SessionModel).where(SessionModel.id == session_obj.id)
                        res = await db.execute(stmt)
                        curr = res.scalar_one_or_none()
                        if curr:
                            curr.title = new_title
                            await db.commit()
                    dialog.close()
                    self._notify("세션 이름이 변경되었습니다.", type="positive")
                    await self.refresh_list()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_rename).props("unelevated color=indigo-6")

        dialog.open()

    def _show_delete_dialog(self, session_id: str) -> None:
        with ui.dialog() as dialog, ui.card().classes("p-4 w-80 bg-slate-900 text-white border border-slate-700"):
            ui.label("세션 삭제").classes("text-lg font-bold text-red-400 mb-2")
            ui.label("이 세션과 모든 대화 내역 및 산출물이 삭제됩니다. 계속하시겠습니까?").classes("text-sm text-slate-300 mb-4")

            async def do_delete():
                # 이 세션의 토론이 백그라운드에서 돌고 있으면 먼저 세웁니다.
                # 그러지 않으면 방금 지운 세션에 발언을 기록하려다 실패합니다.
                get_debate_runner().forget(session_id)

                async with self.session_factory() as db:
                    await db.execute(delete(ToolCallRecordModel).where(ToolCallRecordModel.session_id == session_id))
                    await db.execute(delete(MessageModel).where(MessageModel.session_id == session_id))
                    await db.execute(delete(ArtifactModel).where(ArtifactModel.session_id == session_id))
                    await db.execute(delete(SessionModel).where(SessionModel.id == session_id))
                    await db.commit()

                dialog.close()
                self._notify("세션이 삭제되었습니다.", type="info")

                if self.current_session_id == session_id:
                    self.current_session_id = None
                    # Load another latest session or create new
                    async with self.session_factory() as db:
                        stmt = select(SessionModel).order_by(desc(SessionModel.updated_at)).limit(1)
                        res = await db.execute(stmt)
                        next_sess = res.scalar_one_or_none()

                    if next_sess:
                        self.current_session_id = next_sess.id
                        await self.on_session_selected(next_sess.id)
                    else:
                        await self.on_new_session()

                await self.refresh_list()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("삭제", on_click=do_delete).props("unelevated color=red-6")

        dialog.open()
