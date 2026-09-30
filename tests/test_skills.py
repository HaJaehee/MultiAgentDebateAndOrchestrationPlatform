"""스킬 테스트 스위트 — 폴더 기반 설치 및 에이전트 동적 로드 지침 검증 (`app/agents/skills.py`).

주요 검증 항목:

* SKILL.md 머리말을 신규 의존성 없이 정상 파싱합니다 (따옴표, `|`·`>` 블록 스칼라, 연속 줄, 불필요한 키 무시).
* 유효하지 않은 스킬은 원인과 함께 목록에 유지되며, 어떤 에이전트에게도 할당되지 않습니다.
* 에이전트 가시 스킬 = `allowed_skills` ∩ 활성화된 스킬 ∩ 오류 없는 스킬의 교집합입니다. 활성화/비활성화 및 내용 수정은
  **다음 발언부터 즉시** 반영됩니다 (스냅샷 고정 제외).
* 부속 파일은 스킬 디렉터리 **내부 경로에서만** 조회할 수 있습니다.
* 도구 루프가 스킬 도구를 보안 판정 없이 호스트 레벨에서 직접 실행하며, 실행 기록은 일반 도구와 동일하게 보존합니다.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents import skills as skills_module
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.skills import (
    LOAD_SKILL_TOOL,
    READ_SKILL_FILE_TOOL,
    SkillError,
    is_skill_tool,
    parse_skill_md,
    read_skill_file,
    run_skill_tool,
    scan_skills,
    skill_guidance,
    skill_tools,
    skill_tools_for,
    visible_skills,
)
from app.config import (
    AgentConfig,
    RootConfig,
    add_agent_to_conf_file,
    add_mcp_server_to_conf_file,
    read_conf_file,
    set_agent_allowed_skills_in_conf_file,
    set_skill_enabled_in_conf_file,
    write_conf_file,
)


def _write_skill(root: Path, name: str, description: str = "표를 요약할 때 씁니다.",
                 body: str = "# 지침\n\n1. 먼저 열을 봅니다.", files: dict | None = None) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    for rel, content in (files or {}).items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return folder


def _config(root: Path, disabled=()) -> RootConfig:
    return RootConfig.model_validate({
        "agents": {"orchestrator": {"name": "O", "role": "R"}},
        "skills": {"dir": str(root), "disabled": list(disabled)},
    })


class _SkillFolder:
    """스킬 폴더 하나와, 그것을 가리키는 현재 설정입니다. `disable()` 메서드로 활성화/비활성화를 전환합니다."""

    def __init__(self, root: Path):
        self.root = root
        self.config = _config(root)

    def disable(self, *names: str) -> None:
        self.config = _config(self.root, disabled=names)


@pytest.fixture()
def skill_root(tmp_path: Path, monkeypatch) -> _SkillFolder:
    folder = _SkillFolder(tmp_path / "skills")
    folder.root.mkdir()
    monkeypatch.setattr(skills_module, "get_config", lambda: folder.config)
    return folder


def _agent(*skills: str, key: str = "architect") -> Agent:
    return Agent(key=key, name="A", role="R", model="openai/gpt-4o", api_key="k",
                 allowed_skills=list(skills))


# ================================================================ 머리말


def test_front_matter_simple_and_body():
    meta, body = parse_skill_md("---\nname: pdf\ndescription: PDF 를 다룹니다.\n---\n\n# 본문\n내용\n")
    assert meta == {"name": "pdf", "description": "PDF 를 다룹니다."}
    assert body == "# 본문\n내용"


def test_front_matter_quoted_values_keep_colons_and_escapes():
    text = (
        '---\n'
        'name: "report"\n'
        'description: "형식: 결론 먼저, 근거는 뒤. \\"요약\\" 필수"\n'
        "license: 'It''s MIT'\n"
        '---\n본문\n'
    )
    meta, _ = parse_skill_md(text)
    assert meta["description"] == '형식: 결론 먼저, 근거는 뒤. "요약" 필수'
    assert meta["license"] == "It's MIT"


def test_front_matter_block_scalars_and_continuations():
    text = (
        "---\n"
        "name: x\n"
        "description: >\n"
        "  첫 줄과\n"
        "  둘째 줄은 이어집니다.\n"
        "notes: |\n"
        "  줄바꿈을\n"
        "  지킵니다\n"
        "plain: 여러 줄로\n"
        "  이어진 값  # 주석은 값이 아닙니다\n"
        "metadata:\n"
        "  owner: team\n"
        "  tags: [a, b]\n"
        "# 머리말 안의 주석\n"
        "---\n본문\n"
    )
    meta, _ = parse_skill_md(text)
    assert meta["description"] == "첫 줄과 둘째 줄은 이어집니다."
    assert meta["notes"] == "줄바꿈을\n지킵니다"
    assert meta["plain"] == "여러 줄로 이어진 값  # 주석은 값이 아닙니다"
    assert meta["metadata"] == "", "목록·매핑 값은 쓰지 않으므로 버립니다"


def test_front_matter_comment_after_plain_value_is_dropped_but_csharp_is_not():
    meta, _ = parse_skill_md("---\ndescription: C# 코드를 검토합니다 # 설명\n---\n본문")
    assert meta["description"] == "C# 코드를 검토합니다"


@pytest.mark.parametrize("text, fragment", [
    ("# 머리말 없음\n본문", "머리말"),
    ("---\nname: x\ndescription: y\n본문", "닫는"),
    ("---\ndescription: \"안 닫힘\n---\n본문", "따옴표"),
    ("---\n이건 키가 아님\n---\n본문", "읽지 못했습니다"),
])
def test_broken_front_matter_says_why(text, fragment):
    with pytest.raises(SkillError, match=fragment):
        parse_skill_md(text)


def test_bom_and_crlf_are_accepted():
    meta, body = parse_skill_md("\ufeff---\r\nname: a\r\ndescription: b\r\n---\r\n본문\r\n")
    assert meta["description"] == "b" and body == "본문"


# ================================================================ 폴더 읽기


def test_scan_lists_skills_with_their_files_and_problems(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "csv-profile", files={
        "reference.md": "참고", "scripts/profile.py": "print(1)",
        "scripts/__pycache__/profile.cpython-312.pyc": b"\x00", ".hidden": "x",
    })
    _write_skill(root, "no-desc", description="")
    (root / "empty-folder").mkdir()
    _write_skill(root, "bad name")
    (root / ".git").mkdir()
    (root / "README.md").write_text("폴더가 아닌 파일은 스킬이 아닙니다", encoding="utf-8")

    found = {s.name: s for s in scan_skills(root, disabled=[])}
    assert set(found) == {"csv-profile", "no-desc", "empty-folder", "bad name"}

    good = found["csv-profile"]
    assert good.usable and good.description == "표를 요약할 때 씁니다."
    assert good.files == ("reference.md", "scripts/profile.py"), "SKILL.md·캐시·숨김 파일은 제외됩니다"
    assert "description" in found["no-desc"].problem
    assert "SKILL.md" in found["empty-folder"].problem
    assert "폴더 이름" in found["bad name"].problem
    assert not any(s.usable for n, s in found.items() if n != "csv-profile")


def test_disabled_skills_stay_listed_but_are_not_usable(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "a")
    _write_skill(root, "b")
    found = {s.name: s for s in scan_skills(root, disabled=["b"])}
    assert found["a"].usable and not found["b"].enabled and not found["b"].usable


def test_edits_to_skill_md_are_seen_on_the_next_scan(tmp_path: Path):
    root = tmp_path / "skills"
    folder = _write_skill(root, "a", description="처음 설명")
    assert scan_skills(root, [])[0].description == "처음 설명"

    md = folder / "SKILL.md"
    md.write_text("---\nname: a\ndescription: 고친 설명입니다\n---\n새 본문\n", encoding="utf-8")
    stat = md.stat()
    os.utime(md, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    again = scan_skills(root, [])[0]
    assert again.description == "고친 설명입니다" and again.body == "새 본문"


def test_oversized_skill_md_is_a_problem(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    _write_skill(root, "big", body="가" * 200)
    monkeypatch.setattr(skills_module, "MAX_SKILL_MD_BYTES", 100)
    assert "너무 큽니다" in scan_skills(root, [])[0].problem


def test_missing_skills_folder_is_simply_empty(tmp_path: Path):
    assert scan_skills(tmp_path / "없음", []) == []


# ================================================================ 에이전트에게 보이는 것


def test_an_agent_sees_only_assigned_enabled_and_healthy_skills(skill_root: _SkillFolder):
    _write_skill(skill_root.root, "a")
    _write_skill(skill_root.root, "b")
    _write_skill(skill_root.root, "broken", description="")
    _write_skill(skill_root.root, "other")
    skill_root.disable("b")

    names = [s.name for s in visible_skills(_agent("a", "b", "broken", "ghost"))]
    assert names == ["a"]
    assert visible_skills(_agent()) == []


def test_agents_without_skills_never_scan_the_folder(monkeypatch):
    monkeypatch.setattr(skills_module, "scan_skills", lambda *a, **k: pytest.fail("훑으면 안 됩니다"))
    assert skill_tools_for(_agent()) == []
    assert visible_skills(_agent()) == []


def test_tool_definitions_carry_the_catalog(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "a", description="A 를 할 때")
    _write_skill(root, "b", description="B 를 할 때", files={"ref.md": "x"})
    tools = skill_tools(scan_skills(root, []))

    load, read = tools
    assert load["function"]["name"] == LOAD_SKILL_TOOL
    assert "- a: A 를 할 때" in load["function"]["description"]
    assert "- b: B 를 할 때" in load["function"]["description"]
    assert load["function"]["parameters"]["properties"]["skill"]["enum"] == ["a", "b"]
    assert read["function"]["name"] == READ_SKILL_FILE_TOOL
    assert read["function"]["parameters"]["properties"]["skill"]["enum"] == ["b"], \
        "부속 파일이 존재하는 스킬만 읽기 도구에 등록합니다"

    only_md = skill_tools(scan_skills(root, [])[:1])
    assert [t["function"]["name"] for t in only_md] == [LOAD_SKILL_TOOL]
    assert skill_tools([]) == []


def test_turning_a_skill_off_removes_it_from_the_next_request(skill_root: _SkillFolder):
    _write_skill(skill_root.root, "a")
    agent = _agent("a")
    assert [t["function"]["name"] for t in skill_tools_for(agent)] == [LOAD_SKILL_TOOL]

    skill_root.disable("a")
    assert skill_tools_for(agent) == [], "비활성화 시 다음 발언의 도구 목록에서 즉시 제외됩니다"


def test_an_unreadable_config_does_not_break_the_speech(monkeypatch):
    def boom():
        raise RuntimeError("conf.json 없음")
    monkeypatch.setattr(skills_module, "get_config", boom)
    assert skill_tools_for(_agent("a")) == []


def test_skill_tool_names():
    assert is_skill_tool(LOAD_SKILL_TOOL) and is_skill_tool(READ_SKILL_FILE_TOOL)
    assert is_skill_tool("load_skill"), "모델이 접두사를 생략하고 호출해도 정상 처리합니다"
    assert not is_skill_tool("filesystem__read_file")
    assert not is_skill_tool("other__load_skill")
    assert not is_skill_tool("skills__delete_everything")


def test_guidance_only_for_agents_holding_the_tool(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "a")
    _write_skill(root, "b", files={"ref.md": "x"})
    assert skill_guidance(None) is None
    assert skill_guidance([{"function": {"name": "filesystem__read_file"}}]) is None

    only_load = skill_guidance(skill_tools(scan_skills(root, [])[:1]))
    assert LOAD_SKILL_TOOL in only_load and READ_SKILL_FILE_TOOL not in only_load
    both = skill_guidance(skill_tools(scan_skills(root, [])))
    assert READ_SKILL_FILE_TOOL in both


def test_the_system_prompt_puts_skill_guidance_before_session_instructions(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "a")
    tools = skill_tools(scan_skills(root, []))
    prompt = LLMCaller().build_system_prompt(_agent("a"), "이 대화의 지침", tools)
    assert prompt.index("[스킬]") < prompt.index("[Session Custom Instructions]")
    assert "[스킬]" not in LLMCaller().build_system_prompt(_agent(), "", [])


# ================================================================ 실행


@pytest.mark.asyncio
async def test_load_returns_the_body_and_the_files_to_read(skill_root: _SkillFolder):
    _write_skill(skill_root.root, "a", body="# 지침\n- 결론부터", files={"ref.md": "참고", "ex/sample.md": "예"})
    output, status = await run_skill_tool(_agent("a"), LOAD_SKILL_TOOL, {"skill": "a"})
    assert status == "success"
    assert output.startswith("# 스킬: a")
    assert "- 결론부터" in output
    assert "- ref.md" in output and "- ex/sample.md" in output
    assert "---\nname:" not in output, "머리말은 싣지 않습니다"


@pytest.mark.asyncio
async def test_a_skill_turned_off_mid_speech_is_refused_at_call_time(skill_root: _SkillFolder):
    _write_skill(skill_root.root, "a")
    _write_skill(skill_root.root, "b")
    agent = _agent("a", "b")
    skill_root.disable("a")

    output, status = await run_skill_tool(agent, LOAD_SKILL_TOOL, {"skill": "a"})
    assert status == "error"
    assert "쓸 수 없습니다" in output and "쓸 수 있는 스킬: b" in output


@pytest.mark.asyncio
async def test_unassigned_and_unnamed_skills_are_refused(skill_root: _SkillFolder):
    _write_skill(skill_root.root, "a")
    output, status = await run_skill_tool(_agent("b"), LOAD_SKILL_TOOL, {"skill": "a"})
    assert status == "error" and "지금 쓸 수 있는 스킬이 없습니다" in output
    output, status = await run_skill_tool(_agent("a"), LOAD_SKILL_TOOL, {})
    assert status == "error" and "skill" in output


@pytest.mark.asyncio
async def test_read_skill_file_reads_inside_the_skill_only(skill_root: _SkillFolder, tmp_path: Path):
    _write_skill(skill_root.root, "a", files={"ref.md": "참고 내용", "img.png": b"\x89PNG\x00\x00", ".env": "S=1"})
    (tmp_path / "secret.txt").write_text("비밀", encoding="utf-8")
    agent = _agent("a")

    output, status = await run_skill_tool(agent, READ_SKILL_FILE_TOOL, {"skill": "a", "path": "ref.md"})
    assert (output, status) == ("참고 내용", "success")

    for bad in ("../../secret.txt", "..\\..\\secret.txt", "/etc/passwd", "C:\\Windows\\win.ini",
                ".env", "__pycache__/x.pyc", ""):
        output, status = await run_skill_tool(agent, READ_SKILL_FILE_TOOL, {"skill": "a", "path": bad})
        assert status == "error", bad
        assert "비밀" not in output

    output, status = await run_skill_tool(agent, READ_SKILL_FILE_TOOL, {"skill": "a", "path": "img.png"})
    assert status == "error" and "글 파일이 아니어서" in output

    output, status = await run_skill_tool(agent, READ_SKILL_FILE_TOOL, {"skill": "a", "path": "없음.md"})
    assert status == "error" and "ref.md" in output, "없는 파일이면 있는 파일을 알려 줍니다"


def test_large_reference_files_are_cut_with_a_note(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    _write_skill(root, "a", files={"big.md": "가나다라" * 100})
    monkeypatch.setattr(skills_module, "MAX_READ_BYTES", 50)
    skill = scan_skills(root, [])[0]
    output, status = read_skill_file(skill, "big.md")
    assert status == "success" and "앞 50바이트만" in output


# ================================================================ 도구 루프


def _msg(content="", tool_calls=None):
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=None,
        model_dump=lambda: {"role": "assistant", "content": content},
    )


class _NoTools:
    workspace = None

    def get_openai_tools_for_servers(self, servers):
        return []

    async def execute_tool(self, *args, **kwargs):
        raise AssertionError("스킬 도구는 MCP 로 가지 않습니다")


class _GateThatMustNotJudgeSkills:
    def filter_tools(self, agent_key, tools, mcp):
        return tools

    async def check(self, *args, **kwargs):
        raise AssertionError("스킬 도구는 보안 판정을 거치지 않습니다")


@pytest.mark.asyncio
async def test_the_tool_loop_runs_a_skill_and_logs_it(skill_root: _SkillFolder):
    _write_skill(skill_root.root, "mermaid-diagrams", body="subgraph 제목은 대괄호로 씁니다.")
    agent = _agent("mermaid-diagrams")
    sent = []
    call = SimpleNamespace(id="c1", function=SimpleNamespace(
        name=LOAD_SKILL_TOOL, arguments=json.dumps({"skill": "mermaid-diagrams"})))
    turns = [(_msg("", [call]), "tool_calls"), (_msg("스킬대로 그렸습니다."), "stop")]

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        message, finish = turns[len(sent) - 1]
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])

    logs = []
    with patch("litellm.acompletion", side_effect=fake_acompletion):
        text, tool_logs = await LLMCaller(mcp_manager=_NoTools()).call_agent(
            agent, [{"role": "user", "content": "구조도를 그려 주세요"}],
            on_tool_call=logs.append, tool_gate=_GateThatMustNotJudgeSkills(),
        )

    assert text == "스킬대로 그렸습니다."
    first = sent[0]
    assert [t["function"]["name"] for t in first["tools"]] == [LOAD_SKILL_TOOL]
    assert "[스킬]" in first["messages"][0]["content"]
    tool_message = next(m for m in sent[1]["messages"] if m.get("role") == "tool")
    assert "subgraph 제목은 대괄호로 씁니다." in tool_message["content"]
    assert tool_logs[0]["tool_name"] == LOAD_SKILL_TOOL
    assert tool_logs[0]["status"] == "success" and tool_logs[0]["security"] == {}
    assert logs == tool_logs, "UI 알림 및 실행 로그 콜백도 일반 도구와 동일하게 수신합니다"


# ================================================================ 설정


def test_an_mcp_server_cannot_take_the_skill_prefix(tmp_path: Path):
    with pytest.raises(ValueError, match="skills"):
        RootConfig.model_validate({
            "agents": {"orchestrator": {"name": "O", "role": "R"}},
            "mcp_servers": {"skills": {"command": "node"}},
        })
    path = tmp_path / "conf.json"
    write_conf_file(path, {"agents": {"orchestrator": {"name": "O", "role": "R"}}})
    with pytest.raises(ValueError, match="스킬 도구"):
        add_mcp_server_to_conf_file("skills", command="node", config_path=path)


def test_toggling_a_skill_edits_only_the_disabled_list(tmp_path: Path):
    path = tmp_path / "conf.json"
    write_conf_file(path, {
        "// 설명": "보존됩니다",
        "agents": {"orchestrator": {"name": "O", "role": "R"}},
    })
    set_skill_enabled_in_conf_file("a", False, path)
    set_skill_enabled_in_conf_file("b", False, path)
    set_skill_enabled_in_conf_file("a", False, path)
    data = read_conf_file(path)
    assert data["skills"] == {"disabled": ["b", "a"]}
    assert data["// 설명"] == "보존됩니다"

    set_skill_enabled_in_conf_file("b", True, path)
    assert read_conf_file(path)["skills"]["disabled"] == ["a"]
    with pytest.raises(ValueError):
        set_skill_enabled_in_conf_file("../x", False, path)


def test_assigning_skills_to_an_agent(tmp_path: Path):
    path = tmp_path / "conf.json"
    write_conf_file(path, {"agents": {"orchestrator": {"name": "O", "role": "R"}}})
    set_agent_allowed_skills_in_conf_file("orchestrator", ["a", "b", "a"], path)
    assert read_conf_file(path)["agents"]["orchestrator"]["allowed_skills"] == ["a", "b"]
    set_agent_allowed_skills_in_conf_file("orchestrator", [], path)
    assert "allowed_skills" not in read_conf_file(path)["agents"]["orchestrator"]
    with pytest.raises(ValueError):
        set_agent_allowed_skills_in_conf_file("orchestrator", ["나쁜 이름"], path)


def test_a_new_agent_can_start_with_skills(tmp_path: Path):
    path = tmp_path / "conf.json"
    write_conf_file(path, {"agents": {"orchestrator": {"name": "O", "role": "R"}}})
    add_agent_to_conf_file("writer", "Writer", "Docs", allowed_skills=["report"], config_path=path)
    add_agent_to_conf_file("plain", "Plain", "Docs", config_path=path)
    agents = read_conf_file(path)["agents"]
    assert agents["writer"]["allowed_skills"] == ["report"]
    assert "allowed_skills" not in agents["plain"], "선택한 스킬이 없으면 항목을 기록하지 않습니다"


def test_allowed_skills_ride_along_in_the_session_snapshot():
    from app.agents.personas import agent_from_snapshot, config_snapshot_of

    agent = Agent.from_config("architect", AgentConfig(name="A", role="R", allowed_skills=["a"]))
    restored = agent_from_snapshot("architect", config_snapshot_of(agent))
    assert restored.allowed_skills == ["a"]


def test_the_orchestrator_sees_live_skill_names_and_tool_less_calls_drop_them(skill_root: _SkillFolder):
    from app.orchestration.engine import OrchestratorEngine, format_roster

    _write_skill(skill_root.root, "a")
    _write_skill(skill_root.root, "b")
    skill_root.disable("b")
    agent = _agent("a", "b")
    assert format_roster([agent]).endswith("· 도구: 없음 · 스킬: a")
    assert "스킬" not in format_roster([_agent()])

    bare = OrchestratorEngine._tool_less(agent)
    assert bare.allowed_skills == [] and agent.allowed_skills == ["a", "b"]


# ================================================================ 2단계: 스크립트


RUN_TOOL = {"type": "function", "function": {"name": "sandbox__run_python_file", "parameters": {}}}


@pytest.mark.asyncio
async def test_loading_a_script_skill_stages_it_into_the_workspace(skill_root: _SkillFolder, tmp_path: Path):
    _write_skill(skill_root.root, "csv", files={"scripts/run.py": "print('hi')", "ref.md": "참고"})
    workspace = tmp_path / "ws"
    workspace.mkdir()

    output, status = await run_skill_tool(
        _agent("csv"), LOAD_SKILL_TOOL, {"skill": "csv"}, workspace=workspace, tools=[RUN_TOOL],
    )
    assert status == "success"
    staged = workspace / ".mado" / "skills" / "csv"
    assert (staged / "scripts" / "run.py").read_text(encoding="utf-8") == "print('hi')"
    assert (staged / "SKILL.md").is_file() and (staged / "ref.md").is_file()
    assert "`.mado/skills/csv/scripts/run.py`" in output
    assert "sandbox__run_python_file" in output and "인자 없이" in output


def test_staging_recopies_changed_files_and_keeps_outputs(tmp_path: Path):
    root = tmp_path / "skills"
    folder = _write_skill(root, "csv", files={"scripts/run.py": "v1"})
    workspace = tmp_path / "ws"
    skill = scan_skills(root, [])[0]

    relative = skills_module.stage_skill(skill, workspace)
    staged = workspace / Path(*relative.parts)
    copy = staged / "scripts" / "run.py"
    assert relative.as_posix() == ".mado/skills/csv" and copy.read_text() == "v1"

    # 원본을 수정하면 다음 호출 시 새로운 내용이 배치됩니다.
    source = folder / "scripts" / "run.py"
    source.write_text("v2 — 원본이 바뀜", encoding="utf-8")
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 5_000_000_000))
    skills_module.stage_skill(skill, workspace)
    assert copy.read_text(encoding="utf-8") == "v2 — 원본이 바뀜"

    # 에이전트가 복사본을 수정하더라도 원본으로 복원되며, 스크립트 실행 결과물은 삭제하지 않습니다.
    copy.write_text("조작됨", encoding="utf-8")
    (staged / "scripts" / "result.txt").write_text("결과", encoding="utf-8")
    skills_module.stage_skill(skill, workspace)
    assert copy.read_text(encoding="utf-8") == "v2 — 원본이 바뀜"
    assert (staged / "scripts" / "result.txt").exists()


@pytest.mark.asyncio
async def test_no_copy_without_a_run_tool_or_a_workspace(skill_root: _SkillFolder, tmp_path: Path):
    _write_skill(skill_root.root, "csv", files={"run.py": "print(1)"})
    workspace = tmp_path / "ws"
    workspace.mkdir()

    output, status = await run_skill_tool(
        _agent("csv"), LOAD_SKILL_TOOL, {"skill": "csv"}, workspace=workspace,
        tools=[{"type": "function", "function": {"name": "filesystem__read_file"}}],
    )
    assert status == "success" and "run_python_file" in output and "복사하지 않았습니다" in output
    assert not (workspace / ".mado").exists(), "사용하지 않을 파일로 작업 공간을 어지럽히지 않습니다"

    output, status = await run_skill_tool(
        _agent("csv"), LOAD_SKILL_TOOL, {"skill": "csv"}, workspace=None, tools=[RUN_TOOL],
    )
    assert status == "success" and "작업 공간을 알 수 없어" in output


@pytest.mark.asyncio
async def test_a_skill_too_big_to_stage_still_gives_its_instructions(
    skill_root: _SkillFolder, tmp_path: Path, monkeypatch,
):
    _write_skill(skill_root.root, "csv", body="지침 본문", files={"run.py": "x" * 200})
    monkeypatch.setattr(skills_module, "MAX_STAGE_BYTES", 100)
    output, status = await run_skill_tool(
        _agent("csv"), LOAD_SKILL_TOOL, {"skill": "csv"}, workspace=tmp_path, tools=[RUN_TOOL],
    )
    assert status == "success" and "지침 본문" in output and "복사하지 못했습니다" in output


def test_only_python_files_count_as_scripts(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "a", files={"run.py": "", "tool.sh": "", "Lib/Helper.PY": "", "ref.md": ""})
    assert scan_skills(root, [])[0].scripts == ("run.py", "Lib/Helper.PY"), "최상위 파일이 먼저 정렬됩니다"


@pytest.mark.asyncio
async def test_the_tool_loop_stages_into_the_speech_workspace(skill_root: _SkillFolder, tmp_path: Path):
    """발언이 사용하는 런타임의 작업 공간에 복사하고, 해당 런타임의 실행 도구 이름을 안내합니다."""
    _write_skill(skill_root.root, "csv", files={"scripts/run.py": "print(1)"})
    workspace = tmp_path / "ws"
    workspace.mkdir()

    class _Sandbox:
        def __init__(self):
            self.workspace = workspace

        def get_openai_tools_for_servers(self, servers):
            return [RUN_TOOL]

        async def execute_tool(self, *args, **kwargs):
            raise AssertionError("이 테스트는 스크립트를 실행하지 않습니다")

    call = SimpleNamespace(id="c1", function=SimpleNamespace(
        name=LOAD_SKILL_TOOL, arguments=json.dumps({"skill": "csv"})))
    turns = [(_msg("", [call]), "tool_calls"), (_msg("복사된 스크립트를 확인했습니다."), "stop")]
    sent = []

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append(kwargs)
        message, finish = turns[len(sent) - 1]
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        _text, tool_logs = await LLMCaller(mcp_manager=_Sandbox()).call_agent(
            _agent("csv"), [{"role": "user", "content": "요약해 주세요"}],
        )
    assert (workspace / ".mado" / "skills" / "csv" / "scripts" / "run.py").is_file()
    assert "sandbox__run_python_file" in tool_logs[0]["output"]


# ================================================================ 기본 스킬


PROJECT_SKILLS = Path(__file__).resolve().parent.parent / "skills"


def test_the_bundled_skills_are_all_usable():
    found = {s.name: s for s in scan_skills(PROJECT_SKILLS, [])}
    assert {"mermaid-diagrams", "csv-profile"} <= set(found)
    assert all(s.usable for s in found.values()), {n: s.problem for n, s in found.items() if s.problem}
    assert found["csv-profile"].scripts == ("scripts/profile_csv.py",)


def test_the_bundled_script_passes_the_tool_gate_once_staged():
    """복사된 번들 스크립트가 고정 보호에 걸리지 않고, 기본 모드에서는 확인 없이 실행됩니다.

    스크립트 소스 코드에 포함된 문자열도 정적 검사 대상입니다. 건너뛸 디렉터리명으로 `.memory-graphs`를
    명시하기만 해도 '대화별 지식 그래프 조회 시도'로 판정되어 실행이 차단됩니다.
    """
    from app.config import PROJECT_ROOT
    from app.mcp.policy import ALLOW, Policy, ToolMeta, evaluate, hard_block, profile_call

    workspace = PROJECT_ROOT / "workspace"
    source = (PROJECT_SKILLS / "csv-profile" / "scripts" / "profile_csv.py").read_text(encoding="utf-8")
    meta = ToolMeta(server="sandbox", tool="run_python_file", trusted=True)
    profile = profile_call(
        meta, {"file_path": ".mado/skills/csv-profile/scripts/profile_csv.py"}, workspace,
        read_file=lambda _path: source,
    )
    assert hard_block(profile, workspace, PROJECT_ROOT, []) is None
    assert evaluate(profile, Policy(mode="default")).effect == ALLOW


def test_the_bundled_csv_script_runs_like_the_sandbox_runs_it(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "sales.csv").write_text(
        "지역,금액\n서울,\"1,200\"\n부산,300\n대구,\n", encoding="utf-8"
    )
    (workspace / "data" / "legacy.csv").write_bytes("이름;나이\n홍길동;31\n김철수;abc\n".encode("cp949"))
    (workspace / ".mado").mkdir()
    (workspace / ".mado" / "hidden.csv").write_text("a\n1\n", encoding="utf-8")

    script = PROJECT_SKILLS / "csv-profile" / "scripts" / "profile_csv.py"
    monkeypatch.chdir(workspace)
    # 샌드박스의 run_python_file 과 같이: 작업 공간이 cwd, 인자 없음, 스크립트를 exec.
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"),
         {"__name__": "__main__", "__file__": str(script)})

    report = (workspace / "csv-profile.md").read_text(encoding="utf-8")
    assert "## data/sales.csv" in report and "## data/legacy.csv" in report
    assert "hidden.csv" not in report, "점으로 시작하는 폴더는 건너뜁니다"
    assert "| 금액 | 숫자 | 1 | 최소 300 · 최대 1,200" in report
    assert "인코딩 `cp949` · 구분자 `;`" in report
    assert "숫자로 읽히는 값 1개 섞임" in report
