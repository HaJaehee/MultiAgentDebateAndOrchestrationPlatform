"""서버가 응답하기 시작하면 기본 브라우저로 UI 를 엽니다.

실행 스크립트(`run_mado.bat` / `run_mado.ps1`)가 서버를 띄우기 직전에 이
스크립트를 백그라운드로 함께 띄웁니다. 곧바로 브라우저를 열면 아직 포트가 열리기
전이라 "연결할 수 없음" 이 뜨므로, 여기서 포트가 응답할 때까지 기다렸다가 엽니다.

서버 쪽(`app.main`)에서 열지 않는 이유는 두 가지입니다.

* `conf.json` 의 `"debug": true` 면 uvicorn 이 리로드 모드로 돌고, 파일이 바뀔
  때마다 생명주기가 다시 실행됩니다. 거기서 열면 저장할 때마다 탭이 열립니다.
* 서버는 콘솔을 붙잡고 있어야 로그가 보이고 Ctrl+C 로 멈출 수 있습니다. 그 앞뒤로
  브라우저를 여는 일은 실행 스크립트의 몫입니다.

주소는 `conf.json` 의 `app` 을 그대로 읽고, 실행 스크립트가 `app.main` 에 넘긴
인자(`--host` / `--port`)가 있으면 그쪽을 따릅니다. 두 곳에 같은 값을 적어 두면
반드시 어긋나기 때문입니다.

    python open_browser.py [app.main 에 넘긴 것과 같은 인자들]

환경변수:
    MADO_NO_BROWSER        비어 있지 않으면 아무것도 하지 않고 끝냅니다.
    MADO_BROWSER_TIMEOUT   서버를 기다리는 최대 시간(초). 기본 90.
"""

import os
import socket
import sys
import time
import webbrowser
from typing import List, Optional, Tuple

DEFAULT_TIMEOUT = 90.0
POLL_INTERVAL = 0.4

# 바인딩 주소이지 접속 주소가 아닌 값들. 이대로 브라우저에 넣으면 열리지 않습니다.
WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]", ""}


def _arg_value(argv: List[str], *names: str) -> Optional[str]:
    """`--port 9000` 과 `--port=9000` 을 모두 읽습니다."""
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
    return None


def resolve_target(argv: List[str]) -> Optional[Tuple[str, int]]:
    """브라우저로 열 (host, port). 알아낼 수 없으면 None."""
    host: Optional[str] = None
    port: Optional[int] = None

    try:
        from app.config import get_config

        app_cfg = get_config().app
        host, port = app_cfg.host, app_cfg.port
    except Exception:
        # 설정을 못 읽어도 인자로 받은 값이 있으면 그걸로 진행합니다.
        pass

    override_host = _arg_value(argv, "--host")
    if override_host:
        host = override_host
    override_port = _arg_value(argv, "--port", "-p")
    if override_port:
        try:
            port = int(override_port)
        except ValueError:
            pass

    if not port:
        return None
    if host in WILDCARD_HOSTS or host is None:
        # 모든 인터페이스에 바인딩한 경우. 이 기계에서 여는 것이므로 루프백으로.
        host = "127.0.0.1"
    return host, int(port)


def wait_for_server(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(POLL_INTERVAL)
    return False


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if os.environ.get("MADO_NO_BROWSER", "").strip():
        return 0

    target = resolve_target(argv)
    if target is None:
        print("[open_browser] 접속 주소를 알 수 없어 브라우저를 열지 않습니다.", file=sys.stderr)
        return 1

    host, port = target
    try:
        timeout = float(os.environ.get("MADO_BROWSER_TIMEOUT", "") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    if not wait_for_server(host, port, timeout):
        # 서버가 뜨지 못한 경우입니다. 콘솔에는 그쪽 오류가 이미 찍혀 있으므로
        # 여기서는 조용히 물러납니다.
        print(f"[open_browser] {timeout:.0f}초 안에 서버가 응답하지 않아 브라우저를 열지 않았습니다.",
              file=sys.stderr)
        return 1

    url = f"http://{host}:{port}"
    print(f"[open_browser] 브라우저를 엽니다: {url}")
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 브라우저가 없어도 서버는 계속 돕니다
        print(f"[open_browser] 브라우저를 열지 못했습니다({exc}). 주소로 직접 접속하세요: {url}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
