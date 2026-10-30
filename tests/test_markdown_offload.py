r"""긴 글을 그리는 일이 이벤트 루프를 붙잡지 않는가 (`app/ui/math_markdown.py`, ADR-033).

1. `OFFLOAD_CHARS` 이상은 일꾼 스레드에서 그립니다. 그동안 다른 코루틴이 돌고, 짧은 글은 전처럼 그 자리에서 그립니다.
2. 그리는 사이에 내용이 바뀌면 중간 것은 건너뜁니다. 요소가 사라졌거나 실패해도 뒤가 막히지 않습니다.
3. 제곱 시간이 들던 곳: 자리표시자 치환, 인라인 코드 검사, 닫히지 않은 `\(`.
4. 서버가 뜰 때 Pygments 플러그인을 미리 불러, 첫 ```` ```mermaid ```` 가 루프 위에서 import 하지 않습니다.
5. StarUML 변환은 루프 밖에서 합니다.
"""

import asyncio
import io
import threading
import time
from pathlib import Path
from typing import List

import pytest
from nicegui import core
from pygments.util import ClassNotFound

from app.ui import math_markdown
from app.ui.components import artifact_viewer
from app.ui.components.artifact_viewer import ArtifactViewer
from app.ui.math_markdown import (
    OFFLOAD_CHARS,
    MathMarkdown,
    _code_spans,
    protect_math,
    render_markdown,
    warm_up_lexer_plugins,
)

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = "fenced-code-blocks tables"
LONG = "가" * OFFLOAD_CHARS
RENDER_SECONDS = 0.3


@pytest.fixture(autouse=True)
def _fresh_caches():
    render_markdown.cache_clear()
    math_markdown.tex_to_mathml.cache_clear()
    yield
    render_markdown.cache_clear()
    math_markdown.tex_to_mathml.cache_clear()


@pytest.fixture
def slow_render(monkeypatch):
    """`RENDER_SECONDS` 동안 걸리는 렌더. 무엇을 어느 스레드에서 그렸는지 적어 둡니다."""
    calls: List[dict] = []

    def render(content, extras):
        calls.append({"content": content, "thread": threading.current_thread().name})
        time.sleep(RENDER_SECONDS)
        return f"<p>{len(content)}</p>"

    monkeypatch.setattr(math_markdown, "render_markdown", render)
    return calls


@pytest.fixture
def running_loop(monkeypatch):
    """서버가 도는 상태. 없으면 `MathMarkdown` 은 그 자리에서 그립니다."""

    def install():
        monkeypatch.setattr(core, "loop", asyncio.get_running_loop())

    return install


async def _drawn(element: MathMarkdown) -> None:
    deadline = time.monotonic() + 10
    while element._rendering:  # noqa: SLF001
        assert time.monotonic() < deadline, "그리기가 끝나지 않습니다"
        await asyncio.sleep(0.01)


# ================================================================ 어디서 그리는가


@pytest.mark.asyncio
async def test_other_coroutines_keep_running_while_a_long_text_is_drawn(ui_page, running_loop, slow_render):
    running_loop()
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    start = ticks
    with ui_page:
        element = MathMarkdown(LONG)
    assert element._props["innerHTML"] == "", "그리기 전에는 빈칸이고, 브라우저가 받을 값이 있어야 합니다"  # noqa: SLF001

    await _drawn(element)
    done.set()
    await task

    assert ticks - start >= 5, f"그리는 동안 다른 코루틴이 {ticks - start}번밖에 돌지 못했습니다"
    assert element._props["innerHTML"] == f"<p>{len(LONG)}</p>"  # noqa: SLF001
    assert slow_render[0]["thread"].startswith("mado-markdown")


@pytest.mark.asyncio
async def test_the_worker_draws_the_same_thing(ui_page, running_loop):
    running_loop()
    text = LONG + "\n\n복잡도 $O(n \\times m)$\n\n```python\nx = 1\n```\n"
    with ui_page:
        element = MathMarkdown(text, extras=EXTRAS.split())
    await _drawn(element)
    assert element._props["innerHTML"] == render_markdown(text, EXTRAS)  # noqa: SLF001
    assert "<math" in element._props["innerHTML"]  # noqa: SLF001
    assert element.content == text, "복사·내보내기가 쓰는 원문은 그대로입니다"


@pytest.mark.asyncio
async def test_a_short_text_is_drawn_on_the_spot(ui_page, running_loop, slow_render, monkeypatch):
    running_loop()
    monkeypatch.setattr(math_markdown, "render_markdown", lambda content, extras: "<p>바로</p>")
    with ui_page:
        element = MathMarkdown("가" * (OFFLOAD_CHARS - 1))
    assert element._props["innerHTML"] == "<p>바로</p>"  # noqa: SLF001
    assert not element._rendering  # noqa: SLF001


def test_without_a_running_server_a_long_text_is_drawn_on_the_spot(ui_page):
    """페이지 시험·스크립트에는 돌고 있는 루프가 없습니다. 그때 맡기면 아무도 그리지 않습니다."""
    with ui_page:
        element = MathMarkdown(LONG)
    assert LONG in element._props["innerHTML"]  # noqa: SLF001


# ================================================================ 내용이 바뀔 때


@pytest.mark.asyncio
async def test_only_the_newest_text_is_drawn_and_nothing_piles_up(ui_page, running_loop, slow_render):
    running_loop()
    rendered: List[str] = []
    with ui_page:
        element = MathMarkdown(LONG)
    element.on_rendered = lambda: rendered.append(element._props["innerHTML"])  # noqa: SLF001
    await asyncio.sleep(0.01)

    for extra in ("나", "나다", "나다라"):
        element.set_content(LONG + extra)
    await _drawn(element)

    assert [len(c["content"]) for c in slow_render] == [len(LONG), len(LONG) + 3], "중간 것은 건너뜁니다"
    assert element._props["innerHTML"] == f"<p>{len(LONG) + 3}</p>"  # noqa: SLF001
    assert rendered == [f"<p>{len(LONG) + 3}</p>"], "낡은 것을 그린 뒤에는 알리지 않습니다"


@pytest.mark.asyncio
async def test_a_text_that_became_short_is_not_overwritten_by_the_old_long_one(ui_page, running_loop, monkeypatch):
    running_loop()
    started = threading.Event()

    def render(content, extras):
        if len(content) >= OFFLOAD_CHARS:
            started.set()
            time.sleep(RENDER_SECONDS)
        return f"<p>{len(content)}</p>"

    monkeypatch.setattr(math_markdown, "render_markdown", render)
    with ui_page:
        element = MathMarkdown(LONG)
    while not started.is_set():
        await asyncio.sleep(0.01)

    element.set_content("짧음")
    assert element._props["innerHTML"] == "<p>2</p>"  # noqa: SLF001
    await _drawn(element)
    assert element._props["innerHTML"] == "<p>2</p>"  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_deleted_element_is_left_alone(ui_page, running_loop, slow_render):
    running_loop()
    rendered: List[int] = []
    with ui_page:
        element = MathMarkdown(LONG)
    element.on_rendered = lambda: rendered.append(1)
    await asyncio.sleep(0.01)

    element.delete()
    await _drawn(element)

    assert element._props["innerHTML"] == "" and not rendered  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_failed_draw_does_not_block_the_next_one(ui_page, running_loop, monkeypatch):
    running_loop()
    monkeypatch.setattr(core.app, "handle_exception", lambda exc: None)
    answers = [RuntimeError("그리지 못함"), "<p>다음</p>"]

    def render(content, extras):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(math_markdown, "render_markdown", render)
    with ui_page:
        element = MathMarkdown(LONG)
    await _drawn(element)

    element.set_content(LONG + "나")
    await _drawn(element)
    assert element._props["innerHTML"] == "<p>다음</p>"  # noqa: SLF001


def test_text_markdown_cannot_convert_is_shown_as_it_is():
    """일꾼에서 실패하면 긴 카드가 빈칸으로 남습니다. 인용 표시가 깊으면 markdown2 가 RecursionError 를 냅니다."""
    text = ">" * 2000 + " <b>가</b>"
    html = render_markdown(text, EXTRAS)
    assert html.startswith("<pre>") and "&lt;b&gt;가&lt;/b&gt;" in html


# ================================================================ 제곱 시간이 들던 곳 (앞 코드: 각각 수 초)


def test_many_formulas_are_put_back_in_one_pass():
    text = "$x_{1}$ 와 $y_{2}$\n\n" * 4000
    started = time.perf_counter()
    html = render_markdown(text, EXTRAS)
    assert time.perf_counter() - started < 3, "자리표시자마다 전체를 훑으면 8천 개에 수십 초가 듭니다"
    assert html.count("<math") == 8000 and "MADOMATH" not in html


def test_a_placeholder_is_not_confused_with_a_longer_one():
    """`X1Z` 와 `X10Z` 는 끝 글자로 갈립니다."""
    formulas = " ".join(f"$x_{{{i}}}$" for i in range(12))
    html = render_markdown(formulas, EXTRAS)
    assert html.count("<math") == 12 and "MADOMATH" not in html
    assert html.index(">0<") < html.index(">1<") < html.index(">10<") < html.index(">11<")


def test_many_inline_code_spans_take_little_time():
    started = time.perf_counter()
    protect_math("` 가 " * 20_000)
    assert time.perf_counter() - started < 1, "서로를 견주면 2만 개에 몇 초가 듭니다"


def test_code_spans_are_the_same_as_before():
    text = "앞 `a` 뒤\n\n```python\nx = `b`\n```\n\n끝 `c`\n"
    spans = [text[a:b] for a, b in _code_spans(text)]
    assert spans == ["`a`", "```python\nx = `b`\n```", "`c`"], "울타리 안의 백틱은 따로 세지 않습니다"


@pytest.mark.parametrize("opener", ["\\(", "\\["])
def test_many_unclosed_brackets_do_not_scan_to_the_end_each_time(opener):
    text = f"{opener} 가나다 " * 20_000
    started = time.perf_counter()
    _, found = protect_math(text)
    assert time.perf_counter() - started < 2, "하나마다 글 끝까지 훑으면 8초가 듭니다 (워커에서도 루프가 멈춥니다)"
    assert not found


def test_formulas_of_ordinary_length_still_match():
    inline = "a + " * 200 + "b"      # 800자
    block = "\\frac{1}{2} + " * 200 + "c"   # 2,800자
    html = render_markdown(f"\\({inline}\\)\n\n\\[{block}\\]", EXTRAS)
    assert html.count("<math") == 2 and html.count('display="block"') == 1


# ================================================================ 플러그인 미리 불러 두기


def test_warm_up_loads_the_lexer_plugins(monkeypatch):
    loaded: List[int] = []

    def plugins():
        loaded.append(1)
        return iter(())

    monkeypatch.setitem(math_markdown.get_lexer_by_name.__globals__, "find_plugin_lexers", plugins)
    warm_up_lexer_plugins()
    assert loaded, "내장 lexer 에 없는 이름이라야 플러그인까지 내려갑니다"


@pytest.mark.parametrize("error", [ClassNotFound("없음"), ImportError("깨진 플러그인")])
def test_warm_up_never_stops_the_server(monkeypatch, error):
    def fail(name):
        raise error

    monkeypatch.setattr(math_markdown, "get_lexer_by_name", fail)
    warm_up_lexer_plugins()


def test_the_server_warms_up_off_the_loop_before_it_serves():
    source = io.open(ROOT / "app/main.py", encoding="utf-8").read()
    start = source.index("async def lifespan")
    warm = source.index("await asyncio.to_thread(warm_up_lexer_plugins)", start)
    assert warm < source.index("yield", start)


# ================================================================ 화면 연결


def test_the_feed_scrolls_again_after_a_long_card_is_drawn():
    """높이는 그린 내용이 들어갈 때 바뀝니다. 그 전에 내린 스크롤은 바닥에 닿지 못합니다."""
    source = io.open(ROOT / "app/ui/components/chat_feed/cards.py", encoding="utf-8").read()
    assert "md.on_rendered = self.scroll_to_bottom" in source


@pytest.mark.asyncio
async def test_staruml_conversion_runs_off_the_loop(monkeypatch):
    threads: List[str] = []
    downloads: List[tuple] = []

    def convert(title, code):
        threads.append(threading.current_thread().name)
        return "{}"

    monkeypatch.setattr(artifact_viewer, "convert_mermaid_to_staruml_mdj", convert)
    monkeypatch.setattr(artifact_viewer.ui, "download", lambda data, name: downloads.append((data, name)))
    monkeypatch.setattr(artifact_viewer.ui, "notify", lambda *args, **kwargs: None)

    await ArtifactViewer()._download_mermaid_staruml("구조도", "graph TD\n  A --> B")

    assert threads and threads[0] != threading.main_thread().name
    assert downloads == [(b"{}", "구조도.mdj")]
