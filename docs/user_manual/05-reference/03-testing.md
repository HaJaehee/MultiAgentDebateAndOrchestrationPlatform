# 테스트

> 상위: [레퍼런스 개관](README.md) · 이전: [프로젝트 구조](02-project-layout.md)

```bash
pytest -q               # 전체 (528개, 약 30초)
pytest -v tests/test_config.py
pytest -k "snapshot"
```

---

## 테스트 목록

| 파일 | 지키는 것 |
| :--- | :--- |
| `test_config.py` | `conf.json` 로딩, 환경변수 치환, 오케스트레이터 필수 검증 |
| `test_agent_admin.py` | 에이전트 추가·비활성화·삭제 시 `conf.json` 편집과 화면 잠금 규칙 |
| `test_mcp_admin.py` | MCP 서버 추가·삭제·on/off, 주석·`${VAR}` 보존, 실제 서버 반영 |
| `test_mcp.py` | MCP 클라이언트 연결, 도구 검색, 실행 |
| `test_remote_mcp.py` | 원격(HTTP) MCP 서버: 설정 규칙, 전송 방식 선택, 토큰 보관 |
| `test_llm_settings.py` | `llm` 상속, 파라미터 전달, 단계적 사고 모드 |
| `test_orchestrator.py` | 발언 순서, 전략별 배치 |
| `test_speaker_selection.py` | 오케스트레이터 지명, 실패 시 물러서기 |
| `test_parallel_dispatch.py` | 병렬 지시: 동시 실행, 과업 분배, 라운드 취합, 동시 실행 상한 |
| `test_personas.py` | 페르소나 수명주기: 편집 → 잠금 → 재개 |
| `test_agent_appearance.py` | 카드 색·아이콘: 업로드 → `conf.json` → 화면 → 스냅샷, 그리고 폴백 |
| `test_session_snapshot.py` | **시작한 대화는 자기완결적이다** |
| `test_roster_lock.py` | 토론 중 MCP 구성 잠금 |
| `test_roster_selection.py` | 에이전트 추가 시 기존 대화의 로스터 |
| `test_interaction.py` | 정지 요청과 개입 메모 |
| `test_session_handoff.py` | 세션 이어받기: 컨텍스트만 비우고 작업 공간·지식 그래프는 물려받기 |
| `test_order_preview.py` | 로스터의 발언 순서 미리보기가 실제 순서와 같은가 |
| `test_tool_budget.py` | 도구 호출 상한에 걸린 에이전트를 어떻게 다루는가 |
| `test_context_window.py` | 컨텍스트 창이 찼을 때 무엇을 잃고 무엇을 지키는가 |
| `test_tool_failure_safety.py` | 도구 호출이 실패해도 발언과 프로세스가 살아남는가 |
| `test_tool_loop_content.py` | 도구를 여러 번 부른 발언의 본문이 온전히 남는가 |
| `test_reasoning_isolation.py` | 사고 과정은 기록에 남고 프롬프트에는 실리지 않는다 |
| `test_mermaid_repair.py` | 다이어그램 문법 오류를 오케스트레이터가 그 자리에서 고치는가 |
| `test_export_mermaid.py` | Mermaid → SVG/PNG 렌더러 |
| `test_abort_turn.py` | 긴급 종료: 그 턴만 지우기, 시작 전으로 되돌리기 |
| `test_resilience.py` | 새로고침, 연결 끊김, 컨텍스트 한도, 도구 루프 한도 |
| `test_tool_records.py` | 도구 실행 기록이 그 발언에 붙는가 |
| `test_export.py` | 마크다운 내보내기 |
| `test_chat_card.py` | 발언 카드 접기 규칙, 펼친 카드와 자동 스크롤 |
| `test_sidebar_times.py` | 세션 카드의 시각 표시, 목록을 다시 그리는 시점 |
| `test_db.py` | ORM 스키마, 관계, cascade |
| `test_open_browser.py` | 브라우저 대기 스크립트의 주소 결정 |
| `test_new_features.py` | 페르소나 영속화, host/port 우선순위, MCP roots, 맥락 유지 |

보조:

| 파일 | 용도 |
| :--- | :--- |
| `fake_llm.py` | LLM 스텁 (네트워크 없이 토론을 돌림) |
| `fixtures/stateful_mcp_server.py` | 실제 stdio MCP 서버 (연결·재기동 검증용) |

---

## 특징적인 테스트

### 설정 파일 왕복이 바이트 단위로 같은가

```python
def test_add_then_remove_leaves_the_file_byte_identical(conf):
    before = conf.read_text(encoding="utf-8")
    for _ in range(3):
        add_mcp_server_to_conf_file("temp_server", "node", ["a.js"], {}, True, conf)
        remove_mcp_server_from_conf_file("temp_server", conf)
    assert conf.read_text(encoding="utf-8") == before
```

지웠다 다시 추가하기를 반복해도 파일이 늘어지지 않아야 합니다. **실제
`conf.json`** 으로도 같은 검증을 합니다.

### 실패한 쓰기가 파일을 건드리지 않는가

```python
def test_a_write_that_fails_validation_never_touches_the_file(conf):
    before = conf.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        add_mcp_server_to_conf_file("bad name", "node", [], {}, True, conf)
    assert conf.read_text(encoding="utf-8") == before
```

검증이 쓰기보다 앞에 있어야 반쪽짜리 설정 파일이 생기지 않습니다.

### 실제 MCP 서버에 반영되는가

`tests/fixtures/stateful_mcp_server.py` 를 진짜로 띄워서, 화면 조작이 거치는
경로 그대로 — `conf.json` 을 고치고 `reload_from_config()` — 를 검증합니다.

```python
set_mcp_server_enabled_in_conf_file("probe", False, conf)
await manager.reload_from_config()
assert "probe" not in manager.clients      # 프로세스가 실제로 내려감
```

파일만 고치고 끝나면 화면과 실제로 떠 있는 서버가 어긋납니다.

### 코드 기본값과 설정 파일이 어긋나지 않는가

```python
assert Agent(key="k", name="n", role="r").max_tool_iterations == 30
assert AgentConfig(name="n", role="r").max_tool_iterations == 30
for name in ("conf.json", "conf.example.json"):
    llm = strip_comment_keys(read_conf_file(path)).get("llm", {})
    assert llm.get("max_tool_iterations") == 30
```

셋이 어긋나면 설정이 안 먹는 것처럼 보입니다.

### 시작한 대화가 자기완결적인가

```python
async def test_changing_the_conf_file_does_not_reach_a_started_conversation(db_factory):
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    changed = _pool(critic=AgentConfig(model="anthropic/...", api_base="https://gateway.new/v1", ...))
    agents = {a.key: a for a in await _turn_agents(db_factory, sid, changed)}

    assert agents["critic"].model == "openai/gpt-4o"   # 잠글 때의 값
```

`conf.json` 을 통째로 갈아엎어도 잠긴 대화는 그때의 구성을 씁니다.

### 드래그 삽입 위치

```python
@pytest.mark.parametrize("source", ["architect", "coder", "critic"])
def test_one_drag_can_put_a_card_in_any_position(source):
    ...
    assert landed == {0, 1, 2}
```

카드 하나를 **한 번 끌어서** 어느 자리로든 보낼 수 있어야 합니다. 커서 위치를
보지 않고 늘 대상 '앞' 에 넣던 시절에는 오른쪽 이웃으로 옮기는 것이 제자리였고,
맨 뒤로 보낼 방법이 아예 없었습니다.

---

## 테스트가 잡아 온 실제 버그들

문서로서의 가치가 있는 목록입니다 — 각각이 회귀 방지 테스트로 남아 있습니다.

| 증상 | 원인 |
| :--- | :--- |
| 새로고침하면 토론이 죽음 | 코루틴이 사라진 UI 슬롯을 건드림 → 백그라운드 태스크로 분리 |
| 도구 목록이 두 번 삽입되어 설정이 깨짐 | 프롬프트의 `[검토 항목]` 을 TOML 섹션 헤더로 오독 |
| 새 에이전트가 언제나 맨 뒤 | 전략에 에이전트 키가 하드코딩됨 |
| 에이전트를 추가하면 기존 대화에서 전부 꺼져 보임 | `known_agents` 부재로 "끈 것"과 "없던 것"을 구분 못 함 |
| 라운드가 길어지면 400 | `max_context_window` 가 선언만 되고 안 읽힘 |
| 다음 대화가 이전 대화의 기억을 읽음 | 공식 memory 서버가 프로세스당 그래프 하나 |
| 드래그해도 순서가 안 바뀜 | 커서 위치를 무시하고 늘 대상 앞에 삽입 |
| 두 서버가 다른 `workspace` 를 봄 | 상대 경로를 각자의 cwd 로 해석 |

---

## 새 테스트를 쓸 때

- **네트워크를 타지 마세요.** LLM 은 `fake_llm.py`, MCP 는 fixture 서버를 씁니다
- **전역 설정을 갈아 끼우지 마세요.** `get_config(reload=True, config_path=...)`
  는 프로세스 전역을 바꿉니다. 끝나면 되돌리세요
- **`tmp_path` 를 쓰세요.** 실제 `conf.json` 을 고치는 테스트는 사본으로
- 비동기 테스트에는 `@pytest.mark.asyncio`

---

## 관련 문서

- [프로젝트 구조](02-project-layout.md) — 테스트 대상 모듈
- [핵심 기술 개관](../03-core/README.md) — 각 테스트가 지키는 원리

---

> 처음으로: [MADO 사용 설명서](../README.md)
