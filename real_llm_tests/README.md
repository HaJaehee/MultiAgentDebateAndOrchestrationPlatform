# 실제 LLM 시험 (real LLM tests)

`tests/` 의 단위 테스트는 가짜 LLM 으로 **코드의 규칙**을 확인합니다. 이 폴더는 그와 따로, **실제 모델을 호출해**
번들에 들어 있는 코드가 실제로 그렇게 동작하는지를 확인합니다 — 모델이 형식을 지키는지, 안내를 듣고 그대로
행동하는지, 제공자가 보고한 숫자와 앱의 집계가 맞는지처럼 가짜 LLM 으로는 알 수 없는 것들입니다.

어느 모델인지는 **대상 폴더의 conf.json 과 .env 가 정합니다.** 개발 PC 에서는 DeepSeek(생성형)와 Jev(결정 LLM)이고,
폐쇄망에서는 그 설치본이 가리키는 모델입니다. 시험은 모델 이름을 가정하지 않고, 확인·관찰 문구에 실제 모델 이름을 적습니다.

돈과 시간이 들기 때문에 **켜야만 돕니다.** 저장소 루트에서 `pytest -q` 를 돌려도 이 폴더는 수집되지 않습니다.

## 돌리는 법

```bat
set MADO_REAL_LLM=1
dist\MultiAgentDebateOrchestration_bundle\python_runtime\python.exe -m pytest real_llm_tests -q -p no:cacheprovider
```

한 파일만: `... -m pytest real_llm_tests\test_10_debate_core.py -q`

저장소에서 고친 코드를 번들의 환경(파이썬 · 키 · conf)으로: 저장소 루트에서 `set MADO_REAL_LLM_CODE=%CD%` 를 더합니다.
고친 것만 다시 볼 때는 `-k` 로 그 시험만 고릅니다 — 픽스처(턴)는 고른 시험이 쓰는 것만 돕니다.

| 환경 변수 | 뜻 | 기본값 |
| :--- | :--- | :--- |
| `MADO_REAL_LLM` | `1` 이어야 시험을 수집합니다 | (꺼짐) |
| `MADO_REAL_LLM_TARGET` | 환경을 빌릴 MADO 폴더 (번들 또는 저장소 루트) — 파이썬 · node · MCP · 샌드박스 · `conf.json` · `.env` | `dist/MultiAgentDebateOrchestration_bundle` |
| `MADO_REAL_LLM_CODE` | 미러로 복사할 코드(`app/` 등)의 폴더. 저장소에서 고친 코드를 번들의 환경으로 돌려 볼 때 저장소 루트를 줍니다 | (대상과 같음) |
| `MADO_REAL_LLM_RUN_DIR` | 실행 폴더를 만들 곳 | `%TEMP%\mado-real-llm` |
| `MADO_REAL_LLM_MODEL` | 대상 conf 의 전역 모델(`llm.model`)만 바꿔 돌립니다. 같은 연결로 여러 모델을 차례로 받아 볼 때 씁니다 (연결 정보는 그대로) | (대상 conf 의 모델) |

대상을 정하지 않으면 개발 저장소의 번들(`dist/MultiAgentDebateOrchestration_bundle`)을 쓰고, 그것이 없으면 **이 폴더가
놓인 설치본 자신**을 씁니다 (아래 "폐쇄망에서 새 모델을 받아 볼 때").

파이썬은 **대상의 파이썬**(`python_runtime\python.exe`)으로 돌려야 번들의 패키지 버전(litellm · SQLAlchemy · NiceGUI ·
mcp)으로 시험합니다. 파일끼리는 서로 독립이라, 실행 폴더를 달리 주면 여러 파일을 동시에 돌려도 됩니다.

전체 한 번은 (개발 PC 의 DeepSeek 기준) 요청 약 320~360건(입력 약 3백만 토큰 중 2백만은 캐시 적중, 출력 약 55만 토큰)이고, LiteLLM 이
계산한 비용으로 약 0.5달러입니다 (`deepseek-v4-flash`, 2026-11-11 실측). 차례로 돌리면 약 1시간, 파일마다 따로 띄워
병렬로 돌리면 약 10분입니다.

**돌리기 전에 제공자 잔액을 확인하십시오.** 잔액이 떨어지면 앱은 발언 자리에 "연결 끊김" 을 정직하게 적고 턴을 끝까지
돌리므로(ADR-007) 시험이 겉으로는 멀쩡히 끝납니다. 그래서 하네스는 잔액 부족·인증 실패가 한 번이라도 있었으면 그
실행의 판정을 전부 실패로 돌리고 사유를 알립니다 (`Harness.provider_outage`).

## 폐쇄망에서 새 모델을 받아 볼 때 — 수용 시험

모델은 폐쇄망에만 있으므로, 그 모델로 MADO 가 도는지는 그곳에서 돌려 봐야 압니다. 이 폴더는 기본 소스 패키지에
들어가지 않고, 이름을 적어야 실립니다.

```bat
rem 개발 PC — 소스 패키지에 이 폴더를 함께 담습니다
python package_source.py --with-acceptance

rem 폐쇄망 — 패키지를 설치본 위에 풀고, 설치본 폴더에서
python_runtime\python.exe -m app.modelcheck
set MADO_REAL_LLM=1
python_runtime\python.exe -m pytest real_llm_tests -m acceptance -q -p no:cacheprovider
```

순서는 가벼운 것부터입니다. `app.modelcheck` 는 요청 8건으로 도구 호출·추론 분리·창 크기 같은 **기능 유무**를 보고
(ADR-054), `-m acceptance` 는 그 위에서 **토론 한 턴이 실제로 도는지**를 봅니다.

| 최소 묶음 (`-m acceptance`) | 보는 것 |
| :--- | :--- |
| `test_00_connectivity.py` | 생성형 모델과 결정 LLM 이 답하는가 (결정 LLM 연결이 없으면 그 시험은 건너뜀) |
| `test_05_modelcheck.py` | 연결마다 적합성 점검이 끝까지 가고 기록이 남는가 |
| `test_10_debate_core.py` | 순차 토론 2라운드: 발언·합성·장부·발언 번호·사용량 집계 |
| `test_40_tool_context.py` | 실제 MCP 도구로 지연 로딩과 읽기 압축, 그리고 그 실측 횟수 |

- 설치본의 `conf.json` · DB · `workspace/` 는 건드리지 않습니다 (아래 미러 루트).
- 시나리오가 이름으로 부르는 표준 에이전트(`architect` · `coder` · `critic`)가 운영 conf 에 없으면, 시험용 conf 사본에만
  `conf.example.json` 의 것을 채워 넣고 그 사실을 첫 줄과 보고서(`code.supplemented_agents`)에 적습니다. 연결은 전역
  `llm` 을 물려받습니다.
- 모델의 선택에 달린 것(도구를 불러올지, 주입된 지시를 따를지)은 실패가 아니라 `notes` 에 남습니다. 새 모델의 성향은
  `report.json` 의 `notes` 를 읽어 파악합니다.
- 결과에는 모델이 쓴 글이 들어 있습니다(`records/`, `mado.db`). 반출할 때는 `report.json` 만 가져가거나 내용을 확인하고
  가져가십시오. 내용 없이 상태만 필요하면 `data/modelcheck/*.json` 이 그 용도입니다.

## 대상 폴더를 건드리지 않는 방법 — 미러 루트

시험은 대상 폴더의 `conf.json` · `multiagent.db` · `workspace/` · `data/` 에 **아무것도 쓰지 않습니다.**

1. 실행 폴더(`<run>/root`)에 코드 출처(기본은 대상)의 `app/` · `skills/` · `trial_templates/` · `mcp_servers/` · 예제
   플러그인만 복사합니다. `PROJECT_ROOT` 가 `app/` 의 부모라서, 그 폴더가 프로젝트 루트가 되고 DB · 작업 공간 · data ·
   플러그인 폴더가 전부 실행 폴더 아래에 생깁니다. 복사한 코드가 출처와 같은지, 그리고 대상의 코드와 같은지는 파일
   해시로 확인해 보고서에 남깁니다.
2. node 런타임 · MCP 서버 · 샌드박스처럼 읽기만 하는 큰 폴더는 `run_mado.bat` 과 같은 환경 변수로 대상을 가리킵니다.
3. 대상의 `.env` 값을 빈 값까지 그대로 환경에 넣습니다 (개발 PC 의 키가 섞이지 않게). 키를 파일로 복사하지 않습니다.
4. `conf.json` 은 대상의 것을 바탕으로 사본을 만듭니다. 바꾸는 것은 `db_url`, 웹 검색 끄기, 시험용 MCP 서버 하나
   (`fixtures/webdocs_server.py`), 시험용 에이전트 넷(`rt_*`)뿐입니다. **코드는 바꾸지 않습니다.**

LLM 요청은 가로채 **기록만 하고 그대로 보냅니다** (`litellm.acompletion`, `stream_chunk_builder`, Jev 의 httpx POST).
대역(가짜 응답)은 쓰지 않습니다. 끊긴 턴을 흉내 낼 때만 도구 실행 앞뒤에서 태스크를 취소합니다 (`Harness.crash_turn`).

## 파일

| 파일 | 확인하는 결정 | 시나리오 |
| :--- | :--- | :--- |
| `test_00_connectivity.py` | (연결) · ADR-031 · ADR-042 | DeepSeek · Jev 가 답하는가, 플러그인이 읽혔는가 |
| `test_05_modelcheck.py` | ADR-054 | 대상 conf 의 연결마다 적합성 점검 — 검사가 끝까지 가고 기록이 남는가 (모델이 무엇을 통과하는지는 관찰로) |
| `test_10_debate_core.py` | ADR-031 · 041 · 043 · 045 · 048 · 051 · 052 | 순차 토론 2라운드 한 턴을 여러 각도로 |
| `test_20_revise.py` | ADR-036 · 038 (+048 · 051 · 052) | 발언 다시 실행 → 인라인 코멘트 → 장부 직접 수정 → 다음 턴 |
| `test_30_adaptive_and_subdebate.py` | ADR-034 (+043 · 051) | 적응형 그래프, 서브 토론, Jev 가 내리는 판정 (쟁점별 충돌 → 고른 쟁점의 풀림·당사자, 두 단계) |
| `test_40_tool_context.py` | ADR-039 · 040 | 도구 스키마 지연 로딩, 지난 파일 읽기 압축, 압축 몫을 알리는 사용률 고지 |
| `test_41_outside_content.py` | ADR-047 · 035 | 지시 주입 문서와 오염 승격, 첨부 · 도구가 준 그림 |
| `test_42_plugins.py` | ADR-042 | 예제 전략 · 호스트 도구 플러그인 |
| `test_50_resume.py` | ADR-046 (+048) | 끊긴 쓰기의 디스크 확인, 커널 셀 다시 실행 |
| `test_60_diagnostician.py` | ADR-049 | 실행 기반 진단, 시도 · 검증 이력, 정체 관찰자 |
| `test_70_workflow_builder.py` | ADR-050 (+053) | 대화로 그래프 + 새 에이전트 설계 → 저장 → 그 그래프로 실제 턴, 계획 발언은 읽기만 |
| `test_80_waiting_and_queue.py` | ADR-037 · 044 | 답을 기다리는 물음 알리기, 런타임 대기열 · 양보 · 보존 정리 |

화면에서만 보이는 것(ADR-032 · 033 의 그리기, 043 의 비교 화면, 048 의 막대, 052 의 묶음 · 링크)과 PostgreSQL(ADR-041),
Claude 캐시 마커(ADR-045)는 여기서 다루지 않습니다. 앞의 것은 `serve.py` 로 실행 폴더를 띄워 눈으로 보고, 뒤의 둘은
그 서버 · 그 키가 있어야 합니다.

## 결과 읽기

실행 폴더에 남는 것:

- `report.json` — 확인 하나하나(`checks`: ADR · 이름 · 통과 여부 · 근거)와 관찰(`notes`: 모델이 실제로 한 행동, 수치).
  시험이 끝날 때마다 다시 씁니다.
- `records/<시나리오>.json` — 그 시나리오에서 실제로 보낸 요청 전부(메시지 · 도구 목록 · 제공자가 보고한 사용량 · 답).
  프롬프트가 어떻게 조립됐는지 직접 읽어 볼 수 있습니다.
- `mado.db`, `workspaces/` — 시험이 만든 대화와 작업 공간. `serve.py` 로 화면에서 엽니다.

```bat
dist\MultiAgentDebateOrchestration_bundle\python_runtime\python.exe real_llm_tests\serve.py <실행 폴더> --port 8799
```

## 시험을 쓸 때

- 확인은 `mado.check(adr, 이름, 조건, 근거)` 로 적고 시험 끝에 `mado.settle()` 을 부릅니다. 하나가 실패해도 나머지를
  끝까지 보고 한꺼번에 알립니다.
- **모델의 선택에 달린 것은 실패로 만들지 않습니다.** 서브 토론을 열지, 주입된 지시를 따를지, 정체에 빠질지는 모델이
  정합니다. 그런 것은 `mado.note(adr, 글)` 로 "이번에는 이랬다" 를 남기고, 일어났을 때만 그 뒤의 규칙을 확인합니다.
- 턴은 모듈 범위 픽스처에서 한 번만 돌리고, 시험들은 그 기록을 읽습니다 (한 턴이 몇 분입니다).
- 대상 conf 의 전역 `llm.decision_llm.enabled` 가 켜져 있으면 따로 끄지 않은 에이전트는 System 1(Jev) 판정
  에이전트가 됩니다. 시험용 에이전트는 하네스가 System 2 로 고정합니다.
- 번들 conf 의 오케스트레이터는 filesystem 도구를 갖고 있어, 실제 모델이 계획 단계에서 전문가의 일을 직접 해 버리곤
  합니다. 전문가의 도구 동작을 보려는 시나리오는 `new_session(..., orchestrator_hands_off=True)` 로 그 대화의
  오케스트레이터에게서만 도구를 뗍니다.
