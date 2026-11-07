"""결정 장부의 쟁점 고정 번호와 개정 이력 (ADR-051, `app/orchestration/ledger_history.py`).

핵심 원칙:

1. **번호 부여 주체 (서버 관리)** — 기존 번호는 유지하고, 번호 태그 없이 재작성된 쟁점은 문구 매칭을 통해 기존 번호(닫힌 쟁점 포함)를 복원하며, 신규 쟁점에만 다음 번호를 발급합니다. 중복되거나 임의 조작된 번호는 서버에서 정정하며, 생략 표시 줄(`...`)은 쟁점으로 처리하지 않습니다.
2. **번호 재사용 방지** — 이미 닫힌 쟁점 번호는 다른 쟁점에 재할당하지 않습니다. 다음 턴이나 턴 재개 시에도 DB의 개정 이력을 조회하여 누적 카운트를 이어갑니다.
3. **개정 이력 보존** — 장부가 갱신될 때마다 개정 이력 레코드가 DB에 즉시 기록되고, 이벤트 페이로드에 포함됩니다. 이력 저장 실패가 토론 진행을 중단시키지 않으며, 격리된 트랜잭션으로 처리되어 다른 작업에 롤백 영향을 주지 않습니다.
4. **턴 정리 및 갈래 복제** — 턴 폐기 시 해당 턴의 개정 이력도 함께 정리되며, 세션 포크(갈래 대화) 시 신규 세션 ID로 이력이 안전하게 복제됩니다.
"""

from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from app.database.models import LedgerRevisionModel, SessionModel
from app.database.session import get_session_factory
from app.orchestration import context_memory as memory
from app.orchestration import ledger_history
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.ledger_history import IssueIndex, assign_issue_ids
from app.session_fork import fork_session
from app.session_ops import discard_turn
from tests.fake_llm import FakeLLMCaller
from tests.test_context_memory import DB_URL, _make_session, _pool


def _ledger(*issues: str, decided=("FastAPI 계층 구조로 간다",), rejected=()) -> str:
    def section(title, items):
        return [f"## {title}", *([f"- {item}" for item in items] or ["- 없음"]), ""]

    return "\n".join([
        *section("요구사항·제약", ["사용자 요청을 따른다"]),
        *section("결정 사항", decided),
        *section("기각된 대안", rejected),
        *section("미해결 쟁점", issues),
        *section("담당·다음 할 일", ["코더: 구현"]),
    ]).strip()


def _open(ledger: str) -> List[str]:
    return memory.ledger_items(ledger, "미해결 쟁점") or []


# =============================================================== 1. 번호 부여 검증


def test_new_issues_are_numbered_and_tagged_issues_keep_their_number():
    index = IssueIndex()
    first, issues = assign_issue_ids(_ledger("캐시 키 형식", "장애 시 동작"), index)
    assert _open(first) == ["[I-1] 캐시 키 형식", "[I-2] 장애 시 동작"]
    assert issues == [{"no": 1, "text": "캐시 키 형식"}, {"no": 2, "text": "장애 시 동작"}]

    second, _ = assign_issue_ids(_ledger("[I-2] 장애 시 동작 — 재시도 3회", "인증 토큰 위치"), index)
    assert _open(second) == ["[I-2] 장애 시 동작 — 재시도 3회", "[I-3] 인증 토큰 위치"]
    assert index.high == 3


def test_an_untagged_issue_with_the_same_text_gets_its_old_number():
    index = IssueIndex()
    assign_issue_ids(_ledger("캐시 키 형식", "장애 시 동작"), index)
    again, _ = assign_issue_ids(_ledger("장애 시  동작!", "캐시 키 형식"), index)
    assert _open(again) == ["[I-2] 장애 시  동작!", "[I-1] 캐시 키 형식"], "공백 및 문장부호 차이는 정규화를 통해 동일 쟁점으로 식별합니다"


def test_duplicate_and_made_up_numbers_are_corrected():
    index = IssueIndex()
    assign_issue_ids(_ledger("캐시 키 형식", "장애 시 동작"), index)
    fixed, issues = assign_issue_ids(
        _ledger("[I-1] 캐시 키 형식", "[I-1] 장애 시 동작", "[I-40] 처음 보는 쟁점"), index,
    )
    assert _open(fixed) == ["[I-1] 캐시 키 형식", "[I-2] 장애 시 동작", "[I-3] 처음 보는 쟁점"], \
        "중복된 태그는 문구 기준으로 정정하고, 유효 범위를 초과한 번호는 신규 번호로 재할당합니다"
    assert [i["no"] for i in issues] == [1, 2, 3]


def test_a_closed_number_is_not_given_to_a_different_issue():
    index = IssueIndex()
    assign_issue_ids(_ledger("캐시 키 형식", "장애 시 동작"), index)
    assign_issue_ids(_ledger("[I-1] 캐시 키 형식"), index)  # I-2 닫힘
    later, _ = assign_issue_ids(_ledger("[I-1] 캐시 키 형식", "인증 토큰 위치"), index)
    assert _open(later) == ["[I-1] 캐시 키 형식", "[I-3] 인증 토큰 위치"]


def test_a_reopened_issue_gets_its_old_number_and_shows_among_recently_closed():
    index = IssueIndex()
    assign_issue_ids(_ledger("캐시 키 형식", "장애 시 동작", "인증 토큰 위치"), index)  # R1: I-1..3
    r2, _ = assign_issue_ids(_ledger("[I-1] 캐시 키 형식", "[I-2] 장애 시 동작"), index)  # R2: I-3 닫힘

    block = ledger_history.recently_closed_block(index, r2)
    assert block == "- [I-3] 인증 토큰 위치"

    r3, _ = assign_issue_ids(_ledger("[I-1] 캐시 키 형식", "인증 토큰 위치"), index)  # R3: 태그 없이 재등장
    assert _open(r3) == ["[I-1] 캐시 키 형식", "[I-3] 인증 토큰 위치"]


def test_recently_closed_lists_the_latest_five_and_clips_long_lines():
    index = IssueIndex()
    names = [f"쟁점 {n} " + "가" * (100 if n == 7 else 1) for n in range(1, 8)]
    for name in names:
        assign_issue_ids(_ledger(name), index)  # 쟁점이 순차적으로 등록된 후 닫힘
    current, _ = assign_issue_ids(_ledger("마지막 쟁점"), index)
    lines = ledger_history.recently_closed_block(index, current).splitlines()
    assert [line.split("]")[0] for line in lines] == ["- [I-7", "- [I-6", "- [I-5", "- [I-4", "- [I-3"]
    assert len(lines[0]) <= len("- [I-7] ") + ledger_history.RECENTLY_CLOSED_CHARS
    assert lines[0].endswith("…")


def test_empty_markers_the_clip_note_and_other_sections_are_left_alone():
    ledger = _ledger("없음") + f"\n- {memory.LEDGER_CLIP_NOTE}"
    index = IssueIndex()
    out, issues = assign_issue_ids(ledger, index)
    assert issues == [] and out == ledger
    assert memory.open_issue_count(ledger) == 0, "생략 안내 문구는 미해결 쟁점으로 카운트하지 않습니다"

    moved, issues = assign_issue_ids(_ledger(decided=["[I-1] 캐시 키는 sha1"], rejected=[]), IssueIndex())
    assert memory.ledger_items(moved, "결정 사항") == ["캐시 키는 sha1"], "고정 번호 태그는 미해결 쟁점 항목에만 부여됩니다"
    assert issues == []


def test_a_ledger_without_open_issues_is_unchanged():
    from tests.fake_llm import LEDGER_REPLY

    ledger = LEDGER_REPLY.strip()
    assert assign_issue_ids(ledger, IssueIndex()) == (ledger, [])


def test_absorbing_a_carried_over_ledger_continues_its_numbers():
    index = IssueIndex()
    index.absorb(_ledger("[I-4] 캐시 키 형식", "[I-7] 장애 시 동작"))
    out, _ = assign_issue_ids(_ledger("[I-4] 캐시 키 형식", "장애 시 동작", "새 쟁점"), index)
    assert _open(out) == ["[I-4] 캐시 키 형식", "[I-7] 장애 시 동작", "[I-8] 새 쟁점"]


def test_tags_split_strip_and_parse():
    assert ledger_history.split_issue_tag("[I-12] 캐시") == (12, "캐시")
    assert ledger_history.split_issue_tag("캐시 [I-12]") == (None, "캐시 [I-12]")
    assert ledger_history.strip_issue_tags("[I-1] 가 [I-2] 나") == "가 나"
    assert [ledger_history.parse_issue_label(v) for v in ("I-3", "[I-3]", "i-3", 3, "S3", 0, True)] == [
        3, 3, 3, 3, None, None, None,
    ]
    assert ledger_history.normalize_issue("[I-3] 캐시 키 (sha1) 형식!") == "캐시 키 형식"


def test_summarize_counts_decisions_and_lists_issues():
    summary = ledger_history.summarize(_ledger("[I-2] 장애", "옛 쟁점", decided=["a", "b"], rejected=["c"]))
    assert summary == {"open": [{"no": 2, "text": "장애"}, {"no": None, "text": "옛 쟁점"}], "decided": 2, "rejected": 1}


# =============================================================== 2. 라운드 변화량


def _rev(turn, rnd, kind, open_nos, decided=0, rejected=0):
    return {"id": f"{turn}-{rnd}-{kind}", "turn_id": turn, "round": rnd, "kind": kind,
            "open": [{"no": n, "text": f"쟁점 {n}"} for n in open_nos], "decided": decided, "rejected": rejected}


def test_round_deltas_compare_each_rounds_last_revision_with_the_one_before_the_round():
    revisions = [
        _rev("t1", 1, "round", [1, 2], decided=1),
        _rev("t1", 1, "subdebate", [1], decided=2),        # 동일 라운드 내에서는 마지막 개정을 사용
        _rev("t1", 2, "final", [1, 3], decided=3, rejected=1),
        _rev("t1", 2, "retry", [3], decided=3),            # 라운드 자체는 아니지만 다음 변화량의 비교 기준점이 됨
        _rev(None, 0, "edit", [3, 4], decided=3),          # 턴 외부에서의 사용자 직접 편집
        _rev("t2", 1, "round", [4], decided=5),
    ]
    deltas = ledger_history.round_deltas(revisions)
    assert set(deltas) == {("t1", 1), ("t1", 2), ("t2", 1)}
    assert deltas[("t1", 1)] == {
        "decided": 2, "rejected": 0, "closed": [], "opened": [1], "open": 1, "final": False,
        "revision_id": "t1-1-subdebate",
    }
    assert deltas[("t1", 2)]["opened"] == [3] and deltas[("t1", 2)]["final"] is True
    assert deltas[("t1", 2)]["rejected"] == 1
    assert deltas[("t2", 1)] == {
        "decided": 2, "rejected": 0, "closed": [3], "opened": [], "open": 1, "final": False,
        "revision_id": "t2-1-round",
    }, "사용자가 직접 편집한 장부 내용이 다음 라운드 변화량 계산의 기준점이 됩니다"


# =============================================================== 3. 개정 이력 (엔진)


class ScriptedLedgers(FakeLLMCaller):
    """장부 갱신 요청 시 사전 정의된 장부 내용을 순차 반환하며, 전달된 프롬프트를 수집합니다."""

    def __init__(self, ledgers: List[str], **kwargs):
        super().__init__(**kwargs)
        self.ledgers = list(ledgers)
        self.ledger_prompts: List[str] = []

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        if memory.LEDGER_PROMPT_MARKER in last:
            self.ledger_prompts.append(last)
            return self.ledgers.pop(0) if self.ledgers else _ledger()
        return super()._reply_for(agent, messages)


@pytest.fixture(autouse=True)
def _revisions_are_recorded_on_a_clean_session(monkeypatch):
    """개정 이력 기록 시점에 DB 세션에 커밋되지 않은 별도 변경사항이 없어야 합니다. 이를 통해 저장 실패 시 개정 레코드만 안전하게 롤백할 수 있습니다."""
    original = ledger_history.record_revision
    calls: List[str] = []

    async def checked(db, **kwargs):
        assert not db.new and not db.dirty and not db.deleted, (
            f"{kwargs.get('kind')} 개정 기록 시점에 커밋되지 않은 미반영 변경이 존재합니다: {db.new} {db.dirty} {db.deleted}"
        )
        calls.append(kwargs["kind"])
        return await original(db, **kwargs)

    monkeypatch.setattr(ledger_history, "record_revision", checked)
    return calls


async def _revisions(sid: str) -> List[LedgerRevisionModel]:
    async with get_session_factory(DB_URL)() as db:
        return list((await db.execute(
            select(LedgerRevisionModel).where(LedgerRevisionModel.session_id == sid).order_by(LedgerRevisionModel.seq)
        )).scalars().all())


SCRIPT = [
    _ledger("캐시 키 형식", "장애 시 동작"),                                        # R1: I-1, I-2
    _ledger("[I-1] 캐시 키 형식 (해시 길이)", "인증 토큰 위치", decided=["a", "b"]),  # R2: I-2 해결(닫힘), I-3 신규 등록
    _ledger("장애 시 동작", "[I-9] 지어낸 번호", decided=["a", "b"], rejected=["c"]),  # 최종 합성: I-2 재오픈, I-4 신규 등록
]


@pytest.mark.asyncio
async def test_each_ledger_update_is_saved_at_once_with_stable_numbers(_revisions_are_recorded_on_a_clean_session):
    sid = await _make_session(max_rounds=3)
    llm = ScriptedLedgers(SCRIPT)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt="설계해줘", on_event=on_event,
    )

    rows = await _revisions(sid)
    assert [(r.kind, r.round_number, r.reason) for r in rows] == [
        ("round", 1, "Round 1"), ("round", 2, "Round 2"), ("final", 3, "최종 합성"),
    ]
    assert [[i["no"] for i in r.open_issues] for r in rows] == [[1, 2], [1, 3], [2, 4]]
    assert [r.issue_high for r in rows] == [2, 3, 4]
    assert [(r.decided, r.rejected) for r in rows] == [(1, 0), (2, 0), (2, 1)]
    assert all(r.turn_id == state.turn_id for r in rows) and [r.seq for r in rows] == [1, 2, 3]
    assert rows[-1].ledger == state.decision_ledger
    assert "- [I-2] 장애 시 동작" in state.decision_ledger and "- [I-4] 지어낸 번호" in state.decision_ledger
    assert (await _get_session(sid)).decision_ledger == state.decision_ledger
    assert _revisions_are_recorded_on_a_clean_session == ["round", "round", "final"]

    updates = [e for e in events if e["type"] == "ledger_updated"]
    assert [e["revision"]["id"] for e in updates] == [r.id for r in rows]
    second = updates[1]["revision"]
    assert (second["opened"], second["closed"], second["round"], second["kind"]) == ([3], [2], 2, "round")
    assert second["open"] == [{"no": 1, "text": "캐시 키 형식 (해시 길이)"}, {"no": 3, "text": "인증 토큰 위치"}]
    assert set(second) >= {"id", "turn_id", "round", "kind", "open", "opened", "closed", "decided", "rejected",
                           "created_at"}

    # 프롬프트: 번호 부여 규칙과 함께, 닫힌 쟁점이 존재할 때만 '최근에 닫힌 쟁점' 안내를 포함합니다
    assert "[I-n]" in llm.ledger_prompts[0] and "[최근에 닫힌 쟁점" not in llm.ledger_prompts[1]
    assert "[최근에 닫힌 쟁점" in llm.ledger_prompts[2] and "- [I-2] 장애 시 동작" in llm.ledger_prompts[2]

    loaded = await _load_revisions(sid)
    assert [(r["opened"], r["closed"]) for r in loaded] == [([1, 2], []), ([3], [2]), ([2, 4], [1, 3])]


@pytest.mark.asyncio
async def test_the_next_turn_continues_numbering_from_the_saved_revisions():
    sid = await _make_session(max_rounds=1)
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=ScriptedLedgers([_ledger("캐시 키 형식", "장애 시 동작")]))
    await engine.run_turn(session_id=sid, user_prompt="첫 턴")
    engine.llm_caller = ScriptedLedgers([_ledger("[I-2] 장애 시 동작", "새 쟁점")])
    second = await engine.run_turn(session_id=sid, user_prompt="둘째 턴")

    assert _open(second.decision_ledger) == ["[I-2] 장애 시 동작", "[I-3] 새 쟁점"]
    rows = await _revisions(sid)
    assert [r.issue_high for r in rows] == [2, 3]
    assert len({r.turn_id for r in rows}) == 2


@pytest.mark.asyncio
async def test_a_failed_revision_save_does_not_stop_the_debate(monkeypatch):
    """개정 이력 저장이 실패하더라도 전체 토론 흐름은 중단되지 않으며, 인메모리 장부의 고정 번호는 정상 유지되어 이후 개정이 정상 저장됩니다."""
    real_insert = ledger_history.insert
    failures = {"left": 1}

    class _Broken:
        def __init__(self, table):
            self.statement = real_insert(table)

        def values(self, **kwargs):
            if failures["left"]:
                failures["left"] -= 1
                kwargs["kind"] = None  # NOT NULL 제약조건 위반 유도 (저장 실패 시뮬레이션)
            return self.statement.values(**kwargs)

    monkeypatch.setattr(ledger_history, "insert", _Broken)
    sid = await _make_session(max_rounds=2)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(
        agent_pool=_pool(), llm_caller=ScriptedLedgers(SCRIPT[:1] + [_ledger("[I-2] 장애 시 동작", "새 쟁점")]),
    ).run_turn(session_id=sid, user_prompt="설계해줘", on_event=on_event)

    assert state.status == "completed"
    failed = [e for e in events if e["type"] == "persist_failed"]
    assert len(failed) == 1 and failed[0]["label"] == "결정 장부 개정 이력"
    rows = await _revisions(sid)
    assert [(r.kind, [i["no"] for i in r.open_issues]) for r in rows] == [("final", [2, 3])], \
        "실패한 1라운드 개정만 누락되며, 번호 채번은 인메모리 인덱스를 통해 연속성을 유지합니다"
    assert [e["type"] for e in events].count("ledger_updated") == 2, "개정 저장 실패 여부와 무관하게 갱신된 장부 이벤트는 UI로 정상 전달됩니다"


@pytest.mark.asyncio
async def test_resuming_a_turn_keeps_the_numbers_of_its_earlier_revisions():
    from tests.test_turn_resume import CrashingLLM, REQUEST, _engine, _session
    from app.orchestration import turns
    from app.database.models import TurnModel
    import asyncio

    class Crashing(CrashingLLM):
        def __init__(self, ledgers, **kwargs):
            super().__init__(**kwargs)
            self.ledgers = list(ledgers)

        def _reply_for(self, agent, messages):
            if memory.LEDGER_PROMPT_MARKER in (messages[-1]["content"] if messages else ""):
                return self.ledgers.pop(0) if self.ledgers else _ledger()
            return super()._reply_for(agent, messages)

    sid = await _session(max_rounds=3)
    llm = Crashing([_ledger("캐시 키 형식", "장애 시 동작")], crash_at=("coder", 2))
    with pytest.raises(asyncio.CancelledError):
        await _engine(llm).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()

    # 장부는 턴 종료 시점에 저장되므로 중단된 턴의 이전 장부는 비어 있습니다. 모델이 태그 없이 다시 작성하더라도 기존 번호가 올바르게 복원됩니다.
    resumed = Crashing([_ledger("장애 시 동작", "캐시 키 형식", "재개 뒤 새 쟁점")])
    state = await _engine(resumed).resume_turn(session_id=sid, turn_id=turn.id, mode="continue")

    rows = await _revisions(sid)
    assert [r.kind for r in rows][:2] == ["round", "resume"]
    assert [i["no"] for i in rows[1].open_issues] == [2, 1, 3]
    assert "- [I-3] 재개 뒤 새 쟁점" in state.decision_ledger


# =============================================================== 4. 지우기와 갈래


@pytest.mark.asyncio
async def test_discarding_a_turn_removes_its_revisions_only():
    sid = await _make_session(max_rounds=1)
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=ScriptedLedgers([_ledger("앞 턴 쟁점")]))
    first = await engine.run_turn(session_id=sid, user_prompt="첫 턴")
    engine.llm_caller = ScriptedLedgers([_ledger("[I-1] 앞 턴 쟁점", "버릴 쟁점")])
    second = await engine.run_turn(session_id=sid, user_prompt="둘째 턴")

    async with get_session_factory(DB_URL)() as db:
        await discard_turn(db, sid, [], [], turn_id=second.turn_id)
        await db.commit()

    rows = await _revisions(sid)
    assert {r.turn_id for r in rows} == {first.turn_id}


@pytest.mark.asyncio
async def test_a_fork_copies_the_revisions_up_to_its_turn_with_new_ids(tmp_path):
    # 작업 공간을 지정하지 않으면 갈래 생성 시 실제 workspace/ 디렉터리가 인접 경로로 복제되므로 임시 경로를 사용합니다.
    sid = await _make_session(max_rounds=1, workspace_dir=str(tmp_path / "ws"))
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=ScriptedLedgers([_ledger("첫 쟁점")]))
    first = await engine.run_turn(session_id=sid, user_prompt="첫 턴")
    engine.llm_caller = ScriptedLedgers([_ledger("[I-1] 첫 쟁점", "둘째 쟁점")])
    await engine.run_turn(session_id=sid, user_prompt="둘째 턴")

    async with get_session_factory(DB_URL)() as db:
        result = await fork_session(db, sid, first.turn_id)

    source = [r for r in await _revisions(sid) if r.turn_id == first.turn_id]
    copied = await _revisions(result.session_id)
    assert len(copied) == len(source) == 1
    assert copied[0].id != source[0].id and copied[0].turn_id not in (None, first.turn_id)
    assert (copied[0].open_issues, copied[0].issue_high, copied[0].seq) == (
        source[0].open_issues, source[0].issue_high, source[0].seq,
    )


@pytest.mark.asyncio
async def test_deleting_a_session_through_the_orm_takes_its_revisions_along():
    """체험 서버에서는 db.delete(session)을 통해 세션을 삭제합니다. SQLite 환경에서도 외래키 제약조건에 맞춰 연관 개정이 함께 정리되는지 검증합니다."""
    sid = await _make_session(max_rounds=1)
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=ScriptedLedgers([_ledger("쟁점")])).run_turn(
        session_id=sid, user_prompt="요청",
    )
    assert await _revisions(sid)
    async with get_session_factory(DB_URL)() as db:
        await db.delete(await db.get(SessionModel, sid))
        await db.commit()
    assert await _revisions(sid) == []


# =============================================================== 테스트 헬퍼


async def _get_session(sid: str) -> SessionModel:
    async with get_session_factory(DB_URL)() as db:
        return await db.get(SessionModel, sid)


async def _load_revisions(sid: str) -> List[Dict[str, Any]]:
    async with get_session_factory(DB_URL)() as db:
        return await ledger_history.load_revisions(db, sid)

