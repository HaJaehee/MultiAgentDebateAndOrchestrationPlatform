import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from app.config import MCPServerConfig
from app.mcp.client import MCPClientConnection, MCPToolDefinition
from app.mcp.manager import MCPManager


def test_tool_definition_to_openai():
    tool = MCPToolDefinition(
        server_name="filesystem",
        name="read_file",
        description="Reads a file from workspace",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    openai_format = tool.to_openai_tool()
    assert openai_format["type"] == "function"
    assert openai_format["function"]["name"] == "filesystem__read_file"
    assert "[filesystem]" in openai_format["function"]["description"]
    assert "path" in openai_format["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_mcp_manager_falls_back_to_mock_tools():
    """외부 서버를 띄울 수 없으면 mock 도구로 폴백해 앱이 계속 동작해야 한다.

    실제 서버가 설치되어 있는지에 결과가 좌우되지 않도록, 존재할 수 없는 명령을
    일부러 지정한다.
    """
    server_configs = {
        "filesystem": MCPServerConfig(
            command="__no_such_command_filesystem__",
            args=["./workspace"],
        ),
        "search": MCPServerConfig(
            command="__no_such_command_search__",
            args=[],
        ),
    }
    manager = MCPManager(server_configs)
    await manager.initialize()

    # Verify tool retrieval for servers
    fs_tools = manager.get_tools_for_servers(["filesystem"])
    assert len(fs_tools) > 0
    assert any(t.name == "read_file" for t in fs_tools)

    openai_tools = manager.get_openai_tools_for_servers(["filesystem", "search"])
    assert len(openai_tools) >= 2

    # 폴백 시뮬레이션은 성공으로 보고된다
    output, status = await manager.execute_tool("filesystem__read_file", {"path": "sample.py"})
    assert status == "success"
    assert len(output) > 0
    assert not manager.clients["filesystem"].is_connected

    await manager.shutdown()


FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "stateful_mcp_server.py")


def _field(output: str, key: str) -> str:
    """'count=2 pid=123' 형태의 응답에서 값을 꺼냅니다."""
    return next(part.split("=", 1)[1] for part in output.split() if part.startswith(f"{key}="))


@pytest.mark.asyncio
async def test_session_is_reused_across_tool_calls():
    """도구 호출마다 서버를 재기동하면 서버가 들고 있는 상태가 사라진다.

    코드 실행 샌드박스의 네임스페이스별 커널(변수/데이터프레임)이 이 성질에
    의존하므로, 세션 재사용이 깨지면 여기서 잡힌다.
    """
    manager = MCPManager({"stateful": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    try:
        client = manager.clients["stateful"]
        assert client.is_connected, "stdio 세션이 유지되지 않았습니다"
        assert any(t.name == "bump" for t in client.tools)

        out1, status1 = await manager.execute_tool("stateful__bump", {})
        out2, status2 = await manager.execute_tool("stateful__bump", {})
        assert status1 == "success" and status2 == "success"

        # 상태가 이어진다 (재기동되었다면 둘 다 count=1)
        assert _field(out1, "count") == "1"
        assert _field(out2, "count") == "2"
        # 같은 프로세스가 응답한다
        assert _field(out1, "pid") == _field(out2, "pid")
    finally:
        await manager.shutdown()

    assert manager.clients == {}, "shutdown 후에도 클라이언트가 남아 있습니다"


def _is_process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_shutdown_terminates_server_process():
    """shutdown 이후에는 서버 프로세스가 실제로 종료되어야 한다."""
    manager = MCPManager({"stateful": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    out, _ = await manager.execute_tool("stateful__bump", {})
    pid = int(_field(out, "pid"))

    await manager.shutdown()

    for _ in range(50):  # 프로세스 정리까지 최대 5초 대기
        if not _is_process_alive(pid):
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"MCP 서버 프로세스(pid={pid})가 종료되지 않았습니다")


@pytest.mark.asyncio
async def test_reconnects_after_server_process_dies():
    """서버가 죽으면 다음 호출에서 한 번 재연결한다.

    프로세스가 죽어도 소유 태스크는 곧바로 끝나지 않으므로, 스트림이 닫혔다는
    anyio 예외를 세션 사망 신호로 봐야 한다.
    """
    manager = MCPManager({"stateful": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    try:
        out, _ = await manager.execute_tool("stateful__bump", {})
        old_pid = int(_field(out, "pid"))

        os.kill(old_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        await asyncio.sleep(0.5)

        out2, status2 = await manager.execute_tool("stateful__bump", {})
        assert status2 == "success", f"재연결에 실패했습니다: {out2}"
        assert int(_field(out2, "pid")) != old_pid, "새 서버 프로세스로 붙지 않았습니다"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_connection_status_and_reconnect():
    """UI 가 쓰는 상태 조회와 재연결 경로."""
    good = MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])
    bad = MCPServerConfig(command=sys.executable, args=["-m", "no_such_mcp_server_xyz"])
    manager = MCPManager({"stateful": good, "broken": bad})
    await manager.initialize()
    try:
        status = manager.connection_status()
        assert status["stateful"]["connected"] is True
        assert status["stateful"]["tool_count"] > 0

        assert status["broken"]["connected"] is False
        # 서버가 stderr 로 남긴 실제 원인이 보고되어야 한다
        # (anyio 의 'unhandled errors in a TaskGroup' 이 아니라)
        assert "no_such_mcp_server_xyz" in (status["broken"]["error"] or "")

        # 실패한 서버만 다시 시도하며, 여전히 실패하면 False
        assert await manager.reconnect() is False
        assert manager.connection_status()["stateful"]["connected"] is True

        # 정상 서버를 지정해 재연결하면 새 프로세스로 붙는다
        out_before, _ = await manager.execute_tool("stateful__bump", {})
        assert await manager.reconnect("stateful") is True
        out_after, _ = await manager.execute_tool("stateful__bump", {})
        assert _field(out_before, "pid") != _field(out_after, "pid")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_tool_level_error_is_reported_as_error():
    """서버가 isError 로 보고한 실패는 'error' 상태로 전달되어야 한다.

    MCP 는 프로토콜 오류와 도구 실행 오류를 구분한다. 후자는 성공 응답처럼
    LLM 컨텍스트에 들어가므로, 서버 메시지가 훼손 없이 전달되어야 모델이
    원인을 읽고 스스로 고칠 수 있다.
    """
    manager = MCPManager({"stateful": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    try:
        ok_out, ok_status = await manager.execute_tool("stateful__bump", {})
        assert ok_status == "success"
        assert "count=" in ok_out

        err_out, err_status = await manager.execute_tool(
            "stateful__fail", {"reason": "잘못된 경로"}
        )
        assert err_status == "error", f"isError 가 무시되었습니다: {err_out}"
        # 서버 메시지가 그대로 남아야 모델이 읽고 교정할 수 있다
        assert "잘못된 경로" in err_out
        assert not err_out.startswith("Tool execution failed"), "래핑되어 원문이 가려졌습니다"

        # 실패해도 세션은 살아있어야 한다 (재연결 대상이 아님)
        assert manager.clients["stateful"].is_connected
        after_out, after_status = await manager.execute_tool("stateful__bump", {})
        assert after_status == "success"
        assert _field(after_out, "count") == "2", "실패 호출이 세션을 재기동시켰습니다"
    finally:
        await manager.shutdown()


# ------------------------------------------------------- 대화별 스코프 (_meta)
#
# 어느 대화의 호출인지는 호스트인 앱이 압니다. 모델에게 인자로 물어보면 잊거나
# 잘못 적는 순간 다른 대화의 상태를 건드리므로, 스코프는 매 호출의 `_meta` 로
# 우리가 실어 보냅니다. 아래 테스트들이 그 경로를 고정합니다.


@pytest.mark.asyncio
async def test_tool_calls_carry_the_conversation_scope():
    """execute_tool(scope=...) 가 서버까지 요청 메타데이터로 도착해야 합니다."""
    manager = MCPManager({"stateful": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    try:
        scoped, status = await manager.execute_tool("stateful__echo_scope", {}, scope="sess-A")
        assert status == "success"
        assert scoped.strip() == "scope=sess-A", f"스코프가 서버에 도착하지 않았습니다: {scoped}"

        other, _ = await manager.execute_tool("stateful__echo_scope", {}, scope="sess-B")
        assert other.strip() == "scope=sess-B"

        # 스코프를 안 주면 메타데이터도 안 붙습니다 (서버가 폴백을 정합니다).
        bare, _ = await manager.execute_tool("stateful__echo_scope", {})
        assert bare.strip() == "scope=none"
    finally:
        await manager.shutdown()


def _node_mcp_home() -> Path | None:
    """@modelcontextprotocol/sdk 가 설치된 mcp_node 를 찾습니다 (없으면 None)."""
    candidates = [
        Path(os.environ["MCP_NODE_HOME"]) if os.environ.get("MCP_NODE_HOME") else None,
        Path(__file__).resolve().parent.parent / "mcp_node",
        Path(__file__).resolve().parent.parent / "dist" / "MultiAgentDebateOrchestration_bundle" / "mcp_node",
    ]
    for candidate in candidates:
        if candidate and (candidate / "node_modules" / "@modelcontextprotocol" / "sdk").is_dir():
            return candidate
    return None


def _node_bin() -> str | None:
    import shutil as _shutil
    root = Path(__file__).resolve().parent.parent
    bundled = root / "dist" / "MultiAgentDebateOrchestration_bundle" / "node_runtime" / "node.exe"
    if bundled.is_file():
        return str(bundled)
    return os.environ.get("NODE_BIN") or _shutil.which("node")


needs_node = pytest.mark.skipif(
    _node_mcp_home() is None or _node_bin() is None,
    reason="Node 런타임 또는 mcp_node/node_modules 가 없습니다 (python setup_mcp.py)",
)


def _memory_server(tmp_path) -> dict:
    """포크한 memory 서버를 mcp_node 옆에 놓고 서버 설정을 만듭니다."""
    from app.mcp.manager import VENDORED_MEMORY_FILENAME, sync_vendored_servers

    cfg = MCPServerConfig(
        command=_node_bin(),
        args=[str(_node_mcp_home() / VENDORED_MEMORY_FILENAME)],
        env={"MEMORY_GRAPH_DIR": str(tmp_path / "graphs")},
    )
    servers = {"memory": cfg}
    sync_vendored_servers(servers)
    return servers


async def _entity_names(manager: MCPManager, scope: str | None, arguments: dict | None = None) -> list:
    out, status = await manager.execute_tool("memory__read_graph", arguments or {}, scope=scope)
    assert status == "success", out
    return sorted(e["name"] for e in json.loads(out)["entities"])


async def _remember(manager: MCPManager, scope: str | None, name: str) -> None:
    _, status = await manager.execute_tool(
        "memory__create_entities",
        {"entities": [{"name": name, "entityType": "decision", "observations": ["합의됨"]}]},
        scope=scope,
    )
    assert status == "success"


@needs_node
@pytest.mark.asyncio
async def test_memory_graphs_are_isolated_per_conversation(tmp_path):
    """대화 A 가 기록한 사실이 대화 B 에 보이면 안 됩니다.

    공식 memory 서버는 프로세스 하나에 그래프 하나여서, 서버를 공유하는 다음
    대화가 이전 대화의 기억을 그대로 읽었습니다. 그게 이 포크의 이유입니다.
    """
    manager = MCPManager(_memory_server(tmp_path))
    await manager.initialize()
    try:
        assert manager.clients["memory"].is_connected, manager.clients["memory"].connect_error

        await _remember(manager, "sess-A", "A-만의-결정")
        await _remember(manager, "sess-B", "B-만의-결정")

        assert await _entity_names(manager, "sess-A") == ["A-만의-결정"]
        assert await _entity_names(manager, "sess-B") == ["B-만의-결정"]
    finally:
        await manager.shutdown()


@needs_node
@pytest.mark.asyncio
async def test_request_metadata_beats_a_spoofed_graph_id_argument(tmp_path):
    """모델이 graph_id 로 남의 그래프를 지목해도 호스트 스코프가 이겨야 합니다."""
    manager = MCPManager(_memory_server(tmp_path))
    await manager.initialize()
    try:
        await _remember(manager, "sess-A", "A-만의-결정")
        seen = await _entity_names(manager, "sess-B", {"graph_id": "sess-A"})
        assert seen == [], f"인자로 남의 그래프가 열렸습니다: {seen}"
    finally:
        await manager.shutdown()


@needs_node
@pytest.mark.asyncio
async def test_unscoped_calls_do_not_fall_into_a_shared_graph(tmp_path):
    """스코프가 빠진 호출은 공용 그래프가 아니라 프로세스 한정 그래프로 갑니다.

    공용으로 떨어뜨리면 주입이 조용히 실패했을 때 예전처럼 대화가 섞이고,
    아무도 눈치채지 못합니다.
    """
    manager = MCPManager(_memory_server(tmp_path))
    await manager.initialize()
    try:
        await _remember(manager, "sess-A", "A-만의-결정")
        await _remember(manager, None, "스코프-없는-사실")

        assert await _entity_names(manager, "sess-A") == ["A-만의-결정"]
        assert await _entity_names(manager, None) == ["스코프-없는-사실"]

        graphs = sorted(p.name for p in (tmp_path / "graphs").iterdir())
        assert "sess-A.jsonl" in graphs
        assert any(name.startswith("unscoped-") for name in graphs), graphs
    finally:
        await manager.shutdown()


def test_vendored_memory_server_is_installed_where_the_config_points(tmp_path):
    """설정이 가리키는 자리에 포크 사본이 놓이고, 경로는 절대 경로가 됩니다."""
    from app.mcp.manager import VENDORED_MEMORY_FILENAME, VENDORED_MEMORY_SERVER, sync_vendored_servers

    target = tmp_path / "mcp_node" / VENDORED_MEMORY_FILENAME
    servers = {"memory": MCPServerConfig(command="node", args=[str(target)])}
    sync_vendored_servers(servers)

    assert target.is_file()
    assert target.read_bytes() == VENDORED_MEMORY_SERVER.read_bytes()

    # 상대 경로로 적혀 있어도 프로젝트 루트 기준 절대 경로로 바뀝니다. 상대
    # 경로를 그대로 넘기면 자식 프로세스가 자기 cwd 로 풀어 버립니다.
    relative = {"memory": MCPServerConfig(command="node", args=[f"./mcp_node/{VENDORED_MEMORY_FILENAME}"])}
    sync_vendored_servers(relative)
    assert Path(relative["memory"].args[0]).is_absolute()


def test_scope_policy_splits_the_kernel_but_not_the_graph():
    """서버마다 상태의 경계가 다릅니다. 그 경계를 호스트가 정합니다."""
    from app.mcp.manager import compose_scope

    # 지식 그래프는 대화 단위입니다. 합의된 사실은 참가자 전원이 함께 봐야 합니다.
    assert compose_scope("memory", "sess-A", "coder") == "sess-A"
    assert compose_scope("memory", "sess-A", "critic") == "sess-A"

    # 커널은 발언자 단위입니다. 커널 변수는 다음 발언자의 컨텍스트에 남지 않으므로,
    # 물려주면 아무도 검증할 수 없는 상태가 됩니다. 인계는 작업 공간 파일로 합니다.
    assert compose_scope("sandbox", "sess-A", "coder") == "sess-A-coder"
    assert compose_scope("sandbox", "sess-A", "critic") == "sess-A-critic"
    assert compose_scope("sandbox", "sess-A", None) == "sess-A"

    # 폴더를 공유하는 서버는 경로가 곧 경계라 스코프를 쓰지 않습니다.
    assert compose_scope("filesystem", "sess-A", "coder") == "sess-A"
    assert compose_scope("sandbox", None, "coder") is None


@pytest.mark.asyncio
async def test_two_agents_in_one_debate_get_different_kernels():
    """같은 대화라도 발언자가 다르면 샌드박스 스코프가 달라야 합니다.

    픽스처 서버를 'sandbox' 라는 이름으로 띄워, 정책이 실제 요청 메타데이터까지
    반영되는지 봅니다.
    """
    manager = MCPManager({"sandbox": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    try:
        coder, _ = await manager.execute_tool(
            "sandbox__echo_scope", {}, scope="sess-A", actor="coder"
        )
        critic, _ = await manager.execute_tool(
            "sandbox__echo_scope", {}, scope="sess-A", actor="critic"
        )
        assert coder.strip() == "scope=sess-A-coder"
        assert critic.strip() == "scope=sess-A-critic"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_agents_in_one_debate_share_one_memory_graph():
    """반대로 지식 그래프는 발언자가 달라도 같아야 합니다."""
    manager = MCPManager({"memory": MCPServerConfig(command=sys.executable, args=[FIXTURE_SERVER])})
    await manager.initialize()
    try:
        for actor in ("orchestrator", "architect", "critic"):
            out, _ = await manager.execute_tool(
                "memory__echo_scope", {}, scope="sess-A", actor=actor
            )
            assert out.strip() == "scope=sess-A", f"{actor} 가 다른 그래프를 봅니다: {out}"
    finally:
        await manager.shutdown()


def test_stale_conf_pointing_at_the_official_memory_server_is_reported(caplog):
    """소스만 갱신한 설치본은 conf.json 이 그대로라 공식 서버를 계속 띄웁니다.

    그러면 격리 없이 동작하는데, 조용하면 아무도 모릅니다.
    """
    import logging
    from app.mcp.manager import OFFICIAL_MEMORY_PACKAGE, sync_vendored_servers

    stale = {"memory": MCPServerConfig(
        command="node",
        args=[f"./mcp_node/node_modules/{OFFICIAL_MEMORY_PACKAGE}/dist/index.js"],
    )}
    with caplog.at_level(logging.WARNING, logger="app.mcp.manager"):
        sync_vendored_servers(stale)

    assert any("still points at the official memory server" in r.message for r in caplog.records)
