"""발언 카드에서 여는 두 창 — 이 발언만 다시 실행, 고른 문장에 코멘트 달기 (ADR-036).

문장을 고르는 것은 브라우저의 일입니다. 드래그가 끝나면 그 자리에 작은 버튼을 띄우고, 누르면 서버에 발언 id 와 고른 글을
보냅니다 (`mado_speech_comment`, 입력창의 @언급과 같은 전역 이벤트 통로). 무엇을 달 수 있는지는 카드의 `data-mado-speech`
속성이 정합니다 — 속성이 없는 카드(유저 발언, 실패 안내)에서는 버튼이 뜨지 않습니다.

인용은 화면에 그려진 글입니다. 마크다운 표시는 빠져 있지만, 사람이 집은 문장을 에이전트에게 그대로 보여 주는 데는
그것으로 충분합니다.
"""

from typing import Any, Awaitable, Callable, Dict, Optional

from nicegui import ui

from app.ui.components.chat_feed.common import SPEECH_ATTRIBUTE

SPEECH_COMMENT_EVENT = "mado_speech_comment"

SPEECH_COMMENT_JS = """
(function () {
    if (window.MadoSpeechComment) return;
    window.MadoSpeechComment = true;

    var ATTRIBUTE = '""" + SPEECH_ATTRIBUTE + """';
    var MAX_QUOTE = 1000;
    var button = null;
    var pending = null;

    function cardOf(node) {
        var el = node && (node.nodeType === 1 ? node : node.parentElement);
        return el && el.closest ? el.closest('[' + ATTRIBUTE + ']') : null;
    }

    function hide() {
        pending = null;
        if (button) button.hidden = true;
    }

    function ensureButton() {
        if (button) return button;
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'mado-comment-button';
        button.innerHTML = '<i class="material-icons">add_comment</i>코멘트';
        button.hidden = true;
        // 눌러도 선택이 풀리지 않게 합니다.
        button.addEventListener('mousedown', function (e) { e.preventDefault(); });
        button.addEventListener('click', function () {
            if (pending) emitEvent('""" + SPEECH_COMMENT_EVENT + """', pending);
            var selection = window.getSelection();
            if (selection) selection.removeAllRanges();
            hide();
        });
        document.body.appendChild(button);
        return button;
    }

    function update() {
        var selection = window.getSelection();
        if (!selection || selection.isCollapsed || !selection.rangeCount) return hide();
        var text = selection.toString().trim();
        var card = cardOf(selection.anchorNode);
        // 카드 두 장에 걸친 선택은 어느 발언에 다는지 알 수 없습니다.
        if (!text || !card || card !== cardOf(selection.focusNode)) return hide();
        pending = { message_id: card.getAttribute(ATTRIBUTE), quote: text.slice(0, MAX_QUOTE) };
        var rect = selection.getRangeAt(0).getBoundingClientRect();
        var box = ensureButton();
        box.hidden = false;
        box.style.left = Math.min(Math.max(8, rect.right - box.offsetWidth), window.innerWidth - box.offsetWidth - 8) + 'px';
        box.style.top = Math.max(8, rect.top - box.offsetHeight - 6) + 'px';
    }

    document.addEventListener('mouseup', function () { setTimeout(update, 0); });
    document.addEventListener('keyup', function (e) { if (e.shiftKey || e.key === 'Shift') update(); });
    document.addEventListener('selectionchange', function () {
        var selection = window.getSelection();
        if (!selection || selection.isCollapsed) hide();
    });
    // 스크롤하면 선택이 움직이므로 버튼이 엉뚱한 자리에 남지 않게 합니다.
    document.addEventListener('scroll', hide, true);
})();
"""

# (발언 id, 인용, 코멘트) → 저장된 코멘트. 저장하지 못하면 None.
CommentHandler = Callable[[str, str, str], Awaitable[Optional[Dict[str, Any]]]]
# (발언 id) → 다시 실행 창에 보일 것: agent_name, position, model, options, comments. 못 열면 None.
RetryOptionsProvider = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
# (발언 id, 연결 이름표, 지침)
RetryHandler = Callable[[str, str, str], Awaitable[None]]

# 모델 고르기의 첫 줄 — 이 에이전트의 모델 그대로.
OWN_MODEL_CHOICE = ""


class SpeechActions:
    """다시 실행 창과 코멘트 창. 창은 피드에 하나씩 두고 열 때 내용을 채웁니다."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        can_comment: Callable[[str], bool],
        on_comment_saved: Callable[[str, Dict[str, Any]], None],
        on_add_comment: Optional[CommentHandler] = None,
        retry_options: Optional[RetryOptionsProvider] = None,
        on_retry_speech: Optional[RetryHandler] = None,
    ) -> None:
        self.alive = alive
        self.can_comment = can_comment
        self.on_comment_saved = on_comment_saved
        self.on_add_comment = on_add_comment
        self.retry_options = retry_options
        self.on_retry_speech = on_retry_speech
        self._retry_id = ""
        self._comment_id = ""
        self.retry_dialog: Optional[ui.dialog] = None
        self.retry_title: Optional[ui.label] = None
        self.retry_model: Optional[ui.select] = None
        self.retry_instruction: Optional[ui.textarea] = None
        self.retry_comments: Optional[ui.label] = None
        self.comment_dialog: Optional[ui.dialog] = None
        self.comment_quote: Optional[ui.label] = None
        self.comment_input: Optional[ui.textarea] = None

    def build(self) -> None:
        if self.on_retry_speech is not None and self.retry_options is not None:
            self._build_retry_dialog()
        if self.on_add_comment is not None:
            self._build_comment_dialog()
            ui.on(SPEECH_COMMENT_EVENT, self.handle_selection)

    # ------------------------------------------------------------ 다시 실행

    def _build_retry_dialog(self) -> None:
        with ui.dialog() as self.retry_dialog, ui.card().classes(
            "bg-slate-900 border border-indigo-700 text-slate-200 w-[32rem] max-w-full gap-2"
        ):
            self.retry_title = ui.label("").classes("text-base font-bold text-indigo-200")
            self.retry_model = ui.select({OWN_MODEL_CHOICE: ""}, value=OWN_MODEL_CHOICE, label="모델").props(
                "outlined dark dense"
            ).classes("w-full")
            self.retry_instruction = ui.textarea(
                label="이번에만 더할 지침 (선택)",
                placeholder="예: 비용 관점을 추가하고 더 짧게 써 주세요",
            ).props("outlined dark dense autogrow").classes("w-full")
            self.retry_comments = ui.label("").classes("text-xs text-amber-300")
            ui.label(
                "그 차례 직전까지의 기록으로 답하고, 이 발언만 새 버전으로 바뀝니다. 뒤의 발언과 결론은 다시 돌지 "
                "않으며 이전 버전을 보고 쓰인 그대로 남습니다. 작업 공간 파일은 지금 상태이고, 도구를 쓰면 실제로 "
                "실행됩니다. 이전 버전은 카드에서 넘겨 보고 되돌릴 수 있습니다."
            ).classes("text-[11px] text-slate-400")
            with ui.row().classes("w-full justify-end gap-2 mt-1"):
                ui.button("취소", on_click=self.retry_dialog.close).props("flat dense no-caps color=grey-4")
                ui.button("다시 실행", icon="replay", on_click=self._confirm_retry).props(
                    "unelevated dense no-caps color=indigo-6"
                )

    async def open_retry(self, message_id: str) -> None:
        """카드의 다시 실행 버튼. 지금 열 수 없으면 부른 쪽(`retry_options`)이 사유를 알립니다."""
        if self.retry_dialog is None or self.retry_options is None:
            return
        info = await self.retry_options(message_id)
        if not info or not self.alive():
            return
        self._retry_id = message_id
        self.retry_title.set_text(f"{info['agent_name']} 의 {info['position']} 발언만 다시 실행")
        options = {OWN_MODEL_CHOICE: f"{info['model']} (이 에이전트의 모델)", **info["options"]}
        self.retry_model.set_options(options, value=OWN_MODEL_CHOICE)
        self.retry_instruction.value = ""
        count = int(info.get("comments") or 0)
        self.retry_comments.set_text(f"이 발언에 달아 둔 코멘트 {count}건이 함께 전달됩니다." if count else "")
        self.retry_comments.set_visibility(bool(count))
        self.retry_dialog.open()

    async def _confirm_retry(self) -> None:
        self.retry_dialog.close()
        if self.on_retry_speech is not None and self._retry_id:
            await self.on_retry_speech(
                self._retry_id, self.retry_model.value or OWN_MODEL_CHOICE,
                (self.retry_instruction.value or "").strip(),
            )

    # ------------------------------------------------------------ 코멘트

    def _build_comment_dialog(self) -> None:
        with ui.dialog() as self.comment_dialog, ui.card().classes(
            "bg-slate-900 border border-amber-700 text-slate-200 w-[32rem] max-w-full gap-2"
        ):
            ui.label("이 부분에 코멘트 달기").classes("text-base font-bold text-amber-200")
            self.comment_quote = ui.label("").classes(
                "text-xs text-slate-300 italic border-l-2 border-amber-600 pl-2 max-h-32 overflow-y-auto "
                "whitespace-pre-wrap break-words w-full"
            )
            self.comment_input = ui.textarea(
                label="코멘트", placeholder="이 발언을 쓴 에이전트에게 전할 말을 적어 주십시오",
            ).props("outlined dark dense autogrow autofocus").classes("w-full")
            ui.label(
                "이 발언을 쓴 에이전트의 다음 발언에만 전달됩니다. 다른 참여자에게는 보이지 않습니다. 이번 턴에 "
                "다시 말하지 않으면 다음 턴까지 기다립니다."
            ).classes("text-[11px] text-slate-400")
            with ui.row().classes("w-full justify-end gap-2 mt-1"):
                ui.button("취소", on_click=self.comment_dialog.close).props("flat dense no-caps color=grey-4")
                ui.button("코멘트 저장", icon="add_comment", on_click=self._save_comment).props(
                    "unelevated dense no-caps color=amber-8"
                )

    def handle_selection(self, e) -> None:
        """브라우저에서 문장을 고르고 코멘트 버튼을 눌렀습니다."""
        args = e.args if isinstance(e.args, dict) else {}
        message_id = str(args.get("message_id") or "")
        if not self.alive() or self.comment_dialog is None or not message_id:
            return
        if not self.can_comment(message_id):
            ui.notify("이 발언은 아직 코멘트를 달 수 없습니다. 발언이 끝난 뒤에 달아 주십시오.",
                      type="warning", position="bottom-right")
            return
        self.open_comment(message_id, str(args.get("quote") or ""))

    def open_comment(self, message_id: str, quote: str) -> None:
        self._comment_id = message_id
        self.comment_quote.set_text(quote)
        self.comment_input.value = ""
        self.comment_dialog.open()

    async def _save_comment(self) -> None:
        text = (self.comment_input.value or "").strip()
        if not text:
            ui.notify("코멘트 내용을 적어 주십시오.", type="warning", position="bottom-right")
            return
        saved = await self.on_add_comment(self._comment_id, self.comment_quote.text, text)
        if saved is None:
            return
        self.comment_dialog.close()
        self.on_comment_saved(self._comment_id, saved)
