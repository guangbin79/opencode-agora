# opencode-agora

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](https://www.python.org)
[![made for opencode v2](https://img.shields.io/badge/made%20for-opencode%20v2-6e7bff.svg)](https://opencode.ai)

[English](README.md) | [简体中文](README.zh-CN.md)

Multi-agent delegation for [opencode](https://opencode.ai) v2. Installing it upgrades your main session into an **orchestration agent** — **the LLM plans, the machine verifies**. Each task runs in its own git worktree; only verified work gets merged.

```text
You:    "Fix the login bug in repoB. Run pytest -q, merge when green."
agora:   └─ spawns a worker in an isolated git worktree
             worker fixes → service runs pytest itself → review green → ff-only merge
You:    "How's it going?"   →   board: ✅ merged
```

The difference in one line: **the LLM holds the steering wheel; the machine holds the brakes.** Orchestration — what to run, in what order, when to replan — stays in your conversation, so mid-flight intent (*"now apply the same fix to repoB and repoC"*) becomes a new schedule on the spot. And agents don't grade their own homework here: the verify command runs under the service, not the worker — green means the tests actually passed on the exact commit being merged.

## Quick Start

Prerequisites: [opencode](https://opencode.ai) v2 (`curl -fsSL https://opencode.ai/v2/install | bash`), [bun](https://bun.sh) (`curl -fsSL https://bun.sh/install | bash`), `git`, `python3` ≥ 3.10 (stdlib only — zero pip dependencies).

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

That's it. The installer is idempotent (safe to re-run); it registers the MCP server, copies the plugin + skills, and leaves a root credential at `$AGORA_HOME/.agora/root.token` (0600).

Now just talk to your opencode session as usual:

| You say | What happens |
|---|---|
| *"Have an agent fix X in repoY, verify with `pytest -q`"* | Task created, worker dispatched into its own worktree |
| *"How's it going?"* | Live board: five buckets, dependency chains, escalation feed |
| *"Stop that task"* | Cascade-cancel with confirmation |
| *"Bump the dependency in repoA, then apply the same fix in repoB and repoC"* | `after:` dependency chain — B and C wait for A, unrelated repos run in parallel |

Standalone dashboard: `agora-board` (vi-motion TUI). Update path: `git pull && ./install.sh`.

## Where It Sits

| | LLM-led orchestration | Machine-run quality gate |
|---|:---:|:---:|
| Task-graph orchestrators (plan once, execute a static graph) | ✗ | ✓ |
| Agent-swarm frameworks | ✓ | ✗ (agents self-report) |
| **opencode-agora** | ✓ | ✓ |

Orchestration needs the model's flexibility — real intent surfaces mid-conversation, not in an upfront plan. Merge quality must not trust the agent — it has to run where the agent can't fake it. Most projects take one side; agora is built on the bet that you need both.

Making your session an orchestrator isn't unique — subagent trees do that too. The unique part, on an open-source and model-neutral stack, is the **control plane that keeps the orchestrator honest**: verify gate, role-separated credentials, and append-only audit running as an independent service the agent cannot rewrite. Native tools may absorb this tomorrow; today the gap is real.

Two capabilities this control plane buys in practice:

- **Single repo: enforced worktree writes.** Every agent edit is forced into the task's own worktree by the plugin gate + session tokens — your main checkout is never touched. Convention becomes mechanism.
- **Multiple repos: one root agent, sibling repos.** A single session at the parent directory orchestrates cross-repo chains with `after:` — the downstream task sees the upstream repo's merged result through the `../` sibling layout, while unrelated repos run in parallel.

**Honest trade-offs** (the same reasons you might not want this):

- A wrong `verify_cmd` at creation can stall a task — it fails safe: workers can't edit verify, so the worst case is a stuck task, never a bad merge
- Your main session's context is the orchestration bottleneck — that's why `agora-board` exists: five-bucket state at a glance without spending conversation window
- Every scheduling decision is a model call — if your work pre-decomposes into a clean static graph, a zero-token orchestrator is cheaper and more reproducible

## How It Works

```text
Human ──▶ Main session (skills: agora-dispatch / agora-ops)
              │  MCP (13 tools)
              ▼
         agorad — Python stdlib service on localhost
         state authority · verify gate · auto-merge · message routing
         crash recovery · audit ledger
              │
         Per-task git worktrees (enforced isolation)
```

Task lifecycle: `create → dispatch → worker codes → verify (service-run) → auto-merge (ff-only) → closed`

After install, your session gains two skills (agora-dispatch / agora-ops) and 13 MCP tools. There is no background planner: the model you talk to **is** the orchestrator — task creation, `after:` chains, cancels, escalation replies are all model decisions from conversation context. agorad stays the state authority: verify executor, merge gate, message router, audit ledger.

## Two Quality Gates

**Verify gate** — the non-negotiable one. The verify command is registered at task creation (`task_create(verify_cmd="pytest -q")`, or `verify_pending: true` to defer). The **service runs it in the task worktree**, then trial-merges into a detached integration worktree and runs it again. Only two green runs advance the target branch; integration failures and test side effects never touch your main checkout. Each run has a timeout (300 seconds by default). Red = the worker gets the output and retries. Three consecutive failures = blocked + escalation ping. The worker cannot skip it, fake it, or report results itself.

**Review gate** — an open standard, bring your own reviewer. The contract is one anchor file:

- If `.review-loop/` exists in the task worktree, the last-reviewed anchor is checked at `done` and **re-checked immediately before merge** (closes the "reviewed, then sneaked a commit" window).
- Anchor green + verify green = automatic ff-only merge.
- No `.review-loop/` directory? Everything still works — tasks complete at `done` and **you merge manually**. Autonomy is earned by both gates, not assumed.

Any reviewer fits — OpenCodeReview, CodeRabbit, a human checklist — agora only reads the anchor. A minimal reviewer is ~10 lines:

```bash
#!/usr/bin/env bash
# review-loop.sh <worktree> <target_branch> — run after each commit; exit non-zero if not green
set -euo pipefail
cd "$1"
base=$(git merge-base HEAD "${2:?}"); sha=$(git rev-parse HEAD)
findings=$(your_reviewer "$(git diff "$base"..HEAD)")   # OCR / CodeRabbit / human list
printf '%s\n' "$findings" | grep -qiE 'blocker|high' && { printf '%s\n' "$findings"; exit 1; }
mkdir -p .review-loop && printf '%s\n' "$sha" > .review-loop/.last-reviewed
```

Full spec — contract details, worker protocol, threat model, idle auto-trigger design notes: [docs/review-loop.md](docs/review-loop.md) (Chinese).

## Highlights

- **Verified autonomy** — auto-merge only after service-run tests pass and the review anchor is green, with the anchor re-verified at merge time
- **Enforced isolation** — plugin-level gate blocks git-repo writes without task credentials; workers can't touch your main checkout
- **Cross-repo orchestration** — `after:` dependency chains; unrelated repos dispatch in parallel; hard limits on nesting (5) and concurrency (8)
- **Role-separated credentials** — root / dispatcher / worker session tokens; workers can never cancel tasks, alter verify commands, or read the board
- **Crash recovery** — `kill -9` the service at any moment; append-only state + worker re-injection with the same credentials on restart
- **Real-time accountability** — escalation pings to the main session, append-only JSONL audit for every state change, health patrol for dead dispatchers
- **PR-workflow friendly** — `task_create(target_branch="feature/x")` merges into a feature branch instead of main
- **Lightweight** — Python stdlib service, zero pip dependencies, localhost only, no telemetry

## Comparison

| | Manual multi-session | Swarm frameworks | opencode-agora |
|---|---|---|---|
| Per-task isolation | you manage worktrees | varies | enforced (plugin gate + worktree per task) |
| "Tests pass" means | agent says so | agent says so | service ran the command itself |
| Cross-repo dependencies | manual sequencing | yes | `after:` chains, parallel when unrelated |
| Merge | manual | varies | automatic, only when both gates are green |
| Audit trail | none | partial | append-only JSONL, every state change |
| Install footprint | — | heavy runtimes | python3 stdlib + git + bun |

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `AGORA_HOME` | `~/plaza` | Ledger home (state, messages, audit, wiki) |
| `$AGORA_HOME/.agora/inactive-repos` | empty | Repos where agora is fully dormant (one path per line) |
| `AGORA_BOARD_INTERVAL` | `5` | Dashboard poll interval (seconds) |
| `AGORA_HEALTH_INTERVAL` | `300` | Dispatcher health patrol (seconds) |
| `AGORA_VERIFY_TIMEOUT` | `300` | Timeout per task/integration verify run (seconds); expiry kills the process group |
| `enforce` | on | Global gate switch (root only, audited) |
| `$AGORA_HOME/.agora/email.json` | off | Stuck-task email alerts (`{"to","command"}`) |

## Safety Model

Honest by design — what's machine-checked vs what's trusted:

**Enforced** (cannot be bypassed):
- Git-repo writes only within task worktrees (plugin gate + session tokens)
- Verify must actually run and pass, executed by the service
- Role matrix: workers can't cancel, modify verify, or toggle enforce
- Every state change lands in the append-only audit ledger

**Accepted risks** (single-machine trust model):
- A worker using raw `bash` can bypass the write gate (audit is the backstop)
- Token theft within the same user account
- The review anchor is a process protocol, not an anti-cheat: the worker writes it, the service reads it. The verify gate is the real quality bar; review adds discipline, not security

## FAQ

**How do I update?** `git pull && ./install.sh` — the plugin and service payloads are copies, so re-running the installer is required after pulling.

**How do I uninstall?**
```bash
rm -rf ~/.config/opencode/plugins/agora ~/.config/opencode/skills/agora-dispatch \
       ~/.config/opencode/skills/agora-ops ~/.local/bin/agora ~/.local/bin/agora-board \
       ~/plaza ~/.agora-home
# then remove the "agora" entry from the mcp section of ~/.config/opencode/opencode.json
```

(`~/plaza` is the default `AGORA_HOME` — if you configured a custom one, remove that directory instead.)

**Does it work without a review-loop?** Yes — verify still runs and gates everything; tasks finish at `done` and you merge manually. Implement the [open standard](docs/review-loop.md) to unlock auto-merge.

**Where does my code go?** Nowhere. agorad binds to localhost, workers are local opencode sessions, there is no telemetry, no cloud, no agora API keys. Whatever models your opencode uses, agora orchestrates them.

**Platforms?** Linux (verified). macOS: the code paths are in place (the installer falls back to `nohup` where `setsid` is absent) but not yet verified on a real Mac. Windows: run inside WSL.

## License

MIT
