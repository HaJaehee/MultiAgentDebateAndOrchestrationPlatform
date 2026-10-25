# Workspace Checkpoints & Reverting a Turn (v1.5)

Agents create and edit files in the workspace through tools. People need to see what one turn changed
and undo it if it went wrong — without that, `auto` tool mode is a gamble. The workspace is already a
git repository (`ensure_workspace`), so the host records snapshots there.
Code: [app/workspace_checkpoint.py](file:///d:/MultiAgentDebateOrchestration/app/workspace_checkpoint.py).

## 1. Snapshots that stay out of the agents' git history

At the start of a turn (right after the request is recorded, before planning) and when it ends — also
when it fails — the engine takes a snapshot:

1. Copy the repository's index to a temporary file and point `GIT_INDEX_FILE` at it (unchanged files are
   not re-hashed, and the real index — what an agent `git add`ed — is untouched).
2. `git add -A` with the dependency and cache folders excluded (`node_modules`, `.venv`, `__pycache__`,
   `.mado`, `.memory-graphs`, …; `dist` and `build` are kept), `write-tree`, `commit-tree`.
3. Name the commit `refs/mado/turns/<turn id>/start` or `…/end`.

HEAD, branches and the index never move, so `git log` and `git status` look the same to agents. The turn
row stores `checkpoint = {start, end, changed}` and the engine sends `workspace_checkpointed` after
`turn_completed`. No git, or not a repository: nothing happens and the turn is unaffected.

## 2. Seeing and reverting

The turn's conclusion card gets a file chip (`📁 N`). It opens a dialog with the changed files (added /
modified / deleted) and a per-file diff. `이 턴 되돌리기`:

* deletes files the turn created (and folders left empty), restores modified and deleted ones from
  `start` (`git restore --worktree`);
* **refuses** when a file the turn touched changed again after it ended (a later turn, another
  conversation on the same folder, a person) and lists those files — reverting must not overwrite later
  work;
* is disabled while the conversation is running and after the turn was reverted once;
* records a `system` note in the conversation (`[작업 공간 되돌리기] …`) so later agents know those files
  are back to their earlier state.

Discarding a turn or deleting the conversation removes its refs (`forget`); git collects the objects later.

## 3. Forks

A fork exports the `end` snapshot of the chosen turn into a new folder (`export`: clone, fetch the ref,
`read-tree -u --reset`, index back to HEAD, origin removed). See
[Session Handoff §7](session-handoff.md#7-forking-a-conversation-at-a-turn-v15).

## 4. Tests

`tests/test_workspace_checkpoint.py` (real git in a temporary repository): HEAD/index untouched, excluded
folders, changes and diffs, revert, refusal on later changes, export of a snapshot (including an empty
one in a repository without commits), and an engine turn checkpointed and reverted. Other tests run
with checkpoints switched off (`conftest._no_workspace_checkpoints`), so the developer's own `workspace`
repository collects no refs.
