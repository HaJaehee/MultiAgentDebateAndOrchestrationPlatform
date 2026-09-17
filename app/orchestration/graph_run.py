"""그래프 토론이 어디까지 돌았나 — 화면에 그릴 실행 상태.

같은 그림을 세 곳이 그립니다.

1. **도는 중인 화면** — 엔진 이벤트를 하나씩 받아 갱신합니다.
2. **새로고침한 화면** — 러너 스냅샷(`TurnRun.graph`)과 DB 기록에서 다시 만듭니다.
3. **끝난 대화를 다시 연 화면** — DB 기록과 `sessions.graph_snapshot` 에서 다시 만듭니다.

세 경로가 서로 다른 그림을 그리지 않도록, 상태의 근거는 **발언 기록 하나**입니다. 노드가 낸 발언에는
`graph_node_id` 와 나간 핀(`graph_port`)이 적혀 있어, 방문 횟수 · 판정 갈래 · 흐른 선을 모두 거기서
셉니다. 기록으로 알 수 없는 것은 "지금 돌고 있는 노드" 와 "왜 멈췄나" 뿐이고, 그 둘만 이벤트로 받습니다.

UI 를 모르는 순수 코드입니다 — 러너도 import 하고, 테스트는 LLM 없이 돕니다.
"""

from typing import Any, Dict, Iterable, List, Mapping, Optional

# 방문 상한 안내처럼 노드에 붙었지만 출력이 아닌 기록은 핀이 없습니다.
OUTPUT_PORTS = ("out", "yes", "no")
DECISION_LABELS = {"yes": "예", "no": "아니오"}


def _get(msg: Any, key: str, default: Any = None) -> Any:
    if isinstance(msg, Mapping):
        return msg.get(key, default)
    return getattr(msg, key, default)


def is_turn_closer(msg: Any) -> bool:
    """턴을 마무리한 합성 발언인가 (`MessageModel.turn_started_at` 이 그 표시)."""
    return _get(msg, "turn_started_at") is not None


def last_graph_turn(messages: List[Any], running: bool) -> List[Any]:
    """실행 표시에 쓸 턴의 발언.

    도는 중이면 마지막 합성 뒤(이번 턴)만 봅니다 — 아직 노드가 하나도 돌지 않았으면 빈 목록이고, 지난 턴의
    그림이 새 턴 위에 남지 않습니다. 끝났으면 노드 발언이 있는 가장 최근 턴입니다.
    """
    closers = [i for i, m in enumerate(messages) if is_turn_closer(m)]
    tail_start = closers[-1] + 1 if closers else 0
    tail = messages[tail_start:]
    if running or any(_get(m, "graph_node_id") for m in tail):
        return tail
    bounds = [-1] + closers
    for end, start in zip(reversed(bounds[1:]), reversed(bounds[:-1])):
        turn = messages[start + 1:end + 1]
        if any(_get(m, "graph_node_id") for m in turn):
            return turn
    return []


class GraphRunTracker:
    """한 턴의 그래프 실행 상태.

    `spec` 은 그 턴에 실제로 돈 그래프(`GraphSpec.dump()` 모양)입니다. 파일은 턴 도중에도 고칠 수 있으므로
    화면은 파일이 아니라 이것을 그립니다.
    """

    def __init__(self, spec: Optional[Dict[str, Any]] = None, max_steps: int = 0):
        self.spec: Optional[Dict[str, Any]] = spec
        self.max_steps = max_steps
        self.step = 0
        # 지금 단계에서 아직 출력을 내지 않은 노드.
        self.running: List[str] = []
        # None 이면 아직 도는 중(또는 모름). end · idle · step_cap · stopped.
        self.finished: Optional[str] = None
        # 노드에 붙은 기록 {발언 id: {node, port, error, step}}. 스트리밍 시작과 확정이 같은 id 로 옵니다.
        self._records: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ 만들기

    @classmethod
    def from_history(
        cls,
        spec: Optional[Dict[str, Any]],
        messages: Iterable[Any],
        *,
        state: Optional[Mapping[str, Any]] = None,
    ) -> "GraphRunTracker":
        """기록(한 턴 분량)과, 있으면 러너의 진행 상태(`to_state()`)로 다시 만듭니다."""
        tracker = cls(spec)
        for msg in messages:
            tracker.observe_message(msg)
        if state:
            tracker.max_steps = int(state.get("max_steps") or 0)
            tracker.step = max(tracker.step, int(state.get("step") or 0))
            tracker.finished = state.get("finished")
            # 러너의 목록을 그대로 믿습니다. 기록에서 거르면 스트리밍 중인 발언(핀이 이미 적혀 있음)의 노드가
            # 다 끝난 것으로 보입니다.
            tracker.running = list(state.get("running") or [])
        return tracker

    def to_state(self) -> Dict[str, Any]:
        """러너 스냅샷에 싣는 것. 발언은 스냅샷에 따로 있으므로 빼고, 기록으로 알 수 없는 것만."""
        return {
            "spec": self.spec,
            "max_steps": self.max_steps,
            "step": self.step,
            "running": list(self.running),
            "finished": self.finished,
        }

    # ------------------------------------------------------------ 갱신

    def observe(self, event: Mapping[str, Any]) -> bool:
        """엔진 이벤트 하나를 반영합니다. 화면을 다시 그려야 하면 True."""
        etype = event.get("type")
        if etype == "graph_started":
            self.__init__(event.get("spec"), int(event.get("max_steps") or 0))
            return True
        if etype == "graph_step_started":
            self.step = int(event.get("step") or 0)
            self.max_steps = int(event.get("max_steps") or self.max_steps)
            self.running = [n.get("id") for n in event.get("nodes") or [] if n.get("id")]
            return True
        if etype == "message_stream_start":
            return self.observe_message(event.get("message") or {})
        if etype == "message_added":
            return self.finish_message(event.get("message") or {})
        if etype == "graph_finished":
            self.finished = event.get("reason") or "end"
            self.running = []
            return True
        if etype in ("turn_completed", "run_finished"):
            changed = bool(self.running)
            self.running = []
            return changed
        return False

    def observe_message(self, msg: Any) -> bool:
        node_id = _get(msg, "graph_node_id")
        msg_id = _get(msg, "id")
        if not node_id or not msg_id:
            return False
        port = _get(msg, "graph_port")
        step = int(_get(msg, "round_number", 0) or 0)
        record = {
            "node": node_id,
            "port": port if port in OUTPUT_PORTS else None,
            "error": _get(msg, "msg_type") == "error",
            "step": step,
        }
        if self._records.get(msg_id) == record:
            return False
        self._records[msg_id] = record
        self.step = max(self.step, step)
        return True

    def finish_message(self, msg: Any) -> bool:
        """확정된 발언. 출력이면 그 노드는 이번 단계에서 더 돌지 않습니다 (스트리밍 시작은 아직 도는 중)."""
        changed = self.observe_message(msg)
        node_id = _get(msg, "graph_node_id")
        if node_id in self.running and _get(msg, "graph_port") in OUTPUT_PORTS:
            self.running = [n for n in self.running if n != node_id]
            changed = True
        return changed

    # ------------------------------------------------------------ 읽기

    def view(self) -> Dict[str, Any]:
        """캔버스에 넘기는 그림 (`graph_canvas.js` 의 `setRun`).

        nodes: {id: {state, visits, decision, capped}} — state 는 running · done · error · idle.
        edges: {id: taken | active | idle} — taken 은 한 번이라도 흐른 선, active 는 지금 도는 노드로 들어간 선.
        """
        spec = self.spec or {}
        records = list(self._records.values())
        running = set(self.running)
        nodes: Dict[str, Dict[str, Any]] = {}
        taken_ports: Dict[str, set] = {}
        for node in spec.get("nodes") or []:
            node_id = node.get("id")
            outputs = [r for r in records if r["node"] == node_id and r["port"]]
            last = outputs[-1] if outputs else None
            if node_id in running:
                state = "running"
            elif last is not None:
                state = "error" if last["error"] else "done"
            else:
                state = "idle"
            nodes[node_id] = {
                "state": state,
                "visits": len(outputs),
                "decision": last["port"] if last is not None and node.get("type") == "gate" else None,
                "capped": any(r["node"] == node_id and not r["port"] for r in records),
            }
            taken_ports[node_id] = {r["port"] for r in outputs}
        # 시작 노드는 기록을 남기지 않지만(요청과 계획이 곧 출력) 턴이 돌았다면 흐른 것입니다.
        started = bool(records) or bool(running) or self.step > 0
        for node in spec.get("nodes") or []:
            if node.get("type") == "start" and started:
                nodes[node["id"]]["state"] = "done"
                nodes[node["id"]]["visits"] = 1
                taken_ports[node["id"]] = {"out"}
        # 최종 합성 노드는 발언 대신 "닿았다" 로 끝납니다. 엔진은 입력이 도착하는 즉시 멈추므로, 들어오는 선이
        # (모두 기다림이면 전부) 흘렀으면 닿은 것입니다 — 다시 연 화면도 기록만으로 알 수 있습니다.
        if self.finished in (None, "end"):
            for node in spec.get("nodes") or []:
                if node.get("type") != "end":
                    continue
                incoming = [e for e in spec.get("edges") or [] if (e.get("to") or [None])[0] == node["id"]]
                flowed = [e["from"][1] in taken_ports.get(e["from"][0], set()) for e in incoming]
                reached = all(flowed) if node.get("wait") == "all" else any(flowed)
                if incoming and reached:
                    nodes[node["id"]]["state"] = "done"
                    nodes[node["id"]]["visits"] = 1

        edges: Dict[str, str] = {}
        for edge in spec.get("edges") or []:
            source, port = edge.get("from") or [None, None]
            target = (edge.get("to") or [None])[0]
            if port in taken_ports.get(source, set()):
                edges[edge.get("id")] = "active" if target in running else "taken"
            else:
                edges[edge.get("id")] = "idle"
        return {
            "has_run": started,
            "running": bool(running),
            "step": self.step,
            "max_steps": self.max_steps,
            "finished": self.finished,
            "nodes": nodes,
            "edges": edges,
        }

    def summary(self, active: bool = False) -> str:
        """그래프 미리보기 위에 한 줄로. `active` 는 턴이 아직 도는 중인지 — 단계 사이(장부 정리)나 합성 중에
        "N단계까지 실행" 이라고 쓰면 끝난 것처럼 읽힙니다."""
        view = self.view()
        if not view["has_run"]:
            return ""
        labels = node_labels(self.spec)
        if view["running"]:
            names = ", ".join(labels.get(n, n) for n in self.running)
            return f"{self.step}단계 진행 중 — {names}"
        reason = {
            "end": "최종 합성 노드에 닿음",
            "idle": "더 돌 노드가 없어 멈춤",
            "step_cap": "단계 상한에 닿음",
            "stopped": "사용자 요청으로 정지",
        }.get(self.finished or "", "")
        if active:
            if self.finished is None:
                return f"{self.step}단계 마침 — 다음 단계 준비 중"
            return f"{self.step}단계에서 끝남" + (f" · {reason}" if reason else "") + " — 최종 합성 중"
        return f"{self.step}단계까지 실행" + (f" · {reason}" if reason else "")


def node_labels(spec: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """{노드 id: 화면 이름}. `GraphNode.display` 와 같은 규칙."""
    from app.orchestration.graph import NODE_TYPE_LABELS

    out: Dict[str, str] = {}
    for node in (spec or {}).get("nodes") or []:
        label = (node.get("label") or "").strip()
        out[node.get("id")] = label or node.get("agent") or NODE_TYPE_LABELS.get(node.get("type"), node.get("type"))
    return out


class NodeBadges:
    """발언 카드와 내보내기에 붙이는 노드 이름 — "구현 · 2회차", "판정 · 아니오".

    몇 번째 방문인지는 기록 순서대로 셉니다. 합성 발언(턴의 끝)에서 다시 1부터 셉니다. 같은 발언이 스트리밍
    시작과 확정으로 두 번 와도 처음 매긴 번호를 그대로 돌려줍니다.
    """

    def __init__(self, labels: Optional[Mapping[str, str]] = None):
        self.labels: Dict[str, str] = dict(labels or {})
        self._counts: Dict[str, int] = {}
        self._assigned: Dict[str, int] = {}

    def set_labels(self, labels: Mapping[str, str]) -> None:
        self.labels.update(labels)

    def reset(self) -> None:
        self._counts.clear()
        self._assigned.clear()

    def observe(self, msg: Any) -> Optional[str]:
        node_id = _get(msg, "graph_node_id")
        if not node_id:
            if is_turn_closer(msg):
                self._counts.clear()
            return None
        label = self.labels.get(node_id) or node_id
        port = _get(msg, "graph_port")
        if port not in OUTPUT_PORTS:
            return f"{label} · 방문 상한"
        msg_id = _get(msg, "id") or ""
        visit = self._assigned.get(msg_id)
        if visit is None:
            visit = self._counts.get(node_id, 0) + 1
            self._counts[node_id] = visit
            if msg_id:
                self._assigned[msg_id] = visit
        if port in DECISION_LABELS:
            return f"{label} · {DECISION_LABELS[port]}" + (f" ({visit}회차)" if visit > 1 else "")
        return f"{label} · {visit}회차"
