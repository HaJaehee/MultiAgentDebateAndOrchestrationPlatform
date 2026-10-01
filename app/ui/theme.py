FAVICON_SVG = (
    'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">'
    '<path fill="%236366f1" d="M16 3C8.82 3 3 8.148 3 14.5c0 3.23 1.487 6.155 3.924 8.273L5 28.5l6.398-1.828c1.442.538 3.037.828 4.602.828 7.18 0 13-5.148 13-11.5S23.18 3 16 3z"/>'
    '<circle cx="10" cy="14.5" r="1.8" fill="%23ffffff"/>'
    '<circle cx="16" cy="14.5" r="1.8" fill="%23ffffff"/>'
    '<circle cx="22" cy="14.5" r="1.8" fill="%23ffffff"/>'
    '</svg>'
)

CUSTOM_CSS = """
/* Global Styling */
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}

/* Chat timeline container styling */
.debate-timeline {
    scroll-behavior: smooth;
}

/* Tool execution accordion styling */
.mcp-tool-accordion {
    border: 1px solid rgba(0, 150, 136, 0.3) !important;
    border-radius: 8px !important;
    background-color: rgba(0, 150, 136, 0.05) !important;
    margin-top: 6px;
    margin-bottom: 6px;
}

.mcp-tool-badge {
    font-family: monospace;
    font-size: 0.82rem;
}

/* Agent Card Active State */
.agent-card-active {
    border: 2px solid #1976d2 !important;
    box-shadow: 0 4px 12px rgba(25, 118, 210, 0.25) !important;
}

.agent-card-inactive {
    opacity: 0.55;
    filter: grayscale(40%);
}

/* Artifact tab panel height */
.artifact-content-box {
    max-height: 650px;
    overflow-y: auto;
}

/* --- 마크다운 제목 크기 ------------------------------------------------
   NiceGUI 에는 Tailwind typography(prose) 가 실려 있지 않아, 마크다운 제목이
   브라우저 기본값으로 나옵니다 (본문 14px 인 카드 안에서 h1 32px, h3 30px).
   제목 한 줄이 카드를 다 차지하므로 본문에 비례하는 크기로 다시 잡습니다.
   em 단위라 채팅 카드(14px)와 산출물 뷰어(12px) 양쪽에서 함께 줄어듭니다. */
.nicegui-markdown h1,
.nicegui-markdown h2,
.nicegui-markdown h3,
.nicegui-markdown h4,
.nicegui-markdown h5,
.nicegui-markdown h6 {
    font-weight: 700;
    line-height: 1.35;
    margin: 0.9em 0 0.4em;
    color: #e2e8f0;
}
.nicegui-markdown h1 { font-size: 1.35em; }
.nicegui-markdown h2 { font-size: 1.2em; }
.nicegui-markdown h3 { font-size: 1.08em; }
.nicegui-markdown h4 { font-size: 1em; }
.nicegui-markdown h5,
.nicegui-markdown h6 { font-size: 0.95em; color: #cbd5e1; }

/* 첫 줄이 제목이면 위 여백이 카드 안에서 떠 보입니다. */
.nicegui-markdown > :first-child { margin-top: 0; }

/* 크기를 줄인 만큼 구분은 밑줄이 대신합니다. */
.nicegui-markdown h1,
.nicegui-markdown h2 {
    border-bottom: 1px solid rgba(148, 163, 184, 0.22);
    padding-bottom: 0.22em;
}

.nicegui-markdown p { margin: 0.5em 0; }
.nicegui-markdown ul,
.nicegui-markdown ol { margin: 0.5em 0; padding-left: 1.35em; }
.nicegui-markdown li { margin: 0.2em 0; }

/* --- Mermaid 다이어그램 --------------------------------------------------
   Mermaid 는 밝은 배경을 전제로 그립니다. 화살표와 글자가 검은색이라 어두운
   카드 위에 그대로 올리면 선이 배경에 묻혀 보이지 않습니다. 주변은 어두운
   테마 그대로 두고 다이어그램만 밝은 판 위에 올립니다. */
.mado-mermaid,
.nicegui-mermaid {
    background: #f8fafc;
    color: #0f172a;
    border: 1px solid #cbd5e1;
    border-radius: 10px;
    padding: 14px;
    overflow-x: auto;
}
.mado-mermaid svg,
.nicegui-mermaid svg {
    max-width: 100%;
    height: auto;
}

/* --- 입력창의 @언급 창 (mention_input.py) -----------------------------------
   입력창 바로 위에 뜹니다. 위치는 스크립트가 입력창 좌표로 정합니다(fixed). */
.mado-mention-popup {
    position: fixed;
    z-index: 6000;
    max-height: 300px;
    overflow-y: auto;
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 10px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    padding: 4px;
    font-size: 12px;
    color: #e2e8f0;
}
.mado-mention-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 8px;
    border-radius: 6px;
    cursor: pointer;
    min-width: 0;
}
.mado-mention-item.active,
.mado-mention-item:hover {
    background: #312e81;
}
.mado-mention-icon {
    font-size: 16px;
    flex-shrink: 0;
    color: #94a3b8;
}
.mado-mention-icon.mado-mention-agent { color: #a5b4fc; }
.mado-mention-icon.mado-mention-dir { color: #fbbf24; }
.mado-mention-icon.mado-mention-skill { color: #6ee7b7; }
.mado-mention-label {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.mado-mention-detail {
    flex-shrink: 0;
    color: #64748b;
    font-size: 11px;
    max-width: 40%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.mado-mention-empty {
    padding: 6px 8px;
    color: #64748b;
}

/* --- 수식 (math_markdown.py: LaTeX → MathML) ---------------------------------
   글꼴은 윈도우의 Cambria Math 를 먼저 씁니다. 브라우저 기본 수학 글꼴보다 기호·분수 선이
   또렷합니다. 본문보다 조금 키워 첨자가 묻히지 않게 하고, 넓은 블록 수식은 카드 안에서 가로로
   스크롤합니다 (카드 밖으로 넘치면 옆 패널을 가립니다). */
.nicegui-markdown math {
    font-family: "Cambria Math", "STIX Two Math", "Latin Modern Math", "Noto Sans Math", math;
    font-size: 1.1em;
    color: inherit;
}
.nicegui-markdown math[display="block"] {
    display: block math;
    margin: 0.5em 0;
    max-width: 100%;
    overflow-x: auto;
    overflow-y: hidden;
}
/* 표(cases·행렬): 브라우저는 MathML 의 columnalign·columnspacing 속성을 따르지 않습니다. 칸 사이를 띄우고
   (`1  x ≥ 0` 이 `1x ≥ 0` 으로 붙지 않게) 변환기가 적어 둔 정렬을 CSS 로 옮깁니다. */
.nicegui-markdown math mtd {
    padding: 0.1em 0.45em;
}
.nicegui-markdown math mtd:first-child {
    padding-left: 0;
}
.nicegui-markdown math mtd[columnalign="left"] {
    text-align: left;
}
.nicegui-markdown math mtd[columnalign="right"] {
    text-align: right;
}
.mado-math-fallback {
    font-family: "Cambria Math", "STIX Two Math", serif;
}
div.mado-math-fallback {
    margin: 0.5em 0;
    text-align: center;
}

/* 이미지로 바꾼 다이어그램 (mermaid_export.py 의 MERMAID_IMAGE_JS).
   원본 SVG 는 복사·다운로드를 위해 남기되 숨깁니다 — 숨긴 요소는 배치도 칠하기도 하지 않습니다. */
.mado-mermaid > svg.mado-mermaid-source {
    display: none !important;
}
.mado-mermaid > img.mado-mermaid-image {
    display: block;
    max-width: 100%;
    height: auto;
    margin: 0 auto;
}

/* --- 긴 보고서 산출물은 문단 단위로 건너뛰기 -------------------------------
   피드 카드에 쓴 것과 같은 방법을 보고서의 문단·제목·목록·코드 블록 하나하나에 겁니다.
   카드와 달리 보고서는 요소 하나에 글 전체가 들어 있어, 요소 단위로는 건너뛸 것이
   없었습니다. 폭이 바뀌면 보고서 전체의 줄바꿈을 다시 계산했습니다. 이제는 화면 근처
   문단만 다시 배치합니다. 코드·JSON 산출물은 `<pre>` 하나라 나눌 단위가 없어 해당이
   없습니다. `auto 3em` — 아직 그리지 않은 문단은 높이를 3em 으로 치고, 그린 뒤에는 실제
   높이를 기억합니다.

   **높이만** 추정합니다. `contain-intrinsic-size: auto 3em` 은 폭까지 3em 으로 치는데,
   보고서를 담은 NiceGUI 컬럼은 `align-items: flex-start` 라 내용 폭에 맞춰 줄어듭니다.
   그래서 건너뛴 문단들의 폭(3em)을 따라 보고서 전체가 52px 로 쪼그라들었습니다(실측,
   원래 539px). 화면에서는 스크롤할 때마다 그려진 문단에 따라 폭이 들쭉날쭉했을 것입니다.
   보고서와 그 마크다운이 폭을 **채우도록** 명시하고, 추정은 높이에만 겁니다.

   코드 블록은 자기 상자 안에서 가로로 스크롤하게 합니다. 건너뛰기를 켠 블록은 넘치는
   부분을 자기 상자에서 잘라 버리므로(`contain: paint`), `<pre>` 가 스스로 스크롤하지
   않으면 긴 줄의 뒷부분에 닿을 수 없습니다(실측: 600자 줄이 52px 상자에서 잘림). */
.artifact-report,
.artifact-report .nicegui-markdown {
    width: 100%;
    align-self: stretch;
}
.artifact-report .nicegui-markdown > * {
    content-visibility: auto;
    contain-intrinsic-block-size: auto 3em;
}
.artifact-report .nicegui-markdown pre {
    max-width: 100%;
    overflow-x: auto;
}

/* --- 스플리터를 끄는 동안 창 내용 고정 -----------------------------------------
   quiet_splitter.py 의 SPLITTER_FREEZE_JS 가 잡는 순간 창 안 요소의 폭을 묶습니다. 묶은
   내용이 창보다 넓어지면 스크롤 막대가 생겼다 사라지며 다시 배치하므로, 끄는 동안에는
   창 밖을 잘라 보이지 않게 합니다. */
.mado-splitter-frozen > .q-splitter__panel {
    overflow: hidden !important;
}

/* --- 세션 목록의 가로 폭 -------------------------------------------------
   Quasar 스크롤 영역의 내용 상자(.q-scrollarea__content)는 width:auto 라, 그 안의
   `w-full` 이 "보이는 너비" 가 아니라 "내용 너비" 가 됩니다. 그래서 세션 이름이
   길면 카드가 서랍보다 넓게 그려지고, 오른쪽 끝(이름 뒷부분과 수정·저장·삭제
   버튼)이 서랍 밖으로 밀려나 잘렸습니다. 보이는 너비에 맞춰 고정합니다. */
.session-list .q-scrollarea__content {
    width: 100%;
    max-width: 100%;
}

/* --- 토론 피드의 가로 폭 -------------------------------------------------
   세션 목록과 같은 원인입니다. 피드도 Quasar 스크롤 영역 안에 있어서, 내용 상자
   (.q-scrollarea__content)가 "보이는 너비" 가 아니라 "내용 너비" 로 늘어납니다.
   긴 코드 줄·넓은 표·띄어쓰기 없는 긴 문자열이 하나만 있어도 카드 전체가 그만큼
   넓어지고, 오른쪽에 붙은 복사·펼치기 버튼이 가로 스크롤 끝으로 밀려났습니다.
   가장 흔한 범인은 도구 아코디언이었습니다 — write_file 의 인자는 파일 내용
   전체가 한 줄짜리 JSON 문자열이라 수만 자 너비가 됩니다.

   버튼만 붙잡아 두는(sticky) 방법은 택하지 않았습니다. 카드가 화면보다 넓은 한
   본문을 읽으려면 여전히 옆으로 스크롤해야 하기 때문입니다. 대신 카드는 보이는
   너비에 맞추고, **넓은 내용은 자기 상자 안에서만** 가로로 스크롤하게 합니다. */
.debate-feed .q-scrollarea__content {
    width: 100%;
    max-width: 100%;
}

/* 화면 밖 발언 카드는 배치하지 않습니다.
   세션 목록을 접거나 산출물 스플릿을 움직여 피드 폭이 바뀌면, 브라우저는 화면 밖 카드까지
   전부의 줄바꿈·코드·표를 다시 배치했습니다. 접힌 카드도 `max-height` 로 세 줄만 보일 뿐
   숨은 내용은 그대로 배치됩니다. 카드 150장이면 폭이 한 번 바뀔 때 45~95ms — 서랍
   애니메이션과 스플리터 드래그가 매 프레임 이 값을 내며 버벅였습니다.
   `content-visibility: auto` 는 화면 근처 카드만 배치합니다. 같은 150장에서 1~6ms 이고,
   카드가 늘어도 거의 늘지 않습니다. 화면 밖 카드의 글은 DOM 에 그대로 있어 복사와 찾기
   (Ctrl+F)는 달라지지 않습니다.
   `contain-intrinsic-size: auto 220px` — 아직 한 번도 그리지 않은 카드는 220px 로 치고,
   한 번 그린 카드는 실제 높이를 기억합니다. 그래야 스크롤 막대가 덜 튑니다. */
.debate-timeline > .q-card {
    content-visibility: auto;
    contain-intrinsic-size: auto 220px;
}

/* flex 항목은 기본값(min-width:auto) 때문에 내용보다 좁아지지 못하고 부모를 뚫고
   나갑니다. 카드부터 본문·코드 상자까지 줄어들 수 있게 풀어 줍니다. */
.debate-timeline,
.debate-timeline .q-card,
.debate-timeline .nicegui-column,
.debate-timeline .nicegui-row,
.debate-timeline .q-expansion-item,
.debate-timeline .nicegui-markdown,
.debate-timeline .nicegui-code {
    min-width: 0;
    max-width: 100%;
}

/* 긴 코드 줄과 넓은 표는 그 상자 안에서만 가로로 스크롤합니다. 줄을 강제로
   접으면 코드의 들여쓰기와 표의 열이 무너집니다. */
.debate-timeline .nicegui-markdown pre,
.debate-timeline .nicegui-code pre {
    max-width: 100%;
    overflow-x: auto;
    overflow-wrap: normal;
}
.debate-timeline .nicegui-markdown table {
    display: block;
    max-width: 100%;
    overflow-x: auto;
}
.debate-timeline .nicegui-markdown img {
    max-width: 100%;
    height: auto;
}

/* 띄어쓰기 없는 긴 문자열(URL, 파일 경로, 인라인 코드)은 어디서든 줄바꿈합니다.
   이것들은 가로 스크롤로 보는 것보다 접어서 보는 편이 읽힙니다. */
.debate-timeline .nicegui-markdown p,
.debate-timeline .nicegui-markdown li,
.debate-timeline .nicegui-markdown :not(pre) > code,
.debate-timeline .mcp-tool-output {
    overflow-wrap: anywhere;
}

/* --- 겉모습 편집기의 미리보기 아바타 -------------------------------------
   테두리(border-2)를 단 q-avatar 는 아이콘이 원의 오른쪽 아래로 2px 치우쳤습니다.
   Quasar 는 안쪽 상자(.q-avatar__content)의 크기를 `inherit` 로 정해 바깥 크기
   (48px)를 그대로 물려받게 하는데, Tailwind 가 모든 요소를 border-box 로 두므로
   테두리 안쪽에는 44px 만 남습니다. 48px 상자가 테두리 안쪽의 왼쪽 위에서 시작해
   오른쪽 아래로 넘치고, 그 안에서 가운데 정렬된 아이콘도 함께 밀려났습니다.
   테두리를 없애거나 크기를 바꾸지 않고, 안쪽 상자를 테두리 안쪽에 맞춥니다. */
.appearance-preview .q-avatar__content {
    width: 100%;
    height: 100%;
}

/* --- 에이전트 카드 드래그 -------------------------------------------------
   순서를 바꾸는 동안 무엇을 집었고 어디에 놓이는지 보여야 합니다. 이것이 없으면
   커서를 어디에 두어야 앞이고 어디가 뒤인지 알 방법이 없어, 놓아 보고 결과로
   짐작하게 됩니다. */
.agent-dragging {
    opacity: 0.4;
    cursor: grabbing !important;
}

/* 놓일 자리를 카드 모서리의 굵은 선으로 표시합니다. 카드가 가로로 늘어서므로
   왼쪽 선은 "이 카드 앞", 오른쪽 선은 "이 카드 뒤" 입니다. `box-shadow` 는
   레이아웃을 밀지 않아, 표시가 뜰 때 카드들이 흔들리지 않습니다. */
.agent-drop-before {
    box-shadow: inset 4px 0 0 0 #818cf8;
}
.agent-drop-after {
    box-shadow: inset -4px 0 0 0 #818cf8;
}

/* --- 발언 카드 접기 -------------------------------------------------------
   발언이 끝나면 본문을 세 줄만 남기고 접습니다. 라운드가 몇 번 돌면 카드 하나가
   화면을 다 차지해서, 토론의 흐름을 보려면 계속 스크롤해야 했습니다.

   `max-height` 로 자릅니다. `-webkit-line-clamp` 는 컨테이너를 `-webkit-box` 로
   바꿔야 하는데, 그러면 문단·목록·코드블록이 섞인 마크다운의 블록 배치가 깨집니다.

   잘린 자리는 `mask-image` 로 흐립니다. 배경 그라디언트를 덮는 방식은 카드마다
   배경색이 달라(발언자별 색) 색을 맞춰야 하지만, 마스크는 내용 자체를 투명하게
   만들어 어떤 배경 위에서도 맞습니다. */
.chat-body-clamped {
    max-height: 4.8em;                      /* 본문 줄높이 약 1.6em x 3줄 */
    overflow: hidden;
    -webkit-mask-image: linear-gradient(to bottom, #000 62%, transparent 100%);
    mask-image: linear-gradient(to bottom, #000 62%, transparent 100%);
}

/* --- 생성 중 표시 ---------------------------------------------------------
   LLM 이 첫 글자를 내놓기까지, 그리고 도구가 도는 동안 화면에는 아무 일도
   일어나지 않습니다. 작은 회색 점 세 개와 흐린 글씨만으로는 "멈춘 화면" 과
   구별되지 않아, 사람이 새로고침을 누르게 됩니다 (그러면 붙어 있던 구독만
   끊기고 토론은 그대로 돕니다).

   그래서 세 가지를 함께 씁니다: 쓸리는 진행 막대(무언가 돌고 있다), 튀는 점
   (지금 이 순간에도 움직인다), 경과 시간(얼마나 기다렸는지). 시간이 가장
   중요합니다 — 초가 올라가는 것을 보면 멈춘 것이 아님을 의심할 여지가 없습니다. */

.feed-progress {
    position: relative;
    height: 3px;
    overflow: hidden;
    border-radius: 3px;
    background: rgba(99, 102, 241, 0.15);
}
.feed-progress::after {
    content: "";
    position: absolute;
    top: 0;
    bottom: 0;
    width: 35%;
    background: linear-gradient(90deg, transparent, #818cf8, transparent);
    animation: mado-sweep 1.3s linear infinite;
}
@keyframes mado-sweep {
    0%   { transform: translateX(-120%); }
    100% { transform: translateX(400%); }
}

/* 튀는 점 세 개. `ui.spinner("dots")` 보다 크고 대비가 높습니다. */
.live-dots i {
    display: inline-block;
    width: 7px;
    height: 7px;
    margin-right: 4px;
    border-radius: 50%;
    background: #818cf8;
    animation: mado-dot 1.2s ease-in-out infinite both;
}
.live-dots i:nth-child(2) { animation-delay: 0.16s; }
.live-dots i:nth-child(3) { animation-delay: 0.32s; }
@keyframes mado-dot {
    0%, 80%, 100% { opacity: 0.25; transform: translateY(0); }
    40%           { opacity: 1;    transform: translateY(-4px); }
}

/* 토론이 도는 동안 상태 막대 자체가 살아 있어야 합니다. 테두리 빛이 천천히
   번지므로, 눈에 띄되 글을 읽는 데 방해가 되지는 않습니다. */
.feed-status-live {
    border-color: rgba(129, 140, 248, 0.7) !important;
    animation: mado-pulse 2.4s ease-in-out infinite;
}
@keyframes mado-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(99, 102, 241, 0.35); }
    50%      { box-shadow: 0 0 0 5px rgba(99, 102, 241, 0); }
}

/* 타임라인 맨 아래에 붙는 생성 중 줄. 사람이 보고 있는 곳은 상태 막대가 아니라
   대화가 흐르는 이 자리입니다. */
.live-strip {
    border: 1px dashed rgba(129, 140, 248, 0.55);
    background: rgba(99, 102, 241, 0.08);
}

/* 애니메이션을 줄이도록 설정한 환경에서는 움직임을 멈춥니다. 상태는 글자로도
   전해지므로 (경과 시간·문구) 정보가 사라지지는 않습니다. */
@media (prefers-reduced-motion: reduce) {
    .feed-progress::after,
    .live-dots i,
    .feed-status-live {
        animation: none;
    }
}

/* Custom Scrollbars */
::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}
::-webkit-scrollbar-track {
    background: transparent;
}
::-webkit-scrollbar-thumb {
    background: rgba(120, 120, 120, 0.4);
    border-radius: 3px;
}
::-webkit-scrollbar-thumb:hover {
    background: rgba(120, 120, 120, 0.7);
}
"""
