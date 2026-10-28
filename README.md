# opencode-agora

[English](README.md) | [简体中文](README.zh-CN.md)

Multi-agent delegation for [opencode](https://opencode.ai) v2 — you state goals, agents deliver verified code, merges happen automatically.

```
You: "Fix the login bug in repoB"
     └─ agora dispatches a worker into an isolated git worktree
        worker fixes → tests pass → code reviewed → auto-merged
        You check the board: ✅ merged
```

## Why

| Problem | Solution |
|---------|----------|
| Agents pollute your working tree | Every task gets its own worktree; main checkout untouched |
| No orchestration across repos | Dependency-ordered dispatch (`after:`); unrelated repos run in parallel |
| Dispatched agents are black boxes | Real-time board, escalation alerts, append-only audit ledger |
| No accountability | Verify gate (real test run) + review gate + full audit trail |

## Quick Start

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

Requirements: `python3` (stdlib only), `git`, `opencode` v2, `bun`.

After install, talk to your opencode session normally:

- *"Have an agent fix X in repoY"* → task created, worker dispatched
- *"How's it going?"* → board with five buckets and chain view
- *"Stop that task"* → cascade-cancel with confirmation

Standalone dashboard:

```bash
agora-board    # vi-motion TUI: chain tree, buckets, escalation feed
```

## How It Works

```
Human ──▶ Main Session (skills: agora-dispatch / agora-ops)
              │  MCP (12 tools)
              ▼
         agorad (Python stdlib service)
         state authority · verify gate · auto-merge
         message routing · crash recovery · audit
              │
         Per-task worktrees (git isolation)
```

Each task: `create → dispatch → worker codes → verify → auto-merge (ff-only) → closed`

**Verify gate**: the quality bar. Worker proposes a verify command → dispatcher validates and registers it → service runs it in the worktree → green = merge, red = worker gets output and retries. Three consecutive failures = blocked + escalation alert.

**Review gate**: if [review-loop](#requirements) is installed, done additionally requires a green code review at the exact commit. Without it, verify passes but **no auto-merge** — you review manually.

## Key Features

- **Full autonomy**: verify green + review green = ff-only auto-merge. No human confirmation needed.
- **Enforced isolation**: plugin-level gate blocks git-repo writes without task credentials. Single machine, zero-trust between sessions.
- **Cross-repo orchestration**: `task_create(after: previous_task_id)` for dependency chains. Unrelated repos dispatch in parallel.
- **Crash recovery**: `kill -9` the service at any time. State is append-only snapshots, workers re-inject with same credentials on restart.
- **Real-time escalation**: quality failures, dispatcher death, gate tampering — all ping the main session within seconds.
- **Session tokens**: per-session credentials with role matrix (root / dispatcher / worker). Workers can never cancel, modify verify, or access the board.
- **Scope control**: list repos in `inactive-repos` to make agora fully dormant there.
- **Target branch support**: `task_create(target_branch="feature/x")` merges into that branch instead of main — compatible with PR-based workflows.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `AGORA_HOME` | `~/plaza` | Ledger home (state, messages, audit, wiki) |
| `$AGORA_HOME/.agora/inactive-repos` | empty | Repos where agora is dormant (one path per line) |
| `AGORA_BOARD_INTERVAL` | `5` | Dashboard poll interval (seconds) |
| `enforce` | on | Global gate switch (root only, audited) |
| Chain depth / quota | 5 / 8 | Hard limits on dispatch nesting and concurrency |
| `$AGORA_HOME/.agora/root.token` | auto | Root credential (created by install, 0600) |

## Requirements

- **opencode v2** — session creation, message injection, plugin hooks
- **git** — worktree isolation and ff-only merging
- **python3** (≥3.8) — agorad service and MCP shell (stdlib only, zero pip dependencies)
- **bun** — plugin runtime (TypeScript)
- **review-loop** *(optional)* — code review gate. Without it, verify passes but tasks stay at `done` (no auto-merge). [More info](#review-loop-dependency)

### Review-loop dependency

Auto-merge requires a green code review at the exact commit being merged. This checks the `.review-loop/.last-reviewed` anchor in the worker environment. If the directory doesn't exist, the task stays at `done` — you review and merge manually. This is by design: autonomy is earned by having both quality gates (tests + review).

## Safety Model

**Enforced** (machine-checked, can't bypass):
- Git-repo writes only within task worktrees (plugin gate + session tokens)
- Verify must actually run and pass (service executes the command)
- Review anchor must match HEAD (if review-loop present)
- Role matrix: workers can't cancel, modify verify, or toggle enforce
- All state changes audited to append-only JSONL

**Accepted risks** (single-machine trust model):
- `bash` direct writes bypass the gate (audit backstop)
- Token theft within same user account
- Review anchor is self-written by the worker (service reads but doesn't verify authorship)

## License

MIT
