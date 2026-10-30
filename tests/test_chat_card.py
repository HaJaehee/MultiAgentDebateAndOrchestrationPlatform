"""발언 카드를 세 줄로 접을지 판단하는 규칙.

발언이 끝나면 본문을 세 줄만 남기고 접습니다. 그런데 짧은 발언까지 접으면 아무 일도
하지 않는 펼치기 버튼만 붙습니다 — 한 줄짜리 "이견 없습니다." 옆의 화살표는 누를
이유가 없는데 자리를 차지합니다.

실제 줄 수는 그려 봐야 알 수 있고, 그걸 재려면 카드마다 브라우저에 물어봐야 합니다.
대신 글자 수와 줄바꿈으로 어림합니다. 틀리는 방향은 하나뿐입니다 — 세 줄에 가까운
글에 버튼이 하나 더 붙는 것이고, 그건 눈에 거슬리는 정도입니다.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from nicegui import ui

import app.ui.components.chat_feed as chat_feed_package
from app.ui.components.chat_feed.common import (
    CLAMP_MIN_CHARS,
    CLAMP_MIN_NEWLINES,
    FEED_MARKDOWN_EXTRAS,
    SUBMIT_KEY_EVENT,
    is_clampable,
)
from app.ui.components.chat_feed.cards import CardTimeline
from app.ui.components.chat_feed.feed import ChatFeed
from app.ui.math_markdown import render_markdown
from app.ui.shared.page import MARKDOWN_EXTRAS


@pytest.mark.parametrize("content", [
    "",
    "이견 없습니다.",
    "확인했습니다. 제안하신 방향에 동의하며 추가로 덧붙일 내용은 없습니다.",
    "첫 줄\n둘째 줄",
])
def test_short_speeches_get_no_expand_button(content):
    assert is_clampable(content) is False


@pytest.mark.parametrize("content", [
    "가" * (CLAMP_MIN_CHARS + 1),
    "\n".join(f"{i}번 항목" for i in range(CLAMP_MIN_NEWLINES + 1)),
    "### 제목\n\n본문 한 줄\n\n- 항목",
])
def test_long_speeches_are_collapsible(content):
    assert is_clampable(content) is True


def test_the_threshold_is_about_three_lines():
    """카드 폭에서 한 줄이 대략 60~70자이므로 세 줄이면 200자 안팎입니다."""
    assert is_clampable("가" * CLAMP_MIN_CHARS) is False
    assert is_clampable("가" * (CLAMP_MIN_CHARS + 1)) is True


def test_a_code_block_counts_by_its_line_breaks():
    """긴 글자 수가 아니라 줄바꿈으로도 걸립니다. 코드는 짧아도 줄이 많습니다."""
    snippet = "```python\nx = 1\ny = 2\n```"
    assert len(snippet) < CLAMP_MIN_CHARS
    assert is_clampable(snippet) is True


def test_none_is_treated_as_empty():
    """스트리밍이 시작될 때의 카드는 본문이 없습니다."""
    assert is_clampable(None) is False


# --------------------------------------------------------------- 자동 스크롤
#
# 카드를 펼치는 이유는 그것을 읽기 위해서인데, 그동안에도 다른 에이전트의 글자는
# 계속 도착합니다. 매 청크마다 화면이 맨 아래로 끌려가면 읽던 자리를 잃고, 다시
# 올라가면 곧바로 또 끌려 내려갑니다. 그래서 펼쳐 둔 카드가 하나라도 있으면
# 따라가지 않고, 전부 접히면 다시 따라갑니다.
#
# 여기서 확인하는 것은 그 판단(`following`)과 장부(`_user_expanded`)입니다. 실제
# 스크롤은 브라우저가 하지만, 언제 그것을 부를지는 이 규칙이 정합니다.

LONG_ENOUGH = "가" * (CLAMP_MIN_CHARS + 1)


def _feed():
    """타임라인 부품 하나. 피드나 화면 없이 만들어집니다."""
    return CardTimeline()


def _card(msg_id: str, content: str = LONG_ENOUGH, collapsed: bool = True):
    """화면 없이 카드 상태만 흉내 냅니다 (`_render_card` 가 만드는 딕셔너리)."""
    return {"id": msg_id, "content": content, "collapsed": collapsed}


def test_a_fresh_feed_follows_the_stream():
    assert _feed().following is True


def test_expanding_a_card_stops_the_feed_from_following():
    feed = _feed()
    card = _card("m1")

    feed._toggle_card(card)

    assert card["collapsed"] is False
    assert feed.following is False


def test_collapsing_it_again_resumes_following():
    feed = _feed()
    card = _card("m1")

    feed._toggle_card(card)
    feed._toggle_card(card)

    assert feed.following is True


def test_following_resumes_only_when_every_card_is_collapsed():
    """두 개를 펼쳤다면 하나를 접어도 아직 읽는 중입니다."""
    feed = _feed()
    first, second = _card("m1"), _card("m2")

    feed._toggle_card(first)
    feed._toggle_card(second)
    feed._toggle_card(first)

    assert feed.following is False
    feed._toggle_card(second)
    assert feed.following is True


def test_a_card_drawn_open_does_not_pause_the_feed():
    """생성 중인 카드는 펼쳐진 채로 그려집니다 (`_render_card(streaming=True)`).

    그것까지 세면 토론이 도는 내내 자동 스크롤이 꺼집니다. 멈추는 것은 사람이
    버튼을 누른 경우뿐이어야 하므로, 펼친 채로 그리는 경로는 장부를 건드리지
    않습니다.
    """
    feed = _feed()
    card = _card("m1")

    feed._apply_clamp(card, collapsed=False)   # 그리는 쪽이 하는 일

    assert card["collapsed"] is False
    assert feed._user_expanded == {}
    assert feed.following is True


def test_a_short_card_never_pauses_the_feed():
    """접을 것이 없는 발언에는 펼침 버튼도 없습니다. 눌러도 멈출 이유가 없습니다."""
    feed = _feed()

    feed._toggle_card(_card("m1", content="이견 없습니다."))

    assert feed.following is True


def test_switching_sessions_forgets_the_expanded_cards():
    """카드가 사라졌는데 장부가 남으면, 다음 대화가 있지도 않은 카드 때문에
    자동 스크롤이 꺼진 채로 시작합니다."""
    feed = _feed()
    feed._toggle_card(_card("m1"))
    assert feed.following is False

    feed.clear()

    assert feed.following is True


# --------------------------------------------------------------- 카드 끝의 복사·접기
#
# 펼친 긴 카드는 다 읽은 자리가 카드의 끝인데, 복사·접기 버튼은 머리에만 있었습니다.
# 같은 두 버튼을 끝에도 두되, 보이는 것은 접을 수 있는 카드가 펼쳐져 있을 때뿐입니다.


class _Shown:
    """`set_visibility` 만 흉내 내는 화면 요소."""

    is_deleted = False

    def __init__(self):
        self.visible = None

    def set_visibility(self, visible):
        self.visible = visible


def _card_with_footer(msg_id: str, content: str = LONG_ENOUGH, collapsed: bool = True):
    return {
        **_card(msg_id, content, collapsed),
        "footer": _Shown(),
        "card": SimpleNamespace(id=7, is_deleted=False),
    }


@pytest.fixture
def scripts(monkeypatch):
    sent = []
    monkeypatch.setattr(ui, "run_javascript", lambda code, **_kwargs: sent.append(code))
    return sent


def test_the_footer_shows_only_while_a_long_card_is_open():
    feed = _feed()
    card = _card_with_footer("m1")

    feed._apply_clamp(card, collapsed=True)
    assert card["footer"].visible is False

    feed._apply_clamp(card, collapsed=False)
    assert card["footer"].visible is True


def test_a_short_card_has_no_footer():
    """한 줄짜리 발언 끝에 버튼 두 개가 더 붙으면 카드보다 버튼이 큽니다."""
    feed = _feed()
    card = _card_with_footer("m1", content="이견 없습니다.")

    feed._apply_clamp(card, collapsed=False)

    assert card["footer"].visible is False


def test_collapsing_from_the_footer_brings_the_card_back_into_view(scripts):
    """끝에서 접으면 줄어든 만큼 아래 글이 올라와, 방금 읽던 카드가 화면 위로 사라집니다."""
    feed = _feed()
    card = _card_with_footer("m1")
    feed._toggle_card(card)

    feed._collapse_from_footer(card)

    assert card["collapsed"] is True
    assert card["footer"].visible is False
    assert feed.following is True
    assert scripts == ["getHtmlElement(7)?.scrollIntoView({block: 'nearest'})"]


def test_collapsing_the_last_open_card_mid_debate_follows_the_stream_instead(scripts):
    """마지막 펼침을 접으면 피드가 흐름을 다시 따라갑니다 — 그때는 맨 아래가 볼 곳입니다."""
    feed = _feed()
    feed._active_streams["m9"] = {}
    card = _card_with_footer("m1")
    feed._toggle_card(card)

    feed._collapse_from_footer(card)

    assert card["collapsed"] is True
    assert feed.following is True
    assert scripts == []


def test_collapse_all_closes_every_card_the_reader_opened():
    feed = _feed()
    first, second = _card("m1"), _card("m2")
    feed._toggle_card(first)
    feed._toggle_card(second)

    feed.collapse_all_cards()

    assert first["collapsed"] is True and second["collapsed"] is True
    assert feed._user_expanded == {}
    assert feed.following is True


def test_collapse_all_leaves_a_card_being_written_open():
    """생성 중인 카드는 펼친 채 그려지는 것이 기본입니다. 다 쓰면 스스로 접힙니다."""
    feed = _feed()
    opened, streaming = _card("m1"), _card("m2")
    feed._toggle_card(opened)
    feed._apply_clamp(streaming, collapsed=False)   # 그리는 쪽이 하는 일

    feed.collapse_all_cards()

    assert opened["collapsed"] is True
    assert streaming["collapsed"] is False


def test_collapse_all_does_not_undo_a_manual_scroll():
    """카드를 접었다고 사람이 올려 둔 화면을 끌어내리지 않습니다 (하나씩 접을 때와 같습니다)."""
    feed = _feed()
    feed._toggle_card(_card("m1"))
    feed.handle_manual_scroll()

    feed.collapse_all_cards()

    assert feed.following is False


def test_the_collapse_all_button_shows_only_while_cards_are_open():
    feed = _feed()
    feed.collapse_all_button = _Shown()
    card = _card("m1")

    feed._toggle_card(card)
    assert feed.collapse_all_button.visible is True

    feed._toggle_card(card)
    assert feed.collapse_all_button.visible is False

    feed._toggle_card(card)
    feed.collapse_all_cards()
    assert feed.collapse_all_button.visible is False


def test_the_footer_never_unfolds_a_card(scripts):
    """끝의 버튼은 접기 전용입니다. 이미 접힌 카드에서 눌려도 펼치지 않습니다."""
    feed = _feed()
    card = _card_with_footer("m1", collapsed=True)

    feed._collapse_from_footer(card)

    assert card["collapsed"] is True
    assert feed.following is True
    assert scripts == []


# --------------------------------------------------------------- 직접 스크롤
#
# 펼침만 보던 때는, 카드를 접는 순간 스크롤이 다시 실시간 출력에 묶였습니다. 그
# 상태에서는 쏟아지는 글을 거슬러 올라갈 방법이 아예 없습니다 (올라가도 다음
# 청크에 곧바로 끌려 내려옵니다). 그래서 휠·터치 자체를 신호로 봅니다.


def test_moving_the_wheel_detaches_the_feed():
    feed = _feed()

    feed.handle_manual_scroll()

    assert feed.following is False


def test_the_direction_of_the_wheel_does_not_matter():
    """아래로 굴린 것도 '내가 볼 곳은 내가 정한다' 입니다. 방향을 따져 아래쪽만
    다시 붙이면 맨 아래 근처에서 붙었다 떨어졌다 합니다."""
    feed = _feed()

    feed.handle_manual_scroll({"deltaY": -120})
    feed.handle_manual_scroll({"deltaY": 120})

    assert feed.following is False


def test_the_jump_button_resumes_following_after_a_manual_scroll():
    feed = _feed()
    feed.handle_manual_scroll()

    feed.handle_follow()

    assert feed.following is True


def test_the_jump_button_does_not_unfold_what_the_reader_opened():
    """펼친 카드까지 풀어 버리면, 읽으려던 글이 그대로 흘러가 버립니다."""
    feed = _feed()
    feed._toggle_card(_card("m1"))
    feed.handle_manual_scroll()

    feed.handle_follow()

    assert feed.following is False, "카드가 아직 펼쳐져 있습니다"


def test_collapsing_a_card_does_not_undo_a_manual_scroll():
    """카드를 접었다고 해서, 사람이 올려 둔 화면을 끌어내리지 않습니다."""
    feed = _feed()
    card = _card("m1")
    feed._toggle_card(card)
    feed.handle_manual_scroll()

    feed._toggle_card(card)

    assert feed.following is False


@pytest.mark.asyncio
async def test_sending_something_means_watching_the_result():
    """보낸다는 것은 그 결과를 보겠다는 뜻이므로 따라가기를 되살립니다."""
    sent = []

    async def record(text):
        sent.append(text)

    feed = ChatFeed(record, on_interject=record, on_stop=record)
    # 화면 없이 입력칸만 흉내 냅니다 (`alive` 가 False 라 나머지는 그냥 지나갑니다).
    feed.input_field = SimpleNamespace(value="다음 라운드도 부탁합니다")
    feed.timeline.handle_manual_scroll()

    await feed._handle_send()

    assert sent == ["다음 라운드도 부탁합니다"]
    assert feed.timeline.following is True


@pytest.mark.asyncio
async def test_sending_does_not_close_the_card_being_read():
    """스크롤만 놓아 줍니다. 펼쳐 둔 카드는 사람이 접기 전까지 읽는 중입니다."""
    async def noop(*_args):
        pass

    feed = ChatFeed(noop, on_interject=noop, on_stop=noop)
    feed.input_field = SimpleNamespace(value="계속 진행하세요")
    feed.timeline._toggle_card(_card("m1"))

    await feed._handle_send()

    assert feed.timeline.following is False


def test_switching_sessions_also_forgets_a_manual_scroll():
    feed = _feed()
    feed.handle_manual_scroll()

    feed.clear()

    assert feed.following is True


# --------------------------------------------------------------- 보내기 / 줄바꿈
#
# 입력칸은 autogrow 라 여러 줄을 쓸 수 있는데, 예전에는 `keydown.enter` 가 조합
# 키를 가리지 않아 줄을 바꾸려는 손이 매번 요청을 보내 버렸습니다.
#
# 판정은 브라우저에서 일어나므로 여기서는 **무엇을 등록했는지**를 봅니다.
# NiceGUI 자신의 해석기로 풀어서, 그 문자열이 실제로 어떤 규칙이 되는지 확인합니다.


def _key_spec(event_type: str) -> dict:
    from nicegui.event_listener import EventListener

    return EventListener(
        element_id=1, type=event_type, args=None, handler=lambda: None,
        js_handler=None, throttle=0.0, leading_events=True, trailing_events=True,
        request=None,
    ).to_dict()


def test_plain_enter_submits():
    spec = _key_spec(SUBMIT_KEY_EVENT)

    assert spec["type"] == "keydown"
    assert spec["keys"] == ["enter"]
    # 보낼 때는 브라우저가 줄바꿈을 넣지 않도록 막습니다.
    assert "prevent" in spec["modifiers"]


def test_shift_enter_is_left_to_the_browser():
    """`exact` 가 있으면 Shift 가 눌린 Enter 는 핸들러에 오지 않습니다 (= 줄바꿈)."""
    spec = _key_spec(SUBMIT_KEY_EVENT)

    assert "exact" in spec["modifiers"]


def test_exact_comes_before_prevent():
    """순서가 뒤바뀌면 Shift+Enter 에서도 기본 동작이 막혀 줄바꿈이 사라집니다.

    Vue 는 적힌 순서대로 수식어를 적용합니다. `prevent` 가 먼저면 조합 키를
    가리기 전에 preventDefault 가 호출됩니다.
    """
    modifiers = _key_spec(SUBMIT_KEY_EVENT)["modifiers"]

    assert modifiers.index("exact") < modifiers.index("prevent")


# --------------------------------------------------------------- 카드 본문의 줄바꿈
#
# Shift+Enter 로 나눈 줄은 저장까지는 그대로 가는데, 카드가 마크다운으로 그리면서 한 줄로
# 붙었습니다. 빈 줄 없는 줄바꿈은 마크다운에서 같은 문단이고, 브라우저는 그것을 공백으로
# 보여 주기 때문입니다. 오케스트레이터 계획의 `**1. 이름**` 바로 아래 `- 지시` 도 목록이
# 되지 못하고 제목 줄에 붙었습니다. 체험·쉬운 화면의 요청 말풍선·도우미 대화·합의 상자도
# 같았습니다.

NEWLINE_EXTRAS = {"break-on-newline", "cuddled-lists"}
EVERY_SCREEN = pytest.mark.parametrize(
    "extras", [FEED_MARKDOWN_EXTRAS, MARKDOWN_EXTRAS], ids=["feed", "trial-easy"],
)


def _html(content: str, extras) -> str:
    return render_markdown(content, " ".join(extras))


@EVERY_SCREEN
def test_a_line_break_the_user_typed_stays_a_line_break(extras):
    html = _html("첫째 줄: 요구사항입니다.\n둘째 줄: 제약입니다.", extras)

    assert "요구사항입니다.<br" in html


@EVERY_SCREEN
def test_a_list_right_under_a_line_is_a_list(extras):
    html = _html("**1. Red 정찰 UAV 운용관**\n- `list_own_units` 로 점검하십시오.\n- 둘째 지시", extras)

    assert "<strong>1. Red 정찰 UAV 운용관</strong></p>" in html
    assert html.count("<li>") == 2


@EVERY_SCREEN
@pytest.mark.parametrize("content", [
    "| a | b |\n| --- | --- |\n| 1 | 2 |",
    "```python\ndef f():\n    return 1\n```",
    "1. 하나\n2. 둘\n3. 셋",
    "첫 문단.\n\n둘째 문단.",
])
def test_tables_code_lists_and_paragraphs_look_the_same_as_before(extras, content):
    before = [e for e in extras if e not in NEWLINE_EXTRAS]

    assert _html(content, extras) == _html(content, before)


def test_the_feed_extras_are_the_defaults_plus_line_breaks():
    assert set(FEED_MARKDOWN_EXTRAS) == set(ui.markdown.default_extras) | NEWLINE_EXTRAS


def test_every_markdown_in_the_trial_and_easy_screens_uses_the_shared_extras():
    """요청 말풍선, 도우미 대화, 합의·미합의 상자, 결과 — 같은 글이 화면마다 다르게 보이지 않게."""
    app_dir = Path(chat_feed_package.__file__).parents[3]
    calls = []
    for package in ("trial", "easy"):
        for path in (app_dir / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "MathMarkdown":
                    extras = next((k.value for k in node.keywords if k.arg == "extras"), None)
                    calls.append((path.relative_to(app_dir).as_posix(), getattr(extras, "id", None)))

    assert calls, "체험·쉬운 화면에서 MathMarkdown 을 찾지 못했습니다"
    assert [c for c in calls if c[1] != "MARKDOWN_EXTRAS"] == []


def test_every_markdown_in_the_feed_uses_the_feed_extras():
    """발언 카드, 계획 승인 카드, 사용자 질문 카드의 마크다운 본문 렌더링 규칙이 일관되게 적용되는지 검증합니다."""
    calls = []
    for path in Path(chat_feed_package.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "MathMarkdown":
                extras = next((k.value for k in node.keywords if k.arg == "extras"), None)
                calls.append((path.name, getattr(extras, "id", None)))

    assert sorted(calls) == [
        ("approvals.py", "FEED_MARKDOWN_EXTRAS"),  # 계획 승인 카드
        ("approvals.py", "FEED_MARKDOWN_EXTRAS"),  # 에이전트 사용자 질문 카드 (v1.5)
        ("cards.py", "FEED_MARKDOWN_EXTRAS"),
    ]
