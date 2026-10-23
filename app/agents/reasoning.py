"""모델 응답의 사고 과정(추론 흔적) 다루기: 떼어 내기, 결론 찾기, 인용.

사고 과정은 기록과 화면에만 남기고, 다음 발언자의 프롬프트에는 싣지 않습니다.
"""

import logging


logger = logging.getLogger(__name__)


# Marker emitted by the sequential-thinking protocol, used to hide steps when show_steps = false
CONCLUSION_MARKERS = ("## 최종 결론", "## Final Conclusion", "## 최종결론")

# `native` 모드에서 `_compose_content` 가 추론 트레이스 앞에 붙이는 머리표.
# 그 아래로 인용(`>`) 줄이 이어지고, 빈 줄 뒤부터가 실제 답변입니다.
NATIVE_REASONING_HEADER = "> **[Sequential Thinking]**"

def strip_reasoning_trace(content: str) -> str:
    """발언 본문에서 사고 과정을 떼고 결론만 남깁니다. 못 찾으면 원문 그대로.

    사고 과정은 **그것을 쓴 에이전트에게만** 의미가 있습니다. 다음 발언자에게
    필요한 것은 앞사람이 무엇을 결론지었고 근거가 무엇인가이지, 그가 5단계로
    어떻게 거기 도달했는가가 아닙니다 (순차 토론 지침도 "결론을 입력으로
    받으라" 고 말합니다).

    그런데 트레이스는 발언 본문의 일부라, 그대로 두면 전사를 읽는 모든 자리로
    복사됩니다 — 다음 발언자의 맥락, 최종 합성 전사, 발언자 지명·태스크 분배의
    요약까지. 라운드가 몇 번만 돌아도 전사의 대부분이 '남이 어떻게 생각했는지'
    로 채워지고, 그러면 `fit_context_window` 가 정작 필요한 초반 논의부터
    버리기 시작합니다.

    그래서 **기록에는 남기고 프롬프트에서만 뗍니다.** DB 와 화면은 전문을 그대로
    들고 있으므로 `show_steps` 는 영향을 받지 않습니다.

    두 가지 모양을 처리합니다.

    * `prompt`/`mcp` 모드 — `Thought 1..N` 뒤에 `## 최종 결론` 이 옵니다.
      마커부터 끝까지가 결론입니다.
    * `native` 모드 — `_compose_content` 가 붙인 인용 블록이 본문 앞에 옵니다.
      그 블록만 걷어냅니다.
    """
    text = content or ""
    if not text.strip():
        return text

    # 1) native 모드의 인용 블록. 머리표로 시작할 때만 손댑니다 — 답변 자체가
    #    인용문으로 시작하는 경우를 잘라내지 않기 위해서입니다.
    if text.lstrip().startswith(NATIVE_REASONING_HEADER):
        lines = text.lstrip().splitlines()
        cut = 0
        for i, line in enumerate(lines):
            if line.startswith(">") or not line.strip():
                cut = i + 1
                continue
            break
        remainder = "\n".join(lines[cut:]).strip()
        # 인용 블록이 전부였다면(=답변이 비었다면) 원문을 지킵니다. 빈 발언을
        # 넘기느니 사고 과정이라도 넘기는 편이 낫습니다.
        if remainder:
            text = remainder

    # 2) 단계적 사고 프로토콜의 결론 마커. 여러 마커 중 **가장 먼저 나오는**
    #    위치를 씁니다 (마커 목록의 순서가 아니라 본문에서의 위치).
    found = [idx for idx in (text.find(m) for m in CONCLUSION_MARKERS) if idx != -1]
    if found:
        return text[min(found):].strip()
    return text

def conclusion_from_reasoning(reasoning: str) -> str:
    """사고(reasoning) 안에 쓰인 결론. 결론 마커가 없으면 빈 문자열.

    `prompt` 모드의 사고 프로토콜은 `Thought 1..N` 뒤에 `## 최종 결론` 을 두라고 합니다.
    서버의 reasoning parser 가 사고 종료 표식을 못 찾으면 그 **전부**가 `reasoning_content`
    로 분류되는데, 그때 이 마커가 있으면 결론까지 다 쓰인 것입니다.
    """
    text = reasoning or ""
    found = [idx for idx in (text.find(m) for m in CONCLUSION_MARKERS) if idx != -1]
    return text[min(found):].strip() if found else ""

def reasoning_quote(reasoning: str) -> str:
    """사고를 인용 블록으로. `native` 모드가 본문 앞에 붙이는 모양과 같습니다.

    같은 모양이어야 `strip_reasoning_trace` 가 다음 발언자의 프롬프트에서 걷어냅니다.
    """
    return f"{NATIVE_REASONING_HEADER}\n>\n> " + (reasoning or "").strip().replace("\n", "\n> ")
