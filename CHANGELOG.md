# Changelog

## 0.1.1 — first public release

**btop for your AI coding agents.** Watch which projects are producing files,
find the CLI agents working on them, spot the ones that silently stalled, and
prod them back to work — with a keystroke or automatically.

### Features
- **Project activity** — web dashboard and curses TUI: file-production counts at
  15m / 1h / 24h / 3d, 24-hour sparklines, newest file; local and remote hosts
  scanned over ssh.
- **Agent tracking** — finds claude / codex / aider / gemini CLI sessions, maps
  them to projects by working directory, and detects stalls from real terminal
  activity (tmux or tty).
- **Prodding** — types a nudge and actually submits it (tmux `send-keys`, or
  AppleScript into Terminal.app / iTerm), manually or via `auto_prod`.
- **Approval prompts, close & save, reopen** — answer "Yes, proceed" menus,
  capture + recover a session's resume command, reopen a detached agent.
- **Answer from your phone** — a quiet agent's decision menu is pushed to
  Telegram (optional WhatsApp / iMessage); tap or reply and it's typed back.
- **Nudge experiment** — an epsilon-greedy bandit ranks nudge phrasings by
  whether the prod actually resumed real work.
- **Workbench** — local-first SQLite task/event store: define acceptance
  criteria, verify with `check_commands`, and record structured events via
  `/api/events` or `prodder --event` instead of screen-scraping.

### Security & safety
- Dashboard listens on `127.0.0.1` only, with a per-session token gating every
  state-changing action and anti-DNS-rebinding / anti-clickjacking guards.
- Conservative defaults: `auto_prod` and `approve_prompts` are **off**; a
  `never_approve` list is never auto-approved.
- Single-instance lock, atomic `0600` writes, and screen-history redaction.

### Notes
- Python 3.11+, **standard library only** — no dependencies.
- macOS-centric for non-tmux agents; on Linux, run agents inside tmux and
  everything works. Alpha software, provided as-is under AGPL-3.0-or-later.
