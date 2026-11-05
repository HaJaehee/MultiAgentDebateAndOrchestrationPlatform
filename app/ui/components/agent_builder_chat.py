"""도우미와 대화하여 설계도를 작성하는 공통 채팅 컴포넌트입니다.

간편 화면의 빌더, 전문가 화면의 '에이전트 만들기' 대화창, '대화로 워크플로 만들기' 대화창에서 공통으로 상속받아 사용합니다.
사용자가 화면에서 수정한 설계도는 다음 발화 시 도우미에게 함께 전달되므로 임의로 덮어쓰이지 않습니다(`draft.with_draft`).
설계도의 시각적 표시 방식과 저장 절차는 이를 상속하는 구체 클래스에서 결정합니다 (`read_card`, `fill_card`, `_bubble`).
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

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

    # ------------------------------------------------------------ 도우미 인터랙션 (서브클래스에서 재정의 가능)
    #
    # 대화 흐름(말풍선 렌더링, 스트리밍 출력, 연결 실패 예외 처리)은 `send` 메서드가 일괄 담당하며,
    # 질문 생성 방식과 응답 파싱 절차는 아래 네 메서드에서 구체화합니다.
    # 워크플로 빌더 대화창(`graph_builder_dialog.py`)에서는 그래프 명세와 다중 에이전트를 다루기 위해 이를 재정의합니다.

    def user_entry(self, text: str) -> str:
        """대화 기록에 남길 사용자 발화 텍스트를 구성합니다. 현재 화면의 설계도 내용을 함께 첨부합니다."""
        return with_draft(text, self.read_card())

    def streaming_view(self, text: str) -> str:
        """스트리밍 출력 중 사용자에게 보여줄 텍스트입니다. 설계도 코드 블록이 시작되면 그 이후는 숨깁니다."""
        return streaming_text(text)

    async def ask(self, on_chunk: Callable[[str], None], restart: Callable[[str], None]) -> str:
        """도우미 에이전트에 질의하고 전체 응답을 수신합니다. `restart` 콜백은 동일 말풍선에서 재시도할 때 내용을 비우는 데 사용합니다."""
        return await ask_builder(
            self.history, servers=self.servers, skills=self.skills, guest=self.guest, on_chunk=on_chunk,
        )

    def take_reply(self, content: str) -> str:
        """도우미 응답을 대화 기록과 설계도에 반영하고, 말풍선에 노출할 본문 텍스트를 반환합니다."""
        visible, data = split_reply(content)
        self.history.append({"role": "assistant", "content": content})
        if data is not None:
            merged = merge_draft(self.read_card(), data)
            self.draft = sanitize_draft(merged, [o.id for o in self.servers], [o.id for o in self.skills])
            self.fill_card()
        return visible

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
        self.history.append({"role": "user", "content": self.user_entry(text)})
        streamed: List[str] = []

        def on_chunk(delta: str) -> None:
            streamed.append(delta)
            reply.set_content(self.streaming_view("".join(streamed)) or "…")

        def restart(note: str) -> None:
            streamed.clear()
            reply.set_content(note or "…")

        try:
            content = await self.ask(on_chunk, restart)
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

        visible = self.take_reply(content)
        reply.set_content(visible or self.UPDATED_NOTE)
