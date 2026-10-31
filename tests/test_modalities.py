"""에이전트 카드의 `모달` — @언급한 파일과 도구가 돌려준 파일을 모델에게 내용째 전달합니다 (ADR-035).

* 모달이 비어 있으면 요청은 지금까지와 **똑같습니다**: 글과 파일 경로뿐입니다.
* 체크한 형식만 그 에이전트에게 BASE64 로 갑니다. 다른 에이전트, 다른 형식은 경로만 받습니다.
* 메시지에는 파일을 가리키는 블록만 들고, BASE64 는 요청을 보낼 때만 만듭니다 — 초안(DB)과 도구 기록에 남지 않습니다.
* 모델이 첨부를 거절해도 발언을 잃지 않습니다. 내용 없이 이어 가고 그 사실을 적습니다.
"""

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import litellm
import pytest
from litellm.litellm_core_utils.prompt_templates.factory import anthropic_messages_pt
from litellm.llms.vertex_ai.gemini.transformation import _gemini_convert_messages_with_history
from pydantic import ValidationError

from app.agents import media
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_context import (
    estimate_tokens,
    fit_context_window,
    fit_tool_loop_context,
    merge_consecutive_roles,
)
from app.agents.llm_recovery import AnswerRecoveryMixin
from app.config import AgentConfig, read_conf_file, set_agent_allowed_modalities_in_conf_file, write_conf_file
from app.database.models import TurnModel
from app.database.session import get_session_factory
from app.mcp.client import MCPClientConnection, ToolMedia, ToolOutput
from app.orchestration.control import TurnControl
from app.workspace_files import MentionAgent, expand_mentions
from tests.fake_llm import FakeLLMCaller
from tests.test_interaction import HookedCaller
from tests.test_resilience import _engine, _make_session

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAEAQH/7yMK/gAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode()


def _agent(*modalities: str, **fields: Any) -> Agent:
    return Agent(
        key="vision", name="Vision", role="Review", model=fields.pop("model", "openai/test-model"),
        api_key="sk-test", allowed_modalities=list(modalities), **fields,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "mockup.png").write_bytes(PNG)
    (tmp_path / "uploads" / "talk.mp3").write_bytes(b"ID3-audio")
    (tmp_path / "uploads" / "demo.mp4").write_bytes(b"mp4-video")
    (tmp_path / "uploads" / "spec.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "uploads" / "notes.md").write_text("# 회의 메모\n결론은 A 입니다.", encoding="utf-8")
    (tmp_path / "uploads" / "main.py").write_text("print('hi')", encoding="utf-8")
    return tmp_path


class _NoTools:
    def __init__(self, workspace: Any = None) -> None:
        self.workspace = workspace

    def get_openai_tools_for_servers(self, servers):
        return []


def _msg(content: Any = "", tool_calls: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=None,
        model_dump=lambda: {"role": "assistant", "content": content},
    )


def _reply(message: SimpleNamespace, finish: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


def _capture(replies: List[Any]):
    """요청을 모아 두는 가짜 `acompletion`. 스트리밍은 받지 않는 엔드포인트처럼 답합니다."""
    sent: List[Dict[str, Any]] = []

    async def fake(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        sent.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return sent, fake


# ------------------------------------------------------------------ 설정


def test_the_setting_accepts_only_the_four_formats():
    assert AgentConfig(name="a", role="b").allowed_modalities == [], "기본은 비어 있습니다"
    cfg = AgentConfig(name="a", role="b", allowed_modalities=["image", "document"])
    assert cfg.allowed_modalities == ["image", "document"]
    assert Agent.from_config("a", cfg).allowed_modalities == ["image", "document"]
    with pytest.raises(ValidationError):
        AgentConfig(name="a", role="b", allowed_modalities=["hologram"])


def test_the_setting_is_written_in_a_fixed_order_and_removed_when_empty(tmp_path: Path):
    path = tmp_path / "conf.json"
    write_conf_file(path, {"agents": {"coder": {"name": "c", "role": "r"}}})

    set_agent_allowed_modalities_in_conf_file("coder", ["document", "image", "image"], path)
    assert read_conf_file(path)["agents"]["coder"]["allowed_modalities"] == ["image", "document"]

    set_agent_allowed_modalities_in_conf_file("coder", [], path)
    assert "allowed_modalities" not in read_conf_file(path)["agents"]["coder"]

    with pytest.raises(ValueError):
        set_agent_allowed_modalities_in_conf_file("coder", ["hologram"], path)


# ------------------------------------------------------------------ 언급 → 첨부


def test_files_are_filtered_by_what_this_agent_receives(workspace: Path):
    paths = ["uploads/mockup.png", "uploads/talk.mp3", "uploads/spec.pdf", "uploads/main.py"]

    assert media.attachments_for(_agent(), paths, str(workspace)) == [], "모달이 비어 있으면 아무것도 싣지 않습니다"

    blocks = media.attachments_for(_agent("image", "document"), paths, str(workspace))
    assert [(b["name"], b["kind"]) for b in blocks] == [
        ("uploads/mockup.png", "image"), ("uploads/spec.pdf", "document"),
    ]
    assert media.attachments_for(_agent("video"), paths, str(workspace)) == []


def test_a_path_outside_the_workspace_is_never_read(workspace: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.png"
    outside.write_bytes(PNG)

    for rel in (str(outside), "../secret.png", "uploads/../../secret.png"):
        blocks = media.attachments_for(_agent("image"), [rel], str(workspace))
        assert not any(media.is_attachment(b) for b in blocks), rel
        assert "읽을 수 없습니다" in blocks[0]["text"]


def test_mentions_hand_over_the_files_and_keep_the_text_a_path(workspace: Path):
    agents = [MentionAgent("architect", "System Architect", "Architecture", True)]
    text, report = expand_mentions("@uploads/mockup.png 를 봐 주세요", workspace, agents)
    assert report.attachment_paths == ["uploads/mockup.png"]
    assert "uploads/mockup.png" in text and PNG_B64 not in text


def test_oversized_and_overflowing_files_become_notes(workspace: Path):
    block = media.attachment_block(workspace / "x.mp4", "x.mp4", "video", "video/mp4", media.MAX_ATTACHMENT_BYTES + 1)
    fitted = media.fit_attachments([block], 100_000)
    assert not media.is_attachment(fitted[0]) and "x.mp4" in fitted[0]["text"]

    images = [
        media.attachment_block(workspace / f"{n}.png", f"{n}.png", "image", "image/png", 10) for n in range(4)
    ]
    fitted = media.fit_attachments(images, 8_000)   # 몫은 4,000 토큰, 이미지 하나 1,600
    assert [media.is_attachment(b) for b in fitted] == [True, True, False, False], "앞에 언급한 것부터 남습니다"


def test_attachments_go_on_the_goal_message():
    prompt = [
        {"role": "user", "content": "목표"}, {"role": "assistant", "content": "앞선 발언"},
        {"role": "user", "content": "이번 차례"},
    ]
    block = media.attachment_block(Path("a.png"), "a.png", "image", "image/png", 1)
    out = media.with_attachments(prompt, [block])

    assert out[0]["content"][0] == {"type": "text", "text": "목표"} and media.is_attachment(out[0]["content"][-1])
    assert out[1:] == prompt[1:] and prompt[0]["content"] == "목표", "다른 메시지와 원본은 그대로"
    assert media.with_attachments(prompt, []) is prompt, "첨부가 없으면 같은 목록입니다"


# ------------------------------------------------------------------ 맥락 계산


def _with_goal_attachment(*tail: Dict[str, Any]) -> List[Dict[str, Any]]:
    block = media.attachment_block(Path("a.png"), "a.png", "image", "image/png", 1)
    return [
        {"role": "system", "content": "시스템"},
        {"role": "user", "content": [{"type": "text", "text": "목표"}, block]},
        *tail,
    ]


def test_consecutive_user_messages_still_merge_around_an_attachment():
    merged = merge_consecutive_roles(_with_goal_attachment(
        {"role": "user", "content": "[다른 전문가]: 의견"}, {"role": "user", "content": "이번 차례"},
    ))
    assert [m["role"] for m in merged] == ["system", "user"], "연속 user 는 400 입니다"
    content = merged[1]["content"]
    assert media.is_attachment(content[1])
    assert [b["text"] for b in content[2:]] == ["[다른 전문가]: 의견", "이번 차례"]


def test_tokens_are_estimated_without_the_tokenizer_seeing_the_block():
    plain = [{"role": "user", "content": "목표"}]
    block = media.attachment_block(Path("a.png"), "a.png", "image", "image/png", 1)
    attached = [{"role": "user", "content": [{"type": "text", "text": "목표"}, block]}]
    extra = estimate_tokens("openai/gpt-4o", attached) - estimate_tokens("openai/gpt-4o", plain)
    # 글 블록 포장값이 조금 다를 수 있습니다. 첨부 몫이 실린 것만 봅니다.
    assert media.attachment_tokens(block) - 10 <= extra <= media.attachment_tokens(block) + 10


def test_trimming_keeps_the_attachment(workspace: Path):
    agent = _agent("image", max_context_window=3_000, max_tokens=256)
    filler = "말 " * 1_500
    messages = _with_goal_attachment(
        {"role": "assistant", "content": filler}, {"role": "user", "content": filler}, {"role": "user", "content": "이번 차례"},
    )
    trimmed, dropped = fit_context_window(agent, messages)
    assert dropped and media.has_attachments(trimmed)

    loop_messages = _with_goal_attachment(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "1", "name": "t", "content": filler},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "2", "name": "t", "content": "짧은 결과"},
    )
    trimmed, dropped = fit_tool_loop_context(agent, loop_messages, keep="이번 차례")
    assert dropped and media.has_attachments(trimmed)


def test_a_budget_notice_joins_a_message_carrying_files():
    block = media.attachment_block(Path("a.png"), "a.png", "image", "image/png", 1)
    messages = [{"role": "user", "content": [block]}]
    AnswerRecoveryMixin._append_budget_notice(messages, "남은 호출 3회")
    assert len(messages) == 1, "새 user 메시지를 만들지 않습니다"
    assert messages[0]["content"][-1] == {"type": "text", "text": "남은 호출 3회"}


# ------------------------------------------------------------------ 요청 직전의 변환


async def _wire(agent: Agent, workspace: Path, rel: str) -> Any:
    blocks = media.attachments_for(agent, [rel], str(workspace))
    messages = media.with_attachments([{"role": "user", "content": "요청"}], blocks)
    return (await media.materialize_attachments(agent, messages))[0]["content"][-1]


@pytest.mark.asyncio
async def test_each_format_becomes_the_block_the_provider_takes(workspace: Path):
    every = ("image", "audio", "video", "document")
    agent = _agent(*every)

    image = await _wire(agent, workspace, "uploads/mockup.png")
    assert image == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}

    audio = await _wire(agent, workspace, "uploads/talk.mp3")
    assert audio == {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(b"ID3-audio").decode(), "format": "mp3"},
    }

    video = await _wire(agent, workspace, "uploads/demo.mp4")
    assert video["type"] == "video_url" and video["video_url"]["url"].startswith("data:video/mp4;base64,")

    pdf = await _wire(agent, workspace, "uploads/spec.pdf")
    assert pdf["type"] == "file" and pdf["file"]["filename"] == "spec.pdf"
    assert pdf["file"]["file_data"].startswith("data:application/pdf;base64,")


@pytest.mark.asyncio
async def test_gemini_takes_audio_and_video_as_inline_files(workspace: Path):
    agent = _agent("audio", "video", model="gemini/gemini-2.5-flash")
    for rel, mime in (("uploads/talk.mp3", "audio/mpeg"), ("uploads/demo.mp4", "video/mp4")):
        block = await _wire(agent, workspace, rel)
        assert block["type"] == "file" and block["file"]["format"] == mime
        assert block["file"]["file_data"].startswith(f"data:{mime};base64,")


@pytest.mark.asyncio
async def test_text_documents_go_as_text_not_base64(workspace: Path):
    block = await _wire(_agent("document"), workspace, "uploads/notes.md")
    assert block["type"] == "text" and "결론은 A 입니다." in block["text"] and "uploads/notes.md" in block["text"]


@pytest.mark.asyncio
async def test_a_file_gone_before_the_request_becomes_a_note(workspace: Path):
    agent = _agent("image")
    blocks = media.attachments_for(agent, ["uploads/mockup.png"], str(workspace))
    (workspace / "uploads" / "mockup.png").unlink()
    wire = await media.materialize_attachments(agent, media.with_attachments([{"role": "user", "content": "요청"}], blocks))
    assert wire[0]["content"][-1]["type"] == "text" and "읽을 수 없습니다" in wire[0]["content"][-1]["text"]


@pytest.mark.asyncio
async def test_litellm_translates_the_blocks_for_anthropic_and_gemini(workspace: Path):
    every = _agent("image", "document", model="anthropic/claude-sonnet-5-5")
    image = await _wire(every, workspace, "uploads/mockup.png")
    pdf = await _wire(every, workspace, "uploads/spec.pdf")
    converted = anthropic_messages_pt(
        [{"role": "user", "content": [{"type": "text", "text": "봐 주세요"}, image, pdf]}],
        model="claude-sonnet-5-5", llm_provider="anthropic",
    )
    types = [block["type"] for block in converted[0]["content"]]
    assert "image" in types and "document" in types

    gemini = _agent("image", "audio", "video", "document", model="gemini/gemini-2.5-flash")
    blocks = [{"type": "text", "text": "봐 주세요"}]
    for rel in ("uploads/mockup.png", "uploads/talk.mp3", "uploads/demo.mp4", "uploads/spec.pdf"):
        blocks.append(await _wire(gemini, workspace, rel))
    contents = _gemini_convert_messages_with_history(
        messages=[{"role": "user", "content": blocks}], model="gemini-2.5-flash",
    )
    mimes = [p["inline_data"]["mime_type"] for p in contents[0]["parts"] if "inline_data" in p]
    assert mimes == ["image/png", "audio/mpeg", "video/mp4", "application/pdf"]


# ------------------------------------------------------------------ 한 발언 전체


@pytest.mark.asyncio
async def test_with_no_boxes_checked_the_request_is_text_only(workspace: Path):
    sent, fake = _capture([_reply(_msg("답"))])
    with patch("litellm.acompletion", side_effect=fake):
        await LLMCaller(mcp_manager=_NoTools(workspace)).call_agent(
            _agent(), [{"role": "user", "content": "요청"}],
            attachments=media.attachments_for(_agent(), ["uploads/mockup.png"], str(workspace)),
        )
    assert sent[0][1] == {"role": "user", "content": "요청"}, "지금까지와 똑같은 요청입니다"


@pytest.mark.asyncio
async def test_the_model_gets_the_content_but_the_draft_does_not(workspace: Path):
    agent = _agent("image")
    saved: List[str] = []

    async def checkpoint(state: Dict[str, Any]) -> None:
        saved.append(json.dumps(state, ensure_ascii=False, default=str))

    tool_call = SimpleNamespace(id="c1", function=SimpleNamespace(name="t__read", arguments="{}"))
    sent, fake = _capture([_reply(_msg("", [tool_call]), "tool_calls"), _reply(_msg("답"))])

    class Tools(_NoTools):
        def get_openai_tools_for_servers(self, servers):
            return [{"type": "function", "function": {"name": "t__read", "parameters": {"type": "object"}}}]

        async def execute_tool(self, *args, **kwargs):
            return "결과", "success"

    with patch("litellm.acompletion", side_effect=fake):
        text, _logs = await LLMCaller(mcp_manager=Tools(workspace)).call_agent(
            agent, [{"role": "user", "content": "요청"}], checkpoint=checkpoint,
            attachments=media.attachments_for(agent, ["uploads/mockup.png"], str(workspace)),
        )

    assert text == "답"
    for request in sent:
        assert PNG_B64 in json.dumps(request), "도구 루프의 판마다 실립니다"
    assert saved and not any(PNG_B64 in state for state in saved), "초안에는 파일을 가리키는 블록만 남습니다"
    assert any("uploads/mockup.png" in state for state in saved)


@pytest.mark.asyncio
async def test_a_resumed_speech_reads_the_file_again(workspace: Path):
    agent = _agent("image")
    blocks = media.attachments_for(agent, ["uploads/mockup.png"], str(workspace))
    messages = [{"role": "system", "content": "시스템"}, *media.with_attachments([{"role": "user", "content": "요청"}], blocks)]
    resume_state = {"version": 1, "messages": json.loads(json.dumps(messages)), "segments": [], "limit": 5, "window": 128000}

    sent, fake = _capture([_reply(_msg("이어서 답"))])
    with patch("litellm.acompletion", side_effect=fake):
        await LLMCaller(mcp_manager=_NoTools(workspace)).call_agent(agent, [], resume_state=resume_state)
    assert PNG_B64 in json.dumps(sent[0])


@pytest.mark.asyncio
async def test_a_rejected_attachment_does_not_lose_the_speech(workspace: Path):
    agent = _agent("image")
    rejected = litellm.BadRequestError("image input is not supported", model="m", llm_provider="openai")
    # 스트리밍 뒤 비스트리밍까지 거절당한 다음, 글만 보낸 판이 답합니다.
    sent, fake = _capture([rejected, _reply(_msg("글로 답합니다"))])
    chunks: List[str] = []

    with patch("litellm.acompletion", side_effect=fake):
        text, _logs = await LLMCaller(mcp_manager=_NoTools(workspace)).call_agent(
            agent, [{"role": "user", "content": "요청"}], on_chunk=chunks.append,
            attachments=media.attachments_for(agent, ["uploads/mockup.png"], str(workspace)),
        )

    assert text.startswith("> ⚠️ **첨부 전달 실패**") and "uploads/mockup.png" in text and text.endswith("글로 답합니다")
    assert "첨부 전달 실패" in "".join(chunks), "흘러가는 카드에도 보입니다"
    assert PNG_B64 in json.dumps(sent[0]) and PNG_B64 not in json.dumps(sent[1])
    assert "받지 않습니다" in json.dumps(sent[1], ensure_ascii=False), "모델에게도 받지 못했다고 적습니다"


@pytest.mark.asyncio
async def test_a_rejection_without_attachments_still_fails_as_before(workspace: Path):
    rejected = litellm.BadRequestError("bad request", model="m", llm_provider="openai")
    _sent, fake = _capture([rejected])
    from app.agents.llm_types import LLMUnavailableError

    with patch("litellm.acompletion", side_effect=fake), pytest.raises(LLMUnavailableError):
        await LLMCaller(mcp_manager=_NoTools(workspace)).call_agent(_agent(), [{"role": "user", "content": "요청"}])


class _CapturingLLM(BaseHTTPRequestHandler):
    """받은 요청 본문을 그대로 남기는 OpenAI 호환 서버."""

    bodies: List[Dict[str, Any]] = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        type(self).bodies.append(body)
        payload = json.dumps({
            "id": "x", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "봤습니다"}, "finish_reason": "stop"}],
        }).encode()
        if body.get("stream"):
            chunk = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "봤습니다"}, "finish_reason": "stop"}]}
            payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream" if body.get("stream") else "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.asyncio
async def test_real_call_the_base64_reaches_the_endpoint(workspace: Path):
    """LiteLLM 을 실제로 거쳐 HTTP 본문까지 확인합니다."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), _CapturingLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _CapturingLLM.bodies.clear()
    try:
        agent = _agent("image", "document", model="openai/local", api_base=f"http://127.0.0.1:{port}/v1")
        text, _logs = await LLMCaller(mcp_manager=_NoTools(workspace)).call_agent(
            agent, [{"role": "user", "content": "요청"}],
            attachments=media.attachments_for(agent, ["uploads/mockup.png", "uploads/spec.pdf"], str(workspace)),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert text == "봤습니다"
    content = _CapturingLLM.bodies[0]["messages"][1]["content"]
    types = [block["type"] for block in content]
    assert types[0] == "text" and "image_url" in types and "file" in types
    image = next(b for b in content if b["type"] == "image_url")
    assert image["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    assert not any(b["type"] == media.ATTACHMENT_BLOCK for b in content), "앱 내부 블록은 나가지 않습니다"


# ------------------------------------------------------------------ 도구가 돌려준 파일


def _item(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**fields)


def test_only_formats_a_model_can_take_count_as_media():
    assert media.modality_of_mime("image/png") == "image"
    assert media.modality_of_mime("IMAGE/JPG; q=1") == "image"
    assert media.modality_of_mime("audio/mp3") == "audio"
    assert media.modality_of_mime("application/pdf") == "document"
    assert media.modality_of_mime("image/svg+xml") is None, "함께 실린 첨부까지 거절당합니다"
    assert media.modality_of_mime("application/zip") is None
    assert media.modality_of_mime("") is None


def test_client_separates_text_from_media():
    res = SimpleNamespace(content=[
        _item(type="text", text="그렸습니다"),
        _item(type="image", data=PNG_B64, mimeType="image/png"),
        _item(type="audio", data="QUJD", mimeType="audio/wav"),
        _item(type="resource", resource=_item(text="리소스 글", mimeType="text/plain")),
        _item(type="resource", resource=_item(blob="QUJD", mimeType="application/pdf")),
        _item(type="resource_link", name="큰 파일", uri="file:///a.bin"),
    ])
    output = MCPClientConnection._extract_result(res)

    assert isinstance(output, str) and PNG_B64 not in output, "BASE64 는 글이 되지 않습니다"
    assert "그렸습니다" in output and "리소스 글" in output and "file:///a.bin" in output
    assert [m.mime for m in output.media] == ["image/png", "audio/wav", "application/pdf"]


def _tool_loop_caller(workspace: Path, output: str):
    class Tools(_NoTools):
        def get_openai_tools_for_servers(self, servers):
            return [{"type": "function", "function": {"name": "sandbox__run", "parameters": {"type": "object"}}}]

        async def execute_tool(self, *args, **kwargs):
            return output, "success"

    return LLMCaller(mcp_manager=Tools(workspace))


@pytest.mark.asyncio
async def test_a_tool_image_reaches_an_agent_that_takes_images(workspace: Path):
    output = ToolOutput("차트를 저장했습니다", [ToolMedia("image/png", PNG_B64)])
    tool_call = SimpleNamespace(id="c1", function=SimpleNamespace(name="sandbox__run", arguments="{}"))
    sent, fake = _capture([_reply(_msg("", [tool_call]), "tool_calls"), _reply(_msg("차트를 봤습니다"))])
    logs: List[Dict[str, Any]] = []

    with patch("litellm.acompletion", side_effect=fake):
        await _tool_loop_caller(workspace, output).call_agent(
            _agent("image"), [{"role": "user", "content": "차트를 그려 주세요"}], on_tool_call=logs.append,
        )

    second = sent[1]
    tool_at = next(i for i, m in enumerate(second) if m["role"] == "tool")
    assert isinstance(second[tool_at]["content"], str) and PNG_B64 not in second[tool_at]["content"]
    follow = second[tool_at + 1]
    assert follow["role"] == "user" and any(b["type"] == "image_url" for b in follow["content"])
    assert PNG_B64 in json.dumps(follow), "모델은 이미지를 봅니다"
    assert PNG_B64 not in logs[0]["output"], "기록과 화면에는 글만 남습니다"
    assert list((workspace / ".mado" / "tool_media").iterdir()), "이어 가기를 위해 파일로 풀어 둡니다"


@pytest.mark.asyncio
async def test_an_agent_without_the_box_is_told_it_cannot_see_the_image(workspace: Path):
    output = ToolOutput("차트를 저장했습니다", [ToolMedia("image/png", PNG_B64)])
    tool_call = SimpleNamespace(id="c1", function=SimpleNamespace(name="sandbox__run", arguments="{}"))
    sent, fake = _capture([_reply(_msg("", [tool_call]), "tool_calls"), _reply(_msg("저장했다고만 답합니다"))])

    with patch("litellm.acompletion", side_effect=fake):
        await _tool_loop_caller(workspace, output).call_agent(
            _agent(), [{"role": "user", "content": "차트를 그려 주세요"}],
        )

    dumped = json.dumps(sent[1], ensure_ascii=False)
    assert PNG_B64 not in dumped and "image_url" not in dumped
    assert "볼 수 없습니다" in dumped and "차트를 저장했습니다" in dumped
    assert not (workspace / ".mado" / "tool_media").exists()


@pytest.mark.asyncio
async def test_tool_media_keeps_the_tool_results_paired(workspace: Path):
    """파일을 도구 결과 사이에 끼우면 짝이 깨져 400 입니다."""
    output = ToolOutput("결과", [ToolMedia("image/png", PNG_B64)])
    calls = [
        SimpleNamespace(id=f"c{n}", function=SimpleNamespace(name="sandbox__run", arguments="{}")) for n in (1, 2)
    ]
    sent, fake = _capture([_reply(_msg("", calls), "tool_calls"), _reply(_msg("답"))])

    with patch("litellm.acompletion", side_effect=fake):
        await _tool_loop_caller(workspace, output).call_agent(_agent("image"), [{"role": "user", "content": "요청"}])

    roles = [m["role"] for m in sent[1]]
    at = roles.index("assistant")
    assert roles[at:] == ["assistant", "tool", "tool", "user"]
    assert sum(b["type"] == "image_url" for b in sent[1][-1]["content"]) == 2


SANDBOX_SNIPPET = """
import json, os, sys, tempfile
from pathlib import Path
import config, server
from artifacts import Artifact

folder = Path(tempfile.mkdtemp())
def art(name, data=b"x"):
    path = folder / name
    path.write_bytes(data)
    return Artifact(path=path, rel_path=name, size=len(data), is_image=True)

def shape(result):
    if isinstance(result, str):
        return "text"
    return [type(item).__name__ for item in result]

config.INLINE_IMAGE_MAX_COUNT = 2
config.INLINE_IMAGE_MAX_BYTES = 10
print(json.dumps({
    "enabled": config.INLINE_IMAGES,
    "chart": shape(server._with_images("t", [art("a.png")])),
    "vector_only": shape(server._with_images("t", [art("a.svg")])),
    "too_big": shape(server._with_images("t", [art("a.png", b"x" * 11)])),
    "capped": shape(server._with_images("t", [art("a.png"), art("b.jpg"), art("c.webp")])),
    "none": shape(server._with_images("t", [])),
}))
"""


def _sandbox_probe(flag: str) -> Dict[str, Any]:
    import os
    import subprocess
    import sys

    pytest.importorskip("jupyter_client")
    sandbox = Path(__file__).resolve().parents[1] / "mcp_sandbox"
    # 샌드박스는 이 저장소 밖의 체크아웃입니다 (`setup_mcp.py`). 그림을 돌려주는 판(v0.9.0)이 아니면 볼 것이 없습니다.
    source = sandbox / "server.py"
    if not source.is_file() or "_with_images" not in source.read_text(encoding="utf-8"):
        pytest.skip("mcp_sandbox v0.9.0 이상이 필요합니다")
    done = subprocess.run(
        [sys.executable, "-c", SANDBOX_SNIPPET], cwd=sandbox, capture_output=True, text=True, timeout=120,
        env={**os.environ, "SANDBOX_INLINE_IMAGES": flag, "PYTHONPATH": str(sandbox), "PYTHONIOENCODING": "utf-8"},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_sandbox_returns_new_images_only_when_the_host_asks():
    off = _sandbox_probe("")
    assert off["enabled"] is False and off["chart"] == "text", "결과를 글로 뭉개는 호스트에는 BASE64 를 보내지 않습니다"

    on = _sandbox_probe("1")
    assert on["chart"] == ["str", "Image"]
    assert on["vector_only"] == "text" and on["too_big"] == "text" and on["none"] == "text"
    assert on["capped"] == ["str", "Image", "Image"]


def test_the_host_asks_for_images_and_a_server_entry_can_refuse():
    from app.mcp import client as client_module

    assert client_module.HOST_SERVER_ENV == {"SANDBOX_INLINE_IMAGES": "1"}
    explicit = {**client_module.HOST_SERVER_ENV, **{"SANDBOX_INLINE_IMAGES": "0"}}
    assert client_module.server_environment({}, explicit)["SANDBOX_INLINE_IMAGES"] == "0"


# ------------------------------------------------------------------ 턴


class _RecordingCaller(FakeLLMCaller):
    def __init__(self) -> None:
        super().__init__()
        self.attached: List[tuple] = []

    async def call_agent(self, agent, messages, *args, attachments=(), **kwargs):
        self.attached.append((agent.key, [b.get("name") for b in attachments]))
        return await super().call_agent(agent, messages, *args, **kwargs)


def _with_modalities(engine, key: str, *modalities: str):
    agent = engine.ctx.agent_pool.get(key)
    agent.allowed_modalities = list(modalities)
    return engine


async def _workspace_session(workspace: Path, **kwargs) -> str:
    from app.database.models import SessionModel

    sid = await _make_session(**kwargs)
    async with get_session_factory()() as db:
        row = await db.get(SessionModel, sid)
        row.workspace_dir = str(workspace)
        await db.commit()
    return sid


@pytest.mark.asyncio
async def test_only_the_agent_with_the_box_gets_the_file_for_this_turn(workspace: Path):
    sid = await _workspace_session(workspace)
    caller = _RecordingCaller()
    engine = _with_modalities(_engine(llm_caller=caller), "architect", "image")

    state = await engine.run_turn(session_id=sid, user_prompt="목업을 봐 주세요", attachments=["uploads/mockup.png"])

    by_agent = {key: names for key, names in caller.attached}
    assert by_agent["architect"] == ["uploads/mockup.png"]
    assert by_agent["coder"] == [] and by_agent["critic"] == [] and by_agent["orchestrator"] == []
    async with get_session_factory()() as db:
        turn = await db.get(TurnModel, state.turn_id)
    assert turn.config["attachments"] == ["uploads/mockup.png"], "끊겨도 이어 가도록"

    caller.attached.clear()
    await engine.run_turn(session_id=sid, user_prompt="이어서 설명해 주세요")
    assert all(names == [] for _key, names in caller.attached), "다음 턴에는 실리지 않습니다"

    # 대화가 시작되면 설정은 스냅샷으로 고정됩니다. 카드를 바꿔도 이 대화는 그대로이고, 다시 언급하면 다시 실립니다.
    engine.ctx.agent_pool.get("architect").allowed_modalities = []
    caller.attached.clear()
    await engine.run_turn(session_id=sid, user_prompt="다시 봐 주세요", attachments=["uploads/mockup.png"])
    assert dict(caller.attached)["architect"] == ["uploads/mockup.png"]


@pytest.mark.asyncio
async def test_an_interjection_attaches_a_file_for_the_remaining_speeches(workspace: Path):
    sid = await _workspace_session(workspace)
    control = TurnControl()

    class Caller(HookedCaller):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.attached: List[tuple] = []

        async def call_agent(self, agent, messages, *args, attachments=(), **kwargs):
            self.attached.append((agent.key, [b.get("name") for b in attachments]))
            return await super().call_agent(agent, messages, *args, **kwargs)

    caller = Caller(
        after="architect",
        hook=lambda: control.add_note("이 그림도 봐 주세요", None, ["uploads/mockup.png"]),
    )
    engine = _with_modalities(_with_modalities(_engine(llm_caller=caller), "architect", "image"), "coder", "image")

    state = await engine.run_turn(session_id=sid, user_prompt="검토해 주세요", control=control)

    by_agent = {key: names for key, names in caller.attached}
    assert by_agent["architect"] == [], "이미 끝난 발언에는 없습니다"
    assert by_agent["coder"] == ["uploads/mockup.png"]
    async with get_session_factory()() as db:
        turn = await db.get(TurnModel, state.turn_id)
    assert turn.config["attachments"] == ["uploads/mockup.png"]


def test_control_collects_attachments_with_notes():
    control = TurnControl()
    assert control.add_note("하나", None, ["a.png"])
    assert control.add_note("둘", None, ["a.png", "b.png"])
    assert not control.add_note("  ", None, ["c.png"]), "빈 메모는 첨부도 싣지 않습니다"
    assert control.drain_attachments() == ["a.png", "b.png"]
    assert control.drain_attachments() == []


# ------------------------------------------------------------------ 보낼 때 안내


def test_the_sender_is_told_who_gets_what():
    agents = [SimpleNamespace(name="Vision", allowed_modalities=["image"]), SimpleNamespace(name="Coder", allowed_modalities=[])]
    notices, warnings = media.attachment_notices(
        [("a.png", 10), ("b.pdf", 10), ("huge.png", media.MAX_ATTACHMENT_BYTES + 1)], agents,
    )
    assert notices == ["a.png → Vision 에게 내용을 직접 전달합니다."], "받는 에이전트가 없는 파일은 알리지 않습니다"
    assert len(warnings) == 1 and "huge.png" in warnings[0]
