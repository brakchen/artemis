# ACTIVE.md — artemis live lane registry

> This file contains **live work only**. Completed and abandoned history lives in Git history,
> not in this table. The canonical lifecycle is `docs/agent-git-workflow.md`.

## Registry rules

- States: `draft` -> `active` -> `ready`; temporary `blocked (<reason>)` is allowed. Delete the row
  after merge or abandonment.
- `draft`, `active`, and `blocked` reserve their declared files. A `ready` branch is immutable and
  may be integrated by any session.
- A ready row records immutable `head_commit` and `synced_master` values. Later registry-only
  commits do not invalidate it; later non-registry master changes do.
- `owner(session)` must be the real Pi session UUID, never `本 session`.
- `updated/ready_at (UTC)` is the last state-change time; for `ready`, it is the FIFO queue
  timestamp.
- `handoff/ACTIVE.md` is coordination metadata and must not appear in the owned-file column.
- Edit registry state only in a clean master/coordination/integration worktree under
  `/tmp/artemis-active-md.lock`; lane worktrees do not carry registry-only edits.
- Shared hotspot ownership does not require waiting for the original session: exchange a focused
  patch, hand off ownership explicitly, or create a successor lane from the predecessor's ready
  commit.
- Keep the owned-files column explicit and narrow. A lane owns what it declares — nothing else.

## Live lanes

| lane_id | Topic | owner(session) | branch/worktree | Owned files/directories | State | head_commit | synced_master | updated/ready_at (UTC) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| provider-ui-consistency | Unify model and Provider form styling | Codex:01a1957d-26fb-7173-abdc-ba44f87b31d0 | fix/provider-ui-consistency / D:/Codex Project/artemis-provider-ui | apps/showcase_ui/src/app/pages/home/home.component.html; apps/showcase_ui/src/app/pages/home/home.component.scss; apps/showcase_ui/src/app/core/i18n/zh.ts; docs/aegis/work/2026-11-02-provider-ui.md | active | - | - | 2026-11-02 15:00:00 |
