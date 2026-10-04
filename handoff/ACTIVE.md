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
| _example/lane-id | 示例：把任务描述写成祈使句 | _session-uuid_ | `feat/example` / `.worktrees/example` | `artemis/…`; `tests/unit/…` | draft | — | — | 2026-01-01T00:00Z |
| i18n-complete | 完成 showcase_ui 中文翻译（调优卡/推荐任务卡/其余未翻译文案） | 01a104cc-3128-7668-b8bc-2c98f3ae5784 | `fix/i18n-complete` / `.worktrees/i18n-complete` | `apps/showcase_ui/src/app/**` | draft | — | — | 2026-10-04T03:10Z |

> 上面是格式示例，登记真实 lane 时请删除该行。
