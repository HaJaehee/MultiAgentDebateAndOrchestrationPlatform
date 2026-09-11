"""기록된 시각을 사람이 읽는 말로 옮기는 순수 함수들.

화면(`ChatFeed`), 저장 문서(`app.export`), 합성 보고서(`OrchestratorEngine`)가 같은
규칙으로 시각을 적도록 한 곳에 둡니다. 셋이 각자 적으면 "화면에는 12초인데 문서에는
11초" 같은 어긋남이 생깁니다.

**이 모듈은 앱의 다른 모듈을 import 하지 않습니다.** 예전에는 이 함수들이
`app.export` 에 있었는데, 그 모듈은 전략 이름을 적으려고 `app.orchestration` 을
import 하고, 엔진이 다시 `app.export` 를 import 하면 순환이 됩니다. 순환은 import
순서에 따라 드러났다 숨었다 해서 — `app.main` 은 우연히 안전한 순서였습니다 — 누군가
`app.export` 를 먼저 부르는 날에야 기동 실패로 나타납니다.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def to_local(value: datetime) -> datetime:
    """기록된 시각을 이 기계의 시간대로 옮깁니다.

    기록은 UTC 로 적지만 SQLite 는 오프셋을 버리므로, 읽어 오면 시간대가 없는
    UTC 벽시계입니다. 그대로 찍으면 한국에서는 9시간 전으로 보입니다.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone()


def _as_datetime(value: Any) -> Optional[datetime]:
    """datetime 이나 ISO 문자열을 이 기계 시간대의 datetime 으로. 없거나 못 읽으면 None."""
    if value is None or value == "":
        return None
    try:
        return to_local(value)
    except (TypeError, ValueError):
        return None


def format_duration(seconds: float) -> str:
    """걸린 시간을 사람이 읽는 말로. 1분이 안 되면 초만, 넘으면 분과 초."""
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        # 짧은 발언은 소수점 한 자리가 의미 있습니다 (0.4초와 4초는 다른 이야기).
        return f"{seconds:.1f}초"
    total = int(round(seconds))
    if total < 60:
        return f"{total}초"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}분 {secs}초" if secs else f"{minutes}분"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"


def speech_timing(msg: Dict[str, Any]) -> Dict[str, Any]:
    """발언 하나의 시각을 읽기 좋게 풀어 둡니다.

    돌려주는 값::

        {"started": datetime | None, "finished": datetime | None,
         "seconds": float | None, "turn_seconds": float | None,
         "instant": bool, "legacy": bool}

    - `instant` — 시작과 끝이 같습니다 (사람 발언, 지명 기록처럼 걸리는 시간이 없는 것).
      한 시각만 적습니다.
    - `legacy` — 시작·종료 컬럼이 생기기 전의 발언입니다. `created_at` 하나뿐인데
      그것은 **발언이 끝난 뒤 들어간 정렬 키**라 시작인지 끝인지 말할 수 없으므로,
      "시작" 이나 "종료" 라는 이름을 붙이지 않고 시각만 적습니다.
    - 시작만 있고 끝이 없으면 아직 진행 중인 발언입니다.
    - `turn_seconds` — 이 발언이 **한 턴을 마무리한 합성 발언**이면 그 턴의 총 경과
      시간 (`turn_started_at` 부터 이 발언이 끝날 때까지). 다른 발언은 None 입니다.
    """
    started = _as_datetime(msg.get("started_at"))
    finished = _as_datetime(msg.get("finished_at"))
    if started is None and finished is None:
        legacy = _as_datetime(msg.get("created_at"))
        return {"started": legacy, "finished": None, "seconds": None, "turn_seconds": None,
                "instant": True, "legacy": legacy is not None}

    seconds = (finished - started).total_seconds() if started and finished else None
    return {
        "started": started,
        "finished": finished,
        "seconds": seconds,
        "turn_seconds": turn_elapsed_seconds(msg.get("turn_started_at"), finished),
        # 밀리초 아래의 차이는 같은 순간으로 봅니다 — 한 함수 안에서 두 번 잰 값입니다.
        "instant": seconds is not None and seconds < 0.001,
        "legacy": False,
    }


def turn_elapsed_seconds(turn_started_at: Any, completed_at: Any) -> Optional[float]:
    """한 턴의 총 경과 시간(초). 둘 중 하나라도 없으면 None.

    턴의 시작은 **그 턴을 연 사람 요청이 기록된 시각**입니다. 사람이 전송을 누른
    순간이 아닙니다 — 그 사이에 MCP 런타임이 뜨는 몇 초가 있습니다. 기록된 시각을
    쓰는 이유는, 대화 기록에서 "요청 시각 → 마지막 발언 종료" 를 읽는 사람이 같은
    값을 얻어야 하기 때문입니다. 기록에 없는 구간을 섞으면 문서가 스스로와 어긋납니다.
    """
    start = _as_datetime(turn_started_at)
    end = _as_datetime(completed_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def report_completed_line(completed_at: Any, turn_started_at: Any = None) -> str:
    """종합 보고서 끝에 붙일 완료 시각 한 줄. 시각이 없으면 빈 문자열.

    보고서는 아티팩트로 따로 복사·저장되어 대화 기록과 떨어져 돌아다닙니다. 그래서
    언제 나온 결론인지를 보고서 **본문에** 적어 둡니다 — 화면의 발언 카드나 저장
    문서의 머리말에만 있으면, 보고서만 떼어 온 사람은 그 시각을 알 수 없습니다.

    `turn_started_at` 이 있으면 완료 시각 오른쪽에 그 턴의 총 경과 시간을 붙입니다.
    """
    at = _as_datetime(completed_at)
    if at is None:
        return ""
    line = f"보고서 완료: {at.strftime('%Y-%m-%d %H:%M:%S')}"
    total = turn_elapsed_seconds(turn_started_at, at)
    if total is not None:
        line += f" · 총 경과 {format_duration(total)}"
    return f"*{line}*"


def speech_time_text(msg: Dict[str, Any]) -> str:
    """문서에 적을 발언 시각 한 줄. 시각이 전혀 없으면 빈 문자열.

    문서는 남에게 건네지고 날짜가 바뀐 뒤에도 읽히므로 날짜까지 늘 적습니다.

        시작 2026-09-11 10:56:22 · 종료 2026-09-11 10:58:27 · 경과 2분 5초
        시작 … · 종료 … · 경과 … · 총 경과 12분 5초      ← 턴을 마무리한 합성 발언
    """
    t = speech_timing(msg)
    stamp = "%Y-%m-%d %H:%M:%S"
    if t["started"] is None and t["finished"] is None:
        return ""
    if t["legacy"] or t["instant"]:
        return (t["started"] or t["finished"]).strftime(stamp)
    if t["finished"] is None:
        return f"시작 {t['started'].strftime(stamp)} · 종료 기록 없음"
    if t["started"] is None:
        return f"종료 {t['finished'].strftime(stamp)}"
    text = (
        f"시작 {t['started'].strftime(stamp)} · 종료 {t['finished'].strftime(stamp)}"
        f" · 경과 {format_duration(t['seconds'])}"
    )
    # 한 턴을 마무리한 합성 발언이면 그 턴의 총 경과를 종료·경과 오른쪽에 붙입니다.
    if t["turn_seconds"] is not None:
        text += f" · 총 경과 {format_duration(t['turn_seconds'])}"
    return text
