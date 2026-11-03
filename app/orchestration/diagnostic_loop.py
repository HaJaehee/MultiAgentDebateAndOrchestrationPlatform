"""실행 기반 진단 검증 · 시도 이력 누적 · 외부 정체 관찰자 (ADR-049).

비평가(critic)와 구현자(coder)가 같은 컨텍스트 안에서 의견만 주고받으면 두 가지 이유로 루프가
정체됩니다.

1. 비평 피드백이 컴파일러·테스트·샌드박스 실행으로 검증되지 않은 증상 수준의 추측에 머물고,
   구현 에이전트는 근본 원인 대신 당장 지적된 증상만 덧댑니다.
2. 라운드가 길어지며 앞선 발언이 `## 요지` 로 접히면 **무엇을 고쳤고 어떤 실행 오류로 실패했는지**
   도구 실행 기록이 사라져 이미 실패한 패치를 다시 시도합니다.

이 모듈은 LLM 이나 DB 를 직접 부르지 않는 순수 함수 계층입니다:

* **실행 증거 판별** (`is_execution_tool_name`, `executed_verification_calls`, `available_execution_tools`) —
  `diagnostician` 진영 에이전트가 실제로 샌드박스·테스트 도구를 실행했는지 검사하고, 미실행 시 재시도 프롬프트와
  미검증 경고를 만듭니다.
* **구조화된 시도·검증 이력** (`collect_round_attempts`, `build_attempt_history`) — 완료된 라운드들의 파일 쓰기
  경로, 샌드박스·테스트 실행 결과(트레이스백·판정·원인 위치), 진단 요지를 결정적으로 추출해 후속 라운드 프롬프트
  말미에 주입합니다 (앞부분 프롬프트 캐시는 건드리지 않습니다).
* **외부 정체 관찰자** (`observe_stagnation`, `redesign_prompt`) — 루프 참여자의 설득 문장 대신 도구 실행 결과와
  수정 파일·장부 미해결 쟁점만 외부에서 대조하여, `N`회 연속 동일 실패 시그니처가 반복되면 오케스트레이터 재설계
  단계로 에스컬레이션합니다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.agents.llm_tools import is_file_writing_call
from app.agents.reasoning import strip_reasoning_trace
from app.agents.tool_loading import LOAD_TOOLS_TOOL
from app.mcp.policy import EXEC, KNOWN_TOOLS, tool_outcome
from app.orchestration import context_memory as memory
from app.orchestration import turns

DIAGNOSTICIAN_STANCE = "diagnostician"
DEFAULT_STAGNATION_STREAK = 2

ATTEMPT_HISTORY_HEADING = "[시도·검증 이력]"
ATTEMPT_HISTORY_SHARE = 0.08
ATTEMPT_HISTORY_MAX_CHARS = 3200

REDESIGN_PROMPT_MARKER = "[정체 에스컬레이션 · 오케스트레이터 재설계]"

UNVERIFIED_DIAGNOSTIC_TAG = "[실행 검증 미수행]"
UNVERIFIED_DIAGNOSTIC_NOTICE = (
    f"> ⚠️ **{UNVERIFIED_DIAGNOSTIC_TAG}** 이 진단 발언에는 샌드박스·테스트·컴파일러 실행 기록이 없습니다. "
    "아래 내용은 실행으로 검증되지 않은 추론이므로, 구현 에이전트는 패치 전에 직접 재현·검증해야 합니다."
)

# 실행·검증 도구로 인정하는 단축 이름. 파일 쓰기·읽기·상태 조회 도구는 제외합니다.
_EXECUTION_TOOL_NAMES = frozenset({
    "execute_python_code",
    "run_python_file",
    "run_tests",
    "run_test",
    "pytest",
    "run_command",
    "compile",
    "typecheck",
    "lint",
    "check_syntax",
})
_EXECUTION_TOOL_PREFIXES = ("exec_", "run_test", "test_runner")
_NON_EXECUTION_TOOLS = frozenset({
    "load_tools",
    "load_skill",
    "read_skill_file",
    "reset_kernel_state",
    "list_workspace_files",
    "write_workspace_file",
    "append_workspace_file",
    "install_python_packages",
    "sequentialthinking",
    "ask_user",
})

_SANDBOX_STATUS_RE = re.compile(r"^execution_status:\s*([A-Z_]+)", re.MULTILINE)
_FAILED_SANDBOX_VERDICTS = frozenset({"FAILED", "TIMEOUT", "RAN_WITH_ERRORS"})
_TRACEBACK_HEAD_RE = re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE)
_TRACEBACK_LOC_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?')
_COLON_LOC_RE = re.compile(r"\b([\w./\\-]+\.(?:py|ts|tsx|js|go|rs|java|c|cpp)):(\d+)\b")
_EXCEPTION_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Failure|Fault|Timeout|Exit))\s*(?::\s*(.+))?$",
    re.MULTILINE,
)
_PYTEST_FAIL_RE = re.compile(r"^FAILED\s+(\S+)(?:\s+-\s+(.+))?$", re.MULTILINE)
_EXIT_CODE_RE = re.compile(r"\b(?:exit[_ ]?code|returncode)\s*[:=]\s*([1-9]\d*)\b", re.IGNORECASE)
_REPRO_MARKER_RE = re.compile(
    r"^\s*(?:REPRODUCED|BUG_REPRODUCED|REPRO_FAIL|재현\s*성공|재현됨)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_LINE_NUM_RE = re.compile(r"\bline\s+\d+\b", re.IGNORECASE)
_COLON_LINE_NUM_RE = re.compile(r"(\.[A-Za-z]{2,4}):\d+(?::\d+)?")
_WS_RE = re.compile(r"\s+")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ---------------------------------------------------------------------------
# 1. 실행 기반 진단 검증 (Execution-Grounded Evidence Gate)
# ---------------------------------------------------------------------------


def is_execution_tool_name(tool_name: str) -> bool:
    """샌드박스·테스트·컴파일러처럼 코드를 실제로 실행해 관찰 결과를 내는 도구인지 판별합니다."""
    raw = str(tool_name or "").strip()
    if not raw or is_file_writing_call(raw):
        return False
    short = raw.split("__", 1)[-1].lower()
    if short in _NON_EXECUTION_TOOLS:
        return False
    if short in _EXECUTION_TOOL_NAMES or short.startswith(_EXECUTION_TOOL_PREFIXES):
        return True
    known = KNOWN_TOOLS.get(short)
    return bool(known is not None and known.risk == EXEC)


def available_execution_tools(tools: Sequence[Mapping[str, Any]]) -> List[str]:
    """주어진 도구 스키마 목록에서 실행 도구 이름 목록을 추출합니다.

    지연 로딩 대기 중인 도구까지 포함하려면 `ToolShelf.reachable()`의 결과를 전달합니다.
    """
    found: List[str] = []
    for item in tools or ():
        fn = item.get("function") if isinstance(item, Mapping) else None
        if not isinstance(fn, Mapping):
            continue
        name = str(fn.get("name") or "").strip()
        if is_execution_tool_name(name) and name not in found:
            found.append(name)
    return found


def executed_verification_calls(tool_calls: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """보안 게이트에 차단되지 않고 실제로 실행된 검증 도구 호출만 추립니다."""
    out: List[Dict[str, Any]] = []
    for call in tool_calls or ():
        if not isinstance(call, Mapping):
            continue
        name = str(call.get("tool_name") or call.get("name") or "")
        if not is_execution_tool_name(name):
            continue
        sec = call.get("security")
        decision = str(sec.get("decision") or "").lower() if isinstance(sec, Mapping) else ""
        outcome = tool_outcome(str(call.get("status") or "success"), sec if isinstance(sec, Mapping) else None)
        if outcome == "blocked" or decision.startswith("den"):
            continue
        out.append(dict(call))
    return out


def diagnostic_retry_prompt(exec_tools: Sequence[str], *, to_load: Sequence[str] = ()) -> str:
    """실행 도구가 있는데도 호출 없이 의견만 낸 `diagnostician` 에이전트에게 보내는 1회 보완 지시.

    `to_load`는 이 중 아직 로드되지 않은 지연 로딩 도구 목록입니다. 프롬프트에 먼저 로드하도록 안내를 덧붙입니다.
    """
    names = ", ".join(f"`{t}`" for t in exec_tools[:4])
    pending = [t for t in to_load if t in exec_tools[:4]]
    load_hint = (
        f"이 중 {', '.join(f'`{t}`' for t in pending)}는 아직 불러오지 않았으니 "
        f"`{LOAD_TOOLS_TOOL}` 로 먼저 불러오세요. "
        if pending else ""
    )
    return (
        f"[실행 기반 진단 필수] 방금 작성한 진단에는 실제 실행 도구 호출이 없습니다. "
        f"진단(`{DIAGNOSTICIAN_STANCE}`) 에이전트는 의견 수준의 추측 대신 **실행 가능한 증거**를 산출해야 합니다.\n"
        f"지금 즉시 제공된 실행 도구({names})를 호출해 최소 재현 코드나 검증 테스트를 실제로 실행하고, "
        f"관찰된 표준 출력·트레이스백·판정 상태와 정확한 근본 원인 위치(`파일:줄`, 함수·심볼명)를 포함해 "
        f"진단 보고를 다시 작성하세요. {load_hint}프로덕션 소스 파일은 직접 수정하지 마세요."
    )


def stamp_unverified_diagnostic(content: str) -> str:
    """실행 검증 없이 끝난 진단 발언 상단에 미검증 경고를 부착합니다."""
    body = (content or "").strip()
    if UNVERIFIED_DIAGNOSTIC_TAG in body:
        return content
    if not body:
        return UNVERIFIED_DIAGNOSTIC_NOTICE
    return f"{UNVERIFIED_DIAGNOSTIC_NOTICE}\n\n{body}"


# ---------------------------------------------------------------------------
# 2. 실행 관찰 결과 파싱 및 실패 시그니처 정규화
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionObservation:
    """단일 실행 도구 호출에서 추출한 객관적 검증 결과."""

    tool_name: str
    verdict: str
    failed: bool
    error_line: str = ""
    locations: Tuple[str, ...] = ()
    signature: str = ""
    summary: str = ""


def _clean_path_label(raw_path: str) -> str:
    cleaned = raw_path.strip().replace("\\", "/")
    if cleaned.startswith("<") and cleaned.endswith(">"):
        return cleaned
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if "workspace" in parts:
        idx = len(parts) - 1 - parts[::-1].index("workspace")
        rel = "/".join(parts[idx + 1:])
        if rel:
            return rel
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else cleaned)


def _extract_locations(output: str) -> List[str]:
    """트레이스백 및 에러 출력에서 `파일:줄` 위치 목록을 추출합니다 (작업 공간 파일 우선)."""
    workspace_locs: List[str] = []
    synthetic_locs: List[str] = []
    for match in _TRACEBACK_LOC_RE.finditer(output):
        raw_file, line_no, func = match.group(1), match.group(2), (match.group(3) or "").strip()
        label = _clean_path_label(raw_file)
        loc = f"{label}:{line_no}" + (f" ({func})" if func and func != "<module>" else "")
        if label.startswith("<") or "/site-packages/" in raw_file.replace("\\", "/"):
            if loc not in synthetic_locs:
                synthetic_locs.append(loc)
        elif loc not in workspace_locs:
            workspace_locs.append(loc)
    for match in _COLON_LOC_RE.finditer(output):
        label = f"{_clean_path_label(match.group(1))}:{match.group(2)}"
        if label not in workspace_locs:
            workspace_locs.append(label)
    return (workspace_locs or synthetic_locs)[:3]


def _normalize_signature_piece(text: str) -> str:
    lowered = _HEX_ADDR_RE.sub("0x#", (text or "").strip().lower())
    lowered = _LINE_NUM_RE.sub("line #", lowered)
    lowered = _COLON_LINE_NUM_RE.sub(r"\1:#", lowered)
    return _WS_RE.sub(" ", lowered).strip()[:120]


def _primary_file_key(locations: Sequence[str]) -> str:
    for loc in locations:
        head = loc.split(" ", 1)[0].split(":", 1)[0]
        if head and not head.startswith("<"):
            return os.path.basename(head).lower()
    return ""


def parse_execution_call(call: Mapping[str, Any]) -> ExecutionObservation:
    """실행 도구 호출 하나를 분석해 실패 여부, 원인 위치, 정규화 시그니처를 산출합니다."""
    tool_name = str(call.get("tool_name") or call.get("name") or "")
    short_tool = tool_name.split("__", 1)[-1] or tool_name
    status = str(call.get("status") or "success")
    output = str(call.get("output") or "")

    status_match = _SANDBOX_STATUS_RE.search(output[:200])
    verdict = status_match.group(1) if status_match else ("ERROR" if status != "success" else "OK")

    locations = _extract_locations(output)
    exc_matches = list(_EXCEPTION_LINE_RE.finditer(output))
    pytest_matches = list(_PYTEST_FAIL_RE.finditer(output))
    exit_match = _EXIT_CODE_RE.search(output)
    repro_match = _REPRO_MARKER_RE.search(output)
    has_traceback = bool(_TRACEBACK_HEAD_RE.search(output))

    error_line = ""
    if exc_matches:
        last_exc = exc_matches[-1]
        exc_type, exc_detail = last_exc.group(1), (last_exc.group(2) or "").strip()
        error_line = f"{exc_type}: {exc_detail}" if exc_detail else exc_type
    elif pytest_matches:
        last_pt = pytest_matches[-1]
        target, detail = last_pt.group(1), (last_pt.group(2) or "").strip()
        error_line = f"FAILED {target}" + (f" - {detail}" if detail else "")
    elif repro_match:
        error_line = f"REPRODUCED: {repro_match.group(1).strip()}"
    elif exit_match:
        error_line = f"exit_code={exit_match.group(1)}"
    elif status != "success" or verdict in _FAILED_SANDBOX_VERDICTS:
        # stderr 또는 첫 의미 있는 에러 줄을 추출합니다.
        stderr_part = output.split("--- stderr ---", 1)[1].split("--- stdout ---", 1)[0] if "--- stderr ---" in output else output
        lines = [
            ln.strip() for ln in stderr_part.splitlines()
            if ln.strip() and not ln.startswith(("execution_status:", "active_namespace:", "next_action:", "[workspace"))
        ]
        if lines:
            error_line = lines[-1][:160]

    failed = (
        status != "success"
        or verdict in _FAILED_SANDBOX_VERDICTS
        or has_traceback
        or bool(exc_matches)
        or bool(pytest_matches)
        or bool(exit_match)
        or bool(repro_match)
    )

    signature = ""
    if failed:
        norm_err = _normalize_signature_piece(error_line or verdict)
        file_key = _primary_file_key(locations)
        signature = f"exec:{norm_err}" + (f"@{file_key}" if file_key else "")

    loc_text = f" (위치: {', '.join(locations)})" if locations else ""
    if failed:
        detail = (error_line or verdict)[:140]
        summary = f"`{short_tool}` → 실패 [{verdict}]: {detail}{loc_text}"
    else:
        # stdout 한 줄 요약
        stdout_part = output.split("--- stdout ---", 1)[1].split("--- artifacts ---", 1)[0] if "--- stdout ---" in output else output
        first_out = next(
            (
                ln.strip() for ln in stdout_part.splitlines()
                if ln.strip()
                and "(nothing was printed" not in ln
                and not ln.startswith(("execution_status:", "active_namespace:", "next_action:"))
            ),
            "",
        )
        tail = f" — {first_out[:100]}" if first_out else ""
        summary = f"`{short_tool}` → 정상 실행 [{verdict}]{tail}"

    return ExecutionObservation(
        tool_name=tool_name,
        verdict=verdict,
        failed=failed,
        error_line=error_line[:180],
        locations=tuple(locations),
        signature=signature,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# 3. 라운드별 시도·검증 이력 추출 (`[시도·검증 이력]`)
# ---------------------------------------------------------------------------


@dataclass
class RoundAttempt:
    """한 라운드에서 실제로 수행된 수정·실행·진단 기록."""

    round_number: int
    modified_files: List[str] = field(default_factory=list)
    writers: Dict[str, str] = field(default_factory=dict)
    verifications: List[Tuple[str, bool, ExecutionObservation]] = field(default_factory=list)
    diagnostic_notes: List[Tuple[str, bool, str]] = field(default_factory=list)
    active_failure_signatures: List[str] = field(default_factory=list)
    active_failure_summaries: List[str] = field(default_factory=list)
    open_issues: List[str] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        return bool(self.modified_files or self.verifications or self.diagnostic_notes)


def _is_diagnostician_msg(msg: Any, stances_by_key: Mapping[str, str]) -> bool:
    meta = _get(msg, "turn_meta")
    if isinstance(meta, Mapping) and meta.get("stance") == DIAGNOSTICIAN_STANCE:
        return True
    sender = str(_get(msg, "sender_key") or "")
    return stances_by_key.get(sender) == DIAGNOSTICIAN_STANCE


def _short_diagnostic_note(content: str) -> str:
    cleaned = strip_reasoning_trace(content or "")
    if UNVERIFIED_DIAGNOSTIC_NOTICE in cleaned:
        cleaned = cleaned.replace(UNVERIFIED_DIAGNOSTIC_NOTICE, "").strip()
    digest = memory.extract_digest(cleaned)
    source = digest or cleaned
    lines = [
        ln.strip().lstrip("-*• ").strip()
        for ln in source.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return ""
    joined = " / ".join(lines[:2])
    return joined[:200] + ("…" if len(joined) > 200 else "")


def _normalize_issue(issue: str) -> str:
    text = re.sub(r"\([^)]*\)", "", issue or "")
    text = re.sub(r"[^\w가-힣./_-]+", " ", text.lower())
    return _WS_RE.sub(" ", text).strip()[:80]


def collect_round_attempts(
    messages: Sequence[Any],
    *,
    up_to_round: Optional[int] = None,
    ledger_by_round: Optional[Mapping[int, str]] = None,
    stances_by_key: Optional[Mapping[str, str]] = None,
) -> List[RoundAttempt]:
    """턴 메시지 목록에서 라운드별 수정 파일·실행 검증·진단 결과를 결정적으로 추출합니다."""
    stances = dict(stances_by_key or {})
    ledgers = dict(ledger_by_round or {})
    by_round: Dict[int, List[Any]] = {}

    for msg in messages:
        if _get(msg, "msg_type") == "error" or not turns.in_main_record(msg):
            continue
        kind = turns.kind_of(msg)
        if kind not in (turns.KIND_SPEECH, turns.KIND_MERGE):
            continue
        r = int(_get(msg, "round_number") or 0)
        if r <= 0 or (up_to_round is not None and r > up_to_round):
            continue
        by_round.setdefault(r, []).append(msg)

    attempts: List[RoundAttempt] = []
    for r in sorted(by_round):
        attempt = RoundAttempt(round_number=r)
        diag_ran = False
        diag_failures: List[ExecutionObservation] = []
        impl_failures: List[ExecutionObservation] = []

        for msg in by_round[r]:
            speaker = str(_get(msg, "sender_name") or _get(msg, "sender_key") or "agent")
            tool_calls = _get(msg, "tool_calls") or []
            is_diag = _is_diagnostician_msg(msg, stances)

            for path in memory._written_paths(tool_calls):
                if path not in attempt.modified_files:
                    attempt.modified_files.append(path)
                attempt.writers[path] = speaker

            exec_calls = executed_verification_calls(tool_calls)
            speech_obs = [parse_execution_call(c) for c in exec_calls]
            for obs in speech_obs:
                attempt.verifications.append((speaker, is_diag, obs))

            if is_diag:
                # 검증 여부는 해당 발언 버전의 실제 도구 실행 기록으로만 판정합니다 — 재실행하더라도 기록이 왜곡되지 않습니다.
                note = _short_diagnostic_note(str(_get(msg, "content") or ""))
                if note:
                    attempt.diagnostic_notes.append((speaker, bool(exec_calls), note))
                if speech_obs:
                    diag_ran = True
                    diag_failures.extend(obs for obs in speech_obs if obs.failed)
            elif speech_obs and speech_obs[-1].failed:
                # 구현 에이전트가 발언 내 마지막 검증에서도 해결하지 못한 실행 실패만 남깁니다.
                impl_failures.append(speech_obs[-1])

        active_obs = diag_failures if diag_ran else impl_failures
        for obs in active_obs:
            if obs.signature and obs.signature not in attempt.active_failure_signatures:
                attempt.active_failure_signatures.append(obs.signature)
                attempt.active_failure_summaries.append(obs.summary)

        ledger_text = ledgers.get(r) or ""
        issues = memory.ledger_items(ledger_text, "미해결 쟁점") if ledger_text else None
        if issues:
            attempt.open_issues = list(issues)
            # 실행 도구를 쓰지 않고 같은 파일만 반복 패치하며 동일한 미해결 쟁점이 남는 정체도 포착합니다.
            if not attempt.active_failure_signatures and attempt.modified_files and not diag_ran:
                files_key = ",".join(sorted(os.path.basename(p).lower() for p in attempt.modified_files[:3]))
                for issue in issues[:3]:
                    norm = _normalize_issue(issue)
                    if norm:
                        sig = f"patch:{files_key}|issue:{norm}"
                        if sig not in attempt.active_failure_signatures:
                            attempt.active_failure_signatures.append(sig)
                            attempt.active_failure_summaries.append(
                                f"파일 수정({', '.join(attempt.modified_files[:3])}) 후 미해결 쟁점 지속: {issue}"
                            )

        attempts.append(attempt)
    return attempts


def format_attempt_history(
    attempts: Sequence[RoundAttempt], *, max_chars: int = ATTEMPT_HISTORY_MAX_CHARS,
) -> str:
    """구조화된 라운드별 시도·검증 이력 텍스트를 생성합니다. 활동이 없으면 빈 문자열."""
    active = [a for a in attempts if a.has_activity]
    if not active:
        return ""

    lines: List[str] = [
        f"{ATTEMPT_HISTORY_HEADING} 이전 라운드에서 실제로 수정한 파일과 실행·진단 검증 결과입니다.",
        "이미 실패한 증상 패치를 되풀이하지 말고, 아래 실행 증거와 원인 위치를 기준으로 근본 원인을 해결하세요.",
    ]
    for a in active:
        lines.append(f"\n- **Round {a.round_number}**")
        if a.modified_files:
            files_desc = ", ".join(f"`{p}` ({a.writers.get(p, '')})" for p in a.modified_files[:6])
            lines.append(f"  - 수정 파일: {files_desc}")
        for speaker, is_diag, obs in a.verifications[:4]:
            tag = "진단 실행" if is_diag else "실행 검증"
            lines.append(f"  - {tag} ({speaker}): {obs.summary}")
        for speaker, verified, note in a.diagnostic_notes[:2]:
            badge = "실행 검증됨" if verified else "미검증 의견"
            lines.append(f"  - 진단 요약 ({speaker} · {badge}): {note}")
        if a.open_issues:
            lines.append(f"  - 남은 쟁점: {'; '.join(a.open_issues[:3])}")

    text = "\n".join(lines)
    return memory.clip_chars(text, max_chars, "  - …(이전 시도 이력이 길어 뒷부분을 생략했습니다)")


def build_attempt_history(
    state: Any,
    *,
    stances_by_key: Optional[Mapping[str, str]] = None,
    max_chars: int = ATTEMPT_HISTORY_MAX_CHARS,
) -> str:
    """현재 라운드 이전(`1 .. current_round - 1`)의 누적 시도·검증 이력 블록을 만듭니다."""
    current_round = int(_get(state, "current_round") or 0)
    if current_round <= 1:
        return ""
    start = int(_get(state, "turn_message_start") or 0)
    messages = list(_get(state, "messages") or [])[start:]
    ledgers = round_ledgers_from_messages(
        messages,
        fallback_round=current_round - 1,
        fallback_ledger=str(_get(state, "decision_ledger") or ""),
    )
    attempts = collect_round_attempts(
        messages,
        up_to_round=current_round - 1,
        ledger_by_round=ledgers,
        stances_by_key=stances_by_key,
    )
    return format_attempt_history(attempts, max_chars=max_chars)


def round_ledgers_from_messages(
    turn_messages: Sequence[Any], *, fallback_round: int = 0, fallback_ledger: str = "",
) -> Dict[int, str]:
    """메시지 기록의 `turn_meta` 스냅샷에서 라운드별 장부를 복원합니다."""
    out: Dict[int, str] = {}
    for msg in turn_messages:
        meta = _get(msg, "turn_meta")
        if not isinstance(meta, Mapping):
            continue
        r = int(_get(msg, "round_number") or 0)
        ledger = meta.get("ledger")
        if r > 0 and isinstance(ledger, str) and ledger.strip():
            out[r] = ledger
    if fallback_round > 0 and fallback_ledger.strip() and fallback_round not in out:
        out[fallback_round] = fallback_ledger
    return out


# ---------------------------------------------------------------------------
# 4. 외부 정체 관찰자 및 재설계 에스컬레이션 (External Stagnation Observer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagnationReport:
    """외부 정체 관찰자가 감지한 연속 실패 판정 결과."""

    round_number: int
    streak: int
    rounds: Tuple[int, ...]
    signatures: Tuple[str, ...]
    summaries: Tuple[str, ...]
    modified_files: Tuple[str, ...]

    def as_meta(self) -> Dict[str, Any]:
        return {
            "round": self.round_number,
            "streak": self.streak,
            "rounds": list(self.rounds),
            "signatures": list(self.signatures),
            "summaries": list(self.summaries),
            "modified_files": list(self.modified_files),
        }

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any]) -> "StagnationReport":
        """`turns.KIND_STAGNATION` 안내 메시지의 메타데이터(`as_meta`)로부터 인스턴스를 복원합니다."""
        rounds = tuple(int(r) for r in meta.get("rounds") or ())
        return cls(
            round_number=int(meta.get("round") or 0),
            streak=int(meta.get("streak") or len(rounds)),
            rounds=rounds,
            signatures=tuple(str(x) for x in meta.get("signatures") or ()),
            summaries=tuple(str(x) for x in meta.get("summaries") or ()),
            modified_files=tuple(str(x) for x in meta.get("modified_files") or ()),
        )


def last_stagnation_round(turn_messages: Iterable[Any]) -> int:
    """이번 턴에서 마지막으로 정체 에스컬레이션이 기록된 라운드 번호 (없으면 0)."""
    last = 0
    for msg in turn_messages:
        if turns.kind_of(msg) == turns.KIND_STAGNATION:
            r = int((_get(msg, "turn_meta") or {}).get("round") or _get(msg, "round_number") or 0)
            last = max(last, r)
    return last


def pending_redesign(turn_messages: Sequence[Any], round_num: int) -> Optional[StagnationReport]:
    """`round_num`에서 정체 안내는 기록되었으나 재설계 계획 수립이 완료되지 않은 경우 해당 보고서를 반환합니다 (그 외에는 None).

    재설계 계획 작성 중 비정상 종료된 턴을 재개할 때 사용합니다. 정체 안내 메시지가 이미 존재하므로
    `observe_stagnation`은 중복 판정하지 않으며, 미완료된 계획만 다시 작성합니다. 안내 이후 정상적인
    계획(`turns.KIND_PLAN`) 메시지가 존재하면 재설계가 완료된 것으로 처리합니다.
    """
    note_at: Optional[int] = None
    for index, msg in enumerate(turn_messages):
        if turns.kind_of(msg) != turns.KIND_STAGNATION:
            continue
        meta = _get(msg, "turn_meta") or {}
        if int(meta.get("round") or _get(msg, "round_number") or 0) == round_num:
            note_at = index
    if note_at is None:
        return None
    for msg in turn_messages[note_at + 1:]:
        if turns.kind_of(msg) == turns.KIND_PLAN and _get(msg, "msg_type") != "error":
            return None
    return StagnationReport.from_meta(_get(turn_messages[note_at], "turn_meta") or {})


def observe_stagnation(
    turn_messages: Sequence[Any],
    *,
    round_num: int,
    limit: int = DEFAULT_STAGNATION_STREAK,
    ledger_by_round: Optional[Mapping[int, str]] = None,
    stances_by_key: Optional[Mapping[str, str]] = None,
) -> Optional[StagnationReport]:
    """루프 외부에서 객관적 실행·패치 실패 시그니처를 대조해 `limit`회 연속 정체 여부를 판정합니다.

    참여 에이전트의 자기 평가 문장은 읽지 않으므로 컨텍스트 편향을 공유하지 않습니다.
    직전 재설계 에스컬레이션 이후 최소 `limit` 라운드가 새로 쌓였을 때만 다시 판정합니다.
    """
    if limit <= 0 or round_num < limit:
        return None

    escalated_at = last_stagnation_round(turn_messages)
    if escalated_at >= round_num:
        return None
    window_start = escalated_at + 1
    if round_num - window_start + 1 < limit:
        return None

    ledgers = round_ledgers_from_messages(turn_messages)
    if ledger_by_round:
        ledgers.update({k: v for k, v in ledger_by_round.items() if v})

    attempts = [
        a for a in collect_round_attempts(
            turn_messages,
            up_to_round=round_num,
            ledger_by_round=ledgers,
            stances_by_key=stances_by_key,
        )
        if a.round_number >= window_start
    ]
    by_round = {a.round_number: a for a in attempts}
    expected_tail = list(range(round_num - limit + 1, round_num + 1))
    if any(r not in by_round for r in expected_tail):
        return None

    common = set(by_round[expected_tail[-1]].active_failure_signatures)
    for r in reversed(expected_tail[:-1]):
        common &= set(by_round[r].active_failure_signatures)
    if not common:
        return None

    streak_rounds: List[int] = []
    for r in range(round_num, window_start - 1, -1):
        att = by_round.get(r)
        if att is None or not (set(att.active_failure_signatures) & common):
            break
        streak_rounds.append(r)
    streak_rounds.reverse()

    latest = by_round[round_num]
    summaries: List[str] = []
    for sig, summ in zip(latest.active_failure_signatures, latest.active_failure_summaries):
        if sig in common and summ not in summaries:
            summaries.append(summ)
    modified: List[str] = []
    for r in streak_rounds:
        for path in by_round[r].modified_files:
            if path not in modified:
                modified.append(path)

    return StagnationReport(
        round_number=round_num,
        streak=len(streak_rounds),
        rounds=tuple(streak_rounds),
        signatures=tuple(sorted(common)),
        summaries=tuple(summaries or sorted(common)),
        modified_files=tuple(modified),
    )


def format_stagnation_note(report: StagnationReport, remaining_rounds: int) -> str:
    """채팅 피드와 DB 에 남길 외부 정체 관찰자 안내 문구."""
    round_span = ", ".join(f"Round {r}" for r in report.rounds)
    bullets = "\n".join(f"- {item}" for item in report.summaries[:3])
    files_line = (
        f"\n- 반복 수정된 파일: {', '.join(f'`{p}`' for p in report.modified_files[:5])}"
        if report.modified_files else ""
    )
    return (
        f"[정체 감지 · 재설계 에스컬레이션] 외부 관찰자가 {round_span} ({report.streak}회 연속)에서 "
        f"동일한 실패가 해소되지 않고 반복됨을 감지했습니다.\n"
        f"{bullets}{files_line}\n"
        f"국소 증상 패치를 중단하고, 남은 {remaining_rounds}라운드를 위해 오케스트레이터 **구조 재설계 단계**로 에스컬레이션합니다."
    )


def redesign_prompt(
    *,
    user_prompt: str,
    user_record: str,
    previous_plan: str,
    report: StagnationReport,
    attempt_history: str,
    ledger: str,
    roster_block: str,
    remaining_rounds: int,
) -> List[Dict[str, str]]:
    """오케스트레이터에게 전달하는 편향 격리형 재설계 프롬프트.

    참여 에이전트들의 설득형 대화 원문을 배제하고, 원래 요구사항·실행 실패 증거·시도 이력만 전달해
    기존 접근의 구조적 한계를 버리고 새 설계를 세우게 합니다.
    """
    failure_lines = "\n".join(f"- {s}" for s in report.summaries)
    content = (
        f"{REDESIGN_PROMPT_MARKER}\n"
        f"토론 루프 외부 관찰자가 {report.streak}회 연속 동일 실패({', '.join(f'Round {r}' for r in report.rounds)})를 "
        f"감지했습니다. 기존 계획의 점진적 증상 패치로는 해결되지 않았으므로, 남은 {remaining_rounds}라운드에서 "
        f"실행할 **구조적 재설계 계획**을 수립하세요.\n\n"
        f"[이번 턴 요청]\n{user_prompt}\n\n"
        + (f"{user_record}\n\n" if user_record else "")
        + (f"[기존 계획 — 연속 실패로 대체 대상]\n{previous_plan.strip()}\n\n" if previous_plan.strip() else "")
        + f"[외부 관찰자 감지 실패]\n{failure_lines}\n\n"
        + (f"{attempt_history}\n\n" if attempt_history else "")
        + (f"[현재 결정 장부]\n{ledger.strip()}\n\n" if ledger.strip() else "")
        + f"[이번 토론 참여 전문가]\n{roster_block}\n\n"
        "아래 항목을 포함해 **새 계획 본문만** 명확히 작성하세요 (이 계획이 기존 계획을 대체하여 모든 전문가 프롬프트 상단에 고정됩니다):\n"
        "1. **기존 접근이 실패한 근본 원인**: 실행 증거 기준으로 왜 이전 패치가 통하지 않았는지 한두 문장으로 규정.\n"
        "2. **폐기할 가정과 새 구조적 해법**: 증상 우회(예외 삼키기·임시 분기)를 금지하고, 데이터 흐름·상태 경계·알고리즘 중 무엇을 바꿀지 명시.\n"
        "3. **전문가별 다음 라운드 역할 분배**: 설계·구현·비평·진단(재현 검증) 담당자가 각각 확인해야 할 구체적 완료 기준."
    )
    return [{"role": "user", "content": content}]
