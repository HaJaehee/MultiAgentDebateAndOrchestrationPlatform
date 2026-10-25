"""토론 피드의 상수와 순수 함수 (카드 시각 표기, 접기 기준, 승인 카드 모양, 다시 불러온 도구 출력 자르기)."""

import logging
from typing import Any, Callable, Coroutine, Dict, List, Tuple

from app.agents.llm_usage import Usage, format_tokens, usage_line
from app.timestamps import format_duration, speech_time_text, speech_timing, to_local


logger = logging.getLogger(__name__)

# @언급 후보를 고르는 쪽. 친 글자를 받아 [{kind, label, detail, insert}] 를 돌려줍니다.
MentionProvider = Callable[[str], Coroutine[None, None, List[Dict[str, str]]]]
# 올린 파일을 작업 공간에 넣는 쪽. (파일 이름, 내용) → 작업 공간 기준 경로.
UploadHandler = Callable[[str, bytes], Coroutine[None, None, str]]
# 도구 승인 카드의 답을 받는 쪽. (요청 id, 결정, 허용 범위, 거부 사유) → 받아들여졌는가.
ToolApprovalHandler = Callable[[str, str, List[str], str], Coroutine[None, None, bool]]
# 계획 승인 카드의 답을 받는 쪽 (ADR-028).
# (요청 id, 결정, 전문가별 태스크, 계획 전체 의견, 태스크별 의견) → 받아들여졌는가.
PlanApprovalHandler = Callable[
    [str, str, List[Dict[str, Any]], str, Dict[str, str]], Coroutine[None, None, bool]
]

# 스트리밍 중인 카드를 다시 그리는 간격(초).
#
# 예전에는 조각이 올 때마다 카드 **전체 내용**으로 `set_content()` 를 불렀습니다.
# NiceGUI 는 그때마다 서버의 이벤트 루프 위에서 마크다운 전체를 HTML 로 다시 바꾸고
# (코드 블록이면 하이라이팅까지), 전체 HTML 을 브라우저로 다시 보냅니다. 스크롤도
# 조각마다 따로 한 통씩 나갔습니다. 20,000자 보고서 한 편이면 전체 변환 6,667번에
# 서버 CPU 82초였고, 뒤로 갈수록 이벤트 루프의 절반 이상을 먹었습니다.
#
# 그러면 서버와 브라우저가 둘 다 바빠 웹소켓이 끊기고, NiceGUI 는 다시 붙을 때 이미
# 지운 화면이거나(3초 초과) 끊긴 사이 보낸 메시지가 1,000통을 넘으면 **페이지를
# 새로고침**합니다. 0.25초마다 한 번이면 변환은 10분의 1, 서식은 그대로 보입니다.
STREAM_RENDER_INTERVAL = 0.25


# DB 에서 다시 그릴 때 도구 출력의 상한(글자).
#
# 파일 읽기나 샌드박스 실행 결과는 수십 KB 가 되기도 합니다. 지난 대화를 다시
# 열면 그 세션의 모든 도구 출력이 한꺼번에 들어오는데, 아코디언이 접혀 있어도
# 내용은 DOM 에 그대로 만들어집니다. 화면에서는 앞부분만 보여주고, 전문은 세션
# 저장 파일에 남깁니다 (저장은 DB 에서 직접 읽습니다).
MAX_RELOADED_TOOL_OUTPUT = 4000


def clip_reloaded_output(text: str, limit: int = MAX_RELOADED_TOOL_OUTPUT) -> str:
    """긴 도구 출력을 화면용으로 자릅니다. 자른 사실과 전문의 위치를 함께 알립니다."""
    body = text or ""
    if len(body) <= limit:
        return body
    hidden = len(body) - limit
    return (
        f"{body[:limit]}\n\n"
        f"… {hidden:,}자 생략 — 전문은 세션 저장(💾) 파일에 있습니다."
    )


# 입력창 문구. 토론이 도는 중에는 같은 칸이 "새 요청" 이 아니라 "개입" 으로
# 동작하므로, 무엇이 될지 문구로 먼저 알려 줍니다. props 로 넘어가는 값이라
# 큰따옴표는 쓰지 않습니다.
IDLE_PLACEHOLDER = "멀티 에이전트에게 토론 및 설계를 요청할 목표나 질문을 입력하십시오... (Shift+Enter 줄바꿈)"
INTERJECT_PLACEHOLDER = "토론 진행 중 — 지금 전송하시면 다음 발언 순서에 개입 메시지로 전달됩니다 (Shift+Enter 줄바꿈)"

# 입력칸에서 무엇이 '보내기' 인가.
#
# `.exact` 는 Ctrl·Shift·Alt·Meta 가 하나라도 눌려 있으면 핸들러를 아예 건너뜁니다.
# 그래서 Shift+Enter 는 여기 걸리지 않고 브라우저의 기본 동작(줄바꿈)으로 갑니다 —
# 입력칸이 autogrow 라 실제로 여러 줄을 쓸 수 있는데, 예전에는 `keydown.enter` 가
# 조합 키를 가리지 않아 줄을 바꾸려는 손이 매번 요청을 보내 버렸습니다.
#
# `.prevent` 는 그 다음에 옵니다. 순서가 중요합니다: Vue 는 적힌 순서대로
# 수식어를 적용하므로 `.prevent.exact` 로 쓰면 Shift+Enter 에서도 기본 동작이
# 막혀 줄바꿈이 사라집니다.
SUBMIT_KEY_EVENT = "keydown.enter.exact.prevent"


# 발언 카드와 계획 승인 카드의 마크다운 확장.
#
# NiceGUI 기본값(`fenced-code-blocks`, `tables`)만 쓰면 빈 줄 없는 줄바꿈이 같은 문단 안에
# 남아 화면에서 공백이 됩니다. 유저가 Shift+Enter 로 나눈 줄이 한 줄로 붙고, 오케스트레이터
# 계획의 `**1. 이름**` 바로 다음 줄 `- 지시` 는 목록이 되지 못하고 제목 줄에 붙었습니다.
# `break-on-newline` 은 그 줄바꿈을 `<br>` 로, `cuddled-lists` 는 글 줄 바로 아래 목록을
# 목록으로 그립니다. 표·코드 블록·번호 목록·문단 나눔은 그대로이고, 원문(`content`)과 복사도
# 그대로입니다.
FEED_MARKDOWN_EXTRAS = ["fenced-code-blocks", "tables", "cuddled-lists", "break-on-newline"]


# 접힌 카드가 보여주는 줄 수. 값 자체는 theme.py 의 `.chat-body-clamped` 가
# 정하고, 여기서는 문구에만 씁니다.
CLAMP_LINES = 3

# 펼치기 버튼을 달지 정하는 어림. 카드 폭에서 text-sm 한 줄이 대략 60~70자이므로
# 세 줄이면 200자쯤 됩니다. 실제 줄 수는 그려 봐야 알 수 있지만, 그걸 재려고 매
# 카드마다 브라우저에 물어보는 것보다 이 어림이 낫습니다. 짧은 글에 버튼이 하나
# 더 붙는 것이 최악이고, 그건 눈에 거슬리는 정도입니다.
CLAMP_MIN_CHARS = 200
CLAMP_MIN_NEWLINES = 3

# 맨 아래로 보낼 때 쓰는 픽셀 값. 실제 높이보다 크기만 하면 되고, 브라우저가
# 알아서 바닥으로 잘라 줍니다.
#
# `percent=1.0` 을 쓰면 안 됩니다. Quasar 는 퍼센트를 **자기가 캐시해 둔** 내용
# 높이로 환산하는데, 스트리밍으로 방금 늘어난 만큼은 아직 그 값에 반영되지
# 않아 늘 한 발 뒤에 섭니다 (측정해 보면 카드 하나 높이만큼, 200px 넘게).
SCROLL_TO_BOTTOM_PX = 10_000_000


def is_clampable(content: str) -> bool:
    """세 줄을 넘길 만한 글인지 (펼치기 버튼을 달지 결정)."""
    body = content or ""
    return len(body) > CLAMP_MIN_CHARS or body.count("\n") >= CLAMP_MIN_NEWLINES


# 결정 요청 줄의 종류별 모양. 도구 상한(앰버)과 컨텍스트 창(주황)은 다른
# 물음이므로, 문구를 읽기 전에 색으로 먼저 구분되어야 합니다.
DECISION_STYLES = {
    "tool_budget": {
        "timeout_label": "자동 마무리",
        "icon": "build_circle",
        "bar": "bg-amber-950/50 border-amber-700/70",
        "icon_color": "text-amber-300",
        "label": "text-amber-100",
        "countdown": "text-amber-300",
        "extend": "unelevated dense no-caps color=amber-7 text-color=grey-10 size=12px",
        "wrap_up": "flat dense no-caps color=amber-3 size=12px",
    },
    "context_window": {
        # 컨텍스트의 무응답은 마무리가 아닙니다 — 오래된 것부터 생략하고 계속
        # 돕니다. 사람이 자리에 없다고 토론을 접을 이유는 없습니다.
        "timeout_label": "자동 생략",
        "icon": "compress",
        "bar": "bg-orange-950/50 border-orange-600/70",
        "icon_color": "text-orange-300",
        "label": "text-orange-100",
        "countdown": "text-orange-300",
        "extend": "unelevated dense no-caps color=orange-8 text-color=grey-10 size=12px",
        "wrap_up": "flat dense no-caps color=orange-3 size=12px",
    },
}

# 도구 승인 카드. 한도 쪽지(앰버·주황)와 다른 물음이라 하늘색으로 구분합니다.
APPROVAL_CARD_CLASSES = (
    "w-full gap-1.5 px-3 py-2 rounded-lg text-xs border bg-sky-950/50 border-sky-700/70"
)
# 에이전트의 질문 카드 (v1.5). 승인이 아니라 대답이라 보라색으로 구분합니다.
QUESTION_CARD_CLASSES = (
    "w-full gap-1.5 px-3 py-2 rounded-lg text-xs border bg-violet-950/50 border-violet-600/70"
)
# 계획 승인 카드 (ADR-028). 도구 승인(하늘색)·한도 쪽지(앰버·주황)와 다른 물음이라 초록으로 구분합니다.
PLAN_CARD_CLASSES = (
    "w-full gap-2 px-3 py-2 rounded-lg text-xs border bg-emerald-950/40 border-emerald-700/70"
)
PLAN_TASK_CLASSES = "w-full gap-1 pl-2.5 relative"
# 전문가 섹션 왼쪽의 세로 막대. 섹션의 테두리가 아니라 따로 둔 칸이라, 아바타와 똑같은 방법으로
# 칠할 수 있습니다 (`background_paint`).
PLAN_TASK_BAR_CLASSES = "absolute left-0 top-0 bottom-0 w-0.5 rounded"
# 전문가 섹션 사이에 두는 빈 줄 하나 (카드 글씨 text-xs 의 한 줄 = 1rem). 붙어 있으면 어느 칸이
# 누구의 것인지 한눈에 갈리지 않습니다.
PLAN_TASK_GAP = "mt-4"
# 승인 · 거부처럼 답을 정하는 버튼의 글씨 크기 (계획 승인 카드와 도구 승인 카드). 카드 본문
# (text-xs)과 같은 12px 입니다. Quasar 의 `md`(14px)는 카드의 다른 글씨보다 두드러지게 컸습니다.
DECISION_BUTTON_SIZE = "size=12px"
# 권장안·대안을 고르는 라디오 버튼의 크기. 동그라미가 옆 글씨(text-xs, 12px)와 같은 크기가
# 되게 합니다. 라디오·체크박스·토글의 `size` 는 버튼과 뜻이 다릅니다 — 버튼은 글씨 크기이지만
# 이쪽은 바깥 칸의 크기이고, `dense` 일 때 동그라미는 그 절반입니다. 그래서 24px 입니다.
PLAN_RADIO_SIZE = "size=24px"
# 접어 둔 칸(완료 기준 · 의견)을 여는 버튼. 평평하면 글자처럼 보여 눌리는 것인 줄 모르므로,
# 그림자가 있는 기본 모양에 굵은 글씨로 둡니다.
PLAN_TOGGLE_PROPS = "dense no-caps size=12px color=blue-grey-9 text-color=grey-3"
# 칸 이름은 짧게 두고, 적으면 어떻게 되는지는 툴팁에 둡니다.
PLAN_TASK_HINT = "비워 두면 계획 본문을 따릅니다."
PLAN_DONE_WHEN_HINT = "작성하면 태스크 완료 여부를 기준에 맞추어 평가합니다."
PLAN_COMMENT_HINT = "작성하면 오케스트레이터가 계획을 다시 씁니다."


def toggled_visibility(visible: bool, value: Any) -> bool:
    """접는 버튼을 눌렀을 때 그 칸이 보일지. 글이 적힌 칸은 접지 않습니다.

    접힌 칸의 글도 답에 실립니다. 적어 둔 것이 보이지 않는 채로 전달되면, 무엇을 보냈는지
    사람이 알 수 없습니다.
    """
    if visible and str(value or "").strip():
        return True
    return not visible


def background_paint(color: str) -> Tuple[str, str]:
    """에이전트의 색으로 배경을 칠하는 (클래스, 인라인 스타일). 아바타가 칠해지는 것과 같은 규칙입니다.

    에이전트의 색은 Quasar 색 이름(`teal-8`)이거나 사람이 고른 CSS 색(`#009688`)입니다. 이름이면
    Quasar 의 배경 클래스를, CSS 색이면 인라인 스타일을 씁니다. 표에 적어 둔 근사값(`badge_color`)을
    쓰면 아바타와 살짝 다른 색이 됩니다.
    """
    value = (color or "").strip()
    if not value:
        return "", ""
    if value.startswith("#") or "(" in value:
        return "", f"background-color: {value}"
    return f"bg-{value}", ""


# 위험 등급별 배지 색 (`app/mcp/policy.py` 의 RISK_LABELS 와 같은 키).
RISK_BADGE_COLORS = {
    "read": "blue-grey-7", "state": "blue-grey-7", "write": "amber-9", "exec": "amber-9",
    "outside": "deep-orange-8", "delete": "deep-orange-8", "exec_flagged": "red-8",
    "net": "red-8", "unknown": "red-8",
}
# 도구 기록 아코디언의 결과 배지 (`policy.tool_outcome`): (배지 색, 아이콘, 아이콘 색).
OUTCOME_STYLES = {
    "success": ("teal-8", "build", "text-slate-400"),
    "error": ("amber-9", "build", "text-amber-400"),
    "blocked": ("red-8", "gpp_bad", "text-red-400"),
}

BAR_CLASSES = "w-full items-center gap-2 px-3 py-2 rounded-lg text-xs flex-nowrap"
# 끊긴 턴 안내 줄 (ADR-024). 한도·승인 물음과 달리 토론이 돌지 않을 때 뜨므로 보라색으로 구분합니다.
UNFINISHED_BAR_CLASSES = (
    "w-full items-center gap-2 px-3 py-2 rounded-lg text-xs border "
    "bg-violet-950/50 border-violet-700/70"
)
LABEL_CLASSES = "font-semibold min-w-0 flex-grow"
COUNTDOWN_CLASSES = "font-mono flex-shrink-0 whitespace-nowrap"


def _all_decision_classes(slot: str) -> str:
    """모든 종류가 쓰는 색 클래스. 다시 칠하기 전에 통째로 걷어냅니다."""
    return " ".join(style[slot] for style in DECISION_STYLES.values())


def unfinished_turn_text(info: Dict[str, Any]) -> str:
    """끊긴 턴 안내 줄의 문장. 무엇이 남았고 무엇을 고를 수 있는지."""
    if info.get("phase") == "approval":
        # 계획 승인을 기다리다 멈춘 턴 (ADR-028). 답이 없었든 서버가 내려갔든 오류가 아니고,
        # 아무것도 실행되지 않았습니다.
        head = (
            "계획 승인을 받지 못한 턴이 멈춰 있습니다 — '이어서 진행' 을 누르면 같은 계획으로 "
            "승인 카드가 다시 열립니다"
        )
    elif info.get("status") == "failed":
        head = "이전 턴이 오류로 멈췄습니다"
        if info.get("error"):
            head += f" ({str(info['error']).splitlines()[0][:160]})"
    else:
        head = "서버가 다시 시작되어 이전 턴이 끝나지 못했습니다"
    parts = [head + "."]
    facts = []
    started = info.get("started_at")
    if started:
        try:
            facts.append(f"{to_local(started).strftime('%m-%d %H:%M')} 요청")
        except (TypeError, ValueError):
            pass
    facts.append(f"전문가 발언 {int(info.get('speeches') or 0)}개 기록됨")
    if info.get("orphan_tools"):
        facts.append(f"끝나지 못한 발언이 실행한 도구 {int(info['orphan_tools'])}건")
    if info.get("resumable_speeches") and info.get("can_continue"):
        facts.append(f"도구 단위로 이어 갈 발언 {int(info['resumable_speeches'])}개")
    parts.append(" · ".join(facts) + ".")
    parts.append("새 요청을 보내면 이 턴은 기록에 그대로 남고 더는 묻지 않습니다.")
    return " ".join(parts)


def card_time_text(msg: Dict[str, Any]) -> Tuple[str, str]:
    """발언 카드에 적을 시각 한 줄과 툴팁. 시각이 전혀 없으면 둘 다 빈 문자열.

    발언의 토큰 수(`llm_meta.usage`)를 알면 한 줄 끝에 합계를, 툴팁에 입력·출력·LLM 시간을 붙입니다
    (`card_usage_text`).

    카드는 한 대화 안에서 위아래로 이어 읽으므로 **시각만** 짧게 적고, 날짜까지
    붙은 전체 시각은 툴팁에 둡니다 (저장 문서와 같은 문구 — `speech_time_text`).
    시작과 끝의 날짜가 다르면 끝쪽에만 날짜를 붙입니다. 자정을 넘긴 발언에서
    `23:59:50 → 00:00:12` 만 적으면 거꾸로 흐른 것처럼 보입니다.
    """
    label, full = _card_clock_text(msg)
    if not label:
        return "", ""
    short, detail = card_usage_text(msg)
    if short:
        label += f" · {short}"
    if detail:
        full = f"{full} · {detail}" if full else detail
    return label, full


def card_usage_text(msg: Dict[str, Any]) -> Tuple[str, str]:
    """카드의 사용량: 한 줄에 붙일 `토큰 13.4k` 와 툴팁의 `입력 … · 출력 … · LLM …초`. 모르면 빈 문자열."""
    meta = msg.get("llm_meta")
    usage = Usage.of(meta.get("usage")) if isinstance(meta, dict) else Usage()
    if usage.empty:
        return "", ""
    short = f"토큰 {format_tokens(usage.total_tokens)}" if usage.total_tokens else ""
    return short, usage_line(usage)


def _card_clock_text(msg: Dict[str, Any]) -> Tuple[str, str]:
    t = speech_timing(msg)
    started, finished = t["started"], t["finished"]
    if started is None and finished is None:
        return "", ""

    clock = "%H:%M:%S"
    if t["legacy"] or t["instant"]:
        at = started or finished
        return at.strftime(clock), at.strftime("%Y-%m-%d %H:%M:%S")
    if finished is None:
        # 스트리밍 중인 카드. 끝나면 `_finalize_streaming_message` 가 고쳐 씁니다.
        return f"{started.strftime(clock)} 시작 · 진행 중", f"시작 {started.strftime('%Y-%m-%d %H:%M:%S')}"
    if started is None:
        return f"{finished.strftime(clock)} 종료", speech_time_text(msg)

    end = finished.strftime(clock if finished.date() == started.date() else "%m-%d " + clock)
    # 경과 시간은 종료 시각 **오른쪽에**, 무엇의 시간인지 이름을 붙여 적습니다. 숫자만
    # 두면 "2분 5초" 가 경과인지 남은 시간인지 읽는 사람이 짐작해야 합니다.
    label = f"{started.strftime(clock)} → {end} · 경과 {format_duration(t['seconds'])}"
    return label, speech_time_text(msg)


def _card_classes(msg_type: str, sender_key: str) -> str:
    """말풍선 배경. 실패 안내는 발언과 확실히 구분되어야 합니다."""
    if msg_type == "error":
        return "bg-rose-950/40 border-rose-700/70"
    if msg_type == "user" or sender_key == "user":
        return "bg-blue-950/40 border-blue-800/60"
    if msg_type == "orchestrator" or sender_key == "orchestrator":
        return "bg-indigo-950/50 border-indigo-500/70"
    return "bg-slate-900/90 border-slate-800"


