# Agent Git and worktree workflow

This is the canonical lifecycle for parallel agent-owned repository changes.

The core rule is: **branches and commits carry work; sessions do not**. A finished lane may be
validated, integrated, and cleaned up by any session.

## 1. Lane states

`handoff/ACTIVE.md` contains only live lanes.

| State | Meaning | File ownership |
| --- | --- | --- |
| `draft` | Registered but implementation has not started | Declared files reserved |
| `active` | Implementation is in progress | Declared files reserved |
| `blocked` | Cannot proceed without a named dependency or external action | Declared files remain reserved unless the row explicitly releases them |
| `ready` | Committed, synchronized with a recorded master commit, validated, and pushed | Branch is immutable; no session-specific ownership |
| merged/abandoned | Terminal state | Delete the row; history remains in Git |

Rules:

- `owner(session)` is the actual Pi session UUID, never `本 session`.
- `updated/ready_at (UTC)` is the FIFO timestamp when a lane enters `ready`.
- A ready row records immutable `head_commit` and `synced_master` values.
- `handoff/ACTIVE.md` is coordination metadata. Never list it in a lane's owned files.
- A ready lane may be integrated by its author or any other session.
- A later lane may build on a ready lane only by explicitly recording the predecessor commit and
  creating the successor branch from that commit. Otherwise wait for the short integration step so
  the successor starts from `origin/master`.

## 2. Register before editing

Use the main master worktree when it is clean. If it contains foreign WIP, create a clean temporary
coordination worktree from `origin/master` instead; never disturb the foreign changes.

```bash
git fetch origin
git status --short --branch
git worktree list
```

Then:

1. Read `handoff/ACTIVE.md` and confirm that no `draft`, `active`, or `blocked` lane owns the files
   you need.
2. Compare local master with `origin/master`; never assume local commits or files are yours.
3. If foreign WIP exists, do not modify, stash, restore, or delete it.
4. Under the short registry lock `/tmp/artemis-active-md.lock`, add one row with the real session
   UUID, exact branch/worktree, explicit file list, state `draft`, and UTC timestamp.
5. Commit and push only the registry update so the new worktree starts from a visible registration.

Registry lock scope is only fetch/check/edit/commit/push. Do not hold it while developing or
testing. If the lock is busy, retry shortly; it is not a reason to wait for another lane to finish.

## 3. Create the worktree from current remote master

Use a kebab-case topic and one of `feat/`, `fix/`, `chore/`, or `docs/`:

```bash
git fetch origin
git worktree add .worktrees/<slug> -b <prefix>/<slug> origin/master
cd .worktrees/<slug>
ln -s ../../.venv .venv          # reuse the shared uv environment
```

Because the branch is created from current `origin/master`, no additional merge is needed at
creation time.

- Never edit the main repository's `.env` through a symlink; never commit `.env` or secrets.
- Use the worktree path explicitly in tool calls.
- Run the narrowest safe smoke check (`uv run pytest tests/unit/<file> -q`).

## 4. Ownership and parallel work

- One writer owns each writable worktree.
- File ownership, not session identity, is the conflict boundary.
- Avoid concurrent writers for shared hotspots such as:
  - `artemis/llm/providers.py` and `artemis/services/llm.py`
  - `artemis/core/diagnostics/emulator_manager.py`
  - `apps/admin_console/routers/system.py`
  - `apps/showcase_ui/src/app/pages/home/`
  - `tests/conftest.py`
- If a small hotspot edit is needed, do not leave the entire lane waiting for the original session:
  1. complete and commit all independent work;
  2. give the hotspot owner a focused patch or exact change request; or
  3. after the current hotspot lane is ready, create an explicit successor lane from its commit.
- A lane must not claim `ready` until all required behavior is present, whether implemented
  directly or through an integrated dependency.

## 5. Commit and push policy

Commit atomic, reviewable units. Commit messages use a supported type plus a concise description:

```text
feat: ...
fix: ...
chore: ...
docs: ...
style: ...
merge: ...
```

Before pausing for user input, interruption, review, or handoff:

1. Commit all task-owned changes.
2. Push the lane branch to `origin`.
3. Report the branch and commit.

If the network is unavailable, commit locally and report the exact unpushed commit. Never leave
uncommitted WIP as the only copy and never force-push a shared branch.

## 6. Synchronize before entering ready

Finishing implementation is not enough. The lane must be validated against the master revision it
intends to integrate.

In the lane worktree:

```bash
git status --short --branch
git fetch origin
git merge --no-edit origin/master
```

If the merge conflicts:

1. Preserve both sides' intended behavior.
2. Never use blanket `--ours`, `--theirs`, or `-X theirs` resolution.
3. If intent is unclear, abort and ask rather than guessing.
4. Commit the conflict resolution.

Then:

1. Run diagnostics appropriate to the changed files (`uv run ruff check`, `npm run build`).
2. Run narrow relevant tests.
3. For code/test changes, run `uv run pytest tests/unit -q`.
4. Confirm zero new stable failures.
5. Review `git diff --check` and the final branch diff.
6. Record the current `origin/master` commit as `synced_master`.
7. Push the synchronized branch and record its HEAD as `head_commit`.
8. Under the short registry lock, update the row to `ready` with both commits; the update timestamp
   becomes `ready_at`.

The ready registry commit itself advances master only in `handoff/ACTIVE.md`; it does not
invalidate the lane. After `ready`, the branch is immutable. New fixes require moving it back to
`active`, clearing the recorded commits and old ready timestamp, and repeating synchronization and
validation.

Documentation/config-only lanes do not need application tests when they change no code or tests,
but must validate their own links, paths, syntax, and commands.

## 7. Ready queue and takeover

Ready lanes integrate in ascending `ready_at` order by default. Urgent fixes may jump the queue only
when the row records the reason.

The session that finishes first should normally continue directly into integration. However:

- no lane waits for its original session to return;
- any session may take the next ready lane;
- takeover starts from the recorded branch and commit, not from uncommitted files or session logs;
- the integrator rechecks the branch, validation evidence, and current `origin/master`.

## 8. Final integration into master

Only final integration is serialized. Acquire `/tmp/artemis-master-merge.lock` and hold it through
prospective-master creation, post-merge validation, registry cleanup, and push.

Use a clean temporary integration worktree so foreign WIP in the main worktree cannot affect the
merge:

```bash
git fetch origin
git diff --name-only <synced_master>..origin/master -- . ':(exclude)handoff/ACTIVE.md'
git worktree add --detach .worktrees/.integrate-<slug> origin/master
cd .worktrees/.integrate-<slug>
git merge <branch> --no-ff -m "merge: <slug> (lane <lane-id>)"
```

Before merging, require that:

- the branch HEAD equals the row's `head_commit`;
- no path other than `handoff/ACTIVE.md` changed between `synced_master` and current `origin/master`;
- the temporary integration path does not already exist.

Registry-only commits after `synced_master` are expected and do not invalidate validation. If any
non-registry path changed, release the integration lock, return the lane to `active`, merge the new
`origin/master` into it, rerun required checks, push, and assign new commit fields and `ready_at`.
Do not hold the master lock while resolving lane conflicts or running lane preflight tests.

Validate the exact detached commit that would become master:

- Documentation/config-only lane: confirm the merge contains no code changes, then rerun
  documentation validation (links, paths, commands).
- Code/test lane: rerun `uv run pytest tests/unit -q`, and `npm run build` when UI files changed.
- If two lanes changed the same module, run the full unit suite even when Git reported no textual
  conflict.

After validation, delete the merged lane row from `handoff/ACTIVE.md` in the integration worktree
and commit that registry cleanup. Then push the exact tested commit:

```bash
git push origin HEAD:master
```

Never force-push. Do not push when validation introduces a new failure. Because the prospective
merge lives in a detached integration worktree, a failed integration does not alter the shared
local master branch; retain the worktree for diagnosis or remove it only after confirming it has no
uncommitted evidence that must be preserved.

A remote push rejection means another writer advanced master despite the local lock or from another
machine. Remove or retain the failed integration worktree as appropriate, resynchronize the lane
with the new `origin/master`, rerun validation, and retry normally.

## 9. Cleanup

Only after merge validation, registry cleanup, and successful push:

```bash
git fetch origin
git log --oneline origin/master..<branch>
git worktree remove .worktrees/.integrate-<slug>
git worktree remove .worktrees/<slug>
git branch -D <branch>
git worktree prune
git worktree list
```

`git log --oneline origin/master..<branch>` must be empty before deleting the branch. If it is not
empty, stop because the branch still contains unmerged commits.

## 10. Safe Git operations

See `AGENTS.md` §9 for the full allow/forbid list. Summary: prefer `git revert`,
`git restore --staged`, and per-file restore; never `git reset --hard`, `git checkout -- .`,
`git clean -f`, or blanket conflict resolution.
