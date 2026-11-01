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
| minimax-design | 编写 MiniMax Anthropic 六模块技术方案 | Codex `01a1957d-26fb-7173-abdc-ba44f87b31d0` | `feat/model-config` / current checkout | `docs/designs/2026-11-01-minimax-anthropic.md` | active | — | `e7cbe5950fba16b7d31c852aa1e92309d7d6405c` (origin/main) | 2026-11-01T05:36:10Z |

> 本条为文档任务。当前运行环境为 Codex，owner 是实际 `CODEX_THREAD_ID`，不是 Pi session UUID；当前工具没有 tower-do。远端默认分支为 `origin/main`，不存在 `origin/master`，沿用用户已要求创建的 `feat/model-config` 分支。实现代码尚未认领或修改；方案待用户评审，lane 未进入 ready 或合并阶段。
