# 테스트

> 상위: [레퍼런스 개관](README.md) · 이전: [프로젝트 구조](02-project-layout.md)

```bash
pytest -q               # 전체 테스트 스위트 실행 (2,145개 통과·2개 건너뜀, 약 80초 소요)
pytest -v tests/test_config.py
pytest -k "snapshot"
```

---

## 테스트 스위트 목록

| 테스트 파일명 | 검증 및 보호하는 핵심 기능 |
| :--- | :--- |
| `test_layering.py` | **모듈 사이 의존 방향** — 토론 쪽은 화면·NiceGUI 를, 단계 실행기·서비스는 엔진을, 화면 패키지끼리는 서로를 import 하지 않음 (함수 안 import 포함), 로스터·피드의 컴포넌트끼리도 서로를 모르고 mixin 이 없음 |
| `test_public_surface.py` | **바깥(MADOWarGame 구동기)이 기대는 이름과 시그니처** — `LLMCaller._complete_unthrottled` 재정의 지점, `engine.session_factory`·`llm_caller`, `run_turn` 인자 등 |
| `test_events.py` | 이벤트 이름은 `EventType` 으로만 쓰고, 쓰이지 않는 이름이 없으며, 스냅샷 리듀서는 아는 이벤트만 처리 |
| `test_config.py` | `conf.json` 파싱 로딩, 중첩 환경변수 치환, 오케스트레이터 필수 존재 검증 |
| `test_agent_admin.py` | 에이전트 추가/비활성화/삭제 시 `conf.json` 원자적 편집과 UI 잠금 규칙 |
| `test_mcp_admin.py` | MCP 서버 추가/삭제/on-off, 주석(`//`) 및 `${VAR}` 보존, 런타임 실서버 반영 |
| `test_mcp.py` | MCP 클라이언트 stdio 연결 수립, 도구 스키마 검색 및 안전 실행 |
| `test_remote_mcp.py` | 원격(HTTP/SSE) MCP 서버: 설정 유효성, 전송 방식 자동 폴백, 인증 토큰 격리 |
| `test_anythingllm_kb.py` | AnythingLLM 문서 검색(`kb`): Mock AnythingLLM 서버 기반 검색 결과 및 근거 출처 포맷팅, 실패 시 오류 처리, 작업 공간 허용 목록(allowlist), stdio 연결 및 읽기 권한 판정, 실제 도구 루프 연동 검증 (v1.5.1) |
| `test_llm_offload.py` | 스트림 조립(`stream_chunk_builder`)을 스레드에서: 조립 중에도 다른 코루틴이 도는지, 답·사용량 불변, 조립 중 취소 (v1.5.1) |
| `test_markdown_offload.py` | 긴 마크다운을 일꾼 스레드에서: 그리는 중에도 다른 코루틴이 도는지, 최신 내용만 그리는지, 요소 삭제·실패 처리, 제곱 시간이 들던 세 곳, 서버 시작 시 Pygments 플러그인 미리 불러오기, StarUML 변환 (v1.5.1) |
| `test_llm_settings.py` | `llm` 전역 설정 상속, 요청 파라미터 매핑, 단계적 사고 모드 동작 |
| `test_orchestrator.py` | 에이전트 발언 우선순위 정렬 및 전략별 발언 순서 배치 |
| `test_speaker_selection.py` | 오케스트레이터의 동적 발언자 지명 및 실패 시 안전 폴백 메커니즘 |
| `test_parallel_dispatch.py` | 병렬 지시 전략: 비동기 동시 실행, 세부 태스크 분배, 라운드 취합, 동시성 제한 |
| `test_adaptive_graph.py` | 적응형 그래프 토론: 응답 읽기와 허용 범위, 다시 받기와 물러서기, 기준 그래프, 참여자, 끊긴 턴 재개 |
| `test_sub_debate.py` | 서브 토론: 여는 조건과 상한, 본 토론과 별개인 라운드 수, 결론만 합류, 그래프 노드·판정 합류, 실패·정지·개입, 끊긴 턴 재개 |
| `test_personas.py` | 세션별 페르소나 생애주기: 초안 편집 → 첫 턴 잠금 → 세션 재개 |
| `test_agent_appearance.py` | 카드 색상/아이콘: 이미지 업로드 → 설정 저장 → UI 렌더링 → 스냅샷 및 폴백 |
| `test_session_snapshot.py` | **시작된 대화 세션의 완전한 자기완결성 및 외부 설정 격리 보장** |
| `test_roster_service.py` | 로스터의 conf.json 쓰기·반영·다시 읽기·잠금 규칙을 화면 없이 (`RosterService`) |
| `test_speech_retry.py` · `test_speech_comments.py` · `test_speech_cards.py` | 발언 하나 다시 실행(자리·버전·실패·맥락·장부·취소), 인라인 코멘트(대상·전달 시점·삭제·갈래), 카드와 창 |
| `test_ledger_edit.py` | 결정 장부 직접 편집: 형식 왕복, 낡은 화면 충돌, 유저 발언 기록 고정, 재개·갈래·다시 실행, 로스터 패널 |
| `test_modalities.py` | 입력 모달: 체크한 에이전트에게만 파일 내용 전달, 비우면 요청 불변, 초안에 BASE64 없음, 거절 시 글로 이어 가기, 도구가 돌려준 이미지, 샌드박스 그림 |
| `test_roster_components.py` | 로스터 하위 컴포넌트를 `AgentRosterControl` 없이 하나씩 (대역 서비스와 콜백) |
| `test_feed_components.py` | 토론 피드 부품을 `ChatFeed` 없이 하나씩, 그리고 부품을 잇는 조립 규칙 |
| `test_roster_lock.py` | 토론 백그라운드 진행 중 전역 로스터 및 MCP 설정 변경 차단 잠금 |
| `test_roster_selection.py` | 신규 에이전트 추가 시 기존 대화 세션의 활성/비활성 목록 정합성 |
| `test_interaction.py` | 사용자 정지(Stop) 요청 및 중간 개입 메모(Interjection) 정상 반영 |
| `test_session_handoff.py` | 세션 이어받기: 대화 맥락만 초기화하고 작업 공간 파일 및 지식 그래프 인계 |
| `test_order_preview.py` | 로스터 UI의 발언 순서 미리보기와 엔진의 실제 발언 실행 순서 일치 여부 |
| `test_tool_budget.py` | 도구 호출 상한 도달 시 사용자 중재 및 결론 도출 처리 |
| `test_context_window.py` | 모델 컨텍스트 창 포화 시 중간 발언 축소 및 목표/지침 보존 원칙 |
| `test_tool_failure_safety.py` | 도구 실행 실패 시 예외로 중단되지 않고 피드백 문자열로 정상 전파 |
| `test_tool_loop_content.py` | 다중 도구 호출 루프 완료 후 생성된 발언 본문의 완전한 보존 |
| `test_reasoning_isolation.py` | 단계적 사고 과정은 기록에만 남고 다음 에이전트 프롬프트에는 미전파 |
| `test_mermaid_repair.py` | 다이어그램 문법 오류 발생 시 오케스트레이터의 실시간 구문 보정 및 재작성 |
| `test_export_mermaid.py` | 외부 렌더러 없이 자체 구현된 Mermaid → SVG/PNG 변환기 정합성 |
| `test_abort_turn.py` | 긴급 종료(Emergency Stop): 해당 턴 데이터 롤백 및 시작 전 상태 복원 |
| `test_resilience.py` | 브라우저 새로고침, 네트워크 순단, 컨텍스트 한도, 도구 루프 한도 복원력 |
| `test_tool_records.py` | 도구 실행 이력의 상위 발언 메시지 외래키 정상 바인딩 |
| `test_export.py` | 전체 대화 기록의 마크다운 종합 보고서 내보내기 정합성 |
| `test_chat_card.py` | 발언 카드 접기/펼치기 상태 및 자동 스크롤 추적 해제/재개 상호작용 |
| `test_sidebar_times.py` | 세션 목록 카드의 시간 표시 및 실시간 갱신 시점 |
| `test_attention.py` | 승인 대기 알림: 기다리는 물음 집계, 대화를 가리지 않는 신호, 탭 제목·배지·토스트·데스크톱 알림, 방문자 턴 제외 |
| `test_session_title.py` | 새 대화의 첫 제목: 첫 문장 추출(사고 과정·머리글 제외), 적는 조건, 지워진 발언의 제목 되돌리기 |
| `test_db.py` | SQLAlchemy 비동기 ORM 테이블 스키마, 관계 매핑 및 cascade 삭제 |
| `test_open_browser.py` | 브라우저 자동 실행 스크립트의 동적 주소 감지 로직 |
| `test_new_features.py` | 페르소나 영속화, 바인딩 우선순위, MCP roots 프로토콜 응답 |

보조 유틸리티 및 픽스처:

| 파일명 | 주요 용도 |
| :--- | :--- |
| `fake_llm.py` | 외부 네트워크 없이 고속으로 토론 턴을 모의 실행하는 LLM 스텁 |
| `fixtures/stateful_mcp_server.py` | 실제 stdio 프로세스로 구동되어 연결 및 재기동을 검증하는 모의 MCP 서버 |

---

## 주요 특징적 테스트 케이스 분석

### 설정 파일 왕복(Round-trip) 시 바이트 단위 동일성 보장

```python
def test_add_then_remove_leaves_the_file_byte_identical(conf):
    before = conf.read_text(encoding="utf-8")
    for _ in range(3):
        add_mcp_server_to_conf_file("temp_server", "node", ["a.js"], {}, True, conf)
        remove_mcp_server_from_conf_file("temp_server", conf)
    assert conf.read_text(encoding="utf-8") == before
```

지웠다 다시 추가하기를 반복해도 설정 파일의 주석이나 구조가 변형되지 않아야 합니다. **실제 `conf.json` 원본 파일**을 대상으로도 동일한 엄격한 가역성 검증을 수행합니다.

### 사전 검증 실패 시 파일 불변성 보장

```python
def test_a_write_that_fails_validation_never_touches_the_file(conf):
    before = conf.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        add_mcp_server_to_conf_file("bad name", "node", [], {}, True, conf)
    assert conf.read_text(encoding="utf-8") == before
```

입력 유효성 검증 단계가 디스크 I/O 이전에 엄격히 선행되므로, 유효하지 않은 데이터로 인해 불완전한 반쪽짜리 설정 파일이 남는 사고를 원천 방지합니다.

### 실제 MCP 서버 프로세스 반영 여부 검증

`tests/fixtures/stateful_mcp_server.py`를 실제 자식 프로세스로 구동하여, UI 화면 조작이 거치는 전체 실행 경로(`conf.json` 수정 및 `reload_from_config()`)를 철저히 검증합니다:

```python
set_mcp_server_enabled_in_conf_file("probe", False, conf)
await manager.reload_from_config()
assert "probe" not in manager.clients      # 프로세스가 실제로 종료됨을 검증
```

파일만 수정하고 프로세스가 그대로 남아 화면과 실제 서버 상태가 불일치하는 결함을 방지합니다.

### 소스 코드 기본값과 설정 템플릿의 일관성 보장

```python
assert Agent(key="k", name="n", role="r").max_tool_iterations == 30
assert AgentConfig(name="n", role="r").max_tool_iterations == 30
for name in ("conf.json", "conf.example.json"):
    llm = strip_comment_keys(read_conf_file(path)).get("llm", {})
    assert llm.get("max_tool_iterations") == 30
```

소스 코드의 모델 기본값, 전역 Pydantic 모델, 그리고 실제 `conf.json` 및 `conf.example.json`의 기본값이 모두 일관되게 일치하는지 자동 검증합니다. 어느 한 곳이라도 어긋나면 사용자가 기본값을 오인하는 문제가 발생하기 때문입니다.

### 시작된 대화 세션의 완전한 자기완결성 검증

```python
async def test_changing_the_conf_file_does_not_reach_a_started_conversation(db_factory):
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    changed = _pool(critic=AgentConfig(model="anthropic/...", api_base="https://gateway.new/v1", ...))
    agents = {a.key: a for a in await _turn_agents(db_factory, sid, changed)}

    assert agents["critic"].model == "openai/gpt-4o"   # 잠금 시점의 모델 구성 유지
```

`conf.json` 파일의 내용을 대폭 수정하거나 파일을 삭제하더라도, 이미 시작된 대화 세션은 당시의 `config_snapshot` 구성을 그대로 유지하여 토론을 정상 수행함을 보장합니다.

### 카드 드래그 앤 드롭 삽입 위치 정확성 검증

```python
@pytest.mark.parametrize("source", ["architect", "coder", "critic"])
def test_one_drag_can_put_a_card_in_any_position(source):
    ...
    assert landed == {0, 1, 2}
```

카드를 한 번 드래그하여 목록 내 어떤 위치로든(맨 앞, 중간, 맨 뒤) 정확하게 이동시킬 수 있음을 파라미터화 테스트로 검증합니다. 마우스 커서의 상대적 위치(좌/우 절반)를 감지함으로써 '한 칸 이동'과 '맨 뒤로 이동'이 완벽히 구분 동작함을 보장합니다.

---

## 테스트 스위트가 방지해 온 실제 버그 및 회귀 사례

실제 개발 및 운영 과정에서 발견되어 영구적인 회귀 방지(Regression) 테스트로 구축된 핵심 사례들입니다:

| 발생했던 결함 증상 | 근본 원인 및 아키텍처 개선 |
| :--- | :--- |
| 브라우저 새로고침 시 백그라운드 토론 중단 | 코루틴이 소멸한 UI 슬롯 엘리먼트를 건드림 → 브라우저 독립 백그라운드 태스크로 분리 |
| 도구 목록 중복 삽입으로 설정 구문 파괴 | 프롬프트 내의 `[검토 항목]` 줄을 구형 TOML 섹션 헤더로 오독 → JSON 전환으로 해결 |
| 신규 추가된 에이전트가 항상 발언 맨 뒤로 밀림 | 전략 코드 내에 에이전트 키가 고정 하드코딩됨 → `debate_priority` 객체 속성화 |
| 에이전트 추가 시 기존 대화에서 전부 꺼져 보임 | `known_agents` 부재로 의도적 비활성화와 신규 추가를 구분 못 함 → 세션 스키마 개선 |
| 토론 라운드가 길어지면 외부 LLM 400 에러 발생 | 도구 스키마 및 사고 예산 토큰이 컨텍스트 계산에서 누락됨 → 예산 계산식 정밀화 |
| 다음 대화 세션이 이전 대화의 기억을 읽음 | 공식 memory 서버가 프로세스당 단일 파일만 관리함 → 세션 격리형 서버 포크 구현 |
| 카드 드래그 시 순서가 정상 변경되지 않음 | 커서 위치를 무시하고 대상 앞에만 일방 삽입 → 커서 상대 좌표 기반 판별 구현 |
| 두 도구 서버가 서로 다른 작업 공간을 참조함 | 상대 경로를 각 프로세스의 고유 cwd 기준으로 해석함 → 절대 경로 표준화 |

---

## 신규 테스트 작성 시 준수 규칙

- **외부 네트워크 호출을 절대 발생시키지 마십시오.** LLM API 호출은 `fake_llm.py` 모의 객체를 사용하고, MCP 서버 통신은 전용 fixture 프로세스를 사용합니다.
- **전역 싱글턴 설정을 영구 교체하지 마십시오.** `get_config(reload=True, config_path=...)` 호출은 프로세스 전역 상태를 변경하므로, 테스트 종료 시 반드시 원래 설정으로 안전하게 복원해야 합니다.
- **임시 디렉터리(`tmp_path`)를 활용하십시오.** 실제 배포용 `conf.json` 파일을 직접 수정하지 말고 격리된 임시 사본을 생성하여 검증을 수행합니다.
- 비동기 테스트 함수에는 반드시 `@pytest.mark.asyncio` 데코레이터를 부여하십시오.

---

## 관련 문서

- [프로젝트 구조](02-project-layout.md) — 테스트 대상 애플리케이션 모듈 구조
- [핵심 기술 개관](../03-core/README.md) — 각 테스트가 보장하는 핵심 아키텍처 원리

---

> 처음으로: [MADO 사용 설명서](../README.md)
