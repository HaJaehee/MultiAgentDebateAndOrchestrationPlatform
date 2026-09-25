"""대화 기억 — 컨텍스트 창이 차도 잃으면 안 되는 것을 지킵니다 (v0.8.3).

## 무엇이 문제였나

발언자의 맥락에는 세션의 모든 발언이 원문으로 들어가고, 창을 넘으면
`fit_context_window` 가 **오래된 것부터 통째로** 버렸습니다. 기준이 "얼마나 오래됐나"
하나뿐이라, 한 번만 말하고 다시 나오지 않는 것이 먼저 사라졌습니다.

* 1턴에 사용자가 준 제약("Redis 쓰지 마") — 5턴에서 어기게 됩니다.
* 이번 턴 오케스트레이터의 업무 분배 — 뒤쪽 발언자가 자기 몫을 모릅니다.
* 결정과 그 이유, 기각된 대안 — 끝난 논쟁을 다시 시작합니다.

에이전트마다 모델과 창이 달라 **잊는 양도 달랐고**, 최종 합성도 최신부터 채워 초반
요구사항을 빠뜨릴 수 있었습니다.

## 원칙: 대화는 잊되 상태는 남긴다

세 겹입니다. 앞의 것일수록 싸고 권위가 높습니다.

1. **사용자 발언 고정** (`build_user_record`) — 사람이 한 말은 전부, 원문으로, 매 호출의
   목표 메시지에 둡니다. 목표 메시지는 두 자르기(`fit_context_window`,
   `fit_tool_loop_context`) 모두 남기는 자리입니다. 대화 기록 안의 같은 발언은 짧은
   참조로 바꿔 토큰을 두 번 쓰지 않습니다. 이번 턴 오케스트레이터 계획도 같은 자리에
   고정합니다 (`build_plan_pin`).
2. **결정 장부** (`ledger_prompt`, `parse_ledger`) — 오케스트레이터가 라운드마다
   요구사항·결정·기각안·미해결 쟁점·담당을 구조화해 갱신합니다. 매 호출의 **마지막 사용자
   메시지(이번 차례 지시) 바로 앞**에 들어갑니다 (`llm.place_ledger_last`, 아래 "프롬프트 캐싱").
3. **버리는 대신 요약** (`summary_prompt`, `choose_fold_cut`) — 기록이 창을 넘기 전에
   오래된 구간을 누적 요약으로 접습니다. 요약도 목표 메시지에 들어가 잘리지 않습니다.
   기존의 버리기는 마지막 안전장치로 남습니다.

사람 말이 가장 우선이고, 장부와 요약은 LLM 이 만든 파생물입니다. 프롬프트에도 그
순서를 적습니다.

## 저장

장부와 요약은 `sessions` 에 저장하되 **턴이 정상적으로 끝날 때만** 씁니다. 긴급 종료로
요청을 되돌리면(`session_ops.discard_turn`) 그 턴의 발언이 지워지는데, 장부가 이미 그
발언을 반영해 저장돼 있으면 없던 결정이 남습니다. 어디까지 반영했는지는 발언 id 로
적어 두어(`ledger_through_id`, `summary_through_id`), 턴이 오류로 끊겨 저장을 못 했더라도
다음 턴이 거기서부터 이어 접습니다.

## 발언을 얼마나 넘길까 (v0.8.3)

모든 발언이 원문으로 모든 발언자에게 가면 호출 한 번의 입력이 발언 수에 비례해 늘고, 코드가
든 발언은 라운드마다 통째로 다시 실립니다. 발언자마다 기록을 세 층으로 나눕니다
(`OrchestratorEngine._build_context_for_agent`).

* **새 발언** — 이 발언자가 마지막으로 말한 뒤에 나온 것. 아직 응답하지 않은 내용이라 원문
  그대로, 코드도 그대로 줍니다. 비평가는 방금 나온 코드를 읽어야 검토할 수 있습니다.
* **오래된 긴 발언** — 발언자가 끝에 붙인 `## 요지` 만 줍니다 (`extract_digest`). 요지가 없으면
  긴 코드 블록만 참조로 바꾼 원문을 줍니다 (`reference_code_blocks`).
* **그 밖** — 짧은 발언, 이 발언자를 `@이름` 으로 지목한 발언, 자기 발언은 원문을 주되 긴 코드
  블록은 참조로 바꿉니다. 코드는 작업 공간 파일과 산출물 탭에 남아 있습니다.

요지는 발언자가 발언과 함께 쓰므로 추가 호출이 없습니다.

## 프롬프트 캐싱

OpenAI·Gemini·vLLM 등은 **앞부분이 같은** 요청을 싸고 빠르게 처리합니다. 그래서 자주 바뀌는
것은 뒤로 보냅니다. 결정 장부와 라운드 표시는 시스템 프롬프트·목표 메시지가 아니라 **마지막
사용자 메시지(이번 차례 지시) 바로 앞**에 붙습니다 (`LLMCaller.call_agent` 의 `place_ledger_last`).
시스템 프롬프트(페르소나 + 커스텀 지침)와 목표 메시지는 한 턴 동안 그대로이고, 기록은 뒤에만
붙습니다. 새 발언이 오래된 발언이 되며 요지로 바뀌는 자리부터는 캐시가 다시 쓰입니다.

이 모듈은 LLM 을 부르지 않습니다. 부르는 쪽은 `OrchestratorEngine` 입니다.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.agents.llm import estimate_tokens, is_file_writing_call, strip_reasoning_trace
from app.orchestration.state import DebateMessage, DebateState

# 토론 도중 들어온 사용자 발언의 머리표 (`OrchestratorEngine._apply_interjections`).
INTERJECTION_PREFIX = "[토론 중 유저 개입]"

# ---------------------------------------------------------------- 1. 고정

# 사용자 발언 기록이 쓸 수 있는 몫 (그 발언자의 컨텍스트 예산 대비).
#
# 사람 글은 대체로 짧지만 긴 명세를 붙여 넣을 수도 있습니다. 전부 고정하면 머리가
# 창을 넘기고, 머리는 아무도 자를 수 없어 요청이 400 으로 끝납니다. 넘치면 **최근
# 것부터** 고정하고, 들어가지 못한 발언은 기록에 원문으로 남깁니다 (예전처럼 오래되면
# 잘릴 수 있는 자리).
USER_RECORD_SHARE = 0.25
# 이번 턴 오케스트레이터 계획의 몫. 넘치면 고정하지 않고 기록에 둡니다.
PLAN_PIN_SHARE = 0.15
# 발언자 지명·과업 분배처럼 JSON 한 줄을 받는 짧은 호출에서의 몫.
ROUTING_RECORD_SHARE = 0.1

# ---------------------------------------------------------------- 2. 장부

LEDGER_MAX_CHARS = 5000
# 장부는 모든 호출에 실립니다 (마지막 메시지 앞). 창이 작은 에이전트에서 장부가 창을 먹지
# 않도록, 이번 턴 참여자 중 **가장 작은** 예산에 비례해 상한을 둡니다 (`memory_cap`).
LEDGER_SHARE = 0.1
LEDGER_SECTIONS = ("요구사항·제약", "결정 사항", "기각된 대안", "미해결 쟁점", "담당·다음 할 일")
# 장부 갱신 프롬프트의 머리표. 테스트 대역이 이 호출을 알아보는 데에도 씁니다.
LEDGER_PROMPT_MARKER = "[결정 장부 갱신]"

# ---------------------------------------------------------------- 3. 요약

SUMMARY_MAX_CHARS = 6000
# 요약도 모든 발언자의 목표 메시지에 실리므로 장부와 같은 방식으로 상한을 둡니다.
SUMMARY_SHARE = 0.2
SUMMARY_PROMPT_MARKER = "[대화 요약 갱신]"
# 요약으로 접을 때 창을 이만큼까지만 채웁니다. 꽉 채우면 다음 발언 하나에 또 접습니다 —
# 발언 하나가 창의 1/3 쯤 되는 작은 창에서 0.8 로 두었더니 발언마다 요약을 불렀습니다.
SUMMARY_TARGET_FILL = 0.6
# 가장 최근 발언 몇 개는 접지 않습니다. 지금 판단의 근거라 원문이 필요합니다. 작은 창에서
# 셋을 지키면 그 셋만으로 목표를 넘어 접어도 소용이 없었습니다.
SUMMARY_KEEP_RECENT = 1
# 한 번에 접는 요약 호출의 최대 횟수. 옛 대화를 처음 열었을 때 수십 번 부르지 않게 합니다.
# 모자라면 나머지는 기존 방식대로 오래된 것부터 생략됩니다.
SUMMARY_MAX_BATCHES = 3
# 요약·장부 프롬프트에서 지시문과 이전 본문 몫을 빼고 남길 여유(토큰).
PROMPT_OVERHEAD_TOKENS = 1024


def text_tokens(model: str, text: str) -> int:
    """글 한 덩어리의 토큰 수 (메시지 하나로 감싸 셉니다)."""
    if not text:
        return 0
    return estimate_tokens(model, [{"role": "user", "content": text}])


def clip_to_tokens(model: str, text: str, cap: int) -> str:
    """토큰 상한에 맞게 가운데를 덜어냅니다. 앞과 끝이 대체로 가장 쓸모 있습니다."""
    if cap <= 0:
        return ""
    tokens = text_tokens(model, text)
    if tokens <= cap:
        return text
    keep = max(1, int(len(text) * cap / tokens * 0.9))
    head = text[: keep * 2 // 3]
    tail = text[len(text) - keep // 3:] if keep // 3 else ""
    return f"{head}\n…(길어서 가운데를 생략했습니다)…\n{tail}"


def memory_cap(share: float, max_chars: int, budget: int) -> int:
    """장부·요약의 글자 상한. 예산에 비례하되 절대 상한을 넘지 않습니다.

    한글은 대략 한 글자가 한 토큰이라, 토큰 몫을 그대로 글자 수로 씁니다 (영문이면 넉넉한 쪽).
    """
    if budget <= 0:
        return max_chars
    return max(200, min(max_chars, int(budget * share)))


def clip_chars(text: str, limit: int, note: str) -> str:
    """글자 수 상한. 줄 경계에서 자르고 잘렸다는 표시를 붙입니다."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + f"\n{note}"


def render_message(msg: DebateMessage, body: Optional[str] = None) -> str:
    """요약·장부 입력용 한 덩어리. 사고 과정은 뗍니다 (다른 프롬프트와 같은 규칙).

    `body` 를 주면 본문 대신 씁니다 — 이미 고정된 발언을 참조로 바꿀 때. 긴 코드 블록은
    참조로 바꿉니다. 장부와 요약에 필요한 것은 무엇을 만들었는지이지 코드 전문이 아닙니다.
    """
    if msg.sender_key == "user":
        return f"### [User] · Round {msg.round_number}\n{body or msg.content}"
    return (
        f"### {msg.sender_name} ({msg.sender_role}) · Round {msg.round_number}\n"
        f"{body or reference_code_blocks(strip_reasoning_trace(msg.content), msg.tool_calls)}"
    )


# ---------------------------------------------------------------- 코드는 참조로

# 이보다 긴 코드 블록만 참조로 바꿉니다 (줄 수나 글자 수 중 하나라도 넘으면).
CODE_REF_MIN_LINES = 15
CODE_REF_MIN_CHARS = 800
# 다이어그램은 토론의 내용 자체라 그대로 둡니다 (비평가가 구조를 검토합니다).
CODE_REF_KEEP_LANGS = frozenset({"mermaid"})

_PATH_HINT = re.compile(r"`([\w.\-/\\]+\.[A-Za-z0-9]{1,8})`")
_TITLE_HINT = re.compile(r"""title=["']([^"']+)["']""")


def _written_paths(tool_calls: Sequence[Dict[str, Any]]) -> List[str]:
    """이 발언이 파일 쓰기 도구로 쓴 경로 (이번 턴 발언만 도구 기록을 들고 있습니다)."""
    paths: List[str] = []
    for call in tool_calls or []:
        name = str(call.get("tool_name") or call.get("name") or "")
        args = call.get("arguments") or {}
        if not is_file_writing_call(name) or not isinstance(args, dict):
            continue
        path = args.get("path") or args.get("file_path") or args.get("filename")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


def reference_code_blocks(text: str, tool_calls: Sequence[Dict[str, Any]] = ()) -> str:
    """긴 코드 블록의 내용을 한 줄 참조로 바꿉니다. 펜스는 남겨 마크다운 모양을 지킵니다.

    참조에는 줄 수, 첫 줄, 짐작되는 파일 경로(펜스 바로 위 줄의 `` `경로` `` 나 펜스의 title,
    이 발언이 쓴 파일)를 적습니다. 다음 발언자는 그 경로를 파일 도구로 읽을 수 있습니다.
    """
    from app.orchestration.engine import _iter_code_fences

    text = text or ""
    blocks = _iter_code_fences(text)
    if not blocks:
        return text
    written = _written_paths(tool_calls)
    out: List[str] = []
    cursor = 0
    for block in blocks:
        code = text[block["start"]:block["end"]]
        lang = (block.get("lang") or "").lower()
        lines = code.strip("\n").splitlines()
        if lang in CODE_REF_KEEP_LANGS or (
            len(lines) < CODE_REF_MIN_LINES and len(code) < CODE_REF_MIN_CHARS
        ):
            continue
        # 코드 바로 앞 몇 줄. 마지막 줄이 여는 펜스입니다.
        before = text[max(0, block["start"] - 400):block["start"]].splitlines()
        hints = _TITLE_HINT.findall(before[-1]) if before else []
        hints += [p for p in _PATH_HINT.findall("\n".join(before[-3:])) if p not in hints]
        for path in written:
            if path not in hints:
                hints.append(path)
        first = next((line.strip() for line in lines if line.strip()), "")
        first = first[:80] + ("…" if len(first) > 80 else "")
        where = f" · 관련 파일: {', '.join(hints[:3])}" if hints else ""
        stub = (
            f"[코드 {len(lines)}줄 생략{(' · ' + lang) if lang else ''} · 첫 줄: {first}{where}. "
            f"원문은 작업 공간 파일(있다면)과 작성자의 발언 기록에 있습니다]\n"
        )
        out.append(text[cursor:block["start"]])
        out.append(stub)
        cursor = block["end"]
    out.append(text[cursor:])
    return "".join(out)


# ---------------------------------------------------------------- 요지

# 발언자에게 붙이는 지시. 다른 에이전트가 나중에 읽는 것이 이것뿐일 수 있다는 사실을 알려야
# 요지에 결론·근거·요청이 제대로 담깁니다.
DIGEST_HEADING = "## 요지"
DIGEST_INSTRUCTION = (
    f"발언 맨 끝에 `{DIGEST_HEADING}` 제목으로 3~5줄을 붙이세요 — 핵심 결론, 근거, 다른 "
    f"에이전트에게 요청하거나 넘기는 것, 작성·수정한 파일 경로. 다음 라운드부터 다른 에이전트는 "
    f"이 발언의 요지만 읽을 수 있으니, 요지만 읽어도 무엇이 결정·제안됐는지 알 수 있게 쓰세요."
)
DIGEST_MAX_CHARS = 800
# 이보다 짧은 발언은 요지로 바꿔도 얻는 것이 없어 원문을 줍니다.
DIGEST_MIN_BODY_CHARS = 700

_DIGEST_HEAD = re.compile(r"^\s{0,3}(?:#{1,4}\s*요지\s*:?\s*|\*\*요지\*\*\s*:?\s*)$", re.MULTILINE)
_NEXT_HEAD = re.compile(r"^\s{0,3}#{1,2}\s+\S", re.MULTILINE)


def extract_digest(content: str) -> Optional[str]:
    """발언 끝의 `## 요지` 섹션. 없거나 비었으면 None."""
    text = strip_reasoning_trace(content or "")
    heads = list(_DIGEST_HEAD.finditer(text))
    if not heads:
        return None
    body = text[heads[-1].end():]
    following = _NEXT_HEAD.search(body)
    if following:
        body = body[:following.start()]
    body = body.strip()
    if not body:
        return None
    return clip_chars(body, DIGEST_MAX_CHARS, "…(요지가 길어 뒷부분을 생략했습니다)")


# ---------------------------------------------------------------- 1. 사용자 발언 고정


def opening_index(state: DebateState) -> Optional[int]:
    """이번 턴을 연 요청 발언의 자리. 목표 머리에 이미 있으므로 기록에서 뺍니다."""
    i = state.turn_message_start
    if 0 <= i < len(state.messages):
        msg = state.messages[i]
        if msg.msg_type == "user" and msg.content == state.user_prompt:
            return i
    return None


def _user_label(state: DebateState, index: int, msg: DebateMessage) -> str:
    when = "이전 턴" if index < state.turn_message_start else "이번 턴"
    what = "토론 중 개입" if msg.content.startswith(INTERJECTION_PREFIX) else "요청"
    return f"{when} {what}"


def _user_body(msg: DebateMessage) -> str:
    text = msg.content
    if text.startswith(INTERJECTION_PREFIX):
        text = text[len(INTERJECTION_PREFIX):]
    return text.strip()


@dataclass
class UserRecord:
    text: str = ""
    # state.messages 의 자리 → 기록 번호. 전문이 고정된 발언만 들어갑니다.
    refs: Dict[int, int] = field(default_factory=dict)


def build_user_record(state: DebateState, *, model: str, token_cap: int) -> UserRecord:
    """이 대화에서 사용자가 한 말 (이번 턴을 연 요청은 빼고). 상한 안에서 최근 것부터."""
    skip = opening_index(state)
    candidates = [
        (i, m) for i, m in enumerate(state.messages)
        if m.msg_type == "user" and i != skip and _user_body(m)
    ]
    if not candidates or token_cap <= 0:
        return UserRecord()

    chosen: List[Tuple[int, DebateMessage]] = []
    used = 80  # 머리말
    for i, m in reversed(candidates):
        cost = text_tokens(model, _user_body(m)) + 16
        if used + cost > token_cap:
            continue  # 이것은 기록에 원문으로 남기고, 더 오래된 짧은 발언은 계속 봅니다
        chosen.append((i, m))
        used += cost
    if not chosen:
        return UserRecord()
    chosen.reverse()

    refs = {i: n for n, (i, _m) in enumerate(chosen, start=1)}
    lines = [
        "[유저 발언 기록] 이 대화에서 유저가 한 말입니다. 컨텍스트가 차도 생략되지 않습니다.",
        "앞선 발언끼리 어긋나면 **더 나중 발언**을 따르세요. 결정 장부·요약·다른 에이전트의 "
        "말보다 이 기록이 우선합니다. 토론 기록의 `(유저 발언 #n)` 자리가 여기의 #n 입니다.",
    ]
    for i, m in chosen:
        lines.append(f"\n#{refs[i]} · {_user_label(state, i, m)}\n{_user_body(m)}")
    omitted = len(candidates) - len(chosen)
    if omitted:
        lines.append(
            f"\n(길이 한도로 유저 발언 {omitted}건은 여기에 싣지 못했습니다. 토론 기록에 원문이 있습니다.)"
        )
    return UserRecord(text="\n".join(lines), refs=refs)


def build_plan_pin(state: DebateState, *, model: str, token_cap: int) -> str:
    """이번 턴 오케스트레이터 계획. 몫을 넘거나 계획이 없으면 빈 문자열."""
    i = state.plan_index
    if i is None or not (0 <= i < len(state.messages)):
        return ""
    body = strip_reasoning_trace(state.messages[i].content).strip()
    if not body:
        return ""
    text = (
        "[이번 턴 오케스트레이터 계획] 턴을 시작할 때 세운 목표와 업무 분배입니다. "
        "앞선 기록이 생략돼도 남습니다.\n" + body
    )
    return text if text_tokens(model, text) <= token_cap else ""


def user_placeholder(number: int) -> str:
    return f"(유저 발언 #{number} — 위 [유저 발언 기록]에 전문이 있습니다)"


OPENING_PLACEHOLDER = "(이번 턴 요청 — 위 [User Goal / Current Request]에 전문이 있습니다)"
PLAN_PLACEHOLDER = "(이번 턴 계획 — 위 [이번 턴 오케스트레이터 계획]에 전문이 있습니다)"


# ---------------------------------------------------------------- 2. 결정 장부


def ledger_prompt(
    *,
    previous: str,
    user_record: str,
    blocks: Sequence[str],
    skipped: int,
    user_prompt: str,
    max_chars: int = LEDGER_MAX_CHARS,
) -> List[Dict[str, str]]:
    """장부 갱신 프롬프트. 이전 장부 + 사용자 발언 + 그 뒤로 새로 나온 발언."""
    sections = "\n".join(f"## {name}" for name in LEDGER_SECTIONS)
    skipped_note = (
        f"\n(이보다 앞선 발언 {skipped}건은 길이 한도로 싣지 못했습니다. 이전 장부에 반영돼 있다고 보세요.)\n"
        if skipped else ""
    )
    content = (
        f"{LEDGER_PROMPT_MARKER} 멀티 에이전트 토론의 **결정 장부**를 갱신합니다. 이 장부는 이후 모든 "
        f"에이전트의 시스템 프롬프트에 고정되어, 앞선 발언이 컨텍스트에서 생략돼도 남습니다.\n\n"
        f"[이번 턴 요청]\n{user_prompt}\n\n"
        + (f"{user_record}\n\n" if user_record else "")
        + f"[이전 장부]\n{previous.strip() or '(아직 없음)'}\n\n"
        f"[새로 나온 발언 — 오래된 순]{skipped_note}\n" + "\n\n".join(blocks) + "\n\n"
        "이전 장부에 새 발언을 반영해 **갱신된 장부 전체**를 쓰세요.\n"
        "- 각 항목은 한 줄. 결정과 기각에는 근거를 짧게 붙이세요.\n"
        "- 뒤집힌 결정은 결정 사항에서 빼고 기각된 대안으로 옮기되 이유를 적으세요. 해결된 쟁점은 "
        "미해결에서 지우세요.\n"
        "- 유저 발언과 어긋나는 결정은 유저 발언을 따르세요. 누군가 제안만 했고 합의되지 않은 것은 "
        "결정 사항이 아니라 미해결 쟁점입니다. 없는 사실을 지어내지 마세요.\n"
        "- 파일 경로·함수·인터페이스 이름·수치는 원문 그대로.\n"
        f"- 전체 {max_chars}자 이내. 머리말이나 설명 없이 아래 다섯 제목을 이 순서대로 쓰고, "
        f"비어 있는 칸은 `- 없음` 으로 두세요.\n\n{sections}"
    )
    return [{"role": "user", "content": content}]


def _unfence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_ledger(content: str, max_chars: int = LEDGER_MAX_CHARS) -> Optional[str]:
    """응답에서 장부만 꺼냅니다. `## ` 제목이 하나도 없으면 None (장부가 아닙니다)."""
    text = _unfence(strip_reasoning_trace(content or ""))
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if first is None:
        return None
    ledger = "\n".join(lines[first:]).strip()
    return clip_chars(ledger, max_chars, "- …(장부가 길어 뒷부분을 생략했습니다)")


# ---------------------------------------------------------------- 3. 요약


def summary_block(state: DebateState) -> str:
    """목표 메시지에 들어갈 요약. 요약이 없으면 빈 문자열."""
    if not state.transcript_summary.strip() or state.summary_through <= 0:
        return ""
    return (
        f"[앞선 논의 요약] 이 대화의 앞선 기록 {state.summary_through}건은 컨텍스트 한도로 "
        f"오케스트레이터가 요약했습니다. 원문은 아래 토론 기록에 없습니다. 요약에 없는 세부는 "
        f"지어내지 마세요.\n{state.transcript_summary.strip()}"
    )


def summary_prompt(
    *, previous: str, blocks: Sequence[str], covered: int, max_chars: int = SUMMARY_MAX_CHARS,
) -> List[Dict[str, str]]:
    content = (
        f"{SUMMARY_PROMPT_MARKER} 멀티 에이전트 토론의 오래된 발언을 컨텍스트에서 내보내기 전에 요약으로 "
        f"접습니다. 이 요약은 이후 발언자의 맥락 맨 앞에 고정되어 원문 대신 읽힙니다.\n\n"
        f"[지금까지의 요약 — 앞선 기록 {covered}건]\n{previous.strip() or '(아직 없음)'}\n\n"
        f"[이번에 접을 발언 — 오래된 순]\n" + "\n\n".join(blocks) + "\n\n"
        "위 둘을 합쳐 **갱신된 요약 하나**를 쓰세요.\n"
        "- 누가 무엇을 제안·주장했고 어떻게 정리됐는지 흐름을 남기세요 (발언자 이름 유지).\n"
        "- 결정·기각과 그 이유, 수치·파일 경로·함수·인터페이스 이름·명령은 원문 그대로 남기세요.\n"
        "- 유저 발언은 따로 고정되므로 요약에서는 무엇을 요구했는지만 짧게 적으세요.\n"
        "- 사고 과정, 인사, 반복, 이미 뒤집힌 중간안의 세부는 버리세요. 없는 내용을 지어내지 마세요.\n"
        f"- {max_chars}자 이내. 머리말 없이 요약 본문만 쓰세요."
    )
    return [{"role": "user", "content": content}]


def parse_summary(content: str, max_chars: int = SUMMARY_MAX_CHARS) -> Optional[str]:
    text = _unfence(strip_reasoning_trace(content or ""))
    if not text:
        return None
    return clip_chars(text, max_chars, "…(요약이 길어 뒷부분을 생략했습니다)")


def choose_fold_cut(
    state: DebateState,
    *,
    message_tokens: Sequence[int],
    total_tokens: int,
    budget: int,
    summary_allowance: int,
) -> int:
    """요약으로 접을 끝자리 (state.messages 기준, 이 자리 앞까지). 접을 것이 없으면 지금 자리.

    `message_tokens[k]` 는 `state.messages[state.summary_through + k]` 가 기록에서 차지하는
    토큰입니다. 창을 `SUMMARY_TARGET_FILL` 까지 비울 만큼, 요약이 늘어날 몫까지 더해 접습니다.
    """
    start = state.summary_through
    if budget <= 0 or total_tokens <= budget:
        return start
    need = total_tokens - int(budget * SUMMARY_TARGET_FILL) + max(0, summary_allowance)
    last = len(state.messages) - SUMMARY_KEEP_RECENT
    freed = 0
    cut = start
    for offset, tokens in enumerate(message_tokens):
        index = start + offset
        if index >= last:
            break
        freed += tokens
        cut = index + 1
        if freed >= need:
            break
    # 접어 봐야 조금밖에 안 비는 경우(머리 자체가 크거나, 접을 수 있는 발언이 최근 몇 개뿐일 때)는
    # 접지 않습니다. 그대로 두면 발언마다 한 건씩 접느라 요약 호출이 발언 수만큼 늘어납니다.
    if freed < min(need, int(budget * 0.1)):
        return start
    return cut


def batch_blocks(model: str, blocks: Sequence[str], budget: int) -> List[List[str]]:
    """한 번의 호출에 들어갈 만큼씩 묶습니다. 혼자서도 넘치는 덩어리는 가운데를 덜어냅니다."""
    batches: List[List[str]] = []
    current: List[str] = []
    used = 0
    for block in blocks:
        cost = text_tokens(model, block)
        if cost > budget:
            block = clip_to_tokens(model, block, budget)
            cost = text_tokens(model, block)
        if current and used + cost > budget:
            batches.append(current)
            current, used = [], 0
        current.append(block)
        used += cost
    if current:
        batches.append(current)
    return batches


def through_index(message_ids: Sequence[Optional[str]], through_id: Optional[str]) -> Optional[int]:
    """저장해 둔 "여기까지 반영" id 를 자리로. 비었으면 0, 기록에서 못 찾으면 None."""
    if not through_id:
        return 0
    try:
        return list(message_ids).index(through_id) + 1
    except ValueError:
        return None


def through_id(messages: Sequence[DebateMessage], through: int) -> Optional[str]:
    if through <= 0 or through > len(messages):
        return None
    return messages[through - 1].id


def placeholders_for(
    state: DebateState, record: UserRecord, plan_pinned: bool
) -> Dict[int, str]:
    """기록 안에서 참조로 바꿀 자리 → 참조 문구."""
    out: Dict[int, str] = {i: user_placeholder(n) for i, n in record.refs.items()}
    opening = opening_index(state)
    if opening is not None:
        out[opening] = OPENING_PLACEHOLDER
    if plan_pinned and state.plan_index is not None:
        out[state.plan_index] = PLAN_PLACEHOLDER
    return out

