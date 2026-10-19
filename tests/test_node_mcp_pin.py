"""Node MCP 서버의 버전 범위 — 2.0 계열을 받지 않는다.

2026-10-19 에 나온 2.0.0 은 SDK 를 v2 로 바꿔 `@modelcontextprotocol/sdk` 를 깔지 않습니다.
memory 포크(mcp_servers/memory_scoped)는 sdk 1.x 를 node_modules 에서 찾으므로, 범위 없이
설치하면 새 설치본에서 memory 서버만 조용히 "연결 안 됨" 이 됩니다.

여기서 지키려는 것.

1. 범위는 날짜형 버전의 캐럿(`^2026.8.31`)이다. `<2.0.0` 은 npm 이 1.0.0 으로 푼다.
2. 개발 PC 설치(`setup_mcp.py`)와 전체 번들(`package_offline.py`)이 같은 목록을 쓴다.
3. 범위는 명령줄이 아니라 package.json 으로 npm 에 건넨다 — cmd.exe 가 `^` 를 지운다.
4. 범위를 고정하기 전에 깔린 설치본(2.0.0)은 "이미 설치됨" 으로 넘어가지 않는다.
"""

import json
import re

import package_offline
import setup_mcp


def test_every_node_server_is_held_on_the_calendar_line():
    """`^YYYY.M.D` (YYYY >= 2025) 만 2.0.0 과 1.0.0 을 함께 뺍니다."""
    assert set(setup_mcp.NODE_MCP_PACKAGES) == {
        "@modelcontextprotocol/server-filesystem",
        "@modelcontextprotocol/server-memory",
        "@modelcontextprotocol/server-sequential-thinking",
    }
    for name, spec in setup_mcp.NODE_MCP_PACKAGES.items():
        match = re.fullmatch(r"\^(\d{4})\.\d+\.\d+", spec)
        assert match and int(match.group(1)) >= 2025, f"{name}: {spec}"


def test_offline_bundle_uses_the_same_list():
    assert package_offline.node_packages_label is setup_mcp.node_packages_label
    assert package_offline.write_node_manifest is setup_mcp.write_node_manifest
    assert package_offline.node_servers_installed is setup_mcp.node_servers_installed


def test_manifest_carries_the_ranges_and_keeps_other_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "mcp-node",
        "dependencies": {"@modelcontextprotocol/server-memory": "^2.0.0", "left-pad": "^1.3.0"},
    }), encoding="utf-8")

    setup_mcp.write_node_manifest(tmp_path)

    manifest = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "mcp-node"
    assert manifest["dependencies"]["left-pad"] == "^1.3.0"
    for name, spec in setup_mcp.NODE_MCP_PACKAGES.items():
        assert manifest["dependencies"][name] == spec


def test_an_install_made_before_the_pin_is_not_kept(tmp_path):
    (tmp_path / "node_modules" / "@modelcontextprotocol").mkdir(parents=True)
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {name: "^2.0.0" for name in setup_mcp.NODE_MCP_PACKAGES},
    }), encoding="utf-8")
    assert setup_mcp.node_servers_installed(tmp_path) is False

    setup_mcp.write_node_manifest(tmp_path)
    assert setup_mcp.node_servers_installed(tmp_path) is True


def test_a_manifest_without_node_modules_is_not_an_install(tmp_path):
    setup_mcp.write_node_manifest(tmp_path)
    assert setup_mcp.node_servers_installed(tmp_path) is False


def test_ranges_never_ride_on_the_npm_command_line(tmp_path, monkeypatch):
    """npm.cmd 는 cmd.exe 를 거쳐 `^` 가 지워집니다. 범위는 package.json 으로만 건넵니다."""
    commands = []
    monkeypatch.setattr(setup_mcp, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(setup_mcp.shutil, "which", lambda name: "npm" if name == "npm" else None)
    monkeypatch.setattr(setup_mcp.subprocess, "run", lambda cmd, **kwargs: commands.append(cmd))

    assert setup_mcp.setup_node_servers() is True

    (command,) = commands
    assert command[:2] == ["npm", "install"]
    assert not any(arg.startswith("@modelcontextprotocol/") for arg in command)
    manifest = json.loads((tmp_path / "mcp_node" / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"] == setup_mcp.NODE_MCP_PACKAGES
