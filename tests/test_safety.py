"""Safety-critical decision logic — the behaviour that must never silently
regress: which sessions are hands-off, which prompts may be auto-approved,
which screens are 'finished' (get a gentle reassess, not a blunt continue),
and which screens are numbered menus (pushed to a human, never auto-prodded).

These are pure functions, so no terminals/subprocesses are touched. prod_agent
is exercised only on paths that return BEFORE send_to_terminal, and where a
send could happen it is stubbed, so nothing is ever typed into a real tty.
"""
import types
import pytest

import prodtop


def agent(**kw):
    """Minimal stand-in for AgentProc — only the attributes prod_agent and
    send_to_terminal actually read. A SimpleNamespace keeps the tests robust
    to changes in the AgentProc dataclass definition."""
    base = dict(protected=False, web=False, pane="", tmux="", ssh="", sock="",
                prod_tty="/dev/ttys001", tty="/dev/ttys001", host="local",
                name="claude")
    base.update(kw)
    return types.SimpleNamespace(**base)


# ---------------------------------------------------------------- protected

def test_is_protected_matches_glob():
    assert prodtop.is_protected("cockpit-3", ["cockpit-*", "vps-batch"])
    assert prodtop.is_protected("vps-batch", ["cockpit-*", "vps-batch"])


def test_is_protected_no_false_positive():
    assert not prodtop.is_protected("my-project", ["cockpit-*", "vps-batch"])
    assert not prodtop.is_protected("", ["*"])          # empty name never matches


def test_protected_agent_is_never_prodded(monkeypatch):
    # If prod_agent ever reached send_to_terminal for a protected agent, that
    # would type into a fenced-off fleet session. It must return first.
    def boom(*a, **k):
        raise AssertionError("send_to_terminal must not run for a protected agent")
    monkeypatch.setattr(prodtop, "send_to_terminal", boom)
    msg = prodtop.prod_agent(agent(protected=True, tmux="cockpit-1"),
                             "continue", approve_prompts=True,
                             never_approve=[], screen="1. Yes, proceed")
    assert "protected" in msg.lower()


# ------------------------------------------------------------ never_approve

DANGEROUS_SCREEN = (
    "The agent wants to run a command:\n"
    "    sudo rm -rf /var/tmp/build\n"
    "Would you like to run this command?\n"
    "  1. Yes, proceed\n"
    "  2. No\n"
)

SAFE_SCREEN = (
    "The agent wants to run a command:\n"
    "    ls -la\n"
    "Would you like to run this command?\n"
    "  1. Yes, proceed\n"
    "  2. No\n"
)


def test_never_approve_blocks_dangerous_prompt(monkeypatch):
    monkeypatch.setattr(prodtop, "send_to_terminal",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not auto-approve a never_approve prompt")))
    msg = prodtop.prod_agent(agent(), "continue", approve_prompts=True,
                             never_approve=["rm -rf", "sudo "],
                             screen=DANGEROUS_SCREEN)
    assert "never_approve" in msg
    assert "human" in msg.lower()


def test_safe_prompt_is_approved(monkeypatch):
    sent = {}
    def fake_send(ag, text, submit=True):
        sent["text"] = text          # empty text == "just press Enter to accept"
        return None
    monkeypatch.setattr(prodtop, "send_to_terminal", fake_send)
    msg = prodtop.prod_agent(agent(), "continue", approve_prompts=True,
                             never_approve=["rm -rf", "sudo "],
                             screen=SAFE_SCREEN)
    assert sent["text"] == ""        # accepts the default Yes, doesn't type a nudge
    assert "approved prompt of" in msg


def test_no_approve_when_flag_off(monkeypatch):
    # approve_prompts=False: even on a prompt screen, prod types the nudge and
    # never presses the approval Yes.
    sent = {}
    monkeypatch.setattr(prodtop, "send_to_terminal",
                        lambda ag, text, submit=True: sent.__setitem__("text", text))
    prodtop.prod_agent(agent(), "continue", approve_prompts=False,
                       never_approve=[], screen=DANGEROUS_SCREEN)
    assert sent["text"] == "continue"


def test_default_never_approve_list_is_intact(tmp_path):
    # Golden guard: the shipped default must keep fencing off the classics, so
    # an edit that drops one is a deliberate, reviewed change.
    cfg_path = tmp_path / "prodtop.toml"
    cfg_path.write_text("[[hosts]]\nname='local'\nlocal=true\nroots=['~']\n")
    cfg = prodtop.load_config(str(cfg_path))
    never = " | ".join(cfg["agents"]["never_approve"]).lower()
    for needle in ("rm -rf", "sudo", "git push", "--force", "mkfs", "shutdown"):
        assert needle in never, f"{needle!r} missing from default never_approve"


# --------------------------------------------------------------- looks_done

@pytest.mark.parametrize("screen", [
    "…\nThe task is complete.",
    "- All done\n",
    "Is there anything else you'd like me to do?",
    "Let me know if you want changes.",
    "Shall I proceed with the refactor?",
    "Here's a summary of what I changed:",
    "I'll wait for your input.",
])
def test_looks_done_true(screen):
    assert prodtop.looks_done(screen)


@pytest.mark.parametrize("screen", [
    "Running tests…\nediting src/app.py",
    "Applying migration 0007",
    "we saw the task complete message in the logs",  # WEAK phrase mid-line: no match
    "installing dependencies",
    "",
])
def test_looks_done_false(screen):
    assert not prodtop.looks_done(screen)


# ------------------------------------------------------------ prompt + menu

@pytest.mark.parametrize("tail", [
    "  1. Yes, proceed",
    "Would you like to run this command?",
    "Do you want to proceed?",
])
def test_prompt_rx_matches(tail):
    assert prodtop.PROMPT_RX.search(tail)


def test_extract_menu_finds_numbered_choices():
    screen = (
        "How should I handle this?\n"
        "  1. Rebase onto main\n"
        "  2. Merge instead\n"
        "  3. Cancel\n"
    )
    text, opts = prodtop.extract_menu(screen)
    assert opts == ["1", "2", "3"]
    assert "Rebase" in text


def test_extract_menu_ignores_single_option():
    # A lone "1. …" is not a decision menu (needs >= 2 options).
    text, opts = prodtop.extract_menu("Note:\n  1. see the docs\n")
    assert opts == []
    assert text is None
