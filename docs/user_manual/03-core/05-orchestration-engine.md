# 오케스트레이션 엔진

> 상위: [핵심 기술 개관](README.md) · 이전: [MCP 호스트](04-mcp-host.md) · 다음: [토론 전략](06-debate-strategies.md)
>
> 파일: `app/orchestration/engine.py` (1,492줄) · `runner.py` (386줄) · `state.py` · `control.py`
>
> 단계별 흐름은 [토론 한 턴의 생애주기](../04-workflows/01-debate-turn.md)

---

## 두 개의 객체

| | `OrchestratorEngine` | `DebateRunner` |
| :--- | :--- | :--- |
| 하는 일 | 토론 한 턴을 처음부터 끝까지 실행 | 그 실행을 브라우저에서 떼어 냄 |
| 아는 것 | 에이전트, LLM, MCP, DB | 엔진, asyncio |
| 모르는 것 | UI, 구독자 | UI, 에이전트 내부 |

**둘 다 UI 를 모릅니다.** 이것이 설계의 핵심입니다.

---

## 왜 백그라운드 태스크인가

예전에는 `engine.run_turn()` 이 채팅 입력 핸들러 안에서 그대로 `await` 되었습니다.
페이지를 새로고침하거나 페르소나 화면에 다녀오면:

- NiceGUI 가 그 클라이언트를 지우고, 코루틴이 붙잡고 있던 슬롯의 부모 엘리먼트가
  사라집니다. 이어지는 UI 갱신이 `The parent element this slot belongs to has been
  deleted.` 로 터졌고, **그 예외가 토론 자체를 중단**시켰습니다
- 살아남더라도 진행 상황을 다시 볼 방법이 없었습니다

```text
DebateRunner
  │
  ├── asyncio.Task (세션당 하나)
  │      └── engine.run_turn(on_event=...)
  │
  └── TurnRun  ← 이벤트가 두 갈래로
        │
        ├── 정본 스냅샷    지금까지의 발언·진행 상태
        │                 새로 붙는 화면은 이걸 그대로 그림
        │
        └── 구독 큐        화면당 하나. 화면이 죽으면 큐만 버림
```

엔진 태스크가 NiceGUI 엘리먼트를 건드릴 일이 없으니 클라이언트가 사라져도
터질 곳이 없습니다. 큐 상한은 2,000 — 브라우저 하나가 느려도 토론을 붙잡지
않되 무한히 쌓이지도 않습니다. 넘치면 그 화면은 스냅샷으로 다시 맞춥니다.

---

## 상태 머신

```text
    idle
      │  run_turn()
      ▼
  ┌─────────┐
  │planning │  오케스트레이터가 목표 분해 · 계획 수립
  └────┬────┘
       ▼
  ┌─────────┐◀────────────────┐
  │debating │                 │  round < max_rounds
  └────┬────┘─────────────────┘  and not stop_requested
       │
       │  라운드 소진 · 사용자 정지 · 합의
       ▼
  ┌────────────┐
  │synthesizing│  전체를 종합 → 아티팩트 추출
  └────┬───────┘
       ▼
  ┌─────────┐        ┌───────┐
  │completed│        │ error │
  └─────────┘        └───────┘
```

`DebateState`(Pydantic 모델)가 이 전 과정을 들고 다닙니다.

| 필드 | 뜻 |
| :--- | :--- |
| `messages` | 발언 기록 (이전 턴 포함) |
| `tool_records` | 도구 실행 기록 |
| `artifacts` | 합성된 산출물 |
| `current_round` / `max_rounds` | 진행도 |
| `failed_agent_keys` | **발언하지 못한 에이전트** |
| `stopped_early` | 사용자가 남은 라운드를 건너뛰었는지 |
| `interjection_count` | 사용자 개입 발언 수 |
| `is_consensus_reached` | 실패자 없고 조기 중단도 없을 때만 `True` |

`failed_agent_keys` 와 `stopped_early` 는 **합성 프롬프트가 읽습니다.** 덜 논의된
상태를 알고 써야 "없는 의견" 을 있는 것처럼 다루지 않습니다.

---

## 라운드 루프

```text
for round_num in 1..max_rounds:
    speakers = _select_speakers(...)        ← 전략 또는 오케스트레이터

    for index, agent in enumerate(speakers):
        ├─ control 확인 ── 정지 요청? ──▶ 라운드 루프 탈출
        │              └─ 개입 메모? ──▶ 사용자 발언으로 맥락에 삽입
        │
        ├─ context = _build_context_for_agent(agent, ...)
        │      시스템 프롬프트 + 지금까지의 전사 + 전략 지침(turn_instruction)
        │
        ├─ _speak(agent, context)
        │      └─ LLMCaller.call_agent()  ← 스트리밍 + 도구 루프
        │             성공 → 발언을 DB 에 저장, 이벤트 발행
        │             실패 → 연결 끊김 알림 기록 + failed_agent_keys 에 추가
        │
        └─ 도구 실행 기록을 DB 에 저장
```

**병렬 지시 전략은 라운드째로 갈라집니다.** 발언자를 한 명씩 세우는 위 루프와 섞을
수 없어, `_run_parallel_round()` 가 라운드 전체를 대신 돕니다.

```text
for round_num in 1..max_rounds:                     ← parallel_dispatch
    ├─ 개입 반영 · 정지 확인                          (이 라운드 과업의 근거)
    ├─ _dispatch_parallel_tasks()                    오케스트레이터에게 과업 분배 요청
    │      실패 → 과업 없이 전원 동시 실행 + 물러섬 기록
    ├─ 프롬프트를 **전부 먼저** 만든다                 (동료의 답이 섞이지 않도록)
    ├─ asyncio.gather(  ... )  under Semaphore(parallel_limit)
    │      각 _speak() 는 db_lock 으로 기록 구간만 직렬화
    │      created_at 은 지시 순서로 박아 둔다
    └─ _merge_parallel_round()                       라운드 취합 (정지 요청 시 건너뜀)
```

| 왜 필요한가 | 무엇을 했는가 |
| :--- | :--- |
| `AsyncSession` 은 동시 사용을 허용하지 않음 (`IllegalStateChangeError`) | `_speak(db_lock=...)` 로 **기록 구간만** 잠금. LLM 호출은 락 밖 |
| 기록은 `created_at` 순으로 다시 읽힘 | `_speak(created_at=...)` 에 지시 순서를 박고 `state.messages` 도 같은 순서로 정렬 |
| 늦게 시작한 발언이 동료의 답을 볼 수 있음 | 라운드의 프롬프트를 gather 이전에 전부 생성 |
| 발언 사이라는 틈이 없음 | 개입·정지를 라운드 경계에서 확인 |

정지와 개입은 **발언과 발언 사이의 안전한 지점**에서만 확인합니다. 발언 도중에
끊으면 반쪽짜리 기록이 남습니다.

---

## 사람이 끼어드는 통로

`TurnControl` 은 화면과 엔진 사이의 아주 작은 우편함입니다.

```python
class TurnControl:
    def request_stop(self): ...          # 정지 요청
    def add_note(self, text): ...        # 개입 메모
    def drain_notes(self): ...           # 엔진이 꺼내 감
    async def ask_tool_budget(...): ...  # 도구 상한 확장을 사람에게 물음
    def resolve_tool_budget(...): ...    # 화면이 답함
```

| | 정지 요청 | 태스크 취소 (`cancel()`) |
| :--- | :--- | :--- |
| 진행 중인 발언 | 끝까지 받음 | 잘림 |
| 남은 라운드 | 건너뜀 | 건너뜀 |
| 최종 합성 | **실행됨** | 실행 안 됨 |
| 산출물 | 나옴 | 안 나옴 |

정지가 취소보다 나은 이유입니다 — 지금까지의 토론으로도 결과를 뽑습니다.

**개입 메모**는 다음 발언자의 맥락에 사용자 발언으로 끼어듭니다. 토론 방향을
바꾸고 싶을 때 처음부터 다시 시작할 필요가 없습니다.

### 도구 상한 확장 요청 — 방향이 반대인 통로

정지와 개입은 사람이 엔진에게 보냅니다. 이것만 반대입니다: **도구 호출 상한을 다
쓴 에이전트가 사람에게 묻고**, 답이 올 때까지 그 발언 하나가 기다립니다.

```text
LLM 도구 루프 (상한 소진)
   │  budget_arbiter(info)
   ▼
_make_budget_arbiter()  ──▶ on_event: tool_budget_exhausted ──▶ 화면 (안내 줄)
   │                                                                │
   │  control.ask_tool_budget(timeout=180s)                         │
   ▼                                                                ▼
 대기 ◀────────────── control.resolve_tool_budget(extra, id) ◀── 확장 / 마무리
   │
   ├─ extra > 0 → 상한을 늘려 그 발언을 이어 돌림
   └─ extra = 0 또는 무응답 → 도구 없이 결론만 받아 마무리
```

| | 확장 | 지금 마무리 | 무응답 (3분) |
| :--- | :--- | :--- | :--- |
| 진행 중인 발언 | 이어서 돎 | 도구 없이 결론 | 도구 없이 결론 |
| 그때까지의 글·도구 기록 | 유지 | 유지 | 유지 |
| 상한 | `+extra` (최대 130) | 그대로 | 그대로 |

`request_stop()` 은 대기 중인 물음을 **모두 "확장 없음" 으로 답합니다**. 멈추라고
한 사람에게 "도구를 더 부를까요?" 를 붙잡고 있을 이유가 없습니다.

쪽지는 id 로 구분해 여러 장을 동시에 들 수 있습니다. 병렬 지시 전략에서는 두
에이전트가 같은 순간에 상한에 닿을 수 있고, 한 장만 들고 있으면 나머지는 답할
방법이 없어 3분을 통째로 기다린 뒤에야 마무리로 갑니다.

→ 도구 루프 쪽 동작: [LLM 통합](03-llm-integration.md#도구-루프)

경계를 넘어 공유되는 상태는 이 객체 하나뿐이고, 같은 이벤트 루프 안에서만
읽고 쓰므로 **락이 필요 없습니다.**

---

## 발언자 지명 (오케스트레이터 지명 전략)

보통은 전략이 결정적으로 순서를 정하지만, `orchestrator_led` 전략에서는
오케스트레이터에게 물어 **지금 필요한 에이전트만** 부릅니다.

```python
selector = orchestrator.model_copy(update={...})   # 도구·단계적 사고를 끈 사본
```

**도구와 단계적 사고를 끈 사본으로 부릅니다.** 이건 JSON 한 줄을 받는 라우팅
호출이지 발언이 아닙니다. 도구를 붙이면 지명하려다 파일을 읽기 시작하고,
단계적 사고 프로토콜이 주입되면 `Thought 1..N` 을 쓰다가 형식을 놓칩니다.

### 실패하면 물러서되, 조용히 물러서지 않는다

```text
지명 실패 (엔드포인트 없음 / 아는 키를 하나도 못 찾음)
   │
   ├─ 우선순위 순서로 물러섬
   └─ 피드에 기록:
        [발언자 지명 실패] {이유}
        우선순위 순서로 진행합니다: 아키텍트, 엔지니어, 리뷰어
```

**조용히 다른 순서로 도는 것이 제일 나쁩니다.** 성공했을 때도 누가 왜 불렸는지,
누가 불리지 않았는지를 남깁니다.

```text
[Round 2 발언권] Senior Python Engineer → Security & Quality Critic
(이번 라운드 미지명: System Architect)

1라운드에서 아키텍처는 합의되었고, 지금은 구현과 검증이 필요합니다.
```

---

## 합성

```text
_build_synthesis_prompt(state, orchestrator)
   │  전체 전사 + 도구 실행 결과 + 실패한 에이전트 목록
   │  (컨텍스트 한도에 맞춰 자르되 지시문은 반드시 남김)
   ▼
오케스트레이터 호출
   │
   ▼
_extract_artifacts_from_synthesis()
   │  ```로 감싼 코드 블록을 파싱
   │  language 로 종류 판정: code / mermaid / json / markdown
   ▼
ArtifactModel 로 DB 저장 → 산출물 뷰어 탭
```

`is_consensus_reached` 는 **실패한 에이전트가 없고 조기 중단도 아닐 때만** `True`
입니다. 세 명 중 두 명이 침묵했는데 "합의 도달" 로 표시되면 안 됩니다.

---

## 동시 실행 제약

MCP 서버는 프로세스 전체가 공유하고 작업 공간은 기동 시점에 고정됩니다.
서로 다른 작업 공간의 토론을 동시에 돌리면 나중에 시작한 쪽이 서버를 다시
띄우면서 앞선 토론의 도구가 남의 폴더를 읽고 쓰게 됩니다.

```python
class WorkspaceConflictError(RuntimeError):
    """조용히 틀리느니 시작을 거절합니다."""
```

같은 작업 공간이면 동시 토론이 가능합니다.

---

## 관련 문서

- [토론 한 턴의 생애주기](../04-workflows/01-debate-turn.md) — 단계별 상세
- [토론 전략](06-debate-strategies.md) — 발언자와 순서를 정하는 규칙
- [LLM 통합](03-llm-integration.md) — `_speak` 이 부르는 곳
- [데이터베이스와 세션 스냅샷](07-persistence.md) — 기록이 저장되는 곳

---

> 다음: [토론 전략](06-debate-strategies.md)
