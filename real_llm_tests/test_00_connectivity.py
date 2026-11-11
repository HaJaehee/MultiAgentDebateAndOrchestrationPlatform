"""연결 확인: 대상 코드가 미러에서 그대로 떴는지, DeepSeek(System 2)와 Jev(System 1)가 답하는지.

다른 시험이 전부 이 연결에 기대므로 가장 먼저 돕니다. 여기서 실패하면 나머지를 돌릴 이유가 없습니다.
"""

import pytest


def test_target_code_is_mirrored_unchanged(mado):
    identity = mado.code_identity
    mado.check("env", "미러의 app/ 이 코드 출처의 app/ 과 같은 내용", identity["mirror_matches_code"], identity)
    if not identity["code_is_target_code"]:
        mado.note("env", f"코드 출처({identity['code']})가 대상({identity['target']})의 app/ 과 다릅니다 — 고친 코드를 대상의 환경으로 돌림")
    mado.check("env", "실행 폴더의 DB 에 묶임", mado.db_path.exists(), mado.db_path)
    from app.config import PROJECT_ROOT

    mado.check("env", "프로젝트 루트가 대상 폴더가 아닌 미러", str(PROJECT_ROOT) != str(mado.target), PROJECT_ROOT)
    mado.note("env", f"대상 {identity['target']} · 모델 {identity['model']} · 패키지 {identity['packages']}")
    mado.settle()


def test_plugins_loaded_from_plugin_folders(mado):
    """ADR-042 §1·§2: 플러그인 폴더에 놓은 파일이 시작할 때 한 번 읽힙니다."""
    from app.orchestration.strategies import STRATEGY_MAP
    from app.plugins import plugin_records

    records = {(r.kind, r.name): r.problem for r in plugin_records()}
    mado.check("ADR-042", "예제 전략 플러그인이 전략 목록에 있음", "consensus_voting" in STRATEGY_MAP, sorted(STRATEGY_MAP))
    mado.check("ADR-042", "플러그인 3개가 문제 없이 등록됨",
               len(records) >= 3 and not any(records.values()), records)
    mado.settle()


def test_deepseek_answers(mado):
    """System 2: 번들 .env 의 전역 연결(DeepSeek)로 한 번 묻고 답을 받습니다."""
    from app.agents.pool import get_agent_pool

    critic = get_agent_pool().get("critic")
    plain = critic.model_copy(update={"allowed_mcp_servers": [], "allowed_skills": []})

    async def ask():
        return await mado.engine.llm_caller.call_agent(
            plain, [{"role": "user", "content": "숫자만 답하세요: 17 + 25 = ?"}], "",
        )

    before = len(mado.requests)
    content, _logs = mado.run(ask(), timeout=180)
    request = mado.requests[before]
    mado.check("env", "DeepSeek 가 답함", "42" in content, content[:200])
    mado.check("ADR-031", "스트림 조립이 이벤트 루프 밖 스레드에서 돎",
               mado.assemblies and not mado.assemblies[-1].on_main_thread,
               mado.assemblies[-1] if mado.assemblies else "조립 기록 없음")
    mado.note("env", f"요청 모델 {request.model} · {request.seconds:.1f}초 · 사용량 {mado.assemblies[-1].usage}")
    mado.settle()


def test_jev_answers_all_three_question_kinds(mado):
    """System 1: Jev `/v1/systemone` 이 noul·choice·score 를 한 번에 판정합니다."""
    from app.agents.decision import DecisionQuestion
    from app.config import get_config

    cfg = get_config().llm.decision_llm
    if not cfg.is_live:
        pytest.skip("결정 LLM 연결 정보가 없습니다")
    caller = mado.engine.llm_caller.decision_caller
    questions = {
        "yes": DecisionQuestion(type="noul", instructions="이 글은 파이썬 코드에 대한 이야기입니까?"),
        "pick": DecisionQuestion(
            type="choice", instructions="글의 주제를 고르세요.",
            criteria=("cache", "cooking"), choice_notes=("캐시와 성능", "요리와 음식"),
        ),
        "grade": DecisionQuestion(
            type="score", instructions="글의 기술적 구체성을 평가하세요.", criteria=("낮음", "보통", "높음"),
        ),
    }
    state = "파이썬 dict 로 LRU 캐시를 만들 때 OrderedDict.move_to_end 를 쓰면 O(1) 로 최근 사용을 갱신합니다."

    before = len(mado.decisions)
    response = mado.run(caller.evaluate(cfg, state, questions), timeout=60)
    call = mado.decisions[before]
    answers = response.answers
    mado.check("env", "Jev 가 200 으로 답함", call.status == 200, f"{call.status} {call.error}")
    mado.check("env", "세 질문 모두 답이 돌아옴", set(answers) == set(questions), sorted(answers))
    mado.check("env", "noul 이 0~1 확률", answers.get("yes") is not None and 0.0 <= (answers["yes"].noul or -1) <= 1.0,
               answers.get("yes"))
    mado.check("env", "choice 가 보낸 선택지 키 중 하나", answers.get("pick") is not None
               and answers["pick"].choice in ("cache", "cooking"), answers.get("pick"))
    mado.check("env", "score 가 척도 범위 안", answers.get("grade") is not None
               and answers["grade"].score is not None and 0 <= answers["grade"].score <= 2, answers.get("grade"))
    mado.note("env", f"Jev 응답 {call.seconds:.1f}초 · 모델 {response.model} · 원문 {call.response}")
    mado.settle()
