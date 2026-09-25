"""대화 하나를 사람이 읽는 마크다운 문서로 옮깁니다.

토론 기록은 지금까지 DB 안에만 있었습니다. 화면을 닫으면 남에게 보여줄 방법도,
다른 도구로 넘길 방법도 없었습니다. 여기서 만드는 문서 하나에 그 대화의 전부가
들어갑니다 — 설정, 발언 순서, 도구 실행 기록, 최종 산출물.

DB 를 모르는 순수 함수로 둡니다. 화면은 읽어 온 값을 넘기기만 하고, 이 파일은
그것을 문서로만 바꿉니다.
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.mcp.policy import OUTCOME_LABELS, describe_verdict, tool_outcome
from app.orchestration.graph_run import NodeBadges, node_labels
from app.orchestration.strategies import get_strategy
# 시각 규칙은 `app.timestamps` 가 정본입니다. 여기서도 이름을 내보내 기존 호출자
# (`from app.export import to_local` 등)가 그대로 동작하게 합니다.
from app.timestamps import (  # noqa: F401 - 다시 내보냄
    format_duration,
    report_completed_line,
    speech_time_text,
    speech_timing,
    to_local,
)

# 발언 종류별 머리표. 누가 무슨 자격으로 말했는지가 한눈에 보여야 합니다.
TYPE_LABEL = {
    "user": "🙋 유저",
    "orchestrator": "🧭 오케스트레이터",
    "agent": "🤖 에이전트",
    "error": "⚠️ 실패",
    "system": "⚙️ 시스템",
}

# 파일 이름에 쓸 수 없는 문자. Windows 기준으로 잡습니다.
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(title: str, when: Optional[datetime] = None, ext: str = "md") -> str:
    """제목으로 파일 이름을 만듭니다. 비면 날짜만으로도 이름이 남습니다.

    `when` 은 받은 그대로 씁니다. DB 에서 읽은 값이라면 부르는 쪽에서
    `to_local()` 로 옮겨 넘기세요 — 문서 안의 시각과 어긋나면 안 됩니다.
    """
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M")
    cleaned = _UNSAFE_FILENAME.sub(" ", title or "").strip()
    cleaned = re.sub(r"\s+", "_", cleaned)[:60].strip("_")
    return f"{cleaned or 'debate'}_{stamp}.{ext}"


def _fence(content: str, language: str = "") -> str:
    """코드 블록. 내용 안에 백틱 세 개가 있으면 울타리를 늘립니다.

    LLM 산출물에는 마크다운 코드 블록이 그대로 들어 있는 일이 흔합니다. 울타리를
    늘리지 않으면 문서 뒷부분이 통째로 코드로 보입니다.
    """
    body = content or ""
    longest = max((len(m) for m in re.findall(r"`{3,}", body)), default=2)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{body}\n{fence}"


def _fmt_time(value: Any) -> str:
    if isinstance(value, datetime):
        return to_local(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _tool_call_block(call: Dict[str, Any]) -> List[str]:
    name = call.get("tool_name", "unknown_tool")
    status = call.get("status", "success")
    # 채팅 피드와 같은 말을 씁니다 — 결과 하나, 판정 한 줄 (`policy.describe_verdict`).
    outcome = tool_outcome(status, call.get("security"))
    mark = {"success": "✅", "error": "❌", "blocked": "⛔"}[outcome]
    args = call.get("arguments", {})
    args_text = (
        json.dumps(args, indent=2, ensure_ascii=False)
        if isinstance(args, (dict, list)) else str(args)
    )
    output = str(call.get("output", "") or "")

    lines = [f"<details>", f"<summary>{mark} <code>{name}</code> — {OUTCOME_LABELS[outcome]}</summary>", ""]
    verdict = describe_verdict(call.get("security"))
    if verdict:
        lines.append(f"**판정**: {verdict}")
        lines.append("")
    lines.append("**인자**")
    lines.append("")
    lines.append(_fence(args_text, "json"))
    lines.append("")
    lines.append("**출력**")
    lines.append("")
    lines.append(_fence(output))
    lines.append("")
    lines.append("</details>")
    return lines


def build_session_markdown(
    session: Dict[str, Any],
    messages: List[Dict[str, Any]],
    artifacts: Optional[List[Dict[str, Any]]] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """대화 하나를 마크다운 문서 한 장으로 만듭니다.

    `session` 은 제목·전략·라운드·참여 에이전트 같은 설정, `messages` 는 발언
    (각 발언의 `tool_calls` 포함), `artifacts` 는 최종 산출물입니다.

    `tool_calls` 는 대화 전체의 도구 실행 기록입니다. 어느 발언에서 나온 것인지
    기록이 남지 않은 항목만 문서 끝에 따로 모읍니다 — 발언에 붙은 것을 여기서
    또 보여주면 같은 내용이 두 번 나옵니다.
    """
    artifacts = artifacts or []
    title = session.get("title") or "Untitled Debate"

    out: List[str] = [f"# {title}", ""]

    # --- 설정 요약 -----------------------------------------------------
    agents = session.get("active_agents") or []
    meta = [
        ("생성", _fmt_time(session.get("created_at"))),
        ("최종 수정", _fmt_time(session.get("updated_at"))),
        # 저장된 키가 아니라 **지금 이 대화가 실제로 도는 전략**의 이름을 적습니다.
        # 예전 이름으로 저장된 대화는 이어서 진행하면 지금 전략으로 돌아가므로,
        # 문서에 없어진 키를 남겨 두면 그 문서가 거짓말을 하게 됩니다.
        ("토론 전략", get_strategy(session.get("strategy")).display_name),
        ("최대 라운드", str(session.get("max_rounds") or "")),
        ("참여 에이전트", ", ".join(agents) if agents else "-"),
        ("작업 공간", str(session.get("workspace_dir") or "(기본값)")),
    ]
    # 동시 실행 상한은 병렬 지시 전략에서만 읽히는 값입니다. 순차로 돈 대화에
    # 적어 두면 문서가 있지도 않은 설정을 있는 것처럼 말하게 됩니다.
    strategy = get_strategy(session.get("strategy"))
    if strategy.orchestrator_dispatches_parallel or strategy.runs_graph:
        meta.append(("동시 실행 상한", str(session.get("parallel_limit") or 3)))
    # 그래프 토론은 마지막 턴에 실제로 돈 그래프를 적습니다. 발언 제목의 노드 이름도 이 그래프에서 옵니다
    # (지난 턴이 다른 그래프였다면 그 턴의 노드는 id 로 적힙니다).
    graph = session.get("graph_snapshot") if strategy.runs_graph else None
    if graph:
        meta.append(("그래프", f"{graph.get('name') or graph.get('id')} (data/graphs/{graph.get('id')}.json)"))
    badges = NodeBadges(node_labels(graph))

    out.append("| 항목 | 값 |")
    out.append("| --- | --- |")
    out.extend(f"| {key} | {value or '-'} |" for key, value in meta)
    out.append("")

    instructions = (session.get("custom_instructions") or "").strip()
    if instructions:
        out += ["## 세션 전용 지침", "", instructions, ""]

    # --- 발언 ----------------------------------------------------------
    out += ["## 토론 기록", ""]
    if not messages:
        out += ["_아직 오간 발언이 없습니다._", ""]

    current_round: Optional[int] = None
    for msg in messages:
        round_number = msg.get("round_number") or 0
        if round_number != current_round:
            current_round = round_number
            heading = "준비 및 계획" if round_number == 0 else (
                # 그래프 토론의 라운드는 단계입니다.
                f"{round_number}단계" if msg.get("graph_node_id") else f"Round {round_number}"
            )
            out += [f"### {heading}", ""]

        label = TYPE_LABEL.get(msg.get("msg_type", "agent"), "🤖 에이전트")
        name = msg.get("sender_name") or msg.get("sender_key") or "unknown"
        role = msg.get("sender_role") or ""
        # 시작·종료·소요 시간. 옛 기록은 `created_at` 한 시각으로 물러섭니다
        # (`speech_timing` 의 `legacy` 참고).
        stamp = speech_time_text(msg)
        header = f"#### {label} · {name}"
        if role:
            header += f" ({role})"
        node_badge = badges.observe(msg)
        if node_badge:
            header += f" — 노드 “{node_badge}”"
        out.append(header)
        if stamp:
            out += ["", f"*{stamp}*"]
        out += ["", (msg.get("content") or "").strip(), ""]

        for call in msg.get("tool_calls") or []:
            out += _tool_call_block(call)
            out.append("")

    # --- 발언에 붙지 못한 도구 실행 기록 ---------------------------------
    known_ids = {msg.get("id") for msg in messages if msg.get("id")}
    orphan_calls = [
        call for call in (tool_calls or [])
        if not call.get("message_id") or call.get("message_id") not in known_ids
    ]
    if orphan_calls:
        out += ["## 도구 실행 기록", ""]
        for call in orphan_calls:
            stamp = _fmt_time(call.get("created_at"))
            agent = call.get("agent_key") or "unknown"
            out.append(f"**{agent}**" + (f" · {stamp}" if stamp else ""))
            out.append("")
            out += _tool_call_block(call)
            out.append("")

    # --- 산출물 --------------------------------------------------------
    if artifacts:
        out += ["## 최종 산출물", ""]
        for art in artifacts:
            art_title = art.get("title") or "Artifact"
            art_type = art.get("artifact_type", "markdown")
            language = art.get("language") or art_type
            out += [f"### {art_title}", "", f"`{art_type.upper()}`", ""]
            if art_type == "markdown":
                # 이미 마크다운입니다. 울타리로 감싸면 렌더링되지 않습니다.
                out += [(art.get("content") or "").strip(), ""]
            else:
                out += [_fence(art.get("content") or "", language), ""]

    out += ["---", "", f"_MADO: Multi-Agent Debate & Orchestration Platform 에서 내보냄 "
            f"({datetime.now().strftime('%Y-%m-%d %H:%M')})_", ""]
    return "\n".join(out)
