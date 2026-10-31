"""새 대화의 첫 제목을 오케스트레이터의 첫 공식 발언에서 따옵니다.

이름을 짓지 않은 대화는 목록에서도 승인 대기 알림에서도 모두 같은 이름이라 구분할 수 없었습니다. 그래서 첫 공식
발언의 첫 문장을 초기 제목으로 씁니다. 사고 과정은 문장으로 치지 않습니다. 이름표로만 쓰는 값이라 LLM 을 따로
부르지 않습니다.

초기 제목일 뿐입니다. 자리표시 제목(`PLACEHOLDER_TITLES`)일 때만 적으므로, 사람이 이름을 바꾼 대화와 쉬운·체험
화면처럼 처음부터 이름이 있는 대화는 건드리지 않고, 한 번 정해진 뒤로는 사람의 몫입니다.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.agents.reasoning import strip_reasoning_trace
from app.config import ORCHESTRATOR_KEY

DEFAULT_SESSION_TITLE = "New Collaborative Debate"
API_SESSION_TITLE = "API Debate"
PLACEHOLDER_TITLES = (DEFAULT_SESSION_TITLE, API_SESSION_TITLE)

# 사이드바 카드가 한 줄로 자르고 툴팁에 전체를 보여 주므로 넉넉히 둡니다. 칸은 255자입니다.
MAX_TITLE_CHARS = 80
# 이보다 짧으면 "A." 처럼 문장이 아니라 번호로 봅니다.
_MIN_SENTENCE_CHARS = 5
# 목록 머리나 `핵심 목표:` 처럼 뒤에 올 내용의 이름표로 보는 길이.
_MAX_LABEL_CHARS = 20
_MAX_COLON_LABEL_CHARS = 12

_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.DOTALL | re.IGNORECASE)
_THOUGHT_LINE = re.compile(r"^(?:revision of\s+)?thought\s*\d+", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,}|={3,})$")
_LIST_MARK = re.compile(r"^(?:[-*+•]\s+|\d+[.)]\s+|[(（]\d+[)）]\s+)")
_WHOLE_BOLD = re.compile(r"^(?:\*\*[^*]+\*\*|__[^_]+__)[:：]?$")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
_MARKS = re.compile(r"\*\*|__|`|~~")
_SENTENCE_END = re.compile(r"[.!?。！？](?=[\"'”’)\]]*(?:\s|$))")
_ENDS_SENTENCE = re.compile(r"[.!?。！？][\"'”’)\]]*$")


def _plain(line: str) -> str:
    """한 줄에서 Markdown 표시를 걷어 냅니다. 제목은 그대로 보이므로 `**` 가 남으면 안 됩니다."""
    text = _LINK.sub(r"\1", line)
    text = _TAG.sub("", text)
    text = _MARKS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_sentence_of(line: str) -> str:
    for match in _SENTENCE_END.finditer(line):
        sentence = line[:match.end()].strip()
        if len(sentence) >= _MIN_SENTENCE_CHARS:
            return sentence
    return line


def _as_title(sentence: str) -> str:
    title = sentence.rstrip(".。:： ").strip()
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title


def title_from_speech(content: str) -> str:
    """발언의 첫 문장으로 만든 제목. 쓸 만한 글이 없으면 빈 문자열.

    머리글과 이름표(`**핵심 목표**`, `1. 핵심 목표`)는 문장이 아니라 건너뜁니다. 어느 대화나 똑같은 말이라 구분에
    도움이 되지 않습니다. 그것밖에 없는 발언이면 첫 머리글을 씁니다. 인용(`>`)도 건너뜁니다. 추론 트레이스와
    첨부 실패 안내가 이 모양이고, 어느 쪽도 이 발언의 주장이 아닙니다.

    사고 과정으로 시작하는데 결론 표시가 없으면 공식 발언이 아직 없는 것입니다. 빈 문자열을 돌려줍니다.
    """
    body = strip_reasoning_trace(_THINK_BLOCK.sub("", content or ""))
    in_fence = False
    first_label = ""
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line or _RULE.match(line) or line.startswith((">", "|")):
            continue
        if _THOUGHT_LINE.match(line):
            return ""

        heading = _HEADING.match(line)
        if heading:
            first_label = first_label or _plain(heading.group(1))
            continue

        without_mark = _LIST_MARK.sub("", line)
        had_mark = without_mark != line
        bold = _WHOLE_BOLD.match(without_mark)
        text = _plain(without_mark)
        if not text:
            continue
        punctuated = bool(_ENDS_SENTENCE.search(text))
        is_label = not punctuated and (
            bold is not None
            or (had_mark and len(text) <= _MAX_LABEL_CHARS)
            or (text.endswith((":", "：")) and len(text) <= _MAX_COLON_LABEL_CHARS)
        )
        if is_label:
            first_label = first_label or text
            continue
        return _as_title(_first_sentence_of(text))
    return _as_title(first_label)


def is_official_speech(message: object) -> bool:
    """오케스트레이터의 공식 발언인가. 실패 안내는 발언이 아닙니다."""
    return getattr(message, "sender_key", "") == ORCHESTRATOR_KEY and getattr(message, "msg_type", "") != "error"


def is_new_conversation(earlier: Iterable[object]) -> bool:
    """앞선 턴에 오케스트레이터의 공식 발언이 없는 대화인가.

    제목을 짓지 않은 채 오래 써 온 대화의 이름이 어느 날 바뀌지 않게 합니다. 같은 턴 안에서는 묻지 않습니다 —
    첫 계획에 쓸 만한 문장이 없으면 그 턴의 다음 공식 발언에서 다시 시도합니다.
    """
    return not any(is_official_speech(message) for message in earlier)
