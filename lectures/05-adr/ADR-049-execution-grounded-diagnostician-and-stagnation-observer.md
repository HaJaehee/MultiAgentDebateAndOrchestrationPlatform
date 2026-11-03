# ADR-049. 실행 기반 진단(Diagnostician) 에이전트 · 구조화된 시도 이력 · 외부 정체 관찰자 기반 재설계 에스컬레이션

> [ADR 목록](README.md) · 이전: [ADR-048](ADR-048-token-and-time-mini-charts.md)

## 상태 (Status)

채택됨 · 2026-11-03 · 관련: [ADR-012](ADR-012-agent-owned-debate-order.md), [ADR-014](ADR-014-tool-failure-is-observation.md), [ADR-020](ADR-020-conversation-memory.md), [ADR-024](ADR-024-resume-interrupted-turns.md), [ADR-045](ADR-045-claude-prompt-cache-markers.md)

## 맥락 (Context)

순차 토론(`sequential_debate`)과 대립 토론(`adversarial_debate`)에서 구현 에이전트(`coder`)와 비평 에이전트(`critic`)가 라운드를 거듭하며 코드 수정과 리뷰를 반복할 때, 다음과 같은 세 가지 구조적 이유로 루프가 정체되는 문제가 발생합니다.

1. **실행으로 검증되지 않은 증상 중심 비평**: 비평 에이전트가 실제 컴파일러·테스트·샌드박스를 실행하지 않은 채 정적 독해에 의존한 의견 수준의 지적을 내놓으면, 구현 에이전트 역시 근본 원인(데이터 경계·상태 불일치·알고리즘 결함) 대신 눈앞에 지적된 증상만 임시 분기나 예외 처리로 덮는 패치를 반복합니다.
2. **시도·실패 이력의 유실**: 대화가 길어져 앞선 라운드 발언이 `## 요지`로 압축되거나([ADR-020](ADR-020-conversation-memory.md)) 컨텍스트 창 밖으로 밀리면, 이전 라운드에서 **어느 파일을 어떻게 고쳤고 어떤 실행 오류(`Traceback`, `파일:줄`)로 실패했는지**에 대한 객관적 도구 기록이 사라져 이미 실패한 접근을 다시 시도합니다.
3. **동일 모델·동일 컨텍스트 편향 공유**: 루프 내부 참여자에게 "지금 정체되었는가?"를 스스로 판단하게 하면, 참여자들이 공유하는 설득형 산문("접두사만 조금 고치면 해결됩니다")에 동조하여 구조적 결함을 인정하지 못하고 국소 패치를 이어 갑니다.

## 결정 (Decision)

### 1. 실행 기반 진단(`diagnostician`) 진영 도입 및 쓰기 차단·실행 강제

- 에이전트 진영(`DEBATE_STANCES`)에 `"diagnostician"`을 추가하고([ADR-012](ADR-012-agent-owned-debate-order.md)에 따라 전략 코드에는 특정 에이전트 키를 하드코딩하지 않음), 기본 설정 템플릿(`conf.example.json`)에 `diagnostician`(`debate_priority: 50`, `debate_stance: "diagnostician"`, `temperature: 0.1`, 별도 모델 환경변수 `${DIAGNOSTICIAN_MODEL}`)을 등록합니다.
- **권한 분리**: `tool_security.agents.diagnostician.deny`에 `write(**)`와 파일 쓰기·편집·`git` 변이 도구를 등록합니다. 진단 에이전트는 프로덕션 소스를 읽고(`filesystem`) 격리 샌드박스(`sandbox`)에서 최소 재현 스크립트와 테스트를 실행할 수 있으나, 프로덕션 파일을 직접 패치할 수 없습니다(샌드박스 코드 내부의 `open(..., "w")` 우회 쓰기도 AST 스캔으로 차단).
- **런타임 실행 증거 게이트(`SpeechRunner._enforce_diagnostic_evidence`)**: `diagnostician` 진영 발언이 실행 도구를 실제로 호출하지 않은 채 추측성 의견만 반환한 경우, 실행 도구가 제공된 환경이라면 `[실행 기반 진단 필수]` 지시로 **최대 1회 보완 실행**을 요구합니다. 재시도 후에도 실행 검증이 없거나 실행 도구가 비활성화된 환경이라면, 발언 상단에 `[실행 검증 미수행]` 경고 배너를 명시하고 `turn_meta.verified = False`로 기록하여 구현 에이전트가 미검증 의견을 확정 사실로 오인하지 않게 합니다.
- **라운드 간 바통 인계**: `SequentialDebateStrategy`는 Round 2 이상 첫 발언자에게 직전 라운드를 마감한 전문가(예: `diagnostician`)의 실행 증거를 입력으로 이어받도록 지시하여 `구현 → 비평 → 진단·재현 → 다음 라운드 근본 원인 수정` 루프를 닫습니다.

### 2. 결정적 `[시도·검증 이력]` 누적 및 프롬프트 캐시 보존

- `app/orchestration/diagnostic_loop.py`가 완료된 라운드들의 `tool_calls`와 진단 결과에서 **(a) 수정된 파일 경로와 작성자, (b) 샌드박스·테스트 실행 판정(`execution_status`), 예외 요약, 작업 공간 `파일:줄` 원인 위치, (c) 검증 여부가 표시된 진단 요지, (d) 남은 미해결 쟁점**을 LLM 호출 없이 결정적으로 추출합니다.
- 추출된 `[시도·검증 이력]` 블록은 Round 2부터 `ConversationMemory.build_context_for_agent`의 **마지막 사용자 메시지(`turn_prompt`)** 말미에만 주입합니다. 앞부분 목표 메시지(`message 0·1`)와 이전 라운드 본문은 변경하지 않으므로 [ADR-045](ADR-045-claude-prompt-cache-markers.md)의 프롬프트 캐시 접두사 불변성이 그대로 유지되며, 파일 수정·실행 검증·진단 발언이 전혀 없는 순수 개념 토론에서는 빈 문자열을 반환해 기존 프롬프트를 건드리지 않습니다.

### 3. 루프 외부 정체 관찰자(External Stagnation Observer)와 편향 격리형 재설계 에스컬레이션

- 각 라운드 종료 경계(`DebatePhaseExecutor._close_round`)에서 루프 내부 에이전트가 아닌 엔진 외부 순수 함수 `observe_stagnation`이 라운드별 활성 실패 시그니처를 비교합니다.
- **줄 번호 변화에 강건한 시그니처 정규화**: 국소 증상 패치로 인해 예외 발생 줄 번호나 메모리 주소가 바뀌더라도(`src/cache.py:42` → `src/cache.py:49`), 예외 유형·핵심 메시지와 파일명 기준으로 정규화한 시그니처(`exec:keyerror: 'stale_token'@cache.py`)를 대조합니다.
- **`N=2` 연속 동일 실패 시 재설계 에스컬레이션**: 2라운드 연속 동일 실패 시그니처가 해소되지 않으면:
  1. `turns.KIND_STAGNATION` 안내 메시지와 `EventType.STAGNATION_DETECTED` 이벤트를 기록합니다.
  2. 참여 에이전트들의 설득형 대화 원문을 **의도적으로 배제한** 편향 격리 프롬프트(`redesign_prompt` — 원래 사용자 요청, 기존 계획, 외부 관찰자가 포착한 실패 증거, `[시도·검증 이력]`, 결정 장부만 포함)로 오케스트레이터를 호출하여 구조적 재설계 계획(`turns.KIND_PLAN`)을 수립합니다.
  3. 새 계획으로 `state.plan_index`를 갱신하여 남은 라운드의 상단 고정 계획을 교체하고, 실패한 이전 계획은 `SUPERSEDED_PLAN_PLACEHOLDER`로 접습니다. `turns.KIND_STAGNATION` 기록을 기준으로 에스컬레이션 윈도우를 초기화하므로 서버 중단 후 `resume_turn`([ADR-024](ADR-024-resume-interrupted-turns.md))으로 재개하더라도 동일 라운드에서 중복 재설계가 발생하지 않습니다.

## 결과 (Consequences)

**도입 효과 (Benefits)**

- 비평가의 추상적 우려가 샌드박스 재현 코드·표준 에러 트레이스백·정확한 `파일:줄` 위치라는 실행 가능한 증거로 변환된 뒤 구현 에이전트에 전달됩니다.
- 앞선 발언 본문이 `## 요지`로 접힌 장기 라운드에서도 이미 실패한 파일 수정과 에러 위치가 `[시도·검증 이력]`으로 보존되어 동일 패치 반복이 줄어듭니다.
- 증상 패치가 2회 연속 같은 실행 오류를 낼 때 참여자들의 자기 합리화 문맥과 분리된 오케스트레이터 재설계가 개입하여 구조적 해법 전환을 유도합니다 (`tests/test_diagnostic_stagnation.py`).

**고려 사항 및 한계 (Trade-offs & Limitations)**

- `diagnostician` 에이전트를 활성화하면 라운드당 전문가 발언 1회와 샌드박스 실행 시간이 추가되며, 실행 도구를 호출하지 않았을 때 최대 1회의 재시도 LLM 호출 비용이 발생할 수 있습니다.
- 외부 관찰자의 시그니처 매칭은 실행 도구 출력(`Traceback`, `FAILED`, `exit_code`, 샌드박스 판정 상태) 및 반복 파일 수정 시 미해결 쟁점을 기반으로 동작하므로, 실행 도구 없이 자연어로만 진행되는 순수 설계 토론에서는 의도적으로 개입하지 않습니다.
