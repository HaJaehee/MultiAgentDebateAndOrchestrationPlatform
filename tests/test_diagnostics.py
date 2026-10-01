"""화면이 멈출 때 원인을 남기는 진단 기록 (`app/diagnostics.py`, `app/ui/diagnostics_script.py`).

* 이벤트 루프가 붙잡히면 그 순간 **붙잡은 함수의 스택**이 `stalls.log` 에 남고, 풀리면 걸린 시간이 남습니다.
* 평소에는 아무것도 적지 않습니다.
* 브라우저 보고(긴 작업, 끊긴 사유)는 HTTP 로 받아 한 줄로 적습니다. 모양이 틀리거나 너무 잦으면 버립니다.
"""

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import diagnostics
from app.diagnostics import (
    LoopWatchdog,
    format_client_report,
    parse_client_body,
    record_client_report,
    reset_client_rate_limit,
)
from app.ui.diagnostics_script import CLIENT_DIAGNOSTICS_ENDPOINT, CLIENT_DIAGNOSTICS_JS

ROOT = Path(__file__).resolve().parents[1]


def _hold_the_loop(seconds: float) -> None:
    """이벤트 루프를 붙잡는 동기 작업 (스택에 이 이름이 남아야 합니다)."""
    time.sleep(seconds)


# ================================================================ 서버: 루프 정체


@pytest.mark.asyncio
async def test_a_stall_leaves_the_stack_of_what_held_the_loop(tmp_path: Path):
    dog = LoopWatchdog(0.3, log_dir=tmp_path, resample=0.5, check_interval=0.05)
    dog.start()
    await asyncio.sleep(0.2)
    _hold_the_loop(1.3)
    await asyncio.sleep(0.4)          # 풀린 것을 감시 스레드가 알아차릴 시간
    await dog.stop()

    log = (tmp_path / "stalls.log").read_text(encoding="utf-8")
    assert "event loop stalled" in log
    assert "_hold_the_loop" in log, "붙잡은 함수가 스택에 있어야 원인을 찾습니다"
    assert "held by" in log and "loop thread idle" not in log
    assert "(sample 2)" in log, "길어지면 다시 남겨 같은 자리에 머무는지 보입니다"
    assert "event loop resumed after" in log
    assert dog.summary.count == 1 and dog.summary.longest >= 1.0
    assert "_hold_the_loop" in dog.summary.last_where


@pytest.mark.asyncio
async def test_a_healthy_loop_writes_nothing(tmp_path: Path):
    dog = LoopWatchdog(0.3, log_dir=tmp_path, check_interval=0.05)
    dog.start()
    for _ in range(10):
        await asyncio.sleep(0.05)
    await dog.stop()
    assert not (tmp_path / "stalls.log").exists()
    assert dog.summary.as_dict()["stalls"] == 0 and dog.summary.as_dict()["last"] is None


def test_an_idle_loop_thread_points_at_other_threads():
    """루프 스레드가 `select` 에서 기다리는데도 박동이 늦었다면, 범인은 GIL 을 쥔 다른 스레드입니다."""
    idle = SimpleNamespace(f_code=SimpleNamespace(co_name="_select"))
    busy = SimpleNamespace(f_code=SimpleNamespace(co_name="stream_chunk_builder"))
    assert diagnostics._is_idle(idle) and not diagnostics._is_idle(busy)


def test_the_watchdog_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("MADO_DIAGNOSTICS", "0")
    assert diagnostics.diagnostics_enabled() is False
    monkeypatch.setenv("MADO_DIAGNOSTICS", "1")
    assert diagnostics.diagnostics_enabled() is True


# ================================================================ 브라우저 보고


def _report(**extra):
    return {"kind": "disconnect", "reason": "ping timeout", "at": 1_000_000.0, "visibility": "visible",
            "page": "/", "transport": "websocket",
            "longtasks": {"count": 3, "total_ms": 2400, "max_ms": 1500}, **extra}


def test_a_disconnect_becomes_one_line_with_its_reason_and_delay():
    line = format_client_report(_report(), "10.0.0.5", received_ms=1_000_000.0 + 14_000)
    assert line.startswith("[disconnect] ip=10.0.0.5")
    assert "reason=ping timeout" in line and "transport=websocket" in line
    assert "delivered_after=14.0s" in line, "늦게 도착했다면 그동안 서버가 받지 못한 것입니다"
    assert "longtasks_60s=count:3,total:2400ms,max:1500ms" in line


def test_unknown_or_malformed_reports_are_dropped(tmp_path: Path):
    reset_client_rate_limit()
    assert not record_client_report({"kind": "hack"}, "1.2.3.4", log_dir=tmp_path)
    assert not record_client_report(["x"], "1.2.3.4", log_dir=tmp_path)
    assert record_client_report(_report(), "1.2.3.4", log_dir=tmp_path)
    assert "[disconnect]" in (tmp_path / "client.log").read_text(encoding="utf-8")


def test_reports_are_rate_limited_per_address(tmp_path: Path):
    reset_client_rate_limit()
    stamp = 5_000.0
    results = [record_client_report(_report(kind="longtask"), "9.9.9.9", log_dir=tmp_path, now=stamp)
               for _ in range(diagnostics.MAX_CLIENT_REPORTS_PER_MINUTE + 5)]
    assert results.count(True) == diagnostics.MAX_CLIENT_REPORTS_PER_MINUTE
    assert record_client_report(_report(kind="longtask"), "9.9.9.9", log_dir=tmp_path, now=stamp + 61)
    assert record_client_report(_report(kind="longtask"), "8.8.8.8", log_dir=tmp_path, now=stamp)


def test_bodies_are_checked_before_reading():
    assert parse_client_body(b"x" * (diagnostics.MAX_CLIENT_REPORT_BYTES + 1))[1] == "too large"
    assert parse_client_body(b"{nope")[1] == "not json"
    assert parse_client_body(b"[1]")[1] == "not an object"
    assert parse_client_body(json.dumps(_report()).encode())[0]["kind"] == "disconnect"


def _request(body: bytes) -> Request:
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": CLIENT_DIAGNOSTICS_ENDPOINT, "query_string": b"",
             "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                         (b"user-agent", b"TestBrowser/1.0")],
             "client": ("127.0.0.1", 5555)}
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_the_endpoint_records_and_answers_204(tmp_path: Path, monkeypatch):
    from app.main import diagnostics_client

    reset_client_rate_limit()
    monkeypatch.setattr(diagnostics, "diagnostics_dir", lambda: tmp_path)
    response = await diagnostics_client(_request(json.dumps(_report()).encode()))
    assert response.status_code == 204
    line = (tmp_path / "client.log").read_text(encoding="utf-8")
    assert "[disconnect] ip=127.0.0.1" in line and "ua=TestBrowser/1.0" in line
    assert (await diagnostics_client(_request(b"{nope"))).status_code == 400


def test_health_reports_the_loop_summary():
    src = io.open(ROOT / "app" / "main.py", encoding="utf-8").read()
    assert '"event_loop": loop_health()' in src
    assert "start_loop_watchdog()" in src and "await stop_loop_watchdog()" in src


# ================================================================ 페이지 스크립트


def test_the_page_script_reports_long_tasks_and_disconnects_over_http():
    js = CLIENT_DIAGNOSTICS_JS
    assert "PerformanceObserver" in js and "'longtask'" in js
    assert "visibilityState === 'visible'" in js, "API 가 없는 브라우저는 보이는 탭에서만 타이머로 어림잡습니다"
    assert "socket.on('disconnect'" in js and "socket.on('connect'" in js and "connect_error" in js
    assert "keepalive: true" in js and CLIENT_DIAGNOSTICS_ENDPOINT in js, "끊긴 순간에도 보낼 수 있게 HTTP 로"
    app_src = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
    assert "<script>{CLIENT_DIAGNOSTICS_JS}</script>" in app_src
