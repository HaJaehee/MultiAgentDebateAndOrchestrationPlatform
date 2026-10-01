"""화면이 멈추거나 "Connection lost" 가 뜰 때 원인을 남기는 진단 기록.

"Connection lost" 는 브라우저의 웹소켓이 정해진 시간(`reconnect_timeout=30` 이면 12초) 안에 답을
받지 못했다는 뜻입니다. 원인은 셋 중 하나입니다.

1. **서버의 이벤트 루프가 붙잡혔다.** 어떤 동기 작업이 루프를 놓아 주지 않으면 화면 갱신도, 웹소켓
   핑도, `/api/health` 도 모두 멈춥니다. → 루프에 0.1초 박동을 두고, 별도 스레드가 박동이 늦어지는
   순간 **루프 스레드의 스택**을 `stalls.log` 에 남깁니다. 어느 함수가 붙잡았는지가 그대로 보입니다.
2. **브라우저가 바빴다.** 큰 화면 갱신으로 메인 스레드가 오래 막혀도 같은 증상이 납니다. → 페이지
   스크립트(`app/ui/diagnostics_script.py`)가 긴 작업과 연결이 끊긴 사유를 `client.log` 로 보냅니다.
3. **그 사이의 네트워크가 끊었다.** → 위 둘이 다 깨끗한데 끊긴 기록만 있으면 이쪽입니다.

브라우저는 보고를 웹소켓이 아니라 **HTTP** 로 보냅니다. 소켓이 끊긴 바로 그 순간의 보고가 가장
중요한데, 그때 소켓으로는 보낼 수 없기 때문입니다. 서버가 붙잡혀 있었다면 보고는 늦게 도착하고, 그
지연(브라우저 시각과 받은 시각의 차이)도 함께 적습니다.

기록은 `data/diagnostics/` 에 남고 파일당 2MB 로 세 개까지 돌려 씁니다. 평소에는 아무것도 적지
않습니다 — 임계값을 넘은 일만 적습니다. `MADO_DIAGNOSTICS=0` 이면 감시를 띄우지 않습니다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def diagnostics_enabled() -> bool:
    return os.environ.get("MADO_DIAGNOSTICS", "1").strip().lower() not in ("0", "false", "no", "off")


# 루프 박동 간격과, 이만큼 늦으면 정체로 적는 기준(초).
HEARTBEAT_SECONDS = 0.1
STALL_SECONDS = _env_float("MADO_STALL_SECONDS", 1.0)
# 정체가 길어지면 이 간격마다 스택을 다시 남깁니다. 같은 자리에 머무는지, 옮겨 가는지가 보입니다.
RESAMPLE_SECONDS = 5.0
# 한 번의 스택에 남길 프레임 수 (안쪽부터).
STACK_FRAMES = 40

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2

# 브라우저 보고 한 건의 상한(바이트)과, 한 주소에서 1분에 받는 보고 수.
MAX_CLIENT_REPORT_BYTES = 8 * 1024
MAX_CLIENT_REPORTS_PER_MINUTE = 60

# 이벤트 루프가 일이 없을 때 머무는 함수. 루프 스레드가 여기 있는데도 박동이 늦었다면 루프가 바빴던
# 것이 아니라 **차례를 받지 못한** 것입니다 (다른 스레드가 GIL 을 쥐었거나, 프로세스가 멈췄음).
_IDLE_FRAMES = ("select", "_select", "_poll", "poll", "wait")


def diagnostics_dir() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "diagnostics"


_file_loggers: Dict[str, logging.Logger] = {}
_file_lock = threading.Lock()


def _file_logger(name: str, directory: Optional[Path] = None) -> logging.Logger:
    """`data/diagnostics/<name>.log` 에 적는 로거. 처음 쓸 때 만듭니다 (폴더도)."""
    directory = directory or diagnostics_dir()
    key = str(directory / name)
    with _file_lock:
        existing = _file_loggers.get(key)
        if existing is not None:
            return existing
        directory.mkdir(parents=True, exist_ok=True)
        log = logging.getLogger(f"mado.diagnostics.{name}.{len(_file_loggers)}")
        log.setLevel(logging.INFO)
        log.propagate = False
        handler = RotatingFileHandler(
            directory / f"{name}.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        _file_loggers[key] = log
        return log


# ---------------------------------------------------------------------------
# 서버: 이벤트 루프 정체
# ---------------------------------------------------------------------------


@dataclass
class StallSummary:
    count: int = 0
    longest: float = 0.0
    last_at: str = ""
    last_seconds: float = 0.0
    last_where: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stall_threshold_seconds": STALL_SECONDS,
            "stalls": self.count,
            "longest_seconds": round(self.longest, 1),
            "last": (
                {"at": self.last_at, "seconds": round(self.last_seconds, 1), "where": self.last_where}
                if self.count else None
            ),
        }


def _frame_label(frame: Any) -> str:
    code = frame.f_code
    return f"{Path(code.co_filename).name}:{frame.f_lineno} {code.co_name}"


def _is_idle(frame: Any) -> bool:
    return frame is not None and frame.f_code.co_name in _IDLE_FRAMES


def _innermost_app_frame(frame: Any) -> str:
    """스택에서 우리 코드(app/) 중 가장 안쪽 자리. 없으면 가장 안쪽 프레임."""
    innermost = _frame_label(frame) if frame is not None else "(no frame)"
    while frame is not None:
        if f"{os.sep}app{os.sep}" in frame.f_code.co_filename:
            return _frame_label(frame)
        frame = frame.f_back
    return innermost


class LoopWatchdog:
    """이벤트 루프의 박동을 지켜보다 늦어지면 그 순간의 스택을 남깁니다.

    박동은 루프 안의 태스크가, 감시는 별도의 데몬 스레드가 합니다. 루프가 붙잡혀 있어도 감시 스레드는
    GIL 을 번갈아 받으므로(파이썬은 5ms 마다 스레드를 바꿉니다) 붙잡고 있는 함수의 스택을 읽을 수
    있습니다. 비용은 0.1초마다 깨어나는 태스크 하나와 0.2초마다 깨어나는 스레드 하나입니다.
    """

    def __init__(
        self,
        threshold: float = STALL_SECONDS,
        *,
        log_dir: Optional[Path] = None,
        resample: float = RESAMPLE_SECONDS,
        check_interval: float = 0.2,
    ) -> None:
        self.threshold = threshold
        self.resample = resample
        self.check_interval = check_interval
        self.log_dir = log_dir
        self.summary = StallSummary()
        self._beat = time.monotonic()
        self._loop_thread: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -------------------------------------------------------------- 수명

    def start(self) -> None:
        """실행 중인 루프에서 부릅니다. 박동 태스크와 감시 스레드를 띄웁니다."""
        if self._task is not None:
            return
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heartbeat(), name="mado-loop-heartbeat")
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="mado-loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    async def _heartbeat(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(HEARTBEAT_SECONDS)

    # -------------------------------------------------------------- 감시

    def _watch(self) -> None:
        stalled_since: Optional[float] = None
        last_sample = 0.0
        sample_no = 0
        while not self._stop.wait(self.check_interval):
            now = time.monotonic()
            lag = now - self._beat
            if lag >= self.threshold:
                if stalled_since is None:
                    stalled_since = self._beat
                    sample_no = 1
                    last_sample = now
                    self._record_sample(lag, sample_no)
                elif now - last_sample >= self.resample:
                    sample_no += 1
                    last_sample = now
                    self._record_sample(lag, sample_no)
            elif stalled_since is not None:
                self._record_end(now - stalled_since)
                stalled_since = None

    def _record_sample(self, lag: float, sample_no: int) -> None:
        frames = sys._current_frames()  # noqa: SLF001 - 진단 목적으로만 읽습니다
        frame = frames.get(self._loop_thread) if self._loop_thread is not None else None
        idle = _is_idle(frame)
        where = _innermost_app_frame(frame)
        lines = [
            f"=== event loop stalled {lag:.1f}s (sample {sample_no}) — "
            + ("loop thread idle: it was not given a turn (another thread held the GIL, or the "
               "process was paused)" if idle else f"held by {where}"),
            "--- event loop thread ---",
            "".join(traceback.format_stack(frame, limit=STACK_FRAMES)).rstrip() if frame else "(no frame)",
        ]
        # 루프가 한가했는데 늦었다면 범인은 다른 스레드입니다. 그때는 모든 스레드의 스택을 남깁니다.
        names = {t.ident: t.name for t in threading.enumerate()}
        for ident, other in frames.items():
            if ident in (self._loop_thread, threading.get_ident()):
                continue
            label = f"--- thread {names.get(ident, ident)} ---"
            if idle:
                lines += [label, "".join(traceback.format_stack(other, limit=15)).rstrip()]
            else:
                lines.append(f"{label} {_frame_label(other)}")
        _file_logger("stalls", self.log_dir).info("\n".join(lines))
        if sample_no == 1:
            self.summary.last_where = "(loop thread idle)" if idle else where
            logger.warning(
                "Event loop stalled for %.1fs at %s — stack written to %s",
                lag, self.summary.last_where, (self.log_dir or diagnostics_dir()) / "stalls.log",
            )

    def _record_end(self, seconds: float) -> None:
        summary = self.summary
        summary.count += 1
        summary.longest = max(summary.longest, seconds)
        summary.last_seconds = seconds
        summary.last_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _file_logger("stalls", self.log_dir).info(
            f"=== event loop resumed after {seconds:.1f}s (stall #{summary.count})"
        )
        logger.warning("Event loop resumed after a %.1fs stall", seconds)


_watchdog: Optional[LoopWatchdog] = None


def start_loop_watchdog() -> Optional[LoopWatchdog]:
    """앱 기동 때 부릅니다. 꺼져 있으면 None."""
    global _watchdog
    if not diagnostics_enabled():
        return None
    if _watchdog is None:
        _watchdog = LoopWatchdog()
    _watchdog.start()
    return _watchdog


async def stop_loop_watchdog() -> None:
    if _watchdog is not None:
        await _watchdog.stop()


def loop_health() -> Optional[Dict[str, Any]]:
    """`/api/health` 에 싣는 요약. 감시가 꺼져 있으면 None."""
    return _watchdog.summary.as_dict() if _watchdog is not None else None


# ---------------------------------------------------------------------------
# 브라우저: 긴 작업과 연결 끊김
# ---------------------------------------------------------------------------

CLIENT_KINDS = ("disconnect", "reconnect", "connect_error", "longtask", "pagehide")

_client_seen: Dict[str, Deque[float]] = {}
_client_lock = threading.Lock()


def _rate_limited(ip: str, now: float) -> bool:
    with _client_lock:
        seen = _client_seen.setdefault(ip, deque())
        while seen and now - seen[0] > 60.0:
            seen.popleft()
        if len(seen) >= MAX_CLIENT_REPORTS_PER_MINUTE:
            return True
        seen.append(now)
        return False


def _clip(value: Any, limit: int = 120) -> str:
    text = str(value if value is not None else "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 제외


def format_client_report(report: Dict[str, Any], ip: str, received_ms: float) -> Optional[str]:
    """브라우저 보고 한 건을 한 줄로. 알 수 없는 종류면 None."""
    kind = str(report.get("kind") or "")
    if kind not in CLIENT_KINDS:
        return None
    parts: List[str] = [f"[{kind}]", f"ip={_clip(ip, 45)}"]
    sent_ms = _number(report.get("at"))
    if sent_ms is not None:
        # 브라우저 시계와 서버 시계가 다를 수 있으므로 참고값입니다. 몇 초씩 크면 그 사이 서버가
        # 보고를 받지 못했다는 뜻입니다 (루프가 붙잡혔거나 네트워크가 막혔음).
        parts.append(f"delivered_after={max(0.0, (received_ms - sent_ms) / 1000):.1f}s")
    for key in ("reason", "visibility", "page", "transport"):
        if report.get(key):
            parts.append(f"{key}={_clip(report.get(key), 60)}")
    for key in ("duration_ms", "down_ms"):
        number = _number(report.get(key))
        if number is not None:
            parts.append(f"{key}={number:.0f}")
    tasks = report.get("longtasks")
    if isinstance(tasks, dict):
        count, total, longest = (_number(tasks.get(k)) for k in ("count", "total_ms", "max_ms"))
        if count:
            parts.append(
                f"longtasks_60s=count:{count:.0f},total:{(total or 0):.0f}ms,max:{(longest or 0):.0f}ms"
            )
    if report.get("user_agent"):
        parts.append(f"ua={_clip(report.get('user_agent'), 80)}")
    return " ".join(parts)


def record_client_report(
    report: Any, ip: str, *, log_dir: Optional[Path] = None, now: Optional[float] = None,
) -> bool:
    """브라우저 보고를 `client.log` 에 적습니다. 적었으면 True (모양이 틀렸거나 너무 잦으면 False)."""
    if not isinstance(report, dict):
        return False
    stamp = time.time() if now is None else now
    if _rate_limited(ip or "?", stamp):
        return False
    line = format_client_report(report, ip, stamp * 1000)
    if line is None:
        return False
    _file_logger("client", log_dir).info(line)
    if report.get("kind") in ("disconnect", "connect_error"):
        logger.warning("Browser report: %s", line)
    return True


def reset_client_rate_limit() -> None:
    """테스트용."""
    with _client_lock:
        _client_seen.clear()


def parse_client_body(body: bytes) -> Tuple[Optional[Dict[str, Any]], str]:
    """요청 본문을 보고로 읽습니다. (보고, 오류) — 오류가 비어 있으면 성공."""
    import json

    if len(body) > MAX_CLIENT_REPORT_BYTES:
        return None, "too large"
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return None, "not json"
    if not isinstance(data, dict):
        return None, "not an object"
    return data, ""
