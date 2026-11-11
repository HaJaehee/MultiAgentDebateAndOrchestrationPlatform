"""끝난 턴을 고치는 세 가지를 한 대화에서 이어서 확인합니다.

* ADR-036 발언 하나만 다시 실행 (제자리 갱신·버전 보존·그때의 맥락) 과 인라인 코멘트 (그 에이전트에게만)
* ADR-038 결정 장부 직접 수정 (항목 단위·낙관적 잠금·유저 권위의 기록) → 다음 턴의 모델이 그 결정을 뒤집지 않는가
* 두 번째 턴까지 가면서 ADR-048(턴별 집계), ADR-051(번호를 다시 쓰지 않음), ADR-052(번호가 턴을 넘어 이어짐)도 함께 봅니다.

순서가 있는 이야기라 모듈의 시험은 위에서 아래로 한 번에 돕니다 (`story` 가 전부 실행하고 시험은 기록을 읽습니다).
"""

import re

import pytest

AGENTS = ["orchestrator", "architect", "critic"]
PROMPT_1 = (
    "배치 작업 스케줄러의 재시도 정책을 정해 주세요. 고정 간격 재시도와 지수 백오프(지터 포함) 중 무엇을 기본으로 할지, "
    "최대 재시도 횟수는 몇 번으로 할지가 쟁점입니다. 작업은 하루 2천 건, 실패의 80%는 일시적 네트워크 오류입니다.\n"
    "파일을 만들거나 읽을 필요는 없습니다. 각 발언은 12줄 이내로 쓰세요."
)
PROMPT_2 = (
    "앞선 결정에 맞춰 운영자가 볼 재시도 정책 점검표를 5항목으로 정리해 주세요. "
    "파일을 만들거나 읽을 필요는 없습니다. 각 발언은 10줄 이내로 쓰세요."
)
RETRY_INSTRUCTION = "이번에는 결론을 먼저 한 줄로 쓰고, 그 줄에 반드시 `RETRY-MARK-7` 이라는 표식을 넣으세요."
COMMENT_TEXT = "여기는 근거가 약합니다. 다음 발언에서 수치 근거를 하나 들고, 그 문장 끝에 `COMMENT-ACK-3` 이라고 적어 주세요."
USER_DECISION = "유저 확정: 최대 재시도는 4회로 한다 (USER-DECISION-4)"
USER_ISSUE = "유저 추가 쟁점: 재시도 소진 후 알림 채널을 어디로 보낼지 (USER-ISSUE-9)"


def _kind(message) -> str:
    return (message.turn_meta or {}).get("kind") or ""


@pytest.fixture(scope="module")
def story(mado):
    from app.orchestration import ledger_edit, speech_comments

    async def go():
        sid = await mado.new_session("revise", agents=AGENTS, max_rounds=2, converge_early=False)
        first = await mado.turn(sid, PROMPT_1)
        if first.error is not None:
            raise first.error
        before = await mado.messages(sid)
        turn1 = (await mado.turns(sid))[-1]
        usage_before = dict(turn1.usage or {})
        speeches = [m for m in before if _kind(m) == "speech"]
        target = next(m for m in speeches if m.sender_key == "architect" and m.round_number == 1)
        plan = next(m for m in before if _kind(m) == "plan")
        synthesis = next(m for m in before if _kind(m) == "synthesis")
        snapshot = {m.id: (m.content, m.speech_no, m.round_number, m.created_at, m.revisions) for m in before}

        # ---- ADR-036: 계획·합성은 다시 실행할 수 없음
        refused = {}
        for label, message in (("plan", plan), ("synthesis", synthesis)):
            try:
                await mado.engine.retry_speech(sid, message.id)
                refused[label] = "허용됨"
            except Exception as exc:  # noqa: BLE001
                refused[label] = f"{type(exc).__name__}: {exc}"

        # ---- ADR-036: 발언 하나 다시 실행
        first_request = len(mado.requests)
        retry_error = ""
        try:
            await mado.engine.retry_speech(sid, target.id, instruction=RETRY_INSTRUCTION)
        except Exception as exc:  # noqa: BLE001
            retry_error = f"{type(exc).__name__}: {exc}"
        retry_requests = mado.requests[first_request:]
        after_retry = await mado.messages(sid)
        revisions_after_retry = await mado.revisions(sid)
        turn1_after_retry = (await mado.turns(sid))[-1]

        # ---- ADR-036: 인라인 코멘트 (critic 의 2라운드 발언에)
        critic_last = next(m for m in reversed(speeches) if m.sender_key == "critic")
        quote = (critic_last.content or "").strip().splitlines()[0][:80]
        async with mado.session_factory()() as db:
            comment = await speech_comments.add_comment(db, sid, critic_last.id, quote, COMMENT_TEXT)

        # ---- ADR-038: 장부 직접 수정
        session = await mado.session(sid)
        original = session.decision_ledger or ""
        sections = ledger_edit.editable_sections(original)
        closed_no = None
        for section in sections:
            if section.title == "미해결 쟁점":
                for item in section.items:
                    if item.issue_no is not None and closed_no is None:
                        item.dropped = True
                        closed_no = item.issue_no
                section.items.append(ledger_edit.LedgerItem(text=USER_ISSUE))
            if section.title == "결정 사항":
                section.items.append(ledger_edit.LedgerItem(text=USER_DECISION))
        edited = ledger_edit.render_sections(sections)
        async with mado.session_factory()() as db:
            saved = await ledger_edit.save_edit(db, sid, original, edited)
        conflict = ""
        try:
            async with mado.session_factory()() as db:
                await ledger_edit.save_edit(db, sid, original, edited.replace("USER-DECISION-4", "USER-DECISION-5"))
            conflict = "덮어씀"
        except ledger_edit.LedgerEditError as exc:
            conflict = f"거부: {exc} / current 같음: {exc.current == saved.ledger}"
        session_after_edit = await mado.session(sid)
        turn1_after_edit = (await mado.turns(sid))[-1]
        requests_before_turn2 = len(mado.requests)

        # ---- 두 번째 턴
        second = await mado.turn(sid, PROMPT_2, timeout=1500)
        return {
            "sid": sid, "first": first, "second": second, "snapshot": snapshot, "target": target,
            "refused": refused, "retry_error": retry_error, "retry_requests": retry_requests,
            "after_retry": after_retry, "revisions_after_retry": revisions_after_retry,
            "usage_before": usage_before, "turn1_after_retry": turn1_after_retry,
            "comment": comment, "critic_last": critic_last,
            "original_ledger": original, "saved": saved, "conflict": conflict, "closed_no": closed_no,
            "session_after_edit": session_after_edit, "turn1_after_edit": turn1_after_edit,
            "requests_before_turn2": requests_before_turn2,
            "messages": await mado.messages(sid), "turns": await mado.turns(sid),
            "revisions": await mado.revisions(sid), "session": await mado.session(sid),
            "comments": await _comments(mado, sid),
        }

    result = mado.run(go(), timeout=3000)
    mado.dump("revise-retry", result["retry_requests"])
    mado.dump("revise-turn2", result["second"].requests)
    return result


async def _comments(mado, sid):
    from app.database.models import SpeechCommentModel

    return await mado.rows(SpeechCommentModel, SpeechCommentModel.session_id == sid)


@pytest.mark.adr(36)
def test_adr036_only_specialist_speeches_can_be_retried(mado, story):
    for label, outcome in story["refused"].items():
        mado.check("ADR-036", f"{label} 발언의 재실행은 거절됨", outcome.startswith("SpeechRetryError"), outcome)
    mado.settle()


@pytest.mark.adr(36)
def test_adr036_retry_rewrites_one_speech_in_place(mado, story):
    target = story["target"]
    mado.check("ADR-036", "재실행이 오류 없이 끝남", not story["retry_error"], story["retry_error"])
    after = {m.id: m for m in story["after_retry"]}
    row = after[target.id]
    old_content, old_no, old_round, old_created, _ = story["snapshot"][target.id]
    mado.check("ADR-036", "같은 메시지 id·발언 번호·라운드·기록 시각을 유지",
               (row.speech_no, row.round_number, row.created_at) == (old_no, old_round, old_created),
               f"{row.speech_no}/{row.round_number}/{row.created_at} vs {old_no}/{old_round}/{old_created}")
    mado.check("ADR-036", "본문이 새로 쓰임", row.content != old_content and bool(row.content.strip()), row.content[:120])
    revisions = row.revisions or []
    mado.check("ADR-036", "이전 버전이 messages.revisions 에 원문 그대로 보존됨",
               len(revisions) == 1 and revisions[0].get("content") == old_content,
               [list(r.keys()) for r in revisions])
    mado.check("ADR-036", "유저가 준 이번 시도 지침이 반영됨 (표식)", "RETRY-MARK-7" in row.content, row.content[:200])

    untouched = [mid for mid, (content, *_rest) in story["snapshot"].items()
                 if mid != target.id and after[mid].content != content]
    mado.check("ADR-036", "다른 발언·계획·합성은 다시 쓰이지 않음", not untouched, untouched)
    added = [m for m in story["after_retry"] if m.id not in story["snapshot"]]
    notes = [m for m in added if (m.turn_meta or {}).get("revised_message") == target.id]
    mado.check("ADR-036", "타임라인 끝에 재실행 안내가 한 건 남음", len(notes) == 1 and added[-1].id == notes[0].id,
               [(m.sender_key, _kind(m), m.content[:80]) for m in added])
    kinds = [r.kind for r in story["revisions_after_retry"]]
    mado.check("ADR-036", "장부를 새 발언에 맞춘 개정(`retry`)이 남음", kinds[-1:] == ["retry"], kinds)
    mado.settle()


@pytest.mark.adr(36)
def test_adr036_retry_sees_the_conversation_as_it_was_then(mado, story):
    speech = [r for r in story["retry_requests"] if r.agent == "architect"]
    mado.check("ADR-036", "재실행 요청이 그 전문가의 연결로 나감", bool(speech), [r.agent for r in story["retry_requests"]])
    if not speech:
        mado.settle()
        return
    text = speech[0].text()
    later = [m for m in story["first"].state.messages if False]  # 상태 대신 DB 기록으로 판단합니다
    snapshot = story["snapshot"]
    target = story["target"]
    messages = sorted(((v[3], mid, v[0]) for mid, v in snapshot.items()), key=lambda item: item[0])
    position = next(i for i, (_at, mid, _c) in enumerate(messages) if mid == target.id)
    leaked = []
    for _at, mid, content in messages[position + 1:]:
        body = (content or "").strip()
        probe = body[len(body) // 2: len(body) // 2 + 60]
        if len(probe) >= 40 and probe in text:
            leaked.append(mid)
    mado.check("ADR-036", "그 발언보다 뒤에 나온 발언은 재실행 맥락에 실리지 않음", not leaked, leaked)
    mado.check("ADR-036", "유저의 이번 시도 지침이 요청에 실림", RETRY_INSTRUCTION in text)
    mado.check("ADR-036", "원래 요청(목표)이 맥락에 있음", PROMPT_1.splitlines()[0][:40] in text)
    mado.note("ADR-036", f"재실행에 쓴 LLM 요청 {len(story['retry_requests'])}건 "
                         f"({[r.agent for r in story['retry_requests']]}) · 마지막 지시 끝부분: …{speech[0].last_user()[-300:]}")
    mado.settle()


@pytest.mark.adr(48)
def test_adr048_retried_attempts_are_counted_for_the_turn(mado, story):
    from app.agents.llm_usage import Usage

    before, after = Usage.of(story["usage_before"]), Usage.of(story["turn1_after_retry"].usage)
    spent = sum((r.usage.get("total_tokens") or 0) for r in story["retry_requests"])
    mado.check("ADR-048", "재실행에 쓴 토큰이 그 턴의 합계에 더해짐",
               after.total_tokens - before.total_tokens == spent and spent > 0,
               f"턴 합계 {before.total_tokens} → {after.total_tokens} (차이 {after.total_tokens - before.total_tokens}) · 재실행 요청 합 {spent}")
    mado.settle()


@pytest.mark.adr(38)
def test_adr038_ledger_edit_is_saved_as_a_user_record(mado, story):
    from app.orchestration import context_memory as memory
    from app.orchestration.ledger_history import tagged_open_issues

    saved = story["saved"]
    mado.check("ADR-038", "저장됨", saved is not None)
    if saved is None:
        mado.settle()
        return
    ledger = story["session_after_edit"].decision_ledger
    mado.check("ADR-038", "세션의 장부가 고친 장부로 바뀜", ledger == saved.ledger and "USER-DECISION-4" in ledger)
    for title in memory.LEDGER_SECTIONS:
        mado.check("ADR-038", f"고친 뒤에도 `## {title}` 절이 그대로 있음", f"## {title}" in ledger)
    open_now = dict(tagged_open_issues(ledger))
    mado.check("ADR-038", "체크를 푼 쟁점이 미해결 목록에서 빠짐", story["closed_no"] not in open_now,
               f"닫은 번호 I-{story['closed_no']} · 지금 {sorted(open_now)}")
    new_no = next((no for no, text in open_now.items() if "USER-ISSUE-9" in text), None)
    highs = [r.issue_high for r in story["revisions_after_retry"]]
    mado.check("ADR-051", "편집기에서 더한 쟁점이 지금까지의 최대 번호보다 큰 새 번호를 받음",
               new_no is not None and new_no > max(highs or [0]), f"새 번호 {new_no} · 이전 최대 {max(highs or [0])}")
    note = saved.note
    mado.check("ADR-038", "변경 안내가 `ledger_edit` 종류의 기록으로 남음",
               (note.get("turn_meta") or {}).get("kind") == "ledger_edit", note.get("turn_meta"))
    mado.check("ADR-038", "안내에 닫은 쟁점 번호와 더한 항목이 적힘",
               f"I-{story['closed_no']}" in note["content"] and "USER-DECISION-4" in note["content"], note["content"][:600])
    memory_row = story["turn1_after_edit"].memory or {}
    mado.check("ADR-038", "마지막 턴의 기억(turns.memory)도 같은 장부로 맞춰짐",
               memory_row.get("decision_ledger") == saved.ledger, list(memory_row.keys()))
    mado.check("ADR-038", "그 사이 바뀐 장부 위에 옛 화면으로 저장하면 거부하고 새 장부를 돌려줌",
               story["conflict"].startswith("거부") and "current 같음: True" in story["conflict"], story["conflict"])
    kinds = [r.kind for r in story["revisions"]]
    mado.check("ADR-051", "직접 수정이 `edit` 개정으로 남음", "edit" in kinds, kinds)
    mado.settle()


@pytest.mark.adr(38)
def test_adr038_next_turn_keeps_the_users_decision(mado, story):
    from app.orchestration.ledger_edit import NOTE_HEADER
    from app.orchestration.ledger_history import tagged_open_issues

    second = story["second"]
    mado.check("ADR-038", "두 번째 턴이 끝남", second.error is None, second.error)
    speeches = [r for r in second.requests if r.agent in ("architect", "critic") and "[결정 장부 갱신]" not in r.last_user()]
    carrying = [r for r in speeches if NOTE_HEADER[:20] in r.text()]
    mado.check("ADR-038", "다음 턴의 전문가 요청에 장부 수정 기록이 실림 (유저 발언과 같은 자리)",
               len(carrying) == len(speeches) > 0, f"{len(carrying)}/{len(speeches)}")
    ledger_prompts = [r for r in second.requests if "[결정 장부 갱신]" in r.last_user()]
    mado.check("ADR-038", "장부를 다시 쓰는 요청도 그 수정 기록을 봄",
               ledger_prompts and all(NOTE_HEADER[:20] in r.text() for r in ledger_prompts),
               f"{sum(NOTE_HEADER[:20] in r.text() for r in ledger_prompts)}/{len(ledger_prompts)}")
    final = story["session"].decision_ledger or ""
    open_now = dict(tagged_open_issues(final))
    mado.check("ADR-038", "모델이 다시 쓴 장부에 유저가 확정한 결정이 남아 있음 (4회)",
               "USER-DECISION-4" in final or re.search(r"최대 재시도[^\n]{0,20}4\s*회", final) is not None,
               final[:1500])
    mado.check("ADR-038", "유저가 닫은 쟁점 번호가 다시 열리지 않음", story["closed_no"] not in open_now,
               f"닫은 번호 I-{story['closed_no']} · 지금 {sorted(open_now)}")
    mado.note("ADR-038", f"두 번째 턴 뒤 미해결 쟁점 번호 {sorted(open_now)} · 유저 표식 유지 "
                         f"{'USER-DECISION-4' in final} · 유저 추가 쟁점 {'USER-ISSUE-9' in final or '(풀렸거나 고쳐 씀)'}")
    mado.settle()


@pytest.mark.adr(36)
def test_adr036_inline_comment_reaches_only_its_agent(mado, story):
    second = story["second"]
    marker = "[유저의 인라인 코멘트]"
    carrying = [r for r in second.requests if marker in r.text()]
    agents = sorted({r.agent for r in carrying})
    mado.check("ADR-036", "코멘트가 그 에이전트(critic)의 요청에만 실림", agents == ["critic"], agents)
    if carrying:
        last_user = carrying[0].last_user()
        mado.check("ADR-036", "코멘트가 마지막 유저 메시지의 맨 끝에 붙음",
                   last_user.rstrip().endswith(COMMENT_TEXT) or last_user.find(marker) > len(last_user) * 0.5,
                   last_user[-400:])
    comment = story["comments"][0]
    critic_new = [m for m in story["messages"] if m.sender_key == "critic" and _kind(m) == "speech"
                  and m.turn_id == story["turns"][-1].id]
    mado.check("ADR-036", "발언이 기록되면서 코멘트가 전달됨으로 바뀜",
               comment.status == "delivered" and critic_new and comment.delivered_message_id == critic_new[0].id,
               f"{comment.status} → {comment.delivered_message_id}")
    public = "\n".join((m.content or "") for m in story["messages"]) + (story["session"].decision_ledger or "")
    mado.check("ADR-036", "코멘트 묶음 머리말이 공용 기록·장부에 새지 않음", marker not in public)
    acknowledged = bool(critic_new) and "COMMENT-ACK-3" in (critic_new[0].content or "")
    mado.note("ADR-036", f"critic 이 코멘트의 요청(표식 COMMENT-ACK-3)을 실제로 따름: {acknowledged}")
    mado.settle()


@pytest.mark.adr(52)
def test_adr052_numbers_continue_across_turns_and_survive_a_retry(mado, story):
    numbered = [m for m in story["messages"] if m.speech_no]
    numbers = [m.speech_no for m in numbered]
    mado.check("ADR-052", "발언 번호가 두 턴에 걸쳐 겹치지 않고 이어짐", numbers == sorted(set(numbers)), numbers)
    first_turn, second_turn = story["turns"][0].id, story["turns"][-1].id
    high_first = max(m.speech_no for m in numbered if m.turn_id == first_turn)
    low_second = min(m.speech_no for m in numbered if m.turn_id == second_turn)
    mado.check("ADR-052", "두 번째 턴의 첫 번호 = 첫 턴의 마지막 번호 + 1", low_second == high_first + 1,
               f"{high_first} → {low_second}")
    headers = [r for r in story["second"].requests if f"· S{story['target'].speech_no}]" in r.text()]
    mado.note("ADR-052", f"번호 {numbers} · 다시 쓴 발언(S{story['target'].speech_no})을 머리줄째 실은 두 번째 턴 요청 {len(headers)}건")
    mado.settle()


@pytest.mark.adr(48)
def test_adr048_two_turns_are_reported_per_turn(mado, story):
    from app.orchestration.usage_report import load_usage_report

    async def load():
        async with mado.session_factory()() as db:
            whole = await load_usage_report(db, story["sid"])
            one = await load_usage_report(db, story["sid"], story["turns"][0].id)
            return whole, one

    whole, one = mado.run(load())
    mado.check("ADR-048", "턴이 둘인 대화는 턴별로 집계됨", whole.step_word == "턴별" and len(whole.by_step) == 2,
               [row.label for row in whole.by_step])
    mado.check("ADR-048", "한 턴만 고르면 라운드별로 보임", one.step_word == "라운드별", [row.label for row in one.by_step])
    architect = next((row for row in one.by_agent if row.key == "architect"), None)
    mado.check("ADR-048", "다시 실행한 발언의 지난 버전도 그 에이전트의 시도로 세어짐 (발언 2건 + 재실행 1건)",
               architect is not None and architect.speeches == 3, architect.speeches if architect else None)
    recorded = sum((t.usage or {}).get("prompt_tokens", 0) + (t.usage or {}).get("completion_tokens", 0)
                   for t in story["turns"])
    mado.check("ADR-048", "대화 전체 합계 = 두 턴 기록의 합", whole.total.total_tokens == recorded,
               f"{whole.total.total_tokens} vs {recorded}")
    mado.settle()
