# Air-Gapped & Offline Packaging

In enterprise, defense, and high-security environments, systems often operate in **air-gapped networks** completely isolated from the public internet. The MADO: Multi-Agent Debate & Orchestration Platform includes an automated packaging pipeline in [package_offline.py](file:///d:/MultiAgentDebateOrchestration/package_offline.py) that produces fully self-contained, zero-dependency deployment bundles.

---

## 1. Bundle Anatomy

Running `python package_offline.py` on an internet-connected build machine generates `dist/MultiAgentDebateOrchestration_bundle/` (and a `.zip` archive):

```text
MultiAgentDebateOrchestration_bundle/
├── app/                       # Application source code
├── conf.json                  # Configuration file (copied from conf.example.json)
├── wheels/                    # Offline pip wheel archive
├── python_runtime/            # Portable CPython distribution
├── node_runtime/              # Standalone node.exe binary (no npm needed)
├── mcp_node/                  # Pre-installed Node MCP servers
├── mcp_sandbox/               # AirgappedPySandbox Python code runner
├── workspace/                 # Initialized workspace directory & git repository
├── install_wheels_offline.bat # Re-installation verification utility
├── open_browser.py            # Waits for the port to answer, then opens the default browser
└── run_mado.bat | ps1      # One-click launcher with auto-injected environment variables
```

---

## 2. Key Packaging Mechanisms

### 2.1. Pre-Installation into Portable Runtime
A common failure mode of offline bundles is collecting wheels without verifying that they can be successfully installed and imported in the target runtime.

`package_offline.py` executes:
1. Downloads wheels into `wheels/` using `pip download`.
2. **Installs the wheels directly into `python_runtime/`**.
3. Runs smoke-test imports on all critical packages:
   ```python
   # Verified imports inside the bundle runtime:
   import fastapi, uvicorn, nicegui, litellm, mcp, sqlalchemy, aiosqlite, jupyter_client
   ```
   If any import fails, the packaging script halts immediately, preventing the distribution of a broken bundle.

### 2.2. Version Constraints & MCP 2.x Protection
The vendored `AirgappedPySandbox` server's dependency list specifies `mcp>=1.2.0` without an upper bound. In an unconstrained build, `pip` downloads `mcp 2.x`.

> [!CAUTION]
> **MCP 2.x Breaking Change**: MCP 2.0 removed `mcp.server.fastmcp` (renaming it to `MCPServer`), which breaks `AirgappedPySandbox` at startup.

To protect against this, `package_offline.py` enforces a `constraints.txt` rule:
$$\text{mcp} \ge 1.29.0, < 2.0.0$$
This guarantees that all installed MCP components maintain full API compatibility.

### 2.3. Zero-Dependency Node Runtime
The official Node MCP servers (`filesystem`, `memory`, `sequential-thinking`) are compiled into pure JavaScript (`dist/index.js`) without native C++ addons.

### 2.4. Sandbox Kernel Library Packaging
The Python code execution sandbox (`AirgappedPySandbox`) runs user and agent scripts within an IPython kernel. In an air-gapped environment without internet access, attempting to import common data science packages inside the sandbox would fail.
`package_offline.py` inspects `mcp_sandbox/requirements-kernel.txt` and downloads wheels for:
- `ipykernel`, `pandas`, `numpy`, `matplotlib`, `seaborn`, `scipy`, `sympy`
These wheels are stored in `wheels/` and installed by `install_wheels_offline.bat`.

### 2.5. Workspace Pre-initialization
The packager automatically initializes `workspace/` as an empty Git repository (`git init`), commits `.gitkeep`, and configures local `user.name` and `user.email`. This ensures the `mcp-server-git` server starts up immediately without "not a git repository" errors.

---

## 3. Launcher Automation (`run_mado.bat` / `.ps1`)

> **Source of truth** (v0.5.0): both launchers are *generated* by
> `write_launchers()` in [package_offline.py](file:///d:/MultiAgentDebateOrchestration/package_offline.py).
> A byte-identical snapshot is now committed at the repository root so a source-only checkout
> has something to run and so the shipped launcher is reviewable in diffs — but the generator
> remains authoritative. To change them, edit the generator and re-emit the copy:
>
> ```bash
> python package_offline.py --launchers-only .
> ```
>
> Editing the committed copy alone does **not** change what the bundle ships.

When deployed to an air-gapped target machine, users run `run_mado.bat` or `run_mado.ps1`. The launcher automatically computes absolute paths and injects the following environment variables before starting the server:

```bat
@echo off
set "BUNDLE_ROOT=%~dp0"
set "PYTHON_BIN=%BUNDLE_ROOT%python_runtime\python.exe"
set "NODE_BIN=%BUNDLE_ROOT%node_runtime\node.exe"
set "MCP_NODE_HOME=%BUNDLE_ROOT%mcp_node"
set "MCP_SANDBOX_HOME=%BUNDLE_ROOT%mcp_sandbox"
set "WORKSPACE_DIR=%BUNDLE_ROOT%workspace"
set "SANDBOX_KERNEL_PYTHON=%PYTHON_BIN%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

"%PYTHON_BIN%" -m app.main %*
```

### Automatic Browser Launch
Before handing the console over to the server, the launcher starts `open_browser.py` in the
background:

```bat
if exist "%~dp0open_browser.py" start "" /b "%PYTHON_BIN%" open_browser.py %*
"%PYTHON_BIN%" -m app.main %*
```

The waiter polls the TCP port and opens the default browser only once the server answers —
opening it immediately would land the user on a connection-refused page. It lives outside the
server process on purpose:

- With `debug = true`, uvicorn runs in reload mode and re-executes the lifespan on every file
  change. Opening from there would spawn a tab on each save.
- The server must keep the console so logs are visible and Ctrl+C stops it. Waiting is the
  launcher's job, not the server's.

The address comes from `conf.json`'s `app` object, overridden by the same `--host` / `--port` arguments
that were forwarded to `app.main`, so a custom port always opens the right URL. A wildcard bind
address (`0.0.0.0`) is rewritten to `127.0.0.1` — it is a bind address, not a reachable one.
Set `MADO_NO_BROWSER=1` to opt out, `MADO_BROWSER_TIMEOUT` to change the 90-second wait.

Launchers are generated artifacts, not source. To refresh them on an existing installation
without rebuilding the whole bundle:

```powershell
python package_offline.py --launchers-only "C:\path\to\MultiAgentDebateOrchestration_bundle"
```

Run it on the target after copying new sources when the launcher itself changed.

### Parameter Forwarding & Encoding
- **CLI Parameter Forwarding**: Both `run_mado.ps1` (`$args`) and `run_mado.bat` (`%*`) pass all command-line arguments directly to `app.main`. Users can run `.\run_mado.ps1 --port 9000` to override the bound port dynamically.
- **Runtime Fallback**: When `python_runtime\python.exe` is missing — a source checkout on a development PC — both launchers use the `python` on `PATH` and leave `PYTHONHOME` alone (pointing it at a missing folder stops any interpreter from starting); likewise `node` on `PATH` when `node_runtime\node.exe` is missing. A warning names the interpreter in use. Before this, the committed launchers failed on a dev PC with `Start-Process : The system cannot find the file specified`.
- **UTF-8 BOM Protection**: `run_mado.ps1` is saved with UTF-8 BOM (`utf-8-sig`) and configures `[Console]::OutputEncoding = UTF8`, preventing PowerShell parser errors on Korean Windows systems.
- **Zero Configuration Drift**: Because paths and settings are injected via environment variables, [conf.json](file:///d:/MultiAgentDebateOrchestration/conf.json) requires **zero manual adjustments** when moving between environments.


---

## 4. Source-Only Updates (`package_source.py`)

The full bundle is hundreds of megabytes because it carries a portable CPython, `node.exe`,
the pip wheel archive, and the installed MCP servers. None of that changes when you fix a
bug in `app/`. Re-transferring it means re-doing the transfer review from scratch every time.

[`package_source.py`](file:///d:/MultiAgentDebateOrchestration/package_source.py) packages **only
source and configuration** — roughly 600 KB — to be applied on top of an already-transferred
bundle.

```powershell
python package_source.py   # dist\MultiAgentDebateOrchestration_source_YYYYMMDD.zip
```

### What goes in

Only what a running installation needs in order to be updated.

| Included | Excluded |
| :--- | :--- |
| `app/`, `mcp_servers/` | `python_runtime/`, `node_runtime/`, `wheels/`, `mcp_sandbox/` |
| `mcp_node/memory-scoped.mjs` (the forked server's runnable copy) | `workspace/`, `multiagent.db`, `conf.json` |
| `conf.example.json`, `.env.example`, `requirements.txt` | `tests/`, `wiki/`, `CLAUDE.md` |
| `docs/user_manual/` and `docs/user_manual_html/` (rendered at packaging time) | the working tree's own `docs/user_manual_html/` |
| `setup_mcp.py`, `open_browser.py`, `README.md` | the packaging scripts themselves |

The include list is an **allow-list**, not a deny-list. With a deny-list, a directory added
later rides along silently; with an allow-list it is simply absent, and absence is visible.

**Required paths** (`REQUIRED_PACKAGE_PATHS`). Some files live inside `app/` and are packed with it,
but would break a feature silently if a later ignore or forbidden-name rule filtered them out. The
script checks that each one is in the package and aborts otherwise. Today that is the graph editor's
bundled [Vue Flow](https://vueflow.dev) — `app/ui/static/graph_editor/index.js`, `vue-flow.css`,
`THIRD_PARTY_NOTICES.txt` (MIT · ISC · BSD-3-Clause) and `BUILD.md` (how to rebuild it). It is one ES
module that imports nothing but `vue`, which NiceGUI already puts in the page's importmap, so the
editor loads with no network access. The folder is deliberately **not** named `vendor`: that name is
in `FORBIDDEN_NAMES` to keep runtimes out, and the files would have been dropped.

### Three things the script refuses to do

1. **Overwrite the target's `conf.json`.** The local file is not shipped at all. The
   deployed one holds that network's real endpoints; replacing it would point every agent
   at nothing. New settings are carried over by hand from `conf.example.json`.
2. **Ship an oversized file.** Anything above `--max-file-mb` (default 2 MB) aborts the run.
   A source package has no business containing a megabyte-scale file — if one appears, a
   runtime artifact leaked into the tree.
3. **Ship something that looks like a credential.** API keys, tokens, and private-key headers
   are scanned for and abort the run (`--allow-secrets` to override). `conf.json` is
   gitignored, so nothing stops someone from pasting a real key into it, and a transfer
   review is the wrong place to discover that.

### Applying on the target

Overwrite the installation with the extracted files (replace `app/` wholesale rather than file by file, so modules deleted in this update do not linger and keep getting imported). `conf.json` is not in the package, so the target's own endpoints survive:

Back up `app/` and `conf.json` first. `MANIFEST.txt` lists SHA-256 per file, for the
transfer record and for verifying the extracted tree on the far side:

```powershell
Get-Content MANIFEST.txt | Where-Object { $_ -notmatch '^#' } | ForEach-Object {
    $sha, $size, $rel = ($_ -split '\s+', 3)
    if ((Get-FileHash $rel -Algorithm SHA256).Hash -ne $sha.ToUpper()) { "differs: $rel" }
}
```

### When a source-only update is not enough

If `requirements.txt` changed, a package the runtime does not have was added, and the source
package alone will not run. Compare against the backup you took before applying:

```powershell
Compare-Object (Get-Content _backup_*
equirements.txt) (Get-Content requirements.txt)
```

Any difference means the full bundle has to be rebuilt and re-transferred.

Everything else — new modules, new UI, changed prompts — travels fine in the source package.
`SOURCE_DIRS = ["app", ...]` copies the tree wholesale, so a new file such as
`app/mermaid_lint.py` is picked up without touching the packaging script.

**v0.5.0 example.** That release added a new module, session handoff, the diagram repair loop,
and the roster preview. Its imports are `re`, `dataclasses`, `typing`, `uuid`, `shutil` — all
standard library — and `requirements.txt` was untouched. So the portable Python runtime,
`wheels/`, `node_runtime/`, `mcp_node/` and `mcp_sandbox/` all stay as they are, and the whole
update ships as a **391 KB** source package instead of a several-hundred-megabyte bundle.
