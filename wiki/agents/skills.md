# Skills — Instructions an Agent Loads When the Work Calls for Them

A **skill** is a folder of instructions an agent reads *when its task matches*, instead of carrying them in
its system prompt all the time. A Mermaid style guide, a report format, a review checklist — anything an
organisation wants done its own way.

```text
skills/
  mermaid-diagrams/
    SKILL.md        ← front matter (name, description) + the instructions
    reference.md    ← a supporting file, read only when SKILL.md points to it
  csv-profile/
    SKILL.md
    scripts/profile_csv.py   ← a script, copied into the workspace and run in the sandbox
```

The code is [app/agents/skills.py](file:///d:/MultiAgentDebateOrchestration/app/agents/skills.py). The design
decision is [ADR-026](../../lectures/05-adr/ADR-026-skills-as-host-tools.md).

---

## 1. Progressive disclosure

An agent sees three layers, each only when the previous one said it is needed:

| Layer | What the model sees | When |
| :--- | :--- | :--- |
| Catalog | `- name: description` for each of its skills, inside the description of the `skills__load_skill` tool | every request |
| Body | the SKILL.md text without its front matter, plus the list of supporting files | after it calls `skills__load_skill` |
| Supporting files | one file's text | after it calls `skills__read_skill_file` |

So ten installed skills cost ten description lines per request, not ten documents. The description is
the only thing the model has when deciding to load a skill — it should say *when* to use it.

A short standing instruction is added to the system prompt of agents that hold the tool
([`skill_guidance()`](file:///d:/MultiAgentDebateOrchestration/app/agents/skills.py), placed before
`[Session Custom Instructions]` like the file-writing rules in
[LLM Integration §2.5](llm-integration.md)): *if a listed skill fits the task, load it before answering and
follow it*. Without it, models tend to treat the catalog as optional reading and answer their own way.
Agents without skills get no extra text.

A loaded body lives in that speech's tool loop only. The next speaker's context carries speech bodies, not
tool results, so a skill needed again in a later turn is loaded again (cheap: one tool call).

---

## 2. Host tools, not an MCP server

The two tools look like MCP tools to the model (`server__tool` naming, same loop, same tool-call card, same
record in `tool_calls`), but the host runs them in-process:

- **The catalog differs per agent** (`allowed_skills`) and **changes while the app runs** (folders added,
  skills turned off). An MCP server's tool list is fetched once when it starts and is the same for every
  caller.
- **The skills folder sits inside the MADO install folder**, which no agent tool may touch
  ([Tool Security §6](../mcp/tool-security.md)). The host reads there on the agent's behalf, and only inside
  that one skill's folder.

Because of the naming, `skills` is a reserved name: `mcp_servers.skills` fails validation and the roster's
**서버 추가** refuses it.

**Not judged by the tool gate.** Skill calls skip allow/ask/deny: they read the operator's own instruction
files and touch neither the workspace nor the network, and who may use which skill is already decided by
`allowed_skills` and the on/off switch. The call record therefore has no verdict line.

---

## 3. Live, not frozen

A started conversation normally freezes the whole agent configuration
([Session Personas §6](session-personas.md#6-a-started-conversation-is-self-contained)). Skills are the
second exception after MCP server on/off:

| Change | Started conversation | Not-yet-started conversation |
| :--- | :--- | :--- |
| Edit a SKILL.md or a supporting file | **next speech** sees the new text | same |
| Add or remove a skill folder | **next speech** | same |
| Turn a skill on/off (`skills.disabled`) | **next speech**; a load already in flight is refused at call time | same |
| Change an agent's `allowed_skills` | unaffected — frozen with the agent, like `allowed_mcp_servers` | applies immediately |

The catalog is rebuilt for every speech by scanning the folder
([`scan_skills()`](file:///d:/MultiAgentDebateOrchestration/app/agents/skills.py)): a directory listing plus
file stats, with SKILL.md re-parsed only when its mtime or size changed. `skills__load_skill` checks again
at call time, so a skill turned off after the request went out is answered with "not available now" and the
list of skills that are.

---

## 4. Writing a skill

`SKILL.md` starts with front matter between `---` lines:

```markdown
---
name: report-writing
description: 의사결정 보고서를 작성할 때 사용합니다. 결론을 서두에 배치하고, 추진 근거와 잔여 쟁점을 후반부에 기술합니다.
---

# 의사결정 보고서 작성 지침

1. 첫 단락에는 핵심 결론과 기대 효과를 요약하십시오. ...
```

| Rule | Why |
| :--- | :--- |
| The **folder name** is the skill's name (letters, digits, `_`, `-`; up to 64; starts with a letter or digit). `name:` in the front matter is shown but not used as the key. | It is written into `allowed_skills` and tool arguments. |
| `description` is required (whitespace is collapsed; cut at 1024 characters). | It is all the model sees before loading. |
| The body must not be empty; SKILL.md must be UTF-8 and at most 256 KB. | The whole body goes to the model on load. Put long material in supporting files. |
| Supporting files: any file in the folder except hidden ones, `__pycache__`, `node_modules`, venvs and symlinks. Up to 200 are listed. | `read_skill_file` reads text files up to 256 KB. |

The front-matter reader is a small subset of YAML written for this — `key: value`, quoted values, `|`/`>`
blocks, indented continuation lines — so no YAML library is added to the air-gapped bundle. Keys other
than `name` and `description` are read and ignored.

A skill that breaks a rule stays in the list with its reason (roster chip in red, `/api/skills` `problem`)
and is given to no agent.

---

## 5. Scripts

A skill may carry Python scripts — any `*.py` among its supporting files counts
([`Skill.scripts`](file:///d:/MultiAgentDebateOrchestration/app/agents/skills.py)). The skills folder is inside
the install folder, out of reach of every agent tool, so a script cannot be run where it lies. Instead:

1. An agent that holds a run tool — found by name tail `run_python_file`, like the file-writing rules — loads
   the skill.
2. The host copies the skill folder into **that speech's workspace** at `.mado/skills/<name>/`
   ([`stage_skill()`](file:///d:/MultiAgentDebateOrchestration/app/agents/skills.py)) and appends to the loaded
   body the workspace paths of the scripts and the call to make: `sandbox__run_python_file` with
   `file_path=".mado/skills/<name>/scripts/…"`.
3. The agent runs it. That call is an ordinary sandbox call: the tool gate profiles it, scans the code, and
   allows, asks or denies by mode ([Tool Security §4](../mcp/tool-security.md)). In the `default` mode a clean
   script inside the workspace runs without asking; `review` asks; `read_only` refuses.

| Rule | Why |
| :--- | :--- |
| No run tool, no copy. The body says the agent cannot run the scripts and should hand the run to an agent that can. | Copying exists to run; files nobody can run only clutter the workspace. |
| Only changed files are copied again (size and mtime differ). A copy an agent edited is restored on the next load. Files not in the source are **not** deleted. | A skill edit reaches the next load; a script's own output next to it survives. |
| One lock per target folder; the copy runs in a worker thread. | Two speeches in one workspace may load the same skill at once. |
| At most 500 files / 20 MB per skill. Over that, the body is still returned with a note that the copy failed. | A skill is instructions plus small tools, not a data store. |
| `.mado/` is hidden from @-mentions and the workspace download. | It is MADO's own area in the workspace (the runtime's temp files live there too). |

**Scripts run without arguments.** `run_python_file` sets `sys.argv` to the file path only and uses the
workspace as the working directory. A skill script therefore reads its inputs from workspace files and
writes its outputs there, and SKILL.md says which. The bundled `csv-profile` reads an optional
`csv-profile.targets.txt` and writes `csv-profile.md`.

**Mind the literal strings.** The code scan turns path-looking string literals into read actions before the
script runs. A script that merely *names* `.memory-graphs` (say, in a list of folders to skip) is read as
"reads the conversation knowledge graphs" and is hard-refused. Skip hidden folders by the leading dot
instead of by name. `csv-profile` has a test that stages it and runs the real gate over it.

---

## 6. Configuration

```json
"skills": {
  "dir": "${SKILLS_DIR:-skills}",
  "disabled": []
},
"agents": {
  "architect": {
    "allowed_skills": ["mermaid-diagrams"]
  }
}
```

- `skills.dir` — relative paths resolve against the project root. The source update package replaces the
  default `skills/` folder wholesale (like `trial_templates/`), so point this elsewhere to keep your own.
- `skills.disabled` — skills listed here are off. Anything not listed is on: dropping a folder in is
  installing it.
- `agents.<key>.allowed_skills` — skill names this agent may load. Empty means no skill tools at all.

See [conf.json Reference §2.7](../configuration/conf-json-reference.md).

Agents that must not use skills lose them where they lose tools: the engine's tool-less calls (speaker
nomination, task dispatch, ledger, summaries, Mermaid repair) and trial-server participants.

---

## 7. UI

| Where | What |
| :--- | :--- |
| Roster panel, **스킬** section | one chip per skill with an on/off switch; badge `N/M 켜짐`; red chip with the reason for a broken skill; a *스크립트* badge on skills that carry scripts; tooltip with the description and folder. Not locked during a debate — nothing restarts. Redrawn when the folder changes (checked every 5 s). |
| Agent card, **스킬 N** button | pick the agent's `allowed_skills`. Same lock and same meaning as **도구 N**. Skills that vanished from the folder stay checked with a *폴더에 없음* badge until unchecked. |
| **에이전트 추가** dialog | a *사용할 스킬* row next to the MCP servers. |

The orchestrator's roster line shows each agent's currently usable skills (`· 스킬: mermaid-diagrams`), so it
can hand "draw the diagram" to the agent that has the diagram skill.

---

## 8. HTTP API

`GET /api/skills` returns the folder and every skill with `name`, `title`, `description`, `enabled`,
`usable`, `problem`, `files` and `scripts`. `GET /api/agents` includes each agent's `allowed_skills`.

---

## Related pages

- [Agent Pool & Roles](agent-pool-and-roles.md) · [Roster Editing](roster-editing.md)
- [Session Personas §6](session-personas.md) — what a started conversation freezes
- [Tool Security](../mcp/tool-security.md) — why skill reads skip the gate, and how a skill script's run is judged
