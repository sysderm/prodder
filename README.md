# prodtop

**btop for your AI coding agents.** A terminal dashboard that watches which
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
  detached (window gone) · `⊘`/`leave` marked leave-alone.
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

## Install & run

Python 3.11+ stdlib only — no dependencies.

```sh
git clone <repo> && cd prodtop
cp prodtop.example.toml prodtop.toml   # edit roots/hosts
./prodtop.py            # TUI
./prodtop.py --once     # one plain-text snapshot
```

Keys: `j`/`k` select · `p` prod · `i` leave alone · `a` re-arm ·
`x` close+save · `o` reopen detached · `s` sort · `r` rescan · `q` quit.

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
