"""계획 승인 — 계획과 토론 사이에 사람을 세웁니다 (ADR-028).

## 무엇이 문제였나

오케스트레이터의 계획(0라운드)이 나오면 엔진은 곧바로 토론으로 넘어갔습니다. 계획이 요청을
잘못 읽었어도 사람이 그것을 알게 되는 것은 전문가들이 몇 라운드를 돈 뒤였고, 그때 할 수
있는 것은 개입 메모나 정지뿐이었습니다. 요청을 잘못 읽고 끝까지 가는 것이 가장 비싼
오류인데, 그것을 가장 싸게 고칠 수 있는 자리 — 계획 직후 — 에 사람이 없었습니다.

## 여기서 하는 일

planning harness(MCP 서버)의 `계획 → 사람 승인 → 실행 → 완료 확인` 을 MADO 의 엔진에
맞춰 옮긴 것입니다. MCP 서버는 호스트를 고칠 수 없을 때의 우회로였고, MADO 는 루프를
직접 가지므로 엔진의 단계로 넣습니다. 그래서 그쪽의 약점 — 모델이 계획을 건너뛸 수 있고,
승인 전에 다른 도구를 부를 수 있고, 완료 보고가 자기 보고뿐인 것 — 이 여기에는 없습니다.

1. **분담표** — 계획 발언은 사람과 전문가가 읽는 글입니다. 승인 카드에서 고칠 수 있으려면
   전문가별 태스크가 값으로 있어야 하므로, 계획 직후 도구 없는 호출 한 번으로 받습니다
   (`tasks_prompt` · `parse_tasks`). 깨진 답이어도 전문가 수만큼의 빈 칸은 남습니다.
2. **승인** — 사람은 카드에서 태스크와 완료 기준을 **직접 고치고 한 번에 승인**합니다. 다시
   쓰게 하는 것은 오케스트레이터가 계획을 새로 세워야 할 때뿐입니다 (의견을 적으면 승인
   버튼이 사라지고 수정 요청 버튼이 나타납니다 — 승인은 의견을 싣지 못하기 때문입니다).
   선택이 사람의 취향에 달린 태스크에는 대안이 붙고, 고르면 그 글이 태스크가 됩니다.
3. **실행** — 승인된 분담은 계획 고정문에 실려 모든 발언자의 맥락에 남고, 각 전문가의
   차례 지시 끝에 자기 태스크가 한 번 더 붙습니다. 고친 것이 계획 본문과 다르면 승인된
   쪽이 우선한다고 적어, 두 지시가 서로 어긋나지 않게 합니다.
4. **완료 확인** — 합성 프롬프트에 태스크·완료 기준과 **엔진이 센** 기록(발언 횟수, 응답
   실패)을 싣고, 보고서에 태스크별 완료 확인을 쓰게 합니다. 누가 말했는지는 모델의 보고가
   아니라 기록입니다.

이 모듈은 순수합니다 — 글과 값만 다룹니다. 묻고 기다리는 것은 `control.TurnControl`,
기록하고 다시 계획을 부르는 것은 `engine.OrchestratorEngine._approve_plan` 입니다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.orchestration import turns

# 수정 요청으로 기록되는 유저 발언의 머리표. 유저 발언 기록이 이것으로 "계획 수정 요청" 이라 적습니다
# (`context_memory._user_label`).
PLAN_REVISION_PREFIX = "[계획 수정 요청]"

# 사람이 고를 수 있는 답과, 사람이 고르지 않았는데 닫힌 경우.
DECISION_APPROVE = "approve"
DECISION_REVISE = "revise"
DECISION_TIMEOUT = "timeout"
DECISION_STOPPED = "stopped"
ANSWERS = (DECISION_APPROVE, DECISION_REVISE)

# 분담표를 받는 호출의 표식. 프롬프트의 마지막 줄 근처에 있어, 테스트 대역도 이것으로 알아봅니다.
TASKS_MARKER = "[태스크 분담표]"

# 한 칸의 상한. 카드에 그려지고, 발언마다 프롬프트에 실리는 글입니다.
MAX_TASK_CHARS = 600
MAX_DONE_WHEN_CHARS = 200
MAX_ALTERNATIVES = 3
MAX_COMMENT_CHARS = 1000

# 태스크 칸을 비운 채 승인했을 때. 그 전문가는 계획 본문에 적힌 대로 합니다.
FOLLOWS_PLAN = "(계획 본문을 따릅니다)"


class PlanApprovalExpired(RuntimeError):
    """승인 카드에 답이 없어 턴을 멈춰 둡니다. 아무것도 실행되지 않았습니다.

    오류가 아니라 **대기의 끝**입니다. 턴은 끊긴 턴과 같은 모양으로 남고 (ADR-024), 사람이
    '이어서 진행' 을 누르면 같은 계획으로 승인 카드가 다시 열립니다.
    """


def _clip(text: Any, limit: int) -> str:
    """한 칸의 글. 앞뒤 공백을 떼고 상한에서 자릅니다."""
    return str(text or "").strip()[:limit].strip()


def _one_line(text: Any, limit: int) -> str:
    """한 줄로 적는 칸 (완료 기준). 줄바꿈을 공백으로 접습니다."""
    return " ".join(str(text or "").split())[:limit].strip()


def blank_tasks(specialists: Sequence[Any]) -> List[Dict[str, Any]]:
    """전문가마다 빈 칸 하나. 분담표를 받지 못했을 때도 카드는 이 모양으로 뜹니다."""
    return [
        {"agent": a.key, "name": a.name, "role": a.role, "task": "", "done_when": "", "alternatives": []}
        for a in specialists
    ]


def tasks_prompt(user_prompt: str, plan_body: str, roster: str) -> str:
    """계획 본문을 전문가별 태스크로 옮겨 달라는 지시 (도구 없는 호출용)."""
    return (
        f"[목표]\n{user_prompt}\n\n"
        f"[방금 세운 계획]\n{plan_body}\n\n"
        f"[이번 토론 참여 전문가]\n{roster}\n\n"
        f"{TASKS_MARKER} 위 계획을 유저가 승인 화면에서 읽고 고칠 수 있게, 전문가별 태스크로 "
        f"옮기세요. 계획에 없는 일을 새로 만들지 말고, 목록의 전문가마다 한 줄씩 적으세요.\n"
        f"- task: 그 전문가가 이번 턴에 할 일과 낼 산출물을 한두 문장으로.\n"
        f"- done_when: 결과를 확인할 수 있는 태스크면, 끝났을 때 무엇이 있거나 참인지 한 문장으로. "
        f"확인할 수 없으면 빈 문자열.\n"
        f"- alternatives: 같은 태스크를 다르게 할 방법이 있고 그 선택이 유저의 취향에 달렸을 때만 "
        f"최대 {MAX_ALTERNATIVES - 1}개. 사실로 가릴 수 있는 선택이면 비우고 당신이 정하세요.\n\n"
        f"다음 JSON 형식으로만 답하세요:\n"
        '{"tasks": [{"agent": "에이전트키", "task": "태스크", "done_when": "완료 기준", '
        '"alternatives": ["다른 방법"]}]}'
    )


def _json_object(content: str) -> Optional[Any]:
    text = content or ""
    for pattern in (r"\{.*\}", r"\[.*\]"):
        block = re.search(pattern, text, re.DOTALL)
        if not block:
            continue
        try:
            return json.loads(block.group(0))
        except (ValueError, TypeError):
            continue
    return None


def parse_tasks(content: str, specialists: Sequence[Any]) -> List[Dict[str, Any]]:
    """응답에서 전문가별 태스크를 뽑습니다. 돌려주는 목록은 **언제나** 전문가 전원, 로스터 순서입니다.

    모델이 키 대신 이름을 적거나, 한 명을 빠뜨리거나, JSON 을 깨뜨리는 일은 흔합니다. 그때마다
    승인을 포기하면 사람이 볼 것이 없어지므로, 건질 수 있는 칸만 채우고 나머지는 비워 둡니다.
    빈 칸은 사람이 채우거나, 그대로 승인하면 계획 본문을 따릅니다.
    """
    tasks = blank_tasks(specialists)
    slot = {t["agent"]: t for t in tasks}
    by_name = {str(t["name"]).strip().lower(): t for t in tasks}

    data = _json_object(content)
    if isinstance(data, Mapping):
        raw = data.get("tasks")
        if not isinstance(raw, list):
            raw = data.get("assignments")
    else:
        raw = data
    if not isinstance(raw, list):
        return tasks

    for item in raw:
        if not isinstance(item, Mapping):
            continue
        who = str(item.get("agent") or item.get("key") or item.get("name") or "").strip()
        target = slot.get(who) or by_name.get(who.lower())
        if target is None or target["task"]:
            continue
        target["task"] = _clip(item.get("task") or item.get("instruction"), MAX_TASK_CHARS)
        target["done_when"] = _one_line(item.get("done_when") or item.get("criterion"), MAX_DONE_WHEN_CHARS)
        options = item.get("alternatives")
        if isinstance(options, list):
            seen = {target["task"]}
            for option in options:
                text = _clip(option.get("task") if isinstance(option, Mapping) else option, MAX_TASK_CHARS)
                if text and text not in seen and len(target["alternatives"]) < MAX_ALTERNATIVES - 1:
                    seen.add(text)
                    target["alternatives"].append(text)
    return tasks


def clean_tasks(raw: Any) -> List[Dict[str, Any]]:
    """기록(턴 구성 · 승인 기록)에서 읽은 태스크 목록을 같은 모양으로 맞춥니다."""
    out: List[Dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or not str(item.get("agent") or "").strip():
            continue
        options = item.get("alternatives")
        out.append({
            "agent": str(item["agent"]).strip(),
            "name": str(item.get("name") or item["agent"]).strip(),
            "role": str(item.get("role") or "").strip(),
            "task": _clip(item.get("task"), MAX_TASK_CHARS),
            "done_when": _one_line(item.get("done_when"), MAX_DONE_WHEN_CHARS),
            "alternatives": [
                _clip(o, MAX_TASK_CHARS) for o in (options if isinstance(options, list) else [])
                if str(o or "").strip()
            ][:MAX_ALTERNATIVES - 1],
        })
    return out


def _reply_by_agent(reply: Any) -> Dict[str, Mapping[str, Any]]:
    return {
        str(item.get("agent") or "").strip(): item
        for item in (reply if isinstance(reply, list) else [])
        if isinstance(item, Mapping)
    }


def settle(proposed: Sequence[Mapping[str, Any]], reply: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """사람이 카드에서 돌려준 값으로 승인된 태스크를 정합니다. (태스크, 사람이 고친 곳).

    제안에 없던 전문가는 받지 않고, 답에 없는 전문가는 제안 그대로입니다 — 화면이 낡았거나
    칸을 건드리지 않은 경우입니다. 승인된 태스크에는 대안을 남기지 않습니다. 고른 것이 곧 태스크입니다.
    """
    answers = _reply_by_agent(reply)
    tasks: List[Dict[str, Any]] = []
    changes: List[str] = []
    for item in proposed:
        answer = answers.get(str(item.get("agent")))
        before_task = str(item.get("task") or "")
        before_done = str(item.get("done_when") or "")
        task, done_when = before_task, before_done
        if answer is not None:
            if "task" in answer:
                task = _clip(answer.get("task"), MAX_TASK_CHARS)
            if "done_when" in answer:
                done_when = _one_line(answer.get("done_when"), MAX_DONE_WHEN_CHARS)
        name = str(item.get("name") or item.get("agent"))
        if task != before_task:
            options = [str(o) for o in item.get("alternatives") or []]
            if task in options:
                changes.append(f"{name}: 대안 {options.index(task) + 1} 선택")
            else:
                changes.append(f"{name}: 태스크 {'비움' if not task else '수정'}")
        if done_when != before_done:
            changes.append(
                f"{name}: 완료 기준 "
                + ("삭제" if not done_when else "추가" if not before_done else "수정")
            )
        tasks.append({
            "agent": str(item.get("agent")), "name": name, "role": str(item.get("role") or ""),
            "task": task, "done_when": done_when,
        })
    return tasks, changes


def clean_comments(proposed: Sequence[Mapping[str, Any]], raw: Any) -> Dict[str, str]:
    """태스크별 의견. 제안에 있는 전문가의, 글이 있는 것만."""
    known = {str(item.get("agent")) for item in proposed}
    out: Dict[str, str] = {}
    for key, text in (raw.items() if isinstance(raw, Mapping) else []):
        body = str(text or "").strip()[:MAX_COMMENT_CHARS]
        if str(key) in known and body:
            out[str(key)] = body
    return out


def card_actions(comment: str, task_comments: Optional[Mapping[str, Any]]) -> Tuple[bool, bool]:
    """카드에 어느 버튼이 보이는가 — (승인, 수정 요청).

    승인은 의견을 싣지 못합니다. 의견을 적은 채 승인하면 그 글은 읽히지 않고 버려지므로,
    의견 칸에 글이 있는 동안에는 승인 버튼을 내리고 수정 요청 버튼을 올립니다. 태스크나
    완료 기준을 고친 것은 의견이 아닙니다 — 그것은 승인과 함께 반영됩니다.
    """
    said = bool(str(comment or "").strip()) or any(
        str(text or "").strip() for text in (task_comments or {}).values()
    )
    return (not said, said)


def revision_request(
    proposed: Sequence[Mapping[str, Any]],
    comment: str,
    task_comments: Mapping[str, str],
    reply: Any,
) -> str:
    """수정 요청으로 기록할 유저 발언. 카드에 적은 것은 하나도 버리지 않습니다.

    태스크나 완료 기준을 고친 채 수정을 요청했으면 그 고친 값도 싣습니다. 계획이 통째로 다시
    쓰이므로 카드의 값은 사라지고, 여기에 적지 않으면 사람이 적은 것이 조용히 없어집니다.
    """
    names = {str(item.get("agent")): str(item.get("name") or item.get("agent")) for item in proposed}
    lines = [PLAN_REVISION_PREFIX]
    overall = str(comment or "").strip()[:MAX_COMMENT_CHARS]
    if overall:
        lines.append(overall)
    if task_comments:
        lines.append("\n태스크별 의견:")
        lines += [f"- {names.get(key, key)}: {text}" for key, text in task_comments.items()]
    edited, _changes = settle(proposed, reply)
    before = {str(item.get("agent")): item for item in proposed}
    kept: List[str] = []
    for task in edited:
        was = before[task["agent"]]
        if task["task"] != str(was.get("task") or ""):
            kept.append(f"- {task['name']} 태스크: {task['task'] or '(비움)'}")
        if task["done_when"] != str(was.get("done_when") or ""):
            kept.append(f"- {task['name']} 완료 기준: {task['done_when'] or '(삭제)'}")
    if kept:
        lines.append("\n유저가 카드에서 직접 고친 값 (다시 쓸 때 그대로 반영하세요):")
        lines += kept
    return "\n".join(lines)


def revision_body(content: str) -> str:
    """수정 요청 기록에서 머리표를 뗀 본문."""
    text = content or ""
    if text.startswith(PLAN_REVISION_PREFIX):
        text = text[len(PLAN_REVISION_PREFIX):]
    return text.strip()


def tasks_block(tasks: Iterable[Mapping[str, Any]]) -> str:
    """승인된 태스크 분담을 프롬프트와 기록에 적는 모양."""
    lines: List[str] = []
    for task in tasks:
        who = str(task.get("name") or task.get("agent"))
        if task.get("role"):
            who += f" ({task['role']})"
        lines.append(f"- {who}: {task.get('task') or FOLLOWS_PLAN}")
        if task.get("done_when"):
            lines.append(f"  완료 기준: {task['done_when']}")
    return "\n".join(lines)


def approval_note(tasks: Sequence[Mapping[str, Any]], changes: Sequence[str]) -> str:
    """승인 기록의 글. 무엇이 승인됐고 사람이 무엇을 고쳤는지."""
    head = "[계획 승인] 유저가 계획을 승인했습니다"
    head += f" (유저가 고친 곳 {len(changes)}건)." if changes else "."
    parts = [head, "", "승인된 태스크 분담:", tasks_block(tasks)]
    if changes:
        parts += ["", "유저가 고친 곳: " + "; ".join(changes)]
    return "\n".join(parts)


PINNED_TASKS_HEADING = (
    "[유저가 승인한 태스크 분담] 유저가 승인 화면에서 확인한 것입니다. 위 계획 본문과 다르면 "
    "이쪽이 우선합니다."
)


# 발언자 지명·라운드별 태스크 분배처럼, 계획 본문 없이 분담만 싣는 호출에서의 머리표.
ROUTING_TASKS_HEADING = (
    "[유저가 승인한 태스크 분담] 유저가 승인 화면에서 확인한 것입니다. 이 분담을 벗어나는 일을 "
    "새로 맡기지 마세요."
)


def own_task(tasks: Iterable[Mapping[str, Any]], agent_key: str) -> str:
    """한 전문가의 차례 지시 끝에 붙는 자기 태스크. 태스크가 없으면 빈 문자열."""
    for task in tasks:
        if task.get("agent") != agent_key or not task.get("task"):
            continue
        text = (
            "[유저가 승인한 당신의 태스크] 이번 턴에 당신이 맡은 일입니다. 이번 차례의 지시는 "
            f"이 태스크의 범위 안에서 수행하세요.\n{task['task']}"
        )
        if task.get("done_when"):
            text += f"\n완료 기준: {task['done_when']}"
        return text
    return ""


def completion_block(
    tasks: Sequence[Mapping[str, Any]],
    speeches: Mapping[str, int],
    failed: Sequence[str],
) -> str:
    """합성 프롬프트에 싣는 태스크별 기록. 누가 말했는지는 엔진이 센 값입니다."""
    lines = ["[유저가 승인한 태스크와 기록]"]
    for number, task in enumerate(tasks, start=1):
        key = str(task.get("agent"))
        count = int(speeches.get(key, 0))
        if count:
            record = f"발언 {count}회"
            if key in failed:
                record += " (응답 실패한 차례 있음)"
        else:
            record = "응답 실패 — 발언 없음" if key in failed else "발언 없음"
        line = f"{number}. {task.get('name') or key}: {task.get('task') or FOLLOWS_PLAN}"
        if task.get("done_when"):
            line += f" | 완료 기준: {task['done_when']}"
        lines.append(f"{line} | 기록: {record}")
    return "\n".join(lines)


COMPLETION_INSTRUCTION = (
    "**태스크별 완료 확인** — 위 [유저가 승인한 태스크와 기록]의 태스크마다 한 줄: 담당, "
    "판정(완료 · 부분 · 미완료), 근거(어느 발언이나 파일에서 확인되는지). 완료 기준이 있으면 "
    "그 기준으로 판정하고, 기록이 '발언 없음' 인 태스크는 완료로 적지 마세요."
)


# ---------------------------------------------------------------- 기록에서 다시 읽기


def approval_record(messages: Sequence[Any], start: int = 0) -> Optional[Tuple[int, Dict[str, Any]]]:
    """이번 턴의 승인 기록 — (자리, 기록의 값). 없으면 None."""
    for index in range(len(messages) - 1, start - 1, -1):
        msg = messages[index]
        if turns.kind_of(msg) == turns.KIND_APPROVAL:
            meta = turns._get(msg, "turn_meta")  # noqa: SLF001 - 같은 패키지의 기록 읽기
            return index, dict(meta) if isinstance(meta, Mapping) else {}
    return None


def pending_revision(messages: Sequence[Any], start: int = 0) -> Optional[str]:
    """계획이 아직 답하지 않은 수정 요청의 본문. 없으면 None.

    수정 요청을 기록한 뒤 계획을 다시 쓰기 전에 서버가 내려간 턴이 이렇게 남습니다. 다시 쓰다
    실패한 기록이 뒤에 있으면 답한 것으로 봅니다 — 그때는 사람에게 앞의 계획을 다시 보입니다.
    """
    waiting: Optional[str] = None
    for index in range(start, len(messages)):
        msg = messages[index]
        kind = turns.kind_of(msg)
        if kind == turns.KIND_PLAN_REVISION:
            waiting = revision_body(str(turns._get(msg, "content") or ""))  # noqa: SLF001
        elif kind == turns.KIND_PLAN:
            waiting = None
    return waiting


def revision_count(messages: Sequence[Any], start: int = 0) -> int:
    """이번 턴에 사람이 계획을 다시 쓰게 한 횟수."""
    return turns.count_kind(messages[start:], turns.KIND_PLAN_REVISION)
