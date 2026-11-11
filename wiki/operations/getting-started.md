# Getting Started Guide

This guide walks you through setting up and running the MADO: Multi-Agent Debate & Orchestration Platform on a development machine.

---

## 1. Prerequisites

- **Python**: Version **3.11 or higher**.
- **Git**: Installed and accessible on PATH (required for workspace versioning and sandbox repository checkout).
- **Node.js**: Version **18 or higher** (recommended if using the official Node MCP servers: `filesystem`, `memory`, `sequential_thinking`).
- **Operating System**: Windows, Linux, or macOS.

---

## 2. Step-by-Step Installation

### Step 1: Clone Repository & Create Virtual Environment
```bash
git clone https://github.com/HaJaehee/MultiAgentDebateAndOrchestrationPlatform.git MultiAgentDebateOrchestration
cd MultiAgentDebateOrchestration

# Create and activate virtual environment
python -m venv .venv

# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate
```

### Step 2: Install Python Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Run One-Click MCP Setup ([setup_mcp.py](file:///d:/MultiAgentDebateOrchestration/setup_mcp.py))
The setup script prepares all bundled MCP servers in a single step (requires internet, run once):
```bash
python setup_mcp.py
```

**Actions Performed by `setup_mcp.py`**:
1. Creates `./workspace` and initializes it as a Git repository (`git init -q ./workspace`).
2. Installs official Node MCP servers into `./mcp_node` (`server-filesystem`, `server-memory`, `server-sequential-thinking`), held at `^2026.8.31` — 2.0.0 no longer installs `@modelcontextprotocol/sdk`, which the memory fork needs. The ranges go into `mcp_node/package.json` rather than onto the `npm` command line (cmd.exe strips `^`), and an existing install is kept only when its `package.json` already carries the same ranges.
3. Clones [AirgappedPySandbox](https://github.com/HaJaehee/AirgappedPySandbox) into `./mcp_sandbox`.

> **Custom Options**:
> - If Node.js is not installed: `python setup_mcp.py --skip-node` (remember to set `"enabled": false` for Node servers in `conf.json`).
> - If Python sandbox is not needed: `python setup_mcp.py --skip-sandbox`.

### Step 4: Prepare Configuration Files
Copy the template files to create local configurations:
```bash
# Copy configuration template
cp conf.example.json conf.json

# Copy environment variables template
cp .env.example .env
```

### Step 5: Configure Credentials (Optional)
Edit `.env` to provide your model credentials or local gateway endpoints:

```dotenv
# Option A: Local OpenAI-compatible server (vLLM / LM Studio / Ollama)
LLM_API_BASE=http://localhost:1234/v1
LLM_MODEL=openai/qwen2.5-coder-32b
LLM_API_KEY=sk-dummy-key

# Option B: Cloud API Keys
OPENAI_API_KEY=sk-proj-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIzaSy...
```

> **No offline simulator**: Without a reachable model, agents show as unconfigured and a debate turn reports them as unreachable. Nothing is simulated. Tests use `tests/fake_llm.py` instead.

---

## 3. Running the Application

Start the web application:
```bash
python -m app.main
```

Once initialized, open your browser to the URL displayed in the console:
```text
[INFO] multiagent: Web UI: http://127.0.0.1:8000
```

### Checking a new model first

When `conf.json` points at a model this installation has not used before, check it before the first debate:

```bash
python -m app.modelcheck
```

It sends a few short requests per connection and prints which MADO features work with that model (tool calls,
reasoning apart from the answer, truncation reported, context window) and which setting to change. The same
check is the `적합성 점검` button in the `모델 연결 상태` dialog. See
[LLM Integration §8](../agents/llm-integration.md#8-checking-a-model-before-it-joins-a-debate-adr-054).

---

## 4. Running the Test Suite

Execute the automated test suite with pytest:
```bash
pytest -v tests/
```

### Test Suite Coverage:
- `test_config.py`: Environment variable resolution, nested defaults, and Pydantic validation.
- `test_personas.py`: Persona customization, snapshot freeze on first message, and lock error assertions.
- `test_llm_settings.py`: Global `llm` inheritance, parameter dropping, and sequential thinking modes.
- `test_db.py`: Database session CRUD, message history, and artifact persistence.
- `test_mcp.py`: Stdio MCP client discovery, tool execution, and error resilience.
- `test_orchestrator.py`: Multi-turn debate state transitions and artifact synthesis.
