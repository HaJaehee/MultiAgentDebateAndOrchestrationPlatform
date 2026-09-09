# 데이터베이스와 세션 스냅샷

> 상위: [핵심 기술 개관](README.md) · 이전: [토론 전략](06-debate-strategies.md)
>
> 파일: `app/database/models.py` (132줄) · `session.py`

---

## 스키마

```text
sessions ─────┬──▶ messages ──────▶ tool_calls
              │         ▲                │
              │         └────────────────┘  (message_id, SET NULL)
              ├──▶ artifacts
              ├──▶ tool_calls
              └──▶ session_agents          (session_id + agent_key 유일)
```

전부 `cascade="all, delete-orphan"` 이라 세션을 지우면 딸린 기록이 함께
사라집니다. 모든 관계는 `lazy="selectin"` — 비동기 세션에서 지연 로딩은
`MissingGreenlet` 으로 터지므로 미리 함께 읽습니다.

### `sessions`

| 컬럼 | 타입 | 설명 |
| :--- | :--- | :--- |
| `id` | str(36) | UUID |
| `title` | str(255) | 대화 제목 |
| `strategy` | str(50) | 토론 전략 이름 |
| `max_rounds` | int | 라운드 상한 |
| `active_agents` | JSON | 켜 둔 에이전트 키 목록 |
| `known_agents` | JSON | **그때 존재하던** 에이전트 전부 |
| `custom_instructions` | Text | 이 대화의 커스텀 지침 |
| `personas_locked` | bool | 첫 메시지에 `True` |
| `workspace_dir` | Text | 이 대화의 작업 공간 (비면 기본값) |

**`known_agents` 가 왜 필요한가.** `active_agents` 는 켜 둔 것만 담는 허용
목록이라, 목록에 없는 키가 "사용자가 끈 에이전트" 인지 "그때는 없던 에이전트"
인지 구분할 수 없습니다. 그래서 `conf.json` 에 에이전트를 새로 추가하면 기존
대화에서 **전부 꺼진 것으로** 보였습니다. 그때 무엇이 있었는지를 함께 적어 두면
둘을 가릴 수 있습니다.

**`workspace_dir` 은 잠기지 않습니다.** 페르소나와 달리 토론 도중에도 바꿀 수
있어야 합니다.

### `messages`

| 컬럼 | 설명 |
| :--- | :--- |
| `sender_key` / `sender_name` / `sender_role` | 화자 (`user`, `orchestrator`, 에이전트 키) |
| `content` | 본문 |
| `round_number` | 몇 번째 라운드 (계획은 0) |
| `msg_type` | `user` / `orchestrator` / `agent` / `system` / `error` |

화자 정보를 **키가 아니라 이름까지** 저장합니다. 나중에 에이전트를 지우거나
이름을 바꿔도 기록의 화자는 그대로여야 하기 때문입니다.

### `tool_calls`

| 컬럼 | 설명 |
| :--- | :--- |
| `agent_key` | 호출한 에이전트 |
| `tool_name` | 도구 이름 |
| `arguments` | JSON 인자 |
| `output` | 실행 결과 |
| `status` | `success` / `error` |
| `message_id` | 어느 발언 중의 호출인지 (`SET NULL`) |

`message_id` 가 `SET NULL` 인 이유: 발언이 지워져도 **도구를 실행했다는 사실은
남아야** 합니다. 파일이 실제로 바뀌었기 때문입니다.

### `artifacts`

| 컬럼 | 설명 |
| :--- | :--- |
| `artifact_type` | `code` / `markdown` / `mermaid` / `json` |
| `title` | 탭 제목 |
| `content` | 본문 |
| `language` | 코드 하이라이팅용 |

### `session_agents` — 핵심

```python
class SessionAgentModel(Base):
    __table_args__ = (UniqueConstraint("session_id", "agent_key"),)

    agent_key: str
    name: str                       # 세션별 페르소나
    role: str
    system_prompt: str
    card_color: str                 # 이 대화에서의 카드 색 (빈 문자열 = 키에서 자동)
    icon_path: str                  # 이 대화에서의 아이콘 (이름 또는 이미지 경로)
    config_snapshot: Optional[Any]  # 잠글 때 굳힌 AgentConfig 전체
```

겉모습 두 값은 `config_snapshot` 안에도 들어 있지만 컬럼으로 한 번 더 둡니다.
카드를 그리는 데 반드시 필요한 값이라 JSON 을 풀지 않고 읽을 수 있어야 하고,
스냅샷이 없던 옛 대화에도 붙어야 하기 때문입니다.

`config_snapshot` 에는 **그 시점의 `AgentConfig` 전체**가 들어갑니다 — 모델,
엔드포인트, API 키, 샘플링 값, 도구 권한까지. 이것이 있어야 시작한 대화가
자기완결적입니다.

`None` 이면 이 컬럼이 생기기 전에 잠긴 대화라 살아 있는 `conf.json` 을 그대로
씁니다 (기존 DB 는 기동 시 자동 이관됩니다).

---

## 자기완결적 대화

```text
       세션 생성                첫 메시지                    그 뒤
          │                        │                          │
   ┌──────┴──────┐          ┌──────┴──────┐           ┌───────┴────────┐
   │ conf.json 을 │          │ 모든 에이전트의│           │ session_agents │
   │ 실시간으로   │  ───────▶│ AgentConfig  │  ───────▶ │ 만 읽음         │
   │ 따라감       │          │ 전체를 굳힘   │           │ conf.json 무관  │
   └─────────────┘          └─────────────┘           └────────────────┘
      🟢 편집 가능             🔒 personas_locked=True
```

### 무엇을 막는가

| `conf.json` 에 한 일 | 시작한 대화 | 아직 시작 안 한 대화 |
| :--- | :--- | :--- |
| 에이전트 삭제 | 영향 없음 | 사라짐 |
| 에이전트 비활성화 | 영향 없음 | 참여 안 함 |
| 모델·엔드포인트 변경 | 영향 없음 | 새 값 적용 |
| 도구 권한 변경 | 영향 없음 | 새 값 적용 |
| 카드 색·아이콘 변경 | 영향 없음 | 새 값 적용 |
| 에이전트 추가 | 참여 안 함 | 참여 |

이것이 없으면 **어제 끝난 대화를 오늘 다시 열었을 때 다른 시스템이 됩니다.**
기록에 남은 "System Architect" 가 지금은 다른 모델을 쓰고 다른 도구를 갖고
있는데, 그 대화를 이어서 진행하면 앞뒤가 맞지 않습니다.

### 탈출구

스냅샷이 고정이면 곤란한 경우가 있습니다 — 엔드포인트가 바뀌었거나 API 키가
만료된 대화입니다. 로스터의 **설정 갱신** 버튼이 스냅샷을 지금 `conf.json` 값으로
다시 굳힙니다. **인격은 건드리지 않으므로** 기록의 화자는 그대로입니다.

---

## 비동기 세션

```python
# app/database/session.py
engine = create_async_engine(db_url)           # sqlite+aiosqlite
get_session_factory() -> async_sessionmaker
await init_db(db_url)                          # 테이블 생성 (없으면)
```

`db_url` 이 설정값이므로 나중에 PostgreSQL 로 옮길 때 드라이버 문자열만
바꾸면 됩니다.

```json
"db_url": "postgresql+asyncpg://user:pass@host/dbname"
```

시각은 전부 **UTC 로 저장**하고(`utc_now()`), 화면에 뿌릴 때 로컬로 옮깁니다
(`export.to_local()`). 저장과 표시를 섞으면 반드시 어긋납니다.

### 나중에 생긴 컬럼

`create_all` 은 이미 있는 테이블에 컬럼을 더하지 않습니다. 그래서 기동할 때
`_ADDED_COLUMNS` 에 적힌 컬럼만 `PRAGMA table_info` 로 확인하고 없으면
`ALTER TABLE ... ADD COLUMN` 합니다 (멱등). 이미 쓰던 DB 를 그대로 들고 새
버전으로 올려도 됩니다 — `session_agents.card_color` · `icon_path` 도 이 길로
들어옵니다.

---

## 관련 문서

- [에이전트 풀과 페르소나](02-agent-pool.md) — 스냅샷을 만들고 읽는 쪽
- [세션 생애주기](../04-workflows/02-session-lifecycle.md) — 잠금 시점의 전체 흐름
- [산출물 생성과 내보내기](../04-workflows/04-artifact-and-export.md) — 기록을 문서로

---

> 다음 섹션: [워크플로우 개관](../04-workflows/README.md)
