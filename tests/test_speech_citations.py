"""발언 번호와 인용 라벨 (ADR-052).

핵심 원칙:

1. **번호 부여** — 에이전트 및 오케스트레이터의 발언에만 대화 세션 내에서 1부터 순차 증가하는 고정 번호가 부여됩니다. 유저 발언 및 시스템 메모에는 번호가 부여되지 않습니다.
   병렬 디스패치 발언은 완료 시점이 아닌 호출 배정 순서대로 번호를 발급받습니다. 발언 재실행 시에는 기존 번호를 유지하고, 초안에서 재개된 발언은 초안 번호를 유지하며 이후 신규 발언은 그보다 큰 번호를 부여받습니다. 다음 턴에서도 이전 번호에 이어 누적 카운트됩니다.
2. **프롬프트 반영** — 히스토리 헤더는 `[이름 (역할) · S14]:` 형식으로 구성되며, 라운드가 변경되어도 동일 문구를 유지하여 프롬프트 캐시 효율을 극대화합니다. 대화 재개 시의 기록(`load_history`)에도 번호가 포함되며, 인용 지침(`CITE_INSTRUCTION`)은 요지 작성 지침 앞에 배치됩니다.
3. **연계 동작** — 새 세션으로 이어갈 때는 장부 내 발언 인용 태그(`[S…]`)를 자동으로 정리합니다. 마크다운 내보내기 헤더에도 `S14` 번호가 표기됩니다.
4. **UI 인터랙션** — 본문의 `[S14]` 및 `[I-3]` 태그는 클릭 가능한 인용 링크로 자동 변환되며(인라인 코드, 수식, 기존 마크다운 링크 내부 제외), 클릭 시 해당 발언 카드로 자동 스크롤되거나 장부 요약 띠의 쟁점 칩을 강조 표시합니다. 존재하지 않는 번호는 알림 메시지로 안내합니다.
"""

from typing import List

import pytest
from nicegui import ui
from sqlalchemy import select

from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory
from app.orchestration import context_memory as memory
from app.orchestration import turns
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateMessage
from app.session_ops import continue_session, strip_speech_refs
from app.ui.math_markdown import render_markdown
from tests.test_context_memory import DB_URL, Recorder, _make_session, _pool


async def _rows(sid: str) -> List[MessageModel]:
    async with get_session_factory(DB_URL)() as db:
        return list((await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all())


def _numbered(rows) -> List[tuple]:
    return [(r.sender_key, turns.kind_of(r), r.speech_no) for r in rows]


# =============================================================== 1. 발언 번호 부여


@pytest.mark.asyncio
async def test_speeches_are_numbered_and_notes_and_users_are_not():
    sid = await _make_session(max_rounds=2)
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=Recorder())
    state = await engine.run_turn(session_id=sid, user_prompt="설계해줘")

    rows = await _rows(sid)
    speeches = [r for r in rows if r.speech_no is not None]
    assert [r.speech_no for r in speeches] == list(range(1, len(speeches) + 1)), "누락 없이 1부터 순차적으로 부여"
    assert {turns.kind_of(r) for r in speeches} <= {turns.KIND_PLAN, turns.KIND_SPEECH, turns.KIND_SYNTHESIS}
    assert all(r.speech_no is None for r in rows if r.sender_key == "user" or r.msg_type == "system")
    assert turns.kind_of(speeches[0]) == turns.KIND_PLAN and turns.kind_of(speeches[-1]) == turns.KIND_SYNTHESIS
    assert [m.speech_no for m in state.messages if m.speech_no] == [r.speech_no for r in speeches]

    second = await engine.run_turn(session_id=sid, user_prompt="이어서")
    later = [m.speech_no for m in second.messages[second.turn_message_start:] if m.speech_no]
    assert later and later[0] == speeches[-1].speech_no + 1, "다음 턴에서도 발언 번호가 연속으로 누적 카운트됩니다"


@pytest.mark.asyncio
async def test_parallel_speeches_are_numbered_in_dispatch_order_not_finish_order():
    from tests.test_parallel_dispatch import _DispatchingLLM, _run

    class _ReverseFinish(_DispatchingLLM):
        DELAYS = {"architect": 0.09, "coder": 0.05, "critic": 0.01}

        async def call_agent(self, agent, messages, custom_instructions="", on_tool_call=None, on_chunk=None,
                             session_id=None, **_kwargs):
            if agent.key in self.DELAYS and not self._is_dispatch(messages):
                self.hold = self.DELAYS[agent.key]
            return await super().call_agent(agent, messages, custom_instructions, on_tool_call, on_chunk, session_id)

    llm = _ReverseFinish()
    rows = await _run(llm)
    numbers = {r.sender_key: r.speech_no for r in rows if r.msg_type == "agent"}
    assert numbers["architect"] < numbers["coder"] < numbers["critic"]
    assert sorted(llm.spans, key=lambda k: llm.spans[k][1]) == ["critic", "coder", "architect"], "병렬 실행 완료 순서가 디스패치 순서와 역순임을 확인"


@pytest.mark.asyncio
async def test_a_resumed_speech_keeps_its_drafted_number_and_new_ones_come_after():
    from tests.test_speech_resume import StepLLM, _cut, _drafts, _engine, _session
    from tests.test_speech_resume import DB_URL as RESUME_DB

    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)
    (draft,) = await _drafts(turn_id)
    drafted = draft.state[turns.DRAFT_SPEECH_NO]
    assert isinstance(drafted, int), "초안 메시지에 사전 예약된 발언 번호를 저장해 둡니다"

    async with get_session_factory(RESUME_DB)() as db:
        assert await turns.speech_no_floor(db, sid, turn_id) >= drafted

    await _engine(StepLLM()).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")
    async with get_session_factory(RESUME_DB)() as db:
        rows = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
    coder = next(r for r in rows if r.sender_key == "coder")
    assert coder.id == draft.id and coder.speech_no == drafted
    later = [r.speech_no for r in rows if r.speech_no and r.created_at > coder.created_at]
    assert later and min(later) > drafted, "초안 이후에 새로 배정된 발언 번호는 초안 번호보다 큽니다"


@pytest.mark.asyncio
async def test_retrying_a_speech_keeps_its_number():
    from tests.test_speech_retry import RecordingLLM, _engine as retry_engine, _finished_turn, _rows as retry_rows, _speeches

    sid = await _finished_turn()
    target = _speeches(await retry_rows(sid), "architect")[0]
    before = target.speech_no
    assert before

    await retry_engine(RecordingLLM(replies={"architect": "NEW"})).retry_speech(sid, target.id)
    after = next(r for r in await retry_rows(sid) if r.id == target.id)
    assert after.content == "NEW" and after.speech_no == before


# =============================================================== 2. 프롬프트 생성 및 캐시


def _msg(key: str, no=None, content="발언") -> DebateMessage:
    return DebateMessage(id=f"{key}-{no}", sender_key=key, sender_name=key.title(), sender_role="Role",
                         content=content, round_number=1, speech_no=no)


def test_labels_and_the_cite_instruction():
    assert _msg("architect", 14).tag == "S14" and _msg("architect").tag == ""
    assert _msg("architect", 14).speaker_label == "Architect (Role) · S14"
    assert _msg("architect").speaker_label == "Architect (Role)", "발언 번호가 없는 기존 레거시 발언은 번호 없이 라벨 생성"
    assert memory.render_message(_msg("coder", 3)).startswith("### Coder (Role) · S3 · Round 1")
    assert "[S14]" in memory.CITE_INSTRUCTION and "[I-3]" in memory.CITE_INSTRUCTION


@pytest.mark.asyncio
async def test_speakers_see_numbered_headers_that_stay_put_between_rounds():
    llm = Recorder()
    sid = await _make_session(max_rounds=2)
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="설계해줘")

    rows = await _rows(sid)
    architect_no = next(r.speech_no for r in rows if r.sender_key == "architect")
    speeches = llm.speeches()
    critic_round_one = next(m for a, m, _l in speeches if a.key == "critic")
    header = f"[System Architect (Architecture) · S{architect_no}]:"
    assert any(header in str(part.get("content")) for part in critic_round_one)

    last = critic_round_one[-1]["content"]
    assert last.index(memory.CITE_INSTRUCTION) < last.index(memory.DIGEST_INSTRUCTION), "인용 지침은 요약 지침 앞에 배치됩니다"

    # 동일 발언자의 1·2라운드 요청 프롬프트 접두부(마지막 메시지 직전까지)가 동일합니다 — 발언 번호는 메시지 객체에 영속화된 값을 활용합니다.
    critic = [m for a, m, _l in speeches if a.key == "critic"]
    first, second = critic[0], critic[1]
    shared = len(first) - 1
    assert second[:shared] == first[:shared]

    synthesis = next(m for a, m, _l in llm.sent if "최종 합의 보고서" in m[-1]["content"])
    assert f"### System Architect (Architecture) · S{architect_no}:" in synthesis[-1]["content"]
    assert memory.CITE_INSTRUCTION in synthesis[-1]["content"]


@pytest.mark.asyncio
async def test_a_reopened_conversation_reads_the_numbers_back():
    sid = await _make_session(max_rounds=1)
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=Recorder()).run_turn(session_id=sid, user_prompt="설계해줘")
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=Recorder())
    async with get_session_factory(DB_URL)() as db:
        history = await engine.recorder.load_history(db, sid)
    assert [m.speech_no for m in history] == [r.speech_no for r in await _rows(sid)]
    assert any(m.speech_no for m in history)


# =============================================================== 3. 세션 연계 및 내보내기


@pytest.mark.asyncio
async def test_continuing_a_conversation_drops_speech_numbers_from_the_ledger():
    sid = await _make_session(decision_ledger="## 결정 사항\n- 캐시는 메모리 [S3] 에 둔다 [S4][S5]\n\n## 미해결 쟁점\n- [I-2] 만료")
    async with get_session_factory(DB_URL)() as db:
        result = await continue_session(db, sid)
        await db.commit()
        new = await db.get(SessionModel, result["session_id"])
    assert new.decision_ledger == "## 결정 사항\n- 캐시는 메모리 에 둔다\n\n## 미해결 쟁점\n- [I-2] 만료"
    assert strip_speech_refs("[S1] 앞 [I-1]") == " 앞 [I-1]", "발언 인용만 제거하고 쟁점 번호([I-n])는 그대로 유지합니다"


def test_the_export_header_carries_the_speech_number():
    from app.export import build_session_markdown

    md = build_session_markdown(
        {"title": "T", "strategy": "sequential_debate"},
        [{"id": "m1", "sender_key": "architect", "sender_name": "Architect", "sender_role": "Role",
          "content": "본문", "round_number": 1, "msg_type": "agent", "speech_no": 7, "tool_calls": []},
         {"id": "u1", "sender_key": "user", "sender_name": "User", "sender_role": "",
          "content": "요청", "round_number": 0, "msg_type": "user", "tool_calls": []}],
        [], [],
    )
    assert "Architect (Role) · S7" in md
    assert "User · S" not in md


# =============================================================== 4. UI 렌더링 및 인터랙션


@pytest.mark.parametrize("text, links", [
    ("[S1][S2] 를 보세요", 2),
    ("[I-3] 쟁점", 1),
    ("[S14]: 동의합니다", 1),
    ("가 [S14] 나", 1),
    ("[S14](https://example.com)", 0),
    ("[[S14]]", 0),
    (r"\[S14\]", 0),
    ("ab[S14]", 0),
    ("`[S14]` 코드", 0),
    ("```\n[S14]\n```", 0),
    ("$x + [S1]$", 0),
])
def test_citations_become_links_outside_code_math_and_written_links(text, links):
    assert render_markdown(text, "fenced-code-blocks", True).count('href="#mado-ref-') == links
    assert "mado-ref" not in render_markdown(text, "fenced-code-blocks", False), "인용 링크 기능이 활성화된 화면에서만 변환"


def test_the_line_start_citation_survives_markdown():
    html = render_markdown("[S14]: 동의합니다", "fenced-code-blocks", True)
    assert "동의합니다" in html, "마크다운 참조 정의 링크 문법([id]: text)으로 오인되어 행 전체가 누락되지 않아야 합니다"


def test_the_citation_script_stops_the_hash_jump_first():
    from app.ui.components.chat_feed.common import CITATION_EVENT, CITATION_JS

    assert CITATION_JS.index("e.preventDefault()") < CITATION_JS.index("emitEvent(")
    assert f"'{CITATION_EVENT}'" in CITATION_JS and "a[href^=\"' + PREFIX + '\"]" in CITATION_JS


def _feed():
    from app.ui.components.chat_feed.feed import ChatFeed

    async def noop(*_a):
        return None

    feed = ChatFeed(noop, show_ledger_strip=True, group_rounds=True, link_citations=True)
    feed.build_ui()
    return feed


def _card_msg(mid, no, round_number=1, content="본문"):
    return {"id": mid, "turn_id": "t1", "round_number": round_number, "sender_key": "architect",
            "sender_name": "Architect", "sender_role": "", "content": content, "msg_type": "agent",
            "turn_meta": turns.meta(turns.KIND_SPEECH), "speech_no": no, "tool_calls": []}


def test_cards_carry_their_number_and_render_links(monkeypatch):
    feed = _feed()
    feed.render_all([_card_msg("m1", 3), _card_msg("m2", 4, content="[S3] 에 반대합니다")])
    card = feed.timeline._cards_by_id["m1"]["card"]
    assert any(getattr(el, "text", "") == "S3" for el in card.descendants())
    assert 'href="#mado-ref-S3"' in feed.timeline._cards_by_id["m2"]["markdown"]._props["innerHTML"]
    assert feed.timeline._cards_by_speech_no[4]["id"] == "m2"

    from app.ui.components.chat_feed.feed import ChatFeed

    async def noop(*_a):
        return None

    plain = ChatFeed(noop)
    plain.build_ui()
    plain.render_all([_card_msg("m1", 3, content="[S3]")])
    assert "mado-ref" not in plain.timeline._cards_by_id["m1"]["markdown"]._props["innerHTML"], "초급자 모드 및 라이트 뷰에서는 링크 변환 없이 일반 텍스트로 표시"


def test_following_a_citation_reveals_the_card_or_the_issue_or_says_it_is_missing(monkeypatch):
    shown: List[str] = []
    scripts: List[str] = []
    monkeypatch.setattr(ui, "notify", lambda message, **_k: shown.append(str(message)))
    monkeypatch.setattr(ui, "run_javascript", lambda code, *a, **k: scripts.append(code))
    feed = _feed()
    feed.render_all([_card_msg("m1", 3, 1), _card_msg("m2", 4, 2)])
    feed.add_ledger_revision({"id": "r1", "turn_id": "t1", "round": 2, "kind": "round",
                              "open": [{"no": 5, "text": "만료"}], "decided": 0, "rejected": 0})
    group_one = feed.timeline._groups[("t1", 1)]["expansion"]
    assert group_one.value is False

    assert feed.follow_citation("S3") is True
    assert group_one.value is True and "getHtmlElement" in scripts[-1], "접힌 라운드 그룹을 전개한 후 해당 카드로 스크롤 이동"
    assert feed.follow_citation("I-5") is True
    assert "ring-2" in feed.ledger_strip._chips[5]._classes

    assert feed.follow_citation("S99") is False and shown[-1] == "이 대화에 S99 발언이 없습니다."
    assert feed.follow_citation("I-9") is False and "I-9" in shown[-1]
