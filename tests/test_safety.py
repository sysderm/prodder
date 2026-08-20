"""Starter tests for prodder's safety-critical logic.

These cover the decisions where a bug is dangerous rather than cosmetic:
  * which agents may be auto-prodded at all (recognized / protected / ignore),
  * the never_approve guard that must stop an auto-"Yes" on rm -rf / sudo / …,
  * "looks done" detection, which downgrades a blunt prod to a reassess nudge,
  * numbered-menu detection, which routes a decision to a human instead,
  * agent-name matching (word boundaries; no substring false positives).

Stdlib only (unittest) — no third-party deps, so CI needs no install step, and
importing prodtop here on Linux is itself the cross-platform smoke test: it
proves the module loads with no curses and no macOS toolchain present.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prodtop  # noqa: E402


def agent(**kw):
    """An AgentProc with harmless defaults; override just what a test needs."""
    base = dict(host="local", ssh="", pid=1000, name="claude",
                cwd="/tmp/proj", tty="/dev/ttys001", cpu=0.0)
    base.update(kw)
    a = prodtop.AgentProc(**base)
    # Default to a fresh, deterministic idle unless the test set one.
    if "scanned_at" not in kw:
        a.scanned_at = time.time()
    return a


class AgentRegex(unittest.TestCase):
    def setUp(self):
        self.rx = prodtop.agent_regex(["claude", "codex", "aider"])

    def test_matches_bare_and_pathed(self):
        for s in ("claude", "/usr/local/bin/codex --resume", "python -m aider",
                  "claude.exe --print"):
            self.assertTrue(self.rx.search(s), s)

    def test_no_substring_false_positive(self):
        for s in ("claudette", "mycodex-wrapper", "raider", "aidermatic"):
            self.assertIsNone(self.rx.search(s), s)


class Protected(unittest.TestCase):
    def test_glob_match_and_empty(self):
        pats = ["vps-*", "cockpit-*", "agentd"]
        self.assertTrue(prodtop.is_protected("vps-batch", pats))
        self.assertTrue(prodtop.is_protected("cockpit-1", pats))
        self.assertFalse(prodtop.is_protected("my-shell", pats))
        self.assertFalse(prodtop.is_protected("", pats))   # no session name


class State(unittest.TestCase):
    def test_recognized_gating(self):
        # A plain terminal window is shown but is never a proddable "stalled".
        term = agent(recognized=False, idle=99999)
        self.assertEqual(prodtop.agent_state(term, 600), "term")
        self.assertFalse(prodtop.effective_stalled(term, 600))

    def test_stalled_vs_running(self):
        self.assertEqual(prodtop.agent_state(agent(idle=5), 600), "run")
        self.assertEqual(prodtop.agent_state(agent(idle=9000), 600), "stalled")

    def test_unknown_idle(self):
        self.assertEqual(prodtop.agent_state(agent(idle=-1), 600), "unknown")

    def test_effective_stalled_exclusions(self):
        self.assertTrue(prodtop.effective_stalled(agent(idle=9000), 600))
        self.assertFalse(prodtop.effective_stalled(
            agent(idle=9000, protected=True), 600))
        self.assertFalse(prodtop.effective_stalled(
            agent(idle=9000, policy="ignore"), 600))


class NeverApprove(unittest.TestCase):
    """The single most dangerous decision: auto-approving a command menu."""

    def setUp(self):
        self._orig = prodtop.send_to_terminal
        self.sent = []
        prodtop.send_to_terminal = lambda a, text, submit=True: (
            self.sent.append(text) or None)

    def tearDown(self):
        prodtop.send_to_terminal = self._orig

    def test_dangerous_prompt_is_never_auto_approved(self):
        scr = ("$ rm -rf build/\n"
               "Would you like to run this command?\n"
               "1. Yes, proceed\n2. No")
        a = agent(tty="/dev/ttys009", pane="%1", tmux="sess")
        msg = prodtop.prod_agent(a, "continue", approve_prompts=True,
                                 never_approve=["rm -rf", "sudo "], screen=scr)
        self.assertIn("never_approve", msg)
        self.assertEqual(self.sent, [])        # NOTHING was typed at the agent

    def test_safe_prompt_is_approved_with_empty_enter(self):
        scr = ("Run tests?\n1. Yes, proceed\n2. No")
        a = agent(tty="/dev/ttys009", pane="%1", tmux="sess")
        msg = prodtop.prod_agent(a, "continue", approve_prompts=True,
                                 never_approve=["rm -rf"], screen=scr)
        self.assertIn("approved", msg)
        self.assertEqual(self.sent, [""])      # bare Enter accepts the default

    def test_protected_agent_is_never_prodded(self):
        a = agent(protected=True, tmux="vps-batch")
        msg = prodtop.prod_agent(a, "continue")
        self.assertIn("protected", msg)
        self.assertEqual(self.sent, [])


class LooksDone(unittest.TestCase):
    def test_strong_handbacks(self):
        for s in ("All set. Is there anything else?",
                  "Shall I proceed with the refactor?",
                  "Let me know if you'd like changes."):
            self.assertTrue(prodtop.looks_done(s), s)

    def test_weak_only_at_line_start(self):
        self.assertTrue(prodtop.looks_done("Task complete"))
        # 'completed' mid-sentence in a log line must NOT trip it.
        self.assertFalse(prodtop.looks_done(
            "the migration script completed 3 of 40 files and continues"))

    def test_working_screen_is_not_done(self):
        self.assertFalse(prodtop.looks_done(
            "Editing src/app.py\nRunning tests...\n  12 passed"))


class Menu(unittest.TestCase):
    def test_numbered_menu_extracted(self):
        scr = ("Pick an approach:\n"
               "1. Rewrite it\n2. Patch it\n3. Leave it\n")
        text, opts = prodtop.extract_menu(scr)
        self.assertEqual(opts, ["1", "2", "3"])
        self.assertIn("Rewrite", text)

    def test_prose_is_not_a_menu(self):
        text, opts = prodtop.extract_menu(
            "I found 1 bug and 2 warnings while scanning.")
        self.assertIsNone(text)
        self.assertEqual(opts, [])


class DedupeByTty(unittest.TestCase):
    def test_keeps_lowest_pid_per_terminal(self):
        a = agent(pid=200, tty="/dev/ttys004")
        b = agent(pid=100, tty="/dev/ttys004")   # same tty, lower pid wins
        c = agent(pid=300, tty="/dev/ttys005")
        kept = {x.pid for x in prodtop.dedupe_by_tty([a, b, c])}
        self.assertEqual(kept, {100, 300})


if __name__ == "__main__":
    unittest.main()
