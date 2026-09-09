# Session Personas & Immutability Lifecycle

In the MADO: Multi-Agent Debate & Orchestration Platform, `conf.json` defines system-wide default agent configurations. However, different collaboration scenarios (e.g. cloud migration vs. embedded systems) require tailored system prompts and agent titles.

The session persona management system, implemented in [app/agents/personas.py](file:///d:/MultiAgentOrchestrator/app/agents/personas.py), provides session-specific persona overrides while guaranteeing **persona immutability** once a debate begins.

---

## 1. Why Persona Immutability is Essential

If an agent's persona, role, or instructions change mid-conversation:
- Later arguments directly contradict earlier statements made by the same speaker.
- The Master Orchestrator cannot reliably trace consensus or accountability.
- Re-opening old sessions fails if the global `conf.json` has since been updated.

Therefore, the platform enforces a strict **three-phase lifecycle**:

```mermaid
stateDiagram-v2
    [*] --> UnlockedDraft: New Session Created
    
    state UnlockedDraft {
        [*] --> Editable
        Editable --> SavedToDB: Save Persona (/personas/{id})
        SavedToDB --> Editable: Reset to Default
    }
    
    UnlockedDraft --> FrozenLocked: First User Prompt Submitted\n(prepare_agents_for_turn)
    
    state FrozenLocked {
        [*] --> ReadOnlySnapshot
        ReadOnlySnapshot --> ReadOnlySnapshot: Subsequent Debate Rounds
        ReadOnlySnapshot --> Rejected: Edit Attempt -> PersonasLockedError
    }
    
    FrozenLocked --> [*]: Session Resumed (Immutable History)
```

---

## 2. The 3-Phase Lifecycle

### Phase 1: Unlocked Draft (Session Creation)
- When a new session is created, `session.personas_locked` is `False`.
- The user can open the persona editor at `/personas/{session_id}` (accessed via the **"Persona Settings"** button in the agent roster panel).
- **Editable Fields**: `name`, `role`, `system_prompt`, and the card's appearance — `card_color` and `icon`. The appearance editor is the same one the **Add Agent** dialog uses, so a colour is picked from twelve swatches or a colour picker, and an icon is either a Material icon name or an uploaded image (see [Agent Pool §1](agent-pool-and-roles.md)). The preview avatar and the card border follow the choice immediately.
- Operational settings (`model`, `api_base`, `allowed_mcp_servers`, credentials) are not editable here — they are governed by `conf.json` while the session is open, and frozen into the session at the first message (see §6).
- Draft changes are saved to the [`session_agents`](file:///d:/MultiAgentOrchestrator/app/database/models.py#L94-L115) table in SQLite.
- Agents that have not been edited continue to reflect their `conf.json` defaults.

### Phase 2: Freeze & Lock (First User Message)
- When the user sends their first message, [prepare_agents_for_turn()](file:///d:/MultiAgentOrchestrator/app/agents/personas.py#L217-L229) triggers [`freeze_personas()`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py#L163-L195).
- The system resolves the effective persona for **every** agent in the pool. For any agent lacking an explicit draft in `session_agents`, a snapshot of its current `conf.json` configuration is written to the database.
- `session.personas_locked` is set to `True`.
- Any subsequent attempt to call `save_persona()` or `reset_persona()` raises [`PersonasLockedError`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py#L36-L44).
- The UI transitions the editor into a locked read-only state.

### Phase 3: Session Resumption (Historical Fidelity)
- When a user resumes an existing session days or weeks later, [`effective_personas()`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py#L95-L110) loads the frozen snapshot from SQLite.
- Even if `conf.json` has been modified or updated in the interim, the session continues executing with the exact personas that created the historical debate transcript.

---

## 6. A Started Conversation Is Self-Contained

Freezing the persona alone was not enough. The three editable fields were snapshot, but the agent
*object* was still fetched from the live pool every turn:

```python
return apply_personas(pool.get_active(active_keys), personas)   # old
```

`AgentPool.get_active()` silently skips keys it does not have. So deleting an agent from `conf.json`
made it disappear from conversations that had already used it — the stored persona row could not
bring it back, because the snapshot held no model, endpoint, or credentials to rebuild it with.

### 6.1. What is frozen

`session_agents.config_snapshot` (JSON, nullable) now holds the **entire `AgentConfig`** at lock
time: model, endpoint, API key, sampling values, tool permissions, sequential-thinking settings, card
colour and icon, and the persona merged in. From that moment the conversation does not consult
`conf.json` at all. The two appearance values are additionally written to the `card_color` and
`icon_path` columns, so a card can be drawn without unpacking the snapshot.

| Change to `conf.json` | Started conversation | Not-yet-started conversation |
| :--- | :--- | :--- |
| Add an agent | unaffected — shown unchecked, joins only if the user checks it | enabled by default |
| Delete or disable an agent | **unaffected** — it keeps speaking with its frozen configuration | drops out of the pool |
| Change model / endpoint / key | unaffected | applies immediately |
| Change card colour / icon | unaffected — past turns keep the colours they were recorded with | applies immediately |
| Change `allowed_mcp_servers` | unaffected | applies immediately |
| Enable/disable an MCP **server** | **affected** | affected |

The last row is the one exception. The snapshot records *which servers an agent may call*, but
whether that server process is running is a property of the whole application
([MCPManager](file:///d:/MultiAgentOrchestrator/app/mcp/manager.py)).

The roster and the persona editor read the same frozen set through
[`session_roster_agents()`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py); an agent that
survives only inside one conversation is marked with a **이 대화 전용** badge. If the screen read the
live pool instead, a deleted agent would speak with no card to explain it.

### 6.2. Ordering

`session_agents` rows are written in a single commit, so their `created_at` values tie and sorting by
`(created_at, id)` falls through to a random UUID — card order changed on every read.
[`frozen_agents()`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py) sorts deterministically
instead: orchestrator first, then `conf.json` order, then conversation-only agents.

### 6.3. Re-syncing (the escape hatch)

Making the snapshot authoritative has a cost: rotate an API key or move the gateway, and old
conversations keep hammering a dead endpoint. The **설정 갱신** button, shown on locked sessions,
calls [`resync_agent_configs()`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py), which
rewrites the snapshots from the current `conf.json` **without touching the personas** — the speakers
in the transcript stay who they were. Agents no longer present in `conf.json` are left alone.

### 6.4. Credentials in the database

The snapshot contains API keys, and `multiagent.db` is plaintext SQLite — check file permissions when
deploying. `GET /api/sessions/{session_id}/personas` returns only `AgentPersona` (name, role, system
prompt) and never exposes the snapshot; a test asserts it.

`config_snapshot` is `NULL` for conversations locked before the column existed. Those keep following
the live `conf.json`, exactly as they always did, and
[`_add_missing_columns()`](file:///d:/MultiAgentOrchestrator/app/database/session.py) adds the column
to existing databases at startup.

---

## 3. Difference Detection (`is_customized`)

The UI displays an orange **"Customized"** badge for any agent whose persona differs from the server default.

### Detection Mechanism:
Because `freeze_personas()` snapshots all agents into `session_agents` upon the first message, the mere existence of a database row cannot determine if a user deliberately customized the agent.

Instead, [`_differs()`](file:///d:/MultiAgentOrchestrator/app/agents/personas.py#L59-L62) performs field-by-field value comparison:

```python
EDITABLE_FIELDS = ("name", "role", "system_prompt", "card_color", "icon")

def _differs(a: AgentPersona, b: AgentPersona) -> bool:
    return any(getattr(a, f) != getattr(b, f) for f in EDITABLE_FIELDS)
```

If any of those differs from the `conf.json` baseline, `is_customized` is set to `True`. Appearance
counts: changing only the colour is still a change the badge has to report, or the screen would show
a difference it refuses to acknowledge.

---

## 4. Persona REST API

Clients can query session persona statuses programmatically:

### `GET /api/sessions/{session_id}/personas`

**Response Example**:
```json
{
  "session_id": "7b8e5c84-18be-4cb9-9943-85b42d768134",
  "personas_locked": true,
  "agents": [
    {
      "agent_key": "architect",
      "name": "Cloud Native Architect",
      "role": "Microservices & Kubernetes Specialist",
      "system_prompt": "Focus exclusively on distributed systems and container orchestration.",
      "is_customized": true
    },
    {
      "agent_key": "coder",
      "name": "Senior Python Engineer",
      "role": "Implementation & Code Refinement",
      "system_prompt": "Standard clean code implementation...",
      "is_customized": false
    }
  ]
}
```

---

## 5. Global Persona Configuration Sync (`conf.json` Persistence)

Starting from the latest enhancement, editing an agent's persona on the Web UI (`/personas/{session_id}`) not only updates the draft persona in SQLite for the session, but also persists the updated baseline directly into `conf.json`:

### Workflow
1. **Targeted JSON Modification**:
   [`update_agent_persona_in_conf_file()`](file:///d:/MultiAgentOrchestrator/app/config.py) reads the raw `conf.json` — `//` documentation keys and unresolved `${VAR}` placeholders included — locates `agents.<agent_key>`, updates `name`, `role`, and `system_prompt`, and rewrites the file atomically. A multi-line prompt is written as an array of lines so it stays readable. Everything else in the file, including the notes and every other agent, is carried through untouched.
2. **In-Memory Pool Reloading**:
   After writing to disk, `get_config(reload=True)` re-reads the configuration, and `get_agent_pool().reload()` refreshes all in-memory `Agent` instances.
3. **Reactive UI Synchronization**:
   The main debate page's [`AgentRosterControl`](file:///d:/MultiAgentOrchestrator/app/ui/components/roster.py) rebuilds its roster cards via `refresh_agent_cards()`, so modified agent names and roles appear immediately without a full browser refresh.

