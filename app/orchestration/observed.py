"""앱이 확인한 발언 사실 — 도구 실행과 직접 받은 첨부를 다른 에이전트에게 함께 넘깁니다.

## 무엇이 문제였나

다른 에이전트에게 넘어가는 기록에는 발언의 **글**만 있었습니다. 앱은 그 발언이 어느 도구를 불러 무엇을
받았는지(`DebateMessage.tool_calls`), 유저가 @언급한 파일의 내용을 그 발언자의 요청에 직접 실었는지
(`app/agents/media.py`) 알고 있었지만, 그 사실은 발언자 자신의 요청에만 있었습니다. 검증하는 쪽에는
발언자의 말밖에 없었습니다.

2026-11-09 실험(deepseek-v4-flash)에서 Architect 가 도구로 받은 확인 코드와 첨부 이미지의 글자를 정확히
적었는데, Critic 은 "원시 트레이스가 없다", "유저 글에 파일 내용을 붙이지 않았다고 적혀 있다" 며 환각으로
기각했고 Orchestrator 도 그 판정을 따라 결론을 '조회 실패'·'받지 못함' 으로 냈습니다.

## 어떻게

* **남기기** — 발언이 끝나면 앱이 본 것을 `llm_meta["observed"]` 에 남깁니다 (`observed_meta`): 실행한 도구의
  이름·인자·결과·출력 앞부분과, 모델이 받아들인 요청에 실려 간 첨부(`media.recording_sent`).
  `llm_meta` 에 두는 것은 발언을 다시 실행하거나 되돌릴 때 글과 함께 버전을 따라가기 때문입니다
  (`speech_retry.version_entry`). DB 에 남으므로 다음 턴에도, 서버를 다시 연 뒤에도 같은 글로 그려지고
  프롬프트 캐시의 앞부분도 그대로입니다 (도구 원본은 이어 가는 턴에만 불러옵니다, `TurnRecorder`).
* **넘기기** — 다른 에이전트에게 그 발언을 넘길 때 글 끝에 `[앱 기록]` 을 붙입니다 (`peer_view`). 자기 발언에는
  붙이지 않습니다. assistant 자리에 그 모양이 있으면 모델이 흉내 내 자기 글에 적기 시작합니다. 이미 응답한
  오래된 발언은 요지만 넘기듯 기록도 도구 이름과 결과만 적습니다 (`compact`) — 원문을 요지로 줄여 아낀
  토큰을 기록이 다시 쓰지 않게 합니다.
* **꾸미지 못하게** — 발언 글과 도구 출력 안의 `[앱 기록]` 표시는 무력화합니다 (`neutralize`). 발언자가 그 표시를
  직접 적어 사실처럼 보이게 할 수 없습니다.
* **외부 내용** — 외부 출처 도구의 출력은 `<untrusted_content>` 로 감쌉니다 (ADR-047).

도구를 가진 발언자가 아무 도구도 부르지 않았으면 "실행한 도구: 없음" 을 남깁니다. 파일을 읽었다고 적었지만
읽은 적이 없는 발언도 같은 기록으로 드러납니다.
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
APP_RECORD_HEADING = f"{APP_RECORD_MARK} 발언자가 쓴 글이 아니라 앱이 확인한 사실입니다."

# 발언 글·도구 출력에 들어 있는 같은 표시. 전각 괄호·공백·제로폭 문자로 나눈 변형도 잡습니다.
_GAP = r"[\s​-‍⁠﻿]*"
_MARK_RE = re.compile(rf"[\[［]{_GAP}앱{_GAP}기록{_GAP}[\]］]")
_NEUTRALIZED_MARK = "(발언자가 적은 '앱 기록' 표시)"

# 넘길 만큼만. 도구를 많이 부른 발언도 기록이 발언 글보다 길어지지 않게 합니다.
MAX_TOOLS_SHOWN = 8
ARGS_CHARS = 160
OUTPUT_CHARS = 200

# 기록할 거리가 아닌 호스트 도구 — 도구 스키마 불러오기와 스킬 지침 불러오기.
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
    """이 발언자가 쓸 수 있던 MCP 서버 (단계적 사고 서버는 뺍니다 — `format_roster` 와 같은 규칙)."""
    thinking = getattr(getattr(agent, "sequential_thinking", None), "mcp_server", None)
    return [s for s in (getattr(agent, "allowed_mcp_servers", None) or []) if s and s != thinking]


def observed_meta(
    agent: Any, tool_logs: Sequence[Mapping[str, Any]], sent: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """발언 기록(`llm_meta`)에 남길 앱의 관측. 남길 것이 없으면 None.

    `tool_logs` 는 이 발언이 실행한 도구(`call_agent` 의 기록), `sent` 는 요청에 실려 간 첨부
    (`media.recording_sent`) 입니다. 도구가 없는 발언자이고 첨부도 없었으면 넘길 사실이 없습니다.
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
    """`[앱 기록]` 블록. 기록이 없으면 빈 문자열. `compact` 면 도구는 이름과 결과만 한 줄로 적습니다."""
    meta = llm_meta.get(OBSERVED_KEY) if isinstance(llm_meta, dict) else None
    if not isinstance(meta, dict):
        return ""
    lines = [APP_RECORD_HEADING]
    attachments = [a for a in meta.get("attachments") or [] if isinstance(a, dict) and a.get("name")]
    mentioned = [a for a in attachments if not a.get("from_tool")]
    returned = [a for a in attachments if a.get("from_tool")]
    if mentioned:
        # 받은 쪽에만 보인 안내도 사실로 적습니다. 모르면 "다시 읽지 않았다" 는 말을 지어낸 규칙으로 봅니다
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
    """다른 에이전트에게 넘길 발언 글: 표시를 무력화한 글 + 앱 기록."""
    text = neutralize(body)
    record = footer(getattr(msg, "llm_meta", None), compact=compact)
    return f"{text}\n\n{record}" if record else text
