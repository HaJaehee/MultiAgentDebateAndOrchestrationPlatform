"""이벤트 이름표(`app.events.EventType`)가 유일한 출처인지.

이벤트 이름을 문자열로 흩어 두면 보내는 쪽만 바뀌어도 받는 쪽은 아무 오류 없이 그 이벤트를
무시합니다. 그래서 이름은 `EventType` 으로만 쓰고, 쓰지 않는 이름은 남기지 않습니다.
"""

import re
from pathlib import Path

from app.events import DECISION_RESOLVED, EventType
from app.orchestration.turn_run import _REDUCERS

APP = Path(__file__).resolve().parents[1] / "app"
NAMES = {e.value for e in EventType}


def _sources():
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "events.py":
            continue
        yield path, path.read_text(encoding="utf-8")


def test_event_names_are_never_spelled_out_as_strings():
    pattern = re.compile(r'("type"\]?\s*[:=]=?\s*|etype\s*[!=]=\s*|\.get\("type"\)\s*[!=]=\s*)"([a-z_]+)"')
    spelled = [
        f"{path.relative_to(APP.parent)}: {m.group(0)}"
        for path, text in _sources()
        for m in pattern.finditer(text)
        if m.group(2) in NAMES
    ]
    assert not spelled, "이벤트 이름은 EventType 으로 씁니다:\n" + "\n".join(spelled)


def test_every_event_type_is_sent_somewhere():
    used = {m for _, text in _sources() for m in re.findall(r"EventType\.([A-Z_]+)", text)}
    used |= {e.name for e in DECISION_RESOLVED.values()}
    unused = sorted(e.name for e in EventType if e.name not in used)
    assert not unused, f"아무도 쓰지 않는 이벤트 이름: {unused}"


def test_snapshot_reducers_only_handle_known_events():
    assert set(_REDUCERS) <= NAMES
    # 쪽지가 걸리고 풀리는 이벤트는 반드시 스냅샷에 남아야 새로고침한 화면이 같은 물음을 봅니다.
    for event in (EventType.TOOL_BUDGET_EXHAUSTED, EventType.CONTEXT_WINDOW_EXHAUSTED,
                  EventType.TOOL_APPROVAL_REQUESTED, EventType.PLAN_APPROVAL_REQUESTED,
                  *DECISION_RESOLVED.values(), EventType.TURN_COMPLETED):
        assert event in _REDUCERS, event
