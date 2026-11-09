"""앱이 검증한 발언 실행 사실 — 도구 실행 내역과 직접 수신한 첨부 파일을 다른 에이전트에게 함께 전달합니다.

## 배경 및 문제점

다른 에이전트에게 전달되는 토론 기록에는 발언자의 **본문 텍스트**만 포함되어 있었습니다. 앱 시스템은 해당 발언이
어떤 도구를 호출하여 어떤 결과를 수신했는지(`DebateMessage.tool_calls`), 사용자가 @언급한 파일 내용을 해당 발언자의
요청에 직접 포함했는지(`app/agents/media.py`) 알고 있었으나, 이 실행 사실은 발언자 본인의 요청 컨텍스트에만 존재했습니다.
따라서 이를 검증해야 하는 다른 에이전트 입장에서는 발언자의 진술 외에 진위를 확인할 방법이 없었습니다.

2026-11-09 실험(deepseek-v4-flash)에서 Architect가 도구로 조회한 확인 코드와 첨부 이미지 속 텍스트를 정확하게
인용했음에도, Critic은 "원시 트레이스가 없다", "사용자 메시지에 파일 본문이 첨부되지 않았다고 명시되어 있다"며 환각으로
기각했고, Orchestrator 역시 이 판정에 따라 최종 결론을 '조회 실패'·'수신 불가'로 처리하는 문제가 발생했습니다.

## 해결 방식

* **기록 보관** — 발언이 완료되면 앱이 관측한 실행 사실을 `llm_meta["observed"]`에 기록합니다 (`observed_meta`): 실행한
  도구의 이름·인자·결과·출력 접두부 및 모델 요청에 정상 포함된 첨부 파일 목록(`media.recording_sent`).
  `llm_meta`에 저장함으로써 발언을 재실행하거나 롤백할 때 본문과 동일한 버전으로 관리됩니다
  (`speech_retry.version_entry`). 데이터베이스에 영속화되므로 다음 턴이나 서버 재기동 후에도 동일한 형식으로 렌더링되며,
  프롬프트 캐시 접두부도 일정하게 유지됩니다 (원시 도구 로그는 중단된 턴을 재개할 때만 로드합니다, `TurnRecorder`).
* **정보 전달** — 다른 에이전트에게 해당 발언을 전달할 때 본문 끝에 `[앱 기록]` 블록을 첨부합니다 (`peer_view`). 발언자
  본인의 컨텍스트에는 첨부하지 않습니다 (assistant 메시지 영역에 해당 형식이 포함되면 모델이 이를 모방하여 본문에 직접
  작성하기 시작하기 때문입니다). 이미 응답이 완료된 이전 발언은 본문을 요약하듯 기록 역시 도구 이름과 실행 결과만 간략히
  표기합니다 (`compact`) — 본문 요약으로 절약한 컨텍스트 토큰을 부가 기록으로 낭비하지 않도록 방지합니다.
* **위조 방지** — 발언 본문 및 도구 출력 내의 `[앱 기록]` 표시는 이스케이프하여 무력화합니다 (`neutralize`). 발언자가 해당
  표시를 임의로 작성하여 증빙된 사실처럼 위장하는 것을 차단합니다.
* **외부 콘텐츠 격리** — 외부 출처 도구의 출력 결과는 `<untrusted_content>` 태그로 감싸 안전하게 처리합니다 (ADR-047).

도구 권한이 있는 발언자가 도구를 전혀 호출하지 않은 경우에는 "실행한 도구: 없음"을 명시합니다. 파일을 읽었다고 주장하면서도
실제로는 읽지 않은 거짓 발언 역시 이 기록을 통해 즉시 판별됩니다.
"""

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.agents.media import MODALITY_LABELS
from app.agents.skills import LOAD_SKILL_TOOL
from app.agents.tool_loading import LOAD_TOOLS_TOOL
from app.mcp.policy import wrap_untrusted

OBSERVED_KEY = "observed"

APP_RECORD_MARK = "[앱 기록]"
APP_RECORD_HEADING = f"{APP_RECORD_MARK} 발언자가 작성한 본문이 아니라 앱이 직접 확인한 사실입니다."

# 발언 본문 및 도구 출력에 포함된 동일 표시를 감지합니다. 전각 괄호·공백·제로 너비 문자(zero-width)로 분리된 변형도 함께 처리합니다.
_GAP = r"[\s​-‍⁠﻿]*"
_MARK_RE = re.compile(rf"[\[［]{_GAP}앱{_GAP}기록{_GAP}[\]］]")
_NEUTRALIZED_MARK = "(발언자가 작성한 '앱 기록' 표시)"

# 전달할 분량만 제한합니다. 도구를 다수 호출한 발언이어도 앱 기록이 발언 본문보다 비대해지지 않도록 방지합니다.
MAX_TOOLS_SHOWN = 8
ARGS_CHARS = 160
OUTPUT_CHARS = 200

# 별도 기록 대상에서 제외할 호스트 도구 — 도구 스키마 로드 및 스킬 지침 로드 도구.
_QUIET_TOOLS = frozenset({LOAD_TOOLS_TOOL, LOAD_SKILL_TOOL})

OUTSIDE_RESULTS_SOURCE = "peer-tool-results"


def neutralize(text: str) -> str:
    """글 안의 `[앱 기록]` 표시를 무력화합니다."""
    return _MARK_RE.sub(_NEUTRALIZED_MARK, text or "")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _args_text(arguments: Any) -> str:
    try:
        return json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(arguments)


def _tool_servers(agent: Any) -> List[str]:
    """해당 발언자가 사용할 수 있는 MCP 서버 목록입니다 (단계적 사고 서버는 제외하며, `format_roster`와 동일한 규칙 적용)."""
    thinking = getattr(getattr(agent, "sequential_thinking", None), "mcp_server", None)
    return [s for s in (getattr(agent, "allowed_mcp_servers", None) or []) if s and s != thinking]


def observed_meta(
    agent: Any, tool_logs: Sequence[Mapping[str, Any]], sent: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """발언 기록(`llm_meta`)에 남길 앱의 관측 정보입니다. 기록할 항목이 없으면 None을 반환합니다.

    `tool_logs`는 해당 발언 중 실행된 도구 내역(`call_agent` 기록), `sent`는 요청에 포함되어 전달된 첨부 파일 내역
    (`media.recording_sent`)입니다. 도구 사용 권한이 없고 첨부 파일도 없었던 발언자라면 전달할 관측 사실이 없습니다.
    """
    thinking = getattr(getattr(agent, "sequential_thinking", None), "mcp_server", None)
    thinking_prefix = f"{thinking}__" if thinking else None
    calls: List[Dict[str, Any]] = []
    for log in tool_logs or ():
        name = str(log.get("tool_name") or "")
        if not name or name in _QUIET_TOOLS or (thinking_prefix and name.startswith(thinking_prefix)):
            continue
        calls.append({
            "name": name,
            "args": _clip(_args_text(log.get("arguments")), ARGS_CHARS),
            "status": str(log.get("status") or "success"),
            "output": _clip(str(log.get("output") or ""), OUTPUT_CHARS),
            **({"outside": True} if log.get("outside") else {}),
        })
    attachments = [
        {"name": str(item.get("name") or ""), "kind": str(item.get("kind") or ""),
         **({"from_tool": True} if item.get("from_tool") else {})}
        for item in sent or () if item.get("name")
    ]
    had_tools = bool(_tool_servers(agent))
    if not calls and not attachments and not had_tools:
        return None
    meta: Dict[str, Any] = {}
    if attachments:
        meta["attachments"] = attachments
    if calls or had_tools:
        meta["tools"] = calls[:MAX_TOOLS_SHOWN]
        if len(calls) > MAX_TOOLS_SHOWN:
            meta["more_tools"] = len(calls) - MAX_TOOLS_SHOWN
    return meta


def _call_line(call: Mapping[str, Any]) -> str:
    output = neutralize(str(call.get("output") or ""))
    tail = f": {output}" if output else ""
    return f"  - {call.get('name')}({call.get('args') or ''}) → {call.get('status') or 'success'}{tail}"


def _file_names(items: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(f"{neutralize(str(a['name']))} ({MODALITY_LABELS.get(a.get('kind'), '파일')})" for a in items)


def footer(llm_meta: Any, *, compact: bool = False) -> str:
    """`[앱 기록]` 블록. 기록이 없으면 빈 문자열을 반환하며, `compact=True`인 경우 도구는 이름과 결과만 한 줄로 간략히 표기합니다."""
    meta = llm_meta.get(OBSERVED_KEY) if isinstance(llm_meta, dict) else None
    if not isinstance(meta, dict):
        return ""
    lines = [APP_RECORD_HEADING]
    attachments = [a for a in meta.get("attachments") or [] if isinstance(a, dict) and a.get("name")]
    mentioned = [a for a in attachments if not a.get("from_tool")]
    returned = [a for a in attachments if a.get("from_tool")]
    if mentioned:
        # 수신자에게만 전달된 안내문도 사실로 함께 기록합니다. 모르면 "다시 읽지 않았다"는 진술을 지어낸 규칙으로 오인할 수 있습니다
        # (2026-11-09 실험).
        lines.append(
            f"- 직접 받은 첨부: {_file_names(mentioned)} — 앱이 이 발언자의 요청에 내용을 실어 보내며, "
            f"같은 파일을 도구로 다시 읽지 말라고 안내했습니다."
        )
    if returned:
        lines.append(f"- 도구가 돌려준 파일을 내용째 받음: {_file_names(returned)}")
    if "tools" in meta:
        calls = [c for c in meta.get("tools") or [] if isinstance(c, dict)]
        more = int(meta.get("more_tools") or 0)
        if not calls:
            lines.append("- 실행한 도구: 없음")
        elif compact:
            shown = ", ".join(f"{c.get('name')} → {c.get('status') or 'success'}" for c in calls)
            lines.append(f"- 실행한 도구 {len(calls) + more}건: {shown}" + (f" 외 {more}건" if more else ""))
        else:
            lines.append(f"- 실행한 도구 {len(calls) + more}건:")
            local = [_call_line(c) for c in calls if not c.get("outside")]
            outside = [_call_line(c) for c in calls if c.get("outside")]
            lines.extend(local)
            if outside:
                lines.append(wrap_untrusted("\n".join(outside), OUTSIDE_RESULTS_SOURCE))
            if more:
                lines.append(f"  - 외 {more}건")
    return "\n".join(lines) if len(lines) > 1 else ""


def peer_view(msg: Any, body: str, *, compact: bool = False) -> str:
    """다른 에이전트에게 전달할 발언 텍스트: 위조 표시를 무력화한 본문 + 앱 기록."""
    text = neutralize(body)
    record = footer(getattr(msg, "llm_meta", None), compact=compact)
    return f"{text}\n\n{record}" if record else text
