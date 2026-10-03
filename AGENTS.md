# AGENTS.md — artemis

> Repository-wide instructions for coding agents. Read this file before changing anything.
> Detailed procedures live in `docs/`; this file keeps only rules that apply to most tasks.

## 1. Instruction scope and precedence

1. Explicit user instructions override repository instructions.
2. Rules in a more specific instruction file override this root file for that subtree.
3. Safety rules in this file always apply unless the user explicitly authorizes a documented exception.
4. If two repository instructions conflict, stop and ask instead of choosing the less restrictive rule.
5. Before working on a specialized area, read the matching document in §9.

## 2. Project overview

- Purpose: drive real Android phones/emulators from natural-language tasks — an autonomous
  mobile agent with a web console, an MCP server, and a Python SDK.
- Stack: Python 3.12, FastAPI/uvicorn, Pydantic, LangChain/LangGraph, uv; Angular 22
  (signals + `@if`/`@for` control flow) for the web console; adb / emulator / scrcpy / ffmpeg
  for the device layer; systemd user units for local operations.
- Layout:
  - `artemis/` — core runtime: config, agents, LLM router, drivers, CLI.
  - `apps/admin_console/` — FastAPI backend for the web console (`/api/system/*`, `/api/tasks/*`).
  - `apps/showcase_ui/` — Angular web console.
  - `mcp_server/` — MCP server exposing the agent to AI IDEs.
  - `tests/` — pytest suites (`tests/unit` is the fast, deterministic layer).
  - `config/artemis.jsonc` — model/provider per-role configuration.
- Local operations record (tunnel, auth proxy, SDK, AVD, runbook): `~/setup/artemis/README.md`.
- Work ownership registry: `handoff/ACTIVE.md`.
- Live multi-agent coordination board: tower-do (`tower_do`, `tower_do_talk`, `tower_do_status`; see §8).

## 3. Non-negotiable safety boundaries

- Never commit secrets: `.env`, `~/.local/share/artemis/llm_providers.json`, or any API key.
  Keys live outside the repo; the provider registry masks them in every API/UI response.
- Never run automated tests or task runs against a real phone unless the user explicitly asked
  for that device. Use the emulator (`Pixel_8_API_34`) for anything unattended.
- Respect the single-VM policy: only one emulator instance may run at a time (memory-limited
  host). Do not bypass it with `-read-only` or a second AVD without an explicit request.
- Never execute destructive ADB against a device (`adb shell rm`, factory reset, `pm clear`)
  without explicit human authorization for that device.
- Never use repository-wide destructive Git commands: `git reset --hard`, `git checkout -- .`,
  `git clean -f`, or blanket `--ours` / `--theirs` / `-X theirs` conflict resolution.
- Do not modify, stash, restore, or delete another session's worktree or uncommitted files.
- Do not weaken `apps/admin_console/core/security.py` (Host/Origin boundary) or the basic-auth
  layer in front of the console; the console has no login of its own.
- Never add a destructive HTTP, CLI, or task path without a guard and an explicit confirmation step.
- Do not widen the emulator spawn arguments to open a window (`-no-window` is intentional:
  the service context has no display and screen capture goes through scrcpy).

## 4. Domain invariants

### Model / provider resolution

- `config/artemis.jsonc` maps roles (`default`, `presets`, `nodes`) to `provider` + `model`.
- User-registered providers live in the LLM provider registry (`artemis/llm/providers.py`):
  OpenAI-compatible `api_base` + `api_key` + a `model` / `fallback_model` pair.
- Resolution is shared by the runtime and the console through
  `artemis.llm.providers.resolve_model_config()` — never fork the logic into a second place.
- A registered **default** provider wins for any key-carrying built-in; removing the default
  restores built-in behaviour. `ollama` / `vllm` / `vertexai` are never substituted.

### Web console

- UI strings go through the `t` pipe: `{{ 'English source' | t }}`, with translations in
  `apps/showcase_ui/src/app/core/i18n/zh.ts`. English is the source language; untranslated
  keys fall back to English on purpose.
- The `t` pipe is impure by design so a language switch re-renders immediately.
- String literals inside Angular expressions must be wrapped too: `{{ c ? ('A' | t) : ('B' | t) }}`.
- Run `npm run build` after any UI change; it type-checks templates.

### Conventions by layer

- JSON/TypeScript camelCase, Python snake_case, HTTP headers lowercase-with-hyphens.
- Use `from __future__ import annotations` and type hints in Python modules.
- Prefer signals + `computed` over mutable component state in Angular; components are standalone.

## 5. Canonical commands

| Task | Command |
| --- | --- |
| Fast test suite (unit) | `uv run pytest tests/unit -q` |
| One test file | `uv run pytest tests/unit/test_llm_providers.py -q` |
| Frontend unit tests | `cd apps/showcase_ui && CHROME_BIN=/usr/bin/google-chrome npx ng test --watch=false --browsers=ChromeHeadless` |
| Build web console (type-checks templates) | `cd apps/showcase_ui && npm run build` |
| Python lint / format | `uv run ruff check` · `uv run ruff format` |
| Install dependencies | `make install` (= `uv sync --dev`) |
| Diagnostics | `uv run artemis doctor` |
| Manage LLM providers | `uv run artemis providers list\|add\|models\|test\|set-default\|remove` |
| Restart console service | `systemctl --user restart artemis-ui` |
| Restart auth proxy / tunnel | `systemctl --user restart caddy-artemis frpc-artemis` |
| Service status | `systemctl --user status artemis-ui caddy-artemis frpc-artemis` |
| Service logs | `journalctl --user -u artemis-ui -n 50` |
| Emulator boot log | `journalctl --user -u artemis-ui -f` (or console: View Boot Logs) |

Notes:

- `tests/unit` needs no device and no credentials; anything above it may need both.
  A missing `GOOGLE_API_KEY` alone makes 4 upstream unit tests fail — that is expected.
- The console listens on `127.0.0.1:8001`; the public entry is `http://207.57.126.199:6001`
  behind basic auth (see `~/setup/artemis/README.md`).

### 5.1 Reuse-first implementation policy

Before implementing non-trivial functionality, agents must first search for a maintained existing solution:

1. Search this repository for an existing equivalent.
2. Search official documentation, GitHub, and the relevant package ecosystem:
   - Python: PyPI
   - JavaScript/TypeScript: npm
3. Prefer, in order:
   - existing project code;
   - official SDKs and maintained libraries;
   - small, auditable adaptations of a proven upstream implementation;
   - a new in-house implementation only when no suitable reusable solution exists.

When a suitable library, SDK, or upstream implementation satisfies the request, adopt it directly
without requesting human confirmation. Before adoption, verify compatibility with this repository,
licence, maintenance status, security posture, and dependency footprint. Pin or constrain
dependency versions appropriately. Record the upstream URL, package version or commit, and licence
in relevant code comments or technical documentation.

Do not reimplement functionality that a suitable maintained dependency already provides. Do not add
a dependency or copy upstream code for trivial logic where a small local implementation is clearer
and safer.

## 6. Worktree, review, and completion rules

- Every task starts in a dedicated branch/worktree created from current `origin/master`. Do not
  develop task code in the main master worktree; use it only for short registry updates, or use a
  clean temporary coordination worktree when foreign WIP is present.
- Before the first task edit, register the lane in `handoff/ACTIVE.md` with the real Pi session
  UUID and explicit file ownership. `handoff/ACTIVE.md` is coordination metadata and must not be
  listed as a lane-owned file.
- Track task decomposition, ownership, dependencies, and cross-session progress on tower-do (§7).
  The board complements but never replaces the lane registry.
- `draft`/`active` lanes own their declared files. A `ready` lane is immutable and may be
  integrated by any session; session identity never blocks merge or cleanup.
- Ready lanes merge in `ready_at` order by default. Before entering `ready`, merge current
  `origin/master` into the lane, resolve conflicts, rerun required checks, commit, and push;
  record both lane HEAD and the synchronized master commit. If master later gains non-registry
  changes, repeat synchronization and validation. Commits changing only `handoff/ACTIVE.md` do
  not invalidate the lane.
- Serialize the final master merge, post-merge validation, registry cleanup, and push with
  `/tmp/artemis-master-merge.lock`. Build the prospective master commit in a clean temporary
  integration worktree, merge with `--no-ff`, validate that exact commit, then push it to master
  without force.
- Do not modify or stash another lane's work. For shared hotspots (for example
  `artemis/llm/providers.py`, `artemis/services/llm.py`, `apps/showcase_ui/src/app/pages/home/`),
  keep one writer and exchange a patch or create an explicit successor lane instead of waiting
  for the original session to return.
- Keep each writable worktree owned by one writer unless separate worktrees are used.
- Commit and push the task branch before merge or before pausing for user input. If the network
  is unavailable, commit locally and report that push remains pending.
- Commit messages use `feat/fix/chore/docs/style/merge` plus a concise description.
- In a worktree, never use `git add -A`, `git add .`, or `-A`-style wildcards for staging: they
  sweep in local state and gitignored files. Always stage explicit file paths. If you used `-A`,
  run `git status` before committing and unstage anything that is not your own change.

Definition of done:

1. Run the narrowest relevant test command.
2. For code/test changes, run `uv run pytest tests/unit -q` and (for UI changes)
   `npm run build`; the default requirement is zero failures. Isolate and rerun failures once
   to distinguish flakes.
3. Update contracts and operational documentation affected by the change.
4. Confirm no secrets, unrelated WIP, or staged foreign files are included.
5. Confirm the lane was synchronized with the master revision it integrated, then confirm the
   worktree and master are clean and the required branch/master pushes succeeded.
6. Reconcile tower-do: complete delivered tasks with `changedFiles`, leave honest blocker state,
   and reply to relevant messages/findings.

Detailed lifecycle, environment setup, conflict handling, and cleanup: `docs/agent-git-workflow.md`.

## 7. 多会话 / 多 agent 协调（tower-do）

本仓库使用 Pi 扩展 `tower-do`。它是所有 Pi 会话与子 agent 共享的实时协调板，记录任务、归属、
依赖、留言和 findings。复杂开发任务默认优先在板上拆解；只要涉及多会话或多 agent 并行，就必须
用板协调，避免撞文件、重复劳动和交接丢失。

`tower-do` 与 Git lane 各管一层，两者都要维护：

| 层 | 工具 / 文件 | 职责 |
| --- | --- | --- |
| 实时任务协调 | `tower_do` / `tower_do_talk` / `tower_do_status` | 任务拆解、认领、依赖、留言、findings、完成回执 |
| Git lane 登记 | `handoff/ACTIVE.md` | branch/worktree、文件归属、ready 队列、合并顺序（§6） |

### 7.1 三个工具

- `tower_do_status`：只读查看任务、owner、依赖、留言、findings、在场会话、scope 冲突、板文件路径
  和当前 `revision`。开工前、交接前、结束前都要读。
- `tower_do`：原子更新整个任务板；用于创建、认领（`owner` + `in_progress`）、阻塞（`blocked` +
  `blockedBy`）和完成（`completed` + `changedFiles`）。
- `tower_do_talk`：给任务 owner、`tower` 或 `all` 发留言；用 `finding` 记录范围外的
  `bug` / `improve` / `vuln` / `idea`。`inbox` 会确认已读；只想查看而不确认时用 `tower_do_status`。

### 7.2 开工前先拆解

- 三步以上的开发任务应先上板拆解，再改文件。一个任务应对应一次可独立提交、验证或交接的结果；
  不要把整个大特性塞进一个模糊任务。
- `key` 使用稳定、简短的小写标识；`subject` 写祈使句；`description` 只写耐久任务陈述，临时日志和
  证据放留言或 finding。
- 用 `scope` 声明可能修改的文件或 glob。并行任务的 scope 要尽量不相交；同一文件同一时刻只允许一个
  writer。scope 冲突只是提示，不是锁；看到冲突后立即用 `tower_do_talk` 与 owner 协商。
- 有顺序关系的任务用 `dependsOn`；依赖未完成时不得把后继任务置为 `in_progress` 或 `completed`。
  等待外部动作或非任务标识时，用 `blocked` + `blockedBy` 明确写出等待对象。
- 大特性优先拆成可并行的设计、后端、前端、测试、文档等子任务。用户允许委派且 scope 独立时，尽量交给
  不同 agent / 会话并行执行；需要串行的部分用 `dependsOn`，不要靠口头约定。

### 7.3 认领、写板与完成纪律

- 开始工作前先读 `tower_do_status`，然后把 `owner` 与 `status: in_progress` 在同一次写入中设置；
  不要修改其他 owner 的任务，应用 `tower_do_talk` 联系对方。
- 每次 `tower_do` 写入都要携带最近一次读取或写入返回的 `baseRevision`。遇到 stale 拒绝时重新读板、
  合并同伴更新后再提交。
- `tower_do` 是任务级全量替换：`tasks` 数组必须复述所有要保留的 key；现有任务的未改字段可省略。
  写前先看完整 board，不能因默认视图折叠而漏掉同伴任务或历史完成回执。
- owner 保护适用于任务所有字段。owner 自身在该任务上连续 6 小时无活动时，才可按工具契约接管未完成
  任务：第一次写入只改 `owner`，第二次再更新内容或状态。
- 任务只有在实现和验证都完成后才能标记 `completed`，且同一次写入必须附实际改动的仓库相对路径
  `changedFiles`；失败、未验证或部分完成时保持 `in_progress` 或诚实标记 `blocked`。
- 离开、暂停或最终回复前再次读板并对账：自己的任务都有明确状态，相关留言已回复，认领的 finding 已
  完成、拒绝或附原因延期。

### 7.4 多 agent 并行与交接

- 每个可写 worktree 只安排一个 writer；tower-do 的 task `scope` 应与 `handoff/ACTIVE.md` 的 lane
  文件归属一致。板负责实时协作，§6 的 worktree、测试、提交、推送和合并规则仍是最终约束。
- 把 `tower_do_status` 返回的 board 文件路径交给参与任务的子 agent，作为共享的 file-as-state；
  替子 agent 记账时使用其真实 id 作为 `as`。
- agent 完成子任务后必须回写状态与 `changedFiles`，父会话再汇总依赖、运行整体验证并收尾。不要只在
  聊天里说“完成”而让板保持过期状态。
- 发现不属于当前 scope 的问题时，优先创建 finding 并通知 owner；不要顺手修改别人的 lane。认领
  finding 后要走 `accepted` → `done` / `rejected` / `snoozed` 生命周期，并在关闭或延期时写明原因。

## 8. Required context by task

| When touching | Read first |
| --- | --- |
| Product intent, install, usage | `README.md` |
| Contribution workflow, code style | `CONTRIBUTING.md` |
| Local deployment: tunnel, auth proxy, SDK, AVD, runbook | `~/setup/artemis/README.md` |
| Model/provider resolution | `artemis/llm/providers.py`, `artemis/services/llm.py` |
| Web console API | `apps/admin_console/routers/system.py` |
| Web console UI / i18n | `apps/showcase_ui/src/app/pages/home/`, `apps/showcase_ui/src/app/core/i18n/` |
| Emulator & device lifecycle | `artemis/core/diagnostics/emulator_manager.py` |
| Security boundary (Host/Origin, basic auth) | `apps/admin_console/core/security.py` |

## 9. Safe Git operations

Allowed alternatives to destructive repository-wide resets:

- Revert a public commit: `git revert <commit>`.
- Unstage a file without changing bytes: `git restore --staged <file>`.
- Restore one owned file from a known commit: `git restore --source=<commit> -- <file>`.
- Discard an owned file's local change only after confirming ownership: `git checkout -- <file>`.
- Stash only your own lane's work, never a shared or foreign worktree.

Forbidden:

- `git reset --hard`
- `git checkout -- .`
- `git clean -f`
- blanket `--ours`, `--theirs`, or `-X theirs`
- `git add -A && git commit` as a substitute for a real merge
- deleting a worktree or branch before proving all commits are merged
