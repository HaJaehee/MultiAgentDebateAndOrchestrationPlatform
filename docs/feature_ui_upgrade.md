# 토론 피드 가독성 개선: 쟁점 고정 ID · 장부 요약 띠 · 라운드 묶음 · 인용 라벨

> 상태: 계획 확정, 검토 의견 반영(§11). P1(§12)·P2(§13)·P3(§14)·P4(§15) 완료 (2026-11-07)
> 아키텍처 결정 기록: ADR-051 (쟁점 고정 ID와 장부 개정 이력), ADR-052 (라운드 묶음과 인용 라벨)

## 1. 배경

참여 에이전트가 3인 이상이고 라운드가 2회를 초과하면 단순 시간순 피드만으로는 토론 흐름을 추적하기 어렵습니다. 코드베이스 분석 결과 다음과 같은 세 가지 한계가 확인되었습니다.

1. **결정 장부의 가시성이 낮습니다.** 로스터 패널 최하단의 접이식 아코디언(`app/ui/components/roster/ledger_panel.py`)에만 위치하여 토론 중 확인이 어렵고, 라운드를 거치며 미해결 쟁점이 어떻게 해소되었는지 변화 추이를 확인할 수 없습니다.
2. **라운드 단위 그룹화가 지원되지 않습니다.** 각 카드에 "Round N" 배지만 표시될 뿐 접기/펼치기가 개별 카드 단위로만 동작하여 피드가 과도하게 길어집니다(`app/ui/components/chat_feed/cards.py`, 단일 평면 `message_container`).
3. **발언 및 쟁점의 고유 식별자가 부재합니다.**
   - 결정 장부는 매 라운드마다 LLM이 전체를 새로 작성하므로(`context_memory.ledger_prompt`) 쟁점에 불변 고유 식별자가 없습니다. 서브 토론 등에서 쟁점을 인덱스나 본문 텍스트 일치로만 식별해야 했습니다(`decision.evaluate_sub_debate`, `routing.parse_sub_debate`).
   - `round_number`는 턴마다 1부터 다시 시작하지만 에이전트는 세션 전체 컨텍스트를 참조하므로, `R2-3` 같은 상대적 라벨은 다중 턴 환경에서 지칭 대상이 모호해집니다.

**정정:** 논의 때 "라운드별 장부 스냅샷이 이미 있다"고 했지만, 실제로는 정체 관찰자 메모(`turn_meta["ledger"]`)에만 저장됩니다. 나머지 라운드 장부는 턴 실행 중 메모리에만 있다가 사라집니다. 그래서 장부 개정 이력을 새로 저장해야 합니다.

### 1.1 확정된 결정

| 항목 | 결정 |
|---|---|
| 독자 | 실시간으로 지켜보는 사람과 끝난 뒤 검토하는 사람을 둘 다 고려 |
| 쟁점 ID | 고정 ID 를 붙이고 DB 에 저장. 저장은 **장부 개정 이력 표 하나**(`ledger_revisions`)로 하고, 쟁점 목록은 JSON 칸에 둠. 별도 쟁점 표는 두지 않음 |
| 라벨 형식 | ASCII: 발언 `[S14]`, 쟁점 `[I-3]` |
| 적용 화면 | 요약 띠·라운드 묶음·링크는 **전문가 화면만**. 초급자·체험 화면에서는 라벨이 글자로만 보임 |
| 순서 | 데이터 먼저: P1 쟁점 ID → P2 요약 띠 → P3 라운드 묶음 → P4 인용 라벨 |

### 1.2 범위에서 뺀 것 (후속 과제)
- 쟁점별 스레드 보기(논의 때의 대안 B). 이번 작업의 쟁점 ID 가 그 바탕이 됩니다.
- 아레나 보기, 논쟁 맵, 합의도 퍼센트 게이지.
- 접힌 라운드를 처음 펼칠 때 그리기(지연 렌더링).
- 요약 접기(`transcript_summary`)에서 발언 번호를 보존하는 일. 요약에 접힌 옛 발언은 번호를 잃어도 됩니다.
- 쟁점 클릭 시 팝오버. 툴팁으로 대신합니다.

---

## 2. P1 — 쟁점 고정 ID와 장부 개정 이력 (ADR-051)

### 2.1 형식과 번호 규칙
- 장부의 `## 미해결 쟁점` 항목은 `- [I-3] 내용` 형식으로 작성합니다. 나머지 4개 섹션은 기존 형식을 유지합니다.
- **식별 번호는 서버가 부여합니다.** LLM에게는 "직전 장부의 `[I-n]` 태그를 그대로 유지하고, 신규 쟁점은 번호 없이 작성하라"고 안내합니다.
- 해결된 쟁점은 목록에서 제거하며, 목록에서 누락되면 종결된 것으로 판정합니다. 과거 번호가 그대로 다시 등장하면 동일한 쟁점이 재개된 것으로 처리합니다.
- **종결되었다가 재개된 쟁점**도 옛 번호를 받게 합니다. 직전 장부에는 닫힌 쟁점이 없으므로, 직전 장부만 보면 번호 없이 다시 쓴 쟁점이 새 번호를 받습니다. 그래서 두 가지를 둡니다.
  - 번호 대조 범위를 **세션 전체**로 넓힙니다(쟁점 색인, 2.3). 정규화된 텍스트가 기존 쟁점과 일치하면 종결된 쟁점이라도 해당 번호를 재부여합니다.
  - 장부 갱신 프롬프트에 '최근에 닫힌 쟁점' 목록(최대 5개)을 번호와 함께 제공하고, 동일 안건이 다시 제기되었을 때만 해당 번호로 복원하도록 지시합니다. 문구가 일부 변경된 경우는 이 지침을 통해 번호 연속성이 유지됩니다.
- **식별 번호는 결코 재사용하지 않습니다.** 개정 이력 행마다 `issue_high`(지금까지 쓴 가장 큰 번호)를 저장합니다. 다음 번호는 `max(DB 의 issue_high, 지금 장부 글의 최대 I-n, state 의 값) + 1` 입니다.
  - 장부 글을 함께 보는 이유: `continue_session` 으로 장부 글만 넘어온 경우와, 개정 이력 저장이 실패한 경우를 덮기 위해서입니다.
  - 긴급 종료되거나 폐기된 턴의 번호는 해당 턴의 개정 이력과 함께 롤백/삭제되므로 재사용되어도 안전합니다. 장부 본문과 요약은 턴 종료 시에만 저장되므로 삭제된 번호를 참조하는 텍스트가 남지 않습니다.

### 2.2 새 모듈 `app/orchestration/ledger_history.py` (순수 함수와 기록 함수)
- 쟁점 태그 다루기:
  - `ISSUE_TAG`, `split_issue_tag(text) -> (no|None, text)`, `strip_issue_tags(text)`
  - `normalize_issue`: `diagnostic_loop._normalize_issue` 를 이리로 옮기고, `diagnostic_loop` 는 이것을 import 합니다.
- `IssueIndex`: 세션 내에 등장한 쟁점들을 추적하는 색인 구조체입니다. `{번호: 최신 텍스트}`, `{정규화된 텍스트: 최신 번호}`, `high`, 그리고 각 쟁점이 마지막으로 미해결 상태였던 개정 순번(`seq`)을 보관하여 '최근에 닫힌 쟁점'을 정렬·선별합니다.
- `assign_issue_ids(ledger, index: IssueIndex) -> (ledger, open_issues)`. `index` 를 그 자리에서 갱신합니다. 각 미해결 쟁점 항목의 번호는 다음 순서에 따라 결정됩니다.
  1. 유효한 태그가 존재하고 번호가 `high` 이하이며 현재 장부에서 아직 사용되지 않았다면 그대로 유지합니다 (재개된 쟁점 포함).
  2. 태그가 없거나 형식이 잘못되었더라도 정규화된 텍스트가 색인 내 기존 쟁점과 일치하면 해당 번호를 재사용합니다 (종결된 쟁점 포함). 단, 현재 장부에서 이미 사용된 번호라면 3번으로 넘어갑니다.
  3. 위 조건에 해당하지 않으면 `high += 1`로 신규 번호를 순차 발급합니다.
  - 건너뛸 줄: `EMPTY_ITEMS`, 그리고 `parse_ledger` 가 붙이는 생략 표시 줄 `- …(장부가 길어 뒷부분을 생략했습니다)`. 이 줄은 지금 `open_issue_count` 에도 잘못 세어지므로 함께 고칩니다.
- `summarize(ledger) -> {open: [{no, text}], decided, rejected}`: `context_memory.ledger_items` 를 재사용합니다.
- `round_deltas(revisions)`: (턴 ID, 라운드 번호)별로 해당 라운드의 최종 개정과 직전 개정을 비교하여 `결정 +n`, 종결된 쟁점, 신규 쟁점 등의 상태 변화량을 산출합니다 (`kind=retry`는 집계 제외).
- `record_revision(db, ...)`: 저장 실패 시 롤백을 수행하고 `PERSIST_FAILED` 이벤트를 발행하며, 토론 진행은 중단 없이 계속됩니다.
  - 트랜잭션은 이 저장소의 기존 방식을 그대로 따릅니다. 넣은 즉시 커밋하고, 실패하면 롤백합니다(`persist_memory`, `set_turn` 과 같습니다). 엔진의 모든 쓰기가 그 자리에서 커밋하므로, 6곳의 호출 시점에는 커밋 안 된 다른 변경이 없습니다. 따라서 롤백은 개정 행 하나만 버립니다.
  - SAVEPOINT(`begin_nested`)는 쓰지 않습니다. SQLite 드라이버(pysqlite·aiosqlite)에서 SAVEPOINT 를 제대로 쓰려면 엔진 전체의 트랜잭션 시작 방식을 바꾸는 우회가 필요합니다(SQLAlchemy 문서). 이 저장소는 SAVEPOINT 를 PostgreSQL 검색 경로(`search.py`)에서만 씁니다.
  - 행의 값은 모두 파이썬에서 정합니다(`id` 는 uuid, `created_at` 은 `utc_now()`). 커밋 뒤에 행을 다시 읽지 않고, 이벤트 내용도 넣기 전에 만든 값으로 채웁니다. 롤백은 세션의 ORM 객체를 만료시키는데, 이렇게 하면 만료된 객체를 비동기 문맥에서 읽는 일이 없습니다.
  - 이 전제는 시험이 지킵니다. 시험에서 `record_revision` 을 감싸, 부를 때마다 세션이 깨끗한지(`db.new`·`db.dirty`·`db.deleted` 가 비었는지) 단정합니다.

### 2.3 장부 갱신 경로
- `context_memory.ledger_prompt`: 쟁점 번호 규칙 두 줄과 "최근에 닫힌 쟁점" 목록(최대 5개, 한 줄 80자로 자름)을 더합니다. 목록에 드는 토큰은 `update_ledger` 의 `room` 계산에서 뺍니다.
- `ConversationMemory.update_ledger(*, db, kind, ...)` (`app/orchestration/conversation.py`):
  1. 턴에서 처음 장부를 갱신할 때 세션의 개정 이력을 한 번 읽어 `state.issue_index` 를 만듭니다. 이후로는 메모리에서 갱신합니다. 지금 장부 글의 태그도 색인에 더합니다. `continue_session` 으로 장부 글만 넘어온 경우를 위해서입니다.
     - 세션 재개 시 state가 재초기화되므로 색인도 DB에서 다시 구축합니다. 해당 턴의 이전 개정 이력까지 색인에 반영되므로 장부 본문이 롤백되더라도 번호 일관성이 유지됩니다.
  2. `parse_ledger` 다음에 `assign_issue_ids` 를 부릅니다.
  3. `state.decision_ledger` 를 설정하고, 개정 이력을 **즉시** 저장합니다.
  4. `LEDGER_UPDATED` 를 보냅니다.
- 이벤트 내용: `{reason, ledger, revision: {id, turn_id, round, kind, open, opened, closed, decided, rejected, created_at}}`.
- `kind` 를 명시해 넘기는 호출 6곳: `debate._close_round`(`round`), `graph.after_graph_step`(`step`, 호출부에서 `db` 를 넘겨받도록 고침), `sub_debate`(`subdebate`), `synthesis`(`final`), `engine` 재개(`resume`), `retry._correct_ledger`(`retry`, 재실행 대상 턴의 `turn_id` 로 기록).

### 2.4 DB
- `LedgerRevisionModel` (`ledger_revisions`, `app/database/models.py`) 칸:

  | 칸 | 타입 | 설명 |
  |---|---|---|
  | `id` | String(36) PK | 기록하는 쪽이 uuid 를 미리 정합니다 |
  | `session_id` | String(36), FK `sessions.id` `ondelete=CASCADE`, 색인 | |
  | `turn_id` | String(36), nullable, 색인 | FK 는 두지 않습니다(messages 와 같은 방식) |
  | `seq` | Integer, 기본 0 | 세션 안 순번, 정렬 키. 구현 중 추가(§12) |
  | `round_number` | Integer, 기본 0 | 그래프는 단계 번호, 턴 밖의 직접 수정은 0 |
  | `kind` | String(16) | `round`·`step`·`subdebate`·`final`·`resume`·`retry`·`edit` |
  | `reason` | String(120), 기본 "" | 이벤트의 `reason` (예: "Round 2") |
  | `ledger` | Text | 그 시점의 장부 전문 |
  | `open_issues` | JSON, 기본 `[]` | `[{"no": 3, "text": "…"}]`. 글에는 태그를 넣지 않습니다 |
  | `decided`, `rejected` | Integer, 기본 0 | **개수만** 저장합니다 |
  | `issue_high` | Integer, 기본 0 | 지금까지 쓴 가장 큰 쟁점 번호 |
  | `message_id` | String(36), nullable | 직접 수정 메모 id (`kind=edit`) |
  | `created_at` | DateTime | messages 와 같은 방식 |

  - `decided`·`rejected` 를 개수로 두는 이유: 변화량 머리줄(`결정 +1`)과 요약 띠에는 개수만 필요합니다. 나중에 툴팁에 항목을 보여 줄 때는 같은 행의 `ledger` 글에서 `ledger_items` 로 다시 꺼냅니다. 항목 목록을 JSON 으로 따로 두면 `ledger` 와 같은 내용을 두 번 저장하게 됩니다.
  - `open_issues` 는 장부 글에서도 꺼낼 수 있지만 따로 둡니다. 쟁점 색인과 닫힘·새 쟁점 비교가 장부 글을 매번 파싱하지 않고 이 칸만 읽게 하려는 것입니다(결정 1.1).
- 새 표라 `create_all` 이 만듭니다. `_ADDED_COLUMNS` 는 필요 없습니다.
- 삭제 경로: SQLite 는 FK 를 강제하지 않으므로 직접 지웁니다.
  - 세션 삭제 목록(`app/ui/components/sidebar.py`)
  - `session_ops.discard_turn` 과 긴급 종료(`turn_id` 기준)
- 분기된 대화(`session_fork.fork_session`): 메시지와 동일한 분기 시점 경계 규칙으로 복사하고 `turn_id`를 새로 발급된 ID로 매핑합니다.

### 2.5 소비자
- **`ledger_edit`**
  - `LedgerItem.issue_no` 를 더하고, `parse_sections`·`_render_item` 에 반영합니다. 편집기에는 ID 를 읽기 전용 칩으로 보여 줍니다.
  - `describe_changes` 는 미해결 쟁점을 ID 로 비교합니다(고침 / 닫음 / 넣음).
  - `save_edit` 는 편집기에서 새로 넣은 항목에 번호를 붙이고, `kind=edit` 개정을 메모 id 와 함께 기록합니다. 번호는 세션의 쟁점 색인으로 붙이므로, 옛 쟁점의 글을 그대로 다시 넣으면 옛 번호를 받습니다.
- **서브 토론**
  - `evaluate_sub_debate` 가 고른 항목에서 태그를 떼어 냅니다.
  - `parse_sub_debate` 가 `"issue_id": "I-3"` 를 받도록 합니다.
  - `subdebate_open` 의 `turn_meta` 에 `issue_no` 를 저장합니다.
  - 결론 머리줄을 `[서브 토론 결론 · I-3 · …]` 로 바꿉니다.
- **정체 관찰자** (`diagnostic_loop`): 태그가 있으면 서명에 `issue:I-n` 을 씁니다. 글 일치에 기대지 않게 됩니다.
- **수렴 판정**(`ledger_converged`)과 합의 판정은 개수만 보므로 그대로입니다.
- **체험 화면**의 장부 표시(`trial/store.py`)는 그대로 둡니다. 태그가 글자로 보입니다(결정 1.1).

---

## 3. P2 — 장부 요약 띠 (전문가 화면)

- 새 부품 `app/ui/components/chat_feed/ledger_strip.py` `LedgerStrip`.
  - import 는 `common` 과 `app.orchestration.ledger_history` 만 씁니다. `test_layering` 의 형제 import 규칙을 지킵니다.
- 위치: `ChatFeed.build_ui` 상태 칸의 `plan.build()` 다음입니다. 스크롤과 상관없이 늘 보입니다.
  - `ChatFeed(..., show_ledger_strip=False)` 로 두고, 전문가 화면만 `True` 를 넘깁니다.
- **한 줄, 고정 높이:**
  ```
  결정 5 · 기각 2 · 미해결 2  [I-3] [I-5]   추이 3 → R1 3 → R2 2 → 합성 0 ✓
  ```
  - 줄을 넘기지 않습니다(`flex-nowrap`, `overflow-hidden`, `min-w-0`). 쟁점이 생기고 닫힐 때마다 띠의 높이가 바뀌면, 스트리밍 중에 피드 스크롤 영역이 라운드마다 들썩이기 때문입니다.
  - 겹침은 생기지 않습니다. `ChatFeed` 루트는 세로 flex 이고, 상태 칸은 `flex-shrink-0`, 스크롤 영역은 `flex-grow min-h-0` 입니다(`feed.py`·`cards.build_area`). 띠는 자리를 차지할 뿐 카드를 덮지 않습니다.
  - 폭이 좁을 때 남기는 순서: 개수(항상) → 쟁점 칩(최대 4개, 칩 글은 `I-3` 만, "외 n") → 추이. 추이는 띠에 `container-type: inline-size` 를 주고 CSS 컨테이너 질의(약 560px 이하)로 숨깁니다. 화면 폭이 아니라 스플리터로 좁아진 피드 폭을 따라야 하므로 Tailwind 화면 구간은 쓰지 않습니다. 숨긴 추이는 개수 쪽 툴팁에서 볼 수 있습니다.
  - 추이는 마지막 5개 값까지만 보여 주고, 앞은 "…"으로 줄입니다.
  - 새 대화처럼 장부가 비어 있으면 띠를 숨깁니다.
  - 쟁점 칩: 툴팁에 전문과 열린 시점을 보여 줍니다. 클릭하면 결정 장부를 엽니다(아래 "장부 열기").
  - 추이: **가장 최근 턴**만 보여 줍니다. 시작값은 직전 턴의 마지막 개정입니다. 첫 턴은 직전 값이 없으므로 `R1 3 → …` 처럼 첫 라운드부터 시작합니다. 라운드 칩을 누르면 P3 에서 그 라운드 묶음으로 이동합니다.
  - 마지막 라운드는 따로 장부를 갱신하지 않으므로(`_close_round` 가 건너뜀), 마지막 값은 "합성"으로 표시합니다. `max_rounds=1` 이면 라운드 값 없이 `합성 n` 만 남습니다.
  - **합성 중:** `STATUS_CHANGED` 의 `status="synthesizing"`(`synthesis.py`)이 오면 흐린 `합성 …` 칩을 붙입니다. `kind=final` 개정이 오면 값으로 바꿉니다. 최종 장부 갱신이 실패하거나(`LEDGER_UPDATE_FAILED`) 그 값 없이 턴이 끝나면 칩을 거두고, 추이는 마지막 라운드에서 끝납니다.
  - 수렴: 열린 쟁점이 0 이면 ✓ 를 붙이고, 조기 종료로 끝났으면 "수렴(R3)"으로 표시합니다.
- **장부 열기 (`on_open_ledger`):** chat_feed 는 roster 를 import 할 수 없으므로 페이지가 콜백을 넘깁니다. 로스터는 서랍이 아니라 왼쪽 열 맨 위의 접이식 칸(`AgentRosterControl.expansion`, "⚙️ 에이전트 풀, MCP 서버, 토론 설정")입니다. 결정 장부는 그 안의 스크롤 칸(`max-h-[calc(100vh-400px)] overflow-y-auto`) 맨 아래에 있어, 로스터가 펼쳐져 있어도 대개 보이지 않습니다. 그래서 콜백은 세 가지를 합니다.
  1. 로스터 칸이 접혀 있으면 펼칩니다.
  2. 결정 장부 칸을 펼칩니다.
  3. 장부 칸이 보이게 스크롤합니다. 스크롤 명령은 4장의 `reveal` 스크립트와 같은 방식으로 DOM 갱신 뒤에 실행합니다.
- 실시간: `app/ui/expert/events.py` 의 `LEDGER_UPDATED` 처리에서 기존 로스터 갱신과 함께 `chat_feed.add_ledger_revision(event["revision"])` 를 부릅니다. 직접 수정 저장(`page.on_ledger_edited`)에서도 부릅니다.
  - 합성 중 칩: `STATUS_CHANGED`(합성 시작), `LEDGER_UPDATE_FAILED`, `TURN_COMPLETED` 와 턴 실패 처리에서 띠에 알립니다.
- 검토: `load_session_state` 에서 세션의 개정 이력을 조회해 `chat_feed.set_ledger_revisions(...)` 로 넘깁니다.
- 옛 대화처럼 개정 이력이 없으면 장부 글에서 센 개수만 보여 주고 추이는 비웁니다. 옛 데이터를 채워 넣는 코드는 만들지 않습니다.

---

## 4. P3 — 라운드 묶음 (전문가 화면)

- `CardTimeline(group_rounds=False)` 로 두고, 전문가 화면만 `True` 를 넘깁니다.
- **묶음 열쇠**는 `(turn_id, round_number)` 입니다.
  - `DebateMessage` 에 `turn_id` 를 더하고, `speech.start_event` 와 `MESSAGE_ADDED` 에 실어 보냅니다.
  - `recording` 의 `record_note`·`record_user_message` 도 `turn_id` 를 채웁니다.
  - `page.load_session_state` 가 만드는 메시지 dict 에도 `m.turn_id` 를 더합니다.
- **묶지 않는 것:** round 0, 그리고 `turn_meta.kind` 가 `opening`·`plan`·`plan_approval`·`plan_revision`·`graph_draft`·`synthesis`·`ledger_edit`·`note` 인 메시지.
- **묶음에 넣는 것:** 지명·배정·취합 메모, 서브 토론, 개입, 실패 메모, 정체·조기 종료 메모는 해당 라운드 묶음에 넣습니다.
- 그래프 전략은 "Step N" 으로 표시합니다.
- **카드를 넣을 자리:** 지금은 모든 카드가 `message_container` 에 들어갑니다. 묶음을 켜면 카드를 만들기 전에 `_container_for(turn_id, round, kind)` 로 자리를 고릅니다.
  - 묶지 않는 카드는 `message_container` 에 넣습니다.
  - 묶는 카드는 그 묶음의 본문 칸에 넣습니다. 묶음은 그 열쇠의 첫 카드(스트리밍 시작이든 완성된 발언이든)가 올 때 `message_container` 끝에 만듭니다. 그래서 묶음과 묶지 않는 카드가 도착 순서대로 놓입니다. 스트리밍 시작 이벤트에 `turn_id`·`round` 가 실려 있어야 첫 청크 전에 자리를 정할 수 있습니다.
  - **새 묶음은 늘 펼친 채로 만듭니다**(`value=True`). 따라가는 중이든 아니든 같습니다. 방금 시작한 발언이 접힌 칸 안에서 흐르는 일이 없게 하려는 것입니다.
  - 지난 묶음에 늦게 도착한 카드(라운드가 끝난 뒤 적힌 메모 등)는 끝이 아니라 그 묶음에 넣습니다. 머리줄 숫자만 바뀌고, 펼침 상태는 그대로 둡니다.
  - **스트리밍 중인 카드가 든 묶음은 접지 않습니다**(`_active_streams` 확인). 이전 라운드의 발언을 다시 실행하는 경우(재실행)도 이 규칙에 걸립니다.
- **펼침 애니메이션은 끕니다** (`ui.expansion(...).props("duration=0")`). 이유는 두 가지입니다.
  - Quasar 의 펼침은 약 300ms 동안 높이를 늘립니다. 그 사이에 스크롤하면 목표 위치가 아직 없습니다.
  - 카드 수십 장이 든 묶음의 높이를 프레임마다 바꾸면 스트리밍 중에 배치 계산이 계속 일어납니다.
- 머리줄:
  ```
  ▸ Round 2 · 발언 3 · 결정 +1 · 닫힘 I-3 · 새 I-5 · 도구 4회
  ```
  - 발언 수와 도구 수는 timeline 이 직접 셉니다.
  - 장부 변화량은 페이지가 `ledger_history.round_deltas` 로 계산해 `ChatFeed.set_round_summaries({(turn_id, round): ...})` 로 넘깁니다. timeline 은 DB 를 모르게 둡니다.
  - 마지막 라운드의 변화량은 `final` 개정 기준이며 "(최종 합성 반영)"으로 표시합니다.
- **접힘 규칙**
  - 불러올 때(검토): 끝난 턴의 묶음은 모두 접습니다. 진행 중인 턴이면 마지막 묶음만 펼칩니다. 사용자 요청 카드와 결론 카드는 묶음 밖에 있으므로 늘 보입니다.
  - 실시간: 새 라운드의 첫 카드가 오면 직전 묶음을 접습니다. 단 피드를 따라가는 중(`following`)이고, 사용자가 그 묶음을 직접 펼치지 않았을 때만입니다. 읽고 있는 글이 위로 사라지지 않게 하려는 것입니다.
    - 접으면 피드 전체 높이가 크게 줄어듭니다. 맨 아래를 보던 중이라면 브라우저가 스크롤 위치를 새 최댓값으로 당기므로 바닥에 그대로 남습니다. 그래도 접은 직후 `scroll_to_bottom()` 을 불러, `settle_scroll` 이 배치가 끝난 뒤 바닥을 한 번 더 맞추게 합니다. 접는 것은 따라가는 중일 때뿐이므로 `force` 는 쓰지 않습니다.
    - 이 스크롤 변화로 자동 추적이 풀리지는 않습니다. 추적 해제는 스크롤 위치가 아니라 휠·터치 이벤트로만 판단하기 때문입니다(`handle_manual_scroll`).
  - 묶음을 직접 펼치면 카드를 펼칠 때와 같이 자동 스크롤을 놓습니다. "카드 모두 접기"는 직접 펼친 묶음도 함께 접습니다.
  - `reveal_message` 는 카드가 든 묶음을 먼저 펼칩니다. 검색, 다시 실행, 인용 링크가 모두 이 경로를 씁니다.
  - 새로 `reveal_round(turn_id, round)` 를 두어 요약 띠의 라운드 칩이 씁니다.
- **펼친 뒤 스크롤하는 때:** NiceGUI 는 요소 갱신과 `run_javascript` 를 같은 메시지 묶음으로 보냅니다. 브라우저는 Vue 가 DOM 을 고치기 전에 스크립트를 실행합니다(`scroll_to_bottom` docstring 에 적힌 현상과 같음). 그때 접혀 있던 카드는 아직 `display: none` 이라 `scrollIntoView` 가 아무 일도 하지 않습니다.
  - 그래서 `reveal` 스크립트는 `requestAnimationFrame` 을 두 번 기다린 뒤 `scrollIntoView` 를 부릅니다. 묶음이 이미 펼쳐져 있어도 같은 길을 씁니다.
  - 애니메이션을 껐으므로(위) 두 프레임이면 카드 위치가 정해집니다.
  - `common.py` 에 `reveal_js(element_id, block)` 하나를 두고 `reveal_message`·`reveal_round`·장부 열기가 함께 씁니다.
- **CSS:** 발언 카드에 `mado-card` 클래스를 붙이고, `theme.py` 의 `.debate-timeline > .q-card` 를 `.debate-timeline .mado-card` 로 바꿉니다(content-visibility). `tests/test_feed_layout.py` 도 고칩니다.

---

## 5. P4 — 인용 라벨과 딥링크 (ADR-052)

### 5.1 엔진
- `messages.speech_no` INTEGER nullable 칸을 모델과 `_ADDED_COLUMNS["messages"]` 에 더합니다.
- `DebateMessage` 에 `speech_no` 와 `tag` 속성(`S14`)을 더합니다.
- **번호 매기기:** 에이전트와 오케스트레이터 발언에만 붙입니다. 유저 발언과 시스템 메모에는 붙이지 않습니다.
  - 턴 시작 때(새 턴, 재개, 재실행) `turns.next_speech_no(db, session_id, turn_id)` 로 시작 번호를 정합니다. 값은 `max(DB 의 messages.speech_no 최대값, 이 턴 발언 초안에 적힌 번호의 최대값, state 의 계수기) + 1` 입니다. 그 뒤로는 `state` 의 계수기만 올라갑니다.
  - 한 세션에서는 턴이 하나씩만 돌므로 경쟁은 없습니다. 위 식은 재개 때 겹침을 막습니다.
    - 발언 초안(`SpeechDraftModel.state`)에 예약 번호를 적어 둡니다. 초안에서 재개한 발언은 그 번호를 그대로 씁니다. 그래서 시작 번호는 초안의 번호보다 커야 합니다.
    - 예약만 하고 저장되지 않은 번호(초안도 없이 끊긴 발언)는 다시 쓰여도 됩니다. 그 번호는 어떤 프롬프트에도 실린 적이 없습니다. 순차 발언은 다음 화자가 시작하기 전에 저장되고, 병렬·그래프에서 같은 단계의 발언끼리는 서로를 보지 않기 때문입니다.
  - 병렬은 배정 순서대로, 그래프는 활성화 순서대로 미리 번호를 잡아 둡니다.
  - 실패한 발언도 번호를 유지합니다(빈 번호 허용). 재실행은 원래 번호를 그대로 씁니다. 갈래 대화는 번호를 그대로 복사합니다.
- **저장과 전달:** `message_rows`, `start_event`, `_publish_speech`, `recording.load_history`, `page` 의 dict 에 `speech_no` 를 넣습니다.
  - `load_history` 가 번호를 싣지 않으면 다시 연 뒤 첫 턴의 기록 머리줄이 달라집니다. 그러면 프롬프트 캐시가 깨지므로 반드시 넣습니다.
- **프롬프트 머리줄:** `[System Architect (role) · S14]:` 형식입니다.
  - 고칠 곳: `conversation.build_context_for_agent`, `context_memory.render_message`(장부·요약 입력), `synthesis` 기록(정상·대체 보고), `graph` 입력, `sub_debate` 발언·결론.
  - 라벨은 메시지 자신에게 저장된 값이므로 라운드가 바뀌어도 접두부가 같습니다. ADR-045 캐시 규칙을 지킵니다.
- **`CITE_INSTRUCTION`**: `context_memory` 의 `DIGEST_INSTRUCTION` 옆에 둡니다.
  - 내용: "다른 발언을 인용·반박할 때 `[S14]`, 쟁점은 `[I-3]` 으로 가리키고, 발언 머리줄은 직접 쓰지 마세요."
  - 넣을 곳: `conversation`(DIGEST 앞), `graph.graph_turn_instruction`, `sub_debate.speech_instruction`, `synthesis` 프롬프트.
  - "마지막이 DIGEST 로 끝난다"는 단정(`test_context_memory`)은 순서에 맞게 고칩니다.
- `session_ops.continue_session`: 넘겨받는 장부 글에서 `[S\d+]` 를 지웁니다. 새 세션에서는 발언 번호가 다시 시작하기 때문입니다.
- 내보내기(`export.py`) 머리줄에 `S14` 를 붙입니다.

### 5.2 화면 (전문가)
- `CardTimeline(link_citations=False)` 로 두고, 전문가 화면만 `True` 를 넘깁니다.
- 카드 머리의 라운드 배지 옆에 `S14` 칩을 붙입니다.
- `MathMarkdown(link_refs=True)`:
  - 렌더 전에 `[S14]`·`[I-3]` 을 `#mado-ref-…` 링크로 바꿉니다. 코드 블록과 인라인 코드 안은 건드리지 않습니다.
    - 바꾸기는 `protect_math` 의 글 구간 처리(`handle_prose`)에서 수식 자리표시자를 만든 **뒤에** 합니다. 그래서 코드(`_code_spans`)와 수식 안은 저절로 빠집니다.
  - LRU 캐시 열쇠에 이 플래그를 넣습니다.
  - 바꾸기를 마크다운 처리 전에 하므로, `[S14][S15]` 처럼 붙여 쓴 라벨이 마크다운의 참조 링크 문법과 엉키지 않습니다.
  - **패턴:** `(?<![\[\\\w])\[(S\d{1,5}|I-\d{1,5})\](?![\](])`
    - 앞이 `[`·`\`·글자이면 바꾸지 않습니다. `[[S14]]`(위키 링크), `\[S14\]`(이스케이프), `ab[S14]` 가 여기에 걸립니다.
    - 뒤가 `]`·`(` 이면 바꾸지 않습니다. 모델이 직접 쓴 링크 `[S14](https://…)` 가 여기에 걸립니다.
    - 뒤의 `[` 는 막지 않습니다. `[S14][S15]` 에서 둘 다 링크가 되어야 하기 때문입니다.
    - 뒤의 `:` 도 막지 않습니다. 줄 첫머리의 `[S14]: 동의합니다` 는 그대로 두면 markdown2 가 참조 정의로 보고 줄을 통째로 지웁니다. 링크로 바꾸면 글이 그대로 보입니다.
- 클릭 처리: `SPEECH_COMMENT_JS` 처럼 document 에 위임 리스너를 하나 두고, `emitEvent('mado_cite', {ref})` 를 서버의 `ui.on` 이 받습니다.
  - 리스너는 `a[href^="#mado-ref-"]` 를 찾으면 **먼저 `e.preventDefault()`** 를 부릅니다. 브라우저의 해시 이동(주소창 `#` 변경, 스크롤 점프)을 막고, 이동은 서버의 `reveal` 경로 하나로만 일어나게 합니다.
  - `S-n` → `_cards_by_speech_no` 에서 카드를 찾아 `reveal_message` 합니다(묶음 펼침, 강조 테두리).
  - `I-n` → 요약 띠의 칩을 강조합니다.
  - 이 대화에 없는 번호 → "이 대화에 S99 발언이 없습니다" 알림을 띄웁니다.

---

## 6. 재사용하는 기존 코드
- `context_memory.ledger_items` / `open_issue_count` / `parse_ledger` / `LEDGER_SECTIONS`
- `diagnostic_loop._normalize_issue` (`ledger_history` 로 옮김)
- `ledger_edit.parse_sections` / `render_sections` / `save_edit` (비교 후 교체 방식 유지)
- `CardTimeline.reveal_message`, `following`, `_user_expanded`, `_apply_clamp`, `scroll_to_bottom` / `settle_scroll`, `_active_streams`
- `math_markdown.protect_math` / `_code_spans` (코드·수식 구간 건너뛰기)
- `speech_actions.SPEECH_COMMENT_JS` (위임 리스너 패턴), `mention_input` (`emitEvent` 패턴)
- `session_fork._remap` / `_columns`, `session_ops.discard_turn`
- `tests/fake_llm.FakeLLMCaller` (`LEDGER_REPLY`), `conftest.ui_page`

## 7. 주요 수정 파일
- **엔진:** `app/orchestration/{conversation,context_memory,ledger_edit,diagnostic_loop,speech,recording,state,turns,engine}.py`, `app/orchestration/phases/{debate,graph,sub_debate,synthesis,parallel,retry,routing}.py`, `app/agents/decision.py`
- **DB:** `app/database/models.py`, `app/database/session.py` (`_ADDED_COLUMNS`), `app/session_fork.py`, `app/session_ops.py`, `app/ui/components/sidebar.py` (삭제 목록)
- **화면:** `app/ui/components/chat_feed/{feed,cards,common}.py`, 새 `chat_feed/ledger_strip.py`, `app/ui/math_markdown.py`, `app/ui/expert/{page,events}.py`, `app/ui/components/roster/{control,ledger_panel}.py` (장부 열기), `app/ui/theme.py` (띠의 컨테이너 질의)
- **기타:** `app/export.py`
- **새 모듈:** `app/orchestration/ledger_history.py`

## 8. 엣지 케이스
- 모델이 태그를 빠뜨리거나, 중복하거나, 지어내거나(high 보다 큰 번호), 결정 사항 칸으로 옮기는 경우: 2.2 의 규칙으로 정리합니다.
- 장부 갱신에 실패하면 개정 행도 번호 할당도 없습니다. 개정 저장만 실패하면 메모리의 장부는 ID 를 유지하고, 번호는 장부 글에서 이어 셉니다.
- 재개: 쟁점 색인을 DB 에서 다시 읽으므로 이 턴의 앞선 개정이 포함되고, `I-5` 가 유지됩니다. 발언 초안에서 재개한 발언은 초안에 적어 둔 발언 번호를 그대로 씁니다.
- 닫혔다가 다시 열린 쟁점: 태그를 되살렸거나 글이 같으면 옛 번호를 받습니다. 글을 바꿔 쓰고 태그도 없으면 새 번호를 받습니다. 이 경우는 막지 않습니다.
- 개정 저장 실패: 롤백은 개정 행만 버립니다(2.2). 다음 개정은 정상적으로 저장되고, 번호는 메모리의 색인에서 이어집니다.
- 재실행(옛 턴): `kind=retry` 는 라운드 변화량에서 빠집니다.
- 조기 수렴·정지로 같은 라운드에 개정이 둘이면 마지막 것을 씁니다.
- 그래프: 끝 노드 직전에는 개정이 없으므로 `final` 이 마지막 단계를 덮습니다.
- 턴 사이의 직접 수정은 개정 `kind=edit` 로 남고, 요약 띠에 반영됩니다. 라운드 묶음에는 들어가지 않습니다.
- 옛 대화(개정 없음, `speech_no` NULL): 라벨·변화량·추이가 비어 보입니다. 옛 데이터는 채우지 않습니다.
- 접힌 묶음 안의 글은 브라우저 찾기(Ctrl+F)에 걸리지 않습니다. 앱 검색은 `reveal_message` 로 펼칩니다.

## 9. 검증

### 9.1 자동 시험
- **새 파일** `tests/test_ledger_history.py`: 번호 붙이기 규칙(유지, 글 일치 재사용, 새 번호, 중복, 지어낸 번호, 생략 표시 줄, 다시 열기), `summarize`, `round_deltas`.
  - 다시 열기: R1 에 `[I-3]`, R2 에 닫힘, R3 에 태그 없이 같은 글로 다시 나오면 `I-3` 를 받는지. 프롬프트의 "최근에 닫힌 쟁점"에 `I-3` 가 실리는지.
  - 개정 저장 실패: 커밋이 한 번 실패해도 토론이 이어지는지, 다음 개정이 저장되는지, 메모리 장부의 ID 가 유지되는지. 시험용 래퍼로 부를 때마다 세션이 깨끗한지 단정합니다.
- **고칠 파일:**
  - P1: `test_context_memory`(개정 행·이벤트 내용, 닫힌 번호 재사용 안 함), `test_converge_early`, `test_abort_turn`·`test_turn_resume`(개정 삭제, 재개 시 번호 유지), `test_session_fork`, `test_speech_retry`(`kind=retry`), `test_ledger_edit`(ID 비교, 새 항목 번호, edit 개정), `test_sub_debate`, `test_decision_llm`, `test_diagnostic_stagnation`, `test_postgres`(FK 와 전체 삭제 목록에 새 표)
  - P2·P3: `test_feed_components`, `test_speech_cards`, `test_feed_layout`, `test_layering`
    - 띠: 줄을 넘기지 않는 클래스, 합성 중 칩의 생성과 정리(값으로 교체, 실패 시 거둠), 첫 턴의 추이, 장부 열기 콜백이 로스터와 장부 칸을 모두 펼치는지.
    - 묶음: 새 묶음은 펼친 채로 만들어지는지, 스트리밍 중인 묶음은 접히지 않는지, 접은 뒤 `scroll_to_bottom` 이 예약되는지, 묶음에 `duration=0` 이 붙는지, 접힌 묶음 안의 카드로 `reveal_message` 하면 묶음이 펼쳐지고 스크립트가 두 프레임을 기다리는지.
  - P4: `test_context_memory`(라벨, 접두부 고정), `test_prompt_cache`(라운드 간 캐시), `test_parallel_dispatch`(배정 순서 번호), `test_speech_resume`(`load_history`, 초안 번호 유지, 시작 번호가 초안 번호보다 큰지), `test_export`, `test_math_markdown`
    - 링크 패턴: 바꾸는 것 `[S1][S2]`·`[I-3]`·줄 첫머리 `[S14]: 동의`, 그대로 두는 것 `[S14](https://…)`·`[[S14]]`·`\[S14\]`·인라인 코드·코드 블록·수식 안.
- 단계마다 전체 시험을 돌립니다(SQLite 약 3분): `python -m pytest tests -q`
- PostgreSQL 전체 시험(약 21분, `-p tests.pg_redirect`)은 P1 이 끝난 뒤 한 번 돌립니다. 서버 바이너리를 새로 받아야 하면 먼저 묻습니다.

### 9.2 화면 확인 (실제 DB 를 건드리지 않음)
- conf 사본에서 `db_url` 과 작업 공간을 스크래치로 바꾸고, `get_engine` 을 먼저 부른 뒤, `FakeLLMCaller` 를 장부 응답(`[I-n]` 포함)과 함께 끼워 8799 포트로 띄웁니다.
- 3라운드 순차 토론 한 턴을 돌리며 확인할 것:
  1. 요약 띠의 추이가 라운드마다 갱신되는지
  2. 따라가는 중일 때 직전 라운드가 접히는지, 위로 스크롤해 읽는 중이면 접히지 않는지
  3. 새로고침 뒤 묶음이 접혀 있고 머리줄에 변화량이 나오는지
  4. 본문의 `[S2]` 를 누르면 접힌 묶음이 펼쳐지며 해당 카드로 이동하는지. 누른 뒤 `location.hash` 가 비어 있는지
  5. 장부 직접 수정에서 ID 칩이 보이고, 닫은 쟁점이 띠에 반영되는지
  6. 스플리터를 좁혀도 띠가 한 줄을 지키고 추이만 숨는지
  7. 로스터 칸을 접은 채 쟁점 칩을 누르면 로스터와 장부 칸이 펼쳐지고 장부가 보이는 곳까지 스크롤되는지
  8. 합성 중에 흐린 `합성 …` 칩이 보였다가 값으로 바뀌는지
- 병렬 지시와 그래프 전략에서도 한 턴씩 돌려 번호 순서와 "Step" 묶음을 확인합니다.
- 초급자·체험 화면은 바뀌지 않았는지만 확인합니다.

## 10. 진행 방식
- 단계 순서: P1(엔진·DB → 소비자) → P2 → P3 → P4. 단계마다 시험을 통과시키고 문서를 고친 뒤 커밋할지 묻습니다.
- 함께 고칠 문서:
  - `wiki/orchestration/context-memory.md` §2.2
  - `wiki/architecture/database-schema.md`
  - `wiki/ui/components.md` §1.3
  - `docs/user_manual` 의 토론 턴·결정 장부 부분
  - `lectures/05-adr/` ADR-051·052와 목록 README
  - `wiki/release-notes.md`

## 11. 검토 의견 반영 (2026-11-06)

계획 검토에서 나온 12개 의견을 코드와 대조했습니다. 반영한 곳과, 제안과 다르게 정한 이유는 다음과 같습니다.

| # | 의견 | 처리 | 반영 위치 |
|---|---|---|---|
| P1-1 | 개정 저장 실패 시 롤백이 다른 작업까지 되돌림 → SAVEPOINT | **다르게 정함.** 엔진의 모든 쓰기가 즉시 커밋하므로 호출 시점에 다른 변경이 없습니다. SQLite 드라이버는 SAVEPOINT 를 쓰려면 엔진 전체 설정을 바꿔야 합니다. 기존 방식(즉시 커밋, 실패 시 롤백)을 유지하고, 전제는 시험으로 지킵니다. 롤백 뒤 만료된 객체를 읽지 않도록 값은 파이썬에서 정합니다 | 2.2 |
| P1-2 | 닫혔다 다시 열린 쟁점이 새 번호를 받음 | 반영. 번호 대조를 세션 전체 쟁점 색인으로 넓히고, 프롬프트에 최근에 닫힌 쟁점 5개를 보여 줍니다. 재개 규칙도 이것으로 대신합니다 | 2.1·2.2·2.3 |
| P1-3 | `decided`·`rejected` 타입 미정 | 반영. Integer 개수로 정합니다. 항목은 같은 행의 `ledger` 글에서 꺼냅니다. 칸 타입을 모두 표로 적었습니다 | 2.4 |
| P2-1 | 띠가 줄을 넘기며 높이가 바뀜 | 반영하되 방향을 바꿈. 줄을 넘기지 않는 고정 높이로 하고, 좁으면 컨테이너 질의로 추이를 숨깁니다. 겹침은 flex 배치상 생기지 않습니다 | 3 |
| P2-2 | 첫 턴 시작값, 합성 중 상태 | 반영. 첫 턴은 R1 부터, 합성 중에는 흐린 `합성 …` 칩을 둡니다 | 3 |
| P2-3 | 로스터 서랍이 닫혀 있을 때 | 반영하되 사실 정정. 로스터는 서랍이 아니라 왼쪽 열 맨 위의 접이식 칸이고, 장부는 그 안 스크롤 칸 맨 아래에 있습니다. 두 칸을 펼치고 장부까지 스크롤합니다 | 3 |
| P3-1 | 새 라운드 카드를 넣을 자리 전환, 새 묶음은 펼친 채로 | 반영. 늦게 온 카드와 스트리밍 중인 묶음 규칙도 더했습니다 | 4 |
| P3-2 | 펼침 애니메이션과 스크롤 타이밍 | 반영. 애니메이션을 끄고(`duration=0`), 스크립트는 두 프레임을 기다립니다. 애니메이션만이 아니라 DOM 갱신 전에 스크립트가 도는 문제도 함께 막습니다 | 4 |
| P3-3 | 직전 묶음을 접을 때 바닥 보정 | 반영. 접은 뒤 `scroll_to_bottom()` 을 부르고, 추적이 풀리지 않는 이유를 적었습니다 | 4 |
| P4-1 | 링크 바꾸기 패턴의 격리 | 반영. 이스케이프(`\`)를 더하고, `[S14][S15]`·`[S14]:` 는 바꾸도록 정했습니다 | 5.2 |
| P4-2 | `preventDefault` | 반영 | 5.2 |
| P4-3 | 발언 번호의 재개 안전성 | 반영하되 범위를 넓힘. 발언 초안의 예약 번호까지 최대값에 넣습니다 | 5.1 |

## 12. P1 구현 기록 (2026-11-06)

계획대로 구현했고, 다음은 계획과 다르거나 계획에 없던 것입니다.

| 항목 | 내용 |
|---|---|
| `seq` 칸 | 개정 표에 세션 안 순번을 더해 정렬 키로 씁니다. 개발 PC 의 Python 3.12 에서 `datetime.now()` 가 약 15ms 마다 바뀌어(2만 번 불러 7개 값) 같은 라운드의 개정이 `created_at` 으로 섞일 수 있었습니다. `record_revision` 이 넣기 직전에 `max(seq)+1` 을 읽습니다. |
| 연결 반납 | `load_index` 가 읽은 뒤 커밋해 트랜잭션을 닫습니다. 바로 뒤의 장부 LLM 호출 동안 연결을 쥐면 `test_db_connection_hold`(ADR-041)가 실패했습니다. 호출 시점에 커밋 안 된 변경이 없으므로 아무것도 쓰지 않습니다. |
| `evaluate_sub_debate` | `app.agents` 는 `app.orchestration` 을 가져올 수 없어(계층 규칙) 태그 패턴을 `decision.py` 에 따로 둡니다. 돌려주는 값은 (두 전문가, 쟁점 글, 사유, 쟁점 번호) 입니다. `parse_sub_debate` 도 같은 모양이고, 지금 열린 번호만 받습니다. |
| `save_edit` 반환값 | `SavedEdit(note, ledger, revision)` 입니다. 새 쟁점에 번호가 붙어 저장한 장부가 화면의 글과 달라지므로, 페이지가 저장한 장부를 돌려주고 장부 칸이 그것을 보여 줍니다. |
| 다른 칸의 태그 | 미해결 쟁점이 아닌 칸의 항목 앞 `[I-n]` 은 뗍니다(결정 사항으로 옮겨 간 쟁점). |
| 서브 토론 안내·발언 지시 | 결론 머리줄뿐 아니라 여는 안내(`**쟁점:** I-3 · …`)와 두 전문가의 발언 지시에도 번호를 붙였습니다. |
| 직접 DB 를 고친 장부 | 개정 없이 장부 글만 바뀌면 그 글에만 있던 쟁점의 글은 다음 갱신이 모릅니다. 번호는 `issue_high` 로 지켜지지만 같은 글로 다시 연 쟁점을 알아보지 못합니다. 앱의 모든 경로는 개정을 남기므로 막지 않았습니다. |

시험: 새 `tests/test_ledger_history.py`(19건)와 `test_ledger_edit`·`test_sub_debate`·`test_decision_llm`·`test_diagnostic_stagnation`·`test_speech_retry`·`test_converge_early`·`test_postgres` 에 단정을 더했습니다. 재개·버린 턴·갈래 대화·ORM 삭제는 새 파일에서 확인합니다. PostgreSQL 전체 시험은 아직 돌리지 않았습니다.

## 13. P2 구현 기록 (2026-11-06)

계획대로 구현했고, 다음은 계획과 다르거나 계획에 없던 것입니다.

| 항목 | 내용 |
|---|---|
| 러너 스냅샷의 `phase` | 재연결된 UI 화면이 종합 단계임을 인지할 수 있도록 `TurnRun.snapshot()`에 `phase`를 추가했습니다. `합성 …` 칩은 이 값과 턴 ID를 기반으로 렌더링됩니다. |
| 합성 중 칩의 턴 | `STATUS_CHANGED` 에는 턴 id 가 없어 러너의 `turn_id` 를 씁니다. 라운드 개정이 없는 새 턴(한 라운드)도 직전 턴의 마지막 값 → `합성 …` 으로 보입니다. |
| 통계 레이블 클릭 지원 | 고정 번호가 없는 레거시 쟁점만 존재할 경우 칩이 없어 장부 열기 트리거가 부재하므로, 통계 레이블 클릭 시에도 장부 패널이 열리도록 처리했습니다. |
| 그래프 단계 | 추이에서 그래프 단계는 `단계 n` 입니다. `S` 는 P4 의 발언 번호와 겹칩니다. |
| `reveal_js` 의 대기 | 처음에는 두 프레임 기다리기 + 로스터 칸을 펼쳤으면 350ms 였으나, P3 에서 "붙잡힌 높이가 풀리고 자리가 두 프레임 그대로일 때까지" 로 바꿨습니다(§14). |
| 띠의 `alive` | 띠는 타임라인보다 먼저 그려져 피드의 `alive` 가 아직 거짓이므로, 버려진 화면은 `is_deleted` 로만 압니다. |
| 라운드 칩 | `on_reveal_round` 자리만 두었고 P3 에서 이었습니다(§14). |

## 14. P3 구현 기록 (2026-11-07)

계획대로 구현했고, 다음은 계획과 다르거나 계획에 없던 것입니다.

| 항목 | 내용 |
|---|---|
| 개입은 묶음을 열지 않음 | 개입은 열린 묶음에만 들어갑니다. 합성 뒤로 미룬 개입(`round = current_round + 1`)이 발언 없는 묶음을 만들었기 때문입니다. |
| 묶지 않는 기록 | 계획의 목록에 `kernel_replay`(재개 전 셀 재실행 기록)를 더했습니다. |
| 변화량 계산 자리 | 계획은 "페이지가 `round_deltas` 로 계산해 넘긴다"였지만, 띠가 개정 이력을 이미 들고 있어 `ChatFeed` 가 띠의 개정에서 계산해 `set_round_summaries` 로 넘깁니다. 타임라인이 DB 를 모르는 것은 같고, 불러올 때와 실시간이 한 길입니다. |
| 발언 재생성 시 묶음 자동 확장 | 접힌 묶음 내부의 발언을 재생성할 경우 해당 묶음을 자동으로 펼칩니다. 자동 접힘 방지 규칙만으로는 이미 접혀 있던 묶음 내부의 실시간 스트리밍이 가려지는 문제가 있었기 때문입니다. |
| `duration` | 문자열 `"0"` 이 아니라 숫자 0 으로 넘깁니다(Quasar 의 `duration` 은 Number). |
| 실패한 오류 발언 | 묶음 헤더의 발언 카운트 집계에서 제외합니다. |
| 펼친 뒤 스크롤하는 때 | 계획(4장)의 "`requestAnimationFrame` 두 번" 으로는 모자랐습니다. Quasar 의 슬라이드 전환이 애니메이션을 꺼도(`duration` 0) 본문 높이를 인라인 `height` 로 잠깐 붙잡아 두어, 그동안 묶음이 머리줄 높이로 멈춰 있습니다. 브라우저 확인에서 라운드 칩이 576px 어긋났습니다. `reveal_js` 는 이제 목표 위의 접이식 칸 어디에도 붙잡힌 높이가 없고 목표의 자리가 두 프레임 그대로일 때(최대 45프레임) 스크롤합니다. 로스터 칸의 350ms 대기도 이것으로 대신했습니다. |
| ADR-052 | 라운드 묶음과 인용 라벨을 함께 다루므로 P4 와 함께 씁니다. |

## 15. P4 구현 기록 (2026-11-07)

계획대로 구현했고, 다음은 계획과 다르거나 계획에 없던 것입니다.

| 항목 | 내용 |
|---|---|
| 초기 발언 번호 동기화 시점 | 턴 시작 시마다 번호를 매번 조회하는 대신, 턴 내에서 최초로 번호가 발급되는 시점에 DB와 1회 동기화하도록 개선했습니다(`turns.ensure_speech_counter`). 신규 턴, 재개, 재생성이 모두 동일한 경로를 거쳐 누락을 방지합니다. 조회 트랜잭션은 즉시 커밋하여 커넥션을 풀에 반환합니다(ADR-041). |
| 머리줄 | `DebateMessage.speaker_label` 하나로 모았습니다. 지명·분배 프롬프트의 짧은 발언 목록(`debate`·`parallel`·`planning`)은 범위 밖이라 그대로입니다. |
| 인용 링크의 표시 텍스트 | 렌더링되는 링크 텍스트는 `S14`이며 모노스페이스 칩 스타일로 스타일링됩니다(`theme.py`). 외곽 대괄호는 시각적 간결함을 위해 제외됩니다. |
| 종결된 쟁점 링크 클릭 시 안내 | 화면에 칩이 표시되지 않는 쟁점(이미 종결되었거나 '외 n'에 포함된 경우)을 클릭하면 토스트 알림으로 사유를 안내합니다. |
| 내보내기 | 사이드바의 저장 경로가 만드는 메시지 dict 에도 `speech_no` 를 실었습니다. |
| 시험 | 새 `tests/test_speech_citations.py`(24건). `test_markdown_offload` 의 가짜 렌더러와 `test_math_markdown` 의 소스 문자열을 새 서명에 맞췄습니다. |
