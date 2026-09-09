# Agent Pool & Built-In Roles

The MADO: Multi-Agent Debate & Orchestration Platform coordinates multiple specialized autonomous agents that debate, collaborate, and synthesize solutions. Agent definitions, runtime metadata, and UI appearance are managed by [app/agents/base.py](file:///d:/MultiAgentOrchestrator/app/agents/base.py) and [app/agents/pool.py](file:///d:/MultiAgentOrchestrator/app/agents/pool.py).

---

## 1. Agent Architecture & UI Styling

Each agent is represented by the [`Agent`](file:///d:/MultiAgentOrchestrator/app/agents/base.py) Pydantic
model — `AgentConfig` plus the three values the screen actually draws with. A single function,
[`style_for_agent(key, card_color, icon)`](file:///d:/MultiAgentOrchestrator/app/agents/base.py),
produces them in two steps.

**Step 1 — the key decides a default.** The five built-in keys have a fixed table:

```python
AGENT_STYLE_MAP = {
    "orchestrator": {"avatar": "forum",        "color": "indigo-8",      "badge_color": "#3f51b5"},
    "architect":    {"avatar": "account_tree", "color": "teal-8",        "badge_color": "#009688"},
    "coder":        {"avatar": "code",         "color": "deep-purple-8", "badge_color": "#673ab7"},
    "critic":       {"avatar": "security",     "color": "amber-9",       "badge_color": "#ff8f00"},
    "user":         {"avatar": "chat_bubble",  "color": "blue-grey-8",   "badge_color": "#607d8b"},
}
```

Agents created from the UI are not in that table, so a colour is picked from
`CUSTOM_STYLE_PALETTE` using `crc32(key)` — Python's string `hash()` is randomised per process and
would give the same agent a different colour on every restart. Falling back to one grey robot for
everyone made speakers indistinguishable in the debate feed. This step runs whether or not anyone
has chosen anything, so **appearance is never a required field**.

**Step 2 — `conf.json` overrides it.** Two optional keys per agent:

| Key | Value | Drives |
| :--- | :--- | :--- |
| `card_color` | `"#rrggbb"`, or a Quasar palette name (`"teal-8"`) | avatar background, role badge, and the **card border** in the roster and the debate feed |
| `icon` | a Material icon name (`"query_stats"`), or a path to an image | the avatar |

Both are chosen from the UI — the **Add Agent** dialog and the **persona editor** share one editor
([`ui/components/agent_appearance.py`](file:///d:/MultiAgentOrchestrator/app/ui/components/agent_appearance.py))
— and what is chosen is written back to `conf.json`. Clearing a value removes the key and returns
that agent to step 1. `badge_color` is the chosen colour resolved to something CSS can use directly,
which the border needs: Quasar colour names are classes, not values.

Borders are painted **only for agents whose colour was chosen explicitly**. Without that rule the
roster border would stop meaning "enabled / disabled", and every existing installation would change
appearance on upgrade. The red border on a failed turn is never overwritten — there the colour *is*
the message.

### 1.1. Icon images and the fallback

An uploaded image is copied into `data/agent_icons/` under the project root (created on demand) and
`conf.json` stores only the relative path, so moving the folder — or an air-gapped transfer — keeps
the configuration valid. The filename is `<key>-<content-sha1[:10]>.<ext>`: naming the file ourselves
removes path traversal and name collisions in one move, and the same image uploaded twice is one file.
Accepted: png · jpg · gif · webp · svg · bmp · ico, up to 2 MB.

`avatar` becomes `"img:/agent-icon?src=..."` **only if the file resolves to a real image inside the
project folder.** Every other case — the file was deleted, the path is wrong, the config travelled
without its images, the path points outside the project — falls back to the icon from step 1, with a
warning in the log. The fallback lives in one place (`_avatar_value`) because a blank or broken avatar
is much worse than a stale configuration.

The [`/agent-icon`](../ui/nicegui-fastapi.md) route repeats the same checks server-side and answers a
miss with a default robot SVG at **200, not 404** — by the time the browser asks, the page is already
drawn.

---

## 1.1. Debate Placement Fields

Two fields decide where an agent stands in a round. They live on the agent rather than in strategy
code, because strategies used to match on agent keys and every UI-created agent fell through those
tests.

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `debate_priority` | `100` | Speaking order within a round; lower speaks first. Equal values keep `conf.json` order, so an unconfigured file speaks in file order. |
| `debate_stance` | `"neutral"` | `proponent` / `critic` / `neutral`; read only by the adversarial strategy. |

Both are edited from the roster (drag to reorder, ⋮ menu for stance — see
[roster-editing.md](roster-editing.md)) and both are captured in the session snapshot.

---

## 2. Built-In Agent Roles

```mermaid
graph LR
    User([User Request]) --> Orch[1. Master Orchestrator]
    Orch --> Arch[2. System Architect]
    Arch --> Coder[3. Senior Coder]
    Coder --> Critic[4. Quality Critic]
    Critic -.->|Feedback & Counter-test| Coder
    Critic -.->|Refinement| Arch
    Critic --> Orch
    Orch --> Artifacts([Synthesized Artifacts])
```

### 2.1. Master Orchestrator (`orchestrator`)
- **Key**: `orchestrator` *(Required)*
- **Role**: Moderator, Turn Scheduler & Artifact Synthesizer
- **Allowed MCP Tools**: `filesystem`, `memory`
- **Default Temperature**: `0.2` (Low variance, high precision)
- **Responsibilities**:
  - Analyzes the user's initial objective in Phase 1 and decomposes it into clear discussion topics and guidelines.
  - Controls speaker turns during the debate.
  - Synthesizes the final consensus in Phase 3 into structured artifacts: Markdown comprehensive report, Mermaid diagrams, clean code files, and JSON summaries.

### 2.2. System Architect (`architect`)
- **Key**: `architect`
- **Role**: High-Level Architecture, Tech Stack & Design Patterns
- **Allowed MCP Tools**: `filesystem`, `memory`, `fetch`
- **Default Temperature**: `0.5`
- **Responsibilities**:
  - Formulates system structure, module boundaries, database schemas, and protocols.
  - Drafts architecture flowcharts and sequence diagrams in valid Mermaid syntax.
  - Evaluates third-party libraries and tech stack trade-offs.

### 2.3. Senior Python Engineer (`coder`)
- **Key**: `coder`
- **Role**: Implementation, Refinement & Project Skeleton
- **Allowed MCP Tools**: `filesystem`, `sandbox`, `git`
- **Default Temperature**: `0.1` (Deterministic, bug-free code)
- **Responsibilities**:
  - Implements production-grade, typed Python code adhering to clean architecture.
  - Creates workspace directories and files via the `filesystem` tool.
  - Executes code in the `sandbox` to verify syntax, execution behavior, and test outcomes before submitting.
  - Commits functional checkpoints to the local repository via the `git` tool.

### 2.4. Security & Quality Critic (`critic`)
- **Key**: `critic`
- **Role**: Code Review, Edge Case Analysis & Security Audit
- **Allowed MCP Tools**: `filesystem`, `sandbox`, `git`, `memory`
- **Default Temperature**: `0.3`
- **Responsibilities**:
  - Identifies vulnerabilities (OWASP Top 10, path traversal, injection, unhandled race conditions).
  - Actively writes stress-test and edge-case code, running it in the `sandbox` to substantiate critiques with empirical evidence.
  - Challenges optimistic assumptions made by the Architect or Coder.

---

## 3. The `AgentPool` Registry ([app/agents/pool.py](file:///d:/MultiAgentOrchestrator/app/agents/pool.py))

The [`AgentPool`](file:///d:/MultiAgentOrchestrator/app/agents/pool.py#L9-L56) acts as the runtime registry for all configured agents:

```python
class AgentPool:
    def get(self, key: str) -> Optional[Agent]: ...
    def get_orchestrator(self) -> Agent: ...
    def list_all(self) -> List[Agent]: ...
    def get_active(self, keys: List[str]) -> List[Agent]: ...
```

### Invariants:
1. **Guaranteed Orchestrator**: Calling `pool.get_orchestrator()` raises `RuntimeError` if the orchestrator is missing from `conf.json`.
2. **Orchestrator Prioritization**: `get_active(keys)` guarantees that the Master Orchestrator is always present and placed at the head of the active roster (`keys = ["orchestrator"] + ...`).

---

## 4. Extending with Custom Specialist Agents

To add a new specialist (e.g. a Data Scientist or DevOps Engineer), add an entry to the `agents`
object in [conf.json](file:///d:/MultiAgentOrchestrator/conf.json):

```json
"data_scientist": {
  "name": "Lead Data Scientist",
  "role": "Data Pipeline & ML Architecture",
  "model": "openai/gpt-4o",
  "temperature": 0.2,
  "allowed_mcp_servers": ["filesystem", "sandbox"],
  "system_prompt": [
    "You specialize in data engineering, ETL pipelines, and machine learning models.",
    "Ensure efficient DataFrame operations and validate data validation schemas."
  ],
  "sequential_thinking": {
    "enabled": true,
    "max_steps": 6
  }
}
```

Upon restart (or `pool.reload()`), the new agent is automatically registered, displayed in the web UI roster, and capable of participating in multi-agent debates.
