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
| minimax-implementation | 实施已批准的 MiniMax v1.2 方案 | Codex `01a1957d-26fb-7173-abdc-ba44f87b31d0` | `feat/model-config` / current checkout | `artemis/config/`、`artemis/llm/`、`artemis/services/`、`artemis/context.py`、`artemis/sdk/`、`artemis/agents/`、`artemis/graph/`、`artemis/tools/`、`artemis/utils/`、`artemis/memory/`、`artemis/mcp/`、`artemis/data_engine/`、`artemis/core/diagnostics/`、`third_party/mobile_use/config/`、`third_party/mobile_use/sdk/types/task.py`、`mcp_server/`、`apps/admin_console/`、`apps/showcase_ui/src/`、`playground/backend_manager/app/services/docker_service.py`、`playground/artemis_container/docker-compose.yml`、`config/artemis.jsonc`、`.env.example`、`pyproject.toml`、`uv.lock`、`tests/unit/`、`README.md`、`README_CN.md`、`docs/designs/2026-11-01-minimax-anthropic.md`、`docs/aegis/work/2026-11-01-minimax/` | active | — | `e7cbe5950fba16b7d31c852aa1e92309d7d6405c` (origin/main) | 2026-11-01T06:10:17Z |

> 用户已批准按 v1.2 开发。当前运行环境为 Codex，owner 是实际 `CODEX_THREAD_ID`，不是 Pi session UUID；当前工具没有 tower-do，任务分解与证据保存在上述唯一 work 记录，协作消息同步。远端默认分支为 `origin/main`，不存在 `origin/master`，沿用用户已要求创建的 `feat/model-config` 分支；不合并 main。协调者是唯一 Git 操作者，实施与审查按片段串行交接，不并发修改同一工作区。
