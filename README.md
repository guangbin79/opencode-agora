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

> **Super Agent pillars**: this repo is the dispatch pillar (Router). The other two: [silicon-memory](https://github.com/guangbin79/silicon-memory) (memory/state separation) and [coding-gates](https://github.com/guangbin79/coding-gates) (quality supervision gates). Together they form the super agent implementation.

## Quick Start

Prerequisites: [opencode](https://opencode.ai) v2 (`curl -fsSL https://opencode.ai/v2/install | bash`), [bun](https://bun.sh) (`curl -fsSL https://bun.sh/install | bash`), `git`, `python3` ≥ 3.10 (stdlib only — zero pip dependencies).

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

From PyPI instead — equivalent install, no clone needed (the git route above stays as the source-code form):

```bash
pipx install opencode-agora
agora-install
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
Human ──▶ Main session (skills: agora-dispatch / agora-dispatch-gate / agora-ops)
              │  MCP (16 tools)
              ▼
         agorad — Python stdlib service on localhost
         state authority · verify gate · auto-merge · message routing
         crash recovery · audit ledger
              │
         Per-task git worktrees (enforced isolation)
```

Task lifecycle: `create → dispatch → worker codes → verify (service-run) → auto-merge (ff-only) → closed`

After install, your session gains three skills (agora-dispatch / agora-dispatch-gate / agora-ops) and 16 MCP tools. There is no background planner: the model you talk to **is** the orchestrator — task creation, `after:` chains, cancels, escalation replies are all model decisions from conversation context. agorad stays the state authority: verify executor, merge gate, message router, audit ledger.

Wiki pages under `$AGORA_HOME/wiki/` (everything outside the service-owned `tasks/` prefix) are writable through `wiki_write`/`wiki_read`. The default write is a full-page overwrite; `mode: "append"` is a server-side atomic append (read-concat-write inside the storage write lock, a missing trailing newline is added first), and `wiki_read` returns the page text plus its `hash` (sha256) — pass that back as `expected_hash` for a compare-and-swap guard: on mismatch the write is rejected with 409 and the current hash, page untouched (F180). Client-side read-modify-write races (the F106 incident shape) are closed on the server side.

## Two Quality Gates

**Verify gate** — the non-negotiable one. The verify command is registered at task creation (`task_create(verify_cmd="pytest -q")`, or `verify_pending: true` to defer). Worker-uplinked verify proposals (post kind=verify) pass a mechanical preflight before they are even persisted — the F99 hygiene rules (static checks + /bin/sh -n syntax) run transport-side and a dirty proposal is bounced straight back to the worker, never reaching the dispatcher (F175). The **service runs it in the task worktree**, then trial-merges into a detached integration worktree and runs it again — the integration run carries `AGORA_INTEGRATION_BASE` = this round's integration baseline (managed local tip / external base / legacy baseline head, whichever applies) injected as an `export` prefix that binds the whole command chain, with the baseline also recorded in the artifact metadata (`extra.integration_base`); the task-environment run and the base-red probe get no such injection (F123). Only two green runs advance the target branch; integration failures and test side effects never touch your main checkout. Before the task-environment run, a base-red probe (F107) runs the same command once in a detached worktree at the task's start base: red there proves the command can tell the task's work from the baseline (a FAIL_TO_PASS discipline); green-with-no-exemption is a hard reject back to the worker — switch to a discriminating command, or have the dispatcher exempt the task at creation (`verify_base_exempt: true`; `quick` tasks are auto-exempt; workers cannot self-exempt). The probe runs once per task: its result is cached on the task row (keyed by command + base) and survives reopens; a probe command that dies at base (exit 126/127, missing file — the canonical "task creates its own test script" shape) counts as red and passes, with a genuinely dead command still caught by the task-environment run's dead-command classification; a probe that hits the hard ceiling parks the task in `done` and routes to the dispatcher — no worker bounce, no fail-streak. Fresh checkouts come pre-seeded: submodules recorded in the target commit are populated from the local main checkout — no network, no credentials; pins the reference lacks are skipped and reported, never fetched. git-lfs-tracked files are materialized from the shared local LFS object store the same way (a pointer-only file is the symptom of a missing local object, which is reported, never fetched). A successful merge is the delivery itself — local authority: the service never pushes `origin` and never deletes remote task branches; syncing origin and cleaning up remote branches is the dispatcher's own job (F109, zero network). One hard requirement: your main checkout must be clean (no uncommitted or untracked changes) before the final push, otherwise the task reopens with a cleanup hint. The gate first idempotently writes the `$AGORA_HOME/.agora/ignore.roster` roster (one gitignore pattern per line) into the repo's `.git/info/exclude`; rostered paths don't count as untracked dirt. Root-level untracked hidden directories are auto-exempted: when every blocker is an untracked dot-directory at the repo root (`?? .dir/`), the gate appends those directories to the roster, re-applies it, retries the clean check in the same run, and lists them in the merged notification; mixed dirt (tracked changes, non-hidden paths, nested new files) still reopens unchanged. Data-layer churn gets its own exemption (`clean-ignore`): declare paths in the repo-root `.agora/clean-ignore` (one gitignore-style entry per line) and/or the deployment env `AGORA_CLEAN_IGNORE` (comma-separated); entries match by root-anchored directory prefix, and changes on declared paths — tracked or untracked — no longer block the gate. Division of labor: the roster hides *untracked* paths from `git status` via `info/exclude`; clean-ignore subtracts *declared paths themselves* (tracked changes included) from the clean check — keep task deliverables out of declared paths, or the gate will not see you touch them. Each run has two caps: a soft top (300 seconds by default) that only pings the dispatcher once — still running, no action needed — and a hard top (4× soft by default) that kills the process group. A hard-top kill (rc=124) routes to the dispatcher with the output tail: no worker retry loop, no fail streak, the task stays in done. Red = the worker gets the output and retries. Three consecutive failures = blocked + escalation ping. The worker cannot skip it, fake it, or report results itself. The gate is pooled per repository (F112): same-repo tasks serialize (the delivery lock was already repo-scoped), cross-repo tasks run gates in parallel — one repo's long verify no longer stalls every other lane. On top of the lanes, every gate run holds a cross-process resource lock (F116): a per-repo lock stored in the repo's git common dir (mutual exclusion across agorad instances and any other flock-honoring gate/checker process on the same repo; the kernel releases it if the holder crashes) plus a host-wide slot gate under `$AGORA_HOME` that caps how many different repos run gates at once (`AGORA_GATE_MAX_CONCURRENT`, a positive integer, default 1; bad values fail fast at startup; same-repo concurrency stays fixed at 1 — the knob only widens cross-repo parallelism). Queueing tasks stay `done`, surface `gate_resource.waiting` on the task row and board full view, and emit `gate_resource_wait/acquire/release` events; time spent queueing never counts against verify budgets or the gate watchdog — the budget starts when the lock is granted. Optional gate review (F114): `task_create(review_cmd=...)` registers a review command (dispatcher/root only, same hygiene gate as verify_cmd); after verify goes green the service runs it in the env itself (injecting `AGORA_REVIEW_BASE` = the task's start base via an `export` prefix that binds the whole command chain — F123; a `VAR=x cmd` prefix assignment would only bind the first command of a `cd x && pytest` chain) — a non-zero exit bounces back to the worker on an independent streak (`review_fail_streak`; three disagreements block for human adjudication of code-vs-reviewer). While registered, the worker-side anchor declaration is skipped entirely — the review "green" becomes a service record instead of a worker claim. Command hygiene is checked statically at registration (F99): the command text is lexed without execution — an absolute `cd`/`pushd`, or an absolute path (including `$HOME`/`~` expansion) landing inside the repo outside dependency dirs (`venv/`, `node_modules/`, …) in a bare token or `VAR=` assignment, is rejected with a fix hint; `--flag=<absolute-path>` option-value forms are not detected, so keep option values relative too. Paths under `~/.config` and friends only warn. Relative paths, absolute toolchains outside the repo, cross-repo references and dependency dirs pass. Commands already registered before the gate run unchanged — `doctor` audits them read-only (`verify_hygiene_findings`) instead of rewriting history, and the quick lane filters escape commands out of the history vote. Registration also pre-checks shell syntax (F123): the command text is parsed by `/bin/sh -n -c` — never executed, same dialect as the production runner (`shell=True`) and the sole syntax authority — so legal `&&`/`||`/`|`/`;`, relative `cd` and multi-line commands pass, while unclosed quotes or `if/then/fi` structural errors are rejected before anything is written (no task-row mutation, no fail streak); a heredoc implicitly terminated by EOF passes exactly as it executes in production, and if the prechecker itself is unavailable (`/bin/sh` missing, timeout) registration fails closed instead of passing silently. The quick lane's history vote filters syntax-bad commands out too. Every gate execution (base probe, verify, gate review, integration verify) persists its full untruncated output plus metadata as an artifact under `$AGORA_HOME/state/gate-artifacts/<task>/<timestamp>-<phase>-<seq>/` (F122): `output.log` holds the complete combined stdout+stderr, `meta.json` the phase, command, rc, duration, byte count and failure kind; the task row carries the latest artifact as `gate_artifact`, every write emits a `gate_artifact_written` event, and failure notifications append an `artifact=<path>` line pointing at the evidence. Artifacts are pruned per task to the newest `AGORA_GATE_ARTIFACT_KEEP` (a positive integer, default 50; bad values fail fast at startup), stored `0700`/`0600`. Failures are mechanically classified: `content_failed` (real red — quality streak as before), `overrun` (rc=124 hard-top kill) and `command_dead` (exit 126/127, or exit 2 with a missing-file message) keep their existing park-in-done, route-to-dispatcher, no-streak semantics; **signal** deaths (negative rc from subprocess, or rc 129–165 — SIGKILL/SIGSEGV/SIGABRT, the concurrent oomd shape) are mechanical/infrastructure failures, not quality failures: the task parks in `done`, the fail streak is untouched, the dispatcher is woken via the `verify-infra` escalation channel, and the worker only gets a wait note. A parked signal failure arms a grace timer (`AGORA_VERIFY_INFRA_GRACE`, default 1800 seconds; bad values fail fast at startup): if the dispatcher takes no action before it expires — no reverify/dispatch/cancel, no worker heartbeat, no new gate entry — the service itself requeues the task through the gate exactly once (audit `verify_infra_backstop`, actor=health; `awaiting_external` tasks never trigger it), and a second signal failure after that backstop requeue blocks the task for good (`blocked_reason=verify_infra_backstop_exhausted`); any owner action inside the window cancels the timer. `runner_exception` (the executor itself raised) is recorded as an artifact and follows the existing in-gate exception path — never disguised as a content failure. Notifications are built for truncation (F125): any long or failure-marked injection leads with stable reference lines — `home=` / `msg=state/messages/<id>.md` / `page=wiki/tasks/<tid>.md` / `artifact=` (when one exists) — right after the `[agora]` header and before any body text, so pointers to the full content survive even when the outer display clips the tail; process-note digests head with `home`/`msgs_dir`/`ids` followed by `#<id>`-prefixed 120-char summaries; message bodies and `check_mail` output stay byte-identical, and short acks carry no reference noise. The task page carries a status projection section (`gate_resource` / `gate_artifact` with `path=`/`output=` relative to `$AGORA_HOME`), and the board root list shows `art:<phase>:<rc>` in the note column (escalate > wait > artifact priority).

**Review gate** — an open standard, bring your own reviewer. The contract is one anchor file:

- The last-reviewed anchor is checked at `done` — missing, stale, not green, or a chain-root (`.review-base`) that differs from the task's start base (F111) rejects `done` back to the worker — and **re-checked immediately before merge** (closes the "reviewed, then sneaked a commit" window; a failed re-check auto-reopens the task, it never parks). A done with zero new commits over the target is rejected the same way — unless it is a valid delivery already landed there (env HEAD advanced past the task's start base, or the previously accepted commit is reachable on the target); a true zero-work done (HEAD still at the start base) is rejected — the worker reports no-op via post(kind=question) with a no-op: first line and the service auto-archives it (zero-commit check + prefix match, programmatic cancel, declaration kept on record for appeal — F174).
- Anchor green + verify green = automatic ff-only merge — a behind task branch is rebased onto the target in-gate first (clean sync self-heals; only sync conflicts reopen).
- Phantom-merge guards — `merged` is booked only when the target branch actually contains the delivered commit: an empty rebase (no new commits on the env boot branch) or an idempotent-ff false success reopens the task instead of silently losing the delivery.
- No `.review-loop/` directory? `done` is rejected with instructions to add one — auto-merge requires both gates. Autonomy is earned, not assumed.

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
[ -s .review-loop/.review-base ] || printf '%s\n' "$base" > .review-loop/.review-base  # F111：链根（首轮起点，续轮沿旧）
```

Full spec — contract details, worker protocol, threat model, idle auto-trigger design notes: [docs/review-loop.md](docs/review-loop.md) (Chinese).

## Delivery Semantics

Where your commits land depends on the delivery mode and where authority lives. Every task row carries a machine-readable `delivery` projection (`mode/status/commit/base/attempt/local_status/error`).

- **Managed tasks (the default) — local authority, CAS ff-only (F108; zero network as of F109).** The pinned local branch tip is the authority: the env is based on the local tip (no fetch even with `origin` present); the gate integrates at that tip, replays with an in-gate detached rebase when needed, re-verifies, then advances the local branch via compare-and-swap fast-forward under the repo-level delivery lock — it never pushes `origin` and never deletes remote task branches; syncing origin is the dispatcher's own job. A clean non-ff (someone else advanced the local branch) or a lock timeout consumes one of **at most three total attempts**; exhaustion is a delivery failure (`retry_exhausted`). Content conflicts on rebase and dirty/occupied targets are terminal delivery failures too (`local_target_dirty`/`local_target_busy`): the task goes `blocked` with `delivery.status=failed` and a machine-readable error, the authoritative target stays untouched, the worker is not bounced, and fail-streak is not counted — delivery failure is not a quality verdict. Startup recovery converges crash leftovers (probes are local-only, zero network); a failed delivery never auto-retries (redispatch resets the projection).
- **`external_branch` tasks — external dispatcher authority.** For externally managed dev branches, agora never pushes `origin` and never merges the default branch: the gate verifies privately on the pinned branch, then parks the task at `done` + `awaiting_external` (commit/base/attempt recorded, env and credentials kept). The external dispatcher acknowledges via `task_deliver`: `delivered` closes the task, `non_ff` with the fresh external tip first gets a local ancestry precheck (F110 — requeue-and-reverify abolished): not an ancestor of the handoff means an immediate `failed/diverged` terminal state (no gate queue, no wasted integration verify; already an ancestor is a protocol contradiction rejected with zero mutation; an unresolvable base yields `external_base`) — a real external fork is handled by redispatching with a fresh env at the new tip or by humans, `failed` blocks with a machine-readable error. An acknowledgement carrying a stale handoff commit is rejected with zero state mutation.
- **Delivery terminal-state watch (F110).** External handoffs record `handoff_at`: an `awaiting_external` stay beyond the age threshold (30 min by default) escalates to the dispatcher through health (notification channel plus optional email, with repeat backoff). Managed deliveries record `delivered_at`: merged notifications add “origin not synced, please push yourself” when `ahead>0`, and health reminds again when delivery stays unpublished past the age threshold (1 h by default) — merged is not pushed; origin sync belongs to the dispatcher. Patch equivalence is two-layer (F121): a delivery counts as published when the commit's whole patch-id is in the origin window, or — failing that — when every hunk of the delivered patch is (split/mixed publishing across window commits reconciled at hunk level); hunk sets come from the window only, coverage must be total, and file paths are part of the hash, so identical hunks in unrelated files never match.
- **Multi-instance safety.** Several agora instances may work the same repository: each owns its task worktrees (instance-stamped names; cleanup deregisters by exact path and never removes another instance's registered worktree), and the repo-level delivery lock serializes final advances, so concurrent deliveries produce a linear first-parent history. End-to-end proof: `python3 tests/e2e/multi_instance_delivery.py` (two real service instances, nine scenarios). **Mixing old (pre-F96) and new agora instances on the same repository is not allowed** — every same-repo writer must be upgraded to the lock/instance protocol.

Deployment note: the plugin and service payloads are copies — after upgrading the agora repo you must rerun `./install.sh` **and restart the service**, or the running daemon keeps the old delivery code.

## Highlights

- **Verified autonomy** — auto-merge only after service-run tests pass and the review anchor is green, with the anchor re-verified at merge time
- **Enforced isolation** — plugin-level gate blocks git-repo writes without task credentials; workers can't touch your main checkout.
- **Cross-repo and nested orchestration** — `after:` dependency chains; workers can create and manage their own subtasks, and a parent can finish only after child results return; hard limits on nesting (3) and concurrency (6); leaf workers cannot dispatch subtasks (F41)
- **Controlled waiting on running tasks** — division of labor with `after:`: `after` expresses dependencies at creation time (blocks dispatch), while `task_wait(task, wait_on)` registers a controlled wait on an already-running task that has discovered it depends on a later task's merged result (F124). The task stays `running` with a `waiting_on` field — no new lifecycle state; dispatcher/root only (workers ask via `post(kind=question)`); the dependency must exist, differ from the task, and not form a cycle with existing `after`/`waiting_on` edges; registering against an already-terminal dependency resolves immediately. While waiting, health stall judgment is suspended (no stalled escalation, no self-check nudge, not a stale/doctor candidate — waiting time never fakes `last_seen`); once the dependency reaches `merged`/`closed`/`cancelled`/`blocked`, the health tick clears the wait, audits `wait_resolved`, and notifies both the worker (success → continue; cancelled/blocked → needs adjudication) and the dispatcher; `task_wait(task, wait_on=null)` clears manually for human recovery
- **Role-separated credentials** — root / dispatcher / worker session tokens; workers can cancel or register verify only for their own subtasks, never manage parents or other tasks, or read the board
- **Root token is daemon-bound** — issuance targets the daemon identity (`sid="root"`), never a client session sid, and root calls carrying `X-Agora-Sid` never rebind the token: the live session only updates `main_sid` while task attribution records the real caller sid (chain edges judge by the recorded value). Legacy ledger entries that still bind a root token to a session sid are migrated once at startup back to `sid="root"` (old sid kept in lineage, idempotent, in-flight worker tokens untouched); startup audit remains as a residue watchdog — never revokes, since the root token is often the sole governance credential
- **Crash recovery** — `kill -9` the service at any moment; append-only state + worker re-injection with the same credentials on restart
- **Real-time accountability** — escalation pings to the main session, append-only JSONL audit for every state change, health patrol for dead dispatchers
- **Same-repo peer coordination** — repo_board exposes in-flight tasks in the same repo plus typed peer notes (claim / dead-end / confirm) with claim-collision receipts — parallel workers coordinate through the ledger without reading each other context (F172/F173)
- **PR-workflow friendly** — `task_create(target_branch="feature/x")` merges into a feature branch instead of main
- **Server-side req numbering** — `task_create(req_id="F34")` registers and de-duplicates feature numbers (seeded from `docs/reqs/`, duplicate issuance rejected). Server allocation is the authoritative number source; workers take the number from the task page. Bypassing is not prevented — a convention, not a gate
- **Quick micro-tasks** — a light lane for small single-repo changes: `task_create(quick=true)` needs no verify_cmd; the verify command is resolved dynamically from ledger history (the unique most-frequent command among the repo's merged non-quick tasks, count ≥ 2; insufficient evidence rejects with the candidates — just dispatch the task normally, there is no manual table path); over-envelope diffs are measured mechanically at the gate and fail like a verify red; the worker BOOT drops three informational lines
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
| `AGORA_HOME` | `~/.opencode-agora-plaza` | Ledger home (state, messages, audit, wiki) |
| `$AGORA_HOME/.agora/inactive-repos` | empty | Repos where agora is fully dormant (one path per line) |
| `AGORA_BOARD_INTERVAL` | `5` | Dashboard poll interval (seconds) |
| `AGORA_HEALTH_INTERVAL` | `300` | Dispatcher health patrol (seconds) |
| `AGORA_VERIFY_TIMEOUT` | `300` | Soft top per task/integration verify run (seconds): notify the dispatcher on expiry, no kill |
| `AGORA_VERIFY_CEILING` | `4× soft top` | Hard top (seconds): expiry kills the verify process group; overrun routes to the dispatcher (no worker retry, task stays done) |
| `AGORA_QUICK_MAX_FILES` | `3` | Quick-task envelope: cumulative changed-files cap, measured mechanically before verify; over the cap the task fails like a verify red |
| `AGORA_QUICK_MAX_LINES` | `30` | Quick-task envelope: cumulative added+deleted lines cap, same red path |
| `AGORA_GATE_ARTIFACT_KEEP` | `50` | Gate execution artifacts kept per task (newest N dirs under `$AGORA_HOME/state/gate-artifacts/<task>/`); positive integer, bad values fail fast |
| `AGORA_VERIFY_INFRA_GRACE` | `1800` | Grace window (seconds) for a signal-failure task parked in `done` (F130): if the dispatcher takes no action before it expires, the service requeues it through the gate exactly once; a second signal failure after that backstop blocks the task. Positive number, bad values fail fast |
| `$AGORA_HOME/.agora/agorad.env` | — | Knob defaults file (`KEY=VALUE`, `#` comments); explicit process env takes precedence, survives daemon restarts |
| `$AGORA_HOME/.agora/verify-profiles.json` | empty | Per-project verify budget profiles (F126): `{"<absolute repo root>": {"timeout": seconds, "note": "optional, ≤200 chars"}}`; priority: explicit `task_create.timeout` > profile > `AGORA_VERIFY_TIMEOUT`; invalid JSON/schema fails fast at startup (error names the path/key/field); takes effect on restart |
| `enforce` | on | Global gate switch (root only, audited) |
| `$AGORA_HOME/.agora/email.json` | off | Stuck-task email alerts (`{"to","command"}`) |

Note: `install.sh` seeds `AGORA_VERIFY_TIMEOUT=3600` into `$AGORA_HOME/.agora/agorad.env` — the code/README default is 300 (soft top notifies, never kills); the installer widens it because long-build repos measurably outgrow 300s.

### Per-project verify budget profiles (F126)

The service-level `AGORA_VERIFY_TIMEOUT` is a global default — it cannot express "HXNativeApp check ASAN cold build takes 35-60 min while a light repo's pytest finishes in seconds". Profiles give each project repo its own default budget, so a long-check task no longer inherits the global budget when the dispatcher omits `task_create.timeout`:

```json
{
  "/home/guangbin/Documents/HXAppPlatform/HXNativeApp": {
    "timeout": 5400,
    "note": "check ASAN --with-tests cold build 35-60min"
  }
}
```

- Priority: explicit task `timeout` > project profile > `AGORA_VERIFY_TIMEOUT` global default; the effective budget and its source (task/profile/default) are recorded on the task row and visible in task page/detail/board full_view/doctor; the Gate hard top stays 4× the effective budget (F51 semantics unchanged)
- Profiles are **server-side config**: workers and in-repo config cannot influence acceptance budgets; only new tasks are affected — existing tasks are never rewritten by later profile edits
- Keys are absolute repo roots (realpath-normalized; symlinks and trailing slashes are equivalent); duplicate keys after normalization, non-positive-integer timeouts, notes over 200 chars, or extra fields fail fast at startup
- Calibration flow (manual, from F122 gate artifacts): ① check recent task artifacts' `duration_s`; ② set the profile timeout to 1.5-2× the stable cold-build/verify duration; ③ edit `verify-profiles.json` and restart agorad to apply

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
       ~/.config/opencode/skills/agora-dispatch-gate \
       ~/.config/opencode/skills/agora-ops ~/.local/bin/agora ~/.local/bin/agora-board \
       ~/.opencode-agora-plaza ~/.agora-home
# then remove the "agora" entry from the mcp section of ~/.config/opencode/opencode.json
```

(`~/.opencode-agora-plaza` is the default `AGORA_HOME` — if you configured a custom one, remove that directory instead.)

**Does it work without a review-loop?** Verify still gates everything, but `done` needs an anchor: without one, `done` is bounced back to the worker with instructions. Implement the [open standard](docs/review-loop.md) to unlock auto-merge.

**Where does my code go?** Nowhere. agorad binds to localhost, workers are local opencode sessions, there is no telemetry, no cloud, no agora API keys. Whatever models your opencode uses, agora orchestrates them.

**Platforms?** Linux (verified). macOS: the code paths are in place (the installer falls back to `nohup` where `setsid` is absent) but not yet verified on a real Mac. Windows: run inside WSL.

## License

MIT
