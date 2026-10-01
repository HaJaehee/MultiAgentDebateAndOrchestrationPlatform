"""브라우저 쪽 진단 보고 (서버 쪽은 `app/diagnostics.py`).

"Connection lost" 가 떴을 때 서버가 붙잡혔던 것인지, 브라우저가 바빴던 것인지, 그 사이 네트워크가
끊은 것인지를 가르려면 브라우저가 본 것도 있어야 합니다. 이 스크립트는 세 가지를 보냅니다.

* **긴 작업** — 메인 스레드를 오래 붙잡은 작업. Chromium 은 `PerformanceObserver('longtask')` 로
  정확히 잽니다. 그 API 가 없는 브라우저는 0.5초 타이머가 얼마나 늦게 깨는지로 어림잡습니다 (탭이
  보일 때만 — 숨은 탭의 타이머는 브라우저가 일부러 늦춥니다). 1초 넘는 것 하나는 바로 알리고,
  나머지는 최근 60초 요약으로 다른 보고에 실어 보냅니다.
* **연결 끊김과 복구** — NiceGUI 의 socket.io 클라이언트(`window.socket`)가 알려 주는 끊긴 사유
  (`ping timeout`, `transport close`, …)와 다시 붙기까지 걸린 시간.
* **끊긴 채로 페이지를 떠남** — 다시 붙지 못해 NiceGUI 가 페이지를 새로 고치는 경우.

보고는 웹소켓이 아니라 HTTP(`fetch` keepalive)로 보냅니다. 끊긴 순간의 보고를 소켓으로는 보낼 수
없기 때문입니다. 실패는 조용히 버립니다 — 진단이 화면을 방해하면 안 됩니다.
"""

CLIENT_DIAGNOSTICS_ENDPOINT = "/api/diagnostics/client"

CLIENT_DIAGNOSTICS_JS = """
(function () {
    if (window.MadoDiagnostics) return;

    var ENDPOINT = '""" + CLIENT_DIAGNOSTICS_ENDPOINT + """';
    var REPORT_LONG_MS = 1000;      // 이보다 긴 작업 하나는 바로 알립니다
    var KEEP_MS = 60000;            // 요약에 담는 기간
    var tasks = [];                 // [끝난 시각(ms), 길이(ms)]
    var lastLongReport = 0;
    var lastConnectError = 0;
    var downSince = null;

    function now() { return Date.now(); }

    function prune() {
        var limit = now() - KEEP_MS;
        while (tasks.length && tasks[0][0] < limit) tasks.shift();
    }

    function summary() {
        prune();
        var total = 0, max = 0;
        for (var i = 0; i < tasks.length; i++) { total += tasks[i][1]; if (tasks[i][1] > max) max = tasks[i][1]; }
        return { count: tasks.length, total_ms: Math.round(total), max_ms: Math.round(max) };
    }

    function transport() {
        try { return window.socket.io.engine.transport.name; } catch (e) { return ''; }
    }

    function send(kind, extra) {
        var body = { kind: kind, at: now(), page: location.pathname, visibility: document.visibilityState,
                     transport: transport(), longtasks: summary() };
        for (var key in (extra || {})) body[key] = extra[key];
        try {
            fetch(ENDPOINT, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                              body: JSON.stringify(body), keepalive: true, credentials: 'same-origin' })
                .catch(function () {});
        } catch (e) { /* 진단은 화면을 방해하지 않습니다 */ }
    }

    function noteTask(duration) {
        tasks.push([now(), duration]);
        if (duration >= REPORT_LONG_MS && now() - lastLongReport > 15000) {
            lastLongReport = now();
            send('longtask', { duration_ms: Math.round(duration) });
        }
    }

    var observed = false;
    try {
        new PerformanceObserver(function (list) {
            list.getEntries().forEach(function (entry) { noteTask(entry.duration); });
        }).observe({ type: 'longtask', buffered: true });
        observed = true;
    } catch (e) { observed = false; }

    if (!observed) {
        var expected = now() + 500;
        setInterval(function () {
            var late = now() - expected;
            expected = now() + 500;
            if (document.visibilityState === 'visible' && late >= 200) noteTask(late);
        }, 500);
    }

    function attach(socket) {
        socket.on('disconnect', function (reason) {
            downSince = now();
            send('disconnect', { reason: String(reason || '') });
        });
        socket.on('connect', function () {
            if (downSince === null) return;
            send('reconnect', { down_ms: now() - downSince });
            downSince = null;
        });
        socket.on('connect_error', function (err) {
            if (now() - lastConnectError < 30000) return;
            lastConnectError = now();
            send('connect_error', { reason: String((err && err.message) || err || '') });
        });
    }

    var waited = 0;
    var finder = setInterval(function () {
        waited += 500;
        if (window.socket && typeof window.socket.on === 'function') {
            clearInterval(finder);
            attach(window.socket);
        } else if (waited > 120000) {
            clearInterval(finder);
        }
    }, 500);

    window.addEventListener('pagehide', function () {
        if (downSince !== null) send('pagehide', { reason: 'left while disconnected', down_ms: now() - downSince });
    });

    window.MadoDiagnostics = { summary: summary, send: send };
})();
"""
