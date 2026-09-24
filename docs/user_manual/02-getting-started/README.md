# 시작하기

> 상위: [MADO 사용 설명서](../README.md) · 다음: [설치와 첫 실행](01-installation.md)

---

## 가장 빠른 길

```bash
pip install -r requirements.txt
python setup_mcp.py            # MCP 서버 준비 (인터넷 필요, 최초 1회)
cp conf.example.json conf.json # 설정 템플릿 복사
cp .env.example .env           # 엔드포인트·키 입력
python -m app.main
```

접속 URL은 애플리케이션 기동 콘솔 로그의 `Web UI: http://...` 라인에서 확인하실 수 있습니다.

---

## 미리 알아둘 것

### LLM 엔드포인트 설정이 없으면 토론이 진행되지 않습니다

MADO 시스템은 **가짜 답변을 임의로 날조하지 않습니다.** `.env` 환경 파일에 유효한 LLM 엔드포인트 URL이나 API 키를 입력하지 않으면 에이전트가 자신의 발언 차례에 통신 실패를 일으키고, 해당 위치에 "연결 실패" 카드가 기록됩니다. 웹 UI 화면은 정상 기동되고 신규 대화 세션도 생성할 수 있으나, 실제 토론은 진행되지 않습니다.

최소한으로 요구되는 환경변수 설정 예시입니다.

```dotenv
LLM_API_BASE=http://localhost:1234/v1
LLM_MODEL=openai/qwen2.5-coder-32b
LLM_API_KEY=
```

별도의 API 키가 필요 없는 로컬 추론 서버(Ollama, LM Studio, vLLM 등)를 운용하는 경우 `LLM_API_KEY` 값은 비워두셔도 무방합니다. 시스템은 `LLM_API_BASE` 주소만으로도 실제 모델 호출을 정상 시도합니다.

### MCP 도구 서버 구성은 선택 사항이나 활성화를 권장합니다

`setup_mcp.py` 스크립트를 실행하지 않더라도 메인 웹 애플리케이션은 정상 기동합니다. 다만 이 경우 에이전트가 로컬 파일을 읽고 쓰거나 코드를 실행할 수 없으므로, "이 코드는 정상 동작합니다"라는 주장을 타 전문가가 실질적으로 교차 검증하지 못합니다. 로컬 머신에 Node.js 환경이 설치되어 있지 않다면 `--skip-node` 옵션으로 해당 과정을 건너뛰고, `conf.json` 설정 파일에서 관련 도구 서버를 `"enabled": false`로 비활성화하여 운용하시기 바랍니다.

### `conf.json` 파일은 버전 관리 저장소에 포함되지 않습니다

실제 운영 환경의 내부망 엔드포인트 URL과 민감한 API 자격 증명이 저장되므로 `.gitignore`에 등록되어 있습니다. 시스템 배포를 위한 참조 템플릿은 `conf.example.json` 파일로 제공되며, 저장소를 새롭게 클론한 환경에서는 이를 복사하여 환경 설정을 시작하십시오.

---

## 이 섹션의 문서

- [설치와 첫 실행](01-installation.md) — 단계별 설치 절차, MCP 도구 서버 준비, 실행 옵션을 다룹니다.
- [conf.json 설정](02-configuration.md) — 설정 파일 세부 구조, 환경변수 치환 규칙, 엔드포인트 구성 예시를 다룹니다.

---

## 다음에 읽을 것

- 시스템의 내부 동작 원리를 파악하고자 하실 경우 → [핵심 기술 개관](../03-core/README.md)
- 에이전트 페르소나 및 역할을 즉시 구성하고자 하실 경우 → [로스터 편집](../04-workflows/03-roster-editing.md)
- 오프라인 폐쇄망 환경으로의 이전을 계획 중이실 경우 → [폐쇄망 배포](../04-workflows/05-airgap-deployment.md)

---

> 다음: [설치와 첫 실행](01-installation.md)
