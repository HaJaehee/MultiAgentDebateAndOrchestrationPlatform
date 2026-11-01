"""끊긴 쓰기의 디스크 확인과 커널 셀 재실행 (ADR-046).

지키려는 것.

1. **쓰기 확인** — 쓰기 도구가 도는 도중에 끊기면, 이어 갈 때 파일을 직접 비교해 반영 여부를 모델에게 알린다.
   확인할 수 없는 호출은 예전처럼 "결과 모름" 이고, 작업 공간 밖이나 원격 서버의 경로는 읽지 않는다.
2. **셀 고르기** — 다시 실행할 셀은 도구 기록에서 고른다. 끝까지 실행된 것만, 커널이 비워진 뒤의 것만.
3. **재실행은 사람이 고른다** — `continue` 는 코드를 돌리지 않고 커널이 비었다고 알린다. `replay` 는 커널을 비우고
   셀을 순서대로 돌린다. 첫 실패에서 멈추고, 지금의 보안 판정을 받고, 작업 공간 파일은 재실행 전으로 돌린다.
4. **겹치지 않는다** — 다시 실행한 기록은 다음 재실행의 셀에 들어가지 않는다.
"""

import asyncio
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

import app.agents.llm as llm_module
from app.agents import write_probe
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_loop import (
    FILES_DRIFTED_NOTICE,
    SPEECH_STATE_VERSION,
    UNKNOWN_TOOL_RESULT,
    WRITES_CHECKED_NOTICE,
)
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import (
    TURN_FAILED,
    MessageModel,
    SessionAgentModel,
    SessionModel,
    ToolCallRecordModel,
    TurnModel,
)
from app.database.session import get_session_factory, init_db
from app.mcp.manager import compose_scope
from app.mcp.policy import ToolMeta
from app.mcp.pool import MCPRuntimePool
from app.orchestration import kernel_replay, turns
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from tests.fake_llm import FakeLLMCaller
from tests.test_turn_resume import DB_URL, REQUEST

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _meta(tool: str, **kwargs: Any) -> ToolMeta:
    return ToolMeta(server="filesystem", tool=tool, **kwargs)


# =============================================================== 1. 쓰기 확인: 값 만들기


def test_a_new_file_is_absent_before_and_has_a_known_hash_after(tmp_path):
    plan = write_probe.plan_write(_meta("write_file"), {"path": "a.py", "content": "한글\n"}, tmp_path)
    assert plan["path"] == "a.py" and plan["before"] is None
    assert plan["expected"] == _sha("한글\n".encode("utf-8"))


def test_an_append_is_expected_to_end_with_the_chunk(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\n")
    meta = ToolMeta(server="sandbox", tool="append_workspace_file")
    plan = write_probe.plan_write(meta, {"filename": "a.txt", "content": "two\n"}, tmp_path)
    assert plan["before"] == _sha(b"one\n") and plan["expected"] == _sha(b"one\ntwo\n")


def test_an_edit_has_no_expected_content(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\n")
    plan = write_probe.plan_write(_meta("edit_file"), {"path": "a.txt", "edits": []}, tmp_path)
    assert plan["before"] == _sha(b"one\n") and plan["expected"] is None


@pytest.mark.parametrize("meta, arguments", [
    (_meta("read_file"), {"path": "a.txt"}),
    (_meta("write_file", trusted=False), {"path": "a.txt", "content": "x"}),
    (_meta("write_file", remote_host="files.example.com"), {"path": "a.txt", "content": "x"}),
    (_meta("write_file"), {"content": "경로 없음"}),
    (_meta("write_file"), {"path": "../밖.txt", "content": "x"}),
])
def test_calls_that_cannot_be_checked_are_left_alone(tmp_path, meta, arguments):
    assert write_probe.plan_write(meta, arguments, tmp_path) is None


def test_a_loopback_server_counts_as_this_machine(tmp_path):
    meta = _meta("write_file", remote_host="127.0.0.1")
    assert write_probe.plan_write(meta, {"path": "a.txt", "content": "x"}, tmp_path) is not None


def test_only_regular_files_of_a_reasonable_size_are_read(tmp_path, monkeypatch):
    (tmp_path / "folder").mkdir()
    assert write_probe.plan_write(_meta("write_file"), {"path": "folder", "content": "x"}, tmp_path) is None
    (tmp_path / "big.bin").write_bytes(b"12345")
    monkeypatch.setattr(write_probe, "MAX_HASHED_BYTES", 4)
    assert write_probe.plan_write(_meta("write_file"), {"path": "big.bin", "content": "x"}, tmp_path) is None


# =============================================================== 1. 쓰기 확인: 도구 루프


class _Message(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


def _install(monkeypatch, replies: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """`replies` 를 차례로 돌려주는 가짜 엔드포인트. 각 요청이 보낸 메시지를 모아 돌려줍니다."""
    remaining = list(replies)
    sent: List[List[Dict[str, Any]]] = []

    async def fake_acompletion(**kwargs):
        sent.append([dict(m) for m in kwargs["messages"]])
        fake_acompletion.current = remaining.pop(0)

        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=fake_acompletion.current["content"]),
            )])

        return stream()

    def fake_builder(chunks, messages=None):
        reply = fake_acompletion.current
        calls = [
            SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for i, (name, args) in enumerate(reply.get("calls", []))
        ] or None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=_Message(content=reply["content"], tool_calls=calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)
    return sent


class _DiskMCP:
    """쓰기를 정말 디스크에 합니다. `cut` 은 서버가 내려가는 자리입니다 (쓰기 전·일부만·쓴 뒤)."""

    def __init__(self, workspace: Path, cut: Optional[str] = None):
        self.workspace = workspace
        self.cut = cut
        self.calls: List[str] = []

    def get_openai_tools_for_servers(self, servers):
        return [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]

    def tool_meta(self, name: str) -> ToolMeta:
        return _meta(name)

    async def execute_tool(self, name, args, scope=None, actor=None):
        self.calls.append(name)
        target = self.workspace / (args.get("path") or args.get("filename"))
        content = args.get("content", "")
        if self.cut == "before":
            raise asyncio.CancelledError()
        if self.cut == "partial":
            target.write_text(content[:2], encoding="utf-8", newline="\n")
            raise asyncio.CancelledError()
        with open(target, "a" if name == "append_workspace_file" else "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        if self.cut == "after":
            raise asyncio.CancelledError()
        return "저장됨", "success"


def _coder() -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", api_key="sk-test")


async def _cut_write(monkeypatch, workspace: Path, name: str, args: Dict[str, Any], cut: str) -> Dict[str, Any]:
    """도구 하나를 부르다 끊긴 발언의 마지막 초안."""
    _install(monkeypatch, [{"content": "씁니다", "calls": [(name, args)]}])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    with pytest.raises(asyncio.CancelledError):
        await LLMCaller(mcp_manager=_DiskMCP(workspace, cut=cut)).call_agent(
            _coder(), [{"role": "user", "content": "구현"}], checkpoint=checkpoint,
        )
    return saved[-1]


async def _resume(monkeypatch, workspace: Path, draft: Dict[str, Any]) -> Tuple[str, str]:
    """이어 간 첫 요청의 (끊긴 호출의 도구 결과, 재개 안내)."""
    sent = _install(monkeypatch, [{"content": "이어서 마무리"}])
    mcp = _DiskMCP(workspace)
    await LLMCaller(mcp_manager=mcp).call_agent(
        _coder(), [{"role": "user", "content": "쓰이지 않습니다"}], resume_state=draft,
    )
    assert mcp.calls == [], "이어 가는 것만으로 쓰기를 되풀이하지 않습니다"
    request = sent[0]
    return [m for m in request if m.get("role") == "tool"][-1]["content"], request[-1]["content"]


WRITE = ("write_file", {"path": "a.py", "content": "print('완성')\n"})
APPEND = ("append_workspace_file", {"filename": "a.py", "content": "둘째 조각\n"})


@pytest.mark.asyncio
async def test_a_write_that_reached_the_disk_is_reported_as_applied(monkeypatch, tmp_path):
    draft = await _cut_write(monkeypatch, tmp_path, *WRITE, cut="after")
    result, notice = await _resume(monkeypatch, tmp_path, draft)
    assert result == write_probe.APPLIED_RESULT.format(path="a.py")
    assert WRITES_CHECKED_NOTICE in notice


@pytest.mark.asyncio
async def test_a_write_that_never_ran_is_reported_as_not_applied(monkeypatch, tmp_path):
    draft = await _cut_write(monkeypatch, tmp_path, *WRITE, cut="before")
    result, _notice = await _resume(monkeypatch, tmp_path, draft)
    assert result == write_probe.NOT_APPLIED_RESULT.format(
        path="a.py", absent=" (호출 전에도 지금도 없는 파일입니다)",
    )


@pytest.mark.asyncio
async def test_a_partial_write_asks_the_model_to_look(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("옛 내용\n", encoding="utf-8")
    draft = await _cut_write(monkeypatch, tmp_path, *WRITE, cut="partial")
    result, _notice = await _resume(monkeypatch, tmp_path, draft)
    assert result == write_probe.OTHER_RESULT.format(path="a.py")


@pytest.mark.asyncio
@pytest.mark.parametrize("cut, expected", [
    ("after", write_probe.APPLIED_RESULT.format(path="a.py")),
    ("before", write_probe.NOT_APPLIED_RESULT.format(path="a.py", absent="")),
])
async def test_an_append_is_never_mistaken_for_one_that_must_be_repeated(monkeypatch, tmp_path, cut, expected):
    """다시 부르면 같은 조각이 두 번 들어가는 호출입니다. 이 확인이 가장 값진 자리입니다."""
    (tmp_path / "a.py").write_text("첫 조각\n", encoding="utf-8", newline="\n")
    draft = await _cut_write(monkeypatch, tmp_path, *APPEND, cut=cut)
    result, _notice = await _resume(monkeypatch, tmp_path, draft)
    assert result == expected


@pytest.mark.asyncio
async def test_an_edit_can_only_say_that_the_file_changed(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("옛 내용\n", encoding="utf-8")
    draft = await _cut_write(monkeypatch, tmp_path, "edit_file", {"path": "a.py", "content": "새 내용\n"}, cut="after")
    result, _notice = await _resume(monkeypatch, tmp_path, draft)
    assert result == write_probe.CHANGED_RESULT.format(path="a.py")


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_read_on_resume_stays_unknown(monkeypatch, tmp_path):
    draft = await _cut_write(monkeypatch, tmp_path, *WRITE, cut="before")
    (tmp_path / "a.py").mkdir()
    result, notice = await _resume(monkeypatch, tmp_path, draft)
    assert result == UNKNOWN_TOOL_RESULT and WRITES_CHECKED_NOTICE not in notice


@pytest.mark.asyncio
async def test_the_state_before_a_write_is_saved_before_it_runs_and_cleared_after(monkeypatch, tmp_path):
    _install(monkeypatch, [{"content": "씁니다", "calls": [WRITE]}, {"content": "끝"}])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    await LLMCaller(mcp_manager=_DiskMCP(tmp_path)).call_agent(
        _coder(), [{"role": "user", "content": "구현"}], checkpoint=checkpoint,
    )
    assert len(saved) == 3, "도구를 부른 직후, 실행 직전, 결과를 받은 뒤"
    assert saved[0]["open_writes"] == {}
    assert saved[1]["open_writes"]["call_0"]["before"] is None
    assert saved[2]["open_writes"] == {}
    (entry,) = saved[2]["known_files"].values()
    assert entry == {"path": "a.py", "sha256": _sha("print('완성')\n".encode("utf-8"))}
    assert saved[2]["version"] == SPEECH_STATE_VERSION


@pytest.mark.asyncio
async def test_a_speech_without_drafts_reads_nothing(monkeypatch, tmp_path):
    def must_not_run(*args, **kwargs):
        raise AssertionError("이어 갈 일이 없는 발언은 파일을 읽지 않습니다")

    monkeypatch.setattr(write_probe, "plan_write", must_not_run)
    _install(monkeypatch, [{"content": "씁니다", "calls": [WRITE]}, {"content": "끝"}])
    await LLMCaller(mcp_manager=_DiskMCP(tmp_path)).call_agent(_coder(), [{"role": "user", "content": "구현"}])


@pytest.mark.asyncio
async def test_a_denied_write_is_not_read_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(write_probe, "plan_write", lambda *a, **k: pytest.fail("거부된 호출의 경로를 읽었습니다"))

    class Gate:
        async def check(self, agent, name, args, mcp):
            return SimpleNamespace(allowed=False, output="거부", status="denied", audit={})

        def filter_tools(self, key, tools, mcp):
            return tools

    _install(monkeypatch, [{"content": "씁니다", "calls": [WRITE]}, {"content": "끝"}])

    async def checkpoint(state):
        pass

    mcp = _DiskMCP(tmp_path)
    await LLMCaller(mcp_manager=mcp).call_agent(
        _coder(), [{"role": "user", "content": "구현"}], checkpoint=checkpoint, tool_gate=Gate(),
    )
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_a_file_changed_after_the_last_write_is_named_once(monkeypatch, tmp_path):
    _install(monkeypatch, [{"content": "씁니다", "calls": [WRITE]}])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    # 쓰기는 끝났고, 다음 LLM 호출이 없어 끊긴 발언입니다.
    with pytest.raises(Exception):
        await LLMCaller(mcp_manager=_DiskMCP(tmp_path)).call_agent(
            _coder(), [{"role": "user", "content": "구현"}], checkpoint=checkpoint,
        )
    (tmp_path / "a.py").write_text("사람이 고쳤습니다\n", encoding="utf-8")

    _result, notice = await _resume(monkeypatch, tmp_path, saved[-1])
    assert FILES_DRIFTED_NOTICE.format(paths="`a.py`") in notice


@pytest.mark.asyncio
async def test_an_unchanged_file_is_not_named(monkeypatch, tmp_path):
    _install(monkeypatch, [{"content": "씁니다", "calls": [WRITE]}])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    with pytest.raises(Exception):
        await LLMCaller(mcp_manager=_DiskMCP(tmp_path)).call_agent(
            _coder(), [{"role": "user", "content": "구현"}], checkpoint=checkpoint,
        )
    _result, notice = await _resume(monkeypatch, tmp_path, saved[-1])
    assert "내용이 달라진 파일" not in notice


def test_reconcile_names_a_changed_file_only_once(tmp_path):
    target = tmp_path / "a.txt"
    target.write_bytes(b"one")
    known = {str(target): {"path": "a.txt", "sha256": _sha(b"zero")}}
    assert write_probe.reconcile({}, known)[1] == ["a.txt"]
    assert write_probe.reconcile({}, known)[1] == [], "알린 파일은 지금 해시로 바꿔 둡니다"


@pytest.mark.asyncio
async def test_the_engines_kernel_note_reaches_the_model(monkeypatch, tmp_path):
    draft = await _cut_write(monkeypatch, tmp_path, *WRITE, cut="before")
    _result, notice = await _resume(monkeypatch, tmp_path, {**draft, "kernel_note": "[커널] 비었습니다."})
    assert notice.endswith("[커널] 비었습니다.")


# =============================================================== 2. 셀 고르기


def _row(code: str = "x = 1", *, agent: str = "coder", tool: str = "sandbox__execute_python_code",
         output: str = "execution_status: RAN_UNVERIFIED\n끝", status: str = "success",
         message_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "tool_name": tool, "arguments": {"code": code}, "output": output, "status": status,
        "agent_key": agent, "message_id": message_id,
    }


NS_CODER = compose_scope("sandbox", "s1", "coder")


def _codes(found: Dict[str, List[Dict[str, Any]]], namespace: str = NS_CODER) -> List[str]:
    return [row["arguments"]["code"] for row in found.get(namespace, [])]


def test_only_cells_that_ran_to_the_end_are_chosen():
    found = turns.kernel_cells([
        _row("a"),
        _row("b", output="execution_status: RAN_WITH_ERRORS\n경고"),
        _row("c", output="execution_status: RAN_NO_OUTPUT\n"),
        _row("d", output="execution_status: FAILED\nNameError"),
        _row("e", output="execution_status: TIMEOUT\n멈춤"),
        _row("f", output="REFUSED - 도구가 실행되지 않았습니다", status="denied"),
        _row("g", output="Tool execution failed", status="error"),
    ], "s1")
    assert _codes(found) == ["a", "b", "c"]


def test_a_script_run_counts_as_a_cell_and_other_tools_do_not():
    found = turns.kernel_cells([
        _row("a", tool="sandbox__run_python_file"), _row("b", tool="filesystem__write_file"),
    ], "s1")
    assert _codes(found) == ["a"]


def test_cells_before_the_kernel_was_emptied_are_dropped():
    reset = _row(tool="sandbox__reset_kernel_state", output='Namespace "x" kernel restarted; ...')
    found = turns.kernel_cells([_row("a"), reset, _row("b")], "s1")
    assert _codes(found) == ["b"]


def test_a_failed_reset_empties_nothing():
    reset = _row(tool="sandbox__reset_kernel_state", output="Provide the active_namespace value")
    found = turns.kernel_cells([_row("a"), reset, _row("b")], "s1")
    assert _codes(found) == ["a", "b"]


@pytest.mark.parametrize("output", [
    "execution_status: TIMEOUT\nThe code was interrupted. The kernel had to be restarted, so variables are gone.",
    "execution_status: FAILED\n[sandbox] Kernel was unresponsive and has been restarted",
])
def test_a_kernel_restarted_by_a_timeout_loses_what_came_before(output):
    found = turns.kernel_cells([_row("a"), _row("b", output=output), _row("c")], "s1")
    assert _codes(found) == ["c"]


def test_each_agent_has_its_own_kernel():
    found = turns.kernel_cells([_row("a"), _row("b", agent="critic")], "s1")
    assert _codes(found) == ["a"] and _codes(found, compose_scope("sandbox", "s1", "critic")) == ["b"]


def test_agents_with_similar_keys_keep_their_own_cells():
    session = str(uuid.uuid4())
    found = turns.kernel_cells([_row("a", agent="critic"), _row("b", agent="critic2")], session)
    assert _codes(found, compose_scope("sandbox", session, "critic")) == ["a"]
    assert _codes(found, compose_scope("sandbox", session, "critic2")) == ["b"]


def test_a_server_without_agent_scope_runs_one_kernel_for_everyone():
    """샌드박스를 다른 키로 등록하면 대화 단위 커널 하나입니다. 셀도 한 줄로 묶어 기록 순서대로 돌립니다."""
    tool = "python__execute_python_code"
    found = turns.kernel_cells([_row("a", agent="coder", tool=tool), _row("b", agent="critic", tool=tool)], "s1")
    assert _codes(found, "s1") == ["a", "b"]


def test_replayed_rows_do_not_count_again():
    found = turns.kernel_cells([_row("a"), _row("a", message_id="note-1"), _row("b")], "s1", ["note-1"])
    assert _codes(found) == ["a", "b"]


# =============================================================== 3·4. 재실행


class _Sandbox:
    """커널을 흉내 내는 도구 서버. 코드를 정말 실행하므로 변수와 파일이 남습니다."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.kernels: Dict[str, Dict[str, Any]] = {}
        self.ran: List[str] = []
        self.resets: List[str] = []

    def get_openai_tools_for_servers(self, servers):
        return []

    def tool_meta(self, name: str) -> ToolMeta:
        return ToolMeta(server="sandbox", tool=name.split("__", 1)[-1])

    def hard_refusal(self, name, args):
        return None

    async def execute_tool(self, name, args, scope=None, actor=None):
        if name.endswith("reset_kernel_state"):
            self.resets.append(args["namespace"])
            self.kernels.pop(args["namespace"], None)
            return f'Namespace "{args["namespace"]}" had no live kernel.', "success"
        kernel = self.kernels.setdefault(compose_scope("sandbox", scope, actor), {"WS": self.workspace})
        self.ran.append(args["code"])
        try:
            exec(args["code"], kernel)  # noqa: S102 - 이 파일이 적은 코드만 돕니다
        except Exception as exc:  # noqa: BLE001
            return (
                f"execution_status: FAILED\n뜻\n\n--- stderr ---\nTraceback\n{type(exc).__name__}: {exc}\n"
                "\n--- stdout ---\n(없음)", "success",
            )
        return "execution_status: RAN_UNVERIFIED\n뜻\n\n--- stdout ---\n끝", "success"


class _CellLLM(FakeLLMCaller):
    """coder 의 첫 발언이 `cells` 를 실행하고 판을 남긴 뒤 끊깁니다. 이어 간 발언은 `resumed` 에 남습니다."""

    def __init__(self, sandbox: Optional[_Sandbox] = None, cells: Tuple[str, ...] = (), cut_again: bool = False):
        super().__init__()
        self.sandbox = sandbox
        self.cells = cells
        self.cut_again = cut_again
        self.resumed: List[Dict[str, Any]] = []

    async def call_agent(self, agent, messages, custom_instructions="", on_tool_call=None, *args,
                         checkpoint=None, resume_state=None, **kwargs):
        last = messages[-1]["content"] if messages else ""
        if agent.key == "coder" and resume_state is not None:
            self.resumed.append(resume_state)
            if self.cut_again:
                raise asyncio.CancelledError()
            return "이어서 마무리", list(resume_state["tool_logs"])
        if agent.key == "coder" and self.cells and checkpoint is not None and "[결정 장부 갱신]" not in last:
            logs = []
            for code in self.cells:
                output, status = await self.sandbox.execute_tool(
                    "sandbox__execute_python_code", {"code": code, "namespace": "new"},
                    scope=kwargs.get("session_id"), actor=agent.key,
                )
                log = {
                    "tool_name": "sandbox__execute_python_code", "arguments": {"code": code, "namespace": "new"},
                    "output": output, "status": status,
                }
                await on_tool_call(log)
                logs.append(log)
            await checkpoint({
                "version": SPEECH_STATE_VERSION, "messages": [{"role": "user", "content": "모델이 보던 메시지"}],
                "segments": ["계산합니다"], "used": 1, "limit": 30, "window": 128000, "tool_logs": logs,
                "leaked_retries": 0, "context_asked": False, "announced_bands": [], "turn_anchor": "",
            })
            raise asyncio.CancelledError()
        return await super().call_agent(
            agent, messages, custom_instructions, on_tool_call, *args, checkpoint=checkpoint,
            resume_state=resume_state, **kwargs,
        )


def _agents(servers=("sandbox",)) -> AgentPool:
    return AgentPool({
        key: AgentConfig(
            name=name, role=role, model="fake/model", api_key="k", allowed_mcp_servers=list(servers),
        )
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("coder", "Senior Engineer", "Implementation"),
        )
    })


def _engine(monkeypatch, llm, sandbox: _Sandbox) -> OrchestratorEngine:
    engine = OrchestratorEngine(agent_pool=_agents(), llm_caller=llm)
    monkeypatch.setattr(engine.ctx, "mcp_for", lambda state: sandbox)
    return engine


@pytest.fixture
def repo(tmp_path, workspace_checkpoints):
    root = tmp_path / "ws"
    root.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.name", "tester")
    run("config", "user.email", "t@example.com")
    (root / "keep.txt").write_text("처음\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return root


async def _session(workspace: Path) -> str:
    await init_db(DB_URL)
    sid = f"restore-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="복원", strategy="sequential_debate", max_rounds=1,
            active_agents=["orchestrator", "coder"], workspace_dir=str(workspace),
        ))
        await db.commit()
    return sid


async def _cut(monkeypatch, workspace: Path, cells: Tuple[str, ...]) -> Tuple[str, str]:
    """`cells` 를 돌리다 서버가 내려가고 다시 뜬 대화. (대화 id, 턴 id). 커널은 함께 사라집니다."""
    sid = await _session(workspace)
    sandbox = _Sandbox(workspace)
    with pytest.raises(asyncio.CancelledError):
        await _engine(monkeypatch, _CellLLM(sandbox, cells), sandbox).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        info = await turns.unfinished_turn(db, sid)
    return sid, info.turn_id


async def _replay_notes(sid: str) -> List[MessageModel]:
    async with get_session_factory(DB_URL)() as db:
        rows = (await db.execute(select(MessageModel).where(MessageModel.session_id == sid))).scalars().all()
    return [m for m in rows if turns.kind_of(m) == turns.KIND_KERNEL_REPLAY]


@git_required
@pytest.mark.asyncio
async def test_replay_empties_the_kernel_and_runs_the_cells_in_order(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1", "y = x + 1"))
    async with get_session_factory(DB_URL)() as db:
        assert (await turns.unfinished_turn(db, sid)).replayable_cells == 2

    sandbox, llm = _Sandbox(repo), _CellLLM()
    state = await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")

    assert state.status == "completed"
    assert sandbox.ran == ["x = 1", "y = x + 1"] and len(sandbox.resets) == 1
    assert sandbox.kernels[compose_scope("sandbox", sid, "coder")]["y"] == 2
    (draft_state,) = llm.resumed
    assert draft_state["kernel_note"] == kernel_replay.KERNEL_REPLAYED_NOTE.format(ran=2)

    (note,) = await _replay_notes(sid)
    assert note.turn_meta["kernels"] == [{"agents": ["coder"], "planned": 2, "ran": 2, "stopped": ""}]
    async with get_session_factory(DB_URL)() as db:
        rows = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.message_id == note.id)
        )).scalars().all()
    assert sorted(r.arguments["code"] for r in rows) == ["x = 1", "y = x + 1"]
    assert all(r.decision == "allow" and r.agent_key == "coder" for r in rows), "지금의 보안 판정을 받았습니다"


@git_required
@pytest.mark.asyncio
async def test_continue_runs_no_code_and_says_the_kernel_is_empty(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    sandbox, llm = _Sandbox(repo), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "continue")

    assert sandbox.ran == [] and sandbox.resets == []
    assert llm.resumed[0]["kernel_note"] == kernel_replay.KERNEL_LOST_NOTE
    assert await _replay_notes(sid) == []


@git_required
@pytest.mark.asyncio
async def test_a_turn_that_failed_without_a_restart_keeps_its_kernel(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    async with get_session_factory(DB_URL)() as db:
        (await db.get(TurnModel, turn_id)).status = TURN_FAILED
        await db.commit()
    sandbox, llm = _Sandbox(repo), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "continue")
    assert "kernel_note" not in llm.resumed[0], "엔진 오류로 멈췄을 뿐 커널은 살아 있습니다"


@git_required
@pytest.mark.asyncio
async def test_finishing_runs_no_code(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    sandbox = _Sandbox(repo)
    await _engine(monkeypatch, _CellLLM(), sandbox).resume_turn(sid, turn_id, "finish")
    assert sandbox.ran == [] and await _replay_notes(sid) == []


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="알 수 없는 재개 방식"):
        await OrchestratorEngine(agent_pool=_agents(), llm_caller=_CellLLM()).resume_turn("s", "t", "rewind")


@git_required
@pytest.mark.asyncio
async def test_replay_with_nothing_to_run_just_continues(monkeypatch, repo):
    sid = await _session(repo)
    with pytest.raises(asyncio.CancelledError):
        llm = _CellLLM()
        llm.call_agent = _cancel_on_coder(llm.call_agent)
        await _engine(monkeypatch, llm, _Sandbox(repo)).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        info = await turns.unfinished_turn(db, sid)
    assert info.replayable_cells == 0

    sandbox = _Sandbox(repo)
    state = await _engine(monkeypatch, _CellLLM(), sandbox).resume_turn(sid, info.turn_id, "replay")
    assert state.status == "completed" and sandbox.resets == [] and await _replay_notes(sid) == []


def _cancel_on_coder(call_agent):
    async def wrapped(agent, messages, *args, **kwargs):
        if agent.key == "coder":
            raise asyncio.CancelledError()
        return await call_agent(agent, messages, *args, **kwargs)
    return wrapped


@git_required
@pytest.mark.asyncio
async def test_replay_stops_at_the_first_failure_and_says_where(monkeypatch, repo):
    (repo / "data.txt").write_text("자료", encoding="utf-8")
    cells = ("x = 1", "y = open(WS / 'data.txt', encoding='utf-8').read()", "z = 3")
    sid, turn_id = await _cut(monkeypatch, repo, cells)
    (repo / "data.txt").unlink()

    sandbox, llm = _Sandbox(repo), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")

    assert sandbox.ran == list(cells[:2]), "실패한 셀 뒤의 코드는 돌리지 않습니다"
    note = llm.resumed[0]["kernel_note"]
    assert "셀 3개 중 1개까지만" in note and "2번째 셀이 실패했습니다" in note and "FileNotFoundError" in note
    (message,) = await _replay_notes(sid)
    assert message.turn_meta["kernels"][0]["ran"] == 1 and "FileNotFoundError" in message.content


@git_required
@pytest.mark.asyncio
async def test_replay_goes_through_the_current_security_settings(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1", "y = 2"))
    async with get_session_factory(DB_URL)() as db:
        (await db.get(SessionModel, sid)).tool_mode = "read_only"
        await db.commit()

    sandbox, llm = _Sandbox(repo), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")

    assert sandbox.ran == [], "끊긴 사이 조인 설정을 옛 기록으로 건너뛰지 않습니다"
    assert "보안 판정에 막혔습니다" in llm.resumed[0]["kernel_note"]
    (note,) = await _replay_notes(sid)
    async with get_session_factory(DB_URL)() as db:
        (row,) = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.message_id == note.id)
        )).scalars().all()
    assert row.status == "denied" and row.decision == "deny", "막힌 호출도 감사 기록에 남습니다"


@git_required
@pytest.mark.asyncio
async def test_replay_does_not_call_a_tool_that_is_gone(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    sandbox, llm = _Sandbox(repo), _CellLLM()
    monkeypatch.setattr(sandbox, "tool_meta", lambda name: None)  # 끊긴 사이 서버 설정을 지웠습니다
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")
    assert sandbox.ran == [] and sandbox.resets == []
    assert "도구를 지금은 쓸 수 없습니다" in llm.resumed[0]["kernel_note"]


@git_required
@pytest.mark.asyncio
async def test_replay_keeps_to_the_tools_this_conversations_agent_may_use(monkeypatch, repo):
    """에이전트 설정은 대화에 고정됩니다 (`frozen_agents`). 재실행도 일반 발언과 같은 값을 봅니다."""
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    async with get_session_factory(DB_URL)() as db:
        (coder,) = (await db.execute(select(SessionAgentModel).where(
            SessionAgentModel.session_id == sid, SessionAgentModel.agent_key == "coder",
        ))).scalars().all()
        coder.config_snapshot = {**coder.config_snapshot, "allowed_mcp_servers": []}
        await db.commit()
    sandbox, llm = _Sandbox(repo), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")
    assert sandbox.ran == [] and "도구를 지금은 쓸 수 없습니다" in llm.resumed[0]["kernel_note"]


@git_required
@pytest.mark.asyncio
async def test_a_stop_request_ends_the_replay(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    control = TurnControl()
    control.request_stop()
    sandbox = _Sandbox(repo)
    await _engine(monkeypatch, _CellLLM(), sandbox).resume_turn(sid, turn_id, "replay", control=control)
    assert sandbox.ran == []
    (note,) = await _replay_notes(sid)
    assert note.turn_meta["kernels"][0]["stopped"] == "유저가 정지를 요청했습니다"


@git_required
@pytest.mark.asyncio
async def test_files_touched_by_the_replay_go_back_to_what_they_were(monkeypatch, repo):
    """옛 셀이 파일을 쓰면, 그 뒤에 고친 내용이 옛 내용으로 돌아갑니다. 변수만 되살려야 합니다."""
    cells = (
        "open(WS / 'out.txt', 'w', encoding='utf-8').write('셀이 쓴 내용')",
        "open(WS / 'chart.txt', 'w', encoding='utf-8').write('셀이 만든 파일')",
        "z = 3",
    )
    sid, turn_id = await _cut(monkeypatch, repo, cells)
    (repo / "out.txt").write_text("그 뒤에 고친 내용", encoding="utf-8")
    (repo / "chart.txt").unlink()

    sandbox = _Sandbox(repo)
    await _engine(monkeypatch, _CellLLM(), sandbox).resume_turn(sid, turn_id, "replay")

    assert sandbox.kernels[compose_scope("sandbox", sid, "coder")]["z"] == 3
    assert (repo / "out.txt").read_text(encoding="utf-8") == "그 뒤에 고친 내용"
    assert not (repo / "chart.txt").exists(), "그사이 지운 파일이 되살아나지 않습니다"
    (note,) = await _replay_notes(sid)
    assert "파일 2개를 되돌렸습니다" in note.content and "`out.txt`" in note.content


@pytest.mark.asyncio
async def test_no_snapshot_means_no_replay(monkeypatch, tmp_path):
    """스냅샷이 없으면 파일을 지킬 수 없습니다. 이 시험은 체크포인트가 꺼진 기본 환경에서 돕니다."""
    sid, turn_id = await _cut(monkeypatch, tmp_path, ("x = 1",))
    sandbox, llm = _Sandbox(tmp_path), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")

    assert sandbox.ran == [] and sandbox.resets == []
    assert llm.resumed[0]["kernel_note"] == kernel_replay.KERNEL_NOT_REPLAYED_NOTE.format(
        reason="작업 공간 스냅샷을 만들지 못해 파일을 지킬 수 없습니다",
    )
    (note,) = await _replay_notes(sid)
    assert note.turn_meta["kernels"][0]["ran"] == 0


@git_required
@pytest.mark.asyncio
async def test_another_turn_on_the_same_workspace_means_no_replay(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1",))
    monkeypatch.setattr(MCPRuntimePool, "shared_with", lambda self, workspace, holder: ["다른-대화"])
    sandbox, llm = _Sandbox(repo), _CellLLM()
    await _engine(monkeypatch, llm, sandbox).resume_turn(sid, turn_id, "replay")
    assert sandbox.ran == []
    assert "같은 작업 공간에서 다른 대화가 진행 중입니다" in llm.resumed[0]["kernel_note"]


@git_required
@pytest.mark.asyncio
async def test_a_second_cut_does_not_count_replayed_cells_twice(monkeypatch, repo):
    sid, turn_id = await _cut(monkeypatch, repo, ("x = 1", "y = 2"))
    sandbox = _Sandbox(repo)
    with pytest.raises(asyncio.CancelledError):
        await _engine(monkeypatch, _CellLLM(cut_again=True), sandbox).resume_turn(sid, turn_id, "replay")
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        assert (await turns.unfinished_turn(db, sid)).replayable_cells == 2

    again = _Sandbox(repo)
    await _engine(monkeypatch, _CellLLM(), again).resume_turn(sid, turn_id, "replay")
    assert again.ran == ["x = 1", "y = 2"], "같은 셀을 두 번 돌리지 않습니다"
