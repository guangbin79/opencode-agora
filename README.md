# opencode-agora

A multi-agent delegation layer for [opencode](https://opencode.ai) v2 — one human, many AI agents, many git repos, one machine.

You state goals; agora does the rest: workers are dispatched into per-task worktrees, their work is gated by a real verify command, and a green gate merges ff-only into your target branch automatically. Everything is recorded in an append-only ledger you can audit.

```
Human ── natural language ──▶ main session (skills: agora-dispatch / agora-ops)
                                       │  MCP (11 tools)
                                       ▼
                          agorad — single Python-stdlib service
                          state authority · verify gate & auto-merge
                          message routing · liveness & recovery · audit
                                       │
        per-task worktrees (ironclad: sibling of the repo, ../ topology preserved)
```

## Why

Four pains, four answers:

| Pain | Answer |
|---|---|
| Agents pollute the working tree | Enforced isolation: git-repo writes only inside per-task worktrees, machine-checked by the plugin gate (credentials + cwd ∈ env) |
| No orchestration across repos | Dispatch chains with ordering (B7): coupled repos merge in dependency order; unrelated repos run fully parallel |
| Dispatched agents are black boxes | Chain visibility: board / Agent View TUI, `last_seen`, escalation events, append-only audit ledger |
| No real-time channel | Messages along chain edges, persisted-first (≤5s inject when online, catch-up mailbox when offline), escalation pings to the main session within seconds |

The posture is **full autonomy with two failure tiers**: self-healing failures stay silent (visible in the report); unrecoverable ones (blocked, 3× quality failures, dispatcher death, gate-file tampering) ping the main session in real time and still land in the report.

## Install

Requirements: `python3` (stdlib only), `git`, `opencode` v2, `bun`.

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

`install.sh` is idempotent. It checks dependencies, creates `$AGORA_HOME` (default `~/plaza`), registers the MCP server, installs the plugin and the two skills, captures the root token to `.agora/root.token` (0600), and smoke-tests the service with it. Re-run after `git pull` — installed copies are copies, not links.

First-run bootstrap: run with `enforce off` while you set up, then turn it on. The repo itself is not exempt (dog food).

## Daily use

Talk to your main session; skills load by intent:

- "Have an agent fix the login bug in repoB" → `agora-dispatch` decomposes, writes narrow self-contained goals, dispatches workers, registers their verify commands.
- "How's it going?" → board five buckets (merged / in-flight / failed / blocked / cancelled), chain tree, `last_seen` to spot stalls.
- "Stop that chain" → `agora-ops` shows the cascade scope and asks you to confirm before `task_cancel`.

A standalone dashboard is one command away:

```bash
python3 board_tui.py     # Agent View: chain tree, buckets, escalation feed
                         # vi motion: j/k/g/G, Enter detail, L tail worker session,
                         # d doctor, x cancel (y/n confirm), / filter, r refresh
```

### Task lifecycle

`todo → dispatched → running → done → merged → closed`, with branches `blocked / cancelled / verify_failed / reopened`. The verify gate: worker files a verify proposal → dispatcher validates (executable + covers the goal) and registers it (`task_update`, then locked) → `done` additionally requires the worker's own review loop to be green → the service runs verify in the worktree → green merges ff-only into the target branch, runs integration verify there, and rolls back byte-exact if that fails. Three quality failures → blocked → escalation ping.

### The 11 MCP tools

`task_create · task_dispatch · task_update · task_cancel · post · check_mail · board · doctor · wiki_read · wiki_write · enforce`. There is deliberately no merge tool (the green gate is the only merge path), no delete (records are cancelled/closed, never erased), no per-repo roster beyond scope dormancy.

## Configuration

| Knob | Default | Meaning |
|---|---|---|
| `AGORA_HOME` | `~/plaza` | Ledger home (state/messages/events/wiki) |
| `$AGORA_HOME/.agora/inactive-repos` | empty | Scope roster: repos where agora is fully dormant (sessions there: plain tools pass through, `agora_*` refused). One repo-root path per line; takes effect in new sessions. Central roster — a repo cannot opt itself out |
| `AGORA_BOARD_INTERVAL` | 5s | board_tui poll interval |
| `enforce` | on | Root-only global gate switch; every change audited |
| Chain depth / concurrency quota | 5 / 8 | Hard fuses on dispatch |

## Runbook

**Service lifecycle.** agorad is lazily lifted by the plugin (setsid + single-instance flock); no systemd/cron. Port discovery via `$AGORA_HOME/.agora/service.json` (+ `AGORA_ROOT_TOKEN:` line on stdout at first boot — install captures it). `kill -9` at any time is safe: state is an append-only snapshot log, torn tails are tolerated, and in-flight chains re-inject on restart (same credential, rebound session).

**opencode service restarts.** Sessions persist; agorad probes and resumes. Note: `opencode serve` may rotate its service password on restart — messages degrade safely to the mailbox (persist-first); restart agorad to re-discover credentials. Known platform note: opencode v2.1.4 sometimes doesn't spawn the MCP server in fresh sessions (logged in the e2e troubleshooting record); a retry or new session resolves it.

**Common ops.** Stalled-looking worker → check `board` (`last_seen`), agora only auto-handles death, not stalls — cancel or redispatch (dispatching the same task again issues a fresh credential). Escalation ping → `board?task=<id>` for detail, then cancel / redispatch / leave it. Plugin stuck denying after a service outage → the deny reason carries a `resetLift` recovery hint (or restart the opencode session).

**Safety model (accepted faces).** Single machine, single user: bash direct-writes past the gate, token theft, and self-written review anchors are accepted faces — the audit ledger (three-way: events.jsonl + git history + opencode sessions) is the backstop, not real-time prevention. Role permissions are enforced server-side (root/dispatcher/worker matrix; workers can never cancel or update verify).

## Status

Three implementation plans shipped and merged: agorad core (12 tasks), access layer (9 tasks: MCP shell, plugin hook/cred/inject/lift/scope, install, T2/T3 e2e against real opencode), scope & skills (4 tasks). Test base: 182 python + 41 bun + 31 install assertions + e2e (12→13 scenarios incl. crash windows, kill -9 recovery, ordering gates). Deferred items (rotation/archival, tail-read, etc.) are friction-driven.

**[中文文档 / Chinese README](README.zh-CN.md)**

## License

Private project. All rights reserved.
