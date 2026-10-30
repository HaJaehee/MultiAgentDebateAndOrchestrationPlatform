"""도우미와 대화해 에이전트 설계도를 채우는 부분 — 쉬운 화면의 빌더와 전문가 화면의 '에이전트 만들기' 창이 함께 씁니다.

사용자가 고친 설계도는 다음 말과 함께 도우미에게 넘어가므로 임의로 덮어쓰이지 않습니다(`draft.with_draft`).
설계도를 어디에 보여 주고 어떻게 저장할지는 쓰는 쪽이 정합니다 (`read_card`, `fill_card`, `_bubble`).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from nicegui import ui

from app.agents.catalog import Option
from app.agents.draft import AgentDraft, ask_builder, merge_draft, sanitize_draft, split_reply, streaming_text, with_draft
from app.agents.llm_types import LLMUnavailableError
from app.ui.math_markdown import MathMarkdown

logger = logging.getLogger(__name__)

GREETING = (
    "안녕하세요! 에이전트를 함께 만들어 보겠습니다.\n\n"
    "**어떤 업무를 맡기고 싶으신가요?** 평소 반복되는 업무나 번거로운 작업을 편하게 말씀해 주세요. "
    "몇 가지 질문을 통해 오른쪽 설계도를 함께 완성해 드리겠습니다."
)
EXAMPLES = [
    "매주 회의록을 분석하여 결정 사항과 액션 아이템을 정리해 주는 비서",
    "매출 데이터를 분석하여 인기 상품과 판매 추세를 알려 주는 분석가",
    "보고서의 논리 구조와 맞춤법을 검토하여 개선점을 제안하는 검토자",
    "고객 문의를 유형별로 분류하고 답변 초안을 작성해 주는 상담 도우미",
]


class BuilderChat:
    """도우미와 주고받는 대화와 설계도. 그리는 일은 하위 클래스가 합니다."""

    # 도우미가 설명 없이 설계도만 보냈을 때 대신 보여 줄 말. 설계도가 놓인 자리를 가리킵니다.
    UPDATED_NOTE = "설계도를 갱신했습니다."

    def __init__(self, servers: List[Option], skills: List[Option], *, guest: bool):
        self.guest = guest
        self.servers = servers
        self.skills = skills
        # 도우미와 주고받은 대화 기록. 사용자의 발화에는 당시의 설계도 상태가 포함됩니다.
        self.history: List[Dict[str, str]] = []
        self.draft = AgentDraft()
        self.busy = False

        self.log: Optional[ui.column] = None
        self.input: Optional[ui.textarea] = None
        self.send_button: Optional[ui.button] = None

    def _bubble(self, text: str, *, me: bool) -> MathMarkdown:
        raise NotImplementedError

    def read_card(self) -> AgentDraft:
        """지금 화면에 보이는 설계도. 도우미에게 함께 넘깁니다."""
        raise NotImplementedError

    def fill_card(self) -> None:
        """`self.draft` 를 화면에 옮깁니다."""
        raise NotImplementedError

    # ------------------------------------------------------------ 대화

    async def _send_input(self) -> None:
        text = (self.input.value or "").strip()
        if text:
            self.input.set_value("")
            await self.send(text)

    async def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self.busy:
            return
        self.busy = True
        self.send_button.disable()
        with self.log:
            self._bubble(text, me=True)
            reply = self._bubble("…", me=False)
        self.history.append({"role": "user", "content": with_draft(text, self.read_card())})
        streamed: List[str] = []

        def on_chunk(delta: str) -> None:
            streamed.append(delta)
            reply.set_content(streaming_text("".join(streamed)) or "…")

        try:
            content = await ask_builder(
                self.history, servers=self.servers, skills=self.skills, guest=self.guest, on_chunk=on_chunk,
            )
        except LLMUnavailableError as exc:
            self.history.pop()
            reply.set_content(f"도우미에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.\n\n`{exc}`")
            return
        except Exception as exc:  # noqa: BLE001 - 실패한 원인을 사용자에게 보여주어야 합니다
            logger.error("The agent builder failed: %s", exc, exc_info=True)
            self.history.pop()
            reply.set_content(f"도우미 응답 중 오류가 발생했습니다: {exc}")
            return
        finally:
            self.busy = False
            self.send_button.enable()

        visible, data = split_reply(content)
        reply.set_content(visible or self.UPDATED_NOTE)
        self.history.append({"role": "assistant", "content": content})
        if data is not None:
            merged = merge_draft(self.read_card(), data)
            self.draft = sanitize_draft(merged, [o.id for o in self.servers], [o.id for o in self.skills])
            self.fill_card()
