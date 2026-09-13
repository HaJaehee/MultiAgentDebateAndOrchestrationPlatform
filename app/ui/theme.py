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
