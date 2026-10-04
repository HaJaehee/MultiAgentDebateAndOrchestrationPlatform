"""앱의 신원 — 이름, 버전, 만든 사람.

이 값들이 여러 곳(FastAPI 메타데이터, 화면 헤더, 정보 창, README)에 나옵니다.
한 곳에 두지 않으면 버전을 올릴 때 반드시 하나를 빠뜨리고, 그때부터 화면과
API 가 서로 다른 버전을 말하게 됩니다.
"""

APP_NAME = "MADO: Multi-Agent Debate & Orchestration Platform"
APP_SHORT_NAME = "MADO"
APP_TAGLINE = "MCP-enabled Autonomous Collaborative Debate & Synthesis"

# 표기는 `v0.9.0`, 값은 `0.9.0`. FastAPI 의 `version=` 은 접두사 없는 쪽을 받습니다.
APP_VERSION = "1.2"
APP_VERSION_LABEL = f"v{APP_VERSION}"

AUTHOR = "Ha, Jaehee"
AUTHOR_EMAIL = "lovesm135@naver.com"

LICENSE_NAME = "LGPL-3.0-or-later"

#: 정보 창과 README 에 그대로 들어가는 한 줄.
ABOUT_LINE = f"Author: {AUTHOR}, Email: {AUTHOR_EMAIL}, Version: {APP_VERSION_LABEL}"
