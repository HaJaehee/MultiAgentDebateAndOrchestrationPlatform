# Workspace Checkpoints & Reverting a Turn (v1.5)

Agents create and edit files in the workspace through tools. People need to see what one turn changed
and undo it if it went wrong — without that, `auto` tool mode is a gamble. The workspace is already a
git repository (`ensure_workspace`), so the host records snapshots there.
Code: [app/workspace_checkpoint.py](file:///d:/MultiAgentDebateOrchestration/app/workspace_checkpoint.py).

## 1. Snapshots that stay out of the agents' git history

At the start of a turn (right after the request is recorded, before planning) and when it ends — also
when it fails — the engine takes a snapshot:

1. Copy the repository's index, with its timestamp, to a temporary file and point `GIT_INDEX_FILE` at it
   (unchanged files are not re-hashed, and the real index — what an agent `git add`ed — is untouched).
   The timestamp matters: git re-reads a file only when it changed at or after the index was written, so a
   copy stamped "now" hid a same-size edit made in the second the index was written.
2. `git add -A` with the dependency and cache folders excluded (`node_modules`, `.venv`, `__pycache__`,
   `.mado`, `.memory-graphs`, …; `dist` and `build` are kept), `write-tree`, `commit-tree`.
3. Name the commit `refs/mado/turns/<turn id>/start` or `…/end`.

HEAD, branches and the index never move, so `git log` and `git status` look the same to agents. The turn
row stores `checkpoint = {start, end, changed}` and the engine sends `workspace_checkpointed` after
`turn_completed`. No git, or not a repository: nothing happens and the turn is unaffected.

### 1.1. Round boundaries (ADR-043)

The same snapshot is also taken when a round starts (`…/round-N`), when a graph step starts (`…/step-N`)
and when the debate ends (`…/debate-end`). Nothing else reads these names: the DB row is unchanged, and
revert and forks always work on the whole turn. `turn_segments` turns them into segments:

| Segment | From → to | Listed |
| :--- | :--- | :--- |
| `계획 단계` | `start` → first boundary | Only if files changed |
| `라운드 N` / `N단계` | Its boundary → the next one, `debate-end`, or `end` if the turn stopped first | Always |
| `토론 이후` | `debate-end` → `end` | Only if files changed |

A resumed turn keeps the boundaries it already passed, and a failed boundary snapshot only logs a warning.
Files a retried speech changes join the `end` snapshot, so they appear under `토론 이후`. Turns from before
this change have no boundaries and show the whole turn only.

## 2. Seeing and reverting

The turn's conclusion card gets a file chip (`📁 N`). It opens a dialog with the changed files (added /
modified / deleted) and the [code diff viewer](../ui/components.md#141-code-diff-viewer-adr-043) for the
selected file. Turns with boundaries also get a `구간` selector for the whole turn or one round; the file
stays selected if the new segment has it. `이 턴 되돌리기` always applies to the whole turn:

* deletes files the turn created (and folders left empty), restores modified and deleted ones from
  `start` (`git restore --worktree`);
* **refuses** when a file the turn touched changed again after it ended (a later turn, another
  conversation on the same folder, a person) and lists those files — reverting must not overwrite later
  work;
* is disabled while the conversation is running and after the turn was reverted once;
* records a `system` note in the conversation (`[작업 공간 되돌리기] …`) so later agents know those files
  are back to their earlier state.

Discarding a turn or deleting the conversation removes its refs (`forget`); git collects the objects later.
Older turns lose their snapshots through retention (§4).

## 3. Forks

A fork exports the `end` snapshot of the chosen turn into a new folder (`export`: clone, fetch the ref,
`read-tree -u --reset`, index back to HEAD, origin removed). See
[Session Handoff §7](session-handoff.md#7-forking-a-conversation-at-a-turn-v15).

## 4. Retention and compaction (ADR-044)

Every turn adds refs and objects, so a folder with hundreds of turns keeps growing. When a workspace
has had no holder for `MCP_RUNTIME_IDLE_TTL`, the runtime pool calls `session_ops.maintain_workspace`
([Runtime Isolation](../mcp/runtime-isolation.md#idle-maintenance)):

1. `expired_turns` finds turns outside **both** the newest `CHECKPOINT_KEEP_TURNS` (200) and the last
   `CHECKPOINT_KEEP_DAYS` (30). A turn is as old as its latest snapshot, so a turn resumed yesterday is
   recent. It reads repository refs only, per workspace.
2. `expire_checkpoints` removes `start` and `end` from those rows and keeps `changed`. This comes first:
   if a later step fails, the refs simply remain and the next run finds them again. The other order could
   leave a revert offered on a snapshot that is gone.
3. `forget` deletes the refs.
4. `collect_garbage` runs `git gc --auto` with `gc.autoDetach=false`, up to 15 minutes. Your gc settings
   apply, and server shutdown stops it.

An expired turn shows no file chip, cannot be reverted, and has no round segments. A fork from it copies
the current folder and says so (`from_snapshot=False`). The headless `files_changed` value stays.

Disk space does not come back at once. `gc --auto` acts only past its limits (about 6,700 loose objects or
50 packs), and unreferenced objects survive `gc.pruneExpire` (2 weeks by default). A repository with only a
few large files may stay below the limits.

## 5. Tests

`tests/test_workspace_checkpoint.py` (real git in a temporary repository): HEAD/index untouched, excluded
folders, changes and diffs, revert, refusal on later changes, export of a snapshot (including an empty
one in a repository without commits), a same-size edit in the second the index was written, and an engine
turn checkpointed and reverted, retention limits, `git gc --auto` keeping snapshots, and maintenance updating
the row and the refs in the right order. `tests/test_code_diff.py` covers boundaries: segments, a turn that stopped
mid-debate, a resumed turn, `forget`, a failed snapshot, and a real turn read round by round. Other tests run
with checkpoints switched off (`conftest._no_workspace_checkpoints`), so the developer's own `workspace`
repository collects no refs.
