# 🥕 prodder

### btop for your AI coding agents — see which ones stalled, and nudge them back to work.

[![PyPI](https://img.shields.io/pypi/v/prodder)](https://pypi.org/project/prodder/)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![CI](https://github.com/sysderm/prodder/actions/workflows/ci.yml/badge.svg)](https://github.com/sysderm/prodder/actions/workflows/ci.yml)

Prodder is a terminal + web dashboard that watches which projects are actually
producing files — locally and on remote hosts — finds the claude/codex/aider/gemini
CLI sessions working on them, flags the ones that have silently stalled, and
*prods them back to work* with a keystroke (or automatically). Born from the plague
of coming back to six terminals where every agent is sitting idle waiting for nothing.

> **Try it in 10 seconds, nothing real touched:**
> `pipx install prodder && prodder --demo`

<!-- Demo GIF: record a ~15s capture of `prodder --demo`, save as docs/demo.gif,
     and uncomment the next line for an animated hero shot at the top of the page. -->
<!-- ![prodder dashboard](docs/demo.gif) -->

```
 prodtop  local ● server1 ●   agents: 13 (4 stalled)              14:03:22
 PROJECT        HOST  MODE   AGENT          15m  1h  24h   3d  ACTIVITY 24h
 my-game        local PROD   ●codex 5s        8  45 1088 2100  ▂▅█▃▁...
 tax-papers     local leave  ⚠codex 28m       0   0    0 1302
 my-api         srv1  PROD   ⚠claude 2h       0   3   40  200  ▁▃▁...
```

## What it does

- **Project activity**: auto-discovers every folder under your configured
  roots with files written in the last N days (default 3). Shows counts at
  15m/1h/24h/3d horizons, a 24-hour sparkline, and the newest file. Remote
  hosts are scanned over ssh with a single `find` round-trip.
- **Agent tracking**: finds coding-agent CLI processes, maps them to projects
  via their working directory, and measures idleness from real terminal
  output (tmux activity or tty mtime). `●` running · `⚠` stalled · `⊗`
  detached (window gone) · `⊘`/`leave` marked leave-alone · `▹` a plain
  terminal window with no known agent.
- **Nothing hides**: with `show_all_terminals` (default on), *every* open
  Terminal.app/iTerm2 window on the local Mac is listed — not just the ones
  running a recognised CLI — so an agent whose process prodder doesn't know
  can't sit unseen in a window it never showed. These extra rows are display-
  only: never auto-prodded and never counted as stalled, but you can still
  **Type…** into one by hand.
- **Prodding**: types your nudge (default `continue`) into the agent's
  terminal and *actually submits it* — text first, then a real carriage
  return after a pause, because TUI composers treat a text+newline burst as
  a paste and leave it unsubmitted. Works via tmux `send-keys` (local and
  over ssh) or AppleScript into Terminal.app/iTerm2, including sessions
  wrapped in asciinema/script recorders.
- **Approval prompts**: optionally answers an agent's "Would you like to run
  this command? 1. Yes, proceed" menu with the default Yes (never the
  "don't ask again" variant).
- **Close & save**: `x` captures the session's last screen, recovers the
  resume command from the CLI's own session store (`claude --resume <id>`,
  `codex resume <uuid>`), logs everything to `closed-agents.md`, then kills
  the process tree.
- **Reopen the dead**: `o` takes an agent whose terminal window is gone —
  they can never receive input again — saves its closing statement, kills
  the zombie, and reopens the same conversation in a fresh terminal window.
- **Web chats too**: Chrome tabs with claude.ai / chatgpt.com / gemini open
  appear as rows; `p` focuses the tab and submits the nudge into the page's
  composer (manual-only — a web chat has no reliable "stalled" signal).
- **Per-project modes**: the MODE column shows `PROD` vs `leave`; toggle
  with `i`/`a`. Policies are keyed by project path and survive restarts,
  reopened sessions, and new agents in the same project.
- **Workbench**: create a task with acceptance criteria, move it through
  *Working* → *Needs you* → *Ready to review*, and attach provider-neutral
  lifecycle events. Verify records Git evidence and can run only explicitly
  configured test/lint commands.
- **Answer from your phone**: when a quiet agent has a "1/2/3" decision menu
  on screen, prodtop sends the menu to **Telegram** with answer buttons —
  tap `1`, `2`, `esc`, or reply with free text, and it is typed into that
  terminal. Optional **WhatsApp** pings (outbound-only, via callmebot.com).
  While a question is on your phone, auto-approve holds off
  (`remote_timeout`). Prompts matching `never_approve` patterns (`rm -rf`,
  `--force`, `sudo`, ...) are *never* auto-approved — those always wait for
  a human. Setup is one paste and two taps: `./prodtop.py --setup` opens
  the right chats, validates the token, auto-discovers your chat id, writes
  the config, and sends test messages. (`--test-remote` re-checks later.)

## Install & run

Python 3.11+ stdlib only — no dependencies.

```sh
# Fastest: install + risk-free demo in one line (no clone, no config)
pipx install git+https://github.com/sysderm/prodder && prodder --demo

# Or from a clone (also lets you edit the code):
git clone https://github.com/sysderm/prodder && cd prodder
python3 prodtop.py --demo                # try it risk-free: a simulated fleet,
                                         # no config, no ssh, nothing real touched
pipx install .                           # install the `prodder` command from the clone
cp prodtop.example.toml prodtop.toml     # then edit roots/hosts
prodder                                  # web dashboard (opens in your browser)
prodder --tui                            # classic curses interface instead
prodder --once                           # one plain-text snapshot
prodder --setup                          # guided phone-answering setup (Telegram/WhatsApp)
prodder --emit-event PlanProposed --task-id <task-id> \
  --event-source codex-hook --event-payload '{"steps": 3}'
```

`--demo` is the safe first step — it needs no config and works straight from a
bare clone, so start there. `pipx` is recommended because most modern Pythons
(Homebrew, Debian/Ubuntu, Arch) mark their site-packages "externally managed"
(PEP 668) and refuse a plain `pip install` — see the note below.

The default interface is a dark, glassy web dashboard served on
`127.0.0.1:8737` (`[web] port` in the config, `--port` to override,
`--no-browser` to just serve). Everything the TUI does is a click: the
PROD/leave pill toggles the policy, rows expand into recent files and
per-agent Prod / Type / Nudge / Close / Reopen buttons, the header has the
auto-prod switch, host health pills, sort, and the message log. Actions are
same-origin-only and the server never listens beyond localhost. Every terminal
action is bound to the agent PID and scanned session identity as well as its tty,
so an old browser click is refused rather than reaching a reused terminal. The
dashboard cannot be embedded by another site (anti-clickjacking headers), and
only one engine may run for a configuration directory at a time.

Most modern Python installs (Homebrew, Debian/Ubuntu, and Arch system Python)
refuse `pip install` into their own site-packages (PEP 668, "externally
managed") — the install then silently never happens and `prodder` is not on
PATH. Prefer `pipx install .` (isolated, and it handles PATH), or, if you must
use pip, `python3 -m pip install --break-system-packages .`. Needs Python 3.11+
(for the stdlib `tomllib`); on an older interpreter `./prodtop.py` exits with a
clear message telling you so.

### Remote hosts, including over Tailscale

A host's `ssh` value is anything `ssh(1)` accepts — a `~/.ssh/config` alias,
`user@host`, or a Tailscale MagicDNS name (`root@myvps` via Tailscale SSH,
`me@somelaptop` via macOS Remote Login carried over the tailnet). Remote Macs
additionally need `os = "mac"` in their host block: the scan then uses the BSD
toolchain, and agents sitting in Terminal.app/iTerm2 windows (not tmux) are
prodded by running the same AppleScript on that Mac over ssh. The first such
prod/capture pops a one-time Automation consent dialog **on the remote Mac's
own screen** ("sshd wants to control Terminal") — click Allow there once, or
pre-enable it under System Settings → Privacy & Security → Automation.
Until then, remote-Mac prods fail with a timeout error in the status line.
Sleeping laptops simply show as offline (◌) and are retried each interval.

For a checkout without installing, `./prodtop.py` remains supported. On macOS,
double-click `Prodder.app`: it starts the engine headless (log:
`~/Library/Logs/prodder.log`) and opens the dashboard — reuses the running
server if there is one; the ⏻ button in the dashboard stops it. (Do not
double-click `prodder.command`: Terminal runs a `.command` by *typing* its
path into a fresh interactive shell, so anything in shell startup that drains
pending tty input — e.g. an exec'd asciinema session recorder — silently
discards the command and nothing launches. `./prodder.command` from an
existing shell still works.)

TUI keys (`--tui`): `j`/`k` select · `p` prod · `t` type · `n` nudge ·
`i` leave alone · `a` re-arm · `x` close+save · `o` reopen detached ·
`s` sort · `r` rescan · `q` quit.

### Running on a headless Linux box

To run the engine itself on a server (agents in tmux, no GUI/browser on the
host), start it without opening a browser and reach the localhost-only
dashboard through an ssh tunnel:

```sh
# on the server (inside tmux/ssh):
prodder --no-browser                     # or ./prodtop.py --no-browser
# from your laptop:
ssh -L 8737:127.0.0.1:8737 you@server    # then open http://127.0.0.1:8737
```

The dashboard never listens beyond loopback, so the tunnel (not a public bind)
is how you reach it. `--tui` is the no-browser alternative for driving it
directly in the terminal. Resolving each agent's project needs `lsof` **or**
`/proc` — minimal images without `lsof` still work via `/proc`.

### Workbench: verified agent work, not just activity

The workbench is local-first state stored in `prodder-workspace.sqlite3`
(owner-only). Create a task from the dashboard, write the acceptance criteria
that make it genuinely done, then use **Ready to review** and **Verify** before
marking it done. Verify always captures Git branch, diff, and dirty/clean
evidence. It never guesses a repository's test command; configure vetted checks
under `[workbench]` first:

```toml
[workbench]
check_commands = ["pytest -q", "npm run lint", "npm test"]
check_timeout = 300
```

Commands run without a shell, only from a task within a configured local root,
and only when you press Verify. Remote projects can still carry tasks and
provider events, but verification remains a deliberate local action for now.

Provider adapters, hooks, or CI bridges can record standard events through the
authenticated local `POST /api/events` endpoint or the `--emit-event` command:
`TaskDefined`, `PlanProposed`, `ToolStarted`, `ApprovalNeeded`, `FileChanged`,
`CheckPassed`, `PreviewReady`, `AgentBlocked`, `AgentCompleted`, `CommitCreated`,
`PullRequestOpened`, `CIPassed`, `DeployReady`, `Deployed`, and `Rollback`.
This is the stable integration seam; terminal-screen detection remains the
fallback for tools that cannot emit structured events.

## Nudge experiment — which prods actually work

Not every "continue!" helps, and a badly-timed one sends an agent off inventing
work. prodder treats the nudge phrasing as an experiment:

- **It rotates a pool of phrasings** (`nudge_pool`) with an epsilon-greedy
  bandit — mostly sending whatever has best resumed real work, occasionally
  exploring the rest. A per-project custom nudge (`n` in the TUI) overrides it.
- **It scores each prod's outcome.** A few minutes later (`outcome_window`) it
  checks whether the project produced files — a deliberately narrow activity
  signal, **not proof of correct or useful code**. Outcomes: *file activity* /
  *stayed stuck* / *agent left*. These land
  in `prodder-events.jsonl` and aggregate into `prodder-stats.json`.
- **The Nudge Lab** (🥕 in the dashboard header) ranks phrasings by the share of
  prods that led to real output, with sample sizes. 👍/👎 on a recent prod
  correct an outcome by hand — that also teaches the bandit.

### Avoiding the wrong direction

The main way a prod backfires is prodding an agent that is actually **finished
or waiting for a decision** — a blunt "continue!" then makes it invent work.
Three layers guard against it:

1. **Numbered menus never get a nudge** — they're pushed to your phone for a
   real decision (existing behavior).
2. **Finished / hand-back screens get a reassess nudge, not a directive one.**
   When the screen tail matches completion or question phrasing ("task
   complete", "let me know if…", "shall I…?"), prodder sends `reassess_nudge`
   — an explicit off-ramp ("if done, summarise; if blocked, say so; otherwise
   continue") so the agent self-corrects instead of pushing forward.
3. **The bandit learns to avoid harmful phrasings**, because a prod that leads
   to *stayed stuck* / *agent left* counts against that nudge's score.

Erring toward over-detecting "done" is deliberate: a false positive only sends
the gentler reassess nudge (which still ends with "otherwise continue"); a
false negative sends the harmful blunt one.

## Caveats, honestly

- **macOS-centric for non-tmux agents.** Keystroke delivery and screen capture
  target Terminal.app/iTerm2 via AppleScript, which exists only on macOS. The
  **tmux path works anywhere** (local or over ssh). So on **Linux** the rule is
  simple: run your agents inside **tmux** and everything works — prod, capture,
  auto-prod; a non-tmux agent in gnome-terminal/xterm is still *listed* but
  can't be prodded (there's no AppleScript to type into it). Web prodding needs
  Chrome's *View → Developer → Allow JavaScript from Apple Events* and uses
  best-effort DOM selectors that chat sites may change at any time. Native
  **Windows** isn't supported; use **WSL2** (tmux + ssh work there) and run
  `prodder --no-browser`, then open the dashboard in your Windows browser.
- **A stalled agent and one awaiting your reply look identical.** With
  `auto_prod = true`, an agent politely asking a question gets "continue"
  typed at it after `idle_after` seconds. Defaults ship conservative
  (`auto_prod = false`, `approve_prompts = false`); flip them knowingly.
- **`approve_prompts = true` approves whatever command the agent proposed.**
  That is the point — and the risk. Use `protected` patterns and `leave`
  marks to fence off anything that shouldn't be touched.
- **Prodding costs money and you own the bill.** Every nudge makes a paid coding
  agent keep working — more API tokens, more time, possibly more commands run.
  With `auto_prod = true` prodder will re-nudge on its own; a stuck loop plus a
  misjudged "stalled" screen can burn tokens while you're away. `prod_cooldown`
  (default 900s) and `auto_restart_attempts` bound how often that happens, but
  they don't cap spend. **Set hard spending limits with your model provider**,
  start with the conservative defaults, and turn on `auto_prod` only once you
  trust it in your setup. prodder issues no requests to any AI provider itself —
  it types into agents you already run — so all usage and cost is yours.
- **Local-trust tool.** The dashboard listens only on `127.0.0.1` and gates
  every action behind a per-session token (`prodder-token`, written `0600`), but
  it assumes the local machine and its users are trusted. Don't run it on a host
  where you wouldn't hand another logged-in user a keyboard to your agents.
- **History is sensitive.** `closed-agents.md`, nudge events, policies, and
  dashboard tokens are written owner-only. Terminal tails are redacted for
  common credential patterns before a close is logged; configure
  `history_redact_patterns` for any internal token formats you use.

## Contributing

Development is just Python + the stdlib — no build step for the engine.

```sh
python3 prodtop.py --demo    # run the dashboard against a simulated fleet
python3 -m pytest            # run the tests (safety-critical decision logic)
./build.sh                   # (macOS) rebuild the Prodder.app menu-bar binary
```

The whole engine is the single-file `prodtop.py`. The tests in `tests/` pin the
behaviour that must never silently regress — which prompts are auto-approvable,
which are refused by `never_approve`, and the "is this screen finished?"
detection. Please keep them green (and add a case) when touching that logic.
Issues and PRs welcome.

## Disclaimer

prodder automates keystrokes into AI coding agents that can run commands and
incur usage costs. It is provided **as is, without warranty of any kind**, and
its authors are **not liable** for any resulting cost, data loss, or damage (see
the full terms in `LICENSE`). You are solely responsible for what your agents do
when prodded and for any charges they incur — use the conservative defaults, set
provider spending limits, and enable automation only where you accept the risk.

## License

Copyright © 2026 Alexander Navarini.

prodder is licensed under the **GNU Affero General Public License v3.0 or later**
(AGPL-3.0-or-later) — see [`LICENSE`](LICENSE). In short: you may use, modify,
and share it freely, but if you run a modified version — including as a network
service — you must make your source available under the same license.

**Commercial licensing:** if the AGPL's copyleft/source-sharing terms don't fit
your use (for example, embedding prodder in a closed-source or hosted commercial
product), a separate commercial license is available from the author. Open an
issue or get in touch.
