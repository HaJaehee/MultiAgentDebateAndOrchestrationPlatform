"""`docs/user_manual/`의 마크다운 문서를 정적 HTML 사이트로 렌더링합니다.

    python docs/render_user_manual.py

    docs/user_manual/       (입력, 마크다운 + 폴더 구조)
      └──> docs/user_manual_html/   (출력, 동일한 디렉터리 구조 + 사이드바 + index.html)

**표준 라이브러리만 사용합니다.** 이 프로젝트는 폐쇄망 배포를 전제로 하므로, 문서를
열람하기 위해 새로운 외부 의존성을 추가하거나 CDN을 호출하지 않습니다. 출력 결과물 또한
독립적(Self-contained)입니다 — CSS가 인라인으로 포함되어 외부 네트워크 요청이 전혀
없으므로, 디렉터리째 복사하여 로컬 브라우저로 열어도 정상 동작합니다.

지원하는 마크다운 문법은 이 사용자 매뉴얼에서 실제로 사용하는 핵심 기능 위주입니다:
ATX 제목, 코드 블록(Fenced code block), GFM 표, 목록(중첩 목록 포함), 인용문,
구분선, 그리고 인라인의 `코드`/**굵게**/*기울임*/[링크](url). 문서를 작성하고 열람하는
환경이 단일 저장소 내에 완결되어 있으므로 무거운 범용 마크다운 파서 라이브러리가
필요하지 않습니다.

옵션:
    --src DIR     입력 폴더 (기본: docs/user_manual)
    --out DIR     출력 폴더 (기본: docs/user_manual_html)
    --clean       출력 폴더를 먼저 비웁니다
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 콘솔/파이프 인코딩이 UTF-8이 아닌 환경(Windows 기본 cp949 등)에서도 로그 출력 중
# 예외가 발생하여 프로세스가 비정상 종료되지 않도록 안전하게 설정합니다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


DOCS_DIR = Path(__file__).resolve().parent
DEFAULT_SRC = DOCS_DIR / "user_manual"
DEFAULT_OUT = DOCS_DIR / "user_manual_html"

SITE_TITLE = "MADO 사용 설명서"


# ---------------------------------------------------------------------------
# 인라인 (한 줄 안의) 마크다운
# ---------------------------------------------------------------------------

# 코드 스팬은 가장 먼저 떼어 냅니다. 그 안의 `**` 나 `[` 는 문법이 아니라 글자입니다.
_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_PLACEHOLDER = "\x00{}\x00"


# 현재 렌더링 중인 문서 정보. `rewrite_link()`에서 대상 경로가 이 매뉴얼 내부인지
# 외부인지 판별할 때 참조합니다. 빌드는 단일 스레드로 순차 실행됩니다.
_CURRENT: Dict[str, Optional[Path]] = {"src_page": None, "src_root": None}


def rewrite_link(href: str) -> str:
    """매뉴얼 내부 문서의 `.md` 링크만 대응하는 `.html` 링크로 변환합니다.

    저장소의 다른 파일(`../../README.md`, `../../wiki/...`)을 가리키는 링크는
    HTML로 변환되지 않으므로 그대로 둡니다. 확장자만 바꾸면 존재하지 않는 파일을 가리키게 됩니다.
    앵커(`#섹션-제목`)와 외부 URL도 변경하지 않습니다.
    """
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href) or href.startswith("//"):
        return href  # http:, mailto:, file: ...
    path, sep, anchor = href.partition("#")
    if not path.endswith(".md"):
        return href

    src_page, src_root = _CURRENT["src_page"], _CURRENT["src_root"]
    if src_page is not None and src_root is not None:
        target = (src_page.parent / path).resolve()
        try:
            target.relative_to(src_root.resolve())
        except ValueError:
            return href      # 매뉴얼 외부 문서 — 원본 경로를 그대로 가리킵니다
    return path[: -len(".md")] + ".html" + sep + anchor


def render_inline(text: str) -> str:
    spans: List[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(1))
        return _PLACEHOLDER.format(len(spans) - 1)

    text = _CODE_SPAN.sub(stash, text)
    text = html.escape(text, quote=False)

    def link(match: re.Match) -> str:
        label, href = match.group(1), rewrite_link(match.group(2))
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    for i, code in enumerate(spans):
        text = text.replace(
            _PLACEHOLDER.format(i), f"<code>{html.escape(code, quote=False)}</code>"
        )
    return text


# ---------------------------------------------------------------------------
# 제목 → 앵커
# ---------------------------------------------------------------------------

_ANCHOR_STRIP = re.compile(r"[`*\[\]()]")
_ANCHOR_SEP = re.compile(r"[^0-9a-zA-Z가-힣]+")


def slugify(text: str, used: Dict[str, int]) -> str:
    base = _ANCHOR_SEP.sub("-", _ANCHOR_STRIP.sub("", text)).strip("-").lower() or "section"
    count = used.get(base, 0)
    used[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


# ---------------------------------------------------------------------------
# 블록 (여러 줄) 마크다운
# ---------------------------------------------------------------------------

@dataclass
class Heading:
    level: int
    text: str
    anchor: str


def _render_table(rows: List[str]) -> str:
    def cells(line: str) -> List[str]:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    header = cells(rows[0])
    aligns: List[str] = []
    for spec in cells(rows[1]):
        left, right = spec.startswith(":"), spec.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left")

    out = ["<div class=\"table-scroll\"><table>", "<thead><tr>"]
    for i, cell in enumerate(header):
        align = aligns[i] if i < len(aligns) else "left"
        out.append(f'<th style="text-align:{align}">{render_inline(cell)}</th>')
    out.append("</tr></thead><tbody>")
    for row in rows[2:]:
        out.append("<tr>")
        for i, cell in enumerate(cells(row)):
            align = aligns[i] if i < len(aligns) else "left"
            out.append(f'<td style="text-align:{align}">{render_inline(cell)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def _render_list(lines: List[str]) -> str:
    """중첩 목록. 들여쓰기 폭으로 깊이를 정합니다 (2칸 또는 4칸 모두 받습니다)."""
    root: List[str] = []
    # (indent, tag, buffer) 스택
    stack: List[Tuple[int, str, List[str]]] = []

    def close_to(indent: int) -> None:
        while stack and stack[-1][0] > indent:
            _, tag, buf = stack.pop()
            rendered = f"<{tag}>{''.join(buf)}</{tag}>"
            (stack[-1][2] if stack else root).append(rendered)

    for line in lines:
        match = _LIST_ITEM.match(line)
        if not match:
            # 항목에 이어지는 줄 (들여쓴 본문)
            target = stack[-1][2] if stack else root
            if target and target[-1].endswith("</li>"):
                target[-1] = target[-1][: -len("</li>")] + " " + render_inline(line.strip()) + "</li>"
            continue
        indent = len(match.group(1))
        tag = "ul" if match.group(2) in "-*+" else "ol"
        content = render_inline(match.group(3))

        close_to(indent)
        if not stack or stack[-1][0] < indent:
            stack.append((indent, tag, [f"<li>{content}</li>"]))
        else:
            stack[-1][2].append(f"<li>{content}</li>")

    close_to(-1)
    return "".join(root)


def render_markdown(text: str) -> Tuple[str, List[Heading], str]:
    """마크다운 → (본문 HTML, 제목 목록, 문서 제목)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    headings: List[Heading] = []
    used_anchors: Dict[str, int] = {}
    doc_title = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # --- 빈 줄 ---
        if not stripped:
            i += 1
            continue

        # --- 코드 블록 (Fenced Code Block) ---
        if stripped.startswith("```"):
            language = stripped[3:].strip()
            body: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # 닫는 백틱 코드 블록
            code = html.escape("\n".join(body), quote=False)
            label = f'<span class="code-lang">{html.escape(language)}</span>' if language else ""
            css = f' class="language-{html.escape(language, quote=True)}"' if language else ""
            out.append(f'<div class="code-block">{label}<pre><code{css}>{code}</code></pre></div>')
            continue

        # --- 제목 ---
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            raw = heading.group(2).strip()
            anchor = slugify(raw, used_anchors)
            headings.append(Heading(level, raw, anchor))
            if level == 1 and not doc_title:
                doc_title = _ANCHOR_STRIP.sub("", raw)
            out.append(f'<h{level} id="{anchor}">{render_inline(raw)}'
                       f'<a class="anchor" href="#{anchor}" aria-hidden="true">#</a></h{level}>')
            i += 1
            continue

        # --- 수평선 ---
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            out.append("<hr>")
            i += 1
            continue

        # --- 표 ---
        if "|" in stripped and i + 1 < len(lines) and re.fullmatch(
            r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", lines[i + 1].strip()
        ):
            rows = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(_render_table(rows))
            continue

        # --- 인용 ---
        if stripped.startswith(">"):
            quoted: List[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner, _, _ = render_markdown("\n".join(quoted))
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # --- 목록 ---
        if _LIST_ITEM.match(line):
            items: List[str] = []
            while i < len(lines):
                if _LIST_ITEM.match(lines[i]):
                    items.append(lines[i])
                elif lines[i].strip() and lines[i].startswith((" ", "\t")):
                    items.append(lines[i])   # 항목에 이어지는 줄
                else:
                    break
                i += 1
            out.append(_render_list(items))
            continue

        # --- 문단 ---
        para: List[str] = []
        while i < len(lines) and lines[i].strip():
            nxt = lines[i].strip()
            if nxt.startswith(("```", ">", "#")) or _LIST_ITEM.match(lines[i]):
                break
            para.append(nxt)
            i += 1
        if para:
            out.append(f"<p>{render_inline(' '.join(para))}</p>")
        else:
            i += 1

    return "\n".join(out), headings, doc_title


# ---------------------------------------------------------------------------
# 문서 트리
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """사이드바 탐색 트리의 노드입니다. 디렉터리이거나 개별 문서입니다."""

    name: str                       # 화면에 표시되는 제목
    href: Optional[str] = None      # 출력 디렉터리 기준 상대 경로 (디렉터리 자체는 None일 수 있음)
    children: List["Node"] = field(default_factory=list)
    is_dir: bool = False
    # 정렬은 파일명 기준으로 수행합니다. 문서 제목 기준으로 정렬하면
    # `01-`, `02-` 등의 접두사가 지정한 읽기 순서가 가나다순에 의해 왜곡될 수 있습니다.
    sort_key: str = ""


def _display_name(md_path: Path, fallback: str) -> str:
    """문서의 첫 번째 `# 제목`을 표시 이름으로 사용합니다. 없을 경우 파일명 기본값."""
    try:
        for line in md_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^#\s+(.*)$", line.strip())
            if match:
                return _ANCHOR_STRIP.sub("", match.group(1)).strip()
    except OSError:
        pass
    return fallback


def _sort_key(path: Path) -> Tuple[int, str]:
    # 폴더의 README 가 항상 맨 앞에 옵니다 (그 폴더의 표지이므로).
    return (0 if path.name.lower() == "readme.md" else 1, path.name.lower())


def build_tree(src: Path, root: Path) -> List[Node]:
    nodes: List[Node] = []
    for path in sorted(src.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            children = build_tree(path, root)
            if not children:
                continue
            index = next((c for c in children if c.name and c.href
                          and Path(c.href).stem == "README"), None)
            label = path.name
            readme = path / "README.md"
            if readme.is_file():
                label = _display_name(readme, path.name)
                children = [c for c in children if c is not index]
            nodes.append(Node(
                name=label,
                href=(index.href if index else None),
                children=children,
                is_dir=True,
                sort_key=path.name.lower(),
            ))
        elif path.suffix.lower() == ".md":
            rel = path.relative_to(root).with_suffix(".html")
            nodes.append(Node(
                name=_display_name(path, path.stem),
                href=rel.as_posix(),
                sort_key=path.name.lower(),
            ))
    nodes.sort(key=lambda n: (not n.is_dir, n.sort_key))
    return nodes


def _tree_html(nodes: List[Node], current: str, depth: int = 0) -> str:
    out = [f'<ul class="tree depth-{depth}">']
    for node in nodes:
        classes = ["node"]
        if node.is_dir:
            classes.append("dir")
        if node.href == current:
            classes.append("current")
        label = html.escape(node.name)
        if node.href:
            link = f'<a href="{html.escape(_relative(current, node.href), quote=True)}">{label}</a>'
        else:
            link = f"<span>{label}</span>"
        out.append(f'<li class="{" ".join(classes)}">{link}')
        if node.children:
            out.append(_tree_html(node.children, current, depth + 1))
        out.append("</li>")
    out.append("</ul>")
    return "".join(out)


def _relative(from_page: str, to_page: str) -> str:
    """출력 폴더 기준 두 경로 사이의 상대 링크."""
    from_dir = Path(from_page).parent
    try:
        import os.path
        return Path(os.path.relpath(to_page, from_dir if str(from_dir) != "." else ".")).as_posix()
    except ValueError:
        return to_page


# ---------------------------------------------------------------------------
# 페이지 조립
# ---------------------------------------------------------------------------

STYLE = """
:root {
  --bg: #ffffff; --fg: #1f2933; --muted: #6b7785; --line: #e3e8ee;
  --accent: #1f6feb; --accent-soft: #eaf2ff;
  --code-bg: #f5f7fa; --code-fg: #24292f; --side-bg: #fafbfc; --quote-bg: #f7f9fb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1419; --fg: #d7dde4; --muted: #8b96a3; --line: #232b34;
    --accent: #6ea8ff; --accent-soft: #17222f;
    --code-bg: #161b22; --code-fg: #d7dde4; --side-bg: #12181f; --quote-bg: #141b23;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.75 -apple-system, "Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", Roboto, sans-serif;
}
.layout { display: flex; align-items: flex-start; max-width: 1400px; margin: 0 auto; }
aside {
  position: sticky; top: 0; flex: 0 0 288px; width: 288px; height: 100vh;
  overflow-y: auto; padding: 24px 18px 48px; background: var(--side-bg);
  border-right: 1px solid var(--line);
}
aside .site-title { font-weight: 700; font-size: 15px; margin: 0 0 4px; }
aside .site-title a { color: var(--fg); text-decoration: none; }
aside .site-sub { color: var(--muted); font-size: 12px; margin: 0 0 20px; }
ul.tree { list-style: none; margin: 0; padding: 0; }
ul.tree.depth-1, ul.tree.depth-2 { padding-left: 14px; border-left: 1px solid var(--line); margin-left: 5px; }
li.node { margin: 1px 0; }
li.node > a, li.node > span {
  display: block; padding: 4px 8px; border-radius: 6px;
  color: var(--fg); text-decoration: none; font-size: 13.5px;
}
li.node > a:hover { background: var(--accent-soft); }
li.dir > a, li.dir > span { font-weight: 600; }
li.current > a { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
main { flex: 1 1 auto; min-width: 0; padding: 40px 48px 96px; }
article { max-width: 860px; }
h1, h2, h3, h4 { line-height: 1.35; margin: 2em 0 .7em; }
h1 { font-size: 30px; margin-top: 0; padding-bottom: .35em; border-bottom: 1px solid var(--line); }
h2 { font-size: 22px; padding-bottom: .3em; border-bottom: 1px solid var(--line); }
h3 { font-size: 17px; }
h4 { font-size: 15px; color: var(--muted); }
a { color: var(--accent); }
a.anchor { margin-left: .4em; color: var(--line); text-decoration: none; font-weight: 400; }
h1:hover a.anchor, h2:hover a.anchor, h3:hover a.anchor { color: var(--muted); }
p { margin: 0 0 1em; }
ul, ol { margin: 0 0 1em; padding-left: 1.5em; }
li { margin: .25em 0; }
code {
  background: var(--code-bg); color: var(--code-fg); padding: .12em .38em;
  border-radius: 4px; font-size: .88em;
  font-family: "Cascadia Mono", Consolas, "D2Coding", monospace;
}
.code-block { position: relative; margin: 0 0 1.2em; }
.code-block pre {
  margin: 0; padding: 14px 16px; overflow-x: auto;
  background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
}
.code-block pre code { background: none; padding: 0; font-size: 13px; line-height: 1.6; }
.code-lang {
  position: absolute; top: 0; right: 12px; transform: translateY(-50%);
  background: var(--bg); color: var(--muted); font-size: 11px;
  padding: 0 6px; border: 1px solid var(--line); border-radius: 4px;
}
blockquote {
  margin: 0 0 1.2em; padding: 12px 16px; background: var(--quote-bg);
  border-left: 3px solid var(--accent); border-radius: 0 6px 6px 0;
}
blockquote > :last-child { margin-bottom: 0; }
.table-scroll { overflow-x: auto; margin: 0 0 1.2em; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { border: 1px solid var(--line); padding: 7px 11px; vertical-align: top; }
th { background: var(--code-bg); font-weight: 600; }
hr { border: 0; border-top: 1px solid var(--line); margin: 2em 0; }
.crumb { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.crumb a { color: var(--muted); }
.page-nav {
  display: flex; gap: 12px; justify-content: space-between;
  margin-top: 56px; padding-top: 20px; border-top: 1px solid var(--line); font-size: 14px;
}
.toc {
  border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px;
  margin: 0 0 28px; background: var(--side-bg); font-size: 13.5px;
}
.toc-title { font-weight: 600; font-size: 12px; color: var(--muted);
             text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }
.toc ul { list-style: none; margin: 0; padding: 0; }
.toc li.lv3 { padding-left: 14px; }
.toc a { text-decoration: none; }
.toc a:hover { text-decoration: underline; }
@media (max-width: 900px) {
  .layout { flex-direction: column; }
  aside { position: static; width: 100%; flex-basis: auto; height: auto;
          border-right: 0; border-bottom: 1px solid var(--line); }
  main { padding: 28px 20px 64px; }
}
"""

PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="layout">
<aside>
  <p class="site-title"><a href="{home}">{site_title}</a></p>
  <p class="site-sub">Multi-Agent Debate &amp; Orchestration Platform</p>
  {tree}
</aside>
<main>
  <article>
    {crumb}
    {toc}
    {body}
    {nav}
  </article>
</main>
</div>
</body>
</html>
"""


def _toc_html(headings: List[Heading]) -> str:
    items = [h for h in headings if h.level in (2, 3)]
    if len(items) < 2:
        return ""
    rows = "".join(
        f'<li class="lv{h.level}"><a href="#{h.anchor}">{render_inline(h.text)}</a></li>'
        for h in items
    )
    return f'<nav class="toc"><div class="toc-title">목차</div><ul>{rows}</ul></nav>'


def _crumb_html(rel_html: str, home: str) -> str:
    parts = Path(rel_html).parts
    if len(parts) <= 1:
        return ""
    trail = [f'<a href="{home}">{SITE_TITLE}</a>']
    for part in parts[:-1]:
        trail.append(html.escape(part))
    return f'<div class="crumb">{" / ".join(trail)}</div>'


def _flatten(nodes: List[Node]) -> List[Node]:
    flat: List[Node] = []
    for node in nodes:
        if node.href:
            flat.append(node)
        flat.extend(_flatten(node.children))
    return flat


def _nav_html(order: List[Node], current: str) -> str:
    index = next((i for i, n in enumerate(order) if n.href == current), None)
    if index is None:
        return ""
    left = right = ""
    if index > 0:
        prev = order[index - 1]
        left = (f'<a href="{html.escape(_relative(current, prev.href), quote=True)}">'
                f'← {html.escape(prev.name)}</a>')
    if index + 1 < len(order):
        nxt = order[index + 1]
        right = (f'<a href="{html.escape(_relative(current, nxt.href), quote=True)}">'
                 f'{html.escape(nxt.name)} →</a>')
    if not left and not right:
        return ""
    return f'<div class="page-nav"><span>{left}</span><span>{right}</span></div>'


def build(src: Path, out: Path, clean: bool = False) -> int:
    if not src.is_dir():
        sys.exit(f"입력 폴더가 없습니다: {src}")
    if clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    tree = build_tree(src, src)
    order = _flatten(tree)

    pages = sorted(src.rglob("*.md"))
    for md_path in pages:
        rel_html = md_path.relative_to(src).with_suffix(".html").as_posix()
        _CURRENT["src_page"], _CURRENT["src_root"] = md_path, src
        body, headings, doc_title = render_markdown(md_path.read_text(encoding="utf-8"))
        home = _relative(rel_html, "README.html")

        page = PAGE.format(
            title=html.escape(
                f"{doc_title} · {SITE_TITLE}"
                if doc_title and doc_title != SITE_TITLE
                else SITE_TITLE
            ),
            style=STYLE,
            site_title=html.escape(SITE_TITLE),
            home=html.escape(home, quote=True),
            tree=_tree_html(tree, rel_html),
            crumb=_crumb_html(rel_html, html.escape(home, quote=True)),
            toc=_toc_html(headings),
            body=body,
            nav=_nav_html(order, rel_html),
        )
        target = out / rel_html
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
        print(f"  {md_path.relative_to(src).as_posix()}  ->  {rel_html}")

    # 루트 README를 index.html로 복사합니다 (루트 URL 접속 시 표시되도록 지원).
    root_readme = out / "README.html"
    if root_readme.is_file():
        (out / "index.html").write_text(
            root_readme.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print("  README.html  ->  index.html")

    return len(pages)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="docs/user_manual 디렉터리의 마크다운 문서를 정적 HTML 사이트로 렌더링합니다."
    )
    parser.add_argument("--src", default=str(DEFAULT_SRC), help=f"입력 폴더 (기본: {DEFAULT_SRC})")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"출력 폴더 (기본: {DEFAULT_OUT})")
    parser.add_argument("--clean", action="store_true", help="출력 폴더를 먼저 비웁니다")
    args = parser.parse_args()

    src, out = Path(args.src).resolve(), Path(args.out).resolve()
    print(f"[렌더] {src}  ->  {out}")
    count = build(src, out, clean=args.clean)
    print(f"[완료] 문서 {count}개. 시작 파일: {(out / 'index.html')}")


if __name__ == "__main__":
    main()
