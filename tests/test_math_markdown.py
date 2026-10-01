r"""발언·결론의 LaTeX 수식을 MathML 로 그립니다 (`app/ui/math_markdown.py`).

* `$…$`, `$$…$$`, `\(…\)`, `\[…\]` → MathML. 마크다운으로 바꾸기 **전에** 빼 두므로 `\(`, `x_i` 가 망가지지 않습니다.
* 가격(`$5 에서 $10`)·각주(`\[1\]`)·코드는 건드리지 않습니다.
* `$` 없이 쓴 `\rightarrow`, `\times` 는 유니코드 기호로. 경로(`C:\to\x`)는 그대로.
* 변환기가 없거나 실패하면 유니코드로 푼 글로 대신합니다. 모델이 넣은 `<script>` 는 이스케이프합니다.
"""

import io
from pathlib import Path

import pytest

from app.ui import math_markdown
from app.ui.math_markdown import (
    MathMarkdown,
    _clean_mathml,
    protect_math,
    render_markdown,
    replace_bare_commands,
    tex_to_unicode,
)

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = "fenced-code-blocks tables"


def _render(text: str) -> str:
    return render_markdown(text, EXTRAS)


@pytest.fixture(autouse=True)
def _fresh_caches():
    render_markdown.cache_clear()
    math_markdown.tex_to_mathml.cache_clear()
    yield
    render_markdown.cache_clear()
    math_markdown.tex_to_mathml.cache_clear()


# ================================================================ 수식 구간


def test_arrows_and_operators_in_dollars_become_mathml():
    html = _render(r"요청 $\rightarrow$ 캐시, 실패 시 $A \leftarrow B$, 복잡도 $O(n \times m)$")
    assert html.count("<math") == 3
    assert "&#x02192;" in html and "&#x02190;" in html and "&#x000D7;" in html   # → ← ×
    assert "\\rightarrow" not in html and "$" not in html


def test_display_math_and_both_bracket_forms():
    html = _render("평균:\n\n$$\\frac{1}{n}\\sum_{i=1}^{n} t_i$$\n\n괄호 \\(p \\cdot q\\) 와 \\[ E = mc^2 \\]")
    assert html.count("<math") == 3
    assert html.count('display="block"') == 2
    assert "<mfrac>" in html and "<msup>" in html
    assert "\\(" not in html and "\\[" not in html


def test_markdown_no_longer_eats_underscores_or_backslashes_inside_math():
    html = _render(r"조건 $x_i \le 10$ 과 $y_j$")
    assert "<em>" not in html, "x_i … y_j 의 _ 가 기울임으로 바뀌면 안 됩니다"
    assert "<msub>" in html


def test_prices_footnotes_and_shell_variables_are_not_math():
    text = "가격은 $5 에서 $10 사이, $100/$200, 각주 \\[1\\], 경로 $HOME/$PATH"
    html = _render(text)
    assert "<math" not in html
    assert "$5 에서 $10 사이, $100/$200" in html and "$HOME/$PATH" in html


def test_a_single_variable_and_korean_after_it():
    html = _render("변수 $n$개와 $x'$")
    assert html.count("<math") == 2
    assert "개와" in html


def test_function_calls_absolute_values_and_differences():
    html = _render("$O(n)$, $f(x)$, $|a|$, $a-b$, $2*k$")
    assert html.count("<math") == 5


def test_code_is_left_alone():
    text = "인라인 `$\\times$` 그리고\n\n```python\nx = \"$\\\\leftarrow$\"\n```\n"
    protected, found = protect_math(text)
    assert not found and protected == text
    html = _render(text)
    assert "<math" not in html and "<code>$\\times$</code>" in html


def test_norm_bars_do_not_stretch_but_left_right_ones_do():
    html = _render(r"$\lVert x \rVert_2 = \sqrt{\sum_i x_i^2}$ 와 $\left| \frac{a}{b} \right|$")
    assert html.count('<mo stretchy="false">&#x02016;</mo>') == 2, "LaTeX 의 \\lVert 는 늘어나지 않습니다"
    assert '<mo stretchy="true" fence="true" form="prefix">&#x0007C;</mo>' in html


def test_cases_keep_their_column_alignment_for_the_stylesheet():
    from app.ui.theme import CUSTOM_CSS

    html = _render(r"$$f(x) = \begin{cases} 1 & x \ge 0 \\ 0 & x < 0 \end{cases}$$")
    assert '<mtd columnalign="left">' in html
    assert 'mtd[columnalign="left"]' in CUSTOM_CSS and "math mtd {" in CUSTOM_CSS


def test_a_formula_in_a_table_cell():
    html = _render("| 항목 | 값 |\n| --- | --- |\n| 노름 | $\\lVert x \\rVert_2$ |\n| 절댓값 | $|x|$ |\n")
    assert "<table>" in html and html.count("<math") == 2


def test_an_unfinished_formula_while_streaming_stays_text():
    html = _render("지금 쓰는 중 $O(n \\times")
    assert "<math" not in html and "$O(n" in html
    assert "MADOMATH" not in _render("$$\\frac{1}{2}")


# ================================================================ 맨몸 명령과 대신 보여 줄 글


def test_bare_commands_become_symbols_but_paths_do_not():
    assert replace_bare_commands(r"상태 A \rightarrow 상태 B, 2 \times 3, \alpha \in S") == \
        "상태 A → 상태 B, 2 × 3, α ∈ S"
    assert replace_bare_commands(r"C:\to\file, D:\work\times, \unknown") == r"C:\to\file, D:\work\times, \unknown"
    html = _render(r"상태 A \Rightarrow 상태 B")
    assert "상태 A ⇒ 상태 B" in html


def test_tex_falls_back_to_unicode_text():
    assert tex_to_unicode(r"\frac{n(n+1)}{2} \le x^2 + y_{12} \to \infty") == "(n(n+1))/(2) ≤ x² + y₁₂ → ∞"
    assert tex_to_unicode(r"\text{avg} \approx 120\,\text{ms}") == "avg ≈ 120 ms"
    assert tex_to_unicode(r"x^{ab}") == "x^ab", "첨자로 못 바꾸는 글자가 섞이면 표기를 남깁니다"


def test_without_the_converter_formulas_still_read(monkeypatch):
    monkeypatch.setattr(math_markdown, "_latex2mathml", None)
    html = _render(r"복잡도 $O(n \times m)$ 와 $$a \le b$$")
    assert "<math" not in html
    assert '<span class="mado-math-fallback">O(n × m)</span>' in html
    assert '<div class="mado-math-fallback">a ≤ b</div>' in html


# ================================================================ 안전


def test_script_from_the_model_is_escaped():
    html = _render(r"위험 $\text{<script>alert(1)</script>}$")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_links_and_event_attributes_are_stripped_from_mathml():
    dirty = '<math><mi href="javascript:x" onclick="y()">a</mi><mo>&#x0003C;</mo><mtext><img src=x></mtext></math>'
    clean = _clean_mathml(dirty)
    assert "href" not in clean and "onclick" not in clean
    assert "&#x0003C;" in clean, "변환기가 만든 문자 참조는 그대로 둡니다"
    assert "&lt;img src=x&gt;" in clean


# ================================================================ 화면 연결


def test_math_markdown_keeps_the_raw_content_for_copying():
    assert issubclass(MathMarkdown, math_markdown.ui.markdown)
    assert "_handle_content_change" in MathMarkdown.__dict__, "원문(content)은 그대로, 그리는 HTML 만 바꿉니다"


@pytest.mark.parametrize("rel, needle", [
    ("app/ui/components/chat_feed.py", "md = MathMarkdown(content)"),
    ("app/ui/components/artifact_viewer.py", "MathMarkdown(content)"),
    ("app/ui/components/roster.py", 'self.ledger_view = MathMarkdown("")'),
    ("app/trial/pages/session.py", "MathMarkdown(result.final, extras=MARKDOWN_EXTRAS)"),
])
def test_cards_conclusions_and_the_ledger_render_math(rel, needle):
    assert needle in io.open(ROOT / rel, encoding="utf-8").read()


def test_math_has_its_own_font_and_wide_formulas_scroll():
    from app.ui.theme import CUSTOM_CSS

    assert ".nicegui-markdown math" in CUSTOM_CSS and "Cambria Math" in CUSTOM_CSS
    assert 'math[display="block"]' in CUSTOM_CSS and "overflow-x: auto" in CUSTOM_CSS


def test_the_converter_is_a_listed_dependency():
    assert "latex2mathml" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
