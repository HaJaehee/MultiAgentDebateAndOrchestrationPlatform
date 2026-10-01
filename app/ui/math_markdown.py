"""수식을 그리는 마크다운 — 발언 카드·최종 결론·결정 장부에서 LaTeX 를 MathML 로 보여 줍니다.

모델은 `$O(n \\times m)$`, `$A \\leftarrow B$`, `$$\\frac{1}{n}\\sum t_i$$`, `\\(p \\cdot q\\)` 처럼 LaTeX 로
씁니다. NiceGUI 의 `ui.markdown` 은 이것을 모르므로 글자 그대로 보였고, 오히려 마크다운이 `\\(` 를 `(` 로,
`x_i` 의 `_` 를 기울임으로 바꿔 망가뜨렸습니다.

여기서는 마크다운으로 바꾸기 **전에** 수식 구간을 자리표시자로 빼 두고, 바꾼 **뒤에** 그 자리에 MathML 을
넣습니다 (`latex2mathml`, 순수 파이썬). MathML 은 브라우저가 스크립트 없이 바로 그리고(Chrome·Edge 109+,
Firefox), NiceGUI 의 DOMPurify 도 지우지 않습니다. 폐쇄망에 들여갈 것은 휠 하나뿐입니다.

* **수식 구간** — `$$…$$`, `\\[…\\]` (블록), `\\(…\\)`, `$…$` (줄 안). 코드 블록과 인라인 코드 안은 보지
  않습니다. `$…$` 는 가격(`$5 에서 $10`)과 헷갈리지 않게 pandoc 규칙을 따릅니다 — 여는 `$` 뒤와 닫는 `$`
  앞에 공백이 없고, 닫는 `$` 바로 뒤가 숫자가 아니며, 안에 수식다운 것(`\\명령`, `^`, `_`, `=` 등)이
  있거나 변수 하나(`$x$`)일 때만입니다.
* **맨몸 명령** — `$` 없이 쓴 `A \\rightarrow B`, `2 \\times 3` 은 유니코드 기호(→, ×)로 바꿉니다. 경로
  (`C:\\to\\file`)를 건드리지 않도록 앞뒤 글자를 봅니다.
* **변환 실패·패키지 없음** — 수식은 유니코드 기호로 바꾼 글로 대신 보여 줍니다. 원문 복사·내보내기는
  언제나 원문 그대로입니다 (요소의 `content` 는 바꾸지 않습니다).

`latex2mathml` 은 `\\text{…}` 안의 글자를 이스케이프하지 않습니다. 화면의 DOMPurify 가 지우긴 하지만,
서버에서도 MathML 태그 밖의 `<`, `>` 를 이스케이프하고 링크·이벤트 속성을 걷어 냅니다.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from nicegui import ui
from nicegui.elements.markdown import prepare_content

logger = logging.getLogger(__name__)

try:  # 폐쇄망 반입본에 휠이 빠져 있어도 화면은 떠야 합니다 — 그때는 기호 치환만 합니다.
    import latex2mathml.converter as _latex2mathml
except ImportError:  # pragma: no cover - 설치 환경에 따라 다릅니다
    _latex2mathml = None


# ---------------------------------------------------------------------------
# LaTeX 명령 → 유니코드 (맨몸 명령과, 수식 변환이 안 될 때)
# ---------------------------------------------------------------------------

SYMBOLS: Dict[str, str] = {
    # 화살표
    "leftarrow": "←", "rightarrow": "→", "Leftarrow": "⇐", "Rightarrow": "⇒",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "longleftarrow": "⟵", "longrightarrow": "⟶",
    "Longleftarrow": "⟸", "Longrightarrow": "⟹", "longleftrightarrow": "⟷", "Longleftrightarrow": "⟺",
    "mapsto": "↦", "to": "→", "gets": "←", "uparrow": "↑", "downarrow": "↓", "Uparrow": "⇑",
    "Downarrow": "⇓", "implies": "⟹", "impliedby": "⟸", "iff": "⟺", "rightleftharpoons": "⇌",
    "hookrightarrow": "↪", "nearrow": "↗", "searrow": "↘",
    # 연산
    "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓", "ast": "∗", "circ": "∘",
    "bullet": "•", "oplus": "⊕", "otimes": "⊗", "star": "⋆",
    # 관계
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈",
    "equiv": "≡", "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫",
    "lt": "<", "gt": ">",
    # 집합·논리
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀",
    "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧", "lor": "∨",
    "vee": "∨", "therefore": "∴", "because": "∵", "setminus": "∖",
    # 그 밖
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮",
    "sqrt": "√", "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮", "ddots": "⋱",
    "degree": "°", "angle": "∠", "perp": "⊥", "parallel": "∥", "checkmark": "✓", "prime": "′",
    "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ",
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "langle": "⟨", "rangle": "⟩",
    # 그리스 문자
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

# 맨몸 명령: 앞이 글자·`\`·`/`·`:` 가 아니고(경로 `C:\to\x`, `work\times` 제외) 뒤가 글자·`\`·`/` 가 아닐 때.
_BARE = re.compile(r"(?<![A-Za-z\\/:_])\\([A-Za-z]+)(?![A-Za-z\\/])")

_SUPERSCRIPT = str.maketrans("0123456789+-=()niT", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵀ")
_SUBSCRIPT = str.maketrans("0123456789+-=()aeijkmnopstx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢⱼₖₘₙₒₚₛₜₓ")


def replace_bare_commands(text: str) -> str:
    """`A \\rightarrow B` → `A → B`. 표에 없는 명령은 그대로 둡니다."""
    return _BARE.sub(lambda m: SYMBOLS.get(m.group(1), m.group(0)), text)


def tex_to_unicode(tex: str) -> str:
    """MathML 로 바꾸지 못할 때 보여 줄 글. 기호·분수·첨자를 할 수 있는 만큼만 풉니다."""
    text = tex
    text = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname|textbf|textit|mbox)\{([^{}]*)\}", r"\1", text)
    for _ in range(3):  # 겹친 분수 몇 겹까지
        text = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    text = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)(?![A-Za-z])", "", text)
    text = re.sub(r"\\[,;:! ]", " ", text)
    text = re.sub(r"\\([A-Za-z]+)", lambda m: SYMBOLS.get(m.group(1), m.group(0)), text)

    def script(m: re.Match, table: dict, mark: str) -> str:
        body = m.group(1) if m.group(1) is not None else m.group(2)
        # 모든 글자가 첨자 글자로 바뀔 때만 바꿉니다. 하나라도 없으면(`x^{ab}`) 표기를 그대로 둡니다.
        if body and all(ord(ch) in table for ch in body):
            return body.translate(table)
        return f"{mark}{body}"

    text = re.sub(r"\^\{([^{}]*)\}|\^(\S)", lambda m: script(m, _SUPERSCRIPT, "^"), text)
    text = re.sub(r"_\{([^{}]*)\}|_(\S)", lambda m: script(m, _SUBSCRIPT, "_"), text)
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# MathML 변환과 정리
# ---------------------------------------------------------------------------

_MATHML_TAGS = (
    "math|mrow|mi|mo|mn|ms|mtext|mspace|msub|msup|msubsup|mfrac|msqrt|mroot|mover|munder|munderover|"
    "mtable|mtr|mtd|mstyle|mpadded|mphantom|menclose|merror|semantics|annotation|mmultiscripts|"
    "mprescripts|none|mlabeledtr|maligngroup|malignmark"
)
_TAG = re.compile(rf"(</?(?:{_MATHML_TAGS})\b[^<>]*>)")
_BAD_ATTR = re.compile(r"""\s+(?:on[a-z]+|href|xlink:href|src|style)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_ENTITY = re.compile(r"&(?:#x[0-9A-Fa-f]+|#\d+|[A-Za-z][A-Za-z0-9]*);")
# `\lVert x \rVert`, `\lvert x \rvert` 는 속성 없는 `<mo>‖</mo>`, `<mo>|</mo>` 로 나옵니다. 브라우저는 이것을
# 늘어나는 괄호로 보고 같은 줄의 가장 큰 것(√ 등)만큼 키웁니다. LaTeX 에서 이 기호들은 늘어나지 않습니다
# (`\left|` 처럼 늘이라고 한 것은 속성이 붙어 나오므로 건드리지 않습니다).
_BARE_BAR = re.compile(r"<mo>(&#x02016;|&#x0007C;)</mo>")


def _clean_mathml(markup: str) -> str:
    """MathML 태그는 남기고(위험한 속성은 걷고), 태그 밖의 `<`·`>`·`&` 는 이스케이프합니다."""
    out: List[str] = []
    for part in _TAG.split(markup):
        if _TAG.fullmatch(part):
            out.append(_BAD_ATTR.sub("", part))
            continue
        pieces = _ENTITY.split(part)
        entities = _ENTITY.findall(part)
        escaped = [html.escape(piece, quote=False) for piece in pieces]
        merged: List[str] = []
        for index, piece in enumerate(escaped):
            merged.append(piece)
            if index < len(entities):
                merged.append(entities[index])
        out.append("".join(merged))
    return _BARE_BAR.sub(r'<mo stretchy="false">\1</mo>', "".join(out))


@lru_cache(maxsize=4096)
def tex_to_mathml(tex: str, display: bool) -> Optional[str]:
    """LaTeX 하나를 MathML 로. 못 바꾸면 None. 같은 식은 기억합니다 (스트리밍 중 매번 다시 그리므로)."""
    if _latex2mathml is None:
        return None
    try:
        markup = _latex2mathml.convert(tex.strip(), display="block" if display else "inline")
    except Exception as exc:  # noqa: BLE001 - 한 식이 틀려도 발언 전체는 그려야 합니다
        logger.debug(f"Could not convert TeX to MathML ({type(exc).__name__}): {tex[:80]!r}")
        return None
    return _clean_mathml(markup)


def math_html(tex: str, display: bool) -> str:
    """수식 하나의 HTML. MathML 이 안 되면 유니코드로 푼 글."""
    markup = tex_to_mathml(tex, display)
    if markup is not None:
        return markup
    fallback = html.escape(tex_to_unicode(tex), quote=False)
    tag = "div" if display else "span"
    return f'<{tag} class="mado-math-fallback">{fallback}</{tag}>'


# ---------------------------------------------------------------------------
# 마크다운 안에서 수식 구간 찾기
# ---------------------------------------------------------------------------

# 코드: 울타리 블록(``` 또는 ~~~, 닫히지 않았으면 끝까지 — 스트리밍 중)과 인라인 코드.
_FENCE = re.compile(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*$|\Z)", re.S)
_INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)

_MATH = re.compile(
    r"(?<!\\)\$\$(?P<dd>.+?)(?<!\\)\$\$"
    r"|\\\[(?P<br>.+?)\\\]"
    r"|\\\((?P<pa>.+?)\\\)"
    r"|(?<![\\$0-9A-Za-z])\$(?=[^\s$])(?P<sd>[^$\n]{1,400}?)(?<=[^\s\\])\$(?![0-9$])",
    re.S,
)
# `$…$` 로 받을 만한 내용: 명령·첨자·연산·관계·괄호(`$O(n)$`, `$|x|$`)가 있거나 변수 하나.
# 가격은 여기가 아니라 구분자 규칙(닫는 `$` 앞 공백, 뒤 숫자)에서 걸러집니다. `/` 는 넣지 않습니다
# (`$HOME/$PATH` 같은 셸 변수).
_MATHY = re.compile(r"\\[A-Za-z]|[\^_=<>+\-*|()]")
_SINGLE_VARIABLE = re.compile(r"[A-Za-z](?:'|_?\d{1,2})?")
_NUMERIC = re.compile(r"[\d\s.,%]+")


def _code_spans(text: str) -> List[Tuple[int, int]]:
    spans = [m.span() for m in _FENCE.finditer(text)]
    for m in _INLINE_CODE.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return sorted(spans)


def _accept_math(body: str) -> bool:
    """`$…$`·`\\(…\\)`·`\\[…\\]` 의 내용이 수식다운가. 가격(`$5`)·각주(`\\[1\\]`)를 걸러 냅니다."""
    body = body.strip()
    if not body or _NUMERIC.fullmatch(body):
        return False
    return bool(_MATHY.search(body) or _SINGLE_VARIABLE.fullmatch(body))


def protect_math(text: str) -> Tuple[str, Dict[str, str]]:
    """수식 구간을 자리표시자로 바꾸고 (자리표시자 → HTML) 표를 돌려줍니다. 맨몸 명령도 여기서 바꿉니다."""
    nonce = hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:6]
    found: Dict[str, str] = {}
    out: List[str] = []
    cursor = 0

    def handle_prose(segment: str) -> str:
        def repl(m: re.Match) -> str:
            if m.group("dd") is not None:
                tex, display = m.group("dd"), True       # `$$…$$` 는 쓴 사람이 수식이라고 밝힌 것입니다
            else:
                tex = next(m.group(k) for k in ("br", "pa", "sd") if m.group(k) is not None)
                display = m.group("br") is not None
                if not _accept_math(tex):
                    return m.group(0)
            token = f"MADOMATH{nonce}X{len(found)}Z"
            found[token] = math_html(tex, display)
            return token

        return replace_bare_commands(_MATH.sub(repl, segment))

    for start, end in _code_spans(text):
        out.append(handle_prose(text[cursor:start]))
        out.append(text[start:end])
        cursor = end
    out.append(handle_prose(text[cursor:]))
    return "".join(out), found


@lru_cache(maxsize=1000)
def render_markdown(content: str, extras: str) -> str:
    """마크다운 → HTML, 수식은 MathML. NiceGUI 의 변환(`prepare_content`)을 그대로 쓰고 수식만 끼웁니다."""
    protected, found = protect_math(content)
    rendered = prepare_content(protected, extras)
    for token, markup in found.items():
        rendered = rendered.replace(token, markup)
    return rendered


class MathMarkdown(ui.markdown):
    """`ui.markdown` 과 같되 수식을 그립니다. `content` 는 원문 그대로라 복사·내보내기에는 영향이 없습니다."""

    def _handle_content_change(self, content: str) -> None:
        markup = render_markdown(content, " ".join(self.extras))
        if callable(self._sanitize):
            markup = self._sanitize(markup)
        if self._props.get("innerHTML") != markup:
            self._props["innerHTML"] = markup
