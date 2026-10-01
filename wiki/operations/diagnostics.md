# Diagnostics — Finding Out What Froze

"Connection lost" means the browser's websocket got no answer in time: with `reconnect_timeout=30`
NiceGUI pings every 24 s and gives up after 12 s more. Three different things produce the same popup, and
each needs a different fix:

| What happened | What else you see | Where the evidence is |
| :--- | :--- | :--- |
| **The server's event loop was held** by synchronous work | `/api/health` in another tab hangs too | `data/diagnostics/stalls.log` — the stack of what held it |
| **The browser's main thread was busy** (a huge redraw) | the tab does not scroll or respond; `/api/health` answers | `client.log` — long tasks before the disconnect |
| **The network in between dropped it** (proxy, VPN, Wi-Fi) | both of the above are clean | `client.log` — a disconnect with no stall and no long tasks |

The code is [app/diagnostics.py](file:///d:/MultiAgentDebateOrchestration/app/diagnostics.py) (server) and
[app/ui/diagnostics_script.py](file:///d:/MultiAgentDebateOrchestration/app/ui/diagnostics_script.py)
(browser). Nothing is written while everything is healthy.

---

## 1. Server: event-loop stalls (`stalls.log`)

A task on the event loop updates a heartbeat every 0.1 s. A daemon thread checks it every 0.2 s; when the
heartbeat is more than `MADO_STALL_SECONDS` (default 1.0) late it reads the loop thread's current frame
(`sys._current_frames()`) and writes the stack. Python switches threads every 5 ms, so the watchdog runs even
while the loop is held — it sees the exact function holding it. A long stall is sampled again every 5 s
(`sample 2`, `sample 3`, …), which shows whether it sits in one place or moves. When the heartbeat comes back,
`event loop resumed after N s` is written.

```text
=== event loop stalled 1.3s (sample 1) — held by llm.py:1800 _complete_unthrottled
--- event loop thread ---
  File ".../app/agents/llm.py", line 1800, in _complete_unthrottled
    complete_response = litellm.stream_chunk_builder(chunks, messages=messages)
  ...
--- thread mado-loop-watchdog ...
```

**"loop thread idle".** If the loop thread is waiting in `select`/`_poll` and the heartbeat is still late, the
loop was not busy — it was not given a turn. Either another thread held the GIL, or the whole process was
paused (sleep, heavy swapping, a CPU starved by a local model). The record then includes the full stack of
every other thread.

The console gets one warning per stall (`Event loop stalled for 1.3s at … — stack written to …`), and
`GET /api/health` carries a summary:

```json
"event_loop": {"stall_threshold_seconds": 1.0, "stalls": 2, "longest_seconds": 14.2,
               "last": {"at": "2026-10-01 18:04:15", "seconds": 14.2, "where": "llm.py:1800 _complete_unthrottled"}}
```

`null` when diagnostics are off (`MADO_DIAGNOSTICS=0`).

---

## 2. Browser: long tasks and disconnects (`client.log`)

A script on the main page reports, over **HTTP** (`POST /api/diagnostics/client`, `fetch` with `keepalive`):

| Report | When |
| :--- | :--- |
| `disconnect` | socket.io dropped — with its reason: `ping timeout`, `transport close`, `transport error`, … |
| `reconnect` | it came back — with `down_ms` |
| `connect_error` | a reconnection attempt failed (at most one per 30 s) |
| `longtask` | one main-thread task over 1 s (at most one per 15 s) |
| `pagehide` | the page was left while disconnected — NiceGUI reloading it because it could not resume |

Every report carries the last 60 s of long tasks (`count`, `total`, `max`), the tab's visibility, the transport
(`websocket` or `polling`) and the user agent. Long tasks come from `PerformanceObserver('longtask')`
(Chromium); browsers without it are measured by how late a 0.5 s timer fires, only while the tab is visible
(hidden tabs' timers are throttled on purpose).

HTTP rather than the socket is the point: the most useful report is sent at the moment the socket is gone.
`delivered_after` is the gap between the browser's clock and the time the server received it; several seconds
mean the server could not take the request — a held loop, or a blocked network.

```text
2026-10-01 20:44:58 [disconnect] ip=10.0.0.5 delivered_after=13.8s reason=ping timeout visibility=visible
    page=/ transport=websocket longtasks_60s=count:1,total:71ms,max:71ms ua=Mozilla/5.0 …
```

Reports go through the same access control as the rest of `/api/*` (owner only, same origin). Bodies over
8 KB are refused, unknown kinds are dropped, and one address may send 60 a minute.

---

## 3. Files and settings

| Item | Value |
| :--- | :--- |
| Folder | `data/diagnostics/` (inside `data/`, not tracked by git) |
| Files | `stalls.log`, `client.log` — 2 MB each, rotated to `.1` and `.2` |
| `MADO_DIAGNOSTICS` | `0` turns the server watchdog off; browser reports are still recorded |
| `MADO_STALL_SECONDS` | stall threshold, default `1.0` |

Cost while healthy: one task waking every 0.1 s and one thread every 0.2 s on the server; one observer and a
socket listener in the page.

---

## 4. Reading them together

Match `client.log` disconnects to `stalls.log` by time:

1. A stall that starts before the disconnect and lasts over ~12 s → fix what the stack points at (move it off
   the loop with `asyncio.to_thread`, or make it cheaper).
2. No stall, but long tasks adding up to seconds before the disconnect → the page is drawing too much (see
   [UI Components §1.3.1](../ui/components.md)).
3. Neither, `reason=transport close` or `transport error`, `delivered_after` small after reconnecting → the network
   path; check proxies and VPNs between the browser and the server.

## Related pages

- [UI Components §1.3.1](../ui/components.md) — streaming without taking the page down
- [Getting Started](getting-started.md)
