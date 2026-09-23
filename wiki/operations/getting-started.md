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
2. Installs official Node MCP servers into `./mcp_node` (`server-filesystem`, `server-memory`, `server-sequential-thinking`).
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

> **Offline Simulator Available**: If you do not configure any API keys or local servers, the built-in offline simulator automatically takes over, allowing you to explore the full UI, multi-agent debate, and artifact synthesis immediately!

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
