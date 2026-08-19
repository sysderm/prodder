#!/usr/bin/env python3
"""prodder — btop-style activity monitor for file-producing projects.

Watches project folders on this Mac and on remote hosts (via ssh) and shows
which projects are actively producing files: counts over several horizons,
a 24h sparkline, and the most recently written files.

Also detects coding agents (claude, codex, ...) running in terminals, maps
them to projects via their working directory, flags the ones that have gone
quiet, and can "prod" them by typing a nudge (default: "continue") into
their terminal — manually with [p] or automatically (see [agents] config).

Usage:
    prodtop.py [--config prodtop.toml] [--once]

Keys:  j/k or arrows  select    s  toggle sort (recency / 24h count)
       p              prod the selected row's agent
       r              rescan    q  quit
"""

import argparse
try:
    import curses                       # TUI only; absent on native Windows
except ImportError:                     # pragma: no cover
    curses = None
import fnmatch
import http.server
import json
import os
import random
import re
import secrets
import shlex
import tempfile
import shutil
import signal
import sqlite3
import subprocess
import sys
import webbrowser
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:             # tomllib is stdlib only on 3.11+
    sys.exit("prodder needs Python 3.11+ (for the stdlib 'tomllib' module); "
             "you have Python %d.%d. Install a newer Python (e.g. `brew "
             "install python@3.12`) and run it with that interpreter."
             % sys.version_info[:2])
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

IS_MAC = sys.platform == "darwin"

SPARK_CHARS = " ▁▂▃▄▅▆▇█"
SPARK_BUCKETS = 24          # 24 x 1h sparkline
MAX_RECENT_FILES = 12

SSH_BASE = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/prodtop-%C",
    "-o", "ControlPersist=600",
    # First contact with a tailnet name must not die on the known_hosts
    # prompt (BatchMode can't answer it); accept-new never accepts a
    # CHANGED key, only unknown hosts.
    "-o", "StrictHostKeyChecking=accept-new",
]

# AppleScript app targeting, the hard-won way. iTerm2's *process* name is
# "iTerm2" (used in the System Events `processes whose name` probes below), but
# its *scripting* name is "iTerm" and it is only reachable by display-name
# ("iTerm2") through a fragile Launch-Services alias that intermittently fails
# to load the app's terminology — producing a COMPILE error (-2741,
# "expected end of line but found identifier") on the `newline` keyword that
# takes down the WHOLE script, including the unrelated Terminal.app branch.
# So: always address iTerm by BUNDLE ID (immune to name-registration flakiness),
# and for local sends/captures/listing run the Terminal.app and iTerm blocks as
# SEPARATE osascript invocations (see mac_terminals()/run_local_osa) so a Mac
# without iTerm — the default macOS box has only Terminal.app — never feeds iTerm
# terminology to the compiler at all. The combined OSA_* below are used only for
# the remote-Mac (osascript-over-ssh) path.
ITERM = 'application id "com.googlecode.iterm2"'

# Types text into a Terminal.app tab or iTerm session identified by its tty.
# argv: tty, text, withCR ("1"/"0"). Any Enter is sent SEPARATELY after a
# delay, and as a real carriage return (0x0D): TUI agents (claude/codex input
# boxes) treat text+newline arriving in one burst as a paste, and a plain
# newline is a line feed that composers map to "insert newline" — either way
# the nudge would sit unsubmitted in the prompt.
OSA_SEND_ITERM = '''
on run argv
  set theTTY to item 1 of argv
  set theText to item 2 of argv
  set withCR to item 3 of argv
  tell ''' + ITERM + '''
    repeat with w in windows
      repeat with tb in tabs of w
        repeat with s in sessions of tb
          if (tty of s) is theTTY then
            if theText is not "" then
              tell s to write text theText newline no
            end if
            if withCR is "1" then
              delay 0.6
              tell s to write text (character id 13) newline no
              delay 0.4
              tell s to write text (character id 13) newline no
            end if
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''

OSA_SEND_TERM = '''
on run argv
  set theTTY to item 1 of argv
  set theText to item 2 of argv
  set withCR to item 3 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if (tty of t) is theTTY then
          do script theText in t
          if withCR is "1" then
            delay 0.6
            do script "" in t
          end if
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''

# Combined send — remote-Mac (osascript-over-ssh) path only.
OSA_SEND = '''
on run argv
  set theTTY to item 1 of argv
  set theText to item 2 of argv
  set withCR to item 3 of argv
  tell application "System Events"
    set hasTerm to (count of (processes whose name is "Terminal")) > 0
    set hasIterm to (count of (processes whose name is "iTerm2")) > 0
  end tell
  if hasIterm then
    tell ''' + ITERM + '''
      repeat with w in windows
        repeat with tb in tabs of w
          repeat with s in sessions of tb
            if (tty of s) is theTTY then
              if theText is not "" then
                tell s to write text theText newline no
              end if
              if withCR is "1" then
                delay 0.6
                tell s to write text (character id 13) newline no
                delay 0.4
                tell s to write text (character id 13) newline no
              end if
              return "ok"
            end if
          end repeat
        end repeat
      end repeat
    end tell
  end if
  if hasTerm then
    tell application "Terminal"
      repeat with w in windows
        repeat with t in tabs of w
          if (tty of t) is theTTY then
            do script theText in t
            if withCR is "1" then
              delay 0.6
              do script "" in t
            end if
            return "ok"
          end if
        end repeat
      end repeat
    end tell
  end if
  return "notfound"
end run
'''

# Returns the scrollback/screen of the Terminal.app tab or iTerm session
# owning the given tty.
OSA_CAPTURE_TERM = '''
on run argv
  set theTTY to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if (tty of t) is theTTY then
          return history of t
        end if
      end repeat
    end repeat
  end tell
  return ""
end run
'''

OSA_CAPTURE_ITERM = '''
on run argv
  set theTTY to item 1 of argv
  tell ''' + ITERM + '''
    repeat with w in windows
      repeat with tb in tabs of w
        repeat with s in sessions of tb
          if (tty of s) is theTTY then
            return contents of s
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return ""
end run
'''

# Combined capture — remote-Mac (osascript-over-ssh) path only.
OSA_CAPTURE = '''
on run argv
  set theTTY to item 1 of argv
  tell application "System Events"
    set hasTerm to (count of (processes whose name is "Terminal")) > 0
    set hasIterm to (count of (processes whose name is "iTerm2")) > 0
  end tell
  if hasTerm then
    tell application "Terminal"
      repeat with w in windows
        repeat with t in tabs of w
          if (tty of t) is theTTY then
            return history of t
          end if
        end repeat
      end repeat
    end tell
  end if
  if hasIterm then
    tell ''' + ITERM + '''
      repeat with w in windows
        repeat with tb in tabs of w
          repeat with s in sessions of tb
            if (tty of s) is theTTY then
              return contents of s
            end if
          end repeat
        end repeat
      end repeat
    end tell
  end if
  return ""
end run
'''

# Comma-separated ttys of every live Terminal.app / iTerm tab — the
# authoritative "which ttys still have a window" list (process ancestry lies:
# asciinema wrappers get reparented to launchd while their window lives). Run
# per-app locally and the results merged; see list_window_ttys().
OSA_LIST_TERM = '''
set out to ""
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      set out to out & (tty of t) & ","
    end repeat
  end repeat
end tell
return out
'''

OSA_LIST_ITERM = '''
set out to ""
tell ''' + ITERM + '''
  repeat with w in windows
    repeat with tb in tabs of w
      repeat with s in sessions of tb
        set out to out & (tty of s) & ","
      end repeat
    end repeat
  end repeat
end tell
return out
'''

_MAC_TERMINALS = None


def mac_terminals():
    """(has_terminal, has_iterm): which terminal apps are INSTALLED on this Mac.
    Probed by bundle-id lookup, which — unlike `tell application` — never loads
    the app's scripting terminology, so it cannot raise the -2741 compile error
    when an app is absent. This is what lets us keep iTerm terminology out of the
    compiler entirely on a Terminal-only Mac. Cached: the answer is stable for a
    run (and returns (False, False) off macOS, where osascript is missing)."""
    global _MAC_TERMINALS
    if _MAC_TERMINALS is None:
        def installed(bid):
            try:
                r = subprocess.run(
                    ["osascript", "-e", 'id of application id "%s"' % bid],
                    capture_output=True, text=True, timeout=5)
                return r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        _MAC_TERMINALS = (installed("com.apple.Terminal"),
                          installed("com.googlecode.iterm2"))
    return _MAC_TERMINALS


def open_terminal_window(cmd):
    """Open a new terminal window running `cmd` (iTerm preferred, Terminal.app
    fallback). Returns True on success. Runs each app as a SEPARATE osascript so
    a Mac without iTerm never compiles iTerm terminology (the -2741 bug)."""
    has_term, has_iterm = mac_terminals()
    for present, script in ((has_iterm, OSA_OPEN_ITERM),
                            (has_term, OSA_OPEN_TERM)):
        if not present:
            continue
        try:
            r = subprocess.run(["osascript", "-", cmd], input=script,
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.stdout.strip() == "ok":
            return True
    return False

# Lists Chrome tabs as "windowIdx <tab> tabIdx <tab> url <tab> title" lines.
OSA_CHROME_LIST = '''
set out to ""
set d to tab -- resolve the character OUTSIDE the tell: inside it, "tab" is Chrome's tab class
tell application "System Events"
  set hasChrome to (count of (processes whose name is "Google Chrome")) > 0
end tell
if hasChrome then
  tell application "Google Chrome"
    set wi to 0
    repeat with w in windows
      set wi to wi + 1
      set ti to 0
      repeat with t in tabs of w
        set ti to ti + 1
        set out to out & wi & d & ti & d & (URL of t) & d & (title of t) & linefeed
      end repeat
    end repeat
  end tell
end if
return out
'''

# Focuses a Chrome tab and runs JS in it (argv: windowIdx, tabIdx, js).
OSA_CHROME_JS = '''
on run argv
  set wi to (item 1 of argv) as integer
  set ti to (item 2 of argv) as integer
  set theJS to item 3 of argv
  tell application "Google Chrome"
    set active tab index of window wi to ti
    try
      set r to execute (tab ti of window wi) javascript theJS
      return r as text
    on error errMsg
      return "jserror: " & errMsg
    end try
  end tell
end run
'''

# Best-effort composer fill + send for claude.ai / chatgpt.com / gemini.
WEB_PROD_JS = '''
(function(){
  var NUDGE = "__NUDGE__";
  var t = document.querySelector('#prompt-textarea')
       || document.querySelector('rich-textarea .ql-editor')
       || document.querySelector('div[contenteditable="true"]')
       || document.querySelector('textarea');
  if(!t){ return 'no-input'; }
  t.focus();
  if(t.tagName === 'TEXTAREA'){
    var setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(t, NUDGE);
    t.dispatchEvent(new Event('input', {bubbles: true}));
  } else {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, NUDGE);
  }
  setTimeout(function(){
    var b = document.querySelector('button[data-testid="send-button"]')
         || document.querySelector('button[aria-label*="Send"]')
         || document.querySelector('button[aria-label*="Submit"]')
         || document.querySelector('button[aria-label*="senden"]');
    if(b && !b.disabled){ b.click(); }
    else {
      ['keydown','keypress','keyup'].forEach(function(ev){
        t.dispatchEvent(new KeyboardEvent(ev,
          {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}));
      });
    }
  }, 400);
  return 'ok';
})();
'''

# Opens a new terminal window and runs a shell command in it. iTerm preferred,
# Terminal.app fallback — but as SEPARATE scripts (see open_terminal_window and
# the -2741 note): a `try` does NOT catch an AppleScript *compile* error, so the
# old single combined script died wholesale on a Mac where iTerm terminology
# wouldn't resolve, taking the Terminal fallback down with it. Plain newline is
# fine here — the target is a shell prompt, not a TUI composer.
OSA_OPEN_ITERM = '''
on run argv
  set theCmd to item 1 of argv
  tell ''' + ITERM + '''
    activate
    set w to (create window with default profile)
    -- let the shell finish its init/MOTD first, or the typed command is
    -- flushed away with the startup output
    delay 3
    tell current session of w to write text theCmd
    return "ok"
  end tell
end run
'''

OSA_OPEN_TERM = '''
on run argv
  set theCmd to item 1 of argv
  tell application "Terminal"
    activate
    do script theCmd
    return "ok"
  end tell
end run
'''


@dataclass
class AgentProc:
    host: str
    ssh: str            # ssh target, "" = local
    pid: int
    name: str           # claude / codex / ...
    cwd: str
    tty: str            # /dev/ttys012 or /dev/pts/3
    cpu: float
    work_cpu: float = 0.0  # aggregate CPU of processes sharing this terminal
    tmux: str = ""      # session name, "" = not in tmux
    pane: str = ""      # tmux pane id (%N)
    sock: str = ""      # tmux socket path (remote multi-user servers)
    prod_tty: str = ""  # tty to type into (outermost tty in the process chain;
                        # differs from tty when wrapped in asciinema/script)
    idle: float = -1.0  # seconds without terminal activity at scan time; -1 unknown
    scanned_at: float = 0.0
    protected: bool = False
    detached: bool = False  # wrapper orphaned, no terminal window owns the tty
    mac: bool = False       # remote host is a Mac: prod/capture non-tmux agents
                            # via osascript over ssh (Terminal.app / iTerm2)
    policy: str = "auto"    # auto (proddable) | ignore (leave alone)
    label: str = ""         # display label (web tabs: the tab title)
    recognized: bool = True # False = a plain terminal window with no known agent
                            # (shown so nothing hides, but never auto-prodded)

    @property
    def web(self):
        return self.tty.startswith("tab:")

    def eff_idle(self):
        return self.idle + (time.time() - self.scanned_at) if self.idle >= 0 else -1.0


@dataclass
class Project:
    name: str
    host: str
    root: str
    latest_mtime: float = 0.0
    latest_file: str = ""
    counts: dict = field(default_factory=dict)      # horizon-label -> count
    buckets: list = field(default_factory=list)     # per-hour counts, [0]=now
    recent: list = field(default_factory=list)      # [(mtime, relpath)] newest first
    agents: list = field(default_factory=list)


@dataclass
class HostState:
    name: str
    ok: bool = False
    scanning: bool = False
    error: str = ""
    last_scan: float = 0.0
    projects: dict = field(default_factory=dict)    # name -> Project
    agents: list = field(default_factory=list)


HORIZONS = [("15m", 900), ("1h", 3600), ("24h", 86400), ("3d", 259200)]


def load_config(path):
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"prodder: {path} is not valid TOML: {e}")
    except OSError as e:
        sys.exit(f"prodder: can't read config {path}: {e}")
    s = cfg.setdefault("settings", {})
    s.setdefault("window_days", 3)
    s.setdefault("local_interval", 45)
    s.setdefault("remote_interval", 60)
    s.setdefault("excludes", [".git", "node_modules", ".venv", "venv", "__pycache__"])
    a = cfg.setdefault("agents", {})
    a.setdefault("process_names", ["claude", "codex", "chatgpt", "aider", "gemini", "goose",
                                    "xcode", "antigravity", "cursor"])
    a.setdefault("idle_after", 600)
    a.setdefault("nudge", "continue")
    a.setdefault("approve_prompts", False)   # fail safe: never auto-approve
                                             # command prompts unless opted in
    a.setdefault("web_prod", True)
    a.setdefault("web_sites", {"claude.ai": "claude-web",
                               "chatgpt.com": "chatgpt-web",
                               "chat.openai.com": "chatgpt-web",
                               "gemini.google.com": "gemini-web"})
    a.setdefault("never_approve", [
        "rm -rf", "sudo ", "--force", "force push", "git push",
        "DROP TABLE", "shutdown", "reboot", "mkfs", "diskutil erase",
        "crontab -r", "launchctl unload",
    ])
    r = cfg.setdefault("remote", {})
    r.setdefault("enabled", False)
    r.setdefault("telegram_token", "")
    r.setdefault("telegram_chat_id", "")
    r.setdefault("whatsapp_phone", "")
    r.setdefault("whatsapp_apikey", "")
    r.setdefault("imessage_handle", "")
    r.setdefault("menu_idle", 90)       # agent quiet this long + menu on screen -> notify
    r.setdefault("remote_timeout", 300)  # hold auto-approve this long awaiting a phone answer
    # If you've touched the machine an agent runs on within this many seconds,
    # skip the phone ping — you're right there and will see the prompt. Only
    # applies to hosts whose keyboard idle we can read (this Mac + remote Macs).
    r.setdefault("notify_active_window", 300)
    a.setdefault("auto_prod", False)
    a.setdefault("prod_cooldown", 900)
    a.setdefault("auto_interval", 10)
    a.setdefault("safe_stale_after", 20)
    a.setdefault("safe_stale_cpu", 1.0)
    a.setdefault("auto_restart_attempts", 3)
    a.setdefault("protected", [])
    # Nudge experiment: prodder rotates through nudge_pool (epsilon-greedy
    # bandit favouring whatever resumes real work) and logs each prod's
    # outcome, so the effective phrasings surface in the dashboard. A
    # per-project custom nudge always overrides the pool.
    a.setdefault("nudge_pool", [
        "continue!",
        "continue",
        "Please keep going with the current plan.",
        "What's the next concrete step? Do it.",
        "Proceed.",
    ])
    # Sent instead of a pool nudge when the screen looks finished or like it
    # is asking the user a question — a blunt "continue!" there is what sends
    # an agent off in the wrong direction. This one gives an explicit
    # off-ramp so the agent re-checks rather than inventing work.
    a.setdefault("reassess_nudge",
                 "If the task is complete, briefly summarise what's done and "
                 "what's left. If you're blocked or need a decision, say so. "
                 "Otherwise continue.")
    a.setdefault("bandit_epsilon", 0.15)   # explore rate for the nudge bandit
    a.setdefault("outcome_window", 240)    # seconds to attribute a prod's outcome
    # Surface EVERY open Terminal.app/iTerm2 window on the local Mac, not only
    # the ones running a recognised agent — so a mistyped or unrecognised agent
    # never hides in a window prodder didn't list. These extra rows are shown
    # but never auto-prodded (they're plain shells until proven otherwise).
    a.setdefault("show_all_terminals", True)
    w = cfg.setdefault("web", {})
    w.setdefault("port", 8737)
    cfg["hosts"] = [h for h in cfg.get("hosts", []) if h.get("enabled", True)]
    for i, h in enumerate(cfg["hosts"]):
        # Validate up front with a friendly message, instead of a KeyError
        # traceback at Engine start (missing name) or a cryptic per-scan
        # "scan failed: 'ssh'" much later (remote host missing ssh).
        if not h.get("name"):
            sys.exit(f"prodder: host #{i + 1} in {path} needs a \"name\" "
                     f"(e.g. name = \"local\").")
        if not h.get("local") and not h.get("ssh"):
            sys.exit(f"prodder: host \"{h['name']}\" is not local, so it needs an "
                     f"\"ssh\" target (e.g. ssh = \"user@host\"), or set "
                     f"local = true.")
        h["roots"] = [os.path.expanduser(r) for r in h.get("roots", [])]
        h.setdefault("os", "linux")   # remote hosts: "linux" | "mac"
    cfg["_dir"] = str(Path(path).resolve().parent)
    return cfg


# ------------------------------------------------ per-project prod policies
# Policies are keyed "host|/path/to/project" and apply to any agent whose cwd
# is inside that path — they survive restarts, reopens and pid changes.

def load_state(cfg):
    """(policies, nudges) from prodtop-state.json."""
    try:
        with open(Path(cfg["_dir"]) / "prodtop-state.json") as f:
            d = json.load(f)
            return d.get("policies", {}), d.get("nudges", {})
    except (OSError, ValueError):
        return {}, {}


def save_state(cfg, policies, nudges):
    try:
        with open(Path(cfg["_dir"]) / "prodtop-state.json", "w") as f:
            json.dump({"policies": policies, "nudges": nudges}, f, indent=1)
    except OSError:
        pass


def match_policy(host, path, policies, default="auto"):
    """Most-specific value whose 'host|prefix' key contains path."""
    best_len, best = -1, default
    for k, v in policies.items():
        khost, _, kpath = k.partition("|")
        kpath = kpath.rstrip("/")
        if khost != host or not kpath:
            continue
        if path == kpath or path.startswith(kpath + "/"):
            if len(kpath) > best_len:
                best_len, best = len(kpath), v
    return best


def apply_policies(agents, policies):
    for a in agents:
        a.policy = match_policy(a.host, a.cwd, policies) if a.cwd else "auto"


def row_path(p):
    if p.root.startswith("http"):   # web-tab pseudo row: the URL is the key
        return p.root
    return p.root.rstrip("/") + "/" + p.name


def effective_stalled(a, idle_after):
    return (a.recognized and not a.protected and a.policy != "ignore"
            and agent_state(a, idle_after) == "stalled")


def primary_agent(p):
    return min(p.agents, key=lambda x: x.eff_idle() if x.eff_idle() >= 0 else 1e12)


def agent_regex(names):
    # ".exe" tail: claude-code's real binary is claude.exe even on Unix.
    return re.compile(r"(?:^|[/\s])(" + "|".join(re.escape(n) for n in names)
                      + r")(?:\.exe)?(?:\s|$)")


def is_protected(session_name, patterns):
    return bool(session_name) and any(fnmatch.fnmatch(session_name, p) for p in patterns)


def dedupe_by_tty(agents):
    """CLIs like codex fork helpers on the same tty — keep one agent (the
    lowest pid, i.e. the top-level process) per terminal."""
    best = {}
    for a in agents:
        k = (a.host, a.tty)
        if k not in best or a.pid < best[k].pid:
            best[k] = a
    return list(best.values())


# ---------------------------------------------------------------- scanning

def build_projects(host_name, roots, entries, window):
    """Group (mtime, abspath) entries into per-project stats."""
    now = time.time()
    cutoff = now - window
    grouped = {}
    for mt, path in entries:
        if mt < cutoff:
            continue
        mt = min(mt, now)
        for root in roots:
            prefix = root.rstrip("/") + "/"
            if path.startswith(prefix):
                rel = path[len(prefix):]
                proj_name, _, rel_rest = rel.partition("/")
                if not rel_rest:        # file sitting directly in the root
                    proj_name, rel_rest = "(root)", rel
                grouped.setdefault((root, proj_name), []).append((mt, rel_rest))
                break
    projects = {}
    for (root, name), files in grouped.items():
        files.sort(reverse=True)
        p = Project(name=name, host=host_name, root=root)
        p.latest_mtime, p.latest_file = files[0]
        p.recent = files[:MAX_RECENT_FILES]
        p.counts = {label: sum(1 for mt, _ in files if mt >= now - secs)
                    for label, secs in HORIZONS}
        p.buckets = [0] * SPARK_BUCKETS
        for mt, _ in files:
            idx = int((now - mt) // 3600)
            if idx < SPARK_BUCKETS:
                p.buckets[idx] += 1
        projects[name] = p
    return projects


def scan_local(roots, excludes, window):
    """Walk roots, return [(mtime, abspath)] for files inside the window."""
    cutoff = time.time() - window
    entries = []
    exclude = set(excludes)

    def walk(dirpath):
        try:
            with os.scandir(dirpath) as it:
                for e in it:
                    if e.name in exclude:
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            walk(e.path)
                        elif e.is_file(follow_symlinks=False):
                            mt = e.stat(follow_symlinks=False).st_mtime
                            if mt >= cutoff:
                                entries.append((mt, e.path))
                    except OSError:
                        pass
        except OSError:
            pass

    for root in roots:
        walk(root)
    return entries


def list_local_tmux_panes():
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_tty}|#{pane_id}|#{session_name}|#{window_activity}"],
            capture_output=True, text=True, timeout=10)
        panes = {}
        for line in out.stdout.splitlines():
            parts = line.split("|")
            if len(parts) >= 4:
                try:
                    act = float(parts[3])
                except ValueError:
                    act = 0.0
                panes[parts[0]] = {"pane": parts[1], "session": parts[2],
                                   "activity": act, "sock": ""}
        return panes
    except (OSError, subprocess.SubprocessError):
        return {}


# Process comms that mean "this pid's terminal window is a real, live GUI
# terminal" (used to tell an attached agent from a detached/window-gone one).
# Includes the common Linux terminals so a live gnome-terminal/konsole/xterm
# agent isn't mislabeled "detached" (and then destroyed by a Reopen) on hosts
# where AppleScript window enumeration doesn't exist. Short/ambiguous comms
# (e.g. "st") are deliberately omitted to avoid matching unrelated processes.
TERMINAL_APPS_RX = re.compile(
    r"iTerm|Terminal|Ghostty|kitty|[Aa]lacritty|wezterm|"
    r"gnome-terminal|konsole|xterm|urxvt|tilix|xfce4-terminal|terminator|"
    r"sakura|foot|ptyxis|contour")


def outer_tty(pid, procs):
    """(outermost real tty, attached?) for the ancestry of pid. Agents often
    run inside asciinema/script wrappers on an inner pty; typing must target
    the tty of the actual terminal tab (held by the wrapper's parent chain).
    attached=False means no terminal app appears in the chain — the window
    that hosted this session is gone."""
    last, cur, hops, attached = None, pid, 0, False
    while cur in procs and hops < 15:
        ppid, tty, comm = procs[cur]
        if tty not in ("??", "?", "-"):
            last = tty
        if TERMINAL_APPS_RX.search(comm):
            attached = True
        if ppid <= 1 or ppid == cur:
            break
        cur, hops = ppid, hops + 1
    return last, attached


def list_window_ttys():
    """Set of ttys owned by a live Terminal/iTerm tab, or None if unknown.
    Terminal.app and iTerm are queried as SEPARATE scripts (see the -2741 note)
    so a Mac with only one of them still gets a definitive answer instead of a
    whole-script compile failure."""
    has_term, has_iterm = mac_terminals()
    if not (has_term or has_iterm):
        return None
    ttys, any_ok = set(), False
    for present, script in ((has_term, OSA_LIST_TERM),
                            (has_iterm, OSA_LIST_ITERM)):
        if not present:
            continue
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            any_ok = True
            ttys |= {t.strip() for t in r.stdout.split(",") if t.strip()}
    return ttys if any_ok else None


def scan_web_tabs(agent_cfg):
    """Chrome tabs with an AI chat open, as pseudo-agents on host 'web'.
    No idle signal exists for a web chat, so they are manual-prod only."""
    if not agent_cfg.get("web_prod", True):
        return []
    now = time.time()
    try:
        r = subprocess.run(["osascript", "-e", OSA_CHROME_LIST],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    agents = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        wi, ti, url, title = parts
        for dom, name in agent_cfg["web_sites"].items():
            if dom in url:
                try:
                    a = AgentProc(host="web", ssh="", pid=int(wi) * 10000 + int(ti),
                                  name=name, cwd=url, tty=f"tab:{wi}:{ti}",
                                  cpu=0.0, scanned_at=now)
                except ValueError:
                    break
                a.label = (title.strip() or dom)[:40]
                agents.append(a)
                break
    return agents


def prod_web(agent, nudge):
    wi, ti = agent.tty.split(":")[1:3]
    js = WEB_PROD_JS.replace("__NUDGE__",
                             nudge.replace("\\", "\\\\").replace('"', '\\"'))
    try:
        r = subprocess.run(["osascript", "-", wi, ti, js], input=OSA_CHROME_JS,
                           capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as e:
        return f"web prod failed: {e}"
    res = (r.stdout or r.stderr).strip()
    if res == "ok":
        return f"prodded {agent.name} tab \"{agent.label}\""
    if res == "no-input":
        return f"no composer found in {agent.name} tab (site layout changed?)"
    if "jserror" in res or "JavaScript through AppleScript" in res:
        return ("Chrome blocks scripted JS — enable View → Developer → "
                "Allow JavaScript from Apple Events, then retry")
    return f"web prod failed: {res[:90]}"


def close_web_tab(agent, cfg):
    """Log the tab URL to closed-agents.md, then close the tab."""
    wi, ti = agent.tty.split(":")[1:3]
    log = Path(cfg["_dir"]) / "closed-agents.md"
    try:
        with open(log, "a") as f:
            f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} — {agent.name} "
                    f"tab \"{agent.label}\"\n\n- **resume:** open `{agent.cwd}`\n")
    except OSError:
        pass
    try:
        subprocess.run(["osascript", "-e",
                        f'tell application "Google Chrome" to close tab {ti} '
                        f'of window {wi}'],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not close tab: {e}"
    return f"closed {agent.name} tab — URL saved → closed-agents.md"


def pfloat(s):
    """float() that tolerates a comma decimal separator — ps/stat output on a
    de_DE/fr_FR-style locale prints "0,4"; the LC_ALL=C we set should prevent
    that, but this is the belt to that suspenders (raises ValueError like float)."""
    return float(s.replace(",", ".", 1) if "," in s else s)


def scan_agents_local(host_name, agent_cfg):
    now = time.time()
    rx = agent_regex(agent_cfg["process_names"])
    try:
        # LC_ALL=C so pcpu prints a dot decimal — a comma-decimal locale
        # (de_DE, fr_FR, …) makes float() throw and every agent vanish.
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,pcpu=,args="],
                             capture_output=True, text=True, timeout=15,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return []
    rows, procs, tty_cpu = [], {}, {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, tty, cpu, args = parts
        try:
            procs[int(pid)] = (int(ppid), tty, args.split()[0] if args else "")
        except ValueError:
            continue
        if tty not in ("??", "?", "-"):
            try:
                tty_cpu["/dev/" + tty] = tty_cpu.get("/dev/" + tty, 0.0) + pfloat(cpu)
            except ValueError:
                pass
        if tty in ("??", "?", "-") or "prodtop" in args:
            continue
        m = rx.search(args)
        if not m:
            continue
        try:
            rows.append((int(pid), "/dev/" + tty, pfloat(cpu), m.group(1)))
        except ValueError:
            pass
    cwds = lsof_cwds([p for p, _, _, _ in rows])
    panes = list_local_tmux_panes()
    window_ttys = list_window_ttys()
    agents = []
    for pid, tty, cpu, name in rows:
        a = AgentProc(host=host_name, ssh="", pid=pid, name=name,
                      cwd=cwds.get(pid, ""), tty=tty, cpu=cpu,
                      work_cpu=tty_cpu.get(tty, cpu), scanned_at=now)
        ot, attached = outer_tty(pid, procs)
        a.prod_tty = "/dev/" + ot if ot else tty
        if window_ttys is not None:
            a.detached = a.prod_tty not in window_ttys
        else:
            # No window list (no osascript). The "is there a GUI terminal-app
            # in the ancestry" heuristic is macOS-only — on Linux a live agent
            # in gnome-terminal/konsole/xterm has no such ancestor, so only
            # trust it on macOS; elsewhere a present tty means attached.
            a.detached = IS_MAC and not attached
        pane = panes.get(tty)
        if pane:
            a.pane, a.tmux, a.sock = pane["pane"], pane["session"], pane["sock"]
            a.idle = max(0.0, now - pane["activity"]) if pane["activity"] else -1.0
            a.detached = False      # tmux is its own attachment point
        else:
            try:
                a.idle = max(0.0, now - os.stat(tty).st_mtime)
            except OSError:
                a.idle = -1.0
        a.protected = is_protected(a.tmux, agent_cfg["protected"])
        agents.append(a)
    agents = dedupe_by_tty(agents)
    if agent_cfg.get("show_all_terminals", True) and window_ttys:
        agents += plain_terminals(host_name, window_ttys, agents, procs,
                                  tty_cpu, panes, now)
    return agents


def plain_terminals(host_name, window_ttys, agents, procs, tty_cpu, panes, now):
    """Placeholder rows for open Terminal/iTerm windows that hold no recognised
    agent — so an unrecognised or mistyped agent can't hide in a window prodder
    never listed. Shown, but never auto-prodded (see AgentProc.recognized)."""
    covered = set()
    for a in agents:
        covered.add(a.tty)
        if a.prod_tty:
            covered.add(a.prod_tty)
    want = [w for w in window_ttys if w not in covered]
    if not want:
        return []
    # foreground process per uncovered window tty: the highest-pid process still
    # on that tty (a shell's most recent child), used only for its name + cwd.
    leaf = {}
    for pid, (ppid, t, comm) in procs.items():
        if t in ("??", "?", "-"):
            continue
        full = "/dev/" + t
        if full not in want or "prodtop" in comm:
            continue
        if full not in leaf or pid > leaf[full][0]:
            leaf[full] = (pid, comm)
    extra = lsof_cwds([v[0] for v in leaf.values()])
    out = []
    for wtty in want:
        pid, comm = leaf.get(wtty, (0, ""))
        name = os.path.basename(comm).lstrip("-") or "shell"
        pane = panes.get(wtty)
        a = AgentProc(host=host_name, ssh="", pid=pid, name=name,
                      cwd=extra.get(pid, ""), tty=wtty, cpu=0.0,
                      work_cpu=tty_cpu.get(wtty, 0.0), scanned_at=now,
                      recognized=False)
        a.prod_tty = wtty
        if pane:
            a.tmux, a.pane, a.sock = pane["session"], pane["pane"], pane["sock"]
        try:
            a.idle = max(0.0, now - os.stat(wtty).st_mtime)
        except OSError:
            a.idle = -1.0
        out.append(a)
    return out


def lsof_cwds(pids):
    """pid -> current working directory via one lsof call ({} on any failure)."""
    cwds = {}
    pids = [p for p in pids if p]
    if not pids:
        return cwds
    try:
        lo = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-p", ",".join(str(p) for p in pids), "-Fn"],
            capture_output=True, text=True, timeout=15)
        cur = None
        for ln in lo.stdout.splitlines():
            if ln.startswith("p"):
                try:
                    cur = int(ln[1:])
                except ValueError:
                    cur = None
            elif ln.startswith("n") and cur is not None:
                cwds[cur] = ln[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    # Linux fallback for pids lsof missed (or lsof absent entirely, common on
    # minimal servers/containers): read /proc/<pid>/cwd. No-op on macOS.
    for p in pids:
        if p not in cwds:
            try:
                cwds[p] = os.readlink(f"/proc/{p}/cwd")
            except OSError:
                pass
    return cwds


def remote_command(roots, excludes, days):
    prune = " -o ".join(f"-name {shlex.quote(x)}" for x in excludes)
    roots_q = " ".join(shlex.quote(r) for r in roots)
    find_part = (f"find {roots_q} \\( {prune} \\) -prune -o "
                 f"-type f -mtime -{days} -printf '%T@\\t%p\\n' 2>/dev/null")
    return (
        # C locale so ps pcpu / stat mtimes are dot-decimal, not "0,4"
        "export LC_ALL=C; "
        + find_part + "; "
        "echo ===AGENTS===; ps -eo pid=,tty=,pcpu=,args=; "
        "echo ===TMUX===; for s in /tmp/tmux-*/default; do "
        'tmux -S "$s" list-panes -a -F '
        '"$s|#{pane_tty}|#{pane_id}|#{session_name}|#{window_activity}" 2>/dev/null; '
        "done; "
        "echo ===CWD===; ps -eo pid=,tty= | awk '$2 != \"?\" {print $1}' | "
        'while read p; do c=$(readlink /proc/$p/cwd 2>/dev/null); '
        '[ -n "$c" ] && echo "$p $c"; done; '
        "echo ===TTY===; stat -c '%n %Y' /dev/pts/* 2>/dev/null; true"
    )


def remote_command_mac(roots, excludes, days):
    """BSD variant for remote Macs (no GNU find/stat, no /proc; tmux sockets
    live under the per-user DARWIN_USER_TEMP_DIR; agents usually sit in
    Terminal.app/iTerm2 windows). Emits the same sections as remote_command
    plus PROCS (pid/ppid/tty/comm) so the caller can walk the process chain
    to the outermost tty (asciinema wrappers, see outer_tty)."""
    prune = " -o ".join(f"-name {shlex.quote(x)}" for x in excludes)
    roots_q = " ".join(shlex.quote(r) for r in roots)
    return (
        # C locale so ps pcpu / stat mtimes are dot-decimal, not "0,4"
        "export LC_ALL=C; "
        f"find {roots_q} \\( {prune} \\) -prune -o "
        f"-type f -mtime -{days} -print0 2>/dev/null | "
        "xargs -0 stat -f '%m%t%N' 2>/dev/null; "
        "echo ===AGENTS===; ps -eo pid=,tty=,pcpu=,args=; "
        "echo ===PROCS===; ps -eo pid=,ppid=,tty=,comm=; "
        'echo ===TMUX===; for s in /tmp/tmux-*/default '
        '"$(getconf DARWIN_USER_TEMP_DIR)"tmux-*/default; do '
        'tmux -S "$s" list-panes -a -F '
        '"$s|#{pane_tty}|#{pane_id}|#{session_name}|#{window_activity}" 2>/dev/null; '
        "done; "
        "echo ===CWD===; pids=$(ps -eo pid=,tty= | "
        "awk '$2 ~ /^ttys/ {printf \"%s,\",$1}'); "
        '[ -n "$pids" ] && lsof -a -p "${pids%,}" -d cwd -Fn 2>/dev/null | '
        "awk '/^p/{pid=substr($0,2)} /^n/{print pid, substr($0,2)}'; "
        "echo ===TTY===; stat -f '%N %m' /dev/ttys* 2>/dev/null; true"
    )


def scan_remote(host_name, ssh_target, roots, excludes, window, agent_cfg,
                mac=False):
    """One ssh round-trip: recent files + agent processes + tmux + tty idle.
    The script goes over stdin to /bin/sh so the remote login shell (zsh on
    Macs, aborts on unmatched globs) never parses it."""
    days = max(1, int(window // 86400))
    script = (remote_command_mac if mac else remote_command)(roots, excludes, days)
    out = subprocess.run(SSH_BASE + [ssh_target, "/bin/sh -s"], input=script,
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip()
                           else f"ssh exit {out.returncode}")
    now = time.time()
    sections = {"FIND": [], "AGENTS": [], "PROCS": [], "TMUX": [], "CWD": [],
                "TTY": []}
    cur = "FIND"
    for line in out.stdout.splitlines():
        if line.startswith("===") and line.endswith("===") and line[3:-3] in sections:
            cur = line[3:-3]
            continue
        sections[cur].append(line)

    entries = []
    for line in sections["FIND"]:
        ts, _, path = line.partition("\t")
        try:
            entries.append((float(ts), path))
        except ValueError:
            pass

    panes = {}
    for line in sections["TMUX"]:
        parts = line.split("|")
        if len(parts) >= 5:
            try:
                act = float(parts[4])
            except ValueError:
                act = 0.0
            panes[parts[1]] = {"sock": parts[0], "pane": parts[2],
                               "session": parts[3], "activity": act}
    cwds = {}
    for line in sections["CWD"]:
        pid, _, path = line.partition(" ")
        if path:
            cwds[pid] = path
    tty_mtimes = {}
    for line in sections["TTY"]:
        dev, _, ts = line.rpartition(" ")
        try:
            tty_mtimes[dev] = float(ts)
        except ValueError:
            pass

    procs = {}
    for line in sections["PROCS"]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
        except ValueError:
            continue

    rx = agent_regex(agent_cfg["process_names"])
    agents = []
    tty_cpu = {}
    for line in sections["AGENTS"]:
        parts = line.split(None, 3)
        if len(parts) < 4 or parts[1] in ("??", "?", "-"):
            continue
        try:
            dev = "/dev/" + parts[1]
            tty_cpu[dev] = tty_cpu.get(dev, 0.0) + pfloat(parts[2])
        except ValueError:
            pass
    for line in sections["AGENTS"]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, cpu, args = parts
        if tty in ("??", "?", "-") or "prodtop" in args:
            continue
        m = rx.search(args)
        if not m:
            continue
        dev = "/dev/" + tty
        try:
            a = AgentProc(host=host_name, ssh=ssh_target, pid=int(pid),
                          name=m.group(1), cwd=cwds.get(pid, ""), tty=dev,
                          cpu=pfloat(cpu), work_cpu=tty_cpu.get(dev, pfloat(cpu)),
                          scanned_at=now, mac=mac)
        except ValueError:
            continue
        pane = panes.get(dev)
        if pane:
            a.pane, a.tmux, a.sock = pane["pane"], pane["session"], pane["sock"]
            a.idle = max(0.0, now - pane["activity"]) if pane["activity"] else -1.0
        elif dev in tty_mtimes:
            a.idle = max(0.0, now - tty_mtimes[dev])
        if mac and not a.pane:
            ot, attached = outer_tty(a.pid, procs)
            a.prod_tty = "/dev/" + ot if ot else dev
            a.detached = not attached
            if a.prod_tty != dev and a.prod_tty in tty_mtimes:
                a.idle = max(0.0, now - tty_mtimes[a.prod_tty])
        a.protected = is_protected(a.tmux, agent_cfg["protected"])
        agents.append(a)
    return entries, dedupe_by_tty(agents)


class Scanner(threading.Thread):
    def __init__(self, host_cfg, settings, agent_cfg, state, lock, wake):
        super().__init__(daemon=True)
        self.cfg = host_cfg
        self.settings = settings
        self.agent_cfg = agent_cfg
        self.state = state
        self.lock = lock
        self.wake = wake          # event: force an immediate rescan
        self.stop = False

    def run(self):
        name = self.cfg["name"]
        local = self.cfg.get("local", False)
        interval = self.settings["local_interval"] if local else self.settings["remote_interval"]
        window = self.settings["window_days"] * 86400
        excludes = self.settings["excludes"]
        roots = self.cfg.get("roots", [])
        while not self.stop:
            with self.lock:
                self.state[name].scanning = True
            try:
                if local:
                    agents = (scan_agents_local(name, self.agent_cfg)
                              + scan_web_tabs(self.agent_cfg))
                    with self.lock:      # publish agents before the slow file walk
                        self.state[name].agents = agents
                    entries = scan_local(roots, excludes, window)
                else:
                    entries, agents = scan_remote(name, self.cfg["ssh"], roots,
                                                  excludes, window, self.agent_cfg,
                                                  mac=self.cfg["os"] == "mac")
                projects = build_projects(name, roots, entries, window)
                with self.lock:
                    hs = self.state[name]
                    hs.projects, hs.agents = projects, agents
                    hs.ok, hs.error, hs.last_scan = True, "", time.time()
            except Exception as e:
                with self.lock:
                    hs = self.state[name]
                    hs.ok, hs.error, hs.last_scan = False, str(e)[:120], time.time()
            finally:
                with self.lock:
                    self.state[name].scanning = False
            self.wake.wait(timeout=interval)
            self.wake.clear()


# ---------------------------------------------------------------- prodding

# Approval menus shown by claude/codex when they want to run a command. The
# highlighted default is "Yes, proceed", so a bare Enter accepts it (and never
# the "don't ask again" variant).
PROMPT_RX = re.compile(
    r"Yes, proceed|1\. Yes|Do you want to proceed|Would you like to run")

# A screen that looks FINISHED or is handing the decision back to the user.
# Prodding these with a directive "continue!" is the main way an agent gets
# sent off in the wrong direction (it invents work to satisfy the nudge), so
# when the tail of the screen matches, prodder sends the reassess_nudge — an
# explicit off-ramp — instead. Erring toward over-detection is cheap here: the
# reassess nudge still ends with "otherwise continue", so a false positive
# only sends a gentler prod; a false negative sends the harmful blunt one.
#
# STRONG phrases are distinctive hand-backs / questions — safe to match
# anywhere in the tail. WEAK phrases are generic completion words that could
# appear inside a log line, so they only count at the start of a (bulleted)
# line or as a standalone sentence.
STRONG_DONE_RX = re.compile(
    r"(?i)("
    r"is there anything else|anything else\?|let me know if|"
    r"would you like me to|do you want me to|shall i\b|"
    r"what would you like|how would you like|which (?:option|approach)|"
    r"here'?s (?:a |the )?summary|to summari[sz]e\b|in summary\b|"
    r"waiting for (?:your )?(?:input|response|confirmation|decision)|"
    r"i'?ll (?:wait|stop here)|standing by\b"
    r")")
WEAK_DONE_RX = re.compile(
    r"(?im)^[\s>*•\-]*(?:the )?("
    r"task (?:is )?complete|all done\b|i'?m done\b|"
    r"done!|✓|✅|completed[.!]|finished[.!]"
    r")")


def looks_done(screen):
    """True if the visible tail suggests the agent finished or is asking the
    user something — i.e. a plain 'continue!' would misfire."""
    tail = "\n".join(screen.splitlines()[-12:])
    return bool(STRONG_DONE_RX.search(tail) or WEAK_DONE_RX.search(tail))


def send_to_terminal(agent, text, submit=True):
    """Deliver text (and optionally a submitting Enter) to the agent's
    terminal. text of "\\x1b" sends a bare Escape. Returns None on success,
    an error string otherwise."""
    # Central guard: protected fleet sessions (cockpit-*, vps-batch, agentd,
    # …; see ~/CLAUDE.md) are hands-off for EVERY caller — prod, manual type,
    # recovery. prod_agent also checks earlier, but keystrokes must never
    # reach a protected pane by any path.
    if agent.protected:
        return f"'{agent.tmux}' is a protected session — not typing into it"
    try:
        if agent.pane:
            if text == "\x1b":
                parts = [["send-keys", "-t", agent.pane, "Escape"]]
            elif text == "\x03":
                parts = [["send-keys", "-t", agent.pane, "C-c"]]
            else:
                parts = []
                if text:
                    parts.append(["send-keys", "-t", agent.pane, "-l", text])
                if submit:
                    parts.append(["send-keys", "-t", agent.pane, "Enter"])
            if agent.ssh:
                sock = shlex.quote(agent.sock)
                cmd = " && sleep 0.6 && ".join(
                    f"tmux -S {sock} " + " ".join(shlex.quote(x) for x in p)
                    for p in parts)
                r = subprocess.run(SSH_BASE + [agent.ssh, cmd],
                                   capture_output=True, text=True, timeout=30)
            else:
                r = None
                for i, p in enumerate(parts):
                    if i:
                        time.sleep(0.6)
                    r = subprocess.run(["tmux"] + p, capture_output=True,
                                       text=True, timeout=10)
            if r is not None and r.returncode != 0:
                return f"send failed: {r.stderr.strip()[:80]}"
            return None
        if not agent.ssh and sys.platform != "darwin":
            # No tmux pane and not macOS: there is no way to type into a bare
            # terminal window here. Say so plainly instead of failing later with
            # a cryptic "[Errno 2] ... 'osascript'".
            return ("typing into a bare terminal is macOS-only — run this agent "
                    "inside tmux so prodder can send keys via tmux send-keys")
        if not agent.ssh or agent.mac:
            tty = agent.prod_tty or agent.tty
            osa_args = [tty, text, "1" if submit and text != "\x1b" else "0"]
            if agent.ssh:
                # Remote Mac: one combined AppleScript, executed on that Mac.
                # Needs a one-time Automation consent for sshd on the remote box.
                cmd = "osascript - " + " ".join(shlex.quote(x) for x in osa_args)
                r = subprocess.run(SSH_BASE + [agent.ssh, cmd], input=OSA_SEND,
                                   capture_output=True, text=True, timeout=45)
                res, err = r.stdout.strip(), r.stderr.strip()
            else:
                # Local: run Terminal.app and iTerm as SEPARATE osascripts so a
                # Mac without iTerm never compiles iTerm terminology (the -2741
                # bug); one app's failure can't abort the other's delivery.
                has_term, has_iterm = mac_terminals()
                if not (has_term or has_iterm):
                    return "no scriptable terminal (Terminal.app / iTerm) found"
                res, err = "notfound", ""
                for present, script in ((has_term, OSA_SEND_TERM),
                                        (has_iterm, OSA_SEND_ITERM)):
                    if not present:
                        continue
                    r = subprocess.run(["osascript", "-"] + osa_args, input=script,
                                       capture_output=True, text=True, timeout=30)
                    res = r.stdout.strip()
                    if res == "ok":
                        err = ""
                        break
                    if r.returncode != 0 and r.stderr.strip():
                        err = r.stderr.strip()
            if res == "ok":
                return None
            if res == "notfound" and not err:
                return (f"no terminal tab owns {tty} — session is detached "
                        f"(closed window?) or in an unscriptable terminal")
            return f"send failed: {(err or res)[:100]}"
        return f"can't reach {agent.name} on {agent.host}: not in tmux"
    except (OSError, subprocess.SubprocessError) as e:
        return f"send failed: {e}"


def prod_agent(agent, nudge, approve_prompts=False, never_approve=(), screen=None):
    """Unstick an agent: if it is sitting on an approval prompt, press Enter
    to accept the default "Yes"; otherwise type the nudge text + Enter.
    Prompts matching a never_approve pattern are left for a human. Pass
    `screen` to reuse an already-captured view instead of grabbing it again."""
    if agent.protected:
        return f"'{agent.tmux}' is a protected session — not prodding"
    if agent.web:
        return prod_web(agent, nudge)
    text, verb = nudge, "prodded"
    if approve_prompts:
        if screen is None:
            screen = capture_screen(agent)
        tail = "\n".join(screen.splitlines()[-30:])
        if PROMPT_RX.search(tail):
            low = tail.lower()
            if any(p.lower() in low for p in never_approve):
                return (f"{agent.name} prompt matches never_approve — "
                        f"needs a human decision")
            text, verb = "", "approved prompt of"
    err = send_to_terminal(agent, text, submit=True)
    if err:
        return err
    where = f"tmux {agent.tmux}" if agent.pane else (agent.prod_tty or agent.tty)
    return f"{verb} {agent.name} ({where}) on {agent.host}"


# ------------------------------------------------------- remote answering

MENU_OPT_RX = re.compile(r"^\s*[❯›>]?\s*([1-9])[\.\)]\s+\S")


def extract_menu(screen):
    """(menu_text, option_digits) if the screen shows a numbered choice menu."""
    lines = [l.rstrip() for l in screen.splitlines() if l.strip()][-30:]
    hits = [i for i, l in enumerate(lines) if MENU_OPT_RX.match(l)]
    if len(hits) < 2:
        return None, []
    opts = sorted({MENU_OPT_RX.match(lines[i]).group(1) for i in hits})
    start = max(0, hits[0] - 8)
    return "\n".join(lines[start:hits[-1] + 2])[-1500:], opts


def tg_api(token, method, payload=None, timeout=35):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def wa_notify(phone, apikey, text):
    """Outbound-only WhatsApp ping via CallMeBot (third-party service — the
    message text passes through it; keep it enabled only if that's OK)."""
    q = urllib.parse.urlencode({"phone": phone, "apikey": apikey,
                                "text": text[:800]})
    try:
        urllib.request.urlopen(
            f"https://api.callmebot.com/whatsapp.php?{q}", timeout=20).read()
    except OSError:
        pass


IMSG_DB = Path.home() / "Library/Messages/chat.db"

OSA_IMSG_SEND = '''
on run argv
  set theHandle to item 1 of argv
  set theText to item 2 of argv
  tell application "Messages"
    set s to 1st account whose service type = iMessage
    send theText to participant theHandle of s
  end tell
  return "ok"
end run
'''


def imsg_send(handle, text):
    try:
        r = subprocess.run(["osascript", "-", handle, text],
                           input=OSA_IMSG_SEND, capture_output=True,
                           text=True, timeout=20)
        return r.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


def imsg_extract_text(text, blob):
    """Message text; newer macOS stores it only in the attributedBody
    NSKeyedArchiver blob — extract the embedded NSString payload."""
    if text:
        return text
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    i += len(b"NSString") + 5      # skip archiver marker bytes
    if i >= len(blob):
        return ""
    ln = blob[i]
    if ln == 0x81:
        ln = int.from_bytes(blob[i + 1:i + 3], "little")
        i += 3
    else:
        i += 1
    return blob[i:i + ln].decode("utf-8", "ignore")


def imsg_connect():
    return sqlite3.connect(f"file:{IMSG_DB}?mode=ro", uri=True, timeout=2)


def imsg_max_rowid():
    """Highest message ROWID, or None if chat.db is unreadable (no Full
    Disk Access for the terminal app)."""
    try:
        con = imsg_connect()
        rid = con.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]
        con.close()
        return rid
    except sqlite3.Error:
        return None


def imsg_poll(handle, last_rowid):
    """New messages in the chat with `handle` since last_rowid."""
    try:
        con = imsg_connect()
        rows = con.execute(
            "SELECT m.ROWID, m.text, m.attributedBody FROM message m "
            "JOIN chat_message_join j ON j.message_id = m.ROWID "
            "JOIN chat c ON c.ROWID = j.chat_id "
            "WHERE c.chat_identifier = ? AND m.ROWID > ? ORDER BY m.ROWID",
            (handle, last_rowid)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [(rid, imsg_extract_text(txt, blob)) for rid, txt, blob in rows]


HID_IDLE_RX = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def _hid_idle_from_ioreg(text):
    vals = [int(x) for x in HID_IDLE_RX.findall(text)]
    return min(vals) / 1e9 if vals else None   # ns -> s; min = most recent input


def human_idle_secs(host_cfg):
    """Seconds since the user last touched this machine's keyboard/mouse, or
    None when it can't be known (headless Linux box, unreachable host)."""
    try:
        if host_cfg.get("local"):
            r = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                               capture_output=True, text=True, timeout=5)
            return _hid_idle_from_ioreg(r.stdout)
        if host_cfg.get("os") == "mac" and host_cfg.get("ssh"):
            r = subprocess.run(SSH_BASE + [host_cfg["ssh"], "ioreg -c IOHIDSystem"],
                               capture_output=True, text=True, timeout=10)
            return _hid_idle_from_ioreg(r.stdout)
    except (OSError, subprocess.SubprocessError):
        return None
    return None


class RemoteAnswerer(threading.Thread):
    """Watches quiet agents for on-screen choice menus, pushes them to
    Telegram (buttons) and optionally WhatsApp (notify-only), and types
    answers back into the right terminal. Only the configured chat id is
    obeyed."""

    def __init__(self, cfg, state, lock, msgs, pending_remote, policies):
        super().__init__(daemon=True)
        self.rc, self.agent_cfg = cfg["remote"], cfg["agents"]
        self.hosts = {h["name"]: h for h in cfg["hosts"]}
        self.state, self.lock, self.msgs = state, lock, msgs
        self.pending_remote, self.policies = pending_remote, policies
        self.offset = 0
        self.notified, self.last_check = {}, {}
        self.idle_cache = {}                # host -> (checked_at, idle_secs|None)
        self.last_target = None
        self.imsg_rowid = None
        self.own_texts = deque(maxlen=30)   # texts we sent to the self-chat
        self.stop = False

    def _user_at_machine(self, host, window):
        """True if the user touched `host` within `window` seconds. Cached so
        we don't ioreg/ssh on every loop; unknown hosts return False (notify)."""
        hc = self.hosts.get(host)
        if not hc:
            return False
        now = time.time()
        ts, idle = self.idle_cache.get(host, (0.0, None))
        if now - ts > 15:
            idle = human_idle_secs(hc)
            self.idle_cache[host] = (now, idle)
        return idle is not None and idle < window

    def run(self):
        while not self.stop:
            try:
                self.check_menus()
                self.poll_replies()     # long poll doubles as loop pacing
            except Exception as e:
                self.msgs.append(f"remote: {str(e)[:80]}")
                time.sleep(15)

    def _tg(self, method, payload):
        return tg_api(self.rc["telegram_token"], method, payload)

    def _say(self, text):
        if self.rc["telegram_token"] and self.rc["telegram_chat_id"]:
            self._tg("sendMessage", {"chat_id": self.rc["telegram_chat_id"],
                                     "text": text})
        self._imsg(text)

    def _imsg(self, text):
        if self.rc["imessage_handle"]:
            self.own_texts.append(text)
            imsg_send(self.rc["imessage_handle"], text)

    def check_menus(self):
        with self.lock:
            agents = [a for hs in self.state.values() for a in hs.agents]
            apply_policies(agents, self.policies)
        now = time.time()
        for a in agents:
            key = (a.host, a.tty)
            if a.web or a.protected or a.policy == "ignore" or not a.recognized:
                continue
            if now - self.last_check.get(key, 0) < 60:
                continue
            self.last_check[key] = now
            menu, opts = extract_menu(capture_screen(a))
            if not menu:
                continue
            # Codex redraws its approval UI while it is waiting for input, so
            # the terminal's mtime can stay fresh forever.  Such prompts are
            # still explicit human decisions and must reach the phone; normal
            # numbered menus continue to respect menu_idle.
            if (a.eff_idle() < self.rc["menu_idle"]
                    and not PROMPT_RX.search(menu)):
                self.notified.pop(key, None)
                continue
            # If you're sitting at the machine this agent runs on, you'll see
            # the prompt yourself — don't buzz your phone. Reset notified so a
            # later matured menu still reaches you once you walk away.
            if self._user_at_machine(a.host, self.rc.get("notify_active_window", 300)):
                self.notified.pop(key, None)
                continue
            h = hash(menu)
            if self.notified.get(key) == h:
                continue
            self.notified[key] = h
            self.pending_remote[key] = now
            self.last_target = key
            proj = os.path.basename(a.cwd.rstrip("/")) or a.cwd or "?"
            text = (f"🤖 {a.name} in {proj} ({a.host}) needs a decision:\n\n"
                    f"{menu}\n\nTap a button, or reply with text to type it in.")
            buttons = [{"text": o, "callback_data": f"{a.host}|{a.tty}|{o}"}
                       for o in opts]
            buttons.append({"text": "esc", "callback_data": f"{a.host}|{a.tty}|esc"})
            if self.rc["telegram_token"] and self.rc["telegram_chat_id"]:
                self._tg("sendMessage",
                         {"chat_id": self.rc["telegram_chat_id"], "text": text,
                          "reply_markup": {"inline_keyboard": [buttons]}})
            self._imsg(text + f"\n(reply {'/'.join(opts)} or esc)")
            if self.rc["whatsapp_phone"] and self.rc["whatsapp_apikey"]:
                wa_notify(self.rc["whatsapp_phone"], self.rc["whatsapp_apikey"],
                          text + "\n(answer via Telegram/iMessage)")
            self.msgs.append(f"📱 prompt of {a.name}/{proj} sent to phone")

    def poll_replies(self):
        self.poll_imessage()
        if not (self.rc["telegram_token"] and self.rc["telegram_chat_id"]):
            time.sleep(6 if self.rc["imessage_handle"] else 15)
            return
        ups = tg_api(self.rc["telegram_token"], "getUpdates",
                     {"offset": self.offset, "timeout": 20}, timeout=40)
        me = str(self.rc["telegram_chat_id"])
        for u in ups.get("result", []):
            self.offset = u["update_id"] + 1
            if "callback_query" in u:
                cq = u["callback_query"]
                if str(cq.get("from", {}).get("id")) != me:
                    continue
                host, tty, choice = cq["data"].split("|", 2)
                result = self.answer(host, tty, choice)
                delivered = result.startswith("✓ sent ")
                self._tg("answerCallbackQuery", {
                    "callback_query_id": cq["id"],
                    "text": "✓ Sent — prompt closed" if delivered else result[:180],
                })
                if delivered and cq.get("message"):
                    msg = cq["message"]
                    # Retire the inline keyboard so a question cannot be
                    # answered twice after its choice reached the terminal.
                    closed = (msg.get("text") or "") + (
                        "\n\n✅ Answer sent — this prompt is closed.")
                    self._tg("editMessageText", {
                        "chat_id": msg["chat"]["id"],
                        "message_id": msg["message_id"],
                        "text": closed[:4096],
                    })
                self._say(result)
            elif "message" in u:
                m = u["message"]
                if str(m.get("chat", {}).get("id")) != me:
                    continue
                txt = (m.get("text") or "").strip()
                if not txt:
                    continue
                if self.last_target is None:
                    self._say("no pending prompt to answer")
                    continue
                result = self.answer(*self.last_target, txt)
                self._say(result)

    def poll_imessage(self):
        handle = self.rc["imessage_handle"]
        if not handle:
            return
        if self.imsg_rowid is None:
            self.imsg_rowid = imsg_max_rowid()
            if self.imsg_rowid is None:
                self.msgs.append("iMessage: can't read chat.db — grant your "
                                 "terminal Full Disk Access")
                self.rc["imessage_handle"] = ""   # stop retrying this run
            return
        for rid, txt in imsg_poll(handle, self.imsg_rowid):
            self.imsg_rowid = max(self.imsg_rowid, rid)
            txt = (txt or "").strip()
            # the self-chat echoes our own notifications — skip them
            if not txt or txt in self.own_texts or txt[:1] in ("🤖", "✓", "✅"):
                continue
            if self.last_target is None:
                self._imsg("no pending prompt to answer")
                continue
            self._imsg(self.answer(*self.last_target, txt))

    def answer(self, host, tty, choice):
        with self.lock:
            agent = next((a for hs in self.state.values() for a in hs.agents
                          if a.host == host and a.tty == tty), None)
        if agent is None:
            return f"that agent ({tty}) is gone"
        if choice.lower() == "esc":
            err = send_to_terminal(agent, "\x1b", submit=False)
        else:
            err = send_to_terminal(agent, choice, submit=True)
        self.pending_remote.pop((host, tty), None)
        self.notified.pop((host, tty), None)
        proj = os.path.basename(agent.cwd.rstrip("/")) or "?"
        self.msgs.append(f"📱 answered {agent.name}/{proj}: {choice!r}")
        return err or f"✓ sent {choice!r} to {agent.name} in {proj}"


# ---------------------------------------------------------------- closing

def find_resume(agent):
    """Best-effort (resume_command, session_file) for a local agent, from the
    CLI's own session store, matched by working directory. Session files
    newer than the agent's last activity are skipped when older candidates
    exist — with several agents in one cwd, the newest file belongs to a
    different, possibly still-running session."""
    if agent.ssh or not agent.cwd:
        return None
    cutoff = (agent.scanned_at - agent.idle + 7200) if agent.idle >= 0 else None

    def ordered(files):
        files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        if cutoff is not None:
            good = [f for f in files if f.stat().st_mtime <= cutoff]
            if good:
                return good + [f for f in files if f not in good]
        return files

    home = Path.home()
    try:
        if agent.name == "claude":
            for munge in (agent.cwd.replace("/", "-"), re.sub(r"[/._]", "-", agent.cwd)):
                d = home / ".claude" / "projects" / munge
                if d.is_dir():
                    files = ordered(d.glob("*.jsonl"))
                    if files:
                        return (f"cd {shlex.quote(agent.cwd)} && "
                                f"claude --resume {files[0].stem}", str(files[0]))
        elif agent.name == "codex":
            root = home / ".codex" / "sessions"
            if root.is_dir():
                files = ordered(root.rglob("rollout-*.jsonl"))[:300]
                for f in files:
                    try:
                        head = f.open(errors="ignore").read(8192)
                    except OSError:
                        continue
                    if f'"cwd":"{agent.cwd}"' in head or f'"cwd": "{agent.cwd}"' in head:
                        uuid = f.stem[-36:]
                        return (f"cd {shlex.quote(agent.cwd)} && "
                                f"codex resume {uuid}", str(f))
    except OSError:
        pass
    return None


def ps_map():
    """{pid: (ppid, comm)} snapshot of all local processes."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    m = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 3:
            try:
                m[int(parts[0])] = (int(parts[1]), parts[2])
            except ValueError:
                pass
    return m


def local_chain(pid, m):
    """[(pid, ppid, comm)] from pid up to its topmost ancestor."""
    chain, cur = [], pid
    while cur in m and len(chain) < 10:
        ppid, comm = m[cur]
        chain.append((cur, ppid, comm))
        if ppid <= 1:
            break
        cur = ppid
    return chain


def descendants(pid, m):
    """All transitive children of pid in snapshot m."""
    kids = {}
    for p, (pp, _) in m.items():
        kids.setdefault(pp, []).append(p)
    out, stack = [], [pid]
    while stack:
        for k in kids.get(stack.pop(), []):
            out.append(k)
            stack.append(k)
    return out


def capture_screen(agent):
    try:
        if agent.pane:
            if agent.ssh:
                cmd = (f"tmux -S {shlex.quote(agent.sock)} capture-pane -p "
                       f"-t {shlex.quote(agent.pane)}")
                r = subprocess.run(SSH_BASE + [agent.ssh, cmd],
                                   capture_output=True, text=True, timeout=30)
            else:
                r = subprocess.run(["tmux", "capture-pane", "-p", "-t", agent.pane],
                                   capture_output=True, text=True, timeout=10)
            return r.stdout
        if agent.ssh and agent.mac:
            cmd = "osascript - " + shlex.quote(agent.prod_tty or agent.tty)
            r = subprocess.run(SSH_BASE + [agent.ssh, cmd], input=OSA_CAPTURE,
                               capture_output=True, text=True, timeout=40)
            return r.stdout
        if not agent.ssh and sys.platform == "darwin":
            # Local: Terminal.app and iTerm captured as SEPARATE scripts so a
            # Mac without iTerm never compiles iTerm terminology (the -2741 bug).
            tty = agent.prod_tty or agent.tty
            has_term, has_iterm = mac_terminals()
            for present, script in ((has_term, OSA_CAPTURE_TERM),
                                    (has_iterm, OSA_CAPTURE_ITERM)):
                if not present:
                    continue
                r = subprocess.run(["osascript", "-", tty], input=script,
                                   capture_output=True, text=True, timeout=25)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout
            return ""
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


ANSI_RX = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BUSY_SCREEN_RX = re.compile(
    r"\b(thinking|working|running|loading|processing|compiling|building|"
    r"installing|downloading|fetching)\b", re.I)


def screen_fingerprint(screen):
    """Stable current terminal view, ignoring escape codes and padding."""
    plain = ANSI_RX.sub("", screen)
    lines = [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines()]
    # Terminal.app returns full scrollback.  Only its current tail represents
    # what the agent is doing now; stale historical prompts must not veto a
    # safe nudge forever.
    return "\n".join(line for line in lines if line)[-30:][-4000:]


class SafeStaleTracker:
    """Require a quiet, unchanged terminal before an automatic nudge.

    A low-CPU agent alone is not enough: it may be waiting on a tool.  We
    additionally require that the visible terminal has not meaningfully
    changed for a configured interval and never nudge a displayed decision.
    """
    def __init__(self):
        self.samples = {}

    def ready(self, agent, cfg, now):
        key = (agent.host, agent.tty)
        if (agent.web or agent.protected or agent.policy == "ignore"
                or not agent.recognized):
            self.samples.pop(key, None)
            return False
        if agent.work_cpu > cfg["safe_stale_cpu"]:
            self.samples.pop(key, None)
            return False
        screen = capture_screen(agent)
        fingerprint = screen_fingerprint(screen)
        # An unreadable/empty screen is never evidence that it is safe to
        # interrupt.  Menus are handed to RemoteAnswerer instead.
        if (not fingerprint or PROMPT_RX.search(fingerprint)
                or extract_menu(fingerprint)[0]
                or BUSY_SCREEN_RX.search(fingerprint)):
            self.samples.pop(key, None)
            return False
        old = self.samples.get(key)
        if old is None or old["fingerprint"] != fingerprint:
            self.samples[key] = {"fingerprint": fingerprint, "since": now}
            return False
        return now - old["since"] >= cfg["safe_stale_after"]

    def fingerprint(self, agent):
        sample = self.samples.get((agent.host, agent.tty))
        return sample["fingerprint"] if sample else ""


def close_agent(agent, cfg):
    """Save resume command + screen tail to closed-agents.md, then terminate.
    Orphaned asciinema wrappers (window already gone) are killed along with
    the agent; attached ones keep their tab/shell alive."""
    if agent.web:
        return close_web_tab(agent, cfg)
    screen = capture_screen(agent)
    tail = "\n".join(l.rstrip() for l in screen.splitlines() if l.strip())
    tail = "\n".join(tail.splitlines()[-40:])
    resume = find_resume(agent)

    m = ps_map() if not agent.ssh else {}
    chain = local_chain(agent.pid, m) if not agent.ssh else []
    cast = ""
    wrap_idx = next((i for i, (_, _, c) in enumerate(chain) if "asciinema" in c), None)
    if wrap_idx == 0:
        wrap_idx = None     # the agent itself matched — shouldn't happen, be safe
    if wrap_idx is not None:
        try:
            args = subprocess.run(["ps", "-o", "args=", "-p", str(chain[wrap_idx][0])],
                                  capture_output=True, text=True, timeout=5).stdout
            mm = re.search(r"\S+\.cast\b", args)
            cast = mm.group(0) if mm else ""
        except (OSError, subprocess.SubprocessError):
            pass
    detached = wrap_idx is not None and chain[wrap_idx][1] <= 1

    log = Path(cfg["_dir"]) / "closed-agents.md"
    with open(log, "a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} — {agent.name} "
                f"in {agent.cwd or '?'} ({agent.host})\n\n")
        f.write(f"- pid {agent.pid}, tty {agent.tty}, "
                f"idle {fmt_age(agent.eff_idle())}"
                + (f", tmux {agent.tmux}" if agent.tmux else "")
                + (", window already closed (detached)" if detached else "") + "\n")
        if resume:
            f.write(f"- **resume:** `{resume[0]}`\n")
            f.write(f"- session file: `{resume[1]}`\n")
        else:
            f.write("- resume command not found"
                    + (" (remote host — look on that machine)" if agent.ssh else "")
                    + "\n")
        if cast:
            f.write(f"- asciinema recording: `{cast}`\n")
        if tail:
            f.write(f"\n```text\n{tail}\n```\n")

    if agent.ssh:
        subprocess.run(SSH_BASE + [agent.ssh, f"kill {agent.pid} 2>/dev/null; true"],
                       capture_output=True, text=True, timeout=30)
        survivors = []
    else:
        # kill the whole tree: helper children keep the tty (and the listing)
        # alive if only the parent dies; for detached sessions take the
        # orphaned wrapper and everything under it
        root = chain[wrap_idx][0] if detached else agent.pid
        kill_list = [root] + descendants(root, m)
        for p in kill_list:
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(1.2)
        for p in kill_list:
            try:
                os.kill(p, 0)
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.3)
        survivors = []
        for p in kill_list:
            try:
                os.kill(p, 0)
                survivors.append(p)
            except OSError:
                pass
    return (f"closed {agent.name} pid {agent.pid}"
            + (f" — WARNING pids still alive: {survivors}" if survivors else "")
            + (" — resume cmd saved" if resume else " — NO resume cmd found")
            + " → closed-agents.md")


def reopen_agent(agent, cfg):
    """For a detached agent (window gone): save + kill the zombie session,
    then open a fresh terminal window resuming the same conversation."""
    if agent.web:
        return f"web tab — just open {agent.cwd}"
    if agent.ssh:
        return "reopen only works for local agents"
    resume = find_resume(agent)
    if resume:
        cmd = resume[0]
    elif agent.name == "claude" and agent.cwd:
        cmd = f"cd {shlex.quote(agent.cwd)} && claude --continue"
    elif agent.name == "codex" and agent.cwd:
        cmd = f"cd {shlex.quote(agent.cwd)} && codex resume --last"
    else:
        return f"no resume path known for {agent.name} — closing only"
    msg = close_agent(agent, cfg)
    time.sleep(0.5)
    opened = open_terminal_window(cmd)
    return msg + ("; reopened in a new terminal window" if opened
                  else "; could NOT open a new terminal — resume cmd is in the log")


# The cd path deliberately excludes every shell metacharacter — this string is
# handed to `do script` / `write text`, i.e. executed in a shell, and the screen
# it is scraped from is UNTRUSTED (it is whatever the AI agent printed, which can
# be steered by a malicious repo file or prompt-injected content). A permissive
# path segment here is a remote-code-execution hole. See resume_from_screen.
RESUME_ON_SCREEN_RX = re.compile(
    r"(?:cd\s+[A-Za-z0-9_./~-]+\s*&&\s*)?"
    r"(?:codex\s+resume(?:\s+(?:--last|[A-Za-z0-9._-]+))?|"
    r"claude\s+--resume\s+[A-Za-z0-9._-]+)", re.I)

# Belt-and-braces: even after the tightened regex, refuse to execute anything
# carrying shell metacharacters. Nothing in a legitimate resume command needs
# them, so their presence means the match is not what we think it is.
_SHELL_META_RX = re.compile(r"[;&|`$(){}<>\n\r\\\"']")


def resume_from_screen(screen):
    """A CLI's Ctrl-C output may include the exact resume command to reuse.
    Returns it ONLY if it is metacharacter-free (the screen is untrusted and the
    result is executed in a shell); otherwise "" so the caller falls back to the
    trusted session store."""
    for m in reversed(RESUME_ON_SCREEN_RX.findall(screen)):
        cmd = m.strip()
        # `&&` is the one allowed connector (cd <path> && <cli>); strip it before
        # the metacharacter check so a stray single `&`, `|`, `$(…)`, backtick,
        # etc. is still rejected.
        if cmd and not _SHELL_META_RX.search(cmd.replace("&&", "")):
            return cmd
    return ""


def recover_agent(agent, cfg):
    """Interrupt a local attached agent, then restart it from its resume hint."""
    if agent.ssh or agent.web or agent.detached:
        return "recovery skipped — agent is not an attached local terminal"
    err = send_to_terminal(agent, "\x03", submit=False)
    if err:
        return f"recovery Ctrl-C failed: {err}"
    time.sleep(2)
    # Prefer the CLI's own session store (find_resume: shlex-quoted, trusted)
    # over anything scraped off the screen. resume_from_screen is only a
    # metacharacter-free fallback — the screen is untrusted and `cmd` is run in
    # a shell.
    resume = find_resume(agent)
    cmd = resume[0] if resume else resume_from_screen(capture_screen(agent))
    if not cmd:
        return "recovery stopped agent but found no resume command"
    if open_terminal_window(cmd):
        return f"recovered {agent.name} with `{cmd}`"
    return "recovery restart failed: could not open a new terminal window"


# ---------------------------------------------------------------- display

def fmt_age(secs):
    if secs < 0:
        return "?"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def trunc_path(path, width):
    return path if len(path) <= width else "…" + path[-(width - 1):]


def sparkline(buckets):
    # buckets[0] = current hour; draw oldest -> newest
    vals = list(reversed(buckets))
    peak = max(vals) or 1
    return "".join(SPARK_CHARS[min(8, round(v / peak * 8))] if v else SPARK_CHARS[0]
                   for v in vals)


def agent_state(agent, idle_after):
    if not agent.recognized:
        return "term"
    if agent.protected:
        return "prot"
    idle = agent.eff_idle()
    if idle < 0:
        return "unknown"
    return "run" if idle < idle_after else "stalled"


def agent_cell(p, idle_after, last_prod=None, prod_cooldown=0):
    if not p.agents:
        return ""
    a = primary_agent(p)
    st = agent_state(a, idle_after)
    tag = {"prot": "🔒", "run": "●", "stalled": "⚠", "unknown": "·",
           "term": "▹"}[st]
    if a.detached:
        tag = "⊗"
    if a.policy == "ignore":
        tag = "⊘"
    s = f"{tag}{a.name} {fmt_age(a.eff_idle())}"
    # Make automatic prodding observable: a stale agent displays the live
    # cooldown until its next eligible `continue!`, rather than silently
    # appearing ignored.
    sent_at = (last_prod or {}).get((a.host, a.pid), 0)
    remaining = max(0, prod_cooldown - (time.time() - sent_at)) if sent_at else 0
    if sent_at and remaining:
        s += f" next {fmt_age(remaining)}"
    elif sent_at:
        s += " next now"
    if len(p.agents) > 1:
        s += f" +{len(p.agents) - 1}"
    return s


def gather_rows(state, lock, sort_by, idle_after, policies):
    with lock:
        projects = [p for hs in state.values() for p in hs.projects.values()]
        agents = [a for hs in state.values() for a in hs.agents]
        hosts = {n: (hs.ok, hs.scanning, hs.error, hs.last_scan)
                 for n, hs in state.items()}
        apply_policies(agents, policies)
        for p in projects:
            p.agents = []
        pseudo = {}
        for a in agents:
            owner = None
            for p in projects:
                prefix = p.root.rstrip("/") + "/" + p.name
                if a.host == p.host and (a.cwd == prefix or a.cwd.startswith(prefix + "/")):
                    owner = p
                    break
            if owner is None and a.cwd:
                key = (a.host, a.cwd)
                if key not in pseudo:
                    if a.web:
                        pseudo[key] = Project(name=a.label or a.name, host=a.host,
                                              root=a.cwd,
                                              latest_file="(browser tab)",
                                              buckets=[0] * SPARK_BUCKETS)
                    else:
                        name = os.path.basename(a.cwd.rstrip("/")) or a.cwd
                        blank = "(open terminal)" if not a.recognized else "(no recent files)"
                        pseudo[key] = Project(name=name, host=a.host,
                                              root=os.path.dirname(a.cwd.rstrip("/")),
                                              latest_file=blank,
                                              buckets=[0] * SPARK_BUCKETS)
                owner = pseudo[key]
            elif owner is None and not a.recognized:
                key = (a.host, "__terminals__")
                if key not in pseudo:
                    pseudo[key] = Project(name="terminals", host=a.host, root="",
                                          latest_file="(open terminal windows)",
                                          buckets=[0] * SPARK_BUCKETS)
                owner = pseudo[key]
            if owner is not None:
                owner.agents.append(a)
        projects += list(pseudo.values())

    def stalled(p):
        return any(effective_stalled(a, idle_after) for a in p.agents)

    if sort_by == "recency":
        projects.sort(key=lambda p: (not stalled(p), -p.latest_mtime))
    else:
        projects.sort(key=lambda p: (not stalled(p),
                                     -p.counts.get("24h", 0), -p.latest_mtime))
    return projects, hosts, agents


def read_line(scr, prompt, initial=""):
    """Inline single-line input on the bottom row. Enter=confirm, ESC=cancel
    (returns None). Blocks the UI loop while typing."""
    h, w = scr.getmaxyx()
    buf = list(initial)
    curses.curs_set(1)
    scr.timeout(-1)
    try:
        while True:
            line = " " + prompt + "".join(buf)
            try:
                scr.addnstr(h - 1, 0, " " * (w - 1), w - 1)
                scr.addnstr(h - 1, 0, line[-(w - 2):], w - 2,
                            curses.A_BOLD)
            except curses.error:
                pass
            scr.refresh()
            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                return "".join(buf)
            if ch == "\x1b":
                return None
            if ch in ("\x7f", "\x08") or ch == curses.KEY_BACKSPACE:
                if buf:
                    buf.pop()
            elif isinstance(ch, str) and ch.isprintable():
                buf.append(ch)
    finally:
        scr.timeout(500)
        curses.curs_set(0)


def draw(scr, state, lock, sel, sel_key, sort_by, agent_cfg, ui_msg, policies,
         nudges, last_prod):
    now = time.time()
    idle_after = agent_cfg["idle_after"]
    scr.erase()
    h, w = scr.getmaxyx()
    projects, hosts, agents = gather_rows(state, lock, sort_by, idle_after, policies)
    # selection follows the project, not the list index — rows re-sort on
    # every refresh, and a positional cursor would land keys on the wrong row
    if sel_key is not None:
        for i, p in enumerate(projects):
            if (p.host, row_path(p)) == sel_key:
                sel = i
                break

    def put(y, x, text, attr=0):
        if 0 <= y < h and x < w:
            try:
                scr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:
                pass

    # header
    title = " prodtop "
    put(0, 0, title, curses.A_BOLD | curses.color_pair(4))
    x = len(title) + 1
    for name, (ok, scanning, err, last) in hosts.items():
        dot, pair = ("●", 1) if ok else ("●", 5) if err else ("○", 3)
        if scanning:
            dot = "◌"
        label = f"{name} {dot}"
        put(0, x, label, curses.color_pair(pair))
        x += len(label) + 2
    n_stalled = sum(1 for a in agents if effective_stalled(a, idle_after))
    ag_lbl = f"agents: {len(agents)}" + (f" ({n_stalled} stalled)" if n_stalled else "")
    put(0, x + 2, ag_lbl, curses.color_pair(5 if n_stalled else 3))
    clock = time.strftime("%H:%M:%S")
    put(0, max(x + 2 + len(ag_lbl) + 2, w - len(clock) - 1), clock, curses.color_pair(3))
    auto = "on" if agent_cfg["auto_prod"] else "off"
    put(1, 0, f" sort: {sort_by}  auto-prod: {auto}   [p] prod  [t] type msg  "
              f"[n] set nudge  [a] →PROD  [i] →leave  [x] close  [o] reopen  "
              f"[s] sort  [r] rescan  [q] quit",
        curses.color_pair(3))

    # column layout
    name_w, host_w, mode_w, agent_w, cnt_w = 22, 5, 6, 22, 6
    hdr = (f" {'PROJECT':<{name_w}} {'HOST':<{host_w}} {'MODE':<{mode_w}} "
           f"{'AGENT':<{agent_w}}"
           + "".join(f"{lbl:>{cnt_w}}" for lbl, _ in HORIZONS)
           + f"  {'ACTIVITY 24h':<{SPARK_BUCKETS}}  LAST FILE")
    put(3, 0, hdr, curses.A_BOLD | curses.color_pair(4))

    detail_h = 8
    list_top, list_bot = 4, h - detail_h - 1
    visible = max(1, list_bot - list_top)
    sel = max(0, min(sel, len(projects) - 1)) if projects else 0
    offset = max(0, sel - visible + 1)

    for i, p in enumerate(projects[offset:offset + visible]):
        y = list_top + i
        idx = offset + i
        age = now - p.latest_mtime if p.latest_mtime else -1
        has_stall = any(effective_stalled(a, idle_after) for a in p.agents)
        pair = 5 if has_stall else 1 if 0 <= age < 3600 else 2 if 0 <= age < 86400 else 3
        attr = curses.color_pair(pair)
        if idx == sel:
            attr |= curses.A_REVERSE
        if p.agents and any(a.protected for a in p.agents):
            mode = "prot"
        else:
            mode = ("PROD" if match_policy(p.host, row_path(p), policies) == "auto"
                    else "leave")
        line = (f" {p.name:<{name_w}.{name_w}} {p.host:<{host_w}} {mode:<{mode_w}}"
                f" {agent_cell(p, idle_after, last_prod, agent_cfg['prod_cooldown']):<{agent_w}.{agent_w}}"
                + "".join(f"{p.counts.get(lbl, 0):>{cnt_w}}" for lbl, _ in HORIZONS)
                + f"  {sparkline(p.buckets)}  "
                + f"{trunc_path(p.latest_file, 40)}"
                + (f" ({fmt_age(age)} ago)" if p.latest_mtime else ""))
        put(y, 0, line, attr)

    if not projects:
        put(list_top + 1, 2, "waiting for first scan...", curses.color_pair(3))

    # detail panel
    put(h - detail_h, 0, "─" * (w - 1), curses.color_pair(3))
    if projects:
        p = projects[sel]
        custom = match_policy(p.host, row_path(p), nudges, None)
        put(h - detail_h + 1, 1,
            f"{p.host}:{p.root}/{p.name}"
            + (f"  [nudge: {custom[:40]}]" if custom else "")
            + " — newest files:", curses.A_BOLD)
        for i, (mt, rel) in enumerate(p.recent[:detail_h - 4]):
            put(h - detail_h + 2 + i, 3,
                f"{fmt_age(now - mt):>4} ago  {rel}", curses.color_pair(3))
        for j, a in enumerate(p.agents[:2]):
            where = (f"tmux {a.tmux}" if a.tmux
                     else a.tty + (" DETACHED — [o] to reopen" if a.detached else ""))
            put(h - detail_h + 2 + min(len(p.recent), detail_h - 4) + j, 3,
                f"agent: {a.name} pid {a.pid} in {where}, "
                f"idle {fmt_age(a.eff_idle())}, cpu {a.cpu:.0f}% "
                f"(terminal {a.work_cpu:.0f}%)"
                + (" [protected]" if a.protected else "")
                + (" [left alone]" if a.policy == "ignore" else ""),
                curses.color_pair(2))
    # status / errors at the very bottom
    bottom = []
    if ui_msg:
        bottom.append(ui_msg)
    bottom += [f"{n}: {e[2]}" for n, e in hosts.items() if e[2]]
    if bottom:
        put(h - 1, 0, " " + " | ".join(bottom), curses.color_pair(5))
    scr.refresh()
    return sel, projects


def nudge_for(agent, agent_cfg, nudges):
    return (match_policy(agent.host, agent.cwd, nudges, None)
            or agent_cfg["nudge"]) if agent.cwd else agent_cfg["nudge"]


def prod_succeeded(msg):
    """Whether the nudge actually reached an agent terminal."""
    return msg.startswith(("prodded ", "approved prompt of "))


# ---------------------------------------------------- nudge experiment / log

def _empty_stat():
    return {"sent": 0, "productive": 0, "restalled": 0, "dropped": 0,
            "up": 0, "down": 0}


def stat_good(s):
    """'good' = prods that produced real work. Each prod counts once; a
    thumbs-up reclassifies its outcome to productive rather than adding on
    top (so the rate can never exceed 100%). up/down are kept only as a tally
    of how many outcomes you corrected by hand."""
    return s.get("productive", 0)


def stat_rate(s):
    return stat_good(s) / s["sent"] if s.get("sent") else 0.0


class ProdLog:
    """Records every nudge prodder sends and, a few minutes later, whether the
    agent produced real work — so the dashboard can rank phrasings and the
    bandit can prefer the ones that actually unstick agents.

    'good'  = files appeared in the project after the prod (or a thumbs-up).
    'restalled' = agent stayed put, no output, until the outcome window closed.
    'dropped'   = the agent vanished/was closed with nothing produced.
    Outcomes are heuristic (prodtop is a file-activity monitor, so file output
    is the signal); the thumbs up/down let you correct them by hand."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = Path(cfg["_dir"])
        self.events_path = self.dir / "prodder-events.jsonl"
        self.stats_path = self.dir / "prodder-stats.json"
        self.lock = threading.Lock()
        self.open_events = []                 # awaiting an outcome
        self.recent = deque(maxlen=50)        # newest first, for the dashboard
        self.by_id = {}
        self.stats = self._load_stats()
        self._n = 0

    def _load_stats(self):
        try:
            with open(self.stats_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_stats(self):
        try:
            with open(self.stats_path, "w") as f:
                json.dump(self.stats, f, indent=1)
        except OSError:
            pass

    def _append_event(self, ev):
        try:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass

    def pick(self, pool, epsilon):
        """Epsilon-greedy: explore at rate epsilon, else the best good-rate
        (smoothed so an untried phrasing starts optimistic and gets sampled)."""
        pool = [n for n in pool if n] or [self.cfg["agents"]["nudge"]]
        if random.random() < epsilon:
            return random.choice(pool)
        best, best_score = pool[0], -1.0
        for n in pool:
            s = self.stats.get(n, {})
            score = (stat_good(s) + 1) / (s.get("sent", 0) + 2)   # prior 0.5
            if score > best_score:
                best_score, best = score, n
        return best

    def record(self, agent, nudge, kind, auto):
        with self.lock:
            self._n += 1
            proj = os.path.basename(agent.cwd.rstrip("/")) or agent.host
            ev = {"id": f"e{self._n}", "t": time.time(), "host": agent.host,
                  "path": agent.cwd, "project": proj, "agent": agent.name,
                  "pid": agent.pid, "tty": agent.tty, "nudge": nudge,
                  "kind": kind, "auto": auto,
                  "idle_before": round(max(0.0, agent.eff_idle()), 1),
                  "outcome": None, "human": None}
            self.open_events.append(ev)
            self.recent.appendleft(ev)
            self.by_id[ev["id"]] = ev
            self.stats.setdefault(nudge, _empty_stat())["sent"] += 1
            self._append_event(ev)
            self._save_stats()
            return ev

    def _finish(self, ev, outcome):
        ev["outcome"] = outcome
        s = self.stats.setdefault(ev["nudge"], _empty_stat())
        s[outcome] = s.get(outcome, 0) + 1
        self._append_event({**ev, "resolved": True})
        self._save_stats()

    def resolve(self, proj_mtime, live, now, window):
        """proj_mtime: {(host, projpath): latest_mtime}; live: set of
        (host, pid) still running. Attribute each open prod's outcome."""
        with self.lock:
            for ev in list(self.open_events):
                if ev["outcome"] is not None:
                    self.open_events.remove(ev)
                    continue
                produced = any(
                    h == ev["host"] and (ev["path"] == pp
                                         or ev["path"].startswith(pp + "/")
                                         or pp.startswith(ev["path"]))
                    and mt > ev["t"] + 2
                    for (h, pp), mt in proj_mtime.items())
                alive = (ev["host"], ev["pid"]) in live
                age = now - ev["t"]
                if produced:
                    self._finish(ev, "productive")
                elif not alive and age > 15:
                    self._finish(ev, "dropped")
                elif age >= window:
                    self._finish(ev, "restalled" if alive else "dropped")
                else:
                    continue
                self.open_events.remove(ev)

    def feedback(self, event_id, good):
        """Thumbs re-label an outcome: up -> productive, down -> restalled.
        Reclassifying (not adding) keeps each prod counted exactly once."""
        with self.lock:
            ev = self.by_id.get(event_id)
            if not ev:
                return False
            s = self.stats.setdefault(ev["nudge"], _empty_stat())
            target = "productive" if good else "restalled"
            cur = ev.get("outcome")
            if cur != target:
                if cur in ("productive", "restalled", "dropped"):
                    s[cur] = max(0, s.get(cur, 0) - 1)
                s[target] = s.get(target, 0) + 1
                ev["outcome"] = target
                if ev in self.open_events:
                    self.open_events.remove(ev)   # a hand-labelled prod is resolved
            prev = ev.get("human")
            if prev is True:
                s["up"] = max(0, s.get("up", 0) - 1)
            elif prev is False:
                s["down"] = max(0, s.get("down", 0) - 1)
            ev["human"] = bool(good)
            s["up" if good else "down"] += 1
            self._append_event({**ev, "feedback": True})
            self._save_stats()
            return True

    def leaderboard(self):
        with self.lock:
            rows = []
            for nudge, s in self.stats.items():
                rows.append({"nudge": nudge, "sent": s.get("sent", 0),
                             "good": stat_good(s), "rate": stat_rate(s),
                             "productive": s.get("productive", 0),
                             "restalled": s.get("restalled", 0),
                             "dropped": s.get("dropped", 0),
                             "up": s.get("up", 0), "down": s.get("down", 0)})
            rows.sort(key=lambda r: (-r["rate"], -r["sent"]))
            return rows

    def recent_events(self, n=12):
        with self.lock:
            return [dict(e) for e in list(self.recent)[:n]]


def choose_nudge(agent, agent_cfg, nudges, screen, prodlog):
    """Which phrasing to send, and why. Custom per-project nudge wins; a screen
    that looks finished/awaiting-a-decision gets the reassess off-ramp; else the
    bandit picks from the pool."""
    custom = match_policy(agent.host, agent.cwd, nudges, None) if agent.cwd else None
    if custom:
        return custom, "custom"
    if screen and looks_done(screen):
        return agent_cfg["reassess_nudge"], "reassess"
    pool = agent_cfg.get("nudge_pool") or [agent_cfg["nudge"]]
    if prodlog is not None:
        return prodlog.pick(pool, agent_cfg.get("bandit_epsilon", 0.15)), "pool"
    return pool[0], "pool"


def experiment_prod(agent, cfg, nudges, prodlog, auto=False):
    """Prod an agent with an experiment-chosen nudge and log the outcome.
    Falls back to the plain default for web tabs (no idle/output signal)."""
    agent_cfg = cfg["agents"]
    if agent.protected:
        return prod_agent(agent, "", agent_cfg["approve_prompts"],
                          agent_cfg["never_approve"])
    if agent.web:
        return prod_agent(agent, nudge_for(agent, agent_cfg, nudges or {}),
                          agent_cfg["approve_prompts"], agent_cfg["never_approve"])
    screen = capture_screen(agent)
    text, kind = choose_nudge(agent, agent_cfg, nudges or {}, screen, prodlog)
    msg = prod_agent(agent, text, agent_cfg["approve_prompts"],
                     agent_cfg["never_approve"], screen=screen)
    if prodlog is not None and prod_succeeded(msg) and "approved prompt" not in msg:
        prodlog.record(agent, text, kind, auto)
    return msg


def auto_prod_pass(agents, agent_cfg, last_prod, msgs,
                   pending_remote=None, remote_timeout=300, nudges=None,
                   stale_tracker=None, recovery_attempts=None, cfg=None,
                   prodlog=None):
    now = time.time()
    recovery_attempts = recovery_attempts if recovery_attempts is not None else {}
    for a in agents:
        if stale_tracker is not None:
            is_stale = stale_tracker.ready(a, agent_cfg, now)
        else:
            is_stale = effective_stalled(a, agent_cfg["idle_after"])
        if not is_stale:
            continue
        if pending_remote and now - pending_remote.get((a.host, a.tty), 0) \
                < remote_timeout:
            continue        # a human was asked on the phone — hold off
        key = (a.host, a.pid)
        fingerprint = stale_tracker.fingerprint(a) if stale_tracker else ""
        prior = recovery_attempts.get(key)
        if prior and prior["fingerprint"] != fingerprint:
            recovery_attempts.pop(key, None)  # visible progress reset recovery
            prior = None
        if now - last_prod.get(key, 0) < agent_cfg["prod_cooldown"]:
            continue
        if prior and prior["attempts"] >= agent_cfg["auto_restart_attempts"]:
            msg = recover_agent(a, cfg)
            recovery_attempts.pop(key, None)
            last_prod[key] = now
            msgs.append(f"[auto {time.strftime('%H:%M')}] " + msg)
            continue
        msg = experiment_prod(a, cfg, nudges or {}, prodlog, auto=True)
        # an approved prompt usually precedes more work (or another prompt) —
        # recheck soon instead of waiting out the full cooldown
        if prod_succeeded(msg):
            last_prod[key] = (now - agent_cfg["prod_cooldown"] + 90
                              if "approved prompt" in msg else now)
            recovery_attempts[key] = {
                "fingerprint": fingerprint,
                "attempts": (prior["attempts"] + 1 if prior else 1),
            }
        msgs.append(f"[auto {time.strftime('%H:%M')}] " + msg)


class AutoProdder(threading.Thread):
    """Run expensive stale detection away from the curses/UI thread."""
    def __init__(self, cfg, state, state_lock, policies, nudges,
                 pending_remote, last_prod, msgs, prodlog=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.state, self.state_lock = state, state_lock
        self.policies, self.nudges = policies, nudges
        self.pending_remote, self.last_prod, self.msgs = (
            pending_remote, last_prod, msgs)
        self.prodlog = prodlog
        self.stale_tracker = SafeStaleTracker()
        self.recovery_attempts = {}
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def _resolve_outcomes(self, now):
        """Attribute outcomes to prods whose window has matured."""
        if self.prodlog is None:
            return
        with self.state_lock:
            proj_mtime, live = {}, set()
            for hs in self.state.values():
                for p in hs.projects.values():
                    proj_mtime[(p.host, row_path(p))] = p.latest_mtime
                for a in hs.agents:
                    live.add((a.host, a.pid))
        self.prodlog.resolve(proj_mtime, live, now,
                             self.cfg["agents"].get("outcome_window", 240))

    def run(self):
        agent_cfg, remote_cfg = self.cfg["agents"], self.cfg["remote"]
        while not self.stopping.is_set():
            try:
                self._resolve_outcomes(time.time())
                if agent_cfg["auto_prod"]:
                    with self.state_lock:
                        agents = [a for hs in self.state.values() for a in hs.agents]
                        apply_policies(agents, self.policies)
                    auto_prod_pass(agents, agent_cfg, self.last_prod, self.msgs,
                                   self.pending_remote, remote_cfg["remote_timeout"],
                                   self.nudges, self.stale_tracker,
                                   self.recovery_attempts, self.cfg, self.prodlog)
            except Exception as e:
                self.msgs.append(f"auto-prod: {str(e)[:80]}")
            interval = max(1.0, float(agent_cfg["auto_interval"]))
            self.stopping.wait(interval)


# ------------------------------------------------------------------- demo

# A fabricated fleet so a first-time user sees the whole thing — dashboard,
# stalls, auto-prods, the Nudge Lab filling in — with no config, no ssh, no
# real agents. Each nudge has a hidden "true" success rate; the bandit is left
# to discover it, so the leaderboard tells the product's own story live.
DEMO_HOSTS = ["laptop", "gpu-box", "vps"]
DEMO_PROJECTS = [
    ("laptop", "checkout-flow", True, "PROD"),
    ("laptop", "api-gateway", True, "PROD"),
    ("laptop", "design-system", True, "leave"),
    ("laptop", "marketing-site", False, "leave"),
    ("gpu-box", "train-ranker", True, "PROD"),
    ("gpu-box", "eval-harness", False, "leave"),
    ("vps", "billing-worker", True, "PROD"),
    ("vps", "cron-scripts", False, "leave"),
]
DEMO_QUALITY = {                      # hidden truth the bandit has to find
    "Please keep going with the current plan.": 0.85,
    "reassess": 0.80,
    "continue!": 0.62,
    "What's the next concrete step? Do it.": 0.55,
    "continue": 0.42,
    "Proceed.": 0.30,
}
DEMO_FILES = ["src/router.ts", "app/models.py", "tests/test_flow.py",
              "README.md", "lib/pipeline.py", "components/Button.tsx",
              "train.py", "worker/jobs.py", "config.yaml", "utils/io.rs"]
DEMO_SCREENS = ["editing files, working through the plan",
                "running the test suite", "refactoring the module"]
DEMO_DONE = ["All done! Let me know if you'd like anything else.",
             "Task complete. Shall I deploy?"]


class DemoDriver(threading.Thread):
    """Evolves a fake fleet and simulates prods + outcomes on a fast clock."""

    def __init__(self, cfg, state, lock, policies, nudges, last_prod, msgs,
                 prodlog):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.state, self.lock = state, lock
        self.policies, self.nudges = policies, nudges
        self.last_prod, self.msgs, self.prodlog = last_prod, msgs, prodlog
        self.stopping = threading.Event()
        self.pid = 4000
        self._build()

    def stop(self):
        self.stopping.set()

    def _rand_buckets(self):
        return [random.choice([0, 0, 0, 1, 1, 2, 3, 5]) for _ in range(SPARK_BUCKETS)]

    def _build(self):
        now = time.time()
        for h in DEMO_HOSTS:
            self.state[h] = HostState(name=h, ok=True, last_scan=now)
        for host, name, has_agent, mode in DEMO_PROJECTS:
            root = {"laptop": "/Users/you/Projects", "gpu-box": "/home/you/ml",
                    "vps": "/srv"}[host]
            buckets = self._rand_buckets()
            p = Project(name=name, host=host, root=root,
                        latest_mtime=now - random.randint(20, 4000),
                        latest_file=random.choice(DEMO_FILES),
                        counts={"15m": buckets[0], "1h": sum(buckets[:2]),
                                "24h": sum(buckets), "3d": sum(buckets) + random.randint(0, 40)},
                        buckets=buckets,
                        recent=[(now - i * 180 - 30, random.choice(DEMO_FILES))
                                for i in range(4)])
            self.policies[f"{host}|{root.rstrip('/')}/{name}"] = (
                "auto" if mode == "PROD" else "ignore")
            if has_agent:
                self.pid += 1
                a = AgentProc(host=host, ssh="", pid=self.pid, name=random.choice(
                    ["claude", "codex"]), cwd=f"{root}/{name}",
                    tty=f"/dev/ttys{self.pid}", cpu=random.choice([0.0, 2.0, 8.0]),
                    idle=float(random.randint(0, 30)), scanned_at=now)
                self.state[host].agents.append(a)
                p.agents.append(a)
            self.state[host].projects[name] = p

    def _agents(self):
        return [a for hs in self.state.values() for a in hs.agents]

    def _tick(self):
        now = time.time()
        acfg = self.cfg["agents"]
        with self.lock:
            for hs in self.state.values():
                hs.last_scan = now
            for a in self._agents():
                a.scanned_at = now
                a.idle += random.uniform(1.5, 3.5)
                # projects that are "working" keep producing files
                proj = self._proj_for(a)
                if a.idle < acfg["idle_after"] and proj and random.random() < 0.4:
                    self._produce(proj, now)
            if not acfg["auto_prod"]:
                return
            apply_policies(self._agents(), self.policies)
            for a in self._agents():
                if a.policy == "ignore" or a.idle < acfg["idle_after"]:
                    continue
                key = (a.host, a.pid)
                if now - self.last_prod.get(key, 0) < acfg["prod_cooldown"]:
                    continue
                self._prod(a, now, auto=True)
            self._resolve(now)

    def _proj_for(self, a):
        for p in self.state[a.host].projects.values():
            if a.cwd.endswith("/" + p.name):
                return p
        return None

    def _produce(self, proj, now):
        proj.latest_mtime = now
        proj.latest_file = random.choice(DEMO_FILES)
        proj.buckets = proj.buckets[:-1]
        proj.buckets.insert(0, proj.buckets[0] + 1)
        for k in ("15m", "1h", "24h", "3d"):
            proj.counts[k] = proj.counts.get(k, 0) + 1
        proj.recent.insert(0, (now, proj.latest_file))
        proj.recent = proj.recent[:8]

    def _prod(self, a, now, auto):
        acfg = self.cfg["agents"]
        done = random.random() < 0.18
        screen = random.choice(DEMO_DONE if done else DEMO_SCREENS)
        text, kind = choose_nudge(a, acfg, self.nudges, screen, self.prodlog)
        self.prodlog.record(a, text, kind, auto)
        self.last_prod[(a.host, a.pid)] = now
        quality = DEMO_QUALITY.get("reassess" if kind == "reassess" else text, 0.5)
        proj = self._proj_for(a)
        if random.random() < quality:               # nudge worked
            a.idle = float(random.randint(0, 3))
            a.cpu = random.choice([4.0, 9.0])
            if proj:
                self._produce(proj, now + 1)         # output lands just after
        else:                                        # stayed stuck
            a.idle = float(acfg["idle_after"] + random.randint(2, 8))
        verb = "reassessed" if kind == "reassess" else "prodded"
        self.msgs.append(f"[demo {time.strftime('%H:%M:%S')}] {verb} "
                         f"{a.name}/{proj.name if proj else a.host} — \"{text[:32]}\"")

    def _resolve(self, now):
        proj_mtime, live = {}, set()
        for hs in self.state.values():
            for p in hs.projects.values():
                proj_mtime[(p.host, row_path(p))] = p.latest_mtime
            for a in hs.agents:
                live.add((a.host, a.pid))
        self.prodlog.resolve(proj_mtime, live, now,
                             self.cfg["agents"].get("outcome_window", 15))

    def act(self, action, a=None, proj=None):
        """Simulate a dashboard action without touching the real system."""
        now = time.time()
        with self.lock:
            if action == "prod" and a:
                self._prod(a, now, auto=False)
                return f"prodded {a.name} (demo)"
            if action == "type" and a:
                a.idle = 0.0
                return f"sent keystrokes to {a.name} (demo)"
            if action in ("close", "reopen") and a:
                for hs in self.state.values():
                    hs.agents = [x for x in hs.agents if x.pid != a.pid]
                return f"{action}d {a.name} (demo)"
        return "ok"

    def run(self):
        # warm the leaderboard so the lab isn't empty on first paint
        with self.lock:
            for a in self._agents():
                for _ in range(random.randint(2, 5)):
                    self._prod(a, time.time() - random.randint(30, 400), auto=True)
            self._resolve(time.time())
        while not self.stopping.is_set():
            try:
                self._tick()
            except Exception as e:
                self.msgs.append(f"demo: {str(e)[:80]}")
            self.stopping.wait(2.5)


class Engine:
    """The shared runtime behind every interface: per-host scanner threads,
    the auto-prodder, and the phone answerer. UIs (curses TUI, web) only
    read state and call actions. With demo=True, real scanning/prodding is
    replaced by a self-contained simulation (see DemoDriver)."""

    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.state, self.lock = {}, threading.Lock()
        self.wake = threading.Event()
        self.scanners = []
        self.last_prod, self.msgs = {}, deque(maxlen=30)
        self.policies, self.nudges = ({}, {}) if demo else load_state(cfg)
        self.prodlog = ProdLog(cfg)
        self.pending_remote = {}
        self.remote_thread = None
        if demo:
            self.driver = DemoDriver(cfg, self.state, self.lock, self.policies,
                                     self.nudges, self.last_prod, self.msgs,
                                     self.prodlog)
            return
        for hc in cfg["hosts"]:
            self.state[hc["name"]] = HostState(name=hc["name"])
            self.scanners.append(Scanner(hc, cfg["settings"], cfg["agents"],
                                         self.state, self.lock, self.wake))
        if cfg["remote"]["enabled"] and (cfg["remote"]["telegram_token"]
                                         or cfg["remote"]["imessage_handle"]):
            self.remote_thread = RemoteAnswerer(cfg, self.state, self.lock,
                                                self.msgs, self.pending_remote,
                                                self.policies)
        self.auto_thread = AutoProdder(cfg, self.state, self.lock,
                                       self.policies, self.nudges,
                                       self.pending_remote, self.last_prod,
                                       self.msgs, self.prodlog)

    def start(self):
        if self.demo:
            self.driver.start()
            return
        for t in self.scanners:
            t.start()
        if self.remote_thread:
            self.remote_thread.start()
        self.auto_thread.start()

    def stop(self):
        if self.demo:
            self.driver.stop()
            return
        for t in self.scanners:
            t.stop = True
        self.auto_thread.stop()
        if self.remote_thread:
            self.remote_thread.stop = True
        self.wake.set()

    def drop_agent(self, a):
        with self.lock:
            for hs in self.state.values():
                hs.agents = [x for x in hs.agents
                             if (x.host, x.tty) != (a.host, a.tty)]


def tui(cfg):
    if curses is None:
        sys.exit("--tui needs the 'curses' module, which isn't available on "
                 "this platform (native Windows). Use the web dashboard "
                 "instead: run prodder with no flags, or `prodder --demo`.")
    eng = Engine(cfg)
    eng.start()
    state, lock, wake = eng.state, eng.lock, eng.wake
    last_prod, msgs = eng.last_prod, eng.msgs
    policies, nudges = eng.policies, eng.nudges

    def main(scr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        for i, c in [(1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW),
                     (3, 8 if curses.COLORS > 8 else curses.COLOR_WHITE),
                     (4, curses.COLOR_CYAN), (5, curses.COLOR_RED)]:
            curses.init_pair(i, c, -1)
        scr.timeout(500)
        sel = 0
        sel_key = None      # (host, row_path) of the selected row
        sort_by = "recency"
        pending = None      # ("close"|"reopen", agent)

        def sel_agent(rows):
            if rows and rows[sel].agents:
                return rows[sel], primary_agent(rows[sel])
            if rows:
                msgs.append(f"no agent running in {rows[sel].name}")
            return None, None

        drop_from_state = eng.drop_agent

        while True:
            ui_msg = msgs[-1] if msgs else ""
            sel, rows = draw(scr, state, lock, sel, sel_key, sort_by,
                             cfg["agents"], ui_msg, policies, nudges, last_prod)
            if rows:
                sel_key = (rows[sel].host, row_path(rows[sel]))
            ch = scr.getch()
            if pending is not None and ch != -1:
                action, a = pending
                pending = None
                if ch in (ord("y"), ord("Y")):
                    fn = close_agent if action == "close" else reopen_agent
                    msgs.append(f"[{time.strftime('%H:%M')}] " + fn(a, cfg))
                    drop_from_state(a)
                    wake.set()
                else:
                    msgs.append(f"{action} cancelled")
                continue
            if ch in (ord("q"), 27):
                break
            elif ch in (ord("j"), curses.KEY_DOWN):
                sel = min(sel + 1, max(0, len(rows) - 1))
                if rows:
                    sel_key = (rows[sel].host, row_path(rows[sel]))
            elif ch in (ord("k"), curses.KEY_UP):
                sel = max(sel - 1, 0)
                if rows:
                    sel_key = (rows[sel].host, row_path(rows[sel]))
            elif ch == ord("s"):
                sort_by = "24h" if sort_by == "recency" else "recency"
            elif ch == ord("r"):
                wake.set()
            elif ch == ord("p"):
                _, a = sel_agent(rows)
                if a:
                    msg = experiment_prod(a, cfg, nudges, eng.prodlog)
                    msgs.append(f"[{time.strftime('%H:%M')}] " + msg)
                    if prod_succeeded(msg):
                        last_prod[(a.host, a.pid)] = time.time()
            elif ch == ord("i") and rows:
                p = rows[sel]
                policies[f"{p.host}|{row_path(p)}"] = "ignore"
                save_state(cfg, policies, nudges)
                msgs.append(f"{p.name}: MODE = leave (never prodded)")
            elif ch == ord("a") and rows:
                p = rows[sel]
                key = f"{p.host}|{row_path(p)}"
                policies.pop(key, None)
                # a broader prefix policy (e.g. a whole-home "leave") may
                # still match — pin an explicit auto so most-specific wins
                if match_policy(p.host, row_path(p), policies) != "auto":
                    policies[key] = "auto"
                save_state(cfg, policies, nudges)
                msgs.append(f"{p.name}: MODE = PROD (stalled agents here get prodded)")
            elif ch == ord("n") and rows:
                p = rows[sel]
                key = f"{p.host}|{row_path(p)}"
                txt = read_line(scr, f"nudge for {p.name} (Enter empty = "
                                     f"default '{cfg['agents']['nudge']}', "
                                     f"ESC = cancel): ",
                                nudges.get(key, ""))
                if txt is None:
                    msgs.append("nudge unchanged")
                elif not txt.strip():
                    nudges.pop(key, None)
                    save_state(cfg, policies, nudges)
                    msgs.append(f"{p.name}: nudge reset to default "
                                f"'{cfg['agents']['nudge']}'")
                else:
                    nudges[key] = txt.strip()
                    save_state(cfg, policies, nudges)
                    msgs.append(f"{p.name}: nudge = \"{txt.strip()[:50]}\"")
            elif ch == ord("t"):
                p, a = sel_agent(rows)
                if a:
                    txt = read_line(scr, f"type into {a.name}/{p.name} "
                                         f"(Enter = send, ESC = cancel): ")
                    if txt and txt.strip():
                        err = send_to_terminal(a, txt.strip(), submit=True)
                        msgs.append(err or f"sent \"{txt.strip()[:40]}\" "
                                           f"to {a.name}/{p.name}")
                        last_prod[(a.host, a.pid)] = time.time()
                    else:
                        msgs.append("nothing sent")
            elif ch == ord("x"):
                p, a = sel_agent(rows)
                if a and a.protected:
                    msgs.append(f"'{a.tmux}' is protected fleet infra — not closing")
                elif a:
                    pending = ("close", a)
                    msgs.append(f"close {a.name} pid {a.pid} in {p.name}? "
                                f"saves resume info first — press y to confirm")
            elif ch == ord("o"):
                p, a = sel_agent(rows)
                if a and a.protected:
                    msgs.append(f"'{a.tmux}' is protected fleet infra — not touching")
                elif a and not a.detached:
                    msgs.append(f"{a.name} still has a live terminal — "
                                f"use p to prod or x to close")
                elif a:
                    pending = ("reopen", a)
                    msgs.append(f"reopen {a.name} in {p.name}? kills the detached "
                                f"session and resumes it in a new window — "
                                f"press y to confirm")

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass          # quit-by-Ctrl-C should exit cleanly, not dump a traceback
    finally:
        eng.stop()


# ---------------------------------------------------------------- web UI

from prodtop_web import WEB_PAGE


def demo_config():
    """A throwaway config for --demo: fast clock, no hosts, a temp state dir
    so the demo never reads or writes your real prodder files."""
    d = tempfile.mkdtemp(prefix="prodder-demo-")
    toml = ("[settings]\n"
            "[agents]\nidle_after = 8\nauto_prod = true\nprod_cooldown = 8\n"
            "outcome_window = 14\nbandit_epsilon = 0.2\n"
            "[remote]\nenabled = false\n"
            "[web]\nport = 8737\n")
    p = Path(d) / "demo.toml"
    p.write_text(toml)
    return load_config(str(p))


def web(cfg, open_browser=True, demo=False):
    """Serve the dashboard on localhost and run the engine behind it."""
    eng = Engine(cfg, demo=demo)
    eng.start()
    agent_cfg = cfg["agents"]
    port = cfg["web"]["port"]

    # Per-session token gating state-changing actions. The X-Prodder header only
    # stops cross-site browsers; it does nothing against another local process
    # (or another user on a shared box) that can POST to /api/action and type
    # into your agent terminals. The token is injected into the page we serve
    # (so the browser has it transparently) and written 0600 for the native app
    # / CLI to read. Read-only /api/state stays open (Host + cross-site guarded).
    api_key = secrets.token_urlsafe(24)
    key_path = Path(cfg.get("_dir", ".")) / "prodder-token"
    try:
        key_path.write_text(api_key)
        os.chmod(key_path, 0o600)
    except OSError:
        pass

    def snapshot(sort_by):
        projects, hosts, agents = gather_rows(
            eng.state, eng.lock, sort_by, agent_cfg["idle_after"], eng.policies)
        now = time.time()
        rows = []
        for p in projects:
            path = row_path(p)
            arows = []
            for a in sorted(p.agents, key=lambda x: x.pid):
                sent = eng.last_prod.get((a.host, a.pid), 0)
                nxt = max(0, agent_cfg["prod_cooldown"] - (now - sent)) if sent else 0
                arows.append({
                    "name": a.name, "pid": a.pid, "host": a.host, "tty": a.tty,
                    "idle": round(a.eff_idle(), 1), "cpu": round(a.work_cpu),
                    "stalled": effective_stalled(a, agent_cfg["idle_after"]),
                    "policy": a.policy, "protected": a.protected,
                    "detached": a.detached, "web": a.web,
                    "recognized": a.recognized,
                    "where": (f"tmux {a.tmux}" if a.tmux else
                              a.label if a.web else a.prod_tty or a.tty),
                    "next_prod": round(nxt),
                })
            rows.append({
                "name": p.name, "host": p.host, "path": path,
                "mode": ("PROD" if match_policy(p.host, path, eng.policies)
                         == "auto" else "leave"),
                "nudge": match_policy(p.host, path, eng.nudges, None),
                "counts": p.counts, "buckets": p.buckets or [0] * SPARK_BUCKETS,
                "latest_file": trunc_path(p.latest_file, 44),
                "latest_age": (round(now - p.latest_mtime)
                               if p.latest_mtime else -1),
                "recent": [[round(now - mt), rel] for mt, rel in p.recent[:10]],
                "agents": arows,
            })
        stalled = sum(1 for a in agents if not a.web and a.policy != "ignore"
                      and not a.protected
                      and effective_stalled(a, agent_cfg["idle_after"]))
        events = []
        for e in eng.prodlog.recent_events(14):
            events.append({"id": e["id"], "age": round(now - e["t"]),
                           "project": e["project"], "host": e["host"],
                           "nudge": e["nudge"], "kind": e["kind"],
                           "auto": e["auto"], "outcome": e["outcome"],
                           "human": e["human"]})
        return {
            "rows": rows,
            "hosts": {n: {"ok": ok, "scanning": sc, "error": err,
                          "age": round(now - ls) if ls else -1}
                      for n, (ok, sc, err, ls) in hosts.items()},
            "agent_count": len(agents), "stalled_count": stalled,
            "auto_prod": agent_cfg["auto_prod"],
            "msgs": list(eng.msgs), "default_nudge": agent_cfg["nudge"],
            "leaderboard": eng.prodlog.leaderboard(),
            "events": events,
        }

    def find_agent(host, tty):
        with eng.lock:
            for hs in eng.state.values():
                for a in hs.agents:
                    if a.host == host and a.tty == tty:
                        apply_policies([a], eng.policies)
                        return a
        return None

    KNOWN_ACTIONS = {"rescan", "autoprod", "mode", "nudge", "prod", "type",
                     "close", "reopen", "feedback"}

    def do_action(q):
        act = q.get("action")
        if act not in KNOWN_ACTIONS:
            return "unknown action"
        if act == "feedback":
            ok = eng.prodlog.feedback(str(q.get("id", "")), bool(q.get("good")))
            return "thanks — recorded" if ok else "prod not found"
        if act == "rescan":
            eng.wake.set()
            return "rescanning"
        if act == "autoprod":
            agent_cfg["auto_prod"] = bool(q.get("value"))
            return f"auto-prod {'on' if agent_cfg['auto_prod'] else 'off'}"
        if act == "mode":
            key = f"{q['host']}|{q['path']}"
            if q.get("value") == "ignore":
                eng.policies[key] = "ignore"
                msg = "MODE = leave (never prodded)"
            else:
                eng.policies.pop(key, None)
                if match_policy(q["host"], q["path"], eng.policies) != "auto":
                    eng.policies[key] = "auto"
                msg = "MODE = PROD (stalled agents here get prodded)"
            save_state(cfg, eng.policies, eng.nudges)
            return f"{os.path.basename(q['path'])}: {msg}"
        if act == "nudge":
            key = f"{q['host']}|{q['path']}"
            txt = (q.get("text") or "").strip()
            if txt:
                eng.nudges[key] = txt
                msg = f'nudge = "{txt[:50]}"'
            else:
                eng.nudges.pop(key, None)
                msg = f"nudge reset to default '{agent_cfg['nudge']}'"
            save_state(cfg, eng.policies, eng.nudges)
            return f"{os.path.basename(q['path'])}: {msg}"
        a = find_agent(q.get("host"), q.get("tty"))
        if a is None:
            return "agent is gone — rescan"
        if eng.demo:                       # never touch the real system in a demo
            return eng.driver.act(act, a)
        if act == "prod":
            msg = experiment_prod(a, cfg, eng.nudges, eng.prodlog)
            if prod_succeeded(msg):
                eng.last_prod[(a.host, a.pid)] = time.time()
            return msg
        if act == "type":
            txt = (q.get("text") or "").strip()
            if not txt:
                return "nothing sent"
            err = send_to_terminal(a, txt, submit=True)
            eng.last_prod[(a.host, a.pid)] = time.time()
            return err or f'sent "{txt[:40]}" to {a.name}'
        if act == "close":
            if a.protected:
                return f"'{a.tmux}' is protected fleet infra — not closing"
            msg = close_agent(a, cfg)
            eng.drop_agent(a)
            eng.wake.set()
            return msg
        if act == "reopen":
            if a.protected:
                return f"'{a.tmux}' is protected fleet infra — not touching"
            if not a.detached:
                return f"{a.name} still has a live terminal — prod or close it"
            msg = reopen_agent(a, cfg)
            eng.drop_agent(a)
            eng.wake.set()
            return msg
        return "unknown action"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self):
            # Reject any Host that isn't our own loopback name. A DNS-rebinding
            # page (attacker.com -> 127.0.0.1) is same-origin to the browser,
            # so its fetch may carry X-Prodder; the Host header still reads
            # "attacker.com" and is refused here. Legit local access sends
            # 127.0.0.1/localhost.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host in ("127.0.0.1", "localhost", "::1", ""):
                return True
            self._send(403, "bad host", "text/plain")
            return False

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                self._send(200, WEB_PAGE.replace("__PRODDER_KEY__", api_key),
                           "text/html")
            elif url.path == "/api/state":
                sort_by = urllib.parse.parse_qs(url.query).get(
                    "sort", ["recency"])[0]
                try:
                    self._send(200, json.dumps(snapshot(sort_by)))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)[:200]}))
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path != "/api/action":
                return self._send(404, "not found", "text/plain")
            # same-origin only: browsers can't add this header cross-site
            # without a CORS preflight, which we never answer
            if self.headers.get("X-Prodder") != "1":
                return self._send(403, "forbidden", "text/plain")
            # Per-session token: blocks other local processes/users that could
            # otherwise POST actions (typing into your agent terminals).
            if not secrets.compare_digest(
                    self.headers.get("X-Prodder-Key", ""), api_key):
                return self._send(403, "forbidden", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", 0))
                q = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except (ValueError, OSError):
                return self._send(400, "bad request", "text/plain")
            if q.get("action") == "quit":
                self._send(200, json.dumps({"msg": "bye"}))
                threading.Thread(target=srv.shutdown, daemon=True).start()
                return
            try:
                msg = do_action(q)
            except Exception as e:
                msg = f"action failed: {str(e)[:120]}"
            eng.msgs.append(f"[{time.strftime('%H:%M')}] {msg}")
            self._send(200, json.dumps({"msg": msg}))

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        eng.stop()
        sys.exit(f"prodder: can't bind 127.0.0.1:{port} ({e}). Is prodder "
                 f"already running? Pick another with --port <n>.")
    url = f"http://127.0.0.1:{port}"
    print(f"prodder dashboard: {url}", file=sys.stderr)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
        srv.server_close()


def once(cfg):
    """One-shot text mode: scan everything, print tables, exit. Never prods."""
    settings, agent_cfg = cfg["settings"], cfg["agents"]
    window = settings["window_days"] * 86400
    all_projects, all_agents = [], []
    for hc in cfg["hosts"]:
        name = hc["name"]
        try:
            if hc.get("local"):
                entries = scan_local(hc["roots"], settings["excludes"], window)
                agents = scan_agents_local(name, agent_cfg) + scan_web_tabs(agent_cfg)
            else:
                entries, agents = scan_remote(name, hc["ssh"], hc["roots"],
                                              settings["excludes"], window,
                                              agent_cfg, mac=hc["os"] == "mac")
            all_projects += build_projects(name, hc["roots"], entries, window).values()
            all_agents += agents
        except Exception as e:
            print(f"[{name}] scan failed: {e}", file=sys.stderr)
    all_projects.sort(key=lambda p: -p.latest_mtime)
    now = time.time()
    print(f"{'PROJECT':<24} {'HOST':<6}" +
          "".join(f"{lbl:>6}" for lbl, _ in HORIZONS) +
          f"  {'ACTIVITY 24h':<24}  LAST FILE")
    for p in all_projects:
        print(f"{p.name:<24.24} {p.host:<6}" +
              "".join(f"{p.counts.get(lbl, 0):>6}" for lbl, _ in HORIZONS) +
              f"  {sparkline(p.buckets)}  {trunc_path(p.latest_file, 50)}"
              f" ({fmt_age(now - p.latest_mtime)} ago)")
    if all_agents:
        apply_policies(all_agents, load_state(cfg)[0])
        print(f"\n{'AGENT':<8} {'HOST':<6} {'PID':<7} {'STATE':<8} {'POLICY':<8} "
              f"{'IDLE':<6} {'WHERE':<24} CWD")
        for a in sorted(all_agents, key=lambda x: (x.host, x.name)):
            st = agent_state(a, agent_cfg["idle_after"])
            where = (f"tmux:{a.tmux}" if a.tmux
                     else a.tty + (" DETACHED" if a.detached else ""))
            print(f"{a.name:<8.8} {a.host:<6} {a.pid:<7} {st:<8} {a.policy:<8} "
                  f"{fmt_age(a.eff_idle()):<6} {where:<24.24} {a.cwd}")
    else:
        print("\nno coding agents detected")


def main():
    ap = argparse.ArgumentParser(prog="prodder", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"prodder {__version__}")
    # Config belongs to the project/working directory, not site-packages.
    ap.add_argument("--config", default=str(Path.cwd() / "prodtop.toml"))
    ap.add_argument("--once", action="store_true",
                    help="print one snapshot as plain text and exit")
    ap.add_argument("--test-remote", action="store_true",
                    help="test the Telegram/WhatsApp connection and exit")
    ap.add_argument("--setup", action="store_true",
                    help="guided setup for phone answering (Telegram/WhatsApp)")
    ap.add_argument("--tui", action="store_true",
                    help="curses interface in this terminal instead of the "
                         "web dashboard")
    ap.add_argument("--port", type=int,
                    help="dashboard port (default: [web] port, 8737)")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve the dashboard without opening a browser")
    ap.add_argument("--demo", action="store_true",
                    help="run a self-contained demo with a simulated fleet — "
                         "no config, no ssh, nothing real is touched")
    args = ap.parse_args()
    if args.demo:
        cfg = demo_config()
        if args.port:
            cfg["web"]["port"] = args.port
        print("prodder demo — a simulated fleet; nothing real is touched.",
              file=sys.stderr)
        web(cfg, open_browser=not args.no_browser, demo=True)
        return
    example_candidates = (
        Path.cwd() / "prodtop.example.toml",
        Path(__file__).parent / "prodtop.example.toml",
        Path(sys.prefix) / "share" / "prodder" / "prodtop.example.toml",
    )
    example = next((p for p in example_candidates if p.exists()), None)
    if args.setup:
        if not os.path.exists(args.config):
            if example:
                shutil.copy(example, args.config)
                print(f"created {args.config} from the template")
            else:
                ap.error("no prodtop.example.toml found; provide --config")
        setup_wizard(load_config(args.config), args.config)
        return
    if not os.path.exists(args.config):
        if example:
            print(f"No prodtop.toml here yet.\n"
                  f"  • Try it risk-free:   prodder --demo   "
                  f"(a simulated fleet; nothing real is touched)\n"
                  f"  • Set it up for real: cp {example} prodtop.toml   "
                  f"(then edit its hosts/roots)\n"
                  f"Starting from the bundled example for now — scanning "
                  f"~/Projects, all remote hosts disabled.\n", file=sys.stderr)
            args.config = str(example)
        else:
            ap.error(f"{args.config} not found; create it, pass --config, "
                     f"or try:  prodder --demo")
    cfg = load_config(args.config)
    if args.test_remote:
        test_remote(cfg)
    elif args.once:
        once(cfg)
    elif args.tui:
        tui(cfg)
    else:
        if args.port:
            cfg["web"]["port"] = args.port
        web(cfg, open_browser=not args.no_browser)


def set_remote_config(path, values):
    """Update keys inside the [remote] section of the config file, preserving
    all other content and comments. Appends the section if missing."""
    text = Path(path).read_text()

    def fmt(v):
        return json.dumps(v) if isinstance(v, str) else \
            ("true" if v is True else "false" if v is False else str(v))

    if "[remote]" not in text:
        text += ("\n[remote]\n"
                 + "".join(f"{k} = {fmt(v)}\n" for k, v in values.items()))
    else:
        start = text.index("[remote]")
        nxt = re.search(r"(?m)^\[", text[start + 1:])
        end = start + 1 + nxt.start() if nxt else len(text)
        seg = text[start:end]
        for k, v in values.items():
            if re.search(rf"(?m)^\s*{k}\s*=", seg):
                seg = re.sub(rf"(?m)^(\s*{k}\s*=\s*)[^#\n]*",
                             lambda m: m.group(1) + fmt(v) + "   ",
                             seg, count=1)
            else:
                seg = seg.replace("[remote]", f"[remote]\n{k} = {fmt(v)}", 1)
        text = text[:start] + seg + text[end:]
    Path(path).write_text(text)
    # This file now holds a Telegram bot token (a keystroke-injection
    # credential) — keep it readable only by its owner.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def setup_wizard(cfg, config_path):
    """Interactive one-paste-two-taps setup for Telegram (+ WhatsApp)."""
    rc = cfg["remote"]
    print("prodtop remote setup — Telegram (two-way) + optional WhatsApp "
          "(notify-only)\n")
    print("NOTE: quit any running prodtop first — its poller would swallow "
          "the messages this setup waits for.\n")
    values = {}

    # ---- Telegram
    tok = rc.get("telegram_token", "")
    if tok:
        keep = input("A Telegram bot token is already configured — keep it? "
                     "[Y/n] ").strip().lower()
        if keep in ("n", "no"):
            tok = ""
    if not tok:
        print("STEP 1 — create your bot (Telegram is opening):")
        print("   send @BotFather:  /newbot   then answer its two questions")
        print("   (any display name; the username must end in 'bot')")
        print("   link: https://t.me/BotFather")
        try:
            webbrowser.open("https://t.me/BotFather")
        except Exception:
            pass
        tok = input("\nPaste the HTTP API token BotFather gave you "
                    "(Enter = skip Telegram): ").strip()
    if tok:
        try:
            me = tg_api(tok, "getMe", {})["result"]
        except Exception as e:
            print(f"  ✗ token check failed: {e}")
            return
        print(f"  ✓ token valid — bot @{me['username']}")
        values["telegram_token"] = tok
        chat = rc.get("telegram_chat_id", "") if tok == rc.get("telegram_token") else ""
        if not chat:
            link = f"https://t.me/{me['username']}"
            print(f"STEP 2 — open your bot and press START (link: {link})")
            try:
                webbrowser.open(link)
            except Exception:
                pass
            print("   waiting up to 120 s for your first message ...")
            offset, deadline = 0, time.time() + 120
            while time.time() < deadline and not chat:
                try:
                    ups = tg_api(tok, "getUpdates",
                                 {"offset": offset, "timeout": 20}, timeout=40)
                except Exception:
                    break
                for u in ups.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message")
                    if m:
                        chat = str(m["chat"]["id"])
                        who = m.get("from", {}).get("first_name", "")
                        print(f"  ✓ found you ({who}), chat id {chat}")
                        break
            if not chat:
                print("  ✗ nothing received — token saved; rerun --setup "
                      "to finish")
        if chat:
            values["telegram_chat_id"] = chat
            try:
                tg_api(tok, "sendMessage",
                       {"chat_id": chat,
                        "text": "✅ prodtop connected — decision menus will "
                                "arrive here with answer buttons"})
                print("  ✓ confirmation sent to your Telegram")
            except Exception as e:
                print(f"  test message failed: {e}")

    # ---- WhatsApp
    wa = input("\nAdd WhatsApp pings too? Outbound-only, via the third-party "
               "callmebot.com (prompt text transits their server). "
               "[y/N] ").strip().lower()
    if wa in ("y", "yes"):
        msg = urllib.parse.quote("I allow callmebot to send me messages")
        link = f"https://wa.me/34644519523?text={msg}"
        print("STEP 1 — WhatsApp is opening with a prefilled message to "
              "CallMeBot — just press send.")
        print(f"   link: {link}")
        try:
            webbrowser.open(link)
        except Exception:
            pass
        print("   CallMeBot replies (usually within a minute) with an apikey.")
        phone = input("Your WhatsApp number incl. country code "
                      "(e.g. +41791234567): ").strip()
        key = input("Paste the apikey CallMeBot sent you: ").strip()
        if phone and key:
            values["whatsapp_phone"] = phone
            values["whatsapp_apikey"] = key
            wa_notify(phone, key, "prodtop: WhatsApp notifications connected ✅")
            print("  ✓ test ping sent (check WhatsApp)")

    # ---- iMessage
    im = input("\nAdd iMessage? Two-way, no extra apps — prodtop messages "
               "your own number and reads replies locally. [y/N] ").strip().lower()
    if im in ("y", "yes"):
        handle = input("Your iMessage handle (the phone number or Apple ID "
                       "email you message yourself with, e.g. +41791234567): "
                       ).strip()
        if handle:
            if imsg_send(handle, "✅ prodtop iMessage connected"):
                print("  ✓ test message sent")
            else:
                print("  ✗ send failed — is Messages signed in? "
                      "(saving anyway; retest with --test-remote)")
            if imsg_max_rowid() is None:
                print("  ⚠ cannot read replies yet: grant your terminal app "
                      "Full Disk Access\n    (System Settings → Privacy & "
                      "Security → Full Disk Access), then restart it")
            else:
                print("  ✓ reply reading OK")
            values["imessage_handle"] = handle

    if values:
        values["enabled"] = True
        set_remote_config(config_path, values)
        print(f"\n✓ saved to {config_path} — remote answering ENABLED.")
        print("Start prodtop; agents' decision menus will now reach your phone.")
    else:
        print("\nnothing configured — config unchanged")


def test_remote(cfg):
    rc = cfg["remote"]
    if not rc["telegram_token"]:
        print("No telegram_token set. Setup:\n"
              "  1. In Telegram, talk to @BotFather → /newbot → copy the token\n"
              "  2. Put it in prodtop.toml under [remote] telegram_token\n"
              "  3. Run --test-remote again to discover your chat id")
        return
    if not rc["telegram_chat_id"]:
        print("No telegram_chat_id set. Send your bot any message now — "
              "waiting up to 60s ...")
        offset, deadline = 0, time.time() + 60
        while time.time() < deadline:
            ups = tg_api(rc["telegram_token"], "getUpdates",
                         {"offset": offset, "timeout": 20}, timeout=40)
            for u in ups.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message", {})
                frm = m.get("from", {})
                if m:
                    print(f"  found chat id {m['chat']['id']} "
                          f"({frm.get('first_name', '')} @{frm.get('username', '')})"
                          f" — put it in [remote] telegram_chat_id")
                    return
        print("  nothing received — did you message the right bot?")
        return
    tg_api(rc["telegram_token"], "sendMessage",
           {"chat_id": rc["telegram_chat_id"],
            "text": "✅ prodtop remote answering is connected"})
    print("Telegram: test message sent")
    if rc["whatsapp_phone"] and rc["whatsapp_apikey"]:
        wa_notify(rc["whatsapp_phone"], rc["whatsapp_apikey"],
                  "prodtop: WhatsApp notifications connected")
        print("WhatsApp: test message sent (via CallMeBot)")
    else:
        print("WhatsApp: not configured (optional; see prodtop.toml [remote])")
    if rc["imessage_handle"]:
        ok = imsg_send(rc["imessage_handle"], "✅ prodtop iMessage connected")
        print(f"iMessage: {'test message sent' if ok else 'send FAILED'}")
        if imsg_max_rowid() is None:
            print("iMessage: cannot read replies — grant your terminal app "
                  "Full Disk Access\n  (System Settings → Privacy & Security "
                  "→ Full Disk Access)")
        else:
            print("iMessage: reply reading OK (Full Disk Access granted)")
    else:
        print("iMessage: not configured (optional; see prodtop.toml [remote])")


if __name__ == "__main__":
    main()
