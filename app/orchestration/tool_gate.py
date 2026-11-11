"""도구 보안 판정을 토론에 붙이는 자리.

보안 판정 로직은 `app/mcp/policy.py`의 순수 함수가 전담합니다. 본 모듈은 판정 로직에
**현재 세션 및 발언자 맥락**을 결합하고, '확인(ask)' 판정 시 사용자 승인을 요청하며 결과를
캐싱합니다.

* 설정은 매번 **지금의** conf.json 에서 읽습니다. 대화가 시작된 뒤에 조인 규칙도 곧바로
  걸려야 합니다 (에이전트 설정처럼 대화에 고정되지 않습니다).
* 모드는 대화 모드(로스터 패널, 비어 있으면 conf 의 기본값)와 에이전트별 모드 중
  **더 엄격한 쪽**입니다. 에이전트 설정은 조이기만 합니다.
* "이 대화에서 허용"·"이 대화에서 거부" 는 이 턴이 끝나도 남습니다 (세션 DB 의
  `tool_grants`·`tool_denials`). 다음 턴의 게이트가 그 목록으로 시작하고, 로스터의 규칙
  창에서 보고 지울 수 있습니다.
* 같은 발언자가 이번 턴에 이미 거부된 호출을 똑같이 다시 하면 묻지 않고 거부합니다. 모델이
  거부를 읽고도 같은 호출을 되풀이하면 카드가 끝없이 쌓입니다.
* 게이트 인스턴스는 턴 단위로 공유되지만 외부 내용 열람 여부는 **발언 단위**로 격리됩니다. 따라서 도구 루프가 호출 시마다
  `tainted_by`를 인자로 전달합니다 (동일 라운드에서 병렬 발언하는 전문가 간에 오염 상태가 섞이지 않습니다).
* 대화형 사용자가 부재한 경우(배치 실행/헤드리스) "확인" 판정은 안전하게 거부(Denied) 처리됩니다 — Claude Code의 `dontAsk` 원칙.

게이트 자체에 예외가 발생하면 **안전하게 거부**(Fail Closed) 처리합니다. 보안 판정이 불가능한 상태에서
도구를 임의 실행하지 않습니다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from app.agents.host_tools import host_tool_meta
from app.config import (
    active_config_path,
    add_tool_security_rules_to_conf_file,
    get_config,
)
from app.events import EventCallback, EventType
from app.mcp.policy import (
    ALLOW,
    DENY,
    MODE_LABELS,
    RISK_LABELS,
    TAINT_RULE,
    UNATTENDED_RULE,
    UNATTENDED_TAINT_RULE,
    CallProfile,
    Policy,
    Verdict,
    evaluate,
    denial_covers,
    grant_covers,
    narrow_rules,
    normalize_mode,
    profile_call,
    refusal_text,
    stricter_mode,
    tool_always_denied,
)
from app.orchestration.control import asks_human
from app.tool_rules import Rule, parse_rule, parse_rules

logger = logging.getLogger(__name__)

# (이 대화에서 허용, 이 대화에서 거부) 를 세션에 저장하는 함수.
RuleSaver = Callable[[List[str], List[str]], Awaitable[None]]

# 승인 카드에 싣는 인자 미리보기의 상한(글자). 코드는 따로, 전문으로 싣습니다.
ARGUMENT_PREVIEW_CHARS = 4000
CODE_PREVIEW_CHARS = 20000


@dataclass
class GateResult:
    """게이트의 답. `allowed` 가 False 면 `output`/`status` 가 곧 도구 결과입니다."""

    allowed: bool
    output: str = ""
    status: str = ""
    audit: Dict[str, Any] = field(default_factory=dict)


def _audit(decision: str, verdict_risk: str, rule: str = "", approver: str = "") -> Dict[str, Any]:
    return {"decision": decision, "risk": verdict_risk, "rule": rule, "approver": approver}


def _registered_rule(decision: str, scope: List[str]) -> str:
    """카드에서 기억하게 한 범위를 감사 기록 모양으로 (`session:…` · `always:…`). 아니면 빈 문자열."""
    if not scope:
        return ""
    if decision.endswith("_session"):
        return "session:" + ", ".join(scope)
    if decision.endswith("_always"):
        return "always:" + ", ".join(scope)
    return ""


def _safe_rules(texts: List[str]) -> List[Rule]:
    """저장된 허용 목록을 읽습니다. 규칙 문법이 바뀌어 못 읽는 줄은 건너뜁니다."""
    rules: List[Rule] = []
    for text in texts:
        try:
            rules.append(parse_rule(text))
        except ValueError:
            logger.warning(f"Ignoring an unreadable tool grant: {text!r}")
    return rules


def _tool_meta(tool_name: str, mcp: Any) -> Any:
    """판정에 쓸 도구 정보. MCP 매니저가 모르는 플러그인 호스트 도구는 레지스트리가 답합니다."""
    meta = host_tool_meta(tool_name)
    if meta is not None:
        return meta
    return mcp.tool_meta(tool_name) if hasattr(mcp, "tool_meta") else None


def _preview(arguments: Any) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) > ARGUMENT_PREVIEW_CHARS:
        text = text[:ARGUMENT_PREVIEW_CHARS] + f"\n… ({len(text) - ARGUMENT_PREVIEW_CHARS:,}자 생략)"
    return text


# 사용자가 거부한 도구 호출에 부여되는 후속 지침. 사용자 결정이므로 모델이 우회로를 시도하지 않도록 제한합니다.
_DENIED_BY_USER_NEXT_ACTION = (
    "유저의 결정을 따르세요. 같은 호출을 다시 하지 말고, 같은 결과를 내는 다른 도구로 "
    "돌아가지도 마십시오. 이 작업 없이 진행하거나, 꼭 필요하면 이유를 발언에 적어 "
    "유저가 판단하게 하세요."
)


class ToolGate:
    """세션 턴 동안 도구 호출 권한을 판정하고 사용자 승인을 중개하는 보안 게이트."""

    def __init__(
        self,
        *,
        session_id: str,
        mode: str = "",
        grants: Optional[List[str]] = None,
        denials: Optional[List[str]] = None,
        control: Any = None,
        on_event: Optional[EventCallback] = None,
        save_rules: Optional[RuleSaver] = None,
    ) -> None:
        self.session_id = session_id
        # 비어 있으면 conf.json 의 기본 모드를 따릅니다 (대화가 모드를 고르지 않음).
        self._session_mode = normalize_mode(mode, fallback="")
        self._grants: List[str] = [g for g in (grants or []) if str(g).strip()]
        self._denials: List[str] = [d for d in (denials or []) if str(d).strip()]
        self.control = control
        self.on_event = on_event
        self.save_rules = save_rules
        # 이번 턴에 사용자가 거부한 호출 (발언자·도구·인자) → 모델에 반환된 거부 사유.
        self._rejected: Dict[str, str] = {}

    # -------------------------------------------------- 규칙 묶음

    @property
    def grants(self) -> List[str]:
        return list(self._grants)

    @property
    def denials(self) -> List[str]:
        return list(self._denials)

    def set_mode(self, mode: str) -> None:
        """토론 중에 바꾼 대화 모드. 다음 도구 호출부터 걸립니다."""
        self._session_mode = normalize_mode(mode, fallback="")

    async def replace_rules(self, grants: List[str], denials: List[str]) -> None:
        """로스터의 규칙 창에서 고친 목록으로 갈아 끼우고 저장합니다.

        토론 중에는 저장을 이 게이트 하나가 맡습니다. 화면이 따로 DB 에 쓰면, 그 사이
        카드에서 더해진 규칙을 화면의 옛 목록이 덮어 지웁니다.
        """
        self._grants = [g for g in grants if str(g).strip()]
        self._denials = [d for d in denials if str(d).strip()]
        await self._save()

    async def _save(self) -> None:
        if self.save_rules is None:
            return
        try:
            await self.save_rules(list(self._grants), list(self._denials))
        except Exception as exc:  # noqa: BLE001 - 저장 실패로 호출을 막지 않습니다
            logger.warning(f"Could not save tool rules for {self.session_id}: {exc}")

    def policy_for(self, agent_key: str) -> Policy:
        """이 발언자에게 걸리는 규칙. 매번 지금의 conf.json 으로 만듭니다."""
        security = get_config().tool_security
        extra = security.agents.get(agent_key)
        mode = stricter_mode(
            self._session_mode or security.mode,
            extra.mode if extra is not None else None,
        )
        return Policy(
            mode=mode,
            deny=parse_rules(list(security.deny) + (list(extra.deny) if extra else [])),
            ask=parse_rules(list(security.ask) + (list(extra.ask) if extra else [])),
            allow=parse_rules(security.allow),
            grants=_safe_rules(self._grants),
            denials=_safe_rules(self._denials),
        )

    def filter_tools(self, agent_key: str, tools: List[Dict[str, Any]], mcp: Any) -> List[Dict[str, Any]]:
        """인자와 무관하게 늘 거부될 도구를 목록에서 뺍니다.

        목록을 못 걸러도 판정은 호출마다 다시 하므로, 여기서 실패하면 그대로 둡니다.
        """
        try:
            policy = self.policy_for(agent_key)
            kept = []
            for tool in tools:
                name = str((tool.get("function") or {}).get("name") or "")
                meta = _tool_meta(name, mcp)
                if meta is not None and tool_always_denied(meta, policy):
                    continue
                kept.append(tool)
            return kept
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not filter tools for {agent_key}: {type(exc).__name__}: {exc}")
            return tools

    # -------------------------------------------------- 판정

    async def check(
        self,
        agent: Any,
        tool_name: str,
        arguments: Dict[str, Any],
        mcp: Any,
        tainted_by: Sequence[str] = (),
    ) -> GateResult:
        """단일 도구 호출을 판정합니다. 게이트 내부 예외 발생 시 안전하게 거절(Fail Closed)합니다.

        `tainted_by`는 해당 발언이 앞서 읽은 외부 콘텐츠 출처 목록입니다 (`ToolLoop.tainted_by`).
        """
        try:
            return await self._check(agent, tool_name, arguments, mcp, tainted_by)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - fail closed
            logger.error(
                f"Tool gate failed for {getattr(agent, 'key', '?')} → {tool_name}: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return GateResult(
                False,
                refusal_text(tool_name, f"도구 보안 판정 중 오류가 나서 실행하지 않았습니다 "
                                        f"({type(exc).__name__})."),
                "denied",
                _audit("deny", "", "gate-error"),
            )

    async def _check(
        self, agent: Any, tool_name: str, arguments: Dict[str, Any], mcp: Any, tainted_by: Sequence[str],
    ) -> GateResult:
        meta = _tool_meta(tool_name, mcp)
        if meta is None:
            # 모르는 도구는 실행되지 않습니다 — 매니저가 "Unknown tool" 로 답합니다.
            return GateResult(True)

        hard = mcp.hard_refusal(tool_name, arguments)
        if hard:
            text, status = hard
            return GateResult(False, text, status, _audit("hard", "", "고정 보호" if status == "denied" else "문서 형식"))

        profile = profile_call(meta, arguments, mcp.workspace)
        profile.tainted_by = tuple(tainted_by)
        policy = self.policy_for(agent.key)
        verdict = evaluate(profile, policy)

        if verdict.effect == ALLOW:
            # 감사 기록의 `rule` 칸 모양은 `policy.describe_verdict` 에 적혀 있습니다.
            if verdict.source == "grant":
                rule = f"session:{verdict.rule}"
            elif verdict.source == "rule":
                rule = verdict.rule
            else:
                rule = f"mode:{policy.mode}"
            return GateResult(True, audit=_audit("allow", verdict.risk, rule))

        if verdict.effect == DENY:
            if verdict.source == "denial":
                # 사용자가 세션 내에서 명시적으로 거부한 규칙 범위입니다. 전역 설정 거부와 구분하여 기록합니다.
                return GateResult(
                    False,
                    refusal_text(tool_name, verdict.explain(), _DENIED_BY_USER_NEXT_ACTION),
                    "denied",
                    _audit("rejected", verdict.risk, f"session:{verdict.rule}"),
                )
            return GateResult(
                False, refusal_text(tool_name, verdict.explain()), "denied",
                _audit("deny", verdict.risk, verdict.rule or f"mode:{policy.mode}"),
            )

        signature = self._signature(agent.key, tool_name, arguments)
        if signature in self._rejected:
            return GateResult(
                False,
                self._rejected[signature] + "\n(이번 턴에 이미 거부된 호출이라 다시 묻지 않았습니다.)",
                "denied",
                _audit("rejected", verdict.risk, "repeat"),
            )

        if not asks_human(self.control):
            return GateResult(
                False,
                refusal_text(tool_name, "확인이 필요한 호출인데 물어볼 유저가 없어 실행하지 않았습니다.\n"
                                        + verdict.explain()),
                "denied",
                _audit("deny", verdict.risk, UNATTENDED_TAINT_RULE if verdict.source == "taint" else UNATTENDED_RULE),
            )

        return await self._ask(agent, tool_name, arguments, profile, policy, verdict, signature)

    # -------------------------------------------------- 묻기

    async def _ask(
        self,
        agent: Any,
        tool_name: str,
        arguments: Dict[str, Any],
        profile: CallProfile,
        policy: Policy,
        verdict: Verdict,
        signature: str,
    ) -> GateResult:
        security = get_config().tool_security
        timeout = float(security.approval_timeout)
        code = next((a.target for a in profile.actions if a.kind == "exec" and a.target), "")
        payload = {
            "tool_name": tool_name,
            "server": profile.server,
            "tool": profile.tool,
            "mode": policy.mode,
            "mode_label": MODE_LABELS.get(policy.mode, policy.mode),
            "risk": verdict.risk,
            "risk_label": RISK_LABELS.get(verdict.risk, verdict.risk),
            "headline": verdict.headline,
            "reasons": list(verdict.reasons),
            "arguments_preview": _preview(
                {k: v for k, v in arguments.items() if not (code and v == code)}
            ),
            "code": code[:CODE_PREVIEW_CHARS],
            "suggestions": list(verdict.suggestions),
            "deny_suggestions": narrow_rules(profile),
            # 도구 단위 범위는 그 도구의 **모든** 호출에 걸립니다. 화면이 그렇다고 밝힙니다.
            "tool_wide": any(
                s.startswith("mcp(") for s in (verdict.suggestions or narrow_rules(profile))
            ),
            # 허용을 기억할 수 있는가. ask 규칙에 걸린 호출은 허용을 기억해도 다음에 또
            # 묻습니다 (ask 가 allow 보다 앞섭니다). 거부는 언제나 기억할 수 있습니다.
            "can_remember": bool(verdict.suggestions),
        }

        async def _open(request) -> None:
            if self.on_event:
                await self.on_event({"type": EventType.TOOL_APPROVAL_REQUESTED, **request.describe()})

        request = await self.control.ask_tool_approval(
            agent_key=agent.key,
            agent_name=getattr(agent, "name", agent.key),
            payload=payload,
            timeout=timeout,
            on_open=_open,
            validator=lambda scope, decision: self._scope_problem(scope, decision, profile, policy.mode),
        )
        return await self._settle(request, agent, tool_name, verdict, policy, signature)

    def _scope_problem(
        self, scope: List[str], decision: str, profile: CallProfile, mode: str,
    ) -> Optional[str]:
        """사용자가 수정한 허용 범위의 유효성을 검증합니다. 이상이 있을 경우 사유 반환."""
        try:
            parse_rules(scope)
        except ValueError as exc:
            return str(exc)
        if decision.startswith("allow_"):
            if not grant_covers(scope, profile, mode):
                return "이 범위로는 지금 이 호출이 허용되지 않습니다. 범위를 넓히거나 '이번만 허용' 을 쓰세요."
        elif not denial_covers(scope, profile):
            return "이 범위로는 지금 이 호출이 막히지 않습니다. 범위를 고치거나 '거부' 를 쓰세요."
        return None

    async def _settle(
        self,
        request: Any,
        agent: Any,
        tool_name: str,
        verdict: Verdict,
        policy: Policy,
        signature: str,
    ) -> GateResult:
        decision = request.decision
        scope = list(request.scope)
        approver = request.approver or ""
        persisted: List[str] = []
        note = ""
        # 사용자 확인이 유발된 사유를 감사 표식으로 기록합니다. 규칙 등록 범위가 있는 경우 해당 범위를 우선합니다.
        marker = TAINT_RULE if verdict.source == "taint" else ""

        if decision in ("allow_session", "allow_always", "deny_session", "deny_always") and scope:
            allow = decision.startswith("allow_")
            target = self._grants if allow else self._denials
            remembered = [r for r in scope if r not in target]
            target.extend(remembered)
            if remembered:
                await self._save()
            if decision.endswith("_always"):
                if approver != "local":
                    # 원격 화면에서는 버튼이 비활성화되나, 우회 호출 시 세션 규칙으로 범위를
                    # 제한합니다 — conf.json 수정은 로컬 콘솔 사용자에게만 허용됩니다.
                    note = "원격 클라이언트 응답이므로 conf.json에 저장하지 않고 세션에만 적용했습니다."
                    decision = "allow_session" if allow else "deny_session"
                else:
                    try:
                        path = active_config_path()
                        if path is not None:
                            persisted = await asyncio.to_thread(
                                add_tool_security_rules_to_conf_file,
                                "allow" if allow else "deny", scope, path,
                            )
                    except Exception as exc:  # noqa: BLE001
                        note = f"conf.json 에 저장하지 못했습니다 ({exc}). 이 대화에만 적용했습니다."
                        logger.warning(f"Could not persist tool rules {scope}: {exc}")

        if self.on_event:
            await self.on_event({
                "type": EventType.TOOL_APPROVAL_RESOLVED,
                "id": request.id,
                "agent_name": request.agent_name,
                "tool_name": tool_name,
                "decision": decision,
                "scope": scope,
                "persisted": persisted,
                "persisted_as": "deny" if decision.startswith("deny") else "allow",
                "note": note,
                "session_grants": list(self._grants),
                "session_denials": list(self._denials),
            })

        if request.allowed:
            return GateResult(True, audit=_audit(
                "approved", verdict.risk, _registered_rule(decision, scope) or marker or "once", approver,
            ))

        if decision == "timeout":
            reason = (
                f"{int(get_config().tool_security.approval_timeout)}초 안에 유저의 승인이 없어 "
                f"실행하지 않았습니다.\n{verdict.explain()}"
            )
            text = refusal_text(tool_name, reason)
            audit = _audit("timeout", verdict.risk, marker)
        else:
            reason = "유저가 이 호출을 거부했습니다."
            if decision in ("deny_session", "deny_always") and scope:
                reason += (
                    f" 이 대화 동안 `{'`, `'.join(scope)}` 범위의 호출은 실행되지 않습니다."
                )
            if request.reason:
                reason += f" 사유: {request.reason}"
            text = refusal_text(
                tool_name, f"{reason}\n{verdict.explain()}",
                _DENIED_BY_USER_NEXT_ACTION if (request.reason or decision != "deny") else "",
            )
            audit = _audit("rejected", verdict.risk, _registered_rule(decision, scope) or marker, approver)
        self._rejected[signature] = text
        return GateResult(False, text, "denied", audit)

    @staticmethod
    def _signature(agent_key: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        try:
            body = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            body = str(arguments)
        return f"{agent_key}\x1f{tool_name}\x1f{body}"
