#!/usr/bin/env python3
"""prodtop — btop-style activity monitor for file-producing projects.

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
import curses
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import webbrowser
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

SPARK_CHARS = " ▁▂▃▄▅▆▇█"
SPARK_BUCKETS = 24          # 24 x 1h sparkline
MAX_RECENT_FILES = 12

SSH_BASE = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/prodtop-%C",
    "-o", "ControlPersist=600",
]

# Types text into a Terminal.app tab or iTerm2 session identified by its tty.
# argv: tty, text, withCR ("1"/"0"). Any Enter is sent SEPARATELY after a
# delay, and as a real carriage return (0x0D): TUI agents (claude/codex input
# boxes) treat text+newline arriving in one burst as a paste, and a plain
# newline is a line feed that composers map to "insert newline" — either way
# the nudge would sit unsubmitted in the prompt.
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
    tell application "iTerm2"
      repeat with w in windows
        repeat with tb in tabs of w
          repeat with s in sessions of tb
            if (tty of s) is theTTY then
              if theText is not "" then
                tell s to write text theText newline NO
              end if
              if withCR is "1" then
                delay 0.6
                tell s to write text (character id 13) newline NO
                delay 0.4
                tell s to write text (character id 13) newline NO
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

# Returns the scrollback/screen of the Terminal.app tab or iTerm2 session
# owning the given tty.
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
    tell application "iTerm2"
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

# Returns comma-separated ttys of all Terminal.app tabs and iTerm2 sessions —
# the authoritative "which ttys still have a window" list (process ancestry
# lies: asciinema wrappers get reparented to launchd while their window lives).
OSA_LIST_TTYS = '''
set out to ""
tell application "System Events"
  set hasTerm to (count of (processes whose name is "Terminal")) > 0
  set hasIterm to (count of (processes whose name is "iTerm2")) > 0
end tell
if hasTerm then
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        set out to out & (tty of t) & ","
      end repeat
    end repeat
  end tell
end if
if hasIterm then
  tell application "iTerm2"
    repeat with w in windows
      repeat with tb in tabs of w
        repeat with s in sessions of tb
          set out to out & (tty of s) & ","
        end repeat
      end repeat
    end repeat
  end tell
end if
return out
'''

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

# Opens a new terminal window and runs a shell command in it (iTerm2 first,
# Terminal.app as fallback). Plain newline is fine here — the target is a
# shell prompt, not a TUI composer.
OSA_OPEN = '''
on run argv
  set theCmd to item 1 of argv
  try
    tell application "iTerm2"
      activate
      set w to (create window with default profile)
      -- let the shell finish its init/MOTD first, or the typed command is
      -- flushed away with the startup output
      delay 3
      tell current session of w to write text theCmd
      return "ok"
    end tell
  end try
  try
    tell application "Terminal"
      activate
      do script theCmd
      return "ok"
    end tell
  end try
  return "fail"
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
    tmux: str = ""      # session name, "" = not in tmux
    pane: str = ""      # tmux pane id (%N)
    sock: str = ""      # tmux socket path (remote multi-user servers)
    prod_tty: str = ""  # tty to type into (outermost tty in the process chain;
                        # differs from tty when wrapped in asciinema/script)
    idle: float = -1.0  # seconds without terminal activity at scan time; -1 unknown
    scanned_at: float = 0.0
    protected: bool = False
    detached: bool = False  # wrapper orphaned, no terminal window owns the tty
    policy: str = "auto"    # auto (proddable) | ignore (leave alone)
    label: str = ""         # display label (web tabs: the tab title)

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
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    s = cfg.setdefault("settings", {})
    s.setdefault("window_days", 3)
    s.setdefault("local_interval", 45)
    s.setdefault("remote_interval", 60)
    s.setdefault("excludes", [".git", "node_modules", ".venv", "venv", "__pycache__"])
    a = cfg.setdefault("agents", {})
    a.setdefault("process_names", ["claude", "codex", "chatgpt", "aider", "gemini", "goose"])
    a.setdefault("idle_after", 600)
    a.setdefault("nudge", "continue")
    a.setdefault("approve_prompts", True)
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
    r.setdefault("menu_idle", 90)       # agent quiet this long + menu on screen -> notify
    r.setdefault("remote_timeout", 300)  # hold auto-approve this long awaiting a phone answer
    a.setdefault("auto_prod", False)
    a.setdefault("prod_cooldown", 900)
    a.setdefault("protected", [])
    cfg["hosts"] = [h for h in cfg.get("hosts", []) if h.get("enabled", True)]
    for h in cfg["hosts"]:
        h["roots"] = [os.path.expanduser(r) for r in h.get("roots", [])]
    cfg["_dir"] = str(Path(path).resolve().parent)
    return cfg


# ------------------------------------------------ per-project prod policies
# Policies are keyed "host|/path/to/project" and apply to any agent whose cwd
# is inside that path — they survive restarts, reopens and pid changes.

def load_policies(cfg):
    try:
        with open(Path(cfg["_dir"]) / "prodtop-state.json") as f:
            return json.load(f).get("policies", {})
    except (OSError, ValueError):
        return {}


def save_policies(cfg, policies):
    try:
        with open(Path(cfg["_dir"]) / "prodtop-state.json", "w") as f:
            json.dump({"policies": policies}, f, indent=1)
    except OSError:
        pass


def match_policy(host, path, policies):
    """Most-specific policy whose 'host|prefix' contains path."""
    best_len, best = -1, "auto"
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
    return (not a.protected and a.policy != "ignore"
            and agent_state(a, idle_after) == "stalled")


def primary_agent(p):
    return min(p.agents, key=lambda x: x.eff_idle() if x.eff_idle() >= 0 else 1e12)


def agent_regex(names):
    return re.compile(r"(?:^|[/\s])(" + "|".join(re.escape(n) for n in names) + r")(?:\s|$)")


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


TERMINAL_APPS_RX = re.compile(r"iTerm|Terminal|Ghostty|kitty|[Aa]lacritty|wezterm")


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
    """Set of ttys owned by a live Terminal/iTerm tab, or None if unknown."""
    try:
        r = subprocess.run(["osascript", "-e", OSA_LIST_TTYS],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return {t.strip() for t in r.stdout.split(",") if t.strip()}
    except (OSError, subprocess.SubprocessError):
        pass
    return None


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


def scan_agents_local(host_name, agent_cfg):
    now = time.time()
    rx = agent_regex(agent_cfg["process_names"])
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,pcpu=,args="],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    rows, procs = [], {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, tty, cpu, args = parts
        try:
            procs[int(pid)] = (int(ppid), tty, args.split()[0] if args else "")
        except ValueError:
            continue
        if tty in ("??", "?", "-") or "prodtop" in args:
            continue
        m = rx.search(args)
        if not m:
            continue
        try:
            rows.append((int(pid), "/dev/" + tty, float(cpu), m.group(1)))
        except ValueError:
            pass
    cwds = {}
    if rows:
        try:
            lo = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p",
                 ",".join(str(p) for p, _, _, _ in rows), "-Fn"],
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
    panes = list_local_tmux_panes()
    window_ttys = list_window_ttys()
    agents = []
    for pid, tty, cpu, name in rows:
        a = AgentProc(host=host_name, ssh="", pid=pid, name=name,
                      cwd=cwds.get(pid, ""), tty=tty, cpu=cpu, scanned_at=now)
        ot, attached = outer_tty(pid, procs)
        a.prod_tty = "/dev/" + ot if ot else tty
        if window_ttys is not None:
            a.detached = a.prod_tty not in window_ttys
        else:
            a.detached = not attached
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
    return dedupe_by_tty(agents)


def remote_command(roots, excludes, days):
    prune = " -o ".join(f"-name {shlex.quote(x)}" for x in excludes)
    roots_q = " ".join(shlex.quote(r) for r in roots)
    find_part = (f"find {roots_q} \\( {prune} \\) -prune -o "
                 f"-type f -mtime -{days} -printf '%T@\\t%p\\n' 2>/dev/null")
    return (
        find_part + "; "
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


def scan_remote(host_name, ssh_target, roots, excludes, window, agent_cfg):
    """One ssh round-trip: recent files + agent processes + tmux + tty idle."""
    days = max(1, int(window // 86400))
    out = subprocess.run(SSH_BASE + [ssh_target, remote_command(roots, excludes, days)],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip()
                           else f"ssh exit {out.returncode}")
    now = time.time()
    sections = {"FIND": [], "AGENTS": [], "TMUX": [], "CWD": [], "TTY": []}
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

    rx = agent_regex(agent_cfg["process_names"])
    agents = []
    for line in sections["AGENTS"]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, cpu, args = parts
        if tty in ("?", "-"):
            continue
        m = rx.search(args)
        if not m:
            continue
        dev = "/dev/" + tty
        try:
            a = AgentProc(host=host_name, ssh=ssh_target, pid=int(pid),
                          name=m.group(1), cwd=cwds.get(pid, ""), tty=dev,
                          cpu=float(cpu), scanned_at=now)
        except ValueError:
            continue
        pane = panes.get(dev)
        if pane:
            a.pane, a.tmux, a.sock = pane["pane"], pane["session"], pane["sock"]
            a.idle = max(0.0, now - pane["activity"]) if pane["activity"] else -1.0
        elif dev in tty_mtimes:
            a.idle = max(0.0, now - tty_mtimes[dev])
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
                                                  excludes, window, self.agent_cfg)
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


def send_to_terminal(agent, text, submit=True):
    """Deliver text (and optionally a submitting Enter) to the agent's
    terminal. text of "\\x1b" sends a bare Escape. Returns None on success,
    an error string otherwise."""
    try:
        if agent.pane:
            if text == "\x1b":
                parts = [["send-keys", "-t", agent.pane, "Escape"]]
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
        if not agent.ssh:
            tty = agent.prod_tty or agent.tty
            r = subprocess.run(
                ["osascript", "-", tty, text, "1" if submit and text != "\x1b" else "0"],
                input=OSA_SEND, capture_output=True, text=True, timeout=30)
            res = r.stdout.strip()
            if res == "ok":
                return None
            if res == "notfound":
                return (f"no terminal tab owns {tty} — session is detached "
                        f"(closed window?) or in an unscriptable terminal")
            return f"send failed: {(r.stderr or res).strip()[:80]}"
        return f"can't reach {agent.name} on {agent.host}: not in tmux"
    except (OSError, subprocess.SubprocessError) as e:
        return f"send failed: {e}"


def prod_agent(agent, nudge, approve_prompts=True, never_approve=()):
    """Unstick an agent: if it is sitting on an approval prompt, press Enter
    to accept the default "Yes"; otherwise type the nudge text + Enter.
    Prompts matching a never_approve pattern are left for a human."""
    if agent.protected:
        return f"'{agent.tmux}' is a protected session — not prodding"
    if agent.web:
        return prod_web(agent, nudge)
    text, verb = nudge, "prodded"
    if approve_prompts:
        tail = "\n".join(capture_screen(agent).splitlines()[-30:])
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


class RemoteAnswerer(threading.Thread):
    """Watches quiet agents for on-screen choice menus, pushes them to
    Telegram (buttons) and optionally WhatsApp (notify-only), and types
    answers back into the right terminal. Only the configured chat id is
    obeyed."""

    def __init__(self, cfg, state, lock, msgs, pending_remote, policies):
        super().__init__(daemon=True)
        self.rc, self.agent_cfg = cfg["remote"], cfg["agents"]
        self.state, self.lock, self.msgs = state, lock, msgs
        self.pending_remote, self.policies = pending_remote, policies
        self.offset = 0
        self.notified, self.last_check = {}, {}
        self.last_target = None
        self.stop = False

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
        if self.rc["telegram_chat_id"]:
            self._tg("sendMessage", {"chat_id": self.rc["telegram_chat_id"],
                                     "text": text})

    def check_menus(self):
        with self.lock:
            agents = [a for hs in self.state.values() for a in hs.agents]
            apply_policies(agents, self.policies)
        now = time.time()
        for a in agents:
            key = (a.host, a.tty)
            if a.web or a.protected or a.policy == "ignore":
                continue
            if a.eff_idle() < self.rc["menu_idle"]:
                self.notified.pop(key, None)
                continue
            if now - self.last_check.get(key, 0) < 60:
                continue
            self.last_check[key] = now
            menu, opts = extract_menu(capture_screen(a))
            if not menu:
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
            if self.rc["telegram_chat_id"]:
                self._tg("sendMessage",
                         {"chat_id": self.rc["telegram_chat_id"], "text": text,
                          "reply_markup": {"inline_keyboard": [buttons]}})
            if self.rc["whatsapp_phone"] and self.rc["whatsapp_apikey"]:
                wa_notify(self.rc["whatsapp_phone"], self.rc["whatsapp_apikey"],
                          text + "\n(answer via Telegram)")
            self.msgs.append(f"📱 prompt of {a.name}/{proj} sent to phone")

    def poll_replies(self):
        if not (self.rc["telegram_token"] and self.rc["telegram_chat_id"]):
            time.sleep(15)
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
                self._tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
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
        if not agent.ssh:
            r = subprocess.run(["osascript", "-", agent.prod_tty or agent.tty],
                               input=OSA_CAPTURE, capture_output=True, text=True,
                               timeout=25)
            return r.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


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
    try:
        r = subprocess.run(["osascript", "-", cmd], input=OSA_OPEN,
                           capture_output=True, text=True, timeout=30)
        opened = r.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        opened = False
    return msg + ("; reopened in a new terminal window" if opened
                  else "; could NOT open a new terminal — resume cmd is in the log")


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
    if agent.protected:
        return "prot"
    idle = agent.eff_idle()
    if idle < 0:
        return "unknown"
    return "run" if idle < idle_after else "stalled"


def agent_cell(p, idle_after):
    if not p.agents:
        return ""
    a = primary_agent(p)
    st = agent_state(a, idle_after)
    tag = {"prot": "🔒", "run": "●", "stalled": "⚠", "unknown": "·"}[st]
    if a.detached:
        tag = "⊗"
    if a.policy == "ignore":
        tag = "⊘"
    s = f"{tag}{a.name} {fmt_age(a.eff_idle())}"
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
                        pseudo[key] = Project(name=name, host=a.host,
                                              root=os.path.dirname(a.cwd.rstrip("/")),
                                              latest_file="(no recent files)",
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


def draw(scr, state, lock, sel, sort_by, agent_cfg, ui_msg, policies):
    now = time.time()
    idle_after = agent_cfg["idle_after"]
    scr.erase()
    h, w = scr.getmaxyx()
    projects, hosts, agents = gather_rows(state, lock, sort_by, idle_after, policies)

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
    put(1, 0, f" sort: {sort_by}  auto-prod: {auto}   [p] prod  [i] leave alone  "
              f"[a] re-arm  [x] close+save  [o] reopen detached  [s] sort  "
              f"[r] rescan  [q] quit",
        curses.color_pair(3))

    # column layout
    name_w, host_w, mode_w, agent_w, cnt_w = 22, 5, 6, 16, 6
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
                f" {agent_cell(p, idle_after):<{agent_w}.{agent_w}}"
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
        put(h - detail_h + 1, 1,
            f"{p.host}:{p.root}/{p.name} — newest files:", curses.A_BOLD)
        for i, (mt, rel) in enumerate(p.recent[:detail_h - 4]):
            put(h - detail_h + 2 + i, 3,
                f"{fmt_age(now - mt):>4} ago  {rel}", curses.color_pair(3))
        for j, a in enumerate(p.agents[:2]):
            where = (f"tmux {a.tmux}" if a.tmux
                     else a.tty + (" DETACHED — [o] to reopen" if a.detached else ""))
            put(h - detail_h + 2 + min(len(p.recent), detail_h - 4) + j, 3,
                f"agent: {a.name} pid {a.pid} in {where}, "
                f"idle {fmt_age(a.eff_idle())}, cpu {a.cpu:.0f}%"
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


def auto_prod_pass(agents, agent_cfg, last_prod, msgs,
                   pending_remote=None, remote_timeout=300):
    now = time.time()
    for a in agents:
        if not effective_stalled(a, agent_cfg["idle_after"]):
            continue
        if pending_remote and now - pending_remote.get((a.host, a.tty), 0) \
                < remote_timeout:
            continue        # a human was asked on the phone — hold off
        key = (a.host, a.pid)
        if now - last_prod.get(key, 0) < agent_cfg["prod_cooldown"]:
            continue
        msg = prod_agent(a, agent_cfg["nudge"], agent_cfg["approve_prompts"],
                         agent_cfg["never_approve"])
        # an approved prompt usually precedes more work (or another prompt) —
        # recheck soon instead of waiting out the full cooldown
        last_prod[key] = (now - agent_cfg["prod_cooldown"] + 90
                          if "approved prompt" in msg else now)
        msgs.append(f"[auto {time.strftime('%H:%M')}] " + msg)


def tui(cfg):
    state, lock = {}, threading.Lock()
    wake = threading.Event()
    scanners = []
    for hc in cfg["hosts"]:
        state[hc["name"]] = HostState(name=hc["name"])
        t = Scanner(hc, cfg["settings"], cfg["agents"], state, lock, wake)
        scanners.append(t)
        t.start()
    last_prod, msgs = {}, deque(maxlen=5)
    last_auto = 0.0
    policies = load_policies(cfg)
    pending_remote = {}
    remote_thread = None
    if cfg["remote"]["enabled"] and cfg["remote"]["telegram_token"]:
        remote_thread = RemoteAnswerer(cfg, state, lock, msgs,
                                       pending_remote, policies)
        remote_thread.start()

    def main(scr):
        nonlocal last_auto
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        for i, c in [(1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW),
                     (3, 8 if curses.COLORS > 8 else curses.COLOR_WHITE),
                     (4, curses.COLOR_CYAN), (5, curses.COLOR_RED)]:
            curses.init_pair(i, c, -1)
        scr.timeout(500)
        sel = 0
        sort_by = "recency"
        pending = None      # ("close"|"reopen", agent)

        def sel_agent(rows):
            if rows and rows[sel].agents:
                return rows[sel], primary_agent(rows[sel])
            if rows:
                msgs.append(f"no agent running in {rows[sel].name}")
            return None, None

        def drop_from_state(a):
            with lock:
                for hs in state.values():
                    hs.agents = [x for x in hs.agents
                                 if (x.host, x.tty) != (a.host, a.tty)]

        while True:
            ui_msg = msgs[-1] if msgs else ""
            sel, rows = draw(scr, state, lock, sel, sort_by, cfg["agents"],
                             ui_msg, policies)
            if cfg["agents"]["auto_prod"] and time.time() - last_auto > 10:
                last_auto = time.time()
                with lock:
                    agents = [a for hs in state.values() for a in hs.agents]
                    apply_policies(agents, policies)
                auto_prod_pass(agents, cfg["agents"], last_prod, msgs,
                               pending_remote, cfg["remote"]["remote_timeout"])
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
            elif ch in (ord("k"), curses.KEY_UP):
                sel = max(sel - 1, 0)
            elif ch == ord("s"):
                sort_by = "24h" if sort_by == "recency" else "recency"
            elif ch == ord("r"):
                wake.set()
            elif ch == ord("p"):
                _, a = sel_agent(rows)
                if a:
                    msgs.append(f"[{time.strftime('%H:%M')}] "
                                + prod_agent(a, cfg["agents"]["nudge"],
                                             cfg["agents"]["approve_prompts"],
                                             cfg["agents"]["never_approve"]))
                    last_prod[(a.host, a.pid)] = time.time()
            elif ch == ord("i") and rows:
                p = rows[sel]
                policies[f"{p.host}|{row_path(p)}"] = "ignore"
                save_policies(cfg, policies)
                msgs.append(f"{p.name}: leave alone (no prods, incl. future agents)")
            elif ch == ord("a") and rows:
                p = rows[sel]
                policies.pop(f"{p.host}|{row_path(p)}", None)
                save_policies(cfg, policies)
                msgs.append(f"{p.name}: PRODDING enabled")
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
    finally:
        for t in scanners:
            t.stop = True
        if remote_thread:
            remote_thread.stop = True
        wake.set()


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
                                              settings["excludes"], window, agent_cfg)
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
        apply_policies(all_agents, load_policies(cfg))
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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(Path(__file__).parent / "prodtop.toml"))
    ap.add_argument("--once", action="store_true",
                    help="print one snapshot as plain text and exit")
    ap.add_argument("--test-remote", action="store_true",
                    help="test the Telegram/WhatsApp connection and exit")
    ap.add_argument("--setup", action="store_true",
                    help="guided setup for phone answering (Telegram/WhatsApp)")
    args = ap.parse_args()
    if args.setup:
        if not os.path.exists(args.config):
            example = Path(__file__).parent / "prodtop.example.toml"
            if example.exists():
                shutil.copy(example, args.config)
                print(f"created {args.config} from the template")
        setup_wizard(load_config(args.config), args.config)
        return
    if not os.path.exists(args.config):
        example = Path(__file__).parent / "prodtop.example.toml"
        if example.exists():
            print(f"{args.config} not found — using {example}; copy it to "
                  f"prodtop.toml and edit your hosts", file=sys.stderr)
            args.config = str(example)
    cfg = load_config(args.config)
    if args.test_remote:
        test_remote(cfg)
    elif args.once:
        once(cfg)
    else:
        tui(cfg)


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


if __name__ == "__main__":
    main()
