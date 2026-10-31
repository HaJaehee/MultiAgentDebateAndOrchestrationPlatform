"""전문가 화면(`/`): 세션 목록, 로스터, 토론 피드, 산출물 뷰어를 한 화면에 조립합니다.

토론은 이 화면이 아니라 프로세스(`DebateRunner`)가 돌립니다. 화면은 `RunBinding` 으로 진행 중인
실행에 붙어 이벤트를 그리고(`ExpertEventView`), 새로고침하면 DB 기록과 실행의 스냅샷으로 다시
세웁니다(`load_session_state`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from nicegui import run as ng_run, ui
from sqlalchemy import desc, select

from app.about import APP_NAME, APP_SHORT_NAME
from app.agents.media import attachment_notices
from app.agents.personas import effective_personas, resync_agent_configs, session_roster_agents
from app.agents.skills import scan_skills
from app.config import get_config, resolve_workspace_dir
from app.database.models import ArtifactModel, MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory
from app.orchestration import ledger_edit, speech_comments, speech_retry
from app.orchestration.graph_run import GraphRunTracker, last_graph_turn, node_labels
from app.orchestration.runner import get_debate_runner
from app.orchestration.session_title import DEFAULT_SESSION_TITLE
from app.orchestration.strategies import resolve_strategy_name
from app.orchestration.turns import KIND_SYNTHESIS, kind_of, unfinished_turn
from app.security import is_loopback
from app.session_fork import ForkError, fork_session
from app.session_ops import discard_turn
from app.ui.attention import ATTENTION_JS, WaitingTabTitle, WaitingWatcher
from app.ui.components.artifact_viewer import ArtifactViewer, merge_artifacts
from app.ui.components.chat_feed.feed import ChatFeed
from app.ui.components.chat_feed.common import clip_reloaded_output
from app.ui.components.chat_feed.speech_actions import SPEECH_COMMENT_JS
from app.ui.components.quiet_splitter import SPLITTER_FREEZE_JS, QuietSplitter
from app.ui.components.roster.control import AgentRosterControl
from app.ui.components.sidebar import SessionSidebar, active_agent_count
from app.ui.components.skill_draft_dialog import SkillDraftDialog
from app.ui.components.turn_changes import TurnChangesDialog
from app.ui.components.workspace_download import WorkspaceDownloadDialog
from app.ui.diagnostics_script import CLIENT_DIAGNOSTICS_JS
from app.ui.expert.chrome import build_about_dialog, build_header
from app.ui.expert.events import ExpertEventView
from app.ui.mention_input import MENTION_JS
from app.ui.mermaid_export import MERMAID_EXPORT_JS, MERMAID_IMAGE_JS
from app.ui.run_binding import RunBinding
from app.ui.theme import CUSTOM_CSS, FAVICON_SVG
from app.workspace_files import (
    MentionReport,
    UPLOAD_SUBDIR,
    agents_for_mentions,
    expand_mentions,
    get_workspace_index,
    skills_for_mentions,
    store_workspace_upload,
    suggest_mentions,
)

logger = logging.getLogger(__name__)


def create_ui() -> None:
    """전문가 화면(`/`)을 등록합니다."""
    @ui.page("/", title=APP_NAME, favicon=FAVICON_SVG)
    async def index_page():
        await ExpertPage().build()


class ExpertPage:
    """브라우저 탭 하나의 전문가 화면. 탭마다 하나씩 만들어지고, 탭이 닫히면 구독만 끊깁니다."""

    def __init__(self) -> None:
        self.session_factory = get_session_factory()
        self.runner = get_debate_runner()
        self.client = ui.context.client
        self.current_session_id: Optional[str] = None

    async def build(self) -> None:
        ui.dark_mode(True)
        ui.add_head_html(
            f"<style>{CUSTOM_CSS}</style>"
            f"<script>{MERMAID_EXPORT_JS}</script>"
            f"<script>{MERMAID_IMAGE_JS}</script>"
            f"<script>{SPLITTER_FREEZE_JS}</script>"
            f"<script>{MENTION_JS}</script>"
            f"<script>{SPEECH_COMMENT_JS}</script>"
            f"<script>{CLIENT_DIAGNOSTICS_JS}</script>"
            f"<script>{ATTENTION_JS}</script>"
        )

        self.sidebar = SessionSidebar(self.on_session_selected, self.on_new_session)
        self.sidebar.on_open_message = self.open_search_hit
        drawer = self.sidebar.build_ui()

        self.roster_control = AgentRosterControl(self.on_config_changed, self.on_resync_agents)
        self.chat_feed = ChatFeed(
            self.on_send_message, on_interject=self.on_interject, on_stop=self.on_stop, on_abort=self.on_abort,
            on_decision=self.on_decision,
            on_tool_approval=self.on_tool_approval,
            on_plan_approval=self.on_plan_approval,
            viewer_is_local=self.viewer_is_local,
            mention_provider=self.mention_provider,
            on_upload_file=self.on_upload_file,
            upload_destination=self.upload_destination,
            on_resume_turn=self.on_resume_turn,
            on_discard_turn=self.on_discard_turn,
            on_open_turn_changes=self.open_turn_changes,
            on_user_question=self.on_user_question,
            on_fork_turn=self.on_fork_turn,
            on_draft_skill=self.on_draft_skill,
            on_retry_speech=self.on_retry_speech,
            retry_options=self.retry_options,
            on_restore_version=self.on_restore_version,
            on_add_comment=self.on_add_comment,
            on_delete_comment=self.on_delete_comment,
        )
        # 결론으로 스킬 초안을 만드는 창. 초안은 토론과 같은 호출기로 씁니다 (바꿔 끼운 호출기도 그대로).
        self.skill_draft = SkillDraftDialog(
            self.session_factory, llm_caller=lambda: self.runner.engine.llm_caller,
            on_saved=lambda: self.roster_control.refresh_skills(force=True),
        )
        # 한 턴이 바꾼 파일을 보고 되돌리는 창. 되돌리면 그 사실이 대화에 남으므로 피드를 다시 그립니다.
        self.turn_changes = TurnChangesDialog(
            self.session_factory, is_running=self.runner.is_running, on_reverted=self.reload_current_session,
        )
        # 작업 공간 파일 다운로드. 입력란 아래와 보고서 탭, 두 곳에서 같은 창을 엽니다.
        self.workspace_download = WorkspaceDownloadDialog(self.workspace_root)
        self.roster_control.on_open_workspace_download = self.workspace_download.open
        self.roster_control.on_tool_rules_changed = self.on_tool_rules_changed
        self.roster_control.on_ledger_edited = self.on_ledger_edited
        self.roster_control.viewer_is_local = self.viewer_is_local
        self.roster_control.on_open_tool_call = self.open_tool_call
        self.artifact_viewer = ArtifactViewer(on_open_workspace_files=self.workspace_download.open)

        # 로스터가 카드를 다시 그릴 때마다 채팅 피드도 같은 겉모습을 받습니다.
        # 잠긴 대화에서는 이 값이 conf.json 이 아니라 그때 고정된 스냅샷에서 오므로,
        # 나중에 색이나 아이콘을 바꿔도 지난 발언은 그대로 남습니다.
        self.roster_control.on_roster_changed = self.chat_feed.set_agent_styles
        # 첫 화면은 카드를 곧바로 그리므로 (`build_ui`) 위 콜백을 거치지 않습니다.
        # 여기서 한 번 맞춰 두지 않으면 대화를 하나 열기 전까지 발언 카드만
        # 기본 색으로 나옵니다.
        self.chat_feed.set_agent_styles(self.roster_control.roster_agents())

        # 이 화면이 붙어 있는 백그라운드 토론의 구독. 화면이 사라지면 구독만 끊고 토론은 계속됩니다.
        self.events = ExpertEventView(self)
        self.binding = RunBinding(
            self.client, self.events.apply, alive=lambda: self.chat_feed.alive, name="expert page",
        )
        # 위 구독은 지금 보는 대화 하나의 것입니다. 승인 대기는 모든 대화를 지켜봅니다 (ADR-037).
        self.waiting = WaitingWatcher(
            self.client, self.runner,
            tab_title=WaitingTabTitle(APP_NAME, APP_SHORT_NAME),
            is_showing=self.is_showing_run,
            on_changed=self.sidebar.refresh_list,
            on_open=self.open_waiting_session,
        )

        build_header(drawer, build_about_dialog())

        # Main Splitter Workspace (58% Debate Feed / 42% Artifact Viewer)
        # 드래그하는 동안 서버가 값을 되돌려 보내지 않는 스플리터 (`QuietSplitter`).
        with QuietSplitter(value=58).classes("w-full h-[calc(100vh-65px)] overflow-hidden") as splitter:
            with splitter.before:
                # flex-nowrap 이 없으면 NiceGUI 컬럼 기본값(flex-wrap: wrap) 때문에
                # 로스터가 커졌을 때 채팅 카드가 다음 열로 줄바꿈되어 화면 밖으로 밀려납니다.
                with ui.column().classes(
                    "w-full h-full p-3 gap-3 overflow-hidden flex flex-col flex-nowrap"
                ):
                    self.roster_control.build_ui()
                    with ui.card().classes(
                        "w-full flex-grow bg-slate-900/70 border border-slate-800 p-3 rounded-xl "
                        "shadow-lg flex flex-col flex-nowrap min-h-[240px] overflow-hidden"
                    ):
                        self.chat_feed.build_ui()

            with splitter.after:
                with ui.column().classes(
                    "w-full h-full p-3 overflow-hidden flex flex-col flex-nowrap"
                ):
                    self.artifact_viewer.build_ui()

        # 처음 열면 가장 최근 대화를, 없으면 새 대화를 엽니다.
        async with self.session_factory() as db:
            stmt = select(SessionModel).order_by(desc(SessionModel.updated_at)).limit(1)
            res = await db.execute(stmt)
            latest_session = res.scalar_one_or_none()

        if latest_session:
            self.current_session_id = latest_session.id
            self.sidebar.current_session_id = latest_session.id
            await self.load_session_state(latest_session.id)
        else:
            await self.on_new_session()

        await self.sidebar.refresh_list()
        self.waiting.start()

    # ------------------------------------------------------------ 대화 선택·설정

    async def on_session_selected(self, sid: str) -> None:
        self.current_session_id = sid
        await self.load_session_state(sid)

    async def on_new_session(self) -> None:
        sid = await self.create_new_session_db()
        self.current_session_id = sid
        self.sidebar.current_session_id = sid
        self.roster_control.set_personas_locked(False)
        await self.load_session_state(sid)

    async def on_config_changed(self) -> None:
        if not self.current_session_id:
            return
        agent_count_changed = False
        async with self.session_factory() as db:
            stmt = select(SessionModel).where(SessionModel.id == self.current_session_id)
            res = await db.execute(stmt)
            curr = res.scalar_one_or_none()
            if curr:
                graph_keys: Dict[str, List[str]] = {}
                count_before = active_agent_count(curr, graph_keys)
                curr.active_agents = self.roster_control.get_active_agent_keys()
                # 지금 무엇이 있었는지 함께 남깁니다. 나중에 에이전트가 추가되면
                # 이 목록과 비교해 "새로 생긴 것" 만 켜진 채로 보여줍니다.
                curr.known_agents = self.roster_control.known_agent_keys()
                curr.strategy = self.roster_control.state.strategy_name
                curr.max_rounds = self.roster_control.state.max_rounds
                curr.parallel_limit = self.roster_control.state.parallel_limit
                curr.converge_early = self.roster_control.state.converge_early
                curr.token_budget = self.roster_control.state.token_budget
                curr.ask_user = self.roster_control.state.ask_user
                curr.sub_debates = self.roster_control.state.sub_debates
                curr.sub_debate_rounds = self.roster_control.state.sub_debate_rounds
                curr.tool_mode = self.roster_control.state.tool_mode
                curr.custom_instructions = self.roster_control.state.custom_instructions
                curr.workspace_dir = self.roster_control.state.workspace_dir
                curr.graph_id = self.roster_control.state.graph_id
                agent_count_changed = active_agent_count(curr, graph_keys) != count_before
                await db.commit()
        # 도구 보안 모드는 진행 중인 턴에도 바로 걸립니다 (다음 도구 호출부터).
        self.runner.set_tool_mode(self.current_session_id, self.roster_control.state.tool_mode)
        # 세션 카드의 'N Agents' 는 해당 대화에 실제 참여하는 에이전트 수(활성화된 에이전트, 그래프
        # 토론의 경우 그래프에 배치된 에이전트)를 표시합니다. 세션 목록을 갱신하지 않으면 세션 생성 시점의
        # 초기값(전원 활성화)이 첫 발언 전까지 잔존하여, 참여 변경이나 그래프 선택이 즉시 반영되지 않았습니다.
        # 본 함수는 지침 텍스트 수정 시에도 호출되므로, 실제 참여자 수에 변동이 발생했을 때만 목록을 다시 렌더링합니다.
        if agent_count_changed:
            await self.sidebar.refresh_list()

    async def on_resync_agents(self) -> None:
        """잠긴 대화가 고정해 둔 에이전트 구성을 지금 conf.json 값으로 다시 맞춥니다.

        대화가 자기완결적이 된 대가입니다. 엔드포인트나 API 키가 바뀌면 옛 대화가
        죽은 주소를 계속 두드리므로, 인격은 그대로 두고 운영 설정만 다시 고정합니다.
        """
        if not self.current_session_id:
            return
        pool = self.roster_control.state.agent_pool
        async with self.session_factory() as db:
            res = await db.execute(
                select(SessionModel).where(SessionModel.id == self.current_session_id)
            )
            s_obj = res.scalar_one_or_none()
            if s_obj is None:
                return
            updated = await resync_agent_configs(db, s_obj, pool)
            agents = await session_roster_agents(db, s_obj, pool)
            personas = await effective_personas(db, self.current_session_id, pool)

        self.roster_control.set_session_agents(agents)
        self.roster_control.refresh_agent_cards(personas)
        if updated:
            ui.notify(
                f"에이전트 {len(updated)}개의 모델·엔드포인트·도구 설정을 conf.json 값으로 "
                f"갱신하였습니다 ({', '.join(sorted(updated))}). 페르소나는 유지됩니다.",
                type="positive", position="bottom-right", multi_line=True,
            )
        else:
            ui.notify(
                "갱신할 에이전트가 없습니다. 본 세션의 에이전트가 모두 conf.json에서 "
                "삭제되었습니다.",
                type="warning", position="bottom-right", multi_line=True,
            )

    # ------------------------------------------------------------ @언급·업로드

    def workspace_root(self):
        """이 대화의 작업 공간. 엔진이 턴을 열 때 쓰는 것과 같은 해석입니다."""
        return resolve_workspace_dir(self.roster_control.state.workspace_dir or None)

    def mention_agents(self):
        return agents_for_mentions(
            self.roster_control.roster_agents(), self.roster_control.get_active_agent_keys()
        )

    def mention_skills(self):
        # 스킬은 실시간입니다 (켜기·끄기, 폴더 추가). 지정은 이번 턴에 한해 allowed_skills 밖의
        # 스킬도 줄 수 있으므로, 이 전문가의 것만이 아니라 쓸 수 있는 스킬 전부가 후보입니다.
        return skills_for_mentions(scan_skills())

    async def mention_provider(self, query: str) -> List[Dict[str, str]]:
        # 폴더를 훑는 일은 디스크를 읽으므로 이벤트 루프 밖에서 합니다.
        scan = await ng_run.io_bound(get_workspace_index().get, self.workspace_root())
        skills = await ng_run.io_bound(self.mention_skills)
        return [
            s.to_dict()
            for s in suggest_mentions(query, scan.entries, self.mention_agents(), skills=skills)
        ]

    async def on_upload_file(self, filename: str, content: bytes) -> str:
        return await ng_run.io_bound(store_workspace_upload, self.workspace_root(), filename, content)

    def upload_destination(self) -> str:
        return str(self.workspace_root() / UPLOAD_SUBDIR)

    async def with_references(self, text: str) -> Tuple[str, MentionReport]:
        """보낼 글의 @언급을 풀어 참조 블록을 붙입니다. 뺀 언급은 알려 줍니다.

        글에는 파일 내용을 붙이지 않습니다 — 경로만. 전문가 지목도 글로만 전달합니다. 스킬 지정과 언급한
        파일은 글과 함께 값으로도 돌려줍니다(`MentionReport`). 엔진이 그 전문가의 발언마다 스킬을 미리
        불러 두고, 받을 형식을 켠 에이전트에게 파일 내용을 실으려면 글이 아니라 값으로 받아야 합니다.
        """
        skills = await ng_run.io_bound(self.mention_skills)
        expanded, report = await ng_run.io_bound(
            expand_mentions, text, self.workspace_root(), self.mention_agents(), skills
        )
        for warning in report.warnings():
            ui.notify(warning, type="warning", position="bottom-right", multi_line=True)
        active = set(self.roster_control.get_active_agent_keys())
        notices, oversized = attachment_notices(
            report.files, [a for a in self.roster_control.roster_agents() if a.key in active],
        )
        for warning in oversized:
            ui.notify(warning, type="warning", position="bottom-right", multi_line=True)
        for notice in notices:
            ui.notify(notice, type="info", position="bottom-right", multi_line=True)
        return expanded, report

    # ------------------------------------------------------------ 턴 제어

    async def on_send_message(self, prompt: str) -> None:
        """토론을 시작하고 이 화면을 붙입니다. 실제 실행은 러너가 맡습니다.

        `ChatFeed._handle_send()` 가 이미 입력을 잠근 채로 들어오므로, 시작하지
        못하고 돌아갈 때는 반드시 다시 풀어 주어야 합니다.
        """
        try:
            prompt, mentions = await self.with_references(prompt)
            if not self.current_session_id:
                self.current_session_id = await self.create_new_session_db(title=prompt[:30])
                self.sidebar.current_session_id = self.current_session_id
                self.roster_control.state.session_id = self.current_session_id

            if self.runner.is_running(self.current_session_id):
                ui.notify("이 세션의 토론이 아직 진행 중입니다.", type="warning", position="bottom-right")
                self.chat_feed.set_busy(False, "이미 진행 중인 토론이 있습니다.", "Running")
                return

            # Save latest config before running
            await self.on_config_changed()

            # 끊긴 턴이 있었다면 새 턴이 그것을 "버려짐" 으로 적습니다. 안내도 걷습니다.
            self.chat_feed.set_unfinished_turn(None)

            # 토론은 이 페이지가 아니라 프로세스가 소유합니다. 새로고침하거나
            # 페르소나 화면에 다녀와도 중단되지 않고, 돌아오면 다시 이어 붙습니다.
            # 작업 공간이 다른 대화가 이미 돌고 있어도 막지 않습니다. 폴더마다
            # MCP 서버 묶음이 따로 뜨기 때문입니다 (`app/mcp/pool.py`). 자리가
            # 모자라면 턴이 시작된 뒤 `RuntimeCapacityError` 로 끝나고, 그
            # 사유는 아래 실패 경로를 통해 화면에 그대로 나옵니다.
            run = self.runner.start(
                self.current_session_id, prompt, self.roster_control.state.workspace_dir,
                skill_designations=mentions.skill_designations, attachments=mentions.attachment_paths,
            )
        except Exception as exc:  # noqa: BLE001 - 입력이 잠긴 채로 남으면 안 됩니다
            logger.error(f"Could not start the debate: {exc}", exc_info=True)
            self.chat_feed.set_busy(False, f"토론을 시작하지 못했습니다: {exc}", "Error")
            ui.notify(f"토론을 시작하지 못했습니다: {exc}", type="negative")
            return

        self.chat_feed.set_busy(True, run.status_text, run.round_info)
        # 이 대화가 도구를 쓰기 시작했습니다. MCP 구성은 여기서부터 잠깁니다.
        self.roster_control.refresh_mcp_lock()
        self.binding.attach(run)
        if run.status != "running":
            self.chat_feed.set_busy(False, run.status_text, run.round_info)

    def viewer_is_local(self) -> bool:
        """이 화면이 서버 PC 에서 열렸는가. 모르면 원격으로 봅니다.

        "항상 허용" 은 conf.json 을 고칩니다. 토큰 교체 버튼과 같은 원칙으로, 서버 PC
        앞의 사람만 합니다.
        """
        try:
            return is_loopback(self.client.ip)
        except Exception:  # noqa: BLE001
            return False

    async def on_tool_approval(
        self, request_id: str, decision: str, scope: List[str], reason: str,
    ) -> bool:
        """도구 승인 카드의 답을 토론에 전합니다. 받아들여졌으면 True."""
        if not self.current_session_id:
            return False
        try:
            accepted = self.runner.resolve_tool_approval(
                self.current_session_id, request_id, decision,
                scope=scope, reason=reason,
                approver="local" if self.viewer_is_local() else "remote",
            )
        except ValueError as exc:
            # 범위가 틀렸거나 지금 호출을 덮지 못합니다. 카드는 남겨 고칠 수 있게 합니다.
            ui.notify(str(exc), type="warning", position="bottom-right", multi_line=True)
            return False
        if not accepted:
            self.chat_feed.remove_approval(request_id)
            ui.notify(
                "이미 처리된 요청입니다 (유효 시간이 만료되어 거부되었거나 토론이 종료되었습니다).",
                type="warning",
                position="bottom-right",
            )
        return accepted

    async def on_plan_approval(
        self, request_id: str, decision: str, tasks: List[Dict[str, Any]],
        comment: str, task_comments: Dict[str, str],
    ) -> bool:
        """계획 승인 카드의 답을 토론에 전합니다 (ADR-028). 받아들여졌으면 True."""
        if not self.current_session_id:
            return False
        try:
            accepted = self.runner.resolve_plan_approval(
                self.current_session_id, request_id, decision,
                tasks=tasks, comment=comment, task_comments=task_comments,
                approver="local" if self.viewer_is_local() else "remote",
            )
        except ValueError as exc:
            # 의견 없는 수정 요청이거나 의견이 적힌 승인입니다. 카드는 남겨 고칠 수 있게 합니다.
            ui.notify(str(exc), type="warning", position="bottom-right", multi_line=True)
            return False
        if not accepted:
            self.chat_feed.clear_plan_approval(request_id)
            ui.notify(
                "이미 처리된 요청입니다 (대기 시간이 끝났거나 토론이 종료되었습니다).",
                type="warning",
                position="bottom-right",
            )
        return accepted

    async def on_tool_rules_changed(self, grants: List[str], denials: List[str]) -> None:
        """로스터의 규칙 창에서 지운 결과를 저장합니다.

        토론이 돌고 있으면 그 게이트가 저장합니다 (카드에서 방금 더한 규칙을 이 화면의
        옛 목록이 덮지 않도록). 돌고 있지 않으면 여기서 세션 행에 씁니다.
        """
        if not self.current_session_id:
            return
        if await self.runner.set_tool_rules(self.current_session_id, grants, denials):
            return
        async with self.session_factory() as db:
            row = await db.get(SessionModel, self.current_session_id)
            if row is not None:
                row.tool_grants = list(grants)
                row.tool_denials = list(denials)
                await db.commit()

    async def on_ledger_edited(self, expected: str, ledger: str) -> None:
        """로스터에서 고친 결정 장부를 저장하고, 무엇을 고쳤는지 안내를 피드에 올립니다 (ADR-038)."""
        if not self.current_session_id:
            return
        async with self.session_factory() as db:
            note = await ledger_edit.save_edit(db, self.current_session_id, expected, ledger)
        if note is not None:
            self.chat_feed.append_message(note)

    async def on_decision(self, extra: int, request_id: str) -> None:
        """한도에 닿은 에이전트에게 확장 여부를 알려 줍니다 (도구 상한·컨텍스트 공통).

        답이 늦어 이미 자동 처리로 넘어갔을 수 있습니다. 그때는 조용히
        지나가지 않고, 무슨 일이 있었는지 알려 줍니다.
        """
        if not self.current_session_id or not self.runner.resolve_decision(
            self.current_session_id, extra, request_id or None
        ):
            self.chat_feed.clear_budget_request(request_id or None)
            ui.notify(
                "이미 처리된 요청입니다 (유효 시간이 만료되어 자동으로 처리되었거나 토론이 종료되었습니다).",
                type="warning",
                position="bottom-right",
            )

    async def on_interject(self, text: str) -> None:
        """토론이 도는 중에 들어온 입력.

        새 턴을 열지 않고(러너는 세션당 하나만 돌립니다) 진행 중인 토론의
        다음 발언 차례에 유저 발언으로 끼워 넣습니다.
        """
        outgoing, mentions = (
            await self.with_references(text) if self.current_session_id else (text, MentionReport())
        )
        if not self.current_session_id or not self.runner.interject(
            self.current_session_id, outgoing,
            skill_designations=mentions.skill_designations, attachments=mentions.attachment_paths,
        ):
            # 마지막 발언과 화면 갱신 사이에 눌린 경우. 글을 삼키지 않습니다.
            self.chat_feed.restore_input(text)
            self.chat_feed.set_busy(False, "진행 중인 토론이 없습니다", "Ready")
            ui.notify(
                "토론이 이미 종료되어 개입 메시지를 전달하지 못했습니다. 다시 전송하시면 새로운 턴으로 진행됩니다.",
                type="warning",
                position="bottom-right",
            )

    async def on_abort(self) -> None:
        """요청을 잘못 보냈을 때. 토론을 끊고 그 요청을 없던 일로 되돌립니다.

        정지(`on_stop`)와 하는 일이 정반대입니다. 정지는 지금까지의 논의로
        결론을 받는 것이고, 여기서는 그 논의 자체가 틀린 전제 위에 서 있으므로
        남기지 않습니다. 지운 자리에 사람이 보냈던 글을 입력창으로 돌려주어
        고쳐 쓰게 합니다.
        """
        if not self.current_session_id:
            return
        current = self.runner.get(self.current_session_id)
        if current is not None and current.status == "running" and current.retry_target:
            # 끝난 턴의 기록은 그대로입니다. 다시 그리고 알리기만 합니다.
            self.binding.detach()
            await self.runner.abort(self.current_session_id)
            self.chat_feed.set_single_speech(False)
            await self.load_session_state(self.current_session_id)
            ui.notify(
                "다시 실행을 취소했습니다. 기존 발언은 그대로이며, 이미 실행된 도구의 결과는 작업 공간에 남습니다.",
                type="info", position="bottom-right", multi_line=True,
            )
            return
        produced = await self.runner.abort(self.current_session_id)
        if produced is None:
            self.chat_feed.set_busy(False, "진행 중인 토론이 없습니다", "Ready")
            ui.notify(
                "이미 종료된 토론입니다. 기록은 그대로 유지됩니다.",
                type="warning", position="bottom-right",
            )
            return

        self.binding.detach()
        async with self.session_factory() as db:
            started_over = await discard_turn(
                db,
                self.current_session_id,
                produced["message_ids"],
                produced["artifact_ids"],
                turn_id=produced.get("turn_id"),
            )

        # 기록이 바뀌었으므로 화면을 DB 기준으로 다시 세웁니다. 취소된 실행의
        # 스냅샷은 여기서 쓰이지 않습니다 (`load_session_state` 는 돌고 있는
        # 토론만 얹습니다).
        await self.load_session_state(self.current_session_id)
        restored = self.chat_feed.restore_input(produced["prompt"])
        self.chat_feed.set_busy(
            False,
            "요청을 취소하였습니다 — 수정 후 다시 전송하십시오." if restored
            else "요청을 취소하였습니다 (입력창에 작성 중이던 내용이 있어 유지하였습니다).",
            "Ready",
        )
        await self.sidebar.refresh_list()
        ui.notify(
            ("토론을 중단하고 해당 요청을 기록에서 삭제하였습니다. 입력창의 내용을 수정한 후 다시 전송하십시오."
             if restored else
             "토론을 중단하고 해당 요청을 기록에서 삭제하였습니다. 입력창에 작성 중이던 내용이 있어 "
             "기존 입력 내용을 유지하였습니다.")
            + (" 아직 시작 전이므로 에이전트 구성을 다시 변경하실 수 있습니다." if started_over else ""),
            type="info", position="bottom-right",
        )

    async def refresh_unfinished_turn(self) -> None:
        """이 대화에 끊긴 턴이 있으면 안내 줄을 띄웁니다 (ADR-024)."""
        if not self.current_session_id or self.runner.is_running(self.current_session_id):
            self.chat_feed.set_unfinished_turn(None)
            return
        async with self.session_factory() as db:
            info = await unfinished_turn(db, self.current_session_id)
        self.chat_feed.set_unfinished_turn(info.to_dict() if info else None)

    async def on_resume_turn(self, turn_id: str, mode: str) -> None:
        """끊긴 턴을 마무리하거나(`finish`) 이어 갑니다(`continue`). 실행은 러너가 맡습니다."""
        if not self.current_session_id:
            return
        if self.runner.is_running(self.current_session_id):
            ui.notify("이 세션의 토론이 아직 진행 중입니다.", type="warning", position="bottom-right")
            return
        async with self.session_factory() as db:
            info = await unfinished_turn(db, self.current_session_id)
        if info is None or info.turn_id != turn_id:
            ui.notify("이미 처리된 턴입니다.", type="warning", position="bottom-right")
            await self.load_session_state(self.current_session_id)
            return
        try:
            run = self.runner.resume(
                self.current_session_id, turn_id, mode,
                user_prompt=info.prompt, workspace=info.workspace_dir or None,
            )
        except Exception as exc:  # noqa: BLE001 - 시작하지 못한 사유를 그대로 보여 줍니다
            logger.error(f"Could not resume the turn: {exc}", exc_info=True)
            self.chat_feed.set_busy(False, f"턴을 다시 시작하지 못했습니다: {exc}", "Error")
            ui.notify(f"턴을 다시 시작하지 못했습니다: {exc}", type="negative")
            await self.refresh_unfinished_turn()
            return
        self.chat_feed.set_busy(True, run.status_text, run.round_info)
        self.roster_control.refresh_mcp_lock()
        self.binding.attach(run)
        if run.status != "running":
            self.chat_feed.set_busy(False, run.status_text, run.round_info)

    async def _show_session(self, session_id: str) -> None:
        if session_id != self.current_session_id:
            self.sidebar.current_session_id = session_id
            await self.on_session_selected(session_id)
            await self.sidebar.refresh_list()

    def is_showing_run(self, session_id: str) -> bool:
        """이 화면이 그 대화의 진행 중인 실행을 그리고 있는가 — 승인 카드가 이 화면에 떠 있는가."""
        run = self.runner.get(session_id)
        return session_id == self.current_session_id and run is not None and self.binding.run is run

    async def open_waiting_session(self, session_id: str) -> None:
        """데스크톱 알림을 눌렀습니다. 이미 그리고 있는 대화면 카드에 적던 의견이 사라지지 않게 그대로 둡니다."""
        if self.is_showing_run(session_id):
            return
        self.sidebar.current_session_id = session_id
        await self.on_session_selected(session_id)
        await self.sidebar.refresh_list()

    async def open_search_hit(self, session_id: str, message_id: str) -> None:
        """검색 결과에서 선택된 발언: 해당 대화 세션을 열고 대상 발언 카드로 이동합니다."""
        await self._show_session(session_id)
        if not self.chat_feed.reveal_message(message_id):
            ui.notify("해당 발언을 찾을 수 없습니다 (삭제되었을 수 있습니다).", type="warning", position="bottom-right")

    async def open_tool_call(self, session_id: str, message_id: Optional[str]) -> None:
        """보안 감사에서 고른 호출: 그 대화를 열고, 호출을 담은 발언 카드로 이동합니다.

        끝나지 못한 발언이 낸 호출은 담은 발언이 없으므로 대화만 엽니다.
        """
        if message_id:
            await self.open_search_hit(session_id, message_id)
        else:
            await self._show_session(session_id)

    async def on_user_question(self, request_id: str, answer: str) -> bool:
        """에이전트의 확인 질문에 답변을 전달합니다. 이미 처리되었거나 토론이 종료된 경우 안내합니다."""
        if not self.current_session_id:
            return True
        approver = "local" if self.viewer_is_local() else "remote"
        if self.runner.resolve_user_question(self.current_session_id, request_id, answer, approver=approver):
            return True
        ui.notify("이미 답변이 완료되었거나 응답 대기 시간이 만료된 질문입니다.", type="info", position="bottom-right")
        return True

    # ------------------------------------------------------------ 발언 하나 다시 실행·인라인 코멘트 (ADR-036)

    def _busy_with_debate(self) -> bool:
        if not self.current_session_id or self.runner.is_running(self.current_session_id):
            ui.notify(
                "토론이 진행 중입니다. 끝난 뒤에 다시 시도해 주십시오. 지금 전할 말은 입력창이나 코멘트로 보낼 수 있습니다.",
                type="warning", position="bottom-right", multi_line=True,
            )
            return True
        return False

    async def retry_options(self, message_id: str) -> Optional[Dict[str, Any]]:
        """다시 실행 창에 보일 것. 다시 실행할 수 없으면 사유를 알리고 None."""
        if self._busy_with_debate():
            return None
        try:
            async with self.session_factory() as db:
                row, _turn = await speech_retry.load_target(db, self.current_session_id, message_id)
                pending = await speech_comments.pending_for(
                    db, self.current_session_id, row.sender_key, message_id=message_id,
                )
        except speech_retry.SpeechRetryError as exc:
            ui.notify(str(exc), type="warning", position="bottom-right", multi_line=True)
            return None
        agents = self.roster_control.roster_agents()
        own = next((a for a in agents if a.key == row.sender_key), None)
        options = speech_retry.connection_choices(agents, get_config().llm)
        if own is not None:
            options.pop(speech_retry.connection_id(own), None)
        return {
            "agent_name": row.sender_name,
            "position": speech_retry.position_of(row),
            "model": own.model if own is not None else "",
            "options": options,
            "comments": len(pending),
        }

    def _watch_revision(self, run, message_id: str) -> None:
        self.chat_feed.reveal_message(message_id)
        self.chat_feed.set_single_speech(True)
        self.chat_feed.set_busy(True, run.status_text, run.round_info)
        self.roster_control.refresh_mcp_lock()
        self.binding.attach(run)

    async def on_retry_speech(self, message_id: str, connection: str, instruction: str) -> None:
        if self._busy_with_debate():
            return
        self._watch_revision(
            self.runner.retry_speech(
                self.current_session_id, message_id, connection=connection, instruction=instruction,
                workspace=self.roster_control.state.workspace_dir,
            ),
            message_id,
        )

    async def on_restore_version(self, message_id: str, version: int) -> None:
        if self._busy_with_debate():
            return
        self._watch_revision(
            self.runner.restore_speech(
                self.current_session_id, message_id, version, workspace=self.roster_control.state.workspace_dir,
            ),
            message_id,
        )

    async def after_speech_revision(self, event: Dict[str, Any]) -> None:
        """발언 고치기가 끝났습니다. 버전·코멘트·안내가 모두 기록에 있으므로 기록에서 다시 그리고 그 카드로 돌아갑니다."""
        self.chat_feed.set_single_speech(False)
        if not self.current_session_id:
            return
        await self.load_session_state(self.current_session_id)
        self.chat_feed.reveal_message(event["retry_target"])
        if event.get("status") == "failed":
            error = event.get("error") or "알 수 없는 오류"
            self.chat_feed.set_busy(False, f"발언을 고치지 못했습니다: {error}", "Ready")
            ui.notify(f"발언을 고치지 못했습니다: {error}", type="negative", position="bottom-right", multi_line=True)
        else:
            self.chat_feed.set_busy(False, "대기 중", "Ready")
        await self.sidebar.refresh_list()

    async def on_add_comment(self, message_id: str, quote: str, comment: str) -> Optional[Dict[str, Any]]:
        if not self.current_session_id:
            return None
        try:
            async with self.session_factory() as db:
                saved = await speech_comments.add_comment(db, self.current_session_id, message_id, quote, comment)
        except speech_comments.SpeechCommentError as exc:
            ui.notify(str(exc), type="warning", position="bottom-right", multi_line=True)
            return None
        ui.notify(
            "코멘트를 저장했습니다. 이 발언을 쓴 에이전트의 다음 발언에 전달합니다.", type="info", position="bottom-right",
        )
        return saved

    async def on_delete_comment(self, comment_id: str) -> bool:
        if not self.current_session_id:
            return False
        async with self.session_factory() as db:
            deleted = await speech_comments.delete_pending(db, self.current_session_id, comment_id)
        if not deleted:
            ui.notify("이미 전달된 코멘트입니다.", type="warning", position="bottom-right")
        return deleted

    async def on_draft_skill(self, turn_id: str) -> None:
        await self.skill_draft.open(self.current_session_id, turn_id)

    async def on_fork_turn(self, turn_id: str) -> None:
        """현재 턴 시점으로 대화 세션을 분기(Fork)하여 생성하고 이동합니다 (`app/session_fork.py`)."""
        source = self.current_session_id
        if not source:
            return
        if self.runner.is_running(source):
            ui.notify("토론이 진행 중입니다. 턴이 완료된 후에 세션을 분기해 주세요.", type="warning", position="bottom-right")
            return
        try:
            async with self.session_factory() as db:
                result = await fork_session(db, source, turn_id)
        except ForkError as exc:
            ui.notify(str(exc), type="warning", position="bottom-right")
            return
        where = (
            "해당 턴 완료 시점의 스냅샷으로 새 작업 공간 디렉터리를 생성했습니다"
            if result.from_snapshot else
            "해당 턴의 스냅샷이 없어 현재 작업 공간 디렉터리를 복사했습니다"
        )
        ui.notify(
            f"'{result.title}' 세션을 분기 생성했습니다 (발언 {result.messages}건). {where}: {result.workspace}. "
            "전략·참여자·모델을 조정한 후 새 요청을 시작할 수 있으며, 사이드바의 비교 버튼을 통해 원본 세션과 나란히 비교할 수 있습니다.",
            type="positive", position="bottom-right", multi_line=True, timeout=10000,
        )
        self.sidebar.current_session_id = result.session_id
        await self.on_session_selected(result.session_id)
        await self.sidebar.refresh_list()

    async def open_turn_changes(self, turn_id: str) -> None:
        await self.turn_changes.open(self.current_session_id, turn_id)

    async def reload_current_session(self) -> None:
        if self.current_session_id:
            await self.load_session_state(self.current_session_id)

    async def on_discard_turn(self, turn_id: str) -> None:
        """끊긴 턴을 버립니다. 긴급 종료처럼 요청 글을 입력창으로 돌려줍니다."""
        if not self.current_session_id:
            return
        async with self.session_factory() as db:
            info = await unfinished_turn(db, self.current_session_id)
            if info is None or info.turn_id != turn_id:
                ui.notify("이미 처리된 턴입니다.", type="warning", position="bottom-right")
                started_over = None
            else:
                started_over = await discard_turn(db, self.current_session_id, [], [], turn_id=turn_id)
        await self.load_session_state(self.current_session_id)
        if started_over is None:
            return
        restored = self.chat_feed.restore_input(info.prompt)
        await self.sidebar.refresh_list()
        ui.notify(
            "끊긴 턴을 기록에서 삭제하였습니다."
            + (" 요청 내용을 입력창에 복원하였습니다." if restored else "")
            + (" 아직 시작 전이므로 에이전트 구성을 다시 변경하실 수 있습니다." if started_over else ""),
            type="info", position="bottom-right",
        )

    async def on_stop(self) -> None:
        """남은 라운드를 접고 지금까지의 토론으로 마무리하도록 요청합니다."""
        if not self.current_session_id or not self.runner.request_stop(self.current_session_id):
            self.chat_feed.set_busy(False, "진행 중인 토론이 없습니다", "Ready")
            return

    # ------------------------------------------------------------ 대화 기록 불러오기

    async def create_new_session_db(self, title: str = DEFAULT_SESSION_TITLE) -> str:
        sid = str(uuid.uuid4())
        async with self.session_factory() as db:
            new_session = SessionModel(
                id=sid,
                title=title,
                strategy=self.roster_control.state.strategy_name,
                max_rounds=self.roster_control.state.max_rounds,
                parallel_limit=self.roster_control.state.parallel_limit,
                converge_early=self.roster_control.state.converge_early,
                token_budget=self.roster_control.state.token_budget,
                # 다른 설정과 달리 지금 보던 대화에서 물려받지 않고 늘 켠 채로 시작합니다. 이 칸 이전의 대화는
                # 모두 꺼져 있어(체험 화면을 위한 기본값) 물려받으면 새 대화도 영영 꺼진 채가 됩니다.
                ask_user=True,
                sub_debates=self.roster_control.state.sub_debates,
                sub_debate_rounds=self.roster_control.state.sub_debate_rounds,
                tool_mode=self.roster_control.state.tool_mode,
                active_agents=self.roster_control.get_active_agent_keys(),
                known_agents=self.roster_control.known_agent_keys(),
                custom_instructions=self.roster_control.state.custom_instructions,
                workspace_dir=self.roster_control.state.workspace_dir,
                graph_id=self.roster_control.state.graph_id,
            )
            db.add(new_session)
            await db.commit()
        return sid

    async def load_session_state(self, sid: str) -> None:
        """세션 화면을 DB 기록과 진행 중인 토론 스냅샷으로 다시 구성합니다."""
        self.binding.detach()
        self.events.graph_tracker = None
        graph_spec: Optional[Dict[str, Any]] = None
        self.chat_feed.clear()
        self.artifact_viewer.render_artifacts([])

        async with self.session_factory() as db:
            # Load session settings
            stmt_s = select(SessionModel).where(SessionModel.id == sid)
            res_s = await db.execute(stmt_s)
            s_obj = res_s.scalar_one_or_none()
            if s_obj:
                pool = self.roster_control.state.agent_pool
                personas = await effective_personas(db, sid, pool)
                # 잠긴 대화는 잠글 때 고정된 에이전트로 로스터를 그립니다.
                # conf.json 에서 지워진 에이전트도 이 대화에서는 계속 발언합니다.
                frozen = await session_roster_agents(db, s_obj, pool)
                graph_spec = s_obj.graph_snapshot or None
                self.roster_control.load_from_session(
                    active_keys=s_obj.active_agents or [],
                    known_keys=s_obj.known_agents or [],
                    strategy=resolve_strategy_name(s_obj.strategy),
                    max_rounds=s_obj.max_rounds or 3,
                    parallel_limit=s_obj.parallel_limit or 3,
                    converge_early=bool(s_obj.converge_early),
                    token_budget=s_obj.token_budget or 0,
                    ask_user=bool(s_obj.ask_user),
                    sub_debates=bool(s_obj.sub_debates),
                    sub_debate_rounds=s_obj.sub_debate_rounds,
                    tool_mode=s_obj.tool_mode or "",
                    tool_grants=list(s_obj.tool_grants or []),
                    tool_denials=list(s_obj.tool_denials or []),
                    instructions=s_obj.custom_instructions or "",
                    session_id=sid,
                    personas_locked=bool(s_obj.personas_locked),
                    personas=personas,
                    workspace_dir=s_obj.workspace_dir or "",
                    session_agents=frozen,
                    decision_ledger=s_obj.decision_ledger or "",
                    graph_id=s_obj.graph_id or "",
                )

            # Load messages
            stmt_m = select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
            res_m = await db.execute(stmt_m)
            messages_db = res_m.scalars().all()
            # 다시 실행한 발언의 지난 버전과 인라인 코멘트 (ADR-036).
            revisions = await speech_retry.revisions_with_tools(db, messages_db)
            for entries in revisions.values():
                for entry in entries:
                    for call in entry["tool_calls"]:
                        call["output"] = clip_reloaded_output(call["output"])
            comments = await speech_comments.comments_by_message(db, sid)

            formatted_msgs: List[Dict[str, Any]] = []
            for m in messages_db:
                formatted_msgs.append({
                    "id": m.id,
                    "sender_key": m.sender_key,
                    "sender_name": m.sender_name,
                    "sender_role": m.sender_role,
                    "content": m.content,
                    "round_number": m.round_number,
                    "msg_type": m.msg_type,
                    # 카드에 시작·종료 시각을 적습니다. `created_at` 은 정렬 키라
                    # 시각으로 쓰지 않지만, 두 컬럼이 생기기 전의 발언은 그것밖에
                    # 없으므로 함께 넘깁니다 (`app.timestamps.speech_timing`).
                    "created_at": m.created_at,
                    "started_at": m.started_at,
                    "finished_at": m.finished_at,
                    "turn_started_at": m.turn_started_at,
                    "graph_node_id": m.graph_node_id,
                    "graph_port": m.graph_port,
                    "turn_meta": m.turn_meta,
                    "llm_meta": m.llm_meta,
                    "revisions": revisions.get(m.id),
                    "comments": comments.get(m.id, []),
                    "tool_calls": [
                        {
                            "tool_name": tc.tool_name,
                            "arguments": tc.arguments,
                            "output": clip_reloaded_output(tc.output),
                            "status": tc.status,
                            "security": {
                                "decision": tc.decision or "",
                                "risk": tc.risk or "",
                                "rule": tc.rule or "",
                                "approver": tc.approver or "",
                            },
                        }
                        for tc in m.tool_calls
                    ] if hasattr(m, "tool_calls") and m.tool_calls else [],
                })

            # 턴마다 결론 카드 → (발언 id, 턴 id, 바뀐 파일 수). 체크포인트가 없는 턴은 파일 수가 None 입니다.
            checkpoints = {
                turn_id: checkpoint
                for turn_id, checkpoint in (await db.execute(
                    select(TurnModel.id, TurnModel.checkpoint).where(TurnModel.session_id == sid)
                )).all()
            }
            turn_change_counts: List[Tuple[str, str, Optional[int]]] = [
                (m.id, m.turn_id, (checkpoints[m.turn_id] or {}).get("changed"))
                for m in messages_db
                if m.turn_id in checkpoints and kind_of(m) == KIND_SYNTHESIS
            ]

            # Load artifacts
            stmt_a = select(ArtifactModel).where(ArtifactModel.session_id == sid).order_by(ArtifactModel.created_at)
            res_a = await db.execute(stmt_a)
            arts_db = res_a.scalars().all()

            formatted_arts = [
                {
                    "id": a.id,
                    "artifact_type": a.artifact_type,
                    "title": a.title,
                    "content": a.content,
                    "language": a.language,
                }
                for a in arts_db
            ]

        # 진행 중인 토론이 있으면, 아직 DB 에 커밋되지 않은 발언까지 얹습니다.
        # 끝난 토론은 전부 DB 에 들어가 있으므로 스냅샷을 보지 않습니다.
        # (스냅샷을 계속 신뢰하면 지난 턴의 산출물이 DB 기록을 덮어씁니다.)
        run = self.runner.get(sid)
        running = run is not None and run.status == "running"
        streaming_ids: Set[str] = set()
        if running:
            snapshot = run.snapshot()
            known = {m["id"] for m in formatted_msgs if m.get("id")}
            formatted_msgs.extend(m for m in snapshot["messages"] if m.get("id") not in known)
            streaming_ids = snapshot["streaming_ids"]
            # 다시 실행 중인 발언은 기록에 옛 글이 있습니다. 조각이 그 뒤에 붙지 않게 지금까지 흐른 글로 바꿉니다.
            streamed = {m["id"]: m for m in snapshot["messages"] if m.get("id") in streaming_ids}
            formatted_msgs = [streamed.get(m.get("id"), m) for m in formatted_msgs]
            if snapshot["artifacts"]:
                # 스냅샷은 이번 턴 것뿐입니다. 덮어쓰면 이전 턴 산출물이 화면에서 사라집니다.
                formatted_arts = merge_artifacts(formatted_arts, snapshot["artifacts"])

        # 그래프 토론의 실행 표시와 노드 배지. 도는 중이면 러너가 들고 있는 그래프와 진행 상태를,
        # 끝났으면 그 턴에 고정한 그래프(`sessions.graph_snapshot`)와 기록을 씁니다.
        graph_state = snapshot.get("graph") if running else None
        if graph_state and graph_state.get("spec"):
            graph_spec = graph_state["spec"]
        self.chat_feed.set_graph_labels(node_labels(graph_spec))
        if graph_spec:
            # 도는 중이면 러너가 모은 이번 턴 발언을 씁니다. DB 기록을 합성 발언으로 잘라 내면, 합성이
            # 막 기록되고 턴이 아직 닫히지 않은 순간에 이번 턴이 통째로 "지난 턴" 쪽으로 넘어갑니다.
            history = snapshot["messages"] if running else last_graph_turn(formatted_msgs, running=False)
            self.events.graph_tracker = GraphRunTracker.from_history(graph_spec, history, state=graph_state)
        self.roster_control.set_graph_run(self.events.graph_tracker, active=running)

        if formatted_msgs:
            self.chat_feed.render_all(formatted_msgs, streaming_ids=streaming_ids)
            # 결론 카드마다 그 턴이 작업 공간에서 바꾼 파일 수 (`TurnModel.checkpoint`).
            for message_id, turn_id, changed in turn_change_counts:
                self.chat_feed.set_turn_actions(message_id, turn_id, changed)
        if formatted_arts:
            self.artifact_viewer.render_artifacts(formatted_arts)

        self.roster_control.refresh_mcp_lock()

        self.chat_feed.set_single_speech(running and bool(run.retry_target))
        if running:
            self.chat_feed.set_busy(run.busy, run.status_text, run.round_info)
            # 새로고침 뒤에도 "정지 중" 이라는 사실이 남아 있어야, 이미 접수된
            # 요청을 다시 누르지 않습니다.
            self.chat_feed.set_stop_pending(bool(snapshot["stop_requested"]))
            # 답을 기다리는 물음은 화면이 아니라 토론이 들고 있습니다. 새로
            # 붙은 화면에서도 같은 선택지가 보여야, 새로고침 한 번으로 답할
            # 곳을 잃지 않습니다.
            self.chat_feed.set_decision_request(snapshot.get("decision_request"))
            self.chat_feed.set_approvals(snapshot.get("tool_approvals"))
            self.chat_feed.set_questions(snapshot.get("user_questions"))
            self.chat_feed.set_plan_approval(snapshot.get("plan_approval"))
            # 이번 턴에 갱신된 장부는 턴이 끝나야 DB 에 들어갑니다.
            if snapshot.get("decision_ledger") is not None:
                self.roster_control.set_decision_ledger(snapshot["decision_ledger"])
            self.binding.attach(run)
        else:
            self.chat_feed.set_busy(False, "대기 중", "Ready")
            self.chat_feed.set_budget_request(None)
        await self.refresh_unfinished_turn()
