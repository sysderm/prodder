# prodder

**btop for your AI coding agents.** Prodder is a terminal dashboard that watches which
projects are actually producing files — locally and on remote hosts — finds
the claude/codex/aider/gemini CLI sessions working on them, flags the ones
that have silently stalled, and *prods them back to work* with a keystroke
(or automatically). Born from the plague of coming back to six terminals
where every agent is sitting idle waiting for nothing.

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
git clone <repo> && cd prodtop
cp prodtop.example.toml prodtop.toml   # edit roots/hosts
python3 -m pip install .               # installs the `prodder` application
prodder                                  # web dashboard (opens in your browser)
prodder --tui                           # classic curses interface instead
prodder --once                          # one plain-text snapshot
prodder --setup                         # guided phone-answering setup (Telegram/WhatsApp)
```

The default interface is a dark, glassy web dashboard served on
`127.0.0.1:8737` (`[web] port` in the config, `--port` to override,
`--no-browser` to just serve). Everything the TUI does is a click: the
PROD/leave pill toggles the policy, rows expand into recent files and
per-agent Prod / Type / Nudge / Close / Reopen buttons, the header has the
auto-prod switch, host health pills, sort, and the message log. Actions are
same-origin-only and the server never listens beyond localhost.

Homebrew Python refuses `pip install` into its own site-packages (PEP 668,
"externally managed") — the install then silently never happens and `prodder`
is not on PATH. Use `pipx install .` or
`python3 -m pip install --break-system-packages .` there.

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

## Nudge experiment — which prods actually work

Not every "continue!" helps, and a badly-timed one sends an agent off inventing
work. prodder treats the nudge phrasing as an experiment:

- **It rotates a pool of phrasings** (`nudge_pool`) with an epsilon-greedy
  bandit — mostly sending whatever has best resumed real work, occasionally
  exploring the rest. A per-project custom nudge (`n` in the TUI) overrides it.
- **It scores each prod's outcome.** A few minutes later (`outcome_window`) it
  checks whether the project produced files — the signal prodder already
  tracks. Outcomes: *produced work* / *stayed stuck* / *agent left*. These land
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

- **macOS-centric**: keystroke delivery targets Terminal.app/iTerm2 via
  AppleScript (tmux paths work anywhere). Web prodding needs Chrome's
  *View → Developer → Allow JavaScript from Apple Events* and uses
  best-effort DOM selectors that chat sites may change at any time.
- **A stalled agent and one awaiting your reply look identical.** With
  `auto_prod = true`, an agent politely asking a question gets "continue"
  typed at it after `idle_after` seconds. Defaults ship conservative
  (`auto_prod = false`, `approve_prompts = false`); flip them knowingly.
- **`approve_prompts = true` approves whatever command the agent proposed.**
  That is the point — and the risk. Use `protected` patterns and `leave`
  marks to fence off anything that shouldn't be touched.

## License

MIT
